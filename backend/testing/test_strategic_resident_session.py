"""Tests for the resident strategic session lifecycle backend
(app.strategic_resident_session) + the admin start/stop endpoints and the
agent-token heartbeat endpoint.

Covers:
* the session-state ``Project.vision`` blob helpers,
* ``start`` — feature-flag gate, task adoption + context stamping, lease arming,
* ``stop`` — driving the resident task terminal + releasing the lease,
* ``heartbeat`` — lease renewal + stop_requested delivery.

Session/lease state lives in ``Project.vision`` JSON, so these run
standalone-friendly (no Postgres-only features).
"""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.auth import create_agent_token
from app.main import app
from app.models import (
    LocalRunner,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    Task,
    TaskStatus,
    _utcnow,
)
from app.shared_state import runtime_config
import app.strategic_resident_session as srs
from app.strategic_resident_session import (
    RESIDENT_SESSION_KEY,
    STRATEGIC_SESSION_ENABLED_SETTING,
    STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
    new_session_id,
    read_resident_session,
    renew_resident_session_lease,
    resident_session_is_active,
    resident_session_snapshot,
    router as resident_router,
    start_resident_session,
    stop_resident_session,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    strategic_local_continuation_status_from_vision,
)

TEST_USER_ID = "test-user-123"

# The agent heartbeat router is registered centrally in main.py at integration
# time (this builder must not edit main.py). Self-register it for the test app so
# the HTTP heartbeat path is exercised end-to-end; guarded against duplicates.
if not any(
    getattr(route, "path", "").endswith("/strategic-session/heartbeat")
    for route in app.routes
):
    app.include_router(resident_router)


@pytest.fixture
def enable_strategic_session_flag():
    """Turn the runtime feature flag on for the duration of a test."""
    previous = runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING)
    runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = True
    try:
        yield
    finally:
        if previous is None:
            runtime_config.pop(STRATEGIC_SESSION_ENABLED_SETTING, None)
        else:
            runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = previous


async def _make_project_with_member(db_session, *, name="Resident Test") -> Project:
    project = Project(name=name, user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await db_session.commit()
    return project


async def _make_strategic_review_task(
    db_session,
    project,
    *,
    vm="agent-vm-resident",
    status=TaskStatus.QUEUED,
) -> Task:
    # Default QUEUED (with no ``started_at``) = an *adoptable*, not-yet-executing
    # cadence cycle: the case a resident session start adopts. Tests that need an
    # already-executing one-shot pass ``status=TaskStatus.IN_PROGRESS`` explicitly.
    task = Task(
        project_id=project.id,
        title="Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=status,
        vm_instance_id=vm,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_live_local_runner(
    db_session,
    project,
    *,
    task_types="all",
    last_seen=None,
    agent_available=None,
) -> LocalRunner:
    """Register a strategic-capable local runner (live by default)."""
    caps: dict = {"task_types": task_types, "max_concurrent": 2}
    if agent_available is not None:
        caps["agent_available"] = agent_available
    runner = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        status="active",
        capabilities=caps,
        last_seen_at=last_seen if last_seen is not None else _utcnow(),
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)
    return runner


# ---------------------------------------------------------------------------
# Pure session-state helpers
# ---------------------------------------------------------------------------


def test_session_state_helpers():
    assert new_session_id().startswith("ss-")
    assert read_resident_session(None) is None
    assert read_resident_session({}) is None
    assert resident_session_snapshot({}) is None
    assert resident_session_is_active(None) is False
    assert resident_session_is_active({"status": "active"}) is True
    assert resident_session_is_active({"status": "active", "stop_requested": True}) is False
    assert resident_session_is_active({"status": "ended"}) is False

    vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-abc",
            "task_id": "t-1",
            "status": "active",
            "stop_requested": False,
            "runner_id": None,
        }
    }
    snap = resident_session_snapshot(vision)
    assert snap["session_id"] == "ss-abc"
    assert snap["status"] == "active"
    assert snap["stop_requested"] is False


