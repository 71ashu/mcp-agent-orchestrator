import { describe, expect, it } from "vitest";
import { resolveTemplates, unresolvedRefs } from "../src/templating.js";

describe("resolveTemplates", () => {
  it("keeps the referenced value's type for an exact reference", () => {
    const outputs = new Map([[1, { deployment: { id: "d-482", error_rate_after: 0.061 } }]]);
    expect(resolveTemplates({ deployment_id: "{{steps.1.output.deployment.id}}" }, outputs)).toEqual({
      deployment_id: "d-482",
    });
    expect(resolveTemplates({ rate: "{{steps.1.output.deployment.error_rate_after}}" }, outputs)).toEqual({
      rate: 0.061,
    });
  });

  it("walks into list indexes", () => {
    const outputs = new Map([[2, { commits: [{ sha: "abc" }, { sha: "def" }] }]]);
    expect(resolveTemplates({ sha: "{{steps.2.output.commits[1].sha}}" }, outputs)).toEqual({ sha: "def" });
  });

  it("stringifies a reference embedded in a larger string", () => {
    const outputs = new Map([[1, { count: 5 }]]);
    expect(resolveTemplates({ msg: "found {{steps.1.output.count}} items" }, outputs)).toEqual({
      msg: "found 5 items",
    });
  });

  it("resolves inside nested arrays and objects", () => {
    const outputs = new Map([[1, { id: "x" }]]);
    expect(resolveTemplates({ ids: ["{{steps.1.output.id}}"], meta: { ref: "{{ steps.1.output.id }}" } }, outputs)).toEqual({
      ids: ["x"],
      meta: { ref: "x" },
    });
  });

  it("returns null for a missing path instead of throwing", () => {
    const outputs = new Map([[1, { a: 1 }]]);
    expect(resolveTemplates({ v: "{{steps.1.output.nope.deeper}}" }, outputs)).toEqual({ v: null });
  });

  it("leaves references to unknown steps intact so they can be detected", () => {
    const resolved = resolveTemplates({ x: "{{steps.9.output.foo}}" }, new Map());
    expect(unresolvedRefs(resolved)).toEqual(["{{steps.9.output.foo}}"]);
  });
});
