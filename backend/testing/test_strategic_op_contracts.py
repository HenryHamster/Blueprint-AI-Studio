"""Focused tests for Strategic operation envelopes and compatibility forms."""

import pytest
from fastapi import HTTPException

from app.strategic_op_contracts import (
    normalize_problem_model_envelope,
    normalize_update_direction,
)


def test_update_direction_accepts_canonical_direct_fields():
    assert normalize_update_direction(
        {"direction_id": "d-1", "priority": "high", "gap_id": "g-1"}
    ) == {"direction_id": "d-1", "priority": "high", "gap_id": "g-1"}


def test_update_direction_normalizes_legacy_changes_wrapper():
    assert normalize_update_direction(
        {
            "direction_id": "d-1",
            "rationale": "new evidence",
            "changes": {"priority": "high"},
        }
    ) == {
        "direction_id": "d-1",
        "rationale": "new evidence",
        "priority": "high",
    }


def test_update_direction_rejects_mixed_forms_and_unknown_fields():
    with pytest.raises(HTTPException) as mixed:
        normalize_update_direction(
            {"direction_id": "d-1", "priority": "high", "changes": {"status": "paused"}}
        )
    assert mixed.value.status_code == 400
    assert mixed.value.detail["field"] == "changes"

    with pytest.raises(HTTPException) as unknown:
        normalize_update_direction({"direction_id": "d-1", "surprise": True})
    assert unknown.value.detail["field"] == "surprise"


def test_patch_problem_model_preserves_omitted_nested_fields_and_checks_revision():
    data, dry_run, warning = normalize_problem_model_envelope(
        "patch_problem_model",
        {
            "base_revision": 3,
            "patch": {"intent": {"context": "fresh"}, "current_focus": "test"},
            "dry_run": True,
        },
        current_revision=3,
        current_model={"revision": 3, "intent": {"purpose": "keep", "context": "old"}},
    )
    assert data["intent"] == {"purpose": "keep", "context": "fresh"}
    assert data["current_focus"] == "test"
    assert dry_run is True
    assert warning is None

    with pytest.raises(HTTPException) as conflict:
        normalize_problem_model_envelope(
            "patch_problem_model",
            {"base_revision": 2, "patch": {}},
            current_revision=3,
            current_model={},
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "problem_model_revision_conflict"


def test_replace_requires_explicit_confirmation_and_legacy_alias_is_safe_patch():
    with pytest.raises(HTTPException) as rejected:
        normalize_problem_model_envelope(
            "replace_problem_model",
            {"base_revision": 1, "model": {}},
            current_revision=1,
            current_model={},
        )
    assert rejected.value.detail["field"] == "confirm_replace"

    legacy, _, warning = normalize_problem_model_envelope(
        "set_problem_model",
        {"current_focus": "new"},
        current_revision=1,
        current_model={"revision": 1, "intent": {"purpose": "keep"}},
    )
    assert legacy["intent"] == {"purpose": "keep"}
    assert warning and "deprecated" in warning


def test_replace_translates_omitted_collection_members_to_explicit_deletions():
    model, _, _ = normalize_problem_model_envelope(
        "replace_problem_model",
        {"base_revision": 2, "model": {"gaps": [{"id": "g-keep"}]}, "confirm_replace": True},
        current_revision=2,
        current_model={
            "revision": 2,
            "gaps": [{"id": "g-keep"}, {"id": "g-remove"}],
            "capabilities": [{"name": "old"}],
        },
    )
    assert model["_deletions"]["gaps"] == ["g-remove"]
    assert model["_deletions"]["capabilities"] == ["old"]
