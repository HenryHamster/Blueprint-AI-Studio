"""Standalone tests for persistent anomaly tracking and proposal generation.

These tests do not require a database — all DB calls are mocked.
Run with: bash scripts/test-backend.sh testing/test_anomaly_proposals.py --standalone
"""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.shared_state as _shared_state
from app.anomaly_detection import _compute_research_anomalies, _check_anomaly_proposals
from app.models import TaskStatus
from testing.factories import make_task, make_project, _utcnow


def _make_session(tasks: list, project):
    """Build a mock AsyncSession for _compute_research_anomalies.

    First execute() → terminal tasks via .scalars().all()
    Second execute() → project via .scalar_one_or_none()
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


def _make_proposal_session():
    """Build a mock AsyncSession for _check_anomaly_proposals (no execute calls needed).

    Uses MagicMock for session.add since it's synchronous in SQLAlchemy.
    """
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture(autouse=True)
def _clean_persistent_anomalies():
    """Reset persistent anomalies before each test."""
    _shared_state.persistent_anomalies.clear()
    yield
    _shared_state.persistent_anomalies.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Persistent anomaly tracking in _compute_research_anomalies
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anomalies_tracked_in_shared_state():
    """Detected anomalies should be persisted in _shared_state.persistent_anomalies."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: module not found",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(5)
    ]
    session = _make_session(tasks=tasks, project=project)

    await _compute_research_anomalies(project_id, session)

    proj_key = str(project_id)
    assert proj_key in _shared_state.persistent_anomalies
    assert "CODE_ERROR" in _shared_state.persistent_anomalies[proj_key]
    info = _shared_state.persistent_anomalies[proj_key]["CODE_ERROR"]
    assert info["occurrence_count"] == 1
    assert info["severity"] == "medium"


@pytest.mark.asyncio
async def test_occurrence_count_increments_across_calls():
    """Multiple calls to _compute_research_anomalies increment occurrence_count."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: cannot read property",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(5)
    ]

    for call_num in range(3):
        session = _make_session(tasks=tasks, project=project)
        await _compute_research_anomalies(project_id, session)

    proj_key = str(project_id)
    assert _shared_state.persistent_anomalies[proj_key]["CODE_ERROR"]["occurrence_count"] == 3


@pytest.mark.asyncio
async def test_stale_anomalies_expire_after_48h():
    """Anomalies not seen for >48h are removed from tracking."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    # Pre-seed a stale anomaly last seen 49h ago
    _shared_state.persistent_anomalies[proj_key] = {
        "OLD_ERROR": {
            "first_seen": (now - timedelta(hours=100)).isoformat(),
            "last_seen": (now - timedelta(hours=49)).isoformat(),
            "occurrence_count": 5,
            "severity": "high",
            "latest_details": "Old error",
        }
    }

    # Run with no anomalies detected (empty tasks)
    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)
    await _compute_research_anomalies(project_id, session)

    # OLD_ERROR should be expired
    assert "OLD_ERROR" not in _shared_state.persistent_anomalies.get(proj_key, {})


@pytest.mark.asyncio
async def test_recent_unseen_anomaly_not_expired():
    """Anomalies not seen this cycle but last_seen < 48h ago are kept."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "RECENT_ERROR": {
            "first_seen": (now - timedelta(hours=10)).isoformat(),
            "last_seen": (now - timedelta(hours=10)).isoformat(),
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "Recent error",
        }
    }

    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)
    await _compute_research_anomalies(project_id, session)

    # Should still be there (last_seen only 10h ago)
    assert "RECENT_ERROR" in _shared_state.persistent_anomalies[proj_key]


# ─────────────────────────────────────────────────────────────────────────────
# _check_anomaly_proposals
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proposal_created_after_3_occurrences():
    """Anomaly with occurrence_count >= 3 generates a direction proposal."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "first_seen": (now - timedelta(hours=72)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "AttributeError in module X",
        }
    }

    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    # Should have one proposed direction
    assert len(project.directions) == 1
    d = project.directions[0]
    assert d["status"] == "proposed"
    assert d["proposed_by"] == "ANOMALY_DETECTOR"
    assert "CODE_ERROR" in d["text"]
    assert d["priority"] == "high"

    # Occurrence count should be reset
    assert _shared_state.persistent_anomalies[proj_key]["CODE_ERROR"]["occurrence_count"] == 0


@pytest.mark.asyncio
async def test_no_proposal_below_3_occurrences():
    """Anomaly with occurrence_count < 3 does not generate a proposal."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "first_seen": (now - timedelta(hours=24)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "Some error",
        }
    }

    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    assert len(project.directions) == 0


@pytest.mark.asyncio
async def test_max_one_proposal_per_cycle():
    """Only 1 anomaly-driven proposal is created per call, even with multiple eligible anomalies."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "first_seen": (now - timedelta(hours=72)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 5,
            "severity": "high",
            "latest_details": "Error A",
        },
        "PROVISION_FAILURE": {
            "first_seen": (now - timedelta(hours=48)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 4,
            "severity": "medium",
            "latest_details": "Error B",
        },
    }

    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    # Only 1 proposal created (capped at 1 per cycle)
    assert len(project.directions) == 1


