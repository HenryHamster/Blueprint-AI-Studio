"""Self-review diagnostics and retrospective endpoints.

Extracted from app.routers.admin_digest — these endpoints form a cohesive
self-review analytics surface.  The heavy lifting lives in app.sr_analytics,
app.sr_blind_spots, app.research_completion, and app.research_actionability;
this module is a thin HTTP wrapper around those helpers.

Mounted onto the admin_digest router via router.include_router so all paths
remain under /api/admin without change.
"""

import uuid
from collections import defaultdict
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field as _Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task,
    TaskStatus,
    Project,
    ProjectSRAnalytics,
    Artifact,
    ArtifactType,
    _utcnow,
    StrategicCycle,
)
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role
from app.shared_state import get_project_integration_branch
from app.sr_review_quality_metrics import (
    _SR_CODE_CHANGE_TYPES,
    _author_sr_failed_categories,
    _sr_miss_drilldown,
    _task_prompt_fingerprint,
)
from app.sr_analytics import (
    _MIN_DIRECTION_SR_SAMPLE,
    compute_direction_sr_breakdown_for_endpoint,
    compute_sr_quality_by_fingerprint,
)
from app.sr_blind_spots import (
    aggregate_sr_blind_spot_envelope,
    aggregate_sr_reviewer_disagreements_envelope,
    is_disallowed_blind_spot,
    is_skipped_sr_outcome as _is_skipped_sr_outcome,
    _REVIEWER_ORIGINS,
    reviewer_confirmed_categories,
    safe_check_results,
)
from app.research_completion import lookup_findings_by_source_ref
from app.research_actionability import (
    _MAX_ACTIONABILITY_CYCLES,
    _aggregate_actionability_extended,
    _compute_actionability_rates_extended,
)

router = APIRouter()

# Reviewer-finding severity scale (CRITICAL > HIGH > MEDIUM > LOW), mirroring
# sr_blind_spots._SEVERITY_LEVELS. MAJOR/MINOR are normalized to MEDIUM/LOW,
# consistent with the frontend `reviewerSeverityColor` synonym mapping.
_SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _normalize_severity(raw: Optional[str]) -> str:
    s = (raw or "").strip().upper()
    if s == "MAJOR":
        return "MEDIUM"
    if s == "MINOR":
        return "LOW"
    return s


def _missed_finding_meets_severity(
    reviewer_findings: Any, missed: set, threshold_rank: int
) -> bool:
    """True if any reviewer finding for a MISSED category is at/above threshold."""
    if not isinstance(reviewer_findings, list):
        return False
    for f in reviewer_findings:
        if not isinstance(f, dict) or f.get("category") not in missed:
            continue
        if _SEVERITY_RANK.get(_normalize_severity(f.get("severity")), 0) >= threshold_rank:
            return True
    return False


_SR_INVESTIGATION_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    # PARKED is a live, dedup-relevant state: a parked investigation is still
    # open work, so the same-category dedup lookup must see it or a duplicate
    # investigation could spawn while the parked one waits on its wake.
    TaskStatus.PARKED,
)
_SR_INVESTIGATION_SPAWNED_BY = "operator_sr_investigation"


def _normalize_blind_spot_theme(entry: str) -> str:
    return " ".join(entry.lower().split()[:6])


def _normalize_case_pattern_text(value: str) -> str:
    return " ".join(value.lower().split())


def _edge_matches_case_pattern(edge: Dict[str, Any], case_pattern: str | None) -> bool:
    if not case_pattern:
        return True
    normalized_pattern = _normalize_case_pattern_text(case_pattern)
    if not normalized_pattern:
        return True
    label = _edge_case_label(edge)
    return label == normalized_pattern


def _edge_case_label(edge: Dict[str, Any]) -> str | None:
    case_value = edge.get("case")
    if isinstance(case_value, str) and case_value.strip():
        return " ".join(case_value.lower().split()[:6])

    blind_spot = edge.get("blind_spot")
    if isinstance(blind_spot, str) and blind_spot.strip():
        if is_disallowed_blind_spot(blind_spot):
            return None
        return _normalize_blind_spot_theme(blind_spot)
    return None


def _edge_blind_spot_theme(edge: Dict[str, Any]) -> str | None:
    blind_spot = edge.get("blind_spot")
    if not isinstance(blind_spot, str) or not blind_spot.strip():
        return None
    if is_disallowed_blind_spot(blind_spot):
        return None
    return _normalize_blind_spot_theme(blind_spot)


