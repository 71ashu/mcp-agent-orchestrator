"""Tracing seam.

The orchestrator depends only on the small `Tracer` protocol below;
`switchboard_trace.Tracer` satisfies it. The active tracer is ambient (a context
variable, like OpenTelemetry's global tracer) so deep call sites such as
`LLM.complete` can open spans without threading a tracer through every signature.
`NoopTracer` is the default: with tracing off, behavior and output are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Protocol


class SpanHandle(Protocol):
    id: str | None

    def set_output(self, output: Any) -> None: ...
    def set_tokens(self, tokens_in: int, tokens_out: int) -> None: ...
    def set_status(self, status: str) -> None: ...
    def set_attributes(self, **attributes: Any) -> None: ...
    def record_error(self, error: BaseException | str) -> None: ...


class Tracer(Protocol):
    def run(
        self, task: str, *, model: str | None = None, attributes: Mapping[str, Any] | None = None
    ) -> AbstractContextManager[SpanHandle]: ...

    def span(self, type: str, name: str, **fields: Any) -> AbstractContextManager[SpanHandle]: ...


class _NoopSpan:
    id = None

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


_NOOP_SPAN = _NoopSpan()


class NoopTracer:
    def run(self, task: str, **_kw: Any) -> AbstractContextManager[SpanHandle]:
        return nullcontext(_NOOP_SPAN)

    def span(self, type: str, name: str, **_kw: Any) -> AbstractContextManager[SpanHandle]:
        return nullcontext(_NOOP_SPAN)


_NOOP_TRACER = NoopTracer()
_active: ContextVar[Tracer] = ContextVar("orchestrator_tracer")


def get_tracer() -> Tracer:
    return _active.get(_NOOP_TRACER)


@contextmanager
def use_tracer(tracer: Tracer) -> Iterator[Tracer]:
    token = _active.set(tracer)
    try:
        yield tracer
    finally:
        _active.reset(token)
