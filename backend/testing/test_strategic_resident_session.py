"""Focused regression coverage for the restored Workday resident lifecycle.

Adapted from the legacy hardening suite in commit 06a97b8da. The restored
subsystem intentionally has no global feature gate or separate resident table,
so these tests cover the surviving Project.vision lifecycle, lease, message
handoff, and reaper contracts directly.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from inspect import unwrap
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from sqlalchemy.orm.attributes import flag_modified

import app.runner_pins as runner_pins_module
import app.strategic_resident_session as resident_session_module
from app.database import resolve_async_engine, resolve_dialect_name
from app.conversation_coordinator import (
    ConversationPublicReplyOwner,
    claim_next_workday_consultation_continuation,
    claim_next_workday_consultation_packet,
    claim_operator_messages,
    complete_claimed_public_turn,
)
from app.models import (
    LocalRunner,
    MessageHandlingState,
    MessageSurface,
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
from app.runner_pins import release_unpulled_runner_pins
from app.resident_conversation_scope import (
    RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY,
    RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY,
)
from app.session_message_handoff import (
    RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
    handoff_session_messages,
    repair_stale_session_message_handoffs,
)
from app.shared_state import runtime_config
from app.strategic_resident_session import (
    OPERATOR_DECISION_ABSENT_NOTIFIED_KEY,
    OPERATOR_DECISION_CAPTURE_MIN_SAMPLE,
    RESIDENT_SESSION_KEY,
    RESIDENT_TASK_PRIORITY,
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


def _assert_resident_terminal_receipt(
    emit,
    cycle: StrategicCycle,
    source: str,
    *,
    expect_completion: bool = True,
):
    emit.assert_awaited_once()
    event = emit.await_args.args[0][0]
    assert event["cycle_id"] == cycle.id
    assert event["phase"] == 2
    assert event["phase_state"] == "failed_phase2"
    if not expect_completion:
        assert "completion" not in event
        return event
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


@pytest.mark.asyncio
async def test_runner_pin_release_preserves_post_spawn_provisional_resident(
    db_session,
    monkeypatch,
):
    """A drain in the spawn/finalize gap cannot orphan the new Resident task."""

    if resolve_async_engine(db_session).dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    factory = _test_session_factory(db_session)
    task_born = asyncio.Event()
    resume_start = asyncio.Event()
    original_spawn = resident_session_module._spawn_resident_strategic_task
    born_task_id: uuid.UUID | None = None

    async def pause_after_spawn_commit(*args, **kwargs):
        nonlocal born_task_id
        task = await original_spawn(*args, **kwargs)
        assert task is not None
        born_task_id = task.id
        task_born.set()
        await resume_start.wait()
        return task

    monkeypatch.setattr(
        resident_session_module,
        "_spawn_resident_strategic_task",
        pause_after_spawn_commit,
    )

    async def run_start():
        async with factory() as start_session:
            start_project = await start_session.get(Project, project.id)
            assert start_project is not None
            return await start_resident_session(
                start_session,
                start_project,
                operator_user_id=TEST_USER_ID,
                runner_id=str(runner.id),
                session_factory=factory,
            )

    start_task = asyncio.create_task(run_start())
    await asyncio.wait_for(task_born.wait(), timeout=10)
    try:
        assert born_task_id is not None
        async with factory() as release_session:
            provisional_project = await release_session.get(Project, project.id)
            assert provisional_project is not None
            provisional = read_resident_session(provisional_project.vision)
            assert provisional is not None
            assert provisional["task_id"] is None
            assert provisional["worker_state"] == "starting"

            released = await release_unpulled_runner_pins(
                release_session,
                runner.id,
                preserve_active_resident=True,
            )
            await release_session.commit()
            assert released == 0
    finally:
        resume_start.set()

    started = await asyncio.wait_for(start_task, timeout=15)
    assert started["task_id"] == str(born_task_id)
    async with factory() as verify_session:
        fresh_project = await verify_session.get(Project, project.id)
        fresh_task = await verify_session.get(Task, born_task_id)
    assert fresh_project is not None
    assert fresh_task is not None
    assert read_resident_session(fresh_project.vision)["task_id"] == str(
        born_task_id
    )
    assert fresh_task.status == TaskStatus.PROVISIONING
    assert fresh_task.vm_instance_id == f"runner:{runner.id}"
    assert fresh_task.context["runner_target_id"] == str(runner.id)


@pytest.mark.asyncio
async def test_runner_pin_release_rechecks_finalized_vision_after_candidate_scan(
    db_session,
    monkeypatch,
):
    """A stale candidate scan cannot overwrite a later canonical finalization."""

    if resolve_async_engine(db_session).dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    resident = Task(
        project_id=project.id,
        title="Resident born before finalization",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        vm_provisioned_at=_utcnow(),
        created_by="ORCHESTRATOR",
        context={
            "_resident_session": True,
            "strategic_session_id": "ss-runner-pin-stale-read",
            "_resident_worker_generation": 4,
            "runner_target_id": str(runner.id),
        },
    )
    db_session.add(resident)
    await db_session.flush()
    project.vision = {
        **(project.vision or {}),
        RESIDENT_SESSION_KEY: {
            "session_id": "ss-runner-pin-stale-read",
            "task_id": None,
            "worker_generation": 4,
            "worker_state": "starting",
            "runner_id": str(runner.id),
            "status": "active",
            "stop_requested": False,
        },
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    factory = _test_session_factory(db_session)
    candidate_scanned = asyncio.Event()
    vision_finalized = asyncio.Event()
    original_project_lock = runner_pins_module.project_lock

    @asynccontextmanager
    async def pause_before_locked_reread(*args, **kwargs):
        candidate_scanned.set()
        await vision_finalized.wait()
        async with original_project_lock(*args, **kwargs) as locked:
            yield locked

    monkeypatch.setattr(
        runner_pins_module,
        "project_lock",
        pause_before_locked_reread,
    )

    async def run_release():
        async with factory() as release_session:
            released = await release_unpulled_runner_pins(
                release_session,
                runner.id,
                preserve_active_resident=True,
            )
            await release_session.commit()
            return released

    release_task = asyncio.create_task(run_release())
    await asyncio.wait_for(candidate_scanned.wait(), timeout=5)
    try:
        async with factory() as finalize_session:
            async with resident_session_module.project_lock(
                finalize_session,
                project.id,
                skip_locked=False,
            ) as locked_project:
                assert locked_project is not None
                vision = dict(locked_project.vision or {})
                blob = dict(vision[RESIDENT_SESSION_KEY])
                blob["task_id"] = str(resident.id)
                blob["worker_state"] = "ready"
                vision[RESIDENT_SESSION_KEY] = blob
                locked_project.vision = vision
                flag_modified(locked_project, "vision")
                finalize_session.add(locked_project)
                await finalize_session.commit()
    finally:
        vision_finalized.set()

    assert await asyncio.wait_for(release_task, timeout=10) == 0
    async with factory() as verify_session:
        fresh_project = await verify_session.get(Project, project.id)
        fresh_task = await verify_session.get(Task, resident.id)
    assert fresh_project is not None
    assert fresh_task is not None
    assert read_resident_session(fresh_project.vision)["task_id"] == str(resident.id)
    assert fresh_task.status == TaskStatus.PROVISIONING
    assert fresh_task.vm_instance_id == f"runner:{runner.id}"


@pytest.mark.asyncio
async def test_explicit_runner_pin_release_waits_for_contended_project(
    db_session,
):
    """One-shot admin release is complete rather than a skip-locked best effort."""

    if resolve_async_engine(db_session).dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    pinned = Task(
        project_id=project.id,
        title="Explicitly released runner pin",
        payloads=[{"type": "create"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        vm_provisioned_at=_utcnow(),
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(pinned)
    await db_session.commit()

    factory = _test_session_factory(db_session)
    project_locked = asyncio.Event()
    release_project = asyncio.Event()

    async def hold_project_lock():
        async with factory() as holder_session:
            async with resident_session_module.project_lock(
                holder_session,
                project.id,
                skip_locked=False,
            ) as locked_project:
                assert locked_project is not None
                project_locked.set()
                await release_project.wait()
                await holder_session.commit()

    async def run_explicit_release():
        async with factory() as release_session:
            released = await release_unpulled_runner_pins(
                release_session,
                runner.id,
                preserve_active_resident=False,
            )
            await release_session.commit()
            return released

    holder_task = asyncio.create_task(hold_project_lock())
    await asyncio.wait_for(project_locked.wait(), timeout=5)
    release_task = asyncio.create_task(run_explicit_release())
    await asyncio.sleep(0.1)
    try:
        assert not release_task.done()
    finally:
        release_project.set()
    await asyncio.wait_for(holder_task, timeout=5)
    assert await asyncio.wait_for(release_task, timeout=5) == 1
    async with factory() as verify_session:
        fresh_task = await verify_session.get(Task, pinned.id)
    assert fresh_task is not None
    assert fresh_task.status == TaskStatus.QUEUED
    assert fresh_task.vm_instance_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    (
        "session",
        "generation",
        "runner",
        "stopped",
        "ended",
        "worker_state",
        "task_id",
        "task_runner_target",
    ),
)
async def test_runner_pin_release_does_not_preserve_mismatched_provisional_marker(
    db_session,
    mismatch,
):
    """Only the exact active starting claim receives provisional protection."""

    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    resident_context = {
        "_resident_session": True,
        "strategic_session_id": "ss-provisional-exactness",
        "_resident_worker_generation": 7,
        "runner_target_id": str(runner.id),
    }
    resident = Task(
        project_id=project.id,
        title="Provisional Resident marker",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{runner.id}",
        vm_provisioned_at=_utcnow(),
        created_by="ORCHESTRATOR",
        context=resident_context,
    )
    db_session.add(resident)
    await db_session.flush()
    blob = {
        "session_id": "ss-provisional-exactness",
        "task_id": None,
        "worker_generation": 7,
        "worker_state": "starting",
        "runner_id": str(runner.id),
        "status": "active",
        "stop_requested": False,
    }
    if mismatch == "session":
        resident_context["strategic_session_id"] = "ss-other"
    elif mismatch == "generation":
        resident_context["_resident_worker_generation"] = 8
    elif mismatch == "runner":
        blob["runner_id"] = str(uuid.uuid4())
    elif mismatch == "stopped":
        blob["stop_requested"] = True
    elif mismatch == "ended":
        blob["status"] = "ended"
    elif mismatch == "worker_state":
        blob["worker_state"] = "ready"
    elif mismatch == "task_id":
        blob["task_id"] = str(uuid.uuid4())
    elif mismatch == "task_runner_target":
        resident_context["runner_target_id"] = str(uuid.uuid4())
    resident.context = resident_context
    project.vision = {**(project.vision or {}), RESIDENT_SESSION_KEY: blob}
    flag_modified(project, "vision")
    db_session.add_all([project, resident])
    await db_session.commit()

    released = await release_unpulled_runner_pins(
        db_session,
        runner.id,
        preserve_active_resident=True,
    )
    await db_session.commit()
    await db_session.refresh(resident)

    assert released == 1
    assert resident.status == TaskStatus.QUEUED
    assert resident.vm_instance_id is None
    assert "runner_target_id" not in resident.context


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


def test_session_snapshot_exposes_bounded_activity_without_silence_deadline():
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
                    "reason_code": "consultation_running",
                    "consultation_id": "11111111-1111-4111-8111-111111111111",
                    "autonomy_state": "paused",
                    "autonomy_reason_code": "consultation_running",
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
        "reason_code": "consultation_running",
        "consultation_id": "11111111-1111-4111-8111-111111111111",
        "consultation_verified": False,
        "autonomy_state": "paused",
        "autonomy_reason_code": "consultation_running",
        "pending_operator_messages": 3,
        "next_review_at": "2026-07-25T12:05:00",
        # Operator silence is not a Continuous-session stop clock. The process
        # heartbeat and explicit mode/Stop fences own liveness instead.
        "idle_remaining_seconds": None,
        "pending_watches": 2,
        "fired_watches": 1,
    }


def test_session_snapshot_keeps_quiet_continuous_waiting_and_marks_explicit_end():
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
    quiet = resident_session_snapshot(
        vision,
        now=datetime.fromisoformat("2026-07-25T11:56:00"),
    )
    assert quiet is not None
    assert quiet["activity"]["phase"] == "waiting"
    assert quiet["activity"]["idle_remaining_seconds"] is None

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
    assert ended["activity"]["idle_remaining_seconds"] is None


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
async def test_post_message_bind_revalidates_exact_active_resident(db_session):
    """A committed Chat decision fences bind before best-effort stop finishes."""

    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="user",
        actor_user_id=TEST_USER_ID,
        content="Do not bind me to a stopped Resident.",
        status="open",
        resident_session_id=None,
        context={"_operator_wakeup_state": "paused"},
    )
    db_session.add(message)
    await db_session.commit()
    project_id = project.id
    message_id = message.id
    resident_session_id = started["session_id"]
    factory = _test_session_factory(db_session)

    # The mode endpoint commits Chat before its separate best-effort stop.
    # Leave the old Resident blob deliberately active and prove the persisted
    # mode alone already forbids rebinding a newly accepted row to it.
    async with factory() as mode_session:
        await persist_strategic_workbench_update(
            mode_session,
            TEST_USER_ID,
            project_id,
            {"interaction_mode": "chat"},
            arm_interaction_mode=False,
        )
    async with factory() as bind_session:
        bound = await resident_session_module._bind_pending_operator_message_to_resident(
            bind_session,
            project_id,
            message_id=message_id,
            resident_session_id=resident_session_id,
            operator_user_id=TEST_USER_ID,
        )

    assert bound is False
    async with factory() as verify:
        stored = await verify.get(ProjectMessage, message_id)
        stored_project = await verify.get(Project, project_id)
    assert stored is not None
    assert stored_project is not None
    assert resident_session_is_active(read_resident_session(stored_project.vision))
    assert stored.resident_session_id is None
    assert stored.handling_state == MessageHandlingState.PENDING
    assert stored.handled_by_task_id is None
    assert stored.conversation_claim_id is None
    assert stored.context["_operator_wakeup_state"] == "paused"


@pytest.mark.asyncio
async def test_operator_message_preensure_treats_mode_change_as_chat_safe(
    db_session,
    monkeypatch,
):
    """The internal expected-mode fence is a normal Stop race, not a 500."""

    project = await _make_project(db_session)
    await _make_runner(db_session, project)
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "continuous"},
        arm_interaction_mode=False,
    )

    async def mode_changed(*_args, **_kwargs):
        raise resident_session_module._ResidentEnsureModeChanged("stopped")

    monkeypatch.setattr(resident_session_module, "start_resident_session", mode_changed)
    snapshot = await resident_session_module.ensure_resident_session_before_operator_message(
        project.id,
        operator_user_id=TEST_USER_ID,
        session_factory=_test_session_factory(db_session),
    )

    assert snapshot is None
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
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


@pytest.mark.asyncio
async def test_owner_pinned_start_spawns_while_paused_and_clears_stale_reason(
    db_session,
):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    project.paused = True
    project.vision = {
        **(project.vision or {}),
        resident_session_module.RESIDENT_SESSION_REASON_KEY: {
            "reason": resident_session_module.RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
            "recorded_at": _utcnow().isoformat(),
            "operator_user_id": TEST_USER_ID,
        },
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    response = await unwrap(start_project_strategic_resident_session)(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )

    assert response["ensure_outcome"] == "ensured"
    assert response["session"]["status"] == "active"
    task = await db_session.get(Task, uuid.UUID(response["session"]["task_id"]))
    await db_session.refresh(project)
    assert task is not None
    assert task.status == TaskStatus.PROVISIONING
    assert task.vm_instance_id == f"runner:{runner.id}"
    assert task.context["_resident_session"] is True
    assert project.paused is True
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_existing_healthy_manual_start_clears_same_stale_reason(db_session):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    handler = unwrap(start_project_strategic_resident_session)
    started = await handler(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )
    reason = {
        "reason": resident_session_module.RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
        "recorded_at": _utcnow().isoformat(),
        "operator_user_id": TEST_USER_ID,
    }
    project.vision = {
        **(project.vision or {}),
        resident_session_module.RESIDENT_SESSION_REASON_KEY: reason,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    repeated = await handler(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": TEST_USER_ID},
    )

    assert repeated["session"]["session_id"] == started["session"]["session_id"]
    assert repeated["session"]["task_id"] == started["session"]["task_id"]
    await db_session.refresh(project)
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert len(tasks) == 1
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_member_pinned_start_cannot_bypass_owner_pause(db_session):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    member_user_id = "resident-member-user"
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id=member_user_id,
            role="member",
        )
    )
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as blocked:
        await unwrap(start_project_strategic_resident_session)(
            request=None,
            project_id=project.id,
            payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
            session=db_session,
            current_user={"user_id": member_user_id},
        )

    assert blocked.value.status_code in {403, 409}
    await db_session.refresh(project)
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert tasks == []
    assert read_resident_session(project.vision) is None
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_blank_runner_id_cannot_bypass_paused_start(db_session):
    project = await _make_project(db_session)
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(ValidationError):
        StrategicResidentSessionStart(runner_id="")

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert tasks == []
    assert read_resident_session(project.vision) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enroll_runner", "allow_no_runner"),
    [(True, False), (False, True)],
)
async def test_unpinned_resident_start_remains_blocked_while_paused(
    db_session,
    enroll_runner,
    allow_no_runner,
):
    project = await _make_project(db_session)
    if enroll_runner:
        await _make_runner(db_session, project)
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as blocked:
        await unwrap(start_project_strategic_resident_session)(
            request=None,
            project_id=project.id,
            payload=StrategicResidentSessionStart(allow_no_runner=allow_no_runner),
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

    assert blocked.value.status_code == 409
    assert "paused" in str(blocked.value.detail).lower()
    await db_session.refresh(project)
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert tasks == []
    assert read_resident_session(project.vision) is None
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_pinned_background_ensure_remains_blocked_while_paused(db_session):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as blocked:
        await unwrap(start_project_strategic_resident_session)(
            request=None,
            project_id=project.id,
            payload=StrategicResidentSessionStart(
                runner_id=str(runner.id),
                expected_interaction_mode="continuous",
            ),
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

    assert blocked.value.status_code == 409
    assert "paused" in str(blocked.value.detail).lower()
    await db_session.refresh(project)
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert tasks == []
    assert read_resident_session(project.vision) is None
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_explicit_pinned_start_remains_blocked_when_project_archived(db_session):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    project.archived = True
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as blocked:
        await unwrap(start_project_strategic_resident_session)(
            request=None,
            project_id=project.id,
            payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

    assert blocked.value.status_code == 409
    assert "archived" in str(blocked.value.detail).lower()
    await db_session.refresh(project)
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert tasks == []
    assert read_resident_session(project.vision) is None
    assert resident_session_module.RESIDENT_SESSION_REASON_KEY not in project.vision


@pytest.mark.asyncio
async def test_paused_pinned_start_still_fences_true_active_one_shot(db_session):
    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    project.paused = True
    one_shot = Task(
        project_id=project.id,
        title="Active one-shot strategy",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        started_at=_utcnow(),
        created_by="ORCHESTRATOR",
        context={},
    )
    db_session.add_all([project, one_shot])
    await db_session.commit()
    project_id = project.id
    one_shot_id = one_shot.id
    factory = _test_session_factory(db_session)

    with pytest.raises(HTTPException) as blocked:
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            runner_id=str(runner.id),
            session_factory=factory,
            allow_paused_project=True,
        )

    assert blocked.value.status_code == 409
    assert "cycle" in str(blocked.value.detail).lower()
    async with factory() as verify:
        fresh = await verify.get(Project, project_id)
        tasks = (
            await verify.execute(select(Task).where(Task.project_id == project_id))
        ).scalars().all()
    assert [task.id for task in tasks] == [one_shot_id]
    assert not resident_session_is_active(read_resident_session(fresh.vision))
    reason = fresh.vision[resident_session_module.RESIDENT_SESSION_REASON_KEY]
    assert reason["reason"] == (
        resident_session_module.RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT
    )


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


async def test_start_adopts_task_and_liveness_heartbeat_renews_lease(db_session, monkeypatch):
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
    assert refreshed_task.launched_by_user_id == TEST_USER_ID
    assert refreshed_task.priority == RESIDENT_TASK_PRIORITY

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
    assert datetime.fromisoformat(liveness["leased_until"]) > datetime.fromisoformat(lease)
    refreshed = await db_session.get(Project, project.id)
    marker = refreshed.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["last_renewal_reason"] == "continuous_liveness"


@pytest.mark.asyncio
async def test_repeated_start_uplifts_only_unstarted_canonical_resident(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    runner = await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )
    original_identity = (
        started["session_id"],
        started["task_id"],
        started["worker_generation"],
    )
    task.priority = 50
    db_session.add(task)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    first_ensure = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )
    second_ensure = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )

    await db_session.refresh(task)
    assert task.priority == RESIDENT_TASK_PRIORITY
    assert task.started_at is None
    assert (
        first_ensure["session_id"],
        first_ensure["task_id"],
        first_ensure["worker_generation"],
    ) == original_identity
    assert (
        second_ensure["session_id"],
        second_ensure["task_id"],
        second_ensure["worker_generation"],
    ) == original_identity


def test_priority_uplift_rejects_exact_identity_from_another_project():
    expected_project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    task = Task(
        project_id=other_project_id,
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.PROVISIONING,
        priority=50,
        context={
            "_resident_session": True,
            "strategic_session_id": "ss-cross-project",
            "_resident_worker_generation": 3,
        },
    )

    changed = resident_session_module._uplift_unstarted_resident_task_priority(
        task,
        expected_project_id=expected_project_id,
        session_id="ss-cross-project",
        worker_generation=3,
    )

    assert changed is False
    assert task.priority == 50


@pytest.mark.asyncio
async def test_repeated_start_does_not_uplift_cross_project_vision_pointer(db_session):
    project = await _make_project(db_session)
    resident_task = await _make_task(db_session, project)
    runner = await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )

    other_project = await _make_project(db_session)
    foreign_task = await _make_task(db_session, other_project)
    await db_session.refresh(resident_task)
    foreign_task.context = dict(resident_task.context or {})
    foreign_task.status = TaskStatus.PROVISIONING
    foreign_task.vm_instance_id = f"runner:{runner.id}"
    foreign_task.vm_provisioned_at = _utcnow()
    foreign_task.priority = 50

    await db_session.refresh(project)
    vision = dict(project.vision or {})
    resident = dict(vision[RESIDENT_SESSION_KEY])
    resident["task_id"] = str(foreign_task.id)
    vision[RESIDENT_SESSION_KEY] = resident
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add_all([project, foreign_task])
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    ensured = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )

    await db_session.refresh(foreign_task)
    assert foreign_task.project_id == other_project.id
    assert foreign_task.priority == 50
    assert ensured["session_id"] == started["session_id"]
    assert ensured["task_id"] == str(foreign_task.id)
    assert ensured["worker_generation"] == started["worker_generation"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "started_at"),
    [
        (TaskStatus.IN_PROGRESS, None),
        (TaskStatus.PROVISIONING, _utcnow()),
    ],
)
async def test_repeated_start_does_not_rewrite_started_resident_priority(
    db_session, status, started_at
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    runner = await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )
    task.priority = 50
    task.status = status
    task.started_at = started_at
    task.last_heartbeat = _utcnow()
    db_session.add(task)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    ensured = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )

    await db_session.refresh(task)
    assert task.priority == 50
    assert task.status == status
    assert task.started_at == started_at
    assert ensured["session_id"] == started["session_id"]
    assert ensured["task_id"] == started["task_id"]
    assert ensured["worker_generation"] == started["worker_generation"]


@pytest.mark.asyncio
async def test_poisoned_blocked_reason_write_recovers_before_activation(
    db_session, monkeypatch
):
    """Best-effort reason SQL cannot poison the following activation commit."""

    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    await db_session.refresh(project)
    vision = dict(project.vision or {})
    vision[resident_session_module.RESIDENT_SESSION_REASON_KEY] = {
        "reason": "stale reason",
        "recorded_at": _utcnow().isoformat(),
    }
    project.vision = vision
    flag_modified(project, "vision")
    await db_session.commit()

    async def _poison_reason_write(current_session, *_args, **_kwargs):
        await current_session.execute(
            text("SELECT * FROM __missing_resident_reason_relation__")
        )

    monkeypatch.setattr(
        resident_session_module, "with_vision_locked", _poison_reason_write
    )
    await resident_session_module.set_resident_session_blocked_reason(
        db_session, project, None
    )
    await resident_session_module._enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        snapshot=started,
    )
    activation = await db_session.scalar(
        select(ProjectMessage).where(
            ProjectMessage.idempotency_key
            == f"resident-activation:{started['session_id']}"
        )
    )
    assert activation is not None
    assert activation.context["_resident_activation_prompt"] is True


@pytest.mark.asyncio
async def test_fresh_session_rebinds_only_immediate_predecessor_turn_without_activation_cobatch(
    db_session,
):
    """Seq 1785 is Fast's sole chat input; seq 1784/startup stay excluded."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_session_id = "resident-predecessor-cancelled"
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": predecessor_session_id,
            "task_id": str(uuid.uuid4()),
            "worker_generation": 1,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    old_activity = ProjectMessage(
        project_id=project.id,
        sequence=1783,
        message_type=MessageType.BUG_REPORT,
        surface=MessageSurface.ACTIVITY,
        from_type="user",
        content="old unresolved activity must remain in the broad Resident inbox",
        status="open",
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(minutes=2),
    )
    legacy = ProjectMessage(
        project_id=project.id,
        sequence=1784,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="legacy NULL-session backlog must stay outside the new chat",
        status="open",
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(minutes=1),
    )
    carried = ProjectMessage(
        project_id=project.id,
        sequence=1785,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="what are you working on rn; can you consult smart strategic?",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at + timedelta(minutes=1),
        conversation_claim_id=None,
        conversation_claim_generation=None,
        conversation_claim_incarnation=None,
        conversation_claim_expires_at=None,
        conversation_claim_attempt=11,
        context={},
    )
    db_session.add_all([project, old_activity, legacy, carried])
    flag_modified(project, "vision")
    await db_session.commit()

    # PostgreSQL owns the canonical per-project sequence via its allocation
    # trigger, so explicit fixture values may be replaced at INSERT time.
    # Preserve the intended predecessor ordering while deriving the fresh
    # session boundary from the sequences that were actually persisted.
    await db_session.refresh(old_activity)
    await db_session.refresh(legacy)
    await db_session.refresh(carried)
    persisted_old_activity_sequence = old_activity.sequence
    persisted_legacy_sequence = legacy.sequence
    persisted_carried_sequence = carried.sequence
    assert persisted_old_activity_sequence is not None
    assert persisted_legacy_sequence is not None
    assert persisted_carried_sequence is not None
    assert persisted_legacy_sequence < persisted_carried_sequence
    expected_floor = max(
        persisted_old_activity_sequence,
        persisted_legacy_sequence,
        persisted_carried_sequence,
    ) + 1

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await resident_session_module._enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        snapshot=started,
    )
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(old_activity)
    await db_session.refresh(legacy)
    await db_session.refresh(carried)
    resident = read_resident_session(project.vision)
    floor = resident[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY]

    assert floor == expected_floor
    assert (
        task.context[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY]
        == expected_floor
    )
    assert old_activity.resident_session_id is None
    assert legacy.resident_session_id is None
    assert carried.resident_session_id == started["session_id"]
    current_chat_inputs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.resident_session_id == started["session_id"],
                    ProjectMessage.status == "open",
                    ProjectMessage.from_type == "user",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in current_chat_inputs] == [carried.id]
    assert not any(
        row.context.get("_resident_activation_prompt") is True
        for row in current_chat_inputs
    )
    assert [row["id"] for row in task.first_payload["open_messages"]] == [
        str(old_activity.id),
        str(carried.id)
    ]

    _, work_state = await resident_session_module._compute_resident_work_state(
        db_session,
        project.id,
        resident_session_id=started["session_id"],
        conversation_floor_sequence=floor,
        resident_task_id=task.id,
    )
    # The pre-floor Activity row and carried current input are actionable; the
    # stale pre-floor conversation row is the only one hidden by the boundary.
    assert work_state["new_operator_messages"] == 2

    incarnation = uuid.uuid4()
    stale_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=started["session_id"],
        task_id=task.id,
        worker_generation=started["worker_generation"],
        incarnation_id=incarnation,
        message_ids=[legacy.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=uuid.uuid4(),
        claim_idempotency_key="must-not-claim-legacy-null",
        conversation_floor_sequence=floor,
    )
    assert stale_claim is None

    client_claim_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=started["session_id"],
        task_id=task.id,
        worker_generation=started["worker_generation"],
        incarnation_id=incarnation,
        message_ids=[carried.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=client_claim_id,
        claim_idempotency_key=f"recovered-fast-turn:{client_claim_id}",
        conversation_floor_sequence=floor,
    )
    assert claim is not None
    reply = await complete_claimed_public_turn(
        db_session,
        project_id=project.id,
        claim_id=claim.claim_id,
        task_id=task.id,
        worker_generation=started["worker_generation"],
        incarnation_id=incarnation,
        owner=ConversationPublicReplyOwner.FAST,
        content="I am setting direction with you now; Workday consultation is available.",
        idempotency_key="recovered-predecessor-turn-answer",
        resident_session_id=started["session_id"],
    )
    await db_session.commit()
    await db_session.refresh(carried)

    assert carried.status == "handled"
    assert carried.handling_state == MessageHandlingState.RESPONDED
    assert reply.message.context["_resident_reply_covered_message_ids"] == [
        str(carried.id)
    ]
    assert reply.message.context["_workday_consultation_requested"] is False


