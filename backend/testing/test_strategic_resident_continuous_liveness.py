"""Focused authority and lifetime tests for a Continuous Workday Resident."""

import asyncio
from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

import app.strategic_resident_session as resident_session_module
from app.database import resolve_async_engine
from app.models import (
    LocalRunner,
    MessageHandlingState,
    MessageType,
    MessagesNoisePrefs,
    Project,
    ProjectMember,
    ProjectMessage,
    Task,
    TaskStatus,
    _utcnow,
)
from app.reaper_orphan_classify import _classify_resident_session
from app.shared_state import runtime_config
from app.strategic_resident_session import (
    RESIDENT_SESSION_KEY,
    read_resident_session,
    renew_resident_session_lease,
    resident_session_snapshot,
    start_resident_session,
    stop_resident_session,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    WORKBENCH_STATE_KEY,
    persist_strategic_workbench_update,
)


TEST_USER_ID = "continuous-liveness-owner"


def _session_factory(session):
    return async_sessionmaker(
        resolve_async_engine(session),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _start_continuous(db_session):
    project = Project(name="Continuous liveness", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Resident strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    runner = LocalRunner(
        project_id=project.id,
        token_hash=uuid.uuid4().hex * 2,
        status="active",
        capabilities={"task_types": "all", "max_concurrent": 2},
        last_seen_at=_utcnow(),
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=TEST_USER_ID,
                role="owner",
            ),
            task,
            runner,
        ]
    )
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(runner)

    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project.id,
        {
            "interaction_mode": "continuous",
            "preferred_runner_id": str(runner.id),
        },
        arm_interaction_mode=False,
    )
    await db_session.refresh(project)
    started = await start_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
        runner_id=str(runner.id),
    )
    assert started["task_id"] == str(task.id)
    await db_session.refresh(task)
    assert task.context["_resident_session"] is True
    assert task.context["_resident_infrastructure"] is True
    return project.id, task.id, started


async def _age_live_session(
    db_session,
    project_id,
    *,
    operator_quiet_for: timedelta = timedelta(days=2),
    marker_stale_for: timedelta = timedelta(minutes=30),
):
    project = await db_session.get(Project, project_id)
    assert project is not None
    now = _utcnow()
    vision = dict(project.vision or {})
    resident = dict(vision[RESIDENT_SESSION_KEY])
    resident["started_at"] = (now - operator_quiet_for).isoformat()
    resident["last_operator_interaction_at"] = (now - operator_quiet_for).isoformat()
    resident["first_agent_heartbeat_at"] = (
        now - marker_stale_for - timedelta(minutes=1)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = resident
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (now - marker_stale_for).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    project.vision = vision
    flag_modified(project, "vision")
    prefs = await db_session.scalar(
        select(MessagesNoisePrefs).where(
            MessagesNoisePrefs.project_id == project_id,
            MessagesNoisePrefs.user_id == TEST_USER_ID,
        )
    )
    assert prefs is not None
    raw_prefs = dict(prefs.prefs or {})
    workbench = dict(raw_prefs[WORKBENCH_STATE_KEY])
    workbench["session_started_at"] = resident["started_at"]
    workbench["last_interaction_at"] = resident["last_operator_interaction_at"]
    raw_prefs[WORKBENCH_STATE_KEY] = workbench
    prefs.prefs = raw_prefs
    flag_modified(prefs, "prefs")
    db_session.add_all([project, prefs])
    await db_session.commit()


@pytest.mark.asyncio
async def test_quiet_continuous_liveness_renews_past_ttl_cap_and_daily_interval(
    db_session,
    monkeypatch,
):
    project_id, task_id, started = await _start_continuous(db_session)
    monkeypatch.setitem(runtime_config, "strategic_continuation_ttl_seconds", 60)
    # This retired setting must not turn operator silence into a terminal event.
    monkeypatch.setitem(runtime_config, "strategic_session_max_unattended_seconds", 1)
    await _age_live_session(db_session, project_id)

    project = await db_session.get(Project, project_id)
    renewed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
        autonomous_turn=False,
    )

    assert renewed["authority_held"] is True
    assert renewed["stop_requested"] is False
    assert renewed["status"] == "active"
    await db_session.refresh(project)
    resident = read_resident_session(project.vision)
    marker = project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert resident["status"] == "active"
    assert resident["last_progress_at"] is None
    assert marker["last_renewal_reason"] == "continuous_liveness"
    assert _utcnow() - datetime.fromisoformat(marker["last_seen_at"]) < timedelta(
        minutes=1
    )
    task = await db_session.get(Task, task_id)
    assert task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert task.post_commit_pending is not True


