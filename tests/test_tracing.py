"""Instrumentation tests: the span tree Switchboard emits for real runs.

The real `LLM` class runs against a scripted Anthropic client, and most tests use the
real `deployment` MCP server, so every hook (planner, LLM calls, steps, approvals,
tool calls, retries, synthesis, eval) is exercised the way production would.
"""

from __future__ import annotations

import json

import pytest
from switchboard_trace import HttpTransport, MemoryTransport
from switchboard_trace import Tracer as TraceEmitter

from orchestrator.approval import WriteToolPolicy, auto_approve, auto_reject
from orchestrator.config import load_config
from orchestrator.executor import Executor
from orchestrator.mcp_client import MCPClientPool, ToolCallError
from orchestrator.memory import TaskMemory
from orchestrator.models import Plan, PlanStep, RunMetrics
from orchestrator.orchestrator import Orchestrator
from orchestrator.recovery import RecoveryPolicy
from orchestrator.tracing import use_tracer

from conftest import FakePool, scripted_llm

ROLLBACK_PLAN = {
    "reasoning": "find the live payments deploy, confirm errors, roll it back",
    "steps": [
        {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
         "arguments": {"service": "payments"}},
        {"id": 2, "server": "deployment", "tool": "get_logs", "depends_on": [1],
         "arguments": {"deployment_id": "{{steps.1.output.id}}", "level": "error"}},
        # `reason` is required by the server; omitting it forces a real validation
        # failure -> LLM arg repair -> second attempt.
        {"id": 3, "server": "deployment", "tool": "rollback_deployment", "depends_on": [1, 2],
         "arguments": {"deployment_id": "{{steps.1.output.id}}"}},
    ],
}
REPAIRED_ARGS = {"deployment_id": "d-payments-482", "reason": "5xx rate 6.1% after deploy"}


def materialize(events: list[dict]) -> tuple[dict, dict]:
    """Fold events into {span_id: span} plus the run, like the collector will."""
    run: dict = {"attributes": {}}
    spans: dict[str, dict] = {}
    for e in events:
        if e["kind"].startswith("run."):
            payload = dict(e["payload"])
            run["attributes"].update(payload.pop("attributes", {}))
            run.update(payload)
        else:
            span = spans.setdefault(e["span_id"], {"id": e["span_id"], "events": [], "attributes": {}})
            payload = dict(e["payload"])
            span["attributes"].update(payload.pop("attributes", {}))  # merged, like jsonb ||
            span.update(payload)
            span["events"].append(e["kind"])
    return run, spans


def of_type(spans: dict, type_: str) -> list[dict]:
    return [s for s in spans.values() if s["type"] == type_]


def deployment_config():
    cfg = load_config("config/servers.yaml")
    cfg.servers = [s for s in cfg.servers if s.name == "deployment"]
    return cfg


async def run_traced(goal, llm, *, approver=auto_approve, eval_batch_id=None, tracer=None, tmp_path):
    mem = MemoryTransport()
    tracer = tracer or TraceEmitter(mem, known_secrets=[])
    async with MCPClientPool(deployment_config()) as pool:
        orch = Orchestrator(
            deployment_config(), pool, llm=llm, approver=approver, tracer=tracer,
            memory=TaskMemory(tmp_path / "mem.json"),
        )
        result = await orch.run(goal, eval_batch_id=eval_batch_id)
    return result, mem.events