@pytest.mark.asyncio
async def test_restart_retires_predecessor_activation_before_enqueuing_new_one(
    db_session,
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_session_id = "resident-with-unanswered-activation"
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": predecessor_session_id,
            "task_id": str(uuid.uuid4()),
            "worker_generation": 1,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    old_activation = ProjectMessage(
        project_id=project.id,
        sequence=500,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="synthetic predecessor startup prompt",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=predecessor_session_id,
        created_at=predecessor_started_at + timedelta(seconds=1),
        context={"_resident_activation_prompt": True},
    )
    db_session.add_all([project, old_activation])
    flag_modified(project, "vision")
    await db_session.commit()

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await resident_session_module._enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        snapshot=started,
    )
    await db_session.refresh(old_activation)

    current_inputs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.resident_session_id == started["session_id"],
                    ProjectMessage.status == "open",
                    ProjectMessage.from_type == "user",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(current_inputs) == 1
    assert current_inputs[0].context["_resident_activation_prompt"] is True
    assert current_inputs[0].idempotency_key == (
        f"resident-activation:{started['session_id']}"
    )
    assert old_activation.resident_session_id is None
    assert old_activation.status == "resolved"
    assert old_activation.handling_state == MessageHandlingState.RESPONDED
    assert old_activation.resolution == "session_handoff_activation_retired"


