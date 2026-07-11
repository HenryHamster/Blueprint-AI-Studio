"""Shared websocket event envelope for operator workbench refresh signals.

The admin UI applies websocket events as invalidation hints, not as source data.
Keep the envelope compact and stable so emitters can consistently say which
read-model surfaces should be refreshed without inventing event-specific shapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import Alert, MessageType, ProjectMessage, StrategicCycle, Task
from app.workbench_contracts import (
    DEFAULT_REFETCH_SURFACES as CONTRACT_DEFAULT_REFETCH_SURFACES,
    SURFACE_DIRECTIONS,
    SURFACE_MESSAGES,
    SURFACE_PIPELINE,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    compact_text,
    normalize_refetch_surfaces,
    normalize_target,
)

WORKBENCH_EVENT_SCHEMA_VERSION = 1

DEFAULT_WORKBENCH_REFETCH_SURFACES = CONTRACT_DEFAULT_REFETCH_SURFACES
DEFAULT_WORKBENCH_EVENT_FEED_LIMIT = 24
_SOURCE_QUERY_LIMIT = 12
_OPERATOR_COMMAND_EVENT_TYPE = "operator_command"


def _compact_str(value: Any, *, max_chars: int = 160) -> str | None:
    return compact_text(value, max_chars=max_chars)


def _bounded_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _payload_task_type(task: Task) -> str | None:
    first = task.first_payload if isinstance(task.first_payload, dict) else {}
    return _compact_str(first.get("type"), max_chars=80)


def _event_tone(*, resource: str, action: str, severity: str | None = None) -> str:
    severity_norm = str(severity or "").upper()
    if severity_norm == "ERROR":
        return "critical"
    if severity_norm == "WARNING":
        return "warning"
    if resource == "project_message" and action in {"created", "linked"}:
        return "warning"
    if resource == "task" and action in {"failed", "blocked"}:
        return "critical" if action == "failed" else "warning"
    if resource == "strategic_cycle" and action.startswith("failed"):
        return "critical"
    if resource == "task" and action in {"in_progress", "provisioning", "evaluating"}:
        return "active"
    return "neutral"


def _bounded_surfaces(values: Sequence[str] | None) -> list[str]:
    return normalize_refetch_surfaces(
        values,
        default=DEFAULT_WORKBENCH_REFETCH_SURFACES,
    )


def _bounded_mapping(value: Any, *, max_items: int = 16) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    bounded: dict[str, Any] = {}
    for key, item in value.items():
        if len(bounded) >= max_items:
            break
        key_text = _compact_str(key, max_chars=80)
        if not key_text:
            continue
        if isinstance(item, (int, float, bool)) or item is None:
            bounded[key_text] = item
        else:
            item_text = _compact_str(item, max_chars=240)
            if item_text:
                bounded[key_text] = item_text
    return bounded or None


def workbench_event_envelope(
    project_id: Any,
    *,
    resource: str,
    action: str,
    refetch_surfaces: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return the common v1 envelope every workbench event payload carries."""

    return {
        "schema_version": WORKBENCH_EVENT_SCHEMA_VERSION,
        "project_id": str(project_id),
        "resource": _compact_str(resource, max_chars=80) or "workbench",
        "action": _compact_str(action, max_chars=80) or "updated",
        "refetch": {
            "surfaces": _bounded_surfaces(refetch_surfaces)
        },
    }


