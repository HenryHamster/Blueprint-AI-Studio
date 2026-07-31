"""Focused persistence and byte-identity coverage for Strategic envelopes."""

import base64
from datetime import timedelta
import json
import uuid

import pytest
from sqlalchemy import inspect

from app.models import (
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicContextEnvelopeSnapshot,
)
from app.reaper_sweeps import sweep_strategic_context_retention
from app.strategic_memory_envelope import (
    MAX_CANONICAL_ENVELOPE_BYTES,
    MAX_MEMORY_ITEMS,
    MAX_TRANSCRIPT_CONTENT_BYTES,
    build_and_store_context_envelope_snapshot,
    build_canonical_envelope_core,
    bump_context_authority_generation,
    bump_privacy_generation_and_purge_context_envelopes,
    canonical_envelope_bytes,
    context_envelope_reference,
    get_fenced_context_envelope_by_reference,
    get_fenced_context_envelope_snapshot,
    get_or_create_project_memory_state,
    pin_fenced_context_envelope_for_packet,
    purge_expired_context_envelope_snapshots,
)
from app.time_utils import _utcnow


def test_canonical_core_has_stable_bytes_independent_of_mapping_order():
    first = build_canonical_envelope_core(
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000099"),
        context_authority_generation=2,
        privacy_generation=3,
        transcript_sequence=2,
        ledger_revision=0,
        problem_model={"intent": {"purpose": "ship"}, "revision": 2},
        decision_authority={"revision": "d-2", "owner": "operator"},
        memory=[{"note": "Prefer direct answers"}],
        transcript_suffix=[
            {
                "id": uuid.UUID("00000000-0000-4000-8000-000000000002"),
                "sequence": 2,
                "from_type": "strategic",
                "content": "ack",
                "context": {"b": 2, "a": 1},
            },
            {
                "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
                "sequence": 1,
                "from_type": "user",
                "content": "please ship",
                "context": {},
            },
        ],
    )
    second = build_canonical_envelope_core(
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000099"),
        context_authority_generation=2,
        privacy_generation=3,
        transcript_sequence=2,
        ledger_revision=0,
        problem_model={"revision": 2, "intent": {"purpose": "ship"}},
        decision_authority={"owner": "operator", "revision": "d-2"},
        memory=[{"note": "Prefer direct answers"}],
        transcript_suffix=[
            {
                "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
                "sequence": 1,
                "from_type": "user",
                "content": "please ship",
                "context": {},
            },
            {
                "id": uuid.UUID("00000000-0000-4000-8000-000000000002"),
                "sequence": 2,
                "from_type": "strategic",
                "content": "ack",
                "context": {"a": 1, "b": 2},
            },
        ],
    )

    assert first == second
    assert canonical_envelope_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_canonical_core_serializes_bounded_v2_evidence_and_raw_suffix():
    core = build_canonical_envelope_core(
        project_id=uuid.uuid4(),
        context_authority_generation=1,
        privacy_generation=1,
        transcript_sequence=0,
        ledger_revision=7,
        problem_model={},
        decision_authority={},
        memory=[],
        transcript_suffix=[],
        retrieval={
            "evidence_cards": [{
                "item_id": str(uuid.uuid4()), "version": 2,
                "canonical_text": "eta means $\\eta=0.2$", "status": "active",
                "epistemic_status": "operator_explicit", "confidence": 1,
                "recorded_source_sequence": 7, "raw_quote": "$\\eta=0.2$",
                "raw_quote_available": True, "selection_reasons": ["exact"],
                "relation_path": [],
                "source": {"id": str(uuid.uuid4()), "type": "project_message", "key": "m", "revision": "7"},
            }],
            "raw_suffix": [{"source_id": str(uuid.uuid4()), "source_sequence": 8, "content": "new raw turn"}],
            "answerability": {"answerable": True},
            "memory_degraded": True,
            "retrieval_trace": {"vector": "disabled_unqualified"},
        },
    )
    payload = json.loads(core.canonical_envelope)
    assert payload["memory_v2"]["evidence_cards"][0]["raw_quote"] == "$\\eta=0.2$"
    assert payload["memory_v2"]["raw_suffix"][0]["content"] == "new raw turn"
    assert payload["memory_v2"]["memory_degraded"] is True


