"""Tests for runner heartbeat time-series persistence + post-update resilience.

Covers the production seams added alongside the ``RunnerHeartbeatSnapshot``
time-series table:

  * ``routers.runner.heartbeat`` appends one snapshot row per heartbeat (the
    fire-and-forget ``_persist_heartbeat_snapshot`` helper) WITHOUT disturbing
    the in-place ``capabilities`` update that stays the dispatch source of
    truth.
  * ``_persist_heartbeat_snapshot`` is best-effort: a snapshot failure never
    fails the heartbeat.
  * The heartbeat handler stamps ``capabilities['version_changed_at']`` when a
    runner reports a NEW version, so the post-update silent-offline sweep can
    detect a runner that self-updated and then went dark.
  * ``_cleanup_stale_heartbeat_snapshots`` trims rows past the retention window.
  * ``_check_post_update_silent_runners`` raises/resolves the post-update
    silent alert.
"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.routers.runner as runner_mod
from app.models import (
    LocalRunner,
    Project,
    RunnerHeartbeatSnapshot,
    _utcnow,
)
from app.routers.runner import HeartbeatRequest, _persist_heartbeat_snapshot, heartbeat
from app.runner_health_monitors import (
    _check_post_update_silent_runners,
    _cleanup_stale_heartbeat_snapshots,
)


async def _make_runner(session, *, version="v1", capabilities=None, last_seen_at=None):
    project = Project(name="Heartbeat Project", user_id="test-user-hb")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="0" * 64,
        hostname="hb-host",
        version=version,
        status="active",
        capabilities=capabilities if capabilities is not None else {
            "task_types": "all",
            "has_gpu": True,
            "max_concurrent": 2,
        },
        last_seen_at=last_seen_at,
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return project, runner


@pytest.fixture(autouse=True)
def _silence_heartbeat_side_effects(monkeypatch):
    """Stub the broadcast/strategic side effects so the handler runs in isolation."""
    monkeypatch.setattr(
        runner_mod, "emit_workbench_refetch", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        runner_mod,
        "refresh_strategic_local_continuation_on_runner_heartbeat",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_heartbeat_persists_snapshot_row(db_session):
    """A heartbeat writes exactly one snapshot row carrying its telemetry, and
    still updates the in-place capabilities dict (dispatch source of truth)."""
    _project, runner = await _make_runner(db_session)

    payload = HeartbeatRequest(
        active_task_ids=["t1", "t2"],
        active_compute_job_ids=["j1"],
        agent_available=True,
        gpu_utilization_pct=73,
        disk_free_bytes=5_000_000_000,
        memory_free_bytes=2_000_000_000,
        cpu_load_avg=1.25,
        version="v1",
    )
    resp = await heartbeat(payload=payload, runner=runner, session=db_session)
    assert resp["ok"] is True

    rows = (
        await db_session.execute(
            select(RunnerHeartbeatSnapshot).where(
                RunnerHeartbeatSnapshot.runner_id == runner.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    snap = rows[0]
    assert snap.gpu_utilization_pct == 73
    assert snap.disk_free_bytes == 5_000_000_000
    assert snap.memory_free_bytes == 2_000_000_000
    assert snap.cpu_load_avg == pytest.approx(1.25)
    assert snap.active_task_count == 2
    assert snap.active_compute_job_count == 1
    assert snap.agent_available is True
    assert snap.captured_at is not None

    # The in-place capabilities update is untouched by the snapshot write.
    await db_session.refresh(runner)
    assert runner.capabilities["gpu_utilization_pct"] == 73
    assert runner.capabilities["active_task_ids"] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_snapshot_persist_is_best_effort(db_session, monkeypatch):
    """A failure inside snapshot persistence must be swallowed, not raised —
    the heartbeat response must never fail over telemetry history."""
    _project, runner = await _make_runner(db_session)

    # Force the commit inside the helper to blow up.
    boom = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(db_session, "commit", boom)

    # Must not raise despite the failing commit.
    await _persist_heartbeat_snapshot(
        db_session, runner_id=runner.id, caps={"active_task_ids": ["x"]}
    )


@pytest.mark.asyncio
async def test_heartbeat_stamps_version_changed_at_on_update(db_session):
    """When a runner reports a NEW version, the handler stamps version_changed_at
    + previous_version so the post-update-silence sweep can key on it."""
    _project, runner = await _make_runner(db_session, version="v1")

    payload = HeartbeatRequest(version="v2")
    await heartbeat(payload=payload, runner=runner, session=db_session)

    await db_session.refresh(runner)
    assert runner.version == "v2"
    assert runner.capabilities.get("previous_version") == "v1"
    assert runner.capabilities.get("version_changed_at") is not None


@pytest.mark.asyncio
async def test_heartbeat_no_version_stamp_when_unchanged(db_session):
    """A steady-state heartbeat (same version) must NOT stamp version_changed_at,
    or every runner would look 'just updated' forever."""
    _project, runner = await _make_runner(db_session, version="v1")

    payload = HeartbeatRequest(version="v1")
    await heartbeat(payload=payload, runner=runner, session=db_session)

    await db_session.refresh(runner)
    assert "version_changed_at" not in runner.capabilities


@pytest.mark.asyncio
async def test_cleanup_trims_old_keeps_recent(db_session):
    """Retention cleanup deletes snapshots older than the window, keeps recent."""
    _project, runner = await _make_runner(db_session)
    now = _utcnow()

    old = RunnerHeartbeatSnapshot(
        runner_id=runner.id, captured_at=now - timedelta(days=30)
    )
    recent = RunnerHeartbeatSnapshot(
        runner_id=runner.id, captured_at=now - timedelta(hours=1)
    )
    db_session.add(old)
    db_session.add(recent)
    await db_session.commit()

    deleted = await _cleanup_stale_heartbeat_snapshots(db_session)
    assert deleted == 1

    remaining = (
        await db_session.execute(
            select(RunnerHeartbeatSnapshot).where(
                RunnerHeartbeatSnapshot.runner_id == runner.id
            )
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == recent.id


@pytest.mark.asyncio
async def test_post_update_silent_sweep_alerts_and_resolves(db_session, monkeypatch):
    """A runner that reported a new version then went silent triggers the alert;
    a live runner resolves it instead."""
    import app.alert_dispatch as alert_mod

    create_mock = AsyncMock(return_value=True)
    resolve_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        alert_mod, "_maybe_create_runner_post_update_silent_alert", create_mock
    )
    monkeypatch.setattr(
        alert_mod, "_resolve_runner_post_update_silent_alert", resolve_mock
    )

    now = _utcnow()
    # (a) recently updated + (b) silent → alert.
    _p1, silent_runner = await _make_runner(
        db_session,
        capabilities={
            "version_changed_at": (now - timedelta(seconds=60)).isoformat(),
            "previous_version": "v1",
        },
        last_seen_at=now - timedelta(seconds=1200),  # past liveness window
    )

    await _check_post_update_silent_runners(db_session)

    assert create_mock.await_count == 1
    kwargs = create_mock.await_args.kwargs
    assert kwargs["runner_id"] == silent_runner.id
    assert kwargs["old_version"] == "v1"

    # A live runner (recent heartbeat) resolves rather than alerts.
    create_mock.reset_mock()
    _p2, _live_runner = await _make_runner(
        db_session,
        capabilities={
            "version_changed_at": (now - timedelta(seconds=60)).isoformat(),
        },
        last_seen_at=now,  # live
    )
    await _check_post_update_silent_runners(db_session)
    # The live runner must not raise a new alert; it resolves any open one.
    assert all(
        call.kwargs["runner_id"] != _live_runner.id
        for call in create_mock.await_args_list
    )
    assert resolve_mock.await_count >= 1
