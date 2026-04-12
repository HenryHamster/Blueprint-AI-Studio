"""Standalone tests for _compute_research_anomalies.

These tests do not require a database — all DB calls are mocked.
Run with: bash scripts/test-backend.sh testing/test_anomaly_detection.py --standalone
"""
import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.anomaly_detection import _compute_research_anomalies
from app.main import classify_failure
from app.models import Task, TaskStatus, Project
from testing.factories import make_task, make_project, _utcnow


def _make_session(tasks: list, project: Project):
    """Build a mock AsyncSession.

    First execute() call (terminal tasks) uses .scalars().all().
    Second execute() call (project) uses .scalar_one_or_none().
    """
    session = AsyncMock()
    # session.add is synchronous in SQLAlchemy — use MagicMock to avoid
    # RuntimeWarning about unawaited coroutines
    session.add = MagicMock()

    tasks_scalars = MagicMock()
    tasks_scalars.all.return_value = tasks
    tasks_result = MagicMock()
    tasks_result.scalars.return_value = tasks_scalars

    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project

    session.execute = AsyncMock(side_effect=[tasks_result, project_result])
    return session


# ─────────────────────────────────────────────────────────────────────────────
# Empty / no-data cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_task_history_returns_empty_list():
    """No terminal tasks and no directions → no anomalies."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert result == []


@pytest.mark.asyncio
async def test_few_failures_below_threshold_no_anomaly():
    """3 or fewer failures of a class in 48h (at threshold boundary) → no anomaly."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="non-zero exit code",
            created_at=now - timedelta(hours=1),
        )
        for _ in range(3)  # exactly 3 — threshold is >3
    ]
    session = _make_session(tasks=tasks, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # No CODE_ERROR anomaly because count is 3, not >3
    assert all("CODE_ERROR" not in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# Rising failure counts
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rising_failure_count_produces_high_severity_anomaly():
    """failure_class count doubled from prior 48h to recent 48h → high severity."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 5 CODE_ERROR failures in last 48h (no keyword match → CODE_ERROR default)
    recent = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="AttributeError: 'NoneType' object has no attribute 'get'",
            created_at=now - timedelta(hours=12 + i),
        )
        for i in range(5)
    ]
    # 2 CODE_ERROR failures in prior 48h (48–96h ago)
    prior = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="AttributeError: 'NoneType' object has no attribute 'get'",
            created_at=now - timedelta(hours=60 + i),
        )
        for i in range(2)
    ]
    session = _make_session(tasks=recent + prior, project=project)

    result = await _compute_research_anomalies(project_id, session)

    high = [a for a in result if a["severity"] == "high"]
    assert len(high) >= 1
    assert any("CODE_ERROR" in a["signal"] for a in high)


@pytest.mark.asyncio
async def test_high_failure_count_no_prior_is_medium_severity():
    """More than 3 failures in recent 48h with zero in prior window → medium severity."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 4 recent CODE_ERROR failures, none in prior window (no keyword → CODE_ERROR default)
    recent = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: unsupported operand type for +",
            created_at=now - timedelta(hours=10 + i),
        )
        for i in range(4)
    ]
    session = _make_session(tasks=recent, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # Should have at least one anomaly, all medium (no prior → no doubling)
    assert len(result) >= 1
    code_error = [a for a in result if "CODE_ERROR" in a["signal"]]
    assert len(code_error) >= 1
    assert all(a["severity"] == "medium" for a in code_error)


@pytest.mark.asyncio
async def test_provision_failures_detected_separately_from_code_errors():
    """PROVISION_FAILURE and CODE_ERROR are tracked as separate failure classes."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 4 provision failures in recent window
    provision = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(4)
    ]
    # 4 code failures in recent window (no keyword match → CODE_ERROR default)
    code = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: cannot import name 'foo'",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(4)
    ]
    session = _make_session(tasks=provision + code, project=project)

    result = await _compute_research_anomalies(project_id, session)

    signals = [a["signal"] for a in result]
    assert any("PROVISION_FAILURE" in s for s in signals)
    assert any("CODE_ERROR" in s for s in signals)


# ─────────────────────────────────────────────────────────────────────────────
# classify_failure — agent crash report classification
# ─────────────────────────────────────────────────────────────────────────────

def test_classify_failure_uncaught_exception_is_agent_timeout():
    """[FATAL:uncaughtException] crash reports must classify as AGENT_TIMEOUT, not CODE_ERROR."""
    task = make_task(
        latest_status_message="[512MB cg:2048MB] [FATAL:uncaughtException] TypeError: Cannot read properties of undefined",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_unhandled_rejection_is_agent_timeout():
    """[FATAL:unhandledRejection] crash reports must classify as AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="[256MB cg:2048MB] [FATAL:unhandledRejection] Promise rejected: connection lost",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_sigterm_still_works():
    """[FATAL:SIGTERM] must continue to classify as AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="[256MB cg:2048MB] [FATAL:SIGTERM] Agent received shutdown signal",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_genuine_code_error_no_fatal_prefix():
    """Errors without [FATAL: prefix and no other timeout keywords stay as CODE_ERROR."""
    task = make_task(
        latest_status_message="AttributeError: 'NoneType' object has no attribute 'foo'",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "CODE_ERROR"


# ─────────────────────────────────────────────────────────────────────────────
# Declining merge rates
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_declining_merge_rate_produces_medium_anomaly():
    """Direction with consistently declining merge rate → medium anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-test0001",
        "text": "Improve test coverage for API layer",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},  # rate 0.80
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},  # rate 0.40
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 0},  # rate 0.00
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    medium = [a for a in result if a["severity"] == "medium" and "d-test0001" in a["signal"]]
    assert len(medium) >= 1
    assert "merge rate declining" in medium[0]["signal"]


