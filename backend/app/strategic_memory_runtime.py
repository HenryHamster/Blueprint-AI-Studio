"""Bounded, provider-neutral runtime for Strategic Memory consolidation.

This module is intentionally a small vertical slice: it turns existing native
``ProjectMessage`` history into idempotent source receipts, queues at most one
contiguous consolidation job, dispatches that job through the existing pool as
an idempotent no-workspace Task, and lets the worker claim/renew/submit a
receipt-bound installed proposal.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    ProjectMessage,
    StrategicMemoryBackfillCursor,
    StrategicMemoryConsolidationJob,
    StrategicMemoryProjectState,
    Task,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.strategic_memory_control import (
    CONSERVATIVE_PROMPT_OVERHEAD_TOKENS,
    SourceJobLeaseError,
    claim_consolidation_job,
    enqueue_contiguous_job,
    lock_source_project_state,
    renew_consolidation_job,
)
from app.strategic_memory_service import LedgerAdmissionResult, LedgerProposal, validate_and_admit_proposal
from app.strategic_memory_sources import (
    SourceReceiptInput,
    resolve_source_receipt,
    sha256_hex,
    record_source,
)
from app.time_utils import _utcnow


PROJECT_MESSAGE_PRODUCER = "project_message"
MEMORY_CONSOLIDATION_CAPABILITY = "MEMORY_CONSOLIDATION"
WORKSPACE_REQUIREMENT_NONE = "none"
MAX_BACKFILL_BATCH_SIZE = 200
OPERATOR_BACKFILL_BATCH_SIZE = 100
MAX_CONSOLIDATION_INPUT_BYTES = 128 * 1024
MEMORY_TASK_MAX_PROMPT_TOKENS = 4096
MEMORY_TASK_MAX_COMPLETION_TOKENS = 2048
MEMORY_TASK_MAX_TOTAL_TOKENS = 6144
# The job selector applies the same conservative conversion before enqueue;
# keeping the Task claim ceiling identical makes an oversized prefix visible
# and non-dispatchable rather than a repeating provider retry.
MEMORY_TASK_MAX_INPUT_BYTES = min(
    MAX_CONSOLIDATION_INPUT_BYTES,
    (MEMORY_TASK_MAX_PROMPT_TOKENS - CONSERVATIVE_PROMPT_OVERHEAD_TOKENS) * 2,
)
MEMORY_TASK_IDEMPOTENCY_PREFIX = "memory-consolidation:"
MAX_DISPATCH_PROJECTS_PER_TICK = 25
MEMORY_DISPATCH_INTERVAL_SECONDS = 20

logger = logging.getLogger(__name__)


class MemoryRuntimeError(RuntimeError):
    """A runtime operation is malformed, over budget, or no longer fenced."""


class BackfillPreconditionError(MemoryRuntimeError):
    """The operator's observed cursor no longer matches durable progress."""


@dataclass(frozen=True)
class ConsolidationBudget:
    """A fail-closed per-job allowance supplied by the dispatcher/operator."""

    allowed_providers: frozenset[str]
    max_prompt_tokens: int
    max_completion_tokens: int
    max_total_tokens: int
    max_input_bytes: int = MAX_CONSOLIDATION_INPUT_BYTES

    def __post_init__(self) -> None:
        if (
            not self.allowed_providers
            or min(self.max_prompt_tokens, self.max_completion_tokens, self.max_total_tokens, self.max_input_bytes) < 1
            or self.max_total_tokens < self.max_prompt_tokens
        ):
            raise ValueError("consolidation budget is invalid")


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    model: str
    prompt_version: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ConsolidationInput:
    source_id: uuid.UUID
    source_sequence: int
    source_type: str
    content_hash: str
    canonical_payload: bytes


@dataclass(frozen=True)
class ConsolidationClaim:
    job_id: uuid.UUID
    project_id: uuid.UUID
    lease_owner: str
    lease_generation: int
    base_ledger_revision: int
    context_authority_generation: int
    privacy_generation: int
    workspace_requirement: Literal["none"]
    capability: Literal["MEMORY_CONSOLIDATION"]
    sources: tuple[ConsolidationInput, ...]


