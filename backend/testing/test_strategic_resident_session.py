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

import os
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import MissingGreenlet
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
    RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
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

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")

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
    assert refreshed.context.get("provider_preference") == [
        "anthropic",
        "zai",
        "deepseek",
    ]

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

    # Snapshot ids BEFORE the start: its refusal path rolls the caller session
    # back, expiring every ORM instance — a post-refusal ``one_shot.id`` /
    # ``project.id`` read would raise MissingGreenlet under the asyncpg harness.
    one_shot_id = one_shot.id
    project_id = project.id

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
        refreshed = await s.get(Task, one_shot_id)
        assert (
            refreshed.context is None
            or refreshed.context.get("_resident_session") is not True
        )
        assert refreshed.status == TaskStatus.IN_PROGRESS

    # No provisional claim left behind to wedge future starts.
    vision = await _fresh_vision(factory, project_id)
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

    # Snapshot the id BEFORE the start: its refusal path rolls the caller session
    # back, expiring ``started`` — a post-refusal ``started.id`` read would raise
    # MissingGreenlet under the asyncpg harness.
    started_id = started.id

    # No adoptable cycle + the started one keeps _has_active_strategic_task hot →
    # spawn blocked → refuse rather than adopt the already-dispatched task.
    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session, project, operator_user_id=TEST_USER_ID
        )
    assert exc.value.status_code == 409
    refreshed = await db_session.get(Task, started_id)
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


async def _operator_decision_notices(db_session, project_id):
    """All operator-decision low-capture strategist-internal notices for a project."""
    res = await db_session.execute(
        select(ProjectMessage).where(ProjectMessage.project_id == project_id)
    )
    return [
        m
        for m in res.scalars().all()
        if (m.context or {}).get("event_type") == "operator_decision.capture_rate_low"
    ]


def test_operator_decision_capture_chronically_low_predicate():
    """The chronic-low predicate requires enough samples AND a below-floor rate."""
    # Below the sample floor — never chronic, even at 0%.
    assert srs._operator_decision_capture_chronically_low(
        {"total": 3, "captured": 0, "missed": 3, "capture_rate": 0}
    ) is False
    # Enough samples, rate below floor — chronic.
    assert srs._operator_decision_capture_chronically_low(
        {"total": 5, "captured": 2, "missed": 3, "capture_rate": 40}
    ) is True
    # Enough samples, rate at/above floor — not chronic (and a recovery).
    assert srs._operator_decision_capture_chronically_low(
        {"total": 5, "captured": 3, "missed": 2, "capture_rate": 60}
    ) is False
    assert srs._operator_decision_capture_recovered(
        {"total": 5, "captured": 3, "missed": 2, "capture_rate": 60}
    ) is True
    # Malformed / null-rate summaries are neither low nor recovered.
    assert srs._operator_decision_capture_chronically_low(None) is False
    assert srs._operator_decision_capture_chronically_low(
        {"total": 5, "captured": 0, "missed": 5, "capture_rate": None}
    ) is False


@pytest.mark.asyncio
async def test_heartbeat_surfaces_operator_decision_summary(db_session):
    """A heartbeat's operator_decision_summary round-trips through the snapshot."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)

    summary = {"total": 6, "captured": 5, "missed": 1, "capture_rate": 83}
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
        operator_decision_summary=summary,
    )
    fresh = await db_session.get(Project, project.id)
    snap = resident_session_snapshot(fresh.vision)
    assert snap["operator_decision_summary"] == summary
    # A healthy rate emits NO strategist-internal notice.
    assert await _operator_decision_notices(db_session, project.id) == []


@pytest.mark.asyncio
async def test_operator_decision_capture_low_rate_emits_strategist_internal_notice(
    db_session,
):
    """A chronically-low capture rate emits exactly one STRATEGIST_INTERNAL notice.

    Asserts the full policy: no emit below the sample floor, no emit above the
    low-rate threshold, exactly one emit on the low crossing (with a
    strategist-internal / non-operator-visible audience), dedup across repeated
    low heartbeats, and re-arming after a recovery.
    """
    from app.message_audience import STRATEGIST_INTERNAL, is_operator_visible

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)

    async def _beat(summary):
        proj = await db_session.get(Project, project.id)
        return await renew_resident_session_lease(
            db_session,
            proj,
            session_id=snapshot["session_id"],
            task_id=snapshot["task_id"],
            progress=True,
            operator_decision_summary=summary,
        )

    # Below the sample floor: low rate but too few turns → no notice.
    await _beat({"total": 2, "captured": 0, "missed": 2, "capture_rate": 0})
    assert await _operator_decision_notices(db_session, project.id) == []

    # Enough samples but above the threshold → no notice.
    await _beat({"total": 6, "captured": 5, "missed": 1, "capture_rate": 83})
    assert await _operator_decision_notices(db_session, project.id) == []

    # Chronic low: enough samples AND below the threshold → exactly one notice.
    await _beat({"total": 6, "captured": 2, "missed": 4, "capture_rate": 33})
    notices = await _operator_decision_notices(db_session, project.id)
    assert len(notices) == 1
    notice = notices[0]
    # Strategist-internal: bookkeeping only, must NOT reach the operator.
    assert notice.from_type == "system"
    assert notice.context.get("audience") == STRATEGIST_INTERNAL
    assert is_operator_visible(notice.context["audience"]) is False
    assert notice.context.get("capture_rate") == 33
    assert notice.context.get("session_id") == snapshot["session_id"]

    # Still low on the next heartbeat → deduped, no second notice.
    await _beat({"total": 8, "captured": 2, "missed": 6, "capture_rate": 25})
    assert len(await _operator_decision_notices(db_session, project.id)) == 1

    # Recovery above the floor re-arms the notice for a later crossing …
    await _beat({"total": 10, "captured": 9, "missed": 1, "capture_rate": 90})
    assert len(await _operator_decision_notices(db_session, project.id)) == 1
    # … a new low crossing fires a second, distinct notice.
    await _beat({"total": 12, "captured": 4, "missed": 8, "capture_rate": 33})
    assert len(await _operator_decision_notices(db_session, project.id)) == 2


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
async def test_stop_task_context_write_is_locked_and_merging(db_session):
    """stop_resident_session acquires the project row lock BEFORE the task row
    lock (canonical Project→Task order) and writes task.context as a locked
    read-modify-write via task_lock(populate_existing=True) rather than via a
    stale identity-map snapshot.

    Concretely: a context key committed by a prior strategic-API call must
    survive the stop's failure_category stamp — if the write used a stale
    identity-map copy instead of a fresh locked read, the pre-committed key would
    be silently dropped (lost update).  Clears v22 audit keys e1909456333ceda5
    (Project/Task lock-order inversion) and 8784112825bb8d87 (unlocked context
    write)."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)

    # Commit a context key that simulates a concurrent strategic-API write that
    # landed AFTER the session was started but BEFORE the stop runs.
    await db_session.refresh(task)
    ctx = dict(task.context or {})
    ctx["_strategic_api_called"] = 2
    task.context = ctx
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "ended"
    assert snapshot["stop_requested"] is True

    # Task driven terminal with failure_category set.
    refreshed = await db_session.get(Task, task.id)
    assert refreshed.status == TaskStatus.CANCELLED
    assert refreshed.context.get("failure_category") == "ADMIN_CANCEL"
    # Pre-existing context key must survive — locked read-modify-write, not a
    # stale-snapshot overwrite.
    assert refreshed.context.get("_strategic_api_called") == 2, (
        "stop_resident_session lost a pre-existing task context key — "
        "context write must be a locked read-modify-write"
    )

    # Continuation lease released (vision mutation still committed correctly).
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
async def test_operator_stop_runs_strategic_completion_processing_exactly_once(
    db_session, monkeypatch
):
    """Operator stop triggers _handle_strategic_completion exactly once on the
    cancelled task via _run_resident_completion_on_stop, and the stop itself
    still succeeds and returns the ended snapshot."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)
    project = await db_session.get(Project, project.id)

    calls = []

    async def fake_completion_on_stop(task_id, *, session_factory=None):
        calls.append(task_id)

    monkeypatch.setattr(
        srs, "_run_resident_completion_on_stop", fake_completion_on_stop
    )

    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Stop returned the ended snapshot.
    assert snapshot["status"] == "ended"
    assert snapshot["stop_requested"] is True

    # Completion processing was triggered exactly once with the right task id.
    assert len(calls) == 1
    assert calls[0] == task.id


@pytest.mark.asyncio
async def test_stop_completion_failure_does_not_mask_stop(db_session, monkeypatch):
    """An exception inside _run_resident_completion_on_stop must not propagate —
    stop_resident_session still returns the ended snapshot normally."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)
    project = await db_session.get(Project, project.id)

    async def failing_completion_on_stop(task_id, *, session_factory=None):
        raise RuntimeError("simulated completion failure")

    monkeypatch.setattr(
        srs, "_run_resident_completion_on_stop", failing_completion_on_stop
    )

    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # The stop must still succeed despite the completion handler raising.
    assert snapshot["status"] == "ended"
    assert snapshot["stop_requested"] is True


