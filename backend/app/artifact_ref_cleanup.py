"""Durable project-prefix cleanup for retained ArtifactRef render objects."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.artifact_ref_promotion import drain_artifact_ref_promotion_intents
from app.database import AsyncSessionLocal
from app.models import ArtifactRefCleanupIntent, ArtifactRefPromotionIntent, _utcnow
from app.storage import (
    ARTIFACT_REF_OBJECT_ROOT,
    delete_artifact_ref_project_prefix_best_effort,
)

logger = logging.getLogger(__name__)
_LEASE_SECONDS = 120
_BATCH_SIZE = 16
_MAX_BACKOFF_SECONDS = 3600
_LIFECYCLE_RECONCILE_WAIT_SECONDS = 45
_lifecycle_promotion_drain_task: asyncio.Task[dict[str, int]] | None = None


@dataclass(frozen=True)
class CleanupClaim:
    project_id: uuid.UUID
    claim_id: uuid.UUID
    attempt_count: int


async def ensure_artifact_ref_cleanup_intent(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ArtifactRefCleanupIntent:
    """Stage one cleanup intent in the caller's project-deletion transaction."""

    intent = await session.get(ArtifactRefCleanupIntent, project_id)
    if intent is None:
        intent = ArtifactRefCleanupIntent(
            project_id=project_id,
            object_prefix=f"{ARTIFACT_REF_OBJECT_ROOT}/{project_id}/",
        )
        session.add(intent)
    elif intent.state == "complete":
        intent.state = "pending"
        intent.completed_at = None
        intent.next_attempt_at = _utcnow()
        intent.updated_at = _utcnow()
        session.add(intent)
    return intent


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(_MAX_BACKOFF_SECONDS, 30 * (2 ** max(0, attempt_count - 1)))
    return timedelta(seconds=seconds)


