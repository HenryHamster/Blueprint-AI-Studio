"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)

`cache_first_attempt_classification` is intended to be invoked at
first-review-rejection time by the lifecycle pipeline (sibling cluster wires it
in) so subsequent reads from admin_digest can skip the keyword scan.
"""

import logging
import uuid
from datetime import datetime
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import (
    Project,
    Task,
    TaskStatus,
    _utcnow,
)
from app.sr_blind_spots import _REVIEWER_ORIGINS  # noqa: F401  # re-exported for backward compatibility

# Re-exports from app.playbook_sr_metrics (extracted module).
# SR catch-rate trends, alert title constants, and the suite of helpers
# that feed the SR catch-rate dashboard live in ``app.playbook_sr_metrics``.
# Existing callers continue to ``from app.playbook_learning import <name>``.
from app.playbook_sr_metrics import (  # noqa: F401
    _EDGE_CATS,
    _EXAMPLE_MAX_CATS,
    _EXAMPLE_MAX_DESC_LEN,
    _EXAMPLE_MAX_PER_CAT,
    _EXECUTION_EVIDENCE_CATEGORIES,
    _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    _MIN_DIRECTION_SR_SAMPLE,
    _MIN_SR_FINDINGS,
    _MIN_SR_SAMPLE,
    _PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
    _PR_REF_RE,
    _SENSITIVE_TOKENS,
    _SR_ALERT_TITLE_MAX_PARTS,
    _SR_CODE_CHANGE_TYPES,
    _SR_ENGAGEMENT_MIN_PASSES,
    _SR_ENGAGEMENT_THRESHOLD,
    _SR_FLOOR_ALERT_TITLE_PREFIX,
    _SR_FLOOR_THRESHOLD,
    _SR_QUADRANT_ALERT_TITLE_PREFIX,
    _SR_REGRESSION_ALERT_TITLE_PREFIX,
    _SR_SEVERITY_DELTA_THRESHOLDS,
    _TREND_DELTA_THRESHOLD,
    _TREND_Z_MIN,
    _URL_RE,
    _collect_prompt_versions,
    _compute_cat_fp_stats,
    _compute_cat_severity_calibration,
    _compute_edges_stats,
    _compute_miss_latency_stats,
    _compute_reasoning_engagement,
    _format_sr_alert_title,
    _is_trend_significant,
    _percentile,
    _sr_skip_stats,
    _stats_for_tasks,
    _task_prompt_fingerprint,
    compute_direction_sr_breakdown_for_endpoint,
    compute_sr_catch_rate_by_direction,
    compute_sr_catch_rate_trends,
)

# The playbook entry lifecycle and storage helpers share one canonical home,
# ``app.playbook_storage`` (pending-source finalize, suppression/resurrection,
# retirement, follow/confirm bookkeeping, stale-ref flagging, capacity/archive
# management, and grounding).  Re-exported here so existing callers and tests
# that patch via ``app.playbook_learning.<name>`` keep working unchanged.
from app.playbook_storage import (  # noqa: F401
    _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    _FILE_PATH_RE,
    _IDLE_RETIRE_DAYS,
    _PLAYBOOK_CAPACITY_BYTES,
    _RETIRE_MAX_SUCCESS_RATE,
    _RETIRE_MIN_FOLLOWED,
    _STALE_AGE_DAYS,
    _STALE_CONFIRM_THRESHOLD,
    _extract_file_paths,
    _get_prior_archive_entry,
    _is_auto_playbook_suppressed,
    _playbook_size_bytes,
    branch_grounding_due,
    check_and_flag_stale_refs,
    check_and_flag_stale_refs_branch_aware,
    count_idle_auto_retired_within,
    count_low_success_retired_within,
    finalize_pending_playbook_entries,
    ground_design_doc_systems,
    ground_design_doc_systems_branch_aware,
    mark_branch_grounding_done,
    recover_grounding_downgrades,
    recover_grounding_downgrades_branch_aware,
    maybe_archive_for_capacity,
    record_playbook_applied,
    record_playbook_confirmed,
    retire_idle_auto_entries,
    retire_ineffective_patterns,
)

# In-app Alert row title prefixes for SR catch-rate alerts. The router that
# powers AlertsBanner returns Alert rows verbatim; we use these prefixes both
# to format new rows and to find/dismiss prior matching rows on clear-transition.
# Cap how many regressing categories/severities we name in an Alert title
# before truncating; the full breakdown lives in `Alert.message`.
# z=1.28 ≈ 80% CI two-tailed; used by significance-gated SR trend helpers below.


# Re-exports from sibling SR analytics modules for names not already covered by
# the playbook_sr_metrics re-export block above. Existing callers continue to
# ``from app.playbook_learning import <name>``.
from app.sr_edges_trends import compute_edges_trend  # noqa: E402,F401
from app.sr_signal_trends import (  # noqa: E402,F401
    _FALSE_ATTESTATION_RATE_THRESHOLD,
    compute_sr_false_attestation_trends,
    compute_sr_regression_risk_test_signal_trends,
    get_sr_alert_drilldown_tasks,
)

# Capacity-management constants/functions are in playbook_capacity.py (imported above).
# Entry-lifecycle constants (_RETIRE_MIN_FOLLOWED, _IDLE_RETIRE_DAYS,
# _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS, _STALE_AGE_DAYS,
# _STALE_CONFIRM_THRESHOLD, _FILE_PATH_RE) live in playbook_storage.py
# and are re-exported above for backward compatibility.

# Review-extracted playbook entries are refreshed in-place when their
# updated_at exceeds _REVIEW_PLAYBOOK_REFRESH_DAYS. Stale-flagging and the
# broader storage lifecycle live in app.playbook_storage and are re-exported
# here for backward compatibility.
_REVIEW_PLAYBOOK_REFRESH_DAYS = 30

# Execution-evidence SR check categories. These track whether the agent
# actually ran verification commands (pytest, lint, typecheck) and got exit 0.
# Miss rate = fraction of tasks where the agent reported passed: false.

# _FILE_PATH_RE is re-exported above from playbook_storage.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
# Entry-lifecycle helpers (check_and_flag_stale_refs,
# finalize_pending_playbook_entries, _get_prior_archive_entry,
# _is_auto_playbook_suppressed, record_playbook_applied,
# record_playbook_confirmed, retire_ineffective_patterns,
# retire_idle_auto_entries, count_idle_auto_retired_within,
# count_low_success_retired_within) live in app.playbook_storage and
# are re-exported above.


# ---------------------------------------------------------------------------
# Auto-playbook from failure patterns
# ---------------------------------------------------------------------------
# Failure-signature extraction and the ``_maybe_auto_playbook`` writer were
# extracted to ``app.playbook_auto``.  Re-exported here so callers and tests
# that patch via ``app.playbook_learning._maybe_auto_playbook`` keep working.
from app.playbook_auto import (  # noqa: E402,F401
    _extract_failure_module,
    _extract_failure_signature,
    _hint_for_failure_class,
    _maybe_auto_playbook,
    _signature_to_key,
)
from app.review_learning import (  # noqa: E402,F401
    _DIRECTION_FIRST_ATTEMPT_KEY,
    _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    _MIN_REVIEW_SAMPLE,
    _MODULE_FEEDBACK_DECAY_AGE_DAYS,
    _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
    _MODULE_FEEDBACK_KEY,
    _MODULE_FEEDBACK_MAX_ENTRIES,
    _REVIEW_CATEGORIES,
    _VALID_CATEGORIES,
    _accumulate_direction_first_attempt,
    _categorize_review_issues,
    _categorize_review_issues_keyword,
    _decay_module_feedback_entry,
    _keyword_from_flat,
    _maybe_auto_playbook_from_first_attempt,
    _update_module_feedback_profiles,
    cache_first_attempt_classification,
    categorize_review_issues_llm,
    compute_recurring_first_review_descriptions,
    compute_review_effectiveness,
    extract_review_feedback_to_playbook,
)


def summarize_decomposition_outcomes(direction: dict) -> Optional[dict]:
    """Summarize decomposition_quality from the last 3 productive research cycles.

    Returns None if there is no decomposition data to summarize.
    """
    history = direction.get("research_history", []) or []
    productive = [h for h in history if h.get("tasks_created", 0) > 0][-3:]
    flat: list[dict] = []
    for cycle in productive:
        dq = cycle.get("decomposition_quality") or []
        if isinstance(dq, list):
            flat.extend(e for e in dq if isinstance(e, dict))
    if not flat:
        return None

    def _nums(key, cast=float):
        return [cast(e[key]) for e in flat if isinstance(e.get(key), (int, float))]

    merge_rates = _nums("subtask_merge_rate")
    files_per = _nums("avg_files_per_subtask")
    max_files_vals = _nums("max_files_in_subtask", int)
    dep_depths = _nums("dependency_depth", int)
    high_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) >= 2
    ]
    low_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) < 2
    ]
    high_corr_low = False
    if high_xmod and low_xmod:
        if (sum(high_xmod) / len(high_xmod)) + 0.1 <= (sum(low_xmod) / len(low_xmod)):
            high_corr_low = True

    out: dict = {
        "prior_decomposition_count": len(flat),
        "high_cross_module_correlates_with_low_merge": high_corr_low,
    }
    if merge_rates:
        out["avg_subtask_merge_rate"] = round(sum(merge_rates) / len(merge_rates), 3)
    if files_per:
        out["avg_files_per_subtask"] = round(sum(files_per) / len(files_per), 2)
    if max_files_vals:
        out["max_files_in_subtask"] = max(max_files_vals)
    if dep_depths:
        out["avg_dependency_depth"] = round(sum(dep_depths) / len(dep_depths), 2)
        out["max_dependency_depth"] = max(dep_depths)
    return out


# ---------------------------------------------------------------------------
# Success-pattern capture
# ---------------------------------------------------------------------------
# ``capture_success_pattern``, ``summarize_recent_successes`` and
# ``RECENT_SUCCESSES_TASK_TYPES`` live in ``app.success_patterns``. Re-exported
# here so callers and tests that patch via
# ``app.playbook_learning.<name>`` keep working unchanged.
from app.success_patterns import (  # noqa: E402,F401
    RECENT_SUCCESSES_TASK_TYPES,
    capture_success_pattern,
    summarize_recent_successes,
)


# ---------------------------------------------------------------------------


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


def compute_sr_quality_by_fingerprint(
    tasks: list,
    *,
    min_sample_size: int = 3,
) -> dict[str, dict[str, Any]]:
    """Compute additive SR-quality metrics for prompt fingerprint analytics."""
    from app.sr_blind_spots import (  # noqa: PLC0415
        is_skipped_sr_outcome,
        safe_check_results,
    )

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
            },
        )
        bucket["tasks"].append(task)
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        bucket["sr_sample_size"] += 1
        check_results = safe_check_results(sro)
        sr_failed = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict)
            and not result.get("passed", True)
            and result.get("origin") not in _REVIEWER_ORIGINS
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        confirmed = (
            set(ctx.get("_first_attempt_rejection_categories") or {})
            | set(_categorize_review_issues(rev_hist).keys())
        )
        hits = len(confirmed & sr_failed)
        bucket["catch_hits"] += hits
        bucket["catch_total"] += len(confirmed)
        project_catch_hits += hits
        project_catch_total += len(confirmed)

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
                    "sr_false_positive_rate": None,
                    "sr_false_positive_sample_size": 0,
                    "sr_false_positive_rate_by_category": [],
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
                "sr_catch_rate_delta_vs_project_mean": (
                    round(catch_rate - project_catch_rate, 3)
                    if catch_rate is not None and project_catch_rate is not None
                    else None
                ),
            }
        )
        results[fp] = result
    return results


# ---------------------------------------------------------------------------
# Fix-loop divergence regression alert
# ---------------------------------------------------------------------------


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

    from app.models import ProjectSRAnalytics  # noqa: PLC0415
    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    # Read current SR analytics data (no lock) to check the dedup window.
    # Backward compat: fall back to design_doc if no SR analytics row yet.
    _sr_row = await session.get(ProjectSRAnalytics, project_id)
    if _sr_row is not None:
        _current_sr_data = dict(_sr_row.data or {})
        _alerted_window = _current_sr_data.get("fix_loop_divergence_alerted_window")
    else:
        _proj_check = await session.get(Project, project_id, populate_existing=True)
        if _proj_check is None:
            return None
        _alerted_window = (_proj_check.design_doc or {}).get(
            "fix_loop_divergence_alerted_window"
        )

    if _alerted_window == window_key:
        _now_iso = now.isoformat()

        def _update_signal(data: dict) -> None:
            data["fix_loop_divergence_signal_last_computed_at"] = _now_iso

        await with_sr_analytics_locked(session, project_id, _update_signal)
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
                .options(load_only(Task.id, Task.fix_loop_history, Task.created_at, raiseload=True))
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
    _window_key = window_key
    _regressions = regressions
    _now_iso = now.isoformat()

    def _store_divergence(data: dict) -> None:
        data["fix_loop_divergence_alerted_window"] = _window_key
        data["fix_loop_divergence_regressions"] = _regressions
        data["fix_loop_divergence_detected_at"] = _now_iso
        data["fix_loop_divergence_signal_last_computed_at"] = _now_iso

    await with_sr_analytics_locked(session, project_id, _store_divergence)
    await session.commit()
    logger.info(
        "[FIX_LOOP_DIVERGENCE] project=%s loops=%s",
        project_id,
        [r["loop"] for r in regressions],
    )
    return {"regressions": regressions}
