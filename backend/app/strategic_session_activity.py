"""Bounded machine-readable Strategic session activity contract."""

from __future__ import annotations

import uuid
from typing import Any


RESIDENT_ACTIVITY_REASON_CODES = {
    "consultation_queued",
    "consultation_running",
    "consultation_delivering",
}
RESIDENT_ACTIVITY_REASON_PHASES = {
    "consultation_queued": "waiting",
    "consultation_running": "working",
    "consultation_delivering": "working",
}
RESIDENT_AUTONOMY_STATES = {"active", "paused"}
RESIDENT_AUTONOMY_REASON_CODES = {
    "authority_not_held",
    "foreground_call",
    "chat_or_disabled",
    "consultation_queued",
    "consultation_running",
    "consultation_delivering",
    "repository_unavailable",
    "provider_retry",
    "session_stopping",
    "session_ended",
    "activity_unreported",
}
RESIDENT_ACTIVITY_CONSULTATION_ID_MAX_CHARS = 80
RESIDENT_ACTIVITY_SEMANTIC_FIELDS = (
    "phase",
    "reason_code",
    "consultation_id",
    "consultation_verified",
    "autonomy_state",
    "autonomy_reason_code",
)
SERVER_OWNED_AUTONOMY_REASONS = {"authority_not_held", "foreground_call"}


def _consultation_id(value: Any, *, reason_code: str | None) -> str | None:
    text = str(value or "").strip()
    if (
        reason_code is None
        or not text
        or len(text) > RESIDENT_ACTIVITY_CONSULTATION_ID_MAX_CHARS
    ):
        return None
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError, AttributeError):
        return None


def resident_activity_contract(
    value: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    """Normalize the machine fields for one working/waiting activity update."""
    raw = value if isinstance(value, dict) else {}
    reason_code = str(raw.get("reason_code") or "").strip().lower()
    if (
        reason_code not in RESIDENT_ACTIVITY_REASON_CODES
        or RESIDENT_ACTIVITY_REASON_PHASES.get(reason_code) != phase
    ):
        reason_code = None
    consultation_id = _consultation_id(
        raw.get("consultation_id"), reason_code=reason_code
    )
    if reason_code is not None and consultation_id is None:
        # Public phases are correlated claims, never a free Agent assertion.
        reason_code = None
    autonomy_state = str(raw.get("autonomy_state") or "").strip().lower()
    autonomy_reason_code = str(
        raw.get("autonomy_reason_code") or ""
    ).strip().lower()
    if reason_code is not None:
        autonomy_state = "paused"
        autonomy_reason_code = reason_code
    elif autonomy_state not in RESIDENT_AUTONOMY_STATES:
        autonomy_state = "paused"
        autonomy_reason_code = "activity_unreported"
    elif autonomy_state == "active":
        autonomy_reason_code = None
    elif autonomy_reason_code not in RESIDENT_AUTONOMY_REASON_CODES:
        autonomy_reason_code = "activity_unreported"
    return {
        "reason_code": reason_code,
        "consultation_id": consultation_id,
        "autonomy_state": autonomy_state,
        "autonomy_reason_code": autonomy_reason_code,
    }


def resident_activity_lifecycle_contract(
    value: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    """Normalize machine fields and apply server lifecycle precedence."""
    contract = resident_activity_contract(value, phase=phase)
    lifecycle_reason = {
        "ended": "session_ended",
        "retrying": "provider_retry",
        "stopping": "session_stopping",
    }.get(phase)
    if lifecycle_reason is not None:
        return {
            "reason_code": None,
            "consultation_id": None,
            "autonomy_state": "paused",
            "autonomy_reason_code": lifecycle_reason,
        }
    return contract


def resident_activity_with_server_fences(
    activity: dict[str, Any],
    *,
    authority_held: bool,
    foreground_call_active: bool,
) -> dict[str, Any]:
    """Apply and clear server-owned actability fences in the locked write."""
    result = dict(activity)
    if not authority_held:
        result.update(
            autonomy_state="paused",
            autonomy_reason_code="authority_not_held",
        )
    elif foreground_call_active:
        result.update(
            autonomy_state="paused",
            autonomy_reason_code="foreground_call",
        )
    elif result.get("autonomy_reason_code") in SERVER_OWNED_AUTONOMY_REASONS:
        result.update(autonomy_state="active", autonomy_reason_code=None)
    return result


def resident_activity_semantics(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return fields whose changes warrant a realtime state refetch."""
    return {key: snapshot.get(key) for key in RESIDENT_ACTIVITY_SEMANTIC_FIELDS}
