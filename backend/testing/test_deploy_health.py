"""Tests for the _check_pause_mode state machine in backend/app/deploy_health.py.

Covers:
  1. First and second failure — logs warning but does NOT pause
  2. Third consecutive failure — triggers auto-pause (max_concurrent_vms=0)
  3. Auto-resume gate — 3 consecutive healthy cycles required before resuming
  4. Manual admin pause guard — auto-resume is skipped for non-automated pauses
  5. Failure streak resets to 0 on a healthy cycle
  6. Pre-pause VM count is restored (capped at 2) on resume
"""

import copy
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.deploy_health as _dh
import app.health_check_primitives as _hcp
import app.shared_state as _shared_state
from app.models import DeploymentState, DeploymentStatus, Project
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

# Capture real function references at import time so tests can invoke them
# after the dh_db fixture replaces them with AsyncMocks.
_REAL_CREATE_FORCED_RESUME_ALERTS = _dh._create_forced_resume_alerts

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def dh_db(mocker):
    """Isolated in-memory SQLite DB for deploy_health tests.

    Patches app.deploy_health.AsyncSessionLocal so _check_pause_mode uses this DB.
    Also patches out side-effectful helpers (alert creation, broadcast, webhook).
    """
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.deploy_health.AsyncSessionLocal", _Session)

    # Suppress all side-effects that require running infra
    mocker.patch("app.deploy_health._create_auto_pause_alerts", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._create_auto_resume_alerts", new_callable=AsyncMock)
    mocker.patch(
        "app.deploy_health._create_forced_resume_alerts", new_callable=AsyncMock
    )
    mocker.patch("app.deploy_health.broadcast_deploy_state", new_callable=AsyncMock)

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level streak counters before and after every test."""
    _dh._pause_failure_streak = 0
    _dh._reset_health_recovery_history()
    _dh._last_auto_resume_at = None
    _dh._recent_auto_resumes.clear()
    _dh._last_resume_was_forced = False
    _dh._consecutive_force_resumes = 0
    _shared_state.deploy_monitoring["task_results"].clear()
    yield
    _dh._pause_failure_streak = 0
    _dh._reset_health_recovery_history()
    _dh._last_auto_resume_at = None
    _dh._recent_auto_resumes.clear()
    _dh._last_resume_was_forced = False
    _dh._consecutive_force_resumes = 0
    _shared_state.deploy_monitoring["task_results"].clear()


def _seed_recovery_history(*values):
    """Seed the health recovery ring buffer with the given boolean values."""
    _dh._reset_health_recovery_history()
    for v in values:
        _dh._health_recovery_history.append(bool(v))


@pytest.fixture(autouse=True)
def reset_shared_state():
    """Restore shared_state fields touched by _check_pause_mode after each test."""
    original_ext = copy.deepcopy(_shared_state.external_health)
    original_rc = copy.deepcopy(_shared_state.runtime_config)
    original_dm = copy.deepcopy(_shared_state.deploy_monitoring)
    yield
    _shared_state.external_health.clear()
    _shared_state.external_health.update(original_ext)
    _shared_state.runtime_config.clear()
    _shared_state.runtime_config.update(original_rc)
    _shared_state.deploy_monitoring.clear()
    _shared_state.deploy_monitoring.update(original_dm)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_deploy_state(
    session,
    status=DeploymentStatus.STABLE,
    pause_reason=None,
    pre_pause_max_vms=None,
    paused_at=None,
):
    """Insert a DeploymentState singleton row (id=1) and return it."""
    state = DeploymentState(
        id=1,
        status=status.value,  # store the raw string value, not the enum repr
        pause_reason=pause_reason,
        pre_pause_max_vms=pre_pause_max_vms,
        paused_at=paused_at,
    )
    session.add(state)
    await session.commit()
    await session.refresh(state)
    return state


def _set_critical_failures(*deps):
    """Set failed_deps in external_health to the given dependency names."""
    _shared_state.external_health["failed_deps"] = list(deps)


def _set_healthy():
    """Clear external_health failures (all deps healthy)."""
    _shared_state.external_health["failed_deps"] = []


# ── Tests: failure streak (no pause until 3rd consecutive) ───────────────────


@pytest.mark.asyncio
async def test_first_failure_does_not_pause(dh_db):
    """A single critical failure increments the streak but does NOT trigger a pause."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("db")

    await _dh._check_pause_mode()

    assert _dh._pause_failure_streak == 1


@pytest.mark.asyncio
async def test_provider_specific_failure_without_dispatch_outage_does_not_pause(dh_db):
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("anthropic_api")

    await _dh._check_pause_mode()
    await _dh._check_pause_mode()
    await _dh._check_pause_mode()

    state = await dh_db.get(DeploymentState, 1)
    assert state.status == DeploymentStatus.STABLE.value
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


@pytest.mark.asyncio
async def test_non_anthropic_provider_failure_without_dispatch_outage_does_not_pause(
    dh_db,
):
    """A non-Anthropic provider-specific failure that does not produce a dispatch_api outage
    must not trigger auto-pause — only dispatch_api (and db/github/gcp) are critical."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("openai_api")

    await _dh._check_pause_mode()
    await _dh._check_pause_mode()
    await _dh._check_pause_mode()

    state = await dh_db.get(DeploymentState, 1)
    assert state.status == DeploymentStatus.STABLE.value
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


@pytest.mark.asyncio
async def test_dispatch_api_failure_triggers_pause_after_three_checks(dh_db):
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("dispatch_api")

    await _dh._check_pause_mode()
    await _dh._check_pause_mode()
    await _dh._check_pause_mode()

    state = await dh_db.get(DeploymentState, 1)
    assert state.status == DeploymentStatus.PAUSED.value
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0
    assert state.pre_pause_max_vms == 4
    assert state.pause_reason is not None
    assert "dispatch_api" in state.pause_reason


@pytest.mark.asyncio
async def test_second_consecutive_failure_does_not_pause(dh_db):
    """Two consecutive critical failures still do not trigger a pause."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("github_api")

    # Simulate one pre-existing failure
    _dh._pause_failure_streak = 1

    await _dh._check_pause_mode()

    assert _dh._pause_failure_streak == 2
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value


@pytest.mark.asyncio
async def test_streak_buildup_broadcasts_health_degrading(dh_db, mocker):
    """Sub-threshold failures broadcast health_degrading with streak/threshold/failed_deps."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("db", "gcp")

    bcast = mocker.patch(
        "app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock
    )
    await _dh._check_pause_mode()

    bcast.assert_awaited_once()
    event, payload = bcast.await_args.args
    assert event == "health_degrading"
    assert payload["streak"] == 1
    assert payload["threshold"] == 3
    assert payload["failed_deps"] == ["db", "gcp"]


@pytest.mark.asyncio
async def test_pause_threshold_does_not_broadcast_health_degrading(dh_db, mocker):
    """Once the streak reaches the pause threshold, no health_degrading event is sent."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("db")
    _dh._pause_failure_streak = 2

    bcast = mocker.patch(
        "app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock
    )
    await _dh._check_pause_mode()

    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_third_consecutive_failure_triggers_pause(dh_db):
    """Three consecutive critical failures must pause the pipeline."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("gcp", "db")

    _dh._pause_failure_streak = 2  # two already recorded

    await _dh._check_pause_mode()

    assert _dh._pause_failure_streak == 3
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value
    assert state.pre_pause_max_vms == 4
    assert state.pause_reason is not None
    assert "External dependency down" in state.pause_reason


@pytest.mark.asyncio
async def test_pause_creates_alerts(dh_db):
    """Alerts are created when the pipeline auto-pauses on the 3rd failure."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _set_critical_failures("dispatch_api")

    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    _dh._create_auto_pause_alerts.assert_awaited_once()


# ── Tests: failure streak reset on healthy cycle ──────────────────────────────


@pytest.mark.asyncio
async def test_failure_streak_resets_on_healthy_cycle(dh_db):
    """If health recovers before the pause threshold, the failure streak resets to 0."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_healthy()

    _dh._pause_failure_streak = 2  # two prior failures

    await _dh._check_pause_mode()

    assert _dh._pause_failure_streak == 0
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4  # still running


# ── Tests: recovery streak gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_first_healthy_cycle_does_not_resume(dh_db):
    """First healthy cycle while PAUSED increments recovery streak but does not resume."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_healthy()

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == [True]
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value


@pytest.mark.asyncio
async def test_recovery_second_healthy_cycle_does_not_resume(dh_db):
    """Second healthy cycle still does not resume — needs 3."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: github_api",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_healthy()

    _seed_recovery_history(True)  # one prior healthy cycle

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == [True, True]
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value


@pytest.mark.asyncio
async def test_recovery_third_healthy_cycle_resumes(dh_db):
    """Three consecutive healthy cycles trigger auto-resume."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _seed_recovery_history(True, True)  # two prior healthy cycles

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == []  # reset after resume
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2  # min(2, 4)

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value
    assert state.pause_reason is None
    assert state.pre_pause_max_vms is None


@pytest.mark.asyncio
async def test_resume_creates_alerts(dh_db):
    """Auto-resume should call _create_auto_resume_alerts."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=3,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    _dh._create_auto_resume_alerts.assert_awaited_once()


# ── Tests: pre-pause VM count restoration capped at 2 ────────────────────────


@pytest.mark.asyncio
async def test_resume_vm_count_capped_at_2(dh_db):
    """On resume, max_concurrent_vms is set to min(2, pre_pause_max_vms)."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=10,  # was running 10 VMs
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    # Even though pre_pause was 10, resume starts with min(2, 10) = 2
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2


@pytest.mark.asyncio
async def test_resume_vm_count_uses_pre_pause_when_less_than_2(dh_db):
    """If pre_pause_max_vms was 1, resume uses 1 (not 2)."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=1,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    assert _shared_state.runtime_config["max_concurrent_vms"] == 1  # min(2, 1) = 1


# ── Tests: manual pause guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_pause_is_not_auto_resumed(dh_db):
    """A pause initiated by an admin (pause_reason not starting with 'External dependency down')
    must NOT be auto-resumed by the health checker."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="Manual pause by admin",  # not the automated pattern
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_healthy()

    _seed_recovery_history(True, True)  # would normally trigger resume

    await _dh._check_pause_mode()

    # recovery history reset because it's not an automated pause
    assert list(_dh._health_recovery_history) == []
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value


@pytest.mark.asyncio
async def test_no_pause_reason_is_not_auto_resumed(dh_db):
    """A PAUSED state with no pause_reason is also treated as a manual pause."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason=None,
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_healthy()

    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused


# ── Tests: no DeploymentState row edge case ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_deploy_state_critical_failure_no_crash(dh_db):
    """When there is no DeploymentState row, a critical failure must not crash."""
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("db")

    _dh._pause_failure_streak = 2  # force to threshold

    # Should not raise even with no state row
    await _dh._check_pause_mode()

    # max_concurrent_vms unchanged since state was None
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


@pytest.mark.asyncio
async def test_no_deploy_state_healthy_no_crash(dh_db):
    """When there is no DeploymentState row, a healthy cycle must not crash."""
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_healthy()

    # Should not raise
    await _dh._check_pause_mode()

    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


# ── Tests: already PAUSED/HALTED state skips re-pause ────────────────────────


@pytest.mark.asyncio
async def test_already_paused_state_skips_re_pause(dh_db):
    """If the pipeline is already PAUSED, a third failure should not update state again."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=4,
    )
    # max_concurrent_vms is already 0
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_critical_failures("db")

    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    # Already at 0, state check: max_concurrent_vms > 0 is False, so no update
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0
    # Alert creation should not have been triggered (guard: max_concurrent_vms > 0)
    _dh._create_auto_pause_alerts.assert_not_awaited()


