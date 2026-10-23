/** Resolve {{steps.N.output.path}} references inside plan-step arguments. */

const REF_SOURCE = String.raw`\{\{\s*steps\.(\d+)\.output([^}]*)\}\}`;
const REF_GLOBAL = () => new RegExp(REF_SOURCE, "g");
const REF_EXACT = new RegExp(`^${REF_SOURCE}$`);
const SEGMENT = /\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]/g;

export type StepOutputs = Map<number, unknown>;

function walk(value: unknown, path: string): unknown {
  let current = value;
  for (const m of path.matchAll(SEGMENT)) {
    const [, key, idx] = m;
    if (key !== undefined) {
      current =
        current && typeof current === "object" && !Array.isArray(current)
          ? ((current as Record<string, unknown>)[key] ?? null)
          : null;
    } else {
      const i = Number(idx);
      current = Array.isArray(current) && i < current.length ? current[i] : null;
    }
  }
  return current;
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "null";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/**
 * Recursively substitute step-output references.
 *
 * A string that is *exactly* one reference becomes the referenced value (keeping
 * its type). A reference embedded in a larger string is stringified in place.
 * References to steps with no recorded output are left intact so they can be
 * flagged or resolved later.
 */
export function resolveTemplates<T>(obj: T, outputs: StepOutputs): unknown {
  if (Array.isArray(obj)) return obj.map((v) => resolveTemplates(v, outputs));
  if (obj && typeof obj === "object") {
    return Object.fromEntries(
      Object.entries(obj).map(([k, v]) => [k, resolveTemplates(v, outputs)]),
    );
  }
  if (typeof obj !== "string") return obj;

  const full = REF_EXACT.exec(obj.trim());
  if (full) {
    const stepId = Number(full[1]);
    if (!outputs.has(stepId)) return obj;
    return walk(outputs.get(stepId), full[2] ?? "");
  }

  return obj.replace(REF_GLOBAL(), (whole, id: string, path: string) => {
    const stepId = Number(id);
    return outputs.has(stepId) ? stringify(walk(outputs.get(stepId), path)) : whole;
  });
}

/** List any {{steps.N.output...}} references still present in `obj`. */
export function unresolvedRefs(obj: unknown): string[] {
  const text = typeof obj === "string" ? obj : JSON.stringify(obj) ?? "";
  return [...text.matchAll(REF_GLOBAL())].map((m) => m[0]);
}
