# MCP Agent Orchestrator

An agentic runtime that **discovers and orchestrates tools across multiple MCP
servers** to execute multi-step tasks spanning databases, source control,
filesystems, and custom enterprise services.

This is the *client* side of the Model Context Protocol: the orchestrator connects
to several MCP servers at once, discovers their capabilities at runtime, plans a
dependency-aware execution graph for a natural-language goal, routes each step to
the right server, recovers from failures, gates write operations behind human
approval, and synthesizes a final answer — while tracking evaluation metrics.

```
                 goal (natural language)
                          │
                          ▼
                 ┌───────────────────┐
                 │  Planner (Claude) │  intent → dependency-aware plan
                 └─────────┬─────────┘
                           ▼
                 ┌───────────────────┐
                 │     Executor      │  topo-order · template/LLM arg resolution
                 │                   │  approval gate · recovery loop · replan
                 └─────────┬─────────┘
             ┌─────────────┼─────────────┬───────────────┐
             ▼             ▼             ▼               ▼
        MCPClientPool  (one stdio session per server, tools discovered at startup)
             │             │             │               │
         filesystem     deployment      github          postgres
         MCP server    (custom, incl.   MCP server      MCP server
                        rollback)
                           │
                           ▼
                 ┌───────────────────┐
                 │ Synthesizer (Claude)│  observations → cited answer
                 └───────────────────┘
```

## Layout

| Path | Role |
| --- | --- |
| `src/orchestrator/mcp_client.py` | Multi-server stdio client + tool dispatch |
| `src/orchestrator/registry.py` | Capability catalog built from discovered tools |
| `src/orchestrator/planner.py` | Intent analysis → validated `Plan` |
| `src/orchestrator/templating.py` | `{{steps.N.output.path}}` reference resolution |
| `src/orchestrator/executor.py` | Dependency-ordered execution, runtime arg resolution |
| `src/orchestrator/recovery.py` | Retry / arg-repair / replan / abort decisions |
| `src/orchestrator/approval.py` | Human-in-the-loop gating for WRITE tools |
| `src/orchestrator/memory.py` | Past successful tool sequences → planner hints |
| `src/orchestrator/evaluation.py` | Per-run + aggregate metrics |
| `src/orchestrator/orchestrator.py` | plan → execute → replan → synthesize loop |
| `src/orchestrator/tracing.py` | Tracer protocol + ambient tracer (no-op by default) |
| `switchboard-trace/` | Standalone trace emitter package (see its README) |
| `servers/deployment_server.py` | Custom MCP server (fake deploy control plane) |

## Setup

```bash
cd mcp-agent-orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # add your ANTHROPIC_API_KEY
```

`filesystem` uses `npx @modelcontextprotocol/server-filesystem` (needs Node). The
`deployment` server is pure Python and needs nothing extra. `github` / `postgres`
are defined in `config/servers.yaml` but disabled until you add credentials.

## Use

```bash
# What did we connect to, and what can each server do?
orchestrator servers

# Run a cross-system task (write tools prompt for approval by default)
orchestrator run "Find the latest production deployment for payments, inspect its \
logs, and tell me whether that deployment introduced a problem."

# Non-interactive: auto-approve or auto-reject write tools; machine-readable output
orchestrator run "Roll back the bad payments deployment" --approve prompt
orchestrator run "..." --approve reject --json

# Batch evaluation
orchestrator eval examples/eval_tasks.txt

# Inspect recorded task memory
orchestrator memory
```

## Tracing (Agent Trace Explorer)

Every run can stream live to the Agent Trace Explorer:

```bash
pip install -e ./switchboard-trace
export SWITCHBOARD_TRACE_URL=http://localhost:4100   # collector
export SWITCHBOARD_TRACE_TOKEN=...                   # ingest token, if the collector requires one
orchestrator run "..."
```

Tracing is off unless `SWITCHBOARD_TRACE_URL` is set. It never changes agent behavior. If the collector is down, events are buffered, retried, and eventually dropped with a warning. Each run produces this span tree:

```text
run
├─ plan #1 ─ llm:plan
├─ step 1: deployment.get_latest_deployment ─ tool_call (attempt 1)
├─ step 2: …  (depends_on → step 1)
│   ├─ llm:resolve_args
│   ├─ approval            (approver, decision, wait_ms)
│   ├─ tool_call (attempt 1) ✗
│   ├─ retry               (action: retry | repair_args | replan | abort) ─ llm:repair_args
│   └─ tool_call (attempt 2) ✓
├─ replan #2 …             (after a rejection or unrecoverable failure)
├─ llm:synthesize
└─ eval                    (only under `orchestrator eval`; run tagged with eval_batch_id)
```

Approvers return `ApprovalDecision(approved, approver, reason)`. The CLI records `user:$USER`, and the `--approve auto|reject` policies record `policy:auto-approve|auto-reject`. A plain `bool` still works.

## What each concept maps to

- **Dynamic tool discovery** — `MCPClientPool.connect_all` → `ServerConnection.discover`;
  the planner only ever sees the live `ToolRegistry.catalog_text()`.
- **Intent → plan** — `Planner.create_plan` returns a validated DAG of `PlanStep`s.
- **Dependency-aware execution** — `Executor._in_dependency_order` (Kahn) + template
  resolution; steps whose args depend on prior *outputs* use `{{steps.N.output...}}`
  or `resolve_arguments_at_runtime` (LLM fills args from live observations).
- **Failure recovery (Reflexion-ish)** — `RecoveryPolicy.decide`: transient → backoff
  retry; bad args → LLM repair; wrong tool / missing data → `ReplanRequested` →
  `Orchestrator` replans with the failure as context (bounded by `max_replans`).
- **Human-in-the-loop** — `WriteToolPolicy` flags WRITE tools (from `write_tools:` in
  `config/servers.yaml`); the CLI shows resolved args and asks y/n.
- **Memory** — `TaskMemory` stores successful `<server>.<tool>` sequences and feeds
  the closest matches back to the planner as a hint.
- **Evaluation** — every run records steps, tool calls, failures, retries, replans,
  latency, tokens, and estimated cost (`orchestrator eval` aggregates).

## Tests

```bash
pytest            # offline — no API key or network needed
```

Covers template resolution, dependency ordering, plan validation, approval-driven
replan, and transient-error retry with the fakes in `tests/conftest.py`.
`tests/test_tracing.py` checks the emitted span tree. It runs the real `LLM` class
against a scripted Anthropic client and the real `deployment` server.

The emitter has its own suite: `cd switchboard-trace && pytest -s` (`-s` prints overhead numbers).
