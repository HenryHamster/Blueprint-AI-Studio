"""Self-review analytics, alerts, and persisted trend payloads.

This module is the canonical home for SR catch-rate analytics and the related
alert/drilldown flows. ``app.playbook_learning`` re-exports these names for
backward compatibility while callers migrate.
"""

import logging
import re
import uuid
from datetime import datetime
from datetime import timedelta
from typing import Any, Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Alert,
    AlertSeverity,
    Project,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.playbook_sr_metrics import _task_prompt_fingerprint  # noqa: F401
from app.shared_state import DEFAULT_TASK_SCOPE
from app.sr_blind_spots import _REVIEWER_ORIGINS

logger = logging.getLogger(__name__)


# In-app Alert row title prefixes for SR catch-rate alerts. The router that
# powers AlertsBanner returns Alert rows verbatim; we use these prefixes both
# to format new rows and to find/dismiss prior matching rows on clear-transition.
_SR_REGRESSION_ALERT_TITLE_PREFIX = "Self-review catch-rate regression"
_SR_FLOOR_ALERT_TITLE_PREFIX = "Self-review catch-rate below floor"
_SR_QUADRANT_ALERT_TITLE_PREFIX = "Self-review precision/recall shift"
_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX = "PASS reasoning thinning"
# Cap how many regressing categories/severities we name in an Alert title
# before truncating; the full breakdown lives in `Alert.message`.
_SR_ALERT_TITLE_MAX_PARTS = 5


def _format_sr_alert_title(prefix: str, parts: list[str]) -> str:
    """Compose an Alert title from a prefix and a list of regressing identifiers."""
    if not parts:
        return prefix
    if len(parts) > _SR_ALERT_TITLE_MAX_PARTS:
        head = ", ".join(parts[:_SR_ALERT_TITLE_MAX_PARTS])
        joined = f"{head}, +{len(parts) - _SR_ALERT_TITLE_MAX_PARTS} more"
    else:
        joined = ", ".join(parts)
    return f"{prefix}: {joined}"


# Execution-evidence SR check categories. These track whether the agent
# actually ran verification commands (pytest, lint, typecheck) and got exit 0.
# Miss rate = fraction of tasks where the agent reported passed: false.
_EXECUTION_EVIDENCE_CATEGORIES = frozenset(
    ["tests_executed_cleanly", "lint_executed_cleanly", "typecheck_executed_cleanly"]
)


def _categorize_review_issues(review_history: list) -> dict[str, list[dict]]:
    from app.playbook_learning import _categorize_review_issues as _impl

    return _impl(review_history)


def _categorize_review_issues_keyword(review_history: list) -> dict[str, list[dict]]:
    from app.playbook_learning import _categorize_review_issues_keyword as _impl

    return _impl(review_history)


# Shared trend thresholds used by review and SR analytics.
_TREND_DELTA_THRESHOLD = 0.05
# Minimum z-score (two-proportion z-test) required to assert a trend direction.
# z=1.28 ≈ 80% CI two-tailed; gates directional labels against sampling noise
# while preserving large genuine shifts even at small sample sizes.
_TREND_Z_MIN = 1.28
# Threshold for precision/recall quadrant classification (5pp absolute change).
_PRECISION_RECALL_QUADRANT_THRESHOLD = 0.05
_SR_SEVERITY_DELTA_THRESHOLDS: dict[str, float] = {
    "CRITICAL": 0.03,
    "MAJOR": 0.05,
    "MINOR": 0.10,
}

# Baseline-floor alert: fire when a category's hit-rate stays below this
# threshold for at least _FLOOR_MIN_CONSECUTIVE_WINDOWS measurement windows.
_SR_FLOOR_THRESHOLD = 0.50
_FLOOR_MIN_CONSECUTIVE_WINDOWS = 2

# PASS reasoning thinning alert: fire when a non-regressing category has
# engagement below this threshold with at least _SR_ENGAGEMENT_MIN_PASSES passes.
_SR_ENGAGEMENT_THRESHOLD = 0.5
_SR_ENGAGEMENT_MIN_PASSES = 10

# ---------------------------------------------------------------------------
# SR catch-rate trend tracking
# ---------------------------------------------------------------------------

# Minimum number of tasks with BOTH self-review check_results AND human
# rejection categories required per window.  Half the human-review threshold
# since SR data is sparser.
_MIN_SR_SAMPLE = 5
# Minimum total reviewer findings (across categories in a window) required for
# the catch-rate and its cycle-over-cycle trend to be considered well-sampled.
# Matches _MIN_SR_SAMPLE so the gate is on the true catch-rate denominator
# (finding count) rather than task count; a window with ≥_MIN_SR_SAMPLE tasks
# may still have fewer findings when most tasks flag zero or one category.
_MIN_SR_FINDINGS = 5
# Minimum total reviewer findings (across tasks in a window) required to emit
# a hallucination-rate value; prevents noisy rates on tiny windows.
_HALLUCINATION_RATE_MIN_FINDINGS = 10
_MIN_DIRECTION_SR_SAMPLE = 3

# Example-collection caps for miss-example surfacing in the agent prompt.
_EXAMPLE_MAX_PER_CAT = 3
_EXAMPLE_MAX_CATS = 5
_EXAMPLE_MAX_DESC_LEN = 100

# Tokens that indicate a description may contain sensitive material.
# Case-insensitive substring match; keep as tuple for fast `in` checks.
_SENSITIVE_TOKENS: tuple[str, ...] = (
    "password",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "bearer",
    "authorization:",
    "private_key",
    "sk-ant-",
    "eyJ",
)

_URL_RE = re.compile(r"https?://\S+")
_PR_REF_RE = re.compile(r"\b(?:PR|pr)\s*#?\s*\d+\b|#\s*\d+\b")


def _is_sensitive_desc(desc: str) -> bool:
    """Return True if *desc* appears to mention credentials or secrets."""
    lower = desc.lower()
    return any(tok in lower for tok in _SENSITIVE_TOKENS)


def _clean_example_desc(desc: str) -> Optional[str]:
    """Strip URLs and PR refs, collapse whitespace, truncate to _EXAMPLE_MAX_DESC_LEN."""
    desc = _URL_RE.sub("", desc)
    desc = _PR_REF_RE.sub("", desc)
    desc = " ".join(desc.split())
    if not desc:
        return None
    if len(desc) > _EXAMPLE_MAX_DESC_LEN:
        desc = desc[: _EXAMPLE_MAX_DESC_LEN - 3].rstrip() + "..."
    return desc


def _finalize_examples(raw_examples: list[dict]) -> list[dict]:
    """Sort by severity (CRITICAL first), filter sensitive, clean, deduplicate, cap."""
    _sev_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    sorted_ex = sorted(raw_examples, key=lambda x: _sev_order.get(x["severity"], 3))
    seen: set[str] = set()
    result: list[dict] = []
    for ex in sorted_ex:
        cleaned = _clean_example_desc(ex["desc"])
        if not cleaned or _is_sensitive_desc(cleaned):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append({"severity": ex["severity"], "desc": cleaned})
        if len(result) >= _EXAMPLE_MAX_PER_CAT:
            break
    return result


_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}
_EDGE_CATS = {"correctness", "error-handling"}


def _partition_edges_by_origin(edges: list) -> "tuple[list, list]":
    """Split edges_considered into (author, reviewer) entries.

    Entries without an explicit ``origin`` field count as author entries.
    Non-dict entries are dropped.
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
    """Whether the reviewer caught a case the author missed in this task.

    A catch is either a reviewer entry whose ``case`` (lowercased + trimmed)
    is absent from the author entries, OR a reviewer entry that flags
    ``handled: false`` for a case the author had marked ``handled: true``.
    Entries with missing/non-string ``case`` are skipped.
    """

    def _norm(c: Any) -> Optional[str]:
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
    total_author_edges = 0
    total_reviewer_edges = 0
    n_with_author = 0
    n_with_reviewer = 0
    reviewer_caught = 0
    for t in code_tasks:
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
                with_unhandled_flag.append(t)
            else:
                with_all_handled.append(t)
        if reviewer_e:
            n_with_reviewer += 1
            if _reviewer_caught_author_miss(author_e, reviewer_e):
                reviewer_caught += 1
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
        "author_avg_edges_per_task": round(total_author_edges / n_with_author, 2)
        if n_with_author
        else None,
        "reviewer_avg_edges_per_task": round(total_reviewer_edges / n_with_reviewer, 2)
        if n_with_reviewer
        else None,
        "tasks_with_reviewer_edges_count": n_with_reviewer,
        "tasks_with_reviewer_edges_rate": round(n_with_reviewer / n_total, 3),
        "reviewer_caught_count": reviewer_caught,
        "reviewer_caught_rate": round(reviewer_caught / n_total, 3),
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

    Returns (count, cat_stats, type_cat_stats, all_iter_cat_stats, sev_stats,
    scope_cat_stats). ``all_iter_cat_stats`` uses ALL review iterations as the
    human-truth set (not just iteration 0). Falls back to first-attempt
    categories when ``review_loop.review_history`` is absent (backward compat).
    ``sev_stats`` accumulates per-severity hit/miss counts from review_history
    issues across all categories. ``scope_cat_stats`` mirrors
    ``type_cat_stats`` keyed by task scope (small|medium|large) instead of
    task type, using ``task.context['scope']`` (default: small).
    """
    _sw = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}

    def _origin_bucket(
        entry: dict[str, Any] | None,
    ) -> Literal["sr_alone", "reviewer_save"]:
        origin = entry.get("origin") if isinstance(entry, dict) else None
        if isinstance(origin, str) and origin in _REVIEWER_ORIGINS:
            return "reviewer_save"
        return "sr_alone"

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
        check_results = ctx["_self_review_outcome"].get("check_results") or {}
        # Combined view (author SR + reviewer save) — feeds hit/origin-split
        # counters below where _origin_bucket separates `sr_alone` vs
        # `reviewer_save`, so reviewer-stamped entries must remain present.
        sr_failed = {c for c, r in check_results.items() if not r.get("passed", True)}
        # Author-only view — used for FP/sr_failed_count attribution. A
        # reviewer-stamped failure does not represent something the author SR
        # caught, so it must not inflate the precision/false-positive metrics.
        sr_failed_author = {
            c
            for c in sr_failed
            if (check_results.get(c) or {}).get("origin") not in _REVIEWER_ORIGINS
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        cat_weights: dict[str, int] = {}
        rev_cats: dict[str, list[dict]] = {}
        if rev_hist:
            rev_cats = _categorize_review_issues(rev_hist)
            for cat_name, issues in rev_cats.items():
                cat_weights[cat_name] = max(_sw.get(i["severity"], 1) for i in issues)
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {}) - {
            "other"
        }

        # FP tracking: among SR-failed categories, count those never confirmed by
        # any human reviewer across all review iterations (first rejection OR later).
        # Runs for every task (even those with no first-attempt rejection) because
        # those are exactly the cases where SR may have flagged a false positive.
        # Uses the author-only view so reviewer-stamped overrides are not
        # falsely attributed to the author SR's precision.
        all_human_flagged = set(rev_cats.keys()) | human_cats
        for cat in sr_failed_author:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "sr_alone_hit_count": 0,
                    "reviewer_save_hit_count": 0,
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
            if cat not in all_human_flagged:
                cat_stats[cat]["fp_count"] += 1

        if not human_cats:
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
        for cat in human_cats:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "sr_alone_hit_count": 0,
                    "reviewer_save_hit_count": 0,
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
                bucket = _origin_bucket(check_results.get(cat))
                if bucket == "sr_alone":
                    cat_stats[cat]["sr_alone_hit_count"] += 1
                else:
                    cat_stats[cat]["reviewer_save_hit_count"] += 1
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
                        "sr_alone_hit_count": 0,
                        "reviewer_save_hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                type_cat_stats[task_type][cat]["human_count"] += 1
                if cat in sr_failed:
                    type_cat_stats[task_type][cat]["hit_count"] += 1
                    bucket = _origin_bucket(check_results.get(cat))
                    if bucket == "sr_alone":
                        type_cat_stats[task_type][cat]["sr_alone_hit_count"] += 1
                    else:
                        type_cat_stats[task_type][cat]["reviewer_save_hit_count"] += 1
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
                        "sr_alone_hit_count": 0,
                        "reviewer_save_hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                scope_cat_stats[task_scope][cat]["human_count"] += 1
                if cat in sr_failed:
                    scope_cat_stats[task_scope][cat]["hit_count"] += 1
                    bucket = _origin_bucket(check_results.get(cat))
                    if bucket == "sr_alone":
                        scope_cat_stats[task_scope][cat]["sr_alone_hit_count"] += 1
                    else:
                        scope_cat_stats[task_scope][cat]["reviewer_save_hit_count"] += 1
                else:
                    for issue in rev_cats.get(cat, []):
                        scope_cat_stats[task_scope][cat]["miss_examples"].append(
                            {"desc": issue["desc"], "severity": issue["severity"]}
                        )
        # All-iterations human truth: prefer persisted _all_iterations_rejection_categories
        # (written by task_lifecycle on every rejection iteration); fall back to
        # categorising review_history in-line, then to first-attempt categories.
        _stored_all_iter = ctx.get("_all_iterations_rejection_categories") or {}
        if _stored_all_iter:
            all_iter_cats = set(_stored_all_iter.keys()) - {"other"}
        elif rev_hist:
            all_iter_cats = set(_categorize_review_issues_keyword(rev_hist).keys()) - {
                "other"
            }
        else:
            all_iter_cats = human_cats
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


def _sr_skip_stats(
    tasks: list,
) -> tuple[Optional[float], Optional[dict[str, int]]]:
    """Compute skip-rate and reason distribution over tasks-with-SR-outcome.

    Denominator: tasks that have ``_self_review_outcome`` in their context.
    Numerator: tasks whose outcome is a fallback envelope (structured
    ``skip_reason`` present OR verdict=='unknown' with empty check_results).
    Returns (None, None) when the sample is below _MIN_SR_SAMPLE.
    """
    total = 0
    dist: dict[str, int] = {}
    for t in tasks:
        ctx = t.context or {}
        outcome = ctx.get("_self_review_outcome")
        if outcome is None:
            continue
        total += 1
        reason = outcome.get("skip_reason")
        if reason:
            dist[str(reason)] = dist.get(str(reason), 0) + 1
        elif outcome.get("verdict") == "unknown" and not (
            outcome.get("check_results") or {}
        ):
            dist["legacy_unknown"] = dist.get("legacy_unknown", 0) + 1
    if total < _MIN_SR_SAMPLE:
        return None, None
    skipped = sum(dist.values())
    return round(skipped / total, 3), dist


