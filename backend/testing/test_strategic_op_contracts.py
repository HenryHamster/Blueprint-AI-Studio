"""Focused tests for Strategic operation envelopes and compatibility forms."""

import pytest
from fastapi import HTTPException

from app.strategic_op_contracts import (
    normalize_create_direction,
    normalize_problem_model_envelope,
    normalize_update_direction,
)


def test_create_direction_requires_durable_outcome_contract_and_normalizes_text():
    assert normalize_create_direction(
        {
            "text": "  Deliver a reproducible evaluation artifact. ",
            "success_evidence": " Clean-clone run produces validated output. ",
            "retirement_condition": " Artifact is merged or the bet is invalidated. ",
            "priority": "high",
            "staged": False,
            "conflicts_with": ["d-other"],
        }
    ) == {
        "text": "Deliver a reproducible evaluation artifact.",
        "success_evidence": "Clean-clone run produces validated output.",
        "retirement_condition": "Artifact is merged or the bet is invalidated.",
        "priority": "high",
        "staged": False,
        "conflicts_with": ["d-other"],
    }


def test_create_direction_accepts_existing_mixed_category():
    normalized = normalize_create_direction(
        {
            "text": "Unify product and structural reliability outcomes",
            "success_evidence": "The combined outcome is observable",
            "retirement_condition": "The combined gap is closed",
            "category": "mixed",
        }
    )
    assert normalized["category"] == "mixed"


def test_direction_cost_budget_normalization_accepts_zero_and_update_clear():
    created = normalize_create_direction(
        {
            "text": "Keep campaign spend bounded",
            "success_evidence": "Spend remains inside the declared envelope",
            "retirement_condition": "The campaign lands or the bet is retired",
            "cost_budget_usd": 0,
        }
    )
    assert created["cost_budget_usd"] == 0.0

    assert normalize_update_direction(
        {"direction_id": "d-budget", "cost_budget_usd": 12.5}
    )["cost_budget_usd"] == 12.5
    assert normalize_update_direction(
        {"direction_id": "d-budget", "changes": {"cost_budget_usd": None}}
    )["cost_budget_usd"] is None


@pytest.mark.parametrize(
    "value", [True, "12.5", -1, float("nan"), float("inf"), 10**400]
)
def test_direction_cost_budget_rejects_non_numeric_or_negative_values(value):
    create = {
        "text": "Keep campaign spend bounded",
        "success_evidence": "Spend remains inside the declared envelope",
        "retirement_condition": "The campaign lands or the bet is retired",
        "cost_budget_usd": value,
    }
    for normalizer, payload in (
        (normalize_create_direction, create),
        (normalize_update_direction, {"direction_id": "d-budget", "cost_budget_usd": value}),
    ):
        with pytest.raises(HTTPException) as rejected:
            normalizer(payload)
        assert rejected.value.status_code == 400
        assert rejected.value.detail["code"] == "invalid_strategic_input"
        assert rejected.value.detail["field"] == "cost_budget_usd"


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({}, "text"),
        (
            {
                "text": "outcome",
                "success_evidence": "",
                "retirement_condition": "done",
            },
            "success_evidence",
        ),
        (
            {
                "text": "outcome",
                "success_evidence": "observed",
                "retirement_condition": "done",
                "staged": "yes",
            },
            "staged",
        ),
        (
            {
                "text": "outcome",
                "success_evidence": "observed",
                "retirement_condition": "done",
                "surprise": True,
            },
            "surprise",
        ),
    ],
)
def test_create_direction_returns_stable_structural_errors(data, field):
    with pytest.raises(HTTPException) as rejected:
        normalize_create_direction(data)
    assert rejected.value.status_code == 400
    assert rejected.value.detail["code"] == "invalid_strategic_input"
    assert rejected.value.detail["field"] == field


def test_update_direction_accepts_canonical_direct_fields():
    assert normalize_update_direction(
        {"direction_id": "d-1", "priority": "high", "gap_id": "g-1"}
    ) == {"direction_id": "d-1", "priority": "high", "gap_id": "g-1"}


def test_update_direction_normalizes_legacy_changes_wrapper():
    assert normalize_update_direction(
        {
            "direction_id": "d-1",
            "rationale": "new evidence",
            "terminal_reason": "superseded",
            "changes": {"priority": "high"},
        }
    ) == {
        "direction_id": "d-1",
        "rationale": "new evidence",
        "terminal_reason": "superseded",
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
