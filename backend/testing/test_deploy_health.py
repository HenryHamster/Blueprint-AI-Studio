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
    mocker.patch("app.deploy_health._create_forced_resume_alerts", new_callable=AsyncMock)
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
    yield
    _dh._pause_failure_streak = 0
    _dh._reset_health_recovery_history()
    _dh._last_auto_resume_at = None
    _dh._recent_auto_resumes.clear()


def _seed_recovery_history(*values):
    """Seed the health recovery ring buffer with the given boolean values."""
    _dh._reset_health_recovery_history()
    for v in values:
        _dh._health_recovery_history.append(bool(v))


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
async def test_streak_buildup_broadcasts_health_degrading(dh_db, mocker):
    """Sub-threshold failures broadcast health_degrading with streak/threshold/failed_deps."""
    await _make_deploy_state(dh_db, status=DeploymentStatus.STABLE)
    _shared_state.runtime_config["max_concurrent_vms"] = 4
    _set_critical_failures("db", "gcp")

    bcast = mocker.patch("app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock)
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

    bcast = mocker.patch("app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock)
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

    assert list(_dh._health_recovery_history) == [True]
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

    _seed_recovery_history(True)  # one prior healthy cycle

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == [True, True]
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

    _seed_recovery_history(True, True)  # two prior healthy cycles

    await _dh._check_pause_mode()

    assert list(_dh._health_recovery_history) == []  # reset after resume
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
        pause_reason="External dependency down: claude_api",
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
        pause_reason="External dependency down: claude_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("claude_api")  # health probes still failing

    await _dh._check_pause_mode()

    # Force-resume path: 1 VM, history cleared, CRITICAL alert path invoked.
    assert _shared_state.runtime_config["max_concurrent_vms"] == 1
    assert list(_dh._health_recovery_history) == []
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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

    bcast = mocker.patch("app.deploy_health.manager.broadcast_to_all", new_callable=AsyncMock)
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
        pause_reason="External dependency down: claude_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("claude_api")

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
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    await dh_db.execute(update(DeploymentState).where(DeploymentState.id == 1).values(runtime_max_concurrent_vms=10))
    await dh_db.commit()
    dh_db.expire_all()

    _shared_state.runtime_config["max_concurrent_vms"] = 2  # mid ramp-up
    _set_critical_failures("db")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    dh_db.expire_all()
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
        pause_reason="External dependency down: claude_api",
        pre_pause_max_vms=8,
        paused_at=long_ago,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.runtime_config["max_auto_pause_duration_seconds"] = 7200
    _shared_state.deploy_monitoring["window_start"] = None
    _set_critical_failures("claude_api")

    # Trigger forced resume.
    await _dh._check_pause_mode()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 1

    dh_db.expire_all()
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
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
    await dh_db.execute(update(DeploymentState).where(DeploymentState.id == 1).values(runtime_max_concurrent_vms=6))
    await dh_db.commit()
    dh_db.expire_all()

    # Fully ramped up: runtime == target.
    _shared_state.runtime_config["max_concurrent_vms"] = 6
    _set_critical_failures("gcp")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.PAUSED.value
    assert state.pre_pause_max_vms == 6
    assert state.runtime_max_concurrent_vms == 6



# ── Tests: task_results staleness guard (auto-resume deadlock) ───────────────

import os
from datetime import datetime, timedelta, timezone
from collections import deque
import app.health_check_primitives as _hcp


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_check_gcp_health_stale_provision_failures_return_healthy():
    """Pre-pause PROVISION_FAILURE entries that have aged past the staleness window
    must not keep GCP health failing, or auto-resume deadlocks."""
    _shared_state.deploy_monitoring["task_results"] = deque([
        (_ts(30), "failure", "PROVISION_FAILURE", "create"),
        (_ts(25), "failure", "PROVISION_FAILURE", "create"),
        (_ts(20), "failure", "PROVISION_FAILURE", "create"),
    ], maxlen=100)
    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) is None
    assert ext["gcp"] is True


def test_check_gcp_health_fresh_provision_failures_return_failed():
    _shared_state.deploy_monitoring["task_results"] = deque([
        (_ts(5), "failure", "PROVISION_FAILURE", "create"),
        (_ts(4), "failure", "PROVISION_FAILURE", "create"),
        (_ts(3), "failure", "PROVISION_FAILURE", "create"),
    ], maxlen=100)
    ext = {"errors": {}}
    assert _hcp.check_gcp_health(ext) == "gcp"
    assert ext["gcp"] is False


def test_check_claude_api_health_stale_rate_limits_return_healthy(mocker):
    """Single-key path: stale RATE_LIMIT entries must not keep Claude API failing."""
    mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=False)
    mocker.patch.object(_hcp, "_account_by_id", {})
    _shared_state.deploy_monitoring["task_results"] = deque([
        (_ts(30), "failure", "RATE_LIMIT", "create"),
        (_ts(25), "failure", "RATE_LIMIT", "create"),
        (_ts(20), "failure", "RATE_LIMIT", "create"),
        (_ts(19), "failure", "RATE_LIMIT", "create"),
        (_ts(18), "failure", "RATE_LIMIT", "create"),
    ], maxlen=100)
    ext = {"errors": {}}
    assert _hcp.check_claude_api_health(ext) is None
    assert ext["claude_api"] is True


