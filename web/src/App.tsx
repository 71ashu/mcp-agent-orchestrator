import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatDuration } from "./format";
import { Plan } from "./Steps";
import { activity, initialState, reducer, type RunEvent } from "./state";

const PRESETS = [
  {
    label: "Investigate the payments deploy",
    goal: "Find the latest production deployment for payments, inspect its logs, and tell me whether that deployment introduced a problem.",
  },
  {
    label: "Roll back the bad deploy",
    goal: "Roll back the payments deployment that caused the current incident, citing the evidence from the logs.",
  },
];

interface Recording {
  source: "last" | "recorded";
  name: string;
  goal: string;
  duration_s: number;
}

// A human took this long to decide during the recording; don't make the audience wait.
const MAX_APPROVAL_PAUSE_S = 4;

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [goal, setGoal] = useState(PRESETS[0].goal);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [starting, setStarting] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const replayRef = useRef<{ cancelled: boolean } | null>(null);
  const answerRef = useRef<HTMLDivElement>(null);

  const loadRecordings = useCallback(() => {
    fetch("/api/recordings")
      .then((r) => (r.ok ? r.json() : []))
      .then(setRecordings)
      .catch(() => setRecordings([]));
  }, []);
  useEffect(loadRecordings, [loadRecordings]);

  useEffect(() => {
    if (state.phase === "finished" && state.mode === "live") loadRecordings();
  }, [state.phase, state.mode, loadRecordings]);

  useEffect(() => {
    if (state.answer) answerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [state.answer]);

  const stopAll = () => {
    sourceRef.current?.close();
    sourceRef.current = null;
    if (replayRef.current) replayRef.current.cancelled = true;
    replayRef.current = null;
  };
  useEffect(() => stopAll, []);

  const startRun = async () => {
    const g = goal.trim();
    if (!g) return;
    stopAll();
    setStarting(true);
    try {
      const res = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: g }),
      });
      if (!res.ok) throw new Error(`Server responded ${res.status}`);
      const { run_id } = await res.json();
      dispatch({ type: "reset", mode: "live", goal: g, runId: run_id });
      const es = new EventSource(`/api/runs/${run_id}/events`);
      sourceRef.current = es;
      es.onmessage = (msg) => {
        const event = JSON.parse(msg.data) as RunEvent;
        dispatch({ type: "event", event });
        if (event.type === "done") {
          es.close();
          sourceRef.current = null;
        }
      };
      // EventSource reconnects by itself (resuming after the last event id); only a
      // closed stream is fatal.
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          dispatch({ type: "fail", message: "Lost the connection to the server." });
        }
      };
    } catch (err) {
      dispatch({ type: "reset", mode: "live", goal: g });
      dispatch({ type: "fail", message: `Could not start the run. ${(err as Error).message}` });
    } finally {
      setStarting(false);
    }
  };

  const startReplay = async (rec: Recording) => {
    stopAll();
    const token = { cancelled: false };
    replayRef.current = token;
    setGoal(rec.goal);
    dispatch({ type: "reset", mode: "replay", goal: rec.goal });
    try {
      const res = await fetch(`/api/recordings/${rec.source}/${encodeURIComponent(rec.name)}`);
      if (!res.ok) throw new Error(`Server responded ${res.status}`);
      const events = (await res.json()) as RunEvent[];
      let prevT = 0;
      for (const event of events) {
        let wait = Math.max(0, event.t - prevT);
        if (event.type === "approval_resolved") wait = Math.min(wait, MAX_APPROVAL_PAUSE_S);
        prevT = event.t;
        if (wait > 0) await new Promise((r) => setTimeout(r, wait * 1000));
        if (token.cancelled) return;
        dispatch({ type: "event", event });
      }
      dispatch({ type: "event", event: { seq: Number.MAX_SAFE_INTEGER, t: prevT, type: "done" } });
    } catch (err) {
      if (!token.cancelled) dispatch({ type: "fail", message: `Could not replay. ${(err as Error).message}` });
    }
  };

  const stopReplay = () => {
    stopAll();
    dispatch({ type: "fail", message: "" });
  };

  const decide = async (approved: boolean, reason: string) => {
    const res = await fetch(`/api/runs/${state.runId}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, reason: reason.trim() || null }),
    });
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
  };

  const running = state.phase === "running";
  const replaying = running && state.mode === "replay";
  const now = activity(state);
  const last = recordings.find((r) => r.source === "last");
  const recorded = recordings.filter((r) => r.source === "recorded");

  return (
    <div className="page">
      <header className="masthead">
        <div className="brand">
          <span className="dot" aria-hidden />
          Orchestrator
        </div>
        <p className="tagline">
          An AI agent that plans the work, gathers evidence from your tools, and asks before it
          changes anything.
        </p>
      </header>

      {state.mode === "replay" && state.phase !== "idle" && (
        <div className="replay-banner">
          <span>Replaying a recorded run. No live calls are being made.</span>
          {replaying && (
            <button className="link" onClick={stopReplay}>
              Stop
            </button>
          )}
        </div>
      )}

      <section className="goal">
        <label htmlFor="goal" className="goal-label">
          What should the agent do?
        </label>
        <textarea
          id="goal"
          rows={3}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          disabled={running}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !running) startRun();
          }}
        />
        <div className="goal-row">
          <div className="presets">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                className={`preset${goal === p.goal ? " active" : ""}`}
                onClick={() => setGoal(p.goal)}
                disabled={running}
              >
                {p.label}
              </button>
            ))}
          </div>
          <button className="btn btn-primary" onClick={startRun} disabled={running || starting || !goal.trim()}>
            {running && !replaying ? "Running…" : state.phase === "finished" ? "Run again" : "Run agent"}
          </button>
        </div>
        {(last || recorded.length > 0) && (
          <div className="replays">
            <span>Replay a saved run:</span>
            {last && (
              <button className="link" onClick={() => startReplay(last)} disabled={running}>
                Last run
              </button>
            )}
            {recorded.map((r) => (
              <button
                key={r.name}
                className="link"
                onClick={() => startReplay(r)}
                disabled={running}
                title={r.goal}
              >
                {PRESETS.find((p) => p.goal === r.goal)?.label ?? r.name.replace(/\.jsonl$/, "")}
              </button>
            ))}
          </div>
        )}
      </section>

      <main className="run">
        {state.plans.map((p, i) => (
          <Plan
            key={p.index}
            plan={p}
            superseded={i < state.plans.length - 1}
            replay={state.mode === "replay"}
            onDecide={decide}
          />
        ))}

        {now && (
          <div className="activity" aria-live="polite">
            <span className="pulse" aria-hidden />
            {now}…
          </div>
        )}

        {state.error && <div className="error">{state.error}</div>}

        {state.answer && (
          <section className={`answer${state.answer.success ? "" : " answer-failed"}`} ref={answerRef}>
            <h2>Answer</h2>
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{state.answer.markdown}</ReactMarkdown>
            </div>
          </section>
        )}

        {state.metrics && (
          <p className="metrics">
            {formatDuration(state.metrics.wall_ms)} · {state.metrics.steps} steps ·{" "}
            {state.metrics.tool_calls} tool calls · {state.metrics.retries} retries ·{" "}
            {state.metrics.replans} {state.metrics.replans === 1 ? "replan" : "replans"} · $
            {state.metrics.cost_usd.toFixed(2)}
          </p>
        )}
      </main>
    </div>
  );
}
