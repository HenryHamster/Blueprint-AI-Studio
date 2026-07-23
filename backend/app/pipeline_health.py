"""Pipeline-health summary and SR-catch-rate alert evaluators.

Extracted from ``app.routers.admin_digest`` (a heavy FastAPI router) into a
cohesive, importable non-router module. This cluster is pure
computation/alerting: each entry point takes an ``AsyncSession`` plus a
``project_id`` and returns or raises, with no FastAPI request/response
plumbing. Living here lets consumers (the pipeline regression dispatcher, the
agent router, and the admin health router) import at module top-level without
the lazy in-function imports they previously needed to dodge circular imports.

``app.routers.admin_digest`` re-exports these names so existing call sites and
test patch targets keep working.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.direction_outcomes import (
    consumed_merged_count,
    proven_merged_count,
    verified_merged_count,
)
from app.digest_direction_progress import pr_ineligible_created_in_entry
from app.landed_signal import is_merged_completion, merge_verification_status
from app.models import (
    Task,
    TaskStatus,
    Project,
    Alert,
    AlertSeverity,
    _utcnow,
)
from app.project_telemetry import read_telemetry_many
from app.sr_analytics_store import read_project_sr_analytics
from app.time_utils import _coerce_naive_utc
from app.routers.admin_analytics import _derive_completed_at
from app.sr_blind_spots import has_reviewer_confirmed_categories, safe_check_results
from app.sr_review_quality_metrics import (
    _compute_edges_considered_metrics,
    _serialize_sr_catch_rate_trends,
    compute_sr_prediction_accuracy,
)

logger = logging.getLogger(__name__)


_SR_CATCH_RATE_TARGET_ALERT_PREFIX = "SR catch-rate below target: "
_SR_CATCH_RATE_TARGET_MIN_WINDOWS = 2
_SR_CATCH_RATE_MIN_FINDINGS = (
    10  # minimum reviewer findings to treat a rate as reliable
)
_LONE_CHECKER_ALERT_TITLE = "SR lone-checker blind spot: ≥2 categories uncorroborated"
_LONE_CHECKER_MIN_CATEGORIES = 2


async def _evaluate_sr_catch_rate_targets(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Emit/dismiss WARNING Alerts for per-category SR catch-rate targets.

    For each category in project.sr_catch_rate_targets, emits a WARNING Alert
    when rate_history shows >=2 consecutive windows below the configured target.
    Dismisses existing alerts when the category recovers. Suppressed when
    prompt_version_changed is True (avoids false alarms during prompt rollouts).
    """
    project = await session.get(Project, project_id)
    if not project:
        return
    targets: Dict[str, Any] = project.sr_catch_rate_targets or {}
    if not targets:
        return
    doc = await read_project_sr_analytics(session, project)
    sr_trends = doc.get("sr_catch_rate_trends") or {}
    if sr_trends.get("prompt_version_changed"):
        return
    categories = sr_trends.get("categories") or {}
    all_titles = [
        f"{_SR_CATCH_RATE_TARGET_ALERT_PREFIX}{cat}" for cat in targets
    ]
    existing_alerts = (
        await session.execute(
            select(Alert).where(
                Alert.project_id == project_id,
                Alert.task_id.is_(None),
                Alert.severity == AlertSeverity.WARNING,
                Alert.dismissed_at.is_(None),
                Alert.title.in_(all_titles),
            )
        )
    ).scalars().all()
    existing_by_title: dict = {a.title: a for a in existing_alerts}
    for cat, target in targets.items():
        try:
            target_float = float(target)
        except (TypeError, ValueError):
            continue
        cat_data = categories.get(cat) or {}
        current_hit_rate = cat_data.get("current_hit_rate")
        title = f"{_SR_CATCH_RATE_TARGET_ALERT_PREFIX}{cat}"

        existing = existing_by_title.get(title)
        if current_hit_rate is not None and current_hit_rate >= target_float:
            if existing is not None:
                existing.dismissed_at = _utcnow()
                await session.commit()
            continue
        # Gate on finding count (true denominator) not task count to avoid spurious
        # alerts when the window clears the task-count bar but rests on a handful of findings.
        current_human_count = cat_data.get("current_reviewer_finding_count") or cat_data.get("current_human_count") or 0
        if current_human_count < _SR_CATCH_RATE_MIN_FINDINGS:
            continue
        rate_history = cat_data.get("rate_history") or []
        if len(rate_history) < _SR_CATCH_RATE_TARGET_MIN_WINDOWS:
            continue
        if not all(
            r < target_float for r in rate_history[-_SR_CATCH_RATE_TARGET_MIN_WINDOWS:]
        ):
            continue
        if existing is not None:
            continue
        trend = cat_data.get("trend") or "unknown"
        curr_str = f"{current_hit_rate:.0%}" if current_hit_rate is not None else "n/a"
        message = (
            f"Self-review catch-rate for '{cat}' has been below the configured target "
            f"({target_float:.0%}) for the last {_SR_CATCH_RATE_TARGET_MIN_WINDOWS} "
            f"measurement windows. Current: {curr_str} (n={current_human_count} reviewer findings). "
            f"Trend: {trend}. "
            f"Review the SR Trend Breakdown in the admin dashboard."
        )
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title=title,
                message=message[:1000],
                event_type="sr.catch_rate_below_target",
            )
        )
        await session.commit()