def test_check_claude_api_health_fresh_rate_limits_return_failed(mocker):
    mocker.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=False)
    mocker.patch.object(_hcp, "_account_by_id", {})
    _shared_state.deploy_monitoring["task_results"] = deque([
        (_ts(5), "failure", "RATE_LIMIT", "create"),
        (_ts(4), "failure", "RATE_LIMIT", "create"),
        (_ts(3), "failure", "RATE_LIMIT", "create"),
        (_ts(2), "failure", "RATE_LIMIT", "create"),
        (_ts(1), "failure", "RATE_LIMIT", "create"),
    ], maxlen=100)
    ext = {"errors": {}}
    assert _hcp.check_claude_api_health(ext) == "claude_api"
    assert ext["claude_api"] is False


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
    _dh._recent_auto_resumes.extend([
        now - timedelta(seconds=600),
        now - timedelta(seconds=400),
        now - timedelta(seconds=200),
    ])
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3

    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: claude_api",
        pre_pause_max_vms=4,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _shared_state.deploy_monitoring["window_start"] = None
    _set_healthy()
    # 3-of-5 would normally resume; with flap escalation we need 5-of-5.
    _seed_recovery_history(True, True)

    await _dh._check_pause_mode()

    # Still paused — 3-of-3 did not meet the escalated 5-of-5 bar.
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.PAUSED.value
    _dh._create_auto_resume_alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_flap_escalated_resume_creates_error_alert(dh_db):
    """After the 3rd resume lands inside the window, the auto-resume alert is ERROR-severity."""
    from datetime import timedelta
    now = _dh._utcnow()
    _dh._recent_auto_resumes.extend([
        now - timedelta(seconds=600),
        now - timedelta(seconds=400),
    ])
    _shared_state.runtime_config["flap_window_seconds"] = 1800
    _shared_state.runtime_config["flap_threshold"] = 3

    await _make_deploy_state(
        dh_db,
        status=DeploymentStatus.PAUSED,
        pause_reason="External dependency down: claude_api",
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
    _dh._recent_auto_resumes.extend([
        now - timedelta(seconds=10_000),  # very stale
        now - timedelta(seconds=5_000),   # stale
        now - timedelta(seconds=60),      # fresh
    ])

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
    _dh._recent_auto_resumes.extend([
        now - timedelta(seconds=600),
        now - timedelta(seconds=400),
        now - timedelta(seconds=200),
    ])
    # Resumed 400s ago — past the 300s base grace, but within 2*300s=600s flap-doubled grace.
    _dh._last_auto_resume_at = now - timedelta(seconds=400)

    _set_critical_failures("db")
    _dh._pause_failure_streak = 2

    await _dh._check_pause_mode()

    # No pause — doubled grace period absorbed the reading.
    state = (await dh_db.execute(select(DeploymentState).where(DeploymentState.id == 1))).scalars().first()
    assert state.status == DeploymentStatus.STABLE.value
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


def test_restore_health_streaks_restores_recent_auto_resumes():
    """_restore_health_streaks rehydrates the flap-detection deque."""
    from datetime import datetime as _dt
    t1 = _dt(2026, 4, 17, 10, 30, 0)
    t2 = _dt(2026, 4, 17, 10, 40, 0)
    snapshot = {"_recent_auto_resumes": [t1.isoformat(), t2.isoformat(), "not-a-date"]}
    _dh._restore_health_streaks(snapshot)
    assert list(_dh._recent_auto_resumes) == [t1, t2]
