"""Control-plane invariants for Strategic Memory source processing."""

from datetime import timedelta
import uuid

import pytest
from sqlmodel import select

from app.models import (
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicMemoryProjectState,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
)
from app.strategic_memory_control import (
    SourceDispositionDecision,
    SourceControlInvariantError,
    SourceJobLeaseError,
    advance_incorporation_watermark,
    audit_source_control,
    claim_consolidation_job,
    complete_consolidation_job,
    enqueue_contiguous_job,
    fail_consolidation_job,
    freeze_backfill_cursor,
    renew_consolidation_job,
    update_backfill_progress,
)
from app.time_utils import _utcnow


async def _project(db_session) -> Project:
    project = Project(name="memory-control", user_id="memory-control-user")
    db_session.add(project)
    await db_session.flush()
    return project


async def _source(
    db_session,
    *,
    project_id: uuid.UUID,
    sequence: int,
    status: str = "pending",
) -> StrategicMemorySource:
    source = StrategicMemorySource(
        project_id=project_id,
        source_sequence=sequence,
        native_sequence=sequence,
        ingestion_origin="live",
        source_type="project_message",
        source_key=f"message-{sequence}",
        source_revision=str(sequence),
        content_hash=f"{sequence:064x}",
        occurred_at=_utcnow(),
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        StrategicMemorySourceDisposition(source_id=source.id, status=status)
    )
    await db_session.flush()
    return source


@pytest.mark.asyncio
async def test_auditor_exposes_gap_and_nonterminal_below_watermark(db_session):
    project = await _project(db_session)
    db_session.add(
        StrategicMemoryProjectState(
            project_id=project.id,
            source_high_watermark=3,
            incorporated_source_sequence=2,
        )
    )
    await _source(
        db_session, project_id=project.id, sequence=1, status="incorporated"
    )
    await _source(db_session, project_id=project.id, sequence=2, status="pending")

    audit = await audit_source_control(db_session, project.id)

    assert audit.missing_sequences == (3,)
    assert audit.missing_dispositions == ()
    assert audit.nonterminal_below_watermark == (2,)
    assert not audit.receipt_complete
    assert not audit.watermark_valid


@pytest.mark.asyncio
async def test_watermark_advances_only_over_contiguous_terminal_prefix(db_session):
    project = await _project(db_session)
    state = StrategicMemoryProjectState(
        project_id=project.id,
        source_high_watermark=4,
        incorporated_source_sequence=0,
    )
    db_session.add(state)
    await _source(
        db_session, project_id=project.id, sequence=1, status="incorporated"
    )
    await _source(db_session, project_id=project.id, sequence=2, status="no_memory")
    await _source(db_session, project_id=project.id, sequence=3, status="pending")
    await _source(
        db_session, project_id=project.id, sequence=4, status="quarantined"
    )

    assert await advance_incorporation_watermark(db_session, project.id) == 2
    await db_session.refresh(state)
    assert state.incorporated_source_sequence == 2


@pytest.mark.asyncio
async def test_backfill_cutoff_is_frozen_and_progress_is_monotonic(db_session):
    project = await _project(db_session)
    cursor = await freeze_backfill_cursor(
        db_session,
        project_id=project.id,
        producer="project_message",
        cutoff_spec={"max_sequence": 20},
    )
    same = await freeze_backfill_cursor(
        db_session,
        project_id=project.id,
        producer="project_message",
        cutoff_spec={"max_sequence": 20},
    )
    assert same.id == cursor.id
    with pytest.raises(SourceControlInvariantError):
        await freeze_backfill_cursor(
            db_session,
            project_id=project.id,
            producer="project_message",
            cutoff_spec={"max_sequence": 21},
        )

    cursor = await update_backfill_progress(
        db_session,
        cursor_id=cursor.id,
        cursor_value={"last_sequence": 20},
        scanned_delta=20,
        admitted_delta=18,
        skipped_delta=2,
        complete=True,
    )
    assert cursor.status == "complete"
    assert (cursor.scanned_count, cursor.admitted_count, cursor.skipped_count) == (
        20,
        18,
        2,
    )


