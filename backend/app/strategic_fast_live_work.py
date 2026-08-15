"""Bounded scalar claim-time live-work authority for Strategic Fast replies."""

from __future__ import annotations

import hashlib
import re
import uuid
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from sqlalchemy import case, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    KnowledgeDocument,
    Project,
    ReleaseDeployArtifact,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.strategic_memory_sources import canonical_json_bytes

_FAST_LIVE_WORK_LIMIT = 12
_FAST_LIVE_WORK_TEXT_LIMIT = 512
_FAST_RECENT_TERMINAL_TASK_LIMIT = 6
_FAST_RECENT_TERMINAL_TASK_TEXT_LIMIT = 256
_FAST_RELEASE_VERSION_LIMIT = 4
_FAST_RESULT_EVIDENCE_TEXT_LIMIT = 20_000
_FAST_RESULT_EVIDENCE_UUID_LIMIT = 8
_FAST_RESULT_EVIDENCE_SHA_LIMIT = 8
_FAST_RESULT_CORRELATED_RELEASE_LIMIT = 8

_FAST_RESULT_UUID_PATTERN = re.compile(
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)
_FAST_RESULT_SHA_PATTERN = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])",
    re.IGNORECASE,
)

_FAST_LIVE_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)
_FAST_RECENT_TERMINAL_TASK_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)
_FAST_TERMINAL_DIRECTION_STATUSES = frozenset(
    {"completed", "rejected", "retired", "cancelled", "expired", "superseded"}
)


def _bounded_live_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:_FAST_LIVE_WORK_TEXT_LIMIT]


def _meta(items: list[Any], has_more: bool) -> dict[str, Any]:
    return {"returned_count": len(items), "has_more": has_more}


def _bounded_result_evidence_refs(
    value: Any,
) -> tuple[list[uuid.UUID], list[str], bool, bool]:
    """Extract bounded correlation candidates; never infer state from prose."""

    raw_text = str(value or "")
    text_was_truncated = len(raw_text) > _FAST_RESULT_EVIDENCE_TEXT_LIMIT
    text = raw_text[:_FAST_RESULT_EVIDENCE_TEXT_LIMIT]
    task_or_release_ids: list[uuid.UUID] = []
    seen_ids: set[uuid.UUID] = set()
    uuid_has_more = text_was_truncated
    for match in _FAST_RESULT_UUID_PATTERN.finditer(text):
        candidate = uuid.UUID(match.group(0))
        if candidate in seen_ids:
            continue
        seen_ids.add(candidate)
        if len(task_or_release_ids) >= _FAST_RESULT_EVIDENCE_UUID_LIMIT:
            uuid_has_more = True
            break
        task_or_release_ids.append(candidate)

    sha_refs: list[str] = []
    seen_shas: set[str] = set()
    sha_has_more = text_was_truncated
    for match in _FAST_RESULT_SHA_PATTERN.finditer(text):
        candidate = match.group(0).lower()
        if candidate in seen_shas:
            continue
        seen_shas.add(candidate)
        if len(sha_refs) >= _FAST_RESULT_EVIDENCE_SHA_LIMIT:
            sha_has_more = True
            break
        sha_refs.append(candidate)
    return task_or_release_ids, sha_refs, uuid_has_more, sha_has_more


def _not_resident_infrastructure_task():
    """Exclude Resident generation rows without loading ``Task.context``."""

    return func.coalesce(
        Task.context["_resident_session"].as_boolean(),
        False,
    ).is_(False)


def _linked_managed_outcome_order():
    """Rank direction/work-order-linked outcomes ahead of unrelated chatter."""

    direction_id = func.nullif(
        func.trim(Task.context["direction_id"].as_string()),
        "",
    )
    work_order_id = func.nullif(
        func.trim(Task.context["strategic_work_order_id"].as_string()),
        "",
    )
    return case(
        (or_(direction_id.is_not(None), work_order_id.is_not(None)), 0),
        else_=1,
    )


