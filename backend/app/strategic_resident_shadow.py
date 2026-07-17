"""Zero-model projection and completion for Strategic Resident shadow turns."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.strategic_resident_turns import (
    MUTATION_AUTHORIZED_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    _canonical_json,
    _create_turn_task,
    _is_live_pointer_task,
    _json_safe_payload,
    _lane_successor_attr,
    _load_pointer_tasks_locked,
    _merge_project_change_cursor,
    _promote_successor,
    _repair_pointer_state,
    _sha256,
    _task_payload,
    _utf8_prefix,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _suppress_paused_background_successor(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
    *,
    now: datetime,
) -> None:
    if resident.background_policy != "paused":
        return
    task = tasks.get(resident.background_successor_task_id)
    if task is not None and task.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }:
        task.status = TaskStatus.CANCELLED
        task.completed_at = now
        task.latest_status_message = (
            "Resident background shadow successor suppressed by paused policy"
        )
    resident.background_successor_task_id = None


SHADOW_MANIFEST_SCHEMA = "resident-shadow-manifest-v1"
MAX_MANIFEST_MESSAGES = 64
MAX_MANIFEST_BYTES = 128 * 1024
MAX_SHADOW_SWEEP_BATCH = 100


def _bounded_ref_projection(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    projected: list[dict[str, Any]] = []
    allowed = (
        "id",
        "kind",
        "label",
        "title",
        "artifact_id",
        "tool_id",
        "project_id",
        "revision",
        "checksum",
        "author",
        "timestamp",
        "created_at",
        "updated_at",
        "excerpt",
        "message_id",
        "task_id",
    )
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = {key: str(item[key]) for key in allowed if item.get(key) is not None}
        if entry:
            projected.append(entry)
    return projected


def _bounded_operator_intent(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    allowed = (
        "kind",
        "action",
        "lane_owner",
        "summary",
        "target",
        "priority",
        "approval",
        "operation_id",
        "expected_revision",
    )
    result = {key: str(raw[key])[:500] for key in allowed if raw.get(key) is not None}
    return result or None


def _message_manifest_entry(
    message: ProjectMessage,
    *,
    reply_targets: dict[uuid.UUID, ProjectMessage],
) -> dict[str, Any]:
    context = message.context if isinstance(message.context, dict) else {}
    content = message.content or ""
    attachments = context.get("attachments")
    attachment_projection: list[dict[str, Any]] = []
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            projected = {}
            for key in (
                "id",
                "attachment_id",
                "kind",
                "type",
                "filename",
                "name",
                "label",
                "mime_type",
                "media_type",
                "content_type",
                "size_bytes",
                "revision",
                "checksum",
                "status",
                "author",
                "timestamp",
                "created_at",
                "updated_at",
            ):
                if attachment.get(key) is not None:
                    projected[key] = str(attachment[key])
            if projected:
                attachment_projection.append(projected)
    reply_target = reply_targets.get(message.reply_to_message_id)
    reply_projection = None
    if reply_target is not None:
        reply_content = reply_target.content or ""
        excerpt, excerpt_truncated = _utf8_prefix(reply_content, 2_000)
        reply_projection = {
            "id": str(reply_target.id),
            "sequence": int(reply_target.sequence),
            "actor_user_id": reply_target.actor_user_id,
            "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "excerpt_bytes": len(excerpt.encode("utf-8")),
            "content_sha256": hashlib.sha256(reply_content.encode("utf-8")).hexdigest(),
            "excerpt": excerpt,
            "excerpt_truncated": excerpt_truncated,
        }
    bounded_content, content_truncated = _utf8_prefix(content, 48 * 1024)
    return {
        "id": str(message.id),
        "sequence": int(message.sequence),
        "actor_user_id": message.actor_user_id,
        "created_at": message.created_at.isoformat(),
        "message_type": (
            message.message_type.value
            if hasattr(message.message_type, "value")
            else str(message.message_type)
        ),
        "reply_to_message_id": (
            str(message.reply_to_message_id)
            if message.reply_to_message_id is not None
            else None
        ),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_bytes": len(content.encode("utf-8")),
        "content": bounded_content,
        "content_truncated": content_truncated,
        "source_refs": _bounded_ref_projection(context.get("source_refs")),
        "artifact_refs": _bounded_ref_projection(context.get("artifact_refs")),
        "attachments": attachment_projection,
        "operator_intent": _bounded_operator_intent(context.get("operator_intent")),
        "reply_target": reply_projection,
        "legacy_wakeup_state": context.get("_resident_wakeup_state"),
    }


def _enforce_manifest_budget(manifest: dict[str, Any]) -> dict[str, Any]:
    """Deterministically shrink a manifest to the global encoded-byte ceiling."""

    result = dict(manifest)
    entries = list(manifest.get("messages") or [])
    result["requested_chip_count"] = int(
        manifest.get("requested_chip_count")
        if manifest.get("requested_chip_count") is not None
        else sum(
            sum(
                len(entry.get(key) or ())
                for key in ("source_refs", "artifact_refs", "attachments")
            )
            for entry in entries
        )
    )
    result["messages"] = []
    for entry in entries:
        candidate = {**result, "messages": [*result["messages"], entry]}
        if len(_canonical_json(candidate)) > MAX_MANIFEST_BYTES:
            break
        result["messages"].append(entry)
    if entries and not result["messages"]:
        raise ValueError("one complete Resident message exceeds manifest byte budget")
    result["messages_truncated"] = bool(
        manifest.get("messages_truncated") or len(result["messages"]) < len(entries)
    )
    if len(_canonical_json(result)) > MAX_MANIFEST_BYTES:
        raise ValueError(
            "Resident shadow manifest fixed projection exceeds byte budget"
        )
    result["represented_message_sequence_end"] = (
        result["messages"][-1]["sequence"] if result["messages"] else None
    )
    result["represented_chip_count"] = sum(
        sum(
            len(entry.get(key) or ())
            for key in ("source_refs", "artifact_refs", "attachments")
        )
        for entry in result["messages"]
    )
    return result


async def _build_shadow_manifest(
    session: AsyncSession,
    *,
    project: Project,
    resident: StrategicResident,
    task: Task,
) -> dict[str, Any]:
    payload = _task_payload(task)
    start = payload.get("message_sequence_start")
    end = payload.get("message_sequence_end")
    message_entries: list[dict[str, Any]] = []
    message_count = 0
    requested_chip_count = 0
    if isinstance(start, int) and isinstance(end, int):
        filters = (
            ProjectMessage.project_id == project.id,
            ProjectMessage.surface == MessageSurface.CONVERSATION,
            ProjectMessage.sequence >= start,
            ProjectMessage.sequence <= end,
        )
        message_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(ProjectMessage).where(*filters)
                )
            ).scalar()
            or 0
        )
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            chip_lengths = [
                case(
                    (
                        func.jsonb_typeof(ProjectMessage.context[key]) == "array",
                        func.jsonb_array_length(ProjectMessage.context[key]),
                    ),
                    else_=0,
                )
                for key in ("source_refs", "artifact_refs", "attachments")
            ]
        else:
            chip_lengths = [
                case(
                    (
                        func.json_type(ProjectMessage.context, f"$.{key}") == "array",
                        func.json_array_length(ProjectMessage.context, f"$.{key}"),
                    ),
                    else_=0,
                )
                for key in ("source_refs", "artifact_refs", "attachments")
            ]
        requested_chip_count = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(sum(chip_lengths)), 0))
                    .select_from(ProjectMessage)
                    .where(*filters)
                )
            ).scalar()
            or 0
        )
        messages = (
            (
                await session.execute(
                    select(ProjectMessage)
                    .where(*filters)
                    .order_by(ProjectMessage.sequence.asc())
                    .limit(MAX_MANIFEST_MESSAGES)
                )
            )
            .scalars()
            .all()
        )
        reply_ids = {
            message.reply_to_message_id
            for message in messages
            if message.reply_to_message_id is not None
        }
        reply_targets = {}
        if reply_ids:
            reply_rows = (
                (
                    await session.execute(
                        select(ProjectMessage).where(
                            ProjectMessage.project_id == project.id,
                            ProjectMessage.id.in_(reply_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            reply_targets = {row.id: row for row in reply_rows}
        message_entries = [
            _message_manifest_entry(message, reply_targets=reply_targets)
            for message in messages
        ]

    cp = (
        await session.execute(
            select(StrategicResidentCheckpoint).where(
                StrategicResidentCheckpoint.project_id == project.id,
                StrategicResidentCheckpoint.version == payload["checkpoint_version"],
                StrategicResidentCheckpoint.content_hash == payload["checkpoint_hash"],
            )
        )
    ).scalar_one_or_none()
    if cp is None:
        raise ValueError("frozen Resident checkpoint lineage is missing or corrupt")
    checkpoint = {
        "id": str(cp.id),
        "version": int(cp.version),
        "content_hash": cp.content_hash,
        "authority_generation": int(cp.authority_generation),
    }
    checkpoint_body = cp.checkpoint if isinstance(cp.checkpoint, dict) else {}
    current_checkpoint = None
    if resident.current_checkpoint_id is not None:
        current = await session.get(
            StrategicResidentCheckpoint, resident.current_checkpoint_id
        )
        if current is not None and current.project_id == project.id:
            current_checkpoint = {
                "id": str(current.id),
                "version": int(current.version),
                "content_hash": current.content_hash,
            }

    manifest = {
        "schema": SHADOW_MANIFEST_SCHEMA,
        "project_id": str(project.id),
        "resident_id": str(resident.project_id),
        "turn_task_id": str(task.id),
        "lane": payload.get("lane"),
        "authority_generation": int(payload.get("authority_generation") or 1),
        "engine_owner": resident.engine_owner,
        "checkpoint": checkpoint,
        "current_checkpoint": current_checkpoint,
        "handled_message_sequence": int(resident.handled_message_sequence),
        "handled_project_change_cursor": dict(
            resident.handled_project_change_cursor or {}
        ),
        "message_sequence_start": start,
        "message_sequence_end": end,
        "message_count": message_count,
        "requested_chip_count": requested_chip_count,
        "messages_truncated": message_count > len(message_entries),
        "messages": message_entries,
        # Stable project-state identities, not raw operational history.
        "charter_hash": _sha256(checkpoint_body.get("charter") or {}),
        "working_strategy_hash": _sha256(checkpoint_body.get("working_strategy") or {}),
    }
    return _enforce_manifest_budget(manifest)


async def _complete_shadow_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    now: datetime,
) -> bool:
    # Canonical authority order: Project -> StrategicResident -> active Task.
    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(skip_locked=True, read=True, key_share=True)
        )
    ).scalar_one_or_none()
    if project is None:
        await session.rollback()
        return False
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if resident is None or resident.active_turn_task_id != task_id:
        await session.rollback()
        return False
    if resident.engine_owner != "legacy":
        await session.rollback()
        return False
    tasks = await _load_pointer_tasks_locked(session, resident)
    _repair_pointer_state(resident, tasks)
    if resident.active_turn_task_id != task_id:
        _suppress_paused_background_successor(resident, tasks, now=now)
        promoted = _promote_successor(resident, tasks)
        scheduling = dict(resident.scheduling_state or {})
        scheduling.update(
            {
                "shadow_pointer_repaired_at": now.isoformat(),
                "shadow_repaired_active_task_id": (
                    str(promoted.id) if promoted is not None else None
                ),
            }
        )
        resident.scheduling_state = scheduling
        session.add(resident)
        await session.commit()
        return False
    task = tasks.get(task_id)
    if (
        not _is_live_pointer_task(task, project_id=project_id)
        or task is None
        or not bool((task.context or {}).get(SHADOW_CONTEXT_KEY))
        or bool((task.context or {}).get(MUTATION_AUTHORIZED_CONTEXT_KEY))
    ):
        await session.rollback()
        return False
    if (
        resident.active_turn_lane == "background"
        and resident.background_policy == "paused"
    ):
        task.status = TaskStatus.CANCELLED
        task.completed_at = now
        task.latest_status_message = (
            "Resident background shadow turn suppressed by paused policy"
        )
        resident.active_turn_task_id = None
        resident.active_turn_lane = None
        _suppress_paused_background_successor(resident, tasks, now=now)
        promoted = _promote_successor(resident, tasks)
        scheduling = dict(resident.scheduling_state or {})
        scheduling["shadow_background_cancelled_at"] = now.isoformat()
        scheduling["shadow_active_task_id"] = str(promoted.id) if promoted else None
        resident.scheduling_state = scheduling
        session.add(task)
        session.add(resident)
        await session.commit()
        return False

    payload = _task_payload(task)
    manifest = await _build_shadow_manifest(
        session,
        project=project,
        resident=resident,
        task=task,
    )
    manifest_hash = _sha256(manifest)
    requested_range = [
        manifest.get("message_sequence_start"),
        manifest.get("message_sequence_end"),
    ]
    represented_range = [
        manifest.get("message_sequence_start"),
        manifest.get("represented_message_sequence_end"),
    ]
    requested_chip_count = int(manifest.get("requested_chip_count") or 0)
    represented_chip_count = int(manifest.get("represented_chip_count") or 0)
    checkpoint = manifest.get("checkpoint") or {}
    mismatch_reasons: list[str] = []
    if payload.get("checkpoint_version") != checkpoint.get("version"):
        mismatch_reasons.append("checkpoint_version_changed")
    if payload.get("checkpoint_hash") != checkpoint.get("content_hash"):
        mismatch_reasons.append("checkpoint_hash_changed")
    if requested_range != represented_range:
        mismatch_reasons.append("message_range_requires_remainder")
    if any(
        entry.get("legacy_wakeup_state")
        not in (
            None,
            "queued",
            "coalesced",
            "paused",
            "waiting_for_capacity",
        )
        for entry in manifest.get("messages") or []
    ):
        mismatch_reasons.append("legacy_wakeup_state_unrecognized")
    comparison = {
        "checkpoint_lineage_matches": not any(
            reason.startswith("checkpoint_") for reason in mismatch_reasons
        ),
        "requested_message_range": requested_range,
        "represented_message_range": represented_range,
        "requested_chip_count": requested_chip_count,
        "represented_chip_count": represented_chip_count,
        "shadow_scheduling_decision": "completed_and_promote",
        "mismatch_reasons": mismatch_reasons,
    }
    latency_ms = max(0, int((now - task.created_at).total_seconds() * 1000))
    task.plan_output = {
        "resident_shadow": {
            "manifest": manifest,
            "manifest_hash": manifest_hash,
            "projection_latency_ms": latency_ms,
            "zero_model_calls": True,
            "mutation_authorized": False,
            "legacy_authority_preserved": resident.engine_owner == "legacy",
            "comparison": comparison,
        }
    }
    task.status = TaskStatus.COMPLETED
    task.completed_at = now
    task.updated_at = now
    task.latest_status_message = (
        f"Resident shadow projection completed with zero model calls "
        f"(manifest {manifest_hash[:12]})"
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SHADOW_COMPLETED",
            details={
                "manifest_hash": manifest_hash,
                "projection_latency_ms": latency_ms,
                "zero_model_calls": True,
            },
        )
    )

    message_end = payload.get("message_sequence_end")
    represented_end = manifest.get("represented_message_sequence_end")
    scheduling = dict(resident.scheduling_state or {})
    if isinstance(represented_end, int):
        scheduling["shadow_handled_message_sequence"] = max(
            int(scheduling.get("shadow_handled_message_sequence") or 0),
            represented_end,
        )
    has_remainder = (
        isinstance(message_end, int)
        and isinstance(represented_end, int)
        and represented_end < message_end
    )
    if has_remainder:
        remainder_start = represented_end + 1
        attr = _lane_successor_attr("interactive")
        remainder = tasks.get(getattr(resident, attr))
        if not _is_live_pointer_task(remainder, project_id=project_id):
            remainder = await _create_turn_task(
                session,
                resident,
                lane="interactive",
                trigger_kind="operator_message",
                message_sequence=remainder_start,
                project_change_cursor=None,
                input_frozen=False,
            )
            setattr(resident, attr, remainder.id)
            tasks[remainder.id] = remainder
        remainder_payload = dict(_task_payload(remainder))
        prior_start = remainder_payload.get("message_sequence_start")
        prior_end = remainder_payload.get("message_sequence_end")
        remainder_payload["message_sequence_start"] = min(
            remainder_start,
            prior_start if isinstance(prior_start, int) else remainder_start,
        )
        remainder_payload["message_sequence_end"] = max(
            message_end,
            prior_end if isinstance(prior_end, int) else message_end,
        )
        remainder_payload["trigger_count"] = min(
            10_000,
            max(
                int(remainder_payload.get("trigger_count") or 1),
                remainder_payload["message_sequence_end"]
                - remainder_payload["message_sequence_start"]
                + 1,
            ),
        )
        remainder.payloads = [
            _json_safe_payload(
                validate_internal_payload(
                    remainder_payload, "strategic_resident_turn:remainder"
                )
            )
        ]
        session.add(remainder)
    payload_cursor = payload.get("project_change_cursor")
    if isinstance(payload_cursor, dict) and not has_remainder:
        scheduling["shadow_handled_project_change_cursor"] = (
            _merge_project_change_cursor(
                scheduling.get("shadow_handled_project_change_cursor")
                if isinstance(
                    scheduling.get("shadow_handled_project_change_cursor"), dict
                )
                else {},
                payload_cursor,
            )
        )
    resident.active_turn_task_id = None
    resident.active_turn_lane = None
    _suppress_paused_background_successor(resident, tasks, now=now)
    promoted = _promote_successor(resident, tasks)
    scheduling.update(
        {
            "shadow_last_completed_at": now.isoformat(),
            "shadow_last_completed_task_id": str(task.id),
            "shadow_last_manifest_hash": manifest_hash,
            "shadow_last_projection_latency_ms": latency_ms,
            "shadow_last_comparison": comparison,
            "shadow_projection_state": "healthy",
            "shadow_active_task_id": str(promoted.id) if promoted else None,
        }
    )
    resident.scheduling_state = scheduling
    resident.next_wakeup_at = now if promoted is not None else None
    session.add(resident)
    await session.commit()
    return True


async def _quarantine_failed_shadow_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    now: datetime,
    error: Exception,
) -> bool:
    """Make a failed projection durable so one bad turn cannot wedge a project."""

    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(skip_locked=True, read=True, key_share=True)
        )
    ).scalar_one_or_none()
    if project is None:
        await session.rollback()
        return False
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        resident is None
        or resident.engine_owner != "legacy"
        or resident.active_turn_task_id != task_id
    ):
        await session.rollback()
        return False
    tasks = await _load_pointer_tasks_locked(session, resident)
    task = tasks.get(task_id)
    if (
        task is None
        or not _is_live_pointer_task(task, project_id=project_id)
        or not bool((task.context or {}).get(SHADOW_CONTEXT_KEY))
    ):
        await session.rollback()
        return False

    # Keep the durable, operator-visible state useful without persisting arbitrary
    # exception text, which can contain user content or credentials.
    reason = f"Resident shadow projection raised {type(error).__name__}"
    context = dict(task.context or {})
    context["resident_shadow_projection_failure"] = {
        "reason": reason,
        "exception_type": type(error).__name__,
        "failed_at": now.isoformat(),
    }
    task.context = context
    task.status = TaskStatus.FAILED
    task.completed_at = now
    task.updated_at = now
    task.latest_status_message = f"Resident shadow projection failed: {reason[:400]}"
    resident.active_turn_task_id = None
    resident.active_turn_lane = None
    _suppress_paused_background_successor(resident, tasks, now=now)
    promoted = _promote_successor(resident, tasks)
    scheduling = dict(resident.scheduling_state or {})
    scheduling.update(
        {
            "shadow_projection_state": "degraded",
            "shadow_last_failure_at": now.isoformat(),
            "shadow_last_failure_task_id": str(task.id),
            "shadow_last_failure_reason": reason,
            "shadow_active_task_id": str(promoted.id) if promoted else None,
        }
    )
    resident.scheduling_state = scheduling
    resident.next_wakeup_at = now if promoted is not None else None
    session.add(task)
    session.add(resident)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SHADOW_PROJECTION_FAILED",
            details={"reason": reason},
        )
    )
    await session.commit()
    return True


async def sweep_shadow_resident_turns(
    *,
    session_factory,
    now: datetime | None = None,
    batch_size: int = 32,
) -> int:
    """Complete a bounded batch of active shadow turns with zero model calls."""

    effective_now = now or _utcnow()
    async with session_factory() as scan_session:
        rows = (
            await scan_session.execute(
                select(
                    StrategicResident.project_id,
                    StrategicResident.active_turn_task_id,
                )
                .where(StrategicResident.active_turn_task_id.is_not(None))
                .where(StrategicResident.engine_owner == "legacy")
                .order_by(StrategicResident.last_wakeup_at.asc().nullsfirst())
                .limit(max(1, min(int(batch_size), MAX_SHADOW_SWEEP_BATCH)))
            )
        ).all()
    completed = 0
    for project_id, task_id in rows:
        if task_id is None:
            continue
        try:
            async with session_factory() as worker_session:
                did_complete = await _complete_shadow_turn(
                    worker_session,
                    project_id=project_id,
                    task_id=task_id,
                    now=effective_now,
                )
            if did_complete:
                completed += 1
        except Exception as error:
            logger.exception(
                "[RESIDENT_SHADOW] Failed to project turn %s for project %s",
                task_id,
                project_id,
            )
            try:
                async with session_factory() as recovery_session:
                    quarantined = await _quarantine_failed_shadow_turn(
                        recovery_session,
                        project_id=project_id,
                        task_id=task_id,
                        now=effective_now,
                        error=error,
                    )
                if quarantined:
                    logger.warning(
                        "[RESIDENT_SHADOW] Quarantined failed turn %s for project %s",
                        task_id,
                        project_id,
                    )
            except Exception:
                logger.exception(
                    "[RESIDENT_SHADOW] Failed to quarantine turn %s for project %s",
                    task_id,
                    project_id,
                )
    if completed:
        logger.info(
            "[RESIDENT_SHADOW] Completed %d backend-only turn projection(s)",
            completed,
        )
    return completed


__all__ = [
    "MAX_MANIFEST_BYTES",
    "sweep_shadow_resident_turns",
]
