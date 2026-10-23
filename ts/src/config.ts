/** Load and validate configuration (server registry + environment). */

import { readFileSync } from "node:fs";
import { config as loadDotenv } from "dotenv";
import { parse as parseYaml } from "yaml";
import { z } from "zod";

const ENV_RE = /\$\{([A-Z0-9_]+)\}/g;

function expand(value: unknown): unknown {
  if (typeof value === "string") {
    return value.replace(ENV_RE, (_m, name: string) => process.env[name] ?? "");
  }
  if (Array.isArray(value)) return value.map(expand);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expand(v)]));
  }
  return value;
}

export const ServerConfig = z.object({
  name: z.string().min(1),
  enabled: z.boolean().default(true),
  description: z.string().default(""),
  transport: z.literal("stdio").default("stdio"),
  command: z.string().min(1),
  args: z.array(z.string()).default([]),
  env: z.record(z.string(), z.string()).default({}),
  write_tools: z.array(z.string()).default([]),
});
export type ServerConfig = z.infer<typeof ServerConfig>;

export interface AppConfig {
  model: string;
  maxPlanSteps: number;
  maxStepAttempts: number;
  maxReplans: number;
  servers: ServerConfig[];
}

export const DEFAULT_MODEL = "claude-sonnet-5";

export function enabledServers(config: AppConfig): ServerConfig[] {
  return config.servers.filter((s) => s.enabled);
}

export function loadConfig(path = "config/servers.yaml"): AppConfig {
  loadDotenv({ quiet: true });
  const raw = expand(parseYaml(readFileSync(path, "utf8")) ?? {}) as { servers?: unknown[] };
  const servers = (raw.servers ?? []).map((s) => ServerConfig.parse(s));
  return {
    model: process.env.ORCHESTRATOR_MODEL || DEFAULT_MODEL,
    maxPlanSteps: 12,
    maxStepAttempts: 3,
    maxReplans: 2,
    servers,
  };
}
