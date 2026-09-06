from orchestrator.templating import resolve_templates, unresolved_refs


def test_exact_reference_preserves_type():
    outputs = {1: {"deployment": {"id": "d-482", "error_rate_after": 0.061}}}
    args = {"deployment_id": "{{steps.1.output.deployment.id}}"}
    assert resolve_templates(args, outputs) == {"deployment_id": "d-482"}


def test_reference_into_list_index():
    outputs = {2: {"commits": [{"sha": "abc"}, {"sha": "def"}]}}
    args = {"sha": "{{steps.2.output.commits[1].sha}}"}
    assert resolve_templates(args, outputs) == {"sha": "def"}


def test_embedded_reference_is_stringified():
    outputs = {1: {"count": 5}}
    args = {"msg": "found {{steps.1.output.count}} items"}
    assert resolve_templates(args, outputs) == {"msg": "found 5 items"}


def test_unresolved_refs_detected():
    args = {"x": "{{steps.9.output.foo}}"}
    resolved = resolve_templates(args, {})
    assert unresolved_refs(resolved)
