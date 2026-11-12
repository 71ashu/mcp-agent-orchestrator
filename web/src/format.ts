// Turn tool names and raw tool output into plain language.

const VERBS: Record<string, string> = {
  rollback: "roll back",
  write: "write to",
  edit: "edit",
  create: "create",
  move: "move",
  delete: "delete",
  restart: "restart",
  deploy: "deploy",
};

const TARGET_KEYS = ["deployment_id", "id", "path", "source", "name", "service"];

/** "The agent wants to roll back d-payments-482" */
export function approvalTitle(tool: string, args: Record<string, unknown>): string {
  const [first, ...rest] = tool.split("_");
  const verb = VERBS[first];
  const targetKey =
    TARGET_KEYS.find((k) => typeof args[k] === "string") ??
    Object.keys(args).find((k) => k.endsWith("_id") && typeof args[k] === "string");
  if (verb && targetKey) return `The agent wants to ${verb} ${String(args[targetKey])}`;
  const words = [verb ?? first, ...rest].join(" ");
  return `The agent wants to ${words}`;
}

export function humanizeKey(key: string): string {
  const k = key.replace(/_ms(?=_|$)/, "").replace(/_at$/, "").replace(/_/g, " ").trim();
  return k.charAt(0).toUpperCase() + k.slice(1);
}

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return (
    d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "UTC",
    }) + " UTC"
  );
}

export function clockTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
}

export function formatValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    if (/rate/.test(key) && value < 1) return `${(value * 100).toFixed(1)}%`;
    if (/_ms(_|$)/.test(key)) return `${Math.round(value)} ms`;
    return value.toLocaleString("en-US");
  }
  if (typeof value === "string") return ISO.test(value) ? formatTime(value) : value;
  return JSON.stringify(value);
}

const SKIP_KEY = /sha|hash|checksum|token/i;
const PRIORITY = ["id", "service", "status", "version", "level", "msg", "message"];

/** The few fields of an object worth showing a non-engineer, in a sensible order. */
export function keyFields(obj: Record<string, unknown>, max = 6): [string, unknown][] {
  const scalar = Object.entries(obj).filter(
    ([k, v]) => (v === null || typeof v !== "object") && !SKIP_KEY.test(k),
  );
  scalar.sort((a, b) => rank(a[0]) - rank(b[0]));
  return scalar.slice(0, max);
}

function rank(key: string): number {
  const i = PRIORITY.indexOf(key);
  return i === -1 ? PRIORITY.length : i;
}

export function isLogLine(v: unknown): v is { msg?: string; message?: string; level?: string; ts?: string } {
  return (
    typeof v === "object" &&
    v !== null &&
    !Array.isArray(v) &&
    (typeof (v as any).msg === "string" || typeof (v as any).message === "string")
  );
}

export function formatLatency(ms: number | null): string {
  if (ms === null) return "";
  if (ms < 1000) return `${Math.max(1, Math.round(ms))} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatDuration(ms: number): string {
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