@dataclass(frozen=True)
class BackfillBatchResult:
    cursor_id: uuid.UUID
    scanned: int
    admitted: int
    skipped: int
    complete: bool
    queued_job_id: uuid.UUID | None


@dataclass(frozen=True)
class MemoryDispatchTickResult:
    """Small, content-free outcome for one bounded pool-dispatch tick."""

    scanned_projects: int
    queued_jobs: int
    created_tasks: int


def _memory_task_idempotency_key(job_id: uuid.UUID) -> str:
    return f"{MEMORY_TASK_IDEMPOTENCY_PREFIX}{job_id}"


async def _ensure_memory_job_task(
    session: AsyncSession,
    *,
    job: StrategicMemoryConsolidationJob,
) -> Task | None:
    """Create at most one active pooled Task for a durable job.

    The job remains the source of truth.  A task is only a dispatch envelope,
    so a failed terminal envelope can be replaced for a still-retryable job,
    while the partial active-key index prevents concurrent scheduler ticks from
    double-dispatching it.
    """

    key = _memory_task_idempotency_key(job.id)
    existing = await session.scalar(
        select(Task)
        .where(
            Task.project_id == job.project_id,
            Task.idempotency_key == key,
            Task.status.in_(
                {
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.EVALUATING,
                    TaskStatus.BLOCKED,
                    TaskStatus.PARKED,
                }
            ),
        )
        .with_for_update()
    )
    if existing is not None:
        return None
    payload = validate_internal_payload(
        {
            "type": "memory_consolidation",
            "job_id": str(job.id),
            "max_prompt_tokens": MEMORY_TASK_MAX_PROMPT_TOKENS,
            "max_completion_tokens": MEMORY_TASK_MAX_COMPLETION_TOKENS,
            "max_total_tokens": MEMORY_TASK_MAX_TOTAL_TOKENS,
            "max_input_bytes": MEMORY_TASK_MAX_INPUT_BYTES,
        },
        "strategic_memory_runtime",
    )
    # ``model_dump`` keeps UUID objects; JSONB task storage requires a JSON
    # scalar. The agent endpoint parses this back to UUID before use.
    payload["job_id"] = str(job.id)
    task = Task(
        project_id=job.project_id,
        title="Consolidate bounded Strategic Memory receipts",
        description="Receipt-bound no-workspace Strategic Memory consolidation.",
        payloads=[payload],
        # The text-only memory worker is presently verified only for the
        # native Anthropic runner.  This is a backend-owned per-task routing
        # allow-list (which outranks mutable runtime routing); it is not a
        # preference that may fall through to another provider when Anthropic
        # capacity is unavailable.  Add providers here only with an equivalent
        # text-only isolation contract and a dedicated qualification.
        context={
            "memory_consolidation_job_id": str(job.id),
            "provider_preference": ["anthropic"],
        },
        idempotency_key=key,
        priority=15,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(task)
    await session.flush()
    return task


async def dispatch_pending_memory_consolidation(
    session: AsyncSession,
    *,
    project_limit: int = MAX_DISPATCH_PROJECTS_PER_TICK,
    batch_size: int = 64,
) -> MemoryDispatchTickResult:
    """Bounded live-receipt -> job -> pooled-Task bridge.

    This is intentionally rollout-gated and only selects projects whose
    durable receipt state has an unincorporated high watermark.  The existing
    contiguous-job helper performs the canonical state -> job coordination;
    this bridge then emits a minimal, idempotent Task envelope.  It never reads
    receipt payload bytes.
    """

    if not 1 <= project_limit <= MAX_DISPATCH_PROJECTS_PER_TICK:
        raise ValueError("memory dispatch project limit is outside the bounded range")
    project_ids = list(
        (
            await session.execute(
                select(StrategicMemoryProjectState.project_id)
                .where(
                    StrategicMemoryProjectState.source_high_watermark
                    > StrategicMemoryProjectState.incorporated_source_sequence
                )
                .order_by(StrategicMemoryProjectState.updated_at)
                .limit(project_limit)
            )
        ).scalars()
    )
    queued_jobs = 0
    created_tasks = 0
    for project_id in project_ids:
        # The default is explicitly off.  A deployed receipt writer can keep
        # recording evidence safely before an operator enables any provider
        # spend for this project.
        from app.strategic_memory_governance import memory_v2_rollout

        if not memory_v2_rollout(project_id).shadow_enabled:
            continue
        job = await enqueue_contiguous_job(
            session,
            project_id,
            batch_size=batch_size,
            max_input_bytes=MEMORY_TASK_MAX_INPUT_BYTES,
            max_prompt_tokens=MEMORY_TASK_MAX_PROMPT_TOKENS,
        )
        if job is None:
            continue
        queued_jobs += 1
        if await _ensure_memory_job_task(session, job=job) is not None:
            created_tasks += 1
    return MemoryDispatchTickResult(
        scanned_projects=len(project_ids),
        queued_jobs=queued_jobs,
        created_tasks=created_tasks,
    )


async def memory_consolidation_dispatch_service(
    session_factory: Any,
    *,
    interval_seconds: int = MEMORY_DISPATCH_INTERVAL_SECONDS,
) -> None:
    """Leader-only bounded dispatcher for newly recorded receipt prefixes."""

    if interval_seconds < 5:
        raise ValueError("memory dispatch interval must be at least five seconds")
    while True:
        try:
            async with session_factory() as session:
                result = await dispatch_pending_memory_consolidation(session)
                await session.commit()
                if result.created_tasks:
                    logger.info(
                        "[STRATEGIC_MEMORY] dispatched %d receipt consolidation task(s) ",
                        result.created_tasks,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[STRATEGIC_MEMORY] bounded dispatch tick failed")
        await asyncio.sleep(interval_seconds)


def _validate_usage(usage: ProviderUsage, budget: ConsolidationBudget) -> None:
    if (
        not usage.provider.strip()
        or not usage.model.strip()
        or not usage.prompt_version.strip()
        or min(usage.prompt_tokens, usage.completion_tokens) < 0
    ):
        raise MemoryRuntimeError("provider usage metadata is malformed")
    if usage.provider not in budget.allowed_providers:
        raise MemoryRuntimeError("provider is not allowed for memory consolidation")
    if (
        usage.prompt_tokens > budget.max_prompt_tokens
        or usage.completion_tokens > budget.max_completion_tokens
        or usage.total_tokens > budget.max_total_tokens
    ):
        raise MemoryRuntimeError("memory consolidation token budget exceeded")


async def backfill_project_message_receipts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    batch_size: int = 100,
    enqueue_batch_size: int = 64,
    enqueue: bool = False,
    expected_cursor_id: uuid.UUID | None = None,
    expected_last_sequence: int | None = None,
) -> BackfillBatchResult:
    """Resume one frozen ProjectMessage receipt backfill page and debounce work.

    The frozen max sequence prevents a moving history from changing a backfill
    run.  New writes continue to be captured by the canonical live writer; the
    cursor merely catches up the pre-existing native messages.
    """

    if not 1 <= batch_size <= MAX_BACKFILL_BATCH_SIZE:
        raise ValueError("backfill batch size is outside the bounded range")
    from app.strategic_memory_control import freeze_backfill_cursor, update_backfill_progress

    # Serialize the first cursor freeze as well as resumed pages.  Without the
    # project-state lock, two operator requests could both observe no cursor
    # and race on its unique (project, producer) identity.
    await lock_source_project_state(session, project_id)

    # Once a producer run exists, it owns its original cutoff. Reading that
    # row before calculating a fresh maximum lets a resumed page coexist with
    # messages written after the run began.
    existing_cursor = await session.scalar(
        select(StrategicMemoryBackfillCursor).where(
            StrategicMemoryBackfillCursor.project_id == project_id,
            StrategicMemoryBackfillCursor.producer == PROJECT_MESSAGE_PRODUCER,
        ).with_for_update()
    )
    if expected_last_sequence is not None:
        durable_last_sequence = int(
            ((existing_cursor.cursor if existing_cursor is not None else {}) or {}).get(
                "last_sequence", 0
            )
            or 0
        )
        if (
            (existing_cursor.id if existing_cursor is not None else None)
            != expected_cursor_id
            or durable_last_sequence != expected_last_sequence
        ):
            raise BackfillPreconditionError("memory backfill cursor advanced; refresh status")
        if existing_cursor is not None and existing_cursor.status == "complete":
            raise BackfillPreconditionError("memory backfill is already complete")
    if existing_cursor is not None:
        cutoff = dict(existing_cursor.cutoff_spec)
    else:
        max_sequence = await session.scalar(
            select(func.max(ProjectMessage.sequence)).where(
                ProjectMessage.project_id == project_id
            )
        )
        cutoff = {"max_sequence": int(max_sequence or 0)}
    cursor = await freeze_backfill_cursor(
        session,
        project_id=project_id,
        producer=PROJECT_MESSAGE_PRODUCER,
        cutoff_spec=cutoff,
    )
    last_sequence = int((cursor.cursor or {}).get("last_sequence", 0) or 0)
    frozen_max = int(cursor.cutoff_spec.get("max_sequence", 0) or 0)
    messages = list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.sequence > last_sequence,
                    ProjectMessage.sequence <= frozen_max,
                )
                .order_by(ProjectMessage.sequence)
                .limit(batch_size)
                .with_for_update()
            )
        ).scalars()
    )
    admitted = 0
    for message in messages:
        # Governed deletion retains a content-free native tombstone. This
        # check occurs after Project -> state -> message locks, matching the
        # deletion path, so backfill cannot recreate an active receipt after
        # a privacy transaction.
        if (message.context or {}).get("privacy_state") == "deleted":
            continue
        payload = message.content.encode("utf-8")
        result = await record_source(
            session,
            SourceReceiptInput(
                project_id=project_id,
                source_type="project_message",
                source_key=str(message.id),
                source_revision=str(message.sequence),
                content_hash=sha256_hex(payload),
                occurred_at=message.created_at,
                native_sequence=message.sequence,
                actor_kind=message.from_type,
                actor_id=getattr(message, "actor_user_id", None),
                resident_session_id=getattr(message, "resident_session_id", None),
                # The canonical ProjectMessage before-flush writer records
                # the UI surface as receipt metadata. Backfill must recreate
                # that immutable native identity exactly so a partial legacy
                # run can safely meet a live-written receipt.
                metadata={
                    "surface": (
                        message.surface.value
                        if hasattr(message.surface, "value")
                        else str(message.surface)
                    )
                },
                canonical_payload=None,
                mutable=False,
            ),
        )
        admitted += int(result.created)
    scanned = len(messages)
    next_sequence = messages[-1].sequence if messages else last_sequence
    complete = next_sequence >= frozen_max
    cursor = await update_backfill_progress(
        session,
        cursor_id=cursor.id,
        cursor_value={"last_sequence": int(next_sequence)},
        scanned_delta=scanned,
        admitted_delta=admitted,
        skipped_delta=scanned - admitted,
        complete=complete,
    )
    queued = (
        await enqueue_contiguous_job(session, project_id, batch_size=enqueue_batch_size)
        if enqueue
        else None
    )
    return BackfillBatchResult(
        cursor_id=cursor.id,
        scanned=scanned,
        admitted=admitted,
        skipped=scanned - admitted,
        complete=cursor.status == "complete",
        queued_job_id=queued.id if queued is not None else None,
    )