# ── Tests: ramp-up after auto-resume ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_rampup_increases_toward_target(dh_db):
    """After auto-resume, a healthy cycle with runtime < DB target ramps up by up to 2."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _shared_state.runtime_config["max_concurrent_vms"] = 2  # initial post-resume
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8  # reached target

    # No further ramp once target reached
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8


@pytest.mark.asyncio
async def test_rampup_partial_step_to_target(dh_db):
    """When only 1 VM short of target, ramp-up adds exactly 1 (not 2)."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.MONITORING)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 3
    await dh_db.commit()

    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 3


@pytest.mark.asyncio
async def test_rampup_skipped_when_paused_state(dh_db):
    """While PAUSED, ramp-up must not fire (auto-resume branch handles the transition)."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=8,
        paused_at=_dh._utcnow(),
    )
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    # First healthy cycle: ring buffer at 1/5, does not resume, does not ramp.
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0


@pytest.mark.asyncio
async def test_resume_followed_by_rampup_reaches_target(dh_db):
    """End-to-end: auto-resume sets runtime to min(2,target), subsequent cycles ramp up."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=6,
        paused_at=_dh._utcnow(),
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _seed_recovery_history(True, True)
    await _dh._check_pause_mode()  # triggers resume (3rd healthy probe)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2

    # Subsequent healthy cycles ramp up
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6  # held at target


@pytest.mark.asyncio
async def test_forced_resume_rampup_gated_without_task_success(dh_db):
    """After a forced resume, ramp-up past 2 VMs requires a recent successful task."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _dh._last_resume_was_forced = True
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _shared_state.deploy_monitoring["task_results"].clear()  # no successes
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2  # gated


@pytest.mark.asyncio
async def test_forced_resume_rampup_proceeds_with_task_success(dh_db):
    """A recent successful task unblocks forced-resume ramp-up."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _dh._last_resume_was_forced = True
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _shared_state.deploy_monitoring["task_results"].append(
        (_dh._utcnow().isoformat(), "success", None, "create")
    )
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


