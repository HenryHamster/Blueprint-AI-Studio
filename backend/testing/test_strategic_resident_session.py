"""Focused regression coverage for the restored Workday resident lifecycle.

Adapted from the legacy hardening suite in commit 06a97b8da. The restored
subsystem intentionally has no global feature gate or separate resident table,
so these tests cover the surviving Project.vision lifecycle, lease, message
handoff, and reaper contracts directly.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from inspect import unwrap
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from sqlalchemy.orm.attributes import flag_modified

import app.strategic_resident_session as resident_session_module
from app.database import resolve_async_engine
from app.models import (
    LocalRunner,
    MessageHandlingState,
    MessageType,
    Project,
    ProjectMember,
    StrategicCycle,
    ProjectMessage,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.admin_project_strategic_session import (
    StrategicResidentSessionStart,
    start_project_strategic_call,
    start_project_strategic_resident_session,
)
from app.reaper_orphan_classify import _classify_resident_session
from app.session_message_handoff import (
    handoff_session_messages,
    repair_stale_session_message_handoffs,
)
from app.shared_state import runtime_config
from app.strategic_resident_session import (
    OPERATOR_DECISION_ABSENT_NOTIFIED_KEY,
    OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
    RESIDENT_SESSION_KEY,
    STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
    _OPERATOR_DECISION_SUMMARY_STALE_SECONDS,
    acquire_resident_call_lease,
    acquire_resident_typed_fast_lease,
    claim_resident_worker_incarnation,
    legacy_resident_worker_incarnation,
    new_session_id,
    read_resident_session,
    resident_call_is_active,
    resident_call_snapshot,
    resident_typed_fast_snapshot,
    release_resident_typed_fast_lease,
    renew_resident_call_lease,
    renew_resident_typed_fast_lease,
    renew_resident_session_lease,
    resident_session_is_active,
    resident_session_snapshot,
    start_resident_session,
    sync_resident_worker_retry_wait,
    stop_resident_session,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    persist_strategic_workbench_update,
)


TEST_USER_ID = "resident-lifecycle-user"


def test_new_process_waits_for_bounded_legacy_incarnation_during_rollout():
    task_id = uuid.uuid4()
    now = datetime(2026, 7, 28, 12, 0, 0)
    vision = {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "session_id": "rolling-session",
            "task_id": str(task_id),
            "worker_generation": 1,
        }
    }
    new_incarnation = uuid.uuid4()

    assert (
        claim_resident_worker_incarnation(
            vision,
            task_id=task_id,
            worker_generation=1,
            incarnation_id=new_incarnation,
            now=now,
        )
        is None
    )
    resident = read_resident_session(vision)
    assert resident["worker_incarnation_id"] == str(
        legacy_resident_worker_incarnation(task_id, 1)
    )
    legacy_expiry = datetime.fromisoformat(
        resident["worker_incarnation_expires_at"]
    )
    assert legacy_expiry > now

    assert claim_resident_worker_incarnation(
        vision,
        task_id=task_id,
        worker_generation=1,
        incarnation_id=None,
        now=now + timedelta(seconds=30),
    ) == legacy_resident_worker_incarnation(task_id, 1)
    assert (
        claim_resident_worker_incarnation(
            vision,
            task_id=task_id,
            worker_generation=1,
            incarnation_id=new_incarnation,
            now=now + timedelta(seconds=31),
        )
        is None
    )
    assert claim_resident_worker_incarnation(
        vision,
        task_id=task_id,
        worker_generation=1,
        incarnation_id=new_incarnation,
        now=now + timedelta(seconds=211),
    ) == new_incarnation


async def _make_project(db_session) -> Project:
    project = Project(name="Resident lifecycle", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await db_session.commit()
    return project


async def _make_task(db_session, project: Project) -> Task:
    task = Task(
        project_id=project.id,
        title="Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        vm_instance_id="agent-vm-resident",
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_runner(db_session, project: Project) -> LocalRunner:
    runner = LocalRunner(
        project_id=project.id,
        token_hash=uuid.uuid4().hex * 2,
        status="active",
        capabilities={"task_types": "all", "max_concurrent": 2},
        last_seen_at=_utcnow(),
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)
    return runner


async def _link_phase2_cycle(db_session, project: Project, task: Task) -> StrategicCycle:
    cycle = StrategicCycle(
        id=f"sc-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow(),
        phase1_completed_at=_utcnow(),
        phase2_started_at=_utcnow(),
        phase2_task_id=task.id,
    )
    db_session.add(cycle)
    await db_session.commit()
    return cycle


def _assert_resident_terminal_receipt(emit, cycle: StrategicCycle, source: str):
    emit.assert_awaited_once()
    event = emit.await_args.args[0][0]
    assert event["cycle_id"] == cycle.id
    assert event["phase"] == 2
    assert event["phase_state"] == "failed_phase2"
    assert event["completion"]["status"] == "CANCELLED"
    assert f"source={source}" in event["completion"]["failure_reason"]
    return event


@pytest.mark.asyncio
async def test_call_speech_credentials_require_exact_active_resident_lease(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    owner = await _make_task(db_session, project)
    other = await _make_task(db_session, project)
    owner.context = {"_resident_session": True}
    other.context = {"_resident_session": True}
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "task_id": str(owner.id),
            "worker_generation": 1,
            "worker_incarnation_id":
                "00000000-0000-0000-0000-000000000733",
            "worker_incarnation_expires_at": (
                _utcnow() + timedelta(minutes=5)
            ).isoformat(),
            "call": {
                "call_id": "call-speech-1",
                "generation": 3,
                "status": "active",
                "worker_task_id": str(owner.id),
                "worker_generation": 1,
                "expires_at": (_utcnow() + timedelta(minutes=5)).isoformat(),
            },
        }
    }
    flag_modified(project, "vision")
    db_session.add_all([project, owner, other])
    await db_session.commit()

    secret = "never-log-or-dispatch-this"
    resolver = AsyncMock(return_value={"CALL_DEEPGRAM_API_KEY": secret})
    monkeypatch.setattr(
        resident_session_module,
        "resolve_resident_call_speech_credentials",
        resolver,
    )
    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    monkeypatch.setenv(
        "CALL_DEEPGRAM_API_KEY_SECRET_VERSION",
        "projects/test/secrets/deepgram/versions/7",
    )
    monkeypatch.setenv(
        "CALL_CARTESIA_API_KEY_SECRET_VERSION",
        "projects/test/secrets/cartesia/versions/3",
    )
    monkeypatch.setenv("CALL_DEEPGRAM_STT_MODEL", "flux-general-en")
    monkeypatch.setenv("CALL_CARTESIA_TTS_MODEL", "sonic-3")
    monkeypatch.setenv("CALL_CARTESIA_VOICE_ID", "voice-id")
    payload = resident_session_module.ResidentCallSpeechCredentialsRequest(
        task_id=str(owner.id),
        resident_incarnation_id=uuid.UUID(
            "00000000-0000-0000-0000-000000000733"
        ),
        call_id="call-speech-1",
        call_generation=3,
    )
    response = await resident_session_module.resident_call_speech_credentials(
        request=None,
        project_id=project.id,
        payload=payload,
        session=db_session,
        token={"sub": str(owner.id)},
    )
    assert response == {
        "speech": {"CALL_DEEPGRAM_API_KEY": secret},
        "foreground_qualification": "claude-warm-stream-json:phase0-v1",
    }
    resolver.assert_awaited_once_with(owner)

    with pytest.raises(HTTPException) as wrong_incarnation:
        await resident_session_module.resident_call_speech_credentials(
            request=None,
            project_id=project.id,
            payload=resident_session_module.ResidentCallSpeechCredentialsRequest(
                task_id=str(owner.id),
                resident_incarnation_id=uuid.UUID(
                    "00000000-0000-0000-0000-000000000744"
                ),
                call_id="call-speech-1",
                call_generation=3,
            ),
            session=db_session,
            token={"sub": str(owner.id)},
        )
    assert wrong_incarnation.value.status_code == 409
    resolver.assert_awaited_once()

    # A valid agent token for another task in the same project still receives
    # neither the response nor a Secret Manager lookup.
    with pytest.raises(HTTPException) as wrong_worker:
        await resident_session_module.resident_call_speech_credentials(
            request=None,
            project_id=project.id,
            payload=resident_session_module.ResidentCallSpeechCredentialsRequest(
                task_id=str(other.id), call_id="call-speech-1", call_generation=3
            ),
            session=db_session,
            token={"sub": str(other.id)},
        )
    assert wrong_worker.value.status_code == 409
    assert secret not in str(wrong_worker.value.detail)
    resolver.assert_awaited_once()

    with pytest.raises(HTTPException) as stale_generation:
        await resident_session_module.resident_call_speech_credentials(
            request=None,
            project_id=project.id,
            payload=resident_session_module.ResidentCallSpeechCredentialsRequest(
                task_id=str(owner.id), call_id="call-speech-1", call_generation=2
            ),
            session=db_session,
            token={"sub": str(owner.id)},
        )
    assert stale_generation.value.status_code == 409
    resolver.assert_awaited_once()

    # A token cannot name the owning task in the body while authenticating as
    # another project task.
    with pytest.raises(HTTPException) as token_mismatch:
        await resident_session_module.resident_call_speech_credentials(
            request=None,
            project_id=project.id,
            payload=payload,
            session=db_session,
            token={"sub": str(other.id)},
        )
    assert token_mismatch.value.status_code == 403
    resolver.assert_awaited_once()

    project.vision[RESIDENT_SESSION_KEY]["call"]["expires_at"] = (
        _utcnow() - timedelta(seconds=1)
    ).isoformat()
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    with pytest.raises(HTTPException) as inactive_call:
        await resident_session_module.resident_call_speech_credentials(
            request=None,
            project_id=project.id,
            payload=payload,
            session=db_session,
            token={"sub": str(owner.id)},
        )
    assert inactive_call.value.status_code == 409
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_lease_expires_when_coordinator_never_becomes_ready(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    now = _utcnow()
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "stop_requested": False,
            "call": {
                "call_id": "call-not-ready",
                "generation": 1,
                "status": "active",
                "worker_task_id": str(task.id),
                "account_id": "account-1",
                "slot_reserved": True,
                "started_at": (now - timedelta(seconds=31)).isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            },
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    released = AsyncMock()
    monkeypatch.setattr("app.max_plan_pool.release_max_plan_call_slot", released)

    snapshot = await renew_resident_call_lease(
        db_session,
        project,
        task_id=str(task.id),
        call_id="call-not-ready",
        generation=1,
        ready=False,
    )

    assert snapshot["status"] == "expired"
    assert snapshot["failure"] == "coordinator_not_ready"
    released.assert_awaited_once_with("account-1", project_id=str(project.id))


@pytest.mark.asyncio
async def test_ready_call_renewal_is_capped_at_absolute_duration(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    now = _utcnow()
    started_at = now - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "stop_requested": False,
            "call": {
                "call_id": "call-ready",
                "generation": 2,
                "status": "active",
                "worker_task_id": str(task.id),
                "started_at": started_at.isoformat(),
                "ready_at": (started_at + timedelta(seconds=5)).isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            },
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    snapshot = await renew_resident_call_lease(
        db_session,
        project,
        task_id=str(task.id),
        call_id="call-ready",
        generation=2,
        ttl_seconds=900,
        ready=True,
        usage={
            "input_audio_ms": 12_000,
            "output_characters": 850,
            "provider_errors": -5,
            "credential": "must-not-persist",
        },
    )

    assert snapshot["status"] == "active"
    assert datetime.fromisoformat(snapshot["expires_at"]) <= started_at + timedelta(
        seconds=resident_session_module._RESIDENT_CALL_MAX_DURATION_SECONDS
    )
    assert snapshot["usage"] == {
        "input_audio_ms": 12_000,
        "output_characters": 850,
        "provider_errors": 0,
    }


def _test_session_factory(session):
    return async_sessionmaker(
        resolve_async_engine(session),
        class_=AsyncSession,
        expire_on_commit=False,
    )


def test_session_state_helpers_remain_table_free():
    assert new_session_id().startswith("ss-")
    assert read_resident_session(None) is None
    assert resident_session_snapshot({}) is None
    assert resident_session_is_active({"status": "active"}) is True
    assert (
        resident_session_is_active({"status": "active", "stop_requested": True})
        is False
    )
    snapshot = resident_session_snapshot(
        {
            RESIDENT_SESSION_KEY: {
                "session_id": "ss-restored",
                "task_id": "task-restored",
                "status": "active",
                "stop_requested": False,
            }
        }
    )
    assert snapshot is not None
    assert snapshot["session_id"] == "ss-restored"


def test_nested_call_projection_hides_actor_account_and_playout_receipts():
    now = datetime.fromisoformat("2026-07-27T12:00:00")
    vision = {
        RESIDENT_SESSION_KEY: {
            "status": "active",
            "call": {
                "call_id": "call-opaque",
                "generation": 4,
                "status": "active",
                "actor_user_id": "operator-secret",
                "account_id": "pool-secret",
                "expires_at": "2026-07-27T12:01:00",
                "playout_receipts": [{"phrase_id": "p", "event": "started"}],
                "worker_task_id": "worker",
            },
        }
    }
    assert resident_call_is_active(vision[RESIDENT_SESSION_KEY]["call"], now=now)
    snapshot = resident_call_snapshot(vision, now=now)
    assert snapshot is not None
    assert snapshot["call_id"] == "call-opaque"
    assert "actor_user_id" not in snapshot
    assert "account_id" not in snapshot
    assert "playout_receipts" not in snapshot


@pytest.mark.asyncio
async def test_call_lease_reuse_rechecks_owner_under_project_lock(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-call-owner-race",
            "status": "active",
            "stop_requested": False,
            "call": {
                "call_id": "call-owner-race",
                "generation": 2,
                "status": "active",
                "actor_user_id": TEST_USER_ID,
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            },
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await acquire_resident_call_lease(
            db_session,
            project,
            operator_user_id="different-project-member",
            account_id="pool-account",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Only the call owner may reconnect"
    await db_session.refresh(project)
    call = read_resident_session(project.vision)["call"]
    assert call["actor_user_id"] == TEST_USER_ID
    assert call["call_id"] == "call-owner-race"


@pytest.mark.asyncio
async def test_typed_fast_lease_is_fenced_renewed_and_released(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    incarnation = uuid.uuid4()
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-typed-fast",
            "status": "active",
            "stop_requested": False,
            "task_id": str(task.id),
            "worker_generation": 3,
            "worker_incarnation_id": str(incarnation),
            "worker_incarnation_expires_at": (
                _utcnow() + timedelta(minutes=2)
            ).isoformat(),
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    lease, stale_account, created = await acquire_resident_typed_fast_lease(
        db_session,
        project,
        task_id=task.id,
        worker_generation=3,
        incarnation_id=incarnation,
        account_id="pool-account",
    )
    assert created is True
    assert stale_account is None
    snapshot = resident_typed_fast_snapshot(project.vision)
    assert snapshot is not None
    assert snapshot["status"] == "active"
    assert snapshot["kind"] == "typed_fast"
    assert "account_id" not in snapshot

    with pytest.raises(HTTPException) as fenced:
        await renew_resident_typed_fast_lease(
            db_session,
            project,
            task_id=str(task.id),
            worker_generation=3,
            incarnation_id=incarnation,
            lease_id=lease["lease_id"],
            generation=lease["generation"] + 1,
        )
    assert fenced.value.status_code == 409

    renewed = await renew_resident_typed_fast_lease(
        db_session,
        project,
        task_id=str(task.id),
        worker_generation=3,
        incarnation_id=incarnation,
        lease_id=lease["lease_id"],
        generation=lease["generation"],
    )
    assert renewed["status"] == "active"
    _released, account_id, changed = await release_resident_typed_fast_lease(
        db_session,
        project,
        lease_id=lease["lease_id"],
        generation=lease["generation"],
        task_id=task.id,
        worker_generation=3,
        incarnation_id=incarnation,
        reason="parent_shutdown",
    )
    assert changed is True
    assert account_id == "pool-account"
    assert resident_typed_fast_snapshot(project.vision)["status"] == "ended"


@pytest.mark.asyncio
async def test_typed_fast_capacity_denial_persists_no_lease_for_workday_fallback(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    incarnation = uuid.uuid4()
    task.context = {"_resident_session": True}
    task.llm_account_id = "pool-at-cap"
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-typed-fast-denied",
            "status": "active",
            "stop_requested": False,
            "task_id": str(task.id),
            "worker_generation": 1,
            "worker_incarnation_id": str(incarnation),
            "worker_incarnation_expires_at": (
                _utcnow() + timedelta(minutes=2)
            ).isoformat(),
        }
    }
    flag_modified(task, "context")
    flag_modified(project, "vision")
    db_session.add_all([task, project])
    await db_session.commit()
    monkeypatch.setattr(
        resident_session_module,
        "_resolve_agent_project",
        AsyncMock(return_value=project),
    )
    reserve = AsyncMock(return_value=False)
    monkeypatch.setattr("app.max_plan_pool.reserve_max_plan_auxiliary_slot", reserve)

    result = await resident_session_module.acquire_agent_typed_fast_lease(
        request=AsyncMock(),
        project_id=project.id,
        payload=resident_session_module.ResidentTypedFastLeaseRequest(
            task_id=str(task.id),
            worker_generation=1,
            resident_incarnation_id=incarnation,
        ),
        session=db_session,
        token={"sub": str(task.id)},
    )

    assert result == {"acquired": False, "reason": "no_concurrent_capacity"}
    reserve.assert_awaited_once_with("pool-at-cap", project_id=str(project.id))
    assert resident_typed_fast_snapshot(project.vision) is None


def test_session_snapshot_exposes_bounded_activity_and_live_idle_remaining(
    monkeypatch,
):
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 7200)
    now = datetime.fromisoformat("2026-07-25T12:00:00")
    snapshot = resident_session_snapshot(
        {
            RESIDENT_SESSION_KEY: {
                "session_id": "ss-activity",
                "status": "active",
                "stop_requested": False,
                "started_at": "2026-07-25T10:00:00",
                "updated_at": "2026-07-25T11:59:30",
                "last_operator_interaction_at": "2026-07-25T11:00:00",
                "activity": {
                    "phase": "working",
                    "phase_started_at": "2026-07-25T11:59:00",
                    "last_action": "x" * 500,
                    "pending_operator_messages": 3,
                    "next_review_at": "2026-07-25T12:05:00",
                    "pending_watches": 2,
                    "fired_watches": 1,
                },
            }
        },
        now=now,
    )

    assert snapshot is not None
    assert snapshot["activity"] == {
        "phase": "working",
        "phase_started_at": "2026-07-25T11:59:00",
        "last_action": "x" * 240,
        "pending_operator_messages": 3,
        "next_review_at": "2026-07-25T12:05:00",
        "idle_remaining_seconds": 3600,
        "pending_watches": 2,
        "fired_watches": 1,
    }


def test_session_snapshot_warns_near_idle_end_and_freezes_when_ended(monkeypatch):
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 600)
    vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-idle-warning",
            "status": "active",
            "stop_requested": False,
            "started_at": "2026-07-25T11:50:00",
            "updated_at": "2026-07-25T11:58:00",
            "last_operator_interaction_at": "2026-07-25T11:50:00",
            "activity": {"phase": "waiting", "last_action": "Watching tasks"},
        }
    }
    warning = resident_session_snapshot(
        vision,
        now=datetime.fromisoformat("2026-07-25T11:56:00"),
    )
    assert warning is not None
    assert warning["activity"]["phase"] == "stopping"
    assert warning["activity"]["idle_remaining_seconds"] == 240

    vision[RESIDENT_SESSION_KEY].update(
        {
            "status": "ended",
            "stop_requested": True,
            "ended_at": "2026-07-25T12:00:00",
        }
    )
    ended = resident_session_snapshot(
        vision,
        now=datetime.fromisoformat("2026-07-25T13:00:00"),
    )
    assert ended is not None
    assert ended["activity"]["phase"] == "ended"
    assert ended["activity"]["idle_remaining_seconds"] == 0


@pytest.mark.asyncio
async def test_hydration_ensure_cannot_resurrect_after_committed_mode_leave(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "continuous"},
        arm_interaction_mode=False,
    )
    factory = _test_session_factory(db_session)
    runner_resolved = asyncio.Event()
    resume_ensure = asyncio.Event()

    async def pause_after_runner_resolution(_session, _project_id):
        runner_resolved.set()
        await resume_ensure.wait()
        return None

    monkeypatch.setattr(
        resident_session_module,
        "_find_adoptable_strategic_review_task",
        pause_after_runner_resolution,
    )
    handler = unwrap(start_project_strategic_resident_session)

    async def run_hydration_ensure():
        async with factory() as ensure_session:
            return await handler(
                request=None,
                project_id=project.id,
                payload=StrategicResidentSessionStart(
                    runner_id=str(runner.id),
                    expected_interaction_mode="continuous",
                ),
                session=ensure_session,
                current_user={"user_id": TEST_USER_ID},
            )

    ensure_task = asyncio.create_task(run_hydration_ensure())
    await asyncio.wait_for(runner_resolved.wait(), timeout=5)
    try:
        # This mirrors the durable first half of leaving Workday. It commits
        # prefs + the Project continuation marker before the paused ensure can
        # acquire the Project lock.
        async with factory() as leave_session:
            await persist_strategic_workbench_update(
                leave_session,
                TEST_USER_ID,
                project.id,
                {"interaction_mode": "chat"},
                arm_interaction_mode=False,
            )
    finally:
        resume_ensure.set()
    response = await asyncio.wait_for(ensure_task, timeout=10)

    assert response["ensure_outcome"] == "mode_changed"
    assert response["session"] is None
    async with factory() as verify:
        fresh_project = await verify.get(Project, project.id)
        tasks = (
            (
                await verify.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
    assert read_resident_session(fresh_project.vision) is None
    assert tasks == []


@pytest.mark.asyncio
async def test_continuous_hydration_ensure_succeeds(db_session, monkeypatch):
    monkeypatch.setenv("CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1")
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "continuous"},
        arm_interaction_mode=False,
    )
    project_id = project.id
    runner_id = runner.id
    factory = _test_session_factory(db_session)
    # Hydration is a separate HTTP request/session from the mode save.
    async with factory() as ensure_session:
        response = await unwrap(start_project_strategic_resident_session)(
            request=None,
            project_id=project_id,
            payload=StrategicResidentSessionStart(
                runner_id=str(runner_id),
                expected_interaction_mode="continuous",
            ),
            session=ensure_session,
            current_user={"user_id": TEST_USER_ID},
        )

    assert response["ensure_outcome"] == "ensured"
    assert response["session"]["status"] == "active"
    async with factory() as verify:
        tasks = (
            await verify.execute(select(Task).where(Task.project_id == project_id))
        ).scalars().all()
    assert len(tasks) == 1
    assert str(tasks[0].id) == response["session"]["task_id"]
    assert tasks[0].first_payload["typed_fast_enabled"] is True
    assert tasks[0].first_payload["typed_fast_qualification"] == (
        "claude-warm-stream-json:phase0-v1"
    )


@pytest.mark.asyncio
async def test_call_start_rejects_provider_without_qualified_foreground_adapter(
    db_session,
):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    started = await unwrap(start_project_strategic_resident_session)(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )
    task = await db_session.get(Task, uuid.UUID(started["session"]["task_id"]))
    assert task is not None
    task.llm_account_id = "pool-openai"
    task.context = {**(task.context or {}), "llm_provider": "openai"}
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await unwrap(start_project_strategic_call)(
            request=None,
            project_id=project.id,
            payload=None,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )
    assert exc.value.status_code == 409
    assert "does not yet have a qualified Fast Resident adapter" in str(
        exc.value.detail
    )
    await db_session.refresh(project)
    assert resident_call_snapshot(project.vision) is None


@pytest.mark.asyncio
async def test_mode_changed_ensure_leaves_existing_session_untouched_and_manual_start_works(
    db_session,
):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    handler = unwrap(start_project_strategic_resident_session)

    # An explicit manual Start has no expected-mode fence. It remains an
    # intentional request and enters Workday even from the default Chat mode.
    started = await handler(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )
    assert started["ensure_outcome"] == "ensured"
    assert started["session"]["status"] == "active"
    task_id = uuid.UUID(started["session"]["task_id"])
    task_before = await db_session.get(Task, task_id)
    status_before = task_before.status

    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )
    stale_ensure = await handler(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(
            runner_id=str(runner.id),
            expected_interaction_mode="continuous",
        ),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )

    assert stale_ensure["ensure_outcome"] == "mode_changed"
    assert stale_ensure["session"]["session_id"] == started["session"]["session_id"]
    assert stale_ensure["session"]["task_id"] == started["session"]["task_id"]
    await db_session.refresh(task_before)
    assert task_before.status == status_before
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert [row.id for row in tasks] == [task_id]


async def test_explicit_runner_change_rejects_active_session(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    first_runner = await _make_runner(db_session, project)
    second_runner = await _make_runner(db_session, project)

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(first_runner.id),
    )

    with pytest.raises(HTTPException) as exc:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            runner_id=str(second_runner.id),
        )

    assert exc.value.status_code == 409
    assert "stop the current session" in str(exc.value.detail)
    refreshed = await db_session.get(Project, project.id)
    blob = read_resident_session(dict(refreshed.vision or {}))
    assert blob is not None
    assert blob["session_id"] == started["session_id"]
    assert blob["runner_id"] == str(first_runner.id)


async def test_start_adopts_task_and_liveness_heartbeat_preserves_lease(db_session, monkeypatch):
    monkeypatch.setenv("CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1")
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    runner = await _make_runner(db_session, project)

    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "active"
    assert snapshot["task_id"] == str(task.id)
    assert snapshot["runner_id"] == str(runner.id)
    refreshed_task = await db_session.get(Task, task.id)
    assert refreshed_task.context["_resident_session"] is True
    assert refreshed_task.context["strategic_session_id"] == snapshot["session_id"]
    assert refreshed_task.vm_instance_id == f"runner:{runner.id}"
    assert refreshed_task.first_payload["typed_fast_enabled"] is True
    assert refreshed_task.first_payload["typed_fast_qualification"] == (
        "claude-warm-stream-json:phase0-v1"
    )
    assert refreshed_task.first_payload["typed_fast_models"]["anthropic"]

    project = await db_session.get(Project, project.id)
    progress = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
    )
    lease = progress["leased_until"]
    assert progress["authority_held"] is True
    assert lease is not None

    project = await db_session.get(Project, project.id)
    liveness = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=False,
    )
    assert liveness["authority_held"] is True
    assert liveness["leased_until"] == lease


@pytest.mark.asyncio
async def test_resident_heartbeat_fences_overlapping_process_incarnations(
    db_session,
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    first_incarnation = uuid.UUID(
        "00000000-0000-0000-0000-000000000711"
    )
    second_incarnation = uuid.UUID(
        "00000000-0000-0000-0000-000000000722"
    )
    resident = dict(project.vision[RESIDENT_SESSION_KEY])
    resident["worker_incarnation_id"] = str(first_incarnation)
    resident["worker_incarnation_expires_at"] = (
        _utcnow() + timedelta(minutes=5)
    ).isoformat()
    project.vision = {**project.vision, RESIDENT_SESSION_KEY: resident}
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    first = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        incarnation_id=first_incarnation,
        progress=False,
    )
    assert first["resident_incarnation_held"] is True
    assert first["resident_incarnation_id"] == str(first_incarnation)

    project = await db_session.get(Project, project.id)
    competing = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        incarnation_id=second_incarnation,
        progress=False,
    )
    assert competing["resident_incarnation_held"] is False
    assert competing["authority_held"] is False
    assert competing["stop_requested"] is True

    project = await db_session.get(Project, project.id)
    owner = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        incarnation_id=first_incarnation,
        progress=False,
    )
    assert owner["resident_incarnation_held"] is True


@pytest.mark.asyncio
async def test_predeploy_legacy_grace_is_retryable_for_replacement(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    project = await db_session.get(Project, project.id)
    resident = dict(project.vision[RESIDENT_SESSION_KEY])
    resident.pop("worker_incarnation_protocol", None)
    resident.pop("worker_incarnation_id", None)
    resident.pop("worker_incarnation_expires_at", None)
    project.vision = {**project.vision, RESIDENT_SESSION_KEY: resident}
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    replacement = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        incarnation_id=uuid.UUID(
            "00000000-0000-0000-0000-000000000799"
        ),
        progress=False,
    )

    assert replacement["admission_status"] == "legacy_grace"
    assert replacement["resident_incarnation_held"] is False
    assert replacement["authority_held"] is False
    assert replacement["stop_requested"] is False
    assert replacement["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_incarnation_takeover_uses_time_sampled_after_project_lock(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    first_incarnation = uuid.UUID(
        "00000000-0000-0000-0000-000000000811"
    )
    replacement_incarnation = uuid.UUID(
        "00000000-0000-0000-0000-000000000822"
    )
    before_lock = _utcnow()
    after_lock = before_lock + timedelta(minutes=1)
    resident = dict(project.vision[RESIDENT_SESSION_KEY])
    resident["worker_incarnation_id"] = str(first_incarnation)
    resident["worker_incarnation_expires_at"] = (
        before_lock + timedelta(seconds=30)
    ).isoformat()
    project.vision = {**project.vision, RESIDENT_SESSION_KEY: resident}
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    calls = 0

    def advancing_now():
        nonlocal calls
        calls += 1
        return before_lock if calls == 1 else after_lock

    monkeypatch.setattr(resident_session_module, "_utcnow", advancing_now)
    project = await db_session.get(Project, project.id)
    replacement = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        incarnation_id=replacement_incarnation,
        progress=False,
    )

    assert replacement["resident_incarnation_held"] is True
    assert replacement["resident_incarnation_id"] == str(replacement_incarnation)
    refreshed = await db_session.get(Project, project.id)
    resident = refreshed.vision[RESIDENT_SESSION_KEY]
    assert resident["worker_incarnation_id"] == str(replacement_incarnation)
    assert datetime.fromisoformat(
        resident["worker_incarnation_expires_at"]
    ) > after_lock


@pytest.mark.asyncio
async def test_heartbeat_samples_operator_reengagement_after_project_lock(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    now = _utcnow()
    stale_at = now - timedelta(minutes=10)
    resident = dict(project.vision[RESIDENT_SESSION_KEY])
    resident["started_at"] = stale_at.isoformat()
    resident["last_operator_interaction_at"] = stale_at.isoformat()
    project.vision = {**project.vision, RESIDENT_SESSION_KEY: resident}
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    monkeypatch.setitem(
        runtime_config,
        STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
        60,
    )

    original_project_lock = resident_session_module.project_lock
    project_lock_held = False

    @asynccontextmanager
    async def observed_project_lock(*args, **kwargs):
        nonlocal project_lock_held
        async with original_project_lock(*args, **kwargs) as locked_project:
            project_lock_held = True
            try:
                yield locked_project
            finally:
                project_lock_held = False

    async def latest_operator_interaction(*_args, **_kwargs):
        assert project_lock_held
        return now

    monkeypatch.setattr(
        resident_session_module,
        "project_lock",
        observed_project_lock,
    )
    monkeypatch.setattr(
        resident_session_module,
        "_latest_operator_interaction_at",
        latest_operator_interaction,
    )

    project = await db_session.get(Project, project.id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=False,
    )

    assert renewed["status"] == "active"
    assert renewed["stop_requested"] is False
    refreshed = await db_session.get(Project, project.id)
    resident = read_resident_session(refreshed.vision)
    assert resident["last_operator_interaction_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_new_resident_start_repairs_stale_message_ownership_without_cadence(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    stale = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Release me when the replacement resident starts.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        resident_session_id="ss-ended-before-start",
    )
    db_session.add(stale)
    await db_session.commit()
    rearm = AsyncMock(return_value="resident_active")
    monkeypatch.setattr("app.strategic_trigger.maybe_arm_operator_refresh", rearm)

    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert started["status"] == "active"
    await db_session.refresh(stale)
    assert stale.status == "open"
    assert stale.resident_session_id is None
    assert stale.handling_state == MessageHandlingState.PENDING
    rearm.assert_awaited_once()
    assert rearm.await_args.kwargs["resident_session_active"] is True


@pytest.mark.asyncio
async def test_healthy_existing_resident_ensure_repairs_stale_message_ownership(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    rearm = AsyncMock(return_value="resident_active")
    monkeypatch.setattr("app.strategic_trigger.maybe_arm_operator_refresh", rearm)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    rearm.reset_mock()
    stale = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="A prior handoff failed after the current worker became healthy.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        resident_session_id="ss-ended-after-current-start",
    )
    current = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Keep current ownership isolated.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=started["session_id"],
    )
    db_session.add_all([stale, current])
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    ensured = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert ensured["session_id"] == started["session_id"]
    assert ensured["worker_generation"] == started["worker_generation"]
    await db_session.refresh(stale)
    assert stale.status == "open"
    assert stale.resident_session_id is None
    assert stale.handling_state == MessageHandlingState.PENDING
    await db_session.refresh(current)
    assert current.status == "open"
    assert current.resident_session_id == started["session_id"]
    assert current.handling_state == MessageHandlingState.PENDING
    rearm.assert_awaited_once()
    assert rearm.await_args.kwargs["resident_session_active"] is True


@pytest.mark.asyncio
async def test_retry_wait_is_persisted_reload_idempotent_and_heartbeat_clears(
    db_session,
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    retry_until = _utcnow() + timedelta(minutes=10)

    task = await db_session.get(Task, task.id)
    task.retry_after_until = retry_until
    task.latest_status_message = "Rate limited; waiting for account recovery"
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["updated_at"] = blob["started_at"]
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, task])
    await db_session.commit()

    changed = await sync_resident_worker_retry_wait(
        db_session,
        project_id=project.id,
        task_id=task.id,
        session_id=started["session_id"],
        worker_generation=started["worker_generation"],
    )
    assert changed is True

    db_session.expunge(project)
    project = await db_session.get(Project, project.id)
    waiting = resident_session_snapshot(project.vision)
    assert waiting["worker_state"] == "retry_wait"
    assert waiting["worker_retry_until"] == retry_until.isoformat()
    assert waiting["worker_retry_reason"] == (
        "Rate limited; waiting for account recovery"
    )

    ensured = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    assert ensured["session_id"] == started["session_id"]
    assert ensured["task_id"] == started["task_id"]
    assert ensured["worker_generation"] == started["worker_generation"]
    tasks = (
        await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
    ).scalars().all()
    assert len(tasks) == 1

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        worker_generation=started["worker_generation"],
        progress=False,
    )
    db_session.expunge(project)
    project = await db_session.get(Project, project.id)
    cleared = resident_session_snapshot(project.vision)
    assert cleared["worker_state"] == "ready"
    assert cleared["worker_retry_until"] is None
    assert cleared["worker_retry_reason"] is None


@pytest.mark.asyncio
async def test_reload_ensure_keeps_worker_during_transient_heartbeat_gap(db_session):
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # A resident heartbeat can be delayed by a bounded API retry cycle. This
    # was the production false-positive: the generic task heartbeat was still
    # fresh, but the 90-second resident-only cutoff cancelled the live worker.
    delayed_at = _utcnow() - timedelta(seconds=91)

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.IN_PROGRESS
    old_task.last_heartbeat = _utcnow()
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_heartbeat_at"] = delayed_at.isoformat()
    blob["updated_at"] = delayed_at.isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    reused = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert reused["session_id"] == started["session_id"]
    assert reused["worker_generation"] == started["worker_generation"]
    assert reused["task_id"] == started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_reload_ensure_does_not_supersede_fresh_task_on_stale_projection(
    db_session,
):
    """The exact task heartbeat wins over a lagging resident-session mirror."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    stale_at = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.IN_PROGRESS
    old_task.last_heartbeat = _utcnow() - timedelta(seconds=8)
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_heartbeat_at"] = stale_at.isoformat()
    blob["updated_at"] = stale_at.isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    reused = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert reused["session_id"] == started["session_id"]
    assert reused["worker_generation"] == started["worker_generation"]
    assert reused["task_id"] == started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_status", [TaskStatus.QUEUED, TaskStatus.PROVISIONING])
