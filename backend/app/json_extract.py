"""Bracket-depth JSON extraction utilities.

String-aware O(n) scanners for extracting balanced JSON arrays and objects from
text that may contain surrounding prose or log output.  Lives here so
task_completion, task_completion_self_audit, research_completion, and
task_evaluators can all share the same implementations without circular imports.
"""

import asyncio
import json
from typing import Any, Optional


_ASYNC_JSON_THRESHOLD = 8192


async def async_json_loads(text: str) -> Any:
    """Parse JSON, offloading large payloads to a thread to avoid blocking the event loop.

    Uses a heuristic threshold (``_ASYNC_JSON_THRESHOLD`` bytes) — small strings parse
    synchronously (negligible blocking), large strings parse via ``asyncio.to_thread``
    so CPU-bound :func:`json.loads` doesn't stall the async reactor.
    """
    if len(text) > _ASYNC_JSON_THRESHOLD:
        return await asyncio.to_thread(json.loads, text)
    return json.loads(text)


def _extract_json_array(text: str, *required_keys: str) -> Optional[str]:
    """Find the first JSON array in text containing objects with the required keys.

    Uses bracket-depth counting (O(n)) instead of regex with greedy groups,
    which can cause O(n*k) backtracking on malformed input and block the
    async event loop for seconds.
    """
    # Cap input to prevent scanning megabytes of raw output
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    # Find outermost [ ... ] containing the required keys
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        return candidate
                    break  # This bracket pair didn't match, try next [
        start = text.find("[", start + 1)
    return None


def _extract_last_json_array(text: str, *required_keys: str) -> Optional[str]:
    """Find the LAST JSON array in text containing objects with the required keys.

    Identical scan logic to _extract_json_array but collects all matching spans
    and returns the last one. Used by parse_adversarial_finding_verdicts so that
    an echoed schema/example array that appears before the real final verdict
    array does not shadow the intended output.
    """
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    last_match: Optional[str] = None
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        last_match = candidate
                    break
        start = text.find("[", start + 1)
    return last_match


def _extract_json_object(text: str, *required_keys: str) -> Optional[str]:
    """Find the first balanced JSON object in ``text`` containing ``required_keys``."""
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if all(f'"{key}"' in candidate for key in required_keys):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def _iter_balanced_json_objects(text: str, *, required_key: str | None = None):
    """Yield, in order of appearance, each balanced JSON object in ``text`` that
    parses to a dict and (when ``required_key`` is given) contains that key.

    Shared string-aware scanner behind :func:`scan_balanced_json_objects` and
    :func:`scan_all_balanced_json_objects` so both agree on exactly which
    objects are recoverable.
    """
    idx = text.find("{")
    while idx != -1:
        depth = 0
        in_string = False
        escape_next = False
        for j in range(idx, len(text)):
            c = text[j]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[idx : j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and (
                            required_key is None or required_key in obj
                        ):
                            yield obj
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        idx = text.find("{", idx + 1)


def scan_balanced_json_objects(
    text: str, *, required_key: str | None = None, last: bool = False
) -> dict | None:
    """Find the first (or last when ``last=True``) balanced JSON object in
    ``text`` that parses to a dict and, when ``required_key`` is given,
    contains that key.  Returns ``None`` when no matching object is found.
    """
    result: dict | None = None
    for obj in _iter_balanced_json_objects(text, required_key=required_key):
        if not last:
            return obj
        result = obj
    return result


def scan_all_balanced_json_objects(
    text: str, *, required_key: str | None = None
) -> list[dict]:
    """Return every balanced JSON object in ``text`` (in order of appearance)
    that parses to a dict and, when ``required_key`` is given, contains that key.

    Unlike :func:`scan_balanced_json_objects`, which collapses to a single
    (first/last) object, this preserves every emission — needed to recover an
    A/B experiment that printed the same metric key once per arm, where
    last-object-wins would otherwise discard every arm but the final one.
    """
    return list(_iter_balanced_json_objects(text, required_key=required_key))
