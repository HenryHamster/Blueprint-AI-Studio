"""Shared storage helpers for per-operator message preference state."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import MessagesNoisePrefs


async def get_messages_noise_prefs_row(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    *,
    for_update: bool = False,
    populate_existing: bool = False,
) -> MessagesNoisePrefs | None:
    stmt = select(MessagesNoisePrefs).where(
        MessagesNoisePrefs.user_id == user_id,
        MessagesNoisePrefs.project_id == project_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
        populate_existing = True
    if populate_existing:
        stmt = stmt.execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mutate_messages_noise_prefs(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Lock, merge, and flush the shared JSON prefs row.

    Existing rows are locked before read/modify/write. If two requests race to
    create the first row, the loser rolls back and retries as an update.
    """

    for attempt in range(2):
        row = await get_messages_noise_prefs_row(
            session,
            user_id,
            project_id,
            for_update=True,
        )
        existing = dict(row.prefs or {}) if row is not None else {}
        next_prefs = mutate(existing)

        if row is None:
            row = MessagesNoisePrefs(
                user_id=user_id,
                project_id=project_id,
                prefs=next_prefs,
            )
        else:
            row.prefs = next_prefs
            flag_modified(row, "prefs")
        session.add(row)

        try:
            await session.flush()
            return next_prefs
        except IntegrityError:
            await session.rollback()
            if attempt == 0:
                continue
            raise

    raise RuntimeError("Failed to update message prefs")
