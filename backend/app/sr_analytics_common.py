"""Shared SR analytics constants and helper utilities.

These helpers are used by the catch-rate computation and by compatibility
surfaces that expose adjunct SR analytics helpers.
"""

from collections import Counter
from datetime import datetime
import re
from typing import Any, Optional

from app.models import Task
from app.shared_state import DEFAULT_TASK_SCOPE
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    reviewer_confirmed_categories,
    safe_check_results,
)

# Re-exported from sr_blind_spots (the lowest-level SR module) so consumers that
# already import from sr_analytics_common can reach the guard without reaching
# past it. Defining it here instead would create an import cycle, since this
# module imports from sr_blind_spots above.
__all__ = ["safe_check_results"]


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
# rejection categories required per window. Half the human-review threshold
# since SR data is sparser.
_MIN_SR_SAMPLE = 5
# Minimum total reviewer findings (across categories in a window) required for
# the catch-rate and its cycle-over-cycle trend to be considered well-sampled.
_MIN_SR_FINDINGS = 5
# Minimum total reviewer findings (across tasks in a window) required to emit
# a hallucination-rate value; prevents noisy rates on tiny windows.
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

_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}
_EDGE_CATS = {"correctness", "error-handling"}

# Retired: execution-evidence miss-rate aggregation over tests/lint/typecheck
# outcomes had no real consumer beyond compatibility shims, so the helpers were deleted.


def _categorize_review_issues(review_history: list) -> dict[str, list[dict]]:
    from app.review_learning import _categorize_review_issues as _impl

    return _impl(review_history)


def _categorize_review_issues_keyword(review_history: list) -> dict[str, list[dict]]:
    from app.review_learning import _categorize_review_issues_keyword as _impl

    return _impl(review_history)


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
    sev_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    sorted_ex = sorted(raw_examples, key=lambda x: sev_order.get(x["severity"], 3))
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


def _sr_skip_stats(
    tasks: list,
) -> tuple[Optional[float], Optional[dict[str, int]]]:
    """Compute skip-rate and reason distribution over tasks-with-SR-outcome."""
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


