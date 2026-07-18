"""Admission contract for worker-originated strategic-memory proposals.

The Resident's own turn agent proposes memory inside its structured turn result.
A *bounded worker* (e.g. a completed StrategicCycle) has no such channel; the M4
rule is that any worker memory proposal is admitted **only through Resident
reconciliation**, revision-linked to the checkpoint it was computed against and
grounded in trusted evidence.

This module is that admission gate: a pure, fail-closed function that turns
untrusted ``WorkerMemoryProposal`` records into the reconciliation-native
``ResidentMemoryProposal`` shape (consumed by the existing checkpoint-CAS
``_apply_memory_proposals`` path) — or rejects them. It performs no I/O so it is
exhaustively testable in isolation; the storage/drain wiring that feeds it real
worker proposals is a separate slice.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.strategic_resident_evidence_validation import TrustedEvidenceIndex
from app.strategic_resident_reconciliation_validation import (
    DecisiveEvidenceRequired,
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import (
    Identifier,
    MemoryLayer,
    ResidentMemoryProposal,
)


class WorkerProposalEvidenceRef(BaseModel):
    """One trusted-evidence reference a worker proposal must cite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: Identifier = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=1, max_length=256)


class WorkerMemoryProposal(BaseModel):
    """A bounded worker's proposed strategic-memory change, revision-linked.

    ``target_checkpoint_version``/``target_checkpoint_hash`` bind the proposal to
    the exact checkpoint state the worker computed against; reconciliation admits
    it only while that link is still current, so a proposal computed against a
    superseded strategy is rejected rather than silently applied on top of newer
    memory.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: Identifier = Field(min_length=1, max_length=120)
    layer: MemoryLayer
    updates: dict = Field()
    rationale: str = Field(min_length=1, max_length=4_000)
    target_checkpoint_version: int = Field(ge=1)
    target_checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[WorkerProposalEvidenceRef, ...] = Field(max_length=20)


class WorkerProposalRejected(ReconciliationResultInvalid):
    code = "worker_proposal_rejected"


def admit_worker_memory_proposals(
    proposals: Iterable[WorkerMemoryProposal],
    *,
    current_checkpoint_version: int,
    current_checkpoint_hash: str,
    trusted_evidence: TrustedEvidenceIndex,
) -> list[ResidentMemoryProposal]:
    """Return the admitted proposals as ResidentMemoryProposals, or raise.

    Fail-closed: a proposal is rejected (nothing admitted) if its revision-link
    is stale, if it cites no evidence, or if any cited evidence is not in the
    turn's trusted-evidence index. Duplicate proposal ids are rejected. The
    returned proposals reuse ResidentMemoryProposal's own validators (non-empty
    updates, no open_loops), so malformed updates never reach the apply path.
    """

    admitted: list[ResidentMemoryProposal] = []
    seen_ids: set[str] = set()
    for proposal in proposals:
        if proposal.proposal_id in seen_ids:
            raise WorkerProposalRejected(
                f"duplicate worker proposal id: {proposal.proposal_id}"
            )
        seen_ids.add(proposal.proposal_id)

        if (
            proposal.target_checkpoint_version != int(current_checkpoint_version)
            or proposal.target_checkpoint_hash != str(current_checkpoint_hash)
        ):
            raise WorkerProposalRejected(
                "worker proposal revision-link is stale; recompute against the "
                f"current checkpoint (proposal {proposal.proposal_id})",
            )

        if not proposal.evidence:
            raise DecisiveEvidenceRequired(
                "worker memory proposal must cite decisive evidence",
                proposal_id=proposal.proposal_id,
            )
        for ref in proposal.evidence:
            key = (ref.kind, ref.source_id, ref.revision, ref.checksum)
            if key not in trusted_evidence.entries:
                raise DecisiveEvidenceRequired(
                    "worker proposal cites evidence absent from the trusted index",
                    proposal_id=proposal.proposal_id,
                    evidence_id=ref.evidence_id,
                )

        admitted.append(
            ResidentMemoryProposal(
                proposal_id=proposal.proposal_id,
                layer=proposal.layer,
                updates=proposal.updates,
                rationale=proposal.rationale,
                evidence_ids=[ref.evidence_id for ref in proposal.evidence],
            )
        )
    return admitted


__all__ = [
    "WorkerMemoryProposal",
    "WorkerProposalEvidenceRef",
    "WorkerProposalRejected",
    "admit_worker_memory_proposals",
]
