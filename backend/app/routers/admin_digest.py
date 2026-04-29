"""Catch-up digest and pipeline health summary endpoints."""
import asyncio
import uuid
import logging
import statistics
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.notification_dispatch import dispatch_external_notification, post_residual_fix_unsuspend_message

from app.database import get_session
from app.digest_cache import DigestCache, get_digest_cache
from app.models import (
    Task, TaskStatus, Project, Artifact, ArtifactType, _utcnow,
    MaxPlanAccount,
    ResidualRemediationCase, ResidualRemediationCaseStatus,
)
from app.auth import get_current_user, require_admin
from app.shared_state import runtime_config as _runtime_config
from app.routers.admin_analytics import _derive_completed_at, ON_DEMAND_RATE, SPOT_RATE
from app.direction_metrics import _compute_direction_quality_score
from app.direction_outcomes import synthesize_prompt_quality_patterns
from app.retry_budget import failure_class_history_classes
from app import strategic_cycle as _strategic_cycle
from app.strategic_analysis import _compute_prediction_calibration

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-digest"],
    dependencies=[Depends(get_current_user)],
)

_CPM_RETIREMENT_MULTIPLIER = 3.0  # cost_per_merge > median × this → retirement signal


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


def _compute_review_quality_metrics(terminal_tasks: List[Task]) -> Dict[str, Any]:
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

    tasks_with_sr = [t for t in terminal_tasks if "_self_review_outcome" in (t.context or {})]
    tasks_without_sr = [t for t in terminal_tasks if "_self_review_outcome" not in (t.context or {})]
    sr_clean = [t for t in tasks_with_sr if not (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)]
    sr_fixed = [t for t in tasks_with_sr if (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)]

    self_review_effectiveness: Dict[str, Any] | None = None
    if len(tasks_with_sr) >= 3:
        self_review_effectiveness = {
            "with_self_review_approval_rate": _fa_rate(tasks_with_sr),
            "without_self_review_approval_rate": _fa_rate(tasks_without_sr),
            "clean_verdict_approval_rate": _fa_rate(sr_clean),
            "fixed_verdict_approval_rate": _fa_rate(sr_fixed),
            "sample_size": len(tasks_with_sr),
        }

    review_tasks = [
        t for t in terminal_tasks
        if (t.first_payload or {}).get("type") == "review"
        and (t.context or {}).get("blueprint_review_verdict") is not None
    ]
    review_accuracy: Dict[str, Any] | None = None
    if len(review_tasks) >= 5:
        total = len(review_tasks)
        overrides = sum(1 for t in review_tasks if (t.context or {}).get("user_overrode_review"))
        dismissals = sum(1 for t in review_tasks if (t.context or {}).get("review_dismissed"))
        misses = sum(1 for t in review_tasks if (t.context or {}).get("user_caught_issues"))
        approvals = sum(1 for t in review_tasks if (t.context or {}).get("blueprint_review_verdict") == "APPROVE")
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
        t for t in terminal_tasks
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and "_first_attempt_rejection_categories" in (t.context or {})
    ]
    self_review_prediction_accuracy: Dict[str, Any] | None = None
    if len(tasks_with_both) >= 5:
        total_human = total_overlap = total_missed = total_false = total_sr_failed = 0
        for t in tasks_with_both:
            ctx = t.context or {}
            check_results = ctx.get("_self_review_outcome", {}).get("check_results") or {}
            human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
            sr_failed = {c for c, r in check_results.items() if not r.get("passed", True)}
            total_human += len(human_cats)
            total_overlap += len(human_cats & sr_failed)
            total_missed += len(human_cats - sr_failed)
            total_false += len(sr_failed - human_cats)
            total_sr_failed += len(sr_failed)
        self_review_prediction_accuracy = {
            "prediction_hit_rate": round(total_overlap / total_human, 3) if total_human > 0 else None,
            "miss_rate": round(total_missed / total_human, 3) if total_human > 0 else None,
            "false_alarm_rate": round(total_false / total_sr_failed, 3) if total_sr_failed > 0 else None,
            "sample_size": len(tasks_with_both),
        }

    return {
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "review_accuracy": review_accuracy,
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

    measured = [t for t in terminal_tasks if "_first_attempt_succeeded" in (t.context or {})]
    with_pb = [t for t in measured if (t.context or {}).get("_had_playbook") is True]
    without_pb = [t for t in measured if (t.context or {}).get("_had_playbook") is False]
    with_arch = [t for t in measured if (t.context or {}).get("_had_arch_context") is True]
    without_arch = [t for t in measured if (t.context or {}).get("_had_arch_context") is False]
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
        t for t in terminal_tasks
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


_REVIEW_ELIGIBLE_TYPES = {"create", "refactor"}


def _compute_first_review_approval_breakdowns(terminal_tasks: List[Task]) -> Dict[str, Any]:
    """Compute first-review approval rate with playbook/measurement/type/scope breakdowns.

    A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
    A task is 'approved on first review' if _first_attempt_rejection_categories is absent.
    """
    def _rate(tasks):
        eligible = [
            t for t in tasks
            if (t.first_payload or {}).get("type", "create") in _REVIEW_ELIGIBLE_TYPES
            and (t.context or {}).get("pr_merged") is True
        ]
        if not eligible:
            return None
        approved = sum(
            1 for t in eligible
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

    with_pb = [t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is True]
    without_pb = [t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is False]
    with_m = [t for t in terminal_tasks if (t.context or {}).get("_had_review_measurements") is True]
    without_m = [t for t in terminal_tasks if (t.context or {}).get("_had_review_measurements") is False]

    eligible_merged = [
        t for t in terminal_tasks
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

    _scope_taxonomy_counts: Dict[str, Dict[str, int]] = {}
    for t in failed_tasks:
        scope = (t.context or {}).get("scope", "unknown")
        bucket = _scope_taxonomy_counts.setdefault(scope, {k: 0 for k in _taxonomy_keys})
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
            "dominant_category": max(_taxonomy_keys, key=lambda k: counts[k]) if tot else None,
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
            prs_rejected = 0
            tasks_created_total = 0
            sd_totals: Dict[str, int] = {}
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        prs_rejected += entry.get("prs_rejected") or 0
                        tasks_created_total += entry.get("tasks_created") or 0
                        for k, v in (entry.get("stage_dropout") or {}).items():
                            sd_totals[k] = sd_totals.get(k, 0) + (v or 0)
                except (ValueError, TypeError):
                    pass

            dir_conversion_rate = round(prs_merged / tasks_created_total, 3) if tasks_created_total > 0 else None
            acceptance_denom = prs_merged + prs_rejected
            acceptance_rate = round(prs_merged / acceptance_denom, 3) if acceptance_denom > 0 else None
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
                "completion_criteria": d.get("completion_criteria"),
                "possibly_complete": bool(d.get("possibly_complete", False)),
                "category": d.get("category"),
                "acceptance_rate": acceptance_rate,
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
        "failure_taxonomy_by_scope": failure_taxonomy_by_scope,
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
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Return a catch-up digest of activity within the requested time window.

    Wraps the compute path in a TTL cache with in-flight dedup so that a
    stampede of concurrent admin UI polls doesn't re-execute the 5000-row
    JSONB scan for each request. Cache key is (hours, project_id) — the
    response does not vary per user. Default TTL 15s; tune in digest_cache.
    """
    cache_key = ("digest", hours, str(project_id) if project_id else None)
    return await cache.get_or_compute(
        cache_key,
        lambda: _compute_digest(hours=hours, project_id=project_id, session=session),
    )


@router.get("/residual-fingerprints")
async def get_residual_fingerprints(
    hours: int = Query(24, ge=1, le=168),
    top_n: int = Query(10, ge=1, le=50),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return top-N CODE_ERROR fingerprint clusters with no matching classifier.

    Surfaces emerging failure patterns — task failures that reached the default
    fallthrough in classify_failure() because no keyword bank matched. Groups
    near-duplicate messages by fingerprint so operators can see what new
    classifiers are needed next cycle.

    Excludes tasks where the agent explicitly reported a failure_category,
    since those are known failure modes rather than unclassified residuals.
    """
    from app.failure_classification import classify_failure as _cf  # noqa: PLC0415
    from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415

    window_start = _utcnow() - timedelta(hours=hours)
    filters = [Task.created_at >= window_start, Task.status == TaskStatus.FAILED]
    if project_id:
        filters.append(Task.project_id == project_id)

    result = await session.execute(
        select(Task)
        .where(*filters)
        .options(load_only(
            Task.id, Task.project_id, Task.latest_status_message,
            Task.context, Task.created_at,
        ))
        .limit(2000)
    )
    tasks = list(result.scalars().all())

    clusters: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        if (task.context or {}).get("failure_category"):
            continue
        if _cf(task) != "CODE_ERROR":
            continue
        msg = task.latest_status_message or ""
        if not msg:
            continue
        fp = _error_fingerprint(msg)
        entry = clusters.setdefault(fp, {
            "fingerprint": fp, "count": 0,
            "most_recent_at": None, "sample_message": msg[:300],
            "example_task_ids": [],
        })
        entry["count"] += 1
        ts = task.created_at.isoformat() if task.created_at else None
        if ts and (entry["most_recent_at"] is None or ts > entry["most_recent_at"]):
            entry["most_recent_at"] = ts
        if len(entry["example_task_ids"]) < 3:
            entry["example_task_ids"].append(str(task.id))

    top = sorted(clusters.values(), key=lambda x: x["count"], reverse=True)[:top_n]

    # Bulk-lookup ResidualRemediationCase rows for the returned fingerprints.
    cases: Dict[str, Any] = {}
    top_fps = [e["fingerprint"] for e in top]
    if top_fps:
        case_filters = [ResidualRemediationCase.fingerprint.in_(top_fps)]
        if project_id:
            case_filters.append(ResidualRemediationCase.project_id == project_id)
        case_rows = await session.execute(
            select(ResidualRemediationCase)
            .where(*case_filters)
            .options(load_only(
                ResidualRemediationCase.fingerprint,
                ResidualRemediationCase.status,
                ResidualRemediationCase.last_spawned_pr_number,
                ResidualRemediationCase.consecutive_failure_count,
                ResidualRemediationCase.suspended_at,
                ResidualRemediationCase.audit_defer_count,
                ResidualRemediationCase.audit_deferred_system_id,
            ))
        )
        for c in case_rows.scalars().all():
            cases[c.fingerprint] = c

    enriched = []
    for e in top:
        entry: Dict[str, Any] = {
            "fingerprint": e["fingerprint"],
            "count": e["count"],
            "most_recent_at": e["most_recent_at"],
            "sample_message": e["sample_message"],
            "example_task_ids": e["example_task_ids"],
        }
        c = cases.get(e["fingerprint"])
        if c is not None:
            entry["case_status"] = c.status
            entry["last_spawned_pr_number"] = c.last_spawned_pr_number
            entry["consecutive_failure_count"] = c.consecutive_failure_count
            entry["suspended_at"] = c.suspended_at.isoformat() if c.suspended_at else None
            entry["audit_defer_count"] = c.audit_defer_count
            entry["audit_deferred_system_id"] = c.audit_deferred_system_id
        enriched.append(entry)

    return {
        "window_hours": hours,
        "total_code_error_count": sum(e["count"] for e in clusters.values()),
        "residual_count": len(clusters),
        "top_residuals": enriched,
    }


@router.get("/residual-cases")
async def list_residual_cases(
    status: list[str] | None = Query(None, description="Filter by ResidualRemediationCaseStatus values; repeatable"),
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List residual remediation cases.

    Defaults to open cases (ACTIVE / REMEDIATING / SUSPENDED) so operators can
    see what's currently driving auto-remediation pressure. Pass ``status`` to
    filter — e.g. ``?status=IGNORED&status=RESOLVED`` for a closed-cases view.
    Ordered by last_seen_at desc so the most recently-active fingerprints
    surface first.
    """
    if status:
        valid = {s.value for s in ResidualRemediationCaseStatus}
        invalid = [s for s in status if s not in valid]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status values: {invalid}. Valid: {sorted(valid)}",
            )
        status_filter = list(status)
    else:
        status_filter = [
            ResidualRemediationCaseStatus.ACTIVE.value,
            ResidualRemediationCaseStatus.REMEDIATING.value,
            ResidualRemediationCaseStatus.SUSPENDED.value,
        ]

    filters = [ResidualRemediationCase.status.in_(status_filter)]
    if project_id:
        filters.append(ResidualRemediationCase.project_id == project_id)
    query = (
        select(ResidualRemediationCase)
        .where(*filters)
        .order_by(ResidualRemediationCase.last_seen_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    return {
        "count": len(rows),
        "status_filter": status_filter,
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id) if c.project_id else None,
                "fingerprint": c.fingerprint,
                "status": c.status,
                "hit_count": c.hit_count,
                "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "last_spawned_at": c.last_spawned_at.isoformat() if c.last_spawned_at else None,
                "last_spawned_pr_number": c.last_spawned_pr_number,
                "last_spawned_task_id": str(c.last_spawned_task_id) if c.last_spawned_task_id else None,
                "consecutive_failure_count": c.consecutive_failure_count,
                "suspended_at": c.suspended_at.isoformat() if c.suspended_at else None,
                "sample_message": (c.sample_messages[0] if c.sample_messages else c.sample_message),
                "audit_defer_count": c.audit_defer_count,
                "audit_deferred_system_id": c.audit_deferred_system_id,
            }
            for c in rows
        ],
    }


@router.post("/residual-cases/{fingerprint}/ignore")
async def ignore_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Mark a residual fingerprint as IGNORED for a project.

    Creates or updates the ResidualRemediationCase row for this
    (project_id, fingerprint) pair to status=IGNORED, suppressing future
    auto-remediation spawn decisions for this fingerprint.
    """
    now = _utcnow()
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        case = ResidualRemediationCase(
            project_id=project_id,
            fingerprint=fingerprint,
            status=ResidualRemediationCaseStatus.IGNORED,
            created_at=now,
            updated_at=now,
        )
        session.add(case)
    else:
        case.status = ResidualRemediationCaseStatus.IGNORED
        case.updated_at = now
    await session.commit()
    return {"id": str(case.id), "fingerprint": fingerprint, "status": ResidualRemediationCaseStatus.IGNORED}


@router.post("/residual-cases/{fingerprint}/unignore")
async def unignore_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Reactivate a previously IGNORED residual fingerprint.

    Sets the ResidualRemediationCase back to ACTIVE so auto-remediation
    can resume spawning for this fingerprint. Returns 404 if no case
    exists or if the case is not currently IGNORED.
    """
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != ResidualRemediationCaseStatus.IGNORED:
        raise HTTPException(status_code=404, detail="No IGNORED case found for this fingerprint and project")
    case.status = ResidualRemediationCaseStatus.ACTIVE
    case.updated_at = _utcnow()
    await session.commit()
    return {"id": str(case.id), "fingerprint": fingerprint, "status": ResidualRemediationCaseStatus.ACTIVE}


@router.post("/residual-cases/{fingerprint}/unsuspend")
async def unsuspend_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Re-enable a SUSPENDED residual fingerprint case.

    Transitions status back to ACTIVE, clears suspended_at, and resets
    consecutive_failure_count to 0. Returns 404 if no SUSPENDED case
    exists for (project_id, fingerprint). The cooldown (last_spawned_at)
    is intentionally preserved so the case does not immediately re-spawn.
    """
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != ResidualRemediationCaseStatus.SUSPENDED:
        raise HTTPException(status_code=404, detail="No SUSPENDED case found for this fingerprint and project")
    case.status = ResidualRemediationCaseStatus.ACTIVE
    case.suspended_at = None
    case.consecutive_failure_count = 0
    case.updated_at = _utcnow()
    session.add(case)
    await post_residual_fix_unsuspend_message(session, project_id, fingerprint)
    await session.commit()
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.ACTIVE,
        "consecutive_failure_count": 0,
        "suspended_at": None,
    }


@router.post("/residual-fingerprints/clear")
async def clear_residual_fingerprints(
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Wipe the in-memory residual fingerprint store.

    Used when the store is polluted (bad fingerprints from a prior
    misclassification regime) and needs to be reset. Returns the count
    of fingerprints removed. Does NOT touch the DB — only clears what's
    tracked in-memory for auto-remediation spawn decisions.
    """
    from app.failure_classification import _residual_store, _residual_seen_task_ids  # noqa: PLC0415
    count = len(_residual_store)
    _residual_store.clear()
    _residual_seen_task_ids.clear()
    return {"cleared": count}


@router.get("/provider-cost-summary")
async def get_provider_cost_summary(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate per-provider/account cost and success metrics for a rolling window.

    Groups terminal tasks by the account they ran on (stored in task.context),
    computing: total tasks, success rate, total spend, cost per merged PR.
    Returns rows sorted by total spend descending.
    """
    window_start = _utcnow() - timedelta(hours=hours)
    filters = [
        Task.created_at >= window_start,
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
    ]
    if project_id:
        filters.append(Task.project_id == project_id)

    tasks = (await session.execute(
        select(Task)
        .where(*filters)
        .options(load_only(
            Task.id, Task.status, Task.context,
            Task.vm_spot, Task.vm_provisioned_at, Task.vm_stopped_at,
        ))
        .limit(5000)
    )).scalars().all()

    def _acct_id(ctx: Dict[str, Any] | None) -> str | None:
        if not ctx:
            return None
        return ctx.get("llm_account_id") or ctx.get("max_plan_account_id")

    acct_ids = {a for t in tasks if (a := _acct_id(t.context))}
    acct_map: Dict[str, Dict[str, str]] = {}
    if acct_ids:
        parsed = [uuid.UUID(a) for a in acct_ids if a]
        if parsed:
            rows = (await session.execute(
                select(MaxPlanAccount).where(MaxPlanAccount.id.in_(parsed))
            )).scalars().all()
            for a in rows:
                acct_map[str(a.id)] = {"name": a.name, "provider": a.provider.value}

    groups: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        acct_id = _acct_id(t.context)
        info = acct_map.get(acct_id or "", {"name": "unknown", "provider": "unknown"})
        key = f"{info['provider']}|{acct_id or 'untracked'}"
        g = groups.setdefault(key, {
            "provider": info["provider"],
            "account_id": acct_id,
            "account_name": info["name"],
            "total_tasks": 0,
            "success_count": 0,
            "merged_count": 0,
            "total_cost_usd": 0.0,
        })
        g["total_tasks"] += 1
        if t.status == TaskStatus.COMPLETED:
            g["success_count"] += 1
            if (t.context or {}).get("pr_merged"):
                g["merged_count"] += 1
        if t.vm_provisioned_at and t.vm_stopped_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            g["total_cost_usd"] += (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() * rate

    result_rows = []
    for g in groups.values():
        result_rows.append({
            **g,
            "total_cost_usd": round(g["total_cost_usd"], 2),
            "success_rate": round(g["success_count"] / g["total_tasks"], 3) if g["total_tasks"] else None,
            "cost_per_merged_pr": round(g["total_cost_usd"] / g["merged_count"], 2) if g["merged_count"] else None,
        })
    result_rows.sort(key=lambda x: x["total_cost_usd"], reverse=True)
    return {"window_hours": hours, "rows": result_rows}


@router.get("/drift-snapshot")
async def get_drift_snapshot(
    project_id: uuid.UUID,
    window_days: int = Query(7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Return per-direction drift signals for a project.

    Calls compute_direction_drift on-demand and returns the flagged directions
    (over_claim or stalled_despite_churn) plus the per-system rollup. The UI
    uses this to surface claim-vs-shipped divergence without requiring operators
    to understand the raw signal format.
    """
    from app.drift_detection import compute_direction_drift  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        return {
            "flagged_directions": [],
            "flagged_count": 0,
            "total_directions": 0,
            "any_system_flagged": False,
            "by_system": {},
            "generated_at": _utcnow().isoformat(),
            "window_days": window_days,
        }

    snapshot = await compute_direction_drift(session, project, window_days=window_days)
    direction_map = {d["id"]: d.get("text", "") for d in (project.directions or []) if d.get("id")}

    flagged = [
        {
            "direction_id": did,
            "direction_text": direction_map.get(did, ""),
            "over_claim_flag": sig["over_claim_flag"],
            "stalled_despite_churn_flag": sig["stalled_despite_churn_flag"],
            "claimed_changes_count": sig["claimed_changes_count"],
            "actually_merged_prs": sig["actually_merged_prs"],
            "claim_shipped_consistency": sig["claim_shipped_consistency"],
        }
        for did, sig in snapshot["by_direction"].items()
        if sig.get("over_claim_flag") or sig.get("stalled_despite_churn_flag")
    ]

    return {
        "flagged_directions": flagged,
        "flagged_count": len(flagged),
        "total_directions": len(snapshot["by_direction"]),
        "any_system_flagged": any(
            s.get("over_claim_flag") or s.get("stalled_despite_churn_flag")
            for s in snapshot["by_system"].values()
        ),
        "by_system": snapshot["by_system"],
        "generated_at": snapshot["generated_at"],
        "window_days": snapshot["window_days"],
    }


async def _compute_digest(
    *,
    hours: int,
    project_id: uuid.UUID | None,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Extracted body of `get_digest` so the cache layer can call it without
    FastAPI-dependency plumbing."""
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

    _DIGEST_TASK_ROW_CAP = 5000
    _ACTIVE_TASKS_DISPLAY_LIMIT = 5
    # Project payloads[0] and status_log[-1]->>'t' SQL-side on PostgreSQL.
    # Loading the full JSONB arrays for 5000 rows blew a Cloud Run instance
    # past its 4 GiB limit under concurrent polling; with projection each
    # row carries only the first payload element plus a timestamp string.
    # SQLite (used in tests) doesn't support negative-index JSON paths and
    # returns json_extract results as text rather than parsed dicts, so we
    # fall back to the heavy load path there.
    _use_projection = session.bind.dialect.name == "postgresql"
    if _use_projection:
        tasks_query = (
            select(
                Task,
                Task.payloads[0].label("_projected_first_payload"),
                Task.status_log[-1]["t"].astext.label("_projected_completed_at_ts"),
            )
            .where(*task_filters)
            .options(load_only(
                Task.id, Task.status, Task.project_id, Task.title,
                Task.created_at, Task.started_at,
                Task.context,
                Task.latest_status_message, Task.attempt_count,
                Task.requires_approval,
                Task.vm_provisioned_at, Task.vm_started_at,
                Task.vm_stopped_at, Task.vm_spot,
                # Audit W1: promoted columns read in the digest funnel.
                Task.previous_error_fingerprint,
                Task.auto_remediation,
                # Per-(provider, model) cost attribution on direction CPM.
                Task.provider, Task.model,
            ))
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        tasks_result = await session.execute(tasks_query)
        all_tasks: List[Task] = []
        for row in tasks_result.all():
            task = row[0]
            _stash_projected_task_fields(
                task,
                first_payload=row[1] or {},
                completed_at_ts=row[2],
            )
            all_tasks.append(task)
    else:
        tasks_result = await session.execute(
            select(Task)
            .where(*task_filters)
            .options(load_only(
                Task.id, Task.status, Task.project_id, Task.title,
                Task.created_at, Task.started_at,
                Task.payloads, Task.context, Task.status_log,
                Task.latest_status_message, Task.attempt_count,
                Task.requires_approval,
                Task.vm_provisioned_at, Task.vm_started_at,
                Task.vm_stopped_at, Task.vm_spot,
                # Audit W1: promoted columns read in the digest funnel.
                Task.previous_error_fingerprint,
                Task.auto_remediation,
                # Per-(provider, model) cost attribution on direction CPM.
                Task.provider, Task.model,
            ))
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        all_tasks = list(tasks_result.scalars().all())
    tasks_capped = len(all_tasks) == _DIGEST_TASK_ROW_CAP
    if tasks_capped:
        logger.warning(
            "admin_digest task query hit %d-row safety cap (window=%dh, project_id=%s); digest metrics may be truncated",
            _DIGEST_TASK_ROW_CAP, hours, project_id,
        )

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
    if _use_projection:
        active_result = await session.execute(
            select(
                Task,
                Task.payloads[0].label("_projected_first_payload"),
            ).where(*active_filters)
            .order_by(Task.started_at.asc().nullsfirst())
            .options(load_only(
                Task.id, Task.status, Task.title,
                Task.started_at, Task.project_id, Task.context,
                Task.vm_provisioned_at, Task.vm_spot,
                Task.previous_error_fingerprint,
                Task.auto_remediation,
            ))
        )
        active_tasks_all: List[Task] = []
        for row in active_result.all():
            task = row[0]
            _stash_projected_task_fields(task, first_payload=row[1] or {})
            active_tasks_all.append(task)
    else:
        active_result = await session.execute(
            select(Task).where(*active_filters)
            .order_by(Task.started_at.asc().nullsfirst())
            .options(load_only(
                Task.id, Task.status, Task.title, Task.payloads,
                Task.started_at, Task.project_id, Task.context,
                Task.vm_provisioned_at, Task.vm_spot,
                Task.previous_error_fingerprint,
                Task.auto_remediation,
            ))
        )
        active_tasks_all = list(active_result.scalars().all())

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1 for t in completed_tasks
        if (t.first_payload).get("type", "create") == "research"
    )

    # --- Direction progress (only when project_id provided) ---
    direction_progress_list: List[Dict[str, Any]] = []
    median_cost_per_merged_pr: Optional[float] = None
    dir_id_to_text: Dict[str, str] = {}
    scope_conversion_rates: Dict[str, Dict[str, int]] = {}
    prev_scope_conversion_rates: Dict[str, Dict[str, int]] = {}

    _budget_alert_fired = False
    if project_id:
        project = await session.get(Project, project_id)
        if project:
            active_directions = [
                d for d in (project.directions or [])
                if d.get("status") in ("active", "held", "auto_paused")
            ]

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
                autonomous_prs_merged = 0
                assisted_prs_merged = 0
                prs_rejected = 0
                findings_tasks_created = 0
                merge_time_values: List[float] = []
                dir_scope_agg: Dict[str, Dict[str, int]] = {}
                prev_dir_scope_agg: Dict[str, Dict[str, int]] = {}
                window_product_ratios: List[float] = []
                agg_type_distribution: Dict[str, int] = {}
                agg_type_breakdown: Dict[str, Dict[str, int]] = {}
                dir_taxonomy_summary: Dict[str, int] = {}
                dir_stage_dropout: Dict[str, int] = {}
                recent_failed_titles: List[str] = []
                dir_findings_produced = 0
                dir_batch_cap_applied = 0
                dir_filter_breakdown_agg: Dict[str, int] = {}
                dq_plans = 0
                dq_merge_rate_sum = 0.0
                dq_merge_rate_count = 0
                dq_subtask_count_sum = 0
                dq_cross_module_sum = 0
                dir_reviews_dismissed = 0
                dir_reviews_overridden = 0
                dir_user_issues_caught = 0
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        # Legacy fallback: pre-attribution entries lack autonomous_prs_merged.
                        autonomous_prs_merged += entry.get("autonomous_prs_merged", entry.get("prs_merged")) or 0
                        assisted_prs_merged += entry.get("assisted_prs_merged", 0) or 0
                        prs_rejected += entry.get("prs_rejected") or 0
                        dir_reviews_dismissed += entry.get("reviews_dismissed") or 0
                        dir_reviews_overridden += entry.get("reviews_overridden") or 0
                        dir_user_issues_caught += entry.get("user_issues_caught") or 0
                        findings_tasks_created += entry.get("tasks_created") or 0
                        dir_findings_produced += entry.get("findings_produced") or 0
                        dir_batch_cap_applied += entry.get("batch_cap_applied") or 0
                        for _fk, _fv in (entry.get("filter_breakdown") or {}).items():
                            dir_filter_breakdown_agg[_fk] = dir_filter_breakdown_agg.get(_fk, 0) + (_fv or 0)
                        atm = entry.get("avg_time_to_merge_hours")
                        if atm is not None:
                            merge_time_values.append(float(atm))
                        for scope, stats in (entry.get("scope_breakdown") or {}).items():
                            agg = dir_scope_agg.setdefault(scope, {"created": 0, "merged": 0, "merged_cross_module": 0, "failed": 0, "failed_infra": 0, "failed_code_error": 0, "failed_scope_overflow": 0, "calibrated_in": 0, "calibrated_out": 0, "failure_stages": {}})
                            for k in ("created", "merged", "merged_cross_module", "failed", "failed_infra", "failed_code_error", "failed_scope_overflow", "calibrated_in", "calibrated_out"):
                                agg[k] += stats.get(k, 0)
                            stage_agg = agg.setdefault("failure_stages", {})
                            for stage_name, stage_cnt in (stats.get("failure_stages") or {}).items():
                                stage_agg[stage_name] = stage_agg.get(stage_name, 0) + (stage_cnt or 0)
                            # Weighted predictor accumulators (weight by merged/failed counts
                            # that produced each per-cycle average in direction_outcomes.py).
                            merged_w = stats.get("merged", 0) or 0
                            failed_w = stats.get("failed", 0) or 0
                            for field_name, weight in (
                                ("avg_expansion_ratio_merged", merged_w),
                                ("avg_expansion_ratio_failed", failed_w),
                                ("avg_complexity_merged", merged_w),
                                ("avg_complexity_failed", failed_w),
                            ):
                                v = stats.get(field_name)
                                if isinstance(v, (int, float)) and weight > 0:
                                    agg[f"_{field_name}_sum"] = agg.get(f"_{field_name}_sum", 0.0) + float(v) * weight
                                    agg[f"_{field_name}_w"] = agg.get(f"_{field_name}_w", 0) + weight
                            for effort, buckets in (stats.get("effort_distribution") or {}).items():
                                if not isinstance(buckets, dict):
                                    continue
                                ed = agg.setdefault("effort_distribution", {})
                                eb = ed.setdefault(effort, {"created": 0, "merged": 0, "failed": 0})
                                for bk in ("created", "merged", "failed"):
                                    eb[bk] += buckets.get(bk, 0) or 0
                        pr = entry.get("product_ratio")
                        if pr is not None:
                            window_product_ratios.append(float(pr))
                        for ttype, cnt in (entry.get("type_distribution") or {}).items():
                            agg_type_distribution[ttype] = agg_type_distribution.get(ttype, 0) + cnt
                        for ttype, stats in (entry.get("type_breakdown") or {}).items():
                            tb = agg_type_breakdown.setdefault(ttype, {"created": 0, "merged": 0, "failed": 0})
                            for k in ("created", "merged", "failed"):
                                tb[k] += (stats or {}).get(k, 0) or 0
                        for tax, cnt in (entry.get("taxonomy_summary") or {}).items():
                            dir_taxonomy_summary[tax] = dir_taxonomy_summary.get(tax, 0) + cnt
                        for stage, cnt in (entry.get("stage_dropout") or {}).items():
                            dir_stage_dropout[stage] = dir_stage_dropout.get(stage, 0) + (cnt or 0)
                        for dq in (entry.get("decomposition_quality") or []):
                            if not isinstance(dq, dict):
                                continue
                            dq_plans += 1
                            mr = dq.get("subtask_merge_rate")
                            if mr is not None:
                                dq_merge_rate_sum += float(mr)
                                dq_merge_rate_count += 1
                            dq_subtask_count_sum += dq.get("subtask_count") or 0
                            dq_cross_module_sum += dq.get("cross_module_count") or 0
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

                dir_cost = 0.0
                dir_spot_cost = 0.0
                dir_on_demand_cost = 0.0
                cost_by_scope: Dict[str, float] = {}
                cost_by_status: Dict[str, float] = {}
                cost_by_type: Dict[str, float] = {}
                cost_by_provider_model: Dict[str, float] = {}
                for t in tasks_for_dir:
                    if not (t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start):
                        continue
                    rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                    t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                    dir_cost += t_cost
                    if t.vm_spot:
                        dir_spot_cost += t_cost
                    else:
                        dir_on_demand_cost += t_cost
                    scope_key = (t.context or {}).get("scope", "unknown")
                    cost_by_scope[scope_key] = cost_by_scope.get(scope_key, 0.0) + t_cost
                    status_key = t.status.value if hasattr(t.status, "value") else str(t.status)
                    cost_by_status[status_key] = cost_by_status.get(status_key, 0.0) + t_cost
                    type_key = (t.first_payload or {}).get("type", "create")
                    cost_by_type[type_key] = cost_by_type.get(type_key, 0.0) + t_cost
                    provider_key = t.provider or "unknown"
                    model_key = t.model or "unknown"
                    pm_key = f"{provider_key}|{model_key}"
                    cost_by_provider_model[pm_key] = cost_by_provider_model.get(pm_key, 0.0) + t_cost
                cost_by_scope = {k: round(v, 4) for k, v in cost_by_scope.items()}
                cost_by_status = {k: round(v, 4) for k, v in cost_by_status.items()}
                cost_by_type = {k: round(v, 4) for k, v in cost_by_type.items()}
                cost_by_provider_model = {k: round(v, 4) for k, v in cost_by_provider_model.items()}

                dir_fa_measured = [
                    t for t in tasks_for_dir
                    if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                    and _derive_completed_at(t) >= window_start
                    and "_first_attempt_succeeded" in (t.context or {})
                ]
                dir_fa_sample_size = len(dir_fa_measured)
                dir_fa_rate = (
                    round(sum(1 for t in dir_fa_measured if (t.context or {}).get("_first_attempt_succeeded")) / dir_fa_sample_size, 3)
                    if dir_fa_sample_size > 0 else None
                )

                _history_all = d.get("research_history") or []
                _agg = d.get("history_aggregate") or {}
                lifetime_cycles = len(_history_all) + (_agg.get("total_cycles") or 0)
                lifetime_prs_merged = sum(h.get("prs_merged", 0) for h in _history_all) + (_agg.get("total_prs_merged") or 0)
                lifetime_prs_rejected = sum(h.get("prs_rejected", 0) for h in _history_all) + (_agg.get("total_prs_rejected") or 0)
                lifetime_tasks_created = sum(h.get("tasks_created", 0) for h in _history_all) + (_agg.get("total_tasks_created") or 0)
                lifetime_tasks_failed = sum(h.get("tasks_failed", 0) for h in _history_all) + (_agg.get("total_tasks_failed") or 0)
                lifetime_conversion_rate = (
                    round(lifetime_prs_merged / lifetime_tasks_created, 3) if lifetime_tasks_created > 0 else None
                )

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                total_prs = prs_merged + prs_rejected
                acceptance_rate = round(prs_merged / total_prs, 3) if total_prs > 0 else None
                avg_time_to_merge_hours = round(sum(merge_time_values) / len(merge_time_values), 2) if merge_time_values else None
                conversion_rate = round(prs_merged / findings_tasks_created, 3) if findings_tasks_created > 0 else None
                # Accumulate into project-level scope rate aggregates
                for _s, _st in dir_scope_agg.items():
                    if _s not in scope_conversion_rates:
                        scope_conversion_rates[_s] = {"created": 0, "merged": 0, "merged_cross_module": 0, "failed_scope_overflow": 0, "calibrated_in": 0, "calibrated_out": 0, "failure_stages": {}}
                    scope_conversion_rates[_s]["created"] += _st["created"]
                    scope_conversion_rates[_s]["merged"] += _st["merged"]
                    scope_conversion_rates[_s]["merged_cross_module"] += _st.get("merged_cross_module", 0)
                    scope_conversion_rates[_s]["failed_scope_overflow"] += _st.get("failed_scope_overflow", 0)
                    scope_conversion_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    scope_conversion_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)
                    _stage_acc = scope_conversion_rates[_s].setdefault("failure_stages", {})
                    for _stg, _scnt in (_st.get("failure_stages") or {}).items():
                        _stage_acc[_stg] = _stage_acc.get(_stg, 0) + (_scnt or 0)
                    for _f in ("avg_expansion_ratio_merged", "avg_expansion_ratio_failed"):
                        _sum = _st.get(f"_{_f}_sum", 0.0)
                        _w = _st.get(f"_{_f}_w", 0)
                        if _w > 0:
                            scope_conversion_rates[_s][f"_{_f}_sum"] = scope_conversion_rates[_s].get(f"_{_f}_sum", 0.0) + _sum
                            scope_conversion_rates[_s][f"_{_f}_w"] = scope_conversion_rates[_s].get(f"_{_f}_w", 0) + _w
                for _s, _st in prev_dir_scope_agg.items():
                    if _s not in prev_scope_conversion_rates:
                        prev_scope_conversion_rates[_s] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                    prev_scope_conversion_rates[_s]["created"] += _st["created"]
                    prev_scope_conversion_rates[_s]["merged"] += _st["merged"]
                    prev_scope_conversion_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    prev_scope_conversion_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)

                decomposition_summary = None
                if dq_plans > 0:
                    decomposition_summary = {
                        "plans_assessed": dq_plans,
                        "avg_subtask_merge_rate": (
                            round(dq_merge_rate_sum / dq_merge_rate_count, 3)
                            if dq_merge_rate_count > 0 else None
                        ),
                        "avg_subtask_count": round(dq_subtask_count_sum / dq_plans, 2),
                        "avg_cross_module_count": round(dq_cross_module_sum / dq_plans, 2),
                    }

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

                for _sk, _agg in dir_scope_agg.items():
                    for _field in ("avg_expansion_ratio_merged", "avg_expansion_ratio_failed", "avg_complexity_merged", "avg_complexity_failed"):
                        w = _agg.pop(f"_{_field}_w", 0)
                        s = _agg.pop(f"_{_field}_sum", 0.0)
                        if w > 0:
                            _agg[_field] = round(s / w, 2)
                    if "avg_expansion_ratio_merged" in _agg:
                        _agg["avg_expansion_ratio"] = _agg["avg_expansion_ratio_merged"]

                _dir_rejections = []
                for _t in tasks_for_dir:
                    _ctx = _t.context or {}
                    if not _ctx.get("pr_rejected"):
                        continue
                    _ts = _derive_completed_at(_t)
                    if _ts < window_start:
                        continue
                    _dir_rejections.append((_ts, _t))
                _dir_rejections.sort(key=lambda x: x[0], reverse=True)

                _pq_notes = synthesize_prompt_quality_patterns(d.get("research_history") or [])
                prompt_quality_hint = _pq_notes[0] if _pq_notes else None
                rejection_reasons = [
                    {
                        "task_id": str(_t.id),
                        "title": _t.title,
                        "rejection_note": ((_t.context or {}).get("pr_rejection_note") or "")[:200],
                    }
                    for _ts, _t in _dir_rejections[:3]
                ]

                cost_budget_usd = d.get("cost_budget_usd")
                budget_utilization_pct = None
                budget_exceeded = False
                if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
                    budget_utilization_pct = round((dir_cost / cost_budget_usd) * 100, 1)
                    budget_exceeded = dir_cost > cost_budget_usd
                    if budget_utilization_pct >= 80 and not d.get("_budget_alerted"):
                        d["_budget_alerted"] = True
                        flag_modified(project, "directions")
                        _budget_alert_fired = True
                        asyncio.ensure_future(dispatch_external_notification(
                            project_id,
                            "BUDGET_EXCEEDED",
                            {
                                "direction_id": did,
                                "direction_name": direction_text,
                                "current_spend_usd": round(dir_cost, 2),
                                "budget_limit_usd": float(cost_budget_usd),
                                "utilization_pct": budget_utilization_pct,
                            },
                        ))

                direction_progress_list.append({
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cost_budget_usd": cost_budget_usd,
                    "budget_utilization_pct": budget_utilization_pct,
                    "budget_exceeded": budget_exceeded,
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "autonomous_prs_merged": autonomous_prs_merged,
                    "assisted_prs_merged": assisted_prs_merged,
                    "prs_rejected": prs_rejected,
                    "acceptance_rate": acceptance_rate,
                    "conversion_rate": conversion_rate,
                    "avg_time_to_merge_hours": avg_time_to_merge_hours,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "possibly_complete": bool(d.get("possibly_complete", False)),
                    "auto_paused": bool(d.get("auto_paused", False)),
                    "paused_reason": d.get("paused_reason"),
                    "needs_manual_intervention": bool(d.get("needs_manual_intervention", False)),
                    "completion_criteria": d.get("completion_criteria"),
                    "completion_suggestion": d.get("completion_suggestion"),
                    "category": d.get("category"),
                    "status": d.get("status"),
                    "added_at": d.get("added_at"),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                    "product_ratio": dir_product_ratio,
                    "type_distribution": agg_type_distribution or None,
                    "type_breakdown": agg_type_breakdown or None,
                    "estimated_cost_usd": round(dir_cost, 2),
                    "spot_cost_usd": round(dir_spot_cost, 2),
                    "on_demand_cost_usd": round(dir_on_demand_cost, 2),
                    "cost_by_scope": cost_by_scope or None,
                    "cost_by_status": cost_by_status or None,
                    "cost_by_type": cost_by_type or None,
                    "cost_by_provider_model": cost_by_provider_model or None,
                    "cost_per_merged_pr": round(dir_cost / autonomous_prs_merged, 4) if autonomous_prs_merged > 0 else None,
                    "scope_breakdown": dir_scope_agg or None,
                    "taxonomy_summary": dir_taxonomy_summary or None,
                    "stage_dropout": dir_stage_dropout or None,
                    "recent_failure_titles": recent_failed_titles[-3:] or None,
                    "filter_funnel": filter_funnel,
                    "decomposition_summary": decomposition_summary,
                    "first_attempt_success_rate": dir_fa_rate,
                    "first_attempt_sample_size": dir_fa_sample_size,
                    "quality_score": round(_compute_direction_quality_score(d), 3),
                    "lifetime_cycles": lifetime_cycles,
                    "lifetime_prs_merged": lifetime_prs_merged,
                    "lifetime_prs_rejected": lifetime_prs_rejected,
                    "lifetime_tasks_failed": lifetime_tasks_failed,
                    "lifetime_conversion_rate": lifetime_conversion_rate,
                    "review_quality": {
                        "reviews_dismissed": dir_reviews_dismissed,
                        "reviews_overridden": dir_reviews_overridden,
                        "user_issues_caught": dir_user_issues_caught,
                    },
                    "rejection_reasons": rejection_reasons or None,
                    "prompt_quality_hint": prompt_quality_hint,
                })

            direction_progress_list.sort(key=lambda x: x["cycles_in_window"], reverse=True)

            if _budget_alert_fired:
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()

            for _s, _agg in scope_conversion_rates.items():
                for _f in ("avg_expansion_ratio_merged", "avg_expansion_ratio_failed"):
                    _w = _agg.pop(f"_{_f}_w", 0)
                    _sum = _agg.pop(f"_{_f}_sum", 0.0)
                    if _w > 0:
                        _agg[_f] = round(_sum / _w, 2)

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
    from app.failure_classification import classify_failure as _classify_failure
    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "reason": "failed",
            "error_snippet": error_log_map.get(tid),
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "failure_class": _classify_failure(t, error_log_map.get(tid)),
            "scope": (t.context or {}).get("scope"),
            "merge_retries_saved": (t.context or {}).get("_merge_retries_saved"),
        })
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
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
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
        })
    needs_attention.sort(key=lambda x: (0 if x["reason"] == "approval_required" else 1, -(x["waiting_hours"] or 0)))

    # --- recent_failures (up to 10, desc) ---
    from app.failure_classification import (
        classify_failure as _classify_failure_rf,
        classify_timeout_subtype as _classify_timeout_subtype,
        AGENT_TIMEOUT as _AGENT_TIMEOUT,
    )
    sorted_failures = sorted(failed_tasks, key=_derive_completed_at, reverse=True)
    recent_failures = []
    for t in sorted_failures[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        task_type = (t.first_payload).get("type", "create")
        _rf_cls = _classify_failure_rf(t, error_log_map.get(tid))
        recent_failures.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "failed_at": ts.isoformat() if ts else None,
            "error_snippet": error_log_map.get(tid),
            "waiting_hours": waiting_hours,
            "attempt_count": t.attempt_count,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "task_type": task_type,
            "failure_class": _rf_cls,
            "timeout_subtype": _classify_timeout_subtype(t, error_log_map.get(tid)) if _rf_cls == _AGENT_TIMEOUT else None,
            "scope": (t.context or {}).get("scope"),
            "merge_retries_saved": (t.context or {}).get("_merge_retries_saved"),
        })

    # --- failure_groups (classify failures by type) ---
    from app.failure_classification import (
        classify_failure as _classify_failure,
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY, TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
    )
    from app.failure_stage import _derive_failure_stage
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
        cls = _classify_failure(t, error_log_map.get(tid))
        _task_cost = 0.0
        if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start:
            _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            _task_cost = _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            _cost_by_failure_class[cls] = _cost_by_failure_class.get(cls, 0.0) + _task_cost
        if cls == "PROVISION_FAILURE":
            _provision_waste_cost += _task_cost
            _pw_did = (t.context or {}).get("direction_id")
            if _pw_did:
                _pw_key = str(_pw_did)
                _pw_entry = _provision_waste_by_dir.setdefault(_pw_key, {
                    "direction_id": _pw_key,
                    "direction_text": dir_id_to_text.get(_pw_key, ""),
                    "provision_cost_usd": 0.0,
                    "provision_task_count": 0,
                })
                _pw_entry["provision_cost_usd"] += _task_cost
                _pw_entry["provision_task_count"] += 1
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        _stage = (t.context or {}).get("_previous_failure_stage")
        if not _stage:
            _stage = _derive_failure_stage(cls, t.latest_status_message or error_log_map.get(tid) or "")
        if _stage:
            _failure_stage_counts[_stage] = _failure_stage_counts.get(_stage, 0) + 1
        _failure_class_groups.setdefault(cls, []).append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "error_snippet": error_log_map.get(tid),
            "attempt_count": t.attempt_count,
            "waiting_hours": waiting_hours,
            "taxonomy": _classify_taxonomy(t),
            "timeout_subtype": _classify_timeout_subtype(t, error_log_map.get(tid)) if cls == _AGENT_TIMEOUT else None,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "failure_stage": _stage,
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
            _analysis_overflow_by_scope[_scope] = _analysis_overflow_by_scope.get(_scope, 0) + 1
            if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start:
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _analysis_overflow_cost += _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
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

    _retry_overhead_cost = 0.0
    _retry_task_count = 0
    _retry_attempt_sum = 0
    _retry_waste_by_dir: Dict[str, Dict[str, Any]] = {}
    for t in failed_tasks:
        _attempt = ((t.context or {}).get("_attempt_number") or 0)
        if _attempt > 1:
            _retry_task_count += 1
            _retry_attempt_sum += _attempt
            _task_retry_cost = 0.0
            if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start:
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _task_retry_cost = _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                _retry_overhead_cost += _task_retry_cost
            _rw_did = (t.context or {}).get("direction_id")
            if _rw_did:
                _key = str(_rw_did)
                _entry = _retry_waste_by_dir.setdefault(_key, {
                    "direction_id": _key,
                    "direction_text": dir_id_to_text.get(_key, ""),
                    "retry_cost_usd": 0.0,
                    "retry_task_count": 0,
                })
                _entry["retry_cost_usd"] += _task_retry_cost
                _entry["retry_task_count"] += 1
    retry_overhead_cost_usd = round(_retry_overhead_cost, 4)
    retry_waste_by_direction = sorted(
        [
            {**e, "retry_cost_usd": round(e["retry_cost_usd"], 4)}
            for e in _retry_waste_by_dir.values()
        ],
        key=lambda x: x["retry_cost_usd"],
        reverse=True,
    )[:5]
    retry_task_count = _retry_task_count
    avg_retries_before_failure = round(_retry_attempt_sum / _retry_task_count, 2) if _retry_task_count else None

    # Retry success/waste metrics by _previous_failure_class (the class that triggered the retry)
    _retry_success_by_class: Dict[str, int] = {}
    _retry_failure_by_class: Dict[str, int] = {}
    _retry_success_count = 0
    _retry_recovery_cost = 0.0
    for t in completed_tasks:
        _ctx = t.context or {}
        if (_ctx.get("_attempt_number") or 0) > 1:
            _retry_success_count += 1
            _cls = _ctx.get("_previous_failure_class") or "UNKNOWN"
            _retry_success_by_class[_cls] = _retry_success_by_class.get(_cls, 0) + 1
            try:
                _retry_recovery_cost += float(_ctx.get("_prior_attempts_cost") or 0.0)
            except (TypeError, ValueError):
                pass
    retry_recovery_cost_usd = round(_retry_recovery_cost, 4)
    for t in failed_tasks:
        _ctx = t.context or {}
        if (_ctx.get("_attempt_number") or 0) > 1:
            _cls = _ctx.get("_previous_failure_class") or "UNKNOWN"
            _retry_failure_by_class[_cls] = _retry_failure_by_class.get(_cls, 0) + 1
    _retry_classes = set(_retry_success_by_class) | set(_retry_failure_by_class)
    retry_success_rate_by_class = {
        _cls: {
            "merged": _retry_success_by_class.get(_cls, 0),
            "failed": _retry_failure_by_class.get(_cls, 0),
            "success_rate": round(
                _retry_success_by_class.get(_cls, 0)
                / (_retry_success_by_class.get(_cls, 0) + _retry_failure_by_class.get(_cls, 0)),
                4,
            ),
        }
        for _cls in _retry_classes
    }
    retry_success_count = _retry_success_count
    _total_retried = _retry_success_count + _retry_task_count
    retry_wasted_rate = round(_retry_task_count / _total_retried, 4) if _total_retried else None

    # Taxonomy distribution (4-category: scope/review/infra/code_defect)
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

    _stage_total = sum(_failure_stage_counts.values())
    failure_stage_distribution = {
        "stages": dict(_failure_stage_counts),
        "total_classified": _stage_total,
        "dominant_stage": (
            max(_failure_stage_counts.items(), key=lambda kv: kv[1])[0]
            if _stage_total else None
        ),
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
            "scope": (t.context or {}).get("scope"),
            "review_iterations": (t.context or {}).get("review_loop", {}).get("iteration", 0),
        })

    # --- active_tasks list (oldest started first, capped at _ACTIVE_TASKS_DISPLAY_LIMIT) ---
    _now_active = _utcnow()
    from app.review_pipeline import _max_review_iterations
    active_tasks_list = []
    for t in active_tasks_all[:_ACTIVE_TASKS_DISPLAY_LIMIT]:
        at_dir_id = (t.context or {}).get("direction_id")
        at_dir_text = dir_id_to_text.get(str(at_dir_id)) if at_dir_id else None
        if t.vm_provisioned_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            running_cost_usd = rate * (_now_active - t.vm_provisioned_at).total_seconds()
        else:
            running_cost_usd = None
        t_ctx = t.context or {}
        review_iteration = (t_ctx.get("review_loop") or {}).get("iteration", 0)
        try:
            review_cap = _max_review_iterations(t)
        except Exception:
            scope_val = t_ctx.get("scope")
            review_cap = {"small": 10, "medium": 12}.get(scope_val, 16)
        active_tasks_list.append({
            "task_id": str(t.id),
            "title": t.title or "Active task",
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "direction_id": at_dir_id,
            "direction_text": at_dir_text,
            "running_cost_usd": running_cost_usd,
            "scope": t_ctx.get("scope"),
            "review_iteration": review_iteration,
            "review_cap": review_cap,
        })

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
    total_spot_cost_usd = 0.0
    total_on_demand_cost_usd = 0.0
    for t in all_tasks:
        if not (t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start):
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
        if not (t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        prov_end = t.vm_started_at if (t.vm_started_at and t.vm_started_at >= t.vm_provisioned_at) else t.vm_stopped_at
        if prov_end <= t.vm_provisioned_at:
            continue
        _provisioning_overhead_raw += rate * (prov_end - t.vm_provisioned_at).total_seconds()
    provisioning_overhead_usd = round(_provisioning_overhead_raw, 2)
    provisioning_overhead_pct = (
        round(_provisioning_overhead_raw / _total_compute_cost_raw, 3)
        if _total_compute_cost_raw > 0 else None
    )

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

    all_cpms = [d["cost_per_merged_pr"] for d in direction_progress_list if d.get("cost_per_merged_pr") is not None]
    median_cost_per_merged_pr = round(statistics.median(all_cpms), 4) if all_cpms else None
    for d in direction_progress_list:
        cpm = d.get("cost_per_merged_pr")
        if cpm is not None and median_cost_per_merged_pr is not None:
            d["cost_per_merge_exceeds_threshold"] = cpm > (median_cost_per_merged_pr * _CPM_RETIREMENT_MULTIPLIER)
        else:
            d["cost_per_merge_exceeds_threshold"] = False

    # Per-task-type cost-per-merge: for each task type, total VM cost / merged PR count
    _type_cost: Dict[str, float] = {}
    _type_merges: Dict[str, int] = {}
    for t in all_tasks:
        if not (t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start):
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
    _median_cpm_by_type = round(statistics.median(list(_type_cpms.values())), 4) if _type_cpms else None
    cost_per_merge_by_type = {
        ttype: {
            "total_cost_usd": round(_type_cost[ttype], 4),
            "merged_count": _type_merges.get(ttype, 0),
            "cost_per_merge": round(_type_cpms[ttype], 4) if ttype in _type_cpms else None,
            "exceeds_threshold": (
                _type_cpms[ttype] > (_median_cpm_by_type * _CPM_RETIREMENT_MULTIPLIER)
                if ttype in _type_cpms and _median_cpm_by_type is not None
                else False
            ),
        }
        for ttype in _type_cost
    }

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

    # --- Scope mismatch diagnostics ---
    # Aggregate calibration data across terminal tasks in the window to surface
    # systematic scope-estimation drift and chronic-calibration directions.
    _terminal_in_window = completed_tasks + failed_tasks
    _calibrated_tasks = [
        t for t in _terminal_in_window
        if (t.context or {}).get("scope_calibrated_from")
    ]
    scope_mismatch_summary: Optional[Dict[str, Any]] = None
    if _calibrated_tasks:
        _cal_completed = sum(1 for t in _calibrated_tasks if t.status == TaskStatus.COMPLETED)
        _cal_total = len(_calibrated_tasks)
        _by_orig: Dict[str, Dict[str, int]] = {}
        _by_dir_calibrated: Dict[str, int] = {}
        for t in _calibrated_tasks:
            _orig_raw = str((t.context or {}).get("scope_calibrated_from") or "")
            _orig = _orig_raw.split(":", 1)[0] or "unknown"
            _bucket = _by_orig.setdefault(_orig, {"total": 0, "completed": 0, "failed": 0})
            _bucket["total"] += 1
            if t.status == TaskStatus.COMPLETED:
                _bucket["completed"] += 1
            elif t.status == TaskStatus.FAILED:
                _bucket["failed"] += 1
            _did = (t.context or {}).get("direction_id")
            if _did:
                _by_dir_calibrated[str(_did)] = _by_dir_calibrated.get(str(_did), 0) + 1

        _by_dir_total: Dict[str, int] = {}
        for t in _terminal_in_window:
            _did = (t.context or {}).get("direction_id")
            if _did:
                _by_dir_total[str(_did)] = _by_dir_total.get(str(_did), 0) + 1

        _top_dirs = []
        for _did, _cal_cnt in _by_dir_calibrated.items():
            _tot = _by_dir_total.get(_did, 0)
            if _tot <= 0:
                continue
            _top_dirs.append({
                "direction_id": _did,
                "direction_text": dir_id_to_text.get(_did, ""),
                "calibrated_count": _cal_cnt,
                "total_tasks": _tot,
                "calibration_rate": round(_cal_cnt / _tot, 3),
            })
        _top_dirs.sort(key=lambda d: (d["calibration_rate"], d["calibrated_count"]), reverse=True)

        scope_mismatch_summary = {
            "total_calibrated": _cal_total,
            "calibrated_success_rate": round(_cal_completed / _cal_total, 3) if _cal_total else None,
            "by_original_scope": _by_orig,
            "top_directions": _top_dirs[:3],
        }

    # --- Escalation outcome summary ---
    # Aggregate escalation_outcomes across all active directions and recent
    # research_history entries, so operators can see whether scope escalation
    # is productive or just burning compute.
    escalation_outcome_summary: Optional[Dict[str, Any]] = None
    if project_id:
        _proj_esc = await session.get(Project, project_id)
        if _proj_esc:
            _esc_paths: Dict[str, Dict[str, int]] = {}
            for _d in (_proj_esc.directions or []):
                if _d.get("status") not in ("active", "held", "auto_paused"):
                    continue
                for _entry in _d.get("research_history") or []:
                    try:
                        _ets = datetime.fromisoformat(_entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if _ets < window_start:
                        continue
                    for _path, _counts in (_entry.get("escalation_outcomes") or {}).items():
                        if not isinstance(_counts, dict):
                            continue
                        _b = _esc_paths.setdefault(
                            str(_path), {"attempted": 0, "merged": 0, "failed": 0}
                        )
                        _b["attempted"] += int(_counts.get("attempted") or 0)
                        _b["merged"] += int(_counts.get("merged") or 0)
                        _b["failed"] += int(_counts.get("failed") or 0)
            _by_path: Dict[str, Dict[str, Any]] = {}
            _tot_att = _tot_mer = _tot_fail = 0
            for _path, _b in _esc_paths.items():
                _att = _b["attempted"]
                _by_path[_path] = {
                    "attempted": _att,
                    "merged": _b["merged"],
                    "failed": _b["failed"],
                    "success_rate": round(_b["merged"] / _att, 3) if _att else None,
                }
                _tot_att += _att
                _tot_mer += _b["merged"]
                _tot_fail += _b["failed"]
            escalation_outcome_summary = {
                "total_attempted": _tot_att,
                "total_merged": _tot_mer,
                "total_failed": _tot_fail,
                "overall_success_rate": round(_tot_mer / _tot_att, 3) if _tot_att else None,
                "by_path": _by_path,
            }

    # --- Prevention efficacy summary ---
    # Aggregate preventive-context outcomes across recent research_history
    # entries so operators can see whether proactive context injection actually
    # improves merge rates vs. the overall pipeline.
    prevention_efficacy: Optional[Dict[str, Any]] = None
    if project_id:
        _proj_prev = await session.get(Project, project_id)
        if _proj_prev:
            _prev_created = 0
            _prev_merged = 0
            _prev_failed = 0
            _all_created = 0
            _all_merged = 0
            for _d in (_proj_prev.directions or []):
                for _entry in _d.get("research_history") or []:
                    try:
                        _ets = datetime.fromisoformat(_entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if _ets < window_start:
                        continue
                    _prev_created += int(_entry.get("preventive_context_tasks_created") or 0)
                    _prev_merged += int(_entry.get("preventive_context_tasks_merged") or 0)
                    _prev_failed += int(_entry.get("preventive_context_tasks_failed") or 0)
                    _all_created += int(_entry.get("tasks_created") or 0)
                    _all_merged += int(_entry.get("prs_merged") or 0)
            if _prev_created == 0:
                prevention_efficacy = {
                    "preventive_tasks_created": 0,
                    "preventive_tasks_merged": 0,
                    "preventive_tasks_failed": 0,
                    "preventive_merge_rate": None,
                    "overall_merge_rate": round(_all_merged / _all_created, 3) if _all_created else None,
                    "assessment": "no_data",
                }
            else:
                _prev_rate = _prev_merged / _prev_created
                _overall_rate = (_all_merged / _all_created) if _all_created else None
                if _overall_rate is None:
                    _assessment = "no_data"
                else:
                    _delta = _prev_rate - _overall_rate
                    if _delta > 0.05:
                        _assessment = "positive"
                    elif _delta < -0.05:
                        _assessment = "negative"
                    else:
                        _assessment = "neutral"
                prevention_efficacy = {
                    "preventive_tasks_created": _prev_created,
                    "preventive_tasks_merged": _prev_merged,
                    "preventive_tasks_failed": _prev_failed,
                    "preventive_merge_rate": round(_prev_rate, 3),
                    "overall_merge_rate": round(_overall_rate, 3) if _overall_rate is not None else None,
                    "assessment": _assessment,
                }

    # --- Latest strategic cycle + prediction calibration ---
    latest_cycle = None
    prediction_calibration: Dict[str, Any] | None = None
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
            _sc_history = await _strategic_cycle.recent_for_agent_context(
                session, project_id, limit=20,
            )
            prediction_calibration = _compute_prediction_calibration(_proj, _sc_history)

    _rejected_in_window = [
        t for t in all_tasks
        if (t.context or {}).get("pr_rejected")
        and _derive_completed_at(t) >= window_start
    ]
    _note_counts: Dict[str, int] = {}
    for _t in _rejected_in_window:
        _note = ((_t.context or {}).get("pr_rejection_note") or "").strip()
        if not _note:
            continue
        _snip = " ".join(_note.lower().split())[:120]
        _note_counts[_snip] = _note_counts.get(_snip, 0) + 1
    _top_notes = sorted(_note_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    rejection_pattern_summary = {
        "total_rejected": len(_rejected_in_window),
        "top_notes": [{"snippet": s, "count": c} for s, c in _top_notes],
    }

    # --- Retry pattern summary ---
    # Analyze failed tasks whose _failure_class_history has 2+ entries to see
    # whether retries changed the failure class or repeated the same one.
    _multi_attempt = []
    for _t in failed_tasks:
        _hist = failure_class_history_classes((_t.context or {}).get("_failure_class_history"))
        if len(_hist) >= 2:
            _multi_attempt.append(_hist)
    _repeat_count = sum(
        1 for h in _multi_attempt
        if any(h[i] == h[i + 1] for i in range(len(h) - 1))
    )
    _pattern_counts: Dict[str, int] = {}
    for _hist in _multi_attempt:
        _key = " -> ".join(_hist)
        _pattern_counts[_key] = _pattern_counts.get(_key, 0) + 1
    _top_patterns = sorted(_pattern_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    retry_pattern_summary = {
        "total_multi_attempt_failures": len(_multi_attempt),
        "repeat_class_count": _repeat_count,
        "top_patterns": [{"sequence": s, "count": c} for s, c in _top_patterns],
    }

    # --- Review cap exhaustion summary ---
    # Tasks that FAILED because the review-fix cycle could not converge
    # (failure_detail=review_cap_exhausted) — distinct from tasks that failed
    # because the code itself was wrong.
    _cap_tasks = [
        t for t in failed_tasks
        if (t.context or {}).get("failure_detail") == "review_cap_exhausted"
    ]
    _cap_iters: List[int] = []
    _cap_dir_counts: Dict[str, int] = {}
    _cap_total_cost = 0.0
    for _t in _cap_tasks:
        _ctx = _t.context or {}
        _it = _ctx.get("review_cap_iterations")
        if isinstance(_it, (int, float)):
            _cap_iters.append(int(_it))
        _did = _ctx.get("direction_id")
        if _did:
            _did_s = str(_did)
            _cap_dir_counts[_did_s] = _cap_dir_counts.get(_did_s, 0) + 1
        if _t.vm_provisioned_at and _t.vm_stopped_at and _t.vm_stopped_at >= window_start:
            _rate = SPOT_RATE if _t.vm_spot else ON_DEMAND_RATE
            _cap_total_cost += _rate * (_t.vm_stopped_at - _t.vm_provisioned_at).total_seconds()
    _top_cap_dirs = sorted(_cap_dir_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    review_cap_exhaustion_summary = {
        "count": len(_cap_tasks),
        "avg_iterations": (sum(_cap_iters) / len(_cap_iters)) if _cap_iters else None,
        "top_directions": [{"direction_id": d, "count": c} for d, c in _top_cap_dirs],
        "total_cost_usd": round(_cap_total_cost, 4),
        "avg_cost_usd": round(_cap_total_cost / len(_cap_tasks), 4) if _cap_tasks else None,
    }

    # --- Recurring error fingerprint clusters ---
    # Group failed tasks by normalized error fingerprint to surface systemic
    # failures manifesting across multiple tasks.
    from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415
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
    recurring_error_clusters = [
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

    total_merge_retries_saved = 0
    merge_early_exit_count = 0
    for _t in completed_tasks + failed_tasks:
        _saved = (_t.context or {}).get("_merge_retries_saved")
        if isinstance(_saved, int) and _saved > 0:
            total_merge_retries_saved += _saved
            merge_early_exit_count += 1

    # Resurrection outcomes: count tasks the reaper auto-resurrected and how many
    # of those still ended up failing (net-positive signal for operators).
    _resurrected_completed = 0
    _resurrected_failed = 0
    for _t in completed_tasks:
        if (_t.context or {}).get("_resurrection_count", 0) >= 1:
            _resurrected_completed += 1
    for _t in failed_tasks:
        if (_t.context or {}).get("_resurrection_count", 0) >= 1:
            _resurrected_failed += 1
    _resurrected_total = _resurrected_completed + _resurrected_failed
    resurrection_outcome_summary = {
        "total": _resurrected_total,
        "completed": _resurrected_completed,
        "failed_again": _resurrected_failed,
        "success_rate": (_resurrected_completed / _resurrected_total) if _resurrected_total else None,
    }

    # Remediation funnel: investigation -> diagnosis -> remediation -> merge.
    # Query investigate + remediation tasks in-window regardless of status (the
    # main all_tasks query is terminal-only, so investigate tasks that are still
    # IN_PROGRESS would otherwise be missed).
    _funnel_filters = [Task.created_at >= window_start]
    if project_id:
        _funnel_filters.append(Task.project_id == project_id)
    _inv_result = await session.execute(
        select(Task).where(
            *_funnel_filters,
            Task.payloads[0]["type"].as_string() == "investigate",
        )
    )
    _inv_tasks = _inv_result.scalars().all()
    _rem_result = await session.execute(
        select(Task).where(
            *_funnel_filters,
            Task.context["diagnosis_task_id"].as_string().is_not(None),
        )
    )
    _rem_tasks = _rem_result.scalars().all()

    investigations_spawned = len(_inv_tasks)
    investigations_completed = sum(1 for t in _inv_tasks if t.status == TaskStatus.COMPLETED)
    investigations_with_diagnosis = sum(
        1 for t in _inv_tasks
        if t.status == TaskStatus.COMPLETED
        and isinstance(t.plan_output, dict)
        and str((t.plan_output or {}).get("summary") or "").strip()
    )
    active_investigations = sum(
        1 for t in _inv_tasks
        if t.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING)
    )
    remediations_spawned = len(_rem_tasks)
    remediations_merged = sum(1 for t in _rem_tasks if t.status == TaskStatus.COMPLETED)
    remediations_failed = sum(1 for t in _rem_tasks if t.status == TaskStatus.FAILED)

    _held_projects_q = select(Project)
    if project_id:
        _held_projects_q = _held_projects_q.where(Project.id == project_id)
    _held_projects = (await session.execute(_held_projects_q)).scalars().all()
    fixes_held = 0
    _window_iso = window_start.isoformat()
    for _p in _held_projects:
        for _d in (_p.directions or []):
            for _h in (_d.get("remediation_history") or []):
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
            delta = (completed_at - inv.created_at).total_seconds() / 3600.0
            if delta >= 0:
                _ttr_values.append(delta)
    avg_time_to_remediation_hours = (
        round(sum(_ttr_values) / len(_ttr_values), 2) if _ttr_values else None
    )

    investigations_by_confidence: Dict[str, int] = {}
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED or not isinstance(t.plan_output, dict):
            continue
        conf = str((t.plan_output or {}).get("confidence") or "unknown").strip().lower() or "unknown"
        investigations_by_confidence[conf] = investigations_by_confidence.get(conf, 0) + 1

    failed_remediations_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        if r.status != TaskStatus.FAILED:
            continue
        cls = _classify_failure(r) or "unknown"
        failed_remediations_by_class[cls] = failed_remediations_by_class.get(cls, 0) + 1

    investigations_spawned_by_class: Dict[str, int] = {}
    investigations_completed_by_class: Dict[str, int] = {}
    for t in _inv_tasks:
        cls = str((t.context or {}).get("last_failure_class") or "unknown").strip() or "unknown"
        investigations_spawned_by_class[cls] = investigations_spawned_by_class.get(cls, 0) + 1
        if t.status == TaskStatus.COMPLETED:
            investigations_completed_by_class[cls] = investigations_completed_by_class.get(cls, 0) + 1

    remediations_spawned_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        cls = str((r.context or {}).get("trigger_failure_class") or "unknown").strip() or "unknown"
        remediations_spawned_by_class[cls] = remediations_spawned_by_class.get(cls, 0) + 1

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
        merged_remediations_by_trigger_class[key] = merged_remediations_by_trigger_class.get(key, 0) + 1

    remediation_success_by_confidence: Dict[str, Dict[str, int]] = {}
    for r in _rem_tasks:
        if r.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            continue
        diag_id = (r.context or {}).get("diagnosis_task_id")
        inv = _inv_by_id.get(str(diag_id)) if diag_id else None
        conf = "unknown"
        if inv is not None and isinstance(inv.plan_output, dict):
            conf = str((inv.plan_output or {}).get("confidence") or "unknown").strip().lower() or "unknown"
        bucket = remediation_success_by_confidence.setdefault(conf, {"merged": 0, "failed": 0})
        if r.status == TaskStatus.COMPLETED:
            bucket["merged"] += 1
        else:
            bucket["failed"] += 1

    _completed_inv = [
        t for t in _inv_tasks
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
        recent_investigations.append({
            "task_id": str(t.id),
            "direction_id": (t.context or {}).get("direction_id"),
            "summary": summary,
            "confidence": str(po.get("confidence") or "").strip().lower() or None,
            "severity": str(po.get("severity") or "").strip().lower() or None,
            "completed_at": completed_at.isoformat() if completed_at else None,
        })

    active_investigations_detail: List[Dict[str, Any]] = []
    _now = _utcnow()
    for t in _inv_tasks:
        if t.status not in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
            continue
        elapsed_hours = None
        if t.created_at:
            elapsed_hours = round((_now - t.created_at).total_seconds() / 3600.0, 2)
        active_investigations_detail.append({
            "task_id": str(t.id),
            "direction_id": (t.context or {}).get("direction_id"),
            "elapsed_hours": elapsed_hours,
        })

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
            1 for t in _ci_rem_tasks
            if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged") is True
        ),
        "failed": sum(1 for t in _ci_rem_tasks if t.status == TaskStatus.FAILED),
        "by_ci_failure_type": {},
    }
    _ci_type_buckets: Dict[str, Dict[str, int]] = {}
    for t in _ci_rem_tasks:
        ci_type = str((t.context or {}).get("ci_failure_type") or "unknown").strip() or "unknown"
        bucket = _ci_type_buckets.setdefault(ci_type, {"spawned": 0, "merged": 0, "failed": 0})
        bucket["spawned"] += 1
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged") is True:
            bucket["merged"] += 1
        elif t.status == TaskStatus.FAILED:
            bucket["failed"] += 1
    ci_auto_remediation["by_ci_failure_type"] = _ci_type_buckets

    # --- Residual remediation funnel (sourced from ResidualRemediationCase rows) ---
    _rr_spawned_filters = [ResidualRemediationCase.last_spawned_at >= window_start]
    if project_id:
        _rr_spawned_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_spawned_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_spawned_filters)
    )
    _rr_cases = list(_rr_spawned_result.scalars().all())
    _rr_active_filters = [
        ResidualRemediationCase.status == ResidualRemediationCaseStatus.REMEDIATING,
    ]
    if project_id:
        _rr_active_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_active_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_active_filters)
    )
    _rr_open_filters = [
        ResidualRemediationCase.status.in_([
            ResidualRemediationCaseStatus.ACTIVE,
            ResidualRemediationCaseStatus.REMEDIATING,
            ResidualRemediationCaseStatus.SUSPENDED,
        ]),
    ]
    if project_id:
        _rr_open_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_open_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_open_filters).order_by(ResidualRemediationCase.last_seen_at.desc())
    )
    _rr_open_cases = list(_rr_open_result.scalars().all())
    residual_remediation_funnel: Dict[str, Any] = {
        "spawned": len(_rr_cases),
        "merged": sum(1 for c in _rr_cases if c.status == ResidualRemediationCaseStatus.MERGED),
        "failed": sum(1 for c in _rr_cases if c.status in (
            ResidualRemediationCaseStatus.FAILED, ResidualRemediationCaseStatus.SUSPENDED
        )),
        "remediating": len(list(_rr_active_result.scalars().all())),
        "open_cases_by_error_class": [
            {
                "error_class": "CODE_ERROR",
                "count": len(_rr_open_cases),
                "most_recent_at": _rr_open_cases[0].last_seen_at.isoformat() if _rr_open_cases else None,
                "cases": [
                    {
                        "fingerprint": c.fingerprint,
                        "status": c.status,
                        "hit_count": c.hit_count,
                        "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                        "last_spawned_at": c.last_spawned_at.isoformat() if c.last_spawned_at else None,
                        "sample_message": c.sample_message,
                        "consecutive_failure_count": c.consecutive_failure_count,
                    }
                    for c in _rr_open_cases[:30]
                ],
            }
        ] if _rr_open_cases else [],
    }

    _prior_tasks = [t for t in _ci_rem_tasks if (t.context or {}).get("prior_diagnosis_task_id")]
    _total_with_prior = len(_prior_tasks)
    _merged_with_prior = sum(1 for t in _prior_tasks if t.status == TaskStatus.COMPLETED)
    _failed_with_prior = sum(1 for t in _prior_tasks if t.status == TaskStatus.FAILED)
    _prior_resolved = _merged_with_prior + _failed_with_prior
    prior_investigation_reuse: Dict[str, Any] = {
        "total_with_prior": _total_with_prior,
        "merged_with_prior": _merged_with_prior,
        "failed_with_prior": _failed_with_prior,
        "merge_rate": round(_merged_with_prior / _prior_resolved, 4) if _prior_resolved else None,
    }

    _cum_filters = [
        Task.status == TaskStatus.COMPLETED,
        or_(
            Task.context["diagnosis_task_id"].as_string().is_not(None),
            Task.auto_remediation.is_(True),  # promoted column (audit W1)
        ),
        Task.context["pr_merged"].as_boolean() == True,  # noqa: E712
    ]
    if project_id:
        _cum_filters.append(Task.project_id == project_id)
    _cum_result = await session.execute(select(Task).where(*_cum_filters))
    cumulative_merged_by_trigger_class: Dict[str, int] = {}
    for r in _cum_result.scalars().all():
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
        cumulative_merged_by_trigger_class[key] = cumulative_merged_by_trigger_class.get(key, 0) + 1

    from app.task_completion import _is_structural_diagnosis

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
        parseable = bool(po) and any(str(po.get(k) or "").strip() for k in _PARSEABLE_KEYS)
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
        diagnosis_drop_off_by_reason[reason] = diagnosis_drop_off_by_reason.get(reason, 0) + 1

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

    # --- Anomaly-driven pipeline adjustment snapshot ---
    from app.shared_state import anomaly_adjustments as _anomaly_adjustments  # noqa: PLC0415
    anomaly_adjustment_summary = {
        "active": bool(_anomaly_adjustments.get("active")),
        "adjusted_at": _anomaly_adjustments.get("adjusted_at"),
        "trigger_anomalies": list(_anomaly_adjustments.get("trigger_anomalies") or []),
        "pre_adjustment_max_vms": _anomaly_adjustments.get("pre_adjustment_max_vms"),
        "adjusted_max_vms": _anomaly_adjustments.get("adjusted_max_vms"),
        "failure_gate_threshold": _anomaly_adjustments.get("failure_gate_threshold"),
        "pre_adjustment_failure_gate": _anomaly_adjustments.get("pre_adjustment_failure_gate"),
        "forced_restoration_count": _anomaly_adjustments.get("forced_restoration_count", 0),
        "forced_restored_at": _anomaly_adjustments.get("forced_restored_at"),
    }

    # --- Recurring failure patterns (from persistent_anomalies) ---
    from app.shared_state import persistent_anomalies as _persistent_anomalies  # noqa: PLC0415
    _fp_48h_cutoff = _utcnow() - timedelta(hours=48)
    _fp_dirs_48h: Dict[str, set] = {}
    _fp_count_48h: Dict[str, int] = {}
    for _t in failed_tasks:
        if _derive_completed_at(_t) < _fp_48h_cutoff:
            continue
        _msg = _t.latest_status_message or ""
        if not _msg:
            continue
        _fp_ = _error_fingerprint(_msg)
        _did = (_t.context or {}).get("direction_id")
        if _did:
            _fp_dirs_48h.setdefault(_fp_, set()).add(str(_did))
        _fp_count_48h[_fp_] = _fp_count_48h.get(_fp_, 0) + 1

    _fp_task_states: Dict[str, Dict[str, Any]] = {}
    _inflight_statuses = {TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING}
    for _t in list(all_tasks) + list(active_tasks_all):
        _ctx = _t.context or {}
        # Audit W1: previous_error_fingerprint is a column.
        _fp_ = (
            _ctx.get("diagnosis_fingerprint")
            or _ctx.get("error_fingerprint")
            or _t.previous_error_fingerprint
        )
        if not _fp_:
            continue
        _entry = _fp_task_states.setdefault(str(_fp_), {"inflight": False, "merged": False, "proactive": False})
        if _t.status == TaskStatus.COMPLETED and _ctx.get("pr_url"):
            _entry["merged"] = True
        if _t.status in _inflight_statuses:
            _entry["inflight"] = True
        if _ctx.get("proactive") or _ctx.get("proactive_investigation"):
            _entry["proactive"] = True

    recurring_patterns: List[Dict[str, Any]] = []
    early_warnings: List[Dict[str, Any]] = []
    _anoms_map = (
        {str(project_id): _persistent_anomalies.get(str(project_id), {})}
        if project_id else _persistent_anomalies
    )
    import re as _re  # noqa: PLC0415
    _momentum_rate_re = _re.compile(r"(\d+)%")
    _recent_24h_cutoff = _utcnow() - timedelta(hours=24)
    _dirs_last_24h = {
        str((_t.context or {}).get("direction_id"))
        for _t in failed_tasks
        if (_t.context or {}).get("direction_id")
        and _derive_completed_at(_t) >= _recent_24h_cutoff
    }
    for _proj_anoms in _anoms_map.values():
        for _key, _info in _proj_anoms.items():
            if _info.get("occurrence_count", 0) < 2:
                continue
            if _key.startswith("FailureRateMomentum"):
                _pcts = _momentum_rate_re.findall(_info.get("latest_details") or "")[:3]
                early_warnings.append({
                    "failure_class": _key,
                    "rate_progression": [int(p) / 100 for p in _pcts] if len(_pcts) == 3 else [],
                    "occurrence_count": _info.get("occurrence_count", 0),
                    "first_seen": _info.get("first_seen"),
                    "last_seen": _info.get("last_seen"),
                    "severity": _info.get("severity"),
                    "details": _info.get("latest_details") or "",
                    "affected_direction_count": len(_dirs_last_24h),
                    "suggested_action": (
                        "Investigate the upward trend before it trips the surge "
                        "threshold — check recent deploys, infra health, and "
                        "direction-level failure patterns."
                    ),
                })
                continue
            _fp_key = _key[len("CrossDirectionFingerprint_"):] if _key.startswith("CrossDirectionFingerprint_") else _key
            _state = _fp_task_states.get(_fp_key, {})
            _recent = _fp_count_48h.get(_fp_key, 0)
            if _state.get("merged") and _recent == 0:
                _status = "fix_verified"
            elif _state.get("merged"):
                _status = "fix_shipped"
            elif _state.get("inflight"):
                _status = "investigating"
            else:
                _status = "unaddressed"
            _affected_dir_ids = sorted(_fp_dirs_48h.get(_fp_key, set()))
            recurring_patterns.append({
                "fingerprint": _fp_key,
                "occurrence_count": _info.get("occurrence_count", 0),
                "first_seen": _info.get("first_seen"),
                "last_seen": _info.get("last_seen"),
                "severity": _info.get("severity"),
                "example_message": (_info.get("latest_details") or "")[:240],
                "affected_direction_count": len(_affected_dir_ids),
                "affected_directions": [
                    {"direction_id": _did, "direction_text": dir_id_to_text.get(_did, "")}
                    for _did in _affected_dir_ids
                ],
                "remediation_status": _status,
                "proactive": bool(_state.get("proactive", False)),
            })
    recurring_patterns.sort(key=lambda x: x["occurrence_count"], reverse=True)

    _review_quality = _compute_review_quality_metrics(completed_tasks + failed_tasks)
    _first_attempt_success_rate = _compute_first_attempt_breakdowns(
        completed_tasks + failed_tasks,
        prev_terminal_tasks=prev_completed_tasks + prev_failed_tasks,
    )
    _first_review_approval_rate = _compute_first_review_approval_breakdowns(completed_tasks + failed_tasks)

    from app.main import EXPECTED_SCHEMA_REVISION
    from app.failure_classification import classify_failure as _mig_classify

    def _task_type(t: Task) -> Optional[str]:
        return (t.first_payload or {}).get("type")

    def _touched_migration(t: Task) -> bool:
        ctx = t.context or {}
        files = ctx.get("related_files") or []
        if isinstance(files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/") for f in files
        ):
            return True
        payload_files = (t.first_payload or {}).get("related_files") or []
        return isinstance(payload_files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/") for f in payload_files
        )

    _migrate_completed = [t for t in completed_tasks if _task_type(t) == "migrate"]
    _migrate_failed = [t for t in failed_tasks if _task_type(t) == "migrate"]
    _migration_errors = sum(
        1 for t in failed_tasks
        if (t.context or {}).get("failure_class") == "MIGRATION_ERROR"
        or _mig_classify(t, error_log_map.get(str(t.id))) == "MIGRATION_ERROR"
    )
    _auto_generated_migrations = sum(
        1 for t in (completed_tasks + failed_tasks)
        if _task_type(t) != "migrate" and _touched_migration(t)
    )
    migration_health = {
        "current_schema_revision": EXPECTED_SCHEMA_REVISION,
        "migration_tasks_in_window": len(_migrate_completed) + len(_migrate_failed),
        "migration_tasks_succeeded": len(_migrate_completed),
        "migration_tasks_failed": len(_migrate_failed),
        "migration_errors_in_window": _migration_errors,
        "auto_generated_migrations": _auto_generated_migrations,
    }

    return {
        "window_hours": hours,
        "tasks_truncated": tasks_capped,
        "total_merge_retries_saved": total_merge_retries_saved,
        "first_attempt_success_rate": _first_attempt_success_rate,
        "first_review_approval_rate": _first_review_approval_rate,
        "self_review_effectiveness": _review_quality["self_review_effectiveness"],
        "self_review_prediction_accuracy": _review_quality["self_review_prediction_accuracy"],
        "review_accuracy": _review_quality["review_accuracy"],
        "merge_early_exit_count": merge_early_exit_count,
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
        "failure_stage_distribution": failure_stage_distribution,
        "recent_completions": recent_completions,
        "direction_progress": direction_progress_list,
        "overall_product_ratio": overall_product_ratio,
        "median_cost_per_merged_pr": median_cost_per_merged_pr,
        "cost_per_merge_by_type": cost_per_merge_by_type,
        "median_cost_per_merge_by_type": _median_cpm_by_type,
        "overall_type_distribution": overall_type_distribution or None,
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "spot_cost_usd": total_spot_cost_usd,
        "on_demand_cost_usd": total_on_demand_cost_usd,
        "provisioning_overhead_usd": provisioning_overhead_usd,
        "provisioning_overhead_pct": provisioning_overhead_pct,
        "cost_by_failure_class": cost_by_failure_class,
        "total_failed_cost_usd": total_failed_cost_usd,
        "provision_waste_cost_usd": provision_waste_cost_usd,
        "provision_waste_by_direction": provision_waste_by_direction,
        "analysis_overflow_summary": analysis_overflow_summary,
        "retry_overhead_cost_usd": retry_overhead_cost_usd,
        "retry_recovery_cost_usd": retry_recovery_cost_usd,
        "retry_waste_by_direction": retry_waste_by_direction,
        "retry_task_count": retry_task_count,
        "avg_retries_before_failure": avg_retries_before_failure,
        "retry_success_count": retry_success_count,
        "retry_success_rate_by_class": retry_success_rate_by_class,
        "retry_wasted_rate": retry_wasted_rate,
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
        "scope_mismatch_summary": scope_mismatch_summary,
        "escalation_outcome_summary": escalation_outcome_summary,
        "prevention_efficacy": prevention_efficacy,
        "rejection_pattern_summary": rejection_pattern_summary,
        "retry_pattern_summary": retry_pattern_summary,
        "review_cap_exhaustion_summary": review_cap_exhaustion_summary,
        "resurrection_outcome_summary": resurrection_outcome_summary,
        "recurring_error_clusters": recurring_error_clusters,
        "recurring_patterns": recurring_patterns,
        "early_warnings": early_warnings,
        "remediation_funnel": remediation_funnel,
        "investigation_conversion": investigation_conversion,
        "anomaly_adjustment": anomaly_adjustment_summary,
        "migration_health": migration_health,
        "prediction_calibration_factor": prediction_calibration["factor"] if prediction_calibration else None,
        "prediction_calibration_note": prediction_calibration["note"] if prediction_calibration else None,
        "prediction_calibration_sample_count": prediction_calibration["sample_count"] if prediction_calibration else None,
    }
