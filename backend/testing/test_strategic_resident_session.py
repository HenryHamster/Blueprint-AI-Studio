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
from sqlalchemy.ext.asyncio import async_sessionmaker
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
    RESIDENT_SESSION_REASON_KEY,
    RESIDENT_SESSION_REASON_NO_RUNNER,
    RESIDENT_SESSION_REASON_START_FAILED_PREFIX,
    STRATEGIC_SESSION_ENABLED_SETTING,
    STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
    new_session_id,
    read_resident_session,
    read_resident_session_blocked_reason,
    renew_resident_session_lease,
    resident_session_is_active,
    resident_session_snapshot,
    router as resident_router,
    start_resident_session,
    stop_resident_session,
    sync_resident_session_for_interaction_mode,
)
from app.strategic_state_read_model import _resident_session_availability, compute_model_integrity_conflicts
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


def _dedicated_session_factory(db_session):
    """A dedicated-session factory bound to the SAME test engine as ``db_session``.

    ``sync_resident_session_for_interaction_mode`` runs its auto-start/stop +
    reason-recording on a FRESH session (production: ``AsyncSessionLocal``) so a
    poisoned start cannot break the caller's request session (Fix 2). Under the
    SQLite test harness a bare ``AsyncSessionLocal()`` opens a SEPARATE in-memory
    DB whose writes ``db_session`` can't see — the exact reason the sibling
    dedicated-session compensation tests are Postgres-only. Injecting a factory
    bound to ``db_session``'s own engine keeps the fresh session on the SAME test
    DB, so the committed session blob / blocked-reason is visible to the
    assertions below while the production isolation semantics are unchanged.
    """
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


async def _fresh_vision(factory, project_id) -> dict:
    """Read a project's ``vision`` via a FRESH session on the same test engine.

    The sync hook commits its auto-start/stop + reason writes on its own dedicated
    session; the long-lived ``db_session`` fixture holds a connection snapshot that
    does NOT observe another session's UPDATE to an existing row under the SQLite
    in-memory harness (the same isolation artifact that makes the sibling
    dedicated-session compensation tests Postgres-only). A fresh read session
    observes the committed state reliably, so assertions read through here.
    """
    async with factory() as s:
        proj = await s.get(Project, project_id)
        return dict(proj.vision) if proj is not None and proj.vision else {}


async def _fresh_task_status(factory, task_id):
    """Read a task's committed status via a FRESH session (see ``_fresh_vision``)."""
    async with factory() as s:
        task = await s.get(Task, task_id)
        return task.status if task is not None else None


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
    # A live runner is required now: Workday auto-picks an available device and
    # BLOCKS when none exists (no cloud fallback), so provide one.
    await _make_live_local_runner(db_session, project)

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
    await _make_live_local_runner(db_session, project)

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
    await _make_live_local_runner(db_session, project)

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
    await _make_live_local_runner(db_session, project)

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
    # A live runner so we exercise the real refuse-on-no-adoptable path (the spawn
    # guard), not the no-device block that fires earlier when no runner exists.
    await _make_live_local_runner(db_session, project)

    # The refusal path spawns a provisional claim then compensates it on a FRESH,
    # dedicated session (production: ``AsyncSessionLocal``); inject a factory bound
    # to this test's engine so that cleanup lands on the SAME DB the assertions
    # read (see ``_dedicated_session_factory`` / ``_release_start_claim``).
    factory = _dedicated_session_factory(db_session)
    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            session_factory=factory,
        )
    assert exc.value.status_code == 409

    # The in-flight one-shot was NOT hijacked into a (non-streaming) session
    # (read committed state via a fresh session — see ``_fresh_vision``).
    async with factory() as s:
        refreshed = await s.get(Task, one_shot.id)
        assert (
            refreshed.context is None
            or refreshed.context.get("_resident_session") is not True
        )
        assert refreshed.status == TaskStatus.IN_PROGRESS

    # No provisional claim left behind to wedge future starts.
    vision = await _fresh_vision(factory, project.id)
    assert read_resident_session(vision) is None


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
    # A live runner so the refusal is the spawn guard (no adoptable cycle), not the
    # earlier no-device block.
    await _make_live_local_runner(db_session, project)

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
async def test_start_adopt_pins_task_to_runner(db_session):
    """ADOPT-path twin of the SPAWN-path birth pin: when a resident session adopts
    an existing not-yet-dispatched cadence ``strategic_review`` AND a live runner
    exists, the adopted task is hard runner-pinned IN PLACE — ``PROVISIONING`` +
    ``vm_instance_id='runner:{id}'`` + ``context['runner_target_id']`` (the exact
    ``POST /api/runner/next-task`` contract) — so the operator's local runner pulls
    it directly, independent of the general VM-dispatch cycle (which may be paused,
    ``max_concurrent_vms=0``).

    Regression: adoption only stamped the non-authoritative
    ``preferred_runner_target_id`` hint, leaving the task ``QUEUED`` + ``agent-vm``
    with no ``runner_target_id`` — so it matched none of the four next-task filters
    and relied on the paused dispatch cycle to pin it.
    """
    project = await _make_project_with_member(db_session)
    runner = await _make_live_local_runner(db_session, project)
    # An adoptable cadence strategic_review (QUEUED, started_at NULL) that carries a
    # cloud-vm id AND a stale strategic local-runner hold marker the pin must clear.
    task = await _make_strategic_review_task(
        db_session, project, vm="agent-vm-cadence"
    )
    task.context = {srs.STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: "2020-01-01T00:00:00"}
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()

    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # The pre-existing adoptable task was ADOPTED (not a fresh spawn).
    assert snapshot["task_id"] == str(task.id)

    adopted = await db_session.get(Task, task.id)
    # The four next-task pin fields now land on the adopted task in place.
    assert adopted.status == TaskStatus.PROVISIONING
    assert adopted.vm_instance_id == f"runner:{runner.id}"
    assert adopted.started_at is None
    assert adopted.vm_provisioned_at is not None
    ctx = adopted.context or {}
    assert ctx.get("runner_target_id") == str(runner.id)
    # Resident flags + affinity hint preserved; stale hold marker cleared.
    assert ctx.get("_resident_session") is True
    assert ctx.get("strategic_session_id") == snapshot["session_id"]
    assert ctx.get("preferred_runner_target_id") == str(runner.id)
    assert ctx.get(srs.STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
    assert srs.STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY not in ctx


@pytest.mark.asyncio
async def test_start_blocks_when_no_runner_leaves_task_untouched(db_session):
    """Multi-device: with NO available runner the start BLOCKS (409) rather than
    proceeding unpinned to a cloud VM. The adoptable cadence task is left
    untouched (no resident flags, no claim wedged) so nothing is stranded."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    # No runner registered → auto-pick yields None → BLOCK.
    task = await _make_strategic_review_task(db_session, project, vm="agent-vm-cadence")

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )
    assert exc.value.status_code == 409
    assert "device" in exc.value.detail.lower()

    # The adoptable cadence task is untouched (never stamped / re-pinned) and no
    # session claim was written.
    untouched = await db_session.get(Task, task.id)
    assert untouched.status == TaskStatus.QUEUED
    assert untouched.vm_instance_id == "agent-vm-cadence"
    assert (untouched.context or {}).get("_resident_session") is not True
    fresh_project = await db_session.get(Project, project.id)
    assert read_resident_session(fresh_project.vision) is None


@pytest.mark.asyncio
async def test_start_explicit_runner_id_beats_autoselect(db_session):
    """An explicit ``runner_id`` OVERRIDE is honored: it pins to THAT device even
    when another available runner would auto-pick first (auto-select is only the
    no-override default)."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    # Two live runners. The most-recently-seen one (auto-pick default) is created
    # LAST; the caller overrides to the OTHER (older) device explicitly.
    override_runner = await _make_live_local_runner(
        db_session, project, last_seen=_utcnow() - timedelta(seconds=30)
    )
    await _make_live_local_runner(db_session, project)  # newer → auto-pick default

    snap = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(override_runner.id),
    )
    assert snap["runner_id"] == str(override_runner.id)
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.context.get("preferred_runner_target_id") == str(
        override_runner.id
    )


