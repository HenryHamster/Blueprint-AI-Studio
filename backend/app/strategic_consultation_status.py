"""Server-owned phase truth for live private Workday consultations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
import hashlib
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.conversation_coordinator import _workday_claim_expiry
from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicMemorySource,
)
from app.strategic_conversation_query import strategic_conversation_predicate
from app.time_utils import _utcnow


_ATOMIC_REPLY_ID = "_workday_consultation_atomic_reply_id"
_PACKET_MARKER = "_conversation_consultation_packet"
_RESULT_MARKER = "_workday_consultation_result"
_WORKDAY_CLAIM_ID = "_workday_consultation_claim_id"
_WORKDAY_CLAIM_GENERATION = "_workday_consultation_claim_generation"
_WORKDAY_CLAIM_INCARNATION = "_workday_consultation_claim_incarnation"
_WORKDAY_CLAIM_EXPIRES = "_workday_consultation_claim_expires_at"
_CONSULTATION_REASON_CODES = {
    "consultation_queued",
    "consultation_running",
    "consultation_delivering",
}
_STAGE_COPY = {
    "consultation_queued": ("waiting", "Workday consultation queued"),
    "consultation_running": ("working", "Workday consultation in progress"),
    "consultation_delivering": ("working", "Workday findings ready for Fast"),
}
_RESIDENT_SESSION_KEY = "_resident_strategic_session"
_SERVER_OWNED_AUTONOMY_REASONS = {"authority_not_held", "foreground_call"}


@dataclass(frozen=True)
class VerifiedConsultationActivity:
    """Canonical consultation phase projected from durable queue state."""

    consultation_id: uuid.UUID
    reason_code: str
    phase: str


def _canonical_source(source: ProjectMessage | None, *, session_id: str) -> bool:
    context = source.context if source and isinstance(source.context, dict) else {}
    return bool(
        source is not None
        and source.sequence is not None
        and source.sequence > 0
        and source.surface == MessageSurface.CONVERSATION
        and source.from_type == "strategic"
        and source.resident_session_id == session_id
        and context.get("message_role") == "operator_reply"
        and context.get("_workday_consultation_requested") is True
        and context.get("_conversation_public_reply_owner") == "fast"
        and context.get("audience") not in {STRATEGIST_INTERNAL, PIPELINE_LOG}
        and context.get("privacy_state") not in {"redacted", "deleted"}
    )


def _canonical_internal_candidate(row: ProjectMessage) -> tuple[str, uuid.UUID] | None:
    context = row.context if isinstance(row.context, dict) else {}
    if not (
        context.get("audience") == STRATEGIST_INTERNAL
        and context.get("public_reply_owner") == "fast"
    ):
        return None
    role = context.get("message_role")
    if role == "workday_consultation" and context.get(_PACKET_MARKER) is True:
        kind = "packet"
    elif role == "workday_consultation_result" and context.get(_RESULT_MARKER) is True:
        kind = "result"
    else:
        return None
    try:
        consultation_id = uuid.UUID(str(context.get(_ATOMIC_REPLY_ID) or ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return kind, consultation_id


def _live_workday_claim(row: ProjectMessage) -> bool:
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id is None
    ):
        return False
    context = row.context if isinstance(row.context, dict) else {}
    try:
        uuid.UUID(str(context.get(_WORKDAY_CLAIM_ID) or ""))
        uuid.UUID(str(context.get(_WORKDAY_CLAIM_INCARNATION) or ""))
    except (TypeError, ValueError, AttributeError):
        return False
    generation = context.get(_WORKDAY_CLAIM_GENERATION)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        return False
    expires_at = _workday_claim_expiry(context.get(_WORKDAY_CLAIM_EXPIRES))
    return expires_at is not None and expires_at > _utcnow()


async def derive_verified_consultation_activity(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    resident_session_id: str,
) -> VerifiedConsultationActivity | None:
    """Derive the highest-priority live phase without trusting Agent prose.

    Results outrank active Workday claims, which outrank queued packets. This
    lets a heartbeat reconstruct truthful status even when the public request
    has already fallen outside the bounded transcript window.
    """
    candidates = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.status == "open",
                    ProjectMessage.resident_session_id == resident_session_id,
                    ProjectMessage.handling_state.in_(
                        (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
                    ),
                    or_(
                        and_(
                            ProjectMessage.context["message_role"].as_string()
                            == "workday_consultation",
                            ProjectMessage.context[_PACKET_MARKER]
                            .as_boolean()
                            .is_(True),
                        ),
                        and_(
                            ProjectMessage.context["message_role"].as_string()
                            == "workday_consultation_result",
                            ProjectMessage.context[_RESULT_MARKER]
                            .as_boolean()
                            .is_(True),
                        ),
                    ),
                )
                .order_by(ProjectMessage.sequence, ProjectMessage.id)
            )
        )
        .scalars()
        .all()
    )
    classified: list[tuple[ProjectMessage, str, uuid.UUID]] = []
    source_ids: set[uuid.UUID] = set()
    for row in candidates:
        classification = _canonical_internal_candidate(row)
        if classification is None:
            continue
        kind, consultation_id = classification
        classified.append((row, kind, consultation_id))
        source_ids.add(consultation_id)
    if not source_ids:
        return None
    sources = (
        (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.id.in_(source_ids),
                    strategic_conversation_predicate(),
                )
            )
        )
        .scalars()
        .all()
    )
    canonical_sources = {
        str(source.id): source
        for source in sources
        if _canonical_source(source, session_id=resident_session_id)
    }
    receipts = (
        (
            await session.execute(
                select(StrategicMemorySource).where(
                    StrategicMemorySource.project_id == project_id,
                    StrategicMemorySource.source_type == "project_message",
                    StrategicMemorySource.source_key.in_(canonical_sources),
                    StrategicMemorySource.payload_state == "active",
                    StrategicMemorySource.tombstoned_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    canonical_source_ids = {
        source.id
        for receipt in receipts
        if (source := canonical_sources.get(receipt.source_key)) is not None
        and receipt.source_revision == str(source.sequence)
        and receipt.native_sequence == source.sequence
        and receipt.resident_session_id == resident_session_id
        and receipt.actor_kind == "strategic"
        and (receipt.source_metadata or {}).get("surface") == "conversation"
        and receipt.content_hash
        == hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    }
    valid = [
        (row, kind, consultation_id)
        for row, kind, consultation_id in classified
        if consultation_id in canonical_source_ids
    ]
    for _row, kind, consultation_id in valid:
        if kind == "result":
            return VerifiedConsultationActivity(
                consultation_id=consultation_id,
                reason_code="consultation_delivering",
                phase="working",
            )
    for row, kind, consultation_id in valid:
        if kind == "packet" and _live_workday_claim(row):
            return VerifiedConsultationActivity(
                consultation_id=consultation_id,
                reason_code="consultation_running",
                phase="working",
            )
    for row, kind, consultation_id in valid:
        if (
            kind == "packet"
            and row.handling_state == MessageHandlingState.PENDING
            and row.handled_by_task_id is None
        ):
            return VerifiedConsultationActivity(
                consultation_id=consultation_id,
                reason_code="consultation_queued",
                phase="waiting",
            )
    return None


def reconcile_consultation_activity(
    activity: object,
    verified: VerifiedConsultationActivity | None,
) -> dict[str, Any]:
    """Override Agent consultation claims with the canonical server phase."""
    raw = dict(activity) if isinstance(activity, dict) else {}
    if verified is not None:
        phase, last_action = _STAGE_COPY[verified.reason_code]
        raw.update(
            phase=phase,
            last_action=last_action,
            reason_code=verified.reason_code,
            consultation_id=str(verified.consultation_id),
            autonomy_state="paused",
            autonomy_reason_code=verified.reason_code,
        )
        return raw
    reported_reason = str(raw.get("reason_code") or "").strip().lower()
    if reported_reason in _CONSULTATION_REASON_CODES or raw.get("consultation_id"):
        raw.update(
            phase="waiting",
            last_action="Waiting for project movement",
            reason_code=None,
            consultation_id=None,
            autonomy_state="active",
            autonomy_reason_code=None,
        )
    return raw


async def canonical_consultation_activity_update(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    resident_session_id: str,
    activity: object,
) -> tuple[dict[str, Any], bool]:
    """Return the activity update proven under the caller's Project lock."""
    verified = await derive_verified_consultation_activity(
        session,
        project_id=project_id,
        resident_session_id=resident_session_id,
    )
    return reconcile_consultation_activity(activity, verified), verified is not None


