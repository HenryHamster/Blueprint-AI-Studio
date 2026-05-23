"""Pure metric-computation helpers extracted from admin_digest.

These are synchronous transforms over already-loaded Task / RegressionRemediationCase
data — no AsyncSession and no FastAPI Request. They are imported (and re-exported) by
``app.routers.admin_digest`` so existing call sites and test patch targets keep working.
"""

import statistics
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from app.time_utils import _to_naive_utc
from app.models import Task, RegressionRemediationCase, _utcnow
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    is_skipped_sr_outcome as _is_skipped_sr_outcome_shared,
)

_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}

# Alias preserved for the moved helpers; admin_digest keeps its own re-export too.
_is_skipped_sr_outcome = _is_skipped_sr_outcome_shared

_SR_TREND_STALENESS_DAYS = 7

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
