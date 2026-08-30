"""Bounded release-window Strategic spawn receipts for coding agents."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.acceptance_criteria import CRITERION_RELEASE_VERSION_CONTEXT_KEY
from app.agent_access_redaction import redact_agent_text
from app.auth import get_current_user
from app.database import get_session
from app.models import DiscoveryCycle, StrategicSpawnRequest, Task
from app.release_models import ReleaseVersion, VersionAcceptanceCriterion
from app.routers.agent_access import _get_user_project
from app.strategic_spawn_requests import stable_replay_specification_hash
from app.time_utils import utc_iso


router = APIRouter(prefix="/api/agent-access/v1", tags=["agent-access"])

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CRITERION_RE = re.compile(r"^c-[0-9a-f]{8}$")
_CRITERION_LIMIT = 50
_RECOGNIZED_CREATED_BY = {"STRATEGIC_AGENT"}
_RECOGNIZED_ORIGINATION = {"STRATEGIST"}
_RECOGNIZED_PARENT_CREATED_BY = {"ORCHESTRATOR"}
_RECOGNIZED_PARENT_TYPES = {"strategic_review"}


def _iso(value: datetime | None) -> str | None:
    return utc_iso(value)


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_agent_text(value[:limit])


def _version_classification(value: Any, expected: uuid.UUID) -> tuple[bool, str]:
    if value is None:
        return False, "missing"
    if not isinstance(value, str):
        return False, "invalid"
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False, "invalid"
    return (True, "match") if parsed == expected else (False, "mismatch")


def _uuid_reference_classification(
    value: Any,
    expected: uuid.UUID | None,
) -> tuple[bool, str]:
    if value is None:
        return False, "missing"
    if not isinstance(value, str):
        return False, "invalid"
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False, "invalid"
    if expected is None:
        return False, "unvalidated"
    return (True, "match") if parsed == expected else (False, "mismatch")


def _uuid_pair_classification(left: Any, right: Any) -> tuple[bool, str]:
    if left is None or right is None:
        return False, "missing"
    if not isinstance(left, str) or not isinstance(right, str):
        return False, "invalid"
    try:
        left_uuid = uuid.UUID(left)
        right_uuid = uuid.UUID(right)
    except (TypeError, ValueError, AttributeError):
        return False, "invalid"
    return (True, "match") if left_uuid == right_uuid else (False, "mismatch")


def _uuid_list(value: Any) -> list[uuid.UUID] | None:
    if not isinstance(value, list):
        return None
    parsed: list[uuid.UUID] = []
    try:
        for item in value:
            if not isinstance(item, str):
                return None
            parsed.append(uuid.UUID(item))
    except (TypeError, ValueError, AttributeError):
        return None
    if len(set(parsed)) != len(parsed):
        return None
    return parsed


def _criterion_projection(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "available": False,
            "ids": [],
            "total": 0,
            "returned": 0,
            "invalid_count": 0,
            "invalid": False,
            "truncated": False,
        }
    if not isinstance(value, list):
        return {
            "available": True,
            "ids": [],
            "total": 0,
            "returned": 0,
            "invalid_count": 1,
            "invalid": True,
            "truncated": False,
        }
    valid = [
        item
        for item in value
        if isinstance(item, str) and _CRITERION_RE.fullmatch(item)
    ]
    unique_valid = list(dict.fromkeys(valid))
    invalid = len(unique_valid) != len(value)
    total = len(value)
    ids = unique_valid[:_CRITERION_LIMIT]
    return {
        "available": True,
        "ids": ids,
        "total": total,
        "returned": len(ids),
        "invalid_count": total - len(unique_valid),
        "invalid": invalid,
        "truncated": total > _CRITERION_LIMIT,
    }


def _row_projection(
    row: Any,
    *,
    version_id: uuid.UUID,
    version_opened_at: datetime,
    successor_opened_at: datetime | None,
    active_criterion_keys: frozenset[str],
) -> dict[str, Any]:
    declared_match, declared_state = _version_classification(
        row.declared_version_id, version_id
    )
    task_match, task_state = _version_classification(
        row.task_version_id, version_id
    )
    task_created_in_window = bool(
        row.task_created_at is not None
        and row.task_created_at >= version_opened_at
        and (
            successor_opened_at is None
            or row.task_created_at < successor_opened_at
        )
    )
    criteria = _criterion_projection(row.criterion_ids)
    declared_criteria = _criterion_projection(row.declared_criterion_ids)
    criteria_declaration_match = bool(
        criteria["available"]
        and declared_criteria["available"]
        and not criteria["invalid"]
        and not declared_criteria["invalid"]
        and not criteria["truncated"]
        and not declared_criteria["truncated"]
        and criteria["ids"] == declared_criteria["ids"]
        and criteria["total"] == declared_criteria["total"]
    )
    canonical_criterion_ids = [
        key for key in criteria["ids"] if key in active_criterion_keys
    ]
    criteria_canonical = bool(
        criteria["available"]
        and not criteria["invalid"]
        and not criteria["truncated"]
        and criteria["total"] > 0
        and len(canonical_criterion_ids) == criteria["total"]
    )
    declared_parent_match, declared_parent_state = _uuid_reference_classification(
        row.declared_parent_task_id,
        row.validated_parent_task_id,
    )
    task_parent_match, task_parent_state = _uuid_reference_classification(
        row.task_parent_task_id,
        row.validated_parent_task_id,
    )
    workday_packet_match, workday_packet_state = _uuid_pair_classification(
        row.declared_workday_packet_id,
        row.task_workday_packet_id,
    )
    workday_claim_match, workday_claim_state = _uuid_pair_classification(
        row.declared_workday_claim_id,
        row.task_workday_claim_id,
    )
    workday_provenance_match = workday_packet_match and workday_claim_match
    declared_dependencies = _uuid_list(row.declared_depends_on)
    task_dependencies = _uuid_list(row.task_depends_on)
    dependency_declaration_match = bool(
        declared_dependencies is not None
        and task_dependencies is not None
        and declared_dependencies == task_dependencies
    )
    discovery_cycle_match = bool(
        (
            not row.request_discovery_cycle_present
            and row.task_discovery_cycle_id is None
        )
        or (
            row.request_discovery_cycle_present
            and row.validated_discovery_cycle_id is not None
            and row.task_discovery_cycle_id == row.validated_discovery_cycle_id
        )
    )
    validated_parent = row.validated_parent_task_id
    child_is_distinct = bool(
        row.validated_task_id is not None
        and validated_parent is not None
        and row.validated_task_id != validated_parent
    )
    hash_format_valid = bool(
        isinstance(row.specification_hash, str)
        and _HASH_RE.fullmatch(row.specification_hash)
    )
    hash_matches_replay_specification = bool(
        hash_format_valid
        and isinstance(row.specification, dict)
        and stable_replay_specification_hash(row.specification)
        == row.specification_hash
    )
    created_by = (
        row.created_by if row.created_by in _RECOGNIZED_CREATED_BY else None
    )
    origination = (
        row.origination if row.origination in _RECOGNIZED_ORIGINATION else None
    )
    declared_strategic = row.declared_spawned_by == "strategic_direct"
    task_strategic = row.task_spawned_by == "strategic_direct"
    parent_type = (
        row.parent_task_type
        if row.parent_task_type in _RECOGNIZED_PARENT_TYPES
        else None
    )
    task_type_match = bool(
        isinstance(row.task_type, str)
        and row.task_type
        and row.task_type == row.child_task_type
    )
    timestamp_order_valid = bool(
        row.dispatched_at is not None
        and row.request_created_at is not None
        and row.request_updated_at is not None
        and row.request_created_at <= row.dispatched_at <= row.request_updated_at
    )
    dispatch_timestamp_state = (
        "present"
        if timestamp_order_valid
        else (
            "active_work_deduplicated_unstamped"
            if row.dispatched_at is None
            and row.admission_reason == "active_work_deduplicated"
            else (
                "missing_invalid"
                if row.dispatched_at is None
                else "invalid_order"
            )
        )
    )
    dispatch_receipt_consistent = bool(
        row.state == "dispatched"
        and row.request_task_present
        and row.validated_task_id is not None
        and dispatch_timestamp_state
        in {"present", "active_work_deduplicated_unstamped"}
    )
    new_child_dispatch = bool(
        dispatch_receipt_consistent
        and row.admission_reason == "immediate"
        and row.dispatched_at is not None
        and row.task_created_at is not None
        and row.request_created_at <= row.task_created_at <= row.dispatched_at
    )
    evidence_complete = bool(
        dispatch_receipt_consistent
        and hash_matches_replay_specification
        and declared_match
        and task_match
        and task_created_in_window
        and task_type_match
        and declared_strategic
        and task_strategic
        and created_by == "STRATEGIC_AGENT"
        and origination == "STRATEGIST"
        and criteria["available"]
        and not criteria["invalid"]
        and not criteria["truncated"]
        and criteria_declaration_match
        and criteria_canonical
        and dependency_declaration_match
        and workday_provenance_match
        and discovery_cycle_match
        and row.request_parent_present
        and validated_parent is not None
        and row.parent_created_by in _RECOGNIZED_PARENT_CREATED_BY
        and parent_type == "strategic_review"
        and declared_parent_match
        and task_parent_match
        and child_is_distinct
    )
    return {
        "request_id": str(row.request_id),
        "specification_hash": (
            row.specification_hash
            if hash_matches_replay_specification
            else None
        ),
        "specification_hash_format_valid": hash_format_valid,
        "specification_hash_matches_replay_specification": (
            hash_matches_replay_specification
        ),
        "task_type": _safe_text(row.task_type, 64),
        "state": _safe_text(row.state, 32),
        "admission_reason": _safe_text(row.admission_reason, 255),
        "direction_id": _safe_text(row.direction_id, 128),
        "parent_task_id": str(validated_parent) if validated_parent else None,
        "task_id": (
            str(row.validated_task_id)
            if row.validated_task_id is not None
            else None
        ),
        "discovery_cycle_id": (
            str(row.validated_discovery_cycle_id)
            if row.validated_discovery_cycle_id is not None
            else None
        ),
        "not_before": _iso(row.not_before),
        "created_at": _iso(row.request_created_at),
        "updated_at": _iso(row.request_updated_at),
        "dispatched_at": _iso(row.dispatched_at),
        "dispatch_timestamp_state": dispatch_timestamp_state,
        "dispatch_receipt_consistent": dispatch_receipt_consistent,
        "new_child_dispatch": new_child_dispatch,
        "declared_release_version_id": str(version_id) if declared_match else None,
        "declared_release_version_match": declared_match,
        "declared_release_version_state": declared_state,
        "task_release_version_id": str(version_id) if task_match else None,
        "task_release_version_match": task_match,
        "task_release_version_state": task_state,
        "task_created_within_release_window": task_created_in_window,
        "workday_provenance_match": workday_provenance_match,
        "workday_packet_state": workday_packet_state,
        "workday_claim_state": workday_claim_state,
        "dependency_declaration_match": dependency_declaration_match,
        "dependency_count": (
            len(task_dependencies) if task_dependencies is not None else None
        ),
        "discovery_cycle_link_match": discovery_cycle_match,
        "declared_parent_link_match": declared_parent_match,
        "declared_parent_link_state": declared_parent_state,
        "task_parent_link_match": task_parent_match,
        "task_parent_link_state": task_parent_state,
        "child_distinct_from_parent": child_is_distinct,
        "parent_created_by_orchestrator": (
            row.parent_created_by == "ORCHESTRATOR"
        ),
        "parent_task_type": parent_type,
        "parent_task_type_recognized": parent_type == "strategic_review",
        "request_task_type_matches_child": task_type_match,
        "declared_spawned_by_strategic_direct": declared_strategic,
        "task_spawned_by_strategic_direct": task_strategic,
        "created_by": created_by,
        "created_by_strategic_agent": created_by == "STRATEGIC_AGENT",
        "origination": origination,
        "origination_strategist": origination == "STRATEGIST",
        "acceptance_criteria": {
            **criteria,
            "ids": canonical_criterion_ids,
            "returned": len(canonical_criterion_ids),
            "declared_match": criteria_declaration_match,
            "canonical_active_match": criteria_canonical,
        },
        "task_status": (
            getattr(row.task_status, "value", row.task_status)
            if row.task_status is not None
            else None
        ),
        "task_created_at": _iso(row.task_created_at),
        "task_updated_at": _iso(row.task_updated_at),
        "task_started_at": _iso(row.task_started_at),
        "task_completed_at": _iso(row.task_completed_at),
        "evidence_complete": evidence_complete,
        "new_child_evidence_complete": evidence_complete and new_child_dispatch,
    }


@router.get(
    "/projects/{project_id}/release-versions/{version_id}/strategic-spawn-requests"
)
async def list_release_strategic_spawn_requests(
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return one snapshot-consistent release-window provenance projection."""
    await _get_user_project(project_id, current_user["user_id"], session)
    SuccessorVersion = aliased(ReleaseVersion, name="release_spawn_successor")
    ChildTask = aliased(Task, name="spawned_task")
    ParentTask = aliased(Task, name="parent_strategic_task")
    successor_opened_at = (
        select(SuccessorVersion.opened_at)
        .where(
            SuccessorVersion.project_id == ReleaseVersion.project_id,
            SuccessorVersion.version_number > ReleaseVersion.version_number,
        )
        .order_by(SuccessorVersion.version_number.asc())
        .limit(1)
        .correlate(ReleaseVersion)
        .scalar_subquery()
    )
    release_window = (
        select(
            ReleaseVersion.id.label("version_id"),
            ReleaseVersion.project_id.label("project_id"),
            ReleaseVersion.version_number.label("version_number"),
            ReleaseVersion.opened_at.label("version_opened_at"),
            successor_opened_at.label("window_end"),
        )
        .where(
            ReleaseVersion.id == version_id,
            ReleaseVersion.project_id == project_id,
        )
        .cte("release_spawn_window")
    )
    evidence = (
        select(
            StrategicSpawnRequest.id.label("request_id"),
            StrategicSpawnRequest.specification_hash,
            StrategicSpawnRequest.specification,
            StrategicSpawnRequest.task_type,
            StrategicSpawnRequest.state,
            StrategicSpawnRequest.admission_reason,
            StrategicSpawnRequest.direction_id,
            StrategicSpawnRequest.parent_task_id.is_not(None).label(
                "request_parent_present"
            ),
            StrategicSpawnRequest.task_id.is_not(None).label(
                "request_task_present"
            ),
            StrategicSpawnRequest.discovery_cycle_id.is_not(None).label(
                "request_discovery_cycle_present"
            ),
            ParentTask.id.label("validated_parent_task_id"),
            ChildTask.id.label("validated_task_id"),
            DiscoveryCycle.id.label("validated_discovery_cycle_id"),
            StrategicSpawnRequest.not_before,
            StrategicSpawnRequest.created_at.label("request_created_at"),
            StrategicSpawnRequest.updated_at.label("request_updated_at"),
            StrategicSpawnRequest.dispatched_at,
            StrategicSpawnRequest.specification["task"]["context"][
                CRITERION_RELEASE_VERSION_CONTEXT_KEY
            ].as_string().label("declared_version_id"),
            StrategicSpawnRequest.specification["task"]["context"][
                "spawned_by"
            ].as_string().label("declared_spawned_by"),
            StrategicSpawnRequest.specification["task"]["context"][
                "parent_strategic_task_id"
            ].as_string().label("declared_parent_task_id"),
            StrategicSpawnRequest.specification["task"]["criterion_ids"].label(
                "declared_criterion_ids"
            ),
            StrategicSpawnRequest.specification["task"]["depends_on"].label(
                "declared_depends_on"
            ),
            StrategicSpawnRequest.specification["task"]["context"][
                "_workday_consultation_provenance"
            ]["packet_id"].as_string().label("declared_workday_packet_id"),
            StrategicSpawnRequest.specification["task"]["context"][
                "_workday_consultation_provenance"
            ]["workday_claim_id"].as_string().label("declared_workday_claim_id"),
            ChildTask.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY]
            .as_string()
            .label("task_version_id"),
            ChildTask.context["spawned_by"].as_string().label("task_spawned_by"),
            ChildTask.context["parent_strategic_task_id"]
            .as_string()
            .label("task_parent_task_id"),
            ChildTask.context["_workday_consultation_provenance"]["packet_id"]
            .as_string()
            .label("task_workday_packet_id"),
            ChildTask.context["_workday_consultation_provenance"][
                "workday_claim_id"
            ].as_string().label("task_workday_claim_id"),
            ChildTask.payloads[0]["type"]
            .as_string()
            .label("child_task_type"),
            ChildTask.criterion_ids,
            ChildTask.depends_on.label("task_depends_on"),
            ChildTask.discovery_cycle_id.label("task_discovery_cycle_id"),
            ChildTask.status.label("task_status"),
            ChildTask.created_by,
            ChildTask.origination,
            ChildTask.created_at.label("task_created_at"),
            ChildTask.updated_at.label("task_updated_at"),
            ChildTask.started_at.label("task_started_at"),
            ChildTask.completed_at.label("task_completed_at"),
            ParentTask.created_by.label("parent_created_by"),
            ParentTask.payloads[0]["type"]
            .as_string()
            .label("parent_task_type"),
        )
        .select_from(release_window)
        .join(
            StrategicSpawnRequest,
            and_(
                StrategicSpawnRequest.project_id == release_window.c.project_id,
                StrategicSpawnRequest.created_at
                >= release_window.c.version_opened_at,
                or_(
                    release_window.c.window_end.is_(None),
                    StrategicSpawnRequest.created_at < release_window.c.window_end,
                ),
            ),
        )
        .outerjoin(
            ChildTask,
            and_(
                ChildTask.id == StrategicSpawnRequest.task_id,
                ChildTask.project_id == release_window.c.project_id,
            ),
        )
        .outerjoin(
            ParentTask,
            and_(
                ParentTask.id == StrategicSpawnRequest.parent_task_id,
                ParentTask.project_id == release_window.c.project_id,
            ),
        )
        .outerjoin(
            DiscoveryCycle,
            and_(
                DiscoveryCycle.id == StrategicSpawnRequest.discovery_cycle_id,
                DiscoveryCycle.project_id == release_window.c.project_id,
            ),
        )
        .cte("release_spawn_evidence")
    )
    stats = (
        select(
            func.count(evidence.c.request_id).label("request_total"),
            func.count(evidence.c.validated_task_id).label(
                "validated_task_total"
            ),
            func.count(func.distinct(evidence.c.validated_task_id)).label(
                "distinct_task_total"
            ),
        )
        .select_from(evidence)
        .cte("release_spawn_stats")
    )
    criterion_catalog = (
        select(
            func.array_agg(VersionAcceptanceCriterion.criterion_key).label(
                "active_criterion_keys"
            )
        )
        .select_from(release_window)
        .outerjoin(
            VersionAcceptanceCriterion,
            and_(
                VersionAcceptanceCriterion.project_id
                == release_window.c.project_id,
                VersionAcceptanceCriterion.release_version_id
                == release_window.c.version_id,
                VersionAcceptanceCriterion.status == "active",
            ),
        )
        .cte("release_spawn_criterion_catalog")
    )
    page = (
        select(*evidence.c)
        .order_by(
            evidence.c.request_created_at.asc(),
            evidence.c.request_id.asc(),
        )
        .offset(offset)
        .limit(limit + 1)
        .cte("release_spawn_page")
    )
    stmt = (
        select(
            release_window.c.version_id,
            release_window.c.version_number,
            release_window.c.version_opened_at,
            release_window.c.window_end,
            stats.c.request_total,
            stats.c.validated_task_total,
            stats.c.distinct_task_total,
            criterion_catalog.c.active_criterion_keys,
            *page.c,
        )
        .select_from(release_window)
        .join(stats, true())
        .join(criterion_catalog, true())
        .outerjoin(page, true())
        .order_by(
            page.c.request_created_at.asc().nulls_last(),
            page.c.request_id.asc().nulls_last(),
        )
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Release version not found")

    snapshot = rows[0]
    successor_opened_at_value = snapshot.window_end
    page_rows = [row for row in rows if row.request_id is not None]
    has_more = len(page_rows) > limit
    page_rows = page_rows[:limit]
    active_criterion_keys = frozenset(
        key
        for key in (snapshot.active_criterion_keys or [])
        if isinstance(key, str)
    )
    requests = [
        _row_projection(
            row,
            version_id=snapshot.version_id,
            version_opened_at=snapshot.version_opened_at,
            successor_opened_at=successor_opened_at_value,
            active_criterion_keys=active_criterion_keys,
        )
        for row in page_rows
    ]
    total = int(snapshot.request_total or 0)
    validated_task_total = int(snapshot.validated_task_total or 0)
    distinct_task_total = int(snapshot.distinct_task_total or 0)
    returned_task_ids = [
        row["task_id"] for row in requests if row["task_id"] is not None
    ]
    global_cardinality_complete = bool(
        total == validated_task_total == distinct_task_total
    )
    complete = bool(
        offset == 0
        and not has_more
        and len(requests) == total
        and global_cardinality_complete
        and len(returned_task_ids) == len(requests)
        and len(set(returned_task_ids)) == len(returned_task_ids)
        and all(row["evidence_complete"] for row in requests)
    )
    dependency_chain_complete = False
    if complete and len(page_rows) == 2:
        task_ids = {row.validated_task_id for row in page_rows}
        roots = [
            row
            for row in page_rows
            if _uuid_list(row.task_depends_on) == []
        ]
        children = [
            row
            for row in page_rows
            if (
                (dependencies := _uuid_list(row.task_depends_on)) is not None
                and len(dependencies) == 1
                and dependencies[0] in task_ids
                and dependencies[0] != row.validated_task_id
            )
        ]
        dependency_chain_complete = bool(
            len(roots) == 1
            and len(children) == 1
            and _uuid_list(children[0].task_depends_on)
            == [roots[0].validated_task_id]
        )
    return {
        "schema_version": "blueprint.agent-release-spawn-requests.v1",
        "project_id": str(project_id),
        "release_version": {
            "id": str(snapshot.version_id),
            "version_number": snapshot.version_number,
            "opened_at": _iso(snapshot.version_opened_at),
            "window_end": _iso(successor_opened_at_value),
        },
        "requests": requests,
        "total": total,
        "validated_task_total": validated_task_total,
        "distinct_task_total": distinct_task_total,
        "all_child_tasks_distinct": global_cardinality_complete,
        "two_task_dependency_chain_complete": dependency_chain_complete,
        "limit": limit,
        "offset": offset,
        "returned": len(requests),
        "has_more": has_more,
        "next_offset": offset + len(requests) if has_more else None,
        "complete": complete,
        "two_task_spawn_chain_complete": bool(
            complete
            and total == 2
            and dependency_chain_complete
            and all(row["new_child_evidence_complete"] for row in requests)
        ),
    }
