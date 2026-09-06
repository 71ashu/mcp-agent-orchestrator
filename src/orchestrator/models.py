"""Core data models shared across the orchestrator."""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"


class ToolKind(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolSpec(BaseModel):
    """A single tool exposed by an MCP server, namespaced by server."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    kind: ToolKind = ToolKind.READ

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"


class PlanStep(BaseModel):
    id: int
    server: str
    tool: str
    # Arguments may contain template references to earlier outputs, e.g.
    # "{{steps.1.output}}" or "{{steps.1.output.commits[0].sha}}".
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    rationale: str = ""
    # When true, the executor asks the LLM to finalize `arguments` at run time using
    # the goal + observations from completed dependencies.
    resolve_arguments_at_runtime: bool = False

    status: StepStatus = StepStatus.PENDING
    output: Any = None
    error: str | None = None
    attempts: int = 0
    latency_ms: float | None = None

    @property
    def qualified_tool(self) -> str:
        return f"{self.server}.{self.tool}"


class Plan(BaseModel):
    goal: str
    reasoning: str = ""
    steps: list[PlanStep] = Field(default_factory=list)

    def step(self, step_id: int) -> PlanStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(f"no step {step_id}")


class RunMetrics(BaseModel):
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    planner_tokens_in: int = 0
    planner_tokens_out: int = 0
    synthesis_tokens_in: int = 0
    synthesis_tokens_out: int = 0
    recovery_tokens_in: int = 0
    recovery_tokens_out: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    retries: int = 0
    replans: int = 0
    approvals_requested: int = 0
    approvals_granted: int = 0

    @property
    def wall_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    @property
    def total_tokens_in(self) -> int:
        return self.planner_tokens_in + self.synthesis_tokens_in + self.recovery_tokens_in

    @property
    def total_tokens_out(self) -> int:
        return self.planner_tokens_out + self.synthesis_tokens_out + self.recovery_tokens_out

    def estimated_cost_usd(self, in_per_mtok: float = 3.0, out_per_mtok: float = 15.0) -> float:
        return (
            self.total_tokens_in / 1_000_000 * in_per_mtok
            + self.total_tokens_out / 1_000_000 * out_per_mtok
        )


class RunResult(BaseModel):
    goal: str
    plan: Plan
    answer: str
    success: bool
    metrics: RunMetrics
