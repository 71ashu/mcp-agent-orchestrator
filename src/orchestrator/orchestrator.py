"""Top-level agent loop: plan -> execute -> (recover / replan) -> synthesize."""

from __future__ import annotations

import json
from collections.abc import Callable

from .approval import Approver, WriteToolPolicy, auto_approve
from .config import AppConfig
from .executor import Executor, ReplanRequested
from .llm import LLM
from .mcp_client import MCPClientPool
from .memory import TaskMemory
from .models import Plan, RunMetrics, RunResult
from .planner import Planner
from .recovery import RecoveryPolicy
from .registry import ToolRegistry

PlanListener = Callable[[Plan], None]

SYNTHESIS_SYSTEM = """\
You are the final synthesizer for a multi-server agent. Given the user's goal, the
executed plan, and every step's observation, write a direct, well-structured answer
to the goal. Cite which tool/server produced each key fact. If the evidence is
incomplete, say so explicitly rather than guessing.
"""


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        pool: MCPClientPool,
        *,
        approver: Approver = auto_approve,
        memory: TaskMemory | None = None,
        on_plan: PlanListener | None = None,
        on_step=None,
        llm: LLM | None = None,
    ):
        self.config = config
        self.pool = pool
        self.llm = llm or LLM(config.model)
        self.registry = ToolRegistry(pool.all_tools())
        self.memory = memory or TaskMemory()
        self.approver = approver
        self.on_plan = on_plan or (lambda _p: None)
        self.on_step = on_step or (lambda _s: None)

        self.planner = Planner(self.llm, self.registry, config.max_plan_steps)
        self.recovery = RecoveryPolicy(
            self.llm, self.registry, max_attempts=config.max_step_attempts
        )
        self.approval_policy = WriteToolPolicy(self.registry)

    async def run(self, goal: str) -> RunResult:
        metrics = RunMetrics()
        hint = self.memory.hint_for(goal)
        plan = self.planner.create_plan(goal, metrics, hint=hint)
        self.on_plan(plan)

        executor = Executor(
            self.pool,
            self.registry,
            self.llm,
            self.approval_policy,
            self.approver,
            self.recovery,
            on_step=self.on_step,
        )

        outputs: dict = {}
        for attempt in range(self.config.max_replans + 1):
            try:
                outputs = await executor.run(plan, metrics)
                break
            except ReplanRequested as rp:
                if attempt >= self.config.max_replans:
                    return self._finalize(
                        goal, plan, metrics, outputs, success=False,
                        answer=f"Could not complete the task: {rp.reason}",
                    )
                metrics.replans += 1
                plan = self.planner.create_plan(
                    goal, metrics,
                    hint=(hint or "") + f"\n\nPrevious attempt failed: {rp.reason}. "
                    f"Observations so far: {json.dumps(_summarize(outputs), default=str)[:2000]}",
                )
                self.on_plan(plan)

        answer = self._synthesize(goal, plan, outputs, metrics)
        return self._finalize(goal, plan, metrics, outputs, success=True, answer=answer)

    # ------------------------------------------------------------------ helpers

    def _synthesize(self, goal: str, plan: Plan, outputs: dict, metrics: RunMetrics) -> str:
        transcript = []
        for step in plan.steps:
            transcript.append(
                {
                    "step": step.id,
                    "tool": step.qualified_tool,
                    "status": step.status.value,
                    "observation": outputs.get(step.id),
                    "error": step.error,
                }
            )
        resp = self.llm.complete(
            system=SYNTHESIS_SYSTEM,
            user=f"GOAL:\n{goal}\n\nEXECUTION TRANSCRIPT:\n{_truncate(json.dumps(transcript, default=str, indent=2))}",
            max_tokens=1500,
        )
        metrics.synthesis_tokens_in += resp.tokens_in
        metrics.synthesis_tokens_out += resp.tokens_out
        return resp.text.strip()

    def _finalize(self, goal, plan, metrics, outputs, *, success, answer) -> RunResult:
        metrics.finished_at = __import__("time").time()
        result = RunResult(
            goal=goal, plan=plan, answer=answer, success=success, metrics=metrics
        )
        try:
            self.memory.record(result)
        except Exception:
            pass
        return result


def _summarize(outputs: dict) -> dict:
    return {k: (v if isinstance(v, (int, float, str, bool)) else "<data>") for k, v in outputs.items()}


def _truncate(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"
