"""Unit tests for the _fix_loop_summary helper in backend/app/admin.py."""
import pytest

from app.admin import _fix_loop_summary


def _attempt(exit_reason: str, iteration: int = 1) -> dict:
    return {
        "loop": "lint",
        "iteration": iteration,
        "fingerprint_hash": "abc123",
        "error_tail": "err",
        "change_summary": "+1 -0",
        "exit_reason": exit_reason,
        "source": "agent_emitted",
        "started_at": "2026-05-05T00:00:00",
        "ended_at": "2026-05-05T00:00:10",
    }


# --- None / omit cases -------------------------------------------------------

def test_no_history_returns_none():
    assert _fix_loop_summary(None) is None


def test_empty_history_returns_none():
    assert _fix_loop_summary({}) is None


def test_healthy_one_iteration_passed_returns_none():
    history = {"lint": [_attempt("passed", 1)]}
    assert _fix_loop_summary(history) is None


def test_healthy_two_iterations_passed_returns_none():
    history = {"lint": [_attempt("in_progress", 1), _attempt("passed", 2)]}
    assert _fix_loop_summary(history) is None


def test_early_unchanged_treated_as_success():
    history = {"lint": [_attempt("early_unchanged", 1)]}
    assert _fix_loop_summary(history) is None


def test_multiple_loops_all_healthy_returns_none():
    # lint passes in 2, test passes in 1 — no flags
    history = {
        "lint": [_attempt("in_progress", 1), _attempt("passed", 2)],
        "test": [_attempt("passed", 1)],
    }
    assert _fix_loop_summary(history) is None


# --- flagged cases -----------------------------------------------------------

def test_divergence_oscillation_is_flagged():
    history = {"lint": [_attempt("divergence_oscillation", 1)]}
    result = _fix_loop_summary(history)
    assert result is not None
    assert len(result) == 1
    entry = result[0]
    assert entry["loop"] == "lint"
    assert entry["iterations"] == 1
    assert entry["last_exit_reason"] == "divergence_oscillation"
    assert entry["non_success"] is True


def test_max_attempts_is_flagged():
    history = {"lint": [_attempt("max_attempts", 1)]}
    result = _fix_loop_summary(history)
    assert result is not None
    assert result[0]["non_success"] is True
    assert result[0]["last_exit_reason"] == "max_attempts"


def test_runaway_detected_is_flagged():
    history = {"lint": [_attempt("runaway_detected", 1)]}
    result = _fix_loop_summary(history)
    assert result is not None
    assert result[0]["non_success"] is True


def test_high_iteration_count_lint_flagged_even_when_passed():
    # lint budget is 5, flag if iterations > 2.5 (i.e. >= 3)
    attempts = [_attempt("in_progress", i) for i in range(1, 4)]
    attempts[-1]["exit_reason"] = "passed"
    history = {"lint": attempts}
    result = _fix_loop_summary(history)
    assert result is not None
    assert result[0]["iterations"] == 3
    assert result[0]["non_success"] is False


def test_lint_two_iterations_passed_not_flagged():
    # Exactly 2 iterations is below the threshold (> 5/2 = 2.5), so not flagged
    attempts = [_attempt("in_progress", 1), _attempt("passed", 2)]
    history = {"lint": attempts}
    assert _fix_loop_summary(history) is None


def test_high_iteration_count_test_flagged_when_passed():
    # test budget is 3, flag if iterations > 1.5 (i.e. >= 2)
    attempts = [_attempt("in_progress", 1), _attempt("passed", 2)]
    history = {"test": attempts}
    result = _fix_loop_summary(history)
    assert result is not None
    assert result[0]["loop"] == "test"
    assert result[0]["iterations"] == 2
    assert result[0]["non_success"] is False


def test_multiple_troubled_loops_returned():
    history = {
        "lint": [_attempt("divergence_oscillation", 1)],
        "test": [_attempt("max_attempts", 1)],
    }
    result = _fix_loop_summary(history)
    assert result is not None
    assert len(result) == 2
    loop_names = {e["loop"] for e in result}
    assert loop_names == {"lint", "test"}


def test_only_troubled_loop_included_not_healthy():
    history = {
        "lint": [_attempt("passed", 1)],
        "test": [_attempt("divergence_detected", 1)],
    }
    result = _fix_loop_summary(history)
    assert result is not None
    assert len(result) == 1
    assert result[0]["loop"] == "test"


def test_five_iterations_lint_flagged():
    # Matches the '5/5 iterations' example from the task spec
    attempts = [_attempt("in_progress", i) for i in range(1, 6)]
    attempts[-1]["exit_reason"] = "passed"
    history = {"lint": attempts}
    result = _fix_loop_summary(history)
    assert result is not None
    assert result[0]["iterations"] == 5


def test_summary_fields_are_compact():
    # Ensure error_tail / change_summary / fingerprint_hash are NOT in summary
    history = {"lint": [_attempt("max_attempts", 1)]}
    result = _fix_loop_summary(history)
    assert result is not None
    entry = result[0]
    assert set(entry.keys()) == {"loop", "iterations", "last_exit_reason", "non_success"}
