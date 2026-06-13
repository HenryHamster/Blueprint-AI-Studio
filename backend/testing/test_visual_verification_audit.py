"""Tests for the visual-verification pilot kill-criterion audit helpers.

Covers aggregate_visual_pilot(), extract_visual_verification(),
touches_admin_ui(), and infra_occurrences() with fixture task dicts
representing all relevant cases:

- Tasks with a full visual_verification block (including caught_visual_issue=True)
- Tasks without the block (no self_review_outcome)
- Non-admin-ui tasks (must be ignored)
- Tasks with a VIEWPORT_GATE_INFRA_UNAVAILABLE signal
- Malformed (non-dict) visual_verification entries
- created_before window filtering
"""

from __future__ import annotations

import json

import pytest

from app.visual_verification_audit import (
    VisualPilotAggregates,
    aggregate_visual_pilot,
    changed_files,
    extract_visual_verification,
    infra_occurrences,
    intish,
    touches_admin_ui,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TASK_ADMIN_CAUGHT = {
    "id": "task-caught-aaa",
    "status": "COMPLETED",
    "created_at": "2026-06-01T10:00:00Z",
    "pr_url": "https://github.com/example/repo/pull/1",
    "context": {
        "_pr_changed_files": ["admin-ui/src/App.tsx"],
        "_self_review_outcome": {
            "fixes_applied": True,
            "visual_verification": {
                "passed": True,
                "pairs_inspected": 3,
                "pairs_changed": 2,
                "caught_visual_issue": True,
                "note": "caught a layout shift",
            },
        },
    },
}

_TASK_ADMIN_INFRA_FAIL = {
    "id": "task-infra-bbb",
    "status": "COMPLETED",
    "created_at": "2026-06-02T10:00:00Z",
    "context": {
        "_pr_changed_files": ["admin-ui/src/Button.tsx"],
        "_self_review_outcome": {
            "visual_verification": {
                "passed": False,
                "pairs_inspected": 0,
                "pairs_changed": 0,
                "caught_visual_issue": False,
                "failure_class": "VIEWPORT_GATE_INFRA_UNAVAILABLE",
            },
        },
    },
}

_TASK_ADMIN_NO_ENTRY = {
    "id": "task-no-entry-ccc",
    "status": "COMPLETED",
    "created_at": "2026-06-03T10:00:00Z",
    "context": {
        "_pr_changed_files": ["admin-ui/src/Table.tsx"],
    },
}

_TASK_BACKEND_ONLY = {
    "id": "task-backend-ddd",
    "status": "COMPLETED",
    "created_at": "2026-06-04T10:00:00Z",
    "context": {
        "_pr_changed_files": ["backend/app/main.py"],
        "_self_review_outcome": {
            "visual_verification": {
                "pairs_inspected": 5,
                "caught_visual_issue": True,
            },
        },
    },
}

_TASK_NOT_COMPLETED = {
    "id": "task-active-eee",
    "status": "IN_PROGRESS",
    "created_at": "2026-06-05T10:00:00Z",
    "context": {
        "_pr_changed_files": ["admin-ui/src/Nav.tsx"],
        "_self_review_outcome": {
            "visual_verification": {"pairs_inspected": 1, "caught_visual_issue": True},
        },
    },
}


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


def test_intish_basic():
    assert intish(3) == 3
    assert intish(0) == 0
    assert intish(None) == 0
    assert intish("2") == 2
    assert intish(True) == 1  # bool is int subclass — preserve as 1 not 0
    assert intish(-5) == 0  # clamped to non-negative
    assert intish("bad") == 0


def test_changed_files_prefers_pr_changed_files():
    task = {
        "context": {
            "_pr_changed_files": ["admin-ui/src/App.tsx", "backend/app/main.py"],
            "_file_change_details": [{"path": "other/file.ts"}],
        }
    }
    assert changed_files(task) == ["admin-ui/src/App.tsx", "backend/app/main.py"]


def test_changed_files_falls_back_to_file_change_details():
    task = {
        "context": {
            "_file_change_details": [
                {"path": "admin-ui/src/App.tsx"},
                {"path": "backend/app/main.py"},
                {"not_path": "ignored"},
            ]
        }
    }
    assert changed_files(task) == ["admin-ui/src/App.tsx", "backend/app/main.py"]


def test_changed_files_falls_back_to_top_level_file_change_details():
    task = {
        "context": {},
        "file_change_details": [{"path": "admin-ui/src/Nav.tsx"}],
    }
    assert changed_files(task) == ["admin-ui/src/Nav.tsx"]


def test_touches_admin_ui_true_for_admin_ui_paths():
    task = {"context": {"_pr_changed_files": ["admin-ui/src/Component.tsx"]}}
    assert touches_admin_ui(task) is True


def test_touches_admin_ui_false_for_backend_only():
    task = {"context": {"_pr_changed_files": ["backend/app/main.py"]}}
    assert touches_admin_ui(task) is False


def test_touches_admin_ui_false_when_no_files():
    assert touches_admin_ui({}) is False


def test_extract_visual_verification_from_context_outcome():
    entry, bad = extract_visual_verification(_TASK_ADMIN_CAUGHT)
    assert bad is False
    assert isinstance(entry, dict)
    assert entry["caught_visual_issue"] is True
    assert entry["pairs_inspected"] == 3


def test_extract_visual_verification_absent_returns_none():
    entry, bad = extract_visual_verification(_TASK_ADMIN_NO_ENTRY)
    assert entry is None
    assert bad is False


def test_extract_visual_verification_malformed_json_string():
    # conftest sets up context key only — use 'context'
    task_bad = {
        "context": {
            "_self_review_outcome": "not-valid-json",
            "_pr_changed_files": ["admin-ui/src/X.tsx"],
        }
    }
    entry, bad = extract_visual_verification(task_bad)
    assert entry is None
    assert bad is True


def test_extract_visual_verification_from_json_string():
    inner = {"pairs_inspected": 2, "caught_visual_issue": False}
    task = {
        "self_review_outcome": json.dumps({"visual_verification": inner}),
        "context": {"_pr_changed_files": ["admin-ui/src/Y.tsx"]},
    }
    entry, bad = extract_visual_verification(task)
    assert bad is False
    assert entry == inner


def test_extract_visual_verification_falls_back_to_check_results():
    inner = {"pairs_inspected": 1, "caught_visual_issue": False}
    task = {
        "context": {
            "_self_review_outcome": {
                "check_results": {"visual_verification": inner},
            },
            "_pr_changed_files": ["admin-ui/src/Z.tsx"],
        }
    }
    entry, bad = extract_visual_verification(task)
    assert bad is False
    assert entry == inner


def test_infra_occurrences_sentinel_in_failure_class():
    entry = {"failure_class": "VIEWPORT_GATE_INFRA_UNAVAILABLE", "pairs_inspected": 0}
    assert infra_occurrences(entry) >= 1


def test_infra_occurrences_none_when_clean():
    entry = {"pairs_inspected": 2, "pairs_changed": 1}
    assert infra_occurrences(entry) == 0


def test_infra_occurrences_list_field():
    entry = {"infra_failures": ["fail1", "fail2"]}
    assert infra_occurrences(entry) == 2


# ---------------------------------------------------------------------------
# aggregate_visual_pilot — the main kill-criterion computation
# ---------------------------------------------------------------------------


def _all_tasks() -> list[dict]:
    return [
        _TASK_ADMIN_CAUGHT,
        _TASK_ADMIN_INFRA_FAIL,
        _TASK_ADMIN_NO_ENTRY,
        _TASK_BACKEND_ONLY,
        _TASK_NOT_COMPLETED,
    ]


def test_aggregate_counts_admin_ui_completed_tasks_only():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    # 3 COMPLETED admin-ui tasks; backend-only and IN_PROGRESS are excluded
    assert agg.admin_task_count == 3


def test_aggregate_entry_count_excludes_no_entry_tasks():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    # _TASK_ADMIN_NO_ENTRY has no visual_verification block
    assert agg.entry_count == 2


def test_aggregate_pairs_inspected_sums_correctly():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    # caught: 3, infra-fail: 0 → total 3
    assert agg.pairs_inspected == 3


def test_aggregate_pairs_changed_sums_correctly():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    # caught: 2, infra-fail: 0 → total 2
    assert agg.pairs_changed == 2


def test_aggregate_caught_list_contains_only_caught_tasks():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    assert len(agg.caught) == 1
    caught_task, caught_entry = agg.caught[0]
    assert caught_task["id"] == "task-caught-aaa"
    assert caught_entry["caught_visual_issue"] is True


def test_aggregate_infra_total_counts_sentinel():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    assert agg.infra_total == 1


def test_aggregate_infra_rate():
    agg = aggregate_visual_pilot(_all_tasks(), created_after="2026-06-01T00:00:00Z")
    assert agg.infra_rate == pytest.approx(0.5)


def test_aggregate_empty_task_list():
    agg = aggregate_visual_pilot([], created_after="2026-06-01T00:00:00Z")
    assert isinstance(agg, VisualPilotAggregates)
    assert agg.admin_task_count == 0
    assert agg.entry_count == 0
    assert agg.caught == []
    assert agg.infra_rate == 0.0


def test_aggregate_created_before_filters_tasks():
    agg = aggregate_visual_pilot(
        _all_tasks(),
        created_after="2026-06-01T00:00:00Z",
        created_before="2026-06-02T00:00:00Z",
    )
    # Only _TASK_ADMIN_CAUGHT (created 2026-06-01T10:00:00Z) fits the window
    assert agg.admin_task_count == 1
    assert agg.entry_count == 1
    assert len(agg.caught) == 1


def test_aggregate_malformed_count():
    malformed_task = {
        "id": "task-bad-fff",
        "status": "COMPLETED",
        "created_at": "2026-06-01T12:00:00Z",
        "context": {
            "_pr_changed_files": ["admin-ui/src/Bad.tsx"],
            "_self_review_outcome": "not-valid-json!!!",
        },
    }
    agg = aggregate_visual_pilot([malformed_task], created_after="2026-06-01T00:00:00Z")
    assert agg.malformed_count == 1
    assert agg.entry_count == 0


def test_aggregate_non_admin_ui_not_counted_even_with_visual_entry():
    agg = aggregate_visual_pilot(
        [_TASK_BACKEND_ONLY], created_after="2026-06-01T00:00:00Z"
    )
    assert agg.admin_task_count == 0
    assert agg.entry_count == 0
    assert agg.caught == []


def test_aggregate_in_progress_not_counted():
    agg = aggregate_visual_pilot(
        [_TASK_NOT_COMPLETED], created_after="2026-06-01T00:00:00Z"
    )
    assert agg.admin_task_count == 0
