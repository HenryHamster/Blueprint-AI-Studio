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


async def handoff_session_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    resident_session_id: str | None,
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
    if not resident_session_id:
        return 0

    # Keep this guard for mixed-revision rollout safety.
    if not hasattr(ProjectMessage, "resident_session_id"):
        return 0

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
        return 0

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
    for message in messages:
        answered = (
            _handling_value(message) == MessageHandlingState.RESPONDED.value
            or bool(message.resolution)
            or message.id in replied_to_ids
        )
        if answered:
            message.status = "resolved"
            message.handling_state = MessageHandlingState.RESPONDED
            message.resolution = message.resolution or "session_handoff_answered"
            message.resolved_at = message.resolved_at or now
            answered_count += 1
        else:
            message.resident_session_id = None
            message.handling_state = MessageHandlingState.PENDING
            message.handled_by_task_id = None
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
    # This is the durability boundary. request_strategic_refresh may call
    # session.rollback() after bounded lock contention; cadence failure must not
    # restore the ended session's claims. Existing callers only invoke handoff
    # after committing the session-ending transition, so this commit remains a
    # deliberately separate follow-up transaction.
    await session.commit()

    if rearm_request is not None:
        from app.strategic_trigger import maybe_arm_operator_refresh

        candidate_type, candidate_content = rearm_request
        try:
            outcome = await maybe_arm_operator_refresh(
                session,
                project_id,
                candidate_type,
                message_content=candidate_content,
                resident_session_active=False,
            )
            logger.info(
                "[SESSION_HANDOFF] re-arm outcome %s for released session %s (project %s)",
                outcome,
                resident_session_id,
                project_id,
            )
        except Exception:
            # The handoff is already durable. Leave the session usable for the
            # caller and report only the failed fallback arm.
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

    logger.info(
        "[SESSION_HANDOFF] changed %d open message(s) for session %s (project %s): "
        "%d answered, %d released",
        changed_count,
        resident_session_id,
        project_id,
        answered_count,
        released_count,
    )
    return changed_count