def _compute_execution_evidence_miss_rates(
    tasks: list,
) -> dict[str, dict[str, Any]]:
    """Aggregate miss-rates for execution-evidence SR categories.

    For each task that has ``_self_review_outcome`` with ``check_results``,
    count how many include each execution-evidence category and how many
    reported ``passed: false``. Returns miss_rate, miss_count, sample_count
    per category; omits categories with zero samples.
    """
    totals: dict[str, dict[str, int]] = {}
    for t in tasks:
        ctx = t.context or {}
        check_results = (ctx.get("_self_review_outcome") or {}).get(
            "check_results"
        ) or {}
        for cat in _EXECUTION_EVIDENCE_CATEGORIES:
            result = check_results.get(cat)
            if result is None:
                continue
            if cat not in totals:
                totals[cat] = {"sample_count": 0, "miss_count": 0}
            totals[cat]["sample_count"] += 1
            if not result.get("passed", True):
                totals[cat]["miss_count"] += 1
    out: dict[str, dict[str, Any]] = {}
    for cat, s in totals.items():
        if s["sample_count"] == 0:
            continue
        out[cat] = {
            "miss_rate": round(s["miss_count"] / s["sample_count"], 3),
            "miss_count": s["miss_count"],
            "sample_count": s["sample_count"],
        }
    return out


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
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
            }
            ex = _finalize_examples(stats.get("miss_examples", []))
            if ex:
                entry["examples"] = ex
            categories[cat] = entry
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
            entry: dict[str, Any] = {
                "sample_count": count,
                "miss_rate": weighted_miss_rate,
                "overall_catch_rate": overall_catch_rate,
                "categories": categories,
            }
            if direction_text_map:
                raw_text = direction_text_map.get(direction_id, "")
                if raw_text:
                    entry["direction_text"] = raw_text[:120]
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
                entry["previous_miss_rate"] = prev_miss_rate
                entry["trend"] = dir_trend
                entry["rate_history"] = rate_history
            result[direction_id] = entry
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


def select_sr_miss_examples_for_injection(
    sr_catch_rate_trends: dict,
    direction_id: Optional[str] = None,
    *,
    max_cats: int = 3,
    max_examples_per_cat: int = 3,
    max_total_chars: int = 1200,
) -> dict[str, list[dict]]:
    """Select miss examples for agent SR-prompt injection.

    When *direction_id* is provided and the per-direction snapshot in
    ``by_direction`` contains at least one category with non-empty examples,
    those direction-scoped categories are used as candidates.  Falls back to
    the global top categories when the direction has no qualifying examples.

    Returns ``{category: [{severity, desc}, ...]}`` bounded by *max_cats*,
    *max_examples_per_cat*, and *max_total_chars*.
    """
    global_cats = sr_catch_rate_trends.get("categories") or {}
    if not isinstance(global_cats, dict):
        return {}

    candidate_cats: dict = global_cats
    if direction_id:
        dir_entry = (sr_catch_rate_trends.get("by_direction") or {}).get(
            str(direction_id)
        ) or {}
        dir_cats = dir_entry.get("categories")
        if isinstance(dir_cats, dict) and any(
            isinstance(v, dict)
            and isinstance(v.get("examples"), list)
            and v["examples"]
            for v in dir_cats.values()
        ):
            candidate_cats = dir_cats

    ranked = sorted(
        [
            (cat, data)
            for cat, data in candidate_cats.items()
            if isinstance(data, dict)
            and isinstance(data.get("examples"), list)
            and data["examples"]
        ],
        key=lambda x: x[1].get("weighted_miss_rate") or x[1].get("miss_rate") or 0.0,
        reverse=True,
    )[:max_cats]

    result: dict[str, list[dict]] = {}
    total_chars = 0
    for cat, data in ranked:
        cat_exs: list[dict] = []
        for ex in data["examples"][:max_examples_per_cat]:
            desc = (str(ex.get("desc") or ""))[:200]
            if not desc:
                continue
            if total_chars + len(desc) > max_total_chars:
                break
            total_chars += len(desc)
            cat_exs.append({"severity": ex.get("severity", "MINOR"), "desc": desc})
        if cat_exs:
            result[cat] = cat_exs
        if total_chars >= max_total_chars:
            break
    return result


def _compute_cat_fp_stats(
    tasks: list,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, dict[str, dict[str, int]]],
    dict[str, dict[str, dict[str, int]]],
]:
    """Return per-category fp/tp counts for SR failures.

    A category counted as TP when ANY downstream signal independently
    confirms the SR failure: reviewer_findings, _first_attempt_rejection_categories,
    or review_loop.review_history (keyword-categorised). Otherwise FP.
    Returns (cat_fp, type_cat_fp, scope_cat_fp) where keys are category,
    (task_type, category), and (task_scope, category) respectively.
    """
    cat_fp: dict[str, dict[str, int]] = {}
    type_cat_fp: dict[str, dict[str, dict[str, int]]] = {}
    scope_cat_fp: dict[str, dict[str, dict[str, int]]] = {}
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro:
            continue
        check_results = sro.get("check_results") or {}
        # Author SR failures only — exclude reviewer-stamped overrides so a
        # reviewer rescue is never attributed to the author SR as a TP/FP.
        sr_failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict)
            and not r.get("passed", True)
            and r.get("origin") not in _REVIEWER_ORIGINS
        }
        if not sr_failed:
            continue
        reviewer_cats = {
            f.get("category")
            for f in (sro.get("reviewer_findings") or [])
            if isinstance(f, dict) and f.get("category")
        }
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        rev_hist_cats: set[str] = set()
        if rev_hist:
            rev_hist_cats = set(_categorize_review_issues_keyword(rev_hist).keys())
        confirmed = reviewer_cats | human_cats | rev_hist_cats
        task_scope = ctx.get("scope", DEFAULT_TASK_SCOPE)
        for cat in sr_failed:
            bucket = cat_fp.setdefault(cat, {"fp": 0, "tp": 0})
            tbucket = type_cat_fp.setdefault(task_type, {}).setdefault(
                cat, {"fp": 0, "tp": 0}
            )
            sbucket = scope_cat_fp.setdefault(task_scope, {}).setdefault(
                cat, {"fp": 0, "tp": 0}
            )
            if cat in confirmed:
                bucket["tp"] += 1
                tbucket["tp"] += 1
                sbucket["tp"] += 1
            else:
                bucket["fp"] += 1
                tbucket["fp"] += 1
                sbucket["fp"] += 1
    return cat_fp, type_cat_fp, scope_cat_fp


def _compute_cat_severity_calibration(
    tasks: list,
) -> dict[str, dict[str, int]]:
    """Return per-category severity calibration counts.

    For categories where the agent SR flagged a concern (passed=False, has
    severity) AND the reviewer independently flagged the same category in
    review_history, compare the agent's severity against the reviewer's
    maximum severity for that category.

    Returns {cat: {"under_rated": n, "over_rated": n, "matched": n, "total": n}}.
    Under-rated = agent severity numerically lower than reviewer's — more
    dangerous than over-rating because it suppresses escalation.
    """
    _sw = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    result: dict[str, dict[str, int]] = {}
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro:
            continue
        check_results = sro.get("check_results") or {}
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        if not rev_hist:
            continue
        rev_cats = _categorize_review_issues(rev_hist)
        for cat, rev_issues in rev_cats.items():
            cr = check_results.get(cat)
            if not isinstance(cr, dict):
                continue
            if cr.get("passed", True):
                continue
            if cr.get("origin") in _REVIEWER_ORIGINS:
                continue
            agent_sev = (cr.get("severity") or "").upper()
            if agent_sev not in _sw:
                continue
            sev_vals = [
                _sw[i.get("severity", "")]
                for i in rev_issues
                if isinstance(i, dict) and i.get("severity", "") in _sw
            ]
            if not sev_vals:
                continue
            reviewer_max_val = max(sev_vals)
            agent_val = _sw[agent_sev]
            bucket = result.setdefault(
                cat,
                {"under_rated": 0, "over_rated": 0, "matched": 0, "total": 0},
            )
            bucket["total"] += 1
            if agent_val < reviewer_max_val:
                bucket["under_rated"] += 1
            elif agent_val > reviewer_max_val:
                bucket["over_rated"] += 1
            else:
                bucket["matched"] += 1
    return result


def compute_sr_quality_by_fingerprint(
    tasks: list,
    *,
    min_sample_size: int = 3,
    top_disagreement_categories: int = 3,
) -> dict[str, dict[str, Any]]:
    """Compute additive SR-quality metrics for prompt fingerprint analytics."""
    from app.sr_blind_spots import is_skipped_sr_outcome  # noqa: PLC0415

    buckets: dict[str, dict[str, Any]] = {}
    project_catch_hits = 0
    project_catch_total = 0
    for task in tasks:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        fp = _task_prompt_fingerprint(task)
        bucket = buckets.setdefault(
            fp,
            {
                "tasks": [],
                "sr_sample_size": 0,
                "catch_hits": 0,
                "catch_total": 0,
                "reviewer_rescues": 0,
                "reviewer_rescue_total": 0,
                "disagreements": {},
            },
        )
        bucket["tasks"].append(task)
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        bucket["sr_sample_size"] += 1
        check_results = sro.get("check_results") or {}
        sr_failed = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict)
            and not result.get("passed", True)
            and result.get("origin") not in _REVIEWER_ORIGINS
        }
        reviewer_cats = {
            finding.get("category")
            for finding in (sro.get("reviewer_findings") or [])
            if isinstance(finding, dict) and finding.get("category")
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        confirmed = (
            reviewer_cats
            | set(ctx.get("_first_attempt_rejection_categories") or {})
            | set(_categorize_review_issues(rev_hist).keys())
        )
        hits = len(confirmed & sr_failed)
        misses = confirmed - sr_failed
        bucket["catch_hits"] += hits
        bucket["catch_total"] += len(confirmed)
        bucket["reviewer_rescue_total"] += len(misses)
        bucket["reviewer_rescues"] += len(misses & reviewer_cats)
        project_catch_hits += hits
        project_catch_total += len(confirmed)
        for cat in reviewer_cats - sr_failed:
            entry = bucket["disagreements"].setdefault(
                cat, {"reviewer_overrode_sr_count": 0, "sr_overrode_reviewer_count": 0}
            )
            entry["reviewer_overrode_sr_count"] += 1
        for cat in sr_failed - reviewer_cats:
            entry = bucket["disagreements"].setdefault(
                cat, {"reviewer_overrode_sr_count": 0, "sr_overrode_reviewer_count": 0}
            )
            entry["sr_overrode_reviewer_count"] += 1

    project_catch_rate = (
        round(project_catch_hits / project_catch_total, 3)
        if project_catch_total
        else None
    )
    results: dict[str, dict[str, Any]] = {}
    for fp, bucket in buckets.items():
        result: dict[str, Any] = {"sr_sample_size": bucket["sr_sample_size"]}
        if bucket["sr_sample_size"] < min_sample_size:
            result.update(
                {
                    "sr_catch_rate": None,
                    "sr_catch_sample_size": bucket["catch_total"],
                    "reviewer_rescue_rate": None,
                    "reviewer_rescue_sample_size": bucket["reviewer_rescue_total"],
                    "sr_false_positive_rate": None,
                    "sr_false_positive_sample_size": 0,
                    "sr_false_positive_rate_by_category": [],
                    "sr_reviewer_disagreement_top_categories": [],
                    "sr_catch_rate_delta_vs_project_mean": None,
                }
            )
            results[fp] = result
            continue
        cat_fp_stats, _, _ = _compute_cat_fp_stats(bucket["tasks"])
        fp_total = sum(stats.get("fp", 0) for stats in cat_fp_stats.values())
        tp_total = sum(stats.get("tp", 0) for stats in cat_fp_stats.values())
        catch_rate = (
            round(bucket["catch_hits"] / bucket["catch_total"], 3)
            if bucket["catch_total"]
            else None
        )
        result.update(
            {
                "sr_catch_rate": catch_rate,
                "sr_catch_sample_size": bucket["catch_total"],
                "reviewer_rescue_rate": (
                    round(
                        bucket["reviewer_rescues"] / bucket["reviewer_rescue_total"], 3
                    )
                    if bucket["reviewer_rescue_total"]
                    else None
                ),
                "reviewer_rescue_sample_size": bucket["reviewer_rescue_total"],
                "sr_false_positive_rate": (
                    round(fp_total / (fp_total + tp_total), 3)
                    if (fp_total + tp_total)
                    else None
                ),
                "sr_false_positive_sample_size": fp_total + tp_total,
                "sr_false_positive_rate_by_category": [
                    {
                        "category": cat,
                        "sample_size": stats.get("fp", 0) + stats.get("tp", 0),
                        "false_positive_rate": round(
                            stats.get("fp", 0)
                            / (stats.get("fp", 0) + stats.get("tp", 0)),
                            3,
                        ),
                        "fp_count": stats.get("fp", 0),
                        "tp_count": stats.get("tp", 0),
                    }
                    for cat, stats in sorted(
                        cat_fp_stats.items(),
                        key=lambda item: (
                            -(item[1].get("fp", 0) + item[1].get("tp", 0)),
                            item[0],
                        ),
                    )
                ],
                "sr_reviewer_disagreement_top_categories": [
                    {
                        "category": cat,
                        "sample_size": counts["reviewer_overrode_sr_count"]
                        + counts["sr_overrode_reviewer_count"],
                        **counts,
                    }
                    for cat, counts in sorted(
                        bucket["disagreements"].items(),
                        key=lambda item: (
                            -(
                                item[1]["reviewer_overrode_sr_count"]
                                + item[1]["sr_overrode_reviewer_count"]
                            ),
                            item[0],
                        ),
                    )[:top_disagreement_categories]
                ],
                "sr_catch_rate_delta_vs_project_mean": (
                    round(catch_rate - project_catch_rate, 3)
                    if catch_rate is not None and project_catch_rate is not None
                    else None
                ),
            }
        )
        results[fp] = result
    return results


def _score_reasoning_quality(reasoning: str) -> float:
    """Score reasoning quality on a 0.0–1.0 scale based on specificity signals.

    Rewards: file path citation, test/function name mention, minimum word count (≥6).
    """
    import re

    if not reasoning or not isinstance(reasoning, str):
        return 0.0

    text = reasoning.strip()
    if not text:
        return 0.0

    score = 0.0
    signals = 0

    # Signal 1: file path citation (e.g., "backend/app/foo.py" or "app/foo.py")
    if re.search(r"\b(?:[\w\-./]*[\w\-/]+\.[\w]+)\b", text):
        score += 0.33
        signals += 1

    # Signal 2: test/function name mention (e.g., "test_foo", "test_*.py", "::function")
    if re.search(r"(?:test_\w+|_test\.py|\:\:\w+|\w+::)", text):
        score += 0.33
        signals += 1

    # Signal 3: minimum word count (≥6 words)
    word_count = len(text.split())
    if word_count >= 6:
        score += 0.34
        signals += 1

    # Normalize to 0.0–1.0 range
    return min(1.0, score)


def _compute_reasoning_engagement(
    tasks: list,
    cats: set[str],
) -> dict[str, dict[str, Any]]:
    """For each category in *cats*, compute the fraction of passed entries
    that include a non-trivial reasoning string, plus a quality score.

    A reasoning is 'non-trivial' when it is a non-empty string of more than
    10 characters.  Only tasks with a non-skip SR outcome contribute.

    Returns {cat: {"engagement_rate": float|None, "reasoning_quality_rate": float|None,
                   "with_reasoning": int, "with_quality": int, "total_passes": int}}.
    """
    from app.sr_blind_spots import is_skipped_sr_outcome  # noqa: PLC0415

    stats: dict[str, dict[str, int | float]] = {
        cat: {
            "with_reasoning": 0,
            "with_quality": 0,
            "total_passes": 0,
            "quality_scores": [],
        }
        for cat in cats
    }
    for t in tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        for cat in cats:
            entry = check_results.get(cat)
            if not isinstance(entry, dict) or not entry.get("passed", True):
                continue
            stats[cat]["total_passes"] += 1
            reasoning = entry.get("reasoning") or ""
            if isinstance(reasoning, str) and len(reasoning.strip()) > 10:
                stats[cat]["with_reasoning"] += 1

            # Compute quality score
            quality_score = _score_reasoning_quality(reasoning)
            stats[cat]["quality_scores"].append(quality_score)
            if quality_score > 0.5:
                stats[cat]["with_quality"] += 1

    result: dict[str, dict[str, Any]] = {}
    for cat, s in stats.items():
        total = s["total_passes"]
        quality_scores = s.get("quality_scores", [])
        avg_quality = (
            round(sum(quality_scores) / len(quality_scores), 3)
            if quality_scores
            else None
        )
        result[cat] = {
            "engagement_rate": round(s["with_reasoning"] / total, 3)
            if total > 0
            else None,
            "reasoning_quality_rate": avg_quality,
            "with_reasoning": s["with_reasoning"],
            "with_quality": s["with_quality"],
            "total_passes": total,
        }
    return result


def _collect_prompt_versions(tasks: list) -> dict:
    """Return a Counter of prompt_version values seen in _self_review_outcome."""
    from collections import Counter  # noqa: PLC0415

    c: Counter = Counter()
    for t in tasks:
        pv = (t.context or {}).get("_self_review_outcome", {}).get("prompt_version")
        if pv is not None:
            c[str(pv)] += 1
    return c


def _percentile(values: list, p: float) -> float:
    """Compute p-th percentile (0–100) of a list using linear interpolation."""
    sv = sorted(values)
    n = len(sv)
    if n == 1:
        return float(sv[0])
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sv[-1])
    frac = idx - lo
    return sv[lo] + frac * (sv[hi] - sv[lo])