@pytest.mark.asyncio
async def test_start_override_runner_not_found_raises_404(db_session):
    """An explicit ``runner_id`` that is not a real runner of this project is a
    hard error (404) — never silently pinned to an unknown device."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # A live runner exists, but the override id is bogus → 404 (no silent fallback
    # to the live one).
    await _make_live_local_runner(db_session, project)

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            runner_id="not-a-real-runner",
        )
    assert exc.value.status_code == 404
    fresh_project = await db_session.get(Project, project.id)
    assert read_resident_session(fresh_project.vision) is None


@pytest.mark.asyncio
async def test_start_override_offline_runner_blocks_no_fallback(db_session):
    """Block-on-offline (override): an explicit ``runner_id`` that is OFFLINE
    blocks (409) with a "restart the session" reason — it does NOT silently fall
    back to another live device."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # The chosen device is offline (stale heartbeat); a DIFFERENT device is live.
    offline_runner = await _make_live_local_runner(
        db_session, project, last_seen=_utcnow() - timedelta(hours=1)
    )
    await _make_live_local_runner(db_session, project)  # live, but NOT chosen

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            runner_id=str(offline_runner.id),
        )
    assert exc.value.status_code == 409
    assert "restart the session" in exc.value.detail.lower()
    # No silent fallback: no session pinned to the live device was created.
    fresh_project = await db_session.get(Project, project.id)
    assert read_resident_session(fresh_project.vision) is None