async def test_full_run_emits_a_well_formed_span_tree(tmp_path):
    llm = scripted_llm([json.dumps(ROLLBACK_PLAN), json.dumps(REPAIRED_ARGS), "Rolled back d-payments-482."])
    result, events = await run_traced("Roll back payments if it is erroring", llm, tmp_path=tmp_path)
    assert result.success

    run, spans = materialize(events)
    assert run["task"] == "Roll back payments if it is erroring"
    assert run["model"] == "claude-sonnet-5"
    assert run["status"] == "succeeded"
    assert run["output"] == {"answer": "Rolled back d-payments-482."}
    assert run["attributes"]["servers"] == ["deployment"]
    assert run["attributes"]["retries"] == 1

    # every span is opened and closed exactly once, within this run, with a valid parent
    run_ids = {e["run_id"] for e in events}
    assert len(run_ids) == 1
    for s in spans.values():
        assert s["events"] == ["span.start", "span.end"], s["name"]
        assert s.get("parent_span_id") in (None, *spans)

    [plan] = of_type(spans, "plan")
    assert plan["name"] == "plan #1" and plan["attempt"] == 1
    assert [st["tool"] for st in plan["output"]["steps"]] == [
        "get_latest_deployment", "get_logs", "rollback_deployment"
    ]

    llm_calls = {s["name"]: s for s in of_type(spans, "llm_call")}
    assert set(llm_calls) == {"llm:plan", "llm:repair_args", "llm:synthesize"}
    assert llm_calls["llm:plan"]["parent_span_id"] == plan["id"]
    assert "parent_span_id" not in llm_calls["llm:synthesize"]  # run-level
    for call in llm_calls.values():
        assert (call["tokens_in"], call["tokens_out"]) == (100, 20)
        assert call["model"] == "claude-sonnet-5"
        assert call["output"]["stop_reason"] == "end_turn"

    steps = {s["attributes"]["plan_step_id"]: s for s in of_type(spans, "step")}
    assert all(s["status"] == "succeeded" for s in steps.values())
    assert "depends_on" not in steps[1]
    assert steps[2]["depends_on"] == [steps[1]["id"]]
    assert steps[3]["depends_on"] == [steps[1]["id"], steps[2]["id"]]
    assert steps[3]["attributes"]["attempts"] == 2

    [approval] = of_type(spans, "approval")
    assert approval["parent_span_id"] == steps[3]["id"]
    assert approval["status"] == "succeeded"
    assert approval["attributes"]["approver"] == "policy:auto-approve"
    assert approval["attributes"]["decision"] == "approved"
    assert approval["attributes"]["wait_ms"] >= 0

    calls = [s for s in of_type(spans, "tool_call") if s["parent_span_id"] == steps[3]["id"]]
    assert [(c["attempt"], c["status"]) for c in calls] == [(1, "failed"), (2, "succeeded")]
    assert calls[0]["error"]["type"] == "ToolCallError"
    assert calls[0]["input"] == {"arguments": {"deployment_id": "d-payments-482"}}
    assert calls[1]["input"] == {"arguments": REPAIRED_ARGS}
    assert calls[1]["output"]["restored"] == "d-payments-481"
    assert calls[1]["mcp_server"] == "deployment" and calls[1]["tool"] == "rollback_deployment"

    [retry] = of_type(spans, "retry")
    assert retry["parent_span_id"] == steps[3]["id"]
    assert retry["attributes"]["action"] == "repair_args"
    assert retry["output"]["new_arguments"] == REPAIRED_ARGS
    assert llm_calls["llm:repair_args"]["parent_span_id"] == retry["id"]

    # spans are emitted in causal order: start of a child never precedes its parent's
    order = [e["span_id"] for e in events if e["kind"] == "span.start"]
    for s in spans.values():
        if s.get("parent_span_id"):
            assert order.index(s["parent_span_id"]) < order.index(s["id"])


async def test_rejection_and_replan_are_traced(tmp_path):
    rollback_only = {"reasoning": "roll back", "steps": [
        {"id": 1, "server": "deployment", "tool": "rollback_deployment",
         "arguments": {"deployment_id": "d-payments-482", "reason": "errors"}},
    ]}
    read_only = {"reasoning": "just report", "steps": [
        {"id": 1, "server": "deployment", "tool": "get_service_health",
         "arguments": {"service": "payments"}},
    ]}
    llm = scripted_llm([json.dumps(rollback_only), json.dumps(read_only), "Payments is degraded."])
    result, events = await run_traced("Fix payments", llm, approver=auto_reject, tmp_path=tmp_path)
    assert result.success and result.metrics.replans == 1

    _run, spans = materialize(events)
    plans = sorted(of_type(spans, "plan"), key=lambda s: s["attempt"])
    assert [p["name"] for p in plans] == ["plan #1", "replan #2"]
    assert "was rejected" in plans[1]["attributes"]["replan_reason"]

    [approval] = of_type(spans, "approval")
    assert approval["status"] == "rejected"
    assert approval["attributes"] == {
        "approver": "policy:auto-reject", "decision": "rejected",
        "wait_ms": approval["attributes"]["wait_ms"],
    }
    steps = sorted(of_type(spans, "step"), key=lambda s: s["attributes"]["plan_attempt"])
    assert [(s["tool"], s["status"], s["attributes"]["plan_attempt"]) for s in steps] == [
        ("rollback_deployment", "rejected", 1),
        ("get_service_health", "succeeded", 2),
    ]
    assert steps[0]["error"]["type"] == "ReplanRequested"
    assert not [s for s in of_type(spans, "tool_call") if s["tool"] == "rollback_deployment"]


async def test_exhausted_recovery_fails_the_run_with_every_replan_traced(tmp_path):
    plan = {"reasoning": "r", "steps": [
        {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
         "arguments": {"service": "no-such-service"}},
        {"id": 2, "server": "deployment", "tool": "get_logs", "depends_on": [1],
         "arguments": {"deployment_id": "{{steps.1.output.id}}"}},
    ]}
    # repair returns the same args -> "args not repairable" -> replan, until max_replans
    replies = [json.dumps(plan)]
    for _ in range(2):
        replies += [json.dumps({"service": "no-such-service"}), json.dumps(plan)]
    replies.append(json.dumps({"service": "no-such-service"}))
    result, events = await run_traced("g", scripted_llm(replies), tmp_path=tmp_path)
    assert not result.success

    run, spans = materialize(events)
    assert run["status"] == "failed"
    assert "Could not complete the task" in run["error"]["message"]
    assert len(of_type(spans, "plan")) == 3
    first_steps = [s for s in of_type(spans, "step") if s["attributes"]["plan_step_id"] == 1]
    assert {s["status"] for s in first_steps} == {"failed"}
    assert {s["attributes"]["action"] for s in of_type(spans, "retry")} == {"replan"}


