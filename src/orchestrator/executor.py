"""Dependency-aware execution of a plan across MCP servers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from .approval import ApprovalDecision, ApprovalPolicy, Approver, as_decision
from .llm import LLM
from .mcp_client import MCPClientPool
from .models import Plan, PlanStep, RunMetrics, StepStatus
from .recovery import RecoveryAction, RecoveryDecision, RecoveryPolicy, sleep_backoff
from .registry import ToolRegistry
from .templating import resolve_templates, unresolved_refs
from .tracing import SpanHandle, get_tracer

# Called after each step settles, for live UI rendering.
StepListener = Callable[[PlanStep], None]

ARG_RESOLVE_SYSTEM = """\
You are finalizing the arguments for one tool call inside a larger plan. Given the
overall goal, the tool schema, any partial arguments, and the observations produced
by completed prior steps, return the complete arguments as a single JSON object.
Use only information present in the observations or the goal.
"""


class ReplanRequested(Exception):
    def __init__(self, reason: str, failed_step: PlanStep):
        super().__init__(reason)
        self.reason = reason
        self.failed_step = failed_step


class Executor:
    def __init__(
        self,
        pool: MCPClientPool,
        registry: ToolRegistry,
        llm: LLM,
        approval_policy: ApprovalPolicy,
        approver: Approver,
        recovery: RecoveryPolicy,
        on_step: StepListener | None = None,
    ):
        self.pool = pool
        self.registry = registry
        self.llm = llm
        self.approval_policy = approval_policy
        self.approver = approver
        self.recovery = recovery
        self.on_step = on_step or (lambda _s: None)

    async def run(
        self, plan: Plan, metrics: RunMetrics, *, plan_attempt: int = 1
    ) -> dict[int, Any]:
        outputs: dict[int, Any] = {}
        step_span_ids: dict[int, str] = {}  # plan step id -> span id, for DAG edges
        for step in self._in_dependency_order(plan):
            with get_tracer().span(
                "step",
                f"step {step.id}: {step.qualified_tool}",
                mcp_server=step.server,
                tool=step.tool,
                depends_on=[step_span_ids[d] for d in step.depends_on if d in step_span_ids],
                input={"arguments": step.arguments, "rationale": step.rationale},
                attributes={"plan_step_id": step.id, "plan_attempt": plan_attempt},
            ) as span:
                if span.id:
                    step_span_ids[step.id] = span.id
                try:
                    if any(
                        plan.step(dep).status != StepStatus.SUCCEEDED for dep in step.depends_on
                    ):
                        step.status = StepStatus.SKIPPED
                        self.on_step(step)
                        continue
                    await self._run_step(step, plan, outputs, metrics)
                    outputs[step.id] = step.output
                finally:
                    _close_step_span(span, step)
        return outputs

    # ------------------------------------------------------------------ one step

    async def _run_step(
        self, step: PlanStep, plan: Plan, outputs: dict[int, Any], metrics: RunMetrics
    ) -> None:
        args = self._prepare_arguments(step, plan, outputs, metrics)

        if self.approval_policy.requires_approval(step):
            metrics.approvals_requested += 1
            step.status = StepStatus.AWAITING_APPROVAL
            self.on_step(step)
            decision = self._request_approval(step, args)
            if not decision.approved:
                step.status = StepStatus.REJECTED
                step.error = _rejection_message(decision)
                self.on_step(step)
                raise ReplanRequested(
                    f"step {step.id} ({step.qualified_tool}) was {step.error}", step
                )
            metrics.approvals_granted += 1

        while True:
            step.attempts += 1
            step.status = StepStatus.RUNNING
            self.on_step(step)
            started = time.perf_counter()
            try:
                with get_tracer().span(
                    "tool_call",
                    step.qualified_tool,
                    mcp_server=step.server,
                    tool=step.tool,
                    attempt=step.attempts,
                    input={"arguments": args},
                ) as call:
                    result = await self.pool.call_tool(step.server, step.tool, args)
                    call.set_output(result)
                step.latency_ms = (time.perf_counter() - started) * 1000
                step.output = result
                step.status = StepStatus.SUCCEEDED
                step.error = None
                metrics.tool_calls += 1
                self.on_step(step)
                return
            except Exception as exc:
                step.latency_ms = (time.perf_counter() - started) * 1000
                step.error = str(exc)
                step.status = StepStatus.FAILED
                metrics.tool_calls += 1
                metrics.tool_failures += 1
                self.on_step(step)

                decision = self._recover(step, args, metrics)
                if decision.action == RecoveryAction.RETRY:
                    metrics.retries += 1
                    continue
                if decision.action == RecoveryAction.REPAIR_ARGS:
                    metrics.retries += 1
                    args = decision.new_arguments or args
                    continue
                if decision.action == RecoveryAction.REPLAN:
                    raise ReplanRequested(
                        f"step {step.id} failed: {step.error} ({decision.note})", step
                    )
                raise ReplanRequested(f"step {step.id} aborted: {step.error}", step)

    def _request_approval(self, step: PlanStep, args: dict[str, Any]) -> ApprovalDecision:
        with get_tracer().span(
            "approval",
            f"approve {step.qualified_tool}",
            mcp_server=step.server,
            tool=step.tool,
            status="awaiting_approval",
            input={"arguments": args, "rationale": step.rationale},
        ) as span:
            started = time.perf_counter()
            decision = as_decision(self.approver(step, args))
            span.set_status("succeeded" if decision.approved else "rejected")
            span.set_output(
                {"approved": decision.approved, "approver": decision.approver,
                 "reason": decision.reason}
            )
            span.set_attributes(
                approver=decision.approver,
                decision="approved" if decision.approved else "rejected",
                wait_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return decision

    def _recover(
        self, step: PlanStep, args: dict[str, Any], metrics: RunMetrics
    ) -> RecoveryDecision:
        """Decide how to handle a failed attempt; a RETRY's backoff is part of the span."""
        with get_tracer().span(
            "retry",
            f"recover {step.qualified_tool}",
            mcp_server=step.server,
            tool=step.tool,
            attempt=step.attempts,
            input={"error": step.error, "arguments": args},
        ) as span:
            decision = self.recovery.decide(step, args, step.error or "", metrics)
            span.set_output(
                {"action": decision.action.value, "note": decision.note,
                 "backoff_s": decision.backoff_s, "new_arguments": decision.new_arguments}
            )
            span.set_attributes(action=decision.action.value)
            if decision.action == RecoveryAction.RETRY:
                sleep_backoff(decision.backoff_s)
        return decision

    # ------------------------------------------------------------------ arguments

    def _prepare_arguments(
        self, step: PlanStep, plan: Plan, outputs: dict[int, Any], metrics: RunMetrics
    ) -> dict[str, Any]:
        args = resolve_templates(step.arguments, outputs)
        needs_llm = step.resolve_arguments_at_runtime or bool(unresolved_refs(args))
        if not needs_llm:
            return args

        spec = self.registry.get(step.qualified_tool)
        dep_context = {
            dep: outputs.get(dep) for dep in step.depends_on
        } or {k: v for k, v in outputs.items()}
        user = (
            f"GOAL:\n{plan.goal}\n\n"
            f"TOOL: {step.qualified_tool}\n"
            f"TOOL SCHEMA:\n{json.dumps(spec.input_schema if spec else {}, indent=2)}\n\n"
            f"PARTIAL ARGUMENTS:\n{json.dumps(step.arguments, default=str)}\n\n"
            f"OBSERVATIONS FROM PRIOR STEPS:\n{_truncate(json.dumps(dep_context, default=str))}"
        )
        data, resp = self.llm.complete_json(
            system=ARG_RESOLVE_SYSTEM, user=user, max_tokens=800, purpose="resolve_args"
        )
        metrics.planner_tokens_in += resp.tokens_in
        metrics.planner_tokens_out += resp.tokens_out
        return data if isinstance(data, dict) else args

    # ------------------------------------------------------------------ ordering

    @staticmethod
    def _in_dependency_order(plan: Plan) -> list[PlanStep]:
        remaining = {s.id: set(s.depends_on) for s in plan.steps}
        ordered: list[PlanStep] = []
        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError("dependency cycle in plan")
            for sid in ready:
                ordered.append(plan.step(sid))
                remaining.pop(sid)
                for deps in remaining.values():
                    deps.discard(sid)
        return ordered


_TERMINAL_STATUSES = {
    StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.REJECTED
}


def _close_step_span(span: SpanHandle, step: PlanStep) -> None:
    # A step that dies mid-flight (e.g. arg resolution raised) keeps a non-terminal
    # status; leave the span to default to "failed" from the exception.
    if step.status in _TERMINAL_STATUSES:
        span.set_status(step.status.value)
    span.set_attributes(attempts=step.attempts, latency_ms=step.latency_ms)
    if step.output is not None:
        span.set_output(step.output)
    if step.error and step.status != StepStatus.SUCCEEDED:
        span.record_error(step.error)


def _truncate(text: str, limit: int = 6000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _rejection_message(decision: ApprovalDecision) -> str:
    """Describe a rejection so the replanner acts on the approver's feedback, not a guess."""
    reason = (decision.reason or "").strip()
    if reason:
        return f"rejected by the approver with this feedback: {reason}"
    return "rejected by the approver (no reason given)"