def test_activity_snapshot_does_not_report_operator_silence_as_stopping():
    now = _utcnow()
    snapshot = resident_session_snapshot(
        {
            RESIDENT_SESSION_KEY: {
                "session_id": "quiet-continuous",
                "status": "active",
                "stop_requested": False,
                "started_at": (now - timedelta(days=2)).isoformat(),
                "last_operator_interaction_at": (now - timedelta(days=2)).isoformat(),
                "activity": {
                    "phase": "waiting",
                    "last_action": "Watching project movement",
                },
            }
        },
        now=now,
    )

    assert snapshot["activity"]["phase"] == "waiting"
    assert snapshot["activity"]["idle_remaining_seconds"] is None


@pytest.mark.asyncio
async def test_competing_strategic_task_pauses_then_restores_continuous_authority(
    db_session,
):
    project_id, _, started = await _start_continuous(db_session)
    await _age_live_session(db_session, project_id)
    competing = Task(
        project_id=project_id,
        title="Foreground strategic cycle",
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(competing)
    await db_session.commit()

    project = await db_session.get(Project, project_id)
    paused = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )
    assert paused["authority_held"] is False
    assert paused["stop_requested"] is False
    await db_session.refresh(project)
    assert read_resident_session(project.vision)["status"] == "active"
    assert (
        project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]["last_renewal_reason"]
        == "continuous_liveness"
    )

    competing.status = TaskStatus.COMPLETED
    db_session.add(competing)
    await db_session.commit()
    project = await db_session.get(Project, project_id)
    resumed = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=False,
    )
    assert resumed["authority_held"] is True
    assert resumed["stop_requested"] is False


@pytest.mark.asyncio
async def test_committed_chat_mode_fences_heartbeat_before_best_effort_stop(
    db_session,
):
    project_id, task_id, started = await _start_continuous(db_session)
    pending = ProjectMessage(
        project_id=project_id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Do not strand this turn during the Chat-mode race.",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task_id,
        resident_session_id=started["session_id"],
    )
    db_session.add(pending)
    await db_session.commit()
    # This is the real mode-change gap: Chat is durable, while the separate
    # best-effort stop has not run yet and the old Resident blob is still active.
    await persist_strategic_workbench_update(
        db_session,
        TEST_USER_ID,
        project_id,
        {"interaction_mode": "chat"},
        arm_interaction_mode=False,
    )
    project = await db_session.get(Project, project_id)
    assert read_resident_session(project.vision)["status"] == "active"

    fenced = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert fenced["authority_held"] is False
    assert fenced["stop_requested"] is True
    assert fenced["status"] == "ended"
    await db_session.refresh(project)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in project.vision
    assert read_resident_session(project.vision)["status"] == "ended"
    task = await db_session.get(Task, task_id)
    assert task.status == TaskStatus.CANCELLED
    assert task.context["failure_category"] == "ADMIN_CANCEL"
    await db_session.refresh(pending)
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_heartbeat_first_then_explicit_stop_linearizes_without_rearming(
    db_session,
    monkeypatch,
):
    if resolve_async_engine(db_session).dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    project_id, _, started = await _start_continuous(db_session)
    factory = _session_factory(db_session)
    prefs_locked = asyncio.Event()
    resume_heartbeat = asyncio.Event()
    original_get = resident_session_module.get_strategic_workbench_state

    async def pause_after_canonical_pref_read(*args, **kwargs):
        assert kwargs["for_update"] is True
        assert kwargs["populate_existing"] is True
        workbench = await original_get(*args, **kwargs)
        prefs_locked.set()
        await resume_heartbeat.wait()
        return workbench

    monkeypatch.setattr(
        resident_session_module,
        "get_strategic_workbench_state",
        pause_after_canonical_pref_read,
    )

    async def heartbeat():
        async with factory() as heartbeat_session:
            project = await heartbeat_session.get(Project, project_id)
            return await renew_resident_session_lease(
                heartbeat_session,
                project,
                session_id=started["session_id"],
                task_id=started["task_id"],
                progress=True,
            )

    async def stop():
        async with factory() as stop_session:
            project = await stop_session.get(Project, project_id)
            return await stop_resident_session(
                stop_session,
                project,
                operator_user_id=TEST_USER_ID,
            )

    heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.wait_for(prefs_locked.wait(), timeout=5)
    stop_task = asyncio.create_task(stop())
    try:
        # Stop takes the same canonical prefs -> Project order. It must wait
        # behind this already-linearized heartbeat, not deadlock or jump ahead.
        done, _ = await asyncio.wait({stop_task}, timeout=0.1)
        assert not done
    finally:
        resume_heartbeat.set()
    heartbeat_response = await asyncio.wait_for(heartbeat_task, timeout=10)
    stopped = await asyncio.wait_for(stop_task, timeout=10)

    assert heartbeat_response["authority_held"] is True
    assert heartbeat_response["stop_requested"] is False
    assert stopped["status"] == "ended"
    async with factory() as verify:
        project = await verify.get(Project, project_id)
    assert read_resident_session(project.vision)["status"] == "ended"
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in project.vision