async def test_reload_ensure_keeps_fresh_retry_heartbeat_past_startup_window(
    db_session, retry_status
):
    """A retrying resident must outlive its original five-minute start claim."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = retry_status
    old_task.last_heartbeat = _utcnow()
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob.pop("worker_heartbeat_at", None)
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    reused = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert reused["session_id"] == started["session_id"]
    assert reused["worker_generation"] == started["worker_generation"]
    assert reused["task_id"] == started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == retry_status


@pytest.mark.asyncio
async def test_reload_ensure_keeps_retry_wait_during_post_deadline_claim_grace(
    db_session,
):
    """An elapsed retry deadline still needs bounded scheduler/runner handoff time."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    retry_until = _utcnow() - timedelta(seconds=15)

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.PROVISIONING
    old_task.retry_after_until = retry_until
    old_task.last_heartbeat = None
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_state"] = "retry_wait"
    blob["worker_retry_until"] = retry_until.isoformat()
    blob["worker_retry_reason"] = "Rate limited; waiting for account recovery"
    blob.pop("worker_heartbeat_at", None)
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    reused = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert reused["session_id"] == started["session_id"]
    assert reused["worker_generation"] == started["worker_generation"]
    assert reused["task_id"] == started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.PROVISIONING


@pytest.mark.asyncio
async def test_reload_ensure_replaces_retry_wait_after_claim_grace_expires(db_session):
    """The elapsed retry bridge remains bounded when no worker claims it."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    retry_until = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.PROVISIONING
    old_task.retry_after_until = retry_until
    old_task.last_heartbeat = None
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_state"] = "retry_wait"
    blob["worker_retry_until"] = retry_until.isoformat()
    blob.pop("worker_heartbeat_at", None)
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    replaced = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert replaced["session_id"] == started["session_id"]
    assert replaced["worker_generation"] == started["worker_generation"] + 1
    assert replaced["task_id"] != started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_reload_ensure_replaces_retry_after_task_heartbeat_goes_stale(db_session):
    """The retry bridge stays bounded and still reclaims a genuinely dead task."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    stale_at = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.QUEUED
    old_task.last_heartbeat = stale_at
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob.pop("worker_heartbeat_at", None)
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    replaced = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert replaced["session_id"] == started["session_id"]
    assert replaced["worker_generation"] == started["worker_generation"] + 1
    assert replaced["task_id"] != started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_explicit_runner_replaces_a_genuinely_stale_active_worker(db_session):
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    first_runner = await _make_runner(db_session, project)
    second_runner = await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(first_runner.id),
    )
    stale_at = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.QUEUED
    old_task.last_heartbeat = stale_at
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob.pop("worker_heartbeat_at", None)
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    replaced = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(second_runner.id),
    )

    assert replaced["session_id"] == started["session_id"]
    assert replaced["worker_generation"] == started["worker_generation"] + 1
    assert replaced["task_id"] != started["task_id"]
    assert replaced["runner_id"] == str(second_runner.id)
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_reload_ensure_replaces_worker_after_task_corroboration_grace(
    db_session,
):
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    stale_at = _utcnow() - timedelta(
        seconds=(
            resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS
            + resident_session_module._RESIDENT_TASK_HEARTBEAT_CORROBORATION_GRACE_SECONDS
            + 1
        )
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.IN_PROGRESS
    old_task.last_heartbeat = _utcnow() - timedelta(seconds=8)
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_heartbeat_at"] = stale_at.isoformat()
    blob["updated_at"] = stale_at.isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    replaced = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert replaced["session_id"] == started["session_id"]
    assert replaced["worker_generation"] == started["worker_generation"] + 1
    assert replaced["task_id"] != started["task_id"]
    await db_session.refresh(old_task)
    assert old_task.status == TaskStatus.CANCELLED
    tasks = (
        await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
    ).scalars().all()
    live_residents = [
        row
        for row in tasks
        if (row.context or {}).get("_resident_session") is True
        and row.status in {
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        }
    ]
    assert len(live_residents) == 1
    assert str(live_residents[0].id) == replaced["task_id"]


@pytest.mark.asyncio
async def test_recovery_carries_rate_limit_denylist_but_not_failed_runtime_identity(
    db_session,
):
    """A new Resident generation must not rediscover exhausted accounts."""
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    stale_at = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.IN_PROGRESS
    old_task.last_heartbeat = stale_at
    old_task.llm_account_id = "failed-account"
    old_task.context = {
        **(old_task.context or {}),
        "blacklisted_accounts": ["account-one", " account-two ", "account-one", 7],
        "llm_account_id": "failed-account",
        "llm_provider": "anthropic",
        "llm_model": "claude-opus-5",
    }
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_heartbeat_at"] = stale_at.isoformat()
    blob["updated_at"] = stale_at.isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()

    replaced = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert replaced["task_id"] != started["task_id"]
    replacement = await db_session.get(Task, uuid.UUID(replaced["task_id"]))
    assert replacement is not None
    assert replacement.context["blacklisted_accounts"] == [
        "account-one",
        "account-two",
    ]
    # The dispatch path, rather than recovery, must select the next runtime.
    assert "llm_account_id" not in replacement.context
    assert "llm_provider" not in replacement.context
    assert "llm_model" not in replacement.context
    assert replacement.llm_account_id is None



@pytest.mark.asyncio
async def test_stop_ends_task_releases_continuation_and_hands_off_messages(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    cycle = await _link_phase2_cycle(db_session, project, task)
    factory = _test_session_factory(db_session)
    emit = AsyncMock()
    monkeypatch.setattr(resident_session_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        "app.strategic_cycle.emit_terminal_completion_events", emit
    )
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["typed_fast"] = {
        "lease_id": "typed-fast-stop",
        "generation": 1,
        "kind": "typed_fast",
        "status": "active",
        "account_id": "pool-fast-stop",
        "slot_reserved": True,
        "expires_at": (_utcnow() + timedelta(minutes=5)).isoformat(),
    }
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    pending = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Keep this operator turn after stop.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    released_fast_slot = AsyncMock()
    monkeypatch.setattr(
        "app.max_plan_pool.release_max_plan_auxiliary_slot", released_fast_slot
    )

    project = await db_session.get(Project, project.id)
    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "ended"
    assert snapshot["stop_requested"] is True
    refreshed_task = await db_session.get(Task, task.id)
    assert refreshed_task.status == TaskStatus.CANCELLED
    assert refreshed_task.context["failure_category"] == "ADMIN_CANCEL"
    refreshed_project = await db_session.get(Project, project.id)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (refreshed_project.vision or {})
    assert refreshed_project.vision[RESIDENT_SESSION_KEY]["typed_fast"]["status"] == "ended"
    assert refreshed_project.vision[RESIDENT_SESSION_KEY]["typed_fast"]["slot_reserved"] is False
    released_fast_slot.assert_awaited_once_with(
        "pool-fast-stop", project_id=str(project.id)
    )
    await db_session.refresh(pending)
    assert pending.status == "open"
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None
    refreshed_cycle = await db_session.get(StrategicCycle, cycle.id)
    await db_session.refresh(refreshed_cycle)
    assert refreshed_cycle.outcome == "failed_phase2"
    _assert_resident_terminal_receipt(emit, cycle, "resident_operator_stop")


@pytest.mark.asyncio
async def test_stop_with_strategic_api_marker_preserves_cancelled_cycle_disposition(
    db_session, monkeypatch
):
    """A stop receipt must remain authoritative after API side effects run."""
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    cycle = await _link_phase2_cycle(db_session, project, task)
    task.status = TaskStatus.CANCELLED
    task.context = {
        "strategic_cycle_id": cycle.id,
        "_strategic_api_called": True,
    }
    db_session.add(task)
    await db_session.commit()

    terminal_emit = AsyncMock()
    completion_emit = AsyncMock()
    monkeypatch.setattr(
        resident_session_module,
        "AsyncSessionLocal",
        _test_session_factory(db_session),
    )
    monkeypatch.setattr(
        "app.strategic_cycle.emit_terminal_completion_events", terminal_emit
    )
    monkeypatch.setattr(
        "app.strategic_events.emit_strategic_cycle_completed", completion_emit
    )

    await resident_session_module._run_resident_completion_on_stop(task.id)

    refreshed_cycle = await db_session.get(StrategicCycle, cycle.id)
    await db_session.refresh(refreshed_cycle)
    assert refreshed_cycle.outcome == "failed_phase2"
    _assert_resident_terminal_receipt(
        terminal_emit, cycle, "resident_operator_stop"
    )
    completion_emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_worker_supersession_reconciles_cycle_and_emits_receipt(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    cycle = await _link_phase2_cycle(db_session, project, old_task)
    stale_at = _utcnow() - timedelta(
        seconds=resident_session_module._RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS + 1
    )
    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.QUEUED
    old_task.last_heartbeat = stale_at
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = (_utcnow() - timedelta(minutes=10)).isoformat()
    blob["worker_heartbeat_at"] = stale_at.isoformat()
    blob["updated_at"] = stale_at.isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, old_task])
    await db_session.commit()
    emit = AsyncMock()
    monkeypatch.setattr(
        "app.strategic_cycle.emit_terminal_completion_events", emit
    )
    replaced = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        session_factory=_test_session_factory(db_session),
    )

    assert replaced["task_id"] != started["task_id"]
    refreshed_old_task = await db_session.get(Task, old_task.id)
    refreshed_cycle = await db_session.get(StrategicCycle, cycle.id)
    await db_session.refresh(refreshed_old_task)
    await db_session.refresh(refreshed_cycle)
    assert refreshed_old_task.status == TaskStatus.CANCELLED
    assert refreshed_cycle.outcome == "failed_phase2"
    _assert_resident_terminal_receipt(
        emit, cycle, "resident_worker_supersession"
    )


