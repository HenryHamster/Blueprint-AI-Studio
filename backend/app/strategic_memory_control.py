"""Durable control-plane operations for Strategic Memory source receipts.

This module deliberately stops at receipt completeness and leased range
coordination.  Semantic admission is owned by the V2 ledger service added in
the next phase.  Keeping the boundary explicit lets operators prove that no
durable source was silently skipped before any model is allowed to extract
memory from it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    StrategicMemoryBackfillCursor,
    StrategicMemoryConsolidationJob,
    StrategicMemoryProjectState,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
)
from app.strategic_memory_sources import lock_or_create_source_project_state
from app.time_utils import _utcnow


TERMINAL_DISPOSITIONS = frozenset({"incorporated", "no_memory", "quarantined"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "retry"})
DEFAULT_JOB_BATCH_SIZE = 64
DEFAULT_JOB_LEASE = timedelta(minutes=5)
CONSERVATIVE_PROMPT_OVERHEAD_TOKENS = 1024


class SourceControlInvariantError(RuntimeError):
    """The receipt stream is inconsistent and processing must fail closed."""


class SourceJobLeaseError(RuntimeError):
    """A stale or foreign consolidation worker attempted a state transition."""


@dataclass(frozen=True)
class SourceDispositionDecision:
    """One deterministic terminal classification from a fenced job."""

    status: str
    reason: str | None = None
    classifier_version: str | None = None
    extractor_model: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class SourceControlAudit:
    project_id: uuid.UUID
    source_high_watermark: int
    incorporated_source_sequence: int
    receipt_count: int
    pending_count: int
    terminal_count: int
    missing_sequences: tuple[int, ...]
    missing_dispositions: tuple[int, ...]
    nonterminal_below_watermark: tuple[int, ...]
    cursor_statuses: tuple[tuple[str, str], ...]

    @property
    def receipt_complete(self) -> bool:
        return not self.missing_sequences and not self.missing_dispositions

    @property
    def watermark_valid(self) -> bool:
        return (
            self.incorporated_source_sequence <= self.source_high_watermark
            and not self.nonterminal_below_watermark
        )

    @property
    def backfill_complete(self) -> bool:
        return bool(self.cursor_statuses) and all(
            status == "complete" for _, status in self.cursor_statuses
        )


async def lock_source_project_state(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> StrategicMemoryProjectState:
    """Return the project receipt state under its canonical row lock."""

    return await lock_or_create_source_project_state(session, project_id)


async def audit_source_control(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    missing_sample_limit: int = 100,
) -> SourceControlAudit:
    """Audit receipt/disposition contiguity without mutating durable state."""

    state = await session.get(StrategicMemoryProjectState, project_id)
    high = int(state.source_high_watermark) if state is not None else 0
    incorporated = int(state.incorporated_source_sequence) if state is not None else 0

    rows = (
        await session.execute(
            select(
                StrategicMemorySource.source_sequence,
                StrategicMemorySourceDisposition.status,
            )
            .select_from(StrategicMemorySource)
            .outerjoin(
                StrategicMemorySourceDisposition,
                StrategicMemorySourceDisposition.source_id
                == StrategicMemorySource.id,
            )
            .where(StrategicMemorySource.project_id == project_id)
            .order_by(StrategicMemorySource.source_sequence)
        )
    ).all()
    by_sequence = {int(sequence): status for sequence, status in rows}
    missing_sequences = tuple(
        sequence
        for sequence in range(1, high + 1)
        if sequence not in by_sequence
    )[:missing_sample_limit]
    missing_dispositions = tuple(
        sequence
        for sequence, status in by_sequence.items()
        if sequence <= high and status is None
    )[:missing_sample_limit]
    nonterminal_below = tuple(
        sequence
        for sequence, status in by_sequence.items()
        if sequence <= incorporated and status not in TERMINAL_DISPOSITIONS
    )[:missing_sample_limit]
    pending_count = sum(status == "pending" for status in by_sequence.values())
    terminal_count = sum(status in TERMINAL_DISPOSITIONS for status in by_sequence.values())

    cursors = (
        await session.execute(
            select(
                StrategicMemoryBackfillCursor.producer,
                StrategicMemoryBackfillCursor.status,
            )
            .where(StrategicMemoryBackfillCursor.project_id == project_id)
            .order_by(StrategicMemoryBackfillCursor.producer)
        )
    ).all()
    return SourceControlAudit(
        project_id=project_id,
        source_high_watermark=high,
        incorporated_source_sequence=incorporated,
        receipt_count=len(rows),
        pending_count=pending_count,
        terminal_count=terminal_count,
        missing_sequences=missing_sequences,
        missing_dispositions=missing_dispositions,
        nonterminal_below_watermark=nonterminal_below,
        cursor_statuses=tuple((str(producer), str(status)) for producer, status in cursors),
    )


async def advance_incorporation_watermark(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Advance only across a gap-free prefix of terminal dispositions."""

    state = await lock_source_project_state(session, project_id)
    start = int(state.incorporated_source_sequence) + 1
    high = int(state.source_high_watermark)
    if start > high:
        return int(state.incorporated_source_sequence)

    rows = (
        await session.execute(
            select(
                StrategicMemorySource.source_sequence,
                StrategicMemorySourceDisposition.status,
            )
            .select_from(StrategicMemorySource)
            .outerjoin(
                StrategicMemorySourceDisposition,
                StrategicMemorySourceDisposition.source_id
                == StrategicMemorySource.id,
            )
            .where(
                StrategicMemorySource.project_id == project_id,
                StrategicMemorySource.source_sequence >= start,
                StrategicMemorySource.source_sequence <= high,
            )
            .order_by(StrategicMemorySource.source_sequence)
        )
    ).all()
    expected = start
    advanced = start - 1
    for sequence_raw, status in rows:
        sequence = int(sequence_raw)
        if sequence != expected:
            break
        if status not in TERMINAL_DISPOSITIONS:
            break
        advanced = sequence
        expected += 1
    if advanced >= start:
        state.incorporated_source_sequence = advanced
        state.updated_at = _utcnow()
        session.add(state)
        await session.flush()
    return int(state.incorporated_source_sequence)


