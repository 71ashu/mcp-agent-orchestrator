/** Thin Anthropic wrapper with JSON extraction and token accounting. */

import Anthropic from "@anthropic-ai/sdk";

export interface LLMResponse {
  text: string;
  tokensIn: number;
  tokensOut: number;
  raw?: unknown;
}

export interface CompleteOptions {
  system: string;
  user: string;
  maxTokens?: number;
  tools?: Anthropic.Messages.Tool[];
  toolChoice?: Anthropic.Messages.ToolChoice;
}

/** What the rest of the orchestrator needs from a model. Tests supply a fake. */
export interface LLMClient {
  complete(opts: CompleteOptions): Promise<LLMResponse>;
  completeJson(opts: CompleteOptions): Promise<[unknown, LLMResponse]>;
}

const JSON_BLOCK = /```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```/;

/** Best-effort parse of a JSON object/array from a model response. */
export function extractJson(input: string): unknown {
  const text = input.trim();
  try {
    return JSON.parse(text);
  } catch {
    // fall through
  }
  const block = JSON_BLOCK.exec(text);
  if (block?.[1]) return JSON.parse(block[1]);
  // fall back to the first balanced {...} or [...]
  for (const [opener, closer] of [
    ["{", "}"],
    ["[", "]"],
  ] as const) {
    const start = text.indexOf(opener);
    const end = text.lastIndexOf(closer);
    if (start !== -1 && end > start) return JSON.parse(text.slice(start, end + 1));
  }
  throw new Error(`no JSON found in model response: ${JSON.stringify(text.slice(0, 200))}`);
}

export class LLM implements LLMClient {
  private readonly client: Anthropic;

  constructor(
    readonly model: string,
    apiKey?: string,
  ) {
    this.client = apiKey ? new Anthropic({ apiKey }) : new Anthropic();
  }

  async complete({ system, user, maxTokens = 2000, tools, toolChoice }: CompleteOptions) {
    const msg = await this.client.messages.create({
      model: this.model,
      max_tokens: maxTokens,
      system,
      messages: [{ role: "user", content: user }],
      ...(tools?.length ? { tools } : {}),
      ...(toolChoice ? { tool_choice: toolChoice } : {}),
    });
    const text = msg.content
      .filter((b): b is Anthropic.Messages.TextBlock => b.type === "text")
      .map((b) => b.text)
      .join("\n");
    return {
      text,
      tokensIn: msg.usage.input_tokens,
      tokensOut: msg.usage.output_tokens,
      raw: msg,
    } satisfies LLMResponse;
  }

  async completeJson(opts: CompleteOptions): Promise<[unknown, LLMResponse]> {
    const resp = await this.complete({
      ...opts,
      system: `${opts.system}\n\nRespond with a single JSON value and nothing else.`,
    });
    return [extractJson(resp.text), resp];
  }
}
