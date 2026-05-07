"""Regression tests for admin_scope_measurement._compute_scope_baselines."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models import Task, TaskStatus
from app.routers.admin_scope_measurement import _compute_scope_baselines


def _fake_task(scope_value, status=TaskStatus.COMPLETED, pr_merged=True, days_ago=1):
    t = MagicMock(spec=Task)
    t.context = {"scope": scope_value, "pr_merged": pr_merged}
    t.status = status
    t.created_at = datetime(2026, 4, 20, tzinfo=timezone.utc).replace(tzinfo=None)
    return t


def test_null_scope_coerced_to_unknown_and_does_not_typeerror_on_sort():
    """Regression: a task with context={"scope": None} was returning None
    from .get("scope", "unknown") (default only kicks in on missing keys,
    not null values), then sorted(bucket.items()) raised
    `TypeError: '<' not supported between NoneType and str` because
    bucket keys mixed (week, None) with (week, "medium"). Endpoint
    returned 500 every time such a task existed in the lookback."""
    tasks = [
        _fake_task("medium"),
        _fake_task(None),  # explicit-null scope — the bug case
        _fake_task("small"),
        _fake_task("medium"),
    ]
    # Must not raise.
    result = _compute_scope_baselines(tasks, lookback_days=30)
    assert "series" in result
    assert "summary" in result
    # Null scopes should land under "unknown" bucket.
    unknown_count = sum(1 for s in result["series"] if s["scope"] == "unknown")
    assert unknown_count >= 1


def test_missing_scope_coerced_to_unknown():
    """Existing default-path case: tasks with no 'scope' key still land
    under 'unknown'."""
    t = MagicMock(spec=Task)
    t.context = {"pr_merged": True}  # no scope key
    t.status = TaskStatus.COMPLETED
    t.created_at = datetime(2026, 4, 20).replace(tzinfo=None)
    result = _compute_scope_baselines([t], lookback_days=30)
    assert any(s["scope"] == "unknown" for s in result["series"])


def test_empty_task_list_returns_empty_baselines():
    result = _compute_scope_baselines([], lookback_days=30)
    assert result["series"] == []
    assert result["summary"] == {}
