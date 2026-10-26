"""Thin Anthropic wrapper with JSON extraction and token accounting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from .tracing import get_tracer


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    raw: Any = None


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Best-effort parse of a JSON object/array from a model response."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        return json.loads(m.group(1))
    # fall back to the first balanced {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON found in model response: {text[:200]!r}")


class LLM:
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        purpose: str = "complete",
    ) -> LLMResponse:
        """`purpose` labels the call in traces (plan, resolve_args, repair_args, ...).

        No `temperature`: current Anthropic SDKs removed it from `messages.create`.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        trace_input = {"system": system, "user": user, "max_tokens": max_tokens}
        if tools:
            trace_input["tools"] = [t.get("name") for t in tools]
        with get_tracer().span(
            "llm_call", f"llm:{purpose}", model=self.model, input=trace_input,
            attributes={"purpose": purpose},
        ) as span:
            msg = self._client.messages.create(**kwargs)
            text_parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
            resp = LLMResponse(
                text="\n".join(text_parts),
                tokens_in=msg.usage.input_tokens,
                tokens_out=msg.usage.output_tokens,
                raw=msg,
            )
            span.set_tokens(resp.tokens_in, resp.tokens_out)
            span.set_output({"text": resp.text, "stop_reason": getattr(msg, "stop_reason", None)})
            span.set_attributes(
                cache_read_tokens=getattr(msg.usage, "cache_read_input_tokens", None) or 0,
                cache_write_tokens=getattr(msg.usage, "cache_creation_input_tokens", None) or 0,
            )
        return resp

    def complete_json(
        self, *, system: str, user: str, max_tokens: int = 2000, purpose: str = "complete"
    ) -> tuple[Any, LLMResponse]:
        resp = self.complete(
            system=system + "\n\nRespond with a single JSON value and nothing else.",
            user=user,
            max_tokens=max_tokens,
            purpose=purpose,
        )
        return extract_json(resp.text), resp
