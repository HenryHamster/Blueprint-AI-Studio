from app.direction_terminal_guard import (
    direction_has_attempt_evidence,
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