@pytest.mark.asyncio
async def test_forced_resume_gate_stale_success_does_not_count(dh_db):
    """A success older than 5 minutes does not satisfy the forced-resume gate."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _dh._last_resume_was_forced = True
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    from datetime import timedelta

    stale = (_dh._utcnow() - timedelta(minutes=10)).isoformat()
    _shared_state.deploy_monitoring["task_results"].append(
        (stale, "success", None, "create")
    )
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2  # gated


@pytest.mark.asyncio
async def test_normal_resume_rampup_unaffected_by_gate(dh_db):
    """After a normal (non-forced) resume, ramp-up proceeds without task-success evidence."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    state.runtime_max_concurrent_vms = 8
    await dh_db.commit()

    _dh._last_resume_was_forced = False
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _shared_state.deploy_monitoring["task_results"].clear()
    _set_healthy()

    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


# ── Tests: recovery ring buffer tolerates a single flaky probe ───────────────


@pytest.mark.asyncio
async def test_recovery_streak_resets_on_new_failure(dh_db):
    """A single flaky probe is recorded but prior healthy samples are retained (3-of-5 semantics)."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_critical_failures("gcp")  # dep fails again during recovery

    _seed_recovery_history(True, True)  # was building up

    await _dh._check_pause_mode()

    # Single flake is appended to history but does NOT reset the existing healthy samples.
    # This is the 3-of-5 semantics: one bad probe shouldn't erase progress toward recovery.
    assert list(_dh._health_recovery_history) == [True, True, False]
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused


# ── Tests: 3-of-5 recovery tolerates a flake ─────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_three_of_five_healthy_resumes(dh_db):
    """Auto-resume fires when 3 of the last 5 probes are healthy, even with a flake in the middle."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: github_api",
        pre_pause_max_vms=4,
        paused_at=_dh._utcnow(),
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    # Prior pattern: healthy, flaky, healthy — now a third healthy probe arrives.
    _seed_recovery_history(True, False, True)

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == []  # cleared after resume
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2  # min(2, 4)

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value
    assert state.pause_reason is None


@pytest.mark.asyncio
async def test_single_flake_does_not_reset_recovery(dh_db):
    """A single intermittent failure during recovery does not clear the ring buffer;
    the very next healthy probe after the flake pushes the count back to 3-of-5 and resumes."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=5,
        paused_at=_dh._utcnow(),
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None

    # Two healthy, then a flake: history is [T, T, F]. Still 2/3 healthy — no resume.
    _seed_recovery_history(True, True)
    _set_critical_failures("gcp")
    await _dh._check_pause_mode()
    assert list(_dh._health_recovery_history) == [True, True, False]
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0

    # Flake recovers — next cycle is healthy. History → [T, T, F, T]; 3-of-4 healthy → resume.
    _set_healthy()
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value


# ── Tests: forced resume after max auto-pause duration ───────────────────────


@pytest.mark.asyncio
async def test_forced_resume_after_max_duration(dh_db):
    """After max_auto_pause_duration_seconds, force-resume with 1 VM regardless of health state."""
    from datetime import timedelta

    long_ago = _dh._utcnow() - timedelta(seconds=10_000)  # well past default 7200s
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("dispatch_api")  # health probes still failing

    await _dh._check_pause_mode()

    # Force-resume path: 1 VM, history cleared, CRITICAL alert path invoked.
    assert _shared_state.runtime_config["max_concurrent_vms"] == 1
    assert list(_dh._health_recovery_history) == []
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value
    assert state.pause_reason is None
    _dh._create_forced_resume_alerts.assert_awaited_once()
    # Normal resume alert helper should NOT be called on the forced path.
    _dh._create_auto_resume_alerts.assert_not_awaited()


# ── Tests: anti-flap grace period after auto-resume ─────────────────────────


@pytest.mark.asyncio
async def test_grace_period_suppresses_repause(dh_db, mocker):
    """Health failures during the post-resume grace period must NOT re-trigger auto-pause."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _shared_state.runtime_config["auto_resume_grace_period_seconds"] = 300
    _set_critical_failures("db")

    # Simulate a very recent auto-resume.
    _dh._last_auto_resume_at = _dh._utcnow()
    _dh._pause_failure_streak = 2  # next failure would normally trigger pause

    bcast = mocker.patch(
        "app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock
    )
    await _dh._check_pause_mode()

    # Streak still increments and health_degrading still broadcasts so operators see flap.
    assert _dh._pause_failure_streak == 3
    bcast.assert_awaited_once()
    event, payload = bcast.await_args.args
    assert event == "health_degrading"
    assert payload["in_grace_period"] is True
    assert payload["grace_remaining_seconds"] > 0

    # But the pipeline is NOT paused.
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value
    _dh._create_auto_pause_alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_triggers_after_grace_period_expires(dh_db):
    """Health failures AFTER the grace period must re-trigger auto-pause normally."""
    from datetime import timedelta

    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _shared_state.runtime_config["auto_resume_grace_period_seconds"] = 300
    _set_critical_failures("gcp")

    # Last resume was well before the grace window.
    _dh._last_auto_resume_at = _dh._utcnow() - timedelta(seconds=600)
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    assert _dh._pause_failure_streak == 3
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value


