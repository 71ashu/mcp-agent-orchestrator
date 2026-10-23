/** Intent analysis + multi-server task planning. */

import { z } from "zod";
import type { LLMClient } from "./llm.js";
import { Plan, PlanInput, PlanStep, type RunMetrics } from "./models.js";
import type { ToolRegistry } from "./registry.js";

export const PLANNER_SYSTEM = `\
You are the planner for a multi-server MCP agent orchestrator. You are given a user
goal and a catalog of tools exposed by several connected MCP servers. Produce a
minimal, dependency-aware execution plan that achieves the goal.

Rules:
- Use ONLY tools that appear in the catalog, addressed as "<server>.<tool>".
- Prefer the fewest steps that fully answer the goal. Do not add exploratory steps
  unless the goal genuinely needs them.
- Express data flow with \`depends_on\` (list of earlier step ids). A step runs only
  after its dependencies succeed.
- If a step's arguments depend on the OUTPUT of an earlier step, either:
  (a) reference it with a template string like "{{steps.1.output}}" or a path into
      it such as "{{steps.1.output.deployment.commit_sha}}", OR
  (b) set "resolve_arguments_at_runtime": true and leave arguments partial; the
      executor will fill them from live observations.
- Steps that only READ data need no special handling. WRITE tools (tagged [WRITE])
  will be gated for human approval automatically. Still include them when the goal
  asks for an action.

Return JSON:
{
  "reasoning": "one short paragraph on the approach",
  "steps": [
    {
      "id": 1,
      "server": "github",
      "tool": "search_commits",
      "arguments": {"q": "repo:acme/payments"},
      "depends_on": [],
      "rationale": "why this step",
      "resolve_arguments_at_runtime": false
    }
  ]
}
`;

export class PlanValidationError extends Error {
  override name = "PlanValidationError";
}

export class Planner {
  constructor(
    private readonly llm: LLMClient,
    private readonly registry: ToolRegistry,
    private readonly maxSteps = 12,
  ) {}

  async createPlan(goal: string, metrics: RunMetrics, hint?: string | null): Promise<Plan> {
    let user =
      `GOAL:\n${goal}\n\n` +
      `TOOL CATALOG:\n${this.registry.catalogText()}\n\n` +
      `Plan at most ${this.maxSteps} steps.`;
    if (hint) user += `\n\nADDITIONAL CONTEXT:\n${hint}`;

    const [data, resp] = await this.llm.completeJson({
      system: PLANNER_SYSTEM,
      user,
      maxTokens: 2500,
    });
    metrics.plannerTokensIn += resp.tokensIn;
    metrics.plannerTokensOut += resp.tokensOut;

    const parsed = PlanInput.safeParse(data ?? {});
    if (!parsed.success) {
      throw new PlanValidationError(`planner returned a malformed plan: ${z.prettifyError(parsed.error)}`);
    }
    const steps = parsed.data.steps.map((s) => new PlanStep(s));
    this.validate(steps);
    return new Plan(goal, parsed.data.reasoning, steps);
  }

  private validate(steps: PlanStep[]): void {
    if (steps.length > this.maxSteps) {
      throw new PlanValidationError(`plan has ${steps.length} steps (max ${this.maxSteps})`);
    }
    const ids = new Set<number>();
    for (const s of steps) {
      if (ids.has(s.id)) throw new PlanValidationError(`duplicate step id ${s.id}`);
      ids.add(s.id);
    }
    for (const s of steps) {
      if (!this.registry.get(s.qualifiedTool)) {
        throw new PlanValidationError(`step ${s.id} references unknown tool '${s.qualifiedTool}'`);
      }
      for (const dep of s.dependsOn) {
        if (dep === s.id) throw new PlanValidationError(`step ${s.id} depends on itself`);
        if (!ids.has(dep)) {
          throw new PlanValidationError(`step ${s.id} depends on missing step ${dep}`);
        }
      }
    }
  }
}