@pytest.mark.asyncio
async def test_start_does_not_autoselect_offline_runner(db_session):
    """An OFFLINE (stale ``last_seen_at``) strategic-capable runner is NOT
    eligible for auto-pick; with no other available device the start BLOCKS
    rather than pinning to a dead runner."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # Registered + capable but last heartbeat 1h ago → not live → not auto-picked.
    await _make_live_local_runner(
        db_session, project, last_seen=_utcnow() - timedelta(hours=1)
    )

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# heartbeat (helper + agent-token endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_helper_renews_lease(db_session):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # A live runner is required now (Workday auto-picks a device and BLOCKS when
    # none exists — no cloud fallback), so provide one before starting.
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Adoption hard-pins the resident task to the auto-picked runner, so its
    # vm_instance_id is now ``runner:{id}`` — mint the agent token from the task's
    # ACTUAL post-adopt vm_instance_id so the heartbeat token matches (the endpoint
    # rejects a token whose vm_instance_id != task.vm_instance_id).
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), task.vm_instance_id)
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
    # Core lease-view keys are always present; additive read-only signals (older
    # agents ignore unknown fields).
    assert {"authority_held", "leased_until", "stop_requested", "status",
            "work_state_rev", "work_state_summary",
            "exam_ledger", "active_direction_ids"} <= set(body.keys())
    assert body["authority_held"] is True
    assert body["stop_requested"] is False
    assert body["status"] == "active"
    # work_state_summary is always a dict with the three advisory count keys.
    assert set(body["work_state_summary"].keys()) == {
        "in_flight",
        "terminal_since",
        "new_operator_messages",
    }


@pytest.mark.asyncio
async def test_heartbeat_endpoint_rejects_foreign_task_token(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project, vm="agent-vm-hb2")
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    assert refreshed.context.get("failure_category") == "ADMIN_CANCEL"

    # Continuation lease marker released.
    fresh_project = await db_session.get(Project, project.id)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (fresh_project.vision or {})


@pytest.mark.asyncio
async def test_stop_endpoint(client: AsyncClient, db_session, enable_strategic_session_flag):
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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


@pytest.mark.asyncio
async def test_stop_noop_when_no_session_blob(db_session):
    """Fix 4: stopping a project with NO live session blob is a no-op — it must
    NOT synthesize a phantom ``{session_id: null, status: "ended"}`` record into
    ``Project.vision`` (a spurious write + a non-null null-id "ended" session in
    API responses, even when the feature is dark and nothing ever started)."""
    project = await _make_project_with_member(db_session)
    # Never started a session — vision has no resident-session key.
    assert read_resident_session(project.vision) is None

    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # No live session -> empty snapshot, and crucially no phantom blob written.
    assert snapshot == {}
    fresh = await db_session.get(Project, project.id)
    assert RESIDENT_SESSION_KEY not in (fresh.vision or {})
    assert read_resident_session(fresh.vision) is None


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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    await _make_live_local_runner(db_session, project)

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


@pytest.mark.asyncio
async def test_start_spawn_path_is_minimal_and_lands_task_id(db_session, monkeypatch):
    """End-to-end SPAWN path: with no adoptable cadence task, ``start_resident_session``
    spawns its resident ``strategic_review`` skipping ONLY the slow GitHub-fetching
    context builders (design-doc grounding, capability backing-path verification,
    recent-intervention ancestry, branch-verification credential resolution).

    Those builders hold the request's DB connection open long enough for
    asyncpg/Cloud SQL to idle-close it, so the finalize flush 500s before
    ``task_id`` lands (~143s). They are tripwires here; the start still returns an
    ACTIVE session carrying a ``task_id``, the spawned task carries the resident
    born-context, AND its payload still ships the cheap grounding (directions +
    the queued open operator message) so the resident agent is not context-blind.
    """
    project = await _make_project_with_member(db_session)
    # Resident sessions run on the self-repo; is_self_repo short-circuits the
    # bootstrap-pending guard so the real spawn proceeds. Seed one active
    # direction so the payload's directions grounding is non-empty.
    project.is_self_repo = True
    project.directions = [{"id": "d1", "text": "ship it", "status": "active"}]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    runner = await _make_live_local_runner(db_session, project)
    # An OPEN operator message queued BEFORE the session starts — older than the
    # runner's ~2min first-poll cursor, so it must ride in the spawn payload or be
    # lost entirely.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.QUESTION,
            content="queued operator question",
            status="open",
        )
    )
    await db_session.commit()
    # Deliberately NO adoptable strategic_review task exists → the start SPAWNS.

    import app.strategic_spawning as _sps

    for _name in (
        "resolve_branch_verification_context",
        "_verify_capability_backing_paths",
        "_build_recent_interventions",
        "ground_design_doc",
    ):
        async def _boom(*args, _n=_name, **kwargs):
            raise AssertionError(f"resident spawn must not call {_n}")

        monkeypatch.setattr(_sps, _name, _boom)

    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # A real task_id landed and the session is active (the bug produced no task_id).
    assert snapshot["status"] == "active"
    assert snapshot["stop_requested"] is False
    assert snapshot["task_id"]

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic = [
        t for t in tasks if (t.first_payload or {}).get("type") == "strategic_review"
    ]
    assert len(strategic) == 1
    spawned = strategic[0]
    assert str(spawned.id) == snapshot["task_id"]
    # Born with the resident flags + runner pin (dispatch routes on these).
    ctx = spawned.context or {}
    assert ctx.get("_resident_session") is True
    assert ctx.get("strategic_session_id") == snapshot["session_id"]
    assert ctx.get("preferred_runner_target_id") == str(runner.id)
    payload = spawned.first_payload or {}
    # Cheap grounding shipped: directions + the queued operator message.
    assert [d["id"] for d in payload.get("directions_summary") or []] == ["d1"]
    assert "direction_counts" in payload
    assert any(
        "queued operator question" in (m.get("content") or "")
        for m in payload.get("open_messages") or []
    )
    # GitHub-derived keys absent (their builders were tripwired).
    for _heavy in ("recent_interventions", "design_doc", "conversation_thread"):
        assert _heavy not in payload


@pytest.mark.asyncio
async def test_resident_spawn_is_born_runner_pinned_and_pullable(db_session):
    """The resident SPAWN path must birth its ``strategic_review`` ALREADY pinned to
    the operator's local runner — the exact contract ``POST /api/runner/next-task``
    filters on — so the runner claims it directly, independent of the general
    VM-dispatch cycle (which may be paused, ``max_concurrent_vms=0``).

    Regression: the task was born ``status=QUEUED`` + ``vm_instance_id='agent-vm-…'``
    with NO ``context['runner_target_id']`` (only the non-authoritative
    ``preferred_runner_target_id`` affinity hint). ``next-task`` — which requires
    ``status==PROVISIONING AND vm_instance_id=='runner:{id}' AND
    context['runner_target_id']==id AND started_at IS NULL`` — matched none of those,
    returned 204, and the resident agent never ran (observed live: QUEUED, VM-pinned,
    ``runner_target_id=None``).
    """
    project = await _make_project_with_member(db_session)
    # is_self_repo short-circuits the bootstrap-pending guard so the real spawn runs.
    project.is_self_repo = True
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    runner = await _make_live_local_runner(db_session, project)

    session_id = new_session_id()
    task = await srs._spawn_resident_strategic_task(
        db_session, project, str(runner.id), session_id=session_id
    )
    assert task is not None

    # Read the COMMITTED task fresh and assert the four next-task pin fields.
    committed = await db_session.get(Task, task.id)
    assert committed.status == TaskStatus.PROVISIONING
    assert committed.vm_instance_id == f"runner:{runner.id}"
    assert committed.started_at is None
    ctx = committed.context or {}
    assert ctx.get("runner_target_id") == str(runner.id)
    # Resident routing flags still born in the SAME creating INSERT.
    assert ctx.get("_resident_session") is True
    assert ctx.get("strategic_session_id") == session_id
    # The affinity hint is preserved alongside the authoritative pin.
    assert ctx.get("preferred_runner_target_id") == str(runner.id)


@pytest.mark.asyncio
async def test_resident_spawn_unpinned_born_queued_when_no_runner(db_session):
    """With no runner id, the resident spawn falls back to the unpinned cloud path:
    born QUEUED + fresh ``agent-vm`` id, no ``runner_target_id`` — identical to the
    pre-fix cadence contract, so a degraded start still yields a valid task and no
    cadence caller is affected by the runner-pin branch."""
    project = await _make_project_with_member(db_session)
    project.is_self_repo = True
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await srs._spawn_resident_strategic_task(
        db_session, project, None, session_id=new_session_id()
    )
    assert task is not None
    committed = await db_session.get(Task, task.id)
    assert committed.status == TaskStatus.QUEUED
    assert committed.vm_instance_id.startswith("agent-vm-")
    assert committed.started_at is None
    assert "runner_target_id" not in (committed.context or {})
    # The resident routing flag is still born even on the unpinned fallback.
    assert (committed.context or {}).get("_resident_session") is True


# ---------------------------------------------------------------------------
# Finding 3 — spawn/finalize failure compensates (no orphan task, no wedged claim)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_spawn_then_finalize_failure_compensates(db_session, monkeypatch):
    """If the finalize fails after a task was spawned, compensation cancels the
    orphaned strategic_review task and clears the provisional claim so no
    untracked task persists and no claim wedges future starts."""
    project = await _make_project_with_member(db_session)  # no live strategic task
    # A live runner so the start clears the multi-device gate and reaches the
    # spawn/finalize path this test drives to failure.
    await _make_live_local_runner(db_session, project)

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

    # Compensation runs on a FRESH, dedicated session (production:
    # ``AsyncSessionLocal``); inject a factory bound to this test's engine so the
    # cleanup lands on the SAME DB the assertions read back through.
    factory = _dedicated_session_factory(db_session)
    with pytest.raises(RuntimeError):
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            session_factory=factory,
        )

    # Orphaned spawned task cancelled (read committed state via a fresh session).
    assert await _fresh_task_status(factory, created["id"]) == TaskStatus.CANCELLED
    # Provisional claim cleared — no active session left to wedge future starts.
    vision = await _fresh_vision(factory, project.id)
    assert read_resident_session(vision) is None


# ---------------------------------------------------------------------------
# Workday is the on-switch: auto start/stop the resident session on an
# interaction_mode change (sync_resident_session_for_interaction_mode).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_auto_starts_on_enter_continuous(
    db_session, enable_strategic_session_flag
):
    """Entering Workday (chat -> continuous) with the flag on + a live local
    runner auto-starts the resident session (pinned to that runner) and clears
    any blocked-reason."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    runner = await _make_live_local_runner(db_session, project)

    factory = _dedicated_session_factory(db_session)
    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=factory,
    )
    assert snapshot is not None
    assert snapshot["status"] == "active"
    assert snapshot["task_id"] == str(task.id)
    assert snapshot["runner_id"] == str(runner.id)

    vision = await _fresh_vision(factory, project.id)
    assert resident_session_is_active(read_resident_session(vision))
    # No blocked-reason recorded when the auto-start succeeds.
    assert read_resident_session_blocked_reason(vision) is None


