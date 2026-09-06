import pytest

from orchestrator.approval import WriteToolPolicy, auto_approve, auto_reject
from orchestrator.executor import Executor, ReplanRequested
from orchestrator.mcp_client import ToolCallError
from orchestrator.models import Plan, PlanStep, StepStatus
from orchestrator.recovery import RecoveryPolicy

from conftest import FakeLLM, FakePool


def _executor(pool, registry, llm, approver=auto_approve):
    return Executor(
        pool, registry, llm,
        WriteToolPolicy(registry), approver,
        RecoveryPolicy(llm, registry, max_attempts=3, base_backoff_s=0),
    )


@pytest.mark.asyncio
async def test_dependency_order_and_template_flow(registry, deployment_tools):
    handlers = {
        ("deployment", "get_latest_deployment"): lambda a: {"id": "d-482", "service": a["service"]},
        ("deployment", "get_logs"): lambda a: [{"level": "error", "msg": "boom"}] if a["deployment_id"] == "d-482" else [],
    }
    pool = FakePool(handlers, deployment_tools)
    plan = Plan(goal="check payments", steps=[
        PlanStep(id=1, server="deployment", tool="get_latest_deployment",
                 arguments={"service": "payments"}),
        PlanStep(id=2, server="deployment", tool="get_logs", depends_on=[1],
                 arguments={"deployment_id": "{{steps.1.output.id}}"}),
    ])
    from orchestrator.models import RunMetrics
    outputs = await _executor(pool, registry, FakeLLM()).run(plan, RunMetrics())

    assert [c[1] for c in pool.calls] == ["get_latest_deployment", "get_logs"]
    assert pool.calls[1][2] == {"deployment_id": "d-482"}
    assert outputs[2][0]["msg"] == "boom"
    assert plan.step(2).status == StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_write_tool_rejected_triggers_replan(registry, deployment_tools):
    pool = FakePool({("deployment", "rollback_deployment"): lambda a: {"ok": True}}, deployment_tools)
    plan = Plan(goal="roll back", steps=[
        PlanStep(id=1, server="deployment", tool="rollback_deployment",
                 arguments={"deployment_id": "d-482"}),
    ])
    from orchestrator.models import RunMetrics
    with pytest.raises(ReplanRequested):
        await _executor(pool, registry, FakeLLM(), approver=auto_reject).run(plan, RunMetrics())
    assert plan.step(1).status == StepStatus.REJECTED
    assert pool.calls == []


@pytest.mark.asyncio
async def test_transient_error_is_retried(registry, deployment_tools):
    calls = {"n": 0}

    def flaky(_a):
        calls["n"] += 1
        return ToolCallError("rate limit exceeded (429)") if calls["n"] == 1 else {"id": "d-1"}

    pool = FakePool({("deployment", "get_latest_deployment"): flaky}, deployment_tools)
    plan = Plan(goal="g", steps=[
        PlanStep(id=1, server="deployment", tool="get_latest_deployment",
                 arguments={"service": "payments"}),
    ])
    from orchestrator.models import RunMetrics
    m = RunMetrics()
    outputs = await _executor(pool, registry, FakeLLM()).run(plan, m)
    assert outputs[1] == {"id": "d-1"}
    assert m.retries == 1
    assert m.tool_failures == 1
