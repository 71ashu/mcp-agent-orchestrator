"""HttpTransport against a real local HTTP server, including collector-down paths."""

import logging
import time

from conftest import wait_until

from switchboard_trace import HttpTransport, Tracer


def ev(i: int) -> dict:
    return {"event_id": str(i), "kind": "span.start", "payload": {}}


def test_events_are_batched_and_sent_with_bearer_token(collector):
    t = HttpTransport(collector.url, token="ingest-token", max_batch=50)
    try:
        for i in range(120):
            t.submit(ev(i))
        assert t.flush(timeout=5)
        assert [e["event_id"] for e in collector.events] == [str(i) for i in range(120)]
        assert all(len(b) <= 50 for b in collector.batches)
        assert len(collector.batches) < 120  # actually batched
        assert set(collector.auth_headers) == {"Bearer ingest-token"}
        assert t.stats.sent == 120
    finally:
        t.close()


def test_collector_down_does_not_block_or_raise(dead_url):
    t = HttpTransport(dead_url, initial_backoff_s=0.05)
    try:
        started = time.perf_counter()
        for i in range(1000):
            t.submit(ev(i))
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert elapsed_ms < 100, f"1000 submits took {elapsed_ms:.1f}ms with collector down"
        assert wait_until(lambda: t.stats.failed_requests >= 2)
        assert t.stats.sent == 0
    finally:
        t.close(timeout=2)


def test_transient_failures_are_retried_until_delivered(collector):
    collector.statuses.extend([503, 500, 429])
    t = HttpTransport(collector.url, initial_backoff_s=0.01)
    try:
        for i in range(5):
            t.submit(ev(i))
        assert t.flush(timeout=5)
        assert [e["event_id"] for e in collector.events] == ["0", "1", "2", "3", "4"]
        assert t.stats.failed_requests == 3
    finally:
        t.close()


def test_buffered_events_survive_an_outage_and_arrive_in_order(collector):
    collector.statuses.extend([503] * 5)  # collector "down" for the first few attempts
    t = HttpTransport(collector.url, initial_backoff_s=0.02, max_backoff_s=0.05)
    try:
        for i in range(10):
            t.submit(ev(i))
            time.sleep(0.005)
        assert t.flush(timeout=5)
        assert [e["event_id"] for e in collector.events] == [str(i) for i in range(10)]
    finally:
        t.close()


def test_non_retryable_rejection_drops_batch_instead_of_looping(collector, caplog):
    collector.statuses.append(400)
    t = HttpTransport(collector.url, linger_s=0.2)
    try:
        with caplog.at_level(logging.WARNING, logger="switchboard_trace"):
            t.submit(ev(0))
            assert t.flush(timeout=5)
        assert t.stats.dropped_rejected == 1
        assert "rejected 1 events (400)" in caplog.text
        t.submit(ev(1))  # pipeline still healthy afterwards
        assert t.flush(timeout=5)
        assert [e["event_id"] for e in collector.events] == ["1"]
    finally:
        t.close()


def test_full_buffer_drops_with_rate_limited_warning(dead_url, caplog):
    t = HttpTransport(dead_url, max_queue=10, max_batch=1, initial_backoff_s=10)
    try:
        with caplog.at_level(logging.WARNING, logger="switchboard_trace"):
            for i in range(500):
                t.submit(ev(i))
        # 10 queued + at most 1 in flight; the rest dropped
        assert t.stats.dropped_queue_full >= 489
        assert caplog.text.count("trace buffer full") == 1
    finally:
        t.close(timeout=2)


def test_close_with_collector_down_returns_promptly_and_reports_drops(dead_url, caplog):
    t = HttpTransport(dead_url, initial_backoff_s=0.05)
    for i in range(50):
        t.submit(ev(i))
    with caplog.at_level(logging.WARNING, logger="switchboard_trace"):
        started = time.perf_counter()
        t.close(timeout=3)
        assert time.perf_counter() - started < 3
    assert t.stats.dropped_on_close == 50
    assert "dropped 50 events" in caplog.text
    t.submit(ev(99))  # submitting after close is a silent no-op
    assert t.stats.dropped_on_close == 51


def test_slow_collector_does_not_slow_the_agent(collector):
    collector.delay_s = 0.5
    t = HttpTransport(collector.url, request_timeout_s=2)
    try:
        started = time.perf_counter()
        for i in range(200):
            t.submit(ev(i))
        assert (time.perf_counter() - started) * 1000 < 50
        assert t.flush(timeout=5)
        assert len(collector.events) == 200
    finally:
        t.close()


def test_agent_run_completes_normally_when_collector_is_down(dead_url):
    tracer = Tracer(HttpTransport(dead_url, initial_backoff_s=0.05), known_secrets=[])
    with tracer.run("deploy check") as run:
        with tracer.span("tool_call", "deployment.get_logs") as s:
            s.set_output({"lines": 3})
        run.set_output("done")
    assert tracer.transport.stats.sent == 0
    tracer.close(timeout=2)
