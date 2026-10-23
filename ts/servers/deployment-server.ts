/**
 * Custom MCP server: a fake service-operations / deployment control plane.
 *
 * Exposes read tools (deployments, health, logs) and one write tool (rollback) so
 * the orchestrator has a heterogeneous system to reason about. Data is in-memory
 * and deterministic; the `payments` service has a deliberately bad latest
 * deployment so "did the last deploy break something?" demos have a real answer.
 *
 * Run standalone:  npm run server:deployment
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const NOW = Date.UTC(2026, 8, 5, 14, 0, 0);
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

interface Deployment {
  id: string;
  service: string;
  version: string;
  commit_sha: string;
  environment: string;
  status: "live" | "superseded" | "rolled_back";
  deployed_at: string;
  deployed_by: string;
  error_rate_before: number;
  error_rate_after: number;
  p95_latency_ms_before: number;
  p95_latency_ms_after: number;
}

interface LogLine {
  ts: string;
  level: "info" | "warn" | "error";
  msg: string;
}

interface Health {
  service: string;
  status: string;
  error_rate: number;
  p95_latency_ms: number;
  open_incidents: number;
  healthy_pods: string;
}

const DEPLOYMENTS: Record<string, Deployment> = {
  "d-payments-482": {
    id: "d-payments-482",
    service: "payments",
    version: "2026.9.5-a1b2c3d",
    commit_sha: "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
    environment: "production",
    status: "live",
    deployed_at: iso(75),
    deployed_by: "ci-bot",
    error_rate_before: 0.004,
    error_rate_after: 0.061,
    p95_latency_ms_before: 240,
    p95_latency_ms_after: 910,
  },
  "d-payments-481": {
    id: "d-payments-481",
    service: "payments",
    version: "2026.9.3-9f8e7d6",
    commit_sha: "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c",
    environment: "production",
    status: "superseded",
    deployed_at: iso(2 * 24 * 60),
    deployed_by: "ci-bot",
    error_rate_before: 0.005,
    error_rate_after: 0.004,
    p95_latency_ms_before: 250,
    p95_latency_ms_after: 240,
  },
  "d-checkout-193": {
    id: "d-checkout-193",
    service: "checkout",
    version: "2026.9.4-1122334",
    commit_sha: "1122334455667788990011223344556677889900",
    environment: "production",
    status: "live",
    deployed_at: iso(600),
    deployed_by: "ashu",
    error_rate_before: 0.006,
    error_rate_after: 0.006,
    p95_latency_ms_before: 180,
    p95_latency_ms_after: 175,
  },
};

const LOGS: Record<string, LogLine[]> = {
  "d-payments-482": [
    { ts: iso(74), level: "info", msg: "rollout complete: 12/12 pods healthy" },
    { ts: iso(70), level: "error", msg: "NullPointerException in RefundService.settle() at Refund.java:88" },
    { ts: iso(66), level: "error", msg: "payment.capture timeout after 3000ms calling ledger-service" },
    { ts: iso(60), level: "warn", msg: "circuit breaker 'ledger' half-open" },
    { ts: iso(41), level: "error", msg: "5xx rate 6.1% over last 15m (threshold 1%)" },
  ],
  "d-payments-481": [{ ts: iso(2 * 24 * 60 - 3), level: "info", msg: "rollout complete: 12/12 pods healthy" }],
  "d-checkout-193": [{ ts: iso(598), level: "info", msg: "rollout complete: 8/8 pods healthy" }],
};

const HEALTH: Record<string, Health> = {
  payments: { service: "payments", status: "degraded", error_rate: 0.061, p95_latency_ms: 910, open_incidents: 1, healthy_pods: "9/12" },
  checkout: { service: "checkout", status: "healthy", error_rate: 0.006, p95_latency_ms: 175, open_incidents: 0, healthy_pods: "8/8" },
  ledger: { service: "ledger", status: "healthy", error_rate: 0.002, p95_latency_ms: 95, open_incidents: 0, healthy_pods: "6/6" },
};

const json = (value: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(value) }] });
const newestFirst = (a: Deployment, b: Deployment) => b.deployed_at.localeCompare(a.deployed_at);

export function createDeploymentServer(): McpServer {
  const server = new McpServer({ name: "deployment", version: "0.1.0" });

  server.registerTool(
    "list_services",
    { description: "List all services known to the deployment control plane." },
    async () => json(Object.keys(HEALTH).sort()),
  );

  server.registerTool(
    "get_deployments",
    {
      description: "Return deployments for a service, newest first.",
      inputSchema: { service: z.string(), limit: z.number().int().positive().default(10) },
    },
    async ({ service, limit }) =>
      json(Object.values(DEPLOYMENTS).filter((d) => d.service === service).sort(newestFirst).slice(0, limit)),
  );

  server.registerTool(
    "get_latest_deployment",
    { description: "Return the current live deployment for a service.", inputSchema: { service: z.string() } },
    async ({ service }) => {
      const live = Object.values(DEPLOYMENTS)
        .filter((d) => d.service === service && d.status === "live")
        .sort(newestFirst);
      if (!live[0]) throw new Error(`no live deployment for service '${service}'`);
      return json(live[0]);
    },
  );

  server.registerTool(
    "get_deployment",
    { description: "Return full detail for one deployment by id.", inputSchema: { deployment_id: z.string() } },
    async ({ deployment_id }) => {
      const d = DEPLOYMENTS[deployment_id];
      if (!d) throw new Error(`unknown deployment '${deployment_id}'`);
      return json(d);
    },
  );

  server.registerTool(
    "get_service_health",
    { description: "Return current health metrics for a service.", inputSchema: { service: z.string() } },
    async ({ service }) => {
      const h = HEALTH[service];
      if (!h) throw new Error(`unknown service '${service}'`);
      return json(h);
    },
  );

  server.registerTool(
    "get_logs",
    {
      description: "Return log lines for a deployment, optionally filtered by level (info|warn|error).",
      inputSchema: { deployment_id: z.string(), level: z.enum(["info", "warn", "error"]).optional() },
    },
    async ({ deployment_id, level }) => {
      const lines = LOGS[deployment_id] ?? [];
      return json(level ? lines.filter((l) => l.level === level) : lines);
    },
  );

  server.registerTool(
    "rollback_deployment",
    {
      description: "Roll a service back to the deployment that preceded `deployment_id`. WRITE.",
      inputSchema: { deployment_id: z.string(), reason: z.string() },
      annotations: { destructiveHint: true },
    },
    async ({ deployment_id, reason }) => {
      const target = DEPLOYMENTS[deployment_id];
      if (!target) throw new Error(`unknown deployment '${deployment_id}'`);
      const prior = Object.values(DEPLOYMENTS)
        .filter((d) => d.service === target.service && d.deployed_at < target.deployed_at)
        .sort(newestFirst);
      const restored = prior[0];
      if (!restored) throw new Error(`no earlier deployment to roll back to for '${target.service}'`);
      target.status = "rolled_back";
      restored.status = "live";
      const health = HEALTH[target.service];
      if (health) health.status = "recovering";
      return json({
        rolled_back: deployment_id,
        restored: restored.id,
        service: target.service,
        reason,
        at: new Date(NOW).toISOString(),
      });
    },
  );

  return server;
}

await createDeploymentServer().connect(new StdioServerTransport());
