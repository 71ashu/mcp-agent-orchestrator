"""Intent analysis + multi-server task planning."""

from __future__ import annotations

from .llm import LLM
from .models import Plan, PlanStep, RunMetrics
from .registry import ToolRegistry

PLANNER_SYSTEM = """\
You are the planner for a multi-server MCP agent orchestrator. You are given a user
goal and a catalog of tools exposed by several connected MCP servers. Produce a
minimal, dependency-aware execution plan that achieves the goal.

Rules:
- Use ONLY tools that appear in the catalog, addressed as "<server>.<tool>".
- Prefer the fewest steps that fully answer the goal. Do not add exploratory steps
  unless the goal genuinely needs them.
- Express data flow with `depends_on` (list of earlier step ids). A step runs only
  after its dependencies succeed.
- If a step's arguments depend on the OUTPUT of an earlier step, either:
  (a) reference it with a template string like "{{steps.1.output}}" or a path into
      it such as "{{steps.1.output.deployment.commit_sha}}", OR
  (b) set "resolve_arguments_at_runtime": true and leave arguments partial; the
      executor will fill them from live observations.
- Steps that only READ data need no special handling. WRITE tools (tagged [WRITE])
  will be gated for human approval automatically — still include them when the goal
  asks for an action.

Return JSON:
{
  "reasoning": "one short paragraph on the approach",
  "steps": [
    {
      "id": 1,
      "server": "github",
      "tool": "search_commits",
      "arguments": {"q": "repo:acme/payments"},
      "depends_on": [],
      "rationale": "why this step",
      "resolve_arguments_at_runtime": false
    }
  ]
}
"""


class Planner:
    def __init__(self, llm: LLM, registry: ToolRegistry, max_steps: int = 12):
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps

    def create_plan(self, goal: str, metrics: RunMetrics, hint: str | None = None) -> Plan:
        user = (
            f"GOAL:\n{goal}\n\n"
            f"TOOL CATALOG:\n{self.registry.catalog_text()}\n\n"
            f"Plan at most {self.max_steps} steps."
        )
        if hint:
            user += f"\n\nADDITIONAL CONTEXT:\n{hint}"

        data, resp = self.llm.complete_json(
            system=PLANNER_SYSTEM, user=user, max_tokens=2500
        )
        metrics.planner_tokens_in += resp.tokens_in
        metrics.planner_tokens_out += resp.tokens_out

        steps = [PlanStep(**s) for s in data.get("steps", [])]
        self._validate(steps)
        return Plan(goal=goal, reasoning=data.get("reasoning", ""), steps=steps)

    def _validate(self, steps: list[PlanStep]) -> None:
        ids = {s.id for s in steps}
        for s in steps:
            if self.registry.get(s.qualified_tool) is None:
                raise PlanValidationError(
                    f"step {s.id} references unknown tool '{s.qualified_tool}'"
                )
            for dep in s.depends_on:
                if dep not in ids:
                    raise PlanValidationError(
                        f"step {s.id} depends on missing step {dep}"
                    )
                if dep == s.id:
                    raise PlanValidationError(f"step {s.id} depends on itself")


class PlanValidationError(ValueError):
    pass
