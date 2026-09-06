"""Human-in-the-loop approval for write/destructive tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .models import PlanStep, ToolKind
from .registry import ToolRegistry

# An approver receives (step, resolved_arguments) and returns True to allow.
Approver = Callable[[PlanStep, dict[str, Any]], bool]


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


def auto_approve(_step: PlanStep, _args: dict[str, Any]) -> bool:
    return True


def auto_reject(_step: PlanStep, _args: dict[str, Any]) -> bool:
    return False
