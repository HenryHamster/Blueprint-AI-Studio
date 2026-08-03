"""Durable write-ahead reconciliation for rendered ArtifactRef promotions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.admin_helpers import PROJECT_WRITE_ROLES
from app.models import (
    ArtifactRef,
    ArtifactRefPromotionIntent,
    Project,
    ProjectMember,
    ProjectMessageAttachmentUpload,
    ProjectTool,
    _utcnow,
)
from app.storage import (
    ARTIFACT_REF_GCS_RETRY_DEADLINE_SECONDS,
    artifact_ref_render_uri,
    delete_artifact_ref_render_candidate,
    inspect_artifact_ref_render_candidate,
    parse_artifact_uri,
    promote_artifact_ref_render,
)
from app.task_locking import lock_project_or_429

logger = logging.getLogger(__name__)

_BATCH_SIZE = 16
_MAX_BACKOFF_SECONDS = 3600
_PROMOTION_EFFECT_BUDGET_SECONDS = ARTIFACT_REF_GCS_RETRY_DEADLINE_SECONDS * 6
_PROMOTION_LEASE_SECONDS = max(600, _PROMOTION_EFFECT_BUDGET_SECONDS + 180)
_PROMOTION_SETTLE_SECONDS = _PROMOTION_LEASE_SECONDS + 300
_PROMOTION_PROBE_SECONDS = 30
_PROMOTION_REQUIRED_ABSENT_PROBES = 2
_MAX_TRACKED_PROMOTIONS = 64
_tracked_promotion_tasks: set[asyncio.Task[ArtifactRef | None]] = set()
_tracked_promotion_generation = 0

def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(_MAX_BACKOFF_SECONDS, 30 * (2 ** max(0, attempt_count - 1)))
    return timedelta(seconds=seconds)

class ArtifactRefPromotionBusy(RuntimeError):
    """Another request or reconciler exclusively owns this promotion."""


@dataclass(frozen=True)
class PromotionClaim:
    intent_id: uuid.UUID
    claim_id: uuid.UUID
    lease_expires_at: datetime


async def prepare_artifact_ref_promotion_intent(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    revision: int,
    receipt_id: uuid.UUID,
    actor_user_id: str,
    source_storage_uri: str,
    candidate_storage_uri: str,
    expected_size_bytes: int,
    expected_sha256: str,
    render_width: int | None,
    render_height: int | None,
) -> PromotionClaim:
    """Durably claim one copy effect under Project-first canonical locks."""

    checksum = str(expected_sha256 or "").strip().lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError("render promotion requires lowercase SHA-256")
    if expected_size_bytes < 1:
        raise ValueError("render promotion requires a positive object size")
    if render_width is not None and render_width < 1:
        raise ValueError("render width must be positive")
    if render_height is not None and render_height < 1:
        raise ValueError("render height must be positive")
    if candidate_storage_uri != artifact_ref_render_uri(
        project_id, tool_id, revision, receipt_id
    ):
        raise ValueError("render promotion candidate identity mismatch")
    _, source_name = parse_artifact_uri(source_storage_uri)
    source_prefix = f"message-attachments/{project_id}/{receipt_id}/"
    if not source_name.startswith(source_prefix) or "/" in source_name[len(source_prefix):]:
        raise ValueError("render promotion source identity mismatch")

    now = _utcnow()
    await lock_project_or_429(session, project_id)
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_user_id,
                ProjectMember.role.in_(PROJECT_WRITE_ROLES),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    tool = (
        await session.execute(
            select(ProjectTool).where(
                ProjectTool.id == tool_id,
                ProjectTool.project_id == project_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    receipt = (
        await session.execute(
            select(ProjectMessageAttachmentUpload).where(
                ProjectMessageAttachmentUpload.attachment_id == receipt_id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        raise PermissionError("actor no longer has member access")
    if tool is None or tool.kind != "whiteboard":
        raise LookupError("Whiteboard tool not found")
    if tool.content_revision != revision:
        raise ValueError("Whiteboard revision changed before promotion")
    if (
        receipt is None
        or receipt.project_id != project_id
        or receipt.actor_user_id != actor_user_id
        or receipt.storage_uri != source_storage_uri
        or receipt.expected_size_bytes != expected_size_bytes
        or str(receipt.content_type or "").lower()
        not in {"image/png", "image/jpeg", "image/webp"}
        or receipt.message_id is not None
        or receipt.artifact_ref_id is not None
        or receipt.consumed_at is not None
        or receipt.expires_at <= now
    ):
        raise ValueError("render upload receipt is no longer available")

    intent = (
        await session.execute(
            select(ArtifactRefPromotionIntent)
            .where(ArtifactRefPromotionIntent.receipt_id == receipt_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    logical = (
        await session.execute(
            select(ArtifactRefPromotionIntent)
            .where(
                ArtifactRefPromotionIntent.project_id == project_id,
                ArtifactRefPromotionIntent.tool_id == tool_id,
                ArtifactRefPromotionIntent.revision == revision,
                ArtifactRefPromotionIntent.state != "settled",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if intent is not None and intent.state == "settled":
        raise ValueError("render upload receipt has already been reconciled")
    if intent is not None and intent.state == "owned":
        raise ArtifactRefPromotionBusy("render upload receipt is already owned")
    if logical is not None and (intent is None or logical.id != intent.id):
        raise ArtifactRefPromotionBusy("this Whiteboard revision is already promoting")
    if intent is not None and (
        intent.lease_expires_at is None or intent.lease_expires_at > now
    ):
        raise ArtifactRefPromotionBusy("render promotion is already in progress")
    if intent is None:
        intent = ArtifactRefPromotionIntent(
            project_id=project_id,
            tool_id=tool_id,
            revision=revision,
            receipt_id=receipt_id,
            actor_user_id=actor_user_id,
            source_storage_uri=source_storage_uri,
            candidate_storage_uri=candidate_storage_uri,
            expected_size_bytes=expected_size_bytes,
            expected_sha256=checksum,
            render_width=render_width,
            render_height=render_height,
        )
        session.add(intent)
        await session.flush()
    elif (
        intent.project_id != project_id
        or intent.tool_id != tool_id
        or intent.revision != revision
        or intent.actor_user_id != actor_user_id
        or intent.source_storage_uri != source_storage_uri
        or intent.candidate_storage_uri != candidate_storage_uri
        or intent.expected_size_bytes != expected_size_bytes
        or intent.expected_sha256 != checksum
        or intent.render_width != render_width
        or intent.render_height != render_height
    ):
        raise ValueError("render promotion intent identity mismatch")
    settle_until = now + timedelta(seconds=_PROMOTION_SETTLE_SECONDS)
    # The receipt is promotion authority. Keep it alive through uncertain
    # effect settlement so generic upload reapers cannot invalidate the WAL.
    # Do this only after conflict checks so a redundant receipt does not leave
    # dirty ORM state when its logical promotion is already owned elsewhere.
    receipt.expires_at = max(
        receipt.expires_at,
        settle_until + timedelta(seconds=_PROMOTION_PROBE_SECONDS),
    )
    session.add(receipt)
    claim_id = uuid.uuid4()
    intent.state = "copying"
    intent.claim_id = claim_id
    intent.attempt_count += 1
    intent.lease_expires_at = now + timedelta(seconds=_PROMOTION_LEASE_SECONDS)
    intent.effect_deadline_at = now + timedelta(
        seconds=_PROMOTION_EFFECT_BUDGET_SECONDS
    )
    intent.settle_until = settle_until
    intent.next_attempt_at = now + timedelta(seconds=5)
    intent.destination_generation = None
    intent.settlement_absent_count = 0
    intent.last_error = None
    intent.updated_at = now
    session.add(intent)
    await session.commit()
    return PromotionClaim(intent.id, claim_id, intent.lease_expires_at)


async def record_artifact_ref_promotion_effect(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    generation: int,
) -> bool:
    """Record the observed copy generation only for a live route claim."""

    now = _utcnow()
    identity = await session.get(ArtifactRefPromotionIntent, intent_id)
    if identity is None:
        return False
    project = (
        await session.execute(
            select(Project).where(Project.id == identity.project_id).with_for_update()
        )
    ).scalar_one_or_none()
    intent = (
        await session.execute(
            select(ArtifactRefPromotionIntent)
            .where(ArtifactRefPromotionIntent.id == intent_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        project is None
        or intent is None
        or intent.state != "copying"
        or intent.claim_id != claim_id
        or intent.lease_expires_at is None
        or intent.lease_expires_at <= now
    ):
        await session.rollback()
        return False
    intent.state = "promoted"
    intent.destination_generation = int(generation)
    intent.updated_at = now
    session.add(intent)
    await session.commit()
    return True


async def expire_artifact_ref_promotion_claim(
    *,
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    error: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> bool:
    """Release only a definitively stopped route claim for immediate safe retry."""

    now = _utcnow()
    async with session_factory() as session:
        identity = await session.get(ArtifactRefPromotionIntent, intent_id)
        if identity is None:
            return False
        await session.execute(
            select(Project).where(Project.id == identity.project_id).with_for_update()
        )
        intent = (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(
                    ArtifactRefPromotionIntent.id == intent_id,
                    ArtifactRefPromotionIntent.claim_id == claim_id,
                    ArtifactRefPromotionIntent.state.in_(["copying", "promoted"]),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            return False
        intent.lease_expires_at = now - timedelta(microseconds=1)
        intent.next_attempt_at = now
        intent.last_error = error[:2_000]
        intent.updated_at = now
        session.add(intent)
        await session.commit()
        return True


async def reconcile_one_artifact_ref_promotion(
    *,
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    revision: int,
    receipt_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> ArtifactRef | None:
    """Adopt an exact landed copy and atomically finalize its ArtifactRef."""

    now = _utcnow()
    async with session_factory() as session:
        identity = (
            await session.execute(
                select(ArtifactRefPromotionIntent).where(
                    ArtifactRefPromotionIntent.project_id == project_id,
                    ArtifactRefPromotionIntent.tool_id == tool_id,
                    ArtifactRefPromotionIntent.revision == revision,
                    ArtifactRefPromotionIntent.receipt_id == receipt_id,
                )
            )
        ).scalar_one_or_none()
        if identity is None:
            return None
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
        if project is None:
            return None
        member = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == identity.actor_user_id,
                    ProjectMember.role.in_(PROJECT_WRITE_ROLES),
                ).with_for_update()
            )
        ).scalar_one_or_none()
        tool = (
            await session.execute(
                select(ProjectTool).where(
                    ProjectTool.id == tool_id,
                    ProjectTool.project_id == project_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        receipt = (
            await session.execute(
                select(ProjectMessageAttachmentUpload).where(
                    ProjectMessageAttachmentUpload.attachment_id == receipt_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        intent = (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(ArtifactRefPromotionIntent.id == identity.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            return None
        if intent.state == "owned" and intent.artifact_ref_id is not None:
            return await session.get(ArtifactRef, intent.artifact_ref_id)
        if intent.state not in {"copying", "promoted"}:
            return None
        if (
            intent.claim_id is None
            or intent.lease_expires_at is None
            or intent.lease_expires_at <= now
            or intent.candidate_storage_uri
            != artifact_ref_render_uri(project_id, tool_id, revision, receipt_id)
        ):
            return None
        if (
            member is None
            or tool is None
            or tool.kind != "whiteboard"
            or tool.content_revision != revision
            or receipt is None
            or receipt.project_id != project_id
            or receipt.actor_user_id != intent.actor_user_id
            or receipt.storage_uri != intent.source_storage_uri
            or receipt.message_id is not None
            or receipt.artifact_ref_id is not None
            or receipt.consumed_at is not None
        ):
            intent.state = "cleanup"
            intent.next_attempt_at = max(now, intent.lease_expires_at)
            intent.updated_at = now
            session.add(intent)
            await session.commit()
            return None
        claim_id = intent.claim_id
        state = intent.state
        candidate_uri = intent.candidate_storage_uri
        expected_size = intent.expected_size_bytes
        checksum = intent.expected_sha256
        actor_user_id = intent.actor_user_id
        source_uri = intent.source_storage_uri
        width = intent.render_width
        height = intent.render_height
        intent_id = intent.id
        generation = intent.destination_generation
        await session.commit()

    if state == "copying":
        generation = await inspect_artifact_ref_render_candidate(
            candidate_uri,
            expected_size_bytes=expected_size,
            expected_sha256=checksum,
        )
        if generation is None:
            async with session_factory() as session:
                current = (
                    await session.execute(
                        select(ArtifactRefPromotionIntent)
                        .where(
                            ArtifactRefPromotionIntent.id == intent_id,
                            ArtifactRefPromotionIntent.state == "copying",
                            ArtifactRefPromotionIntent.claim_id == claim_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if current is not None:
                    current.next_attempt_at = _utcnow() + timedelta(seconds=5)
                    session.add(current)
                    await session.commit()
            return None
        async with session_factory() as session:
            if not await record_artifact_ref_promotion_effect(
                session,
                intent_id=intent_id,
                claim_id=claim_id,
                generation=generation,
            ):
                return None

    async with session_factory() as session:
        from app.artifact_refs import create_tool_artifact_ref

        ref = await create_tool_artifact_ref(
            session,
            project_id=project_id,
            tool_id=tool_id,
            actor_user_id=actor_user_id,
            expected_revision=revision,
            rendered_image={
                "attachment_id": str(receipt_id),
                "storage_uri": source_uri,
                "_durable_storage_uri": candidate_uri,
                "checksum": checksum,
                "width": width,
                "height": height,
            },
            promotion_intent_id=intent_id,
            promotion_claim_id=claim_id,
        )
        await session.commit()
        await session.refresh(ref)
        return ref


def start_tracked_artifact_ref_promotion(
    *,
    intent_id: uuid.UUID,
    claim_id: uuid.UUID,
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    revision: int,
    receipt_id: uuid.UUID,
    source_storage_uri: str,
    expected_size_bytes: int,
    expected_sha256: str,
) -> asyncio.Task[ArtifactRef | None]:
    """Keep copy/finalization alive after the interactive 202 wait budget."""

    if len(_tracked_promotion_tasks) >= _MAX_TRACKED_PROMOTIONS:
        raise ArtifactRefPromotionBusy("render promotion capacity is full")
    runtime_generation = _tracked_promotion_generation

    async def _run() -> ArtifactRef | None:
        try:
            promotion = await promote_artifact_ref_render(
                source_storage_uri,
                project_id=project_id,
                tool_id=tool_id,
                revision=revision,
                attachment_id=receipt_id,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
            )
            if runtime_generation != _tracked_promotion_generation:
                return None
            async with AsyncSessionLocal() as session:
                if runtime_generation != _tracked_promotion_generation:
                    return None
                recorded = await record_artifact_ref_promotion_effect(
                    session,
                    intent_id=intent_id,
                    claim_id=claim_id,
                    generation=promotion.generation,
                )
            if not recorded:
                return None
            if runtime_generation != _tracked_promotion_generation:
                return None
            return await reconcile_one_artifact_ref_promotion(
                project_id=project_id,
                tool_id=tool_id,
                revision=revision,
                receipt_id=receipt_id,
            )
        except Exception as exc:
            if runtime_generation == _tracked_promotion_generation:
                await expire_artifact_ref_promotion_claim(
                    intent_id=intent_id,
                    claim_id=claim_id,
                    error=str(exc),
                )
            raise

    task = asyncio.create_task(_run())
    _tracked_promotion_tasks.add(task)

    def _settled(done: asyncio.Task[ArtifactRef | None]) -> None:
        _tracked_promotion_tasks.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning(
                "Tracked ArtifactRef promotion failed; WAL reconciliation remains active",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_settled)
    return task


async def shutdown_tracked_artifact_ref_promotions(
    *, timeout_seconds: float = 5.0
) -> None:
    """Fence, cancel, and boundedly drain detached interactive promotions."""

    global _tracked_promotion_generation
    _tracked_promotion_generation += 1
    tasks = list(_tracked_promotion_tasks)
    for task in tasks:
        task.cancel()
    if not tasks:
        _tracked_promotion_tasks.clear()
        return
    completion = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(
            asyncio.shield(completion),
            timeout=max(0.01, float(timeout_seconds)),
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "Tracked ArtifactRef promotions exceeded the %.1fs shutdown bound",
            timeout_seconds,
        )
        completion.add_done_callback(lambda done: done.exception())
    finally:
        _tracked_promotion_tasks.clear()


async def _claim_promotion_intent(
    session_factory: async_sessionmaker[AsyncSession],
    intent_id: uuid.UUID,
) -> PromotionClaim | None:
    now = _utcnow()
    async with session_factory() as session:
        identity = await session.get(ArtifactRefPromotionIntent, intent_id)
        if identity is None:
            return None
        await session.execute(
            select(Project).where(Project.id == identity.project_id).with_for_update()
        )
        intent = (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(ArtifactRefPromotionIntent.id == intent_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if intent is None or intent.state in {"owned", "settled"}:
            return None
        if intent.next_attempt_at > now:
            return None
        if intent.state in {"copying", "promoted", "cleanup_claimed"} and (
            intent.lease_expires_at is not None and intent.lease_expires_at > now
        ):
            return None

        exact_ref = (
            await session.execute(
                select(ArtifactRef).where(
                    ArtifactRef.project_id == intent.project_id,
                    ArtifactRef.tool_id == intent.tool_id,
                    ArtifactRef.revision == intent.revision,
                )
            )
        ).scalar_one_or_none()
        if exact_ref is not None:
            retrieval = exact_ref.retrieval_metadata or {}
            if (
                str(retrieval.get("storage_uri") or "")
                == intent.candidate_storage_uri
                and str(retrieval.get("attachment_id") or "")
                == str(intent.receipt_id)
            ):
                intent.state = "owned"
                intent.artifact_ref_id = exact_ref.id
                intent.completed_at = now
                intent.claim_id = None
                intent.lease_expires_at = None
                intent.last_error = None
                intent.updated_at = now
                session.add(intent)
                await session.commit()
                return None

        claim_id = uuid.uuid4()
        intent.state = "cleanup_claimed"
        intent.claim_id = claim_id
        intent.attempt_count += 1
        intent.lease_expires_at = now + timedelta(seconds=_PROMOTION_LEASE_SECONDS)
        intent.updated_at = now
        session.add(intent)
        await session.commit()
        return PromotionClaim(intent.id, claim_id, intent.lease_expires_at)


async def _finish_promotion_probe(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claim: PromotionClaim,
    observed_object: bool,
    error: str | None,
) -> bool:
    now = _utcnow()
    async with session_factory() as session:
        identity = await session.get(ArtifactRefPromotionIntent, claim.intent_id)
        if identity is None:
            return False
        await session.execute(
            select(Project).where(Project.id == identity.project_id).with_for_update()
        )
        intent = (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(
                    ArtifactRefPromotionIntent.id == claim.intent_id,
                    ArtifactRefPromotionIntent.state == "cleanup_claimed",
                    ArtifactRefPromotionIntent.claim_id == claim.claim_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is None:
            return False
        intent.claim_id = None
        intent.lease_expires_at = None
        intent.updated_at = now
        if error:
            intent.state = "cleanup"
            intent.last_error = error[:2_000]
            intent.next_attempt_at = now + _retry_delay(intent.attempt_count)
        else:
            intent.last_error = None
            if observed_object:
                # A separately committed later probe must establish absence.
                intent.settlement_absent_count = 0
            else:
                intent.settlement_absent_count += 1
            if (
                not observed_object
                and intent.settle_until is not None
                and now >= intent.settle_until
                and intent.settlement_absent_count >= _PROMOTION_REQUIRED_ABSENT_PROBES
            ):
                intent.state = "settled"
                intent.completed_at = now
            else:
                intent.state = "cleanup"
                intent.next_attempt_at = now + timedelta(
                    seconds=_PROMOTION_PROBE_SECONDS
                )
        session.add(intent)
        await session.commit()
        return True


async def drain_artifact_ref_promotion_intents(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    limit: int = _BATCH_SIZE,
) -> dict[str, int]:
    """Reconcile expired/cleanup copy effects without holding DB locks over GCS."""

    now = _utcnow()
    async with session_factory() as session:
        intent_ids = list(
            (
                await session.execute(
                    select(ArtifactRefPromotionIntent.id)
                    .where(
                        ArtifactRefPromotionIntent.state.notin_(["owned", "settled"]),
                        ArtifactRefPromotionIntent.next_attempt_at <= now,
                    )
                    .order_by(ArtifactRefPromotionIntent.next_attempt_at.asc())
                    .limit(max(1, min(int(limit), 100)))
                )
            ).scalars()
        )
    claimed = completed = deferred = 0
    for intent_id in intent_ids:
        async with session_factory() as session:
            identity = await session.get(ArtifactRefPromotionIntent, intent_id)
            if identity is None:
                continue
            project_id = identity.project_id
            tool_id = identity.tool_id
            revision = identity.revision
            receipt_id = identity.receipt_id
        try:
            adopted = await reconcile_one_artifact_ref_promotion(
                project_id=project_id,
                tool_id=tool_id,
                revision=revision,
                receipt_id=receipt_id,
                session_factory=session_factory,
            )
            if adopted is not None:
                completed += 1
                continue
        except Exception:
            logger.exception("ArtifactRef promotion adoption failed for %s", intent_id)
        async with session_factory() as session:
            current = await session.get(ArtifactRefPromotionIntent, intent_id)
            if current is None:
                continue
            if (
                current.state in {"copying", "promoted", "cleanup_claimed"}
                and current.lease_expires_at is not None
                and current.lease_expires_at > _utcnow()
            ):
                deferred += 1
                continue
        claim = await _claim_promotion_intent(session_factory, intent_id)
        if claim is None:
            continue
        claimed += 1
        async with session_factory() as session:
            intent = await session.get(ArtifactRefPromotionIntent, intent_id)
            if intent is None or intent.claim_id != claim.claim_id:
                continue
            uri = intent.candidate_storage_uri
            expected_size = intent.expected_size_bytes
            checksum = intent.expected_sha256
        observed = False
        error = None
        try:
            generation = await inspect_artifact_ref_render_candidate(
                uri,
                expected_size_bytes=expected_size,
                expected_sha256=checksum,
            )
            if generation is not None:
                observed = True
                await delete_artifact_ref_render_candidate(
                    uri, generation=generation
                )
        except Exception as exc:
            logger.exception("ArtifactRef promotion reconciliation failed for %s", intent_id)
            error = str(exc)
        finished = await _finish_promotion_probe(
            session_factory,
            claim=claim,
            observed_object=observed,
            error=error,
        )
        if not finished or error:
            deferred += 1
        else:
            async with session_factory() as session:
                state = (await session.get(ArtifactRefPromotionIntent, intent_id)).state
            completed += int(state == "settled")
            deferred += int(state != "settled")
    return {"claimed": claimed, "completed": completed, "deferred": deferred}


async def mark_project_artifact_ref_promotions_cleanup(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Fence every nonterminal project copy before the Project row is deleted."""

    now = _utcnow()
    intents = list(
        (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(
                    ArtifactRefPromotionIntent.project_id == project_id,
                    ArtifactRefPromotionIntent.state.notin_(["settled"]),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for intent in intents:
        intent.state = "cleanup"
        # Preserve a live copy lease: cleanup cannot conclude before the bounded
        # external effect and its settlement horizon have elapsed.
        intent.next_attempt_at = max(
            now,
            intent.lease_expires_at or now,
        )
        intent.artifact_ref_id = None
        intent.completed_at = None
        intent.updated_at = now
        session.add(intent)
    return len(intents)



__all__ = [
    "ArtifactRefPromotionBusy",
    "drain_artifact_ref_promotion_intents",
    "expire_artifact_ref_promotion_claim",
    "mark_project_artifact_ref_promotions_cleanup",
    "prepare_artifact_ref_promotion_intent",
    "record_artifact_ref_promotion_effect",
    "shutdown_tracked_artifact_ref_promotions",
    "start_tracked_artifact_ref_promotion",
]