async def _restart_with_claimed_predecessor_coordination_row(
    db_session,
    *,
    context: dict,
) -> tuple[Project, Task, ProjectMessage, dict, str]:
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_session_id = f"resident-private-predecessor-{uuid.uuid4()}"
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": predecessor_session_id,
            "task_id": str(uuid.uuid4()),
            "worker_generation": 7,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    row = ProjectMessage(
        project_id=project.id,
        sequence=700,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="private Workday coordination survives Resident replacement",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=predecessor_session_id,
        created_at=predecessor_started_at + timedelta(minutes=1),
        context=context,
    )
    db_session.add_all([project, row])
    flag_modified(project, "vision")
    await db_session.commit()

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await db_session.refresh(task)
    await db_session.refresh(row)
    return project, task, row, started, predecessor_session_id


@pytest.mark.asyncio
async def test_restart_rebinds_pending_workday_packet_with_fresh_lease(db_session):
    old_claim_id = uuid.uuid4()
    project, task, packet, started, predecessor_session_id = (
        await _restart_with_claimed_predecessor_coordination_row(
            db_session,
            context={
                "_conversation_consultation_packet": True,
                "audience": "strategist-internal",
                "message_role": "workday_consultation",
                "public_reply_owner": "fast",
                "_workday_consultation_atomic_reply_id": str(uuid.uuid4()),
                "_workday_consultation_claim_id": str(old_claim_id),
                "_workday_consultation_claim_generation": 7,
                "_workday_consultation_claim_incarnation": str(uuid.uuid4()),
                "_workday_consultation_claim_expires_at": (
                    _utcnow() + timedelta(minutes=5)
                ).isoformat(),
                "_workday_consultation_claim_attempt": 1,
                "_workday_consultation_claim_started_at": (
                    _utcnow() - timedelta(minutes=2)
                ).isoformat(),
            },
        )
    )

    assert packet.resident_session_id == started["session_id"]
    assert packet.handling_state == MessageHandlingState.PENDING
    assert packet.handled_by_task_id is None
    assert packet.context[RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY] == (
        predecessor_session_id
    )
    for key in (
        "_workday_consultation_claim_id",
        "_workday_consultation_claim_generation",
        "_workday_consultation_claim_incarnation",
        "_workday_consultation_claim_expires_at",
        "_workday_consultation_claim_started_at",
    ):
        assert key not in packet.context
    assert packet.context["_workday_consultation_claim_attempt"] == 1
    assert len(packet.context["_workday_consultation_attempts"]) == 1
    old_attempt = packet.context["_workday_consultation_attempts"][0]
    assert old_attempt["workday_claim_id"] == str(old_claim_id)
    assert old_attempt["attempt"] == 1
    assert old_attempt["outcome"] == "abandoned"
    assert old_attempt["terminal_predicate"] == (
        "resident_session_handoff_without_ack"
    )
    assert old_attempt["provider_terminal_reason"] is None
    assert old_attempt["provider_output_tail"] is None
    assert old_attempt["trace"][-1]["label"] == "provider_diagnostic_missing"
    assert old_attempt["trace_truncated"] is True
    old_authority = packet.context["_workday_consultation_claim_authorities"][0]
    assert old_authority["workday_claim_id"] == str(old_claim_id)
    assert old_authority["attempt"] == 1
    assert old_authority["diagnostic_state"] == "server_missing"

    new_claim = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=started["worker_generation"],
        worker_incarnation=uuid.uuid4(),
        resident_session_id=started["session_id"],
    )
    assert new_claim is not None
    assert new_claim.message.id == packet.id
    assert new_claim.workday_claim_id != old_claim_id
    assert new_claim.attempt == 2
    await db_session.refresh(packet)
    authorities = packet.context["_workday_consultation_claim_authorities"]
    assert [item["attempt"] for item in authorities] == [1, 2]
    assert [item["diagnostic_state"] for item in authorities] == [
        "server_missing",
        "open",
    ]