# ---------------------------------------------------------------------------
# start (helper + admin endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_helper_adopts_task_and_arms_lease(db_session):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)

    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "active"
    assert snapshot["stop_requested"] is False
    assert snapshot["task_id"] == str(task.id)
    assert snapshot["session_id"].startswith("ss-")

    # Task adopted: resident-session context flags stamped.
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.context.get("_resident_session") is True
    assert refreshed.context.get("strategic_session_id") == snapshot["session_id"]

    # Session-state blob + continuation lease marker both live in vision.
    fresh_project = await db_session.get(Project, project.id)
    assert RESIDENT_SESSION_KEY in (fresh_project.vision or {})
    assert STRATEGIC_LOCAL_CONTINUATION_KEY in (fresh_project.vision or {})


@pytest.mark.asyncio
async def test_start_is_idempotent(db_session):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)

    first = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)
    second = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert first["session_id"] == second["session_id"]
    assert first["task_id"] == second["task_id"]


@pytest.mark.asyncio
async def test_start_endpoint_gated_when_flag_disabled(client: AsyncClient, db_session):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # No enable_strategic_session_flag fixture — flag defaults off.
    runtime_config.pop(STRATEGIC_SESSION_ENABLED_SETTING, None)

    res = await client.post(f"/api/admin/projects/{project.id}/strategic-session/start")
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_start_endpoint_starts_when_enabled(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)

    res = await client.post(f"/api/admin/projects/{project.id}/strategic-session/start")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enabled"] is True
    assert body["session"]["status"] == "active"
    assert body["session"]["task_id"] == str(task.id)

    # DB persistence: the endpoint committed on a separate connection.
    await db_session.rollback()
    await db_session.refresh(project)
    await db_session.refresh(task)
    assert RESIDENT_SESSION_KEY in (project.vision or {}), f"vision={project.vision}"
    assert task.context.get("_resident_session") is True, f"context={task.context}"


@pytest.mark.asyncio
async def test_start_over_stale_marker_arms_fresh_authoritative_lease(db_session):
    """A resident session started OVER a leftover STALE continuation marker must
    arm a FRESH, authoritative lease. A session start IS an operator interaction,
    so freshness must anchor to NOW regardless of the leftover marker. Without
    ``renewal_reason='operator_interaction'`` the marker's freshness contract
    PRESERVES the stale ``last_seen_at`` — leaving the brand-new session born
    non-authoritative (fresh=False) so the cloud cadence keeps spawning
    competing cycles until the first progress op."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)

    # Plant a leftover, STALE continuation marker (same operator; last_seen_at
    # well past the TTL) as if a prior session lapsed without a clean release.
    stale_iso = (_utcnow() - timedelta(hours=2)).isoformat()
    vision = dict(project.vision or {})
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = {
        "version": 1,
        "active": True,
        "operator_user_id": TEST_USER_ID,
        "updated_at": stale_iso,
        "last_seen_at": stale_iso,
    }
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # Sanity: the planted leftover marker is indeed stale before the start.
    project = await db_session.get(Project, project.id)
    pre = strategic_local_continuation_status_from_vision(
        project.vision, operator_user_id=TEST_USER_ID
    )
    assert pre["fresh"] is False, f"precondition not stale: {pre}"

    await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # After the start the lease must be FRESHLY armed (last_seen_at bumped to
    # now) — the session is authoritative, not born stale.
    fresh_project = await db_session.get(Project, project.id)
    cont = strategic_local_continuation_status_from_vision(
        fresh_project.vision, operator_user_id=TEST_USER_ID
    )
    assert cont["fresh"] is True, f"session born stale over leftover marker: {cont}"
    assert cont["reason"] == "armed"
    seen = srs._parse_iso_naive(cont["last_seen_at"])
    assert seen is not None
    assert (_utcnow() - seen).total_seconds() < 60, cont

    # The freshness renewal was attributed to the session-start operator
    # interaction (the fix), not a preserved stale anchor.
    marker = fresh_project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker.get("last_renewal_reason") == "operator_interaction"


# ---------------------------------------------------------------------------
# Adoption safety — do NOT adopt an already-executing one-shot cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_does_not_adopt_in_progress_one_shot(db_session):
    """A ``strategic_review`` task already IN_PROGRESS is executing the one-shot
    ``handleStrategic`` — dispatch was decided at claim time and will NOT re-route
    to the streaming ``handleStrategicResident`` just because we stamp the flag.
    ``start`` must therefore NOT adopt it (that would leave the session pointed at
    a non-streaming task). With the in-flight one-shot being the only active
    strategic task, the ``_has_active_strategic_task`` guard also blocks a
    concurrent spawn, so ``start`` refuses (409) rather than silently adopting the
    non-streaming task — and leaves the one-shot untouched + no wedged claim."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    one_shot = await _make_strategic_review_task(
        db_session, project, status=TaskStatus.IN_PROGRESS
    )

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )
    assert exc.value.status_code == 409

    # The in-flight one-shot was NOT hijacked into a (non-streaming) session.
    refreshed = await db_session.get(Task, one_shot.id)
    assert refreshed.context is None or refreshed.context.get("_resident_session") is not True
    assert refreshed.status == TaskStatus.IN_PROGRESS

    # No provisional claim left behind to wedge future starts.
    fresh = await db_session.get(Project, project.id)
    assert read_resident_session(fresh.vision) is None


