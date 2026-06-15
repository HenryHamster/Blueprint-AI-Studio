"""Pure-logic helpers for the visual-verification pilot kill-criterion audit.

These functions are dependency-free (no DB, no API calls) and can be used
both by the offline operator script (scripts/audit_tasks_prs.py) and by
backend tests that feed fixture task dicts directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

VISUAL_INFRA_SENTINEL = "VIEWPORT_GATE_INFRA_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Low-level field extractors
# ---------------------------------------------------------------------------


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def intish(value: Any) -> int:
    """Safely convert a value to a non-negative int."""
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def changed_files(task: dict[str, Any]) -> list[str]:
    """Return the list of file paths changed by this task, using all available sources."""
    ctx = task.get("context") or {}
    raw: list[Any] = ctx.get("_pr_changed_files") or task.get("changed_files") or []
    if not raw and isinstance(ctx.get("_file_change_details"), list):
        raw = [
            item.get("path")
            for item in ctx["_file_change_details"]
            if isinstance(item, dict)
        ]
    if not raw and isinstance(task.get("file_change_details"), list):
        raw = [
            item.get("path")
            for item in task["file_change_details"]
            if isinstance(item, dict)
        ]
    return [str(p) for p in raw if p]


def touches_admin_ui(task: dict[str, Any]) -> bool:
    """Return True if any changed file belongs to admin-ui."""
    return any(
        p == "admin-ui" or p.startswith("admin-ui/") or "/admin-ui/" in p
        for p in changed_files(task)
    )


def extract_visual_verification(
    task: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Return (visual_verification_entry, malformed).

    - entry is None when the task carries no visual_verification block.
    - malformed is True when a self_review_outcome was present but could not
      be parsed, signalling that the entry was dropped silently.
    """
    outcome = (task.get("context") or {}).get("_self_review_outcome") or task.get(
        "self_review_outcome"
    )
    if isinstance(outcome, str):
        try:
            outcome = json.loads(outcome)
        except json.JSONDecodeError:
            return None, True
    if not isinstance(outcome, dict):
        return None, False
    entry = outcome.get("visual_verification")
    # fall back to the (incorrect but historically observed) location inside check_results
    if entry is None and isinstance(outcome.get("check_results"), dict):
        entry = outcome["check_results"].get("visual_verification")
    if entry is not None:
        return (entry, not isinstance(entry, dict))
    return None, False


def infra_occurrences(entry: dict[str, Any]) -> int:
    """Count snapshot-infra-failure signals inside a visual_verification entry."""
    raw = entry.get("infra_failures") or entry.get("infra_failure_occurrences")
    if isinstance(raw, list):
        return len(raw)
    count = intish(raw)
    text = " ".join(
        str(entry.get(k, ""))
        for k in ("failure_class", "failure_reason", "issue", "evidence")
    )
    return max(
        count,
        1
        if VISUAL_INFRA_SENTINEL in text or "viewport infra-unavailable" in text.lower()
        else 0,
    )


# ---------------------------------------------------------------------------
# Aggregate result dataclass
# ---------------------------------------------------------------------------


@dataclass
class VisualPilotAggregates:
    """Aggregated kill-criterion inputs for the visual-verification pilot."""

    admin_task_count: int = 0
    entry_count: int = 0
    malformed_count: int = 0
    pairs_inspected: int = 0
    pairs_changed: int = 0
    infra_total: int = 0
    days_elapsed: int = 0
    caught: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)

    @property
    def infra_rate(self) -> float:
        return (self.infra_total / self.entry_count) if self.entry_count else 0.0


# ---------------------------------------------------------------------------
# Aggregate computation
# ---------------------------------------------------------------------------


def aggregate_visual_pilot(
    tasks: list[dict[str, Any]],
    *,
    created_after: str | None = None,
    created_before: str | None = None,
) -> VisualPilotAggregates:
    """Compute kill-criterion aggregate stats from a list of task dicts.

    Only COMPLETED tasks that touch admin-ui are included.  Tasks whose
    created_at is after *created_before* (when provided) are excluded via
    local filtering (the caller may have fetched more rows than the window
    to handle paging correctly).
    """
    before = _parse_iso(created_before)
    completed = [t for t in tasks if t.get("status") == "COMPLETED"]
    if before:
        completed = [
            t
            for t in completed
            if (
                _parse_iso(t.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            <= before
        ]

    admin_tasks = [t for t in completed if touches_admin_ui(t)]
    agg = VisualPilotAggregates(admin_task_count=len(admin_tasks))

    entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task in admin_tasks:
        entry, bad = extract_visual_verification(task)
        agg.malformed_count += int(bad)
        if entry:
            entries.append((task, entry))

    agg.entry_count = len(entries)
    agg.pairs_inspected = sum(intish(e.get("pairs_inspected")) for _, e in entries)
    agg.pairs_changed = sum(intish(e.get("pairs_changed")) for _, e in entries)
    agg.infra_total = sum(infra_occurrences(e) for _, e in entries)
    agg.caught = [(t, e) for t, e in entries if bool(e.get("caught_visual_issue"))]

    start = _parse_iso(created_after) or min(
        (_parse_iso(t.get("created_at")) for t, _ in entries),
        default=None,
    )
    agg.days_elapsed = (datetime.now(timezone.utc) - start).days if start else 0

    return agg