@pytest.mark.asyncio
async def test_stable_merge_rate_no_declining_anomaly():
    """Direction with flat merge rate → no declining merge rate anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-stable01",
        "text": "Add observability layer",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("merge rate declining" in a["signal"] for a in result)


@pytest.mark.asyncio
async def test_fewer_than_3_history_cycles_no_merge_rate_anomaly():
    """Direction with only 2 history entries → insufficient data, no merge rate anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-short01",
        "text": "Short history direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 1, "tasks_failed": 0},
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("merge rate declining" in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# High task failure rate per direction
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_direction_failure_rate_produces_high_anomaly():
    """Direction with >50% failure rate in last 2 cycles → high severity anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-faildir1",
        "text": "Migrate authentication to OAuth2",
        "status": "active",
        "priority": "high",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 3},  # 75% fail
            {"tasks_created": 6, "prs_merged": 0, "tasks_failed": 4},  # 67% fail
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    high = [a for a in result if a["severity"] == "high" and "d-faildir1" in a["signal"]]
    assert len(high) >= 1
    assert "failure rate" in high[0]["signal"]


@pytest.mark.asyncio
async def test_low_direction_failure_rate_no_anomaly():
    """Direction with 40% failure rate in last 2 cycles → below threshold, no anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-lowfail1",
        "text": "Improve caching strategy",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 2},  # 40% fail
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 2},  # 40% fail
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("d-lowfail1" in a["signal"] and "failure rate" in a["signal"] for a in result)


@pytest.mark.asyncio
async def test_paused_directions_excluded_from_analysis():
    """Paused directions are skipped — only active directions are analyzed."""
    project_id = uuid.uuid4()
    paused_direction = {
        "id": "d-paused01",
        "text": "Paused work direction",
        "status": "paused",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 4},
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 4},
        ],
    }
    project = make_project(directions=[paused_direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("d-paused01" in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# Combined scenarios
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_anomalies_returned_when_multiple_signals_present():
    """Multiple independent signals all appear in the returned anomaly list."""
    now = _utcnow()
    project_id = uuid.uuid4()

    direction = {
        "id": "d-multi01",
        "text": "Multi-signal direction for testing",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},  # 0.80
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},  # 0.40
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 4},  # 0.00, 80% fail
        ],
    }
    project = make_project(directions=[direction])

    # 5 provision failures in recent 48h, 2 in prior 48h → high severity
    recent_provisions = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=10 + i),
        )
        for i in range(5)
    ]
    prior_provisions = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=60 + i),
        )
        for i in range(2)
    ]
    session = _make_session(tasks=recent_provisions + prior_provisions, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # Should have at least: provision failure anomaly + merge rate or failure rate anomaly
    assert len(result) >= 2
    signals = [a["signal"] for a in result]
    assert any("PROVISION_FAILURE" in s for s in signals)
    assert any("d-multi01" in s for s in signals)
