"""Capability registry: turns discovered MCP tools into planner-facing context."""

from __future__ import annotations

import json

from .models import ToolSpec


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]):
        self.tools = tools
        self._by_qualified = {t.qualified_name: t for t in tools}

    def get(self, qualified_name: str) -> ToolSpec | None:
        return self._by_qualified.get(qualified_name)

    def servers(self) -> list[str]:
        return sorted({t.server for t in self.tools})

    def catalog_text(self, include_schema: bool = True) -> str:
        """Human/LLM-readable listing grouped by server."""
        lines: list[str] = []
        for server in self.servers():
            lines.append(f"## server: {server}")
            for tool in [t for t in self.tools if t.server == server]:
                tag = "WRITE" if tool.kind.value == "write" else "read"
                lines.append(f"- {tool.qualified_name}  [{tag}]")
                if tool.description:
                    lines.append(f"    {tool.description.strip()}")
                if include_schema and tool.input_schema:
                    props = tool.input_schema.get("properties", {})
                    required = set(tool.input_schema.get("required", []))
                    if props:
                        arg_bits = []
                        for pname, pdef in props.items():
                            typ = pdef.get("type", "any")
                            req = "*" if pname in required else ""
                            arg_bits.append(f"{pname}{req}:{typ}")
                        lines.append(f"    args: {', '.join(arg_bits)}")
            lines.append("")
        return "\n".join(lines).strip()

    def anthropic_tools(self) -> list[dict]:
        """Tool schemas in Anthropic tool-use format (used by runtime arg resolution)."""
        out = []
        for tool in self.tools:
            out.append(
                {
                    "name": tool.qualified_name.replace(".", "__"),
                    "description": tool.description or tool.qualified_name,
                    "input_schema": tool.input_schema
                    or {"type": "object", "properties": {}},
                }
            )
        return out

    def write_tools(self) -> list[ToolSpec]:
        return [t for t in self.tools if t.kind.value == "write"]

    def to_json(self) -> str:
        return json.dumps(
            [t.model_dump(mode="json") for t in self.tools], indent=2
        )
