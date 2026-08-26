"""Self-describing, read-only access contract for external coding agents."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.agent_access_redaction import is_secret_field, redact_agent_text
from app.auth import get_current_user
from app.admin import (
    _get_user_project,
    get_all_tasks as get_admin_tasks,
    get_task as get_admin_task,
)
from app.database import get_session
from app.models import Project, ProjectMember, ServiceCredential, Task, TaskStatus
from app.operator_attention import build_operator_attention
from app.routers.admin_debug import get_project_diagnostics
from app.routers.admin_notifications import (
    get_message_counts,
    list_messages as get_admin_messages,
)
from app.routers.admin_project_config import get_project_resource_summary
from app.schemas import redact_command_log_text
from app.time_utils import utc_iso


router = APIRouter(prefix="/api/agent-access/v1", tags=["agent-access"])


_CAPABILITIES_BY_SCOPE = {
    "observer:read": [
        "projects.list",
        "project.attention",
        "project.diagnostics",
        "project.resources",
    ],
    "coding-agent:read": [
        "projects.list",
        "project.snapshot",
        "project.attention",
        "project.diagnostics",
        "project.resources",
        "tasks.list",
        "tasks.read",
        "messages.list",
        "directions.list",
    ],
    "project-operator": [
        "projects.list",
        "project.snapshot",
        "project.attention",
        "project.diagnostics",
        "project.resources",
        "tasks.list",
        "tasks.read",
        "tasks.create",
        "tasks.requeue",
        "tasks.cancel",
        "tasks.request_changes",
        "messages.list",
        "messages.send",
        "messages.answer",
        "directions.list",
        "directions.create",
        "directions.update",
        "directions.activate",
        "directions.retarget",
        "directions.cancel",
        "strategic.start",
        "strategic.continue",
        "strategic.cycle",
    ],
}


# The admin task-detail route is deliberately rich and UI-shaped. Coding-agent
# credentials receive a smaller projection with an explicit response budget so a
# single pathological task cannot flood an MCP context window. The final
# response-size guard is a backstop; the section budgets normally keep the body
# comfortably below it while preserving recent diagnostic evidence.
_AGENT_TASK_RESPONSE_MAX_BYTES = 256_000
_AGENT_TASK_PAYLOAD_LIMIT = 4
_AGENT_TASK_STATUS_LOG_LIMIT = 50
_AGENT_TASK_HISTORY_LIMIT = 50
_AGENT_TASK_ARTIFACT_LIMIT = 50
_AGENT_TASK_CHILD_LIMIT = 100
_AGENT_TASK_NESTED_DEPTH_LIMIT = 6
_AGENT_TASK_NESTED_COLLECTION_LIMIT = 50
_AGENT_TASK_KEY_MAX_BYTES = 96
_AGENT_TASK_STRING_MAX_BYTES = 8_000
_AGENT_LIST_RESPONSE_MAX_BYTES = 256_000
_AGENT_LIST_ITEM_LIMIT = 200
_AGENT_LIST_ITEM_MAX_BYTES = 16_000
_AGENT_SNAPSHOT_RESPONSE_MAX_BYTES = 256_000
_AGENT_ACCESS_PROJECT_LIMIT = 50

_AGENT_SNAPSHOT_SECTION_BUDGETS = {
    "project": 8_000,
    "attention": 36_000,
    "diagnostics": 96_000,
    "resources": 40_000,
    "message_counts": 8_000,
    "server": 2_000,
}

_AGENT_TASK_SECTION_BUDGETS = {
    "task": 24_000,
    "payloads": 24_000,
    "plan_output": 24_000,
    "status": 32_000,
    "history": 32_000,
    "artifacts": 24_000,
    "children": 8_000,
    "failure_evidence": 40_000,
    "task_list_item": _AGENT_LIST_ITEM_MAX_BYTES,
    "message_list_item": _AGENT_LIST_ITEM_MAX_BYTES,
    "list_project": 4_000,
    "list_filters": 4_000,
    **{
        f"snapshot_{section}": budget
        for section, budget in _AGENT_SNAPSHOT_SECTION_BUDGETS.items()
    },
}

def _new_projection_state(byte_budget: int) -> dict[str, int | bool]:
    return {
        "bytes_left": byte_budget,
        "nodes_left": 750,
        "truncated": False,
        "redactions": 0,
    }


def _clip_utf8(
    value: str,
    *,
    max_bytes: int,
    tail: bool,
) -> tuple[str, bool]:
    """Clip *value* to a valid UTF-8 prefix/tail within *max_bytes*."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    if max_bytes <= 0:
        return "", True
    marker = "[truncated]\n" if tail else "\n[truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore"), True
    content_bytes = max_bytes - len(marker_bytes)
    if tail:
        clipped = encoded[-content_bytes:].decode("utf-8", errors="ignore")
        return marker + clipped, True
    clipped = encoded[:content_bytes].decode("utf-8", errors="ignore")
    return clipped + marker, True


def _serialized_json_string_size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _clip_serialized_json_string(
    value: str,
    *,
    max_bytes: int,
    tail: bool,
) -> tuple[str, bool]:
    """Clip a string by its encoded JSON representation, including escaping."""
    if _serialized_json_string_size(value) <= max_bytes:
        return value, False
    if max_bytes <= 2:
        return "", True
    marker = "[truncated]\n" if tail else "\n[truncated]"
    if _serialized_json_string_size(marker) > max_bytes:
        return "", True

    low = 0
    high = len(value)
    while low < high:
        midpoint = (low + high + 1) // 2
        content = value[-midpoint:] if tail else value[:midpoint]
        candidate = marker + content if tail else content + marker
        if _serialized_json_string_size(candidate) <= max_bytes:
            low = midpoint
        else:
            high = midpoint - 1
    content = value[-low:] if tail and low else value[:low]
    return (marker + content if tail else content + marker), True


