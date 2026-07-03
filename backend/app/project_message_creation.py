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
    persist_strategic_workbench_update,
    workbench_context_for_strategic_session_snapshot,
    workbench_state_exceeds_max_bytes,
)

OPERATOR_CONTEXT_MAX_BYTES = 50_000

# Operator-authored context remains free-form for legacy callers, but the keys
# owned by the workbench contract are bounded before they become durable hidden
# message state.
OPERATOR_CONTEXT_TEXT_CAPS = {
    "user_event_type": 120,
    "reply_to": 160,
    "reply_preview": 240,
    "answer": 2_000,
    "resolution": 2_000,
    "direction_id": 160,
    "task_id": 160,
    "source_task_id": 160,
    "source_message_id": 160,
    "dedupe_key": 240,
}
RESERVED_OPERATOR_CONTEXT_KEYS = frozenset(
    {"event_type", "strategic_session", "operator_command", "check_in"}
)


@dataclass(frozen=True)
class OperatorMessageCreateResult:
    message: ProjectMessage
    status: str
    arming: str
    message_event: dict[str, Any] | None = None

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
    for reserved_key in RESERVED_OPERATOR_CONTEXT_KEYS - {"event_type"}:
        context.pop(reserved_key, None)
    if user_supplied_event_type is not None:
        context["user_event_type"] = user_supplied_event_type
    for key, max_chars in OPERATOR_CONTEXT_TEXT_CAPS.items():
        value = context.get(key)
        if value is not None:
            context[key] = _compact_context_text(value, max_chars=max_chars)
    return context


def _compact_context_text(value: Any, *, max_chars: int) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:max_chars]


def _ensure_context_size(context: dict[str, Any], *, compact: bool) -> None:
    separators = ((",", ":") if compact else None)
    if len(json.dumps(context, separators=separators, default=str)) > OPERATOR_CONTEXT_MAX_BYTES:
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
    enforce_raw_content_max: bool = False,
    sync_local_continuation_marker: bool = False,
) -> OperatorMessageCreateResult:
    """Create or idempotently return an operator-authored ProjectMessage."""

    project_id = project.id
    trimmed_content = content.strip()
    if not trimmed_content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    content_for_limit = content if enforce_raw_content_max else trimmed_content
    if len(content_for_limit) > MESSAGE_CONTENT_MAX:
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
        workbench_write = await persist_strategic_workbench_update(
            session,
            user_id,
            project_id,
            workbench_context or {},
            sync_local_continuation_marker=sync_local_continuation_marker,
            arm_interaction_mode=False,
            commit=False,
        )
        stored_workbench = workbench_write.state
        workbench_context_for_snapshot = None
        if workbench_context is not None:
            workbench_context_for_snapshot = (
                workbench_context_for_strategic_session_snapshot(
                    stored_workbench,
                    workbench_context,
                )
            )

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
