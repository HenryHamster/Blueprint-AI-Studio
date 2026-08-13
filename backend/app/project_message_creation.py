"""Shared durable creation for operator-authored project messages."""

from __future__ import annotations

import json
import hashlib
import logging
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import resolve_dialect_name
from sqlmodel import select

from app.models import (
    MESSAGE_CONTENT_MAX,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    ProjectMessageSequence,
    _utcnow,
)
from app.project_message_projection import (
    WAKEUP_STATE_CONTEXT_KEY,
    message_event_payload,
)
from app.strategic_trigger import (
    ARMING_NOT_ARMED,
    ARMING_RESIDENT_SESSION,
    ARMING_STEERING,
    maybe_arm_operator_refresh,
    strategic_refresh_pending,
)
from app.workbench_state import (
    build_message_strategic_session_snapshot,
    persist_strategic_workbench_update,
    workbench_context_for_strategic_session_snapshot,
    workbench_state_exceeds_max_bytes,
)

logger = logging.getLogger(__name__)

OPERATOR_CONTEXT_MAX_BYTES = 50_000
_REQUEST_FINGERPRINT_KEY = "_message_request_fingerprint"
_VISIBLE_REQUEST_CONTEXT_KEYS = (
    "source_refs",
    "artifact_refs",
    "attachments",
    "operator_intent",
)
CONVERSATION_EVENT_TYPES = frozenset(
    {
        "auto_fix_escalated",
        "residual_fix_escalated",
        "residual_fix_audit_deferred",
        "remediation_escalated",
        "direction_burn_pause",
        "direction_outcome_scaffolding_escalation",
        "direction_churn_pause",
        "failure_alert",
        "rollback_recommended",
        "fix_loop_divergence_regressed",
        "release_calcifying",
        "release_build_stalled",
        "release_stabilize_stalled",
        "release_ci_unresolved_stall",
        "direction_outcome_stalled",
        "direction_staged_activated",
        "promotion_readiness_reached",
        "strategic_steering_deferred",
        "strategic_steering_acted",
        "capability_trial_stalled",
        "capability_verdict_decided",
        "capability_trial_ambiguous_lift_escalation",
        "question_answer",
        "feedback_reply",
        "capability_proof_gate_graduated_observe",
        "capability_proof_gate_graduated_enforce",
        "mode_knob_graduated_observe",
        "mode_knob_graduated_enforce",
        "criterion_retracted",
        "criterion_superseded",
        "artifact_handoff_failed",
    }
)


def _is_uuid_string(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


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
    {
        "event_type",
        "strategic_session",
        "operator_command",
        "check_in",
        "_resident_activation_prompt",
        "message_role",
        "_workday_result_packet_id",
        "_resident_handoff_from_session_id",
        "_resident_handoff_at",
    }
)


@dataclass(frozen=True)
class OperatorMessageCreateResult:
    message: ProjectMessage
    status: str
    arming: str
    wakeup_state: str
    message_event: dict[str, Any] | None = None

    @property
    def message_id(self) -> str:
        return str(self.message.id)


def parse_operator_message_type(
    raw_message_type: Any,
    *,
    default_note_for_empty: bool = True,
) -> MessageType:
    raw_value = str(
        raw_message_type or ("NOTE" if default_note_for_empty else "")
    ).upper()
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
    separators = (",", ":") if compact else None
    encoded = json.dumps(
        context,
        separators=separators,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    if len(encoded) > OPERATOR_CONTEXT_MAX_BYTES:
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


async def _lock_operator_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
) -> None:
    """Serialize one project/client identity before early transactional work."""

    if resolve_dialect_name(session) != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": (f"project-message:{project_id}:{idempotency_key}")},
    )


async def lock_project_and_next_message_sequence(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    lock_timeout_ms: int | None = None,
) -> int:
    """Allocate one sequence through the canonical parent-first bulk helper."""

    return (
        await reserve_project_message_sequences(
            session,
            project_id,
            count=1,
            lock_timeout_ms=lock_timeout_ms,
        )
    )[0]