@pytest.mark.asyncio
async def test_snapshot_is_idempotent_byte_identical_and_privacy_fenced(db_session):
    project = Project(
        name="Envelope persistence",
        user_id="envelope-test-user",
        problem_model={"revision": "pm-7", "intent": {"purpose": "persist context"}},
    )
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Preserve this exact conversation.",
        context={"source": "operator"},
    )
    db_session.add(message)
    await db_session.flush()

    kwargs = {
        "project_id": project.id,
        "session_id": "strategic-envelope-test",
        "claim_id": uuid.uuid4(),
        "worker_generation": 3,
        "worker_incarnation": uuid.uuid4(),
        "input_revision": 4,
        "transcript_sequence": 1,
        "decision_authority": {"revision": "decision-9", "owner": "operator"},
        "ledger_revision": 0,
        "expires_at": _utcnow() + timedelta(minutes=5),
    }
    first = await build_and_store_context_envelope_snapshot(db_session, **kwargs)
    second = await build_and_store_context_envelope_snapshot(db_session, **kwargs)

    assert first.snapshot_id == second.snapshot_id
    decoded = base64.b64decode(first.canonical_envelope_b64)
    assert decoded == base64.b64decode(second.canonical_envelope_b64)
    assert first.canonical_sha256 == second.canonical_sha256
    canonical = json.loads(decoded)
    assert canonical["project_id"] == str(project.id)
    assert canonical["context_authority_generation"] == 0
    assert canonical["privacy_generation"] == 0
    assert canonical["transcript_sequence"] == 1
    assert canonical["ledger_revision"] == 0
    assert canonical["recent_conversation"][-1]["content"] == (
        "Preserve this exact conversation."
    )

    fetched = await get_fenced_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        snapshot_id=first.snapshot_id,
        session_id=kwargs["session_id"],
        claim_id=kwargs["claim_id"],
        worker_generation=kwargs["worker_generation"],
        worker_incarnation=kwargs["worker_incarnation"],
    )
    assert fetched == first

    state = await get_or_create_project_memory_state(db_session, project.id)
    state.privacy_generation += 1
    await db_session.flush()
    assert (
        await get_fenced_context_envelope_snapshot(
            db_session,
            project_id=project.id,
            snapshot_id=first.snapshot_id,
        )
        is None
    )


def test_canonical_core_bounds_context_and_oversized_legacy_memory():
    core = build_canonical_envelope_core(
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000099"),
        context_authority_generation=0,
        privacy_generation=0,
        transcript_sequence=7,
        ledger_revision=0,
        problem_model={"huge": "x" * (MAX_CANONICAL_ENVELOPE_BYTES * 2)},
        decision_authority={},
        memory=[
            {
                "note": f"note-{index}-" + ("n" * 10_000),
                "secret": "must never reach the envelope",
            }
            for index in range(MAX_MEMORY_ITEMS + 5)
        ],
        transcript_suffix=[
            {
                "id": uuid.uuid4(),
                "sequence": 7,
                "from_type": "user",
                "content": "z" * (MAX_TRANSCRIPT_CONTENT_BYTES * 2),
                "context": {
                    "source": "operator",
                    "authorization": "must never reach the envelope",
                    "nested": {"opaque": "must never reach the envelope"},
                },
            }
        ],
    )

    assert len(core.canonical_envelope) <= MAX_CANONICAL_ENVELOPE_BYTES
    payload = json.loads(core.canonical_envelope)
    assert payload["problem_model"]["_truncated"] is True
    assert len(payload["memory"]) == MAX_MEMORY_ITEMS
    assert "secret" not in payload["memory"][-1]
    row = payload["recent_conversation"][-1]
    assert len(row["content"].encode("utf-8")) <= MAX_TRANSCRIPT_CONTENT_BYTES
    assert row["content_truncated"] is True
    assert row["context"] == {"source": "operator"}


