"""Standalone tests for regression signal first-tripped timestamp persistence.

Covers the hydrate/persist boundary so MTTD timestamps survive pod restarts.

Run with: bash scripts/test-backend.sh testing/test_regression_signal_persistence.py --standalone
"""

import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock

import app.shared_state as _ss
from app.shared_state import (
    hydrate_regression_signal_state,
    persist_regression_signal_snapshot,
)
from testing.factories import make_project


def _make_get_session(project):
    session = AsyncMock()
    session.get = AsyncMock(return_value=project)
    session.add = MagicMock()
    return session


def _make_execute_session(projects):
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = projects
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_persist_writes_to_regression_signal_state():
    project = make_project()
    project.id = uuid.uuid4()
    project.regression_signal_state = {}
    session = _make_get_session(project)

    state = {
        "sr_catch_rate": "2026-05-01T10:00:00",
        "fix_loop_divergence": "2026-05-01T11:00:00",
    }
    await persist_regression_signal_snapshot(str(project.id), state, session)

    assert project.regression_signal_state == state
    session.add.assert_called_once_with(project)


@pytest.mark.asyncio
async def test_persist_bad_project_id_is_noop():
    session = AsyncMock()
    session.get = AsyncMock()
    await persist_regression_signal_snapshot("not-a-uuid", {}, session)
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_persist_missing_project_is_noop():
    session = _make_get_session(None)
    await persist_regression_signal_snapshot(
        str(uuid.uuid4()), {"sr_catch_rate": "2026-05-01T10:00:00"}, session
    )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_hydrate_loads_stored_entries():
    project = make_project()
    project.id = uuid.uuid4()
    project.regression_signal_state = {
        "sr_catch_rate": "2026-05-01T10:00:00",
        "fix_loop_divergence": "2026-05-01T11:00:00",
    }
    session = _make_execute_session([project])

    _ss.regression_signal_first_tripped.clear()
    loaded = await hydrate_regression_signal_state(session)

    assert loaded == 2
    proj_state = _ss.regression_signal_first_tripped[str(project.id)]
    assert proj_state["sr_catch_rate"] == "2026-05-01T10:00:00"
    assert proj_state["fix_loop_divergence"] == "2026-05-01T11:00:00"


@pytest.mark.asyncio
async def test_hydrate_does_not_clobber_in_memory_entries():
    """In-memory entries written after startup began must win over stored ones."""
    project = make_project()
    project.id = uuid.uuid4()
    project.regression_signal_state = {"sr_catch_rate": "2026-05-01T08:00:00"}
    session = _make_execute_session([project])

    _ss.regression_signal_first_tripped.clear()
    _ss.regression_signal_first_tripped[str(project.id)] = {
        "sr_catch_rate": "2026-05-06T10:00:00"
    }
    await hydrate_regression_signal_state(session)

    assert (
        _ss.regression_signal_first_tripped[str(project.id)]["sr_catch_rate"]
        == "2026-05-06T10:00:00"
    )


@pytest.mark.asyncio
async def test_hydrate_skips_projects_with_empty_state():
    project = make_project()
    project.id = uuid.uuid4()
    project.regression_signal_state = {}
    session = _make_execute_session([project])

    _ss.regression_signal_first_tripped.clear()
    loaded = await hydrate_regression_signal_state(session)

    assert loaded == 0
    assert str(project.id) not in _ss.regression_signal_first_tripped


@pytest.mark.asyncio
async def test_roundtrip_survives_simulated_restart():
    """persist → clear in-memory → hydrate produces the same state."""
    project = make_project()
    project.id = uuid.uuid4()
    project.regression_signal_state = {}

    state = {"pipeline_success_rate": "2026-05-03T09:00:00"}
    persist_session = _make_get_session(project)
    await persist_regression_signal_snapshot(str(project.id), state, persist_session)

    _ss.regression_signal_first_tripped.clear()
    hydrate_session = _make_execute_session([project])
    loaded = await hydrate_regression_signal_state(hydrate_session)

    assert loaded == 1
    assert _ss.regression_signal_first_tripped[str(project.id)] == state
