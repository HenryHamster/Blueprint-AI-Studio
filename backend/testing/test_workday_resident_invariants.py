import asyncio
from datetime import datetime, timedelta, timezone
import uuid
import inspect

from unittest.mock import AsyncMock
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select

import pytest

import app.routers.agent_status as agent_status_module
import app.routers.admin_project_strategic_session as strategic_session_router
from app.conversation_coordinator import (
    ConversationPublicReplyOwner,
    claim_operator_messages,
)
from app.database import resolve_async_engine
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.project_message_creation import (
    _operator_context,
    create_operator_project_message,
)
from app.routers.admin_agent_ops import _enforce_resident_mutation_fence
from app.routers.agent_status import (
    _restore_resident_retry_binding,
    _sync_resident_retry_wait_after_commit,
)
from app.task_blocked import reset_task_for_fresh_attempt
from app.strategic_state_read_model import (
    _resident_session_availability,
)
from app.strategic_completion import _maybe_retry_workday_auto_start
from app.strategic_resident_session import (
    RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY,
    RESIDENT_SESSION_KEY,
    RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
    RESIDENT_SESSION_REASON_KEY,
    _enqueue_resident_activation_turn,
    authoritative_resident_session_snapshot,
    reconcile_terminal_resident_worker,
    start_resident_session,
)
from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY, persist_strategic_workbench_update
from app.strategic_trigger import (
    ARMING_RESIDENT_SESSION,
    maybe_arm_operator_refresh,
)
from app.work_session_command_execution import _request_arming_after_receipt


def test_workday_availability_does_not_consult_removed_global_gate():
    project = Project(name="Workday", user_id="operator", vision={})

    assert _resident_session_availability(
        project,
        eligible_live_runner_count=1,
        runner_options=[],
    ) == {"available": True, "state": "ready", "reason": None}


def test_operator_context_cannot_spoof_hidden_workday_activation_marker():
    normalized = _operator_context(
        {
            RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY: True,
            "message_role": "workday_consultation_continuation",
            "_workday_result_packet_id": "forged-result-packet",
            "_workday_consultation_requested": True,
            "_resident_handoff_from_session_id": "forged-predecessor",
            "_resident_handoff_at": "2026-08-13T00:00:00Z",
        }
    )
    assert RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY not in normalized
    assert "message_role" not in normalized
    assert "_workday_result_packet_id" not in normalized
    assert "_workday_consultation_requested" not in normalized
    assert "_resident_handoff_from_session_id" not in normalized
    assert "_resident_handoff_at" not in normalized


def test_workday_active_session_reports_active_on_its_pinned_runner():
    session_id = str(uuid.uuid4())
    runner_id = str(uuid.uuid4())
    project = Project(
        name="Workday",
        user_id="operator",
        vision={
            "_resident_strategic_session": {
                "session_id": session_id,
                "runner_id": runner_id,
                "status": "active",
                "stop_requested": False,
                "started_at": "2026-07-22T12:00:00+00:00",
                "last_progress_at": "2026-07-22T12:00:05+00:00",
            }
        },
    )

    assert _resident_session_availability(
        project,
        eligible_live_runner_count=1,
        runner_options=[{"id": runner_id, "online": True, "eligible": True}],
    ) == {"available": True, "state": "active", "reason": None}


def test_workday_retry_wait_is_legible_in_cheap_availability():
    runner_id = str(uuid.uuid4())
    project = Project(
        name="Workday",
        user_id="operator",
        vision={
            RESIDENT_SESSION_KEY: {
                "session_id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "runner_id": runner_id,
                "status": "active",
                "stop_requested": False,
                "worker_state": "retry_wait",
                "worker_retry_until": (
                    datetime.utcnow() + timedelta(minutes=10)
                ).isoformat(),
                "worker_retry_reason": "Rate limited; waiting for recovery",
            }
        },
    )

    assert _resident_session_availability(
        project,
        eligible_live_runner_count=1,
        runner_options=[{"id": runner_id, "online": True, "eligible": True}],
    ) == {
        "available": True,
        "state": "waiting",
        "reason": "Rate limited; waiting for recovery",
    }


