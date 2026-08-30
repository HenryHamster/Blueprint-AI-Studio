"""Exact marker and no-workspace authority for cold-successor proof tasks."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException


COLD_SUCCESSOR_CONTEXT_KEY = "_cold_successor"
COLD_SUCCESSOR_PAYLOAD_KEY = "cold_successor_contract"
COLD_SUCCESSOR_LAUNCH_MODE = "cold_successor"
RESIDENT_SESSION_KEY = "_resident_strategic_session"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COLD_SUCCESSOR_AGENT_PATH_RE = re.compile(
    r"^/api/agent/projects/"
    r"(?P<project_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/strategic-session/"
    r"cold-successor/(?P<action>heartbeat|attest|receipt|terminal)$"
)
_COLD_SUCCESSOR_AGENT_METHODS = {
    "heartbeat": "POST",
    "attest": "POST",
    "receipt": "GET",
    "terminal": "POST",
}


def cold_successor_marker_state(task: Any) -> str:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    context = task.context if isinstance(task.context, dict) else {}
    payload_has_marker = COLD_SUCCESSOR_PAYLOAD_KEY in payload
    context_has_marker = COLD_SUCCESSOR_CONTEXT_KEY in context
    if not payload_has_marker and not context_has_marker:
        return "ordinary"
    contract = payload.get(COLD_SUCCESSOR_PAYLOAD_KEY)
    manifest = contract.get("manifest") if isinstance(contract, dict) else None
    core_section = manifest.get("core") if isinstance(manifest, dict) else None
    try:
        generation = int(context.get("_resident_worker_generation"))
    except (TypeError, ValueError):
        generation = 0
    exact = bool(
        payload.get("type") == "strategic_review"
        and isinstance(contract, dict)
        and isinstance(contract.get("core"), dict)
        and isinstance(manifest, dict)
        and isinstance(core_section, dict)
        and core_section.get("sha256") == context.get("_cold_successor_core_sha256")
        and context.get(COLD_SUCCESSOR_CONTEXT_KEY) is True
        and context.get("_resident_session") is True
        and context.get("_resident_infrastructure") is True
        and isinstance(context.get("strategic_session_id"), str)
        and context["strategic_session_id"]
        and generation >= 1
        and isinstance(context.get("runner_target_id"), str)
        and context["runner_target_id"]
        and context.get("preferred_runner_target_id") == context["runner_target_id"]
        and isinstance(context.get("_cold_successor_core_sha256"), str)
        and _SHA256_RE.fullmatch(context["_cold_successor_core_sha256"])
        and isinstance(context.get("_cold_successor_idempotency_fingerprint"), str)
        and _SHA256_RE.fullmatch(
            context["_cold_successor_idempotency_fingerprint"]
        )
        and isinstance(context.get("_cold_successor_launch_effect_sha256"), str)
        and _SHA256_RE.fullmatch(context["_cold_successor_launch_effect_sha256"])
        and str(getattr(task, "vm_instance_id", ""))
        == f"runner:{context['runner_target_id']}"
    )
    return "exact" if exact else "malformed"


def has_exact_cold_successor_markers(task: Any) -> bool:
    return cold_successor_marker_state(task) == "exact"


def is_exact_cold_successor_task(task: Any) -> bool:
    """Return proof membership, rejecting every one-sided or malformed marker."""
    state = cold_successor_marker_state(task)
    if state == "ordinary":
        return False
    if state != "exact":
        raise HTTPException(
            status_code=409,
            detail="Cold successor task markers are incomplete or malformed",
        )
    return True


def enforce_cold_successor_request_authority(
    task: Any,
    *,
    method: str,
    path: str,
) -> bool:
    """Fence a cold proof JWT to its minimal boot and receipt protocol.

    The decision is derived only from persisted task markers after the normal
    token-to-task/project/VM binding.  Exact cold tasks may claim, refresh their
    short-lived JWT, and call only the four dedicated lifecycle actions for
    their own project.  A malformed one-sided marker may reach only ``/claim``
    so the canonical admission check can reject it without transitioning the
    task.  Ordinary tasks retain the existing API surface.
    """

    marker_state = cold_successor_marker_state(task)
    if marker_state == "ordinary":
        return False
    normalized_method = str(method or "").upper()
    if normalized_method == "POST" and path == "/api/agent/claim":
        return True
    if marker_state != "exact":
        raise HTTPException(
            status_code=403,
            detail="Cold successor task path is not allowed",
        )
    if normalized_method == "POST" and path == "/api/agent/refresh-token":
        return True
    match = _COLD_SUCCESSOR_AGENT_PATH_RE.fullmatch(path)
    if (
        match is not None
        and _COLD_SUCCESSOR_AGENT_METHODS.get(match.group("action"))
        == normalized_method
        and match.group("project_id").lower() == str(task.project_id).lower()
    ):
        return True
    raise HTTPException(
        status_code=403,
        detail="Cold successor task path is not allowed",
    )


def is_active_cold_successor_blob(blob: Any) -> bool:
    return bool(
        isinstance(blob, dict)
        and blob.get("launch_mode") == COLD_SUCCESSOR_LAUNCH_MODE
        and blob.get("status") == "active"
        and blob.get("stop_requested") is not True
        and blob.get("task_id")
    )


def active_cold_successor_blob(vision: Any) -> dict[str, Any] | None:
    blob = vision.get(RESIDENT_SESSION_KEY) if isinstance(vision, dict) else None
    return blob if is_active_cold_successor_blob(blob) else None


def cold_successor_blob_matches_task(blob: Any, task: Any) -> bool:
    context = task.context if isinstance(task.context, dict) else {}
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    contract = payload.get(COLD_SUCCESSOR_PAYLOAD_KEY)
    manifest = contract.get("manifest") if isinstance(contract, dict) else None
    core_section = manifest.get("core") if isinstance(manifest, dict) else None
    try:
        generation = int(blob.get("worker_generation") or 1)
        context_generation = int(context.get("_resident_worker_generation") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(
        isinstance(blob, dict)
        and blob.get("launch_mode") == COLD_SUCCESSOR_LAUNCH_MODE
        and blob.get("status") in {"active", "ended"}
        and has_exact_cold_successor_markers(task)
        and str(blob.get("task_id")) == str(task.id)
        and str(blob.get("session_id")) == str(context.get("strategic_session_id"))
        and generation == context_generation
        and blob.get("core_sha256") == context.get("_cold_successor_core_sha256")
        and isinstance(core_section, dict)
        and core_section.get("sha256") == blob.get("core_sha256")
        and blob.get("idempotency_fingerprint")
        == context.get("_cold_successor_idempotency_fingerprint")
        and blob.get("launch_effect_sha256")
        == context.get("_cold_successor_launch_effect_sha256")
        and str(blob.get("runner_id")) == str(context.get("runner_target_id"))
        and str(context.get("preferred_runner_target_id"))
        == str(context.get("runner_target_id"))
        and str(getattr(task, "vm_instance_id", ""))
        == f"runner:{context.get('runner_target_id')}"
    )


def cold_successor_blob_owns_task(blob: Any, task: Any) -> bool:
    return is_active_cold_successor_blob(blob) and cold_successor_blob_matches_task(
        blob, task
    )


def enforce_cold_successor_task_authority(vision: Any, task: Any) -> bool:
    """Admit ordinary work or the one exact proof owner; reject every other mix."""
    state = cold_successor_marker_state(task)
    blob = active_cold_successor_blob(vision)
    if state == "ordinary" and blob is None:
        return False
    if state == "exact" and blob is not None and cold_successor_blob_owns_task(
        blob, task
    ):
        return True
    raise HTTPException(
        status_code=409,
        detail="Cold successor task authority is absent, malformed, or superseded",
    )


def reject_active_cold_successor(vision: Any) -> None:
    if active_cold_successor_blob(vision) is not None:
        raise HTTPException(
            status_code=409,
            detail="Cold successor proof has exclusive strategic authority",
        )


def reject_cold_successor_repository_authority(task: Any) -> None:
    """Deny every cold-marker task access to repository credentials/proxies."""

    if cold_successor_marker_state(task) != "ordinary":
        raise HTTPException(
            status_code=403,
            detail="Cold successor tasks have no repository authority",
        )


def cold_successor_capability_slice(
    task: Any,
    capability: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Copy the static capability and remove repo authority only for exact proof."""
    if not is_exact_cold_successor_task(task):
        return capability
    if capability is None:
        raise HTTPException(status_code=409, detail="Cold successor capability is missing")
    return {**capability, "workspace_requirement": "none"}


def cold_successor_workspace_requirement(task: Any, default: str) -> str:
    return "none" if is_exact_cold_successor_task(task) else default
