"""Shared durable creation for operator-authored project messages."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MESSAGE_CONTENT_MAX, MessageType, Project, ProjectMessage
from app.project_message_projection import message_event_payload
from app.strategic_trigger import ARMING_STEERING, maybe_arm_operator_refresh
from app.workbench_state import (
    build_message_strategic_session_snapshot,
    upsert_strategic_workbench_state_with_previous,
    workbench_state_exceeds_max_bytes,
)


@dataclass(frozen=True)
class OperatorMessageCreateResult:
    message: ProjectMessage
    status: str
    arming: str
    message_event: dict[str, str] | None = None

    @property
    def message_id(self) -> str:
        return str(self.message.id)


def parse_operator_message_type(
    raw_message_type: Any,
    *,
    default_note_for_empty: bool = True,
) -> MessageType:
    raw_value = str(raw_message_type or ("NOTE" if default_note_for_empty else "")).upper()
    try:
        return MessageType(raw_value)
    except ValueError:
        valid_types = ", ".join(t.value for t in MessageType)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message_type. Must be one of: {valid_types}",
        ) from None


def _operator_context(raw_context: dict[str, Any] | None) -> dict[str, Any]:
    context = dict(raw_context or {})
    user_supplied_event_type = context.pop("event_type", None)
    if user_supplied_event_type is not None:
        context["user_event_type"] = str(user_supplied_event_type)[:120]
    return context


def _ensure_context_size(context: dict[str, Any], *, compact: bool) -> None:
    separators = ((",", ":") if compact else None)
    if len(json.dumps(context, separators=separators, default=str)) > 50_000:
        raise HTTPException(status_code=400, detail="Context too large (max 50KB)")


async def _message_for_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
) -> ProjectMessage | None:
    return (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def create_operator_project_message(
    session: AsyncSession,
    *,
    project: Project,
    user_id: str,
    message_type: MessageType,
    content: str,
    context: dict[str, Any] | None,
    workbench_context: dict[str, Any] | None,
    idempotency_key: str | None,
    persist_empty_workbench_context: bool = False,
    compact_context_size: bool = True,
) -> OperatorMessageCreateResult:
    """Create or idempotently return an operator-authored ProjectMessage."""

    project_id = project.id
    trimmed_content = content.strip()
    if not trimmed_content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if len(trimmed_content) > MESSAGE_CONTENT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds {MESSAGE_CONTENT_MAX} character limit",
        )
    if workbench_state_exceeds_max_bytes(workbench_context):
        raise HTTPException(
            status_code=400,
            detail="Workbench context too large (max 50KB)",
        )

    idem_key = (idempotency_key or "").strip() or None
    if idem_key:
        if len(idem_key) > 255:
            raise HTTPException(
                status_code=400,
                detail="idempotency_key exceeds 255 characters",
            )
        existing = await _message_for_idempotency_key(session, project_id, idem_key)
        if existing is not None:
            return OperatorMessageCreateResult(
                message=existing,
                status="duplicate",
                arming="duplicate",
            )

    message_context = _operator_context(context)
    _ensure_context_size(message_context, compact=compact_context_size)

    if message_type in (MessageType.BUG_REPORT, MessageType.BLOCKER):
        from app.source_ref_validation import compute_message_citation_warning

        warning = await compute_message_citation_warning(
            session,
            project,
            trimmed_content,
        )
        if warning:
            message_context = {**message_context, "_provenance_warnings": warning}

    workbench_context_for_snapshot = workbench_context
    should_persist_workbench = (
        workbench_context is not None or persist_empty_workbench_context
    )
    if should_persist_workbench:
        (
            _previous_workbench,
            stored_workbench,
        ) = await upsert_strategic_workbench_state_with_previous(
            session,
            user_id,
            project_id,
            workbench_context or {},
        )
        workbench_context_for_snapshot = None
        if workbench_context is not None:
            workbench_context_for_snapshot = {
                **stored_workbench,
                "context_package_id": workbench_context.get("context_package_id"),
            }

    strategic_session = await build_message_strategic_session_snapshot(
        session,
        user_id,
        project_id,
        workbench_context_for_snapshot,
    )
    if strategic_session:
        message_context = {
            **message_context,
            "strategic_session": strategic_session,
        }
    _ensure_context_size(message_context, compact=compact_context_size)

    msg = ProjectMessage(
        project_id=project_id,
        message_type=message_type,
        from_type="user",
        content=trimmed_content,
        context=message_context,
        idempotency_key=idem_key,
    )
    session.add(msg)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idem_key:
            existing = await _message_for_idempotency_key(session, project_id, idem_key)
            if existing is not None:
                return OperatorMessageCreateResult(
                    message=existing,
                    status="duplicate",
                    arming="duplicate",
                )
        raise
    await session.refresh(msg)
    message_id = msg.id
    message_event = message_event_payload(msg)

    arming = await maybe_arm_operator_refresh(
        session,
        project_id,
        message_type,
        message_content=trimmed_content,
    )
    if arming == ARMING_STEERING:
        await session.commit()

    reloaded = await session.get(ProjectMessage, message_id)
    if reloaded is None:
        raise HTTPException(
            status_code=500,
            detail="Created message could not be reloaded",
        )
    await session.refresh(reloaded)
    return OperatorMessageCreateResult(
        message=reloaded,
        status="created",
        arming=arming,
        message_event=message_event,
    )