@pytest.mark.asyncio
async def test_failed_start_compensation_reconciles_cycle_and_emits_receipt(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    session_id = "ss-failed-start"
    task = Task(
        project_id=project.id,
        title="Resident strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": 1,
        },
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.flush()
    cycle = await _link_phase2_cycle(db_session, project, task)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": session_id,
            "worker_generation": 1,
            "status": "active",
            "task_id": str(task.id),
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    emit = AsyncMock()
    monkeypatch.setattr(
        "app.strategic_cycle.emit_terminal_completion_events", emit
    )

    await resident_session_module._release_start_claim(
        project.id,
        session_id,
        task.id,
        worker_generation=1,
        session_factory=_test_session_factory(db_session),
    )

    refreshed_task = await db_session.get(Task, task.id)
    refreshed_cycle = await db_session.get(StrategicCycle, cycle.id)
    refreshed_project = await db_session.get(Project, project.id)
    await db_session.refresh(refreshed_task)
    await db_session.refresh(refreshed_cycle)
    await db_session.refresh(refreshed_project)
    assert refreshed_task.status == TaskStatus.CANCELLED
    assert refreshed_cycle.outcome == "failed_phase2"
    assert RESIDENT_SESSION_KEY not in (refreshed_project.vision or {})
    _assert_resident_terminal_receipt(
        emit, cycle, "resident_start_compensation"
    )


@pytest.mark.asyncio
async def test_stop_handoff_is_atomic_with_session_end(db_session, monkeypatch):
    """Message release must commit in the same transaction as the session-end.

    Even if rearm fails after the commit, the message mutations are durable.
    This validates the non-atomic two-transaction window is closed: previously,
    a process death between the session-end commit and the follow-up handoff
    commit would leave messages stamped with the ended session permanently.
    """
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    pending = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Will this message survive rearm failure?",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()

    # Simulate rearm raising after commit — handoff mutations must already be durable.
    import app.session_message_handoff as handoff_module

    async def _raise_on_rearm(*args, **kwargs):
        raise RuntimeError("rearm failed — simulating post-commit crash")

    monkeypatch.setattr(handoff_module, "_run_handoff_rearm", _raise_on_rearm)

    project = await db_session.get(Project, project.id)
    # stop_resident_session should still succeed despite rearm raising
    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "ended"
    # The message must already be released (mutations committed atomically with session-end)
    await db_session.refresh(pending)
    assert pending.resident_session_id is None, (
        "message.resident_session_id must be None: handoff must commit atomically "
        "with session-end, not in a separate follow-up transaction"
    )
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_unattended_cap_persists_message_handoff(db_session, monkeypatch):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    pending = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Do not lose this turn when the unattended cap ends Workday.",
        created_at=_utcnow() - timedelta(hours=2),
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
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

    project = await db_session.get(Project, project.id)
    capped = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert capped["authority_held"] is False
    assert capped["stop_requested"] is True
    assert capped["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.latest_status_message == (
        "Resident strategic session ended after reaching the idle limit"
    )
    assert task.post_commit_pending is True
    assert task.context["_post_commit_pending_meta"]["reason"] == (
        "resident_session_ended"
    )
    assert task.context["_post_commit_pending_meta"]["payload"]["status"] == (
        TaskStatus.COMPLETED
    )
    await db_session.refresh(pending)
    assert pending.status == "open"
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_unattended_cap_handoff_is_atomic_with_cap_end(
    db_session, monkeypatch
):
    """Message release must commit atomically with the cap-end, not after it.

    Even if rearm raises after the commit, the message release must be durable.
    """
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    pending = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Will this survive rearm failure on cap?",
        created_at=_utcnow() - timedelta(hours=2),
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 60)

    # Simulate rearm raising after commit — message mutations must be durable.
    import app.session_message_handoff as handoff_module

    async def _raise_on_rearm(*args, **kwargs):
        raise RuntimeError("rearm failed — simulating post-cap crash")

    monkeypatch.setattr(handoff_module, "_run_handoff_rearm", _raise_on_rearm)

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
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert capped["authority_held"] is False
    assert capped["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True
    # Message must be released even though rearm raised
    await db_session.refresh(pending)
    assert pending.resident_session_id is None, (
        "message.resident_session_id must be None: cap handoff must commit "
        "atomically with the cap-end, not in a separate follow-up transaction"
    )
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_stale_continuation_lease_atomically_terminalizes_resident_task(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(
        runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 86_400
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    lapsed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )

    assert lapsed["authority_held"] is False
    assert lapsed["stop_requested"] is True
    assert lapsed["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True
    assert task.latest_status_message == (
        "Resident strategic session ended after its continuation lease lapsed"
    )
    pending = task.context["_post_commit_pending_meta"]
    assert pending["reason"] == "resident_session_ended"
    assert pending["payload"]["status"] == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cap_never_terminalizes_cross_project_resident_task_pointer(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    foreign_project = Project(
        name="Foreign resident project", user_id=TEST_USER_ID
    )
    db_session.add(foreign_project)
    await db_session.commit()
    await db_session.refresh(foreign_project)
    foreign_task = Task(
        project_id=foreign_project.id,
        payloads=[{"type": "strategic_review", "resident_session": True}],
        status=TaskStatus.IN_PROGRESS,
        context={
            "_resident_session": True,
            "strategic_session_id": started["session_id"],
            "_resident_worker_generation": 1,
        },
    )
    db_session.add(foreign_task)
    await db_session.commit()
    await db_session.refresh(foreign_task)

    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 60)
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["task_id"] = str(foreign_task.id)
    blob["worker_incarnation_id"] = str(
        legacy_resident_worker_incarnation(foreign_task.id, 1)
    )
    blob["worker_incarnation_expires_at"] = (
        _utcnow() + timedelta(minutes=5)
    ).isoformat()
    blob["last_operator_interaction_at"] = (
        _utcnow() - timedelta(hours=2)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    ended = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=str(foreign_task.id),
        progress=False,
    )

    assert ended["status"] == "ended"
    await db_session.refresh(foreign_task)
    assert foreign_task.status == TaskStatus.IN_PROGRESS
    assert foreign_task.post_commit_pending is False


@pytest.mark.asyncio
async def test_handoff_releases_unanswered_and_resolves_answered(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    session_id = "ss-ended"
    durable_claim_id = uuid.uuid4()
    unanswered = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="What should happen next?",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        conversation_claim_id=durable_claim_id,
        conversation_claim_generation=1,
        conversation_claim_expires_at=_utcnow() + timedelta(minutes=1),
        conversation_claim_attempt=1,
        resident_session_id=session_id,
    )
    answered = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Use the smaller rollout.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resolution="agent reply recorded",
        resident_session_id=session_id,
    )
    other_session = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Do not touch this session.",
        status="open",
        resident_session_id="ss-other",
    )
    db_session.add_all([unanswered, answered, other_session])
    await db_session.commit()

    rearm = AsyncMock(return_value="steering")
    monkeypatch.setattr("app.strategic_trigger.maybe_arm_operator_refresh", rearm)
    changed = await handoff_session_messages(db_session, project.id, session_id)
    await db_session.commit()

    assert changed == 2
    await db_session.refresh(unanswered)
    assert unanswered.status == "open"
    assert unanswered.resident_session_id is None
    assert unanswered.handling_state == MessageHandlingState.PENDING
    assert unanswered.handled_by_task_id is None
    assert unanswered.conversation_claim_id == durable_claim_id
    assert unanswered.conversation_claim_expires_at <= _utcnow()
    await db_session.refresh(answered)
    assert answered.status == "resolved"
    assert answered.handling_state == MessageHandlingState.RESPONDED
    assert answered.resolution == "agent reply recorded"
    assert answered.resolved_at is not None
    await db_session.refresh(other_session)
    assert other_session.status == "open"
    assert other_session.resident_session_id == "ss-other"
    rearm.assert_awaited_once()
    assert rearm.await_args.kwargs["resident_session_active"] is False


@pytest.mark.asyncio
async def test_repair_excludes_db_authoritative_active_session(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-current",
            "task_id": str(task.id),
            "worker_generation": 2,
            "worker_state": "ready",
            "status": "active",
            "stop_requested": False,
            "started_at": _utcnow().isoformat(),
            "updated_at": _utcnow().isoformat(),
        }
    }
    flag_modified(project, "vision")
    db_session.add(project)
    stale = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="This survived an interrupted end-session handoff.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id="ss-ended-before-handoff",
    )
    current = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="This belongs only to the current resident.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id="ss-current",
    )
    db_session.add_all([stale, current])
    await db_session.commit()

    rearm = AsyncMock(return_value="resident_active")
    monkeypatch.setattr("app.strategic_trigger.maybe_arm_operator_refresh", rearm)

    changed = await repair_stale_session_message_handoffs(
        db_session,
        project.id,
    )

    assert changed == 1
    await db_session.refresh(stale)
    assert stale.status == "open"
    assert stale.resident_session_id is None
    assert stale.handling_state == MessageHandlingState.PENDING
    assert stale.handled_by_task_id is None
    await db_session.refresh(current)
    assert current.status == "open"
    assert current.resident_session_id == "ss-current"
    assert current.handling_state == MessageHandlingState.PENDING
    rearm.assert_awaited_once()
    assert rearm.await_args.kwargs["resident_session_active"] is True

    # A later ensure is idempotent after the repair has committed.
    assert (
        await repair_stale_session_message_handoffs(
            db_session,
            project.id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_repair_noop_candidate_releases_per_id_project_transaction(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    stale = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="A competing repair releases this after discovery.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id="ss-already-repaired",
    )
    db_session.add(stale)
    await db_session.commit()

    handoff_calls: list[tuple[str, bool]] = []

    async def already_repaired_before_application(
        session,
        project_id,
        resident_session_id,
        *,
        replacement_resident_active=False,
    ):
        del project_id
        assert session.in_transaction() is True
        handoff_calls.append(
            (resident_session_id, replacement_resident_active)
        )
        return 0

    monkeypatch.setattr(
        "app.session_message_handoff.handoff_session_messages",
        already_repaired_before_application,
    )

    changed = await repair_stale_session_message_handoffs(
        db_session,
        project.id,
    )

    assert changed == 0
    assert handoff_calls == [("ss-already-repaired", False)]
    assert db_session.in_transaction() is False


@pytest.mark.asyncio
async def test_repair_rechecks_active_session_between_stale_ids(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    first = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="This belongs to the first ended resident.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id="ss-a",
    )
    replacement = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="This becomes owned by the replacement resident.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id="ss-b",
    )
    db_session.add_all([first, replacement])
    await db_session.commit()

    repaired_ids: list[str] = []

    async def activate_replacement_during_first_handoff(
        session,
        project_id,
        resident_session_id,
        *,
        replacement_resident_active=False,
    ):
        del replacement_resident_active
        repaired_ids.append(resident_session_id)
        assert resident_session_id == "ss-a"
        current_project = await session.get(Project, project_id)
        now = _utcnow().isoformat()
        current_project.vision = {
            **(current_project.vision or {}),
            RESIDENT_SESSION_KEY: {
                "session_id": "ss-b",
                "task_id": str(task.id),
                "worker_generation": 2,
                "worker_state": "ready",
                "status": "active",
                "stop_requested": False,
                "started_at": now,
                "updated_at": now,
            },
        }
        flag_modified(current_project, "vision")
        session.add(current_project)
        await session.commit()
        return 1

    monkeypatch.setattr(
        "app.session_message_handoff.handoff_session_messages",
        activate_replacement_during_first_handoff,
    )

    changed = await repair_stale_session_message_handoffs(
        db_session,
        project.id,
    )

    assert changed == 1
    assert repaired_ids == ["ss-a"]
    await db_session.refresh(replacement)
    assert replacement.resident_session_id == "ss-b"
    assert replacement.status == "open"


@pytest.mark.asyncio
async def test_handoff_survives_rearm_rollback(db_session, monkeypatch):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    pending = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Persist me before cadence arming.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id="ss-rearm-rollback",
    )
    db_session.add(pending)
    await db_session.commit()

    async def rollback_and_decline(session, *_args, **_kwargs):
        await session.rollback()
        return "not_armed"

    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh", rollback_and_decline
    )
    changed = await handoff_session_messages(
        db_session, project.id, "ss-rearm-rollback"
    )

    assert changed == 1
    # No caller commit: the handoff's own durability boundary must survive the
    # cadence helper rolling back its separate transaction.
    await db_session.refresh(pending)
    assert pending.status == "open"
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None
    assert pending.resolution is None
    assert pending.resolved_at is None