class SpawnSrInvestigationRequest(BaseModel):
    category: str = _Field(..., min_length=1, max_length=100)
    current_hit_rate: float | None = None
    previous_hit_rate: float | None = None
    sample_size: int | None = None
    signals: list[str] = _Field(default_factory=list)


@router.get("/projects/{project_id}/sr-missed-retrospectives")
async def list_sr_missed_retrospectives(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    category: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    scope: Optional[str] = Query(None, max_length=64),
    min_severity: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List code-change tasks where self-review passed a category that human review later flagged.

    Useful for operator-driven learning: surface systematic blind spots so prompt
    or check changes can target them.
    """
    cutoff = _utcnow() - timedelta(days=days)
    page_start = page * page_size
    page_end = page_start + page_size
    # 0 disables the severity filter (absent param or unrecognized value).
    min_severity_rank = (
        _SEVERITY_RANK.get(_normalize_severity(min_severity), 0) if min_severity else 0
    )

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
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
    )

    page_items = []
    total_matched = 0
    all_task_types: set[str] = set()
    category_counts: Dict[str, int] = defaultdict(int)
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        ctx = t.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        check_results = safe_check_results(sr_outcome)
        if not check_results:
            continue
        reviewer_confirmed_cats = reviewer_confirmed_categories(ctx)
        if not reviewer_confirmed_cats:
            continue
        sr_failed = _author_sr_failed_categories(check_results)
        missed = reviewer_confirmed_cats - sr_failed
        if not missed:
            continue
        t_type = (t.first_payload or {}).get("type")
        # The `scope` filter narrows the full population (items, total, AND
        # category_counts) — unlike `category`, which narrows only items /
        # total / pagination below. Applying it before the category_counts
        # accumulation keeps the chip counts honest to the scope-filtered subset.
        if scope and ctx.get("scope") != scope:
            continue
        # Severity filter: keep only tasks with a reviewer finding for a MISSED
        # category at or above the chosen severity. Applied here — alongside
        # category/scope — so total/category_counts/pagination stay honest.
        if min_severity_rank and not _missed_finding_meets_severity(
            sr_outcome.get("reviewer_findings"), missed, min_severity_rank
        ):
            continue
        # Accumulate task types before the task_type filter so the full type
        # list is available in the response regardless of which filter is active.
        if t_type:
            all_task_types.add(t_type)
        if task_type and t_type != task_type:
            continue

        # Accumulate category_counts for the full missed-retrospective
        # population within the (project, days, task_type) window — independent
        # of the `category` filter. This keeps the chip counts for other
        # categories honest when a category chip is active, so they show the
        # same numbers an operator would see with the filter cleared.
        for cat in missed:
            category_counts[cat] += 1

        # The `category` filter narrows only items / total / pagination, not
        # the chip counts accumulated above.
        if category and category not in missed:
            continue

        current_index = total_matched
        total_matched += 1
        if current_index < page_start or current_index >= page_end:
            continue

        caught = reviewer_confirmed_cats & sr_failed
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
                "sr_check_results": m["sr_check_results"],
                "reviewer_findings": m["reviewer_findings"],
                "pr_url": pr_map.get(m["task_id"]),
            }
        )

    return {"items": items, "total": total_matched, "task_types": sorted(all_task_types), "category_counts": dict(category_counts)}


@router.get("/projects/{project_id}/sr-unhandled-edges-retrospectives")
async def list_sr_unhandled_edges_retrospectives(
    project_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    task_type: Optional[str] = Query(None),
    blind_spot: Optional[str] = Query(None),
    case_pattern: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List code-change tasks with unhandled edge cases flagged by the agent.

    Surfaces which specific recent tasks shipped with unhandled-edge admissions
    (handled: false in edges_considered), enabling audit of whether these
    correlate with later reviewer rejections and spot patterns.

    ``blind_spot_counts`` aggregates recurring blind-spot themes across the
    (days + task_type + case_pattern) filtered universe — counted once per
    unique theme per task, ignoring the ``blind_spot`` filter so the pills
    remain swap-able. Themes use the same first-6-token-lowercase
    normalisation as ``sr_blind_spots._scan_blind_spot_counts``.

    ``edge_case_counts`` frequency-ranks the edge ``case`` labels across the
    (days + task_type + blind_spot) filtered universe — counted once per
    unique label per task, ignoring the ``case_pattern`` filter so the badges
    remain swap-able while staying stable across pagination.
    """
    cutoff = _utcnow() - timedelta(days=days)
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
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
    )

    page_items = []
    total_matched = 0
    all_task_types: set[str] = set()
    blind_spot_counts: Dict[str, int] = defaultdict(int)
    edge_case_counts: Dict[str, int] = defaultdict(int)
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

        matched_case_pattern_edges = [
            edge
            for edge in unhandled_edges
            if _edge_matches_case_pattern(edge, case_pattern)
        ]
        matched_blind_spot_edges = unhandled_edges
        if blind_spot:
            matched_blind_spot_edges = [
                edge
                for edge in unhandled_edges
                if _edge_blind_spot_theme(edge) == blind_spot
            ]

        t_type = (t.first_payload or {}).get("type")
        # Accumulate task types after case-pattern matching so the response can
        # still expose the full type menu even when task_type narrows rows.
        if matched_case_pattern_edges and t_type:
            all_task_types.add(t_type)
        if task_type and t_type != task_type:
            continue

        # Tokenize each unhandled edge's blind_spot into a 6-token theme.
        # Dedupe themes within a single task so one task contributes at most
        # one bump per theme to blind_spot_counts.
        task_themes: set[str] = set()
        for edge in matched_case_pattern_edges:
            theme = _edge_blind_spot_theme(edge)
            if theme:
                task_themes.add(theme)

        # Aggregate blind_spot_counts over the days+task_type+case_pattern
        # universe BEFORE applying the theme filter so operators can swap
        # pills meaningfully.
        for theme in task_themes:
            blind_spot_counts[theme] += 1

        task_edge_case_labels = {
            label for edge in matched_blind_spot_edges if (label := _edge_case_label(edge))
        }
        for label in task_edge_case_labels:
            edge_case_counts[label] += 1

        if not matched_case_pattern_edges:
            continue

        matched_edges = matched_case_pattern_edges
        if blind_spot:
            matched_edges = [
                edge
                for edge in matched_case_pattern_edges
                if _edge_blind_spot_theme(edge) == blind_spot
            ]
        if not matched_edges:
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
                # Filters decide which tasks qualify for the page, but the
                # drill-down payload still returns each task's full raw
                # unhandled-edge evidence.
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

    return {
        "items": items,
        "total": total_matched,
        "task_types": sorted(all_task_types),
        "blind_spot_counts": dict(blind_spot_counts),
        "edge_case_counts": dict(
            sorted(edge_case_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
    }


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

    from app.sr_analytics import get_sr_alert_drilldown_tasks  # noqa: PLC0415

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
            raiseload=True,)
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

    cutoff = _utcnow() - timedelta(days=days)
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
            raiseload=True,)
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
    envelope = await aggregate_sr_reviewer_disagreements_envelope(
        session,
        project_id,
        days=days,
        limit=limit,
    )
    # Spread the envelope (window_days, total_distinct_categories,
    # returned_entries, disagreements) and preserve the legacy ``total_entries``
    # field (= returned slice size) for sibling-owned consumers that have not
    # yet migrated to the additive true-total fields.
    return {
        **envelope,
        "total_entries": envelope["returned_entries"],
    }