@pytest.mark.asyncio
async def test_sync_auto_stops_on_leave_continuous(
    db_session, enable_strategic_session_flag
):
    """Leaving Workday (continuous -> chat) auto-stops the live resident
    session and hands authority back to the cadence."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)

    project = await db_session.get(Project, project.id)
    factory = _dedicated_session_factory(db_session)
    result = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="continuous",
        new_mode="chat",
        session_factory=factory,
    )
    assert result is None

    vision = await _fresh_vision(factory, project.id)
    blob = read_resident_session(vision)
    assert blob is not None and blob["status"] == "ended"
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in vision
    assert await _fresh_task_status(factory, task.id) == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_sync_records_no_runner_when_no_live_runner(
    db_session, enable_strategic_session_flag
):
    """Flag on but NO live eligible local runner: entering Workday records the
    ``no_runner`` reason and does NOT start a session (Workday runs on the
    operator's box, not a cloud VM)."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # No runner registered.

    factory = _dedicated_session_factory(db_session)
    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=factory,
    )
    assert snapshot is None

    vision = await _fresh_vision(factory, project.id)
    assert read_resident_session(vision) is None
    assert (
        read_resident_session_blocked_reason(vision)
        == RESIDENT_SESSION_REASON_NO_RUNNER
    )
    assert RESIDENT_SESSION_REASON_KEY in vision


@pytest.mark.asyncio
async def test_sync_records_start_failed_reason(
    db_session, enable_strategic_session_flag, monkeypatch
):
    """When the auto-start path errors (Track-1 makes it fail fast + clean), the
    sync records a ``start_failed: <msg>`` reason and never propagates the error
    (the mode switch must stand)."""
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)

    async def boom(*args, **kwargs):
        raise HTTPException(status_code=409, detail="no task to host session")

    monkeypatch.setattr(srs, "start_resident_session", boom)

    # Must NOT raise — the mode switch is already committed by the caller.
    factory = _dedicated_session_factory(db_session)
    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=factory,
    )
    assert snapshot is None

    vision = await _fresh_vision(factory, project.id)
    reason = read_resident_session_blocked_reason(vision)
    assert reason is not None
    assert reason.startswith(RESIDENT_SESSION_REASON_START_FAILED_PREFIX)
    assert "no task to host session" in reason


@pytest.mark.asyncio
async def test_sync_noop_when_flag_disabled(db_session):
    """Flag off: entering Workday records nothing (the read model derives
    ``sessions_disabled`` from the flag) and starts no session."""
    runtime_config.pop(STRATEGIC_SESSION_ENABLED_SETTING, None)
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)

    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
    )
    assert snapshot is None
    fresh = await db_session.get(Project, project.id)
    assert read_resident_session(fresh.vision) is None
    assert read_resident_session_blocked_reason(fresh.vision) is None


@pytest.mark.asyncio
async def test_sync_ignores_non_transition(
    db_session, enable_strategic_session_flag
):
    """A workbench save that does NOT cross the continuous boundary (e.g. a
    selected_refs edit that keeps the same mode) is a no-op — no start, no stop,
    no reason churn."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)

    # chat -> chat (no transition).
    assert (
        await sync_resident_session_for_interaction_mode(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            previous_mode="chat",
            new_mode="chat",
        )
        is None
    )
    fresh = await db_session.get(Project, project.id)
    assert read_resident_session(fresh.vision) is None
    assert RESIDENT_SESSION_REASON_KEY not in (fresh.vision or {})


@pytest.mark.asyncio
async def test_sync_runs_start_on_dedicated_session(
    db_session, enable_strategic_session_flag
):
    """Fix 2: the auto-start (and reason recording) run on a FRESH, dedicated
    session — NEVER the caller's request ``session`` — so a poisoned start can
    neither poison the request session nor 5xx the endpoint's trailing
    ``get_project_strategic_state`` read. Asserts a dedicated session distinct
    from ``db_session`` hosted the work, and the caller session stays usable."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)

    opened: list = []
    base_factory = _dedicated_session_factory(db_session)

    def tracking_factory():
        s = base_factory()
        opened.append(s)
        return s

    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=tracking_factory,
    )
    assert snapshot is not None
    assert snapshot["status"] == "active"
    assert snapshot["task_id"] == str(task.id)

    # The start ran on a dedicated session, never the caller's request session.
    assert len(opened) >= 1
    assert all(s is not db_session for s in opened)

    # The caller's request session stays clean/usable, and the committed session
    # blob is observable (read via a fresh session — see ``_fresh_vision``).
    vision = await _fresh_vision(base_factory, project.id)
    assert resident_session_is_active(read_resident_session(vision))


# NOTE: an end-to-end HTTP test of the mode-change endpoint auto-starting the
# session (POST /strategic-session with interaction_mode=continuous) requires
# Postgres — the endpoint's arming/refresh + strategic-state read paths use
# Postgres-only SQL (``jsonb_array_length``), the same reason the sibling
# compensation/dedicated-session tests here run only under the Postgres harness.
# CI (Postgres) covers that HTTP path; the wiring is unit-tested directly above
# via ``sync_resident_session_for_interaction_mode``.


# ---------------------------------------------------------------------------
# Read-model availability projection (_resident_session_availability): the
# legible state + reason the operator UI renders.
# ---------------------------------------------------------------------------


def _project_with_vision(vision: dict) -> Project:
    return Project(name="avail", user_id=TEST_USER_ID, vision=vision)


def test_resident_session_availability_off_when_flag_off():
    runtime_config.pop(STRATEGIC_SESSION_ENABLED_SETTING, None)
    result = _resident_session_availability(_project_with_vision({}))
    assert result == {"available": False, "state": "off", "reason": "sessions_disabled"}