async def _claim_intent(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
) -> CleanupClaim | None:
    now = _utcnow()
    async with session_factory() as session:
        intent = (
            await session.execute(
                select(ArtifactRefCleanupIntent)
                .where(ArtifactRefCleanupIntent.project_id == project_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if intent is None or intent.state == "complete":
            return None
        if intent.state == "processing" and (
            intent.lease_expires_at is not None and intent.lease_expires_at > now
        ):
            return None
        if intent.next_attempt_at > now:
            return None
        pending_promotion = (
            await session.execute(
                select(ArtifactRefPromotionIntent.id)
                .where(
                    ArtifactRefPromotionIntent.project_id == project_id,
                    ArtifactRefPromotionIntent.state.notin_(["owned", "settled"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending_promotion is not None:
            return None
        intent.state = "processing"
        intent.attempt_count += 1
        intent.claim_id = uuid.uuid4()
        intent.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
        intent.updated_at = now
        session.add(intent)
        await session.commit()
        return CleanupClaim(intent.project_id, intent.claim_id, intent.attempt_count)


async def _finish_intent(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    *,
    claim_id: uuid.UUID,
    complete: bool,
    error: str | None,
) -> bool:
    now = _utcnow()
    async with session_factory() as session:
        intent = (
            await session.execute(
                select(ArtifactRefCleanupIntent)
                .where(
                    ArtifactRefCleanupIntent.project_id == project_id,
                    ArtifactRefCleanupIntent.state == "processing",
                    ArtifactRefCleanupIntent.claim_id == claim_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            return False
        intent.lease_expires_at = None
        intent.claim_id = None
        intent.updated_at = now
        if complete:
            intent.state = "complete"
            intent.completed_at = now
            intent.last_error = None
        else:
            intent.state = "pending"
            intent.completed_at = None
            intent.last_error = (error or "cleanup incomplete")[:2_000]
            intent.next_attempt_at = now + _retry_delay(intent.attempt_count)
        session.add(intent)
        await session.commit()
        return True


async def drain_artifact_ref_cleanup_intents(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    limit: int = _BATCH_SIZE,
) -> dict[str, int]:
    """Claim due prefixes, perform bounded storage I/O, and persist outcomes."""

    now = _utcnow()
    async with session_factory() as session:
        project_ids = list(
            (
                await session.execute(
                    select(ArtifactRefCleanupIntent.project_id)
                    .where(
                        ArtifactRefCleanupIntent.state != "complete",
                        ArtifactRefCleanupIntent.next_attempt_at <= now,
                        or_(
                            ArtifactRefCleanupIntent.state == "pending",
                            ArtifactRefCleanupIntent.lease_expires_at.is_(None),
                            ArtifactRefCleanupIntent.lease_expires_at <= now,
                        ),
                    )
                    .order_by(ArtifactRefCleanupIntent.next_attempt_at.asc())
                    .limit(max(1, min(int(limit), 100)))
                )
            ).scalars()
        )
    completed = deferred = 0
    for project_id in project_ids:
        claim = await _claim_intent(session_factory, project_id)
        if claim is None:
            continue
        try:
            result = await delete_artifact_ref_project_prefix_best_effort(project_id)
            complete = bool(result.get("complete"))
            error = None if complete else "bounded cleanup incomplete"
        except Exception as exc:
            logger.exception("ArtifactRef prefix cleanup failed for project %s", project_id)
            complete = False
            error = str(exc)
        await _finish_intent(
            session_factory,
            project_id,
            claim_id=claim.claim_id,
            complete=complete,
            error=error,
        )
        completed += int(complete)
        deferred += int(not complete)
    return {"claimed": completed + deferred, "completed": completed, "deferred": deferred}


async def _await_tracked_promotion_drain() -> bool:
    """Await one strong-referenced drain without cancelling its GCS thread.

    A timed-out worker thread keeps running. Keeping this single task in a
    module registry lets the next lifecycle cycle adopt that work instead of
    launching a duplicate candidate stream.
    """

    global _lifecycle_promotion_drain_task
    task = _lifecycle_promotion_drain_task
    if task is not None and task.done():
        if _lifecycle_promotion_drain_task is task:
            _lifecycle_promotion_drain_task = None
        task.result()
        return True
    if task is None:
        task = asyncio.create_task(drain_artifact_ref_promotion_intents(limit=8))
        _lifecycle_promotion_drain_task = task
    try:
        async with asyncio.timeout(_LIFECYCLE_RECONCILE_WAIT_SECONDS):
            await asyncio.shield(task)
    except TimeoutError:
        return False
    except BaseException:
        if _lifecycle_promotion_drain_task is task and task.done():
            _lifecycle_promotion_drain_task = None
        raise
    if _lifecycle_promotion_drain_task is task:
        _lifecycle_promotion_drain_task = None
    return True


async def artifact_ref_lifecycle_service() -> None:
    """Leader-only bounded recovery, independently supervised from VM cleanup."""

    while True:
        await asyncio.sleep(10)
        try:
            promotions_complete = await _await_tracked_promotion_drain()
            if not promotions_complete:
                logger.warning(
                    "ArtifactRef promotion reconciliation remains in progress"
                )
                continue
            async with asyncio.timeout(45):
                await drain_artifact_ref_cleanup_intents(limit=8)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("ArtifactRef lifecycle cycle exceeded its 45s budget")
        except Exception:
            logger.exception("ArtifactRef lifecycle cycle failed")


async def shutdown_artifact_ref_lifecycle() -> None:
    """Cancel and forget any shielded promotion drain before pool shutdown."""

    global _lifecycle_promotion_drain_task
    task = _lifecycle_promotion_drain_task
    if task is None:
        return
    try:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        if _lifecycle_promotion_drain_task is task:
            _lifecycle_promotion_drain_task = None


__all__ = [
    "artifact_ref_lifecycle_service",
    "drain_artifact_ref_cleanup_intents",
    "ensure_artifact_ref_cleanup_intent",
    "shutdown_artifact_ref_lifecycle",
]
