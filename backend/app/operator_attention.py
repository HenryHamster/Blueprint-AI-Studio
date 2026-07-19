"""Operator attention read model for the Strategic workbench.

This module derives attention from existing durable state. It intentionally does
not create a second task/message/alert lifecycle.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, not_, or_, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only
from sqlmodel import select

from app.message_convergence import OPEN_STATUS, derive_needs_action
from app.acceptance_criteria import (
    UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
    get_criteria_for_version,
    get_version_coverage_summary,
)
from app.failure_classification import is_non_death_cancellation
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    CompletionDebtEntry,
    LocalRunner,
    MessageType,
    MessagesLastSeen,
    Project,
    ProjectMessage,
    ReleaseVersion,
    VersionAcceptanceCriterion,
    Task,
    TaskStatus,
    VmContact,
)
from app.completion_debt import STATUS_OPEN as _DEBT_STATUS_OPEN, resolve_linked_task_statuses as _resolve_linked_task_statuses
from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    operator_visible_alert_filter,
    operator_visible_audience_filter,
)
from app.reaper_orphan_classify import _classify_orphan
from app.release_version import current_open_version
from app.routers.admin_runners import _runner_health
from app.runner_project_binding import runner_allowed_for_project
from app.strategic_runtime_work import (
    local_runner_hold_task_filters,
    strategic_local_runner_hold_task_filters,
    task_awaiting_container_runner_since,
    task_awaiting_local_runner_since,
    task_awaiting_strategic_local_runner_since,
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
# Reserved keys in MessagesNoisePrefs.prefs that are not event-type visibility prefs.
_PREFS_RESERVED_KEYS = frozenset({"_temp_mutes", "_mute_window", "_strategic_workbench"})
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
# Upper bound on the stale-queued-dependency candidate scan. The scan loads slim
# task rows (no heavy JSONB) and a second slim lookup of their dependencies; this
# cap keeps both bounded on a poll-frequent endpoint. The reported count is
# therefore capped at this many candidates (an anomalous overflow, not a normal
# operating state).
_STALE_QUEUED_SCAN_LIMIT = 200
_MESSAGE_BLOCKER_PREVIEW_LIMIT = 8
_MESSAGE_QUESTION_PREVIEW_LIMIT = 8
_MESSAGE_WATCH_PREVIEW_LIMIT = 24
_ALERT_ERROR_PREVIEW_LIMIT = 4
_ALERT_WARNING_PREVIEW_LIMIT = 4
_DEAD_RUNNER_PREVIEW_LIMIT = 4
_STUCK_ACCEPTANCE_CRITERIA_PREVIEW_LIMIT = 4
_STALE_COMPLETION_DEBT_AFTER = timedelta(hours=24)
_STALE_COMPLETION_DEBT_PREVIEW_LIMIT = 4

# How long before an operator attestation expires the system starts warning
# the operator.  Attestations carry ATTESTATION_WINDOW_DAYS (7 days) from
# attested_at; when the remaining time drops below this threshold, an
# 'attestation_expiring_soon' attention item surfaces so the operator can
# re-attest or provide evidence before the criterion lapses to 'partial'.
_ATTESTATION_EXPIRY_WARNING_BEFORE = timedelta(days=2)

# Scalar columns consumed by _task_attention_item / build_task_movement_explanation.
# Used with load_only(..., raiseload=True) on every bare select(Task) for attention
# item builders to avoid transferring large JSONB blobs that are never read there.
_TASK_ATTENTION_COLS = (
    Task.id,
    Task.project_id,
    Task.status,
    Task.title,
    Task.latest_status_message,
    Task.blocked_reason,
    Task.park_reason,
    Task.description,
    Task.requires_approval,
    Task.blocker_kind,
    Task.depends_on,
    Task.priority,
    Task.created_at,
    Task.updated_at,
)


def _local_runner_hold_preview_statement(
    local_runner_hold_filters: tuple[Any, ...],
    strategic_local_runner_hold_filters: tuple[Any, ...],
):
    """Load both hold previews in one round trip without widening the row.

    ``select(Task).from_statement(union_all(select(Task), ...))`` selects every
    Task column even when loader options are attached to the UNION branches.
    That silently reintroduced large JSON payload/status columns that the
    attention read model intentionally excludes. Project the exact consumed
    columns into a subquery, then map the narrow rows back to Task instances.
    """

    projection = (
        *_TASK_ATTENTION_COLS,
        Task.vm_instance_id,
        Task.context,
    )
    combined = union_all(
        select(*projection)
        .where(*local_runner_hold_filters)
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(4),
        select(*projection)
        .where(*strategic_local_runner_hold_filters)
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(4),
    ).subquery()
    held_task = aliased(Task, combined, adapt_on_names=True)
    loaded_attributes = [getattr(held_task, column.key) for column in projection]
    return select(held_task).options(
        load_only(*loaded_attributes, raiseload=True)
    )


_ATTENTION_VISIBLE_PRIORITY: dict[tuple[str, str], int] = {
    ("needs_operator", "approval_required"): 10,
    ("needs_operator", "hand_merge_required"): 15,
    ("needs_operator", "alert"): 20,
    ("needs_operator", "local_runner_hold"): 30,
    ("needs_operator", "stale_runner_pin"): 40,
    ("needs_operator", "dead_runner"): 45,
    ("needs_operator", "stale_provisioning_task"): 50,
    ("needs_operator", "stale_queued_task"): 60,
    ("needs_operator", "stuck_criteria"): 65,
    ("needs_operator", "stuck_acceptance_criterion"): 65,
    ("needs_operator", "phantom_criterion_bookkeeping"): 64,
    ("needs_operator", "coverage_blocked"): 66,
    ("needs_operator", "attestation_expired"): 67,
    ("needs_operator", "corroboration_failed"): 68,
    ("needs_operator", "attestation_expiring_soon"): 69,
    ("needs_operator", "manual_intervention"): 70,
    ("needs_operator", "confirmation_credentials_needed"): 71,
    ("needs_operator", "confirmation_access_needed"): 72,
    ("needs_operator", "confirmation_intent_change"): 73,
    ("needs_operator", "confirmation_major_decision"): 74,
    ("needs_operator", "churn_retirement_recommended"): 72,
    ("needs_operator", "stale_completion_debt"): 75,
    ("needs_operator", "blocker"): 80,
    ("needs_operator", "question"): 90,
    ("watch", "alert"): 200,
    ("watch", "dead_runner"): 205,
    ("watch", "experiment_outcome"): 215,
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
    "hand_merge_required": {
        "mode": "task_lifecycle",
        "clears_when": "task_archived_or_pr_merged_or_closed",
        "operator_action": "hand_merge_or_archive_task",
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
    "stuck_criteria": {
        "mode": "version_lifecycle",
        "clears_when": "criterion_mapped_to_active_direction_or_operator_attested",
        "operator_action": "remap_or_attest_criterion",
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
    # Pending operator confirmations (problem_model.pending_user_confirmation).
    # These always need a human decision and never auto-acknowledge — they clear
    # when the operator accepts/dismisses the item (endpoints below). For
    # credentials/access requests they also clear once the requested
    # credential/access becomes available, which pairs with the auto-clear reaper
    # sweep that removes moot confirmations.
    "confirmation_credentials_needed": {
        "mode": "problem_model_lifecycle",
        "clears_when": "confirmation_accepted_dismissed_or_credential_provided",
        "operator_action": "provide_credentials_or_dismiss",
        "accept_action": "pending_confirm_accept",
        "dismiss_action": "pending_confirm_dismiss",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "confirmation_access_needed": {
        "mode": "problem_model_lifecycle",
        "clears_when": "confirmation_accepted_dismissed_or_access_granted",
        "operator_action": "grant_access_or_dismiss",
        "accept_action": "pending_confirm_accept",
        "dismiss_action": "pending_confirm_dismiss",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "confirmation_intent_change": {
        "mode": "problem_model_lifecycle",
        "clears_when": "confirmation_accepted_or_dismissed",
        "operator_action": "confirm_intent_change_or_dismiss",
        "accept_action": "pending_confirm_accept",
        "dismiss_action": "pending_confirm_dismiss",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "confirmation_major_decision": {
        "mode": "problem_model_lifecycle",
        "clears_when": "confirmation_accepted_or_dismissed",
        "operator_action": "answer_decision_or_dismiss",
        "accept_action": "pending_confirm_accept",
        "dismiss_action": "pending_confirm_dismiss",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "stuck_acceptance_criterion": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criterion_covered_or_version_closed_or_attested",
        "operator_action": "remap_direction_or_attest_criterion",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "coverage_blocked": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criterion_covered_or_version_closed",
        "operator_action": "monitor_active_direction_progress",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "phantom_criterion_bookkeeping": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criteria_registered_or_prose_reconciled_or_version_closed",
        "operator_action": "register_criteria_or_correct_referenced_keys",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "attestation_expired": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criterion_covered_or_version_closed_or_re_attested",
        "operator_action": "re_attest_or_provide_evidence",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "attestation_expiring_soon": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criterion_covered_or_version_closed_or_re_attested_or_expires",
        "operator_action": "re_attest_or_provide_machine_evidence",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "corroboration_failed": {
        "mode": "acceptance_criteria_lifecycle",
        "clears_when": "criterion_covered_or_version_closed_or_re_attested",
        "operator_action": "re_attest_or_provide_evidence_or_retry",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    },
    "stale_completion_debt": {
        "mode": "completion_debt_lifecycle",
        "clears_when": "debt_resolved_dismissed_or_linked_to_work",
        "operator_action": "link_task_or_resolve_debt",
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
    "experiment_outcome": {
        "mode": "task_lifecycle",
        "clears_when": "task_archived_or_outside_6h_window",
        "operator_action": "inspect_experiment_result",
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


logger = logging.getLogger(__name__)


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


def _direction_churn_retirement_recommended(direction: Any) -> bool:
    """True when a direction is held PAUSED pending a keep/retire decision.

    The churn/zombie retire ladder flags ``churn_retirement_recommended=True`` and
    holds the direction paused (not active), so the ``status=='active'``
    manual-intervention path never surfaces it — this makes the pending decision
    visible to the operator instead.
    """
    return (
        isinstance(direction, dict)
        and bool(direction.get("churn_retirement_recommended"))
    )


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
        if _message_needs_operator_attention(message)
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


def _message_needs_operator_attention(message: ProjectMessage) -> bool:
    """Whether this message needs operator attention — delegates to derive_needs_action."""
    return derive_needs_action(message)


def _task_attention_item(task: Task, *, reason: str, severity: str) -> dict[str, Any]:
    status = _enum_value(task.status)
    label = {
        "approval_required": "Approval required",
        "hand_merge_required": "Hand-merge required",
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
    container_since = task_awaiting_container_runner_since(task)
    awaiting_since = (
        task_awaiting_local_runner_since(task)
        or container_since
        or task_awaiting_strategic_local_runner_since(task)
    )
    # A container hold is waiting on a Docker-capable runtime, not offline
    # hardware — word the detail accordingly so the operator sees the real cause.
    hold_subject = (
        "container-capable runner"
        if container_since and not task_awaiting_local_runner_since(task)
        else "local runner hardware"
    )
    hold_detail = (
        f"Waiting for {hold_subject} since {awaiting_since}"
        if awaiting_since
        else f"Waiting for {hold_subject}"
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


def _churn_retirement_recommended_attention_item(
    project_id: uuid.UUID,
    direction: dict[str, Any],
) -> dict[str, Any]:
    """Attention item for a paused-pending-decision churn-retirement recommendation."""
    direction_id = str(direction.get("id") or "unknown")
    detail = direction.get("churn_retirement_recommended_reason") or direction.get(
        "text"
    )
    return {
        "id": f"direction:{direction_id}:churn_retirement_recommended",
        "kind": "direction",
        "severity": "needs_operator",
        "reason": "churn_retirement_recommended",
        "label": "Direction retirement recommended",
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


# Per-type presentation for pending_user_confirmation attention items. The
# `reason` is `confirmation_<type>` so each type carries its own lifecycle entry
# (distinct clears_when) while all items share `kind='confirmation'`.
_PENDING_CONFIRMATION_META: dict[str, dict[str, str]] = {
    "credentials_needed": {
        "label": "Credentials needed",
        "accept_label": "Mark provided",
    },
    "access_needed": {
        "label": "Access needed",
        "accept_label": "Mark granted",
    },
    "intent_change": {
        "label": "Intent change to confirm",
        "accept_label": "Apply change",
    },
    "major_decision": {
        "label": "Decision needed",
        "accept_label": "Answer",
    },
}
_DEFAULT_PENDING_CONFIRMATION_META = {
    "label": "Confirmation needed",
    "accept_label": "Accept",
}


def _pending_confirmation_attention_item(
    project_id: uuid.UUID,
    confirmation: dict[str, Any],
) -> dict[str, Any]:
    """Attention item for one durable ``pending_user_confirmation`` entry.

    These are external blockers the strategist cannot resolve on its own
    (credentials/access/intent-change/major-decision). They always need a human,
    so they surface as ``needs_operator`` and expose accept/dismiss affordances so
    the operator can act inline from the daily surface instead of opening the
    strategy-internals problem-model view.
    """
    pending_id = str(confirmation.get("id") or "unknown")
    conf_type = str(confirmation.get("type") or "major_decision")
    meta = _PENDING_CONFIRMATION_META.get(
        conf_type, _DEFAULT_PENDING_CONFIRMATION_META
    )
    proposal = confirmation.get("proposal") or confirmation.get("rationale")
    base_path = f"/api/admin/projects/{project_id}/pending-confirm/{pending_id}"
    return {
        "id": f"confirmation:{pending_id}",
        "kind": "confirmation",
        "severity": "needs_operator",
        "reason": f"confirmation_{conf_type}",
        "confirmation_type": conf_type,
        "label": meta["label"],
        "detail": _compact_text(proposal, max_chars=180),
        "unread": True,
        "created_at": confirmation.get("created_at") or None,
        "actions": [
            {
                "kind": "accept",
                "label": meta["accept_label"],
                "method": "POST",
                "path": f"{base_path}/accept",
            },
            {
                "kind": "dismiss",
                "label": "Dismiss",
                "method": "POST",
                "path": f"{base_path}/dismiss",
            },
        ],
        "ref": {
            "kind": "confirmation",
            "id": pending_id,
            "project_id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "problem_model",
        }),
    }


def _stuck_acceptance_criterion_attention_item(
    project_id: uuid.UUID,
    version: ReleaseVersion,
    criterion: VersionAcceptanceCriterion,
    summary_row: dict[str, Any],
) -> dict[str, Any]:
    stuck_reason = str(summary_row.get("stuck_reason") or "")
    reason_label = {
        "no_mapping": "Acceptance criterion is unmapped",
        "direction_retired": "Acceptance criterion points to a retired direction",
    }.get(stuck_reason, "Acceptance criterion needs attention")
    detail_parts = [criterion.text, reason_label]
    detail = " - ".join(part for part in detail_parts if part)
    return {
        "id": f"version:{version.id}:acceptance_criterion:{criterion.criterion_key}",
        "kind": "acceptance_criterion",
        "severity": "needs_operator",
        "reason": "stuck_acceptance_criterion",
        "label": reason_label,
        "detail": _compact_text(detail, max_chars=180),
        "status": _enum_value(criterion.status),
        "created_at": criterion.created_at.isoformat() if criterion.created_at else None,
        "criterion_key": criterion.criterion_key,
        "stuck_reason": stuck_reason or None,
        "release_version_id": str(version.id),
        "release_version_number": version.version_number,
        "ref": {
            "kind": "acceptance_criterion",
            "id": criterion.criterion_key,
            "project_id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "manage",
        }),
    }


def _attestation_expired_attention_item(
    project_id: uuid.UUID,
    version: ReleaseVersion,
    criterion_key: str,
    criterion_text: str,
) -> dict[str, Any]:
    """Attention item for an active criterion whose operator attestation has expired.

    The criterion is stuck in 'partial' coverage with no valid attestation or
    machine-verified evidence.  The operator must re-attest or provide evidence.
    """
    return {
        "id": f"version:{version.id}:attestation_expired:{criterion_key}",
        "kind": "acceptance_criterion",
        "severity": "needs_operator",
        "reason": "attestation_expired",
        "label": "Attestation expired",
        "detail": _compact_text(
            f"Criterion '{criterion_text}' was operator-attested but the "
            f"attestation window has expired — re-attest or provide evidence",
            max_chars=180,
        ),
        "criterion_key": criterion_key,
        "release_version_id": str(version.id),
        "release_version_number": version.version_number,
        "ref": {
            "kind": "acceptance_criterion",
            "id": criterion_key,
            "project_id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "manage",
        }),
    }


def _attestation_expiring_soon_attention_item(
    project_id: uuid.UUID,
    version: ReleaseVersion,
    criterion_key: str,
    criterion_text: str,
) -> dict[str, Any]:
    """Attention item for an active criterion whose operator attestation is about to expire.

    The attestation is still valid (coverage='attested') but the remaining
    time before TTL expiry is below the warning threshold.  The operator
    should re-attest or provide machine evidence so the criterion does not
    lapse to 'partial' coverage.
    """
    return {
        "id": f"version:{version.id}:attestation_expiring_soon:{criterion_key}",
        "kind": "acceptance_criterion",
        "severity": "needs_operator",
        "reason": "attestation_expiring_soon",
        "label": "Attestation approaching expiry",
        "detail": _compact_text(
            f"Criterion '{criterion_text}' was operator-attested and is "
            f"approaching its expiry window — re-attest or provide "
            f"machine evidence before the criterion lapses",
            max_chars=180,
        ),
        "criterion_key": criterion_key,
        "release_version_id": str(version.id),
        "release_version_number": version.version_number,
        "ref": {
            "kind": "acceptance_criterion",
            "id": criterion_key,
            "project_id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "manage",
        }),
    }


def _corroboration_failed_attention_item(
    project_id: uuid.UUID,
    version: ReleaseVersion,
    criterion_key: str,
    criterion_text: str,
) -> dict[str, Any]:
    """Attention item for an active criterion whose corroboration verification task failed.

    The system spawned a verification task for this operator-attested criterion
    and that task FAILED or was CANCELLED — the attestation is no longer trusted
    and the operator must re-attest or provide real evidence.
    """
    return {
        "id": f"version:{version.id}:corroboration_failed:{criterion_key}",
        "kind": "acceptance_criterion",
        "severity": "needs_operator",
        "reason": "corroboration_failed",
        "label": "Attestation verification failed",
        "detail": _compact_text(
            f"Criterion '{criterion_text}' was operator-attested but the "
            f"verification task failed — re-attest or provide machine evidence",
            max_chars=180,
        ),
        "criterion_key": criterion_key,
        "release_version_id": str(version.id),
        "release_version_number": version.version_number,
        "ref": {
            "kind": "acceptance_criterion",
            "id": criterion_key,
            "project_id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "manage",
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


def _handmerge_preserved_task_filter(project_id: uuid.UUID) -> tuple[Any, ...]:
    return (
        Task.project_id == project_id,
        Task.archived.is_(False),
        Task.status == TaskStatus.FAILED,
        Task.context["pr_preserved_for_handmerge"].as_boolean().is_(True),
    )


async def _load_handmerge_preserved_tasks(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[int, list[Task]]:
    """Return the exact count plus the bounded operator-attention preview."""
    result = await session.execute(
        select(Task, func.count().over().label("_total_count"))
        .options(load_only(*_TASK_ATTENTION_COLS, raiseload=True))
        .where(*_handmerge_preserved_task_filter(project_id))
        .order_by(Task.updated_at.desc())
        .limit(_STALE_TASK_PREVIEW_LIMIT)
    )
    rows = result.all()
    count = int(rows[0][1] or 0) if rows else 0
    return count, [row[0] for row in rows]


async def _load_stale_runner_pins(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, list[tuple[Task, LocalRunner | None]]]:
    pinned_tasks = list(
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        *_TASK_ATTENTION_COLS,
                        Task.vm_instance_id,
                        Task.context,
                        raiseload=True,
                    )
                )
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
                        runner_allowed_for_project(project_id),
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
    runner_rows: list[LocalRunner] | None = None,
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
    if runner_rows is None:
        runner_rows = list(
            (
                await session.execute(
                    select(LocalRunner).where(
                        runner_allowed_for_project(project_id),
                        LocalRunner.status != "deleted",
                    )
                )
            )
            .scalars()
            .all()
        )
    dead = [runner for runner in runner_rows if _runner_is_dead(runner)]
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


def _dead_runner_attention_item(
    runner: LocalRunner,
    *,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    # Use the requested project's id so secondary-bound runners route back to
    # the viewing project (B), not the runner's enrollment project (A).
    scoped_project_id = project_id if project_id is not None else runner.project_id
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
            "project_id": str(scoped_project_id),
        },
        "target": _attention_target(
            {
                "surface": "runtime",
                "project_id": str(scoped_project_id),
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
                .options(
                    load_only(
                        *_TASK_ATTENTION_COLS,
                        Task.vm_instance_id,
                        Task.context,
                        Task.last_heartbeat,
                        Task.last_tool_use_at,
                        Task.vm_provisioned_at,
                        Task.started_at,
                        Task.payloads,
                        raiseload=True,
                    )
                )
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
    # Slim, bounded scan: only the columns the stale-queued attention items and
    # the dependency-status check read (no context/payloads/plan_output JSONB),
    # capped so a poll-frequent endpoint never materializes an unbounded row set.
    # Local-runner holds are excluded in SQL via their authoritative predicates
    # (already surfaced as their own attention items), which also removes the
    # last reason to load payloads/context for candidates.
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
                    func.jsonb_array_length(Task.depends_on) > 0,
                    not_(and_(*local_runner_hold_task_filters(project_id))),
                    not_(and_(*strategic_local_runner_hold_task_filters(project_id))),
                )
                .options(
                    load_only(
                        Task.id,
                        Task.project_id,
                        Task.status,
                        Task.priority,
                        Task.created_at,
                        Task.updated_at,
                        Task.depends_on,
                        Task.title,
                        Task.description,
                        Task.latest_status_message,
                        Task.blocked_reason,
                        Task.park_reason,
                        Task.requires_approval,
                        Task.blocker_kind,
                        raiseload=True,
                    )
                )
                .order_by(Task.priority.asc(), Task.created_at.asc())
                .limit(_STALE_QUEUED_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
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
        # Only id/status/context are read from dependency rows below; keep the
        # heavy JSONB columns (payloads, plan_output, ...) off this lookup.
        dep_tasks_by_id = {
            task.id: task
            for task in (
                await session.execute(
                    select(Task)
                    .where(Task.id.in_(all_dep_ids))
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.context,
                            raiseload=True,
                        )
                    )
                )
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


_EXPERIMENT_OUTCOME_WINDOW = timedelta(hours=6)
_EXPERIMENT_OUTCOME_PREVIEW_LIMIT = 4
_EXPERIMENT_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


def _experiment_outcome_attention_item(task: Task) -> dict[str, Any]:
    plan_output = task.plan_output or {}
    compute_result = plan_output.get("compute_result") or {} if isinstance(plan_output, dict) else {}
    criterion = compute_result.get("criterion") if isinstance(compute_result, dict) else {}
    criterion = criterion if isinstance(criterion, dict) else {}
    criterion_met = criterion.get("met")
    if criterion_met is False and criterion.get("measurable") is not False:
        outcome_label = "unmet"
    else:
        outcome_label = "inconclusive"
    criterion_summary = _compact_text(
        criterion.get("summary") or criterion.get("description"), max_chars=180
    )
    detail = criterion_summary or _compact_text(
        task.latest_status_message or task.title, max_chars=180
    )
    return {
        "id": f"task:{task.id}:experiment_outcome",
        "kind": "task",
        "severity": "watch",
        "reason": "experiment_outcome",
        "label": f"Experiment criterion {outcome_label}",
        "detail": detail,
        "status": _enum_value(task.status),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "ref": {
            "kind": "task",
            "id": str(task.id),
            "project_id": str(task.project_id),
        },
        "target": _attention_target({
            "surface": "runtime",
            "project_id": str(task.project_id),
            "section": "compute",
            "task_id": str(task.id),
        }),
    }


def _stuck_criteria_attention_item(
    project_id: uuid.UUID,
    stuck_count: int,
    no_mapping_count: int,
    direction_retired_count: int,
    direction_inactive_count: int = 0,
) -> dict[str, Any]:
    parts = []
    if no_mapping_count:
        noun = "criterion" if no_mapping_count == 1 else "criteria"
        parts.append(f"{no_mapping_count} {noun} with no direction mapping")
    if direction_retired_count:
        noun = "criterion" if direction_retired_count == 1 else "criteria"
        parts.append(f"{direction_retired_count} {noun} whose direction is retired")
    if direction_inactive_count:
        noun = "criterion" if direction_inactive_count == 1 else "criteria"
        parts.append(f"{direction_inactive_count} {noun} whose direction is inactive")
    detail = "; ".join(parts) if parts else f"{stuck_count} stuck criteria"
    plural = "criteria" if stuck_count != 1 else "criterion"
    return {
        "id": f"project:{project_id}:stuck_criteria",
        "kind": "version",
        "severity": "needs_operator",
        "reason": "stuck_criteria",
        "label": f"{stuck_count} acceptance {plural} stuck",
        "detail": _compact_text(detail, max_chars=180),
        "ref": {
            "kind": "project",
            "id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "acceptance_criteria",
        }),
    }


def _coverage_blocked_attention_item(
    project_id: uuid.UUID,
    coverage_blocked_count: int,
) -> dict[str, Any]:
    plural = "criteria" if coverage_blocked_count != 1 else "criterion"
    return {
        "id": f"project:{project_id}:coverage_blocked",
        "kind": "version",
        "severity": "needs_operator",
        "reason": "coverage_blocked",
        "label": f"{coverage_blocked_count} acceptance {plural} awaiting active direction",
        "detail": (
            f"{coverage_blocked_count} uncovered {plural} mapped to active "
            "direction(s) — promotion is blocked until covered"
        ),
        "ref": {
            "kind": "project",
            "id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "acceptance_criteria",
        }),
    }


def _phantom_criterion_bookkeeping_attention_item(
    project_id: uuid.UUID,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Surface criterion-key drift: prose tracks coverage against keys that are
    not registered on the open version (or no criteria are registered at all).

    Without this the version reads PASS/not-applicable at every gate silently —
    the "phantom bookkeeping" failure. The item points the operator at the
    acceptance-criteria surface to register the real criteria or fix the prose.
    """
    phantom_keys = report.get("phantom_keys") or []
    zero_registered = bool(report.get("zero_registered"))
    parts: list[str] = []
    if zero_registered:
        parts.append(
            "focus prose references acceptance criteria but the open version "
            "has zero registered criteria"
        )
    if phantom_keys:
        preview = ", ".join(phantom_keys[:5])
        noun = "key" if len(phantom_keys) == 1 else "keys"
        parts.append(f"{len(phantom_keys)} referenced {noun} not registered: {preview}")
    detail = "; ".join(parts) if parts else "criterion-key bookkeeping drift detected"
    label = (
        "Acceptance criteria referenced in prose are not registered"
        if zero_registered
        else "Referenced acceptance-criteria keys are not registered"
    )
    return {
        "id": f"project:{project_id}:phantom_criterion_bookkeeping",
        "kind": "version",
        "severity": "needs_operator",
        "reason": "phantom_criterion_bookkeeping",
        "label": label,
        "detail": _compact_text(detail, max_chars=200),
        "ref": {
            "kind": "project",
            "id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "acceptance_criteria",
        }),
    }