@pytest.mark.asyncio
async def test_normal_auto_resume_sets_grace_timestamp(dh_db):
    """A normal (3-of-5) auto-resume must set _last_auto_resume_at for future grace checks."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()
    _seed_recovery_history(True, True)

    assert _dh._last_auto_resume_at is None
    await _dh._check_pause_mode()
    assert _dh._last_auto_resume_at is not None


@pytest.mark.asyncio
async def test_forced_resume_sets_grace_timestamp(dh_db):
    """A forced resume (max-duration exceeded) must also set _last_auto_resume_at."""
    from datetime import timedelta

    long_ago = _dh._utcnow() - timedelta(seconds=10_000)
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("dispatch_api")

    assert _dh._last_auto_resume_at is None
    await _dh._check_pause_mode()
    assert _dh._last_auto_resume_at is not None


# ── Tests: persisted-state restore on startup ────────────────────────────────


def test_restore_health_streaks_roundtrip():
    """_restore_health_streaks must rehydrate all three pieces of state from a snapshot."""
    from datetime import datetime as _dt

    resume_at = _dt(2026, 4, 17, 10, 30, 0)
    snapshot = {
        "_pause_failure_streak": 2,
        "_health_recovery_history": [True, False, True],
        "_last_auto_resume_at": resume_at.isoformat(),
    }
    _dh._restore_health_streaks(snapshot)
    assert _dh._pause_failure_streak == 2
    assert list(_dh._health_recovery_history) == [True, False, True]
    assert _dh._last_auto_resume_at == resume_at


def test_restore_health_streaks_tolerates_missing_keys():
    """Empty snapshot must restore to safe defaults (0 / empty / None)."""
    _dh._pause_failure_streak = 99
    _dh._health_recovery_history.append(True)
    _dh._last_auto_resume_at = _dh._utcnow()

    _dh._restore_health_streaks({})
    assert _dh._pause_failure_streak == 0
    assert list(_dh._health_recovery_history) == []
    assert _dh._last_auto_resume_at is None


def test_restore_health_streaks_tolerates_invalid_timestamp():
    """Corrupt ISO string for _last_auto_resume_at must fall back to None."""
    _dh._restore_health_streaks({"_last_auto_resume_at": "not-a-date"})
    assert _dh._last_auto_resume_at is None


@pytest.mark.asyncio
async def test_restored_grace_period_suppresses_repause_after_restart(dh_db, mocker):
    """Simulated restart: after restoring _last_auto_resume_at, grace period still applies."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _shared_state.runtime_config["auto_resume_grace_period_seconds"] = 300
    _set_critical_failures("db")

    # Persist a recent resume, then simulate restart by clearing module state and restoring.
    recent = _dh._utcnow()
    snapshot = {
        "_pause_failure_streak": 2,
        "_health_recovery_history": [],
        "_last_auto_resume_at": recent.isoformat(),
    }
    _dh._pause_failure_streak = 0
    _dh._last_auto_resume_at = None
    _dh._restore_health_streaks(snapshot)
    assert _dh._last_auto_resume_at is not None

    mocker.patch("app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock)
    await _dh._check_pause_mode()

    # Grace period honored — no pause triggered despite streak reaching 3.
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value


# ── Tests: nested pause preserves original pre-pause VM count ────────────────


@pytest.mark.asyncio
async def test_nested_pause_during_rampup_preserves_original_count(dh_db):
    """After normal auto-resume, a second pause during ramp-up must capture the
    ORIGINAL pre-pause capacity (from the DB ramp-up target), not the reduced
    runtime value. Prevents the staircase ratcheting bug."""
    # Simulate post-resume ramp-up state: DB holds target=10, runtime at 2.
    from sqlalchemy import update

    await _make_deploy_state(dh_db, status=DeploymentStatus.MONITORING)
    await dh_db.execute(
        update(DeploymentState)
        .where(DeploymentState.id == 1)
        .values(runtime_max_concurrent_vms=10)
    )
    await dh_db.commit()
    dh_db.expire_all()

    _shared_state.runtime_config["max_concurrent_vms"] = 2  # mid ramp-up
    _set_critical_failures("db")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    dh_db.expire_all()
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value
    # Must preserve the ORIGINAL target (10), NOT the intermediate runtime (2).
    assert state.pre_pause_max_vms == 10
    assert state.runtime_max_concurrent_vms == 10


@pytest.mark.asyncio
async def test_forced_resume_then_pause_preserves_original_count(dh_db):
    """Forced resume leaves the DB ramp-up target intact at the original; a subsequent
    pause must capture that original value, not the forced-resume's 1-VM runtime."""
    from datetime import timedelta

    long_ago = _dh._utcnow() - timedelta(seconds=10_000)
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("dispatch_api")

    # Trigger forced resume.
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 1

    dh_db.expire_all()
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    # Forced resume must preserve the ORIGINAL target (8) as runtime_max_concurrent_vms,
    # not the forced-resume's initial_vms (=1). Without this, a subsequent pause
    # during ramp-up captures 1 instead of 8.
    assert state.runtime_max_concurrent_vms == 8
    assert state.pre_pause_max_vms is None  # cleared by resume


@pytest.mark.asyncio
async def test_full_recovery_pause_captures_target(dh_db):
    """When ramp-up has fully reached the target, a fresh pause correctly captures
    that target (no leftover ratcheting or phantom state)."""
    from sqlalchemy import update

    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    await dh_db.execute(
        update(DeploymentState)
        .where(DeploymentState.id == 1)
        .values(runtime_max_concurrent_vms=6)
    )
    await dh_db.commit()
    dh_db.expire_all()

    # Fully ramped up: runtime == target.
    _shared_state.runtime_config["max_concurrent_vms"] = 6
    _set_critical_failures("gcp")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value
    assert state.pre_pause_max_vms == 6
    assert state.runtime_max_concurrent_vms == 6


# ── Tests: task_results staleness guard (auto-resume deadlock) ───────────────


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_check_gcp_health_stale_provision_failures_return_healthy():
    """Pre-pause PROVISION_FAILURE entries that have aged past the staleness window
    must not keep GCP health failing, or auto-resume deadlocks."""
    _shared_state.deploy_monitoring["task_results"] = deque(
        [
            (_ts(30), "failure", "PROVISION_FAILURE", "create"),
            (_ts(25), "failure", "PROVISION_FAILURE", "create"),
            (_ts(20), "failure", "PROVISION_FAILURE", "create"),
        ],
        maxlen=100,
    )
    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) is None
    assert ext["gcp"] is True


def test_check_gcp_health_fresh_provision_failures_return_failed():
    _shared_state.deploy_monitoring["task_results"] = deque(
        [
            (_ts(5), "failure", "PROVISION_FAILURE", "create"),
            (_ts(4), "failure", "PROVISION_FAILURE", "create"),
            (_ts(3), "failure", "PROVISION_FAILURE", "create"),
        ],
        maxlen=100,
    )
    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) == "gcp"
    assert ext["gcp"] is False


def test_check_gcp_health_ignores_local_runner_provision_failures():
    rows = [
        (_ts(5), "failure", "PROVISION_FAILURE", "strategic_review"),
        (_ts(4), "failure", "PROVISION_FAILURE", "strategic_review"),
        (_ts(3), "failure", "PROVISION_FAILURE", "design_doc_update"),
    ]
    _shared_state.deploy_monitoring["task_results"] = deque(rows, maxlen=100)
    _shared_state.deploy_monitoring["task_result_metadata"] = {
        row[0]: {"backend": "local_runner"} for row in rows
    }

    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) is None
    assert ext["gcp"] is True


def test_check_gcp_health_still_counts_cloud_failures_with_local_runner_noise():
    cloud_rows = [
        (_ts(5), "failure", "PROVISION_FAILURE", "create"),
        (_ts(4), "failure", "PROVISION_FAILURE", "create"),
        (_ts(3), "failure", "PROVISION_FAILURE", "create"),
    ]
    local_row = (_ts(2), "failure", "PROVISION_FAILURE", "strategic_review")
    _shared_state.deploy_monitoring["task_results"] = deque(
        [*cloud_rows, local_row], maxlen=100
    )
    _shared_state.deploy_monitoring["task_result_metadata"] = {
        **{row[0]: {"backend": "cloud"} for row in cloud_rows},
        local_row[0]: {"backend": "local_runner"},
    }

    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) == "gcp"
    assert ext["gcp"] is False


def _ts_naive(minutes_ago: float) -> str:
    """Production-faithful task_results timestamp.

    record_task_result() writes _utcnow().isoformat() — a tz-NAIVE string.
    The aware _ts() helper above predates the C10 fix and does NOT match
    production; use this for the boundary regression below.
    """
    from app.time_utils import _utcnow

    return (_utcnow() - timedelta(minutes=minutes_ago)).isoformat()


