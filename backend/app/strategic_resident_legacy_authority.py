"""Authority fencing shared by legacy autonomous Resident writers."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, StrategicResident


class ResidentOwnershipTransitionError(RuntimeError):
    """The requested engine transition cannot be completed safely."""


class LegacyEngineAuthorityExpired(ResidentOwnershipTransitionError):
    """A legacy writer lost engine ownership or its observed generation."""


async def lock_and_validate_legacy_authority(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    expected_generation: int | None = None,
) -> int:
    """Lock Project -> Resident and validate a legacy writer's CAS token."""
    # Do not autoflush pending legacy mutations until the Project -> Resident
    # fence is held. An early flush would both write before validation and invert
    # the cutover lock order. Scalar columns also avoid overwriting unflushed
    # Resident scheduling changes staged by legacy shadow wakeups.
    with session.no_autoflush:
        project = (
            await session.execute(
                select(Project.id)
                .where(Project.id == project_id)
                .with_for_update(key_share=True)
            )
        ).scalar_one_or_none()
        if project is None:
            raise LegacyEngineAuthorityExpired("project no longer exists")
        resident_authority = (
            await session.execute(
                select(
                    StrategicResident.engine_owner,
                    StrategicResident.authority_generation,
                )
                .where(StrategicResident.project_id == project_id)
                .with_for_update()
            )
        ).one_or_none()
    if resident_authority is None:
        if expected_generation not in (None, 0):
            raise LegacyEngineAuthorityExpired(
                "legacy engine authority generation has been superseded"
            )
        # Rollout compatibility for projects not reached by the control-plane
        # backfill yet. Generation 0 is reserved for the absent-row state. The
        # held Project lock blocks the canonical initializer until commit; a row
        # observed on the final recheck supersedes this token.
        return 0
    if resident_authority.engine_owner != "legacy":
        raise LegacyEngineAuthorityExpired("legacy engine no longer owns project")
    generation = int(resident_authority.authority_generation)
    # Generation 0 represents the pre-backfill legacy owner. The initial
    # control-plane backfill (or this writer's own shadow-wakeup initialization)
    # materializes that same authority as legacy generation 1; it is not an
    # ownership transition. Every actual transition increments beyond 1, so no
    # cutover generation can be admitted by this compatibility bridge.
    if expected_generation == 0 and generation == 1:
        return generation
    if expected_generation is not None and generation != int(expected_generation):
        raise LegacyEngineAuthorityExpired(
            "legacy engine authority generation has been superseded"
        )
    return generation


async def commit_legacy_authorized(
    session: AsyncSession,
    project_id: uuid.UUID,
    expected_generation: int,
) -> None:
    """Commit only while the observed legacy generation still owns the project."""
    try:
        await lock_and_validate_legacy_authority(
            session,
            project_id,
            expected_generation=expected_generation,
        )
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        finally:
            # asyncio cancellation is a BaseException on supported Python
            # versions. It must drop staged effects just like an authority
            # rejection; otherwise a reused session could emit them later.
            if "_deferred_external_notifications" in session.info:
                from app.notification_dispatch import (
                    discard_deferred_external_notifications,
                )

                discard_deferred_external_notifications(session)
        raise
    if "_deferred_external_notifications" in session.info:
        from app.notification_dispatch import flush_deferred_external_notifications

        flush_deferred_external_notifications(session)


__all__ = [
    "LegacyEngineAuthorityExpired",
    "ResidentOwnershipTransitionError",
    "commit_legacy_authorized",
    "lock_and_validate_legacy_authority",
]