@pytest.mark.asyncio
async def test_start_does_not_adopt_started_provisioning_cycle(db_session):
    """Only a not-yet-dispatched cycle (QUEUED, no ``started_at``) is adoptable;
    a cycle already stamped ``started_at`` (PROVISIONING onward) is not — it has,
    or is about to have, an agent that already dispatched it as a one-shot."""
    project = await _make_project_with_member(db_session)
    # A cycle that has begun dispatch: PROVISIONING with started_at stamped.
    started = await _make_strategic_review_task(
        db_session, project, status=TaskStatus.PROVISIONING
    )
    started.started_at = _utcnow()
    db_session.add(started)
    await db_session.commit()

    from fastapi import HTTPException

    # No adoptable cycle + the started one keeps _has_active_strategic_task hot →
    # spawn blocked → refuse rather than adopt the already-dispatched task.
    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )
    assert exc.value.status_code == 409
    refreshed = await db_session.get(Task, started.id)
    assert refreshed.context is None or refreshed.context.get("_resident_session") is not True


# ---------------------------------------------------------------------------
# Runner auto-selection — pin to the operator's box when no runner is specified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_auto_selects_local_runner_when_none_specified(db_session):
    """Called without ``runner_id`` (and no operator-chosen preferred runner),
    ``start`` auto-pins the resident task to a LIVE strategic-capable local runner
    so the warm session runs on the operator's box instead of a fresh cloud VM."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    runner = await _make_live_local_runner(db_session, project)

    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # Session records the auto-selected runner.
    assert snap["runner_id"] == str(runner.id)

    # The adopted task is hard-pinned to that runner (no cloud fallback).
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.context.get("preferred_runner_target_id") == str(runner.id)
    assert (
        refreshed.context.get(srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    )

    # Continuation marker armed with the auto-selected runner as its preferred pin.
    fresh_project = await db_session.get(Project, project.id)
    marker = fresh_project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker.get("preferred_runner_id") == str(runner.id)


@pytest.mark.asyncio
async def test_start_explicit_runner_id_beats_autoselect(db_session):
    """An explicit ``runner_id`` is honored as-is (auto-selection is only a
    fallback for the no-runner case)."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    # A live runner exists, but the caller pins a DIFFERENT explicit id.
    await _make_live_local_runner(db_session, project)

    explicit = "runner-explicit-123"
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID, runner_id=explicit
    )
    assert snap["runner_id"] == explicit
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.context.get("preferred_runner_target_id") == explicit