@pytest.mark.asyncio
async def test_handoff_treats_canonical_child_reply_as_answer_evidence(db_session):
    project = await _make_project(db_session)
    source = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Is the restore complete?",
        status="open",
        resident_session_id="ss-replied",
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="Yes.",
            status="open",
            reply_to_message_id=source.id,
        )
    )
    await db_session.commit()

    changed = await handoff_session_messages(db_session, project.id, "ss-replied")
    await db_session.commit()

    assert changed == 1
    await db_session.refresh(source)
    assert source.status == "resolved"
    assert source.handling_state == MessageHandlingState.RESPONDED


@pytest.mark.asyncio
async def test_handoff_does_not_treat_user_child_as_answer_evidence(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    source = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Is this actually answered by Strategic?",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id="ss-user-child",
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Another operator comment is not an agent answer.",
            status="open",
            reply_to_message_id=source.id,
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )

    changed = await handoff_session_messages(
        db_session, project.id, "ss-user-child"
    )

    assert changed == 1
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.resident_session_id is None
    assert source.handling_state == MessageHandlingState.PENDING
    assert source.handled_by_task_id is None
    assert source.resolution is None
    assert source.resolved_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_claim", ["unclaimed", "mismatched"])
async def test_handoff_rejects_task_bearing_reply_without_explicit_task_match(
    db_session, monkeypatch, source_claim
):
    project = await _make_project(db_session)
    handling_task = await _make_task(db_session, project)
    reply_task = Task(
        project_id=project.id,
        title="Different Strategic producer",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        vm_instance_id="agent-vm-different-producer",
        created_by="ORCHESTRATOR",
    )
    db_session.add(reply_task)
    await db_session.commit()
    await db_session.refresh(reply_task)

    source = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content=f"Task provenance case: {source_claim}",
        status="open",
        handling_state=(
            MessageHandlingState.PENDING
            if source_claim == "unclaimed"
            else MessageHandlingState.CLAIMED
        ),
        handled_by_task_id=(
            None if source_claim == "unclaimed" else handling_task.id
        ),
        resident_session_id=f"ss-task-evidence-{source_claim}",
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            from_task_id=reply_task.id,
            content="A task-bearing child without an explicit source-task match.",
            status="resolved",
            reply_to_message_id=source.id,
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )

    changed = await handoff_session_messages(
        db_session, project.id, f"ss-task-evidence-{source_claim}"
    )

    assert changed == 1
    await db_session.refresh(source)
    assert source.status == "open"
    assert source.resident_session_id is None
    assert source.handling_state == MessageHandlingState.PENDING
    assert source.handled_by_task_id is None
    assert source.resolution is None
    assert source.resolved_at is None


