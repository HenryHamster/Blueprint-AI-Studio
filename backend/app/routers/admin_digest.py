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
from app.digest_cache import DigestCache, get_digest_cache
from app.time_utils import _to_naive_utc
from app.models import (
    Task,
    TaskStatus,
    Project,
    Artifact,
    ArtifactType,
    _utcnow,
    RegressionRemediationCase,
    StrategicCycle,
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
)
from app.auth import get_current_user, require_admin
from app.shared_state import runtime_config as _runtime_config
from app.routers.admin_analytics import _derive_completed_at, ON_DEMAND_RATE, SPOT_RATE
from app import strategic_cycle as _strategic_cycle
from app.strategic_analysis import _compute_prediction_calibration
from app.digest_metrics import (
    _compute_edges_considered_metrics,  # noqa: F401  re-exported for external importers
    _compute_first_attempt_breakdowns,
    _compute_first_review_approval_breakdowns,
    _compute_layer_coverage_by_class,  # noqa: F401  re-exported for external importers
    _compute_regression_case_summary,  # noqa: F401  re-exported for external importers
    _compute_sr_disallowed_blind_spot_rate,
    _compute_sr_skip_rate,
    _compute_symptom_layers_by_class,  # noqa: F401  re-exported for external importers
    _serialize_sr_catch_rate_trends,
    _stash_projected_task_fields,
    _task_prompt_fingerprint,  # noqa: F401  re-exported for external importers
    compute_anomaly_adjustment_summary,  # noqa: F401  re-exported for external importers
    compute_digest_cost_economics,
    compute_digest_trend_history,  # noqa: F401  re-exported for external importers
    compute_direction_progress,
    compute_failure_grouping,  # noqa: F401  re-exported for external importers
    _CPM_RETIREMENT_MULTIPLIER,  # noqa: F401  re-exported for external importers
    compute_migration_health,  # noqa: F401  re-exported for external importers
    compute_recurring_error_clusters,
    compute_rejection_pattern_summary,  # noqa: F401  re-exported for external importers
    compute_remediation_and_patterns,  # noqa: F401  re-exported for external importers
    compute_remediation_funnel,  # noqa: F401  re-exported for external importers
    compute_resurrection_outcome_summary,  # noqa: F401  re-exported for external importers
    compute_retry_pattern_summary,  # noqa: F401  re-exported for external importers
    compute_review_cap_exhaustion_summary,  # noqa: F401  re-exported for external importers
    compute_review_quality_metrics,
    compute_scope_mismatch_summary,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-digest"],
    dependencies=[Depends(get_current_user)],
)

# Viewport-debt analytics live in their own module (direction d-ce93e938 —
# serialize one monolith at a time). Re-mount the sub-router here so the
# public path /api/admin/projects/{project_id}/viewport-debt, the
# get_current_user dependency, and the response shape are unchanged.
from app.viewport_debt import router as _viewport_debt_router  # noqa: E402
from app.sr_diagnostics_endpoints import router as _sr_diagnostics_router  # noqa: E402
from app.anomaly_detection import router as _anomaly_detection_router  # noqa: E402
from app.efficiency_snapshot import router as _efficiency_snapshot_router  # noqa: E402
from app.routers.admin_pipeline_observability import router as _pipeline_obs_router  # noqa: E402
from app.routers.admin_release_version import router as _release_version_router  # noqa: E402

router.include_router(_viewport_debt_router)
router.include_router(_sr_diagnostics_router)
router.include_router(_anomaly_detection_router)
router.include_router(_efficiency_snapshot_router)
router.include_router(_pipeline_obs_router)
router.include_router(_release_version_router)

# Residual/regression-case CRUD endpoints relocated to app.regression_case_endpoints.
# Re-exported here so existing call sites keep working.
from app.regression_case_endpoints import (  # noqa: E402
    router as _regression_case_router,
    clear_regression_manual_intervention,  # noqa: F401  re-exported for external importers
)

router.include_router(_regression_case_router)

# Release version lifecycle endpoints relocated to app.routers.admin_release_version.
# Re-exported here so existing call sites keep working.
from app.routers.admin_release_version import (  # noqa: E402, F401
    _MAX_RETIREMENT_CRITIQUES,
    _SetVersionPhaseBody,
    _VERDICT_HISTORY_LIMIT,
    _format_qualification,
    get_retirement_critiques,
    get_version_lifecycle,
    list_qualification_verdicts,
    set_version_phase,
)

_SIGNAL_STALENESS_TITLE_PREFIX = "Signal staleness: "
_ACTIVE_TASKS_QUERY_LIMIT = 500

