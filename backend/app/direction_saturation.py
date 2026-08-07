"""Forced strategist disposition for saturated directions.

A direction that keeps producing research findings but spawns no work is
saturated in the sense that matters here: it owns a live outcome and generates
nothing.  Previously that outcome was a silent longer refractory wait
(``direction_saturation_refractory_minutes``) plus an idle direction until the
next research cycle re-queued it.  This module escalates the saturated outcome
into a durable disposition item the strategist is forced to answer with one of
a bounded disposition set — commission concrete work, narrow the direction, or
retire it.

The item is stored on the direction dict (``saturation_disposition``) so it
persists across cycles until the strategist resolves it.  While pending it is
never re-created (one durable item per saturated direction, not re-emitted every
cycle) and never silently dropped.  Resolving the item removes it so a genuine
NEW saturation episode can re-flag later.
"""

from typing import Any, List

# Key on the direction dict that carries the durable disposition item.
SATURATION_DISPOSITION_KEY = "saturation_disposition"

# Item status values.
DISPOSITION_REQUIRED = "required"

# Bounded disposition vocabulary the strategist must choose from when resolving
# a saturated direction.  ``commission_work`` = spawn concrete tasks in the
# direction, ``narrow_scope`` = tighten the direction's scope/contract so it can
# produce work again, ``retire`` = retire the direction as exhausted.
VALID_DISPOSITIONS = frozenset({"commission_work", "narrow_scope", "retire"})

_RETIRED_DIRECTION_STATUSES = frozenset(
    {"expired", "retired", "superseded", "completed"}
)


def direction_user_paused(direction: dict) -> bool:
    """True when the operator paused the direction themselves.

    A ``paused`` direction whose ``auto_paused`` flag is absent/false was paused
    by the operator (or the legacy pause path), not by an autonomous rule.
    USER_PAUSED directions must never be escalated: the operator already made a
    decision about them, so a forced strategist disposition would be noise.
    """
    return direction.get("status") == "paused" and not direction.get("auto_paused")


def flag_saturation_disposition(
    direction: dict,
    *,
    now_iso: str,
    findings_produced: int,
    tasks_created: int,
) -> bool:
    """Escalate a saturated direction into a durable strategist-disposition item.

    Returns ``True`` when a NEW item was created; ``False`` when the direction
    already carries a pending item (dedup — never re-emitted across cycles), is
    user-paused (excluded), or is already terminal (nothing left to dispose of).

    The item persists on the direction until the strategist resolves it via
    ``resolve_saturation_disposition`` or one of the disposition ops
    (retire_direction / update_direction / spawn_task) removes it.
    """
    if direction_user_paused(direction):
        return False
    if direction.get("status") in _RETIRED_DIRECTION_STATUSES:
        return False
    if direction.get(SATURATION_DISPOSITION_KEY):
        # One durable item per saturated direction — never re-emit while pending.
        return False
    history = direction.get("research_history") or []
    direction[SATURATION_DISPOSITION_KEY] = {
        "status": DISPOSITION_REQUIRED,
        "flagged_at": now_iso,
        "cycles_researched": len(history),
        "findings_produced": int(findings_produced or 0),
        "recent_tasks_created": int(tasks_created or 0),
        "reason": (
            "Direction research keeps producing findings but no spawnable work; "
            "requires an explicit strategist disposition (commission work, "
            "narrow the scope, or retire)"
        ),
    }
    return True


def resolve_saturation_disposition(
    direction: dict,
    *,
    disposition: str,
    resolved_at: str,
) -> bool:
    """Mark a pending saturation-disposition item resolved.

    The item is removed entirely rather than retained: a resolved disposition is
    the strategist's concrete answer (commission/narrow/retire), and removing it
    lets a genuinely NEW saturation episode re-flag on a future cycle.  Returns
    ``True`` when a pending item was present and removed.
    """
    if not direction.get(SATURATION_DISPOSITION_KEY):
        return False
    direction.pop(SATURATION_DISPOSITION_KEY, None)
    return True


def clear_saturation_disposition(direction: dict) -> bool:
    """Drop the saturation-disposition item without recording a disposition.

    Used by the strategist disposition ops when the direction is retired,
    substantively updated (narrow), or commissioned with new work — any of which
    answers the pending item.  Returns ``True`` when an item was removed.
    """
    if not direction.get(SATURATION_DISPOSITION_KEY):
        return False
    direction.pop(SATURATION_DISPOSITION_KEY, None)
    return True


def build_saturation_dispositions(project: Any) -> List[dict]:
    """Bounded list of directions carrying a pending saturation disposition.

    Each entry is ``{id, text, priority, flagged_at, cycles_researched,
    findings_produced, recent_tasks_created}`` — exactly the detail the
    strategist needs to pick a disposition.  Only directions whose item status is
    ``required`` are surfaced; resolved/absent items are omitted.  Pure over
    ``project.directions`` (no DB / no network), so it is safe to call in the
    payload build.
    """
    out: List[dict] = []
    for d in project.directions or []:
        # A direction the operator paused is never escalated, even if it carried
        # a pending item from before the pause: the operator's pause is the
        # disposition. The dormant item resumes surfacing if the direction is
        # unpaused while still saturated (it is never silently dropped).
        if direction_user_paused(d):
            continue
        item = d.get(SATURATION_DISPOSITION_KEY)
        if not isinstance(item, dict) or item.get("status") != DISPOSITION_REQUIRED:
            continue
        out.append(
            {
                "id": d.get("id"),
                "text": (d.get("text") or "")[:200],
                "priority": d.get("priority"),
                "flagged_at": item.get("flagged_at"),
                "cycles_researched": item.get("cycles_researched"),
                "findings_produced": item.get("findings_produced", 0),
                "recent_tasks_created": item.get("recent_tasks_created", 0),
            }
        )
    return out