async def reserve_project_message_sequences(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    count: int,
    lock_timeout_ms: int | None = None,
) -> list[int]:
    """Reserve a sequence block after locking the parent Project.

    The Project lock must live here, not only in the single-sequence wrapper:
    strategic completion reserves blocks directly, and a first counter-row
    insert otherwise takes its FK lock after the counter uniqueness lock.
    """

    count = int(count)
    if count < 1 or count > 100:
        raise ValueError("message sequence reservation count must be between 1 and 100")

    dialect = resolve_dialect_name(session)
    if dialect == "postgresql":
        if lock_timeout_ms is not None:
            await session.execute(
                text(f"SET LOCAL lock_timeout = '{max(1, int(lock_timeout_ms))}ms'")
            )
    try:
        locked_project_id = await session.scalar(
            select(Project.id)
            .where(Project.id == project_id)
            .with_for_update(read=True, key_share=True)
        )
    except OperationalError as exc:
        await session.rollback()
        if lock_timeout_ms is not None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Message was not accepted because the project conversation "
                    "is busy; retry with the same idempotency key"
                ),
                headers={"Retry-After": "1"},
            ) from exc
        raise
    if locked_project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if dialect == "postgresql":
        try:
            value = await session.scalar(
                text(
                    """
                    INSERT INTO projectmessagesequence AS counter
                      (project_id, last_sequence)
                    SELECT :project_id, coalesce(max(message.sequence), 0) + :count
                    FROM projectmessage AS message
                    WHERE message.project_id = :project_id
                    ON CONFLICT (project_id) DO UPDATE
                    SET last_sequence = greatest(
                      counter.last_sequence + :count,
                      EXCLUDED.last_sequence
                    )
                    RETURNING last_sequence
                    """
                ),
                {"project_id": project_id, "count": count},
            )
        except OperationalError as exc:
            await session.rollback()
            if lock_timeout_ms is not None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Message was not accepted because conversation sequencing "
                        "is busy; retry with the same idempotency key"
                    ),
                    headers={"Retry-After": "1"},
                ) from exc
            raise
        if value is None:
            raise HTTPException(
                status_code=500, detail="Message sequence allocation failed"
            )
        if lock_timeout_ms is not None:
            await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        last_sequence = int(value)
        return list(range(last_sequence - count + 1, last_sequence + 1))

    # SQLite/test fallback. Production PostgreSQL uses the single-statement
    # UPSERT above; SQLite serializes writes at the database level.
    counter = await session.get(ProjectMessageSequence, project_id)
    if counter is None:
        max_sequence = await session.scalar(
            select(func.max(ProjectMessage.sequence)).where(
                ProjectMessage.project_id == project_id
            )
        )
        counter = ProjectMessageSequence(
            project_id=project_id,
            last_sequence=int(max_sequence or 0) + count,
        )
    else:
        counter.last_sequence += count
    session.add(counter)
    await session.flush()
    last_sequence = int(counter.last_sequence)
    return list(range(last_sequence - count + 1, last_sequence + 1))