def test_check_gcp_health_naive_entries_counted_at_boundary():
    """Audit C10 regression: cutoff must be NAIVE so naive task_results entries
    near the staleness boundary aren't undercounted.

    Pre-fix, check_gcp_health built its cutoff from datetime.now(timezone.utc)
    (an aware '+00:00' string). String comparison against record_task_result()'s
    naive entries made same-instant naive entries sort BELOW the aware cutoff, so
    fresh failures just inside the 15-min window were dropped and the >=3 trip
    never fired. With the naive cutoff, three fresh naive PROVISION_FAILUREs trip
    the gate as intended.
    """
    _shared_state.deploy_monitoring["task_results"] = deque(
        [
            (_ts_naive(5), "failure", "PROVISION_FAILURE", "create"),
            (_ts_naive(4), "failure", "PROVISION_FAILURE", "create"),
            (_ts_naive(3), "failure", "PROVISION_FAILURE", "create"),
        ],
        maxlen=100,
    )
    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) == "gcp"
    assert ext["gcp"] is False


def test_check_provider_api_health_anthropic_stale_rate_limits_return_healthy(mocker):
    """Stale RATE_LIMIT entries must not keep Anthropic provider health failing."""
    mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=False)
    mocker.patch.object(_hcp, "_account_by_id", {})
    _shared_state.deploy_monitoring["task_results"] = deque(
        [
            (_ts(30), "failure", "RATE_LIMIT", "create"),
            (_ts(25), "failure", "RATE_LIMIT", "create"),
            (_ts(20), "failure", "RATE_LIMIT", "create"),
            (_ts(19), "failure", "RATE_LIMIT", "create"),
            (_ts(18), "failure", "RATE_LIMIT", "create"),
        ],
        maxlen=100,
    )
    ext = {"errors": {}}
    assert _hcp.check_provider_api_health(ext, "anthropic") is None
    assert ext["anthropic_api"] is True


def test_check_provider_api_health_anthropic_fresh_rate_limits_return_failed(mocker):
    mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=False)
    mocker.patch.object(_hcp, "_account_by_id", {})
    _shared_state.deploy_monitoring["task_results"] = deque(
        [
            (_ts(5), "failure", "RATE_LIMIT", "create"),
            (_ts(4), "failure", "RATE_LIMIT", "create"),
            (_ts(3), "failure", "RATE_LIMIT", "create"),
            (_ts(2), "failure", "RATE_LIMIT", "create"),
            (_ts(1), "failure", "RATE_LIMIT", "create"),
        ],
        maxlen=100,
    )
    ext = {"errors": {}}
    assert _hcp.check_provider_api_health(ext, "anthropic") == "anthropic_api"
    assert ext["anthropic_api"] is False


def test_check_provider_api_health_openai_down_when_no_account_can_dispatch(mocker):
    account = {
        "id": "acct-openai",
        "provider": "openai",
        "enabled": True,
        "runtime_secret": "sk-test",
        "runtime_ready": True,
    }
    mocker.patch.object(_hcp, "_account_by_id", {"acct-openai": account})
    mocker.patch.object(_hcp, "_account_available_for_model", return_value=False)
    _shared_state.runtime_config["category_routing"] = {
        "_default": {"providers": ["openai"], "effort": "medium"}
    }
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["provider_selection_mode"] = "exclusive"

    ext = {"errors": {}, "provider_health": {}}
    assert _hcp.check_provider_api_health(ext, "openai") == "openai_api"
    assert ext["openai_api"] is False
    assert ext["provider_health"]["openai"]["status"] == "down"


def test_get_dispatch_health_snapshot_degrades_when_primary_provider_down_but_fallback_available():
    _shared_state.runtime_config["category_routing"] = {
        "_default": {"providers": ["anthropic", "openai"], "effort": "medium"}
    }
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["task_type_model_preferences"] = {}
    _shared_state.runtime_config["task_type_models"] = {}
    _shared_state.runtime_config["provider_selection_mode"] = "preferred"

    ext = {
        "provider_health": {
            "anthropic": {"status": "down"},
            "openai": {"status": "ok"},
        }
    }

    snapshot = _hcp.get_dispatch_health_snapshot(ext)

    assert snapshot["status"] == "degraded"
    assert snapshot["required_providers"] == ["anthropic", "openai"]
    assert snapshot["available_providers"] == ["openai"]
    assert snapshot["down_providers"] == ["anthropic"]


def test_get_dispatch_health_snapshot_uses_task_type_models_to_infer_required_provider():
    from app.models import LLMProviderName
    from app.providers.registry import get_provider

    _shared_state.runtime_config["category_routing"] = {}
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["task_type_provider_preference"] = {}
    _shared_state.runtime_config["task_type_model_preferences"] = {}
    # Derive a CURRENT OpenAI tier model rather than hardcoding one:
    # infer_provider_for_model only matches models that equal a provider's
    # current tier mapping, so a hardcoded id goes stale on every model bump
    # (e.g. gpt-5.4 stopped matching when the defaults moved to gpt-5.6-*).
    openai_model = get_provider(LLMProviderName.OPENAI).tier_to_model(
        "sonnet", _shared_state.runtime_config
    )
    assert openai_model, "openai provider must define a sonnet-tier model"
    _shared_state.runtime_config["task_type_models"] = {"create": openai_model}
    _shared_state.runtime_config["cross_provider_fallback_enabled"] = False

    ext = {
        "provider_health": {
            "openai": {"status": "ok"},
        }
    }

    snapshot = _hcp.get_dispatch_health_snapshot(ext)

    assert snapshot["required_providers"] == ["openai"]
    assert snapshot["available_providers"] == ["openai"]


def test_get_dispatch_health_snapshot_down_when_all_providers_down():
    """When every relevant provider is down, dispatch status must be 'down' (not 'degraded')."""
    _shared_state.runtime_config["category_routing"] = {
        "_default": {"providers": ["anthropic", "openai"], "effort": "medium"}
    }
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["task_type_model_preferences"] = {}
    _shared_state.runtime_config["task_type_models"] = {}
    _shared_state.runtime_config["provider_selection_mode"] = "preferred"

    ext = {
        "provider_health": {
            "anthropic": {"status": "down"},
            "openai": {"status": "down"},
        }
    }

    snapshot = _hcp.get_dispatch_health_snapshot(ext)

    assert snapshot["status"] == "down"
    assert snapshot["available_providers"] == []
    assert set(snapshot["down_providers"]) == {"anthropic", "openai"}


def test_get_dispatch_health_snapshot_uses_task_type_model_preferences():
    """task_type_model_preferences (category routing) contributes to required providers."""
    _shared_state.runtime_config["category_routing"] = {}
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["task_type_provider_preference"] = {}
    _shared_state.runtime_config["task_type_model_preferences"] = {
        "create": [{"provider": "openai", "model": "gpt-5"}]
    }
    _shared_state.runtime_config["task_type_models"] = {}
    _shared_state.runtime_config["cross_provider_fallback_enabled"] = False

    ext = {
        "provider_health": {
            "openai": {"status": "ok"},
        }
    }

    snapshot = _hcp.get_dispatch_health_snapshot(ext)

    assert "openai" in snapshot["required_providers"]
    assert "openai" in snapshot["available_providers"]
    assert snapshot["status"] == "ok"


