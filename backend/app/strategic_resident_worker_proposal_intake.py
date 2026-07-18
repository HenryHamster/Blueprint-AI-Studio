"""Storage + reconciliation drain for worker strategic-memory proposals.

The producer (:func:`submit_worker_memory_proposal`) durably records a bounded
worker's proposal as ``pending``. The consumer (:func:`drain_worker_memory_proposals`)
runs INSIDE the Resident reconciliation transaction: it loads pending rows,
admits each through the revision-linked, evidence-gated admission contract, and
marks it ``admitted`` or ``rejected`` — so a worker's proposal only ever changes
strategic memory through Resident reconciliation's checkpoint-CAS apply path, and
one bad proposal is rejected without failing the turn.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StrategicWorkerMemoryProposal
from app.strategic_resident_evidence_validation import TrustedEvidenceIndex
from app.strategic_resident_reconciliation_validation import (
    DecisiveEvidenceRequired,
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import ResidentMemoryProposal
from app.strategic_resident_worker_proposal import (
    WorkerMemoryProposal,
    WorkerProposalEvidenceRef,
    admit_worker_memory_proposals,
)
from app.time_utils import _utcnow


# Bound the number of pending proposals a single reconciliation drains, so a
# flood of worker proposals cannot unbound the turn transaction.
_MAX_DRAIN = 50


async def pending_worker_proposal_cursor(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> dict[str, str]:
    """Return a bounded fingerprint that advances after every partial drain.

    The fingerprint covers the next drain window, rather than the lifetime row
    count.  When a turn resolves the first ``_MAX_DRAIN`` rows from a larger
    backlog, the next window changes and therefore schedules a successor instead
    of treating the still-pending project as already handled.
    """

    pending_ids = list(
        (
            await session.execute(
                select(StrategicWorkerMemoryProposal.id)
                .where(
                    StrategicWorkerMemoryProposal.project_id == project_id,
                    StrategicWorkerMemoryProposal.status == "pending",
                )
                .order_by(
                    StrategicWorkerMemoryProposal.created_at.asc(),
                    StrategicWorkerMemoryProposal.id.asc(),
                )
                .limit(_MAX_DRAIN + 1)
            )
        ).scalars()
    )
    if not pending_ids:
        return {}
    encoded = ":".join(str(item) for item in pending_ids).encode("utf-8")
    return {
        "worker_proposal_pending_window": hashlib.sha256(encoded).hexdigest()
    }


async def submit_worker_memory_proposal(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    proposal: WorkerMemoryProposal,
    source_cycle_id: str | None = None,
) -> StrategicWorkerMemoryProposal:
    """Durably record a worker proposal as ``pending`` for later admission.

    The proposal is validated by ``WorkerMemoryProposal`` before it reaches here
    (revision-link fields, bounded updates/rationale/evidence). Admission — the
    revision-link + evidence-trust decision — happens only at drain time, against
    the checkpoint current then.
    """

    row = StrategicWorkerMemoryProposal(
        project_id=project_id,
        proposal_id=proposal.proposal_id,
        layer=proposal.layer,
        updates=dict(proposal.updates),
        rationale=proposal.rationale,
        evidence=[ref.model_dump() for ref in proposal.evidence],
        target_checkpoint_version=proposal.target_checkpoint_version,
        target_checkpoint_hash=proposal.target_checkpoint_hash,
        status="pending",
        source_cycle_id=source_cycle_id,
    )
    session.add(row)
    await session.flush()
    return row


def _to_domain(row: StrategicWorkerMemoryProposal) -> WorkerMemoryProposal:
    return WorkerMemoryProposal(
        proposal_id=row.proposal_id,
        layer=row.layer,
        updates=dict(row.updates or {}),
        rationale=row.rationale,
        target_checkpoint_version=int(row.target_checkpoint_version),
        target_checkpoint_hash=str(row.target_checkpoint_hash),
        evidence=tuple(
            WorkerProposalEvidenceRef.model_validate(item)
            for item in (row.evidence or [])
        ),
    )


async def drain_worker_memory_proposals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    current_checkpoint_version: int,
    current_checkpoint_hash: str,
    trusted_evidence: TrustedEvidenceIndex,
) -> list[ResidentMemoryProposal]:
    """Admit pending worker proposals for a project; return the admitted ones.

    Runs under the reconciliation transaction's checkpoint lock. Each pending row
    is admitted independently: a revision-stale, evidence-free, untrusted, or
    malformed proposal is marked ``rejected`` (with a reason) and skipped, never
    raising — one bad worker proposal must not fail the Resident turn. Admitted
    rows are marked ``admitted`` and returned in reconciliation-native shape for
    the checkpoint-CAS apply path.
    """

    rows = (
        (
            await session.execute(
                select(StrategicWorkerMemoryProposal)
                .where(
                    StrategicWorkerMemoryProposal.project_id == project_id,
                    StrategicWorkerMemoryProposal.status == "pending",
                )
                .order_by(StrategicWorkerMemoryProposal.created_at.asc())
                .limit(_MAX_DRAIN)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = _utcnow()
    admitted: list[ResidentMemoryProposal] = []
    for row in rows:
        try:
            resident_proposals = admit_worker_memory_proposals(
                [_to_domain(row)],
                current_checkpoint_version=current_checkpoint_version,
                current_checkpoint_hash=current_checkpoint_hash,
                trusted_evidence=trusted_evidence,
            )
        except (
            DecisiveEvidenceRequired,
            ReconciliationResultInvalid,
            ValueError,
        ) as exc:
            row.status = "rejected"
            row.rejection_reason = str(exc)[:500]
            row.resolved_at = now
            session.add(row)
            continue
        row.status = "admitted"
        row.resolved_at = now
        session.add(row)
        admitted.extend(resident_proposals)
    return admitted


__all__ = [
    "drain_worker_memory_proposals",
    "pending_worker_proposal_cursor",
    "submit_worker_memory_proposal",
]
