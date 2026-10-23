/**
 * End-to-end wiring test against the REAL custom MCP server, with a scripted LLM.
 *
 * Exercises: stdio connect + discovery -> plan -> dependency-aware execution with
 * {{steps.N.output}} flow -> tool errors surfacing as failures -> synthesis.
 * No API key or network needed (only the TypeScript `deployment` server is used).
 */

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { MCPClientPool, ToolCallError } from "../src/mcp-client.js";
import { TaskMemory } from "../src/memory.js";
import { Orchestrator } from "../src/orchestrator.js";
import { FakeLLM } from "./fakes.js";

const cfg = loadConfig("config/servers.yaml");
cfg.servers = cfg.servers.filter((s) => s.name === "deployment");

let pool: MCPClientPool;

beforeAll(async () => {
  pool = await MCPClientPool.connect(cfg);
}, 30_000);

afterAll(async () => {
  await pool?.close();
});

describe("deployment MCP server (real stdio)", () => {
  it("discovers tools and tags write tools from config", () => {
    const names = pool.allTools().map((t) => t.name);
    expect(names).toContain("get_latest_deployment");
    expect(pool.getTool("deployment", "rollback_deployment")?.kind).toBe("write");
    expect(pool.getTool("deployment", "get_logs")?.kind).toBe("read");
  });

  it("surfaces server-side errors as ToolCallError", async () => {
    await expect(pool.callTool("deployment", "get_deployment", { deployment_id: "nope" })).rejects.toBeInstanceOf(
      ToolCallError,
    );
  });

  it("runs a cross-step plan end to end", async () => {
    const llm = new FakeLLM(
      [
        {
          reasoning: "get the live payments deployment, then read its error logs",
          steps: [
            { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" }, depends_on: [] },
            {
              id: 2,
              server: "deployment",
              tool: "get_logs",
              depends_on: [1],
              arguments: { deployment_id: "{{steps.1.output.id}}", level: "error" },
            },
          ],
        },
      ],
      "payments deploy d-payments-482 raised 5xx to 6.1%",
    );
    const memory = new TaskMemory(join(mkdtempSync(join(tmpdir(), "switchboard-")), "mem.json"));

    const result = await new Orchestrator(cfg, pool, { llm, memory }).run(
      "Did the latest payments deployment break anything?",
    );

    expect(result.success).toBe(true);
    const logs = result.plan.steps[1]?.output as { level: string }[];
    expect(logs.length).toBe(3);
    expect(logs.every((l) => l.level === "error")).toBe(true);
    expect(result.answer).toContain("6.1%");
    expect(result.metrics.toolCalls).toBe(2);
  });
});
