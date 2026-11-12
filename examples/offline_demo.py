"""Offline replay of a cross-system run. No API key needed.

What is real:    MCP server processes, tool discovery, dependency ordering, {{steps.N}}
                 templating, tool calls and their results, the write-tool approval gate.
What is scripted: the model. The plan and the final answer below are canned replies fed
                 through the real `LLM` class, so this shows the harness, NOT model quality.

Run from the repo root:  python examples/offline_demo.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from rich.panel import Panel

from orchestrator.approval import ApprovalDecision
from orchestrator.cli import _render_plan, _render_step, console
from orchestrator.config import load_config
from orchestrator.llm import LLM
from orchestrator.mcp_client import MCPClientPool
from orchestrator.memory import TaskMemory
from orchestrator.orchestrator import Orchestrator

GOAL = (
    "Find the latest production deployment for payments, inspect its error logs and the "
    "service health, check who owns the service, and if it is clearly bad, roll it back."
)

PLAN = {
    "reasoning": (
        "Get the live payments deployment, then read its error logs; check service health "
        "and ownership notes independently; roll back only after the evidence is in."
    ),
    "steps": [
        {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
         "arguments": {"service": "payments"}, "depends_on": [],
         "rationale": "identify the live deployment"},
        {"id": 2, "server": "deployment", "tool": "get_logs",
         "arguments": {"deployment_id": "{{steps.1.output.id}}", "level": "error"},
         "depends_on": [1], "rationale": "error logs for that deployment"},
        {"id": 3, "server": "deployment", "tool": "get_service_health",
         "arguments": {"service": "payments"}, "depends_on": [],
         "rationale": "current error rate and latency"},
        {"id": 4, "server": "filesystem", "tool": "read_text_file",
         "arguments": {"path": "README.md"}, "depends_on": [],
         "rationale": "who owns the payments service"},
        {"id": 5, "server": "deployment", "tool": "rollback_deployment",
         "arguments": {"deployment_id": "{{steps.1.output.id}}",
                       "reason": "5xx rate 6.1% and p95 910ms after rollout"},
         "depends_on": [2, 3], "rationale": "WRITE: restore the previous deployment"},
    ],
}

ANSWER = (
    "Scripted answer. The live payments deployment d-payments-482 (deployment.get_latest_"
    "deployment) is unhealthy: error rate 0.4% -> 6.1% and p95 240ms -> 910ms, with a "
    "NullPointerException in RefundService.settle and ledger timeouts (deployment.get_logs, "
    "deployment.get_service_health). Owner: #payments-oncall (filesystem.read_text_file). "
    "It was rolled back to d-payments-481 after approval (deployment.rollback_deployment)."
)


class ScriptedClient:
    """Minimal stand-in for `anthropic.Anthropic`: replies in order, fixed fake usage."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.messages = self

    def create(self, **_kwargs):
        text = self.replies.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            stop_reason="end_turn",
        )


def scripted_approver(step, args):
    console.print(Panel(json.dumps(args, indent=2), border_style="magenta",
                        title=f"[magenta]Approval required[/] — {step.qualified_tool}",
                        subtitle="scripted approver: approving"))
    return ApprovalDecision(True, approver="demo:scripted-approver")


async def main() -> None:
    llm = LLM("claude-sonnet-5-5", api_key="demo-not-used")
    llm._client = ScriptedClient([json.dumps(PLAN), ANSWER])

    cfg = load_config("config/servers.yaml")
    async with MCPClientPool(cfg) as pool:
        with tempfile.TemporaryDirectory() as tmp:
            orch = Orchestrator(
                cfg, pool, llm=llm, approver=scripted_approver,
                memory=TaskMemory(Path(tmp) / "memory.json"),
                on_plan=_render_plan, on_step=_render_step,
            )
            console.print(f"[bold]Goal:[/] {GOAL}\n")
            result = await orch.run(GOAL)

    console.print("\n[bold]Tool results (real, truncated):[/]")
    for s in result.plan.steps:
        text = json.dumps(s.output, default=str)
        console.print(
            f"  step {s.id} {s.qualified_tool}: {text[:44]}{'…' if len(text) > 44 else ''}",
            no_wrap=True, overflow="ellipsis", highlight=False,
        )
    console.print(Panel(result.answer, title="Answer (scripted)", border_style="green"))
    m = result.metrics
    console.print(
        f"[dim]steps={len(result.plan.steps)} tool_calls={m.tool_calls} failures={m.tool_failures} "
        f"retries={m.retries} replans={m.replans} approvals={m.approvals_granted}/"
        f"{m.approvals_requested}[/]"
    )


if __name__ == "__main__":
    asyncio.run(main())
