"""Catch-up digest and pipeline health summary endpoints."""

import asyncio
import re as _re
import uuid
import logging
import statistics
from collections import defaultdict
from pathlib import Path as _Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field as _Field
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.notification_dispatch import (
    dispatch_external_notification,
    post_residual_fix_unsuspend_message,
)

from app.database import get_session
from app.digest_cache import DigestCache, get_digest_cache
from app.time_utils import _to_naive_utc
from app.models import (
    Task,
    TaskStatus,
    Project,
    Artifact,
    ArtifactType,
    _utcnow,
    MaxPlanAccount,
    ResidualRemediationCase,
    ResidualRemediationCaseStatus,
    RegressionRemediationCase,
    RegressionCaseStatus,
    ReleaseVersion,
    StrategicCycle,
    Alert,
    AlertSeverity,
    CLOSE_REASON_RECOVERED_UNATTENDED,
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
    QualificationVerdict,
)
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role
from app.shared_state import runtime_config as _runtime_config
from app.routers.admin_analytics import _derive_completed_at, ON_DEMAND_RATE, SPOT_RATE
from app.direction_metrics import _compute_direction_quality_score
from app.direction_outcomes import synthesize_prompt_quality_patterns
from app.retry_budget import failure_class_history_classes
from app import strategic_cycle as _strategic_cycle
from app import release_version as _release_version
from app.strategic_analysis import _compute_prediction_calibration
from app.playbook_learning import (
    _MIN_DIRECTION_SR_SAMPLE,
    _categorize_review_issues,
    compute_direction_sr_breakdown_for_endpoint,
    compute_sr_quality_by_fingerprint,
    resolve_sr_miss_human_truth,
)
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    aggregate_sr_blind_spot_envelope,
    aggregate_sr_reviewer_disagreements,
    is_skipped_sr_outcome as _is_skipped_sr_outcome_shared,
)
from app.research_completion import lookup_findings_by_source_ref

logger = logging.getLogger(__name__)

_SR_SEVERITY_WEIGHTS: Dict[str, int] = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-digest"],
    dependencies=[Depends(get_current_user)],
)

_CPM_RETIREMENT_MULTIPLIER = 3.0  # cost_per_merge > median × this → retirement signal
_REGRESSION_STALL_THRESHOLD_HOURS = (
    4  # ACTIVE case without PR older than this → stalled
)
_SIGNAL_STALENESS_TITLE_PREFIX = "Signal staleness: "
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
    doc = project.design_doc or {}
    sr_trends = doc.get("sr_catch_rate_trends") or {}
    if sr_trends.get("prompt_version_changed"):
        return
    categories = sr_trends.get("categories") or {}
    for cat, target in targets.items():
        try:
            target_float = float(target)
        except (TypeError, ValueError):
            continue
        cat_data = categories.get(cat) or {}
        current_hit_rate = cat_data.get("current_hit_rate")
        title = f"{_SR_CATCH_RATE_TARGET_ALERT_PREFIX}{cat}"

        existing = (
            (
                await session.execute(
                    select(Alert).where(
                        Alert.project_id == project_id,
                        Alert.task_id.is_(None),
                        Alert.severity == AlertSeverity.WARNING,
                        Alert.dismissed_at.is_(None),
                        Alert.title == title,
                    )
                )
            )
            .scalars()
            .first()
        )
        if current_hit_rate is not None and current_hit_rate >= target_float:
            if existing is not None:
                existing.dismissed_at = _utcnow()
                await session.commit()
            continue
        # Gate on finding count (true denominator) not task count to avoid spurious
        # alerts when the window clears the task-count bar but rests on a handful of findings.
        current_human_count = cat_data.get("current_human_count") or 0
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
    doc = project.design_doc or {}
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
        )
    )
    await session.commit()


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


_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}

# Re-exported from app.sr_blind_spots so existing call sites keep working
# after the aggregation logic was extracted to be shared with the agent
# router. Tests and other modules import this from admin_digest today.
_is_skipped_sr_outcome = _is_skipped_sr_outcome_shared


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
    serialized = {
        "computed_at": sr_trends.get("computed_at"),
        "signal_last_computed_at": sr_trends.get("signal_last_computed_at")
        or sr_trends.get("computed_at"),
        "categories": sr_trends.get("categories") or {},
        "by_task_type": sr_trends.get("by_task_type") or {},
        "by_fix_iteration": sr_trends.get("by_fix_iteration") or {},
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


def _compute_sr_prediction_accuracy(
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
    self_review_prediction_accuracy = _compute_sr_prediction_accuracy(tasks_with_both)
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


_LAYER_SUMMARY_WINDOW = timedelta(hours=48)


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


_REVIEW_ELIGIBLE_TYPES = {"create", "refactor"}


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
            return datetime.fromisoformat(ts_str or "") >= cutoff
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
                Task.context,
                Task.latest_status_message,
                Task.payloads,
                Task.status_log,
            )
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

            dir_conversion_rate = (
                round(prs_merged / tasks_created_total, 3)
                if tasks_created_total > 0
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
                }
            )

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
    pipeline_conversion_rate = (
        round(_pipeline_total_merged / _pipeline_total_created, 3)
        if _pipeline_total_created > 0
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
        first_success = sum(
            1
            for t in tasks
            if (t.context or {}).get("_first_attempt_succeeded") is True
        )
        # Only count tasks that have the metric recorded
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(
            1 for t in measured if t.context["_first_attempt_succeeded"]
        )
        return round(first_success / len(measured), 2)

    _review_eligible_types = {"create", "refactor"}

    def _first_review_approval_rate(tasks):
        """Fraction of review-eligible merged tasks that had no first-review rejection.

        A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
        A task 'approved on first review' if it does NOT have _first_attempt_rejection_categories
        in its context (meaning the first review returned APPROVE, not REQUEST_CHANGES).
        """
        eligible = [
            t
            for t in tasks
            if (t.first_payload or {}).get("type", "create") in _review_eligible_types
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
            check_results = (t.context or {}).get("_self_review_outcome", {}).get(
                "check_results"
            ) or {}
            all_categories.update(check_results.keys())

        category_correlation: Dict[str, Any] = {}
        for cat in all_categories:
            tasks_cat_findings = [
                t
                for t in tasks_with_self_review
                if not (t.context or {})
                .get("_self_review_outcome", {})
                .get("check_results", {})
                .get(cat, {})
                .get("passed", True)
            ]
            tasks_cat_clean = [
                t
                for t in tasks_with_self_review
                if cat
                in (
                    (t.context or {})
                    .get("_self_review_outcome", {})
                    .get("check_results")
                    or {}
                )
                and (t.context or {})
                .get("_self_review_outcome", {})
                .get("check_results", {})
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
        misses = sum(
            1 for t in review_tasks if (t.context or {}).get("user_caught_issues")
        )
        total_approvals = sum(
            1
            for t in review_tasks
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

    # SR catch-rate trends from compute_sr_catch_rate_trends stored in project.design_doc.
    self_review_prediction_accuracy_trends: Optional[Dict[str, Any]] = None
    sr_regression_risk_test_signal_trends: Optional[Dict[str, Any]] = None
    sr_false_attestation_trends: Optional[Dict[str, Any]] = None
    if project and project.design_doc:
        sr_trends = project.design_doc.get("sr_catch_rate_trends") or {}
        if sr_trends:
            self_review_prediction_accuracy_trends = _serialize_sr_catch_rate_trends(
                sr_trends
            )
        sr_rrts = project.design_doc.get("sr_regression_risk_test_signal_trends")
        if sr_rrts:
            sr_regression_risk_test_signal_trends = sr_rrts
        sft = project.design_doc.get("sr_false_attestation_trends")
        if sft:
            sr_false_attestation_trends = sft

    # Fresh-reviewer cycle-over-cycle trend from compute_fresh_reviewer_trend stored in project.design_doc.
    fresh_reviewer_trend: Optional[Dict[str, Any]] = None
    if project and project.design_doc:
        fr_trends = project.design_doc.get("fresh_reviewer_trend") or {}
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

    # Edges-considered cycle-over-cycle trend from compute_edges_trend stored in project.design_doc.
    edges_trend: Optional[Dict[str, Any]] = None
    if project and project.design_doc:
        et = project.design_doc.get("edges_trend") or {}
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
    if project and project.design_doc:
        dfa = project.design_doc.get("direction_first_attempt_feedback")
        if dfa:
            direction_first_attempt_feedback = dfa

    # Idle auto-playbook retirements in the last 24h — surfaces accidental
    # over-retirement of auto-generated entries whose last_referenced_at
    # crossed the idle threshold (see playbook_learning.retire_idle_auto_entries).
    auto_playbook_idle_retired_24h: Optional[int] = None
    auto_playbook_low_success_retired_24h: Optional[int] = None
    if project and project.design_doc:
        from app.playbook_learning import (  # noqa: PLC0415
            count_idle_auto_retired_within,
            count_low_success_retired_within,
        )

        auto_playbook_idle_retired_24h = count_idle_auto_retired_within(
            project.design_doc, hours=24
        )
        auto_playbook_low_success_retired_24h = count_low_success_retired_within(
            project.design_doc, hours=24
        )

    # Self-review prediction accuracy: compare self-review check_results against human rejection categories
    tasks_with_both_24h = [
        t
        for t in terminal_tasks_24h
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and "_first_attempt_rejection_categories" in (t.context or {})
    ]
    self_review_prediction_accuracy = _compute_sr_prediction_accuracy(
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
            select(Task).where(
                Task.project_id == project_id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                Task.created_at >= ms_cutoff,
            )
        )
        ms_all = ms_result.scalars().all()
        ms_medium = [t for t in ms_all if (t.context or {}).get("scope") == "medium"]
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
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.latest_status_message,
                Task.context,
                Task.created_at,
                # Task.status is read by classify_failure → record_residual_fingerprint's
                # status-gate (added in fc071105). Without it the attribute access
                # triggers async lazy-load and raises MissingGreenlet under FastAPI.
                Task.status,
                Task.status_log,
            )
        )
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
        entry = clusters.setdefault(
            fp,
            {
                "fingerprint": fp,
                "count": 0,
                "most_recent_at": None,
                "sample_message": msg[:300],
                "example_task_ids": [],
            },
        )
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
            .options(
                load_only(
                    ResidualRemediationCase.fingerprint,
                    ResidualRemediationCase.status,
                    ResidualRemediationCase.last_spawned_pr_number,
                    ResidualRemediationCase.consecutive_failure_count,
                    ResidualRemediationCase.suspended_at,
                    ResidualRemediationCase.audit_defer_count,
                    ResidualRemediationCase.audit_deferred_system_id,
                )
            )
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
            entry["suspended_at"] = (
                c.suspended_at.isoformat() if c.suspended_at else None
            )
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
    status: list[str] | None = Query(
        None, description="Filter by ResidualRemediationCaseStatus values; repeatable"
    ),
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
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "last_spawned_at": c.last_spawned_at.isoformat()
                if c.last_spawned_at
                else None,
                "last_spawned_pr_number": c.last_spawned_pr_number,
                "last_spawned_task_id": str(c.last_spawned_task_id)
                if c.last_spawned_task_id
                else None,
                "consecutive_failure_count": c.consecutive_failure_count,
                "suspended_at": c.suspended_at.isoformat() if c.suspended_at else None,
                "sample_message": (
                    c.sample_messages[0] if c.sample_messages else c.sample_message
                ),
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
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.IGNORED,
    }


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
        raise HTTPException(
            status_code=404,
            detail="No IGNORED case found for this fingerprint and project",
        )
    case.status = ResidualRemediationCaseStatus.ACTIVE
    case.updated_at = _utcnow()
    await session.commit()
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.ACTIVE,
    }


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
        raise HTTPException(
            status_code=404,
            detail="No SUSPENDED case found for this fingerprint and project",
        )
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


