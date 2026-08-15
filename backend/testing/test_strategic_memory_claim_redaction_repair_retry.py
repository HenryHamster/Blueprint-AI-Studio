"""Retry and sequential-drain contracts for repaired claim-redaction receipts."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.models import (
    Project,
    StrategicMemoryConsolidationJob,
    StrategicMemoryProjectState,
    Task,
)
from app.strategic_memory_claim_redaction_repair import (
    REPAIR_DISPOSITION_REASON,
    REPAIR_POLICY_VERSION,
    enqueue_next_repaired_claim_source,
)
from app.strategic_memory_control import (
    SourceDispositionDecision,
    claim_consolidation_job,
    complete_consolidation_job,
    enqueue_contiguous_job,
    fail_consolidation_job,
)
from app.strategic_memory_runtime import dispatch_pending_memory_consolidation
from app.strategic_memory_sources import SourceReceiptInput, record_source, sha256_hex
from app.time_utils import _utcnow


async def _record_repaired_source(
    db_session,
    *,
    project: Project | None = None,
    source_key: str,
):
    if project is None:
        project = Project(name="repair-retry", user_id="repair-retry-user")
        db_session.add(project)
        await db_session.flush()
    payload = f"restored claim receipt {source_key}".encode()
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="operator_action",
            source_key=source_key,
            source_revision="1",
            content_hash=sha256_hex(payload),
            occurred_at=_utcnow(),
            canonical_payload=payload,
            mutable=True,
        ),
    )
    disposition = receipt.disposition
    disposition.status = "quarantined"
    disposition.reason = REPAIR_DISPOSITION_REASON
    disposition.classifier_version = REPAIR_POLICY_VERSION
    disposition.consolidation_job_id = None
    disposition.processed_at = _utcnow()
    db_session.add(disposition)
    await db_session.flush()
    return project, receipt.source, disposition


@pytest.mark.asyncio
async def test_stale_repair_job_requeues_same_source_then_drains_sequentially(
    db_session,
):
    project, first_source, first_disposition = await _record_repaired_source(
        db_session,
        source_key="first",
    )
    stale_job = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert stale_job is not None
    assert stale_job.target_source_id == first_source.id

    _, second_source, second_disposition = await _record_repaired_source(
        db_session,
        project=project,
        source_key="second",
    )
    while_active = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert while_active is not None and while_active.id == stale_job.id

    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert state is not None
    state.context_authority_generation += 1
    state.privacy_generation += 1
    db_session.add(state)
    await db_session.flush()

    assert await enqueue_contiguous_job(db_session, project.id) is None
    assert stale_job.status == "dead"
    assert stale_job.error == "source_generation_changed"
    assert first_disposition.consolidation_job_id is None

    replacement = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert replacement is not None
    assert replacement.id != stale_job.id
    assert replacement.target_source_id == first_source.id
    assert (
        replacement.context_authority_generation == state.context_authority_generation
    )
    assert replacement.privacy_generation == state.privacy_generation

    replay = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert replay is not None and replay.id == replacement.id

    first_claim = await claim_consolidation_job(
        db_session,
        job_id=replacement.id,
        lease_owner="repair-worker-first",
    )
    completed_first = await complete_consolidation_job(
        db_session,
        job_id=replacement.id,
        lease_owner="repair-worker-first",
        lease_generation=first_claim.lease_generation,
        decisions={
            first_source.id: SourceDispositionDecision(
                status="incorporated",
                reason="repair_reconsolidated",
            )
        },
    )
    assert completed_first.status == "complete"
    assert first_disposition.status == "incorporated"
    assert first_disposition.consolidation_job_id == replacement.id

    next_job = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert next_job is not None
    assert next_job.target_source_id == second_source.id

    second_claim = await claim_consolidation_job(
        db_session,
        job_id=next_job.id,
        lease_owner="repair-worker-second",
    )
    await complete_consolidation_job(
        db_session,
        job_id=next_job.id,
        lease_owner="repair-worker-second",
        lease_generation=second_claim.lease_generation,
        decisions={
            second_source.id: SourceDispositionDecision(
                status="no_memory",
                reason="repair_receipt_not_memorable",
            )
        },
    )
    assert second_disposition.consolidation_job_id == next_job.id
    assert (
        await enqueue_next_repaired_claim_source(
            db_session,
            project_id=project.id,
        )
        is None
    )

    first_jobs = list(
        (
            await db_session.execute(
                select(StrategicMemoryConsolidationJob).where(
                    StrategicMemoryConsolidationJob.target_source_id == first_source.id
                )
            )
        ).scalars()
    )
    assert {job.id for job in first_jobs} == {stale_job.id, replacement.id}


@pytest.mark.asyncio
async def test_terminal_provider_failure_remains_runtime_retryable_and_idempotent(
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "true")
    project, source, disposition = await _record_repaired_source(
        db_session,
        source_key="provider-failure",
    )
    failed_job = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert failed_job is not None
    failed_claim = await claim_consolidation_job(
        db_session,
        job_id=failed_job.id,
        lease_owner="failed-provider-worker",
    )
    terminal = await fail_consolidation_job(
        db_session,
        job_id=failed_job.id,
        lease_owner="failed-provider-worker",
        lease_generation=failed_claim.lease_generation,
        error="provider unavailable",
        max_attempts=1,
    )
    assert terminal.status == "dead"
    assert disposition.status == "quarantined"
    assert disposition.reason == REPAIR_DISPOSITION_REASON
    assert disposition.consolidation_job_id is None

    first_dispatch = await dispatch_pending_memory_consolidation(db_session)
    second_dispatch = await dispatch_pending_memory_consolidation(db_session)
    assert first_dispatch.created_tasks == 1
    assert second_dispatch.created_tasks == 0

    replacement = await enqueue_next_repaired_claim_source(
        db_session,
        project_id=project.id,
    )
    assert replacement is not None
    assert replacement.id != failed_job.id
    assert replacement.target_source_id == source.id
    assert replacement.status == "queued"
    assert (
        await enqueue_next_repaired_claim_source(
            db_session,
            project_id=project.id,
        )
    ).id == replacement.id

    tasks = list(
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["memory_consolidation_job_id"].as_string()
                    == str(replacement.id),
                )
            )
        ).scalars()
    )
    assert len(tasks) == 1
