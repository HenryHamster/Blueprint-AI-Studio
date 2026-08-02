"""SR catch-rate analytics helpers and trend tracking.

Canonical home for the catch-rate family extracted from ``sr_analytics.py``.
Compatibility shims in ``sr_catch_rate_trends`` and ``sr_analytics`` re-export
these names for older imports.
"""

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_state import DEFAULT_TASK_SCOPE
from app.sr_analytics_common import (
    _EDGE_CATS,
    _MIN_DIRECTION_SR_SAMPLE,
    _SR_CODE_CHANGE_TYPES,
    _TREND_DELTA_THRESHOLD,
    _categorize_review_issues,
    _finalize_examples,
    _is_trend_significant,
)
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    is_skipped_sr_outcome,
    reviewer_confirmed_categories,
    safe_check_results,
)


def _author_edges(edges: list) -> list[dict[str, Any]]:
    """Return author evidence while ignoring retired auxiliary-review entries."""
    return [
        entry
        for entry in edges
        if isinstance(entry, dict) and entry.get("origin") != "reviewer"
    ]


def _compute_edges_stats(tasks: list) -> Optional[dict[str, Any]]:
    """Compute edges_considered rolling metrics for a task list.

    Returns None when fewer than 3 qualifying code-change tasks with
    ``_self_review_outcome`` exist. When sufficient data is present, returns::

        {
          "sample_size": int,
          "tasks_with_edges_rate": float,
          "avg_edges_per_task": float | None,
          "unhandled_edge_flagged_rate": float | None,
          "correctness_rejection_rate_with_edges": float | None,
          "correctness_rejection_rate_without_edges": float | None,
        }

    The last two fields correlate tasks that enumerated edges vs. those that
    didn't against subsequent correctness/error-handling reviewer flags,
    enabling validation that edges_considered actually improves catch rate.
    """
    code_tasks = [
        t
        for t in tasks
        if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
        and "_self_review_outcome" in (t.context or {})
    ]
    if len(code_tasks) < 3:
        return None

    with_edges: list = []
    without_edges: list = []
    with_unhandled_flag: list = []
    with_all_handled: list = []
    total_edges = 0
    unhandled_flagged = 0
    for t in code_tasks:
        edges = (t.context or {}).get("_self_review_outcome", {}).get(
            "edges_considered"
        ) or []
        if not isinstance(edges, list):
            edges = []
        edges = _author_edges(edges)
        if edges:
            if any(not e.get("handled", True) for e in edges):
                with_unhandled_flag.append(t)
            else:
                with_all_handled.append(t)
        if edges:
            with_edges.append(t)
            total_edges += len(edges)
            if any(not e.get("handled", True) for e in edges if isinstance(e, dict)):
                unhandled_flagged += 1
        else:
            without_edges.append(t)

    def _rejection_rate(task_list: list) -> Optional[float]:
        measured = [
            t
            for t in task_list
            if "_first_attempt_rejection_categories" in (t.context or {})
        ]
        if not measured:
            return None
        flagged = sum(
            1
            for t in measured
            if _EDGE_CATS
            & set(t.context.get("_first_attempt_rejection_categories") or [])
        )
        return round(flagged / len(measured), 3)

    n_with = len(with_edges)
    n_total = len(code_tasks)
    return {
        "sample_size": n_total,
        "tasks_with_edges_rate": round(n_with / n_total, 3),
        "avg_edges_per_task": round(total_edges / n_with, 2) if n_with else None,
        "unhandled_edge_flagged_rate": round(unhandled_flagged / n_with, 3)
        if n_with
        else None,
        "correctness_rejection_rate_with_edges": _rejection_rate(with_edges),
        "correctness_rejection_rate_without_edges": _rejection_rate(without_edges),
        "correctness_rejection_rate_with_unhandled_flag": _rejection_rate(
            with_unhandled_flag
        ),
        "correctness_rejection_rate_with_all_handled": _rejection_rate(
            with_all_handled
        ),
    }


