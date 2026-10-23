import { describe, expect, it } from "vitest";
import { type Approver, autoApprove, autoReject, WriteToolPolicy } from "../src/approval.js";
import { Executor, ReplanRequested } from "../src/executor.js";
import { ToolCallError } from "../src/mcp-client.js";
import { Plan, PlanStep, type PlanStepInput, RunMetrics, StepStatus } from "../src/models.js";
import { RecoveryPolicy } from "../src/recovery.js";
import { deploymentRegistry, deploymentTools, FakeLLM, FakePool } from "./fakes.js";

function executor(pool: FakePool, llm = new FakeLLM(), approver: Approver = autoApprove) {
  const registry = deploymentRegistry();
  return new Executor({
    pool,
    registry,
    llm,
    approvalPolicy: new WriteToolPolicy(registry),
    approver,
    recovery: new RecoveryPolicy(llm, registry, 3, 0),
  });
}

const plan = (goal: string, steps: PlanStepInput[]) => new Plan(goal, "", steps.map((s) => new PlanStep(s)));

describe("Executor", () => {
  it("runs steps in dependency order and flows outputs through templates", async () => {
    const pool = new FakePool(
      {
        "deployment.get_latest_deployment": (a) => ({ id: "d-482", service: a.service }),
        "deployment.get_logs": (a) => (a.deployment_id === "d-482" ? [{ level: "error", msg: "boom" }] : []),
      },
      deploymentTools(),
    );
    // Declared out of order on purpose: step 2 must still run after step 1.
    const p = plan("check payments", [
      { id: 2, server: "deployment", tool: "get_logs", depends_on: [1], arguments: { deployment_id: "{{steps.1.output.id}}" } },
      { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } },
    ]);

    const outputs = await executor(pool).run(p, new RunMetrics());

    expect(pool.calls.map((c) => c[1])).toEqual(["get_latest_deployment", "get_logs"]);
    expect(pool.calls[1]?.[2]).toEqual({ deployment_id: "d-482" });
    expect(outputs.get(2)).toEqual([{ level: "error", msg: "boom" }]);
    expect(p.step(2).status).toBe(StepStatus.Succeeded);
  });

  it("gates write tools and replans when the human rejects", async () => {
    const pool = new FakePool({ "deployment.rollback_deployment": () => ({ ok: true }) }, deploymentTools());
    const p = plan("roll back", [
      { id: 1, server: "deployment", tool: "rollback_deployment", arguments: { deployment_id: "d-482" } },
    ]);
    const metrics = new RunMetrics();

    await expect(executor(pool, new FakeLLM(), autoReject).run(p, metrics)).rejects.toBeInstanceOf(ReplanRequested);
    expect(p.step(1).status).toBe(StepStatus.Rejected);
    expect(pool.calls).toEqual([]);
    expect(metrics.approvalsRequested).toBe(1);
    expect(metrics.approvalsGranted).toBe(0);
  });

  it("shows the approver the resolved arguments, not the templates", async () => {
    const seen: Record<string, unknown>[] = [];
    const pool = new FakePool(
      {
        "deployment.get_latest_deployment": () => ({ id: "d-482" }),
        "deployment.rollback_deployment": () => ({ ok: true }),
      },
      deploymentTools(),
    );
    const p = plan("roll back latest", [
      { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } },
      { id: 2, server: "deployment", tool: "rollback_deployment", depends_on: [1], arguments: { deployment_id: "{{steps.1.output.id}}" } },
    ]);
    await executor(pool, new FakeLLM(), (_s, args) => (seen.push(args), true)).run(p, new RunMetrics());
    expect(seen).toEqual([{ deployment_id: "d-482" }]);
  });

  it("retries transient errors with backoff", async () => {
    let n = 0;
    const pool = new FakePool(
      {
        "deployment.get_latest_deployment": () =>
          ++n === 1 ? new ToolCallError("rate limit exceeded (429)") : { id: "d-1" },
      },
      deploymentTools(),
    );
    const p = plan("g", [
      { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } },
    ]);
    const metrics = new RunMetrics();

    const outputs = await executor(pool).run(p, metrics);

    expect(outputs.get(1)).toEqual({ id: "d-1" });
    expect(metrics.retries).toBe(1);
    expect(metrics.toolFailures).toBe(1);
    expect(p.step(1).attempts).toBe(2);
  });

  it("repairs bad arguments with the LLM and retries", async () => {
    const pool = new FakePool(
      {
        "deployment.get_latest_deployment": (a) =>
          a.service === "payments" ? { id: "d-482" } : new ToolCallError(`unknown service '${String(a.service)}'`),
      },
      deploymentTools(),
    );
    const llm = new FakeLLM([{ service: "payments" }]); // the repair suggestion
    const p = plan("g", [
      { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "Payments Service" } },
    ]);
    const metrics = new RunMetrics();

    const outputs = await executor(pool, llm).run(p, metrics);

    expect(outputs.get(1)).toEqual({ id: "d-482" });
    expect(pool.calls.map((c) => c[2])).toEqual([{ service: "Payments Service" }, { service: "payments" }]);
    expect(metrics.recoveryTokensIn).toBeGreaterThan(0);
  });

  it("asks for a replan when the error is not repairable", async () => {
    const pool = new FakePool(
      { "deployment.get_logs": () => new ToolCallError("deployment not found") },
      deploymentTools(),
    );
    const llm = new FakeLLM([{ unfixable: true }]);
    const p = plan("g", [{ id: 1, server: "deployment", tool: "get_logs", arguments: { deployment_id: "nope" } }]);

    await expect(executor(pool, llm).run(p, new RunMetrics())).rejects.toThrow(/args not repairable/);
  });

  it("detects dependency cycles", () => {
    const p = plan("g", [
      { id: 1, server: "deployment", tool: "get_logs", depends_on: [2], arguments: {} },
      { id: 2, server: "deployment", tool: "get_logs", depends_on: [1], arguments: {} },
    ]);
    expect(() => Executor.inDependencyOrder(p)).toThrow(/cycle/);
  });

  it("fills runtime arguments from prior observations via the LLM", async () => {
    const pool = new FakePool(
      {
        "deployment.get_latest_deployment": () => ({ id: "d-482" }),
        "deployment.get_logs": (a) => [{ for: a.deployment_id }],
      },
      deploymentTools(),
    );
    const llm = new FakeLLM([{ deployment_id: "d-482" }]);
    const p = plan("g", [
      { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" } },
      { id: 2, server: "deployment", tool: "get_logs", depends_on: [1], resolve_arguments_at_runtime: true },
    ]);

    const outputs = await executor(pool, llm).run(p, new RunMetrics());

    expect(outputs.get(2)).toEqual([{ for: "d-482" }]);
    expect(llm.prompts.at(-1)).toContain('"id":"d-482"');
  });
});
