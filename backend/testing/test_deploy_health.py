"""Tests for the _check_pause_mode state machine in backend/app/deploy_health.py.

Covers:
  1. First and second failure — logs warning but does NOT pause
  2. Third consecutive failure — triggers auto-pause (max_concurrent_vms=0)
  3. Auto-resume gate — 3 consecutive healthy cycles required before resuming
  4. Manual admin pause guard — auto-resume is skipped for non-automated pauses
  5. Failure streak resets to 0 on a healthy cycle
  6. Pre-pause VM count is restored (capped at 2) on resume
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.deploy_health as _dh
import app.shared_state as _shared_state
from app.models import DeploymentState, DeploymentStatus, Project

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler
if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)


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
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.deploy_health.AsyncSessionLocal", _Session)

    # Suppress all side-effects that require running infra
    mocker.patch("app.deploy_health._create_auto_pause_alerts", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._create_auto_resume_alerts", new_callable=AsyncMock)
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
    _dh._health_recovery_streak = 0
    yield
    _dh._pause_failure_streak = 0
    _dh._health_recovery_streak = 0


@pytest.fixture(autouse=True)
def reset_shared_state():
    """Restore shared_state fields touched by _check_pause_mode after each test."""
    original_ext = dict(_shared_state.external_health)
    original_rc = dict(_shared_state.runtime_config)
    original_dm = dict(_shared_state.deploy_monitoring)
    yield
    _shared_state.external_health.update(original_ext)
    _shared_state.runtime_config.update(original_rc)
    _shared_state.deploy_monitoring.update(original_dm)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _make_deploy_state(session, status=DeploymentStatus.STABLE,
                              pause_reason=None, pre_pause_max_vms=None,
                              paused_at=None):
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
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4  # unchanged

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.STABLE.value


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

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.STABLE.value


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

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.PAUSED.value
    assert state.pre_pause_max_vms == 4
    assert state.pause_reason is not None
    assert "External dependency down" in state.pause_reason


@pytest.mark.asyncio
async def test_pause_creates_alerts(dh_db):
    """Alerts are created when the pipeline auto-pauses on the 3rd failure."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 2
    _set_critical_failures("claude_api")

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

    assert _dh._health_recovery_streak == 1
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    _dh._health_recovery_streak = 1  # one prior healthy cycle

    await _dh._check_pause_mode()

    assert _dh._health_recovery_streak == 2
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    _dh._health_recovery_streak = 2  # two prior healthy cycles

    await _dh._check_pause_mode()

    assert _dh._health_recovery_streak == 0  # reset after resume
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2  # min(2, 4)

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    _dh._health_recovery_streak = 2

    await _dh._check_pause_mode()

    _dh._create_auto_resume_alerts.assert_awaited_once()


# ── Tests: pre-pause VM count restoration capped at 2 ────────────────────────

@pytest.mark.asyncio
async def test_resume_vm_count_capped_at_2(dh_db):
    """On resume, max_concurrent_vms is set to min(2, pre_pause_max_vms)."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: claude_api",
        pre_pause_max_vms=10,  # was running 10 VMs
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()

    _dh._health_recovery_streak = 2

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

    _dh._health_recovery_streak = 2

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

    _dh._health_recovery_streak = 2  # would normally trigger resume

    await _dh._check_pause_mode()

    # recovery streak reset because it's not an automated pause
    assert _dh._health_recovery_streak == 0
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    _dh._health_recovery_streak = 2

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


# ── Tests: recovery streak resets on new failure ─────────────────────────────

@pytest.mark.asyncio
async def test_recovery_streak_resets_on_new_failure(dh_db):
    """If a failure occurs while building the recovery streak, the streak resets to 0."""
    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: gcp",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _set_critical_failures("gcp")  # dep fails again during recovery

    _dh._health_recovery_streak = 2  # was building up

    await _dh._check_pause_mode()

    assert _dh._health_recovery_streak == 0  # reset
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0  # still paused