async def freeze_backfill_cursor(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    producer: str,
    cutoff_spec: dict[str, Any],
) -> StrategicMemoryBackfillCursor:
    """Create a producer-specific frozen cutoff, or validate exact replay."""

    producer = producer.strip()
    if not producer:
        raise ValueError("producer is required")
    existing = await session.scalar(
        select(StrategicMemoryBackfillCursor)
        .where(
            StrategicMemoryBackfillCursor.project_id == project_id,
            StrategicMemoryBackfillCursor.producer == producer,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.cutoff_spec != cutoff_spec:
            raise SourceControlInvariantError(
                f"backfill cutoff for producer {producer!r} is already frozen"
            )
        return existing
    cursor = StrategicMemoryBackfillCursor(
        project_id=project_id,
        producer=producer,
        cutoff_spec=cutoff_spec,
        cursor={},
        status="pending",
    )
    session.add(cursor)
    await session.flush()
    return cursor


async def update_backfill_progress(
    session: AsyncSession,
    *,
    cursor_id: uuid.UUID,
    cursor_value: dict[str, Any],
    scanned_delta: int,
    admitted_delta: int,
    skipped_delta: int,
    complete: bool = False,
    error: str | None = None,
) -> StrategicMemoryBackfillCursor:
    """Advance a frozen producer cursor monotonically under a row lock."""

    if min(scanned_delta, admitted_delta, skipped_delta) < 0:
        raise ValueError("backfill count deltas must be nonnegative")
    if admitted_delta + skipped_delta > scanned_delta:
        raise ValueError("admitted plus skipped cannot exceed scanned")
    cursor = await session.scalar(
        select(StrategicMemoryBackfillCursor)
        .where(StrategicMemoryBackfillCursor.id == cursor_id)
        .with_for_update()
    )
    if cursor is None:
        raise SourceControlInvariantError("backfill cursor not found")
    if cursor.status == "complete":
        if any((scanned_delta, admitted_delta, skipped_delta, error)):
            raise SourceControlInvariantError("completed backfill cursor is immutable")
        return cursor
    cursor.cursor = cursor_value
    cursor.scanned_count += scanned_delta
    cursor.admitted_count += admitted_delta
    cursor.skipped_count += skipped_delta
    cursor.error = error
    cursor.status = "failed" if error else ("complete" if complete else "running")
    cursor.updated_at = _utcnow()
    session.add(cursor)
    await session.flush()
    return cursor


async def enqueue_contiguous_job(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    batch_size: int = DEFAULT_JOB_BATCH_SIZE,
    max_input_bytes: int | None = None,
    max_prompt_tokens: int | None = None,
) -> StrategicMemoryConsolidationJob | None:
    """Queue one gap-free pending range without crossing an existing gap."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if (max_input_bytes is None) != (max_prompt_tokens is None):
        raise ValueError("memory dispatch byte and prompt caps must be supplied together")
    if max_input_bytes is not None and (
        max_input_bytes < 1
        or max_prompt_tokens is None
        or max_prompt_tokens <= CONSERVATIVE_PROMPT_OVERHEAD_TOKENS
    ):
        raise ValueError("memory dispatch caps are invalid")
    state = await lock_source_project_state(session, project_id)
    # Governed privacy can terminally classify an unincorporated receipt
    # without a model job. Consume that prefix before considering active work.
    await advance_incorporation_watermark(session, project_id)
    await session.refresh(state)
    active = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(
            StrategicMemoryConsolidationJob.project_id == project_id,
            StrategicMemoryConsolidationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(StrategicMemoryConsolidationJob.created_at)
        .with_for_update()
    )
    if active is not None:
        stale_reason: str | None = None
        if (
            int(active.context_authority_generation)
            != int(state.context_authority_generation)
            or int(active.privacy_generation) != int(state.privacy_generation)
        ):
            stale_reason = "source_generation_changed"
        elif (
            active.mode == "contiguous"
            and active.source_start is not None
            and int(active.source_start)
            != int(state.incorporated_source_sequence) + 1
        ):
            stale_reason = "source_watermark_advanced"
        if stale_reason is None:
            if active.mode == "reconsolidation":
                source_scope = StrategicMemorySource.id == active.target_source_id
            elif active.source_start is not None and active.source_end is not None:
                source_scope = and_(
                    StrategicMemorySource.source_sequence >= active.source_start,
                    StrategicMemorySource.source_sequence <= active.source_end,
                )
            else:
                stale_reason = "source_range_invalid"
                source_scope = StrategicMemorySource.id == uuid.uuid4()
            inactive_count = await session.scalar(
                select(func.count())
                .select_from(StrategicMemorySource)
                .where(
                    StrategicMemorySource.project_id == project_id,
                    StrategicMemorySource.payload_state != "active",
                    source_scope,
                )
            )
            if int(inactive_count or 0) > 0:
                stale_reason = "source_unavailable"
        if stale_reason is None:
            # Jobs queued before the dispatch bridge may not carry its bounded
            # transport policy. Never attach a pooled Task to one unless its
            # exact resolved receipt range also fits today's worker cap.
            if max_input_bytes is not None and active.mode == "contiguous":
                active_sources = list(
                    (
                        await session.execute(
                            select(StrategicMemorySource)
                            .where(
                                StrategicMemorySource.project_id == project_id,
                                StrategicMemorySource.source_sequence >= int(active.source_start or 0),
                                StrategicMemorySource.source_sequence <= int(active.source_end or -1),
                            )
                            .order_by(StrategicMemorySource.source_sequence)
                        )
                    ).scalars()
                )
                from app.strategic_memory_sources import resolve_source_receipt

                active_bytes = 0
                assert max_prompt_tokens is not None
                active_prompt_byte_cap = (
                    max_prompt_tokens - CONSERVATIVE_PROMPT_OVERHEAD_TOKENS
                ) * 2
                for source in active_sources:
                    resolved = await resolve_source_receipt(
                        session, source, expected_project_id=project_id
                    )
                    active_bytes += len(resolved.canonical_payload)
                    if active_bytes > max_input_bytes or active_bytes > active_prompt_byte_cap:
                        stale_reason = "active_job_exceeds_memory_worker_input_prompt_budget"
                        state.last_error = stale_reason
                        break
            if stale_reason is None:
                return active
        active.status = "dead"
        active.error = stale_reason
        active.lease_owner = None
        active.lease_expires_at = None
        active.completed_at = _utcnow()
        active.updated_at = active.completed_at
        session.add(active)
        await session.flush()
    start = int(state.incorporated_source_sequence) + 1
    if start > int(state.source_high_watermark):
        return None
    rows = (
        await session.execute(
            select(
                StrategicMemorySource,
                StrategicMemorySourceDisposition.status,
            )
            .join(
                StrategicMemorySourceDisposition,
                StrategicMemorySourceDisposition.source_id
                == StrategicMemorySource.id,
            )
            .where(
                StrategicMemorySource.project_id == project_id,
                StrategicMemorySource.source_sequence >= start,
                StrategicMemorySource.source_sequence
                <= min(int(state.source_high_watermark), start + batch_size - 1),
            )
            .order_by(StrategicMemorySource.source_sequence)
        )
    ).all()
    expected = start
    end: int | None = None
    total_input_bytes = 0
    # Base64 evidence transport and JSON framing use more than one byte/token;
    # halve the available prompt remainder, which is stricter than the worker's
    # actual ~4/9 token-per-canonical-byte expansion.  No unclaimable job is
    # ever created merely because the receipt stream contains a large item.
    prompt_input_byte_cap = (
        (max_prompt_tokens - CONSERVATIVE_PROMPT_OVERHEAD_TOKENS) * 2
        if max_prompt_tokens is not None
        else None
    )
    for source, status in rows:
        sequence = int(source.source_sequence)
        if sequence != expected or status != "pending":
            break
        if max_input_bytes is not None:
            from app.strategic_memory_sources import resolve_source_receipt

            resolved = await resolve_source_receipt(
                session, source, expected_project_id=project_id
            )
            next_total = total_input_bytes + len(resolved.canonical_payload)
            assert prompt_input_byte_cap is not None
            if next_total > max_input_bytes or next_total > prompt_input_byte_cap:
                if end is None:
                    state.last_error = "next pending receipt exceeds memory worker input/prompt budget"
                    state.updated_at = _utcnow()
                    session.add(state)
                    await session.flush()
                break
            total_input_bytes = next_total
        end = sequence
        expected += 1
    if end is None:
        return None
    job = StrategicMemoryConsolidationJob(
        project_id=project_id,
        source_start=start,
        source_end=end,
        mode="contiguous",
        base_ledger_revision=state.ledger_revision,
        context_authority_generation=state.context_authority_generation,
        privacy_generation=state.privacy_generation,
        status="queued",
    )
    session.add(job)
    await session.flush()
    return job


async def claim_consolidation_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    lease_for: timedelta = DEFAULT_JOB_LEASE,
    now: datetime | None = None,
) -> StrategicMemoryConsolidationJob:
    """Claim/reclaim a queued, retryable, or expired leased job."""

    now = now or _utcnow()
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(StrategicMemoryConsolidationJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise SourceJobLeaseError("consolidation job not found")
    expired = job.lease_expires_at is None or job.lease_expires_at <= now
    if job.status == "running" and not expired:
        raise SourceJobLeaseError("consolidation job already has a live lease")
    if job.status not in {"queued", "retry", "running"}:
        raise SourceJobLeaseError(f"consolidation job is terminal: {job.status}")
    job.status = "running"
    job.lease_owner = lease_owner
    job.lease_generation += 1
    job.lease_expires_at = now + lease_for
    job.attempt += 1
    job.error = None
    job.updated_at = now
    session.add(job)
    await session.flush()
    return job


async def renew_consolidation_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    lease_generation: int,
    lease_for: timedelta = DEFAULT_JOB_LEASE,
    now: datetime | None = None,
) -> StrategicMemoryConsolidationJob:
    """Renew only the exact current worker generation."""

    now = now or _utcnow()
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(StrategicMemoryConsolidationJob.id == job_id)
        .with_for_update()
    )
    _require_job_lease(job, lease_owner, lease_generation, now)
    assert job is not None
    job.lease_expires_at = now + lease_for
    job.updated_at = now
    session.add(job)
    await session.flush()
    return job


async def complete_consolidation_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    lease_generation: int,
    decisions: Mapping[uuid.UUID, SourceDispositionDecision],
    now: datetime | None = None,
) -> StrategicMemoryConsolidationJob:
    """Commit terminal source dispositions behind the exact live job lease.

    Semantic ledger writes, when enabled, occur in the caller's same
    transaction before this helper.  This function deliberately never commits:
    any validation, disposition, watermark, or ledger failure rolls the whole
    admission transaction back.
    """

    now = now or _utcnow()
    # Canonical control-plane lock order is state -> job -> sources.  Enqueue
    # follows the same state -> active-job order, avoiding a completion/enqueue
    # cycle under PostgreSQL.
    project_id = await session.scalar(
        select(StrategicMemoryConsolidationJob.project_id).where(
            StrategicMemoryConsolidationJob.id == job_id
        )
    )
    if project_id is None:
        raise SourceJobLeaseError("consolidation job not found")
    state = await lock_source_project_state(session, project_id)
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(StrategicMemoryConsolidationJob.id == job_id)
        .with_for_update()
    )
    _require_job_lease(job, lease_owner, lease_generation, now)
    assert job is not None
    if (
        int(job.context_authority_generation)
        != int(state.context_authority_generation)
        or int(job.privacy_generation) != int(state.privacy_generation)
    ):
        raise SourceJobLeaseError("source authority/privacy generation changed")

    if job.mode == "contiguous":
        if (
            job.source_start is None
            or job.source_end is None
            or int(job.source_start) != int(state.incorporated_source_sequence) + 1
        ):
            raise SourceControlInvariantError(
                "contiguous job no longer begins at the incorporation watermark"
            )
        rows = (
            await session.execute(
                select(StrategicMemorySource, StrategicMemorySourceDisposition)
                .join(
                    StrategicMemorySourceDisposition,
                    StrategicMemorySourceDisposition.source_id
                    == StrategicMemorySource.id,
                )
                .where(
                    StrategicMemorySource.project_id == job.project_id,
                    StrategicMemorySource.source_sequence >= job.source_start,
                    StrategicMemorySource.source_sequence <= job.source_end,
                )
                .order_by(StrategicMemorySource.source_sequence)
                .with_for_update()
            )
        ).all()
        expected_sequences = list(range(int(job.source_start), int(job.source_end) + 1))
        if [int(source.source_sequence) for source, _ in rows] != expected_sequences:
            raise SourceControlInvariantError("contiguous job source range has a gap")
    elif job.mode == "reconsolidation":
        if job.target_source_id is None:
            raise SourceControlInvariantError("reconsolidation job has no target")
        row = (
            await session.execute(
                select(StrategicMemorySource, StrategicMemorySourceDisposition)
                .join(
                    StrategicMemorySourceDisposition,
                    StrategicMemorySourceDisposition.source_id
                    == StrategicMemorySource.id,
                )
                .where(
                    StrategicMemorySource.id == job.target_source_id,
                    StrategicMemorySource.project_id == job.project_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        rows = [] if row is None else [row]
        if not rows or rows[0][1].status != "quarantined":
            raise SourceControlInvariantError(
                "reconsolidation target is no longer quarantined"
            )
    else:  # pragma: no cover - database constraint
        raise SourceControlInvariantError(f"unknown consolidation mode: {job.mode}")

    expected_ids = {source.id for source, _ in rows}
    if set(decisions) != expected_ids:
        raise SourceControlInvariantError(
            "terminal decisions must cover the exact claimed source range"
        )
    for source, disposition in rows:
        if source.payload_state != "active" or source.tombstoned_at is not None:
            raise SourceJobLeaseError("claimed source was redacted or deleted")
        decision = decisions[source.id]
        if decision.status not in TERMINAL_DISPOSITIONS:
            raise SourceControlInvariantError(
                f"invalid terminal disposition: {decision.status}"
            )
        if job.mode == "contiguous" and disposition.status != "pending":
            raise SourceControlInvariantError(
                "contiguous source disposition is no longer pending"
            )
        disposition.status = decision.status
        disposition.reason = decision.reason
        disposition.classifier_version = decision.classifier_version
        disposition.extractor_model = decision.extractor_model
        disposition.prompt_version = decision.prompt_version
        disposition.consolidation_job_id = job.id
        disposition.processed_at = now
        session.add(disposition)

    await session.flush()
    if job.mode == "contiguous":
        advanced = await advance_incorporation_watermark(session, job.project_id)
        if advanced != int(job.source_end):
            raise SourceControlInvariantError(
                "terminal commit did not advance through the exact claimed range"
            )
    job.status = "complete"
    job.completed_at = now
    job.lease_expires_at = None
    job.updated_at = now
    session.add(job)
    await session.flush()
    return job


async def fail_consolidation_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    lease_generation: int,
    error: str,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> StrategicMemoryConsolidationJob:
    """Release a failed exact lease to retry, or make it visibly dead."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    now = now or _utcnow()
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(StrategicMemoryConsolidationJob.id == job_id)
        .with_for_update()
    )
    _require_job_lease(job, lease_owner, lease_generation, now)
    assert job is not None
    job.status = "dead" if int(job.attempt) >= max_attempts else "retry"
    job.error = error[:8_000]
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = now if job.status == "dead" else None
    job.updated_at = now
    session.add(job)
    await session.flush()
    return job


def _require_job_lease(
    job: StrategicMemoryConsolidationJob | None,
    lease_owner: str,
    lease_generation: int,
    now: datetime,
) -> None:
    if job is None:
        raise SourceJobLeaseError("consolidation job not found")
    if (
        job.status != "running"
        or job.lease_owner != lease_owner
        or int(job.lease_generation) != lease_generation
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        raise SourceJobLeaseError("stale or expired consolidation job lease")


def all_producers_complete(
    audit: SourceControlAudit,
    enabled_producers: Iterable[str],
) -> bool:
    """Require every explicitly enabled producer to have completed its cutoff."""

    statuses = dict(audit.cursor_statuses)
    enabled = tuple(enabled_producers)
    return bool(enabled) and all(statuses.get(producer) == "complete" for producer in enabled)
