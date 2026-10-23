/** Offline test doubles: no network, no API key required. */

import type { CompleteOptions, LLMClient, LLMResponse } from "../src/llm.js";
import type { ToolPool } from "../src/mcp-client.js";
import type { ToolSpec } from "../src/models.js";
import { ToolRegistry } from "../src/registry.js";

type Handler = (args: Record<string, unknown>) => unknown;

/** Stands in for MCPClientPool. `handlers["server.tool"](args)`; returning an Error throws it. */
export class FakePool implements ToolPool {
  readonly calls: [server: string, tool: string, args: Record<string, unknown>][] = [];

  constructor(
    private readonly handlers: Record<string, Handler>,
    private readonly tools: ToolSpec[],
  ) {}

  allTools() {
    return this.tools;
  }

  getTool(server: string, name: string) {
    return this.tools.find((t) => t.server === server && t.name === name);
  }

  async callTool(server: string, name: string, args: Record<string, unknown>) {
    this.calls.push([server, name, args]);
    const fn = this.handlers[`${server}.${name}`];
    if (!fn) throw new Error(`no fake handler for ${server}.${name}`);
    const result = fn(args);
    if (result instanceof Error) throw result;
    return result;
  }
}

/** Returns canned JSON/text and records prompts. */
export class FakeLLM implements LLMClient {
  readonly prompts: string[] = [];
  private readonly queue: unknown[];

  constructor(jsonQueue: unknown[] = [], readonly text = "synthesized answer") {
    this.queue = [...jsonQueue];
  }

  private resp(text: string): LLMResponse {
    return { text, tokensIn: 10, tokensOut: 5 };
  }

  async complete({ user }: CompleteOptions) {
    this.prompts.push(user);
    return this.resp(this.text);
  }

  async completeJson({ user }: CompleteOptions): Promise<[unknown, LLMResponse]> {
    this.prompts.push(user);
    return [this.queue.length ? this.queue.shift() : {}, this.resp("{}")];
  }
}

const obj = (props: Record<string, unknown>, required: string[] = []) => ({
  type: "object",
  properties: props,
  required,
});

export function deploymentTools(): ToolSpec[] {
  return [
    {
      server: "deployment",
      name: "get_latest_deployment",
      description: "latest",
      kind: "read",
      inputSchema: obj({ service: { type: "string" } }, ["service"]),
    },
    {
      server: "deployment",
      name: "get_logs",
      description: "logs",
      kind: "read",
      inputSchema: obj({ deployment_id: { type: "string" } }),
    },
    {
      server: "deployment",
      name: "rollback_deployment",
      description: "rollback",
      kind: "write",
      inputSchema: obj({ deployment_id: { type: "string" } }),
    },
  ];
}

export function deploymentRegistry(): ToolRegistry {
  return new ToolRegistry(deploymentTools());
}
