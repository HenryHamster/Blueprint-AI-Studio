"""Shared last-seen timestamp helpers for message and activity surfaces."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessagesLastSeen, _utcnow


def timestamp_ms_to_naive_utc(timestamp_ms: int) -> datetime:
    try:
        return datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=timezone.utc
        ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid timestamp") from exc


def naive_utc_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)


async def upsert_messages_last_seen(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    new_ts: datetime,
) -> datetime:
    """Persist last-seen monotonically so stale tabs cannot re-mark old messages unread."""
    updated = await session.execute(
        update(MessagesLastSeen)
        .where(
            MessagesLastSeen.user_id == user_id,
            MessagesLastSeen.project_id == project_id,
            MessagesLastSeen.last_seen_at < new_ts,
        )
        .values(last_seen_at=new_ts, updated_at=_utcnow())
    )
    if updated.rowcount:
        return new_ts

    stored_ts = await _select_last_seen_at(session, user_id, project_id)
    if stored_ts is not None:
        return stored_ts

    session.add(
        MessagesLastSeen(
            user_id=user_id,
            project_id=project_id,
            last_seen_at=new_ts,
            updated_at=new_ts,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        updated = await session.execute(
            update(MessagesLastSeen)
            .where(
                MessagesLastSeen.user_id == user_id,
                MessagesLastSeen.project_id == project_id,
                MessagesLastSeen.last_seen_at < new_ts,
            )
            .values(last_seen_at=new_ts, updated_at=_utcnow())
        )
        if updated.rowcount:
            return new_ts
        stored_ts = await _select_last_seen_at(session, user_id, project_id)
        if stored_ts is not None:
            return stored_ts
        raise
    return new_ts


async def _select_last_seen_at(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
) -> datetime | None:
    return (
        await session.execute(
            select(MessagesLastSeen.last_seen_at).where(
                MessagesLastSeen.user_id == user_id,
                MessagesLastSeen.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
