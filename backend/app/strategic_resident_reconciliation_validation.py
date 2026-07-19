"""Acceptance and decisive-evidence validation for Resident reconciliation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

import uuid

from app.strategic_operation_authority_validation import StrategicAuthorityError
from app.strategic_resident_turn_results import (
    ResidentEvidence,
    ResidentEvidenceInspection,
    ResidentMemoryProposal,
    StrategicResidentTurnResult,
)
from app.strategic_resident_evidence_validation import (
    TrustedEvidenceIndex,
    validate_result_evidence_is_trusted,
)


_HIGH_IMPACT_KINDS = frozenset(
    {"strategy_change", "abandon_work", "merge", "deploy"}
)
_MAX_ACCEPTED_IDS = 50


class ReconcileAcceptance(Protocol):
    accepted_memory_proposal_ids: tuple[str, ...]
    accepted_implication_ids: tuple[str, ...]
    evidence_inspections: tuple[ResidentEvidenceInspection, ...]


class ReconcileIdentity(ReconcileAcceptance, Protocol):
    project_id: uuid.UUID
    turn_task_id: uuid.UUID
    authority_generation: int
    expected_turn_task_version: int
    expected_checkpoint_id: uuid.UUID
    expected_checkpoint_version: int
    expected_checkpoint_hash: str
    context_hash: str
    accepted_open_loop_ids: tuple[str, ...]
    resolved_open_loop_ids: tuple[str, ...]


class DecisiveEvidenceRequired(StrategicAuthorityError):
    code = "decisive_evidence_required"


class ReconciliationResultInvalid(StrategicAuthorityError):
    code = "reconciliation_result_invalid"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a stable JSON projection for hashes and checkpoint bounds."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def bounded_reconciliation_ids(
    values: tuple[str, ...],
    *,
    name: str,
) -> tuple[str, ...]:
    if len(values) > _MAX_ACCEPTED_IDS:
        raise ReconciliationResultInvalid(f"{name} exceeds {_MAX_ACCEPTED_IDS} items")
    cleaned = tuple(str(value).strip() for value in values)
    if any(not value or len(value) > 120 for value in cleaned):
        raise ReconciliationResultInvalid(f"{name} contains an invalid identifier")
    if len(set(cleaned)) != len(cleaned):
        raise ReconciliationResultInvalid(f"{name} contains duplicate identifiers")
    return cleaned


def indexed_unique(items: list[Any], attr: str, *, name: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        identity = str(getattr(item, attr))
        if identity in result:
            raise ReconciliationResultInvalid(f"duplicate {name} id: {identity}")
        result[identity] = item
    return result


def reconciliation_request_hash(
    request: ReconcileIdentity,
    *,
    result_hash: str,
) -> str:
    identity_sets = {
        name: sorted(bounded_reconciliation_ids(values, name=name))
        for name, values in (
            ("accepted_memory_proposal_ids", request.accepted_memory_proposal_ids),
            ("accepted_implication_ids", request.accepted_implication_ids),
            ("accepted_open_loop_ids", request.accepted_open_loop_ids),
            ("resolved_open_loop_ids", request.resolved_open_loop_ids),
        )
    }
    projection = {
        "project_id": str(request.project_id),
        "turn_task_id": str(request.turn_task_id),
        "authority_generation": int(request.authority_generation),
        "expected_turn_task_version": int(request.expected_turn_task_version),
        "expected_checkpoint_id": str(request.expected_checkpoint_id),
        "expected_checkpoint_version": int(request.expected_checkpoint_version),
        "expected_checkpoint_hash": str(request.expected_checkpoint_hash),
        "context_hash": str(request.context_hash),
        "result_hash": result_hash,
        **identity_sets,
        "evidence_inspections": sorted(
            (item.model_dump(mode="json") for item in request.evidence_inspections),
            key=lambda item: str(item.get("evidence_id") or ""),
        ),
    }
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_decisive_metadata(
    evidence: ResidentEvidence,
    *,
    reason: str,
) -> None:
    if not evidence.revision or not evidence.checksum:
        raise DecisiveEvidenceRequired(
            f"{reason} requires revisioned, checksummed decisive evidence",
            evidence_id=evidence.evidence_id,
        )


def _validate_inspection(
    evidence: ResidentEvidence,
    inspection: ResidentEvidenceInspection | None,
    *,
    reason: str,
) -> None:
    _require_decisive_metadata(evidence, reason=reason)
    if (
        inspection is None
        or inspection.decisive is not True
        or inspection.kind != evidence.kind
        or inspection.source_id != evidence.source_id
        or inspection.revision != evidence.revision
        or inspection.checksum != evidence.checksum
    ):
        raise DecisiveEvidenceRequired(
            f"{reason} was not independently inspected by the Resident",
            evidence_id=evidence.evidence_id,
        )


def validate_evidence_acceptance(
    result: StrategicResidentTurnResult,
    request: ReconcileAcceptance,
    *,
    trusted_evidence: TrustedEvidenceIndex,
) -> tuple[list[ResidentMemoryProposal], list[str]]:
    untrusted_evidence_id = validate_result_evidence_is_trusted(
        result,
        trusted_evidence,
    )
    if untrusted_evidence_id is not None:
        raise DecisiveEvidenceRequired(
            "result evidence was not delivered in the frozen input or a "
            "context-bound typed inspection receipt",
            evidence_id=untrusted_evidence_id,
        )
    evidence = indexed_unique(result.evidence, "evidence_id", name="evidence")
    proposals = indexed_unique(
        result.proposed_memory,
        "proposal_id",
        name="memory proposal",
    )
    implications = indexed_unique(
        result.strategic_implications,
        "implication_id",
        name="strategic implication",
    )
    inspections = indexed_unique(
        list(request.evidence_inspections),
        "evidence_id",
        name="evidence inspection",
    )
    for fact in result.facts:
        for evidence_id in fact.evidence_ids:
            if evidence_id not in evidence:
                raise ReconciliationResultInvalid(
                    f"fact {fact.fact_id} references unknown evidence {evidence_id}"
                )
    for implication in result.strategic_implications:
        if implication.kind in _HIGH_IMPACT_KINDS and not implication.evidence_ids:
            raise DecisiveEvidenceRequired(
                f"{implication.kind} implication requires evidence",
                implication_id=implication.implication_id,
            )
        for evidence_id in implication.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                raise ReconciliationResultInvalid(
                    f"implication {implication.implication_id} references "
                    f"unknown evidence {evidence_id}"
                )
            if implication.kind in _HIGH_IMPACT_KINDS:
                _require_decisive_metadata(item, reason=implication.kind)

    accepted_proposals: list[ResidentMemoryProposal] = []
    for proposal_id in bounded_reconciliation_ids(
        request.accepted_memory_proposal_ids,
        name="accepted_memory_proposal_ids",
    ):
        proposal = proposals.get(proposal_id)
        if proposal is None:
            raise ReconciliationResultInvalid(
                f"accepted memory proposal is absent: {proposal_id}"
            )
        if not proposal.evidence_ids:
            raise DecisiveEvidenceRequired(
                "accepted strategic memory requires evidence",
                proposal_id=proposal_id,
            )
        for evidence_id in proposal.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                raise ReconciliationResultInvalid(
                    f"proposal {proposal_id} references unknown evidence {evidence_id}"
                )
            _validate_inspection(
                item,
                inspections.get(evidence_id),
                reason=f"accepted {proposal.layer} update",
            )
        accepted_proposals.append(proposal)

    accepted_implication_ids = bounded_reconciliation_ids(
        request.accepted_implication_ids,
        name="accepted_implication_ids",
    )
    for implication_id in accepted_implication_ids:
        implication = implications.get(implication_id)
        if implication is None:
            raise ReconciliationResultInvalid(
                f"accepted implication is absent: {implication_id}"
            )
        if implication.kind in _HIGH_IMPACT_KINDS:
            for evidence_id in implication.evidence_ids:
                _validate_inspection(
                    evidence[evidence_id],
                    inspections.get(evidence_id),
                    reason=f"accepted {implication.kind} implication",
                )
    return accepted_proposals, list(accepted_implication_ids)


__all__ = [
    "DecisiveEvidenceRequired",
    "ReconciliationResultInvalid",
    "bounded_reconciliation_ids",
    "indexed_unique",
    "reconciliation_request_hash",
    "validate_evidence_acceptance",
]
