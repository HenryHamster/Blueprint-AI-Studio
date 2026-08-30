"""Catch-up digest and pipeline health summary endpoints."""

import time
import uuid
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session, resolve_dialect_name
from app.project_telemetry import read_telemetry_or_doc as _read_telemetry_or_doc
from app.digest_cache import DigestCache, get_digest_cache
from app.digest_precompute import (
    DIGEST_KIND,
    PRECOMPUTE_STALE_FALLBACK_SECONDS,
    annotate_precompute_metadata,
    get_precompute_cache,
    read_shared,
    shared_cache_key,
)
from app.time_utils import _to_naive_utc, _coerce_naive_utc, utc_iso
from app.models import (
    Task,
    TaskStatus,
    Project,
    Artifact,
    ArtifactType,
    _utcnow,
    StrategicCycle,
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
)
from app.auth import get_current_user, require_admin
from app.digest_latency import record_digest_request
from app.shared_state import runtime_config as _runtime_config
from app.routers.admin_analytics import _derive_completed_at, ON_DEMAND_RATE, SPOT_RATE
from app import strategic_cycle as _strategic_cycle
from app.strategic_analysis import _compute_prediction_calibration
from app.strategic_completion import strategic_challenge_summary
from app.task_surface_visibility import non_resident_infrastructure_task_filter
from app.capability_trial_verdict import (
    LANE_PILOT_CAP_NAME,
    evaluate_lane_pilot_verdict,
    evaluate_trial_verdict,
    get_effective_holdback_fraction,
    get_trial_definitions,
    is_ambiguous_lift_escalation_needed,
    is_trial_stalled,
    persist_accrual_projections_in_transaction,
    persist_ambiguous_lift_escalations_in_transaction,
    persist_stalled_trial_notices_in_transaction,
    persist_trial_verdicts_in_transaction,
)
from app.digest_metrics import (
    _compute_edges_considered_metrics,  # noqa: F401  re-exported for external importers
    _compute_first_attempt_breakdowns,
    _compute_first_review_approval_breakdowns,
    _compute_sr_disallowed_blind_spot_rate,
    _compute_sr_skip_rate,
    _serialize_sr_catch_rate_trends,
    _stash_projected_task_fields,
    _task_prompt_fingerprint,  # noqa: F401  re-exported for external importers
    compute_anomaly_adjustment_summary,  # noqa: F401  re-exported for external importers
    compute_effort_model_efficiency,
    compute_dependency_health_signals,
    compute_trial_arm_accumulation,
    compute_trial_arm_accrual,
    compute_trial_arm_cost_delta,
    compute_park_wake_health,
    compute_merge_gate_fleet_health,
    compute_digest_cost_economics,
    compute_digest_trend_history,  # noqa: F401  re-exported for external importers
    compute_direction_progress,
    compute_failure_grouping,  # noqa: F401  re-exported for external importers
    _CPM_RETIREMENT_MULTIPLIER,  # noqa: F401  re-exported for external importers
    compute_migration_health,  # noqa: F401  re-exported for external importers
    compute_migration_slot_contention,
    compute_narrative_summary,
    compute_outer_loop_convergence,  # noqa: F401  re-exported for external importers
    compute_recurring_error_clusters,
    compute_rejection_pattern_summary,  # noqa: F401  re-exported for external importers
    compute_remediation_and_patterns,  # noqa: F401  re-exported for external importers
    compute_remediation_funnel,  # noqa: F401  re-exported for external importers
    compute_resurrection_outcome_summary,  # noqa: F401  re-exported for external importers
    compute_retry_analytics,  # noqa: F401  re-exported for external importers
    compute_retry_pattern_summary,  # noqa: F401  re-exported for external importers
    compute_review_cap_exhaustion_summary,  # noqa: F401  re-exported for external importers
    compute_release_qualifier_infra_metrics,  # noqa: F401  re-exported for external importers
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
from app.routers.admin_release_qualification_control import router as _qualification_control_router  # noqa: E402

router.include_router(_viewport_debt_router)
router.include_router(_sr_diagnostics_router)
router.include_router(_anomaly_detection_router)
router.include_router(_efficiency_snapshot_router)
router.include_router(_pipeline_obs_router)
router.include_router(_release_version_router)
router.include_router(_qualification_control_router)

# Residual-case CRUD endpoints relocated to app.residual_case_endpoints.
# Re-exported here so existing call sites keep working.
from app.residual_case_endpoints import (  # noqa: E402
    router as _residual_case_router,
)

router.include_router(_residual_case_router)

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


# Pipeline-observability endpoints relocated to app.routers.admin_pipeline_observability
# (cluster 1/4 — keep admin_digest focused on the catch-up digest). Re-exported here so
# existing `from app.routers.admin_digest import get_X` call sites and test patch targets
# keep working. The modularity tripwire (test_admin_digest_modularity.py) enforces this.
from app.anomaly_detection import (  # noqa: E402, F401
    get_anomaly_tracker_snapshot,
)
from app.cost_telemetry_reconciliation import (  # noqa: E402, F401
    reconcile_llm_cost,
    reconcile_provider_reported_usage,
)
from app.efficiency_snapshot import (  # noqa: E402, F401
    compute_provider_routing_share,
    compute_provider_routing_verdict,
    get_efficiency_axes,
    get_llm_cost_breakdown,
)
from app.routers.admin_pipeline_observability import (  # noqa: E402, F401
    get_drift_snapshot,
    get_fix_loop_convergence,
    get_fix_loop_convergence_trend,
    get_fix_loop_diverged_tasks,
    get_outer_loop_convergence,
    get_outer_loop_convergence_trend,
    get_pipeline_efficiency,
    get_pipeline_regressions,
    get_provider_cost_summary,
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
    read_project_sr_analytics,
)


def _without_per_task(reconciliation: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Drop the zero-reader per_task rows from a served reconciliation dict.

    per_task has no readers anywhere in the codebase (backend or admin-ui) but
    dominates the digest response's byte size, since the same dict is
    serialized four times (nested under effort_model_efficiency twice, and
    top-level twice). Strip it at the response boundary only — the in-process
    ReconciliationResult.asdict() output used elsewhere keeps per_task intact.
    """
    if reconciliation is None:
        return None
    return {k: v for k, v in reconciliation.items() if k != "per_task"}


@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Return a catch-up digest of activity within the requested time window.

    Read path (Design-audit #1 fix): the heavy 5000-row JSONB scan is now run
    on a leader-only background timer (``digest_precompute_service``) and the
    result stored in a lightweight in-process cache. This endpoint first serves
    that precomputed payload when it is fresh (within
    ``PRECOMPUTE_STALE_FALLBACK_SECONDS``), so a normal admin poll is a cheap
    dict read — no per-request 5000-row scan, the documented root of the
    2026-05-29 pool-starvation hang.

    Freshness invariant (multi-instance / non-leader correctness): only a
    cache entry written by the background WORKER is served as
    fresh-for-``PRECOMPUTE_STALE_FALLBACK_SECONDS`` (``worker_only=True``),
    because only the leader's tick continuously refreshes it. On a NON-leader
    instance the worker never runs, so there is no worker-written entry and we
    deliberately fall through to the ``DigestCache`` path below — its short TTL
    keeps non-leader freshness bounded, NOT the 5-minute fallback
    window. (An earlier version primed the precompute cache opportunistically
    from this on-demand compute; that one-shot, never-refreshed entry would
    then be served as precomputed-for-300s on subsequent non-leader polls,
    regressing freshness from 15s to 5 minutes — so we no longer prime it
    here.)

    Bounded fallback: when there is no fresh WORKER entry for this key (fresh
    boot, non-leader instance, or a non-primed window such as a custom
    ``hours``), fall back to the existing ``DigestCache`` (short TTL + in-flight
    dedup + serve-stale-on-failure) so at most one on-demand 5000-row scan runs
    per key per TTL — the prior behavior, preserved as a safety net.
    """
    _t0 = time.monotonic()
    precompute_cache = get_precompute_cache()
    entry = precompute_cache.get_fresh(
        hours,
        project_id,
        max_age_seconds=PRECOMPUTE_STALE_FALLBACK_SECONDS,
        worker_only=True,
    )
    if entry is not None:
        record_digest_request(time.monotonic() - _t0, "l1_precompute")
        return annotate_precompute_metadata(
            entry.result,
            precomputed=True,
            precomputed_at=entry.computed_wall_iso or None,
            precompute_age_seconds=time.monotonic() - entry.computed_monotonic,
        )

    # L2: cross-instance shared cache. On a NON-leader instance (or any instance
    # right after a deploy) L1 is never worker-filled, so without this every
    # dashboard poll would fall through to the heavy on-demand compute below —
    # the dominant source of the 2026-06-03 statement_timeout 500s. A row here
    # was written by the leader's worker tick, so it carries the SAME freshness
    # guarantee as a local worker L1 entry. Fail-open: read_shared returns None
    # on any error, falling through to the on-demand path unchanged.
    shared = await read_shared(
        shared_cache_key(DIGEST_KIND, hours, project_id),
        max_age_seconds=PRECOMPUTE_STALE_FALLBACK_SECONDS,
        session=session,
    )
    if shared is not None:
        record_digest_request(time.monotonic() - _t0, "l2_shared_cache")
        return annotate_precompute_metadata(
            shared.payload,
            precomputed=True,
            precomputed_at=utc_iso(shared.computed_at),
            precompute_age_seconds=max(
                0.0, (_utcnow() - shared.computed_at).total_seconds()
            ),
        )

    cache_key = ("digest", hours, str(project_id) if project_id else None)
    result, served_stale = await cache.get_or_compute_meta(
        cache_key,
        lambda: _compute_digest(hours=hours, project_id=project_id, session=session),
        ttl=60.0,
    )
    # NOTE: deliberately do NOT prime the precompute cache here. The DigestCache
    # already gives this on-demand path a short TTL + in-flight dedup; priming a
    # non-worker entry would let a non-leader instance serve a never-refreshed
    # payload as fresh-for-PRECOMPUTE_STALE_FALLBACK_SECONDS, regressing
    # non-leader digest freshness from 15s to 5 minutes.
    record_digest_request(time.monotonic() - _t0, "l3_on_demand")
    annotated = annotate_precompute_metadata(
        result,
        precomputed=False,
        precomputed_at=result.get("generated_at") or None,
        precompute_age_seconds=0.0,
    )
    annotated["served_stale"] = served_stale
    return annotated


# Durable keep-list for every top-level key emitted by `_compute_digest`.
#
# Each entry must declare EXACTLY ONE of:
#   {"consumer": "<ComponentName or code path>"}  — a named reader currently renders this key
#   {"status": "retired"|"pending", "reason": "...", "date": "YYYY-MM-DD"}
#       — a retired key (computation removed) or a pending key (emitted but no
#         reader yet).
#
# The invariant tests in test_digest_payload_metric_manifest.py enforce that:
#   1. every emitted top-level key appears here, and
#   2. every active-consumer entry still corresponds to an emitted key.
#
# Precompute-metadata keys added by `annotate_precompute_metadata`
# (precomputed, precomputed_at, precompute_age_seconds, served_stale) are
# governed separately and excluded from this manifest's scope.
DIGEST_PAYLOAD_METRIC_MANIFEST: dict[str, dict] = {
    "window_hours": {"consumer": "CatchUpPanel, CatchUpNarrativeSummary"},
    "tasks_truncated": {"consumer": "DashboardHome"},
    "total_merge_retries_saved": {"consumer": "CatchUpOperationalMetricsSection"},
    "first_attempt_success_rate": {
        "consumer": "CatchUpStatsRow, ReviewRejectionBreakdown"
    },
    "first_review_approval_rate": {"consumer": "CatchUpReviewQualitySection"},
    "self_review_effectiveness": {"consumer": "CatchUpReviewQualitySection"},
    "self_review_prediction_accuracy": {"consumer": "CatchUpReviewQualitySection"},
    "self_review_prediction_accuracy_trends": {
        "consumer": "CatchUpReviewQualitySection"
    },
    "sr_regression_risk_test_signal_trends": {"consumer": "ReviewQualitySection"},
    "sr_false_attestation_trends": {"consumer": "ReviewQualitySection"},
    "self_review_skip_rate": {"consumer": "CatchUpReviewQualitySection"},
    "sr_disallowed_blind_spot_rate": {"consumer": "ReviewQualitySection"},
    "review_accuracy": {"consumer": "CatchUpReviewQualitySection"},
    "edges_trend": {"consumer": "ReviewQualitySection"},
    "rejection_pattern_summary": {
        "consumer": "RejectionPatternSummary, CatchUpCostDiagnosticsSection"
    },
    "merge_early_exit_count": {"consumer": "CatchUpOperationalMetricsSection"},
    "generated_at": {"consumer": "DashboardHome"},
    "completed_count": {"consumer": "CatchUpStatsRow, DashboardHome"},
    "failed_count": {"consumer": "CatchUpStatsRow, DashboardHome"},
    "pending_approval_count": {"consumer": "CatchUpStatsRow"},
    "research_cycles": {"consumer": "CatchUpStatsRow, CatchUpResearchYieldSection"},
    "prs_opened": {"consumer": "CatchUpStatsRow, CatchUpPanel"},
    "active_count": {"consumer": "DashboardHome, CatchUpStatsRow"},
    "running_count": {"consumer": "CatchUpStatsRow"},
    "starting_count": {"consumer": "CatchUpStatsRow"},
    "evaluating_count": {"consumer": "CatchUpStatsRow"},
    "queued_count": {"consumer": "DashboardHome"},
    "stale_heartbeat_count": {"consumer": "OrchestratorHealthBar"},
    "active_tasks": {"consumer": "CatchUpInProgressSection"},
    "success_rate": {"consumer": "CatchUpStatsRow, DashboardHome"},
    "needs_attention": {"consumer": "CatchUpNeedsAttentionSection"},
    "recent_failures": {"consumer": "CatchUpFailureSignalsSection"},
    "failure_groups": {"consumer": "DashboardHome, CatchUpFailureSignalsSection"},
    "failure_taxonomy_distribution": {"consumer": "CatchUpCostDiagnosticsSection"},
    "failure_stage_distribution": {"consumer": "CatchUpCostDiagnosticsSection"},
    "recent_completions": {"consumer": "CatchUpRecentCompletionsSection"},
    "direction_progress": {"consumer": "CatchUpPanel, DirectionPortfolioSection"},
    "direction_portfolio": {"consumer": "DirectionPortfolioSection"},
    "overall_product_ratio": {"consumer": "DashboardHome"},
    "median_cost_per_merged_pr": {"consumer": "CatchUpCostDiagnosticsSection"},
    "cost_per_merge_by_type": {"consumer": "CatchUpCostDiagnosticsSection"},
    "median_cost_per_merge_by_type": {"consumer": "CatchUpCostDiagnosticsSection"},
    "effort_model_efficiency": {"consumer": "DashboardHome"},
    "provider_routing_share": {"consumer": "EffortEfficiencyPanel"},
    "cost_telemetry_reconciliation": {"consumer": "EffortEfficiencyPanel"},
    "cost_telemetry_reconciliation_provider_reported": {"consumer": "EffortEfficiencyPanel"},
    "overall_type_distribution": {"consumer": "CatchUpStatsRow"},
    "total_estimated_cost_usd": {"consumer": "CatchUpStatsRow"},
    "spot_cost_usd": {"consumer": "CatchUpStatsRow"},
    "on_demand_cost_usd": {"consumer": "CatchUpStatsRow"},
    "provisioning_overhead_usd": {"consumer": "CatchUpStatsRow"},
    "provisioning_overhead_pct": {"consumer": "CatchUpCostDiagnosticsSection"},
    "cost_by_failure_class": {
        "consumer": "DashboardHome, CatchUpCostDiagnosticsSection"
    },
    "total_failed_cost_usd": {"consumer": "CatchUpStatsRow, CostOfFailuresWidget"},
    "provision_waste_cost_usd": {"consumer": "CatchUpCostDiagnosticsSection"},
    "provision_waste_by_direction": {"consumer": "CatchUpCostDiagnosticsSection"},
    "analysis_overflow_summary": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_overhead_cost_usd": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_recovery_cost_usd": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_waste_by_direction": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_task_count": {"consumer": "CatchUpCostDiagnosticsSection"},
    "avg_retries_before_failure": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_success_count": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_success_rate_by_class": {"consumer": "CatchUpCostDiagnosticsSection"},
    "retry_wasted_rate": {"consumer": "CatchUpCostDiagnosticsSection"},
    "daily_cost_usd": {"consumer": "DashboardHome, OrchestratorHealthBar"},
    "daily_cost_ceiling_usd": {"consumer": "DashboardHome, OrchestratorHealthBar"},
    "narrative_summary": {"consumer": "CatchUpNarrativeSummary"},
    "prev_completed_count": {"consumer": "CatchUpStatsRow"},
    "prev_failed_count": {"consumer": "CatchUpStatsRow"},
    "prev_success_rate": {"consumer": "CatchUpStatsRow"},
    "completed_delta": {"consumer": "CatchUpStatsRow"},
    "failed_delta": {"consumer": "CatchUpStatsRow"},
    "success_rate_delta": {"consumer": "CatchUpStatsRow"},
    "trend": {"consumer": "CatchUpStatsRow, DashboardHome"},
    "trend_history": {"consumer": "CatchUpPanel"},
    "latest_cycle": {"consumer": "CatchUpLatestCycleSection, DashboardHome"},
    "memo_pathway_exits": {"consumer": "CatchUpMemoPathwaySection"},
    "memo_ever_cited": {"consumer": "CatchUpMemoPathwaySection"},
    "recent_cycle_memo_stats": {"consumer": "CatchUpMemoPathwaySection"},
    "scope_conversion_rates": {
        "consumer": "CatchUpResearchYieldSection, DashboardHome"
    },
    "prev_scope_conversion_rates": {
        "consumer": "CatchUpResearchYieldSection, DashboardHome"
    },
    "pipeline_acceptance_rate": {
        "consumer": "DashboardHome, CatchUpCostDiagnosticsSection"
    },
    "pipeline_conversion_rate": {
        "consumer": "DashboardHome, CatchUpCostDiagnosticsSection"
    },
    "pipeline_filter_funnel": {"consumer": "CatchUpCostDiagnosticsSection"},
    "scope_mismatch_summary": {"consumer": "CatchUpCostDiagnosticsSection"},
    "escalation_outcome_summary": {"consumer": "CatchUpResearchYieldSection"},
    "prevention_efficacy": {"consumer": "CatchUpResearchYieldSection"},
    "retry_pattern_summary": {"consumer": "CatchUpFailureSignalsSection"},
    "review_cap_exhaustion_summary": {"consumer": "CatchUpCostDiagnosticsSection"},
    "resurrection_outcome_summary": {"consumer": "CatchUpOperationalMetricsSection"},
    "recurring_error_clusters": {"consumer": "CatchUpFailureSignalsSection"},
    "recurring_patterns": {"consumer": "CatchUpFailureSignalsSection"},
    "early_warnings": {"consumer": "CatchUpFailureSignalsSection"},
    "remediation_funnel": {"consumer": "CatchUpRemediationPipelineSection"},
    "investigation_conversion": {"consumer": "DashboardHome"},
    "anomaly_adjustment": {"consumer": "AnomalyThrottleBanner"},
    "dependency_health_signals": {"consumer": "DependencyHealthCard"},
    "park_wake_health": {"consumer": "DashboardHome"},
    "merge_gate_fleet_health": {"consumer": "DashboardHome"},
    "migration_health": {"consumer": "CatchUpOperationalMetricsSection"},
    "migration_slot_contention": {"consumer": "CatchUpOperationalMetricsSection"},
    "prediction_calibration_factor": {"consumer": "DashboardHome"},
    "prediction_calibration_note": {"consumer": "DashboardHome"},
    "prediction_calibration_sample_count": {"consumer": "DashboardHome"},
    "release_qualifier_infra_metrics": {"consumer": "CatchUpOperationalMetricsSection"},
    "capability_adoption_verdicts": {"consumer": "CapabilityTrialsPanel"},
}


async def _compute_digest(
    *,
    hours: int,
    project_id: uuid.UUID | None,
    session: AsyncSession,
    _statement_timeout_secs: int = 30,
) -> Dict[str, Any]:
    """Extracted body of `get_digest` so the cache layer can call it without
    FastAPI-dependency plumbing."""
    window_start = _utcnow() - timedelta(hours=hours)
    prev_window_start = window_start - timedelta(hours=hours)
    # Narrow main scan to current + previous window only (2xhours).
    # Trend history (7 periods) is computed from a separate lean query below.
    lookback_start = prev_window_start

    # Fast-fail guard: cap this analytics scan well under the 120s global
    # statement_timeout so a slow query releases its pooled connection quickly
    # instead of starving the shared pool (which stalls the orchestrator main
    # cycle → "no progress heartbeat" hang). On timeout the cache layer serves
    # the last good result (see digest_cache.DigestCache). Postgres-only.
    # Background precompute passes a lower budget (e.g. 5s) so a stalled key
    # doesn't pin a background connection for the full 30s.
    if resolve_dialect_name(session) == "postgresql":
        await session.execute(
            text(f"SET LOCAL statement_timeout = '{_statement_timeout_secs}s'")
        )

    # --- Query tasks from narrowed main window (current + previous window) ---
    # The OR clause (status IN terminal OR requires_approval IS TRUE) defeated
    # any composite index, forcing a BitmapOr / seq-scan over the full slice.
    # Split into two index-served queries merged in Python so each half can use
    # its own index (migration 0275 adds ix_task_requires_approval partial index).
    #
    # Query A: terminal tasks (COMPLETED/FAILED/CANCELLED)
    terminal_filters = [
        Task.created_at >= lookback_start,
        Task.status.in_(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
        non_resident_infrastructure_task_filter(),
    ]
    if project_id:
        terminal_filters.append(Task.project_id == project_id)

    # Query B: non-terminal approval tasks (requires_approval=True, not yet terminal).
    # Uses the ix_task_requires_approval partial index (migration 0275).
    # Only current window is needed — approval tasks are surfaced for operator
    # action and are never older than the current window in practice.
    approval_filters = [
        Task.created_at >= window_start,
        Task.requires_approval.is_(True),
        Task.status.not_in(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
        non_resident_infrastructure_task_filter(),
    ]
    if project_id:
        approval_filters.append(Task.project_id == project_id)

    _DIGEST_TASK_ROW_CAP = 5000
    _ACTIVE_TASKS_DISPLAY_LIMIT = 5
    _ACTIVE_TASKS_SIGNAL_LIMIT = 50
    # Project payloads[0] and status_log[-1]->>'t' SQL-side on PostgreSQL.
    # Loading the full JSONB arrays for 5000 rows blew a Cloud Run instance
    # past its 4 GiB limit under concurrent polling; with projection each
    # row carries only the first payload element plus a timestamp string.
    # SQLite (used in tests) doesn't support negative-index JSON paths and
    # returns json_extract results as text rather than parsed dicts, so we
    # fall back to the heavy load path there.
    _use_projection = resolve_dialect_name(session) == "postgresql"

    def _fat_load_options(*, include_status_log: bool = False):
        """Load-only options for the main terminal+approval task queries."""
        cols = [
            Task.id,
            Task.status,
            Task.project_id,
            Task.title,
            Task.created_at,
            Task.completed_at,
            Task.started_at,
            Task.last_heartbeat,
            Task.context,
            Task.latest_status_message,
            Task.attempt_count,
            Task.requires_approval,
            Task.vm_provisioned_at,
            Task.vm_started_at,
            Task.vm_stopped_at,
            Task.vm_spot,
            Task.previous_error_fingerprint,
            Task.auto_remediation,
            Task.provider,
            Task.model,
            Task.attempt_history_db,
            Task.api_call_summary,
            # provider_reported_usage: read by reconcile_provider_reported_usage
            # (cost_telemetry_reconciliation.py) for cross-checking agent token
            # counters against the Anthropic API's raw response.usage fields.
            Task.provider_reported_usage,
            Task.llm_input_tokens_banked_db,
            Task.llm_output_tokens_banked_db,
            Task.llm_call_count_banked_db,
            Task.format_compliance_summary,
            # parent_task_id: read by _rollup_parent_ids (via
            # compute_effort_model_efficiency and _cost_and_merges) so
            # merged-PR metrics can exclude rollup parents without tripping
            # raiseload=True.
            Task.parent_task_id,
        ]
        if include_status_log:
            # SQLite path: no SQL-side projection, so load both columns directly.
            cols.append(Task.status_log)
            cols.append(Task.payloads)
        # PG path: payloads[0] and status_log[-1]["t"] are projected as separate
        # SELECT columns (see _stash_projected_task_fields); including them in
        # load_only would duplicate them in the result row and shift column indices.
        return load_only(*cols, raiseload=True)

    all_tasks: List[Task] = []

    if _use_projection:
        # --- Query A: terminal tasks (PG, with SQL-side projections) ---
        terminal_result = await session.execute(
            select(
                Task,
                Task.payloads[0].label("_projected_first_payload"),
                Task.status_log[-1]["t"].astext.label("_projected_completed_at_ts"),
            )
            .where(*terminal_filters)
            .options(_fat_load_options())
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        for row in terminal_result.all():
            task = row[0]
            _stash_projected_task_fields(
                task,
                first_payload=row[1] or {},
                completed_at_ts=row[2],
            )
            all_tasks.append(task)

        # --- Query B: non-terminal approval tasks (PG) ---
        approval_result = await session.execute(
            select(
                Task,
                Task.payloads[0].label("_projected_first_payload"),
                Task.status_log[-1]["t"].astext.label("_projected_completed_at_ts"),
            )
            .where(*approval_filters)
            .options(_fat_load_options())
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        for row in approval_result.all():
            task = row[0]
            _stash_projected_task_fields(
                task,
                first_payload=row[1] or {},
                completed_at_ts=row[2],
            )
            all_tasks.append(task)

        # --- Lean trend history query (PG): 7xhours lookback, narrow columns only ---
        # The main scan covers only 2xhours; trend history needs 7 periods.
        # This separate query loads only the columns required by
        # compute_digest_trend_history (no payloads/api_call_summary/etc.).
        trend_lookback_start = _utcnow() - timedelta(hours=hours * 7)
        trend_filters = [
            Task.created_at >= trend_lookback_start,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            non_resident_infrastructure_task_filter(),
        ]
        if project_id:
            trend_filters.append(Task.project_id == project_id)
        trend_result = await session.execute(
            select(
                Task,
                Task.status_log[-1]["t"].astext.label("_projected_completed_at_ts"),
            )
            .where(*trend_filters)
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.created_at,
                    Task.completed_at,
                    Task.last_heartbeat,
                    Task.started_at,
                    Task.vm_provisioned_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    Task.context,
                    Task.latest_status_message,
                    raiseload=True,
                )
            )
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        trend_tasks: List[Task] = []
        for row in trend_result.all():
            task = row[0]
            # Set ONLY the completion-timestamp projection; do NOT call
            # _stash_projected_task_fields here because that would also set
            # _projected_first_payload={} — overwriting the correct value
            # already stashed by the terminal/approval queries above for
            # tasks that appear in both result sets (SQLAlchemy identity map
            # returns the same Python object for the same PK). Trend history
            # never reads first_payload, so leaving it as-is is safe.
            object.__setattr__(task, "_projected_completed_at_ts", row[1])
            trend_tasks.append(task)
    else:
        # SQLite path (tests): no negative-index JSON, load status_log directly.
        terminal_result = await session.execute(
            select(Task)
            .where(*terminal_filters)
            .options(_fat_load_options(include_status_log=True))
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        all_tasks.extend(terminal_result.scalars().all())

        approval_result = await session.execute(
            select(Task)
            .where(*approval_filters)
            .options(_fat_load_options(include_status_log=True))
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        all_tasks.extend(approval_result.scalars().all())

        # Lean trend history query (SQLite).
        trend_lookback_start = _utcnow() - timedelta(hours=hours * 7)
        trend_filters = [
            Task.created_at >= trend_lookback_start,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            non_resident_infrastructure_task_filter(),
        ]
        if project_id:
            trend_filters.append(Task.project_id == project_id)
        trend_result = await session.execute(
            select(Task)
            .where(*trend_filters)
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.created_at,
                    Task.completed_at,
                    Task.last_heartbeat,
                    Task.started_at,
                    Task.vm_provisioned_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    Task.context,
                    Task.latest_status_message,
                    Task.status_log,
                    raiseload=True,
                )
            )
            .limit(_DIGEST_TASK_ROW_CAP)
        )
        trend_tasks = list(trend_result.scalars().all())

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
    cancelled_tasks = [
        t
        for t in all_tasks
        if t.status == TaskStatus.CANCELLED and _derive_completed_at(t) >= window_start
    ]
    # Approval tasks use created_at (they haven't completed yet)
    # _to_naive_utc strips tz from t.created_at (TIMESTAMPTZ since 0061) for
    # comparison with naive window_start. See time_utils._to_naive_utc.
    approval_tasks = [
        t
        for t in all_tasks
        if t.requires_approval
        and t.status
        not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        and _to_naive_utc(t.created_at) >= window_start
    ]

    # --- Query active tasks (not filtered by window — they may have started before it) ---
    _active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [
        Task.status.in_(_active_statuses),
        non_resident_infrastructure_task_filter(),
    ]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_counts_result = await session.execute(
        select(Task.status, func.count()).where(*active_filters).group_by(Task.status)
    )
    active_count_by_status = {
        row_status: int(row_count or 0)
        for row_status, row_count in active_counts_result.all()
    }

    # Stale-heartbeat count: IN_PROGRESS tasks whose last_heartbeat is >5 min old.
    # Scoped to project_id when provided (same pattern as active_filters above).
    _stale_threshold = _utcnow() - timedelta(minutes=5)
    stale_filters = [
        Task.status == TaskStatus.IN_PROGRESS,
        Task.last_heartbeat < _stale_threshold,
        non_resident_infrastructure_task_filter(),
    ]
    if project_id:
        stale_filters.append(Task.project_id == project_id)
    stale_count_result = await session.execute(
        select(func.count()).where(*stale_filters)
    )
    stale_heartbeat_count = int(stale_count_result.scalar() or 0)
    if _use_projection:
        active_result = await session.execute(
            select(
                Task,
                Task.payloads[0].label("_projected_first_payload"),
            )
            .where(*active_filters)
            .order_by(Task.started_at.asc().nullsfirst())
            .limit(_ACTIVE_TASKS_SIGNAL_LIMIT)
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.title,
                    Task.started_at,
                    Task.created_at,
                    Task.completed_at,
                    Task.updated_at,
                    Task.latest_status_message,
                    Task.project_id,
                    Task.context,
                    Task.vm_provisioned_at,
                    Task.vm_spot,
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                    raiseload=True,
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
            .limit(_ACTIVE_TASKS_SIGNAL_LIMIT)
            .options(
                load_only(
                    Task.id,
                    Task.status,
                    Task.title,
                    Task.payloads,
                    Task.started_at,
                    Task.created_at,
                    Task.completed_at,
                    Task.updated_at,
                    Task.latest_status_message,
                    Task.project_id,
                    Task.context,
                    Task.vm_provisioned_at,
                    Task.vm_spot,
                    Task.previous_error_fingerprint,
                    Task.auto_remediation,
                    raiseload=True,
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
    direction_portfolio: Dict[str, Any] = {
        "active_count": 0,
        "self_sufficient_count": 0,
        "composition": [],
    }

    _digest_project: Optional[Project] = None
    if project_id:
        project = await session.get(Project, project_id)
        if project:
            _digest_project = project
            (
                direction_progress_list,
                scope_conversion_rates,
                prev_scope_conversion_rates,
                dir_id_to_text,
                _,
                direction_portfolio,
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
            select(
                Artifact.task_id,
                Artifact.artifact_type,
                Artifact.pr_url,
                func.substr(Artifact.logs, 1, 500).label("error_log_snippet"),
                Artifact.created_at,
            )
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
        for (
            task_id,
            artifact_type,
            pr_url,
            error_log_snippet,
            created_at,
        ) in artifacts_result.all():
            tid = str(task_id)
            if artifact_type == ArtifactType.PULL_REQUEST:
                if created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = pr_url or ""
            elif (
                artifact_type == ArtifactType.ERROR_LOG
                and error_log_snippet
                and tid not in error_log_map
            ):
                error_log_map[tid] = error_log_snippet

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
                "waiting_since": utc_iso(waiting_since),
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
        payload = t.first_payload
        task_type = payload.get("type", "create")
        entry = {
            "task_id": tid,
            "title": t.title or "Pending approval",
            "reason": "approval_required",
            "error_snippet": None,
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": utc_iso(waiting_since),
            "waiting_hours": waiting_hours,
            "approval_reason": t.latest_status_message,
            "touched_critical": (t.context or {}).get("touched_critical", []),
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "task_type": task_type,
        }
        # Held compute_experiment launches carry a proposed spend cap and
        # deadline in their payload — surface them so the operator can approve,
        # adjust the cap, or reject with full context.
        if task_type == "compute_experiment":
            entry["spend_cap_usd"] = payload.get("spend_cap_usd")
            entry["deadline_seconds"] = payload.get("deadline_seconds")
        needs_attention.append(entry)
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
                "failed_at": utc_iso(ts),
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

    _retry_analytics = compute_retry_analytics(
        failed_tasks=failed_tasks,
        completed_tasks=completed_tasks,
        dir_id_to_text=dir_id_to_text,
        window_start=window_start,
    )
    retry_overhead_cost_usd = _retry_analytics["retry_overhead_cost_usd"]
    retry_recovery_cost_usd = _retry_analytics["retry_recovery_cost_usd"]
    retry_waste_by_direction = _retry_analytics["retry_waste_by_direction"]
    retry_task_count = _retry_analytics["retry_task_count"]
    retry_success_count = _retry_analytics["retry_success_count"]
    avg_retries_before_failure = _retry_analytics["avg_retries_before_failure"]
    retry_success_rate_by_class = _retry_analytics["retry_success_rate_by_class"]
    retry_wasted_rate = _retry_analytics["retry_wasted_rate"]

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
                "completed_at": utc_iso(ts),
                "pr_url": pr_artifact_map.get(tid),
                "task_type": task_type,
                "direction_id": t_dir_id,
                "direction_text": t_dir_text,
                "duration_seconds": duration_seconds,
                "scope": (t.context or {}).get("scope"),
                "review_iterations": (t.context or {})
                .get("review_loop", {})
                .get("iteration", 0),
                # No-CI merge gate fall-open reason KEY (None for verified /
                # older merges). Read the same way admin.py reads it so the
                # Recent Merges timeline stays consistent with the per-task
                # Merge Test Gate panel.
                "unverified_merge_reason": (t.context or {})
                .get("review_loop", {})
                .get("unverified_merge_reason"),
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
                "started_at": utc_iso(t.started_at),
                "direction_id": at_dir_id,
                "direction_text": at_dir_text,
                "running_cost_usd": running_cost_usd,
                "scope": t_ctx.get("scope"),
                "review_iteration": review_iteration,
                "review_cap": review_cap,
            }
        )

    running_count = active_count_by_status.get(TaskStatus.IN_PROGRESS, 0)
    starting_count = active_count_by_status.get(TaskStatus.PROVISIONING, 0)
    evaluating_count = active_count_by_status.get(TaskStatus.EVALUATING, 0)
    queued_count = active_count_by_status.get(TaskStatus.QUEUED, 0)
    active_count = running_count + starting_count + evaluating_count + queued_count

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = (
        round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None
    )

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

    # Compute trend history: 7 periods (6 previous + current), oldest first.
    # Uses the separate lean trend_tasks query (7xhours lookback, narrow columns)
    # so the main all_tasks scan stays at the narrower 2xhours window.
    trend_history = compute_digest_trend_history(
        all_tasks=trend_tasks,
        hours=hours,
        now=_now,
    )

    # Outer review<->fix PR-cycle convergence verdict for the plain-language
    # briefing. Read-only telemetry surfacing: reuse the same per-window aggregate
    # and per-day trend the convergence widget shows so the narrative can state
    # whether issue-churn is falling/rising and name the dominant remaining
    # blocker. compute_narrative_summary gates the sentence on signal sufficiency
    # (>=2 trend buckets carrying churn), mirroring its dominant-failure-class
    # gating — thin windows get no convergence sentence.
    outer_loop_convergence_summary = await get_outer_loop_convergence(
        hours=hours, project_id=project_id, session=session
    )
    outer_loop_convergence_trend = await get_outer_loop_convergence_trend(
        days=7, project_id=project_id, session=session
    )

    # Durable AGENT_TIMEOUT-rate measure: read the overall current-vs-baseline
    # drift off the efficiency snapshot so the narrative can append a real
    # before/after sentence when agent-timeouts dominate (direction d-46468a41).
    # Best-effort — a missing/empty snapshot yields no sentence, never an error.
    _timeout_rate_drift: dict | None = None
    try:
        from app.efficiency_snapshot import read_baseline_with_drift

        _eff = await read_baseline_with_drift(session, project_id)
        _timeout_rate_drift = (_eff.get("agent_timeout_rate") or {}).get("overall")
    except Exception:
        _timeout_rate_drift = None

    narrative_summary = compute_narrative_summary(
        hours=hours,
        project_id=project_id,
        completed_count=completed_count,
        failed_count=failed_count,
        pending_approval_count=pending_approval_count,
        needs_attention=needs_attention,
        failure_groups=failure_groups,
        failure_taxonomy_distribution=failure_taxonomy_distribution,
        trend=trend,
        completed_delta=completed_delta,
        prev_completed_count=prev_completed_count,
        sorted_completions=sorted_completions,
        sorted_failures=sorted_failures,
        direction_progress_list=direction_progress_list,
        running_count=running_count,
        starting_count=starting_count,
        evaluating_count=evaluating_count,
        queued_count=queued_count,
        pr_count=pr_count,
        error_log_map=error_log_map,
        failed_tasks=failed_tasks,
        outer_loop_convergence_summary=outer_loop_convergence_summary,
        outer_loop_convergence_trend=outer_loop_convergence_trend,
        timeout_rate_drift=_timeout_rate_drift,
    )

    # Today's settled off-VM compute-job spend so daily_cost_usd reflects total
    # spend, not just VM runtime. Mirrors the today_start the pure function uses.
    from app.compute_jobs import get_recent_terminal_compute_spend_usd

    _cost_now = _utcnow()
    _today_start = _cost_now.replace(hour=0, minute=0, second=0, microsecond=0)
    compute_job_daily_cost_usd = await get_recent_terminal_compute_spend_usd(
        session, project_id=project_id, since=_today_start
    )

    _cost_econ = compute_digest_cost_economics(
        all_tasks=all_tasks,
        direction_progress_list=direction_progress_list,
        window_start=window_start,
        now=_cost_now,
        daily_cost_ceiling_usd=float(_runtime_config.get("daily_cost_ceiling_usd", 0)),
        compute_job_daily_cost_usd=compute_job_daily_cost_usd,
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
    # Retired: pipeline_autonomous_prs_merged / pipeline_assisted_prs_merged /
    # pipeline_operator_action_count — consumer-less digest duplicates of the
    # per-direction autonomous_prs_merged / assisted_prs_merged /
    # operator_action_count fields (already surfaced in DirectionCard).
    overall_type_distribution = _cost_econ["overall_type_distribution"]
    pipeline_filter_funnel = _cost_econ["pipeline_filter_funnel"]
    # Per-effort-tier / per-actual-model LLM efficiency rollup over the window's
    # finished (terminal) tasks. Additive — answers whether effort-tier scaling
    # earns its money in landed PRs. Reuses the shared LLM pricing so its totals
    # reconcile with the per-model cost surface.
    effort_model_efficiency = compute_effort_model_efficiency(
        completed_tasks + failed_tasks + cancelled_tasks,
        runtime_config=_runtime_config,
    )
    # Provider routing A/B verdict derived from the per-model efficiency breakdown.
    # Injected into the same payload so live consumers see ADOPT/REJECT/INSUFFICIENT_DATA
    # alongside the per-model rows that informed it.
    effort_model_efficiency["provider_routing_verdict"] = (
        compute_provider_routing_verdict(effort_model_efficiency["by_model"])
    )
    # Per-provider routing share derived from the same by-model rows — shows
    # task count and verified merge rate by provider at a glance.
    effort_model_efficiency["provider_routing_share"] = (
        compute_provider_routing_share(effort_model_efficiency["by_model"])
    )
    # Cost-telemetry reconciliation: recompute LLM cost and cache-hit rate via a
    # structurally independent path to surface whether the primary cost telemetry
    # is trustworthy within declared tolerance. Injected into the efficiency dict
    # and surfaced as a top-level key for the EffortEfficiencyPanel trust indicator.
    _reconciliation_window = completed_tasks + failed_tasks + cancelled_tasks
    effort_model_efficiency["cost_telemetry_reconciliation"] = _without_per_task(
        reconcile_llm_cost(_reconciliation_window).asdict()
        if _reconciliation_window
        else None
    )
    _provider_recon = (
        reconcile_provider_reported_usage(_reconciliation_window)
        if _reconciliation_window
        else None
    )
    if _provider_recon is not None and not _provider_recon.provider_reported_within_tolerance:
        if _provider_recon.task_count > 0:
            logger.warning(
                "cost_telemetry_reconciliation provider_reported FAILED — "
                "delta=%.4f (tolerance=%.4f) over %d task(s) with provider data",
                _provider_recon.provider_reported_delta,
                _provider_recon.provider_reported_tolerance,
                _provider_recon.task_count,
            )
        else:
            # A zero-coverage window is no longer silently green: its tolerance
            # verdict is unproven, so surface it instead of letting the
            # task_count > 0 guard swallow the non-passing result.
            logger.warning(
                "cost_telemetry_reconciliation provider_reported has NO verified tasks — "
                "%d task(s) in window carried no provider-reported usage; "
                "the tolerance verdict is unproven, not passing",
                _provider_recon.total_task_count,
            )
    effort_model_efficiency["cost_telemetry_reconciliation_provider_reported"] = (
        _without_per_task(_provider_recon.asdict())
        if _provider_recon is not None
        else None
    )

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
                        _ets = _coerce_naive_utc(
                            datetime.fromisoformat(_entry.get("timestamp", ""))
                        )
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
                        _ets = _coerce_naive_utc(
                            datetime.fromisoformat(_entry.get("timestamp", ""))
                        )
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
    edges_trend: Optional[Dict[str, Any]] = None
    self_review_prediction_accuracy_trends: Optional[Dict[str, Any]] = None
    sr_regression_risk_test_signal_trends: Optional[Dict[str, Any]] = None
    sr_false_attestation_trends: Optional[Dict[str, Any]] = None
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
            _sr_data = await read_project_sr_analytics(session, _proj)
            _sr_trends = _sr_data.get("sr_catch_rate_trends") or {}
            if _sr_trends:
                self_review_prediction_accuracy_trends = (
                    _serialize_sr_catch_rate_trends(_sr_trends)
                )
            _sr_rrts = _sr_data.get("sr_regression_risk_test_signal_trends")
            if _sr_rrts:
                sr_regression_risk_test_signal_trends = _sr_rrts
            _sft = _sr_data.get("sr_false_attestation_trends")
            if _sft:
                sr_false_attestation_trends = _sft
            # Read the newest terminal cycle from the StrategicCycle table.
            # Terminal = outcome not in ACTIVE_OUTCOMES (completed, failed_*, stalled_*).
            # vision._strategic_cycle_history was retired in migration 0047.
            _sc_stmt = (
                select(StrategicCycle)
                .where(
                    StrategicCycle.project_id == project_id,
                    StrategicCycle.outcome.not_in(
                        list(_strategic_cycle.ACTIVE_OUTCOMES)
                    ),
                )
                .order_by(desc(StrategicCycle.phase1_started_at))
                .limit(1)
            )
            _sc_row = (await session.execute(_sc_stmt)).scalars().first()
            if _sc_row is not None:
                latest_cycle = {
                    "cycle_id": _sc_row.id,
                    "started_at": utc_iso(_sc_row.phase1_started_at),
                    "completed_at": utc_iso(_sc_row.phase2_completed_at),
                    "outcome": _sc_row.outcome,
                    "directions_created": int(_sc_row.directions_created or 0),
                    "directions_retired": int(_sc_row.directions_retired or 0),
                    "vision_updated": bool(_sc_row.vision_updated),
                    "summary_text": _sc_row.summary_text,
                }
                _ms = _sc_row.memo_stats()
                latest_cycle[CYCLE_META_MEMOS_AVAILABLE] = _ms[
                    CYCLE_META_MEMOS_AVAILABLE
                ]
                latest_cycle[CYCLE_META_MEMOS_CITED] = _ms[CYCLE_META_MEMOS_CITED]
                # Compact strategic-judgment-challenge summary when this
                # cycle carried a challenge record; omit entirely for
                # legacy cycles so absence never reads as a failure.
                _challenge_record = (_sc_row.cycle_metadata or {}).get(
                    "strategic_judgment_challenge"
                )
                _challenge_summary = strategic_challenge_summary(_challenge_record)
                if _challenge_summary is not None:
                    latest_cycle["strategic_judgment_challenge"] = _challenge_summary
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
            # Edges-considered cycle-over-cycle trend persisted by compute_edges_trend.
            _et = (
                await _read_telemetry_or_doc(
                    session, _proj.id, _proj.design_doc, "edges_trend"
                )
                or {}
            )
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

    release_qualifier_infra_metrics = await compute_release_qualifier_infra_metrics(
        session=session,
        project_id=project_id,
        window_start=window_start,
    )

    _remediation_result = await compute_remediation_and_patterns(
        session=session,
        project_id=project_id,
        window_start=window_start,
        failed_tasks=failed_tasks,
        all_tasks=all_tasks,
        dir_id_to_text=dir_id_to_text,
    )
    remediation_funnel = _remediation_result["remediation_funnel"]
    investigation_conversion = _remediation_result["investigation_conversion"]
    recurring_patterns = _remediation_result["recurring_patterns"]
    early_warnings = _remediation_result["early_warnings"]

    # --- Anomaly-driven pipeline adjustment snapshot ---
    anomaly_adjustment_summary = compute_anomaly_adjustment_summary()

    # --- Dependency-health observability (parked blockers + duplicated
    # foundations) — read-only signals so the two otherwise-invisible failure
    # modes the foundation-resolution work prevents become measurable. ---
    dependency_health_signals = await compute_dependency_health_signals(
        session, project_id
    )

    # --- Park/wake operational health (read-only) — PARKED population + wake
    # delivery backlog for the park-and-wake primitive. Distinct from the
    # parked_blockers block above (escalated BLOCKED tasks); this grounds
    # "park/wake is healthy" in observed PARKED/FIRED runtime state. ---
    park_wake_health = await compute_park_wake_health(session, project_id)

    # --- Fleet-wide no-CI merge-gate verification health (read-only) — surfaces
    # whether merges across the fleet actually ran real tests vs. fell open to an
    # UNVERIFIED merge, which fall-open reason dominates, and how many projects
    # have a degraded gate. NEVER influences a merge decision. ---
    merge_gate_fleet_health = await compute_merge_gate_fleet_health(
        session, project_id, window_start=window_start
    )

    _review_quality = compute_review_quality_metrics(completed_tasks + failed_tasks)
    _self_review_skip_rate = _compute_sr_skip_rate(completed_tasks + failed_tasks)
    _sr_disallowed_blind_spot_rate = _compute_sr_disallowed_blind_spot_rate(
        completed_tasks + failed_tasks
    )
    _first_attempt_success_rate = _compute_first_attempt_breakdowns(
        completed_tasks + failed_tasks,
        prev_terminal_tasks=prev_completed_tasks + prev_failed_tasks,
    )

    # --- Capability trial verdict (write-through side-effect) ---
    # Persist a durable adopt/reject/continue-trial verdict on the project.  Only
    # runs when a specific project is in scope (no-op for cross-project digests).
    #
    # The DECISION basis is CUMULATIVE, not windowed: at realistic task volume the
    # holdback (without-)arm never reaches the min-N threshold inside any single
    # digest window, so a windowed basis emits continue-trial forever.  We instead
    # evaluate over ALL terminal tasks carrying the trial's arm flag since the trial
    # began (a bounded aggregate count that does NOT widen the digest's own task
    # scan).  The WINDOWED per-arm rates/counts in ``_first_attempt_success_rate``
    # (``trial_breakdowns``, ``with_{cap}_n``, …) are left untouched for dashboard
    # display — only the decision basis changes.
    _augmented_verdicts: dict = {}
    if _digest_project is not None:
        from app.time_utils import _utcnow as _tv_utcnow  # noqa: PLC0415

        _verdict_evaluated_at = _tv_utcnow().isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
        _pending_trial_verdicts: list[tuple[str, dict[str, Any]]] = []
        # Collected gate-integrity alert detail (if any) — persisted durably in
        # the write-owned helper alongside the verdicts, NOT on the request
        # session, which the caller may roll back.
        _gate_alert_detail: str | None = None
        for _td in get_trial_definitions():
            _cap = _td["cap_name"]
            # Lane pilot uses its own multi-axis evaluator (KILL/ADOPT thresholds
            # differ from the generic lift/min_n model) — skip it here and handle
            # it in the dedicated block below.
            if _cap == LANE_PILOT_CAP_NAME:
                continue
            _accum = await compute_trial_arm_accumulation(
                session,
                project_id=project_id,
                context_key=_td["context_key"],
                assignment_key=_td.get("assignment_key"),
            )
            _cost = await compute_trial_arm_cost_delta(
                session,
                project_id=project_id,
                context_key=_td["context_key"],
                assignment_key=_td.get("assignment_key"),
            )
            _verdict_entry = evaluate_trial_verdict(
                lift=_accum["lift"],
                n_with=_accum["with_n"],
                n_without=_accum["without_n"],
                window_hours=hours,
                evaluated_at=_verdict_evaluated_at,
                config=_td["thresholds"],
                accumulation_basis="cumulative_terminal_arm_tasks",
                arm_composition=_accum.get("arm_composition"),
                receipt_only_trial=(_td.get("assignment_key") is None),
                per_arm_cost=_cost,
            )
            _pending_trial_verdicts.append((_cap, _verdict_entry))

        # ── Lane pilot: dedicated multi-axis verdict path ─────────────────────
        # Uses compute_lane_pilot_aggregates -> evaluate_lane_pilot_verdict which
        # checks lane-specific KILL/ADOPT thresholds (merge-rate floor, cost
        # ceiling, gate-integrity violations, checkpoint-loss rate, futility-N).
        # A KILL verdict degrades spawning + force-expires reservations; an
        # immediate_alert verdict posts a BLOCKER ProjectMessage.
        from app.review_fix_lane_pilot import (  # noqa: PLC0415
            apply_lane_pilot_kill_in_transaction,
            compute_lane_pilot_aggregates,
            compute_lane_arm_time_medians,
            is_review_fix_lane_pilot_enabled,
            is_review_fix_lane_pilot_shadow_mode_enabled,
        )

        if project_id is not None:
            _lane_aggs = await compute_lane_pilot_aggregates(session, project_id)
            # The lane trial can only accrue intent-to-treat arm traffic when the
            # pilot is enabled OR in shadow mode — those are the exact conditions
            # under which record_lane_arm_assignment writes the arm key.  When
            # neither holds the pilot is awaiting enablement, so the verdict is
            # annotated distinctly from generic insufficient_data and the debt
            # path defers to the dormant-knob obligation instead of duplicating it.
            _lane_pilot_active = bool(
                is_review_fix_lane_pilot_enabled()
                or is_review_fix_lane_pilot_shadow_mode_enabled()
            )
            _lane_verdict = evaluate_lane_pilot_verdict(
                _lane_aggs, pilot_active=_lane_pilot_active
            )
            _lane_verdict["evaluated_at"] = _verdict_evaluated_at
            # Materialize a compact lane-vs-relay comparison: relay medians from
            # the most recent closed version's persisted baseline plus lane-arm
            # medians computed live, plus deltas.  Only bounded scalars are
            # stored — no per-PR row arrays or per-task chains.
            try:
                from app.lane_baseline_snapshot import (  # noqa: PLC0415
                    read_version_lane_baseline_preferring_table,
                )
                from app.models import ReleaseVersion as _ReleaseVersion  # noqa: PLC0415

                _latest_rv = (
                    (
                        await session.execute(
                            select(_ReleaseVersion)
                            .where(
                                _ReleaseVersion.project_id == project_id,
                                _ReleaseVersion.status.in_(["promoted", "abandoned"]),
                            )
                            .order_by(desc(_ReleaseVersion.version_number))
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                if _latest_rv is not None and _digest_project is not None:
                    _rb = await read_version_lane_baseline_preferring_table(
                        session, _digest_project, _latest_rv.version_number
                    )
                    if _rb is not None:
                        _relay_m = _rb.get("relay_medians") or {}
                        _relay_fields = {
                            "wall_clock_seconds": _relay_m.get("wall_clock_seconds"),
                            "active_seconds": _relay_m.get("active_seconds"),
                            "gap_seconds": _relay_m.get("gap_seconds"),
                            "task_count": _relay_m.get("task_count"),
                            "pr_count": _relay_m.get("pr_count", 0),
                            "fix_active_seconds": _relay_m.get("fix_active_seconds"),
                            "review_active_seconds": _relay_m.get(
                                "review_active_seconds"
                            ),
                        }
                        _lane_m = await compute_lane_arm_time_medians(
                            session, project_id
                        )
                        _deltas: dict[str, Any] = {}
                        for _mf in (
                            "wall_clock_seconds",
                            "active_seconds",
                            "gap_seconds",
                            "task_count",
                            "fix_active_seconds",
                            "review_active_seconds",
                        ):
                            _rv, _lv = _relay_fields.get(_mf), _lane_m.get(_mf)
                            _deltas[_mf] = (
                                (_lv - _rv)
                                if (_rv is not None and _lv is not None)
                                else None
                            )
                        _lane_verdict["lane_relay_comparison"] = {
                            "source_version": {
                                "version_number": _latest_rv.version_number,
                                "captured_at": _rb.get("captured_at"),
                                "terminal_status": _rb.get("terminal_status"),
                            },
                            "relay": _relay_fields,
                            "lane": _lane_m,
                            "deltas": _deltas,
                        }
            except Exception:
                logger.warning(
                    "[LANE_PILOT] failed to compute lane_relay_comparison",
                    exc_info=True,
                )
            if _lane_verdict.get("verdict") == "KILL":
                await apply_lane_pilot_kill_in_transaction(
                    session,
                    project_id,
                    verdict_entry=_lane_verdict,
                    reason=_lane_verdict.get("verdict_reason", "digest_eval"),
                    alert_detail=(
                        str(_lane_verdict.get("verdict_reason", ""))
                        if _lane_verdict.get("immediate_alert")
                        else None
                    ),
                )
            elif _lane_verdict.get("immediate_alert"):
                # Route the alert through the durable write-owned helper below
                # instead of the request session, which the caller may roll back.
                _gate_alert_detail = str(_lane_verdict.get("verdict_reason", ""))
            _pending_trial_verdicts.append((LANE_PILOT_CAP_NAME, _lane_verdict))

        # Persist ALL collected trial verdicts AND any gate-integrity alert in an
        # explicit write-owned transaction whose commit is INDEPENDENT of the
        # request session. On the request path the caller's session is closed
        # without committing; on the precompute path the request session's commit
        # is contingent on the L2 cache write (write_shared) succeeding. The
        # helper re-fetches the project fresh inside its own session so
        # record_trial_verdict's KILL-survival / self-revert guards see current
        # committed state — and so a lane-pilot KILL already committed by
        # apply_lane_pilot_kill_in_transaction is never clobbered by a stale
        # re-record of the early-fetched _digest_project row.
        _persisted_verdicts: dict = {}
        try:
            _persisted_verdicts = await persist_trial_verdicts_in_transaction(
                session,
                project_id,
                pending_verdicts=_pending_trial_verdicts,
                gate_alert_detail=_gate_alert_detail,
            )
        except Exception:
            logger.warning(
                "[CAPABILITY_TRIAL] failed to durably persist verdicts", exc_info=True
            )
            _persisted_verdicts = {}

        # --- Per-trial accrual projection (persisted onto verdict entries) ---
        # Augment the verdict entries returned in the response with a recent
        # assigned-arm accrual rate and a projection of how many days remain until
        # BOTH arms' assigned subpopulation reaches the min-N decision threshold at
        # the current rate. Only assignment-based trials (an ``assignment_key``) have
        # a randomized holdback arm to project; receipt-only trials get no projection.
        #
        # This is read-only display compute. We build the response payload from
        # ``_persisted_verdicts`` — the snapshot returned by the durable helper,
        # which mirrors EXACTLY what was committed (after record_trial_verdict's
        # KILL-survival / self-revert guards ran against fresh committed state).
        # We deliberately do NOT read off the early-fetched ``_digest_project``
        # row: it is stale relative to the helper's commit, and mutating it would
        # risk a later autoflush persisting ``accrual_projection`` to the JSONB
        # column. Instead we build a response-only copy with the projection
        # grafted on.
        _accrual_window_days = 7
        _stored_verdicts = _persisted_verdicts
        _augmented_verdicts = {
            _k: (dict(_v) if isinstance(_v, dict) else _v)
            for _k, _v in _stored_verdicts.items()
        }
        for _td in get_trial_definitions():
            _assignment_key = _td.get("assignment_key")
            if not _assignment_key or _td["cap_name"] == LANE_PILOT_CAP_NAME:
                continue
            _cap = _td["cap_name"]
            _verdict_entry = _augmented_verdicts.get(_cap)
            if not isinstance(_verdict_entry, dict):
                continue
            _accrual = await compute_trial_arm_accrual(
                session,
                project_id=project_id,
                assignment_key=_assignment_key,
                window_days=_accrual_window_days,
            )
            _default_holdback_fraction = float(
                _runtime_config.get(f"{_cap}_holdback_fraction", 0.0)
            )
            _holdback_fraction = get_effective_holdback_fraction(
                _stored_verdicts, _cap, _default_holdback_fraction
            )
            _with_daily = _accrual["with_7d"] / _accrual_window_days
            _without_daily = _accrual["without_7d"] / _accrual_window_days
            _thresholds = _verdict_entry.get("thresholds") or {}
            _min_n = int(_thresholds.get("min_n_per_arm", 0))
            _arm_comp = _verdict_entry.get("arm_composition") or {}
            _with_assigned = int(_arm_comp.get("with_assigned") or 0)
            _without_assigned = int(_arm_comp.get("without_assigned") or 0)
            # Days for the slower-filling assigned arm to reach min_n at its recent rate.
            _with_days = (
                (_min_n - _with_assigned) / _with_daily if _with_daily > 0 else None
            )
            _without_days = (
                (_min_n - _without_assigned) / _without_daily
                if _without_daily > 0
                else None
            )
            _with_remaining = max(_with_days, 0.0) if _with_days is not None else None
            _without_remaining = (
                max(_without_days, 0.0) if _without_days is not None else None
            )
            _days_to_min_n: "float | None"
            if _with_assigned >= _min_n and _without_assigned >= _min_n:
                _days_to_min_n = 0.0
            elif _with_remaining is None or _without_remaining is None:
                # At least one arm has a zero recent rate and is still under min_n —
                # it can never reach threshold at the current accrual, so no estimate.
                _days_to_min_n = None
            else:
                _days_to_min_n = round(max(_with_remaining, _without_remaining), 1)
            # Derive total daily throughput to project days-to-threshold at
            # alternative holdback fractions so operators can see the time-to-verdict
            # tradeoff before adjusting holdback_fraction.
            if 0 < _holdback_fraction < 1:
                _total_daily = _with_daily / (1.0 - _holdback_fraction)
            elif _holdback_fraction == 0:
                _total_daily = _with_daily
            elif _holdback_fraction == 1:
                _total_daily = _without_daily
            else:
                _total_daily = 0.0
            _alt_fractions = [0.1, 0.3, 0.5]
            _alternative_projections: "dict[str, float | None]" = {}
            for _alt_frac in _alt_fractions:
                if _total_daily <= 0:
                    _alternative_projections[str(_alt_frac)] = None
                    continue
                _alt_with_daily = _total_daily * (1.0 - _alt_frac)
                _alt_without_daily = _total_daily * _alt_frac
                _alt_with_days = (
                    (_min_n - _with_assigned) / _alt_with_daily
                    if _alt_with_daily > 0
                    else None
                )
                _alt_without_days = (
                    (_min_n - _without_assigned) / _alt_without_daily
                    if _alt_without_daily > 0
                    else None
                )
                _alt_with_rem = (
                    max(_alt_with_days, 0.0) if _alt_with_days is not None else None
                )
                _alt_without_rem = (
                    max(_alt_without_days, 0.0)
                    if _alt_without_days is not None
                    else None
                )
                if _with_assigned >= _min_n and _without_assigned >= _min_n:
                    _alternative_projections[str(_alt_frac)] = 0.0
                elif _alt_with_rem is None or _alt_without_rem is None:
                    _alternative_projections[str(_alt_frac)] = None
                else:
                    _alternative_projections[str(_alt_frac)] = round(
                        max(_alt_with_rem, _alt_without_rem), 1
                    )
            _verdict_entry["accrual_projection"] = {
                "with_7d": _accrual["with_7d"],
                "without_7d": _accrual["without_7d"],
                "window_days": _accrual_window_days,
                "holdback_fraction": _holdback_fraction,
                "days_to_min_n": _days_to_min_n,
                "alternative_projections": _alternative_projections,
            }

        # Stamp is_stalled on every augmented verdict entry and collect stalled ones.
        _stalled_trials: list[tuple[str, dict[str, Any]]] = []
        for _cap, _entry in _augmented_verdicts.items():
            if not isinstance(_entry, dict):
                continue
            _stalled = is_trial_stalled(_entry)
            _entry["is_stalled"] = _stalled
            if _stalled:
                _stalled_trials.append((_cap, _entry))

        # Persist accrual-projection snapshots onto the durable verdict entries so
        # strategic readers can observe whether a trial can reach min_n at current
        # throughput without going through the admin digest endpoint.
        _projections_to_persist: list[tuple[str, dict, bool]] = [
            (_cap, _entry["accrual_projection"], bool(_entry.get("is_stalled")))
            for _cap, _entry in _augmented_verdicts.items()
            if isinstance(_entry, dict) and "accrual_projection" in _entry
        ]
        if _projections_to_persist:
            try:
                await persist_accrual_projections_in_transaction(
                    session,
                    project_id,
                    projections=_projections_to_persist,
                )
            except Exception:
                logger.warning(
                    "[CAPABILITY_TRIAL] failed to persist accrual projections",
                    exc_info=True,
                )

        if _stalled_trials:
            try:
                await persist_stalled_trial_notices_in_transaction(
                    session,
                    project_id,
                    stalled=_stalled_trials,
                )
            except Exception:
                logger.warning(
                    "[CAPABILITY_TRIAL] failed to persist stalled-trial notices",
                    exc_info=True,
                )

        # Escalate ambiguous_lift trials that have been inconclusive past the dwell threshold.
        _escalated_trials: list[tuple[str, dict[str, Any]]] = []
        for _cap, _entry in _augmented_verdicts.items():
            if not isinstance(_entry, dict):
                continue
            if is_ambiguous_lift_escalation_needed(_entry):
                _escalated_trials.append((_cap, _entry))
        if _escalated_trials:
            try:
                await persist_ambiguous_lift_escalations_in_transaction(
                    session,
                    project_id,
                    escalated=_escalated_trials,
                )
            except Exception:
                logger.warning(
                    "[CAPABILITY_TRIAL] failed to persist ambiguous-lift escalation notices",
                    exc_info=True,
                )

    _first_review_approval_rate = _compute_first_review_approval_breakdowns(
        completed_tasks + failed_tasks
    )

    # Migration-serialization-slot contention (read-only) — surfaces whether a
    # migration task currently holds the per-project slot and for how long, how
    # many schema-touching tasks are deferred waiting, and the longest current
    # wait. Counts QUEUED/BLOCKED migration-deferred siblings that neither the
    # throughput-only migration_health nor the escalated-only parked-blocker
    # signal can see. Never influences serialization.
    migration_slot_contention = await compute_migration_slot_contention(
        session, project_id
    )

    migration_health = compute_migration_health(
        completed_tasks,
        failed_tasks,
        error_log_map=error_log_map,
        active_tasks=active_tasks_all,
        migration_slot_contention=migration_slot_contention,
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
        "sr_false_attestation_trends": sr_false_attestation_trends,
        "self_review_skip_rate": _self_review_skip_rate,
        "sr_disallowed_blind_spot_rate": _sr_disallowed_blind_spot_rate,
        "review_accuracy": _review_quality["review_accuracy"],
        # Retired: edges_considered_metrics digest key was emitted without a
        # named reader (edges_trend below IS consumed by ReviewQualitySection).
        # _compute_edges_considered_metrics computation remains live (used by
        # pipeline_health.py and re-exported for external importers).
        "edges_trend": edges_trend,
        "merge_early_exit_count": merge_early_exit_count,
        "generated_at": utc_iso(_utcnow()),
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "pending_approval_count": len(approval_tasks),
        "research_cycles": research_cycles,
        "prs_opened": pr_count,
        "active_count": active_count,
        "running_count": running_count,
        "starting_count": starting_count,
        "evaluating_count": evaluating_count,
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
        "direction_portfolio": direction_portfolio,
        "overall_product_ratio": overall_product_ratio,
        "median_cost_per_merged_pr": median_cost_per_merged_pr,
        "cost_per_merge_by_type": cost_per_merge_by_type,
        "median_cost_per_merge_by_type": _median_cpm_by_type,
        "effort_model_efficiency": effort_model_efficiency,
        "provider_routing_share": effort_model_efficiency.get(
            "provider_routing_share"
        ),
        "cost_telemetry_reconciliation": effort_model_efficiency.get(
            "cost_telemetry_reconciliation"
        ),
        "cost_telemetry_reconciliation_provider_reported": effort_model_efficiency.get(
            "cost_telemetry_reconciliation_provider_reported"
        ),
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
        # Retired: pipeline_autonomous_prs_merged / pipeline_assisted_prs_merged /
        # pipeline_operator_action_count — consumer-less duplicates of per-direction
        # outcome fields (see retirement note at the extraction site above).
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
        "dependency_health_signals": dependency_health_signals,
        "park_wake_health": park_wake_health,
        "merge_gate_fleet_health": merge_gate_fleet_health,
        "migration_health": migration_health,
        "migration_slot_contention": migration_slot_contention,
        "prediction_calibration_factor": prediction_calibration["factor"]
        if prediction_calibration
        else None,
        "prediction_calibration_note": prediction_calibration["note"]
        if prediction_calibration
        else None,
        "prediction_calibration_sample_count": prediction_calibration["sample_count"]
        if prediction_calibration
        else None,
        "release_qualifier_infra_metrics": release_qualifier_infra_metrics,
        "capability_adoption_verdicts": _augmented_verdicts
        if _digest_project is not None
        else {},
        "stale_heartbeat_count": stale_heartbeat_count,
    }