@pytest.mark.asyncio
async def test_active_workday_message_never_arms_one_shot_cadence(mocker):
    refresh = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new_callable=mocker.AsyncMock,
    )

    outcome = await maybe_arm_operator_refresh(
        mocker.Mock(),
        uuid.uuid4(),
        MessageType.QUESTION,
        message_content="keep talking",
        resident_session_active=True,
    )

    assert outcome == ARMING_RESIDENT_SESSION
    refresh.assert_not_awaited()


def _test_session_factory(session):
    return async_sessionmaker(
        resolve_async_engine(session),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_resident_record(
    session,
    *,
    task_status=TaskStatus.FAILED,
    blob_generation=1,
    stop_requested=False,
):
    project = Project(name="Resident continuity", user_id="operator", vision={})
    session.add(project)
    await session.commit()
    await session.refresh(project)

    session_id = str(uuid.uuid4())
    runner_id = str(uuid.uuid4())
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "resident"}],
        status=task_status,
        created_by="ORCHESTRATOR",
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": 1,
        },
        depends_on=[],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    project.vision = {
        RESIDENT_SESSION_KEY: {
            "version": 2,
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": blob_generation,
            "worker_state": "ready",
            "runner_id": runner_id,
            "operator_user_id": "operator",
            "status": "active",
            "stop_requested": stop_requested,
        }
    }
    session.add(project)
    await session.commit()
    await session.refresh(project)
    # An active Resident is authoritative only while the operator has selected
    # Workday. Keep fabricated lifecycle rows aligned with the real start path,
    # which commits this preference before creating activation/input messages.
    await persist_strategic_workbench_update(
        session,
        "operator",
        project.id,
        {"interaction_mode": "continuous"},
        sync_local_continuation_marker=False,
        arm_interaction_mode=False,
    )

    return project, task, runner_id


@pytest.mark.asyncio
async def test_workday_activation_enqueues_one_canonical_fast_turn_per_session(
    db_session,
):
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    snapshot = await authoritative_resident_session_snapshot(db_session, project)
    assert snapshot is not None

    await _enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id="operator",
        snapshot=snapshot,
    )
    # A retry/adoption of the same session must not create another opening.
    await _enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id="operator",
        snapshot=snapshot,
    )

    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    opening = rows[0]
    assert opening.from_type == "user"
    assert opening.surface == MessageSurface.CONVERSATION
    assert opening.message_type == MessageType.NOTE
    assert opening.status == "open"
    assert opening.handling_state == "pending"
    assert opening.resident_session_id == snapshot["session_id"]
    assert opening.idempotency_key == f"resident-activation:{snapshot['session_id']}"
    assert opening.context[RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY] is True
    assert "started Workday" in opening.content

    claim_id = uuid.uuid4()
    opening_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=snapshot["session_id"],
        task_id=task.id,
        worker_generation=snapshot["worker_generation"],
        incarnation_id=uuid.uuid4(),
        message_ids=[opening.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=claim_id,
        claim_idempotency_key=f"activation-only:{claim_id}",
    )
    assert opening_claim is not None
    assert [row.id for row in opening_claim.rows] == [opening.id]


@pytest.mark.asyncio
async def test_real_operator_input_supersedes_unclaimed_activation_before_poll(
    db_session,
):
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    snapshot = await authoritative_resident_session_snapshot(db_session, project)
    assert snapshot is not None

    await _enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id="operator",
        snapshot=snapshot,
    )
    activation = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"resident-activation:{snapshot['session_id']}",
            )
        )
    ).scalar_one()

    real = await create_operator_project_message(
        db_session,
        project=project,
        user_id="operator",
        message_type=MessageType.NOTE,
        content="What are you working on right now?",
        context={},
        workbench_context=None,
        idempotency_key=str(uuid.uuid4()),
        surface=MessageSurface.CONVERSATION,
        include_strategic_session_snapshot=False,
    )
    await db_session.refresh(activation)

    assert activation.status == "resolved"
    assert activation.handling_state == MessageHandlingState.SUPERSEDED
    assert activation.resolution == "operator_input_superseded_activation"
    assert activation.resolved_at is not None
    assert activation.conversation_claim_id is None
    assert real.message.status == "open"
    assert real.message.handling_state == MessageHandlingState.PENDING
    assert real.message.resident_session_id == snapshot["session_id"]

    open_current_inputs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.resident_session_id == snapshot["session_id"],
                    ProjectMessage.status == "open",
                    ProjectMessage.from_type == "user",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in open_current_inputs] == [real.message.id]

    stale_claim_id = uuid.uuid4()
    stale_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=snapshot["session_id"],
        task_id=task.id,
        worker_generation=snapshot["worker_generation"],
        incarnation_id=uuid.uuid4(),
        message_ids=[activation.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=stale_claim_id,
        claim_idempotency_key=f"superseded-activation:{stale_claim_id}",
    )
    assert stale_claim is None

    real_claim_id = uuid.uuid4()
    real_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id=snapshot["session_id"],
        task_id=task.id,
        worker_generation=snapshot["worker_generation"],
        incarnation_id=uuid.uuid4(),
        message_ids=[real.message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=real_claim_id,
        claim_idempotency_key=f"real-input-only:{real_claim_id}",
    )
    assert real_claim is not None
    assert [row.id for row in real_claim.rows] == [real.message.id]