@pytest.mark.asyncio
async def test_run_resident_completion_on_stop_skips_when_no_strategic_activity(
    db_session, monkeypatch
):
    """_run_resident_completion_on_stop must NOT invoke _handle_strategic_completion
    and must NOT increment _strategic_failures when the task has no stashed
    strategic work and no API-activity markers — the false-failure case."""
    from app.strategic_resident_session import _run_resident_completion_on_stop

    from app.strategic_completion import (
        QUALIFY_REQUEST_CONTEXT_KEY,
        VERSION_PHASE_DECISION_CONTEXT_KEY,
        NEW_VERSION_GOAL_CONTEXT_KEY,
        NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
    )

    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    # No stashed strategic work keys or API-activity markers — only infra keys like
    # provides_capability may be present, which are not checked by _run_resident_completion_on_stop.
    ctx = task.context or {}
    _stash_keys = (
        QUALIFY_REQUEST_CONTEXT_KEY,
        VERSION_PHASE_DECISION_CONTEXT_KEY,
        NEW_VERSION_GOAL_CONTEXT_KEY,
        NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
    )
    _api_markers = (
        "_strategic_vision_set",
        "_strategic_dirs_created",
        "_strategic_dirs_retired",
        "_strategic_dirs_staged",
        "_strategic_dirs_updated",
        "_strategic_proposals_approved",
        "_strategic_proposals_rejected",
        "_strategic_tasks_spawned",
        "_strategic_api_called",
    )
    assert not any(ctx.get(k) for k in _stash_keys), f"Unexpected stash key in task context: {ctx}"
    assert not any(ctx.get(k) for k in _api_markers), f"Unexpected API marker in task context: {ctx}"

    completion_calls = []

    async def fake_handle_completion(t, logs, session, *, project=None):
        completion_calls.append({"task_id": t.id, "project": project})

    monkeypatch.setattr(
        "app.strategic_completion._handle_strategic_completion",
        fake_handle_completion,
    )

    factory = _dedicated_session_factory(db_session)
    await _run_resident_completion_on_stop(task.id, session_factory=factory)

    # Handler must NOT have been called for a task with no strategic work.
    assert completion_calls == [], (
        "_handle_strategic_completion was called despite no strategic activity"
    )

    # _strategic_failures must not have been written.
    fresh = await db_session.get(Project, project.id)
    assert "_strategic_failures" not in (fresh.vision or {}), (
        "_strategic_failures was incremented for an operator stop with no API activity"
    )


@pytest.mark.asyncio
async def test_run_resident_completion_on_stop_applies_stashed_work(
    db_session, monkeypatch
):
    """_run_resident_completion_on_stop must call _handle_strategic_completion with
    a non-None locked project when the task carries stashed strategic work — e.g.
    a qualify_request written by set_problem_model — and must re-fetch the task
    via the injected session_factory."""
    from app.strategic_resident_session import _run_resident_completion_on_stop
    from app.strategic_completion import QUALIFY_REQUEST_CONTEXT_KEY

    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)

    # Stash a qualify_request in the task context (set by set_problem_model API).
    ctx = dict(task.context or {})
    ctx[QUALIFY_REQUEST_CONTEXT_KEY] = {"reason": "ready for qualification"}
    task.context = ctx
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    completion_calls = []

    async def fake_handle_completion(t, logs, session, *, project=None):
        completion_calls.append({"task_id": t.id, "project": project})

    monkeypatch.setattr(
        "app.strategic_completion._handle_strategic_completion",
        fake_handle_completion,
    )

    # Bypass the FOR UPDATE lock (Postgres-only) while preserving the project
    # re-fetch so the test is runnable on SQLite.
    async def fake_lock_project(session, project_id, *, max_retries=3):
        return await session.get(Project, project_id)

    monkeypatch.setattr(
        "app.task_locking.lock_project_for_strategic_transition",
        fake_lock_project,
    )

    factory = _dedicated_session_factory(db_session)
    await _run_resident_completion_on_stop(task.id, session_factory=factory)

    # Handler must have been called exactly once with a non-None project.
    assert len(completion_calls) == 1, (
        f"Expected exactly one _handle_strategic_completion call, got {len(completion_calls)}"
    )
    assert completion_calls[0]["task_id"] == task.id
    assert completion_calls[0]["project"] is not None, (
        "_handle_strategic_completion was called with project=None (missing project lock)"
    )


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
    budget_source = Project(name="resident budget source", user_id=TEST_USER_ID)
    db_session.add(budget_source)
    await db_session.commit()
    db_session.add(
        Task(
            project_id=budget_source.id,
            title="Budget source task",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=_utcnow() - timedelta(minutes=5),
        )
    )
    await db_session.commit()
    original_mode = runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = runtime_config["max_global_autonomous_tasks_per_hour"]
    runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    # Deliberately NO adoptable strategic_review task exists → the start SPAWNS.

    try:
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
    finally:
        runtime_config["autonomous_spawn_budget_mode"] = original_mode
        runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = original_initiative
        runtime_config["max_global_autonomous_tasks_per_hour"] = original_global

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

    async def fake_spawn(session, proj, runner_id, *, session_id, exam_ledger=None, message_cursor=None, session_summary=None, decisions_register=None):
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

    # Snapshot the id BEFORE the start: the failure path rolls the caller session
    # back, expiring ``project`` — a post-failure ``project.id`` read would raise
    # MissingGreenlet under the asyncpg harness.
    project_id = project.id

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
    vision = await _fresh_vision(factory, project_id)
    assert read_resident_session(vision) is None


@pytest.mark.asyncio
async def test_start_compensation_receives_plain_snapshotted_project_id(
    db_session, monkeypatch
):
    """WI-7: start-failure compensation must receive a PLAIN id value snapshotted
    before ``session.rollback()`` — it must never re-read the (post-rollback)
    ``project.id`` ORM attribute.

    The ``except`` block's ``session.rollback()`` expires ``project``'s ORM
    attributes; under asyncpg a subsequent ``project.id`` read emits a sync
    lazy-load in an async context → ``MissingGreenlet`` (the ~1-2/hr prod wedge).

    Fault-injection: a proxy wraps ``project`` and raises ``MissingGreenlet`` on
    ``.id`` access once ``session.rollback()`` has been called.  With the fix,
    the except block captures ``project_id = project.id`` (via
    ``proxy.id``) BEFORE the rollback, so the subsequent compensation call
    uses the pre-captured scalar and never touches the poisoned proxy.  Without
    the fix, code that read ``project.id`` after rollback would trigger the
    proxy's guard and raise ``MissingGreenlet`` — exactly the production failure.
    """

    class _ProjectIdPoisonAfterRollback:
        """Delegates ``.id`` to the real project until ``session.rollback()``
        is called; after that ``.id`` raises ``MissingGreenlet``."""

        def __init__(self, real_project):
            self._real = real_project
            self._rolled_back = False

        @property
        def id(self):
            if self._rolled_back:
                raise MissingGreenlet(
                    "simulated post-rollback lazy-load of expired 'id'"
                )
            return self._real.id

        def __getattr__(self, name):
            return getattr(self._real, name)

    project = await _make_project_with_member(db_session)  # no live strategic task
    await _make_live_local_runner(db_session, project)
    # Snapshot the expected id while ``project`` is still loaded (before the run).
    expected_id = project.id

    proxy = _ProjectIdPoisonAfterRollback(project)
    real_rollback = db_session.rollback

    async def rollback_then_poison(*args, **kwargs):
        result = await real_rollback(*args, **kwargs)
        proxy._rolled_back = True
        return result

    monkeypatch.setattr(db_session, "rollback", rollback_then_poison)

    async def fake_spawn(session, proj, runner_id, *, session_id, exam_ledger=None, message_cursor=None, session_summary=None, decisions_register=None):
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
        return t

    async def boom(*args, **kwargs):
        raise RuntimeError("finalize boom")

    monkeypatch.setattr(srs, "_spawn_resident_strategic_task", fake_spawn)
    monkeypatch.setattr(srs, "persist_strategic_workbench_update", boom)

    captured: dict = {}
    real_release = srs._release_start_claim

    async def spy_release(
        project_id, session_id, spawned_task_id, *, session_factory=None
    ):
        captured["project_id"] = project_id
        return await real_release(
            project_id, session_id, spawned_task_id, session_factory=session_factory
        )

    monkeypatch.setattr(srs, "_release_start_claim", spy_release)

    factory = _dedicated_session_factory(db_session)
    with pytest.raises(RuntimeError):
        await start_resident_session(
            db_session,
            proxy,
            operator_user_id=TEST_USER_ID,
            session_factory=factory,
        )

    # Compensation ran and received the pre-rollback SNAPSHOT of the id: correct
    # value, concrete uuid.UUID (same plain type — not a coroutine/deferred).
    assert captured, "compensation (_release_start_claim) was never invoked"
    assert captured["project_id"] == expected_id
    assert type(captured["project_id"]) is type(expected_id)
    # Claim actually cleared → compensation completed (not aborted mid-way).
    # Use the pre-rollback ``expected_id`` snapshot: ``project`` was expired by
    # the failure path's rollback, so ``project.id`` would raise MissingGreenlet.
    vision = await _fresh_vision(factory, expected_id)
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
    # Snapshot ids BEFORE the sync: it rolls the caller session back up front,
    # expiring every ORM instance — post-sync ``.id`` reads would raise
    # MissingGreenlet under the asyncpg harness.
    project_id, task_id, runner_id = project.id, task.id, runner.id

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
    assert snapshot["task_id"] == str(task_id)
    assert snapshot["runner_id"] == str(runner_id)

    vision = await _fresh_vision(factory, project_id)
    assert resident_session_is_active(read_resident_session(vision))
    # No blocked-reason recorded when the auto-start succeeds.
    assert read_resident_session_blocked_reason(vision) is None