def test_resident_session_availability_reasons(enable_strategic_session_flag):
    # no_runner recorded -> blocked + reason.
    blocked = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_REASON_KEY: {"reason": RESIDENT_SESSION_REASON_NO_RUNNER}}
        )
    )
    assert blocked["state"] == "blocked"
    assert blocked["reason"] == RESIDENT_SESSION_REASON_NO_RUNNER

    # start_failed recorded -> error + full reason.
    failed = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_REASON_KEY: {"reason": "start_failed: boom"}}
        )
    )
    assert failed["state"] == "error"
    assert failed["reason"] == "start_failed: boom"

    # Active session that has begun streaming (progress recorded) -> active.
    active = _resident_session_availability(
        _project_with_vision(
            {
                RESIDENT_SESSION_KEY: {
                    "status": "active",
                    "stop_requested": False,
                    "started_at": "2026-01-01T00:00:00",
                    "last_progress_at": "2026-01-01T00:05:00",
                }
            }
        )
    )
    assert active["state"] == "active"
    assert active["reason"] is None


def test_resident_session_availability_starting_before_agent_streams(
    enable_strategic_session_flag,
):
    """A live session blob whose agent has not begun (no progress, only the
    start-seeded operator interaction) reads as ``starting`` — the transient
    bring-up window — not yet ``active``."""
    # Freshly started: last_progress_at is None and last_operator_interaction_at
    # equals started_at (the seed). The agent has not begun -> starting.
    starting = _resident_session_availability(
        _project_with_vision(
            {
                RESIDENT_SESSION_KEY: {
                    "status": "active",
                    "stop_requested": False,
                    "started_at": "2026-01-01T00:00:00",
                    "last_progress_at": None,
                    "last_operator_interaction_at": "2026-01-01T00:00:00",
                }
            }
        )
    )
    assert starting["state"] == "starting"
    assert starting["reason"] is None

    # A provisional claim (no task pinned yet, no progress) is also starting.
    provisional = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_KEY: {"status": "active", "stop_requested": False}}
        )
    )
    assert provisional["state"] == "starting"

    # Once a post-start operator interaction lands (a later timestamp), the
    # operator<->agent loop is live -> active.
    active = _resident_session_availability(
        _project_with_vision(
            {
                RESIDENT_SESSION_KEY: {
                    "status": "active",
                    "stop_requested": False,
                    "started_at": "2026-01-01T00:00:00",
                    "last_progress_at": None,
                    "last_operator_interaction_at": "2026-01-01T00:02:00",
                }
            }
        )
    )
    assert active["state"] == "active"

    # Enabled, no reason, zero eligible runners -> inferred no_runner.
    inferred = _resident_session_availability(
        _project_with_vision({}), eligible_live_runner_count=0
    )
    assert inferred["state"] == "blocked"
    assert inferred["reason"] == RESIDENT_SESSION_REASON_NO_RUNNER

    # Enabled, no reason, a live runner attached -> ready.
    ready = _resident_session_availability(
        _project_with_vision({}), eligible_live_runner_count=1
    )
    assert ready["state"] == "ready"
    assert ready["reason"] is None


def test_resident_session_availability_live_runner_overrides_stale_no_runner(
    enable_strategic_session_flag,
):
    """Fix 1: a stale ``no_runner`` reason (recorded on a runner-less Workday entry
    and never re-run when a runner later attaches) is OVERRIDDEN by a positive live
    runner count — the read model reports ``ready``, not a phantom "blocked: no
    runner" with a runner plainly attached. A recorded ``start_failed`` still wins
    over a live count (a real failure is never masked)."""
    # Stale no_runner recorded, but a live runner is now attached -> ready.
    overridden = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_REASON_KEY: {"reason": RESIDENT_SESSION_REASON_NO_RUNNER}}
        ),
        eligible_live_runner_count=1,
    )
    assert overridden["state"] == "ready"
    assert overridden["reason"] is None

    # A recorded no_runner is still authoritative while the live count is 0.
    still_blocked = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_REASON_KEY: {"reason": RESIDENT_SESSION_REASON_NO_RUNNER}}
        ),
        eligible_live_runner_count=0,
    )
    assert still_blocked["state"] == "blocked"
    assert still_blocked["reason"] == RESIDENT_SESSION_REASON_NO_RUNNER

    # start_failed is NEVER masked by a live runner count.
    start_failed_wins = _resident_session_availability(
        _project_with_vision(
            {RESIDENT_SESSION_REASON_KEY: {"reason": "start_failed: boom"}}
        ),
        eligible_live_runner_count=1,
    )
    assert start_failed_wins["state"] == "error"
    assert start_failed_wins["reason"] == "start_failed: boom"


def _active_session_vision(runner_id: str | None) -> dict:
    """An ACTIVE, streaming resident-session blob pinned to *runner_id*."""
    return {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "stop_requested": False,
            "runner_id": runner_id,
            "started_at": "2026-01-01T00:00:00",
            "last_progress_at": "2026-01-01T00:05:00",
        }
    }


def test_resident_session_availability_mid_session_offline_blocks(
    enable_strategic_session_flag,
):
    """Block-on-offline (mid-session): a live session pinned to a device that is
    now OFFLINE (or de-enrolled) reports ``blocked``/``runner_offline`` — the UI
    prompts a restart to pick another device, NO auto-migration."""
    # Pinned device present but offline -> blocked/runner_offline.
    offline = _resident_session_availability(
        _project_with_vision(_active_session_vision("r-1")),
        eligible_live_runner_count=0,
        runner_options=[{"id": "r-1", "name": "box-1", "online": False}],
    )
    assert offline["state"] == "blocked"
    assert offline["reason"] == srs.RESIDENT_SESSION_REASON_RUNNER_OFFLINE

    # Pinned device de-enrolled (absent from the live options) -> also blocked.
    missing = _resident_session_availability(
        _project_with_vision(_active_session_vision("r-gone")),
        runner_options=[{"id": "r-1", "name": "box-1", "online": True}],
    )
    assert missing["state"] == "blocked"
    assert missing["reason"] == srs.RESIDENT_SESSION_REASON_RUNNER_OFFLINE

    # Pinned device still online -> stays active (no false offline block).
    online = _resident_session_availability(
        _project_with_vision(_active_session_vision("r-1")),
        runner_options=[{"id": "r-1", "name": "box-1", "online": True}],
    )
    assert online["state"] == "active"
    assert online["reason"] is None

    # An UNPINNED (legacy cloud) active session never flips to the offline block.
    unpinned = _resident_session_availability(
        _project_with_vision(_active_session_vision(None)),
        runner_options=[],
    )
    assert unpinned["state"] == "active"