def _compute_miss_latency_stats(tasks: list) -> dict:
    """Return per-category time-to-detect latency stats for SR misses.

    An SR miss is a category where check_results[cat].passed == True but the
    same category appears in _first_attempt_rejection_categories.  Latency =
    rejection timestamp − SR completion timestamp.

    Skips samples where both timestamps fall back to the same value (task.updated_at),
    because the latency would be 0 and not a real signal.
    """

    def _parse_iso(s: object) -> Optional[datetime]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(str(s))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, TypeError):
            return None

    def _updated_naive(t: object) -> Optional[datetime]:
        ua = getattr(t, "updated_at", None)
        if ua is None:
            return None
        return ua.replace(tzinfo=None) if ua.tzinfo else ua  # type: ignore[return-value]

    _LONE_CHECKER_P95_THRESHOLD = 7 * 86400  # 7 days in seconds

    # per category: list of latency seconds
    latencies: dict[str, list[float]] = {}

    for task in tasks:
        ctx = task.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        check_results = sr_outcome.get("check_results") or {}
        rejection_cats = ctx.get("_first_attempt_rejection_categories") or {}

        if not check_results or not rejection_cats:
            continue

        missed_cats = [
            cat
            for cat, cr in check_results.items()
            if isinstance(cr, dict)
            and cr.get("passed") is True
            and cat in rejection_cats
        ]
        if not missed_cats:
            continue

        # Resolve SR timestamp
        sr_ts: Optional[datetime] = None
        for field in ("completed_at", "computed_at", "stamped_at"):
            sr_ts = _parse_iso(sr_outcome.get(field))
            if sr_ts is not None:
                break
        sr_fallback = sr_ts is None
        if sr_ts is None:
            sr_ts = _updated_naive(task)

        # Resolve rejection timestamp
        rej_ts: Optional[datetime] = None
        for field in (
            "_first_attempt_rejection_at",
            "_first_attempt_rejection_categories_at",
        ):
            rej_ts = _parse_iso(ctx.get(field))
            if rej_ts is not None:
                break
        rej_fallback = rej_ts is None
        if rej_ts is None:
            rej_ts = _updated_naive(task)

        # Skip if both timestamps came from the same fallback — no real signal
        if sr_fallback and rej_fallback:
            continue

        if sr_ts is None or rej_ts is None:
            continue

        latency = (rej_ts - sr_ts).total_seconds()
        if latency <= 0:
            continue

        for cat in missed_cats:
            latencies.setdefault(cat, []).append(latency)

    result: dict[str, dict] = {}
    for cat, lats in latencies.items():
        n = len(lats)
        p50 = int(_percentile(lats, 50))
        p95 = int(_percentile(lats, 95))
        result[cat] = {
            "time_to_miss_detect_p50_sec": p50,
            "time_to_miss_detect_p95_sec": p95,
            "time_to_miss_detect_sample_count": n,
            "time_to_miss_detect_lone_checker": n >= 2
            and p95 > _LONE_CHECKER_P95_THRESHOLD,
        }
    return result


def _is_trend_significant(p1: float, n1: int, p2: float, n2: int) -> bool:
    """Two-proportion z-test: return True when the observed difference exceeds
    what sampling noise alone predicts at the _TREND_Z_MIN threshold.

    Uses a pooled proportion under H0 (no real difference).  Falls back to the
    raw delta >= _TREND_DELTA_THRESHOLD when the pooled SE is degenerate (both
    rates are 0 or 1 and the standard error collapses to zero).
    """
    if n1 < 1 or n2 < 1:
        return False
    pooled_p = (p1 * n1 + p2 * n2) / (n1 + n2)
    if pooled_p <= 0 or pooled_p >= 1:
        return abs(p1 - p2) >= _TREND_DELTA_THRESHOLD
    se = (pooled_p * (1 - pooled_p) * (1 / n1 + 1 / n2)) ** 0.5
    if se == 0:
        return False
    return abs(p1 - p2) / se >= _TREND_Z_MIN


