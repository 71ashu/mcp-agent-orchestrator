"""Web demo: run the Orchestrator from a browser and stream its progress as SSE.

Each run gets its own worker thread with its own event loop (``asyncio.run``), its own
MCP server connections, and an approver that blocks on a ``threading.Event`` until the
browser POSTs a decision. Every event is also appended to ``data/runs/<ts>.jsonl`` so a
run can be replayed later without calling the API.

Event types (one JSON object per SSE message, all with ``seq`` and ``t`` seconds since
the run started): run_started, status, plan, step, approval_request, approval_resolved,
answer, metrics, error, done.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .approval import ApprovalDecision
from .config import load_config
from .mcp_client import MCPClientPool
from .models import Plan, PlanStep, StepStatus, ToolKind
from .orchestrator import Orchestrator

ROOT = Path.cwd()
RUNS_DIR = ROOT / "data" / "runs"  # every run, gitignored
RECORDINGS_DIR = ROOT / "data" / "recordings"  # curated runs checked into the repo
DIST_DIR = ROOT / "web" / "dist"

APPROVAL_TIMEOUT_S = 30 * 60
_MAX_OUTPUT_CHARS = 40_000


def _jsonable(value: Any) -> Any:
    """Snapshot a value as plain JSON (the executor keeps mutating step objects)."""
    text = json.dumps(value, default=str)
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return json.loads(text)


class RunSession:
    """One orchestrator run: its event log, recording file, and pending approval."""

    def __init__(self, goal: str, config_path: str):
        self.id = uuid.uuid4().hex[:12]
        self.goal = goal
        self.config_path = config_path
        self.started = time.time()
        self.events: list[dict[str, Any]] = []
        self.done = False
        self._lock = threading.Lock()
        self._approval_event = threading.Event()
        self._decision: ApprovalDecision | None = None
        self.awaiting_step: int | None = None
        self._plan_index = 0
        self._last_problem: dict[str, Any] | None = None
        self._kinds: dict[str, ToolKind] = {}

        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.record_path = RUNS_DIR / f"{stamp}-{self.id[:4]}.jsonl"
        self._record = self.record_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------ events

    def emit(self, type_: str, **data: Any) -> None:
        with self._lock:
            event = {
                "seq": len(self.events),
                "t": round(time.time() - self.started, 3),
                "type": type_,
                **data,
            }
            self.events.append(event)
            try:
                self._record.write(json.dumps(event, default=str) + "\n")
                self._record.flush()
            except ValueError:  # file already closed
                pass

    def on_plan(self, plan: Plan) -> None:
        self._plan_index += 1
        replan_reason = None
        if self._plan_index > 1:
            replan_reason = _describe_problem(self._last_problem)
        self.emit(
            "plan",
            plan_index=self._plan_index,
            reasoning=plan.reasoning,
            replan_reason=replan_reason,
            steps=[_step_definition(s, self._kinds.get(s.qualified_tool)) for s in plan.steps],
        )
        self._last_problem = None

    def on_step(self, step: PlanStep) -> None:
        payload = {
            "plan_index": self._plan_index,
            "id": step.id,
            "status": step.status.value,
            "latency_ms": round(step.latency_ms, 1) if step.latency_ms is not None else None,
            "attempts": step.attempts,
            "arguments": _jsonable(step.resolved_arguments),
            "output": _jsonable(step.output) if step.status == StepStatus.SUCCEEDED else None,
            "error": step.error,
        }
        if step.status in (StepStatus.FAILED, StepStatus.REJECTED):
            self._last_problem = {
                "status": step.status.value,
                "step": step.id,
                "tool": step.qualified_tool,
                "error": step.error,
                "reason": self._decision.reason if self._decision is not None else None,
            }
        self.emit("step", **payload)

    def approver(self, step: PlanStep, args: dict[str, Any]) -> ApprovalDecision:
        """Called synchronously by the executor; blocks until the browser decides."""
        self._approval_event.clear()
        self._decision = None
        self.awaiting_step = step.id
        self.emit(
            "approval_request",
            plan_index=self._plan_index,
            step_id=step.id,
            server=step.server,
            tool=step.tool,
            rationale=step.rationale,
            arguments=_jsonable(args),
        )
        if not self._approval_event.wait(APPROVAL_TIMEOUT_S):
            self._decision = ApprovalDecision(
                False, approver="policy:timeout", reason="No decision within 30 minutes"
            )
        # Not `or`: a rejection is falsy (ApprovalDecision.__bool__), which would drop its reason.
        decision = self._decision
        if decision is None:
            decision = ApprovalDecision(False, approver="user:web")
        self.awaiting_step = None
        self.emit(
            "approval_resolved",
            plan_index=self._plan_index,
            step_id=step.id,
            approved=decision.approved,
            approver=decision.approver,
            reason=decision.reason,
        )
        return decision

    def decide(self, approved: bool, reason: str | None) -> None:
        if self.awaiting_step is None:
            raise HTTPException(409, "this run is not waiting for an approval")
        reason = (reason or "").strip() or None
        self._decision = ApprovalDecision(approved, approver="user:web", reason=reason)
        self._approval_event.set()

    # ------------------------------------------------------------------ worker

    def start(self) -> None:
        threading.Thread(target=self._thread_main, name=f"run-{self.id}", daemon=True).start()

    def _thread_main(self) -> None:
        self.emit("run_started", run_id=self.id, goal=self.goal)
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.emit("error", message=f"{type(exc).__name__}: {exc}")
        finally:
            self.emit("done")
            self.done = True
            self._record.close()

    async def _run(self) -> None:
        config = load_config(self.config_path)
        self.emit("status", message="Connecting to tools")
        pool = MCPClientPool(config)
        try:
            await pool.connect_all()
            self._kinds = {t.qualified_name: t.kind for t in pool.all_tools()}
            self.emit(
                "status",
                message="Planning",
                servers=sorted(pool.connections),
                tool_count=len(self._kinds),
            )
            orch = Orchestrator(
                config,
                pool,
                approver=self.approver,
                on_plan=self.on_plan,
                on_step=self.on_step,
            )
            result = await orch.run(self.goal)
        finally:
            await pool.aclose()

        m = result.metrics
        self.emit("answer", markdown=result.answer, success=result.success)
        self.emit(
            "metrics",
            wall_ms=round(m.wall_ms),
            steps=len(result.plan.steps),
            tool_calls=m.tool_calls,
            tool_failures=m.tool_failures,
            retries=m.retries,
            replans=m.replans,
            approvals_requested=m.approvals_requested,
            approvals_granted=m.approvals_granted,
            tokens_in=m.total_tokens_in,
            tokens_out=m.total_tokens_out,
            cost_usd=round(m.estimated_cost_usd(), 4),
        )


def _step_definition(step: PlanStep, kind: ToolKind | None) -> dict[str, Any]:
    return {
        "id": step.id,
        "server": step.server,
        "tool": step.tool,
        "rationale": step.rationale,
        "depends_on": step.depends_on,
        "arguments": _jsonable(step.arguments),
        "write": kind == ToolKind.WRITE,
    }


def _describe_problem(problem: dict[str, Any] | None) -> str:
    if not problem:
        return "the previous plan could not finish"
    if problem["status"] == "rejected":
        if problem.get("reason"):
            return f"you rejected step {problem['step']}: “{problem['reason']}”"
        return f"you rejected step {problem['step']}"
    return f"step {problem['step']} failed ({(problem.get('error') or '')[:160]})"


# ---------------------------------------------------------------------- app


class StartRun(BaseModel):
    goal: str


class Decision(BaseModel):
    approved: bool
    reason: str | None = None


def create_app(config_path: str = "config/servers.yaml") -> FastAPI:
    app = FastAPI(title="MCP Agent Orchestrator demo")
    runs: dict[str, RunSession] = {}

    @app.post("/api/runs")
    def start_run(body: StartRun) -> dict[str, str]:
        goal = body.goal.strip()
        if not goal:
            raise HTTPException(400, "goal is empty")
        session = RunSession(goal, config_path)
        runs[session.id] = session
        session.start()
        return {"run_id": session.id}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        session = runs.get(run_id)
        if session is None:
            raise HTTPException(404, "unknown run")
        # EventSource resends the last id it saw when it reconnects; resume after it.
        last = request.headers.get("last-event-id")
        start = int(last) + 1 if last and last.isdigit() else 0
        return StreamingResponse(
            _sse(session, start, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/approval")
    def approve(run_id: str, body: Decision) -> dict[str, bool]:
        session = runs.get(run_id)
        if session is None:
            raise HTTPException(404, "unknown run")
        session.decide(body.approved, body.reason)
        return {"ok": True}

    @app.get("/api/recordings")
    def list_recordings() -> list[dict[str, Any]]:
        """Saved runs, newest first: the last live run, then the curated recordings."""
        items: list[dict[str, Any]] = []
        latest = _latest_complete_run()
        if latest:
            items.append(_recording_info(latest, "last"))
        for path in sorted(RECORDINGS_DIR.glob("*.jsonl")):
            items.append(_recording_info(path, "recorded"))
        return items

    @app.get("/api/recordings/{source}/{name}")
    def get_recording(source: str, name: str) -> list[dict[str, Any]]:
        base = {"last": RUNS_DIR, "recorded": RECORDINGS_DIR}.get(source)
        path = (base / name) if base else None
        if path is None or path.name != name or not path.is_file():
            raise HTTPException(404, "unknown recording")
        return _read_events(path)

    if DIST_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

        @app.get("/{_path:path}")
        def index(_path: str) -> FileResponse:
            return FileResponse(DIST_DIR / "index.html")

    return app


async def _sse(session: RunSession, start: int, request: Request):
    idx = start
    last_send = time.monotonic()
    yield "retry: 1500\n\n"
    while True:
        if await request.is_disconnected():
            return
        if idx < len(session.events):
            event = session.events[idx]
            yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
            idx += 1
            last_send = time.monotonic()
            continue
        if session.done:
            return
        if time.monotonic() - last_send > 15:
            yield ": keepalive\n\n"
            last_send = time.monotonic()
        await asyncio.sleep(0.05)


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def _latest_complete_run() -> Path | None:
    """Newest run that finished with an answer, so a crashed run is never replayed."""
    if not RUNS_DIR.is_dir():
        return None
    for path in sorted(RUNS_DIR.glob("*.jsonl"), reverse=True):
        if any(e.get("type") == "answer" for e in _read_events(path)):
            return path
    return None


def _recording_info(path: Path, source: str) -> dict[str, Any]:
    events = _read_events(path)
    goal = next((e.get("goal") for e in events if e.get("type") == "run_started"), "")
    duration = events[-1]["t"] if events else 0
    return {"source": source, "name": path.name, "goal": goal, "duration_s": duration}
