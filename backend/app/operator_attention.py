"""Operator attention read model for the Strategic workbench.

This module derives attention from existing durable state. It intentionally does
not create a second task/message/alert lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.message_convergence import OPEN_STATUS
from app.failure_classification import is_non_death_cancellation
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    LocalRunner,
    MessageType,
    MessagesLastSeen,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
    VmContact,
)
from app.reaper_orphan_classify import _classify_orphan
from app.routers.admin_runners import _runner_health
from app.strategic_runtime_work import (
    local_runner_hold_task_filters,
    strategic_local_runner_hold_task_filters,
    task_awaiting_local_runner_since,
    task_awaiting_strategic_local_runner_since,
    task_is_local_runner_hold,
    task_is_strategic_local_runner_hold,
)
from app.task_movement import build_task_movement_explanation
from app.time_utils import _utcnow
from app.workbench_contracts import compact_text, normalize_target


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
_STALE_DEPENDENCY_QUEUE_AFTER = timedelta(minutes=10)
_STALE_TASK_PREVIEW_LIMIT = 4
_MESSAGE_BLOCKER_PREVIEW_LIMIT = 8
_MESSAGE_QUESTION_PREVIEW_LIMIT = 8
_MESSAGE_WATCH_PREVIEW_LIMIT = 24
_ALERT_ERROR_PREVIEW_LIMIT = 4
_ALERT_WARNING_PREVIEW_LIMIT = 4
_DEAD_RUNNER_PREVIEW_LIMIT = 4

_ATTENTION_VISIBLE_PRIORITY: dict[tuple[str, str], int] = {
    ("needs_operator", "approval_required"): 10,
    ("needs_operator", "alert"): 20,
    ("needs_operator", "local_runner_hold"): 30,
    ("needs_operator", "stale_runner_pin"): 40,
    ("needs_operator", "dead_runner"): 45,
    ("needs_operator", "stale_provisioning_task"): 50,
    ("needs_operator", "stale_queued_task"): 60,
    ("needs_operator", "manual_intervention"): 70,
    ("needs_operator", "blocker"): 80,
    ("needs_operator", "question"): 90,
    ("watch", "alert"): 200,
    ("watch", "dead_runner"): 205,
    ("watch", "failed_task"): 210,
    ("watch", "blocked_task"): 220,
    ("watch", "parked_task"): 230,
    ("watch", "bug_report"): 240,
    ("watch", "feedback"): 250,
}
_DEFAULT_NEEDS_OPERATOR_VISIBLE_PRIORITY = 100
_DEFAULT_WATCH_VISIBLE_PRIORITY = 300

_ATTENTION_LIFECYCLE_BY_REASON: dict[str, dict[str, Any]] = {
    "question": {
        "mode": "message_lifecycle",
        "clears_when": "message_resolved_or_deleted",
        "operator_action": "reply_or_resolve_message",
        "acknowledgeable": True,
        "acknowledge_effect": "clears_unread_only",
    },
    "blocker": {
        "mode": "message_lifecycle",
        "clears_when": "message_resolved_or_deleted",
        "operator_action": "inspect_or_resolve_message",
        "acknowledgeable": True,
        "acknowledge_effect": "clears_unread_only",
    },
    "bug_report": {
        "mode": "message_lifecycle",
        "clears_when": "message_resolved_or_deleted",
        "operator_action": "inspect_or_resolve_message",
        "acknowledgeable": True,
        "acknowledge_effect": "clears_unread_only",
    },
    "feedback": {
        "mode": "message_lifecycle",
        "clears_when": "message_resolved_or_deleted",
        "operator_action": "inspect_or_resolve_message",
        "acknowledgeable": True,
        "acknowledge_effect": "clears_unread_only",
    },
    "approval_required": {
        "mode": "task_lifecycle",
        "clears_when": "approval_resolved_or_task_terminal_or_archived",
        "operator_action": "approve_or_reject_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "local_runner_hold": {
        "mode": "runtime_lifecycle",
        "clears_when": "eligible_runner_available_or_task_dispatches_expires_terminal_or_archived",
        "operator_action": "connect_or_select_local_runner",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "stale_runner_pin": {
        "mode": "runtime_lifecycle",
        "clears_when": "runner_recovers_task_unpins_dispatches_terminal_or_archived",
        "operator_action": "recover_runner_or_retarget_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "dead_runner": {
        "mode": "runtime_lifecycle",
        "clears_when": "runner_heartbeats_back_to_live_or_is_deleted",
        "operator_action": "recover_or_remove_runner",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "stale_provisioning_task": {
        "mode": "task_lifecycle",
        "clears_when": "task_claims_healthy_runtime_recovers_terminal_or_archived",
        "operator_action": "inspect_runtime_or_requeue_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "stale_queued_task": {
        "mode": "task_lifecycle",
        "clears_when": "dependency_recovers_task_leaves_queue_terminal_or_archived",
        "operator_action": "inspect_dependency_or_requeue_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "manual_intervention": {
        "mode": "direction_lifecycle",
        "clears_when": "direction_flag_cleared_or_direction_inactive",
        "operator_action": "update_direction",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "alert": {
        "mode": "alert_lifecycle",
        "clears_when": "alert_dismissed_or_resolved_at_source",
        "operator_action": "dismiss_or_fix_alert_source",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "blocked_task": {
        "mode": "task_lifecycle",
        "clears_when": "task_unblocked_terminal_or_archived",
        "operator_action": "inspect_blocked_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "parked_task": {
        "mode": "task_lifecycle",
        "clears_when": "task_unparked_terminal_or_archived",
        "operator_action": "inspect_parked_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "failed_task": {
        "mode": "task_lifecycle",
        "clears_when": "task_requeued_archived_or_otherwise_resolved",
        "operator_action": "inspect_failed_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
}

_DEFAULT_ATTENTION_LIFECYCLE = {
    "mode": "source_lifecycle",
    "clears_when": "underlying_source_clears",
    "operator_action": "inspect_source",
    "acknowledgeable": False,
    "acknowledge_effect": "none",
}


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _compact_text(value: Any, *, max_chars: int = 180) -> str:
    return compact_text(value, max_chars=max_chars)


def _attention_target(target: dict[str, Any]) -> dict[str, Any]:
    return normalize_target(target) or {}


def _attention_lifecycle(reason: Any) -> dict[str, Any]:
    reason_key = str(reason or "")
    return dict(
        _ATTENTION_LIFECYCLE_BY_REASON.get(
            reason_key,
            _DEFAULT_ATTENTION_LIFECYCLE,
        )
    )


def _attention_visible_priority(item: dict[str, Any]) -> int:
    severity = str(item.get("severity") or "")
    reason = str(item.get("reason") or "")
    mapped_priority = _ATTENTION_VISIBLE_PRIORITY.get((severity, reason))
    if mapped_priority is not None:
        return mapped_priority
    if severity == "needs_operator":
        return _DEFAULT_NEEDS_OPERATOR_VISIBLE_PRIORITY
    if severity == "watch":
        return _DEFAULT_WATCH_VISIBLE_PRIORITY
    return _DEFAULT_WATCH_VISIBLE_PRIORITY + 100


def _attention_timestamp_sort_value(item: dict[str, Any]) -> float:
    timestamp = item.get("updated_at") or item.get("created_at")
    if not isinstance(timestamp, str) or not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _attention_visible_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    return (
        int(item.get("_visible_priority", 999)),
        -_attention_timestamp_sort_value(item),
        str(item.get("id") or ""),
    )


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
        "target": _attention_target({
            "surface": "messages",
            "project_id": str(message.project_id),
            "message_id": str(message.id),
            "action": "reply" if message.message_type == MessageType.QUESTION else "highlight",
        }),
    }


def _task_attention_item(task: Task, *, reason: str, severity: str) -> dict[str, Any]:
    status = _enum_value(task.status)
    label = {
        "approval_required": "Approval required",
        "failed_task": "Task failed",
        "blocked_task": "Task blocked",
        "parked_task": "Task parked",
        "local_runner_hold": "Local runner needed",
        "stale_runner_pin": "Runner pin stale",
        "stale_provisioning_task": "Provisioning stale",
        "stale_queued_task": "Dependency queue stale",
    }.get(reason, "Task needs attention")
    detail = (
        task.latest_status_message
        or task.blocked_reason
        or task.park_reason
        or task.description
    )
    movement_explanation = build_task_movement_explanation(task, max_chars=180)
    target = {
        "surface": "task",
        "project_id": str(task.project_id),
        "task_id": str(task.id),
    }
    if reason == "approval_required":
        target["action"] = "approve"
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
        "target": _attention_target(target),
    }


def _local_runner_hold_attention_item(task: Task) -> dict[str, Any]:
    awaiting_since = (
        task_awaiting_local_runner_since(task)
        or task_awaiting_strategic_local_runner_since(task)
    )
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
    item["target"] = _attention_target({
        "surface": "runtime",
        "project_id": str(task.project_id),
        "section": "devices",
        "task_id": str(task.id),
    })
    return item


def _runner_id_from_task(task: Task) -> str | None:
    context = task.context or {}
    runner_target_id = context.get("runner_target_id")
    if isinstance(runner_target_id, str) and runner_target_id.strip():
        return runner_target_id.strip()
    vm_instance_id = task.vm_instance_id
    if isinstance(vm_instance_id, str) and vm_instance_id.startswith("runner:"):
        runner_id = vm_instance_id.removeprefix("runner:").strip()
        return runner_id or None
    return None


def _stale_runner_pin_attention_item(
    task: Task,
    *,
    runner: LocalRunner | None,
) -> dict[str, Any]:
    runner_id = _runner_id_from_task(task)
    if runner is None:
        detail = "Task is pinned to a runner that is no longer registered."
    else:
        health = _runner_health(runner)
        reason = health.get("stale_reason") or health.get("device_status_reason")
        seconds = health.get("seconds_since_last_seen")
        if isinstance(seconds, int):
            detail = (
                f"Runner has not checked in for {seconds}s"
                if reason == "heartbeat_stale"
                else f"Runner is not ready ({reason or 'offline'})"
            )
        else:
            detail = f"Runner is not ready ({reason or 'never_seen'})"
    if task.latest_status_message:
        detail = f"{detail}: {task.latest_status_message}"
    item = _task_attention_item(
        task,
        reason="stale_runner_pin",
        severity="needs_operator",
    )
    item["detail"] = _compact_text(detail, max_chars=180)
    item["runner_id"] = runner_id
    item["target"] = _attention_target({
        "surface": "runtime",
        "project_id": str(task.project_id),
        "section": "devices",
        "task_id": str(task.id),
    })
    if runner_id:
        item["target"]["runner_id"] = runner_id
    return item


def _stale_task_attention_item(task: Task, *, reason: str) -> dict[str, Any]:
    item = _task_attention_item(task, reason=reason, severity="needs_operator")
    if reason == "stale_queued_task":
        dep_count = len(task.depends_on or [])
        dep_label = "dependency" if dep_count == 1 else "dependencies"
        detail = (
            task.latest_status_message
            or task.blocked_reason
            or f"Queued for more than 10 minutes behind {dep_count} {dep_label}."
        )
    else:
        detail = (
            task.latest_status_message
            or "Provisioning exceeded the existing stale-runner recovery threshold."
        )
    item["detail"] = _compact_text(detail, max_chars=180)
    return item


def _alert_attention_item(alert: Alert) -> dict[str, Any]:
    alert_severity = _enum_value(alert.severity)
    severity = "needs_operator" if alert.severity == AlertSeverity.ERROR else "watch"
    event_route_text = f"{alert.event_type or ''} {alert.domain or ''}"
    target_section = "devices" if "runner" in event_route_text else "health"
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
        "target": _attention_target({
            "surface": "task" if alert.task_id else "runtime",
            "project_id": str(alert.project_id),
            **({"task_id": str(alert.task_id)} if alert.task_id else {}),
            **({} if alert.task_id else {"section": target_section}),
            "alert_id": str(alert.id),
        }),
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
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "directions",
            "direction_id": direction_id,
        }),
    }


def _runner_pin_filter(project_id: uuid.UUID) -> tuple[Any, ...]:
    return (
        Task.project_id == project_id,
        Task.archived.is_(False),
        Task.status == TaskStatus.PROVISIONING,
        Task.requires_approval.is_not(True),
        Task.started_at.is_(None),
        Task.vm_instance_id.like("runner:%"),
    )


def _runner_is_stale_or_missing(runner: LocalRunner | None) -> bool:
    if runner is None:
        return True
    return not bool(_runner_health(runner).get("is_live"))


async def _load_stale_runner_pins(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, list[tuple[Task, LocalRunner | None]]]:
    pinned_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(*_runner_pin_filter(project_id))
                .order_by(Task.priority.asc(), Task.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    runner_ids: set[uuid.UUID] = set()
    runner_id_by_task: dict[uuid.UUID, str] = {}
    for task in pinned_tasks:
        runner_id = _runner_id_from_task(task)
        if not runner_id:
            continue
        runner_id_by_task[task.id] = runner_id
        try:
            runner_ids.add(uuid.UUID(runner_id))
        except ValueError:
            continue
    runners_by_id: dict[str, LocalRunner] = {}
    if runner_ids:
        runners_by_id = {
            str(runner.id): runner
            for runner in (
                await session.execute(
                    select(LocalRunner).where(
                        LocalRunner.project_id == project_id,
                        LocalRunner.id.in_(runner_ids),
                    )
                )
            )
            .scalars()
            .all()
        }
    stale = [
        (task, runners_by_id.get(runner_id_by_task.get(task.id, "")))
        for task in pinned_tasks
        if _runner_is_stale_or_missing(
            runners_by_id.get(runner_id_by_task.get(task.id, ""))
        )
    ]
    return len(stale), stale[:_STALE_TASK_PREVIEW_LIMIT]


def _runner_is_dead(runner: LocalRunner) -> bool:
    """A registered runner that WAS live but is now offline/stale.

    True when the runner is not live AND either it has been swept ``inactive`` or
    it heartbeated at least once before going stale. A never-heartbeated ``active``
    runner is excluded — that is a booting/just-registered runner, not a death, so
    surfacing it would be a false positive (the stale-runner sweep will flip it to
    ``inactive`` after the liveness window if it never comes up, and it surfaces
    then via its runner-deactivated alert).
    """
    if bool(_runner_health(runner).get("is_live")):
        return False
    return runner.status != "active" or runner.last_seen_at is not None


async def _load_dead_runners(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, int, list[LocalRunner]]:
    """Registered runners that have died/stalled, independent of any pinned task.

    Fills the gap where an IDLE registered runner (or one running a pulled,
    mid-job task) could go offline and surface nothing in the attention feed:
    the only prior runner signal, :func:`_stale_runner_pin_attention_item`,
    required a PROVISIONING task pinned-but-UNPULLED on it, so an idle runner or
    one whose task the runner already pulled produced nothing. Two exclusions
    keep it non-redundant:

    * runners already surfaced by a stale unpulled-pin item (they carry an
      ``_runner_pin_filter`` pin) — the task-centric item covers them; and
    * runners with an OPEN runner-deactivated alert — that alert already appears
      via :func:`_alert_attention_item` (routed to the ``devices`` section).

    Returns ``(needs_operator_count, watch_count, preview_runners)``: a dead
    runner that had active work in flight is a ``needs_operator`` item (its work
    may be interrupted), an idle one is a ``watch`` item — mirroring the
    ERROR/WARNING split of the runner-deactivated alert.
    """
    runners = list(
        (
            await session.execute(
                select(LocalRunner).where(
                    LocalRunner.project_id == project_id,
                    LocalRunner.status != "deleted",
                )
            )
        )
        .scalars()
        .all()
    )
    dead = [runner for runner in runners if _runner_is_dead(runner)]
    if not dead:
        return 0, 0, []
    # Exclude runners already surfaced by an unpulled-pin (stale_runner_pin) item.
    pin_vm_ids = (
        await session.execute(
            select(Task.vm_instance_id).where(*_runner_pin_filter(project_id))
        )
    ).scalars().all()
    pinned_runner_ids = {
        vm.removeprefix("runner:")
        for vm in pin_vm_ids
        if isinstance(vm, str) and vm.startswith("runner:")
    }
    dead = [runner for runner in dead if str(runner.id) not in pinned_runner_ids]
    if not dead:
        return 0, 0, []
    from app.alert_dispatch import _runner_deactivated_dedupe_key

    dedupe_keys = {_runner_deactivated_dedupe_key(runner.id) for runner in dead}
    alerted_keys = set(
        (
            await session.execute(
                select(Alert.dedupe_key).where(
                    Alert.project_id == project_id,
                    Alert.dismissed_at.is_(None),
                    Alert.dedupe_key.in_(dedupe_keys),
                )
            )
        )
        .scalars()
        .all()
    )
    uncovered = [
        runner
        for runner in dead
        if _runner_deactivated_dedupe_key(runner.id) not in alerted_keys
    ]
    if not uncovered:
        return 0, 0, []
    uncovered.sort(key=lambda runner: runner.last_seen_at or runner.created_at or _utcnow())
    needs_operator_count = sum(
        1
        for runner in uncovered
        if int(_runner_health(runner).get("active_work_count") or 0) > 0
    )
    watch_count = len(uncovered) - needs_operator_count
    return needs_operator_count, watch_count, uncovered[:_DEAD_RUNNER_PREVIEW_LIMIT]


def _dead_runner_attention_item(runner: LocalRunner) -> dict[str, Any]:
    health = _runner_health(runner)
    active_work = int(health.get("active_work_count") or 0)
    had_active_work = active_work > 0
    device_status = health.get("device_status") or "offline"
    reason_text = (
        health.get("stale_reason") or health.get("device_status_reason") or "offline"
    )
    seconds = health.get("seconds_since_last_seen")
    label = runner.hostname or f"Runner {str(runner.id)[:8]}"
    if had_active_work:
        plural = "s" if active_work != 1 else ""
        detail = (
            f"Runner '{label}' is {device_status} ({reason_text}) with "
            f"{active_work} active task{plural} — the work may be interrupted."
        )
    elif isinstance(seconds, int):
        detail = (
            f"Runner '{label}' is {device_status} — last heartbeat {seconds}s ago "
            f"({reason_text})."
        )
    else:
        detail = f"Runner '{label}' is {device_status} ({reason_text})."
    item: dict[str, Any] = {
        "id": f"runner:{runner.id}:offline",
        "kind": "runner",
        "severity": "needs_operator" if had_active_work else "watch",
        "reason": "dead_runner",
        "label": (
            "Fleet runner offline mid-job"
            if had_active_work
            else "Fleet runner offline"
        ),
        "detail": _compact_text(detail, max_chars=180),
        "status": device_status,
        "created_at": runner.created_at.isoformat() if runner.created_at else None,
        "updated_at": (
            runner.last_seen_at.isoformat() if runner.last_seen_at else None
        ),
        "runner_id": str(runner.id),
        "ref": {
            "kind": "runner",
            "id": str(runner.id),
            "project_id": str(runner.project_id),
        },
        "target": _attention_target(
            {
                "surface": "runtime",
                "project_id": str(runner.project_id),
                "section": "devices",
                "runner_id": str(runner.id),
            }
        ),
    }
    return item


async def _load_stale_provisioning_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, list[Task]]:
    now = _utcnow()
    candidates = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.archived.is_(False),
                    Task.status == TaskStatus.PROVISIONING,
                    Task.requires_approval.is_not(True),
                    (Task.vm_instance_id.is_(None) | ~Task.vm_instance_id.like("runner:%")),
                )
                .order_by(Task.priority.asc(), Task.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return 0, []

    task_ids = [task.id for task in candidates]
    pr_task_ids = set(
        (
            await session.execute(
                select(Artifact.task_id).where(
                    Artifact.task_id.in_(task_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .all()
    )
    vm_ids = {task.vm_instance_id for task in candidates if task.vm_instance_id}
    vm_contact_lookup: dict[str, Any] = {}
    vm_tool_use_lookup: dict[str, Any] = {}
    if vm_ids:
        contact_rows = (
            await session.execute(
                select(
                    VmContact.vm_instance_id,
                    VmContact.last_contact_at,
                    VmContact.last_tool_use_at,
                ).where(VmContact.vm_instance_id.in_(vm_ids))
            )
        ).all()
        vm_contact_lookup = {
            vm_id: last_contact
            for vm_id, last_contact, _last_tool in contact_rows
            if last_contact is not None
        }
        vm_tool_use_lookup = {
            vm_id: last_tool
            for vm_id, _last_contact, last_tool in contact_rows
            if last_tool is not None
        }

    stale = [
        task
        for task in candidates
        if _classify_orphan(
            task,
            now,
            has_open_pr=task.id in pr_task_ids,
            vm_contact_lookup=vm_contact_lookup,
            vm_tool_use_lookup=vm_tool_use_lookup,
        )
    ]
    return len(stale), stale[:_STALE_TASK_PREVIEW_LIMIT]


async def _load_stale_queued_dependency_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, list[Task]]:
    cutoff = _utcnow() - _STALE_DEPENDENCY_QUEUE_AFTER
    candidates = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.archived.is_(False),
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval.is_not(True),
                    Task.created_at < cutoff,
                )
                .order_by(Task.priority.asc(), Task.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        task
        for task in candidates
        if (task.depends_on or [])
        and not task_is_local_runner_hold(task)
        and not task_is_strategic_local_runner_hold(task)
    ]
    if not candidates:
        return 0, []

    dep_ids_by_task: dict[uuid.UUID, set[uuid.UUID]] = {}
    malformed_dep_task_ids: set[uuid.UUID] = set()
    all_dep_ids: set[uuid.UUID] = set()
    for task in candidates:
        dep_ids: set[uuid.UUID] = set()
        for dep in task.depends_on or []:
            try:
                dep_id = uuid.UUID(str(dep))
            except (TypeError, ValueError, AttributeError):
                malformed_dep_task_ids.add(task.id)
                continue
            dep_ids.add(dep_id)
            all_dep_ids.add(dep_id)
        dep_ids_by_task[task.id] = dep_ids

    dep_tasks_by_id: dict[uuid.UUID, Task] = {}
    if all_dep_ids:
        dep_tasks_by_id = {
            task.id: task
            for task in (
                await session.execute(select(Task).where(Task.id.in_(all_dep_ids)))
            )
            .scalars()
            .all()
        }

    def dependency_needs_operator(dep_task: Task | None) -> bool:
        if dep_task is None:
            return True
        if dep_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return not is_non_death_cancellation(dep_task)
        return dep_task.status == TaskStatus.BLOCKED and bool(
            (dep_task.context or {}).get("_blocked_escalated")
        )

    stale = []
    for task in candidates:
        if task.id in malformed_dep_task_ids:
            stale.append(task)
            continue
        dep_ids = dep_ids_by_task.get(task.id, set())
        if not dep_ids:
            continue
        if any(
            dependency_needs_operator(dep_tasks_by_id.get(dep_id))
            for dep_id in dep_ids
        ):
            stale.append(task)
    return len(stale), stale[:_STALE_TASK_PREVIEW_LIMIT]


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

    blocker_message_rows = list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type != "user",
                    ProjectMessage.message_type == MessageType.BLOCKER,
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(_MESSAGE_BLOCKER_PREVIEW_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    question_message_rows = list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type != "user",
                    ProjectMessage.message_type == MessageType.QUESTION,
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(_MESSAGE_QUESTION_PREVIEW_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    watch_message_rows = list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type != "user",
                    ProjectMessage.message_type.in_(
                        [MessageType.BUG_REPORT, MessageType.FEEDBACK]
                    ),
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(_MESSAGE_WATCH_PREVIEW_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    message_rows = [*blocker_message_rows, *question_message_rows, *watch_message_rows]
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
    strategic_local_runner_hold_filters = strategic_local_runner_hold_task_filters(
        project.id
    )
    local_runner_hold_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Task).where(*local_runner_hold_filters)
            )
        ).scalar()
        or 0
    )
    strategic_local_runner_hold_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(*strategic_local_runner_hold_filters)
            )
        ).scalar()
        or 0
    )
    compute_local_runner_hold_tasks = list(
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
    strategic_local_runner_hold_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(*strategic_local_runner_hold_filters)
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )
    local_runner_hold_tasks = [
        *compute_local_runner_hold_tasks,
        *[
            task
            for task in strategic_local_runner_hold_tasks
            if all(task.id != existing.id for existing in compute_local_runner_hold_tasks)
        ],
    ]
    local_runner_hold_tasks.sort(
        key=lambda task: (task.priority, task.created_at or _utcnow())
    )
    local_runner_hold_tasks = local_runner_hold_tasks[:4]
    total_local_runner_hold_count = (
        local_runner_hold_count + strategic_local_runner_hold_count
    )

    stale_runner_pin_count, stale_runner_pin_tasks = await _load_stale_runner_pins(
        session,
        project.id,
    )
    (
        dead_runner_needs_operator_count,
        dead_runner_watch_count,
        dead_runners,
    ) = await _load_dead_runners(session, project.id)
    (
        stale_provisioning_count,
        stale_provisioning_tasks,
    ) = await _load_stale_provisioning_tasks(session, project.id)
    (
        stale_queued_count,
        stale_queued_tasks,
    ) = await _load_stale_queued_dependency_tasks(session, project.id)

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
    error_alerts = list(
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.severity == AlertSeverity.ERROR,
                )
                .order_by(Alert.created_at.desc())
                .limit(_ALERT_ERROR_PREVIEW_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    warning_alerts = list(
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                )
                .order_by(Alert.created_at.desc())
                .limit(_ALERT_WARNING_PREVIEW_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    alerts = [*error_alerts, *warning_alerts]

    directions = project.directions if isinstance(project.directions, list) else []
    manual_directions = [
        direction
        for direction in directions
        if isinstance(direction, dict)
        and _direction_manual_intervention(direction)
        and direction.get("status") == "active"
    ]
    manual_directions.sort(key=_direction_priority)

    candidate_items: list[dict[str, Any]] = []

    def add_candidate(item: dict[str, Any]) -> None:
        item.setdefault("lifecycle", _attention_lifecycle(item.get("reason")))
        item["_visible_priority"] = _attention_visible_priority(item)
        candidate_items.append(item)

    for message in [*open_questions, *open_blockers]:
        add_candidate(
            _message_attention_item(
                message,
                unread=message.id in unread_message_ids,
            )
        )
    for task in approval_tasks:
        add_candidate(
            _task_attention_item(
                task,
                reason="approval_required",
                severity="needs_operator",
            )
        )
    for task in local_runner_hold_tasks:
        add_candidate(_local_runner_hold_attention_item(task))
    for task, runner in stale_runner_pin_tasks:
        add_candidate(_stale_runner_pin_attention_item(task, runner=runner))
    for runner in dead_runners:
        add_candidate(_dead_runner_attention_item(runner))
    for task in stale_provisioning_tasks:
        add_candidate(
            _stale_task_attention_item(task, reason="stale_provisioning_task")
        )
    for task in stale_queued_tasks:
        add_candidate(_stale_task_attention_item(task, reason="stale_queued_task"))
    for direction in manual_directions[:3]:
        add_candidate(_direction_attention_item(project.id, direction))
    for alert in alerts:
        add_candidate(_alert_attention_item(alert))
    for task in held_tasks:
        reason = "parked_task" if task.status == TaskStatus.PARKED else "blocked_task"
        add_candidate(_task_attention_item(task, reason=reason, severity="watch"))
    for task in failed_tasks:
        add_candidate(_task_attention_item(task, reason="failed_task", severity="watch"))
    for message in watch_messages:
        add_candidate(
            _message_attention_item(
                message,
                unread=message.id in unread_message_ids,
            )
        )

    candidate_items.sort(key=_attention_visible_sort_key)
    items = candidate_items[:_MAX_ATTENTION_ITEMS]
    for item in items:
        item.pop("_visible_priority", None)

    needs_operator_count = sum(
        1 for item in items if item["severity"] == "needs_operator"
    )
    watch_count = sum(1 for item in items if item["severity"] == "watch")
    total_needs_operator = (
        open_question_count
        + open_blocker_count
        + approval_count
        + total_local_runner_hold_count
        + stale_runner_pin_count
        + dead_runner_needs_operator_count
        + stale_provisioning_count
        + stale_queued_count
        + len(manual_directions)
        + error_alert_count
    )
    total_watch = (
        failed_count
        + blocked_count
        + parked_count
        + warning_alert_count
        + dead_runner_watch_count
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
    total_attention_count = total_needs_operator + total_watch

    return {
        "level": level,
        "summary": summary,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "has_more": total_attention_count > len(items),
        "limits": {
            "items": _MAX_ATTENTION_ITEMS,
            "message_blocker_preview": _MESSAGE_BLOCKER_PREVIEW_LIMIT,
            "message_question_preview": _MESSAGE_QUESTION_PREVIEW_LIMIT,
            "message_watch_preview": _MESSAGE_WATCH_PREVIEW_LIMIT,
            "stale_task_preview": _STALE_TASK_PREVIEW_LIMIT,
            "alert_error_preview": _ALERT_ERROR_PREVIEW_LIMIT,
            "alert_warning_preview": _ALERT_WARNING_PREVIEW_LIMIT,
        },
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
            "local_runner_holds": total_local_runner_hold_count,
            "compute_local_runner_holds": local_runner_hold_count,
            "strategic_local_runner_holds": strategic_local_runner_hold_count,
            "stale_runner_pins": stale_runner_pin_count,
            "dead_runners": dead_runner_needs_operator_count + dead_runner_watch_count,
            "stale_provisioning_tasks": stale_provisioning_count,
            "stale_queued_tasks": stale_queued_count,
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
