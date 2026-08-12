"""Invariant guards for the explicit trusted-CI prepromotion contract.

The gate is version-scoped and metadata-selected. It must never depend on a
hard-coded criterion key, fuzzy prose, or the retired Hamsterwheel rehearsal
receipt shape.
"""

from app.release_prepromotion_rehearsal import (
    PREPROMOTION_REHEARSAL_CHECKS,
    PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
    PREPROMOTION_REHEARSAL_DEFERRED,
    PREPROMOTION_REHEARSAL_KIND,
    apply_rehearsal_evidence,
    normalize_rehearsal_evidence_contract,
    rubric_requires_rehearsal_contract,
    validate_prepromotion_gradeability,
)


_CANDIDATE = "a" * 40
_CONTRACT = {
    "type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
    "predicate": {"required": True},
}
_CATEGORY = {
    "criterion_key": "c-deadbeef",
    "text": "Trusted CI must build and smoke the candidate",
    "evidence_contract": _CONTRACT,
}


def _receipt() -> dict:
    return {
        "kind": PREPROMOTION_REHEARSAL_KIND,
        "schema_version": 1,
        "source": "github_actions",
        "project_id": "11111111-1111-4111-8111-111111111111",
        "release_version_id": "22222222-2222-4222-8222-222222222222",
        "request_id": "33333333-3333-4333-8333-333333333333",
        "candidate_sha": _CANDIDATE,
        "repository": "acme/widget",
        "workflow_file": "prepromotion-rehearsal.yml",
        "workflow_ref": "main",
        "workflow_head_sha": "b" * 40,
        "run_id": 42,
        "run_attempt": 1,
        "result": "passed",
        "checks": list(PREPROMOTION_REHEARSAL_CHECKS),
        "deferred": list(PREPROMOTION_REHEARSAL_DEFERRED),
        "completed_at": "2026-08-11T12:00:00+00:00",
    }


def test_rehearsal_gate_requires_the_closed_structured_contract():
    assert normalize_rehearsal_evidence_contract(_CONTRACT) == _CONTRACT
    assert normalize_rehearsal_evidence_contract(None) is None
    assert normalize_rehearsal_evidence_contract({"type": "host_rehearsal"}) is None
    assert normalize_rehearsal_evidence_contract(
        {"type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE, "predicate": {"required": False}}
    ) is None


def test_rubric_opt_in_is_metadata_driven_not_text_or_fixed_key():
    assert rubric_requires_rehearsal_contract([_CATEGORY]) is True
    assert rubric_requires_rehearsal_contract(
        [{"criterion_key": "c-cafe3815", "text": _CATEGORY["text"]}]
    ) is False


def test_machine_receipt_grades_the_explicit_active_key_without_grader_seed():
    result = apply_rehearsal_evidence([], [_CATEGORY], _receipt(), _CANDIDATE)
    assert result is not None
    assert len(result) == 1
    assert result[0]["category_key"] == "c-deadbeef"
    assert result[0]["status"] == "met"
    assert result[0]["tier"] == "machine"


def test_no_active_contracted_category_means_no_machine_verdict():
    assert apply_rehearsal_evidence([], [], _receipt(), _CANDIDATE) == []


def test_structured_gate_is_pregradeable_and_circular_override_fails_closed():
    validate_prepromotion_gradeability([_CATEGORY])
    try:
        validate_prepromotion_gradeability(
            [_CATEGORY],
            contracts={
                "c-deadbeef": {
                    "producer": "late_rehearsal",
                    "producer_phase": "promotion_gate",
                    "gate_phase": "promotion_gate",
                }
            },
        )
    except ValueError as exc:
        assert "at or after its promotion gate" in str(exc)
    else:
        raise AssertionError("circular evidence producer must fail closed")
