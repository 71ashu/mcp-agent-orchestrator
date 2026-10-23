#!/usr/bin/env node
/** CLI for the multi-server MCP agent orchestrator. */

import { existsSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline/promises";
import { Command, Option } from "commander";
import pc from "picocolors";
import { type Approver, autoApprove, autoReject } from "./approval.js";
import { loadConfig } from "./config.js";
import { evalRow, summarize } from "./evaluation.js";
import { MCPClientPool } from "./mcp-client.js";
import { type Plan, type PlanStep, type RunResult, StepStatus } from "./models.js";
import { Orchestrator } from "./orchestrator.js";

const STATUS_COLOR: Record<StepStatus, (s: string) => string> = {
  [StepStatus.Succeeded]: pc.green,
  [StepStatus.Failed]: pc.red,
  [StepStatus.Running]: pc.yellow,
  [StepStatus.Skipped]: pc.dim,
  [StepStatus.Rejected]: pc.red,
  [StepStatus.AwaitingApproval]: pc.magenta,
  [StepStatus.Pending]: pc.dim,
};

function table(headers: string[], rows: string[][]): string {
  const widths = headers.map((h, i) => Math.max(h.length, ...rows.map((r) => (r[i] ?? "").length)));
  const fmt = (cells: string[]) => cells.map((c, i) => c.padEnd(widths[i] ?? 0)).join("  ");
  return [pc.bold(fmt(headers)), pc.dim(widths.map((w) => "-".repeat(w)).join("  ")), ...rows.map(fmt)].join("\n");
}

function renderPlan(plan: Plan): void {
  if (plan.reasoning) console.log(`${pc.dim("Planner reasoning:")} ${plan.reasoning}\n`);
  console.log(
    table(
      ["#", "server.tool", "depends", "rationale"],
      plan.steps.map((s) => [
        String(s.id),
        s.qualifiedTool,
        s.dependsOn.join(",") || "-",
        s.rationale + (s.resolveArgumentsAtRuntime ? " (runtime args)" : ""),
      ]),
    ),
  );
  console.log();
}

function renderStep(step: PlanStep): void {
  const color = STATUS_COLOR[step.status];
  const lat = step.latencyMs ? ` ${step.latencyMs.toFixed(0)}ms` : "";
  let line = color(`● step ${step.id}  ${step.qualifiedTool}  ${step.status}${lat}`);
  if (step.error && (step.status === StepStatus.Failed || step.status === StepStatus.Rejected)) {
    line += `\n   ${pc.red(step.error.slice(0, 300))}`;
  }
  console.log(line);
}

const promptApprover: Approver = async (step, args) => {
  console.log(pc.magenta(`\nApproval required: ${step.qualifiedTool}`));
  if (step.rationale) console.log(pc.dim(step.rationale));
  console.log(JSON.stringify(args, null, 2));
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = await rl.question("  Approve this write operation? [y/N] ");
    return /^y(es)?$/i.test(answer.trim());
  } finally {
    rl.close();
  }
};

const APPROVERS: Record<string, Approver> = {
  prompt: promptApprover,
  auto: autoApprove,
  reject: autoReject,
};

function metricsLine(r: RunResult): string {
  const m = r.metrics;
  return pc.dim(
    `steps=${r.plan.steps.length} tool_calls=${m.toolCalls} failures=${m.toolFailures} ` +
      `retries=${m.retries} replans=${m.replans} approvals=${m.approvalsGranted}/${m.approvalsRequested} ` +
      `wall=${m.wallMs.toFixed(0)}ms tokens=${m.totalTokensIn}+${m.totalTokensOut} ` +
      `~$${m.estimatedCostUsd().toFixed(4)}`,
  );
}

const approveOption = (fallback: string) =>
  new Option("--approve <policy>", "write-tool policy").choices(["prompt", "auto", "reject"]).default(fallback);

const program = new Command()
  .name("switchboard")
  .description("Multi-server MCP agent orchestrator")
  .option("-c, --config <path>", "server registry", "config/servers.yaml");

program
  .command("servers")
  .description("Connect to every enabled MCP server and list discovered tools.")
  .action(async () => {
    await using pool = await MCPClientPool.connect(loadConfig(program.opts().config));
    for (const [name, conn] of pool.connections) {
      console.log(pc.bold(`\n${name}  (${conn.tools.length} tools)`));
      console.log(
        table(
          ["tool", "kind", "description"],
          conn.tools.map((t) => [t.name, t.kind, t.description.slice(0, 100)]),
        ),
      );
    }
  });

program
  .command("run")
  .description("Plan and execute a task across the connected MCP servers.")
  .argument("<goal>", "the task to accomplish")
  .addOption(approveOption("prompt"))
  .option("--json", "emit machine-readable result", false)
  .action(async (goal: string, opts: { approve: string; json: boolean }) => {
    const cfg = loadConfig(program.opts().config);
    let result: RunResult;
    {
      await using pool = await MCPClientPool.connect(cfg);
      const orch = new Orchestrator(cfg, pool, {
        approver: APPROVERS[opts.approve],
        ...(opts.json ? {} : { onPlan: renderPlan, onStep: renderStep }),
      });
      result = await orch.run(goal);
    }
    if (opts.json) {
      console.log(JSON.stringify(result, null, 2));
      return;
    }
    console.log(`\n${(result.success ? pc.green : pc.red)(pc.bold("Answer"))}\n${result.answer}\n`);
    console.log(metricsLine(result));
  });

program
  .command("eval")
  .description("Run a batch of goals and print an evaluation table.")
  .argument("<tasksFile>", "text file, one goal per line")
  .addOption(approveOption("auto"))
  .action(async (tasksFile: string, opts: { approve: string }) => {
    const goals = readFileSync(tasksFile, "utf8")
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#"));
    const cfg = loadConfig(program.opts().config);
    const results: RunResult[] = [];
    {
      await using pool = await MCPClientPool.connect(cfg);
      const orch = new Orchestrator(cfg, pool, { approver: APPROVERS[opts.approve] });
      for (const goal of goals) {
        console.log(pc.dim(`── ${goal}`));
        results.push(await orch.run(goal));
      }
    }
    console.log(
      table(
        ["goal", "ok", "steps", "calls", "fail", "retry", "replan", "wall(ms)", "tok(in/out)", "$"],
        results.map(evalRow).map((r) => [
          r.goal.slice(0, 40),
          r.success ? "✓" : "✗",
          String(r.steps),
          String(r.toolCalls),
          String(r.toolFailures),
          String(r.retries),
          String(r.replans),
          r.wallMs.toFixed(0),
          `${r.tokensIn}/${r.tokensOut}`,
          r.costUsd.toFixed(4),
        ]),
      ),
    );
    console.log(JSON.stringify(summarize(results), null, 2));
  });

program
  .command("memory")
  .description("Show recorded task memory.")
  .option("--path <path>", "memory file", "data/memory.json")
  .action((opts: { path: string }) => {
    if (!existsSync(opts.path)) {
      console.log(pc.dim("no memory yet"));
      return;
    }
    console.log(readFileSync(opts.path, "utf8"));
  });

program.parseAsync().catch((err: unknown) => {
  console.error(pc.red(err instanceof Error ? err.message : String(err)));
  process.exitCode = 1;
});
