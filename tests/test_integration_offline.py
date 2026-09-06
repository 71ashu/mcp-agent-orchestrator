"""End-to-end wiring test against the REAL custom MCP server, with a scripted LLM.

Exercises: multi-server connect + discovery -> plan -> dependency-aware execution
with {{steps.N.output}} flow -> synthesis. No API key or Node required (only the
pure-Python `deployment` server is used).
"""

from __future__ import annotations

import pytest

from orchestrator.config import load_config
from orchestrator.mcp_client import MCPClientPool
from orchestrator.memory import TaskMemory
from orchestrator.orchestrator import Orchestrator

from conftest import FakeLLM


@pytest.mark.asyncio
async def test_cross_step_plan_runs_against_real_server(tmp_path):
    cfg = load_config("config/servers.yaml")
    cfg.servers = [s for s in cfg.servers if s.name == "deployment"]

    plan_json = {
        "reasoning": "get the live payments deployment, then read its error logs",
        "steps": [
            {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
             "arguments": {"service": "payments"}, "depends_on": []},
            {"id": 2, "server": "deployment", "tool": "get_logs", "depends_on": [1],
             "arguments": {"deployment_id": "{{steps.1.output.id}}", "level": "error"}},
        ],
    }
    llm = FakeLLM(json_queue=[plan_json], text="payments deploy d-payments-482 raised 5xx to 6.1%")

    async with MCPClientPool(cfg) as pool:
        orch = Orchestrator(
            cfg, pool, llm=llm, memory=TaskMemory(tmp_path / "mem.json")
        )
        result = await orch.run("Did the latest payments deployment break anything?")

    assert result.success
    assert result.plan.steps[1].output  # logs came back
    assert result.plan.steps[1].status.value == "succeeded"
    assert "6.1%" in result.answer
    assert result.metrics.tool_calls == 2
