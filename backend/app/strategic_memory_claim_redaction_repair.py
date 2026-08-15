"""Bounded repair for the Fast-claim ProjectMessage redaction regression.

One mixed-version release treated every ``ProjectMessage.context`` rewrite as
a privacy mutation.  The foreground claim path only added server-owned lease
metadata, but that broad hook tombstoned the immutable Strategic Memory source
receipt anyway.  This module repairs only an operator-reviewed, digest-fenced
set whose native message still proves the exact immutable receipt bytes and
whose current context has the distinctive Fast-claim signature.

The repair never rewinds the incorporation watermark.  A receipt that had
already reached a real terminal disposition keeps it.  A receipt forced to
``no_memory/source_redacted`` by the bug is restored as ``quarantined`` and is
reconsolidated through the ordinary one-source ledger gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageSurface,
    ProjectMessage,
    StrategicMemoryConsolidationJob,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
    _PROJECT_MESSAGE_PRIVACY_CONTEXT_KEYS,
)
from app.strategic_memory_control import ACTIVE_JOB_STATUSES, lock_source_project_state
from app.strategic_memory_sources import (
    SourceReceiptInput,
    canonical_json_bytes,
    record_source,
    sha256_hex,
)
from app.time_utils import _utcnow


REPAIR_POLICY_VERSION = "fast-claim-context-redaction-v1"
REPAIR_DISPOSITION_REASON = "false_fast_claim_context_redaction_repaired_v1"
REPAIR_AUDIT_REASON = "false_fast_claim_context_redaction_audit_v1"
REPAIR_SOURCE_KEY_PREFIX = "memory-repair:fast-claim-context:"
MAX_REPAIR_CANDIDATES = 250
MAX_REPAIR_WINDOW = timedelta(days=31)

_PUBLIC_REPLY_OWNER_KEY = "_conversation_public_reply_owner"
_CLAIM_IDEMPOTENCY_KEY = "_conversation_claim_idempotency_key"
_DEPLOY_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ClaimRedactionRepairError(RuntimeError):
    """The requested repair is unsafe, stale, or outside its explicit fence."""


@dataclass(frozen=True)
class ClaimRedactionCandidate:
    source: StrategicMemorySource
    disposition: StrategicMemorySourceDisposition
    message: ProjectMessage
    fingerprint: dict[str, Any]


@dataclass(frozen=True)
class ClaimRedactionAudit:
    project_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    candidate_count: int
    candidate_digest: str
    candidates: tuple[ClaimRedactionCandidate, ...]


@dataclass(frozen=True)
class ClaimRedactionRepairResult:
    audit: ClaimRedactionAudit
    operation_id: str
    audit_source_id: uuid.UUID
    restored_count: int
    reconsolidation_pending_count: int
    queued_job_id: uuid.UUID | None
    replayed: bool
    serving_revision: str


def current_serving_revision() -> str:
    """Return the immutable deploy identity used by both local and Cloud Run."""

    return (
        os.getenv("DEPLOY_SHA")
        or os.getenv("K_REVISION")
        or "local-dev"
    ).strip()


def _canonical_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _validated_window(
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime]:
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ClaimRedactionRepairError("repair window timestamps must include a timezone")
    start = _canonical_utc(window_start)
    end = _canonical_utc(window_end)
    if start >= end:
        raise ClaimRedactionRepairError("repair window start must precede its end")
    if end - start > MAX_REPAIR_WINDOW:
        raise ClaimRedactionRepairError("repair window exceeds the 31-day safety bound")
    if end > _utcnow() + timedelta(minutes=5):
        raise ClaimRedactionRepairError("repair window cannot extend into the future")
    return start, end


def _surface_value(value: Any) -> str:
    return value.value if isinstance(value, MessageSurface) else str(value)


def _candidate_fingerprint(
    source: StrategicMemorySource,
    disposition: StrategicMemorySourceDisposition,
    message: ProjectMessage,
) -> dict[str, Any]:
    context = message.context if isinstance(message.context, dict) else {}
    assert source.tombstoned_at is not None
    return {
        "source_id": str(source.id),
        "message_id": str(message.id),
        "source_sequence": int(source.source_sequence),
        "native_sequence": int(source.native_sequence or 0),
        "content_hash": source.content_hash,
        "tombstoned_at": _canonical_utc(source.tombstoned_at).isoformat(),
        "disposition_status": disposition.status,
        "disposition_reason": disposition.reason,
        "conversation_claim_id": str(message.conversation_claim_id),
        "claim_idempotency_key": str(context[_CLAIM_IDEMPOTENCY_KEY]),
    }


def _is_exact_false_claim_redaction(
    source: StrategicMemorySource,
    disposition: StrategicMemorySourceDisposition,
    message: ProjectMessage | None,
    *,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Prove the narrow signature without trusting mutable prose or content."""

    if message is None or source.tombstoned_at is None:
        return False
    tombstoned_at = _canonical_utc(source.tombstoned_at)
    if not window_start <= tombstoned_at < window_end:
        return False
    if source.payload_state != "redacted" or source.source_type != "project_message":
        return False
    if message.project_id != source.project_id or str(message.id) != source.source_key:
        return False
    if message.sequence is None:
        return False
    if source.source_revision != str(message.sequence):
        return False
    if source.native_sequence != message.sequence:
        return False
    if sha256_hex(message.content.encode("utf-8")) != source.content_hash:
        return False
    if source.actor_kind != message.from_type or source.actor_id != message.actor_user_id:
        return False
    if source.resident_session_id != message.resident_session_id:
        return False
    if _canonical_utc(source.occurred_at) != _canonical_utc(message.created_at):
        return False
    if _canonical_utc(source.recorded_at) > tombstoned_at:
        return False
    surface = _surface_value(message.surface)
    if surface != MessageSurface.CONVERSATION.value:
        return False
    if source.source_metadata != {"surface": surface}:
        return False
    if message.from_type not in {"user", "operator"}:
        return False
    context = message.context if isinstance(message.context, dict) else {}
    if context.get(_PUBLIC_REPLY_OWNER_KEY) != "fast":
        return False
    claim_key = context.get(_CLAIM_IDEMPOTENCY_KEY)
    if not isinstance(claim_key, str) or not claim_key.strip():
        return False
    # The repair deliberately rejects even null governed keys.  Without the
    # old JSON value, key presence is not enough to prove that a real audience
    # or transcript mutation did not precede the Fast claim.
    if any(key in context for key in _PROJECT_MESSAGE_PRIVACY_CONTEXT_KEYS):
        return False
    if "privacy_state" in context:
        return False
    if (
        message.conversation_claim_id is None
        or message.conversation_claim_generation is None
        or message.conversation_claim_incarnation is None
        or message.handled_by_task_id is None
        or int(message.conversation_claim_attempt or 0) < 1
    ):
        return False
    # A pending disposition cannot have been terminally redacted by the buggy
    # hook and is therefore not part of this repair class.
    return disposition.status in {"incorporated", "no_memory", "quarantined"}


