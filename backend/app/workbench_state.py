"""Per-operator Strategic workbench state.

The workbench state is intentionally small and stored inside the existing
MessagesNoisePrefs row so it follows the operator across devices without adding
another table for v1.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.messages_noise_prefs_store import (
    get_messages_noise_prefs_row,
)
from app.models import LocalRunner, MessagesNoisePrefs, _utcnow
from app.runner_project_binding import runner_allowed_for_project
from app.strategic_trigger import (
    ARMING_NOT_ARMED,
    ARMING_PASSIVE_NOTE,
    ARMING_RESIDENT_SESSION,
    ARMING_STEERING,
    ORIGIN_OPERATOR,
    request_strategic_refresh,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_locking import with_vision_locked

WORKBENCH_STATE_KEY = "_strategic_workbench"
STRATEGIC_LOCAL_CONTINUATION_KEY = "_strategic_local_continuation"
STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY = "strategic_local_runner_required"
STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY = "strategic_local_runner_reason"
STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY = "awaiting_strategic_local_runner_since"
WORKBENCH_STATE_MAX_BYTES = 50_000
SESSION_IDLE_TTL_SECONDS = 14_400  # 4 hours
# Default freshness TTL (seconds) for the local-Strategic continuation marker.
# This is only the FALLBACK default — the live value comes from the
# ``strategic_continuation_ttl_seconds`` runtime setting (see
# ``_strategic_local_continuation_ttl_seconds``). Replaces the old hardcoded 12h
# (43200): the marker's freshness now renews on operator interaction OR an
# applied strategic op (progress), NOT bare runner liveness, so an
# opened-and-abandoned continuous local session self-heals within this short
# window instead of pinning the project locally for 12h.
STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS = 1_500  # 25 min lease window (default)
STRATEGIC_LOCAL_CONTINUATION_RENEWAL_MIN_SECONDS = 60


def _strategic_local_continuation_ttl_seconds() -> int:
    """Live TTL (seconds) for local-Strategic continuation freshness.

    Reads the ``strategic_continuation_ttl_seconds`` runtime setting, falling
    back to ``STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS`` when unset or malformed.
    Imported lazily to avoid an import-time cycle with ``app.shared_state``.
    """

    try:
        from app.shared_state import runtime_config

        value = int(
            runtime_config.get(
                "strategic_continuation_ttl_seconds",
                STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
            )
        )
    except (ImportError, TypeError, ValueError):
        return STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS
    return value if value > 0 else STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS

# The strategic interaction vocabulary collapsed to two modes: ``chat`` and
# ``continuous`` ("Workday"). The retired ``check_in`` / ``monitor`` modes are no
# longer accepted going forward — a legacy value read off old data (or a stale
# client) is tolerated by coercing it to ``chat`` (never a crash). The 0367
# migration rewrites any persisted ``check_in`` / ``monitor`` to ``chat``.
_ALLOWED_INTERACTION_MODES = frozenset({"chat", "continuous"})
_ALLOWED_ACTIVE_SURFACES = frozenset({"strategic", "pipeline", "project", "runtime"})
_MAX_SELECTED_REFS = 12
_MAX_SELECTED_REF_DESCRIPTORS = 24
# Only "Workday" (``continuous``) arms the strategic refresh on entry now that
# ``check_in`` is retired (it was the other arming mode).
_STRATEGIC_ARMING_INTERACTION_MODES = frozenset({"continuous"})
_OPERATOR_CONTEXT_TARGET_KEYS = (
    "surface",
    "section",
    "project_id",
    "task_id",
    "message_id",
    "direction_id",
    "alert_id",
    "action",
    "document_id",
    "artifact_id",
    "runner_id",
    "cycle_id",
)


@dataclass(frozen=True)
class StrategicWorkbenchWriteResult:
    previous_state: dict[str, Any]
    state: dict[str, Any]
    arming: str = ARMING_PASSIVE_NOTE


def workbench_state_size_bytes(raw: Any) -> int:
    return len(json.dumps(raw, separators=(",", ":"), default=str).encode("utf-8"))


def workbench_state_exceeds_max_bytes(raw: Any) -> bool:
    return workbench_state_size_bytes(raw) > WORKBENCH_STATE_MAX_BYTES


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
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = _string(entry.get("kind"), max_chars=40)
        ref_id = _string(entry.get("id"), max_chars=120)
        if not kind or not ref_id:
            continue
        key = (kind, ref_id)
        if key in seen:
            continue
        seen.add(key)
        item = {"kind": kind, "id": ref_id}
        label = _string(entry.get("label"), max_chars=160)
        if label:
            item["label"] = label
        selected_refs.append(item)
        if len(selected_refs) >= _MAX_SELECTED_REFS:
            break
    return selected_refs


def _sanitize_context_target(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    target: dict[str, str] = {}
    for key in _OPERATOR_CONTEXT_TARGET_KEYS:
        value = _string(raw.get(key), max_chars=180)
        if value:
            target[key] = value
    return target or None


def _runner_handles_any_strategic_work(capabilities: Any) -> bool:
    caps = capabilities if isinstance(capabilities, dict) else {}
    task_types = caps.get("task_types", "all")
    if task_types == "all":
        return True
    if not isinstance(task_types, list):
        return False
    return any(
        isinstance(task_type, str) and task_type in STRATEGIC_TASK_TYPES
        for task_type in task_types
    )


def _message_ref_target(ref_id: str) -> dict[str, str]:
    message_id = ref_id.removeprefix("message:")
    return {"surface": "messages", "message_id": message_id}


def _sanitize_ref_descriptor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = _string(raw.get("kind"), max_chars=40)
    ref_id = _string(raw.get("id"), max_chars=160)
    if not kind or not ref_id:
        return None
    canonical_kind = kind.lower()

    descriptor: dict[str, Any] = {"kind": kind, "id": ref_id}
    # Client-supplied labels for message refs can contain copied message bodies.
    # Durable snapshots keep the pointer and let read projections resolve labels.
    label = (
        None if canonical_kind == "message" else _string(raw.get("label"), max_chars=180)
    )
    if label:
        descriptor["label"] = label
    # Message descriptor summaries are usually compacted message bodies. Keep
    # message refs navigable, but do not make body text durable hidden context.
    summary = (
        None
        if canonical_kind == "message"
        else _string(raw.get("summary"), max_chars=220)
    )
    if summary:
        descriptor["summary"] = summary
    status = (
        None if canonical_kind == "message" else _string(raw.get("status"), max_chars=80)
    )
    if status:
        descriptor["status"] = status
    target = (
        _message_ref_target(ref_id)
        if canonical_kind == "message"
        else _sanitize_context_target(raw.get("target"))
    )
    if target:
        descriptor["target"] = target
    return descriptor


def _sanitize_ref_descriptors(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    descriptors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        descriptor = _sanitize_ref_descriptor(entry)
        if not descriptor:
            continue
        key = (descriptor["kind"], descriptor["id"])
        if key in seen:
            continue
        seen.add(key)
        descriptors.append(descriptor)
        if len(descriptors) >= _MAX_SELECTED_REF_DESCRIPTORS:
            break
    return descriptors


def _sanitize_context_package_descriptor(raw: Any) -> dict[str, Any] | None:
    descriptor = _sanitize_ref_descriptor(raw)
    if descriptor:
        return descriptor
    if not isinstance(raw, dict):
        return None
    package_id = _string(
        raw.get("id") or raw.get("context_package_id"),
        max_chars=180,
    )
    if not package_id:
        return None
    result: dict[str, Any] = {"id": package_id}
    label = _string(raw.get("label") or raw.get("title"), max_chars=180)
    if label:
        result["label"] = label
    summary = _string(raw.get("summary"), max_chars=220)
    if summary:
        result["summary"] = summary
    status = _string(raw.get("status"), max_chars=80)
    if status:
        result["status"] = status
    return result


def sanitize_strategic_session_snapshot(
    raw: Any,
    *,
    fallback: dict[str, Any] | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    previous = fallback if isinstance(fallback, dict) else {}

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
    context_package_id = _string(
        data.get("context_package_id", previous.get("context_package_id")),
        max_chars=160,
    )
    snapshot: dict[str, Any] = {
        "version": 1,
        "interaction_mode": interaction_mode,
        "active_surface": active_surface,
        "session_started_at": session_started_at,
        "context_package_id": context_package_id,
        "selected_refs": _sanitize_selected_refs(
            data.get("selected_refs", previous.get("selected_refs"))
        ),
    }
    selected_ref_descriptors = _sanitize_ref_descriptors(
        data.get(
            "selected_ref_descriptors",
            previous.get("selected_ref_descriptors"),
        )
    )
    if selected_ref_descriptors:
        snapshot["selected_ref_descriptors"] = selected_ref_descriptors
    context_package_descriptor = _sanitize_context_package_descriptor(
        data.get("context_package_descriptor")
        or data.get("context_package")
        or previous.get("context_package_descriptor")
        or previous.get("context_package")
    )
    if context_package_descriptor:
        snapshot["context_package_descriptor"] = context_package_descriptor
    stored_captured_at = _string(data.get("captured_at"), max_chars=80)
    effective_captured_at = captured_at or stored_captured_at
    if effective_captured_at:
        snapshot["captured_at"] = effective_captured_at
    return snapshot


def strategic_session_snapshot_has_signal(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot.get("selected_refs")
        or snapshot.get("interaction_mode") not in (None, "chat")
        or snapshot.get("active_surface") not in (None, "strategic")
        or snapshot.get("session_started_at")
        or snapshot.get("context_package_id")
        or snapshot.get("selected_ref_descriptors")
        or snapshot.get("context_package_descriptor")
    )


async def build_message_strategic_session_snapshot(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    workbench_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    fallback_workbench = None
    if workbench_context is None:
        fallback_workbench = await get_strategic_workbench_state(
            session,
            user_id,
            project_id,
        )
    snapshot = sanitize_strategic_session_snapshot(
        workbench_context if workbench_context is not None else fallback_workbench,
        fallback=fallback_workbench,
        captured_at=_utcnow().isoformat(),
    )
    if workbench_context is not None or strategic_session_snapshot_has_signal(snapshot):
        return snapshot
    return None


def sanitize_workbench_state(
    raw: Any,
    *,
    existing: dict[str, Any] | None = None,
    touch: bool = True,
    valid_preferred_runner_ids: set[str] | None = None,
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

    state: dict[str, Any] = {
        "version": 1,
        "interaction_mode": interaction_mode,
        "active_surface": active_surface,
        "selected_refs": _sanitize_selected_refs(
            data.get("selected_refs", previous.get("selected_refs"))
        ),
        "session_started_at": session_started_at,
        "last_interaction_at": last_interaction_at,
    }
    preferred_runner_raw = (
        data["preferred_runner_id"]
        if "preferred_runner_id" in data
        else previous.get("preferred_runner_id")
    )
    preferred_runner_id = _string(preferred_runner_raw, max_chars=80)
    if (
        preferred_runner_id
        and valid_preferred_runner_ids is not None
        and preferred_runner_id not in valid_preferred_runner_ids
    ):
        preferred_runner_id = None
    if preferred_runner_id:
        state["preferred_runner_id"] = preferred_runner_id
    return state


def _effective_last_active(state: dict[str, Any]) -> str | None:
    """Return the best freshness timestamp for TTL purposes.

    Prefers last_interaction_at (written on every save); falls back to
    session_started_at for legacy rows where the interaction field was never
    populated.
    """
    return state.get("last_interaction_at") or state.get("session_started_at")


def _workbench_session_expired(state: dict[str, Any]) -> bool:
    """Return True when the session has been idle longer than SESSION_IDLE_TTL_SECONDS."""
    ts = _effective_last_active(state)
    if not ts:
        return False
    try:
        last_active = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    elapsed = (_utcnow() - last_active).total_seconds()
    return elapsed > SESSION_IDLE_TTL_SECONDS


def _parse_naive_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _strategic_local_continuation_marker_is_fresh(marker: dict[str, Any]) -> bool:
    freshness_ts = _parse_naive_utc_timestamp(
        marker.get("last_seen_at") or marker.get("updated_at")
    )
    if freshness_ts is None:
        return False
    elapsed = (_utcnow() - freshness_ts).total_seconds()
    return elapsed <= _strategic_local_continuation_ttl_seconds()


def _timestamp_is_older_than(
    value: Any,
    *,
    now: datetime,
    seconds: int,
) -> bool:
    parsed = _parse_naive_utc_timestamp(value)
    if parsed is None:
        return True
    return (now - parsed).total_seconds() >= seconds


def _strategic_local_continuation_renewal_due(
    marker: dict[str, Any],
    workbench_state: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    return (
        _timestamp_is_older_than(
            marker.get("last_seen_at") or marker.get("updated_at"),
            now=now,
            seconds=STRATEGIC_LOCAL_CONTINUATION_RENEWAL_MIN_SECONDS,
        )
        or _timestamp_is_older_than(
            _effective_last_active(workbench_state),
            now=now,
            seconds=STRATEGIC_LOCAL_CONTINUATION_RENEWAL_MIN_SECONDS,
        )
    )


def _strategic_local_continuation_marker_owner_id(
    marker: dict[str, Any],
) -> str | None:
    return _string(marker.get("operator_user_id"), max_chars=160)


def _strategic_local_continuation_marker_session_started_at(
    marker: dict[str, Any],
) -> str | None:
    return _string(marker.get("owner_session_started_at"), max_chars=80)


def _strategic_local_continuation_marker_is_protected_foreign(
    marker: dict[str, Any],
    *,
    operator_id: str | None,
) -> bool:
    marker_operator_id = _strategic_local_continuation_marker_owner_id(marker)
    return (
        marker.get("version") == 1
        and marker.get("active") is True
        and marker_operator_id is not None
        and marker_operator_id != operator_id
        and _strategic_local_continuation_marker_is_fresh(marker)
    )


def _strategic_local_continuation_marker_is_protected_newer_owner_session(
    marker: dict[str, Any],
    *,
    operator_id: str | None,
    session_started_at: str | None,
) -> bool:
    marker_operator_id = _strategic_local_continuation_marker_owner_id(marker)
    marker_session_started_at = (
        _strategic_local_continuation_marker_session_started_at(marker)
    )
    if (
        marker.get("version") != 1
        or marker.get("active") is not True
        or marker_operator_id is None
        or marker_operator_id != operator_id
        or marker_session_started_at is None
        or session_started_at is None
        or marker_session_started_at == session_started_at
        or not _strategic_local_continuation_marker_is_fresh(marker)
    ):
        return False
    marker_started = _parse_naive_utc_timestamp(marker_session_started_at)
    state_started = _parse_naive_utc_timestamp(session_started_at)
    if marker_started is None or state_started is None:
        return False
    return marker_started > state_started


def apply_strategic_local_continuation_marker(
    vision: dict[str, Any],
    workbench_state: dict[str, Any],
    *,
    operator_user_id: str,
    preferred_runner_id: str | None = None,
    renewal_reason: str | None = None,
) -> None:
    """Update the compact project-level marker for local Strategic continuation.

    This is the canonical renewal entry point. Marker freshness is legitimately
    renewed here on an OPERATOR INTERACTION (a workbench save carries a fresh
    ``last_interaction_at``) or, via ``renewal_reason``, on an APPLIED STRATEGIC
    OP (progress). When ``renewal_reason`` is supplied the marker's freshness is
    bumped to *now* (and the reason recorded on the marker) even if the caller's
    ``workbench_state`` does not carry a fresh ``last_interaction_at`` — this is
    what a progress heartbeat calls to renew the lease on progress.

    Bare runner liveness must NOT reach this path: the dead-man's switch depends
    on freshness tracking real interaction/progress, not the runner merely being
    alive (see ``refresh_strategic_local_continuation_on_runner_heartbeat``).
    """

    operator_id = _string(str(operator_user_id), max_chars=160)
    session_started_at = _string(
        workbench_state.get("session_started_at"),
        max_chars=80,
    )
    if workbench_state.get("interaction_mode") != "continuous":
        marker = vision.get(STRATEGIC_LOCAL_CONTINUATION_KEY)
        if (
            not isinstance(marker, dict)
            or not (
                _strategic_local_continuation_marker_is_protected_foreign(
                    marker,
                    operator_id=operator_id,
                )
                or _strategic_local_continuation_marker_is_protected_newer_owner_session(
                    marker,
                    operator_id=operator_id,
                    session_started_at=session_started_at,
                )
            )
        ):
            vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)
        return

    marker = vision.get(STRATEGIC_LOCAL_CONTINUATION_KEY)
    if isinstance(marker, dict) and (
        _strategic_local_continuation_marker_is_protected_foreign(
            marker,
            operator_id=operator_id,
        )
        or _strategic_local_continuation_marker_is_protected_newer_owner_session(
            marker,
            operator_id=operator_id,
            session_started_at=session_started_at,
        )
    ):
        return

    prior_marker = marker if isinstance(marker, dict) else None
    now = _utcnow().isoformat()
    # ``last_seen_at`` is the dead-man's-switch FRESHNESS anchor. It advances to
    # *now* ONLY on a genuine renewal — a truthy ``renewal_reason``
    # ('operator_interaction' from an operator workbench save, or
    # 'strategic_progress' from a heartbeat reporting an applied
    # strategic op). A ``renewal_reason`` of None is a LIVENESS-ONLY touch (e.g.
    # a bare heartbeat with progress=False): record that the marker was
    # seen (bump ``updated_at`` below) but DO NOT advance freshness, so an
    # alive-but-idle session still goes stale on schedule and the cloud cadence /
    # cloud fallback can reclaim it. Freshness must track real interaction /
    # progress, NOT mere liveness — bumping it from ``workbench_state``'s
    # ``last_interaction_at`` on every touch (the old behavior) made the reason
    # parameter dead and let a heartbeating-but-idle session pin authority
    # forever. Preserve the prior anchor on a liveness touch; a brand-new marker
    # with no reason anchors to now so its first write is fresh.
    if renewal_reason:
        last_seen_at = now
    elif prior_marker is not None:
        last_seen_at = (
            _string(prior_marker.get("last_seen_at"), max_chars=80)
            or _string(prior_marker.get("updated_at"), max_chars=80)
            or now
        )
    else:
        last_seen_at = now
    marker: dict[str, Any] = {
        "version": 1,
        "active": True,
        "operator_user_id": operator_id,
        "updated_at": now,
        "last_seen_at": last_seen_at,
    }
    reason = _string(renewal_reason, max_chars=60)
    if reason:
        marker["last_renewal_reason"] = reason
    if session_started_at:
        marker["owner_session_started_at"] = session_started_at
    runner_source = (
        preferred_runner_id
        if preferred_runner_id is not None
        else workbench_state.get("preferred_runner_id")
    )
    runner_id = _string(str(runner_source), max_chars=80) if runner_source else None
    if runner_id:
        marker["preferred_runner_id"] = runner_id
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = marker


def strategic_local_continuation_status_from_vision(
    vision: dict[str, Any] | None,
    *,
    operator_user_id: str | None = None,
) -> dict[str, Any]:
    ttl_seconds = _strategic_local_continuation_ttl_seconds()
    marker = (vision or {}).get(STRATEGIC_LOCAL_CONTINUATION_KEY)
    if not isinstance(marker, dict) or marker.get("active") is False:
        return {
            "enabled": False,
            "fresh": False,
            "preferred_runner_id": None,
            "reason": "not_enabled",
            "updated_at": None,
            "last_seen_at": None,
            "expires_at": None,
            "owner_user_id": None,
            "owner_session_started_at": None,
            "owned_by_current_operator": False,
            "ownership": "none",
            "ttl_seconds": ttl_seconds,
        }

    updated_at = _string(marker.get("updated_at"), max_chars=80)
    last_seen_at = _string(marker.get("last_seen_at"), max_chars=80)
    owner_user_id = _strategic_local_continuation_marker_owner_id(marker)
    owner_session_started_at = (
        _strategic_local_continuation_marker_session_started_at(marker)
    )
    current_operator_id = (
        _string(str(operator_user_id), max_chars=160) if operator_user_id else None
    )
    owned_by_current_operator = (
        current_operator_id is not None and owner_user_id == current_operator_id
    )
    if owner_user_id is None:
        ownership = "unknown"
    elif current_operator_id is None:
        ownership = "owned"
    elif owned_by_current_operator:
        ownership = "current_operator"
    else:
        ownership = "other_operator"
    freshness_ts = _parse_naive_utc_timestamp(last_seen_at or updated_at)
    if freshness_ts is None:
        fresh = False
        reason = "missing_timestamp"
        expires_at = None
    else:
        elapsed = (_utcnow() - freshness_ts).total_seconds()
        fresh = elapsed <= ttl_seconds
        reason = "armed" if fresh else "stale"
        expires_at = (
            freshness_ts + timedelta(seconds=ttl_seconds)
        ).isoformat()
    public_owner_user_id = owner_user_id if owned_by_current_operator else None

    return {
        "enabled": True,
        "fresh": fresh,
        "preferred_runner_id": _string(
            marker.get("preferred_runner_id"),
            max_chars=80,
        ),
        "reason": reason,
        "updated_at": updated_at,
        "last_seen_at": last_seen_at,
        "expires_at": expires_at,
        "owner_user_id": public_owner_user_id,
        "owner_session_started_at": owner_session_started_at,
        "owned_by_current_operator": owned_by_current_operator,
        "ownership": ownership,
        "ttl_seconds": ttl_seconds,
    }


def workbench_state_from_prefs(prefs: dict[str, Any] | None) -> dict[str, Any]:
    raw = (prefs or {}).get(WORKBENCH_STATE_KEY)
    if raw is None:
        return default_workbench_state()
    state = sanitize_workbench_state(raw, touch=False)
    if _workbench_session_expired(state):
        state = {
            **state,
            "interaction_mode": "chat",
            "active_surface": "strategic",
            "selected_refs": [],
            "session_started_at": None,
        }
    return state


def workbench_mode_should_arm_strategic_refresh(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    """True when the operator intentionally enters an active Strategic mode."""

    current_mode = current.get("interaction_mode")
    if current_mode not in _STRATEGIC_ARMING_INTERACTION_MODES:
        return False
    previous_mode = (
        previous.get("interaction_mode") if isinstance(previous, dict) else None
    )
    return previous_mode != current_mode


def workbench_mode_refresh_excerpt(state: dict[str, Any]) -> str:
    mode = state.get("interaction_mode")
    mode_label = "workday" if mode == "continuous" else "chat"
    active_surface = state.get("active_surface") or "strategic"
    refs = state.get("selected_refs") if isinstance(state.get("selected_refs"), list) else []
    ref_labels = [
        ref.get("label") or f"{ref.get('kind')}:{ref.get('id')}"
        for ref in refs[:3]
        if isinstance(ref, dict) and (ref.get("label") or ref.get("id"))
    ]
    detail = f"Operator switched Strategic workbench to {mode_label} mode"
    detail += f" from {active_surface}."
    if ref_labels:
        detail += " Context: " + "; ".join(str(label)[:120] for label in ref_labels)
    return detail[:600]


async def get_strategic_workbench_state(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    *,
    for_update: bool = False,
    populate_existing: bool = False,
) -> dict[str, Any]:
    row = await get_messages_noise_prefs_row(
        session,
        user_id,
        project_id,
        for_update=for_update,
        populate_existing=populate_existing,
    )
    return workbench_state_from_prefs(row.prefs if row else None)


async def _valid_preferred_runner_ids(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> set[str]:
    runners = (
        await session.execute(
            select(LocalRunner)
            .where(
                runner_allowed_for_project(project_id),
                LocalRunner.status != "deleted",
            )
            .with_for_update()
        )
    ).scalars().all()
    return {
        str(runner.id)
        for runner in runners
        if _runner_handles_any_strategic_work(runner.capabilities)
    }


async def clear_preferred_runner_references(
    session: AsyncSession,
    project_id: uuid.UUID,
    runner_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Remove a runner preference from saved workbench state after revocation."""

    runner_id_text = _string(str(runner_id), max_chars=80)
    if not runner_id_text:
        return {"workbench_states": 0, "continuation_marker": False}

    rows = (
        await session.execute(
            select(MessagesNoisePrefs)
            .where(MessagesNoisePrefs.project_id == project_id)
            .with_for_update()
        )
    ).scalars().all()
    cleared_workbench_states = 0
    for row in rows:
        prefs = dict(row.prefs or {})
        workbench = prefs.get(WORKBENCH_STATE_KEY)
        if not isinstance(workbench, dict):
            continue
        preferred_runner_id = _string(
            workbench.get("preferred_runner_id"),
            max_chars=80,
        )
        if preferred_runner_id != runner_id_text:
            continue
        next_workbench = dict(workbench)
        next_workbench.pop("preferred_runner_id", None)
        prefs[WORKBENCH_STATE_KEY] = next_workbench
        row.prefs = prefs
        flag_modified(row, "prefs")
        session.add(row)
        cleared_workbench_states += 1

    cleared_marker = False

    def _clear_continuation_marker(vision: dict[str, Any]) -> None:
        nonlocal cleared_marker
        marker = vision.get(STRATEGIC_LOCAL_CONTINUATION_KEY)
        if not isinstance(marker, dict):
            return
        preferred_runner_id = _string(
            marker.get("preferred_runner_id"),
            max_chars=80,
        )
        if preferred_runner_id != runner_id_text:
            return
        next_marker = dict(marker)
        next_marker.pop("preferred_runner_id", None)
        vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = next_marker
        cleared_marker = True

    await with_vision_locked(
        session,
        project_id,
        _clear_continuation_marker,
        skip_locked=False,
    )
    return {
        "workbench_states": cleared_workbench_states,
        "continuation_marker": cleared_marker,
    }


