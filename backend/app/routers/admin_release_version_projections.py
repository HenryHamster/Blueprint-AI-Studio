"""Pure, bounded projections shared by release-version lifecycle endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.models import ReleaseQualificationIntent, ReleaseVersion
from app.time_utils import _coerce_naive_utc, utc_iso


_MODULE_SIZE_GOVERNANCE_CAP: int = 250


def _qualification_intent_projection(
    row: ReleaseQualificationIntent,
) -> dict[str, Any]:
    """Bounded exact receipt projection for qualification delivery recovery."""

    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "release_version_id": str(row.release_version_id),
        "source_task_id": str(row.source_task_id),
        "authority_kind": row.authority_kind,
        "authority_key": row.authority_key,
        "idempotency_key": row.idempotency_key,
        "specification_hash": row.specification_hash,
        "reason": row.reason,
        "requested_probes": list(row.requested_probes or []),
        "expected_phase_changed_at": utc_iso(row.expected_phase_changed_at),
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "claim_id": str(row.claim_id) if row.claim_id is not None else None,
        "lease_expires_at": utc_iso(row.lease_expires_at),
        "next_attempt_at": utc_iso(row.next_attempt_at),
        "result": dict(row.result or {}),
        "last_error": row.last_error,
        "created_at": utc_iso(row.created_at),
        "updated_at": utc_iso(row.updated_at),
        "completed_at": utc_iso(row.completed_at),
    }


def _compose_serving_block(
    aggregate: dict,
    artifacts: list,
    version: ReleaseVersion,
) -> dict:
    """Build the resolved serving block from aggregate + artifact evidence."""

    aggregate_status = aggregate.get("serving_status")
    serving_sha = next(
        (
            evidence["served_identifier"]
            for evidence in artifacts
            if evidence.get("serving_status") == "serving_confirmed"
            and evidence.get("served_identifier") is not None
        ),
        getattr(version, "served_sha", None),
    )
    confirmed_at = next(
        (
            evidence["served_at"]
            for evidence in artifacts
            if evidence.get("serving_status") == "serving_confirmed"
            and evidence.get("served_at")
        ),
        utc_iso(version.served_at),
    )
    return {
        "status": aggregate_status,
        "sha": serving_sha,
        "confirmed_at": confirmed_at,
        "failure_reason": (
            aggregate.get("reason_code")
            if aggregate_status == "deploy_failed"
            else None
        ),
        "fleet_freshness": aggregate.get("fleet_freshness"),
        "artifacts": artifacts,
    }


def _format_qualification(qual: Dict[str, Any]) -> Dict[str, Any]:
    """Extract operator-facing fields from a last_qualification record."""

    from app.release_readiness import format_qualification_for_api  # noqa: PLC0415

    out = format_qualification_for_api(qual)
    candidate_gate = qual.get("candidate_operator_authorship_gate")
    if not isinstance(candidate_gate, dict):
        sub_scores = qual.get("sub_scores")
        candidate_gate = (
            sub_scores.get("candidate_operator_authorship")
            if isinstance(sub_scores, dict)
            else None
        )
    out.update(
        {
            "status": qual.get("status"),
            "qualification_attempt_id": qual.get("qualification_attempt_id"),
            "retry_episode": qual.get("retry_episode"),
            "retry_episode_cap": qual.get("retry_episode_cap"),
            "confidence": qual.get("confidence"),
            "threshold": qual.get("threshold"),
            "sub_scores": qual.get("sub_scores"),
            "ci_resolution": qual.get("ci_resolution"),
            "blocker": qual.get("blocker"),
            "finalized_at": qual.get("finalized_at"),
            "candidate_sha": qual.get("candidate_sha"),
            "candidate_operator_authorship_gate": candidate_gate,
            "error": qual.get("error"),
            "consecutive_infra_errors": qual.get("consecutive_infra_errors"),
            "is_permanent_gate_incapacity": qual.get(
                "is_permanent_gate_incapacity"
            ),
            "audit_missing_reason": qual.get("audit_missing_reason"),
            "audit_reconciliation": qual.get("audit_reconciliation"),
        }
    )
    return out


def _closed_version_until(version: ReleaseVersion) -> Optional[datetime]:
    """Return the upper timestamp bound for a closed version's task window."""

    if version.status == "promoted" and version.promoted_at is not None:
        return version.promoted_at
    if version.status == "abandoned":
        raw = (version.last_qualification or {}).get("abandoned_at")
        if isinstance(raw, str):
            try:
                return _coerce_naive_utc(datetime.fromisoformat(raw))
            except (ValueError, TypeError):
                pass
    return None