def _fast_live_task_statement(
    *,
    project_id: uuid.UUID,
    resident_task_id: uuid.UUID,
):
    """Select only bounded task scalars needed by Fast's live-work view.

    ``Task.context`` can be multi-megabyte TOAST data on active tasks. Extract
    the three linkage keys in SQL instead of transferring the complete JSONB
    value on every foreground conversation claim.
    """

    return (
        select(
            Task.id,
            Task.title,
            Task.status,
            Task.created_by,
            Task.origination,
            Task.context["direction_id"].as_string().label("direction_id"),
            Task.context["parent_strategic_task_id"]
            .as_string()
            .label("parent_strategic_task_id"),
            Task.context["strategic_work_order_id"]
            .as_string()
            .label("strategic_work_order_id"),
            Task.latest_status_message,
            Task.blocked_reason,
            Task.recheck_after,
            Task.updated_at,
        )
        .where(
            Task.project_id == project_id,
            Task.id != resident_task_id,
            Task.status.in_(_FAST_LIVE_TASK_STATUSES),
            _not_resident_infrastructure_task(),
        )
        .order_by(Task.updated_at.desc(), Task.id.asc())
        # Fetch one sentinel row so the projection can truthfully say whether
        # more live work exists without counting or transferring every task.
        .limit(_FAST_LIVE_WORK_LIMIT + 1)
    )


def _fast_recent_terminal_task_statement(
    *,
    project_id: uuid.UUID,
    resident_task_id: uuid.UUID,
):
    """Select bounded terminal task scalars without fetching ``Task.context``."""

    return (
        select(
            Task.id,
            Task.title,
            Task.status,
            Task.created_by,
            Task.origination,
            Task.context["direction_id"].as_string().label("direction_id"),
            Task.context["parent_strategic_task_id"]
            .as_string()
            .label("parent_strategic_task_id"),
            Task.context["strategic_work_order_id"]
            .as_string()
            .label("strategic_work_order_id"),
            Task.completed_at,
            Task.updated_at,
        )
        .where(
            Task.project_id == project_id,
            Task.id != resident_task_id,
            Task.status.in_(_FAST_RECENT_TERMINAL_TASK_STATUSES),
            _not_resident_infrastructure_task(),
        )
        .order_by(
            _linked_managed_outcome_order(),
            Task.updated_at.desc(),
            Task.id.asc(),
        )
        .limit(_FAST_RECENT_TERMINAL_TASK_LIMIT + 1)
    )


def _fast_result_correlated_task_statement(
    *,
    project_id: uuid.UUID,
    resident_task_id: uuid.UUID,
    task_ids: list[uuid.UUID],
):
    """Resolve result-mentioned ids to canonical project task scalars."""

    return (
        select(
            Task.id,
            Task.title,
            Task.status,
            Task.created_by,
            Task.origination,
            Task.context["direction_id"].as_string().label("direction_id"),
            Task.context["parent_strategic_task_id"]
            .as_string()
            .label("parent_strategic_task_id"),
            Task.context["strategic_work_order_id"]
            .as_string()
            .label("strategic_work_order_id"),
            Task.latest_status_message,
            Task.blocked_reason,
            Task.recheck_after,
            Task.completed_at,
            Task.updated_at,
        )
        .where(
            Task.project_id == project_id,
            Task.id.in_(task_ids),
            Task.id != resident_task_id,
            _not_resident_infrastructure_task(),
        )
        .order_by(Task.updated_at.desc(), Task.id.asc())
        .limit(_FAST_RESULT_EVIDENCE_UUID_LIMIT)
    )


def _fast_result_correlated_document_statement(
    *,
    project_id: uuid.UUID,
    document_ids: list[uuid.UUID],
):
    """Resolve result-mentioned knowledge ids without loading document bodies."""

    return (
        select(
            KnowledgeDocument.id,
            KnowledgeDocument.source_task_id,
            KnowledgeDocument.source_kind,
            KnowledgeDocument.title,
            KnowledgeDocument.summary,
            KnowledgeDocument.topic,
            KnowledgeDocument.direction_id,
            KnowledgeDocument.head_sha,
            KnowledgeDocument.lifecycle_status,
            KnowledgeDocument.created_at,
            KnowledgeDocument.updated_at,
        )
        .where(
            KnowledgeDocument.project_id == project_id,
            KnowledgeDocument.id.in_(document_ids),
        )
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.asc())
        .limit(_FAST_RESULT_EVIDENCE_UUID_LIMIT)
    )


