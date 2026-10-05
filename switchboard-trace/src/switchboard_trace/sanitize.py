"""Make arbitrary agent data safe to ship: JSON-able, secret-free, size-bounded.

Every payload field passes through `Sanitizer.clean` on the caller's thread. That
both redacts and *snapshots* the value, so later mutation by the agent can't change
what was recorded.

Work is bounded by the size budget, not the input: once `max_field_bytes` worth of
output has been produced, the walk stops and leaves truncation markers. A 1 MB tool
result costs about as much to trace as a 32 KB one.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "…[truncated]"
DEFAULT_MAX_FIELD_BYTES = 32 * 1024
_MAX_DEPTH = 32
_REDACT_WINDOW_SLACK = 256  # look past the cut so a secret straddling it is still caught
_MARKER_SLACK_BYTES = 1024

# Key names (lower-cased, separators stripped) whose values are always redacted.
_SECRET_KEY_SUBSTRINGS = (
    "apikey", "secret", "password", "passwd", "passphrase", "authorization",
    "privatekey", "accesstoken", "refreshtoken", "idtoken", "authtoken",
    "sessiontoken", "cookie", "credential",
)
_SECRET_KEY_EXACT = frozenset({"token", "auth", "bearer", "pat", "sessionid"})

# Well-known credential formats, redacted wherever they appear inside strings.
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^@\s/]+@")
# `key: value` / `key=value` pairs inside free text: JSON in an LLM reply, query
# strings, env dumps. Only redacted when the key itself looks like a credential.
_KV_IN_TEXT = re.compile(
    r"""(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,40})(?P<kq>["']?)(?P<sep>[ \t]*[:=][ \t]*)"""
    r"""(?:"(?P<dq>[^"\n]*)"|'(?P<sq>[^'\n]*)'|(?P<bare>[^\s"',;&}\]]+))"""
)


def is_secret_key(key: str) -> bool:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    return norm in _SECRET_KEY_EXACT or any(s in norm for s in _SECRET_KEY_SUBSTRINGS)


def secrets_from_env(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of env vars that look like credentials (e.g. ANTHROPIC_API_KEY)."""
    env = os.environ if environ is None else environ
    return [v for k, v in env.items() if is_secret_key(k) and len(v) >= 8]


_SCHEMES = ("bearer", "basic")  # "Authorization: Bearer x" is left to _BEARER


def _redact_kv(m: re.Match[str]) -> str:
    already_handled = m.group(0).endswith(REDACTED) or (m["bare"] or "").lower() in _SCHEMES
    if already_handled or not is_secret_key(m["key"]):
        return m.group(0)
    quote = '"' if m["dq"] is not None else "'" if m["sq"] is not None else ""
    return f"{m['key']}{m['kq']}{m['sep']}{quote}{REDACTED}{quote}"


class _Budget:
    __slots__ = ("exhausted", "remaining")

    def __init__(self, size: int):
        self.remaining = size
        self.exhausted = False

    def charge(self, n: int) -> None:
        self.remaining -= n


class Sanitizer:
    def __init__(
        self,
        *,
        max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES,
        known_secrets: Iterable[str] = (),
    ):
        self.max_field_bytes = max_field_bytes
        # Longest first so a secret that contains another is replaced whole.
        self._known = sorted({s for s in known_secrets if s}, key=len, reverse=True)
        self._window_slack = max((len(s) for s in self._known), default=0) + _REDACT_WINDOW_SLACK

    def clean(self, value: Any) -> tuple[Any, bool]:
        """Return (redacted JSON-safe copy, was_truncated).

        A truncated value keeps its shape: long strings end in TRUNCATED_SUFFIX and
        containers gain a marker entry saying how much was left out.
        """
        budget = _Budget(self.max_field_bytes)
        safe = self._redact(value, 0, budget)
        if not budget.exhausted:
            return safe, False
        # Markers and JSON escaping aren't charged exactly; enforce a hard ceiling.
        encoded = json.dumps(safe, ensure_ascii=False).encode()
        if len(encoded) > self.max_field_bytes + _MARKER_SLACK_BYTES:
            preview = encoded[: self.max_field_bytes].decode(errors="ignore")
            return preview + TRUNCATED_SUFFIX, True
        return safe, True

    def redact_text(self, text: str) -> str:
        for secret in self._known:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in _SECRET_VALUE_PATTERNS:
            text = pattern.sub(REDACTED, text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}:{REDACTED}@", text)
        return _KV_IN_TEXT.sub(_redact_kv, text) if ("=" in text or ":" in text) else text

    def _redact(self, value: Any, depth: int, budget: _Budget) -> Any:
        if depth > _MAX_DEPTH:
            return "<max depth exceeded>"
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)  # NaN/Infinity aren't valid JSON
        if value is None or isinstance(value, (bool, int, float)):
            budget.charge(8)
            return value
        if isinstance(value, str):
            return self._redact_str(value, budget)
        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth, budget)
        if isinstance(value, (list, tuple, set, frozenset)):
            return self._redact_sequence(value, depth, budget)
        if hasattr(value, "model_dump"):  # pydantic v2
            return self._redact(value.model_dump(mode="json"), depth + 1, budget)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._redact(dataclasses.asdict(value), depth + 1, budget)
        if isinstance(value, bytes):
            return f"<{len(value)} bytes>"
        return self._redact_str(str(value), budget)

    def _redact_str(self, value: str, budget: _Budget) -> str:
        limit = max(budget.remaining, 0)
        size = len(value) if value.isascii() else len(value.encode())
        if size <= limit:
            budget.charge(size + 2)
            return self.redact_text(value)
        budget.exhausted = True
        budget.remaining = 0
        # Redact a window wider than the cut, then cut (on a UTF-8 boundary): no
        # half-secret can survive.
        window = self.redact_text(value[: limit + self._window_slack])
        return window.encode()[:limit].decode(errors="ignore") + TRUNCATED_SUFFIX

    def _redact_mapping(self, value: Mapping, depth: int, budget: _Budget) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if budget.remaining <= 0:
                budget.exhausted = True
                out["…"] = f"<{len(value) - i} more keys truncated>"
                break
            key = str(k)
            budget.charge(len(key) + 4)
            out[key] = REDACTED if is_secret_key(key) else self._redact(v, depth + 1, budget)
        return out

    def _redact_sequence(self, value: Iterable, depth: int, budget: _Budget) -> list[Any]:
        items = list(value) if not isinstance(value, (list, tuple)) else value
        out: list[Any] = []
        for i, v in enumerate(items):
            if budget.remaining <= 0:
                budget.exhausted = True
                out.append(f"<{len(items) - i} more items truncated>")
                break
            budget.charge(1)
            out.append(self._redact(v, depth + 1, budget))
        return out