@pytest.mark.asyncio
async def test_workday_activation_enqueue_failure_does_not_fail_started_session(
    db_session, mocker
):
    project, _, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    project_id = project.id
    snapshot = await authoritative_resident_session_snapshot(db_session, project)
    assert snapshot is not None
    mocker.patch(
        "app.project_message_creation.create_operator_project_message",
        new=mocker.AsyncMock(side_effect=RuntimeError("message store unavailable")),
    )

    await _enqueue_resident_activation_turn(
        db_session,
        project,
        operator_user_id="operator",
        snapshot=snapshot,
    )

    fresh = await db_session.get(Project, project_id)
    assert fresh is not None
    assert fresh.vision[RESIDENT_SESSION_KEY]["status"] == "active"


@pytest.mark.asyncio
async def test_resident_fresh_attempt_restores_actual_runner_columns(db_session):
    project, task, runner_id = await _make_resident_record(
        db_session, task_status=TaskStatus.FAILED
    )
    prior_context = dict(task.context or {})
    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
    task.vm_provisioned_at = None

    before = datetime.utcnow()
    assert await _restore_resident_retry_binding(
        task, db_session, prior_context
    ) is True
    await db_session.commit()

    factory = _test_session_factory(db_session)
    async with factory() as verify:
        fresh = await verify.get(Task, task.id)
        assert fresh.status == TaskStatus.PROVISIONING
        assert fresh.vm_instance_id == f"runner:{runner_id}"
        assert fresh.vm_provisioned_at is not None
        assert fresh.vm_provisioned_at >= before
        assert fresh.context["runner_target_id"] == runner_id
        assert fresh.context["preferred_runner_target_id"] == runner_id
        assert fresh.context["_resident_session"] is True


@pytest.mark.asyncio
async def test_resident_retry_does_not_restore_task_declined_runner(db_session):
    _, task, runner_id = await _make_resident_record(
        db_session, task_status=TaskStatus.FAILED
    )
    prior_context = dict(task.context or {})
    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED

    restored = await _restore_resident_retry_binding(
        task,
        db_session,
        prior_context,
        excluded_runner_ids={runner_id},
    )

    assert restored is False
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id is None
    assert "runner_target_id" not in (task.context or {})


