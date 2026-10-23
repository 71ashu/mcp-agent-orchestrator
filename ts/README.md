# Switchboard (TypeScript)

A TypeScript port of the MCP agent orchestrator in the repo root. Same architecture,
same behavior, same custom deployment server: it connects to several MCP servers at
once, discovers their tools at runtime, plans a dependency-aware execution graph for
a natural-language goal, gates write operations behind human approval, recovers
from failures, replans when needed, and synthesizes a cited answer while tracking
evaluation metrics.

Built on the official `@modelcontextprotocol/sdk` (client and server) and
`@anthropic-ai/sdk`, with `zod` validating every plan the model produces before
anything executes.

## Layout

| Path | Role |
| --- | --- |
| `src/mcp-client.ts` | Multi-server stdio client, paginated tool discovery, dispatch |
| `src/registry.ts` | Capability catalog built from discovered tools |
| `src/planner.ts` | Intent analysis to a zod-validated `Plan` (unknown tools, dangling or duplicate ids rejected) |
| `src/templating.ts` | `{{steps.N.output.path}}` reference resolution |
| `src/executor.ts` | Dependency-ordered execution (Kahn), runtime arg resolution, approval gate, recovery loop |
| `src/recovery.ts` | Retry with backoff / LLM arg repair / replan decisions |
| `src/approval.ts` | Human-in-the-loop gating for WRITE tools |
| `src/memory.ts` | Past successful tool sequences as planner hints |
| `src/evaluation.ts` | Per-run and aggregate metrics |
| `src/orchestrator.ts` | plan, execute, replan, synthesize loop |
| `src/cli.ts` | `servers`, `run`, `eval`, `memory` commands |
| `servers/deployment-server.ts` | Custom MCP server (fake deploy control plane with rollback) |

## Setup

```bash
cd ts
npm install
cp ../.env.example .env        # add ANTHROPIC_API_KEY
mkdir -p sandbox               # root for the filesystem MCP server
```

Requires Node 20+. The `deployment` server runs directly from TypeScript via `tsx`,
so there is no build step for development.

## Use

```bash
npm run dev -- servers
npm run dev -- run "Find the latest production deployment for payments, inspect its logs, and tell me whether that deployment introduced a problem."
npm run dev -- run "Roll back the bad payments deployment" --approve prompt
npm run dev -- run "..." --approve reject --json
npm run dev -- eval ../examples/eval_tasks.txt
npm run dev -- memory
```

Or `npm run build` and use `node dist/src/cli.js` (also exposed as the `switchboard` bin).

## Tests

```bash
npm test          # offline: no API key or network needed
npm run typecheck
```

28 tests covering template resolution, plan validation (including malformed model
output), dependency ordering and cycle detection, approval gating and rejection,
transient-error retry, LLM argument repair, runtime argument resolution, replanning
with prior observations, the replan budget, memory hints, and an end-to-end run
against the real deployment server over stdio.

## Differences from the Python version

- **Plans are schema-validated.** Model output goes through zod before it becomes a
  `PlanStep`, so a malformed plan fails fast as a `PlanValidationError` instead of
  reaching execution. The planner also rejects duplicate step ids and plans over
  the step budget.
- **Replans see what already happened.** In the Python loop the executor raises
  before returning its outputs, so the replan hint always got an empty observation
  set. Here the orchestrator reads the outputs of steps that succeeded off the
  failed plan and passes a truncated summary to the planner.
- **Async everywhere.** LLM calls, approvals and recovery are all `async`, so the
  backoff no longer blocks the event loop and the approval prompt is a normal
  awaited readline.
- **Structured tool results.** If a server returns `structuredContent`, it's used
  directly; otherwise text blocks are JSON-parsed as before.
- **Resource cleanup** uses `await using` on the client pool.
- No `temperature` parameter: current Anthropic SDK models don't accept it.
