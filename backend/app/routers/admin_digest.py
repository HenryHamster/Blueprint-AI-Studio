"""Catch-up digest and pipeline health summary endpoints."""
import uuid
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task, TaskStatus, Project, Artifact, ArtifactType, _utcnow,
)
from app.auth import get_current_user, require_admin
from app.shared_state import runtime_config as _runtime_config
from app.routers.admin_analytics import _derive_completed_at, ON_DEMAND_RATE, SPOT_RATE

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-digest"],
    dependencies=[Depends(get_current_user)],
)


async def compute_pipeline_health_summary(session: AsyncSession, project_id: uuid.UUID, lookback_hours: int = 24) -> Dict[str, Any]:
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
            return datetime.fromisoformat(ts_str or "") >= cutoff
        except (ValueError, TypeError):
            return False

    # Query tasks from extended 48h window to compute both current and previous period
    tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= prev_window_start,
        )
    )
    all_tasks = tasks_result.scalars().all()

    # Post-filter by completion timestamp
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # Previous window for trend comparison
    prev_completed = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total = len(prev_completed) + len(prev_failed)
    prev_success_rate = round(len(prev_completed) / prev_total, 2) if prev_total > 0 else None

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
        TAXONOMY_REVIEW_QUALITY, TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
    )
    _taxonomy_keys = (
        TAXONOMY_SCOPE_RELATED, TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED, TAXONOMY_CODE_DEFECT,
    )
    taxonomy_counts: Dict[str, int] = {k: 0 for k in _taxonomy_keys}
    for t in failed_tasks:
        taxonomy_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(taxonomy_counts.values())
    failure_taxonomy = {
        "categories": {
            k: {
                "count": taxonomy_counts[k],
                "percentage": round(taxonomy_counts[k] / _tax_total * 100, 1) if _tax_total else 0.0,
            }
            for k in _taxonomy_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_taxonomy_keys, key=lambda k: taxonomy_counts[k]) if _tax_total else None,
    }

    # Direction stats: conversion_rate and cycle count per active direction
    direction_stats: List[Dict[str, Any]] = []
    project = await session.get(Project, project_id)
    if project:
        active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

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
                1 for t in tasks_for_dir
                if t.status == TaskStatus.COMPLETED
                and (t.first_payload).get("type", "create") == "research"
                and _derive_completed_at(t) >= window_start
            )

            if cycles == 0:
                continue

            prs_merged = 0
            tasks_created_total = 0
            sd_totals: Dict[str, int] = {}
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        tasks_created_total += entry.get("tasks_created") or 0
                        for k, v in (entry.get("stage_dropout") or {}).items():
                            sd_totals[k] = sd_totals.get(k, 0) + (v or 0)
                except (ValueError, TypeError):
                    pass

            dir_conversion_rate = round(prs_merged / tasks_created_total, 3) if tasks_created_total > 0 else None
            dominant_dropout = (
                max(sd_totals, key=lambda k: sd_totals[k])
                if sd_totals and max(sd_totals.values()) > 0
                else None
            )
            direction_stats.append({
                "direction_id": did,
                "cycles": cycles,
                "possibly_saturated": bool(d.get("possibly_saturated", False)),
                "conversion_rate": dir_conversion_rate,
                "dominant_dropout_stage": dominant_dropout,
            })

        direction_stats.sort(key=lambda x: x["cycles"], reverse=True)

    # Pipeline-level conversion rate: total prs_merged / total tasks_created across all directions
    _pipeline_total_created = sum(
        entry.get("tasks_created") or 0
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    _pipeline_total_merged = sum(
        entry.get("prs_merged") or 0
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    pipeline_conversion_rate = round(_pipeline_total_merged / _pipeline_total_created, 3) if _pipeline_total_created > 0 else None

    # First-attempt success rate: tasks that completed on the first try
    terminal_tasks_24h = completed_tasks + failed_tasks
    by_type: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)

    def _first_attempt_rate(tasks):
        if not tasks:
            return None
        first_success = sum(
            1 for t in tasks
            if (t.context or {}).get("_first_attempt_succeeded") is True
        )
        # Only count tasks that have the metric recorded
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(first_success / len(measured), 2)

    _review_eligible_types = {"create", "refactor"}

    def _first_review_approval_rate(tasks):
        """Fraction of review-eligible merged tasks that had no first-review rejection.

        A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
        A task 'approved on first review' if it does NOT have _first_attempt_rejection_categories
        in its context (meaning the first review returned APPROVE, not REQUEST_CHANGES).
        """
        eligible = [
            t for t in tasks
            if (t.first_payload or {}).get("type", "create") in _review_eligible_types
            and (t.context or {}).get("pr_merged") is True
        ]
        if not eligible:
            return None
        approved = sum(
            1 for t in eligible
            if "_first_attempt_rejection_categories" not in (t.context or {})
        )
        return round(approved / len(eligible), 2)

    tasks_with_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    _arch_eligible = [t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]
    tasks_with_arch_context = [t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is True]
    tasks_without_arch_context = [t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is False]

    tasks_with_review_measurements = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_with_measurements = tasks_with_review_measurements
    tasks_without_review_measurements = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_measurements = [
        t for t in terminal_tasks_24h
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
    for d in (project.directions or [] if project else []):
        for entry in (d.get("research_history") or []):
            ts_str = entry.get("timestamp")
            sb = entry.get("scope_breakdown") or {}
            if not sb:
                continue
            in_cur = _safe_ts_in_window(ts_str, window_start)
            in_prev = (
                not in_cur
                and _safe_ts_in_window(ts_str, prev_window_start)
            )
            target = _scope_cur if in_cur else (_scope_prev if in_prev else None)
            if target is None:
                continue
            for scope, counts in sb.items():
                if scope not in target:
                    target[scope] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
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
        cur_rate = round(cur.get("merged", 0) / cur_created, 3) if cur_created > 0 else None
        prev_rate = round(prev.get("merged", 0) / prev_created, 3) if prev_created > 0 else None
        delta = round(cur_rate - prev_rate, 3) if (cur_rate is not None and prev_rate is not None) else None
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
        t for t in terminal_tasks_24h
        if (t.context or {}).get("scope") == "medium"
        and (t.context or {}).get("review_loop") is not None
    ]
    medium_scope_review_iterations: float | None = None
    if medium_scope_tasks_with_loop:
        total_iters = sum(
            (t.context or {}).get("review_loop", {}).get("iteration", 0)
            for t in medium_scope_tasks_with_loop
        )
        medium_scope_review_iterations = round(total_iters / len(medium_scope_tasks_with_loop), 2)

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
        t for t in terminal_tasks_24h
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
        failed = [t for t in tasks if "_first_attempt_rejection_categories" in (t.context or {})]
        if not failed:
            return None
        arch = sum(1 for t in failed if "architecture" in (t.context or {}).get("_first_attempt_rejection_categories", {}))
        return round(arch / len(failed), 2)

    first_attempt_success_rate = {
        "overall": current_first_attempt_rate,
        "trend": first_attempt_trend,
        "with_playbook": _first_attempt_rate(tasks_with_playbook) if tasks_with_playbook else None,
        "without_playbook": _first_attempt_rate(tasks_without_playbook) if tasks_without_playbook else None,
        "with_arch_context": _first_attempt_rate(tasks_with_arch_context) if tasks_with_arch_context else None,
        "without_arch_context": _first_attempt_rate(tasks_without_arch_context) if tasks_without_arch_context else None,
        "arch_rejection_rate_with_context": _arch_rejection_rate(tasks_with_arch_context),
        "arch_rejection_rate_without_context": _arch_rejection_rate(tasks_without_arch_context),
        "with_review_measurements": _first_attempt_rate(tasks_with_review_measurements) if tasks_with_review_measurements else None,
        "without_review_measurements": _first_attempt_rate(tasks_without_review_measurements) if tasks_without_review_measurements else None,
        "sample_size": len([t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]),
        "by_type": first_attempt_by_type,
        "by_scope": first_attempt_by_scope,
        "first_attempt_by_review_category": first_attempt_by_review_category,
        "with_measurements": _first_attempt_rate(tasks_with_measurements) if tasks_with_measurements else None,
        "without_measurements": _first_attempt_rate(tasks_without_measurements) if tasks_without_measurements else None,
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
        t for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type", "create") in _review_eligible_types
        and (t.context or {}).get("pr_merged") is True
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
        t for t in terminal_tasks_24h
        if "_self_review_outcome" in (t.context or {})
    ]
    tasks_without_self_review = [
        t for t in terminal_tasks_24h
        if "_self_review_outcome" not in (t.context or {})
    ]
    tasks_self_review_clean = [
        t for t in tasks_with_self_review
        if not (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]
    tasks_self_review_fixed = [
        t for t in tasks_with_self_review
        if (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]

    self_review_effectiveness: Dict[str, Any] | None = None
    if len(tasks_with_self_review) >= 3:
        # Category-level correlation: for each check category, compare first-attempt rate
        # between tasks where that category flagged issues vs tasks where it passed clean.
        all_categories: set = set()
        for t in tasks_with_self_review:
            check_results = (t.context or {}).get("_self_review_outcome", {}).get("check_results") or {}
            all_categories.update(check_results.keys())

        category_correlation: Dict[str, Any] = {}
        for cat in all_categories:
            tasks_cat_findings = [
                t for t in tasks_with_self_review
                if not (t.context or {}).get("_self_review_outcome", {}).get("check_results", {}).get(cat, {}).get("passed", True)
            ]
            tasks_cat_clean = [
                t for t in tasks_with_self_review
                if cat in ((t.context or {}).get("_self_review_outcome", {}).get("check_results") or {})
                and (t.context or {}).get("_self_review_outcome", {}).get("check_results", {}).get(cat, {}).get("passed", True)
            ]
            cat_findings_rate = _first_attempt_rate(tasks_cat_findings) if tasks_cat_findings else None
            cat_clean_rate = _first_attempt_rate(tasks_cat_clean) if tasks_cat_clean else None
            if cat_findings_rate is not None or cat_clean_rate is not None:
                category_correlation[cat] = {
                    "with_findings_approval_rate": cat_findings_rate,
                    "clean_approval_rate": cat_clean_rate,
                }

        self_review_effectiveness = {
            "with_self_review_approval_rate": _first_attempt_rate(tasks_with_self_review),
            "without_self_review_approval_rate": _first_attempt_rate(tasks_without_self_review),
            "clean_verdict_approval_rate": _first_attempt_rate(tasks_self_review_clean),
            "fixed_verdict_approval_rate": _first_attempt_rate(tasks_self_review_fixed),
            "sample_size": len(tasks_with_self_review),
            "category_correlation": category_correlation,
        }

    # Review accuracy: precision and recall from user feedback signals on completed review tasks
    review_tasks = [
        t for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type") == "review"
        and (t.context or {}).get("blueprint_review_verdict") is not None
    ]
    review_accuracy: Dict[str, Any] | None = None
    if len(review_tasks) >= 5:
        total_reviews = len(review_tasks)
        overrides = sum(1 for t in review_tasks if (t.context or {}).get("user_overrode_review"))
        dismissals = sum(1 for t in review_tasks if (t.context or {}).get("review_dismissed"))
        misses = sum(1 for t in review_tasks if (t.context or {}).get("user_caught_issues"))
        total_approvals = sum(
            1 for t in review_tasks
            if (t.context or {}).get("blueprint_review_verdict") == "APPROVE"
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

    direction_first_attempt_feedback: Optional[Dict[str, Any]] = None
    if project and project.design_doc:
        dfa = project.design_doc.get("direction_first_attempt_feedback")
        if dfa:
            direction_first_attempt_feedback = dfa

    # Self-review prediction accuracy: compare self-review check_results against human rejection categories
    self_review_prediction_accuracy: Dict[str, Any] | None = None
    tasks_with_both = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if len(tasks_with_both) >= 5:
        total_human_cats = 0
        total_overlap = 0
        total_missed = 0
        total_false_alarms = 0
        total_sr_failed_cats = 0
        for t in tasks_with_both:
            ctx = t.context or {}
            check_results = ctx.get("_self_review_outcome", {}).get("check_results") or {}
            human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
            sr_failed = {cat for cat, res in check_results.items() if not res.get("passed", True)}
            overlap = human_cats & sr_failed
            missed = human_cats - sr_failed
            false_alarms = sr_failed - human_cats
            total_human_cats += len(human_cats)
            total_overlap += len(overlap)
            total_missed += len(missed)
            total_false_alarms += len(false_alarms)
            total_sr_failed_cats += len(sr_failed)
        self_review_prediction_accuracy = {
            "prediction_hit_rate": round(total_overlap / total_human_cats, 3) if total_human_cats > 0 else None,
            "miss_rate": round(total_missed / total_human_cats, 3) if total_human_cats > 0 else None,
            "false_alarm_rate": round(total_false_alarms / total_sr_failed_cats, 3) if total_sr_failed_cats > 0 else None,
            "sample_size": len(tasks_with_both),
        }

    # Medium-scope pre/post investment measurement — computed over a longer lookback
    # so research agents see the actual before/after merge rates straight from the prompt
    # instead of hitting /medium-scope-measurement (admin-auth only; agent tokens 401).
    medium_scope_measurement: Dict[str, Any] | None = None
    try:
        ms_cutoff = _now - timedelta(days=365)
        ms_result = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
                Task.created_at >= ms_cutoff,
            )
        )
        ms_all = ms_result.scalars().all()
        ms_medium = [t for t in ms_all if (t.context or {}).get("scope") == "medium"]
        if ms_medium:
            from app.routers.admin_scope_measurement import _compute_medium_scope_measurement  # noqa: PLC0415
            full = _compute_medium_scope_measurement(ms_medium, project, None, 365)
            medium_scope_measurement = {
                "medium_scope_conversion_rate": full.get("medium_scope_conversion_rate"),
                "total_created": full.get("total_created"),
                "total_merged": full.get("total_merged"),
                "pre_investment": full.get("pre_investment"),
                "post_investment": full.get("post_investment"),
                "recent_30d": full.get("recent_30d"),
                "recent_7d": full.get("recent_7d"),
                "investment_start": full.get("investment_start"),
            }
    except Exception:
        logger.exception("[ANALYTICS] medium_scope_measurement computation failed (non-critical)")

    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415

    return {
        "success_rate": success_rate,
        "trend": trend,
        "failure_distribution": failure_distribution,
        "failure_taxonomy": failure_taxonomy,
        "direction_stats": direction_stats,
        "first_attempt_success_rate": first_attempt_success_rate,
        "first_review_approval_rate": first_review_approval_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "review_accuracy": review_accuracy,
        "persistent_review_issues": persistent_review_issues,
        "review_effectiveness_trends": review_effectiveness_trends,
        "review_recurrence_by_category": review_recurrence_by_category,
        "scope_conversion_rates": scope_conversion_rates,
        "scope_file_budgets": SCOPE_FILE_BUDGETS,
        "medium_scope_review_iterations": medium_scope_review_iterations,
        "direction_first_attempt_feedback": direction_first_attempt_feedback,
        "medium_scope_measurement": medium_scope_measurement,
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Return a catch-up digest of activity within the requested time window."""
    window_start = _utcnow() - timedelta(hours=hours)
    prev_window_start = window_start - timedelta(hours=hours)
    # Extend lookback to capture long-running tasks and 7 periods for trend history
    raw_lookback = _utcnow() - timedelta(hours=min(hours * 7, 24 * 14))
    lookback_start = min(raw_lookback, prev_window_start)

    # --- Query tasks from extended lookback window ---
    # Load terminal tasks (COMPLETED/FAILED) for digest stats, plus any task
    # with requires_approval=True so we can surface pending-approval items in
    # needs_attention even while they are still IN_PROGRESS/QUEUED/etc.
    # QUEUED/PROVISIONING/IN_PROGRESS without approval flags are excluded here
    # to keep JSONB payload memory bounded; active counts come from a separate
    # lean query below.
    task_filters = [
        Task.created_at >= lookback_start,
        or_(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.requires_approval.is_(True),
        ),
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(select(Task).where(*task_filters))
    all_tasks = tasks_result.scalars().all()

    # Post-filter completed/failed by completion timestamp, not creation time
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]
    # Approval tasks use created_at (they haven't completed yet)
    approval_tasks = [
        t for t in all_tasks
        if t.requires_approval
        and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and t.created_at >= window_start
    ]

    # --- Query active tasks (not filtered by window — they may have started before it) ---
    _active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [Task.status.in_(_active_statuses)]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_result = await session.execute(
        select(Task).where(*active_filters)
        .order_by(Task.started_at.asc().nullsfirst())
        .options(load_only(
            Task.id, Task.status, Task.title, Task.payloads,
            Task.started_at, Task.project_id,
        ))
    )
    active_tasks_all = active_result.scalars().all()

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1 for t in completed_tasks
        if (t.first_payload).get("type", "create") == "research"
    )

    # --- Direction progress (only when project_id provided) ---
    direction_progress_list: List[Dict[str, Any]] = []
    dir_id_to_text: Dict[str, str] = {}
    scope_conversion_rates: Dict[str, Dict[str, int]] = {}
    prev_scope_conversion_rates: Dict[str, Dict[str, int]] = {}

    if project_id:
        project = await session.get(Project, project_id)
        if project:
            active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

            # Build direction_id -> text lookup
            for d in active_directions:
                did = d.get("id") or d.get("direction_id")
                if did:
                    dir_id_to_text[str(did)] = d.get("text") or d.get("description") or ""

            # Build direction_id -> tasks mapping
            dir_tasks: Dict[str, List] = {}
            for t in all_tasks:
                did = (t.context or {}).get("direction_id")
                if did:
                    dir_tasks.setdefault(str(did), []).append(t)

            for d in active_directions:
                did = str(d.get("id") or d.get("direction_id") or "")
                if not did:
                    continue

                tasks_for_dir = dir_tasks.get(did, [])
                cycles_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.status == TaskStatus.COMPLETED
                    and (t.first_payload).get("type", "create") == "research"
                    and _derive_completed_at(t) >= window_start
                )
                tasks_created_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.created_at >= window_start
                )

                # Single pass over research_history: aggregate PR/scope/product/type/taxonomy
                prs_merged = 0
                prs_rejected = 0
                findings_tasks_created = 0
                merge_time_values: List[float] = []
                dir_scope_agg: Dict[str, Dict[str, int]] = {}
                prev_dir_scope_agg: Dict[str, Dict[str, int]] = {}
                window_product_ratios: List[float] = []
                agg_type_distribution: Dict[str, int] = {}
                dir_taxonomy_summary: Dict[str, int] = {}
                dir_stage_dropout: Dict[str, int] = {}
                recent_failed_titles: List[str] = []
                dir_findings_produced = 0
                dir_batch_cap_applied = 0
                dir_filter_breakdown_agg: Dict[str, int] = {}
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        prs_rejected += entry.get("prs_rejected") or 0
                        findings_tasks_created += entry.get("tasks_created") or 0
                        dir_findings_produced += entry.get("findings_produced") or 0
                        dir_batch_cap_applied += entry.get("batch_cap_applied") or 0
                        for _fk, _fv in (entry.get("filter_breakdown") or {}).items():
                            dir_filter_breakdown_agg[_fk] = dir_filter_breakdown_agg.get(_fk, 0) + (_fv or 0)
                        atm = entry.get("avg_time_to_merge_hours")
                        if atm is not None:
                            merge_time_values.append(float(atm))
                        for scope, stats in (entry.get("scope_breakdown") or {}).items():
                            agg = dir_scope_agg.setdefault(scope, {"created": 0, "merged": 0, "failed": 0, "failed_infra": 0, "failed_code_error": 0, "failed_scope_overflow": 0, "calibrated_in": 0, "calibrated_out": 0})
                            for k in ("created", "merged", "failed", "failed_infra", "failed_code_error", "failed_scope_overflow", "calibrated_in", "calibrated_out"):
                                agg[k] += stats.get(k, 0)
                        pr = entry.get("product_ratio")
                        if pr is not None:
                            window_product_ratios.append(float(pr))
                        for ttype, cnt in (entry.get("type_distribution") or {}).items():
                            agg_type_distribution[ttype] = agg_type_distribution.get(ttype, 0) + cnt
                        for tax, cnt in (entry.get("taxonomy_summary") or {}).items():
                            dir_taxonomy_summary[tax] = dir_taxonomy_summary.get(tax, 0) + cnt
                        for stage, cnt in (entry.get("stage_dropout") or {}).items():
                            dir_stage_dropout[stage] = dir_stage_dropout.get(stage, 0) + (cnt or 0)
                        for fd in (entry.get("failed_task_details") or []):
                            title = (fd or {}).get("title")
                            if title:
                                recent_failed_titles.append(title)
                    elif entry_ts >= prev_window_start:
                        for scope, stats in (entry.get("scope_breakdown") or {}).items():
                            agg = prev_dir_scope_agg.setdefault(scope, {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0})
                            for k in ("created", "merged", "calibrated_in", "calibrated_out"):
                                agg[k] += stats.get(k, 0)

                if cycles_in_window == 0 and tasks_created_in_window == 0:
                    continue

                last_entry = (d.get("research_history") or [])[-1] if d.get("research_history") else None
                latest_insights = last_entry.get("cycle_insights", []) if last_entry else []
                recent_finding_titles = last_entry.get("finding_titles", []) if last_entry else []
                latest_research_at = last_entry.get("timestamp") if last_entry else None
                dir_product_ratio = round(sum(window_product_ratios) / len(window_product_ratios), 3) if window_product_ratios else None

                dir_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in tasks_for_dir if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start)

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                total_prs = prs_merged + prs_rejected
                acceptance_rate = round(prs_merged / total_prs, 3) if total_prs > 0 else None
                avg_time_to_merge_hours = round(sum(merge_time_values) / len(merge_time_values), 2) if merge_time_values else None
                conversion_rate = round(prs_merged / findings_tasks_created, 3) if findings_tasks_created > 0 else None
                # Accumulate into project-level scope rate aggregates
                for _s, _st in dir_scope_agg.items():
                    if _s not in scope_conversion_rates:
                        scope_conversion_rates[_s] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                    scope_conversion_rates[_s]["created"] += _st["created"]
                    scope_conversion_rates[_s]["merged"] += _st["merged"]
                    scope_conversion_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    scope_conversion_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)
                for _s, _st in prev_dir_scope_agg.items():
                    if _s not in prev_scope_conversion_rates:
                        prev_scope_conversion_rates[_s] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                    prev_scope_conversion_rates[_s]["created"] += _st["created"]
                    prev_scope_conversion_rates[_s]["merged"] += _st["merged"]
                    prev_scope_conversion_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    prev_scope_conversion_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)

                filter_funnel = None
                if dir_findings_produced or dir_batch_cap_applied or dir_filter_breakdown_agg or findings_tasks_created:
                    filter_funnel = {
                        "findings_produced": dir_findings_produced,
                        "hash_dedup": dir_filter_breakdown_agg.get("hash_dedup", 0),
                        "word_overlap": dir_filter_breakdown_agg.get("word_overlap_dedup", 0),
                        "file_overlap": dir_filter_breakdown_agg.get("file_overlap_dedup", 0),
                        "type_filtered": dir_filter_breakdown_agg.get("type_filtered", 0),
                        "empty_prompt": dir_filter_breakdown_agg.get("empty_prompt", 0),
                        "batch_cap": dir_batch_cap_applied,
                        "tasks_created": findings_tasks_created,
                    }

                direction_progress_list.append({
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "prs_rejected": prs_rejected,
                    "acceptance_rate": acceptance_rate,
                    "conversion_rate": conversion_rate,
                    "avg_time_to_merge_hours": avg_time_to_merge_hours,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                    "product_ratio": dir_product_ratio,
                    "type_distribution": agg_type_distribution or None,
                    "estimated_cost_usd": round(dir_cost, 2),
                    "cost_per_merged_pr": round(dir_cost / prs_merged, 4) if prs_merged > 0 else None,
                    "scope_breakdown": dir_scope_agg or None,
                    "taxonomy_summary": dir_taxonomy_summary or None,
                    "stage_dropout": dir_stage_dropout or None,
                    "recent_failure_titles": recent_failed_titles[-3:] or None,
                    "filter_funnel": filter_funnel,
                })

            direction_progress_list.sort(key=lambda x: x["cycles_in_window"], reverse=True)

    # --- Batch artifact query ---
    all_task_ids = [t.id for t in all_tasks]
    pr_count = 0
    pr_artifact_map: Dict[str, str] = {}   # task_id -> pr_url
    error_log_map: Dict[str, str] = {}     # task_id -> truncated error snippet

    if all_task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(all_task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                if a.created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = a.pr_url or ""
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:500]

    # --- needs_attention: failed + pending approval ---
    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "reason": "failed",
            "error_snippet": error_log_map.get(tid),
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
        })
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Pending approval",
            "reason": "approval_required",
            "error_snippet": None,
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
            "approval_reason": t.latest_status_message,
            "touched_critical": (t.context or {}).get("touched_critical", []),
        })
    needs_attention.sort(key=lambda x: (0 if x["reason"] == "approval_required" else 1, -(x["waiting_hours"] or 0)))

    # --- recent_failures (up to 10, desc) ---
    sorted_failures = sorted(failed_tasks, key=_derive_completed_at, reverse=True)
    recent_failures = []
    for t in sorted_failures[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        recent_failures.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "failed_at": ts.isoformat() if ts else None,
            "error_snippet": error_log_map.get(tid),
            "waiting_hours": waiting_hours,
            "attempt_count": t.attempt_count,
        })

    # --- failure_groups (classify failures by type) ---
    from app.failure_classification import classify_failure as _classify_failure
    _failure_class_labels = {
        "CODE_ERROR": "Code Error",
        "PROVISION_FAILURE": "Provision Failure",
        "AGENT_TIMEOUT": "Agent Timeout",
        "EXTERNAL_API": "External API",
        "RATE_LIMIT": "Rate Limit",
        "SCOPE_OVERFLOW": "Scope Overflow",
    }
    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in failed_tasks:
        tid = str(t.id)
        cls = _classify_failure(t, error_log_map.get(tid))
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        _failure_class_groups.setdefault(cls, []).append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "error_snippet": error_log_map.get(tid),
            "attempt_count": t.attempt_count,
            "waiting_hours": waiting_hours,
        })
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

    # Taxonomy distribution (4-category: scope/review/infra/code_defect)
    from app.failure_classification import (
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY, TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
    )
    _tax_keys = (
        TAXONOMY_SCOPE_RELATED, TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED, TAXONOMY_CODE_DEFECT,
    )
    _tax_counts: Dict[str, int] = {k: 0 for k in _tax_keys}
    for t in failed_tasks:
        _tax_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(_tax_counts.values())
    failure_taxonomy_distribution = {
        "categories": {
            k: {
                "count": _tax_counts[k],
                "percentage": round(_tax_counts[k] / _tax_total * 100, 1) if _tax_total else 0.0,
            }
            for k in _tax_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_tax_keys, key=lambda k: _tax_counts[k]) if _tax_total else None,
    }

    # --- recent_completions (up to 10, desc) ---
    sorted_completions = sorted(completed_tasks, key=_derive_completed_at, reverse=True)
    recent_completions = []
    for t in sorted_completions[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        task_type = (t.first_payload).get("type", "create")
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        duration_seconds = None
        if t.started_at and ts:
            duration_seconds = int((ts - t.started_at).total_seconds())
        recent_completions.append({
            "task_id": tid,
            "title": t.title or "Completed task",
            "completed_at": ts.isoformat() if ts else None,
            "pr_url": pr_artifact_map.get(tid),
            "task_type": task_type,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "duration_seconds": duration_seconds,
        })

    # --- active_tasks list (up to 5, oldest started first) ---
    active_tasks_list = [
        {
            "task_id": str(t.id),
            "title": t.title or "Active task",
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }
        for t in active_tasks_all[:5]
    ]

    running_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING}
    queued_statuses = {TaskStatus.QUEUED, TaskStatus.EVALUATING}
    running_count = sum(1 for t in active_tasks_all if t.status in running_statuses)
    queued_count = sum(1 for t in active_tasks_all if t.status in queued_statuses)

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # --- narrative_summary ---
    completed_count = len(completed_tasks)
    failed_count = len(failed_tasks)
    pending_approval_count = len(approval_tasks)

    # --- Previous window metrics for trend comparison ---
    prev_completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total_terminal = len(prev_completed_tasks) + len(prev_failed_tasks)
    prev_success_rate = round(len(prev_completed_tasks) / prev_total_terminal, 2) if prev_total_terminal > 0 else None
    prev_completed_count = len(prev_completed_tasks)
    prev_failed_count = len(prev_failed_tasks)

    completed_delta = completed_count - prev_completed_count
    failed_delta = failed_count - prev_failed_count
    success_rate_delta: float | None = (
        round(success_rate - prev_success_rate, 2)
        if success_rate is not None and prev_success_rate is not None
        else None
    )
    if success_rate_delta is not None:
        if success_rate_delta > 0.05:
            trend: str | None = "improving"
        elif success_rate_delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = None

    # Compute trend history: 7 periods (6 previous + current), oldest first
    _now = _utcnow()
    trend_history = []
    for _i in range(7):
        _p_start = _now - timedelta(hours=hours * (7 - _i))
        _p_end = _now - timedelta(hours=hours * (6 - _i))
        _p_completed = sum(
            1 for t in all_tasks
            if t.status == TaskStatus.COMPLETED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_failed_tasks = [
            t for t in all_tasks
            if t.status == TaskStatus.FAILED
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_failed = len(_p_failed_tasks)
        _p_by_class: dict[str, int] = {}
        _p_by_taxonomy: dict[str, int] = {}
        for _ft in _p_failed_tasks:
            _cls = _classify_failure(_ft)
            _p_by_class[_cls] = _p_by_class.get(_cls, 0) + 1
            _tax = _classify_taxonomy(_ft)
            _p_by_taxonomy[_tax] = _p_by_taxonomy.get(_tax, 0) + 1
        _p_total = _p_completed + _p_failed
        _p_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and _p_start <= t.vm_stopped_at < _p_end)
        _p_terminal = [
            t for t in all_tasks
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_measured = [t for t in _p_terminal if "_first_attempt_succeeded" in (t.context or {})]
        _p_fa_rate = round(sum(1 for t in _p_measured if (t.context or {}).get("_first_attempt_succeeded")) / len(_p_measured), 2) if _p_measured else None
        trend_history.append({
            "period_end": _p_end.isoformat(),
            "success_rate": round(_p_completed / _p_total, 2) if _p_total > 0 else None,
            "completed": _p_completed,
            "failed": _p_failed,
            "failed_by_class": _p_by_class,
            "failed_by_taxonomy": _p_by_taxonomy,
            "period_cost_usd": round(_p_cost, 4),
            "first_attempt_success_rate": _p_fa_rate,
            "first_attempt_sample_size": len(_p_measured),
        })

    # Action-first narrative: executives want "what do I need to do?" before "how did things go?"
    narrative_parts = []

    # All clear shortcut: nothing needs attention and there are completions
    if pending_approval_count == 0 and failed_count == 0 and completed_count > 0:
        all_clear = f"All clear — {completed_count} task{'s' if completed_count != 1 else ''} completed"
        if pr_count > 0:
            all_clear += f" with {pr_count} PR{'s' if pr_count != 1 else ''} merged"
        all_clear += ", no action needed."
        narrative_summary = all_clear
    else:
        # FIRST: Approval items — most actionable, lead with them
        if pending_approval_count > 0:
            approval_items = [
                x for x in needs_attention if x["reason"] == "approval_required"
            ]
            max_wait = max((x.get("waiting_hours") or 0 for x in approval_items), default=0)
            approval_str = f"{pending_approval_count} task{'s' if pending_approval_count != 1 else ''} awaiting your approval"
            if max_wait > 0:
                approval_str += f" ({max_wait}h)"
            narrative_parts.append(approval_str + ".")

        # SECOND: Failures with root cause
        if failed_count > 0:
            dominant_group = failure_groups[0] if failure_groups else None
            dominant_msg = None
            if dominant_group and dominant_group["count"] >= 2 and dominant_group["count"] / failed_count >= 0.6:
                cls = dominant_group["failure_class"]
                cnt = dominant_group["count"]
                _root_cause_msgs = {
                    "RATE_LIMIT": f"Most failures ({cnt}/{failed_count}) are rate-limit errors — consider pausing until account recovery.",
                    "PROVISION_FAILURE": f"Most failures ({cnt}/{failed_count}) are provisioning issues — check GCP quota and zone health.",
                    "EXTERNAL_API": f"Most failures ({cnt}/{failed_count}) are external API errors — GitHub/Claude API may be experiencing issues.",
                    "AGENT_TIMEOUT": f"Most failures ({cnt}/{failed_count}) are agent timeouts — tasks may need complexity reduction.",
                    "CODE_ERROR": f"Most failures ({cnt}/{failed_count}) are code errors in agent-written code.",
                    "SCOPE_OVERFLOW": f"Most failures ({cnt}/{failed_count}) exceeded scope budget (touched more files than scope allows) — consider task decomposition or scope escalation.",
                }
                if cls == "CODE_ERROR":
                    _ce_tax: Dict[str, int] = {}
                    for _t in failed_tasks:
                        if _classify_failure(_t, error_log_map.get(str(_t.id))) == "CODE_ERROR":
                            _sub = _classify_taxonomy(_t)
                            _ce_tax[_sub] = _ce_tax.get(_sub, 0) + 1
                    if _ce_tax:
                        _sub_dom, _sub_cnt = max(_ce_tax.items(), key=lambda kv: kv[1])
                        if _sub_cnt / cnt >= 0.6:
                            _sub_msgs = {
                                TAXONOMY_SCOPE_RELATED: f"Most failures ({cnt}/{failed_count}) are code errors driven by scope budget overruns — consider task decomposition or scope escalation.",
                                TAXONOMY_REVIEW_QUALITY: f"Most failures ({cnt}/{failed_count}) are code errors from test/lint failures — strengthen self-review before PR creation.",
                                TAXONOMY_CODE_DEFECT: f"Most failures ({cnt}/{failed_count}) are logical code defects in agent-written code.",
                            }
                            _refined = _sub_msgs.get(_sub_dom)
                            if _refined:
                                _root_cause_msgs["CODE_ERROR"] = _refined
                dominant_msg = _root_cause_msgs.get(cls)
            if dominant_msg:
                narrative_parts.append(dominant_msg)
            else:
                failure_titles = [t.title or "Failed task" for t in sorted_failures[:2]]
                failed_str = f"{failed_count} task{'s' if failed_count != 1 else ''} failed"
                if failure_titles:
                    failed_str += f" ({', '.join(failure_titles)})"
                narrative_parts.append(failed_str + ".")

            _dom_cat = failure_taxonomy_distribution.get("dominant_category")
            _tax_tot = failure_taxonomy_distribution.get("total_failures") or 0
            if _dom_cat and _tax_tot >= 2:
                _dom_count = failure_taxonomy_distribution["categories"][_dom_cat]["count"]
                if _dom_count / _tax_tot >= 0.5:
                    _taxonomy_msgs = {
                        "review_quality": "Review quality issues dominate failures — improving lint/test coverage may help.",
                        "scope_related": "Scope mismatch is the top failure pattern — consider task decomposition.",
                        "infra_related": "Infrastructure issues dominate — check provisioning and rate limits.",
                        "code_defect": "Code defects dominate failures — strengthening self-review may help.",
                    }
                    _tax_msg = _taxonomy_msgs.get(_dom_cat)
                    if _tax_msg:
                        narrative_parts.append(_tax_msg)

        # THIRD: Completion stats with trend prefix embedded
        if completed_count > 0:
            trend_prefix = ""
            if trend == "declining":
                trend_prefix = "Pipeline health declining — "
            elif trend == "improving":
                trend_prefix = "Good progress — "
            shipped = f"{trend_prefix}{completed_count} task{'s' if completed_count != 1 else ''} completed"
            if pr_count > 0:
                shipped += f" with {pr_count} PR{'s' if pr_count != 1 else ''} opened"
            completion_titles = [t.title or "Completed task" for t in sorted_completions[:2]]
            completion_titles = [t[:50] + "..." if len(t) > 50 else t for t in completion_titles]
            if completion_titles:
                shipped += f" including {', '.join(completion_titles)}"
            if completed_delta > 0:
                shipped += f" (up from {prev_completed_count} last period)"
            elif completed_delta < 0:
                shipped += f" (down from {prev_completed_count} last period)"
            narrative_parts.append(shipped + ".")

        # FOURTH: Direction progress
        if project_id and direction_progress_list:
            _dir_tax_hints = {
                "review_quality": "mostly review-quality failures",
                "scope_related": "mostly scope-related failures",
                "infra_related": "mostly infra-related failures",
                "code_defect": "mostly code-defect failures",
            }
            dir_summaries = []
            for dp in direction_progress_list:
                dt = (dp["direction_text"] or "")[:40]
                if dp["prs_merged"] > 0:
                    entry = f"{dt}: {dp['prs_merged']} PR{'s' if dp['prs_merged'] != 1 else ''} merged"
                elif dp.get("possibly_saturated"):
                    entry = f"{dt}: may be saturated"
                elif dp["tasks_created"] > 0:
                    entry = f"{dt}: {dp['tasks_created']} task{'s' if dp['tasks_created'] != 1 else ''} in progress"
                else:
                    continue
                _tax = dp.get("taxonomy_summary") or {}
                _tax_total = sum(_tax.values())
                if _tax_total >= 2:
                    _dom = max(_tax, key=_tax.get)
                    if _tax[_dom] / _tax_total >= 0.5:
                        _hint = _dir_tax_hints.get(_dom)
                        if _hint:
                            entry += f" ({_hint})"
                dir_summaries.append(entry)
            if dir_summaries:
                narrative_parts.append("Research: " + "; ".join(dir_summaries) + ".")

        # FIFTH: Pipeline status
        if running_count > 0 or queued_count > 0:
            pipeline_parts = []
            if running_count > 0:
                pipeline_parts.append(f"{running_count} running")
            if queued_count > 0:
                pipeline_parts.append(f"{queued_count} queued")
            narrative_parts.append(", ".join(pipeline_parts) + ".")

        if narrative_parts:
            narrative_summary = " ".join(narrative_parts)
        else:
            narrative_summary = f"All quiet — no activity in the last {hours}h."

    # Compute total estimated cost (directed + undirected tasks)
    directed_task_ids = {t.id for t in all_tasks if (t.context or {}).get("direction_id")}
    undirected_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start and t.id not in directed_task_ids)
    total_estimated_cost_usd = round(sum(d["estimated_cost_usd"] for d in direction_progress_list) + undirected_cost, 2)

    # Compute today's cost for cost ceiling warning
    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_cost_usd = round(sum(
        ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= today_start
    ), 2)
    daily_cost_ceiling_usd = float(_runtime_config.get("daily_cost_ceiling_usd", 0))

    # Compute overall product_ratio and type_distribution across all directions
    all_product_ratios = [d["product_ratio"] for d in direction_progress_list if d.get("product_ratio") is not None]
    overall_product_ratio = round(sum(all_product_ratios) / len(all_product_ratios), 3) if all_product_ratios else None

    # Pipeline-level acceptance rate: total merged / (total merged + total rejected) across directions in window
    _pipeline_total_merged_d = sum(d.get("prs_merged") or 0 for d in direction_progress_list)
    _pipeline_total_rejected_d = sum(d.get("prs_rejected") or 0 for d in direction_progress_list)
    _pipeline_total_decided = _pipeline_total_merged_d + _pipeline_total_rejected_d
    pipeline_acceptance_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_decided, 3)
        if _pipeline_total_decided > 0 else None
    )
    _pipeline_total_created_d = sum(d.get("tasks_created") or 0 for d in direction_progress_list)
    pipeline_conversion_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_created_d, 3)
        if _pipeline_total_created_d > 0 else None
    )
    overall_type_distribution: Dict[str, int] = {}
    for d in direction_progress_list:
        for ttype, cnt in (d.get("type_distribution") or {}).items():
            overall_type_distribution[ttype] = overall_type_distribution.get(ttype, 0) + cnt

    pipeline_filter_funnel: Dict[str, int] = {}
    for d in direction_progress_list:
        ff = d.get("filter_funnel") or {}
        for _k, _v in ff.items():
            pipeline_filter_funnel[_k] = pipeline_filter_funnel.get(_k, 0) + (_v or 0)

    # --- Latest strategic cycle ---
    latest_cycle = None
    if project_id:
        _proj = await session.get(Project, project_id)
        if _proj:
            cycle_history = (_proj.vision or {}).get("_strategic_cycle_history", [])
            completed_cycles = [c for c in cycle_history if c.get("outcome") not in (None, "in_progress")]
            if completed_cycles:
                last = max(completed_cycles, key=lambda c: c.get("started_at") or "")
                latest_cycle = {
                    "cycle_id": last.get("cycle_id"),
                    "started_at": last.get("started_at"),
                    "completed_at": last.get("phase2_completed_at"),
                    "outcome": last.get("outcome"),
                    "directions_created": last.get("directions_created", 0),
                    "directions_retired": last.get("directions_retired", 0),
                    "vision_updated": last.get("vision_updated", False),
                    "summary_text": last.get("summary_text"),
                }

    return {
        "window_hours": hours,
        "generated_at": _utcnow().isoformat(),
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "pending_approval_count": len(approval_tasks),
        "research_cycles": research_cycles,
        "prs_opened": pr_count,
        "active_count": len(active_tasks_all),
        "running_count": running_count,
        "queued_count": queued_count,
        "active_tasks": active_tasks_list,
        "success_rate": success_rate,
        "needs_attention": needs_attention,
        "recent_failures": recent_failures,
        "failure_groups": failure_groups,
        "failure_taxonomy_distribution": failure_taxonomy_distribution,
        "recent_completions": recent_completions,
        "direction_progress": direction_progress_list,
        "overall_product_ratio": overall_product_ratio,
        "overall_type_distribution": overall_type_distribution or None,
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "daily_cost_usd": daily_cost_usd,
        "daily_cost_ceiling_usd": daily_cost_ceiling_usd,
        "narrative_summary": narrative_summary,
        "prev_completed_count": prev_completed_count,
        "prev_failed_count": prev_failed_count,
        "prev_success_rate": prev_success_rate,
        "completed_delta": completed_delta,
        "failed_delta": failed_delta,
        "success_rate_delta": success_rate_delta,
        "trend": trend,
        "trend_history": trend_history,
        "latest_cycle": latest_cycle,
        "scope_conversion_rates": scope_conversion_rates or None,
        "prev_scope_conversion_rates": prev_scope_conversion_rates or None,
        "pipeline_acceptance_rate": pipeline_acceptance_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "pipeline_filter_funnel": pipeline_filter_funnel or None,
    }
