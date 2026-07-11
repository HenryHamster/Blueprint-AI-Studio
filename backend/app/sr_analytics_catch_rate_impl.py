"""Private implementation helpers for SR catch-rate trends."""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.sr_analytics_catch_rate import (
    _stats_for_tasks,
    compute_sr_catch_rate_by_direction,
)
from app.sr_analytics_common import (
    _EXAMPLE_MAX_CATS,
    _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    _HALLUCINATION_RATE_MIN_FINDINGS,
    _MIN_SR_FINDINGS,
    _MIN_SR_SAMPLE,
    _PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
    _SR_ENGAGEMENT_MIN_PASSES,
    _SR_ENGAGEMENT_THRESHOLD,
    _SR_FLOOR_ALERT_TITLE_PREFIX,
    _SR_FLOOR_THRESHOLD,
    _SR_QUADRANT_ALERT_TITLE_PREFIX,
    _SR_REGRESSION_ALERT_TITLE_PREFIX,
    _SR_SEVERITY_DELTA_THRESHOLDS,
    _TREND_DELTA_THRESHOLD,
    _collect_prompt_versions,
    _compute_cat_fp_stats,
    _compute_cat_severity_calibration,
    _compute_miss_latency_stats,
    _compute_reasoning_engagement,
    _finalize_examples,
    _format_sr_alert_title,
    _is_trend_significant,
    _sr_skip_stats,
)

logger = logging.getLogger(__name__)


def _reviewer_finding_count(category_data: dict[str, Any], prefix: str) -> int:
    """Read reviewer-grounded category counts with legacy fallbacks.

    Canonical payload fields are ``{prefix}_reviewer_finding_count``. Older
    payloads may still expose ``{prefix}_finding_count`` or
    ``{prefix}_human_count``.
    """

    canonical_key = f"{prefix}_reviewer_finding_count"
    if canonical_key in category_data:
        return int(category_data.get(canonical_key) or 0)
    finding_key = f"{prefix}_finding_count"
    if finding_key in category_data:
        return int(category_data.get(finding_key) or 0)
    return int(category_data.get(f"{prefix}_human_count") or 0)