def test_resident_reaper_uses_own_heartbeat_window():
    now = _utcnow()
    task = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title="Resident",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        context={"_resident_session": True},
        vm_instance_id="agent-vm-resident-reaper",
        created_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=10),
        last_heartbeat=now - timedelta(seconds=30),
    )
    previous = runtime_config.get("resident_session_heartbeat_stale_seconds")
    runtime_config["resident_session_heartbeat_stale_seconds"] = 60
    try:
        assert _classify_resident_session(task, now, None) is False
        task.last_heartbeat = now - timedelta(minutes=2)
        assert _classify_resident_session(task, now, None) is True
        assert (
            _classify_resident_session(
                task,
                now,
                {task.vm_instance_id: now - timedelta(seconds=10)},
            )
            is False
        )
        assert (
            _classify_resident_session(
                task,
                now,
                {task.vm_instance_id: now - timedelta(seconds=10)},
                runner_excluded=True,
            )
            is True
        )
    finally:
        if previous is None:
            runtime_config.pop("resident_session_heartbeat_stale_seconds", None)
        else:
            runtime_config["resident_session_heartbeat_stale_seconds"] = previous


@pytest.mark.asyncio
async def test_heartbeat_handled_ids_only_affect_own_session(db_session, monkeypatch):
    """Handled ids affect own-session messages and exact server wake barriers.

    Legacy / cadence messages (resident_session_id IS NULL) and messages pinned
    to a different session must remain untouched even when their IDs appear in
    handled_message_ids. A NULL-session strategist-internal wake barrier is the
    only project-scoped exception.
    """
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)

    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )

    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    session_id = started["session_id"]

    # Own-session message: must be marked handled.
    own_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="This operator turn belongs to the active session.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=session_id,
    )
    # NULL-session (legacy/cadence): must NOT be touched.
    null_session_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Standing project message (BUG_REPORT style) — no session.",
        status="open",
        resident_session_id=None,
    )
    # Different session: must NOT be touched.
    other_session_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="From an earlier or parallel session.",
        status="open",
        resident_session_id="ss-other-entirely",
    )
    wake_barrier = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Discovery direction is parked pending a reproducible result.",
        status="open",
        resident_session_id=None,
        context={
            "audience": "strategist-internal",
            "event_type": "discovery.direction_parked",
            "resident_wake_barrier": True,
            "cycle_id": "11111111-1111-1111-1111-111111111111",
            "cycle_revision": 7,
            "direction_key": "bounded-research-bet",
            "resume_condition": "A reproducible result is recorded.",
        },
    )
    db_session.add_all(
        [own_msg, null_session_msg, other_session_msg, wake_barrier]
    )
    await db_session.commit()
    for m in (own_msg, null_session_msg, other_session_msg, wake_barrier):
        await db_session.refresh(m)

    project = await db_session.get(Project, project.id)
    initial = await renew_resident_session_lease(
        db_session,
        project,
        session_id=session_id,
        task_id=started["task_id"],
        progress=False,
    )
    assert initial["fired_barriers"] == [
        {
            "id": str(wake_barrier.id),
            "event_type": "discovery.direction_parked",
            "summary": "Discovery direction is parked pending a reproducible result.",
            "created_at": wake_barrier.created_at.isoformat(),
            "task_id": None,
            "context": {
                "event_type": "discovery.direction_parked",
                "cycle_id": "11111111-1111-1111-1111-111111111111",
                "cycle_revision": 7,
                "direction_key": "bounded-research-bet",
                "resume_condition": "A reproducible result is recorded.",
            },
        }
    ]
    acknowledged = await renew_resident_session_lease(
        db_session,
        project,
        session_id=session_id,
        task_id=started["task_id"],
        progress=False,
        handled_message_ids=[
            str(own_msg.id),
            str(null_session_msg.id),
            str(other_session_msg.id),
            str(wake_barrier.id),
        ],
    )
    assert acknowledged["fired_barriers"] == []
    assert acknowledged["work_state_summary"]["pending_barriers"] == 0

    followup = await renew_resident_session_lease(
        db_session,
        project,
        session_id=session_id,
        task_id=started["task_id"],
        progress=False,
    )
    assert followup["fired_barriers"] == []
    assert followup["work_state_summary"]["pending_barriers"] == 0
    assert followup["work_state_rev"] == acknowledged["work_state_rev"]
    assert acknowledged["work_state_rev"] == initial["work_state_rev"]

    await db_session.refresh(own_msg)
    await db_session.refresh(null_session_msg)
    await db_session.refresh(other_session_msg)
    await db_session.refresh(wake_barrier)

    assert own_msg.status == "handled", (
        "Own-session message must be marked handled by the heartbeat"
    )
    assert null_session_msg.status == "open", (
        "NULL-session (legacy/cadence) message must NOT be marked handled"
    )
    assert other_session_msg.status == "open", (
        "Message from a different session must NOT be marked handled"
    )
    assert wake_barrier.status == "handled"


