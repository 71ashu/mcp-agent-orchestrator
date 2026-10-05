"""A real HTTP server standing in for the collector, so transport tests exercise
actual sockets, timeouts and connection-refused behavior rather than mocks."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeCollector:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.auth_headers: list[str | None] = []
        self.statuses: deque[int] = deque()  # scripted responses; 202 once exhausted
        self.delay_s = 0.0
        self._lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(collector.delay_s)
                with collector._lock:
                    status = collector.statuses.popleft() if collector.statuses else 202
                    if 200 <= status < 300:
                        collector.batches.append(json.loads(body)["events"])
                        collector.auth_headers.append(self.headers.get("Authorization"))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return [e for batch in self.batches for e in batch]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    c = FakeCollector()
    yield c
    c.stop()


@pytest.fixture
def dead_url() -> str:
    """A localhost URL with nothing listening: connections are refused."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