@pytest.mark.asyncio
async def test_sync_resident_snapshots_project_id_before_rollback(
    db_session, enable_strategic_session_flag, monkeypatch
):
    """WI-7 regression (true fault-injecting guard): the interaction-mode sync
    snapshots ``project.id`` BEFORE its up-front ``session.rollback()`` and never
    re-reads the (post-rollback) ORM attribute.

    ``sync_resident_session_for_interaction_mode`` only ever reads ``project.id``
    (it never ``refresh``-es or ``add``-s the instance), so this passes a proxy
    whose ``.id`` starts delegating to the real id and, once the rollback fires,
    raises ``MissingGreenlet`` — exactly what asyncpg does when the code lazy-reads
    an expired attribute after a rollback in an async context. This faithfully
    reproduces the prod fault the SQLite harness cannot (it retains the PK and
    services re-loads silently).

    With the fix (id snapshotted before the rollback) the sync passes the plain
    snapshot to ``_auto_start_on_workday_entry`` and succeeds → active snapshot.
    On the PRE-FIX code the post-rollback ``project.id`` read raises inside the
    ``try`` and the whole sync is swallowed to ``None`` (a silently wedged auto-
    start) — so this assertion fails, catching the regression.
    """
    from sqlalchemy.exc import MissingGreenlet

    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)

    class _ProjectIdPoisonAfterRollback:
        """Delegates ``.id`` to the real project until the caller rolls back;
        after that a ``.id`` read raises ``MissingGreenlet`` (mimics asyncpg's
        post-rollback expired-attribute lazy-load in an async context)."""

        def __init__(self, real_id):
            self._real_id = real_id
            self._rolled_back = False

        @property
        def id(self):
            if self._rolled_back:
                raise MissingGreenlet(
                    "simulated post-rollback lazy-load of an expired attribute"
                )
            return self._real_id

    proxy = _ProjectIdPoisonAfterRollback(project.id)

    real_rollback = db_session.rollback

    async def rollback_then_poison(*args, **kwargs):
        await real_rollback(*args, **kwargs)
        proxy._rolled_back = True

    monkeypatch.setattr(db_session, "rollback", rollback_then_poison)

    captured: dict = {}
    real_auto_start = srs._auto_start_on_workday_entry

    async def spy_auto_start(project_id, *, operator_user_id, session_factory):
        captured["project_id"] = project_id
        return await real_auto_start(
            project_id,
            operator_user_id=operator_user_id,
            session_factory=session_factory,
        )

    monkeypatch.setattr(srs, "_auto_start_on_workday_entry", spy_auto_start)

    factory = _dedicated_session_factory(db_session)
    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        proxy,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=factory,
    )

    # Fix present: the pre-rollback snapshot let the auto-start run to success.
    assert snapshot is not None and snapshot["status"] == "active"
    assert captured, "auto-start hand-off was never reached (post-rollback read?)"
    assert captured["project_id"] == proxy._real_id
    assert type(captured["project_id"]) is type(proxy._real_id)


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
    # Snapshot ids BEFORE the sync: its up-front rollback expires every ORM
    # instance, so post-sync ``.id`` reads would raise MissingGreenlet.
    project_id, task_id = project.id, task.id
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

    vision = await _fresh_vision(factory, project_id)
    blob = read_resident_session(vision)
    assert blob is not None and blob["status"] == "ended"
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in vision
    assert await _fresh_task_status(factory, task_id) == TaskStatus.CANCELLED


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
    # Snapshot the id BEFORE the sync: its up-front rollback expires every ORM
    # instance, so a post-sync ``project.id`` read would raise MissingGreenlet.
    project_id = project.id

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

    vision = await _fresh_vision(factory, project_id)
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

    # Snapshot the id BEFORE the sync: its up-front rollback expires every ORM
    # instance, so a post-sync ``project.id`` read would raise MissingGreenlet.
    project_id = project.id

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

    vision = await _fresh_vision(factory, project_id)
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
    # Snapshot ids BEFORE the sync: its up-front rollback expires every ORM
    # instance, so post-sync ``.id`` reads would raise MissingGreenlet.
    project_id, task_id = project.id, task.id

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
    assert snapshot["task_id"] == str(task_id)

    # The start ran on a dedicated session, never the caller's request session.
    assert len(opened) >= 1
    assert all(s is not db_session for s in opened)

    # The caller's request session stays clean/usable, and the committed session
    # blob is observable (read via a fresh session — see ``_fresh_vision``).
    vision = await _fresh_vision(base_factory, project_id)
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


def test_availability_start_failed_wins_over_active_blob(enable_strategic_session_flag):
    """A recorded start_failed reason ALWAYS wins — even if the session blob is
    still marked active (stale blob state). The read model must report 'error',
    not 'starting' or 'active'. This ensures a real failure is never masked by
    a transient blob state."""
    # Both active blob AND recorded start_failed: the reason must win.
    result = _resident_session_availability(
        _project_with_vision(
            {
                RESIDENT_SESSION_KEY: {
                    "status": "active",
                    "stop_requested": False,
                    "started_at": "2026-01-01T00:00:00",
                    "last_progress_at": None,
                    "last_operator_interaction_at": "2026-01-01T00:00:00",
                },
                RESIDENT_SESSION_REASON_KEY: {"reason": "start_failed: agent init failed"},
            }
        )
    )
    assert result["state"] == "error"
    assert result["reason"] == "start_failed: agent init failed"


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


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on pool pre-ping is asyncpg-specific; aiosqlite uses a "
        "single shared connection that does not pre-ping and handles re-use "
        "transparently, masking the poisoned-session failure mode. Fault injection "
        "would require intercepting SQLAlchemy connection-pool internals."
    ),
)
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


async def _make_operator_message(
    db_session, project, *, content="hi", created_at=None, resident_session_id=None
):
    """Insert an operator-authored (from_type='user') open ProjectMessage.

    ``resident_session_id`` stamps the row with the session it was sent into
    (``None`` = legacy/cadence, always treated as unscoped/visible).
    """
    msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        content=content,
        status="open",
        resident_session_id=resident_session_id,
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


@pytest.mark.asyncio
async def test_heartbeat_work_state_notices_descendant_task_transitions(db_session):
    """The work-state signal must advance when descendant tasks (children of
    strategist-spawned tasks) transition, not just direct STRATEGIC_AGENT tasks.
    When a plan child or follow-up fix blocks/completes, the wake signal advances
    so the resident reacts promptly, not waiting for the root task."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Create a root task spawned by the strategist
    root = await _make_strategic_agent_task(
        db_session, project, status=TaskStatus.IN_PROGRESS
    )

    # Create a descendant (child) task with a different created_by
    child = Task(
        project_id=project.id,
        parent_task_id=root.id,
        title="child of strategist spawn",
        payloads=[{"type": "research"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    # First heartbeat: capture the initial work_state_rev
    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    rev1 = r1["work_state_rev"]
    # Root is in-flight, child is in-flight → 2 in-flight descendant-aware tasks
    assert r1["work_state_summary"]["in_flight"] >= 1

    # Transition the child task (not a STRATEGIC_AGENT task, but a descendant)
    child.status = TaskStatus.COMPLETED
    db_session.add(child)
    await db_session.commit()

    # Second heartbeat: work_state_rev must advance because child transitioned
    project = await db_session.get(Project, project.id)
    r2 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    rev2 = r2["work_state_rev"]

    # The rev must have advanced (descendant transition detected).
    assert rev2 is not None
    assert rev2 > rev1, (
        f"work_state_rev should advance when descendant transitions: {rev1} → {rev2}"
    )
    # Child no longer in-flight; summary should reflect it (fewer in-flight).
    assert r2["work_state_summary"]["terminal_since"] >= 1


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


# ---------------------------------------------------------------------------
# Session-scope isolation (design b182b46e / migration 0396): a message stamped
# for a DIFFERENT resident session must not re-arm THIS session's unattended cap
# or pollute its pacing signal, while legacy/cadence NULL-session rows stay
# visible so no pre-migration backlog is silently dropped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unattended_cap_ignores_other_session_message(db_session, monkeypatch):
    """A FRESH operator message stamped for a DIFFERENT resident session must NOT
    re-arm this session's unattended cap. Without session scoping the cross-session
    message would keep the clock hot (the bleed this feature eliminates); with it,
    the message is invisible here so the aged clock still fires and the session
    ends."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

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

    # Fresh message addressed to ANOTHER session — must not re-arm our clock.
    await _make_operator_message(
        db_session, project, content="for session A", resident_session_id="ss-other"
    )

    project = await db_session.get(Project, project.id)
    capped = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    assert capped["stop_requested"] is True
    assert capped["status"] == "ended"


