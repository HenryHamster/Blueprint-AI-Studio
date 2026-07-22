"""Commit helpers for the legacy autonomous strategic writers.

Historically these helpers fenced legacy strategic writes against a project's
resident *authority row* (``StrategicResident.engine_owner`` /
``authority_generation``) during the resident/legacy engine cutover. The
strategic-resident subsystem has since been fully removed and its schema dropped
(migration ``0657``), so there is a single engine and no authority contention.

The helpers are retained with their original names/signatures so the many
call sites need only re-point their import here. Post-resident they collapse to:

* ``commit_legacy_authorized`` — a plain ``session.commit()`` that preserves the
  deferred external-notification flush-on-success / discard-on-error semantics
  the fenced commit provided (this behavior is unrelated to resident authority).
* ``lock_and_validate_legacy_authority`` — a no-op that returns the sentinel
  legacy generation ``0``; the parameters are accepted for call-site
  compatibility with the former CAS validator.

The two exception types are kept so existing ``except`` clauses still resolve;
they are no longer raised for authority reasons now that there is one engine.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession


class ResidentOwnershipTransitionError(RuntimeError):
    """Retained for callers that still catch legacy-authority transition errors."""


class LegacyEngineAuthorityExpired(ResidentOwnershipTransitionError):
    """Retained exception type; no longer raised now that there is a single engine."""


async def lock_and_validate_legacy_authority(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    expected_generation: int | None = None,
) -> int:
    """No-op post-resident: a single engine, no authority row to validate.

    Returns the sentinel legacy generation ``0``. ``expected_generation`` is
    accepted (and ignored) for compatibility with the former CAS validator.
    """
    return 0


async def commit_legacy_authorized(
    session: AsyncSession,
    project_id: uuid.UUID,
    expected_generation: int | None = None,
) -> None:
    """Commit the session, preserving deferred external-notification semantics.

    Post-resident there is no authority to validate, so this is a plain commit
    that keeps the same deferred-notification flush-on-success / discard-on-error
    behavior the fenced commit had.
    """
    try:
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        finally:
            # asyncio cancellation is a BaseException; like a commit failure it
            # must drop staged external effects so a reused session cannot emit
            # them later.
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