async def compute_sr_catch_rate_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Compare SR prediction hit-rate per category, bucketed by strategic cycle.

    Primary mode: uses the two most recently COMPLETED strategic cycles as
    "current" and "previous" buckets (tasks keyed by ``strategic_cycle_id``).
    Rolls further back through completed cycles if either bucket has fewer than
    :data:`_MIN_SR_SAMPLE` qualifying tasks (both ``_self_review_outcome`` and
    ``_first_attempt_rejection_categories`` present).

    Fallback mode: when fewer than two completed cycles exist *or* no pair of
    consecutive cycles has enough tasks, falls back to the existing 14-day
    calendar windows so the metric is non-blank during onboarding and for
    tasks that were never assigned a ``strategic_cycle_id``.

    **Side effect**: writes the result to ``project.design_doc["sr_catch_rate_trends"]``
    and maintains a per-category ``rate_history`` list (capped at 5 entries).
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_count = 0
    previous_count = 0
    current_stats: dict[str, dict] = {}
    previous_stats: dict[str, dict] = {}
    current_type_stats: dict[str, dict[str, dict]] = {}
    current_window_tasks: list = []
    previous_window_tasks: list = []
    current_all_iter_stats: dict[str, dict] = {}
    previous_all_iter_stats: dict[str, dict] = {}
    current_sev_stats: dict[str, dict] = {}
    previous_sev_stats: dict[str, dict] = {}

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        # Targeted fetch scoped to the candidate cycle IDs — no date cap, no row
        # cap — so each cycle's full task set is available for comparison,
        # independent of the throughput-capped recency window in all_tasks.
        _relevant_cycle_ids = [r.id for r in cycle_rows]
        _dialect = session.bind.dialect.name if session.bind else "postgresql"
        _cycle_where = [
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.COMPLETED]),
            Task.strategic_cycle_id.in_(_relevant_cycle_ids),
        ]
        if _dialect != "sqlite":
            _cycle_where.append(Task.context.has_key("_self_review_outcome"))
        _cycle_tasks = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.context,
                            Task.created_at,
                            Task.project_id,
                            Task.strategic_cycle_id,
                            Task.payloads,
                        )
                    )
                    .where(*_cycle_where)
                )
            )
            .scalars()
            .all()
        )
        task_by_cycle: dict[str, list] = {}
        for t in _cycle_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            (
                c_count,
                c_stats,
                c_type_stats,
                c_all_iter,
                c_sev,
                c_scope_stats,
            ) = _stats_for_tasks(task_by_cycle.get(c_cycle.id, []))
            p_count, p_stats, _, p_all_iter, p_sev, _ = _stats_for_tasks(
                task_by_cycle.get(p_cycle.id, [])
            )
            if c_count >= _MIN_SR_SAMPLE and p_count >= _MIN_SR_SAMPLE:
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_count = c_count
                previous_count = p_count
                current_stats = c_stats
                previous_stats = p_stats
                current_type_stats = c_type_stats
                current_scope_stats = c_scope_stats
                current_window_tasks = task_by_cycle.get(c_cycle.id, [])
                previous_window_tasks = task_by_cycle.get(p_cycle.id, [])
                current_all_iter_stats = c_all_iter
                previous_all_iter_stats = p_all_iter
                current_sev_stats = c_sev
                previous_sev_stats = p_sev
                break

    if not use_cycle_mode:
        # Calendar fallback for onboarding / tasks without strategic_cycle_id.
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        cal_current = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        cal_previous = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]
        (
            current_count,
            current_stats,
            current_type_stats,
            current_all_iter_stats,
            current_sev_stats,
            current_scope_stats,
        ) = _stats_for_tasks(cal_current)
        (
            previous_count,
            previous_stats,
            _,
            previous_all_iter_stats,
            previous_sev_stats,
            _,
        ) = _stats_for_tasks(cal_previous)
        current_window_tasks = cal_current
        previous_window_tasks = cal_previous

    # Gate on finding count (true catch-rate denominator), not task count.
    # finding_count = total reviewer-flagged (task, category) pairs in the window.
    current_finding_count = sum(s.get("human_count", 0) for s in current_stats.values())
    previous_finding_count = sum(
        s.get("human_count", 0) for s in previous_stats.values()
    )
    if (
        current_finding_count < _MIN_SR_FINDINGS
        or previous_finding_count < _MIN_SR_FINDINGS
    ):
        # Stamp a heartbeat so the staleness watchdog knows the job ran but had
        # insufficient reviewer-rejection findings (low-data / idle state), not
        # that the computation broke.  Mirrors sr_blind_spots heartbeat pattern.
        # data_sufficient=False distinguishes this from a real refresh so the
        # watchdog and UI can tell when the displayed trend is actually current.
        _proj_row = await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(skip_locked=False, key_share=True)
        )
        _project = _proj_row.scalar_one_or_none()
        if _project is not None:
            _doc = dict(_project.design_doc or {})
            _trends = dict(_doc.get("sr_catch_rate_trends") or {})
            _trends["signal_last_computed_at"] = now.isoformat()
            _trends["data_sufficient"] = False
            # last_sufficient_computed_at is preserved (not overwritten) so
            # callers can tell when data was last actually sufficient.
            _doc["sr_catch_rate_trends"] = _trends
            _project.design_doc = _doc
            flag_modified(_project, "design_doc")
            session.add(_project)
        return None

    all_cats = (
        set(current_stats.keys())
        | set(previous_stats.keys())
        | set(current_all_iter_stats.keys())
    )
    if not all_cats:
        return None

    _empty_cat: dict[str, Any] = {
        "hit_count": 0,
        "sr_alone_hit_count": 0,
        "reviewer_save_hit_count": 0,
        "human_count": 0,
        "w_hit": 0.0,
        "w_total": 0.0,
        "sr_failed_count": 0,
        "fp_count": 0,
    }
    categories: dict[str, Any] = {}
    for cat in all_cats:
        curr = current_stats.get(cat, _empty_cat)
        prev = previous_stats.get(cat, _empty_cat)
        current_hit_rate = (
            round(curr["hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )
        previous_hit_rate = (
            round(prev["hit_count"] / prev["human_count"], 3)
            if prev["human_count"] > 0
            else None
        )
        miss_rate = (
            round(1 - current_hit_rate, 3) if current_hit_rate is not None else None
        )
        sr_alone_hit_rate = (
            round(curr["sr_alone_hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )
        reviewer_save_rate = (
            round(curr["reviewer_save_hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )

        # Only compute trend when both windows have enough findings for the
        # delta to be meaningful; fewer findings make the delta pure noise.
        # A two-proportion z-test additionally gates directional labels: even
        # with _MIN_SR_FINDINGS samples a delta near the floor threshold can
        # be pure sampling noise, so we require statistical significance before
        # asserting "increasing" / "decreasing".
        _curr_findings = curr.get("human_count", 0)
        _prev_findings = prev.get("human_count", 0)
        _has_trend_data = (
            current_hit_rate is not None
            and previous_hit_rate is not None
            and _curr_findings >= _MIN_SR_FINDINGS
            and _prev_findings >= _MIN_SR_FINDINGS
        )
        if _has_trend_data:
            delta = current_hit_rate - previous_hit_rate
            _sig = _is_trend_significant(
                current_hit_rate,
                _curr_findings,
                previous_hit_rate,
                _prev_findings,
            )
            if _sig and delta >= _TREND_DELTA_THRESHOLD:
                trend = "increasing"
            elif _sig and delta <= -_TREND_DELTA_THRESHOLD:
                trend = "decreasing"
            else:
                trend = "stable"
            trend_significant: Optional[bool] = (
                _sig and abs(delta) >= _TREND_DELTA_THRESHOLD
            )
        else:
            trend = "stable"
            trend_significant = None

        w_total = curr.get("w_total", 0.0)
        weighted_miss_rate = (
            round(1.0 - curr["w_hit"] / w_total, 3) if w_total > 0 else miss_rate
        )

        curr_sr_failed = curr.get("sr_failed_count", 0)
        curr_fp = curr.get("fp_count", 0)
        prev_sr_failed = prev.get("sr_failed_count", 0)
        prev_fp = prev.get("fp_count", 0)
        current_fp_rate = (
            round(curr_fp / curr_sr_failed, 3) if curr_sr_failed > 0 else None
        )
        previous_fp_rate = (
            round(prev_fp / prev_sr_failed, 3) if prev_sr_failed > 0 else None
        )
        if current_fp_rate is not None and previous_fp_rate is not None:
            fp_delta = current_fp_rate - previous_fp_rate
            _fp_sig = (
                curr_sr_failed >= _MIN_SR_FINDINGS
                and prev_sr_failed >= _MIN_SR_FINDINGS
                and _is_trend_significant(
                    current_fp_rate, curr_sr_failed, previous_fp_rate, prev_sr_failed
                )
            )
            if _fp_sig and fp_delta >= _TREND_DELTA_THRESHOLD:
                fp_trend = "increasing"
            elif _fp_sig and fp_delta <= -_TREND_DELTA_THRESHOLD:
                fp_trend = "decreasing"
            else:
                fp_trend = "stable"
        else:
            fp_trend = "stable"
        precision = (
            round((curr_sr_failed - curr_fp) / curr_sr_failed, 3)
            if curr_sr_failed > 0
            else None
        )

        curr_all = current_all_iter_stats.get(cat, {"hit_count": 0, "human_count": 0})
        all_iter_hit_rate = (
            round(curr_all["hit_count"] / curr_all["human_count"], 3)
            if curr_all["human_count"] > 0
            else None
        )
        all_iter_miss_rate = (
            round(1 - all_iter_hit_rate, 3) if all_iter_hit_rate is not None else None
        )

        # Precision/recall quadrant: compare current vs previous window deltas.
        previous_precision = (
            round((prev_sr_failed - prev_fp) / prev_sr_failed, 3)
            if prev_sr_failed > 0
            else None
        )
        recall_delta: Optional[float] = (
            round(current_hit_rate - previous_hit_rate, 3)
            if current_hit_rate is not None and previous_hit_rate is not None
            else None
        )
        precision_delta: Optional[float] = (
            round(precision - previous_precision, 3)
            if precision is not None and previous_precision is not None
            else None
        )
        precision_recall_quadrant: Optional[str] = None
        if recall_delta is not None and precision_delta is not None:
            _q = _PRECISION_RECALL_QUADRANT_THRESHOLD
            p_up = precision_delta >= _q
            p_down = precision_delta <= -_q
            r_up = recall_delta >= _q
            r_down = recall_delta <= -_q
            if p_up and r_up:
                precision_recall_quadrant = "healthy"
            elif p_up and r_down:
                precision_recall_quadrant = "conservative_shift"
            elif p_down and r_up:
                precision_recall_quadrant = "noisy_shift"
            elif p_down and r_down:
                precision_recall_quadrant = "both_declining"
            elif not p_up and not p_down and not r_up and not r_down:
                precision_recall_quadrant = "flat"
            else:
                precision_recall_quadrant = "mixed"

        categories[cat] = {
            "current_hit_rate": current_hit_rate,
            "previous_hit_rate": previous_hit_rate,
            "current_human_count": int(curr.get("human_count") or 0),
            "previous_human_count": int(prev.get("human_count") or 0),
            "low_confidence_finding_count": _curr_findings < _MIN_SR_FINDINGS,
            "sr_alone_hit_count": int(curr.get("sr_alone_hit_count") or 0),
            "reviewer_save_hit_count": int(curr.get("reviewer_save_hit_count") or 0),
            "sr_alone_hit_rate": sr_alone_hit_rate,
            "reviewer_save_rate": reviewer_save_rate,
            "miss_rate": miss_rate,
            "weighted_miss_rate": weighted_miss_rate,
            "trend": trend,
            "trend_significant": trend_significant,
            "false_positive_rate": current_fp_rate,
            "previous_fp_rate": previous_fp_rate,
            "fp_trend": fp_trend,
            "precision": precision,
            "all_iterations_catch_rate": all_iter_hit_rate,
            "all_iterations_miss_rate": all_iter_miss_rate,
            "precision_recall_quadrant": precision_recall_quadrant,
            "precision_delta": precision_delta,
            "recall_delta": recall_delta,
        }

    # Attach miss examples to top-_EXAMPLE_MAX_CATS categories by weighted_miss_rate.
    top_cats_for_examples = sorted(
        all_cats,
        key=lambda c: categories[c].get("weighted_miss_rate") or 0.0,
        reverse=True,
    )[:_EXAMPLE_MAX_CATS]
    for cat in top_cats_for_examples:
        raw = (current_stats.get(cat) or {}).get("miss_examples", [])
        examples = _finalize_examples(raw)
        if examples:
            categories[cat]["examples"] = examples

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing: dict = dict(doc.get("sr_catch_rate_trends") or {})
    existing_computed_at: Optional[str] = existing.get("computed_at")

    is_new_window = True
    if use_cycle_mode:
        # In cycle mode a new window = new current cycle id.
        is_new_window = existing.get("current_cycle_id") != current_cycle_id
    elif existing_computed_at:
        try:
            last_dt = datetime.fromisoformat(existing_computed_at)
            is_new_window = (now - last_dt).days >= 14
        except (ValueError, TypeError):
            pass

    # Detect bucket-mode transition (cycle ↔ calendar). Rate history entries
    # computed in one mode are not comparable to those from the other (cycle
    # buckets span multi-week strategic cycles; calendar buckets are fixed
    # 14-day windows), so history is reset on transition to avoid mixing them.
    current_bucket_mode = "cycle" if use_cycle_mode else "calendar"
    mode_changed = (
        existing.get("bucket_mode") is not None
        and existing.get("bucket_mode") != current_bucket_mode
    )

    existing_cats: dict = dict(existing.get("categories") or {})
    for cat, data in categories.items():
        prev_cat: dict = dict(existing_cats.get(cat) or {})
        rate_history: list = list(prev_cat.get("rate_history") or [])
        if mode_changed:
            rate_history = []
        if (is_new_window or not rate_history) and data["current_hit_rate"] is not None:
            rate_history.append(data["current_hit_rate"])
            rate_history = rate_history[-5:]
        data["rate_history"] = rate_history

    # Mark categories currently below the baseline floor so the UI can surface them.
    _floor_thresh: float = float(doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
    _floor_min_win: int = int(
        doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )
    for cat, data in categories.items():
        history = data.get("rate_history") or []
        data["below_floor"] = len(history) >= _floor_min_win and all(
            r < _floor_thresh for r in history[-_floor_min_win:]
        )

    # Attach reasoning engagement rate for all categories (not just regressing ones).
    # Regressing = trend=="decreasing" OR below_floor.
    regressing_cats = {
        cat
        for cat, data in categories.items()
        if data.get("trend") == "decreasing" or data.get("below_floor")
    }
    all_cats = set(categories.keys())
    eng = _compute_reasoning_engagement(current_window_tasks, all_cats)
    for cat, cat_eng in eng.items():
        categories[cat]["reasoning_engagement_rate"] = cat_eng["engagement_rate"]
        categories[cat]["reasoning_quality_rate"] = cat_eng["reasoning_quality_rate"]
        categories[cat]["reasoning_engagement_with"] = cat_eng["with_reasoning"]
        categories[cat]["reasoning_engagement_total"] = cat_eng["total_passes"]

    # Attach engagement_thinning flag for each category.
    # Mirrors the thinning Alert predicate (lines ~4918) but also fires for regressing
    # categories — the agent gate consumer takes the union of regressing+thinning itself.
    _eng_threshold: float = float(
        doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD
    )
    _eng_min_passes: int = int(
        doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES
    )
    for cat, data in categories.items():
        _rate = data.get("reasoning_engagement_rate")
        _total = data.get("reasoning_engagement_total") or 0
        data["engagement_thinning"] = bool(
            _rate is not None and _rate < _eng_threshold and _total >= _eng_min_passes
        )

    # Attach false-positive stats: SR flagged but reviewer didn't independently flag.
    # Both windows use _compute_cat_fp_stats so reviewer_findings count as confirmations
    # consistently — without this, previous_fp_rate overcounts FPs and can trigger the
    # sr_false_positive dispatcher signal spuriously after a fresh-reviewer wave.
    cat_fp_stats, type_cat_fp_stats, scope_cat_fp_stats = _compute_cat_fp_stats(
        current_window_tasks
    )
    prev_cat_fp_stats, _, _ = _compute_cat_fp_stats(previous_window_tasks)
    for cat, data in categories.items():
        fp_data = cat_fp_stats.get(cat) or {}
        fp = fp_data.get("fp", 0)
        tp = fp_data.get("tp", 0)
        data["fp_count"] = fp
        data["tp_count"] = tp
        data["false_positive_rate"] = (
            round(fp / (fp + tp), 3) if (fp + tp) > 0 else None
        )
        prev_fp_data = prev_cat_fp_stats.get(cat) or {}
        prev_fp = prev_fp_data.get("fp", 0)
        prev_tp = prev_fp_data.get("tp", 0)
        data["previous_fp_rate"] = (
            round(prev_fp / (prev_fp + prev_tp), 3) if (prev_fp + prev_tp) > 0 else None
        )

    # Attach severity calibration stats: compares agent severity vs reviewer severity
    # for categories where both sides flagged it. Under-rating is the dangerous case.
    sev_cal = _compute_cat_severity_calibration(current_window_tasks)
    for cat, data in categories.items():
        cal = sev_cal.get(cat)
        if cal:
            data["severity_calibration"] = cal

    # Attach time-to-miss-detect latency stats per category.
    miss_latency = _compute_miss_latency_stats(current_window_tasks)
    for cat, data in categories.items():
        stats = miss_latency.get(cat) or {}
        data["time_to_miss_detect_p50_sec"] = stats.get("time_to_miss_detect_p50_sec")
        data["time_to_miss_detect_p95_sec"] = stats.get("time_to_miss_detect_p95_sec")
        data["time_to_miss_detect_sample_count"] = stats.get(
            "time_to_miss_detect_sample_count", 0
        )
        data["time_to_miss_detect_lone_checker"] = stats.get(
            "time_to_miss_detect_lone_checker", False
        )

    # Compute per-task-type category breakdown from current window only.
    # Cells with fewer than _MIN_SR_SAMPLE observations are omitted.
    by_task_type: dict[str, dict[str, Any]] = {}
    for ttype, tcat_stats in current_type_stats.items():
        type_cats: dict[str, Any] = {}
        for cat, stats in tcat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            sr_alone_hit_rate = (
                round(stats["sr_alone_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            reviewer_save_rate = (
                round(stats["reviewer_save_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            raw_ex = stats.get("miss_examples", [])
            ex = _finalize_examples(raw_ex)
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
                "sr_alone_hit_count": stats["sr_alone_hit_count"],
                "reviewer_save_hit_count": stats["reviewer_save_hit_count"],
                "sr_alone_hit_rate": sr_alone_hit_rate,
                "reviewer_save_rate": reviewer_save_rate,
            }
            if ex:
                entry["examples"] = ex
            tfp = (type_cat_fp_stats.get(ttype) or {}).get(cat) or {}
            fp_t = tfp.get("fp", 0)
            tp_t = tfp.get("tp", 0)
            entry["fp_count"] = fp_t
            entry["tp_count"] = tp_t
            entry["false_positive_rate"] = (
                round(fp_t / (fp_t + tp_t), 3) if (fp_t + tp_t) > 0 else None
            )
            type_cats[cat] = entry
        if type_cats:
            by_task_type[ttype] = type_cats

    # Per-fix-iteration breakdown — bucket current_window_tasks by the
    # fix_iteration stamped on _self_review_outcome (1, 2, 3+) so operators
    # can see which categories degrade across review-fix passes vs. which
    # are weak from the start. Original tasks (no fix_iteration) bucket as "1".
    def _fix_iter_bucket(t) -> str:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        fi = sro.get("fix_iteration")
        if fi is None:
            fi_int = 1
        else:
            try:
                fi_int = int(fi)
            except (TypeError, ValueError):
                fi_int = 1
        if fi_int <= 1:
            return "1"
        if fi_int == 2:
            return "2"
        return "3+"

    by_fix_iteration: dict[str, dict[str, Any]] = {}
    fix_iter_buckets: dict[str, list] = {"1": [], "2": [], "3+": []}
    for _t in current_window_tasks:
        fix_iter_buckets[_fix_iter_bucket(_t)].append(_t)
    for bucket_key in ("1", "2", "3+"):
        bucket_tasks = fix_iter_buckets[bucket_key]
        if not bucket_tasks:
            continue
        _, b_cat_stats, _, b_all_iter_stats, _, _ = _stats_for_tasks(bucket_tasks)
        bucket_cats: dict[str, Any] = {}
        for cat, stats in b_cat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            entry: dict[str, Any] = {
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
            }
            ai = b_all_iter_stats.get(cat)
            if ai and ai["human_count"] > 0:
                ai_hit_rate = round(ai["hit_count"] / ai["human_count"], 3)
                entry["all_iterations_miss_rate"] = round(1 - ai_hit_rate, 3)
            ex = _finalize_examples(stats.get("miss_examples", []))
            if ex:
                entry["examples"] = ex
            bucket_cats[cat] = entry
        if bucket_cats:
            by_fix_iteration[bucket_key] = bucket_cats

    # Compute per-scope category breakdown from current window only.
    # Cells with fewer than _MIN_SR_SAMPLE observations are omitted.
    existing_by_scope: dict = dict(existing.get("by_scope") or {})
    by_scope: dict[str, dict[str, Any]] = {}
    for scope, scat_stats in current_scope_stats.items():
        scope_cats: dict[str, Any] = {}
        for cat, stats in scat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            sr_alone_hit_rate = (
                round(stats["sr_alone_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            reviewer_save_rate = (
                round(stats["reviewer_save_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            raw_ex = stats.get("miss_examples", [])
            ex = _finalize_examples(raw_ex)
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
                "sr_alone_hit_count": stats["sr_alone_hit_count"],
                "reviewer_save_hit_count": stats["reviewer_save_hit_count"],
                "sr_alone_hit_rate": sr_alone_hit_rate,
                "reviewer_save_rate": reviewer_save_rate,
            }
            if ex:
                entry["examples"] = ex
            sfp = (scope_cat_fp_stats.get(scope) or {}).get(cat) or {}
            fp_s = sfp.get("fp", 0)
            tp_s = sfp.get("tp", 0)
            entry["fp_count"] = fp_s
            entry["tp_count"] = tp_s
            entry["false_positive_rate"] = (
                round(fp_s / (fp_s + tp_s), 3) if (fp_s + tp_s) > 0 else None
            )
            # Persist rate_history and trend for this (scope, category) cell.
            prev_scope_entry: dict = dict(
                (existing_by_scope.get(scope) or {}).get(cat) or {}
            )
            scope_rate_history: list = list(prev_scope_entry.get("rate_history") or [])
            if mode_changed:
                scope_rate_history = []
            if is_new_window or not scope_rate_history:
                scope_rate_history.append(hit_rate)
                scope_rate_history = scope_rate_history[-5:]
            prev_scope_hit_rate = prev_scope_entry.get("hit_rate")
            _prev_scope_sample = int(prev_scope_entry.get("sample_count") or 0)
            if prev_scope_hit_rate is not None:
                s_delta = hit_rate - prev_scope_hit_rate
                _scope_n_curr = stats["human_count"]
                _scope_sig = (
                    _scope_n_curr >= _MIN_SR_FINDINGS
                    and _prev_scope_sample >= _MIN_SR_FINDINGS
                    and _is_trend_significant(
                        hit_rate, _scope_n_curr, prev_scope_hit_rate, _prev_scope_sample
                    )
                )
                if _scope_sig and s_delta >= _TREND_DELTA_THRESHOLD:
                    scope_trend: str = "increasing"
                elif _scope_sig and s_delta <= -_TREND_DELTA_THRESHOLD:
                    scope_trend = "decreasing"
                else:
                    scope_trend = "stable"
            else:
                scope_trend = "stable"
            entry["rate_history"] = scope_rate_history
            entry["trend"] = scope_trend
            scope_cats[cat] = entry
        if scope_cats:
            by_scope[scope] = scope_cats

    sr_skip_rate, sr_skip_dist = _sr_skip_stats(current_window_tasks)

    # Compute per-severity catch-rate breakdown across all categories.
    by_severity: dict[str, Any] = {}
    for sev in ("CRITICAL", "MAJOR", "MINOR"):
        curr_s = current_sev_stats.get(sev, {"hit_count": 0, "human_count": 0})
        prev_s = previous_sev_stats.get(sev, {"hit_count": 0, "human_count": 0})
        curr_hit = (
            round(curr_s["hit_count"] / curr_s["human_count"], 3)
            if curr_s["human_count"] > 0
            else None
        )
        prev_hit = (
            round(prev_s["hit_count"] / prev_s["human_count"], 3)
            if prev_s["human_count"] > 0
            else None
        )
        curr_miss = round(1.0 - curr_hit, 3) if curr_hit is not None else None
        prev_miss = round(1.0 - prev_hit, 3) if prev_hit is not None else None
        if curr_miss is not None and prev_miss is not None:
            sev_delta = curr_miss - prev_miss
            thr = _SR_SEVERITY_DELTA_THRESHOLDS[sev]
            _sev_n_curr = curr_s["human_count"]
            _sev_n_prev = prev_s["human_count"]
            _sev_sig = (
                _sev_n_curr >= _MIN_SR_FINDINGS
                and _sev_n_prev >= _MIN_SR_FINDINGS
                and _is_trend_significant(
                    curr_miss, _sev_n_curr, prev_miss, _sev_n_prev
                )
            )
            sev_trend = (
                "regressing"
                if _sev_sig and sev_delta >= thr
                else "improving"
                if _sev_sig and sev_delta <= -thr
                else "stable"
            )
        else:
            sev_trend = "stable"
        by_severity[sev] = {
            "current_miss_rate": curr_miss,
            "previous_miss_rate": prev_miss,
            "current_hit_rate": curr_hit,
            "previous_hit_rate": prev_hit,
            "current_sample": curr_s["human_count"],
            "previous_sample": prev_s["human_count"],
            "trend": sev_trend,
        }

    # Compute overall (aggregate) hit rates across all categories, weighted by human_count.
    # This represents the true cycle-over-cycle change of the headline metric.
    current_total_hits = 0
    current_total_humans = 0
    previous_total_hits = 0
    previous_total_humans = 0
    for data in categories.values():
        curr_hit_rate = data.get("current_hit_rate")
        curr_human_count = data.get("current_human_count", 0)
        prev_hit_rate = data.get("previous_hit_rate")
        prev_human_count = data.get("previous_human_count", 0)
        if curr_hit_rate is not None and curr_human_count > 0:
            current_total_hits += round(curr_hit_rate * curr_human_count)
            current_total_humans += curr_human_count
        if prev_hit_rate is not None and prev_human_count > 0:
            previous_total_hits += round(prev_hit_rate * prev_human_count)
            previous_total_humans += prev_human_count
    overall_hit_rate = (
        round(current_total_hits / current_total_humans, 3)
        if current_total_humans > 0
        else None
    )
    previous_overall_hit_rate = (
        round(previous_total_hits / previous_total_humans, 3)
        if previous_total_humans > 0
        else None
    )

    # Compute author-only (SR-alone) overall hit rate by summing raw integer counts
    # across categories — preferred over reconstructing from rounded per-category rates.
    current_sr_alone_hits = 0
    current_sr_alone_humans = 0
    previous_sr_alone_hits = 0
    previous_sr_alone_humans = 0
    for cat in all_cats:
        c = current_stats.get(cat, _empty_cat)
        p = previous_stats.get(cat, _empty_cat)
        if c.get("human_count", 0) > 0:
            current_sr_alone_hits += c.get("sr_alone_hit_count", 0)
            current_sr_alone_humans += c["human_count"]
        if p.get("human_count", 0) > 0:
            previous_sr_alone_hits += p.get("sr_alone_hit_count", 0)
            previous_sr_alone_humans += p["human_count"]
    overall_sr_alone_hit_rate: Optional[float] = (
        round(current_sr_alone_hits / current_sr_alone_humans, 3)
        if current_sr_alone_humans > 0
        else None
    )
    previous_overall_sr_alone_hit_rate: Optional[float] = (
        round(previous_sr_alone_hits / previous_sr_alone_humans, 3)
        if previous_sr_alone_humans > 0
        else None
    )
    _sr_alone_data_sufficient = (
        overall_sr_alone_hit_rate is not None
        and previous_overall_sr_alone_hit_rate is not None
        and current_sr_alone_humans >= _MIN_SR_FINDINGS
        and previous_sr_alone_humans >= _MIN_SR_FINDINGS
    )
    if _sr_alone_data_sufficient:
        _sr_alone_delta = overall_sr_alone_hit_rate - previous_overall_sr_alone_hit_rate
        _sr_alone_sig = _is_trend_significant(
            overall_sr_alone_hit_rate,
            current_sr_alone_humans,
            previous_overall_sr_alone_hit_rate,
            previous_sr_alone_humans,
        )
        if _sr_alone_sig and _sr_alone_delta >= _TREND_DELTA_THRESHOLD:
            overall_sr_alone_trend_verdict: str = "improving"
        elif _sr_alone_sig and _sr_alone_delta <= -_TREND_DELTA_THRESHOLD:
            overall_sr_alone_trend_verdict = "declining"
        else:
            overall_sr_alone_trend_verdict = "stable"
        overall_sr_alone_trend_significant: Optional[bool] = (
            _sr_alone_sig and abs(_sr_alone_delta) >= _TREND_DELTA_THRESHOLD
        )
    else:
        overall_sr_alone_trend_verdict = "stable"
        overall_sr_alone_trend_significant = None

    # Significance-gated overall trend verdict — mirrors the per-category treatment
    # so the headline is as trustworthy as the per-category breakdown.
    # Only labeled "improving"/"declining" when both windows have >= _MIN_SR_FINDINGS
    # and the change passes the two-proportion z-test (_is_trend_significant).
    _overall_data_sufficient = (
        overall_hit_rate is not None
        and previous_overall_hit_rate is not None
        and current_total_humans >= _MIN_SR_FINDINGS
        and previous_total_humans >= _MIN_SR_FINDINGS
    )
    if _overall_data_sufficient:
        _overall_delta = overall_hit_rate - previous_overall_hit_rate
        _overall_sig = _is_trend_significant(
            overall_hit_rate,
            current_total_humans,
            previous_overall_hit_rate,
            previous_total_humans,
        )
        if _overall_sig and _overall_delta >= _TREND_DELTA_THRESHOLD:
            overall_trend_verdict: str = "improving"
        elif _overall_sig and _overall_delta <= -_TREND_DELTA_THRESHOLD:
            overall_trend_verdict = "declining"
        else:
            overall_trend_verdict = "stable"
        overall_trend_significant: Optional[bool] = (
            _overall_sig and abs(_overall_delta) >= _TREND_DELTA_THRESHOLD
        )
    else:
        overall_trend_verdict = "stable"
        overall_trend_significant = None

    # Prompt-version awareness: detect mixed-regime windows so the agent can
    # caveat calibration surfaces and avoid mistaking prompt rollouts for regressions.
    pv_counter = _collect_prompt_versions(current_window_tasks)
    pv_counter_prev = _collect_prompt_versions(previous_window_tasks)
    current_prompt_version: Optional[str] = (
        pv_counter.most_common(1)[0][0] if pv_counter else None
    )
    prev_prompt_version: Optional[str] = (
        pv_counter_prev.most_common(1)[0][0] if pv_counter_prev else None
    )

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "signal_last_computed_at": now.isoformat(),
        "last_sufficient_computed_at": now.isoformat(),
        "data_sufficient": True,
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "categories": categories,
        "by_task_type": by_task_type,
        "by_fix_iteration": by_fix_iteration,
        "by_scope": by_scope,
        "by_severity": by_severity,
        "sr_skip_rate": sr_skip_rate,
        "sr_skip_reason_distribution": sr_skip_dist if sr_skip_dist is not None else {},
        "overall_hit_rate": overall_hit_rate,
        "previous_overall_hit_rate": previous_overall_hit_rate,
        "overall_trend_verdict": overall_trend_verdict,
        "overall_trend_significant": overall_trend_significant,
        "overall_trend_data_sufficient": _overall_data_sufficient,
        "overall_sr_alone_hit_rate": overall_sr_alone_hit_rate,
        "previous_overall_sr_alone_hit_rate": previous_overall_sr_alone_hit_rate,
        "overall_sr_alone_trend_verdict": overall_sr_alone_trend_verdict,
        "overall_sr_alone_trend_significant": overall_sr_alone_trend_significant,
        "overall_sr_alone_trend_data_sufficient": _sr_alone_data_sufficient,
        "current_finding_count": current_finding_count,
        "previous_finding_count": previous_finding_count,
    }
    if current_prompt_version is not None:
        result["prompt_version"] = current_prompt_version
    if len(pv_counter) > 1:
        result["mixed_prompt_versions"] = True
    if (
        current_prompt_version is not None
        and prev_prompt_version is not None
        and current_prompt_version != prev_prompt_version
    ):
        result["prompt_version_changed"] = True
        for _scope_cats in by_scope.values():
            for _scope_entry in _scope_cats.values():
                _scope_entry["prompt_version_changed"] = True
    if sr_skip_rate is not None:
        result["sr_skip_rate"] = sr_skip_rate
        result["sr_skip_reason_distribution"] = sr_skip_dist

    _dir_text_map: dict[str, str] = {
        str(d.get("id") or d.get("direction_id") or ""): (
            d.get("text") or d.get("description") or ""
        )
        for d in (project.directions or [])
        if d.get("id") or d.get("direction_id")
    }
    by_direction = compute_sr_catch_rate_by_direction(
        all_tasks,
        existing_by_direction={}
        if mode_changed
        else (existing.get("by_direction") or {}),
        is_new_window=is_new_window,
        direction_text_map=_dir_text_map or None,
    )
    result["by_direction"] = by_direction or {}

    from app.sr_blind_spots import (
        aggregate_sr_co_failure_pairs,
        _count_reviewer_disagreement_directions,
    )  # noqa: PLC0415

    co_pairs = aggregate_sr_co_failure_pairs(current_window_tasks)
    result["co_failure_pairs"] = co_pairs or []

    # Attach per-category failure-mode breakdown (reading_gap / judgment_gap / coverage_gap).
    # Uses origin tags set by the fresh-reviewer pass in standard.ts:441-457.
    failure_mode_dir_counts = _count_reviewer_disagreement_directions(
        current_window_tasks
    )
    cat_failure_modes: dict[str, dict[str, int]] = {}
    for (cat, _ttype), bucket in failure_mode_dir_counts.items():
        if cat not in cat_failure_modes:
            cat_failure_modes[cat] = {
                "reading_gap": 0,
                "judgment_gap": 0,
                "coverage_gap": 0,
            }
        cat_failure_modes[cat]["reading_gap"] += bucket["miss_recovery_incomplete"]
        cat_failure_modes[cat]["judgment_gap"] += bucket["miss_disagreement"]
        cat_failure_modes[cat]["coverage_gap"] += bucket["miss_coverage_gap"]
    for cat, data in categories.items():
        modes = cat_failure_modes.get(cat)
        if modes and sum(modes.values()) > 0:
            dominant = max(modes.items(), key=lambda x: x[1])
            data["miss_mode_breakdown"] = {**modes, "dominant_mode": dominant[0]}

    # Reviewer hallucination rate: proportion of reviewer findings filtered as phantom file refs.
    # Uses reviewer_findings_filtered written by the fresh-reviewer pass in standard.ts.
    curr_hall_total = 0
    curr_hall_filtered = 0
    for _t in current_window_tasks:
        _sro = (getattr(_t, "context", None) or {}).get("_self_review_outcome") or {}
        _rff = _sro.get("reviewer_findings_filtered") or {}
        _t_total = _rff.get("total_reviewer_findings")
        _t_filtered = _rff.get("filtered_count")
        if isinstance(_t_total, int) and isinstance(_t_filtered, int):
            curr_hall_total += _t_total
            curr_hall_filtered += _t_filtered
    prev_hall_total = 0
    prev_hall_filtered = 0
    for _t in previous_window_tasks:
        _sro = (getattr(_t, "context", None) or {}).get("_self_review_outcome") or {}
        _rff = _sro.get("reviewer_findings_filtered") or {}
        _t_total = _rff.get("total_reviewer_findings")
        _t_filtered = _rff.get("filtered_count")
        if isinstance(_t_total, int) and isinstance(_t_filtered, int):
            prev_hall_total += _t_total
            prev_hall_filtered += _t_filtered
    curr_hall_rate: Optional[float] = (
        round(curr_hall_filtered / curr_hall_total, 3)
        if curr_hall_total >= _HALLUCINATION_RATE_MIN_FINDINGS
        else None
    )
    prev_hall_rate: Optional[float] = (
        round(prev_hall_filtered / prev_hall_total, 3)
        if prev_hall_total >= _HALLUCINATION_RATE_MIN_FINDINGS
        else None
    )
    if curr_hall_rate is not None or prev_hall_rate is not None:
        hall_delta: Optional[float] = (
            round(curr_hall_rate - prev_hall_rate, 3)
            if curr_hall_rate is not None and prev_hall_rate is not None
            else None
        )
        result["reviewer_hallucination_rate"] = {
            "current_rate": curr_hall_rate,
            "previous_rate": prev_hall_rate,
            "current_sample": curr_hall_total,
            "previous_sample": prev_hall_total,
            "delta": hall_delta,
        }

    doc["sr_catch_rate_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_CATCH_RATE_TRENDS] project=%s categories=%d",
        project_id,
        len(categories),
    )

    # Fire notification when categories or severity buckets are regressing.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        regressing = {
            cat: data
            for cat, data in categories.items()
            if data.get("trend") == "decreasing"
        }
        regressing_severities = {
            sev: data
            for sev, data in by_severity.items()
            if data.get("trend") == "regressing"
        }
        fp_spiking = {
            cat: data
            for cat, data in categories.items()
            if data.get("fp_trend") == "increasing"
        }
        alertable = {**regressing, **fp_spiking}
        if alertable or regressing_severities:
            # Dedup: in cycle mode key off cycle_id; in calendar mode use 14-day window.
            # Dedup key includes both regressing categories and regressing severities.
            alert_state: dict = dict(doc.get("sr_catch_rate_trends_alert") or {})
            alert_computed_at: Optional[str] = alert_state.get("computed_at")
            alerted_cats: list = list(alert_state.get("categories") or [])
            alerted_sevs: list = list(alert_state.get("severities") or [])
            current_regressing_cats = sorted(alertable.keys())
            current_regressing_sevs = sorted(regressing_severities.keys())

            already_alerted = False
            if use_cycle_mode:
                already_alerted = (
                    alert_state.get("cycle_id") == current_cycle_id
                    and alerted_cats == current_regressing_cats
                    and alerted_sevs == current_regressing_sevs
                )
            elif alert_computed_at:
                try:
                    last_alert_dt = datetime.fromisoformat(alert_computed_at)
                    if (
                        (now - last_alert_dt).days < 14
                        and alerted_cats == current_regressing_cats
                        and alerted_sevs == current_regressing_sevs
                    ):
                        already_alerted = True
                except (ValueError, TypeError):
                    pass

            if not already_alerted:
                lines = ["SR catch-rate regression detected:"]
                for cat in current_regressing_cats:
                    data = alertable[cat]
                    if cat in regressing:
                        prev = data.get("previous_hit_rate")
                        curr_rate = data.get("current_hit_rate")
                        sample = current_stats.get(cat, {}).get("human_count", 0)
                        prev_str = f"{prev:.0%}" if prev is not None else "N/A"
                        curr_str = (
                            f"{curr_rate:.0%}" if curr_rate is not None else "N/A"
                        )
                        lines.append(
                            f"  • {cat}: hit-rate {prev_str} → {curr_str} (n={sample})"
                        )
                    else:
                        prev_fp = data.get("previous_fp_rate")
                        curr_fp = data.get("false_positive_rate")
                        sample = current_stats.get(cat, {}).get("sr_failed_count", 0)
                        prev_str = f"{prev_fp:.0%}" if prev_fp is not None else "N/A"
                        curr_str = f"{curr_fp:.0%}" if curr_fp is not None else "N/A"
                        lines.append(
                            f"  • {cat}: FP-rate {prev_str} → {curr_str} (n={sample})"
                        )
                if current_regressing_sevs:
                    lines.append("Severity regressions:")
                    for sev in current_regressing_sevs:
                        data_sev = regressing_severities[sev]
                        prev_miss = data_sev.get("previous_miss_rate")
                        curr_miss = data_sev.get("current_miss_rate")
                        sample_n = data_sev.get("current_sample", 0)
                        prev_str = (
                            f"{prev_miss:.0%}" if prev_miss is not None else "N/A"
                        )
                        curr_str = (
                            f"{curr_miss:.0%}" if curr_miss is not None else "N/A"
                        )
                        lines.append(
                            f"  • {sev}: {prev_str} → {curr_str} (n={sample_n})"
                        )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                # Surface the regression in AlertsBanner as a real Alert row.
                # Dedup mirror: any prior undismissed regression Alert rows
                # for this project are dismissed unless their title matches
                # what we are about to insert (defense-in-depth — same gate
                # as the design_doc dedup state above).
                title_parts = list(current_regressing_cats) + [
                    f"{s} severity" for s in current_regressing_sevs
                ]
                new_alert_title = _format_sr_alert_title(
                    _SR_REGRESSION_ALERT_TITLE_PREFIX, title_parts
                )
                existing_reg_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(
                                    f"{_SR_REGRESSION_ALERT_TITLE_PREFIX}%"
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_alert_title for a in existing_reg_alerts):
                    for a in existing_reg_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_alert_title,
                            message=description,
                        )
                    )

                webhook_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "regressing_categories": [
                        {
                            "category": cat,
                            "previous_hit_rate": alertable[cat].get(
                                "previous_hit_rate"
                            ),
                            "current_hit_rate": alertable[cat].get("current_hit_rate"),
                            "false_positive_rate": alertable[cat].get(
                                "false_positive_rate"
                            ),
                            "fp_trend": alertable[cat].get("fp_trend"),
                            "sample_size": current_stats.get(cat, {}).get(
                                "human_count", 0
                            ),
                        }
                        for cat in current_regressing_cats
                    ],
                    "regressing_severities": [
                        {
                            "severity": sev,
                            "previous_miss_rate": regressing_severities[sev].get(
                                "previous_miss_rate"
                            ),
                            "current_miss_rate": regressing_severities[sev].get(
                                "current_miss_rate"
                            ),
                            "sample_size": regressing_severities[sev].get(
                                "current_sample", 0
                            ),
                        }
                        for sev in current_regressing_sevs
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    webhook_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_catch_rate_regression",
                        payload=webhook_payload,
                    )
                )

                doc["sr_catch_rate_trends_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_regressing_cats,
                    "severities": current_regressing_sevs,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s regression_alert categories=%s severities=%s",
                    project_id,
                    current_regressing_cats,
                    current_regressing_sevs,
                )
        elif doc.get("sr_catch_rate_trends_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            # Dismiss any matching undismissed Alert rows + clear design_doc state.
            stale_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_REGRESSION_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_catch_rate_trends_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception("[SR_CATCH_RATE_TRENDS] notification failed, suppressing")

    # Baseline-floor alert: fire when a category stays below _SR_FLOOR_THRESHOLD
    # for at least _FLOOR_MIN_CONSECUTIVE_WINDOWS consecutive measurement windows.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        floor_threshold: float = float(
            (doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
        )
        floor_min_windows: int = int(
            (doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS)
        )

        below_floor = {
            cat: data
            for cat, data in categories.items()
            if (
                data.get("current_hit_rate") is not None
                and len(data.get("rate_history") or []) >= floor_min_windows
                and all(
                    r < floor_threshold
                    for r in (data.get("rate_history") or [])[-floor_min_windows:]
                )
            )
        }

        if below_floor:
            floor_alert_state: dict = dict(doc.get("sr_catch_rate_floor_alert") or {})
            floor_alerted_cats: list = list(floor_alert_state.get("categories") or [])
            current_floor_cats = sorted(below_floor.keys())

            floor_already_alerted = False
            if use_cycle_mode:
                floor_already_alerted = (
                    floor_alert_state.get("cycle_id") == current_cycle_id
                    and floor_alerted_cats == current_floor_cats
                )
            else:
                floor_alert_at: Optional[str] = floor_alert_state.get("computed_at")
                if floor_alert_at:
                    try:
                        last_floor_dt = datetime.fromisoformat(floor_alert_at)
                        if (
                            now - last_floor_dt
                        ).days < 14 and floor_alerted_cats == current_floor_cats:
                            floor_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not floor_already_alerted:
                lines = [
                    f"SR catch-rate below floor ({floor_threshold:.0%}) for {floor_min_windows}+ windows:"
                ]
                for cat in current_floor_cats:
                    data = below_floor[cat]
                    curr_rate = data.get("current_hit_rate")
                    sample = current_stats.get(cat, {}).get("human_count", 0)
                    curr_str = f"{curr_rate:.0%}" if curr_rate is not None else "N/A"
                    history = data.get("rate_history") or []
                    history_str = ", ".join(
                        f"{r:.0%}" for r in history[-floor_min_windows:]
                    )
                    lines.append(
                        f"  • {cat}: {curr_str} (last {floor_min_windows} windows: {history_str}, n={sample})"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                # Surface as ERROR Alert row in AlertsBanner (stricter than the
                # WARNING regression path — staying below floor for N consecutive
                # windows is a higher-severity signal). Defense-in-depth dedup
                # mirrors the regression path.
                new_floor_title = _format_sr_alert_title(
                    _SR_FLOOR_ALERT_TITLE_PREFIX, current_floor_cats
                )
                existing_floor_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.ERROR,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(f"{_SR_FLOOR_ALERT_TITLE_PREFIX}%"),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_floor_title for a in existing_floor_alerts):
                    for a in existing_floor_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.ERROR,
                            title=new_floor_title,
                            message=description,
                        )
                    )

                floor_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "floor_threshold": floor_threshold,
                    "below_floor_categories": [
                        {
                            "category": cat,
                            "current_hit_rate": below_floor[cat].get(
                                "current_hit_rate"
                            ),
                            "rate_history": below_floor[cat].get("rate_history"),
                            "sample_size": current_stats.get(cat, {}).get(
                                "human_count", 0
                            ),
                        }
                        for cat in current_floor_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    floor_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_catch_rate_below_floor",
                        payload=floor_payload,
                    )
                )

                doc["sr_catch_rate_floor_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_floor_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s floor_alert categories=%s",
                    project_id,
                    current_floor_cats,
                )
        elif doc.get("sr_catch_rate_floor_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            stale_floor_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.ERROR,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_FLOOR_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_floor_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_catch_rate_floor_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] floor notification failed, suppressing"
        )

    # Quadrant alert: fire when a category is classified as conservative_shift or
    # noisy_shift.  both_declining is intentionally excluded (covered by regression
    # alert); healthy/mixed/flat are silent.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        quadrant_alertable = {
            cat: data
            for cat, data in categories.items()
            if data.get("precision_recall_quadrant")
            in ("conservative_shift", "noisy_shift")
        }

        if quadrant_alertable:
            quadrant_alert_state: dict = dict(doc.get("sr_quadrant_alert") or {})
            q_alerted_cats: list = list(quadrant_alert_state.get("categories") or [])
            current_quadrant_cats = sorted(quadrant_alertable.keys())

            q_already_alerted = False
            if use_cycle_mode:
                q_already_alerted = (
                    quadrant_alert_state.get("cycle_id") == current_cycle_id
                    and q_alerted_cats == current_quadrant_cats
                )
            else:
                q_alert_at: Optional[str] = quadrant_alert_state.get("computed_at")
                if q_alert_at:
                    try:
                        last_q_dt = datetime.fromisoformat(q_alert_at)
                        if (
                            now - last_q_dt
                        ).days < 14 and q_alerted_cats == current_quadrant_cats:
                            q_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not q_already_alerted:
                lines = ["SR precision/recall shift detected:"]
                for cat in current_quadrant_cats:
                    data = quadrant_alertable[cat]
                    quadrant = data.get("precision_recall_quadrant")
                    pdelta = data.get("precision_delta")
                    rdelta = data.get("recall_delta")
                    p_str = f"{pdelta:+.0%}" if pdelta is not None else "N/A"
                    r_str = f"{rdelta:+.0%}" if rdelta is not None else "N/A"
                    lines.append(
                        f"  • {cat} [{quadrant}]: precision {p_str} / recall {r_str}"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                new_quadrant_title = _format_sr_alert_title(
                    _SR_QUADRANT_ALERT_TITLE_PREFIX, current_quadrant_cats
                )
                existing_q_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_quadrant_title for a in existing_q_alerts):
                    for a in existing_q_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_quadrant_title,
                            message=description,
                        )
                    )

                q_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "quadrant_categories": [
                        {
                            "category": cat,
                            "quadrant": quadrant_alertable[cat].get(
                                "precision_recall_quadrant"
                            ),
                            "precision_delta": quadrant_alertable[cat].get(
                                "precision_delta"
                            ),
                            "recall_delta": quadrant_alertable[cat].get("recall_delta"),
                        }
                        for cat in current_quadrant_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    q_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_precision_recall_shift",
                        payload=q_payload,
                    )
                )

                doc["sr_quadrant_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_quadrant_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s quadrant_alert categories=%s",
                    project_id,
                    current_quadrant_cats,
                )
        elif doc.get("sr_quadrant_alert"):
            # Clear transition: no longer alertable this cycle.
            stale_q_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_q_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_quadrant_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] quadrant notification failed, suppressing"
        )

    # PASS reasoning thinning alert: fire when a non-regressing category has
    # engagement below threshold with enough PASS samples.  Suppressed for
    # categories already covered by a regression or floor alert this cycle.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        eng_threshold: float = float(
            (doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD)
        )
        eng_min_passes: int = int(
            (doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES)
        )

        thinning_cats = {
            cat: data
            for cat, data in categories.items()
            if (
                cat not in regressing_cats
                and data.get("reasoning_engagement_rate") is not None
                and data["reasoning_engagement_rate"] < eng_threshold
                and (data.get("reasoning_engagement_total") or 0) >= eng_min_passes
            )
        }

        if thinning_cats:
            eng_alert_state: dict = dict(doc.get("sr_pass_engagement_alert") or {})
            eng_alerted_cats: list = list(eng_alert_state.get("categories") or [])
            current_thinning_cats = sorted(thinning_cats.keys())

            eng_already_alerted = False
            if use_cycle_mode:
                eng_already_alerted = (
                    eng_alert_state.get("cycle_id") == current_cycle_id
                    and eng_alerted_cats == current_thinning_cats
                )
            else:
                eng_alert_at: Optional[str] = eng_alert_state.get("computed_at")
                if eng_alert_at:
                    try:
                        last_eng_dt = datetime.fromisoformat(eng_alert_at)
                        if (
                            now - last_eng_dt
                        ).days < 14 and eng_alerted_cats == current_thinning_cats:
                            eng_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not eng_already_alerted:
                lines = [
                    f"PASS reasoning thinning (engagement < {eng_threshold:.0%}, may regress soon):"
                ]
                for cat in current_thinning_cats:
                    data = thinning_cats[cat]
                    rate = data.get("reasoning_engagement_rate")
                    total = data.get("reasoning_engagement_total", 0)
                    with_r = data.get("reasoning_engagement_with", 0)
                    rate_str = f"{rate:.0%}" if rate is not None else "N/A"
                    lines.append(
                        f"  • {cat}: {with_r}/{total} PASSes with reasoning ({rate_str})"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                new_eng_title = _format_sr_alert_title(
                    _PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX, current_thinning_cats
                )
                existing_eng_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(
                                    f"{_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX}%"
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_eng_title for a in existing_eng_alerts):
                    for a in existing_eng_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_eng_title,
                            message=description,
                        )
                    )

                thinning_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "eng_threshold": eng_threshold,
                    "thinning_categories": [
                        {
                            "category": cat,
                            "engagement_rate": thinning_cats[cat].get(
                                "reasoning_engagement_rate"
                            ),
                            "total_passes": thinning_cats[cat].get(
                                "reasoning_engagement_total", 0
                            ),
                        }
                        for cat in current_thinning_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    thinning_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_pass_reasoning_thinning",
                        payload=thinning_payload,
                    )
                )

                doc["sr_pass_engagement_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_thinning_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s engagement_thinning_alert categories=%s",
                    project_id,
                    current_thinning_cats,
                )
        elif doc.get("sr_pass_engagement_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            stale_eng_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(
                                f"{_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX}%"
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_eng_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_pass_engagement_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] engagement thinning notification failed, suppressing"
        )

    await session.flush()

    return result


async def check_fix_loop_divergence_regression(
    project_id: uuid.UUID,
    session: AsyncSession,
) -> Optional[dict[str, Any]]:
    """Detect fix-loop divergence rate regression across two consecutive 7-day windows.

    Mirrors _MIN_SR_SAMPLE / _TREND_DELTA_THRESHOLD from compute_sr_catch_rate_trends.
    Idempotent per ISO year-week via project.design_doc["fix_loop_divergence_alerted_window"]
    so a persistent regression alerts at most once per week.
    """
    from app.notification_dispatch import (  # noqa: PLC0415
        post_fix_loop_divergence_regression_message,
    )

    now = _utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    window_key = f"{iso_year}-W{iso_week:02d}"

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None
    doc = dict(project.design_doc or {})
    if doc.get("fix_loop_divergence_alerted_window") == window_key:
        doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        await session.commit()
        return None

    tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.created_at >= now - timedelta(days=14),
                )
                .options(load_only(Task.id, Task.fix_loop_history, Task.created_at))
            )
        )
        .scalars()
        .all()
    )

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    c7 = _naive(now - timedelta(days=7))
    c14 = _naive(now - timedelta(days=14))
    loops = ("lint", "test", "check")

    def _divergence_stats(start: datetime, end: datetime) -> dict[str, dict]:
        s: dict[str, dict] = {loop: {"n": 0, "d": 0} for loop in loops}
        for t in tasks:
            ts = _naive(t.created_at) if t.created_at else None
            if ts is None or not (start <= ts < end):
                continue
            for loop in loops:
                bucket = (t.fix_loop_history or {}).get(loop) or []
                if not bucket:
                    continue
                last_exit: Optional[str] = None
                for ev in reversed(bucket):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None:
                    last_exit = (
                        bucket[-1].get("exit_reason")
                        if isinstance(bucket[-1], dict)
                        else None
                    )
                s[loop]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    s[loop]["d"] += 1
        return s

    curr = _divergence_stats(c7, datetime(9999, 12, 31))
    prev = _divergence_stats(c14, c7)

    regressions: list[dict] = []
    for loop in loops:
        cn, cd = curr[loop]["n"], curr[loop]["d"]
        pn, pd = prev[loop]["n"], prev[loop]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            continue
        if cd / cn - pd / pn >= _TREND_DELTA_THRESHOLD:
            regressions.append(
                {
                    "loop": loop,
                    "prior_rate": round(pd / pn, 3),
                    "current_rate": round(cd / cn, 3),
                    "prior_count": pn,
                    "current_count": cn,
                }
            )

    if not regressions:
        return None

    await post_fix_loop_divergence_regression_message(session, project_id, regressions)
    doc["fix_loop_divergence_alerted_window"] = window_key
    doc["fix_loop_divergence_regressions"] = regressions
    doc["fix_loop_divergence_detected_at"] = now.isoformat()
    doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()
    logger.info(
        "[FIX_LOOP_DIVERGENCE] project=%s loops=%s",
        project_id,
        [r["loop"] for r in regressions],
    )
    return {"regressions": regressions}


