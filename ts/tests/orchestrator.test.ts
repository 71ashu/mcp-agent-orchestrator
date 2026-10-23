import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { Approver } from "../src/approval.js";
import type { AppConfig } from "../src/config.js";
import { TaskMemory } from "../src/memory.js";
import { Orchestrator } from "../src/orchestrator.js";
import { deploymentTools, FakeLLM, FakePool } from "./fakes.js";

const config: AppConfig = { model: "test", maxPlanSteps: 12, maxStepAttempts: 3, maxReplans: 2, servers: [] };
const tmpMemory = () => new TaskMemory(join(mkdtempSync(join(tmpdir(), "switchboard-")), "mem.json"));

const lookupThenRollback = {
  reasoning: "find the live deployment, then roll it back",
  steps: [
    { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } },
    { id: 2, server: "deployment", tool: "rollback_deployment", depends_on: [1], arguments: { deployment_id: "{{steps.1.output.id}}" } },
  ],
};
const lookupOnly = {
  reasoning: "rollback was declined; report findings instead",
  steps: [{ id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } }],
};

function pool() {
  return new FakePool(
    {
      "deployment.get_latest_deployment": () => ({ id: "d-482", error_rate_after: 0.061 }),
      "deployment.rollback_deployment": () => ({ rolled_back: "d-482" }),
    },
    deploymentTools(),
  );
}

describe("Orchestrator", () => {
  it("replans after a rejected write and feeds prior observations to the planner", async () => {
    const llm = new FakeLLM([lookupThenRollback, lookupOnly], "payments is degraded; rollback not performed");
    const p = pool();
    let asked = 0;
    const rejectOnce: Approver = () => (asked++, false);

    const result = await new Orchestrator(config, p, { llm, approver: rejectOnce, memory: tmpMemory() }).run(
      "Roll back the bad payments deployment",
    );

    expect(result.success).toBe(true);
    expect(result.metrics.replans).toBe(1);
    expect(asked).toBe(1);
    expect(p.calls.some((c) => c[1] === "rollback_deployment")).toBe(false);
    const replanPrompt = llm.prompts[1] ?? "";
    expect(replanPrompt).toContain("was rejected");
    expect(replanPrompt).toContain("d-482"); // observation from the first attempt survives the replan
  });

  it("gives up after the replan budget and reports failure", async () => {
    const llm = new FakeLLM([lookupThenRollback, lookupThenRollback, lookupThenRollback]);
    const memory = tmpMemory();

    const result = await new Orchestrator(config, pool(), { llm, approver: () => false, memory }).run("roll back");

    expect(result.success).toBe(false);
    expect(result.answer).toMatch(/^Could not complete the task/);
    expect(result.metrics.replans).toBe(2);
    expect(memory.entries).toHaveLength(1);
    expect(memory.entries[0]?.success).toBe(false);
  });

  it("uses memory of past successes as a planner hint", async () => {
    const memory = tmpMemory();
    const first = new FakeLLM([lookupOnly]);
    await new Orchestrator(config, pool(), { llm: first, memory }).run("check the payments deployment status");

    const second = new FakeLLM([lookupOnly]);
    await new Orchestrator(config, pool(), { llm: second, memory }).run("payments deployment health check");

    expect(second.prompts[0]).toContain("Similar past tasks that succeeded");
    expect(second.prompts[0]).toContain("deployment.get_latest_deployment");
  });
});
