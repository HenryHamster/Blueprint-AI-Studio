"""Direction-level failure-streak / hold tracking (split out of shared_state).

Owns the per-direction consecutive-failure counters and the hold state machine
(code vs infra streaks, timeout auto-clear). Re-exported from
:mod:`app.shared_state` as a thin facade. Behaviour-preserving.
"""

import logging
from datetime import timedelta
from typing import Any, Dict

from app.failure_classification import INFRA_ANALYTICS_CLASSES as _DIRECTION_INFRA_CLASSES
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Direction-Level Failure Streak Tracking ---
# direction_id -> {
#   consecutive_failures: int,
#   last_failure_class: str,
#   last_failure_at: datetime,
#   held: bool,
#   held_at: Optional[datetime],
#   project_id: str,
# }
direction_failure_streaks: Dict[str, Dict] = {}

# Number of consecutive CODE_ERROR failures before a direction is held
DIRECTION_HOLD_THRESHOLD = 3
# Automatic hold expiry (safety valve so a direction doesn't stay blocked forever)
DIRECTION_HOLD_TIMEOUT_HOURS = 2

# _DIRECTION_INFRA_CLASSES: the INFRA analytics superset, imported from
# failure_classification at module top-level. Infrastructure failures
# (RATE_LIMIT, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API, SCOPE_OVERFLOW,
# and the terminal routing-contract class UNPLACEABLE_EXECUTION_SITE — the task
# never ran) do NOT count toward the direction streak — they're platform noise,
# not the direction's fault. Account health + provisioning tracking handle them
# upstream.


def record_direction_result(
    direction_id: str,
    success: bool,
    failure_class: str = "",
    project_id: str = "",
) -> None:
    """Update the per-direction failure streak on task completion.

    Tracks two parallel counters: code failures (CODE_ERROR and other non-infra)
    and infra failures (PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT, EXTERNAL_API,
    SCOPE_OVERFLOW). Each has its own hold threshold. A success resets both
    counters and clears any existing hold.
    """
    if not direction_id:
        return

    streak = direction_failure_streaks.setdefault(
        direction_id,
        {
            "consecutive_failures": 0,
            "consecutive_infra_failures": 0,
            "last_failure_class": "",
            "last_failure_at": None,
            "held": False,
            "held_at": None,
            "held_reason": "",
            "held_failure_class": "",
            "project_id": project_id,
        },
    )
    # Backfill new fields for entries created before infra tracking existed.
    streak.setdefault("consecutive_infra_failures", 0)
    streak.setdefault("held_reason", "")
    streak.setdefault("held_failure_class", "")
    if project_id:
        streak["project_id"] = project_id

    if success:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["last_failure_class"] = ""
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        return

    if failure_class in _DIRECTION_INFRA_CLASSES:
        # Infra failures (RATE_LIMIT, PROVISION_FAILURE, AGENT_TIMEOUT,
        # EXTERNAL_API, SCOPE_OVERFLOW) are not the direction's fault — they
        # happen at the pool / VM / third-party level. Counting them against
        # the direction wrongly holds innocent directions when the platform
        # has a bad hour. Account health + VM provisioning tracking handle
        # these upstream; the direction's streak stays unaffected.
        return

    streak["consecutive_failures"] += 1
    streak["last_failure_class"] = failure_class
    streak["last_failure_at"] = _utcnow()
    if (
        streak["consecutive_failures"] >= DIRECTION_HOLD_THRESHOLD
        and not streak["held"]
    ):
        streak["held"] = True
        streak["held_at"] = _utcnow()
        streak["held_reason"] = "code"
        streak["held_failure_class"] = failure_class
        logger.warning(
            f"[DIRECTION HEALTH] Direction {direction_id} HELD after "
            f"{streak['consecutive_failures']} consecutive {failure_class} failures."
        )


def clear_direction_hold(direction_id: str) -> None:
    """Explicitly clear a direction hold (e.g. after research_context update)."""
    streak = direction_failure_streaks.get(direction_id)
    if streak:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""


def get_direction_hold_info(direction_id: str) -> Dict[str, Any]:
    """Return hold metadata for a direction.

    Shape:
      {
        "held": bool,
        "reason": str,
        "held_at": Optional[datetime],
        "held_until": Optional[datetime],
      }

    Applies timeout auto-clear semantics identically to is_direction_held.
    """
    base = {"held": False, "reason": "", "held_at": None, "held_until": None}
    if not direction_id:
        return base

    streak = direction_failure_streaks.get(direction_id)
    if not streak or not streak.get("held"):
        return base

    held_at = streak.get("held_at")
    if (
        held_at
        and (_utcnow() - held_at).total_seconds() > DIRECTION_HOLD_TIMEOUT_HOURS * 3600
    ):
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        logger.info(
            f"[DIRECTION HEALTH] Direction {direction_id} hold expired (timeout)."
        )
        return base

    held_until = (
        held_at + timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS) if held_at else None
    )
    if streak.get("held_reason") == "infra":
        count = streak.get("consecutive_infra_failures", 0)
        fc = streak.get("held_failure_class") or streak["last_failure_class"] or "INFRA"
        reason = (
            f"Held: direction has {count} consecutive infra failures "
            f"(last: {fc}) — investigate scope/scale before retrying"
        )
    else:
        count = streak["consecutive_failures"]
        fc = (
            streak.get("held_failure_class")
            or streak["last_failure_class"]
            or "CODE_ERROR"
        )
        reason = f"Held: direction has {count} consecutive {fc} failures — awaiting investigation"

    return {
        "held": True,
        "reason": reason,
        "held_at": held_at,
        "held_until": held_until,
    }


def is_direction_held(direction_id: str) -> tuple[bool, str]:
    """Return (held, reason) for the direction.

    Checks timeout expiry: if the hold has been active for more than
    DIRECTION_HOLD_TIMEOUT_HOURS the hold is auto-cleared and (False, '') is returned.
    Returns (False, '') for unknown directions.
    """
    info = get_direction_hold_info(direction_id)
    return bool(info["held"]), str(info["reason"])


def get_held_directions() -> Dict[str, Dict]:
    """Return snapshot of all currently held directions (after applying timeout logic)."""
    result = {}
    for did, streak in direction_failure_streaks.items():
        held, reason = is_direction_held(did)
        if held:
            result[did] = {
                "consecutive_failures": streak["consecutive_failures"],
                "consecutive_infra_failures": streak.get(
                    "consecutive_infra_failures", 0
                ),
                "last_failure_class": streak["last_failure_class"],
                "last_failure_at": streak["last_failure_at"].isoformat()
                if streak.get("last_failure_at")
                else None,
                "held_at": streak["held_at"].isoformat()
                if streak.get("held_at")
                else None,
                "held_reason": streak.get("held_reason", "code"),
                "project_id": streak.get("project_id", ""),
                "reason": reason,
            }
    return result