async def build_project_message(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    sequence: int | None = None,
    surface: MessageSurface | None = None,
    handling_state: MessageHandlingState | None = None,
    **values: Any,
) -> ProjectMessage:
    """Construct one canonical message with a durable project-wide sequence.

    Production writers use this helper instead of constructing ProjectMessage
    directly. This keeps new activity and conversation rows on the same
    allocator while the migration trigger remains only as a mixed-version
    rollback fence.
    """

    context = values.get("context")
    event_type = (
        str(context.get("event_type") or "") if isinstance(context, dict) else ""
    )
    from_type = str(values.get("from_type") or "")
    reply_to_message_id = values.get("reply_to_message_id")
    resolved_surface = surface or (
        MessageSurface.CONVERSATION
        if (
            from_type in {"user", "strategic"}
            or reply_to_message_id is not None
            or event_type in CONVERSATION_EVENT_TYPES
        )
        else MessageSurface.ACTIVITY
    )
    resolved_handling = handling_state or (
        MessageHandlingState.RESPONDED
        if from_type == "strategic"
        else MessageHandlingState.PENDING
    )
    reply_reference_ids: set[uuid.UUID] = set()
    if isinstance(reply_to_message_id, uuid.UUID):
        reply_reference_ids.add(reply_to_message_id)
    if isinstance(context, dict) and isinstance(
        context.get("_resident_reply_covered_message_ids"), list
    ):
        reply_reference_ids.update(
            uuid.UUID(message_id)
            for message_id in context["_resident_reply_covered_message_ids"]
            if isinstance(message_id, str) and _is_uuid_string(message_id)
        )
    has_reply_provenance = False
    if reply_reference_ids:
        has_reply_provenance = (
            await session.scalar(
                select(ProjectMessage.id)
                .where(
                    ProjectMessage.id.in_(reply_reference_ids),
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type.in_(("user", "operator")),
                )
                .limit(1)
            )
            is not None
        )
    is_operator_reply = (
        resolved_surface == MessageSurface.CONVERSATION
        and from_type == "strategic"
        and resolved_handling == MessageHandlingState.RESPONDED
        and has_reply_provenance
    )
    # ``open`` is the model/default legacy value and is never valid for a
    # proven outbound operator reply. Override it even when a caller supplied
    # it explicitly; preserve other terminal states for replay/import callers.
    if is_operator_reply and values.get("status", "open") == "open":
        values["status"] = "resolved"
        if values.get("resolved_at") is None:
            values["resolved_at"] = _utcnow()
    return ProjectMessage(
        project_id=project_id,
        sequence=(
            sequence
            if sequence is not None
            else await lock_project_and_next_message_sequence(session, project_id)
        ),
        surface=resolved_surface,
        handling_state=resolved_handling,
        **values,
    )


def wakeup_state_for_arming(
    arming: str,
    *,
    refresh_was_pending: bool,
) -> str:
    if arming == ARMING_RESIDENT_SESSION:
        return "queued"
    if arming == ARMING_STEERING:
        return "coalesced" if refresh_was_pending else "queued"
    if arming == ARMING_NOT_ARMED:
        return "waiting_for_capacity"
    return "paused"


def stored_wakeup_state(message: ProjectMessage) -> str:
    context = message.context if isinstance(message.context, dict) else {}
    value = context.get(WAKEUP_STATE_CONTEXT_KEY)
    if value in {"queued", "coalesced", "paused", "waiting_for_capacity"}:
        return value
    return "waiting_for_capacity"


def _operator_idempotency_matches(
    message: ProjectMessage,
    *,
    user_id: str,
    message_type: MessageType,
    content: str,
    surface: MessageSurface,
    reply_to_message_id: uuid.UUID | None,
    request_fingerprint: str,
    message_context: dict[str, Any],
) -> bool:
    """Reject key reuse instead of adopting an unrelated durable message."""

    actor = getattr(message, "actor_user_id", None)
    context = message.context if isinstance(message.context, dict) else {}
    stored_fingerprint = context.get(_REQUEST_FINGERPRINT_KEY)
    if stored_fingerprint is not None:
        return bool(
            message.from_type == "user"
            and str(actor or "") == str(user_id)
            and stored_fingerprint == request_fingerprint
        )
    return bool(
        message.from_type == "user"
        and str(actor or "") == str(user_id)
        and message.message_type == message_type
        and message.content == content
        and getattr(message, "surface", surface) == surface
        and getattr(message, "reply_to_message_id", None) == reply_to_message_id
        and {
            key: context.get(key)
            for key in _VISIBLE_REQUEST_CONTEXT_KEYS
            if context.get(key) is not None
        }
        == _request_visible_context(message_context)
    )


def _assert_operator_idempotency_match(
    message: ProjectMessage,
    **expected: Any,
) -> None:
    if not _operator_idempotency_matches(message, **expected):
        raise HTTPException(
            status_code=409,
            detail="idempotency_key already belongs to a different message",
        )


