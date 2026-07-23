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