@pytest.mark.asyncio
async def test_start_proceeds_unpinned_when_no_eligible_runner(db_session):
    """No runner registered → auto-selection yields None → the session proceeds
    unpinned (the degraded cloud fallback, unchanged from before)."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)

    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap["runner_id"] is None
    refreshed = await db_session.get(Task, task.id)
    assert (
        refreshed.context is None
        or refreshed.context.get(srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
        is not True
    )


@pytest.mark.asyncio
async def test_start_does_not_autoselect_offline_runner(db_session):
    """An OFFLINE (stale ``last_seen_at``) strategic-capable runner is NOT
    eligible for auto-selection; the session proceeds unpinned rather than
    pinning to a dead runner."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    # Registered + capable but last heartbeat 1h ago → not live.
    await _make_live_local_runner(
        db_session, project, last_seen=_utcnow() - timedelta(hours=1)
    )

    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap["runner_id"] is None
    refreshed = await db_session.get(Task, task.id)
    assert (
        refreshed.context is None
        or refreshed.context.get(srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
        is not True
    )


# ---------------------------------------------------------------------------
# heartbeat (helper + agent-token endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_helper_renews_lease(db_session):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)

    result = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
    )
    assert result["authority_held"] is True
    assert result["stop_requested"] is False
    assert result["status"] == "active"
    assert result["leased_until"] is not None

    # progress=True recorded last_progress_at on the blob.
    fresh_project = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh_project.vision)
    assert blob["last_progress_at"] is not None


@pytest.mark.asyncio
async def test_heartbeat_helper_mismatch_rejected(db_session):
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)

    with pytest.raises(HTTPException) as exc:
        await renew_resident_session_lease(
            db_session,
            project,
            session_id="ss-wrong",
            task_id=snapshot["task_id"],
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_heartbeat_helper_reports_stop_requested_without_renewing(db_session):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Simulate an operator soft-stop: flip the flag on the blob directly.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["stop_requested"] = True
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
    )
    assert result["stop_requested"] is True
    assert result["authority_held"] is False


