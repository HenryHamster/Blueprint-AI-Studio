"""Metric-computation helpers extracted from admin_digest.

Most are synchronous transforms over already-loaded Task / RegressionRemediationCase
data — no AsyncSession and no FastAPI Request. A small number (e.g.
``compute_remediation_funnel``) are async DB helpers that run their own self-contained
queries off a passed-in ``AsyncSession``. They are imported (and re-exported) by
``app.routers.admin_digest`` so existing call sites and test patch targets keep working.
"""

import logging
import statistics
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.time_utils import _to_naive_utc
from app.models import Project, Task, TaskStatus, RegressionRemediationCase, _utcnow
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    is_skipped_sr_outcome as _is_skipped_sr_outcome_shared,
)
from app.review_learning import _categorize_review_issues

logger = logging.getLogger(__name__)

_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}

_SR_SEVERITY_WEIGHTS: Dict[str, int] = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}

# Alias preserved for the moved helpers; admin_digest keeps its own re-export too.
_is_skipped_sr_outcome = _is_skipped_sr_outcome_shared

_SR_TREND_STALENESS_DAYS = 7

_CPM_RETIREMENT_MULTIPLIER = 3.0  # cost_per_merge > median × this → retirement signal

_LAYER_SUMMARY_WINDOW = timedelta(hours=48)

_REVIEW_ELIGIBLE_TYPES = {"create", "refactor"}


def _stash_projected_task_fields(
    task: Task,
    *,
    first_payload: Dict[str, Any] | None = None,
    completed_at_ts: str | None = None,
) -> None:
    """Attach SQL-projected task fields without going through Pydantic setattr.

    SQLModel ORM-loaded instances can be materialized without Pydantic private
    state, so assigning ad hoc underscore attributes via normal setattr can
    raise AttributeError on Pydantic v2. Store these transient projection
    values directly on the instance dict instead.
    """
    # Always set both attrs (even when the projection is None) so downstream
    # accessors take the projection branch instead of falling back to lazy-load
    # an unloaded JSONB column. _derive_completed_at already handles a None
    # cached_ts by returning task.created_at.
    object.__setattr__(task, "_projected_first_payload", first_payload or {})
    object.__setattr__(task, "_projected_completed_at_ts", completed_at_ts)


def _task_prompt_fingerprint(task: Task) -> str:
    """Resolve the task's prompt fingerprint with legacy context fallback."""
    if isinstance(task.prompt_fingerprint, str) and task.prompt_fingerprint:
        return task.prompt_fingerprint
    ctx = task.context or {}
    legacy = ctx.get("prompt_fingerprint")
    if isinstance(legacy, str) and legacy:
        return legacy
    return "unknown"


def _author_sr_failed_categories(check_results: Dict[str, Any]) -> set[str]:
    """Return categories the author self-review actually failed.

    Reviewer-stamped check_results entries are intentionally excluded so
    reviewer rescues are not misattributed as author catches.
    """
    return {
        category
        for category, result in (check_results or {}).items()
        if isinstance(result, dict)
        and not result.get("passed", True)
        and result.get("origin") not in _REVIEWER_ORIGINS
    }


def _serialize_sr_catch_rate_trends(sr_trends: Dict[str, Any]) -> Dict[str, Any]:
    computed_at_str = sr_trends.get("computed_at")
    if computed_at_str:
        try:
            computed_at_dt = datetime.fromisoformat(computed_at_str)
            is_stale = computed_at_dt < datetime.utcnow() - timedelta(
                days=_SR_TREND_STALENESS_DAYS
            )
        except (ValueError, TypeError):
            is_stale = True
    else:
        is_stale = True

    serialized = {
        "computed_at": computed_at_str,
        "signal_last_computed_at": sr_trends.get("signal_last_computed_at")
        or computed_at_str,
        "categories": sr_trends.get("categories") or {},
        "by_task_type": sr_trends.get("by_task_type") or {},
        "by_fix_iteration": sr_trends.get("by_fix_iteration") or {},
        "is_stale": is_stale,
        # Significance-gated overall trend fields — computed by compute_sr_catch_rate_trends
        # using the same two-proportion z-test as per-category trends. Explicitly promoted
        # here so callers can rely on their presence rather than the setdefault passthrough.
        "overall_trend_verdict": sr_trends.get("overall_trend_verdict"),
        "overall_trend_significant": sr_trends.get("overall_trend_significant"),
        "overall_trend_data_sufficient": sr_trends.get("overall_trend_data_sufficient"),
        # Author-only (SR-alone) overall rates and significance-gated trend — mirrors the
        # combined overall fields above but uses only catches from origin='sr', excluding
        # fresh-reviewer rescues. Promoted explicitly so callers can detect when the author
        # self-review is regressing even while the combined gate looks healthy.
        "overall_sr_alone_hit_rate": sr_trends.get("overall_sr_alone_hit_rate"),
        "previous_overall_sr_alone_hit_rate": sr_trends.get(
            "previous_overall_sr_alone_hit_rate"
        ),
        "overall_sr_alone_trend_verdict": sr_trends.get(
            "overall_sr_alone_trend_verdict"
        ),
        "overall_sr_alone_trend_significant": sr_trends.get(
            "overall_sr_alone_trend_significant"
        ),
        "overall_sr_alone_trend_data_sufficient": sr_trends.get(
            "overall_sr_alone_trend_data_sufficient"
        ),
    }
    for key, value in sr_trends.items():
        serialized.setdefault(key, value)
    return serialized


def _compute_sr_skip_rate(terminal_tasks: List[Task]) -> "Dict[str, Any] | None":
    """Share of code-change tasks (create/fix/refactor/migrate) where the
    self-review JSON was missing or invalid. Detects the bash ./done
    fallback envelope. Returns None when sample size < 5.
    """
    code_change_tasks: List[Task] = []
    for t in terminal_tasks:
        ttype = (t.first_payload or {}).get("type")
        if ttype in _SR_CODE_CHANGE_TYPES:
            code_change_tasks.append(t)

    if len(code_change_tasks) < 5:
        return None

    skipped_count = 0
    by_type: Dict[str, Dict[str, int]] = {}
    for t in code_change_tasks:
        ttype = (t.first_payload or {}).get("type")
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        is_skip = _is_skipped_sr_outcome(sro)
        bucket = by_type.setdefault(ttype, {"skipped": 0, "total": 0})
        bucket["total"] += 1
        if is_skip:
            skipped_count += 1
            bucket["skipped"] += 1

    by_task_type: Dict[str, Dict[str, Any]] = {}
    for ttype, counts in by_type.items():
        total = counts["total"]
        if total < 5:
            continue
        by_task_type[ttype] = {
            "rate": round(counts["skipped"] / total, 3),
            "skipped_count": counts["skipped"],
            "sample_size": total,
        }

    sample_size = len(code_change_tasks)
    return {
        "overall_rate": round(skipped_count / sample_size, 3),
        "skipped_count": skipped_count,
        "sample_size": sample_size,
        "by_task_type": by_task_type,
    }


def _compute_sr_disallowed_blind_spot_rate(
    terminal_tasks: List[Task],
) -> "Dict[str, Any] | None":
    """Share of code-change tasks where the agent emitted at least one disallowed
    blind-spot phrase (captured as ``_filtered_blind_spots_count > 0`` by
    ``filter_disallowed_blind_spots``). Returns None when sample size < 5.
    """
    code_change_tasks: List[Task] = []
    for t in terminal_tasks:
        ttype = (t.first_payload or {}).get("type")
        if ttype in _SR_CODE_CHANGE_TYPES:
            code_change_tasks.append(t)

    if len(code_change_tasks) < 5:
        return None

    violation_count = 0
    by_type: Dict[str, Dict[str, int]] = {}
    for t in code_change_tasks:
        ttype = (t.first_payload or {}).get("type")
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome") or {}
        has_violation = (sro.get("_filtered_blind_spots_count") or 0) > 0
        bucket = by_type.setdefault(ttype, {"violations": 0, "total": 0})
        bucket["total"] += 1
        if has_violation:
            violation_count += 1
            bucket["violations"] += 1

    by_task_type: Dict[str, Dict[str, Any]] = {}
    for ttype, counts in by_type.items():
        total = counts["total"]
        if total < 5:
            continue
        by_task_type[ttype] = {
            "rate": round(counts["violations"] / total, 3),
            "violation_count": counts["violations"],
            "sample_size": total,
        }

    sample_size = len(code_change_tasks)
    return {
        "overall_rate": round(violation_count / sample_size, 3),
        "violation_count": violation_count,
        "sample_size": sample_size,
        "by_task_type": by_task_type,
    }