def _stats_for_tasks(
    tasks: list,
) -> tuple[
    int,
    dict[str, dict],
    dict[str, dict[str, dict]],
    dict[str, dict],
    dict[str, dict],
    dict[str, dict[str, dict]],
]:
    """Compute SR stats for a pre-filtered task list (no date filtering).

    The "ground truth" these rates are scored against is the automated
    persisted reviewer-confirmed truth, not review-history reconstruction.
    Catch/miss/precision/recall here therefore measure author-SR-agent vs.
    reviewer-confirmed agreement — an inter-agent agreement signal, not a
    catch rate against persisted reviewer-confirmed truth. Variables are named
    ``reviewer_*`` accordingly. Internal ``*human_count`` aggregation keys are
    a legacy implementation detail retained so the stats builder can keep its
    existing shape while the serialized/UI contract exposes reviewer-grounded
    names.

    Deliberately-skipped self-reviews (the ``./done`` fallback envelope,
    detected via :func:`is_skipped_sr_outcome`) are EXCLUDED so a skip is not
    counted as a miss — matching the sibling reasoning-engagement and
    execution-evidence metrics. The separate skip rate is surfaced by
    :func:`_sr_skip_stats`.

    Returns (count, cat_stats, type_cat_stats, all_iter_cat_stats, sev_stats,
    scope_cat_stats). ``all_iter_cat_stats`` uses ALL review iterations as the
    reviewer-confirmed truth set (not just iteration 0). Falls back to
    first-attempt categories when ``_all_iterations_rejection_categories`` is
    absent (backward compat). ``sev_stats`` accumulates per-severity hit/miss
    counts from review_history issues across all categories. ``scope_cat_stats``
    mirrors ``type_cat_stats`` keyed by task scope (small|medium|large)
    instead of task type, using ``task.context['scope']`` (default: small).
    """
    _sw = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}

    count = 0
    cat_stats: dict[str, dict] = {}
    type_cat_stats: dict[str, dict[str, dict]] = {}
    all_iter_cat_stats: dict[str, dict] = {}
    sev_stats: dict[str, dict] = {}
    scope_cat_stats: dict[str, dict[str, dict]] = {}
    for t in tasks:
        ctx = t.context or {}
        if "_self_review_outcome" not in ctx:
            continue
        sro = ctx["_self_review_outcome"]
        # Exclude deliberately-skipped self-reviews: their check_results are
        # empty, so every reviewer-confirmed category would otherwise land in
        # the miss bucket, conflating "agent skipped review" with "agent
        # reviewed and missed". The skip rate is surfaced separately.
        if is_skipped_sr_outcome(sro):
            continue
        check_results = safe_check_results(sro)
        # Only author self-review signals count. Retired auxiliary-origin rows
        # remain readable but cannot inflate current catch-rate analytics.
        sr_failed = {
            c
            for c, r in check_results.items()
            if not r.get("passed", True) and r.get("origin") not in _REVIEWER_ORIGINS
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        cat_weights: dict[str, int] = {}
        rev_cats: dict[str, list[dict]] = {}
        if rev_hist:
            rev_cats = _categorize_review_issues(rev_hist)
            for cat_name, issues in rev_cats.items():
                cat_weights[cat_name] = max(_sw.get(i["severity"], 1) for i in issues)
        reviewer_confirmed_cats = reviewer_confirmed_categories(ctx)
        # First-attempt-only confirmed truth drives the ``current_hit_rate``
        # (first-attempt) metric below. It is intentionally NOT the
        # all-iterations-preferred ``reviewer_confirmed_cats``: a category the
        # reviewer raised only on a later iteration must surface in
        # ``all_iterations_catch_rate`` but stay absent from the first-attempt
        # metric, so the two rates can diverge as documented.
        first_attempt_cats = set(
            ctx.get("_first_attempt_rejection_categories") or {}
        ) - {"other"}

        # FP tracking: among SR-failed categories, count those never confirmed
        # by any reviewer-LLM verdict across all review iterations (first
        # rejection OR later). Runs for every task (even those with no
        # first-attempt rejection) because those are exactly the cases where SR
        # may have flagged a false positive. Uses the author-only view so
        # reviewer-stamped overrides are not falsely attributed to the author
        # SR's precision.
        all_reviewer_flagged = set(rev_cats.keys()) | reviewer_confirmed_cats
        for cat in sr_failed:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "human_count": 0,
                    "w_hit": 0.0,
                    "w_total": 0.0,
                    "miss_examples": [],
                    "sr_failed_count": 0,
                    "fp_count": 0,
                }
            cat_stats[cat].setdefault("sr_failed_count", 0)
            cat_stats[cat].setdefault("fp_count", 0)
            cat_stats[cat]["sr_failed_count"] += 1
            if cat not in all_reviewer_flagged:
                cat_stats[cat]["fp_count"] += 1

        if not reviewer_confirmed_cats:
            continue
        count += 1
        for cat_name, issues in rev_cats.items():
            for issue in issues:
                sev = issue.get("severity", "")
                if sev not in _sw:
                    continue
                if sev not in sev_stats:
                    sev_stats[sev] = {"hit_count": 0, "human_count": 0}
                sev_stats[sev]["human_count"] += 1
                if cat_name in sr_failed:
                    sev_stats[sev]["hit_count"] += 1
        task_type = t.first_payload.get("type", "")
        task_scope = (t.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        for cat in first_attempt_cats:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "human_count": 0,
                    "w_hit": 0.0,
                    "w_total": 0.0,
                    "miss_examples": [],
                    "sr_failed_count": 0,
                    "fp_count": 0,
                }
            cat_stats[cat]["human_count"] += 1
            w = cat_weights.get(cat, 1)
            cat_stats[cat]["w_total"] += w
            if cat in sr_failed:
                cat_stats[cat]["hit_count"] += 1
                cat_stats[cat]["w_hit"] += w
            else:
                for issue in rev_cats.get(cat, []):
                    cat_stats[cat]["miss_examples"].append(
                        {"desc": issue["desc"], "severity": issue["severity"]}
                    )
            if task_type:
                if task_type not in type_cat_stats:
                    type_cat_stats[task_type] = {}
                if cat not in type_cat_stats[task_type]:
                    type_cat_stats[task_type][cat] = {
                        "hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                type_cat_stats[task_type][cat]["human_count"] += 1
                if cat in sr_failed:
                    type_cat_stats[task_type][cat]["hit_count"] += 1
                else:
                    for issue in rev_cats.get(cat, []):
                        type_cat_stats[task_type][cat]["miss_examples"].append(
                            {"desc": issue["desc"], "severity": issue["severity"]}
                        )
            if task_scope:
                if task_scope not in scope_cat_stats:
                    scope_cat_stats[task_scope] = {}
                if cat not in scope_cat_stats[task_scope]:
                    scope_cat_stats[task_scope][cat] = {
                        "hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                scope_cat_stats[task_scope][cat]["human_count"] += 1
                if cat in sr_failed:
                    scope_cat_stats[task_scope][cat]["hit_count"] += 1
                else:
                    for issue in rev_cats.get(cat, []):
                        scope_cat_stats[task_scope][cat]["miss_examples"].append(
                            {"desc": issue["desc"], "severity": issue["severity"]}
                        )
        # All-iterations reviewer-confirmed truth: prefer the persisted set
        # written by task_lifecycle on every rejection iteration. If that field
        # is absent, fall back to the first-attempt categories only; do not
        # reconstruct truth from review_history.
        all_iter_cats = reviewer_confirmed_categories(ctx)
        for cat in all_iter_cats:
            if cat not in all_iter_cat_stats:
                all_iter_cat_stats[cat] = {"hit_count": 0, "human_count": 0}
            all_iter_cat_stats[cat]["human_count"] += 1
            if cat in sr_failed:
                all_iter_cat_stats[cat]["hit_count"] += 1
    return (
        count,
        cat_stats,
        type_cat_stats,
        all_iter_cat_stats,
        sev_stats,
        scope_cat_stats,
    )


def compute_sr_catch_rate_by_direction(
    all_tasks: list,
    *,
    existing_by_direction: Optional[dict] = None,
    is_new_window: bool = True,
    direction_text_map: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-direction SR catch-rate breakdown.

    Groups *all_tasks* by ``context.direction_id`` and, for each direction with
    at least :data:`_MIN_DIRECTION_SR_SAMPLE` qualifying tasks (both
    ``_self_review_outcome`` and ``_first_attempt_rejection_categories`` present),
    computes category-level miss rates and sample counts.

    When *existing_by_direction* is provided (a previously persisted
    ``by_direction`` snapshot), each entry is enriched with cycle-over-cycle
    trend fields: ``previous_miss_rate``, ``trend`` ('increasing' | 'decreasing'
    | 'stable'), and ``rate_history`` (list of recent miss_rates, capped at 5).
    *is_new_window* controls whether the current measurement is appended to
    ``rate_history`` (mirrors the per-category rate_history logic).

    When *direction_text_map* is provided, each entry gains a ``direction_text``
    field (truncated to 120 chars) so investigation prompts can reference the
    human-readable direction title without a secondary lookup.

    Returns a dict keyed by direction_id::

        {
          "d-abc123": {
            "sample_count": 5,
            "miss_rate": 0.6,
            "direction_text": "...",  # only when direction_text_map provided
            "categories": {
              "regression_risk": {
                "hit_rate": 0.4,
                "miss_rate": 0.6,
                "sample_count": 5,
                "examples": [...]   # only when misses exist
              },
              ...
            }
          },
          ...
        }

    Directions with fewer than _MIN_DIRECTION_SR_SAMPLE qualifying tasks are
    omitted (insufficient signal). Both directions and categories are sorted
    worst-first by miss rate for stable admin/UI rendering.
    """
    by_direction: dict[str, list] = {}
    for t in all_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            by_direction.setdefault(did, []).append(t)

    result: dict[str, dict[str, Any]] = {}
    for direction_id, tasks in by_direction.items():
        count, cat_stats, _, _, _, _ = _stats_for_tasks(tasks)
        if count < _MIN_DIRECTION_SR_SAMPLE:
            continue
        categories: dict[str, Any] = {}
        for cat, stats in cat_stats.items():
            if stats["human_count"] == 0:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            cat_entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
            }
            ex = _finalize_examples(stats.get("miss_examples", []))
            if ex:
                cat_entry["examples"] = ex
            categories[cat] = cat_entry
        if categories:
            categories = dict(
                sorted(
                    categories.items(),
                    key=lambda item: (-item[1]["miss_rate"], item[0]),
                )
            )
            total_samples = sum(entry["sample_count"] for entry in categories.values())
            weighted_miss_rate = (
                round(
                    sum(
                        entry["miss_rate"] * entry["sample_count"]
                        for entry in categories.values()
                    )
                    / total_samples,
                    3,
                )
                if total_samples
                else None
            )
            overall_catch_rate = (
                round(1 - weighted_miss_rate, 3)
                if weighted_miss_rate is not None
                else None
            )
            direction_entry: dict[str, Any] = {
                "sample_count": count,
                "miss_rate": weighted_miss_rate,
                "overall_catch_rate": overall_catch_rate,
                "categories": categories,
            }
            if direction_text_map:
                raw_text = direction_text_map.get(direction_id, "")
                if raw_text:
                    direction_entry["direction_text"] = raw_text[:120]
            if existing_by_direction is not None:
                prev_entry: dict = dict(existing_by_direction.get(direction_id) or {})
                prev_miss_rate = prev_entry.get("miss_rate")
                rate_history: list = list(prev_entry.get("rate_history") or [])
                if (
                    is_new_window or not rate_history
                ) and weighted_miss_rate is not None:
                    rate_history.append(weighted_miss_rate)
                    rate_history = rate_history[-5:]
                if prev_miss_rate is not None and weighted_miss_rate is not None:
                    delta = weighted_miss_rate - prev_miss_rate
                    # Convert miss rates to hit rates for significance test
                    curr_hit_rate = 1 - weighted_miss_rate
                    prev_hit_rate = 1 - prev_miss_rate
                    prev_sample_count = prev_entry.get("sample_count", 0)
                    # Apply two-proportion z-test; trend requires both significance AND delta
                    is_sig = _is_trend_significant(
                        curr_hit_rate, count, prev_hit_rate, prev_sample_count
                    )
                    if is_sig and delta >= _TREND_DELTA_THRESHOLD:
                        dir_trend = "increasing"
                    elif is_sig and delta <= -_TREND_DELTA_THRESHOLD:
                        dir_trend = "decreasing"
                    else:
                        dir_trend = "stable"
                else:
                    dir_trend = "stable"
                direction_entry["previous_miss_rate"] = prev_miss_rate
                direction_entry["trend"] = dir_trend
                direction_entry["rate_history"] = rate_history
            result[direction_id] = direction_entry
    return dict(
        sorted(result.items(), key=lambda item: (-(item[1]["miss_rate"] or 0), item[0]))
    )


def compute_direction_sr_breakdown_for_endpoint(
    all_tasks: list,
    direction_text_map: dict[str, str],
    *,
    existing_by_direction: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """Endpoint-shaped per-direction SR breakdown.

    Wraps :func:`compute_sr_catch_rate_by_direction` and decorates each entry
    with ``direction_text`` (looked up from *direction_text_map*), exposes
    ``sample_size`` (alias of canonical ``sample_count``), and adds
    ``worst_category`` derived from the canonical worst-first ordering of
    categories. Returned list preserves canonical worst-first direction
    ordering.
    """
    canonical = compute_sr_catch_rate_by_direction(
        all_tasks, existing_by_direction=existing_by_direction
    )
    out: list[dict[str, Any]] = []
    for direction_id, entry in canonical.items():
        categories = entry.get("categories") or {}
        worst_category = next(iter(categories), None)
        out.append(
            {
                "direction_id": direction_id,
                "direction_text": direction_text_map.get(direction_id, ""),
                "sample_size": entry["sample_count"],
                "overall_catch_rate": entry.get("overall_catch_rate"),
                "categories": categories,
                "worst_category": worst_category,
            }
        )
    return out


async def compute_sr_catch_rate_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Compare SR prediction hit-rate per category, bucketed by strategic cycle."""
    from app.sr_analytics_catch_rate_impl import _compute_sr_catch_rate_trends_impl

    return await _compute_sr_catch_rate_trends_impl(project_id, session, all_tasks)
