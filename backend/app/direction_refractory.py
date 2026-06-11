"""Shared direction refractory helpers.

Keeps the eligibility window logic in one place so completion stamping,
manual re-spawn checks, and payload projection all agree on the same hold.
"""

from datetime import datetime, timedelta
from typing import Any

from app.time_utils import _coerce_naive_utc, _utcnow


def _runtime_config() -> dict[str, Any]:
    from app.shared_state import runtime_config as _runtime_config

    return _runtime_config


def _resolve_standard_refractory_minutes(direction: dict[str, Any]) -> float:
    runtime_config = _runtime_config()
    explicit = runtime_config.get("direction_refractory_minutes")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        if explicit > 0:
            cooldown_minutes = float(explicit)
        else:
            cooldown_minutes = float(
                runtime_config.get("autonomous_direction_cooldown_minutes", 240)
            )
    else:
        cooldown_minutes = float(
            runtime_config.get("autonomous_direction_cooldown_minutes", 240)
        )

    if direction.get("last_cycle_productive"):
        return float(runtime_config.get("productive_direction_cooldown_minutes", 45))

    consecutive = int(direction.get("consecutive_unproductive_cycles", 0) or 0)
    if consecutive <= 1:
        multiplier = 0.375
    elif consecutive == 2:
        multiplier = 0.75
    else:
        multiplier = 1.0
    return cooldown_minutes * multiplier


def resolve_direction_refractory_minutes(direction: dict[str, Any]) -> float:
    """Return the effective refractory minutes for the current direction state."""
    runtime_config = _runtime_config()
    if direction.get("possibly_saturated"):
        explicit = runtime_config.get("direction_saturation_refractory_minutes")
        if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
            if explicit > 0:
                return float(explicit)
        return _resolve_standard_refractory_minutes(direction) * 2.0
    return _resolve_standard_refractory_minutes(direction)


def stamp_direction_refractory(
    direction: dict[str, Any], *, now: datetime | None = None
) -> None:
    """Materialize refractory_until from the direction's current state."""
    refractory_minutes = resolve_direction_refractory_minutes(direction)
    if refractory_minutes <= 0:
        direction.pop("refractory_until", None)
        return
    now = now or _utcnow()
    direction["refractory_until"] = (
        now + timedelta(minutes=float(refractory_minutes))
    ).isoformat()


def get_direction_refractory_info(
    direction: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return the current refractory hold projection for API payloads."""
    refractory_until = direction.get("refractory_until")
    if not refractory_until:
        return {
            "refractory_until": None,
            "refractory_reason": None,
            "refractory_remaining_minutes": None,
        }
    try:
        refractory_dt = datetime.fromisoformat(refractory_until)
    except (TypeError, ValueError):
        return {
            "refractory_until": None,
            "refractory_reason": None,
            "refractory_remaining_minutes": None,
        }
    refractory_dt = _coerce_naive_utc(refractory_dt)
    now = now or _utcnow()
    remaining_minutes = (refractory_dt - now).total_seconds() / 60
    if remaining_minutes <= 0:
        return {
            "refractory_until": None,
            "refractory_reason": None,
            "refractory_remaining_minutes": None,
        }
    return {
        "refractory_until": refractory_dt,
        "refractory_reason": (
            "saturation" if direction.get("possibly_saturated") else "standard"
        ),
        "refractory_remaining_minutes": remaining_minutes,
    }


def is_direction_refractory(direction: dict[str, Any], *, now: datetime | None = None) -> bool:
    info = get_direction_refractory_info(direction, now=now)
    return bool(info["refractory_remaining_minutes"] and info["refractory_remaining_minutes"] > 0)
