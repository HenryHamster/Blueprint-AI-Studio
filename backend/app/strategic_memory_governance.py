"""Governed operator access to Strategic Memory V2.

This module is deliberately a thin policy boundary around the immutable ledger:
it never asks a provider to decide a mutation.  An operator action becomes a
project-scoped, hash-addressed ``operator_action`` receipt and is admitted by
the same fenced ledger path used by consolidation workers.  Privacy deletion
is stricter still: it fences first, tombstones the native receipt, removes all
derived caches, and only then deletes the native message in the same
transaction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ProjectMessage,
    ProjectMessageAttachmentUpload,
    StrategicMemoryConsolidationJob,
    StrategicMemoryItem,
    StrategicMemoryItemSource,
    StrategicMemoryItemVersion,
    StrategicMemoryProjection,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
    _purge_derived_strategic_context_for_privacy_mutation,
    _tombstone_project_message_source_receipts_for_privacy_mutation,
)
from app.strategic_memory_control import claim_consolidation_job
from app.strategic_memory_service import (
    LedgerProposal,
    SourceDispositionMutation,
    TransitionMutation,
    validate_and_admit_proposal,
    as_of_memory_items,
)
from app.strategic_memory_sources import (
    SourceReceiptInput,
    canonical_json_bytes,
    lock_or_create_source_project_state,
    record_source,
)
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)
MemoryAction = Literal["challenge", "supersede", "retract", "forget"]
_ACTIONS = frozenset({"challenge", "supersede", "retract", "forget"})


class MemoryGovernanceError(RuntimeError):
    """A governed read or mutation cannot be safely performed."""


class MemoryObjectNotFound(MemoryGovernanceError):
    """Deliberately uniform not-found result for project-scoped opaque IDs."""


@dataclass(frozen=True)
class MemoryV2Rollout:
    read_enabled: bool
    shadow_enabled: bool
    actions_enabled: bool
    backfill_enabled: bool


@dataclass(frozen=True)
class GovernedActionResult:
    operation_id: str
    ledger_revision: int
    source_id: uuid.UUID
    job_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True)
class PrivacyDeletionResult:
    message_id: uuid.UUID
    privacy_generation: int
    tombstoned_source_count: int
    suppressed_last_provenance_versions: int


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def memory_v2_rollout(project_id: uuid.UUID | None = None) -> MemoryV2Rollout:
    """Read rollout policy without tying this boundary to any provider."""

    selected = {
        value.strip()
        for value in os.getenv("STRATEGIC_MEMORY_V2_PROJECT_IDS", "").split(",")
        if value.strip()
    }
    # An allow-list is optional, but when supplied it is an additional
    # fail-closed fence for every V2 surface (including envelope assembly).
    selected_here = not selected or (project_id is not None and str(project_id) in selected)
    return MemoryV2Rollout(
        # Retrieval is ON by default. It was previously fail-closed pending a
        # per-deployment rollout decision, but that decision was never taken in
        # any environment: the flag is read here and set in no config file, so
        # 686 accumulated knowledge documents were being written and never read.
        # A capability that ships dark is a capability that does not exist.
        #
        # Defaulting open is safe by construction rather than by optimism: the
        # sole consumer (``strategic_memory_envelope``) wraps the retrieval call
        # in a catch-all that logs and degrades back to the established V1
        # envelope core, because "retrieval has no authority to block a live
        # conversation". The kill switch survives -- an operator can still set
        # STRATEGIC_MEMORY_V2_READ_ENABLED=0 to force it closed, or scope it with
        # STRATEGIC_MEMORY_V2_PROJECT_IDS.
        read_enabled=selected_here and _env_bool("STRATEGIC_MEMORY_V2_READ_ENABLED", True),
        shadow_enabled=selected_here and _env_bool("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", False),
        actions_enabled=selected_here and _env_bool("STRATEGIC_MEMORY_V2_ACTIONS_ENABLED", False),
        # Historical ingestion is never a global switch: an explicit project
        # allow-list is mandatory in addition to the default-off feature flag.
        backfill_enabled=bool(selected)
        and selected_here
        and _env_bool("STRATEGIC_MEMORY_V2_BACKFILL_ENABLED", False),
    )


def record_memory_v2_metric(event: str, *, project_id: uuid.UUID, started_at: float, **fields: Any) -> None:
    """Emit provider-neutral, content-free observability for the rollout."""

    logger.info(
        "strategic_memory_v2_metric",
        extra={
            "event": event,
            "project_id": str(project_id),
            "latency_ms": round((time.monotonic() - started_at) * 1000, 3),
            **fields,
        },
    )


def _item_summary(item: StrategicMemoryItem, version: StrategicMemoryItemVersion) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "kind": item.kind,
        "logical_key": item.logical_key,
        "current_version": int(item.current_version),
        "current_status": item.current_status,
        "canonical_text": version.canonical_text,
        "epistemic_status": version.epistemic_status,
        "confidence": float(version.confidence),
        "recorded_source_sequence": int(version.recorded_source_sequence),
    }


async def list_project_memory(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """Return current item heads only; callers must authorize the project first."""

    state = await lock_or_create_source_project_state(session, project_id)
    visible = {
        (row["item_id"], int(row["version"]))
        for row in await as_of_memory_items(
            session, project_id=project_id, source_sequence=int(state.source_high_watermark)
        )
    }
    rows = (
        await session.execute(
            select(StrategicMemoryItem, StrategicMemoryItemVersion)
            .join(
                StrategicMemoryItemVersion,
                (StrategicMemoryItemVersion.project_id == StrategicMemoryItem.project_id)
                & (StrategicMemoryItemVersion.item_id == StrategicMemoryItem.id)
                & (StrategicMemoryItemVersion.version == StrategicMemoryItem.current_version),
            )
            .where(StrategicMemoryItem.project_id == project_id)
            .order_by(StrategicMemoryItem.logical_key, StrategicMemoryItem.id)
        )
    ).all()
    # Filter before pagination: an unavailable head must not create a content
    # leak or make subsequent safe heads unreachable.
    safe_rows = [(item, version) for item, version in rows if (item.id, int(version.version)) in visible]
    return [_item_summary(item, version) for item, version in safe_rows[offset : offset + limit]]


async def get_project_memory_detail(
    session: AsyncSession, *, project_id: uuid.UUID, item_id: uuid.UUID
) -> dict[str, Any]:
    """Return a project-scoped item and its versions/provenance receipt IDs."""

    item = await session.scalar(
        select(StrategicMemoryItem)
        .where(StrategicMemoryItem.project_id == project_id, StrategicMemoryItem.id == item_id)
    )
    if item is None:
        raise MemoryObjectNotFound("Memory item not found")
    state = await lock_or_create_source_project_state(session, project_id)
    visible = {
        (row["item_id"], int(row["version"]))
        for row in await as_of_memory_items(
            session, project_id=project_id, source_sequence=int(state.source_high_watermark)
        )
    }
    if (item.id, int(item.current_version)) not in visible:
        # The same opaque not-found result covers a deleted-only head,
        # unavailable evidence, correction barriers, and a guessed ID.
        raise MemoryObjectNotFound("Memory item not found")
    versions = (
        await session.execute(
            select(StrategicMemoryItemVersion)
            .where(
                StrategicMemoryItemVersion.project_id == project_id,
                StrategicMemoryItemVersion.item_id == item_id,
            )
            .order_by(StrategicMemoryItemVersion.version)
        )
    ).scalars().all()
    provenance = (
        await session.execute(
            select(StrategicMemoryItemSource)
            .where(
                StrategicMemoryItemSource.project_id == project_id,
                StrategicMemoryItemSource.item_id == item_id,
            )
            .order_by(StrategicMemoryItemSource.item_version, StrategicMemoryItemSource.span_index)
        )
    ).scalars().all()
    return {
        "item": {
            "id": str(item.id),
            "kind": item.kind,
            "logical_key": item.logical_key,
            "current_version": int(item.current_version),
            "current_status": item.current_status,
        },
        "versions": [
            {
                "version": int(version.version),
                "canonical_text": version.canonical_text if (item.id, int(version.version)) in visible else None,
                "structured_value": version.structured_value if (item.id, int(version.version)) in visible else {},
                "epistemic_status": version.epistemic_status if (item.id, int(version.version)) in visible else "redacted",
                "confidence": float(version.confidence) if (item.id, int(version.version)) in visible else None,
                "recorded_source_sequence": int(version.recorded_source_sequence),
                "exact_terms": list(version.exact_terms) if (item.id, int(version.version)) in visible else [],
                "redacted": (item.id, int(version.version)) not in visible,
            }
            for version in versions
        ],
        "provenance": [
            {
                "version": int(row.item_version),
                "source_id": str(row.source_id),
                "source_role": row.source_role,
                "span_kind": row.span_kind,
                "span_start": row.span_start,
                "span_end": row.span_end,
                "span_hash": row.span_hash,
            }
            for row in provenance
            if (item.id, int(row.item_version)) in visible
        ],
    }


async def get_project_memory_fetch_trace(
    session: AsyncSession, *, project_id: uuid.UUID, snapshot_id: uuid.UUID
) -> dict[str, Any]:
    """Expose only a trace, never stored envelope bytes or provider payloads."""

    from app.models import StrategicContextEnvelopeSnapshot

    snapshot = await session.scalar(
        select(StrategicContextEnvelopeSnapshot).where(
            StrategicContextEnvelopeSnapshot.project_id == project_id,
            StrategicContextEnvelopeSnapshot.snapshot_id == snapshot_id,
        )
    )
    if snapshot is None:
        raise MemoryObjectNotFound("Memory fetch trace not found")
    return {
        "snapshot_id": str(snapshot.snapshot_id),
        "ledger_revision": int(snapshot.ledger_revision),
        "context_authority_generation": int(snapshot.context_authority_generation),
        "privacy_generation": int(snapshot.privacy_generation),
        "transcript_sequence": int(snapshot.transcript_sequence),
        "retrieval_trace": dict(snapshot.retrieval_trace or {}),
        "created_at": snapshot.created_at,
        "expires_at": snapshot.expires_at,
    }


async def _current_item_for_action(
    session: AsyncSession, *, project_id: uuid.UUID, item_id: uuid.UUID, expected_version: int
) -> StrategicMemoryItem:
    item = await session.scalar(
        select(StrategicMemoryItem)
        .where(StrategicMemoryItem.project_id == project_id, StrategicMemoryItem.id == item_id)
        .with_for_update()
    )
    if item is None:
        raise MemoryObjectNotFound("Memory item not found")
    if int(item.current_version) != expected_version:
        raise MemoryGovernanceError("Memory item revision changed")
    return item


async def apply_governed_memory_action(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: str,
    operation_id: str,
    action: MemoryAction,
    item_id: uuid.UUID,
    expected_version: int,
    reason: str,
    expected_ledger_revision: int,
    target_item_id: uuid.UUID | None = None,
    target_version: int | None = None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> GovernedActionResult:
    """Append one operator-authorized transition through the normal ledger gate."""

    if action not in _ACTIONS or not operation_id.strip() or not reason.strip():
        raise MemoryGovernanceError("Invalid governed memory action")
    if action == "supersede" and (target_item_id is None or target_version is None):
        raise MemoryGovernanceError("Supersede requires a target item and version")
    if action != "supersede" and (target_item_id is not None or target_version is not None):
        raise MemoryGovernanceError("Only supersede accepts a target")

    # This shared allocator takes Project -> state, the global lock order.
    state = await lock_or_create_source_project_state(session, project_id)
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "action": action,
        "item_id": str(item_id),
        "expected_version": expected_version,
        "target_item_id": str(target_item_id) if target_item_id else None,
        "target_version": target_version,
        "reason": reason,
        "actor_id": actor_id,
    }
    canonical_payload = canonical_json_bytes(payload)
    content_hash = hashlib.sha256(canonical_payload).hexdigest()
    existing_source = await session.scalar(
        select(StrategicMemorySource).where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.source_type == "operator_action",
            StrategicMemorySource.source_key == f"memory-action:{operation_id}",
            StrategicMemorySource.source_revision == "1",
        )
    )
    if existing_source is not None:
        if existing_source.content_hash != content_hash:
            raise MemoryGovernanceError("Operation id already belongs to a different action")
        existing_job = await session.scalar(
            select(StrategicMemoryConsolidationJob)
            .where(
                StrategicMemoryConsolidationJob.project_id == project_id,
                StrategicMemoryConsolidationJob.target_source_id == existing_source.id,
            )
            .order_by(StrategicMemoryConsolidationJob.created_at.desc())
        )
        if existing_job is not None and existing_job.status == "complete":
            return GovernedActionResult(
                operation_id,
                int(existing_job.resulting_ledger_revision),
                existing_source.id,
                existing_job.id,
                True,
            )
        raise MemoryGovernanceError("Memory operation is already in progress")

    await _current_item_for_action(
        session, project_id=project_id, item_id=item_id, expected_version=expected_version
    )
    if target_item_id is not None:
        await _current_item_for_action(
            session,
            project_id=project_id,
            item_id=target_item_id,
            expected_version=int(target_version),
        )
    receipt = await record_source(
        session,
        SourceReceiptInput(
            project_id=project_id,
            source_type="operator_action",
            source_key=f"memory-action:{operation_id}",
            source_revision="1",
            content_hash=content_hash,
            occurred_at=_utcnow(),
            actor_kind="operator",
            actor_id=actor_id,
            visibility="project",
            sensitivity="normal",
            metadata={"policy": "operator_governed_v1", "provider": None},
            canonical_payload=canonical_payload,
            mutable=True,
        ),
    )
    source = receipt.source
    if int(state.ledger_revision) != expected_ledger_revision:
        # The just-recorded receipt is part of this transaction and therefore
        # rolls back with this conflict.  Checking after replay detection keeps
        # a successful operation idempotent even if later ledger work advanced
        # the project's revision.
        raise MemoryGovernanceError("Memory ledger revision changed")

    # A reconsolidation job gives this human action the same lease and exact
    # proposal-replay guarantees as a worker action.  It is deliberately a
    # one-source job, so unrelated pending receipts can never be consumed.
    disposition = receipt.disposition
    disposition.status = "quarantined"
    disposition.reason = "operator_governed_action"
    disposition.processed_at = _utcnow()
    session.add(disposition)
    await session.flush()
    job = StrategicMemoryConsolidationJob(
        project_id=project_id,
        mode="reconsolidation",
        target_source_id=source.id,
        base_ledger_revision=int(state.ledger_revision),
        context_authority_generation=int(state.context_authority_generation),
        privacy_generation=int(state.privacy_generation),
        status="queued",
    )
    session.add(job)
    await session.flush()
    owner = f"operator:{actor_id}"[:255]
    job = await claim_consolidation_job(session, job_id=job.id, lease_owner=owner)
    proposal = LedgerProposal(
        schema_version=1,
        job_id=job.id,
        base_ledger_revision=int(state.ledger_revision),
        idempotency_key=f"operator-action:{operation_id}",
        dispositions=(
            SourceDispositionMutation(
                source_id=source.id,
                source_hash=source.content_hash,
                status="incorporated",
                reason="operator_governed_action",
                classifier_version="operator-policy-v1",
            ),
        ),
        transitions=(
            TransitionMutation(
                item_id=item_id,
                item_version=expected_version,
                transition=action,
                source_id=source.id,
                target_item_id=target_item_id,
                target_version=target_version,
                effective_from=effective_from,
                effective_until=effective_until,
                reason=reason,
            ),
        ),
    )
    admitted = await validate_and_admit_proposal(
        session, proposal, lease_owner=owner, lease_generation=int(job.lease_generation)
    )
    return GovernedActionResult(operation_id, admitted.ledger_revision, source.id, admitted.job_id, admitted.replayed)


async def delete_native_project_message_governed(
    session: AsyncSession, *, project_id: uuid.UUID, message_id: uuid.UUID
) -> PrivacyDeletionResult:
    """Delete a canonical message only after privacy fencing and receipt tombstone."""

    started = time.monotonic()
    # The source allocator obtains Project -> state, before the message/source
    # work below.  Do not use ORM deletion: its generic hook is intentionally
    # conservative, while this path must fence *before* native deletion.
    state = await lock_or_create_source_project_state(session, project_id)
    message = await session.scalar(
        select(ProjectMessage)
        .where(ProjectMessage.project_id == project_id, ProjectMessage.id == message_id)
        .with_for_update()
    )
    if message is None or (message.context or {}).get("privacy_state") == "deleted":
        raise MemoryObjectNotFound("Project message not found")
    # Upload receipts point at externally stored bytes.  This boundary has no
    # storage-erasure primitive, so it must fail closed rather than tombstone a
    # message and falsely imply that a linked attachment was erased.
    attachment_exists = await session.scalar(
        select(ProjectMessageAttachmentUpload.attachment_id)
        .where(
            ProjectMessageAttachmentUpload.project_id == project_id,
            ProjectMessageAttachmentUpload.message_id == message_id,
        )
        .limit(1)
    )
    if attachment_exists is not None:
        raise MemoryGovernanceError(
            "Project message has attachments; erase attachments through their storage lifecycle before deletion"
        )
    source_rows = (
        await session.execute(
            select(StrategicMemorySource.id).where(
                StrategicMemorySource.project_id == project_id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(message_id),
                StrategicMemorySource.payload_state == "active",
            )
        )
    ).scalars().all()
    source_ids = set(source_rows)
    affected_versions = set()
    if source_ids:
        affected_versions = set(
            (
                await session.execute(
                    select(StrategicMemoryItemSource.item_id, StrategicMemoryItemSource.item_version).where(
                        StrategicMemoryItemSource.project_id == project_id,
                        StrategicMemoryItemSource.source_id.in_(source_ids),
                    )
                )
            ).all()
        )

    state.privacy_generation = int(state.privacy_generation) + 1
    state.updated_at = _utcnow()
    session.add(state)
    await session.flush()
    await session.run_sync(_purge_derived_strategic_context_for_privacy_mutation, project_id)
    await session.run_sync(
        _tombstone_project_message_source_receipts_for_privacy_mutation,
        project_id=project_id,
        message_id=message_id,
        payload_state="deleted",
    )
    remaining_active = await session.scalar(
        select(StrategicMemorySource.id)
        .where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.source_type == "project_message",
            StrategicMemorySource.source_key == str(message_id),
            StrategicMemorySource.payload_state == "active",
            StrategicMemorySource.tombstoned_at.is_(None),
        )
        .limit(1)
    )
    if remaining_active is not None:  # pragma: no cover - transactional invariant
        raise MemoryGovernanceError("Project-message receipt tombstone did not complete")
    # Projection is derived content; it must not survive until a later rebuild.
    await session.execute(delete(StrategicMemoryProjection).where(StrategicMemoryProjection.project_id == project_id))
    # Retain the native row as an irreversible audit tombstone.  This preserves
    # identity/timing/actor audit fields without leaving recoverable bytes in
    # either the row or its context JSON.
    message.content = ""
    message.context = {"privacy_state": "deleted", "memory_tombstone": True}
    session.expunge(message)
    await session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.project_id == project_id, ProjectMessage.id == message_id)
        .values(content="", context={"privacy_state": "deleted", "memory_tombstone": True})
    )
    await session.flush()

    # Verify the privacy invariant explicitly: a version whose only provenance
    # was this receipt cannot retain an active source.  Retrieval independently
    # enforces the same rule; this check protects future query refactors.
    last_provenance_heads: list[tuple[uuid.UUID, int]] = []
    leak_count = 0
    for item_id, version in affected_versions:
        active_other = await session.scalar(
            select(StrategicMemoryItemSource.source_id)
            .join(StrategicMemorySource, StrategicMemorySource.id == StrategicMemoryItemSource.source_id)
            .where(
                StrategicMemoryItemSource.project_id == project_id,
                StrategicMemoryItemSource.item_id == item_id,
                StrategicMemoryItemSource.item_version == version,
                StrategicMemorySource.payload_state == "active",
                StrategicMemorySource.tombstoned_at.is_(None),
            )
            .limit(1)
        )
        if active_other is None:
            leak_count += 1
            is_current = await session.scalar(
                select(StrategicMemoryItem.id).where(
                    StrategicMemoryItem.project_id == project_id,
                    StrategicMemoryItem.id == item_id,
                    StrategicMemoryItem.current_version == version,
                )
            )
            if is_current is not None:
                last_provenance_heads.append((item_id, version))

    if last_provenance_heads:
        # Source tombstoning makes retrieval safe immediately.  We also append
        # explicit forget transitions so the immutable ledger records why each
        # affected current head disappeared, rather than relying on a mutable
        # cache rebuild to infer privacy history.
        action_payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "action": "privacy_forget",
                "message_id": str(message_id),
                "privacy_generation": int(state.privacy_generation),
                "heads": [
                    {"item_id": str(item_id), "version": version}
                    for item_id, version in sorted(last_provenance_heads, key=lambda pair: (str(pair[0]), pair[1]))
                ],
            }
        )
        receipt = await record_source(
            session,
            SourceReceiptInput(
                project_id=project_id,
                source_type="operator_action",
                source_key=f"privacy-delete:{message_id}",
                source_revision="1",
                content_hash=hashlib.sha256(action_payload).hexdigest(),
                occurred_at=_utcnow(),
                actor_kind="privacy_governance",
                actor_id="system",
                visibility="project",
                sensitivity="restricted",
                metadata={"policy": "privacy_delete_v1", "message_id": str(message_id)},
                canonical_payload=action_payload,
                mutable=True,
            ),
        )
        disposition = receipt.disposition
        disposition.status = "quarantined"
        disposition.reason = "privacy_governed_forget"
        disposition.processed_at = _utcnow()
        session.add(disposition)
        await session.flush()
        job = StrategicMemoryConsolidationJob(
            project_id=project_id,
            mode="reconsolidation",
            target_source_id=receipt.source.id,
            base_ledger_revision=int(state.ledger_revision),
            context_authority_generation=int(state.context_authority_generation),
            privacy_generation=int(state.privacy_generation),
            status="queued",
        )
        session.add(job)
        await session.flush()
        owner = f"privacy:{message_id}"[:255]
        job = await claim_consolidation_job(session, job_id=job.id, lease_owner=owner)
        proposal = LedgerProposal(
            schema_version=1,
            job_id=job.id,
            base_ledger_revision=int(state.ledger_revision),
            idempotency_key=f"privacy-delete:{message_id}",
            dispositions=(
                SourceDispositionMutation(
                    source_id=receipt.source.id,
                    source_hash=receipt.source.content_hash,
                    status="incorporated",
                    reason="privacy_governed_forget",
                    classifier_version="privacy-policy-v1",
                ),
            ),
            transitions=tuple(
                TransitionMutation(
                    item_id=item_id,
                    item_version=version,
                    transition="forget",
                    source_id=receipt.source.id,
                    reason="native project message deleted",
                )
                for item_id, version in last_provenance_heads
            ),
        )
        await validate_and_admit_proposal(
            session, proposal, lease_owner=owner, lease_generation=int(job.lease_generation)
        )
    record_memory_v2_metric(
        "privacy_delete", project_id=project_id, started_at=started,
        tombstoned_source_count=len(source_ids), suppressed_last_provenance_versions=leak_count,
    )
    return PrivacyDeletionResult(message_id, int(state.privacy_generation), len(source_ids), leak_count)
