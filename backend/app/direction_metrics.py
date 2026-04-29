"""Direction history metric and aggregation helpers.

Pure metric/aggregation functions extracted from app.research_completion.
These operate on direction/project data structures and have no coupling to the
research pipeline orchestration.
"""
from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified

from app.failure_classification import (
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
)

_HISTORY_AGG_DEFAULTS = {
    "total_cycles": 0,
    "total_tasks_created": 0,
    "total_prs_merged": 0,
    "total_prs_rejected": 0,
    "total_tasks_failed": 0,
    "total_tasks_cancelled": 0,
    "first_cycle_at": None,
    "total_autonomous_prs_merged": 0,
    "total_assisted_prs_merged": 0,
    "total_operator_actions": 0,
}

_DECOMP_AGG_DEFAULTS = {
    "total_decompositions": 0,
    "subtask_merge_rate_weighted_sum": 0.0,
    "subtask_count_sum": 0,
    "cross_module_count_sum": 0,
}


def _fold_evicted_into_aggregate(prev_agg: dict | None, evicted: list[dict]) -> dict:
    """Fold evicted research_history entries into a running history_aggregate.

    Preserves basic counters plus per-scope breakdown and decomposition-quality
    signals that _calibrate_scope and predict_spawn_success read. Without this,
    directions older than 10 cycles silently lost all scope/decomposition data.
    """
    agg = prev_agg or {}
    for k, v in _HISTORY_AGG_DEFAULTS.items():
        agg.setdefault(k, v)
    sb = agg.setdefault("scope_breakdown", {})
    dq = agg.setdefault("decomposition_quality", dict(_DECOMP_AGG_DEFAULTS))
    for ev in evicted:
        agg["total_cycles"] += 1
        agg["total_tasks_created"] += ev.get("tasks_created", 0)
        agg["total_prs_merged"] += ev.get("prs_merged", 0)
        agg["total_prs_rejected"] += ev.get("prs_rejected", 0)
        agg["total_tasks_failed"] += ev.get("tasks_failed", 0)
        agg["total_tasks_cancelled"] += ev.get("tasks_cancelled", 0)
        # Legacy fallback: pre-attribution entries have no autonomous_prs_merged;
        # treat all merges as autonomous so CPM is conservative for old data.
        agg["total_autonomous_prs_merged"] += ev.get("autonomous_prs_merged", ev.get("prs_merged", 0))
        agg["total_assisted_prs_merged"] += ev.get("assisted_prs_merged", 0)
        agg["total_operator_actions"] += ev.get("operator_action_count", 0)
        if not agg["first_cycle_at"] and ev.get("timestamp"):
            agg["first_cycle_at"] = ev["timestamp"]
        for scope, stats in (ev.get("scope_breakdown") or {}).items():
            bucket = sb.setdefault(scope, {
                "created": 0, "merged": 0, "failed": 0, "failed_infra": 0,
                "failed_scope_overflow": 0,
            })
            bucket["created"] += stats.get("created", 0)
            bucket["merged"] += stats.get("merged", 0)
            bucket["failed"] += stats.get("failed", 0)
            bucket["failed_infra"] += stats.get("failed_infra", 0)
            bucket["failed_scope_overflow"] = bucket.get("failed_scope_overflow", 0) + stats.get("failed_scope_overflow", 0)
        for qd in (ev.get("decomposition_quality") or []):
            dq["total_decompositions"] += 1
            count = qd.get("subtask_count") or 0
            dq["subtask_count_sum"] += count
            dq["cross_module_count_sum"] += qd.get("cross_module_count") or 0
            rate = qd.get("subtask_merge_rate")
            if isinstance(rate, (int, float)) and count > 0:
                dq["subtask_merge_rate_weighted_sum"] += float(rate) * count
    return agg


def _compute_momentum_factor(d: dict) -> float:
    """Return a momentum multiplier based on recent research history productivity."""
    history = d.get("research_history", [])
    if len(history) >= 2 and all(e.get("tasks_created", 0) > 0 for e in history[-2:]):
        return 2.0  # hot streak: last 2 cycles both productive
    if len(history) >= 3 and all(e.get("tasks_created", 0) == 0 for e in history[-3:]):
        return 0.5  # stalled: last 3 cycles all empty
    return 1.0  # neutral


