"""Helpers for direction recommended_mode handling."""

from __future__ import annotations

from typing import Any

from app.time_utils import _utcnow

# Sources that the auto-clear path considers "auto-set" and therefore eligible
# to be cleared when the system recovers. Any unknown or future source (e.g.
# "operator") is treated as a manual pin and is never auto-cleared.
_AUTO_CLEAR_CLEARABLE_SOURCES: frozenset[str] = frozenset({"self_audit", "strategic"})

# Direction JSONB key that tracks consecutive healthy audit assessments.
# Stored alongside the mode so it survives across ticks without a separate table.
_HEALTHY_CONSECUTIVE_KEY = "_healthy_consecutive_count"

# Number of consecutive ship + no-drift audit assessments required before the
# mode is automatically cleared. Two ticks gives one false-positive buffer.
_AUTO_CLEAR_CONSECUTIVE_THRESHOLD = 2

# Matches models.RECOMMENDED_MODE_META_KEY — duplicated here to avoid an app
# import that would introduce a circular-dependency risk.
_RECOMMENDED_MODE_META_KEY = "recommended_mode_meta"


def normalized_recommended_mode(direction: dict[str, Any] | None) -> str | None:
    mode = (direction or {}).get("recommended_mode")
    if not isinstance(mode, str):
        return None
    mode = mode.strip().lower()
    return mode or None


def is_pause_mode(direction: dict[str, Any] | None) -> bool:
    return normalized_recommended_mode(direction) == "pause"


def append_prompt_suffix(payload: dict[str, Any], suffix: str) -> None:
    existing = payload.get("prompt_suffix", "")
    payload["prompt_suffix"] = f"{existing}\n{suffix}".strip() if existing else suffix


def mode_prompt_hint(mode: str | None) -> str | None:
    if mode == "audit":
        return (
            "Mode hint: this direction is in audit mode. Prioritize auditing and validation of the "
            "target system's health, drift, and claim-vs-shipped consistency over new feature work."
        )
    if mode == "fundamentals":
        return (
            "Mode hint: this direction is in fundamentals mode. Prioritize foundational reliability, "
            "architecture, tests, and maintainability over incremental product-facing features."
        )
    return None


def maybe_auto_clear_mode(
    direction: dict[str, Any],
    maturity_recommended_mode: str | None,
    *,
    has_drift_flags: bool = False,
) -> bool:
    """Auto-clear audit/fundamentals mode when system maturity has recovered.

    Mutates *direction* in place. Returns True when the mode was cleared.

    Rules:
    - Only clears "audit" or "fundamentals" modes.
    - Only clears modes set by auto sources (self_audit, strategic). Unknown or
      future operator-pinned sources are left untouched.
    - Requires _AUTO_CLEAR_CONSECUTIVE_THRESHOLD consecutive assessments where
      the system's maturity is "ship" AND drift signals are clean.
    - Stamps _mode_auto_cleared_at / _mode_auto_cleared_from on clearance so
      operators can see what happened and when.
    """
    mode = normalized_recommended_mode(direction)
    if mode not in ("audit", "fundamentals"):
        direction.pop(_HEALTHY_CONSECUTIVE_KEY, None)
        return False

    meta = direction.get(_RECOMMENDED_MODE_META_KEY) or {}
    if (
        not isinstance(meta, dict)
        or meta.get("source") not in _AUTO_CLEAR_CLEARABLE_SOURCES
    ):
        # Operator-pinned or unknown provenance — never auto-clear.
        return False

    if maturity_recommended_mode != "ship" or has_drift_flags:
        direction[_HEALTHY_CONSECUTIVE_KEY] = 0
        return False

    count = int(direction.get(_HEALTHY_CONSECUTIVE_KEY) or 0) + 1
    direction[_HEALTHY_CONSECUTIVE_KEY] = count

    if count < _AUTO_CLEAR_CONSECUTIVE_THRESHOLD:
        return False

    # All conditions met — clear the mode and record provenance.
    prev_mode = mode
    direction.pop("recommended_mode", None)
    direction.pop(_RECOMMENDED_MODE_META_KEY, None)
    direction.pop(_HEALTHY_CONSECUTIVE_KEY, None)
    direction["_mode_auto_cleared_at"] = _utcnow().isoformat()
    direction["_mode_auto_cleared_from"] = prev_mode
    return True
