/** Top-level agent loop: plan -> execute -> (recover / replan) -> synthesize. */

import { type Approver, autoApprove, WriteToolPolicy } from "./approval.js";
import type { AppConfig } from "./config.js";
import { Executor, ReplanRequested, type StepListener, truncate } from "./executor.js";
import { LLM, type LLMClient } from "./llm.js";
import type { ToolPool } from "./mcp-client.js";
import { TaskMemory } from "./memory.js";
import { type Plan, RunMetrics, type RunResult } from "./models.js";
import { Planner } from "./planner.js";
import { RecoveryPolicy } from "./recovery.js";
import { ToolRegistry } from "./registry.js";
import type { StepOutputs } from "./templating.js";

export type PlanListener = (plan: Plan) => void;

export const SYNTHESIS_SYSTEM = `\
You are the final synthesizer for a multi-server agent. Given the user's goal, the
executed plan, and every step's observation, write a direct, well-structured answer
to the goal. Cite which tool/server produced each key fact. If the evidence is
incomplete, say so explicitly rather than guessing.
`;

export interface OrchestratorOptions {
  approver?: Approver;
  memory?: TaskMemory;
  onPlan?: PlanListener;
  onStep?: StepListener;
  llm?: LLMClient;
}

export class Orchestrator {
  readonly llm: LLMClient;
  readonly registry: ToolRegistry;
  readonly memory: TaskMemory;
  private readonly approver: Approver;
  private readonly onPlan: PlanListener;
  private readonly onStep: StepListener;
  private readonly planner: Planner;
  private readonly recovery: RecoveryPolicy;
  private readonly approvalPolicy: WriteToolPolicy;

  constructor(
    private readonly config: AppConfig,
    private readonly pool: ToolPool,
    opts: OrchestratorOptions = {},
  ) {
    this.llm = opts.llm ?? new LLM(config.model);
    this.registry = new ToolRegistry(pool.allTools());
    this.memory = opts.memory ?? new TaskMemory();
    this.approver = opts.approver ?? autoApprove;
    this.onPlan = opts.onPlan ?? (() => {});
    this.onStep = opts.onStep ?? (() => {});

    this.planner = new Planner(this.llm, this.registry, config.maxPlanSteps);
    this.recovery = new RecoveryPolicy(this.llm, this.registry, config.maxStepAttempts);
    this.approvalPolicy = new WriteToolPolicy(this.registry);
  }

  async run(goal: string): Promise<RunResult> {
    const metrics = new RunMetrics();
    const hint = this.memory.hintFor(goal);
    let plan = await this.planner.createPlan(goal, metrics, hint);
    this.onPlan(plan);

    const executor = new Executor({
      pool: this.pool,
      registry: this.registry,
      llm: this.llm,
      approvalPolicy: this.approvalPolicy,
      approver: this.approver,
      recovery: this.recovery,
      onStep: this.onStep,
    });

    let outputs: StepOutputs = new Map();
    for (let attempt = 0; attempt <= this.config.maxReplans; attempt++) {
      try {
        outputs = await executor.run(plan, metrics);
        break;
      } catch (err) {
        if (!(err instanceof ReplanRequested)) throw err;
        if (attempt >= this.config.maxReplans) {
          return this.finalize(goal, plan, metrics, false, `Could not complete the task: ${err.reason}`);
        }
        metrics.replans += 1;
        const observed = JSON.stringify(summarizeOutputs(collectOutputs(plan))).slice(0, 2000);
        plan = await this.planner.createPlan(
          goal,
          metrics,
          `${hint ?? ""}\n\nPrevious attempt failed: ${err.reason}. Observations so far: ${observed}`,
        );
        this.onPlan(plan);
      }
    }

    const answer = await this.synthesize(goal, plan, outputs, metrics);
    return this.finalize(goal, plan, metrics, true, answer);
  }

  // ------------------------------------------------------------------ helpers

  private async synthesize(
    goal: string,
    plan: Plan,
    outputs: StepOutputs,
    metrics: RunMetrics,
  ): Promise<string> {
    const transcript = plan.steps.map((step) => ({
      step: step.id,
      tool: step.qualifiedTool,
      status: step.status,
      observation: outputs.get(step.id) ?? null,
      error: step.error,
    }));
    const resp = await this.llm.complete({
      system: SYNTHESIS_SYSTEM,
      user: `GOAL:\n${goal}\n\nEXECUTION TRANSCRIPT:\n${truncate(JSON.stringify(transcript, null, 2), 12000)}`,
      maxTokens: 1500,
    });
    metrics.synthesisTokensIn += resp.tokensIn;
    metrics.synthesisTokensOut += resp.tokensOut;
    return resp.text.trim();
  }

  private finalize(
    goal: string,
    plan: Plan,
    metrics: RunMetrics,
    success: boolean,
    answer: string,
  ): RunResult {
    metrics.finishedAt = Date.now();
    const result: RunResult = { goal, plan, answer, success, metrics };
    try {
      this.memory.record(result);
    } catch {
      // memory is best-effort; never fail a run because the file couldn't be written
    }
    return result;
  }
}

/**
 * Outputs of steps that succeeded before the failure. The executor throws before
 * returning its map, so read them off the plan instead (the Python version passed
 * an always-empty dict here, which left the replanner without observations).
 */
function collectOutputs(plan: Plan): StepOutputs {
  return new Map(
    plan.steps.filter((s) => s.status === "succeeded").map((s) => [s.id, s.output]),
  );
}

function summarizeOutputs(outputs: StepOutputs): Record<number, unknown> {
  return Object.fromEntries(
    [...outputs].map(([k, v]) => [
      k,
      ["number", "string", "boolean"].includes(typeof v) ? v : truncate(JSON.stringify(v) ?? "", 400),
    ]),
  );
}