@router.get("/regression-cases")
async def list_regression_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    status: list[str] | None = Query(
        None, description="Filter by RegressionCaseStatus values; repeatable"
    ),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression remediation cases for a project.

    Defaults to open cases (ACTIVE / REMEDIATING / NEEDS_MANUAL_INTERVENTION).
    Ordered by last_seen_at desc.
    """
    if status:
        valid = {s.value for s in RegressionCaseStatus}
        invalid = [s for s in status if s not in valid]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status values: {invalid}. Valid: {sorted(valid)}",
            )
        status_filter = list(status)
    else:
        status_filter = [
            RegressionCaseStatus.ACTIVE,
            RegressionCaseStatus.REMEDIATING,
            RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        ]

    query = (
        select(RegressionRemediationCase)
        .where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.status.in_(status_filter),
        )
        .order_by(RegressionRemediationCase.last_seen_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    now = _utcnow()
    stall_threshold = now - timedelta(hours=_REGRESSION_STALL_THRESHOLD_HOURS)
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_class_layer_defense_counts,
        compute_untargeted_layers,
    )

    closed_rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # Aggregate fix-log entries by regression_class across all open+closed cases
    # so each case row can show class-level defense counts (not just its own log).
    fix_logs_by_class: dict[str, list] = {}
    for c in list(rows) + list(closed_rows):
        bucket = fix_logs_by_class.setdefault(c.regression_class, [])
        bucket.extend(c.fingerprint_to_fix_log or [])
    return {
        "count": len(rows),
        "status_filter": status_filter,
        "summary": _compute_regression_case_summary(
            closed_rows, all_rows=list(rows) + list(closed_rows)
        ),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "status": c.status,
                "attempts": c.attempts,
                "hit_count": c.hit_count,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "last_remediation_task_id": str(c.last_remediation_task_id)
                if c.last_remediation_task_id
                else None,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_remediation_result": c.last_remediation_result,
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "recurrence_count": _regression_case_recurrence_count(c),
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "sample_message": c.sample_message,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "escalation_reason": c.escalation_reason,
                "stalled": c.status == RegressionCaseStatus.ACTIVE
                and c.last_remediation_pr_number is None
                and c.first_seen_at < stall_threshold,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    fix_logs_by_class.get(c.regression_class)
                ),
                "layer_defense_counts_by_class": compute_class_layer_defense_counts(
                    fix_logs_by_class.get(c.regression_class)
                ),
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/layer-defense-density")
async def regression_cases_layer_defense_density(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Per-canonical-layer defense activity over the last ``days`` days.

    Loads every ``RegressionRemediationCase`` for the project, walks each
    case's ``fingerprint_to_fix_log`` entries, and returns one row per
    canonical infrastructure layer (CI_WORKFLOW, AGENT_VM, AGENT_VERB,
    BASELINE_GATE, REVIEWER_PASS, VIEWPORT) — including layers with zero
    defenses — so the UI can render a stable six-row table.
    """
    from app.pipeline_regression_dispatcher import (
        _compute_layer_fragility_stats,
        compute_layer_defense_density,
    )

    lookback_cutoff = datetime.utcnow() - timedelta(days=days)
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.updated_at >= lookback_cutoff,
                )
                .order_by(RegressionRemediationCase.updated_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    pairs: list[tuple[str, dict[str, Any]]] = []
    for case in cases:
        for entry in case.fingerprint_to_fix_log or []:
            pairs.append((str(case.id), entry))
    layers = compute_layer_defense_density(pairs, lookback_days=days)
    all_fix_logs = [entry for _, entry in pairs]
    fragility = _compute_layer_fragility_stats(all_fix_logs, lookback_days=days)
    for row in layers:
        frag = fragility.get(row["layer"], {})
        row["avg_hold_days"] = frag.get("avg_hold_days")
        row["still_holding_count"] = frag.get("still_holding_count", 0)
        row["held_gt_7d_count"] = frag.get("held_gt_7d_count", 0)
        row["retripped_within_48h_count"] = frag.get("retripped_within_48h_count", 0)
        row["single_layer_fix_count"] = frag.get("single_layer_fix_count", 0)
        row["single_layer_avg_hold_days"] = frag.get("single_layer_avg_hold_days")
        row["multi_layer_fix_count"] = frag.get("multi_layer_fix_count", 0)
        row["multi_layer_avg_hold_days"] = frag.get("multi_layer_avg_hold_days")
    return {
        "days": days,
        "generated_at": _utcnow().isoformat(),
        "layers": layers,
    }


@router.post("/regression-cases/{regression_class}/clear-manual-intervention")
async def clear_regression_manual_intervention(
    regression_class: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Clear NEEDS_MANUAL_INTERVENTION on a RegressionRemediationCase.

    Transitions status back to ACTIVE and clears needs_manual_intervention_at
    so the dispatcher can resume spawning. Returns 404 if no case exists or
    if the case is not currently NEEDS_MANUAL_INTERVENTION. Cooldown / last
    timestamps are preserved so the next tick honors the existing window
    rather than immediately re-spawning.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
        raise HTTPException(
            status_code=404,
            detail="No NEEDS_MANUAL_INTERVENTION case found for this regression_class and project",
        )
    case.status = RegressionCaseStatus.ACTIVE
    case.needs_manual_intervention_at = None
    # Reset the durable recovery clock so a re-escalated case does not pick
    # up a stale healthy_since and auto-close on the first tick, bypassing
    # the stable window.  The case row's signal_first_seen_at /
    # signal_first_crossed_at columns are intentionally preserved so MTTD on
    # any future spawn reflects the original detection latency.
    case.signal_healthy_since_at = None
    case.updated_at = _utcnow()
    session.add(case)
    await session.commit()
    return {
        "id": str(case.id),
        "regression_class": regression_class,
        "status": RegressionCaseStatus.ACTIVE,
        "attempts": case.attempts,
        "needs_manual_intervention_at": None,
    }


@router.post("/regression-cases/{case_id}/acknowledge")
async def acknowledge_regression_case(
    case_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Stamp operator_acknowledged_at on a RegressionRemediationCase.

    Idempotent: if the case is already acknowledged the existing timestamp is
    returned unchanged. Returns 404 if no matching case exists for this project.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.id == case_id,
            RegressionRemediationCase.project_id == project_id,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail="Regression case not found")
    if case.operator_acknowledged_at is None:
        case.operator_acknowledged_at = _utcnow()
        case.updated_at = _utcnow()
        session.add(case)
        await session.commit()
    return {
        "id": str(case.id),
        "operator_acknowledged_at": case.operator_acknowledged_at.isoformat(),
    }


@router.post("/regression-cases/close")
async def close_regression_case(
    regression_class: str = Query(
        ...,
        description=(
            "Full regression_class string. Passed as a query parameter (not a "
            "path segment) because regression_class values may contain slashes "
            "or other URL-reserved characters."
        ),
    ),
    project_id: uuid.UUID = Query(...),
    reason: str | None = Query(
        None, description="Optional human-readable reason for the close"
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Operator-close a RegressionRemediationCase from any non-CLOSED status.

    Used to clear cases that became noise — e.g. spawns triggered by a signal
    pattern that has since been gated out at the source (and so will never
    auto-recover via run_recovery_once, which only handles MERGED→CLOSED).

    Idempotent: returns 200 with status=CLOSED if the case was already CLOSED.
    Returns 404 if no row exists for (project_id, regression_class).

    regression_class is a query parameter rather than a path segment so values
    containing slashes (e.g. ``sr_blind_spot:theme:agent/src/prompt.ts ...``)
    don't fragment FastAPI's path routing.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(
            status_code=404,
            detail="No regression case found for this regression_class and project",
        )
    prior_status = case.status
    if prior_status != RegressionCaseStatus.CLOSED:
        case.status = RegressionCaseStatus.CLOSED
        case.last_remediation_result = "operator_closed"
        case.needs_manual_intervention_at = None
        # Drop the durable recovery clock so a future re-escalation on the same
        # row does not pick up a stale healthy_since and auto-close on the first
        # tick, bypassing the stable window. signal_first_seen_at /
        # signal_first_crossed_at are intentionally preserved so a future spawn
        # reflects the original detection latency.
        case.signal_healthy_since_at = None
        case.updated_at = _utcnow()
        case.fingerprint_to_fix_log = list(case.fingerprint_to_fix_log or []) + [
            {
                "fingerprint": regression_class,
                "operator_closed_at": _utcnow().isoformat(),
                "operator_close_reason": reason,
                "prior_status": prior_status,
            }
        ]
        session.add(case)
        await session.commit()

    return {
        "id": str(case.id),
        "regression_class": regression_class,
        "status": case.status,
        "prior_status": prior_status,
        "attempts": case.attempts,
    }


@router.get("/regression-cases/manual-intervention")
async def list_regression_manual_intervention_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases escalated to manual intervention."""
    now = _utcnow()
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_untargeted_layers,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status
                    == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "recurrence_count": _regression_case_recurrence_count(c),
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "investigate_spawned_at": c.investigate_spawned_at.isoformat()
                if c.investigate_spawned_at
                else None,
                "time_to_investigate_sec": c.time_to_investigate_sec,
                "escalation_reason": c.escalation_reason,
                "operator_acknowledged_at": c.operator_acknowledged_at.isoformat()
                if c.operator_acknowledged_at
                else None,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/auto-recovered")
async def list_regression_auto_recovered_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases auto-closed by the watchdog after unattended recovery.

    Mirrors the response shape of /regression-cases/manual-intervention so the
    admin UI can render auto-recovered cases alongside outstanding escalations.
    Adds a ``recovered_at`` field pulled from the latest ``recovered_unattended``
    log entry and an ``updated_at`` field so operators can sort by close time.
    """
    now = _utcnow()
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_untargeted_layers,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                    RegressionRemediationCase.last_remediation_result
                    == CLOSE_REASON_RECOVERED_UNATTENDED,
                )
                .order_by(RegressionRemediationCase.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    cases = []
    for c in rows:
        recovered_at = None
        for entry in reversed(c.fingerprint_to_fix_log or []):
            if (
                isinstance(entry, dict)
                and entry.get("reason") == CLOSE_REASON_RECOVERED_UNATTENDED
            ):
                recovered_at = entry.get("recovered_at")
                break
        cases.append(
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "recurrence_count": _regression_case_recurrence_count(c),
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "escalation_reason": c.escalation_reason,
                "recovered_at": recovered_at,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
        )
    return {"count": len(cases), "cases": cases}


@router.get("/regression-cases/stalled")
async def list_regression_stalled_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases stalled without PR (ACTIVE, no PR, and older than threshold)."""
    now = _utcnow()
    stall_threshold = now - timedelta(hours=_REGRESSION_STALL_THRESHOLD_HOURS)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.ACTIVE,
                    RegressionRemediationCase.last_remediation_pr_number.is_(None),
                    RegressionRemediationCase.first_seen_at < stall_threshold,
                )
                .order_by(RegressionRemediationCase.first_seen_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "hit_count": c.hit_count,
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/recently-closed")
async def list_recently_closed_regression_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(5, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List recently closed regression cases (autonomous wins)."""
    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        _extract_recurrence_cycles,
        compute_untargeted_layers,
    )

    cases = []
    for c in rows:
        fix_pr_number = None
        recovered_at = None
        merged_at = None
        latest_merged_at = None
        hold_days_history = []

        if c.fingerprint_to_fix_log:
            for entry in c.fingerprint_to_fix_log:
                if "fix_pr_number" in entry:
                    fix_pr_number = entry["fix_pr_number"]
                if entry.get("merged_at"):
                    latest_merged_at = entry["merged_at"]
                if "recovered_at" in entry:
                    recovered_at = entry["recovered_at"]
                    # Pair this recovery with the latest merge that preceded it,
                    # mirroring _compute_regression_case_summary (lines 940–966).
                    merged_at = latest_merged_at

        # Pair merged_at/recovered_at across separate fix-log entries
        # (production shape: mark_merged writes one entry, the recovery scan
        # writes a second with recovered_at — see pipeline_regression_dispatcher
        # ._extract_recurrence_cycles).
        for cycle in _extract_recurrence_cycles(c):
            merged_dt = cycle.get("merged_at")
            recovered_dt = cycle.get("recovered_at")
            if not (
                isinstance(merged_dt, datetime) and isinstance(recovered_dt, datetime)
            ):
                continue
            merged_naive = _to_naive_utc(merged_dt)
            recovered_naive = _to_naive_utc(recovered_dt)
            hold_duration_seconds = (recovered_naive - merged_naive).total_seconds()
            hold_days = hold_duration_seconds / (24 * 3600)
            hold_days_history.append(round(hold_days, 2))

        is_fragile = any(hold_days < 2.0 for hold_days in hold_days_history)

        time_to_recover_sec = None
        if recovered_at and c.first_seen_at:
            try:
                recovered_dt = datetime.fromisoformat(
                    recovered_at.replace("Z", "+00:00")
                )
                # first_seen_at is stored as NaiveUTCDateTime; normalize both sides.
                recovered_naive = _to_naive_utc(recovered_dt)
                first_seen_naive = _to_naive_utc(c.first_seen_at)
                time_to_recover_sec = int(
                    (recovered_naive - first_seen_naive).total_seconds()
                )
            except (ValueError, TypeError):
                pass

        cases.append(
            {
                "id": str(c.id),
                "regression_class": c.regression_class,
                "fix_pr_number": fix_pr_number,
                "merged_at": merged_at,
                "recovered_at": recovered_at,
                "recurrence_count": _regression_case_recurrence_count(c),
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "time_to_recover_sec": time_to_recover_sec,
                "investigate_spawned_at": c.investigate_spawned_at.isoformat()
                if c.investigate_spawned_at
                else None,
                "time_to_investigate_sec": c.time_to_investigate_sec,
                "hold_days_history": hold_days_history,
                "is_fragile": is_fragile,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
        )

    return {
        "count": len(cases),
        "cases": cases,
    }


@router.get("/regression-cases/cross-layer-compliance")
async def get_cross_layer_compliance(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    window_days: int = Query(30, ge=1, le=90, description="Rolling window in days"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Cross-layer escalation compliance metric over a rolling window.

    For fix-log entries produced by cross-layer-escalated investigations,
    computes how often the resulting fix actually targeted >=2 distinct
    canonical infrastructure layers (compliant) vs. only 1 or 0 (non-compliant).
    Entries still awaiting the layers_targeted backfill are excluded from
    both numerator and denominator and reported in awaiting_backfill_count.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return compute_cross_layer_compliance(rows, cutoff)


@router.get("/regression-cases/first-attempt-compliance-trend")
async def get_first_attempt_compliance_trend(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    num_windows: int = Query(6, ge=1, le=12, description="Number of rolling windows"),
    window_days: int = Query(
        7, ge=1, le=30, description="Width of each window in days"
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """First-attempt cross-layer compliance trend over rolling weekly windows.

    Distinguishes whether the original meta-investigation produced a >=2-layer
    defense (first_attempt_compliant) vs. only the corrective compliance-followup
    did (followup_rescued). Cases awaiting layers_targeted backfill are excluded
    from the rate denominator and reported in awaiting_backfill.
    """
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_first_attempt_compliance_trend,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return compute_first_attempt_compliance_trend(
        list(rows), num_windows=num_windows, window_days=window_days
    )


@router.get("/regression-class-kpis")
async def get_regression_class_kpis(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    window_hours: int = Query(168, ge=1, le=720, description="Rolling window in hours"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-class KPI snapshot from RegressionRemediationCase records.

    Returns cases_opened, cases_closed, dispatch_attempt_count,
    remediation_success_rate, mean_time_to_detect_seconds, and
    mean_time_to_remediate_seconds grouped by regression_class.
    cases_opened/closed are scoped to the window; timing stats use all
    available rows for the class (more data = better mean estimate).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )

    by_class: Dict[str, Any] = {}
    for case in rows:
        cls = case.regression_class
        b = by_class.setdefault(
            cls, {"opened": 0, "closed": 0, "attempts": 0, "mttd": [], "mttr": []}
        )
        first = case.first_seen_at and _to_naive_utc(case.first_seen_at)
        if first and first >= cutoff:
            b["opened"] += 1
        if case.status in (RegressionCaseStatus.CLOSED, RegressionCaseStatus.MERGED):
            last = case.last_seen_at and _to_naive_utc(case.last_seen_at)
            if last and last >= cutoff:
                b["closed"] += 1
        b["attempts"] += case.attempts or 0
        if case.time_to_detect_sec is not None:
            b["mttd"].append(case.time_to_detect_sec)
        if case.fingerprint_to_fix_log and first:
            ep_start = first
            latest_merged_ep: Optional[datetime] = None
            for entry in case.fingerprint_to_fix_log:
                if entry.get("merged_at"):
                    try:
                        merged = _to_naive_utc(
                            datetime.fromisoformat(
                                str(entry["merged_at"]).replace("Z", "+00:00")
                            )
                        )
                        b["mttr"].append((merged - ep_start).total_seconds())
                        latest_merged_ep = merged
                    except (ValueError, TypeError):
                        pass
                if entry.get("recovered_at") and latest_merged_ep is not None:
                    try:
                        ep_start = _to_naive_utc(
                            datetime.fromisoformat(
                                str(entry["recovered_at"]).replace("Z", "+00:00")
                            )
                        )
                        latest_merged_ep = None
                    except (ValueError, TypeError):
                        pass

    classes = [
        {
            "regression_class": cls,
            "cases_opened": b["opened"],
            "cases_closed": b["closed"],
            "dispatch_attempt_count": b["attempts"],
            "remediation_success_rate": round(b["closed"] / b["attempts"], 3)
            if b["attempts"]
            else None,
            "mean_time_to_detect_seconds": round(statistics.mean(b["mttd"]), 1)
            if b["mttd"]
            else None,
            "mean_time_to_remediate_seconds": round(statistics.mean(b["mttr"]), 1)
            if b["mttr"]
            else None,
        }
        for cls, b in sorted(by_class.items())
    ]
    return {
        "project_id": str(project_id),
        "window_hours": window_hours,
        "class_count": len(classes),
        "classes": classes,
    }


@router.get("/regression-cases/trend")
async def get_regression_case_trend(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    days: int = Query(30, ge=7, le=90, description="Rolling window in days"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Daily opened-vs-closed trend for regression cases over the last N days.

    opened: cases whose first_seen_at falls in the day bucket.
    closed: cases whose updated_at falls in the day bucket AND status is CLOSED or MERGED.
    Returns one entry per day plus a per-class breakdown for sparkline rendering.
    """
    cutoff = _utcnow() - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    or_(
                        RegressionRemediationCase.first_seen_at >= cutoff,
                        RegressionRemediationCase.updated_at >= cutoff,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    now = _utcnow()
    # Pre-fill all day buckets so the frontend gets a contiguous series.
    buckets: Dict[str, Dict[str, Any]] = {
        (now - timedelta(days=days - 1 - d)).strftime("%Y-%m-%d"): {
            "date": (now - timedelta(days=days - 1 - d)).strftime("%Y-%m-%d"),
            "opened": 0,
            "closed": 0,
        }
        for d in range(days)
    }
    by_class: Dict[str, Dict[str, Dict[str, int]]] = {}

    closed_statuses = {RegressionCaseStatus.CLOSED, RegressionCaseStatus.MERGED}
    for case in rows:
        cls = case.regression_class
        if case.first_seen_at:
            day = _to_naive_utc(case.first_seen_at).strftime("%Y-%m-%d")
            if day in buckets:
                buckets[day]["opened"] += 1
                by_class.setdefault(cls, {}).setdefault(
                    day, {"opened": 0, "closed": 0}
                )["opened"] += 1
        if case.updated_at and case.status in closed_statuses:
            day = _to_naive_utc(case.updated_at).strftime("%Y-%m-%d")
            if day in buckets:
                buckets[day]["closed"] += 1
                by_class.setdefault(cls, {}).setdefault(
                    day, {"opened": 0, "closed": 0}
                )["closed"] += 1

    daily = sorted(buckets.values(), key=lambda b: b["date"])
    class_trends = {
        cls: [
            {
                "date": b["date"],
                **by_class[cls].get(b["date"], {"opened": 0, "closed": 0}),
            }
            for b in daily
        ]
        for cls in sorted(by_class)
    }
    return {
        "project_id": str(project_id),
        "days": days,
        "daily": daily,
        "by_class": class_trends,
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

    tasks = (
        (
            await session.execute(
                select(Task)
                .where(*filters)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.vm_spot,
                        Task.vm_provisioned_at,
                        Task.vm_stopped_at,
                    )
                )
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )

    def _acct_id(ctx: Dict[str, Any] | None) -> str | None:
        if not ctx:
            return None
        return ctx.get("llm_account_id") or ctx.get("max_plan_account_id")

    acct_ids = {a for t in tasks if (a := _acct_id(t.context))}
    acct_map: Dict[str, Dict[str, str]] = {}
    if acct_ids:
        parsed = [uuid.UUID(a) for a in acct_ids if a]
        if parsed:
            rows = (
                (
                    await session.execute(
                        select(MaxPlanAccount).where(MaxPlanAccount.id.in_(parsed))
                    )
                )
                .scalars()
                .all()
            )
            for a in rows:
                acct_map[str(a.id)] = {"name": a.name, "provider": a.provider.value}

    groups: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        acct_id = _acct_id(t.context)
        info = acct_map.get(acct_id or "", {"name": "unknown", "provider": "unknown"})
        key = f"{info['provider']}|{acct_id or 'untracked'}"
        g = groups.setdefault(
            key,
            {
                "provider": info["provider"],
                "account_id": acct_id,
                "account_name": info["name"],
                "total_tasks": 0,
                "success_count": 0,
                "merged_count": 0,
                "total_cost_usd": 0.0,
            },
        )
        g["total_tasks"] += 1
        if t.status == TaskStatus.COMPLETED:
            g["success_count"] += 1
            if (t.context or {}).get("pr_merged"):
                g["merged_count"] += 1
        if t.vm_provisioned_at and t.vm_stopped_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            g["total_cost_usd"] += (
                t.vm_stopped_at - t.vm_provisioned_at
            ).total_seconds() * rate

    result_rows = []
    for g in groups.values():
        result_rows.append(
            {
                **g,
                "total_cost_usd": round(g["total_cost_usd"], 2),
                "success_rate": round(g["success_count"] / g["total_tasks"], 3)
                if g["total_tasks"]
                else None,
                "cost_per_merged_pr": round(g["total_cost_usd"] / g["merged_count"], 2)
                if g["merged_count"]
                else None,
            }
        )
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
    from app.drift_detection import compute_direction_drift, last_n_drift_snapshots  # noqa: PLC0415

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
    direction_map = {
        d["id"]: d.get("text", "") for d in (project.directions or []) if d.get("id")
    }

    _SPARKLINE_N = 7
    flagged = []
    for did, sig in snapshot["by_direction"].items():
        if not (sig.get("over_claim_flag") or sig.get("stalled_despite_churn_flag")):
            continue
        raw_snaps = await last_n_drift_snapshots(
            session, project.id, did, n=_SPARKLINE_N
        )
        # Reverse to chronological order for sparkline rendering (oldest first).
        history = [
            {
                "snapshot_at": s.snapshot_at.isoformat(),
                "claim_shipped_consistency": s.claim_shipped_consistency,
                "over_claim_flag": s.over_claim_flag,
                "stalled_despite_churn_flag": s.stalled_despite_churn_flag,
            }
            for s in reversed(raw_snaps)
        ]
        flagged.append(
            {
                "direction_id": did,
                "direction_text": direction_map.get(did, ""),
                "over_claim_flag": sig["over_claim_flag"],
                "stalled_despite_churn_flag": sig["stalled_despite_churn_flag"],
                "claimed_changes_count": sig["claimed_changes_count"],
                "actually_merged_prs": sig["actually_merged_prs"],
                "claim_shipped_consistency": sig["claim_shipped_consistency"],
                "drift_history": history,
            }
        )

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
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.project_id,
                    Task.title,
                    Task.created_at,
                    Task.started_at,
                    Task.context,
                    Task.latest_status_message,
                    Task.attempt_count,
                    Task.requires_approval,
                    Task.vm_provisioned_at,
                    Task.vm_started_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    # Audit W1: promoted columns read in the digest funnel.
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                    # Per-(provider, model) cost attribution on direction CPM.
                    Task.provider,
                    Task.model,
                )
            )
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
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.project_id,
                    Task.title,
                    Task.created_at,
                    Task.started_at,
                    Task.payloads,
                    Task.context,
                    Task.status_log,
                    Task.latest_status_message,
                    Task.attempt_count,
                    Task.requires_approval,
                    Task.vm_provisioned_at,
                    Task.vm_started_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    # Audit W1: promoted columns read in the digest funnel.
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                    # Per-(provider, model) cost attribution on direction CPM.
                    Task.provider,
                    Task.model,
                )
            )
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        all_tasks = list(tasks_result.scalars().all())
    tasks_capped = len(all_tasks) == _DIGEST_TASK_ROW_CAP
    if tasks_capped:
        logger.warning(
            "admin_digest task query hit %d-row safety cap (window=%dh, project_id=%s); digest metrics may be truncated",
            _DIGEST_TASK_ROW_CAP,
            hours,
            project_id,
        )

    # Post-filter completed/failed by completion timestamp, not creation time
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
    # Approval tasks use created_at (they haven't completed yet)
    # _to_naive_utc strips tz from t.created_at (TIMESTAMPTZ since 0061) for
    # comparison with naive window_start. See time_utils._to_naive_utc.
    approval_tasks = [
        t
        for t in all_tasks
        if t.requires_approval
        and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and _to_naive_utc(t.created_at) >= window_start
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
            )
            .where(*active_filters)
            .order_by(Task.started_at.asc().nullsfirst())
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.title,
                    Task.started_at,
                    Task.project_id,
                    Task.context,
                    Task.vm_provisioned_at,
                    Task.vm_spot,
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                )
            )
        )
        active_tasks_all: List[Task] = []
        for row in active_result.all():
            task = row[0]
            _stash_projected_task_fields(task, first_payload=row[1] or {})
            active_tasks_all.append(task)
    else:
        active_result = await session.execute(
            select(Task)
            .where(*active_filters)
            .order_by(Task.started_at.asc().nullsfirst())
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.title,
                    Task.payloads,
                    Task.started_at,
                    Task.project_id,
                    Task.context,
                    Task.vm_provisioned_at,
                    Task.vm_spot,
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                )
            )
        )
        active_tasks_all = list(active_result.scalars().all())

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1
        for t in completed_tasks
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
                d
                for d in (project.directions or [])
                if d.get("status") in ("active", "held", "auto_paused")
            ]

            # Build direction_id -> text lookup
            for d in active_directions:
                did = d.get("id") or d.get("direction_id")
                if did:
                    dir_id_to_text[str(did)] = (
                        d.get("text") or d.get("description") or ""
                    )

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
                    1
                    for t in tasks_for_dir
                    if t.status == TaskStatus.COMPLETED
                    and (t.first_payload).get("type", "create") == "research"
                    and _derive_completed_at(t) >= window_start
                )
                tasks_created_in_window = sum(
                    1
                    for t in tasks_for_dir
                    if _to_naive_utc(t.created_at) >= window_start
                )

                # Single pass over research_history: aggregate PR/scope/product/type/taxonomy
                prs_merged = 0
                autonomous_prs_merged = 0
                assisted_prs_merged = 0
                operator_action_count = 0
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
                        autonomous_prs_merged += (
                            entry.get("autonomous_prs_merged", entry.get("prs_merged"))
                            or 0
                        )
                        assisted_prs_merged += entry.get("assisted_prs_merged", 0) or 0
                        operator_action_count += (
                            entry.get("operator_action_count", 0) or 0
                        )
                        prs_rejected += entry.get("prs_rejected") or 0
                        dir_reviews_dismissed += entry.get("reviews_dismissed") or 0
                        dir_reviews_overridden += entry.get("reviews_overridden") or 0
                        dir_user_issues_caught += entry.get("user_issues_caught") or 0
                        findings_tasks_created += entry.get("tasks_created") or 0
                        dir_findings_produced += entry.get("findings_produced") or 0
                        dir_batch_cap_applied += entry.get("batch_cap_applied") or 0
                        for _fk, _fv in (entry.get("filter_breakdown") or {}).items():
                            dir_filter_breakdown_agg[_fk] = (
                                dir_filter_breakdown_agg.get(_fk, 0) + (_fv or 0)
                            )
                        atm = entry.get("avg_time_to_merge_hours")
                        if atm is not None:
                            merge_time_values.append(float(atm))
                        for scope, stats in (
                            entry.get("scope_breakdown") or {}
                        ).items():
                            agg = dir_scope_agg.setdefault(
                                scope,
                                {
                                    "created": 0,
                                    "merged": 0,
                                    "merged_cross_module": 0,
                                    "failed": 0,
                                    "failed_infra": 0,
                                    "failed_code_error": 0,
                                    "failed_scope_overflow": 0,
                                    "calibrated_in": 0,
                                    "calibrated_out": 0,
                                    "failure_stages": {},
                                },
                            )
                            for k in (
                                "created",
                                "merged",
                                "merged_cross_module",
                                "failed",
                                "failed_infra",
                                "failed_code_error",
                                "failed_scope_overflow",
                                "calibrated_in",
                                "calibrated_out",
                            ):
                                agg[k] += stats.get(k, 0)
                            stage_agg = agg.setdefault("failure_stages", {})
                            for stage_name, stage_cnt in (
                                stats.get("failure_stages") or {}
                            ).items():
                                stage_agg[stage_name] = stage_agg.get(stage_name, 0) + (
                                    stage_cnt or 0
                                )
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
                                    agg[f"_{field_name}_sum"] = (
                                        agg.get(f"_{field_name}_sum", 0.0)
                                        + float(v) * weight
                                    )
                                    agg[f"_{field_name}_w"] = (
                                        agg.get(f"_{field_name}_w", 0) + weight
                                    )
                            for effort, buckets in (
                                stats.get("effort_distribution") or {}
                            ).items():
                                if not isinstance(buckets, dict):
                                    continue
                                ed = agg.setdefault("effort_distribution", {})
                                eb = ed.setdefault(
                                    effort, {"created": 0, "merged": 0, "failed": 0}
                                )
                                for bk in ("created", "merged", "failed"):
                                    eb[bk] += buckets.get(bk, 0) or 0
                        pr = entry.get("product_ratio")
                        if pr is not None:
                            window_product_ratios.append(float(pr))
                        for ttype, cnt in (
                            entry.get("type_distribution") or {}
                        ).items():
                            agg_type_distribution[ttype] = (
                                agg_type_distribution.get(ttype, 0) + cnt
                            )
                        for ttype, stats in (entry.get("type_breakdown") or {}).items():
                            tb = agg_type_breakdown.setdefault(
                                ttype, {"created": 0, "merged": 0, "failed": 0}
                            )
                            for k in ("created", "merged", "failed"):
                                tb[k] += (stats or {}).get(k, 0) or 0
                        for tax, cnt in (entry.get("taxonomy_summary") or {}).items():
                            dir_taxonomy_summary[tax] = (
                                dir_taxonomy_summary.get(tax, 0) + cnt
                            )
                        for stage, cnt in (entry.get("stage_dropout") or {}).items():
                            dir_stage_dropout[stage] = dir_stage_dropout.get(
                                stage, 0
                            ) + (cnt or 0)
                        for dq in entry.get("decomposition_quality") or []:
                            if not isinstance(dq, dict):
                                continue
                            dq_plans += 1
                            mr = dq.get("subtask_merge_rate")
                            if mr is not None:
                                dq_merge_rate_sum += float(mr)
                                dq_merge_rate_count += 1
                            dq_subtask_count_sum += dq.get("subtask_count") or 0
                            dq_cross_module_sum += dq.get("cross_module_count") or 0
                        for fd in entry.get("failed_task_details") or []:
                            title = (fd or {}).get("title")
                            if title:
                                recent_failed_titles.append(title)
                    elif entry_ts >= prev_window_start:
                        for scope, stats in (
                            entry.get("scope_breakdown") or {}
                        ).items():
                            agg = prev_dir_scope_agg.setdefault(
                                scope,
                                {
                                    "created": 0,
                                    "merged": 0,
                                    "calibrated_in": 0,
                                    "calibrated_out": 0,
                                },
                            )
                            for k in (
                                "created",
                                "merged",
                                "calibrated_in",
                                "calibrated_out",
                            ):
                                agg[k] += stats.get(k, 0)

                if cycles_in_window == 0 and tasks_created_in_window == 0:
                    continue

                last_entry = (
                    (d.get("research_history") or [])[-1]
                    if d.get("research_history")
                    else None
                )
                latest_insights = (
                    last_entry.get("cycle_insights", []) if last_entry else []
                )
                recent_finding_titles = (
                    last_entry.get("finding_titles", []) if last_entry else []
                )
                latest_research_at = last_entry.get("timestamp") if last_entry else None
                dir_product_ratio = (
                    round(sum(window_product_ratios) / len(window_product_ratios), 3)
                    if window_product_ratios
                    else None
                )

                dir_cost = 0.0
                dir_spot_cost = 0.0
                dir_on_demand_cost = 0.0
                cost_by_scope: Dict[str, float] = {}
                cost_by_status: Dict[str, float] = {}
                cost_by_type: Dict[str, float] = {}
                cost_by_provider_model: Dict[str, float] = {}
                for t in tasks_for_dir:
                    if not (
                        t.vm_provisioned_at
                        and t.vm_stopped_at
                        and t.vm_stopped_at >= window_start
                    ):
                        continue
                    rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                    t_cost = (
                        rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                    )
                    dir_cost += t_cost
                    if t.vm_spot:
                        dir_spot_cost += t_cost
                    else:
                        dir_on_demand_cost += t_cost
                    scope_key = (t.context or {}).get("scope", "unknown")
                    cost_by_scope[scope_key] = (
                        cost_by_scope.get(scope_key, 0.0) + t_cost
                    )
                    status_key = (
                        t.status.value if hasattr(t.status, "value") else str(t.status)
                    )
                    cost_by_status[status_key] = (
                        cost_by_status.get(status_key, 0.0) + t_cost
                    )
                    type_key = (t.first_payload or {}).get("type", "create")
                    cost_by_type[type_key] = cost_by_type.get(type_key, 0.0) + t_cost
                    provider_key = t.provider or "unknown"
                    model_key = t.model or "unknown"
                    pm_key = f"{provider_key}|{model_key}"
                    cost_by_provider_model[pm_key] = (
                        cost_by_provider_model.get(pm_key, 0.0) + t_cost
                    )
                cost_by_scope = {k: round(v, 4) for k, v in cost_by_scope.items()}
                cost_by_status = {k: round(v, 4) for k, v in cost_by_status.items()}
                cost_by_type = {k: round(v, 4) for k, v in cost_by_type.items()}
                cost_by_provider_model = {
                    k: round(v, 4) for k, v in cost_by_provider_model.items()
                }

                dir_fa_measured = [
                    t
                    for t in tasks_for_dir
                    if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                    and _derive_completed_at(t) >= window_start
                    and "_first_attempt_succeeded" in (t.context or {})
                ]
                dir_fa_sample_size = len(dir_fa_measured)
                dir_fa_rate = (
                    round(
                        sum(
                            1
                            for t in dir_fa_measured
                            if (t.context or {}).get("_first_attempt_succeeded")
                        )
                        / dir_fa_sample_size,
                        3,
                    )
                    if dir_fa_sample_size > 0
                    else None
                )

                _history_all = d.get("research_history") or []
                _agg = d.get("history_aggregate") or {}
                lifetime_cycles = len(_history_all) + (_agg.get("total_cycles") or 0)
                lifetime_prs_merged = sum(
                    h.get("prs_merged", 0) for h in _history_all
                ) + (_agg.get("total_prs_merged") or 0)
                lifetime_prs_rejected = sum(
                    h.get("prs_rejected", 0) for h in _history_all
                ) + (_agg.get("total_prs_rejected") or 0)
                lifetime_tasks_created = sum(
                    h.get("tasks_created", 0) for h in _history_all
                ) + (_agg.get("total_tasks_created") or 0)
                lifetime_tasks_failed = sum(
                    h.get("tasks_failed", 0) for h in _history_all
                ) + (_agg.get("total_tasks_failed") or 0)
                lifetime_conversion_rate = (
                    round(lifetime_prs_merged / lifetime_tasks_created, 3)
                    if lifetime_tasks_created > 0
                    else None
                )
                lifetime_autonomous_prs_merged = sum(
                    h.get("autonomous_prs_merged", h.get("prs_merged", 0))
                    for h in _history_all
                ) + (_agg.get("total_autonomous_prs_merged") or 0)
                lifetime_operator_actions = sum(
                    h.get("operator_action_count", 0) for h in _history_all
                ) + (_agg.get("total_operator_actions") or 0)
                retirement_eligible = (
                    lifetime_autonomous_prs_merged >= 10
                    and lifetime_operator_actions == 0
                )

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                total_prs = prs_merged + prs_rejected
                acceptance_rate = (
                    round(prs_merged / total_prs, 3) if total_prs > 0 else None
                )
                avg_time_to_merge_hours = (
                    round(sum(merge_time_values) / len(merge_time_values), 2)
                    if merge_time_values
                    else None
                )
                conversion_rate = (
                    round(prs_merged / findings_tasks_created, 3)
                    if findings_tasks_created > 0
                    else None
                )
                # Accumulate into project-level scope rate aggregates
                for _s, _st in dir_scope_agg.items():
                    if _s not in scope_conversion_rates:
                        scope_conversion_rates[_s] = {
                            "created": 0,
                            "merged": 0,
                            "merged_cross_module": 0,
                            "failed_scope_overflow": 0,
                            "calibrated_in": 0,
                            "calibrated_out": 0,
                            "failure_stages": {},
                        }
                    scope_conversion_rates[_s]["created"] += _st["created"]
                    scope_conversion_rates[_s]["merged"] += _st["merged"]
                    scope_conversion_rates[_s]["merged_cross_module"] += _st.get(
                        "merged_cross_module", 0
                    )
                    scope_conversion_rates[_s]["failed_scope_overflow"] += _st.get(
                        "failed_scope_overflow", 0
                    )
                    scope_conversion_rates[_s]["calibrated_in"] += _st.get(
                        "calibrated_in", 0
                    )
                    scope_conversion_rates[_s]["calibrated_out"] += _st.get(
                        "calibrated_out", 0
                    )
                    _stage_acc = scope_conversion_rates[_s].setdefault(
                        "failure_stages", {}
                    )
                    for _stg, _scnt in (_st.get("failure_stages") or {}).items():
                        _stage_acc[_stg] = _stage_acc.get(_stg, 0) + (_scnt or 0)
                    for _f in (
                        "avg_expansion_ratio_merged",
                        "avg_expansion_ratio_failed",
                    ):
                        _sum = _st.get(f"_{_f}_sum", 0.0)
                        _w = _st.get(f"_{_f}_w", 0)
                        if _w > 0:
                            scope_conversion_rates[_s][f"_{_f}_sum"] = (
                                scope_conversion_rates[_s].get(f"_{_f}_sum", 0.0) + _sum
                            )
                            scope_conversion_rates[_s][f"_{_f}_w"] = (
                                scope_conversion_rates[_s].get(f"_{_f}_w", 0) + _w
                            )
                for _s, _st in prev_dir_scope_agg.items():
                    if _s not in prev_scope_conversion_rates:
                        prev_scope_conversion_rates[_s] = {
                            "created": 0,
                            "merged": 0,
                            "calibrated_in": 0,
                            "calibrated_out": 0,
                        }
                    prev_scope_conversion_rates[_s]["created"] += _st["created"]
                    prev_scope_conversion_rates[_s]["merged"] += _st["merged"]
                    prev_scope_conversion_rates[_s]["calibrated_in"] += _st.get(
                        "calibrated_in", 0
                    )
                    prev_scope_conversion_rates[_s]["calibrated_out"] += _st.get(
                        "calibrated_out", 0
                    )

                decomposition_summary = None
                if dq_plans > 0:
                    decomposition_summary = {
                        "plans_assessed": dq_plans,
                        "avg_subtask_merge_rate": (
                            round(dq_merge_rate_sum / dq_merge_rate_count, 3)
                            if dq_merge_rate_count > 0
                            else None
                        ),
                        "avg_subtask_count": round(dq_subtask_count_sum / dq_plans, 2),
                        "avg_cross_module_count": round(
                            dq_cross_module_sum / dq_plans, 2
                        ),
                    }

                filter_funnel = None
                if (
                    dir_findings_produced
                    or dir_batch_cap_applied
                    or dir_filter_breakdown_agg
                    or findings_tasks_created
                ):
                    filter_funnel = {
                        "findings_produced": dir_findings_produced,
                        "hash_dedup": dir_filter_breakdown_agg.get("hash_dedup", 0),
                        "word_overlap": dir_filter_breakdown_agg.get(
                            "word_overlap_dedup", 0
                        ),
                        "file_overlap": dir_filter_breakdown_agg.get(
                            "file_overlap_dedup", 0
                        ),
                        "type_filtered": dir_filter_breakdown_agg.get(
                            "type_filtered", 0
                        ),
                        "empty_prompt": dir_filter_breakdown_agg.get("empty_prompt", 0),
                        "batch_cap": dir_batch_cap_applied,
                        "tasks_created": findings_tasks_created,
                    }

                for _sk, _agg in dir_scope_agg.items():
                    for _field in (
                        "avg_expansion_ratio_merged",
                        "avg_expansion_ratio_failed",
                        "avg_complexity_merged",
                        "avg_complexity_failed",
                    ):
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

                _pq_notes = synthesize_prompt_quality_patterns(
                    d.get("research_history") or []
                )
                prompt_quality_hint = _pq_notes[0] if _pq_notes else None
                rejection_reasons = [
                    {
                        "task_id": str(_t.id),
                        "title": _t.title,
                        "rejection_note": (
                            (_t.context or {}).get("pr_rejection_note") or ""
                        )[:200],
                    }
                    for _ts, _t in _dir_rejections[:3]
                ]

                cost_budget_usd = d.get("cost_budget_usd")
                budget_utilization_pct = None
                budget_exceeded = False
                if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
                    budget_utilization_pct = round(
                        (dir_cost / cost_budget_usd) * 100, 1
                    )
                    budget_exceeded = dir_cost > cost_budget_usd
                    if budget_utilization_pct >= 80 and not d.get("_budget_alerted"):
                        d["_budget_alerted"] = True
                        flag_modified(project, "directions")
                        _budget_alert_fired = True
                        asyncio.ensure_future(
                            dispatch_external_notification(
                                project_id,
                                "BUDGET_EXCEEDED",
                                {
                                    "direction_id": did,
                                    "direction_name": direction_text,
                                    "current_spend_usd": round(dir_cost, 2),
                                    "budget_limit_usd": float(cost_budget_usd),
                                    "utilization_pct": budget_utilization_pct,
                                },
                            )
                        )

                direction_progress_list.append(
                    {
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
                        "operator_action_count": operator_action_count,
                        "prs_rejected": prs_rejected,
                        "acceptance_rate": acceptance_rate,
                        "conversion_rate": conversion_rate,
                        "avg_time_to_merge_hours": avg_time_to_merge_hours,
                        "possibly_saturated": bool(d.get("possibly_saturated", False)),
                        "possibly_complete": bool(d.get("possibly_complete", False)),
                        "auto_paused": bool(d.get("auto_paused", False)),
                        "paused_reason": d.get("paused_reason"),
                        "needs_manual_intervention": bool(
                            d.get("needs_manual_intervention", False)
                        ),
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
                        "cost_per_merged_pr": round(dir_cost / autonomous_prs_merged, 4)
                        if autonomous_prs_merged > 0
                        else None,
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
                        "lifetime_autonomous_prs_merged": lifetime_autonomous_prs_merged,
                        "lifetime_operator_actions": lifetime_operator_actions,
                        "retirement_eligible": retirement_eligible,
                        "review_quality": {
                            "reviews_dismissed": dir_reviews_dismissed,
                            "reviews_overridden": dir_reviews_overridden,
                            "user_issues_caught": dir_user_issues_caught,
                        },
                        "rejection_reasons": rejection_reasons or None,
                        "prompt_quality_hint": prompt_quality_hint,
                    }
                )

            direction_progress_list.sort(
                key=lambda x: x["cycles_in_window"], reverse=True
            )

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
    pr_artifact_map: Dict[str, str] = {}  # task_id -> pr_url
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error snippet

    if all_task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(all_task_ids),
                Artifact.artifact_type.in_(
                    [
                        ArtifactType.PULL_REQUEST,
                        ArtifactType.ERROR_LOG,
                    ]
                ),
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                if a.created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = a.pr_url or ""
            elif (
                a.artifact_type == ArtifactType.ERROR_LOG
                and a.logs
                and tid not in error_log_map
            ):
                error_log_map[tid] = a.logs[:500]

    # --- needs_attention: failed + pending approval ---
    from app.failure_classification import classify_failure as _classify_failure

    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = (
            round((_now - waiting_since).total_seconds() / 3600)
            if waiting_since
            else None
        )
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        needs_attention.append(
            {
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
            }
        )
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = (
            round((_now - waiting_since).total_seconds() / 3600)
            if waiting_since
            else None
        )
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        needs_attention.append(
            {
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
            }
        )
    needs_attention.sort(
        key=lambda x: (
            0 if x["reason"] == "approval_required" else 1,
            -(x["waiting_hours"] or 0),
        )
    )

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
        waiting_hours = (
            round((_now - waiting_since).total_seconds() / 3600)
            if waiting_since
            else None
        )
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        task_type = (t.first_payload).get("type", "create")
        _rf_cls = _classify_failure_rf(t, error_log_map.get(tid))
        recent_failures.append(
            {
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
                "timeout_subtype": _classify_timeout_subtype(t, error_log_map.get(tid))
                if _rf_cls == _AGENT_TIMEOUT
                else None,
                "scope": (t.context or {}).get("scope"),
                "merge_retries_saved": (t.context or {}).get("_merge_retries_saved"),
            }
        )

    # --- failure_groups (classify failures by type) ---
    from app.failure_classification import (
        classify_failure as _classify_failure,
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT,
        TAXONOMY_INFRA_RELATED,
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
            round((_now - waiting_since).total_seconds() / 3600)
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
                "taxonomy": _classify_taxonomy(t),
                "timeout_subtype": _classify_timeout_subtype(t, error_log_map.get(tid))
                if cls == _AGENT_TIMEOUT
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

    _retry_overhead_cost = 0.0
    _retry_task_count = 0
    _retry_attempt_sum = 0
    _retry_waste_by_dir: Dict[str, Dict[str, Any]] = {}
    for t in failed_tasks:
        _attempt = (t.context or {}).get("_attempt_number") or 0
        if _attempt > 1:
            _retry_task_count += 1
            _retry_attempt_sum += _attempt
            _task_retry_cost = 0.0
            if (
                t.vm_provisioned_at
                and t.vm_stopped_at
                and t.vm_stopped_at >= window_start
            ):
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _task_retry_cost = (
                    _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                )
                _retry_overhead_cost += _task_retry_cost
            _rw_did = (t.context or {}).get("direction_id")
            if _rw_did:
                _key = str(_rw_did)
                _entry = _retry_waste_by_dir.setdefault(
                    _key,
                    {
                        "direction_id": _key,
                        "direction_text": dir_id_to_text.get(_key, ""),
                        "retry_cost_usd": 0.0,
                        "retry_task_count": 0,
                    },
                )
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
    avg_retries_before_failure = (
        round(_retry_attempt_sum / _retry_task_count, 2) if _retry_task_count else None
    )

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
                / (
                    _retry_success_by_class.get(_cls, 0)
                    + _retry_failure_by_class.get(_cls, 0)
                ),
                4,
            ),
        }
        for _cls in _retry_classes
    }
    retry_success_count = _retry_success_count
    _total_retried = _retry_success_count + _retry_task_count
    retry_wasted_rate = (
        round(_retry_task_count / _total_retried, 4) if _total_retried else None
    )

    # Taxonomy distribution (4-category: scope/review/infra/code_defect)
    _tax_keys = (
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED,
        TAXONOMY_CODE_DEFECT,
    )
    _tax_counts: Dict[str, int] = {k: 0 for k in _tax_keys}
    for t in failed_tasks:
        _tax_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(_tax_counts.values())
    failure_taxonomy_distribution = {
        "categories": {
            k: {
                "count": _tax_counts[k],
                "percentage": round(_tax_counts[k] / _tax_total * 100, 1)
                if _tax_total
                else 0.0,
            }
            for k in _tax_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_tax_keys, key=lambda k: _tax_counts[k])
        if _tax_total
        else None,
    }

    _stage_total = sum(_failure_stage_counts.values())
    failure_stage_distribution = {
        "stages": dict(_failure_stage_counts),
        "total_classified": _stage_total,
        "dominant_stage": (
            max(_failure_stage_counts.items(), key=lambda kv: kv[1])[0]
            if _stage_total
            else None
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
        recent_completions.append(
            {
                "task_id": tid,
                "title": t.title or "Completed task",
                "completed_at": ts.isoformat() if ts else None,
                "pr_url": pr_artifact_map.get(tid),
                "task_type": task_type,
                "direction_id": t_dir_id,
                "direction_text": t_dir_text,
                "duration_seconds": duration_seconds,
                "scope": (t.context or {}).get("scope"),
                "review_iterations": (t.context or {})
                .get("review_loop", {})
                .get("iteration", 0),
            }
        )

    # --- active_tasks list (oldest started first, capped at _ACTIVE_TASKS_DISPLAY_LIMIT) ---
    _now_active = _utcnow()
    from app.review_pipeline import _max_review_iterations

    active_tasks_list = []
    for t in active_tasks_all[:_ACTIVE_TASKS_DISPLAY_LIMIT]:
        at_dir_id = (t.context or {}).get("direction_id")
        at_dir_text = dir_id_to_text.get(str(at_dir_id)) if at_dir_id else None
        if t.vm_provisioned_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            running_cost_usd = (
                rate * (_now_active - t.vm_provisioned_at).total_seconds()
            )
        else:
            running_cost_usd = None
        t_ctx = t.context or {}
        review_iteration = (t_ctx.get("review_loop") or {}).get("iteration", 0)
        try:
            review_cap = _max_review_iterations(t)
        except Exception:
            scope_val = t_ctx.get("scope")
            review_cap = {"small": 10, "medium": 12}.get(scope_val, 16)
        active_tasks_list.append(
            {
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
            }
        )

    running_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING}
    queued_statuses = {TaskStatus.QUEUED, TaskStatus.EVALUATING}
    running_count = sum(1 for t in active_tasks_all if t.status in running_statuses)
    queued_count = sum(1 for t in active_tasks_all if t.status in queued_statuses)

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = (
        round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None
    )

    # --- narrative_summary ---
    completed_count = len(completed_tasks)
    failed_count = len(failed_tasks)
    pending_approval_count = len(approval_tasks)

    # --- Previous window metrics for trend comparison ---
    prev_completed_tasks = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed_tasks = [
        t
        for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total_terminal = len(prev_completed_tasks) + len(prev_failed_tasks)
    prev_success_rate = (
        round(len(prev_completed_tasks) / prev_total_terminal, 2)
        if prev_total_terminal > 0
        else None
    )
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
            _cls = _classify_failure(_ft)
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
            max_wait = max(
                (x.get("waiting_hours") or 0 for x in approval_items), default=0
            )
            approval_str = f"{pending_approval_count} task{'s' if pending_approval_count != 1 else ''} awaiting your approval"
            if max_wait > 0:
                approval_str += f" ({max_wait}h)"
            narrative_parts.append(approval_str + ".")

        # SECOND: Failures with root cause
        if failed_count > 0:
            dominant_group = failure_groups[0] if failure_groups else None
            dominant_msg = None
            if (
                dominant_group
                and dominant_group["count"] >= 2
                and dominant_group["count"] / failed_count >= 0.6
            ):
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
                        if (
                            _classify_failure(_t, error_log_map.get(str(_t.id)))
                            == "CODE_ERROR"
                        ):
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
                failed_str = (
                    f"{failed_count} task{'s' if failed_count != 1 else ''} failed"
                )
                if failure_titles:
                    failed_str += f" ({', '.join(failure_titles)})"
                narrative_parts.append(failed_str + ".")

            _dom_cat = failure_taxonomy_distribution.get("dominant_category")
            _tax_tot = failure_taxonomy_distribution.get("total_failures") or 0
            if _dom_cat and _tax_tot >= 2:
                _dom_count = failure_taxonomy_distribution["categories"][_dom_cat][
                    "count"
                ]
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
            completion_titles = [
                t.title or "Completed task" for t in sorted_completions[:2]
            ]
            completion_titles = [
                t[:50] + "..." if len(t) > 50 else t for t in completion_titles
            ]
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
    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
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
    daily_cost_ceiling_usd = float(_runtime_config.get("daily_cost_ceiling_usd", 0))

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
    _median_cpm_by_type = (
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
                _type_cpms[ttype] > (_median_cpm_by_type * _CPM_RETIREMENT_MULTIPLIER)
                if ttype in _type_cpms and _median_cpm_by_type is not None
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

    # --- Scope mismatch diagnostics ---
    # Aggregate calibration data across terminal tasks in the window to surface
    # systematic scope-estimation drift and chronic-calibration directions.
    _terminal_in_window = completed_tasks + failed_tasks
    _calibrated_tasks = [
        t for t in _terminal_in_window if (t.context or {}).get("scope_calibrated_from")
    ]
    scope_mismatch_summary: Optional[Dict[str, Any]] = None
    if _calibrated_tasks:
        _cal_completed = sum(
            1 for t in _calibrated_tasks if t.status == TaskStatus.COMPLETED
        )
        _cal_total = len(_calibrated_tasks)
        _by_orig: Dict[str, Dict[str, int]] = {}
        _by_dir_calibrated: Dict[str, int] = {}
        for t in _calibrated_tasks:
            _orig_raw = str((t.context or {}).get("scope_calibrated_from") or "")
            _orig = _orig_raw.split(":", 1)[0] or "unknown"
            _bucket = _by_orig.setdefault(
                _orig, {"total": 0, "completed": 0, "failed": 0}
            )
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
            _top_dirs.append(
                {
                    "direction_id": _did,
                    "direction_text": dir_id_to_text.get(_did, ""),
                    "calibrated_count": _cal_cnt,
                    "total_tasks": _tot,
                    "calibration_rate": round(_cal_cnt / _tot, 3),
                }
            )
        _top_dirs.sort(
            key=lambda d: (d["calibration_rate"], d["calibrated_count"]), reverse=True
        )

        scope_mismatch_summary = {
            "total_calibrated": _cal_total,
            "calibrated_success_rate": round(_cal_completed / _cal_total, 3)
            if _cal_total
            else None,
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
            for _d in _proj_esc.directions or []:
                if _d.get("status") not in ("active", "held", "auto_paused"):
                    continue
                for _entry in _d.get("research_history") or []:
                    try:
                        _ets = datetime.fromisoformat(_entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if _ets < window_start:
                        continue
                    for _path, _counts in (
                        _entry.get("escalation_outcomes") or {}
                    ).items():
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
                "overall_success_rate": round(_tot_mer / _tot_att, 3)
                if _tot_att
                else None,
                "by_path": _by_path,
            }

    # --- Prevention-rate 7d aggregate ---
    avg_prevention_rate_7d: Optional[Dict[str, Any]] = None
    if project_id:
        from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
            compute_avg_prevention_rate_7d,
        )

        _prev_cases = list(
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project_id
                    )
                )
            ).scalars()
        )
        avg_prevention_rate_7d = compute_avg_prevention_rate_7d(_prev_cases)

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
            for _d in _proj_prev.directions or []:
                for _entry in _d.get("research_history") or []:
                    try:
                        _ets = datetime.fromisoformat(_entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if _ets < window_start:
                        continue
                    _prev_created += int(
                        _entry.get("preventive_context_tasks_created") or 0
                    )
                    _prev_merged += int(
                        _entry.get("preventive_context_tasks_merged") or 0
                    )
                    _prev_failed += int(
                        _entry.get("preventive_context_tasks_failed") or 0
                    )
                    _all_created += int(_entry.get("tasks_created") or 0)
                    _all_merged += int(_entry.get("prs_merged") or 0)
            if _prev_created == 0:
                prevention_efficacy = {
                    "preventive_tasks_created": 0,
                    "preventive_tasks_merged": 0,
                    "preventive_tasks_failed": 0,
                    "preventive_merge_rate": None,
                    "overall_merge_rate": round(_all_merged / _all_created, 3)
                    if _all_created
                    else None,
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
                    "overall_merge_rate": round(_overall_rate, 3)
                    if _overall_rate is not None
                    else None,
                    "assessment": _assessment,
                }

    # --- Latest strategic cycle + prediction calibration ---
    latest_cycle = None
    prediction_calibration: Dict[str, Any] | None = None
    fresh_reviewer_trend: Optional[Dict[str, Any]] = None
    edges_trend: Optional[Dict[str, Any]] = None
    self_review_prediction_accuracy_trends: Optional[Dict[str, Any]] = None
    sr_regression_risk_test_signal_trends: Optional[Dict[str, Any]] = None
    memo_pathway_exits: int = 0
    recent_cycle_memo_stats: list[dict[str, Any]] = []
    if project_id:
        _proj = await session.get(Project, project_id)
        memo_pathway_exits = await _strategic_cycle.count_memo_exits(
            session, project_id
        )
        if _proj:
            _sr_trends = (_proj.design_doc or {}).get("sr_catch_rate_trends") or {}
            if _sr_trends:
                self_review_prediction_accuracy_trends = (
                    _serialize_sr_catch_rate_trends(_sr_trends)
                )
            _sr_rrts = (_proj.design_doc or {}).get(
                "sr_regression_risk_test_signal_trends"
            )
            if _sr_rrts:
                sr_regression_risk_test_signal_trends = _sr_rrts
            cycle_history = (_proj.vision or {}).get("_strategic_cycle_history", [])
            completed_cycles = [
                c
                for c in cycle_history
                if c.get("outcome") not in (None, "in_progress")
            ]
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
                _cycle_id = last.get("cycle_id")
                if _cycle_id:
                    _sc_row = await session.get(StrategicCycle, _cycle_id)
                    if _sc_row is not None:
                        _ms = _sc_row.memo_stats()
                        latest_cycle[CYCLE_META_MEMOS_AVAILABLE] = _ms[
                            CYCLE_META_MEMOS_AVAILABLE
                        ]
                        latest_cycle[CYCLE_META_MEMOS_CITED] = _ms[
                            CYCLE_META_MEMOS_CITED
                        ]
            _sc_history = await _strategic_cycle.recent_for_agent_context(
                session,
                project_id,
                limit=20,
            )
            prediction_calibration = _compute_prediction_calibration(_proj, _sc_history)
            recent_cycle_memo_stats = [
                {
                    "cycle_id": c.get("cycle_id"),
                    "started_at": c.get("started_at"),
                    CYCLE_META_MEMOS_AVAILABLE: int(
                        c.get(CYCLE_META_MEMOS_AVAILABLE) or 0
                    ),
                    CYCLE_META_MEMOS_CITED: int(c.get(CYCLE_META_MEMOS_CITED) or 0),
                }
                for c in _sc_history
                if c.get("outcome") not in (None, "in_progress")
            ]
            # Fresh-reviewer cycle-over-cycle trend persisted by compute_fresh_reviewer_trend.
            _fr_trends = (_proj.design_doc or {}).get("fresh_reviewer_trend") or {}
            if _fr_trends:
                fresh_reviewer_trend = {
                    "computed_at": _fr_trends.get("computed_at"),
                    "bucket_mode": _fr_trends.get("bucket_mode"),
                    "current_cycle_id": _fr_trends.get("current_cycle_id"),
                    "previous_cycle_id": _fr_trends.get("previous_cycle_id"),
                    "current_sample_size": _fr_trends.get("current_sample_size"),
                    "previous_sample_size": _fr_trends.get("previous_sample_size"),
                    "per_category_hit_rate": _fr_trends.get("per_category_hit_rate")
                    or {},
                    "added_value": _fr_trends.get("added_value") or {},
                    "approval_rate_lift": _fr_trends.get("approval_rate_lift") or {},
                }
            # Edges-considered cycle-over-cycle trend persisted by compute_edges_trend.
            _et = (_proj.design_doc or {}).get("edges_trend") or {}
            if _et:
                edges_trend = {
                    "computed_at": _et.get("computed_at"),
                    "bucket_mode": _et.get("bucket_mode"),
                    "current_cycle_id": _et.get("current_cycle_id"),
                    "previous_cycle_id": _et.get("previous_cycle_id"),
                    "current": _et.get("current") or {},
                    "previous": _et.get("previous") or {},
                    "tasks_with_edges_rate_delta": _et.get(
                        "tasks_with_edges_rate_delta"
                    ),
                    "trend": _et.get("trend"),
                    "rate_history": _et.get("rate_history") or [],
                }

    _rejected_in_window = [
        t
        for t in all_tasks
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
        _hist = failure_class_history_classes(
            (_t.context or {}).get("_failure_class_history")
        )
        if len(_hist) >= 2:
            _multi_attempt.append(_hist)
    _repeat_count = sum(
        1 for h in _multi_attempt if any(h[i] == h[i + 1] for i in range(len(h) - 1))
    )
    _pattern_counts: Dict[str, int] = {}
    for _hist in _multi_attempt:
        _key = " -> ".join(_hist)
        _pattern_counts[_key] = _pattern_counts.get(_key, 0) + 1
    _top_patterns = sorted(_pattern_counts.items(), key=lambda x: x[1], reverse=True)[
        :3
    ]
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
        t
        for t in failed_tasks
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
        if (
            _t.vm_provisioned_at
            and _t.vm_stopped_at
            and _t.vm_stopped_at >= window_start
        ):
            _rate = SPOT_RATE if _t.vm_spot else ON_DEMAND_RATE
            _cap_total_cost += (
                _rate * (_t.vm_stopped_at - _t.vm_provisioned_at).total_seconds()
            )
    _top_cap_dirs = sorted(_cap_dir_counts.items(), key=lambda x: x[1], reverse=True)[
        :3
    ]
    review_cap_exhaustion_summary = {
        "count": len(_cap_tasks),
        "avg_iterations": (sum(_cap_iters) / len(_cap_iters)) if _cap_iters else None,
        "top_directions": [{"direction_id": d, "count": c} for d, c in _top_cap_dirs],
        "total_cost_usd": round(_cap_total_cost, 4),
        "avg_cost_usd": round(_cap_total_cost / len(_cap_tasks), 4)
        if _cap_tasks
        else None,
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
        "success_rate": (_resurrected_completed / _resurrected_total)
        if _resurrected_total
        else None,
    }

    # Remediation funnel: investigation -> diagnosis -> remediation -> merge.
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
        select(Task).where(
            *_funnel_filters,
            Task.context.has_key("diagnosis_task_id"),
        )
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
    _held_projects = (await session.execute(_held_projects_q)).scalars().all()
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
        cls = _classify_failure(r) or "unknown"
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

    # --- Residual remediation funnel (sourced from ResidualRemediationCase rows) ---
    _rr_spawned_filters = [ResidualRemediationCase.last_spawned_at >= window_start]
    if project_id:
        _rr_spawned_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_spawned_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_spawned_filters).limit(500)
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
        ResidualRemediationCase.status.in_(
            [
                ResidualRemediationCaseStatus.ACTIVE,
                ResidualRemediationCaseStatus.REMEDIATING,
                ResidualRemediationCaseStatus.SUSPENDED,
            ]
        ),
    ]
    if project_id:
        _rr_open_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_open_result = await session.execute(
        select(ResidualRemediationCase)
        .where(*_rr_open_filters)
        .order_by(ResidualRemediationCase.last_seen_at.desc())
        .limit(500)
    )
    _rr_open_cases = list(_rr_open_result.scalars().all())
    residual_remediation_funnel: Dict[str, Any] = {
        "spawned": len(_rr_cases),
        "merged": sum(
            1 for c in _rr_cases if c.status == ResidualRemediationCaseStatus.MERGED
        ),
        "failed": sum(
            1
            for c in _rr_cases
            if c.status
            in (
                ResidualRemediationCaseStatus.FAILED,
                ResidualRemediationCaseStatus.SUSPENDED,
            )
        ),
        "remediating": len(list(_rr_active_result.scalars().all())),
        "open_cases_by_error_class": [
            {
                "error_class": "CODE_ERROR",
                "count": len(_rr_open_cases),
                "most_recent_at": _rr_open_cases[0].last_seen_at.isoformat()
                if _rr_open_cases
                else None,
                "cases": [
                    {
                        "fingerprint": c.fingerprint,
                        "status": c.status,
                        "hit_count": c.hit_count,
                        "last_seen_at": c.last_seen_at.isoformat()
                        if c.last_seen_at
                        else None,
                        "last_spawned_at": c.last_spawned_at.isoformat()
                        if c.last_spawned_at
                        else None,
                        "sample_message": c.sample_message,
                        "consecutive_failure_count": c.consecutive_failure_count,
                    }
                    for c in _rr_open_cases[:30]
                ],
            }
        ]
        if _rr_open_cases
        else [],
    }

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
        cumulative_merged_by_trigger_class[key] = (
            cumulative_merged_by_trigger_class.get(key, 0) + 1
        )

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

    # --- Anomaly-driven pipeline adjustment snapshot ---
    from app.shared_state import anomaly_adjustments as _anomaly_adjustments  # noqa: PLC0415

    anomaly_adjustment_summary = {
        "active": bool(_anomaly_adjustments.get("active")),
        "adjusted_at": _anomaly_adjustments.get("adjusted_at"),
        "trigger_anomalies": list(_anomaly_adjustments.get("trigger_anomalies") or []),
        "pre_adjustment_max_vms": _anomaly_adjustments.get("pre_adjustment_max_vms"),
        "adjusted_max_vms": _anomaly_adjustments.get("adjusted_max_vms"),
        "failure_gate_threshold": _anomaly_adjustments.get("failure_gate_threshold"),
        "pre_adjustment_failure_gate": _anomaly_adjustments.get(
            "pre_adjustment_failure_gate"
        ),
        "forced_restoration_count": _anomaly_adjustments.get(
            "forced_restoration_count", 0
        ),
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
    _inflight_statuses = {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
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
        _entry = _fp_task_states.setdefault(
            str(_fp_), {"inflight": False, "merged": False, "proactive": False}
        )
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
        if project_id
        else _persistent_anomalies
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
            if not isinstance(_info, dict):
                continue
            if _info.get("occurrence_count", 0) < 2:
                continue
            if _key.startswith("FailureRateMomentum"):
                _pcts = _momentum_rate_re.findall(_info.get("latest_details") or "")[:3]
                early_warnings.append(
                    {
                        "failure_class": _key,
                        "rate_progression": [int(p) / 100 for p in _pcts]
                        if len(_pcts) == 3
                        else [],
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
                    }
                )
                continue
            _fp_key = (
                _key[len("CrossDirectionFingerprint_") :]
                if _key.startswith("CrossDirectionFingerprint_")
                else _key
            )
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
            recurring_patterns.append(
                {
                    "fingerprint": _fp_key,
                    "occurrence_count": _info.get("occurrence_count", 0),
                    "first_seen": _info.get("first_seen"),
                    "last_seen": _info.get("last_seen"),
                    "severity": _info.get("severity"),
                    "example_message": (_info.get("latest_details") or "")[:240],
                    "affected_direction_count": len(_affected_dir_ids),
                    "affected_directions": [
                        {
                            "direction_id": _did,
                            "direction_text": dir_id_to_text.get(_did, ""),
                        }
                        for _did in _affected_dir_ids
                    ],
                    "remediation_status": _status,
                    "proactive": bool(_state.get("proactive", False)),
                }
            )
    recurring_patterns.sort(key=lambda x: x["occurrence_count"], reverse=True)

    _review_quality = _compute_review_quality_metrics(completed_tasks + failed_tasks)
    _self_review_skip_rate = _compute_sr_skip_rate(completed_tasks + failed_tasks)
    _sr_disallowed_blind_spot_rate = _compute_sr_disallowed_blind_spot_rate(
        completed_tasks + failed_tasks
    )
    _first_attempt_success_rate = _compute_first_attempt_breakdowns(
        completed_tasks + failed_tasks,
        prev_terminal_tasks=prev_completed_tasks + prev_failed_tasks,
    )
    _first_review_approval_rate = _compute_first_review_approval_breakdowns(
        completed_tasks + failed_tasks
    )

    from app.main import EXPECTED_SCHEMA_REVISION
    from app.failure_classification import classify_failure as _mig_classify

    def _task_type(t: Task) -> Optional[str]:
        return (t.first_payload or {}).get("type")

    def _touched_migration(t: Task) -> bool:
        ctx = t.context or {}
        files = ctx.get("related_files") or []
        if isinstance(files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/")
            for f in files
        ):
            return True
        payload_files = (t.first_payload or {}).get("related_files") or []
        return isinstance(payload_files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/")
            for f in payload_files
        )

    _migrate_completed = [t for t in completed_tasks if _task_type(t) == "migrate"]
    _migrate_failed = [t for t in failed_tasks if _task_type(t) == "migrate"]
    _migration_errors = sum(
        1
        for t in failed_tasks
        if (t.context or {}).get("failure_class") == "MIGRATION_ERROR"
        or _mig_classify(t, error_log_map.get(str(t.id))) == "MIGRATION_ERROR"
    )
    _auto_generated_migrations = sum(
        1
        for t in (completed_tasks + failed_tasks)
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
        "self_review_prediction_accuracy": _review_quality[
            "self_review_prediction_accuracy"
        ],
        "self_review_prediction_accuracy_trends": self_review_prediction_accuracy_trends,
        "sr_regression_risk_test_signal_trends": sr_regression_risk_test_signal_trends,
        "self_review_skip_rate": _self_review_skip_rate,
        "sr_disallowed_blind_spot_rate": _sr_disallowed_blind_spot_rate,
        "review_accuracy": _review_quality["review_accuracy"],
        "fresh_reviewer_effectiveness": _review_quality["fresh_reviewer_effectiveness"],
        "fresh_reviewer_trend": fresh_reviewer_trend,
        "edges_trend": edges_trend,
        "edges_considered_metrics": _review_quality["edges_considered_metrics"],
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
        "memo_pathway_exits": memo_pathway_exits,
        "recent_cycle_memo_stats": recent_cycle_memo_stats,
        "scope_conversion_rates": scope_conversion_rates or None,
        "prev_scope_conversion_rates": prev_scope_conversion_rates or None,
        "pipeline_acceptance_rate": pipeline_acceptance_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "pipeline_autonomous_prs_merged": pipeline_autonomous_prs_merged,
        "pipeline_assisted_prs_merged": pipeline_assisted_prs_merged,
        "pipeline_operator_action_count": pipeline_operator_action_count,
        "pipeline_filter_funnel": pipeline_filter_funnel or None,
        "scope_mismatch_summary": scope_mismatch_summary,
        "escalation_outcome_summary": escalation_outcome_summary,
        "avg_prevention_rate_7d": avg_prevention_rate_7d,
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
        "prediction_calibration_factor": prediction_calibration["factor"]
        if prediction_calibration
        else None,
        "prediction_calibration_note": prediction_calibration["note"]
        if prediction_calibration
        else None,
        "prediction_calibration_sample_count": prediction_calibration["sample_count"]
        if prediction_calibration
        else None,
    }


# ---------------------------------------------------------------------------
# Fix-loop convergence aggregate endpoint
# ---------------------------------------------------------------------------


@router.get("/fix-loop-convergence")
async def get_fix_loop_convergence(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate fix-loop telemetry across recently-completed/failed tasks.

    Returns per-loop average iterations, convergence rate, divergence reason
    histogram, and iteration-count distribution suitable for the dashboard
    convergence widget.  Also returns ``tasks_abandoned_on_divergence`` (int)
    and ``recent_abandoned_task_ids`` (list, capped at 25) for drill-in.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.context,
            )
        )
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    result = await session.execute(stmt)
    tasks = result.scalars().all()

    # Only consider tasks that have at least one fix-loop event recorded.
    tasks_with_history = [t for t in tasks if t.fix_loop_history]

    # Abandoned-on-divergence counters (additive — do not affect per-loop aggs).
    abandoned_ids: List[str] = []
    for t in tasks:
        if (t.context or {}).get("fix_loop_abandoned"):
            abandoned_ids.append(str(t.id))
    abandoned_ids_recent = abandoned_ids[
        -25:
    ]  # keep 25 most recent (insertion order ~ created_at)

    loop_names = ("lint", "test", "check")

    # Per-loop bucket: list of (iterations, exit_reason) tuples
    per_loop: Dict[str, List[tuple]] = {loop: [] for loop in loop_names}

    for task in tasks_with_history:
        history = task.fix_loop_history or {}
        for loop in loop_names:
            bucket = history.get(loop) or []
            if not bucket:
                continue
            iterations = len(bucket)
            # Terminal exit reason is the last non-in_progress entry, else last entry.
            last_exit: Optional[str] = None
            for ev in reversed(bucket):
                reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                if reason and reason != "in_progress":
                    last_exit = reason
                    break
            if last_exit is None and bucket:
                last_exit = (
                    bucket[-1].get("exit_reason")
                    if isinstance(bucket[-1], dict)
                    else None
                )
            per_loop[loop].append((iterations, last_exit))

    def _aggregate_loop(entries: List[tuple]) -> Dict[str, Any]:
        if not entries:
            return {
                "task_count": 0,
                "avg_iterations": None,
                "convergence_rate": None,
                "exit_reason_counts": {},
                "iteration_distribution": {},
            }
        total_tasks = len(entries)
        total_iterations = sum(n for n, _ in entries)
        avg_iterations = round(total_iterations / total_tasks, 2)

        converged = sum(1 for _, r in entries if r == "passed")
        convergence_rate = round(converged / total_tasks, 3)

        exit_reason_counts: Dict[str, int] = {}
        for _, reason in entries:
            key = reason or "unknown"
            exit_reason_counts[key] = exit_reason_counts.get(key, 0) + 1

        # Iteration distribution: bucket by count (1, 2, 3, 4+)
        dist: Dict[str, int] = {"1": 0, "2": 0, "3": 0, "4+": 0}
        for n, _ in entries:
            if n == 1:
                dist["1"] += 1
            elif n == 2:
                dist["2"] += 1
            elif n == 3:
                dist["3"] += 1
            else:
                dist["4+"] += 1

        return {
            "task_count": total_tasks,
            "avg_iterations": avg_iterations,
            "convergence_rate": convergence_rate,
            "exit_reason_counts": exit_reason_counts,
            "iteration_distribution": dist,
        }

    return {
        "window_hours": hours,
        "tasks_scanned": len(tasks),
        "tasks_with_fix_loops": len(tasks_with_history),
        "tasks_abandoned_on_divergence": len(abandoned_ids),
        "recent_abandoned_task_ids": abandoned_ids_recent,
        "lint": _aggregate_loop(per_loop["lint"]),
        "test": _aggregate_loop(per_loop["test"]),
        "check": _aggregate_loop(per_loop["check"]),
    }


_MAX_RETIREMENT_CRITIQUES = 100


@router.get("/projects/{project_id}/retirement-critiques")
async def get_retirement_critiques(
    project_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=_MAX_RETIREMENT_CRITIQUES),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return recent retirement critiques stored across StrategicCycle rows.

    Each StrategicCycle may carry a list of retirement critiques written by the
    strategic agent when it retires directions. Returns the most recent `limit`
    critiques across the last 20 completed cycles, ordered newest-cycle-first.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    result = await session.execute(
        select(StrategicCycle)
        .options(
            load_only(
                StrategicCycle.id,
                StrategicCycle.cycle_metadata,
                StrategicCycle.created_at,
            )
        )
        .where(StrategicCycle.project_id == project_id)
        .order_by(StrategicCycle.created_at.desc())
        .limit(20)
    )
    cycles = result.scalars().all()

    critiques_out: list[dict] = []
    for cycle in cycles:
        if len(critiques_out) >= limit:
            break
        raw = cycle.cycle_metadata or {}
        critiques = raw.get("retirement_critiques", [])
        if not isinstance(critiques, list):
            continue
        for c in critiques:
            if not isinstance(c, dict):
                continue
            if c.get("no_eligible_retirements"):
                continue
            critiques_out.append(
                {
                    "direction_id": c.get("direction_id"),
                    "what_it_addressed": c.get("what_it_addressed", ""),
                    "pattern_that_produced_it": c.get("pattern_that_produced_it", ""),
                    "lesson": c.get("lesson", ""),
                    "cycle_id": cycle.id,
                    "cycle_created_at": cycle.created_at.isoformat()
                    if cycle.created_at
                    else None,
                }
            )
            if len(critiques_out) >= limit:
                break

    return {"critiques": critiques_out, "total": len(critiques_out)}


_MAX_ACTIONABILITY_CYCLES = 50


def _aggregate_actionability_bucket(
    research_tasks_in_bucket: list,
    followon_tasks: list,
    research_task_ids: set,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Aggregate (grade_distribution, grade_tasked, grade_merged) for a bucket
    of research tasks. Reuses the same merge-attribution rules as the global
    aggregation: research tasks are excluded from follow-ons (self-attribution
    guard), and a follow-on must be created strictly after the bucket's
    earliest research task that produced the finding's direction.
    """
    finding_direction_ids: set[str] = set()
    findings_by_dir: dict[str, list[dict]] = {}
    grade_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    dir_research_first_at: dict[str, datetime] = {}

    for t in research_tasks_in_bucket:
        raw = t.plan_output
        if not isinstance(raw, list):
            continue
        for f in raw:
            if not isinstance(f, dict) or not f.get("title"):
                continue
            grade = f.get("actionability_grade", "MEDIUM")
            if grade not in grade_counts:
                grade = "MEDIUM"
            grade_counts[grade] += 1
            dir_id = f.get("direction_id")
            if dir_id:
                key = str(dir_id)
                finding_direction_ids.add(key)
                findings_by_dir.setdefault(key, []).append({"grade": grade})
                if t.created_at is not None:
                    prev = dir_research_first_at.get(key)
                    if prev is None or t.created_at < prev:
                        dir_research_first_at[key] = t.created_at

    dir_outcome: dict[str, dict] = {}
    for t in followon_tasks:
        if t.id in research_task_ids:
            continue
        t_type = (t.first_payload or {}).get("type")
        if t_type == "research":
            continue
        ctx = t.context or {}
        dir_id = ctx.get("direction_id") or ctx.get("related_direction_id")
        if not dir_id or str(dir_id) not in finding_direction_ids:
            continue
        key = str(dir_id)
        research_at = dir_research_first_at.get(key)
        if (
            research_at is not None
            and t.created_at is not None
            and t.created_at <= research_at
        ):
            continue
        entry = dir_outcome.setdefault(key, {"tasked": False, "merged": False})
        entry["tasked"] = True
        if t.status == TaskStatus.COMPLETED and ctx.get("pr_merged") is True:
            entry["merged"] = True

    grade_tasked: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    grade_merged: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for dir_id, findings in findings_by_dir.items():
        outcome = dir_outcome.get(dir_id, {})
        for f in findings:
            g = f["grade"]
            if outcome.get("tasked"):
                grade_tasked[g] = grade_tasked.get(g, 0) + 1
            if outcome.get("merged"):
                grade_merged[g] = grade_merged.get(g, 0) + 1

    return grade_counts, grade_tasked, grade_merged


def _compute_actionability_rates(
    grade_counts: dict[str, int],
    grade_tasked: dict[str, int],
    grade_merged: dict[str, int],
) -> tuple[dict[str, Optional[float]], dict[str, Optional[float]], Optional[float]]:
    """Derive (grade_merge_rate, grade_merge_rate_of_tasked, calibration_score).

    Rates are None (not 0.0) when the denominator is 0 so the UI can
    distinguish 'no data' from 'zero merges'. calibration_score is HIGH-rate
    minus LOW-rate, or None when either is None.
    """
    grade_merge_rate: dict[str, Optional[float]] = {}
    grade_merge_rate_of_tasked: dict[str, Optional[float]] = {}
    for g in ("HIGH", "MEDIUM", "LOW"):
        gd = grade_counts.get(g, 0)
        gt = grade_tasked.get(g, 0)
        gm = grade_merged.get(g, 0)
        grade_merge_rate[g] = (gm / gd) if gd > 0 else None
        grade_merge_rate_of_tasked[g] = (gm / gt) if gt > 0 else None
    high_rate = grade_merge_rate["HIGH"]
    low_rate = grade_merge_rate["LOW"]
    calibration_score = (
        high_rate - low_rate
        if (high_rate is not None and low_rate is not None)
        else None
    )
    return grade_merge_rate, grade_merge_rate_of_tasked, calibration_score


@router.get("/projects/{project_id}/research-actionability")
async def get_research_actionability(
    project_id: uuid.UUID,
    cycles: int = Query(default=10, ge=1, le=_MAX_ACTIONABILITY_CYCLES),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return actionability grade distribution + merge outcome by grade tier.

    Scans research task plan_output for the most recent `cycles` completed
    strategic cycles (by StrategicCycle.created_at). For each finding, records
    its actionability_grade and whether it spawned a follow-on task whose PR
    landed.

    Merge linkage heuristic: a finding is counted as "tasked" when a non-research
    task in the same project shares its direction_id and was created strictly
    after the research task that produced the finding (research tasks are
    excluded from the follow-on set so a research task does not self-attribute).
    It is counted as "merged" when that follow-on task is COMPLETED and its
    `context['pr_merged']` flag is True — that flag is set by the merge pipeline
    only after a PULL_REQUEST artifact is actually merged, so it is a reliable
    proxy for "has a merged-PR artifact." This is conservative — it under-counts
    rather than over-counts.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    # Determine time window from the N most recent strategic cycles.
    cycle_result = await session.execute(
        select(StrategicCycle)
        .options(load_only(StrategicCycle.id, StrategicCycle.created_at))
        .where(StrategicCycle.project_id == project_id)
        .order_by(StrategicCycle.created_at.desc())
        .limit(cycles)
    )
    recent_cycles = cycle_result.scalars().all()
    if recent_cycles:
        oldest_cycle_at = min(c.created_at for c in recent_cycles if c.created_at)
    else:
        oldest_cycle_at = _utcnow() - timedelta(days=30)

    # Fetch completed research tasks within the window. The payload-type filter
    # is essential: plan_output is also written by non-research task types
    # (planning, etc.) and would otherwise leak into the grade distribution.
    research_result = await session.execute(
        select(Task)
        .options(load_only(Task.id, Task.plan_output, Task.created_at, Task.context))
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= oldest_cycle_at,
            Task.plan_output.isnot(None),
            Task.payloads[0]["type"].as_string() == "research",
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    research_tasks = research_result.scalars().all()
    research_task_ids: set[uuid.UUID] = {t.id for t in research_tasks}

    # Fetch follow-on tasks (same project, after oldest_cycle_at). We pull
    # payloads so we can exclude research-type tasks below.
    followon_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id, Task.status, Task.context, Task.created_at, Task.payloads
            )
        )
        .where(
            Task.project_id == project_id,
            Task.created_at >= oldest_cycle_at,
        )
        .limit(2000)
    )
    followon_tasks = followon_result.scalars().all()

    # Global aggregation across the full window.
    grade_counts, grade_tasked, grade_merged = _aggregate_actionability_bucket(
        research_tasks,
        followon_tasks,
        research_task_ids,
    )
    grade_merge_rate, grade_merge_rate_of_tasked, calibration_score = (
        _compute_actionability_rates(grade_counts, grade_tasked, grade_merged)
    )

    # Per-cycle bucketing. Each cycle window = [cycle.created_at, next_newer_cycle.created_at);
    # the newest cycle has an open upper bound.
    cycles_oldest_first = sorted(
        [c for c in recent_cycles if c.created_at is not None],
        key=lambda c: c.created_at,
    )
    cycle_history: list[dict[str, Any]] = []
    for idx, cyc in enumerate(cycles_oldest_first):
        lo = cyc.created_at
        hi = (
            cycles_oldest_first[idx + 1].created_at
            if idx + 1 < len(cycles_oldest_first)
            else None
        )
        bucket_tasks = [
            t
            for t in research_tasks
            if t.created_at is not None
            and t.created_at >= lo
            and (hi is None or t.created_at < hi)
        ]
        b_counts, b_tasked, b_merged = _aggregate_actionability_bucket(
            bucket_tasks,
            followon_tasks,
            research_task_ids,
        )
        b_rate, _b_rate_t, b_calib = _compute_actionability_rates(
            b_counts,
            b_tasked,
            b_merged,
        )
        cycle_history.append(
            {
                "cycle_id": cyc.id,
                "cycle_created_at": cyc.created_at.isoformat(),
                "total_findings": sum(b_counts.values()),
                "structural_downgrades": sum(
                    int(
                        (t.context or {}).get("_actionability_structural_downgrades", 0)
                        or 0
                    )
                    for t in bucket_tasks
                ),
                "grade_distribution": b_counts,
                "grade_merged": b_merged,
                "grade_merge_rate": b_rate,
                "calibration_score": b_calib,
            }
        )

    total = sum(grade_counts.values())
    return {
        "cycles_scanned": len(recent_cycles),
        "total_findings": total,
        "grade_distribution": grade_counts,
        "grade_tasked": grade_tasked,
        "grade_merged": grade_merged,
        "grade_merge_rate": grade_merge_rate,
        "grade_merge_rate_of_tasked": grade_merge_rate_of_tasked,
        "calibration_score": calibration_score,
        "cycle_history": cycle_history,
    }


# ---------------------------------------------------------------------------
# Fix-loop convergence daily trend endpoint
# ---------------------------------------------------------------------------


@router.get("/fix-loop-convergence-trend")
async def get_fix_loop_convergence_trend(
    days: int = Query(7, ge=1, le=30),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return per-day buckets of fix-loop convergence and divergence rates.

    Each bucket contains, for lint/test/check loops:
      - task_count: tasks with that loop type
      - convergence_rate: fraction that exited with 'passed'
      - divergence_rate: fraction that exited with a divergence_* reason
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.created_at,
            )
        )
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    result = await session.execute(stmt)
    tasks = result.scalars().all()

    loop_names = ("lint", "test", "check")

    # day_str -> loop -> list of exit_reasons
    day_buckets: Dict[str, Dict[str, List[Optional[str]]]] = defaultdict(
        lambda: {loop: [] for loop in loop_names}
    )

    for task in tasks:
        history = task.fix_loop_history or {}
        if not history:
            continue
        created = task.created_at
        if created is None:
            continue
        day_str = created.strftime("%Y-%m-%d")
        for loop in loop_names:
            bucket = history.get(loop) or []
            if not bucket:
                continue
            last_exit: Optional[str] = None
            for ev in reversed(bucket):
                reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                if reason and reason != "in_progress":
                    last_exit = reason
                    break
            if last_exit is None and bucket:
                last_exit = (
                    bucket[-1].get("exit_reason")
                    if isinstance(bucket[-1], dict)
                    else None
                )
            day_buckets[day_str][loop].append(last_exit)

    def _day_loop_stats(reasons: List[Optional[str]]) -> Dict[str, Any]:
        n = len(reasons)
        if n == 0:
            return {"task_count": 0, "convergence_rate": None, "divergence_rate": None}
        converged = sum(1 for r in reasons if r == "passed")
        diverged = sum(1 for r in reasons if r and r.startswith("divergence_"))
        return {
            "task_count": n,
            "convergence_rate": round(converged / n, 3),
            "divergence_rate": round(diverged / n, 3),
        }

    # Build sorted list covering every day in the window
    buckets = []
    for i in range(days - 1, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        entry: Dict[str, Any] = {"date": day}
        day_data = day_buckets.get(day, {ln: [] for ln in loop_names})
        for loop in loop_names:
            entry[loop] = _day_loop_stats(day_data.get(loop, []))
        buckets.append(entry)

    if project_id is not None:
        try:
            from app.playbook_learning import check_fix_loop_divergence_regression  # noqa: PLC0415

            await check_fix_loop_divergence_regression(project_id, session)
        except Exception:
            logger.exception(
                "[ANALYTICS] fix_loop_divergence_regression check failed (non-critical)"
            )

    return {
        "days": days,
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# Fix-loop diverged-task drill-down endpoint
# ---------------------------------------------------------------------------


@router.get("/fix-loop-diverged-tasks")
async def get_fix_loop_diverged_tasks(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    loop: str | None = Query(
        None, description="Filter by loop name: lint, test, or check"
    ),
    exit_reason: str | None = Query(
        None, description="Filter by exit reason, e.g. divergence_oscillation"
    ),
    limit: int = Query(50, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List tasks that exited a fix loop with a specific reason in the given window.

    Returns up to `limit` tasks ordered by most-recent first, with enough
    context (title, type, scope, project, exit reason, iteration count,
    error_tail) for an operator to decide which task to investigate next.
    """
    if loop is not None and loop not in ("lint", "test", "check"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid loop '{loop}'. Must be one of: lint, test, check.",
        )

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.title,
                Task.status,
                Task.fix_loop_history,
                Task.context,
                Task.payloads,
                Task.created_at,
            )
        )
        .order_by(Task.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    result = await session.execute(stmt)
    tasks = result.scalars().all()

    loop_names = [loop] if loop is not None else ["lint", "test", "check"]

    rows = []
    for task in tasks:
        if len(rows) >= limit:
            break
        history = task.fix_loop_history or {}
        for lname in loop_names:
            bucket = history.get(lname) or []
            if not bucket:
                continue
            # Resolve terminal exit reason (last non-in_progress event).
            last_exit: Optional[str] = None
            last_error_tail: Optional[str] = None
            for ev in reversed(bucket):
                if not isinstance(ev, dict):
                    continue
                reason = ev.get("exit_reason")
                if reason and reason != "in_progress":
                    last_exit = reason
                    last_error_tail = ev.get("error_tail")
                    break
            if exit_reason is not None and last_exit != exit_reason:
                continue
            if last_exit is None or last_exit == "passed":
                continue
            ctx = task.context or {}
            rows.append(
                {
                    "task_id": str(task.id),
                    "title": task.title or "Untitled",
                    "task_type": (task.first_payload or {}).get("type", "unknown"),
                    "scope": ctx.get("scope"),
                    "project_id": str(task.project_id),
                    "status": task.status,
                    "loop": lname,
                    "exit_reason": last_exit,
                    "iteration_count": len(bucket),
                    "error_tail": last_error_tail,
                }
            )
            if len(rows) >= limit:
                break

    return {
        "window_hours": hours,
        "loop_filter": loop,
        "exit_reason_filter": exit_reason,
        "total": len(rows),
        "tasks": rows,
    }


@router.get("/projects/{project_id}/sr-missed-retrospectives")
async def list_sr_missed_retrospectives(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    category: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List code-change tasks where self-review passed a category that human review later flagged.

    Useful for operator-driven learning: surface systematic blind spots so prompt
    or check changes can target them.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    page_start = page * page_size
    page_end = page_start + page_size

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.created_at,
                Task.context,
                Task.project_id,
                Task.payloads,
            )
        )
        .order_by(Task.created_at.desc())
    )

    page_items = []
    total_matched = 0
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        ctx = t.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        check_results = sr_outcome.get("check_results") or {}
        if not check_results:
            continue
        human_cats = resolve_sr_miss_human_truth(ctx)
        if not human_cats:
            continue
        sr_failed = _author_sr_failed_categories(check_results)
        missed = human_cats - sr_failed
        if not missed:
            continue
        t_type = (t.first_payload or {}).get("type")
        if category and category not in missed:
            continue
        if task_type and t_type != task_type:
            continue

        current_index = total_matched
        total_matched += 1
        if current_index < page_start or current_index >= page_end:
            continue

        caught = human_cats & sr_failed
        sr_check_results, reviewer_findings = _sr_miss_drilldown(sr_outcome, missed)
        page_items.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": t_type,
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "missed_categories": sorted(missed),
                "caught_categories": sorted(caught),
                "sr_check_results": sr_check_results,
                "reviewer_findings": reviewer_findings,
            }
        )

    # Batch-fetch PR artifacts for items on this page
    page_task_ids = [uuid.UUID(m["task_id"]) for m in page_items]
    pr_map: Dict[str, str] = {}
    if page_task_ids:
        art_result = await session.execute(
            select(Artifact.task_id, Artifact.pr_url).where(
                Artifact.task_id.in_(page_task_ids),
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            )
        )
        for row in art_result.all():
            if row.pr_url:
                pr_map[str(row.task_id)] = row.pr_url

    items = []
    for m in page_items:
        items.append(
            {
                "task_id": m["task_id"],
                "title": m["title"],
                "task_type": m["task_type"],
                "scope": m["scope"],
                "created_at": m["created_at"],
                "missed_categories": m["missed_categories"],
                "caught_categories": m["caught_categories"],
                "pr_url": pr_map.get(m["task_id"]),
            }
        )

    return {"items": items, "total": total_matched}


@router.get("/projects/{project_id}/sr-unhandled-edges-retrospectives")
async def list_sr_unhandled_edges_retrospectives(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    task_type: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List code-change tasks with unhandled edge cases flagged by the agent.

    Surfaces which specific recent tasks shipped with unhandled-edge admissions
    (handled: false in edges_considered), enabling audit of whether these
    correlate with later reviewer rejections and spot patterns.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    page_start = page * page_size
    page_end = page_start + page_size

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.created_at,
                Task.context,
                Task.project_id,
                Task.payloads,
            )
        )
        .order_by(Task.created_at.desc())
    )

    page_items = []
    total_matched = 0
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        ctx = t.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        edges = sr_outcome.get("edges_considered") or []
        if not isinstance(edges, list):
            edges = []

        # Find unhandled edges
        unhandled_edges = [
            e for e in edges if isinstance(e, dict) and not e.get("handled", True)
        ]
        if not unhandled_edges:
            continue

        t_type = (t.first_payload or {}).get("type")
        if task_type and t_type != task_type:
            continue

        current_index = total_matched
        total_matched += 1
        if current_index < page_start or current_index >= page_end:
            continue

        page_items.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": t_type,
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "unhandled_edges": unhandled_edges,
            }
        )

    # Batch-fetch PR artifacts for items on this page
    page_task_ids = [uuid.UUID(m["task_id"]) for m in page_items]
    pr_map: Dict[str, str] = {}
    if page_task_ids:
        art_result = await session.execute(
            select(Artifact.task_id, Artifact.pr_url).where(
                Artifact.task_id.in_(page_task_ids),
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            )
        )
        for row in art_result.all():
            if row.pr_url:
                pr_map[str(row.task_id)] = row.pr_url

    items = []
    for m in page_items:
        items.append(
            {
                "task_id": m["task_id"],
                "title": m["title"],
                "task_type": m["task_type"],
                "scope": m["scope"],
                "created_at": m["created_at"],
                "unhandled_edges": m["unhandled_edges"],
                "pr_url": pr_map.get(m["task_id"]),
            }
        )

    return {"items": items, "total": total_matched}


@router.get("/projects/{project_id}/sr-alert-drilldown")
async def get_sr_alert_drilldown(
    project_id: uuid.UUID,
    categories: List[str] = Query(...),
    alert_type: str = Query("trends"),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return tasks from the current SR alert window where SR missed the given categories.

    Used to drill down from a regression alert to the tasks that contributed to the
    catch-rate regression.  Window is derived from the stored alert state (cycle_id
    or 14-day calendar fallback).  alert_type must be 'trends' or 'floor'.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    if alert_type not in ("trends", "floor"):
        raise HTTPException(
            status_code=400, detail="alert_type must be 'trends' or 'floor'"
        )

    from app.playbook_learning import get_sr_alert_drilldown_tasks  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    doc = project.design_doc or {}

    stmt = (
        select(Task)
        .where(Task.project_id == project_id, Task.status == TaskStatus.COMPLETED)
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.created_at,
                Task.context,
                Task.payloads,
                Task.strategic_cycle_id,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(2000)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    rows = get_sr_alert_drilldown_tasks(
        all_tasks, doc, categories, alert_type=alert_type, page_size=page_size
    )

    task_ids = [uuid.UUID(r["task_id"]) for r in rows]
    pr_map: Dict[str, str] = {}
    if task_ids:
        art_result = await session.execute(
            select(Artifact.task_id, Artifact.pr_url).where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            )
        )
        for art_row in art_result.all():
            if art_row.pr_url:
                pr_map[str(art_row.task_id)] = art_row.pr_url

    task_map = {str(t.id): t for t in all_tasks}

    for r in rows:
        r["pr_url"] = pr_map.get(r["task_id"])

        task = task_map.get(r["task_id"])
        ctx = (task.context or {}) if task else {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        findings_list = sr_outcome.get("reviewer_findings") or []
        findings_by_cat = {
            f["category"]: f
            for f in findings_list
            if isinstance(f, dict) and "category" in f
        }
        missed_findings: Dict[str, Any] = {}
        for cat in r.get("missed_categories", []):
            finding = findings_by_cat.get(cat)
            if finding:
                desc = finding.get("description") or ""
                missed_findings[cat] = {
                    "severity": finding.get("severity"),
                    "description": desc[:140],
                    "has_more": len(desc) > 140,
                    "full_description": desc if len(desc) > 140 else None,
                }
            else:
                missed_findings[cat] = {
                    "severity": None,
                    "description": "no description recorded",
                    "has_more": False,
                    "full_description": None,
                }
        r["missed_category_findings"] = missed_findings

    return {
        "items": rows,
        "total": len(rows),
        "alert_type": alert_type,
        "categories": categories,
    }


@router.get("/pipeline-regressions")
async def get_pipeline_regressions(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate active pipeline regression signals with autonomous loop response status.

    Reads flags already written by the existing detectors (sr_catch_rate_trends_alert,
    fix_loop_divergence_alerted_window, Alert table) — does not re-implement detection.
    Returns an empty regressions list when the system is healthy. Field names match
    the PipelineRegression interface consumed by admin-ui PipelineRegressionsPanel.
    """
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    doc = project.design_doc or {}
    now = datetime.utcnow()
    regressions: List[Dict[str, Any]] = []

    # SR catch-rate regression signals — regression_class uses the colon form the
    # dispatcher actually persists (pipeline_regression_dispatcher.py:306).
    sr_alert = doc.get("sr_catch_rate_trends_alert") or {}
    for cat in list(sr_alert.get("categories") or []):
        cat_data = (
            (doc.get("sr_catch_rate_trends") or {}).get("categories") or {}
        ).get(cat) or {}
        regressions.append(
            {
                "signal_name": f"SR catch-rate regression: {cat}",
                "regression_class": f"sr_catch_rate:{cat}",
                "detected_at": sr_alert.get("computed_at"),
                "severity": "warning",
                "current_rate": cat_data.get("current_hit_rate"),
                "prior_rate": cat_data.get("previous_hit_rate"),
                "resolved": False,
                "resolved_at": None,
            }
        )

    # Fix-loop divergence regression signals — colon form per dispatcher line 394.
    iso_year, iso_week, _ = now.isocalendar()
    if doc.get("fix_loop_divergence_alerted_window") == f"{iso_year}-W{iso_week:02d}":
        for reg in list(doc.get("fix_loop_divergence_regressions") or []):
            loop = reg.get("loop", "unknown")
            regressions.append(
                {
                    "signal_name": f"Fix-loop divergence regression: {loop} loop",
                    "regression_class": f"fix_loop_divergence:{loop}",
                    "detected_at": doc.get("fix_loop_divergence_detected_at"),
                    "severity": "warning",
                    "current_rate": reg.get("current_rate"),
                    "prior_rate": reg.get("prior_rate"),
                    "resolved": False,
                    "resolved_at": None,
                }
            )

    # Background service crash signals — undismissed ERROR alerts in last 48h.
    crash_cutoff = now - timedelta(hours=48)
    crash_rows = (
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.created_at >= crash_cutoff,
                    Alert.dismissed_at.is_(None),
                )
                .order_by(Alert.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    _CRASH_TITLE_PREFIX = "Background service crashed: "
    for alert in crash_rows:
        if alert.title and alert.title.startswith(_CRASH_TITLE_PREFIX):
            service_name = alert.title[len(_CRASH_TITLE_PREFIX) :].strip() or "unknown"
            reg_class = f"background_service_crash:{service_name}"
        else:
            reg_class = "background_service_crash"
        regressions.append(
            {
                "signal_name": alert.title,
                "regression_class": reg_class,
                "detected_at": alert.created_at.isoformat()
                if alert.created_at
                else None,
                "severity": "critical",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
            }
        )

    staleness_rows = (
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.created_at >= now - timedelta(days=14),
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SIGNAL_STALENESS_TITLE_PREFIX}%"),
                )
                .order_by(Alert.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for alert in staleness_rows:
        signal_name = alert.title[len(_SIGNAL_STALENESS_TITLE_PREFIX) :].strip()
        regressions.append(
            {
                "signal_name": alert.title,
                "regression_class": f"signal_staleness:{signal_name or 'unknown'}",
                "detected_at": alert.created_at.isoformat()
                if alert.created_at
                else None,
                "severity": "warning",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
                "details": alert.message,
            }
        )

    # Viewport-regression cases — active RegressionRemediationCase rows whose
    # regression_class starts with "viewport_regression:". Sourced from the durable
    # table written by the pipeline_regression_dispatcher; no design_doc flags needed.
    viewport_cutoff = now - timedelta(days=30)
    viewport_rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.regression_class.like(
                        "viewport_regression:%"
                    ),
                    RegressionRemediationCase.status.in_(
                        [
                            RegressionCaseStatus.ACTIVE,
                            RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                        ]
                    ),
                    RegressionRemediationCase.first_seen_at >= viewport_cutoff,
                )
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for case in viewport_rows:
        subkey = case.regression_class.split(":", 1)[-1]
        regressions.append(
            {
                "signal_name": f"Viewport regression: {subkey}",
                "regression_class": case.regression_class,
                "detected_at": case.first_seen_at.isoformat()
                if case.first_seen_at
                else None,
                "severity": "warning",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
                "introducing_pr_number": getattr(case, "introducing_pr_number", None),
                "introducing_commit_sha": getattr(case, "introducing_commit_sha", None),
            }
        )

    # Enrich each regression with the status of any spawned investigation task.
    # Includes both pipeline_regression_dispatcher tasks (SR/fix-loop classes) and
    # crash_investigation tasks spawned by lifecycle._spawn_crash_investigations.
    if regressions:
        dispatcher_tasks = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project_id,
                        Task.context["spawned_by"]
                        .as_string()
                        .in_(("pipeline_regression_dispatcher", "crash_investigation")),
                        Task.created_at >= now - timedelta(hours=48),
                    )
                    .options(load_only(Task.id, Task.title, Task.status, Task.context))
                    .order_by(Task.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        by_class: Dict[str, Task] = {}
        for t in dispatcher_tasks:
            rc = (t.context or {}).get("regression_class")
            if rc and rc not in by_class:
                by_class[rc] = t
        for r in regressions:
            task = by_class.get(r["regression_class"])
            if task is None:
                r["spawned_task_id"] = None
                r["spawned_task_status"] = None
                r["spawned_task_title"] = None
            else:
                r["spawned_task_id"] = str(task.id)
                r["spawned_task_status"] = (
                    task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status)
                )
                r["spawned_task_title"] = task.title

    return {
        "project_id": str(project_id),
        "regressions": regressions,
        "healthy": len(regressions) == 0,
    }


@router.get("/projects/{project_id}/sr-blind-spots")
async def list_sr_blind_spots(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate blind_spots from recent self-review outcomes.

    Groups raw blind_spot strings by their first 6 tokens to surface recurring
    themes without exposing full task-specific text.  Skipped/fallback SR
    envelopes are excluded so noise from the ./done bash fallback doesn't inflate
    theme counts.
    """
    return await aggregate_sr_blind_spot_envelope(
        session,
        project_id,
        days=days,
        limit=limit,
    )


@router.get("/projects/{project_id}/prompt-fingerprint-analytics")
async def get_prompt_fingerprint_analytics(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    min_sample_size: int = Query(3, ge=1, le=50),
    top_n: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Group recent code-change task outcomes by prompt fingerprint.

    Lets operators compare prompt revisions side-by-side without bisecting
    git history. Buckets COMPLETED create/fix/refactor/migrate tasks within
    the lookback window by `Task.prompt_fingerprint` (with an `"unknown"`
    fallback for older rows that pre-date the column) and reports per-bucket
    sample size, first-attempt success rate, SR skip rate, rejection-category
    counts, delta vs. the project mean success rate, and additive SR-quality
    fields. Buckets below `min_sample_size` are excluded so a single-task
    fingerprint cannot dominate the view; SR-quality fields apply the same gate
    independently using `sr_sample_size`.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.created_at,
                Task.context,
                Task.payloads,
                Task.prompt_fingerprint,
                Task.project_id,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(2000)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    code_change_tasks: List[Task] = []
    for t in all_tasks:
        ttype = (t.first_payload or {}).get("type")
        if ttype in _SR_CODE_CHANGE_TYPES:
            code_change_tasks.append(t)
    sr_quality_by_fp = compute_sr_quality_by_fingerprint(
        code_change_tasks,
        min_sample_size=min_sample_size,
    )

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "sample_size": 0,
            "first_attempt_measured": 0,
            "first_attempt_succeeded": 0,
            "sr_skip_measured": 0,
            "sr_skipped": 0,
            "rejection_category_counts": defaultdict(int),
        }
    )

    project_first_attempt_measured = 0
    project_first_attempt_succeeded = 0
    project_sr_measured = 0
    project_sr_skipped = 0

    for t in code_change_tasks:
        fp = _task_prompt_fingerprint(t)
        ctx = t.context or {}
        bucket = buckets[fp]
        bucket["sample_size"] += 1

        if "_first_attempt_succeeded" in ctx:
            bucket["first_attempt_measured"] += 1
            project_first_attempt_measured += 1
            if ctx["_first_attempt_succeeded"]:
                bucket["first_attempt_succeeded"] += 1
                project_first_attempt_succeeded += 1

        # SR skip rate: every code-change task is eligible — the skipped
        # envelope is a sentinel written by the bash ./done fallback when
        # the agent didn't emit a structured self-review.
        bucket["sr_measured"] = bucket.get("sr_measured", 0) + 1
        project_sr_measured += 1
        sro = ctx.get("_self_review_outcome")
        if _is_skipped_sr_outcome(sro):
            bucket["sr_skipped"] += 1
            project_sr_skipped += 1

        # Rejection-category presence per task — works for both dict-shaped
        # ({"correctness": 1, ...}) and list-shaped (["correctness", ...])
        # values that already coexist in the wild.
        for cat in set(ctx.get("_first_attempt_rejection_categories") or {}):
            bucket["rejection_category_counts"][cat] += 1

    project_first_attempt_rate: Optional[float] = (
        round(project_first_attempt_succeeded / project_first_attempt_measured, 3)
        if project_first_attempt_measured
        else None
    )
    project_sr_skip_rate: Optional[float] = (
        round(project_sr_skipped / project_sr_measured, 3)
        if project_sr_measured
        else None
    )

    fingerprints_out: List[Dict[str, Any]] = []
    for fp, b in buckets.items():
        if b["sample_size"] < min_sample_size:
            continue
        first_attempt_rate: Optional[float] = (
            round(b["first_attempt_succeeded"] / b["first_attempt_measured"], 3)
            if b["first_attempt_measured"]
            else None
        )
        sr_skip_rate: Optional[float] = (
            round(b["sr_skipped"] / b["sr_measured"], 3)
            if b.get("sr_measured")
            else None
        )
        delta: Optional[float] = (
            round(first_attempt_rate - project_first_attempt_rate, 3)
            if first_attempt_rate is not None and project_first_attempt_rate is not None
            else None
        )
        fingerprints_out.append(
            {
                "fingerprint": fp,
                "sample_size": b["sample_size"],
                "first_attempt_measured": b["first_attempt_measured"],
                "first_attempt_success_rate": first_attempt_rate,
                "sr_skip_rate": sr_skip_rate,
                "rejection_category_counts": dict(b["rejection_category_counts"]),
                "delta_vs_project_mean": delta,
                **sr_quality_by_fp.get(fp, {}),
            }
        )

    fingerprints_out.sort(key=lambda x: x["sample_size"], reverse=True)
    fingerprints_out = fingerprints_out[:top_n]

    return {
        "project_id": str(project_id),
        "window_days": days,
        "sample_size": len(code_change_tasks),
        "project_first_attempt_success_rate": project_first_attempt_rate,
        "project_sr_skip_rate": project_sr_skip_rate,
        "fingerprints": fingerprints_out,
    }


@router.get("/projects/{project_id}/sr-reviewer-disagreements")
async def list_sr_reviewer_disagreements(
    project_id: uuid.UUID,
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate categories where self-review passed but the fresh-reviewer flagged an issue.

    Returns up to ``limit`` entries sorted by disagreement count descending.
    Only code-change tasks (create/fix/refactor/migrate) with both
    ``check_results`` and ``reviewer_findings`` are included.
    """
    entries = await aggregate_sr_reviewer_disagreements(
        session,
        project_id,
        days=days,
        limit=limit,
    )
    return {
        "window_days": days,
        "total_entries": len(entries),
        "disagreements": entries,
    }


@router.get("/projects/{project_id}/direction-sr-breakdown")
async def get_direction_sr_breakdown(
    project_id: uuid.UUID,
    days: int = Query(30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-direction SR catch-rate breakdown.

    Delegates to the canonical ``compute_sr_catch_rate_by_direction`` model
    (severity-aware ``_categorize_review_issues``, sr_alone/reviewer_save
    origin tracking, ``_finalize_examples`` deduped/sensitive-stripped/capped
    examples, all-iterations human-truth set). Only directions with at least
    ``_MIN_DIRECTION_SR_SAMPLE`` qualifying tasks (both SR ``check_results``
    and ``_first_attempt_rejection_categories`` present) are included.

    Per-direction shape::

        {
          "direction_id": str,
          "direction_text": str,
          "sample_size": int,
          "overall_catch_rate": float | None,
          "categories": {
            "<cat>": {"hit_rate", "miss_rate", "sample_count", "examples"?}
          },
          "worst_category": str | None
        }
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(Task.id, Task.title, Task.context, Task.payloads, Task.project_id)
        )
        .order_by(Task.created_at.desc())
        .limit(2000)
    )
    all_tasks = (await session.execute(stmt)).scalars().all()

    dir_text: Dict[str, str] = {}
    stored_by_dir: Dict[str, Any] = {}
    project = await session.get(Project, project_id)
    if project:
        for d in (project.design_doc or {}).get("directions") or []:
            did = str(d.get("id") or d.get("direction_id") or "")
            if did:
                dir_text[did] = d.get("text") or d.get("description") or ""
        stored_trends = (project.design_doc or {}).get("sr_catch_rate_trends") or {}
        stored_by_dir = stored_trends.get("by_direction") or {}

    directions_out = compute_direction_sr_breakdown_for_endpoint(
        all_tasks, dir_text, existing_by_direction=stored_by_dir
    )
    return {"directions": directions_out, "min_sample": _MIN_DIRECTION_SR_SAMPLE}


@router.get("/projects/{project_id}/sr-recent-misses")
async def get_sr_recent_misses(
    project_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return the most recent tasks where SR missed a human-caught category.

    A 'miss' is a category present in _first_attempt_rejection_categories
    that the agent's check_results either marked passed or did not record.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.created_at,
                Task.context,
                Task.payloads,
                Task.project_id,
            )
        )
        .order_by(Task.created_at.desc())
    )

    rows = []
    matched_task_ids: list[uuid.UUID] = []
    total_matched = 0
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        ctx = t.context or {}
        sr_outcome = ctx.get("_self_review_outcome")
        if not sr_outcome:
            continue
        human_cats = resolve_sr_miss_human_truth(ctx)
        if not human_cats:
            continue
        check_results = sr_outcome.get("check_results") or {}
        sr_failed = _author_sr_failed_categories(check_results)
        missed = human_cats - sr_failed
        if not missed:
            continue
        if category and category not in missed:
            continue
        total_matched += 1
        if len(rows) < limit:
            sr_check_results, reviewer_findings = _sr_miss_drilldown(sr_outcome, missed)
            fp = t.first_payload or {}
            rows.append(
                {
                    "task_id": str(t.id),
                    "title": t.title,
                    "scope": ctx.get("scope"),
                    "task_type": fp.get("type") or ctx.get("type"),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "missed_categories": sorted(missed),
                    "caught_categories": sorted(human_cats & sr_failed),
                    "sr_check_results": sr_check_results,
                    "reviewer_findings": reviewer_findings,
                    "pr_url": None,
                }
            )
            matched_task_ids.append(t.id)

    if matched_task_ids:
        art_stmt = select(Artifact.task_id, Artifact.pr_url).where(
            Artifact.task_id.in_(matched_task_ids),
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
        )
        art_rows = (await session.execute(art_stmt)).all()
        pr_map = {str(r.task_id): r.pr_url for r in art_rows}
        for row in rows:
            row["pr_url"] = pr_map.get(row["task_id"])

    return {"misses": rows, "total": total_matched}


@router.get("/projects/{project_id}/findings-by-source")
async def get_findings_by_source(
    project_id: uuid.UUID,
    ref: str = Query(
        ..., description="File path or PR ref to look up (e.g. 'backend/app/foo.py')"
    ),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return all findings (across directions) whose source_refs cited the given ref.

    Supports file paths (e.g. 'backend/app/foo.py') and PR/message refs.
    Prefix matching is applied for file paths — e.g. 'backend/app' matches
    any finding that cites a file under that path.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    findings = await lookup_findings_by_source_ref(
        session, project_id, ref, limit=limit
    )
    return {"ref": ref, "findings": findings, "total": len(findings)}


@router.get("/projects/{project_id}/directions/{direction_id}/evicted-findings")
async def get_evicted_findings(
    project_id: uuid.UUID,
    direction_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return findings that were evicted from a direction's active context.

    Evicted findings are stored in direction.history_aggregate.evicted_findings
    (FIFO, capped at 50) when the active context exceeds the 7500-char threshold.
    Each entry has: title, claims, source_refs, status, evicted_at.
    Supports offset/limit pagination over the stored list.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    directions = project.directions or []
    direction = next(
        (
            d
            for d in directions
            if str(d.get("id") or d.get("direction_id") or "") == direction_id
        ),
        None,
    )
    if direction is None:
        raise HTTPException(status_code=404, detail="Direction not found")
    agg = direction.get("history_aggregate") or {}
    all_evicted = list(agg.get("evicted_findings") or [])
    page = all_evicted[offset : offset + limit]
    return {
        "direction_id": direction_id,
        "total": len(all_evicted),
        "offset": offset,
        "limit": limit,
        "evicted_findings": page,
    }


@router.get("/regression-dispatcher/health")
async def get_regression_dispatcher_health(
    project_id: Optional[uuid.UUID] = Query(default=None),
    window_hours: int = Query(default=24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Dispatcher health and throughput summary for the last N hours.

    Returns aggregate cycle counts, per-hour spawn sparkline, and per-class
    last-spawn/cooldown info derived from the durable
    ``DispatcherCycleRecord`` table and the ``RegressionRemediationCase``
    table.  Works at all viewport widths and returns zero / empty arrays on
    a cold start (empty table) — never raises — so the UI shows 'no data'
    rather than crashing.
    """
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_mtti_p50_sec,
        get_dispatcher_cycle_records,
    )

    pid_str = str(project_id) if project_id else None
    # ``window_hours`` is bounded to 168 above; pull a large enough page that
    # the aggregates and sparkline both cover the full window.  Default tick
    # interval is 60s → 168h * 60 = ~10080 rows worst case; we cap at 11000
    # to cover the last week at 1-minute resolution without truncation.  The
    # (project_id, cycle_started_at) index makes this read cheap.
    cycles = await get_dispatcher_cycle_records(
        session,
        project_id=pid_str,
        limit=11000,
        window_hours=window_hours,
    )

    # Aggregate totals
    total_cycles = len(cycles)
    total_signals_evaluated = sum(r.get("signals_evaluated", 0) for r in cycles)
    total_signals_tripped = sum(r.get("signals_tripped", 0) for r in cycles)
    total_spawns = sum(r.get("spawns_emitted", 0) for r in cycles)
    total_cooldown = sum(
        r.get("rejections_by_reason", {}).get("cooldown", 0) for r in cycles
    )
    total_dedup = sum(r.get("rejections_by_reason", {}).get("dedup", 0) for r in cycles)
    total_cap = sum(
        r.get("rejections_by_reason", {}).get("system_cap", 0) for r in cycles
    )
    total_mi_suppressed = sum(
        r.get("rejections_by_reason", {}).get("manual_intervention_suppressed", 0)
        for r in cycles
    )

    # Compute mttd_p50_sec as mean of non-None values
    mttd_values = [
        r.get("mttd_p50_sec") for r in cycles if r.get("mttd_p50_sec") is not None
    ]
    mttd_p50_sec = sum(mttd_values) / len(mttd_values) if mttd_values else None

    # Sum recovery counts across cycles
    total_marginal_recoveries = sum(
        r.get("marginal_recoveries_count", 0) for r in cycles
    )
    total_confirmed_recoveries = sum(
        r.get("confirmed_recoveries_count", 0) for r in cycles
    )

    # Per-hour sparkline: count spawns_emitted in each 1-hour bucket
    now = datetime.utcnow()
    sparkline: List[Dict[str, Any]] = []
    for h in range(window_hours - 1, -1, -1):
        bucket_start = (now - timedelta(hours=h + 1)).isoformat()
        bucket_end = (now - timedelta(hours=h)).isoformat()
        bucket_spawns = sum(
            r.get("spawns_emitted", 0)
            for r in cycles
            if bucket_start <= r.get("cycle_started_at", "") < bucket_end
        )
        bucket_cycles = sum(
            1
            for r in cycles
            if bucket_start <= r.get("cycle_started_at", "") < bucket_end
        )
        sparkline.append(
            {
                "hour_start": bucket_start,
                "spawns": bucket_spawns,
                "cycles": bucket_cycles,
            }
        )

    # Per-class info from DB: last seen_at, status, cooldown
    where_clause = [RegressionRemediationCase.status != RegressionCaseStatus.CLOSED]
    if project_id is not None:
        where_clause.append(RegressionRemediationCase.project_id == project_id)
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where_clause)
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    from app.pipeline_regression_dispatcher import _REGRESSION_COOLDOWN  # noqa: PLC0415

    # Compute MTTI: query cases acknowledged within the window
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    mtti_where = [
        RegressionRemediationCase.operator_acknowledged_at >= cutoff,
        RegressionRemediationCase.needs_manual_intervention_at.is_not(None),
    ]
    if project_id is not None:
        mtti_where.append(RegressionRemediationCase.project_id == project_id)
    acked_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(*mtti_where).limit(1000)
            )
        )
        .scalars()
        .all()
    )
    mtti_p50_sec = compute_mtti_p50_sec(list(acked_cases))

    per_class: List[Dict[str, Any]] = []
    for case in cases:
        last_seen = case.last_seen_at
        cooldown_until: Optional[str] = None
        if last_seen:
            cooldown_end = last_seen + _REGRESSION_COOLDOWN
            if cooldown_end > now:
                cooldown_until = cooldown_end.isoformat()
        per_class.append(
            {
                "project_id": str(case.project_id),
                "regression_class": case.regression_class,
                "status": case.status,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "attempts": case.attempts,
                "in_cooldown_until": cooldown_until,
            }
        )

    return {
        "window_hours": window_hours,
        "aggregate": {
            "cycles": total_cycles,
            "signals_evaluated": total_signals_evaluated,
            "signals_tripped": total_signals_tripped,
            "spawns": total_spawns,
            "mttd_p50_sec": mttd_p50_sec,
            "mtti_p50_sec": mtti_p50_sec,
            "recoveries": {
                "marginal": total_marginal_recoveries,
                "confirmed": total_confirmed_recoveries,
            },
            "rejections": {
                "cooldown": total_cooldown,
                "dedup": total_dedup,
                "system_cap": total_cap,
                "manual_intervention_suppressed": total_mi_suppressed,
            },
        },
        "spawns_sparkline": sparkline,
        "per_class": per_class,
    }


@router.get("/anomaly-tracker/snapshot")
async def get_anomaly_tracker_snapshot(
    project_id: Optional[uuid.UUID] = Query(default=None),
    freshness_hours: int = Query(default=24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Read-only snapshot of shared_state.persistent_anomalies.

    Surfaces which fingerprints the detection pipeline is currently tracking,
    how aged they are, and when the detection sweep last ran (so a silent
    writer failure is visible). Pure passthrough of stored entries — no
    normalization, no garbage collection.
    """
    # Lazy import: shared_state mutates at runtime (in-memory cache), so we
    # always read the live module attribute rather than a cached binding.
    from app.shared_state import (  # noqa: PLC0415
        persistent_anomalies as _persistent_anomalies,
        last_anomaly_detection_run_at as _last_run,
    )

    pid_filter = str(project_id) if project_id else None
    now = _utcnow()
    stale_cutoff_iso = (now - timedelta(hours=freshness_hours)).isoformat()

    # Collect (project_id, RegressionRemediationCase) for last_dispatch_attempt_at.
    # Best-effort lookup: an active case whose regression_class is the dispatcher's
    # f"anomaly:{anomaly_key}" form gets its last_seen_at echoed; missing rows
    # return null. Filter to ACTIVE so this scales with backlog rather than
    # whole-table size on a hot endpoint.
    case_where = [RegressionRemediationCase.status == RegressionCaseStatus.ACTIVE]
    if project_id is not None:
        case_where.append(RegressionRemediationCase.project_id == project_id)
    cases = (
        (await session.execute(select(RegressionRemediationCase).where(*case_where)))
        .scalars()
        .all()
    )
    # (project_id, anomaly_key) -> latest last_seen_at across rows. The
    # dispatcher writes regression_class = f"anomaly:{anomaly_key}"
    # (pipeline_regression_dispatcher.py:1748) — strip that prefix here so the
    # index keys match the persistent_anomalies anomaly_key directly.
    case_index: Dict[tuple, datetime] = {}
    _ANOMALY_PREFIX = "anomaly:"
    for c in cases:
        if not c.last_seen_at:
            continue
        rclass = c.regression_class or ""
        if not rclass.startswith(_ANOMALY_PREFIX):
            continue
        anomaly_key = rclass[len(_ANOMALY_PREFIX) :]
        key = (str(c.project_id), anomaly_key)
        prior = case_index.get(key)
        if prior is None or c.last_seen_at > prior:
            case_index[key] = c.last_seen_at

    projects_payload: List[Dict[str, Any]] = []
    total = 0
    for proj_key, proj_anomalies in _persistent_anomalies.items():
        if pid_filter and proj_key != pid_filter:
            continue
        if not proj_anomalies:
            continue
        entries: List[Dict[str, Any]] = []
        for anomaly_key, entry in proj_anomalies.items():
            if not isinstance(entry, dict):
                continue
            first_seen = entry.get("first_seen")
            last_seen = entry.get("last_seen")
            first_seen_iso = (
                first_seen.isoformat()
                if isinstance(first_seen, datetime)
                else (first_seen or None)
            )
            last_seen_iso = (
                last_seen.isoformat()
                if isinstance(last_seen, datetime)
                else (last_seen or None)
            )
            stuck = bool(last_seen_iso and last_seen_iso < stale_cutoff_iso)
            dispatch_at = case_index.get((proj_key, anomaly_key))
            entries.append(
                {
                    "key": anomaly_key,
                    "first_seen": first_seen_iso,
                    "last_seen": last_seen_iso,
                    "occurrence_count": entry.get("occurrence_count", 0),
                    "severity": entry.get("severity"),
                    "latest_details": entry.get("latest_details"),
                    "fingerprint": entry.get("fingerprint"),
                    "affected_directions": entry.get("affected_directions"),
                    "last_dispatch_attempt_at": dispatch_at.isoformat()
                    if dispatch_at
                    else None,
                    "stuck": stuck,
                }
            )
        # Sort by last_seen desc; entries with missing last_seen sink to the bottom.
        entries.sort(key=lambda e: e.get("last_seen") or "", reverse=True)
        total += len(entries)
        projects_payload.append({"project_id": proj_key, "anomalies": entries})

    return {
        "last_detection_run_at": _last_run.isoformat() if _last_run else None,
        "freshness_window_hours": freshness_hours,
        "total": total,
        "projects": projects_payload,
    }


@router.get("/regression-dispatcher/mtta")
async def get_regression_mtta(
    project_id: Optional[uuid.UUID] = Query(default=None),
    window_days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """MTTA (Mean-Time-to-Auto-Fix) per regression class.

    Returns p50/p90 lifecycle stage durations from historical cases and live
    elapsed times for currently-active cases so operators can see 'is this
    auto-fixing on schedule or has it stalled?'
    """
    from collections import defaultdict  # noqa: PLC0415

    from app.pipeline_regression_dispatcher import _REGRESSION_RECOVERY_COOLDOWN  # noqa: PLC0415

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    where: List = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where)
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    now = datetime.utcnow()

    def _pct(vals: List[float], p: float) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        return s[min(int(len(s) * p), len(s) - 1)]

    def _parse_dt(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except (ValueError, TypeError):
            return None

    class_cases: Dict[str, List] = defaultdict(list)
    for c in cases:
        class_cases[c.regression_class].append(c)

    rows = []
    for rclass, cls_cases in class_cases.items():
        detect_s: List[float] = []
        investigate_s: List[float] = []
        inv_to_merge_s: List[float] = []
        merge_to_rec_s: List[float] = []
        active_info: List[Dict[str, Any]] = []

        for c in cls_cases:
            ttd = c.time_to_detect_sec
            if ttd is not None and ttd >= 0:
                detect_s.append(ttd)
            tti = c.time_to_investigate_sec
            if tti is not None and tti >= 0:
                investigate_s.append(float(tti))
            # inv_anchor advances to recovered_dt after each cycle so
            # re-armed cases report per-episode investigate→merge durations.
            inv_anchor: Optional[datetime] = c.investigate_spawned_at
            latest_merged_fix: Optional[datetime] = None
            for entry in c.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                merged_dt = _parse_dt(entry.get("merged_at"))
                recovered_dt = _parse_dt(entry.get("recovered_at"))
                if merged_dt and inv_anchor:
                    d = (merged_dt - inv_anchor).total_seconds()
                    if d >= 0:
                        inv_to_merge_s.append(d)
                    latest_merged_fix = merged_dt
                if merged_dt and recovered_dt:
                    d2 = (recovered_dt - merged_dt).total_seconds()
                    if d2 >= 0:
                        merge_to_rec_s.append(d2)
                if recovered_dt and latest_merged_fix is not None and not merged_dt:
                    inv_anchor = recovered_dt
                    latest_merged_fix = None

            if c.status != RegressionCaseStatus.CLOSED:
                anchor = (
                    c.signal_first_crossed_at
                    or c.signal_first_seen_at
                    or c.first_seen_at
                )
                live_elapsed = round((now - anchor).total_seconds()) if anchor else None
                live_inv = (
                    round((now - c.investigate_spawned_at).total_seconds())
                    if c.investigate_spawned_at
                    else None
                )
                rec_check_in: Optional[int] = None
                if c.status == RegressionCaseStatus.MERGED:
                    last_entry = next(
                        (
                            e
                            for e in reversed(c.fingerprint_to_fix_log or [])
                            if isinstance(e, dict) and e.get("merged_at")
                        ),
                        None,
                    )
                    if last_entry:
                        m = _parse_dt(last_entry["merged_at"])
                        if m:
                            secs = int(
                                (
                                    m + _REGRESSION_RECOVERY_COOLDOWN - now
                                ).total_seconds()
                            )
                            rec_check_in = secs if secs > 0 else None
                active_info.append(
                    {
                        "case_id": str(c.id),
                        "status": c.status,
                        "live_elapsed_sec": live_elapsed,
                        "live_elapsed_investigate_sec": live_inv,
                        "recovery_check_in_sec": rec_check_in,
                    }
                )

        rows.append(
            {
                "regression_class": rclass,
                "sample_count": len(cls_cases),
                "stages": {
                    "signal_trip_to_detect": {
                        "p50_sec": _pct(detect_s, 0.5),
                        "p90_sec": _pct(detect_s, 0.9),
                        "sample_count": len(detect_s),
                    },
                    "detect_to_investigate": {
                        "p50_sec": _pct(investigate_s, 0.5),
                        "p90_sec": _pct(investigate_s, 0.9),
                        "sample_count": len(investigate_s),
                    },
                    "investigate_to_merge": {
                        "p50_sec": _pct(inv_to_merge_s, 0.5),
                        "p90_sec": _pct(inv_to_merge_s, 0.9),
                        "sample_count": len(inv_to_merge_s),
                    },
                    "merge_to_recovery": {
                        "p50_sec": _pct(merge_to_rec_s, 0.5),
                        "p90_sec": _pct(merge_to_rec_s, 0.9),
                        "sample_count": len(merge_to_rec_s),
                    },
                },
                "active_cases": active_info,
            }
        )

    rows.sort(key=lambda r: r["sample_count"], reverse=True)
    return {
        "project_id": str(project_id) if project_id else None,
        "window_days": window_days,
        "class_count": len(rows),
        "classes": rows,
        "recovery_cooldown_sec": int(_REGRESSION_RECOVERY_COOLDOWN.total_seconds()),
    }


# ---------------------------------------------------------------------------
# Viewport Debt Endpoint
# ---------------------------------------------------------------------------

_VIEWPORT_DEBT_CACHE: Optional[
    tuple[datetime, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]
] = None
_VIEWPORT_DEBT_CACHE_TTL = 300  # 5 minutes
_VIEWPORT_DEBT_UNTESTED_CAP = 30

_ESLINT_VIEWPORT_RE = _re.compile(
    r"eslint-disable(?:-next-line)?.*viewport/(?:min-touch-target|no-fixed-width)"
)
_DATA_SMALL_TARGET_RE = _re.compile(r"data-viewport-allow-small-target")
_DATA_OVERFLOW_RE = _re.compile(r"data-viewport-allow-overflow")
_DATA_TRUNCATION_RE = _re.compile(r"data-viewport-allow-truncation")


def _find_components_dir() -> Optional[_Path]:
    here = _Path(__file__).resolve().parent
    for _ in range(6):
        candidate = here / "admin-ui" / "src" / "components"
        if candidate.is_dir():
            return candidate
        here = here.parent
    return None


def _scan_viewport_debt_rows() -> tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]
]:
    global _VIEWPORT_DEBT_CACHE
    now = datetime.utcnow()
    if _VIEWPORT_DEBT_CACHE is not None:
        cached_at, cached_rows, cached_untested, cached_summary = _VIEWPORT_DEBT_CACHE
        if (now - cached_at).total_seconds() < _VIEWPORT_DEBT_CACHE_TTL:
            return cached_rows, cached_untested, cached_summary

    components_dir = _find_components_dir()
    if components_dir is None:
        empty_summary: Dict[str, Any] = {
            "total_components": 0,
            "untested_count": 0,
            "untested_components": [],
        }
        return [], [], empty_summary

    rows = []
    untested_rows: List[Dict[str, Any]] = []
    untested_paths: List[str] = []
    total_components = 0
    for tsx_file in sorted(components_dir.glob("*.tsx")):
        name = tsx_file.name
        if ".test." in name or ".spec." in name:
            continue
        try:
            content = tsx_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        total_components += 1
        eslint_count = len(_ESLINT_VIEWPORT_RE.findall(content))
        small_target = len(_DATA_SMALL_TARGET_RE.findall(content))
        overflow = len(_DATA_OVERFLOW_RE.findall(content))
        truncation = len(_DATA_TRUNCATION_RE.findall(content))
        total = eslint_count + small_target + overflow + truncation

        stem = tsx_file.stem  # e.g. "MessagesPanel"
        vp_test = (tsx_file.parent / f"{stem}.viewport.test.tsx").exists()
        rel_path = f"admin-ui/src/components/{name}"

        if not vp_test:
            untested_paths.append(rel_path)

        if total == 0:
            if not vp_test:
                untested_rows.append(
                    {
                        "component": rel_path,
                        "eslint_disable_count": 0,
                        "data_viewport_allow_small_target": 0,
                        "data_viewport_allow_overflow": 0,
                        "data_viewport_allow_truncation": 0,
                        "total_opt_outs": 0,
                        "has_viewport_test": False,
                        "regression_case_count": 0,
                        "regression_case_breakdown": {},
                        "source_url": None,
                    }
                )
            continue

        rows.append(
            {
                "component": rel_path,
                "eslint_disable_count": eslint_count,
                "data_viewport_allow_small_target": small_target,
                "data_viewport_allow_overflow": overflow,
                "data_viewport_allow_truncation": truncation,
                "total_opt_outs": total,
                "has_viewport_test": vp_test,
                "regression_case_count": 0,
                "regression_case_breakdown": {},
                "source_url": None,
            }
        )

    rows.sort(key=lambda r: r["total_opt_outs"], reverse=True)
    untested_rows.sort(key=lambda r: r["component"])

    coverage_summary: Dict[str, Any] = {
        "total_components": total_components,
        "untested_count": len(untested_paths),
        "untested_components": sorted(untested_paths)[:50],
    }

    rows = rows[:15]
    untested_rows = untested_rows[:_VIEWPORT_DEBT_UNTESTED_CAP]
    _VIEWPORT_DEBT_CACHE = (now, rows, untested_rows, coverage_summary)
    return rows, untested_rows, coverage_summary


@router.get("/projects/{project_id}/viewport-debt")
async def get_viewport_debt(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-component viewport opt-out debt summary.

    Scans admin-ui/src/components/*.tsx for eslint-disable viewport/* lines
    and data-viewport-allow-* attributes.  Returns top-15 components sorted
    by total opt-out count, with a flag indicating whether a .viewport.test.tsx
    companion exists and a count of viewport_regression dispatcher cases in the
    last 30 days.  Result is cached in-process for 5 minutes.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    # Shallow-copy cached rows: the scan result is a process-wide cache and
    # we annotate per-project below. Without the copy, concurrent requests
    # for different projects would race on regression_case_count / source_url.
    top_rows, top_untested, coverage_summary = await asyncio.to_thread(
        _scan_viewport_debt_rows
    )
    rows = [dict(r) for r in top_rows]
    untested_rows = [dict(r) for r in top_untested]

    if rows or untested_rows:
        from app.regression_layer_classifier import (  # noqa: PLC0415
            parse_viewport_regression_class,
        )

        cutoff = datetime.utcnow() - timedelta(days=30)
        stmt = select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class.startswith(
                "viewport_regression:"
            ),
            RegressionRemediationCase.first_seen_at >= cutoff,
        )
        vp_cases = (await session.execute(stmt)).scalars().all()

        # Map component stem → case count and per-assertion-type breakdown
        case_counts: Dict[str, int] = {}
        case_breakdown: Dict[str, Dict[str, int]] = {}
        for c in vp_cases:
            parsed = parse_viewport_regression_class(c.regression_class)
            comp = parsed.get("component")
            if comp:
                # Normalize component name to .tsx extension for consistent lookup
                if not comp.endswith(".tsx"):
                    comp = f"{comp}.tsx"
                case_counts[comp] = case_counts.get(comp, 0) + 1
                atype = parsed.get("assertion_type") or "unknown"
                bd = case_breakdown.setdefault(comp, {})
                bd[atype] = bd.get(atype, 0) + 1

        project = await session.get(Project, project_id)
        gh_owner = project.github_owner if project else None
        gh_repo = project.github_repo if project else None

        for row in (*rows, *untested_rows):
            stem_tsx = _Path(row["component"]).name  # e.g. "MessagesPanel.tsx"
            row["regression_case_count"] = case_counts.get(stem_tsx, 0)
            row["regression_case_breakdown"] = case_breakdown.get(stem_tsx, {})
            if gh_owner and gh_repo:
                row["source_url"] = (
                    f"https://github.com/{gh_owner}/{gh_repo}/blob/main/{row['component']}"
                )

    return {
        "rows": rows,
        "untested_rows": untested_rows,
        "scanned_at": datetime.utcnow().isoformat(),
        "coverage_summary": coverage_summary,
    }


@router.get("/projects/{project_id}/efficiency-axes")
async def get_efficiency_axes(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return per-axis current/baseline/drift/history for the project's efficiency baseline.

    Reads ``project.design_doc["efficiency_baseline"]`` (populated weekly by
    the ``_efficiency_snapshot_service``) and returns each of the five
    foundational axes — ``cost_per_merged_pr``, ``vm_cold_start_p50_ms``,
    ``vm_reuse_rate``, ``median_api_calls_per_task``,
    ``median_review_iterations`` — with the latest snapshot value, the
    immutable baseline value, percentage drift from baseline, and a
    time-series of up to 26 weekly snapshots for trend rendering.

    Response shape::

        {
          "axes": {
            "<axis>": {
              "current": float|None,
              "baseline": float|None,
              "drift_pct": float|None,
              "history": [{"captured_at": iso8601, "value": float|None}, ...]
            },
            ...
          },
          "baseline_captured_at": iso8601|None,
          "last_snapshot_at": iso8601|None
        }

    ``drift_pct`` is None whenever either side is missing or baseline is 0.
    ``history`` is ordered oldest-first; before any snapshot fires it is ``[]``.
    Before the first snapshot fires, all axis values are None and both
    timestamps are None.
    """
    from app.efficiency_snapshot import read_baseline_with_drift  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return await read_baseline_with_drift(session, project_id)


@router.get("/regression-dispatcher/escalation-outcomes")
async def get_regression_escalation_outcomes(
    project_id: Optional[uuid.UUID] = Query(default=None),
    lookback_days: int = Query(default=30, ge=1, le=180),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate case outcomes by escalation_reason bucket.

    Groups RegressionRemediationCase rows into six buckets: four meta-escalation
    reasons (escalated_cross_layer, escalated_layer_concentration,
    escalated_cross_case_fingerprint, escalated_recurrence), one
    compliance_followup bucket for cross-layer cases where a followup fix was
    spawned and merged, and all_other.  Returns per-bucket merged_count,
    retripped_within_30d_count, durable_resolution_rate, median_hold_days,
    prevention_rate_30d, recurrences_observed, and in_flight_excluded_count.
    Cross-layer cases with a compliance followup fix are re-bucketed into
    compliance_followup so operators can compare the loop's lift against baseline.
    """
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_escalation_outcome_breakdown,
    )

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    where: List = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where)
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    return compute_escalation_outcome_breakdown(
        list(cases), lookback_days=lookback_days
    )


@router.get("/pipeline-efficiency")
async def get_pipeline_efficiency(
    project_id: Optional[uuid.UUID] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate VM cold-start latency, warm reuse rate, and review iterations.

    Returns p50/p95 of cold-start setup_secs, warm VM reuse fraction, and
    review-iteration median/p95 for the last 24h and 7d windows, each with a
    14-day-prior delta for trend comparison.
    """
    now = _utcnow()
    filters = [
        Task.created_at >= now - timedelta(days=21),
        Task.status == TaskStatus.COMPLETED,
    ]
    if project_id:
        filters.append(Task.project_id == project_id)
    tasks = (
        (
            await session.execute(
                select(Task)
                .where(*filters)
                .options(load_only(Task.id, Task.status, Task.context, Task.created_at))
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )

    def _pct(values: list, p: int):
        if not values:
            return None
        s = sorted(values)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    def _metrics(bucket: list) -> Dict[str, Any]:
        timing_list = [(t.context or {}).get("_timing") or {} for t in bucket]
        cold = [
            float(tm["setup_secs"])
            for tm in timing_list
            if isinstance(tm.get("setup_secs"), (int, float))
            and not tm.get("warm_start")
        ]
        warm_n = sum(1 for tm in timing_list if tm.get("warm_start") is True)
        timed_n = sum(1 for tm in timing_list if tm)
        iters = [
            int((t.context or {}).get("review_loop", {}).get("iteration") or 0)
            for t in bucket
            if (t.context or {}).get("review_loop")
        ]
        return {
            "setup_p50_secs": _pct(cold, 50),
            "setup_p95_secs": _pct(cold, 95),
            "setup_sample_n": len(cold),
            "warm_reuse_rate": round(warm_n / timed_n, 3) if timed_n else None,
            "warm_reuse_sample_n": timed_n,
            "review_iter_median": _pct(iters, 50),
            "review_iter_p95": _pct(iters, 95),
            "review_iter_sample_n": len(iters),
        }

    def _in(start, end):
        return [t for t in tasks if start <= (t.created_at or now) < end]

    prior_end = now - timedelta(days=14)
    cur_24h = _metrics(_in(now - timedelta(hours=24), now))
    cur_7d = _metrics(_in(now - timedelta(days=7), now))
    pri_24h = _metrics(_in(now - timedelta(hours=24) - timedelta(days=14), prior_end))
    pri_7d = _metrics(_in(now - timedelta(days=21), prior_end))

    def _delta(a, b, k):
        av, bv = a.get(k), b.get(k)
        return round(av - bv, 4) if av is not None and bv is not None else None

    def _augment(cur, pri):
        return {
            **cur,
            "vs_prior_setup_p50_delta": _delta(cur, pri, "setup_p50_secs"),
            "vs_prior_warm_reuse_rate_delta": _delta(cur, pri, "warm_reuse_rate"),
            "vs_prior_review_iter_median_delta": _delta(cur, pri, "review_iter_median"),
        }

    return {
        "computed_at": now.isoformat(),
        "window_24h": _augment(cur_24h, pri_24h),
        "window_7d": _augment(cur_7d, pri_7d),
    }


@router.get("/canonical-regression-layers")
async def get_canonical_regression_layers(
    current_user: dict = Depends(get_current_user),  # noqa: ARG001
):
    """Return the canonical regression layers with descriptions and display order."""
    from app.regression_layer_classifier import (  # noqa: PLC0415
        LAYER_DISPLAY_ORDER,
        LAYER_DESCRIPTIONS,
    )

    return {
        "layers": [
            {
                "name": layer,
                "description": LAYER_DESCRIPTIONS.get(layer, ""),
            }
            for layer in LAYER_DISPLAY_ORDER
        ]
    }


_SR_INVESTIGATION_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)
_SR_INVESTIGATION_SPAWNED_BY = "operator_sr_investigation"


class SpawnSrInvestigationRequest(BaseModel):
    category: str = _Field(..., min_length=1, max_length=100)
    current_hit_rate: float | None = None
    previous_hit_rate: float | None = None
    sample_size: int | None = None
    signals: list[str] = _Field(default_factory=list)


@router.post("/projects/{project_id}/sr-investigation")
async def spawn_sr_investigation(
    project_id: uuid.UUID,
    req: SpawnSrInvestigationRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Spawn an operator-triggered SR investigation task for a category.

    Deduplicates: if an open investigation for the same category already exists,
    returns the existing task id without creating a new one.
    Requires at least member role on the project — this spawns a billable
    agent task, consistent with request_fresh_reviewer and other task-spawning
    admin endpoints.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    dedup_stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(_SR_INVESTIGATION_ACTIVE_STATUSES),
            Task.context["spawned_by"].as_string() == _SR_INVESTIGATION_SPAWNED_BY,
            Task.context["sr_category"].as_string() == req.category,
        )
        .options(load_only(Task.id, Task.status))
        .limit(1)
    )
    existing = (await session.execute(dedup_stmt)).scalars().first()
    if existing:
        return {"task_id": str(existing.id), "created": False}

    signals_str = ", ".join(req.signals) if req.signals else "no specific signal"
    rate_info = ""
    if req.current_hit_rate is not None:
        rate_info = f" (current hit-rate: {req.current_hit_rate:.0%}"
        if req.previous_hit_rate is not None:
            rate_info += f", previous: {req.previous_hit_rate:.0%}"
        rate_info += f", n={req.sample_size or '?'})"

    prompt = (
        f"Operator-requested SR investigation for category '{req.category}'{rate_info}.\n"
        f"Signals that tripped: {signals_str}.\n\n"
        f"Investigate why the self-review catch-rate for this category has regressed. "
        f"Identify root causes (e.g. prompt gaps, task patterns, reviewer hallucinations) "
        f"and propose concrete fixes."
    )

    new_task = Task(
        project_id=project_id,
        title=f"Investigate SR catch-rate: {req.category}"[:200],
        description=f"Operator-triggered SR investigation for category '{req.category}'"[
            :500
        ],
        payloads=[
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": _runtime_config.get("integration_branch", "main"),
                "effort": "low",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
        context={
            "spawned_by": _SR_INVESTIGATION_SPAWNED_BY,
            "sr_category": req.category,
            "signals": req.signals,
            "current_hit_rate": req.current_hit_rate,
            "previous_hit_rate": req.previous_hit_rate,
            "sample_size": req.sample_size,
        },
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    return {"task_id": str(new_task.id), "created": True}


@router.get("/projects/{project_id}/sr-investigation-task-ids")
async def get_sr_investigation_task_ids(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a map of category → task_id for open operator-spawned SR investigations.

    Used by the UI to deduplicate investigation spawning and surface existing task ids
    inline on category cards.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(_SR_INVESTIGATION_ACTIVE_STATUSES),
            Task.context["spawned_by"].as_string() == _SR_INVESTIGATION_SPAWNED_BY,
        )
        .options(load_only(Task.id, Task.context))
    )
    tasks = (await session.execute(stmt)).scalars().all()

    task_ids: Dict[str, str] = {}
    for t in tasks:
        cat = (t.context or {}).get("sr_category")
        if cat:
            task_ids[str(cat)] = str(t.id)

    return {"task_ids": task_ids}


# ---------------------------------------------------------------------------
# /projects/{project_id}/version-lifecycle
# ---------------------------------------------------------------------------


def _format_qualification(qual: Dict[str, Any]) -> Dict[str, Any]:
    """Extract operator-facing fields from a last_qualification record."""
    return {
        "status": qual.get("status"),
        "confidence": qual.get("confidence"),
        "threshold": qual.get("threshold"),
        "sub_scores": qual.get("sub_scores"),
        "blocker": qual.get("blocker"),
        "finalized_at": qual.get("finalized_at"),
    }


@router.get("/projects/{project_id}/version-lifecycle")
async def get_version_lifecycle(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the release version lifecycle state for a project.

    Exposes the current open version (phase, qualification verdicts, merged-task
    count) and a short history of recent closed versions for calibration.
    Projects with no version lifecycle return ``null`` for ``current_version``
    and an empty ``recent_versions`` list.
    """
    from app.release_version import count_merged_since  # noqa: PLC0415
    from app.release_qualifier import compute_promotion_readiness  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    open_row = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    current_version_out: Optional[Dict[str, Any]] = None
    if open_row is not None:
        merged_count = await count_merged_since(session, project_id, open_row.opened_at)
        qual = open_row.last_qualification or {}
        current_version_out = {
            "version_number": open_row.version_number,
            "phase": open_row.phase,
            "phase_changed_at": open_row.phase_changed_at.isoformat()
            if open_row.phase_changed_at
            else None,
            "phase_reasoning": open_row.phase_reasoning,
            "opened_at": open_row.opened_at.isoformat(),
            "qualification_attempts": open_row.qualification_attempts,
            "merged_task_count": merged_count,
            "last_qualification": _format_qualification(qual) if qual else None,
        }

    history_rows = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status != "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )

    promotion_readiness = await compute_promotion_readiness(session, project_id)

    return {
        "current_version": current_version_out,
        "recent_versions": [
            {
                "version_number": v.version_number,
                "status": v.status,
                "opened_at": v.opened_at.isoformat(),
                "promoted_at": v.promoted_at.isoformat() if v.promoted_at else None,
                "qualification_attempts": v.qualification_attempts,
                "last_qualification": _format_qualification(v.last_qualification)
                if v.last_qualification
                else None,
            }
            for v in history_rows
        ],
        "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
        "promotion_readiness": promotion_readiness,
    }


class _SetVersionPhaseBody(BaseModel):
    phase: str = _Field(..., pattern=r"^(build|stabilize)$")
    reasoning: str = _Field(default="", max_length=500)


@router.post("/projects/{project_id}/version-phase")
async def set_version_phase(
    project_id: uuid.UUID,
    payload: _SetVersionPhaseBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set the phase of the open release version for a project.

    Member+ role required.  Returns 404 when there is no open version.
    Combined with POST /qualify this gives the operator a direct lever to
    advance a version to stabilize and trigger the release gate end-to-end,
    producing a dry-run QualificationVerdict without waiting for the strategic
    agent's conservative stall threshold (~14 days).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    open_ver = await _release_version.current_open_version(session, project_id)
    if open_ver is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    operator_id = current_user.get("email") or current_user["user_id"]
    full_reasoning = (
        f"[operator:{operator_id}] {payload.reasoning}".strip()
        if payload.reasoning
        else f"[operator:{operator_id}]"
    )
    _release_version.set_version_phase(
        open_ver, payload.phase, reasoning=full_reasoning
    )
    session.add(open_ver)
    await session.commit()
    await session.refresh(open_ver)
    return {
        "version_number": open_ver.version_number,
        "phase": open_ver.phase,
        "phase_changed_at": open_ver.phase_changed_at.isoformat()
        if open_ver.phase_changed_at
        else None,
        "phase_reasoning": open_ver.phase_reasoning,
    }


_VERDICT_HISTORY_LIMIT = 50


@router.get("/projects/{project_id}/qualification-verdicts")
async def list_qualification_verdicts(
    project_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=_VERDICT_HISTORY_LIMIT),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return recent QualificationVerdict rows for a project, newest-first.

    Viewer+ role required.  Returns a bounded list (max 50) of per-verdict
    history rows covering both dry-run and promotion-enabled regimes, giving
    operators a live view of qualifier behavior across the full history.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    stmt = (
        select(QualificationVerdict)
        .where(QualificationVerdict.project_id == project_id)
        .order_by(QualificationVerdict.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    return {
        "verdicts": [
            {
                "id": str(v.id),
                "release_version_id": str(v.release_version_id),
                "candidate_sha": v.candidate_sha,
                "confidence": v.confidence,
                "sub_scores": v.sub_scores,
                "blocker": v.blocker,
                "would_promote": v.would_promote,
                "promotion_enabled": v.promotion_enabled,
                "terminal_status": v.terminal_status,
                "threshold": v.threshold,
                "started_at": v.started_at.isoformat() if v.started_at else None,
                "finalized_at": v.finalized_at.isoformat() if v.finalized_at else None,
                "created_at": v.created_at.isoformat(),
            }
            for v in rows
        ],
        "total": len(rows),
    }
