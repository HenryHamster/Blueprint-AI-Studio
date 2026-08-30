#!/usr/bin/env python3
"""Read-only stdio MCP adapter for Blueprint's scoped agent-access API.

The server deliberately does not import ``blueprint.py``: remote diagnosis must
not depend on Linux deploy-agent modules or the operator CLI's large argparse
tree. It reuses ``blueprint_api`` for the single HTTP/auth implementation, but
requires an explicitly configured ``coding-agent:read`` or ``project-operator``
ServiceCredential. An ambient admin config, gcloud bootstrap, agent task JWT, or
observer token is rejected before any project data is read.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any
from urllib.parse import urlencode

from blueprint_api import APIError, api, load_config


SERVER_NAME = "blueprint"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
LEGACY_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-06-18",
    LEGACY_PROTOCOL_VERSION,
)
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, *reversed(LEGACY_PROTOCOL_VERSIONS))
CODING_AGENT_SCOPE = "coding-agent:read"
PROJECT_OPERATOR_SCOPE = "project-operator"
MCP_READ_SCOPES = (CODING_AGENT_SCOPE, PROJECT_OPERATOR_SCOPE)
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PUBLIC_CATALOG_TTL_MS = 300_000
PROJECT_PAGE_LIMIT = 50
PROJECT_FIELD_MAX_SERIALIZED_BYTES = 512
PROJECT_SELECTOR_MAX_CHARS = 200
PROJECT_ERROR_PREVIEW_LIMIT = 5
DIRECTION_PAGE_LIMIT = 100

SERVER_INSTRUCTIONS = (
    "Blueprint is a read-only operational context source. Start with "
    "get_project_snapshot, use exact task IDs for get_task, and use "
    "list_tasks/list_directions/list_messages only for bounded drill-down. No tool in "
    "this server authorizes mutations."
)

TASK_STATUSES = [
    "QUEUED",
    "PROVISIONING",
    "IN_PROGRESS",
    "EVALUATING",
    "BLOCKED",
    "PARKED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
MESSAGE_STATUSES = ["open", "linked", "resolved", "needs_action", "all"]


class ToolError(RuntimeError):
    """Safe, user-actionable MCP tool failure."""


class ProtocolError(RuntimeError):
    """JSON-RPC/MCP request-shape failure returned outside a tool result."""

    def __init__(
        self, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _project_selector_properties() -> dict[str, dict[str, Any]]:
    return {
        "project_id": {
            "type": "string",
            "format": "uuid",
            "description": "Exact Blueprint project UUID.",
        },
        "project": {
            "type": "string",
            "maxLength": PROJECT_SELECTOR_MAX_CHARS,
            "description": "Exact or uniquely matching Blueprint project name.",
        },
    }


def tool_definitions() -> list[dict[str, Any]]:
    """Return the complete, intentionally read-only MCP tool registry."""
    selector = _project_selector_properties()
    tools = [
        {
            "name": "list_projects",
            "description": (
                "List one bounded, offset-pageable set of projects this scoped "
                "credential can read. Use this when project identity "
                "is ambiguous."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": PROJECT_PAGE_LIMIT,
                        "default": PROJECT_PAGE_LIMIT,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1_000_000,
                        "default": 0,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_project_snapshot",
            "description": (
                "Canonical first call for Blueprint orientation. Returns bounded "
                "operator attention, stuck-task diagnostics, execution capacity, "
                "and timestamps with explicit truncation metadata. Use list_projects "
                "for the credential's effective project catalog."
            ),
            "inputSchema": {
                "type": "object",
                "properties": selector,
                "additionalProperties": False,
            },
        },
        {
            "name": "list_tasks",
            "description": (
                "List one bounded, offset-pageable set of Blueprint tasks for "
                "an authorized project. Use get_task for exact history and artifacts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **selector,
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string", "enum": TASK_STATUSES},
                        "uniqueItems": True,
                    },
                    "task_type": {"type": "string", "minLength": 1},
                    "search": {"type": "string", "maxLength": 200},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1_000_000,
                        "default": 0,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_task",
            "description": (
                "Read one exact Blueprint task by UUID, including its redacted "
                "payload, movement/history, artifacts, failure evidence, and links."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "format": "uuid"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_directions",
            "description": (
                "List one bounded, offset-pageable set of Blueprint directions "
                "for an authorized project. The backend excludes rich project "
                "state and returns explicit truncation metadata."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **selector,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": DIRECTION_PAGE_LIMIT,
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1_000_000,
                        "default": 0,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_messages",
            "description": (
                "List one bounded, offset-pageable set of operator-visible Blueprint "
                "messages for one project. The backend excludes non-operator audiences "
                "and large context blobs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **selector,
                    "status": {
                        "type": "string",
                        "enum": MESSAGE_STATUSES,
                        "default": "open",
                    },
                    "query": {"type": "string", "maxLength": 200},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1_000_000,
                        "default": 0,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_release_spawn_requests",
            "description": (
                "Read one snapshot-consistent, bounded release-window projection "
                "of Resident Strategic spawn receipts. Use its complete and "
                "two_task_spawn_chain_complete predicates with exact task readback "
                "for a canary proof."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **selector,
                    "version_id": {"type": "string", "format": "uuid"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1_000_000,
                        "default": 0,
                    },
                },
                "required": ["version_id"],
                "additionalProperties": False,
            },
        },
    ]
    for tool in tools:
        tool["annotations"] = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    return tools


def _explicit_agent_config_present() -> bool:
    if os.environ.get("BLUEPRINT_CONFIG"):
        return True
    return bool(
        os.environ.get("BLUEPRINT_API_URL")
        and os.environ.get("BLUEPRINT_API_KEY")
    )


def _access_profile(
    *, limit: int | None = None, offset: int | None = None
) -> dict[str, Any]:
    """Load and prove a supported scoped identity without leaking its token."""
    if not _explicit_agent_config_present():
        raise ToolError(
            "Blueprint MCP requires an explicit BLUEPRINT_CONFIG or "
            "BLUEPRINT_API_URL + BLUEPRINT_API_KEY. Ambient repo/home config and "
            "gcloud admin bootstrap are intentionally disabled for MCP clients."
        )
    config = _as_object(load_config(), "Blueprint config")
    token = str(config.get("api_key") or "")
    if not token.startswith("bpsc_"):
        raise ToolError(
            "Blueprint MCP refuses this credential because it is not a scoped "
            "ServiceCredential (expected a bpsc_ token)."
        )
    path = "/api/agent-access/v1/me"
    if limit is not None or offset is not None:
        path = _with_query(
            path,
            [("limit", limit or PROJECT_PAGE_LIMIT), ("offset", offset or 0)],
        )
    profile = _as_object(api("GET", path), "agent-access profile")
    credential = _as_object(profile.get("credential") or {}, "profile credential")
    scopes = credential.get("scopes") or []
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) for scope in scopes
    ):
        raise ToolError("profile credential scopes must be an array of strings")
    projects = profile.get("projects") or []
    if not isinstance(projects, list) or any(
        not isinstance(project, dict) for project in projects
    ):
        raise ToolError("profile projects must be an array of objects")
    if not set(scopes).intersection(MCP_READ_SCOPES):
        raise ToolError(
            "Blueprint MCP requires the coding-agent:read or project-operator "
            "scope; this credential "
            f"has: {', '.join(scopes) if scopes else '(none)'}"
        )
    return profile


def _as_object(value: Any, label: str = "arguments") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"{label} must be an object")
    return value


def _bounded_int(
    value: Any,
    *,
    label: str,
    default: int,
    maximum: int,
    minimum: int = 1,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ToolError(f"{label} must be between {minimum} and {maximum}")
    return value


def _validate_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{label} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError(f"{label} must be a valid UUID") from exc


def _repo_name(project: dict[str, Any]) -> str:
    owner = str(project.get("github_owner") or "").strip()
    repo = str(project.get("github_repo") or "").strip()
    return f"{owner}/{repo}" if owner and repo else repo


def _project_error_preview(
    projects: list[dict[str, Any]], *, total: int | None = None
) -> str:
    """Return a small safe catalog hint for selector errors."""
    labels = [
        str(project.get("name") or project.get("id") or "?")[:120]
        for project in projects[:PROJECT_ERROR_PREVIEW_LIMIT]
    ]
    total = max(len(projects), total or 0)
    suffix = (
        f", ... ({total} total; use list_projects pages)"
        if total > len(labels)
        else ""
    )
    return ", ".join(labels) + suffix


def _validated_project_selector(
    arguments: dict[str, Any],
) -> tuple[bool, bool, str | None, str | None]:
    raw_id = arguments.get("project_id")
    raw_name = arguments.get("project")
    has_id = "project_id" in arguments
    has_name = "project" in arguments
    if has_id and has_name:
        raise ToolError("pass project_id or project, not both")
    project_id = _validate_uuid(raw_id, "project_id") if has_id else None
    if has_name:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ToolError("project must be a non-empty string")
        if len(raw_name) > PROJECT_SELECTOR_MAX_CHARS:
            raise ToolError(
                f"project must be at most {PROJECT_SELECTOR_MAX_CHARS} characters"
            )
    project_name = raw_name.strip() if isinstance(raw_name, str) else None
    return has_id, has_name, project_id, project_name


def _select_project(
    arguments: dict[str, Any], *, profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve an authorized project without the CLI helper's SystemExit paths."""
    has_id, has_name, project_id, project_name = _validated_project_selector(arguments)

    profile = profile or _access_profile()
    projects = profile.get("projects") or []
    total = int(profile.get("total") or len(projects))
    has_more = bool(profile.get("has_more", total > len(projects)))
    if not projects:
        raise ToolError("this scoped credential has no project memberships")

    if has_id:
        return {"id": project_id}
    if has_name:
        if has_more:
            raise ToolError(
                f"project name resolution requires a complete catalog, but this "
                f"credential has {total} authorized projects; page list_projects "
                "and pass project_id"
            )
        needle = str(project_name).casefold()
        exact = [
            item
            for item in projects
            if str(item.get("name") or "").casefold() == needle
            or _repo_name(item).casefold() == needle
        ]
        if len(exact) == 1:
            return exact[0]
        matches = [
            item
            for item in projects
            if needle in str(item.get("name") or "").casefold()
            or needle in _repo_name(item).casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        available = _project_error_preview(projects, total=total)
        if not matches:
            raise ToolError(
                f"no authorized project matches {project_name!r}; available: {available}"
            )
        names = _project_error_preview(matches, total=len(matches))
        raise ToolError(f"project {project_name!r} is ambiguous; matches: {names}")

    config = load_config()
    configured_id = os.environ.get("BLUEPRINT_PROJECT_ID") or config.get("project_id")
    if configured_id:
        normalized_id = _validate_uuid(str(configured_id), "configured project_id")
        return {"id": normalized_id}
    default_repo = str(config.get("default_repo") or "").strip().casefold()
    if default_repo:
        if has_more:
            raise ToolError(
                f"default_repo resolution requires a complete catalog, but this "
                f"credential has {total} authorized projects; page list_projects "
                "and configure BLUEPRINT_PROJECT_ID"
            )
        matches = [item for item in projects if _repo_name(item).casefold() == default_repo]
        if len(matches) == 1:
            return matches[0]
    if len(projects) == 1:
        return projects[0]
    names = _project_error_preview(projects, total=total)
    raise ToolError(
        "multiple projects are authorized; pass project/project_id or configure "
        f"BLUEPRINT_PROJECT_ID. Available: {names}"
    )


def _with_query(path: str, pairs: list[tuple[str, Any]]) -> str:
    return f"{path}?{urlencode(pairs, doseq=True)}" if pairs else path


def _bounded_profile_text(value: Any) -> str | None:
    """Clip one profile string by its serialized JSON size."""
    if value is None:
        return None
    raw = str(value)
    if (
        len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        <= PROJECT_FIELD_MAX_SERIALIZED_BYTES
    ):
        return raw
    marker = "[truncated]"
    low = 0
    high = len(raw)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = raw[:midpoint] + marker
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= PROJECT_FIELD_MAX_SERIALIZED_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    return raw[:low] + marker


def _list_projects(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(
        arguments.get("limit"),
        label="limit",
        default=PROJECT_PAGE_LIMIT,
        maximum=PROJECT_PAGE_LIMIT,
    )
    offset = _bounded_int(
        arguments.get("offset"),
        label="offset",
        default=0,
        maximum=1_000_000,
        minimum=0,
    )
    profile = (
        _access_profile()
        if limit == PROJECT_PAGE_LIMIT and offset == 0
        else _access_profile(limit=limit, offset=offset)
    )
    source_projects = profile["projects"]
    selected_projects = source_projects[:limit]
    projects = [
        {
            key: _bounded_profile_text(project.get(key))
            for key in ("id", "name", "github_owner", "github_repo", "role")
        }
        for project in selected_projects
    ]
    project_fields_truncated = any(
        bounded.get(key)
        != (None if source.get(key) is None else str(source.get(key)))
        for source, bounded in zip(selected_projects, projects, strict=True)
        for key in ("id", "name", "github_owner", "github_repo", "role")
    )
    total = int(profile.get("total", offset + len(projects)))
    has_more = bool(profile.get("has_more", offset + len(projects) < total))
    credential = profile.get("credential") or {}
    bounded_credential = {
        key: _bounded_profile_text(credential.get(key))
        for key in ("id", "name", "expires_at")
    }
    access_fields_truncated = any(
        bounded_credential[key]
        != (None if credential.get(key) is None else str(credential.get(key)))
        for key in bounded_credential
    )
    source_scopes = list(credential.get("scopes") or [])
    scopes = [_bounded_profile_text(value) for value in source_scopes[:20]]
    source_capabilities = list(profile.get("capabilities") or [])
    capabilities = [
        _bounded_profile_text(value) for value in source_capabilities[:50]
    ]
    source_revision = (profile.get("server") or {}).get("revision")
    revision = _bounded_profile_text(source_revision)
    access_fields_truncated = access_fields_truncated or (
        len(source_scopes) > len(scopes)
        or len(source_capabilities) > len(capabilities)
        or any(value != str(source) for value, source in zip(scopes, source_scopes))
        or any(
            value != str(source)
            for value, source in zip(capabilities, source_capabilities)
        )
        or revision != (None if source_revision is None else str(source_revision))
    )
    profile_truncation = profile.get("truncation") or {}
    backend_fields_truncated = bool(
        profile_truncation.get("item_fields")
        or profile_truncation.get("items")
        or profile_truncation.get("response_bytes")
    )
    return {
        "schema_version": "blueprint.projects.v1",
        "complete": not (
            has_more
            or project_fields_truncated
            or access_fields_truncated
            or backend_fields_truncated
        ),
        "limits": {
            "maximum_limit": PROJECT_PAGE_LIMIT,
            "project_field_serialized_bytes": PROJECT_FIELD_MAX_SERIALIZED_BYTES,
        },
        "truncation": {
            "source_page": has_more,
            "project_fields": project_fields_truncated,
            "access_fields": access_fields_truncated,
            "backend_fields": backend_fields_truncated,
        },
        "projects": projects,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(projects),
        "has_more": has_more,
        "next_offset": offset + len(projects) if has_more else None,
        "access": {
            "credential": bounded_credential,
            "scopes": scopes,
            "capabilities": capabilities,
        },
        "server": {"revision": revision},
    }


def _get_project_snapshot(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    # Reject malformed explicit selectors before making any authenticated read.
    _validated_project_selector(arguments)
    profile = _access_profile()
    project = _select_project(arguments, profile=profile)
    project_id = project["id"]
    result = _as_object(
        api("GET", f"/api/agent-access/v1/projects/{project_id}/snapshot"),
        "project snapshot",
    )
    # A bounded snapshot can be intentionally partial. Its explicit
    # complete/truncation metadata is useful data, not an MCP execution error.
    return result, False


def _list_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _select_project(arguments)
    limit = _bounded_int(arguments.get("limit"), label="limit", default=50, maximum=200)
    offset = _bounded_int(
        arguments.get("offset"),
        label="offset",
        default=0,
        maximum=1_000_000,
        minimum=0,
    )
    pairs: list[tuple[str, Any]] = [
        ("project_id", project["id"]),
        ("limit", limit),
        ("offset", offset),
    ]
    statuses = arguments.get("statuses") or []
    if not isinstance(statuses, list) or any(status not in TASK_STATUSES for status in statuses):
        raise ToolError("statuses must contain only supported task statuses")
    pairs.extend(("status", status) for status in dict.fromkeys(statuses))
    task_type = arguments.get("task_type")
    if task_type is not None:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ToolError("task_type must be a non-empty string")
        pairs.append(("task_type", task_type.strip()))
    search = arguments.get("search")
    if search is not None:
        if not isinstance(search, str) or len(search) > 200:
            raise ToolError("search must be a string of at most 200 characters")
        if search.strip():
            pairs.append(("search", search.strip()))
    return _as_object(
        api("GET", _with_query("/api/agent-access/v1/tasks", pairs)),
        "bounded agent task list",
    )


def _get_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = _validate_uuid(arguments.get("task_id"), "task_id")
    # get_task has no project selector, so explicitly prove the configured
    # credential before the backend performs the task -> project membership
    # lookup. This also prevents a legacy admin key from reaching the route.
    _access_profile()
    return _as_object(
        api("GET", f"/api/agent-access/v1/tasks/{task_id}"),
        "bounded agent task",
    )


def _list_directions(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _select_project(arguments)
    limit = _bounded_int(
        arguments.get("limit"),
        label="limit",
        default=50,
        maximum=DIRECTION_PAGE_LIMIT,
    )
    offset = _bounded_int(
        arguments.get("offset"),
        label="offset",
        default=0,
        maximum=1_000_000,
        minimum=0,
    )
    return _as_object(
        api(
            "GET",
            _with_query(
                "/api/agent-access/v1/directions",
                [
                    ("project_id", project["id"]),
                    ("limit", limit),
                    ("offset", offset),
                ],
            ),
        ),
        "bounded agent direction list",
    )


def _list_messages(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _select_project(arguments)
    limit = _bounded_int(arguments.get("limit"), label="limit", default=50, maximum=200)
    offset = _bounded_int(
        arguments.get("offset"),
        label="offset",
        default=0,
        maximum=1_000_000,
        minimum=0,
    )
    status_value = arguments.get("status", "open")
    if status_value not in MESSAGE_STATUSES:
        raise ToolError(f"status must be one of: {', '.join(MESSAGE_STATUSES)}")
    pairs: list[tuple[str, Any]] = [
        ("project_id", project["id"]),
        ("status", status_value),
        ("limit", limit),
        ("offset", offset),
    ]
    query = arguments.get("query")
    if query is not None:
        if not isinstance(query, str) or len(query) > 200:
            raise ToolError("query must be a string of at most 200 characters")
        if query.strip():
            pairs.append(("q", query.strip()))
    return _as_object(
        api("GET", _with_query("/api/agent-access/v1/messages", pairs)),
        "bounded agent message list",
    )


def _list_release_spawn_requests(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _select_project(arguments)
    version_id = _validate_uuid(arguments.get("version_id"), "version_id")
    limit = _bounded_int(
        arguments.get("limit"), label="limit", default=20, maximum=50
    )
    offset = _bounded_int(
        arguments.get("offset"),
        label="offset",
        default=0,
        maximum=1_000_000,
        minimum=0,
    )
    path = (
        f"/api/agent-access/v1/projects/{project['id']}/release-versions/"
        f"{version_id}/strategic-spawn-requests"
    )
    return _as_object(
        api(
            "GET",
            _with_query(path, [("limit", limit), ("offset", offset)]),
        ),
        "bounded release spawn request list",
    )


def call_tool(name: str, arguments: Any) -> tuple[dict[str, Any], bool]:
    """Execute a known Blueprint tool after the MCP envelope is validated."""
    args = _as_object(arguments)
    allowed_keys = {
        tool["name"]: set(tool["inputSchema"].get("properties", {}))
        for tool in tool_definitions()
    }
    if name not in allowed_keys:
        raise ProtocolError(-32602, f"Unknown tool: {name}")
    unexpected = sorted(set(args) - allowed_keys[name])
    if unexpected:
        raise ProtocolError(
            -32602,
            f"Invalid arguments for tool {name}: unexpected field(s): "
            f"{', '.join(unexpected)}",
        )
    if name == "list_projects":
        return _list_projects(args), False
    if name == "get_project_snapshot":
        return _get_project_snapshot(args)
    if name == "list_tasks":
        return _list_tasks(args), False
    if name == "get_task":
        return _get_task(args), False
    if name == "list_directions":
        return _list_directions(args), False
    if name == "list_messages":
        return _list_messages(args), False
    if name == "list_release_spawn_requests":
        return _list_release_spawn_requests(args), False
    raise ProtocolError(-32602, f"Unknown tool: {name}")


def _server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, default=str, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def _complete_result(
    result: dict[str, Any],
    *,
    modern: bool,
    ttl_ms: int | None = None,
    cache_scope: str | None = None,
) -> dict[str, Any]:
    """Attach 2026-era result metadata without changing legacy wire responses."""
    if not modern:
        return result
    completed = {"resultType": "complete", **result}
    meta = dict(completed.get("_meta") or {})
    meta.setdefault(SERVER_INFO_META_KEY, _server_info())
    completed["_meta"] = meta
    if ttl_ms is not None:
        completed["ttlMs"] = ttl_ms
    if cache_scope is not None:
        completed["cacheScope"] = cache_scope
    return completed


def _success_response(
    request_id: Any,
    result: dict[str, Any],
    *,
    modern: bool,
    ttl_ms: int | None = None,
    cache_scope: str | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": _complete_result(
            result,
            modern=modern,
            ttl_ms=ttl_ms,
            cache_scope=cache_scope,
        ),
    }


def _error_response(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _valid_request_id(value: Any) -> bool:
    # JSON-RPC permits a Null id (while discouraging it); missing ``id`` alone
    # denotes a notification.
    return value is None or (
        not isinstance(value, bool) and isinstance(value, (str, int, float))
    )


def _request_id_for_error(request: Any) -> Any:
    if not isinstance(request, dict) or "id" not in request:
        return None
    request_id = request["id"]
    return request_id if _valid_request_id(request_id) else None


def _params_object(request: dict[str, Any]) -> dict[str, Any]:
    if "params" not in request:
        return {}
    params = request["params"]
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "Invalid params: params must be an object")
    return params


def _reject_unexpected_params(
    params: dict[str, Any], allowed: set[str], method: str
) -> None:
    unexpected = sorted(set(params) - allowed)
    if unexpected:
        raise ProtocolError(
            -32602,
            f"Invalid params for {method}: unexpected field(s): "
            f"{', '.join(unexpected)}",
        )


def _validate_implementation(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ProtocolError(-32602, f"Invalid params: {label} must be an object")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ProtocolError(
            -32602, f"Invalid params: {label}.name must be a non-empty string"
        )
    if not isinstance(value.get("version"), str) or not value["version"]:
        raise ProtocolError(
            -32602, f"Invalid params: {label}.version must be a non-empty string"
        )


def _is_modern_request(method: str, params: dict[str, Any]) -> bool:
    """Validate the 2026 per-request envelope and identify the wire era."""
    if "_meta" not in params:
        if method == "server/discover":
            raise ProtocolError(
                -32602,
                "Invalid params: server/discover requires the MCP request _meta envelope",
            )
        return False

    meta = params["_meta"]
    if not isinstance(meta, dict):
        raise ProtocolError(-32602, "Invalid params: _meta must be an object")
    if PROTOCOL_VERSION_META_KEY not in meta:
        if method == "server/discover":
            raise ProtocolError(
                -32602,
                f"Invalid params: _meta.{PROTOCOL_VERSION_META_KEY} is required",
            )
        # Legacy MCP also permits arbitrary metadata. The reserved protocol
        # version key is the unambiguous marker for a modern request.
        return False

    requested_version = meta[PROTOCOL_VERSION_META_KEY]
    if not isinstance(requested_version, str):
        raise ProtocolError(
            -32602,
            f"Invalid params: _meta.{PROTOCOL_VERSION_META_KEY} must be a string",
        )
    if requested_version != PROTOCOL_VERSION:
        raise ProtocolError(
            -32022,
            "Unsupported protocol version",
            {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": requested_version,
            },
        )
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, dict):
        raise ProtocolError(
            -32602,
            f"Invalid params: _meta.{CLIENT_CAPABILITIES_META_KEY} "
            "must be an object",
        )
    if CLIENT_INFO_META_KEY in meta:
        _validate_implementation(
            meta[CLIENT_INFO_META_KEY], f"_meta.{CLIENT_INFO_META_KEY}"
        )
    return True


def _legacy_initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested_version = params.get("protocolVersion")
    if not isinstance(requested_version, str) or not requested_version:
        raise ProtocolError(
            -32602, "Invalid params: initialize.protocolVersion is required"
        )
    if not isinstance(params.get("capabilities"), dict):
        raise ProtocolError(
            -32602,
            "Invalid params: initialize.capabilities is required and must be an object",
        )
    if "clientInfo" not in params:
        raise ProtocolError(
            -32602, "Invalid params: initialize.clientInfo is required"
        )
    _validate_implementation(params["clientInfo"], "initialize.clientInfo")
    negotiated_version = (
        requested_version
        if requested_version in LEGACY_PROTOCOL_VERSIONS
        else LEGACY_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": negotiated_version,
        "capabilities": {"tools": {}},
        "serverInfo": _server_info(),
        "instructions": SERVER_INSTRUCTIONS,
    }


def _dispatch_request(request: dict[str, Any]) -> dict[str, Any]:
    method = request["method"]
    request_id = request["id"]
    params = _params_object(request)

    if method == "initialize":
        return _success_response(
            request_id, _legacy_initialize_result(params), modern=False
        )

    modern = _is_modern_request(method, params)
    if method == "server/discover":
        _reject_unexpected_params(params, {"_meta"}, method)
        return _success_response(
            request_id,
            {
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": SERVER_INSTRUCTIONS,
            },
            modern=True,
            ttl_ms=PUBLIC_CATALOG_TTL_MS,
            cache_scope="public",
        )
    if method == "ping":
        _reject_unexpected_params(params, {"_meta"}, method)
        return _success_response(request_id, {}, modern=modern)
    if method == "tools/list":
        _reject_unexpected_params(params, {"_meta", "cursor"}, method)
        if params.get("cursor") is not None:
            raise ProtocolError(-32602, "Invalid params: unknown tools/list cursor")
        return _success_response(
            request_id,
            {"tools": tool_definitions()},
            modern=modern,
            ttl_ms=PUBLIC_CATALOG_TTL_MS if modern else None,
            cache_scope="public" if modern else None,
        )
    if method == "tools/call":
        _reject_unexpected_params(
            params,
            {"_meta", "name", "arguments"},
            method,
        )
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(
                -32602, "Invalid params: tools/call.name must be a non-empty string"
            )
        known_tools = {tool["name"] for tool in tool_definitions()}
        if name not in known_tools:
            raise ProtocolError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolError(
                -32602, "Invalid params: tools/call.arguments must be an object"
            )
        try:
            payload, partial = call_tool(name, arguments)
            result = _tool_result(payload, is_error=partial)
        except ProtocolError:
            raise
        except (APIError, ToolError, OSError, json.JSONDecodeError) as exc:
            result = _tool_result(
                {
                    "schema_version": "blueprint.tool-error.v1",
                    "error": str(exc),
                },
                is_error=True,
            )
        return _success_response(request_id, result, modern=modern)
    raise ProtocolError(-32601, f"Method not found: {method}")


def handle_request(request: Any) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message; valid notifications get no reply."""
    request_id = _request_id_for_error(request)
    if not isinstance(request, dict):
        return _error_response(None, -32600, "Invalid Request")
    if request.get("jsonrpc") != "2.0":
        return _error_response(request_id, -32600, "Invalid Request")
    if not isinstance(request.get("method"), str) or not request["method"]:
        return _error_response(request_id, -32600, "Invalid Request")
    if "id" in request and not _valid_request_id(request["id"]):
        return _error_response(None, -32600, "Invalid Request")
    if "id" not in request:
        # JSON-RPC notifications never receive a response, even when the method
        # is unsupported or its parameters would otherwise be invalid.
        return None
    try:
        return _dispatch_request(request)
    except ProtocolError as exc:
        return _error_response(request["id"], exc.code, exc.message, exc.data)
    except Exception:  # noqa: BLE001 - never leak internals over the MCP boundary
        return _error_response(request["id"], -32603, "Internal error")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def handle_message(raw_message: bytes | str) -> dict[str, Any] | None:
    """Decode one UTF-8 JSON-RPC frame, including standard parse errors."""
    try:
        request = json.loads(raw_message, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return _error_response(None, -32700, "Parse error")
    return handle_request(request)


def _binary_stream(stream: Any) -> Any:
    return getattr(stream, "buffer", stream)


def _write_message(stream: Any, response: dict[str, Any]) -> None:
    encoded = (
        json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    try:
        stream.write(encoded)
    except TypeError:
        # Text streams are useful for direct tests; production always selects
        # the underlying binary standard stream above.
        stream.write(encoded.decode("utf-8"))
    stream.flush()


def serve(stdin: Any = None, stdout: Any = None) -> None:
    """Serve newline-delimited, binary UTF-8 MCP JSON-RPC messages over stdio."""
    input_stream = stdin if stdin is not None else _binary_stream(sys.stdin)
    output_stream = stdout if stdout is not None else _binary_stream(sys.stdout)
    for raw_line in input_stream:
        response = handle_message(raw_line)
        if response is not None:
            _write_message(output_stream, response)


if __name__ == "__main__":
    serve()
