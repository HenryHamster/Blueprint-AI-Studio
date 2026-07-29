"""Pure normalization and state transitions for strategic stall advisories.

These records are evidence for later policy decisions.  They are intentionally
advisory: applying one never changes a direction's lifecycle or task state.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

STALL_ADVISORY_HISTORY_MAX = 20
STALL_ADVISORY_VERSION = 1
STALL_BLOCKING_INVARIANT_MAX = 500
STALL_EVIDENCE_ITEMS_MAX = 20
STALL_EVIDENCE_ITEM_MAX = 300
STALL_RATIONALE_MAX = 500
STALL_REPEAT_WARNING_THRESHOLD = 1
_VALID_RECOMMENDATIONS = frozenset({"continue", "pivot"})
_WHITESPACE = re.compile(r"\s+")


def normalize_blocking_invariant(value: str) -> str:
    """Normalize display text without changing case-sensitive identifiers."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())


def blocking_invariant_fingerprint(normalized_invariant: str) -> str:
    canonical = normalized_invariant.casefold()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_stall_advisory_fields(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize optional stall fields, returning None for legacy calls.

    Existing callers may continue to submit only ``status=stalled`` and a
    rationale.  Once any structured field is supplied, all four are required
    so a partial record cannot masquerade as decision-grade evidence.
    """
    field_names = {
        "blocking_invariant",
        "new_evidence_since_previous_cycle",
        "recommendation",
        "continuation_pivot_rationale",
    }
    has_version = "stall_advisory_version" in item
    if not has_version and not any(name in item for name in field_names):
        return None
    version = item.get("stall_advisory_version")
    if isinstance(version, bool) or version != STALL_ADVISORY_VERSION:
        raise ValueError(
            f"stall_advisory_version must be {STALL_ADVISORY_VERSION}"
        )
    missing = sorted(name for name in field_names if name not in item)
    if missing:
        raise ValueError(f"missing required stall advisory fields: {', '.join(missing)}")

    invariant_raw = item["blocking_invariant"]
    if not isinstance(invariant_raw, str):
        raise ValueError("blocking_invariant must be a string")
    if len(invariant_raw) > STALL_BLOCKING_INVARIANT_MAX:
        raise ValueError(
            f"blocking_invariant exceeds {STALL_BLOCKING_INVARIANT_MAX} characters"
        )
    invariant = normalize_blocking_invariant(invariant_raw)
    if not invariant:
        raise ValueError("blocking_invariant must be non-empty")

    raw_evidence = item["new_evidence_since_previous_cycle"]
    if not isinstance(raw_evidence, list):
        raise ValueError("new_evidence_since_previous_cycle must be a list")
    if len(raw_evidence) > STALL_EVIDENCE_ITEMS_MAX:
        raise ValueError(
            "new_evidence_since_previous_cycle exceeds "
            f"{STALL_EVIDENCE_ITEMS_MAX} items"
        )
    evidence: list[str] = []
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, str):
            raise ValueError(
                f"new_evidence_since_previous_cycle[{index}] must be a string"
            )
        if len(raw) > STALL_EVIDENCE_ITEM_MAX:
            raise ValueError(
                f"new_evidence_since_previous_cycle[{index}] exceeds "
                f"{STALL_EVIDENCE_ITEM_MAX} characters"
            )
        cleaned = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw).strip())
        if not cleaned:
            raise ValueError(
                f"new_evidence_since_previous_cycle[{index}] must be non-empty"
            )
        evidence.append(cleaned)

    recommendation_raw = item["recommendation"]
    if not isinstance(recommendation_raw, str):
        raise ValueError("recommendation must be a string")
    recommendation = recommendation_raw.strip().lower()
    if recommendation not in _VALID_RECOMMENDATIONS:
        raise ValueError("recommendation must be one of: continue, pivot")

    rationale_raw = item["continuation_pivot_rationale"]
    if not isinstance(rationale_raw, str):
        raise ValueError("continuation_pivot_rationale must be a string")
    if len(rationale_raw) > STALL_RATIONALE_MAX:
        raise ValueError(
            f"continuation_pivot_rationale exceeds {STALL_RATIONALE_MAX} characters"
        )
    rationale = _WHITESPACE.sub(
        " ", unicodedata.normalize("NFKC", rationale_raw).strip()
    )
    if not rationale:
        raise ValueError("continuation_pivot_rationale must be non-empty")

    return {
        "version": STALL_ADVISORY_VERSION,
        "blocking_invariant": invariant,
        "invariant_fingerprint": blocking_invariant_fingerprint(invariant),
        "new_evidence_since_previous_cycle": evidence,
        "recommendation": recommendation,
        "continuation_pivot_rationale": rationale,
    }


def apply_stall_advisory(
    direction: dict[str, Any],
    normalized: dict[str, Any],
    *,
    recorded_at: str,
    cycle_index: int,
    cycle_task_id: str,
) -> dict[str, Any]:
    """Persist one bounded advisory and return it.

    A repeat counts only when the exact normalized invariant is unchanged and
    the new-evidence list is empty.  New evidence or a changed invariant starts
    a fresh run.  The warning is data only; callers must not enforce it.
    """
    history = direction.get("stall_advisory_history")
    bounded_history = list(history) if isinstance(history, list) else []
    # Completion/recovery can replay a Strategic task after later cycles exist.
    # A prior observation from the same task is already authoritative: returning
    # it as-is preserves ordering, repeat semantics, and current/latest truth.
    for entry in bounded_history:
        if (
            isinstance(entry, dict)
            and entry.get("cycle_task_id") == cycle_task_id
        ):
            return entry
    prior_history = bounded_history
    previous = prior_history[-1] if prior_history else None
    same_invariant = (
        isinstance(previous, dict)
        and previous.get("invariant_fingerprint")
        == normalized["invariant_fingerprint"]
    )
    no_new_evidence = not normalized["new_evidence_since_previous_cycle"]
    previous_count = (
        int(previous.get("repeat_without_evidence_count") or 0)
        if isinstance(previous, dict)
        else 0
    )
    repeat_count = previous_count + 1 if same_invariant and no_new_evidence else 0
    record = {
        **normalized,
        "repeat_without_evidence_count": repeat_count,
        "warning": repeat_count >= STALL_REPEAT_WARNING_THRESHOLD,
        "recorded_at": recorded_at,
        "cycle_index": cycle_index,
        "cycle_task_id": cycle_task_id,
    }
    prior_history.append(record)
    direction["stall_advisory_history"] = prior_history[-STALL_ADVISORY_HISTORY_MAX:]
    direction["stall_advisory"] = record
    return record


def clear_current_stall_advisory(direction: dict[str, Any]) -> bool:
    """Clear current advisory state while retaining bounded historical evidence."""
    return direction.pop("stall_advisory", None) is not None
