/** Capability registry: turns discovered MCP tools into planner-facing context. */

import type Anthropic from "@anthropic-ai/sdk";
import { qualifiedName, type ToolSpec } from "./models.js";

export class ToolRegistry {
  private readonly byQualified: Map<string, ToolSpec>;

  constructor(readonly tools: ToolSpec[]) {
    this.byQualified = new Map(tools.map((t) => [qualifiedName(t), t]));
  }

  get(qualified: string): ToolSpec | undefined {
    return this.byQualified.get(qualified);
  }

  servers(): string[] {
    return [...new Set(this.tools.map((t) => t.server))].sort();
  }

  /** Human/LLM-readable listing grouped by server. */
  catalogText(includeSchema = true): string {
    const lines: string[] = [];
    for (const server of this.servers()) {
      lines.push(`## server: ${server}`);
      for (const tool of this.tools.filter((t) => t.server === server)) {
        lines.push(`- ${qualifiedName(tool)}  [${tool.kind === "write" ? "WRITE" : "read"}]`);
        if (tool.description) lines.push(`    ${tool.description.trim()}`);
        if (includeSchema) {
          const props = (tool.inputSchema.properties ?? {}) as Record<string, { type?: unknown }>;
          const required = new Set((tool.inputSchema.required ?? []) as string[]);
          const bits = Object.entries(props).map(
            ([name, def]) => `${name}${required.has(name) ? "*" : ""}:${formatType(def?.type)}`,
          );
          if (bits.length) lines.push(`    args: ${bits.join(", ")}`);
        }
      }
      lines.push("");
    }
    return lines.join("\n").trim();
  }

  /** Tool schemas in Anthropic tool-use format (used by runtime arg resolution). */
  anthropicTools(): Anthropic.Messages.Tool[] {
    return this.tools.map((tool) => ({
      name: qualifiedName(tool).replaceAll(".", "__"),
      description: tool.description || qualifiedName(tool),
      input_schema: (Object.keys(tool.inputSchema).length
        ? tool.inputSchema
        : { type: "object", properties: {} }) as Anthropic.Messages.Tool.InputSchema,
    }));
  }

  writeTools(): ToolSpec[] {
    return this.tools.filter((t) => t.kind === "write");
  }

  toJSON(): ToolSpec[] {
    return this.tools;
  }
}

function formatType(type: unknown): string {
  if (Array.isArray(type)) return type.join("|");
  return typeof type === "string" ? type : "any";
}