@pytest.mark.asyncio
async def test_heartbeat_endpoint_agent_token(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project, vm="agent-vm-hb")
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    token = create_agent_token(str(task.id), "agent-vm-hb")
    res = await client.post(
        f"/api/agent/projects/{project.id}/strategic-session/heartbeat",
        headers={"x-agent-token": token},
        json={
            "session_id": snapshot["session_id"],
            "task_id": snapshot["task_id"],
            "progress": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body.keys()) == {
        "authority_held",
        "leased_until",
        "stop_requested",
        "status",
    }
    assert body["authority_held"] is True
    assert body["stop_requested"] is False
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_heartbeat_endpoint_rejects_foreign_task_token(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project, vm="agent-vm-hb2")
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # A different, non-resident live task on the same project.
    other = await _make_strategic_review_task(
        db_session, project, vm="agent-vm-other"
    )

    # Token belongs to a different (non-resident) task than payload.task_id.
    token = create_agent_token(str(other.id), "agent-vm-other")
    res = await client.post(
        f"/api/agent/projects/{project.id}/strategic-session/heartbeat",
        headers={"x-agent-token": token},
        json={
            "session_id": snapshot["session_id"],
            "task_id": snapshot["task_id"],
        },
    )
    assert res.status_code == 403, res.text


# ---------------------------------------------------------------------------
# stop (helper + admin endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_helper_drives_task_terminal_and_releases_lease(db_session):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)

    project = await db_session.get(Project, project.id)
    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snapshot["status"] == "ended"
    assert snapshot["stop_requested"] is True
    assert snapshot["ended_at"] is not None

    # Resident task driven terminal.
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.status == TaskStatus.CANCELLED

    # Continuation lease marker released.
    fresh_project = await db_session.get(Project, project.id)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (fresh_project.vision or {})


@pytest.mark.asyncio
async def test_stop_endpoint(client: AsyncClient, db_session, enable_strategic_session_flag):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await client.post(f"/api/admin/projects/{project.id}/strategic-session/start")

    res = await client.post(f"/api/admin/projects/{project.id}/strategic-session/stop")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session"]["status"] == "ended"
    assert body["session"]["stop_requested"] is True

    # The HTTP handler committed on a separate connection; end this session's
    # transaction and refresh the tracked instances to see the committed state.
    await db_session.rollback()
    await db_session.refresh(project)
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    blob = (project.vision or {}).get(RESIDENT_SESSION_KEY)
    assert blob is not None and blob.get("status") == "ended"


# ---------------------------------------------------------------------------
# Finding 1 — stop <-> heartbeat race must not re-arm the lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_stop_race_does_not_rearm_lease(db_session, monkeypatch):
    """If a stop lands AFTER the heartbeat's pre-lock snapshot but before its
    in-lock write, the FRESH in-lock blob (stopped) must veto the re-arm —
    authority_held=False, and the lease is not renewed."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)

    real_with_vision_locked = srs.with_vision_locked
    calls = {"n": 0}

    async def racing_with_vision_locked(session, project_id, mutate_fn, **kwargs):
        # Simulate a concurrent stop (a separate committed request) landing
        # between the heartbeat's pre-lock snapshot and its in-lock read: flip
        # the persisted blob to ended and COMMIT it FIRST, then run the real
        # renew closure — which must re-read the fresh (committed) in-lock blob.
        calls["n"] += 1
        if calls["n"] == 1:

            def _stop(vision):
                blob = vision.get(RESIDENT_SESSION_KEY)
                if isinstance(blob, dict):
                    blob = dict(blob)
                    blob["stop_requested"] = True
                    blob["status"] = "ended"
                    vision[RESIDENT_SESSION_KEY] = blob

            await real_with_vision_locked(session, project_id, _stop, **kwargs)
            await session.commit()
        return await real_with_vision_locked(session, project_id, mutate_fn, **kwargs)

    monkeypatch.setattr(srs, "with_vision_locked", racing_with_vision_locked)

    result = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
    )
    assert result["authority_held"] is False
    assert result["stop_requested"] is True

    # The lease was NOT re-armed onto an ended session.
    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob["status"] == "ended"


# ---------------------------------------------------------------------------
# Finding 2 — concurrent start must not spawn a second task (atomic claim)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_backs_off_on_inflight_provisional_claim(db_session):
    """An in-flight provisional spawn-claim (active blob, task_id=None) from a
    concurrent start makes a second start BACK OFF rather than spawn a competing
    strategic_review task — the single-active-session invariant."""
    project = await _make_project_with_member(db_session)
    # Deliberately NO live strategic_review task exists, so the only way to
    # avoid a spawn is to honor the concurrent claim.
    now_iso = _utcnow().isoformat()
    vision = dict(project.vision or {})
    vision[RESIDENT_SESSION_KEY] = {
        "version": 1,
        "session_id": "ss-concurrent",
        "task_id": None,
        "runner_id": None,
        "operator_user_id": TEST_USER_ID,
        "status": "active",
        "stop_requested": False,
        "started_at": now_iso,
        "updated_at": now_iso,
        "ended_at": None,
        "last_progress_at": None,
        "last_operator_interaction_at": now_iso,
    }
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # Backed off to the in-flight session; no competing task was spawned.
    assert snap["session_id"] == "ss-concurrent"

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic = [
        t for t in tasks if (t.first_payload or {}).get("type") == "strategic_review"
    ]
    assert strategic == []


@pytest.mark.asyncio
async def test_start_single_task_across_two_starts(db_session):
    """Two starts against a live strategic_review task adopt the SAME single
    task (never spawn a second) and return the same session."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)

    first = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)
    second = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert first["session_id"] == second["session_id"]
    assert first["task_id"] == str(task.id)

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic = [
        t for t in tasks if (t.first_payload or {}).get("type") == "strategic_review"
    ]
    assert len(strategic) == 1


# ---------------------------------------------------------------------------
# Finding 3 — spawn/finalize failure compensates (no orphan task, no wedged claim)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_spawn_then_finalize_failure_compensates(db_session, monkeypatch):
    """If the finalize fails after a task was spawned, compensation cancels the
    orphaned strategic_review task and clears the provisional claim so no
    untracked task persists and no claim wedges future starts."""
    project = await _make_project_with_member(db_session)  # no live strategic task

    created: dict = {}

    async def fake_spawn(session, proj, runner_id, *, session_id):
        # Mimic the canonical spawn: create + commit a strategic_review task.
        t = Task(
            project_id=proj.id,
            title="spawned resident",
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="agent-vm-spawned",
            created_by="ORCHESTRATOR",
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        created["id"] = t.id
        return t

    async def boom(*args, **kwargs):
        raise RuntimeError("finalize boom")

    monkeypatch.setattr(srs, "_spawn_resident_strategic_task", fake_spawn)
    monkeypatch.setattr(srs, "persist_strategic_workbench_update", boom)

    with pytest.raises(RuntimeError):
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )

    # Orphaned spawned task cancelled.
    t = await db_session.get(Task, created["id"])
    assert t.status == TaskStatus.CANCELLED
    # Provisional claim cleared — no active session left to wedge future starts.
    fresh = await db_session.get(Project, project.id)
    assert read_resident_session(fresh.vision) is None


