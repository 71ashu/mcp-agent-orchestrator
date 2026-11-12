// Event types streamed by src/orchestrator/web.py, and the reducer that folds them into
// what the page shows. Live runs (SSE) and replays (saved .jsonl) share this path.

export type StepStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped"
  | "awaiting_approval"
  | "rejected";

export interface StepDef {
  id: number;
  server: string;
  tool: string;
  rationale: string;
  depends_on: number[];
  arguments: Record<string, unknown>;
  write: boolean;
}

export interface Decision {
  approved: boolean;
  reason: string | null;
}

export interface StepView extends StepDef {
  status: StepStatus;
  latency_ms: number | null;
  attempts: number;
  output: unknown;
  error: string | null;
  resolved: Record<string, unknown> | null;
  approval: { arguments: Record<string, unknown>; decision: Decision | null } | null;
}

export interface PlanView {
  index: number;
  reasoning: string;
  replanReason: string | null;
  steps: StepView[];
}

export interface Metrics {
  wall_ms: number;
  steps: number;
  tool_calls: number;
  retries: number;
  replans: number;
  cost_usd: number;
}

export type RunEvent = { seq: number; t: number; type: string; [k: string]: any };

export interface RunState {
  phase: "idle" | "running" | "finished";
  mode: "live" | "replay";
  runId: string | null;
  goal: string;
  status: string | null;
  plans: PlanView[];
  answer: { markdown: string; success: boolean } | null;
  metrics: Metrics | null;
  error: string | null;
  lastSeq: number;
}

export const initialState: RunState = {
  phase: "idle",
  mode: "live",
  runId: null,
  goal: "",
  status: null,
  plans: [],
  answer: null,
  metrics: null,
  error: null,
  lastSeq: -1,
};

export type Action =
  | { type: "reset"; mode: "live" | "replay"; goal: string; runId?: string | null }
  | { type: "event"; event: RunEvent }
  | { type: "fail"; message: string };

function updateStep(
  state: RunState,
  planIndex: number,
  stepId: number,
  fn: (s: StepView) => StepView,
): RunState {
  return {
    ...state,
    plans: state.plans.map((p) =>
      p.index !== planIndex
        ? p
        : { ...p, steps: p.steps.map((s) => (s.id === stepId ? fn(s) : s)) },
    ),
  };
}

export function reducer(state: RunState, action: Action): RunState {
  if (action.type === "reset") {
    return {
      ...initialState,
      phase: "running",
      mode: action.mode,
      goal: action.goal,
      runId: action.runId ?? null,
    };
  }
  if (action.type === "fail") {
    return { ...state, phase: "finished", error: action.message };
  }

  const e = action.event;
  // EventSource may resend after a reconnect; apply each event once.
  if (e.seq <= state.lastSeq) return state;
  state = { ...state, lastSeq: e.seq };

  switch (e.type) {
    case "run_started":
      return { ...state, goal: e.goal ?? state.goal };
    case "status":
      return { ...state, status: e.message };
    case "plan":
      return {
        ...state,
        status: null,
        plans: [
          ...state.plans,
          {
            index: e.plan_index,
            reasoning: e.reasoning ?? "",
            replanReason: e.replan_reason ?? null,
            steps: (e.steps as StepDef[]).map((d) => ({
              ...d,
              status: "pending",
              latency_ms: null,
              attempts: 0,
              output: null,
              error: null,
              resolved: null,
              approval: null,
            })),
          },
        ],
      };
    case "step":
      return updateStep(state, e.plan_index, e.id, (s) => ({
        ...s,
        status: e.status,
        latency_ms: e.latency_ms,
        attempts: e.attempts,
        output: e.output,
        error: e.error,
        resolved: e.arguments ?? s.resolved,
      }));
    case "approval_request":
      return updateStep(state, e.plan_index, e.step_id, (s) => ({
        ...s,
        approval: { arguments: e.arguments ?? {}, decision: null },
      }));
    case "approval_resolved":
      return updateStep(state, e.plan_index, e.step_id, (s) => ({
        ...s,
        approval: {
          arguments: s.approval?.arguments ?? {},
          decision: { approved: e.approved, reason: e.reason ?? null },
        },
      }));
    case "answer":
      return { ...state, answer: { markdown: e.markdown, success: e.success } };
    case "metrics":
      return { ...state, metrics: e as unknown as Metrics };
    case "error":
      return { ...state, error: e.message };
    case "done":
      return { ...state, phase: "finished", status: null };
    default:
      return state;
  }
}

/** The step currently waiting on a human, if any. */
export function pendingApproval(state: RunState): { plan: number; step: StepView } | null {
  for (const p of state.plans) {
    for (const s of p.steps) {
      if (s.approval && !s.approval.decision) return { plan: p.index, step: s };
    }
  }
  return null;
}

/** One plain-language line for what the agent is doing between visible events. */
export function activity(state: RunState): string | null {
  if (state.phase !== "running") return null;
  if (state.status === "Connecting to tools") return "Connecting to tools";
  if (state.plans.length === 0) return "Planning the steps";
  if (pendingApproval(state)) return null;
  const plan = state.plans[state.plans.length - 1];
  if (plan.steps.some((s) => s.status === "running")) return null;
  const settled = (s: StepView) =>
    s.status === "succeeded" || s.status === "skipped" || s.status === "failed";
  if (plan.steps.some((s) => s.status === "rejected")) return "Replanning with your feedback";
  if (plan.steps.every(settled) && !state.answer) return "Writing the answer";
  if (plan.steps.some((s) => s.status === "failed")) return "Recovering from an error";
  return "Preparing the next step";
}
