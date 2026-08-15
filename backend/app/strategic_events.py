"""Realtime websocket events for strategist STATE & OUTPUTS.

Centralizes the three event-name contracts the admin UI depends on so every
backend change site emits a consistent trigger-a-refetch signal. Payloads are
intentionally tiny (ids / labels only) — the frontend refetches full state over
HTTP, so every emission stays well under the ~7KB cross-instance NOTIFY ceiling
enforced by event_fanout.py.

All emitters MUST be awaited AFTER the DB commit, never while holding a
directions/project lock, mirroring the established ``task_updated`` /
``alert_created`` post-commit broadcast convention. ``broadcast_to_project``
swallows its own exceptions, so a failed emit never escapes into the calling
HTTP handler after its commit.

These event names are a contract with the frontend — do not rename without a
coordinated admin-ui change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from app.broadcast import _sanitize_log, broadcast_to_project
from app.workbench_events import (
    SURFACE_DIRECTIONS,
    SURFACE_STRATEGIC_SESSION,
    workbench_event_envelope,
)

# Event-name contract shared with the admin UI.
STRATEGIC_CYCLE_STARTED = "strategic_cycle_started"
STRATEGIC_CYCLE_COMPLETED = "strategic_cycle_completed"
STRATEGIC_UPDATED = "strategic_updated"
# Strategic-session live transcript delta. Each
# emission is one coarsened per-sentence / per-turn chunk streamed to the operator
# chat. Contract with admin-ui/src/hooks/useLogViewer.ts — do not rename.
STRATEGIC_SESSION = "strategic_session"
# Durable resident activity/actability transition. Unlike transcript deltas,
# this carries no prose stream; it is a scoped refetch trigger emitted only when
# normalized semantic activity fields change.
STRATEGIC_SESSION_STATE = "strategic_session_state"

# Allowed ``kind`` values for a ``strategic_session`` delta. Scopes what the
# strategic agent may stream: chunk of reasoning, an applied strategic action, a
# tool call, a lifecycle status line, or an assistant message. The five
# "activity" kinds (command / file / read / subagent / tool_result) are the
# compact one-liners the handler emits for the LIVE, ephemeral activity
# strip (a Bash command, a file edit, a read, a spawned sub-agent, a tool
# result); they are never persisted (only ``message`` reaches the durable
# transcript, and only as a ProjectMessage — never via this event).
STRATEGIC_SESSION_KIND_REASONING = "reasoning"
STRATEGIC_SESSION_KIND_ACTION = "action"
STRATEGIC_SESSION_KIND_TOOL = "tool"
STRATEGIC_SESSION_KIND_STATUS = "status"
STRATEGIC_SESSION_KIND_MESSAGE = "message"
STRATEGIC_SESSION_KIND_COMMAND = "command"
STRATEGIC_SESSION_KIND_FILE = "file"
STRATEGIC_SESSION_KIND_READ = "read"
STRATEGIC_SESSION_KIND_SUBAGENT = "subagent"
STRATEGIC_SESSION_KIND_TOOL_RESULT = "tool_result"
_STRATEGIC_SESSION_KINDS = frozenset(
    {
        STRATEGIC_SESSION_KIND_REASONING,
        STRATEGIC_SESSION_KIND_ACTION,
        STRATEGIC_SESSION_KIND_TOOL,
        STRATEGIC_SESSION_KIND_STATUS,
        STRATEGIC_SESSION_KIND_MESSAGE,
        STRATEGIC_SESSION_KIND_COMMAND,
        STRATEGIC_SESSION_KIND_FILE,
        STRATEGIC_SESSION_KIND_READ,
        STRATEGIC_SESSION_KIND_SUBAGENT,
        STRATEGIC_SESSION_KIND_TOOL_RESULT,
    }
)

# Optional lifecycle ``status`` on a ``strategic_session`` delta: ``responding``
# marks the start of an OPERATOR turn (drives the "Workday's on it" indicator),
# ``working`` marks the start of an AUTONOMOUS turn (advancing/watching indicator),
# ``done`` marks the end of any turn (clears the ephemeral activity strip). Absent
# on ordinary text/activity deltas. NEVER persisted — purely a live signal.
STRATEGIC_SESSION_STATUS_RESPONDING = "responding"
STRATEGIC_SESSION_STATUS_WORKING = "working"
STRATEGIC_SESSION_STATUS_DONE = "done"
_STRATEGIC_SESSION_STATUSES = frozenset(
    {
        STRATEGIC_SESSION_STATUS_RESPONDING,
        STRATEGIC_SESSION_STATUS_WORKING,
        STRATEGIC_SESSION_STATUS_DONE,
    }
)
# Per-delta text cap (chars). Kept below the NOTIFY soft-limit so a single
# coarsened chunk rides the payload; the byte-level enforcer is the backstop.
_STRATEGIC_SESSION_TEXT_MAX_CHARS = 5_000

# Allowed ``kind`` values for a ``strategic_updated`` event. Scopes the signal
# to the strategist's persisted OUTPUTS surfaced in the directions view.
STRATEGIC_KIND_DIRECTIONS = "directions"
STRATEGIC_KIND_RUNNING_UPDATE = "running_update"
STRATEGIC_KIND_PROBLEM_MODEL = "problem_model"

_STEERING_EXCERPT_MAX_CHARS = 240
_STATUS_MAX_CHARS = 160
_ID_LIST_MAX = 25
_NOTIFY_PAYLOAD_SOFT_LIMIT = 6_000
_STRATEGIC_REFETCH_SURFACES = (SURFACE_STRATEGIC_SESSION, SURFACE_DIRECTIONS)

_PHASE_LABELS = {
    1: "Architecture review",
    2: "Strategic review",
}

_PHASE_STATES = {
    ("started", 1): "phase1_running",
    ("started", 2): "phase2_running",
    ("completed", 1): "phase1_done",
    ("completed", 2): "cycle_done",
}


def _compact_str(value: Any, *, max_chars: int = _STATUS_MAX_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _compact_ids(values: Sequence[Any] | None) -> list[str]:
    if not values:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact_str(value, max_chars=80)
        if text and text not in seen:
            ids.append(text)
            seen.add(text)
        if len(ids) >= _ID_LIST_MAX:
            break
    return ids


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_envelope(
    project_id: Any,
    *,
    resource: str,
    action: str,
    refetch_surfaces: Sequence[str] = _STRATEGIC_REFETCH_SURFACES,
) -> dict[str, Any]:
    return workbench_event_envelope(
        project_id,
        resource=resource,
        action=action,
        refetch_surfaces=refetch_surfaces,
    )


def _phase_payload(
    project_id: Any,
    task_id: Any,
    phase: Any,
    *,
    event: str,
    cycle_id: Any = None,
    phase_label: str | None = None,
    phase_state: str | None = None,
    steering_origin: str | None = None,
    steering_excerpt: str | None = None,
    completion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task_id_str = str(task_id)
    payload: dict[str, Any] = {
        "task_id": task_id_str,
        "phase": phase,
        **_event_envelope(
            project_id,
            resource="strategic_cycle",
            action=event,
        ),
    }
    strategic_summary: dict[str, Any] = {
        "kind": "cycle",
        "task_id": task_id_str,
        "phase": phase,
    }
    phase_num = _as_int(phase, default=0)
    if cycle_id:
        cycle_id_str = str(cycle_id)
        payload["cycle_id"] = cycle_id_str
        strategic_summary["cycle_id"] = cycle_id_str
    if phase_num:
        payload["phase_number"] = phase_num
        payload["phase_label"] = phase_label or _PHASE_LABELS.get(
            phase_num, f"Phase {phase_num}"
        )
        state = phase_state or _PHASE_STATES.get((event, phase_num))
        if state:
            payload["phase_state"] = state
    origin = _compact_str(steering_origin, max_chars=40)
    if origin:
        payload["steering_origin"] = origin
    excerpt = _compact_str(steering_excerpt, max_chars=_STEERING_EXCERPT_MAX_CHARS)
    if excerpt:
        payload["steering_excerpt"] = excerpt
    if completion:
        payload["completion"] = _completion_payload(completion)
    payload["strategic"] = strategic_summary
    return _enforce_soft_limit(payload)


def _output_payload(
    project_id: Any,
    kind: str,
    *,
    task_id: Any = None,
    cycle_id: Any = None,
    output_kind: str | None = None,
    op: str | None = None,
    changed_direction_ids: Sequence[Any] | None = None,
    changed_count: int | None = None,
    problem_model_changed: bool | None = None,
    running_update_changed: bool | None = None,
) -> dict[str, Any]:
    direction_ids = _compact_ids(changed_direction_ids)
    action = _compact_str(op, max_chars=80) or "updated"
    payload: dict[str, Any] = {
        "kind": kind,
        **_event_envelope(
            project_id,
            resource="strategic_output",
            action=action,
        ),
    }
    strategic_summary: dict[str, Any] = {
        "kind": kind,
        "output_kind": output_kind or kind,
    }
    if task_id:
        task_id_str = str(task_id)
        payload["task_id"] = task_id_str
        strategic_summary["task_id"] = task_id_str
    if cycle_id:
        cycle_id_str = str(cycle_id)
        payload["cycle_id"] = cycle_id_str
        strategic_summary["cycle_id"] = cycle_id_str
    if output_kind:
        payload["output_kind"] = output_kind
    if op:
        payload["op"] = op
    if direction_ids:
        payload["changed_direction_ids"] = direction_ids
    if changed_count is not None or direction_ids:
        payload["changed_count"] = max(0, _as_int(changed_count))
        if changed_count is None:
            payload["changed_count"] = len(direction_ids)
        strategic_summary["changed_count"] = payload["changed_count"]
    if problem_model_changed is not None:
        payload["problem_model_changed"] = bool(problem_model_changed)
    if running_update_changed is not None:
        payload["running_update_changed"] = bool(running_update_changed)
    payload["strategic"] = strategic_summary
    return _enforce_soft_limit(payload)


def _completion_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    created_ids = _compact_ids(summary.get("created_direction_ids"))
    retired_ids = _compact_ids(summary.get("retired_direction_ids"))
    staged_ids = _compact_ids(summary.get("staged_direction_ids"))
    updated_ids = _compact_ids(summary.get("updated_direction_ids"))
    directions_changed = bool(
        summary.get("directions_changed")
        or created_ids
        or retired_ids
        or staged_ids
        or updated_ids
        or _as_int(summary.get("created_count"))
        or _as_int(summary.get("retired_count"))
        or _as_int(summary.get("staged_count"))
        or _as_int(summary.get("updated_count"))
    )
    payload: dict[str, Any] = {
        "status": _compact_str(summary.get("status"), max_chars=40) or "completed",
        "created_count": _as_int(
            summary.get("created_count"), default=len(created_ids)
        ),
        "retired_count": _as_int(
            summary.get("retired_count"), default=len(retired_ids)
        ),
        "staged_count": _as_int(summary.get("staged_count"), default=len(staged_ids)),
        "updated_count": _as_int(
            summary.get("updated_count"), default=len(updated_ids)
        ),
        "tasks_spawned": _as_int(summary.get("tasks_spawned")),
        "directions_changed": directions_changed,
        "problem_model_changed": bool(summary.get("problem_model_changed")),
        "running_update_changed": bool(summary.get("running_update_changed")),
    }
    if created_ids:
        payload["created_direction_ids"] = created_ids
    if retired_ids:
        payload["retired_direction_ids"] = retired_ids
    if staged_ids:
        payload["staged_direction_ids"] = staged_ids
    if updated_ids:
        payload["updated_direction_ids"] = updated_ids
    status_message = _compact_str(summary.get("status_message"))
    if status_message:
        payload["status_message"] = status_message
    failure_reason = _compact_str(summary.get("failure_reason"))
    if failure_reason:
        payload["failure_reason"] = failure_reason
    # Compact strategic-judgment-challenge summary (mode/verdict/warned/blocked).
    # Built upstream by strategic_completion._strategic_event_completion_summary;
    # forwarded verbatim here as four scalar fields so the full bounded item list
    # never rides the websocket payload. Absent for legacy cycles with no
    # challenge record, keeping this an additive, backward-compatible key.
    challenge = summary.get("strategic_judgment_challenge")
    if isinstance(challenge, dict):
        payload["strategic_judgment_challenge"] = challenge
    return payload


def _enforce_soft_limit(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep websocket/NOTIFY payloads compact even when callers pass long data."""
    encoded = json.dumps(payload, default=str).encode("utf-8")
    if len(encoded) <= _NOTIFY_PAYLOAD_SOFT_LIMIT:
        return payload
    trimmed = dict(payload)
    completion = trimmed.get("completion")
    if isinstance(completion, dict):
        completion = dict(completion)
        for key in (
            "created_direction_ids",
            "retired_direction_ids",
            "staged_direction_ids",
            "updated_direction_ids",
        ):
            if key in completion:
                completion[key] = completion[key][:_ID_LIST_MAX]
        trimmed["completion"] = completion
    if "steering_excerpt" in trimmed:
        trimmed["steering_excerpt"] = _compact_str(
            trimmed["steering_excerpt"], max_chars=120
        )
    return trimmed