async def _evaluate_lone_checker_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Open a WARNING Alert when >=2 categories are in lone-checker state.

    Lone-checker means SR is the only reviewer for a category, so an SR PASS
    is uncorroborated and catch-rate measurement is unreliable.  Dedup mirrors
    _evaluate_sr_catch_rate_targets — skip if an open Alert with the same title
    already exists.
    """
    project = await session.get(Project, project_id)
    if not project:
        return
    doc = await read_project_sr_analytics(session, project)
    categories = (doc.get("sr_catch_rate_trends") or {}).get("categories") or {}
    lone_cats = [
        cat
        for cat, data in categories.items()
        if (data or {}).get("time_to_miss_detect_lone_checker")
    ]
    if len(lone_cats) < _LONE_CHECKER_MIN_CATEGORIES:
        return

    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == _LONE_CHECKER_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return

    cats_str = ", ".join(sorted(lone_cats))
    message = (
        f"{len(lone_cats)} categories have entered lone-checker state "
        f"(SR is the only reviewer, catch-rate unmeasurable): {cats_str}. "
        f"Fresh-reviewer is auto-enabled for tasks whose risk surface overlaps "
        f"these categories. Operator action: arrange human review coverage or "
        f"adjust category scope to exit lone-checker state."
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=_LONE_CHECKER_ALERT_TITLE,
            message=message[:1000],
            event_type="sr.lone_checker",
        )
    )
    await session.commit()


async def compute_pipeline_health_summary(
    session: AsyncSession, project_id: uuid.UUID, lookback_hours: int = 24
) -> Dict[str, Any]:
    """Compute lightweight pipeline health metrics for the specified lookback window.

    Returns a dict with:
    - success_rate: float or None
    - trend: 'improving' | 'stable' | 'declining' | None
    - failure_distribution: {failure_class: count} top 3
    - direction_stats: [{direction_id, conversion_rate, cycles, possibly_saturated}]
    """
    from app.failure_classification import classify_failure as _classify_failure

    _now = _utcnow()
    window_start = _now - timedelta(hours=lookback_hours)
    prev_window_start = window_start - timedelta(hours=lookback_hours)

    def _safe_ts_in_window(ts_str: str | None, cutoff: datetime) -> bool:
        try:
            return _coerce_naive_utc(datetime.fromisoformat(ts_str or "")) >= cutoff
        except (ValueError, TypeError):
            return False

    # Query tasks from extended 48h window to compute both current and previous period.
    # Status is filtered in SQL to avoid loading IN_PROGRESS/QUEUED tasks; timestamp
    # window is post-filtered in Python via _derive_completed_at (uses status_log).
    _HEALTH_SUMMARY_TASK_CAP = 5000
    tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.created_at >= prev_window_start,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.created_at,
                Task.completed_at,
                Task.context,
                Task.latest_status_message,
                Task.payloads,
                Task.status_log,
                # Read by classify_failure → _stuck_llm_call_no_progress
                # (failure_classification.py); without it the raiseload error is
                # silently swallowed there and the RATE_LIMIT reclassification
                # is disabled.
                Task.api_call_summary,
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
        .limit(_HEALTH_SUMMARY_TASK_CAP)
    )
    all_tasks = tasks_result.scalars().all()
    if len(all_tasks) >= _HEALTH_SUMMARY_TASK_CAP:
        logger.warning(
            "pipeline_health_summary: task result capped at %d for project %s — "
            "metrics may be truncated; consider reducing the lookback window",
            _HEALTH_SUMMARY_TASK_CAP,
            project_id,
        )

    # Post-filter by completion timestamp
    completed_tasks = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t
        for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = (
        round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None
    )

    # Previous window for trend comparison
    prev_completed = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed = [
        t
        for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total = len(prev_completed) + len(prev_failed)
    prev_success_rate = (
        round(len(prev_completed) / prev_total, 2) if prev_total > 0 else None
    )

    trend: str | None = None
    if success_rate is not None and prev_success_rate is not None:
        delta = success_rate - prev_success_rate
        if delta > 0.05:
            trend = "improving"
        elif delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"

    # Failure distribution (top 3 by count)
    failure_counts: Dict[str, int] = {}
    for t in failed_tasks:
        cls = _classify_failure(t)
        failure_counts[cls] = failure_counts.get(cls, 0) + 1
    failure_distribution = dict(
        sorted(failure_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    )

    from app.failure_classification import (
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT,
        TAXONOMY_INFRA_RELATED,
    )

    _taxonomy_keys = (
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED,
        TAXONOMY_CODE_DEFECT,
    )
    taxonomy_counts: Dict[str, int] = {k: 0 for k in _taxonomy_keys}
    for t in failed_tasks:
        taxonomy_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(taxonomy_counts.values())
    failure_taxonomy = {
        "categories": {
            k: {
                "count": taxonomy_counts[k],
                "percentage": round(taxonomy_counts[k] / _tax_total * 100, 1)
                if _tax_total
                else 0.0,
            }
            for k in _taxonomy_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_taxonomy_keys, key=lambda k: taxonomy_counts[k])
        if _tax_total
        else None,
    }

    _scope_taxonomy_counts: Dict[str, Dict[str, int]] = {}
    for t in failed_tasks:
        scope = (t.context or {}).get("scope", "unknown")
        bucket = _scope_taxonomy_counts.setdefault(
            scope, {k: 0 for k in _taxonomy_keys}
        )
        bucket[_classify_taxonomy(t)] += 1
    failure_taxonomy_by_scope = {}
    for scope, counts in _scope_taxonomy_counts.items():
        tot = sum(counts.values())
        failure_taxonomy_by_scope[scope] = {
            "categories": {
                k: {
                    "count": counts[k],
                    "percentage": round(counts[k] / tot * 100, 1) if tot else 0.0,
                }
                for k in _taxonomy_keys
            },
            "total_failures": tot,
            "dominant_category": max(_taxonomy_keys, key=lambda k: counts[k])
            if tot
            else None,
        }

    # Direction stats: conversion_rate and cycle count per active direction
    direction_stats: List[Dict[str, Any]] = []
    project = await session.get(Project, project_id)
    if project:
        active_directions = [
            d for d in (project.directions or []) if d.get("status") == "active"
        ]

        dir_tasks: Dict[str, list] = {}
        for t in all_tasks:
            did = (t.context or {}).get("direction_id")
            if did:
                dir_tasks.setdefault(str(did), []).append(t)

        for d in active_directions:
            did = str(d.get("id") or d.get("direction_id") or "")
            if not did:
                continue

            tasks_for_dir = dir_tasks.get(did, [])
            cycles = sum(
                1
                for t in tasks_for_dir
                if t.status == TaskStatus.COMPLETED
                and (t.first_payload).get("type", "create") == "research"
                and _derive_completed_at(t) >= window_start
            )

            if cycles == 0:
                continue

            prs_merged = 0
            verified_merged = 0
            proven_merged = 0
            consumed_merged = 0
            prs_rejected = 0
            tasks_created_total = 0
            pr_ineligible_total = 0
            sd_totals: Dict[str, int] = {}
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = _coerce_naive_utc(
                        datetime.fromisoformat(entry.get("timestamp", ""))
                    )
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        verified_merged += verified_merged_count(entry)
                        proven_merged += proven_merged_count(entry)
                        consumed_merged += consumed_merged_count(entry)
                        prs_rejected += entry.get("prs_rejected") or 0
                        tasks_created_total += entry.get("tasks_created") or 0
                        pr_ineligible_total += pr_ineligible_created_in_entry(entry)
                        for k, v in (entry.get("stage_dropout") or {}).items():
                            sd_totals[k] = sd_totals.get(k, 0) + (v or 0)
                except (ValueError, TypeError):
                    pass

            # Conversion is verification-aware (verified_merged) and uses PR-eligible
            # denominator (excludes non-PR task types) to agree with pipeline-level rate.
            pr_eligible_total = tasks_created_total - pr_ineligible_total
            dir_conversion_rate = (
                round(verified_merged / pr_eligible_total, 3)
                if pr_eligible_total > 0
                else None
            )
            acceptance_denom = prs_merged + prs_rejected
            acceptance_rate = (
                round(prs_merged / acceptance_denom, 3)
                if acceptance_denom > 0
                else None
            )
            dominant_dropout = (
                max(sd_totals, key=lambda k: sd_totals[k])
                if sd_totals and max(sd_totals.values()) > 0
                else None
            )
            direction_stats.append(
                {
                    "direction_id": did,
                    "cycles": cycles,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "conversion_rate": dir_conversion_rate,
                    "dominant_dropout_stage": dominant_dropout,
                    "completion_criteria": d.get("completion_criteria"),
                    "possibly_complete": bool(d.get("possibly_complete", False)),
                    "category": d.get("category"),
                    "acceptance_rate": acceptance_rate,
                    "verified_prs_merged": verified_merged,
                    "proven_prs_merged": proven_merged,
                    "consumed_prs_merged": consumed_merged,
                }
            )

        direction_stats.sort(key=lambda x: x["cycles"], reverse=True)

    # Pipeline-level conversion rate: total prs_merged / total PR-eligible tasks
    # created across all directions. The denominator excludes PR-ineligible task
    # types (research/audit/memo/investigate) so it is not diluted by non-PR work.
    _pipeline_total_created = sum(
        entry.get("tasks_created") or 0
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    _pipeline_total_ineligible = sum(
        pr_ineligible_created_in_entry(entry)
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    _pipeline_total_merged = sum(
        verified_merged_count(entry)
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    _pipeline_eligible_created = _pipeline_total_created - _pipeline_total_ineligible
    pipeline_conversion_rate = (
        round(_pipeline_total_merged / _pipeline_eligible_created, 3)
        if _pipeline_eligible_created > 0
        else None
    )

    # First-attempt success rate: tasks that completed on the first try
    terminal_tasks_24h = completed_tasks + failed_tasks
    by_type: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)

    def _first_attempt_rate(tasks):
        if not tasks:
            return None
        # Only count tasks that have the metric recorded
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(
            1 for t in measured if t.context["_first_attempt_succeeded"]
        )
        return round(first_success / len(measured), 2)

    def _verified_first_attempt_rate(tasks):
        """Like _first_attempt_rate but excludes degraded-unverified merges.

        A task with _first_attempt_succeeded=True whose PR fell open for a
        degraded reason (environment/setup failure blocked verification) is NOT
        a proven fix — exclude it from the verified count.  Benign fall-opens
        (no suite, kill-switch off) still count as successes.
        """
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(
            1
            for t in measured
            if t.context["_first_attempt_succeeded"]
            and merge_verification_status(t) != "unverified_degraded"
        )
        return round(first_success / len(measured), 2)

    _review_eligible_types = {"create", "refactor"}

    def _first_review_approval_rate(tasks):
        """Fraction of review-eligible merged tasks that had no first-review rejection.

        A task is 'review-eligible' if it is a create/refactor task that
        landed on the integration branch.
        A task 'approved on first review' if it does NOT have _first_attempt_rejection_categories
        in its context (meaning the first review returned APPROVE, not REQUEST_CHANGES).
        """
        eligible = [
            t
            for t in tasks
            if (t.first_payload or {}).get("type", "create") in _review_eligible_types
            and is_merged_completion(t)
        ]
        if not eligible:
            return None
        approved = sum(
            1
            for t in eligible
            if "_first_attempt_rejection_categories" not in (t.context or {})
        )
        return round(approved / len(eligible), 2)

    tasks_with_playbook = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_playbook = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    _arch_eligible = [
        t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_with_arch_context = [
        t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is True
    ]
    tasks_without_arch_context = [
        t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is False
    ]

    tasks_with_review_measurements = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_with_measurements = tasks_with_review_measurements
    tasks_without_review_measurements = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_measurements = [
        t
        for t in terminal_tasks_24h
        if not (t.context or {}).get("_had_review_measurements")
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    first_attempt_by_type = {}
    for task_type, tasks in by_type.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_type[task_type] = rate

    by_scope: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        scope = (t.context or {}).get("scope", "unknown")
        by_scope.setdefault(scope, []).append(t)

    first_attempt_by_scope = {}
    for scope, tasks in by_scope.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_scope[scope] = rate

    # Scope merge rates: per-scope merged/created from research_history scope_breakdown
    _scope_cur: Dict[str, Dict[str, int]] = {}
    _scope_prev: Dict[str, Dict[str, int]] = {}
    for d in project.directions or [] if project else []:
        for entry in d.get("research_history") or []:
            ts_str = entry.get("timestamp")
            sb = entry.get("scope_breakdown") or {}
            if not sb:
                continue
            in_cur = _safe_ts_in_window(ts_str, window_start)
            in_prev = not in_cur and _safe_ts_in_window(ts_str, prev_window_start)
            target = _scope_cur if in_cur else (_scope_prev if in_prev else None)
            if target is None:
                continue
            for scope, counts in sb.items():
                if scope not in target:
                    target[scope] = {
                        "created": 0,
                        "merged": 0,
                        "calibrated_in": 0,
                        "calibrated_out": 0,
                    }
                target[scope]["created"] += counts.get("created") or 0
                target[scope]["merged"] += counts.get("merged") or 0
                target[scope]["calibrated_in"] += counts.get("calibrated_in") or 0
                target[scope]["calibrated_out"] += counts.get("calibrated_out") or 0

    all_scopes = set(_scope_cur) | set(_scope_prev)
    scope_conversion_rates: Dict[str, Any] = {}
    for scope in all_scopes:
        cur = _scope_cur.get(scope, {})
        prev = _scope_prev.get(scope, {})
        cur_created = cur.get("created", 0)
        prev_created = prev.get("created", 0)
        cur_rate = (
            round(cur.get("merged", 0) / cur_created, 3) if cur_created > 0 else None
        )
        prev_rate = (
            round(prev.get("merged", 0) / prev_created, 3) if prev_created > 0 else None
        )
        delta = (
            round(cur_rate - prev_rate, 3)
            if (cur_rate is not None and prev_rate is not None)
            else None
        )
        scope_trend: str | None = None
        if delta is not None:
            if delta > 0.05:
                scope_trend = "improving"
            elif delta < -0.05:
                scope_trend = "declining"
            else:
                scope_trend = "stable"
        scope_conversion_rates[scope] = {
            "current_rate": cur_rate,
            "prev_rate": prev_rate,
            "delta": delta,
            "trend": scope_trend,
            "created": cur_created,
            "merged": cur.get("merged", 0),
            "calibrated_in": cur.get("calibrated_in", 0),
            "calibrated_out": cur.get("calibrated_out", 0),
        }

    # Medium-scope review iterations: average iteration count for medium-scope terminal tasks
    medium_scope_tasks_with_loop = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("scope") == "medium"
        and (t.context or {}).get("review_loop") is not None
    ]
    medium_scope_review_iterations: float | None = None
    if medium_scope_tasks_with_loop:
        total_iters = sum(
            (t.context or {}).get("review_loop", {}).get("iteration", 0)
            for t in medium_scope_tasks_with_loop
        )
        medium_scope_review_iterations = round(
            total_iters / len(medium_scope_tasks_with_loop), 2
        )

    current_first_attempt_rate = _first_attempt_rate(terminal_tasks_24h)

    # Previous window for first_attempt trend comparison
    prev_terminal_tasks = prev_completed + prev_failed
    prev_first_attempt_rate = _first_attempt_rate(prev_terminal_tasks)

    first_attempt_trend: str | None = None
    if current_first_attempt_rate is not None and prev_first_attempt_rate is not None:
        fa_delta = current_first_attempt_rate - prev_first_attempt_rate
        if fa_delta > 0.05:
            first_attempt_trend = "improving"
        elif fa_delta < -0.05:
            first_attempt_trend = "declining"
        else:
            first_attempt_trend = "stable"

    # First-attempt failures by review category: which categories caused the most
    # first-attempt rejections, as a fraction of tasks that had their first review rejected.
    first_attempt_by_review_category: Dict[str, float] | None = None
    first_attempt_failed_with_cats = [
        t
        for t in terminal_tasks_24h
        if "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if first_attempt_failed_with_cats:
        total_with_cats = len(first_attempt_failed_with_cats)
        cat_counts: Dict[str, int] = {}
        for t in first_attempt_failed_with_cats:
            cats = (t.context or {}).get("_first_attempt_rejection_categories", {})
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        first_attempt_by_review_category = {
            cat: round(count / total_with_cats, 2)
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
        }

    def _arch_rejection_rate(tasks: list) -> float | None:
        failed = [
            t
            for t in tasks
            if "_first_attempt_rejection_categories" in (t.context or {})
        ]
        if not failed:
            return None
        arch = sum(
            1
            for t in failed
            if "architecture"
            in (t.context or {}).get("_first_attempt_rejection_categories", {})
        )
        return round(arch / len(failed), 2)

    first_attempt_success_rate = {
        "overall": current_first_attempt_rate,
        "trend": first_attempt_trend,
        "with_playbook": _first_attempt_rate(tasks_with_playbook)
        if tasks_with_playbook
        else None,
        "without_playbook": _first_attempt_rate(tasks_without_playbook)
        if tasks_without_playbook
        else None,
        "with_arch_context": _first_attempt_rate(tasks_with_arch_context)
        if tasks_with_arch_context
        else None,
        "without_arch_context": _first_attempt_rate(tasks_without_arch_context)
        if tasks_without_arch_context
        else None,
        "arch_rejection_rate_with_context": _arch_rejection_rate(
            tasks_with_arch_context
        ),
        "arch_rejection_rate_without_context": _arch_rejection_rate(
            tasks_without_arch_context
        ),
        "with_review_measurements": _first_attempt_rate(tasks_with_review_measurements)
        if tasks_with_review_measurements
        else None,
        "without_review_measurements": _first_attempt_rate(
            tasks_without_review_measurements
        )
        if tasks_without_review_measurements
        else None,
        "sample_size": len(
            [
                t
                for t in terminal_tasks_24h
                if "_first_attempt_succeeded" in (t.context or {})
            ]
        ),
        "by_type": first_attempt_by_type,
        "by_scope": first_attempt_by_scope,
        "first_attempt_by_review_category": first_attempt_by_review_category,
        "with_measurements": _first_attempt_rate(tasks_with_measurements)
        if tasks_with_measurements
        else None,
        "without_measurements": _first_attempt_rate(tasks_without_measurements)
        if tasks_without_measurements
        else None,
        # verified_overall: excludes degraded-unverified merges (environment/setup
        # failures that blocked real test verification). Aligned with the
        # merge-gate-health verified-vs-fell-open split so strategic scoring is
        # not misled by unproven fixes. Additive — does not replace "overall".
        "verified_overall": _verified_first_attempt_rate(terminal_tasks_24h),
    }

    # First-review approval rate: of create/refactor tasks that merged, what fraction
    # were approved on the first review (i.e., no _first_attempt_rejection_categories)?
    # This is the true measure of verification gate effectiveness.
    _fra_by_type: Dict[str, float] = {}
    for task_type, tasks in by_type.items():
        rate = _first_review_approval_rate(tasks)
        if rate is not None:
            _fra_by_type[task_type] = rate

    _fra_by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope.items():
        rate = _first_review_approval_rate(tasks)
        if rate is not None:
            _fra_by_scope[scope] = rate

    _review_eligible_merged = [
        t
        for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type", "create") in _review_eligible_types
        and is_merged_completion(t)
    ]
    first_review_approval_rate = {
        "overall": _first_review_approval_rate(terminal_tasks_24h),
        "sample_size": len(_review_eligible_merged),
        "by_type": _fra_by_type,
        "by_scope": _fra_by_scope,
        "with_playbook": _first_review_approval_rate(tasks_with_playbook),
        "without_playbook": _first_review_approval_rate(tasks_without_playbook),
        "with_measurements": _first_review_approval_rate(tasks_with_measurements),
        "without_measurements": _first_review_approval_rate(tasks_without_measurements),
    }

    # Self-review effectiveness: correlate self-review verdicts with first-attempt approval outcomes
    tasks_with_self_review = [
        t for t in terminal_tasks_24h if "_self_review_outcome" in (t.context or {})
    ]
    tasks_without_self_review = [
        t for t in terminal_tasks_24h if "_self_review_outcome" not in (t.context or {})
    ]
    tasks_self_review_clean = [
        t
        for t in tasks_with_self_review
        if not (t.context or {})
        .get("_self_review_outcome", {})
        .get("fixes_applied", False)
    ]
    tasks_self_review_fixed = [
        t
        for t in tasks_with_self_review
        if (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]

    self_review_effectiveness: Dict[str, Any] | None = None
    if len(tasks_with_self_review) >= 3:
        # Category-level correlation: for each check category, compare first-attempt rate
        # between tasks where that category flagged issues vs tasks where it passed clean.
        all_categories: set = set()
        for t in tasks_with_self_review:
            check_results = safe_check_results(
                (t.context or {}).get("_self_review_outcome", {})
            )
            all_categories.update(check_results.keys())

        category_correlation: Dict[str, Any] = {}
        for cat in all_categories:
            tasks_cat_findings = [
                t
                for t in tasks_with_self_review
                if not safe_check_results(
                    (t.context or {}).get("_self_review_outcome", {})
                )
                .get(cat, {})
                .get("passed", True)
            ]
            tasks_cat_clean = [
                t
                for t in tasks_with_self_review
                if cat
                in safe_check_results(
                    (t.context or {}).get("_self_review_outcome", {})
                )
                and safe_check_results(
                    (t.context or {}).get("_self_review_outcome", {})
                )
                .get(cat, {})
                .get("passed", True)
            ]
            cat_findings_rate = (
                _first_attempt_rate(tasks_cat_findings) if tasks_cat_findings else None
            )
            cat_clean_rate = (
                _first_attempt_rate(tasks_cat_clean) if tasks_cat_clean else None
            )
            if cat_findings_rate is not None or cat_clean_rate is not None:
                category_correlation[cat] = {
                    "with_findings_approval_rate": cat_findings_rate,
                    "clean_approval_rate": cat_clean_rate,
                }

        self_review_effectiveness = {
            "with_self_review_approval_rate": _first_attempt_rate(
                tasks_with_self_review
            ),
            "without_self_review_approval_rate": _first_attempt_rate(
                tasks_without_self_review
            ),
            "clean_verdict_approval_rate": _first_attempt_rate(tasks_self_review_clean),
            "fixed_verdict_approval_rate": _first_attempt_rate(tasks_self_review_fixed),
            "sample_size": len(tasks_with_self_review),
            "sr_clean_count": len(tasks_self_review_clean),
            "sr_fixed_count": len(tasks_self_review_fixed),
            "category_correlation": category_correlation,
        }

    # Review accuracy: precision and recall from user feedback signals on completed review tasks
    review_tasks = [
        t
        for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type") == "review"
        and (t.context or {}).get("blueprint_review_verdict") is not None
    ]
    review_accuracy: Dict[str, Any] | None = None
    if len(review_tasks) >= 5:
        total_reviews = len(review_tasks)
        overrides = sum(
            1 for t in review_tasks if (t.context or {}).get("user_overrode_review")
        )
        dismissals = sum(
            1 for t in review_tasks if (t.context or {}).get("review_dismissed")
        )
        total_approvals = sum(
            1
            for t in review_tasks
            if (t.context or {}).get("blueprint_review_verdict") == "APPROVE"
        )
        misses = sum(
            1
            for t in review_tasks
            if (t.context or {}).get("user_caught_issues")
            and (t.context or {}).get("blueprint_review_verdict") == "APPROVE"
        )
        precision = round(1 - (overrides + dismissals) / total_reviews, 2)
        recall = round(1 - misses / total_approvals, 2) if total_approvals > 0 else None
        review_accuracy = {
            "total_reviews": total_reviews,
            "overrides": overrides,
            "dismissals": dismissals,
            "misses": misses,
            "total_approvals": total_approvals,
            "precision": precision,
            "recall": recall,
        }

    # Persistent review issues and full review effectiveness trends from
    # compute_review_effectiveness stored in project.design_doc.
    persistent_review_issues: Optional[List[str]] = None
    review_effectiveness_trends: Optional[Dict[str, Any]] = None
    review_recurrence_by_category: Optional[Dict[str, float]] = None
    project = await session.get(Project, project_id)
    if project and project.design_doc:
        eff = project.design_doc.get("review_effectiveness") or {}
        issues = eff.get("persistent_issues") or []
        if issues:
            persistent_review_issues = issues
        if eff:
            review_effectiveness_trends = {
                "computed_at": eff.get("computed_at"),
                "persistent_issues": eff.get("persistent_issues") or [],
                "categories": eff.get("categories") or {},
            }
        cats = eff.get("categories") or {}
        recurrence: Dict[str, float] = {}
        for cat, cat_data in cats.items():
            rate = cat_data.get("recurrence_rate")
            if rate is not None:
                recurrence[cat] = rate
        if recurrence:
            review_recurrence_by_category = recurrence

    # SR analytics live in their dedicated row. Generic project telemetry is
    # still used for direction-first-attempt feedback only.
    _sr_data: Dict[str, Any] = {}
    _tel_blobs: Dict[str, Any] = {}
    if project:
        _sr_data = await read_project_sr_analytics(session, project)
        _tel_blobs = await read_telemetry_many(
            session, project_id, ["direction_first_attempt_feedback"]
        )
    _design_doc: Dict[str, Any] = (project.design_doc or {}) if project else {}

    def _generic_telemetry_blob(key: str) -> Any:
        blob = _tel_blobs.get(key)
        return blob if blob is not None else _design_doc.get(key)

    # SR catch-rate trends from compute_sr_catch_rate_trends.
    self_review_prediction_accuracy_trends: Optional[Dict[str, Any]] = None
    sr_regression_risk_test_signal_trends: Optional[Dict[str, Any]] = None
    sr_false_attestation_trends: Optional[Dict[str, Any]] = None
    if project:
        sr_trends = _sr_data.get("sr_catch_rate_trends") or {}
        if sr_trends:
            self_review_prediction_accuracy_trends = _serialize_sr_catch_rate_trends(
                sr_trends
            )
        sr_rrts = _sr_data.get("sr_regression_risk_test_signal_trends")
        if sr_rrts:
            sr_regression_risk_test_signal_trends = sr_rrts
        sft = _sr_data.get("sr_false_attestation_trends")
        if sft:
            sr_false_attestation_trends = sft

    # Fresh-reviewer cycle-over-cycle trend from compute_fresh_reviewer_trend.
    fresh_reviewer_trend: Optional[Dict[str, Any]] = None
    if project:
        fr_trends = _sr_data.get("fresh_reviewer_trend") or {}
        if fr_trends:
            fresh_reviewer_trend = {
                "computed_at": fr_trends.get("computed_at"),
                "bucket_mode": fr_trends.get("bucket_mode"),
                "current_cycle_id": fr_trends.get("current_cycle_id"),
                "previous_cycle_id": fr_trends.get("previous_cycle_id"),
                "current_sample_size": fr_trends.get("current_sample_size"),
                "previous_sample_size": fr_trends.get("previous_sample_size"),
                "per_category_hit_rate": fr_trends.get("per_category_hit_rate") or {},
                "added_value": fr_trends.get("added_value") or {},
                "approval_rate_lift": fr_trends.get("approval_rate_lift") or {},
            }

    # Edges-considered cycle-over-cycle trend from compute_edges_trend.
    edges_trend: Optional[Dict[str, Any]] = None
    if project:
        et = _sr_data.get("edges_trend") or {}
        if et:
            edges_trend = {
                "computed_at": et.get("computed_at"),
                "bucket_mode": et.get("bucket_mode"),
                "current_cycle_id": et.get("current_cycle_id"),
                "previous_cycle_id": et.get("previous_cycle_id"),
                "current": et.get("current") or {},
                "previous": et.get("previous") or {},
                "tasks_with_edges_rate_delta": et.get("tasks_with_edges_rate_delta"),
                "trend": et.get("trend"),
                "rate_history": et.get("rate_history") or [],
            }

    direction_first_attempt_feedback: Optional[Dict[str, Any]] = None
    if project:
        dfa = _generic_telemetry_blob("direction_first_attempt_feedback")
        if dfa:
            direction_first_attempt_feedback = dfa

    # Idle auto-playbook retirements in the last 24h — surfaces accidental
    # over-retirement of auto-generated entries whose last_referenced_at
    # crossed the idle threshold (see playbook_learning.retire_idle_auto_entries).
    auto_playbook_idle_retired_24h: Optional[int] = None
    auto_playbook_low_success_retired_24h: Optional[int] = None
    if project and project.design_doc:
        from app.playbook_storage import (
            count_idle_auto_retired_within,
            count_low_success_retired_within,
        )

        auto_playbook_idle_retired_24h = count_idle_auto_retired_within(
            project.design_doc, hours=24
        )
        auto_playbook_low_success_retired_24h = count_low_success_retired_within(
            project.design_doc, hours=24
        )

    # Self-review prediction accuracy: compare self-review check_results
    # against reviewer-confirmed rejection categories on ANY iteration (shared
    # has_reviewer_confirmed_categories predicate — matches the scorer's ground
    # truth so later-iteration-only confirmations are sampled, not dropped).
    tasks_with_both_24h = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and has_reviewer_confirmed_categories(t.context or {})
    ]
    self_review_prediction_accuracy = compute_sr_prediction_accuracy(
        tasks_with_both_24h
    )
    edges_considered_metrics = _compute_edges_considered_metrics(terminal_tasks_24h)

    # Medium-scope pre/post investment measurement — computed over a longer lookback
    # so research agents see the actual before/after merge rates straight from the prompt
    # instead of hitting /medium-scope-measurement (admin-auth only; agent tokens 401).
    medium_scope_measurement: Dict[str, Any] | None = None
    try:
        ms_cutoff = _now - timedelta(days=365)
        ms_result = await session.execute(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                Task.created_at >= ms_cutoff,
                Task.context["scope"].as_string() == "medium",
            )
            .order_by(Task.created_at.desc())
            .limit(500)
        )
        ms_medium = ms_result.scalars().all()
        if ms_medium:
            from app.routers.admin_scope_measurement import (
                _compute_medium_scope_measurement,
            )  # noqa: PLC0415

            full = _compute_medium_scope_measurement(ms_medium, project, None, 365)
            medium_scope_measurement = {
                "medium_scope_conversion_rate": full.get(
                    "medium_scope_conversion_rate"
                ),
                "total_created": full.get("total_created"),
                "total_merged": full.get("total_merged"),
                "pre_investment": full.get("pre_investment"),
                "post_investment": full.get("post_investment"),
                "recent_30d": full.get("recent_30d"),
                "recent_7d": full.get("recent_7d"),
                "investment_start": full.get("investment_start"),
            }
    except Exception:
        logger.exception(
            "[ANALYTICS] medium_scope_measurement computation failed (non-critical)"
        )

    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415

    # Park/wake operational health (read-only) — PARKED population + wake-delivery
    # backlog for the park-and-wake primitive. Degrades to None on any error so a
    # transient query failure never sinks the whole health summary.
    park_wake_health: Dict[str, Any] | None = None
    try:
        from app.digest_metrics import compute_park_wake_health

        park_wake_health = await compute_park_wake_health(session, project_id)
    except Exception:
        logger.exception(
            "[ANALYTICS] park_wake_health computation failed (non-critical)"
        )

    return {
        "success_rate": success_rate,
        "trend": trend,
        "park_wake_health": park_wake_health,
        "failure_distribution": failure_distribution,
        "failure_taxonomy": failure_taxonomy,
        "failure_taxonomy_by_scope": failure_taxonomy_by_scope,
        "direction_stats": direction_stats,
        "first_attempt_success_rate": first_attempt_success_rate,
        "first_review_approval_rate": first_review_approval_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "self_review_prediction_accuracy_trends": self_review_prediction_accuracy_trends,
        "sr_regression_risk_test_signal_trends": sr_regression_risk_test_signal_trends,
        "sr_false_attestation_trends": sr_false_attestation_trends,
        "fresh_reviewer_trend": fresh_reviewer_trend,
        "edges_trend": edges_trend,
        "edges_considered_metrics": edges_considered_metrics,
        "review_accuracy": review_accuracy,
        "persistent_review_issues": persistent_review_issues,
        "review_effectiveness_trends": review_effectiveness_trends,
        "review_recurrence_by_category": review_recurrence_by_category,
        "scope_conversion_rates": scope_conversion_rates,
        "scope_file_budgets": SCOPE_FILE_BUDGETS,
        "medium_scope_review_iterations": medium_scope_review_iterations,
        "direction_first_attempt_feedback": direction_first_attempt_feedback,
        "medium_scope_measurement": medium_scope_measurement,
        "auto_playbook_idle_retired_24h": auto_playbook_idle_retired_24h,
        "auto_playbook_low_success_retired_24h": auto_playbook_low_success_retired_24h,
    }
