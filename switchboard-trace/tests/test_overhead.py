"""Non-functional requirement: < 5 ms emitter overhead per event on the agent side.

Measures the caller-thread cost of opening + closing a span (two events) with a
realistic payload, through the real HttpTransport pointed at a dead collector, which
is the worst case for the agent, since the buffer keeps filling and retrying.
"""

import statistics
import time

from switchboard_trace import HttpTransport, Tracer

TYPICAL_INPUT = {
    "deployment_id": "d-payments-482",
    "level": "error",
    "headers": {"Authorization": "Bearer abcdefgh12345678"},
    "context": [{"line": i, "msg": "connection reset by peer " * 4} for i in range(20)],
}
TYPICAL_OUTPUT = [{"ts": "2026-10-05T12:00:00Z", "level": "error", "msg": "5xx spike " * 10}] * 30


def _per_event_ms(tracer: Tracer, iterations: int, payload_out) -> list[float]:
    samples = []
    with tracer.run("overhead"):
        for i in range(iterations):
            t0 = time.perf_counter()
            with tracer.span("tool_call", "deployment.get_logs", input=TYPICAL_INPUT, attempt=1) as s:
                s.set_output(payload_out)
            samples.append((time.perf_counter() - t0) * 1000 / 2)  # 2 events per span
    return samples


def _report(label: str, samples: list[float]) -> float:
    p50 = statistics.median(samples)
    p99 = statistics.quantiles(samples, n=100)[98]
    print(f"\n{label}: p50={p50 * 1000:.0f}µs p99={p99 * 1000:.0f}µs per event (n={len(samples)})")
    return p99


def test_per_event_overhead_typical_payload(dead_url):
    tracer = Tracer(HttpTransport(dead_url, max_queue=100_000), known_secrets=["x" * 16])
    try:
        p99 = _report("typical payload (~4KB)", _per_event_ms(tracer, 2000, TYPICAL_OUTPUT))
        assert p99 < 5.0
    finally:
        tracer.close(timeout=2)


def test_per_event_overhead_oversized_payload_is_bounded(dead_url):
    huge_output = [{"row": i, "blob": "y" * 200} for i in range(5000)]  # ~1.2 MB
    tracer = Tracer(HttpTransport(dead_url, max_queue=100_000), known_secrets=[])
    try:
        p99 = _report("oversized payload (~1.2MB, truncated)", _per_event_ms(tracer, 200, huge_output))
        assert p99 < 5.0  # sanitizer work is bounded by the 32 KB budget
    finally:
        tracer.close(timeout=2)