def _bounded_agent_value(
    value: Any,
    state: dict[str, int | bool],
    *,
    depth: int = 0,
    tail_strings: bool = False,
    serialized_strings: bool = False,
) -> Any:
    """Recursively redact and bound one JSON-compatible task-detail value."""
    nodes_left = int(state["nodes_left"])
    if nodes_left <= 0:
        state["truncated"] = True
        return None
    state["nodes_left"] = nodes_left - 1

    if isinstance(value, dict):
        if depth >= _AGENT_TASK_NESTED_DEPTH_LIMIT:
            state["truncated"] = True
            return {}
        result: dict[str, Any] = {}
        entries = list(value.items())
        if len(entries) > _AGENT_TASK_NESTED_COLLECTION_LIMIT:
            state["truncated"] = True
        for raw_key, nested in entries[:_AGENT_TASK_NESTED_COLLECTION_LIMIT]:
            if is_secret_field(raw_key):
                state["redactions"] = int(state["redactions"]) + 1
                continue
            key_clip = (
                _clip_serialized_json_string if serialized_strings else _clip_utf8
            )
            key, key_truncated = key_clip(
                str(raw_key),
                max_bytes=min(
                    _AGENT_TASK_KEY_MAX_BYTES,
                    max(0, int(state["bytes_left"])),
                ),
                tail=False,
            )
            if key_truncated:
                state["truncated"] = True
            key_size = (
                _serialized_json_string_size(key)
                if serialized_strings
                else len(key.encode("utf-8"))
            )
            state["bytes_left"] = max(0, int(state["bytes_left"]) - key_size)
            if not key or int(state["bytes_left"]) <= 0:
                state["truncated"] = True
                break
            result[key] = _bounded_agent_value(
                nested,
                state,
                depth=depth + 1,
                tail_strings=tail_strings,
                serialized_strings=serialized_strings,
            )
            if int(state["bytes_left"]) <= 0 or int(state["nodes_left"]) <= 0:
                state["truncated"] = True
                break
        return result

    if isinstance(value, (list, tuple)):
        if depth >= _AGENT_TASK_NESTED_DEPTH_LIMIT:
            state["truncated"] = True
            return []
        if len(value) > _AGENT_TASK_NESTED_COLLECTION_LIMIT:
            state["truncated"] = True
        result = []
        for nested in value[:_AGENT_TASK_NESTED_COLLECTION_LIMIT]:
            if int(state["bytes_left"]) <= 0 or int(state["nodes_left"]) <= 0:
                state["truncated"] = True
                break
            result.append(
                _bounded_agent_value(
                    nested,
                    state,
                    depth=depth + 1,
                    tail_strings=tail_strings,
                    serialized_strings=serialized_strings,
                )
            )
        return result

    if value is None or isinstance(value, (bool, int, float)):
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        if len(encoded) > int(state["bytes_left"]):
            state["truncated"] = True
            return None
        state["bytes_left"] = int(state["bytes_left"]) - len(encoded)
        return value

    raw_text = value.value if hasattr(value, "value") else str(value)
    sanitized = redact_command_log_text(redact_agent_text(str(raw_text)))
    if sanitized != str(raw_text):
        state["redactions"] = int(state["redactions"]) + 1
    max_bytes = min(
        _AGENT_TASK_STRING_MAX_BYTES,
        max(0, int(state["bytes_left"])),
    )
    string_clip = (
        _clip_serialized_json_string if serialized_strings else _clip_utf8
    )
    clipped, was_truncated = string_clip(
        sanitized,
        max_bytes=max_bytes,
        tail=tail_strings,
    )
    if was_truncated:
        state["truncated"] = True
    clipped_size = (
        _serialized_json_string_size(clipped)
        if serialized_strings
        else len(clipped.encode("utf-8"))
    )
    state["bytes_left"] = max(0, int(state["bytes_left"]) - clipped_size)
    return clipped


def _bounded_agent_section(
    value: Any,
    section: str,
    *,
    tail_strings: bool = False,
    serialized_strings: bool = False,
) -> tuple[Any, bool, int]:
    state = _new_projection_state(_AGENT_TASK_SECTION_BUDGETS[section])
    projected = _bounded_agent_value(
        value,
        state,
        tail_strings=tail_strings,
        serialized_strings=serialized_strings,
    )
    return projected, bool(state["truncated"]), int(state["redactions"])


def _agent_task_response_size(response: dict[str, Any]) -> int:
    return len(
        json.dumps(response, separators=(",", ":"), default=str, ensure_ascii=False).encode(
            "utf-8"
        )
    )


