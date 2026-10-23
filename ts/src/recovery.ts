/**
 * Failure recovery: classify a failed tool call and decide what to do next.
 *
 * Strategy (bounded, Reflexion-style):
 *   - transient / rate-limit     -> retry with backoff
 *   - bad arguments / schema     -> ask the LLM to repair arguments, then retry
 *   - wrong tool / missing data  -> signal a re-plan
 *   - otherwise                  -> abort the step
 */

import type { LLMClient } from "./llm.js";
import type { JsonSchema, PlanStep, RunMetrics } from "./models.js";
import type { ToolRegistry } from "./registry.js";

const TRANSIENT_MARKERS = [
  "rate limit",
  "rate-limit",
  "429",
  "timeout",
  "timed out",
  "temporarily unavailable",
  "503",
  "connection reset",
];

export const RecoveryAction = {
  Retry: "retry",
  RepairArgs: "repair_args",
  Replan: "replan",
  Abort: "abort",
} as const;
export type RecoveryAction = (typeof RecoveryAction)[keyof typeof RecoveryAction];

export interface RecoveryDecision {
  action: RecoveryAction;
  newArguments?: Record<string, unknown>;
  backoffMs?: number;
  note: string;
}

export const REPAIR_SYSTEM = `\
A tool call failed. Given the tool's JSON schema, the arguments that were sent, and
the error message, return corrected arguments as a single JSON object. If the error
cannot be fixed by changing arguments, return {"unfixable": true}.
`;

export class RecoveryPolicy {
  constructor(
    private readonly llm: LLMClient,
    private readonly registry: ToolRegistry,
    readonly maxAttempts = 3,
    readonly baseBackoffMs = 1500,
  ) {}

  async decide(
    step: PlanStep,
    resolvedArgs: Record<string, unknown>,
    error: string,
    metrics: RunMetrics,
  ): Promise<RecoveryDecision> {
    if (step.attempts >= this.maxAttempts) {
      return { action: RecoveryAction.Replan, note: "attempt budget exhausted" };
    }

    const low = error.toLowerCase();
    if (TRANSIENT_MARKERS.some((m) => low.includes(m))) {
      return {
        action: RecoveryAction.Retry,
        backoffMs: this.baseBackoffMs * 2 ** (step.attempts - 1),
        note: "transient error",
      };
    }

    const spec = this.registry.get(step.qualifiedTool);
    if (!spec) return { action: RecoveryAction.Replan, note: "tool no longer available" };

    const repaired = await this.repairArgs(spec.inputSchema, resolvedArgs, error, metrics);
    if (repaired && !deepEqual(repaired, resolvedArgs)) {
      return { action: RecoveryAction.RepairArgs, newArguments: repaired, note: "llm-repaired args" };
    }
    return { action: RecoveryAction.Replan, note: "args not repairable" };
  }

  private async repairArgs(
    schema: JsonSchema,
    args: Record<string, unknown>,
    error: string,
    metrics: RunMetrics,
  ): Promise<Record<string, unknown> | null> {
    const user =
      `TOOL SCHEMA:\n${JSON.stringify(schema, null, 2)}\n\n` +
      `ARGUMENTS SENT:\n${JSON.stringify(args, null, 2)}\n\n` +
      `ERROR:\n${error}`;
    let data: unknown;
    try {
      const [parsed, resp] = await this.llm.completeJson({
        system: REPAIR_SYSTEM,
        user,
        maxTokens: 800,
      });
      metrics.recoveryTokensIn += resp.tokensIn;
      metrics.recoveryTokensOut += resp.tokensOut;
      data = parsed;
    } catch {
      return null;
    }
    if (!isPlainObject(data) || data.unfixable) return null;
    return data;
  }
}

export function sleep(ms: number): Promise<void> {
  return ms > 0 ? new Promise((r) => setTimeout(r, ms)) : Promise.resolve();
}

export function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(sortKeys(a)) === JSON.stringify(sortKeys(b));
}

function sortKeys(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (isPlainObject(v)) {
    return Object.fromEntries(
      Object.keys(v)
        .sort()
        .map((k) => [k, sortKeys(v[k])]),
    );
  }
  return v;
}