def _request_visible_context(context: dict[str, Any] | None) -> dict[str, Any]:
    source = context if isinstance(context, dict) else {}
    return {
        key: source.get(key)
        for key in _VISIBLE_REQUEST_CONTEXT_KEYS
        if source.get(key) is not None
    }


def _operator_request_fingerprint(
    *,
    user_id: str,
    message_type: MessageType,
    content: str,
    surface: MessageSurface,
    reply_to_message_id: uuid.UUID | None,
    message_context: dict[str, Any],
) -> str:
    payload = {
        "actor_user_id": str(user_id),
        "surface": surface.value,
        "message_type": message_type.value,
        "content": content,
        "reply_to_message_id": str(reply_to_message_id)
        if reply_to_message_id
        else None,
        "context": _request_visible_context(message_context),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _retire_unclaimed_resident_activation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    resident_session_id: str,
    activation_context_key: str,
) -> None:
    """Retire a synthetic opening that real operator intent supersedes.

    The caller already holds the Project row lock shared by every canonical
    operator-message writer.  Lock the activation row as well so a concurrent
    Fast claim either wins first (and is left alone) or observes the terminal
    activation after this real message commits.  Both mutations remain in the
    same transaction: a rejected real message cannot consume the opening.
    """

    activation = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.resident_session_id == resident_session_id,
                ProjectMessage.idempotency_key
                == f"resident-activation:{resident_session_id}",
                ProjectMessage.from_type == "user",
                ProjectMessage.surface == MessageSurface.CONVERSATION,
                ProjectMessage.status == "open",
                ProjectMessage.handling_state == MessageHandlingState.PENDING,
                ProjectMessage.conversation_claim_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if activation is None or (
        dict(activation.context or {}).get(activation_context_key) is not True
    ):
        return

    now = _utcnow()
    activation.status = "resolved"
    activation.handling_state = MessageHandlingState.SUPERSEDED
    activation.resolution = "operator_input_superseded_activation"
    activation.resolved_at = now
    activation.updated_at = now
    session.add(activation)