# Statuses that indicate a linked task is genuinely in-flight or already
# succeeded — a debt with at least one such task is NOT stale-unworked.
# FAILED, CANCELLED, and missing (unknown) task ids do NOT count.
_DEBT_LINKED_WORKING_STATUSES: frozenset[str] = frozenset({
    "QUEUED",
    "PROVISIONING",
    "IN_PROGRESS",
    "EVALUATING",
    "BLOCKED",
    "PARKED",
    "COMPLETED",
})

# Upper bound on aged-open-debt candidates we inspect per call. A debt with
# more than this many aged open rows is an anomaly; the count is therefore
# capped at this many.
_STALE_DEBT_SCAN_LIMIT = 200


async def _load_stale_unworked_completion_debts(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, list[CompletionDebtEntry]]:
    """Return (count, oldest_preview) of aged open debts with no genuinely
    active or successful linked task.

    Scoped to the project plus global (project_id IS NULL) rows, matching the
    visibility rule used by the strategist completion-debt fetch.

    A debt is considered UNWORKED unless it has at least one linked task whose
    live status is in :data:`_DEBT_LINKED_WORKING_STATUSES` (QUEUED/IN_PROGRESS/
    EVALUATING/BLOCKED/PARKED or COMPLETED).  Linked task ids that resolve to
    FAILED, CANCELLED, or no Task row at all do NOT count as work-in-progress
    and the debt resurfaces as stale after the age threshold.
    """
    cutoff = _utcnow() - _STALE_COMPLETION_DEBT_AFTER
    base_filters = (
        or_(
            CompletionDebtEntry.project_id == project_id,
            CompletionDebtEntry.project_id.is_(None),
        ),
        CompletionDebtEntry.status == _DEBT_STATUS_OPEN,
        CompletionDebtEntry.created_at < cutoff,
    )
    # Load a bounded set of aged open candidates (all linked-task configurations).
    candidates = list(
        (
            await session.execute(
                select(CompletionDebtEntry)
                .where(*base_filters)
                .order_by(
                    CompletionDebtEntry.created_at.asc(),
                    CompletionDebtEntry.id.asc(),
                )
                .limit(_STALE_DEBT_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return 0, []

    # Collect all referenced task ids so we can resolve statuses in one query.
    all_task_ids: list[str] = []
    for entry in candidates:
        detail_dict = entry.detail if isinstance(entry.detail, dict) else {}
        linked = detail_dict.get("linked_tasks")
        if isinstance(linked, list):
            for x in linked:
                if isinstance(x, dict) and x.get("task_id"):
                    all_task_ids.append(str(x["task_id"]))

    status_map = await _resolve_linked_task_statuses(session, all_task_ids)

    # Keep only debts with no genuinely active or successful linked task.
    unworked: list[CompletionDebtEntry] = []
    for entry in candidates:
        detail_dict = entry.detail if isinstance(entry.detail, dict) else {}
        linked = detail_dict.get("linked_tasks")
        if not isinstance(linked, list) or len(linked) == 0:
            unworked.append(entry)
            continue
        has_working = any(
            status_map.get(str(x.get("task_id")), "") in _DEBT_LINKED_WORKING_STATUSES
            for x in linked
            if isinstance(x, dict) and x.get("task_id")
        )
        if not has_working:
            unworked.append(entry)

    count = len(unworked)
    return count, unworked[:_STALE_COMPLETION_DEBT_PREVIEW_LIMIT]


def _stale_completion_debt_attention_item(
    project_id: uuid.UUID,
    count: int,
    entries: list[CompletionDebtEntry],
) -> dict[str, Any]:
    plural = "obligations" if count != 1 else "obligation"
    if entries:
        first_title = _compact_text(entries[0].title, max_chars=140)
        detail = first_title if count == 1 else f"{first_title} (+{count - 1} more)"
    else:
        detail = f"{count} unworked completion-debt {plural}"
    return {
        "id": f"project:{project_id}:stale_completion_debt",
        "kind": "completion_debt",
        "severity": "needs_operator",
        "reason": "stale_completion_debt",
        "label": f"{count} completion-debt {plural} unworked",
        "detail": detail,
        "count": count,
        "ref": {
            "kind": "project",
            "id": str(project_id),
        },
        "target": _attention_target({
            "surface": "project",
            "project_id": str(project_id),
            "section": "completion_debt",
        }),
    }


async def get_operator_message_counts(
    session: AsyncSession,
    project_id: uuid.UUID,
    last_seen_at: datetime | None = None,
    user_id: str | None = None,
) -> dict[str, int]:
    """Return high-signal open message counts by type plus an unread total.

    Lightweight alternative to build_operator_attention for badge-poll callers
    that only need integers, not full message previews.  Includes high-signal
    NOTE escalations (event_type in HIGH_SIGNAL_NOTE_EVENT_TYPES) and honours
    per-operator noise prefs / temp-mutes when user_id is provided.
    """
    from app.notification_dispatch import HIGH_SIGNAL_NOTE_EVENT_TYPES
    from app.messages_noise_prefs_store import get_messages_noise_prefs_row

    # Build the set of event types the operator has muted.
    muted_event_types: set[str] = set()
    if user_id is not None:
        prefs_row = await get_messages_noise_prefs_row(session, user_id, project_id)
        if prefs_row is not None:
            prefs_dict = dict(prefs_row.prefs or {})
            # Permanent per-event-type mutes and routines (excluded from badge)
            muted_event_types = {
                et
                for et, pref in prefs_dict.items()
                if et not in _PREFS_RESERVED_KEYS and pref in ("mute", "routine")
            }
            # Active temporary mutes
            now_ms = int(_utcnow().timestamp() * 1000)
            temp_mutes = prefs_dict.get("_temp_mutes") or {}
            if isinstance(temp_mutes, dict):
                muted_event_types |= {
                    et
                    for et, exp in temp_mutes.items()
                    if isinstance(exp, (int, float)) and exp > now_ms
                }

    _hs_types = list(_HIGH_SIGNAL_MESSAGE_TYPES)
    _operator_visible_message_filters = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.status == OPEN_STATUS,
        ProjectMessage.from_type != "user",
        operator_visible_audience_filter(ProjectMessage.context),
    ]
    count_rows = (
        await session.execute(
            select(ProjectMessage.message_type, func.count())
            .where(
                *_operator_visible_message_filters,
                ProjectMessage.message_type.in_(_hs_types),
            )
            .group_by(ProjectMessage.message_type)
        )
    ).all()
    counts = {mt: int(cnt or 0) for mt, cnt in count_rows}

    # Count high-signal NOTE escalations, excluding muted event types.
    included_note_ets = list(HIGH_SIGNAL_NOTE_EVENT_TYPES - muted_event_types)
    note_count = 0
    if included_note_ets:
        note_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ProjectMessage)
                    .where(
                        *_operator_visible_message_filters,
                        ProjectMessage.message_type == MessageType.NOTE,
                        ProjectMessage.context.op("->>")(
                            "event_type"
                        ).in_(included_note_ets),
                    )
                )
            ).scalar()
            or 0
        )

    _ts_filter = (
        [ProjectMessage.created_at > last_seen_at] if last_seen_at is not None else []
    )
    unread = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProjectMessage)
                .where(
                    *_operator_visible_message_filters,
                    ProjectMessage.message_type.in_(_hs_types),
                    *_ts_filter,
                )
            )
        ).scalar()
        or 0
    )
    if included_note_ets:
        unread += int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ProjectMessage)
                    .where(
                        *_operator_visible_message_filters,
                        ProjectMessage.message_type == MessageType.NOTE,
                        ProjectMessage.context.op("->>")(
                            "event_type"
                        ).in_(included_note_ets),
                        *_ts_filter,
                    )
                )
            ).scalar()
            or 0
        )
    # needs_operator here is the message-based component: open questions + blockers.
    # The audience-routed action rows are included too, so the lightweight badge
    # endpoint and the full attention projection share the same authority.
    action_message_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProjectMessage)
                .where(
                    *_operator_visible_message_filters,
                    ProjectMessage.context["audience"].as_string()
                    == OPERATOR_ACTION_NEEDED,
                    ProjectMessage.message_type.notin_(
                        list(_NEEDS_OPERATOR_MESSAGE_TYPES)
                    ),
                )
            )
        ).scalar()
        or 0
    )
    needs_operator = (
        counts.get(MessageType.BLOCKER, 0)
        + counts.get(MessageType.QUESTION, 0)
        + action_message_count
    )
    # Count undismissed ERROR and WARNING alerts for the cross-tab indicator.
    # Mirrors the alert count in build_operator_attention (lines ~2290).
    active_alerts = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.dismissed_at.is_(None),
                    Alert.severity.in_(list(_ACTIONABLE_ALERT_SEVERITIES)),
                    operator_visible_alert_filter(Alert.event_type),
                )
            )
        ).scalar()
        or 0
    )
    return {
        "blocker": counts.get(MessageType.BLOCKER, 0),
        "question": counts.get(MessageType.QUESTION, 0),
        "bug_report": counts.get(MessageType.BUG_REPORT, 0),
        "feedback": counts.get(MessageType.FEEDBACK, 0),
        "high_signal_note": note_count,
        "unread": unread,
        "needs_operator": needs_operator,
        "active_alerts": active_alerts,
    }


