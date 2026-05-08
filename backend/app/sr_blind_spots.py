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


def stamp_sr_blind_spot_computed_at(
    doc: dict,
    now: datetime | None = None,
) -> None:
    """Write the current UTC timestamp into *doc* under the sr_blind_spot
    signal_last_computed_at key consumed by the pipeline staleness watchdog.

    Centralising the write here keeps the design-doc key name canonical and
    lets callers avoid hardcoding it.  Pass *now* in tests to fix the clock.
    """
    ts = (now or datetime.utcnow()).isoformat()
    doc["sr_blind_spot_signal_last_computed_at"] = ts


def is_skipped_sr_outcome(sro: Dict[str, Any] | None) -> bool:
    """A self-review outcome is a 'skip' when the agent emitted the
    bash ./done fallback envelope: verdict=='unknown' with empty
    check_results, or a blind_spots marker matching the fallback paths.
    """
    if not sro:
        return False
    blind_spots = sro.get("blind_spots") or []
    for entry in blind_spots:
        if isinstance(entry, str) and any(
            m in entry for m in _SR_SKIP_BLIND_SPOT_MARKERS
        ):
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
        for entry in sro.get("blind_spots") or []:
            if not isinstance(entry, str) or not entry.strip():
                continue
            theme = " ".join(entry.lower().split()[:6])
            theme_counts[theme] += 1
    return theme_counts


async def compute_sr_blind_spot_themes(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int = 14,
    now: datetime | None = None,
) -> None:
    """Background variant of blind-spot aggregation that stamps the design-doc
    staleness key consumed by the pipeline-regression dispatcher's
    telemetry-staleness watchdog.

    Call on task completion alongside compute_sr_catch_rate_trends so the
    dispatcher can detect when this job stops running and spawn an
    investigation automatically.
    """
    from app.models import Project
    from sqlalchemy.orm.attributes import flag_modified

    project = await session.get(Project, project_id)
    if project is None:
        return
    doc = dict(project.design_doc or {})
    stamp_sr_blind_spot_computed_at(doc, now=now)
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)


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
        precision = (
            hit_count / (hit_count + fp_count) if (hit_count + fp_count) > 0 else None
        )
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

_EXECUTION_EVIDENCE_CATEGORIES = (
    "tests_executed_cleanly",
    "lint_executed_cleanly",
    "typecheck_executed_cleanly",
)


