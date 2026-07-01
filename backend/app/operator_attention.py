"""Operator attention read model for the Strategic workbench.

This module derives attention from existing durable state. It intentionally does
not create a second task/message/alert lifecycle.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.message_convergence import OPEN_STATUS
from app.models import (
    Alert,
    AlertSeverity,
    MessageType,
    MessagesLastSeen,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.strategic_runtime_work import (
    local_runner_hold_task_filters,
    task_awaiting_local_runner_since,
)
from app.task_movement import build_task_movement_explanation


_HIGH_SIGNAL_MESSAGE_TYPES = (
    MessageType.QUESTION,
    MessageType.BLOCKER,
    MessageType.BUG_REPORT,
    MessageType.FEEDBACK,
)
_NEEDS_OPERATOR_MESSAGE_TYPES = (MessageType.QUESTION, MessageType.BLOCKER)
_APPROVAL_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)
_MAX_ATTENTION_ITEMS = 8
_ACTIONABLE_ALERT_SEVERITIES = (AlertSeverity.ERROR, AlertSeverity.WARNING)


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _compact_text(value: Any, *, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _direction_manual_intervention(direction: Any) -> bool:
    if not isinstance(direction, dict):
        return False
    if direction.get("needs_manual_intervention") is True:
        return True
    context = direction.get("context")
    if not isinstance(context, dict):
        return False
    return context.get("needs_manual_intervention") is True


def _direction_priority(direction: dict[str, Any]) -> int:
    priority = str(direction.get("priority") or "").lower()
    return {"critical": 0, "high": 1, "medium": 2, "normal": 3, "low": 4}.get(
        priority,
        5,
    )


def _message_attention_item(message: ProjectMessage, *, unread: bool) -> dict[str, Any]:
    message_type = _enum_value(message.message_type)
    severity = (
        "needs_operator"
        if message.message_type in _NEEDS_OPERATOR_MESSAGE_TYPES
        else "watch"
    )
    label = {
        "QUESTION": "Question waiting",
        "BLOCKER": "Blocker surfaced",
        "BUG_REPORT": "Bug report surfaced",
        "FEEDBACK": "Strategic feedback",
    }.get(message_type, "Message surfaced")
    return {
        "id": f"message:{message.id}",
        "kind": "message",
        "severity": severity,
        "reason": message_type.lower(),
        "label": label,
        "detail": _compact_text(message.content, max_chars=180),
        "unread": unread,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "ref": {
            "kind": "message",
            "id": str(message.id),
            "project_id": str(message.project_id),
        },
    }


def _task_attention_item(task: Task, *, reason: str, severity: str) -> dict[str, Any]:
    status = _enum_value(task.status)
    label = {
        "approval_required": "Approval required",
        "failed_task": "Task failed",
        "blocked_task": "Task blocked",
        "parked_task": "Task parked",
        "local_runner_hold": "Local runner needed",
    }.get(reason, "Task needs attention")
    detail = (
        task.latest_status_message
        or task.blocked_reason
        or task.park_reason
        or task.description
    )
    movement_explanation = build_task_movement_explanation(task, max_chars=180)
    return {
        "id": f"task:{task.id}:{reason}",
        "kind": "task",
        "severity": severity,
        "reason": reason,
        "label": label,
        "detail": _compact_text(detail or task.title, max_chars=180),
        "movement_explanation": movement_explanation,
        "status": status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "ref": {
            "kind": "task",
            "id": str(task.id),
            "project_id": str(task.project_id),
        },
    }


def _local_runner_hold_attention_item(task: Task) -> dict[str, Any]:
    awaiting_since = task_awaiting_local_runner_since(task)
    hold_detail = (
        f"Waiting for local runner hardware since {awaiting_since}"
        if awaiting_since
        else "Waiting for local runner hardware"
    )
    task_detail = task.latest_status_message or task.blocked_reason or task.park_reason
    if task_detail:
        hold_detail = f"{hold_detail}: {task_detail}"
    item = _task_attention_item(
        task,
        reason="local_runner_hold",
        severity="needs_operator",
    )
    item["detail"] = _compact_text(hold_detail, max_chars=180)
    item["awaiting_since"] = awaiting_since
    return item


def _alert_attention_item(alert: Alert) -> dict[str, Any]:
    alert_severity = _enum_value(alert.severity)
    severity = "needs_operator" if alert.severity == AlertSeverity.ERROR else "watch"
    return {
        "id": f"alert:{alert.id}",
        "kind": "alert",
        "severity": severity,
        "reason": "alert",
        "label": alert.title,
        "detail": _compact_text(alert.message, max_chars=180),
        "status": alert_severity,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "updated_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None,
        "ref": {
            "kind": "alert",
            "id": str(alert.id),
            "project_id": str(alert.project_id),
        },
        "task_id": str(alert.task_id) if alert.task_id else None,
        "domain": alert.domain,
        "event_type": alert.event_type,
    }


def _direction_attention_item(
    project_id: uuid.UUID,
    direction: dict[str, Any],
) -> dict[str, Any]:
    direction_id = str(direction.get("id") or "unknown")
    detail = direction.get("needs_manual_intervention_reason") or direction.get("text")
    return {
        "id": f"direction:{direction_id}:manual_intervention",
        "kind": "direction",
        "severity": "needs_operator",
        "reason": "manual_intervention",
        "label": "Direction needs intervention",
        "detail": _compact_text(detail, max_chars=180),
        "ref": {
            "kind": "direction",
            "id": direction_id,
            "project_id": str(project_id),
        },
    }


async def build_operator_attention(
    session: AsyncSession,
    project: Project,
    user_id: str,
) -> dict[str, Any]:
    """Build the operator attention block for a project.

    The result is bounded and intentionally lossy: enough for the workbench rail
    to tell the operator what deserves a look, while deeper truth stays in the
    existing messages, task, direction, and runtime endpoints.
    """

    last_seen_at = (
        await session.execute(
            select(MessagesLastSeen.last_seen_at).where(
                MessagesLastSeen.project_id == project.id,
                MessagesLastSeen.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    message_rows = list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type != "user",
                    ProjectMessage.message_type.in_(list(_HIGH_SIGNAL_MESSAGE_TYPES)),
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(24)
            )
        )
        .scalars()
        .all()
    )
    message_count_rows = (
        await session.execute(
            select(ProjectMessage.message_type, func.count())
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status == OPEN_STATUS,
                ProjectMessage.from_type != "user",
                ProjectMessage.message_type.in_(list(_HIGH_SIGNAL_MESSAGE_TYPES)),
            )
            .group_by(ProjectMessage.message_type)
        )
    ).all()
    message_counts = {message_type: int(count or 0) for message_type, count in message_count_rows}
    open_question_count = message_counts.get(MessageType.QUESTION, 0)
    open_blocker_count = message_counts.get(MessageType.BLOCKER, 0)
    bug_report_count = message_counts.get(MessageType.BUG_REPORT, 0)
    feedback_count = message_counts.get(MessageType.FEEDBACK, 0)
    unread_count_filters: list[Any] = []
    if last_seen_at is not None:
        unread_count_filters.append(ProjectMessage.created_at > last_seen_at)
    unread_message_count = int(
        (
            await session.execute(
                select(func.count()).select_from(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type != "user",
                    ProjectMessage.message_type.in_(list(_HIGH_SIGNAL_MESSAGE_TYPES)),
                    *unread_count_filters,
                )
            )
        ).scalar()
        or 0
    )

    unread_display_messages = [
        message
        for message in message_rows
        if last_seen_at is None
        or (message.created_at is not None and message.created_at > last_seen_at)
    ]
    unread_message_ids = {message.id for message in unread_display_messages}
    open_questions = [
        message
        for message in message_rows
        if message.message_type == MessageType.QUESTION
    ]
    open_blockers = [
        message
        for message in message_rows
        if message.message_type == MessageType.BLOCKER
    ]
    watch_messages = [
        message
        for message in message_rows
        if message.message_type in (MessageType.BUG_REPORT, MessageType.FEEDBACK)
    ]

    task_count_result = (
        await session.execute(
            select(
                func.sum(case(
                    (
                        and_(
                            Task.requires_approval.is_(True),
                            Task.status.in_(list(_APPROVAL_TASK_STATUSES)),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("approval"),
                func.sum(case(
                    (Task.status == TaskStatus.FAILED, 1),
                    else_=0,
                )).label("failed"),
                func.sum(case(
                    (
                        and_(
                            Task.status == TaskStatus.BLOCKED,
                            Task.requires_approval.is_not(True),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("blocked"),
                func.sum(case(
                    (
                        and_(
                            Task.status == TaskStatus.PARKED,
                            Task.requires_approval.is_not(True),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("parked"),
            )
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.archived.is_(False),
            )
        )
    ).one()

    approval_count = int(task_count_result.approval or 0)
    failed_count = int(task_count_result.failed or 0)
    blocked_count = int(task_count_result.blocked or 0)
    parked_count = int(task_count_result.parked or 0)

    local_runner_hold_filters = local_runner_hold_task_filters(project.id)
    local_runner_hold_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Task).where(*local_runner_hold_filters)
            )
        ).scalar()
        or 0
    )
    local_runner_hold_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(*local_runner_hold_filters)
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )

    alert_count_rows = (
        await session.execute(
            select(Alert.severity, func.count()).where(
                Alert.project_id == project.id,
                Alert.dismissed_at.is_(None),
                Alert.severity.in_(list(_ACTIONABLE_ALERT_SEVERITIES)),
            ).group_by(Alert.severity)
        )
    ).all()
    error_alert_count = 0
    warning_alert_count = 0
    for severity, count in alert_count_rows:
        if severity == AlertSeverity.ERROR:
            error_alert_count = int(count or 0)
        elif severity == AlertSeverity.WARNING:
            warning_alert_count = int(count or 0)

    approval_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.requires_approval.is_(True),
                    Task.status.in_(list(_APPROVAL_TASK_STATUSES)),
                )
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )
    failed_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.status == TaskStatus.FAILED,
                )
                .order_by(Task.updated_at.desc())
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    held_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.status.in_([TaskStatus.BLOCKED, TaskStatus.PARKED]),
                    Task.requires_approval.is_not(True),
                )
                .order_by(Task.updated_at.desc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )
    alerts = list(
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.severity.in_(list(_ACTIONABLE_ALERT_SEVERITIES)),
                )
                .order_by(Alert.created_at.desc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )

    directions = project.directions if isinstance(project.directions, list) else []
    manual_directions = [
        direction
        for direction in directions
        if isinstance(direction, dict)
        and _direction_manual_intervention(direction)
        and direction.get("status") == "active"
    ]
    manual_directions.sort(key=_direction_priority)

    items: list[dict[str, Any]] = []

    def add_item(item: dict[str, Any]) -> None:
        if len(items) < _MAX_ATTENTION_ITEMS:
            items.append(item)

    for message in [*open_questions, *open_blockers]:
        add_item(
            _message_attention_item(
                message,
                unread=message.id in unread_message_ids,
            )
        )
    for task in approval_tasks:
        add_item(
            _task_attention_item(
                task,
                reason="approval_required",
                severity="needs_operator",
            )
        )
    for task in local_runner_hold_tasks:
        add_item(_local_runner_hold_attention_item(task))
    for direction in manual_directions[:3]:
        add_item(_direction_attention_item(project.id, direction))
    for alert in alerts:
        add_item(_alert_attention_item(alert))
    for task in held_tasks:
        reason = "parked_task" if task.status == TaskStatus.PARKED else "blocked_task"
        add_item(_task_attention_item(task, reason=reason, severity="watch"))
    for task in failed_tasks:
        add_item(_task_attention_item(task, reason="failed_task", severity="watch"))
    for message in watch_messages:
        add_item(
            _message_attention_item(
                message,
                unread=message.id in unread_message_ids,
            )
        )

    needs_operator_count = sum(
        1 for item in items if item["severity"] == "needs_operator"
    )
    watch_count = sum(1 for item in items if item["severity"] == "watch")
    total_needs_operator = (
        open_question_count
        + open_blocker_count
        + approval_count
        + local_runner_hold_count
        + len(manual_directions)
        + error_alert_count
    )
    total_watch = (
        failed_count
        + blocked_count
        + parked_count
        + warning_alert_count
        + bug_report_count
        + feedback_count
    )
    level = (
        "needs_operator"
        if total_needs_operator
        else "watch"
        if total_watch
        else "clear"
    )
    if total_needs_operator:
        plural = "s" if total_needs_operator != 1 else ""
        summary = f"{total_needs_operator} operator item{plural} need review"
    elif total_watch:
        plural = "s" if total_watch != 1 else ""
        summary = f"{total_watch} pipeline item{plural} worth watching"
    else:
        summary = "No operator action is currently flagged"

    return {
        "level": level,
        "summary": summary,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "counts": {
            "needs_operator": total_needs_operator,
            "watch": total_watch,
            "visible_needs_operator": needs_operator_count,
            "visible_watch": watch_count,
            "unread": unread_message_count,
            "questions": open_question_count,
            "blockers": open_blocker_count,
            "bug_reports": bug_report_count,
            "feedback": feedback_count,
            "approvals": approval_count,
            "local_runner_holds": local_runner_hold_count,
            "manual_directions": len(manual_directions),
            "alerts": error_alert_count + warning_alert_count,
            "error_alerts": error_alert_count,
            "warning_alerts": warning_alert_count,
            "failed_tasks": failed_count,
            "blocked_tasks": blocked_count,
            "parked_tasks": parked_count,
        },
        "items": items,
    }
