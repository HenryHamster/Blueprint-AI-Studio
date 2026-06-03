"""Pure-math helpers for research actionability grade aggregation."""

from datetime import datetime
from typing import Optional

from app.landed_signal import is_merged_completion

_MAX_ACTIONABILITY_CYCLES = 50


def _aggregate_actionability_bucket(
    research_tasks_in_bucket: list,
    followon_tasks: list,
    research_task_ids: set,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Aggregate (grade_distribution, grade_tasked, grade_merged) for a bucket
    of research tasks. Reuses the same merge-attribution rules as the global
    aggregation: research tasks are excluded from follow-ons (self-attribution
    guard), and a follow-on must be created strictly after the bucket's
    earliest research task that produced the finding's direction.
    """
    finding_direction_ids: set[str] = set()
    findings_by_dir: dict[str, list[dict]] = {}
    grade_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    dir_research_first_at: dict[str, datetime] = {}

    for t in research_tasks_in_bucket:
        raw = t.plan_output
        if not isinstance(raw, list):
            continue
        for f in raw:
            if not isinstance(f, dict) or not f.get("title"):
                continue
            grade = f.get("actionability_grade", "MEDIUM")
            if grade not in grade_counts:
                grade = "MEDIUM"
            grade_counts[grade] += 1
            dir_id = f.get("direction_id")
            if dir_id:
                key = str(dir_id)
                finding_direction_ids.add(key)
                findings_by_dir.setdefault(key, []).append({"grade": grade})
                if t.created_at is not None:
                    prev = dir_research_first_at.get(key)
                    if prev is None or t.created_at < prev:
                        dir_research_first_at[key] = t.created_at

    dir_outcome: dict[str, dict] = {}
    for t in followon_tasks:
        if t.id in research_task_ids:
            continue
        t_type = (t.first_payload or {}).get("type")
        if t_type == "research":
            continue
        ctx = t.context or {}
        dir_id = ctx.get("direction_id") or ctx.get("related_direction_id")
        if not dir_id or str(dir_id) not in finding_direction_ids:
            continue
        key = str(dir_id)
        research_at = dir_research_first_at.get(key)
        if (
            research_at is not None
            and t.created_at is not None
            and t.created_at <= research_at
        ):
            continue
        entry = dir_outcome.setdefault(key, {"tasked": False, "merged": False})
        entry["tasked"] = True
        if is_merged_completion(t):
            entry["merged"] = True

    grade_tasked: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    grade_merged: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for dir_id, findings in findings_by_dir.items():
        outcome = dir_outcome.get(dir_id, {})
        for f in findings:
            g = f["grade"]
            if outcome.get("tasked"):
                grade_tasked[g] = grade_tasked.get(g, 0) + 1
            if outcome.get("merged"):
                grade_merged[g] = grade_merged.get(g, 0) + 1

    return grade_counts, grade_tasked, grade_merged


def _compute_actionability_rates(
    grade_counts: dict[str, int],
    grade_tasked: dict[str, int],
    grade_merged: dict[str, int],
) -> tuple[dict[str, Optional[float]], dict[str, Optional[float]], Optional[float]]:
    """Derive (grade_merge_rate, grade_merge_rate_of_tasked, calibration_score).

    Rates are None (not 0.0) when the denominator is 0 so the UI can
    distinguish 'no data' from 'zero merges'. calibration_score is HIGH-rate
    minus LOW-rate, or None when either is None.
    """
    grade_merge_rate: dict[str, Optional[float]] = {}
    grade_merge_rate_of_tasked: dict[str, Optional[float]] = {}
    for g in ("HIGH", "MEDIUM", "LOW"):
        gd = grade_counts.get(g, 0)
        gt = grade_tasked.get(g, 0)
        gm = grade_merged.get(g, 0)
        grade_merge_rate[g] = (gm / gd) if gd > 0 else None
        grade_merge_rate_of_tasked[g] = (gm / gt) if gt > 0 else None
    high_rate = grade_merge_rate["HIGH"]
    low_rate = grade_merge_rate["LOW"]
    calibration_score = (
        high_rate - low_rate
        if (high_rate is not None and low_rate is not None)
        else None
    )
    return grade_merge_rate, grade_merge_rate_of_tasked, calibration_score