# ---------------------------------------------------------------------------
# Finding 4 — freshness renews only on progress (liveness heartbeat preserves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_liveness_only_does_not_extend_lease(db_session):
    """progress=True renews (extends) freshness; progress=False is liveness-only
    and PRESERVES the existing freshness without bumping it."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    lease1 = r1["leased_until"]
    assert r1["authority_held"] is True
    assert lease1 is not None

    # Liveness-only heartbeat must NOT advance the lease.
    project = await db_session.get(Project, project.id)
    r2 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    assert r2["authority_held"] is True  # still fresh (preserved)
    assert r2["leased_until"] == lease1  # not extended

    # A subsequent progress heartbeat DOES advance it again.
    project = await db_session.get(Project, project.id)
    r3 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    assert r3["leased_until"] is not None
    assert r3["leased_until"] >= lease1


# ---------------------------------------------------------------------------
# Finding 1 (this review) — lease lapse / unattended cap END the session so the
# resident task goes terminal and the legacy cadence reclaims (no indefinite
# stall). A fresh / actively-working session is NEVER stop-requested.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_unattended_cap_ends_session(db_session, monkeypatch):
    """Past the max-unattended cap with no operator interaction, the heartbeat
    ENDS the session (stop_requested=True, status=ended, lease released) so the
    resident task can go terminal and ``_spawn_strategic_review`` is unblocked —
    rather than leaving it alive-but-passive and stalling strategic progress
    forever."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Small cap; age the last operator interaction well past it; no messages yet.
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 60)
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["last_operator_interaction_at"] = (
        _utcnow() - timedelta(hours=2)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    capped = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,  # progress does NOT override the cap
    )
    assert capped["authority_held"] is False
    assert capped["stop_requested"] is True
    assert capped["status"] == "ended"

    # Blob driven to a consistent ending state + lease released.
    fresh = await db_session.get(Project, project.id)
    reblob = read_resident_session(fresh.vision)
    assert reblob["status"] == "ended"
    assert reblob["stop_requested"] is True
    assert reblob["ended_at"] is not None
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (fresh.vision or {})


@pytest.mark.asyncio
async def test_heartbeat_operator_return_before_cap_keeps_session_alive(
    db_session, monkeypatch
):
    """A fresh operator message that arrives BEFORE the unattended cap elapses
    re-arms the cap clock, so the session keeps its authority and is not
    stop-requested — the operator can keep a session alive by interacting."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Age the recorded interaction past a small cap, but land a fresher operator
    # message so ``op_reengaged`` wins and the cap does not fire.
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 60)
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["last_operator_interaction_at"] = (
        _utcnow() - timedelta(hours=2)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            status="open",
            message_type=MessageType.NOTE,
            content="still here",
        )
    )
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    reengaged = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,  # operator interaction re-arms the cap
    )
    assert reengaged["authority_held"] is True
    assert reengaged["stop_requested"] is False
    assert reengaged["status"] == "active"

    fresh = await db_session.get(Project, project.id)
    reblob = read_resident_session(fresh.vision)
    # Cap clock advanced to the operator message time (no longer 2h stale).
    assert (
        _utcnow() - srs._parse_iso_naive(reblob["last_operator_interaction_at"])
    ).total_seconds() < 60


@pytest.mark.asyncio
async def test_heartbeat_lease_lapse_stale_ends_session(db_session):
    """A genuinely LAPSED lease (freshness anchor elapsed past the TTL, i.e. no
    progress and no operator interaction for the whole window) ENDS the session
    on the next heartbeat, handing authority back to the cadence — this is an
    elapsed-time condition, not a transient non-fresh read."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Age the continuation marker's freshness anchor well past the TTL (default
    # 1500s) so the lease is genuinely stale. last_operator_interaction stays
    # recent so this is NOT the unattended cap — purely the freshness lapse.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    stale_iso = (_utcnow() - timedelta(hours=1)).isoformat()
    marker["last_seen_at"] = stale_iso
    marker["updated_at"] = stale_iso
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # A bare liveness heartbeat PRESERVES the (now-stale) freshness → lapse → end.
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    assert result["stop_requested"] is True
    assert result["authority_held"] is False
    assert result["status"] == "ended"

    fresh = await db_session.get(Project, project.id)
    reblob = read_resident_session(fresh.vision)
    assert reblob["status"] == "ended"
    assert reblob["stop_requested"] is True
    # Lease released on lapse.
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (fresh.vision or {})