async def create_operator_project_message(
    session: AsyncSession,
    *,
    project: Project,
    user_id: str,
    message_type: MessageType,
    content: str,
    context: dict[str, Any] | None,
    server_context: dict[str, Any] | None = None,
    workbench_context: dict[str, Any] | None,
    idempotency_key: str | None,
    persist_empty_workbench_context: bool = False,
    compact_context_size: bool = True,
    enforce_raw_content_max: bool = False,
    sync_local_continuation_marker: bool = False,
    surface: MessageSurface = MessageSurface.ACTIVITY,
    reply_to_message_id: uuid.UUID | None = None,
    include_strategic_session_snapshot: bool = True,
    precommit_validate: Callable[
        [AsyncSession, uuid.UUID, dict[str, Any]],
        Awaitable[dict[str, Any] | None],
    ]
    | None = None,
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

    message_context = {
        **_operator_context(context),
        **dict(server_context or {}),
    }
    request_fingerprint = _operator_request_fingerprint(
        user_id=user_id,
        message_type=message_type,
        content=trimmed_content,
        surface=surface,
        reply_to_message_id=reply_to_message_id,
        message_context=message_context,
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
            _assert_operator_idempotency_match(
                existing,
                user_id=user_id,
                message_type=message_type,
                content=trimmed_content,
                surface=surface,
                reply_to_message_id=reply_to_message_id,
                request_fingerprint=request_fingerprint,
                message_context=message_context,
            )
            return OperatorMessageCreateResult(
                message=existing,
                status="duplicate",
                arming="duplicate",
                wakeup_state=stored_wakeup_state(existing),
            )

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
        # Persistence locks prefs -> Project. Flush the already-locked Project
        # mutation before populate_existing refreshes the ownership fence.
        await session.flush()
        stored_workbench = workbench_write.state
        workbench_context_for_snapshot = None
        if workbench_context is not None:
            workbench_context_for_snapshot = (
                workbench_context_for_strategic_session_snapshot(
                    stored_workbench,
                    workbench_context,
                )
            )

    if include_strategic_session_snapshot:
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

    # Fence cadence arming and resident message association on the same locked
    # Project snapshot. A live Workday resident consumes the durable message
    # directly, so it suppresses the one-shot strategic refresh marker.
    # Keep the low-level message creation module importable without pulling in
    # the resident runtime (and, through it, the admin/release router graph).
    # This import is intentionally local to the Workday-only association path.
    from app.strategic_resident_session import (  # noqa: PLC0415
        RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY,
        resident_session_snapshot,
    )

    locked_project = await session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_vision = locked_project.vision if locked_project is not None else None
    resident_snapshot = resident_session_snapshot(locked_vision)
    resident_active = bool(
        resident_snapshot
        and resident_snapshot.get("status") == "active"
        and not resident_snapshot.get("stop_requested")
    )
    resident_session_id = (
        str(resident_snapshot.get("session_id"))
        if resident_active and resident_snapshot.get("session_id")
        else None
    )
    refresh_was_pending = strategic_refresh_pending(locked_vision)
    arming = await maybe_arm_operator_refresh(
        session,
        project_id,
        message_type,
        message_content=trimmed_content,
        resident_session_active=resident_active,
    )
    if not session.in_transaction():
        # A failed wake releases every lock. Restore the canonical
        # prefs -> Project order before re-deriving the arming state.
        if should_persist_workbench:
            await persist_strategic_workbench_update(
                session,
                user_id,
                project_id,
                workbench_context or {},
                sync_local_continuation_marker=sync_local_continuation_marker,
                arm_interaction_mode=False,
                commit=False,
            )
            await session.flush()
        locked_project = await session.scalar(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        locked_vision = locked_project.vision if locked_project is not None else None
        resident_snapshot = resident_session_snapshot(locked_vision)
        resident_active = bool(
            resident_snapshot
            and resident_snapshot.get("status") == "active"
            and not resident_snapshot.get("stop_requested")
        )
        resident_session_id = (
            str(resident_snapshot.get("session_id"))
            if resident_active and resident_snapshot.get("session_id")
            else None
        )
        refresh_was_pending = strategic_refresh_pending(locked_vision)
        if resident_active:
            arming = ARMING_RESIDENT_SESSION

    if (
        resident_session_id
        and surface == MessageSurface.CONVERSATION
        and message_context.get(RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY) is not True
    ):
        await _retire_unclaimed_resident_activation(
            session,
            project_id=project_id,
            resident_session_id=resident_session_id,
            activation_context_key=RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY,
        )

    if idem_key:
        # Same-identity retries serialize before reply/source/attachment
        # mutations. The waiter adopts the committed winner without consuming
        # an attachment twice or repeating other early transactional effects.
        await _lock_operator_idempotency_key(session, project_id, idem_key)
        existing = await _message_for_idempotency_key(session, project_id, idem_key)
        if existing is not None:
            _assert_operator_idempotency_match(
                existing,
                user_id=user_id,
                message_type=message_type,
                content=trimmed_content,
                surface=surface,
                reply_to_message_id=reply_to_message_id,
                request_fingerprint=request_fingerprint,
                message_context=message_context,
            )
            existing_id = existing.id
            await session.rollback()
            existing = await session.get(ProjectMessage, existing_id)
            if existing is None:
                raise HTTPException(
                    status_code=500,
                    detail="Existing message could not be reloaded",
                )
            return OperatorMessageCreateResult(
                message=existing,
                status="duplicate",
                arming="duplicate",
                wakeup_state=stored_wakeup_state(existing),
            )

    if reply_to_message_id is not None:
        reply_target = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.id == reply_to_message_id,
                    ProjectMessage.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if reply_target is None:
            raise HTTPException(status_code=400, detail="Invalid reply_to_message_id")
        if (
            reply_target.message_type == MessageType.QUESTION
            and reply_target.status == "open"
        ):
            now = _utcnow()
            reply_target.status = "resolved"
            reply_target.resolution = trimmed_content
            reply_target.resolved_at = now
            reply_target.handling_state = MessageHandlingState.RESPONDED
            reply_target.updated_at = now
            session.add(reply_target)

    message_id = uuid.uuid4()
    # Re-run project ownership/revision guards before reserving the sequence.
    # The validation remains in the message transaction, so attachment receipt
    # consumption and reply resolution still commit atomically with the message.
    if precommit_validate is not None:
        validated_context = await precommit_validate(
            session,
            message_id,
            message_context,
        )
        if validated_context is not None:
            message_context = validated_context

    # Reserve only after all potentially slow validation. From this point to
    # commit, the counter window is bounded to idempotency race closure,
    # canonical message insert, and their atomic commit.
    next_sequence = await lock_project_and_next_message_sequence(
        session,
        project_id,
        lock_timeout_ms=1_500,
    )
    if idem_key:
        # The first lookup is a latency fast path. This second lookup under the
        # sequence fence closes the concurrent double-submit window. Rolling
        # back before adoption also discards this loser's early reply,
        # workbench, source, and attachment mutations.
        existing = await _message_for_idempotency_key(session, project_id, idem_key)
        if existing is not None:
            _assert_operator_idempotency_match(
                existing,
                user_id=user_id,
                message_type=message_type,
                content=trimmed_content,
                surface=surface,
                reply_to_message_id=reply_to_message_id,
                request_fingerprint=request_fingerprint,
                message_context=message_context,
            )
            existing_id = existing.id
            await session.rollback()
            existing = await session.get(ProjectMessage, existing_id)
            if existing is None:
                raise HTTPException(
                    status_code=500,
                    detail="Existing message could not be reloaded",
                )
            return OperatorMessageCreateResult(
                message=existing,
                status="duplicate",
                arming="duplicate",
                wakeup_state=stored_wakeup_state(existing),
            )

    wakeup_state = wakeup_state_for_arming(
        arming,
        refresh_was_pending=refresh_was_pending,
    )
    message_context = {
        **message_context,
        WAKEUP_STATE_CONTEXT_KEY: wakeup_state,
        _REQUEST_FINGERPRINT_KEY: request_fingerprint,
    }
    _ensure_context_size(message_context, compact=compact_context_size)
    msg = ProjectMessage(
        id=message_id,
        project_id=project_id,
        actor_user_id=user_id,
        sequence=next_sequence,
        surface=surface,
        handling_state=MessageHandlingState.PENDING,
        reply_to_message_id=reply_to_message_id,
        message_type=message_type,
        from_type="user",
        content=trimmed_content,
        context=message_context,
        idempotency_key=idem_key,
        resident_session_id=resident_session_id,
    )
    session.add(msg)
    try:
        # Flush the canonical message and its ORM-managed source receipt before
        # correction detection.  Barrier creation and the authority-generation
        # bump remain in this same transaction and commit atomically below.
        await session.flush()
        from app.strategic_memory_corrections import (
            process_operator_message_correction_barriers,
        )

        await process_operator_message_correction_barriers(session, msg)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idem_key:
            existing = await _message_for_idempotency_key(session, project_id, idem_key)
            if existing is not None:
                _assert_operator_idempotency_match(
                    existing,
                    user_id=user_id,
                    message_type=message_type,
                    content=trimmed_content,
                    surface=surface,
                    reply_to_message_id=reply_to_message_id,
                    request_fingerprint=request_fingerprint,
                    message_context=message_context,
                )
                return OperatorMessageCreateResult(
                    message=existing,
                    status="duplicate",
                    arming="duplicate",
                    wakeup_state=stored_wakeup_state(existing),
                )
        raise
    except Exception:
        await session.rollback()
        raise
    await session.refresh(msg)
    message_id = msg.id
    message_event = message_event_payload(msg)

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
        wakeup_state=wakeup_state,
        message_event=message_event,
    )
