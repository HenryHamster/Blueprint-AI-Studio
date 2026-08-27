"""Narrow, backend-owned contracts for high-risk Strategic operations.

The strategic endpoint predates a schema surface and intentionally supports a
large vocabulary.  This module starts with the operations whose undocumented
shape or destructive semantics have already caused real operator friction.
It is deliberately small: individual handlers still own their domain rules,
while these contracts own envelope validation and canonical compatibility
normalisation.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from fastapi import HTTPException


UPDATE_DIRECTION_FIELDS = frozenset(
    {
        "direction_id",
        "text",
        "priority",
        "status",
        "gap_id",
        "success_evidence",
        "retirement_condition",
        "implementation_constraints",
        "rationale",
        "terminal_reason",
        "dedup_reasoning",
        "criterion_keys",
        "recommended_mode",
        "system_id",
        "superseded_by",
        "successor_id",
        "superseded_by_id",
        "conflicts_with",
        "cost_budget_usd",
    }
)

# These describe the update rather than direction fields.  The legacy nested
# envelope has always allowed them beside ``changes``; retain that compatibility
# without treating them as a second direct-field update form.
UPDATE_DIRECTION_METADATA_FIELDS = frozenset(
    {
        "rationale",
        "terminal_reason",
        "dedup_reasoning",
        "successor_id",
        "superseded_by_id",
    }
)

CREATE_DIRECTION_FIELDS = frozenset(
    {
        "text",
        "success_evidence",
        "retirement_condition",
        "rationale",
        "priority",
        "gap_id",
        "implementation_constraints",
        "criterion_keys",
        "recommended_mode",
        "system_id",
        "conflicts_with",
        "staged",
        "dedup_reasoning",
        "cost_budget_usd",
        # Retained compatibility fields. New callers should keep file routing
        # and implementation decomposition in spawn_task instead.
        "category",
        "paths",
    }
)

PROBLEM_MODEL_PATCH_FIELDS = frozenset(
    {"base_revision", "patch", "transient_actions", "dry_run"}
)
PROBLEM_MODEL_REPLACE_FIELDS = frozenset(
    {"base_revision", "model", "confirm_replace", "dry_run"}
)


def _field_error(field: str, message: str, *, status_code: int = 400) -> HTTPException:
    """Produce a stable structured error rather than an opaque handler error."""
    return HTTPException(
        status_code=status_code,
        detail={"code": "invalid_strategic_input", "field": field, "message": message},
    )


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _field_error(field, "must be an object")
    return dict(value)


def _check_unknown(data: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _field_error(
            unknown[0],
            f"unknown field(s): {', '.join(unknown)}; accepted fields: {', '.join(sorted(allowed))}",
        )


def _normalize_cost_budget_usd(value: Any, *, allow_null: bool) -> float | None:
    """Return a finite non-negative USD budget, with update-only null clears."""
    if value is None:
        if allow_null:
            return None
        raise _field_error("cost_budget_usd", "must be a non-negative number")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _field_error("cost_budget_usd", "must be a non-negative number")
    try:
        normalized = float(value)
    except OverflowError:
        raise _field_error(
            "cost_budget_usd", "must be a finite non-negative number"
        ) from None
    if not isfinite(normalized) or normalized < 0:
        raise _field_error("cost_budget_usd", "must be a finite non-negative number")
    return normalized


def normalize_create_direction(data: Any) -> dict[str, Any]:
    """Validate and normalize the structural create-direction envelope.

    This intentionally does not attempt to judge whether prose is "strategic".
    The prompt/challenge own that semantic judgment; this boundary owns stable
    errors, accepted fields, and unambiguous scalar/list shapes.
    """
    raw = _object(data, "data")
    _check_unknown(raw, CREATE_DIRECTION_FIELDS)
    for field in ("text", "success_evidence", "retirement_condition"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _field_error(field, "is required and must be a non-empty string")
        raw[field] = value.strip()

    for field in (
        "rationale",
        "gap_id",
        "implementation_constraints",
        "recommended_mode",
        "system_id",
    ):
        if field in raw and raw[field] is not None and not isinstance(raw[field], str):
            raise _field_error(field, "must be a string")

    if "priority" in raw and raw["priority"] not in {"high", "medium", "low"}:
        raise _field_error("priority", "must be one of: high, medium, low")
    if "staged" in raw and not isinstance(raw["staged"], bool):
        raise _field_error("staged", "must be a boolean")
    if "category" in raw and raw["category"] not in {
        "product",
        "structural",
        "mixed",
    }:
        raise _field_error(
            "category", "must be one of: product, structural, mixed"
        )

    for field in ("criterion_keys", "conflicts_with", "paths"):
        if field not in raw:
            continue
        value = raw[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _field_error(field, "must be a list of strings")
    if "dedup_reasoning" in raw and not isinstance(raw["dedup_reasoning"], Mapping):
        raise _field_error("dedup_reasoning", "must be an object")
    if "cost_budget_usd" in raw:
        raw["cost_budget_usd"] = _normalize_cost_budget_usd(
            raw["cost_budget_usd"], allow_null=False
        )
    return raw


def normalize_update_direction(data: Any) -> dict[str, Any]:
    """Return the canonical direct-field update-direction form.

    The previous ``{direction_id, changes: {...}}`` envelope is retained as a
    one-release adapter.  Mixing direct fields and ``changes`` is rejected so
    callers cannot accidentally submit contradictory requests.
    """
    raw = _object(data, "data")
    if "direction_id" not in raw or not isinstance(raw["direction_id"], str) or not raw["direction_id"].strip():
        raise _field_error("direction_id", "is required and must be a non-empty string")

    has_changes = "changes" in raw
    direct_fields = set(raw) - {"direction_id", "changes"} - UPDATE_DIRECTION_METADATA_FIELDS
    if has_changes and direct_fields:
        raise _field_error("changes", "cannot be mixed with direct update fields")

    if has_changes:
        changes = _object(raw["changes"], "changes")
        _check_unknown(changes, UPDATE_DIRECTION_FIELDS - {"direction_id"})
        _check_unknown(
            raw,
            UPDATE_DIRECTION_FIELDS | UPDATE_DIRECTION_METADATA_FIELDS | {"changes"},
        )
        metadata = {
            key: raw[key]
            for key in UPDATE_DIRECTION_METADATA_FIELDS
            if key in raw
        }
        normalized = {"direction_id": raw["direction_id"], **metadata, **changes}
    else:
        _check_unknown(raw, UPDATE_DIRECTION_FIELDS)
        normalized = raw

    if "cost_budget_usd" in normalized:
        normalized["cost_budget_usd"] = _normalize_cost_budget_usd(
            normalized["cost_budget_usd"], allow_null=True
        )
    return normalized


def normalize_problem_model_envelope(
    op: str,
    data: Any,
    *,
    current_revision: int,
    current_model: dict[str, Any],
) -> tuple[dict[str, Any], bool, str | None]:
    """Normalize patch/replace operations into the legacy handler's model data.

    Returns ``(model_data, dry_run, warning)``.  The handler keeps ownership of
    merge, persistence, and transient action application, so existing safety
    logic remains in one place.
    """
    if op == "set_problem_model":
        legacy = _object(data, "data")
        return _deep_merge(current_model, legacy), False, (
            "set-problem-model is deprecated; it now uses safe patch semantics. "
            "Use patch-problem-model with base_revision for new calls."
        )

    raw = _object(data, "data")
    if op == "patch_problem_model":
        _check_unknown(raw, PROBLEM_MODEL_PATCH_FIELDS)
        required = ("base_revision", "patch")
        model_key = "patch"
    elif op == "replace_problem_model":
        _check_unknown(raw, PROBLEM_MODEL_REPLACE_FIELDS)
        required = ("base_revision", "model", "confirm_replace")
        model_key = "model"
        if raw.get("confirm_replace") is not True:
            raise _field_error("confirm_replace", "must be true for replace-problem-model")
    else:  # Defensive: callers should only route the three supported operations.
        raise _field_error("op", f"unsupported problem-model operation: {op}")

    for field in required:
        if field not in raw:
            raise _field_error(field, "is required")
    base_revision = raw["base_revision"]
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise _field_error("base_revision", "must be an integer")
    if base_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "problem_model_revision_conflict",
                "expected_revision": base_revision,
                "current_revision": current_revision,
                "message": "problem model changed; refetch and retry with the current revision",
            },
        )
    model = _object(raw[model_key], model_key)
    transient = raw.get("transient_actions", {})
    if transient is not None:
        transient = _object(transient, "transient_actions")
    dry_run = raw.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _field_error("dry_run", "must be a boolean")

    if op == "patch_problem_model":
        merged = _deep_merge(current_model, model)
    else:
        # Keep apply_agent_update as the one persistence path, but make the
        # confirmed replacement semantically whole-model by translating omitted
        # durable collection entries into that function's explicit deletion
        # directive.  Its protected-gap and reference-integrity guards still
        # apply; replace does not gain a bypass around those safety contracts.
        merged = _replacement_with_explicit_deletions(current_model, model)
    if transient:
        collisions = set(merged) & set(transient)
        if collisions:
            raise _field_error(
                "transient_actions",
                f"must not duplicate model fields: {', '.join(sorted(collisions))}",
            )
        merged.update(transient)
    return merged, dry_run, None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively; arrays/scalars are deliberate replacements."""
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), dict(value))
        else:
            result[key] = value
    return result


def _replacement_with_explicit_deletions(
    current: dict[str, Any], replacement: dict[str, Any]
) -> dict[str, Any]:
    result = dict(replacement)
    existing_directive = result.get("_deletions")
    if existing_directive is not None and not isinstance(existing_directive, Mapping):
        raise _field_error("model._deletions", "must be an object when supplied")
    deletions = dict(existing_directive or {})
    for field, id_key in (
        ("gaps", "id"),
        ("capabilities", "name"),
        ("friction", "id"),
        ("pending_user_confirmation", "id"),
    ):
        prior_ids = {
            str(item[id_key])
            for item in (current.get(field) or [])
            if isinstance(item, Mapping) and item.get(id_key) is not None
        }
        replacement_ids = {
            str(item[id_key])
            for item in (result.get(field) or [])
            if isinstance(item, Mapping) and item.get(id_key) is not None
        }
        supplied = deletions.get(field) or []
        if not isinstance(supplied, list):
            raise _field_error(f"model._deletions.{field}", "must be a list")
        deletions[field] = sorted(
            (prior_ids - replacement_ids) | {str(item) for item in supplied}
        )
    result["_deletions"] = deletions
    return result
