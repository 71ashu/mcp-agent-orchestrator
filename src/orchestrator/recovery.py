"""Failure recovery: classify a failed tool call and decide what to do next.

Strategy (bounded, Reflexion-style):
  - transient / rate-limit  -> retry with backoff
  - bad arguments / schema  -> ask the LLM to repair arguments, then retry
  - wrong tool / missing data -> signal a re-plan
  - otherwise               -> abort the step
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .llm import LLM
from .models import PlanStep, RunMetrics
from .registry import ToolRegistry

_TRANSIENT_MARKERS = (
    "rate limit",
    "rate-limit",
    "429",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "503",
    "connection reset",
)


class RecoveryAction(str, Enum):
    RETRY = "retry"
    REPAIR_ARGS = "repair_args"
    REPLAN = "replan"
    ABORT = "abort"


@dataclass
class RecoveryDecision:
    action: RecoveryAction
    new_arguments: dict[str, Any] | None = None
    backoff_s: float = 0.0
    note: str = ""


REPAIR_SYSTEM = """\
A tool call failed. Given the tool's JSON schema, the arguments that were sent, and
the error message, return corrected arguments as a single JSON object. If the error
cannot be fixed by changing arguments, return {"unfixable": true}.
"""


class RecoveryPolicy:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        max_attempts: int = 3,
        base_backoff_s: float = 1.5,
    ):
        self.llm = llm
        self.registry = registry
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s

    def decide(
        self,
        step: PlanStep,
        resolved_args: dict[str, Any],
        error: str,
        metrics: RunMetrics,
    ) -> RecoveryDecision:
        if step.attempts >= self.max_attempts:
            return RecoveryDecision(RecoveryAction.REPLAN, note="attempt budget exhausted")

        low = error.lower()
        if any(m in low for m in _TRANSIENT_MARKERS):
            backoff = self.base_backoff_s * (2 ** (step.attempts - 1))
            return RecoveryDecision(
                RecoveryAction.RETRY, backoff_s=backoff, note="transient error"
            )

        spec = self.registry.get(step.qualified_tool)
        if spec is None:
            return RecoveryDecision(RecoveryAction.REPLAN, note="tool no longer available")

        repaired = self._repair_args(spec.input_schema, resolved_args, error, metrics)
        if repaired is not None and repaired != resolved_args:
            return RecoveryDecision(
                RecoveryAction.REPAIR_ARGS, new_arguments=repaired, note="llm-repaired args"
            )
        return RecoveryDecision(RecoveryAction.REPLAN, note="args not repairable")

    def _repair_args(
        self,
        schema: dict[str, Any],
        args: dict[str, Any],
        error: str,
        metrics: RunMetrics,
    ) -> dict[str, Any] | None:
        import json

        user = (
            f"TOOL SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
            f"ARGUMENTS SENT:\n{json.dumps(args, indent=2, default=str)}\n\n"
            f"ERROR:\n{error}"
        )
        try:
            data, resp = self.llm.complete_json(
                system=REPAIR_SYSTEM, user=user, max_tokens=800
            )
        except Exception:
            return None
        metrics.recovery_tokens_in += resp.tokens_in
        metrics.recovery_tokens_out += resp.tokens_out
        if isinstance(data, dict) and data.get("unfixable"):
            return None
        return data if isinstance(data, dict) else None


def sleep_backoff(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