async def refresh_strategic_local_continuation_on_runner_heartbeat(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    runner_id: uuid.UUID | str,
    capabilities: dict[str, Any] | None,
) -> bool:
    """No-op: bare runner liveness must NOT renew Strategic continuation.

    SAFETY (dead-man's switch). The local-Strategic continuation marker's
    freshness renews ONLY on (a) an operator interaction or (b) an applied
    strategic op (progress) — never on a mere runner heartbeat. This helper
    previously extended the marker (and bumped ``last_interaction_at``) purely
    because a pinned Strategic-capable runner was alive, which let an
    alive-but-idle local session hold authority forever with zero operator
    interaction (the cloud cadence could never reclaim it). It is now
    intentionally inert: it performs no read or write and always returns
    ``False``.

    The legitimate renewal path is ``apply_strategic_local_continuation_marker``
    (operator workbench save, or a progress heartbeat calling it with a
    ``renewal_reason`` on applied progress). Kept as a callable no-op so the
    runner heartbeat handler's call site stays unchanged; the parameters are
    accepted and ignored.
    """

    del session, project_id, runner_id, capabilities
    return False


async def upsert_strategic_workbench_state_with_previous(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    raw_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    for attempt in range(2):
        valid_runner_ids = await _valid_preferred_runner_ids(session, project_id)
        row = await get_messages_noise_prefs_row(
            session,
            user_id,
            project_id,
            for_update=True,
        )
        existing_prefs = dict(row.prefs or {}) if row is not None else {}
        previous = workbench_state_from_prefs(existing_prefs)
        state = sanitize_workbench_state(
            raw_state,
            existing=previous,
            valid_preferred_runner_ids=valid_runner_ids,
        )
        next_prefs = dict(existing_prefs)
        next_prefs[WORKBENCH_STATE_KEY] = state

        if row is None:
            row = MessagesNoisePrefs(
                user_id=user_id,
                project_id=project_id,
                prefs=next_prefs,
            )
        else:
            row.prefs = next_prefs
            flag_modified(row, "prefs")
        session.add(row)

        try:
            await session.flush()
            return previous, state
        except IntegrityError:
            await session.rollback()
            if attempt == 0:
                continue
            raise

    raise RuntimeError("Failed to update strategic workbench state")


def workbench_context_for_strategic_session_snapshot(
    stored_workbench: dict[str, Any],
    raw_workbench_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge persisted workbench state with transient snapshot-only descriptors."""

    if raw_workbench_context is None:
        return None
    snapshot_context: dict[str, Any] = {
        **stored_workbench,
        "context_package_id": raw_workbench_context.get("context_package_id"),
    }
    selected_ref_descriptors = raw_workbench_context.get("selected_ref_descriptors")
    if selected_ref_descriptors is not None:
        snapshot_context["selected_ref_descriptors"] = selected_ref_descriptors
    context_package_descriptor = (
        raw_workbench_context.get("context_package_descriptor")
        or raw_workbench_context.get("context_package")
    )
    if context_package_descriptor is not None:
        snapshot_context["context_package_descriptor"] = context_package_descriptor
    return snapshot_context


async def persist_strategic_workbench_update(
    session: AsyncSession,
    user_id: str,
    project_id: uuid.UUID,
    raw_state: dict[str, Any],
    *,
    sync_local_continuation_marker: bool = True,
    arm_interaction_mode: bool = True,
    resident_session_active: bool = False,
    commit: bool = True,
) -> StrategicWorkbenchWriteResult:
    """Persist operator workbench state and run canonical side effects."""

    previous_state, state = await upsert_strategic_workbench_state_with_previous(
        session,
        user_id,
        project_id,
        raw_state,
    )
    if sync_local_continuation_marker:
        def _update_local_continuation_marker(vision: dict[str, Any]) -> None:
            # An operator workbench save IS a genuine operator interaction — pass
            # a truthy renewal_reason so the marker's freshness is legitimately
            # renewed (bare liveness never reaches this path). Without a reason
            # the marker now only records liveness and would NOT renew freshness.
            apply_strategic_local_continuation_marker(
                vision,
                state,
                operator_user_id=user_id,
                renewal_reason="operator_interaction",
            )

        await with_vision_locked(
            session,
            project_id,
            _update_local_continuation_marker,
            skip_locked=False,
        )

    arming = ARMING_PASSIVE_NOTE
    should_arm = (
        arm_interaction_mode
        and workbench_mode_should_arm_strategic_refresh(previous_state, state)
    )
    if should_arm and resident_session_active:
        if commit:
            await session.commit()
        return StrategicWorkbenchWriteResult(
            previous_state=previous_state,
            state=state,
            arming=ARMING_RESIDENT_SESSION,
        )
    if commit:
        await session.commit()
    if should_arm:
        if not commit:
            return StrategicWorkbenchWriteResult(
                previous_state=previous_state,
                state=state,
                arming=ARMING_NOT_ARMED,
            )
        armed = await request_strategic_refresh(
            session,
            project_id,
            "workbench_interaction_mode",
            origin=ORIGIN_OPERATOR,
            steering_excerpt=workbench_mode_refresh_excerpt(state),
            skip_locked=False,
        )
        arming = ARMING_STEERING if armed else ARMING_NOT_ARMED
        if armed:
            await session.commit()
    return StrategicWorkbenchWriteResult(
        previous_state=previous_state,
        state=state,
        arming=arming,
    )
