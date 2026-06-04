"""Unit tests for ``summarize_running_update_reconciliation``.

Pure (no-DB) coverage for the read-only ledger summary that surfaces
operator-wins running_update reconciliations on the directions payload. The
mutating reconciliation logic itself is covered in test_strategic_completion.py.
"""

from app.running_update_merge import (
    RUNNING_UPDATE_REJECTED_KIND,
    summarize_running_update_reconciliation,
)
from app.vision_merge import REJECTED_EDITS_KEY


def _ru_entry(at: str) -> dict:
    return {
        "at": at,
        "field": "running_update",
        "kind": RUNNING_UPDATE_REJECTED_KIND,
        "agent_value": "AGENT SUPERSEDED DOC",
    }


def test_summary_none_for_none_vision():
    """No vision at all → no summary (UI omits the badge)."""
    assert summarize_running_update_reconciliation(None) is None


def test_summary_none_for_non_dict_vision():
    """A non-dict vision (legacy/garbage) degrades to None, not a crash."""
    assert summarize_running_update_reconciliation("not a dict") is None
    assert summarize_running_update_reconciliation([]) is None


def test_summary_none_when_no_ledger():
    """A vision with no rejected-edits ledger → None."""
    assert summarize_running_update_reconciliation({}) is None


def test_summary_none_when_ledger_has_no_running_update_entries():
    """Vision/problem-model field conflicts share the ledger; they must NOT be
    counted as running_update reconciliations."""
    vision = {
        REJECTED_EDITS_KEY: [
            {"at": "2026-06-01T00:00:00", "field": "purpose", "kind": "vision_field"},
            {"at": "2026-06-02T00:00:00", "field": "gaps", "kind": "problem_model"},
        ]
    }
    assert summarize_running_update_reconciliation(vision) is None


def test_summary_counts_running_update_entries_and_picks_latest():
    """N running_update_superseded entries → count N and the most recent ``at``,
    ignoring interleaved non-running-update ledger entries."""
    vision = {
        REJECTED_EDITS_KEY: [
            _ru_entry("2026-06-01T10:00:00"),
            {"at": "2026-06-01T11:00:00", "field": "purpose", "kind": "vision_field"},
            _ru_entry("2026-06-03T09:30:00"),
            _ru_entry("2026-06-02T08:00:00"),
        ]
    }
    summary = summarize_running_update_reconciliation(vision)
    assert summary == {
        "operator_wins_count": 3,
        "last_operator_win_at": "2026-06-03T09:30:00",
    }


def test_summary_handles_missing_at_field():
    """An entry missing ``at`` is still counted; last_operator_win_at falls back
    to None when no entry carries a timestamp."""
    vision = {
        REJECTED_EDITS_KEY: [
            {"field": "running_update", "kind": RUNNING_UPDATE_REJECTED_KIND},
        ]
    }
    summary = summarize_running_update_reconciliation(vision)
    assert summary == {"operator_wins_count": 1, "last_operator_win_at": None}


def test_summary_ignores_non_dict_ledger_entries():
    """Malformed (non-dict) ledger entries are skipped, not fatal."""
    vision = {
        REJECTED_EDITS_KEY: [
            "garbage",
            None,
            _ru_entry("2026-06-04T12:00:00"),
        ]
    }
    summary = summarize_running_update_reconciliation(vision)
    assert summary == {
        "operator_wins_count": 1,
        "last_operator_win_at": "2026-06-04T12:00:00",
    }


def test_summary_none_when_ledger_not_a_list():
    """A corrupt non-list ledger value degrades to None."""
    assert summarize_running_update_reconciliation({REJECTED_EDITS_KEY: "oops"}) is None
