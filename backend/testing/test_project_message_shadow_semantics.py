"""Focused acceptance semantics for the operator-message Resident shadow hook."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    ProjectMessageAttachmentUpload,
    StrategicResident,
    Task,
    _utcnow,
)
from app.project_message_creation import create_operator_project_message
from app.routers.admin_project_strategic_session import (
    _consume_message_attachment_uploads,
)
from app.strategic_resident_turns import (
    coalesce_shadow_resident_turn as real_coalesce_shadow_resident_turn,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident


async def _project_with_resident(
    session,
    *,
    engine_owner: str = "legacy",
) -> tuple[Project, StrategicResident]:
    project = Project(
        name="Message shadow semantics",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    resident = await initialize_strategic_resident(session, project.id)
    resident.engine_owner = engine_owner
    session.add(resident)
    await session.commit()
    await session.refresh(project)
    await session.refresh(resident)
    return project, resident


async def _create_message(
    session,
    project: Project,
    *,
    message_type: MessageType = MessageType.NOTE,
    content: str,
    idempotency_key: str,
    surface: MessageSurface,
    reply_to_message_id: uuid.UUID | None = None,
    context: dict | None = None,
    precommit_validate=None,
):
    return await create_operator_project_message(
        session,
        project=project,
        user_id="operator",
        message_type=message_type,
        content=content,
        context=context or {},
        workbench_context=None,
        idempotency_key=idempotency_key,
        surface=surface,
        reply_to_message_id=reply_to_message_id,
        include_strategic_session_snapshot=False,
        precommit_validate=precommit_validate,
    )


@pytest.mark.asyncio
async def test_activity_message_commits_without_resident_or_shadow_task(db_session):
    project = Project(
        name="Activity only",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    with (
        patch(
            "app.strategic_resident_checkpointing.initialize_strategic_resident",
            new=AsyncMock(side_effect=AssertionError("activity initialized Resident")),
        ) as initialize,
        patch(
            "app.strategic_resident_turns.coalesce_shadow_resident_turn",
            new=AsyncMock(side_effect=AssertionError("activity coalesced Resident")),
        ) as coalesce,
    ):
        result = await _create_message(
            db_session,
            project,
            content="Routine project activity.",
            idempotency_key="activity-shadow-skip",
            surface=MessageSurface.ACTIVITY,
        )

    assert result.status == "created"
    assert result.message.surface == MessageSurface.ACTIVITY
    initialize.assert_not_awaited()
    coalesce.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StrategicResident)
            .where(StrategicResident.project_id == project.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_legacy_shadow_failure_fails_open_with_truthful_wake_state(db_session):
    project, _resident = await _project_with_resident(db_session)

    with patch(
        "app.strategic_resident_turns.coalesce_shadow_resident_turn",
        new=AsyncMock(side_effect=RuntimeError("shadow comparison unavailable")),
    ) as coalesce:
        result = await _create_message(
            db_session,
            project,
            message_type=MessageType.FEEDBACK,
            content="Prioritize the operator-visible rollout.",
            idempotency_key="legacy-shadow-fail-open",
            surface=MessageSurface.CONVERSATION,
        )

    assert result.status == "created"
    assert result.arming == "steering"
    assert result.wakeup_state == "queued"
    assert result.message.context["_resident_wakeup_state"] == "queued"
    coalesce.assert_awaited_once()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == "legacy-shadow-fail-open",
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_legacy_resident_prelock_failure_accepts_without_shadow_task(db_session):
    project, _resident = await _project_with_resident(db_session)

    with patch(
        "app.strategic_resident_message_locking.lock_shadow_resident_turn_state",
        new=AsyncMock(side_effect=RuntimeError("Resident row busy")),
    ):
        result = await _create_message(
            db_session,
            project,
            message_type=MessageType.NOTE,
            content="Accept this while shadow state is unavailable.",
            idempotency_key="legacy-prelock-fail-open",
            surface=MessageSurface.CONVERSATION,
        )

    assert result.status == "created"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_strategic_owner_prelock_failure_rejects_before_message_insert(db_session):
    project, _resident = await _project_with_resident(
        db_session,
        engine_owner="strategic_resident",
    )

    with (
        patch(
            "app.strategic_resident_message_locking.lock_shadow_resident_turn_state",
            new=AsyncMock(side_effect=RuntimeError("Resident row busy")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _create_message(
            db_session,
            project,
            message_type=MessageType.NOTE,
            content="This requires an authoritative successor.",
            idempotency_key="strategic-owner-prelock-fail-closed",
            surface=MessageSurface.CONVERSATION,
        )

    assert exc_info.value.status_code == 503
    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == "strategic-owner-prelock-fail-closed",
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_strategic_owner_shadow_failure_rolls_back_all_message_effects(
    db_session,
):
    project, _resident = await _project_with_resident(
        db_session,
        engine_owner="strategic_resident",
    )
    question = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Should the rollout remain paused?",
        status="open",
    )
    attachment_id = uuid.uuid4()
    storage_uri = (
        f"gs://blueprint-test/message-attachments/"
        f"{project.id}/{attachment_id}/evidence.txt"
    )
    upload = ProjectMessageAttachmentUpload(
        attachment_id=attachment_id,
        project_id=project.id,
        actor_user_id="operator",
        storage_uri=storage_uri,
        filename="evidence.txt",
        content_type="text/plain",
        expected_size_bytes=12,
        expires_at=_utcnow() + timedelta(minutes=15),
    )
    db_session.add_all([question, upload])
    await db_session.commit()
    await db_session.refresh(question)

    context = {
        "attachments": [
            {
                "attachment_id": str(attachment_id),
                "storage_uri": storage_uri,
                "filename": "evidence.txt",
            }
        ]
    }

    async def consume_attachments(session, message_id, current_context):
        return await _consume_message_attachment_uploads(
            session,
            project_id=project.id,
            actor_user_id="operator",
            message_id=message_id,
            context=current_context,
        )

    with (
        patch(
            "app.strategic_resident_turns.coalesce_shadow_resident_turn",
            new=AsyncMock(side_effect=RuntimeError("authoritative enqueue failed")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _create_message(
            db_session,
            project,
            content="Keep it paused until verification passes.",
            idempotency_key="strategic-owner-shadow-failure",
            surface=MessageSurface.CONVERSATION,
            reply_to_message_id=question.id,
            context=context,
            precommit_validate=consume_attachments,
        )

    assert exc_info.value.status_code == 503
    assert "was not accepted" in str(exc_info.value.detail)
    await db_session.rollback()
    await db_session.refresh(question)
    await db_session.refresh(upload)
    assert question.status == "open"
    assert question.resolution is None
    assert question.handling_state == MessageHandlingState.PENDING
    assert upload.consumed_at is None
    assert upload.message_id is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == "strategic-owner-shadow-failure",
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_duplicate_replay_does_not_add_another_shadow_wake(db_session):
    project, _resident = await _project_with_resident(db_session)
    coalesce = AsyncMock(wraps=real_coalesce_shadow_resident_turn)

    with patch(
        "app.strategic_resident_turns.coalesce_shadow_resident_turn",
        new=coalesce,
    ):
        first = await _create_message(
            db_session,
            project,
            content="Persist this operator note once.",
            idempotency_key="shadow-replay-once",
            surface=MessageSurface.CONVERSATION,
        )
        duplicate = await _create_message(
            db_session,
            project,
            content="Persist this operator note once.",
            idempotency_key="shadow-replay-once",
            surface=MessageSurface.CONVERSATION,
        )

    assert first.status == "created"
    assert duplicate.status == "duplicate"
    assert duplicate.message.id == first.message.id
    coalesce.assert_awaited_once()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 1
    )
