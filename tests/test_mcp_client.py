from types import SimpleNamespace

from orchestrator.mcp_client import _normalize_content, _unwrap_structured


def _block(text):
    return SimpleNamespace(text=text)


def test_multiple_json_blocks_are_all_kept():
    blocks = [_block('{"n": 1}'), _block('{"n": 2}'), _block('{"n": 3}')]
    assert _normalize_content(blocks) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_single_json_block_is_unwrapped():
    assert _normalize_content([_block('{"id": "d-1"}')]) == {"id": "d-1"}


def test_plain_text_blocks():
    assert _normalize_content([_block("hello")]) == "hello"
    assert _normalize_content([_block("a"), _block("b")]) == ["a", "b"]


def test_empty_content():
    assert _normalize_content([]) is None


def test_structured_result_wrapper_is_unwrapped():
    assert _unwrap_structured({"result": [1, 2, 3]}) == [1, 2, 3]
    assert _unwrap_structured({"id": "d-1", "result": "x"}) == {"id": "d-1", "result": "x"}
