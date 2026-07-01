"""Read-model command descriptors for the Strategic work session.

Commands are presentation descriptors over existing work-session state. They do
not execute anything themselves and intentionally point only at existing admin
endpoints.
"""

from __future__ import annotations

from typing import Any


MAX_WORK_SESSION_COMMANDS = 24
_STRATEGIC_SESSION_SURFACE = "strategic_session"
_RUNTIME_SURFACE = "runtime"
_COMMAND_TARGET_KEYS = (
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
    "runner_id",
)


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


def _compact_target(target: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    compact: dict[str, Any] = {}
    for key in _COMMAND_TARGET_KEYS:
        value = _compact_text(target.get(key), max_chars=160)
        if value:
            compact[key] = value
    return compact or None


def _bounded_surfaces(values: list[str] | tuple[str, ...] | None) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        surface = _compact_text(raw, max_chars=80)
        if not surface or surface in seen:
            continue
        surfaces.append(surface)
        seen.add(surface)
        if len(surfaces) >= 8:
            break
    return surfaces


def _mutation(
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "method": method,
        "path": path,
        "body": body or {},
    }


def _command(
    command_id: str,
    label: str,
    *,
    group: str,
    priority: str,
    enabled: bool,
    target: dict[str, Any] | None,
    refetch_surfaces: list[str] | tuple[str, ...] | None = None,
    mutation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    compact_id = _compact_text(command_id, max_chars=180)
    compact_label = _compact_text(label, max_chars=96)
    compact_target = _compact_target(target)
    if not compact_id or not compact_label or not compact_target:
        return None
    command: dict[str, Any] = {
        "id": compact_id,
        "label": compact_label,
        "group": _compact_text(group, max_chars=40) or "session",
        "priority": _compact_text(priority, max_chars=40) or "normal",
        "enabled": bool(enabled),
        "target": compact_target,
        "refetch_surfaces": _bounded_surfaces(refetch_surfaces),
    }
    if mutation:
        command["mutation"] = mutation
    return command


def _strategic_session_endpoint(project_id: Any) -> str:
    return f"/api/admin/projects/{project_id}/strategic-session"


def _mode_command(
    *,
    project_id: str,
    current_mode: str,
    mode: str,
    label: str,
    refetch_surfaces: tuple[str, ...] = (_STRATEGIC_SESSION_SURFACE,),
) -> dict[str, Any] | None:
    return _command(
        f"session.mode.{mode}",
        label,
        group="mode",
        priority="normal",
        enabled=current_mode != mode,
        target={
            "surface": "messages",
            "project_id": project_id,
            "action": f"set_{mode}_mode",
        },
        refetch_surfaces=refetch_surfaces,
        mutation=_mutation(
            method="POST",
            path=_strategic_session_endpoint(project_id),
            body={"workbench": {"interaction_mode": mode}},
        ),
    )


def _context_canvas_command(
    *,
    project_id: str,
    context_package: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(context_package, dict):
        return None
    canvas = context_package.get("canvas")
    if not isinstance(canvas, dict):
        return None
    canvas_id = _compact_text(canvas.get("id"), max_chars=160)
    expected_id = f"strategic-canvas:{project_id}"
    if canvas_id != expected_id:
        return None

    label = _compact_text(canvas.get("title"), max_chars=80) or "Open context canvas"
    target: dict[str, Any] | None = None
    resources = context_package.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            if resource.get("kind") == "canvas" and resource.get("id") == canvas_id:
                target = resource.get("target")
                break
    if not isinstance(target, dict):
        target = {"surface": "project", "section": "problem-model"}
    return _command(
        "context.canvas.open",
        label,
        group="context",
        priority="low",
        enabled=True,
        target={**target, "project_id": project_id},
    )


def _fixed_session_commands(
    *,
    project_id: str,
    workbench_state: dict[str, Any] | None,
    context_package: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    state = workbench_state if isinstance(workbench_state, dict) else {}
    current_mode = str(state.get("interaction_mode") or "chat")
    selected_refs = state.get("selected_refs")
    has_selected_refs = isinstance(selected_refs, list) and len(selected_refs) > 0

    candidates = [
        _command(
            "session.check_in",
            "Check in",
            group="session",
            priority="normal",
            enabled=True,
            target={
                "surface": "messages",
                "project_id": project_id,
                "action": "check_in",
            },
            refetch_surfaces=(_STRATEGIC_SESSION_SURFACE,),
        ),
        _mode_command(
            project_id=project_id,
            current_mode=current_mode,
            mode="continuous",
            label="Workday mode",
            refetch_surfaces=(_STRATEGIC_SESSION_SURFACE, _RUNTIME_SURFACE),
        ),
        _mode_command(
            project_id=project_id,
            current_mode=current_mode,
            mode="monitor",
            label="Monitor mode",
        ),
        _mode_command(
            project_id=project_id,
            current_mode=current_mode,
            mode="chat",
            label="Chat mode",
        ),
        _command(
            "session.clear_selected_refs",
            "Clear selected refs",
            group="context",
            priority="normal",
            enabled=has_selected_refs,
            target={
                "surface": "project",
                "project_id": project_id,
                "section": "selected_refs",
                "action": "clear",
            },
            refetch_surfaces=(_STRATEGIC_SESSION_SURFACE,),
            mutation=_mutation(
                method="POST",
                path=_strategic_session_endpoint(project_id),
                body={"workbench": {"selected_refs": []}},
            ),
        ),
        _context_canvas_command(
            project_id=project_id,
            context_package=context_package,
        ),
    ]
    return [command for command in candidates if command]


def _action_command(action: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    action_id = _compact_text(action.get("id"), max_chars=180)
    label = _compact_text(action.get("label"), max_chars=96)
    target = _compact_target(action.get("target"))
    if not action_id or not label or not target:
        return None
    return _command(
        action_id,
        label,
        group=_compact_text(action.get("source"), max_chars=40) or "navigation",
        priority=_compact_text(action.get("priority"), max_chars=40) or "normal",
        enabled=bool(action.get("enabled", True)),
        target=target,
    )


def build_work_session_commands(
    *,
    project_id: Any,
    actions: list[dict[str, Any]] | None,
    workbench_state: dict[str, Any] | None,
    context_package: dict[str, Any] | None,
    limit: int = MAX_WORK_SESSION_COMMANDS,
) -> list[dict[str, Any]]:
    """Build bounded work-session command descriptors from read-model inputs."""

    project_id_text = _compact_text(project_id, max_chars=160)
    if not project_id_text:
        return []

    max_items = min(max(int(limit or 0), 0), MAX_WORK_SESSION_COMMANDS)
    if max_items == 0:
        return []

    candidates = [
        *_fixed_session_commands(
            project_id=project_id_text,
            workbench_state=workbench_state,
            context_package=context_package,
        ),
        *[
            command
            for command in (
                _action_command(action)
                for action in (actions if isinstance(actions, list) else [])
            )
            if command
        ],
    ]

    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for command in candidates:
        command_id = str(command.get("id") or "")
        if not command_id or command_id in seen:
            continue
        seen.add(command_id)
        commands.append(command)
        if len(commands) >= max_items:
            break
    return commands
