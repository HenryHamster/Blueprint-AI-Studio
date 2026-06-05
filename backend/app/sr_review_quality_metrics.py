"""SR/review-quality metric helpers extracted from digest_metrics.

Self-contained synchronous transforms over already-loaded Task data for
self-review catch rate, prediction accuracy, reviewer effectiveness, and
related quality signals. Imported and re-exported by digest_metrics so
existing call sites keep working without changes.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.models import Task
from app.review_learning import _categorize_review_issues
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    is_skipped_sr_outcome,
)
from app.time_utils import _to_naive_utc

_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}

_SR_SEVERITY_WEIGHTS: Dict[str, int] = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}

_is_skipped_sr_outcome = is_skipped_sr_outcome

_SR_TREND_STALENESS_DAYS = 7

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
            computed_at_dt = _to_naive_utc(datetime.fromisoformat(computed_at_str))
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
    # Retired: parallel_review_effectiveness measured review-task parallelization,
    # but had no consumer and two placeholder subfields never had real inputs.

    return {
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "review_accuracy": review_accuracy,
        "fresh_reviewer_effectiveness": fresh_reviewer_effectiveness,
        "edges_considered_metrics": edges_considered_metrics,
    }