def test_get_dispatch_health_snapshot_uses_category_routing_candidates():
    _shared_state.runtime_config["category_routing"] = {
        "_default": {"providers": ["zai", "anthropic", "openai"], "effort": "medium"},
        "ops": {"providers": ["deepseek", "anthropic"], "effort": "low"},
    }
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["task_type_provider_preference"] = {}
    _shared_state.runtime_config["task_type_model_preferences"] = {}
    _shared_state.runtime_config["task_type_models"] = {}
    _shared_state.runtime_config["provider_selection_mode"] = "preferred"

    ext = {
        "provider_health": {
            "anthropic": {"status": "ok"},
            "deepseek": {"status": "ok"},
            "openai": {"status": "ok"},
            "zai": {"status": "ok"},
        }
    }

    snapshot = _hcp.get_dispatch_health_snapshot(ext)

    assert snapshot["status"] == "ok"
    assert snapshot["fallback_enabled"] is True
    assert set(snapshot["required_providers"]) == {
        "anthropic",
        "deepseek",
        "openai",
        "zai",
    }


def test_get_provider_health_snapshot_uses_category_effort_model_for_deepseek(
    mocker,
):
    mocker.patch.dict(os.environ, {}, clear=True)
    mocker.patch.object(_hcp, "_account_by_id", {})
    _shared_state.runtime_config["category_routing"] = {
        "ops": {"providers": ["deepseek", "anthropic"], "effort": "low"},
    }
    _shared_state.runtime_config["task_type_routing"] = {}
    _shared_state.runtime_config["provider_settings_by_effort"] = {
        "deepseek": {"low": {"model": "deepseek-v4-flash", "effort": "low"}}
    }
    _shared_state.runtime_config["provider_selection_mode"] = "preferred"

    snapshot = _hcp.get_provider_health_snapshot("deepseek")

    assert snapshot["primary_model"] == "deepseek-v4-flash"
    assert snapshot["dispatch_relevant"] is True


# ── Tests: check_github_health (passive + active modes) ───────────────────────


@pytest.mark.asyncio
async def test_check_github_health_passive_mode_recent_success(mocker):
    """Recent last_github_success (< 60s) → skip API call, report healthy."""
    now = datetime.now(timezone.utc)
    mocker.patch.object(
        _hcp._shared_state, "last_github_success", now - timedelta(seconds=30)
    )
    client_factory = mocker.patch("httpx.AsyncClient")

    ext = {"errors": {"github_api": "stale error"}}
    result = await _hcp.check_github_health(ext, now)

    assert result is None
    assert ext["github_api"] is True
    assert "github_api" not in ext["errors"]
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_check_github_health_active_mode_stale_success(mocker):
    """Stale last_github_success (> 60s) → active API probe, healthy on 200."""
    now = datetime.now(timezone.utc)
    mocker.patch.object(
        _hcp._shared_state, "last_github_success", now - timedelta(seconds=120)
    )
    mocker.patch("app.github.generate_github_app_jwt", return_value="jwt-token")

    mock_resp = mocker.MagicMock(status_code=200)
    mock_client = mocker.MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    mocker.patch("httpx.AsyncClient", return_value=mock_client)

    ext = {"errors": {}}
    result = await _hcp.check_github_health(ext, now)

    assert result is None
    assert ext["github_api"] is True
    assert _hcp._shared_state.last_github_success == now
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_github_health_active_mode_cold_start(mocker):
    """last_github_success is None → active probe runs."""
    now = datetime.now(timezone.utc)
    mocker.patch.object(_hcp._shared_state, "last_github_success", None)
    mocker.patch("app.github.generate_github_app_jwt", return_value="jwt-token")

    mock_resp = mocker.MagicMock(status_code=200)
    mock_client = mocker.MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    mocker.patch("httpx.AsyncClient", return_value=mock_client)

    ext = {"errors": {}}
    result = await _hcp.check_github_health(ext, now)

    assert result is None
    assert ext["github_api"] is True
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_github_health_active_mode_raises(mocker):
    """API call raises → return 'github_api', record error, set unhealthy."""
    now = datetime.now(timezone.utc)
    mocker.patch.object(
        _hcp._shared_state, "last_github_success", now - timedelta(seconds=120)
    )
    mocker.patch("app.github.generate_github_app_jwt", return_value="jwt-token")

    mock_client = mocker.MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
    mocker.patch("httpx.AsyncClient", return_value=mock_client)

    ext = {"errors": {}}
    result = await _hcp.check_github_health(ext, now)

    assert result == "github_api"
    assert ext["github_api"] is False
    assert "RuntimeError" in ext["errors"]["github_api"]
    assert "connection refused" in ext["errors"]["github_api"]


@pytest.mark.asyncio
async def test_check_github_health_active_mode_non_200(mocker):
    """API returns non-200 → return 'github_api' with HTTP status in error."""
    now = datetime.now(timezone.utc)
    mocker.patch.object(
        _hcp._shared_state, "last_github_success", now - timedelta(seconds=120)
    )
    mocker.patch("app.github.generate_github_app_jwt", return_value="jwt-token")

    mock_resp = mocker.MagicMock(status_code=503)
    mock_client = mocker.MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    mocker.patch("httpx.AsyncClient", return_value=mock_client)

    ext = {"errors": {}}
    result = await _hcp.check_github_health(ext, now)

    assert result == "github_api"
    assert ext["github_api"] is False
    assert "503" in ext["errors"]["github_api"]


# ── Tests: check_db_health ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_db_health_success(mocker):
    """DB query succeeds → ext['db']=True and returns None."""
    mock_session = mocker.MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(return_value=mocker.MagicMock())
    mocker.patch(
        "app.health_check_primitives.AsyncSessionLocal", return_value=mock_session
    )

    ext = {"errors": {"db": "stale error"}}
    result = await _hcp.check_db_health(ext)

    assert result is None
    assert ext["db"] is True
    assert "db" not in ext["errors"]
    mock_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_db_health_failure(mocker):
    """DB query raises → ext['db']=False and returns 'db' with error recorded."""
    mock_session = mocker.MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(side_effect=RuntimeError("connection lost"))
    mocker.patch(
        "app.health_check_primitives.AsyncSessionLocal", return_value=mock_session
    )

    ext = {"errors": {}}
    result = await _hcp.check_db_health(ext)

    assert result == "db"
    assert ext["db"] is False
    assert "RuntimeError" in ext["errors"]["db"]
    assert "connection lost" in ext["errors"]["db"]