def _enforce_agent_task_response_size(response: dict[str, Any]) -> None:
    """Fail-safe the aggregate body below the advertised hard byte limit."""
    reducers = [
        ("plan_output", lambda: response.__setitem__("plan_output", None)),
        ("payloads", lambda: response.__setitem__("payloads", [])),
        ("status_log", lambda: response["status"].__setitem__("log", [])),
        ("history", lambda: response.__setitem__("history", [])),
        ("artifacts", lambda: response.__setitem__("artifacts", [])),
        (
            "failure_evidence",
            lambda: response.__setitem__(
                "failure_evidence",
                {
                    "blocker_kind": response["failure_evidence"].get("blocker_kind"),
                    "blocked_reason": response["failure_evidence"].get("blocked_reason"),
                },
            ),
        ),
    ]
    for flag, reducer in reducers:
        if _agent_task_response_size(response) <= _AGENT_TASK_RESPONSE_MAX_BYTES:
            break
        reducer()
        response["truncation"][flag] = True

    if _agent_task_response_size(response) > _AGENT_TASK_RESPONSE_MAX_BYTES:
        # Section budgets count the source UTF-8 bytes, but JSON escaping can
        # expand control characters by up to six times (``\u0001``). Preserve a
        # small identity core and replace every diagnostic section so the
        # advertised aggregate ceiling remains literal even for adversarial or
        # corrupt legacy task text.
        original_task = response.get("task")
        original_task = original_task if isinstance(original_task, dict) else {}
        identity_state = _new_projection_state(2_048)
        minimal_task = _bounded_agent_value(
            {
                key: original_task.get(key)
                for key in ("id", "project_id", "status", "task_type")
            },
            identity_state,
        )
        response["task"] = minimal_task
        response["payloads"] = []
        response["plan_output"] = None
        response["status"] = {
            "latest_message": None,
            "log": [],
            "returned": 0,
            "available_in_source": None,
            "selection": "omitted_for_response_budget",
        }
        response["history"] = []
        response["artifacts"] = []
        response["children"] = {
            "ids": [],
            "returned": 0,
            "available_in_source": None,
            "source_truncated": True,
            "selection": "omitted_for_response_budget",
        }
        response["failure_evidence"] = {}
        response["truncation"] = {
            **dict.fromkeys(response["truncation"], True),
            "response_bytes": True,
        }
        response["redaction"]["fields_or_values"] += int(
            identity_state["redactions"]
        )

    if _agent_task_response_size(response) > _AGENT_TASK_RESPONSE_MAX_BYTES:
        # The bounded identity above is already far below the limit. Keep a
        # static last resort so future schema growth cannot accidentally weaken
        # the hard cap without making a test fail.
        response.clear()
        response.update(
            {
                "schema_version": "blueprint.agent-task.v1",
                "complete": False,
                "limits": {"response_bytes": _AGENT_TASK_RESPONSE_MAX_BYTES},
                "truncation": {"response_bytes": True},
                "redaction": {
                    "applied": True,
                    "policy": "response omitted to enforce the hard byte budget",
                },
                "task": {},
                "payloads": [],
                "plan_output": None,
                "status": {"latest_message": None, "log": []},
                "history": [],
                "artifacts": [],
                "children": {"ids": []},
                "failure_evidence": {},
            }
        )
    response["complete"] = not any(response["truncation"].values())


