"""Focused regression coverage for the restored Workday resident lifecycle.

Adapted from the legacy hardening suite in commit 06a97b8da. The restored
subsystem intentionally has no global feature gate or separate resident table,
so these tests cover the surviving Project.vision lifecycle, lease, message
handoff, and reaper contracts directly.
"""

import asyncio
from datetime import timedelta
from inspect import unwrap
import uuid
from unittest.mock import AsyncMock

import pytest
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
    ProjectMessage,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.admin_project_strategic_session import (
    StrategicResidentSessionStart,
    start_project_strategic_resident_session,
)
from app.reaper_orphan_classify import _classify_resident_session
from app.session_message_handoff import handoff_session_messages
from app.shared_state import runtime_config
from app.strategic_resident_session import (
    RESIDENT_SESSION_KEY,
    STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
    new_session_id,
    read_resident_session,
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
async def test_continuous_hydration_ensure_succeeds(db_session):
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


async def test_start_adopts_task_and_liveness_heartbeat_preserves_lease(db_session):
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
async def test_reload_ensure_replaces_worker_stale_past_ui_threshold(db_session):
    project = await _make_project(db_session)
    old_task = await _make_task(db_session, project)
    await _make_runner(db_session, project)
    started = await start_resident_session(
        db_session, project, operator_user_id=TEST_USER_ID
    )
    stale_at = _utcnow() - timedelta(seconds=91)

    old_task = await db_session.get(Task, old_task.id)
    old_task.status = TaskStatus.IN_PROGRESS
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
async def test_stop_ends_task_releases_continuation_and_hands_off_messages(
    db_session, monkeypatch
):
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
    await db_session.refresh(pending)
    assert pending.status == "open"
    assert pending.resident_session_id is None
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


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
    # Message must be released even though rearm raised
    await db_session.refresh(pending)
    assert pending.resident_session_id is None, (
        "message.resident_session_id must be None: cap handoff must commit "
        "atomically with the cap-end, not in a separate follow-up transaction"
    )
    assert pending.handling_state == MessageHandlingState.PENDING
    assert pending.handled_by_task_id is None


@pytest.mark.asyncio
async def test_handoff_releases_unanswered_and_resolves_answered(
    db_session, monkeypatch
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    session_id = "ss-ended"
    unanswered = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="What should happen next?",
        status="open",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
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
    """handled_message_ids in renew_resident_session_lease must ONLY mark messages
    as 'handled' when their resident_session_id matches the active session.

    Legacy / cadence messages (resident_session_id IS NULL) and messages pinned
    to a different session must remain untouched even when their IDs appear in
    handled_message_ids.  This is the invariant enforced at
    strategic_resident_session.py::L3425 (WHERE resident_session_id == session_id).
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
    db_session.add_all([own_msg, null_session_msg, other_session_msg])
    await db_session.commit()
    for m in (own_msg, null_session_msg, other_session_msg):
        await db_session.refresh(m)

    project = await db_session.get(Project, project.id)
    await renew_resident_session_lease(
        db_session,
        project,
        session_id=session_id,
        task_id=started["task_id"],
        progress=False,
        handled_message_ids=[
            str(own_msg.id),
            str(null_session_msg.id),
            str(other_session_msg.id),
        ],
    )

    await db_session.refresh(own_msg)
    await db_session.refresh(null_session_msg)
    await db_session.refresh(other_session_msg)

    assert own_msg.status == "handled", (
        "Own-session message must be marked handled by the heartbeat"
    )
    assert null_session_msg.status == "open", (
        "NULL-session (legacy/cadence) message must NOT be marked handled"
    )
    assert other_session_msg.status == "open", (
        "Message from a different session must NOT be marked handled"
    )
