"""Rich CLI for the MCP agent orchestrator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from .approval import auto_approve, auto_reject
from .config import load_config
from .evaluation import EvalRow, summarize
from .mcp_client import MCPClientPool
from .models import Plan, PlanStep, StepStatus
from .orchestrator import Orchestrator

app = typer.Typer(add_completion=False, help="Multi-server MCP agent orchestrator")
console = Console()

_STATUS_STYLE = {
    StepStatus.SUCCEEDED: "green",
    StepStatus.FAILED: "red",
    StepStatus.RUNNING: "yellow",
    StepStatus.SKIPPED: "dim",
    StepStatus.REJECTED: "red",
    StepStatus.AWAITING_APPROVAL: "magenta",
    StepStatus.PENDING: "dim",
}


def _render_plan(plan: Plan) -> None:
    table = Table(title="Execution plan", show_lines=False, expand=True)
    table.add_column("#", justify="right", width=3)
    table.add_column("server.tool", style="cyan")
    table.add_column("depends", width=9)
    table.add_column("rationale")
    for s in plan.steps:
        deps = ",".join(map(str, s.depends_on)) or "-"
        rt = " [italic dim](runtime args)[/]" if s.resolve_arguments_at_runtime else ""
        table.add_row(str(s.id), s.qualified_tool, deps, s.rationale + rt)
    console.print(Panel(plan.reasoning or "", title="Planner reasoning", border_style="dim"))
    console.print(table)


def _render_step(step: PlanStep) -> None:
    style = _STATUS_STYLE.get(step.status, "white")
    lat = f" {step.latency_ms:.0f}ms" if step.latency_ms else ""
    line = f"[{style}]● step {step.id}  {step.qualified_tool}  {step.status.value}{lat}[/]"
    if step.error and step.status in (StepStatus.FAILED, StepStatus.REJECTED):
        line += f"\n   [red]{step.error[:300]}[/]"
    console.print(line)


def _approver_prompt(step: PlanStep, args: dict[str, Any]) -> bool:
    console.print(
        Panel(
            JSON(json.dumps(args, default=str, indent=2)),
            title=f"[magenta]Approval required[/] — {step.qualified_tool}",
            subtitle=step.rationale,
            border_style="magenta",
        )
    )
    return typer.confirm("  Approve this write operation?", default=False)


async def _with_pool(config_path: str):
    config = load_config(config_path)
    pool = MCPClientPool(config)
    await pool.connect_all()
    return config, pool


@app.command()
def servers(config: str = typer.Option("config/servers.yaml", help="server registry")):
    """Connect to every enabled MCP server and list discovered tools."""

    async def _run():
        _cfg, pool = await _with_pool(config)
        try:
            for name, conn in pool.connections.items():
                t = Table(title=f"{name}  ({len(conn.tools)} tools)", expand=True)
                t.add_column("tool", style="cyan")
                t.add_column("kind", width=6)
                t.add_column("description")
                for tool in conn.tools:
                    t.add_row(tool.name, tool.kind.value, (tool.description or "")[:100])
                console.print(t)
        finally:
            await pool.aclose()

    asyncio.run(_run())


@app.command()
def run(
    goal: str = typer.Argument(..., help="the task to accomplish"),
    config: str = typer.Option("config/servers.yaml"),
    approve: str = typer.Option(
        "prompt", help="write-tool policy: prompt | auto | reject"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit machine-readable result"),
):
    """Plan and execute a task across the connected MCP servers."""
    approver = {"prompt": _approver_prompt, "auto": auto_approve, "reject": auto_reject}[
        approve
    ]

    async def _run():
        cfg, pool = await _with_pool(config)
        try:
            orch = Orchestrator(
                cfg,
                pool,
                approver=approver,
                on_plan=None if as_json else _render_plan,
                on_step=None if as_json else _render_step,
            )
            result = await orch.run(goal)
        finally:
            await pool.aclose()

        if as_json:
            console.print_json(result.model_dump_json())
            return

        console.print(Panel(result.answer, title="Answer", border_style="green" if result.success else "red"))
        m = result.metrics
        console.print(
            f"[dim]steps={len(result.plan.steps)} tool_calls={m.tool_calls} "
            f"failures={m.tool_failures} retries={m.retries} replans={m.replans} "
            f"approvals={m.approvals_granted}/{m.approvals_requested} "
            f"wall={m.wall_ms:.0f}ms tokens={m.total_tokens_in}+{m.total_tokens_out} "
            f"~${m.estimated_cost_usd():.4f}[/]"
        )

    asyncio.run(_run())


@app.command("eval")
def eval_cmd(
    tasks_file: Path = typer.Argument(..., help="text file, one goal per line"),
    config: str = typer.Option("config/servers.yaml"),
    approve: str = typer.Option("auto", help="prompt | auto | reject"),
):
    """Run a batch of goals and print an evaluation table."""
    approver = {"prompt": _approver_prompt, "auto": auto_approve, "reject": auto_reject}[approve]
    goals = [
        ln.strip()
        for ln in tasks_file.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]

    async def _run():
        cfg, pool = await _with_pool(config)
        results = []
        try:
            orch = Orchestrator(cfg, pool, approver=approver)
            for g in goals:
                console.rule(g)
                results.append(await orch.run(g))
        finally:
            await pool.aclose()

        table = Table(title="Evaluation", expand=True)
        for col in ("goal", "ok", "steps", "calls", "fail", "retry", "replan", "wall(ms)", "tok(in/out)", "$"):
            table.add_column(col)
        for r in results:
            row = EvalRow.from_result(r)
            table.add_row(
                row.goal[:40],
                "[green]✓[/]" if row.success else "[red]✗[/]",
                str(row.steps), str(row.tool_calls), str(row.tool_failures),
                str(row.retries), str(row.replans), f"{row.wall_ms:.0f}",
                f"{row.tokens_in}/{row.tokens_out}", f"{row.cost_usd:.4f}",
            )
        console.print(table)
        console.print_json(json.dumps(summarize(results)))

    asyncio.run(_run())


@app.command()
def memory(path: str = typer.Option("data/memory.json")):
    """Show recorded task memory."""
    p = Path(path)
    if not p.exists():
        console.print("[dim]no memory yet[/]")
        raise typer.Exit()
    console.print_json(p.read_text())


if __name__ == "__main__":
    app()