async def project_current_consultation_activity(
    session: AsyncSession,
    *,
    project: Project,
    resident_session_id: str,
) -> dict[str, Any] | None:
    """Project queue truth while the caller still owns the Project lock."""
    verified = await derive_verified_consultation_activity(
        session,
        project_id=project.id,
        resident_session_id=resident_session_id,
    )
    return project_consultation_activity(
        project,
        resident_session_id=resident_session_id,
        verified=verified,
    )


async def emit_consultation_activity_if_changed(
    project_id: uuid.UUID,
    *,
    task_id: uuid.UUID,
    activity: dict[str, Any] | None,
) -> None:
    """Broadcast a committed canonical consultation-state transition."""
    if activity is None:
        return
    from app.strategic_events import emit_strategic_session_state_updated

    await emit_strategic_session_state_updated(
        project_id,
        task_id=task_id,
        activity=activity,
    )


async def completed_workday_consultation_id(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
) -> str | None:
    """Recover only a server-linked consultation identity for a completed claim."""
    reply_id = (
        await session.execute(
            select(ProjectMessage.id).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type == "strategic",
                ProjectMessage.context["_conversation_claim_id"].as_string()
                == str(claim_id),
            )
        )
    ).scalars().first()
    if reply_id is None:
        return None
    packet = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type == "system",
                ProjectMessage.context[_ATOMIC_REPLY_ID].as_string()
                == str(reply_id),
            )
        )
    ).scalars().first()
    context = (
        packet.context
        if packet is not None and isinstance(packet.context, dict)
        else {}
    )
    return str(reply_id) if context.get(_PACKET_MARKER) is True else None