@pytest.mark.asyncio
async def test_unattended_cap_rearmed_by_null_session_message(db_session, monkeypatch):
    """A FRESH legacy/cadence NULL-session operator message REMAINS visible and
    re-arms this session's unattended cap (backward compat: NULL is never hidden),
    so the session keeps its authority instead of ending."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

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

    # Fresh NULL-session (legacy/cadence) message — visible, re-arms the clock.
    await _make_operator_message(
        db_session, project, content="unstamped", resident_session_id=None
    )

    project = await db_session.get(Project, project.id)
    reengaged = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    assert reengaged["authority_held"] is True
    assert reengaged["stop_requested"] is False
    assert reengaged["status"] == "active"


@pytest.mark.asyncio
async def test_work_state_pacing_scoped_to_session(db_session):
    """``new_operator_messages`` counts only messages addressed to THIS session
    (its own stamp) plus unstamped NULL rows — a message stamped for another
    session is excluded so it cannot drive this session's pacing / work_state."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    this_session = snap["session_id"]

    # One for us, one NULL (legacy/cadence), one for another session.
    await _make_operator_message(
        db_session, project, resident_session_id=this_session
    )
    await _make_operator_message(db_session, project, resident_session_id=None)
    await _make_operator_message(
        db_session, project, resident_session_id="ss-other"
    )

    _, summary = await srs._compute_resident_work_state(
        db_session, project.id, resident_session_id=this_session
    )
    # Our stamp + the NULL row are counted; the other-session row is not.
    assert summary["new_operator_messages"] == 2

    # A None scope (older caller / cadence path) still counts every message
    # project-wide — the backward-compatible behaviour is unchanged.
    _, summary_all = await srs._compute_resident_work_state(
        db_session, project.id
    )
    assert summary_all["new_operator_messages"] == 3


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

    async def fake_spawn(
        session,
        proj,
        runner_id,
        *,
        session_id,
        exam_ledger=None,
        message_cursor=None,
        session_summary=None,
        decisions_register=None,
    ):
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
    assert extra["provider_preference"] == ["anthropic", "zai", "deepseek"]
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
    """With no runner, born-context still constrains the resident protocol to
    Claude-compatible providers but carries no local-runner pin."""
    ctx = srs._resident_born_context("ss-nopin", None)
    assert ctx["_resident_session"] is True
    assert ctx["strategic_session_id"] == "ss-nopin"
    assert ctx["provider_preference"] == ["anthropic", "zai", "deepseek"]
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
async def test_exam_ledger_round_trips_through_task_context(db_session):
    """FULL-RESTART round-trip: the heartbeat-persisted exam ledger is carried
    forward into the NEXT resident session's ``task.context["_exam_ledger"]`` so
    the agent restores the examination rotation on startup instead of resetting
    to never-examined-first.

    A real restart ENDS the prior resident task and starts a fresh session on a
    NEW (spawned/adopted) task; the ledger the heartbeat mirrored onto the OLD
    task's context is not reachable from the new task. This test persists a
    ledger via a session-1 heartbeat, drives that task terminal, starts a second
    session that ADOPTS a fresh strategic_review task, and asserts the SAME
    records round-trip losslessly through the new task's context (read back via a
    FRESH session — the delivery path an agent restart actually sees).
    """
    import json as _json

    project = await _make_project_with_member(db_session)
    task1 = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap1 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Session 1 heartbeat carrying the serialized ledger → mirrored into task1's
    # context AND the session blob.
    note = _json.dumps(_SAMPLE_LEDGER)
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project, session_id=snap1["session_id"],
        task_id=snap1["task_id"], progress=False, note=note,
    )
    task1 = await db_session.get(Task, task1.id)
    assert (task1.context or {}).get("_exam_ledger") is not None  # same-task path

    # Simulate a real restart: the old resident task goes terminal and a fresh
    # adoptable strategic_review is queued to host the next session.
    task1.status = TaskStatus.COMPLETED
    db_session.add(task1)
    task2 = await _make_strategic_review_task(db_session, project)
    await db_session.commit()

    # Start session 2 → adopts task2, carrying the ledger forward from the prior
    # session blob into the NEW task's context.
    project = await db_session.get(Project, project.id)
    snap2 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap2["session_id"] != snap1["session_id"]
    assert str(snap2["task_id"]) == str(task2.id)

    # Read back via a FRESH session (the task.context an agent restart is served).
    factory = _dedicated_session_factory(db_session)
    async with factory() as s:
        fresh_task2 = await s.get(Task, task2.id)
        carried = (fresh_task2.context or {}).get("_exam_ledger")
    assert carried is not None, "exam ledger not carried into the new session's task"

    # Lossless: every field of every record survives the persist → restart → read.
    restored = _json.loads(carried)
    assert restored["tick"] == _SAMPLE_LEDGER["tick"]
    assert restored["corrective"] == _SAMPLE_LEDGER["corrective"]
    assert restored["records"] == _SAMPLE_LEDGER["records"]

    # The carried ledger is NOT leaked by the operator-facing session snapshot.
    fresh_project = await db_session.get(Project, project.id)
    snap_dict = resident_session_snapshot(fresh_project.vision)
    assert snap_dict is not None and "_exam_ledger" not in snap_dict


