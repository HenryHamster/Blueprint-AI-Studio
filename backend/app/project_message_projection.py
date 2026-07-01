"""Shared read projections for ProjectMessage.

The message table backs several public surfaces with intentionally different
field names. Keep those compatibility wrappers here so enum normalization,
timestamps, and empty context handling do not drift route-by-route.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.models import ProjectMessage
from app.workbench_state import (
    sanitize_strategic_session_snapshot,
    strategic_session_snapshot_has_signal,
)
from app.workbench_events import (
    SURFACE_MESSAGES,
    SURFACE_STRATEGIC_SESSION,
    workbench_event_envelope,
)


DEFAULT_MESSAGE_CONTEXT_PREVIEW_KEYS = (
    "event_type",
    "direction_id",
    "task_id",
    "source_task_id",
    "dedupe_key",
)

MESSAGE_EVENT_REFETCH_SURFACES = (SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION)
_MESSAGE_EVENT_EXCERPT_MAX_CHARS = 160


def enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None


def _message_context(message: ProjectMessage) -> dict[str, Any]:
    context = message.context
    return context if isinstance(context, dict) else {}


def compact_text(value: Any, *, max_chars: int = 360) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def project_message_base(message: ProjectMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "project_id": str(message.project_id),
        "message_type": enum_value(message.message_type),
        "from_type": message.from_type,
        "from_task_id": str(message.from_task_id) if message.from_task_id else None,
        "content": message.content,
        "context": _message_context(message),
        "status": enum_value(message.status),
        "resolution": message.resolution,
        "resolved_at": _isoformat(message.resolved_at),
        "created_at": _isoformat(message.created_at),
    }


def admin_message_dict(message: ProjectMessage) -> dict[str, Any]:
    return project_message_base(message)


def message_event_payload(
    message: ProjectMessage,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    message_type = enum_value(message.message_type)
    message_status = status or enum_value(message.status)
    action = "created"
    if status:
        action = "resolved" if status == "resolved" else "updated"
    message_summary: dict[str, Any] = {
        "id": str(message.id),
        "type": message_type,
        "from": str(message.from_type),
        "status": message_status,
    }
    excerpt = compact_text(
        message.content,
        max_chars=_MESSAGE_EVENT_EXCERPT_MAX_CHARS,
    )
    if excerpt:
        message_summary["excerpt"] = excerpt
    payload: dict[str, Any] = {
        "message_id": str(message.id),
        "type": message_type,
        "from": str(message.from_type),
        "message": message_summary,
        **workbench_event_envelope(
            message.project_id,
            resource="project_message",
            action=action,
            refetch_surfaces=MESSAGE_EVENT_REFETCH_SURFACES,
        ),
    }
    if status is not None:
        payload["status"] = status
    return payload


def message_updated_event_payload(message: ProjectMessage) -> dict[str, Any]:
    return message_event_payload(message, status=enum_value(message.status))


def legacy_direction_message_dict(message: ProjectMessage) -> dict[str, Any]:
    base = project_message_base(message)
    return {
        "id": base["id"],
        "type": base["message_type"],
        "content": base["content"],
        "from": base["from_type"],
        "status": base["status"],
        "context": base["context"],
        "created_at": base["created_at"],
    }


def agent_thread_message_dict(message: ProjectMessage) -> dict[str, Any]:
    base = project_message_base(message)
    return {
        "id": base["id"],
        "type": base["message_type"],
        "content": base["content"],
        "context": base["context"],
        "from": base["from_type"],
        "status": base["status"],
        "resolution": base["resolution"],
        "created_at": base["created_at"],
        "resolved_at": base["resolved_at"],
    }


def agent_fetch_message_dict(
    message: ProjectMessage,
    *,
    convergence_link: Any = None,
) -> dict[str, Any]:
    base = project_message_base(message)
    return {
        "id": base["id"],
        "content": base["content"],
        "type": base["message_type"],
        "from_type": base["from_type"],
        "status": base["status"],
        "resolution": base["resolution"],
        "convergence_link": convergence_link,
        "created_at": base["created_at"],
    }


def message_preview_dict(
    message: ProjectMessage,
    *,
    context_keys: Iterable[str] = DEFAULT_MESSAGE_CONTEXT_PREVIEW_KEYS,
    content_max_chars: int | None = 280,
    context_max_chars: int = 120,
) -> dict[str, Any]:
    base = project_message_base(message)
    raw_context = base["context"]
    context_preview = {
        key: compact_text(raw_context.get(key), max_chars=context_max_chars)
        for key in context_keys
        if raw_context.get(key) is not None
    }
    if isinstance(raw_context.get("strategic_session"), dict):
        strategic_session = sanitize_strategic_session_snapshot(
            raw_context.get("strategic_session"),
        )
        if strategic_session_snapshot_has_signal(strategic_session):
            context_preview["strategic_session"] = strategic_session
    return {
        **base,
        "content": (
            base["content"]
            if content_max_chars is None
            else compact_text(base["content"], max_chars=content_max_chars) or ""
        ),
        "context": context_preview,
    }