async def aggregate_sr_execution_evidence_miss_rates(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate miss-rates for execution-evidence SR categories from the DB.

    For each task's ``_self_review_outcome.check_results``, counts presence
    and failures for tests_executed_cleanly, lint_executed_cleanly, and
    typecheck_executed_cleanly. Returns miss_rate, miss_count, sample_count
    per category; omits categories with zero samples. Skipped/fallback SR
    envelopes are excluded.
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

    totals: Dict[str, Dict[str, int]] = {}
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        for cat in _EXECUTION_EVIDENCE_CATEGORIES:
            entry = check_results.get(cat)
            if entry is None:
                continue
            if cat not in totals:
                totals[cat] = {"sample_count": 0, "miss_count": 0}
            totals[cat]["sample_count"] += 1
            if not entry.get("passed", True):
                totals[cat]["miss_count"] += 1

    out: Dict[str, Dict[str, Any]] = {}
    for cat, s in totals.items():
        if s["sample_count"] == 0:
            continue
        out[cat] = {
            "miss_rate": round(s["miss_count"] / s["sample_count"], 3),
            "miss_count": s["miss_count"],
            "sample_count": s["sample_count"],
        }
    return out


# Mirror playbook_learning._MIN_SR_SAMPLE — update both if the threshold changes.
_SR_MIN_SAMPLE = 5


def has_test_loop_signal(task) -> bool:
    """Return True when the task's test fix-loop ran non-trivially.

    'Non-trivial' means ≥2 test attempts, or a single attempt whose
    exit_reason is not 'passed' (divergence, max-attempts, etc.).
    """
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


def aggregate_sr_regression_risk_test_signal(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
) -> Dict[str, Any] | None:
    """Synchronous aggregation of regression_risk vs test-signal for a task list.

    Classifies each qualifying code-change task (non-skip SR outcome with
    ``check_results.regression_risk.passed`` present) into one of four buckets:

    - ``sr_passed_no_signal``   — true-pass: SR ok, test loop quiet
    - ``sr_passed_with_signal`` — miss-by-test-signal: SR ok, test loop ran
    - ``sr_failed_with_signal`` — hit-by-test-signal: SR flagged, test loop ran
    - ``sr_failed_no_signal``   — false-flag: SR flagged, test loop quiet

    Returns ``None`` when fewer than *min_sample* tasks qualify.
    """
    qualifying = []
    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        rr = (sro.get("check_results") or {}).get("regression_risk")
        if not isinstance(rr, dict) or "passed" not in rr:
            continue
        qualifying.append(t)

    if len(qualifying) < min_sample:
        return None

    stats: Dict[str, int] = {
        "sr_passed_no_signal": 0,
        "sr_passed_with_signal": 0,
        "sr_failed_with_signal": 0,
        "sr_failed_no_signal": 0,
    }
    for t in qualifying:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        rr = (sro.get("check_results") or {}).get("regression_risk") or {}
        sr_passed = bool(rr.get("passed"))
        signal = has_test_loop_signal(t)
        if sr_passed and signal:
            stats["sr_passed_with_signal"] += 1
        elif sr_passed:
            stats["sr_passed_no_signal"] += 1
        elif signal:
            stats["sr_failed_with_signal"] += 1
        else:
            stats["sr_failed_no_signal"] += 1

    numer = stats["sr_passed_with_signal"]
    denom = stats["sr_passed_no_signal"] + stats["sr_passed_with_signal"]
    rate = round(numer / denom, 3) if denom > 0 else None
    return {
        **stats,
        "miss_by_test_signal_rate": rate,
        "numerator": numer,
        "denominator": denom,
        "sample_size": len(qualifying),
    }


def aggregate_sr_scope_drift_vs_expansion(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
) -> Dict[str, Any] | None:
    """Cross-validate SR scope-drift verdict against the objective expansion_ratio signal.

    Qualifying tasks: code-change type, non-skip SR outcome with a
    ``check_results["scope-drift"]`` entry, and a pre-computed
    ``expansion_ratio`` in task.context (set by the agent status router when
    changed_files and related_files are both present on PR submission).

    Four buckets:

    - ``consistent``        — SR passed + ratio ≤ 1.5 (true-pass, not a miss)
    - ``miss_by_expansion`` — SR passed + ratio ≥ 2.0 (SR missed scope drift)
    - ``hit_by_expansion``  — SR failed scope-drift + ratio ≥ 1.5 (SR caught it)
    - ``fp_candidate``      — SR failed scope-drift + ratio ≤ 1.0 (likely false positive)
    - ``gray_zone``         — SR passed + 1.5 < ratio < 2.0 (ambiguous; not counted in rate)

    Returns ``None`` when fewer than *min_sample* tasks qualify.
    """
    qualifying = []
    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        sd = (sro.get("check_results") or {}).get("scope-drift")
        if not isinstance(sd, dict) or "passed" not in sd:
            continue
        if ctx.get("expansion_ratio") is None:
            continue
        qualifying.append(t)

    if len(qualifying) < min_sample:
        return None

    stats: Dict[str, int] = {
        "consistent": 0,
        "miss_by_expansion": 0,
        "hit_by_expansion": 0,
        "fp_candidate": 0,
        "gray_zone": 0,
    }
    for t in qualifying:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        sd = (sro.get("check_results") or {}).get("scope-drift") or {}
        sr_passed = bool(sd.get("passed"))
        ratio = max(float((t.context or {}).get("expansion_ratio") or 1.0), 1.0)
        if sr_passed:
            if ratio <= 1.5:
                stats["consistent"] += 1
            elif ratio >= 2.0:
                stats["miss_by_expansion"] += 1
            else:
                stats["gray_zone"] += 1
        else:
            if ratio >= 1.5:
                stats["hit_by_expansion"] += 1
            elif ratio <= 1.0:
                stats["fp_candidate"] += 1

    numer = stats["miss_by_expansion"]
    denom = stats["consistent"] + stats["miss_by_expansion"]
    rate = round(numer / denom, 3) if denom > 0 else None
    return {
        **stats,
        "miss_by_expansion_rate": rate,
        "numerator": numer,
        "denominator": denom,
        "sample_size": len(qualifying),
    }


def _count_reviewer_disagreement_directions(tasks) -> Dict[tuple, Dict[str, int]]:
    counts: Dict[tuple, Dict[str, int]] = defaultdict(
        lambda: {"miss": 0, "tp": 0, "fp": 0}
    )
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        reviewer_findings = sro.get("reviewer_findings") or []
        check_results = sro.get("check_results") or {}
        reviewer_cats = {
            finding.get("category")
            for finding in reviewer_findings
            if isinstance(finding, dict) and finding.get("category")
        }
        sr_failed = {
            cat
            for cat, result in check_results.items()
            if not result.get("passed", True)
        }
        for cat in reviewer_cats:
            cr = check_results.get(cat)
            if cr is None or cr.get("passed", True):
                counts[(cat, task_type)]["miss"] += 1
        for cat in sr_failed:
            bucket = counts[(cat, task_type)]
            if cat in reviewer_cats:
                bucket["tp"] += 1
            else:
                bucket["fp"] += 1
    return counts


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
    counts = _count_reviewer_disagreement_directions(tasks)
    return {key: bucket["miss"] for key, bucket in counts.items() if bucket["miss"] > 0}


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

    direction_counts = _count_reviewer_disagreement_directions(all_tasks)
    top = [
        item
        for item in sorted(direction_counts.items(), key=lambda x: -x[1]["miss"])
        if item[1]["miss"] > 0
    ][:limit]
    return [
        {
            "category": cat,
            "task_type": ttype,
            "count": bucket["miss"],
            "false_positive_count": bucket["fp"],
            "true_positive_count": bucket["tp"],
            "false_positive_rate": round(
                bucket["fp"] / (bucket["fp"] + bucket["tp"]), 3
            )
            if (bucket["fp"] + bucket["tp"]) > 0
            else None,
        }
        for (cat, ttype), bucket in top
    ]