async def claim_memory_consolidation(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    budget: ConsolidationBudget,
    expected_project_id: uuid.UUID | None = None,
    lease_for: timedelta | None = None,
) -> ConsolidationClaim:
    """Claim one job and materialize its exact, bounded source-input receipt."""

    # A failed receipt resolution or input-budget check must not strand the
    # job with a live lease. The savepoint rolls the claim back to queued while
    # preserving the caller's outer transaction.
    async with session.begin_nested():
        job = await claim_consolidation_job(
            session,
            job_id=job_id,
            lease_owner=lease_owner,
            **({"lease_for": lease_for} if lease_for is not None else {}),
        )
        # Scope must be checked while the lease row is locked and before any
        # source receipt is resolved.  The savepoint releases the lease if an
        # invalid/misbound task payload names a foreign project's job.
        if expected_project_id is not None and job.project_id != expected_project_id:
            raise SourceJobLeaseError("consolidation job is outside task project")
        from app.strategic_memory_service import _load_claimed_sources, _models

        rows = await _load_claimed_sources(session, _models(), job)
        inputs: list[ConsolidationInput] = []
        total_bytes = 0
        for source, _ in rows:
            resolved = await resolve_source_receipt(
                session, source, expected_project_id=job.project_id
            )
            total_bytes += len(resolved.canonical_payload)
            if total_bytes > budget.max_input_bytes:
                raise MemoryRuntimeError("memory consolidation input byte budget exceeded")
            inputs.append(
                ConsolidationInput(
                    source_id=source.id,
                    source_sequence=int(source.source_sequence),
                    source_type=source.source_type,
                    content_hash=source.content_hash,
                    canonical_payload=resolved.canonical_payload,
                )
            )
        if not inputs:
            raise MemoryRuntimeError("claimed consolidation job has no resolvable sources")
        return ConsolidationClaim(
            job_id=job.id,
            project_id=job.project_id,
            lease_owner=lease_owner,
            lease_generation=int(job.lease_generation),
            base_ledger_revision=int(job.base_ledger_revision),
            context_authority_generation=int(job.context_authority_generation),
            privacy_generation=int(job.privacy_generation),
            workspace_requirement=WORKSPACE_REQUIREMENT_NONE,
            capability=MEMORY_CONSOLIDATION_CAPABILITY,
            sources=tuple(inputs),
        )


