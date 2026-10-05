# switchboard-trace

Non-blocking trace emitter for Switchboard agent runs. It streams OpenTelemetry-style
run/span events to the [Agent Trace Explorer](../../agent-trace-explorer) collector.

```python
from switchboard_trace import Tracer

tracer = Tracer.from_env()  # None unless SWITCHBOARD_TRACE_URL is set
with tracer.run("Did the payments deploy break anything?", model="claude-sonnet-5") as run:
    with tracer.span("tool_call", "deployment.get_logs", mcp_server="deployment",
                     tool="get_logs", input={"deployment_id": "d-482"}) as span:
        span.set_output(logs)
```

| Env var | Meaning |
|---|---|
| `SWITCHBOARD_TRACE_URL` | Collector base URL, e.g. `http://localhost:4100`. Events POST to `/v1/events`. |
| `SWITCHBOARD_TRACE_TOKEN` | Bearer token for the ingest endpoint (optional locally). |

## Guarantees

- **Never breaks the agent.** Agent exceptions pass through unchanged. Tracer and transport errors are logged on the `switchboard_trace` logger and swallowed.
- **Non-blocking.** `submit` is a `put_nowait` onto a bounded queue (10k events). A daemon thread batches events (≤500 per request, 20 ms linger) to the collector.
- **Collector down.** The worker keeps the current batch and retries with capped exponential backoff (0.25 s → 5 s). When the queue fills, new events are dropped and a warning is logged (rate-limited). A 4xx other than 408/429 drops the batch, because retrying can't fix it. `close()` makes one last attempt, then reports how many events it dropped.
- **Overhead.** Measured by `tests/test_overhead.py` against a dead collector, which is the worst case. A typical ~4 KB payload costs ≈0.3 ms p50 and ≈0.5 ms p99 per event. A 1.2 MB payload costs ≈1.3 ms p99, because sanitizing stops once the field's size budget is spent. Exception: pydantic models and dataclasses are converted to dicts in full before that budget applies.
- **Secrets.** Values are replaced with `[REDACTED]` in three cases:
  - keys that look like credentials (`api_key`, `authorization`, `password`, `token`, …),
  - well-known token formats inside strings (Anthropic/OpenAI/GitHub/Slack/AWS keys, JWTs, `Bearer …`, `scheme://user:pass@`),
  - the literal value of every credential-like environment variable.
- **Size.** Every input, output and attributes field is capped at 32 KB. Truncated fields keep their JSON shape, with markers, and the span gets `attributes.{input,output}_truncated = true`.

## Event format (schema v1)

```jsonc
{
  "schema_version": 1,
  "event_id": "uuid",            // idempotency key: the collector dedupes on it
  "kind": "run.start | run.end | span.start | span.end",
  "run_id": "uuid",
  "span_id": "uuid | null",      // null for run.* events
  "ts": "ISO-8601 UTC",
  "payload": { ... }
}
```

| kind | payload fields (all optional unless noted) |
|---|---|
| `run.start` | `task` (required), `model`, `attributes` |
| `run.end` | `status` (required), `output`, `error {type, message}`, `attributes` |
| `span.start` | `type` (required), `name` (required), `parent_span_id`, `mcp_server`, `tool`, `model`, `depends_on [span ids]`, `attempt`, `status`, `input`, `attributes` |
| `span.end` | `status` (required), `output`, `error`, `tokens_in`, `tokens_out`, `attributes` |

- `type` is one of `plan`, `step`, `llm_call`, `tool_call`, `approval`, `retry`, `eval`.
- `status` is one of `running`, `succeeded`, `failed`, `skipped`, `awaiting_approval`, `rejected`.
- The emitter sends tokens only. The collector computes cost from its pricing table.

## Development

```bash
pip install -e ".[dev]"
pytest -s          # -s prints the overhead measurements
```
