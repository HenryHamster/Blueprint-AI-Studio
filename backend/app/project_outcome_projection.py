"""Read-only projection of project delivery and evidence truth.

This module deliberately owns no durable state.  It composes facts already
owned by the task and release ledgers so operator surfaces do not collapse
"implemented", "promoted", "serving", and "evidenced" into one done flag.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from app.evidence_primitive import PROOF_FRESHNESS_TTL
from app.acceptance_criteria import criterion_evidence_status
from app.release_artifacts import summarize_release_artifacts


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _freshness(
    timestamp: datetime | None,
    *,
    now: datetime,
) -> str:
    if timestamp is None:
        return "unknown"
    return "stale" if now - timestamp > PROOF_FRESHNESS_TTL else "current"


def build_project_outcome_projection(
    *,
    project_id: str,
    task_facts: Iterable[tuple[str, int, datetime | None]],
    current_version: Any | None,
    promoted_version: Any | None,
    project: Any | None,
    criteria: Iterable[Any],
    evidence: Iterable[Any],
    deploy_artifacts: Iterable[Any],
    now: datetime,
) -> dict[str, Any]:
    """Compose authoritative project facts without inventing a lifecycle state."""

    task_rows = list(task_facts)
    counts = {
        str(getattr(status, "value", status)).lower(): int(count)
        for status, count, _updated_at in task_rows
    }
    task_latest = max(
        (
            updated_at
            for _status, _count, updated_at in task_rows
            if updated_at is not None
        ),
        default=None,
    )
    active_count = sum(
        counts.get(status, 0)
        for status in ("queued", "provisioning", "in_progress", "evaluating", "blocked")
    )
    if not task_rows:
        task_activity_value = "unknown"
    elif active_count:
        task_activity_value = "active"
    elif counts.get("completed", 0):
        task_activity_value = "completed_present"
    elif counts.get("failed", 0):
        task_activity_value = "failed_only"
    else:
        task_activity_value = "no_completed_tasks"

    current_opened_at = getattr(current_version, "opened_at", None)
    promoted_at = getattr(promoted_version, "promoted_at", None)
    integration = {
        "value": (
            "promoted_present"
            if promoted_version is not None
            else ("not_promoted" if current_version is not None else "unknown")
        ),
        "freshness": _freshness(promoted_at, now=now),
        "source": "release_version",
        "source_timestamp": _iso(promoted_at),
        "current_version": (
            {
                "version_id": str(current_version.id),
                "version_number": current_version.version_number,
                "status": current_version.status,
                "opened_at": _iso(current_opened_at),
            }
            if current_version is not None
            else None
        ),
        "latest_promoted": (
            {
                "version_id": str(promoted_version.id),
                "version_number": promoted_version.version_number,
                "commit_sha": promoted_version.promoted_sha,
                "promoted_at": _iso(promoted_at),
            }
            if promoted_version is not None
            else None
        ),
    }

    artifacts = list(deploy_artifacts)
    artifacts_by_kind = {
        getattr(item, "artifact_kind", None): item for item in artifacts
    }
    deployment_source = "release_artifact_registry"
    if promoted_version is None or not getattr(promoted_version, "promoted_sha", None):
        deployment_value = "not_applicable"
        artifact_evidence: list[dict[str, Any]] = []
    else:
        deployment_summary, artifact_evidence = summarize_release_artifacts(
            promoted_version,
            artifacts,
            project=project,
        )
        deployment_value = deployment_summary["serving_status"] or "unknown"
    participating_required = [
        item
        for item in artifact_evidence
        if item["required"] and item["serving_status"] is not None
    ]
    participating_timestamps: list[datetime | None] = []
    for item in participating_required:
        artifact = artifacts_by_kind.get(item["artifact_kind"])
        timestamp = (
            (
                getattr(artifact, "updated_at", None)
                or getattr(artifact, "served_at", None)
            )
            if artifact is not None
            else None
        )
        if timestamp is None and item["artifact_kind"] == "backend_revision":
            timestamp = getattr(promoted_version, "served_at", None)
        participating_timestamps.append(timestamp)
    artifact_timestamp = (
        min(timestamp for timestamp in participating_timestamps if timestamp is not None)
        if participating_timestamps
        and all(timestamp is not None for timestamp in participating_timestamps)
        else None
    )

    criterion_rows = [
        criterion
        for criterion in criteria
        if getattr(criterion, "status", "active") == "active"
    ]
    active_criterion_ids = {
        getattr(criterion, "id", None) for criterion in criterion_rows
    }
    evidence_rows = [
        item
        for item in evidence
        if getattr(item, "criterion_id", None) in active_criterion_ids
    ]
    by_criterion: dict[Any, list[Any]] = defaultdict(list)
    for item in evidence_rows:
        by_criterion[getattr(item, "criterion_id", None)].append(item)
    criterion_statuses = {
        getattr(criterion, "id", None): criterion_evidence_status(
            by_criterion[getattr(criterion, "id", None)]
        )
        for criterion in criterion_rows
    }
    status_counts = {
        status: sum(1 for value in criterion_statuses.values() if value == status)
        for status in (
            "proven",
            "path_proven",
            "partial",
            "unproven",
            "contradicted",
            "unverified",
        )
    }
    covered = status_counts["proven"]
    evidence_latest = max(
        (
            getattr(item, "recorded_at", None)
            for item in evidence_rows
            if getattr(item, "recorded_at", None) is not None
        ),
        default=None,
    )
    criterion_freshness = {
        criterion_id: _freshness(
            max(
                (
                    getattr(item, "recorded_at", None)
                    for item in by_criterion[criterion_id]
                    if getattr(item, "recorded_at", None) is not None
                ),
                default=None,
            ),
            now=now,
        )
        for criterion_id in criterion_statuses
    }
    if any(
        freshness == "stale" for freshness in criterion_freshness.values()
    ):
        evidence_freshness = "stale"
    elif evidence_rows:
        evidence_freshness = "current"
    else:
        evidence_freshness = "unknown"
    if not criterion_rows:
        evidence_value = "unknown"
    elif status_counts["contradicted"]:
        evidence_value = "contradicted"
    elif evidence_freshness == "stale" and evidence_rows:
        evidence_value = "stale"
    elif covered == len(criterion_rows):
        evidence_value = "proven"
    elif covered or status_counts["partial"] or status_counts["path_proven"]:
        evidence_value = "partial"
    else:
        evidence_value = "unproven"

    return {
        "project_id": project_id,
        "as_of": _iso(now),
        "task_activity": {
            "value": task_activity_value,
            "freshness": _freshness(task_latest, now=now),
            "source": "task_ledger",
            "source_timestamp": _iso(task_latest),
            "counts": dict(sorted(counts.items())),
        },
        "integration": integration,
        "deployment": {
            "value": deployment_value,
            "freshness": _freshness(artifact_timestamp, now=now),
            "source": deployment_source,
            "source_timestamp": _iso(artifact_timestamp),
            "participating_required_artifact_count": len(participating_required),
        },
        "evidence": {
            "value": evidence_value,
            "freshness": evidence_freshness,
            "source": "version_acceptance_criterion+criterion_evidence",
            "source_timestamp": _iso(evidence_latest),
            "version_id": (
                str(current_version.id) if current_version is not None else None
            ),
            "active_criteria": len(criterion_rows),
            "covered_criteria": covered,
            "criterion_status_counts": status_counts,
        },
    }
