"""Per-operator Strategic workbench state.

The workbench state is intentionally small and stored inside the existing
MessagesNoisePrefs row so it follows the operator across devices without adding
another table for v1.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.messages_noise_prefs_store import (
    get_messages_noise_prefs_row,
    mutate_messages_noise_prefs,
)
from app.models import _utcnow

WORKBENCH_STATE_KEY = "_strategic_workbench"
WORKBENCH_STATE_MAX_BYTES = 50_000

_ALLOWED_INTERACTION_MODES = frozenset(
    {"chat", "check_in", "monitor", "continuous"}
)
_ALLOWED_ACTIVE_SURFACES = frozenset({"strategic", "pipeline", "project", "runtime"})
_MAX_SELECTED_REFS = 12


def workbench_state_size_bytes(raw: Any) -> int:
    return len(json.dumps(raw, separators=(",", ":"), default=str).encode("utf-8"))


def default_workbench_state() -> dict[str, Any]:
    return {
        "version": 1,
        "interaction_mode": "chat",
        "active_surface": "strategic",
        "selected_refs": [],
        "session_started_at": None,
        "last_interaction_at": None,
    }


def _string(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    return text[:max_chars]


def _sanitize_selected_refs(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    selected_refs: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = _string(entry.get("kind"), max_chars=40)
        ref_id = _string(entry.get("id"), max_chars=120)
        if not kind or not ref_id:
            continue
        item = {"kind": kind, "id": ref_id}
        label = _string(entry.get("label"), max_chars=160)
        if label:
            item["label"] = label
        selected_refs.append(item)
        if len(selected_refs) >= _MAX_SELECTED_REFS:
            break
    return selected_refs


def sanitize_workbench_state(
    raw: Any,
    *,
    existing: dict[str, Any] | None = None,
    touch: bool = True,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    previous = existing if isinstance(existing, dict) else {}
    now = _utcnow().isoformat()

    interaction_mode = _string(
        data.get("interaction_mode", previous.get("interaction_mode")),
        max_chars=40,
    )
    if interaction_mode not in _ALLOWED_INTERACTION_MODES:
        interaction_mode = "chat"

    active_surface = _string(
        data.get("active_surface", previous.get("active_surface")),
        max_chars=40,
    )
    if active_surface not in _ALLOWED_ACTIVE_SURFACES:
        active_surface = "strategic"

    session_started_at = _string(
        data.get("session_started_at", previous.get("session_started_at")),
        max_chars=80,
    )
    if session_started_at is None and touch:
        session_started_at = now

    last_interaction_at = _string(
        data.get("last_interaction_at", previous.get("last_interaction_at")),
        max_chars=80,
    )
    if touch:
        last_interaction_at = now

    return {
        "version": 1,
        "interaction_mode": interaction_mode,
        "active_surface": active_surface,
        "selected_refs": _sanitize_selected_refs(
            data.get("selected_refs", previous.get("selected_refs"))
        ),
        "session_started_at": session_started_at,
        "last_interaction_at": last_interaction_at,
    }


def workbench_state_from_prefs(prefs: dict[str, Any] | None) -> dict[str, Any]:
    raw = (prefs or {}).get(WORKBENCH_STATE_KEY)
    if raw is None:
        return default_workbench_state()
    return sanitize_workbench_state(raw, touch=False)


async def get_strategic_workbench_state(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    row = await get_messages_noise_prefs_row(session, user_id, project_id)
    return workbench_state_from_prefs(row.prefs if row else None)


async def upsert_strategic_workbench_state(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    raw_state: dict[str, Any],
) -> dict[str, Any]:
    state_holder: dict[str, dict[str, Any]] = {}

    def merge(existing_prefs: dict[str, Any]) -> dict[str, Any]:
        state = sanitize_workbench_state(
            raw_state,
            existing=workbench_state_from_prefs(existing_prefs),
        )
        state_holder["state"] = state
        prefs = dict(existing_prefs)
        prefs[WORKBENCH_STATE_KEY] = state
        return prefs

    await mutate_messages_noise_prefs(session, user_id, project_id, merge)
    return state_holder["state"]