@pytest.mark.asyncio
async def test_retry_wait_projection_failure_keeps_durable_retry(
    db_session, monkeypatch
):
    project, task, runner_id = await _make_resident_record(
        db_session, task_status=TaskStatus.FAILED
    )
    prior_context = dict(task.context or {})
    reset_task_for_fresh_attempt(task)
    task.retry_after_until = datetime.utcnow() + timedelta(minutes=10)
    task.latest_status_message = "Rate limited; deliberate retry wait"
    assert await _restore_resident_retry_binding(
        task, db_session, prior_context
    ) is True
    await db_session.commit()

    failed_projection = AsyncMock(side_effect=RuntimeError("projection unavailable"))
    monkeypatch.setattr(
        "app.strategic_resident_session.sync_resident_worker_retry_wait",
        failed_projection,
    )
    assert await _sync_resident_retry_wait_after_commit(
        task, db_session, prior_context
    ) is False

    factory = _test_session_factory(db_session)
    async with factory() as verify:
        fresh_project = await verify.get(Project, project.id)
        fresh = await verify.get(Task, task.id)
        assert fresh.status == TaskStatus.PROVISIONING
        assert fresh.vm_instance_id == f"runner:{runner_id}"
        assert fresh.retry_after_until is not None
        assert fresh.retry_after_until > datetime.utcnow()
        assert fresh.context["_resident_session"] is True
        # The durable Task is authoritative even when the best-effort Project
        # projection failed: cheap/full reads must still report a healthy wait.
        snapshot = await authoritative_resident_session_snapshot(
            verify, fresh_project
        )
        assert snapshot["worker_state"] == "retry_wait"
        assert snapshot["worker_retry_until"] == fresh.retry_after_until.isoformat()
        assert snapshot["worker_retry_reason"] == task.latest_status_message
        assert _resident_session_availability(
            fresh_project,
            eligible_live_runner_count=1,
            runner_options=[
                {"id": runner_id, "online": True, "eligible": True}
            ],
            snapshot=snapshot,
        ) == {
            "available": True,
            "state": "waiting",
            "reason": "Rate limited; deliberate retry wait",
        }



def test_cheap_and_full_resident_reads_use_authoritative_snapshot():
    """Both API read surfaces must expose the Task-derived retry projection."""
    for endpoint in (
        strategic_session_router.get_project_strategic_liveness,
        strategic_session_router.get_project_strategic_state,
    ):
        source = inspect.getsource(endpoint)
        assert (
            "resident_snapshot = await authoritative_resident_session_snapshot("
            in source
        )
        assert '"resident_session": resident_snapshot' in source
        assert "snapshot=resident_snapshot" in source


def test_all_three_fresh_attempt_paths_reapply_resident_binding():
    source = inspect.getsource(agent_status_module)
    reset_marker = "reset_task_for_fresh_attempt(task)"
    segments = source.split(reset_marker)
    assert len(segments) == 4, "expected rate-limit, dead-auth, and general resets"
    expected_path_evidence = (
        "external_retry_delay_minutes",
        "_dead_auth_blacklist",
        "_prior_retry_ctx",
    )
    for evidence, post_reset in zip(expected_path_evidence, segments[1:]):
        window = post_reset[:12000]
        assert evidence in window
        assert "await _restore_resident_retry_binding(" in window
    assert source.count(
        "await _sync_resident_retry_wait_after_commit("
    ) == 3


