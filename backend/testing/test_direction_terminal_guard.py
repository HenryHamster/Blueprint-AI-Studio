from app.direction_terminal_guard import (
    direction_has_attempt_evidence,
    direction_success_evidence_met,
    mark_auto_direction_terminal,
    validate_direction_terminal_transition,
)


def test_direction_attempt_evidence_accepts_cycle_or_history():
    assert direction_has_attempt_evidence({"total_cycles": 1})
    assert direction_has_attempt_evidence({"research_history": [{"cycle": 1}]})
    assert not direction_has_attempt_evidence({"total_cycles": 0, "prs_merged": 0})


def test_attempted_direction_requires_outcome():
    assert validate_direction_terminal_transition(
        {"total_cycles": 1}, rationale="", terminal_reason=None
    ) == (False, "attempted_direction_requires_terminal_outcome")
    assert validate_direction_terminal_transition(
        {"total_cycles": 1}, rationale="No lift after the trial.", terminal_reason=None
    ) == (True, None)


def test_zero_attempt_direction_requires_typed_reason_and_rationale():
    assert validate_direction_terminal_transition(
        {"total_cycles": 0}, rationale="Never started.", terminal_reason=None
    ) == (False, "zero_attempt_direction_requires_typed_terminal_reason")
    assert validate_direction_terminal_transition(
        {"total_cycles": 0},
        rationale="Duplicates d-existing.",
        terminal_reason="duplicate",
    ) == (True, None)


def test_success_requires_declared_evidence_and_challenge_approved_met_verdict():
    base = {
        "context": {"success_evidence": "The live probe passes twice."},
        "evidence_progress": {"status": "met", "challenge_approved": True},
    }
    assert direction_success_evidence_met(base)
    assert not direction_success_evidence_met(
        {**base, "context": {"success_evidence": ""}}
    )
    assert not direction_success_evidence_met(
        {
            **base,
            "evidence_progress": {"status": "met", "challenge_approved": False},
        }
    )
    assert not direction_success_evidence_met(
        {
            **base,
            "evidence_progress": {
                "status": "advancing",
                "challenge_approved": True,
            },
        }
    )


def test_auto_terminal_success_and_exhaustion_have_distinct_status_and_outcome():
    successful = {}
    mark_auto_direction_terminal(
        successful,
        outcome="success_evidence_met",
        reason="declared evidence passed",
        completed_at="2026-07-31T00:00:00",
    )
    assert successful["status"] == "completed"
    assert successful["retirement_outcome"] == "success_evidence_met"

    exhausted = {}
    mark_auto_direction_terminal(
        exhausted,
        outcome="exhausted",
        reason="no work remains without proving the outcome",
        completed_at="2026-07-31T00:00:00",
    )
    assert exhausted["status"] == "retired"
    assert exhausted["retirement_outcome"] == "exhausted"