@pytest.mark.asyncio
async def test_restart_rebinds_awaiting_workday_continuation_with_fresh_lease(
    db_session,
):
    old_claim_id = uuid.uuid4()
    project, task, result, started, predecessor_session_id = (
        await _restart_with_claimed_predecessor_coordination_row(
            db_session,
            context={
                "_workday_consultation_result": True,
                "audience": "strategist-internal",
                "message_role": "workday_consultation_result",
                "public_reply_owner": "fast",
                "_workday_continuation_claim_id": str(old_claim_id),
                "_workday_continuation_claim_generation": 7,
                "_workday_continuation_claim_incarnation": str(uuid.uuid4()),
                "_workday_continuation_claim_expires_at": (
                    _utcnow() + timedelta(minutes=5)
                ).isoformat(),
                "_workday_continuation_claim_attempt": 3,
            },
        )
    )

    assert result.resident_session_id == started["session_id"]
    assert result.handling_state == MessageHandlingState.PENDING
    assert result.handled_by_task_id is None
    assert result.context[RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY] == (
        predecessor_session_id
    )
    for key in (
        "_workday_continuation_claim_id",
        "_workday_continuation_claim_generation",
        "_workday_continuation_claim_incarnation",
        "_workday_continuation_claim_expires_at",
        "_workday_continuation_claim_attempt",
    ):
        assert key not in result.context

    new_claim = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=started["worker_generation"],
        worker_incarnation=uuid.uuid4(),
        resident_session_id=started["session_id"],
    )
    assert new_claim is not None
    assert new_claim.message.id == result.id
    assert new_claim.continuation_claim_id != old_claim_id
    assert new_claim.attempt == 1


