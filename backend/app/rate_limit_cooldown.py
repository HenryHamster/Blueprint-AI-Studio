"""Single-source cooldown-window math for disabled_models entries.

Shared by max_plan_pool (live pool state) and models.MaxPlanAccount
(persisted-state mirror, used by failure-classification correlation).
Kept dependency-free so both sides can import it at module scope without
introducing a cycle (max_plan_pool imports models).
"""

from __future__ import annotations

_QUOTA_EXHAUSTED_COOLDOWN_SECONDS = 24 * 60 * 60  # flat 24h for billing walls
_QUARANTINE_COOLDOWN_SECONDS = (
    15 * 60
)  # floor for confidence-gated quarantine recheck
_REGRESSION_COOLDOWN_SECONDS = 6 * 60 * 60  # flat 6h reprobe for regression quarantine


def cooldown_seconds_for_info(info: dict) -> int:
    """Compute remaining-effective cooldown window (seconds) for a disabled_models entry.

    Rules:
    - kind="quota_exhausted" → flat 24h. These are billing walls; the
      exponential-backoff curve doesn't apply because the error doesn't
      resolve on clock.
    - kind="regression" → flat 6h reprobe. Regression triggers on slow
      drift in success rate; a short cooldown would keep cycling the
      bad combination.
    - Otherwise exponential: min(30 * 2^(hits-1), 240) minutes.
    - kind="quarantine" additionally floors the exponential curve at 15min:
      the trip-wire fires on top of an existing exponential backoff (hits
      already >= QUARANTINE_CONSECUTIVE_N), so the flat 15min is a floor for
      accounts still early in the curve, never a ceiling that shortens a
      later hit's longer exponential window.
    - Server-provided retry_after_seconds is treated as a floor — we never
      shorten below it. All three floors (quarantine flat, exponential,
      server hint) compose as a single max, so a quarantined entry with a
      longer server hint is never shortened by the flat floor.
    """
    if info.get("kind") == "quota_exhausted":
        return _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
    if info.get("kind") == "regression":
        return _REGRESSION_COOLDOWN_SECONDS
    hits = info.get("hits", 1)
    exponential = min(30 * (2 ** (hits - 1)), 240) * 60
    quarantine_floor = (
        _QUARANTINE_COOLDOWN_SECONDS if info.get("kind") == "quarantine" else 0
    )
    hint = info.get("retry_after_seconds")
    hint_seconds = int(hint) if isinstance(hint, (int, float)) and hint > 0 else 0
    return max(quarantine_floor, exponential, hint_seconds)