async def test_transient_retry_includes_backoff_inside_retry_span(registry, deployment_tools):
    n = {"calls": 0}

    def flaky(_args):
        n["calls"] += 1
        return ToolCallError("503 temporarily unavailable") if n["calls"] == 1 else {"id": "d-1"}

    pool = FakePool({("deployment", "get_latest_deployment"): flaky}, deployment_tools)
    llm = scripted_llm([])
    executor = Executor(
        pool, registry, llm, WriteToolPolicy(registry), auto_approve,
        RecoveryPolicy(llm, registry, base_backoff_s=0.05),
    )
    plan = Plan(goal="g", steps=[PlanStep(id=1, server="deployment", tool="get_latest_deployment",
                                          arguments={"service": "payments"})])
    mem = MemoryTransport()
    tracer = TraceEmitter(mem, known_secrets=[])
    with use_tracer(tracer), tracer.run("g"):
        await executor.run(plan, RunMetrics())

    _run, spans = materialize(mem.events)
    [retry] = of_type(spans, "retry")
    assert retry["output"]["action"] == "retry" and retry["output"]["backoff_s"] == 0.05
    starts = {e["span_id"]: e["ts"] for e in mem.events if e["kind"] == "span.start"}
    ends = {e["span_id"]: e["ts"] for e in mem.events if e["kind"] == "span.end"}
    assert ends[retry["id"]] > starts[retry["id"]]
    calls = sorted(of_type(spans, "tool_call"), key=lambda s: s["attempt"])
    assert [c["status"] for c in calls] == ["failed", "succeeded"]
    assert calls[0]["error"]["message"] == "503 temporarily unavailable"


async def test_eval_runs_are_tagged_and_get_an_eval_span(tmp_path):
    plan = {"reasoning": "r", "steps": [
        {"id": 1, "server": "deployment", "tool": "list_services", "arguments": {}},
    ]}
    llm = scripted_llm([json.dumps(plan), "payments, checkout"])
    _result, events = await run_traced("list services", llm, eval_batch_id="eval-1", tmp_path=tmp_path)

    run, spans = materialize(events)
    assert run["attributes"]["eval_batch_id"] == "eval-1"
    [ev] = of_type(spans, "eval")
    assert ev["attributes"] == {"eval_batch_id": "eval-1", "passed": True}
    assert ev["output"]["success"] is True and ev["output"]["tool_calls"] == 1
    assert "cost_usd" not in ev["output"]


async def test_secrets_never_reach_trace_events(tmp_path, monkeypatch):
    secret = "sk-ant-api03-SUPERSECRETVALUE0123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    plan = {"reasoning": "r", "steps": [
        {"id": 1, "server": "deployment", "tool": "list_services",
         "arguments": {"api_key": "hunter2-hunter2"}},
    ]}
    llm = scripted_llm([json.dumps(plan), f"done; I used {secret}"])
    mem = MemoryTransport()
    tracer = TraceEmitter(mem)  # default: picks credential-like env vars up
    await run_traced(f"use key {secret} to list services", llm, tracer=tracer, tmp_path=tmp_path)

    blob = json.dumps(mem.events)
    assert secret not in blob and "SUPERSECRET" not in blob
    assert "hunter2-hunter2" not in blob
    assert "[REDACTED]" in blob


async def test_agent_result_is_identical_when_collector_is_down(tmp_path, unused_tcp_port):
    plan = {"reasoning": "r", "steps": [
        {"id": 1, "server": "deployment", "tool": "get_service_health",
         "arguments": {"service": "payments"}},
    ]}
    dead = TraceEmitter(HttpTransport(f"http://127.0.0.1:{unused_tcp_port}", initial_backoff_s=0.05))
    try:
        traced, _ = await run_traced("health?", scripted_llm([json.dumps(plan), "ok"]),
                                     tracer=dead, tmp_path=tmp_path)
    finally:
        dead.close(timeout=2)
    async with MCPClientPool(deployment_config()) as pool:
        untraced = await Orchestrator(
            deployment_config(), pool, llm=scripted_llm([json.dumps(plan), "ok"]),
            memory=TaskMemory(tmp_path / "mem2.json"),
        ).run("health?")
    assert traced.success and untraced.success
    assert traced.answer == untraced.answer
    assert traced.plan.steps[0].output == untraced.plan.steps[0].output
    assert dead.transport.stats.sent == 0


def test_cli_builds_tracer_only_when_configured(monkeypatch):
    from orchestrator.cli import _make_tracer

    monkeypatch.delenv("SWITCHBOARD_TRACE_URL", raising=False)
    assert _make_tracer() is None
    monkeypatch.setenv("SWITCHBOARD_TRACE_URL", "http://127.0.0.1:9")
    tracer = _make_tracer()
    try:
        assert isinstance(tracer, TraceEmitter)
    finally:
        tracer.close(timeout=1)


@pytest.fixture
def unused_tcp_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
