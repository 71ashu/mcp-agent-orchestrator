/** Core data models shared across the orchestrator. */

import { z } from "zod";

export const StepStatus = {
  Pending: "pending",
  Running: "running",
  Succeeded: "succeeded",
  Failed: "failed",
  Skipped: "skipped",
  AwaitingApproval: "awaiting_approval",
  Rejected: "rejected",
} as const;
export type StepStatus = (typeof StepStatus)[keyof typeof StepStatus];

export type ToolKind = "read" | "write";

export type JsonSchema = Record<string, unknown>;

/** A single tool exposed by an MCP server, namespaced by server. */
export interface ToolSpec {
  server: string;
  name: string;
  description: string;
  inputSchema: JsonSchema;
  kind: ToolKind;
}

export function qualifiedName(tool: Pick<ToolSpec, "server" | "name">): string {
  return `${tool.server}.${tool.name}`;
}

/**
 * Shape of one step as the planner LLM emits it (snake_case JSON). Parsing through
 * zod gives us defaults plus a hard failure on malformed model output, instead of
 * letting a bad plan leak into execution.
 */
export const PlanStepInput = z.object({
  id: z.coerce.number().int(),
  server: z.string().min(1),
  tool: z.string().min(1),
  // Arguments may contain template references to earlier outputs, e.g.
  // "{{steps.1.output}}" or "{{steps.1.output.commits[0].sha}}".
  arguments: z.record(z.string(), z.unknown()).default({}),
  depends_on: z.array(z.coerce.number().int()).default([]),
  rationale: z.string().default(""),
  // When true, the executor asks the LLM to finalize `arguments` at run time using
  // the goal + observations from completed dependencies.
  resolve_arguments_at_runtime: z.boolean().default(false),
});
export type PlanStepInput = z.input<typeof PlanStepInput>;

export const PlanInput = z.object({
  reasoning: z.string().default(""),
  steps: z.array(PlanStepInput).default([]),
});

export class PlanStep {
  readonly id: number;
  readonly server: string;
  readonly tool: string;
  arguments: Record<string, unknown>;
  readonly dependsOn: number[];
  readonly rationale: string;
  readonly resolveArgumentsAtRuntime: boolean;

  status: StepStatus = StepStatus.Pending;
  output: unknown = null;
  error: string | null = null;
  attempts = 0;
  latencyMs: number | null = null;

  constructor(input: PlanStepInput) {
    const s = PlanStepInput.parse(input);
    this.id = s.id;
    this.server = s.server;
    this.tool = s.tool;
    this.arguments = s.arguments;
    this.dependsOn = s.depends_on;
    this.rationale = s.rationale;
    this.resolveArgumentsAtRuntime = s.resolve_arguments_at_runtime;
  }

  get qualifiedTool(): string {
    return `${this.server}.${this.tool}`;
  }

  toJSON() {
    return {
      id: this.id,
      server: this.server,
      tool: this.tool,
      arguments: this.arguments,
      depends_on: this.dependsOn,
      rationale: this.rationale,
      resolve_arguments_at_runtime: this.resolveArgumentsAtRuntime,
      status: this.status,
      output: this.output,
      error: this.error,
      attempts: this.attempts,
      latency_ms: this.latencyMs,
    };
  }
}

export class Plan {
  constructor(
    readonly goal: string,
    readonly reasoning: string = "",
    readonly steps: PlanStep[] = [],
  ) {}

  step(stepId: number): PlanStep {
    const found = this.steps.find((s) => s.id === stepId);
    if (!found) throw new Error(`no step ${stepId}`);
    return found;
  }
}

export class RunMetrics {
  startedAt = Date.now();
  finishedAt: number | null = null;
  plannerTokensIn = 0;
  plannerTokensOut = 0;
  synthesisTokensIn = 0;
  synthesisTokensOut = 0;
  recoveryTokensIn = 0;
  recoveryTokensOut = 0;
  toolCalls = 0;
  toolFailures = 0;
  retries = 0;
  replans = 0;
  approvalsRequested = 0;
  approvalsGranted = 0;

  get wallMs(): number {
    return (this.finishedAt ?? Date.now()) - this.startedAt;
  }

  get totalTokensIn(): number {
    return this.plannerTokensIn + this.synthesisTokensIn + this.recoveryTokensIn;
  }

  get totalTokensOut(): number {
    return this.plannerTokensOut + this.synthesisTokensOut + this.recoveryTokensOut;
  }

  estimatedCostUsd(inPerMtok = 3.0, outPerMtok = 15.0): number {
    return (
      (this.totalTokensIn / 1_000_000) * inPerMtok +
      (this.totalTokensOut / 1_000_000) * outPerMtok
    );
  }

  toJSON() {
    return {
      started_at: this.startedAt,
      finished_at: this.finishedAt,
      planner_tokens_in: this.plannerTokensIn,
      planner_tokens_out: this.plannerTokensOut,
      synthesis_tokens_in: this.synthesisTokensIn,
      synthesis_tokens_out: this.synthesisTokensOut,
      recovery_tokens_in: this.recoveryTokensIn,
      recovery_tokens_out: this.recoveryTokensOut,
      tool_calls: this.toolCalls,
      tool_failures: this.toolFailures,
      retries: this.retries,
      replans: this.replans,
      approvals_requested: this.approvalsRequested,
      approvals_granted: this.approvalsGranted,
      wall_ms: this.wallMs,
      estimated_cost_usd: this.estimatedCostUsd(),
    };
  }
}

export interface RunResult {
  goal: string;
  plan: Plan;
  answer: string;
  success: boolean;
  metrics: RunMetrics;
}
