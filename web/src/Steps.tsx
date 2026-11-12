import { useEffect, useRef, useState } from "react";
import {
  approvalTitle,
  clockTime,
  formatLatency,
  formatValue,
  humanizeKey,
  isLogLine,
  keyFields,
} from "./format";
import type { PlanView, StepView } from "./state";

interface PlanProps {
  plan: PlanView;
  superseded: boolean;
  replay: boolean;
  onDecide: (approved: boolean, reason: string) => Promise<void>;
}

export function Plan({ plan, superseded, replay, onDecide }: PlanProps) {
  const dividerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (plan.replanReason) dividerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [plan.replanReason]);

  return (
    <section className={`plan${superseded ? " superseded" : ""}`}>
      {plan.index > 1 && (
        <div className="replan" ref={dividerRef}>
          <span className="replan-label">
            Replanning: {plan.replanReason ?? "the previous plan could not finish"}
          </span>
        </div>
      )}
      <header className="plan-head">
        <h2>{plan.index > 1 ? "New plan" : "Plan"}</h2>
        {plan.reasoning && <p className="plan-reasoning">{plan.reasoning}</p>}
      </header>
      <ol className="steps">
        {plan.steps.map((s) => (
          <Step key={s.id} step={s} replay={replay} onDecide={onDecide} />
        ))}
      </ol>
    </section>
  );
}

const STATUS_TEXT: Record<string, string> = {
  pending: "Waiting",
  running: "Working",
  succeeded: "Done",
  failed: "Failed",
  skipped: "Skipped",
  awaiting_approval: "Needs approval",
  rejected: "Rejected",
};

function Step({
  step,
  replay,
  onDecide,
}: {
  step: StepView;
  replay: boolean;
  onDecide: PlanProps["onDecide"];
}) {
  const status = step.status;
  let label = STATUS_TEXT[status] ?? status;
  if (status === "succeeded" && step.latency_ms !== null) label += ` · ${formatLatency(step.latency_ms)}`;
  if (status === "running" && step.attempts > 1) label = `Retrying (attempt ${step.attempts})`;

  return (
    <li className={`step s-${status}`}>
      <div className="marker" aria-hidden>
        <Marker status={status} id={step.id} />
      </div>
      <div className="step-body">
        <div className="step-top">
          <div className="step-title">
            <h3>{step.rationale || `${step.server}.${step.tool}`}</h3>
            <div className="step-meta">
              <code className="tool">
                {step.server}.{step.tool}
              </code>
              {step.depends_on.map((d) => (
                <span key={d} className="chip">
                  after step {d}
                </span>
              ))}
              {step.write && <span className="chip chip-write">changes something</span>}
            </div>
          </div>
          <span className={`status status-${status}`}>{label}</span>
        </div>

        {step.approval && (
          <ApprovalCard step={step} replay={replay} onDecide={onDecide} />
        )}

        {status === "succeeded" && <Evidence output={step.output} args={step.resolved} />}
        {(status === "failed" || (status === "running" && step.error)) && step.error && (
          <p className="step-error">{step.error}</p>
        )}
      </div>
    </li>
  );
}

function Marker({ status, id }: { status: string; id: number }) {
  if (status === "succeeded")
    return (
      <svg viewBox="0 0 20 20" className="mk mk-done">
        <circle cx="10" cy="10" r="10" />
        <path d="M6 10.5l2.6 2.6L14 7.6" />
      </svg>
    );
  if (status === "failed" || status === "rejected")
    return (
      <svg viewBox="0 0 20 20" className="mk mk-bad">
        <circle cx="10" cy="10" r="10" />
        <path d="M7 7l6 6M13 7l-6 6" />
      </svg>
    );
  return <span className={`mk mk-num mk-${status}`}>{id}</span>;
}

// ---------------------------------------------------------------- evidence

function Evidence({ output, args }: { output: unknown; args: Record<string, unknown> | null }) {
  return (
    <div className="evidence">
      <EvidenceBody output={output} />
      <details className="raw">
        <summary>Details</summary>
        {args && Object.keys(args).length > 0 && (
          <>
            <div className="raw-label">Called with</div>
            <pre>{JSON.stringify(args, null, 2)}</pre>
          </>
        )}
        <div className="raw-label">Returned</div>
        <pre>{typeof output === "string" ? output : JSON.stringify(output, null, 2)}</pre>
      </details>
    </div>
  );
}