@pytest.mark.asyncio
async def test_restart_cursor_floor_survives_packed_open_message_eviction(
    db_session,
):
    """An omitted ordinary predecessor row remains reachable by initial poll."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_session_id = "resident-before-cursor-floor"
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    carried_cursor_dt = predecessor_started_at + timedelta(minutes=9)
    carried_cursor = (
        carried_cursor_dt.replace(tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": predecessor_session_id,
            "task_id": str(uuid.uuid4()),
            "worker_generation": 2,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(seconds=30)).isoformat(),
            "_message_cursor": carried_cursor,
        }
    }
    old_ordinary = ProjectMessage(
        project_id=project.id,
        sequence=3000,
        message_type=MessageType.NOTE,
        surface=MessageSurface.ACTIVITY,
        from_type="user",
        content="old ordinary predecessor input omitted behind critical rows",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=predecessor_session_id,
        created_at=predecessor_started_at + timedelta(minutes=1),
    )
    newer_criticals = [
        ProjectMessage(
            project_id=project.id,
            sequence=3100 + index,
            message_type=MessageType.BUG_REPORT,
            surface=MessageSurface.ACTIVITY,
            from_type="user",
            content=f"newer critical inbox row {index}",
            status="open",
            handling_state=MessageHandlingState.PENDING,
            resident_session_id=None,
            created_at=(
                predecessor_started_at
                + timedelta(minutes=2, seconds=index)
            ),
        )
        for index in range(41)
    ]
    db_session.add_all([project, old_ordinary, *newer_criticals])
    flag_modified(project, "vision")
    await db_session.commit()

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await db_session.refresh(task)
    await db_session.refresh(old_ordinary)

    oldest_utc = (
        old_ordinary.created_at.replace(tzinfo=timezone.utc)
        if old_ordinary.created_at.tzinfo is None
        else old_ordinary.created_at.astimezone(timezone.utc)
    )
    expected_floor = oldest_utc.isoformat().replace("+00:00", "Z")
    packed_ids = {row["id"] for row in task.first_payload["open_messages"]}

    assert old_ordinary.resident_session_id is None
    assert task.context["strategic_session_id"] == started["session_id"]
    assert task.context["message_cursor"] == carried_cursor
    assert task.context[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY] == expected_floor
    assert (
        task.first_payload[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY]
        == expected_floor
    )
    assert expected_floor.endswith("Z")
    assert len(task.first_payload["open_messages"]) == 40
    assert str(old_ordinary.id) not in packed_ids
    assert packed_ids.issubset({str(row.id) for row in newer_criticals})


@pytest.mark.asyncio
async def test_fresh_session_does_not_rebind_marked_rows_from_older_sessions(
    db_session,
):
    """Only the immediate predecessor marker crosses the new session boundary."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_session_id = "resident-immediate-predecessor"
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": predecessor_session_id,
            "task_id": str(uuid.uuid4()),
            "worker_generation": 3,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    older_one = ProjectMessage(
        project_id=project.id,
        sequence=200,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="marked carry from an older, unrelated session",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(hours=2),
        context={RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY: "resident-old-one"},
    )
    older_two = ProjectMessage(
        project_id=project.id,
        sequence=201,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="marked carry from another old session",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(hours=1),
        context={RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY: "resident-old-two"},
    )
    immediate = ProjectMessage(
        project_id=project.id,
        sequence=202,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="explicit carry from the immediate predecessor",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at + timedelta(minutes=1),
        context={
            RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY: predecessor_session_id
        },
    )
    db_session.add_all([project, older_one, older_two, immediate])
    flag_modified(project, "vision")
    await db_session.commit()

    # PostgreSQL owns the canonical per-project sequence via its allocation
    # trigger, so explicit fixture values may be replaced at INSERT time.
    # Derive the new-session floor from the persisted immediate-predecessor row
    # while retaining the ordering and old-session exclusion assertions below.
    await db_session.refresh(older_one)
    await db_session.refresh(older_two)
    await db_session.refresh(immediate)
    persisted_older_one_sequence = older_one.sequence
    persisted_older_two_sequence = older_two.sequence
    persisted_immediate_sequence = immediate.sequence
    assert persisted_older_one_sequence is not None
    assert persisted_older_two_sequence is not None
    assert persisted_immediate_sequence is not None
    assert (
        persisted_older_one_sequence
        < persisted_older_two_sequence
        < persisted_immediate_sequence
    )
    expected_floor = persisted_immediate_sequence + 1

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(older_one)
    await db_session.refresh(older_two)
    await db_session.refresh(immediate)

    assert read_resident_session(project.vision)[
        RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY
    ] == expected_floor
    assert older_one.resident_session_id is None
    assert older_two.resident_session_id is None
    assert immediate.resident_session_id == started["session_id"]
    assert [row["id"] for row in task.first_payload["open_messages"]] == [
        str(immediate.id)
    ]