def _fast_task_projection(
    row: Any,
    *,
    reference_key: str,
    references: list[Any],
) -> dict[str, Any]:
    status = row.status.value if isinstance(row.status, TaskStatus) else str(row.status)
    item = {
        "id": str(row.id),
        reference_key: references,
        "title": (_bounded_live_text(row.title) or "Untitled Task")[
            :_FAST_RECENT_TERMINAL_TASK_TEXT_LIMIT
        ],
        "status": status,
        "created_by": str(row.created_by or ""),
        "origination": str(row.origination or ""),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    for key in (
        "direction_id",
        "parent_strategic_task_id",
        "strategic_work_order_id",
    ):
        value = _bounded_live_text(getattr(row, key, None))
        if value is not None:
            item[key] = value[:_FAST_RECENT_TERMINAL_TASK_TEXT_LIMIT]
    for key, value in (
        ("latest_status", _bounded_live_text(row.latest_status_message)),
        ("blocked_reason", _bounded_live_text(row.blocked_reason)),
        (
            "recheck_after",
            row.recheck_after.isoformat() if row.recheck_after else None,
        ),
    ):
        if value is not None:
            item[key] = value
    return item


def _fast_document_projection(
    row: Any,
    *,
    reference_key: str,
    references: list[Any],
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        reference_key: references,
        "source_task_id": str(row.source_task_id) if row.source_task_id else None,
        "source_kind": str(row.source_kind or "unknown"),
        "title": _bounded_live_text(row.title) or "Untitled Document",
        "summary": _bounded_live_text(row.summary),
        "topic": _bounded_live_text(row.topic),
        "direction_id": _bounded_live_text(row.direction_id),
        "head_sha": _bounded_live_text(row.head_sha),
        "lifecycle_status": str(row.lifecycle_status or "unknown"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _fast_release_version_statement(*, project_id: uuid.UUID):
    """Select bounded release scalars without loading qualification JSON."""

    return (
        select(
            ReleaseVersion.id,
            ReleaseVersion.project_id,
            ReleaseVersion.version_number,
            ReleaseVersion.phase,
            ReleaseVersion.status,
            ReleaseVersion.base_sha,
            ReleaseVersion.promoted_sha,
            ReleaseVersion.promoted_at,
            ReleaseVersion.serving_status,
            ReleaseVersion.served_sha,
            ReleaseVersion.served_at,
            ReleaseVersion.deploy_confirm_deadline,
            ReleaseVersion.deploy_failure_reason,
            ReleaseVersion.active_qualification_attempt_id,
            ReleaseVersion.qualification_attempts,
            ReleaseVersion.updated_at,
        )
        .where(ReleaseVersion.project_id == project_id)
        .order_by(ReleaseVersion.version_number.desc(), ReleaseVersion.id.asc())
        .limit(_FAST_RELEASE_VERSION_LIMIT + 1)
    )


def _fast_result_correlated_release_statement(
    *,
    project_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    sha_refs: list[str],
):
    """Resolve result-mentioned release ids or SHAs to canonical scalars."""

    correlations = []
    if version_ids:
        correlations.append(ReleaseVersion.id.in_(version_ids))
    if sha_refs:
        correlations.extend(
            (
                func.lower(ReleaseVersion.promoted_sha).in_(sha_refs),
                func.lower(ReleaseVersion.served_sha).in_(sha_refs),
            )
        )
    return (
        select(
            ReleaseVersion.id,
            ReleaseVersion.project_id,
            ReleaseVersion.version_number,
            ReleaseVersion.phase,
            ReleaseVersion.status,
            ReleaseVersion.base_sha,
            ReleaseVersion.promoted_sha,
            ReleaseVersion.promoted_at,
            ReleaseVersion.serving_status,
            ReleaseVersion.served_sha,
            ReleaseVersion.served_at,
            ReleaseVersion.deploy_confirm_deadline,
            ReleaseVersion.deploy_failure_reason,
            ReleaseVersion.active_qualification_attempt_id,
            ReleaseVersion.qualification_attempts,
            ReleaseVersion.updated_at,
        )
        .where(
            ReleaseVersion.project_id == project_id,
            or_(*correlations),
        )
        .order_by(ReleaseVersion.version_number.desc(), ReleaseVersion.id.asc())
        .limit(_FAST_RESULT_CORRELATED_RELEASE_LIMIT + 1)
    )


def _fast_release_artifact_statement(*, version_ids: list[uuid.UUID]):
    """Select only durable per-artifact serving scalars for bounded releases."""

    return select(
        ReleaseDeployArtifact.id,
        ReleaseDeployArtifact.version_id,
        ReleaseDeployArtifact.artifact_kind,
        ReleaseDeployArtifact.serving_status,
        ReleaseDeployArtifact.served_identifier,
        ReleaseDeployArtifact.confirmation_deadline,
        ReleaseDeployArtifact.reason_code,
        ReleaseDeployArtifact.served_at,
        ReleaseDeployArtifact.updated_at,
    ).where(ReleaseDeployArtifact.version_id.in_(version_ids))


async def _fast_live_work_authority(
    session: AsyncSession,
    *,
    project: Project,
    resident_task_id: uuid.UUID,
    result_evidence_text: str | None = None,
    consultation_operation_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return claim-time work truth for the tool-less public Fast child."""

    (
        result_evidence_ids,
        result_evidence_shas,
        result_evidence_ids_have_more,
        result_evidence_shas_have_more,
    ) = _bounded_result_evidence_refs(result_evidence_text)
    receipt_task_refs: dict[uuid.UUID, list[dict[str, str]]] = {}
    receipt_direction_refs: dict[str, list[dict[str, str]]] = {}
    receipt_document_refs: dict[uuid.UUID, list[dict[str, str]]] = {}
    for receipt in list(consultation_operation_receipts or [])[:8]:
        operation_id = str(receipt.get("operation_id") or "").strip()
        entity_id = str(receipt.get("entity_id") or "").strip()
        effect = str(receipt.get("effect") or "").strip()
        if not operation_id or not entity_id or effect not in {
            "created",
            "updated",
            "replayed",
            "deferred",
        }:
            continue
        ref = {"operation_id": operation_id, "effect": effect}
        if receipt.get("entity_kind") == "task":
            try:
                receipt_task_refs.setdefault(uuid.UUID(entity_id), []).append(ref)
            except (TypeError, ValueError, AttributeError):
                continue
        elif receipt.get("entity_kind") == "direction" and len(entity_id) <= 255:
            receipt_direction_refs.setdefault(entity_id, []).append(ref)
        elif receipt.get("entity_kind") == "knowledge_document":
            try:
                receipt_document_refs.setdefault(uuid.UUID(entity_id), []).append(ref)
            except (TypeError, ValueError, AttributeError):
                continue

    active_directions: list[dict[str, Any]] = []
    active_direction_count = 0
    for raw in project.directions or []:
        if not isinstance(raw, dict):
            continue
        direction_id = str(raw.get("id") or "").strip()
        status = str(raw.get("status") or "unknown").strip().lower()
        if (
            not direction_id
            or status in _FAST_TERMINAL_DIRECTION_STATUSES
            or bool(raw.get("auto_completed"))
        ):
            continue
        active_direction_count += 1
        if len(active_directions) >= _FAST_LIVE_WORK_LIMIT:
            continue
        item: dict[str, Any] = {"id": direction_id, "status": status}
        for key in ("text", "priority"):
            value = _bounded_live_text(raw.get(key))
            if value is not None:
                item[key] = value
        active_directions.append(item)

    receipt_correlated_directions: list[dict[str, Any]] = []
    for raw in project.directions or []:
        if not isinstance(raw, dict):
            continue
        direction_id = str(raw.get("id") or "").strip()
        refs = receipt_direction_refs.get(direction_id)
        if not refs:
            continue
        item = {
            "id": direction_id,
            "status": str(raw.get("status") or "unknown").strip().lower(),
            "consultation_receipts": refs,
        }
        for key in ("text", "priority"):
            value = _bounded_live_text(raw.get(key))
            if value is not None:
                item[key] = value
        receipt_correlated_directions.append(item)

    task_rows = (
        await session.execute(
            _fast_live_task_statement(
                project_id=project.id,
                resident_task_id=resident_task_id,
            )
        )
    ).all()
    active_tasks: list[dict[str, Any]] = []
    active_tasks_have_more = len(task_rows) > _FAST_LIVE_WORK_LIMIT
    for row in task_rows[:_FAST_LIVE_WORK_LIMIT]:
        status = (
            row.status.value
            if isinstance(row.status, TaskStatus)
            else str(row.status)
        )
        item = {
            "id": str(row.id),
            "title": _bounded_live_text(row.title) or "Untitled Task",
            "status": status,
            "created_by": str(row.created_by or ""),
            "origination": str(row.origination or ""),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for key in (
            "direction_id",
            "parent_strategic_task_id",
            "strategic_work_order_id",
        ):
            value = _bounded_live_text(getattr(row, key, None))
            if value is not None:
                item[key] = value
        status_message = _bounded_live_text(row.latest_status_message)
        if status_message is not None:
            item["latest_status"] = status_message
        blocker = _bounded_live_text(row.blocked_reason)
        if blocker is not None:
            item["blocked_reason"] = blocker
        if row.recheck_after is not None:
            item["recheck_after"] = row.recheck_after.isoformat()
        active_tasks.append(item)

    terminal_task_rows = (
        await session.execute(
            _fast_recent_terminal_task_statement(
                project_id=project.id,
                resident_task_id=resident_task_id,
            )
        )
    ).all()
    recent_terminal_tasks: list[dict[str, Any]] = []
    recent_terminal_tasks_have_more = (
        len(terminal_task_rows) > _FAST_RECENT_TERMINAL_TASK_LIMIT
    )
    for row in terminal_task_rows[:_FAST_RECENT_TERMINAL_TASK_LIMIT]:
        status = (
            row.status.value
            if isinstance(row.status, TaskStatus)
            else str(row.status)
        )
        item = {
            "id": str(row.id),
            "title": (
                _bounded_live_text(row.title) or "Untitled Task"
            )[:_FAST_RECENT_TERMINAL_TASK_TEXT_LIMIT],
            "status": status,
            "created_by": str(row.created_by or ""),
            "origination": str(row.origination or ""),
            "completed_at": (
                row.completed_at.isoformat() if row.completed_at else None
            ),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for key in (
            "direction_id",
            "parent_strategic_task_id",
            "strategic_work_order_id",
        ):
            value = _bounded_live_text(getattr(row, key, None))
            if value is not None:
                item[key] = value[:_FAST_RECENT_TERMINAL_TASK_TEXT_LIMIT]
        recent_terminal_tasks.append(item)

    result_correlated_task_rows = []
    if result_evidence_ids:
        result_correlated_task_rows = (
            await session.execute(
                _fast_result_correlated_task_statement(
                    project_id=project.id,
                    resident_task_id=resident_task_id,
                    task_ids=result_evidence_ids,
                )
            )
        ).all()
    result_correlated_tasks = [
        _fast_task_projection(
            row, reference_key="correlation_refs", references=[str(row.id)]
        )
        for row in result_correlated_task_rows
    ]

    receipt_correlated_task_rows = []
    if receipt_task_refs:
        receipt_correlated_task_rows = (
            await session.execute(
                _fast_result_correlated_task_statement(
                    project_id=project.id,
                    resident_task_id=resident_task_id,
                    task_ids=list(receipt_task_refs),
                )
            )
        ).all()
    receipt_correlated_tasks = [
        _fast_task_projection(
            row,
            reference_key="consultation_receipts",
            references=receipt_task_refs[row.id],
        )
        for row in receipt_correlated_task_rows
    ]

    result_correlated_document_rows = []
    if result_evidence_ids:
        result_correlated_document_rows = (
            await session.execute(
                _fast_result_correlated_document_statement(
                    project_id=project.id,
                    document_ids=result_evidence_ids,
                )
            )
        ).all()
    result_correlated_documents: list[dict[str, Any]] = []
    for row in result_correlated_document_rows:
        result_correlated_documents.append(
            _fast_document_projection(
                row, reference_key="correlation_refs", references=[str(row.id)]
            )
        )
    receipt_document_rows = []
    if receipt_document_refs:
        receipt_document_rows = (
            await session.execute(
                _fast_result_correlated_document_statement(
                    project_id=project.id,
                    document_ids=list(receipt_document_refs),
                )
            )
        ).all()
    receipt_correlated_documents = [
        _fast_document_projection(
            row,
            reference_key="consultation_receipts",
            references=receipt_document_refs[row.id],
        )
        for row in receipt_document_rows
    ]

    release_rows = (
        await session.execute(_fast_release_version_statement(project_id=project.id))
    ).all()
    release_versions_have_more = len(release_rows) > _FAST_RELEASE_VERSION_LIMIT
    bounded_release_rows = release_rows[:_FAST_RELEASE_VERSION_LIMIT]
    correlated_release_rows = []
    if result_evidence_ids or result_evidence_shas:
        correlated_release_rows = (
            await session.execute(
                _fast_result_correlated_release_statement(
                    project_id=project.id,
                    version_ids=result_evidence_ids,
                    sha_refs=result_evidence_shas,
                )
            )
        ).all()
    result_correlated_releases_have_more = bool(
        len(correlated_release_rows) > _FAST_RESULT_CORRELATED_RELEASE_LIMIT
        or result_evidence_ids_have_more
        or result_evidence_shas_have_more
    )
    bounded_correlated_release_rows = correlated_release_rows[
        :_FAST_RESULT_CORRELATED_RELEASE_LIMIT
    ]
    release_rows_by_id = {
        row.id: row
        for row in [*bounded_release_rows, *bounded_correlated_release_rows]
    }
    release_version_ids = list(release_rows_by_id)
    artifact_rows = []
    if release_version_ids:
        artifact_rows = (
            await session.execute(
                _fast_release_artifact_statement(version_ids=release_version_ids)
            )
        ).all()
    artifacts_by_version: dict[uuid.UUID, list[Any]] = {
        version_id: [] for version_id in release_version_ids
    }
    for row in artifact_rows:
        if row.version_id not in artifacts_by_version:
            continue
        artifacts_by_version[row.version_id].append(
            SimpleNamespace(
                id=row.id,
                version_id=row.version_id,
                artifact_kind=row.artifact_kind,
                serving_status=row.serving_status,
                served_identifier=row.served_identifier,
                confirmation_deadline=row.confirmation_deadline,
                reason_code=row.reason_code,
                served_at=row.served_at,
                updated_at=row.updated_at,
            )
        )

    # Use the canonical aggregate serving resolver, but feed it only the scalar
    # release/artifact rows selected above. This avoids loading qualification,
    # rehearsal, or other JSON evidence into the foreground claim path.
    from app.release_artifacts import version_serving_summary

    release_projection_by_id: dict[uuid.UUID, dict[str, Any]] = {}
    result_evidence_id_strings = {str(candidate) for candidate in result_evidence_ids}
    result_evidence_sha_set = set(result_evidence_shas)
    for row in release_rows_by_id.values():
        version = SimpleNamespace(
            id=row.id,
            project_id=row.project_id,
            version_number=row.version_number,
            status=row.status,
            serving_status=row.serving_status,
            served_sha=row.served_sha,
            served_at=row.served_at,
            deploy_confirm_deadline=row.deploy_confirm_deadline,
            deploy_failure_reason=row.deploy_failure_reason,
            deploy_artifacts=artifacts_by_version.get(row.id, []),
        )
        aggregate, artifact_evidence = version_serving_summary(version, project)
        correlation_refs: list[str] = []
        if str(row.id) in result_evidence_id_strings:
            correlation_refs.append(str(row.id))
        for candidate_sha in (row.promoted_sha, row.served_sha):
            normalized_sha = str(candidate_sha or "").strip().lower()
            if (
                normalized_sha in result_evidence_sha_set
                and normalized_sha not in correlation_refs
            ):
                correlation_refs.append(normalized_sha)
        projection = {
            "id": str(row.id),
            "version_number": row.version_number,
            "phase": row.phase,
            "status": row.status,
            "base_sha": row.base_sha,
            "promoted_sha": row.promoted_sha,
            "promoted_at": (
                row.promoted_at.isoformat() if row.promoted_at else None
            ),
            "serving_status": aggregate.get("serving_status"),
            "serving_reason_code": aggregate.get("reason_code"),
            "served_sha": row.served_sha,
            "served_at": row.served_at.isoformat() if row.served_at else None,
            "deploy_confirm_deadline": (
                row.deploy_confirm_deadline.isoformat()
                if row.deploy_confirm_deadline
                else None
            ),
            "active_qualification_attempt_id": (
                str(row.active_qualification_attempt_id)
                if row.active_qualification_attempt_id
                else None
            ),
            "qualification_attempts": row.qualification_attempts,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "required_artifacts": [
                evidence
                for evidence in artifact_evidence
                if evidence.get("required")
                and evidence.get("serving_status") is not None
            ],
        }
        if correlation_refs:
            projection["correlation_refs"] = correlation_refs
        release_projection_by_id[row.id] = projection
    release_deployments = [
        release_projection_by_id[row.id] for row in bounded_release_rows
    ]
    result_correlated_releases = [
        release_projection_by_id[row.id]
        for row in bounded_correlated_release_rows
    ]

    problem_model = (
        project.problem_model if isinstance(project.problem_model, dict) else {}
    )
    problem_model_revision = problem_model.get("revision")
    if not isinstance(problem_model_revision, (int, str)):
        problem_model_revision = None
    running_update = (
        project.running_update.strip()
        if isinstance(project.running_update, str)
        else ""
    )
    running_update_bytes = running_update.encode("utf-8")
    public_vision = {
        key: value
        for key, value in (project.vision or {}).items()
        if not str(key).startswith("_")
    }
    vision_revision = public_vision.get("revision")
    if not isinstance(vision_revision, (int, str)) or isinstance(
        vision_revision, bool
    ):
        vision_revision = None
    current_project_state_authority = {
        "problem_model": {
            "present": bool(problem_model),
            "revision": problem_model_revision,
            "sha256": hashlib.sha256(canonical_json_bytes(problem_model)).hexdigest(),
            "last_updated": _bounded_live_text(problem_model.get("last_updated")),
            "last_updated_by": _bounded_live_text(
                problem_model.get("last_updated_by")
            ),
        },
        "running_update": {
            "present": bool(running_update),
            "sha256": (
                hashlib.sha256(running_update_bytes).hexdigest()
                if running_update
                else None
            ),
            "bytes": len(running_update_bytes),
        },
        "vision": {
            "present": bool(public_vision),
            "revision": vision_revision,
            "sha256": hashlib.sha256(
                canonical_json_bytes(public_vision)
            ).hexdigest(),
        },
        "authorship_rule": (
            "Current existence, revision, or digest does not identify which actor "
            "or consultation wrote it without an exact durable mutation receipt."
        ),
    }

    resident = (project.vision or {}).get("_resident_strategic_session") or {}
    activity = resident.get("activity") if isinstance(resident, dict) else None
    operator_user_id = (
        str(resident.get("operator_user_id") or project.user_id or "").strip()
        if isinstance(resident, dict)
        else str(project.user_id or "").strip()
    )
    interaction_mode = "chat"
    if operator_user_id:
        from app.workbench_state import get_strategic_workbench_state

        workbench = await get_strategic_workbench_state(
            session,
            operator_user_id,
            project.id,
        )
        if workbench.get("interaction_mode") == "continuous":
            interaction_mode = "continuous"
    private_autonomy_configured = interaction_mode == "continuous"
    resident_currently_active = bool(
        isinstance(resident, dict)
        and resident.get("status") == "active"
        and not resident.get("stop_requested")
    )
    raw_autonomy_state = (
        str(activity.get("autonomy_state") or "").strip().lower()
        if isinstance(activity, dict)
        else ""
    )
    raw_autonomy_reason = (
        str(activity.get("autonomy_reason_code") or "").strip().lower()
        if isinstance(activity, dict)
        else ""
    )
    private_autonomy_enabled = bool(
        private_autonomy_configured
        and resident_currently_active
        and raw_autonomy_state == "active"
    )
    if not private_autonomy_configured:
        private_autonomy_state = "disabled"
        private_autonomy_reason = "chat_or_disabled"
    elif not resident_currently_active:
        private_autonomy_state = "unavailable"
        private_autonomy_reason = "resident_inactive"
    elif raw_autonomy_state == "active":
        private_autonomy_state = "enabled"
        private_autonomy_reason = None
    elif raw_autonomy_state == "paused":
        private_autonomy_state = "paused"
        private_autonomy_reason = raw_autonomy_reason or "reason_unreported"
    else:
        private_autonomy_state = "unknown"
        private_autonomy_reason = "activity_unreported"
    return {
        "role_contract": {
            "public_reply_owner": "fast",
            "workday_request_receipt": "private_consultation_only",
            "interaction_mode": interaction_mode,
            "private_workday_autonomy_configured": private_autonomy_configured,
            "private_workday_autonomy": private_autonomy_state,
            "private_workday_autonomy_reason_code": private_autonomy_reason,
            "project_autonomous_enabled": bool(project.autonomous_enabled),
        },
        "resident": {
            "session_id": (
                resident.get("session_id") if isinstance(resident, dict) else None
            ),
            "status": resident.get("status") if isinstance(resident, dict) else None,
            "worker_generation": (
                resident.get("worker_generation")
                if isinstance(resident, dict)
                else None
            ),
            "activity": activity if isinstance(activity, dict) else None,
            "interaction_mode": interaction_mode,
            "private_autonomy_configured": private_autonomy_configured,
            "private_autonomy_enabled": private_autonomy_enabled,
        },
        "collection_metadata": {
            "active_directions": _meta(
                active_directions,
                active_direction_count > len(active_directions),
            ),
            "active_tasks": _meta(active_tasks, active_tasks_have_more),
            "recent_terminal_tasks": _meta(
                recent_terminal_tasks, recent_terminal_tasks_have_more
            ),
            "release_deployments": _meta(release_deployments, release_versions_have_more),
            "result_correlated_tasks": _meta(
                result_correlated_tasks, result_evidence_ids_have_more
            ),
            "receipt_correlated_directions": _meta(receipt_correlated_directions, False),
            "receipt_correlated_tasks": _meta(receipt_correlated_tasks, False),
            "receipt_correlated_documents": _meta(receipt_correlated_documents, False),
            "result_correlated_documents": _meta(
                result_correlated_documents, result_evidence_ids_have_more
            ),
            "result_correlated_releases": _meta(
                result_correlated_releases, result_correlated_releases_have_more
            ),
            "absence_rule": (
                "When a collection has_more, an unlisted id or state is unknown; "
                "absence never proves that a direction, task, document, release, or "
                "deployment does not exist or has not reached a particular state."
            ),
        },
        "result_correlation": {
            "candidate_source": "bounded_workday_result_refs_for_correlation_only",
            "canonical_state_source": "project_scoped_database_projection",
            "uuid_candidate_count": len(result_evidence_ids),
            "sha_candidate_count": len(result_evidence_shas),
            "candidate_refs_have_more": bool(
                result_evidence_ids_have_more or result_evidence_shas_have_more
            ),
        },
        "active_directions": active_directions,
        "active_tasks": active_tasks,
        "recent_terminal_tasks": recent_terminal_tasks,
        "release_deployments": release_deployments,
        "result_correlated_tasks": result_correlated_tasks,
        "receipt_correlated_directions": receipt_correlated_directions,
        "receipt_correlated_tasks": receipt_correlated_tasks,
        "receipt_correlated_documents": receipt_correlated_documents,
        "result_correlated_documents": result_correlated_documents,
        "result_correlated_releases": result_correlated_releases,
        "current_project_state_authority": current_project_state_authority,
    }