@pytest.mark.asyncio
async def test_session_summary_round_trips_through_task_context(db_session):
    """FULL-RESTART round-trip: the ``set_session_summary`` op's persisted
    ``_session_summary`` is carried forward into the NEXT resident session's
    ``task.context["_session_summary"]`` so the agent receives "Prior session
    context" on restart instead of starting blank.

    A real restart ENDS the prior resident task and starts a fresh session on a
    NEW (spawned/adopted) task; the summary the op wrote onto the OLD task's
    context is not reachable from the new task. This test persists a summary
    into the session-1 blob (via the same path ``set_session_summary`` takes),
    drives that task terminal, starts a second session that ADOPTS a fresh
    ``strategic_review`` task, and asserts the SAME summary round-trips
    losslessly through the new task's context (read back via a FRESH session).
    """
    project = await _make_project_with_member(db_session)
    task1 = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap1 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Write _session_summary into the session-1 blob (the same path
    # set_session_summary uses so it survives a restart).
    SUMMARY = "Research revealed three key gap areas to investigate."
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = read_resident_session(vision)
    assert blob is not None, "session-1 blob must exist"
    blob["_session_summary"] = SUMMARY
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # Confirm the blob now carries the summary.
    fresh_project = await db_session.get(Project, project.id)
    fresh_blob = read_resident_session(fresh_project.vision)
    assert fresh_blob is not None
    assert fresh_blob.get("_session_summary") == SUMMARY

    # Simulate a real restart: the old resident task goes terminal and a fresh
    # adoptable strategic_review is queued to host the next session.
    task1 = await db_session.get(Task, task1.id)
    task1.status = TaskStatus.COMPLETED
    db_session.add(task1)
    task2 = await _make_strategic_review_task(db_session, project)
    await db_session.commit()

    # Start session 2 → adopts task2, carrying the summary forward from the
    # prior session blob into the NEW task's context.
    project = await db_session.get(Project, project.id)
    snap2 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap2["session_id"] != snap1["session_id"]
    assert str(snap2["task_id"]) == str(task2.id)

    # Read back via a FRESH session (the task.context an agent restart sees).
    factory = _dedicated_session_factory(db_session)
    async with factory() as s:
        fresh_task2 = await s.get(Task, task2.id)
        carried = (fresh_task2.context or {}).get("_session_summary")
    assert carried is not None, "_session_summary not carried into the new session's task"
    assert carried == SUMMARY

    # The carried _session_summary is NOT leaked by the operator-facing snapshot.
    fresh_project = await db_session.get(Project, project.id)
    snap_dict = resident_session_snapshot(fresh_project.vision)
    assert snap_dict is not None and "_session_summary" not in snap_dict


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


@pytest.mark.asyncio
async def test_checkpoint_state_surfaces_recent_handled_conversation(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    """The checkpoint-state endpoint carries a recently-ANSWERED operator
    conversation tail so a correction the operator made — now marked handled and
    gone from the open feed — survives a compaction restart.

    Invariants asserted:
      * a handled operator message appears in recent_handled_conversation but NOT
        in open_messages (the two feeds are disjoint by status);
      * an OPEN message appears in open_messages but NOT in the handled tail;
      * a `system` (internal bookkeeping) message is excluded from the tail even
        when handled — the tail is operator conversation only.
    """
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project, vm="agent-vm-rh")
    await _make_live_local_runner(db_session, project)
    await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    await db_session.refresh(task)

    # A handled operator message — the just-answered correction. Marked handled so
    # it has dropped out of the open feed entirely.
    handled = await _make_operator_message(
        db_session, project, content="actually, drop the caching idea"
    )
    handled.status = "handled"
    db_session.add(handled)
    # A still-open operator message — belongs in open_messages, not the tail.
    still_open = await _make_operator_message(
        db_session, project, content="what is the ETA on the runner fix?"
    )
    # A handled SYSTEM message — internal bookkeeping, must never reach the tail.
    sys_msg = ProjectMessage(
        project_id=project.id,
        from_type="system",
        content="internal: cycle rollover bookkeeping",
        status="handled",
        message_type=MessageType.NOTE,
    )
    db_session.add(sys_msg)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert "recent_handled_conversation" in body, (
        "checkpoint-state must include recent_handled_conversation key"
    )
    tail = body["recent_handled_conversation"]
    assert isinstance(tail, list)
    tail_ids = {m["id"] for m in tail}
    tail_contents = {m["content"] for m in tail}
    open_ids = {m["id"] for m in body["open_messages"]}

    # The handled operator correction is in the tail, and NOT in the open feed.
    assert str(handled.id) in tail_ids
    assert "actually, drop the caching idea" in tail_contents
    assert str(handled.id) not in open_ids

    # The still-open message is in the open feed, and NOT in the handled tail.
    assert str(still_open.id) in open_ids
    assert str(still_open.id) not in tail_ids

    # The handled system message is internal bookkeeping — excluded from the tail.
    assert str(sys_msg.id) not in tail_ids
    assert "internal: cycle rollover bookkeeping" not in tail_contents


@pytest.mark.asyncio
async def test_checkpoint_state_endpoint_returns_current_data(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    """The checkpoint-state endpoint returns the current problem model, memory
    notes, running update, and open operator messages — not the bootstrap
    snapshot — so the compaction checkpoint carries fresh state after mid-session
    mutations (state edits AND messages filed/resolved)."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project, vm="agent-vm-cp")
    await _make_live_local_runner(db_session, project)
    await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    await db_session.refresh(task)

    # Set up initial project state.
    project.problem_model = {"claim": "original"}
    project.strategic_memory_notes = [{"note": "original note", "set_at": "2025-01-01T00:00:00"}]
    project.running_update = "original update"
    flag_modified(project, "problem_model")
    flag_modified(project, "strategic_memory_notes")
    await db_session.commit()

    # An open operator message present at session start — must be reflected in the
    # endpoint's open_messages feed.
    msg_start = await _make_operator_message(
        db_session, project, content="original operator ask"
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["problem_model"] == {"claim": "original"}
    assert body["strategic_memory_notes"] == [{"note": "original note", "set_at": "2025-01-01T00:00:00"}]
    assert body["running_update"] == "original update"
    assert isinstance(body["vision"], dict)
    # open_messages carries the current open feed, each row with its full id.
    assert isinstance(body["open_messages"], list)
    open_ids = {m["id"] for m in body["open_messages"]}
    open_contents = {m["content"] for m in body["open_messages"]}
    assert str(msg_start.id) in open_ids
    assert "original operator ask" in open_contents

    # Simulate a mid-session mutation: update the project state AND change the
    # open-message set — resolve the start message, file a new one after session
    # start.
    project.problem_model = {"claim": "updated"}
    project.strategic_memory_notes = [{"note": "updated note", "set_at": "2025-06-01T00:00:00"}]
    project.running_update = "updated update"
    flag_modified(project, "problem_model")
    flag_modified(project, "strategic_memory_notes")
    msg_start.status = "resolved"
    db_session.add(msg_start)
    await db_session.commit()
    msg_new = await _make_operator_message(
        db_session, project, content="new mid-session operator ask"
    )

    res2 = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    # Must carry the updated state, not the original.
    assert body2["problem_model"] == {"claim": "updated"}
    assert body2["strategic_memory_notes"] == [{"note": "updated note", "set_at": "2025-06-01T00:00:00"}]
    assert body2["running_update"] == "updated update"
    # And the CURRENT open-message set: the resolved start message drops out, the
    # newly-filed one appears — the exact staleness this refresh closes.
    open_ids2 = {m["id"] for m in body2["open_messages"]}
    open_contents2 = {m["content"] for m in body2["open_messages"]}
    assert str(msg_start.id) not in open_ids2
    assert "original operator ask" not in open_contents2
    assert str(msg_new.id) in open_ids2
    assert "new mid-session operator ask" in open_contents2

    # open_messages must be present and reflect the CURRENT open-message state.
    assert "open_messages" in body2, "checkpoint-state must include open_messages key"
    assert isinstance(body2["open_messages"], list)
    # NOTE: an "assert body2['open_messages'] == []" used to sit here, left over from
    # an earlier revision of this test that filed no messages before this point. It
    # directly contradicts the assertions just above — msg_new was filed open at this
    # point, so the feed is non-empty BY DESIGN and is asserted exactly (resolved
    # message out, new one in) at the block above. The precise membership checks are
    # strictly stronger than an emptiness check, so nothing is lost by its removal.

    # Add an open message and confirm it appears.
    msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        content="open question",
        status="open",
        message_type=MessageType.QUESTION,
    )
    db_session.add(msg)
    await db_session.commit()

    res3 = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res3.status_code == 200, res3.text
    body3 = res3.json()
    assert "open_messages" in body3
    msg_ids = [m["id"] for m in body3["open_messages"]]
    assert str(msg.id) in msg_ids, "open message must appear in checkpoint-state feed"

    # Resolve the message and confirm it disappears.
    msg.status = "resolved"
    db_session.add(msg)
    await db_session.commit()

    res4 = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res4.status_code == 200, res4.text
    body4 = res4.json()
    resolved_ids = [m["id"] for m in body4["open_messages"]]
    assert str(msg.id) not in resolved_ids, "resolved message must not appear in checkpoint-state feed"


@pytest.mark.asyncio
async def test_checkpoint_state_applies_agent_view_trim(
    client: AsyncClient, db_session, enable_strategic_session_flag
):
    """The checkpoint-state refresh must return the SAME agent-view-trimmed shape
    the session bootstrap ships — not raw project.problem_model / project.vision.

    The compaction checkpoint refreshes grounding from this endpoint. If it
    returned raw project state, a successful refresh would re-bloat the restarted
    session's grounding with archived_gaps, the rejected_agent_edits ledger,
    unwindowed per-gap memory, and the internal vision continuation blob — a
    larger, differently-shaped grounding than the session bootstrapped with, which
    is the opposite of the coherence the refresh was added to preserve.
    """
    from app.strategic_problem_view import (
        _GAP_HISTORY_KEEP,
        _agent_view_problem_model,
    )

    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project, vm="agent-vm-trim")
    await _make_live_local_runner(db_session, project)
    await start_resident_session(db_session, project, operator_user_id=TEST_USER_ID)
    await db_session.refresh(task)

    # A problem_model carrying every unbounded field the agent-view trim caps:
    # archived_gaps, the rejected_agent_edits ledger, and an active gap whose
    # append-only history exceeds the retained window.
    long_history = [{"tick": i, "note": f"h{i}"} for i in range(_GAP_HISTORY_KEEP + 7)]
    project.problem_model = {
        "claim": "trim-me",
        "archived_gaps": [{"id": "g-archived", "status": "closed"}],
        "rejected_agent_edits": [{"id": "rej-1", "reason": "stale"}],
        "gaps": [{"id": "g-live", "status": "open", "history": long_history}],
    }
    # A vision carrying the backend-only continuation blob that _agent_visible_vision strips.
    project.vision = {
        "north_star": "keep me",
        STRATEGIC_LOCAL_CONTINUATION_KEY: {"secret": "internal-only"},
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "vision")
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/strategic-session/checkpoint-state",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    pm = body["problem_model"]
    # archived_gaps and the rejected-edits ledger must be stripped.
    assert "archived_gaps" not in pm, "archived_gaps must be stripped from refreshed problem_model"
    assert "rejected_agent_edits" not in pm, "rejected_agent_edits must be stripped from refreshed problem_model"
    # Per-gap history must be windowed to the retained tail, not shipped whole.
    live_gap = next(g for g in pm["gaps"] if g["id"] == "g-live")
    assert len(live_gap["history"]) == _GAP_HISTORY_KEEP, "gap history must be windowed to the retained window"
    assert live_gap["history"] == long_history[-_GAP_HISTORY_KEEP:], "history window must keep the most recent entries"
    # The durable, non-trimmable content survives.
    assert pm["claim"] == "trim-me"

    # The endpoint output must be byte-identical to applying the bootstrap trim helper directly.
    assert pm == _agent_view_problem_model(project.problem_model)

    # Vision must drop the internal continuation blob while keeping operator-visible fields.
    assert body["vision"] == {"north_star": "keep me"}
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in body["vision"], (
        "internal continuation blob must be stripped from refreshed vision"
    )


# ---------------------------------------------------------------------------
# cycle_in_flight: named block reason + completion retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workday_entry_while_cycle_in_flight_records_named_reason(
    db_session, enable_strategic_session_flag
):
    """Entering Workday while a batch strategic cycle is IN_PROGRESS (non-adoptable)
    records ``cycle_in_flight`` — NOT the generic ``start_failed`` reason — and the
    read model surfaces ``blocked`` (not ``error``) with the named reason.

    This exercises the new pre-check in ``_auto_start_on_workday_entry`` that
    detects the spawn-guard condition before the start path raises a 409.
    """
    project = await _make_project_with_member(db_session)
    # A runner is required to get past the no_runner gate; the cycle_in_flight
    # check comes AFTER the runner check so both must pass.
    await _make_live_local_runner(db_session, project)
    # An IN_PROGRESS strategic_review task (not adoptable — started_at is set)
    # means: nothing to adopt AND _has_active_strategic_task returns True.
    in_flight = Task(
        project_id=project.id,
        title="batch cycle",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-batch",
        started_at=_utcnow(),
        created_by="ORCHESTRATOR",
    )
    db_session.add(in_flight)
    await db_session.commit()

    factory = _dedicated_session_factory(db_session)
    snapshot = await sync_resident_session_for_interaction_mode(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        previous_mode="chat",
        new_mode="continuous",
        session_factory=factory,
    )
    # Auto-start was blocked — no session started.
    assert snapshot is None

    vision = await _fresh_vision(factory, project.id)
    blocked_reason = read_resident_session_blocked_reason(vision)
    # Must be the named cycle_in_flight reason, NOT the generic start_failed.
    assert blocked_reason == RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT

    # The read model must surface ``blocked`` (not ``error``).
    avail = _resident_session_availability(
        _project_with_vision(vision), eligible_live_runner_count=1
    )
    assert avail["state"] == "blocked"
    assert avail["reason"] == RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT

    # The operator_user_id must be stored so the retry handler can use it.
    raw_blob = vision.get(RESIDENT_SESSION_REASON_KEY) or {}
    assert raw_blob.get("operator_user_id") == TEST_USER_ID


@pytest.mark.asyncio
async def test_cycle_completion_retries_workday_auto_start(
    db_session, enable_strategic_session_flag
):
    """Completing a blocking strategic cycle re-triggers the Workday auto-start
    when the project is in continuous mode with a ``cycle_in_flight`` block.

    This exercises ``_maybe_retry_workday_auto_start`` from
    ``app.strategic_completion``, which reads the blocked reason, checks the
    interaction mode, and calls ``_auto_start_on_workday_entry`` on a fresh
    session. The retry succeeds when an adoptable task and a live runner are
    available after the blocking cycle finishes.
    """
    from app.strategic_completion import _maybe_retry_workday_auto_start
    from app.workbench_state import upsert_strategic_workbench_state_with_previous

    project = await _make_project_with_member(db_session)
    # Provide a live runner so the retry auto-start can pick a device.
    await _make_live_local_runner(db_session, project)
    # Provide an adoptable strategic_review task (QUEUED, no started_at) so the
    # retry can adopt it rather than spawning (which would be blocked by the
    # still-present in-flight task state).
    adoptable_task = await _make_strategic_review_task(db_session, project)

    # Set interaction_mode=continuous via the real per-user prefs store
    # (MessagesNoisePrefs), which is where workbench state is actually persisted.
    await upsert_strategic_workbench_state_with_previous(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "continuous"},
    )

    # Manually set the RESIDENT_SESSION_REASON_KEY == cycle_in_flight in vision.
    project = await db_session.get(Project, project.id)
    from sqlalchemy.orm.attributes import flag_modified as _fm

    vision = dict(project.vision or {})
    vision[RESIDENT_SESSION_REASON_KEY] = {
        "reason": RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
        "recorded_at": _utcnow().isoformat(),
        "operator_user_id": TEST_USER_ID,
    }
    project.vision = vision
    _fm(project, "vision")
    db_session.add(project)
    await db_session.commit()

    factory = _dedicated_session_factory(db_session)
    # Call the retry helper directly (as strategic_completion does post-commit).
    await _maybe_retry_workday_auto_start(project.id, session_factory=factory)

    # The retry should have started a session: the session blob is now active.
    vision_after = await _fresh_vision(factory, project.id)
    assert resident_session_is_active(read_resident_session(vision_after)), (
        "Expected an active resident session after the cycle_in_flight retry"
    )
    # The session must have adopted the available strategic_review task.
    blob = read_resident_session(vision_after) or {}
    assert blob.get("task_id") == str(adoptable_task.id)
    # The blocked reason is cleared on a successful start.
    assert read_resident_session_blocked_reason(vision_after) is None


# ---------------------------------------------------------------------------
# message_cursor + handled_message_ids (durable resident message state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_persists_message_cursor_in_session_blob_and_task_context(db_session):
    """A heartbeat carrying ``message_cursor`` writes it to both the session blob
    (``_message_cursor``) and ``task.context["message_cursor"]``, so a restarted
    agent restores the high-water mark instead of re-delivering answered messages."""
    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    cursor = "2026-07-06T12:34:56.789000"
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=False,
        message_cursor=cursor,
    )

    # The heartbeat itself succeeds.
    assert result["stop_requested"] is False

    # The cursor is persisted in the session blob.
    fresh_project = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh_project.vision)
    assert blob is not None
    assert blob.get("_message_cursor") == cursor

    # The cursor is mirrored into task.context so the agent reads it on restart.
    fresh_task = await db_session.get(Task, task.id)
    assert fresh_task is not None
    assert (fresh_task.context or {}).get("message_cursor") == cursor

    # The cursor must NOT appear in the operator-facing session snapshot.
    snap_dict = resident_session_snapshot(fresh_project.vision)
    assert snap_dict is not None
    assert "_message_cursor" not in snap_dict


@pytest.mark.asyncio
async def test_start_carries_message_cursor_forward_across_restart(db_session):
    """A cursor persisted by the session-1 heartbeat is carried into the NEW
    task's ``context["message_cursor"]`` when a second session starts — the same
    round-trip the exam ledger uses."""
    project = await _make_project_with_member(db_session)
    task1 = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap1 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    cursor = "2026-07-06T10:00:00.000000"
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap1["session_id"],
        task_id=snap1["task_id"],
        progress=False,
        message_cursor=cursor,
    )

    # Drive session-1 task to terminal and queue a fresh task for adoption.
    task1 = await db_session.get(Task, task1.id)
    task1.status = TaskStatus.COMPLETED
    db_session.add(task1)
    task2 = await _make_strategic_review_task(db_session, project)
    await db_session.commit()

    # Start session 2 → adopts task2; cursor should be carried forward.
    project = await db_session.get(Project, project.id)
    snap2 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap2["session_id"] != snap1["session_id"]

    # The new task carries the cursor in its context (delivery path on restart).
    fresh_task2 = await db_session.get(Task, task2.id)
    assert fresh_task2 is not None
    assert (fresh_task2.context or {}).get("message_cursor") == cursor


@pytest.mark.asyncio
async def test_start_carries_decisions_reg_forward_across_restart(db_session):
    """A decisions register persisted by the session-1 heartbeat is carried into
    the NEW task's ``context["_decisions_reg"]`` when a second session starts —
    the same round-trip the exam ledger, message cursor, and session summary
    already use."""
    project = await _make_project_with_member(db_session)
    task1 = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap1 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    reg = '{"entries": [{"tick": 1, "surfaceKey": "g-1", "verdict": "accept"}]}'
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap1["session_id"],
        task_id=snap1["task_id"],
        progress=False,
        decisions_reg=reg,
    )

    # Drive session-1 task to terminal and queue a fresh task for adoption.
    task1 = await db_session.get(Task, task1.id)
    task1.status = TaskStatus.COMPLETED
    db_session.add(task1)
    task2 = await _make_strategic_review_task(db_session, project)
    await db_session.commit()

    # Start session 2 → adopts task2; decisions register should be carried forward.
    project = await db_session.get(Project, project.id)
    snap2 = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap2["session_id"] != snap1["session_id"]

    # The new task carries the decisions register in its context.
    fresh_task2 = await db_session.get(Task, task2.id)
    assert fresh_task2 is not None
    assert (fresh_task2.context or {}).get("_decisions_reg") == reg


@pytest.mark.asyncio
async def test_heartbeat_marks_handled_message_ids(db_session):
    """A heartbeat carrying ``handled_message_ids`` updates those messages'
    ``status`` from ``'open'`` to ``'handled'``, scoped to the project and session."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    session_id = snap["session_id"]

    # Insert two messages scoped to THIS session.
    msg1 = await _make_operator_message(
        db_session, project, content="hello", resident_session_id=session_id
    )
    msg2 = await _make_operator_message(
        db_session, project, content="world", resident_session_id=session_id
    )

    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project,
        session_id=session_id,
        task_id=snap["task_id"],
        progress=True,
        handled_message_ids=[str(msg1.id), str(msg2.id)],
    )
    assert result["stop_requested"] is False

    # Both messages are now marked handled.
    fresh_msg1 = await db_session.get(ProjectMessage, msg1.id)
    fresh_msg2 = await db_session.get(ProjectMessage, msg2.id)
    assert fresh_msg1 is not None and fresh_msg1.status == "handled"
    assert fresh_msg2 is not None and fresh_msg2.status == "handled"


@pytest.mark.asyncio
async def test_handled_messages_excluded_from_work_state_new_operator_messages_count(db_session):
    """Messages marked ``handled`` are excluded from
    ``work_state_summary.new_operator_messages`` so the strategist's inbox count
    reflects only genuinely-unread messages, not ones it already answered."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    session_id = snap["session_id"]

    # Insert one open and one to-be-handled message.
    await _make_operator_message(
        db_session, project, content="open", resident_session_id=session_id
    )
    handled_msg = await _make_operator_message(
        db_session, project, content="answered", resident_session_id=session_id
    )

    # Mark the second message handled via a progress heartbeat.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=session_id,
        task_id=snap["task_id"],
        progress=True,
        handled_message_ids=[str(handled_msg.id)],
    )

    # A subsequent heartbeat must count only the open message.
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project,
        session_id=session_id,
        task_id=snap["task_id"],
        progress=False,
    )
    summary = result.get("work_state_summary", {})
    assert summary.get("new_operator_messages") == 1, (
        f"Expected 1 open (the handled one must be excluded), got {summary}"
    )
    # Verify the handled message really has status=handled.
    fresh_handled = await db_session.get(ProjectMessage, handled_msg.id)
    assert fresh_handled is not None and fresh_handled.status == "handled"


@pytest.mark.asyncio
async def test_heartbeat_handled_ids_only_affect_own_session(db_session):
    """Handled-id updates are scoped to the session: a message from a DIFFERENT
    session is NOT affected even if its id appears in ``handled_message_ids``."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    session_id = snap["session_id"]

    # Message without a resident_session_id (legacy/cadence row — no session scope).
    unscoped_msg = await _make_operator_message(db_session, project, content="no-session")

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=session_id,
        task_id=snap["task_id"],
        progress=True,
        handled_message_ids=[str(unscoped_msg.id)],
    )

    # The unscoped message is NOT marked handled (different session_id / None).
    fresh_msg = await db_session.get(ProjectMessage, unscoped_msg.id)
    assert fresh_msg is not None and fresh_msg.status == "open"