# ---------------------------------------------------------------------------
# Fresh-reviewer cycle-over-cycle trend
# ---------------------------------------------------------------------------

_FR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}


async def compute_fresh_reviewer_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for fresh-reviewer effectiveness.

    Mirrors the cycle-vs-calendar bucketing from ``compute_sr_catch_rate_trends``.
    Requires at least ``_MIN_SR_SAMPLE`` tasks *with* reviewer_findings in both
    the current and previous bucket before computing rates.

    Output keys per category: ``per_category_hit_rate``, ``added_value``,
    ``approval_rate_lift`` — each with ``current``, ``previous``, ``delta``,
    and ``trend`` (stable / improving / regressing).

    **Side effect**: writes result to ``project.design_doc["fresh_reviewer_trend"]``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    _code_tasks = [
        t
        for t in all_tasks
        if (t.first_payload or {}).get("type") in _FR_CODE_CHANGE_TYPES
    ]

    def _bucket_stats(tasks: list) -> dict[str, Any]:
        """Compute fresh-reviewer stats for a pre-filtered task list."""
        with_reviewer = [
            t
            for t in tasks
            if (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]
        without_reviewer = [
            t
            for t in tasks
            if not (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]

        def _fa_rate(ts: list) -> Optional[float]:
            measured = [
                t for t in ts if "_first_attempt_succeeded" in (t.context or {})
            ]
            if not measured:
                return None
            return round(
                sum(1 for t in measured if t.context["_first_attempt_succeeded"])
                / len(measured),
                3,
            )

        cat_flagged: dict[str, int] = {}
        cat_confirmed: dict[str, int] = {}
        cat_added_value: dict[str, int] = {}
        for t in with_reviewer:
            ctx = t.context or {}
            findings = (
                ctx.get("_self_review_outcome", {}).get("reviewer_findings") or []
            )
            human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
            check_results = (
                ctx.get("_self_review_outcome", {}).get("check_results") or {}
            )
            # Author SR failures only — a reviewer-stamped override means the
            # author SR did NOT catch the category, so it must still count
            # toward `cat_added_value` when the human confirms the rescue.
            agent_failed = {
                c
                for c, r in check_results.items()
                if isinstance(r, dict)
                and not r.get("passed", True)
                and r.get("origin") not in _REVIEWER_ORIGINS
            }
            for finding in findings:
                cat = finding.get("category") if isinstance(finding, dict) else None
                if not cat:
                    continue
                cat_flagged[cat] = cat_flagged.get(cat, 0) + 1
                if cat in human_cats:
                    cat_confirmed[cat] = cat_confirmed.get(cat, 0) + 1
                    if cat not in agent_failed:
                        cat_added_value[cat] = cat_added_value.get(cat, 0) + 1

        return {
            "sample_size": len(with_reviewer),
            "cat_flagged": cat_flagged,
            "cat_confirmed": cat_confirmed,
            "cat_added_value": cat_added_value,
            "fa_rate_with": _fa_rate(with_reviewer),
            "fa_rate_without": _fa_rate(without_reviewer),
        }

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_stats: dict[str, Any] = {}
    previous_stats: dict[str, Any] = {}

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in _code_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_stats = _bucket_stats(task_by_cycle.get(c_cycle.id, []))
            p_stats = _bucket_stats(task_by_cycle.get(p_cycle.id, []))
            if (
                c_stats["sample_size"] >= _MIN_SR_SAMPLE
                and p_stats["sample_size"] >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_stats = c_stats
                previous_stats = p_stats
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        cal_current = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        cal_previous = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]
        current_stats = _bucket_stats(cal_current)
        previous_stats = _bucket_stats(cal_previous)

    if (
        current_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
        or previous_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
    ):
        return None

    def _classify(curr: Optional[float], prev: Optional[float]) -> str:
        if curr is None or prev is None:
            return "stable"
        delta = curr - prev
        if delta >= _TREND_DELTA_THRESHOLD:
            return "improving"
        if delta <= -_TREND_DELTA_THRESHOLD:
            return "regressing"
        return "stable"

    # Per-category hit rate
    c_flagged = current_stats["cat_flagged"]
    p_flagged = previous_stats["cat_flagged"]
    all_cats = set(c_flagged.keys()) | set(p_flagged.keys())

    per_category_hit_rate: dict[str, Any] = {}
    for cat in all_cats:
        c_total = c_flagged.get(cat, 0)
        p_total = p_flagged.get(cat, 0)
        c_rate = (
            round(current_stats["cat_confirmed"].get(cat, 0) / c_total, 3)
            if c_total >= 2
            else None
        )
        p_rate = (
            round(previous_stats["cat_confirmed"].get(cat, 0) / p_total, 3)
            if p_total >= 2
            else None
        )
        per_category_hit_rate[cat] = {
            "current": c_rate,
            "previous": p_rate,
            "delta": round(c_rate - p_rate, 3)
            if c_rate is not None and p_rate is not None
            else None,
            "trend": _classify(c_rate, p_rate),
        }

    # Added value (fresh-reviewer catches what agent self-review missed)
    c_av = current_stats["cat_added_value"]
    p_av = previous_stats["cat_added_value"]
    av_cats = set(c_av.keys()) | set(p_av.keys())
    added_value: dict[str, Any] = {}
    for cat in av_cats:
        c_cnt = c_av.get(cat, 0)
        p_cnt = p_av.get(cat, 0)
        added_value[cat] = {
            "current": c_cnt,
            "previous": p_cnt,
            "trend": _classify(float(c_cnt), float(p_cnt)),
        }

    # Approval rate lift
    c_with = current_stats["fa_rate_with"]
    c_without = current_stats["fa_rate_without"]
    p_with = previous_stats["fa_rate_with"]
    p_without = previous_stats["fa_rate_without"]
    c_lift = (
        round(c_with - c_without, 3)
        if c_with is not None and c_without is not None
        else None
    )
    p_lift = (
        round(p_with - p_without, 3)
        if p_with is not None and p_without is not None
        else None
    )
    approval_rate_lift: dict[str, Any] = {
        "current": c_lift,
        "previous": p_lift,
        "delta": round(c_lift - p_lift, 3)
        if c_lift is not None and p_lift is not None
        else None,
        "trend": _classify(c_lift, p_lift),
    }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current_sample_size": current_stats["sample_size"],
        "previous_sample_size": previous_stats["sample_size"],
        "per_category_hit_rate": per_category_hit_rate,
        "added_value": added_value,
        "approval_rate_lift": approval_rate_lift,
    }

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["fresh_reviewer_trend"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[FRESH_REVIEWER_TREND] project=%s categories=%d bucket_mode=%s",
        project_id,
        len(per_category_hit_rate),
        result["bucket_mode"],
    )

    return result


async def compute_sr_reviewer_disagreements(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
    *,
    window_days: int = 30,
    limit: int = 20,
) -> Optional[dict[str, Any]]:
    """Aggregate categories where SR passed but the fresh reviewer flagged an issue.

    Scans ``all_tasks`` (already fetched by the caller) for completed
    code-change tasks that have both ``check_results`` and
    ``reviewer_findings`` in their ``_self_review_outcome``. A disagreement
    is counted for every (category, task_type) pair where the fresh reviewer
    raised a finding but self-review either omitted the category or marked it
    ``passed: true``.

    **Side effect**: writes the result to
    ``project.design_doc["sr_reviewer_disagreements"]`` so downstream readers
    (admin digest, agent project-context) can access pre-computed data without
    re-scanning tasks.

    Returns the result dict, or ``None`` when no disagreements are found.
    """
    from app.sr_blind_spots import _count_reviewer_disagreement_directions  # noqa: PLC0415

    now = _utcnow()
    cutoff = now - timedelta(days=window_days)

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    cutoff_naive = _naive(cutoff)

    eligible = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and (not t.created_at or _naive(t.created_at) >= cutoff_naive)
    ]
    direction_counts = _count_reviewer_disagreement_directions(eligible)

    if not any(bucket["miss"] > 0 for bucket in direction_counts.values()):
        return None

    top = sorted(
        [(k, v) for k, v in direction_counts.items() if v["miss"] > 0],
        key=lambda x: -x[1]["miss"],
    )[:limit]
    entries = [
        {
            "category": cat,
            "task_type": ttype,
            "count": bucket["miss"],
            "examples": bucket["examples"],
        }
        for (cat, ttype), bucket in top
    ]
    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "window_days": window_days,
        "disagreements": entries,
    }

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if project:
        doc = dict(project.design_doc or {})
        doc["sr_reviewer_disagreements"] = result
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

    logger.info(
        "[SR_REVIEWER_DISAGREEMENTS] project=%s entries=%d",
        project_id,
        len(entries),
    )
    return result