@router.get("/projects/{project_id}/sr-reviewer-disagreements/tasks")
async def fetch_sr_reviewer_disagreement_tasks(
    project_id: uuid.UUID,
    days: int = Query(30, ge=1, le=90),
    category: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return paginated task-level rows behind a reviewer-disagreement badge.

    A disagreement is a category present in reviewer_findings where self-review
    either passed or omitted the category.  Filters by category, task_type,
    and severity (of the reviewer finding).
    """
    cutoff = _utcnow() - timedelta(days=days)
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
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
    )

    page_items = []
    total_matched = 0
    category_counts: Dict[str, int] = defaultdict(int)
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        t_type = (t.first_payload or {}).get("type", "")
        if t_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or _is_skipped_sr_outcome(sro):
            continue
        check_results = safe_check_results(sro)
        reviewer_findings = sro.get("reviewer_findings") or []
        if not reviewer_findings:
            continue

        reviewer_cats = {
            f.get("category")
            for f in reviewer_findings
            if isinstance(f, dict) and f.get("category")
        }
        origin_rescued = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict) and result.get("origin") in _REVIEWER_ORIGINS
        }
        if origin_rescued:
            missed_cats = origin_rescued
        else:
            missed_cats = {
                cat
                for cat in reviewer_cats
                if check_results.get(cat) is None
                or (check_results.get(cat) or {}).get("passed", True)
            }

        if not missed_cats:
            continue
        if task_type and t_type != task_type:
            continue
        if category and category not in missed_cats:
            continue

        # Severity filter: skip if no finding for the selected category matches severity
        if severity and category:
            matching = next(
                (
                    f
                    for f in reviewer_findings
                    if isinstance(f, dict)
                    and f.get("category") == category
                    and f.get("severity") == severity
                ),
                None,
            )
            if not matching:
                continue
        elif severity:
            # Filter to tasks where any missed-cat finding has the requested severity
            has_match = any(
                isinstance(f, dict)
                and f.get("category") in missed_cats
                and f.get("severity") == severity
                for f in reviewer_findings
            )
            if not has_match:
                continue

        current_index = total_matched
        total_matched += 1
        for cat in missed_cats:
            category_counts[cat] += 1

        if current_index < page_start or current_index >= page_end:
            continue

        sr_check_results, reviewer_findings_out = _sr_miss_drilldown(sro, missed_cats)
        page_items.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": t_type,
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "sr_check_results": sr_check_results,
                "reviewer_findings": reviewer_findings_out,
            }
        )

    # Batch-fetch PR artifacts for page items
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

    items = [
        {**m, "pr_url": pr_map.get(m["task_id"])}
        for m in page_items
    ]

    return {
        "items": items,
        "total": total_matched,
        "page": page,
        "page_size": page_size,
        "category_counts": dict(category_counts),
    }


@router.get("/projects/{project_id}/direction-sr-breakdown")
async def get_direction_sr_breakdown(
    project_id: uuid.UUID,
    days: int = Query(30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-direction SR catch-rate breakdown.

    Primary path: reads the canonical ``sr_catch_rate_trends.by_direction``
    snapshot already persisted on the project, so direction-level data uses
    the same measurement population as the headline project-wide SR metric
    (includes failed terminal tasks, preserves trend/history metadata, and
    is not starved by the 2000-task COMPLETED-only fallback window).

    Fallback (no stored snapshot): recomputes from COMPLETED tasks within
    *days*, matching the pre-snapshot behaviour.

    Per-direction shape::

        {
          "direction_id": str,
          "direction_text": str,
          "sample_size": int,
          "overall_catch_rate": float | None,
          "categories": {
            "<cat>": {"hit_rate", "miss_rate", "sample_count", "examples"?}
          },
          "worst_category": str | None,
          "trend": str | None,
          "previous_miss_rate": float | None,
          "rate_history": list | None
        }
    """
    dir_text: Dict[str, str] = {}
    stored_by_dir: Dict[str, Any] = {}
    project = await session.get(Project, project_id)
    if project:
        for d in project.directions or []:
            did = str(d.get("id") or d.get("direction_id") or "")
            if did:
                dir_text[did] = d.get("text") or d.get("description") or ""
        sr_row = await session.get(ProjectSRAnalytics, project_id)
        _sr_data = sr_row.data if sr_row else None
        stored_trends = (_sr_data or (project.design_doc or {})).get("sr_catch_rate_trends") or {}
        stored_by_dir = stored_trends.get("by_direction") or {}

    if stored_by_dir:
        directions_out: list = []
        sorted_entries = sorted(
            stored_by_dir.items(),
            key=lambda item: (-(item[1].get("miss_rate") or 0), item[0]),
        )
        for direction_id, entry in sorted_entries:
            categories = dict(
                sorted(
                    (entry.get("categories") or {}).items(),
                    key=lambda item: (-(item[1].get("miss_rate") or 0), item[0]),
                )
            )
            worst_category = next(iter(categories), None)
            directions_out.append(
                {
                    "direction_id": direction_id,
                    "direction_text": entry.get("direction_text")
                    or dir_text.get(direction_id, ""),
                    "sample_size": entry.get("sample_count", 0),
                    "overall_catch_rate": entry.get("overall_catch_rate"),
                    "categories": categories,
                    "worst_category": worst_category,
                    "trend": entry.get("trend"),
                    "previous_miss_rate": entry.get("previous_miss_rate"),
                    "rate_history": entry.get("rate_history"),
                }
            )
        return {"directions": directions_out, "min_sample": _MIN_DIRECTION_SR_SAMPLE}

    # No stored snapshot yet — fall back to recomputing from COMPLETED tasks.
    cutoff = _utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(Task.id, Task.title, Task.context, Task.payloads, Task.project_id, raiseload=True)
        )
        .order_by(Task.created_at.desc())
        .limit(2000)
    )
    all_tasks = (await session.execute(stmt)).scalars().all()
    directions_out = compute_direction_sr_breakdown_for_endpoint(
        all_tasks, dir_text, existing_by_direction={}
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
    cutoff = _utcnow() - timedelta(days=days)
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
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
    )

    rows: list[dict[str, Any]] = []
    matched_task_ids: list[uuid.UUID] = []
    total_matched = 0
    task_stream = await session.stream(stmt)
    async for t in task_stream.scalars():
        ctx = t.context or {}
        sr_outcome = ctx.get("_self_review_outcome")
        if not sr_outcome:
            continue
        reviewer_confirmed_cats = reviewer_confirmed_categories(ctx)
        if not reviewer_confirmed_cats:
            continue
        check_results = safe_check_results(sr_outcome)
        sr_failed = _author_sr_failed_categories(check_results)
        missed = reviewer_confirmed_cats - sr_failed
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
                    "caught_categories": sorted(reviewer_confirmed_cats & sr_failed),
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
    project = await _get_user_project_with_role(
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
        .options(load_only(Task.id, Task.status, raiseload=True))
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
                "base_branch": get_project_integration_branch(project),
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
        .options(load_only(Task.id, Task.context, raiseload=True))
    )
    tasks = (await session.execute(stmt)).scalars().all()

    task_ids: Dict[str, str] = {}
    for t in tasks:
        cat = (t.context or {}).get("sr_category")
        if cat:
            task_ids[str(cat)] = str(t.id)

    return {"task_ids": task_ids}


