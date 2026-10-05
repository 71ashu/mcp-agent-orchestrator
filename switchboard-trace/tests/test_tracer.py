import asyncio
import logging

import pytest

from switchboard_trace import REDACTED, MemoryTransport, Tracer, current_span


@pytest.fixture
def mem():
    return MemoryTransport()


@pytest.fixture
def tracer(mem):
    return Tracer(mem, known_secrets=[])


def kinds(events):
    return [(e["kind"], e["payload"].get("name") or e["payload"].get("task")) for e in events]


def test_run_and_nested_spans_emit_start_end_events_with_parents(tracer, mem):
    with tracer.run("check payments", model="claude-sonnet-5", attributes={"v": 1}) as run:
        with tracer.span("step", "step 1", mcp_server="deployment", tool="get_logs") as step:
            with tracer.span("tool_call", "deployment.get_logs", input={"id": "d-1"}, attempt=1) as tc:
                tc.set_output([{"msg": "boom"}])
            step.set_status("succeeded")
        with tracer.span("llm_call", "llm:synthesize", model="claude-sonnet-5") as llm:
            llm.set_tokens(120, 40)
        run.set_output({"answer": "ok"})

    assert kinds(mem.events) == [
        ("run.start", "check payments"),
        ("span.start", "step 1"),
        ("span.start", "deployment.get_logs"),
        ("span.end", None),
        ("span.end", None),
        ("span.start", "llm:synthesize"),
        ("span.end", None),
        ("run.end", None),
    ]
    ev = mem.events
    assert all(e["run_id"] == run.id and e["schema_version"] == 1 for e in ev)
    assert len({e["event_id"] for e in ev}) == len(ev)
    assert ev[0]["span_id"] is None and ev[0]["payload"]["model"] == "claude-sonnet-5"
    assert "parent_span_id" not in ev[1]["payload"]  # step is a root span
    assert ev[2]["payload"]["parent_span_id"] == step.id
    assert ev[2]["payload"]["input"] == {"id": "d-1"} and ev[2]["payload"]["attempt"] == 1
    assert ev[3]["span_id"] == tc.id and ev[3]["payload"]["output"] == [{"msg": "boom"}]
    assert ev[6]["payload"]["tokens_in"] == 120 and ev[6]["payload"]["tokens_out"] == 40
    assert ev[7]["payload"] == {"status": "succeeded", "output": {"answer": "ok"}}


def test_agent_exceptions_propagate_and_mark_spans_failed(tracer, mem):
    with pytest.raises(ValueError, match="bad args"), tracer.run("t"), tracer.span("tool_call", "x.y"):
        raise ValueError("bad args")

    span_end, run_end = mem.events[2], mem.events[3]
    assert span_end["payload"]["status"] == "failed"
    assert span_end["payload"]["error"] == {"type": "ValueError", "message": "bad args"}
    assert run_end["payload"]["status"] == "failed"


def test_explicit_status_wins_over_exception_default(tracer, mem):
    class Rejected(Exception):
        pass

    with pytest.raises(Rejected), tracer.run("t"), tracer.span("step", "s") as s:
        s.set_status("rejected")
        raise Rejected("rejected by approver")
    assert mem.events[2]["payload"]["status"] == "rejected"
    assert mem.events[2]["payload"]["error"]["type"] == "Rejected"


def test_initial_status_and_unknown_values_are_handled(tracer, mem, caplog):
    with tracer.run("t"):
        with tracer.span("approval", "approve", status="awaiting_approval") as a:
            a.set_status("approved-ish")  # invalid -> ignored, warned
        with tracer.span("not-a-type", "x") as noop:
            noop.set_output("ignored")
    assert mem.events[1]["payload"]["status"] == "awaiting_approval"
    assert mem.events[2]["payload"]["status"] == "succeeded"
    assert len(mem.events) == 4  # unknown span type is not recorded
    assert "unknown span type" in caplog.text and "unknown span status" in caplog.text


def test_spans_outside_a_run_are_noops(tracer, mem):
    with tracer.span("tool_call", "x") as s:
        s.set_output("anything")
        assert s.id is None
    assert mem.events == []


async def test_parenting_is_isolated_per_asyncio_task(tracer, mem):
    async def child(name):
        with tracer.span("tool_call", name) as s:
            await asyncio.sleep(0.01)
            assert current_span() is s
            return s

    with tracer.run("t"), tracer.span("step", "parent") as parent:
        a, b = await asyncio.gather(child("a"), child("b"))
        assert current_span() is parent

    starts = {e["payload"]["name"]: e for e in mem.events if e["kind"] == "span.start"}
    assert starts["a"]["payload"]["parent_span_id"] == parent.id
    assert starts["b"]["payload"]["parent_span_id"] == parent.id
    assert a.id != b.id


def test_payloads_are_redacted_and_truncated(mem):
    tracer = Tracer(mem, max_field_bytes=64, known_secrets=["sk-live-secret-value"])
    with tracer.run("t"), tracer.span("tool_call", "x", input={"api_key": "k", "q": "ok"},
                     attributes={"password": "p"}) as s:
        s.set_output("x" * 500)
        raise_error = RuntimeError("auth failed for sk-live-secret-value")
        s.record_error(raise_error)

    start, end = mem.events[1]["payload"], mem.events[2]["payload"]
    assert start["input"] == {"api_key": REDACTED, "q": "ok"}
    assert start["attributes"] == {"password": REDACTED}
    assert end["output"] == "x" * 64 + "…[truncated]"
    assert end["attributes"] == {"output_truncated": True}
    assert end["error"]["message"] == f"auth failed for {REDACTED}"


def test_tracer_failures_never_reach_the_agent(caplog):
    class BrokenTransport(MemoryTransport):
        def submit(self, event):
            raise ConnectionError("transport exploded")

    tracer = Tracer(BrokenTransport(), known_secrets=[])
    with (
        caplog.at_level(logging.ERROR, logger="switchboard_trace"),
        tracer.run("t"),
        tracer.span("tool_call", "x") as s,
    ):
        s.set_output({"ok": True})
        result = "agent kept going"
    assert result == "agent kept going"
    assert "failed to emit" in caplog.text


def test_from_env_requires_url():
    assert Tracer.from_env({}) is None
    t = Tracer.from_env({"SWITCHBOARD_TRACE_URL": "http://127.0.0.1:9", "SWITCHBOARD_TRACE_TOKEN": "tk"})
    try:
        assert t is not None and t.transport.url == "http://127.0.0.1:9/v1/events"
    finally:
        t.close(timeout=1)
