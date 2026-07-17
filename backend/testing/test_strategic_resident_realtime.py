"""Focused failure and project-scope tests for Resident realtime projection."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Project, ProjectMessage, Task, TaskStatus
from app.strategic_resident_realtime import broadcast_resident_reconciliation
from app.strategic_resident_reconciliation import StrategicResidentReconcileOutcome


def _factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=session.bind,
        expire_on_commit=False,
        autoflush=False,
    )


def _outcome(
    *,
    reply_message_id: uuid.UUID | None = None,
    responded_message_ids: tuple[uuid.UUID, ...] = (),
) -> StrategicResidentReconcileOutcome:
    return StrategicResidentReconcileOutcome(
        checkpoint_id=uuid.uuid4(),
        checkpoint_version=2,
        checkpoint_hash="a" * 64,
        reply_message_id=reply_message_id,
        promoted_task_id=None,
        idempotent_replay=False,
        responded_message_ids=responded_message_ids,
    )


class _BrokenSessionContext:
    async def __aenter__(self):
        raise RuntimeError("committed rows are temporarily unavailable")

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_realtime_refetch_failure_is_best_effort(mocker):
    broadcast = mocker.patch(
        "app.strategic_resident_realtime.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    await broadcast_resident_reconciliation(
        lambda: _BrokenSessionContext(),
        project_id=uuid.uuid4(),
        turn_task_id=uuid.uuid4(),
        outcome=_outcome(responded_message_ids=(uuid.uuid4(),)),
    )

    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_realtime_never_projects_cross_project_rows(db_session, mocker):
    target = Project(
        name="Realtime target",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    foreign = Project(
        name="Realtime foreign",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add_all([target, foreign])
    await db_session.flush()
    foreign_task = Task(
        project_id=foreign.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={},
    )
    db_session.add(foreign_task)
    await db_session.flush()
    foreign_message = ProjectMessage(
        project_id=foreign.id,
        sequence=1,
        surface="conversation",
        handling_state="responded",
        handled_by_task_id=foreign_task.id,
        from_type="user",
        content="Never project me into another project.",
    )
    db_session.add(foreign_message)
    await db_session.commit()
    broadcast = mocker.patch(
        "app.strategic_resident_realtime.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    await broadcast_resident_reconciliation(
        _factory(db_session),
        project_id=target.id,
        turn_task_id=foreign_task.id,
        outcome=_outcome(
            reply_message_id=foreign_message.id,
            responded_message_ids=(foreign_message.id,),
        ),
    )

    broadcast.assert_not_awaited()