def _project_agent_task_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Project the canonical admin detail into a bounded coding-agent contract."""
    redactions = 0
    truncation: dict[str, bool] = {}

    task_fields = {
        key: detail.get(key)
        for key in (
            "id",
            "project_id",
            "status",
            "task_type",
            "attempt_count",
            "attempt_number",
            "title",
            "description",
            "requires_approval",
            "parent_task_id",
            "depends_on",
            "created_at",
            "started_at",
            "last_heartbeat",
            "blocked_at",
            "recheck_after",
            "parked_at",
            "pr_url",
            "commit_hash",
            "repo_url",
            "post_commit_pending",
        )
    }
    task, truncation["task"], section_redactions = _bounded_agent_section(
        task_fields, "task"
    )
    redactions += section_redactions

    raw_payloads = detail.get("payloads")
    raw_payloads = raw_payloads if isinstance(raw_payloads, list) else []
    payloads, nested_truncated, section_redactions = _bounded_agent_section(
        raw_payloads[:_AGENT_TASK_PAYLOAD_LIMIT], "payloads"
    )
    truncation["payloads"] = (
        len(raw_payloads) > _AGENT_TASK_PAYLOAD_LIMIT or nested_truncated
    )
    redactions += section_redactions

    plan_output, truncation["plan_output"], section_redactions = (
        _bounded_agent_section(detail.get("plan_output"), "plan_output")
    )
    redactions += section_redactions

    raw_status_log = detail.get("status_log")
    raw_status_log = raw_status_log if isinstance(raw_status_log, list) else []
    selected_status_log = raw_status_log[-_AGENT_TASK_STATUS_LOG_LIMIT:]
    status, nested_truncated, section_redactions = _bounded_agent_section(
        {
            "latest_message": detail.get("latest_status_message"),
            "log": selected_status_log,
            "returned": len(selected_status_log),
            "available_in_source": len(raw_status_log),
            "selection": "most_recent",
        },
        "status",
    )
    truncation["status_log"] = (
        len(raw_status_log) > _AGENT_TASK_STATUS_LOG_LIMIT or nested_truncated
    )
    redactions += section_redactions

    raw_history = detail.get("history")
    raw_history = raw_history if isinstance(raw_history, list) else []
    selected_history = raw_history[:_AGENT_TASK_HISTORY_LIMIT]
    history, nested_truncated, section_redactions = _bounded_agent_section(
        selected_history, "history"
    )
    truncation["history"] = (
        len(raw_history) > _AGENT_TASK_HISTORY_LIMIT or nested_truncated
    )
    redactions += section_redactions

    raw_artifacts = detail.get("artifacts")
    raw_artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    selected_artifacts = raw_artifacts[-_AGENT_TASK_ARTIFACT_LIMIT:]
    artifact_fields = (
        "artifact_type",
        "pr_url",
        "commit_hash",
        "repo_url",
        "iteration",
        "created_at",
    )
    artifact_projection = [
        {key: artifact.get(key) for key in artifact_fields}
        for artifact in selected_artifacts
        if isinstance(artifact, dict)
    ]
    artifacts, nested_truncated, section_redactions = _bounded_agent_section(
        artifact_projection, "artifacts"
    )
    truncation["artifacts"] = (
        len(raw_artifacts) > _AGENT_TASK_ARTIFACT_LIMIT or nested_truncated
    )
    redactions += section_redactions

    raw_children = detail.get("child_task_ids")
    raw_children = raw_children if isinstance(raw_children, list) else []
    selected_children = raw_children[:_AGENT_TASK_CHILD_LIMIT]
    children, nested_truncated, section_redactions = _bounded_agent_section(
        {
            "ids": selected_children,
            "returned": len(selected_children),
            "available_in_source": len(raw_children),
            "source_truncated": bool(detail.get("child_task_ids_truncated")),
            "selection": "first",
        },
        "children",
    )
    truncation["children"] = (
        len(raw_children) > _AGENT_TASK_CHILD_LIMIT
        or bool(detail.get("child_task_ids_truncated"))
        or nested_truncated
    )
    redactions += section_redactions

    failure_evidence, truncation["failure_evidence"], section_redactions = (
        _bounded_agent_section(
            {
                "blocker_kind": detail.get("blocker_kind"),
                "blocked_reason": detail.get("blocked_reason"),
                "park_reason": detail.get("park_reason"),
                "movement_explanation": detail.get("movement_explanation"),
                "error_log_tail": detail.get("error_logs"),
                "evaluation_summary": detail.get("evaluation_summary"),
            },
            "failure_evidence",
            tail_strings=True,
        )
    )
    redactions += section_redactions

    response = {
        "schema_version": "blueprint.agent-task.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not any(truncation.values()),
        "limits": {
            "response_bytes": _AGENT_TASK_RESPONSE_MAX_BYTES,
            "payload_entries": _AGENT_TASK_PAYLOAD_LIMIT,
            "status_log_entries": _AGENT_TASK_STATUS_LOG_LIMIT,
            "history_entries": _AGENT_TASK_HISTORY_LIMIT,
            "artifact_entries": _AGENT_TASK_ARTIFACT_LIMIT,
            "child_task_ids": _AGENT_TASK_CHILD_LIMIT,
            "nested_depth": _AGENT_TASK_NESTED_DEPTH_LIMIT,
            "nested_collection_entries": _AGENT_TASK_NESTED_COLLECTION_LIMIT,
            "string_bytes": _AGENT_TASK_STRING_MAX_BYTES,
            "status_selection": "most_recent",
            "history_selection": "most_recent",
            "artifact_selection": "most_recent",
            "failure_log_selection": "tail",
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like string values are removed",
        },
        "task": task,
        "payloads": payloads,
        "plan_output": plan_output,
        "status": status,
        "history": history,
        "artifacts": artifacts,
        "children": children,
        "failure_evidence": failure_evidence,
        "server": {"revision": os.getenv("K_REVISION", "local-dev")},
    }
    _enforce_agent_task_response_size(response)
    return response


def _agent_project_field(project: Project | dict[str, Any], key: str) -> Any:
    if isinstance(project, dict):
        return project.get(key)
    return getattr(project, key, None)


def _project_agent_list_project(
    project: Project | dict[str, Any],
) -> tuple[dict[str, Any], bool, int]:
    raw = {
        "id": str(_agent_project_field(project, "id")),
        "name": _agent_project_field(project, "name"),
        "github_owner": _agent_project_field(project, "github_owner"),
        "github_repo": _agent_project_field(project, "github_repo"),
    }
    return _bounded_agent_section(
        raw,
        "list_project",
        serialized_strings=True,
    )


def _project_agent_list_items(
    raw_items: Any,
    *,
    fields: tuple[str, ...],
    section: str,
    limit: int,
) -> tuple[list[dict[str, Any]], bool, bool, int, int]:
    source = raw_items if isinstance(raw_items, list) else []
    selected = source[: min(limit, _AGENT_LIST_ITEM_LIMIT)]
    projected: list[dict[str, Any]] = []
    source_truncated = len(source) > len(selected)
    fields_truncated = False
    redactions = 0
    for item in selected:
        if not isinstance(item, dict):
            fields_truncated = True
            continue
        value, item_truncated, item_redactions = _bounded_agent_section(
            {key: item.get(key) for key in fields},
            section,
            serialized_strings=True,
        )
        if isinstance(value, dict):
            projected.append(value)
        else:
            fields_truncated = True
        fields_truncated = fields_truncated or item_truncated
        redactions += item_redactions
    return (
        projected,
        source_truncated,
        fields_truncated,
        redactions,
        len(source),
    )


def _enforce_agent_list_response_size(
    response: dict[str, Any], *, item_key: str
) -> None:
    """Enforce the literal aggregate byte ceiling, including JSON escaping."""
    if _agent_task_response_size(response) <= _AGENT_LIST_RESPONSE_MAX_BYTES:
        response["complete"] = not any(response["truncation"].values())
        _finalize_agent_list_pagination(response)
        return

    original_items = response.get(item_key)
    original_items = original_items if isinstance(original_items, list) else []
    response["truncation"]["response_bytes"] = True
    response["truncation"]["items"] = True
    response["has_more"] = True
    response["complete"] = False

    low = 0
    high = len(original_items)
    while low < high:
        midpoint = (low + high + 1) // 2
        response[item_key] = original_items[:midpoint]
        response["returned"] = midpoint
        if _agent_task_response_size(response) <= _AGENT_LIST_RESPONSE_MAX_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    response[item_key] = original_items[:low]
    response["returned"] = low

    if _agent_task_response_size(response) > _AGENT_LIST_RESPONSE_MAX_BYTES:
        schema_version = response.get("schema_version")
        project = response.get("project")
        limits = response.get("limits")
        total = response.get("total")
        limit = response.get("limit")
        offset = response.get("offset")
        source_returned = response.get("source_returned")
        paged = "next_offset" in response
        response.clear()
        response.update(
            {
                "schema_version": schema_version,
                "complete": False,
                "project": project,
                "limits": limits,
                "truncation": {
                    "source_page": True,
                    "items": True,
                    "item_fields": True,
                    "response_bytes": True,
                },
                "redaction": {
                    "applied": True,
                    "policy": "list items omitted to enforce the hard byte budget",
                },
                item_key: [],
                "total": total,
                "limit": limit,
                "offset": offset,
                "returned": 0,
                "source_returned": source_returned,
                "has_more": True,
            }
        )
        if paged:
            response["next_offset"] = None
    response["complete"] = not any(response["truncation"].values())
    _finalize_agent_list_pagination(response)


def _finalize_agent_list_pagination(response: dict[str, Any]) -> None:
    """Derive page continuation from rows actually returned to avoid skips."""
    total = response.get("total")
    offset = response.get("offset")
    returned = response.get("returned")
    if not all(isinstance(value, int) for value in (total, offset, returned)):
        return
    has_more = offset + returned < total
    response["has_more"] = has_more
    if "next_offset" in response:
        response["next_offset"] = offset + returned if has_more and returned else None


def _project_agent_task_list(
    raw_response: dict[str, Any],
    project: Project | dict[str, Any],
    *,
    limit: int,
    offset: int,
    statuses: list[TaskStatus],
    task_type: str | None,
    search: str | None,
    id_prefix: str | None,
    direction_id: str | None,
    approval_needed: bool | None,
) -> dict[str, Any]:
    item_fields = (
        "id",
        "project_id",
        "status",
        "task_type",
        "attempt_count",
        "attempt_number",
        "title",
        "description",
        "latest_status_message",
        "requires_approval",
        "parent_task_id",
        "depends_on",
        "created_at",
        # Freshness. Without it a caller cannot separate a task that moved
        # seconds ago from one frozen for hours -- the first question asked
        # when diagnosing a wedge from this projection.
        "updated_at",
        "started_at",
        "last_heartbeat",
        "blocker_kind",
        "blocked_reason",
        "blocked_at",
        "recheck_after",
        "parked_at",
        "park_reason",
        "archived",
        "payloads",
        "pr_url",
        "commit_hash",
        "repo_url",
        "failure_class",
        "post_commit_pending",
    )
    tasks, items_truncated, item_fields_truncated, redactions, source_returned = (
        _project_agent_list_items(
            raw_response.get("tasks"),
            fields=item_fields,
            section="task_list_item",
            limit=limit,
        )
    )
    project_projection, project_truncated, project_redactions = (
        _project_agent_list_project(project)
    )
    filters, filters_truncated, filter_redactions = _bounded_agent_section(
        {
            "status": [status.value for status in statuses],
            "task_type": task_type,
            "search": search,
            "id_prefix": id_prefix,
            "direction_id": direction_id,
            "approval_needed": approval_needed,
        },
        "list_filters",
        serialized_strings=True,
    )
    redactions += project_redactions + filter_redactions
    total = int(raw_response.get("total") or 0)
    page_incomplete = total > offset + source_returned
    items_incomplete = items_truncated or source_returned > len(tasks)
    truncation = {
        "source_page": page_incomplete,
        "items": items_incomplete,
        "item_fields": item_fields_truncated
        or project_truncated
        or filters_truncated,
        "response_bytes": False,
    }
    response = {
        "schema_version": "blueprint.agent-tasks.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not any(truncation.values()),
        "project": project_projection,
        "filters": filters,
        "limits": {
            "response_bytes": _AGENT_LIST_RESPONSE_MAX_BYTES,
            "maximum_limit": _AGENT_LIST_ITEM_LIMIT,
            "requested_limit": limit,
            "item_value_budget_bytes": _AGENT_LIST_ITEM_MAX_BYTES,
            "string_serialized_bytes": _AGENT_TASK_STRING_MAX_BYTES,
            "nested_depth": _AGENT_TASK_NESTED_DEPTH_LIMIT,
            "nested_collection_entries": _AGENT_TASK_NESTED_COLLECTION_LIMIT,
            "selection": "most_recent",
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like string values are removed",
        },
        "tasks": tasks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(tasks),
        "source_returned": source_returned,
        "has_more": page_incomplete or items_incomplete,
        "next_offset": (
            offset + len(tasks) if page_incomplete or items_incomplete else None
        ),
        "counts": {
            "active": int(raw_response.get("scoped_active_count") or 0),
            "starting": int(raw_response.get("scoped_starting_count") or 0),
            "evaluating": int(raw_response.get("scoped_evaluating_count") or 0),
            "queued": int(raw_response.get("scoped_queued_count") or 0),
        },
        "server": {"revision": os.getenv("K_REVISION", "local-dev")},
    }
    _enforce_agent_list_response_size(response, item_key="tasks")
    return response


def _project_agent_message_list(
    raw_response: dict[str, Any],
    project: Project | dict[str, Any],
    *,
    limit: int,
    offset: int,
    message_status: str | None,
    query: str | None,
) -> dict[str, Any]:
    item_fields = (
        "id",
        "project_id",
        "message_type",
        "from_type",
        "from_task_id",
        "actor_user_id",
        "sequence",
        "surface",
        "handling_state",
        "handled_by_task_id",
        "reply_to_message_id",
        "content",
        "context",
        "status",
        "resolution",
        "resolved_at",
        "delivered_at",
        "created_at",
        "updated_at",
    )
    messages, items_truncated, item_fields_truncated, redactions, source_returned = (
        _project_agent_list_items(
            raw_response.get("messages"),
            fields=item_fields,
            section="message_list_item",
            limit=limit,
        )
    )
    project_projection, project_truncated, project_redactions = (
        _project_agent_list_project(project)
    )
    filters, filters_truncated, filter_redactions = _bounded_agent_section(
        {"status": message_status, "q": query},
        "list_filters",
        serialized_strings=True,
    )
    redactions += project_redactions + filter_redactions
    total = int(raw_response.get("total") or 0)
    page_incomplete = total > offset + source_returned
    items_incomplete = items_truncated or source_returned > len(messages)
    truncation = {
        "source_page": page_incomplete,
        "items": items_incomplete,
        "item_fields": item_fields_truncated
        or project_truncated
        or filters_truncated,
        "response_bytes": False,
    }
    response = {
        "schema_version": "blueprint.agent-messages.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not any(truncation.values()),
        "project": project_projection,
        "filters": filters,
        "limits": {
            "response_bytes": _AGENT_LIST_RESPONSE_MAX_BYTES,
            "maximum_limit": _AGENT_LIST_ITEM_LIMIT,
            "requested_limit": limit,
            "item_value_budget_bytes": _AGENT_LIST_ITEM_MAX_BYTES,
            "string_serialized_bytes": _AGENT_TASK_STRING_MAX_BYTES,
            "nested_depth": _AGENT_TASK_NESTED_DEPTH_LIMIT,
            "nested_collection_entries": _AGENT_TASK_NESTED_COLLECTION_LIMIT,
            "selection": "operator-visible most recent",
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like string values are removed",
        },
        "messages": messages,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(messages),
        "source_returned": source_returned,
        "has_more": total > offset + len(messages),
        "next_offset": (
            offset + len(messages) if total > offset + len(messages) else None
        ),
        "server": {"revision": os.getenv("K_REVISION", "local-dev")},
    }
    _enforce_agent_list_response_size(response, item_key="messages")
    return response


def _project_agent_snapshot(
    project: Project | dict[str, Any],
    *,
    attention: Any,
    diagnostics: Any,
    resources: Any,
    message_counts: Any,
) -> dict[str, Any]:
    """Project canonical orientation reads into one bounded external contract."""
    raw_sections = {
        "project": {
            "id": str(_agent_project_field(project, "id")),
            "name": _agent_project_field(project, "name"),
            "github_owner": _agent_project_field(project, "github_owner"),
            "github_repo": _agent_project_field(project, "github_repo"),
        },
        "attention": attention,
        "diagnostics": diagnostics,
        "resources": resources,
        "message_counts": message_counts,
        "server": {"revision": os.getenv("K_REVISION", "local-dev")},
    }
    projected: dict[str, Any] = {}
    truncation: dict[str, bool] = {}
    redactions = 0
    for section, value in raw_sections.items():
        bounded, section_truncated, section_redactions = _bounded_agent_section(
            value,
            f"snapshot_{section}",
            serialized_strings=True,
        )
        projected[section] = bounded
        truncation[section] = section_truncated
        redactions += section_redactions
    truncation["response_bytes"] = False

    response = {
        "schema_version": "blueprint.project-snapshot.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not any(truncation.values()),
        "limits": {
            "response_bytes": _AGENT_SNAPSHOT_RESPONSE_MAX_BYTES,
            "section_value_budget_bytes": dict(_AGENT_SNAPSHOT_SECTION_BUDGETS),
            "string_serialized_bytes": _AGENT_TASK_STRING_MAX_BYTES,
            "nested_depth": _AGENT_TASK_NESTED_DEPTH_LIMIT,
            "nested_collection_entries": _AGENT_TASK_NESTED_COLLECTION_LIMIT,
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like string values are removed",
        },
        **projected,
        "read_state": {
            "tracked": False,
            "reason": "read-only service credentials do not own a last-seen cursor",
        },
    }
    _enforce_agent_snapshot_response_size(response)
    return response


def _enforce_agent_snapshot_response_size(response: dict[str, Any]) -> None:
    """Fail closed to a small snapshot if section budgets ever exceed the cap."""
    if _agent_task_response_size(response) <= _AGENT_SNAPSHOT_RESPONSE_MAX_BYTES:
        response["complete"] = not any(response["truncation"].values())
        return

    response["truncation"]["response_bytes"] = True
    for section in ("diagnostics", "attention", "resources", "message_counts"):
        response[section] = {}
        response["truncation"][section] = True
        if _agent_task_response_size(response) <= _AGENT_SNAPSHOT_RESPONSE_MAX_BYTES:
            break

    if _agent_task_response_size(response) > _AGENT_SNAPSHOT_RESPONSE_MAX_BYTES:
        project = response.get("project")
        project = project if isinstance(project, dict) else {}
        response.clear()
        response.update(
            {
                "schema_version": "blueprint.project-snapshot.v1",
                "complete": False,
                "project": project,
                "limits": {"response_bytes": _AGENT_SNAPSHOT_RESPONSE_MAX_BYTES},
                "truncation": {
                    "project": False,
                    "attention": True,
                    "diagnostics": True,
                    "resources": True,
                    "message_counts": True,
                    "server": True,
                    "response_bytes": True,
                },
                "redaction": {
                    "applied": True,
                    "policy": "response omitted to enforce the hard byte budget",
                },
                "attention": {},
                "diagnostics": {},
                "resources": {},
                "message_counts": {},
                "read_state": {
                    "tracked": False,
                    "reason": "read-only service credentials do not own a last-seen cursor",
                },
                "server": {},
            }
        )
    response["complete"] = not any(response["truncation"].values())


def _task_link_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def _filter_agent_task_links_to_project(
    detail: dict[str, Any], session: AsyncSession
) -> dict[str, Any]:
    """Drop related task IDs that do not belong to the authorized task project.

    The canonical admin detail already membership-checks the requested task, but
    its child lookup follows ``parent_task_id`` without an additional project
    predicate. Filtering the related IDs here keeps this external projection
    tenant-safe even if corrupt legacy data contains a cross-project relation.
    """
    project_id = _task_link_uuid(detail.get("project_id"))
    if project_id is None:
        return detail

    parent_id = _task_link_uuid(detail.get("parent_task_id"))
    dependencies = detail.get("depends_on")
    dependencies = dependencies if isinstance(dependencies, list) else []
    child_ids = detail.get("child_task_ids")
    child_ids = child_ids if isinstance(child_ids, list) else []
    candidates = {
        candidate
        for candidate in (
            parent_id,
            *(_task_link_uuid(value) for value in dependencies),
            *(_task_link_uuid(value) for value in child_ids),
        )
        if candidate is not None
    }
    if not candidates:
        return detail

    same_project_ids = set(
        (
            await session.execute(
                select(Task.id).where(
                    Task.project_id == project_id,
                    Task.id.in_(candidates),
                )
            )
        ).scalars()
    )
    filtered = dict(detail)
    filtered["parent_task_id"] = (
        str(parent_id) if parent_id in same_project_ids else None
    )
    filtered["depends_on"] = [
        str(candidate)
        for value in dependencies
        if (candidate := _task_link_uuid(value)) in same_project_ids
    ]
    filtered["child_task_ids"] = [
        str(candidate)
        for value in child_ids
        if (candidate := _task_link_uuid(value)) in same_project_ids
    ]
    return filtered


def _remove_actor_relative_read_state(
    attention: dict[str, Any], message_counts: dict[str, Any]
) -> None:
    """Remove read cursors that a read-only service principal cannot advance."""
    attention.pop("last_seen_at", None)
    counts = attention.get("counts")
    if isinstance(counts, dict):
        counts.pop("unread", None)
    items = attention.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item.pop("unread", None)
    message_counts.pop("unread", None)


@router.get("/me")
async def get_agent_access_profile(
    limit: int = Query(default=_AGENT_ACCESS_PROJECT_LIMIT, ge=1, le=_AGENT_ACCESS_PROJECT_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the effective identity, scopes, projects, and capabilities.

    This endpoint is intentionally callable only through an allowlisted service
    credential. It lets an MCP adapter prove its effective grants without
    receiving or reflecting the bearer token itself.
    """
    credential_id = current_user.get("service_credential_id")
    if not credential_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A scoped service credential is required",
        )

    credential = (
        await session.execute(
            select(ServiceCredential).where(
                ServiceCredential.id == uuid.UUID(credential_id),
                ServiceCredential.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service credential is unavailable",
        )

    membership_filter = ProjectMember.user_id == credential.principal_user_id
    total = int(
        (
            await session.execute(
                select(func.count(ProjectMember.id)).where(membership_filter)
            )
        ).scalar()
        or 0
    )
    membership_rows = (
        await session.execute(
            select(ProjectMember, Project)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(membership_filter)
            .order_by(Project.name.asc(), Project.id.asc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    projects: list[dict[str, Any]] = []
    fields_truncated = False
    redactions = 0
    for membership, project in membership_rows:
        projected, truncated, item_redactions = _bounded_agent_section(
            {
            "id": str(project.id),
            "name": project.name,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "role": membership.role,
            },
            "list_project",
            serialized_strings=True,
        )
        projects.append(projected if isinstance(projected, dict) else {})
        fields_truncated = fields_truncated or truncated
        redactions += item_redactions
    capabilities = sorted(
        {
            capability
            for scope in credential.scopes
            for capability in _CAPABILITIES_BY_SCOPE.get(scope, [])
        }
    )
    credential_state = _new_projection_state(16_000)
    bounded_credential = _bounded_agent_value(
        {
            "id": str(credential.id),
            "name": credential.name,
            "principal_user_id": credential.principal_user_id,
            "scopes": credential.scopes,
            "expires_at": utc_iso(credential.expires_at),
        },
        credential_state,
        serialized_strings=True,
    )
    fields_truncated = fields_truncated or bool(credential_state["truncated"])
    redactions += int(credential_state["redactions"])
    server_state = _new_projection_state(2_000)
    bounded_server = _bounded_agent_value(
        {"revision": os.getenv("K_REVISION", "local-dev")},
        server_state,
        serialized_strings=True,
    )
    fields_truncated = fields_truncated or bool(server_state["truncated"])
    redactions += int(server_state["redactions"])
    page_incomplete = total > offset + len(projects)
    truncation = {
        "source_page": page_incomplete,
        "items": False,
        "item_fields": fields_truncated,
        "response_bytes": False,
    }
    response = {
        "schema_version": "blueprint.agent-access.v1",
        "complete": not any(truncation.values()),
        "limits": {
            "response_bytes": _AGENT_LIST_RESPONSE_MAX_BYTES,
            "maximum_limit": _AGENT_ACCESS_PROJECT_LIMIT,
            "project_value_budget_bytes": _AGENT_TASK_SECTION_BUDGETS["list_project"],
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like values are removed",
        },
        "credential": bounded_credential,
        "projects": projects,
        "capabilities": capabilities,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(projects),
        "source_returned": len(membership_rows),
        "has_more": page_incomplete,
        "next_offset": offset + len(projects) if page_incomplete else None,
        "server": bounded_server,
    }
    _enforce_agent_list_response_size(response, item_key="projects")
    return response


@router.get("/projects/{project_id}/snapshot")
async def get_agent_project_snapshot(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return one versioned, bounded orientation read for a coding agent.

    The projection composes existing backend-owned read models so causal and
    capacity semantics stay identical across the UI, REST API, CLI fallback,
    and MCP adapter. The service-credential allowlist restricts this route to
    ``coding-agent:read``; the project lookup independently enforces viewer
    membership.
    """
    project = await _get_user_project(project_id, current_user["user_id"], session)
    attention = await build_operator_attention(
        session, project, current_user["user_id"]
    )
    diagnostics = await get_project_diagnostics(
        project_id=project_id,
        session=session,
        current_user=current_user,
        max_active_tasks=500,
    )
    resources = await get_project_resource_summary(
        project_id=project_id,
        session=session,
        current_user=current_user,
    )
    message_counts = await get_message_counts(
        project_id=str(project_id),
        last_seen_ms=None,
        session=session,
        current_user=current_user,
    )
    _remove_actor_relative_read_state(attention, message_counts)
    return _project_agent_snapshot(
        project,
        attention=attention,
        diagnostics=diagnostics,
        resources=resources,
        message_counts=message_counts,
    )


@router.get("/tasks")
async def list_agent_tasks(
    project_id: uuid.UUID = Query(...),
    status_filter: list[TaskStatus] = Query(default=[], alias="status"),
    task_type: str | None = Query(default=None, max_length=100),
    search: str | None = Query(default=None, max_length=200),
    id_prefix: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=50, ge=1, le=_AGENT_LIST_ITEM_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    approval_needed: bool | None = Query(default=None),
    direction_id: str | None = Query(default=None, max_length=255),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a bounded canonical task-list projection for one member project."""
    project = await _get_user_project(project_id, current_user["user_id"], session)
    raw_response = await get_admin_tasks(
        limit=limit,
        offset=offset,
        status=status_filter,
        terminal_only=False,
        archived=None,
        search=search,
        id_prefix=id_prefix,
        created_after=None,
        direction_id=direction_id,
        task_type=task_type,
        project_id=project_id,
        spawned_by=None,
        approval_needed=approval_needed,
        include_backend_managed=False,
        include_resident_infrastructure=False,
        session=session,
        current_user=current_user,
    )
    return _project_agent_task_list(
        raw_response,
        project,
        limit=limit,
        offset=offset,
        statuses=status_filter,
        task_type=task_type,
        search=search,
        id_prefix=id_prefix,
        direction_id=direction_id,
        approval_needed=approval_needed,
    )


@router.get("/messages")
async def list_agent_messages(
    project_id: uuid.UUID = Query(...),
    message_status: str | None = Query(default="open", alias="status"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=_AGENT_LIST_ITEM_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return bounded canonical operator-visible messages for one member project."""
    project = await _get_user_project(project_id, current_user["user_id"], session)
    raw_response = await get_admin_messages(
        project_id=str(project_id),
        status=message_status,
        limit=limit,
        offset=offset,
        q=q,
        include_guaranteed=False,
        session=session,
        current_user=current_user,
    )
    return _project_agent_message_list(
        raw_response,
        project,
        limit=limit,
        offset=offset,
        message_status=message_status,
        query=q,
    )


@router.get("/tasks/{task_id}")
async def get_agent_task_detail(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a bounded, redacted task detail for an authorized coding agent.

    The canonical admin task-detail reader remains the source of membership and
    task semantics. This route deliberately projects its result instead of
    exposing that UI-shaped response directly: every large collection and string
    has a published limit, credential-bearing fields/values are removed, and the
    response reports any truncation.
    """
    detail = await get_admin_task(
        task_id=str(task_id),
        session=session,
        current_user=current_user,
    )
    detail = await _filter_agent_task_links_to_project(detail, session)
    return _project_agent_task_detail(detail)