@pytest.mark.asyncio
async def test_resident_retry_reloads_live_project_before_repinning(db_session):
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.FAILED
    )
    prior_context = dict(task.context or {})
    factory = _test_session_factory(db_session)
    # Keep the active Project instance in db_session's identity map, then end
    # it independently. The retry helper must populate_existing from the DB.
    assert project.vision[RESIDENT_SESSION_KEY]["status"] == "active"
    async with factory() as writer:
        fresh_project = await writer.get(Project, project.id)
        vision = dict(fresh_project.vision or {})
        blob = dict(vision[RESIDENT_SESSION_KEY])
        blob["status"] = "ended"
        blob["stop_requested"] = True
        vision[RESIDENT_SESSION_KEY] = blob
        fresh_project.vision = vision
        writer.add(fresh_project)
        await writer.commit()

    reset_task_for_fresh_attempt(task)
    assert await _restore_resident_retry_binding(
        task, db_session, prior_context
    ) is False
    assert task.status != TaskStatus.PROVISIONING
    assert not str(task.vm_instance_id or "").startswith("runner:")


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["start", "stop"])
async def test_operator_send_serializes_with_resident_transition(
    db_session, transition
):
    project = Project(name="Message fence", user_id="operator", vision={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    if transition == "start":
        await persist_strategic_workbench_update(
            db_session,
            "operator",
            project.id,
            {"interaction_mode": "continuous"},
            sync_local_continuation_marker=False,
            arm_interaction_mode=False,
        )
    factory = _test_session_factory(db_session)

    async with factory() as sender, factory() as controller:
        stale_project = await sender.get(Project, project.id)
        locked = await controller.scalar(
            select(Project).where(Project.id == project.id).with_for_update()
        )
        session_id = str(uuid.uuid4())
        locked.vision = {
            RESIDENT_SESSION_KEY: {
                "session_id": session_id,
                "status": "active" if transition == "start" else "ended",
                "stop_requested": transition == "stop",
            }
        }
        await controller.flush()
        send = asyncio.create_task(
            create_operator_project_message(
                sender,
                project=stale_project,
                user_id="operator",
                message_type=MessageType.NOTE,
                content=f"during {transition}",
                context={},
                workbench_context=None,
                idempotency_key=str(uuid.uuid4()),
                surface=MessageSurface.CONVERSATION,
                include_strategic_session_snapshot=False,
            )
        )
        await asyncio.sleep(0.05)
        assert not send.done()
        await controller.commit()
        result = await asyncio.wait_for(send, timeout=5)
        expected = session_id if transition == "start" else None
        actual = (
            str(result.message.resident_session_id)
            if result.message.resident_session_id
            else None
        )
        assert actual == expected

def _continuation_marker(*, fresh: bool) -> dict:
    seen = datetime.now(timezone.utc)
    if not fresh:
        seen = seen - timedelta(hours=2)
    stamp = seen.isoformat()
    return {
        "version": 1,
        "active": True,
        "operator_user_id": "operator",
        "updated_at": stamp,
        "last_seen_at": stamp,
    }


@pytest.mark.asyncio
async def test_resident_mutations_reject_expired_continuation_but_messages_pass(
    db_session,
):
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    vision = dict(project.vision or {})
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = _continuation_marker(fresh=False)
    project.vision = vision
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(HTTPException) as expired:
        await _enforce_resident_mutation_fence(
            db_session, project, task, "update_running_update"
        )
    assert expired.value.status_code == 409
    assert "expired" in str(expired.value.detail).lower()

    await _enforce_resident_mutation_fence(
        db_session, project, task, "create_message"
    )
    await _enforce_resident_mutation_fence(
        db_session, project, task, "resolve_message"
    )


@pytest.mark.asyncio
async def test_resident_mutations_reject_overlapping_one_shot_but_messages_pass(
    db_session,
):
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    vision = dict(project.vision or {})
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = _continuation_marker(fresh=True)
    project.vision = vision
    one_shot = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "superseding one-shot"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        context={},
        depends_on=[],
    )
    db_session.add_all([project, one_shot])
    await db_session.commit()

    with pytest.raises(HTTPException) as overlap:
        await _enforce_resident_mutation_fence(
            db_session, project, task, "update_running_update"
        )
    assert overlap.value.status_code == 409
    assert "lane" in str(overlap.value.detail).lower()

    await _enforce_resident_mutation_fence(
        db_session, project, task, "create_message"
    )




@pytest.mark.asyncio
async def test_terminal_reconciler_ignores_requeued_worker(db_session, mocker):
    _, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.QUEUED
    )
    factory = _test_session_factory(db_session)
    start = mocker.patch(
        "app.strategic_resident_session.start_resident_session",
        new_callable=mocker.AsyncMock,
    )

    assert await reconcile_terminal_resident_worker(
        task.id, session_factory=factory
    ) is False
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_reconciler_replaces_exact_continuous_generation(
    db_session, mocker
):
    _, task, runner_id = await _make_resident_record(db_session)
    factory = _test_session_factory(db_session)
    mocker.patch(
        "app.strategic_resident_session.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    start = mocker.patch(
        "app.strategic_resident_session.start_resident_session",
        new=mocker.AsyncMock(return_value={"worker_generation": 2}),
    )

    assert await reconcile_terminal_resident_worker(
        task.id, session_factory=factory
    ) is True
    assert start.await_args.kwargs["runner_id"] == runner_id
    assert start.await_args.kwargs["operator_user_id"] == "operator"
    assert start.await_args.kwargs["expected_recovery_identity"][1] == str(task.id)