@pytest.mark.asyncio
async def test_heartbeat_fresh_session_is_not_stop_requested(db_session):
    """A freshly-started session's first heartbeat (even bare liveness) must NOT
    be insta-stopped: the lease was just armed on the operator-interaction start,
    so it reads fresh, not lapsed."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    assert result["stop_requested"] is False
    assert result["status"] == "active"
    assert result["authority_held"] is True


@pytest.mark.asyncio
async def test_heartbeat_active_progress_is_not_stop_requested(db_session):
    """An actively-working session (progress heartbeats) holds authority and is
    never stop-requested."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    for _ in range(3):
        project = await db_session.get(Project, project.id)
        result = await renew_resident_session_lease(
            db_session, project, session_id=snap["session_id"],
            task_id=snap["task_id"], progress=True,
        )
        assert result["authority_held"] is True
        assert result["stop_requested"] is False
        assert result["status"] == "active"


# ---------------------------------------------------------------------------
# Finding 2 (this review) — adopt candidate re-validated under the lock; if it
# was claimed as a one-shot under us, spawn fresh instead of hijacking it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_revalidates_adopt_candidate_and_spawns_fresh(
    db_session, monkeypatch
):
    """If the adopt candidate the pre-lock finder returned is no longer adoptable
    when re-validated under the claim locks (a warm runner claimed it as a
    one-shot — started_at stamped / status advanced), ``start`` must NOT stamp the
    resident flags onto it. It spawns a fresh resident task instead so the session
    never points at a task that already dispatched as a non-streaming one-shot."""
    project = await _make_project_with_member(db_session)

    # A candidate that has already been claimed as a one-shot (started_at stamped,
    # PROVISIONING) — i.e. non-adoptable by the time we re-validate under the lock.
    claimed = await _make_strategic_review_task(
        db_session, project, status=TaskStatus.PROVISIONING
    )
    claimed.started_at = _utcnow()
    db_session.add(claimed)
    await db_session.commit()

    # Force the pre-lock finder to hand back the (now non-adoptable) candidate,
    # simulating the TOCTOU: it looked adoptable when first read, then a runner
    # claimed it before the resident-context stamp.
    async def fake_finder(session, project_id):
        return await session.get(Task, claimed.id)

    monkeypatch.setattr(srs, "_find_adoptable_strategic_review_task", fake_finder)

    # A fresh spawn returns an adoptable resident task (avoids exercising the real
    # cadence spawn path in a standalone test).
    fresh_holder: dict = {}

    async def fake_spawn(session, proj, runner_id, *, session_id):
        # The real spawn wrapper BIRTHS the task with the resident context flags
        # (via extra_context); mirror that here so the fresh task carries them in
        # its first committed state, exactly as start now relies on (no post-spawn
        # re-stamp).
        t = Task(
            project_id=proj.id,
            title="fresh resident",
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-fresh",
            created_by="ORCHESTRATOR",
            context=srs._resident_born_context(session_id, runner_id),
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        fresh_holder["id"] = t.id
        return t

    monkeypatch.setattr(srs, "_spawn_resident_strategic_task", fake_spawn)

    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Session points at the freshly-spawned task, NOT the already-claimed candidate.
    assert snap["task_id"] == str(fresh_holder["id"])
    assert snap["task_id"] != str(claimed.id)

    # The already-claimed candidate was left untouched (no resident flags stamped).
    refreshed_claimed = await db_session.get(Task, claimed.id)
    assert (
        refreshed_claimed.context is None
        or refreshed_claimed.context.get("_resident_session") is not True
    )
    assert refreshed_claimed.status == TaskStatus.PROVISIONING

    # The fresh task carries the resident flags.
    fresh_task = await db_session.get(Task, fresh_holder["id"])
    assert fresh_task.context.get("_resident_session") is True
    assert fresh_task.context.get("strategic_session_id") == snap["session_id"]


# ---------------------------------------------------------------------------
# SPAWN-path race fix — the spawned resident task is BORN with the flag
# (seeded into the creating INSERT), so it never exists claimable without it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_wrapper_seeds_born_context_into_creating_insert(
    db_session, monkeypatch
):
    """``_spawn_resident_strategic_task`` must hand the resident flags + runner pin
    to the canonical spawn as ``extra_context`` so the strategic_review task is
    committed ALREADY carrying ``_resident_session`` / ``strategic_session_id`` (+
    the local-runner pin) in its FIRST committed state — never in a committed,
    claimable state without the flag. Captures the ``extra_context`` handed to
    ``_spawn_strategic_review`` and asserts the committed task's context matches."""
    import app.strategic_spawning as ss

    project = await _make_project_with_member(db_session)
    captured: dict = {}

    async def fake_inner_spawn(proj, sess, *args, **kwargs):
        # Stand in for the heavy canonical spawn: create + commit a task with
        # exactly the context the real INSERT would use (derived + extra_context).
        extra = kwargs.get("extra_context")
        captured["extra_context"] = extra
        captured["preferred_runner_target_id"] = kwargs.get(
            "preferred_runner_target_id"
        )
        born = {"strategic_cycle_id": "cyc-xyz", **(extra or {})}
        t = Task(
            project_id=proj.id,
            title="born resident",
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-born",
            created_by="ORCHESTRATOR",
            context=born,
        )
        sess.add(t)
        await sess.commit()
        return True

    monkeypatch.setattr(ss, "_spawn_strategic_review", fake_inner_spawn)

    session_id = "ss-born-test"
    runner_id = "runner-xyz"
    task = await srs._spawn_resident_strategic_task(
        db_session, project, runner_id, session_id=session_id
    )
    assert task is not None

    # The wrapper forwarded the resident flags + hard local-runner pin as
    # extra_context (born at INSERT time, not stamped a beat later).
    extra = captured["extra_context"]
    assert extra["_resident_session"] is True
    assert extra["strategic_session_id"] == session_id
    assert extra["preferred_runner_target_id"] == runner_id
    assert extra[srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] is True
    assert (
        extra[srs.STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY] == "resident_session"
    )
    # The passed runner is also forwarded as the affinity hint (unchanged path).
    assert captured["preferred_runner_target_id"] == runner_id

    # The COMMITTED task carries the flag in its first (only) committed state, so
    # a warm runner claiming it before the session blob finalizes reads
    # _resident_session and dispatches the streaming handler.
    committed = await db_session.get(Task, task.id)
    assert committed.context.get("_resident_session") is True
    assert committed.context.get("strategic_session_id") == session_id
    assert committed.context.get("preferred_runner_target_id") == runner_id
    assert (
        committed.context.get(srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
        is True
    )
    # Caller-derived context (e.g. the cycle id) is preserved alongside the flags.
    assert committed.context.get("strategic_cycle_id") == "cyc-xyz"


@pytest.mark.asyncio
async def test_born_context_unpinned_when_no_runner():
    """With no runner, born-context carries ONLY the resident flags (no local-
    runner pin) — matching the unpinned cloud-fallback stamping semantics."""
    ctx = srs._resident_born_context("ss-nopin", None)
    assert ctx["_resident_session"] is True
    assert ctx["strategic_session_id"] == "ss-nopin"
    assert "preferred_runner_target_id" not in ctx
    assert srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY not in ctx