async def _compute_sr_catch_rate_trends_impl(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

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
        # id-only projection (only .id is used below) + the
        # ix_strategiccycle_project_outcome_phase1 index: ordered index scan
        # satisfies LIMIT 4 directly (was a full scan + sort of every completed
        # cycle → 120s statement_timeout that poisoned the claim, 2026-05-29).
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        relevant_cycle_ids = [r.id for r in cycle_rows]
        dialect = resolve_dialect_name(session)
        cycle_where = [
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.COMPLETED]),
            Task.strategic_cycle_id.in_(relevant_cycle_ids),
        ]
        if dialect != "sqlite":
            cycle_where.append(Task.context.has_key("_self_review_outcome"))
        cycle_tasks = (
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
                            # Read by sr_analytics_common._updated_naive via
                            # getattr(task, "updated_at", None) — getattr does NOT
                            # suppress the raiseload InvalidRequestError (only
                            # AttributeError), which is otherwise swallowed by the
                            # caller and silently aborts the catch-rate update.
                            Task.updated_at,
                        raiseload=True,)
                    )
                    .where(*cycle_where)
                )
            )
            .scalars()
            .all()
        )
        task_by_cycle: dict[str, list] = {}
        for t in cycle_tasks:
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

    current_finding_count = sum(s.get("human_count", 0) for s in current_stats.values())
    previous_finding_count = sum(
        s.get("human_count", 0) for s in previous_stats.values()
    )
    if (
        current_finding_count < _MIN_SR_FINDINGS
        or previous_finding_count < _MIN_SR_FINDINGS
    ):
        # skip_locked=True: a concurrent refresh holding the project row is
        # already updating this marker — drop rather than serialize the claim.
        # scalar_one_or_none() then yields None for both "no project" and
        # "row held", and the existing not-None guard skips the write.
        proj_row = await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(skip_locked=True, key_share=True)
        )
        project = proj_row.scalar_one_or_none()
        if project is not None:
            doc = dict(project.design_doc or {})
            trends = dict(doc.get("sr_catch_rate_trends") or {})
            trends["signal_last_computed_at"] = now.isoformat()
            trends["data_sufficient"] = False
            doc["sr_catch_rate_trends"] = trends
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
        return None

    all_cats = (
        set(current_stats.keys())
        | set(previous_stats.keys())
        | set(current_all_iter_stats.keys())
    )
    if not all_cats:
        return None

    empty_cat: dict[str, Any] = {
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
        curr = current_stats.get(cat, empty_cat)
        prev = previous_stats.get(cat, empty_cat)
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

        curr_findings = curr.get("human_count", 0)
        prev_findings = prev.get("human_count", 0)
        has_trend_data = (
            current_hit_rate is not None
            and previous_hit_rate is not None
            and curr_findings >= _MIN_SR_FINDINGS
            and prev_findings >= _MIN_SR_FINDINGS
        )
        if has_trend_data:
            delta = current_hit_rate - previous_hit_rate
            sig = _is_trend_significant(
                current_hit_rate,
                curr_findings,
                previous_hit_rate,
                prev_findings,
            )
            if sig and delta >= _TREND_DELTA_THRESHOLD:
                trend = "increasing"
            elif sig and delta <= -_TREND_DELTA_THRESHOLD:
                trend = "decreasing"
            else:
                trend = "stable"
            trend_significant: Optional[bool] = (
                sig and abs(delta) >= _TREND_DELTA_THRESHOLD
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
            fp_sig = (
                curr_sr_failed >= _MIN_SR_FINDINGS
                and prev_sr_failed >= _MIN_SR_FINDINGS
                and _is_trend_significant(
                    current_fp_rate, curr_sr_failed, previous_fp_rate, prev_sr_failed
                )
            )
            if fp_sig and fp_delta >= _TREND_DELTA_THRESHOLD:
                fp_trend = "increasing"
            elif fp_sig and fp_delta <= -_TREND_DELTA_THRESHOLD:
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
            q = _PRECISION_RECALL_QUADRANT_THRESHOLD
            p_up = precision_delta >= q
            p_down = precision_delta <= -q
            r_up = recall_delta >= q
            r_down = recall_delta <= -q
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
            "current_reviewer_finding_count": curr_findings,
            "previous_reviewer_finding_count": prev_findings,
            "low_confidence_finding_count": curr_findings < _MIN_SR_FINDINGS,
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

    # skip_locked=True: if a concurrent refresh already holds the project row,
    # skip this recompute's write rather than serialize the claim path behind
    # it. scalar_one_or_none() returns None when the row is held (or absent),
    # and the existing not-found guard below skips the write — the in-flight
    # refresh produces the updated trend, so behavior is preserved.
    proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=True, key_share=True)
    )
    project = proj_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing: dict = dict(doc.get("sr_catch_rate_trends") or {})
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

    floor_thresh = float(doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
    floor_min_win = int(
        doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )
    for cat, data in categories.items():
        history = data.get("rate_history") or []
        data["below_floor"] = len(history) >= floor_min_win and all(
            r < floor_thresh for r in history[-floor_min_win:]
        )

    regressing_cats = {
        cat
        for cat, data in categories.items()
        if data.get("trend") == "decreasing" or data.get("below_floor")
    }
    all_cats = set(categories.keys())
    eng = _compute_reasoning_engagement(current_window_tasks, all_cats)
    for cat, cat_eng in eng.items():
        categories[cat]["reasoning_engagement_rate"] = cat_eng["engagement_rate"]
        categories[cat]["reasoning_engagement_with"] = cat_eng["with_reasoning"]
        categories[cat]["reasoning_engagement_total"] = cat_eng["total_passes"]

    eng_threshold = float(doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD)
    eng_min_passes = int(
        doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES
    )
    for cat, data in categories.items():
        rate = data.get("reasoning_engagement_rate")
        total = data.get("reasoning_engagement_total") or 0
        data["engagement_thinning"] = bool(
            rate is not None and rate < eng_threshold and total >= eng_min_passes
        )

    cat_fp_stats, type_cat_fp_stats, scope_cat_fp_stats = _compute_cat_fp_stats(
        current_window_tasks
    )
    prev_cat_fp_stats, _, _ = _compute_cat_fp_stats(previous_window_tasks)
    for cat, data in categories.items():
        fp_data = cat_fp_stats.get(cat) or {}
        fp = fp_data.get("fp", 0)
        tp = fp_data.get("tp", 0)
        curr_total = fp + tp
        data["fp_count"] = fp
        data["tp_count"] = tp
        data["false_positive_rate"] = (
            round(fp / curr_total, 3) if curr_total > 0 else None
        )
        # precision = 1 - fpr by construction (same fp/tp basis)
        data["precision"] = round(tp / curr_total, 3) if curr_total > 0 else None
        prev_fp_data = prev_cat_fp_stats.get(cat) or {}
        prev_fp = prev_fp_data.get("fp", 0)
        prev_tp = prev_fp_data.get("tp", 0)
        prev_total = prev_fp + prev_tp
        data["previous_fp_rate"] = (
            round(prev_fp / prev_total, 3) if prev_total > 0 else None
        )
        prev_precision = round(prev_tp / prev_total, 3) if prev_total > 0 else None
        # Recompute fp_trend from the same basis
        curr_fpr = data["false_positive_rate"]
        prev_fpr = data["previous_fp_rate"]
        if curr_fpr is not None and prev_fpr is not None:
            fp_delta = curr_fpr - prev_fpr
            fp_sig = (
                curr_total >= _MIN_SR_FINDINGS
                and prev_total >= _MIN_SR_FINDINGS
                and _is_trend_significant(curr_fpr, curr_total, prev_fpr, prev_total)
            )
            if fp_sig and fp_delta >= _TREND_DELTA_THRESHOLD:
                data["fp_trend"] = "increasing"
            elif fp_sig and fp_delta <= -_TREND_DELTA_THRESHOLD:
                data["fp_trend"] = "decreasing"
            else:
                data["fp_trend"] = "stable"
        else:
            data["fp_trend"] = "stable"
        # Recompute precision_delta and precision_recall_quadrant from consistent basis
        precision_delta_new: Optional[float] = (
            round(data["precision"] - prev_precision, 3)
            if data["precision"] is not None and prev_precision is not None
            else None
        )
        data["precision_delta"] = precision_delta_new
        recall_delta = data.get("recall_delta")
        if recall_delta is not None and precision_delta_new is not None:
            q = _PRECISION_RECALL_QUADRANT_THRESHOLD
            p_up = precision_delta_new >= q
            p_down = precision_delta_new <= -q
            r_up = recall_delta >= q
            r_down = recall_delta <= -q
            if p_up and r_up:
                data["precision_recall_quadrant"] = "healthy"
            elif p_up and r_down:
                data["precision_recall_quadrant"] = "conservative_shift"
            elif p_down and r_up:
                data["precision_recall_quadrant"] = "noisy_shift"
            elif p_down and r_down:
                data["precision_recall_quadrant"] = "both_declining"
            elif not p_up and not p_down and not r_up and not r_down:
                data["precision_recall_quadrant"] = "flat"
            else:
                data["precision_recall_quadrant"] = "mixed"
        else:
            data["precision_recall_quadrant"] = data.get("precision_recall_quadrant")

    sev_cal = _compute_cat_severity_calibration(current_window_tasks)
    for cat, data in categories.items():
        cal = sev_cal.get(cat)
        if cal:
            data["severity_calibration"] = cal

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
    for task in current_window_tasks:
        fix_iter_buckets[_fix_iter_bucket(task)].append(task)
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
            entry = {
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
            prev_scope_sample = int(prev_scope_entry.get("sample_count") or 0)
            if prev_scope_hit_rate is not None:
                s_delta = hit_rate - prev_scope_hit_rate
                scope_n_curr = stats["human_count"]
                scope_sig = (
                    scope_n_curr >= _MIN_SR_FINDINGS
                    and prev_scope_sample >= _MIN_SR_FINDINGS
                    and _is_trend_significant(
                        hit_rate, scope_n_curr, prev_scope_hit_rate, prev_scope_sample
                    )
                )
                if scope_sig and s_delta >= _TREND_DELTA_THRESHOLD:
                    scope_trend: str = "increasing"
                elif scope_sig and s_delta <= -_TREND_DELTA_THRESHOLD:
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
            sev_n_curr = curr_s["human_count"]
            sev_n_prev = prev_s["human_count"]
            sev_sig = (
                sev_n_curr >= _MIN_SR_FINDINGS
                and sev_n_prev >= _MIN_SR_FINDINGS
                and _is_trend_significant(curr_miss, sev_n_curr, prev_miss, sev_n_prev)
            )
            sev_trend = (
                "regressing"
                if sev_sig and sev_delta >= thr
                else "improving"
                if sev_sig and sev_delta <= -thr
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

    current_total_hits = 0
    current_total_reviewers = 0
    previous_total_hits = 0
    previous_total_reviewers = 0
    for cat in all_cats:
        curr = current_stats.get(cat, empty_cat)
        prev = previous_stats.get(cat, empty_cat)
        if curr.get("human_count", 0) > 0:
            current_total_hits += curr.get("hit_count", 0)
            current_total_reviewers += curr["human_count"]
        if prev.get("human_count", 0) > 0:
            previous_total_hits += prev.get("hit_count", 0)
            previous_total_reviewers += prev["human_count"]
    overall_hit_rate = (
        round(current_total_hits / current_total_reviewers, 3)
        if current_total_reviewers > 0
        else None
    )
    previous_overall_hit_rate = (
        round(previous_total_hits / previous_total_reviewers, 3)
        if previous_total_reviewers > 0
        else None
    )

    current_sr_alone_hits = 0
    current_sr_alone_humans = 0
    previous_sr_alone_hits = 0
    previous_sr_alone_humans = 0
    for cat in all_cats:
        c = current_stats.get(cat, empty_cat)
        p = previous_stats.get(cat, empty_cat)
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
    sr_alone_data_sufficient = (
        overall_sr_alone_hit_rate is not None
        and previous_overall_sr_alone_hit_rate is not None
        and current_sr_alone_humans >= _MIN_SR_FINDINGS
        and previous_sr_alone_humans >= _MIN_SR_FINDINGS
    )
    if sr_alone_data_sufficient:
        sr_alone_delta = overall_sr_alone_hit_rate - previous_overall_sr_alone_hit_rate
        sr_alone_sig = _is_trend_significant(
            overall_sr_alone_hit_rate,
            current_sr_alone_humans,
            previous_overall_sr_alone_hit_rate,
            previous_sr_alone_humans,
        )
        if sr_alone_sig and sr_alone_delta >= _TREND_DELTA_THRESHOLD:
            overall_sr_alone_trend_verdict: str = "improving"
        elif sr_alone_sig and sr_alone_delta <= -_TREND_DELTA_THRESHOLD:
            overall_sr_alone_trend_verdict = "declining"
        else:
            overall_sr_alone_trend_verdict = "stable"
        overall_sr_alone_trend_significant: Optional[bool] = (
            sr_alone_sig and abs(sr_alone_delta) >= _TREND_DELTA_THRESHOLD
        )
    else:
        overall_sr_alone_trend_verdict = "stable"
        overall_sr_alone_trend_significant = None

    overall_data_sufficient = (
        overall_hit_rate is not None
        and previous_overall_hit_rate is not None
        and current_total_reviewers >= _MIN_SR_FINDINGS
        and previous_total_reviewers >= _MIN_SR_FINDINGS
    )
    if overall_data_sufficient:
        overall_delta = overall_hit_rate - previous_overall_hit_rate
        overall_sig = _is_trend_significant(
            overall_hit_rate,
            current_total_reviewers,
            previous_overall_hit_rate,
            previous_total_reviewers,
        )
        if overall_sig and overall_delta >= _TREND_DELTA_THRESHOLD:
            overall_trend_verdict: str = "improving"
        elif overall_sig and overall_delta <= -_TREND_DELTA_THRESHOLD:
            overall_trend_verdict = "declining"
        else:
            overall_trend_verdict = "stable"
        overall_trend_significant: Optional[bool] = (
            overall_sig and abs(overall_delta) >= _TREND_DELTA_THRESHOLD
        )
    else:
        overall_trend_verdict = "stable"
        overall_trend_significant = None

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
        "overall_trend_data_sufficient": overall_data_sufficient,
        "overall_sr_alone_hit_rate": overall_sr_alone_hit_rate,
        "previous_overall_sr_alone_hit_rate": previous_overall_sr_alone_hit_rate,
        "overall_sr_alone_trend_verdict": overall_sr_alone_trend_verdict,
        "overall_sr_alone_trend_significant": overall_sr_alone_trend_significant,
        "overall_sr_alone_trend_data_sufficient": sr_alone_data_sufficient,
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
        for scope_cats in by_scope.values():
            for scope_entry in scope_cats.values():
                scope_entry["prompt_version_changed"] = True
    if sr_skip_rate is not None:
        result["sr_skip_rate"] = sr_skip_rate
        result["sr_skip_reason_distribution"] = sr_skip_dist

    dir_text_map: dict[str, str] = {
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
        direction_text_map=dir_text_map or None,
    )
    result["by_direction"] = by_direction or {}

    from app.sr_blind_spots import (
        aggregate_sr_co_failure_pairs,
        _count_reviewer_disagreement_directions,
    )

    co_pairs = aggregate_sr_co_failure_pairs(current_window_tasks)
    result["co_failure_pairs"] = co_pairs or []

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

    curr_hall_total = 0
    curr_hall_filtered = 0
    for task in current_window_tasks:
        sro = (getattr(task, "context", None) or {}).get("_self_review_outcome") or {}
        rff = sro.get("reviewer_findings_filtered") or {}
        task_total = rff.get("total_reviewer_findings")
        task_filtered = rff.get("filtered_count")
        if isinstance(task_total, int) and isinstance(task_filtered, int):
            curr_hall_total += task_total
            curr_hall_filtered += task_filtered
    prev_hall_total = 0
    prev_hall_filtered = 0
    for task in previous_window_tasks:
        sro = (getattr(task, "context", None) or {}).get("_self_review_outcome") or {}
        rff = sro.get("reviewer_findings_filtered") or {}
        task_total = rff.get("total_reviewer_findings")
        task_filtered = rff.get("filtered_count")
        if isinstance(task_total, int) and isinstance(task_filtered, int):
            prev_hall_total += task_total
            prev_hall_filtered += task_filtered
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

    try:
        import asyncio
        from app.notification_dispatch import (
            dispatch_external_notification,
            post_failure_alert_message,
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
                        curr_str = f"{curr_rate:.0%}" if curr_rate is not None else "N/A"
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
                        prev_str = f"{prev_miss:.0%}" if prev_miss is not None else "N/A"
                        curr_str = f"{curr_miss:.0%}" if curr_miss is not None else "N/A"
                        lines.append(f"  • {sev}: {prev_str} → {curr_str} (n={sample_n})")
                description = "\n".join(lines)

                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

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
                                Alert.title.like(f"{_SR_REGRESSION_ALERT_TITLE_PREFIX}%"),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_alert_title for a in existing_reg_alerts):
                    for alert in existing_reg_alerts:
                        alert.dismissed_at = now
                        session.add(alert)
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
                            "previous_hit_rate": alertable[cat].get("previous_hit_rate"),
                            "current_hit_rate": alertable[cat].get("current_hit_rate"),
                            "false_positive_rate": alertable[cat].get("false_positive_rate"),
                            "fp_trend": alertable[cat].get("fp_trend"),
                            "sample_size": current_stats.get(cat, {}).get("human_count", 0),
                        }
                        for cat in current_regressing_cats
                    ],
                    "regressing_severities": [
                        {
                            "severity": sev,
                            "previous_miss_rate": regressing_severities[sev].get("previous_miss_rate"),
                            "current_miss_rate": regressing_severities[sev].get("current_miss_rate"),
                            "sample_size": regressing_severities[sev].get("current_sample", 0),
                        }
                        for sev in current_regressing_sevs
                    ],
                }
                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
        elif doc.get("sr_catch_rate_trends_alert"):
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
            for alert in stale_alerts:
                alert.dismissed_at = now
                session.add(alert)
            doc.pop("sr_catch_rate_trends_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception("[SR_CATCH_RATE_TRENDS] notification failed, suppressing")

    try:
        import asyncio
        from app.notification_dispatch import (
            dispatch_external_notification,
            post_failure_alert_message,
        )

        floor_threshold: float = float(doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
        floor_min_windows: int = int(
            doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS
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

                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

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
                    for alert in existing_floor_alerts:
                        alert.dismissed_at = now
                        session.add(alert)
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
                            "current_hit_rate": below_floor[cat].get("current_hit_rate"),
                            "rate_history": below_floor[cat].get("rate_history"),
                            "sample_size": current_stats.get(cat, {}).get("human_count", 0),
                        }
                        for cat in current_floor_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
        elif doc.get("sr_catch_rate_floor_alert"):
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
            for alert in stale_floor_alerts:
                alert.dismissed_at = now
                session.add(alert)
            doc.pop("sr_catch_rate_floor_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception("[SR_CATCH_RATE_TRENDS] floor notification failed, suppressing")

    try:
        import asyncio
        from app.notification_dispatch import (
            dispatch_external_notification,
            post_failure_alert_message,
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

                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
                    for alert in existing_q_alerts:
                        alert.dismissed_at = now
                        session.add(alert)
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
                            "quadrant": quadrant_alertable[cat].get("precision_recall_quadrant"),
                            "precision_delta": quadrant_alertable[cat].get("precision_delta"),
                            "recall_delta": quadrant_alertable[cat].get("recall_delta"),
                        }
                        for cat in current_quadrant_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
        elif doc.get("sr_quadrant_alert"):
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
            for alert in stale_q_alerts:
                alert.dismissed_at = now
                session.add(alert)
            doc.pop("sr_quadrant_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception("[SR_CATCH_RATE_TRENDS] quadrant notification failed, suppressing")

    try:
        import asyncio
        from app.notification_dispatch import (
            dispatch_external_notification,
            post_failure_alert_message,
        )

        eng_threshold = float(doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD)
        eng_min_passes = int(
            doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES
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

                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
                    for alert in existing_eng_alerts:
                        alert.dismissed_at = now
                        session.add(alert)
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
                            "engagement_rate": thinning_cats[cat].get("reasoning_engagement_rate"),
                            "total_passes": thinning_cats[cat].get("reasoning_engagement_total", 0),
                        }
                        for cat in current_thinning_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get("mixed_prompt_versions"):
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
        elif doc.get("sr_pass_engagement_alert"):
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
            for alert in stale_eng_alerts:
                alert.dismissed_at = now
                session.add(alert)
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