function EvidenceBody({ output }: { output: unknown }) {
  if (output === null || output === undefined || (Array.isArray(output) && output.length === 0)) {
    return <p className="ev-empty">Nothing found.</p>;
  }
  if (typeof output === "string") {
    const text = output.length > 700 ? output.slice(0, 700) + "…" : output;
    return <p className="ev-text">{text}</p>;
  }
  if (typeof output !== "object") return <p className="ev-text">{String(output)}</p>;

  if (Array.isArray(output)) {
    if (output.every(isLogLine)) {
      return (
        <ul className="logs">
          {output.slice(0, 12).map((line, i) => (
            <li key={i}>
              {line.ts && <span className="log-ts">{clockTime(line.ts)}</span>}
              {line.level && <span className={`lvl lvl-${line.level}`}>{line.level}</span>}
              <span className="log-msg">{line.msg ?? line.message}</span>
            </li>
          ))}
        </ul>
      );
    }
    if (output.every((v) => v === null || typeof v !== "object")) {
      return (
        <ul className="ev-list">
          {output.slice(0, 12).map((v, i) => (
            <li key={i}>{String(v)}</li>
          ))}
        </ul>
      );
    }
    return (
      <div className="ev-rows">
        {output.slice(0, 5).map((item, i) => (
          <Facts key={i} obj={item as Record<string, unknown>} max={4} compact />
        ))}
        {output.length > 5 && <p className="ev-more">and {output.length - 5} more</p>}
      </div>
    );
  }
  return <Facts obj={output as Record<string, unknown>} max={6} />;
}

function Facts({ obj, max, compact }: { obj: Record<string, unknown>; max: number; compact?: boolean }) {
  if (typeof obj !== "object" || obj === null) return <p className="ev-text">{String(obj)}</p>;
  const fields = keyFields(obj, max);
  return (
    <dl className={`facts${compact ? " compact" : ""}`}>
      {fields.map(([k, v]) => (
        <div key={k} className={`fact${typeof v === "string" && v.length > 48 ? " wide" : ""}`}>
          <dt>{humanizeKey(k)}</dt>
          <dd>{formatValue(k, v)}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------- approval

function ApprovalCard({
  step,
  replay,
  onDecide,
}: {
  step: StepView;
  replay: boolean;
  onDecide: PlanProps["onDecide"];
}) {
  const approval = step.approval!;
  const decision = approval.decision;
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!decision) cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [decision]);

  const decide = async (approved: boolean) => {
    setSending(approved ? "approve" : "reject");
    try {
      await onDecide(approved, reason);
    } catch {
      setSending(null);
    }
  };

  if (decision) {
    return (
      <div className={`decision ${decision.approved ? "ok" : "no"}`}>
        {decision.approved ? "Approved" : "Rejected"}
        {decision.reason && <span className="decision-reason"> — “{decision.reason}”</span>}
      </div>
    );
  }

  const args = approval.arguments;
  return (
    <div className="approval" ref={cardRef} role="alertdialog" aria-label="Approval required">
      <div className="approval-kicker">Your approval is needed</div>
      <h4 className="approval-title">{approvalTitle(step.tool, args)}</h4>
      <p className="approval-why">
        Nothing has changed yet. This step will run only if you approve it.
      </p>
      <dl className="approval-args">
        {Object.entries(args).map(([k, v]) => (
          <div key={k} className="arg">
            <dt>{humanizeKey(k)}</dt>
            <dd>{typeof v === "string" ? v : JSON.stringify(v, null, 2)}</dd>
          </div>
        ))}
      </dl>
      {replay ? (
        <p className="replay-note">Replay: the decision made during the recording will be applied.</p>
      ) : (
        <>
          <label className="reason">
            <span>Reason, if you reject (optional): the agent will replan with it</span>
            <textarea
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Don't roll back yet, just summarize the evidence for on-call"
              disabled={sending !== null}
            />
          </label>
          <div className="approval-actions">
            <button className="btn btn-primary" onClick={() => decide(true)} disabled={sending !== null}>
              {sending === "approve" ? "Approving…" : "Approve"}
            </button>
            <button className="btn btn-secondary" onClick={() => decide(false)} disabled={sending !== null}>
              {sending === "reject" ? "Rejecting…" : "Reject"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