async def renew_memory_consolidation(
    session: AsyncSession,
    *,
    claim: ConsolidationClaim,
    lease_for: timedelta | None = None,
) -> ConsolidationClaim:
    """Renew only the exact claim; source bytes are intentionally not re-read."""

    job = await renew_consolidation_job(
        session,
        job_id=claim.job_id,
        lease_owner=claim.lease_owner,
        lease_generation=claim.lease_generation,
        **({"lease_for": lease_for} if lease_for is not None else {}),
    )
    return ConsolidationClaim(
        **{**claim.__dict__, "lease_generation": int(job.lease_generation)}
    )


async def submit_memory_consolidation(
    session: AsyncSession,
    *,
    claim: ConsolidationClaim,
    proposal: LedgerProposal,
    usage: ProviderUsage,
    budget: ConsolidationBudget,
) -> LedgerAdmissionResult:
    """Fence a provider response to its exact claim and submit it atomically."""

    _validate_usage(usage, budget)
    if proposal.job_id != claim.job_id or proposal.base_ledger_revision != claim.base_ledger_revision:
        raise MemoryRuntimeError("proposal does not match the claimed consolidation receipt")
    if len(proposal.canonical_bytes()) > budget.max_input_bytes:
        raise MemoryRuntimeError("proposal byte budget exceeded")
    # Admission owns the canonical Project -> state -> job lock order.  Do
    # not pre-lock the job here or an enqueue/privacy mutation can deadlock
    # against submit.  The metadata write occurs only after that fenced path.
    admitted = await validate_and_admit_proposal(
        session,
        proposal,
        lease_owner=claim.lease_owner,
        lease_generation=claim.lease_generation,
    )
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(
            StrategicMemoryConsolidationJob.id == claim.job_id,
            StrategicMemoryConsolidationJob.project_id == claim.project_id,
        )
        .with_for_update()
    )
    if job is None:
        raise SourceJobLeaseError("consolidation job not found")
    job.model = f"{usage.provider}:{usage.model}"[:255]
    job.prompt_version = usage.prompt_version[:128]
    job.updated_at = _utcnow()
    session.add(job)
    await session.flush()
    return admitted
