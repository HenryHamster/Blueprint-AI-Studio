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

import app.routers.admin_runners as admin_runners_mod
import app.routers.runner as runner_mod
from app.models import (
    LocalRunner,
    Project,
    RunnerHeartbeatSnapshot,
    _utcnow,
)
from app.routers.runner import HeartbeatRequest, _persist_heartbeat_snapshot, heartbeat
from app.runner_health_monitors import (
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
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


def test_liveness_constant_shared_from_canonical_source():
    """runner.py and admin_runners.py must import _RUNNER_LIVENESS_SECONDS
    and _RUNNER_DEGRADED_FAILURE_THRESHOLD from runner_health_monitors, not
    define their own copies — ensures a single change propagates to all consumers.
    """
    assert runner_mod._RUNNER_LIVENESS_SECONDS is _RUNNER_LIVENESS_SECONDS, (
        "runner.py defines its own _RUNNER_LIVENESS_SECONDS instead of importing "
        "from runner_health_monitors"
    )
    assert admin_runners_mod._RUNNER_LIVENESS_SECONDS is _RUNNER_LIVENESS_SECONDS, (
        "admin_runners.py defines its own _RUNNER_LIVENESS_SECONDS instead of "
        "importing from runner_health_monitors"
    )
    assert admin_runners_mod._RUNNER_DEGRADED_FAILURE_THRESHOLD is _RUNNER_DEGRADED_FAILURE_THRESHOLD, (
        "admin_runners.py defines its own degraded threshold instead of importing "
        "from runner_health_monitors"
    )
    # Confirm the canonical values remain correct.
    assert _RUNNER_LIVENESS_SECONDS == 300
    assert _RUNNER_DEGRADED_FAILURE_THRESHOLD == 3


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
async def test_heartbeat_persists_agent_unavailable_legs(db_session):
    """The per-leg breakdown map rides in capabilities JSONB alongside the
    compound reason string, so the fleet panel can render a per-leg checklist."""
    _project, runner = await _make_runner(db_session)

    legs = {
        "bwrap_sandbox": False,
        "agent_dist": True,
        "node_runtime": False,
        "docker": False,
    }
    await heartbeat(
        payload=HeartbeatRequest(
            agent_available=False,
            agent_unavailable_reason=(
                "bwrap sandbox unavailable; node runtime not functional; "
                "docker agent unavailable"
            ),
            agent_unavailable_legs=legs,
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.capabilities["agent_unavailable_legs"] == legs
    # The richer compound reason is stored verbatim.
    assert (
        runner.capabilities["agent_unavailable_reason"]
        == "bwrap sandbox unavailable; node runtime not functional; "
        "docker agent unavailable"
    )


@pytest.mark.asyncio
async def test_heartbeat_without_legs_is_tolerated(db_session):
    """An older runner that omits agent_unavailable_legs heartbeats unchanged —
    the key is simply absent from capabilities, no error."""
    _project, runner = await _make_runner(db_session)

    resp = await heartbeat(
        payload=HeartbeatRequest(agent_available=True),
        runner=runner,
        session=db_session,
    )
    assert resp["ok"] is True
    await db_session.refresh(runner)
    assert "agent_unavailable_legs" not in runner.capabilities


@pytest.mark.asyncio
async def test_heartbeat_without_report_does_not_refresh_stamp(db_session):
    """A heartbeat that is NOT a genuine active-work report (active_work_report
    False) must NOT refresh active_work_reported_at nor clobber the active set.

    Regression for the reaper zombie-override hazard (interleaving 1): stamping
    the report timestamp on a non-report makes the reaper trust a stale/absent
    report as fresh. The first (genuine) report establishes the stamp+set; the
    second (non-report) heartbeat must leave BOTH untouched.
    """
    _project, runner = await _make_runner(db_session)

    # 1) Genuine report: stamps active_work_reported_at + records the live set.
    await heartbeat(
        payload=HeartbeatRequest(
            active_task_ids=["live-task"], active_work_report=True
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    stamp = runner.capabilities.get("active_work_reported_at")
    assert stamp is not None
    assert runner.capabilities.get("active_task_ids") == ["live-task"]

    # 2) Non-report heartbeat (e.g. a probe): empty set, active_work_report False.
    await heartbeat(
        payload=HeartbeatRequest(active_task_ids=[], active_work_report=False),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    # Stamp is NOT refreshed and the live set is NOT zeroed by the non-report.
    assert runner.capabilities.get("active_work_reported_at") == stamp
    assert runner.capabilities.get("active_task_ids") == ["live-task"]


@pytest.mark.asyncio
async def test_check_preflight_does_not_zero_live_task_claim(db_session):
    """A --check preflight (active_task_ids=[], active_work_report=False) must not
    let the reaper conclude a live task is unclaimed and reap it.

    Regression for the reaper zombie-override hazard (interleaving 2): builds the
    reaper's runner-report lookup from the runner row AFTER a genuine report then
    a preflight, and asserts the live task is still NOT excluded by the
    zombie-override predicate.
    """
    from datetime import datetime

    from app.reaper_orphan_classify import (
        _build_runner_report_lookup,
        _runner_report_excludes_task,
    )

    _project, runner = await _make_runner(db_session)

    # 1) Genuine report claiming the live task.
    await heartbeat(
        payload=HeartbeatRequest(
            active_task_ids=["live-task"], active_work_report=True
        ),
        runner=runner,
        session=db_session,
    )
    # 2) A --check preflight probe arrives (empty set, non-report).
    await heartbeat(
        payload=HeartbeatRequest(active_task_ids=[], active_work_report=False),
        runner=runner,
        session=db_session,
    )

    vm_id = f"runner:{runner.id}"
    lookup = await _build_runner_report_lookup(db_session, {vm_id})
    assert vm_id in lookup, "runner report should survive the preflight"
    reported_at, active_ids = lookup[vm_id]
    assert "live-task" in active_ids

    # The live task, claimed well before the (still-fresh) report, must NOT be
    # excluded by the zombie-override — so the reaper leaves it alone.
    class _Task:
        id = "live-task"
        vm_instance_id = vm_id
        started_at = reported_at - timedelta(hours=1)

    hb_cutoff = reported_at - timedelta(seconds=300)
    assert (
        _runner_report_excludes_task(_Task(), hb_cutoff, lookup) is False
    )
    assert isinstance(reported_at, datetime)


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
async def test_heartbeat_response_carries_live_safe_config(db_session):
    """The heartbeat RESPONSE carries the server-authoritative LIVE-SAFE config
    block (the daemon applies it mid-flight, no restart) plus a deterministic
    ``config_generation`` marker, alongside the backward-compatible
    ``enrolled_max_concurrent`` / ``version_hint`` an older daemon reads.

    This is the orchestrator half of the config push the salvaged findings ask
    for: an operator changes ``max_concurrent`` / GPU allowlist on the row and
    the running daemon learns of it from the next heartbeat response.
    """
    from app.runner_config import config_generation, live_safe_config

    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": ["fix", "create"],
            "max_concurrent": 3,
            "max_concurrent_compute": 1,
            "gpu_indices_allowed": [0, 1],
        },
    )

    resp = await heartbeat(
        payload=HeartbeatRequest(), runner=runner, session=db_session
    )

    # Backward-compat fields older daemons read.
    assert resp["enrolled_max_concurrent"] == 3
    assert "version_hint" in resp

    # New two-way config channel: the desired live-safe subset + its generation.
    config = resp["config"]
    assert config["max_concurrent"] == 3
    assert config["max_concurrent_compute"] == 1
    assert config["task_types"] == ["fix", "create"]
    assert config["gpu_indices_allowed"] == [0, 1]
    expected_generation = config_generation(live_safe_config(runner.capabilities))
    assert config["config_generation"] == expected_generation
    # Also mirrored at the top level so a daemon can compare without descending.
    assert resp["config_generation"] == expected_generation


@pytest.mark.asyncio
async def test_heartbeat_ingests_applied_config_report_without_clobbering_desired(
    db_session,
):
    """The heartbeat REQUEST carries the daemon's applied-config report. The
    handler stores it under dedicated caps keys (``applied_config`` /
    ``applied_config_generation``) so the Fleet UI can tell desired-vs-applied
    apart — and must NOT overwrite the operator-configured DESIRED subset
    (``max_concurrent`` / ``task_types`` / ``gpu_indices_allowed``)."""
    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": ["fix"],
            "max_concurrent": 4,
            "gpu_indices_allowed": [2],
        },
    )

    await heartbeat(
        payload=HeartbeatRequest(
            applied_config_generation="gen-applied-1",
            applied_max_concurrent=4,
            applied_max_concurrent_compute=4,
            applied_task_types=["fix"],
            applied_gpu_indices_allowed=[2],
        ),
        runner=runner,
        session=db_session,
    )

    await db_session.refresh(runner)
    caps = runner.capabilities
    # Applied report landed under its dedicated keys.
    assert caps["applied_config_generation"] == "gen-applied-1"
    assert caps["applied_config"]["max_concurrent"] == 4
    assert caps["applied_config"]["max_concurrent_compute"] == 4
    assert caps["applied_config"]["task_types"] == ["fix"]
    assert caps["applied_config"]["gpu_indices_allowed"] == [2]
    # The operator-owned DESIRED subset is untouched by the applied report.
    assert caps["max_concurrent"] == 4
    assert caps["task_types"] == ["fix"]
    assert caps["gpu_indices_allowed"] == [2]


@pytest.mark.asyncio
async def test_heartbeat_config_generation_tracks_gpu_allowlist_change(db_session):
    """The response ``config_generation`` changes iff the live-safe subset
    changes — so a daemon (and the Fleet UI) can detect a pushed GPU-allowlist
    edit by comparing generations, without diffing every field."""
    from app.routers.admin_runners import _normalize_gpu_indices_allowed

    _project, runner = await _make_runner(
        db_session,
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "gpu_indices_allowed": [0]},
    )

    first = await heartbeat(
        payload=HeartbeatRequest(), runner=runner, session=db_session
    )
    gen_before = first["config_generation"]

    # Operator widens the GPU allowlist on the row (as the admin PATCH does).
    caps = dict(runner.capabilities)
    caps["gpu_indices_allowed"] = _normalize_gpu_indices_allowed([0, 1])
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    second = await heartbeat(
        payload=HeartbeatRequest(), runner=runner, session=db_session
    )
    assert second["config_generation"] != gen_before
    assert second["config"]["gpu_indices_allowed"] == [0, 1]


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