# ---------------------------------------------------------------------------
# operator_decision_summary absence/staleness gap tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_absent_decision_summary_fires_alert_after_stale_window(
    db_session, monkeypatch
):
    """A heartbeat with operator_decision_summary=None fires a strategist-internal
    absence notice when the prior summary timestamp is older than
    _OPERATOR_DECISION_SUMMARY_STALE_SECONDS and total >= OPERATOR_DECISION_CAPTURE_MIN_SAMPLE.
    """
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # First heartbeat: report a valid summary with enough sample count so the
    # staleness detector has prior data to reference.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "captured": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "missed": 0,
            "capture_rate": 100,
        },
    )

    # Artificially age the _operator_decision_summary_at so it looks stale.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["_operator_decision_summary_at"] = (
        _utcnow() - timedelta(seconds=_OPERATOR_DECISION_SUMMARY_STALE_SECONDS + 60)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # Second heartbeat: agent stops sending the summary (None). Should fire notice.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )

    # Verify the dedup flag was set.
    project = await db_session.get(Project, project.id)
    blob = read_resident_session(project.vision)
    assert blob.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY) is True, (
        "Absence dedup flag must be set after notice fires"
    )

    # Verify a strategist-internal notice message was posted.
    from sqlmodel import select as _sel
    msgs = (
        await db_session.execute(
            _sel(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["event_type"].as_string()
                == "operator_decision.summary_absent",
            )
        )
    ).scalars().all()
    assert len(msgs) == 1, (
        "Exactly one absence notice must be posted on the first stale absent heartbeat"
    )
    assert "stopped refreshing" in msgs[0].content


