"""Shared aggregator for project-wide self-review blind-spot themes.

Both ``GET /api/admin/projects/{project_id}/sr-blind-spots`` and the agent
task-fetch project_context injection consume the same aggregation, so the
SQL+normalization logic lives here to avoid drift between them.

NOTE: there is no cached aggregate today (unlike ``sr_catch_rate_trends``
which is precomputed into ``project.design_doc``). Callers scan up to
2000 recent COMPLETED tasks per request — acceptable for the digest
endpoint and the per-task agent fetch in current volumes, but a future
optimization could mirror the ``sr_catch_rate_trends`` cache pattern in
``playbook_learning.compute_sr_catch_rate_trends``.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Task, TaskStatus

_SR_SKIP_BLIND_SPOT_MARKERS = ("self-review skipped", "self-review JSON parsing failed")

_TASK_SCAN_CAP = 2000


def is_skipped_sr_outcome(sro: Dict[str, Any] | None) -> bool:
    """A self-review outcome is a 'skip' when the agent emitted the
    bash ./done fallback envelope: verdict=='unknown' with empty
    check_results, or a blind_spots marker matching the fallback paths.
    """
    if not sro:
        return False
    blind_spots = sro.get("blind_spots") or []
    for entry in blind_spots:
        if isinstance(entry, str) and any(m in entry for m in _SR_SKIP_BLIND_SPOT_MARKERS):
            return True
    if sro.get("verdict") == "unknown" and not (sro.get("check_results") or {}):
        return True
    return False


async def _scan_blind_spot_counts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    theme_counts: Dict[str, int] = defaultdict(int)
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        for entry in (sro.get("blind_spots") or []):
            if not isinstance(entry, str) or not entry.strip():
                continue
            theme = " ".join(entry.lower().split()[:6])
            theme_counts[theme] += 1
    return theme_counts


async def aggregate_sr_blind_spot_themes(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Return up to ``limit`` recurring blind-spot themes for the project.

    Each blind_spot string is normalized to its first 6 lowercase tokens;
    skipped/fallback SR envelopes are excluded so noise from the bash
    ./done fallback path doesn't inflate counts. Result is ``[{"theme":
    str, "count": int}, ...]`` sorted by count desc.
    """
    theme_counts = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_counts.items(), key=lambda x: -x[1])[:limit]
    return [{"theme": t, "count": c} for t, c in top]


async def _scan_fp_stats(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, int]]:
    """Return per-category {sr_failed, fp_count, hit_count} counts.

    A false positive: SR marks category C as FAILED but no human review
    iteration ever confirmed C (checked via _all_iterations_rejection_categories
    falling back to _first_attempt_rejection_categories).
    A hit: SR marks C as FAILED and a human confirmed C in any iteration.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    cat_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"sr_failed": 0, "fp_count": 0, "hit_count": 0}
    )
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        sr_failed = {c for c, r in check_results.items() if not r.get("passed", True)}
        if not sr_failed:
            continue
        all_human = set(ctx.get("_all_iterations_rejection_categories") or {})
        if not all_human:
            all_human = set(ctx.get("_first_attempt_rejection_categories") or {})
        for cat in sr_failed:
            cat_stats[cat]["sr_failed"] += 1
            if cat in all_human:
                cat_stats[cat]["hit_count"] += 1
            else:
                cat_stats[cat]["fp_count"] += 1
    return cat_stats


async def aggregate_sr_fp_rates(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, Any]]:
    """Per-category false-positive rates for self-review check_results.

    For each category where SR emitted at least one FAILED verdict:
    - ``false_positive_rate``: fraction of SR-fails with no human confirmation
    - ``hit_rate``: fraction of SR-fails confirmed by a human reviewer
    - ``precision``: hit_count / (hit_count + fp_count); None when both are 0

    Skipped/fallback SR envelopes are excluded. Categories with zero
    SR-failed tasks are omitted.
    """
    cat_stats = await _scan_fp_stats(session, project_id, days=days)
    result: Dict[str, Dict[str, Any]] = {}
    for cat, stats in cat_stats.items():
        sr_failed = stats["sr_failed"]
        if sr_failed == 0:
            continue
        fp_count = stats["fp_count"]
        hit_count = stats["hit_count"]
        precision = hit_count / (hit_count + fp_count) if (hit_count + fp_count) > 0 else None
        result[cat] = {
            "sr_failed_count": sr_failed,
            "fp_count": fp_count,
            "hit_count": hit_count,
            "false_positive_rate": round(fp_count / sr_failed, 4),
            "hit_rate": round(hit_count / sr_failed, 4),
            "precision": round(precision, 4) if precision is not None else None,
        }
    return result


async def aggregate_sr_blind_spot_envelope(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> Dict[str, Any]:
    """Same data as :func:`aggregate_sr_blind_spot_themes` but wrapped in
    the response envelope the digest endpoint exposes.

    Kept separate so the agent injection can use the bare list while the
    HTTP endpoint preserves its ``window_days`` / ``total_distinct_themes``
    contract.
    """
    theme_counts = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_counts.items(), key=lambda x: -x[1])[:limit]
    return {
        "window_days": days,
        "total_distinct_themes": len(theme_counts),
        "themes": [{"theme": t, "count": c} for t, c in top],
    }


_SR_CODE_CHANGE_TYPES = frozenset({"create", "fix", "refactor", "migrate"})


def count_reviewer_disagreements_in_tasks(tasks) -> Dict[tuple, int]:
    """Single source of truth for the (category, task_type) -> count tally.

    The caller is responsible for filtering tasks by status and by created_at
    cutoff (each call site has slightly different fetch semantics — DB vs.
    pre-fetched playbook list — but the disagreement-counting rule itself
    must not drift between them).

    A *disagreement* is counted for every reviewer_finding category that
    self-review either omitted or marked ``passed: true``. Skipped/fallback
    self-review envelopes are excluded so the bash ./done fallback does not
    inflate counts.
    """
    counts: Dict[tuple, int] = defaultdict(int)
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        reviewer_findings = sro.get("reviewer_findings") or []
        if not reviewer_findings:
            continue
        check_results = sro.get("check_results") or {}
        for finding in reviewer_findings:
            if not isinstance(finding, dict):
                continue
            cat = finding.get("category")
            if not cat:
                continue
            cr = check_results.get(cat)
            if cr is None or cr.get("passed", True):
                counts[(cat, task_type)] += 1
    return counts


async def aggregate_sr_reviewer_disagreements(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Aggregate categories where self-review passed but the fresh-reviewer flagged an issue.

    For each completed code-change task that has both ``check_results`` and
    ``reviewer_findings``, a *disagreement* is recorded for every category
    where the fresh-reviewer raised a finding but self-review either omitted
    the category or marked it ``passed: true``.

    Returns up to ``limit`` entries sorted by count descending:
      [{"category": str, "task_type": str, "count": int}, ...]
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    disagreement_counts = count_reviewer_disagreements_in_tasks(all_tasks)
    top = sorted(disagreement_counts.items(), key=lambda x: -x[1])[:limit]
    return [{"category": cat, "task_type": ttype, "count": cnt} for (cat, ttype), cnt in top]