@pytest.mark.asyncio
async def test_fresh_session_severs_shared_predecessor_claim_before_narrow_rebind(
    db_session,
):
    """A stale below-floor claim member cannot strand the carried current row."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "resident-predecessor-shared-turn",
            "task_id": str(uuid.uuid4()),
            "worker_generation": 1,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    shared_claim_id = uuid.uuid4()
    shared_context = {
        "_conversation_public_reply_owner": "fast",
        "_conversation_claim_idempotency_key": "old-broad-fast-turn",
    }
    legacy_member = ProjectMessage(
        project_id=project.id,
        sequence=1784,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="stale pre-session member of the broad old claim",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(minutes=1),
        conversation_claim_id=shared_claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=uuid.uuid4(),
        conversation_claim_expires_at=_utcnow() - timedelta(minutes=1),
        conversation_claim_attempt=1,
        context=dict(shared_context),
    )
    carried_member = ProjectMessage(
        project_id=project.id,
        sequence=1785,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="current predecessor input that must form a fresh exact turn",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at + timedelta(minutes=1),
        conversation_claim_id=shared_claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=uuid.uuid4(),
        conversation_claim_expires_at=_utcnow() - timedelta(minutes=1),
        conversation_claim_attempt=1,
        context=dict(shared_context),
    )
    db_session.add_all([project, legacy_member, carried_member])
    flag_modified(project, "vision")
    await db_session.commit()

    await db_session.refresh(legacy_member)
    await db_session.refresh(carried_member)
    persisted_legacy_sequence = legacy_member.sequence
    persisted_carried_sequence = carried_member.sequence
    assert persisted_legacy_sequence is not None
    assert persisted_carried_sequence is not None
    assert persisted_legacy_sequence < persisted_carried_sequence
    expected_floor = persisted_carried_sequence + 1

    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(legacy_member)
    await db_session.refresh(carried_member)
    floor = read_resident_session(project.vision)[
        RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY
    ]

    assert floor == expected_floor
    assert legacy_member.resident_session_id is None
    assert legacy_member.conversation_claim_id == shared_claim_id
    assert legacy_member.context["_conversation_public_reply_owner"] == "fast"
    assert carried_member.resident_session_id == started["session_id"]
    assert carried_member.conversation_claim_id is None
    assert carried_member.conversation_claim_generation is None
    assert carried_member.conversation_claim_incarnation is None
    assert carried_member.conversation_claim_expires_at is None
    assert "_conversation_public_reply_owner" not in carried_member.context
    assert "_conversation_claim_idempotency_key" not in carried_member.context

    incarnation = uuid.uuid4()
    fresh_claim_id = uuid.uuid4()
    fresh_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=started["session_id"],
        task_id=task.id,
        worker_generation=started["worker_generation"],
        incarnation_id=incarnation,
        message_ids=[carried_member.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=fresh_claim_id,
        claim_idempotency_key=f"fresh-narrow-turn:{fresh_claim_id}",
        conversation_floor_sequence=floor,
    )

    assert fresh_claim is not None
    assert fresh_claim.claim_id == fresh_claim_id
    assert [row.id for row in fresh_claim.rows] == [carried_member.id]


@pytest.mark.asyncio
async def test_failed_provisional_start_handoff_is_recovered_by_next_start(
    db_session,
    monkeypatch,
):
    """A start failure cannot strand input already rebound to its claim."""

    project = await _make_project(db_session)
    runner = await _make_runner(db_session, project)
    runner_id = str(runner.id)
    predecessor_started_at = _utcnow() - timedelta(minutes=10)
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "resident-before-failed-start",
            "task_id": str(uuid.uuid4()),
            "worker_generation": 1,
            "status": "ended",
            "stop_requested": True,
            "started_at": predecessor_started_at.isoformat(),
            "ended_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
        }
    }
    stale = ProjectMessage(
        project_id=project.id,
        sequence=400,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="old stale chat must remain excluded after retry",
        status="open",
        resident_session_id=None,
        created_at=predecessor_started_at - timedelta(minutes=1),
    )
    pending = ProjectMessage(
        project_id=project.id,
        sequence=401,
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="immediate predecessor input must survive failed start",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=None,
        created_at=predecessor_started_at + timedelta(minutes=1),
    )
    db_session.add_all([project, stale, pending])
    flag_modified(project, "vision")
    await db_session.commit()

    original_spawn = resident_session_module._spawn_resident_strategic_task

    async def _fail_before_task(*args, **kwargs):
        raise RuntimeError("forced provisional spawn failure")

    monkeypatch.setattr(
        resident_session_module,
        "_spawn_resident_strategic_task",
        _fail_before_task,
    )
    with pytest.raises(RuntimeError, match="forced provisional spawn failure"):
        await start_resident_session(
            db_session,
            project,
            operator_user_id=TEST_USER_ID,
            runner_id=runner_id,
            session_factory=_test_session_factory(db_session),
        )

    await db_session.refresh(project)
    await db_session.refresh(stale)
    await db_session.refresh(pending)
    failed = read_resident_session(project.vision)
    failed_session_id = failed["session_id"]
    assert failed["status"] == "ended"
    assert failed["worker_state"] == "restart_failed"
    assert failed["ended_at"]
    assert project.vision[resident_session_module.RESIDENT_SESSION_REASON_KEY][
        "reason"
    ].startswith("start_failed: forced provisional spawn failure")
    assert stale.resident_session_id is None
    assert pending.resident_session_id is None
    assert pending.context[RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY] == (
        failed_session_id
    )

    monkeypatch.setattr(
        resident_session_module,
        "_spawn_resident_strategic_task",
        original_spawn,
    )
    task = await _make_task(db_session, project)
    project = await db_session.get(Project, project.id)
    recovered = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=runner_id,
        session_factory=_test_session_factory(db_session),
    )
    await db_session.refresh(stale)
    await db_session.refresh(pending)
    await db_session.refresh(task)

    assert recovered["session_id"] != failed_session_id
    assert stale.resident_session_id is None
    assert pending.resident_session_id == recovered["session_id"]
    assert [row["id"] for row in task.first_payload["open_messages"]] == [
        str(pending.id)
    ]


@pytest.mark.asyncio
async def test_member_started_resident_spawn_persists_launcher(db_session, monkeypatch):
    """A fresh Workday task uses owner resources but retains its member launcher."""
    monkeypatch.setenv("CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1")
    project = await _make_project(db_session)
    member_user_id = "resident-member-launcher"
    db_session.add(
        ProjectMember(project_id=project.id, user_id=member_user_id, role="member")
    )
    await db_session.commit()
    runner = await _make_runner(db_session, project)

    started = await unwrap(start_project_strategic_resident_session)(
        request=None,
        project_id=project.id,
        payload=StrategicResidentSessionStart(runner_id=str(runner.id)),
        session=db_session,
        current_user={"user_id": member_user_id},
    )

    task = await db_session.get(Task, uuid.UUID(started["session"]["task_id"]))
    assert task is not None
    assert task.launched_by_user_id == member_user_id
    assert task.context["_resident_session"] is True
    assert task.priority == RESIDENT_TASK_PRIORITY


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
    owner_blob = read_resident_session(project.vision)
    owner_heartbeat = owner_blob["worker_heartbeat_at"]
    owner_expiry = owner_blob["worker_incarnation_expires_at"]

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
    persisted_after_competitor = read_resident_session(project.vision)
    assert persisted_after_competitor["worker_incarnation_id"] == str(first_incarnation)
    assert persisted_after_competitor["worker_incarnation_expires_at"] == owner_expiry
    assert persisted_after_competitor["worker_heartbeat_at"] == owner_heartbeat

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
async def test_heartbeat_persists_authority_pause_before_returning_false(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    competing = Task(
        project_id=project.id,
        title="Foreground strategic cycle",
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(competing)
    await db_session.commit()
    await db_session.refresh(project)

    response = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        activity={
            "phase": "waiting",
            "last_action": "Waiting",
            "reason_code": None,
            "consultation_id": None,
            "autonomy_state": "active",
            "autonomy_reason_code": None,
        },
    )

    assert response["authority_held"] is False
    await db_session.refresh(project)
    persisted = read_resident_session(project.vision)["activity"]
    assert persisted["autonomy_state"] == "paused"
    assert persisted["autonomy_reason_code"] == "authority_not_held"


@pytest.mark.asyncio
async def test_heartbeat_rearms_stale_server_owned_pause_before_returning_true(
    db_session,
):
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    for stale_reason in ("authority_not_held", "foreground_call"):
        response = await renew_resident_session_lease(
            db_session,
            project,
            session_id=started["session_id"],
            task_id=started["task_id"],
            progress=False,
            activity={
                "phase": "waiting",
                "last_action": "Waiting",
                "reason_code": None,
                "consultation_id": None,
                "autonomy_state": "paused",
                "autonomy_reason_code": stale_reason,
            },
        )

        assert response["authority_held"] is True
        await db_session.refresh(project)
        persisted = read_resident_session(project.vision)["activity"]
        assert persisted["autonomy_state"] == "active"
        assert persisted["autonomy_reason_code"] is None


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
async def test_heartbeat_samples_committed_chat_mode_and_fails_closed(db_session):
    """A committed Chat preference wins even before best-effort Stop runs."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    snapshot = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )

    project = await db_session.get(Project, project.id)
    fenced = await renew_resident_session_lease(
        db_session,
        project,
        session_id=snapshot["session_id"],
        task_id=snapshot["task_id"],
        progress=True,
    )

    assert fenced["authority_held"] is False
    assert fenced["status"] == "ended"
    assert fenced["stop_requested"] is True
    refreshed = await db_session.get(Project, project.id)
    resident = read_resident_session(refreshed.vision)
    assert resident["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert task.context["failure_category"] == "ADMIN_CANCEL"


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
async def test_terminal_worker_restart_preserves_checkpoint_and_single_generation(
    db_session,
):
    """A terminal backing task restarts from the persisted Resident authority."""
    project = await _make_project(db_session)
    await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    old_task = await db_session.get(Task, uuid.UUID(started["task_id"]))
    assert old_task is not None
    old_task.status = TaskStatus.FAILED

    checkpoint = {
        "_exam_ledger": '{"tick":179,"records":[]}',
        "_message_cursor": "2026-08-28T14:03:01.801065+00:00",
        "_session_summary": "Keep the corrected v54 two-task dependency packet.",
        "_decisions_reg": '{"entries":[{"atTick":178}]}',
    }
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob.update(checkpoint)
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
    replacement = await db_session.get(Task, uuid.UUID(replaced["task_id"]))
    assert replacement is not None
    assert replacement.context["_exam_ledger"] == checkpoint["_exam_ledger"]
    assert replacement.context["message_cursor"] == checkpoint["_message_cursor"]
    assert replacement.context["_session_summary"] == checkpoint["_session_summary"]
    assert replacement.context["_decisions_reg"] == checkpoint["_decisions_reg"]

    await db_session.refresh(project)
    replacement_blob = project.vision[RESIDENT_SESSION_KEY]
    for key, value in checkpoint.items():
        assert replacement_blob[key] == value

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    live_residents = [
        row
        for row in tasks
        if (row.context or {}).get("_resident_session") is True
        and row.status
        in {
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        }
    ]
    assert [str(row.id) for row in live_residents] == [replaced["task_id"]]


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
    _assert_resident_terminal_receipt(
        emit,
        cycle,
        "resident_operator_stop",
        expect_completion=False,
    )


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
        terminal_emit,
        cycle,
        "resident_operator_stop",
        expect_completion=False,
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
        # Adopted tasks are finalized into the session blob but never assigned
        # to start_resident_session's spawned_task_id local. Model an unknown
        # finalize-commit outcome: compensation must recover this exact task
        # from the identity-guarded blob and leave no live old-generation task.
        None,
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
    failed_session = read_resident_session(refreshed_project.vision)
    assert failed_session["session_id"] == session_id
    assert failed_session["worker_generation"] == 1
    assert failed_session["status"] == "ended"
    assert failed_session["worker_state"] == "restart_failed"
    assert failed_session["ended_at"]
    assert refreshed_project.vision[
        resident_session_module.RESIDENT_SESSION_REASON_KEY
    ]["reason"].startswith("start_failed:")
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
async def test_stop_survives_poisoned_handoff_statement(db_session, monkeypatch):
    """A real SQL handoff failure rolls back its SAVEPOINT, not the stop."""

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
        content="Keep the stop durable when handoff SQL fails.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()

    import app.session_message_handoff as handoff_module

    real_apply_handoff_mutations = handoff_module._apply_handoff_mutations

    async def _poison_handoff(current_session, *_args, **_kwargs):
        # PostgreSQL marks the current transaction failed after this statement;
        # SQLite raises the same SQLAlchemy DB error. The production SAVEPOINT
        # must restore a usable outer transaction on both engines.
        await current_session.execute(
            text("SELECT * FROM __missing_resident_handoff_relation__")
        )

    monkeypatch.setattr(
        handoff_module, "_apply_handoff_mutations", _poison_handoff
    )

    project = await db_session.get(Project, project.id)
    snapshot = await stop_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )

    assert snapshot["status"] == "ended"
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(pending)
    assert read_resident_session(project.vision)["status"] == "ended"
    assert task.status == TaskStatus.CANCELLED
    # ``begin_nested`` flushes the staged Task cancellation before opening the
    # SAVEPOINT. PostgreSQL's ProjectMessage/lease consistency hook therefore
    # returns the message to pending while preserving its terminal producer as
    # provenance. The failed handoff can leave that owner and the exact stale
    # Resident session id attached; the repair sweep owns their convergence.
    assert pending.resident_session_id == started["session_id"]
    assert pending.handled_by_task_id == task.id
    if resolve_dialect_name(db_session) == "postgresql":
        assert pending.handling_state == MessageHandlingState.PENDING
    else:
        # The lightweight create_all SQLite fixture does not install the
        # PostgreSQL ProjectMessage consistency trigger. It still proves that
        # the failed handoff rolled back without losing the outer stop.
        assert pending.handling_state == MessageHandlingState.CLAIMED

    monkeypatch.setattr(
        handoff_module, "_apply_handoff_mutations", real_apply_handoff_mutations
    )
    assert (
        await repair_stale_session_message_handoffs(db_session, project.id) == 1
    )
    await db_session.refresh(pending)
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_operator_silence_preserves_claimed_message_and_resident_task(
    db_session, monkeypatch
):
    """A healthy quiet heartbeat must not manufacture a terminal handoff."""

    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    pending = ProjectMessage(
        project_id=project.id,
        sequence=501,
        message_type=MessageType.FEEDBACK,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Keep this claimed turn while quiet Continuous Workday stays live.",
        created_at=_utcnow() - timedelta(hours=2),
        status="open",
        handling_state=MessageHandlingState.PENDING,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    incarnation = legacy_resident_worker_incarnation(
        task.id, started["worker_generation"]
    )
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=started["session_id"],
        task_id=task.id,
        worker_generation=started["worker_generation"],
        incarnation_id=incarnation,
        message_ids=[pending.id],
        lease_seconds=45,
        canonical_conversation_only=True,
    )
    assert claim is not None
    await db_session.commit()
    # The retired setting remains intentionally inert: operator silence is not
    # process death and cannot end an exact live Continuous worker.
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
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert renewed["authority_held"] is True
    assert renewed["stop_requested"] is False
    assert renewed["status"] == "active"
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert task.post_commit_pending is not True
    await db_session.refresh(pending)
    assert pending.status == "open"
    assert pending.resident_session_id == started["session_id"]
    assert pending.handling_state == MessageHandlingState.CLAIMED
    assert pending.handled_by_task_id == task.id


