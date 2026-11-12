"""The request `LLM` builds must be accepted by the installed Anthropic SDK."""

from __future__ import annotations

import json

from conftest import scripted_llm


def test_complete_sends_only_parameters_the_sdk_accepts():
    llm = scripted_llm(["hello"])
    resp = llm.complete(system="s", user="u", max_tokens=50, purpose="plan")

    assert resp.text == "hello"
    sent = llm._client.requests[0]
    assert sent["model"] == "claude-sonnet-5-5"
    assert "temperature" not in sent


def test_complete_json_round_trips():
    llm = scripted_llm([json.dumps({"steps": []})])
    data, _resp = llm.complete_json(system="s", user="u", purpose="plan")
    assert data == {"steps": []}
