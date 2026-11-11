"""Event transports.

`HttpTransport` is the production path: `submit` is a non-blocking `put_nowait` onto
a bounded queue, and a daemon thread batches events to the collector. A thread
(rather than an asyncio task) is deliberate: Switchboard makes blocking LLM calls
and `time.sleep`s inside async code, which would starve an event-loop worker exactly
when it has the most to send.

Failure policy:
  - network error / 408 / 429 / 5xx -> keep the batch, retry with capped backoff
  - other 4xx                        -> drop the batch (retrying can't help), warn
  - queue full                       -> drop the new event, warn (rate-limited)
  - close() while collector is down  -> one last attempt, then drop and warn
Nothing here ever raises into the caller.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import httpx

log = logging.getLogger("switchboard_trace")

INGEST_PATH = "/v1/events"


class Transport(Protocol):
    def submit(self, event: dict[str, Any]) -> None: ...
    def flush(self, timeout: float = 5.0) -> bool: ...
    def close(self, timeout: float = 5.0) -> None: ...


class MemoryTransport:
    """Collects events in a list. For tests and local debugging."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def submit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def close(self, timeout: float = 5.0) -> None:
        pass


@dataclass
class TransportStats:
    sent: int = 0
    dropped_queue_full: int = 0
    dropped_rejected: int = 0
    dropped_on_close: int = 0
    failed_requests: int = 0


class _Outcome(Enum):
    SENT = "sent"
    DROP = "drop"
    RETRY = "retry"


class HttpTransport:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        max_queue: int = 10_000,
        max_batch: int = 500,
        linger_s: float = 0.02,
        request_timeout_s: float = 2.0,
        initial_backoff_s: float = 0.25,
        max_backoff_s: float = 5.0,
        warn_interval_s: float = 10.0,
        client: httpx.Client | None = None,
    ):
        self.url = endpoint.rstrip("/") + INGEST_PATH
        self.stats = TransportStats()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._max_batch = max_batch
        self._linger_s = linger_s
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._warn_interval_s = warn_interval_s
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=request_timeout_s)

        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._pending = 0  # submitted but not yet sent or dropped
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._last_warned: dict[str, float] = {}
        self._thread = threading.Thread(target=self._worker, name="switchboard-trace", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    # ------------------------------------------------------------ caller side

    def submit(self, event: dict[str, Any]) -> None:
        if self._stop.is_set():
            self.stats.dropped_on_close += 1
            return
        with self._cond:
            self._pending += 1
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.stats.dropped_queue_full += 1
            self._settle(1)
            self._warn(
                "full",
                "trace buffer full (%d events); dropping events (%d dropped so far)",
                self._q.maxsize, self.stats.dropped_queue_full,
            )

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything submitted so far is sent or dropped."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def close(self, timeout: float = 5.0) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout)
        if self._owns_client:
            self._client.close()
        atexit.unregister(self.close)

    # ------------------------------------------------------------ worker side

    def _worker(self) -> None:
        batch: list[dict[str, Any]] = []
        backoff = self._initial_backoff_s
        while True:
            try:
                if not batch:
                    batch = self._collect()
                    if not batch:
                        if self._stop.is_set():
                            return
                        continue
                outcome = self._send(batch)
                if outcome is not _Outcome.RETRY:
                    self._settle(len(batch))
                    batch, backoff = [], self._initial_backoff_s
                    continue
                if self._stop.is_set():
                    self._drop_on_close(batch)
                    return
                self._stop.wait(backoff)  # close() cuts the wait short
                backoff = min(backoff * 2, self._max_backoff_s)
            except Exception:  # the worker must outlive any bug in a single batch
                log.exception("switchboard-trace worker error; dropping %d events", len(batch))
                self._settle(len(batch))
                batch = []

    def _collect(self) -> list[dict[str, Any]]:
        try:
            batch = [self._q.get(timeout=0.1)]
        except queue.Empty:
            return []
        deadline = time.monotonic() + (0 if self._stop.is_set() else self._linger_s)
        while len(batch) < self._max_batch:
            remaining = deadline - time.monotonic()
            try:
                batch.append(self._q.get(timeout=remaining) if remaining > 0 else self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _send(self, batch: list[dict[str, Any]]) -> _Outcome:
        try:
            resp = self._client.post(self.url, json={"events": batch}, headers=self._headers)
        except httpx.HTTPError as exc:
            self.stats.failed_requests += 1
            self._warn("unreachable", "trace collector unreachable at %s (%s); buffering", self.url, exc)
            return _Outcome.RETRY
        if resp.is_success:
            self.stats.sent += len(batch)
            return _Outcome.SENT
        if resp.status_code in (408, 429) or resp.status_code >= 500:
            self.stats.failed_requests += 1
            self._warn("unavailable", "trace collector returned %d; retrying", resp.status_code)
            return _Outcome.RETRY
        self.stats.dropped_rejected += len(batch)
        log.warning(
            "trace collector rejected %d events (%d): %s",
            len(batch), resp.status_code, resp.text[:500],
        )
        return _Outcome.DROP

    def _drop_on_close(self, batch: list[dict[str, Any]]) -> None:
        dropped = len(batch)
        while True:
            try:
                self._q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        self.stats.dropped_on_close += dropped
        self._settle(dropped)
        log.warning("trace collector unavailable at shutdown; dropped %d events", dropped)

    def _settle(self, n: int) -> None:
        with self._cond:
            self._pending -= n
            self._cond.notify_all()

    def _warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last_warned.get(key, -1e9) >= self._warn_interval_s:
            self._last_warned[key] = now
            log.warning(msg, *args)
