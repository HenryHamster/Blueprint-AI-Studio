"""Durable review boundary for bounded discovery cycles.

The Resident is deliberately not part of this state machine.  A normal
``discovery_review`` task judges a quiescent cycle from a server-built evidence
packet; a durable ProjectMessage merely makes the resulting decision available
to the next strategic session, whenever that session happens to be online.

All mutating helpers operate on the caller's transaction and never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Any, Mapping
import uuid

from sqlalchemy import func
from sqlmodel import select

from app.models import (
    Artifact,
    DiscoveryAdmission,
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    ProjectMessage,
    StrategicSpawnRequest,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.notification_dispatch import post_event_message
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)

DISCOVERY_REVIEW_TASK_TYPE = "discovery_review"
DISCOVERY_REVIEW_PACKET_VERSION = 1
DISCOVERY_REVIEW_MAX_TASKS = 120
DISCOVERY_REVIEW_MAX_ARTIFACTS = 80
DISCOVERY_REVIEW_MAX_PACKET_ITEMS = 200
DISCOVERY_REVIEW_METADATA_EXCERPT_CHARS = 4_000
DISCOVERY_REVIEW_SWEEP_LIMIT = 40
DISCOVERY_REVIEW_BLOCKED_GRACE = timedelta(hours=2)
MAX_AUTONOMOUS_DISCOVERY_GENERATIONS = 3

_ACTIVE_COHORT_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
)
_SOURCE_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)
_SPAWNABLE_CYCLE_STATES = frozenset(
    {"open", "admission_closed", "review_pending"}
)
_VALID_VERDICTS = frozenset(
    {
        "renew",
        "pivot",
        "expand",
        "branch",
        "fallback_pending",
        "park",
        "stop",
        "ask_operator",
        "inconclusive",
    }
)
_VALID_PHASES = frozenset({"probe", "demonstrate", "productize", "release"})
_AUTONOMOUS_CONTINUATION_VERDICTS = frozenset({"renew", "inconclusive"})
_CONSEQUENTIAL_VERDICTS = frozenset(
    {"pivot", "expand", "branch", "fallback_pending"}
)
_RECOMMENDED_LIMIT_KEYS = frozenset(
    {"task_credit_limit", "attempt_credit_limit", "concurrency_limit"}
)


@dataclass(frozen=True)
class DiscoveryReviewSpawnResult:
    cycle_id: uuid.UUID
    task_id: uuid.UUID | None
    spawned: bool
    ready: bool
    reason: str


class DiscoveryReviewResultError(ValueError):
    """The review output cannot authoritatively transition its cycle."""


async def resolve_discovery_cycle(
    session,
    *,
    project_id: uuid.UUID,
    direction_key: str,
    action: str,
    next_bet: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> DiscoveryCycle:
    """Apply an explicit operator decision without depending on Resident uptime."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"continue", "close"}:
        raise ValueError("action must be continue or close")
    if normalized_action == "continue":
        live_successor = (
            await session.execute(
                select(DiscoveryCycle)
                .where(
                    DiscoveryCycle.project_id == project_id,
                    DiscoveryCycle.direction_key == direction_key,
                    DiscoveryCycle.state == "open",
                )
                .order_by(DiscoveryCycle.generation.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if live_successor is not None:
            predecessor = (
                await session.execute(
                    select(DiscoveryCycle).where(
                        DiscoveryCycle.project_id == project_id,
                        DiscoveryCycle.direction_key == direction_key,
                        DiscoveryCycle.generation == live_successor.generation - 1,
                    )
                )
            ).scalar_one_or_none()
            prior_resolution = (
                (predecessor.review_result or {}).get("operator_resolution")
                if predecessor is not None
                and isinstance(predecessor.review_result, dict)
                else None
            )
            if (
                isinstance(prior_resolution, Mapping)
                and prior_resolution.get("action") == "continue"
                and dict(live_successor.bet_snapshot or {}) == dict(next_bet or {})
            ):
                return live_successor
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.project_id == project_id,
                DiscoveryCycle.direction_key == direction_key,
                DiscoveryCycle.state.in_({"reviewed", "review_blocked"}),
            )
            .order_by(DiscoveryCycle.generation.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise ValueError("no operator-resolvable discovery cycle exists")
    resolution = {
        "action": normalized_action,
        "note": str(note or "")[:2_000] or None,
        "resolved_at": _utcnow().isoformat(),
    }
    prior = dict(cycle.review_result or {})
    prior["operator_resolution"] = resolution
    cycle.review_result = prior
    cycle.updated_at = _utcnow()
    if normalized_action == "close":
        cycle.verdict = "stop"
        session.add(cycle)
        return cycle
    if not isinstance(next_bet, Mapping) or not next_bet:
        raise ValueError("continue requires a non-empty next_bet")
    cycle.state = "superseded"
    successor = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=cycle.generation + 1,
        outcome_kind=cycle.outcome_kind,
        phase=cycle.phase,
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=dict(next_bet),
        mechanism_fingerprint=_checksum(dict(next_bet)),
        task_credit_limit=cycle.task_credit_limit,
        attempt_credit_limit=cycle.attempt_credit_limit,
        concurrency_limit=cycle.concurrency_limit,
        deadline_at=_utcnow() + timedelta(hours=24),
    )
    session.add(cycle)
    session.add(successor)
    await session.flush()
    return successor


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


def _task_snapshot(task: Task) -> dict[str, Any]:
    plan_excerpt, plan_truncated = _bounded_json_excerpt(task.plan_output)
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
            "_research_surviving_findings",
        )
        if key in context
    }
    return {
        "id": str(task.id),
        "type": str(task.first_payload.get("type") or ""),
        "title": task.title,
        "description": task.description,
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
        "logical_key": artifact.logical_key,
        "commit_hash": artifact.commit_hash,
        "repo_url": artifact.repo_url,
        "pr_url": artifact.pr_url,
        "storage_uri": artifact.storage_uri,
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
    projection: dict[str, Any] = {
        "evidence_id": str(item.id),
        "semantic_role": item.semantic_role,
        "source_kind": item.source_kind,
        "checksum": item.checksum,
        "source_id": item.source_id,
        "source_uri": item.source_uri,
        "content_type": item.content_type,
        "head_sha": item.head_sha,
        "mechanism_fingerprint": item.mechanism_fingerprint,
        "evidence_metadata": item.evidence_metadata or {},
    }
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
]:
    """Build and ledger a bounded, cycle-isolated evidence packet.

    The caller must hold the cycle row lock.  Quiescence is checked separately
    over the full task set; the bounds here limit only what is sent to the model.
    """

    now = now or _utcnow()
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
    artifacts = list(
        (
            await session.execute(
                select(Artifact)
                .join(Task, Artifact.task_id == Task.id)
                .where(*task_scope)
                .order_by(Artifact.created_at.asc(), Artifact.id.asc())
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
                    "logical_key": artifact.logical_key,
                    "source_task_id": str(artifact.task_id),
                    "storage_uri": artifact.storage_uri,
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
    required_evidence_omitted = bool(
        omitted_tasks or omitted_artifacts or overdue_spawn_request_count
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
            "included": len(artifacts),
            "omitted": omitted_artifacts,
        },
        "overdue_blocked": {
            "count": overdue_blocked_count,
            "included_task_ids": overdue_blocked_ids,
        },
        "overdue_spawn_requests": {"count": overdue_spawn_request_count},
        "required_evidence_omitted": required_evidence_omitted,
        "incomplete": bool(required_evidence_omitted or overdue_blocked_count),
        "incomplete_reasons": [
            reason
            for condition, reason in (
                (bool(omitted_tasks), "task_limit"),
                (bool(omitted_artifacts), "artifact_limit"),
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
    return packet, _checksum(packet), artifact_prerequisites, manifest


async def _cycle_is_quiescent(
    session,
    cycle: DiscoveryCycle,
    *,
    now: datetime,
) -> tuple[bool, str]:
    if cycle.source_research_task_id is None:
        return False, "source_research_task_missing"
    source = await session.get(Task, cycle.source_research_task_id)
    if source is None:
        return False, "source_research_task_missing"
    if source.status not in _SOURCE_TERMINAL_STATUSES:
        return False, "source_research_task_active"

    active = (
        await session.execute(
            select(Task.id)
            .where(
                Task.discovery_cycle_id == cycle.id,
                Task.status.in_(_ACTIVE_COHORT_STATUSES),
                Task.id != cycle.review_task_id
                if cycle.review_task_id is not None
                else True,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return False, "cohort_active"
    blocked = (
        await session.execute(
            select(Task.id)
            .where(
                Task.discovery_cycle_id == cycle.id,
                Task.status == TaskStatus.BLOCKED,
                Task.id != cycle.review_task_id
                if cycle.review_task_id is not None
                else True,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if blocked is not None and (
        cycle.deadline_at is None
        or now < cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ):
        return False, "cohort_blocked_in_grace"
    pending_request = (
        await session.execute(
            select(StrategicSpawnRequest.id)
            .where(
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                StrategicSpawnRequest.task_id.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending_request is not None and (
        cycle.deadline_at is None
        or now < cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ):
        return False, "spawn_requests_in_grace"
    return True, "quiescent"


async def _cancel_overdue_spawn_requests(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> int:
    """Disposition deferred intents that outlived the envelope and grace period."""

    if (
        cycle.deadline_at is None
        or now < cycle.deadline_at + DISCOVERY_REVIEW_BLOCKED_GRACE
    ):
        return 0
    requests = (
        await session.execute(
            select(StrategicSpawnRequest)
            .where(
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                StrategicSpawnRequest.task_id.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    if not requests:
        return 0
    request_ids = [request.id for request in requests]
    admissions = (
        await session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == cycle.id,
                DiscoveryAdmission.spawn_request_id.in_(request_ids),
                DiscoveryAdmission.state == "reserved",
            )
        )
    ).scalars().all()
    from app.discovery_admission import release_discovery_admission

    for admission in admissions:
        await release_discovery_admission(
            session,
            cycle_id=cycle.id,
            admission_key=admission.admission_key,
            now=now,
        )
    for request in requests:
        request.state = "cancelled"
        request.admission_reason = "discovery_cycle_review_boundary"
        request.not_before = None
        request.updated_at = now
        session.add(request)
    return len(requests)


async def maybe_close_and_spawn_discovery_review(
    session,
    cycle_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> DiscoveryReviewSpawnResult:
    """Close a terminal cohort and atomically queue exactly one review task."""

    parsed_cycle_id = (
        cycle_id if isinstance(cycle_id, uuid.UUID) else uuid.UUID(str(cycle_id))
    )
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == parsed_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return DiscoveryReviewSpawnResult(
            parsed_cycle_id, None, False, False, "cycle_missing"
        )
    if cycle.review_task_id is not None:
        return DiscoveryReviewSpawnResult(
            cycle.id, cycle.review_task_id, False, True, "review_exists"
        )
    if cycle.state not in _SPAWNABLE_CYCLE_STATES:
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, False, f"cycle_state_{cycle.state}"
        )

    now = now or _utcnow()
    if cycle.deadline_at is not None and now >= cycle.deadline_at:
        cycle.state = "admission_closed"

    ready, reason = await _cycle_is_quiescent(session, cycle, now=now)
    if not ready:
        # Once the source is terminal no new descendant admissions should start,
        # but already-running/queued work remains intact and is allowed to drain.
        if reason in {
            "cohort_active",
            "cohort_blocked_in_grace",
            "spawn_requests_in_grace",
        } and cycle.state == "open":
            cycle.state = "admission_closed"
        if cycle.state == "admission_closed":
            cycle.state = "review_pending"
        session.add(cycle)
        return DiscoveryReviewSpawnResult(cycle.id, None, False, False, reason)

    packet, packet_checksum, artifact_prerequisites, packet_manifest = (
        await build_discovery_review_packet(session, cycle, now=now)
    )
    await _cancel_overdue_spawn_requests(session, cycle=cycle, now=now)
    review_revision = int(cycle.revision or 0) + 1
    task = Task(
        project_id=cycle.project_id,
        discovery_cycle_id=cycle.id,
        title=f"Discovery review: {cycle.direction_key}",
        description=(
            "Judge the completed discovery cycle against its frozen intent and bet "
            "using the server-authorized evidence packet."
        ),
        parent_task_id=None,
        payloads=[
            {
                "type": DISCOVERY_REVIEW_TASK_TYPE,
                "cycle_id": str(cycle.id),
                "expected_revision": review_revision,
                "direction_key": cycle.direction_key,
                "intent_snapshot": cycle.intent_snapshot or {},
                "bet_snapshot": cycle.bet_snapshot or {},
                "outcome_kind": cycle.outcome_kind,
                "phase": cycle.phase,
                "evidence_packet": packet,
                "evidence_packet_manifest": packet_manifest,
                "packet_checksum": packet_checksum,
                "candidate_sha": cycle.candidate_sha,
                "mechanism_fingerprint": cycle.mechanism_fingerprint,
                "review_round": 1,
                "prior_review_task_id": None,
            }
        ],
        context={
            "discovery_review": True,
            "discovery_cycle_id": str(cycle.id),
            "source_research_task_id": (
                str(cycle.source_research_task_id)
                if cycle.source_research_task_id
                else None
            ),
            "artifact_prerequisites": artifact_prerequisites,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        idempotency_key=f"discovery-review:{cycle.id}:round:1",
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "DISCOVERY_REVIEW_BOUNDARY",
                "cycle_id": str(cycle.id),
                "packet_checksum": packet_checksum,
            },
        )
    )
    await session.flush()

    cycle.review_task_id = task.id
    cycle.state = "reviewing"
    cycle.revision = review_revision
    cycle.updated_at = now
    session.add(cycle)
    return DiscoveryReviewSpawnResult(cycle.id, task.id, True, True, "spawned")


def parse_discovery_review_result(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate the model result without accepting prose or inferred defaults."""

    if isinstance(raw, Mapping):
        result = dict(raw)
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DiscoveryReviewResultError("review result is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise DiscoveryReviewResultError("review result must be a JSON object")
        result = parsed
    else:
        raise DiscoveryReviewResultError("review result must be an object")

    try:
        result["cycle_id"] = str(uuid.UUID(str(result.get("cycle_id"))))
    except (TypeError, ValueError, AttributeError):
        raise DiscoveryReviewResultError("cycle_id must be a UUID") from None
    reviewed_revision = result.get("reviewed_revision")
    if isinstance(reviewed_revision, bool) or not isinstance(reviewed_revision, int):
        raise DiscoveryReviewResultError("reviewed_revision must be an integer")

    verdict = result.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise DiscoveryReviewResultError("review verdict is missing or invalid")
    confidence = result.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise DiscoveryReviewResultError("confidence must be between 0 and 1")
    for key in ("decision_summary", "goal_progress_assessment"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DiscoveryReviewResultError(f"{key} must be a non-empty string")
    contradictions = result.get("contradictions")
    if (
        not isinstance(contradictions, list)
        or len(contradictions) > 50
        or any(not isinstance(value, str) for value in contradictions)
    ):
        raise DiscoveryReviewResultError(
            "contradictions must be a bounded list of strings"
        )
    next_bet = result.get("next_bet")
    if next_bet is not None and not isinstance(next_bet, dict):
        raise DiscoveryReviewResultError("next_bet must be an object or null")
    recommended_phase = result.get("recommended_phase")
    if recommended_phase is not None and recommended_phase not in _VALID_PHASES:
        raise DiscoveryReviewResultError("recommended_phase is invalid")
    recommended_limits = result.get("recommended_limits")
    if not isinstance(recommended_limits, dict) or set(recommended_limits) != set(
        _RECOMMENDED_LIMIT_KEYS
    ):
        raise DiscoveryReviewResultError(
            "recommended_limits must contain exactly the three cycle limit keys"
        )
    for key, value in recommended_limits.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise DiscoveryReviewResultError(
                f"recommended_limits.{key} must be a positive integer or null"
            )
    operator_question = result.get("operator_question")
    if operator_question is not None and (
        not isinstance(operator_question, str) or not operator_question.strip()
    ):
        raise DiscoveryReviewResultError(
            "operator_question must be a non-empty string or null"
        )
    if verdict == "ask_operator" and not operator_question:
        raise DiscoveryReviewResultError(
            "ask_operator verdict requires operator_question"
        )

    evidence_ids = result.get("evidence_citations")
    if (
        not isinstance(evidence_ids, list)
        or len(evidence_ids) > DISCOVERY_REVIEW_MAX_PACKET_ITEMS
    ):
        raise DiscoveryReviewResultError(
            "evidence_citations must be a bounded list"
        )
    if not evidence_ids and verdict not in {"inconclusive", "ask_operator"}:
        raise DiscoveryReviewResultError(
            "substantive verdicts require at least one evidence citation"
        )
    normalized_ids: list[str] = []
    for value in evidence_ids:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise DiscoveryReviewResultError(
                "evidence_citations contains an invalid UUID"
            ) from None
        if normalized not in normalized_ids:
            normalized_ids.append(normalized)
    result["evidence_citations"] = normalized_ids
    result["confidence"] = float(confidence)
    return result


async def _emit_review_event(
    session,
    *,
    cycle: DiscoveryCycle,
    task_id: uuid.UUID,
    event_type: str,
    summary: str,
    extra: Mapping[str, Any] | None = None,
) -> ProjectMessage | None:
    context = {
        "cycle_id": str(cycle.id),
        "review_task_id": str(task_id),
        "direction_key": cycle.direction_key,
        **dict(extra or {}),
    }
    return await post_event_message(
        session,
        cycle.project_id,
        event_type=event_type,
        summary=summary,
        context=context,
        task_id=task_id,
    )


async def _create_successor_cycle(
    session,
    *,
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
    now: datetime,
) -> DiscoveryCycle:
    """Apply a safe continuation verdict without requiring Resident uptime."""

    limits = dict(result["recommended_limits"])
    next_bet = (
        dict(result["next_bet"])
        if isinstance(result.get("next_bet"), Mapping)
        else dict(cycle.bet_snapshot or {})
    )
    successor = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=cycle.generation + 1,
        outcome_kind=cycle.outcome_kind,
        phase=result.get("recommended_phase") or cycle.phase,
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=next_bet,
        mechanism_fingerprint=_checksum(next_bet),
        task_credit_limit=(
            limits["task_credit_limit"]
            if limits["task_credit_limit"] is not None
            else cycle.task_credit_limit
        ),
        attempt_credit_limit=(
            limits["attempt_credit_limit"]
            if limits["attempt_credit_limit"] is not None
            else cycle.attempt_credit_limit
        ),
        concurrency_limit=(
            limits["concurrency_limit"]
            if limits["concurrency_limit"] is not None
            else cycle.concurrency_limit
        ),
        deadline_at=now + timedelta(hours=24),
    )
    session.add(successor)
    await session.flush()
    return successor


def _decision_proposal(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verdict": result.get("verdict"),
        "next_bet": result.get("next_bet"),
        "recommended_phase": result.get("recommended_phase"),
        "recommended_limits": result.get("recommended_limits"),
    }


async def _spawn_confirmation_review(
    session,
    *,
    cycle: DiscoveryCycle,
    prior_task: Task,
    first_result: Mapping[str, Any],
    now: datetime,
) -> Task:
    """Queue an independent second opinion before a consequential direction change."""

    next_revision = int(cycle.revision or 0) + 1
    payload = dict(prior_task.first_payload)
    payload.update(
        {
            "expected_revision": next_revision,
            "review_round": 2,
            "prior_review_task_id": str(prior_task.id),
            # Round two judges this server-canonical proposal against the same
            # frozen evidence. It can reproduce it exactly to confirm or emit a
            # different decision to reject/escalate.
            "confirmation_proposal": _decision_proposal(first_result),
        }
    )
    context = dict(prior_task.context or {})
    context["prior_review_task_id"] = str(prior_task.id)
    task = Task(
        project_id=cycle.project_id,
        discovery_cycle_id=cycle.id,
        title=f"Independent confirmation: {cycle.direction_key}",
        description=(
            "Independently judge the frozen discovery evidence. A consequential "
            "direction change requires two matching verdicts."
        ),
        parent_task_id=None,
        payloads=[payload],
        context=context,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        idempotency_key=f"discovery-review:{cycle.id}:round:2",
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "DISCOVERY_REVIEW_CONFIRMATION",
                "cycle_id": str(cycle.id),
                "prior_review_task_id": str(prior_task.id),
                "packet_checksum": payload.get("packet_checksum"),
            },
        )
    )
    await session.flush()
    cycle.review_task_id = task.id
    cycle.review_result = {
        "first_review": dict(first_result),
        "confirmation_task_id": str(task.id),
    }
    cycle.revision = next_revision
    cycle.updated_at = now
    session.add(cycle)
    return task


def _continuation_block_reason(
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
) -> str | None:
    if cycle.generation >= MAX_AUTONOMOUS_DISCOVERY_GENERATIONS:
        return "autonomous_generation_cap_reached"
    next_bet = result.get("next_bet")
    if not isinstance(next_bet, Mapping) or not next_bet:
        return "next_bet_required_for_autonomous_continuation"
    return None


async def complete_discovery_review(
    session,
    *,
    task: Task,
    raw_result: str | Mapping[str, Any],
) -> DiscoveryCycle:
    """Apply a valid, still-current verdict or fail closed as review_blocked."""

    if task.discovery_cycle_id is None:
        raise DiscoveryReviewResultError("review task has no discovery cycle")
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == task.discovery_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise DiscoveryReviewResultError("discovery cycle no longer exists")
    if cycle.review_task_id != task.id:
        raise DiscoveryReviewResultError("review task is stale for this cycle")
    if cycle.state == "reviewed" and cycle.review_result is not None:
        # Post-commit replay: the verdict and notification already landed.
        return cycle

    payload = task.first_payload
    expected_revision = payload.get("expected_revision")
    stale_reason: str | None = None
    if cycle.state != "reviewing":
        stale_reason = f"cycle is {cycle.state}, not reviewing"
    elif not isinstance(expected_revision, int) or expected_revision != cycle.revision:
        stale_reason = "cycle revision changed while review was running"

    try:
        result = parse_discovery_review_result(raw_result)
        if stale_reason:
            raise DiscoveryReviewResultError(stale_reason)
        if result["cycle_id"] != str(cycle.id):
            raise DiscoveryReviewResultError(
                "review cycle_id does not match the task cycle"
            )
        if result["reviewed_revision"] != expected_revision:
            raise DiscoveryReviewResultError(
                "reviewed_revision does not match the reviewed cycle snapshot"
            )
        packet_manifest = payload.get("evidence_packet_manifest")
        if (
            isinstance(packet_manifest, Mapping)
            and packet_manifest.get("required_evidence_omitted") is True
            and result["verdict"] not in {"inconclusive", "ask_operator"}
        ):
            raise DiscoveryReviewResultError(
                "incomplete packet permits only inconclusive or ask_operator"
            )
        payload_candidate_sha = payload.get("candidate_sha")
        if cycle.candidate_sha and payload_candidate_sha != cycle.candidate_sha:
            raise DiscoveryReviewResultError(
                "task candidate_sha does not match the pinned cycle candidate"
            )
        cited_ids = {str(uuid.UUID(value)) for value in result["evidence_citations"]}
        packet_ids = {
            str(item.get("evidence_id"))
            for item in payload.get("evidence_packet", [])
            if isinstance(item, Mapping) and item.get("evidence_id")
        }
        if not cited_ids.issubset(packet_ids):
            raise DiscoveryReviewResultError(
                "review cited evidence outside its authorized packet"
            )
        review_round = payload.get("review_round", 1)
        if review_round not in {1, 2}:
            raise DiscoveryReviewResultError("review_round must be one or two")
        first_result: Mapping[str, Any] | None = None
        if review_round == 2:
            stored = cycle.review_result or {}
            candidate = (
                stored.get("first_review")
                if isinstance(stored, Mapping)
                else None
            )
            if not isinstance(candidate, Mapping):
                raise DiscoveryReviewResultError(
                    "confirmation review has no durable first-review result"
                )
            first_result = candidate
            proposal = payload.get("confirmation_proposal")
            if (
                not isinstance(proposal, Mapping)
                or _canonical_json(proposal)
                != _canonical_json(_decision_proposal(first_result))
            ):
                raise DiscoveryReviewResultError(
                    "confirmation proposal does not match the durable first review"
                )
    except DiscoveryReviewResultError as exc:
        if cycle.state == "reviewing":
            cycle.state = "review_blocked"
            cycle.review_result = {
                "error": str(exc),
                "review_task_id": str(task.id),
                "expected_revision": expected_revision,
            }
            cycle.revision = int(cycle.revision or 0) + 1
            cycle.updated_at = _utcnow()
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.review_blocked",
                summary=(
                    f"Discovery review blocked for {cycle.direction_key}: {str(exc)[:240]}"
                ),
                extra={"reason": str(exc)[:500]},
            )
        raise

    verdict = result["verdict"]
    if review_round == 1 and verdict in _CONSEQUENTIAL_VERDICTS:
        confirmation = await _spawn_confirmation_review(
            session,
            cycle=cycle,
            prior_task=task,
            first_result=result,
            now=_utcnow(),
        )
        await _emit_review_event(
            session,
            cycle=cycle,
            task_id=task.id,
            event_type="discovery.review_completed",
            summary=(
                f"Discovery review for {cycle.direction_key}: {verdict}; "
                "independent confirmation queued."
            ),
            extra={
                "verdict": verdict,
                "confidence": result["confidence"],
                "review_result": result,
                "confirmation_task_id": str(confirmation.id),
                "policy_status": "confirmation_queued",
            },
        )
        return cycle

    if review_round == 2:
        assert first_result is not None
        first_decision = _decision_proposal(first_result)
        confirmation_decision = _decision_proposal(result)
        if _canonical_json(first_decision) != _canonical_json(
            confirmation_decision
        ):
            cycle.state = "review_blocked"
            cycle.verdict = "ask_operator"
            cycle.review_result = {
                "first_review": dict(first_result),
                "confirmation_review": result,
                "error": "independent reviews disagree",
            }
            cycle.reviewed_at = _utcnow()
            cycle.updated_at = cycle.reviewed_at
            cycle.revision = int(cycle.revision or 0) + 1
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.operator_question",
                summary=(
                    f"Discovery reviews disagree for {cycle.direction_key}: "
                    "their proposed decisions differ."
                ),
                extra={
                    "first_review": dict(first_result),
                    "confirmation_review": result,
                    "policy_status": "operator_required_review_disagreement",
                },
            )
            return cycle

    cycle.verdict = verdict
    cycle.review_result = (
        {
            "first_review": dict(first_result),
            "confirmation_review": result,
        }
        if first_result is not None
        else result
    )
    cycle.reviewed_at = _utcnow()
    cycle.updated_at = cycle.reviewed_at
    cycle.state = "reviewed"
    cycle.revision = int(cycle.revision or 0) + 1
    session.add(cycle)
    await session.flush()

    successor: DiscoveryCycle | None = None
    continuation_requested = (
        verdict in _AUTONOMOUS_CONTINUATION_VERDICTS
        or (review_round == 2 and verdict in _CONSEQUENTIAL_VERDICTS)
    )
    continuation_block_reason = (
        _continuation_block_reason(cycle, result)
        if continuation_requested
        else None
    )
    if continuation_requested and continuation_block_reason is None:
        successor = await _create_successor_cycle(
            session,
            cycle=cycle,
            result=result,
            now=cycle.reviewed_at,
        )

    event_type = "discovery.review_completed"
    if (
        verdict in {"ask_operator", "stop", "park"}
        or continuation_block_reason is not None
    ):
        event_type = "discovery.operator_question"
    await _emit_review_event(
        session,
        cycle=cycle,
        task_id=task.id,
        event_type=event_type,
        summary=(
            f"Discovery review for {cycle.direction_key}: {verdict} "
            f"({result['decision_summary'][:240]})"
        ),
        extra={
            "verdict": verdict,
            "confidence": result["confidence"],
            "review_result": cycle.review_result,
            "next_cycle_id": str(successor.id) if successor is not None else None,
            "policy_status": (
                "continued"
                if successor is not None
                else (
                    continuation_block_reason
                    or (
                        "operator_notified"
                        if verdict in {"ask_operator", "stop", "park"}
                        else "halted"
                    )
                )
            ),
        },
    )
    return cycle


async def block_failed_discovery_review(
    session,
    *,
    task: Task,
    reason: str,
) -> bool:
    """Fail closed after the normal task exhausts its own retries."""

    if task.discovery_cycle_id is None:
        return False
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == task.discovery_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        cycle is None
        or cycle.review_task_id != task.id
        or cycle.state != "reviewing"
    ):
        return False
    cycle.state = "review_blocked"
    cycle.review_result = {
        "error": reason[:2_000],
        "review_task_id": str(task.id),
        "task_status": str(_enum_value(task.status)),
    }
    cycle.revision = int(cycle.revision or 0) + 1
    cycle.updated_at = _utcnow()
    session.add(cycle)
    await _emit_review_event(
        session,
        cycle=cycle,
        task_id=task.id,
        event_type="discovery.review_blocked",
        summary=f"Discovery review blocked for {cycle.direction_key}: {reason[:240]}",
        extra={"reason": reason[:500]},
    )
    return True


async def _handle_discovery_review_completion(
    task: Task,
    logs: str,
    session,
) -> None:
    """Task-lifecycle adapter for a completed normal review task.

    The agent handler already performs one focused JSON repair.  Invalid output
    here is therefore a terminal review outcome, not a reason to reinterpret
    prose or fan out more work.
    """

    try:
        await complete_discovery_review(
            session,
            task=task,
            raw_result=logs,
        )
    except DiscoveryReviewResultError:
        logger.warning(
            "[DISCOVERY_REVIEW] Review task %s failed closed",
            task.id,
            exc_info=True,
        )


async def sweep_discovery_reviews(
    session,
    now: datetime | None = None,
    *,
    limit: int = DISCOVERY_REVIEW_SWEEP_LIMIT,
) -> int:
    """Bounded missed-trigger repair for expired or quiescent cycles."""

    now = now or _utcnow()
    cycle_ids = list(
        (
            await session.execute(
                select(DiscoveryCycle.id)
                .where(DiscoveryCycle.state.in_(_SPAWNABLE_CYCLE_STATES))
                .order_by(DiscoveryCycle.updated_at.asc(), DiscoveryCycle.id.asc())
                .limit(max(1, min(limit, DISCOVERY_REVIEW_SWEEP_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for cycle_id in cycle_ids:
        try:
            async with session.begin_nested():
                result = await maybe_close_and_spawn_discovery_review(
                    session, cycle_id, now=now
                )
            if result.spawned or result.reason in {
                "cohort_active",
                "cohort_blocked_in_grace",
                "spawn_requests_in_grace",
                "source_research_task_active",
            }:
                changed += 1
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Discovery-review sweep skipped malformed cycle %s",
                cycle_id,
            )
    return changed
