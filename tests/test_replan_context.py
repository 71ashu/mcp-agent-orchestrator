"""What the replanner is told after earlier attempts were rejected or failed.

Regression for a live demo run: the approver rejected a rollback with "check whether
ledger-service is the real cause", the replan tried a wrong service name and failed,
and the *second* replan had forgotten the approver's feedback and saw no observations.
"""

from __future__ import annotations

import json

from test_tracing import run_traced

from orchestrator.approval import ApprovalDecision, WriteToolPolicy, auto_approve
from orchestrator.executor import Executor, ReplanRequested
from orchestrator.models import Plan, PlanStep, RunMetrics
from orchestrator.orchestrator import _replan_context
from orchestrator.recovery import RecoveryPolicy

from conftest import FakeLLM, FakePool, scripted_llm

FEEDBACK = "Don't roll back yet, first check whether ledger-service is the real cause"


def _plan(*steps):
    return json.dumps({"reasoning": "r", "steps": list(steps)})


def _planner_prompts(llm) -> list[str]:
    prompts = [json.dumps(r["messages"]) for r in llm._client.requests]
    return [p for p in prompts if "TOOL CATALOG" in p]


async def test_approver_feedback_and_observations_survive_a_second_replan(tmp_path):
    rollback = _plan(
        {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
         "arguments": {"service": "payments"}},
        {"id": 2, "server": "deployment", "tool": "rollback_deployment", "depends_on": [1],
         "arguments": {"deployment_id": "d-payments-482", "reason": "5xx after deploy"}},
    )
    wrong_service = _plan(
        {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
         "arguments": {"service": "ledger-service"}},
    )
    check_ledger = _plan(
        {"id": 1, "server": "deployment", "tool": "get_service_health",
         "arguments": {"service": "ledger"}},
    )
    llm = scripted_llm([
        rollback,
        wrong_service,
        json.dumps({"service": "ledger-service"}),  # repair can't fix it -> replan
        check_ledger,
        "Ledger is healthy; payments is the cause.",
    ])

    def reject_with_feedback(_step, _args):
        return ApprovalDecision(False, approver="user:test", reason=FEEDBACK)

    result, _events = await run_traced(
        "Roll back payments", llm, approver=reject_with_feedback, tmp_path=tmp_path
    )
    assert result.success and result.metrics.replans == 2

    first, second, third = _planner_prompts(llm)
    assert "APPROVER FEEDBACK" not in first
    for replan in (second, third):
        assert "APPROVER FEEDBACK" in replan
        assert "ledger-service is the real cause" in replan
        # real data from attempt 1, not a "<data>" placeholder
        assert "d-payments-482" in replan.split("OBSERVATIONS SO FAR")[1]
    # the third prompt also knows attempt 2 used a service name that doesn't exist
    assert "attempt 2" in third and "no live deployment" in third
    assert '\\"service\\": \\"ledger-service\\"' in third

    # The final answer sees the same history, so it can explain the held-back rollback
    # and cite data found before the last replan.
    synthesis = json.dumps(llm._client.requests[-1]["messages"])
    assert "ledger-service is the real cause" in synthesis
    assert "d-payments-482" in synthesis.split("EARLIER ATTEMPTS")[1]
    # ...and it knows what each final step was called with, so it can say whose data it is
    final_transcript = json.loads(
        llm._client.requests[-1]["messages"][0]["content"].split(
            "FINAL PLAN EXECUTION TRANSCRIPT:\n"
        )[1]
    )
    assert final_transcript[0]["arguments"] == {"service": "ledger"}


async def test_runtime_arguments_follow_the_steps_purpose(registry, deployment_tools):
    """A dependency check inside a payments goal must not resolve to service=payments."""
    step = PlanStep(
        id=1, server="deployment", tool="get_latest_deployment", arguments={},
        resolve_arguments_at_runtime=True,
        rationale="Check whether the ledger dependency changed recently",
    )
    pool = FakePool({("deployment", "get_latest_deployment"): lambda a: a}, deployment_tools)
    llm = FakeLLM(json_queue=[{"service": "ledger"}])
    executor = Executor(
        pool, registry, llm, WriteToolPolicy(registry), auto_approve,
        RecoveryPolicy(llm, registry, max_attempts=1, base_backoff_s=0),
    )
    await executor.run(Plan(goal="Roll back the payments deployment", steps=[step]), RunMetrics())

    [resolve_prompt] = llm.prompts
    assert "THIS STEP'S PURPOSE:\nCheck whether the ledger dependency" in resolve_prompt
    assert pool.calls == [("deployment", "get_latest_deployment", {"service": "ledger"})]


async def test_runtime_arguments_see_observations_from_earlier_attempts(
    registry, deployment_tools
):
    """A rollback reason resolved after a replan can cite logs fetched before it."""
    step = PlanStep(
        id=1, server="deployment", tool="rollback_deployment", arguments={},
        resolve_arguments_at_runtime=True, rationale="Roll back, citing the log evidence",
    )
    pool = FakePool({("deployment", "rollback_deployment"): lambda a: {"ok": True}},
                    deployment_tools)
    llm = FakeLLM(json_queue=[{"deployment_id": "d-payments-482", "reason": "NPE"}])
    executor = Executor(
        pool, registry, llm, WriteToolPolicy(registry), auto_approve,
        RecoveryPolicy(llm, registry, max_attempts=1, base_backoff_s=0),
    )
    earlier = [{"tool": "deployment.get_logs", "arguments": {"deployment_id": "d-payments-482"},
                "output": [{"msg": "NullPointerException in RefundService.settle()"}]}]
    await executor.run(
        Plan(goal="Roll back payments", steps=[step]), RunMetrics(), prior_observations=earlier
    )

    [resolve_prompt] = llm.prompts
    assert "OBSERVATIONS FROM EARLIER ATTEMPTS" in resolve_prompt
    assert "NullPointerException in RefundService.settle()" in resolve_prompt


def test_replan_context_keeps_every_feedback_and_clips_large_outputs():
    step = PlanStep(id=1, server="deployment", tool="rollback_deployment", arguments={})
    rejected = ReplanRequested("step 1 was rejected", step, arguments={}, feedback="wrong id")
    rejected.observations = [
        {"tool": "deployment.get_logs", "arguments": {}, "output": "x" * 50_000},
        {"tool": "deployment.get_service_health", "arguments": {"service": "payments"},
         "output": {"status": "degraded"}},
    ]
    failed = ReplanRequested("step 1 failed: timeout", step, arguments={"id": "d-1"})

    text = _replan_context(
        "memory hint", [rejected, failed], rejected.observations + failed.observations
    )
    assert text.startswith("memory hint")
    assert "- wrong id" in text
    assert "attempt 1: step 1 was rejected" in text and "attempt 2: step 1 failed" in text
    # one huge log is clipped, so later observations still fit
    assert "degraded" in text
    assert len(text) < 5000