@pytest.mark.asyncio
async def test_committed_chat_mode_handoff_is_atomic_with_mode_end(
    db_session, monkeypatch
):
    """Message release must commit atomically with the Chat-mode end.

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
        content="Will this survive rearm failure after leaving Workday?",
        created_at=_utcnow() - timedelta(hours=2),
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    # Simulate rearm raising after commit — message mutations must be durable.
    import app.session_message_handoff as handoff_module

    async def _raise_on_rearm(*args, **kwargs):
        raise RuntimeError("rearm failed — simulating post-mode-change crash")

    monkeypatch.setattr(handoff_module, "_run_handoff_rearm", _raise_on_rearm)

    # Model the real race: Chat is already committed, while the separate
    # best-effort Stop side effect has not run yet.
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )

    project = await db_session.get(Project, project.id)
    ended = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert ended["authority_held"] is False
    assert ended["stop_requested"] is True
    assert ended["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert task.context["failure_category"] == "ADMIN_CANCEL"
    # Message must be released even though rearm raised
    await db_session.refresh(pending)
    assert pending.resident_session_id is None, (
        "message.resident_session_id must be None: mode-change handoff must "
        "commit atomically with the session end"
    )
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_committed_chat_mode_end_survives_poisoned_handoff_statement(
    db_session, monkeypatch
):
    """A failed handoff query cannot roll back the heartbeat's Chat-mode end."""

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
        content="Keep the mode end durable when handoff SQL fails.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()

    import app.session_message_handoff as handoff_module

    real_apply_handoff_mutations = handoff_module._apply_handoff_mutations

    async def _poison_handoff(current_session, *_args, **_kwargs):
        await current_session.execute(
            text("SELECT * FROM __missing_resident_mode_handoff_relation__")
        )

    monkeypatch.setattr(
        handoff_module, "_apply_handoff_mutations", _poison_handoff
    )
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )

    project = await db_session.get(Project, project.id)
    ended = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert ended["authority_held"] is False
    assert ended["stop_requested"] is True
    assert ended["status"] == "ended"
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(pending)
    assert read_resident_session(project.vision)["status"] == "ended"
    assert task.status == TaskStatus.CANCELLED
    # As in explicit Stop, the pre-SAVEPOINT flush terminalizes the Task.
    # PostgreSQL returns the row to pending while preserving the terminal task
    # as provenance until the bounded handoff-repair sweep clears both bindings.
    assert pending.resident_session_id == started["session_id"]
    assert pending.handled_by_task_id == task.id
    if resolve_dialect_name(db_session) == "postgresql":
        assert pending.handling_state == MessageHandlingState.PENDING
    else:
        assert pending.handling_state == MessageHandlingState.CLAIMED

    monkeypatch.setattr(
        handoff_module, "_apply_handoff_mutations", real_apply_handoff_mutations
    )
    assert (
        await repair_stale_session_message_handoffs(db_session, project.id) == 1
    )
    await db_session.refresh(pending)
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_stale_marker_exact_heartbeat_renews_without_terminalizing_task(
    db_session, monkeypatch
):
    """A stale marker is re-anchored by the exact live Continuous process."""
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    # Model a post-claim session so this exercises ordinary Continuous liveness,
    # not first-heartbeat compatibility.
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["first_agent_heartbeat_at"] = (_utcnow() - timedelta(minutes=6)).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )

    assert renewed["authority_held"] is True
    assert renewed["stop_requested"] is False
    assert renewed["status"] == "active"
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert task.post_commit_pending is not True
    refreshed = await db_session.get(Project, project.id)
    marker = refreshed.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["last_renewal_reason"] == "continuous_liveness"
    assert _utcnow() - datetime.fromisoformat(marker["last_seen_at"]) < timedelta(
        minutes=1
    )