# ── Tests: flap detection ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_resume_no_flap_escalation(dh_db):
    """A normal single pause/resume records one entry but stays below the flap threshold."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()
    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    assert len(_dh._recent_auto_resumes) == 1
    # Not flapping yet → INFO alert (flapping=False)
    _dh._create_auto_resume_alerts.assert_awaited_once()
    _, kwargs = _dh._create_auto_resume_alerts.call_args
    assert kwargs.get("flapping", False) is False


@pytest.mark.asyncio
async def test_flap_escalation_requires_5_of_5_recovery(dh_db):
    """When already flapping (threshold met), recovery demands 5-of-5 healthy probes."""
    from datetime import timedelta

    now = _dh._utcnow()
    # Seed prior recent resumes so we are at the threshold BEFORE this cycle.
    _dh._recent_auto_resumes.extend(
        [
            now - timedelta(seconds=600),
            now - timedelta(seconds=400),
            now - timedelta(seconds=200),
        ]
    )
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3

    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()
    # 3-of-5 would normally resume; with flap escalation we need 5-of-5.
    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    # Still paused — 3-of-3 did not meet the escalated 5-of-5 bar.
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.PAUSED.value
    _dh._create_auto_resume_alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_flap_escalated_resume_creates_error_alert(dh_db):
    """After the 3rd resume lands inside the window, the auto-resume alert is ERROR-severity."""
    from datetime import timedelta

    now = _dh._utcnow()
    _dh._recent_auto_resumes.extend(
        [
            now - timedelta(seconds=600),
            now - timedelta(seconds=400),
        ]
    )
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3

    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()
    # Full 5-of-5 healthy to clear even the escalated bar.
    _seed_recovery_history(True, True, True, True)

    await _dh._check_pause_mode()

    # Third resume recorded; alert fired with flapping=True.
    assert len(_dh._recent_auto_resumes) == 3
    _dh._create_auto_resume_alerts.assert_awaited_once()
    _, kwargs = _dh._create_auto_resume_alerts.call_args
    assert kwargs.get("flapping") is True


def test_flap_window_prunes_stale_entries():
    """Entries older than flap_window_seconds are dropped on the next prune."""
    from datetime import timedelta

    _shared_state.runtime_config["flap_window_seconds"] = 300
    _shared_state.runtime_config["flap_threshold"] = 3
    now = _dh._utcnow()
    _dh._recent_auto_resumes.extend(
        [
            now - timedelta(seconds=10_000),  # very stale
            now - timedelta(seconds=5_000),  # stale
            now - timedelta(seconds=60),  # fresh
        ]
    )

    _dh._prune_flap_window(now)

    assert len(_dh._recent_auto_resumes) == 1
    assert _dh._is_flapping(now) is False


@pytest.mark.asyncio
async def test_flap_doubles_grace_period(dh_db, mocker):
    """While flapping, the post-resume grace period that suppresses re-pause is doubled."""
    from datetime import timedelta

    mocker.patch("app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock)

    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _shared_state.runtime_config["auto_resume_grace_period_seconds"] = 300
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3

    now = _dh._utcnow()
    # Trip flap threshold.
    _dh._recent_auto_resumes.extend(
        [
            now - timedelta(seconds=600),
            now - timedelta(seconds=400),
            now - timedelta(seconds=200),
        ]
    )
    # Resumed 400s ago — past the 300s base grace, but within 2*300s=600s flap-doubled grace.
    _dh._last_auto_resume_at = now - timedelta(seconds=400)

    _set_critical_failures("db")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    # No pause — doubled grace period absorbed the reading.
    state = (
        (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )
    assert state.status == DeploymentStatus.STABLE.value
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


@pytest.mark.asyncio
async def test_external_health_uses_short_interval_when_paused(mocker):
    """While paused (max_concurrent_vms == 0), the probe interval drops to 15s."""
    from datetime import datetime as _dt, timedelta as _td

    mocker.patch(
        "app.deploy_health.check_db_health", new_callable=AsyncMock, return_value=None
    )
    mocker.patch(
        "app.deploy_health.check_github_health",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch("app.deploy_health.check_provider_api_health", return_value=None)
    mocker.patch("app.deploy_health.check_gcp_health", return_value=None)
    mocker.patch("app.deploy_health.recheck_disabled_accounts", new_callable=AsyncMock)
    mocker.patch("app.deploy_health.log_zone_health_transitions")
    mocker.patch(
        "app.deploy_health._get_zone_health_summary",
        return_value={"total_zones": 0, "available": 0, "exhausted": []},
    )
    now_mock = _dt(2026, 4, 17, 12, 0, 0)
    mocker.patch("app.deploy_health._utcnow", return_value=now_mock)

    _shared_state.runtime_config["max_concurrent_vms"] = 0

    # Paused: 20s since last check → 15s interval elapsed, probe runs.
    _shared_state.external_health["last_check"] = now_mock - _td(seconds=20)
    await _dh._check_external_health()
    assert _shared_state.external_health["last_check"] == now_mock

    # Paused: 10s since last check → under 15s, probe skipped.
    _shared_state.external_health["last_check"] = now_mock - _td(seconds=10)
    await _dh._check_external_health()
    assert _shared_state.external_health["last_check"] == now_mock - _td(seconds=10)


@pytest.mark.asyncio
async def test_external_health_uses_normal_interval_when_active(mocker):
    """Active pipeline (max_concurrent_vms > 0) keeps the 60s probe cadence."""
    from datetime import datetime as _dt, timedelta as _td

    mocker.patch(
        "app.deploy_health.check_db_health", new_callable=AsyncMock, return_value=None
    )
    mocker.patch(
        "app.deploy_health.check_github_health",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch("app.deploy_health.check_provider_api_health", return_value=None)
    mocker.patch("app.deploy_health.check_gcp_health", return_value=None)
    mocker.patch("app.deploy_health.recheck_disabled_accounts", new_callable=AsyncMock)
    mocker.patch("app.deploy_health.log_zone_health_transitions")
    mocker.patch(
        "app.deploy_health._get_zone_health_summary",
        return_value={"total_zones": 0, "available": 0, "exhausted": []},
    )
    now_mock = _dt(2026, 4, 17, 12, 0, 0)
    mocker.patch("app.deploy_health._utcnow", return_value=now_mock)

    _shared_state.runtime_config["max_concurrent_vms"] = 4

    # Active: 20s since last check → under 60s, probe skipped.
    _shared_state.external_health["last_check"] = now_mock - _td(seconds=20)
    await _dh._check_external_health()
    assert _shared_state.external_health["last_check"] == now_mock - _td(seconds=20)

    # Active: 65s since last check → probe runs.
    _shared_state.external_health["last_check"] = now_mock - _td(seconds=65)
    await _dh._check_external_health()
    assert _shared_state.external_health["last_check"] == now_mock


def test_restore_health_streaks_restores_recent_auto_resumes():
    """_restore_health_streaks rehydrates the flap-detection deque."""
    from datetime import datetime as _dt

    t1 = _dt(2026, 4, 17, 10, 30, 0)
    t2 = _dt(2026, 4, 17, 10, 40, 0)
    snapshot = {"_recent_auto_resumes": [t1.isoformat(), t2.isoformat(), "not-a-date"]}
    _dh._restore_health_streaks(snapshot)
    assert list(_dh._recent_auto_resumes) == [t1, t2]


@pytest.mark.asyncio
async def test_orchestrator_persist_zone_health_round_trips_recent_auto_resumes(mocker):
    """orchestrator._persist_zone_health must save _recent_auto_resumes so restore can rehydrate it."""
    from datetime import datetime as _dt
    from unittest.mock import MagicMock
    import threading
    from collections import deque as _deque
    from app.orchestrator import _persist_zone_health as _orch_persist

    mocker.patch("app.compute._zone_health", {})
    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    mocker.patch("app.deploy_health._health_recovery_history", _deque(maxlen=5))
    mocker.patch("app.deploy_health._last_auto_resume_at", None)

    t1 = _dt(2026, 4, 17, 11, 0, 0)
    t2 = _dt(2026, 4, 17, 11, 30, 0)
    mocker.patch("app.deploy_health._recent_auto_resumes", _deque([t1, t2]))

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    mocker.patch(
        "app.state_persistence.AsyncSessionLocal", return_value=mock_session_ctx
    )
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _orch_persist()

    snapshot = mock_state.zone_health_state
    assert snapshot["_recent_auto_resumes"] == [t1.isoformat(), t2.isoformat()]

    # Round-trip: clear and restore.
    _dh._recent_auto_resumes.clear()
    _dh._restore_health_streaks(snapshot)
    assert list(_dh._recent_auto_resumes) == [t1, t2]


@pytest.mark.asyncio
async def test_orchestrator_persist_zone_health_round_trips_last_resume_was_forced(
    mocker,
):
    """_last_resume_was_forced must survive persist/restore so forced-resume ramp gate holds across restarts."""
    import threading
    from collections import deque as _deque
    from unittest.mock import MagicMock
    from app.orchestrator import _persist_zone_health as _orch_persist

    mocker.patch("app.compute._zone_health", {})
    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    mocker.patch("app.deploy_health._health_recovery_history", _deque(maxlen=5))
    mocker.patch("app.deploy_health._last_auto_resume_at", None)
    mocker.patch("app.deploy_health._recent_auto_resumes", _deque())
    mocker.patch("app.deploy_health._last_resume_was_forced", True)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    mocker.patch(
        "app.state_persistence.AsyncSessionLocal", return_value=mock_session_ctx
    )
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _orch_persist()

    snapshot = mock_state.zone_health_state
    assert snapshot["_last_resume_was_forced"] is True

    _dh._last_resume_was_forced = False
    _dh._restore_health_streaks(snapshot)
    assert _dh._last_resume_was_forced is True

    # Missing key -> safe default False.
    _dh._last_resume_was_forced = True
    _dh._restore_health_streaks({})
    assert _dh._last_resume_was_forced is False


# ── Tests: consecutive force-resume counter and escalation ───────────────────


@pytest.mark.asyncio
async def test_force_resume_increments_consecutive_counter(dh_db):
    """Each force-resume increments _consecutive_force_resumes."""
    from datetime import timedelta

    long_ago = _dh._utcnow() - timedelta(seconds=10_000)
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: dispatch_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("dispatch_api")

    _dh._consecutive_force_resumes = 1  # simulating a prior force-resume

    await _dh._check_pause_mode()

    assert _dh._consecutive_force_resumes == 2
    _dh._create_forced_resume_alerts.assert_awaited_once()
    call = _dh._create_forced_resume_alerts.await_args
    # 4th positional arg is the counter.
    assert call.args[3] == 2


@pytest.mark.asyncio
async def test_normal_resume_resets_consecutive_force_counter(dh_db):
    """A normal (healthy) auto-resume resets the force-resume counter to 0."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: db",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _dh._consecutive_force_resumes = 2
    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    # Third healthy probe triggers a normal (not forced) resume; counter resets.
    assert _shared_state.runtime_config["max_concurrent_vms"] > 0
    assert _dh._consecutive_force_resumes == 0


