"""The tracing API used by instrumented code.

    tracer = Tracer.from_env()          # None unless SWITCHBOARD_TRACE_URL is set
    with tracer.run("deploy check", model="claude-sonnet-5-5") as run:
        with tracer.span("tool_call", "deployment.get_logs", mcp_server="deployment",
                         tool="get_logs", input=args) as span:
            span.set_output(result)

Parenting is implicit: the innermost open span in the current context (thread or
asyncio task) becomes the parent. Spans opened outside any run are no-ops.

Invariant: tracing never changes the agent's behavior. The agent's own exceptions
pass through unchanged, and failures inside the tracer are logged and swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .events import SPAN_TYPES, STATUSES, drop_none, make_event, new_id
from .sanitize import DEFAULT_MAX_FIELD_BYTES, Sanitizer, secrets_from_env
from .transport import HttpTransport, Transport

log = logging.getLogger("switchboard_trace")

_MAX_ERROR_CHARS = 4000
_UNSET: Any = object()


class Span:
    """Handle for an open run or span. Setters only record; data ships on close."""

    def __init__(self, span_id: str | None, run_id: str | None):
        self.id = span_id
        self.run_id = run_id
        self._status: str | None = None
        self._output: Any = _UNSET
        self._tokens: tuple[int, int] | None = None
        self._attributes: dict[str, Any] = {}
        self._error: dict[str, str] | None = None

    def set_output(self, output: Any) -> None:
        self._output = output

    def set_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self._tokens = (int(tokens_in), int(tokens_out))

    def set_status(self, status: str) -> None:
        if status not in STATUSES:
            log.warning("ignoring unknown span status %r", status)
            return
        self._status = status

    def set_attributes(self, **attributes: Any) -> None:
        self._attributes.update(attributes)

    def record_error(self, error: BaseException | str) -> None:
        if isinstance(error, BaseException):
            self._error = {"type": type(error).__name__, "message": str(error)}
        else:
            self._error = {"type": "Error", "message": error}

    def _final_status(self) -> str:
        return self._status or ("failed" if self._error else "succeeded")


class _NoopSpan(Span):
    """Handed out when there is nothing to trace into. Shared, so it keeps no state."""

    def set_output(self, output: Any) -> None:
        pass

    def set_tokens(self, tokens_in: int, tokens_out: int) -> None:
        pass

    def set_status(self, status: str) -> None:
        pass

    def set_attributes(self, **attributes: Any) -> None:
        pass

    def record_error(self, error: BaseException | str) -> None:
        pass


_NOOP_SPAN = _NoopSpan(None, None)

_current_run: ContextVar[Span | None] = ContextVar("switchboard_trace_run", default=None)
_current_span: ContextVar[Span | None] = ContextVar("switchboard_trace_span", default=None)


def current_span() -> Span | None:
    return _current_span.get()


class Tracer:
    def __init__(
        self,
        transport: Transport,
        *,
        max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES,
        known_secrets: Sequence[str] | None = None,
    ):
        self.transport = transport
        self.sanitizer = Sanitizer(
            max_field_bytes=max_field_bytes,
            known_secrets=secrets_from_env() if known_secrets is None else known_secrets,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Tracer | None:
        """Build an HTTP tracer from SWITCHBOARD_TRACE_URL / SWITCHBOARD_TRACE_TOKEN."""
        import os

        env = os.environ if environ is None else environ
        url = env.get("SWITCHBOARD_TRACE_URL")
        if not url:
            return None
        return cls(HttpTransport(url, token=env.get("SWITCHBOARD_TRACE_TOKEN") or None))

    # ------------------------------------------------------------------ API

    @contextmanager
    def run(
        self,
        task: str,
        *,
        model: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        run_id = new_id()
        handle = Span(run_id, run_id)
        self._emit_start(
            "run.start", handle, {"task": task, "model": model}, None, attributes
        )
        run_token = _current_run.set(handle)
        span_token = _current_span.set(None)
        try:
            yield handle
        except BaseException as exc:
            handle.record_error(exc)
            raise
        finally:
            _reset(_current_span, span_token)
            _reset(_current_run, run_token)
            self._emit_end("run.end", handle)

    @contextmanager
    def span(
        self,
        type: str,
        name: str,
        *,
        input: Any = None,
        mcp_server: str | None = None,
        tool: str | None = None,
        model: str | None = None,
        depends_on: Sequence[str] | None = None,
        attempt: int | None = None,
        status: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        run = _current_run.get()
        if run is None or type not in SPAN_TYPES:
            if run is not None:
                log.warning("unknown span type %r; span not recorded", type)
            yield _NOOP_SPAN
            return

        parent = _current_span.get()
        handle = Span(new_id(), run.run_id)
        self._emit_start(
            "span.start",
            handle,
            {
                "parent_span_id": parent.id if parent else None,
                "type": type,
                "name": name,
                "mcp_server": mcp_server,
                "tool": tool,
                "model": model,
                "depends_on": list(depends_on) if depends_on else None,
                "attempt": attempt,
                "status": status if status in STATUSES else None,
            },
            input,
            attributes,
        )
        token = _current_span.set(handle)
        try:
            yield handle
        except BaseException as exc:
            handle.record_error(exc)
            raise
        finally:
            _reset(_current_span, token)
            self._emit_end("span.end", handle)

    def flush(self, timeout: float = 5.0) -> bool:
        return self.transport.flush(timeout)

    def close(self, timeout: float = 5.0) -> None:
        self.transport.close(timeout)

    # ------------------------------------------------------------------ emission

    def _emit_start(
        self,
        kind: str,
        handle: Span,
        fields: dict[str, Any],
        input_value: Any,
        attributes: Mapping[str, Any] | None,
    ) -> None:
        try:
            payload = {
                k: self.sanitizer.redact_text(v) if isinstance(v, str) else v
                for k, v in drop_none(fields).items()
            }  # task / name are free text too
            attrs = dict(attributes or {})
            if input_value is not None:
                payload["input"], attrs["input_truncated"] = self.sanitizer.clean(input_value)
                if not attrs["input_truncated"]:
                    del attrs["input_truncated"]
            if attrs:
                payload["attributes"] = self.sanitizer.clean(attrs)[0]
            span_id = None if kind == "run.start" else handle.id
            self.transport.submit(make_event(kind, handle.run_id, span_id, payload))  # type: ignore[arg-type]
        except Exception:
            log.exception("failed to emit %s", kind)

    def _emit_end(self, kind: str, handle: Span) -> None:
        try:
            payload: dict[str, Any] = {"status": handle._final_status()}
            attrs = dict(handle._attributes)
            if handle._output is not _UNSET:
                payload["output"], truncated = self.sanitizer.clean(handle._output)
                if truncated:
                    attrs["output_truncated"] = True
            if handle._error:
                payload["error"] = {
                    "type": handle._error["type"],
                    "message": self.sanitizer.redact_text(handle._error["message"])[
                        :_MAX_ERROR_CHARS
                    ],
                }
            if handle._tokens:
                payload["tokens_in"], payload["tokens_out"] = handle._tokens
            if attrs:
                payload["attributes"] = self.sanitizer.clean(attrs)[0]
            span_id = None if kind == "run.end" else handle.id
            self.transport.submit(make_event(kind, handle.run_id, span_id, payload))  # type: ignore[arg-type]
        except Exception:
            log.exception("failed to emit %s", kind)


def _reset(var: ContextVar[Span | None], token: Any) -> None:
    try:
        var.reset(token)
    except ValueError:  # exited in a different context than entered; nothing to restore
        pass
