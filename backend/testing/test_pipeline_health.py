"""Standalone tests for compute_pipeline_health_summary.

These tests do not require a database — all DB calls are mocked.
Run with: bash scripts/test-backend.sh testing/test_pipeline_health.py --standalone
"""
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.admin_analytics import compute_pipeline_health_summary
from app.models import Task, TaskStatus, Project
from testing.factories import make_task, make_project, _utcnow


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


# ── Empty project ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_project_returns_sensible_defaults():
    """Empty project (no tasks, no directions) → null success_rate, empty distribution."""
    project = make_project(directions=[])
    session = _make_mock_session(tasks=[], project=project)
    project_id = uuid.uuid4()

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] is None
    assert result["trend"] is None
    assert result["failure_distribution"] == {}
    assert result["direction_stats"] == []


# ── Success rate ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_rate_computed_correctly():
    """3 completed + 1 failed → success_rate = 0.75."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=3)),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="ImportError: missing module"),
    ]
    # status_log is empty so _derive_completed_at falls back to created_at (within window)
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 0.75


@pytest.mark.asyncio
async def test_success_rate_all_completed():
    """All completed → success_rate = 1.0."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_success_rate_all_failed():
    """All failed → success_rate = 0.0."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="timeout"),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 0.0


# ── Failure distribution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_distribution_uses_classify_failure_categories():
    """Failure distribution maps to the correct classify_failure classes."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        # CODE_ERROR (default)
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="SyntaxError: invalid syntax"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=2),
                  latest_status_message="AttributeError: NoneType has no attribute"),
        # PROVISION_FAILURE
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=3),
                  latest_status_message="gcp quota exceeded for zone us-central1"),
        # RATE_LIMIT
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="API Error: Rate limit reached"),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    dist = result["failure_distribution"]
    assert dist.get("CODE_ERROR") == 2
    assert dist.get("PROVISION_FAILURE") == 1
    assert dist.get("RATE_LIMIT") == 1


@pytest.mark.asyncio
async def test_failure_distribution_top_3_only():
    """Failure distribution is capped at top 3 classes by count."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # 4 distinct failure classes — only top 3 should appear
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="SyntaxError"),  # CODE_ERROR x3
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=2),
                  latest_status_message="ImportError"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=3),
                  latest_status_message="NameError"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="gcp quota exceeded"),  # PROVISION_FAILURE x2
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=5),
                  latest_status_message="vm startup failed network"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=6),
                  latest_status_message="API Error: Rate limit reached"),  # RATE_LIMIT x1 (should be cut)
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=7),
                  latest_status_message="heartbeat lost timeout oom"),  # AGENT_TIMEOUT x1 (should be cut)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    dist = result["failure_distribution"]
    assert len(dist) == 3
    assert "CODE_ERROR" in dist
    assert "PROVISION_FAILURE" in dist


# ── Trend ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_improving_when_success_rate_increased():
    """success_rate 0.5 → 0.8 (delta +0.3 > 0.05) → trend = improving."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # Previous window (24–48h ago): 1 completed, 1 failed → 50%
    prev_completed = make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                                created_at=now - timedelta(hours=36))
    prev_failed = make_task(project_id=project_id, status=TaskStatus.FAILED,
                             created_at=now - timedelta(hours=36),
                             latest_status_message="error")
    # Current window (0–24h ago): 4 completed, 1 failed → 80%
    curr_tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=5)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=6)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=7)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=8)),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=9),
                  latest_status_message="error"),
    ]
    tasks = [prev_completed, prev_failed] + curr_tasks
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["trend"] == "improving"


@pytest.mark.asyncio
async def test_trend_none_when_no_previous_window_data():
    """No tasks in previous window → trend is None (can't compare)."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["trend"] is None


# ── Direction stats ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direction_stats_merge_rate():
    """Direction with 2 research cycles and 1 PR merged → merge_rate = 0.5."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Improve test coverage",
        "possibly_saturated": False,
        "research_history": [
            {"timestamp": (now - timedelta(hours=5)).isoformat(), "prs_merged": 1},
        ],
    }])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=6)),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["direction_id"] == direction_id
    assert stats[0]["cycles"] == 2
    assert stats[0]["merge_rate"] == 0.5
    assert stats[0]["possibly_saturated"] is False


@pytest.mark.asyncio
async def test_direction_with_zero_cycles_excluded():
    """Active direction with no completed research cycles in window is excluded from stats."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Direction with no cycles",
        "possibly_saturated": False,
        "research_history": [],
    }])

    # Task exists but is FAILED, not COMPLETED
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4),
                  latest_status_message="error"),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["direction_stats"] == []


# ── Return structure ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_return_structure_has_all_required_keys():
    """Result always contains all required keys regardless of data."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert set(result.keys()) == {"success_rate", "trend", "failure_distribution", "direction_stats", "first_attempt_success_rate", "pipeline_conversion_rate"}
