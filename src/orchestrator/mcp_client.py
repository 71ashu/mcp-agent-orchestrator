"""Multi-server MCP client.

Owns one stdio connection per enabled server, discovers each server's tools, and
dispatches tool calls to the right session.
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import AppConfig, ServerConfig
from .models import ToolKind, ToolSpec


class ServerConnection:
    def __init__(self, config: ServerConfig, session: ClientSession):
        self.config = config
        self.session = session
        self.tools: list[ToolSpec] = []

    async def discover(self) -> list[ToolSpec]:
        result = await self.session.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            kind = (
                ToolKind.WRITE
                if tool.name in self.config.write_tools
                else ToolKind.READ
            )
            specs.append(
                ToolSpec(
                    server=self.config.name,
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                    kind=kind,
                )
            )
        self.tools = specs
        return specs


class MCPClientPool:
    """Connects to every enabled server and keeps the sessions open for the run."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._stack = AsyncExitStack()
        self.connections: dict[str, ServerConnection] = {}

    async def __aenter__(self) -> MCPClientPool:  # noqa: PYI034
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def connect_all(self) -> None:
        for server in self.config.enabled_servers:
            await self._connect(server)

    async def _connect(self, server: ServerConfig) -> None:
        command = _resolve_command(server.command)
        project_root = str(Path.cwd())
        child_env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                p for p in [project_root, str(Path("src").resolve()), os.environ.get("PYTHONPATH", "")] if p
            ),
            **server.env,
        }
        params = StdioServerParameters(
            command=command,
            args=server.args,
            env=child_env,
            cwd=project_root,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        conn = ServerConnection(server, session)
        await conn.discover()
        self.connections[server.name] = conn

    async def aclose(self) -> None:
        await self._stack.aclose()
        self.connections.clear()

    # ------------------------------------------------------------------ queries

    def all_tools(self) -> list[ToolSpec]:
        tools: list[ToolSpec] = []
        for conn in self.connections.values():
            tools.extend(conn.tools)
        return tools

    def get_tool(self, server: str, name: str) -> ToolSpec | None:
        conn = self.connections.get(server)
        if not conn:
            return None
        return next((t for t in conn.tools if t.name == name), None)

    # ------------------------------------------------------------------ dispatch

    async def call_tool(self, server: str, name: str, arguments: dict[str, Any]) -> Any:
        conn = self.connections.get(server)
        if not conn:
            raise KeyError(f"unknown server '{server}'")
        result = await conn.session.call_tool(name, arguments or {})
        if result.isError:
            raise ToolCallError(_stringify_content(result.content))
        return _normalize_content(result.content)


class ToolCallError(RuntimeError):
    """Raised when an MCP server reports a tool execution error."""


def _resolve_command(command: str) -> str:
    """Map a bare `python`/`python3` to the interpreter running the orchestrator so
    stdio child servers don't accidentally launch under a different Python."""
    if command in ("python", "python3"):
        return sys.executable
    return shutil.which(command) or command


def _stringify_content(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts) or "unknown tool error"


def _normalize_content(content: list[Any]) -> Any:
    """Collapse MCP content blocks into plain Python for the LLM and templates."""
    import json

    texts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is None:
            texts.append(str(block))
            continue
        try:
            return json.loads(text)  # many servers return a single JSON text block
        except (ValueError, TypeError):
            texts.append(text)
    if len(texts) == 1:
        return texts[0]
    return texts
