"""Pure-math helpers for research actionability grade aggregation."""

from datetime import datetime
from typing import Optional

from app.findings_index import finalized_research_findings_from_state
from app.landed_signal import is_merged_completion
from app.models import TaskStatus

_MAX_ACTIONABILITY_CYCLES = 50


def _aggregate_actionability_extended(
    research_tasks_in_bucket: list,
    followon_tasks: list,
    research_task_ids: set,
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """Aggregate (grade_distribution, grade_tasked, grade_merged, grade_succeeded).

    Tracks both the merge-based code sub-metric (``grade_merged``) and the
    generalized forward-quality outcome: a finding's grade is counted in
    ``grade_succeeded`` when its direction's follow-on task achieved ANY terminal
    evaluation success — not just a PR merge, but also research quality,
    audit findings, memo artifacts, or verification verdicts (resolved via
    :func:`~app.task_outcomes.is_successful_outcome`).

    Reuses the same merge-attribution rules as the global aggregation: research
    tasks are excluded from follow-ons (self-attribution guard), and a follow-on
    must be created strictly after the bucket's earliest research task.
    """
    from app.task_outcomes import is_successful_outcome  # noqa: PLC0415

    finding_direction_ids: set[str] = set()
    findings_by_dir: dict[str, list[dict]] = {}
    grade_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    dir_research_first_at: dict[str, datetime] = {}

    for t in research_tasks_in_bucket:
        for f in finalized_research_findings_from_state(
            t.plan_output,
            t.context,
            status=TaskStatus.COMPLETED,
            task_type="research",
        ):
            if not f.get("title"):
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
        entry = dir_outcome.setdefault(
            key, {"tasked": False, "merged": False, "succeeded": False}
        )
        entry["tasked"] = True
        if is_merged_completion(t):
            entry["merged"] = True
        if is_successful_outcome(t):
            entry["succeeded"] = True

    grade_tasked: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    grade_merged: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    grade_succeeded: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for dir_id, findings in findings_by_dir.items():
        outcome = dir_outcome.get(dir_id, {})
        for f in findings:
            g = f["grade"]
            if outcome.get("tasked"):
                grade_tasked[g] = grade_tasked.get(g, 0) + 1
            if outcome.get("merged"):
                grade_merged[g] = grade_merged.get(g, 0) + 1
            if outcome.get("succeeded"):
                grade_succeeded[g] = grade_succeeded.get(g, 0) + 1

    return grade_counts, grade_tasked, grade_merged, grade_succeeded


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


def _compute_actionability_rates_extended(
    grade_counts: dict[str, int],
    grade_tasked: dict[str, int],
    grade_merged: dict[str, int],
    grade_succeeded: dict[str, int],
) -> dict[str, object]:
    """Derive both merge-rate (code sub-metric) and generalized success-rate.

    Returns a dict with:
    - ``grade_merge_rate``: per-grade rate of findings that led to a merged PR.
    - ``grade_merge_rate_of_tasked``: merge rate among tasked findings only.
    - ``calibration_score``: HIGH-rate minus LOW-rate (merge-based).
    - ``grade_success_rate``: per-grade rate of findings with ANY successful
      follow-on (PR merge OR successful non-code evaluation outcome).
    - ``generalized_calibration_score``: HIGH-success-rate minus LOW-success-rate.

    This is the extended form consumed by new analytics paths; existing callers
    use :func:`_compute_actionability_rates` for the 3-tuple form.
    """
    merge_rate, merge_rate_tasked, calibration = _compute_actionability_rates(
        grade_counts, grade_tasked, grade_merged
    )
    grade_success_rate: dict[str, Optional[float]] = {}
    for g in ("HIGH", "MEDIUM", "LOW"):
        gd = grade_counts.get(g, 0)
        gs = grade_succeeded.get(g, 0)
        grade_success_rate[g] = (gs / gd) if gd > 0 else None
    high_sr = grade_success_rate["HIGH"]
    low_sr = grade_success_rate["LOW"]
    generalized_calibration = (
        high_sr - low_sr if (high_sr is not None and low_sr is not None) else None
    )
    return {
        "grade_merge_rate": merge_rate,
        "grade_merge_rate_of_tasked": merge_rate_tasked,
        "calibration_score": calibration,
        "grade_success_rate": grade_success_rate,
        "generalized_calibration_score": generalized_calibration,
    }
