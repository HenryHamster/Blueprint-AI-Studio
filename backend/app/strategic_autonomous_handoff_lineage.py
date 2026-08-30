"""Server-authoritative lineage checks for autonomous Workday handoffs."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.message_audience import STRATEGIST_INTERNAL
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    ProjectMessage,
    StrategicContextEnvelopeSnapshot,
    Task,
)
from app.session_message_handoff import RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY
from app.strategic_workday_rebind_provenance import (
    workday_rebind_root_provenance,
)

_AUTONOMOUS_MARKER = "_autonomous_workday_handoff"
_RESULT_MARKER = "_workday_consultation_result"


async def autonomous_workday_result_lineage_valid(
    session: AsyncSession,
    *,
    row: ProjectMessage,
    key_parts: list[str],
) -> bool | None:
    """Return validated projections, or ``None`` for a non-autonomous key."""

    if not key_parts or key_parts[0] != "autonomous-workday-handoff":
        return None
    if len(key_parts) != 3:
        return False
    try:
        source_task_id = uuid.UUID(key_parts[1])
        client_turn_id = uuid.UUID(key_parts[2])
    except (TypeError, ValueError, AttributeError):
        return False
    source_task = (
        await session.execute(
            select(Task)
            .where(
                Task.project_id == row.project_id,
                Task.id == source_task_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source_task is None:
        return False

    context = row.context if isinstance(row.context, dict) else {}
    task_context = source_task.context if isinstance(source_task.context, dict) else {}
    first_payload = source_task.first_payload or {}
    source_session_id = str(task_context.get("strategic_session_id") or "").strip()
    prior_session_id = str(
        context.get(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY) or ""
    ).strip()
    row_session_id = str(row.resident_session_id or "").strip()
    source_generation = task_context.get("_resident_worker_generation")
    projected_generation = context.get("autonomous_worker_generation")
    envelope_reference = (
        context.get("envelope") if isinstance(context.get("envelope"), dict) else {}
    )
    try:
        projected_task_id = uuid.UUID(str(context.get("autonomous_task_id")))
        projected_client_turn_id = uuid.UUID(
            str(context.get("autonomous_handoff_idempotency_key"))
        )
        turn_id = uuid.UUID(str(context.get("autonomous_turn_id")))
        claim_id = uuid.UUID(str(context.get("claim_id")))
        uuid.UUID(str(context.get("autonomous_turn_lease_id")))
        projected_incarnation = uuid.UUID(
            str(context.get("autonomous_worker_incarnation"))
        )
        continuation_incarnation = uuid.UUID(
            str(context.get("_workday_continuation_claim_incarnation"))
        )
        snapshot_id = uuid.UUID(str(envelope_reference.get("snapshot_id")))
    except (TypeError, ValueError, AttributeError):
        return False

    snapshot = (
        await session.execute(
            select(StrategicContextEnvelopeSnapshot)
            .where(
                StrategicContextEnvelopeSnapshot.project_id == row.project_id,
                StrategicContextEnvelopeSnapshot.snapshot_id == snapshot_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if snapshot is None:
        return False
    sequence = row.sequence
    continuation_generation = context.get("_workday_continuation_claim_generation")
    root_provenance = workday_rebind_root_provenance(snapshot)
    rebind_depth = (
        root_provenance.get("workday_rebind_depth")
        if root_provenance is not None
        else None
    )
    root_provenance_valid = bool(
        root_provenance is not None
        and root_provenance.get("workday_rebind_root_session_id")
        == source_session_id
        and root_provenance.get("workday_rebind_root_worker_generation")
        == source_generation
        and root_provenance.get("workday_rebind_root_worker_generation")
        == projected_generation
        and root_provenance.get("workday_rebind_root_worker_incarnation")
        == str(projected_incarnation)
        and root_provenance.get("workday_rebind_root_claim_id") == str(turn_id)
    )
    original_session_link = bool(
        prior_session_id == source_session_id
        if prior_session_id
        else row_session_id == source_session_id
    )
    original_snapshot_owner = bool(
        root_provenance_valid
        and rebind_depth == 0
        and original_session_link
        and snapshot.session_id == source_session_id
        and snapshot.worker_generation == projected_generation
        and snapshot.worker_incarnation == projected_incarnation
    )
    prior_rebound_snapshot_owner = bool(
        root_provenance_valid
        and isinstance(rebind_depth, int)
        and rebind_depth > 0
        and row.handling_state == MessageHandlingState.CLAIMED
        and prior_session_id
        and prior_session_id != row_session_id
        and snapshot.session_id == prior_session_id
    )
    current_rebound_snapshot_owner = bool(
        root_provenance_valid
        and isinstance(rebind_depth, int)
        and rebind_depth > 0
        and row.handling_state == MessageHandlingState.CLAIMED
        and not prior_session_id
        and snapshot.session_id == row_session_id
        and snapshot.session_id != source_session_id
        and snapshot.worker_generation == continuation_generation
        and snapshot.worker_incarnation == continuation_incarnation
    )
    forbidden_provenance_keys = (
        "_workday_consultation_atomic_reply_id",
        "source_packet_id",
        "_workday_consultation_operation_ledger",
        "_workday_consultation_mutation_receipts",
        "_workday_consultation_mutation_receipts_has_more",
    )
    finding_kind = context.get("autonomous_finding_kind")
    return bool(
        task_context.get("_resident_session") is True
        and source_session_id
        and type(source_generation) is int
        and source_generation > 0
        and type(projected_generation) is int
        and projected_generation == source_generation
        and type(continuation_generation) is int
        and continuation_generation > 0
        and first_payload.get("type") == "strategic_review"
        and first_payload.get("typed_fast_enabled") is True
        and row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.message_type == MessageType.NOTE
        and row.status == "open"
        and row.handling_state
        in {MessageHandlingState.PENDING, MessageHandlingState.CLAIMED}
        and row.reply_to_message_id is None
        and isinstance(sequence, int)
        and sequence > 0
        and row_session_id
        and context.get(_RESULT_MARKER) is True
        and context.get(_AUTONOMOUS_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation_result"
        and context.get("public_reply_owner") == "fast"
        and context.get("result_state") == "completed"
        and projected_task_id == source_task_id
        and projected_client_turn_id == client_turn_id
        and claim_id == turn_id
        and finding_kind in {"question", "blocker", "finding"}
        and context.get("delivery_key") == f"autonomous-{finding_kind}"
        and context.get("_conversation_consultation_continuation")
        == "autonomous-workday-v1"
        and isinstance(row.content, str)
        and row.content.startswith(f"Autonomous Workday {finding_kind}\n\n")
        and not any(key in context for key in forbidden_provenance_keys)
        and snapshot.claim_id == turn_id
        and snapshot.input_revision == sequence - 1
        and snapshot.transcript_sequence == sequence - 1
        and envelope_reference.get("canonical_sha256") == snapshot.canonical_sha256
        and envelope_reference.get("context_authority_generation")
        == snapshot.context_authority_generation
        and envelope_reference.get("privacy_generation")
        == snapshot.privacy_generation
        and envelope_reference.get("transcript_sequence")
        == snapshot.transcript_sequence
        and envelope_reference.get("ledger_revision") == snapshot.ledger_revision
        and hashlib.sha256(snapshot.canonical_envelope).hexdigest()
        == snapshot.canonical_sha256
        and (
            original_snapshot_owner
            or prior_rebound_snapshot_owner
            or current_rebound_snapshot_owner
        )
    )


__all__ = ["autonomous_workday_result_lineage_valid"]
