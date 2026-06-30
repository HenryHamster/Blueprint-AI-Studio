"""Derived operator actions for the Strategic work session.

The action feed is a bounded projection over existing attention/runtime state.
It deliberately does not introduce a second lifecycle for tasks, messages, or
alerts; durable truth remains in those existing models and endpoints.
"""

from __future__ import annotations

from typing import Any


def _compact_text(value: Any, *, max_chars: int = 180) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def _compact_action_target(target: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "surface",
        "project_id",
        "section",
        "task_id",
        "message_id",
        "direction_id",
        "alert_id",
        "action",
        "document_id",
        "artifact_id",
        "cycle_id",
    ):
        value = _compact_text(target.get(key), max_chars=160)
        if value:
            compact[key] = value
    return compact or None


def _action(
    action_id: str,
    kind: str,
    label: str,
    reason: str | None,
    *,
    priority: str,
    source: str,
    target: dict[str, Any] | None = None,
    enabled: bool = True,
    blocked_reason: str | None = None,
) -> dict[str, Any] | None:
    action_label = _compact_text(label, max_chars=96)
    if not action_label:
        return None
    action: dict[str, Any] = {
        "id": _compact_text(action_id, max_chars=180) or action_id[:180],
        "kind": _compact_text(kind, max_chars=40) or "open",
        "label": action_label,
        "reason": _compact_text(reason, max_chars=180) or "",
        "priority": _compact_text(priority, max_chars=40) or "normal",
        "source": _compact_text(source, max_chars=40) or "session",
        "target": _compact_action_target(target),
        "enabled": bool(enabled),
    }
    compact_blocked_reason = _compact_text(blocked_reason, max_chars=160)
    if compact_blocked_reason:
        action["blocked_reason"] = compact_blocked_reason
    return action


def _attention_action_kind(item: dict[str, Any]) -> str:
    item_kind = str(item.get("kind") or "")
    reason = str(item.get("reason") or "")
    if item_kind == "message" and reason == "question":
        return "reply"
    if reason == "approval_required":
        return "approve"
    if reason in {"manual_intervention", "blocked_task", "failed_task"}:
        return "review"
    return "inspect"


def _attention_action_priority(item: dict[str, Any]) -> str:
    severity = str(item.get("severity") or "")
    reason = str(item.get("reason") or "")
    if severity == "needs_operator" and reason in {
        "question",
        "blocker",
        "local_runner_hold",
        "alert",
    }:
        return "critical"
    if severity == "needs_operator":
        return "high"
    if reason in {"failed_task", "blocked_task", "parked_task"}:
        return "normal"
    return "low"


def _attention_action_label(item: dict[str, Any]) -> str:
    reason = str(item.get("reason") or "")
    if reason == "question":
        return "Reply to Strategic question"
    if reason == "blocker":
        return "Inspect blocker"
    if reason == "approval_required":
        return "Review approval"
    if reason == "local_runner_hold":
        return "Check local runner"
    if reason == "manual_intervention":
        return "Review direction"
    if reason == "failed_task":
        return "Inspect failed task"
    if reason == "blocked_task":
        return "Inspect blocked task"
    if reason == "parked_task":
        return "Inspect parked task"
    if reason == "alert":
        return "Inspect alert"
    return str(item.get("label") or "Open item")


def _attention_action_target(item: dict[str, Any]) -> dict[str, Any] | None:
    ref = item.get("ref") if isinstance(item.get("ref"), dict) else {}
    ref_kind = str(ref.get("kind") or item.get("kind") or "")
    ref_id = _compact_text(ref.get("id"), max_chars=160)
    project_id = _compact_text(ref.get("project_id"), max_chars=160)
    reason = str(item.get("reason") or "")

    if ref_kind == "message" and ref_id:
        target = {
            "surface": "messages",
            "message_id": ref_id,
            "action": "reply" if reason == "question" else "highlight",
        }
    elif ref_kind == "task" and ref_id:
        target = {
            "surface": "runtime" if reason == "local_runner_hold" else "task",
            "task_id": ref_id,
        }
        if reason == "approval_required":
            target["action"] = "approve"
        if reason == "local_runner_hold":
            target["section"] = "devices"
    elif ref_kind == "direction" and ref_id:
        target = {
            "surface": "project",
            "section": "directions",
            "direction_id": ref_id,
        }
    elif ref_kind == "alert" and ref_id:
        event_type = str(item.get("event_type") or "")
        domain = str(item.get("domain") or "")
        task_id = _compact_text(item.get("task_id"), max_chars=160)
        if task_id:
            target = {
                "surface": "task",
                "task_id": task_id,
                "alert_id": ref_id,
            }
        else:
            section = "devices" if "runner" in f"{event_type} {domain}" else "health"
            target = {
                "surface": "runtime",
                "alert_id": ref_id,
                "section": section,
            }
    else:
        return None

    if project_id:
        target["project_id"] = project_id
    return target