async def emit_strategic_cycle_started(
    project_id,
    task_id,
    phase,
    *,
    cycle_id=None,
    phase_label: str | None = None,
    phase_state: str | None = None,
    steering_origin: str | None = None,
    steering_excerpt: str | None = None,
) -> None:
    """Broadcast that a strategic cycle task became active (post-commit)."""
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_CYCLE_STARTED,
        _phase_payload(
            project_id,
            task_id,
            phase,
            event="started",
            cycle_id=cycle_id,
            phase_label=phase_label,
            phase_state=phase_state,
            steering_origin=steering_origin,
            steering_excerpt=steering_excerpt,
        ),
    )


async def emit_strategic_cycle_completed(
    project_id,
    task_id,
    phase,
    *,
    cycle_id=None,
    phase_label: str | None = None,
    phase_state: str | None = None,
    completion: Mapping[str, Any] | None = None,
) -> None:
    """Broadcast that a strategic cycle finished — success OR failure (post-commit)."""
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_CYCLE_COMPLETED,
        _phase_payload(
            project_id,
            task_id,
            phase,
            event="completed",
            cycle_id=cycle_id,
            phase_label=phase_label,
            phase_state=phase_state,
            completion=completion,
        ),
    )


def _session_ts_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_session_text(
    value: Any, *, max_chars: int = _STRATEGIC_SESSION_TEXT_MAX_CHARS
) -> str:
    """Redact secrets from a session-delta chunk and cap its length.

    Runs the same secret-redaction pass the WS log stream uses
    (``broadcast._sanitize_log``) so a tool echo or reasoning chunk can never
    leak a token into the operator transcript, then bounds the length.
    """
    if value is None:
        return ""
    text = _sanitize_log(str(value))
    if len(text) > max_chars:
        return f"{text[: max_chars - 1]}…"
    return text


