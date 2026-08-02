"""Bounded evidence-packet construction for discovery review.

This module snapshots cycle tasks and artifacts, records their durable evidence
ledger entries, and builds the bounded model packet. Discovery decisions and
cycle state transitions remain in :mod:`app.discovery_review`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import String, case, cast, func
from sqlmodel import select

from app.findings_index import finalized_research_findings
from app.models import (
    Artifact,
    ArtifactType,
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    StrategicSpawnRequest,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


DISCOVERY_REVIEW_PACKET_VERSION = 1
DISCOVERY_REVIEW_MAX_TASKS = 12
DISCOVERY_REVIEW_MAX_ARTIFACTS = 8
DISCOVERY_REVIEW_MAX_PACKET_ITEMS = 24
DISCOVERY_REVIEW_METADATA_EXCERPT_CHARS = 1_500
DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS = 4_000
DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS = 8_000
DISCOVERY_REVIEW_TASK_TITLE_CHARS = 300
DISCOVERY_REVIEW_TASK_DESCRIPTION_CHARS = 800
DISCOVERY_REVIEW_URI_CHARS = 1_000
DISCOVERY_REVIEW_BLOCKED_GRACE = timedelta(hours=2)

_NON_OUTCOME_ARTIFACT_TYPES = (
    ArtifactType.GIT_COMMIT,
    ArtifactType.PULL_REQUEST,
)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return str(_enum_value(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_json_excerpt(value: Any) -> tuple[str, bool]:
    rendered = _canonical_json(value)
    if len(rendered) <= DISCOVERY_REVIEW_METADATA_EXCERPT_CHARS:
        return rendered, False
    return rendered[:DISCOVERY_REVIEW_METADATA_EXCERPT_CHARS], True


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _bounded_evidence_metadata(value: Any) -> tuple[Any, bool]:
    rendered = _canonical_json(value or {})
    if len(rendered) <= DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS:
        return json.loads(rendered), False
    return (
        {
            "excerpt": rendered[:DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS],
            "original_checksum": hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest(),
            "truncated": True,
        },
        True,
    )


def _bounded_decision_snapshot(value: Any) -> tuple[dict[str, Any], bool]:
    """Keep task payloads bounded without pretending an excerpt is complete."""

    rendered = _canonical_json(value or {})
    if len(rendered) <= DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS:
        decoded = json.loads(rendered)
        return (decoded if isinstance(decoded, dict) else {}), False
    return (
        {
            "snapshot_truncated": True,
            "excerpt": rendered[:DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS],
            "original_checksum": hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest(),
        },
        True,
    )


def _task_snapshot(task: Task) -> dict[str, Any]:
    task_type = str(task.first_payload.get("type") or "")
    # Research plan_output is a resumable pre-gate buffer until completion
    # stamps its authoritative survivor set. Discovery review is a truth
    # consumer, so failed/unfinalized raw findings must never enter its packet.
    plan_output = (
        finalized_research_findings(task)
        if task_type == "research"
        else task.plan_output
    )
    plan_excerpt, plan_truncated = _bounded_json_excerpt(plan_output)
    context = task.context or {}
    context_projection = {
        key: context.get(key)
        for key in (
            "direction_id",
            "research_task_id",
            "acceptance_verdict",
            "acceptance_evidence_result",
            "failure_category",
            "failure_class",
            "pr_merged",
            "landed_on_integration",
            "rollup_pr_number",
            "capacity_degraded",
            "research_surviving_findings",
        )
        if key in context
    }
    return {
        "id": str(task.id),
        "type": task_type,
        "title": _bounded_text(task.title, DISCOVERY_REVIEW_TASK_TITLE_CHARS),
        "description": _bounded_text(
            task.description,
            DISCOVERY_REVIEW_TASK_DESCRIPTION_CHARS,
        ),
        "status": str(_enum_value(task.status)),
        "attempt_count": int(task.attempt_count or 0),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "created_by": task.created_by,
        "latest_status_message": (task.latest_status_message or "")[:1_000],
        "context": context_projection,
        "plan_output_excerpt": plan_excerpt,
        "plan_output_truncated": plan_truncated,
    }


def _artifact_snapshot(artifact: Artifact) -> dict[str, Any]:
    logs_excerpt, logs_truncated = _bounded_json_excerpt(artifact.logs)
    return {
        "id": str(artifact.id),
        "task_id": str(artifact.task_id),
        "artifact_type": str(_enum_value(artifact.artifact_type)),
        "logical_key": _bounded_text(artifact.logical_key, 500),
        "commit_hash": artifact.commit_hash,
        "repo_url": _bounded_text(artifact.repo_url, DISCOVERY_REVIEW_URI_CHARS),
        "pr_url": _bounded_text(artifact.pr_url, DISCOVERY_REVIEW_URI_CHARS),
        "storage_uri": _bounded_text(
            artifact.storage_uri,
            DISCOVERY_REVIEW_URI_CHARS,
        ),
        "iteration": artifact.iteration,
        "created_at": artifact.created_at,
        "logs_excerpt": logs_excerpt,
        "logs_truncated": logs_truncated,
    }


async def _get_or_add_evidence(
    session,
    *,
    cycle: DiscoveryCycle,
    dedupe_key: str,
    semantic_role: str,
    source_kind: str,
    source_id: str,
    source_uri: str | None,
    content_type: str,
    head_sha: str | None,
    snapshot: Mapping[str, Any],
) -> DiscoveryEvidenceItem:
    existing = (
        await session.execute(
            select(DiscoveryEvidenceItem).where(
                DiscoveryEvidenceItem.cycle_id == cycle.id,
                DiscoveryEvidenceItem.dedupe_key == dedupe_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    item = DiscoveryEvidenceItem(
        cycle_id=cycle.id,
        semantic_role=semantic_role,
        source_kind=source_kind,
        source_id=source_id,
        source_uri=source_uri,
        checksum=_checksum(snapshot),
        content_type=content_type,
        head_sha=head_sha,
        mechanism_fingerprint=cycle.mechanism_fingerprint,
        evidence_metadata={
            "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
            # Round-trip through the canonical encoder so UUID/datetime/Enum
            # values become JSON-safe before reaching the JSONB bind processor.
            "snapshot": json.loads(_canonical_json(snapshot)),
        },
        dedupe_key=dedupe_key,
    )
    session.add(item)
    await session.flush()
    return item


def _evidence_projection(
    item: DiscoveryEvidenceItem,
    *,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    evidence_metadata, evidence_metadata_truncated = (
        _bounded_evidence_metadata(item.evidence_metadata)
    )
    projection: dict[str, Any] = {
        "evidence_id": str(item.id),
        "semantic_role": item.semantic_role,
        "source_kind": item.source_kind,
        "checksum": item.checksum,
        "source_id": item.source_id,
        "source_uri": _bounded_text(item.source_uri, DISCOVERY_REVIEW_URI_CHARS),
        "content_type": item.content_type,
        "head_sha": item.head_sha,
        "mechanism_fingerprint": item.mechanism_fingerprint,
        "evidence_metadata": evidence_metadata,
    }
    if evidence_metadata_truncated:
        projection["evidence_metadata_truncated"] = True
    if artifact_id is not None:
        projection["artifact_id"] = artifact_id
    return projection


async def build_discovery_review_packet(
    session,
    cycle: DiscoveryCycle,
    *,
    now: datetime | None = None,
) -> tuple[
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Build and ledger a bounded, cycle-isolated evidence packet.

    The caller must hold the cycle row lock.  Quiescence is checked separately
    over the full task set; the bounds here limit only what is sent to the model.
    """

    now = now or _utcnow()
    bounded_intent, intent_truncated = _bounded_decision_snapshot(
        cycle.intent_snapshot
    )
    bounded_bet, bet_truncated = _bounded_decision_snapshot(cycle.bet_snapshot)
    task_scope = [
        Task.discovery_cycle_id == cycle.id,
        (
            Task.id != cycle.review_task_id
            if cycle.review_task_id is not None
            else True
        ),
    ]
    total_tasks = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(*task_scope)
            )
        ).scalar_one()
        or 0
    )
    task_packet_limit = min(
        DISCOVERY_REVIEW_MAX_TASKS,
        max(0, DISCOVERY_REVIEW_MAX_PACKET_ITEMS - 1),
    )

    # Reserve slots for the source and one representative of every outcome
    # state before filling chronologically. This prevents a large successful
    # cohort from pushing the source or rarer failures out of the bounded packet.
    tasks: list[Task] = []
    selected_task_ids: set[uuid.UUID] = set()

    async def add_task(task: Task | None) -> None:
        if task is None or task.id in selected_task_ids:
            return
        if len(tasks) >= task_packet_limit:
            return
        tasks.append(task)
        selected_task_ids.add(task.id)

    if cycle.source_research_task_id is not None:
        await add_task(
            (
                await session.execute(
                    select(Task).where(
                        *task_scope,
                        Task.id == cycle.source_research_task_id,
                    )
                )
            ).scalar_one_or_none()
        )
    for status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PARKED,
        TaskStatus.BLOCKED,
    ):
        await add_task(
            (
                await session.execute(
                    select(Task)
                    .where(*task_scope, Task.status == status)
                    .order_by(Task.created_at.asc(), Task.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        )

    remaining_slots = task_packet_limit - len(tasks)
    if remaining_slots:
        filler = list(
            (
                await session.execute(
                    select(Task)
                    .where(
                        *task_scope,
                        (
                            Task.id.notin_(selected_task_ids)
                            if selected_task_ids
                            else True
                        ),
                    )
                    .order_by(Task.created_at.asc(), Task.id.asc())
                    .limit(remaining_slots)
                )
            )
            .scalars()
            .all()
        )
        for task in filler:
            await add_task(task)

    total_artifacts = int(
        (
            await session.execute(
                select(func.count(Artifact.id))
                .select_from(Artifact)
                .join(Task, Artifact.task_id == Task.id)
                .where(*task_scope)
            )
        ).scalar_one()
        or 0
    )
    artifact_packet_limit = min(
        DISCOVERY_REVIEW_MAX_ARTIFACTS,
        max(0, DISCOVERY_REVIEW_MAX_PACKET_ITEMS - 1 - len(tasks)),
    )
    # A producer may update the same logical artifact many times. Only its
    # newest version is decision-bearing; older versions are optional history,
    # not missing required evidence. Artifacts without a stable logical key are
    # deliberately unique so unrelated outcomes cannot be collapsed silently.
    artifact_group_key = func.coalesce(
        func.nullif(Artifact.logical_key, ""),
        cast(Artifact.id, String),
    )
    ranked_artifacts = (
        select(
            Artifact.id.label("artifact_id"),
            func.row_number()
            .over(
                partition_by=(
                    Artifact.task_id,
                    Artifact.artifact_type,
                    artifact_group_key,
                ),
                order_by=(Artifact.created_at.desc(), Artifact.id.desc()),
            )
            .label("group_rank"),
        )
        .join(Task, Artifact.task_id == Task.id)
        .where(*task_scope)
        .subquery()
    )
    required_artifact_total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ranked_artifacts)
                .where(ranked_artifacts.c.group_rank == 1)
            )
        ).scalar_one()
        or 0
    )
    artifacts = list(
        (
            await session.execute(
                select(Artifact)
                .join(
                    ranked_artifacts,
                    ranked_artifacts.c.artifact_id == Artifact.id,
                )
                .where(ranked_artifacts.c.group_rank == 1)
                .order_by(
                    case(
                        (
                            Artifact.artifact_type.in_(
                                _NON_OUTCOME_ARTIFACT_TYPES
                            ),
                            1,
                        ),
                        else_=0,
                    ),
                    Artifact.created_at.desc(),
                    Artifact.id.desc(),
                )
                .limit(artifact_packet_limit)
            )
        )
        .scalars()
        .all()
    )

    task_evidence: list[DiscoveryEvidenceItem] = []
    for task in tasks:
        snapshot = _task_snapshot(task)
        role = (
            "source_research"
            if task.id == cycle.source_research_task_id
            else "cohort_task"
        )
        task_evidence.append(
            await _get_or_add_evidence(
                session,
                cycle=cycle,
                dedupe_key=f"task:{task.id}:terminal:v1",
                semantic_role=role,
                source_kind="task",
                source_id=str(task.id),
                source_uri=None,
                content_type="application/vnd.blueprint.task+json",
                head_sha=cycle.candidate_sha,
                snapshot=snapshot,
            )
        )

    artifact_prerequisites: list[dict[str, Any]] = []
    artifact_evidence: list[DiscoveryEvidenceItem] = []
    for artifact in artifacts:
        snapshot = _artifact_snapshot(artifact)
        artifact_evidence.append(
            await _get_or_add_evidence(
                session,
                cycle=cycle,
                dedupe_key=f"artifact:{artifact.id}:v1",
                semantic_role="outcome_artifact",
                source_kind="artifact",
                source_id=str(artifact.id),
                source_uri=artifact.storage_uri or artifact.pr_url,
                content_type=f"application/vnd.blueprint.artifact.{str(_enum_value(artifact.artifact_type)).lower()}+json",
                head_sha=artifact.commit_hash or cycle.candidate_sha,
                snapshot=snapshot,
            )
        )
        if artifact.storage_uri:
            artifact_prerequisites.append(
                {
                    "artifact_id": str(artifact.id),
                    "artifact_type": str(_enum_value(artifact.artifact_type)),
                    "logical_key": _bounded_text(artifact.logical_key, 500),
                    "source_task_id": str(artifact.task_id),
                    "storage_uri": _bounded_text(
                        artifact.storage_uri,
                        DISCOVERY_REVIEW_URI_CHARS,
                    ),
                }
            )

    overdue_blocked_ids = [
        str(task.id)
        for task in tasks
        if task.status == TaskStatus.BLOCKED
        and cycle.deadline_at is not None
        and now >= cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ]
    overdue_blocked_count = 0
    if (
        cycle.deadline_at is not None
        and now >= cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ):
        overdue_blocked_count = int(
            (
                await session.execute(
                    select(func.count(Task.id)).where(
                        *task_scope,
                        Task.status == TaskStatus.BLOCKED,
                    )
                )
            ).scalar_one()
            or 0
        )
    overdue_spawn_request_count = 0
    if (
        cycle.deadline_at is not None
        and now >= cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ):
        overdue_spawn_request_count = int(
            (
                await session.execute(
                    select(func.count(StrategicSpawnRequest.id)).where(
                        StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                        StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                        StrategicSpawnRequest.task_id.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
    omitted_tasks = max(0, total_tasks - len(tasks))
    omitted_artifacts = max(0, total_artifacts - len(artifacts))
    omitted_required_artifacts = max(
        0, required_artifact_total - len(artifacts)
    )
    omitted_optional_artifacts = max(
        0, total_artifacts - required_artifact_total
    )
    required_evidence_omitted = bool(
        omitted_tasks
        or omitted_required_artifacts
        or overdue_spawn_request_count
        or intent_truncated
        or bet_truncated
    )
    manifest: dict[str, Any] = {
        "version": DISCOVERY_REVIEW_PACKET_VERSION,
        "tasks": {
            "total": total_tasks,
            "included": len(tasks),
            "omitted": omitted_tasks,
        },
        "artifacts": {
            "total": total_artifacts,
            "required_total": required_artifact_total,
            "included": len(artifacts),
            "omitted": omitted_artifacts,
            "omitted_required": omitted_required_artifacts,
            "omitted_optional": omitted_optional_artifacts,
        },
        "decision_snapshots": {
            "intent": {
                "checksum": _checksum(cycle.intent_snapshot or {}),
                "truncated": intent_truncated,
            },
            "bet": {
                "checksum": _checksum(cycle.bet_snapshot or {}),
                "truncated": bet_truncated,
            },
        },
        "overdue_blocked": {
            "count": overdue_blocked_count,
            "included_task_ids": overdue_blocked_ids,
        },
        "overdue_spawn_requests": {"count": overdue_spawn_request_count},
        "required_evidence_omitted": required_evidence_omitted,
        "incomplete": bool(
            required_evidence_omitted
            or overdue_blocked_count
            or omitted_optional_artifacts
        ),
        "incomplete_reasons": [
            reason
            for condition, reason in (
                (bool(omitted_tasks), "task_limit"),
                (bool(omitted_required_artifacts), "artifact_limit"),
                (
                    bool(omitted_optional_artifacts),
                    "artifact_redundancy_deduplicated",
                ),
                (intent_truncated, "intent_snapshot_truncated"),
                (bet_truncated, "bet_snapshot_truncated"),
                (bool(overdue_blocked_count), "overdue_blocked_tasks"),
                (
                    bool(overdue_spawn_request_count),
                    "overdue_deferred_spawn_requests",
                ),
            )
            if condition
        ],
    }
    pre_manifest_ledger_total = int(
        (
            await session.execute(
                select(func.count(DiscoveryEvidenceItem.id)).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        ).scalar_one()
        or 0
    )
    manifest["ledger"] = {
        "total": pre_manifest_ledger_total + 1,
        "included": min(
            pre_manifest_ledger_total + 1,
            DISCOVERY_REVIEW_MAX_PACKET_ITEMS,
        ),
        "omitted": max(
            0,
            pre_manifest_ledger_total + 1 - DISCOVERY_REVIEW_MAX_PACKET_ITEMS,
        ),
    }
    if manifest["ledger"]["omitted"]:
        manifest["incomplete"] = True
        manifest["required_evidence_omitted"] = True
        manifest["incomplete_reasons"].append("ledger_packet_limit")
    manifest_item = await _get_or_add_evidence(
        session,
        cycle=cycle,
        dedupe_key="packet-manifest:v1",
        semantic_role="packet_manifest",
        source_kind="server_ledger",
        source_id=str(cycle.id),
        source_uri=None,
        content_type="application/vnd.blueprint.discovery-review-manifest+json",
        head_sha=cycle.candidate_sha,
        snapshot=manifest,
    )

    # Re-read the ledger as the packet authority so evidence captured before
    # this boundary (for example an experiment or experiential observation)
    # is not lost merely because it was not represented by a legacy Artifact
    # row. Stable creation order plus the hard bound keeps prompt size fixed.
    priority_items = [manifest_item, *task_evidence, *artifact_evidence]
    priority_ids = {item.id for item in priority_items}
    remaining_packet_slots = max(
        0, DISCOVERY_REVIEW_MAX_PACKET_ITEMS - len(priority_items)
    )
    ledger_items = priority_items[:DISCOVERY_REVIEW_MAX_PACKET_ITEMS]
    if remaining_packet_slots:
        ledger_items.extend(
            list(
                (
                    await session.execute(
                        select(DiscoveryEvidenceItem)
                        .where(
                            DiscoveryEvidenceItem.cycle_id == cycle.id,
                            DiscoveryEvidenceItem.id.notin_(priority_ids),
                        )
                        .order_by(
                            DiscoveryEvidenceItem.created_at.asc(),
                            DiscoveryEvidenceItem.id.asc(),
                        )
                        .limit(remaining_packet_slots)
                    )
                )
                .scalars()
                .all()
            )
        )
    artifact_ids = {str(artifact.id) for artifact in artifacts}
    packet = [
        _evidence_projection(
            item,
            artifact_id=(
                item.source_id
                if item.source_kind == "artifact"
                and item.source_id in artifact_ids
                else None
            ),
        )
        for item in ledger_items
    ]
    if any(entry.get("evidence_metadata_truncated") for entry in packet):
        manifest["incomplete"] = True
        manifest["required_evidence_omitted"] = True
        if "evidence_metadata_truncated" not in manifest["incomplete_reasons"]:
            manifest["incomplete_reasons"].append("evidence_metadata_truncated")
        # Keep the durable manifest item and its packet projection aligned with
        # the fail-closed payload manifest after discovering oversized legacy
        # evidence. This is the sole mutable ledger row: it describes the packet
        # boundary itself rather than source evidence.
        manifest_item.checksum = _checksum(manifest)
        manifest_item.evidence_metadata = {
            "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
            "snapshot": json.loads(_canonical_json(manifest)),
        }
        session.add(manifest_item)
        packet = [
            (
                _evidence_projection(manifest_item)
                if entry.get("evidence_id") == str(manifest_item.id)
                else entry
            )
            for entry in packet
        ]
    allowed_artifact_ids = {
        str(entry["artifact_id"])
        for entry in packet
        if entry.get("artifact_id")
    }
    artifact_prerequisites = [
        pointer
        for pointer in artifact_prerequisites
        if pointer["artifact_id"] in allowed_artifact_ids
    ]
    return (
        packet,
        _checksum(packet),
        artifact_prerequisites,
        manifest,
        bounded_intent,
        bounded_bet,
    )
