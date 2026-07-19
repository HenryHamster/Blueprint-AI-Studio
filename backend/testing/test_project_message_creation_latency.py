"""Focused lock-window tests for durable operator message creation."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicResident,
    Task,
)
from app.notification_dispatch import post_event_message
from app.project_message_creation import (
    _lock_operator_idempotency_key,
    create_operator_project_message,
    lock_project_and_next_message_sequence,
    reserve_project_message_sequences,
)
from app.strategic_resident_turns import (
    coalesce_shadow_resident_turn,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident
from testing.conftest import TestingSessionLocal, USE_POSTGRES


async def _project_with_resident(session) -> Project:
    project = Project(
        name="Message lock window",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    await initialize_strategic_resident(session, project.id)
    await session.commit()
    await session.refresh(project)
    return project


async def _send_conversation(
    session,
    project_id: uuid.UUID,
    *,
    content: str,
    idempotency_key: str,
    precommit_validate=None,
    message_type: MessageType = MessageType.NOTE,
):
    project = await session.get(Project, project_id)
    assert project is not None
    return await create_operator_project_message(
        session,
        project=project,
        user_id="operator",
        message_type=message_type,
        content=content,
        context={},
        workbench_context=None,
        idempotency_key=idempotency_key,
        surface=MessageSurface.CONVERSATION,
        include_strategic_session_snapshot=False,
        precommit_validate=precommit_validate,
    )


def test_sequence_critical_section_contains_only_atomic_acceptance_work():
    source = inspect.getsource(create_operator_project_message)
    workbench_at = source.index("await persist_strategic_workbench_update")
    workbench_flush_at = source.index("await session.flush()", workbench_at)
    project_fence_at = source.index("await lock_resident_message_owner_state")
    arming_at = source.index("arming = await maybe_arm_operator_refresh", project_fence_at)
    identity_lock_at = source.index(
        "await _lock_operator_idempotency_key",
        project_fence_at,
    )
    reply_at = source.index("if reply_to_message_id is not None:", identity_lock_at)
    precommit_at = source.index("if precommit_validate is not None:", reply_at)
    resident_prelock_at = source.index("locked_resident = None", precommit_at)
    sequence_at = source.index(
        "next_sequence = await lock_project_and_next_message_sequence",
        resident_prelock_at,
    )
    race_close_at = source.index("if idem_key:", sequence_at)
    shadow_at = source.index(
        "if locked_resident is not None:",
        race_close_at,
    )
    message_at = source.index("msg = ProjectMessage(", shadow_at)
    commit_at = source.index("await session.commit()", message_at)

    assert (
        workbench_at
        < workbench_flush_at
        < project_fence_at
        < arming_at
        < identity_lock_at
        < reply_at
        < precommit_at
        < resident_prelock_at
        < sequence_at
        < race_close_at
        < shadow_at
        < message_at
        < commit_at
    )
    assert (
        source.count("next_sequence = await lock_project_and_next_message_sequence")
        == 1
    )


def test_shared_allocator_enforces_project_before_counter_for_all_writers():
    allocator_source = inspect.getsource(reserve_project_message_sequences)
    project_at = allocator_source.index(".with_for_update(read=True, key_share=True)")
    counter_at = allocator_source.index(
        "INSERT INTO projectmessagesequence AS counter"
    )
    assert project_at < counter_at

    from app.routers.admin_agent_ops import strategic_operation
    from app.routers.admin_notifications import resolve_message
    from app.routers.admin_project_strategic_session import (
        create_project_strategic_session_check_in,
    )

    resolve_source = inspect.getsource(resolve_message)
    assert resolve_source.index(".with_for_update()") < resolve_source.index(
        "reply_sequence = await lock_project_and_next_message_sequence"
    )

    agent_source = inspect.getsource(strategic_operation)
    project_at = agent_source.index(
        "project = await _lock_project_or_429(session, task.project_id)"
    )
    sequence_at = agent_source.index(
        "await lock_project_and_next_message_sequence",
        project_at,
    )
    task_at = agent_source.index("async with task_lock(session, task.id", sequence_at)
    assert project_at < sequence_at < task_at

    check_in_source = inspect.getsource(create_project_strategic_session_check_in)
    assert "next_sequence = await lock_project_and_next_message_sequence" in (
        check_in_source
    )
    assert "message = await build_project_message" in check_in_source


@pytest.mark.asyncio
async def test_postgres_idempotency_lock_is_project_and_identity_scoped():
    session = MagicMock(spec=AsyncSession)
    session.bind = None
    session.get_bind.return_value.dialect.name = "postgresql"
    session.execute = AsyncMock()
    project_id = uuid.uuid4()

    await _lock_operator_idempotency_key(session, project_id, "client-send-1")

    statement, params = session.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert "hashtextextended" in str(statement)
    assert params == {"identity": f"project-message:{project_id}:client-send-1"}


@pytest.mark.asyncio
async def test_sequence_allocator_resolves_lazy_session_postgres_bind():
    session = MagicMock(spec=AsyncSession)
    session.bind = None
    session.get_bind.return_value.dialect.name = "postgresql"
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=42)

    result = await lock_project_and_next_message_sequence(
        session,
        uuid.uuid4(),
        lock_timeout_ms=1_500,
    )

    assert result == 42
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert statements == [
        "SET LOCAL lock_timeout = '1500ms'",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    assert "projectmessagesequence" in str(session.scalar.await_args.args[0])


@pytest.mark.asyncio
async def test_invalid_reply_is_rejected_before_sequence_reservation(db_session):
    project = await _project_with_resident(db_session)

    with (
        patch(
            "app.project_message_creation.lock_project_and_next_message_sequence",
            new=AsyncMock(),
        ) as reserve,
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_operator_project_message(
            db_session,
            project=project,
            user_id="operator",
            message_type=MessageType.NOTE,
            content="Reply to a missing message.",
            context={},
            workbench_context=None,
            idempotency_key="invalid-reply-before-sequence",
            surface=MessageSurface.CONVERSATION,
            reply_to_message_id=uuid.uuid4(),
            include_strategic_session_snapshot=False,
        )

    reserve.assert_not_awaited()
    assert exc_info.value.detail == "Invalid reply_to_message_id"


@pytest.mark.asyncio
async def test_precommit_failure_is_rejected_before_sequence_reservation(db_session):
    project = await _project_with_resident(db_session)

    async def reject(_session, _message_id, _context):
        raise HTTPException(status_code=409, detail="source revision changed")

    with (
        patch(
            "app.project_message_creation.lock_project_and_next_message_sequence",
            new=AsyncMock(),
        ) as reserve,
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_operator_project_message(
            db_session,
            project=project,
            user_id="operator",
            message_type=MessageType.NOTE,
            content="Validate this source.",
            context={},
            workbench_context=None,
            idempotency_key="invalid-source-before-sequence",
            surface=MessageSurface.CONVERSATION,
            include_strategic_session_snapshot=False,
            precommit_validate=reject,
        )

    reserve.assert_not_awaited()
    assert exc_info.value.detail == "source revision changed"


@pytest.mark.asyncio
async def test_precommit_gets_final_message_id_and_activity_skips_resident(db_session):
    project = Project(
        name="Activity message",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    seen_message_id = None

    async def capture(_session, message_id, context):
        nonlocal seen_message_id
        seen_message_id = message_id
        return context

    with patch(
        "app.strategic_resident_checkpointing.initialize_strategic_resident",
        new=AsyncMock(side_effect=AssertionError("activity send touched Resident")),
    ) as initialize:
        result = await create_operator_project_message(
            db_session,
            project=project,
            user_id="operator",
            message_type=MessageType.NOTE,
            content="Routine activity.",
            context={},
            workbench_context=None,
            idempotency_key="activity-no-resident",
            surface=MessageSurface.ACTIVITY,
            include_strategic_session_snapshot=False,
            precommit_validate=capture,
        )

    assert seen_message_id == result.message.id
    initialize.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StrategicResident)
            .where(StrategicResident.project_id == project.id)
        )
        == 0
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="owner-fence serialization of concurrent same-project sends requires PostgreSQL row locks",
)
@pytest.mark.asyncio
async def test_slow_precommit_serializes_concurrent_send_without_deadlock(db_session):
    # The resident owner fence (SELECT ... FOR UPDATE on the project's Resident
    # row) is acquired up front and held across precommit_validate, because the
    # in-lock reference re-check (see _revalidate_message_context) must not race
    # a competing commit. A concurrent same-project send therefore SERIALIZES
    # behind an in-flight slow precommit — it blocks on the exclusive fence until
    # the first send releases, then both complete deadlock-free with monotonic
    # sequences. (It must NOT skip ahead: that only happens on SQLite, where
    # FOR UPDATE is a no-op, which is why this test is PostgreSQL-only.)
    project = await _project_with_resident(db_session)
    project_id = project.id
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_precommit(_session, _message_id, context):
        entered.set()
        await release.wait()
        return context

    async def first_send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="First message validates slowly.",
                idempotency_key="slow-validation-first",
                precommit_validate=slow_precommit,
            )

    async def second_send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="Second message serializes behind the owner fence.",
                idempotency_key="fast-validation-second",
            )

    first = asyncio.create_task(first_send())
    await asyncio.wait_for(entered.wait(), timeout=2)
    # While the first send is parked in its slow precommit holding the exclusive
    # owner fence, a second same-project send must block on that fence: it cannot
    # skip ahead and must not have committed.
    second = asyncio.create_task(second_send())
    await asyncio.sleep(0.1)
    assert not second.done()
    assert not first.done()
    release.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second),
        timeout=5,
    )

    # First acquired the fence first, so it commits sequence 1; the serialized
    # second send commits sequence 2. Both succeed — no deadlock, no lost send.
    assert first_result.message.sequence == 1
    assert second_result.message.sequence == 2


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="idempotency advisory-lock concurrency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_concurrent_same_identity_runs_precommit_once_and_adopts_winner(
    db_session,
):
    project = await _project_with_resident(db_session)
    project_id = project.id
    entered = asyncio.Event()
    release = asyncio.Event()
    precommit_calls = 0

    async def precommit(_session, _message_id, context):
        nonlocal precommit_calls
        precommit_calls += 1
        entered.set()
        await release.wait()
        return context

    async def send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="Recover this accepted message exactly once.",
                idempotency_key="concurrent-same-identity",
                precommit_validate=precommit,
            )

    first = asyncio.create_task(send())
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(send())
    await asyncio.sleep(0.05)
    assert precommit_calls == 1
    release.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second),
        timeout=5,
    )

    assert first_result.message.id == second_result.message.id
    assert {first_result.status, second_result.status} == {"created", "duplicate"}
    assert precommit_calls == 1
    async with TestingSessionLocal() as verify:
        count = await verify.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project_id)
        )
    assert count == 1


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="strategic-spawn row-lock concurrency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_send_and_strategic_spawn_complete_without_deadlock(db_session):
    project = await _project_with_resident(db_session)
    project_id = project.id
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn_started = asyncio.Event()

    async def slow_precommit(_session, _message_id, context):
        entered.set()
        await release.wait()
        return context

    async def send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="Serialize this send with a background wake.",
                idempotency_key="send-vs-strategic-spawn",
                precommit_validate=slow_precommit,
            )

    async def strategic_spawn():
        async with TestingSessionLocal() as session:
            spawn_started.set()
            wake = await coalesce_shadow_resident_turn(
                session,
                project_id=project_id,
                lane="background",
                trigger_kind="project_change",
                project_change_cursor={"latency_regression": 1},
            )
            await session.commit()
            return wake

    send_task = asyncio.create_task(send())
    await asyncio.wait_for(entered.wait(), timeout=2)
    spawn_task = asyncio.create_task(strategic_spawn())
    await asyncio.wait_for(spawn_started.wait(), timeout=2)
    await asyncio.sleep(0.05)
    release.set()
    send_result, spawn_result = await asyncio.wait_for(
        asyncio.gather(send_task, spawn_task),
        timeout=5,
    )

    assert send_result.message.sequence == 1
    assert spawn_result.task_id is not None
    assert spawn_result.state in {"queued", "coalesced"}
    async with TestingSessionLocal() as verify:
        messages = (
            (
                await verify.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(messages) == 1


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="generic-message foreign-key lock concurrency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_steering_send_and_generic_event_complete_without_deadlock(db_session):
    # Real concurrency, no synthetic parking. An operator steering send and a
    # routine system event target the same project; they SERIALIZE on the
    # Resident owner fence but must both COMMIT deadlock-free.
    #
    # The earlier version parked the send mid-transaction holding the fence
    # (via a patched allocator + release Event) to force a specific interleave.
    # On real PostgreSQL that park is a Python `await` while holding an
    # exclusive row lock, which the server cannot see as a lock wait — so a
    # concurrent op that needs the fence blocks forever with no deadlock
    # detection. That is a test-harness artifact: production holds the fence
    # only for the brief message-creation critical section, never across an
    # unbounded await. Driving both operations as genuine concurrent tasks
    # exercises the same fence contention without the un-observable park.
    project = await _project_with_resident(db_session)
    project_id = project.id

    async def send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="Steer while a routine project event is recorded.",
                idempotency_key="steering-send-vs-generic-event",
                message_type=MessageType.FEEDBACK,
            )

    async def generic_event():
        async with TestingSessionLocal() as session:
            message = await post_event_message(
                session,
                project_id,
                event_type="failure_alert",
                summary="A generic event raced the operator send.",
            )
            assert message is not None
            await session.commit()
            return message

    # A real deadlock would exhaust this generous timeout; correct serialization
    # completes in well under a second.
    send_result, event_result = await asyncio.wait_for(
        asyncio.gather(send(), generic_event()),
        timeout=15,
    )

    assert send_result.message.project_id == project_id
    assert event_result.project_id == project_id


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="first bulk sequence reservation concurrency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_steering_send_and_first_bulk_reservation_complete_without_deadlock(
    db_session,
):
    # Real concurrency, no synthetic parking (see
    # test_steering_send_and_generic_event_complete_without_deadlock for why the
    # patched-allocator park is a harness-only deadlock). An operator steering
    # send and a first bulk sequence reservation run concurrently against the
    # same project; both must complete deadlock-free.
    project = await _project_with_resident(db_session)
    project_id = project.id

    async def send():
        async with TestingSessionLocal() as session:
            return await _send_conversation(
                session,
                project_id,
                content="Steer while strategic completion reserves reply sequences.",
                idempotency_key="steering-send-vs-first-bulk-reservation",
                message_type=MessageType.FEEDBACK,
            )

    async def first_bulk_reservation():
        async with TestingSessionLocal() as session:
            reserved = await reserve_project_message_sequences(
                session,
                project_id,
                count=2,
                lock_timeout_ms=1_500,
            )
            await session.commit()
            return reserved

    send_result, reserved = await asyncio.wait_for(
        asyncio.gather(send(), first_bulk_reservation()),
        timeout=15,
    )

    assert send_result.message.project_id == project_id
    assert len(reserved) == 2


async def _assert_resident_contention_precedes_sequence(
    *,
    project_id: uuid.UUID,
    lock_contended_row,
) -> None:
    row_locked = asyncio.Event()
    release_row = asyncio.Event()
    sequence_called = asyncio.Event()
    real_allocator = lock_project_and_next_message_sequence

    async def holder():
        async with TestingSessionLocal() as session:
            await lock_contended_row(session)
            row_locked.set()
            await release_row.wait()
            await session.commit()

    async def observed_allocator(session, locked_project_id, **kwargs):
        sequence_called.set()
        return await real_allocator(session, locked_project_id, **kwargs)

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(row_locked.wait(), timeout=2)
    with patch(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        new=observed_allocator,
    ):
        async with TestingSessionLocal() as session:
            send_task = asyncio.create_task(
                _send_conversation(
                    session,
                    project_id,
                    content="Do not convoy the message counter on Resident state.",
                    idempotency_key=f"resident-prelock-{uuid.uuid4()}",
                )
            )
            await asyncio.sleep(0.1)
            assert not sequence_called.is_set()
            release_row.set()
            await asyncio.wait_for(send_task, timeout=5)
    await asyncio.wait_for(holder_task, timeout=2)
    assert sequence_called.is_set()


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="Resident row-lock latency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_resident_lock_contention_happens_before_sequence(db_session):
    project = await _project_with_resident(db_session)

    async def lock_resident(session):
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project.id)
            .with_for_update()
        )

    await _assert_resident_contention_precedes_sequence(
        project_id=project.id,
        lock_contended_row=lock_resident,
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="Resident Task-pointer lock latency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_resident_task_pointer_contention_happens_before_sequence(db_session):
    project = await _project_with_resident(db_session)
    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
        project_change_cursor={"runner_state": 1},
    )
    await db_session.commit()
    assert wake.task_id is not None

    async def lock_pointer_task(session):
        await session.execute(
            select(Task).where(Task.id == wake.task_id).with_for_update()
        )

    await _assert_resident_contention_precedes_sequence(
        project_id=project.id,
        lock_contended_row=lock_pointer_task,
    )
