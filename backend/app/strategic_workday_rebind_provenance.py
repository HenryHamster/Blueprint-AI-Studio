"""Stable producer provenance for restart-surviving Workday envelopes."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.models import StrategicContextEnvelopeSnapshot


WORKDAY_REBIND_DEPTH_MAX = 2_147_483_647
WORKDAY_REBIND_EDGE_KEYS = frozenset(
    {
        "workday_worker_rebind",
        "source_snapshot_id",
        "source_session_id",
        "target_session_id",
    }
)
WORKDAY_REBIND_ROOT_KEYS = (
    "workday_rebind_root_snapshot_id",
    "workday_rebind_root_project_id",
    "workday_rebind_root_session_id",
    "workday_rebind_root_worker_generation",
    "workday_rebind_root_worker_incarnation",
    "workday_rebind_root_claim_id",
    "workday_rebind_root_input_revision",
    "workday_rebind_root_transcript_sequence",
    "workday_rebind_root_ledger_revision",
    "workday_rebind_root_problem_model_revision",
    "workday_rebind_root_decision_authority_revision",
    "workday_rebind_root_context_authority_generation",
    "workday_rebind_root_privacy_generation",
    "workday_rebind_root_canonical_sha256",
)


def workday_rebind_root_provenance(
    snapshot: StrategicContextEnvelopeSnapshot,
) -> dict[str, Any] | None:
    """Return stable producer provenance carried by a server-created rebind."""

    trace = snapshot.retrieval_trace if isinstance(snapshot.retrieval_trace, dict) else {}
    is_rebound = trace.get("workday_worker_rebind") is True
    if not is_rebound:
        if (
            any(key in trace for key in WORKDAY_REBIND_EDGE_KEYS)
            or any(key in trace for key in WORKDAY_REBIND_ROOT_KEYS)
            or "workday_rebind_depth" in trace
        ):
            return None
        provenance: dict[str, Any] = {
            "workday_rebind_depth": 0,
            "workday_rebind_root_snapshot_id": str(snapshot.snapshot_id),
            "workday_rebind_root_project_id": str(snapshot.project_id),
            "workday_rebind_root_session_id": snapshot.session_id,
            "workday_rebind_root_worker_generation": snapshot.worker_generation,
            "workday_rebind_root_worker_incarnation": str(
                snapshot.worker_incarnation
            ),
            "workday_rebind_root_claim_id": str(snapshot.claim_id),
            "workday_rebind_root_input_revision": snapshot.input_revision,
            "workday_rebind_root_transcript_sequence": snapshot.transcript_sequence,
            "workday_rebind_root_ledger_revision": snapshot.ledger_revision,
            "workday_rebind_root_problem_model_revision": (
                snapshot.problem_model_revision
            ),
            "workday_rebind_root_decision_authority_revision": (
                snapshot.decision_authority_revision
            ),
            "workday_rebind_root_context_authority_generation": (
                snapshot.context_authority_generation
            ),
            "workday_rebind_root_privacy_generation": snapshot.privacy_generation,
            "workday_rebind_root_canonical_sha256": snapshot.canonical_sha256,
        }
    else:
        if not all(key in trace for key in WORKDAY_REBIND_EDGE_KEYS) or not all(
            key in trace for key in WORKDAY_REBIND_ROOT_KEYS
        ):
            return None
        provenance = {
            "workday_rebind_depth": trace.get("workday_rebind_depth"),
            **{key: trace.get(key) for key in WORKDAY_REBIND_ROOT_KEYS},
        }
        source_session_id = str(trace.get("source_session_id") or "").strip()
        target_session_id = str(trace.get("target_session_id") or "").strip()
        try:
            source_snapshot_id = uuid.UUID(str(trace.get("source_snapshot_id")))
        except (TypeError, ValueError, AttributeError):
            return None
        if (
            not source_session_id
            or not target_session_id
            or target_session_id != snapshot.session_id
            or source_snapshot_id == snapshot.snapshot_id
        ):
            return None

    try:
        root_snapshot_id = uuid.UUID(
            str(provenance["workday_rebind_root_snapshot_id"])
        )
        root_project_id = uuid.UUID(
            str(provenance["workday_rebind_root_project_id"])
        )
        root_incarnation = uuid.UUID(
            str(provenance["workday_rebind_root_worker_incarnation"])
        )
        root_claim_id = uuid.UUID(
            str(provenance["workday_rebind_root_claim_id"])
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    depth = provenance.get("workday_rebind_depth")
    root_generation = provenance.get("workday_rebind_root_worker_generation")
    root_input_revision = provenance.get("workday_rebind_root_input_revision")
    root_transcript_sequence = provenance.get(
        "workday_rebind_root_transcript_sequence"
    )
    root_ledger_revision = provenance.get("workday_rebind_root_ledger_revision")
    root_context_generation = provenance.get(
        "workday_rebind_root_context_authority_generation"
    )
    root_privacy_generation = provenance.get(
        "workday_rebind_root_privacy_generation"
    )
    exact_integers = (
        depth,
        root_generation,
        root_input_revision,
        root_transcript_sequence,
        root_ledger_revision,
        root_context_generation,
        root_privacy_generation,
    )
    if not all(type(value) is int for value in exact_integers):
        return None
    if not (
        0 <= depth <= WORKDAY_REBIND_DEPTH_MAX
        and root_generation > 0
        and provenance.get("workday_rebind_root_worker_incarnation")
        == str(root_incarnation)
        and root_input_revision >= 0
        and root_transcript_sequence >= 0
        and root_ledger_revision >= 0
        and root_context_generation >= 0
        and root_privacy_generation >= 0
        and bool(str(provenance.get("workday_rebind_root_session_id") or "").strip())
        and root_project_id == snapshot.project_id
        and root_claim_id == snapshot.claim_id
        and root_input_revision == snapshot.input_revision
        and root_transcript_sequence == snapshot.transcript_sequence
        and root_ledger_revision == snapshot.ledger_revision
        and provenance.get("workday_rebind_root_problem_model_revision")
        == snapshot.problem_model_revision
        and provenance.get("workday_rebind_root_decision_authority_revision")
        == snapshot.decision_authority_revision
        and root_context_generation == snapshot.context_authority_generation
        and root_privacy_generation == snapshot.privacy_generation
        and provenance.get("workday_rebind_root_canonical_sha256")
        == snapshot.canonical_sha256
        and hashlib.sha256(snapshot.canonical_envelope).hexdigest()
        == snapshot.canonical_sha256
        and ((depth == 0 and root_snapshot_id == snapshot.snapshot_id) or depth > 0)
        and ((depth == 0) or root_snapshot_id != snapshot.snapshot_id)
        and (
            not is_rebound
            or depth != 1
            or root_snapshot_id == source_snapshot_id
        )
        and ((depth > 0 and is_rebound) or (depth == 0 and not is_rebound))
    ):
        return None
    return provenance


__all__ = [
    "WORKDAY_REBIND_DEPTH_MAX",
    "WORKDAY_REBIND_ROOT_KEYS",
    "workday_rebind_root_provenance",
]
