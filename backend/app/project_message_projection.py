"""Shared read projections for ProjectMessage.

The message table backs several public surfaces with intentionally different
field names. Keep those compatibility wrappers here so enum normalization,
timestamps, and empty context handling do not drift route-by-route.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

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
    "reply_preview",
    "attachments",
    "source_refs",
    "artifact_refs",
    "operator_intent",
)

# Compact set of context keys rendered by the list UI (grouping, severity badges,
# reply previews, convergence links, etc.). strategic_session is handled separately
# below via sanitize_strategic_session_snapshot to keep the payload bounded.
_LIST_CONTEXT_KEYS = frozenset({
    "event_type",
    "severity",
    "annotation_type",
    "task_type",
    "source",
    "milestone",
    "reply_to",
    "reply_preview",
    "attachments",
    "source_refs",
    "artifact_refs",
    "operator_intent",
    "_convergence_link",
    "_provenance_warnings",
    "task_id",
    "pr_url",
    "result",
})

MESSAGE_EVENT_REFETCH_SURFACES = (SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION)
_MESSAGE_EVENT_EXCERPT_MAX_CHARS = 160
MESSAGE_EVENT_MAX_BYTES = 16_384
MESSAGE_CONTEXT_PROJECTION_MAX_BYTES = 16_384
_STRUCTURED_CONTEXT_KEYS = frozenset({
    "attachments",
    "source_refs",
    "artifact_refs",
    "operator_intent",
    "reply_preview",
})
_SECRET_FIELD_MARKERS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "cookie",
    "signed_url",
    "storage_uri",
    "download_url",
    "upload_url",
)
_STRUCTURED_FIELD_ALLOWLISTS: dict[str, frozenset[str]] = {
    "attachments": frozenset({
        "id", "attachment_id", "kind", "type", "filename", "name", "label",
        "mime_type", "media_type", "revision", "checksum", "size_bytes",
        "status", "author", "timestamp", "created_at", "updated_at",
    }),
    "source_refs": frozenset({
        "id", "message_id", "task_id", "artifact_id", "tool_id", "project_id",
        "kind", "label", "title", "excerpt", "revision", "checksum", "author",
        "timestamp", "created_at", "updated_at",
    }),
    "artifact_refs": frozenset({
        "id", "artifact_id", "tool_id", "project_id", "kind", "label", "title",
        "revision", "checksum", "author", "timestamp", "created_at", "updated_at",
    }),
    "operator_intent": frozenset({
        "kind", "action", "lane_owner", "summary", "target", "priority",
        "approval", "operation_id", "artifact_ref_id", "expected_revision",
    }),
    "reply_preview": frozenset({
        "id", "message_id", "from_type", "actor_user_id", "sequence", "content",
        "created_at", "updated_at",
    }),
}
_UNSAFE_STRUCTURED_VALUE = object()
# Durable internal context key stamping the operator refresh wakeup_state on a
# message row.
WAKEUP_STATE_CONTEXT_KEY = "_operator_wakeup_state"
_INTERNAL_MESSAGE_CONTEXT_KEYS = frozenset({
    "_message_request_fingerprint",
    WAKEUP_STATE_CONTEXT_KEY,
})


def enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None


def _message_context(message: ProjectMessage) -> dict[str, Any]:
    context = message.context
    if not isinstance(context, dict):
        return {}
    # Internal delivery/idempotency receipts are durable server state, not
    # operator-visible chips and not strategist prompt context.
    projected: dict[str, Any] = {}
    preferred = [
        *DEFAULT_MESSAGE_CONTEXT_PREVIEW_KEYS,
        "strategic_session",
        "_convergence_link",
        "_provenance_warnings",
    ]
    ordered_keys = [*preferred, *(key for key in context if key not in preferred)]
    truncated = False
    for key in ordered_keys[:128]:
        if key not in context:
            continue
        value = context[key]
        if key in _INTERNAL_MESSAGE_CONTEXT_KEYS:
            continue
        if _field_is_sensitive(key):
            continue
        if key in _STRUCTURED_CONTEXT_KEYS:
            sanitized = _sanitize_structured_context(key, value)
        elif key == "strategic_session" and isinstance(value, dict):
            sanitized = sanitize_strategic_session_snapshot(value)
        else:
            sanitized = _bounded_legacy_context_value(value)
        if sanitized is _UNSAFE_STRUCTURED_VALUE or sanitized is None:
            continue
        candidate = {**projected, key: sanitized}
        if len(json.dumps(candidate, separators=(",", ":"), default=str).encode("utf-8")) > MESSAGE_CONTEXT_PROJECTION_MAX_BYTES - 64:
            truncated = True
            continue
        projected = candidate
    if len(context) > 128:
        truncated = True
    if truncated:
        projected["projection_truncated"] = True
    return projected


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
        "actor_user_id": (
            str(message.actor_user_id)
            if getattr(message, "actor_user_id", None)
            else None
        ),
        "sequence": getattr(message, "sequence", None),
        "surface": enum_value(getattr(message, "surface", "activity")),
        "handling_state": enum_value(
            getattr(message, "handling_state", "pending")
        ),
        "handled_by_task_id": (
            str(message.handled_by_task_id)
            if getattr(message, "handled_by_task_id", None)
            else None
        ),
        "reply_to_message_id": (
            str(message.reply_to_message_id)
            if getattr(message, "reply_to_message_id", None)
            else None
        ),
        "idempotency_key": getattr(message, "idempotency_key", None),
        "content": message.content,
        "context": _message_context(message),
        "status": enum_value(message.status),
        "resolution": message.resolution,
        "resolved_at": _isoformat(message.resolved_at),
        "delivered_at": _isoformat(getattr(message, "delivered_at", None)),
        "created_at": _isoformat(message.created_at),
        "updated_at": _isoformat(getattr(message, "updated_at", None)),
    }


def admin_message_dict(message: ProjectMessage) -> dict[str, Any]:
    return project_message_base(message)


def admin_message_list_dict(message: ProjectMessage) -> dict[str, Any]:
    """Like admin_message_dict but filters context to the compact set rendered by the list UI.

    The list endpoint fetches context but strips large keys before returning, keeping
    the 30-second poll payload small while preserving context fields needed for grouping,
    severity, badges, reply previews, and the strategic context descriptor on user messages.
    strategic_session is included but sanitized via sanitize_strategic_session_snapshot.
    """
    ctx = _message_context(message)
    compact_ctx = {}
    for key, value in ctx.items():
        if key not in _LIST_CONTEXT_KEYS:
            continue
        if key in _STRUCTURED_CONTEXT_KEYS:
            sanitized = _sanitize_structured_context(key, value)
            if sanitized is not None:
                compact_ctx[key] = sanitized
        else:
            compact_ctx[key] = value
    if isinstance(ctx.get("strategic_session"), dict):
        sanitized = sanitize_strategic_session_snapshot(ctx["strategic_session"])
        if strategic_session_snapshot_has_signal(sanitized):
            compact_ctx["strategic_session"] = sanitized
    return {**project_message_base(message), "context": compact_ctx}


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
    canonical_message = message_preview_dict(message, content_max_chars=None)
    canonical_message["client_idempotency_key"] = canonical_message.get(
        "idempotency_key"
    )
    message_summary: dict[str, Any] = {
        **canonical_message,
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
    return _fit_message_event_budget(payload)


def message_updated_event_payload(message: ProjectMessage) -> dict[str, Any]:
    return message_event_payload(message, status=enum_value(message.status))


def _field_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered.endswith("_uri")
        or lowered.endswith("_url")
        or any(marker in lowered for marker in _SECRET_FIELD_MARKERS)
    )


def _bounded_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            item[:160] if isinstance(item, str) else item
            for item in list(value)[:8]
            if item is None or isinstance(item, (str, bool, int, float))
        ]
    # Structured context fields are a typed transport contract. Stringifying a
    # mapping or arbitrary object here can smuggle nested credentials through
    # an otherwise-safe scalar field such as ``author`` or ``target``.
    return _UNSAFE_STRUCTURED_VALUE


def _bounded_legacy_context_value(value: Any, *, depth: int = 0) -> Any:
    """Bound legacy non-contract context without stringifying nested objects."""

    if isinstance(value, str):
        return value[:2_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return _UNSAFE_STRUCTURED_VALUE
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:24]:
            key = str(raw_key)[:120]
            if _field_is_sensitive(key):
                continue
            bounded = _bounded_legacy_context_value(item, depth=depth + 1)
            if bounded is not _UNSAFE_STRUCTURED_VALUE:
                result[key] = bounded
        return result
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for item in list(value)[:12]:
            bounded = _bounded_legacy_context_value(item, depth=depth + 1)
            if bounded is not _UNSAFE_STRUCTURED_VALUE:
                result.append(bounded)
        return result
    return _UNSAFE_STRUCTURED_VALUE


def _sanitize_structured_record(
    kind: str,
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = _STRUCTURED_FIELD_ALLOWLISTS[kind]
    sanitized: dict[str, Any] = {}
    for raw_key, item in list(value.items())[:32]:
        key = str(raw_key)
        if key not in allowed or _field_is_sensitive(key):
            continue
        bounded = _bounded_public_value(item)
        if bounded is _UNSAFE_STRUCTURED_VALUE:
            continue
        sanitized[key] = bounded
    return sanitized or None


def _sanitize_structured_context(
    kind: str,
    value: Any,
) -> Any:
    """Return only the public, typed shape for a visible message context field."""

    if kind == "reply_preview" and isinstance(value, str):
        return value[:500]
    if kind in {"attachments", "source_refs", "artifact_refs"}:
        if not isinstance(value, (list, tuple)):
            return None
        rows = [
            record
            for item in list(value)[:12]
            if (record := _sanitize_structured_record(kind, item)) is not None
        ]
        return rows or None
    return _sanitize_structured_record(kind, value)


def _payload_size(value: Any) -> int:
    return len(
        json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
    )


def _transport_text(value: Any, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:max_chars] or None


def _minimal_message_event_payload(
    payload: dict[str, Any],
    message: dict[str, Any],
    *,
    had_content: bool,
    had_resolution: bool,
) -> dict[str, Any]:
    """Rebuild a genuinely bounded event instead of retaining legacy aliases."""

    idempotency_key = _transport_text(
        message.get("idempotency_key") or message.get("client_idempotency_key"),
        max_chars=255,
    )
    sequence = message.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        sequence = None
    refetch = payload.get("refetch") if isinstance(payload.get("refetch"), dict) else {}
    raw_surfaces = refetch.get("surfaces") if isinstance(refetch, dict) else []
    surfaces = [
        surface
        for item in (raw_surfaces if isinstance(raw_surfaces, list) else [])[:8]
        if (surface := _transport_text(item, max_chars=80))
    ]
    minimal_message = {
        "id": _transport_text(message.get("id"), max_chars=80),
        "project_id": _transport_text(message.get("project_id"), max_chars=80),
        "sequence": sequence,
        "surface": _transport_text(message.get("surface"), max_chars=32),
        "handling_state": _transport_text(message.get("handling_state"), max_chars=32),
        "reply_to_message_id": _transport_text(
            message.get("reply_to_message_id"), max_chars=80
        ),
        "idempotency_key": idempotency_key,
        "client_idempotency_key": idempotency_key,
        "content": "",
        "context": {},
        "projection_truncated": True,
        "content_truncated": had_content,
        "resolution_truncated": had_resolution,
    }
    minimal = {
        "message_id": _transport_text(payload.get("message_id"), max_chars=80),
        "type": _transport_text(payload.get("type"), max_chars=64),
        "from": _transport_text(payload.get("from"), max_chars=32),
        "message": minimal_message,
        "schema_version": 1,
        "project_id": _transport_text(payload.get("project_id"), max_chars=80),
        "resource": "project_message",
        "action": _transport_text(payload.get("action"), max_chars=32) or "updated",
        "refetch": {"surfaces": surfaces},
        "canonical_refetch_required": True,
    }
    if "status" in payload:
        minimal["status"] = _transport_text(payload.get("status"), max_chars=32)
    return minimal


def _fit_message_event_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce a hard serialized websocket-event ceiling.

    The durable row remains canonical and refetchable. If a pathological legacy
    row exceeds the realtime budget, trim the transport projection truthfully
    rather than allowing one event to monopolize the project websocket.
    """

    if _payload_size(payload) <= MESSAGE_EVENT_MAX_BYTES:
        return payload
    message = payload.get("message")
    if not isinstance(message, dict):
        message = {}
    original_content = str(message.get("content") or "")
    original_resolution = str(message.get("resolution") or "")
    message["projection_truncated"] = True
    payload["canonical_refetch_required"] = True
    message["content"] = compact_text(original_content, max_chars=8_000) or ""
    if message["content"] != original_content:
        message["content_truncated"] = True
    if len(original_resolution) > 1_000:
        message["resolution"] = compact_text(original_resolution, max_chars=1_000)
        message["resolution_truncated"] = True
    context = message.get("context")
    if isinstance(context, dict):
        context["projection_truncated"] = True
        for key in ("attachments", "artifact_refs", "source_refs", "operator_intent", "reply_preview"):
            if _payload_size(payload) <= MESSAGE_EVENT_MAX_BYTES:
                break
            context.pop(key, None)
    if _payload_size(payload) > MESSAGE_EVENT_MAX_BYTES:
        message["context"] = {"projection_truncated": True}
    if _payload_size(payload) > MESSAGE_EVENT_MAX_BYTES:
        message["content"] = compact_text(
            message.get("content"), max_chars=_MESSAGE_EVENT_EXCERPT_MAX_CHARS
        ) or ""
        message["content_truncated"] = True
    if _payload_size(payload) > MESSAGE_EVENT_MAX_BYTES:
        # Full content/context/resolution remains available from the
        # authenticated cursor GET. Rebuild outer aliases too: legacy rows may
        # contain pathological unbounded enum/status strings.
        payload = _minimal_message_event_payload(
            payload,
            message,
            had_content=bool(original_content),
            had_resolution=bool(original_resolution),
        )
    return payload


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
        "actor_user_id": base["actor_user_id"],
        "sequence": base["sequence"],
        "surface": base["surface"],
        "handling_state": base["handling_state"],
        "handled_by_task_id": base["handled_by_task_id"],
        "reply_to_message_id": base["reply_to_message_id"],
        "status": base["status"],
        "resolution": base["resolution"],
        "created_at": base["created_at"],
        "updated_at": base["updated_at"],
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
        "project_id": base["project_id"],
        "content": base["content"],
        "type": base["message_type"],
        "from_type": base["from_type"],
        "from_task_id": base["from_task_id"],
        "actor_user_id": base["actor_user_id"],
        "sequence": base["sequence"],
        "surface": base["surface"],
        "handling_state": base["handling_state"],
        "handled_by_task_id": base["handled_by_task_id"],
        "reply_to_message_id": base["reply_to_message_id"],
        "idempotency_key": base["idempotency_key"],
        "client_idempotency_key": base["idempotency_key"],
        "status": base["status"],
        "resolution": base["resolution"],
        "convergence_link": convergence_link,
        "created_at": base["created_at"],
        "updated_at": base["updated_at"],
        "resolved_at": base["resolved_at"],
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
    context_preview = {}
    for key in context_keys:
        value = raw_context.get(key)
        if value is None:
            continue
        if key in _STRUCTURED_CONTEXT_KEYS:
            sanitized = _sanitize_structured_context(key, value)
            if sanitized is not None:
                context_preview[key] = sanitized
        else:
            context_preview[key] = compact_text(value, max_chars=context_max_chars)
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