# ---------------------------------------------------------------------------
# turn_latency_summary — heartbeat persists rolling latency telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_persists_turn_latency_summary(db_session):
    """A heartbeat carrying ``turn_latency_summary`` stores it on the session blob."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    latency = {
        "operator": {
            "count": 2, "mean_turn_ms": 1500, "max_turn_ms": 2000,
            "mean_ttft_ms": 300, "interrupted_count": 0, "total_tool_calls": 4,
        },
        "autonomous": {
            "count": 3, "mean_turn_ms": 5000, "max_turn_ms": 8000,
            "mean_ttft_ms": None, "interrupted_count": 1, "total_tool_calls": 6,
        },
    }

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=True,
        turn_latency_summary=latency,
    )

    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob is not None
    stored = blob.get("_turn_latency_summary")
    assert isinstance(stored, dict), f"Expected dict, got {stored!r}"
    assert stored["operator"]["count"] == 2
    assert stored["operator"]["mean_turn_ms"] == 1500
    assert stored["autonomous"]["count"] == 3
    assert stored["autonomous"]["interrupted_count"] == 1


@pytest.mark.asyncio
async def test_heartbeat_without_turn_latency_summary_is_liveness_ok(db_session):
    """A heartbeat without ``turn_latency_summary`` (liveness-only) succeeds and
    leaves any pre-existing ``_turn_latency_summary`` on the blob untouched."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # First heartbeat: persist a summary.
    latency = {
        "operator": {
            "count": 1, "mean_turn_ms": 1000, "max_turn_ms": 1000,
            "mean_ttft_ms": 200, "interrupted_count": 0, "total_tool_calls": 2,
        },
        "autonomous": {
            "count": 0, "mean_turn_ms": None, "max_turn_ms": 0,
            "mean_ttft_ms": None, "interrupted_count": 0, "total_tool_calls": 0,
        },
    }
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=True,
        turn_latency_summary=latency,
    )

    # Second heartbeat: no summary (liveness only).
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=False,
    )
    assert result["stop_requested"] is False

    # The blob still has the first summary (liveness heartbeat did not erase it).
    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob is not None
    stored = blob.get("_turn_latency_summary")
    assert isinstance(stored, dict)
    assert stored["operator"]["count"] == 1