@pytest.mark.asyncio
async def test_build_resident_runner_options_flags(db_session):
    """``build_resident_runner_options`` returns one entry per non-deleted runner
    with online / agent-ready / eligible flags for the device picker."""
    project = await _make_project_with_member(db_session)
    live = await _make_live_local_runner(db_session, project)
    offline = await _make_live_local_runner(
        db_session, project, last_seen=_utcnow() - timedelta(hours=1)
    )
    agent_down = await _make_live_local_runner(
        db_session, project, agent_available=False
    )

    options = await srs.build_resident_runner_options(db_session, project.id)
    by_id = {o["id"]: o for o in options}
    assert set(by_id) == {str(live.id), str(offline.id), str(agent_down.id)}

    assert by_id[str(live.id)]["online"] is True
    assert by_id[str(live.id)]["agent_ready"] is True
    assert by_id[str(live.id)]["eligible"] is True

    # Offline (stale heartbeat): not online, not eligible.
    assert by_id[str(offline.id)]["online"] is False
    assert by_id[str(offline.id)]["eligible"] is False

    # Live but agent unavailable: online yet not agent-ready, so not eligible.
    assert by_id[str(agent_down.id)]["online"] is True
    assert by_id[str(agent_down.id)]["agent_ready"] is False
    assert by_id[str(agent_down.id)]["eligible"] is False

    # Every option carries a display name for the picker.
    assert all(isinstance(o["name"], str) and o["name"] for o in options)


@pytest.mark.asyncio
async def test_release_start_claim_runs_on_dedicated_session(db_session):
    """Compensation must run on a DEDICATED session opened via the injected
    factory — never the caller's (possibly poisoned) session — and must both
    clear OUR provisional claim and cancel the orphaned spawned task.

    This locks the fix for the live crash: running compensation on the caller
    session (poisoned by the original spawn/finalize failure) raised
    ``MissingGreenlet`` on a pool pre-ping and masked the real error, leaving a
    half-initialized (``task_id: null``) session behind. A fresh connection can
    always clean up.
    """
    project = await _make_project_with_member(db_session)

    # Commit a provisional claim (task_id=None) + an orphaned spawned task, the
    # exact mid-flight state a failed start leaves between claim and finalize.
    session_id = "ss-comp-dedicated"
    now_iso = _utcnow().isoformat()
    orphan = Task(
        project_id=project.id,
        title="orphan resident",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-orphan",
        created_by="ORCHESTRATOR",
    )
    db_session.add(orphan)
    vision = dict(project.vision or {})
    vision[RESIDENT_SESSION_KEY] = {
        "version": 1,
        "session_id": session_id,
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
    orphan_id = orphan.id

    # A dedicated-session factory bound to this test's engine so compensation's
    # FRESH session (production: ``AsyncSessionLocal``) lands on the SAME DB the
    # assertions read — see ``_dedicated_session_factory``.
    base_factory = _dedicated_session_factory(db_session)
    used_sessions: list = []

    def tracking_factory():
        s = base_factory()
        used_sessions.append(s)
        return s

    await srs._release_start_claim(
        project.id, session_id, orphan_id, session_factory=tracking_factory
    )

    # Compensation opened its OWN dedicated session (not the caller's db_session).
    assert len(used_sessions) == 1
    assert used_sessions[0] is not db_session

    # Claim cleared + orphan cancelled (visible on a fresh read of committed state).
    vision = await _fresh_vision(base_factory, project.id)
    assert read_resident_session(vision) is None
    assert await _fresh_task_status(base_factory, orphan_id) == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_release_start_claim_only_clears_own_claim(db_session):
    """Guarded on ``session_id``: compensation for a stale/foreign session_id must
    NOT clear a claim a concurrent starter legitimately owns."""
    project = await _make_project_with_member(db_session)

    now_iso = _utcnow().isoformat()
    owner_blob = {
        "version": 1,
        "session_id": "ss-owned-by-peer",
        "task_id": "t-peer",
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
    vision = dict(project.vision or {})
    vision[RESIDENT_SESSION_KEY] = owner_blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # We failed a DIFFERENT session; compensation must leave the peer's claim intact.
    await srs._release_start_claim(project.id, "ss-ours-that-failed", None)

    await db_session.rollback()
    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob is not None
    assert blob["session_id"] == "ss-owned-by-peer"


# ---------------------------------------------------------------------------
# Finding 4 — freshness renews only on progress (liveness heartbeat preserves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_liveness_only_does_not_extend_lease(db_session):
    """progress=True renews (extends) freshness; progress=False is liveness-only
    and PRESERVES the existing freshness without bumping it."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
# Resident pacing substrate — the heartbeat carries an additive, READ-ONLY
# work-state signal (``work_state_rev`` + ``work_state_summary``) for the
# agent's state-gated pacing. It must be present, monotonic over strategist-
# relevant change sources, and must NOT extend the lease.
# ---------------------------------------------------------------------------


async def _make_operator_message(db_session, project, *, content="hi", created_at=None):
    """Insert an operator-authored (from_type='user') open ProjectMessage."""
    msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        content=content,
        status="open",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    if created_at is not None:
        msg.created_at = created_at
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)
    return msg


async def _make_strategic_agent_task(
    db_session, project, *, status=TaskStatus.IN_PROGRESS, updated_at=None
):
    """Insert a STRATEGIC_AGENT-created (strategist-spawned) task."""
    task = Task(
        project_id=project.id,
        title="spawned by strategist",
        payloads=[{"type": "research"}],
        status=status,
        created_by="STRATEGIC_AGENT",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    if updated_at is not None:
        task.updated_at = updated_at
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_heartbeat_reports_work_state_fields(db_session):
    """The heartbeat response carries ``work_state_rev`` (max ISO stamp over
    strategist-relevant sources) and a ``work_state_summary`` dict with the three
    advisory count keys the agent's tick context consumes."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Establish concrete strategist-relevant world state: one in-flight spawned
    # task and one open operator message.
    await _make_strategic_agent_task(db_session, project, status=TaskStatus.IN_PROGRESS)
    await _make_operator_message(db_session, project)

    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )

    # rev present and ISO-8601-ish (has a date/time separator).
    assert result["work_state_rev"] is not None
    assert "T" in result["work_state_rev"]

    summary = result["work_state_summary"]
    assert set(summary.keys()) == {"in_flight", "terminal_since", "new_operator_messages"}
    assert summary["in_flight"] == 1  # our one IN_PROGRESS STRATEGIC_AGENT task
    assert summary["new_operator_messages"] == 1  # our one open operator message
    assert summary["terminal_since"] == 0  # no terminal strategist tasks yet
    assert all(isinstance(v, int) for v in summary.values())


@pytest.mark.asyncio
async def test_heartbeat_work_state_rev_is_monotonic(db_session):
    """A newer strategist-relevant change (a later operator message) advances
    ``work_state_rev``; the marker never moves backward."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    base = _utcnow()
    await _make_operator_message(
        db_session, project, created_at=base - timedelta(minutes=5)
    )
    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    rev1 = r1["work_state_rev"]
    assert rev1 is not None

    # A newer operator message must strictly advance the rev.
    await _make_operator_message(
        db_session, project, created_at=base + timedelta(minutes=5)
    )
    project = await db_session.get(Project, project.id)
    r2 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    rev2 = r2["work_state_rev"]
    assert rev2 is not None
    assert rev2 > rev1  # ISO-8601 same-format strings compare monotonically
    assert r2["work_state_summary"]["new_operator_messages"] == 2


@pytest.mark.asyncio
async def test_heartbeat_work_state_does_not_extend_lease(db_session):
    """Surfacing the work-state signal is read-only: a liveness-only (progress=
    False) heartbeat that computes a non-null work_state_rev still PRESERVES the
    existing lease — the dead-man's switch is never extended by the new fields."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # Strategist-relevant activity so work_state_rev is genuinely populated.
    await _make_strategic_agent_task(db_session, project, status=TaskStatus.IN_PROGRESS)
    await _make_operator_message(db_session, project)

    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    lease1 = r1["leased_until"]
    assert lease1 is not None
    assert r1["work_state_rev"] is not None

    # Liveness-only heartbeat: work-state computed + returned, but lease unmoved.
    project = await db_session.get(Project, project.id)
    r2 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    assert r2["authority_held"] is True
    assert r2["leased_until"] == lease1  # NOT extended by the work-state read
    assert r2["work_state_rev"] is not None


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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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
    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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

    # A live runner is required now (Workday blocks when no device is available).
    await _make_live_local_runner(db_session, project)
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


# ---------------------------------------------------------------------------
# compute_model_integrity_conflicts unit tests
# ---------------------------------------------------------------------------

def _make_gap(id: str, name: str, status: str = "active", active_moves: list | None = None, deferral: dict | None = None) -> dict:
    g: dict = {"id": id, "name": name, "status": status}
    if active_moves is not None:
        g["active_moves"] = active_moves
    if deferral is not None:
        g["deferral"] = deferral
    return g


def _make_dir(id: str, text: str, gap_id: str | None = None, status: str = "active") -> dict:
    d: dict = {"id": id, "text": text, "status": status}
    if gap_id is not None:
        d["gap_id"] = gap_id
    return d


def test_compute_model_integrity_conflicts_clean_model_returns_none():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_make_dir("d-1", "Direction 1", gap_id="g-1")]
    assert compute_model_integrity_conflicts(pm, dirs) is None


