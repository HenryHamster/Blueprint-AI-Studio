"""Focused bounded-runtime coverage for Strategic Memory source processing."""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from app.models import (
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicMemoryBackfillCursor,
    StrategicMemoryConsolidationJob,
    StrategicMemorySource,
    Task,
)
from app.strategic_memory_control import SourceJobLeaseError
from app.strategic_memory_governance import delete_native_project_message_governed
from app.strategic_memory_runtime import (
    ConsolidationBudget,
    MemoryRuntimeError,
    ProviderUsage,
    backfill_project_message_receipts,
    claim_memory_consolidation,
    dispatch_pending_memory_consolidation,
    submit_memory_consolidation,
)
from app.strategic_memory_service import LedgerProposal, SourceDispositionMutation
from app.strategic_memory_sources import SourceReceiptInput, record_source, sha256_hex
from app.time_utils import _utcnow


async def _project(db_session) -> Project:
    project = Project(name="memory-runtime", user_id="memory-runtime-user")
    db_session.add(project)
    await db_session.flush()
    return project


async def _message(db_session, *, project_id: uuid.UUID, sequence: int, content: str) -> ProjectMessage:
    message = ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        actor_user_id="operator",
        content=content,
    )
    db_session.add(message)
    await db_session.flush()
    return message


def _budget(*, input_bytes: int = 4096) -> ConsolidationBudget:
    return ConsolidationBudget(
        allowed_providers=frozenset({"openai", "anthropic"}),
        max_prompt_tokens=500,
        max_completion_tokens=200,
        max_total_tokens=600,
        max_input_bytes=input_bytes,
    )


@pytest.mark.asyncio
async def test_project_message_backfill_freezes_cursor_resumes_and_debounces_job(db_session):
    project = await _project(db_session)
    await _message(db_session, project_id=project.id, sequence=1, content="one")
    await _message(db_session, project_id=project.id, sequence=2, content="two")
    await _message(db_session, project_id=project.id, sequence=3, content="three")

    first = await backfill_project_message_receipts(
        db_session,
        project_id=project.id,
        batch_size=2,
        enqueue_batch_size=2,
        enqueue=True,
    )
    assert (first.scanned, first.complete, first.queued_job_id is not None) == (2, False, True)
    cursor = await db_session.scalar(
        select(StrategicMemoryBackfillCursor).where(
            StrategicMemoryBackfillCursor.project_id == project.id
        )
    )
    assert cursor is not None and cursor.cursor == {"last_sequence": 2}

    # New history after the first page is deliberately outside the frozen run.
    await _message(db_session, project_id=project.id, sequence=4, content="later")
    second = await backfill_project_message_receipts(
        db_session,
        project_id=project.id,
        batch_size=2,
        enqueue_batch_size=2,
        enqueue=True,
    )
    assert (second.scanned, second.complete) == (1, True)
    await db_session.refresh(cursor)
    assert cursor.cutoff_spec == {"max_sequence": 3}
    assert cursor.cursor == {"last_sequence": 3}
    # Existing active job is returned rather than a duplicate being queued.
    assert second.queued_job_id == first.queued_job_id


@pytest.mark.asyncio
async def test_backfill_skips_governed_privacy_tombstones_without_enqueuing(db_session):
    project = await _project(db_session)
    message = await _message(
        db_session,
        project_id=project.id,
        sequence=1,
        content="private historical fact",
    )
    await delete_native_project_message_governed(
        db_session,
        project_id=project.id,
        message_id=message.id,
    )
    await db_session.flush()

    result = await backfill_project_message_receipts(
        db_session,
        project_id=project.id,
        batch_size=10,
        enqueue=False,
        expected_cursor_id=None,
        expected_last_sequence=0,
    )

    assert (result.scanned, result.admitted, result.skipped, result.complete) == (
        1,
        0,
        1,
        True,
    )
    assert result.queued_job_id is None
    receipt = await db_session.scalar(
        select(StrategicMemorySource).where(
            StrategicMemorySource.project_id == project.id,
            StrategicMemorySource.source_key == str(message.id),
            StrategicMemorySource.payload_state == "active",
        )
    )
    assert receipt is None


@pytest.mark.asyncio
async def test_claim_is_no_workspace_receipt_bound_and_fails_closed_on_input_budget(db_session):
    project = await _project(db_session)
    await _message(db_session, project_id=project.id, sequence=1, content="operator fact")
    queued = await backfill_project_message_receipts(
        db_session, project_id=project.id, batch_size=10, enqueue=True
    )
    assert queued.queued_job_id is not None
    with pytest.raises(MemoryRuntimeError, match="input byte budget"):
        await claim_memory_consolidation(
            db_session,
            job_id=queued.queued_job_id,
            lease_owner="memory-worker-too-small",
            budget=_budget(input_bytes=1),
        )
    claim = await claim_memory_consolidation(
        db_session,
        job_id=queued.queued_job_id,
        lease_owner="pooled-memory-worker",
        budget=_budget(),
    )
    assert claim.workspace_requirement == "none"
    assert claim.capability == "MEMORY_CONSOLIDATION"
    assert claim.sources[0].content_hash == sha256_hex("operator fact")
    with pytest.raises(SourceJobLeaseError, match="live lease"):
        await claim_memory_consolidation(
            db_session,
            job_id=queued.queued_job_id,
            lease_owner="another-worker",
            budget=_budget(),
        )


