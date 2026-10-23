/** Human-in-the-loop approval for write/destructive tools. */

import type { PlanStep } from "./models.js";
import type { ToolRegistry } from "./registry.js";

/** Receives (step, resolved arguments) and resolves true to allow the call. */
export type Approver = (
  step: PlanStep,
  args: Record<string, unknown>,
) => boolean | Promise<boolean>;

export interface ApprovalPolicy {
  requiresApproval(step: PlanStep): boolean;
}

/** Approve any tool the registry classifies as a WRITE operation. */
export class WriteToolPolicy implements ApprovalPolicy {
  constructor(
    private readonly registry: ToolRegistry,
    private readonly alwaysAskServers: Set<string> = new Set(),
  ) {}

  requiresApproval(step: PlanStep): boolean {
    if (this.alwaysAskServers.has(step.server)) return true;
    return this.registry.get(step.qualifiedTool)?.kind === "write";
  }
}

export const autoApprove: Approver = () => true;
export const autoReject: Approver = () => false;