def test_compute_model_integrity_conflicts_empty_model_returns_none():
    assert compute_model_integrity_conflicts({}, []) is None
    assert compute_model_integrity_conflicts(None, None) is None


def test_compute_model_integrity_conflicts_orphaned_gap():
    pm = {"gaps": [_make_gap("g-1", "Gap 1")]}
    dirs = [_make_dir("d-1", "Direction 1", gap_id=None)]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    assert len(result["orphaned_gaps"]) == 1
    assert result["orphaned_gaps"][0]["id"] == "g-1"
    assert result["counts"]["orphaned_gaps"] == 1
    assert result["counts"]["total"] >= 1


def test_compute_model_integrity_conflicts_deferred_gap_not_orphaned():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", deferral={"reason": "parked"})]}
    dirs = []
    assert compute_model_integrity_conflicts(pm, dirs) is None


def test_compute_model_integrity_conflicts_ungrounded_direction_no_gap():
    pm = {"gaps": []}
    dirs = [_make_dir("d-1", "Active direction with no gap")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"]
    assert len(ud) == 1
    assert ud[0]["id"] == "d-1"
    assert ud[0]["reason"] == "no_gap_id"
    assert ud[0]["gap_id"] is None


def test_compute_model_integrity_conflicts_ungrounded_direction_closed_gap():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", status="closed")]}
    dirs = [_make_dir("d-1", "Direction", gap_id="g-1")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"]
    assert len(ud) == 1
    assert ud[0]["reason"] == "gap_not_active"
    assert ud[0]["gap_id"] == "g-1"


def test_compute_model_integrity_conflicts_stale_active_moves():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-old", "d-1"])]}
    dirs = [_make_dir("d-1", "Direction 1", gap_id="g-1")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    sam = result["stale_active_moves"]
    assert len(sam) == 1
    assert sam[0]["gap_id"] == "g-1"
    assert "d-old" in sam[0]["stale_direction_ids"]
    assert "d-1" not in sam[0]["stale_direction_ids"]


def test_compute_model_integrity_conflicts_paused_direction_not_active():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-paused"])]}
    dirs = [_make_dir("d-paused", "Paused dir", gap_id="g-1", status="paused")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    # d-paused is paused, so it is not in active_dir_ids → stale_active_moves
    assert len(result["stale_active_moves"]) == 1
    # g-1 is not referenced by any active direction → orphaned
    assert len(result["orphaned_gaps"]) == 1
    assert result["counts"]["total"] >= 2


def test_compute_model_integrity_conflicts_gap_conflicts_carry_recovery_hints():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-old"])]}
    dirs = [_make_dir("d-1", "Ungrounded dir")]  # no gap → ungrounded + orphan
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    # Every gap-integrity entry surfaces an actionable, operator-facing hint.
    assert result["orphaned_gaps"][0]["recovery_hint"] == (
        "Assign an active direction to this gap or record a deferral."
    )
    assert result["ungrounded_directions"][0]["recovery_hint"] == (
        "Link this direction to an open gap or retire it."
    )
    assert result["stale_active_moves"][0]["recovery_hint"] == (
        "Reconcile the gap's active_moves — the listed directions are no longer active."
    )


def test_compute_model_integrity_conflicts_ungrounded_closed_gap_hint():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", status="closed")]}
    dirs = [_make_dir("d-1", "Direction", gap_id="g-1")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"][0]
    assert ud["reason"] == "gap_not_active"
    assert ud["recovery_hint"] == (
        "Reground this direction to an active gap — its current gap is closed."
    )


def test_compute_model_integrity_conflicts_criterion_links_absent_by_default():
    # A gap-clean model with no criterion inputs stays clean (backward compatible).
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_make_dir("d-1", "Direction 1", gap_id="g-1")]
    assert compute_model_integrity_conflicts(pm, dirs) is None


def test_compute_model_integrity_conflicts_criterion_link_inactive_and_dangling():
    pm = {"gaps": [_make_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_make_dir("d-1", "Direction 1", gap_id="g-1")]
    criterion_links = [
        {
            "id": "d-1",
            "text": "Direction 1",
            "status": "active",
            "criterion_keys": ["c-ok", "c-retracted", "c-unknown"],
        }
    ]
    result = compute_model_integrity_conflicts(
        pm,
        dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset({"c-ok"}),
        inactive_criterion_keys=frozenset({"c-retracted"}),
    )
    assert result is not None
    clc = result["criterion_link_conflicts"]
    assert len(clc) == 2
    by_key = {c["criterion_key"]: c for c in clc}
    assert by_key["c-retracted"]["classification"] == "inactive"
    assert by_key["c-retracted"]["recovery_hint"] == (
        "Remap to an active criterion — this key is retracted or superseded."
    )
    assert by_key["c-unknown"]["classification"] == "dangling"
    assert by_key["c-unknown"]["recovery_hint"] == (
        "Remap to a criterion in the open version — this key is unknown there."
    )
    assert by_key["c-retracted"]["direction_id"] == "d-1"
    assert result["counts"]["criterion_link_conflicts"] == 2
    assert result["counts"]["total"] == 2


def test_compute_model_integrity_conflicts_criterion_links_ignore_inactive_directions():
    # A retired/paused direction's stale criterion keys are not operator-actionable.
    pm = {"gaps": []}
    dirs: list = []
    criterion_links = [
        {
            "id": "d-old",
            "text": "Retired direction",
            "status": "retired",
            "criterion_keys": ["c-unknown"],
        }
    ]
    result = compute_model_integrity_conflicts(
        pm,
        dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset(),
        inactive_criterion_keys=frozenset(),
    )
    assert result is None


# ---------------------------------------------------------------------------
# Exam-ledger persistence: heartbeat note round-trips into the session blob
# and is echoed back in the response; the lease is NOT extended (liveness-only
# heartbeat). Satisfies the acceptance-criteria gate named test.
# ---------------------------------------------------------------------------


_SAMPLE_LEDGER = {
    "records": [
        {
            "surfaceKey": "direction:d-abc123",
            "atTick": 1,
            "at": "2026-01-01T00:01:00",
            "verdict": "clear",
            "note": "checked the direction; nothing to do",
        },
        {
            "surfaceKey": "qualification",
            "atTick": 2,
            "at": "2026-01-01T00:02:00",
            "verdict": "found",
            "note": "audit finding needs a follow-up direction",
        },
    ],
    "tick": 2,
    "corrective": False,
}


@pytest.mark.asyncio
async def test_heartbeat_note_ledger_summary_persists_without_extending_lease(db_session):
    """The heartbeat ``note`` field carries the agent's serialized exam ledger.
    The backend parses it (JSON), stores it as ``_exam_ledger`` in the session
    blob, and echoes ``exam_ledger`` in the response. A liveness-only (progress=
    False) heartbeat that carries a note PERSISTS the ledger AND still does NOT
    extend the continuation lease — both the persistence contract (write path)
    and the lease-immutability contract (read path) hold simultaneously.
    """
    import json as _json

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Establish a baseline lease by sending a progress heartbeat first.
    project = await db_session.get(Project, project.id)
    r0 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    baseline_lease = r0["leased_until"]
    assert baseline_lease is not None

    # Liveness-only heartbeat WITH a note: carries the exam ledger.
    note = _json.dumps(_SAMPLE_LEDGER)
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False, note=note,
    )

    # --- lease-immutability contract (pinned by existing test) ---------------
    assert result["authority_held"] is True  # still fresh (preserved, not bumped)
    assert result["leased_until"] == baseline_lease  # NOT extended by liveness

    # --- persistence contract (new) -----------------------------------------
    # The echoed exam_ledger field is the stored JSON string.
    assert result.get("exam_ledger") == note

    # The blob carries ``_exam_ledger`` (written inside the locked _renew closure).
    fresh_project = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh_project.vision)
    assert blob is not None
    assert "_exam_ledger" in blob
    stored = _json.loads(blob["_exam_ledger"])
    assert stored["tick"] == _SAMPLE_LEDGER["tick"]
    assert len(stored["records"]) == len(_SAMPLE_LEDGER["records"])
    assert stored["records"][0]["surfaceKey"] == "direction:d-abc123"
    assert stored["records"][1]["verdict"] == "found"

    # --- snapshot does NOT expose the internal ledger -------------------------
    # ``_exam_ledger`` is an implementation detail; ``resident_session_snapshot``
    # must NOT leak it to operator-facing API responses.
    snap_dict = resident_session_snapshot(fresh_project.vision)
    assert snap_dict is not None
    assert "_exam_ledger" not in snap_dict


@pytest.mark.asyncio
async def test_heartbeat_note_ledger_mirrored_to_task_context(db_session):
    """The exam ledger is written to task.context._exam_ledger (not only to
    project.vision) so a runner/deploy restart can restore it: the agent reads
    task.context._exam_ledger on startup before making any heartbeat call."""
    import json as _json

    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    note = _json.dumps(_SAMPLE_LEDGER)
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False, note=note,
    )

    # The task.context must carry _exam_ledger so the agent can read it on restart.
    fresh_task = await db_session.get(Task, task.id)
    assert fresh_task is not None
    assert "_exam_ledger" in (fresh_task.context or {})
    stored = _json.loads(fresh_task.context["_exam_ledger"])
    assert stored["tick"] == _SAMPLE_LEDGER["tick"]
    assert len(stored["records"]) == len(_SAMPLE_LEDGER["records"])


@pytest.mark.asyncio
async def test_heartbeat_note_malformed_json_is_ignored(db_session):
    """A malformed or non-dict note is silently ignored: the blob is updated
    normally (no crash, no _exam_ledger written), and the response is valid."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False, note="not-json{{{",
    )
    assert result["stop_requested"] is False
    assert result["exam_ledger"] is None

    fresh_project = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh_project.vision)
    assert "_exam_ledger" not in (blob or {})


@pytest.mark.asyncio
async def test_heartbeat_returns_active_direction_ids(db_session):
    """The heartbeat response includes ``active_direction_ids`` populated from
    Direction rows with non-retired statuses; empty when no rows exist."""
    from app.models import Direction as DirectionModel

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # No Direction rows yet → empty list.
    project = await db_session.get(Project, project.id)
    r0 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    assert r0["active_direction_ids"] == []

    # Add directions covering all vocabulary statuses.
    included_statuses = [("d-active1", "active"), ("d-paused1", "paused"), ("d-proposed1", "proposed")]
    excluded_statuses = [
        ("d-retired1", "retired"),
        ("d-completed1", "completed"),
        ("d-superseded1", "superseded"),
        ("d-rejected1", "rejected"),
        ("d-expired1", "expired"),
    ]
    for key, status in included_statuses + excluded_statuses:
        db_session.add(
            DirectionModel(
                project_id=project.id,
                direction_key=key,
                status=status,
                data={},
            )
        )
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    ids = set(r1["active_direction_ids"])
    # All three included statuses must appear.
    assert "d-active1" in ids
    assert "d-paused1" in ids
    assert "d-proposed1" in ids
    # All excluded statuses must be absent.
    for key, _ in excluded_statuses:
        assert key not in ids, f"{key} should be excluded from active_direction_ids"