@pytest.mark.asyncio
async def test_terminal_reconciler_exposes_start_after_replacement_failure(
    db_session, mocker
):
    project, task, _ = await _make_resident_record(db_session)
    factory = _test_session_factory(db_session)
    mocker.patch(
        "app.strategic_resident_session.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    mocker.patch(
        "app.strategic_resident_session.start_resident_session",
        new=mocker.AsyncMock(
            side_effect=HTTPException(status_code=409, detail="runner unavailable")
        ),
    )

    assert await reconcile_terminal_resident_worker(
        task.id, session_factory=factory
    ) is False
    async with factory() as verify:
        fresh = await verify.get(Project, project.id)
        blob = fresh.vision[RESIDENT_SESSION_KEY]
        reason = fresh.vision[RESIDENT_SESSION_REASON_KEY]["reason"]
    assert blob["status"] == "ended"
    assert blob["worker_state"] == "restart_failed"
    assert reason.startswith("start_failed: runner unavailable")


@pytest.mark.asyncio
async def test_terminal_reconciler_never_restarts_superseded_or_stopped_worker(
    db_session, mocker
):
    _, superseded, _ = await _make_resident_record(
        db_session, blob_generation=2
    )
    _, stopped, _ = await _make_resident_record(
        db_session, stop_requested=True
    )
    factory = _test_session_factory(db_session)
    start = mocker.patch(
        "app.strategic_resident_session.start_resident_session",
        new_callable=mocker.AsyncMock,
    )

    assert await reconcile_terminal_resident_worker(
        superseded.id, session_factory=factory
    ) is False
    assert await reconcile_terminal_resident_worker(
        stopped.id, session_factory=factory
    ) is False
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_one_shot_records_cycle_reason_then_completion_retries(
    db_session, mocker
):
    project = Project(name="Single writer", user_id="operator", vision={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    one_shot = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "one shot"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        context={},
        depends_on=[],
    )
    db_session.add(one_shot)
    await db_session.commit()
    await db_session.refresh(one_shot)
    factory = _test_session_factory(db_session)
    project_id = project.id
    one_shot_id = one_shot.id
    mocker.patch(
        "app.strategic_resident_session._auto_select_local_runner",
        new=mocker.AsyncMock(return_value=str(uuid.uuid4())),
    )
    mocker.patch(
        "app.strategic_resident_session._spawn_resident_strategic_task",
        new=mocker.AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as blocked:
        await start_resident_session(
            db_session,
            project,
            operator_user_id="operator",
            session_factory=factory,
        )
    assert blocked.value.status_code == 409
    async with factory() as verify:
        fresh = await verify.get(Project, project_id)
        reason = fresh.vision[RESIDENT_SESSION_REASON_KEY]
    assert reason["reason"] == RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT
    assert reason["operator_user_id"] == "operator"

    one_shot.status = TaskStatus.COMPLETED
    one_shot = await db_session.get(Task, one_shot_id)
    db_session.add(one_shot)
    await db_session.commit()
    mocker.patch(
        "app.workbench_state.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    retry = mocker.patch(
        "app.strategic_resident_session._auto_start_on_workday_entry",
        new_callable=mocker.AsyncMock,
    )
    await _maybe_retry_workday_auto_start(project_id, session_factory=factory)
    retry.assert_awaited_once_with(
        project_id,
        operator_user_id="operator",
        session_factory=factory,
    )


@pytest.mark.asyncio
async def test_completion_retry_not_suppressed_by_active_blob_with_dead_worker(
    db_session, mocker
):
    """A terminal resident worker must NOT suppress the post-commit Workday retry.

    ``_resident_strategic_session`` is a status-only operator-facing projection:
    it keeps ``status: "active"`` after its worker task goes terminal. Gating the
    post-commit retry on ``resident_session_is_active`` alone therefore suppresses
    the recovery forever — the Resident is dead, nothing restarts it, and the
    Workday auto-start this path exists to re-drive never happens.
    """
    project, task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.FAILED
    )
    # The blob still advertises a live session even though its exact worker died.
    assert project.vision[RESIDENT_SESSION_KEY]["status"] == "active"
    assert project.vision[RESIDENT_SESSION_KEY]["stop_requested"] is False
    assert str(project.vision[RESIDENT_SESSION_KEY]["task_id"]) == str(task.id)
    assert task.status == TaskStatus.FAILED

    vision = dict(project.vision or {})
    vision[RESIDENT_SESSION_REASON_KEY] = {
        "reason": RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
        "operator_user_id": "operator",
    }
    project.vision = vision
    db_session.add(project)
    await db_session.commit()

    factory = _test_session_factory(db_session)
    project_id = project.id
    mocker.patch(
        "app.workbench_state.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    retry = mocker.patch(
        "app.strategic_resident_session._auto_start_on_workday_entry",
        new_callable=mocker.AsyncMock,
    )

    await _maybe_retry_workday_auto_start(project_id, session_factory=factory)

    retry.assert_awaited_once_with(
        project_id,
        operator_user_id="operator",
        session_factory=factory,
    )


@pytest.mark.asyncio
async def test_completion_retry_still_suppressed_by_healthy_resident_worker(
    db_session, mocker
):
    """The health guard must not degrade into "always retry".

    A live, identity-matched, freshly-heartbeating worker still owns the project,
    so the post-commit retry must stay suppressed and never spawn a second one.
    """
    project, _task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = now_iso
    blob["worker_heartbeat_at"] = now_iso
    vision[RESIDENT_SESSION_KEY] = blob
    vision[RESIDENT_SESSION_REASON_KEY] = {
        "reason": RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
        "operator_user_id": "operator",
    }
    project.vision = vision
    db_session.add(project)
    await db_session.commit()

    factory = _test_session_factory(db_session)
    project_id = project.id
    mocker.patch(
        "app.workbench_state.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    retry = mocker.patch(
        "app.strategic_resident_session._auto_start_on_workday_entry",
        new_callable=mocker.AsyncMock,
    )

    await _maybe_retry_workday_auto_start(project_id, session_factory=factory)

    retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_retry_suppressed_when_worker_generation_superseded(
    db_session, mocker
):
    """An active blob naming a REPLACEMENT generation is not this task's liveness.

    The recorded task belongs to generation 1 while the blob advertises
    generation 2, so the blob's ``task_id`` no longer identifies a worker this
    projection can vouch for. The identity leg of the health contract must reject
    it exactly as the liveness sweep does, and the retry must proceed.
    """
    project, _task, _ = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS, blob_generation=2
    )
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    vision = dict(project.vision or {})
    blob = dict(vision[RESIDENT_SESSION_KEY])
    blob["started_at"] = now_iso
    blob["worker_heartbeat_at"] = now_iso
    vision[RESIDENT_SESSION_KEY] = blob
    vision[RESIDENT_SESSION_REASON_KEY] = {
        "reason": RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
        "operator_user_id": "operator",
    }
    project.vision = vision
    db_session.add(project)
    await db_session.commit()

    factory = _test_session_factory(db_session)
    project_id = project.id
    mocker.patch(
        "app.workbench_state.get_strategic_workbench_state",
        new=mocker.AsyncMock(return_value={"interaction_mode": "continuous"}),
    )
    retry = mocker.patch(
        "app.strategic_resident_session._auto_start_on_workday_entry",
        new_callable=mocker.AsyncMock,
    )

    await _maybe_retry_workday_auto_start(project_id, session_factory=factory)

    retry.assert_awaited_once_with(
        project_id,
        operator_user_id="operator",
        session_factory=factory,
    )


