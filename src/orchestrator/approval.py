"""Human-in-the-loop approval for write/destructive tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .models import PlanStep, ToolKind
from .registry import ToolRegistry


@dataclass(frozen=True)
class ApprovalDecision:
    """Who decided, and why. Truthy when approved, for code that expects a bool."""

    approved: bool
    approver: str = "unknown"
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.approved


# An approver receives (step, resolved_arguments) and returns a decision. A plain
# bool is still accepted (recorded with approver "unknown").
Approver = Callable[[PlanStep, dict[str, Any]], "ApprovalDecision | bool"]


def as_decision(result: ApprovalDecision | bool) -> ApprovalDecision:
    return result if isinstance(result, ApprovalDecision) else ApprovalDecision(bool(result))


class ApprovalPolicy(Protocol):
    def requires_approval(self, step: PlanStep) -> bool: ...


class WriteToolPolicy:
    """Approve any tool the registry classifies as a WRITE operation."""

    def __init__(self, registry: ToolRegistry, always_ask_servers: set[str] | None = None):
        self.registry = registry
        self.always_ask_servers = always_ask_servers or set()

    def requires_approval(self, step: PlanStep) -> bool:
        if step.server in self.always_ask_servers:
            return True
        spec = self.registry.get(step.qualified_tool)
        return bool(spec and spec.kind == ToolKind.WRITE)


def auto_approve(_step: PlanStep, _args: dict[str, Any]) -> ApprovalDecision:
    return ApprovalDecision(True, approver="policy:auto-approve")


def auto_reject(_step: PlanStep, _args: dict[str, Any]) -> ApprovalDecision:
    return ApprovalDecision(False, approver="policy:auto-reject")