def _compute_acceptance_rate(d: dict) -> float | None:
    """Return operator acceptance rate (merged / (merged + rejected)) with recency weighting.

    Weights the last 3 cycles 3x relative to the lifetime average, matching
    _compute_productivity_score's recency scheme. Returns None when no PRs exist (merged
    or rejected) so callers can distinguish "unknown" from a low rate.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (agg.get("total_prs_rejected") or 0)
    total_prs = total_merged + total_rejected
    if total_prs == 0:
        return None  # no data yet
    lifetime_rate = total_merged / total_prs
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(e.get("prs_merged", 0) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_prs = recent_merged + recent_rejected
    if recent_prs == 0:
        return lifetime_rate
    recent_rate = recent_merged / recent_prs
    return (recent_rate * 3 + lifetime_rate) / 4


def _compute_difficulty_index(d: dict) -> float:
    """Return tasks-per-merge ratio across direction lifetime.

    Higher values mean the direction needed more attempts per successful merge
    (empirically harder). Defaults to 1.0 when no merges have happened yet
    (treat as easy until proven otherwise) and is capped at 30 to avoid
    blowups from directions with many failures and zero merges.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_tasks = sum(e.get("tasks_created", 0) for e in history) + (agg.get("total_tasks_created") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    if total_merged <= 0:
        return 1.0
    return min(30.0, total_tasks / total_merged)


def _compute_productivity_score(d: dict) -> float:
    """Return net productivity score for a direction, with recency weighting.

    Accounts for both merged PRs (positive signal) and rejected PRs (negative
    signal from operator close-without-merge). Weights the last 3 cycles 3x
    relative to the lifetime average for responsiveness to recent changes.
    Returns 0.5 (neutral) if never researched.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (agg.get("total_prs_rejected") or 0)
    if total_cycles == 0:
        return 0.5  # unknown, neutral
    # Net productivity: merge contributes +1, rejection contributes -0.5
    # (rejection is penalized but not as harshly as a merge is rewarded)
    lifetime_rate = (total_merged - 0.5 * total_rejected) / total_cycles
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(e.get("prs_merged", 0) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_rate = (recent_merged - 0.5 * recent_rejected) / len(recent)
    return (recent_rate * 3 + lifetime_rate) / 4


def _compute_direction_quality_score(d: dict) -> float:
    """Aggregate recent taxonomy signals into a [0.0, 1.0] quality score.

    Reads the last 3 research_history entries and combines three signals:
    (a) taxonomy health — actionable failures (code_defect + scope_related) vs
        structural (infra_related + review_quality). Higher is better.
    (b) wasted-cycle signal — 1 - (cycles with tasks_created>0 but prs_merged==0
        over cycles with tasks_created>0). Higher is better.
    (c) scope calibration — 1.0 when scope_related does not dominate (>50%),
        else 0.0.

    Uses linear recency weighting within the 3-cycle window (oldest=1, newest=3),
    mirroring the 3x recency emphasis in _compute_productivity_score. Returns
    0.5 (neutral) when no history exists.
    """
    history = d.get("research_history", [])
    recent = history[-3:]
    if not recent:
        return 0.5

    weights = list(range(1, len(recent) + 1))  # oldest-of-window = 1x, newest = 3x
    actionable_w = structural_w = scope_w = total_taxon_w = 0.0
    active_w = wasted_w = 0.0

    for entry, w in zip(recent, weights):
        tx = entry.get("taxonomy_summary") or {}
        code_defect = tx.get(TAXONOMY_CODE_DEFECT, 0) or 0
        scope_related = tx.get(TAXONOMY_SCOPE_RELATED, 0) or 0
        infra_related = tx.get(TAXONOMY_INFRA_RELATED, 0) or 0
        review_quality = tx.get(TAXONOMY_REVIEW_QUALITY, 0) or 0
        actionable_w += w * (code_defect + scope_related)
        structural_w += w * (infra_related + review_quality)
        scope_w += w * scope_related
        total_taxon_w += w * (code_defect + scope_related + infra_related + review_quality)

        tasks_created = entry.get("tasks_created", 0) or 0
        prs_merged = entry.get("prs_merged", 0) or 0
        if tasks_created > 0:
            active_w += w
            if prs_merged == 0:
                wasted_w += w

    if (actionable_w + structural_w) > 0:
        taxonomy_health = actionable_w / (actionable_w + structural_w)
    else:
        taxonomy_health = 0.5

    if active_w > 0:
        productivity_signal = 1.0 - (wasted_w / active_w)
    else:
        productivity_signal = 0.5

    if total_taxon_w > 0:
        scope_signal = 0.0 if (scope_w / total_taxon_w) > 0.5 else 1.0
    else:
        scope_signal = 0.5

    score = (taxonomy_health + productivity_signal + scope_signal) / 3.0
    return max(0.0, min(1.0, score))


def _compute_rolling_median_review_iterations(d: dict) -> float | None:
    """Return the median of per-cycle median_review_iterations across the last 3 cycles.

    Reads median_review_iterations from each of the most recent research_history
    entries (populated by direction_outcomes). Returns None when no cycle in the
    window recorded a value, so callers can distinguish "unknown" from 0.
    """
    history = d.get("research_history", [])
    recent = history[-3:]
    values = [
        float(e["median_review_iterations"])
        for e in recent
        if isinstance(e.get("median_review_iterations"), (int, float))
    ]
    if not values:
        return None
    values.sort()
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _update_cycle_history(project: Any, cycle_id: str, updates: Dict[str, Any]) -> None:
    """Find a cycle entry by cycle_id in project.vision and apply updates in-place."""
    vision = dict(project.vision or {})
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    for entry in cycle_history:
        if entry.get("cycle_id") == cycle_id:
            entry.update(updates)
            break
    vision["_strategic_cycle_history"] = cycle_history
    project.vision = vision
    flag_modified(project, "vision")