# ---------------------------------------------------------------------------
# Edges-considered cycle-over-cycle trend
# ---------------------------------------------------------------------------


async def compute_edges_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for edges_considered metrics.

    Mirrors the cycle-vs-calendar bucketing from ``compute_fresh_reviewer_trend``.
    Requires at least ``_MIN_SR_SAMPLE`` code-change tasks with
    ``_self_review_outcome`` in both the current and previous bucket.

    Primary trend signal: ``tasks_with_edges_rate`` — the fraction of code tasks
    that included an ``edges_considered`` list in their self-review.  Trend is
    classified as ``'improving'`` | ``'regressing'`` | ``'stable'`` relative to the
    previous bucket using ``_TREND_DELTA_THRESHOLD``.

    **Side effect**: writes result to ``project.design_doc['edges_trend']`` and
    maintains a top-level ``rate_history`` list (capped at 5 entries).  Append
    rule mirrors ``sr_catch_rate_trends``: in cycle mode append on new
    ``current_cycle_id``; in calendar mode append when ≥14 days since last
    ``computed_at``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifying_count(tasks: list) -> int:
        return sum(
            1
            for t in tasks
            if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
            and "_self_review_outcome" in (t.context or {})
        )

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if (
        _qualifying_count(current_bucket) < _MIN_SR_SAMPLE
        or _qualifying_count(previous_bucket) < _MIN_SR_SAMPLE
    ):
        return None

    current_stats = _compute_edges_stats(current_bucket)
    previous_stats = _compute_edges_stats(previous_bucket)

    if current_stats is None or previous_stats is None:
        return None

    c_rate = current_stats["tasks_with_edges_rate"]
    p_rate = previous_stats["tasks_with_edges_rate"]
    delta = round(c_rate - p_rate, 3)
    if delta >= _TREND_DELTA_THRESHOLD:
        trend = "improving"
    elif delta <= -_TREND_DELTA_THRESHOLD:
        trend = "regressing"
    else:
        trend = "stable"

    rc_current = current_stats.get("reviewer_caught_rate")
    rc_previous = previous_stats.get("reviewer_caught_rate")
    if rc_current is not None and rc_previous is not None:
        rc_delta = round(rc_current - rc_previous, 3)
        if rc_delta >= _TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "improving"
        elif rc_delta <= -_TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "regressing"
        else:
            reviewer_caught_trend = "stable"
    else:
        rc_delta = None
        reviewer_caught_trend = None

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": current_stats,
        "previous": previous_stats,
        "tasks_with_edges_rate_delta": delta,
        "trend": trend,
        "reviewer_caught_rate_delta": rc_delta,
        "reviewer_caught_trend": reviewer_caught_trend,
    }

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing: dict = dict(doc.get("edges_trend") or {})
    existing_computed_at: Optional[str] = existing.get("computed_at")

    is_new_window = True
    if use_cycle_mode:
        is_new_window = existing.get("current_cycle_id") != current_cycle_id
    elif existing_computed_at:
        try:
            last_dt = datetime.fromisoformat(existing_computed_at)
            is_new_window = (now - last_dt).days >= 14
        except (ValueError, TypeError):
            pass

    rate_history: list = list(existing.get("rate_history") or [])
    if is_new_window or not rate_history:
        rate_history.append(c_rate)
        rate_history = rate_history[-5:]
    result["rate_history"] = rate_history

    doc["edges_trend"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[EDGES_TREND] project=%s trend=%s bucket_mode=%s",
        project_id,
        trend,
        result["bucket_mode"],
    )

    return result


