"""Custom MCP server: a fake service-operations / deployment control plane.

Exposes read tools (deployments, health, logs) and one write tool (rollback) so the
orchestrator has a fourth heterogeneous system to reason about. Data is in-memory
and deterministic; the `payments` service has a deliberately bad latest deployment
so cross-system "did the last deploy break something?" demos have a real answer.

Run standalone:  python -m servers.deployment_server
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("deployment")

_NOW = dt.datetime(2026, 9, 5, 14, 0, 0, tzinfo=dt.timezone.utc)


def _iso(minutes_ago: int) -> str:
    return (_NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


DEPLOYMENTS: dict[str, dict[str, Any]] = {
    "d-payments-482": {
        "id": "d-payments-482",
        "service": "payments",
        "version": "2026.9.5-a1b2c3d",
        "commit_sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "environment": "production",
        "status": "live",
        "deployed_at": _iso(75),
        "deployed_by": "ci-bot",
        "error_rate_before": 0.004,
        "error_rate_after": 0.061,
        "p95_latency_ms_before": 240,
        "p95_latency_ms_after": 910,
    },
    "d-payments-481": {
        "id": "d-payments-481",
        "service": "payments",
        "version": "2026.9.3-9f8e7d6",
        "commit_sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c",
        "environment": "production",
        "status": "superseded",
        "deployed_at": _iso(2 * 24 * 60),
        "deployed_by": "ci-bot",
        "error_rate_before": 0.005,
        "error_rate_after": 0.004,
        "p95_latency_ms_before": 250,
        "p95_latency_ms_after": 240,
    },
    "d-checkout-193": {
        "id": "d-checkout-193",
        "service": "checkout",
        "version": "2026.9.4-1122334",
        "commit_sha": "1122334455667788990011223344556677889900",
        "environment": "production",
        "status": "live",
        "deployed_at": _iso(600),
        "deployed_by": "ashu",
        "error_rate_before": 0.006,
        "error_rate_after": 0.006,
        "p95_latency_ms_before": 180,
        "p95_latency_ms_after": 175,
    },
}

LOGS: dict[str, list[dict[str, Any]]] = {
    "d-payments-482": [
        {"ts": _iso(74), "level": "info", "msg": "rollout complete: 12/12 pods healthy"},
        {"ts": _iso(70), "level": "error", "msg": "NullPointerException in RefundService.settle() at Refund.java:88"},
        {"ts": _iso(66), "level": "error", "msg": "payment.capture timeout after 3000ms calling ledger-service"},
        {"ts": _iso(60), "level": "warn", "msg": "circuit breaker 'ledger' half-open"},
        {"ts": _iso(41), "level": "error", "msg": "5xx rate 6.1% over last 15m (threshold 1%)"},
    ],
    "d-payments-481": [
        {"ts": _iso(2 * 24 * 60 - 3), "level": "info", "msg": "rollout complete: 12/12 pods healthy"},
    ],
    "d-checkout-193": [
        {"ts": _iso(598), "level": "info", "msg": "rollout complete: 8/8 pods healthy"},
    ],
}

HEALTH: dict[str, dict[str, Any]] = {
    "payments": {"service": "payments", "status": "degraded", "error_rate": 0.061,
                 "p95_latency_ms": 910, "open_incidents": 1, "healthy_pods": "9/12"},
    "checkout": {"service": "checkout", "status": "healthy", "error_rate": 0.006,
                 "p95_latency_ms": 175, "open_incidents": 0, "healthy_pods": "8/8"},
    "ledger": {"service": "ledger", "status": "healthy", "error_rate": 0.002,
               "p95_latency_ms": 95, "open_incidents": 0, "healthy_pods": "6/6"},
}


@mcp.tool()
def list_services() -> list[str]:
    """List all services known to the deployment control plane."""
    return sorted(HEALTH.keys())


@mcp.tool()
def get_deployments(service: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return deployments for a service, newest first."""
    items = [d for d in DEPLOYMENTS.values() if d["service"] == service]
    items.sort(key=lambda d: d["deployed_at"], reverse=True)
    return items[:limit]


@mcp.tool()
def get_latest_deployment(service: str) -> dict[str, Any]:
    """Return the current live deployment for a service."""
    live = [d for d in DEPLOYMENTS.values() if d["service"] == service and d["status"] == "live"]
    if not live:
        raise ValueError(f"no live deployment for service '{service}'")
    return max(live, key=lambda d: d["deployed_at"])


@mcp.tool()
def get_deployment(deployment_id: str) -> dict[str, Any]:
    """Return full detail for one deployment by id."""
    if deployment_id not in DEPLOYMENTS:
        raise ValueError(f"unknown deployment '{deployment_id}'")
    return DEPLOYMENTS[deployment_id]


@mcp.tool()
def get_service_health(service: str) -> dict[str, Any]:
    """Return current health metrics for a service."""
    if service not in HEALTH:
        raise ValueError(f"unknown service '{service}'")
    return HEALTH[service]


@mcp.tool()
def get_logs(deployment_id: str, level: str | None = None) -> list[dict[str, Any]]:
    """Return log lines for a deployment, optionally filtered by level (info|warn|error)."""
    lines = LOGS.get(deployment_id, [])
    if level:
        lines = [ln for ln in lines if ln["level"] == level]
    return lines


@mcp.tool()
def rollback_deployment(deployment_id: str, reason: str) -> dict[str, Any]:
    """Roll a service back to the deployment that preceded `deployment_id`. WRITE."""
    if deployment_id not in DEPLOYMENTS:
        raise ValueError(f"unknown deployment '{deployment_id}'")
    target = DEPLOYMENTS[deployment_id]
    service = target["service"]
    prior = sorted(
        (d for d in DEPLOYMENTS.values() if d["service"] == service and d["deployed_at"] < target["deployed_at"]),
        key=lambda d: d["deployed_at"],
    )
    if not prior:
        raise ValueError(f"no earlier deployment to roll back to for '{service}'")
    restored = prior[-1]
    target["status"] = "rolled_back"
    restored["status"] = "live"
    HEALTH[service]["status"] = "recovering"
    return {
        "rolled_back": deployment_id,
        "restored": restored["id"],
        "service": service,
        "reason": reason,
        "at": _NOW.isoformat(),
    }


if __name__ == "__main__":
    mcp.run()
