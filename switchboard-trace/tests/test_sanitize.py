import json
from dataclasses import dataclass

from pydantic import BaseModel

from switchboard_trace.sanitize import (
    REDACTED,
    TRUNCATED_SUFFIX,
    Sanitizer,
    is_secret_key,
    secrets_from_env,
)


def clean(value, **kw):
    return Sanitizer(**kw).clean(value)[0]


def test_secret_keys_are_redacted_at_any_depth():
    out = clean({
        "api_key": "abc123",
        "headers": {"Authorization": "Bearer xyz", "X-Api-Key": "k"},
        "nested": [{"password": "hunter2", "user": "ashu"}],
        "db": {"client_secret": "s", "accessToken": "t"},
    })
    assert out["api_key"] == REDACTED
    assert out["headers"] == {"Authorization": REDACTED, "X-Api-Key": REDACTED}
    assert out["nested"] == [{"password": REDACTED, "user": "ashu"}]
    assert out["db"] == {"client_secret": REDACTED, "accessToken": REDACTED}


def test_token_counts_are_not_mistaken_for_secrets():
    for key in ("tokens_in", "tokens_out", "max_tokens", "input_tokens", "token_count"):
        assert not is_secret_key(key), key
    for key in ("token", "auth_token", "GITHUB_PERSONAL_ACCESS_TOKEN", "session_id"):
        assert is_secret_key(key), key


def test_secret_formats_are_redacted_inside_strings():
    text = (
        "key sk-ant-api03-AbCdEf0123456789xyz and ghp_abcdefghijklmnopqrstuvwxyz0123 "
        "aws AKIAABCDEFGHIJKLMNOP header 'Authorization: Bearer eyJhbGciOi.payload.sig' "
        "dsn postgresql://admin:s3cret@db:5432/app"
    )
    out = clean(text)
    for leaked in ("sk-ant-api03", "ghp_", "AKIAABCD", "eyJhbGciOi", "s3cret"):
        assert leaked not in out
    assert "postgresql://admin:[REDACTED]@db:5432/app" in out
    assert "Bearer [REDACTED]" in out


def test_credential_key_value_pairs_inside_text_are_redacted():
    out = clean(
        'model said {"api_key": "hunter2-hunter2", "tokens_in": 12} and '
        "GET /x?access_token=abc123&page=2 with PASSWORD='p w' token: zzz"
    )
    assert "hunter2" not in out and "abc123" not in out and "p w" not in out and "zzz" not in out
    assert '"api_key": "[REDACTED]"' in out
    assert '"tokens_in": 12' in out and "page=2" in out


def test_known_secret_values_are_redacted_wherever_they_appear():
    s = Sanitizer(known_secrets=["my-internal-key-123"])
    out, _ = s.clean({"note": "called with my-internal-key-123 inline"})
    assert out == {"note": f"called with {REDACTED} inline"}


def test_secrets_from_env_picks_credential_like_vars_only():
    env = {"ANTHROPIC_API_KEY": "sk-ant-live-key", "HOME": "/Users/x", "DB_PASSWORD": "short"}
    assert secrets_from_env(env) == ["sk-ant-live-key"]  # "short" is < 8 chars


def test_non_json_values_become_json_safe_snapshots():
    class Model(BaseModel):
        name: str
        token: str

    @dataclass
    class Point:
        x: int

    source = {"items": [1, 2]}
    out = clean({
        "model": Model(name="m", token="t"),
        "point": Point(1),
        "set": {3},
        "raw": b"\x00\x01",
        "obj": object(),
        "nan": float("nan"),
        "source": source,
    })
    source["items"].append(3)  # mutation after capture must not leak into the trace
    assert out["model"] == {"name": "m", "token": REDACTED}
    assert out["point"] == {"x": 1}
    assert out["set"] == [3]
    assert out["raw"] == "<2 bytes>"
    assert out["obj"].startswith("<object object")
    assert out["nan"] == "nan"
    assert out["source"] == {"items": [1, 2]}
    json.dumps(out, allow_nan=False)


def test_cycles_do_not_recurse_forever():
    a: dict = {}
    a["self"] = a
    out = clean(a)
    assert "<max depth exceeded>" in json.dumps(out)


def test_small_fields_pass_through_untouched():
    out, truncated = Sanitizer(max_field_bytes=100).clean({"x": "a" * 10})
    assert not truncated and out == {"x": "a" * 10}


def test_oversized_fields_keep_their_shape_with_truncation_markers():
    s = Sanitizer(max_field_bytes=200)
    out, truncated = s.clean({"rows": [{"id": i, "msg": "m" * 20} for i in range(1000)], "after": 1})
    assert truncated
    rows = out["rows"]
    assert rows[0] == {"id": 0, "msg": "m" * 20}
    assert rows[-1].endswith("more items truncated>")
    assert out["…"] == "<1 more keys truncated>"
    assert len(json.dumps(out).encode()) < 200 + 1024

    text, truncated = s.clean("é" * 500)
    assert truncated and text.endswith(TRUNCATED_SUFFIX)
    assert len(text.removesuffix(TRUNCATED_SUFFIX).encode()) <= 200


def test_secret_straddling_the_truncation_point_is_still_redacted():
    s = Sanitizer(max_field_bytes=30)
    out, truncated = s.clean("x" * 20 + " sk-ant-api03-AbCdEf0123456789xyz tail")
    assert truncated
    assert "sk-ant" not in out and "AbCdEf" not in out


def test_truncation_cost_is_bounded_by_the_limit_not_the_input():
    import time

    s = Sanitizer()  # 32 KB
    small = [{"row": i, "blob": "y" * 200} for i in range(150)]  # ~32 KB
    huge = [{"row": i, "blob": "y" * 200} for i in range(50_000)]  # ~12 MB

    def cost(value):
        t0 = time.perf_counter()
        for _ in range(5):
            s.clean(value)
        return (time.perf_counter() - t0) / 5

    assert cost(huge) < cost(small) * 5  # would be ~350x if work scaled with input