def build_workbench_event_payload(
    project_id: Any,
    *,
    resource: str,
    action: str,
    refetch_surfaces: Sequence[str] | None = None,
    summary_key: str | None = None,
    summary: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a compact operator-workbench websocket payload.

    ``extra`` preserves legacy top-level fields such as ``message_id`` or
    ``task_id``. ``summary_key`` lets callers add a small resource-specific
    nested summary while still sharing the same envelope.
    """

    payload = {
        **extra,
        **workbench_event_envelope(
            project_id,
            resource=resource,
            action=action,
            refetch_surfaces=refetch_surfaces,
        ),
    }
    if summary_key and summary is not None:
        payload[summary_key] = dict(summary)
    return payload


def _feed_item(
    project_id: Any,
    *,
    item_id: str,
    occurred_at: datetime | None,
    source: str,
    resource: str,
    action: str,
    title: str,
    summary: str | None = None,
    tone: str | None = None,
    ref: Mapping[str, Any] | None = None,
    target: Mapping[str, Any] | None = None,
    refetch_surfaces: Sequence[str] | None = None,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": item_id[:240],
        "occurred_at": _isoformat(occurred_at),
        "source": _compact_str(source, max_chars=80) or "system",
        "title": _compact_str(title, max_chars=180) or "Workbench event",
        **workbench_event_envelope(
            project_id,
            resource=resource,
            action=action,
            refetch_surfaces=refetch_surfaces,
        ),
    }
    compact_summary = _compact_str(summary, max_chars=280)
    if compact_summary:
        payload["summary"] = compact_summary
    if tone:
        payload["tone"] = tone
    if ref:
        payload["ref"] = dict(ref)
    normalized_target = normalize_target(target)
    if normalized_target:
        payload["target"] = normalized_target
    if data:
        payload["data"] = dict(data)
    return payload


def _message_feed_item(message: ProjectMessage) -> dict[str, Any]:
    context = message.context if isinstance(message.context, Mapping) else {}
    if context.get("event_type") == _OPERATOR_COMMAND_EVENT_TYPE:
        return _operator_command_feed_item(message, context)

    message_type = _enum_value(message.message_type)
    status = _enum_value(message.status)
    action = "resolved" if status == "resolved" else "linked" if status == "linked" else "created"
    if message.message_type == MessageType.QUESTION and status != "resolved":
        target_action = "reply"
    else:
        target_action = "highlight"
    tone = "critical" if message.message_type == MessageType.BLOCKER and status != "resolved" else _event_tone(
        resource="project_message",
        action=action,
    )
    return _feed_item(
        message.project_id,
        item_id=f"message:{message.id}:{action}",
        occurred_at=message.resolved_at if action == "resolved" else message.created_at,
        source=str(message.from_type or "message"),
        resource="project_message",
        action=action,
        title=f"{message_type.title().replace('_', ' ')} message",
        summary=message.content,
        tone=tone,
        ref={"kind": "message", "id": str(message.id)},
        target={
            "surface": "messages",
            "message_id": str(message.id),
            "action": target_action,
        },
        refetch_surfaces=(SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION),
        data={
            "message_type": message_type,
            "status": status,
            "from_type": str(message.from_type),
        },
    )


def _operator_command_feed_item(
    message: ProjectMessage,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = context.get(_OPERATOR_COMMAND_EVENT_TYPE)
    receipt = receipt if isinstance(receipt, Mapping) else {}
    status = _compact_str(receipt.get("status"), max_chars=40) or "succeeded"
    label = _compact_str(receipt.get("label"), max_chars=120) or "Workbench command"
    command_id = _compact_str(receipt.get("command_id"), max_chars=180)
    target = receipt.get("target") if isinstance(receipt.get("target"), Mapping) else None
    refetch_surfaces = (
        [
            surface
            for surface in receipt.get("refetch_surfaces", [])
            if isinstance(surface, str)
        ]
        if isinstance(receipt.get("refetch_surfaces"), list)
        else (SURFACE_STRATEGIC_SESSION,)
    )
    data = {
        "command_id": command_id,
        "status": status,
    }
    result = receipt.get("result")
    bounded_result = _bounded_mapping(result)
    if bounded_result:
        data["result"] = bounded_result
    tone = "active"
    if status == "failed":
        status_code = _bounded_int(bounded_result.get("status_code") if bounded_result else None)
        tone = "critical" if status_code >= 500 else "warning"
    elif status != "succeeded":
        tone = "warning"
    return _feed_item(
        message.project_id,
        item_id=f"operator_command:{message.id}:{status}",
        occurred_at=message.resolved_at or message.created_at,
        source="operator_command",
        resource=_OPERATOR_COMMAND_EVENT_TYPE,
        action=status,
        title=f"Command: {label}",
        summary=message.content,
        tone=tone,
        ref={"kind": _OPERATOR_COMMAND_EVENT_TYPE, "id": str(message.id)},
        target=target,
        refetch_surfaces=refetch_surfaces,
        data=data,
    )


def _task_feed_item(task: Task) -> dict[str, Any]:
    status = _enum_value(task.status)
    action = status.lower()
    occurred_at = _task_occurred_at(task)
    task_type = _payload_task_type(task)
    return _feed_item(
        task.project_id,
        item_id=f"task:{task.id}:{action}",
        occurred_at=occurred_at,
        source=str(task.created_by or "task"),
        resource="task",
        action=action,
        title=task.title or "Task update",
        summary=task.latest_status_message,
        tone=_event_tone(resource="task", action=action),
        ref={"kind": "task", "id": str(task.id)},
        target={"surface": "task", "task_id": str(task.id)},
        refetch_surfaces=(SURFACE_PIPELINE, SURFACE_STRATEGIC_SESSION),
        data={
            "status": status,
            "task_type": task_type,
        },
    )


def _task_status_transition_occurred_at(task: Task) -> datetime | None:
    entries = task.status_log if isinstance(task.status_log, list) else []
    for entry in reversed(entries):
        if not isinstance(entry, Mapping):
            continue
        message = _compact_str(entry.get("msg"), max_chars=80)
        if not message or not message.startswith("Status changed:"):
            continue
        occurred_at = _parse_iso_datetime(entry.get("t"))
        if occurred_at:
            return occurred_at
    return None


def _task_occurred_at(task: Task) -> datetime | None:
    return (
        _task_status_transition_occurred_at(task)
        or task.completed_at
        or task.started_at
        or task.created_at
    )


def _alert_feed_item(alert: Alert) -> dict[str, Any]:
    severity = _enum_value(alert.severity)
    action = "dismissed" if alert.dismissed_at else "raised"
    target = (
        {"surface": "task", "task_id": str(alert.task_id)}
        if alert.task_id
        else {"surface": "runtime", "section": "health"}
    )
    return _feed_item(
        alert.project_id,
        item_id=f"alert:{alert.id}:{action}",
        occurred_at=alert.dismissed_at or alert.created_at,
        source=alert.source or alert.domain or "alert",
        resource="alert",
        action=action,
        title=alert.title,
        summary=alert.message,
        tone=_event_tone(resource="alert", action=action, severity=severity),
        ref={"kind": "alert", "id": str(alert.id)},
        target=target,
        refetch_surfaces=(SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION),
        data={
            "severity": severity,
            "domain": alert.domain,
            "event_type": alert.event_type,
            "active": alert.dismissed_at is None,
        },
    )


def _cycle_occurred_at(cycle: StrategicCycle) -> datetime | None:
    return (
        cycle.phase2_completed_at
        or cycle.phase1_completed_at
        or cycle.updated_at
        or cycle.created_at
    )


def _cycle_feed_item(cycle: StrategicCycle) -> dict[str, Any]:
    outcome = _compact_str(cycle.outcome, max_chars=80) or "updated"
    action = "completed" if outcome == "completed" else outcome
    summary = cycle.summary_text or cycle.failure_reason or cycle.cycle_reasoning
    phase_task_id = cycle.phase2_task_id or cycle.phase1_task_id
    target = (
        {"surface": "task", "task_id": str(phase_task_id)}
        if phase_task_id
        else {"surface": "project", "section": "directions"}
    )
    return _feed_item(
        cycle.project_id,
        item_id=f"strategic_cycle:{cycle.id}:{action}",
        occurred_at=_cycle_occurred_at(cycle),
        source="strategic",
        resource="strategic_cycle",
        action=action,
        title="Strategic cycle",
        summary=summary,
        tone=_event_tone(resource="strategic_cycle", action=action),
        ref={"kind": "strategic_cycle", "id": str(cycle.id)},
        target=target,
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_DIRECTIONS),
        data={
            "outcome": outcome,
            "tasks_spawned": cycle.tasks_spawned,
            "directions_created": cycle.directions_created,
            "directions_retired": cycle.directions_retired,
        },
    )


async def build_workbench_event_feed(
    session: AsyncSession,
    project_id: Any,
    *,
    limit: int = DEFAULT_WORKBENCH_EVENT_FEED_LIMIT,
    runtime_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded durable current-state event feed for the work session."""

    bounded_limit = max(1, min(int(limit or DEFAULT_WORKBENCH_EVENT_FEED_LIMIT), 50))
    source_limit = max(_SOURCE_QUERY_LIMIT, bounded_limit + 1)
    message_rows = (
        await session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project_id)
            .order_by(ProjectMessage.created_at.desc())
            .limit(source_limit)
        )
    ).scalars().all()
    task_occurred_at = func.coalesce(Task.completed_at, Task.started_at, Task.created_at)
    task_recent_activity = func.coalesce(
        Task.completed_at,
        Task.updated_at,
        Task.started_at,
        Task.created_at,
    )
    task_activity_source_limit = source_limit
    _first_payload_col = Task.payloads[0].label("_projected_first_payload")
    task_base_query = (
        select(Task, _first_payload_col)
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.title,
                Task.latest_status_message,
                Task.created_by,
                Task.status_log,
                Task.completed_at,
                Task.started_at,
                Task.created_at,
                raiseload=True,
            )
        )
        .where(Task.project_id == project_id, Task.archived.is_(False))
    )

    def _unpack_task_rows(rows: list) -> list[Task]:
        result = []
        for row in rows:
            task, fp = row[0], row[1]
            # Always stash so task.first_payload never falls back to Task.payloads
            # (which is excluded from load_only and raises under raiseload=True).
            object.__setattr__(task, "_projected_first_payload", fp if isinstance(fp, dict) else {})
            result.append(task)
        return result

    task_lifecycle_rows = _unpack_task_rows(
        (
            await session.execute(
                task_base_query
                .order_by(task_occurred_at.desc())
                .limit(source_limit)
            )
        ).all()
    )
    task_activity_rows = _unpack_task_rows(
        (
            await session.execute(
                task_base_query
                .order_by(task_recent_activity.desc())
                .limit(task_activity_source_limit)
            )
        ).all()
    )
    task_rows_by_id: dict[str, Task] = {}
    for row in [*task_lifecycle_rows, *task_activity_rows]:
        task_rows_by_id[str(row.id)] = row
    task_rows = list(task_rows_by_id.values())
    alert_rows = (
        await session.execute(
            select(Alert)
            .where(Alert.project_id == project_id)
            .order_by(func.coalesce(Alert.dismissed_at, Alert.created_at).desc())
            .limit(source_limit)
        )
    ).scalars().all()
    cycle_occurred_at = func.coalesce(
        StrategicCycle.phase2_completed_at,
        StrategicCycle.phase1_completed_at,
        StrategicCycle.updated_at,
        StrategicCycle.created_at,
    )
    cycle_rows = (
        await session.execute(
            select(StrategicCycle)
            .where(StrategicCycle.project_id == project_id)
            .order_by(cycle_occurred_at.desc())
            .limit(source_limit)
        )
    ).scalars().all()
    items = [
        *[_message_feed_item(row) for row in message_rows],
        *[_task_feed_item(row) for row in task_rows],
        *[_alert_feed_item(row) for row in alert_rows],
        *[_cycle_feed_item(row) for row in cycle_rows],
        *_runtime_feed_items(project_id, runtime_summary),
    ]
    items.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return {
        "version": 1,
        "limit": bounded_limit,
        "has_more": len(items) > bounded_limit,
        "items": items[:bounded_limit],
    }


