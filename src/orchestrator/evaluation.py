"""Aggregate run metrics into an evaluation report."""

from __future__ import annotations

from dataclasses import dataclass

from .models import RunResult


@dataclass
class EvalRow:
    goal: str
    success: bool
    steps: int
    tool_calls: int
    tool_failures: int
    retries: int
    replans: int
    approvals: str
    wall_ms: float
    tokens_in: int
    tokens_out: int
    cost_usd: float

    @classmethod
    def from_result(cls, r: RunResult) -> EvalRow:
        m = r.metrics
        return cls(
            goal=r.goal,
            success=r.success,
            steps=len(r.plan.steps),
            tool_calls=m.tool_calls,
            tool_failures=m.tool_failures,
            retries=m.retries,
            replans=m.replans,
            approvals=f"{m.approvals_granted}/{m.approvals_requested}",
            wall_ms=round(m.wall_ms, 1),
            tokens_in=m.total_tokens_in,
            tokens_out=m.total_tokens_out,
            cost_usd=round(m.estimated_cost_usd(), 5),
        )


def summarize(results: list[RunResult]) -> dict:
    if not results:
        return {}
    n = len(results)
    return {
        "runs": n,
        "success_rate": round(sum(r.success for r in results) / n, 3),
        "avg_steps": round(sum(len(r.plan.steps) for r in results) / n, 2),
        "avg_tool_calls": round(sum(r.metrics.tool_calls for r in results) / n, 2),
        "total_tool_failures": sum(r.metrics.tool_failures for r in results),
        "total_retries": sum(r.metrics.retries for r in results),
        "total_replans": sum(r.metrics.replans for r in results),
        "avg_wall_ms": round(sum(r.metrics.wall_ms for r in results) / n, 1),
        "total_cost_usd": round(sum(r.metrics.estimated_cost_usd() for r in results), 4),
    }
