"""Resolve {{steps.N.output.path}} references inside plan-step arguments."""

from __future__ import annotations

import re
from typing import Any

_REF = re.compile(r"\{\{\s*steps\.(\d+)\.output([^}]*)\}\}")
_SEG = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def _walk(value: Any, path: str) -> Any:
    for m in _SEG.finditer(path):
        key, idx = m.group(1), m.group(2)
        if key is not None:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = getattr(value, key, None)
        else:
            i = int(idx)
            if isinstance(value, (list, tuple)) and -len(value) <= i < len(value):
                value = value[i]
            else:
                value = None
    return value


def resolve_templates(obj: Any, outputs: dict[int, Any]) -> Any:
    """Recursively substitute step-output references.

    A string that is *exactly* one reference becomes the referenced value (keeping
    its type). A reference embedded in a larger string is stringified in place.
    """
    if isinstance(obj, dict):
        return {k: resolve_templates(v, outputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_templates(v, outputs) for v in obj]
    if not isinstance(obj, str):
        return obj

    full = _REF.fullmatch(obj.strip())
    if full:
        step_id, path = int(full.group(1)), full.group(2)
        if step_id not in outputs:
            return obj  # leave intact so it can be flagged / resolved later
        return _walk(outputs[step_id], path)

    def _sub(m: re.Match) -> str:
        step_id, path = int(m.group(1)), m.group(2)
        if step_id not in outputs:
            return m.group(0)
        return str(_walk(outputs[step_id], path))

    return _REF.sub(_sub, obj)


def unresolved_refs(obj: Any) -> list[str]:
    """List any {{steps.N.output...}} references still present in `obj`."""
    return [m.group(0) for m in _REF.finditer(repr(obj))]
