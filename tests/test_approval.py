import pytest
from rich.console import Console

from orchestrator.approval import ApprovalDecision, WriteToolPolicy
from orchestrator.cli import _approval_panel
from orchestrator.executor import Executor, ReplanRequested
from orchestrator.models import Plan, PlanStep, RunMetrics
from orchestrator.recovery import RecoveryPolicy

from conftest import FakeLLM, FakePool

LONG_REASON = (
    "Rolling back payments deployment d-payments-482 because the error rate rose from 0.4% "
    "to 6.1% and the logs show NullPointerException in RefundService.settle() END-OF-REASON"
)


def _rollback_plan():
    return Plan(
        goal="roll back payments",
        steps=[
            PlanStep(
                id=1, server="deployment", tool="rollback_deployment",
                arguments={"deployment_id": "d-payments-482", "reason": "x"},
                rationale="roll back the bad deploy",
            )
        ],
    )


def _run(registry, deployment_tools, approver):
    pool = FakePool(
        {("deployment", "rollback_deployment"): lambda _a: {"ok": True}}, deployment_tools
    )
    llm = FakeLLM()
    executor = Executor(
        pool, registry, llm, WriteToolPolicy(registry), approver,
        RecoveryPolicy(llm, registry, max_attempts=3, base_backoff_s=0),
    )
    return executor.run(_rollback_plan(), RunMetrics())


async def test_rejection_feedback_reaches_the_replan(registry, deployment_tools):
    def reject_with_reason(_step, _args):
        return ApprovalDecision(False, approver="user:test", reason="wrong deployment id")

    with pytest.raises(ReplanRequested) as exc:
        await _run(registry, deployment_tools, reject_with_reason)
    assert "wrong deployment id" in exc.value.reason
    assert "wrong deployment id" in exc.value.failed_step.error


async def test_rejection_without_reason_says_so(registry, deployment_tools):
    with pytest.raises(ReplanRequested) as exc:
        await _run(registry, deployment_tools, lambda _s, _a: ApprovalDecision(False))
    assert "no reason given" in exc.value.reason


def test_approval_panel_shows_long_arguments_in_full():
    step = _rollback_plan().steps[0]
    console = Console(width=60, record=True)
    console.print(_approval_panel(step, {"deployment_id": "d-payments-482", "reason": LONG_REASON}))
    flat = "".join(console.export_text().split())
    assert "END-OF-REASON" in flat
    assert "d-payments-482" in flat
