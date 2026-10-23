/**
 * Lightweight task memory: successful tool sequences and past outcomes.
 *
 * Stored as a JSON file so the orchestrator can offer the planner a hint from
 * similar past tasks and so evaluation can aggregate across runs.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { RunResult } from "./models.js";

export interface MemoryEntry {
  ts: number;
  goal: string;
  success: boolean;
  tool_sequence: string[];
  steps: number;
  tool_calls: number;
  retries: number;
  replans: number;
  wall_ms: number;
  cost_usd: number;
}

export class TaskMemory {
  private entriesList: MemoryEntry[] = [];

  constructor(readonly path = "data/memory.json") {
    mkdirSync(dirname(path), { recursive: true });
    if (existsSync(path)) {
      try {
        const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
        this.entriesList = Array.isArray(parsed) ? (parsed as MemoryEntry[]) : [];
      } catch {
        this.entriesList = [];
      }
    }
  }

  /** Naive keyword-overlap ranking against past successful runs. */
  recall(goal: string, limit = 3): MemoryEntry[] {
    const goalWords = tokens(goal);
    const scored: [number, MemoryEntry][] = [];
    for (const e of this.entriesList) {
      if (!e.success) continue;
      const past = tokens(e.goal);
      const overlap = [...goalWords].filter((w) => past.has(w)).length;
      if (overlap) scored.push([overlap / (goalWords.size || 1), e]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    return scored.slice(0, limit).map(([, e]) => e);
  }

  hintFor(goal: string): string | null {
    const matches = this.recall(goal);
    if (!matches.length) return null;
    const lines = ["Similar past tasks that succeeded:"];
    for (const m of matches) {
      lines.push(`- goal: ${m.goal}\n  tools: ${m.tool_sequence.join(" -> ")}`);
    }
    return lines.join("\n");
  }

  record(result: RunResult): void {
    const m = result.metrics;
    this.entriesList.push({
      ts: Date.now() / 1000,
      goal: result.goal,
      success: result.success,
      tool_sequence: result.plan.steps.map((s) => s.qualifiedTool),
      steps: result.plan.steps.length,
      tool_calls: m.toolCalls,
      retries: m.retries,
      replans: m.replans,
      wall_ms: round(m.wallMs, 1),
      cost_usd: round(m.estimatedCostUsd(), 5),
    });
    writeFileSync(this.path, JSON.stringify(this.entriesList, null, 2));
  }

  get entries(): MemoryEntry[] {
    return [...this.entriesList];
  }
}

function tokens(text: string): Set<string> {
  return new Set(
    text
      .split(/\s+/)
      .filter((w) => w.length > 3)
      .map((w) => w.toLowerCase()),
  );
}

export function round(n: number, digits: number): number {
  const f = 10 ** digits;
  return Math.round(n * f) / f;
}
