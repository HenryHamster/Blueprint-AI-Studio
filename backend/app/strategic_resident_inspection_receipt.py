"""Server-authored receipts for read-only Resident inspections.

An inspection returns bounded evidence in-band; to let the Resident CITE that
evidence in a memory proposal, ``build_trusted_evidence_index`` must find a
matching ``StrategicOperationReceipt``. This builds that receipt — bound to the
turn's frozen ``context_hash`` and marked ``applied`` — and maps the inspection
evidence ``kind`` into the trust taxonomy so ``evidence_key`` accepts it.
"""

from __future__ import annotations

from typing import Any

from app.models import StrategicOperationReceipt
from app.strategic_resident_evidence_policy import (
    INSPECTION_CAPABILITY_KINDS,
    INSPECTION_EVIDENCE_KIND_MAP,
)


# Where the frozen turn ``context_hash`` is persisted on the turn task context.
TURN_CONTEXT_HASH_KEY = "_resident_turn_context_hash"
_MAX_OPERATION_ID = 128


def inspection_operation_id(turn_task_id: object, request_hash: str) -> str:
    """Return the deterministic identity for one turn-bound inspection."""

    return f"inspect:{turn_task_id}:{request_hash}"[:_MAX_OPERATION_ID]


def citable_inspection_evidence(evidence_json: dict[str, Any]) -> dict[str, Any]:
    """Return mutation-ready evidence in the trust taxonomy.

    Task inspection models call the timestamp ``observed_at`` while the mutation
    envelope deliberately calls it ``inspected_at``. Translate that wire field
    here, at the server-authored boundary, so the response can be copied exactly
    into a later mutation without provider-side schema invention.
    """

    trust_kind = INSPECTION_EVIDENCE_KIND_MAP.get(str(evidence_json.get("kind")))
    result = dict(evidence_json)
    if trust_kind is not None:
        result["kind"] = trust_kind
    observed_at = result.pop("observed_at", None)
    if observed_at is not None:
        result["inspected_at"] = observed_at
    return result


def build_inspection_receipt(
    *,
    capability_id: str,
    turn_task: Any,
    context_hash: str,
    request_hash: str,
    evidence_json: dict[str, Any],
) -> StrategicOperationReceipt | None:
    """Build an ``applied`` inspection receipt, or None if it can't be bound.

    Returns None when the capability is not a mapped inspection or the turn has
    no persisted ``context_hash`` (e.g. context was never served) — the
    inspection response is still returned; only citability is skipped.
    """

    operation_kind = INSPECTION_CAPABILITY_KINDS.get(capability_id)
    if operation_kind is None or not context_hash:
        return None
    payload = (
        turn_task.first_payload if isinstance(turn_task.first_payload, dict) else {}
    )
    context = turn_task.context if isinstance(turn_task.context, dict) else {}
    raw_generation = payload.get("authority_generation")
    if raw_generation is None:
        raw_generation = context.get("_resident_authority_generation", 0)
    try:
        authority_generation = int(raw_generation)
    except (TypeError, ValueError):
        authority_generation = 0
    citable = citable_inspection_evidence(evidence_json)
    # Deterministic per (turn, capability, params) so the ledger's one-write-per
    # -fingerprint guarantee keeps the (project_id, operation_id) unique.
    operation_id = inspection_operation_id(turn_task.id, request_hash)
    return StrategicOperationReceipt(
        project_id=turn_task.project_id,
        turn_task_id=turn_task.id,
        authority_generation=authority_generation,
        operation_id=operation_id,
        operation_kind=operation_kind,
        capability=capability_id,
        # The receipt risk CHECK constraint allows low|medium|high|critical only;
        # a read-only inspection is the lowest-risk operation.
        risk_level="low",
        request_hash=request_hash,
        expected_revisions={"context_hash": context_hash},
        status="applied",
        receipt={
            "schema_version": 1,
            "structured_result": {"evidence": [citable]},
        },
    )


__all__ = [
    "TURN_CONTEXT_HASH_KEY",
    "build_inspection_receipt",
    "citable_inspection_evidence",
    "inspection_operation_id",
]