@pytest.mark.asyncio
async def test_workbench_and_work_session_arming_suppressed_during_resident_session(
    db_session, mocker
):
    """Both operator-arming paths must return ARMING_RESIDENT_SESSION when a
    resident session is live, and must NOT call request_strategic_refresh."""
    refresh = mocker.patch(
        "app.workbench_state.request_strategic_refresh",
        new_callable=mocker.AsyncMock,
    )
    refresh2 = mocker.patch(
        "app.work_session_command_execution.request_strategic_refresh",
        new_callable=mocker.AsyncMock,
    )

    # ── path 1: direct workbench update (admin_notifications.py endpoint) ──
    project = Project(name="Resident arming guard", user_id="operator", vision={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Arm into continuous mode first so should_arm=True
    initial_state = {"interaction_mode": "continuous"}
    result = await persist_strategic_workbench_update(
        db_session,
        "operator",
        project.id,
        initial_state,
        resident_session_active=True,
    )
    assert result.arming == ARMING_RESIDENT_SESSION, (
        f"workbench update with active resident returned {result.arming!r}, "
        "expected ARMING_RESIDENT_SESSION"
    )
    refresh.assert_not_awaited()

    # ── path 2: work-session command receipt path ──
    mock_session = mocker.AsyncMock()
    arming = await _request_arming_after_receipt(
        mock_session,
        project_id=project.id,
        state={"interaction_mode": "continuous"},
        resident_session_active=True,
    )
    assert arming == ARMING_RESIDENT_SESSION, (
        f"_request_arming_after_receipt with active resident returned {arming!r}, "
        "expected ARMING_RESIDENT_SESSION"
    )
    refresh2.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# Operator-message delivery invariant.
#
# ``create_operator_project_message`` hardcodes ``from_type="user"``: every row
# it writes is operator-authored. It used to default ``surface`` to ``activity``,
# the display-only projection, and two of its five callers -- the two live
# human-send routes (``admin_notifications`` POST /messages and ``directions``
# POST /projects/{id}/messages, which is what ``blueprint messages send`` hits)
# -- relied on that default.
#
# The consequence is not cosmetic labelling. Inside this same function the
# ``arming = ARMING_RESIDENT_SESSION`` assignment is gated on
# ``surface == CONVERSATION``, so an operator message on ``activity`` never arms
# the active Resident. The message persists, the Resident can see it pending,
# and nothing is ever armed to consume it.
#
# The 0608/0638 Postgres compat trigger cannot repair this: its ``should_project``
# promotion requires the row to arrive with ``surface='conversation'`` already or
# with ``sequence IS NULL``, and this function pre-allocates the sequence before
# INSERT, so the wrong value is durable.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_message_default_surface_is_the_serviced_lane(db_session):
    """An operator message created without an explicit surface is deliverable."""
    project = Project(name="Operator channel", user_id="operator", vision={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await create_operator_project_message(
        db_session,
        project=project,
        user_id="operator",
        message_type=MessageType.NOTE,
        content="Why is the fleet idle?",
        context={},
        workbench_context=None,
        idempotency_key=str(uuid.uuid4()),
        include_strategic_session_snapshot=False,
        # surface deliberately omitted -- the default is what is under test.
    )

    row = result.message
    assert row.from_type == "user"
    assert row.surface == MessageSurface.CONVERSATION
    # Literal pin: the serviced lane is the string 'conversation'. Renaming the
    # enum member must not silently re-route operator mail to a dead surface.
    # (``row.surface`` is a bare str pre-refresh, so normalise before comparing.)
    assert MessageSurface(row.surface) is MessageSurface.CONVERSATION
    assert MessageSurface.CONVERSATION.value == "conversation"
    # Pending, not terminal: the row is claimable by the Resident poll.
    assert row.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_operator_message_default_arms_the_active_resident(db_session):
    """The default must arm an active Resident; the activity surface must not.

    Asserted A/B in one test so it cannot pass vacuously: if the surface gate on
    the arming branch ever stops binding, the ``activity`` leg below fails and
    the test still catches it.
    """
    project, _task, _runner = await _make_resident_record(
        db_session, task_status=TaskStatus.IN_PROGRESS
    )

    defaulted = await create_operator_project_message(
        db_session,
        project=project,
        user_id="operator",
        message_type=MessageType.NOTE,
        content="Status on the promotion?",
        context={},
        workbench_context=None,
        idempotency_key=str(uuid.uuid4()),
        include_strategic_session_snapshot=False,
        # surface deliberately omitted.
    )
    assert defaulted.message.surface == MessageSurface.CONVERSATION
    assert defaulted.arming == ARMING_RESIDENT_SESSION

    # Contrast leg: the display-only surface is what used to be the default, and
    # it leaves the active Resident unarmed. This is the defect being fenced.
    parked = await create_operator_project_message(
        db_session,
        project=project,
        user_id="operator",
        message_type=MessageType.NOTE,
        content="Filed for the activity feed only.",
        context={},
        workbench_context=None,
        idempotency_key=str(uuid.uuid4()),
        surface=MessageSurface.ACTIVITY,
        include_strategic_session_snapshot=False,
    )
    assert parked.message.surface == MessageSurface.ACTIVITY
    assert parked.arming != ARMING_RESIDENT_SESSION
