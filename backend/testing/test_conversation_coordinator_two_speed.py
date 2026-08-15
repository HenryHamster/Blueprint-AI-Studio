"""Focused durability and ownership tests for the two-speed conversation path."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.conversation_coordinator import (
    ConversationPublicReplyOwner,
    abandon_fast_public_turn,
    acknowledge_workday_consultation_packet,
    claim_next_workday_consultation_packet,
    claim_next_workday_consultation_continuation,
    claim_operator_messages,
    complete_claimed_public_turn,
    complete_workday_consultation_continuation,
    create_or_get_workday_consultation_packet,
    native_resident_reply_may_complete_claim,
    renew_workday_consultation_packet,
    renew_workday_consultation_continuation,
    release_workday_consultation_continuation,
    standard_fallback_is_admissible,
)
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMessage,
    Task,
)


async def _project_with_input(db_session, *, sequence: int = 1):
    project = Project(name="two speed conversation", user_id="two-speed-user")
    db_session.add(project)
    await db_session.commit()
    message = ProjectMessage(
        project_id=project.id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Can you investigate this?",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    task = Task(
        project_id=project.id,
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
    sequence = max(int(candidate.sequence or 0) for candidate in claim.rows)
    return await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="session",
        claim_id=claim.claim_id,
        worker_generation=int(row.conversation_claim_generation or 1),
        worker_incarnation=row.conversation_claim_incarnation,
        input_revision=sequence,
        transcript_sequence=sequence,
        decision_authority={},
        ledger_revision=0,
    )


@pytest.mark.asyncio
async def test_fast_claim_fences_other_public_owner(db_session):
    project, message, task = await _project_with_input(db_session)
    fast_task_id = task.id
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
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
        project_id=project.id,
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
    source_reply_id = uuid.uuid4()
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
    )
    assert claim is not None
    envelope = await _envelope(db_session, project, claim)
    atomic_reply_id = uuid.uuid4()
    packet = await create_or_get_workday_consultation_packet(
        db_session,
        project_id=project.id,
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
        project_id=project.id,
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
        project_id=project.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert duplicate_delivery is not None
    assert duplicate_delivery.workday_claim_id == delivery.workday_claim_id
    duplicate_ack = await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project.id,
        packet_id=packet.message.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=delivery.workday_claim_id,
        outcome="completed",
        result_summary="The exact evidence supports the proposed derivation.",
    )
    assert not duplicate_ack
    await db_session.commit()

    retry_ack = await acknowledge_workday_consultation_packet(
        db_session,
        project_id=project.id,
        packet_id=packet.message.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
        workday_claim_id=delivery.workday_claim_id,
        outcome="completed",
        result_summary="The exact evidence supports the proposed derivation.",
    )
    assert retry_ack
    assert await claim_next_workday_consultation_packet(
        db_session,
        project_id=project.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) is None

    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project.id,
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
        project_id=project.id,
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
        project_id=project.id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) == "released"
    assert await release_workday_consultation_continuation(
        db_session,
        project_id=project.id,
        result_packet_id=continuation.message.id,
        continuation_claim_id=continuation.continuation_claim_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    ) == "already_released"
    continuation = await claim_next_workday_consultation_continuation(
        db_session,
        project_id=project.id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=incarnation_id,
        resident_session_id="session",
    )
    assert continuation is not None
    assert continuation.attempt == 2
    reply = await complete_workday_consultation_continuation(
        db_session,
        project_id=project.id,
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
        atomic_public_reply_id=uuid.uuid4(),
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
        atomic_public_reply_id=uuid.uuid4(),
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
        )
    await db_session.rollback()
    reclaimed = await claim_next_workday_consultation_packet(
        db_session,
        project_id=project_id,
        task_id=task_id,
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        resident_session_id="session",
    )
    assert reclaimed is not None
    assert reclaimed.workday_claim_id != workday_claim_id


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
