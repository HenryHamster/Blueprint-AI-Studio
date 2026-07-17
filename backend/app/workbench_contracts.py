"""Canonical operator workbench command, target, and event contracts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlparse

WORKBENCH_CONTRACT_VERSION = 1

SURFACE_MESSAGES = "messages"
SURFACE_PIPELINE = "pipeline"
SURFACE_PROJECT = "project"
SURFACE_RUNTIME = "runtime"
SURFACE_TASK = "task"
SURFACE_KNOWLEDGE = "knowledge"
SURFACE_STRATEGIC_SESSION = "strategic_session"
SURFACE_DIRECTIONS = "directions"
SURFACE_EXTERNAL = "external"

TARGET_SURFACES = frozenset(
    (
        SURFACE_MESSAGES,
        SURFACE_PIPELINE,
        SURFACE_PROJECT,
        SURFACE_RUNTIME,
        SURFACE_TASK,
        SURFACE_KNOWLEDGE,
        SURFACE_EXTERNAL,
    )
)
REFETCH_SURFACES = frozenset(
    (
        SURFACE_MESSAGES,
        SURFACE_STRATEGIC_SESSION,
        SURFACE_DIRECTIONS,
        SURFACE_PIPELINE,
        SURFACE_RUNTIME,
    )
)
DEFAULT_REFETCH_SURFACES = (SURFACE_STRATEGIC_SESSION,)

PROJECT_SECTIONS = frozenset(
    (
        "running-update",
        "directions",
        "problem-model",
        "design-doc",
        "manage",
        "selected_refs",
        "overview",
        "build-config",
        "branches",
        "auto-review",
        "resource-envelope",
        "auto-remediation",
        "issue-implementation",
        "alert-domains",
        "access",
    )
)
PIPELINE_SECTIONS = frozenset(("flow", "board", "archive"))
RUNTIME_SECTIONS = frozenset(
    (
        "health",
        "devices",
        "compute",
        "accounts",
        "settings",
        "analytics",
        "general",
        "orchestrator",
        "deploy",
        "provider-routing",
        "provider-settings",
        "notifications",
        "advanced",
    )
)

TARGET_KEYS = (
    "surface",
    "id",
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
    "runner_id",
    "url",
)

COMMAND_GROUPS = frozenset(
    (
        "session",
        "mode",
        "context",
        "runtime",
        "attention",
        "navigation",
        "task",
        "message",
    )
)
COMMAND_PRIORITIES = frozenset(("critical", "high", "normal", "low"))
# Command-id classification set (session_mode_from_command_id). Intentionally
# retains the retired ``check_in`` / ``monitor`` ids so a stale command from an
# old client is still recognized as a mode command and tolerated (its persisted
# value is coerced to ``chat`` by sanitize_workbench_state). Only ``chat`` and
# ``continuous`` mode commands are emitted going forward.
EXECUTABLE_SESSION_MODES = frozenset(("chat", "check_in", "continuous", "monitor"))

COMMAND_ID_SESSION_CHECK_IN = "session.check_in"
COMMAND_ID_SESSION_CLEAR_SELECTED_REFS = "session.clear_selected_refs"
COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED = "session.runner.clear_preferred"
COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN = "attention.messages.mark_seen"
SESSION_MODE_COMMAND_PREFIX = "session.mode."
RUNNER_PREFER_COMMAND_PREFIX = "session.runner.prefer."
ALERT_DISMISS_COMMAND_PREFIX = "attention.alert.dismiss:"
MUTATION_METHODS = frozenset(("PATCH", "POST", "PUT"))

MESSAGE_ARMING_OUTCOMES = (
    "steering",
    "passive_note",
    "routine_event",
    "not_armed",
    "duplicate",
)

_SURFACE_ALIASES = {
    "chat": SURFACE_MESSAGES,
    "message": SURFACE_MESSAGES,
}

_SECTION_ALIASES_BY_SURFACE = {
    SURFACE_RUNTIME: {
        "runner": "devices",
        "runners": "devices",
        "local_runner": "devices",
        "local-runners": "devices",
        "fleet": "devices",
    },
}

_SECTIONS_BY_SURFACE = {
    SURFACE_PROJECT: PROJECT_SECTIONS,
    SURFACE_PIPELINE: PIPELINE_SECTIONS,
    SURFACE_RUNTIME: RUNTIME_SECTIONS,
}

WEBSOCKET_EMITTER_INVENTORY = {
    "workbench_v1": ("backend/app/workbench_events.py",),
    "workbench_v1_producers": (
        "backend/app/orchestrator_runner_dispatch.py",
        "backend/app/project_message_projection.py",
        "backend/app/routers/admin_notifications.py",
        "backend/app/routers/admin_project_strategic_session.py",
        "backend/app/routers/admin_runners.py",
        "backend/app/routers/runner.py",
        "backend/app/strategic_events.py",
    ),
    "transport": (
        "backend/app/broadcast.py",
        "backend/app/event_fanout.py",
        "backend/app/routers/projects.py",
    ),
    "legacy_compatibility": (
        "backend/app/admin_task_mutations.py",
        "backend/app/agent_status_spawn.py",
        "backend/app/alert_dispatch.py",
        "backend/app/alert_policy.py",
        "backend/app/autonomous_research.py",
        "backend/app/blocked_task_unblock_sweeps.py",
        "backend/app/branch_cleanup.py",
        "backend/app/branch_resolution.py",
        "backend/app/ci_completion.py",
        "backend/app/cost_ceiling.py",
        "backend/app/dep_lifecycle.py",
        "backend/app/deploy_health.py",
        "backend/app/deploy_health_alerts.py",
        "backend/app/hotfix_backmerge.py",
        "backend/app/knowledge_completion.py",
        "backend/app/message_supersession.py",
        "backend/app/monthly_budget_alerts.py",
        "backend/app/orchestrator.py",
        "backend/app/orchestrator_ci_monitor.py",
        "backend/app/orchestrator_provision.py",
        "backend/app/orchestrator_runner_dispatch.py",
        "backend/app/plan_completion.py",
        "backend/app/reaper.py",
        "backend/app/reaper_bug_message_sweeps.py",
        "backend/app/reaper_infra_recovery.py",
        "backend/app/reaper_sweeps.py",
        "backend/app/recovery_evaluating.py",
        "backend/app/recovery_evaluating_approval.py",
        "backend/app/recovery_evaluating_ci_backstop.py",
        "backend/app/recovery_evaluating_hard_timeout.py",
        "backend/app/recovery_evaluating_pr_check.py",
        "backend/app/recovery_evaluating_reaped_owner.py",
        "backend/app/recovery_evaluating_strand.py",
        "backend/app/recovery_post_commit_orphans.py",
        "backend/app/research_direction_state.py",
        "backend/app/strategic_completion.py",
        "backend/app/strategic_resident_realtime.py",
        "backend/app/strategic_resident_terminal.py",
        "backend/app/review_pipeline.py",
        "backend/app/rollup_management.py",
        "backend/app/routers/admin_agent_ops.py",
        "backend/app/routers/admin_debug.py",
        "backend/app/routers/admin_notifications.py",
        "backend/app/routers/admin_project_strategic_session.py",
        "backend/app/routers/agent.py",
        "backend/app/routers/agent_status.py",
        "backend/app/routers/agent_warm_continuity.py",
        "backend/app/routers/directions.py",
        "backend/app/routers/runner.py",
        "backend/app/runner_decline.py",
        "backend/app/runner_health_alerts.py",
        "backend/app/runner_pull_skip.py",
        "backend/app/services.py",
        "backend/app/strategic_completion.py",
        "backend/app/strategic_events.py",
        "backend/app/task_completion.py",
        "backend/app/task_completion_self_audit.py",
        "backend/app/task_lifecycle.py",
        "backend/app/task_lifecycle_requeue.py",
        "backend/app/warm_pool_maintainer.py",
    ),
}

WorkSessionCommandKind = Literal[
    "strategic_workbench",
    "messages_last_seen",
    "alert_dismiss",
]


@dataclass(frozen=True)
class WorkSessionExecutableCommand:
    command_id: str
    kind: WorkSessionCommandKind
    method: str
    path: str
    workbench_patch: dict[str, Any] | None = None
    messages_seen_timestamp: int | None = None
    runner_id: str | None = None
    alert_id: str | None = None


def compact_text(value: Any, *, max_chars: int = 180) -> str | None:
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


def _normalize_surface(value: Any) -> str | None:
    surface = compact_text(value, max_chars=80)
    if not surface:
        return None
    surface = _SURFACE_ALIASES.get(surface, surface)
    return surface if surface in TARGET_SURFACES else None


def _normalize_section(surface: str, value: Any) -> str | None:
    section = compact_text(value, max_chars=80)
    if not section:
        return None
    section = _SECTION_ALIASES_BY_SURFACE.get(surface, {}).get(section, section)
    allowed = _SECTIONS_BY_SURFACE.get(surface)
    if allowed is not None and section not in allowed:
        return None
    return section


def normalize_target(target: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(target, Mapping):
        return None
    surface = _normalize_surface(target.get("surface"))
    if not surface:
        return None

    normalized: dict[str, Any] = {"surface": surface}
    for key in TARGET_KEYS:
        if key == "surface":
            continue
        if key == "section":
            section = _normalize_section(surface, target.get("section"))
            if target.get("section") is not None and not section:
                if surface == SURFACE_RUNTIME and compact_text(
                    target.get("runner_id"), max_chars=160
                ):
                    continue
                return None
            if section:
                normalized[key] = section
            continue
        value = compact_text(target.get(key), max_chars=160)
        if value:
            normalized[key] = value

    if surface == SURFACE_TASK and "task_id" not in normalized:
        return None
    if surface == SURFACE_EXTERNAL:
        url = normalized.get("url")
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
    return normalized


def normalize_refetch_surfaces(
    values: Sequence[Any] | None,
    *,
    default: Sequence[str] | None = DEFAULT_REFETCH_SURFACES,
    limit: int = 8,
) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    source_values = values if values is not None else default
    for raw in source_values or ():
        surface = compact_text(raw, max_chars=80)
        if not surface or surface not in REFETCH_SURFACES or surface in seen:
            continue
        surfaces.append(surface)
        seen.add(surface)
        if len(surfaces) >= limit:
            break
    if not surfaces and values is not None and default:
        return normalize_refetch_surfaces(None, default=default, limit=limit)
    return surfaces


def normalize_command_group(value: Any) -> str:
    group = compact_text(value, max_chars=40)
    return group if group in COMMAND_GROUPS else "navigation"


def normalize_command_priority(value: Any) -> str:
    priority = compact_text(value, max_chars=40)
    return priority if priority in COMMAND_PRIORITIES else "normal"


def strategic_session_endpoint(project_id: Any) -> str:
    return f"/api/admin/projects/{quote(str(project_id), safe='')}/strategic-session"


def messages_last_seen_endpoint() -> str:
    return "/api/admin/messages/last-seen"


def alert_dismiss_endpoint(alert_id: Any) -> str:
    return f"/api/admin/alerts/{quote(str(alert_id), safe='')}/dismiss"


def session_mode_command_id(mode: str) -> str:
    return f"{SESSION_MODE_COMMAND_PREFIX}{mode}"


def session_mode_from_command_id(command_id: str) -> str | None:
    if not command_id.startswith(SESSION_MODE_COMMAND_PREFIX):
        return None
    mode = command_id.removeprefix(SESSION_MODE_COMMAND_PREFIX)
    return mode if mode in EXECUTABLE_SESSION_MODES else None


def runner_preference_command_id(runner_id: str) -> str:
    return f"{RUNNER_PREFER_COMMAND_PREFIX}{runner_id}"


def alert_id_from_command_id(command_id: str) -> str | None:
    if not command_id.startswith(ALERT_DISMISS_COMMAND_PREFIX):
        return None
    try:
        return str(uuid.UUID(command_id.removeprefix(ALERT_DISMISS_COMMAND_PREFIX)))
    except (TypeError, ValueError, AttributeError):
        return None


def runner_id_from_command_id(command_id: str) -> str | None:
    if not command_id.startswith(RUNNER_PREFER_COMMAND_PREFIX):
        return None
    return compact_text(
        command_id.removeprefix(RUNNER_PREFER_COMMAND_PREFIX),
        max_chars=160,
    )


def alert_dismiss_command_id(alert_id: str | uuid.UUID) -> str:
    return f"{ALERT_DISMISS_COMMAND_PREFIX}{alert_id}"


def work_session_command_kind(command_id: str) -> WorkSessionCommandKind | None:
    if session_mode_from_command_id(command_id) is not None:
        return "strategic_workbench"
    if command_id == COMMAND_ID_SESSION_CLEAR_SELECTED_REFS:
        return "strategic_workbench"
    if command_id.startswith(RUNNER_PREFER_COMMAND_PREFIX):
        return "strategic_workbench" if runner_id_from_command_id(command_id) else None
    if command_id == COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED:
        return "strategic_workbench"
    if command_id == COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN:
        return "messages_last_seen"
    if command_id.startswith(ALERT_DISMISS_COMMAND_PREFIX):
        return "alert_dismiss" if alert_id_from_command_id(command_id) else None
    return None


def normalize_command_descriptor(
    command: Mapping[str, Any] | None,
    *,
    project_id: Any | None = None,
) -> dict[str, Any] | None:
    if not isinstance(command, Mapping):
        return None
    command_id = compact_text(command.get("id"), max_chars=180)
    label = compact_text(command.get("label"), max_chars=96)
    target = normalize_target(command.get("target"))
    if not command_id or not label or not target:
        return None
    normalized: dict[str, Any] = {
        "id": command_id,
        "label": label,
        "group": normalize_command_group(command.get("group")),
        "priority": normalize_command_priority(command.get("priority")),
        "enabled": bool(command.get("enabled", True)),
        "target": target,
        "refetch_surfaces": normalize_refetch_surfaces(
            command.get("refetch_surfaces")
            if isinstance(command.get("refetch_surfaces"), Sequence)
            and not isinstance(command.get("refetch_surfaces"), (str, bytes))
            else None,
            default=(),
        ),
        "executable": False,
    }
    mutation = command.get("mutation")
    if isinstance(mutation, Mapping):
        method = compact_text(mutation.get("method"), max_chars=20)
        path = compact_text(mutation.get("path"), max_chars=500)
        if not method or not path:
            return None
        body = mutation.get("body") if isinstance(mutation.get("body"), Mapping) else {}
        normalized["mutation"] = {
            "method": method.upper(),
            "path": path,
            "body": dict(body),
        }
        if validate_executable_command_descriptor(
            normalized,
            project_id=project_id,
        ):
            return None
        normalized["executable"] = True
    return normalized


def _mutation(command: Mapping[str, Any]) -> Mapping[str, Any]:
    mutation = command.get("mutation")
    return mutation if isinstance(mutation, Mapping) else {}


def _mutation_body(command: Mapping[str, Any]) -> Mapping[str, Any]:
    body = _mutation(command).get("body")
    return body if isinstance(body, Mapping) else {}


def _project_id_text(project_id: Any | None, target: Mapping[str, Any]) -> str | None:
    if project_id is not None:
        return str(project_id)
    return compact_text(target.get("project_id"), max_chars=160)


def _target_project_matches(project_id: Any | None, target: Mapping[str, Any]) -> bool:
    expected = _project_id_text(project_id, target)
    return bool(expected and target.get("project_id") == expected)


def _method_and_path(command: Mapping[str, Any]) -> tuple[str, str] | None:
    mutation = _mutation(command)
    method = compact_text(mutation.get("method"), max_chars=20)
    path = compact_text(mutation.get("path"), max_chars=500)
    if not method or not path:
        return None
    return method.upper(), path


def _body_exact(command: Mapping[str, Any], expected: dict[str, Any]) -> bool:
    return _mutation_body(command) == expected


def _strategic_workbench_command_contract(
    command: Mapping[str, Any],
    *,
    project_id: Any,
    command_id: str,
    method: str,
    path: str,
    target: Mapping[str, Any],
) -> WorkSessionExecutableCommand | None:
    project_text = str(project_id)
    if method != "POST" or path != strategic_session_endpoint(project_text):
        return None
    if not _target_project_matches(project_text, target):
        return None

    mode = session_mode_from_command_id(command_id)
    if mode is not None:
        expected_body = {"workbench": {"interaction_mode": mode}}
        if (
            target.get("surface") != SURFACE_MESSAGES
            or target.get("action") != f"set_{mode}_mode"
            or not _body_exact(command, expected_body)
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="strategic_workbench",
            method=method,
            path=path,
            workbench_patch={"interaction_mode": mode},
        )

    if command_id == COMMAND_ID_SESSION_CLEAR_SELECTED_REFS:
        expected_body = {"workbench": {"selected_refs": []}}
        if (
            target.get("surface") != SURFACE_PROJECT
            or target.get("section") != "selected_refs"
            or target.get("action") != "clear"
            or not _body_exact(command, expected_body)
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="strategic_workbench",
            method=method,
            path=path,
            workbench_patch={"selected_refs": []},
        )

    runner_id = runner_id_from_command_id(command_id)
    if runner_id is not None:
        expected_body = {"workbench": {"preferred_runner_id": runner_id}}
        if (
            target.get("surface") != SURFACE_RUNTIME
            or target.get("section") != "devices"
            or target.get("action") != "prefer_runner"
            or target.get("runner_id") != runner_id
            or not _body_exact(command, expected_body)
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="strategic_workbench",
            method=method,
            path=path,
            workbench_patch={"preferred_runner_id": runner_id},
            runner_id=runner_id,
        )

    if command_id == COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED:
        runner_id = compact_text(target.get("runner_id"), max_chars=160)
        expected_body = {"workbench": {"preferred_runner_id": None}}
        if (
            target.get("surface") != SURFACE_RUNTIME
            or target.get("section") != "devices"
            or target.get("action") != "clear_preferred_runner"
            or not runner_id
            or not _body_exact(command, expected_body)
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="strategic_workbench",
            method=method,
            path=path,
            workbench_patch={"preferred_runner_id": None},
            runner_id=runner_id,
        )

    return None


def executable_work_session_command_contract(
    command: Mapping[str, Any],
    *,
    project_id: Any,
) -> WorkSessionExecutableCommand | None:
    """Return the parsed executable contract for a mutation descriptor."""

    command_id = compact_text(command.get("id"), max_chars=180)
    if not command_id:
        return None
    kind = work_session_command_kind(command_id)
    method_path = _method_and_path(command)
    target = normalize_target(command.get("target"))
    if kind is None or method_path is None or not target:
        return None
    method, path = method_path
    if method not in MUTATION_METHODS or not _target_project_matches(project_id, target):
        return None

    if kind == "strategic_workbench":
        return _strategic_workbench_command_contract(
            command,
            project_id=project_id,
            command_id=command_id,
            method=method,
            path=path,
            target=target,
        )

    project_text = str(project_id)
    if kind == "messages_last_seen":
        body = _mutation_body(command)
        timestamp = body.get("timestamp")
        expected_body = {"project_id": project_text, "timestamp": timestamp}
        if (
            method != "PUT"
            or path != messages_last_seen_endpoint()
            or target.get("surface") != SURFACE_MESSAGES
            or target.get("action") != "mark_seen"
            or not isinstance(timestamp, int)
            or not _body_exact(command, expected_body)
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="messages_last_seen",
            method=method,
            path=path,
            messages_seen_timestamp=timestamp,
        )

    if kind == "alert_dismiss":
        alert_id = alert_id_from_command_id(command_id)
        if (
            not alert_id
            or method != "POST"
            or path != alert_dismiss_endpoint(alert_id)
            or target.get("surface") != SURFACE_RUNTIME
            or target.get("section") not in {"health", "devices"}
            or target.get("action") != "dismiss_alert"
            or target.get("alert_id") != alert_id
            or not _body_exact(command, {})
        ):
            return None
        return WorkSessionExecutableCommand(
            command_id=command_id,
            kind="alert_dismiss",
            method=method,
            path=path,
            alert_id=alert_id,
        )

    return None


def validate_executable_command_descriptor(
    command: Mapping[str, Any],
    *,
    project_id: Any | None = None,
) -> str | None:
    """Return an error string when an executable command descriptor is invalid."""

    command_id = compact_text(command.get("id"), max_chars=180)
    if not command_id:
        return "missing command id"
    kind = work_session_command_kind(command_id)
    if not kind:
        return "command is not executable"

    target = normalize_target(command.get("target"))
    if not target:
        return "command target is invalid"
    if not _target_project_matches(project_id, target):
        return "command target project mismatch"

    method_path = _method_and_path(command)
    if method_path is None:
        return "command mutation is missing method or path"
    method, _path = method_path
    if method not in MUTATION_METHODS:
        return "command mutation method is not allowlisted"

    if project_id is None:
        return "command target project mismatch"
    if executable_work_session_command_contract(command, project_id=project_id) is None:
        return "command descriptor mismatch"
    return None


def build_operator_workbench_contract_fixture() -> dict[str, Any]:
    project_id = "proj-1"
    command = normalize_command_descriptor(
        {
            "id": "session.mode.continuous",
            "label": "Workday mode",
            "group": "mode",
            "priority": "normal",
            "enabled": True,
            "target": {
                "surface": "messages",
                "project_id": project_id,
                "action": "set_continuous_mode",
            },
            "refetch_surfaces": ["strategic_session", "runtime"],
            "mutation": {
                "method": "POST",
                "path": strategic_session_endpoint(project_id),
                "body": {"workbench": {"interaction_mode": "continuous"}},
            },
        },
        project_id=project_id,
    )
    runtime_target = normalize_target(
        {"surface": "runtime", "section": "runners", "runner_id": "runner-1"}
    )
    task_target = normalize_target({"surface": "task", "task_id": "task-1"})
    message_target = normalize_target(
        {"surface": "messages", "message_id": "msg-1", "action": "reply"}
    )
    event_payload = {
        "schema_version": 1,
        "project_id": project_id,
        "resource": "local_runner",
        "action": "heartbeat",
        "refetch": {"surfaces": ["runtime", "strategic_session"]},
        "target": runtime_target,
    }
    strategic_state_response = {
        "project_id": project_id,
        "strategic": {
            "active_cycle": None,
            "active_task": None,
            "active_phase": None,
            "latest_cycle": None,
            "refresh": {
                "pending": None,
                "last_fired_at": None,
                "last_fired_origin": None,
            },
        },
        "running_update": {
            "excerpt": "Strategic is validating the operator workbench contract.",
            "char_count": 58,
            "acknowledged_at": None,
        },
        "problem_model": {
            "revision": 1,
            "current_focus": "Operator workbench contracts",
            "purpose": "Keep commands and targets contract-driven.",
            "active_gap_count": 1,
            "capability_count": 1,
            "pending_confirmation_count": 0,
            "top_gap": {
                "id": "gap-contracts",
                "name": "Command targets must stay canonical",
                "priority": "high",
                "status": "active",
            },
        },
        "directions": {
            "total_count": 1,
            "active_count": 1,
            "paused_count": 0,
            "primary": {
                "id": "direction-main",
                "text": "Keep operator commands aligned.",
                "priority": "high",
                "status": "active",
                "mode": "continuous",
            },
        },
        "messages": {"counts": {"open": 1, "linked": 0}, "items": []},
        "tasks": {
            "counts": {
                "queued": 0,
                "running": 1,
                "evaluating": 0,
                "blocked": 0,
                "parked": 0,
                "failed": 0,
                "cancelled": 0,
                "completed": 0,
                "total": 1,
            },
            "cumulative_estimated_cost_usd": 0,
            "focus": {"version": 1, "bucket_limit": 8, "has_more": False, "buckets": {}},
        },
        "attention": {
            "level": "watch",
            "summary": "1 open message",
            "last_seen_at": None,
            "has_more": True,
            "limits": {
                "items": 8,
                "message_blocker_preview": 8,
                "message_question_preview": 8,
                "message_watch_preview": 24,
                "stale_task_preview": 4,
                "alert_error_preview": 4,
                "alert_warning_preview": 4,
            },
            "counts": {
                "needs_operator": 0,
                "watch": 1,
                "visible_needs_operator": 0,
                "visible_watch": 0,
                "unread": 1,
                "questions": 0,
                "blockers": 0,
                "bug_reports": 0,
                "feedback": 1,
                "approvals": 0,
                "local_runner_holds": 0,
                "compute_local_runner_holds": 0,
                "strategic_local_runner_holds": 0,
                "stale_runner_pins": 0,
                "stale_provisioning_tasks": 0,
                "stale_queued_tasks": 0,
                "manual_directions": 0,
                "alerts": 0,
                "error_alerts": 0,
                "warning_alerts": 0,
                "failed_tasks": 0,
                "blocked_tasks": 0,
                "parked_tasks": 0,
            },
            "items": [],
        },
        "context_package": {
            "id": "strategic-context:proj-1",
            "title": "Operator workbench contract",
            "status": "ready",
            "updated_at": "2026-07-01T10:00:00Z",
            "item_count": 3,
            "sources": [{"kind": "runtime", "count": 1}],
            "canvas": {
                "id": "strategic-canvas:proj-1",
                "title": "Strategic context map",
                "kind": "strategic_context",
                "node_count": 1,
                "edge_count": 0,
                "nodes": [
                    {
                        "id": "runner:runner-1",
                        "kind": "local_runner",
                        "label": "operator-desk",
                        "status": "connected",
                        "target": runtime_target,
                    }
                ],
                "edges": [],
                "drilldowns": [],
            },
            "resources": [
                {
                    "id": "strategic-canvas:proj-1",
                    "kind": "canvas",
                    "label": "Strategic context map",
                    "target": {"surface": "project", "section": "problem-model"},
                }
            ],
            "evidence": {
                "knowledge": {"total": 0, "items": []},
                "artifacts": {"total": 0, "items": []},
            },
            "drilldowns": [],
        },
        "runtime": {
            "local_runner": {
                "status": "idle",
                "location": "locally",
                "status_reason": None,
                "mode": "local_ready",
                "strategic_runner_count": 1,
                "live_runner_count": 1,
                "eligible_live_count": 1,
                "stale_runner_count": 0,
                "busy_runner_count": 0,
                "agent_unavailable_count": 0,
                "active_runner_id": None,
                "active_task_pinned": False,
                "active_task_requires_local": False,
                "active_task_awaiting_local_runner_since": None,
                "active_task_runtime": None,
                "runners": [
                    {
                        "id": "runner-1",
                        "hostname": "operator-desk",
                        "status": "active",
                        "last_seen_at": "2026-07-01T10:00:00Z",
                        "created_at": "2026-07-01T09:00:00Z",
                        "capabilities": {
                            "runner_preset": "strategic_local",
                            "workload": "strategic",
                            "task_types": ["design_doc_update", "strategic_review"],
                            "has_gpu": False,
                            "max_concurrent": 1,
                            "agent_available": True,
                        },
                        "health": {
                            "device_status": "connected",
                            "is_live": True,
                            "seconds_since_last_seen": 12,
                            "active_task_count": 1,
                            "active_compute_job_count": 0,
                            "active_work_count": 1,
                            "max_concurrent": 1,
                            "available_slots": 0,
                            "agent_available": True,
                            "liveness_window_seconds": 300,
                        },
                        "handles_active_task_type": True,
                        "eligible_for_active_task": True,
                        "is_preferred": True,
                        "is_active_runner": False,
                    }
                ],
                "fallback_reason": None,
                "liveness_window_seconds": 300,
                "continuation": {
                    "enabled": False,
                    "expires_at": None,
                    "fresh": False,
                    "last_seen_at": None,
                    "owned_by_current_operator": False,
                    "owner_session_started_at": None,
                    "owner_user_id": None,
                    "ownership": "none",
                    "preferred_runner_id": None,
                    "reason": "not_enabled",
                    # Mirrors the live continuation status default, which now reads
                    # the ``strategic_continuation_ttl_seconds`` runtime setting
                    # (default 1500s / 25 min) — REPLACES the old hardcoded 12h
                    # (43200) wall that the resident-session dead-man's switch
                    # removed.
                    "ttl_seconds": 1500,
                    "updated_at": None,
                },
            },
            "work": {
                "compute_jobs": {
                    "window_minutes": 60,
                    "counts": {
                        "active": 0,
                        "terminal_recent": 0,
                        "total": 0,
                        "queued": 0,
                        "starting": 0,
                        "running": 0,
                        "polling": 0,
                        "succeeded_recent": 0,
                        "failed_recent": 0,
                        "cancelled_recent": 0,
                        "timed_out_recent": 0,
                        "criterion_measured": 0,
                        "criterion_met": 0,
                    },
                    "spend": {
                        "active_accrued_usd": 0,
                        "recent_terminal_usd": 0,
                        "window_total_usd": 0,
                    },
                    "items": [],
                },
                "held_approvals": {"count": 0, "items": []},
                "local_runner_holds": {
                    "count": 0,
                    "compute_count": 0,
                    "strategic_count": 0,
                    "items": [],
                },
                "parked_wakes": {
                    "counts": {
                        "active": 0,
                        "pending": 0,
                        "fired": 0,
                        "problem": 0,
                        "expired": 0,
                        "failed": 0,
                    },
                    "items": [],
                },
            },
        },
        "workbench": {
            "version": 1,
            "interaction_mode": "continuous",
            "active_surface": "strategic",
            "preferred_runner_id": "runner-1",
            "selected_refs": [],
            "session_started_at": "2026-07-01T09:55:00Z",
            "last_interaction_at": "2026-07-01T10:00:00Z",
        },
        "work_session": {
            "version": 1,
            "id": "strategic-session:proj-1",
            "mode": "continuous",
            "active_surface": "strategic",
            "status": "idle",
            "location": "locally",
            "status_reason": None,
            "session_started_at": "2026-07-01T09:55:00Z",
            "last_interaction_at": "2026-07-01T10:00:00Z",
            "context_package_id": "strategic-context:proj-1",
            "attention_level": "watch",
            "selected_refs": [],
            "selected_ref_descriptors": [],
            "curated_metrics": [
                {
                    "id": "runtime.local_strategic_ready",
                    "label": "Local ready",
                    "value": 1,
                    "tone": "active",
                    "target": runtime_target,
                    "provenance": {
                        "surface": "runtime",
                        "path": "runtime.local_runner.eligible_live_count",
                    },
                }
            ],
            "context_sources": [{"kind": "runtime", "count": 1}],
            "commands": [command],
            "jobs": {
                "version": 1,
                "limit": 8,
                "has_more": False,
                "items": [
                    {
                        "id": "receipt-1",
                        "command_id": "session.mode.continuous",
                        "label": "Workday mode",
                        "status": "succeeded",
                        "summary": "Command completed: Workday mode",
                        "created_at": "2026-07-01T10:00:00Z",
                        "completed_at": "2026-07-01T10:00:00Z",
                        "target": message_target,
                        "refetch_surfaces": ["strategic_session", "runtime"],
                        "result": {"kind": "workbench_update"},
                    }
                ],
            },
            "events": {
                "version": 1,
                "limit": 24,
                "has_more": False,
                "items": [
                    {
                        **event_payload,
                        "id": "event-1",
                        "occurred_at": "2026-07-01T10:00:00Z",
                        "source": "runtime",
                        "title": "Local runner ready",
                        "summary": "operator-desk connected",
                        "tone": "active",
                        "ref": {"kind": "local_runner", "id": "runner-1"},
                        "data": {"runner_id": "runner-1"},
                    }
                ],
            },
            "last_check_in": None,
            "since_last_check_in": {
                "version": 1,
                "available": False,
                "reason": "no_prior_check_in",
                "since": None,
                "until": "2026-07-01T10:00:00Z",
            },
        },
        "design": {"has_design_doc": False, "revision": None},
        "sha_freshness": {
            "active_candidate_sha": None,
            "integration_head_sha": None,
            "verdict": "unknown",
            "generated_at": None,
            "checked_at": None,
            "integration_branch": None,
            "stale": None,
            "stale_reason": "no_open_version",
        },
    }
    strategic_session_response = {
        "project_id": project_id,
        "status": "idle",
        "location": "locally",
        "status_reason": None,
        "session": {
            **strategic_state_response["work_session"],
            "runtime": strategic_state_response["runtime"]["local_runner"],
            "conversation": {
                "limit": 20,
                "has_more": False,
                "order": "oldest_first",
                "items": [
                    {
                        "id": "msg-1",
                        "project_id": project_id,
                        "message_type": "FEEDBACK",
                        "from_type": "user",
                        "from_task_id": None,
                        "content": "Keep it simple.",
                        "status": "open",
                        "created_at": "2026-07-01T10:00:00Z",
                        "context": {},
                    }
                ],
            },
            "recent_outputs": [],
        },
        "workbench": strategic_state_response["workbench"],
        "strategic": strategic_state_response["strategic"],
        "runtime": strategic_state_response["runtime"],
        "context_package": strategic_state_response["context_package"],
        "conversation": {
            "limit": 20,
            "has_more": False,
            "order": "oldest_first",
            "items": [
                {
                    "id": "msg-1",
                    "project_id": project_id,
                    "message_type": "FEEDBACK",
                    "from_type": "user",
                    "from_task_id": None,
                    "content": "Keep it simple.",
                    "status": "open",
                    "created_at": "2026-07-01T10:00:00Z",
                    "context": {},
                }
            ],
        },
        "recent_outputs": [],
        "arming": None,
    }
    return {
        "schema_version": WORKBENCH_CONTRACT_VERSION,
        "target_vocabulary": {
            "surfaces": sorted(TARGET_SURFACES),
            "refetch_surfaces": sorted(REFETCH_SURFACES),
            "project_sections": sorted(PROJECT_SECTIONS),
            "pipeline_sections": sorted(PIPELINE_SECTIONS),
            "runtime_sections": sorted(RUNTIME_SECTIONS),
            "target_keys": list(TARGET_KEYS),
        },
        "context_targets": {
            "runtime_runner_alias_normalized": runtime_target,
            "task": task_target,
            "message_reply": message_target,
        },
        "command_descriptor": command,
        "workbench_event_payload": event_payload,
        "message_arming_outcomes": list(MESSAGE_ARMING_OUTCOMES),
        "task_detail_sideboard_context": {
            "project": {"id": project_id, "name": "Operator Workbench", "repo": "owner/repo"},
            "purpose": "Keep commands and targets contract-driven.",
            "strategic": {
                "direction": {
                    "id": "direction-main",
                    "text": "Keep operator commands aligned.",
                    "priority": "high",
                    "status": "active",
                },
                "cycle": None,
            },
            "pipeline": {
                "parent_task_id": None,
                "child_count": 0,
                "dependency_count": 0,
                "blocked_reason": None,
                "movement_explanation": None,
            },
            "artifacts": {
                "total": 0,
                "by_type": {},
                "primary_pr_url": None,
                "primary_commit": None,
            },
            "messages": {"total_recent": 0, "items": []},
            "links": [
                {
                    "id": "task",
                    "label": "Open task",
                    "target": task_target,
                }
            ],
        },
        "strategic_state_response": strategic_state_response,
        "strategic_session_response": strategic_session_response,
    }
