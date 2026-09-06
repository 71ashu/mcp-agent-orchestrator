"""Offline test doubles — no network, no API key required."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.models import ToolKind, ToolSpec
from orchestrator.registry import ToolRegistry


class FakePool:
    """Stands in for MCPClientPool. `handlers[(server, tool)] -> callable(args)`."""

    def __init__(self, handlers: dict[tuple[str, str], Any], tools: list[ToolSpec]):
        self.handlers = handlers
        self._tools = tools
        self.calls: list[tuple[str, str, dict]] = []

    def all_tools(self) -> list[ToolSpec]:
        return self._tools

    def get_tool(self, server: str, name: str) -> ToolSpec | None:
        return next((t for t in self._tools if t.server == server and t.name == name), None)

    async def call_tool(self, server: str, name: str, arguments: dict) -> Any:
        self.calls.append((server, name, arguments))
        fn = self.handlers[(server, name)]
        result = fn(arguments)
        if isinstance(result, Exception):
            raise result
        return result


class FakeLLM:
    """Returns canned JSON/text; records prompts."""

    def __init__(self, json_queue: list[Any] | None = None, text: str = "synthesized answer"):
        self.json_queue = list(json_queue or [])
        self.text = text
        self.prompts: list[str] = []

    def complete(self, *, system: str, user: str, **_kw):
        self.prompts.append(user)
        from orchestrator.llm import LLMResponse

        return LLMResponse(text=self.text, tokens_in=10, tokens_out=5)

    def complete_json(self, *, system: str, user: str, **_kw):
        self.prompts.append(user)
        from orchestrator.llm import LLMResponse

        data = self.json_queue.pop(0) if self.json_queue else {}
        return data, LLMResponse(text="{}", tokens_in=10, tokens_out=5)


@pytest.fixture
def deployment_tools() -> list[ToolSpec]:
    return [
        ToolSpec(server="deployment", name="get_latest_deployment", description="latest",
                 input_schema={"type": "object", "properties": {"service": {"type": "string"}},
                               "required": ["service"]}),
        ToolSpec(server="deployment", name="get_logs", description="logs",
                 input_schema={"type": "object", "properties": {"deployment_id": {"type": "string"}}}),
        ToolSpec(server="deployment", name="rollback_deployment", description="rollback",
                 kind=ToolKind.WRITE,
                 input_schema={"type": "object", "properties": {"deployment_id": {"type": "string"}}}),
    ]


@pytest.fixture
def registry(deployment_tools) -> ToolRegistry:
    return ToolRegistry(deployment_tools)
