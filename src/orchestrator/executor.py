"""Dependency-aware execution of a plan across MCP servers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from .approval import ApprovalPolicy, Approver
from .llm import LLM
from .mcp_client import MCPClientPool
from .models import Plan, PlanStep, RunMetrics, StepStatus
from .recovery import RecoveryAction, RecoveryPolicy, sleep_backoff
from .registry import ToolRegistry
from .templating import resolve_templates, unresolved_refs

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

    async def run(self, plan: Plan, metrics: RunMetrics) -> dict[int, Any]:
        outputs: dict[int, Any] = {}
        for step in self._in_dependency_order(plan):
            if any(
                plan.step(dep).status != StepStatus.SUCCEEDED for dep in step.depends_on
            ):
                step.status = StepStatus.SKIPPED
                self.on_step(step)
                continue
            await self._run_step(step, plan, outputs, metrics)
            outputs[step.id] = step.output
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
            if not self.approver(step, args):
                step.status = StepStatus.REJECTED
                step.error = "rejected by human approver"
                self.on_step(step)
                raise ReplanRequested(
                    f"step {step.id} ({step.qualified_tool}) was rejected", step
                )
            metrics.approvals_granted += 1

        while True:
            step.attempts += 1
            step.status = StepStatus.RUNNING
            self.on_step(step)
            started = time.perf_counter()
            try:
                result = await self.pool.call_tool(step.server, step.tool, args)
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

                decision = self.recovery.decide(step, args, step.error, metrics)
                if decision.action == RecoveryAction.RETRY:
                    metrics.retries += 1
                    sleep_backoff(decision.backoff_s)
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
            system=ARG_RESOLVE_SYSTEM, user=user, max_tokens=800
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


def _truncate(text: str, limit: int = 6000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"