# ---------------------------------------------------------------------------
# SR regression_risk vs testFix-loop signal cross-validation
# ---------------------------------------------------------------------------


async def compute_sr_regression_risk_test_signal_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-validate SR ``regression_risk.passed`` verdicts against testFix-loop signal.

    Companion metric to ``compute_sr_catch_rate_trends`` — that one counts a
    miss only when a human reviewer raised the ``regression_risk`` category.
    This one uses the agent's own testFix loop as an objective signal: when the
    test loop ran non-trivially, real regression surface existed regardless of
    whether any human reviewer flagged it.

    Per-task classification (qualifying = code-change task with a non-skip
    self-review outcome that includes ``check_results.regression_risk.passed``)::

      sr_passed_no_signal   (true-pass)             — SR ok, test loop quiet
      sr_passed_with_signal (miss-by-test-signal)   — SR ok, test loop ran
      sr_failed_with_signal (hit-by-test-signal)    — SR flagged, test loop ran
      sr_failed_no_signal   (false-flag)            — SR flagged, test loop quiet

    Test-signal predicate: ``len(history.get("test") or []) >= 2`` OR the last
    test attempt's ``exit_reason`` is non-``passed`` (covers single-iteration
    runs that diverged or maxed out attempts).

    Mirrors :func:`compute_sr_catch_rate_trends` cycle-vs-calendar bucketing
    and the :data:`_MIN_SR_SAMPLE` floor.

    **Side effect**: writes the result to
    ``project.design_doc["sr_regression_risk_test_signal_trends"]``.
    """
    from app.sr_blind_spots import is_skipped_sr_outcome

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rr = (sro.get("check_results") or {}).get("regression_risk")
        return isinstance(rr, dict) and "passed" in rr

    def _has_test_signal(task) -> bool:
        history = getattr(task, "fix_loop_history", None) or {}
        test_attempts = history.get("test") or []
        if len(test_attempts) >= 2:
            return True
        if test_attempts:
            last = test_attempts[-1]
            if isinstance(last, dict):
                reason = last.get("exit_reason")
                if reason and reason != "passed":
                    return True
        return False

    def _aggregate(tasks: list) -> dict[str, int]:
        stats = {
            "sr_passed_no_signal": 0,
            "sr_passed_with_signal": 0,
            "sr_failed_with_signal": 0,
            "sr_failed_no_signal": 0,
        }
        for t in tasks:
            if not _qualifies(t):
                continue
            sro = (t.context or {}).get("_self_review_outcome") or {}
            rr = (sro.get("check_results") or {}).get("regression_risk") or {}
            sr_passed = bool(rr.get("passed"))
            signal = _has_test_signal(t)
            if sr_passed and signal:
                stats["sr_passed_with_signal"] += 1
            elif sr_passed:
                stats["sr_passed_no_signal"] += 1
            elif signal:
                stats["sr_failed_with_signal"] += 1
            else:
                stats["sr_failed_no_signal"] += 1
        return stats

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    current_qual = _qualifying_count(current_bucket)
    previous_qual = _qualifying_count(previous_bucket)
    if current_qual < _MIN_SR_SAMPLE or previous_qual < _MIN_SR_SAMPLE:
        return None

    current_counts = _aggregate(current_bucket)
    previous_counts = _aggregate(previous_bucket)

    def _miss_rate(stats: dict[str, int]) -> tuple[Optional[float], int, int]:
        numerator = stats["sr_passed_with_signal"]
        denominator = stats["sr_passed_no_signal"] + stats["sr_passed_with_signal"]
        if denominator == 0:
            return None, numerator, denominator
        return round(numerator / denominator, 3), numerator, denominator

    current_rate, current_numer, current_denom = _miss_rate(current_counts)
    previous_rate, previous_numer, previous_denom = _miss_rate(previous_counts)

    if current_rate is not None and previous_rate is not None:
        delta = round(current_rate - previous_rate, 3)
        if delta >= _TREND_DELTA_THRESHOLD:
            trend = "regressing"
        elif delta <= -_TREND_DELTA_THRESHOLD:
            trend = "improving"
        else:
            trend = "stable"
    else:
        delta = None
        trend = "stable"

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": {
            **current_counts,
            "miss_by_test_signal_rate": current_rate,
            "numerator": current_numer,
            "denominator": current_denom,
            "sample_size": current_qual,
        },
        "previous": {
            **previous_counts,
            "miss_by_test_signal_rate": previous_rate,
            "numerator": previous_numer,
            "denominator": previous_denom,
            "sample_size": previous_qual,
        },
        "miss_by_test_signal_rate_delta": delta,
        "trend": trend,
    }

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["sr_regression_risk_test_signal_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_REGRESSION_RISK_TEST_SIGNAL] project=%s trend=%s miss_rate=%s n=%d",
        project_id,
        trend,
        current_rate,
        current_qual,
    )

    return result


# ---------------------------------------------------------------------------
# SR false-attestation trends
# ---------------------------------------------------------------------------

_FALSE_ATTESTATION_RATE_THRESHOLD = 0.10
_FALSE_ATTESTATION_ALERT_TITLE_PREFIX = "SR false attestation rate elevated"


async def compute_sr_false_attestation_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-check SR execution-evidence pass claims against CI outcomes cycle-over-cycle.

    For each code-change task whose SR marks tests_executed_cleanly, lint_executed_cleanly,
    or typecheck_executed_cleanly as passed=True, checks context["review_loop"]["ci_passed"].
    Counts tasks where SR claimed passed but CI recorded ci_passed=False as false attestations.

    Mirrors :func:`compute_sr_regression_risk_test_signal_trends` cycle-vs-calendar bucketing.

    **Side effect**: writes the result to
    ``project.design_doc["sr_false_attestation_trends"]`` and emits a WARNING Alert when
    any category's false attestation rate exceeds _FALSE_ATTESTATION_RATE_THRESHOLD.
    """
    from app.sr_blind_spots import (
        aggregate_sr_false_attestation_rates,
        is_skipped_sr_outcome,
    )

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rl = ctx.get("review_loop")
        return rl is not None and "ci_passed" in rl

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if _qualifying_count(current_bucket) < _MIN_SR_SAMPLE:
        return None

    current_rates = aggregate_sr_false_attestation_rates(current_bucket, min_sample=1)
    previous_rates = aggregate_sr_false_attestation_rates(previous_bucket, min_sample=1)

    if not current_rates:
        return None

    per_category: dict[str, Any] = {}
    for cat, curr in current_rates.items():
        prev = (previous_rates or {}).get(cat) or {}
        curr_rate = curr.get("false_attestation_rate")
        prev_rate = prev.get("false_attestation_rate")
        if curr_rate is not None and prev_rate is not None:
            delta = round(curr_rate - prev_rate, 3)
            trend = (
                "regressing"
                if delta >= _TREND_DELTA_THRESHOLD
                else "improving"
                if delta <= -_TREND_DELTA_THRESHOLD
                else "stable"
            )
        else:
            delta = None
            trend = "stable"
        per_category[cat] = {
            **curr,
            "previous_false_attestation_rate": prev_rate,
            "delta": delta,
            "trend": trend,
        }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "per_category": per_category,
    }

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["sr_false_attestation_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_FALSE_ATTESTATION] project=%s bucket=%s cats=%s",
        project_id,
        "cycle" if use_cycle_mode else "calendar",
        list(per_category.keys()),
    )

    elevated = {
        cat: data
        for cat, data in per_category.items()
        if (data.get("false_attestation_rate") or 0) > _FALSE_ATTESTATION_RATE_THRESHOLD
    }
    if elevated:
        try:
            from app.notification_dispatch import post_failure_alert_message  # noqa: PLC0415

            lines = [
                "SR false attestation rate elevated (SR claimed passed but CI failed):"
            ]
            for cat, data in sorted(elevated.items()):
                rate = data.get("false_attestation_rate")
                n = data.get("false_attested", 0)
                total = data.get("ci_confirmed", 0) + n
                lines.append(f"  • {cat}: {rate:.0%} ({n}/{total})")
            description = "\n".join(lines)
            await post_failure_alert_message(session, project_id, description)

            new_title = _format_sr_alert_title(
                _FALSE_ATTESTATION_ALERT_TITLE_PREFIX, sorted(elevated.keys())
            )
            existing_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(
                                f"{_FALSE_ATTESTATION_ALERT_TITLE_PREFIX}%"
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not any(a.title == new_title for a in existing_alerts):
                for a in existing_alerts:
                    a.dismissed_at = now
                    session.add(a)
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=new_title,
                        message=description,
                    )
                )
        except Exception:
            logger.exception(
                "[SR_FALSE_ATTESTATION] alert emission failed (non-critical)"
            )

    return result


