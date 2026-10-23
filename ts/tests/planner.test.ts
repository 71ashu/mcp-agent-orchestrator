import { describe, expect, it } from "vitest";
import { extractJson } from "../src/llm.js";
import { RunMetrics } from "../src/models.js";
import { Planner, PlanValidationError } from "../src/planner.js";
import { deploymentRegistry, FakeLLM } from "./fakes.js";

const plannerWith = (plan: unknown) => new Planner(new FakeLLM([plan]), deploymentRegistry());

describe("Planner", () => {
  it("parses a valid plan and applies defaults", async () => {
    const plan = await plannerWith({
      reasoning: "look up the deployment then its logs",
      steps: [
        { id: 1, server: "deployment", tool: "get_latest_deployment", arguments: { service: "payments" }, depends_on: [] },
        { id: 2, server: "deployment", tool: "get_logs", depends_on: [1], arguments: { deployment_id: "{{steps.1.output.id}}" } },
      ],
    }).createPlan("check payments", new RunMetrics());

    expect(plan.steps).toHaveLength(2);
    expect(plan.steps[1]?.dependsOn).toEqual([1]);
    expect(plan.steps[0]?.resolveArgumentsAtRuntime).toBe(false);
    expect(plan.reasoning).toContain("logs");
  });

  it("records planner token usage", async () => {
    const metrics = new RunMetrics();
    await plannerWith({ steps: [] }).createPlan("x", metrics);
    expect(metrics.plannerTokensIn).toBe(10);
    expect(metrics.plannerTokensOut).toBe(5);
  });

  it("rejects an unknown tool", async () => {
    await expect(
      plannerWith({ steps: [{ id: 1, server: "deployment", tool: "nonexistent", arguments: {} }] }).createPlan(
        "x",
        new RunMetrics(),
      ),
    ).rejects.toBeInstanceOf(PlanValidationError);
  });

  it("rejects a dangling dependency", async () => {
    await expect(
      plannerWith({
        steps: [{ id: 1, server: "deployment", tool: "get_logs", arguments: {}, depends_on: [7] }],
      }).createPlan("x", new RunMetrics()),
    ).rejects.toThrow(/missing step 7/);
  });

  it("rejects duplicate step ids", async () => {
    const step = { id: 1, server: "deployment", tool: "get_logs", arguments: {} };
    await expect(plannerWith({ steps: [step, step] }).createPlan("x", new RunMetrics())).rejects.toThrow(
      /duplicate step id/,
    );
  });

  it("rejects malformed model output instead of executing it", async () => {
    await expect(
      plannerWith({ steps: [{ id: "one", server: "", tool: 42 }] }).createPlan("x", new RunMetrics()),
    ).rejects.toBeInstanceOf(PlanValidationError);
  });
});

describe("extractJson", () => {
  it("handles bare JSON, fenced JSON, and JSON wrapped in prose", () => {
    expect(extractJson('{"a":1}')).toEqual({ a: 1 });
    expect(extractJson('Here you go:\n```json\n{"a":2}\n```')).toEqual({ a: 2 });
    expect(extractJson('Sure! {"a":3} hope that helps')).toEqual({ a: 3 });
  });

  it("throws when there is no JSON", () => {
    expect(() => extractJson("no json here")).toThrow(/no JSON/);
  });
});
