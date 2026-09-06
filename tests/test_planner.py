import pytest

from orchestrator.models import RunMetrics
from orchestrator.planner import Planner, PlanValidationError

from conftest import FakeLLM


def test_valid_plan_parses(registry):
    llm = FakeLLM(json_queue=[{
        "reasoning": "look up the deployment then its logs",
        "steps": [
            {"id": 1, "server": "deployment", "tool": "get_latest_deployment",
             "arguments": {"service": "payments"}, "depends_on": []},
            {"id": 2, "server": "deployment", "tool": "get_logs", "depends_on": [1],
             "arguments": {"deployment_id": "{{steps.1.output.id}}"}},
        ],
    }])
    plan = Planner(llm, registry).create_plan("check payments", RunMetrics())
    assert len(plan.steps) == 2
    assert plan.steps[1].depends_on == [1]


def test_unknown_tool_rejected(registry):
    llm = FakeLLM(json_queue=[{
        "steps": [{"id": 1, "server": "deployment", "tool": "nonexistent", "arguments": {}}]
    }])
    with pytest.raises(PlanValidationError):
        Planner(llm, registry).create_plan("x", RunMetrics())


def test_dangling_dependency_rejected(registry):
    llm = FakeLLM(json_queue=[{
        "steps": [{"id": 1, "server": "deployment", "tool": "get_logs",
                   "arguments": {}, "depends_on": [7]}]
    }])
    with pytest.raises(PlanValidationError):
        Planner(llm, registry).create_plan("x", RunMetrics())
