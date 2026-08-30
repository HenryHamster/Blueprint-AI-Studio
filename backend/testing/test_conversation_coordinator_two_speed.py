"""Focused durability and ownership tests for the two-speed conversation path."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.conversation_coordinator import (
    _OPERATOR_TURN_MAX_DEAD_CLAIMS,
    ConversationPublicReplyOwner,
    OperatorTurnParked,
    abandon_fast_public_turn,
    acknowledge_workday_consultation_packet,
    claim_next_workday_consultation_packet,
    claim_next_workday_consultation_continuation,
    claim_operator_messages,
    complete_claimed_public_turn,
    complete_workday_consultation_continuation,
    create_or_get_workday_consultation_packet,
    native_resident_reply_may_complete_claim,
    read_workday_consultation_attempt_diagnostic,
    record_workday_consultation_attempt_diagnostic,
    record_workday_consultation_gate_failure,
    renew_workday_consultation_packet,
    reset_conversation_claim_for_session_handoff,
    renew_workday_consultation_continuation,
    release_workday_consultation_continuation,
    standard_fallback_is_admissible,
)
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicMemoryProjectState,
    Task,
)


async def _project_with_input(db_session, *, sequence: int = 1):
    project = Project(name="two speed conversation", user_id="two-speed-user")
    db_session.add(project)
    await db_session.commit()
    project_id = project.id
    message = ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Can you investigate this?",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    task = Task(
        project_id=project_id,
        title="two speed conversation worker",
        payloads=[],
    )
    db_session.add_all([message, task])
    await db_session.commit()
    return project, message, task


async def _envelope(db_session, project: Project, claim):
    from app.strategic_memory_envelope import (
        build_and_store_context_envelope_snapshot,
    )
    row = claim.rows[0]
    project_id = project.id
    sequence = max(int(candidate.sequence or 0) for candidate in claim.rows)
    return await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project_id,
        session_id="session",
        claim_id=claim.claim_id,
        worker_generation=int(row.conversation_claim_generation or 1),
        worker_incarnation=row.conversation_claim_incarnation,
        input_revision=sequence,
        transcript_sequence=sequence,
        decision_authority={},
        ledger_revision=0,
    )


def _attempt_diagnostic(
    delivery,
    outcome: str,
    *,
    predicate: str = "provider_result",
    tool_calls: int = 0,
) -> dict:
    started = datetime.now(timezone.utc)
    ended = started + timedelta(milliseconds=10)
    return {
        "attempt": delivery.attempt,
        "workday_claim_id": str(delivery.workday_claim_id),
        "outcome": outcome,
        "terminal_predicate": predicate,
        "provider_terminal_reason": "success" if outcome == "completed" else None,
        "provider_output_tail": None,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "turn_ms": 10,
        "ttft_ms": 1,
        "tool_call_count": tool_calls,
        "tool_call_duration_ms": 0,
        "result_chars": 12 if outcome == "completed" else 0,
        "trace": [
            {"elapsed_ms": 10, "kind": "result", "label": predicate}
        ],
        "trace_truncated": False,
    }

@pytest.mark.asyncio
async def test_fast_claim_fences_other_public_owner(db_session):
    project, message, task = await _project_with_input(db_session)
    project_id = project.id
    fast_task_id = task.id
    claim = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=fast_task_id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    await db_session.commit()

    competing = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=uuid.uuid4(),
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.STANDARD,
    )
    assert competing is None
    legacy_competing = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=fast_task_id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
    )
    assert legacy_competing is None
    claimed_row = await db_session.get(ProjectMessage, message.id)
    assert claimed_row is not None
    # This is the additional generic/native reply fence: a failed Fast abandon
    # must not let the broad Workday reply route complete the same live claim.
    assert not native_resident_reply_may_complete_claim(claimed_row)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_live_claim_membership_cannot_be_expanded(db_session):
    project, first, task = await _project_with_input(db_session, sequence=1)
    second = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="And one more thing",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    db_session.add(second)
    await db_session.commit()
    task_id = task.id
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[first.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    await db_session.commit()

    expanded = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[first.id, second.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert expanded is None


@pytest.mark.asyncio
async def test_client_claim_id_reconciles_response_loss_exactly(db_session):
    project, message, task = await _project_with_input(db_session)
    task_id = task.id
    incarnation_id = uuid.uuid4()
    client_claim_id = uuid.uuid4()
    first = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=client_claim_id,
        claim_idempotency_key="fast-client-turn-1",
    )
    assert first is not None
    assert first.claim_id == client_claim_id
    await db_session.commit()

    replay = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=client_claim_id,
        claim_idempotency_key="fast-client-turn-1",
    )
    assert replay is not None
    assert replay.claim_id == client_claim_id
    assert [row.id for row in replay.rows] == [message.id]

    conflicting = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=client_claim_id,
        claim_idempotency_key="different-key",
    )
    assert conflicting is None


@pytest.mark.asyncio
async def test_expired_exact_claim_rebinds_to_replacement_client_identity(db_session):
    project, message, task = await _project_with_input(db_session)
    project_id = project.id
    message_id = message.id
    task_id = task.id
    newer = ProjectMessage(
        project_id=project_id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="newer input",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    db_session.add(newer)
    await db_session.flush()
    newer_id = newer.id
    await db_session.commit()
    old_client_claim_id = uuid.uuid4()
    old_incarnation_id = uuid.uuid4()
    first = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=old_incarnation_id,
        message_ids=[message_id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=old_client_claim_id,
        claim_idempotency_key="old-fast-turn",
    )
    assert first is not None
    claimed_row = await db_session.get(ProjectMessage, message_id)
    assert claimed_row is not None
    claimed_row.conversation_claim_expires_at = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - timedelta(seconds=1)
    db_session.add(claimed_row)
    await db_session.commit()

    replacement_client_claim_id = uuid.uuid4()
    replacement_incarnation_id = uuid.uuid4()
    expanded = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=replacement_incarnation_id,
        message_ids=[message_id, newer_id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=replacement_client_claim_id,
        claim_idempotency_key="replacement-fast-turn",
    )
    assert expanded is None
    await db_session.rollback()

    replacement = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=replacement_incarnation_id,
        message_ids=[message_id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=replacement_client_claim_id,
        claim_idempotency_key="replacement-fast-turn",
    )

    assert replacement is not None
    assert replacement.claim_id == replacement_client_claim_id
    assert replacement.attempt == 2
    assert [row.id for row in replacement.rows] == [message_id]
    assert replacement.rows[0].conversation_claim_incarnation == replacement_incarnation_id
    assert (
        replacement.rows[0].context["_conversation_claim_idempotency_key"]
        == "replacement-fast-turn"
    )
    untouched_newer = await db_session.get(ProjectMessage, newer_id)
    assert untouched_newer is not None
    assert untouched_newer.handling_state == MessageHandlingState.PENDING
    assert untouched_newer.conversation_claim_id is None
    await db_session.commit()

    # If the replacement's successful response is lost, its stable request
    # must replay the newly rebound identity instead of waiting out another
    # lease interval.
    replay = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=replacement_incarnation_id,
        message_ids=[message_id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=replacement_client_claim_id,
        claim_idempotency_key="replacement-fast-turn",
    )
    assert replay is not None
    assert replay.claim_id == replacement_client_claim_id
    assert replay.attempt == 2


@pytest.mark.asyncio
async def test_workday_packet_and_continuation_are_idempotent_and_leave_typed_input_open(
    db_session,
):
    project, message, task = await _project_with_input(db_session)
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    envelope = await _envelope(db_session, project, claim)
    # This coordinator-only fixture does not exercise handoff-source
    # validation, but the packet's relational lineage must still reference a
    # durable same-project message instead of a nonexistent UUID.
    source_reply_id = message.id
    first = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=envelope,
        delivery_key="turn-1",
        question="Inspect the exact implementation evidence.",
        resident_session_id="workday-session",
        atomic_public_reply_id=source_reply_id,
    )
    assert not first.duplicate
    first_pointer = dict(first.message.context["envelope"])
    await db_session.commit()

    # A process restart replays the same packet instead of duplicating it.
    replay = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=envelope,
        delivery_key="turn-1",
        question="Inspect the exact implementation evidence.",
        resident_session_id="workday-session",
        atomic_public_reply_id=source_reply_id,
    )
    assert replay.duplicate
    assert replay.message.id == first.message.id
    replay_pointer = dict(replay.message.context["envelope"])
    assert replay_pointer["snapshot_id"] == first_pointer["snapshot_id"]
    assert (
        replay_pointer["context_authority_generation"]
        == first_pointer["context_authority_generation"]
    )
    assert replay_pointer["privacy_generation"] == first_pointer["privacy_generation"]
    assert replay_pointer["canonical_sha256"] == first_pointer["canonical_sha256"]
    assert replay_pointer["expires_at"] >= first_pointer["expires_at"]
    continuation = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=envelope,
        delivery_key="turn-1",
        question="Inspect the exact implementation evidence.",
        continuation_key="investigation-1",
        resident_session_id="workday-session",
        atomic_public_reply_id=source_reply_id,
    )
    assert not continuation.duplicate
    continuation_pointer = continuation.message.context["envelope"]
    assert continuation_pointer["snapshot_id"] == first_pointer["snapshot_id"]
    assert (
        continuation_pointer["context_authority_generation"]
        == first_pointer["context_authority_generation"]
    )
    assert (
        continuation_pointer["privacy_generation"]
        == first_pointer["privacy_generation"]
    )
    await db_session.commit()

    inbound = await db_session.get(ProjectMessage, message.id)
    assert inbound is not None
    assert inbound.status == "open"
    assert inbound.handling_state == MessageHandlingState.CLAIMED
    packets = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.from_type == "system",
            )
        )
    ).scalars().all()
    assert len(packets) == 2
    assert all(packet.context["audience"] == "strategist-internal" for packet in packets)


@pytest.mark.asyncio
async def test_workday_packet_is_claimed_once_and_acked_without_public_reply(db_session):
    project, message, task = await _project_with_input(db_session)
    project_id = project.id
    task_id = task.id
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    envelope = await _envelope(db_session, project, claim)
    atomic_reply_id = message.id
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project_id,
        claim=claim,
        envelope=envelope,
        delivery_key="needs-evidence",
        question="Find the exact evidence.",
        resident_session_id="session",
        atomic_public_reply_id=atomic_reply_id,
    )
    await db_session.commit()

    delivery = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert delivery is not None
    assert delivery.message.id == packet.message.id
    assert delivery.message.content == "Find the exact evidence."
    await db_session.commit()

    duplicate_delivery = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert duplicate_delivery is not None
    assert duplicate_delivery.workday_claim_id == delivery.workday_claim_id
    completed_diagnostic = _attempt_diagnostic(delivery, "completed")
    duplicate_ack = await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project_id,
        packet_id=packet.message.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=delivery.workday_claim_id,
        outcome="completed",
        result_summary="The exact evidence supports the proposed derivation.",
        attempt_diagnostic=completed_diagnostic,
    )
    assert not duplicate_ack
    await db_session.commit()

    result_key = (
        f"workday-consultation-result:{packet.message.id}:"
        f"{delivery.workday_claim_id}"
    )
    persisted_result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == result_key,
            )
        )
    ).scalar_one()
    assert persisted_result.reply_to_message_id == packet.message.id
    # Simulate a terminal result committed by the pre-relational deployment.
    # Only the exact ack replay may repair the missing packet link.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == persisted_result.id)
        .values(reply_to_message_id=None)
    )
    await db_session.commit()
    await db_session.refresh(persisted_result)

    retry_ack = await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project_id,
        packet_id=packet.message.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=delivery.workday_claim_id,
        outcome="completed",
        result_summary="The exact evidence supports the proposed derivation.",
        attempt_diagnostic=completed_diagnostic,
    )
    assert retry_ack
    repaired_result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == result_key,
            )
        )
    ).scalar_one()
    assert repaired_result.reply_to_message_id == packet.message.id
    await db_session.commit()
    repaired_result.reply_to_message_id = message.id
    db_session.add(repaired_result)
    await db_session.flush()
    with pytest.raises(
        ValueError, match="consultation result relational lineage conflicts"
    ):
        await acknowledge_workday_consultation_packet(
            db_session,
            project_id=project_id,
            packet_id=packet.message.id,
            task_id=task_id,
            worker_generation=1,
            worker_incarnation=incarnation_id,
            resident_session_id="session",
            workday_claim_id=delivery.workday_claim_id,
            outcome="completed",
            result_summary=(
                "The exact evidence supports the proposed derivation."
            ),
            attempt_diagnostic=completed_diagnostic,
        )
    await db_session.rollback()
    assert await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) is None

    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert continuation is not None
    assert continuation.attempt == 1
    assert continuation.message.context["_workday_consultation_atomic_reply_id"] == str(
        atomic_reply_id
    )
    renewed = await renew_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        lease_seconds=90,
    )
    assert renewed.continuation_claim_id == continuation.continuation_claim_id
    assert renewed.lease_expires_at > continuation.lease_expires_at
    assert await release_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) == "released"
    assert await release_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) == "already_released"
    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert continuation is not None
    assert continuation.attempt == 2
    reply = await complete_workday_consultation_continuation(
        db_session,
        project_id=project_id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        content="I checked it: the derivation is consistent under that assumption.",
        idempotency_key="continuation-reply-1",
    )
    assert not reply.duplicate
    assert reply.message.context["_workday_result_packet_id"] == str(
        continuation.message.id
    )
    assert reply.message.context["_workday_consultation_atomic_reply_id"] == str(
        atomic_reply_id
    )


@pytest.mark.asyncio
async def test_workday_empty_result_retries_once_then_creates_terminal_failure(db_session):
    project, message, task = await _project_with_input(db_session)
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=await _envelope(db_session, project, claim),
        delivery_key="empty-result",
        question="Find exact evidence.",
        resident_session_id="session",
        atomic_public_reply_id=message.id,
    )
    first = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert first is not None and first.attempt == 1
    with pytest.raises(ValueError, match="at least two attempts"):
        await acknowledge_workday_consultation_packet(
            db_session,
            project_id=project.id,
            packet_id=packet.message.id,
            task_id=task.id,
            worker_generation=1,
            worker_incarnation=incarnation_id,
            resident_session_id="session",
            workday_claim_id=first.workday_claim_id,
            outcome="failed",
            attempt_diagnostic=_attempt_diagnostic(
                first, "failed", predicate="empty_provider_result"
            ),
        )
    await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project.id,
        packet_id=packet.message.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=first.workday_claim_id,
        outcome="abandoned",
        attempt_diagnostic=_attempt_diagnostic(
            first, "abandoned", predicate="empty_provider_result"
        ),
    )
    await db_session.commit()
    second = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert second is not None and second.attempt == 2
    failed_diagnostic = _attempt_diagnostic(
        second, "failed", predicate="empty_provider_result"
    )
    assert not await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project.id,
        packet_id=packet.message.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=second.workday_claim_id,
        outcome="failed",
        attempt_diagnostic=failed_diagnostic,
    )
    await db_session.commit()
    assert await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project.id,
        packet_id=packet.message.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=second.workday_claim_id,
        outcome="failed",
        attempt_diagnostic=failed_diagnostic,
    )
    assert await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) is None
    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert continuation is not None
    assert continuation.message.context["result_state"] == "failed"
    assert "failure notice itself does not verify" in continuation.message.content
    assert [
        item["terminal_predicate"]
        for item in continuation.message.context["_workday_consultation_attempts"]
    ] == ["empty_provider_result", "empty_provider_result"]


@pytest.mark.asyncio
async def test_claiming_next_fast_turn_preserves_prior_workday_packet(db_session):
    project, first_input, task = await _project_with_input(db_session)
    first_incarnation_id = uuid.uuid4()
    first_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=first_incarnation_id,
        message_ids=[first_input.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
    )
    assert first_claim is not None
    envelope = await _envelope(db_session, project, first_claim)
    first_completion = await complete_claimed_public_turn(
        db_session,
        project_id=project.id,
        claim_id=first_claim.claim_id,
        task_id=task.id,
        worker_generation=1,
        incarnation_id=first_incarnation_id,
        owner=ConversationPublicReplyOwner.FAST,
        content="I can answer now while Workday verifies the implementation.",
        idempotency_key="fast-reply-with-workday-turn-1",
        resident_session_id="session",
        workday_request={
            "delivery_key": "turn-1-verification",
            "question": "Verify the exact implementation evidence.",
        },
        envelope=envelope,
    )
    assert not first_completion.duplicate
    assert first_completion.consultation_packet is not None
    assert first_completion.message.context["_workday_consultation_requested"] is True
    assert (
        first_completion.consultation_packet.context[
            "_workday_consultation_atomic_reply_id"
        ]
        == str(first_completion.message.id)
    )
    packet_id = first_completion.consultation_packet.id
    next_sequence = max(
        int(first_completion.message.sequence or 0),
        int(first_completion.consultation_packet.sequence or 0),
    ) + 1
    await db_session.commit()

    persisted_packet = await db_session.get(ProjectMessage, packet_id)
    assert persisted_packet is not None
    assert persisted_packet.context["audience"] == "strategist-internal"
    assert persisted_packet.reply_to_message_id == first_completion.message.id
    memory_state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert memory_state is not None
    privacy_generation_before_repair = memory_state.privacy_generation
    # Simulate a packet committed before the relational lineage column was
    # populated.  The exact public-reply replay has independent server-key and
    # content receipts, so it may fill only this NULL link.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == packet_id)
        .values(reply_to_message_id=None)
    )
    await db_session.commit()
    await db_session.refresh(persisted_packet)
    replay_completion = await complete_claimed_public_turn(
        db_session,
        project_id=project.id,
        claim_id=first_claim.claim_id,
        task_id=task.id,
        worker_generation=1,
        incarnation_id=first_incarnation_id,
        owner=ConversationPublicReplyOwner.FAST,
        content="I can answer now while Workday verifies the implementation.",
        idempotency_key="fast-reply-with-workday-turn-1",
        resident_session_id="session",
        workday_request={
            "delivery_key": "turn-1-verification",
            "question": "Verify the exact implementation evidence.",
        },
        envelope=envelope,
    )
    assert replay_completion.duplicate
    assert replay_completion.consultation_packet is not None
    assert (
        replay_completion.consultation_packet.reply_to_message_id
        == first_completion.message.id
    )
    await db_session.commit()
    repaired_memory_state = (
        await db_session.execute(
            select(StrategicMemoryProjectState)
            .where(StrategicMemoryProjectState.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert repaired_memory_state.privacy_generation == privacy_generation_before_repair
    assert await db_session.get(ProjectMessage, packet_id) is not None

    second_input = ProjectMessage(
        project_id=project.id,
        sequence=next_sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Here is the next operator turn.",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    db_session.add(second_input)
    await db_session.commit()

    second_incarnation_id = uuid.uuid4()
    second_claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=second_incarnation_id,
        message_ids=[second_input.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
    )
    assert second_claim is not None
    await db_session.commit()

    persisted_packet = await db_session.get(ProjectMessage, packet_id)
    assert persisted_packet is not None
    delivery = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=second_incarnation_id,
        resident_session_id="session",
    )
    assert delivery is not None
    assert delivery.message.id == packet_id


@pytest.mark.asyncio
async def test_expired_workday_packet_cannot_ack_or_renew_and_is_reclaimable(db_session):
    project, message, task = await _project_with_input(db_session)
    project_id = project.id
    task_id = task.id
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project_id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    envelope = await _envelope(db_session, project, claim)
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project_id,
        claim=claim,
        envelope=envelope,
        delivery_key="expired-evidence",
        question="Find evidence.",
        resident_session_id="session",
        atomic_public_reply_id=message.id,
    )
    delivery = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert delivery is not None
    packet_id = packet.message.id
    workday_claim_id = delivery.workday_claim_id
    context = dict(packet.message.context)
    context["_workday_consultation_claim_expires_at"] = "2000-01-01T00:00:00"
    packet.message.context = context
    db_session.add(packet.message)
    await db_session.commit()

    with pytest.raises(ValueError, match="lease is expired"):
        await renew_workday_consultation_packet(
            db_session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            worker_generation=1,
            worker_incarnation=incarnation_id,
            resident_session_id="session",
            workday_claim_id=workday_claim_id,
        )
    await db_session.rollback()
    with pytest.raises(ValueError, match="lease is expired"):
        await acknowledge_workday_consultation_packet(
            db_session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            worker_generation=1,
            worker_incarnation=incarnation_id,
            resident_session_id="session",
            workday_claim_id=workday_claim_id,
            outcome="completed",
            result_summary="Late result",
            attempt_diagnostic=_attempt_diagnostic(delivery, "completed"),
        )
    await db_session.rollback()
    successor_incarnation = uuid.uuid4()
    reclaimed = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=successor_incarnation,
        resident_session_id="session",
    )
    assert reclaimed is not None
    assert reclaimed.workday_claim_id != workday_claim_id
    assert reclaimed.attempt == 2
    placeholder = reclaimed.prior_attempts[0]
    assert placeholder["workday_claim_id"] == str(workday_claim_id)
    assert placeholder["terminal_predicate"] == "claim_lease_expired_without_ack"
    assert placeholder["trace_truncated"] is True

    diagnostic = _attempt_diagnostic(
        delivery,
        "abandoned",
        predicate="consultation_lease_renewal_failed",
    )
    disposition, state, recorded = (
        await record_workday_consultation_attempt_diagnostic(
            db_session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            resident_session_id="session",
            worker_generation=1,
            worker_incarnation=incarnation_id,
            workday_claim_id=workday_claim_id,
            attempt_diagnostic=diagnostic,
        )
    )
    assert (disposition, state) == ("upgraded", "recorded")
    assert recorded["terminal_predicate"] == "consultation_lease_renewal_failed"
    duplicate, duplicate_state, _ = (
        await record_workday_consultation_attempt_diagnostic(
            db_session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            resident_session_id="session",
            worker_generation=1,
            worker_incarnation=incarnation_id,
            workday_claim_id=workday_claim_id,
            attempt_diagnostic=diagnostic,
        )
    )
    assert (duplicate, duplicate_state) == ("duplicate", "recorded")
    receipt_state, receipt = await read_workday_consultation_attempt_diagnostic(
        db_session,
        project_id=project_id,
        packet_id=packet_id,
        task_id=task_id,
        resident_session_id="session",
        worker_generation=1,
        worker_incarnation=incarnation_id,
        workday_claim_id=workday_claim_id,
    )
    assert receipt_state == "recorded"
    assert receipt == recorded
    refreshed_packet = await db_session.get(ProjectMessage, packet_id)
    assert refreshed_packet is not None
    assert refreshed_packet.context["_workday_consultation_claim_id"] == str(
        reclaimed.workday_claim_id
    )
    assert refreshed_packet.context["_workday_consultation_claim_attempt"] == 2


@pytest.mark.asyncio
async def test_fast_abandon_releases_only_exact_uncompleted_claim(db_session):
    project, message, task = await _project_with_input(db_session)
    task_id = task.id
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
    )
    assert claim is not None
    assert await abandon_fast_public_turn(
        db_session,
        project_id=project.id,
        claim_id=claim.claim_id,
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
    ) == "abandoned"
    await db_session.commit()

    released = await db_session.get(ProjectMessage, message.id)
    assert released is not None
    assert released.status == "open"
    assert released.handling_state == MessageHandlingState.PENDING
    assert released.conversation_claim_id is None
    assert released.context.get("_conversation_public_reply_owner") is None
    assert await abandon_fast_public_turn(
        db_session,
        project_id=project.id,
        claim_id=claim.claim_id,
        task_id=task_id,
        worker_generation=1,
        incarnation_id=incarnation_id,
    ) == "already_abandoned"


@pytest.mark.asyncio
async def test_typed_claim_rejects_non_conversation_input(db_session):
    project, _, task = await _project_with_input(db_session)
    activity = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.ACTIVITY,
        from_type="user",
        content="not operator conversation",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    db_session.add(activity)
    await db_session.commit()
    assert await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[activity.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
    ) is None
    # The ordinary Workday lane still has access to non-conversation work; the
    # typed Fast projection must be a subset rather than a destructive filter.
    ordinary = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[activity.id],
        lease_seconds=45,
    )
    assert ordinary is not None


def test_standard_fallback_is_explicit_and_only_when_fast_has_no_capacity():
    assert not standard_fallback_is_admissible(
        fast_capacity_available=True,
        explicitly_requested=True,
    )
    assert not standard_fallback_is_admissible(
        fast_capacity_available=False,
        explicitly_requested=False,
    )
    assert standard_fallback_is_admissible(
        fast_capacity_available=False,
        explicitly_requested=True,
    )


async def _expire_workday_claim_lease(db_session, packet_id: uuid.UUID) -> None:
    """Simulate a worker that died without ever acking: force the packet's
    already-issued claim lease into the past so the NEXT claim_next call sees
    it as reclaimable, exactly like real lease expiry -- without sleeping."""
    row = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == packet_id)
        )
    ).scalar_one()
    context = dict(row.context or {})
    context["_workday_consultation_claim_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).isoformat()
    row.context = context
    db_session.add(row)
    await db_session.commit()


@pytest.mark.asyncio
async def test_workday_packet_parks_after_max_unacked_reclaims(db_session):
    """Root-cause regression for the requeue-not-escalate churn defect: a
    packet whose worker keeps dying before it can ack (lease simply expires,
    exactly what happens when session.interrupt() kills the whole Claude Code
    child) must stop being handed out indefinitely and instead park itself as
    a terminal failure with an explanatory reason, once claimed
    _WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS (3) times without a terminal ack.
    """
    project, message, task = await _project_with_input(db_session)
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=await _envelope(db_session, project, claim),
        delivery_key="parked-after-cap",
        question="Investigate the recurring churn.",
        resident_session_id="session",
        atomic_public_reply_id=message.id,
    )

    first = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert first is not None and first.attempt == 1
    await db_session.commit()
    await _expire_workday_claim_lease(db_session, packet.message.id)

    second = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert second is not None and second.attempt == 2
    await db_session.commit()
    await _expire_workday_claim_lease(db_session, packet.message.id)

    # The 3rd reclaim attempt hits the cap: no claim is handed out, and the
    # packet is parked instead of requeued a 4th time.
    third = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert third is None
    await db_session.commit()

    parked = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == packet.message.id)
        )
    ).scalar_one()
    assert parked.handling_state == MessageHandlingState.RESPONDED
    assert parked.context.get("_workday_consultation_ack_outcome") == "failed"
    reason = parked.context.get("_workday_consultation_result_summary") or ""
    assert "3 claim attempts" in reason
    assert "parked" in reason

    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert continuation is not None
    assert continuation.message.context["result_state"] == "failed"
    assert "parked" in continuation.message.content
    assert "3 claim attempts" in continuation.message.content


@pytest.mark.asyncio
async def test_gate_failure_detail_persists_logs_and_surfaces_in_parked_reason(
    db_session, caplog
):
    """Root-cause regression for the zero-observability defect: a renewal/ack
    gate failure (409) must leave a structured server log AND a queryable
    reason on the packet, and that reason must in turn surface inside a later
    attempt-cap park so the incident carries its own diagnosis end to end."""
    project, message, task = await _project_with_input(db_session)
    incarnation_id = uuid.uuid4()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=incarnation_id,
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
        claim=claim,
        envelope=await _envelope(db_session, project, claim),
        delivery_key="gate-failure-detail",
        question="Investigate the stale-envelope 409.",
        resident_session_id="session",
        atomic_public_reply_id=message.id,
    )

    # Never-claimed packet: exercises the "row exists but nothing else
    # matches" path, same shape a stale/expired-envelope 409 would hit.
    with caplog.at_level(logging.WARNING, logger="app.conversation_coordinator"):
        await record_workday_consultation_gate_failure(
            db_session,
            project_id=project.id,
            packet_id=packet.message.id,
            task_id=task.id,
            resident_session_id="session",
            gate="envelope_resolution",
            detail="Strategic context envelope is stale, expired, or revoked",
        )
        await db_session.commit()

    assert any(
        "gate=envelope_resolution" in record.message
        and "envelope is stale" in record.message
        for record in caplog.records
    )

    refreshed = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == packet.message.id)
        )
    ).scalar_one()
    last_failure = refreshed.context.get("_workday_consultation_last_failure")
    assert isinstance(last_failure, dict)
    assert last_failure["gate"] == "envelope_resolution"
    assert "stale" in last_failure["detail"]
    assert last_failure["task_id"] == str(task.id)
    assert last_failure["resident_session_id"] == "session"
    assert last_failure["recorded_at"]

    # A gate failure against a nonexistent packet must not raise -- it is a
    # best-effort diagnostic, never allowed to mask the caller's real error.
    await record_workday_consultation_gate_failure(
        db_session,
        project_id=project.id,
        packet_id=uuid.uuid4(),
        task_id=task.id,
        resident_session_id="session",
        gate="acknowledge",
        detail="does not matter",
    )
    await db_session.commit()

    # Now drive the packet to the attempt cap and confirm the recorded detail
    # is woven into the parked reason -- the whole point of persisting it.
    first = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert first is not None and first.attempt == 1
    await db_session.commit()
    await _expire_workday_claim_lease(db_session, packet.message.id)
    second = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert second is not None and second.attempt == 2
    await db_session.commit()
    await _expire_workday_claim_lease(db_session, packet.message.id)

    third = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task.id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert third is None
    await db_session.commit()

    parked = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == packet.message.id)
        )
    ).scalar_one()
    reason = parked.context.get("_workday_consultation_result_summary") or ""
    assert "Last recorded gate failure (envelope_resolution)" in reason
    assert "stale" in reason


async def _expire_operator_claim_lease(db_session, message_id: uuid.UUID) -> None:
    """Simulate a claimant that died (or moved on) without ever replying: push
    the already-issued operator-turn lease into the past so the NEXT claim is a
    reclaim, exactly like real lease expiry -- without sleeping."""
    row = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == message_id)
        )
    ).scalar_one()
    row.conversation_claim_expires_at = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - timedelta(seconds=5)
    db_session.add(row)
    await db_session.commit()


@pytest.mark.asyncio
async def test_operator_turn_parks_after_max_dead_claims(db_session):
    """Root-cause regression for the operator-message claim deadlock.

    Production state: 0 of 2 operator messages ever reached 'responded' while
    14/14 strategic messages did. The operator rows sat at
    handling_state='claimed', held by a task that simultaneously reported
    "waiting for operator input". The private Workday consultation lane had had
    an unacked-claim cap for months; the operator lane -- the one a *human* is
    waiting on -- had none, so the turn could be handed out forever and the
    operator was never told anything at all.

    An operator turn whose claim has DIED _OPERATOR_TURN_MAX_DEAD_CLAIMS times
    without a public reply must now DECIDE: answer the operator with the
    failure and close the turn, rather than hand the question back into the
    same silence.
    """
    # Bare literal pin: the bound itself, independent of any assertion below
    # that is phrased in terms of the constant.
    assert _OPERATOR_TURN_MAX_DEAD_CLAIMS == 5

    project, message, task = await _project_with_input(db_session)

    def _claim(generation: int):
        return claim_operator_messages(
            db_session,
            project_id=project.id,
            session_id="session",
            task_id=task.id,
            worker_generation=generation,
            incarnation_id=uuid.uuid4(),
            message_ids=[message.id],
            lease_seconds=45,
            public_reply_owner=ConversationPublicReplyOwner.FAST,
        )

    async def _row():
        return (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.id == message.id)
            )
        ).scalar_one()

    # Handout 1 is fresh (no death yet); handouts 2..5 each reclaim over a
    # corpse lease, so by the last successful claim four deaths are recorded.
    for generation in range(1, 6):
        claim = await _claim(generation)
        assert claim is not None, f"handout {generation} is below the cap"
        assert claim.attempt == generation
        await db_session.commit()
        row = await _row()
        assert (row.context or {}).get("_conversation_turn_dead_claims") == (
            generation - 1 or None
        )
        await _expire_operator_claim_lease(db_session, message.id)

    with pytest.raises(OperatorTurnParked) as excinfo:
        await _claim(6)
    await db_session.commit()

    parked = excinfo.value
    assert parked.dead_claims == 5
    assert parked.message_ids == (message.id,)
    # Surface reasons, not states: the operator-facing text says how many
    # claims died and what to do next.
    assert "died 5 times" in parked.reason
    assert "Send it again to retry" in parked.reason

    row = await _row()
    assert row.handling_state == MessageHandlingState.RESPONDED
    assert row.status == "handled"
    assert row.context.get("_conversation_turn_parked") is True
    assert row.context.get("_conversation_turn_parked_deaths") == 5
    assert "could not process this message" in (
        row.context.get("_conversation_turn_parked_reason") or ""
    )

    replies = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "strategic",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(replies) == 1
    reply = replies[0]
    assert reply.surface == MessageSurface.CONVERSATION
    assert reply.handling_state == MessageHandlingState.RESPONDED
    assert reply.content == parked.reason
    assert reply.context["_resident_reply_covered_message_ids"] == [str(message.id)]

    # Terminal: the closed turn is never handed out again, and re-claiming does
    # not raise a second park at the operator.
    assert await _claim(7) is None


@pytest.mark.asyncio
async def test_operator_turn_handouts_without_deaths_never_park(db_session):
    """A high handout count is NOT evidence of a dead turn.

    conversation_claim_attempt carries EVERY handout forward, including the
    benign Resident session rebind that clears the claim id at session
    handoff -- production rows routinely sit in double digits while remaining
    perfectly answerable (see test_strategic_resident_session.py, which seeds
    attempt=11 and expects a claim). Keying the operator-turn bound on that
    field false-parks healthy turns, so the bound counts only claims that
    provably ENDED BADLY. This test pins that distinction.
    """
    project, message, task = await _project_with_input(db_session)
    for generation in range(1, 3 * _OPERATOR_TURN_MAX_DEAD_CLAIMS):
        claim = await claim_operator_messages(
            db_session,
            project_id=project.id,
            session_id=f"session-{generation}",
            task_id=task.id,
            worker_generation=generation,
            incarnation_id=uuid.uuid4(),
            message_ids=[message.id],
            lease_seconds=45,
            public_reply_owner=ConversationPublicReplyOwner.FAST,
        )
        assert claim is not None, f"handout {generation} must still be claimable"
        assert claim.attempt == generation
        await db_session.commit()
        # Session handoff: the rebind clears the claim, exactly as
        # reset_conversation_claim_for_session_handoff does in production.
        row = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.id == message.id)
            )
        ).scalar_one()
        reset_conversation_claim_for_session_handoff(row)
        db_session.add(row)
        await db_session.commit()

    row = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == message.id)
        )
    ).scalar_one()
    assert row.conversation_claim_attempt > _OPERATOR_TURN_MAX_DEAD_CLAIMS
    assert (row.context or {}).get("_conversation_turn_dead_claims") is None
    assert (row.context or {}).get("_conversation_turn_parked") is None


@pytest.mark.asyncio
async def test_operator_turn_same_owner_replay_does_not_burn_an_attempt(db_session):
    """A live worker replaying its own claim must not be pushed toward the cap.

    Only a FRESH handout (a new claim id, or a reclaim by a replacement owner)
    counts against _OPERATOR_TURN_MAX_DEAD_CLAIMS; otherwise a healthy worker
    that retries an idempotent claim call would park its own operator.
    """
    project, message, task = await _project_with_input(db_session)
    incarnation_id = uuid.uuid4()
    for _ in range(4):
        claim = await claim_operator_messages(
            db_session,
            project_id=project.id,
            session_id="session",
            task_id=task.id,
            worker_generation=1,
            incarnation_id=incarnation_id,
            message_ids=[message.id],
            lease_seconds=45,
            public_reply_owner=ConversationPublicReplyOwner.FAST,
        )
        assert claim is not None
        assert claim.attempt == 1
        await db_session.commit()

    row = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == message.id)
        )
    ).scalar_one()
    assert row.handling_state == MessageHandlingState.CLAIMED
    assert row.context.get("_conversation_turn_parked") is None
