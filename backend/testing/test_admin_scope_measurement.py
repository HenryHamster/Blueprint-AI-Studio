"""Regression tests for admin_scope_measurement._compute_scope_baselines."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.routers.admin_scope_measurement as asm
from app.models import Task, TaskStatus
from app.routers.admin_scope_measurement import (
    _compute_scope_baselines,
    get_scope_baselines,
    get_medium_scope_measurement,
    get_first_attempt_measurement,
)


def _fake_task(scope_value, status=TaskStatus.COMPLETED, pr_merged=True, days_ago=1):
    t = MagicMock(spec=Task)
    t.context = {"scope": scope_value, "pr_merged": pr_merged}
    t.status = status
    when = datetime(2026, 4, 20, tzinfo=timezone.utc).replace(tzinfo=None)
    t.created_at = when
    t.updated_at = when
    t.title = "Test Task"
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


# ── Endpoint safety-cap / truncation behaviour ──────────────────────────────
#
# These endpoints aggregate over the full lookback window, so they intentionally
# do NOT paginate (offset paging over an un-ordered aggregate skips/duplicates
# rows and a small page size silently changes the computed answer). Instead a
# single deterministic safety cap (_MAX_AGGREGATION_SCAN) bounds worst-case
# memory and the response advertises `truncated` + `scanned` rather than lying
# about a partial aggregate.


def _make_mock_session(tasks=None, project=None):
    """Build a mock AsyncSession whose execute() returns a given task list."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = tasks or []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.get = AsyncMock(return_value=project)
    return session


@pytest.mark.asyncio
async def test_scope_baselines_reports_full_scan_when_under_cap():
    """Normal volumes are never truncated: the aggregate is over every task and
    the legacy 'pagination' contract is gone (no page-size truncation)."""
    tasks = [_fake_task("medium"), _fake_task("small"), _fake_task("medium")]
    session = _make_mock_session(tasks=tasks)
    result = await get_scope_baselines(
        project_id=uuid.uuid4(),
        lookback_days=30,
        session=session,
        current_user={},
    )
    assert result["truncated"] is False
    assert result["scanned"] == 3
    assert "pagination" not in result
    # All three tasks contribute to the aggregate.
    assert result["summary"]["medium"]["created"] == 2
    assert result["summary"]["small"]["created"] == 1


@pytest.mark.asyncio
async def test_scope_baselines_safety_cap_truncates_deterministically(monkeypatch):
    """When the dataset exceeds the cap the scan is bounded to exactly the cap,
    `truncated` is advertised, and only the capped subset feeds the aggregate."""
    monkeypatch.setattr(asm, "_MAX_AGGREGATION_SCAN", 3)
    # First 3 (recency-ordered in prod) are medium; the trailing small rows are
    # beyond the cap and must NOT appear in the computed baseline.
    tasks = [
        _fake_task("medium"),
        _fake_task("medium"),
        _fake_task("medium"),
        _fake_task("small"),
        _fake_task("small"),
    ]
    session = _make_mock_session(tasks=tasks)
    result = await get_scope_baselines(
        project_id=uuid.uuid4(),
        lookback_days=30,
        session=session,
        current_user={},
    )
    assert result["truncated"] is True
    assert result["scanned"] == 3
    assert result["summary"]["medium"]["created"] == 3
    assert "small" not in result["summary"]


@pytest.mark.asyncio
async def test_medium_scope_measurement_filters_medium_after_cap(monkeypatch):
    """The cap is applied to terminal tasks BEFORE the medium filter, so the
    medium count reflects the (deterministic) capped window, not a pre-filtered
    slice that could exclude every medium task."""
    monkeypatch.setattr(asm, "_MAX_AGGREGATION_SCAN", 3)
    tasks = [
        _fake_task("medium"),
        _fake_task("medium"),
        _fake_task("small"),
        _fake_task("medium"),  # beyond the cap — excluded
        _fake_task("medium"),  # beyond the cap — excluded
    ]
    session = _make_mock_session(tasks=tasks, project=None)
    result = await get_medium_scope_measurement(
        project_id=uuid.uuid4(),
        direction_id=None,
        lookback_days=365,
        session=session,
        current_user={},
    )
    assert result["truncated"] is True
    assert result["scanned"] == 3
    # Of the first 3 (capped) tasks, exactly 2 are medium.
    assert result["total_created"] == 2
    assert "pagination" not in result


@pytest.mark.asyncio
async def test_first_attempt_measurement_reports_truncation(monkeypatch):
    monkeypatch.setattr(asm, "_MAX_AGGREGATION_SCAN", 2)
    tasks = [_fake_task("medium") for _ in range(4)]
    session = _make_mock_session(tasks=tasks, project=None)
    result = await get_first_attempt_measurement(
        project_id=uuid.uuid4(),
        lookback_days=90,
        split_date=None,
        session=session,
        current_user={},
    )
    assert result["truncated"] is True
    assert result["scanned"] == 2
    assert "pagination" not in result
