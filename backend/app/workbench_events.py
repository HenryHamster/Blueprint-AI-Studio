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

WORKBENCH_EVENT_SCHEMA_VERSION = 1

SURFACE_MESSAGES = "messages"
SURFACE_STRATEGIC_SESSION = "strategic_session"
SURFACE_DIRECTIONS = "directions"
SURFACE_PIPELINE = "pipeline"
SURFACE_RUNTIME = "runtime"

DEFAULT_WORKBENCH_REFETCH_SURFACES = (SURFACE_STRATEGIC_SESSION,)
DEFAULT_WORKBENCH_EVENT_FEED_LIMIT = 24
_SOURCE_QUERY_LIMIT = 12


def _compact_str(value: Any, *, max_chars: int = 160) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}..."


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
    payloads = task.payloads if isinstance(task.payloads, list) else []
    first = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
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
    surfaces: list[str] = []
    seen: set[str] = set()
    for raw in values or DEFAULT_WORKBENCH_REFETCH_SURFACES:
        surface = _compact_str(raw, max_chars=80)
        if not surface or surface in seen:
            continue
        surfaces.append(surface)
        seen.add(surface)
        if len(surfaces) >= 8:
            break
    return surfaces


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
        "refetch": {"surfaces": _bounded_surfaces(refetch_surfaces)},
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
    if target:
        payload["target"] = dict(target)
    if data:
        payload["data"] = dict(data)
    return payload


def _message_feed_item(message: ProjectMessage) -> dict[str, Any]:
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
    task_activity_source_limit = max(source_limit, min(200, bounded_limit * 4))
    task_base_query = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.payloads,
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
    task_lifecycle_rows = (
        await session.execute(
            task_base_query
            .order_by(task_occurred_at.desc())
            .limit(source_limit)
        )
    ).scalars().all()
    task_activity_rows = (
        await session.execute(
            task_base_query
            .order_by(task_recent_activity.desc())
            .limit(task_activity_source_limit)
        )
    ).scalars().all()
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
    ]
    items.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return {
        "version": 1,
        "limit": bounded_limit,
        "has_more": len(items) > bounded_limit,
        "items": items[:bounded_limit],
    }


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
