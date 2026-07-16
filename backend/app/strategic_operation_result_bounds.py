"""Bound and normalize structured Strategic Resident operation results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.strategic_operation_authority_validation import (
    BoundedOperationResult,
    OperationResultTooLarge,
)


_RESULT_MAX_BYTES = 64_000
_RESULT_LIST_MAX = 50
_RESULT_TEXT_MAX = 4_000
_RESULT_MAX_DEPTH = 8
_RESULT_NODE_MAX = 5_000


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _bounded_text(value: Any) -> str:
    return str(value)[:_RESULT_TEXT_MAX]


def _bounded_record(
    record: Mapping[str, Any],
    *,
    depth: int = 0,
    node_budget: list[int] | None = None,
    ancestors: set[int] | None = None,
) -> dict[str, Any]:
    if depth >= _RESULT_MAX_DEPTH:
        return {}
    if node_budget is None:
        node_budget = [_RESULT_NODE_MAX]
    if ancestors is None:
        ancestors = set()
    identity = id(record)
    if identity in ancestors:
        raise OperationResultTooLarge("operation result contains a reference cycle")
    ancestors.add(identity)
    bounded: dict[str, Any] = {}
    try:
        for key, value in list(record.items())[:30]:
            node_budget[0] -= 1
            if node_budget[0] < 0:
                raise OperationResultTooLarge(
                    "operation result exceeds the node budget"
                )
            out_key = str(key)[:120]
            if isinstance(value, str):
                bounded[out_key] = value[:_RESULT_TEXT_MAX]
            elif value is None or isinstance(value, (bool, int, float)):
                bounded[out_key] = value
            elif isinstance(value, list):
                list_identity = id(value)
                if list_identity in ancestors:
                    raise OperationResultTooLarge(
                        "operation result contains a reference cycle"
                    )
                ancestors.add(list_identity)
                try:
                    items: list[Any] = []
                    for item in value[:20]:
                        node_budget[0] -= 1
                        if node_budget[0] < 0:
                            raise OperationResultTooLarge(
                                "operation result exceeds the node budget"
                            )
                        items.append(
                            _bounded_text(item)
                            if not isinstance(item, Mapping)
                            else _bounded_record(
                                item,
                                depth=depth + 1,
                                node_budget=node_budget,
                                ancestors=ancestors,
                            )
                        )
                    bounded[out_key] = items
                finally:
                    ancestors.remove(list_identity)
            elif isinstance(value, Mapping):
                bounded[out_key] = _bounded_record(
                    value,
                    depth=depth + 1,
                    node_budget=node_budget,
                    ancestors=ancestors,
                )
            else:
                bounded[out_key] = _bounded_text(value)
    finally:
        ancestors.remove(identity)
    return bounded


def _bounded_text_items(
    values: Any,
    *,
    node_budget: list[int],
) -> list[str]:
    bounded: list[str] = []
    for item in list(values or [])[:_RESULT_LIST_MAX]:
        node_budget[0] -= 1
        if node_budget[0] < 0:
            raise OperationResultTooLarge("operation result exceeds the node budget")
        bounded.append(_bounded_text(item))
    return bounded


def bounded_result(
    value: BoundedOperationResult | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the durable bounded projection for a callback result."""

    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    node_budget = [_RESULT_NODE_MAX]
    ancestors: set[int] = set()
    result = {
        "facts": [
            _bounded_record(item, node_budget=node_budget, ancestors=ancestors)
            for item in list(raw.get("facts") or [])[:_RESULT_LIST_MAX]
            if isinstance(item, Mapping)
        ],
        "evidence": [
            _bounded_record(item, node_budget=node_budget, ancestors=ancestors)
            for item in list(raw.get("evidence") or [])[:_RESULT_LIST_MAX]
            if isinstance(item, Mapping)
        ],
        "operations": [
            _bounded_record(item, node_budget=node_budget, ancestors=ancestors)
            for item in list(raw.get("operations") or [])[:_RESULT_LIST_MAX]
            if isinstance(item, Mapping)
        ],
        "strategic_implications": _bounded_text_items(
            raw.get("strategic_implications"),
            node_budget=node_budget,
        ),
        "proposed_memory": [
            _bounded_record(item, node_budget=node_budget, ancestors=ancestors)
            for item in list(raw.get("proposed_memory") or [])[:_RESULT_LIST_MAX]
            if isinstance(item, Mapping)
        ],
        "follow_up": (
            _bounded_record(
                raw["follow_up"],
                node_budget=node_budget,
                ancestors=ancestors,
            )
            if isinstance(raw.get("follow_up"), Mapping)
            else None
        ),
        "required_attention": [
            _bounded_record(item, node_budget=node_budget, ancestors=ancestors)
            for item in list(raw.get("required_attention") or [])[:_RESULT_LIST_MAX]
            if isinstance(item, Mapping)
        ],
    }
    encoded = _canonical_json(result)
    if len(encoded) > _RESULT_MAX_BYTES:
        raise OperationResultTooLarge(
            f"operation result exceeds {_RESULT_MAX_BYTES} bytes",
            size_bytes=len(encoded),
        )
    return result
