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
    Alert,
    LocalRunner,
    Project,
    RunnerHeartbeatSnapshot,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.runner import (
    HeartbeatRequest,
    _acknowledge_authenticated_token_generation,
    _persist_heartbeat_snapshot,
    heartbeat,
)
from app.runner_health_monitors import (
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
    _get_degraded_failure_threshold,
    _get_liveness_seconds,
    _check_post_update_silent_runners,
    _cleanup_stale_heartbeat_snapshots,
)


@pytest.mark.asyncio
async def test_stale_in_flight_heartbeat_cannot_confirm_rotated_token(db_session):
    """An old-token handler reaching its CAS after rotation must be ignored."""
    project = Project(name="Token generation race", user_id="race-user")
    db_session.add(project)
    await db_session.flush()
    runner = LocalRunner(
        project_id=project.id,
        token_hash="0" * 64,
        token_generation=2,
        last_heartbeat_token_generation=0,
    )
    db_session.add(runner)
    await db_session.commit()

    # Generation 1 authenticated before rotation, but arrives at the final
    # acknowledgement after the row has advanced to generation 2.
    await _acknowledge_authenticated_token_generation(
        db_session,
        runner_id=runner.id,
        authenticated_generation=1,
    )
    await db_session.commit()
    await db_session.refresh(runner)
    assert runner.last_heartbeat_token_generation == 0

    await _acknowledge_authenticated_token_generation(
        db_session,
        runner_id=runner.id,
        authenticated_generation=2,
    )
    await db_session.commit()
    await db_session.refresh(runner)
    assert runner.last_heartbeat_token_generation == 2


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
    """Both routers must use the canonical liveness source.

    The runner heartbeat keeps the static compatibility constant while the
    admin health view intentionally calls the runtime-config-aware getter.
    """
    assert runner_mod._RUNNER_LIVENESS_SECONDS is _RUNNER_LIVENESS_SECONDS, (
        "runner.py defines its own _RUNNER_LIVENESS_SECONDS instead of importing "
        "from runner_health_monitors"
    )
    assert admin_runners_mod._get_liveness_seconds is _get_liveness_seconds, (
        "admin_runners.py must import the runtime-config-aware liveness getter "
        "from runner_health_monitors"
    )
    assert admin_runners_mod._get_degraded_failure_threshold is _get_degraded_failure_threshold, (
        "admin_runners.py must import the runtime-config-aware degraded threshold "
        "getter from runner_health_monitors"
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
        workspace_free_bytes=5_000_000_000,
        container_image_store_runtime="docker",
        container_image_store_path="/var/lib/docker",
        container_image_store_free_bytes=4_000_000_000,
        container_image_reclaim={
            "attempted_at": "2026-07-24T12:00:00Z",
            "runtime": "docker",
            "store_root": "/var/lib/docker",
            "free_bytes_before": 1_000_000,
            "free_bytes_after": 4_000_000_000,
            "removed": 3,
            "kept": 1,
            "in_use_skipped": 2,
            "reason": "removed stale images",
        },
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
    assert runner.capabilities["workspace_free_bytes"] == 5_000_000_000
    assert runner.capabilities["container_image_store_runtime"] == "docker"
    assert runner.capabilities["container_image_store_path"] == "/var/lib/docker"
    assert runner.capabilities["container_image_store_free_bytes"] == 4_000_000_000
    assert runner.capabilities["container_image_reclaim"] == {
        "attempted_at": "2026-07-24T12:00:00Z",
        "runtime": "docker",
        "store_root": "/var/lib/docker",
        "free_bytes_before": 1_000_000,
        "free_bytes_after": 4_000_000_000,
        "removed": 3,
        "kept": 1,
        "in_use_skipped": 2,
        "reason": "removed stale images",
    }


@pytest.mark.asyncio
async def test_heartbeat_persists_bounded_shared_resource_capacity(db_session):
    _project, runner = await _make_runner(db_session)
    await heartbeat(
        payload=HeartbeatRequest(
            resource_capacity={
                "enabled": True,
                "reservations": 3,
                "cpu": {"total_cores": 8, "reserved_cores": 3.5, "free_cores": 4.5},
                "memory": {"total_bytes": 16_000, "reserved_bytes": 6_000, "free_bytes": 7_000},
                "disk": {"total_bytes": 50_000, "reserved_bytes": 4_000, "free_bytes": 30_000},
                "gpu": {"total": 2, "reserved": 1, "free": 1},
                "unexpected": "discarded",
            }
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    capacity = runner.capabilities["resource_capacity"]
    assert capacity["enabled"] is True
    assert capacity["cpu"] == {
        "total_cores": 8.0,
        "reserved_cores": 3.5,
        "host_reserved_cores": None,
        "free_cores": 4.5,
    }
    assert capacity["memory"]["reserved_bytes"] == 6_000
    assert capacity["disk"]["reserved_bytes"] == 4_000
    assert capacity["gpu"] == {"total": 2, "reserved": 1, "free": 1}
    assert "unexpected" not in capacity


@pytest.mark.asyncio
async def test_explicit_null_image_store_telemetry_clears_stale_capacity(db_session):
    """A transient Docker-info failure must not retain an old low data-root."""
    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": "all",
            "disk_free_bytes": 20_000_000_000,
            "workspace_free_bytes": 20_000_000_000,
            "container_image_store_runtime": "docker",
            "container_image_store_path": "/var/lib/docker",
            "container_image_store_free_bytes": 1_000_000,
            "container_image_reclaim": {"reason": "old cleanup result"},
        },
    )

    # Omitted fields preserve compatibility with older runners and must not
    # erase a still-valid report.
    await heartbeat(
        payload=HeartbeatRequest(agent_available=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.capabilities["container_image_store_free_bytes"] == 1_000_000

    # local_runner sends each field as explicit null when the data-root probe is
    # unavailable. That must clear the old low reading so the legacy workspace
    # measurement is not permanently shadowed by stale Docker telemetry.
    await heartbeat(
        payload=HeartbeatRequest(
            container_image_store_runtime=None,
            container_image_store_path=None,
            container_image_store_free_bytes=None,
            container_image_reclaim=None,
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.capabilities["workspace_free_bytes"] == 20_000_000_000
    for key in (
        "container_image_store_runtime",
        "container_image_store_path",
        "container_image_store_free_bytes",
        "container_image_reclaim",
    ):
        assert key not in runner.capabilities


@pytest.mark.asyncio
async def test_heartbeat_persists_bounded_container_images_present(db_session):
    """The present-image inventory is stored sanitized and bounded, an omitted
    field keeps the previous report, and an explicit null clears it (a stale
    "image present" claim must never waive a real image-pull disk floor)."""
    _project, runner = await _make_runner(db_session)

    oversized = [f"repo/image:{i}" for i in range(150)]
    await heartbeat(
        payload=HeartbeatRequest(
            container_images_present=[
                "nvcr.io/nvidia/tensorflow:24.12-tf2-py3",
                "  python:3.12-slim  ",
                "",
                "x" * 1000,
                *oversized,
            ]
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    stored = runner.capabilities["container_images_present"]
    assert stored[0] == "nvcr.io/nvidia/tensorflow:24.12-tf2-py3"
    assert stored[1] == "python:3.12-slim"  # whitespace stripped
    assert "" not in stored  # blank entries dropped
    assert len(stored[2]) == 256  # per-entry length bound
    assert len(stored) == 100  # entry-count bound

    # Omission (older daemon / probe not run this tick) keeps the last report.
    await heartbeat(
        payload=HeartbeatRequest(agent_available=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert "container_images_present" in runner.capabilities

    # Explicit null clears it, same discipline as the image-store fields.
    await heartbeat(
        payload=HeartbeatRequest(container_images_present=None),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert "container_images_present" not in runner.capabilities


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
async def test_heartbeat_persists_normalized_project_verification(db_session):
    """Zero-exit v1 evidence is persisted without clobbering operator policy."""
    _project, runner = await _make_runner(db_session)
    report = {
        "version": 1,
        "boundary": {"class": "agent", "backend": "bwrap"},
        "ready": True,
        "reason": None,
        "probes": {
            "ruff": {"ok": True, "exit_code": 0, "reason": None},
            "docker_postgres": {"ok": True, "exit_code": 0, "reason": None},
        },
    }

    await heartbeat(
        payload=HeartbeatRequest(project_verification=report),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)

    assert runner.capabilities["project_verification"] == report
    assert runner.capabilities["task_types"] == "all"
    assert runner.capabilities["max_concurrent"] == 2


@pytest.mark.asyncio
async def test_heartbeat_derives_verification_ready_from_zero_exit_evidence(db_session):
    """A runner cannot mint readiness by claiming ready over a failed probe."""
    _project, runner = await _make_runner(db_session)
    await heartbeat(
        payload=HeartbeatRequest(
            project_verification={
                "version": 1,
                "boundary": {"class": "agent", "backend": "bwrap"},
                "ready": True,
                "probes": {
                    "ruff": {"ok": True, "exit_code": 0},
                    "docker_postgres": {
                        "ok": True,
                        "exit_code": 1,
                        "reason": "daemon inaccessible in bwrap",
                    },
                },
            }
        ),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)

    normalized = runner.capabilities["project_verification"]
    assert normalized["ready"] is False
    assert normalized["probes"]["ruff"]["ok"] is True
    assert normalized["probes"]["docker_postgres"] == {
        "ok": False,
        "exit_code": 1,
        "reason": "daemon inaccessible in bwrap",
    }


@pytest.mark.asyncio
async def test_old_runner_heartbeat_is_compatible_but_clears_stale_proof(db_session):
    """Omission stays 200-compatible and cannot retain a prior binary's proof."""
    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": "all",
            "project_verification": {"version": 1, "ready": True},
        },
    )

    response = await heartbeat(
        payload=HeartbeatRequest(active_work_report=True),
        runner=runner,
        session=db_session,
    )
    assert response["ok"] is True
    await db_session.refresh(runner)
    assert "project_verification" not in runner.capabilities


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
async def test_update_drain_heartbeat_releases_unpulled_pins(db_session):
    """A live-but-draining runner must not strand work in PROVISIONING."""
    project, runner = await _make_runner(db_session)
    unpulled = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        context={"runner_target_id": str(runner.id)},
        started_at=None,
    )
    pulled = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        context={"runner_target_id": str(runner.id)},
        started_at=_utcnow(),
    )
    db_session.add_all([unpulled, pulled])
    await db_session.commit()

    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    await db_session.refresh(unpulled)
    await db_session.refresh(pulled)

    assert runner.capabilities["update_draining"] is True
    assert unpulled.status == TaskStatus.QUEUED
    assert unpulled.vm_instance_id is None
    assert "runner_target_id" not in unpulled.context
    assert pulled.status == TaskStatus.PROVISIONING
    assert pulled.vm_instance_id == f"runner:{runner.id}"

    # Close the dispatcher-snapshot race: a pin created after the transition
    # heartbeat is released by the next heartbeat while drain remains active.
    late_pin = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        context={"runner_target_id": str(runner.id)},
        started_at=None,
    )
    db_session.add(late_pin)
    await db_session.commit()
    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(late_pin)
    assert late_pin.status == TaskStatus.QUEUED
    assert late_pin.vm_instance_id is None


@pytest.mark.asyncio
async def test_update_drain_heartbeat_keeps_unpulled_resident_pin(db_session):
    """An auto-update drain preserves only the canonical active Resident pin."""
    project, runner = await _make_runner(db_session)
    resident = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        vm_provisioned_at=_utcnow(),
        context={
            "_resident_session": True,
            "strategic_session_id": "ss-hard-pin",
            "_resident_worker_generation": 2,
            "runner_target_id": str(runner.id),
            "strategic_local_runner_required": True,
            "strategic_local_runner_reason": "resident_session",
        },
        started_at=None,
    )
    stale_marker = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        vm_provisioned_at=_utcnow(),
        context={
            "_resident_session": True,
            "strategic_session_id": "ss-stale-marker",
            "_resident_worker_generation": 1,
            "runner_target_id": str(runner.id),
            "preferred_runner_target_id": str(runner.id),
        },
        started_at=None,
    )
    project.vision = {
        **(project.vision or {}),
        "_resident_strategic_session": {
            "session_id": "ss-hard-pin",
            "task_id": str(resident.id),
            "worker_generation": 2,
            "runner_id": str(runner.id),
            "status": "active",
            "stop_requested": False,
        },
    }
    db_session.add_all([project, resident, stale_marker])
    await db_session.commit()
    pinned_at = resident.vm_provisioned_at
    pinned_context = dict(resident.context or {})

    for _ in range(2):
        await heartbeat(
            payload=HeartbeatRequest(update_draining=True),
            runner=runner,
            session=db_session,
        )
    await db_session.refresh(resident)
    await db_session.refresh(stale_marker)

    assert resident.status == TaskStatus.PROVISIONING
    assert resident.vm_instance_id == f"runner:{runner.id}"
    assert resident.vm_provisioned_at == pinned_at
    assert resident.context == pinned_context
    assert stale_marker.status == TaskStatus.QUEUED
    assert stale_marker.vm_instance_id is None
    assert "runner_target_id" not in stale_marker.context


def test_update_drain_stuck_literal_matches_shared_constant():
    """The heartbeat writes ``event_type`` as a literal; pin it to the constant.

    ``test_message_audience`` resolves an alert's ``event_type`` only from a
    same-module ``NAME = "..."`` assignment, so the heartbeat cannot reference
    the shared constant through its lazy cross-module import without reading
    as an unregistered event type. It therefore spells the string out. This
    pin fails if the literal and the shared constant ever drift apart.
    """
    from pathlib import Path

    from app.runner_health_alerts import _RUNNER_UPDATE_DRAIN_STUCK_EVENT_TYPE

    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    assert (
        f'event_type="{_RUNNER_UPDATE_DRAIN_STUCK_EVENT_TYPE}"' in src
    ), "heartbeat alert event_type literal drifted from the shared constant"
    from app.message_audience import _EVENT_AUDIENCE_REGISTRY

    assert _RUNNER_UPDATE_DRAIN_STUCK_EVENT_TYPE in _EVENT_AUDIENCE_REGISTRY


@pytest.mark.asyncio
async def test_update_drain_stuck_raises_one_alert_per_episode(db_session):
    """An idle runner draining past the threshold raises exactly one alert.

    Regression for the 2026-08-17 fleet outage: the serving orchestrator
    advertised a version hint unreachable from main, every runner looped its
    self-update retry every 60s, and the runner-side /update-report dedup
    (one report per distinct failure per daemon lifetime) meant zero operator
    alerts while the whole fleet sat out of the dispatch pool. The server now
    tracks the drain episode from heartbeats alone.
    """
    project, runner = await _make_runner(db_session)

    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.capabilities["update_draining_since"]

    async def _stuck_alerts():
        result = await db_session.execute(
            select(Alert).where(Alert.event_type == "runner.update_drain_stuck")
        )
        return result.scalars().all()

    # Young drain: no alert yet.
    assert await _stuck_alerts() == []

    # Backdate the episode start past the threshold.
    caps = dict(runner.capabilities)
    caps["update_draining_since"] = (
        _utcnow()
        - timedelta(seconds=runner_mod._UPDATE_DRAIN_STUCK_ALERT_SECONDS + 60)
    ).isoformat()
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    alerts = await _stuck_alerts()
    assert len(alerts) == 1
    assert alerts[0].project_id == project.id
    assert alerts[0].source == "runner"
    # Findability contract: the recovery path locates this exact incident by
    # its deterministic key. A null dedupe_key made the row unresolvable.
    assert alerts[0].dedupe_key == f"runner.update_drain_stuck:{runner.id}"
    await db_session.refresh(runner)
    assert runner.capabilities["update_drain_alerted_at"]

    # Further draining heartbeats do not duplicate the alert.
    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    assert len(await _stuck_alerts()) == 1

    # Ending the drain clears the episode markers so the NEXT episode can
    # alert again from a fresh clock.
    await heartbeat(
        payload=HeartbeatRequest(update_draining=False),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert "update_draining_since" not in runner.capabilities
    assert "update_drain_alerted_at" not in runner.capabilities
    # ...and the episode's alert is resolved, not left standing.
    assert [a.dismissed_at is not None for a in await _stuck_alerts()] == [True]


@pytest.mark.asyncio
async def test_update_drain_recovery_dismisses_the_stuck_alert(db_session):
    """Leaving the drain resolves the alert; a later episode opens a new one.

    Regression for iconlab, 2026-08-20: the runner self-updated to the hinted
    SHA and went back to claiming work, but its stuck-drain WARNING stayed
    open for hours still asserting "the self-update is failing repeatedly".
    Clearing the episode markers without clearing the alert leaves the
    operator feed asserting a condition that has already ended, which is a
    worse failure than duplicate alerts: it cannot be distinguished from a
    live outage, so it trains the reader to ignore the feed.
    """
    _project, runner = await _make_runner(db_session)

    async def _stuck_alerts():
        result = await db_session.execute(
            select(Alert)
            .where(Alert.event_type == "runner.update_drain_stuck")
            .order_by(Alert.created_at)
        )
        return result.scalars().all()

    async def _drain_past_threshold():
        """Enter the drain and backdate it so the next heartbeat alerts."""
        await heartbeat(
            payload=HeartbeatRequest(update_draining=True),
            runner=runner,
            session=db_session,
        )
        await db_session.refresh(runner)
        caps = dict(runner.capabilities)
        caps["update_draining_since"] = (
            _utcnow()
            - timedelta(seconds=runner_mod._UPDATE_DRAIN_STUCK_ALERT_SECONDS + 60)
        ).isoformat()
        runner.capabilities = caps
        db_session.add(runner)
        await db_session.commit()
        await heartbeat(
            payload=HeartbeatRequest(update_draining=True),
            runner=runner,
            session=db_session,
        )

    await _drain_past_threshold()
    opened = await _stuck_alerts()
    assert len(opened) == 1
    assert opened[0].dismissed_at is None
    first_alert_id = opened[0].id

    # The runner finishes its update and reports healthy.
    await heartbeat(
        payload=HeartbeatRequest(update_draining=False),
        runner=runner,
        session=db_session,
    )
    resolved = await _stuck_alerts()
    assert len(resolved) == 1, "recovery must resolve, not delete or duplicate"
    assert resolved[0].dismissed_at is not None

    # A healthy heartbeat is not a recovery transition: it must not re-run the
    # resolve, and it must not resurrect or duplicate anything.
    await heartbeat(
        payload=HeartbeatRequest(update_draining=False),
        runner=runner,
        session=db_session,
    )
    assert len(await _stuck_alerts()) == 1

    # A SECOND wedge is a new incident: the resolved row stays resolved and a
    # fresh open alert is raised, so persistence is still visible per episode.
    await _drain_past_threshold()
    both = await _stuck_alerts()
    assert len(both) == 2
    by_id = {a.id: a for a in both}
    assert by_id[first_alert_id].dismissed_at is not None
    still_open = [a for a in both if a.dismissed_at is None]
    assert len(still_open) == 1
    assert still_open[0].id != first_alert_id
    assert still_open[0].dedupe_key == f"runner.update_drain_stuck:{runner.id}"


@pytest.mark.asyncio
async def test_update_drain_stuck_alert_suppressed_while_work_active(db_session):
    """A long drain with genuine active work is normal, not an alert."""
    project, runner = await _make_runner(db_session)

    await heartbeat(
        payload=HeartbeatRequest(update_draining=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    caps = dict(runner.capabilities)
    caps["update_draining_since"] = (
        _utcnow()
        - timedelta(seconds=runner_mod._UPDATE_DRAIN_STUCK_ALERT_SECONDS + 60)
    ).isoformat()
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    await heartbeat(
        payload=HeartbeatRequest(
            update_draining=True,
            active_task_ids=["11111111-1111-1111-1111-111111111111"],
        ),
        runner=runner,
        session=db_session,
    )
    result = await db_session.execute(
        select(Alert).where(Alert.event_type == "runner.update_drain_stuck")
    )
    assert result.scalars().all() == []
    await db_session.refresh(runner)
    assert "update_drain_alerted_at" not in runner.capabilities


@pytest.mark.asyncio
async def test_daemon_heartbeat_omission_invalidates_stale_gpu_proof(db_session):
    """An old replacement daemon cannot inherit a prior process's proof."""
    stale_proof = {
        "docker": {"verified": True, "mode": "cdi", "reason": "verified"}
    }
    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": "all",
            "container_gpu_passthrough": stale_proof,
        },
    )

    await heartbeat(
        payload=HeartbeatRequest(active_work_report=True),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert "container_gpu_passthrough" not in runner.capabilities


@pytest.mark.asyncio
async def test_preflight_omission_preserves_last_gpu_proof(db_session):
    """A non-daemon --check probe does not erase genuine daemon inventory."""
    stale_proof = {
        "docker": {"verified": True, "mode": "cdi", "reason": "verified"}
    }
    _project, runner = await _make_runner(
        db_session,
        capabilities={
            "task_types": "all",
            "container_gpu_passthrough": stale_proof,
        },
    )

    await heartbeat(
        payload=HeartbeatRequest(active_work_report=False),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.capabilities["container_gpu_passthrough"] == stale_proof

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
async def test_heartbeat_persists_and_clears_checkout_source_identity(db_session):
    """A clean SHA is durable, while an explicit unknown/dirty report clears it."""
    _project, runner = await _make_runner(db_session)

    await heartbeat(
        payload=HeartbeatRequest(git_sha="a" * 40),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.source_sha == "a" * 40

    await heartbeat(
        payload=HeartbeatRequest(git_sha=None),
        runner=runner,
        session=db_session,
    )
    await db_session.refresh(runner)
    assert runner.source_sha is None


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
