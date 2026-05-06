"""Standalone tests for anomaly fingerprint persistence in shared_state.

Covers the hydrate/persist boundary: a recovery scan run against state
captured before a simulated pod restart produces the same decision as if
the restart hadn't happened.

Run with: bash scripts/test-backend.sh testing/test_anomaly_persistence.py --standalone
"""
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.shared_state as _ss
from app.shared_state import (
    hydrate_persistent_anomalies,
    persist_anomaly_snapshot,
    _ANOMALY_FINGERPRINTS_KEY,
)
from testing.factories import make_project


def _make_get_session(project):
    """Mock session whose .get(Project, pid) returns project."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=project)
    session.add = MagicMock()
    return session


def _make_execute_session(projects):
    """Mock session whose .execute() yields a project list."""
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = projects
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)
    return session


# ─────────────────────────────────────────────────────────────────────────────
# persist_anomaly_snapshot
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_persist_writes_to_design_doc():
    project = make_project()
    project.id = uuid.uuid4()
    project.design_doc = {}
    session = _make_get_session(project)

    anomalies = {
        "CODE_ERROR": {
            "first_seen": "2026-05-01T00:00:00",
            "last_seen": "2026-05-06T00:00:00",
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "lots of errors",
        }
    }
    await persist_anomaly_snapshot(str(project.id), anomalies, session)

    assert project.design_doc[_ANOMALY_FINGERPRINTS_KEY] == anomalies
    session.add.assert_called_once_with(project)


@pytest.mark.asyncio
async def test_persist_preserves_existing_design_doc_keys():
    project = make_project()
    project.id = uuid.uuid4()
    project.design_doc = {"architecture": "existing content"}
    session = _make_get_session(project)

    await persist_anomaly_snapshot(str(project.id), {"CODE_ERROR": {"occurrence_count": 1}}, session)

    assert project.design_doc["architecture"] == "existing content"
    assert _ANOMALY_FINGERPRINTS_KEY in project.design_doc


@pytest.mark.asyncio
async def test_persist_bad_project_id_is_noop():
    session = AsyncMock()
    session.get = AsyncMock()
    await persist_anomaly_snapshot("not-a-uuid", {}, session)
    session.get.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# hydrate_persistent_anomalies
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hydrate_loads_stored_entries():
    project = make_project()
    project.id = uuid.uuid4()
    stored = {
        "CODE_ERROR": {
            "first_seen": "2026-05-01T00:00:00",
            "last_seen": "2026-05-05T00:00:00",
            "occurrence_count": 5,
            "severity": "high",
            "latest_details": "test error",
        }
    }
    project.design_doc = {_ANOMALY_FINGERPRINTS_KEY: stored}
    session = _make_execute_session([project])

    _ss.persistent_anomalies.clear()
    loaded = await hydrate_persistent_anomalies(session)

    assert loaded == 1
    assert _ss.persistent_anomalies[str(project.id)]["CODE_ERROR"]["occurrence_count"] == 5


@pytest.mark.asyncio
async def test_hydrate_does_not_clobber_in_memory_entries():
    """In-memory entries written after startup began must win over stored ones."""
    project = make_project()
    project.id = uuid.uuid4()
    stored = {"CODE_ERROR": {"occurrence_count": 3}}
    project.design_doc = {_ANOMALY_FINGERPRINTS_KEY: stored}
    session = _make_execute_session([project])

    _ss.persistent_anomalies.clear()
    _ss.persistent_anomalies[str(project.id)] = {"CODE_ERROR": {"occurrence_count": 7}}
    await hydrate_persistent_anomalies(session)

    assert _ss.persistent_anomalies[str(project.id)]["CODE_ERROR"]["occurrence_count"] == 7


@pytest.mark.asyncio
async def test_hydrate_skips_projects_without_stored_anomalies():
    project = make_project()
    project.id = uuid.uuid4()
    project.design_doc = {"architecture": "no anomaly key here"}
    session = _make_execute_session([project])

    _ss.persistent_anomalies.clear()
    loaded = await hydrate_persistent_anomalies(session)

    assert loaded == 0
    assert str(project.id) not in _ss.persistent_anomalies


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip: persist → restart → hydrate → same decision
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_roundtrip_same_anomaly_state_after_restart():
    """Simulated pod restart: persist → clear in-memory → hydrate → same data."""
    project = make_project()
    project.id = uuid.uuid4()
    project.design_doc = {}

    anomalies = {
        "PROVISION_FAILURE": {
            "first_seen": "2026-05-01T00:00:00",
            "last_seen": "2026-05-06T00:00:00",
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "provision timed out",
        }
    }

    persist_session = _make_get_session(project)
    _ss.persistent_anomalies[str(project.id)] = anomalies
    await persist_anomaly_snapshot(str(project.id), anomalies, persist_session)

    # Simulate pod restart: clear in-memory state
    _ss.persistent_anomalies.clear()
    assert str(project.id) not in _ss.persistent_anomalies

    # Hydrate from what was persisted
    hydrate_session = _make_execute_session([project])
    loaded = await hydrate_persistent_anomalies(hydrate_session)

    assert loaded == 1
    restored = _ss.persistent_anomalies[str(project.id)]
    assert restored["PROVISION_FAILURE"]["occurrence_count"] == 4
    assert restored["PROVISION_FAILURE"]["severity"] == "high"
