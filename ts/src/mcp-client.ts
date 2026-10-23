/**
 * Multi-server MCP client.
 *
 * Owns one stdio connection per enabled server, discovers each server's tools, and
 * dispatches tool calls to the right session.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { type AppConfig, enabledServers, type ServerConfig } from "./config.js";
import type { JsonSchema, ToolSpec } from "./models.js";

/** The slice of the pool the executor and orchestrator depend on (faked in tests). */
export interface ToolPool {
  allTools(): ToolSpec[];
  getTool(server: string, name: string): ToolSpec | undefined;
  callTool(server: string, name: string, args: Record<string, unknown>): Promise<unknown>;
}

/** Raised when an MCP server reports a tool execution error. */
export class ToolCallError extends Error {
  override name = "ToolCallError";
}

export class ServerConnection {
  tools: ToolSpec[] = [];

  constructor(
    readonly config: ServerConfig,
    readonly client: Client,
  ) {}

  async discover(): Promise<ToolSpec[]> {
    const specs: ToolSpec[] = [];
    let cursor: string | undefined;
    do {
      const page = await this.client.listTools(cursor ? { cursor } : undefined);
      for (const tool of page.tools) {
        specs.push({
          server: this.config.name,
          name: tool.name,
          description: tool.description ?? "",
          inputSchema: (tool.inputSchema ?? {}) as JsonSchema,
          kind: this.config.write_tools.includes(tool.name) ? "write" : "read",
        });
      }
      cursor = page.nextCursor;
    } while (cursor);
    this.tools = specs;
    return specs;
  }
}

/** Connects to every enabled server and keeps the sessions open for the run. */
export class MCPClientPool implements ToolPool, AsyncDisposable {
  readonly connections = new Map<string, ServerConnection>();

  constructor(readonly config: AppConfig) {}

  static async connect(config: AppConfig): Promise<MCPClientPool> {
    const pool = new MCPClientPool(config);
    try {
      await pool.connectAll();
    } catch (err) {
      await pool.close();
      throw err;
    }
    return pool;
  }

  async connectAll(): Promise<void> {
    for (const server of enabledServers(this.config)) {
      await this.connectOne(server);
    }
  }

  private async connectOne(server: ServerConfig): Promise<void> {
    const transport = new StdioClientTransport({
      command: resolveCommand(server.command),
      args: server.args,
      env: { ...definedEnv(process.env), ...server.env },
      cwd: process.cwd(),
      stderr: "pipe",
    });
    const client = new Client({ name: "switchboard", version: "0.1.0" });
    try {
      await client.connect(transport);
    } catch (err) {
      throw new Error(`failed to start MCP server '${server.name}': ${errorMessage(err)}`);
    }
    // Register before discovery so a failure here still gets cleaned up by close().
    const conn = new ServerConnection(server, client);
    this.connections.set(server.name, conn);
    await conn.discover();
  }

  async close(): Promise<void> {
    const clients = [...this.connections.values()].map((c) => c.client);
    this.connections.clear();
    await Promise.allSettled(clients.map((c) => c.close()));
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  // ------------------------------------------------------------------ queries

  allTools(): ToolSpec[] {
    return [...this.connections.values()].flatMap((c) => c.tools);
  }

  getTool(server: string, name: string): ToolSpec | undefined {
    return this.connections.get(server)?.tools.find((t) => t.name === name);
  }

  // ------------------------------------------------------------------ dispatch

  async callTool(server: string, name: string, args: Record<string, unknown>): Promise<unknown> {
    const conn = this.connections.get(server);
    if (!conn) throw new Error(`unknown server '${server}'`);
    const result = await conn.client.callTool({ name, arguments: args ?? {} });
    const content = (result.content ?? []) as ContentBlock[];
    if (result.isError) throw new ToolCallError(stringifyContent(content));
    if (result.structuredContent !== undefined) return result.structuredContent;
    return normalizeContent(content);
  }
}

type ContentBlock = { type: string; text?: string; [k: string]: unknown };

/**
 * Map a bare `node` to the interpreter running the orchestrator so stdio child
 * servers don't accidentally launch under a different Node.
 */
function resolveCommand(command: string): string {
  return command === "node" ? process.execPath : command;
}

function definedEnv(env: NodeJS.ProcessEnv): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter((e): e is [string, string] => e[1] !== undefined),
  );
}

function stringifyContent(content: ContentBlock[]): string {
  const parts = content.map((b) => b.text ?? JSON.stringify(b));
  return parts.join("\n") || "unknown tool error";
}

/** Collapse MCP content blocks into plain values for the LLM and templates. */
export function normalizeContent(content: ContentBlock[]): unknown {
  const texts: string[] = [];
  for (const block of content) {
    if (block.text === undefined) {
      texts.push(JSON.stringify(block));
      continue;
    }
    try {
      return JSON.parse(block.text); // many servers return a single JSON text block
    } catch {
      texts.push(block.text);
    }
  }
  return texts.length === 1 ? texts[0] : texts;
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