@pytest.mark.asyncio
async def test_authority_bump_fences_prior_snapshot_and_cold_read(db_session):
    project = Project(name="authority fence", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="retain through restart",
    )
    db_session.add(message)
    await db_session.flush()
    kwargs = {
        "project_id": project.id,
        "session_id": "cold-read",
        "claim_id": uuid.uuid4(),
        "worker_generation": 1,
        "worker_incarnation": uuid.uuid4(),
        "input_revision": 1,
        "transcript_sequence": 1,
        "decision_authority": {},
    }
    transport = await build_and_store_context_envelope_snapshot(db_session, **kwargs)
    # Drop the ORM identity map to ensure retrieval is from durable state, not
    # an in-process cached snapshot.
    db_session.expunge_all()
    cold = await get_fenced_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        snapshot_id=transport.snapshot_id,
        session_id=kwargs["session_id"],
        claim_id=kwargs["claim_id"],
        worker_generation=kwargs["worker_generation"],
        worker_incarnation=kwargs["worker_incarnation"],
    )
    assert cold == transport

    state = await bump_context_authority_generation(db_session, project.id)
    assert state.context_authority_generation == 1
    assert (
        await get_fenced_context_envelope_snapshot(
            db_session, project_id=project.id, snapshot_id=transport.snapshot_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_expiry_purge_and_privacy_purge_delete_payloads(db_session):
    project = Project(name="purge envelope", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="erase derived copies",
    )
    db_session.add(message)
    await db_session.flush()
    common = {
        "project_id": project.id,
        "session_id": "purge",
        "worker_generation": 1,
        "worker_incarnation": uuid.uuid4(),
        "input_revision": 1,
        "transcript_sequence": 1,
        "decision_authority": {},
    }
    expired = await build_and_store_context_envelope_snapshot(
        db_session,
        claim_id=uuid.uuid4(),
        expires_at=_utcnow() + timedelta(seconds=1),
        **common,
    )
    assert await purge_expired_context_envelope_snapshots(
        db_session, project_id=project.id, now=_utcnow() + timedelta(seconds=2)
    ) == 1
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, expired.snapshot_id)
        is None
    )

    active = await build_and_store_context_envelope_snapshot(
        db_session,
        claim_id=uuid.uuid4(),
        expires_at=_utcnow() + timedelta(minutes=5),
        **common,
    )
    # The explicit privacy helper is the required escape hatch for a future
    # bulk-redaction path. It must erase durable Workday-derived text too, not
    # merely the envelope table, and must scrub a same-session Python object.
    derived_packet = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="derived private question containing erased source text",
        context={
            "_conversation_consultation_packet": True,
            "audience": "strategist-internal",
        },
    )
    db_session.add(derived_packet)
    await db_session.flush()
    derived_packet_id = derived_packet.id
    state = await bump_privacy_generation_and_purge_context_envelopes(
        db_session, project.id
    )
    assert state.privacy_generation == 1
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, active.snapshot_id)
        is None
    )
    assert await db_session.get(ProjectMessage, derived_packet_id) is None
    assert inspect(derived_packet).detached is True
    assert derived_packet.content == ""
    assert derived_packet.context == {}


@pytest.mark.asyncio
async def test_project_authority_fields_auto_bump_envelope_fence(db_session):
    project = Project(
        name="automatic authority fence",
        user_id="envelope-test-user",
        problem_model={"purpose": "before"},
        strategic_memory_notes=[{"note": "before"}],
    )
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="does this keep the right context?",
    )
    db_session.add(message)
    await db_session.flush()
    transport = await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="automatic-authority",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
    )

    project.problem_model = {"purpose": "after"}
    await db_session.flush()
    state = await get_or_create_project_memory_state(db_session, project.id)
    assert state.context_authority_generation == 1
    assert (
        await get_fenced_context_envelope_snapshot(
            db_session, project_id=project.id, snapshot_id=transport.snapshot_id
        )
        is None
    )

    project.strategic_memory_notes = [{"note": "replacement"}]
    await db_session.flush()
    state = await get_or_create_project_memory_state(db_session, project.id)
    assert state.context_authority_generation == 2


@pytest.mark.asyncio
async def test_envelope_locks_project_before_state_and_reads_fresh_source(db_session):
    """A normal Project->state mutation cannot deadlock or create stale bytes.

    The seam represents a mutation that holds the Project authority boundary
    and then performs its normal before-flush state increment. The envelope
    builder must use that same Project -> state order; reversing it deadlocks
    against a normal mutation route that already holds Project. It must then
    refresh both rows before serialization, so new source fields and the new
    generation appear together in the stored bytes.
    """

    project = Project(
        name="source-lock ordering",
        user_id="envelope-test-user",
        problem_model={"purpose": "before"},
    )
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="build against the current model",
    )
    db_session.add(message)
    await db_session.flush()

    async def mutate_authority_after_project_lock(_project):
        project.problem_model = {"purpose": "after"}
        await db_session.flush()

    transport = await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="source-lock-order",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
        _after_project_lock=mutate_authority_after_project_lock,
    )
    payload = json.loads(base64.b64decode(transport.canonical_envelope_b64))
    assert payload["problem_model"] == {"purpose": "after"}
    assert payload["context_authority_generation"] == 1
    assert transport.context_authority_generation == 1


