/** Dependency-aware execution of a plan across MCP servers. */

import type { ApprovalPolicy, Approver } from "./approval.js";
import type { LLMClient } from "./llm.js";
import { errorMessage, type ToolPool } from "./mcp-client.js";
import { type Plan, type PlanStep, type RunMetrics, StepStatus } from "./models.js";
import { isPlainObject, RecoveryAction, type RecoveryPolicy, sleep } from "./recovery.js";
import type { ToolRegistry } from "./registry.js";
import { resolveTemplates, type StepOutputs, unresolvedRefs } from "./templating.js";

/** Called after each step changes state, for live UI rendering. */
export type StepListener = (step: PlanStep) => void;

export const ARG_RESOLVE_SYSTEM = `\
You are finalizing the arguments for one tool call inside a larger plan. Given the
overall goal, the tool schema, any partial arguments, and the observations produced
by completed prior steps, return the complete arguments as a single JSON object.
Use only information present in the observations or the goal.
`;

export class ReplanRequested extends Error {
  override name = "ReplanRequested";

  constructor(
    readonly reason: string,
    readonly failedStep: PlanStep,
  ) {
    super(reason);
  }
}

export interface ExecutorDeps {
  pool: ToolPool;
  registry: ToolRegistry;
  llm: LLMClient;
  approvalPolicy: ApprovalPolicy;
  approver: Approver;
  recovery: RecoveryPolicy;
  onStep?: StepListener;
}

export class Executor {
  private readonly onStep: StepListener;

  constructor(private readonly deps: ExecutorDeps) {
    this.onStep = deps.onStep ?? (() => {});
  }

  async run(plan: Plan, metrics: RunMetrics): Promise<StepOutputs> {
    const outputs: StepOutputs = new Map();
    for (const step of Executor.inDependencyOrder(plan)) {
      if (step.dependsOn.some((dep) => plan.step(dep).status !== StepStatus.Succeeded)) {
        step.status = StepStatus.Skipped;
        this.onStep(step);
        continue;
      }
      await this.runStep(step, plan, outputs, metrics);
      outputs.set(step.id, step.output);
    }
    return outputs;
  }

  // ------------------------------------------------------------------ one step

  private async runStep(
    step: PlanStep,
    plan: Plan,
    outputs: StepOutputs,
    metrics: RunMetrics,
  ): Promise<void> {
    let args = await this.prepareArguments(step, plan, outputs, metrics);

    if (this.deps.approvalPolicy.requiresApproval(step)) {
      metrics.approvalsRequested += 1;
      step.status = StepStatus.AwaitingApproval;
      this.onStep(step);
      if (!(await this.deps.approver(step, args))) {
        step.status = StepStatus.Rejected;
        step.error = "rejected by human approver";
        this.onStep(step);
        throw new ReplanRequested(`step ${step.id} (${step.qualifiedTool}) was rejected`, step);
      }
      metrics.approvalsGranted += 1;
    }

    for (;;) {
      step.attempts += 1;
      step.status = StepStatus.Running;
      this.onStep(step);
      const started = performance.now();
      try {
        const result = await this.deps.pool.callTool(step.server, step.tool, args);
        step.latencyMs = performance.now() - started;
        step.output = result;
        step.status = StepStatus.Succeeded;
        step.error = null;
        metrics.toolCalls += 1;
        this.onStep(step);
        return;
      } catch (err) {
        step.latencyMs = performance.now() - started;
        step.error = errorMessage(err);
        step.status = StepStatus.Failed;
        metrics.toolCalls += 1;
        metrics.toolFailures += 1;
        this.onStep(step);

        const decision = await this.deps.recovery.decide(step, args, step.error, metrics);
        switch (decision.action) {
          case RecoveryAction.Retry:
            metrics.retries += 1;
            await sleep(decision.backoffMs ?? 0);
            continue;
          case RecoveryAction.RepairArgs:
            metrics.retries += 1;
            args = decision.newArguments ?? args;
            continue;
          case RecoveryAction.Replan:
            throw new ReplanRequested(
              `step ${step.id} failed: ${step.error} (${decision.note})`,
              step,
            );
          default:
            throw new ReplanRequested(`step ${step.id} aborted: ${step.error}`, step);
        }
      }
    }
  }

  // ------------------------------------------------------------------ arguments

  private async prepareArguments(
    step: PlanStep,
    plan: Plan,
    outputs: StepOutputs,
    metrics: RunMetrics,
  ): Promise<Record<string, unknown>> {
    const args = resolveTemplates(step.arguments, outputs) as Record<string, unknown>;
    const needsLlm = step.resolveArgumentsAtRuntime || unresolvedRefs(args).length > 0;
    if (!needsLlm) return args;

    const spec = this.deps.registry.get(step.qualifiedTool);
    const depContext = step.dependsOn.length
      ? Object.fromEntries(step.dependsOn.map((dep) => [dep, outputs.get(dep) ?? null]))
      : Object.fromEntries(outputs);
    const user =
      `GOAL:\n${plan.goal}\n\n` +
      `TOOL: ${step.qualifiedTool}\n` +
      `TOOL SCHEMA:\n${JSON.stringify(spec?.inputSchema ?? {}, null, 2)}\n\n` +
      `PARTIAL ARGUMENTS:\n${JSON.stringify(step.arguments)}\n\n` +
      `OBSERVATIONS FROM PRIOR STEPS:\n${truncate(JSON.stringify(depContext))}`;
    const [data, resp] = await this.deps.llm.completeJson({
      system: ARG_RESOLVE_SYSTEM,
      user,
      maxTokens: 800,
    });
    metrics.plannerTokensIn += resp.tokensIn;
    metrics.plannerTokensOut += resp.tokensOut;
    return isPlainObject(data) ? data : args;
  }

  // ------------------------------------------------------------------ ordering

  /** Kahn's algorithm, lowest id first among ready steps for deterministic runs. */
  static inDependencyOrder(plan: Plan): PlanStep[] {
    const remaining = new Map(plan.steps.map((s) => [s.id, new Set(s.dependsOn)]));
    const ordered: PlanStep[] = [];
    while (remaining.size) {
      const ready = [...remaining.entries()]
        .filter(([, deps]) => deps.size === 0)
        .map(([id]) => id)
        .sort((a, b) => a - b);
      if (!ready.length) throw new Error("dependency cycle in plan");
      for (const id of ready) {
        ordered.push(plan.step(id));
        remaining.delete(id);
        for (const deps of remaining.values()) deps.delete(id);
      }
    }
    return ordered;
  }
}

export function truncate(text: string, limit = 6000): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}\n...[truncated]`;
}