def _compute_fresh_reviewer_effectiveness(
    terminal_tasks: List[Task],
) -> "Dict[str, Any]":
    """Compute fresh-reviewer pass effectiveness: approval rate lift and per-category
    hit rate (did human confirm what fresh-reviewer flagged?). Always returns a dict
    so callers can check sample_size before trusting rates.
    """
    code_change_tasks = [
        t
        for t in terminal_tasks
        if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_with_reviewer = [
        t
        for t in code_change_tasks
        if (t.context or {}).get("_self_review_outcome", {}).get("reviewer_findings")
    ]
    tasks_without_reviewer = [
        t
        for t in code_change_tasks
        if not (t.context or {})
        .get("_self_review_outcome", {})
        .get("reviewer_findings")
    ]

    def _fa_rate(tasks: List[Task]) -> "float | None":
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        return round(
            sum(1 for t in measured if t.context["_first_attempt_succeeded"])
            / len(measured),
            2,
        )

    tasks_with_both = [
        t
        for t in tasks_with_reviewer
        if "_first_attempt_rejection_categories" in (t.context or {})
    ]
    cat_flagged: Dict[str, int] = {}
    cat_confirmed: Dict[str, int] = {}
    cat_added_value: Dict[str, int] = {}
    for t in tasks_with_both:
        ctx = t.context or {}
        findings = ctx.get("_self_review_outcome", {}).get("reviewer_findings") or []
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
        check_results = ctx.get("_self_review_outcome", {}).get("check_results") or {}
        agent_failed = _author_sr_failed_categories(check_results)
        for finding in findings:
            cat = finding.get("category") if isinstance(finding, dict) else None
            if not cat:
                continue
            cat_flagged[cat] = cat_flagged.get(cat, 0) + 1
            if cat in human_cats:
                cat_confirmed[cat] = cat_confirmed.get(cat, 0) + 1
            if cat not in agent_failed and cat in human_cats:
                cat_added_value[cat] = cat_added_value.get(cat, 0) + 1

    return {
        "fresh_reviewer_sample_size": len(tasks_with_reviewer),
        "fresh_reviewer_first_attempt_approval_rate": _fa_rate(tasks_with_reviewer),
        "baseline_first_attempt_approval_rate": _fa_rate(tasks_without_reviewer),
        "per_category_hit_rate": {
            cat: round(cat_confirmed.get(cat, 0) / total, 3)
            for cat, total in cat_flagged.items()
            if total >= 2
        },
        "reviewer_finding_added_value": dict(cat_added_value),
        "prediction_sample_size": len(tasks_with_both),
    }


def _sr_miss_drilldown(
    sr_outcome: Dict[str, Any], missed_categories: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    check_results = sr_outcome.get("check_results") or {}
    sr_check_results = {}
    for cat in missed_categories:
        result = check_results.get(cat)
        if isinstance(result, dict):
            sr_check_results[cat] = {
                "passed": bool(result.get("passed", True)),
                "issue": result.get("issue"),
                "origin": result.get("origin"),
            }
    reviewer_findings = [
        {
            "category": finding.get("category"),
            "severity": finding.get("severity"),
            "description": (finding.get("description") or "")[:400],
        }
        for finding in (sr_outcome.get("reviewer_findings") or [])
        if isinstance(finding, dict) and finding.get("category") in missed_categories
    ]
    return sr_check_results, reviewer_findings


def _partition_edges_by_origin(edges: List[Any]) -> "tuple[list, list]":
    """Split an edges_considered list into (author, reviewer) entries.

    Entries without an explicit ``origin`` field are treated as author entries.
    Non-dict entries are dropped (matches the existing read pattern in
    aggregation code which silently skips malformed entries).
    """
    author: list = []
    reviewer: list = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if e.get("origin") == "reviewer":
            reviewer.append(e)
        else:
            author.append(e)
    return author, reviewer


def _reviewer_caught_author_miss(author_entries: list, reviewer_entries: list) -> bool:
    """Return True iff at least one reviewer entry catches what the author missed.

    A reviewer catch is one of:
      * reviewer raises a ``case`` not present in the author's entries (mismatch
        compared by lowercased + trimmed case strings); or
      * reviewer flags ``handled: false`` for a case the author had marked
        ``handled: true`` (default-True when missing).

    Entries on either side missing/non-string ``case`` are skipped — we cannot
    correlate them, and per spec we skip rather than crash.
    """

    def _norm(c: Any) -> "str | None":
        return c.strip().lower() if isinstance(c, str) and c.strip() else None

    author_handled: dict[str, bool] = {}
    for e in author_entries:
        c = _norm(e.get("case"))
        if c is None:
            continue
        h = bool(e.get("handled", True))
        author_handled[c] = author_handled.get(c, False) or h

    for e in reviewer_entries:
        c = _norm(e.get("case"))
        if c is None:
            continue
        if c not in author_handled:
            return True
        if author_handled[c] and not bool(e.get("handled", True)):
            return True
    return False


def _compute_edges_considered_metrics(
    terminal_tasks: List[Task],
) -> "Dict[str, Any] | None":
    """Rolling metrics for the edges_considered field in self-review outcomes.

    Computes avg edges enumerated per task and unhandled-edge-flagged rate.
    Correlates tasks with vs. without enumerated edges against correctness/
    error-handling reviewer rejections to validate field utility.

    Author vs reviewer breakdowns: entries may now carry ``origin: "reviewer"``
    (from the fresh-reviewer pass) or ``origin: "author"`` (or no origin, the
    legacy default). The existing ``avg_edges_per_task`` /
    ``tasks_with_edges_rate`` / ``unhandled_edge_flagged_rate`` keys describe
    the COMBINED edges array. New per-origin keys break out author vs reviewer
    contributions and report ``reviewer_caught_count`` — tasks where the
    reviewer raised an edge the author missed.

    Returns None when fewer than 3 qualifying code-change tasks exist.
    """
    code_change_tasks = [
        t
        for t in terminal_tasks
        if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
        and "_self_review_outcome" in (t.context or {})
    ]
    if len(code_change_tasks) < 3:
        return None

    tasks_with_edges: List[Task] = []
    tasks_without_edges: List[Task] = []
    tasks_with_unhandled_flag: List[Task] = []
    tasks_with_all_handled: List[Task] = []
    total_edges = 0
    unhandled_flagged = 0
    total_author_edges = 0
    total_reviewer_edges = 0
    n_with_author = 0
    n_with_reviewer = 0
    reviewer_caught = 0
    for t in code_change_tasks:
        edges = (t.context or {}).get("_self_review_outcome", {}).get(
            "edges_considered"
        ) or []
        if not isinstance(edges, list):
            edges = []
        author_e, reviewer_e = _partition_edges_by_origin(edges)
        total_author_edges += len(author_e)
        total_reviewer_edges += len(reviewer_e)
        if author_e:
            n_with_author += 1
            if any(not e.get("handled", True) for e in author_e):
                tasks_with_unhandled_flag.append(t)
            else:
                tasks_with_all_handled.append(t)
        if reviewer_e:
            n_with_reviewer += 1
            if _reviewer_caught_author_miss(author_e, reviewer_e):
                reviewer_caught += 1
        if edges:
            tasks_with_edges.append(t)
            total_edges += len(edges)
            if any(not e.get("handled", True) for e in edges if isinstance(e, dict)):
                unhandled_flagged += 1
        else:
            tasks_without_edges.append(t)

    _edge_cats = {"correctness", "error-handling"}

    def _rejection_rate(tasks: List[Task]) -> "float | None":
        measured = [
            t
            for t in tasks
            if "_first_attempt_rejection_categories" in (t.context or {})
        ]
        if not measured:
            return None
        flagged = sum(
            1
            for t in measured
            if _edge_cats
            & set(t.context.get("_first_attempt_rejection_categories") or [])
        )
        return round(flagged / len(measured), 3)

    n_with = len(tasks_with_edges)
    sample_size = len(code_change_tasks)
    return {
        "sample_size": sample_size,
        "tasks_with_edges_count": n_with,
        "tasks_with_edges_rate": round(n_with / sample_size, 3),
        "avg_edges_per_task": round(total_edges / n_with, 2) if n_with else None,
        "unhandled_edge_flagged_rate": round(unhandled_flagged / n_with, 3)
        if n_with
        else None,
        "correctness_rejection_rate_with_edges": _rejection_rate(tasks_with_edges),
        "correctness_rejection_rate_without_edges": _rejection_rate(
            tasks_without_edges
        ),
        "correctness_rejection_rate_with_unhandled_flag": _rejection_rate(
            tasks_with_unhandled_flag
        ),
        "correctness_rejection_rate_with_all_handled": _rejection_rate(
            tasks_with_all_handled
        ),
        "author_avg_edges_per_task": round(total_author_edges / n_with_author, 2)
        if n_with_author
        else None,
        "reviewer_avg_edges_per_task": round(total_reviewer_edges / n_with_reviewer, 2)
        if n_with_reviewer
        else None,
        "tasks_with_reviewer_edges_count": n_with_reviewer,
        "tasks_with_reviewer_edges_rate": round(n_with_reviewer / sample_size, 3),
        "reviewer_caught_count": reviewer_caught,
        "reviewer_caught_rate": round(reviewer_caught / sample_size, 3),
    }


def compute_sr_prediction_accuracy(
    tasks_with_both: List[Task],
) -> "Dict[str, Any] | None":
    """Compute self-review prediction accuracy from tasks that have both
    _self_review_outcome.check_results and _first_attempt_rejection_categories.
    Returns None when sample size < 5.
    """
    if len(tasks_with_both) < 5:
        return None
    total_human = total_overlap = total_missed = total_false = total_sr_failed = 0
    w_caught = w_missed = 0.0
    self_flagged_task_count = 0
    tasks_with_rejections = 0
    for t in tasks_with_both:
        ctx = t.context or {}
        check_results = ctx.get("_self_review_outcome", {}).get("check_results") or {}
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
        sr_failed = _author_sr_failed_categories(check_results)
        total_human += len(human_cats)
        total_overlap += len(human_cats & sr_failed)
        total_missed += len(human_cats - sr_failed)
        total_false += len(sr_failed - human_cats)
        total_sr_failed += len(sr_failed)
        if human_cats:
            tasks_with_rejections += 1
            if human_cats & sr_failed:
                self_flagged_task_count += 1
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        cat_weights: Dict[str, int] = {}
        cached_entry = ctx.get("_first_attempt_categorized_issues")
        cached_buckets = (cached_entry or {}).get("buckets") if cached_entry else None
        if cached_buckets:
            for cat, issues in _categorize_review_issues(
                rev_hist, cached=cached_buckets
            ).items():
                cat_weights[cat] = max(
                    _SR_SEVERITY_WEIGHTS.get(i["severity"], 1) for i in issues
                )
        elif rev_hist:
            for cat, issues in _categorize_review_issues(rev_hist).items():
                cat_weights[cat] = max(
                    _SR_SEVERITY_WEIGHTS.get(i["severity"], 1) for i in issues
                )
        for cat in human_cats:
            w = cat_weights.get(cat, 1)
            if cat in sr_failed:
                w_caught += w
            else:
                w_missed += w
    total_w = w_caught + w_missed
    return {
        "prediction_hit_rate": round(total_overlap / total_human, 3)
        if total_human > 0
        else None,
        "miss_rate": round(total_missed / total_human, 3) if total_human > 0 else None,
        "false_alarm_rate": round(total_false / total_sr_failed, 3)
        if total_sr_failed > 0
        else None,
        "weighted_hit_rate": round(w_caught / total_w, 3) if total_w > 0 else None,
        "weighted_miss_rate": round(w_missed / total_w, 3) if total_w > 0 else None,
        "sample_size": len(tasks_with_both),
        # Tasks where SR already flagged the issue but the agent submitted without fixing it.
        "self_flagged_shipped_unfixed_task_count": self_flagged_task_count,
        "self_flagged_shipped_unfixed_task_rate": round(
            self_flagged_task_count / tasks_with_rejections, 3
        )
        if tasks_with_rejections > 0
        else None,
    }


def compute_review_quality_metrics(terminal_tasks: List[Task]) -> Dict[str, Any]:
    """Compute self-review effectiveness, prediction accuracy, and review accuracy
    from a list of terminal tasks. Returns dict with three keys, each None when
    sample sizes are too small.
    """

    def _fa_rate(tasks):
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        succ = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(succ / len(measured), 2)

    tasks_with_sr = [
        t for t in terminal_tasks if "_self_review_outcome" in (t.context or {})
    ]
    tasks_without_sr = [
        t for t in terminal_tasks if "_self_review_outcome" not in (t.context or {})
    ]
    sr_clean = [
        t
        for t in tasks_with_sr
        if not (t.context or {})
        .get("_self_review_outcome", {})
        .get("fixes_applied", False)
    ]
    sr_fixed = [
        t
        for t in tasks_with_sr
        if (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]

    self_review_effectiveness: Dict[str, Any] | None = None
    if len(tasks_with_sr) >= 3:
        self_review_effectiveness = {
            "with_self_review_approval_rate": _fa_rate(tasks_with_sr),
            "without_self_review_approval_rate": _fa_rate(tasks_without_sr),
            "clean_verdict_approval_rate": _fa_rate(sr_clean),
            "fixed_verdict_approval_rate": _fa_rate(sr_fixed),
            "sample_size": len(tasks_with_sr),
            "sr_clean_count": len(sr_clean),
            "sr_fixed_count": len(sr_fixed),
        }

    review_tasks = [
        t
        for t in terminal_tasks
        if (t.first_payload or {}).get("type") == "review"
        and (t.context or {}).get("blueprint_review_verdict") is not None
    ]
    review_accuracy: Dict[str, Any] | None = None
    if len(review_tasks) >= 5:
        total = len(review_tasks)
        overrides = sum(
            1 for t in review_tasks if (t.context or {}).get("user_overrode_review")
        )
        dismissals = sum(
            1 for t in review_tasks if (t.context or {}).get("review_dismissed")
        )
        misses = sum(
            1 for t in review_tasks if (t.context or {}).get("user_caught_issues")
        )
        approvals = sum(
            1
            for t in review_tasks
            if (t.context or {}).get("blueprint_review_verdict") == "APPROVE"
        )
        review_accuracy = {
            "total_reviews": total,
            "overrides": overrides,
            "dismissals": dismissals,
            "misses": misses,
            "total_approvals": approvals,
            "precision": round(1 - (overrides + dismissals) / total, 2),
            "recall": round(1 - misses / approvals, 2) if approvals > 0 else None,
        }

    tasks_with_both = [
        t
        for t in terminal_tasks
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and "_first_attempt_rejection_categories" in (t.context or {})
    ]
    self_review_prediction_accuracy = compute_sr_prediction_accuracy(tasks_with_both)
    fresh_reviewer_effectiveness = _compute_fresh_reviewer_effectiveness(terminal_tasks)
    edges_considered_metrics = _compute_edges_considered_metrics(terminal_tasks)

    return {
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "review_accuracy": review_accuracy,
        "fresh_reviewer_effectiveness": fresh_reviewer_effectiveness,
        "edges_considered_metrics": edges_considered_metrics,
    }


def _compute_first_attempt_breakdowns(
    terminal_tasks: List[Task],
    prev_terminal_tasks: List[Task] | None = None,
) -> Dict[str, Any]:
    """Compute overall first-attempt success rate plus playbook and arch-context
    effectiveness comparisons from a list of terminal tasks."""

    def _fa_rate(tasks):
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        succ = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(succ / len(measured), 2)

    measured = [
        t for t in terminal_tasks if "_first_attempt_succeeded" in (t.context or {})
    ]
    with_pb = [t for t in measured if (t.context or {}).get("_had_playbook") is True]
    without_pb = [
        t for t in measured if (t.context or {}).get("_had_playbook") is False
    ]
    with_arch = [
        t for t in measured if (t.context or {}).get("_had_arch_context") is True
    ]
    without_arch = [
        t for t in measured if (t.context or {}).get("_had_arch_context") is False
    ]
    sample_size = len(measured)

    by_type_groups: Dict[str, list] = {}
    for t in terminal_tasks:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type_groups.setdefault(task_type, []).append(t)
    by_type: Dict[str, float] = {}
    for task_type, tasks in by_type_groups.items():
        rate = _fa_rate(tasks)
        if rate is not None:
            by_type[task_type] = rate

    by_scope_groups: Dict[str, list] = {}
    for t in terminal_tasks:
        scope = (t.context or {}).get("scope", "unknown")
        by_scope_groups.setdefault(scope, []).append(t)
    by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope_groups.items():
        rate = _fa_rate(tasks)
        if rate is not None:
            by_scope[scope] = rate

    overall = _fa_rate(terminal_tasks)
    trend: str | None = None
    if prev_terminal_tasks is not None:
        prev_rate = _fa_rate(prev_terminal_tasks)
        if overall is not None and prev_rate is not None:
            delta = overall - prev_rate
            if delta > 0.05:
                trend = "improving"
            elif delta < -0.05:
                trend = "declining"
            else:
                trend = "stable"

    first_attempt_by_review_category: Dict[str, float] | None = None
    failed_with_cats = [
        t
        for t in terminal_tasks
        if "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if failed_with_cats:
        total_with_cats = len(failed_with_cats)
        cat_counts: Dict[str, int] = {}
        for t in failed_with_cats:
            cats = (t.context or {}).get("_first_attempt_rejection_categories", {})
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        first_attempt_by_review_category = {
            cat: round(count / total_with_cats, 2)
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
        }

    return {
        "overall": overall,
        "with_playbook": _fa_rate(with_pb),
        "without_playbook": _fa_rate(without_pb),
        "with_arch_context": _fa_rate(with_arch),
        "without_arch_context": _fa_rate(without_arch),
        "sample_size": sample_size,
        "by_type": by_type,
        "by_scope": by_scope,
        "trend": trend,
        "first_attempt_by_review_category": first_attempt_by_review_category,
    }


def _summarize_symptoms_by_layer(
    symptoms_by_layer: Dict[str, Any] | None,
    *,
    now: datetime,
    window: timedelta = _LAYER_SUMMARY_WINDOW,
    fingerprint_to_fix_log: list[Any] | None = None,
) -> Dict[str, Any]:
    """Compact per-layer summary of recent regression-case symptom observations.

    Reads the JSONB shape ``{layer: [{fingerprint, observed_at, sample}, ...]}``
    populated by the pipeline regression dispatcher. Returns layers with at
    least one observation inside ``window`` (default 48h), sorted by
    most-recent ``last_observed_at`` first; cross_layer is true when >=2
    layers have non-empty entries within the window.

    When ``fingerprint_to_fix_log`` is supplied, the response also carries
    ``layer_mismatch``, ``prior_fix_layers``, and ``current_symptom_layers``
    so the admin UI can flag cases where prior fixes never targeted the
    layer where the regression is currently surfacing. ``layer_mismatch``
    is True only when both sides are non-empty AND fully disjoint.
    """
    cutoff = now - window
    layers_out: list[dict[str, Any]] = []
    if isinstance(symptoms_by_layer, dict):
        for layer, entries in symptoms_by_layer.items():
            if not isinstance(entries, list):
                continue
            recent: list[tuple[datetime, dict[str, Any]]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    obs_dt = datetime.fromisoformat(
                        str(entry.get("observed_at")).replace("Z", "+00:00")
                    )
                except (TypeError, ValueError):
                    continue
                if obs_dt.tzinfo is not None:
                    obs_dt = obs_dt.replace(tzinfo=None)
                if obs_dt >= cutoff:
                    recent.append((obs_dt, entry))
            if not recent:
                continue
            recent.sort(key=lambda p: p[0])
            latest_dt, latest_entry = recent[-1]
            distinct_fps = {
                e.get("fingerprint") for _, e in recent if e.get("fingerprint")
            }
            layers_out.append(
                {
                    "layer": layer,
                    "count": len(recent),
                    "distinct_fingerprints": len(distinct_fps),
                    "last_observed_at": latest_dt.isoformat(),
                    "sample": str(latest_entry.get("sample") or "")[:200],
                }
            )
    layers_out.sort(key=lambda d: d["last_observed_at"], reverse=True)
    affected = sorted(d["layer"] for d in layers_out)
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        _layers_targeted_union,
    )

    prior_set = _layers_targeted_union(fingerprint_to_fix_log)
    affected_set = set(affected)
    layer_mismatch = (
        bool(prior_set) and bool(affected_set) and prior_set.isdisjoint(affected_set)
    )
    return {
        "layers": layers_out,
        "layers_affected": affected,
        "cross_layer": len(affected) >= 2,
        "window_hours": int(window.total_seconds() // 3600),
        "layer_mismatch": layer_mismatch,
        "prior_fix_layers": sorted(prior_set),
        "current_symptom_layers": affected,
    }


def _compute_layer_coverage_by_class(
    rows: List[RegressionRemediationCase],
) -> Dict[str, Dict[str, Any]]:
    """Per-class histogram of which layer-combinations remediation PRs targeted.

    Aggregates over fingerprint_to_fix_log entries that have ``layers_targeted``
    populated. Entries that lack the key are treated as unclassified and
    excluded from the histogram (they neither raise nor lower the count).

    Returns a mapping ``regression_class -> {total_fixes_with_layers, combinations}``
    where each combination is ``{"layers": [...], "count": N}``. Combinations
    are sorted by count descending then alphabetic on the joined layer string,
    so the response is deterministic.

    Classes whose fix-logs have no classified entries are omitted from the
    result (rather than emitted with an empty histogram) so the strategic
    agent's antipattern scan can iterate the dict without filtering.
    """
    by_class: Dict[str, Dict[tuple, int]] = {}
    for case in rows:
        for entry in case.fingerprint_to_fix_log or []:
            if not isinstance(entry, dict):
                continue
            if "layers_targeted" not in entry:
                continue
            raw = entry.get("layers_targeted") or []
            if not isinstance(raw, list):
                continue
            # Normalize layer order + dedupe so ["AGENT_VM","CI_WORKFLOW"]
            # and ["CI_WORKFLOW","AGENT_VM","AGENT_VM"] aggregate to the same
            # bucket. Empty list aggregates to a "no-layer" bucket — treat
            # those as classified (caller asked us to backfill them) so the
            # strategic agent can spot when fixes consistently miss all 5
            # canonical layers (e.g. doc-only or test-only remediations).
            combo = tuple(sorted({str(x) for x in raw}))
            bucket = by_class.setdefault(case.regression_class, {})
            bucket[combo] = bucket.get(combo, 0) + 1

    out: Dict[str, Dict[str, Any]] = {}
    for regression_class, combos in by_class.items():
        total = sum(combos.values())
        sorted_combos = sorted(
            (
                {"layers": list(combo), "count": count}
                for combo, count in combos.items()
            ),
            key=lambda d: (-d["count"], ",".join(d["layers"])),
        )
        out[regression_class] = {
            "total_fixes_with_layers": total,
            "combinations": sorted_combos,
        }
    return out


def _compute_symptom_layers_by_class(
    rows: List[RegressionRemediationCase],
) -> Dict[str, List[str]]:
    """Union of canonical symptom layers observed per regression class.

    Used by the UI to compute the under-defended signal: a class is
    under-defended when the fix-history omits a layer that has appeared
    in its symptoms (i.e. we keep patching where the pain isn't).
    """
    by_class: Dict[str, set] = {}
    for case in rows:
        if not case.symptoms_by_layer:
            continue
        layers = {str(k) for k in case.symptoms_by_layer.keys()}
        by_class.setdefault(case.regression_class, set()).update(layers)
    return {cls: sorted(layers) for cls, layers in sorted(by_class.items())}


def _compute_recovery_mismatches_by_class(
    rows: List[RegressionRemediationCase],
) -> Dict[str, List[Dict[str, Any]]]:
    """Recovery-layer-mismatch events from fix logs, keyed by regression class.

    Scans fingerprint_to_fix_log for entries of type 'recovery_layer_mismatch'
    (written by _compute_recovery_layer_mismatch at case closure).  Used by
    the pipeline health summary so operators can see which closed cases had
    fix/symptom layer divergence.
    """
    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for case in rows:
        for entry in case.fingerprint_to_fix_log or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "recovery_layer_mismatch":
                continue
            by_class.setdefault(case.regression_class, []).append(entry)
    return by_class


def compute_cross_layer_compliance(
    rows: List[RegressionRemediationCase],
    cutoff: datetime,
    over_threshold_hours: int = 24,
) -> Dict[str, Any]:
    """Compliance metric for cross-layer-escalated regression remediations.

    For fix-log entries where ``spawn_was_cross_layer`` is True:
    - Entries without a ``layers_targeted`` key are awaiting backfill and are
      excluded from both numerator and denominator.
    - Compliant: fix touched >=2 distinct canonical layers.
    - Non-compliant: fix touched 0 or 1 canonical layers.
      - doc_test_only: layers_targeted is empty (no canonical layers).
      - most_recent_layer_only: layers_targeted matches exactly the last
        observed layer from spawn_layers_enumerated.

    ``cutoff`` is the oldest ``merged_at`` timestamp to include.
    Non-cross-layer entries are never counted in any bucket.

    Also computes per-bucket recurrence: for each cross-layer fix, whether
    the regression re-tripped (another fix merged within 14 days) so operators
    can validate whether compliant fixes hold longer than non-compliant ones.
    """
    from app.regression_layer_classifier import ALL_LAYERS

    _RE_TRIP_WINDOW = timedelta(days=14)

    total = compliant = doc_test_only = most_recent_layer_only = awaiting_backfill = 0
    non_compliant = 0
    awaiting_over_threshold = 0
    awaiting_oldest_merged_at: Optional[datetime] = None
    by_class: Dict[str, Dict[str, Any]] = {}
    # bucket -> list of hold_days (None = no re-trip within 14 days)
    bucket_records: Dict[str, List] = {
        "compliant": [],
        "doc_test_only": [],
        "most_recent_layer_only": [],
        "awaiting_backfill": [],
    }

    for case in rows:
        cls_compliant = cls_non_compliant = cls_awaiting = 0

        # Collect all fix merged timestamps for re-trip detection (any entry with a fix_pr_number)
        all_fix_dts: List[datetime] = []
        for e in case.fingerprint_to_fix_log or []:
            if not isinstance(e, dict) or e.get("fix_pr_number") is None:
                continue
            raw = e.get("merged_at")
            if not raw:
                continue
            try:
                all_fix_dts.append(
                    _to_naive_utc(
                        datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    )
                )
            except (ValueError, TypeError):
                pass
        all_fix_dts.sort()

        for entry in case.fingerprint_to_fix_log or []:
            if not isinstance(entry, dict):
                continue
            if not entry.get("spawn_was_cross_layer"):
                continue
            merged_at_raw = entry.get("merged_at")
            if not merged_at_raw:
                continue
            try:
                merged_dt = _to_naive_utc(
                    datetime.fromisoformat(str(merged_at_raw).replace("Z", "+00:00"))
                )
            except (ValueError, TypeError):
                continue
            if merged_dt < cutoff:
                continue

            if "layers_targeted" not in entry:
                awaiting_backfill += 1
                cls_awaiting += 1
                bucket_name = "awaiting_backfill"
                over_threshold_cutoff = _utcnow() - timedelta(
                    hours=over_threshold_hours
                )
                if merged_dt < over_threshold_cutoff:
                    awaiting_over_threshold += 1
                if (
                    awaiting_oldest_merged_at is None
                    or merged_dt < awaiting_oldest_merged_at
                ):
                    awaiting_oldest_merged_at = merged_dt
            else:
                raw_targeted = entry.get("layers_targeted") or []
                targeted_canonical = {
                    str(x) for x in raw_targeted if str(x) in ALL_LAYERS
                }
                spawn_layers = list(entry.get("spawn_layers_enumerated") or [])

                total += 1
                if len(targeted_canonical) >= 2:
                    compliant += 1
                    cls_compliant += 1
                    bucket_name = "compliant"
                else:
                    non_compliant += 1
                    cls_non_compliant += 1
                    if not targeted_canonical:
                        doc_test_only += 1
                        bucket_name = "doc_test_only"
                    elif spawn_layers and targeted_canonical == {spawn_layers[-1]}:
                        most_recent_layer_only += 1
                        bucket_name = "most_recent_layer_only"
                    else:
                        bucket_name = "doc_test_only"

            # Detect re-trip: earliest subsequent fix within 14 days
            hold_days = None
            for next_dt in all_fix_dts:
                if next_dt <= merged_dt:
                    continue
                delta = next_dt - merged_dt
                if delta <= _RE_TRIP_WINDOW:
                    hold_days = delta.total_seconds() / 86400
                break
            bucket_records[bucket_name].append(hold_days)

        if cls_compliant + cls_non_compliant + cls_awaiting > 0:
            cls_total = cls_compliant + cls_non_compliant
            by_class[case.regression_class] = {
                "compliant": cls_compliant,
                "non_compliant": cls_non_compliant,
                "awaiting_backfill": cls_awaiting,
                "compliance_rate": (
                    round(cls_compliant / cls_total, 4) if cls_total > 0 else None
                ),
            }

    recurrence_by_bucket: Dict[str, Any] = {}
    for bname, records in bucket_records.items():
        merged_count = len(records)
        retripped = [hd for hd in records if hd is not None]
        retripped_count = len(retripped)
        recurrence_by_bucket[bname] = {
            "merged_count": merged_count,
            "retripped_count": retripped_count,
            "recurrence_rate": round(retripped_count / merged_count, 4)
            if merged_count > 0
            else None,
            "median_hold_days": round(statistics.median(retripped), 2)
            if retripped
            else None,
        }

    return {
        "total_classified": total,
        "compliant": compliant,
        "non_compliant": non_compliant,
        "awaiting_backfill_count": awaiting_backfill,
        "awaiting_backfill_oldest_merged_at": (
            awaiting_oldest_merged_at.isoformat() if awaiting_oldest_merged_at else None
        ),
        "awaiting_backfill_over_threshold_count": awaiting_over_threshold,
        "compliance_rate": round(compliant / total, 4) if total > 0 else None,
        "failure_modes": {
            "doc_test_only": doc_test_only,
            "most_recent_layer_only": most_recent_layer_only,
        },
        "by_class": by_class,
        "recurrence_by_bucket": recurrence_by_bucket,
    }


def _compute_regression_case_summary(
    rows: List[RegressionRemediationCase],
    all_rows: List[RegressionRemediationCase] | None = None,
) -> Dict[str, Any]:
    by_class: Dict[str, Dict[str, List[float]]] = {}
    for case in rows:
        if not case.first_seen_at:
            continue
        # episode_start advances to each cycle's recovered_at so re-armed cases
        # report per-episode durations rather than lifetime spans.
        episode_start = _to_naive_utc(case.first_seen_at)
        latest_merged_at = None
        bucket_written = False
        for entry in case.fingerprint_to_fix_log or []:
            if entry.get("merged_at"):
                latest_merged_at = _to_naive_utc(
                    datetime.fromisoformat(
                        str(entry["merged_at"]).replace("Z", "+00:00")
                    )
                )
            if entry.get("recovered_at") and latest_merged_at is not None:
                recovered_dt = _to_naive_utc(
                    datetime.fromisoformat(
                        str(entry["recovered_at"]).replace("Z", "+00:00")
                    )
                )
                bucket = by_class.setdefault(
                    case.regression_class,
                    {"to_merged": [], "to_recovered": [], "to_detect": []},
                )
                bucket["to_merged"].append(
                    (latest_merged_at - episode_start).total_seconds()
                )
                bucket["to_recovered"].append(
                    (recovered_dt - latest_merged_at).total_seconds()
                )
                episode_start = recovered_dt
                latest_merged_at = None
                bucket_written = True
        if bucket_written and case.signal_first_crossed_at:
            by_class[case.regression_class]["to_detect"].append(
                (
                    _to_naive_utc(case.first_seen_at)
                    - _to_naive_utc(case.signal_first_crossed_at)
                ).total_seconds()
            )
    layer_coverage_by_class = _compute_layer_coverage_by_class(rows)
    all_rows_list = list(all_rows) if all_rows is not None else rows
    symptom_layers_by_class = _compute_symptom_layers_by_class(all_rows_list)
    recovery_mismatches_by_class = _compute_recovery_mismatches_by_class(all_rows_list)

    def _entry(regression_class: str, bucket: Dict[str, List[float]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "regression_class": regression_class,
            "count": len(bucket["to_merged"]),
            "median_time_from_first_seen_to_merged_seconds": round(
                statistics.median(bucket["to_merged"]), 3
            ),
            "median_time_from_merged_to_recovered_seconds": round(
                statistics.median(bucket["to_recovered"]), 3
            ),
            "median_time_to_detect_seconds": round(
                statistics.median(bucket["to_detect"]), 3
            )
            if bucket["to_detect"]
            else None,
        }
        # Only attach layer_coverage when there's a classified fix-log entry
        # for this class — keeps the response shape backwards-compatible for
        # callers that strictly equality-check the per-class dict.
        coverage = layer_coverage_by_class.get(regression_class)
        if coverage is not None:
            out["layer_coverage"] = coverage
        return out

    response: Dict[str, Any] = {
        "by_regression_class": [
            _entry(regression_class, bucket)
            for regression_class, bucket in sorted(by_class.items())
        ],
    }
    # Top-level histogram is emitted only when at least one class has
    # classified fix entries — keeps the empty-summary contract intact.
    if layer_coverage_by_class:
        response["layer_coverage_by_class"] = layer_coverage_by_class
    if symptom_layers_by_class:
        response["symptom_layers_by_class"] = symptom_layers_by_class
    if recovery_mismatches_by_class:
        response["recovery_mismatches_by_class"] = recovery_mismatches_by_class
    return response


def _regression_case_recurrence_count(case: RegressionRemediationCase) -> int:
    """Count fix→recovery cycles in the log; mirrors the dispatcher's pairing.

    A normal happy-path closure writes two entries (one fix, one recovery),
    which collapse into a single cycle. Counting raw entries would double-count.
    """
    cycles = 0
    last_unpaired_fix = False
    for entry in case.fingerprint_to_fix_log or []:
        if not isinstance(entry, dict):
            continue
        has_fix = entry.get("fix_pr_number") is not None
        has_recovery = entry.get("recovered_at") is not None
        if has_fix:
            cycles += 1
            last_unpaired_fix = not has_recovery
        elif has_recovery:
            if last_unpaired_fix:
                last_unpaired_fix = False
            else:
                cycles += 1
    return cycles


def _compute_first_review_approval_breakdowns(
    terminal_tasks: List[Task],
) -> Dict[str, Any]:
    """Compute first-review approval rate with playbook/measurement/type/scope breakdowns.

    A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
    A task is 'approved on first review' if _first_attempt_rejection_categories is absent.
    """

    def _rate(tasks):
        eligible = [
            t
            for t in tasks
            if (t.first_payload or {}).get("type", "create") in _REVIEW_ELIGIBLE_TYPES
            and (t.context or {}).get("pr_merged") is True
        ]
        if not eligible:
            return None
        approved = sum(
            1
            for t in eligible
            if "_first_attempt_rejection_categories" not in (t.context or {})
        )
        return round(approved / len(eligible), 2)

    by_type: Dict[str, list] = {}
    by_scope: Dict[str, list] = {}
    for t in terminal_tasks:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)
        scope = (t.context or {}).get("scope", "unknown")
        by_scope.setdefault(scope, []).append(t)

    fra_by_type: Dict[str, float] = {}
    for task_type, tasks in by_type.items():
        r = _rate(tasks)
        if r is not None:
            fra_by_type[task_type] = r
    fra_by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope.items():
        r = _rate(tasks)
        if r is not None:
            fra_by_scope[scope] = r

    with_pb = [
        t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is True
    ]
    without_pb = [
        t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is False
    ]
    with_m = [
        t
        for t in terminal_tasks
        if (t.context or {}).get("_had_review_measurements") is True
    ]
    without_m = [
        t
        for t in terminal_tasks
        if (t.context or {}).get("_had_review_measurements") is False
    ]

    eligible_merged = [
        t
        for t in terminal_tasks
        if (t.first_payload or {}).get("type", "create") in _REVIEW_ELIGIBLE_TYPES
        and (t.context or {}).get("pr_merged") is True
    ]

    return {
        "overall": _rate(terminal_tasks),
        "sample_size": len(eligible_merged),
        "by_type": fra_by_type,
        "by_scope": fra_by_scope,
        "with_playbook": _rate(with_pb),
        "without_playbook": _rate(without_pb),
        "with_measurements": _rate(with_m),
        "without_measurements": _rate(without_m),
    }


def compute_failure_grouping(
    *,
    failed_tasks: List[Task],
    error_log_map: Dict[str, Any],
    dir_id_to_text: Dict[str, str],
    window_start: "datetime",
    now: "datetime",
) -> Dict[str, Any]:
    """Group failed tasks by failure class with cost/stage/waste breakdowns.

    Pure function — no database calls. Returns a dict with:
      failure_groups, cost_by_failure_class, total_failed_cost_usd,
      provision_waste_cost_usd, failure_stage_counts,
      analysis_overflow_summary, provision_waste_by_direction.
    """
    from app.failure_classification import (
        classify_failure,
        classify_failure_taxonomy,
        classify_timeout_subtype,
        AGENT_TIMEOUT,
    )
    from app.failure_stage import _derive_failure_stage
    from app.routers.admin_analytics import (
        ON_DEMAND_RATE,
        SPOT_RATE,
        _derive_completed_at,
    )

    _failure_class_labels = {
        "CODE_ERROR": "Code Error",
        "PROVISION_FAILURE": "Provision Failure",
        "AGENT_TIMEOUT": "Agent Timeout",
        "EXTERNAL_API": "External API",
        "RATE_LIMIT": "Rate Limit",
        "SCOPE_OVERFLOW": "Scope Overflow",
    }
    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    _failure_stage_counts: Dict[str, int] = {}
    _cost_by_failure_class: Dict[str, float] = {}
    _provision_waste_cost = 0.0
    _provision_waste_by_dir: Dict[str, Dict[str, Any]] = {}

    for t in failed_tasks:
        tid = str(t.id)
        cls = classify_failure(
            t, error_log_map.get(tid), at_time=t.last_heartbeat or t.started_at
        )
        _task_cost = 0.0
        if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start:
            _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            _task_cost = _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            _cost_by_failure_class[cls] = (
                _cost_by_failure_class.get(cls, 0.0) + _task_cost
            )
        if cls == "PROVISION_FAILURE":
            _provision_waste_cost += _task_cost
            _pw_did = (t.context or {}).get("direction_id")
            if _pw_did:
                _pw_key = str(_pw_did)
                _pw_entry = _provision_waste_by_dir.setdefault(
                    _pw_key,
                    {
                        "direction_id": _pw_key,
                        "direction_text": dir_id_to_text.get(_pw_key, ""),
                        "provision_cost_usd": 0.0,
                        "provision_task_count": 0,
                    },
                )
                _pw_entry["provision_cost_usd"] += _task_cost
                _pw_entry["provision_task_count"] += 1
        waiting_since = _derive_completed_at(t)
        waiting_hours = (
            round((now - waiting_since).total_seconds() / 3600)
            if waiting_since
            else None
        )
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        _stage = (t.context or {}).get("_previous_failure_stage")
        if not _stage:
            _stage = _derive_failure_stage(
                cls, t.latest_status_message or error_log_map.get(tid) or ""
            )
        if _stage:
            _failure_stage_counts[_stage] = _failure_stage_counts.get(_stage, 0) + 1
        _failure_class_groups.setdefault(cls, []).append(
            {
                "task_id": tid,
                "title": t.title or "Failed task",
                "error_snippet": error_log_map.get(tid),
                "attempt_count": t.attempt_count,
                "waiting_hours": waiting_hours,
                "taxonomy": classify_failure_taxonomy(t),
                "timeout_subtype": classify_timeout_subtype(t, error_log_map.get(tid))
                if cls == AGENT_TIMEOUT
                else None,
                "direction_id": t_dir_id,
                "direction_text": t_dir_text,
                "failure_stage": _stage,
            }
        )

    failure_groups = sorted(
        [
            {
                "failure_class": cls,
                "label": _failure_class_labels.get(cls, cls),
                "count": len(tasks),
                "tasks": tasks,
            }
            for cls, tasks in _failure_class_groups.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )
    cost_by_failure_class = {k: round(v, 4) for k, v in _cost_by_failure_class.items()}
    total_failed_cost_usd = round(sum(_cost_by_failure_class.values()), 4)
    provision_waste_cost_usd = round(_provision_waste_cost, 4)

    _analysis_overflow_count = 0
    _analysis_overflow_cost = 0.0
    _analysis_overflow_by_scope: Dict[str, int] = {}
    for t in failed_tasks:
        _ctx = t.context or {}
        if _ctx.get("scope_gate_triggered") == "analysis_overflow":
            _analysis_overflow_count += 1
            _scope = _ctx.get("scope") or "unknown"
            _analysis_overflow_by_scope[_scope] = (
                _analysis_overflow_by_scope.get(_scope, 0) + 1
            )
            if (
                t.vm_provisioned_at
                and t.vm_stopped_at
                and t.vm_stopped_at >= window_start
            ):
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _analysis_overflow_cost += (
                    _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                )
    analysis_overflow_summary = {
        "total_count": _analysis_overflow_count,
        "by_scope": _analysis_overflow_by_scope,
        "cost_usd": round(_analysis_overflow_cost, 4),
    }
    provision_waste_by_direction = sorted(
        [
            {**e, "provision_cost_usd": round(e["provision_cost_usd"], 4)}
            for e in _provision_waste_by_dir.values()
        ],
        key=lambda x: x["provision_cost_usd"],
        reverse=True,
    )[:5]

    return {
        "failure_groups": failure_groups,
        "cost_by_failure_class": cost_by_failure_class,
        "total_failed_cost_usd": total_failed_cost_usd,
        "provision_waste_cost_usd": provision_waste_cost_usd,
        "failure_stage_counts": _failure_stage_counts,
        "analysis_overflow_summary": analysis_overflow_summary,
        "provision_waste_by_direction": provision_waste_by_direction,
    }


def compute_recurring_error_clusters(
    *,
    failed_tasks: List[Task],
    error_log_map: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Group failed tasks by normalized error fingerprint.

    Pure function — no database calls. Returns up to 5 clusters with count >= 2,
    sorted by count descending. Each cluster aggregates direction_ids and
    task_types across the constituent failed tasks.
    """
    from app.anomaly_detection import _error_fingerprint

    _clusters: Dict[str, Dict[str, Any]] = {}
    for _t in failed_tasks:
        _tid = str(_t.id)
        _msg = error_log_map.get(_tid) or (_t.latest_status_message or "")
        if not _msg:
            continue
        _fp = _error_fingerprint(_msg)
        _entry = _clusters.get(_fp)
        if _entry is None:
            _entry = {
                "fingerprint": _fp,
                "count": 0,
                "error_snippet": _msg[:200],
                "direction_ids": set(),
                "task_types": set(),
                "first_task_id": _tid,
            }
            _clusters[_fp] = _entry
        _entry["count"] += 1
        _dir_id = (_t.context or {}).get("direction_id")
        if _dir_id:
            _entry["direction_ids"].add(str(_dir_id))
        _ttype = (_t.first_payload or {}).get("type")
        if _ttype:
            _entry["task_types"].add(str(_ttype))
    return [
        {
            "fingerprint": c["fingerprint"],
            "count": c["count"],
            "error_snippet": c["error_snippet"],
            "direction_ids": sorted(c["direction_ids"]),
            "task_types": sorted(c["task_types"]),
            "first_task_id": c["first_task_id"],
        }
        for c in sorted(_clusters.values(), key=lambda x: x["count"], reverse=True)
        if c["count"] >= 2
    ][:5]


def compute_digest_trend_history(
    *,
    all_tasks: List[Task],
    hours: int,
    now: "datetime",
) -> List[Dict[str, Any]]:
    """Build the 7-period trend series (6 previous + current, oldest first).

    Pure function — no database calls. Returns a list of per-period dicts
    containing success_rate, completed, failed, failed_by_class,
    failed_by_taxonomy, period_cost_usd, and first-attempt metrics.
    """
    from app.failure_classification import (
        classify_failure as _classify_failure,
        classify_failure_taxonomy as _classify_taxonomy,
    )
    from app.routers.admin_analytics import (
        ON_DEMAND_RATE,
        SPOT_RATE,
        _derive_completed_at,
    )

    trend_history = []
    for _i in range(7):
        _p_start = now - timedelta(hours=hours * (7 - _i))
        _p_end = now - timedelta(hours=hours * (6 - _i))
        _p_completed = sum(
            1
            for t in all_tasks
            if t.status == TaskStatus.COMPLETED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_failed_tasks = [
            t
            for t in all_tasks
            if t.status == TaskStatus.FAILED
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_failed = len(_p_failed_tasks)
        _p_by_class: dict[str, int] = {}
        _p_by_taxonomy: dict[str, int] = {}
        for _ft in _p_failed_tasks:
            _cls = _classify_failure(_ft, at_time=_ft.last_heartbeat or _ft.started_at)
            _p_by_class[_cls] = _p_by_class.get(_cls, 0) + 1
            _tax = _classify_taxonomy(_ft)
            _p_by_taxonomy[_tax] = _p_by_taxonomy.get(_tax, 0) + 1
        _p_total = _p_completed + _p_failed
        _p_cost = sum(
            ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE)
            * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            for t in all_tasks
            if t.vm_provisioned_at
            and t.vm_stopped_at
            and _p_start <= t.vm_stopped_at < _p_end
        )
        _p_terminal = [
            t
            for t in all_tasks
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_measured = [
            t for t in _p_terminal if "_first_attempt_succeeded" in (t.context or {})
        ]
        _p_fa_rate = (
            round(
                sum(
                    1
                    for t in _p_measured
                    if (t.context or {}).get("_first_attempt_succeeded")
                )
                / len(_p_measured),
                2,
            )
            if _p_measured
            else None
        )
        trend_history.append(
            {
                "period_end": _p_end.isoformat(),
                "success_rate": round(_p_completed / _p_total, 2)
                if _p_total > 0
                else None,
                "completed": _p_completed,
                "failed": _p_failed,
                "failed_by_class": _p_by_class,
                "failed_by_taxonomy": _p_by_taxonomy,
                "period_cost_usd": round(_p_cost, 4),
                "first_attempt_success_rate": _p_fa_rate,
                "first_attempt_sample_size": len(_p_measured),
            }
        )
    return trend_history


def compute_digest_cost_economics(
    *,
    all_tasks: List[Task],
    direction_progress_list: List[Dict[str, Any]],
    window_start: "datetime",
    now: "datetime",
    daily_cost_ceiling_usd: float,
) -> Dict[str, Any]:
    """Compute the digest's entire cost/economics section.

    Pure function — no database calls. Reads only the already-loaded task
    list, the already-computed direction-progress list, the window start,
    the current time, and the configured daily cost ceiling.

    Mutates ``direction_progress_list`` in place: stamps a
    ``cost_per_merge_exceeds_threshold`` flag onto each direction entry
    (preserving the original in-place behavior of ``_compute_digest``).

    Returns a dict with total/spot/on-demand estimated cost, provisioning
    overhead (USD and pct), daily cost vs ceiling, overall product_ratio,
    median cost-per-merged-PR, per-task-type cost-per-merge, and
    pipeline-level acceptance/conversion rates, autonomous/assisted merge
    counts, operator-action count, overall type distribution, and
    filter-funnel aggregation.
    """
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    # Compute total estimated cost (directed + undirected tasks)
    directed_task_ids = {
        t.id for t in all_tasks if (t.context or {}).get("direction_id")
    }
    undirected_cost = sum(
        ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE)
        * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        for t in all_tasks
        if t.vm_provisioned_at
        and t.vm_stopped_at
        and t.vm_stopped_at >= window_start
        and t.id not in directed_task_ids
    )
    total_estimated_cost_usd = round(
        sum(d["estimated_cost_usd"] for d in direction_progress_list) + undirected_cost,
        2,
    )
    total_spot_cost_usd = 0.0
    total_on_demand_cost_usd = 0.0
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        if t.vm_spot:
            total_spot_cost_usd += t_cost
        else:
            total_on_demand_cost_usd += t_cost
    total_spot_cost_usd = round(total_spot_cost_usd, 2)
    total_on_demand_cost_usd = round(total_on_demand_cost_usd, 2)

    # Provisioning overhead: cost of time spent between VM provision and agent claim
    # (vm_provisioned_at → vm_started_at). When vm_started_at is missing, the agent
    # never claimed the task, so the entire VM lifetime counts as provisioning waste.
    _provisioning_overhead_raw = 0.0
    _total_compute_cost_raw = total_spot_cost_usd + total_on_demand_cost_usd
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        prov_end = (
            t.vm_started_at
            if (t.vm_started_at and t.vm_started_at >= t.vm_provisioned_at)
            else t.vm_stopped_at
        )
        if prov_end <= t.vm_provisioned_at:
            continue
        _provisioning_overhead_raw += (
            rate * (prov_end - t.vm_provisioned_at).total_seconds()
        )
    provisioning_overhead_usd = round(_provisioning_overhead_raw, 2)
    provisioning_overhead_pct = (
        round(_provisioning_overhead_raw / _total_compute_cost_raw, 3)
        if _total_compute_cost_raw > 0
        else None
    )

    # Compute today's cost for cost ceiling warning
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_cost_usd = round(
        sum(
            ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE)
            * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            for t in all_tasks
            if t.vm_provisioned_at
            and t.vm_stopped_at
            and t.vm_stopped_at >= today_start
        ),
        2,
    )

    # Compute overall product_ratio and type_distribution across all directions
    all_product_ratios = [
        d["product_ratio"]
        for d in direction_progress_list
        if d.get("product_ratio") is not None
    ]
    overall_product_ratio = (
        round(sum(all_product_ratios) / len(all_product_ratios), 3)
        if all_product_ratios
        else None
    )

    all_cpms = [
        d["cost_per_merged_pr"]
        for d in direction_progress_list
        if d.get("cost_per_merged_pr") is not None
    ]
    median_cost_per_merged_pr = (
        round(statistics.median(all_cpms), 4) if all_cpms else None
    )
    for d in direction_progress_list:
        cpm = d.get("cost_per_merged_pr")
        if cpm is not None and median_cost_per_merged_pr is not None:
            d["cost_per_merge_exceeds_threshold"] = cpm > (
                median_cost_per_merged_pr * _CPM_RETIREMENT_MULTIPLIER
            )
        else:
            d["cost_per_merge_exceeds_threshold"] = False

    # Per-task-type cost-per-merge: for each task type, total VM cost / merged PR count
    _type_cost: Dict[str, float] = {}
    _type_merges: Dict[str, int] = {}
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        task_type = (t.first_payload or {}).get("type", "unknown")
        _type_cost[task_type] = _type_cost.get(task_type, 0.0) + t_cost
        if (t.context or {}).get("pr_merged") is True:
            _type_merges[task_type] = _type_merges.get(task_type, 0) + 1
    _type_cpms = {
        ttype: _type_cost[ttype] / _type_merges[ttype]
        for ttype in _type_cost
        if _type_merges.get(ttype, 0) > 0
    }
    median_cpm_by_type = (
        round(statistics.median(list(_type_cpms.values())), 4) if _type_cpms else None
    )
    cost_per_merge_by_type = {
        ttype: {
            "total_cost_usd": round(_type_cost[ttype], 4),
            "merged_count": _type_merges.get(ttype, 0),
            "cost_per_merge": round(_type_cpms[ttype], 4)
            if ttype in _type_cpms
            else None,
            "exceeds_threshold": (
                _type_cpms[ttype] > (median_cpm_by_type * _CPM_RETIREMENT_MULTIPLIER)
                if ttype in _type_cpms and median_cpm_by_type is not None
                else False
            ),
        }
        for ttype in _type_cost
    }

    # Pipeline-level acceptance rate: total merged / (total merged + total rejected) across directions in window
    _pipeline_total_merged_d = sum(
        d.get("prs_merged") or 0 for d in direction_progress_list
    )
    _pipeline_total_rejected_d = sum(
        d.get("prs_rejected") or 0 for d in direction_progress_list
    )
    _pipeline_total_decided = _pipeline_total_merged_d + _pipeline_total_rejected_d
    pipeline_acceptance_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_decided, 3)
        if _pipeline_total_decided > 0
        else None
    )
    _pipeline_total_created_d = sum(
        d.get("tasks_created") or 0 for d in direction_progress_list
    )
    pipeline_conversion_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_created_d, 3)
        if _pipeline_total_created_d > 0
        else None
    )
    pipeline_autonomous_prs_merged = sum(
        d.get("autonomous_prs_merged") or 0 for d in direction_progress_list
    )
    pipeline_assisted_prs_merged = sum(
        d.get("assisted_prs_merged") or 0 for d in direction_progress_list
    )
    pipeline_operator_action_count = sum(
        d.get("operator_action_count") or 0 for d in direction_progress_list
    )
    overall_type_distribution: Dict[str, int] = {}
    for d in direction_progress_list:
        for ttype, cnt in (d.get("type_distribution") or {}).items():
            overall_type_distribution[ttype] = (
                overall_type_distribution.get(ttype, 0) + cnt
            )

    pipeline_filter_funnel: Dict[str, int] = {}
    for d in direction_progress_list:
        ff = d.get("filter_funnel") or {}
        for _k, _v in ff.items():
            pipeline_filter_funnel[_k] = pipeline_filter_funnel.get(_k, 0) + (_v or 0)

    return {
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "total_spot_cost_usd": total_spot_cost_usd,
        "total_on_demand_cost_usd": total_on_demand_cost_usd,
        "provisioning_overhead_usd": provisioning_overhead_usd,
        "provisioning_overhead_pct": provisioning_overhead_pct,
        "daily_cost_usd": daily_cost_usd,
        "daily_cost_ceiling_usd": daily_cost_ceiling_usd,
        "overall_product_ratio": overall_product_ratio,
        "median_cost_per_merged_pr": median_cost_per_merged_pr,
        "cost_per_merge_by_type": cost_per_merge_by_type,
        "median_cpm_by_type": median_cpm_by_type,
        "pipeline_acceptance_rate": pipeline_acceptance_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "pipeline_autonomous_prs_merged": pipeline_autonomous_prs_merged,
        "pipeline_assisted_prs_merged": pipeline_assisted_prs_merged,
        "pipeline_operator_action_count": pipeline_operator_action_count,
        "overall_type_distribution": overall_type_distribution,
        "pipeline_filter_funnel": pipeline_filter_funnel,
    }


def compute_scope_mismatch_summary(
    *,
    completed_tasks: List[Task],
    failed_tasks: List[Task],
    dir_id_to_text: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Aggregate scope-calibration data across terminal tasks in the window.

    Surfaces systematic scope-estimation drift and chronic-calibration
    directions: counts calibrated tasks, computes their success rate, builds a
    breakdown keyed by original declared scope, and ranks the top directions by
    calibration rate. Pure function — reads only the already-loaded
    completed/failed task lists and the direction-id-to-text map; no database
    calls. Returns the summary dict, or None when no calibrated tasks exist in
    the window.
    """
    terminal_in_window = completed_tasks + failed_tasks
    calibrated_tasks = [
        t for t in terminal_in_window if (t.context or {}).get("scope_calibrated_from")
    ]
    if not calibrated_tasks:
        return None

    cal_completed = sum(1 for t in calibrated_tasks if t.status == TaskStatus.COMPLETED)
    cal_total = len(calibrated_tasks)
    by_orig: Dict[str, Dict[str, int]] = {}
    by_dir_calibrated: Dict[str, int] = {}
    for t in calibrated_tasks:
        orig_raw = str((t.context or {}).get("scope_calibrated_from") or "")
        orig = orig_raw.split(":", 1)[0] or "unknown"
        bucket = by_orig.setdefault(orig, {"total": 0, "completed": 0, "failed": 0})
        bucket["total"] += 1
        if t.status == TaskStatus.COMPLETED:
            bucket["completed"] += 1
        elif t.status == TaskStatus.FAILED:
            bucket["failed"] += 1
        did = (t.context or {}).get("direction_id")
        if did:
            by_dir_calibrated[str(did)] = by_dir_calibrated.get(str(did), 0) + 1

    by_dir_total: Dict[str, int] = {}
    for t in terminal_in_window:
        did = (t.context or {}).get("direction_id")
        if did:
            by_dir_total[str(did)] = by_dir_total.get(str(did), 0) + 1

    top_dirs = []
    for did, cal_cnt in by_dir_calibrated.items():
        tot = by_dir_total.get(did, 0)
        if tot <= 0:
            continue
        top_dirs.append(
            {
                "direction_id": did,
                "direction_text": dir_id_to_text.get(did, ""),
                "calibrated_count": cal_cnt,
                "total_tasks": tot,
                "calibration_rate": round(cal_cnt / tot, 3),
            }
        )
    top_dirs.sort(
        key=lambda d: (d["calibration_rate"], d["calibrated_count"]), reverse=True
    )

    return {
        "total_calibrated": cal_total,
        "calibrated_success_rate": round(cal_completed / cal_total, 3)
        if cal_total
        else None,
        "by_original_scope": by_orig,
        "top_directions": top_dirs[:3],
    }


async def compute_remediation_funnel(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    window_start: "datetime",
    residual_remediation_funnel: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the investigation -> diagnosis -> remediation -> merge funnel.

    Runs its own self-contained Task/Project queries off ``session`` (scoped to
    ``window_start`` and the optional ``project_id``) and assembles the
    ``remediation_funnel`` digest block, including the adjacent CI auto-remediation
    funnel. The residual-remediation funnel is a separate responsibility computed by
    the caller and passed in via ``residual_remediation_funnel`` so it lands in the
    returned funnel dict at its original position.

    Returns a ``(remediation_funnel, investigation_conversion)`` tuple — the latter
    is a separate top-level digest key, spliced in by the caller unchanged.
    """
    from app.failure_classification import classify_failure as _classify_failure
    from app.routers.admin_analytics import _derive_completed_at
    from app.task_completion import _is_structural_diagnosis

    # Query investigate + remediation tasks in-window regardless of status (the
    # main all_tasks query is terminal-only, so investigate tasks that are still
    # IN_PROGRESS would otherwise be missed).
    _funnel_filters = [Task.created_at >= window_start]
    if project_id:
        _funnel_filters.append(Task.project_id == project_id)
    _inv_result = await session.execute(
        select(Task)
        .where(
            *_funnel_filters,
            Task.payloads[0]["type"].as_string() == "investigate",
        )
        .limit(2000)
    )
    _inv_tasks = _inv_result.scalars().all()
    # R4-W2 (M3): ``has_key`` uses the JSONB ``?`` operator so the
    # ``ix_task_context_gin`` GIN index can satisfy the predicate.
    _rem_result = await session.execute(
        select(Task)
        .where(
            *_funnel_filters,
            Task.context.has_key("diagnosis_task_id"),
        )
        .limit(10000)
    )
    _rem_tasks = _rem_result.scalars().all()

    investigations_spawned = len(_inv_tasks)
    investigations_completed = sum(
        1 for t in _inv_tasks if t.status == TaskStatus.COMPLETED
    )
    investigations_with_diagnosis = sum(
        1
        for t in _inv_tasks
        if t.status == TaskStatus.COMPLETED
        and isinstance(t.plan_output, dict)
        and str((t.plan_output or {}).get("summary") or "").strip()
    )
    active_investigations = sum(
        1
        for t in _inv_tasks
        if t.status
        in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        )
    )
    remediations_spawned = len(_rem_tasks)
    remediations_merged = sum(1 for t in _rem_tasks if t.status == TaskStatus.COMPLETED)
    remediations_failed = sum(1 for t in _rem_tasks if t.status == TaskStatus.FAILED)

    _held_projects_q = select(Project).options(
        load_only(Project.id, Project.directions)
    )
    if project_id:
        _held_projects_q = _held_projects_q.where(Project.id == project_id)
    else:
        _held_projects_q = _held_projects_q.where(Project.directions.isnot(None))
    _held_projects = (
        (await session.execute(_held_projects_q.limit(10000))).scalars().all()
    )

    fixes_held = 0
    _window_iso = window_start.isoformat()
    for _p in _held_projects:
        for _d in _p.directions or []:
            for _h in _d.get("remediation_history") or []:
                if (_h.get("result") or "") != "held":
                    continue
                _ts = _h.get("held_at") or _h.get("timestamp") or ""
                if _ts and _ts >= _window_iso:
                    fixes_held += 1

    _inv_by_id = {str(t.id): t for t in _inv_tasks}
    _ttr_values: List[float] = []
    for r in _rem_tasks:
        if r.status != TaskStatus.COMPLETED:
            continue
        diag_id = (r.context or {}).get("diagnosis_task_id")
        inv = _inv_by_id.get(str(diag_id)) if diag_id else None
        if inv is None:
            continue
        completed_at = _derive_completed_at(r)
        if completed_at and inv.created_at:
            inv_created = (
                inv.created_at.replace(tzinfo=None)
                if inv.created_at.tzinfo is not None
                else inv.created_at
            )
            delta = (completed_at - inv_created).total_seconds() / 3600.0
            if delta >= 0:
                _ttr_values.append(delta)
    avg_time_to_remediation_hours = (
        round(sum(_ttr_values) / len(_ttr_values), 2) if _ttr_values else None
    )

    investigations_by_confidence: Dict[str, int] = {}
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED or not isinstance(t.plan_output, dict):
            continue
        conf = (
            str((t.plan_output or {}).get("confidence") or "unknown").strip().lower()
            or "unknown"
        )
        investigations_by_confidence[conf] = (
            investigations_by_confidence.get(conf, 0) + 1
        )

    failed_remediations_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        if r.status != TaskStatus.FAILED:
            continue
        cls = (
            _classify_failure(r, at_time=r.last_heartbeat or r.started_at) or "unknown"
        )
        failed_remediations_by_class[cls] = failed_remediations_by_class.get(cls, 0) + 1

    investigations_spawned_by_class: Dict[str, int] = {}
    investigations_completed_by_class: Dict[str, int] = {}
    for t in _inv_tasks:
        cls = (
            str((t.context or {}).get("last_failure_class") or "unknown").strip()
            or "unknown"
        )
        investigations_spawned_by_class[cls] = (
            investigations_spawned_by_class.get(cls, 0) + 1
        )
        if t.status == TaskStatus.COMPLETED:
            investigations_completed_by_class[cls] = (
                investigations_completed_by_class.get(cls, 0) + 1
            )

    remediations_spawned_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        cls = (
            str((r.context or {}).get("trigger_failure_class") or "unknown").strip()
            or "unknown"
        )
        remediations_spawned_by_class[cls] = (
            remediations_spawned_by_class.get(cls, 0) + 1
        )

    merged_remediations_by_trigger_class: Dict[str, int] = {}
    for r in _rem_tasks:
        if r.status != TaskStatus.COMPLETED:
            continue
        trigger_cls = (r.context or {}).get("trigger_failure_class")
        if not trigger_cls:
            diag_id = (r.context or {}).get("diagnosis_task_id")
            inv = _inv_by_id.get(str(diag_id)) if diag_id else None
            if inv is not None:
                trigger_cls = (inv.context or {}).get("last_failure_class")
        key = str(trigger_cls).strip() if trigger_cls else "unknown"
        if not key:
            key = "unknown"
        merged_remediations_by_trigger_class[key] = (
            merged_remediations_by_trigger_class.get(key, 0) + 1
        )

    remediation_success_by_confidence: Dict[str, Dict[str, int]] = {}
    for r in _rem_tasks:
        if r.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            continue
        diag_id = (r.context or {}).get("diagnosis_task_id")
        inv = _inv_by_id.get(str(diag_id)) if diag_id else None
        conf = "unknown"
        if inv is not None and isinstance(inv.plan_output, dict):
            conf = (
                str((inv.plan_output or {}).get("confidence") or "unknown")
                .strip()
                .lower()
                or "unknown"
            )
        bucket = remediation_success_by_confidence.setdefault(
            conf, {"merged": 0, "failed": 0}
        )
        if r.status == TaskStatus.COMPLETED:
            bucket["merged"] += 1
        else:
            bucket["failed"] += 1

    _completed_inv = [
        t
        for t in _inv_tasks
        if t.status == TaskStatus.COMPLETED and isinstance(t.plan_output, dict)
    ]
    _completed_inv.sort(
        key=lambda t: _derive_completed_at(t) or t.created_at or _utcnow(),
        reverse=True,
    )
    recent_investigations: List[Dict[str, Any]] = []
    for t in _completed_inv[:5]:
        po = t.plan_output or {}
        summary = str(po.get("summary") or "").strip()
        if len(summary) > 200:
            summary = summary[:200].rstrip() + "…"
        completed_at = _derive_completed_at(t)
        recent_investigations.append(
            {
                "task_id": str(t.id),
                "direction_id": (t.context or {}).get("direction_id"),
                "summary": summary,
                "confidence": str(po.get("confidence") or "").strip().lower() or None,
                "severity": str(po.get("severity") or "").strip().lower() or None,
                "completed_at": completed_at.isoformat() if completed_at else None,
            }
        )

    active_investigations_detail: List[Dict[str, Any]] = []
    _now = _utcnow()
    for t in _inv_tasks:
        if t.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ):
            continue
        elapsed_hours = None
        if t.created_at:
            elapsed_hours = round(
                (_now - _to_naive_utc(t.created_at)).total_seconds() / 3600.0, 2
            )
        active_investigations_detail.append(
            {
                "task_id": str(t.id),
                "direction_id": (t.context or {}).get("direction_id"),
                "elapsed_hours": elapsed_hours,
            }
        )

    _ci_rem_result = await session.execute(
        select(Task).where(
            *_funnel_filters,
            Task.auto_remediation.is_(True),  # promoted column (audit W1)
        )
    )
    _ci_rem_tasks = list(_ci_rem_result.scalars().all())
    ci_auto_remediation: Dict[str, Any] = {
        "spawned": len(_ci_rem_tasks),
        "merged": sum(
            1
            for t in _ci_rem_tasks
            if t.status == TaskStatus.COMPLETED
            and (t.context or {}).get("pr_merged") is True
        ),
        "failed": sum(1 for t in _ci_rem_tasks if t.status == TaskStatus.FAILED),
        "by_ci_failure_type": {},
    }
    _ci_type_buckets: Dict[str, Dict[str, int]] = {}
    for t in _ci_rem_tasks:
        ci_type = (
            str((t.context or {}).get("ci_failure_type") or "unknown").strip()
            or "unknown"
        )
        bucket = _ci_type_buckets.setdefault(
            ci_type, {"spawned": 0, "merged": 0, "failed": 0}
        )
        bucket["spawned"] += 1
        if (
            t.status == TaskStatus.COMPLETED
            and (t.context or {}).get("pr_merged") is True
        ):
            bucket["merged"] += 1
        elif t.status == TaskStatus.FAILED:
            bucket["failed"] += 1
    ci_auto_remediation["by_ci_failure_type"] = _ci_type_buckets

    _prior_tasks = [
        t for t in _ci_rem_tasks if (t.context or {}).get("prior_diagnosis_task_id")
    ]
    _total_with_prior = len(_prior_tasks)
    _merged_with_prior = sum(
        1 for t in _prior_tasks if t.status == TaskStatus.COMPLETED
    )
    _failed_with_prior = sum(1 for t in _prior_tasks if t.status == TaskStatus.FAILED)
    _prior_resolved = _merged_with_prior + _failed_with_prior
    prior_investigation_reuse: Dict[str, Any] = {
        "total_with_prior": _total_with_prior,
        "merged_with_prior": _merged_with_prior,
        "failed_with_prior": _failed_with_prior,
        "merge_rate": round(_merged_with_prior / _prior_resolved, 4)
        if _prior_resolved
        else None,
    }

    # R4-W2 (M3): ``has_key`` uses the JSONB ``?`` operator so the
    # ``ix_task_context_gin`` GIN index can satisfy the predicate.
    _cum_filters = [
        Task.status == TaskStatus.COMPLETED,
        or_(
            Task.context.has_key("diagnosis_task_id"),
            Task.auto_remediation.is_(True),  # promoted column (audit W1)
        ),
        Task.context["pr_merged"].as_boolean() == True,  # noqa: E712
    ]
    if project_id:
        _cum_filters.append(Task.project_id == project_id)
    _CUM_TASK_CAP = 10000
    _cum_result = await session.execute(
        select(Task).where(*_cum_filters).limit(_CUM_TASK_CAP)
    )
    cumulative_merged_by_trigger_class: Dict[str, int] = {}
    _cum_rows = _cum_result.scalars().all()
    if len(_cum_rows) >= _CUM_TASK_CAP:
        logger.warning(
            "cumulative_merged_tasks: result capped at %d for project %s — "
            "metrics may be truncated",
            _CUM_TASK_CAP,
            project_id,
        )
    for r in _cum_rows:
        ctx = r.context or {}
        trigger_cls = ctx.get("trigger_failure_class")
        if not trigger_cls:
            diag_id = ctx.get("diagnosis_task_id")
            inv = _inv_by_id.get(str(diag_id)) if diag_id else None
            if inv is not None:
                trigger_cls = (inv.context or {}).get("last_failure_class")
        key = str(trigger_cls).strip() if trigger_cls else "unknown"
        if not key:
            key = "unknown"
        cumulative_merged_by_trigger_class[key] = (
            cumulative_merged_by_trigger_class.get(key, 0) + 1
        )

    _inv_ids_with_remediation = {
        str((r.context or {}).get("diagnosis_task_id"))
        for r in _rem_tasks
        if (r.context or {}).get("diagnosis_task_id")
    }
    _PARSEABLE_KEYS = ("summary", "root_cause", "recommended_fix", "confidence")
    ic_total = 0
    ic_parseable = 0
    ic_unparseable = 0
    ic_structural = 0
    ic_low_confidence = 0
    ic_actionable = 0
    ic_remediation_spawned = 0
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED:
            continue
        ic_total += 1
        po = t.plan_output if isinstance(t.plan_output, dict) else None
        parseable = bool(po) and any(
            str(po.get(k) or "").strip() for k in _PARSEABLE_KEYS
        )
        if parseable:
            ic_parseable += 1
        else:
            ic_unparseable += 1
            continue
        root_cause = str((po or {}).get("root_cause") or "")
        recommended_fix = str((po or {}).get("recommended_fix") or "")
        confidence = str((po or {}).get("confidence") or "").strip().lower()
        structural = _is_structural_diagnosis(root_cause, recommended_fix)
        if structural:
            ic_structural += 1
            continue
        if confidence == "low":
            ic_low_confidence += 1
        elif confidence in ("high", "medium") and recommended_fix.strip():
            ic_actionable += 1
        if str(t.id) in _inv_ids_with_remediation:
            ic_remediation_spawned += 1
    investigation_conversion = {
        "total_completed": ic_total,
        "parseable": ic_parseable,
        "unparseable": ic_unparseable,
        "structural": ic_structural,
        "low_confidence": ic_low_confidence,
        "actionable": ic_actionable,
        "remediation_spawned": ic_remediation_spawned,
    }

    diagnosis_drop_off_by_reason: Dict[str, int] = {}
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED:
            continue
        po = t.plan_output if isinstance(t.plan_output, dict) else None
        if not po:
            continue
        if not str(po.get("summary") or "").strip():
            continue
        if str(t.id) in _inv_ids_with_remediation:
            continue
        root_cause = str(po.get("root_cause") or "")
        recommended_fix = str(po.get("recommended_fix") or "").strip()
        confidence = str(po.get("confidence") or "").strip().lower()
        direction_id_val = (t.context or {}).get("direction_id")
        if _is_structural_diagnosis(root_cause, recommended_fix):
            reason = "structural"
        elif not recommended_fix:
            reason = "no_recommended_fix"
        elif confidence not in ("high", "medium"):
            reason = "low_confidence"
        elif not direction_id_val:
            reason = "no_direction"
        else:
            continue
        diagnosis_drop_off_by_reason[reason] = (
            diagnosis_drop_off_by_reason.get(reason, 0) + 1
        )

    remediation_funnel = {
        "investigations_spawned": investigations_spawned,
        "investigations_completed": investigations_completed,
        "investigations_with_diagnosis": investigations_with_diagnosis,
        "remediations_spawned": remediations_spawned,
        "remediations_merged": remediations_merged,
        "remediations_failed": remediations_failed,
        "fixes_held": fixes_held,
        "avg_time_to_remediation_hours": avg_time_to_remediation_hours,
        "active_investigations": active_investigations,
        "investigations_by_confidence": investigations_by_confidence,
        "failed_remediations_by_class": failed_remediations_by_class,
        "merged_remediations_by_trigger_class": merged_remediations_by_trigger_class,
        "investigations_spawned_by_class": investigations_spawned_by_class,
        "investigations_completed_by_class": investigations_completed_by_class,
        "remediations_spawned_by_class": remediations_spawned_by_class,
        "cumulative_merged_by_trigger_class": cumulative_merged_by_trigger_class,
        "remediation_success_by_confidence": remediation_success_by_confidence,
        "recent_investigations": recent_investigations,
        "active_investigations_detail": active_investigations_detail,
        "ci_auto_remediation": ci_auto_remediation,
        "residual_remediation_funnel": residual_remediation_funnel,
        "prior_investigation_reuse": prior_investigation_reuse,
        "diagnosis_drop_off_by_reason": diagnosis_drop_off_by_reason,
    }
    return remediation_funnel, investigation_conversion
