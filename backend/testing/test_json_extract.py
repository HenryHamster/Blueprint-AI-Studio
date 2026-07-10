"""Direct unit tests for the canonical JSON-extraction helpers in app.json_extract."""

import json

import pytest

from app.json_extract import (
    _extract_json_array,
    _extract_json_object,
    _extract_last_json_array,
    async_json_loads,
    scan_balanced_json_objects,
)


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_object_clean():
    text = '{"title": "t", "summary": "s", "body": "b"}'
    result = _extract_json_object(text, "title", "summary", "body")
    assert result is not None
    obj = json.loads(result)
    assert obj["title"] == "t"


def test_extract_json_object_prose_wrapped():
    text = 'Here is the result:\n{"title": "t", "summary": "s", "body": "b"}\nDone.'
    result = _extract_json_object(text, "title", "summary", "body")
    assert result is not None
    obj = json.loads(result)
    assert obj["body"] == "b"


def test_extract_json_object_missing_required_key_returns_none():
    text = '{"title": "t", "summary": "s"}'
    assert _extract_json_object(text, "title", "summary", "body") is None


def test_extract_json_object_no_json_returns_none():
    assert _extract_json_object("no braces here", "key") is None


def test_extract_json_object_string_with_braces_inside():
    # A brace inside a string value must not confuse the depth counter.
    text = '{"code": "if (x) { return; }", "verdict": "APPROVE"}'
    result = _extract_json_object(text, "verdict")
    assert result is not None
    obj = json.loads(result)
    assert obj["verdict"] == "APPROVE"


def test_extract_json_object_skips_malformed_first_object():
    # First balanced {} lacks the required key; scanner advances to the second.
    text = '{"unrelated": 1} prose {"key": "value"}'
    result = _extract_json_object(text, "key")
    assert result is not None
    assert json.loads(result) == {"key": "value"}


def test_extract_json_object_returns_first_matching():
    # When multiple objects contain the key, the first is returned.
    text = '{"verdict": "APPROVE"} {"verdict": "REQUEST_CHANGES"}'
    result = _extract_json_object(text, "verdict")
    assert result is not None
    assert json.loads(result)["verdict"] == "APPROVE"


# ---------------------------------------------------------------------------
# scan_balanced_json_objects
# ---------------------------------------------------------------------------


def test_scan_balanced_json_objects_clean():
    text = '{"verdict": "APPROVE", "feedback": "lgtm"}'
    result = scan_balanced_json_objects(text, required_key="verdict")
    assert result == {"verdict": "APPROVE", "feedback": "lgtm"}


def test_scan_balanced_json_objects_missing_key_returns_none():
    text = '{"outcome": "pass"}'
    assert scan_balanced_json_objects(text, required_key="verdict") is None


def test_scan_balanced_json_objects_no_required_key_returns_first():
    text = 'prose {"a": 1} more {"b": 2}'
    result = scan_balanced_json_objects(text)
    assert result == {"a": 1}


def test_scan_balanced_json_objects_last_returns_last():
    text = '{"verdict": "APPROVE"} noise {"verdict": "REQUEST_CHANGES"}'
    result = scan_balanced_json_objects(text, required_key="verdict", last=True)
    assert result is not None
    assert result["verdict"] == "REQUEST_CHANGES"


def test_scan_balanced_json_objects_skips_malformed_json():
    # First balanced span is not valid JSON; scanner advances to the next.
    text = '{ not valid json } {"verdict": "APPROVE"}'
    result = scan_balanced_json_objects(text, required_key="verdict")
    assert result == {"verdict": "APPROVE"}


def test_scan_balanced_json_objects_empty_text_returns_none():
    assert scan_balanced_json_objects("", required_key="verdict") is None


def test_scan_balanced_json_objects_string_with_braces():
    # Braces inside string values must not confuse the depth counter.
    text = '{"code": "if (x) { return; }", "verdict": "APPROVE"}'
    result = scan_balanced_json_objects(text, required_key="verdict")
    assert result is not None
    assert result["verdict"] == "APPROVE"
    assert result["code"] == "if (x) { return; }"


# ---------------------------------------------------------------------------
# _extract_json_array
# ---------------------------------------------------------------------------


def test_extract_json_array_clean():
    text = '[{"title": "a"}, {"title": "b"}]'
    result = _extract_json_array(text, "title")
    assert result is not None
    assert json.loads(result) == [{"title": "a"}, {"title": "b"}]


def test_extract_json_array_missing_key_returns_none():
    text = '[{"name": "a"}]'
    assert _extract_json_array(text, "title") is None


def test_extract_json_array_prose_wrapped():
    text = 'Results:\n[{"title": "x"}]\nEnd.'
    result = _extract_json_array(text, "title")
    assert result is not None
    assert json.loads(result)[0]["title"] == "x"


# ---------------------------------------------------------------------------
# _extract_last_json_array
# ---------------------------------------------------------------------------


def test_extract_last_json_array_returns_last():
    text = '[{"verdict": "first"}] noise [{"verdict": "last"}]'
    result = _extract_last_json_array(text, "verdict")
    assert result is not None
    assert json.loads(result)[0]["verdict"] == "last"


def test_extract_last_json_array_missing_key_returns_none():
    text = '[{"a": 1}]'
    assert _extract_last_json_array(text, "verdict") is None


# ---------------------------------------------------------------------------
# async_json_loads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_json_loads_small_payload():
    """Small payload (< threshold) parses synchronously and returns correct result."""
    small = '{"key": "value", "number": 42}'
    result = await async_json_loads(small)
    assert result == {"key": "value", "number": 42}


@pytest.mark.asyncio
async def test_async_json_loads_large_payload():
    """Large payload (> threshold) offloads to thread and returns correct result."""
    # Build a payload just over the 8192-byte threshold
    large = '{"data": "' + "x" * 8200 + '"}'
    assert len(large) > 8192
    result = await async_json_loads(large)
    assert result["data"] == "x" * 8200


@pytest.mark.asyncio
async def test_async_json_loads_exactly_at_threshold():
    """Payload at exactly 8192 bytes parses synchronously (not > threshold)."""
    # Craft a payload that equals exactly 8192 bytes
    inner = "x" * (8192 - len('{"data": ""}'))
    payload = '{"data": "' + inner + '"}'
    assert len(payload) == 8192, f"expected 8192, got {len(payload)}"
    result = await async_json_loads(payload)
    assert result["data"] == inner


@pytest.mark.asyncio
async def test_async_json_loads_invalid_json():
    """Invalid JSON raises json.JSONDecodeError regardless of payload size."""
    with pytest.raises(json.JSONDecodeError):
        await async_json_loads("{invalid json}")
    large_truncated = "{" + "x" * 9000
    with pytest.raises(json.JSONDecodeError):
        await async_json_loads(large_truncated)


@pytest.mark.asyncio
async def test_async_json_loads_empty_string():
    """Empty string raises JSONDecodeError."""
    with pytest.raises(json.JSONDecodeError):
        await async_json_loads("")


@pytest.mark.asyncio
async def test_async_json_loads_array():
    """Top-level JSON array also works."""
    result = await async_json_loads('[1, 2, 3]')
    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_json_loads_large_array():
    """Large array offloads correctly."""
    large_arr = list(range(2000))
    text = json.dumps(large_arr)
    assert len(text) > 8192, f"need larger array, got {len(text)} bytes"
    result = await async_json_loads(text)
    assert result == large_arr
