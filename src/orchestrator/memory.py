"""Lightweight task memory: successful tool sequences and past outcomes.

Stored as a JSON file so the orchestrator can offer the planner a hint from similar
past tasks and so evaluation can aggregate across runs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import Plan, RunResult


class TaskMemory:
    def __init__(self, path: str | Path = "data/memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text())
            except ValueError:
                self._entries = []

    def recall(self, goal: str, limit: int = 3) -> list[dict[str, Any]]:
        """Naive keyword overlap ranking against past successful runs."""
        goal_words = _tokens(goal)
        scored: list[tuple[float, dict]] = []
        for e in self._entries:
            if not e.get("success"):
                continue
            overlap = len(goal_words & _tokens(e["goal"]))
            if overlap:
                scored.append((overlap / (len(goal_words) or 1), e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]

    def hint_for(self, goal: str) -> str | None:
        matches = self.recall(goal)
        if not matches:
            return None
        lines = ["Similar past tasks that succeeded:"]
        for m in matches:
            seq = " -> ".join(m["tool_sequence"])
            lines.append(f"- goal: {m['goal']}\n  tools: {seq}")
        return "\n".join(lines)

    def record(self, result: RunResult) -> None:
        self._entries.append(
            {
                "ts": time.time(),
                "goal": result.goal,
                "success": result.success,
                "tool_sequence": _tool_sequence(result.plan),
                "steps": len(result.plan.steps),
                "tool_calls": result.metrics.tool_calls,
                "retries": result.metrics.retries,
                "replans": result.metrics.replans,
                "wall_ms": round(result.metrics.wall_ms, 1),
                "cost_usd": round(result.metrics.estimated_cost_usd(), 5),
            }
        )
        self.path.write_text(json.dumps(self._entries, indent=2))

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in text.split() if len(w) > 3}


def _tool_sequence(plan: Plan) -> list[str]:
    return [s.qualified_tool for s in plan.steps]