async def build_operator_attention(
    session: AsyncSession,
    project: Project,
    user_id: str,
    *,
    runner_rows: list[LocalRunner] | None = None,
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

    _common_message_filters = [
        ProjectMessage.project_id == project.id,
        ProjectMessage.status == OPEN_STATUS,
        ProjectMessage.from_type != "user",
        operator_visible_audience_filter(ProjectMessage.context),
    ]
    # UNION ALL with per-type LIMITs: one round-trip while preserving independent
    # per-priority caps so a burst of BUG_REPORT/FEEDBACK can't crowd out BLOCKERs.
    _blocker_q = (
        select(ProjectMessage)
        .where(*_common_message_filters, ProjectMessage.message_type == MessageType.BLOCKER)
        .order_by(ProjectMessage.created_at.desc())
        .limit(_MESSAGE_BLOCKER_PREVIEW_LIMIT)
    )
    _question_q = (
        select(ProjectMessage)
        .where(
            *_common_message_filters,
            or_(
                ProjectMessage.message_type == MessageType.QUESTION,
                and_(
                    ProjectMessage.context["audience"].as_string()
                    == OPERATOR_ACTION_NEEDED,
                    ProjectMessage.message_type.notin_(
                        list(_NEEDS_OPERATOR_MESSAGE_TYPES)
                    ),
                ),
            ),
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(_MESSAGE_QUESTION_PREVIEW_LIMIT)
    )
    _watch_q = (
        select(ProjectMessage)
        .where(
            *_common_message_filters,
            ProjectMessage.message_type.in_([MessageType.BUG_REPORT, MessageType.FEEDBACK]),
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(_MESSAGE_WATCH_PREVIEW_LIMIT)
    )
    message_rows = list(
        (
            await session.execute(
                select(ProjectMessage).from_statement(
                    union_all(_blocker_q, _question_q, _watch_q)
                )
            )
        )
        .scalars()
        .all()
    )
    message_count_rows = (
        await session.execute(
            select(ProjectMessage.message_type, func.count())
            .where(
                *_common_message_filters,
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
    open_action_message_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProjectMessage)
                .where(
                    *_common_message_filters,
                    ProjectMessage.context["audience"].as_string()
                    == OPERATOR_ACTION_NEEDED,
                    ProjectMessage.message_type.notin_(
                        list(_NEEDS_OPERATOR_MESSAGE_TYPES)
                    ),
                )
            )
        ).scalar()
        or 0
    )
    unread_count_filters: list[Any] = []
    if last_seen_at is not None:
        unread_count_filters.append(ProjectMessage.created_at > last_seen_at)
    unread_message_count = int(
        (
            await session.execute(
                select(func.count()).select_from(ProjectMessage).where(
                    *_common_message_filters,
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
    open_action_messages = [
        message
        for message in message_rows
        if _message_needs_operator_attention(message)
        and message.message_type not in _NEEDS_OPERATOR_MESSAGE_TYPES
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
                            operator_visible_audience_filter(Task.context),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("approval"),
                func.sum(case(
                    (
                        and_(
                            Task.status == TaskStatus.FAILED,
                            operator_visible_audience_filter(Task.context),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("failed"),
                func.sum(case(
                    (
                        and_(
                            Task.status == TaskStatus.BLOCKED,
                            Task.requires_approval.is_not(True),
                            operator_visible_audience_filter(Task.context),
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
                            operator_visible_audience_filter(Task.context),
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

    (
        handmerge_preserved_count,
        handmerge_preserved_tasks,
    ) = await _load_handmerge_preserved_tasks(
        session,
        project.id,
    )
    # Keep preserved hand-merge PRs visible as their own operator item rather
    # than letting the generic FAILED-task watch preview consume the signal.
    generic_failed_count = max(failed_count - handmerge_preserved_count, 0)

    local_runner_hold_filters = local_runner_hold_task_filters(project.id)
    strategic_local_runner_hold_filters = strategic_local_runner_hold_task_filters(
        project.id
    )
    _hold_counts = (
        await session.execute(
            select(
                func.count().filter(and_(*local_runner_hold_filters)).label("local"),
                func.count().filter(and_(*strategic_local_runner_hold_filters)).label("strategic"),
            )
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.archived.is_(False),
                Task.status == TaskStatus.QUEUED,
                Task.requires_approval.is_not(True),
            )
        )
    ).one()
    local_runner_hold_count = int(_hold_counts.local or 0)
    strategic_local_runner_hold_count = int(_hold_counts.strategic or 0)
    hold_task_rows = list(
        (
            await session.execute(
                _local_runner_hold_preview_statement(
                    local_runner_hold_filters,
                    strategic_local_runner_hold_filters,
                )
            )
        )
        .scalars()
        .all()
    )
    # Deduplicate (a task could theoretically match both filters).
    seen_ids: set = set()
    local_runner_hold_tasks = []
    for task in hold_task_rows:
        if task.id not in seen_ids:
            seen_ids.add(task.id)
            local_runner_hold_tasks.append(task)
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
    ) = await _load_dead_runners(session, project.id, runner_rows=runner_rows)
    (
        stale_provisioning_count,
        stale_provisioning_tasks,
    ) = await _load_stale_provisioning_tasks(session, project.id)
    (
        stale_queued_count,
        stale_queued_tasks,
    ) = await _load_stale_queued_dependency_tasks(session, project.id)

    (
        stale_debt_count,
        stale_debt_entries,
    ) = await _load_stale_unworked_completion_debts(session, project.id)

    experiment_cutoff = _utcnow() - _EXPERIMENT_OUTCOME_WINDOW
    # JSONB `->` yields JSON null (not SQL NULL) when "met" is present with a
    # null value, so IS NOT NULL means the criterion carries a "met" key at
    # all; `->>` then yields SQL NULL for JSON null, and IS DISTINCT FROM
    # 'true' keeps both null (inconclusive) and false (unmet) verdicts.
    experiment_criterion_met = Task.plan_output["compute_result"]["criterion"]["met"]
    experiment_outcome_filters = (
        Task.project_id == project.id,
        Task.archived.is_(False),
        Task.payloads[0]["type"].as_string() == "compute_experiment",
        Task.status.in_(list(_EXPERIMENT_TERMINAL_STATUSES)),
        Task.completed_at.is_not(None),
        Task.completed_at >= experiment_cutoff,
        experiment_criterion_met.is_not(None),
        experiment_criterion_met.as_string().is_distinct_from("true"),
    )
    unmet_experiment_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(*experiment_outcome_filters)
            )
        ).scalar_one()
        or 0
    )
    unmet_experiment_preview = list(
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.project_id,
                        Task.status,
                        Task.title,
                        Task.latest_status_message,
                        Task.created_at,
                        Task.updated_at,
                        Task.plan_output,
                        raiseload=True,
                    )
                )
                .where(*experiment_outcome_filters)
                .order_by(Task.completed_at.desc())
                .limit(_EXPERIMENT_OUTCOME_PREVIEW_LIMIT)
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
                operator_visible_alert_filter(Alert.event_type),
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
                .options(load_only(*_TASK_ATTENTION_COLS, raiseload=True))
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.requires_approval.is_(True),
                    Task.status.in_(list(_APPROVAL_TASK_STATUSES)),
                    operator_visible_audience_filter(Task.context),
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
                .options(load_only(*_TASK_ATTENTION_COLS, raiseload=True))
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.status == TaskStatus.FAILED,
                    Task.context["pr_preserved_for_handmerge"].as_boolean().is_not(True),
                    operator_visible_audience_filter(Task.context),
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
                .options(load_only(*_TASK_ATTENTION_COLS, raiseload=True))
                .where(
                    Task.project_id == project.id,
                    Task.archived.is_(False),
                    Task.status.in_([TaskStatus.BLOCKED, TaskStatus.PARKED]),
                    Task.requires_approval.is_not(True),
                    operator_visible_audience_filter(Task.context),
                )
                .order_by(Task.updated_at.desc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )
    _alert_base_filters = (
        Alert.project_id == project.id,
        Alert.dismissed_at.is_(None),
        operator_visible_alert_filter(Alert.event_type),
    )
    combined_alerts = list(
        (
            await session.execute(
                select(Alert).from_statement(
                    union_all(
                        select(Alert)
                        .where(*_alert_base_filters, Alert.severity == AlertSeverity.ERROR)
                        .order_by(Alert.created_at.desc())
                        .limit(_ALERT_ERROR_PREVIEW_LIMIT),
                        select(Alert)
                        .where(*_alert_base_filters, Alert.severity == AlertSeverity.WARNING)
                        .order_by(Alert.created_at.desc())
                        .limit(_ALERT_WARNING_PREVIEW_LIMIT),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    error_alerts = [a for a in combined_alerts if a.severity == AlertSeverity.ERROR]
    warning_alerts = [a for a in combined_alerts if a.severity == AlertSeverity.WARNING]
    alerts = [*error_alerts, *warning_alerts]

    # Stuck acceptance criteria: uncovered active criteria with no active direction.
    stuck_criteria_count = 0
    stuck_no_mapping_count = 0
    stuck_direction_retired_count = 0
    stuck_direction_inactive_count = 0
    stuck_acceptance_criteria_count = 0
    stuck_acceptance_criteria: list[dict[str, Any]] = []
    coverage_blocked_count = 0
    attestation_expired_count = 0
    attestation_expired_items: list[dict[str, Any]] = []
    attestation_expiring_count = 0
    attestation_expiring_items: list[dict[str, Any]] = []
    corroboration_failed_count = 0
    corroboration_failed_items: list[dict[str, Any]] = []
    failed_corroboration_keys: set[str] = set()
    phantom_bookkeeping_report: dict[str, Any] = {}
    try:
        open_version = await current_open_version(session, project.id)
        if open_version is not None:
            coverage = await get_version_coverage_summary(session, open_version.id)

            phantom_bookkeeping_report = coverage.get("phantom_bookkeeping") or {}

            # Read failed corroboration keys from the coverage summary, which
            # already computed them via get_failed_corroboration_keys internally.
            # This avoids a redundant DB round-trip and keeps the two in sync.
            failed_corroboration_keys = set(
                coverage.get("failed_corroboration_keys", [])
            )

            stuck_criteria_count = coverage.get("stuck_count", 0)
            for criterion in coverage.get("criteria", []):
                reason = criterion.get("stuck_reason")
                if reason == "no_mapping":
                    stuck_no_mapping_count += 1
                elif reason == "direction_retired":
                    stuck_direction_retired_count += 1
                elif reason == "direction_inactive":
                    stuck_direction_inactive_count += 1
                elif (
                    criterion.get("status") == "active"
                    and criterion.get("coverage") == "uncovered"
                    and reason is None
                ):
                    coverage_blocked_count += 1
                # Detect expired attestations or failed corroborations: active
                # criteria with 'partial' coverage whose evidence includes an
                # operator_attestation (the attestation window lapsed, or the
                # corroboration task failed, dropping coverage from 'attested'
                # to 'partial' — re-attestation or machine evidence is needed).
                # When a failed corroboration task exists it takes priority over
                # the generic attestation-expired surface.
                elif (
                    criterion.get("status") == "active"
                    and criterion.get("coverage") == "partial"
                ):
                    evidence = criterion.get("evidence", [])
                    has_operator_attestation = any(
                        e.get("source_kind") == "operator_attestation"
                        for e in evidence
                    )
                    has_backfill = any(
                        e.get("source_kind") == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND
                        for e in evidence
                    )
                    ck = criterion["criterion_key"]
                    if (
                        has_operator_attestation or has_backfill
                    ) and ck in failed_corroboration_keys:
                        # A spawned Verify: {key} task failed, downgrading either an
                        # operator attestation or an unverified-merge backfill from
                        # its lower-confidence tier to 'partial'.  Both surface as a
                        # corroboration_failed item so the operator can re-verify or
                        # provide real machine evidence.
                        corroboration_failed_count += 1
                        corroboration_failed_items.append(
                            _corroboration_failed_attention_item(
                                project.id,
                                open_version,
                                ck,
                                criterion.get("text", ""),
                            )
                        )
                    elif has_operator_attestation:
                        # An operator attestation lapsed without a failed
                        # corroboration (the attestation window expired).  Backfill
                        # coverage has no time-window expiry, so it never lands here.
                        attestation_expired_count += 1
                        attestation_expired_items.append(
                            _attestation_expired_attention_item(
                                project.id,
                                open_version,
                                ck,
                                criterion.get("text", ""),
                            )
                        )
                # Detect attestations approaching expiry: active criteria with
                # 'attested' coverage whose TTL is close to lapsing.  Unlike the
                # 'partial' branches above (already-lapsed), this is a proactive
                # warning — the attestation is still valid but will soon expire
                # and drop coverage to 'partial'.
                elif (
                    criterion.get("status") == "active"
                    and criterion.get("coverage") == "attested"
                ):
                    expiry_str = criterion.get("attestation_expires_at")
                    if expiry_str is not None:
                        try:
                            expiry_dt = datetime.fromisoformat(str(expiry_str))
                            if _utcnow() >= expiry_dt - _ATTESTATION_EXPIRY_WARNING_BEFORE:
                                ck = criterion["criterion_key"]
                                attestation_expiring_count += 1
                                attestation_expiring_items.append(
                                    _attestation_expiring_soon_attention_item(
                                        project.id,
                                        open_version,
                                        ck,
                                        criterion.get("text", ""),
                                    )
                                )
                        except (TypeError, ValueError):
                            pass
            stuck_by_key = {
                c["criterion_key"]: c
                for c in coverage.get("criteria", [])
                if c["status"] == "active"
                and c["coverage"] == "uncovered"
                and c.get("stuck_reason") is not None
            }
            stuck_acceptance_criteria_count = len(stuck_by_key)
            if stuck_acceptance_criteria_count:
                try:
                    criterion_rows = await get_criteria_for_version(
                        session, open_version.id
                    )
                    for criterion in criterion_rows:
                        summary_row = stuck_by_key.get(criterion.criterion_key)
                        if summary_row is None:
                            continue
                        stuck_acceptance_criteria.append(
                            _stuck_acceptance_criterion_attention_item(
                                project.id,
                                open_version,
                                criterion,
                                summary_row,
                            )
                        )
                    stuck_acceptance_criteria = stuck_acceptance_criteria[
                        :_STUCK_ACCEPTANCE_CRITERIA_PREVIEW_LIMIT
                    ]
                except Exception:
                    logger.exception(
                        "operator_attention: failed to load stuck acceptance criteria for project %s",
                        project.id,
                    )
    except Exception:
        pass

    directions = project.directions if isinstance(project.directions, list) else []
    manual_directions = [
        direction
        for direction in directions
        if isinstance(direction, dict)
        and _direction_manual_intervention(direction)
        and direction.get("status") == "active"
    ]
    manual_directions.sort(key=_direction_priority)

    # Churn-retirement-recommended directions are held PAUSED (not active) pending
    # an explicit keep/retire decision, so the active-only manual-intervention path
    # above skips them. Surface them as their own needs_operator items so the
    # pending decision is visible even though the direction is paused.
    churn_recommended_directions = [
        direction
        for direction in directions
        if _direction_churn_retirement_recommended(direction)
    ]
    churn_recommended_directions.sort(key=_direction_priority)

    # Durable operator confirmations (problem_model.pending_user_confirmation).
    # Read the raw dict defensively — never parse-and-raise here, so a malformed
    # model can't 500 this poll-frequent endpoint.
    problem_model = project.problem_model if isinstance(project.problem_model, dict) else {}
    raw_pending = problem_model.get("pending_user_confirmation")
    pending_confirmations = [
        confirmation
        for confirmation in (raw_pending if isinstance(raw_pending, list) else [])
        if isinstance(confirmation, dict) and confirmation.get("id")
    ]
    pending_confirmation_count = len(pending_confirmations)

    candidate_items: list[dict[str, Any]] = []

    def add_candidate(item: dict[str, Any]) -> None:
        item.setdefault("lifecycle", _attention_lifecycle(item.get("reason")))
        item["_visible_priority"] = _attention_visible_priority(item)
        candidate_items.append(item)

    for message in [*open_questions, *open_blockers, *open_action_messages]:
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
        add_candidate(_dead_runner_attention_item(runner, project_id=project.id))
    for task in stale_provisioning_tasks:
        add_candidate(
            _stale_task_attention_item(task, reason="stale_provisioning_task")
        )
    for task in stale_queued_tasks:
        add_candidate(_stale_task_attention_item(task, reason="stale_queued_task"))
    if stale_debt_count > 0:
        add_candidate(
            _stale_completion_debt_attention_item(project.id, stale_debt_count, stale_debt_entries)
        )
    if stuck_criteria_count > 0:
        add_candidate(
            _stuck_criteria_attention_item(
                project.id,
                stuck_criteria_count,
                stuck_no_mapping_count,
                stuck_direction_retired_count,
                stuck_direction_inactive_count,
            )
        )
    if coverage_blocked_count > 0:
        add_candidate(_coverage_blocked_attention_item(project.id, coverage_blocked_count))
    if phantom_bookkeeping_report.get("has_drift"):
        add_candidate(
            _phantom_criterion_bookkeeping_attention_item(
                project.id, phantom_bookkeeping_report
            )
        )
    for direction in manual_directions[:3]:
        add_candidate(_direction_attention_item(project.id, direction))
    for direction in churn_recommended_directions[:3]:
        add_candidate(
            _churn_retirement_recommended_attention_item(project.id, direction)
        )
    for confirmation in pending_confirmations:
        add_candidate(_pending_confirmation_attention_item(project.id, confirmation))
    for item in stuck_acceptance_criteria:
        add_candidate(item)
    for item in attestation_expired_items:
        add_candidate(item)
    for item in attestation_expiring_items:
        add_candidate(item)
    for item in corroboration_failed_items:
        add_candidate(item)
    for alert in alerts:
        add_candidate(_alert_attention_item(alert))
    for task in held_tasks:
        reason = "parked_task" if task.status == TaskStatus.PARKED else "blocked_task"
        add_candidate(_task_attention_item(task, reason=reason, severity="watch"))
    for task in handmerge_preserved_tasks:
        add_candidate(
            _task_attention_item(
                task,
                reason="hand_merge_required",
                severity="needs_operator",
            )
        )
    for task in failed_tasks:
        add_candidate(_task_attention_item(task, reason="failed_task", severity="watch"))
    for task in unmet_experiment_preview:
        add_candidate(_experiment_outcome_attention_item(task))
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
        + open_action_message_count
        + approval_count
        + handmerge_preserved_count
        + total_local_runner_hold_count
        + stale_runner_pin_count
        + dead_runner_needs_operator_count
        + stale_provisioning_count
        + stale_queued_count
        + len(manual_directions)
        + len(churn_recommended_directions)
        + pending_confirmation_count
        + error_alert_count
        + stuck_criteria_count
        + coverage_blocked_count
        + attestation_expired_count
        + attestation_expiring_count
        + corroboration_failed_count
        + (1 if stale_debt_count > 0 else 0)
    )
    total_watch = (
        generic_failed_count
        + blocked_count
        + parked_count
        + warning_alert_count
        + dead_runner_watch_count
        + bug_report_count
        + feedback_count
        + unmet_experiment_count
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
            "stuck_acceptance_criteria_preview": _STUCK_ACCEPTANCE_CRITERIA_PREVIEW_LIMIT,
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
            "stuck_acceptance_criteria": stuck_acceptance_criteria_count,
            "manual_directions": len(manual_directions),
            "churn_retirement_recommended": len(churn_recommended_directions),
            "pending_confirmations": pending_confirmation_count,
            "stuck_criteria": stuck_criteria_count,
            "coverage_blocked": coverage_blocked_count,
            "phantom_criterion_bookkeeping": (
                1 if phantom_bookkeeping_report.get("has_drift") else 0
            ),
            "attestation_expired": attestation_expired_count,
            "attestation_expiring": attestation_expiring_count,
            "corroboration_failed": corroboration_failed_count,
            "alerts": error_alert_count + warning_alert_count,
            "error_alerts": error_alert_count,
            "warning_alerts": warning_alert_count,
            "failed_tasks": generic_failed_count,
            "blocked_tasks": blocked_count,
            "parked_tasks": parked_count,
            "handmerge_preserved_tasks": handmerge_preserved_count,
            "experiment_outcomes": unmet_experiment_count,
            "stale_completion_debts": stale_debt_count,
        },
        "items": items,
    }