@pytest.mark.asyncio
async def test_job_range_is_bounded_and_lease_generation_fences_workers(db_session):
    project = await _project(db_session)
    db_session.add(
        StrategicMemoryProjectState(
            project_id=project.id,
            source_high_watermark=3,
            incorporated_source_sequence=0,
        )
    )
    for sequence in range(1, 4):
        await _source(db_session, project_id=project.id, sequence=sequence)

    job = await enqueue_contiguous_job(db_session, project.id, batch_size=2)
    assert job is not None
    assert (job.source_start, job.source_end) == (1, 2)
    claimed = await claim_consolidation_job(
        db_session,
        job_id=job.id,
        lease_owner="worker-a",
        now=_utcnow(),
    )
    generation = claimed.lease_generation
    assert generation == 1
    with pytest.raises(SourceJobLeaseError):
        await renew_consolidation_job(
            db_session,
            job_id=job.id,
            lease_owner="worker-b",
            lease_generation=generation,
        )
    renewed = await renew_consolidation_job(
        db_session,
        job_id=job.id,
        lease_owner="worker-a",
        lease_generation=generation,
        lease_for=timedelta(minutes=10),
    )
    assert renewed.lease_generation == generation


@pytest.mark.asyncio
async def test_completion_is_exact_lease_fenced_and_advances_atomically(db_session):
    project = await _project(db_session)
    state = StrategicMemoryProjectState(
        project_id=project.id,
        source_high_watermark=2,
        incorporated_source_sequence=0,
        context_authority_generation=4,
        privacy_generation=2,
    )
    db_session.add(state)
    sources = [
        await _source(db_session, project_id=project.id, sequence=sequence)
        for sequence in (1, 2)
    ]
    job = await enqueue_contiguous_job(db_session, project.id)
    assert job is not None
    claimed = await claim_consolidation_job(
        db_session, job_id=job.id, lease_owner="worker-a"
    )

    completed = await complete_consolidation_job(
        db_session,
        job_id=job.id,
        lease_owner="worker-a",
        lease_generation=claimed.lease_generation,
        decisions={
            sources[0].id: SourceDispositionDecision(status="incorporated"),
            sources[1].id: SourceDispositionDecision(status="no_memory"),
        },
    )

    assert completed.status == "complete"
    await db_session.refresh(state)
    assert state.incorporated_source_sequence == 2


@pytest.mark.asyncio
async def test_completion_rejects_generation_change_and_failure_retries(db_session):
    project = await _project(db_session)
    state = StrategicMemoryProjectState(
        project_id=project.id,
        source_high_watermark=1,
        incorporated_source_sequence=0,
    )
    db_session.add(state)
    source = await _source(db_session, project_id=project.id, sequence=1)
    job = await enqueue_contiguous_job(db_session, project.id)
    assert job is not None
    claimed = await claim_consolidation_job(
        db_session, job_id=job.id, lease_owner="worker-a"
    )
    generation = claimed.lease_generation
    state.privacy_generation += 1
    db_session.add(state)
    await db_session.flush()
    with pytest.raises(SourceJobLeaseError, match="generation changed"):
        await complete_consolidation_job(
            db_session,
            job_id=job.id,
            lease_owner="worker-a",
            lease_generation=generation,
            decisions={
                source.id: SourceDispositionDecision(status="incorporated")
            },
        )
    # Restoring the generation simulates a separate fresh fixture transaction;
    # failure handling itself remains exact-lease fenced and does not mutate
    # source disposition or watermark.
    state.privacy_generation -= 1
    db_session.add(state)
    await db_session.flush()
    retried = await fail_consolidation_job(
        db_session,
        job_id=job.id,
        lease_owner="worker-a",
        lease_generation=generation,
        error="provider unavailable",
        max_attempts=2,
    )
    assert retried.status == "retry"
    assert state.incorporated_source_sequence == 0


@pytest.mark.asyncio
async def test_redacted_pending_source_is_terminal_and_cannot_wedge_queue(db_session):
    project = await _project(db_session)
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="remember eta = 0.2",
    )
    db_session.add(message)
    await db_session.flush()
    source = await db_session.scalar(
        select(StrategicMemorySource).where(
            StrategicMemorySource.project_id == project.id,
            StrategicMemorySource.source_key == str(message.id),
        )
    )
    assert source is not None
    job = await enqueue_contiguous_job(db_session, project.id)
    assert job is not None

    message.content = "[redacted]"
    db_session.add(message)
    await db_session.flush()

    disposition = await db_session.get(StrategicMemorySourceDisposition, source.id)
    await db_session.refresh(job)
    assert source.payload_state == "redacted"
    assert disposition is not None
    assert disposition.status == "no_memory"
    assert disposition.reason == "source_redacted"
    assert job.status == "dead"

    replacement = await enqueue_contiguous_job(db_session, project.id)
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert replacement is None
    assert state is not None
    assert state.incorporated_source_sequence == 1