@pytest.mark.asyncio
async def test_autonomous_working_turn_renews_freshness_without_mutation(
    db_session, monkeypatch
):
    """An autonomous review records examination work without faking mutation.

    A completed autonomous working turn (``autonomous_turn=True``) that applied
    NO strategic op still renews the continuation lease with the distinct
    ``autonomous_turn`` reason: examination IS work, but it is not mutation
    progress. Quiet exact heartbeats also renew Continuous liveness; process
    reaping, incarnation fencing, and the agent-side consecutive-stall detector
    remain the stuck-worker backstops.
    """
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    # Age the legacy marker so this proves the exact autonomous heartbeat
    # re-anchors it with the autonomous-turn reason.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    # Model a POST-CLAIM session so the renewal is classified as autonomous work,
    # not the one-time first-agent-heartbeat compatibility reason.
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["first_agent_heartbeat_at"] = (_utcnow() - timedelta(minutes=6)).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        autonomous_turn=True,
    )

    assert renewed["authority_held"] is True
    assert not renewed["stop_requested"]
    assert renewed["status"] != "ended"
    # The session was NOT ended: the backing resident task is not terminalized
    # and no terminalization is queued.
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert task.post_commit_pending is not True

    # Freshness advanced to now with the autonomous renewal reason, and it is NOT
    # accounted as strategic progress (last_progress_at stays None).
    refreshed = await db_session.get(Project, project.id)
    fresh_marker = dict(refreshed.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert fresh_marker["last_renewal_reason"] == "autonomous_turn"
    seen_at = datetime.fromisoformat(fresh_marker["last_seen_at"])
    assert (_utcnow() - seen_at) < timedelta(minutes=1)
    blob = read_resident_session(dict(refreshed.vision or {}))
    assert blob["last_progress_at"] is None


@pytest.mark.asyncio
async def test_autonomous_turn_flag_cannot_rescue_committed_chat_mode(
    db_session, monkeypatch
):
    """Even an asserted autonomous turn cannot renew after Chat commits."""
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["first_agent_heartbeat_at"] = (_utcnow() - timedelta(minutes=6)).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )

    project = await db_session.get(Project, project.id)
    fenced = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        autonomous_turn=True,
    )

    assert fenced["authority_held"] is False
    assert fenced["stop_requested"] is True
    assert fenced["status"] == "ended"
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert task.latest_status_message == (
        "Resident strategic session stopped after Workday left Continuous mode"
    )


@pytest.mark.asyncio
async def test_first_agent_heartbeat_reanchors_born_stale_session(
    db_session, monkeypatch
):
    """Arm-at-claim: the FIRST agent heartbeat re-anchors a born-stale marker.

    The continuation marker is armed at session START; long queue/provision
    latency alone can drive it past the TTL before the agent runs a single turn.
    The agent's FIRST heartbeat must re-anchor freshness to NOW and NOT lapse the
    session — provisioning time is not charged against the work window. This is
    the bc2ebf44 repro (a session that carried a real ledger yet ran ~3s before
    the born-stale marker terminalized it), now fixed.
    """
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    # Drive the marker past the TTL to emulate long provisioning BEFORE the agent
    # sent any heartbeat. Crucially the blob has NO first_agent_heartbeat_at, so
    # the heartbeat below is the session's FIRST agent contact (the claim beat).
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

    assert read_resident_session(dict(vision)).get("first_agent_heartbeat_at") is None

    project = await db_session.get(Project, project.id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )

    assert renewed["authority_held"] is True
    assert not renewed["stop_requested"]
    assert renewed["status"] != "ended"
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert task.post_commit_pending is not True

    # Freshness re-anchored to now with the compatibility claim-beat reason; all
    # later exact heartbeats use the ordinary Continuous-liveness reason.
    refreshed = await db_session.get(Project, project.id)
    fresh_marker = dict(refreshed.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert fresh_marker["last_renewal_reason"] == "first_agent_heartbeat"
    seen_at = datetime.fromisoformat(fresh_marker["last_seen_at"])
    assert (_utcnow() - seen_at) < timedelta(minutes=1)
    blob = read_resident_session(dict(refreshed.vision or {}))
    assert blob["first_agent_heartbeat_at"] is not None
    # The claim beat re-anchors freshness but reports NO strategic progress.
    assert blob["last_progress_at"] is None


@pytest.mark.asyncio
async def test_post_claim_quiet_heartbeat_renews_continuous_liveness(
    db_session, monkeypatch
):
    """After the claim beat, exact process liveness re-anchors a stale marker."""
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)

    # Claim beat already happened, then the legacy marker aged past its old TTL.
    project = await db_session.get(Project, project.id)
    vision = dict(project.vision or {})
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["first_agent_heartbeat_at"] = (_utcnow() - timedelta(minutes=6)).isoformat()
    vision[RESIDENT_SESSION_KEY] = blob
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )

    assert renewed["authority_held"] is True
    assert renewed["stop_requested"] is False
    assert renewed["status"] == "active"
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    refreshed = await db_session.get(Project, project.id)
    marker = refreshed.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["last_renewal_reason"] == "continuous_liveness"


@pytest.mark.asyncio
async def test_autonomous_turn_survives_operator_silence_with_exact_heartbeat(
    db_session, monkeypatch
):
    """A live autonomous turn is not terminalized merely for operator silence."""
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    # The retired cap setting must be inert even for autonomous heartbeats.
    monkeypatch.setitem(runtime_config, STRATEGIC_SESSION_MAX_UNATTENDED_SETTING, 60)
    monkeypatch.setitem(
        runtime_config, "strategic_continuation_ttl_seconds", 86_400
    )

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
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        autonomous_turn=True,
    )

    assert renewed["authority_held"] is True
    assert renewed["stop_requested"] is False
    assert renewed["status"] == "active"
    await db_session.refresh(task)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    refreshed = await db_session.get(Project, project.id)
    marker = refreshed.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["last_renewal_reason"] == "autonomous_turn"


@pytest.mark.asyncio
async def test_chat_mode_end_never_terminalizes_cross_project_resident_pointer(
    db_session,
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
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )

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
    assert unanswered.conversation_claim_id is None
    assert unanswered.conversation_claim_generation is None
    assert unanswered.conversation_claim_incarnation is None
    assert unanswered.conversation_claim_expires_at is None
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
