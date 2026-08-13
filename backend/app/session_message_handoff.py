"""Preserve unresolved operator messages when a Workday session concludes.

Session-scoped message identity prevents one resident worker from claiming another
worker's conversation. Ending that session must not, however, turn unanswered
operator input into a synthetic response. This module therefore distinguishes
messages with durable answer evidence from messages that still need attention:

* answered messages are resolved and remain associated with the historical
  session;
* unanswered messages are left open, released from the ended session, and reset
  to pending so the next resident can claim them;
* unanswered QUESTION/FEEDBACK messages attempt to re-arm the ordinary strategic
  refresh marker as a fallback when no replacement resident starts immediately.

The durable handoff is committed before best-effort cadence arming. This split is
intentional: the arming helper may roll back its transaction on lock contention,
which must never resurrect an ended session's claims on unanswered messages.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageType,
    Project,
    ProjectMessage,
)

logger = logging.getLogger(__name__)

RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY = "_resident_handoff_from_session_id"
RESIDENT_HANDOFF_AT_CONTEXT_KEY = "_resident_handoff_at"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _handling_value(message: ProjectMessage) -> str:
    value = message.handling_state
    return value.value if isinstance(value, MessageHandlingState) else str(value)


def _message_type(message: ProjectMessage) -> MessageType | None:
    value = message.message_type
    if isinstance(value, MessageType):
        return value
    try:
        return MessageType(value)
    except (TypeError, ValueError):
        return None


async def _apply_handoff_mutations(
    session: AsyncSession,
    project_id: uuid.UUID,
    resident_session_id: str | None,
) -> tuple[int, tuple[MessageType, str] | None]:
    """Apply message-release mutations to the open session without committing.

    Locks the Project row (canonical Project→ProjectMessage order), classifies
    each open message as answered or unanswered, and writes the mutations into
    the current transaction. Returns ``(changed_count, rearm_request)`` where
    ``rearm_request`` is the ``(MessageType, content)`` pair for the oldest
    pending QUESTION/FEEDBACK message, or ``None`` when no re-arm is needed.

    Callers are responsible for committing. Use ``_run_handoff_rearm`` after the
    commit to perform the best-effort cadence arm without endangering durability.
    """
    if not resident_session_id:
        return 0, None

    # Keep this guard for mixed-revision rollout safety.
    if not hasattr(ProjectMessage, "resident_session_id"):
        return 0, None

    await session.execute(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    messages = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.resident_session_id == resident_session_id,
                    ProjectMessage.status == "open",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not messages:
        return 0, None

    message_ids = [message.id for message in messages]
    messages_by_id = {message.id: message for message in messages}
    strategic_replies = (
        await session.execute(
            select(
                ProjectMessage.reply_to_message_id,
                ProjectMessage.resident_session_id,
                ProjectMessage.from_task_id,
            ).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.reply_to_message_id.in_(message_ids),
                ProjectMessage.from_type == "strategic",
            )
        )
    ).all()
    replied_to_ids: set[uuid.UUID] = set()
    for reply_to_id, reply_session_id, reply_task_id in strategic_replies:
        source = messages_by_id.get(reply_to_id)
        if source is None:
            continue
        # Prefer the strongest provenance signal available. Legacy strategic
        # replies may have neither field; their canonical writer identity still
        # makes them valid answer evidence. Explicitly mismatched evidence does
        # not.
        if reply_session_id is not None:
            if reply_session_id == resident_session_id:
                replied_to_ids.add(reply_to_id)
            continue
        if reply_task_id is not None:
            if (
                source.handled_by_task_id is not None
                and reply_task_id == source.handled_by_task_id
            ):
                replied_to_ids.add(reply_to_id)
            continue
        # Only truly provenance-free legacy Strategic replies reach this path.
        replied_to_ids.add(reply_to_id)

    now = _utcnow_naive()
    answered_count = 0
    released_count = 0
    rearm_candidates: list[ProjectMessage] = []
    # Local import keeps this low-level handoff module independent at import
    # time while reusing the coordinator's exact claim-identity reset contract.
    from app.conversation_coordinator import (
        reset_conversation_claim_for_session_handoff,
    )

    for message in messages:
        answered = (
            _handling_value(message) == MessageHandlingState.RESPONDED.value
            or bool(message.resolution)
            or message.id in replied_to_ids
        )
        if answered:
            message.status = "resolved"
            message.handling_state = MessageHandlingState.RESPONDED
            message.conversation_claim_expires_at = None
            message.resolution = message.resolution or "session_handoff_answered"
            message.resolved_at = message.resolved_at or now
            answered_count += 1
        else:
            message.context = {
                **dict(message.context or {}),
                RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY: resident_session_id,
                RESIDENT_HANDOFF_AT_CONTEXT_KEY: now.isoformat(),
            }
            message.resident_session_id = None
            message.handling_state = MessageHandlingState.PENDING
            message.handled_by_task_id = None
            # Terminal authority can no longer complete this claim. Retaining
            # its immutable membership would poison a replacement Resident's
            # broad Activity claim whenever the old group also contained an
            # excluded row, so every unanswered released row starts a fresh
            # exact claim under the replacement generation.
            reset_conversation_claim_for_session_handoff(message)
            message.resolution = None
            message.resolved_at = None
            released_count += 1
            if _message_type(message) in (MessageType.QUESTION, MessageType.FEEDBACK):
                rearm_candidates.append(message)
        session.add(message)

    rearm_request: tuple[MessageType, str] | None = None
    if rearm_candidates:
        # One marker is sufficient because the open-message payload carries every
        # released row. Prefer the oldest pending message for deterministic
        # steering text while request_strategic_refresh coalesces concurrent arms.
        candidate = min(
            rearm_candidates,
            key=lambda message: (message.created_at, str(message.id)),
        )
        candidate_type = _message_type(candidate)
        if candidate_type is not None:
            rearm_request = (candidate_type, candidate.content)

    changed_count = answered_count + released_count
    logger.info(
        "[SESSION_HANDOFF] staged %d open message(s) for session %s (project %s): "
        "%d answered, %d released",
        changed_count,
        resident_session_id,
        project_id,
        answered_count,
        released_count,
    )
    return changed_count, rearm_request


async def _run_handoff_rearm(
    session: AsyncSession,
    project_id: uuid.UUID,
    resident_session_id: str | None,
    rearm_request: tuple[MessageType, str] | None,
    *,
    replacement_resident_active: bool = False,
) -> None:
    """Best-effort cadence arm after the handoff mutations have been committed.

    A rollback inside ``maybe_arm_operator_refresh`` must never resurrect an ended
    session's claims on messages — those are already committed. This helper is
    always called after the durability boundary so rearm failure is isolated.
    """
    if rearm_request is None:
        return
    from app.strategic_trigger import maybe_arm_operator_refresh

    candidate_type, candidate_content = rearm_request
    try:
        outcome = await maybe_arm_operator_refresh(
            session,
            project_id,
            candidate_type,
            message_content=candidate_content,
            resident_session_active=replacement_resident_active,
        )
        logger.info(
            "[SESSION_HANDOFF] re-arm outcome %s for released session %s (project %s)",
            outcome,
            resident_session_id,
            project_id,
        )
    except Exception:
        # The handoff mutations are already durable. Leave the session usable and
        # report only the failed fallback arm.
        try:
            await session.rollback()
        except Exception:
            pass
        logger.warning(
            "[SESSION_HANDOFF] re-arm failed for released session %s (project %s)",
            resident_session_id,
            project_id,
            exc_info=True,
        )


async def handoff_session_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    resident_session_id: str | None,
    *,
    replacement_resident_active: bool = False,
) -> int:
    """Hand open messages from an ended resident session to the next owner.

    Answer evidence is deliberately durable and narrow: an open message is
    considered answered when its handling state is already ``responded``, it
    has a stored resolution, or a canonical strategic child reply points to it.
    When a child reply carries resident-session or producer-task evidence, that
    evidence must match the message being handed off. Those rows are repaired
    to the normal resolved shape.

    Every other open row remains open. Its ended-session association and stale
    claim identity are cleared, and its handling state returns to ``pending``.
    The row changes are committed before QUESTION/FEEDBACK rows make a separate,
    best-effort cadence-arm attempt. A replacement resident can therefore see
    the same rows even if cadence arming rolls back or is unavailable.

    The Project row is locked before message rows to preserve the repository's
    canonical Project -> ProjectMessage mutation order. Returns the number of
    changed messages. A non-zero result has already been durably committed.
    """
    changed_count, rearm_request = await _apply_handoff_mutations(
        session, project_id, resident_session_id
    )
    if changed_count:
        # This is the durability boundary. request_strategic_refresh may call
        # session.rollback() after bounded lock contention; cadence failure must
        # not restore the ended session's claims on unanswered messages.
        await session.commit()
    await _run_handoff_rearm(
        session,
        project_id,
        resident_session_id,
        rearm_request,
        replacement_resident_active=replacement_resident_active,
    )
    return changed_count


async def repair_stale_session_message_handoffs(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Retry handoff for open rows still owned by an older resident session.

    A session-ending transition normally commits its message release atomically.
    This repair remains necessary for rows stranded by older deployments and for
    a stop/cap transition that deliberately commits after a caught staging error.
    Every ensure excludes the DB-authoritative active session, then rechecks that
    authority around each stale id so a replacement cannot lose its own rows.
    """
    if not hasattr(ProjectMessage, "resident_session_id"):
        return 0

    from app.strategic_resident_session import (
        read_resident_session,
        resident_session_is_active,
    )

    locked_project = (
        (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )
        .scalars()
        .one_or_none()
    )
    authoritative_active_id: str | None = None
    if locked_project is not None:
        active_blob = read_resident_session(locked_project.vision)
        if resident_session_is_active(active_blob):
            authoritative_active_id = (
                str(active_blob.get("session_id") or "") or None
            )

    stale_ids_query = select(ProjectMessage.resident_session_id).where(
        ProjectMessage.project_id == project_id,
        ProjectMessage.status == "open",
        ProjectMessage.resident_session_id.is_not(None),
    )
    if authoritative_active_id:
        stale_ids_query = stale_ids_query.where(
            ProjectMessage.resident_session_id != authoritative_active_id
        )
    stale_session_ids = (
        (
            await session.execute(
                stale_ids_query.distinct().order_by(
                    ProjectMessage.resident_session_id.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    # Candidate discovery is only a snapshot. Release its lock, then establish
    # a fresh Project authority boundary per handoff because each handoff commits.
    await session.commit()

    repaired = 0
    for stale_session_id in stale_session_ids:
        if not stale_session_id:
            continue
        try:
            current_project = (
                (
                    await session.execute(
                        select(Project)
                        .where(Project.id == project_id)
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                )
                .scalars()
                .one_or_none()
            )
            current_active_id: str | None = None
            if current_project is not None:
                current_blob = read_resident_session(current_project.vision)
                if resident_session_is_active(current_blob):
                    current_active_id = (
                        str(current_blob.get("session_id") or "") or None
                    )
            if str(stale_session_id) == current_active_id:
                await session.commit()
                continue
            changed_count = await handoff_session_messages(
                session,
                project_id,
                str(stale_session_id),
                replacement_resident_active=bool(current_active_id),
            )
            repaired += changed_count
            if changed_count == 0:
                # The handoff helper intentionally leaves a no-op transaction
                # to its caller. This repair loop owns the per-ID project lock,
                # so release it before checking the next stale candidate.
                await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.warning(
                "[SESSION_HANDOFF] repair failed for stale session %s "
                "(project %s); a later resident ensure will retry",
                stale_session_id,
                project_id,
                exc_info=True,
            )
    await session.commit()
    return repaired