def _compute_cat_fp_stats(
    tasks: list,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, dict[str, dict[str, int]]],
    dict[str, dict[str, dict[str, int]]],
]:
    """Return per-category fp/tp counts for SR failures."""
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
        check_results = safe_check_results(sro)
        sr_failed = {
            category
            for category, result in check_results.items()
            if isinstance(result, dict)
            and not result.get("passed", True)
            and result.get("origin") not in _REVIEWER_ORIGINS
        }
        if not sr_failed:
            continue
        reviewer_confirmed_cats = reviewer_confirmed_categories(ctx)
        confirmed = reviewer_confirmed_cats
        task_scope = ctx.get("scope", DEFAULT_TASK_SCOPE)
        for category in sr_failed:
            bucket = cat_fp.setdefault(category, {"fp": 0, "tp": 0})
            tbucket = type_cat_fp.setdefault(task_type, {}).setdefault(
                category, {"fp": 0, "tp": 0}
            )
            sbucket = scope_cat_fp.setdefault(task_scope, {}).setdefault(
                category, {"fp": 0, "tp": 0}
            )
            if category in confirmed:
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
    """Return per-category severity calibration counts."""
    sev_weight = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    result: dict[str, dict[str, int]] = {}
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro:
            continue
        check_results = safe_check_results(sro)
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        if not rev_hist:
            continue
        confirmed = reviewer_confirmed_categories(ctx)
        if not confirmed:
            continue
        rev_cats = _categorize_review_issues(rev_hist)
        for category, rev_issues in rev_cats.items():
            if category not in confirmed:
                continue
            cr = check_results.get(category)
            if not isinstance(cr, dict):
                continue
            if cr.get("passed", True):
                continue
            if cr.get("origin") in _REVIEWER_ORIGINS:
                continue
            agent_sev = (cr.get("severity") or "").upper()
            if agent_sev not in sev_weight:
                continue
            sev_vals = [
                sev_weight[issue.get("severity", "")]
                for issue in rev_issues
                if isinstance(issue, dict) and issue.get("severity", "") in sev_weight
            ]
            if not sev_vals:
                continue
            reviewer_max_val = max(sev_vals)
            agent_val = sev_weight[agent_sev]
            bucket = result.setdefault(
                category,
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


def _task_prompt_fingerprint(task: Task) -> str:
    """Resolve the task's prompt fingerprint with legacy context fallback."""
    if isinstance(task.prompt_fingerprint, str) and task.prompt_fingerprint:
        return task.prompt_fingerprint
    ctx = task.context or {}
    legacy = ctx.get("prompt_fingerprint")
    if isinstance(legacy, str) and legacy:
        return legacy
    return "unknown"


def _compute_reasoning_engagement(
    tasks: list,
    cats: set[str],
) -> dict[str, dict[str, Any]]:
    """Compute PASS reasoning engagement rate for each category."""
    from app.sr_blind_spots import is_skipped_sr_outcome  # noqa: PLC0415

    stats: dict[str, dict[str, Any]] = {
        cat: {
            "with_reasoning": 0,
            "total_passes": 0,
        }
        for cat in cats
    }
    for t in tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = safe_check_results(sro)
        for cat in cats:
            entry = check_results.get(cat)
            if not isinstance(entry, dict) or not entry.get("passed", True):
                continue
            stats[cat]["total_passes"] += 1
            reasoning = entry.get("reasoning") or ""
            if isinstance(reasoning, str) and len(reasoning.strip()) > 10:
                stats[cat]["with_reasoning"] += 1

    result: dict[str, dict[str, Any]] = {}
    for cat, values in stats.items():
        total = values["total_passes"]
        result[cat] = {
            "engagement_rate": round(values["with_reasoning"] / total, 3)
            if total > 0
            else None,
            "with_reasoning": values["with_reasoning"],
            "total_passes": total,
        }
    return result


def _collect_prompt_versions(tasks: list) -> dict:
    """Return a Counter of prompt_version values seen in _self_review_outcome."""
    counter: Counter = Counter()
    for t in tasks:
        pv = (t.context or {}).get("_self_review_outcome", {}).get("prompt_version")
        if pv is not None:
            counter[str(pv)] += 1
    return counter


def _percentile(values: list, p: float) -> float:
    """Compute p-th percentile (0-100) of a list using linear interpolation."""
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sorted_values[-1])
    frac = idx - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def _compute_miss_latency_stats(tasks: list) -> dict:
    """Return per-category time-to-detect latency stats for SR misses."""

    def _parse_iso(value: object) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, TypeError):
            return None

    def _updated_naive(task: object) -> Optional[datetime]:
        updated_at = getattr(task, "updated_at", None)
        if updated_at is None:
            return None
        return updated_at.replace(tzinfo=None) if updated_at.tzinfo else updated_at

    lone_checker_p95_threshold = 7 * 86400
    latencies: dict[str, list[float]] = {}

    for task in tasks:
        ctx = task.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        check_results = safe_check_results(sr_outcome)
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

        sr_ts: Optional[datetime] = None
        for field in ("completed_at", "computed_at", "stamped_at"):
            sr_ts = _parse_iso(sr_outcome.get(field))
            if sr_ts is not None:
                break
        sr_fallback = sr_ts is None
        if sr_ts is None:
            sr_ts = _updated_naive(task)

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

        if sr_fallback and rej_fallback:
            continue
        if sr_ts is None or rej_ts is None:
            continue

        latency = (rej_ts - sr_ts).total_seconds()
        if latency <= 0:
            continue

        for cat in missed_cats:
            latencies.setdefault(cat, []).append(latency)

    result: dict[str, dict[str, Any]] = {}
    for cat, lats in latencies.items():
        sample_count = len(lats)
        p50 = int(_percentile(lats, 50))
        p95 = int(_percentile(lats, 95))
        result[cat] = {
            "time_to_miss_detect_p50_sec": p50,
            "time_to_miss_detect_p95_sec": p95,
            "time_to_miss_detect_sample_count": sample_count,
            "time_to_miss_detect_lone_checker": sample_count >= 2
            and p95 > lone_checker_p95_threshold,
        }
    return result


def _is_trend_significant(p1: float, n1: int, p2: float, n2: int) -> bool:
    """Return True when the observed two-proportion delta exceeds noise."""
    if n1 < 1 or n2 < 1:
        return False
    pooled_p = (p1 * n1 + p2 * n2) / (n1 + n2)
    if pooled_p <= 0 or pooled_p >= 1:
        return abs(p1 - p2) >= _TREND_DELTA_THRESHOLD
    se = (pooled_p * (1 - pooled_p) * (1 / n1 + 1 / n2)) ** 0.5
    if se == 0:
        return False
    return abs(p1 - p2) / se >= _TREND_Z_MIN