async def audit_false_fast_claim_redactions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    lock: bool = False,
) -> ClaimRedactionAudit:
    """Return a content-free, deterministic candidate set and review digest."""

    start, end = _validated_window(window_start, window_end)
    statement = (
        select(StrategicMemorySource, StrategicMemorySourceDisposition)
        .join(
            StrategicMemorySourceDisposition,
            StrategicMemorySourceDisposition.source_id == StrategicMemorySource.id,
        )
        .where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.source_type == "project_message",
            StrategicMemorySource.payload_state == "redacted",
            StrategicMemorySource.tombstoned_at >= start,
            StrategicMemorySource.tombstoned_at < end,
        )
        .order_by(
            StrategicMemorySource.source_sequence,
            StrategicMemorySource.id,
        )
        .limit(MAX_REPAIR_CANDIDATES + 1)
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).all()
    if len(rows) > MAX_REPAIR_CANDIDATES:
        raise ClaimRedactionRepairError(
            f"repair candidate scan exceeds the {MAX_REPAIR_CANDIDATES}-receipt bound"
        )

    message_ids: list[uuid.UUID] = []
    keyed_rows: list[
        tuple[StrategicMemorySource, StrategicMemorySourceDisposition, uuid.UUID]
    ] = []
    for source, disposition in rows:
        try:
            message_id = uuid.UUID(source.source_key)
        except (TypeError, ValueError, AttributeError):
            continue
        message_ids.append(message_id)
        keyed_rows.append((source, disposition, message_id))
    messages: dict[uuid.UUID, ProjectMessage] = {}
    if message_ids:
        message_statement = select(ProjectMessage).where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.id.in_(message_ids),
        )
        if lock:
            message_statement = message_statement.with_for_update()
        messages = {
            row.id: row
            for row in (await session.execute(message_statement)).scalars().all()
        }

    candidates: list[ClaimRedactionCandidate] = []
    for source, disposition, message_id in keyed_rows:
        message = messages.get(message_id)
        if not _is_exact_false_claim_redaction(
            source,
            disposition,
            message,
            window_start=start,
            window_end=end,
        ):
            continue
        assert message is not None
        fingerprint = _candidate_fingerprint(source, disposition, message)
        candidates.append(
            ClaimRedactionCandidate(source, disposition, message, fingerprint)
        )
    candidate_bytes = canonical_json_bytes(
        [candidate.fingerprint for candidate in candidates]
    )
    return ClaimRedactionAudit(
        project_id=project_id,
        window_start=start,
        window_end=end,
        candidate_count=len(candidates),
        candidate_digest=hashlib.sha256(candidate_bytes).hexdigest(),
        candidates=tuple(candidates),
    )


