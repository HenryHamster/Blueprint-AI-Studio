"""Trusted decisive-evidence indexing for Resident reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.models import StrategicOperationReceipt
from app.strategic_operation_authority_validation import StrategicAuthorityError
from app.strategic_resident_evidence_policy import (
    EvidenceKey,
    INSPECTION_CAPABILITY_KINDS,
    MAX_TRUSTED_EVIDENCE,
    SUCCESSFUL_INSPECTION_STATUSES,
    evidence_key,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult

@dataclass(frozen=True)
class TrustedEvidenceIndex:
    context_hash: str
    entries: frozenset[EvidenceKey]


class TrustedEvidenceError(StrategicAuthorityError):
    code = "reconciliation_result_invalid"


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_trusted_evidence_index(
    provider_input: Mapping[str, Any],
    receipts: Iterable[StrategicOperationReceipt],
    *,
    expected_context_hash: str,
) -> TrustedEvidenceIndex:
    """Index exact revisioned evidence delivered under this context lineage."""

    lineage = provider_input.get("lineage")
    evidence = provider_input.get("evidence")
    if not isinstance(lineage, Mapping) or not isinstance(evidence, list):
        raise TrustedEvidenceError("Resident provider evidence is malformed")
    context_hash = str(lineage.get("context_hash") or "")
    if context_hash != str(expected_context_hash):
        raise TrustedEvidenceError("trusted evidence context lineage changed")
    if str(lineage.get("evidence_hash") or "") != _canonical_hash(evidence):
        raise TrustedEvidenceError("Resident provider evidence hash is invalid")

    entries: set[EvidenceKey] = set()
    for item in evidence:
        key = (
            evidence_key(item, allow_source_aliases=True)
            if isinstance(item, Mapping)
            else None
        )
        if key is not None:
            entries.add(key)
    for receipt in receipts:
        if len(entries) >= MAX_TRUSTED_EVIDENCE:
            break
        expected = receipt.expected_revisions or {}
        body = receipt.receipt or {}
        required_operation_kind = INSPECTION_CAPABILITY_KINDS.get(
            str(receipt.capability)
        )
        if (
            required_operation_kind is None
            or str(receipt.operation_kind) != required_operation_kind
            or str(receipt.status) not in SUCCESSFUL_INSPECTION_STATUSES
            or not isinstance(expected, Mapping)
            or str(expected.get("context_hash") or "") != context_hash
            or not isinstance(body, Mapping)
            or body.get("schema_version") != 1
        ):
            continue
        structured_result = body.get("structured_result")
        if not isinstance(structured_result, Mapping):
            continue
        receipt_evidence = structured_result.get("evidence")
        if not isinstance(receipt_evidence, list):
            continue
        for item in receipt_evidence[:MAX_TRUSTED_EVIDENCE]:
            key = (
                evidence_key(item, allow_source_aliases=False)
                if isinstance(item, Mapping)
                else None
            )
            if key is not None:
                entries.add(key)
            if len(entries) >= MAX_TRUSTED_EVIDENCE:
                break
    return TrustedEvidenceIndex(context_hash=context_hash, entries=frozenset(entries))


def validate_result_evidence_is_trusted(
    result: StrategicResidentTurnResult,
    index: TrustedEvidenceIndex,
) -> str | None:
    """Reject model-invented evidence even when its inspection claim matches."""

    for item in result.evidence:
        key = (item.kind, item.source_id, item.revision or "", item.checksum or "")
        if key not in index.entries:
            return item.evidence_id
    return None


__all__ = [
    "TrustedEvidenceError",
    "TrustedEvidenceIndex",
    "build_trusted_evidence_index",
    "validate_result_evidence_is_trusted",
]
