"""Wire format for trace events (schema version 1).

Events are append-only. A run or span is opened by a `*.start` event and closed by
a `*.end` event; the collector materializes `runs` / `spans` rows from them.

    {
      "schema_version": 1,
      "event_id": "<uuid4>",          # idempotency key, unique per event
      "kind": "run.start" | "run.end" | "span.start" | "span.end",
      "run_id": "<uuid4>",
      "span_id": "<uuid4>" | null,    # null for run.* events
      "ts": "2026-10-05T17:02:11.123456+00:00",
      "payload": {...}                # see the builders below
    }
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = 1

EventKind = Literal["run.start", "run.end", "span.start", "span.end"]
SpanType = Literal["plan", "step", "llm_call", "tool_call", "approval", "retry", "eval"]
Status = Literal[
    "running", "succeeded", "failed", "skipped", "awaiting_approval", "rejected"
]

SPAN_TYPES: frozenset[str] = frozenset(SpanType.__args__)  # type: ignore[attr-defined]
STATUSES: frozenset[str] = frozenset(Status.__args__)  # type: ignore[attr-defined]


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(
    kind: EventKind, run_id: str, span_id: str | None, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": new_id(),
        "kind": kind,
        "run_id": run_id,
        "span_id": span_id,
        "ts": now_iso(),
        "payload": payload,
    }


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Omit unset optional fields to keep events small."""
    return {k: v for k, v in d.items() if v is not None}
