"""Load and validate configuration (server registry + environment)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


class ServerConfig(BaseModel):
    name: str
    enabled: bool = True
    description: str = ""
    transport: str = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    write_tools: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    model: str = "claude-sonnet-5"
    max_plan_steps: int = 12
    max_step_attempts: int = 3
    max_replans: int = 2
    servers: list[ServerConfig] = Field(default_factory=list)

    @property
    def enabled_servers(self) -> list[ServerConfig]:
        return [s for s in self.servers if s.enabled]


def load_config(path: str | Path = "config/servers.yaml") -> AppConfig:
    load_dotenv()
    raw = yaml.safe_load(Path(path).read_text())
    raw = _expand(raw)
    servers = [ServerConfig(**s) for s in raw.get("servers", [])]
    return AppConfig(
        model=os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-5"),
        servers=servers,
    )