# ---------------------------------------------------------------------------
# SR alert drilldown helper
# ---------------------------------------------------------------------------


def get_sr_alert_drilldown_tasks(
    all_tasks: list,
    design_doc: dict,
    categories: list[str],
    *,
    alert_type: str = "trends",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Return tasks from the current SR alert window where SR missed the given categories.

    Finds tasks where the human reviewer flagged at least one category from
    ``categories`` but SR did not (category absent from ``check_results`` or
    ``passed`` is True).  Window is derived from the stored alert state:
    - cycle_id present -> filter by ``strategic_cycle_id``
    - no cycle_id -> last 14 calendar days (calendar fallback)

    PR URLs are not fetched here; the calling endpoint should batch-query
    Artifact rows by the returned task_id list.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    alert_key = (
        "sr_catch_rate_trends_alert"
        if alert_type == "trends"
        else "sr_catch_rate_floor_alert"
    )
    alert_state: dict = dict(design_doc.get(alert_key) or {})
    cycle_id: Optional[str] = alert_state.get("cycle_id")
    target_cats = set(categories)

    if cycle_id:
        window_tasks = [
            t for t in all_tasks if getattr(t, "strategic_cycle_id", None) == cycle_id
        ]
    else:
        cutoff = _naive(now - timedelta(days=14))
        window_tasks = [
            t for t in all_tasks if t.created_at and _naive(t.created_at) >= cutoff
        ]

    results: list[dict[str, Any]] = []
    for t in window_tasks:
        ctx = t.context or {}
        if "_self_review_outcome" not in ctx:
            continue
        check_results = ctx["_self_review_outcome"].get("check_results") or {}
        # Author SR failures only — a reviewer-rescued category should still
        # surface in the drilldown as `missed_in_target` because the SR itself
        # did not catch it.
        sr_failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict)
            and not r.get("passed", True)
            and r.get("origin") not in _REVIEWER_ORIGINS
        }
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or [])
        missed_in_target = (human_cats & target_cats) - sr_failed
        if not missed_in_target:
            continue
        caught_in_target = human_cats & target_cats & sr_failed
        payload = t.first_payload or {}
        findings_list = ctx["_self_review_outcome"].get("reviewer_findings") or []
        findings_by_cat = {
            f["category"]: f
            for f in findings_list
            if isinstance(f, dict) and "category" in f
        }
        missed_category_findings: dict[str, Any] = {}
        for cat in missed_in_target:
            finding = findings_by_cat.get(cat)
            if finding:
                desc = finding.get("description") or ""
                missed_category_findings[cat] = {
                    "severity": finding.get("severity"),
                    "description": desc[:140],
                    "has_more": len(desc) > 140,
                    "full_description": desc if len(desc) > 140 else None,
                }
            else:
                missed_category_findings[cat] = {
                    "severity": None,
                    "description": "no description recorded",
                    "has_more": False,
                    "full_description": None,
                }
        results.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": payload.get("type", ""),
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "missed_categories": sorted(missed_in_target),
                "caught_categories": sorted(caught_in_target),
                "sr_verdict": ctx["_self_review_outcome"].get("verdict"),
                "missed_category_findings": missed_category_findings,
            }
        )

    results.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return results[:page_size]