def _repair_source_key(operation_id: str) -> str:
    normalized = operation_id.strip()
    if not normalized or len(normalized) > 128:
        raise ClaimRedactionRepairError("repair operation id is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", normalized):
        raise ClaimRedactionRepairError("repair operation id is invalid")
    return f"{REPAIR_SOURCE_KEY_PREFIX}{normalized}"


def _validate_serving_revision(expected_serving_revision: str) -> str:
    expected = expected_serving_revision.strip()
    current = current_serving_revision()
    if not _DEPLOY_REVISION_RE.fullmatch(expected):
        raise ClaimRedactionRepairError("expected serving revision is invalid")
    if current != expected:
        raise ClaimRedactionRepairError(
            f"serving revision changed (expected {expected}, found {current})"
        )
    return current


async def _load_repair_replay(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_key: str,
    expected_payload: dict[str, Any],
) -> tuple[StrategicMemorySource, dict[str, Any]] | None:
    existing = await session.scalar(
        select(StrategicMemorySource)
        .where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.source_type == "operator_action",
            StrategicMemorySource.source_key == source_key,
            StrategicMemorySource.source_revision == "1",
        )
        .with_for_update()
    )
    if existing is None:
        return None
    try:
        stored_payload = json.loads(bytes(existing.canonical_payload or b"{}"))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ClaimRedactionRepairError("repair audit receipt is malformed") from exc
    if not isinstance(stored_payload, dict) or any(
        stored_payload.get(key) != value for key, value in expected_payload.items()
    ):
        raise ClaimRedactionRepairError(
            "repair operation id already belongs to a different action"
        )
    stored_bytes = canonical_json_bytes(stored_payload)
    if (
        existing.payload_state != "active"
        or existing.tombstoned_at is not None
        or existing.canonical_payload is None
        or bytes(existing.canonical_payload) != stored_bytes
        or existing.content_hash != hashlib.sha256(stored_bytes).hexdigest()
    ):
        raise ClaimRedactionRepairError(
            "repair operation id already belongs to a different or unavailable action"
        )
    disposition = await session.get(
        StrategicMemorySourceDisposition,
        existing.id,
        with_for_update=True,
    )
    if (
        disposition is None
        or disposition.status != "no_memory"
        or disposition.reason != REPAIR_AUDIT_REASON
    ):
        raise ClaimRedactionRepairError("repair audit receipt is inconsistent")
    return existing, stored_payload