@pytest.mark.asyncio
async def test_claim_rejects_foreign_task_project_before_loading_source_bytes(db_session):
    owner_project = await _project(db_session)
    foreign_project = await _project(db_session)
    await _message(db_session, project_id=owner_project.id, sequence=1, content="private owner bytes")
    queued = await backfill_project_message_receipts(
        db_session, project_id=owner_project.id, enqueue=True
    )
    assert queued.queued_job_id is not None

    with pytest.raises(SourceJobLeaseError, match="outside task project"):
        await claim_memory_consolidation(
            db_session,
            job_id=queued.queued_job_id,
            lease_owner="foreign-task",
            expected_project_id=foreign_project.id,
            budget=_budget(),
        )
    job = await db_session.get(StrategicMemoryConsolidationJob, queued.queued_job_id)
    assert job is not None and job.status == "queued" and job.lease_owner is None


@pytest.mark.asyncio
async def test_live_receipt_dispatches_one_idempotent_task_then_claim_submit_ledger(
    db_session, monkeypatch
):
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "true")
    project = await _project(db_session)
    payload = b"durable live receipt"
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="project_message",
            source_key="one",
            source_revision="1",
            content_hash=sha256_hex(payload),
            occurred_at=_utcnow(),
            canonical_payload=payload,
            mutable=True,
        ),
    )
    assert receipt.created

    first = await dispatch_pending_memory_consolidation(db_session)
    second = await dispatch_pending_memory_consolidation(db_session)
    assert (first.created_tasks, second.created_tasks) == (1, 0)
    tasks = list(
        (await db_session.execute(select(Task).where(Task.project_id == project.id))).scalars()
    )
    assert len(tasks) == 1
    task = tasks[0]
    assert task.first_payload["type"] == "memory_consolidation"
    assert task.context["provider_preference"] == ["anthropic"]
    job_id = uuid.UUID(str(task.first_payload["job_id"]))
    job = await db_session.get(StrategicMemoryConsolidationJob, job_id)
    assert job is not None and job.status == "queued"

    claim = await claim_memory_consolidation(
        db_session, job_id=job_id, lease_owner=str(task.id), expected_project_id=project.id, budget=_budget()
    )
    result = await submit_memory_consolidation(
        db_session,
        claim=claim,
        proposal=LedgerProposal(
            schema_version=1,
            job_id=claim.job_id,
            base_ledger_revision=claim.base_ledger_revision,
            idempotency_key="live-receipt-e2e",
            dispositions=(
                SourceDispositionMutation(
                    source_id=claim.sources[0].source_id,
                    source_hash=claim.sources[0].content_hash,
                    status="no_memory",
                    classifier_version="memory-consolidation-v1",
                ),
            ),
        ),
        usage=ProviderUsage("openai", "gpt-fast", "memory-v1", 10, 5),
        budget=_budget(),
    )
    assert result.job_id == job_id
    await db_session.refresh(job)
    assert job.status == "complete" and job.resulting_ledger_revision == result.ledger_revision


@pytest.mark.asyncio
async def test_dispatch_never_queues_an_unclaimable_receipt_prefix(db_session, monkeypatch):
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "true")
    project = await _project(db_session)
    payload = b"x" * 7_000  # exceeds the 4096-token worker's conservative 6144-byte input cap
    await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="project_message",
            source_key="oversized",
            source_revision="1",
            content_hash=sha256_hex(payload),
            occurred_at=_utcnow(),
            canonical_payload=payload,
            mutable=True,
        ),
    )
    result = await dispatch_pending_memory_consolidation(db_session)
    jobs = list(
        (await db_session.execute(select(StrategicMemoryConsolidationJob).where(
            StrategicMemoryConsolidationJob.project_id == project.id
        ))).scalars()
    )
    tasks = list(
        (await db_session.execute(select(Task).where(Task.project_id == project.id))).scalars()
    )
    assert result.created_tasks == 0
    assert jobs == [] and tasks == []


@pytest.mark.asyncio
async def test_submit_uses_installed_schema_and_persists_bounded_provider_metadata(db_session):
    project = await _project(db_session)
    await _message(db_session, project_id=project.id, sequence=1, content="no durable fact")
    queued = await backfill_project_message_receipts(
        db_session, project_id=project.id, batch_size=10, enqueue=True
    )
    assert queued.queued_job_id is not None
    claim = await claim_memory_consolidation(
        db_session,
        job_id=queued.queued_job_id,
        lease_owner="pooled-memory-worker",
        budget=_budget(),
    )
    proposal = LedgerProposal(
        schema_version=1,
        job_id=claim.job_id,
        base_ledger_revision=claim.base_ledger_revision,
        idempotency_key="runtime-no-memory",
        dispositions=(
            SourceDispositionMutation(
                source_id=claim.sources[0].source_id,
                source_hash=claim.sources[0].content_hash,
                status="no_memory",
                classifier_version="memory-consolidation-v1",
            ),
        ),
    )
    result = await submit_memory_consolidation(
        db_session,
        claim=claim,
        proposal=proposal,
        usage=ProviderUsage("openai", "gpt-fast", "memory-v1", 10, 5),
        budget=_budget(),
    )
    assert result.job_id == claim.job_id
    job = await db_session.get(StrategicMemoryConsolidationJob, claim.job_id)
    assert job is not None
    assert (job.status, job.model, job.prompt_version) == (
        "complete",
        "openai:gpt-fast",
        "memory-v1",
    )


def test_provider_usage_budget_is_strict_and_provider_neutral():
    from app.strategic_memory_runtime import _validate_usage

    _validate_usage(
        ProviderUsage("openai", "gpt-fast", "memory-v1", 400, 100), _budget()
    )
    with pytest.raises(MemoryRuntimeError, match="not allowed"):
        _validate_usage(
            ProviderUsage("other", "model", "memory-v1", 1, 1), _budget()
        )
    with pytest.raises(MemoryRuntimeError, match="token budget"):
        _validate_usage(
            ProviderUsage("openai", "model", "memory-v1", 501, 1), _budget()
        )