@pytest.mark.asyncio
async def test_heartbeat_persists_operator_decision_summary(db_session):
    """A heartbeat carrying ``operator_decision_summary`` stores it on the session blob."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    op_summary = {
        "total": 4,
        "captured": 3,
        "missed": 1,
        "capture_rate": 75,
    }

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=True,
        operator_decision_summary=op_summary,
    )

    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob is not None
    stored = blob.get("_operator_decision_summary")
    assert isinstance(stored, dict), f"Expected dict, got {stored!r}"
    assert stored["total"] == 4
    assert stored["captured"] == 3
    assert stored["missed"] == 1
    assert stored["capture_rate"] == 75


@pytest.mark.asyncio
async def test_heartbeat_without_operator_decision_summary_leaves_blob_unchanged(db_session):
    """A heartbeat without ``operator_decision_summary`` succeeds and leaves any
    pre-existing ``_operator_decision_summary`` on the blob untouched."""
    project = await _make_project_with_member(db_session)
    await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # First heartbeat: persist a summary.
    op_summary = {"total": 2, "captured": 1, "missed": 1, "capture_rate": 50}
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=True,
        operator_decision_summary=op_summary,
    )

    # Second heartbeat: no summary (liveness only).
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project,
        session_id=snap["session_id"],
        task_id=snap["task_id"],
        progress=False,
    )
    assert result["stop_requested"] is False

    # The blob still has the first summary (liveness heartbeat did not erase it).
    fresh = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh.vision)
    assert blob is not None
    stored = blob.get("_operator_decision_summary")
    assert isinstance(stored, dict)
    assert stored["total"] == 2


# ---------------------------------------------------------------------------
# Restart continuity — memory notes and session summary survive into the
# NEXT resident session's bootstrap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_restart_loads_memory_notes(db_session):
    """Memory notes written via set_memory appear in the bootstrap payload of the
    NEXT spawned resident task (the restart-continuity claim)."""
    project = await _make_project_with_member(db_session)
    # Bypass the bootstrap-pending guard.
    project.is_self_repo = True
    # Seed strategic memory notes on the project (set_memory op writes these).
    project.strategic_memory_notes = [
        {
            "note": "Always verify claims before escalating them.",
            "set_at": "2026-01-01T00:00:00",
        },
        {
            "note": "Prefer concrete action over abstract discussion.",
            "set_at": "2026-01-02T00:00:00",
        },
    ]
    flag_modified(project, "strategic_memory_notes")
    flag_modified(project, "is_self_repo")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _make_live_local_runner(db_session, project)

    # No adoptable strategic_review task exists -> spawn path.
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snapshot["status"] == "active"
    assert snapshot["task_id"]

    # The spawned task's bootstrap payload must include strategic_memory_notes.
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic = [
        t for t in tasks if (t.first_payload or {}).get("type") == "strategic_review"
    ]
    assert len(strategic) == 1
    payload = strategic[0].first_payload or {}
    notes = payload.get("strategic_memory_notes")
    assert notes is not None, (
        f"strategic_memory_notes missing from payload keys: {list(payload.keys())}"
    )
    assert len(notes) == 2
    assert notes[0]["note"] == "Always verify claims before escalating them."
    assert notes[1]["note"] == "Prefer concrete action over abstract discussion."


@pytest.mark.asyncio
async def test_session_restart_loads_session_summary(db_session):
    """A session summary written during session A is carried forward into a freshly
    spawned session B's task context (restart-continuity claim)."""
    project = await _make_project_with_member(db_session)
    project.is_self_repo = True
    flag_modified(project, "is_self_repo")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _make_live_local_runner(db_session, project)

    # Session A: start and write a session summary to the blob.
    snap_a = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap_a["status"] == "active"

    # Write a session summary to the blob (what set_session_summary does).
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["_session_summary"] = (
        "Research revealed three key gap areas to investigate."
    )
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # Stop session A.
    project = await db_session.get(Project, project.id)
    ended = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert ended["status"] == "ended"

    # Session B: start fresh (spawn path since session A's task was cancelled).
    project = await db_session.get(Project, project.id)
    snap_b = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert snap_b["status"] == "active"
    assert snap_b["task_id"] != snap_a["task_id"]

    # Session B's task context must carry the session summary forward.
    from uuid import UUID
    task_b = await db_session.get(Task, UUID(snap_b["task_id"]))
    assert task_b is not None
    ctx = task_b.context or {}
    assert ctx.get("_session_summary") == (
        "Research revealed three key gap areas to investigate."
    )