@pytest.mark.asyncio
async def test_heartbeat_absent_decision_summary_no_alert_before_stale_window(
    db_session, monkeypatch
):
    """No absence notice fires when the prior summary timestamp is within the
    staleness window (recent gap, not a sustained absence).
    """
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # First heartbeat with a valid summary.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "captured": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "missed": 0,
            "capture_rate": 100,
        },
    )

    # Leave the _operator_decision_summary_at fresh (just now) and send None.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )

    project = await db_session.get(Project, project.id)
    blob = read_resident_session(project.vision)
    assert not blob.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY), (
        "Absence dedup flag must NOT be set when gap is within the stale window"
    )

    from sqlmodel import select as _sel
    count = (
        await db_session.execute(
            _sel(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["event_type"].as_string()
                == "operator_decision.summary_absent",
            )
        )
    ).scalars().all()
    assert count == [], "No absence notice must fire within the staleness window"


@pytest.mark.asyncio
async def test_heartbeat_absent_decision_summary_no_alert_below_min_sample(
    db_session, monkeypatch
):
    """No absence notice fires when the prior summary's total is below
    OPERATOR_DECISION_CAPTURE_MIN_SAMPLE (session too new to judge).
    """
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # First heartbeat: summary with total below the minimum sample threshold.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE - 1,
            "captured": 0,
            "missed": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE - 1,
            "capture_rate": 0,
        },
    )

    # Age the timestamp past the stale window.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["_operator_decision_summary_at"] = (
        _utcnow() - timedelta(seconds=_OPERATOR_DECISION_SUMMARY_STALE_SECONDS + 60)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )

    project = await db_session.get(Project, project.id)
    blob = read_resident_session(project.vision)
    assert not blob.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY), (
        "Absence dedup flag must NOT be set when prior total < OPERATOR_DECISION_CAPTURE_MIN_SAMPLE"
    )