def _runtime_feed_items(
    project_id: Any,
    runtime_summary: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(runtime_summary, Mapping):
        return []
    return [
        *_local_runner_feed_items(project_id, runtime_summary.get("local_runner")),
        *_compute_job_feed_items(project_id, runtime_summary.get("work")),
    ]


def _parse_runtime_occurred_at(*values: Any) -> datetime | None:
    for value in values:
        if isinstance(value, datetime):
            return value
        parsed = _parse_iso_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _local_runner_feed_items(
    project_id: Any,
    local_runner: Any,
) -> list[dict[str, Any]]:
    if not isinstance(local_runner, Mapping):
        return []
    items: list[dict[str, Any]] = []
    runners = local_runner.get("runners")
    runners = runners if isinstance(runners, list) else []
    active_runner_id = _compact_str(local_runner.get("active_runner_id"), max_chars=80)
    continuation = (
        local_runner.get("continuation")
        if isinstance(local_runner.get("continuation"), Mapping)
        else {}
    )
    preferred_runner_id = _compact_str(
        continuation.get("preferred_runner_id"),
        max_chars=80,
    )

    for runner in runners:
        if not isinstance(runner, Mapping):
            continue
        runner_id = _compact_str(runner.get("id"), max_chars=80)
        if not runner_id:
            continue
        health = (
            runner.get("health")
            if isinstance(runner.get("health"), Mapping)
            else {}
        )
        capabilities = (
            runner.get("capabilities")
            if isinstance(runner.get("capabilities"), Mapping)
            else {}
        )
        is_live = health.get("is_live") is True
        blocked_reason = _compact_str(runner.get("blocked_reason"), max_chars=120)
        device_status = _compact_str(health.get("device_status"), max_chars=80)
        action = (
            "selected"
            if runner.get("is_active_runner") is True
            else "preferred"
            if runner.get("is_preferred") is True
            else "ready"
            if runner.get("eligible_for_active_task") is True
            else "unavailable"
        )
        title_status = device_status or ("live" if is_live else "offline")
        summary_parts = [
            _compact_str(runner.get("hostname"), max_chars=80) or "Local runner",
            title_status,
        ]
        if blocked_reason:
            summary_parts.append(blocked_reason)
        items.append(
            _feed_item(
                project_id,
                item_id=f"local_runner:{runner_id}:{action}",
                occurred_at=_parse_runtime_occurred_at(
                    runner.get("last_seen_at"),
                    runner.get("created_at"),
                ),
                source="runtime",
                resource="local_runner",
                action=action,
                title=f"Local runner {action}",
                summary=" - ".join(summary_parts),
                tone="active"
                if action in {"selected", "ready"}
                else "warning"
                if action == "unavailable"
                else "neutral",
                ref={"kind": "local_runner", "id": runner_id},
                target={
                    "surface": "runtime",
                    "section": "devices",
                    "runner_id": runner_id,
                },
                refetch_surfaces=(SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION),
                data={
                    "runner_id": runner_id,
                    "hostname": _compact_str(runner.get("hostname"), max_chars=160),
                    "is_active_runner": runner.get("is_active_runner") is True,
                    "is_preferred": runner.get("is_preferred") is True,
                    "eligible_for_active_task": runner.get("eligible_for_active_task")
                    is True,
                    "blocked_reason": blocked_reason,
                    "health": {
                        key: health.get(key)
                        for key in (
                            "device_status",
                            "device_status_reason",
                            "stale_reason",
                            "is_live",
                            "agent_available",
                            "active_work_count",
                            "active_compute_job_count",
                            "available_slots",
                            "max_concurrent",
                        )
                        if key in health
                    },
                    "capabilities": {
                        key: capabilities.get(key)
                        for key in (
                            "runner_preset",
                            "workload",
                            "has_gpu",
                            "max_concurrent",
                            "agent_available",
                        )
                        if key in capabilities
                    },
                },
            )
        )

    if not runners and (
        _bounded_int(local_runner.get("strategic_runner_count")) > 0
        or local_runner.get("active_task_pinned") is True
        or preferred_runner_id
    ):
        mode = _compact_str(local_runner.get("mode"), max_chars=80) or "unknown"
        reason = _compact_str(
            local_runner.get("fallback_reason") or local_runner.get("status_reason"),
            max_chars=120,
        )
        items.append(
            _feed_item(
                project_id,
                item_id=(
                    f"local_runner:selection:{mode}:"
                    f"{active_runner_id or preferred_runner_id or 'none'}"
                ),
                occurred_at=None,
                source="runtime",
                resource="local_runner",
                action=mode,
                title="Local runner selection",
                summary=reason,
                tone="warning" if reason else "neutral",
                target={"surface": "runtime", "section": "devices"},
                refetch_surfaces=(SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION),
                data={
                    "mode": mode,
                    "status": _compact_str(local_runner.get("status"), max_chars=80),
                    "location": _compact_str(
                        local_runner.get("location"),
                        max_chars=80,
                    ),
                    "status_reason": _compact_str(
                        local_runner.get("status_reason"),
                        max_chars=120,
                    ),
                    "active_runner_id": active_runner_id,
                    "preferred_runner_id": preferred_runner_id,
                    "strategic_runner_count": _bounded_int(
                        local_runner.get("strategic_runner_count")
                    ),
                    "live_runner_count": _bounded_int(
                        local_runner.get("live_runner_count")
                    ),
                    "eligible_live_count": _bounded_int(
                        local_runner.get("eligible_live_count")
                    ),
                },
            )
        )
    return items


def _compute_job_feed_items(
    project_id: Any,
    runtime_work: Any,
) -> list[dict[str, Any]]:
    if not isinstance(runtime_work, Mapping):
        return []
    compute_jobs = (
        runtime_work.get("compute_jobs")
        if isinstance(runtime_work.get("compute_jobs"), Mapping)
        else {}
    )
    job_items = compute_jobs.get("items")
    if not isinstance(job_items, list):
        return []
    items: list[dict[str, Any]] = []
    for job in job_items:
        if not isinstance(job, Mapping):
            continue
        job_id = _compact_str(job.get("id"), max_chars=80)
        status = _compact_str(job.get("status"), max_chars=80)
        if not job_id or not status:
            continue
        action = status.lower()
        task_title = _compact_str(job.get("task_title"), max_chars=120)
        terminal_reason = _compact_str(job.get("terminal_reason"), max_chars=180)
        title = f"Compute job {action.replace('_', ' ')}"
        if task_title:
            title = f"{title}: {task_title}"
        summary: str | None = terminal_reason
        tone = (
            "critical"
            if status in {"FAILED", "TIMED_OUT"}
            else "warning"
            if status == "CANCELLED"
            else "active"
            if status in {"QUEUED", "STARTING", "RUNNING", "POLLING"}
            else "neutral"
        )
        experiment_outcome = job.get("experiment_outcome")
        if isinstance(experiment_outcome, Mapping) and status in {
            "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"
        }:
            outcome_label = _compact_str(
                experiment_outcome.get("outcome_label"), max_chars=40
            )
            criterion_summary = _compact_str(
                experiment_outcome.get("criterion_summary"), max_chars=240
            )
            if outcome_label == "met":
                base_title = "Experiment: criterion met"
                title = f"{base_title}: {task_title}" if task_title else base_title
                tone = "active"
                summary = criterion_summary or terminal_reason
            elif outcome_label in {"unmet", "inconclusive"}:
                base_title = f"Experiment: criterion {outcome_label}"
                title = f"{base_title}: {task_title}" if task_title else base_title
                tone = "warning"
                summary = criterion_summary or terminal_reason
        data: dict[str, Any] = {
            "job_id": job_id,
            "task_id": _compact_str(job.get("task_id"), max_chars=80),
            "task_type": _compact_str(job.get("task_type"), max_chars=80),
            "status": status,
            "backend": _compact_str(job.get("backend"), max_chars=80),
            "machine_type": _compact_str(job.get("machine_type"), max_chars=120),
            "accelerator_type": _compact_str(
                job.get("accelerator_type"),
                max_chars=80,
            ),
            "accelerator_count": job.get("accelerator_count"),
            "spend_cap_usd": job.get("spend_cap_usd"),
            "cost_usd": job.get("cost_usd"),
            "started_at": _compact_str(job.get("started_at"), max_chars=80),
            "completed_at": _compact_str(job.get("completed_at"), max_chars=80),
            "last_heartbeat_at": _compact_str(
                job.get("last_heartbeat_at"),
                max_chars=80,
            ),
            "terminal_reason": terminal_reason,
        }
        if isinstance(experiment_outcome, Mapping):
            outcome_label = _compact_str(
                experiment_outcome.get("outcome_label"), max_chars=40
            )
            if outcome_label:
                data["experiment_outcome"] = {
                    "outcome_label": outcome_label,
                    "criterion_met": experiment_outcome.get("criterion_met"),
                    "criterion_summary": _compact_str(
                        experiment_outcome.get("criterion_summary"), max_chars=240
                    ),
                    "hypothesis_held": experiment_outcome.get("hypothesis_held"),
                }
        target = {"surface": "runtime", "section": "compute"}
        task_id = data["task_id"]
        if task_id:
            target["task_id"] = task_id
        items.append(
            _feed_item(
                project_id,
                item_id=f"compute_job:{job_id}:{action}",
                occurred_at=_parse_runtime_occurred_at(
                    job.get("completed_at"),
                    job.get("updated_at"),
                    job.get("started_at"),
                    job.get("created_at"),
                ),
                source="runtime",
                resource="compute_job",
                action=action,
                title=title,
                summary=summary,
                tone=tone,
                ref={"kind": "compute_job", "id": job_id},
                target=target,
                refetch_surfaces=(SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION),
                data={key: value for key, value in data.items() if value is not None},
            )
        )
    return items


async def emit_workbench_refetch(
    project_id: Any,
    *,
    resource: str,
    action: str,
    refetch_surfaces: Sequence[str] | None = None,
    summary_key: str | None = None,
    summary: Mapping[str, Any] | None = None,
    event_name: str = "workbench_refetch",
    **extra: Any,
) -> None:
    """Broadcast a generic workbench invalidation event after commit."""

    await broadcast_to_project(
        str(project_id),
        event_name,
        build_workbench_event_payload(
            project_id,
            resource=resource,
            action=action,
            refetch_surfaces=refetch_surfaces,
            summary_key=summary_key,
            summary=summary,
            **extra,
        ),
    )