async def enqueue_next_repaired_claim_source(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> StrategicMemoryConsolidationJob | None:
    """Queue at most one repaired receipt through the normal exact-source gate."""

    state = await lock_source_project_state(session, project_id)
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
        return active
    source = await session.scalar(
        select(StrategicMemorySource)
        .join(
            StrategicMemorySourceDisposition,
            StrategicMemorySourceDisposition.source_id == StrategicMemorySource.id,
        )
        .where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.payload_state == "active",
            StrategicMemorySource.tombstoned_at.is_(None),
            StrategicMemorySourceDisposition.status == "quarantined",
            StrategicMemorySourceDisposition.reason == REPAIR_DISPOSITION_REASON,
            # A dead/reaped reconsolidation never writes this receipt, so the
            # exact repaired source remains retryable. A successful terminal
            # classification writes the completing job id atomically with its
            # disposition and must never be enqueued again, even if a malformed
            # classifier response preserved the repair quarantine reason.
            StrategicMemorySourceDisposition.consolidation_job_id.is_(None),
        )
        .order_by(StrategicMemorySource.source_sequence, StrategicMemorySource.id)
        .limit(1)
        .with_for_update()
    )
    if source is None:
        return None
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
    return job


async def apply_false_fast_claim_redaction_repair(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: str,
    operation_id: str,
    buggy_serving_revision: str,
    expected_serving_revision: str,
    window_start: datetime,
    window_end: datetime,
    expected_candidate_count: int,
    expected_candidate_digest: str,
) -> ClaimRedactionRepairResult:
    """Apply one exact reviewed set and persist an idempotency/audit receipt."""

    if not 0 <= expected_candidate_count <= MAX_REPAIR_CANDIDATES:
        raise ClaimRedactionRepairError("expected candidate count is outside the bound")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_candidate_digest):
        raise ClaimRedactionRepairError("expected candidate digest is invalid")
    if not _DEPLOY_REVISION_RE.fullmatch(buggy_serving_revision.strip()):
        raise ClaimRedactionRepairError("buggy serving revision is invalid")
    serving_revision = _validate_serving_revision(expected_serving_revision)
    start, end = _validated_window(window_start, window_end)
    if buggy_serving_revision.strip() == serving_revision:
        raise ClaimRedactionRepairError(
            "repair must run on a revision different from the buggy revision"
        )
    source_key = _repair_source_key(operation_id)
    request_payload = {
        "schema_version": 1,
        "policy_version": REPAIR_POLICY_VERSION,
        "project_id": str(project_id),
        "operation_id": operation_id.strip(),
        "buggy_serving_revision": buggy_serving_revision.strip(),
        "repair_serving_revision": serving_revision,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "candidate_count": expected_candidate_count,
        "candidate_digest": expected_candidate_digest,
    }

    await lock_source_project_state(session, project_id)
    replay = await _load_repair_replay(
        session,
        project_id=project_id,
        source_key=source_key,
        expected_payload=request_payload,
    )
    if replay is not None:
        replay_source, replay_payload = replay
        replay_audit = ClaimRedactionAudit(
            project_id=project_id,
            window_start=start,
            window_end=end,
            candidate_count=expected_candidate_count,
            candidate_digest=expected_candidate_digest,
            candidates=(),
        )
        return ClaimRedactionRepairResult(
            audit=replay_audit,
            operation_id=operation_id.strip(),
            audit_source_id=replay_source.id,
            restored_count=int(
                replay_payload.get("restored_count", expected_candidate_count)
            ),
            reconsolidation_pending_count=int(
                replay_payload.get("reconsolidation_pending_count", 0)
            ),
            queued_job_id=None,
            replayed=True,
            serving_revision=serving_revision,
        )

    # Applying the repair creates asynchronous, one-source reconsolidation
    # work.  The memory dispatcher is deliberately disabled unless shadow
    # processing is enabled for this project; restoring receipts while that
    # gate is closed would leave a queued job and quarantined sources with no
    # executable path.  Exact replays above remain harmless when the rollout is
    # later disabled because they perform no mutation or dispatch.
    from app.strategic_memory_governance import memory_v2_rollout

    if not memory_v2_rollout(project_id).shadow_enabled:
        raise ClaimRedactionRepairError(
            "Strategic Memory shadow processing must be enabled before applying "
            "the repair"
        )

    audit = await audit_false_fast_claim_redactions(
        session,
        project_id=project_id,
        window_start=start.replace(tzinfo=timezone.utc),
        window_end=end.replace(tzinfo=timezone.utc),
        lock=True,
    )
    if (
        audit.candidate_count != expected_candidate_count
        or audit.candidate_digest != expected_candidate_digest
    ):
        raise ClaimRedactionRepairError(
            "repair candidate set changed; repeat the dry-run review"
        )

    reconsolidation_pending = 0
    now = _utcnow()
    for candidate in audit.candidates:
        source = candidate.source
        disposition = candidate.disposition
        source.payload_state = "active"
        source.tombstoned_at = None
        session.add(source)
        if disposition.status == "no_memory" and disposition.reason == "source_redacted":
            disposition.status = "quarantined"
            disposition.reason = REPAIR_DISPOSITION_REASON
            disposition.classifier_version = REPAIR_POLICY_VERSION
            disposition.extractor_model = None
            disposition.prompt_version = None
            disposition.consolidation_job_id = None
            disposition.processed_at = now
            reconsolidation_pending += 1
            session.add(disposition)
    await session.flush()

    payload = {
        **request_payload,
        "restored_count": audit.candidate_count,
        "reconsolidation_pending_count": reconsolidation_pending,
        "candidate_source_ids": [
            str(candidate.source.id) for candidate in audit.candidates
        ],
    }
    payload_bytes = canonical_json_bytes(payload)
    receipt = await record_source(
        session,
        SourceReceiptInput(
            project_id=project_id,
            source_type="operator_action",
            source_key=source_key,
            source_revision="1",
            content_hash=hashlib.sha256(payload_bytes).hexdigest(),
            occurred_at=now,
            actor_kind="operator",
            actor_id=actor_id,
            visibility="project",
            sensitivity="normal",
            metadata={
                "policy": REPAIR_POLICY_VERSION,
                "candidate_digest": expected_candidate_digest,
            },
            canonical_payload=payload_bytes,
            mutable=True,
        ),
    )
    receipt.disposition.status = "no_memory"
    receipt.disposition.reason = REPAIR_AUDIT_REASON
    receipt.disposition.classifier_version = REPAIR_POLICY_VERSION
    receipt.disposition.processed_at = now
    session.add(receipt.disposition)
    await session.flush()

    queued = await enqueue_next_repaired_claim_source(
        session,
        project_id=project_id,
    )
    return ClaimRedactionRepairResult(
        audit=audit,
        operation_id=operation_id.strip(),
        audit_source_id=receipt.source.id,
        restored_count=audit.candidate_count,
        reconsolidation_pending_count=reconsolidation_pending,
        queued_job_id=(
            queued.id
            if queued is not None and queued.mode == "reconsolidation"
            else None
        ),
        replayed=False,
        serving_revision=serving_revision,
    )