def project_consultation_activity(
    project: Project,
    *,
    resident_session_id: str,
    verified: VerifiedConsultationActivity | None,
) -> dict[str, Any] | None:
    """Atomically mirror canonical queue state into the locked Project vision.

    The returned activity is non-null only for a semantic transition and is
    safe to broadcast after the surrounding transaction commits.
    """
    vision = dict(project.vision or {})
    resident_raw = vision.get(_RESIDENT_SESSION_KEY)
    if not isinstance(resident_raw, dict):
        return None
    resident = dict(resident_raw)
    if (
        resident.get("session_id") != resident_session_id
        or resident.get("status") != "active"
    ):
        return None
    current = (
        dict(resident.get("activity"))
        if isinstance(resident.get("activity"), dict)
        else {}
    )
    updated = reconcile_consultation_activity(current, verified)
    updated["consultation_verified"] = verified is not None
    prior_fence = current.get("autonomy_reason_code")
    if prior_fence in _SERVER_OWNED_AUTONOMY_REASONS:
        updated.update(
            autonomy_state="paused",
            autonomy_reason_code=prior_fence,
        )
    now_iso = _utcnow().isoformat()
    if updated.get("phase") != current.get("phase"):
        updated["phase_started_at"] = now_iso
    elif current.get("phase_started_at"):
        updated["phase_started_at"] = current["phase_started_at"]
    semantic_keys = (
        "phase",
        "reason_code",
        "consultation_id",
        "consultation_verified",
        "autonomy_state",
        "autonomy_reason_code",
    )
    if all(updated.get(key) == current.get(key) for key in semantic_keys):
        return None
    resident["activity"] = updated
    resident["updated_at"] = now_iso
    vision[_RESIDENT_SESSION_KEY] = resident
    project.vision = vision
    flag_modified(project, "vision")
    return updated


async def consultation_activity_is_verified(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    resident_session_id: str,
    activity: object,
) -> bool:
    """Return whether an Agent-reported stage exactly matches canonical state."""
    raw = activity if isinstance(activity, dict) else {}
    try:
        reported_id = uuid.UUID(str(raw.get("consultation_id") or ""))
    except (TypeError, ValueError, AttributeError):
        return False
    verified = await derive_verified_consultation_activity(
        session,
        project_id=project_id,
        resident_session_id=resident_session_id,
    )
    return bool(
        verified is not None
        and reported_id == verified.consultation_id
        and str(raw.get("reason_code") or "").strip().lower() == verified.reason_code
    )
