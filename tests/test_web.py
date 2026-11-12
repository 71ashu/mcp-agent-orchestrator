"""Web demo: the browser approval bridge and the run recording. No servers, no API."""

from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("fastapi")

from orchestrator import web
from orchestrator.models import PlanStep, StepStatus


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path)
    return web.RunSession("roll back payments", "config/servers.yaml")


def _step() -> PlanStep:
    return PlanStep(id=5, server="deployment", tool="rollback_deployment", rationale="undo it")


def _decide_when_asked(session, approved, reason):
    def run():
        while session.awaiting_step is None:
            threading.Event().wait(0.01)
        session.decide(approved, reason)

    t = threading.Thread(target=run)
    t.start()
    return t


def test_rejection_keeps_the_reason(session):
    t = _decide_when_asked(session, False, "  summarize first  ")
    decision = session.approver(_step(), {"deployment_id": "d-1"})
    t.join()
    assert not decision.approved
    assert decision.reason == "summarize first"
    assert decision.approver == "user:web"
    resolved = [e for e in session.events if e["type"] == "approval_resolved"][-1]
    assert resolved["reason"] == "summarize first"


def test_replan_divider_quotes_the_rejection(session):
    t = _decide_when_asked(session, False, "not yet")
    step = _step()
    session.on_plan(type("P", (), {"reasoning": "", "steps": [step]})())
    session.approver(step, {})
    t.join()
    step.status = StepStatus.REJECTED
    step.error = "rejected by the approver with this feedback: not yet"
    session.on_step(step)
    session.on_plan(type("P", (), {"reasoning": "", "steps": []})())
    plan = [e for e in session.events if e["type"] == "plan"][-1]
    assert plan["plan_index"] == 2
    assert "“not yet”" in plan["replan_reason"]


def test_events_are_recorded_to_jsonl(session):
    t = _decide_when_asked(session, True, None)
    session.approver(_step(), {"deployment_id": "d-1"})
    t.join()
    session._record.close()
    lines = [json.loads(ln) for ln in session.record_path.read_text().splitlines()]
    assert [e["type"] for e in lines] == ["approval_request", "approval_resolved"]
    assert [e["seq"] for e in lines] == [0, 1]
    assert lines[0]["arguments"] == {"deployment_id": "d-1"}


def test_decide_without_pending_approval_is_rejected(session):
    with pytest.raises(web.HTTPException):
        session.decide(True, None)