# Pipeline-observability endpoints relocated to app.routers.admin_pipeline_observability
# (cluster 1/4 — keep admin_digest focused on the catch-up digest). Re-exported here so
# existing `from app.routers.admin_digest import get_X` call sites and test patch targets
# keep working. The modularity tripwire (test_admin_digest_modularity.py) enforces this.
from app.anomaly_detection import (  # noqa: E402, F401
    get_anomaly_tracker_snapshot,
)
from app.efficiency_snapshot import (  # noqa: E402, F401
    get_efficiency_axes,
    get_llm_cost_breakdown,
)
from app.routers.admin_pipeline_observability import (  # noqa: E402, F401
    get_canonical_regression_layers,
    get_drift_snapshot,
    get_fix_loop_convergence,
    get_fix_loop_convergence_trend,
    get_fix_loop_diverged_tasks,
    get_pipeline_efficiency,
    get_pipeline_regressions,
    get_provider_cost_summary,
    get_regression_dispatcher_health,
    get_regression_escalation_outcomes,
    get_regression_mtta,
    get_warm_reuse_funnel,
)

# Pipeline-health cluster relocated to app.pipeline_health (non-router module).
# Re-exported here so existing call sites and test patch targets keep working.
from app.pipeline_health import (  # noqa: E402
    _SR_CATCH_RATE_TARGET_ALERT_PREFIX,  # noqa: F401
    _SR_CATCH_RATE_TARGET_MIN_WINDOWS,  # noqa: F401
    _SR_CATCH_RATE_MIN_FINDINGS,  # noqa: F401
    _LONE_CHECKER_ALERT_TITLE,  # noqa: F401
    _LONE_CHECKER_MIN_CATEGORIES,  # noqa: F401
    _evaluate_sr_catch_rate_targets,  # noqa: F401
    _evaluate_lone_checker_alert,  # noqa: F401
    compute_pipeline_health_summary,  # noqa: F401
)


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
                    # at_time correlation in compute_failure_grouping /
                    # compute_digest_trend_history reads last_heartbeat; load it
                    # here so the access doesn't trigger an async lazy-load.
                    Task.last_heartbeat,
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
                    # at_time correlation in compute_failure_grouping /
                    # compute_digest_trend_history reads last_heartbeat; load it
                    # here so the access doesn't trigger an async lazy-load.
                    Task.last_heartbeat,
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
            .limit(_ACTIVE_TASKS_QUERY_LIMIT)
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
            .limit(_ACTIVE_TASKS_QUERY_LIMIT)
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

    if project_id:
        project = await session.get(Project, project_id)
        if project:
            (
                direction_progress_list,
                scope_conversion_rates,
                prev_scope_conversion_rates,
                dir_id_to_text,
                _,
            ) = await compute_direction_progress(
                session,
                project=project,
                all_tasks=all_tasks,
                window_start=window_start,
                prev_window_start=prev_window_start,
            )

    # --- Batch artifact query ---
    all_task_ids = [t.id for t in all_tasks]
    pr_count = 0
    pr_artifact_map: Dict[str, str] = {}  # task_id -> pr_url
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error snippet

    for _chunk_start in range(0, len(all_task_ids), 200):
        _chunk = all_task_ids[_chunk_start : _chunk_start + 200]
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(_chunk),
                Artifact.artifact_type.in_(
                    [
                        ArtifactType.PULL_REQUEST,
                        ArtifactType.ERROR_LOG,
                    ]
                ),
            )
            .order_by(Artifact.created_at.asc())
            .limit(500)
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
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT,
        TAXONOMY_INFRA_RELATED,
    )

    _fg = compute_failure_grouping(
        failed_tasks=failed_tasks,
        error_log_map=error_log_map,
        dir_id_to_text=dir_id_to_text,
        window_start=window_start,
        now=_now,
    )
    failure_groups = _fg["failure_groups"]
    cost_by_failure_class = _fg["cost_by_failure_class"]
    total_failed_cost_usd = _fg["total_failed_cost_usd"]
    provision_waste_cost_usd = _fg["provision_waste_cost_usd"]
    _failure_stage_counts = _fg["failure_stage_counts"]
    analysis_overflow_summary = _fg["analysis_overflow_summary"]
    provision_waste_by_direction = _fg["provision_waste_by_direction"]

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
    trend_history = compute_digest_trend_history(
        all_tasks=all_tasks,
        hours=hours,
        now=_now,
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

    _cost_econ = compute_digest_cost_economics(
        all_tasks=all_tasks,
        direction_progress_list=direction_progress_list,
        window_start=window_start,
        now=_utcnow(),
        daily_cost_ceiling_usd=float(_runtime_config.get("daily_cost_ceiling_usd", 0)),
    )
    total_estimated_cost_usd = _cost_econ["total_estimated_cost_usd"]
    total_spot_cost_usd = _cost_econ["total_spot_cost_usd"]
    total_on_demand_cost_usd = _cost_econ["total_on_demand_cost_usd"]
    provisioning_overhead_usd = _cost_econ["provisioning_overhead_usd"]
    provisioning_overhead_pct = _cost_econ["provisioning_overhead_pct"]
    daily_cost_usd = _cost_econ["daily_cost_usd"]
    daily_cost_ceiling_usd = _cost_econ["daily_cost_ceiling_usd"]
    overall_product_ratio = _cost_econ["overall_product_ratio"]
    median_cost_per_merged_pr = _cost_econ["median_cost_per_merged_pr"]
    cost_per_merge_by_type = _cost_econ["cost_per_merge_by_type"]
    _median_cpm_by_type = _cost_econ["median_cpm_by_type"]
    pipeline_acceptance_rate = _cost_econ["pipeline_acceptance_rate"]
    pipeline_conversion_rate = _cost_econ["pipeline_conversion_rate"]
    pipeline_autonomous_prs_merged = _cost_econ["pipeline_autonomous_prs_merged"]
    pipeline_assisted_prs_merged = _cost_econ["pipeline_assisted_prs_merged"]
    pipeline_operator_action_count = _cost_econ["pipeline_operator_action_count"]
    overall_type_distribution = _cost_econ["overall_type_distribution"]
    pipeline_filter_funnel = _cost_econ["pipeline_filter_funnel"]

    # --- Scope mismatch diagnostics ---
    # Aggregate calibration data across terminal tasks in the window to surface
    # systematic scope-estimation drift and chronic-calibration directions.
    scope_mismatch_summary: Optional[Dict[str, Any]] = compute_scope_mismatch_summary(
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        dir_id_to_text=dir_id_to_text,
    )

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
    memo_ever_cited: dict[str, Any] | None = None
    if project_id:
        _proj = await session.get(Project, project_id)
        memo_pathway_exits = await _strategic_cycle.count_memo_exits(
            session, project_id
        )
        memo_ever_cited = await _strategic_cycle.ever_cited_memo(session, project_id)
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

    rejection_pattern_summary = compute_rejection_pattern_summary(
        all_tasks, window_start=window_start
    )

    # --- Retry pattern summary ---
    retry_pattern_summary = compute_retry_pattern_summary(failed_tasks)

    # --- Review cap exhaustion summary ---
    review_cap_exhaustion_summary = compute_review_cap_exhaustion_summary(
        failed_tasks, window_start=window_start
    )

    # --- Recurring error fingerprint clusters ---
    recurring_error_clusters = compute_recurring_error_clusters(
        failed_tasks=failed_tasks,
        error_log_map=error_log_map,
    )

    total_merge_retries_saved = 0
    merge_early_exit_count = 0
    for _t in completed_tasks + failed_tasks:
        _saved = (_t.context or {}).get("_merge_retries_saved")
        if isinstance(_saved, int) and _saved > 0:
            total_merge_retries_saved += _saved
            merge_early_exit_count += 1

    # Resurrection outcomes: count tasks the reaper auto-resurrected and how many
    # of those still ended up failing (net-positive signal for operators).
    resurrection_outcome_summary = compute_resurrection_outcome_summary(
        completed_tasks, failed_tasks
    )

    _remediation_result = await compute_remediation_and_patterns(
        session=session,
        project_id=project_id,
        window_start=window_start,
        failed_tasks=failed_tasks,
        all_tasks=all_tasks,
        active_tasks_all=active_tasks_all,
        dir_id_to_text=dir_id_to_text,
    )
    remediation_funnel = _remediation_result["remediation_funnel"]
    investigation_conversion = _remediation_result["investigation_conversion"]
    recurring_patterns = _remediation_result["recurring_patterns"]
    early_warnings = _remediation_result["early_warnings"]

    # --- Anomaly-driven pipeline adjustment snapshot ---
    anomaly_adjustment_summary = compute_anomaly_adjustment_summary()

    _review_quality = compute_review_quality_metrics(completed_tasks + failed_tasks)
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

    migration_health = compute_migration_health(
        completed_tasks, failed_tasks, error_log_map=error_log_map
    )

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
        "parallel_review_effectiveness": _review_quality[
            "parallel_review_effectiveness"
        ],
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
        "memo_ever_cited": memo_ever_cited,
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
