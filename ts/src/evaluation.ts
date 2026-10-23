/** Aggregate run metrics into an evaluation report. */

import { round } from "./memory.js";
import type { RunResult } from "./models.js";

export interface EvalRow {
  goal: string;
  success: boolean;
  steps: number;
  toolCalls: number;
  toolFailures: number;
  retries: number;
  replans: number;
  approvals: string;
  wallMs: number;
  tokensIn: number;
  tokensOut: number;
  costUsd: number;
}

export function evalRow(r: RunResult): EvalRow {
  const m = r.metrics;
  return {
    goal: r.goal,
    success: r.success,
    steps: r.plan.steps.length,
    toolCalls: m.toolCalls,
    toolFailures: m.toolFailures,
    retries: m.retries,
    replans: m.replans,
    approvals: `${m.approvalsGranted}/${m.approvalsRequested}`,
    wallMs: round(m.wallMs, 1),
    tokensIn: m.totalTokensIn,
    tokensOut: m.totalTokensOut,
    costUsd: round(m.estimatedCostUsd(), 5),
  };
}

export interface EvalSummary {
  runs: number;
  success_rate: number;
  avg_steps: number;
  avg_tool_calls: number;
  total_tool_failures: number;
  total_retries: number;
  total_replans: number;
  avg_wall_ms: number;
  total_cost_usd: number;
}

export function summarize(results: RunResult[]): EvalSummary | Record<string, never> {
  const n = results.length;
  if (!n) return {};
  const sum = (f: (r: RunResult) => number) => results.reduce((acc, r) => acc + f(r), 0);
  return {
    runs: n,
    success_rate: round(sum((r) => Number(r.success)) / n, 3),
    avg_steps: round(sum((r) => r.plan.steps.length) / n, 2),
    avg_tool_calls: round(sum((r) => r.metrics.toolCalls) / n, 2),
    total_tool_failures: sum((r) => r.metrics.toolFailures),
    total_retries: sum((r) => r.metrics.retries),
    total_replans: sum((r) => r.metrics.replans),
    avg_wall_ms: round(sum((r) => r.metrics.wallMs) / n, 1),
    total_cost_usd: round(sum((r) => r.metrics.estimatedCostUsd()), 4),
  };
}