def _attention_actions(attention: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(attention, dict):
        return []
    items = attention.get("items", [])
    if not isinstance(items, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = _compact_text(item.get("id"), max_chars=160)
        if not item_id:
            continue
        action = _action(
            f"attention.{item_id}",
            _attention_action_kind(item),
            _attention_action_label(item),
            item.get("detail") or item.get("label"),
            priority=_attention_action_priority(item),
            source="attention",
            target=_attention_action_target(item),
        )
        if action:
            actions.append(action)
    return actions


def _runtime_actions(
    runtime_work: dict[str, Any] | None,
    *,
    has_local_runner_attention: bool,
) -> list[dict[str, Any]]:
    if not isinstance(runtime_work, dict):
        return []
    actions: list[dict[str, Any]] = []
    compute_jobs = runtime_work.get("compute_jobs", {})
    compute_counts = (
        compute_jobs.get("counts", {}) if isinstance(compute_jobs, dict) else {}
    )
    failed_recent = int(compute_counts.get("failed_recent") or 0)
    timed_out_recent = int(compute_counts.get("timed_out_recent") or 0)
    if failed_recent or timed_out_recent:
        problem_count = failed_recent + timed_out_recent
        label = "Inspect failed compute" if failed_recent else "Inspect timed out compute"
        action = _action(
            "runtime.compute.problem_recent",
            "inspect",
            label,
            f"{problem_count} compute job problem in the recent window",
            priority="high",
            source="runtime",
            target={"surface": "runtime", "section": "compute"},
        )
        if action:
            actions.append(action)

    local_runner_holds = runtime_work.get("local_runner_holds", {})
    local_runner_hold_count = (
        int(local_runner_holds.get("count") or 0)
        if isinstance(local_runner_holds, dict)
        else 0
    )
    if local_runner_hold_count and not has_local_runner_attention:
        action = _action(
            "runtime.local_runner_holds",
            "inspect",
            "Check local runner holds",
            f"{local_runner_hold_count} task needs local hardware",
            priority="high",
            source="runtime",
            target={"surface": "runtime", "section": "devices"},
        )
        if action:
            actions.append(action)

    parked_wakes = runtime_work.get("parked_wakes", {})
    wake_counts = parked_wakes.get("counts", {}) if isinstance(parked_wakes, dict) else {}
    wake_problem_count = int(wake_counts.get("problem") or 0)
    if wake_problem_count:
        action = _action(
            "runtime.parked_wakes.problem",
            "inspect",
            "Inspect wake problems",
            f"{wake_problem_count} parked wake needs attention",
            priority="high",
            source="runtime",
            target={"surface": "runtime", "section": "compute"},
        )
        if action:
            actions.append(action)
    return actions


def _local_runner_actions(
    local_runner: dict[str, Any] | None,
    *,
    has_local_runner_attention: bool,
) -> list[dict[str, Any]]:
    if has_local_runner_attention:
        return []
    if not isinstance(local_runner, dict):
        return []
    continuation = local_runner.get("continuation")
    if not isinstance(continuation, dict):
        return []
    if not (continuation.get("enabled") and continuation.get("fresh")):
        return []
    if str(local_runner.get("mode") or "") == "local":
        return []
    if str(local_runner.get("status") or "") == "working" and (
        local_runner.get("location") == "locally"
    ):
        return []
    try:
        eligible_live_count = int(local_runner.get("eligible_live_count") or 0)
    except (TypeError, ValueError):
        eligible_live_count = 0
    if eligible_live_count > 0:
        return []

    reason = _compact_text(
        continuation.get("reason")
        or local_runner.get("status_reason")
        or local_runner.get("fallback_reason")
        or "runner_unavailable",
        max_chars=80,
    )
    action = _action(
        "runtime.local_strategic.continuation_runner",
        "inspect",
        "Check Workday runner",
        "Workday mode is active but no eligible local Strategic runner is ready",
        priority="high",
        source="runtime",
        target={"surface": "runtime", "section": "devices"},
        blocked_reason=reason,
    )
    return [action] if action else []


def _priority_rank(priority: Any) -> int:
    return {
        "critical": 0,
        "high": 1,
        "normal": 2,
        "low": 3,
    }.get(str(priority or ""), 4)


def build_work_session_actions(
    *,
    attention: dict[str, Any] | None,
    runtime_work: dict[str, Any] | None,
    context_status: str | None,
    local_runner: dict[str, Any] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    attention_actions = _attention_actions(attention)
    has_local_runner_attention = any(
        isinstance(action.get("target"), dict)
        and (
            action["target"].get("section") == "devices"
            or "local_runner" in str(action.get("id") or "")
        )
        for action in attention_actions
    )
    actions = [
        *attention_actions,
        *_runtime_actions(
            runtime_work,
            has_local_runner_attention=has_local_runner_attention,
        ),
        *_local_runner_actions(
            local_runner,
            has_local_runner_attention=has_local_runner_attention,
        ),
    ]
    if context_status == "stale":
        action = _action(
            "context.package.stale",
            "review",
            "Review stale context",
            "The Strategic context package is stale",
            priority="normal",
            source="context",
            target={"surface": "project", "section": "problem-model"},
        )
        if action:
            actions.append(action)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in actions:
        action_id = str(action.get("id") or "")
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        deduped.append(action)
    ranked = sorted(
        enumerate(deduped),
        key=lambda entry: (_priority_rank(entry[1].get("priority")), entry[0]),
    )
    return [action for _, action in ranked[:limit]]