def _enforce_session_soft_limit(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the ``text`` field until the encoded payload fits the NOTIFY ceiling.

    ``text`` is the only unbounded-by-caller field; every other key is a short
    id / scalar. Trims iteratively (each pass strictly shrinks ``text``) so the
    loop always terminates even for multibyte content.
    """
    trimmed = dict(payload)
    while True:
        encoded = json.dumps(trimmed, default=str).encode("utf-8")
        if len(encoded) <= _NOTIFY_PAYLOAD_SOFT_LIMIT:
            return trimmed
        text = trimmed.get("text") or ""
        if not text:
            # Nothing left to shed; remaining fields are already tiny + bounded.
            return trimmed
        overflow = len(encoded) - _NOTIFY_PAYLOAD_SOFT_LIMIT
        # chars≈bytes for ASCII; for multibyte we drop >= overflow chars, which
        # sheds even more bytes — still converges. Keep a little headroom.
        keep = min(len(text) - 1, max(0, len(text) - overflow - 32))
        trimmed["text"] = f"{text[:keep]}…" if keep > 0 else ""


def _strategic_session_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    src = payload if isinstance(payload, Mapping) else {}
    kind = src.get("kind")
    if kind not in _STRATEGIC_SESSION_KINDS:
        kind = STRATEGIC_SESSION_KIND_STATUS
    out: dict[str, Any] = {
        "session_id": _compact_str(src.get("session_id"), max_chars=80),
        "task_id": _compact_str(src.get("task_id"), max_chars=80),
        "seq": _as_int(src.get("seq")),
        "kind": kind,
        "text": _sanitize_session_text(src.get("text")),
        "ts": _compact_str(src.get("ts"), max_chars=40) or _session_ts_now(),
    }
    # Optional lifecycle status (``responding`` / ``done``). Only forwarded when it
    # is one of the known values so a malformed frame can never inject arbitrary
    # state onto the wire; absent otherwise so ordinary deltas stay lean.
    status = src.get("status")
    if status in _STRATEGIC_SESSION_STATUSES:
        out["status"] = status
    return _enforce_session_soft_limit(out)


async def emit_strategic_session_delta(
    project_id,
    payload: Mapping[str, Any] | None,
) -> None:
    """Broadcast one strategic-session transcript delta (post-commit).

    ``payload`` carries ``{ session_id, task_id, seq, kind, text, ts }`` where
    ``kind`` is one of reasoning / action / tool / status / message. ``text`` is
    secret-sanitized via ``broadcast._sanitize_log`` and the whole payload is
    bounded under the NOTIFY soft-limit before it goes on the wire. Coarsen to
    per-sentence / per-turn upstream — one emission == one visible chunk.

    Like the other ``emit_strategic_*`` helpers this MUST be awaited AFTER the
    DB commit; ``broadcast_to_project`` swallows its own exceptions so a failed
    emit never escapes the caller.
    """
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_SESSION,
        _strategic_session_payload(payload),
    )


async def emit_strategic_session_state_updated(
    project_id: Any,
    *,
    task_id: Any,
    activity: Mapping[str, Any] | None,
) -> None:
    """Broadcast one durable Resident activity transition after commit."""
    source = activity if isinstance(activity, Mapping) else {}
    semantic = {
        key: source.get(key)
        for key in (
            "phase",
            "reason_code",
            "consultation_id",
            "autonomy_state",
            "autonomy_reason_code",
        )
    }
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_SESSION_STATE,
        {
            **_event_envelope(
                project_id,
                resource="strategic_session",
                action="activity_updated",
                refetch_surfaces=(SURFACE_STRATEGIC_SESSION,),
            ),
            "task_id": _compact_str(task_id, max_chars=80),
            "activity": semantic,
        },
    )


async def emit_strategic_updated(
    project_id,
    kind,
    *,
    task_id=None,
    cycle_id=None,
    output_kind: str | None = None,
    op: str | None = None,
    changed_direction_ids: Sequence[Any] | None = None,
    changed_count: int | None = None,
    problem_model_changed: bool | None = None,
    running_update_changed: bool | None = None,
) -> None:
    """Broadcast that a strategist output changed (post-commit).

    ``kind`` is one of ``directions`` / ``running_update`` / ``problem_model``.
    """
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_UPDATED,
        _output_payload(
            project_id,
            kind,
            task_id=task_id,
            cycle_id=cycle_id,
            output_kind=output_kind,
            op=op,
            changed_direction_ids=changed_direction_ids,
            changed_count=changed_count,
            problem_model_changed=problem_model_changed,
            running_update_changed=running_update_changed,
        ),
    )