@pytest.mark.asyncio
async def test_proposal_respects_10_pending_cap():
    """No proposal created when 10 pending proposals already exist."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "first_seen": (now - timedelta(hours=72)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 5,
            "severity": "high",
            "latest_details": "Error details",
        }
    }

    # Create 10 existing proposed directions
    existing = [
        {"id": f"d-cap{i:05d}", "text": f"Existing proposal {i}", "status": "proposed"}
        for i in range(10)
    ]
    project = make_project(directions=existing)
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    # Should still be 10 (no new proposal added)
    assert len(project.directions) == 10


@pytest.mark.asyncio
async def test_duplicate_proposal_skipped():
    """Anomaly proposal overlapping with existing direction is skipped."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "first_seen": (now - timedelta(hours=72)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "AttributeError in module X",
        }
    }

    # Existing direction that overlaps with the proposal text
    existing = [{
        "id": "d-existing1",
        "text": "Investigate and reduce CODE_ERROR task failures in the pipeline",
        "status": "active",
    }]
    project = make_project(directions=existing)
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    # No new proposal added (duplicate detected)
    assert len(project.directions) == 1


@pytest.mark.asyncio
async def test_proposal_has_correct_fields():
    """Verify the structure of a generated anomaly proposal."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "PROVISION_FAILURE": {
            "first_seen": (now - timedelta(hours=96)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 4,
            "severity": "medium",
            "latest_details": "VM startup timeout",
        }
    }

    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    d = project.directions[0]
    assert d["id"].startswith("d-")
    assert d["status"] == "proposed"
    assert d["proposed_by"] == "ANOMALY_DETECTOR"
    assert "PROVISION_FAILURE" in d["text"]
    assert d["priority"] == "medium"
    assert "proposed_at" in d
    assert "proposal_rationale" in d
    assert "4 times" in d["proposal_rationale"]
    assert d["paths"] == []


@pytest.mark.asyncio
async def test_no_anomalies_no_proposals():
    """Empty persistent_anomalies dict produces no proposals."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    assert len(project.directions) == 0


@pytest.mark.asyncio
async def test_end_to_end_3_cycles_generates_proposal():
    """Simulate 3 detection cycles of the same anomaly, then verify a proposal is created."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(5)
    ]

    # Simulate 3 detection cycles
    for _ in range(3):
        session = _make_session(tasks=tasks, project=project)
        await _compute_research_anomalies(project_id, session)

    proj_key = str(project_id)
    assert _shared_state.persistent_anomalies[proj_key]["PROVISION_FAILURE"]["occurrence_count"] == 3

    # Now check for proposals
    project.id = project_id
    session = _make_proposal_session()
    await _check_anomaly_proposals(project, session)

    assert len(project.directions) == 1
    assert project.directions[0]["proposed_by"] == "ANOMALY_DETECTOR"
    assert "PROVISION_FAILURE" in project.directions[0]["text"]

    # Occurrence count reset after proposal
    assert _shared_state.persistent_anomalies[proj_key]["PROVISION_FAILURE"]["occurrence_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Direction-level anomaly tracking and proposals
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direction_anomalies_tracked_with_unique_keys():
    """Direction-level anomalies use Direction_<id> keys, not a shared 'Direction' key."""
    project_id = uuid.uuid4()
    dir1 = {
        "id": "d-aaa00001",
        "text": "First direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 0},
        ],
    }
    dir2 = {
        "id": "d-bbb00002",
        "text": "Second direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 0},
        ],
    }
    project = make_project(directions=[dir1, dir2])
    session = _make_session(tasks=[], project=project)

    await _compute_research_anomalies(project_id, session)

    proj_key = str(project_id)
    anomalies = _shared_state.persistent_anomalies.get(proj_key, {})
    # Each direction should have its own key
    assert "Direction_d-aaa00001" in anomalies
    assert "Direction_d-bbb00002" in anomalies
    # The generic "Direction" key should NOT exist
    assert "Direction" not in anomalies


@pytest.mark.asyncio
async def test_direction_anomaly_proposal_after_3_occurrences():
    """Direction-level anomaly with occurrence_count >= 3 generates a proposal."""
    project_id = uuid.uuid4()
    proj_key = str(project_id)
    now = _utcnow()

    _shared_state.persistent_anomalies[proj_key] = {
        "Direction_d-test0001": {
            "first_seen": (now - timedelta(hours=72)).isoformat(),
            "last_seen": now.isoformat(),
            "occurrence_count": 3,
            "severity": "medium",
            "latest_details": 'Direction "Improve test coverage" (id: d-test0001) has seen a consistent decline',
        }
    }

    project = make_project(directions=[])
    project.id = project_id
    session = _make_proposal_session()

    await _check_anomaly_proposals(project, session)

    assert len(project.directions) == 1
    d = project.directions[0]
    assert d["status"] == "proposed"
    assert d["proposed_by"] == "ANOMALY_DETECTOR"
    assert "Direction_d-test0001" in d["text"]
    assert _shared_state.persistent_anomalies[proj_key]["Direction_d-test0001"]["occurrence_count"] == 0


@pytest.mark.asyncio
async def test_multiple_direction_anomalies_tracked_independently():
    """Two directions with anomalies increment their own occurrence counts independently."""
    project_id = uuid.uuid4()
    dir1 = {
        "id": "d-ind00001",
        "text": "Direction one",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 3},
            {"tasks_created": 6, "prs_merged": 0, "tasks_failed": 4},
        ],
    }
    dir2 = {
        "id": "d-ind00002",
        "text": "Direction two",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 3},
            {"tasks_created": 6, "prs_merged": 0, "tasks_failed": 5},
        ],
    }
    project = make_project(directions=[dir1, dir2])

    # Run 3 cycles to build up occurrence counts
    for _ in range(3):
        session = _make_session(tasks=[], project=project)
        await _compute_research_anomalies(project_id, session)

    proj_key = str(project_id)
    anomalies = _shared_state.persistent_anomalies[proj_key]
    assert anomalies["Direction_d-ind00001"]["occurrence_count"] == 3
    assert anomalies["Direction_d-ind00002"]["occurrence_count"] == 3