@pytest.mark.asyncio
async def test_message_redaction_or_orm_delete_auto_purges_envelopes(db_session):
    project = Project(name="automatic privacy fence", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="sensitive original text",
    )
    db_session.add(message)
    await db_session.flush()

    async def make_snapshot() -> object:
        return await build_and_store_context_envelope_snapshot(
            db_session,
            project_id=project.id,
            session_id="automatic-privacy",
            claim_id=uuid.uuid4(),
            worker_generation=1,
            worker_incarnation=uuid.uuid4(),
            input_revision=1,
            transcript_sequence=1,
            decision_authority={},
        )

    redacted = await make_snapshot()
    # Keep an actual ORM instance live across the Core-SQL privacy deletion.
    # The hook must invalidate this identity-map entry too, not merely remove
    # the row for a later cold query.
    loaded_redacted = await db_session.get(
        StrategicContextEnvelopeSnapshot, redacted.snapshot_id
    )
    assert loaded_redacted is not None
    message.content = "[redacted]"
    await db_session.flush()
    state = await get_or_create_project_memory_state(db_session, project.id)
    assert state.privacy_generation == 1
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, redacted.snapshot_id)
        is None
    )
    assert inspect(loaded_redacted).detached is True
    assert loaded_redacted.canonical_envelope == b""
    assert loaded_redacted.retrieval_trace == {}

    deleted = await make_snapshot()
    await db_session.delete(message)
    await db_session.flush()
    state = await get_or_create_project_memory_state(db_session, project.id)
    assert state.privacy_generation == 2
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, deleted.snapshot_id)
        is None
    )


@pytest.mark.asyncio
async def test_every_mutable_transcript_or_visibility_field_privacy_purges(db_session):
    project = Project(name="projection privacy fence", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="private source text",
        context={"source": "private-topic"},
    )
    db_session.add(message)
    await db_session.flush()

    async def make_snapshot(*, transcript_sequence: int) -> object:
        return await build_and_store_context_envelope_snapshot(
            db_session,
            project_id=project.id,
            session_id="projection-privacy",
            claim_id=uuid.uuid4(),
            worker_generation=1,
            worker_incarnation=uuid.uuid4(),
            input_revision=transcript_sequence,
            transcript_sequence=transcript_sequence,
            decision_authority={},
        )

    # Context and sender type are copied directly into the canonical bytes.
    context_snapshot = await make_snapshot(transcript_sequence=1)
    message.context = {"source": "redacted-topic"}
    await db_session.flush()
    assert (
        await db_session.get(
            StrategicContextEnvelopeSnapshot, context_snapshot.snapshot_id
        )
        is None
    )

    sender_snapshot = await make_snapshot(transcript_sequence=1)
    message.from_type = "agent"
    await db_session.flush()
    assert (
        await db_session.get(
            StrategicContextEnvelopeSnapshot, sender_snapshot.snapshot_id
        )
        is None
    )

    # Sequence is serialized and surface controls the canonical query.  Both
    # must invalidate snapshots even though neither changes message content.
    sequence_snapshot = await make_snapshot(transcript_sequence=1)
    message.sequence = 2
    await db_session.flush()
    assert (
        await db_session.get(
            StrategicContextEnvelopeSnapshot, sequence_snapshot.snapshot_id
        )
        is None
    )

    surface_snapshot = await make_snapshot(transcript_sequence=2)
    message.surface = MessageSurface.ACTIVITY
    await db_session.flush()
    assert (
        await db_session.get(
            StrategicContextEnvelopeSnapshot, surface_snapshot.snapshot_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_envelope_build_piggybacks_expired_snapshot_reaping(db_session):
    project = Project(name="piggyback envelope reaper", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="fresh turn",
    )
    db_session.add(message)
    await db_session.flush()
    now = _utcnow()
    expired = StrategicContextEnvelopeSnapshot(
        project_id=project.id,
        session_id="expired-cache",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=0,
        transcript_sequence=0,
        ledger_revision=0,
        problem_model_revision="0",
        decision_authority_revision="0",
        context_authority_generation=0,
        privacy_generation=0,
        canonical_envelope=b'{"expired":true}',
        canonical_sha256="0" * 64,
        created_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=10),
    )
    db_session.add(expired)
    await db_session.flush()

    await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="active-cache",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
    )
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, expired.snapshot_id)
        is None
    )


