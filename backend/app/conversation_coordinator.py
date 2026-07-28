"""Durable foreground conversation admission for Strategic Residents.

The message row is the canonical operator input. ``conversation_claim_id``
groups the exact rows admitted to one foreground turn, while the short lease
and worker-generation fence make that turn reclaimable after a provider,
process, task, or runner failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageHandlingState, ProjectMessage
from app.time_utils import _utcnow


@dataclass(frozen=True)
class ConversationClaim:
    claim_id: uuid.UUID
    rows: tuple[ProjectMessage, ...]
    lease_expires_at: datetime
    attempt: int


def legacy_conversation_claim_incarnation(
    task_id: uuid.UUID,
    worker_generation: int,
) -> uuid.UUID:
    """Stable rollout identity for pre-incarnation Resident agents."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            "blueprint:resident-conversation-claim:"
            f"{task_id}:{worker_generation}"
        ),
    )


def _session_scope(session_id: str) -> object:
    return or_(
        ProjectMessage.resident_session_id == session_id,
        ProjectMessage.resident_session_id.is_(None),
    )


def _message_ordering() -> tuple[object, ...]:
    return (
        ProjectMessage.sequence.asc().nulls_last(),
        ProjectMessage.created_at.asc(),
        ProjectMessage.id.asc(),
    )


async def claim_operator_messages(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
    message_ids: list[uuid.UUID],
    lease_seconds: int,
) -> ConversationClaim | None:
    """Claim exactly one canonical operator batch under the caller's Project lock.

    The caller supplies rows from the read-only poll. The lock plus exact ID
    validation closes the poll/claim race. A live claim owned by another worker
    is not stealable; an expired claim is reclaimed as the same logical turn.
    """
    if not message_ids:
        return None
    now = _utcnow()
    lease_seconds = min(300, max(15, int(lease_seconds)))
    requested = list(dict.fromkeys(message_ids))
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.id.in_(requested),
                    ProjectMessage.from_type == "user",
                    ProjectMessage.status == "open",
                    _session_scope(session_id),
                )
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(requested):
        return None
    ordered = tuple(by_id[value] for value in requested)

    existing_ids = {
        row.conversation_claim_id
        for row in ordered
        if row.conversation_claim_id is not None
    }
    if len(existing_ids) > 1:
        return None
    existing_claim_id = next(iter(existing_ids), None)
    claimed_rows = tuple(
        row for row in ordered if row.conversation_claim_id is not None
    )
    legacy_incarnation = legacy_conversation_claim_incarnation(
        task_id,
        worker_generation,
    )
    same_owner = bool(claimed_rows) and all(
        row.handling_state == MessageHandlingState.CLAIMED
        and row.handled_by_task_id == task_id
        and row.conversation_claim_generation == worker_generation
        and (
            row.conversation_claim_incarnation == incarnation_id
            or (
                row.conversation_claim_incarnation is None
                and incarnation_id == legacy_incarnation
            )
        )
        for row in claimed_rows
    )
    lease_live = any(
        row.conversation_claim_expires_at is not None
        and row.conversation_claim_expires_at > now
        for row in claimed_rows
    )
    if existing_claim_id is not None and lease_live and not same_owner:
        return None

    claim_id = existing_claim_id or uuid.uuid4()
    reclaimed = existing_claim_id is not None and not same_owner
    expires_at = now + timedelta(seconds=lease_seconds)
    attempt = max((row.conversation_claim_attempt for row in ordered), default=0)
    if existing_claim_id is None or reclaimed:
        attempt += 1
    attempt = max(1, attempt)

    for row in ordered:
        row.handling_state = MessageHandlingState.CLAIMED
        row.handled_by_task_id = task_id
        row.conversation_claim_id = claim_id
        row.conversation_claim_generation = worker_generation
        row.conversation_claim_incarnation = incarnation_id
        row.conversation_claim_expires_at = expires_at
        row.conversation_claim_attempt = attempt
        row.updated_at = now
        session.add(row)
    await session.flush()
    return ConversationClaim(
        claim_id=claim_id,
        rows=ordered,
        lease_expires_at=expires_at,
        attempt=attempt,
    )


async def renew_conversation_claim(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
    lease_seconds: int = 45,
) -> bool:
    """Renew only an exact live claim owner; stale generations fail closed."""
    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.conversation_claim_id == claim_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    legacy_incarnation = legacy_conversation_claim_incarnation(
        task_id,
        worker_generation,
    )
    if not rows or any(
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or row.conversation_claim_generation != worker_generation
        or (
            row.conversation_claim_incarnation != incarnation_id
            and not (
                row.conversation_claim_incarnation is None
                and incarnation_id == legacy_incarnation
            )
        )
        or row.conversation_claim_expires_at is None
        or row.conversation_claim_expires_at <= now
        for row in rows
    ):
        return False
    expires_at = now + timedelta(seconds=min(300, max(15, int(lease_seconds))))
    for row in rows:
        row.conversation_claim_incarnation = incarnation_id
        row.conversation_claim_expires_at = expires_at
        row.updated_at = now
        session.add(row)
    await session.flush()
    return True
