"""Pure proposal-contract coverage for the Phase-4 ledger service."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.strategic_memory_service import (
    EdgeMutation,
    ItemMutation,
    LedgerProposal,
    SourceDispositionMutation,
    SourceSpanProposal,
)
from app.models import (
    Project,
    StrategicMemoryCorrectionBarrier,
    StrategicMemoryCorrectionBarrierEvent,
    StrategicMemoryItem,
    StrategicMemoryProjection,
    StrategicMemoryProjectState,
)
from app.strategic_memory_control import claim_consolidation_job, enqueue_contiguous_job
from app.strategic_memory_sources import SourceReceiptInput, record_source, sha256_hex
from app.strategic_memory_service import (
    LedgerAdmissionError,
    TransitionMutation,
    _assert_acyclic_supersession,
    as_of_memory_items,
    rebuild_present_projection,
    validate_and_admit_proposal,
)
from app.time_utils import _utcnow
from sqlmodel import select


def _source_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-4000-8000-000000000001")


def test_installed_proposal_is_byte_stable_and_rejects_unknown_or_duplicate_dispositions():
    source_id = _source_id()
    first = LedgerProposal(
        schema_version=1,
        job_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        base_ledger_revision=0,
        idempotency_key="receipt-1",
        dispositions=[
            SourceDispositionMutation(source_id=source_id, source_hash="a" * 64, status="no_memory")
        ],
    )
    second = LedgerProposal.model_validate(first.model_dump(mode="json"))
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_hash() == second.content_hash()
    with pytest.raises(ValidationError, match="repeats a source disposition"):
        LedgerProposal(
            schema_version=1,
            job_id=uuid.uuid4(),
            base_ledger_revision=0,
            idempotency_key="duplicate",
            dispositions=[
                SourceDispositionMutation(source_id=source_id, source_hash="a" * 64, status="no_memory"),
                SourceDispositionMutation(source_id=source_id, source_hash="a" * 64, status="no_memory"),
            ],
        )
    with pytest.raises(ValidationError, match="not installed"):
        LedgerProposal(schema_version=2, job_id=uuid.uuid4(), base_ledger_revision=0, idempotency_key="future", dispositions=[])


def test_item_provenance_requires_exact_span_shape_and_valid_semantic_shape():
    source_id = _source_id()
    with pytest.raises(ValidationError, match="both bounds"):
        SourceSpanProposal(source_id=source_id, source_role="assertion", span_index=0, span_kind="inline_math", span_start=1, span_hash="a" * 64)
    with pytest.raises(ValidationError, match="logical key, text, and provenance"):
        ItemMutation(kind="notation", logical_key="", canonical_text="", epistemic_status="operator_explicit", confidence=1, extractor_version="v1", sources=[])
    valid = ItemMutation(
        kind="notation",
        logical_key="eta",
        canonical_text="eta is 0.2",
        epistemic_status="operator_explicit",
        confidence=1,
        extractor_version="v1",
        sources=[SourceSpanProposal(source_id=source_id, source_role="assertion", span_index=0, span_kind="inline_math", span_start=0, span_end=3, span_hash="b" * 64)],
    )
    assert valid.logical_key == "eta"


def test_supersession_rejects_self_targets_and_cycles() -> None:
    item_a, item_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValidationError, match="cannot target itself"):
        TransitionMutation(
            item_id=item_a,
            item_version=1,
            transition="supersede",
            target_item_id=item_a,
            target_version=1,
            source_id=uuid.uuid4(),
        )
    with pytest.raises(LedgerAdmissionError, match="cycle"):
        _assert_acyclic_supersession({(item_a, 1): (item_b, 1), (item_b, 1): (item_a, 1)})


async def _claimed_source(db_session, *, project: Project, content: str, key: str):
    payload = content.encode("utf-8")
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="task_result",
            source_key=key,
            source_revision="1",
            content_hash=sha256_hex(payload),
            occurred_at=_utcnow(),
            actor_kind="user",
            actor_id="operator",
            canonical_payload=payload,
            mutable=True,
        ),
    )
    job = await enqueue_contiguous_job(db_session, project.id)
    assert job is not None
    claimed = await claim_consolidation_job(
        db_session, job_id=job.id, lease_owner="ledger-test"
    )
    return receipt.source, claimed


def _proposal(*, job_id: uuid.UUID, source_id: uuid.UUID, source_hash: str, base: int, item_id: uuid.UUID | None = None, text: str = "eta is 0.2", key: str = "eta", transition: str | None = None) -> LedgerProposal:
    raw = text.encode("utf-8")
    items = [
        ItemMutation(
            item_id=item_id,
            kind="notation",
            logical_key=key,
            canonical_text=text,
            epistemic_status="operator_explicit",
            confidence=1,
            extractor_version="unit-v1",
            sources=[
                SourceSpanProposal(
                    source_id=source_id,
                    source_role="assertion",
                    span_index=0,
                    span_kind="text",
                    span_start=0,
                    span_end=len(raw),
                    span_hash=sha256_hex(raw),
                )
            ],
        )
    ]
    transitions = []
    if transition is not None:
        assert item_id is not None
        transitions.append(
            {
                "item_id": item_id,
                "item_version": 2,
                "transition": transition,
                "source_id": source_id,
            }
        )
    return LedgerProposal(
        schema_version=1,
        job_id=job_id,
        base_ledger_revision=base,
        idempotency_key=f"proposal-{job_id}",
        dispositions=[
            SourceDispositionMutation(
                source_id=source_id, source_hash=source_hash, status="incorporated"
            )
        ],
        items=items,
        transitions=transitions,
    )


@pytest.mark.asyncio
async def test_admission_is_atomic_provenanced_and_exactly_replayable(db_session):
    project = Project(name="memory-ledger", user_id="memory-ledger-user")
    db_session.add(project)
    await db_session.flush()
    source, job = await _claimed_source(
        db_session, project=project, content="eta is 0.2", key="task-1"
    )
    proposal = _proposal(
        job_id=job.id,
        source_id=source.id,
        source_hash=source.content_hash,
        base=0,
    )
    no_memory = proposal.model_copy(
        update={
            "dispositions": (
                SourceDispositionMutation(
                    source_id=source.id,
                    source_hash=source.content_hash,
                    status="no_memory",
                ),
            )
        }
    )
    with pytest.raises(LedgerAdmissionError, match="require incorporated"):
        await validate_and_admit_proposal(
            db_session,
            no_memory,
            lease_owner="ledger-test",
            lease_generation=job.lease_generation,
        )

    accepted = await validate_and_admit_proposal(
        db_session,
        proposal,
        lease_owner="ledger-test",
        lease_generation=job.lease_generation,
    )
    assert accepted.ledger_revision == 1
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None and (state.ledger_revision, state.incorporated_source_sequence) == (1, 1)
    item = await db_session.scalar(select(StrategicMemoryItem).where(StrategicMemoryItem.project_id == project.id))
    assert item is not None and item.current_version == 1

    replay = await validate_and_admit_proposal(
        db_session,
        proposal,
        lease_owner="ledger-test",
        lease_generation=job.lease_generation,
    )
    assert replay.replayed and replay.ledger_revision == 1
    with pytest.raises(LedgerAdmissionError, match="exact proposal replay"):
        await validate_and_admit_proposal(
            db_session,
            proposal.model_copy(update={"idempotency_key": "a-different-receipt"}),
            lease_owner="ledger-test",
            lease_generation=job.lease_generation,
        )


@pytest.mark.asyncio
async def test_as_of_resolution_uses_source_sequence_and_item_version(db_session):
    project = Project(name="memory-asof", user_id="memory-asof-user")
    db_session.add(project)
    await db_session.flush()
    first_source, first_job = await _claimed_source(
        db_session, project=project, content="eta is 0.2", key="task-1"
    )
    await validate_and_admit_proposal(
        db_session,
        _proposal(job_id=first_job.id, source_id=first_source.id, source_hash=first_source.content_hash, base=0),
        lease_owner="ledger-test",
        lease_generation=first_job.lease_generation,
    )
    item = await db_session.scalar(select(StrategicMemoryItem).where(StrategicMemoryItem.project_id == project.id))
    assert item is not None
    second_source, second_job = await _claimed_source(
        db_session, project=project, content="eta is 0.3", key="task-2"
    )
    await validate_and_admit_proposal(
        db_session,
        _proposal(job_id=second_job.id, source_id=second_source.id, source_hash=second_source.content_hash, base=1, item_id=item.id, text="eta is 0.3", transition="retract"),
        lease_owner="ledger-test",
        lease_generation=second_job.lease_generation,
    )
    at_one = await as_of_memory_items(db_session, project_id=project.id, source_sequence=1)
    at_two = await as_of_memory_items(db_session, project_id=project.id, source_sequence=2)
    assert [(entry["version"], entry["status"], entry["canonical_text"]) for entry in at_one] == [(1, "active", "eta is 0.2")]
    assert [(entry["version"], entry["status"], entry["canonical_text"]) for entry in at_two] == [(2, "retracted", "eta is 0.3")]
    await db_session.refresh(item)
    assert (item.current_version, item.current_status) == (2, "retracted")


@pytest.mark.asyncio
async def test_failed_admission_rolls_back_to_savepoint_before_outer_commit(db_session):
    project = Project(name="memory-savepoint", user_id="memory-savepoint-user")
    db_session.add(project)
    await db_session.flush()
    source, job = await _claimed_source(
        db_session, project=project, content="eta is 0.2", key="task-1"
    )
    proposal = _proposal(
        job_id=job.id, source_id=source.id, source_hash=source.content_hash, base=0
    ).model_copy(
        update={
            "edges": (
                EdgeMutation(
                    from_item_id=uuid.uuid4(),
                    from_version=1,
                    relation="supports",
                    to_item_id=uuid.uuid4(),
                    to_version=1,
                    confidence=1,
                    source_ids=(source.id,),
                ),
            )
        }
    )
    with pytest.raises(LedgerAdmissionError, match="edge endpoint"):
        await validate_and_admit_proposal(
            db_session,
            proposal,
            lease_owner="ledger-test",
            lease_generation=job.lease_generation,
        )
    await db_session.commit()
    assert await db_session.scalar(
        select(StrategicMemoryItem).where(StrategicMemoryItem.project_id == project.id)
    ) is None
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None and state.ledger_revision == 0


@pytest.mark.asyncio
async def test_as_of_and_projection_exclude_redacted_provenance(db_session):
    project = Project(name="memory-privacy", user_id="memory-privacy-user")
    db_session.add(project)
    await db_session.flush()
    source, job = await _claimed_source(
        db_session, project=project, content="eta is 0.2", key="task-1"
    )
    await validate_and_admit_proposal(
        db_session,
        _proposal(job_id=job.id, source_id=source.id, source_hash=source.content_hash, base=0),
        lease_owner="ledger-test",
        lease_generation=job.lease_generation,
    )
    source.payload_state = "redacted"
    source.tombstoned_at = _utcnow()
    db_session.add(source)
    await db_session.flush()
    assert await as_of_memory_items(
        db_session, project_id=project.id, source_sequence=1
    ) == []
    await rebuild_present_projection(
        db_session, project_id=project.id, ledger_revision=1, through_source_sequence=1
    )
    projection = await db_session.get(StrategicMemoryProjection, project.id)
    assert projection is not None and projection.structured_state["items"] == []


@pytest.mark.asyncio
async def test_as_of_suppresses_unresolved_correction_barrier_then_restores_on_resolution(db_session):
    project = Project(name="memory-barrier", user_id="memory-barrier-user")
    db_session.add(project)
    await db_session.flush()
    source, job = await _claimed_source(
        db_session, project=project, content="eta is 0.2", key="task-1"
    )
    await validate_and_admit_proposal(
        db_session,
        _proposal(job_id=job.id, source_id=source.id, source_hash=source.content_hash, base=0),
        lease_owner="ledger-test",
        lease_generation=job.lease_generation,
    )
    item = await db_session.scalar(
        select(StrategicMemoryItem).where(StrategicMemoryItem.project_id == project.id)
    )
    assert item is not None
    barrier = StrategicMemoryCorrectionBarrier(
        project_id=project.id,
        correction_source_id=source.id,
        candidate_item_id=item.id,
        candidate_version=1,
        match_kind="exact",
        match_score=1,
        context_generation=0,
    )
    db_session.add(barrier)
    await db_session.flush()
    db_session.add(
        StrategicMemoryCorrectionBarrierEvent(
            project_id=project.id,
            barrier_id=barrier.id,
            event_type="opened",
            source_id=source.id,
            actor_kind="operator",
            reason="correction",
            recorded_source_sequence=1,
            context_generation=0,
        )
    )
    await db_session.flush()
    assert await as_of_memory_items(
        db_session, project_id=project.id, source_sequence=1
    ) == []
    db_session.add(
        StrategicMemoryCorrectionBarrierEvent(
            project_id=project.id,
            barrier_id=barrier.id,
            event_type="resolved",
            source_id=source.id,
            actor_kind="consolidator",
            reason="transition recorded",
            recorded_source_sequence=1,
            context_generation=0,
        )
    )
    await db_session.flush()
    restored = await as_of_memory_items(
        db_session, project_id=project.id, source_sequence=1
    )
    assert [(entry["version"], entry["status"]) for entry in restored] == [(1, "active")]