@router.get("/projects/{project_id}/sr_catch_rate/health")
async def get_sr_catch_rate_health(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Summarize the persisted SR catch-rate snapshot for ops/audit/CI.

    READ-ONLY: reads ``project.design_doc['sr_catch_rate_trends']`` (written
    asynchronously by ``compute_sr_catch_rate_trends``) and never recomputes.
    The derived ``status`` mirrors the dispatcher staleness watchdog's branch
    semantics and thresholds so this endpoint and the alert system agree.
    Viewer+ role required.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    # Function-local import to avoid a circular import. The signal-staleness
    # windows + ISO parser were re-homed to the pipeline alert-scan service
    # when the regression dispatcher was removed; this endpoint and the alert
    # system share the same thresholds so their status semantics agree.
    from app.pipeline_alert_scan import (  # noqa: PLC0415
        _SIGNAL_STALENESS_WINDOWS,
        _SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW,
        _SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW,
        _parse_iso_timestamp,
    )

    now = _utcnow()
    general_window = _SIGNAL_STALENESS_WINDOWS["sr_catch_rate"]
    _sr_row = await session.get(ProjectSRAnalytics, project_id)
    _sr_data = _sr_row.data if _sr_row else None
    trends: Dict[str, Any] = (_sr_data or (project.design_doc or {})).get(
        "sr_catch_rate_trends"
    ) or {}

    signal_raw = trends.get("signal_last_computed_at")
    sufficient_raw = trends.get("last_sufficient_computed_at")
    signal_at = _parse_iso_timestamp(signal_raw)
    sufficient_at = _parse_iso_timestamp(sufficient_raw)
    data_sufficient = bool(trends.get("data_sufficient"))

    def _age_hours(dt: Optional[datetime]) -> Optional[float]:
        return round((now - dt).total_seconds() / 3600, 2) if dt is not None else None

    if not trends:
        status = "missing"
    elif signal_raw and not sufficient_raw:
        status = "never_sufficient"
    elif (
        signal_at is not None
        and sufficient_at is not None
        and (now - signal_at) <= _SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW
        and (now - sufficient_at) > _SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW
    ):
        status = "frozen"
    elif sufficient_at is not None and (now - sufficient_at) > general_window:
        status = "stale"
    elif not data_sufficient:
        status = "low_data"
    else:
        status = "healthy"

    categories = trends.get("categories") or {}
    categories_with_hit_rate = sum(
        1
        for c in categories.values()
        if isinstance(c, dict) and c.get("current_hit_rate") is not None
    )

    return {
        "status": status,
        "data_sufficient": data_sufficient,
        "last_sufficient_computed_at": sufficient_raw,
        "last_sufficient_age_hours": _age_hours(sufficient_at),
        "signal_last_computed_at": signal_raw,
        "signal_age_hours": _age_hours(signal_at),
        "category_count": len(categories),
        "categories_with_hit_rate": categories_with_hit_rate,
        "overall_hit_rate": trends.get("overall_hit_rate"),
        "previous_overall_hit_rate": trends.get("previous_overall_hit_rate"),
        "overall_trend_verdict": trends.get("overall_trend_verdict"),
        "overall_trend_significant": trends.get("overall_trend_significant"),
        "bucket_mode": trends.get("bucket_mode"),
        "prompt_version_changed": bool(trends.get("prompt_version_changed")),
        "mixed_prompt_versions": bool(trends.get("mixed_prompt_versions")),
    }


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
        .options(load_only(StrategicCycle.id, StrategicCycle.created_at, raiseload=True))
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
        .options(load_only(Task.id, Task.plan_output, Task.created_at, Task.context, raiseload=True))
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
    # payloads so we can exclude research-type tasks below. The generalized
    # outcome walk (research_actionability -> task_outcomes.is_successful_outcome)
    # additionally classifies CANCELLED follow-ons through
    # direction_outcomes._is_cascade_cancelled, which reads
    # latest_status_message — left deferred, that read lazy-loads inside the
    # async session and raises MissingGreenlet (prod 2026-06-07). plan_output
    # backs the read-only types' honest success signals (audit findings, memo
    # artifact, verify-tests verdict), so without it a non-code follow-on
    # silently scores neutral and grade_succeeded under-counts.
    followon_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.payloads,
                Task.latest_status_message,
                Task.plan_output,
            raiseload=True,)
        )
        .where(
            Task.project_id == project_id,
            Task.created_at >= oldest_cycle_at,
        )
        .limit(2000)
    )
    followon_tasks = followon_result.scalars().all()

    # Global aggregation across the full window. The extended form additionally
    # tracks grade_succeeded (findings whose follow-on achieved ANY terminal
    # evaluation success, not merge-only) so research calibration reflects
    # forward quality, not merge-only conversion.
    grade_counts, grade_tasked, grade_merged, grade_succeeded = (
        _aggregate_actionability_extended(
            research_tasks,
            followon_tasks,
            research_task_ids,
        )
    )
    rates = _compute_actionability_rates_extended(
        grade_counts, grade_tasked, grade_merged, grade_succeeded
    )
    grade_merge_rate = rates["grade_merge_rate"]
    grade_merge_rate_of_tasked = rates["grade_merge_rate_of_tasked"]
    calibration_score = rates["calibration_score"]
    grade_success_rate = rates["grade_success_rate"]
    generalized_calibration_score = rates["generalized_calibration_score"]

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
        b_counts, b_tasked, b_merged, b_succeeded = _aggregate_actionability_extended(
            bucket_tasks,
            followon_tasks,
            research_task_ids,
        )
        b_rates = _compute_actionability_rates_extended(
            b_counts,
            b_tasked,
            b_merged,
            b_succeeded,
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
                "grade_merge_rate": b_rates["grade_merge_rate"],
                "calibration_score": b_rates["calibration_score"],
                # Generalized forward-quality sub-metrics (additive; non-code
                # follow-on successes count alongside merges).
                "grade_succeeded": b_succeeded,
                "grade_success_rate": b_rates["grade_success_rate"],
                "generalized_calibration_score": b_rates[
                    "generalized_calibration_score"
                ],
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
        # Generalized forward-quality calibration (additive). grade_succeeded
        # counts findings whose follow-on achieved ANY terminal evaluation
        # success (PR merge OR successful audit/memo/research/verification),
        # so research calibration is not merge-only. grade_merge_* above
        # remain the code-task sub-metric.
        "grade_succeeded": grade_succeeded,
        "grade_success_rate": grade_success_rate,
        "generalized_calibration_score": generalized_calibration_score,
        "cycle_history": cycle_history,
    }