@pytest.mark.asyncio
async def test_force_resume_escalation_threshold_changes_title(dh_db, mocker):
    """After 3 consecutive force-resumes, the alert title escalates to 'Prolonged pipeline stall'."""
    mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)

    # Seed a project so the alert helper has somewhere to write.
    from app.models import Alert
    import uuid as _uuid

    project = Project(id=_uuid.uuid4(), name="p", user_id="u")
    dh_db.add(project)
    await dh_db.commit()

    # Non-escalated case (counter = 1).
    await _REAL_CREATE_FORCED_RESUME_ALERTS(dh_db, 7200, ["dispatch_api"], 1)
    await dh_db.commit()
    alerts = (await dh_db.execute(select(Alert))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].title == "Pipeline force-resumed: max auto-pause duration exceeded"

    # Escalated case (counter = 3).
    await _REAL_CREATE_FORCED_RESUME_ALERTS(dh_db, 7200, ["dispatch_api"], 3)
    await dh_db.commit()
    alerts = (
        (await dh_db.execute(select(Alert).order_by(Alert.created_at))).scalars().all()
    )
    escalated = [a for a in alerts if a.title.startswith("Prolonged pipeline stall")]
    assert len(escalated) == 1
    assert "3 consecutive force-resumes" in escalated[0].title
    assert "persistent infrastructure failure" in escalated[0].message.lower()


def test_restore_health_streaks_restores_consecutive_force_resumes():
    """_restore_health_streaks rehydrates _consecutive_force_resumes across restarts."""
    _dh._consecutive_force_resumes = 0
    _dh._restore_health_streaks({"_consecutive_force_resumes": 5})
    assert _dh._consecutive_force_resumes == 5

    # Missing key -> safe default 0.
    _dh._consecutive_force_resumes = 7
    _dh._restore_health_streaks({})
    assert _dh._consecutive_force_resumes == 0


@pytest.mark.asyncio
async def test_orchestrator_persist_zone_health_round_trips_consecutive_force_resumes(
    mocker,
):
    """_consecutive_force_resumes must survive persist/restore so escalation counter holds across restarts."""
    import threading
    from collections import deque as _deque
    from unittest.mock import MagicMock
    from app.orchestrator import _persist_zone_health as _orch_persist

    mocker.patch("app.compute._zone_health", {})
    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    mocker.patch("app.deploy_health._health_recovery_history", _deque(maxlen=5))
    mocker.patch("app.deploy_health._last_auto_resume_at", None)
    mocker.patch("app.deploy_health._recent_auto_resumes", _deque())
    mocker.patch("app.deploy_health._last_resume_was_forced", False)
    mocker.patch("app.deploy_health._consecutive_force_resumes", 4)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    mocker.patch(
        "app.state_persistence.AsyncSessionLocal", return_value=mock_session_ctx
    )
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _orch_persist()

    snapshot = mock_state.zone_health_state
    assert snapshot["_consecutive_force_resumes"] == 4

    _dh._consecutive_force_resumes = 0
    _dh._restore_health_streaks(snapshot)
    assert _dh._consecutive_force_resumes == 4
