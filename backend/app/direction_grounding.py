"""Shared grounding checks for strategic direction creation."""

from __future__ import annotations

from typing import Any


def _norm_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def validate_create_direction_grounding(
    dedup_reasoning: object,
    directions: list[dict[str, Any]] | None,
    retired_memory: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Ground ``dedup_reasoning`` against live and recently-retired directions."""
    if not isinstance(dedup_reasoning, dict):
        return None

    raw_closest = dedup_reasoning.get("closest_directions")
    closest = [
        str(item).strip()
        for item in raw_closest
        if isinstance(raw_closest, list) and str(item).strip()
    ][:3] if isinstance(raw_closest, list) else []
    distinction = str(dedup_reasoning.get("distinction") or "").strip()

    live_ids: list[str] = []
    retired_ids: list[str] = []
    unresolved: list[str] = []
    directions = directions or []
    retired_memory = retired_memory or []

    for item in closest:
        item_norm = _norm_text(item)
        live_match = next(
            (
                d
                for d in directions
                if isinstance(d, dict)
                and (
                    _norm_text(d.get("id")) == item_norm
                    or _norm_text(d.get("text")) == item_norm
                )
            ),
            None,
        )
        if live_match is not None:
            live_ids.append(str(live_match.get("id") or item))
            continue

        retired_match = next(
            (
                mem
                for mem in retired_memory
                if isinstance(mem, dict)
                and (
                    _norm_text(mem.get("id")) == item_norm
                    or (
                        isinstance(mem.get("text_tokens"), list)
                        and len(
                            set(item_norm.split())
                            & {str(token).lower() for token in mem.get("text_tokens") or []}
                        )
                        >= 4
                    )
                )
            ),
            None,
        )
        if retired_match is not None:
            retired_ids.append(str(retired_match.get("id") or item))
        else:
            unresolved.append(item)

    status = "validated" if live_ids or retired_ids else "unverified"
    reason = (
        "matched live directions"
        if live_ids
        else "matched recently retired directions"
        if retired_ids
        else "no cited closest_directions matched project state"
    )
    if not distinction:
        status = "unverified"
        reason = "dedup_reasoning.distinction missing or blank"

    return {
        "status": status,
        "reason": reason,
        "distinction": distinction,
        "closest_directions": closest,
        "live_direction_ids": live_ids,
        "retired_direction_ids": retired_ids,
        "unresolved_closest_directions": unresolved,
    }