@pytest.mark.asyncio
async def test_heartbeat_absent_decision_summary_dedup_second_heartbeat(
    db_session, monkeypatch
):
    """A second absent heartbeat after the dedup flag is set must NOT re-emit the
    notice.
    """
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Seed a stale summary.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "captured": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "missed": 0,
            "capture_rate": 100,
        },
    )
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["_operator_decision_summary_at"] = (
        _utcnow() - timedelta(seconds=_OPERATOR_DECISION_SUMMARY_STALE_SECONDS + 60)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    # First absent heartbeat — notice fires.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )

    # Second absent heartbeat — dedup flag already set, no second notice.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )

    from sqlmodel import select as _sel
    msgs = (
        await db_session.execute(
            _sel(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["event_type"].as_string()
                == "operator_decision.summary_absent",
            )
        )
    ).scalars().all()
    assert len(msgs) == 1, (
        "Absence notice must fire exactly once even across multiple absent heartbeats"
    )


@pytest.mark.asyncio
async def test_heartbeat_absent_decision_summary_recovery_clears_dedup(
    db_session, monkeypatch
):
    """When the agent resumes sending a valid operator_decision_summary, the
    absence dedup flag is cleared so a future absence episode can re-fire.
    """
    monkeypatch.setattr(
        "app.strategic_trigger.maybe_arm_operator_refresh",
        AsyncMock(return_value="steering"),
    )
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    # Seed a stale summary and trigger the absence notice.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "captured": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
            "missed": 0,
            "capture_rate": 100,
        },
    )
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["_operator_decision_summary_at"] = (
        _utcnow() - timedelta(seconds=_OPERATOR_DECISION_SUMMARY_STALE_SECONDS + 60)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary=None,
    )
    project = await db_session.get(Project, project.id)
    assert read_resident_session(project.vision).get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY) is True

    # Recovery heartbeat: agent sends a valid summary → dedup flag cleared.
    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        operator_decision_summary={
            "total": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE + 1,
            "captured": OPERATOR_DECISION_CAPTURE_MIN_SAMPLE + 1,
            "missed": 0,
            "capture_rate": 100,
        },
    )

    project = await db_session.get(Project, project.id)
    blob = read_resident_session(project.vision)
    assert blob.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY) is False, (
        "Absence dedup flag must be cleared when agent resumes sending a valid summary"
    )


# ══════════════════════════════════════════════════════════════════════════════
# allow_no_runner fallback
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_start_allow_no_runner_creates_session_without_task(db_session):
    """With no available runner and ``allow_no_runner=True``, the session starts
    without a backing task — ``task_id`` and ``runner_id`` are None, no
    ``strategic_review`` Task row is spawned, and the blob is live.
    """
    project = await _make_project(db_session)
    # No runner enrolled — auto-select returns None.

    snapshot = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        allow_no_runner=True,
    )

    assert snapshot["status"] == "active", (
        "Session status must be 'active' — the lifecycle is exercised"
    )
    assert snapshot["task_id"] is None, (
        "No backing task when allow_no_runner skips spawn"
    )
    assert snapshot["runner_id"] is None, (
        "No runner when none is enrolled and allow_no_runner is True"
    )
    assert snapshot.get("worker_state") == "starting", (
        "Worker state must remain 'starting' — no agent process streams"
    )

    # Verify no Task row was created.
    tasks = (
        await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
    ).scalars().all()
    assert len(tasks) == 0, (
        "No strategic_review task must be spawned when allow_no_runner is True"
    )

    # Verify the blob is durable (re-read through resident_session_snapshot).
    project = await db_session.get(Project, project.id)
    blob = read_resident_session(project.vision)
    assert blob is not None
    assert blob["session_id"] == snapshot["session_id"]
    assert blob["task_id"] is None
    assert blob["runner_id"] is None
    assert blob["worker_state"] == "starting"
    assert blob["worker_incarnation_protocol"] == 1
    assert blob["status"] == "active"


@pytest.mark.asyncio
async def test_allow_no_runner_default_false_blocks_without_device(db_session):
    """Default ``allow_no_runner=False`` preserves the existing 409 block when
    no eligible runner exists.
    """
    project = await _make_project(db_session)
    # No runner enrolled.

    with pytest.raises(HTTPException) as exc_info:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
        )
    assert exc_info.value.status_code == 409
    assert "No device available" in exc_info.value.detail


@pytest.mark.asyncio
async def test_stop_cleans_up_no_runner_session_without_error(db_session):
    """A session started with ``allow_no_runner=True`` can be stopped cleanly —
    the stop path already tolerates ``task_id: None`` in the blob.
    """
    project = await _make_project(db_session)

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        allow_no_runner=True,
    )
    assert started["status"] == "active"
    assert started["task_id"] is None

    project = await db_session.get(Project, project.id)
    ended = await stop_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    assert ended["status"] == "ended"
    assert ended["session_id"] == started["session_id"]
    assert ended.get("stop_requested") is True

    # A second stop is a no-op (no blob to end).
    project = await db_session.get(Project, project.id)
    second = await stop_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    assert second.get("status") == "ended"
    assert second.get("session_id") == started["session_id"]