@pytest.mark.asyncio
async def test_packet_reference_never_copies_bytes_and_fences_redaction(db_session):
    project = Project(name="packet pointer", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    source = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="private theory premise",
    )
    db_session.add(source)
    await db_session.flush()
    transport = await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="packet-pointer",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
    )
    reference = context_envelope_reference(transport)
    assert "canonical_envelope_b64" not in reference
    assert await get_fenced_context_envelope_by_reference(
        db_session, project_id=project.id, reference=reference
    ) == transport

    # A packet can safely outlive Fast's normal 10-minute response cache, but
    # only while its snapshot remains current.
    pinned = await pin_fenced_context_envelope_for_packet(
        db_session, project_id=project.id, reference=reference
    )
    assert pinned is not None
    pinned_reference = context_envelope_reference(pinned)
    assert pinned.expires_at > transport.expires_at
    # A retry may still carry the original short-TTL pointer.  Retention
    # extension is not a semantic/authority change, so it resolves the same
    # current snapshot rather than spuriously dropping a duplicate packet.
    retry_from_original_pointer = await get_fenced_context_envelope_by_reference(
        db_session, project_id=project.id, reference=reference
    )
    assert retry_from_original_pointer is not None
    assert retry_from_original_pointer.snapshot_id == pinned.snapshot_id

    source.content = "[redacted]"
    await db_session.flush()
    assert await get_fenced_context_envelope_by_reference(
        db_session, project_id=project.id, reference=pinned_reference
    ) is None


@pytest.mark.asyncio
async def test_packet_pin_rechecks_fence_after_acquiring_state_lock(db_session):
    """A generation mutation between pin's row locks must fail closed.

    SQLite does not provide PostgreSQL's row-lock interleaving semantics, so
    the internal seam makes the critical ordering deterministic: the state
    lock is acquired, a concurrent-authority-equivalent change is flushed, and
    only then does pin load and lock the snapshot row.
    """
    project = Project(name="pin fence interleaving", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    source = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="current authority source",
    )
    db_session.add(source)
    await db_session.flush()
    transport = await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="pin-interleaving",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
    )
    reference = context_envelope_reference(transport)

    async def mutate_authority_after_state_lock(state):
        state.context_authority_generation += 1
        await db_session.flush()

    assert await pin_fenced_context_envelope_for_packet(
        db_session,
        project_id=project.id,
        reference=reference,
        _after_state_lock=mutate_authority_after_state_lock,
    ) is None


@pytest.mark.asyncio
async def test_source_privacy_mutation_scrubs_derived_packet_rows(db_session):
    project = Project(name="derived packet redaction", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    source = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="erase this everywhere",
    )
    derived = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="derived private result mentioning erase this everywhere",
        context={
            "_workday_consultation_result": True,
            "audience": "strategist-internal",
        },
    )
    db_session.add_all([source, derived])
    await db_session.flush()
    derived_id = derived.id
    source.content = "[redacted]"
    await db_session.flush()
    assert await db_session.get(ProjectMessage, derived_id) is None


@pytest.mark.asyncio
async def test_private_packet_lease_bookkeeping_does_not_spuriously_fence_core(db_session):
    project = Project(name="private packet lease", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    source = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="ordinary conversation source",
    )
    packet = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="private question",
        context={
            "_conversation_consultation_packet": True,
            "audience": "strategist-internal",
        },
    )
    db_session.add_all([source, packet])
    await db_session.flush()
    transport = await build_and_store_context_envelope_snapshot(
        db_session,
        project_id=project.id,
        session_id="private-packet-lease",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=1,
        transcript_sequence=1,
        decision_authority={},
    )
    packet.context = {
        **packet.context,
        "_workday_consultation_claim_id": str(uuid.uuid4()),
    }
    await db_session.flush()
    state = await get_or_create_project_memory_state(db_session, project.id)
    assert state.privacy_generation == 0
    assert await get_fenced_context_envelope_by_reference(
        db_session,
        project_id=project.id,
        reference=context_envelope_reference(transport),
    ) == transport


@pytest.mark.asyncio
async def test_global_context_reaper_purges_dormant_project_snapshot(db_session):
    project = Project(name="dormant envelope retention", user_id="envelope-test-user")
    db_session.add(project)
    await db_session.flush()
    now = _utcnow()
    expired = StrategicContextEnvelopeSnapshot(
        project_id=project.id,
        session_id="dormant",
        claim_id=uuid.uuid4(),
        worker_generation=1,
        worker_incarnation=uuid.uuid4(),
        input_revision=0,
        transcript_sequence=0,
        ledger_revision=0,
        problem_model_revision="0",
        decision_authority_revision="0",
        context_authority_generation=0,
        privacy_generation=0,
        canonical_envelope=b'{"dormant":true}',
        canonical_sha256="0" * 64,
        created_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=10),
    )
    db_session.add(expired)
    await db_session.flush()

    assert await sweep_strategic_context_retention(db_session) == 1
    assert await db_session.get(StrategicContextEnvelopeSnapshot, expired.snapshot_id) is None