@pytest.mark.asyncio
async def test_explicit_stop_before_heartbeat_cannot_be_rearmed(db_session):
    project_id, _, started = await _start_continuous(db_session)
    project = await db_session.get(Project, project_id)
    stopped = await stop_resident_session(
        db_session,
        project,
        operator_user_id=TEST_USER_ID,
    )
    assert stopped["status"] == "ended"

    project = await db_session.get(Project, project_id)
    response = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        progress=True,
    )

    assert response["authority_held"] is False
    assert response["stop_requested"] is True
    await db_session.refresh(project)
    assert read_resident_session(project.vision)["status"] == "ended"
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in project.vision


@pytest.mark.asyncio
async def test_competing_incarnation_cannot_renew_continuous_liveness(
    db_session,
):
    project_id, _, started = await _start_continuous(db_session)
    owner_incarnation = uuid.UUID("00000000-0000-0000-0000-000000000901")
    competing_incarnation = uuid.UUID("00000000-0000-0000-0000-000000000902")
    project = await db_session.get(Project, project_id)
    vision = dict(project.vision or {})
    resident = dict(vision[RESIDENT_SESSION_KEY])
    resident["worker_incarnation_id"] = str(owner_incarnation)
    resident["worker_incarnation_expires_at"] = (
        _utcnow() + timedelta(minutes=5)
    ).isoformat()
    resident["first_agent_heartbeat_at"] = _utcnow().isoformat()
    vision[RESIDENT_SESSION_KEY] = resident
    marker = dict(vision[STRATEGIC_LOCAL_CONTINUATION_KEY])
    marker["last_seen_at"] = (_utcnow() - timedelta(minutes=5)).isoformat()
    marker["updated_at"] = marker["last_seen_at"]
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    rejected_anchor = marker["last_seen_at"]

    project = await db_session.get(Project, project_id)
    rejected = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        incarnation_id=competing_incarnation,
        progress=False,
    )
    assert rejected["authority_held"] is False
    assert rejected["resident_incarnation_held"] is False
    assert rejected["stop_requested"] is True
    await db_session.refresh(project)
    assert (
        project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]["last_seen_at"]
        == rejected_anchor
    )

    owner = await renew_resident_session_lease(
        db_session,
        project,
        session_id=started["session_id"],
        task_id=started["task_id"],
        incarnation_id=owner_incarnation,
        progress=False,
    )
    assert owner["authority_held"] is True
    assert owner["resident_incarnation_held"] is True
    await db_session.refresh(project)
    assert (
        project.vision[STRATEGIC_LOCAL_CONTINUATION_KEY]["last_renewal_reason"]
        == "continuous_liveness"
    )


def test_dead_resident_process_remains_reapable_without_heartbeat(monkeypatch):
    now = _utcnow()
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        context={"_resident_session": True},
        vm_instance_id="runner:dead-continuous-resident",
        created_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=10),
        last_heartbeat=now - timedelta(minutes=2),
    )
    monkeypatch.setitem(
        runtime_config,
        "resident_session_heartbeat_stale_seconds",
        60,
    )

    assert _classify_resident_session(task, now, None) is True