# ---------------------------------------------------------------------------
# Compaction hints: gap history threshold-based signal for the strategist
# ---------------------------------------------------------------------------


def test_compaction_hints_surfaced_when_gap_history_exceeds_threshold():
    """_build_compaction_hints returns a list of hints when any active gap's
    combined history + past_moves entries cross the threshold, and None when
    no gap qualifies."""
    from app.strategic_spawning import (
        _COMPACTION_HISTORY_ENTRY_THRESHOLD,
        _build_compaction_hints,
    )

    # Below threshold — no hints expected.
    thin_history = [{"ts": "2026-01-01T00:00:00Z"} for _ in range(_COMPACTION_HISTORY_ENTRY_THRESHOLD - 2)]
    below = _build_compaction_hints(
        {"gaps": [{"id": "g-ok", "name": "Safe gap", "status": "active", "history": thin_history}]}
    )
    assert below is None, f"Expected None below threshold, got {below}"

    # At threshold — should produce a hint.
    at_threshold = [{"ts": "2026-01-01T00:00:00Z"} for _ in range(_COMPACTION_HISTORY_ENTRY_THRESHOLD)]
    at = _build_compaction_hints(
        {"gaps": [{"id": "g-heavy", "name": "Heavy gap", "status": "active", "history": at_threshold, "past_moves": []}]}
    )
    assert at is not None
    assert at[0]["gap_id"] == "g-heavy"
    assert at[0]["history_entry_count"] == _COMPACTION_HISTORY_ENTRY_THRESHOLD
    assert at[0]["past_moves_count"] == 0
    assert at[0]["total_entries"] == _COMPACTION_HISTORY_ENTRY_THRESHOLD

    # Past_moves also count toward the total.
    with_history = [{"ts": "2026-01-01T00:00:00Z"} for _ in range(5)]
    with_moves = [{"move": "reviewed"} for _ in range(_COMPACTION_HISTORY_ENTRY_THRESHOLD - 5 + 1)]
    combined = _build_compaction_hints(
        {"gaps": [{"id": "g-combined", "name": "Combined gap", "status": "active", "history": with_history, "past_moves": with_moves}]}
    )
    assert combined is not None
    assert combined[0]["gap_id"] == "g-combined"
    assert combined[0]["total_entries"] == len(with_history) + len(with_moves)

    # Non-active gaps are ignored.
    non_active = _build_compaction_hints(
        {"gaps": [{"id": "g-closed", "name": "Closed gap", "status": "closed", "history": at_threshold}]}
    )
    assert non_active is None

    # None / empty input.
    assert _build_compaction_hints(None) is None
    assert _build_compaction_hints({}) is None


# ---------------------------------------------------------------------------
# Decisions-register persistence: heartbeat carries decisions_reg into the
# project.vision blob and mirrors to task.context (same contract as exam
# ledger — this is a separate write path that also needs explicit coverage).
# ---------------------------------------------------------------------------

_SAMPLE_DECISIONS_REG = (
    '{"version": 1, "decisions": ['
    '{"id": "dec-1", "tick": 1, "summary": "Use agent-foo for X"},'
    '{"id": "dec-2", "tick": 2, "summary": "Skip Y until Z"}'
    "]}"
)


@pytest.mark.asyncio
async def test_heartbeat_decisions_reg_persisted_to_blob_and_task_context(db_session):
    """The heartbeat ``decisions_reg`` field persists ``_decisions_reg`` into
    the project.vision blob AND mirrors it to the task's ``task.context`` so a
    full restart (VM recycle, process crash) can restore settled cross-tick
    decisions instead of falling back to empty."""
    import json as _json

    project = await _make_project_with_member(db_session)
    task = await _make_strategic_review_task(db_session, project)
    await _make_live_local_runner(db_session, project)
    snap = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Establish a baseline lease so we can send a liveness heartbeat.
    project = await db_session.get(Project, project.id)
    r0 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=True,
    )
    baseline_lease = r0["leased_until"]
    assert baseline_lease is not None

    # Liveness heartbeat carrying the decisions register.
    project = await db_session.get(Project, project.id)
    result = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
        decisions_reg=_SAMPLE_DECISIONS_REG,
    )

    # --- lease-immutability (liveness does NOT extend) ---------------------------
    assert result["authority_held"] is True
    assert result["leased_until"] == baseline_lease

    # --- blob persistence --------------------------------------------------------
    fresh_project = await db_session.get(Project, project.id)
    blob = read_resident_session(fresh_project.vision)
    assert blob is not None
    assert "_decisions_reg" in blob
    stored = _json.loads(blob["_decisions_reg"])
    assert stored["version"] == 1
    assert len(stored["decisions"]) == 2
    assert stored["decisions"][0]["id"] == "dec-1"
    assert stored["decisions"][1]["summary"] == "Skip Y until Z"

    # --- task.context mirror -----------------------------------------------------
    fresh_task = await db_session.get(Task, task.id)
    assert fresh_task is not None
    assert "_decisions_reg" in (fresh_task.context or {})
    ctx_stored = _json.loads(fresh_task.context["_decisions_reg"])
    assert ctx_stored["version"] == 1
    assert len(ctx_stored["decisions"]) == 2
    assert ctx_stored["decisions"][0]["id"] == "dec-1"
    assert ctx_stored["decisions"][1]["tick"] == 2


# ---------------------------------------------------------------------------
# Direction-grounding in heartbeat response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_returns_direction_grounding(db_session):
    """The heartbeat response includes ``direction_grounding`` populated from
    Direction rows with non-retired statuses, structured as a list of
    ``{id, text, status}`` dicts. Test verifies both presence and correct
    structure when active Direction rows exist, and an empty list when none
    do."""
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
    assert r0["direction_grounding"] == []

    # Add active directions with text and status.
    included_statuses = [
        ("d-active1", "active", "Build observability dashboards"),
        ("d-paused1", "paused", "Pause refactoring until data is ready"),
        ("d-proposed1", "proposed", "Propose new caching layer"),
    ]
    excluded_statuses = [
        ("d-retired1", "retired", "No longer needed"),
        ("d-completed1", "completed", "Done in v23"),
    ]
    for key, status, text in included_statuses + excluded_statuses:
        db_session.add(
            DirectionModel(
                project_id=project.id,
                direction_key=key,
                status=status,
                text=text,
                data={},
            )
        )
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    r1 = await renew_resident_session_lease(
        db_session, project, session_id=snap["session_id"],
        task_id=snap["task_id"], progress=False,
    )
    grounding = r1["direction_grounding"]
    assert isinstance(grounding, list)

    # All three included statuses must appear with correct structure.
    for key, status, text in included_statuses:
        match = [d for d in grounding if d["id"] == key]
        assert len(match) == 1, f"{key} should appear once in direction_grounding"
        assert match[0]["status"] == status
        assert match[0]["text"] == text

    # All excluded statuses must be absent.
    for key, _, _ in excluded_statuses:
        match = [d for d in grounding if d["id"] == key]
        assert len(match) == 0, f"{key} should be excluded from direction_grounding"

    # Each entry has exactly the three expected keys.
    for entry in grounding:
        assert set(entry.keys()) == {"id", "text", "status"}
