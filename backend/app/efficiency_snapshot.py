"""Weekly efficiency-baseline snapshot persistence.

Stores per-project efficiency axes (cost per merged PR, VM cold-start p50,
VM reuse rate, median API calls per task, median review iterations) inside
``project.design_doc["efficiency_baseline"]``.  Mirrors the storage pattern
used by ``viewport_coverage_snapshot`` / ``sr_catch_rate_trends`` so we get
the same round-trip cost (one JSONB column) and the same operator semantics.

The first snapshot doubles as the baseline (the 2026-04-05 reference point
predates this telemetry and is not recoverable). Subsequent snapshots append
to ``snapshots`` only — the baseline is immutable until an operator clears
``project.design_doc["efficiency_baseline"]`` manually.

Drift is reported as percentage delta from baseline so operators can read
each axis as ``current_value`` / ``baseline_value`` / ``drift_pct``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import statistics
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.admin_helpers import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.landed_signal import (
    has_merge_unblock_signal,
    is_merged_completion,
    is_verified_merge,
)
from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    PoolIdleCostLedger,
    PoolIdleSpan,
    PoolVm,
    PoolVmState,
    Project,
    ProjectEfficiencySnapshot,
    Task,
    TaskStatus,
    _utcnow,
    get_task_current_attempt_vm_cost_usd,
    get_task_total_llm_totals,
    get_task_total_vm_cost_usd,
)
from app.cost_telemetry_reconciliation import reconcile_llm_cost
from app.llm_cost_attribution import (
    CACHE_CREATION_INPUT_RATE_MULTIPLIER,  # noqa: F401 — re-exported for callers
    LLM_CACHE_READ_INPUT_PRICING,  # noqa: F401 — re-exported for callers
    CACHE_READ_INPUT_RATE_MULTIPLIER,  # noqa: F401 — re-exported for callers
    LLM_TOKEN_PRICING,
    SELF_REPAIR_BURN_SHARE_KEY,
    _UNPRICED_COMBOS_CAP,
    _compact_llm_cost_attribution,
    _compute_llm_cost_per_merged_pr,
    _compute_llm_cost_per_verified_task,
    _compute_llm_cost_pricing_coverage,
    _price_llm_attempt,
    _task_llm_attempts,
    _task_llm_cost,
    compute_llm_cost_breakdown,
    compute_llm_cost_by_failure_class,  # noqa: F401 — re-exported for callers
    compute_llm_cost_by_task_type,
    compute_pr_rework_chain_breakdown,
)
from app.project_telemetry import (
    insert_efficiency_snapshot,
    read_recent_efficiency_snapshots,
)
from app.task_capabilities import is_pr_eligible_task_type
from app.task_locking import project_lock
from app.vm_cost_rates import idle_cost_usd

router = APIRouter()
agent_router = APIRouter(prefix="/api/agent", tags=["agent"])

# ---------------------------------------------------------------------------
# Cold-start stage breakdown (per-stage p50 decomposition)
# ---------------------------------------------------------------------------
# Each entry is (source_key, dest_key, metric_label). "vm_dispatched_at" is
# resolved from Task.vm_dispatched_at; all others are ISO strings written into
# task.context["_cold_start_stages"] by agent_heartbeat and claim_task.
_COLD_START_STAGE_PAIRS: tuple[tuple[str, str, str], ...] = (
    # Queue-wait: time dispatch spends waiting for a provisioning slot to open.
    # Populated by compute.py after _acquire_provision_slot succeeds. Absent on
    # older tasks — stage value is None in that case, not an error.
    ("vm_dispatched_at", "provision_slot_acquired_at", "dispatch_to_slot_ms"),
    ("vm_dispatched_at", "beacon_a_at", "dispatch_to_creds_ms"),
    ("beacon_a_at", "beacon_b_at", "creds_to_env_ms"),
    ("beacon_b_at", "beacon_c_at", "env_to_payload_ms"),
    ("beacon_c_at", "beacon_d_at", "payload_to_systemctl_ms"),
    ("beacon_d_at", "startup_complete_at", "systemctl_to_startup_ms"),
    ("startup_complete_at", "claim_at", "startup_to_claim_ms"),
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared key constants (writer + reader must agree on these).
# A prior incident (fingerprint 691593a5c0a7 in admin_digest.py) was caused
# by snake_case vs human-category vocabulary mismatch — keep the strings
# centralised so the same constant flows through compute -> write -> read.
# ---------------------------------------------------------------------------
AXIS_COST_PER_MERGED_PR = "cost_per_merged_pr"
AXIS_VM_COLD_START_P50_MS = "vm_cold_start_p50_ms"
AXIS_VM_REUSE_RATE = "vm_reuse_rate"
AXIS_MEDIAN_API_CALLS_PER_TASK = "median_api_calls_per_task"
AXIS_MEDIAN_REVIEW_ITERATIONS = "median_review_iterations"
AXIS_LLM_COST_PER_MERGED_PR = "llm_cost_per_merged_pr"
AXIS_COST_PER_VERIFIED_TASK = "cost_per_verified_task"
AXIS_LLM_COST_PER_VERIFIED_TASK = "llm_cost_per_verified_task"
AXIS_LANE_COST_PER_MERGED_PR = "lane_cost_per_merged_pr"
AXIS_LANE_WARM_RESUME_RATE = "lane_warm_resume_rate"
AXIS_CHECKPOINT_LOSS_RATE = "checkpoint_loss_rate"
AXIS_PROMPT_STATIC_BYTES = "prompt_static_bytes"
AXIS_LLM_CACHE_HIT_RATE = "llm_cache_hit_rate"
AXIS_TOTAL_COST_PER_VERIFIED_TASK = "total_cost_per_verified_task"
# Prompt-cache lever's DIRECT effect signal: the fleet-level rollup of the
# per-task-type ``cache_read_share`` already surfaced by
# ``compute_llm_cost_by_task_type``. Kept distinct from AXIS_LLM_CACHE_HIT_RATE
# (which is computed independently from raw attempts) so the fleet number is a
# token-weighted aggregate that reconciles EXACTLY with the per-task-type cost
# table operators read, giving the cost-reduction direction a baselined
# denominator for the prompt-restructuring lever's before/after measurement.
AXIS_PROMPT_CACHE_READ_SHARE = "prompt_cache_read_share"

# Minimum verified-merge count before drift_pct is emitted for
# AXIS_TOTAL_COST_PER_VERIFIED_TASK. Below this threshold the ratio is too noisy
# (small denominator) and drift_pct is suppressed to None to avoid phantom
# regression signals from early low-N windows.
VERIFIED_TASK_DRIFT_MIN_COUNT = 20

AXIS_REWORK_COST_SHARE = "rework_cost_share"
AXIS_SALVAGE_COUNT = "salvage_count"
AXIS_SALVAGE_MERGE_RATE = "salvage_merge_rate"
AXIS_ACCEPTANCE_PROVEN_RATE = "acceptance_proven_rate"
AXIS_ACCEPTANCE_SATISFIED_RATE = "acceptance_satisfied_rate"

# Guard threshold for prompt_static_bytes: alert when static prompt size exceeds
# this many source characters. Set well above the current measured value (~31k chars)
# to catch significant growth while avoiding false positives on minor edits.
PROMPT_STATIC_BYTES_THRESHOLD = 55_000
FORMAT_COMPLIANCE_KEY = "format_compliance"
FORMAT_COMPLIANCE_RATE_KEY = "format_compliance_rate"
SELF_REPAIR_SUCCESS_RATE_KEY = "self_repair_success_rate"

# Minimum repair_attempts in the current snapshot window before self_repair_success_rate
# drift is meaningful. Below this threshold the ratio is too noisy (rare-event denominator)
# and drift_pct is suppressed to None to avoid phantom regression signals.
SELF_REPAIR_MIN_ATTEMPTS = 10
# Guard threshold for self_repair_burn_share: alert when repair tokens consume
# more than this fraction of total LLM tokens. When repair-call overhead exceeds
# the ceiling, it signals a drift in self-repair effectiveness and should trigger
# an investigation of failure patterns that require multiple repair attempts.
SELF_REPAIR_BURN_SHARE_THRESHOLD = 0.20

ALL_AXES: tuple[str, ...] = (
    AXIS_COST_PER_MERGED_PR,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_LLM_COST_PER_MERGED_PR,
    AXIS_COST_PER_VERIFIED_TASK,
    AXIS_LLM_COST_PER_VERIFIED_TASK,
    AXIS_LANE_COST_PER_MERGED_PR,
    AXIS_LANE_WARM_RESUME_RATE,
    AXIS_CHECKPOINT_LOSS_RATE,
    AXIS_PROMPT_STATIC_BYTES,
    AXIS_LLM_CACHE_HIT_RATE,
    AXIS_TOTAL_COST_PER_VERIFIED_TASK,
    AXIS_PROMPT_CACHE_READ_SHARE,
    AXIS_REWORK_COST_SHARE,
    AXIS_SALVAGE_COUNT,
    AXIS_SALVAGE_MERGE_RATE,
    AXIS_ACCEPTANCE_PROVEN_RATE,
    AXIS_ACCEPTANCE_SATISFIED_RATE,
)

# ---------------------------------------------------------------------------
# Durable axis-consumer manifest (governance pattern — mirrors
# RESEARCH_HISTORY_METRIC_MANIFEST in direction_outcomes.py).
#
# Every ALL_AXES member MUST have an entry. Valid dispositions:
#   {"consumer": "<surface>"} — active axis with a named reader.
#   {"retired": "<reason>", "date": "YYYY-MM-DD"} — retired with record.
#   {"pending": "<reason>"} — tracked but awaiting a consumer.
#
# Invariant (test_efficiency_snapshot.py::test_all_axes_have_named_consumer):
#   (a) every ALL_AXES member is in this dict, and
#   (b) every dict key with a "consumer" entry is still in ALL_AXES
#       (so stale consumer records don't outlive their axis).
# ---------------------------------------------------------------------------
AXIS_CONSUMERS: dict[str, dict] = {
    AXIS_COST_PER_MERGED_PR: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_VM_COLD_START_P50_MS: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_VM_REUSE_RATE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_MEDIAN_API_CALLS_PER_TASK: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_MEDIAN_REVIEW_ITERATIONS: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_LLM_COST_PER_MERGED_PR: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_COST_PER_VERIFIED_TASK: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_LLM_COST_PER_VERIFIED_TASK: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_LANE_COST_PER_MERGED_PR: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_LANE_WARM_RESUME_RATE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_CHECKPOINT_LOSS_RATE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_PROMPT_STATIC_BYTES: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_LLM_CACHE_HIT_RATE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_TOTAL_COST_PER_VERIFIED_TASK: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_PROMPT_CACHE_READ_SHARE: {
        "pending": "prompt-cache lever direct-effect axis — baselined for the v24 "
        "cost-reduction direction; awaiting a UI consumer"
    },
    AXIS_REWORK_COST_SHARE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_SALVAGE_COUNT: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_SALVAGE_MERGE_RATE: {
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)"
    },
    AXIS_ACCEPTANCE_PROVEN_RATE: {
        "pending": "acceptance quality trend axis — awaiting UI consumer"
    },
    AXIS_ACCEPTANCE_SATISFIED_RATE: {
        "pending": "acceptance quality trend axis — awaiting UI consumer"
    },
}

NESTED_METRIC_CONSUMERS: dict[str, dict] = {
    "agent_timeout_rate": {
        "ground_truth": "Task.context.failure_category + Task.context.timeout_subtype",
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)",
    },
    FORMAT_COMPLIANCE_KEY: {
        "ground_truth": "Task.format_compliance_summary",
        "consumer": "MetricsDashboard.EFFICIENCY_AXES (admin-ui operator dashboard)",
    },
    "llm_cost_attribution": {
        "ground_truth": "Task.llm_*_tokens_banked + api_call_summary tokens priced via LLM_TOKEN_PRICING; self_repair_burn_share from Task.format_compliance_summary.format_repair_attempts; by_failure_class from Task.context.failure_category",
        "consumer": "read_baseline_with_drift -> llm_cost_attribution (strategic_spawning digest payload)",
    },
    "effort_model_efficiency": {
        "ground_truth": "Task.first_payload[effort] + Task.provider + Task.model + Task.context.pr_merged + api_call_summary tokens priced via LLM_TOKEN_PRICING",
        "consumer": "efficiency_snapshot.write_snapshot → read_baseline_with_drift (persisted by_model efficiency breakdown for DeepSeek vs Anthropic A/B)",
    },
    "compute_cost_attribution": {
        "ground_truth": "ComputeJob.final_cost_usd / accrued_cost_usd per project window; priced-vs-unpriced classified via ComputeJob.polling_metadata.cost_rate_per_second + backend + OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND",
        "consumer": "efficiency_snapshot.compute_current_axes → total_cost_per_verified_task numerator (cost/efficiency ranking) + unpriced-coverage honesty block",
    },
}

DESIGN_DOC_KEY = "efficiency_baseline"

# Sub-key under the efficiency_baseline container recording the last warm-pool
# payoff verdict state we raised an operator alert for. The per-project
# transition check (:func:`maybe_alert_pool_payoff_transition`) compares the
# current verdict against this marker so it fires once per decision-boundary
# crossing rather than every hourly tick.
LAST_ALERTED_PAYOFF_KEY = "last_alerted_payoff_verdict"

# Sub-key under the efficiency_baseline container recording whether the last
# unpriced-models coverage check alerted on a degraded state. The per-project
# transition check (:func:`maybe_alert_unpriced_models_coverage`) compares the
# current degraded/healthy state against this marker so it fires once per
# degradation crossing rather than every tick, and re-arms on recovery.
LAST_ALERTED_UNPRICED_KEY = "last_alerted_unpriced_coverage"

# Sub-key under the efficiency_baseline container recording whether the last
# self-repair burn-share check alerted on an elevated state. The per-project
# transition check (:func:`maybe_alert_self_repair_burn_share`) compares the
# current burn-share against this marker so it fires once per threshold crossing
# rather than every tick, and re-arms on recovery.
LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY = "last_alerted_self_repair_burn_share"

# Sub-key under the efficiency_baseline container recording whether the last
# provider-routing verdict transition check alerted on ADOPT or REJECT. The
# per-project transition check (:func:`maybe_alert_provider_routing_transition`)
# compares the current verdict against this marker so it fires once per
# decision-boundary crossing rather than every hourly tick.
LAST_ALERTED_ROUTING_KEY = "last_alerted_routing_verdict"

# Fire the unpriced-models alert when at least this share of attributed
# (provider+model+token-bearing) tasks ran on combos absent from the pricing
# table, and at least this many such tasks exist (so a single stray task on a
# tiny sample never pages the operator). Tasks with no tokens or no resolved
# provider/model are excluded from the attributed denominator and never count.
UNPRICED_ALERT_SHARE_THRESHOLD = 0.20
UNPRICED_ALERT_MIN_TASKS = 3

# Window over which "current" axes are computed for each daily snapshot.
_AXIS_WINDOW_DAYS = 7

# Cap snapshot history length (reduced from 14 to 7 to keep the
# efficiency_baseline blob bounded — each snapshot is ~17–21 KB, so 7
# entries keeps it at roughly 147 KB plus baseline, leaving more room
# in the overall design_doc row for agent-owned content.  7 is still
# deep enough for the window-delta computation (needs 6: 3 recent + 3 prior).
_SNAPSHOT_HISTORY_MAX = 7

# Metric-definition version stamp applied to every new snapshot.  Increment
# this whenever a metric's computation changes in a way that produces a
# discontinuity in the persisted history series, so consumers (dashboard,
# strategist) can annotate the boundary rather than treating it as real drift.
#
# v6: cost_per_merged_pr_incl_pool_idle switched from the all-time pool idle
#     accumulator to a windowed figure matching the dispatch-funnel window.
# v7: three improvement-lane axes registered (lane_cost_per_merged_pr,
#     lane_warm_resume_rate, checkpoint_loss_rate).
# v8: per-driver LLM cost attribution block (by_model, by_task_type, rework_share,
#     total_llm_usd) folded into the daily snapshot for retroactive decomposition
#     of llm_cost_per_merged_pr drift.
# v9: self_repair_burn_share added to llm_cost_attribution — fraction of total LLM
#     calls that are self-repair reopen attempts, quantifying repair overhead as a
#     cost driver alongside rework_share.
# v10: prompt_static_bytes axis added — source character count of the dominant static
#      prompt sections (SHARED_LIFECYCLE_REMAINDER + assembleFailureGuidanceBlocks
#      constants in agent/src/prompt.ts) as a leading indicator for token budget growth.
# v11: LLM spend axes became provider-cache-aware and pricing rows were refreshed
#      for current Opus/Haiku/OpenAI Codex proxy rates.
# v12: AXIS_TOTAL_COST_PER_VERIFIED_TASK added — total LLM+VM cost per verified
#      merge (is_verified_merge denominator, rollup parents excluded), with a
#      drift floor of ≥20 verified merges.
# v12: rework_cost_share axis added — fraction of total LLM spend consumed by fix/
#      re-attempt tasks, computed from the existing per-PR chain breakdown.
# v13: effort_model_efficiency block added — per-(provider,model) by_model efficiency
#      breakdown (verified_landed_rate, cost_per_merge, run counts) persisted from
#      compute_effort_model_efficiency for retroactive A/B verification of DeepSeek
#      vs Anthropic routing across daily snapshots.
# v14: total_cost_per_verified_task now folds ComputeJob (GPU experiment) cost into
#      its numerator alongside VM+LLM cost, so experiment compute is visible to the
#      cost/efficiency ranking; a compute_cost_attribution honesty block (priced vs
#      unpriced coverage) is added mirroring llm_cost_pricing_coverage.
# v15: prompt_cache_read_share axis added — fleet-level token-weighted rollup of the
#      per-task-type cache_read_share (cache_read_input_tokens / input_tokens across
#      task-type buckets), the prompt-cache lever's direct-effect signal. Baselined
#      on the pre-lever codebase so a later prompt-restructuring change is measured
#      against a frozen denominator.
# v16: _load_recent_tasks now orders by updated_at DESC before applying the 5000-row
#      cap so the most-recent terminal tasks are selected (terminal-time anchored).
#      compute_current_axes now exposes cohort_task_count (actual row count),
#      cohort_capped (bool: True when 5000-row limit was reached), and
#      blocked_provisioned_vm_spend_usd (VM spend by BLOCKED tasks in the window),
#      making silent sampling and BLOCKED exclusion explicit.
METRIC_DEF_VERSION = "v16"


async def _read_snapshot_history(
    session: AsyncSession,
    project_id: uuid.UUID,
    container: Mapping[str, Any],
) -> list[Dict[str, Any]]:
    """Return the daily snapshot history, MERGING the table and design_doc.

    Rows from ``project_efficiency_snapshot`` (returned newest-first by
    :func:`app.project_telemetry.read_recent_efficiency_snapshots`) are merged
    with the design_doc container's ``snapshots`` list, deduped by
    ``captured_at`` (table rows win on conflict), sorted oldest-first and
    truncated to the newest ``_SNAPSHOT_HISTORY_MAX`` entries.

    The current implementation deliberately leaves ``efficiency_baseline``
    dual-written and does NOT backfill the table, so on a mature project the
    table starts empty while design_doc still holds up to ``_SNAPSHOT_HISTORY_MAX`` historical
    snapshots. A prefer-table-exclusive read would drop those older design_doc
    snapshots until the table naturally refills (~7 daily cycles), transiently
    truncating operator-facing trend arrays and hiding a ``serving_sha`` whose
    only snapshot still lives in design_doc. Merging (mirroring the sibling
    :func:`app.lane_baseline_snapshot.read_all_lane_baselines_preferring_table`)
    keeps every stored snapshot visible throughout the dual-write window; once
    the table fully refills its rows simply override the equal-``captured_at``
    design_doc entries with no change in output.
    """
    by_captured_at: Dict[Any, Dict[str, Any]] = {}
    order: list[Any] = []

    def _add(snap: Any) -> None:
        if not isinstance(snap, Mapping):
            return
        # Fall back to object identity when captured_at is missing so a
        # timestamp-less snapshot is never silently collapsed with another.
        key = snap.get("captured_at")
        if key is None:
            key = ("__no_captured_at__", id(snap))
        if key not in by_captured_at:
            order.append(key)
        by_captured_at[key] = dict(snap)

    # design_doc first so equal-captured_at table rows override them.
    for snap in container.get("snapshots") or []:
        _add(snap)
    for row in await read_recent_efficiency_snapshots(session, project_id):
        _add(row.blob)

    def _sort_key(key: Any) -> tuple[int, str]:
        captured = by_captured_at[key].get("captured_at")
        if isinstance(captured, str) and captured:
            return (1, captured)
        # captured_at-less snapshots sort to the front (treated as oldest).
        return (0, "")

    merged = [by_captured_at[key] for key in sorted(order, key=_sort_key)]
    if len(merged) > _SNAPSHOT_HISTORY_MAX:
        merged = merged[-_SNAPSHOT_HISTORY_MAX:]
    return merged


async def _batch_read_snapshot_histories(
    session: AsyncSession,
    projects: Sequence[Project],
) -> Dict[uuid.UUID, list[Dict[str, Any]]]:
    """Return merged snapshot history for each project (one batch DB query).

    Equivalent to calling :func:`_read_snapshot_history` once per project but
    issues a single ``IN (...)`` query to the ``project_efficiency_snapshot``
    table instead of N separate queries, making it safe for fleet-wide use.
    """
    if not projects:
        return {}

    project_ids = [p.id for p in projects]
    result = await session.execute(
        select(ProjectEfficiencySnapshot)
        .where(ProjectEfficiencySnapshot.project_id.in_(project_ids))
        .order_by(ProjectEfficiencySnapshot.captured_at.desc())
    )
    table_rows_by_project: Dict[uuid.UUID, list[ProjectEfficiencySnapshot]] = {}
    for row in result.scalars().all():
        table_rows_by_project.setdefault(row.project_id, []).append(row)

    histories: Dict[uuid.UUID, list[Dict[str, Any]]] = {}
    for project in projects:
        container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
        by_captured_at: Dict[Any, Dict[str, Any]] = {}
        order: list[Any] = []

        def _add(snap: Any, _bca: dict = by_captured_at, _ord: list = order) -> None:  # noqa: ANN001
            if not isinstance(snap, Mapping):
                return
            key = snap.get("captured_at")
            if key is None:
                key = ("__no_captured_at__", id(snap))
            if key not in _bca:
                _ord.append(key)
            _bca[key] = dict(snap)

        for snap in container.get("snapshots") or []:
            _add(snap)
        for row in table_rows_by_project.get(project.id, []):
            _add(row.blob)

        def _sort_key(key: Any, _bca: dict = by_captured_at) -> tuple:  # noqa: ANN001
            captured = _bca[key].get("captured_at")
            if isinstance(captured, str) and captured:
                return (1, captured)
            return (0, "")

        merged = [by_captured_at[k] for k in sorted(order, key=_sort_key)]
        if len(merged) > _SNAPSHOT_HISTORY_MAX:
            merged = merged[-_SNAPSHOT_HISTORY_MAX:]
        histories[project.id] = merged

    return histories


async def _load_recent_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> list[Task]:
    """Load tasks for the project that reached a terminal state in the window.

    Includes CANCELLED alongside COMPLETED/FAILED so VM cost incurred by
    cancelled tasks (which now record ``vm_stopped_at`` on cancel) is counted
    in ``cost_per_merged_pr``. The merged-PR denominator still only counts
    COMPLETED tasks with ``pr_merged``, so cancelled tasks add cost without
    inflating the merge count.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            Task.updated_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                # started_at: presence marks a task that actually reached
                # execution — the AGENT_TIMEOUT-rate measure's denominator.
                Task.started_at,
                Task.vm_spot,
                Task.vm_machine_type,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                # Banked retry attribution must stay loaded so pricing and
                # alerting see the full attempt history after projection.
                Task.vm_runtime_cost_banked_db,
                Task.llm_input_tokens_banked_db,
                Task.llm_output_tokens_banked_db,
                Task.llm_call_count_banked_db,
                Task.attempt_history_db,
                Task.vm_cold_start_ms,
                Task.vm_warm_start,
                Task.api_call_summary,
                Task.vm_dispatched_at,
                Task.provider,
                Task.model,
                # Read by estimate_pool_hit_rate via get_task_llm_account_id,
                # which prefers this promoted column over the context fallback;
                # must be loaded so the access doesn't trip raiseload=True.
                Task.llm_account_id,
                Task.format_compliance_summary,
                # parent_task_id: read by _rollup_parent_ids so merged-PR
                # metrics can exclude rollup parents without tripping
                # raiseload=True.
                Task.parent_task_id,
                # payloads: required so task.first_payload can be read without
                # triggering an async lazy-load (used by compute_llm_cost_by_task_type
                # and compute_effort_model_efficiency when called from this path).
                Task.payloads,
                # strategic_cycle_id: required for strategic_cycle_cost_per_event
                # aggregation in _compact_llm_cost_attribution.
                Task.strategic_cycle_id,
                # updated_at: used for ordering and windowed filtering.
                Task.updated_at,
                raiseload=True,
            )
        )
        .order_by(Task.updated_at.desc())
        .limit(5000)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _compute_blocked_vm_spend(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> float:
    """Sum timestamped VM attempts for BLOCKED tasks within the window.

    BLOCKED tasks release their VM (vm_stopped_at is stamped on transition), so
    each current and banked attempt is fully accountable. ``vm_provisioned_at``
    remains the window anchor, but banked cost is read from attempt history
    instead of the lifetime scalar so attempts before the cutoff are excluded.
    Legacy banked cost without a valid timestamp is intentionally unattributed.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    stmt = select(Task).options(
        load_only(
            Task.vm_provisioned_at,
            Task.vm_stopped_at,
            Task.vm_spot,
            Task.vm_machine_type,
            Task.attempt_history_db,
            raiseload=True,
        )
    ).where(
        Task.project_id == project_id,
        Task.status == TaskStatus.BLOCKED,
        or_(Task.vm_provisioned_at >= cutoff, Task.attempt_history_db.isnot(None)),
    )
    tasks = list((await session.execute(stmt)).scalars().all())
    spend = 0.0
    for task in tasks:
        if task.vm_provisioned_at is not None and task.vm_provisioned_at >= cutoff:
            spend += get_task_current_attempt_vm_cost_usd(task)
        for attempt in task.attempt_history_db or []:
            try:
                provisioned_at = datetime.fromisoformat(attempt["vm_provisioned_at"])
                if provisioned_at.tzinfo is not None:
                    provisioned_at = provisioned_at.replace(tzinfo=None)
                if provisioned_at >= cutoff:
                    spend += max(0.0, float(attempt.get("vm_runtime_cost_usd") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
    return spend


async def _load_project_lanes(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[ImprovementLane]:
    """Load all non-KILLED ImprovementLane rows for the project."""
    stmt = select(ImprovementLane).where(
        ImprovementLane.project_id == project_id,
        ImprovementLane.status != ImprovementLaneStatus.KILLED.value,
    )
    return list((await session.execute(stmt)).scalars().all())


def _compute_lane_cost_per_merged_pr(
    lanes: list[ImprovementLane],
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> float | None:
    """Total lane VM+idle cost / RETIRED lane count in the window. None when no retired lanes.

    Uses ``retired_at`` to scope to lanes that reached their terminal success
    state within the rolling window, mirroring how task-based axes use
    ``updated_at`` to bound the denominator. Lanes with no ``retired_at`` are
    excluded even if their status is RETIRED.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    total_cost = 0.0
    retired_count = 0
    for lane in lanes:
        if lane.status != ImprovementLaneStatus.RETIRED.value:
            continue
        if lane.retired_at is None or lane.retired_at < cutoff:
            continue
        total_cost += (lane.vm_cost_usd_total or 0.0) + (
            lane.reserved_idle_cost_usd_total or 0.0
        )
        retired_count += 1
    if not retired_count:
        return None
    return round(total_cost / retired_count, 4)


def _compute_lane_warm_resume_rate(lanes: list[ImprovementLane]) -> float | None:
    """Share of lane phase resumes that reused a warm workspace.

    Reads ``warm_resume`` from each checkpoint record in ``checkpoints[]``.
    Uses all non-KILLED project lanes so ACTIVE/PARKED lanes contribute
    real-time signal alongside completed RETIRED ones.
    Returns None when no checkpoints exist across any lane.
    """
    total_checkpoints = 0
    warm_checkpoints = 0
    for lane in lanes:
        for checkpoint in lane.checkpoints or []:
            total_checkpoints += 1
            if checkpoint.get("warm_resume") is True:
                warm_checkpoints += 1
    if not total_checkpoints:
        return None
    return round(warm_checkpoints / total_checkpoints, 4)


def _compute_checkpoint_loss_rate(lanes: list[ImprovementLane]) -> float | None:
    """Fraction of checkpoint attempts that were lost (phase requeued without a checkpoint).

    Formula: sum(phase_requeues) / (sum(checkpoint_seq) + sum(phase_requeues))
    across all non-KILLED project lanes. Returns None when the denominator is 0
    (no lanes have attempted any phases).
    """
    total_seq = 0
    total_requeues = 0
    for lane in lanes:
        total_seq += int(lane.checkpoint_seq or 0)
        total_requeues += int(lane.phase_requeues or 0)
    denominator = total_seq + total_requeues
    if not denominator:
        return None
    return round(total_requeues / denominator, 4)


def _rollup_parent_ids(tasks: list[Task]) -> frozenset:
    """Return the set of task IDs that are parents of at least one other task.

    Rollup parents get is_merged_completion=True alongside all their children
    when the rollup lands, so counting them in merged-PR metrics double-counts
    the landing. Callers exclude these IDs from merged/landed counts.
    """
    return frozenset(t.parent_task_id for t in tasks if t.parent_task_id is not None)


def _cost_and_merges(tasks: list[Task]) -> tuple[float, int]:
    """Return (total task-attached VM cost, merged-PR count) over the window."""
    total_cost = 0.0
    merged = 0
    parent_ids = _rollup_parent_ids(tasks)
    for t in tasks:
        total_cost += get_task_total_vm_cost_usd(t)
        if is_merged_completion(t) and t.id not in parent_ids:
            merged += 1
    return total_cost, merged


def _compute_cost_per_merged_pr(tasks: list[Task]) -> float | None:
    """Total VM cost / merged PR count over the window. None when no merges."""
    total_cost, merged = _cost_and_merges(tasks)
    if not merged:
        return None
    return round(total_cost / merged, 4)


def _compute_cost_per_verified_task(tasks: list[Task]) -> float | None:
    """Total VM cost / CI-verified merge count over the window.

    Uses :func:`is_verified_merge` as the denominator so fall-open (CI-bypassed)
    merges do not dilute the per-task cost. Returns None when no verified merges.
    """
    total_cost = 0.0
    verified = 0
    parent_ids = _rollup_parent_ids(tasks)
    for t in tasks:
        total_cost += get_task_total_vm_cost_usd(t)
        if is_verified_merge(t) and t.id not in parent_ids:
            verified += 1
    if not verified:
        return None
    return round(total_cost / verified, 4)


# ---------------------------------------------------------------------------
# Warm-pool idle-VM cost accounting (direction d-5fd69fe1)
# ---------------------------------------------------------------------------
# Idle pool members still bill GCE wall-clock even though they carry no task, so
# task-attached cost (get_task_total_vm_cost_usd) understates real spend once a
# pool exists — pre-warming could make cost_per_merged_pr look BETTER while
# total spend rises. We make that honest two ways:
#   1. settle_pool_vm_idle_cost() banks elapsed idle seconds * the fleet
#      per-second rate onto PoolVm.idle_cost_banked when a member leaves IDLE
#      (the lease / reaper paths in app.warm_pool are the intended callers).
#   2. compute_pool_idle_cost() sums that banked cost AND the live (not-yet-
#      settled) accrual of currently-IDLE members, so the figure is correct even
#      before any settle call fires.
# The figure is surfaced as a clearly-labelled companion to cost_per_merged_pr
# (cost_per_merged_pr_incl_pool_idle) rather than folded into the immutable
# baseline axis, so existing drift comparisons stay apples-to-apples.


def settle_pool_vm_idle_cost(
    member: PoolVm, *, now: datetime | None = None, session=None
) -> float:
    """Bank elapsed idle cost onto ``member.idle_cost_banked``; return the amount.

    Computes ``(now - idle_since) * fleet_rate`` at the fleet machine type /
    spot mode, adds it to the running ``idle_cost_banked`` total, and advances
    ``idle_since`` to ``now`` so the same span is never billed twice. A no-op
    (returns 0.0) when the member is not currently idle or no time has elapsed.
    The caller owns the transaction (commit).

    When ``session`` is provided, also appends a ``PoolIdleSpan`` row so the
    windowed idle-cost query can filter on settled_at rather than on the
    member's retirement timestamp, eliminating lump-attribution distortion.
    """
    if member.idle_since is None:
        return 0.0
    if now is None:
        now = _utcnow()
    started_at = member.idle_since
    elapsed = (now - started_at).total_seconds()
    if elapsed <= 0:
        return 0.0
    cost = idle_cost_usd(elapsed)
    member.idle_cost_banked = (member.idle_cost_banked or 0.0) + cost
    member.idle_since = now
    if session is not None:
        session.add(
            PoolIdleSpan(
                project_id=member.project_id,
                vm_instance_id=member.vm_instance_id,
                started_at=started_at,
                settled_at=now,
                cost_usd=cost,
            )
        )
    return cost


async def compute_pool_idle_cost(
    session: AsyncSession, project_id: uuid.UUID | None = None
) -> Dict[str, Any]:
    """Aggregate warm-pool size + idle cost (banked + live accrual).

    When ``project_id`` is given the scan is scoped to that tenant's members;
    otherwise it spans every pool member (the fleet-wide admin view).

    ``pool_size`` counts live members (PROVISIONING/IDLE/LEASED); RETIRING
    members are excluded from the size but their already-banked idle cost is
    still summed so teardown never erases measured spend. ``live_idle_cost_usd``
    is the not-yet-settled accrual of currently-IDLE members.

    Because the idle reaper HARD-DELETES a member's row on teardown, the banked
    cost of every already-reaped member is carried in the durable
    ``PoolIdleCostLedger`` (written atomically with the delete). That carry-
    forward is summed into ``settled_idle_cost_usd`` here so spend survives the
    delete — without it the metric would only ever see members that happen to
    still exist at snapshot time, undercounting by the bulk of pool idle cost.

    ``by_account`` breaks idle cost down by the member's pinned ``account_id`` so
    an operator can read which account's idle members drive cost during a
    controlled enable. It covers LIVE members only (banked + live accrual of
    rows that still exist); the ledger carry-forward of already-reaped members
    cannot be attributed per account (``PoolIdleCostLedger`` carries no
    ``account_id``), so it stays in the fleet-wide ``retired_idle_cost_usd`` and
    is intentionally absent from the per-account totals.
    """
    from app.warm_backlog_reuse import _account_label

    stmt = select(PoolVm)
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    members = list((await session.execute(stmt)).scalars().all())

    now = _utcnow()
    settled = 0.0
    live = 0.0
    pool_size = 0
    idle_count = 0
    by_account: Dict[str, Dict[str, Any]] = {}

    def _bucket(member: PoolVm) -> Dict[str, Any]:
        return by_account.setdefault(
            _account_label(member.account_id),
            {
                "settled_idle_cost_usd": 0.0,
                "live_idle_cost_usd": 0.0,
                "member_count": 0,
                "idle_count": 0,
            },
        )

    for m in members:
        banked = m.idle_cost_banked or 0.0
        settled += banked
        bucket = _bucket(m)
        bucket["settled_idle_cost_usd"] += banked
        if m.state == PoolVmState.RETIRING:
            continue
        pool_size += 1
        bucket["member_count"] += 1
        if m.state == PoolVmState.IDLE and m.idle_since is not None:
            idle_count += 1
            elapsed = (now - m.idle_since).total_seconds()
            if elapsed > 0:
                accrued = idle_cost_usd(elapsed)
                live += accrued
                bucket["live_idle_cost_usd"] += accrued
                bucket["idle_count"] += 1

    for bucket in by_account.values():
        bucket["settled_idle_cost_usd"] = round(bucket["settled_idle_cost_usd"], 6)
        bucket["live_idle_cost_usd"] = round(bucket["live_idle_cost_usd"], 6)
        bucket["total_idle_cost_usd"] = round(
            bucket["settled_idle_cost_usd"] + bucket["live_idle_cost_usd"], 6
        )

    # Carry-forward: durable idle spend of members already reaped (rows deleted).
    ledger_stmt = select(func.coalesce(func.sum(PoolIdleCostLedger.idle_cost_usd), 0.0))
    if project_id is not None:
        ledger_stmt = ledger_stmt.where(PoolIdleCostLedger.project_id == project_id)
    retired_idle = float((await session.execute(ledger_stmt)).scalar() or 0.0)
    settled += retired_idle

    return {
        "pool_size": pool_size,
        "idle_count": idle_count,
        "settled_idle_cost_usd": round(settled, 6),
        "retired_idle_cost_usd": round(retired_idle, 6),
        "live_idle_cost_usd": round(live, 6),
        "total_idle_cost_usd": round(settled + live, 6),
        "by_account": by_account,
    }


async def _compute_windowed_idle_cost(
    session: AsyncSession,
    project_id: uuid.UUID | None,
    cutoff: datetime,
) -> Dict[str, Any]:
    """Sum pool idle cost scoped to a rolling window.

    When ``project_id`` is ``None``, the query spans every pool member
    (fleet-wide); otherwise it is scoped to the given project. Returns a dict
    with ``total`` (the windowed idle cost in USD) and ``by_account`` (windowed
    per-account idle cost from live IDLE members, as ``{label: {"total_idle_cost_usd":
    float}}``). Ledger carry-forward of already-reaped members has no account
    attribution and is therefore excluded from per-account totals (but included
    in ``total``).

    Span rows (settled spans): sum ``cost_usd`` where ``settled_at >= cutoff``
    — each completed idle period has an exact settled_at timestamp, so only
    spans that ended inside the window are counted; pre-window spans of
    in-window retirees are excluded rather than lump-attributed.

    Live IDLE members: accrue ``(now - max(idle_since, cutoff))`` seconds so
    only the portion of the current in-progress idle period that falls inside
    the window is counted. ``idle_cost_banked`` aggregates past idle periods
    (already present in PoolIdleSpan rows) and is intentionally excluded here.
    """
    from app.warm_backlog_reuse import _account_label

    now = _utcnow()

    # Settled spans: only rows whose settlement timestamp falls within the window.
    span_stmt = select(func.coalesce(func.sum(PoolIdleSpan.cost_usd), 0.0)).where(
        PoolIdleSpan.settled_at >= cutoff
    )
    if project_id is not None:
        span_stmt = span_stmt.where(PoolIdleSpan.project_id == project_id)
    windowed_retired = float((await session.execute(span_stmt)).scalar() or 0.0)

    # Live IDLE members: current idle period clamped to the window start.
    member_stmt = select(PoolVm).where(
        PoolVm.state == PoolVmState.IDLE,
        PoolVm.idle_since.isnot(None),
    )
    if project_id is not None:
        member_stmt = member_stmt.where(PoolVm.project_id == project_id)
    members = list((await session.execute(member_stmt)).scalars().all())
    windowed_live = 0.0
    by_account: Dict[str, Any] = {}
    for m in members:
        effective_start = max(m.idle_since, cutoff)  # type: ignore[arg-type]
        elapsed = (now - effective_start).total_seconds()
        if elapsed > 0:
            cost = idle_cost_usd(elapsed)
            windowed_live += cost
            label = _account_label(m.account_id)
            acc = by_account.setdefault(label, {"total_idle_cost_usd": 0.0})
            acc["total_idle_cost_usd"] = round(acc["total_idle_cost_usd"] + cost, 6)

    return {
        "total": round(windowed_retired + windowed_live, 6),
        "by_account": by_account,
    }


def compute_pool_cost_amortization(
    idle_cost_usd: float, funnel: Dict[str, Any]
) -> Dict[str, Any]:
    """Cost-amortization view of the warm pool over the funnel's window.

    Combines the warm-pool idle-cost accrual with the pool dispatch funnel so
    operators can see whether idle spend is being amortized by warm-start hits —
    the same two live signals the maintainer's cost-aware auto-drain guard acts
    on (see ``app.warm_pool_maintainer.evaluate_pool_cost_guard``), surfaced
    read-only here. ``cost_per_pool_hit_usd`` is the idle dollars spent per warm
    start over the window (``None`` when no hit has landed yet — idle spend is
    not being amortized at all).
    """
    hits = int(funnel.get("pool_lease_hits") or 0)
    idle = round(float(idle_cost_usd or 0.0), 6)
    return {
        "window_hours": funnel.get("window_hours"),
        "pool_idle_cost_usd": idle,
        "pool_lease_hits": hits,
        "pool_hit_rate": funnel.get("pool_hit_rate"),
        "total_dispatches": funnel.get("total_dispatches", 0),
        "cost_per_pool_hit_usd": round(idle / hits, 6) if hits else None,
    }


def compute_pool_cost_amortization_by_account(
    idle_by_account: Dict[str, Any], funnel: Dict[str, Any]
) -> Dict[str, Any]:
    """Per-account cost-amortization: join per-account pool idle cost with the
    per-account dispatch funnel.

    Lets an operator answer, PER ACCOUNT, how many dispatches the pool served
    warm, the per-account hit rate, the per-account (live-member) idle cost, and
    the idle dollars spent per warm start — the breakdown a controlled enable's
    'retire on success or pivot' decision depends on. ``idle_by_account`` is the
    ``by_account`` map from :func:`compute_pool_idle_cost`; ``funnel`` is
    :func:`app.warm_backlog_reuse.get_pool_funnel_snapshot`. Accounts present on
    only one side are still emitted (idle members but no dispatch yet, or
    dispatches but no live member). ``cost_per_pool_hit_usd`` is ``None`` when
    that account has no warm hit yet — its idle spend is not being amortized.
    """
    funnel_accounts = funnel.get("by_account") or {}
    out: Dict[str, Any] = {}
    for label in set(idle_by_account) | set(funnel_accounts):
        idle = idle_by_account.get(label) or {}
        fb = funnel_accounts.get(label) or {}
        hits = int(fb.get("pool_lease_hits") or 0)
        idle_cost = round(float(idle.get("total_idle_cost_usd") or 0.0), 6)
        out[label] = {
            "pool_idle_cost_usd": idle_cost,
            "pool_lease_hits": hits,
            "cold_dispatches": int(fb.get("cold_dispatches") or 0),
            "total_dispatches": int(fb.get("total_dispatches") or 0),
            "pool_hit_rate": fb.get("pool_hit_rate"),
            "cost_per_pool_hit_usd": round(idle_cost / hits, 6) if hits else None,
        }
    return out


# ---------------------------------------------------------------------------
# Per-project warm-pool PAYOFF VERDICT (direction d-… bounded-enable proof)
# ---------------------------------------------------------------------------
# The backend already assembles every signal needed to judge whether a bounded
# single-project warm-pool enable paid off — the idle-inclusive cost-per-merged-
# PR drift (NO-regression half) and the POOL-origin warm-start share + pool
# dispatch-funnel hit rate (LIFT half). What it lacks is a single synthesized
# answer. This classifier folds those raw numbers into one of four verdict
# states with the supporting numbers and a recommended next action, so an
# operator managing the pool sees the direction question — "did enabling the
# pool produce a measured reuse lift WITHOUT a cost regression?" — answered in
# one place instead of cross-referencing several figures and a trend chart.
#
# GROUNDING RULES (mirrors the codebase warnings in _compute_warm_start_origin_
# breakdown and warm_pool_maintainer.evaluate_pool_cost_guard):
#   * Read LIFT off the POOL-specific signals — the pool dispatch-funnel hit
#     rate and the pool-origin SHARE of warm starts — NOT the aggregate
#     vm_reuse_rate, which is contaminated by chain-continuity / backlog-reuse /
#     fix-loop reruns.
#   * Read NO-REGRESSION off the idle-inclusive cost-per-merged-PR drift versus
#     the immutable pre-pool baseline (that axis already folds pool idle spend
#     into the numerator, so its drift directly answers the cost question).
# It is a PURE, DB-free synthesis over fields already on the efficiency-axes
# response — no new data collection, schema, or migration — so the operator
# gets a fresh judgment on demand (the cost side updates with the daily
# snapshot, which is fine for a multi-day proven-over-the-window verdict).

# Verdict states.
POOL_PAYOFF_PROVEN = "PROVEN"
POOL_PAYOFF_COST_REGRESSION = "COST_REGRESSION"
POOL_PAYOFF_NO_LIFT = "NO_LIFT"
POOL_PAYOFF_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Thresholds. The hit-rate floor and minimum dispatch sample size deliberately
# mirror the warm-pool cost-guard defaults (warm_pool_cost_guard_min_hit_rate /
# warm_pool_cost_guard_min_dispatches in warm_pool_maintainer) so the verdict's
# "is the pool serving / do we have enough dispatches to judge" bar matches the
# guard the maintainer actually drains on. The cost tolerance is a small slack
# band on the idle-inclusive cost drift — within it, the pool has NOT regressed
# cost per PR.
POOL_PAYOFF_HIT_RATE_FLOOR = 0.10
POOL_PAYOFF_ORIGIN_SHARE_FLOOR = 0.10
POOL_PAYOFF_MIN_POOL_DISPATCHES = 20
POOL_PAYOFF_COST_TOLERANCE_PCT = 5.0

# Minimum pool-origin warm starts before a PROVEN verdict is treated as
# materially supported in reader-facing summaries. PROVEN is useful history, but
# it is not a durable operator alert; COST_REGRESSION remains the actionable
# payoff transition.
POOL_PAYOFF_PROVEN_MIN_WARM_STARTS = 5

# ---------------------------------------------------------------------------
# Provider routing A/B verdict (direction d-cf39cb40)
# ---------------------------------------------------------------------------
# Verdict states for the DeepSeek-vs-Anthropic routing A/B: is routing
# low/medium-effort tasks to DeepSeek cutting cost without regressing quality
# (verified-merge rate)?

PROVIDER_ROUTING_ADOPT = "ADOPT"
PROVIDER_ROUTING_REJECT = "REJECT"
PROVIDER_ROUTING_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Minimum PR-eligible tasks per provider before a verdict can be issued.
PROVIDER_ROUTING_MIN_PR_ELIGIBLE_TASKS = 20

# Cost-ratio threshold: DeepSeek cost-per-merge must be strictly below this
# fraction of Anthropic's to count as meaningfully cheaper.  0.90 = at least
# 10 % cheaper.  Below 1.0 the raw direction is "cheaper"; the 0.90 bar
# filters out near-parity noise so the verdict is actionable.
PROVIDER_ROUTING_COST_RATIO_THRESHOLD = 0.90

# Maximum allowable verified-merge-rate regression in *percentage points*.
# DeepSeek's verified_landed_rate may be at most this many points below
# Anthropic's.  -5.0 means "5 percentage points lower is acceptable."
PROVIDER_ROUTING_QUALITY_FLOOR_PCT = -5.0


def compute_pool_payoff_verdict(
    origin_breakdown: Dict[str, Any] | None,
    cost_incl_axis: Dict[str, Any] | None,
    pool_funnel: Dict[str, Any] | None,
    *,
    hit_rate_floor: float = POOL_PAYOFF_HIT_RATE_FLOOR,
    origin_share_floor: float = POOL_PAYOFF_ORIGIN_SHARE_FLOOR,
    min_pool_dispatches: int = POOL_PAYOFF_MIN_POOL_DISPATCHES,
    cost_tolerance_pct: float = POOL_PAYOFF_COST_TOLERANCE_PCT,
) -> Dict[str, Any]:
    """Synthesize the warm-pool payoff verdict for one project.

    ``origin_breakdown`` is :func:`_compute_warm_start_origin_breakdown`'s output
    (``pool_origin_share`` + per-origin counts) or ``None``. ``cost_incl_axis``
    is the ``cost_per_merged_pr_incl_pool_idle_axis`` block
    (``current``/``baseline``/``drift_pct``) or ``None``. ``pool_funnel`` is the
    persisted ``pool_dispatch_funnel`` block (``pool_hit_rate`` +
    ``total_dispatches``) or ``None``. Pure and DB-free so it is unit-testable
    over synthetic axis inputs.

    Returns ``{state, supporting, recommended_action}`` where ``state`` is one of
    the ``POOL_PAYOFF_*`` constants and ``supporting`` carries the numbers the
    classification was read off so the operator can see WHY.

    Classification (checked in order):
      * ``INSUFFICIENT_DATA`` — no cost baseline/current, or too few pool
        dispatches to judge lift. Nothing to conclude yet; keep measuring.
      * ``COST_REGRESSION`` — idle-inclusive cost per merged PR is materially
        above the pre-pool baseline (``drift_pct`` over tolerance). The headline
        stop signal regardless of lift: you are paying more per PR.
      * ``NO_LIFT`` — the pool is barely serving (BOTH the funnel hit rate and
        the pool-origin share below their floors) so idle spend is not being
        amortized, even though cost has not (yet) materially regressed.
      * ``PROVEN`` — the pool is measurably serving warm starts (hit rate OR
        pool-origin share at/above floor) AND cost per merged PR is within
        tolerance of the pre-pool baseline.
    """
    cost = cost_incl_axis or {}
    cost_current = cost.get("current")
    cost_baseline = cost.get("baseline")
    cost_drift_pct = cost.get("drift_pct")

    funnel = pool_funnel or {}
    hit_rate = funnel.get("pool_hit_rate")
    total_dispatches = int(funnel.get("total_dispatches") or 0)

    breakdown = origin_breakdown or {}
    pool_origin_share = breakdown.get("pool_origin_share")
    pool_warm_starts = int(breakdown.get("warm_pool") or 0)

    supporting = {
        "pool_origin_share": pool_origin_share,
        "pool_warm_starts": pool_warm_starts,
        "pool_hit_rate": hit_rate,
        "total_pool_dispatches": total_dispatches,
        "cost_drift_pct": cost_drift_pct,
        "cost_incl_idle_current": cost_current,
        "cost_baseline": cost_baseline,
    }

    def _result(state: str, recommended_action: str) -> Dict[str, Any]:
        return {
            "state": state,
            "supporting": supporting,
            "recommended_action": recommended_action,
        }

    # INSUFFICIENT_DATA: no baseline/snapshot to compare cost against, or too
    # few pool dispatches to read lift off the funnel.
    has_cost = (
        isinstance(cost_current, (int, float))
        and isinstance(cost_baseline, (int, float))
        and cost_baseline != 0
    )
    if not has_cost or total_dispatches < min_pool_dispatches:
        return _result(
            POOL_PAYOFF_INSUFFICIENT_DATA,
            "Keep measuring — not enough merged PRs, pool dispatches, or "
            "baseline yet to judge payoff.",
        )

    # COST_REGRESSION: idle-inclusive cost per merged PR materially above the
    # pre-pool baseline. The headline stop signal — flag it regardless of lift.
    if isinstance(cost_drift_pct, (int, float)) and cost_drift_pct > cost_tolerance_pct:
        return _result(
            POOL_PAYOFF_COST_REGRESSION,
            "Reduce the pool target or disable — idle-inclusive cost per merged "
            "PR has regressed above the pre-pool baseline.",
        )

    # NO_LIFT: the pool is barely serving (hit rate AND pool-origin share both
    # below floor) so idle spend is not being amortized.
    serving = (isinstance(hit_rate, (int, float)) and hit_rate >= hit_rate_floor) or (
        isinstance(pool_origin_share, (int, float))
        and pool_origin_share >= origin_share_floor
    )
    if not serving:
        return _result(
            POOL_PAYOFF_NO_LIFT,
            "Tune the pool target or disable — the pool is barely serving warm "
            "starts, so idle spend is not being amortized.",
        )

    # PROVEN: measurably serving AND cost within tolerance of the baseline.
    return _result(
        POOL_PAYOFF_PROVEN,
        "Keep the pool — it is serving warm starts without regressing cost per "
        "merged PR. Consider widening scope.",
    )


def compute_provider_routing_verdict(
    by_model_rows: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """Synthesize the DeepSeek-vs-Anthropic routing A/B verdict.

    Takes the ``by_model`` rows from :func:`compute_effort_model_efficiency`
    (each row carries ``provider``, ``model``, ``run_count``,
    ``pr_eligible_run_count``, ``verified_landed_count``,
    ``verified_landed_rate``, ``total_cost_usd``, ``cost_per_merge``) and
    aggregates by provider, then classifies the routing payoff.

    Returns ``{state, supporting, recommended_action}`` where ``state`` is one of
    the ``PROVIDER_ROUTING_*`` constants and ``supporting`` carries the per-provider
    numbers the classification was read off.

    Classification (checked in order):

    * ``INSUFFICIENT_DATA`` — fewer than the minimum PR-eligible tasks on either
      provider side.  Not enough signal to judge; keep measuring.
    * ``REJECT`` — verified-merge rate on DeepSeek is materially below the
      Anthropic baseline (rate delta below the quality floor), OR DeepSeek
      cost-per-merge is not meaningfully cheaper than Anthropic.
    * ``ADOPT`` — DeepSeek is cheaper AND verified-merge rate is within the
      quality guardrail.  The routing is paying off.
    """
    # -- aggregate by provider ------------------------------------------------
    _deepseek: Dict[str, Any] = {
        "run_count": 0,
        "pr_eligible_run_count": 0,
        "verified_landed_count": 0,
        "total_cost_usd": 0.0,
    }
    _anthropic: Dict[str, Any] = {
        "run_count": 0,
        "pr_eligible_run_count": 0,
        "verified_landed_count": 0,
        "total_cost_usd": 0.0,
    }

    for row in by_model_rows:
        provider = str(row.get("provider") or "").lower()
        if provider == "deepseek":
            b = _deepseek
        elif provider == "anthropic":
            b = _anthropic
        else:
            continue
        b["run_count"] += int(row.get("run_count", 0))
        b["pr_eligible_run_count"] += int(row.get("pr_eligible_run_count", 0))
        b["verified_landed_count"] += int(row.get("verified_landed_count", 0))
        b["total_cost_usd"] += float(row.get("total_cost_usd", 0))

    # -- derived metrics ------------------------------------------------------
    def _cost_per_merge(b: Dict[str, Any]) -> float | None:
        v = b["verified_landed_count"]
        return round(b["total_cost_usd"] / v, 6) if v else None

    def _verified_rate(b: Dict[str, Any]) -> float | None:
        p = b["pr_eligible_run_count"]
        return round(b["verified_landed_count"] / p, 4) if p else None

    ds_cpm = _cost_per_merge(_deepseek)
    an_cpm = _cost_per_merge(_anthropic)
    ds_rate = _verified_rate(_deepseek)
    an_rate = _verified_rate(_anthropic)

    cost_ratio: float | None = (
        round(ds_cpm / an_cpm, 4)
        if (ds_cpm is not None and an_cpm is not None and an_cpm > 0)
        else None
    )
    rate_delta_pct: float | None = (
        round((ds_rate - an_rate) * 100, 2)
        if (ds_rate is not None and an_rate is not None)
        else None
    )

    supporting: Dict[str, Any] = {
        "deepseek_run_count": _deepseek["run_count"],
        "deepseek_pr_eligible_run_count": _deepseek["pr_eligible_run_count"],
        "deepseek_verified_landed_count": _deepseek["verified_landed_count"],
        "deepseek_verified_landed_rate": ds_rate,
        "deepseek_cost_per_merge": ds_cpm,
        "deepseek_total_cost_usd": round(_deepseek["total_cost_usd"], 6),
        "anthropic_run_count": _anthropic["run_count"],
        "anthropic_pr_eligible_run_count": _anthropic["pr_eligible_run_count"],
        "anthropic_verified_landed_count": _anthropic["verified_landed_count"],
        "anthropic_verified_landed_rate": an_rate,
        "anthropic_cost_per_merge": an_cpm,
        "anthropic_total_cost_usd": round(_anthropic["total_cost_usd"], 6),
        "cost_ratio": cost_ratio,
        "verified_rate_delta_pct": rate_delta_pct,
    }

    def _result(state: str, recommended_action: str) -> Dict[str, Any]:
        return {
            "state": state,
            "supporting": supporting,
            "recommended_action": recommended_action,
        }

    # -- classify -------------------------------------------------------------
    if (
        _deepseek["pr_eligible_run_count"] < PROVIDER_ROUTING_MIN_PR_ELIGIBLE_TASKS
        or _anthropic["pr_eligible_run_count"] < PROVIDER_ROUTING_MIN_PR_ELIGIBLE_TASKS
    ):
        return _result(
            PROVIDER_ROUTING_INSUFFICIENT_DATA,
            "Keep measuring — not enough PR-eligible tasks per provider to judge "
            "routing payoff.",
        )

    # Quality regression: DeepSeek verified rate materially below Anthropic.
    if (
        rate_delta_pct is not None
        and rate_delta_pct < PROVIDER_ROUTING_QUALITY_FLOOR_PCT
    ):
        return _result(
            PROVIDER_ROUTING_REJECT,
            "Reject DeepSeek routing — verified-merge rate regressed materially "
            "below the Anthropic baseline.",
        )

    # Cost not meaningfully cheaper.
    if cost_ratio is None or cost_ratio >= PROVIDER_ROUTING_COST_RATIO_THRESHOLD:
        return _result(
            PROVIDER_ROUTING_REJECT,
            "Reject DeepSeek routing — cost per merge is not meaningfully lower "
            "than Anthropic.",
        )

    # Cheaper AND quality held → adopt.
    return _result(
        PROVIDER_ROUTING_ADOPT,
        "Adopt DeepSeek routing for low/medium-effort tasks — cost is lower with "
        "no quality regression.",
    )


def compute_provider_routing_share(
    by_model_rows: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate by-model efficiency rows into a per-provider routing share block.

    Takes the same ``by_model`` rows from :func:`compute_effort_model_efficiency`
    as :func:`compute_provider_routing_verdict` and returns a compact summary of
    task count and verified merge rate by provider, plus overall totals — so
    operators can confirm at a glance which provider handled what fraction of
    tasks and at what verified-landing rate.

    Returns::

        {
            "by_provider": [
                {"provider": "deepseek", "run_count": 30,
                 "pr_eligible_run_count": 28, "verified_landed_count": 20,
                 "verified_landed_rate": 0.714, "total_cost_usd": 12.50,
                 "cost_per_merge": 0.625},
                ...
            ],
            "total_run_count": 40,
            "total_pr_eligible_run_count": 36,
        }
    """
    from collections import defaultdict

    # Aggregate by lowercased provider.
    accum: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "run_count": 0,
            "pr_eligible_run_count": 0,
            "verified_landed_count": 0,
            "total_cost_usd": 0.0,
        }
    )
    for row in by_model_rows:
        provider = str(row.get("provider") or "other").lower()
        b = accum[provider]
        b["run_count"] += int(row.get("run_count", 0))
        b["pr_eligible_run_count"] += int(row.get("pr_eligible_run_count", 0))
        b["verified_landed_count"] += int(row.get("verified_landed_count", 0))
        b["total_cost_usd"] += float(row.get("total_cost_usd", 0))

    by_provider = []
    total_run_count = 0
    total_pr_eligible_run_count = 0

    for provider in sorted(accum.keys()):
        b = accum[provider]
        total_run_count += b["run_count"]
        total_pr_eligible_run_count += b["pr_eligible_run_count"]
        pe = b["pr_eligible_run_count"]
        vc = b["verified_landed_count"]
        by_provider.append(
            {
                "provider": provider,
                "run_count": b["run_count"],
                "pr_eligible_run_count": pe,
                "verified_landed_count": vc,
                "verified_landed_rate": round(vc / pe, 4) if pe else None,
                "total_cost_usd": round(b["total_cost_usd"], 6),
                "cost_per_merge": (
                    round(b["total_cost_usd"] / vc, 6) if vc else None
                ),
            }
        )

    return {
        "by_provider": by_provider,
        "total_run_count": total_run_count,
        "total_pr_eligible_run_count": total_pr_eligible_run_count,
    }


# ---------------------------------------------------------------------------
# Pre-enable counterfactual warm-pool hit-rate estimate (direction d-5fd69fe1)
# ---------------------------------------------------------------------------
# The warm pool ships dark and is unexercised in prod (pool_hit_rate=0), so no
# operator will enable it for a project without evidence it will pay off. This
# is an OFFLINE, READ-ONLY estimate of the pool hit rate a project WOULD achieve
# at a candidate target size, derived purely from durable data we already record
# (per-task resolved account + dispatch timestamps on recent terminal tasks).
#
# It is deliberately a COARSE counterfactual, not a discrete-event simulation: a
# defensible number grounded in the real arrival pattern is more useful than a
# precise-looking model that is hard to trust. The model replays the per-account
# dispatch sequence over a small simulated idle pool:
#   * ``target_size`` idle members are spread across the project's demand-active
#     accounts (demand-weighted even split), mirroring how the maintainer steers
#     a small pool across a project's account mix (reconcile_project_pool /
#     _pick_demand_weighted_account). With ``account_retarget`` ON, a single
#     shared pool of ``target_size`` members can serve ANY account (the
#     secret-manifest retarget path), so the slots are not account-pinned.
#   * A dispatch is a likely WARM HIT iff a compatible member is plausibly idle
#     at that moment. Serving a member leases it; a replacement is ready only
#     after ``replenish_seconds`` (the maintainer interval) — so a tight arrival
#     burst that exceeds the idle supply spills to cold dispatch.
#   * A member left idle past ``idle_ttl_seconds`` is reaped and re-provisioned
#     (a ``replenish_seconds`` gap), so a longer TTL keeps sparse-but-steady
#     demand warm while a short TTL churns it cold.
# NOTHING here touches the dispatch/claim path or provisions anything — it reads
# history and computes a number.


def _estimate_pool_hit_rate(
    dispatches: list[tuple[str | None, datetime | None]],
    *,
    target_size: int,
    replenish_seconds: float,
    idle_ttl_seconds: float,
    account_retarget: bool = False,
) -> Dict[str, Any]:
    """Coarse counterfactual: pool hit rate a candidate ``target_size`` would yield.

    ``dispatches`` is a list of ``(account_label, dispatched_at)`` tuples (the
    account is the resolved LLM account the dispatch was served for; use
    :func:`app.warm_backlog_reuse._account_label` so ``None`` collapses to the
    unattributed bucket). Entries without a timestamp are ignored. Pure and
    DB-free so it is unit-testable over synthetic histories.

    Returns ``estimated_hit_rate`` (None only when there are no timestamped
    dispatches), the warm-hit count, and a ``by_account`` breakdown. Works in
    seconds relative to the first dispatch, so it is timezone-agnostic.
    """
    items = [(label, ts) for (label, ts) in dispatches if ts is not None]
    total = len(items)
    target_size = max(0, int(target_size))
    replenish = max(0.0, float(replenish_seconds))
    ttl = max(0.0, float(idle_ttl_seconds))

    if total == 0:
        return {
            "target_size": target_size,
            "total_dispatches": 0,
            "estimated_warm_hits": 0,
            "estimated_hit_rate": None,
            "by_account": {},
            "account_retarget": account_retarget,
        }

    items.sort(key=lambda x: x[1])
    t0 = items[0][1]
    seq = [(label, (ts - t0).total_seconds()) for label, ts in items]

    from collections import Counter

    demand = Counter(label for label, _ in seq)
    # Demand-active accounts, busiest first — so a pool smaller than the account
    # count covers the heaviest-demand accounts (mirrors the maintainer's
    # demand-weighted water-fill steering).
    accounts = [a for a, _ in demand.most_common()]
    n = len(accounts)

    # Allocate slots. Each slot is its next-available time (relative seconds);
    # all members start ready at t0 (pool pre-warmed at window start).
    alloc: Dict[str | None, int] = dict.fromkeys(accounts, 0)
    if account_retarget:
        shared_slots = [0.0] * target_size
    else:
        if n > 0 and target_size > 0:
            base = target_size // n
            rem = target_size % n
            for i, a in enumerate(accounts):
                alloc[a] = base + (1 if i < rem else 0)
        slots: Dict[str | None, list[float]] = {a: [0.0] * alloc[a] for a in accounts}

    def _serve(slot_list: list[float], t: float) -> bool:
        """Lease the first compatible idle member at time ``t``; True iff warm."""
        for i, free_at in enumerate(slot_list):
            fa = free_at
            # Reap + re-provision cycles for a member left idle past the TTL.
            # cycle = ttl + replenish > 0 (ttl > 0), so this always terminates.
            if ttl > 0:
                cycle = ttl + replenish
                while fa <= t and (t - fa) >= ttl:
                    fa += cycle
            slot_list[i] = fa
            if fa <= t:
                # Idle and ready — lease it; a replacement is ready after replenish.
                slot_list[i] = t + replenish
                return True
        return False

    hits_by_acct: Dict[str | None, int] = dict.fromkeys(accounts, 0)
    for label, t in seq:
        if account_retarget:
            if _serve(shared_slots, t):
                hits_by_acct[label] = hits_by_acct.get(label, 0) + 1
        else:
            sl = slots.get(label)
            if sl and _serve(sl, t):
                hits_by_acct[label] += 1

    total_hits = sum(hits_by_acct.values())
    by_account: Dict[str, Any] = {}
    for a in accounts:
        d = demand[a]
        h = hits_by_acct.get(a, 0)
        by_account[str(a)] = {
            "total_dispatches": d,
            "estimated_warm_hits": h,
            "estimated_hit_rate": round(h / d, 4) if d else None,
            "allocated_slots": target_size if account_retarget else alloc[a],
        }
    return {
        "target_size": target_size,
        "total_dispatches": total,
        "estimated_warm_hits": total_hits,
        "estimated_hit_rate": round(total_hits / total, 4) if total else None,
        "by_account": by_account,
        "account_retarget": account_retarget,
    }


_POOL_ESTIMATE_ASSUMPTIONS = (
    "Coarse offline estimate, NOT a measurement: replays recent per-account "
    "dispatch timestamps over a simulated idle pool of the candidate target "
    "size. A dispatch counts as a likely warm hit when a compatible idle member "
    "would plausibly be ready — leasing a member costs one replenish interval "
    "before a replacement is ready, and a member idle past the TTL is reaped and "
    "re-provisioned. Assumes the pool was pre-warmed at the window start and "
    "ignores provider/model compatibility beyond the resolved account. The "
    "account-retarget scenario lets any idle member serve any account."
)


async def estimate_pool_hit_rate(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    target_size: int,
    window_days: int = _AXIS_WINDOW_DAYS,
    runtime_config: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Offline pool hit-rate estimate for a project at a candidate target size.

    Loads recent terminal tasks via :func:`_load_recent_tasks` (already carries
    ``vm_dispatched_at`` + ``context``), buckets each dispatch by its resolved
    LLM account, and runs the pure :func:`_estimate_pool_hit_rate` counterfactual
    twice: account-pinned (the maintainer's per-account spread) and with
    account-retarget on (any idle member serves any account). Read-only — it
    provisions nothing and never touches the dispatch path.

    ``replenish_seconds`` and ``idle_ttl_seconds`` are read from runtime config
    (the maintainer interval and the pool idle TTL) but threaded through as
    explicit params to the pure helper for deterministic testing.
    """
    from app.max_plan_pool import get_task_llm_account_id
    from app.warm_backlog_reuse import _account_label

    if runtime_config is None:
        from app.shared_state import runtime_config as _rc

        runtime_config = _rc

    try:
        replenish = int(
            runtime_config.get("warm_pool_maintainer_interval_seconds", 120) or 120
        )
    except (TypeError, ValueError):
        replenish = 120
    try:
        idle_ttl = int(runtime_config.get("warm_pool_idle_ttl_seconds", 1800) or 1800)
    except (TypeError, ValueError):
        idle_ttl = 1800

    tasks = await _load_recent_tasks(session, project_id, window_days=window_days)
    dispatches: list[tuple[str | None, datetime | None]] = []
    for t in tasks:
        if t.vm_dispatched_at is None:
            continue
        dispatches.append(
            (_account_label(get_task_llm_account_id(t)), t.vm_dispatched_at)
        )

    pinned = _estimate_pool_hit_rate(
        dispatches,
        target_size=target_size,
        replenish_seconds=replenish,
        idle_ttl_seconds=idle_ttl,
        account_retarget=False,
    )
    retarget = _estimate_pool_hit_rate(
        dispatches,
        target_size=target_size,
        replenish_seconds=replenish,
        idle_ttl_seconds=idle_ttl,
        account_retarget=True,
    )
    return {
        "project_id": str(project_id),
        "target_size": int(max(0, target_size)),
        "window_days": window_days,
        "replenish_seconds": replenish,
        "idle_ttl_seconds": idle_ttl,
        "total_dispatches": pinned["total_dispatches"],
        "estimated_warm_hits": pinned["estimated_warm_hits"],
        "estimated_hit_rate": pinned["estimated_hit_rate"],
        "by_account": pinned["by_account"],
        "account_retarget_scenario": {
            "estimated_warm_hits": retarget["estimated_warm_hits"],
            "estimated_hit_rate": retarget["estimated_hit_rate"],
        },
        "assumptions": _POOL_ESTIMATE_ASSUMPTIONS,
    }


def compute_vm_cost_by_task_type(tasks: list[Task]) -> list[Dict[str, Any]]:
    """Per-task-type VM cost rollup over the given tasks.

    Buckets each task by ``task.first_payload.get("type", "unspecified")``
    and accumulates :func:`get_task_total_vm_cost_usd` per bucket. Mirrors
    the structure of :func:`compute_llm_cost_by_task_type` so the two surfaces
    reconcile via the same task set. Returns a list sorted by ``total_cost_usd``
    descending so the heaviest VM-cost type is first.

    .. note::
        ``cost_per_merge`` here divides by ``merged_count`` (all merged completions
        via :func:`is_merged_completion`), which is consistent with the VM-cost
        ``cost_per_merged_pr`` axis. The sibling
        :func:`compute_llm_cost_by_task_type` uses ``verified_merged_count``
        (CI-gated subset via :func:`is_verified_merge`) for its ``cost_per_merge``
        field. Callers comparing ``cost_per_merge`` across the two surfaces should
        be aware of this denominator difference.
    """
    from collections import defaultdict

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "merged_count": 0, "cost_usd": 0.0}
    )
    total_cost = 0.0
    parent_ids = _rollup_parent_ids(tasks)

    for t in tasks:
        task_type = (t.first_payload or {}).get("type") or "unspecified"
        merged = is_merged_completion(t) and t.id not in parent_ids
        vm_cost = get_task_total_vm_cost_usd(t)
        b = buckets[task_type]
        b["task_count"] += 1
        if merged:
            b["merged_count"] += 1
        b["cost_usd"] += vm_cost
        total_cost += vm_cost

    def _share(cost: float) -> float | None:
        if total_cost <= 0:
            return None
        return round(cost / total_cost, 4)

    def _cost_per_merge(cost: float, merged: float) -> float | None:
        if cost <= 0 or merged <= 0:
            return None
        return round(cost / merged, 6)

    return [
        {
            "task_type": task_type,
            "task_count": int(b["task_count"]),
            "merged_count": int(b["merged_count"]),
            "total_cost_usd": round(b["cost_usd"], 6),
            "share_of_total_cost": _share(b["cost_usd"]),
            "cost_per_merge": _cost_per_merge(b["cost_usd"], b["merged_count"]),
        }
        for task_type, b in sorted(
            buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]


# ---------------------------------------------------------------------------
# Per-effort-tier and per-actual-model efficiency rollup (operator payoff view)
# ---------------------------------------------------------------------------
# Agent runs are scaled along an effort ladder (low->haiku, medium->sonnet,
# high/max->opus) where higher effort selects a more capable AND more expensive
# model. The existing cost surfaces break spend down by task TYPE and give a
# per-MODEL cost breakdown, but neither answers the direction question: does
# paying for a higher effort tier (or a pricier resolved model) actually earn
# its money in LANDED PRs? This rollup attributes LLM token cost — reusing the
# same LLM_TOKEN_PRICING table and the same is_merged_completion "shipped"
# predicate the other cost surfaces use, so its totals reconcile with
# compute_llm_cost_breakdown — to BOTH:
#   * the REQUESTED effort tier (task.first_payload["effort"]), and
#   * the ACTUAL resolved (provider, model),
# alongside run count, landed/merge rate, cost-per-merge, and token volume.
#
# Honesty rules (mirrors compute_llm_cost_breakdown):
#   * Unpriced (provider, model) combos contribute 0 cost but are still counted
#     in run/token totals and surfaced in ``unpriced_combos`` +
#     ``unpriced_coverage_fraction`` — never silently folded into priced totals.
#   * Requested-vs-actual divergence is made explicit: each effort row carries a
#     ``resolved_models`` breakdown (which models a given requested tier actually
#     ran on), and the top-level ``fallbacks`` block aggregates the
#     ``context.model_fallback`` downgrade records (preview->GA etc.).

# Canonical effort-ladder ordering for stable, low->max row sorting. Unknown /
# unspecified efforts sort last (alphabetically among themselves).
_EFFORT_TIER_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "max": 3}
_EFFORT_UNSPECIFIED = "unspecified"
# Cap on the surfaced model-fallback divergence list so a noisy window never
# bloats the digest payload.
_FALLBACK_LIST_CAP = 20


def _new_efficiency_bucket() -> Dict[str, Any]:
    return {
        "run_count": 0,
        "landed_count": 0,
        # Verified landings only (is_verified_merge) — degraded fall-open merges
        # count in landed_count but NOT here, mirroring compute_llm_cost_breakdown.
        "verified_landed_count": 0,
        # Tasks whose type can land a PR (evaluation_strategy=ci_and_review).
        # landed_rate / verified_landed_rate divide by this, not run_count, so
        # review/plan/research tasks that can never open a PR don't deflate the rate.
        "pr_eligible_run_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "llm_call_count": 0,
        "cost_usd": 0.0,
        # Among token-bearing ("priceable") attempt-rows only — mirrors
        # compute_llm_cost_breakdown's priced/unpriced attempt accounting so
        # coverage fractions reconcile.
        "priced_run_count": 0,
        "unpriced_run_count": 0,
    }


def _finalize_efficiency_bucket(b: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    run_count = int(b["run_count"])
    landed_count = int(b["landed_count"])
    verified_landed_count = int(b["verified_landed_count"])
    pr_eligible_run_count = int(b["pr_eligible_run_count"])
    cost = round(float(b["cost_usd"]), 6)
    priceable = int(b["priced_run_count"]) + int(b["unpriced_run_count"])
    out: Dict[str, Any] = {
        "run_count": run_count,
        # PR-eligible denominator: only tasks that can land a PR are counted.
        # Review/plan/research tasks inflate run_count but can never contribute
        # to the numerator, so landed_rate uses this narrower denominator.
        "pr_eligible_run_count": pr_eligible_run_count,
        "landed_count": landed_count,
        "landed_rate": (
            round(landed_count / pr_eligible_run_count, 4)
            if pr_eligible_run_count
            else None
        ),
        # Verified payoff alongside the raw landed figures: degraded (no-CI
        # fall-open) merges are kept in landed_count/landed_rate but excluded here.
        "verified_landed_count": verified_landed_count,
        "verified_landed_rate": (
            round(verified_landed_count / pr_eligible_run_count, 4)
            if pr_eligible_run_count
            else None
        ),
        "total_cost_usd": cost,
        # Cost-per-merge is the headline payoff number: priced LLM dollars spent
        # per VERIFIED PR that landed, so degraded fall-open merges do not make
        # spend look cheaper. None when nothing verified-landed (no denominator).
        "cost_per_merge": (
            round(cost / verified_landed_count, 6) if verified_landed_count else None
        ),
        "input_tokens": int(b["input_tokens"]),
        "output_tokens": int(b["output_tokens"]),
        "llm_call_count": int(b["llm_call_count"]),
        "priced_run_count": int(b["priced_run_count"]),
        "unpriced_run_count": int(b["unpriced_run_count"]),
        "unpriced_coverage_fraction": (
            round(b["priced_run_count"] / priceable, 4) if priceable else None
        ),
    }
    out.update(extra)
    return out


def compute_effort_model_efficiency(
    tasks: list[Task],
    runtime_config: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Per-effort-tier and per-actual-model efficiency rollup over finished tasks.

    Pure function — no DB calls. For each task it resolves the EFFECTIVE routing
    effort — the same effort the dispatcher resolves to select the model — via
    :func:`max_plan_pool.resolve_effective_effort`, reading the SQL-projected
    ``task.first_payload`` (type + payload effort) and ``task.context`` (never
    ``task.payloads``, which is deferred under the digest projection). This
    attributes EVERY terminal task to the tier that actually drove its cost —
    not just the scope-aware implementation types that stamp an explicit payload
    effort — so review/strategic (opus) and ops (haiku) tasks land in their
    category tier instead of collapsing into ``unspecified``. Explicit payload
    effort still wins as the most-specific layer, so previously-bucketed tasks
    are unaffected. ``runtime_config`` defaults to the shared
    :data:`shared_state.runtime_config` (mirrors :func:`estimate_pool_hit_rate`).

    It then splits the task across its banked
    attempt history (:func:`_task_llm_attempts`) so each attempt's tokens are
    priced at the (provider, model) that attempt actually ran on — exactly as
    :func:`compute_llm_cost_breakdown` does. A task that retried on a different
    model therefore lands its tokens/cost in each attempt's own model bucket, so
    the total AND the per-model breakdown reconcile with the sibling cost surface.
    The landed/merge status comes from :func:`is_merged_completion` (raw landings)
    and :func:`is_verified_merge` (verified landings). LLM cost is priced with the
    shared :data:`LLM_TOKEN_PRICING` table.

    Run counts and landed counts stay PER-TASK — credited once per task (and once
    per distinct model bucket the task touched), never once per attempt-row — so a
    multi-attempt task never inflates run/landed counts.

    Returns a dict with:
      - ``by_effort``: rows keyed by effective routing effort tier (low->max,
        then ``unspecified`` for tasks with no projected first payload at all).
        Each row carries run count, landed count + rate,
        verified landed count + rate, priced LLM cost, cost-per-merge (over the
        verified count), token volume, priced/unpriced coverage, and a
        ``resolved_models`` breakdown exposing which actual models that requested
        tier ran on (requested-vs-actual divergence).
      - ``by_model``: rows keyed by actual (provider, model) with a ``priced``
        flag, sorted by cost desc. Unpriced combos appear with 0 cost.
      - ``totals``: aggregate run/landed/verified/cost/token totals + priced/
        unpriced coverage.
      - ``unpriced_combos``: capped list of ``provider:model`` strings with
        non-zero tokens but no pricing entry (never folded into priced cost).
      - ``fallbacks``: aggregate of ``context.model_fallback`` downgrade records
        (requested != effective resolved model), with a capped per-combo list.
    """
    from collections import defaultdict

    from app import max_plan_pool

    if runtime_config is None:
        from app.shared_state import runtime_config as _rc

        runtime_config = _rc

    effort_buckets: Dict[str, Dict[str, Any]] = defaultdict(_new_efficiency_bucket)
    # effort -> (provider, model) -> {run_count, landed_count}
    effort_models: Dict[str, Dict[tuple[str, str], Dict[str, Any]]] = defaultdict(dict)
    model_buckets: Dict[tuple[str, str], Dict[str, Any]] = defaultdict(
        _new_efficiency_bucket
    )
    priced_model_keys: set[tuple[str, str]] = set()
    unpriced_combos: list[str] = []

    totals = _new_efficiency_bucket()

    # Model-fallback (requested-vs-actual divergence) aggregation.
    fallback_total = 0
    fallback_combos: Dict[tuple[str, str, str, str], int] = defaultdict(int)

    # Rollup parents get is_merged_completion=True alongside all their children
    # when a rollup lands; exclude them so each rollup counts as N child landings,
    # not N+1.
    _parent_ids = _rollup_parent_ids(tasks)

    def _accumulate_attempt(
        b: Dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        call_count: int,
        cost: float,
        priced: bool,
    ) -> None:
        b["input_tokens"] += int(input_tokens)
        b["output_tokens"] += int(output_tokens)
        b["llm_call_count"] += int(call_count)
        b["cost_usd"] += cost
        # Each token-bearing attempt-row is one priceable unit (mirrors the
        # sibling's per-attempt priced/unpriced accounting).
        if priced:
            b["priced_run_count"] += 1
        else:
            b["unpriced_run_count"] += 1

    for t in tasks:
        landed = is_merged_completion(t) and t.id not in _parent_ids
        verified = is_verified_merge(t) and t.id not in _parent_ids
        # Bucket by the EFFECTIVE routing effort the dispatcher resolves (the
        # tier that actually selected the model and drove cost), not just the
        # explicit payload effort that only scope-aware types carry. Read the
        # SQL-projected first payload + context — never t.payloads (deferred
        # under the digest load_only projection). 'unspecified' now holds only
        # genuinely unresolvable tasks: those with no projected first payload.
        first_payload = t.first_payload or {}
        if first_payload:
            effort_resolved = max_plan_pool.resolve_effective_effort(
                first_payload, t.context, first_payload.get("type"), runtime_config
            )
            effort = str(effort_resolved) if effort_resolved else _EFFORT_UNSPECIFIED
        else:
            effort = _EFFORT_UNSPECIFIED
        eff_bucket = effort_buckets[effort]

        # Distinct model buckets this task contributed a token-bearing row to.
        # Per-task run/landed credit is applied once per distinct bucket below,
        # never per attempt-row.
        task_model_keys: set[tuple[str, str]] = set()

        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            if not provider or not model:
                provider, model = "unattributed", "unknown"
            else:
                provider, model = provider.lower(), model.lower()
            key = (provider, model)

            pricing = LLM_TOKEN_PRICING.get(key)
            priced = pricing is not None
            if priced:
                input_rate, output_rate = pricing
                cost = _price_llm_attempt(
                    input_tokens,
                    output_tokens,
                    cache_creation,
                    cache_read,
                    input_rate,
                    output_rate,
                    provider=provider,
                    model=model,
                )
                priced_model_keys.add(key)
            else:
                cost = 0.0
                combo = f"{provider}:{model}"
                if (
                    combo not in unpriced_combos
                    and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
                ):
                    unpriced_combos.append(combo)

            _accumulate_attempt(
                eff_bucket, input_tokens, output_tokens, call_count, cost, priced
            )
            _accumulate_attempt(
                model_buckets[key],
                input_tokens,
                output_tokens,
                call_count,
                cost,
                priced,
            )
            _accumulate_attempt(
                totals, input_tokens, output_tokens, call_count, cost, priced
            )
            task_model_keys.add(key)

        # Per-task crediting. A task that burned no LLM tokens still counts as a
        # run (and lands its merge) under its resolved final (provider, model) so
        # it is not dropped from the model surface.
        if task_model_keys:
            credit_keys = task_model_keys
        elif t.provider and t.model:
            credit_keys = {(t.provider.lower(), t.model.lower())}
        else:
            credit_keys = {("unattributed", "unknown")}

        # Effort is a TASK-level attribute: credit its bucket and the grand total
        # once per task. Model buckets are credited once per distinct bucket the
        # task touched, so a multi-attempt/multi-model task never inflates counts.
        task_type_str = first_payload.get("type") if first_payload else None
        pr_eligible = is_pr_eligible_task_type(task_type_str or "")
        eff_bucket["run_count"] += 1
        totals["run_count"] += 1
        if pr_eligible:
            eff_bucket["pr_eligible_run_count"] += 1
            totals["pr_eligible_run_count"] += 1
        if landed:
            eff_bucket["landed_count"] += 1
            totals["landed_count"] += 1
        if verified:
            eff_bucket["verified_landed_count"] += 1
            totals["verified_landed_count"] += 1
        for key in credit_keys:
            mb = model_buckets[key]
            mb["run_count"] += 1
            if pr_eligible:
                mb["pr_eligible_run_count"] += 1
            if landed:
                mb["landed_count"] += 1
            if verified:
                mb["verified_landed_count"] += 1
            em = effort_models[effort].setdefault(
                key, {"run_count": 0, "landed_count": 0, "pr_eligible_run_count": 0}
            )
            em["run_count"] += 1
            if pr_eligible:
                em["pr_eligible_run_count"] += 1
            if landed:
                em["landed_count"] += 1

        fallback = (t.context or {}).get("model_fallback")
        if isinstance(fallback, dict) and fallback.get("requested"):
            fallback_total += 1
            fcombo = (
                str(fallback.get("requested") or ""),
                str(fallback.get("effective") or ""),
                str(fallback.get("provider") or ""),
                str(fallback.get("reason") or ""),
            )
            fallback_combos[fcombo] += 1

    def _resolved_models(effort: str) -> list[Dict[str, Any]]:
        rows = [
            {
                "provider": prov,
                "model": mdl,
                "run_count": int(v["run_count"]),
                "pr_eligible_run_count": int(v.get("pr_eligible_run_count", 0)),
                "landed_count": int(v["landed_count"]),
                "priced": (prov, mdl) in priced_model_keys,
            }
            for (prov, mdl), v in effort_models.get(effort, {}).items()
        ]
        rows.sort(key=lambda r: (-r["run_count"], r["provider"], r["model"]))
        return rows

    by_effort = [
        _finalize_efficiency_bucket(
            effort_buckets[effort],
            effort=effort,
            resolved_models=_resolved_models(effort),
        )
        for effort in sorted(
            effort_buckets, key=lambda e: (_EFFORT_TIER_ORDER.get(e, 99), e)
        )
    ]
    by_model = [
        _finalize_efficiency_bucket(
            model_buckets[key],
            provider=key[0],
            model=key[1],
            priced=key in priced_model_keys,
        )
        for key in sorted(
            model_buckets, key=lambda k: (-model_buckets[k]["cost_usd"], k[0], k[1])
        )
    ]
    fallback_list = [
        {
            "requested": req,
            "effective": eff,
            "provider": prov,
            "reason": reason,
            "count": count,
        }
        for (req, eff, prov, reason), count in sorted(
            fallback_combos.items(), key=lambda kv: (-kv[1], kv[0])
        )[:_FALLBACK_LIST_CAP]
    ]

    return {
        "by_effort": by_effort,
        "by_model": by_model,
        "totals": _finalize_efficiency_bucket(totals),
        "unpriced_combos": unpriced_combos,
        "fallbacks": {
            "total": fallback_total,
            "combos": fallback_list,
        },
    }


def _compute_vm_cold_start_p50_ms(tasks: list[Task]) -> float | None:
    samples = [int(t.vm_cold_start_ms) for t in tasks if t.vm_cold_start_ms is not None]
    if not samples:
        return None
    return round(float(statistics.median(samples)), 2)


def _compute_vm_reuse_rate(tasks: list[Task]) -> float | None:
    """warm / participating. None when no VMs participated.

    Reads ``context['_timing']['warm_start']`` — the same source the
    ``/pipeline-efficiency`` endpoint uses, and the canonical signal stamped by
    the warm-claim / backlog-reuse path
    (``routers/agent_warm_continuity._finalize_warm_claim``). Cold dispatch
    writes ``vm_warm_start=False``; the warm-claim path writes both
    ``vm_warm_start=True`` and ``_timing['warm_start']=True``, so the column and
    ``_timing`` now agree on warm tasks. The metric keys on ``_timing`` because
    it is the agent-authoritative source. A task participates when it has a
    non-empty ``_timing`` dict OR a recorded ``vm_provisioned_at``.
    """
    warm = 0
    total = 0
    for t in tasks:
        timing = (t.context or {}).get("_timing") or {}
        if timing or t.vm_provisioned_at is not None:
            total += 1
            if timing.get("warm_start") is True:
                warm += 1
    if not total:
        return None
    return round(warm / total, 4)


def _compute_warm_start_origin_breakdown(
    tasks: list[Task],
) -> Dict[str, Any] | None:
    """Break the warm-start population down by claim origin over the window.

    Reads ``context['_timing']['warm_claim_origin']`` for every task whose
    ``_timing['warm_start'] is True`` and buckets it into one of three origins:
    ``warm_pool`` (a pre-provisioned pool member served the dispatch),
    ``backlog_reuse`` (a finishing VM picked up an unrelated QUEUED task from the
    same project's backlog), and ``chain_continuity`` (the PR-chain follow-on
    path, origin ``next_role_endpoint`` or legacy-unset).

    The aggregate :func:`_compute_vm_reuse_rate` sums every origin into one
    number; this companion isolates the POOL-ORIGIN share so an operator who
    enables the pool can confirm it lifted reuse above the floor (the pool's
    entire reason to exist) rather than reading lift that chain-continuity or
    backlog-reuse actually contributed. Returns None when no warm starts exist
    in the window (nothing to attribute). ``pool_origin_share`` is the pool
    bucket as a fraction of all warm starts (``None`` only when ``warm_total``
    is 0, which already returns None above).
    """
    from app.warm_pool import WARM_POOL_ORIGIN

    warm_pool = 0
    backlog_reuse = 0
    chain_continuity = 0
    for t in tasks:
        timing = (t.context or {}).get("_timing") or {}
        if timing.get("warm_start") is not True:
            continue
        origin = timing.get("warm_claim_origin")
        if origin == WARM_POOL_ORIGIN:
            warm_pool += 1
        elif origin == "backlog_reuse":
            backlog_reuse += 1
        else:
            # "next_role_endpoint" or legacy-unset — the chain-continuity path.
            chain_continuity += 1

    warm_total = warm_pool + backlog_reuse + chain_continuity
    if not warm_total:
        return None
    return {
        "warm_pool": warm_pool,
        "backlog_reuse": backlog_reuse,
        "chain_continuity": chain_continuity,
        "warm_total": warm_total,
        "pool_origin_share": round(warm_pool / warm_total, 4),
    }


def _compute_median_api_calls_per_task(tasks: list[Task]) -> float | None:
    counts: list[int] = []
    for t in tasks:
        if t.api_call_summary is None and t.llm_call_count_banked is None:
            continue
        _, _, call_count = get_task_total_llm_totals(t)
        counts.append(int(call_count))
    if not counts:
        return None
    return round(float(statistics.median(counts)), 2)


def _compute_median_review_iterations(tasks: list[Task]) -> float | None:
    iters: list[int] = []
    for t in tasks:
        ctx = t.context or {}
        loop = ctx.get("review_loop")
        if isinstance(loop, dict):
            n = loop.get("iteration")
            if isinstance(n, int) and n >= 0:
                iters.append(n)
        elif has_merge_unblock_signal(t):
            # Review was skipped (e.g. auto-approved small diff) — counts as 0 iterations.
            # Excluding these tasks inflates the median whenever the pipeline gets more
            # efficient by skipping review, inverting the signal.
            iters.append(0)
    if not iters:
        return None
    return round(float(statistics.median(iters)), 2)


def _parse_stage_ts(val: Any) -> datetime | None:
    """Parse a stage timestamp (datetime or ISO string) to a naive datetime."""
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo is not None else val
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        except (ValueError, TypeError):
            return None
    return None


def _compute_cold_start_breakdown_p50(
    tasks: list[Task],
) -> Dict[str, float | None] | None:
    """Per-stage p50 ms breakdown of the VM cold-start interval.

    Returns None when no tasks have any stage data.  Individual stage values
    are None when fewer than one sample exists for that stage.
    """
    stage_samples: Dict[str, list[float]] = {
        label: [] for _, _, label in _COLD_START_STAGE_PAIRS
    }
    for t in tasks:
        stages: Dict[str, Any] = dict((t.context or {}).get("_cold_start_stages") or {})
        if not stages:
            continue
        if t.vm_dispatched_at is not None:
            stages["vm_dispatched_at"] = t.vm_dispatched_at
        for src_key, dst_key, label in _COLD_START_STAGE_PAIRS:
            if src_key not in stages or dst_key not in stages:
                continue
            t_src = _parse_stage_ts(stages[src_key])
            t_dst = _parse_stage_ts(stages[dst_key])
            if t_src is None or t_dst is None:
                continue
            delta_ms = (t_dst - t_src).total_seconds() * 1000
            if 0 <= delta_ms < 300_000:  # sanity: cap at 5 min per stage
                stage_samples[label].append(delta_ms)
    if not any(stage_samples.values()):
        return None
    return {
        label: round(float(statistics.median(stage_samples[label])), 2)
        if stage_samples[label]
        else None
        for _, _, label in _COLD_START_STAGE_PAIRS
    }



def _find_agent_src() -> str | None:
    """Locate the agent/src directory for prompt.ts static-size introspection.

    Search order mirrors test_agent_branch_invariants._resolve_agent_src_root:
      1. AGENT_SRC_ROOT env override.
      2. /agent/src — Docker volume-mount path used by scripts/test-backend.sh.
      3. Host filesystem relative to this file (backend/app -> repo root -> agent/src).

    Returns the first existing directory path, or None.
    """
    candidates: list[Path] = []
    env_override = os.environ.get("AGENT_SRC_ROOT")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path("/agent/src"))
    candidates.append(Path(__file__).resolve().parents[2] / "agent" / "src")
    for c in candidates:
        if c.is_dir():
            return str(c)
    return None


def _extract_ts_template_literal_length(src: str, const_name: str) -> int | None:
    """Find a TypeScript `const NAME = backtick...backtick` and return its source char count.

    Scans from the opening backtick to the matching unescaped closing backtick,
    advancing by 2 on any backslash escape sequence so that escaped backticks and
    escaped backslashes inside the literal do not prematurely terminate the scan.
    Returns None if the constant is not found or the literal is unterminated.
    """
    pattern = re.compile(r"\bconst\s+" + re.escape(const_name) + r"\s*=\s*`")
    m = pattern.search(src)
    if not m:
        return None
    pos = m.end()
    start = pos
    n = len(src)
    while pos < n:
        ch = src[pos]
        if ch == "\\":
            pos += 2
        elif ch == "`":
            return pos - start
        else:
            pos += 1
    return None


def _extract_ts_plain_string_length(src: str, const_name: str) -> int | None:
    """Find a TypeScript `const NAME = '...'` or `"..."` and return its source char count.

    Walks forward from the opening quote, honouring backslash escape sequences.
    Returns None if the constant declaration is not found or the string is unterminated.
    """
    pattern = re.compile(r"\bconst\s+" + re.escape(const_name) + r"""\s*=\s*(['"])""")
    m = pattern.search(src)
    if not m:
        return None
    quote = m.group(1)
    pos = m.end()
    start = pos
    n = len(src)
    while pos < n:
        ch = src[pos]
        if ch == "\\":
            pos += 2
        elif ch == quote:
            return pos - start
        else:
            pos += 1
    return None


_PROMPT_STATIC_CONST_NAMES: tuple[str, ...] = (
    "SHARED_LIFECYCLE_PREAMBLE",
    "BLUEPRINT_CHECK_INTRO",
    "SHARED_LIFECYCLE_REMAINDER",
    "READ_THE_FAILURE_MESSAGE",
    "SIZE_GUARDRAIL_GUIDANCE",
    "PRIOR_ATTEMPT_DIVERGENCE",
    "BEYOND_SCOPE_REPORTING",
    "EDGES_CONSIDERED_GUIDANCE",
    "PLAYBOOK_GUIDANCE",
)

# Plain-string constants (single- or double-quoted) that are also part of the
# static prompt footprint and are not template literals.
_PROMPT_STATIC_PLAIN_CONST_NAMES: tuple[str, ...] = ("DEFAULT_TOOL_GUIDANCE",)

# Template-literal constants from agent/src/prompts/task-type-focus.ts that
# contribute to the static prompt footprint. MIGRATE_TASK_FOCUS is omitted
# because it is a function-call assignment (buildMigrateTaskFocus()) rather than
# a direct template literal and cannot be measured by _extract_ts_template_literal_length.
_TASK_TYPE_FOCUS_CONST_NAMES: tuple[str, ...] = (
    "REFACTOR_TASK_FOCUS",
    "QA_TASK_FOCUS",
    "CREATE_TASK_FOCUS",
    "FIX_TASK_FOCUS",
    "DOCS_TASK_FOCUS",
    "DEPS_TASK_FOCUS",
)


def _compute_llm_cache_hit_rate(tasks: list[Task]) -> float | None:
    """LLM prompt cache hit fraction: cache-read input tokens / total input tokens.

    Computed from the same per-attempt token data as
    :func:`compute_llm_cost_breakdown` so the two stay consistent.
    Returns ``None`` when there are no input tokens in the window.
    """
    total_input = 0
    total_cache_read = 0
    for t in tasks:
        for (
            _provider,
            _model,
            input_tokens,
            _output_tokens,
            _call_count,
            _cache_creation,
            cache_read,
        ) in _task_llm_attempts(t):
            if input_tokens > 0:
                total_input += int(input_tokens)
                total_cache_read += int(cache_read)
    if total_input <= 0:
        return None
    return round(total_cache_read / total_input, 4)


def _compute_prompt_cache_read_share(tasks: list[Task]) -> float | None:
    """Prompt-cache lever's direct effect: fleet rollup of per-task-type cache_read_share.

    Sums ``total_cache_read_input_tokens`` and ``total_input_tokens`` across every
    task-type bucket produced by :func:`compute_llm_cost_by_task_type` and returns
    their token-weighted ratio. Sourcing from that rollup (rather than re-walking
    raw attempts like :func:`_compute_llm_cache_hit_rate`) guarantees the fleet
    number reconciles EXACTLY with the per-task-type cost table the operator reads:
    it is the token-weighted mean of the very ``cache_read_share`` values that
    table displays. This is the prompt-cache lever's baselined before/after signal.

    Returns ``None`` when the window carries no input tokens (no divide-by-zero),
    mirroring the ``None`` contract of the sibling cache axis.
    """
    total_input = 0
    total_cache_read = 0
    for row in compute_llm_cost_by_task_type(tasks):
        total_input += int(row.get("total_input_tokens") or 0)
        total_cache_read += int(row.get("total_cache_read_input_tokens") or 0)
    if total_input <= 0:
        return None
    return round(total_cache_read / total_input, 4)


def _compute_total_cost_per_verified_task(
    tasks: list[Task],
    compute_cost_by_task: Mapping[uuid.UUID, float] | None = None,
) -> float | None:
    """Total LLM + VM + ComputeJob cost / verified merge count.

    Mirrors the numerator aggregation of the existing cost axes (VM cost from
    :func:`get_task_total_vm_cost_usd` + LLM cost from :func:`_task_llm_cost`)
    and additionally folds in each task's attributed ComputeJob (GPU experiment)
    cost via ``compute_cost_by_task`` — a ``{task_id: compute_usd}`` map built by
    :func:`_load_compute_job_costs`. Without it, experiment compute spend is
    invisible to the cost/efficiency ranking (the axis this feeds). The map is
    optional so the pure per-task VM+LLM valuation is unchanged when omitted.

    Divides by the VERIFIED merge count — the subset of merged completions whose
    PR was gated by a real CI/test verdict, excluding degraded fall-open merges.
    Rollup parents are excluded so each rollup counts as N child landings, not
    N+1.

    Returns None when no verified merges exist in the window.
    """
    total_cost = 0.0
    verified = 0
    parent_ids = _rollup_parent_ids(tasks)
    compute_by_task = compute_cost_by_task or {}
    for t in tasks:
        total_cost += get_task_total_vm_cost_usd(t)
        llm_cost, _ = _task_llm_cost(t)
        total_cost += llm_cost
        total_cost += float(compute_by_task.get(t.id, 0.0))
        if is_verified_merge(t) and t.id not in parent_ids:
            verified += 1
    if not verified:
        return None
    return round(total_cost / verified, 4)


def _compute_verified_merge_count(tasks: list[Task]) -> int:
    """Return the verified merge count (is_verified_merge, rollup parents excluded).

    Companion to :func:`_compute_total_cost_per_verified_task` — stored alongside
    the axis value in each snapshot so :func:`read_baseline_with_drift` can
    suppress drift_pct when the denominator is below
    :data:`VERIFIED_TASK_DRIFT_MIN_COUNT`.
    """
    parent_ids = _rollup_parent_ids(tasks)
    return sum(
        1 for t in tasks if is_verified_merge(t) and t.id not in parent_ids
    )


# ---------------------------------------------------------------------------
# ComputeJob (GPU experiment) cost — grounding operator/compute spend so it is
# visible to the cost/efficiency ranking.
# ---------------------------------------------------------------------------
# Experiment compute runs as ComputeJob rows (own budget/cost lifecycle),
# entirely decoupled from Task VM/LLM cost. Until now efficiency_snapshot read
# NEITHER accrued_cost_usd NOR final_cost_usd, so GPU-job spend was invisible to
# the per-verified-task cost aggregate the ranking consumes. These helpers close
# that gap two ways, mirroring the LLM cost honesty pattern in
# llm_cost_attribution.py:
#   1. per-task compute cost feeds the total-cost-per-verified-task numerator.
#   2. an unpriced-coverage honesty block distinguishes a $0 that means "free"
#      (operator hardware we do not pay for, priced at the nominal rate) from a
#      $0 that means "UNPRICED" (no cloud rate, no operator rate — a real gap),
#      so an unpriced job is not silently read as free in the ranking.


def _compute_job_cost_usd(job) -> float:
    """Settled (``final_cost_usd``) cost when present, else live ``accrued_cost_usd``.

    ``0.0`` when neither is recorded. Matches the precedence used by the compute
    monitor and the spend-ceiling gate (final cost is authoritative once the job
    is terminal; accrued cost covers in-flight jobs).
    """
    if job.final_cost_usd is not None:
        return float(job.final_cost_usd)
    return float(job.accrued_cost_usd or 0.0)


def _compute_job_is_priced(job) -> bool:
    """True when the job's cost rests on a real rate basis; False when its $0/None
    cost reflects the ABSENCE of any rate (genuinely UNPRICED, not free).

    A job is PRICED when any of these hold:
      * it recorded a positive cost (final or accrued), or
      * a positive ``cost_rate_per_second`` was stamped at launch (a cloud rate), or
      * it is a runner-subprocess job and the operator-hardware nominal rate is
        active (``OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND > 0``).

    Otherwise it is UNPRICED: a $0 with no cloud rate and no operator rate, which
    must NOT be read as "free" in the cost/efficiency ranking. Mirrors the LLM
    ``unpriced_coverage_fraction`` honesty signal.
    """
    from app.compute_backends import (
        OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND,
        RUNNER_SUBPROCESS_BACKEND_NAME,
    )

    if _compute_job_cost_usd(job) > 0:
        return True
    rate = float((job.polling_metadata or {}).get("cost_rate_per_second") or 0.0)
    if rate > 0:
        return True
    # Operator-hardware nominal rate covers runner-subprocess jobs even at $0.
    return (
        job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        and OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND > 0
    )


async def _load_compute_job_costs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> tuple[dict[uuid.UUID, float], Dict[str, Any]]:
    """Per-task ComputeJob cost for the window plus an unpriced-coverage honesty block.

    Returns ``(by_task, attribution)``:

      * ``by_task`` maps an owning ``task_id`` to the summed compute cost of its
        jobs (:func:`_compute_job_cost_usd`). Fed into
        :func:`_compute_total_cost_per_verified_task` so experiment compute is
        visible to the cost/efficiency ranking. Jobs with no ``task_id`` cannot
        be attributed and contribute only to the honesty totals.
      * ``attribution`` mirrors the LLM ``unpriced_coverage`` pattern: total
        compute cost, cost-bearing job count, priced/unpriced split,
        ``unpriced_coverage_fraction`` and a capped ``unpriced_combos`` list, so
        a $0 that means "UNPRICED" is not mistaken for "free".

    A job is "cost-bearing" when it actually ran (``started_at`` set) or recorded
    a positive cost; not-yet-started zero-cost jobs are skipped entirely (they
    legitimately cost nothing and would dilute the coverage fraction). The window
    is bounded on ``updated_at`` to match the task-cost numerator's window.
    """
    from app.models import ComputeJob

    cutoff = _utcnow() - timedelta(days=window_days)
    stmt = (
        select(ComputeJob)
        .where(
            ComputeJob.project_id == project_id,
            ComputeJob.updated_at >= cutoff,
        )
        .options(
            load_only(
                ComputeJob.id,
                ComputeJob.task_id,
                ComputeJob.backend,
                ComputeJob.machine_type,
                ComputeJob.accelerator_type,
                ComputeJob.started_at,
                ComputeJob.accrued_cost_usd,
                ComputeJob.final_cost_usd,
                ComputeJob.polling_metadata,
                raiseload=True,
            )
        )
        .limit(5000)
    )
    jobs = list((await session.execute(stmt)).scalars().all())

    by_task: dict[uuid.UUID, float] = {}
    total_cost = 0.0
    attributed_cost = 0.0
    priced_count = 0
    unpriced_count = 0
    unpriced_combos: list[str] = []
    for job in jobs:
        cost = _compute_job_cost_usd(job)
        # Skip jobs that neither ran nor spent — not cost-bearing.
        if job.started_at is None and cost <= 0:
            continue
        total_cost += cost
        if job.task_id is not None:
            by_task[job.task_id] = by_task.get(job.task_id, 0.0) + cost
            attributed_cost += cost
        if _compute_job_is_priced(job):
            priced_count += 1
        else:
            unpriced_count += 1
            combo = f"{job.backend}:{job.machine_type or job.accelerator_type or 'none'}"
            if (
                combo not in unpriced_combos
                and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
            ):
                unpriced_combos.append(combo)

    cost_bearing = priced_count + unpriced_count
    unpriced_coverage_fraction: float | None = (
        round(priced_count / cost_bearing, 4) if cost_bearing else None
    )
    attribution: Dict[str, Any] = {
        "total_compute_cost_usd": round(total_cost, 6),
        "attributed_cost_usd": round(attributed_cost, 6),
        "unattributed_cost_usd": round(total_cost - attributed_cost, 6),
        "job_count": cost_bearing,
        "priced_job_count": priced_count,
        "unpriced_job_count": unpriced_count,
        "unpriced_coverage_fraction": unpriced_coverage_fraction,
        "unpriced_combos": unpriced_combos,
    }
    return by_task, attribution


def _compute_rework_cost_share(tasks: list[Task]) -> float | None:
    """Fraction of total LLM spend consumed by fix/re-attempt (rework) tasks.

    Delegates to :func:`compute_pr_rework_chain_breakdown` for the per-PR
    first-pass-vs-rework split and returns the aggregate
    ``rework_share_of_total_llm_cost``. Returns ``None`` when there are no
    priced tasks in the window (total LLM cost is zero).

    Rework is a direct cost multiplier — high rework means the same PR
    consumed 2-3x the tokens it should have. Making it driftable lets the
    direction drift detector catch cost regressions caused by review-loop
    inflation, which is orthogonal to routing savings.
    """
    breakdown = compute_pr_rework_chain_breakdown(tasks)
    return breakdown.get("rework_share_of_total_llm_cost")


def _compute_cost_telemetry_reconciliation(tasks: list[Task]) -> dict | None:
    """Self-check: reconcile LLM cost telemetry against an independent
    recomputation and log if the two paths disagree beyond the declared
    tolerance.

    Returns the reconciliation dict when tasks is non-empty and at least one
    task contributes priced LLM attempts; returns None when there are no tasks
    (the check would be vacuous) or when no tasks have a priced provider/model
    (zero-cost input with no pricing coverage is not a telemetry defect).
    """
    if not tasks:
        return None
    result = reconcile_llm_cost(tasks)
    # Only flag divergence when there is actual cost to reconcile — tasks with
    # zero-cost unpriced combos produce 0.0 from both paths and trivially agree.
    if result.total_cost_snapshot_path > 0 and not result.within_tolerance:
        logger.warning(
            "cost_telemetry_reconciliation FAILED — snapshot %.8f vs direct %.8f "
            "(delta=%.2e, tolerance=%.2e) over %d task(s)",
            result.total_cost_snapshot_path,
            result.total_cost_direct_path,
            result.absolute_delta,
            result.tolerance,
            result.task_count,
        )
    if not result.cache_hit_rate_within_tolerance and result.total_input_tokens_snapshot_path > 0:
        logger.warning(
            "cost_telemetry_reconciliation cache-hit-rate FAILED — snapshot %.10f vs direct %.10f "
            "(delta=%.2e) over %d task(s)",
            result.cache_hit_rate_snapshot_path,
            result.cache_hit_rate_direct_path,
            result.cache_hit_rate_delta,
            result.task_count,
        )
    return result.asdict()


async def _compute_prompt_static_bytes() -> int | None:
    """Sum the source character lengths of all static code-change prompt sections.

    Reads agent/src/prompt.ts and measures the template-literal constants in
    _PROMPT_STATIC_CONST_NAMES plus the plain-string constants in
    _PROMPT_STATIC_PLAIN_CONST_NAMES. Also reads agent/src/prompts/task-type-focus.ts
    and measures the template-literal constants in _TASK_TYPE_FOCUS_CONST_NAMES.
    Returns None when any source file is unavailable (production backend containers
    that do not mount agent/src) or when any declared constant is absent.

    Baseline (post-v16, 2026-06-20): ~16 420 bytes / ~4 678 tokens from prompt.ts.
    v11 adds task-type-focus.ts constants (~5 500 bytes), raising the combined
    baseline to ~21 900 bytes.
    """
    agent_src = _find_agent_src()
    if agent_src is None:
        return None
    prompt_ts = os.path.join(agent_src, "prompt.ts")
    if not os.path.exists(prompt_ts):
        return None
    try:
        src = await asyncio.to_thread(lambda: open(prompt_ts, encoding="utf-8").read())
    except OSError:
        return None
    total = 0
    for name in _PROMPT_STATIC_CONST_NAMES:
        length = _extract_ts_template_literal_length(src, name)
        if length is None:
            return None
        total += length
    for name in _PROMPT_STATIC_PLAIN_CONST_NAMES:
        length = _extract_ts_plain_string_length(src, name)
        if length is None:
            return None
        total += length

    task_type_focus_ts = os.path.join(agent_src, "prompts", "task-type-focus.ts")
    if not os.path.exists(task_type_focus_ts):
        return None
    try:
        ttf_src = await asyncio.to_thread(lambda: open(task_type_focus_ts, encoding="utf-8").read())
    except OSError:
        return None
    for name in _TASK_TYPE_FOCUS_CONST_NAMES:
        length = _extract_ts_template_literal_length(ttf_src, name)
        if length is None:
            return None
        total += length
    return total


async def _compute_salvage_axes(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, int | float | None]:
    """Query salvage-bundle tasks created in the axis window.

    Returns a dict with ``salvage_count`` (total salvage_bundle tasks created
    in the window) and ``salvage_merge_rate`` (fraction of COMPLETED salvage
    bundles that have ``pr_merged == true``).  Both are ``None`` when there
    are no salvage bundles in the window.
    """
    cutoff = _utcnow() - timedelta(days=_AXIS_WINDOW_DAYS)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.updated_at >= cutoff,
            Task.payloads[0]["type"].as_string() == "salvage_bundle",
        )
        .options(load_only(Task.id, Task.status, Task.context, raiseload=True))
    )
    salvage_tasks = list((await session.execute(stmt)).scalars().all())

    if not salvage_tasks:
        return {"salvage_count": None, "salvage_merge_rate": None}

    total = len(salvage_tasks)
    completed_merged = sum(
        1
        for t in salvage_tasks
        if t.status == TaskStatus.COMPLETED
        and (t.context or {}).get("pr_merged")
    )

    return {
        "salvage_count": total,
        "salvage_merge_rate": (
            round(completed_merged / total, 4) if total > 0 else None
        ),
    }


def _acceptance_quality_axes(tasks: list[Task]) -> dict[str, float | None]:
    """Return proven_rate and satisfied_rate snapshot axes from recent tasks."""
    from app.acceptance_gate import summarize_version_acceptance_aggregate

    agg = summarize_version_acceptance_aggregate(tasks)
    return {
        AXIS_ACCEPTANCE_PROVEN_RATE: agg["proven_rate"],
        AXIS_ACCEPTANCE_SATISFIED_RATE: agg["satisfied_rate"],
    }


async def compute_current_axes(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Compute all efficiency axes (plus cold-start breakdown) from recent terminal tasks."""
    from app.digest_metrics import (
        compute_format_compliance_metrics,
        compute_timeout_rate,
    )
    from app.warm_backlog_reuse import (
        compute_dominant_pool_miss,
        compute_dominant_reuse_dropout,
    )

    tasks = await _load_recent_tasks(session, project_id)
    _COHORT_CAP = 5000
    cohort_task_count = len(tasks)
    cohort_capped = cohort_task_count >= _COHORT_CAP
    blocked_vm_spend = await _compute_blocked_vm_spend(session, project_id)
    lanes = await _load_project_lanes(session, project_id)
    pool_metrics = await compute_pool_idle_cost(session, project_id)
    # ComputeJob (GPU experiment) cost for the window: per-task map (fed into the
    # total-cost-per-verified-task numerator) plus the unpriced-coverage honesty
    # block so experiment compute is visible to the cost/efficiency ranking and a
    # $0 that means "unpriced" is distinguishable from "free".
    compute_cost_by_task, compute_cost_attribution = await _load_compute_job_costs(
        session, project_id
    )

    # Provider routing A/B verdict (direction d-cf39cb40): derive from the
    # per-model efficiency breakdown so the verdict carries BOTH cost ratio AND
    # verified-merge-rate delta — the two halves of the A/B guardrail.
    _effort_model = compute_effort_model_efficiency(tasks)
    provider_routing_verdict = compute_provider_routing_verdict(_effort_model["by_model"])

    # Propagate verdict state to live routing so the dispatch path reorders
    # providers without waiting for a config deploy (direction d-cf39cb40).
    try:
        from app.shared_state import runtime_config as _rc

        _rc["provider_routing_verdict_state"] = provider_routing_verdict["state"]
    except Exception:
        pass

    # Honest net-cost companion: fold the project's warm-pool idle cost into the
    # cost_per_merged_pr numerator so pre-warming a pool can't make the headline
    # axis look better while real spend rises. Kept SEPARATE from the immutable
    # AXIS_COST_PER_MERGED_PR baseline (drift comparisons stay apples-to-apples).
    #
    # Use a windowed idle cost (same 7-day window as the task-cost numerator and
    # merged-PR denominator) so the all-time monotonically-growing idle accumulator
    # cannot ratchet the axis upward independently of current pool behavior.
    # pool_metrics["total_idle_cost_usd"] is preserved in the snapshot payload as
    # the durable all-time spend figure ("teardown never erases measured spend").
    axis_cutoff = _utcnow() - timedelta(days=_AXIS_WINDOW_DAYS)
    windowed_idle = await _compute_windowed_idle_cost(session, project_id, axis_cutoff)
    total_vm_cost, merged = _cost_and_merges(tasks)
    cost_incl_pool_idle: float | None = (
        round((total_vm_cost + windowed_idle["total"]) / merged, 4) if merged else None
    )

    # Salvage-bundle effectiveness: count of salvage tasks spawned in the
    # window and the fraction that reached a merged state.
    _salvage = await _compute_salvage_axes(session, project_id)

    return {
        AXIS_COST_PER_MERGED_PR: _compute_cost_per_merged_pr(tasks),
        AXIS_VM_COLD_START_P50_MS: _compute_vm_cold_start_p50_ms(tasks),
        AXIS_VM_REUSE_RATE: _compute_vm_reuse_rate(tasks),
        # Per-origin companion to the aggregate vm_reuse_rate: isolates the
        # pool-origin share of warm starts so an operator can confirm the pool
        # (not chain-continuity/backlog-reuse) lifted reuse above the floor.
        # Kept ALONGSIDE the immutable aggregate axis, like pool_metrics.
        "warm_start_origin_breakdown": _compute_warm_start_origin_breakdown(tasks),
        AXIS_MEDIAN_API_CALLS_PER_TASK: _compute_median_api_calls_per_task(tasks),
        AXIS_MEDIAN_REVIEW_ITERATIONS: _compute_median_review_iterations(tasks),
        AXIS_LLM_COST_PER_MERGED_PR: _compute_llm_cost_per_merged_pr(tasks),
        AXIS_COST_PER_VERIFIED_TASK: _compute_cost_per_verified_task(tasks),
        AXIS_LLM_COST_PER_VERIFIED_TASK: _compute_llm_cost_per_verified_task(tasks),
        AXIS_LANE_COST_PER_MERGED_PR: _compute_lane_cost_per_merged_pr(lanes),
        AXIS_LANE_WARM_RESUME_RATE: _compute_lane_warm_resume_rate(lanes),
        AXIS_CHECKPOINT_LOSS_RATE: _compute_checkpoint_loss_rate(lanes),
        AXIS_PROMPT_STATIC_BYTES: await _compute_prompt_static_bytes(),
        AXIS_LLM_CACHE_HIT_RATE: _compute_llm_cache_hit_rate(tasks),
        AXIS_PROMPT_CACHE_READ_SHARE: _compute_prompt_cache_read_share(tasks),
        AXIS_TOTAL_COST_PER_VERIFIED_TASK: _compute_total_cost_per_verified_task(
            tasks, compute_cost_by_task
        ),
        "total_cost_per_verified_task_denominator": _compute_verified_merge_count(tasks),
        # ComputeJob (GPU experiment) cost grounding: total compute spend folded
        # into total_cost_per_verified_task above, plus the unpriced-coverage
        # honesty block (mirrors llm_cost_pricing_coverage) so a genuinely
        # UNPRICED $0 compute job is not read as free by the ranking.
        "compute_cost_attribution": compute_cost_attribution,
        AXIS_REWORK_COST_SHARE: _compute_rework_cost_share(tasks),
        AXIS_SALVAGE_COUNT: _salvage["salvage_count"],
        AXIS_SALVAGE_MERGE_RATE: _salvage["salvage_merge_rate"],
        "vm_cold_start_breakdown": _compute_cold_start_breakdown_p50(tasks),
        "llm_cost_pricing_coverage": _compute_llm_cost_pricing_coverage(tasks),
        "pool_idle_cost_usd": pool_metrics["total_idle_cost_usd"],
        "pool_metrics": pool_metrics,
        "cost_per_merged_pr_incl_pool_idle": cost_incl_pool_idle,
        # Durable snapshot of the warm-reuse drop-out ranking. The live funnel
        # counters are in-memory and reset on every deploy/restart (frequent on
        # Cloud Run); folding the ranked breakdown into the daily snapshot makes
        # the dominant drop-out survive restarts and trendable day over day
        # (surfaced via read_baseline_with_drift -> the warm-VM admin view).
        "reuse_dropout": compute_dominant_reuse_dropout(),
        # Durable snapshot of the warm-POOL dispatch funnel (a DIFFERENT funnel
        # from reuse_dropout above — do not conflate). The live pool funnel
        # counters (lease hits, cold dispatches, lease-miss-by-reason, per-account
        # hit rates) are in-memory and reset on every deploy/restart (frequent on
        # Cloud Run); folding the aggregates + the dominant-miss-reason call-out
        # into the daily snapshot makes the dispatch funnel survive restarts and
        # trendable day over day. This is what tells an operator enabling the pool
        # not just whether it is hitting but, when it misses, WHY (no idle member
        # vs no compatible member vs lost-lease race) — the answer that drives the
        # next tuning action. Surfaced via read_baseline_with_drift -> warm-VM view.
        "pool_dispatch_funnel": compute_dominant_pool_miss(),
        # Durable, windowed AGENT_TIMEOUT-rate measure (direction d-46468a41):
        # overall rate + per-subtype decomposition + dominant subtype, all read
        # from PERSISTED per-task ground truth (ctx['failure_category'] /
        # ctx['timeout_subtype']), never re-derived from logs. Folded into the
        # daily snapshot so it inherits the first-snapshot-becomes-immutable-
        # baseline semantics for free; current-vs-baseline drift is surfaced by
        # read_baseline_with_drift -> the efficiency-axes response and the digest
        # AGENT_TIMEOUT before/after sentence. The denominator is execution-
        # reached tasks (started_at present) so the rate is not gameable by raw
        # volume. See app.digest_metrics.compute_timeout_rate.
        "agent_timeout_rate": compute_timeout_rate(tasks),
        # Durable structured-output format-compliance measure (v7 shared
        # self-repair adoption): overall block from persisted
        # Task.format_compliance_summary, plus a per-task-type breakdown keyed
        # by task type. The by_type block lets read-only research agents
        # (authenticated with the agent token) obtain the durable per-type
        # breakdown for a snapshot day without hitting admin-auth endpoints.
        FORMAT_COMPLIANCE_KEY: (_fc := compute_format_compliance_metrics(tasks))[
            "overall"
        ],
        "format_compliance_by_type": _fc["by_type"],
        # Durable per-driver LLM cost attribution (v8): compact breakdown of
        # total LLM dollars by (provider,model), by task-type, and by
        # first-pass/rework split. Folded into the daily snapshot so the
        # llm_cost_per_merged_pr drift observed across days can be decomposed
        # retroactively — which model, which task type, or which rework share
        # drove the change — without relying on the on-demand live endpoint
        # (which only covers the current window). Reconciles with the scalar
        # llm_cost_per_merged_pr axis via the shared _task_llm_attempts path.
        "llm_cost_attribution": _compact_llm_cost_attribution(tasks),
        # VM cost by-task-type attribution: compact breakdown of total VM dollars
        # by task type so operators can see how much of cost_per_merged_pr drift
        # comes from primary tasks (review, create) vs quality-gate side-cars.
        # Reconciles with cost_per_merged_pr via the shared get_task_total_vm_cost_usd path.
        "vm_cost_attribution": {
            "by_task_type": compute_vm_cost_by_task_type(tasks),
            "total_vm_usd": round(sum(get_task_total_vm_cost_usd(t) for t in tasks), 6),
        },
        # Provider routing A/B verdict (direction d-cf39cb40): DeepSeek vs
        # Anthropic cost ratio + verified-merge-rate delta, persisted in the
        # daily snapshot so the A/B is machine-checkable on the deployed instance.
        "provider_routing_verdict": provider_routing_verdict,
        # Per-(provider,model) efficiency breakdown from compute_effort_model_efficiency:
        # by_model rows with verified_landed_rate, cost_per_merge, run counts, and token
        # volumes, plus aggregate totals. Persisted in the daily snapshot so the DeepSeek
        # vs Anthropic A/B (direction d-cf39cb40) can be verified retroactively from
        # persisted history rather than only from the live digest endpoint.
        "effort_model_efficiency": {
            "by_model": _effort_model["by_model"],
            "totals": _effort_model["totals"],
        },
        # Per-provider routing share (direction d-cf39cb40): aggregates by_model into
        # a compact by_provider block so operators can confirm at a glance which
        # provider handled what fraction of tasks, at what cost and verified-landing
        # rate. Persisted alongside the verdict for retroactive decomposition.
        "provider_routing_share": compute_provider_routing_share(_effort_model["by_model"]),
        # Self-check: reconcile LLM cost telemetry against an independent
        # recomputation path so operators are alerted if the two diverge beyond
        # the declared tolerance. Non-zero tasks with at least one priced attempt
        # are required for a meaningful check.
        "cost_telemetry_reconciliation": _compute_cost_telemetry_reconciliation(tasks),
        # Cohort coverage: how many terminal tasks were loaded and whether the
        # 5000-row cap was reached (cohort_capped=True means the window contains
        # more tasks than the cap and totals are cohort-scoped, not window-complete).
        "cohort_task_count": cohort_task_count,
        "cohort_capped": cohort_capped,
        # BLOCKED provisioned spend: VM cost incurred by BLOCKED tasks whose VM
        # was provisioned within the window. BLOCKED tasks are excluded from the
        # terminal cohort but still consumed real VM spend; surfaced separately
        # so the admin SQL total (terminal + BLOCKED) can be reconciled against
        # the cohort-scoped figure.
        "blocked_provisioned_vm_spend_usd": round(blocked_vm_spend, 6),
        # Acceptance evidence quality trend (direction d-99daddbd): proven_rate
        # (machine-checked proofs / total merges) and satisfied_rate
        # (proven + satisfied_unproven / total merges) persisted as snapshot axes
        # so operators can observe whether evidence quality improves or degrades
        # as acceptance-gate changes land. None when no completed tasks in window.
        **_acceptance_quality_axes(tasks),
    }


async def write_snapshot(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Compute axes and persist them under ``design_doc["efficiency_baseline"]``.

    First-snapshot semantics: the very first call seeds ``baseline``; later
    calls only append to ``snapshots`` (capped at _SNAPSHOT_HISTORY_MAX).
    Returns the snapshot dict that was written.
    """
    # Compute axes before acquiring lock — reads only Task rows, not the project.
    axes = await compute_current_axes(session, project_id)
    captured_dt = _utcnow()
    captured_at = captured_dt.isoformat()
    from app.deployed_sha import get_serving_sha

    snapshot: Dict[str, Any] = {
        "captured_at": captured_at,
        # metric_def_version versions the metric DEFINITIONS; serving_sha records
        # which deployed backend code was running when this snapshot was written.
        # Both allow consumers to partition history across definition changes and
        # deployment boundaries. serving_sha is None on pre-stamp snapshots.
        "metric_def_version": METRIC_DEF_VERSION,
        "serving_sha": get_serving_sha(),
        **axes,
    }

    # Lock the project row for the read-modify-write so concurrent background
    # writers (e.g. sr_blind_spots, playbook_learning) cannot clobber each
    # other's keys with stale reads.  skip_locked=False: background callers
    # should wait rather than silently skip (no 429 to surface).
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise ValueError(f"Project {project_id} not found")

        container = dict((project.design_doc or {}).get(DESIGN_DOC_KEY) or {})

        if not container.get("baseline"):
            container["baseline"] = dict(snapshot)
            # Record per-member provenance for the initial seed so every
            # member carries a durable "seeded from which snapshot" annotation.
            _prov: Dict[str, Any] = {}
            for _axis in ALL_AXES:
                if snapshot.get(_axis) is not None:
                    _prov[_axis] = {
                        "seeded_at": captured_at,
                        "snapshot_captured_at": captured_at,
                    }
            for _key in (
                "agent_timeout_rate",
                FORMAT_COMPLIANCE_KEY,
                "llm_cost_attribution",
                "effort_model_efficiency",
            ):
                if snapshot.get(_key) is not None:
                    _prov[_key] = {
                        "seeded_at": captured_at,
                        "snapshot_captured_at": captured_at,
                    }
            container["baseline"]["_member_provenance"] = _prov
        else:
            baseline = dict(container["baseline"])
            # Per-member provenance: records when each baseline member was seeded
            # (or re-seeded after a migration) and from which snapshot's data it
            # came.  Missing on pre-provenance baselines — see retroactive
            # inference below.
            provenance: Dict[str, Any] = dict(baseline.get("_member_provenance") or {})
            # Retroactive annotation: if provenance is empty and snapshots already
            # exist, scan the snapshot history to find the earliest snapshot whose
            # value matches each current baseline member.  This annotates patchwork
            # baselines (where individual members were re-seeded after the original
            # capture date) without a separate migration.  Runs once; subsequent
            # calls find provenance already populated and skip this block.
            existing_snapshots = list(container.get("snapshots") or [])
            if not provenance and existing_snapshots:
                for _member in [
                    *ALL_AXES,
                    "agent_timeout_rate",
                    FORMAT_COMPLIANCE_KEY,
                    "llm_cost_attribution",
                    "effort_model_efficiency",
                ]:
                    _bval = baseline.get(_member)
                    if _bval is None:
                        continue
                    for _snap in existing_snapshots:
                        if _snap.get(_member) == _bval:
                            provenance[_member] = {
                                "seeded_at": _snap.get("captured_at"),
                                "snapshot_captured_at": _snap.get("captured_at"),
                                "_inferred": True,
                            }
                            break
            # One-time migration: baselines captured before the heartbeat-beacon fix
            # (schema_version 0) stored vm_cold_start_p50_ms from the first boot
            # beacon rather than true dispatch-to-claim latency. Clear it so the
            # next snapshot re-seeds with accurate data.
            if not baseline.get("_schema_version"):
                baseline[AXIS_VM_COLD_START_P50_MS] = None
                baseline["_schema_version"] = 1
            # One-time migration: baselines captured before review-skipped tasks
            # counted as 0 iterations (schema_version 1) excluded auto-approved
            # tasks, biasing the median upward. Clear and re-seed.
            if baseline.get("_schema_version") == 1:
                baseline[AXIS_MEDIAN_REVIEW_ITERATIONS] = None
                baseline["_schema_version"] = 2
            # One-time migration: baselines captured before the vm_dispatched_at fix
            # (schema_version 2) stored vm_cold_start_p50_ms anchored at
            # provision-confirm time, excluding GCE boot latency. Clear and re-seed
            # from the corrected dispatch->agent-ready measurement.
            if baseline.get("_schema_version") == 2:
                baseline[AXIS_VM_COLD_START_P50_MS] = None
                baseline["_schema_version"] = 3
            # One-time migration: baselines captured before LLM token pricing was
            # added (schema_version 3) may have been seeded with a VM-only cost
            # value under the llm_cost_per_merged_pr key. Clear and re-seed so
            # current-vs-baseline drift is apples-to-apples.
            if baseline.get("_schema_version") == 3:
                baseline[AXIS_LLM_COST_PER_MERGED_PR] = None
                baseline["_schema_version"] = 4
            # One-time migration: baselines captured before pricing was added for
            # gpt-5.3-codex-spark, gpt-5.2, and the Gemini 3.x preview models
            # (schema_version 4) may be silently zero for tasks on those models.
            # Clear llm_cost_per_merged_pr so it re-seeds with the now-complete
            # pricing table.
            if baseline.get("_schema_version") == 4:
                baseline[AXIS_LLM_COST_PER_MERGED_PR] = None
                baseline["_schema_version"] = 5
            # One-time migration: baselines captured before improvement-lane
            # efficiency axes were registered (schema_version 5) have no lane_*
            # or checkpoint_* keys. Clear/seed them so current-vs-baseline drift
            # is apples-to-apples.
            if baseline.get("_schema_version") == 5:
                baseline[AXIS_LANE_COST_PER_MERGED_PR] = None
                baseline[AXIS_LANE_WARM_RESUME_RATE] = None
                baseline[AXIS_CHECKPOINT_LOSS_RATE] = None
                baseline["_schema_version"] = 6
            # Re-seed any individual axes that were cleared (e.g. by the migration
            # above) once a valid current value is available.
            for axis in ALL_AXES:
                if baseline.get(axis) is None and snapshot.get(axis) is not None:
                    baseline[axis] = snapshot[axis]
                    provenance[axis] = {
                        "seeded_at": captured_at,
                        "snapshot_captured_at": captured_at,
                    }
            # agent_timeout_rate is a nested block, not a member of ALL_AXES, so
            # the re-seed loop above never reaches it. Backfill it for baselines
            # that predate this measure (the live, daily-snapshotted Blueprint
            # project among them) so read_baseline_with_drift can report a real
            # current-vs-baseline drift instead of baseline=None / drift_pct=None.
            if (
                baseline.get("agent_timeout_rate") is None
                and snapshot.get("agent_timeout_rate") is not None
            ):
                baseline["agent_timeout_rate"] = snapshot["agent_timeout_rate"]
                provenance["agent_timeout_rate"] = {
                    "seeded_at": captured_at,
                    "snapshot_captured_at": captured_at,
                }
            if (
                baseline.get(FORMAT_COMPLIANCE_KEY) is None
                and snapshot.get(FORMAT_COMPLIANCE_KEY) is not None
            ):
                baseline[FORMAT_COMPLIANCE_KEY] = snapshot[FORMAT_COMPLIANCE_KEY]
                provenance[FORMAT_COMPLIANCE_KEY] = {
                    "seeded_at": captured_at,
                    "snapshot_captured_at": captured_at,
                }
            if (
                baseline.get("llm_cost_attribution") is None
                and snapshot.get("llm_cost_attribution") is not None
            ):
                baseline["llm_cost_attribution"] = snapshot["llm_cost_attribution"]
                provenance["llm_cost_attribution"] = {
                    "seeded_at": captured_at,
                    "snapshot_captured_at": captured_at,
                }
            if (
                baseline.get("effort_model_efficiency") is None
                and snapshot.get("effort_model_efficiency") is not None
            ):
                baseline["effort_model_efficiency"] = snapshot["effort_model_efficiency"]
                provenance["effort_model_efficiency"] = {
                    "seeded_at": captured_at,
                    "snapshot_captured_at": captured_at,
                }
            baseline["_member_provenance"] = provenance
            container["baseline"] = baseline

        snapshots = list(container.get("snapshots") or [])
        snapshots.append(snapshot)
        if len(snapshots) > _SNAPSHOT_HISTORY_MAX:
            snapshots = snapshots[-_SNAPSHOT_HISTORY_MAX:]
        container["snapshots"] = snapshots
        container["last_run_at"] = captured_at

        doc = dict(project.design_doc or {})
        doc[DESIGN_DOC_KEY] = container
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

    from app.project_telemetry import set_project_telemetry
    await set_project_telemetry(session, project_id, DESIGN_DOC_KEY, container)

    # Dual-write the snapshot time-series to the dedicated
    # project_efficiency_snapshot table. Done OUTSIDE the project_lock block so
    # the row-scoped ON CONFLICT upsert never takes the Project row lock; the
    # accessor enforces the same _SNAPSHOT_HISTORY_MAX retention the design_doc
    # container used. The design_doc write above stays authoritative for safe
    # rollback until the read path is fully cut over. Caller commits.
    await insert_efficiency_snapshot(
        session,
        project_id,
        captured_at=captured_dt,
        metric_def_version=METRIC_DEF_VERSION,
        blob=snapshot,
    )

    logger.info(
        "[EFFICIENCY_SNAPSHOT] project=%s captured_at=%s axes=%s",
        project_id,
        captured_at,
        {k: axes[k] for k in ALL_AXES},
    )

    # Trigger alert if cost telemetry reconciliation failed tolerance.
    reconciliation: dict | None = axes.get("cost_telemetry_reconciliation")
    if (
        reconciliation
        and reconciliation.get("within_tolerance") is False
        and (reconciliation.get("total_cost_snapshot_path") or 0) > 0
    ):
        from app.alert_dispatch import (
            _maybe_create_cost_telemetry_reconciliation_alert,
        )

        await _maybe_create_cost_telemetry_reconciliation_alert(
            project_id,
            session,
            absolute_delta=reconciliation.get("absolute_delta", 0.0),
            tolerance=reconciliation.get("tolerance", 1e-6),
            task_count=reconciliation.get("task_count", 0),
        )

    return snapshot


def compute_llm_cost_attribution_baseline_delta(
    baseline_attr: Dict[str, Any] | None,
    current_attr: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    """Baseline-vs-current delta for a compact llm_cost_attribution block.

    Returns None when either attribution block is absent.  The delta answers
    *what* shifted when llm_cost_per_merged_pr drifts: which model's share grew,
    whether rework or self-repair rose, and whether the move is in total spend.

    Each scalar field carries ``{baseline, current, abs_delta, rel_pct}``.
    Per-model and per-task-type rows carry absolute cost delta and share-point
    shift so the dominant driver is immediately readable.  Both lists are sorted
    by abs(cost_delta_usd) descending so the biggest mover is first.
    """
    if not baseline_attr or not current_attr:
        return None

    def _scalar_delta(b: Any, c: Any) -> Dict[str, Any]:
        b_val = b if isinstance(b, (int, float)) else None
        c_val = c if isinstance(c, (int, float)) else None
        abs_delta: float | None = None
        rel_pct: float | None = None
        if b_val is not None and c_val is not None:
            abs_delta = round(c_val - b_val, 6)
            if b_val != 0:
                rel_pct = round((c_val - b_val) / b_val * 100.0, 2)
        return {"baseline": b_val, "current": c_val, "abs_delta": abs_delta, "rel_pct": rel_pct}

    b_total = baseline_attr.get("total_llm_usd") or 0.0
    c_total = current_attr.get("total_llm_usd") or 0.0

    def _share(cost: Any, total: float) -> float | None:
        if not isinstance(cost, (int, float)) or total <= 0:
            return None
        return round(cost / total, 4)

    def _list_delta(b_rows: list, c_rows: list, key: str) -> list:
        b_map = {r[key]: r.get("cost_usd", 0.0) for r in (b_rows or [])}
        c_map = {r[key]: r.get("cost_usd", 0.0) for r in (c_rows or [])}
        all_keys = set(b_map) | set(c_map)
        rows = []
        for k in all_keys:
            b_cost = b_map.get(k, 0.0)
            c_cost = c_map.get(k, 0.0)
            rows.append({
                key: k,
                "baseline_cost_usd": round(b_cost, 6),
                "current_cost_usd": round(c_cost, 6),
                "cost_delta_usd": round(c_cost - b_cost, 6),
                "baseline_share": _share(b_cost, b_total),
                "current_share": _share(c_cost, c_total),
                "share_delta": (
                    round(_share(c_cost, c_total) - _share(b_cost, b_total), 4)
                    if _share(c_cost, c_total) is not None and _share(b_cost, b_total) is not None
                    else None
                ),
            })
        rows.sort(key=lambda r: abs(r["cost_delta_usd"]), reverse=True)
        return rows

    b_by_model = baseline_attr.get("by_model") or []
    c_by_model = current_attr.get("by_model") or []
    b_map_model = {(r["provider"], r["model"]): r.get("cost_usd", 0.0) for r in b_by_model}
    c_map_model = {(r["provider"], r["model"]): r.get("cost_usd", 0.0) for r in c_by_model}
    all_model_keys = set(b_map_model) | set(c_map_model)
    by_model_delta = sorted(
        [
            {
                "provider": k[0],
                "model": k[1],
                "baseline_cost_usd": round(b_map_model.get(k, 0.0), 6),
                "current_cost_usd": round(c_map_model.get(k, 0.0), 6),
                "cost_delta_usd": round(c_map_model.get(k, 0.0) - b_map_model.get(k, 0.0), 6),
                "baseline_share": _share(b_map_model.get(k, 0.0), b_total),
                "current_share": _share(c_map_model.get(k, 0.0), c_total),
            }
            for k in all_model_keys
        ],
        key=lambda r: abs(r["cost_delta_usd"]),
        reverse=True,
    )

    return {
        "total_llm_usd": _scalar_delta(baseline_attr.get("total_llm_usd"), current_attr.get("total_llm_usd")),
        "rework_share": _scalar_delta(baseline_attr.get("rework_share"), current_attr.get("rework_share")),
        SELF_REPAIR_BURN_SHARE_KEY: _scalar_delta(
            baseline_attr.get(SELF_REPAIR_BURN_SHARE_KEY), current_attr.get(SELF_REPAIR_BURN_SHARE_KEY)
        ),
        "by_model_delta": by_model_delta,
        "by_task_type_delta": _list_delta(
            baseline_attr.get("by_task_type"), current_attr.get("by_task_type"), "task_type"
        ),
    }


def compute_llm_cost_attribution_window_delta(
    trend: list,
    *,
    recent_n: int = 3,
) -> Dict[str, Any] | None:
    """Recent-vs-prior trailing-window cost-mix delta derived from the stored trend.

    Complements ``compute_llm_cost_attribution_baseline_delta`` (which compares
    the immutable genesis baseline against the current snapshot).  This function
    compares the RECENT window (the most recent ``recent_n`` snapshots) against
    the PRIOR window (the ``recent_n`` snapshots immediately preceding them),
    both windows averaged/aggregated, so a '+X% cost' claim can be decomposed as
    "did this happen recently or is it cumulative drift since genesis?".

    Returns None when the trend is too short to populate both windows (i.e. fewer
    than ``recent_n * 2`` entries after filtering to entries that carry attribution
    data).  Scalar fields (``total_llm_usd``, ``rework_share``,
    ``self_repair_burn_share``) are averaged across the window; per-model and
    per-task-type ``cost_usd`` values are summed then compared.  Output shape is
    the same as ``compute_llm_cost_attribution_baseline_delta`` with an additional
    ``window_meta`` key describing the snapshot counts and captured_at bounds.
    """
    if not trend or len(trend) < recent_n * 2:
        return None

    prior_window = trend[-(recent_n * 2):-recent_n]
    recent_window = trend[-recent_n:]

    if len(prior_window) < recent_n or len(recent_window) < recent_n:
        return None

    def _mean_scalar(window: list, key: str) -> float | None:
        vals = [e[key] for e in window if isinstance(e.get(key), (int, float))]
        return round(sum(vals) / len(vals), 6) if vals else None

    def _scalar_delta(p: Any, r: Any) -> Dict[str, Any]:
        p_val = p if isinstance(p, (int, float)) else None
        r_val = r if isinstance(r, (int, float)) else None
        abs_delta: float | None = None
        rel_pct: float | None = None
        if p_val is not None and r_val is not None:
            abs_delta = round(r_val - p_val, 6)
            if p_val != 0:
                rel_pct = round((r_val - p_val) / p_val * 100.0, 2)
        return {"prior": p_val, "recent": r_val, "abs_delta": abs_delta, "rel_pct": rel_pct}

    p_total = _mean_scalar(prior_window, "total_llm_usd") or 0.0
    r_total = _mean_scalar(recent_window, "total_llm_usd") or 0.0

    def _share(cost: float, total: float) -> float | None:
        if total <= 0:
            return None
        return round(cost / total, 4)

    def _model_delta(prior_window: list, recent_window: list) -> list:
        def _sum_models(window: list) -> Dict[tuple, float]:
            totals: Dict[tuple, float] = {}
            for entry in window:
                for row in (entry.get("by_model") or []):
                    k = (row.get("provider"), row.get("model"))
                    totals[k] = totals.get(k, 0.0) + float(row.get("cost_usd") or 0.0)
            n = len(window)
            return {k: round(v / n, 6) for k, v in totals.items()} if n else {}

        p_map = _sum_models(prior_window)
        r_map = _sum_models(recent_window)
        all_keys = set(p_map) | set(r_map)
        rows = []
        for k in all_keys:
            p_cost = p_map.get(k, 0.0)
            r_cost = r_map.get(k, 0.0)
            rows.append({
                "provider": k[0],
                "model": k[1],
                "prior_cost_usd": round(p_cost, 6),
                "recent_cost_usd": round(r_cost, 6),
                "cost_delta_usd": round(r_cost - p_cost, 6),
                "prior_share": _share(p_cost, p_total),
                "recent_share": _share(r_cost, r_total),
                "share_delta": (
                    round(_share(r_cost, r_total) - _share(p_cost, p_total), 4)
                    if _share(r_cost, r_total) is not None and _share(p_cost, p_total) is not None
                    else None
                ),
            })
        rows.sort(key=lambda r: abs(r["cost_delta_usd"]), reverse=True)
        return rows

    def _task_type_delta(prior_window: list, recent_window: list) -> list:
        def _sum_types(window: list) -> Dict[str, float]:
            totals: Dict[str, float] = {}
            for entry in window:
                for row in (entry.get("by_task_type") or []):
                    k = row.get("task_type")
                    if k is not None:
                        totals[k] = totals.get(k, 0.0) + float(row.get("cost_usd") or 0.0)
            n = len(window)
            return {k: round(v / n, 6) for k, v in totals.items()} if n else {}

        p_map = _sum_types(prior_window)
        r_map = _sum_types(recent_window)
        all_keys = set(p_map) | set(r_map)
        rows = []
        for k in all_keys:
            p_cost = p_map.get(k, 0.0)
            r_cost = r_map.get(k, 0.0)
            rows.append({
                "task_type": k,
                "prior_cost_usd": round(p_cost, 6),
                "recent_cost_usd": round(r_cost, 6),
                "cost_delta_usd": round(r_cost - p_cost, 6),
                "prior_share": _share(p_cost, p_total),
                "recent_share": _share(r_cost, r_total),
                "share_delta": (
                    round(_share(r_cost, r_total) - _share(p_cost, p_total), 4)
                    if _share(r_cost, r_total) is not None and _share(p_cost, p_total) is not None
                    else None
                ),
            })
        rows.sort(key=lambda r: abs(r["cost_delta_usd"]), reverse=True)
        return rows

    prior_captured = [e.get("captured_at") for e in prior_window if e.get("captured_at")]
    recent_captured = [e.get("captured_at") for e in recent_window if e.get("captured_at")]

    return {
        "total_llm_usd": _scalar_delta(_mean_scalar(prior_window, "total_llm_usd"), _mean_scalar(recent_window, "total_llm_usd")),
        "rework_share": _scalar_delta(_mean_scalar(prior_window, "rework_share"), _mean_scalar(recent_window, "rework_share")),
        SELF_REPAIR_BURN_SHARE_KEY: _scalar_delta(
            _mean_scalar(prior_window, SELF_REPAIR_BURN_SHARE_KEY),
            _mean_scalar(recent_window, SELF_REPAIR_BURN_SHARE_KEY),
        ),
        "by_model_delta": _model_delta(prior_window, recent_window),
        "by_task_type_delta": _task_type_delta(prior_window, recent_window),
        "window_meta": {
            "recent_n": recent_n,
            "prior_count": len(prior_window),
            "recent_count": len(recent_window),
            "prior_start": prior_captured[0] if prior_captured else None,
            "prior_end": prior_captured[-1] if prior_captured else None,
            "recent_start": recent_captured[0] if recent_captured else None,
            "recent_end": recent_captured[-1] if recent_captured else None,
        },
    }


async def read_baseline_with_drift(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Return per-axis ``{current, baseline, drift_pct}`` plus capture metadata.

    ``drift_pct`` is None when either side is missing or baseline is 0.
    ``current`` reflects the latest stored snapshot (NOT a re-compute) so
    operators see the same numbers the snapshot service published. Endpoint
    consumers that want a freshly recomputed view should call
    ``compute_current_axes`` directly.

    Also includes ``reuse_funnel``: rolling 24h warm-reuse claim funnel
    counters (see :func:`app.warm_backlog_reuse.get_reuse_funnel_snapshot`).
    These counters are in-memory and instance-local; they reset on restart and
    are not stored in the efficiency baseline snapshot.

    ``reuse_dropout`` is the latest *persisted* warm-reuse drop-out ranking
    (see :func:`app.warm_backlog_reuse.compute_dominant_reuse_dropout`), folded
    into each daily snapshot so it survives the restarts that wipe
    ``reuse_funnel``. ``reuse_dropout_trend`` is the oldest-first day-over-day
    series of ``{captured_at, dominant_reason, dominant_share, dropout_total}``
    so an operator can confirm a widening change reduced the dominant leak.
    """
    from app.warm_backlog_reuse import (
        get_pool_funnel_snapshot,
        get_reuse_funnel_snapshot,
    )

    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    pool_funnel = get_pool_funnel_snapshot()

    # Compute windowed idle cost over the SAME window as the pool dispatch
    # funnel so cost_per_pool_hit_usd uses commensurable numerator and
    # denominator. Pre-window all-time ledger carry-forward is excluded.
    _amortization_window_hours = int(pool_funnel.get("window_hours") or 24)
    _amortization_cutoff = _utcnow() - timedelta(hours=_amortization_window_hours)
    _windowed_idle_result = await _compute_windowed_idle_cost(
        session, project_id, _amortization_cutoff
    )
    _pool_amortization_windowed_idle_usd = _windowed_idle_result["total"]
    _pool_amortization_windowed_by_account = _windowed_idle_result["by_account"]

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    baseline = container.get("baseline") or {}
    snapshots = await _read_snapshot_history(session, project_id, container)
    latest = snapshots[-1] if snapshots else {}

    # Durable warm-reuse drop-out: the latest persisted ranking plus a day-over-
    # day trend of the dominant reason / its share / the drop-out total, so an
    # operator can confirm a widening change reduced the dominant leak even
    # across the restarts that wipe the in-memory ``reuse_funnel`` counters.
    reuse_dropout_trend = [
        {
            "captured_at": snap.get("captured_at"),
            "dominant_reason": (
                (snap.get("reuse_dropout") or {}).get("dominant_dropout") or {}
            ).get("reason"),
            "dominant_share": (
                (snap.get("reuse_dropout") or {}).get("dominant_dropout") or {}
            ).get("share"),
            "dropout_total": (snap.get("reuse_dropout") or {}).get("dropout_total"),
        }
        for snap in snapshots
        if snap.get("reuse_dropout")
    ]

    # Durable warm-POOL dispatch funnel: the latest persisted funnel block plus a
    # day-over-day trend of the key aggregates (hit rate, hits/cold counts) and
    # the dominant lease-miss reason, so an operator can read the WHY-it-missed
    # breakdown across the multi-day windows even though the live pool funnel
    # counters reset on every restart. A DIFFERENT funnel from reuse_dropout_trend.
    pool_dispatch_funnel_trend = [
        {
            "captured_at": snap.get("captured_at"),
            "pool_lease_hits": (snap.get("pool_dispatch_funnel") or {}).get(
                "pool_lease_hits"
            ),
            "cold_dispatches": (snap.get("pool_dispatch_funnel") or {}).get(
                "cold_dispatches"
            ),
            "pool_hit_rate": (snap.get("pool_dispatch_funnel") or {}).get(
                "pool_hit_rate"
            ),
            "lease_miss_total": (snap.get("pool_dispatch_funnel") or {}).get(
                "lease_miss_total"
            ),
            "dominant_miss_reason": (
                (snap.get("pool_dispatch_funnel") or {}).get("dominant_miss") or {}
            ).get("reason"),
        }
        for snap in snapshots
        if snap.get("pool_dispatch_funnel")
    ]

    # Durable per-driver LLM cost attribution trend: day-over-day series of
    # the compact attribution block's headline scalars (by_task_type, by_model,
    # rework_share, total_llm_usd) so an operator can confirm that a fix reduced
    # rework spend, that a specific model's share contracted, or that a task-type
    # (review vs create vs side-car) drove the drift across consecutive snapshots.
    # Mirrors the reuse_dropout_trend pattern: omits snapshots that predate v8
    # (those have no llm_cost_attribution key and are naturally excluded).
    llm_cost_attribution_trend = [
        {
            "captured_at": snap.get("captured_at"),
            "by_task_type": (snap.get("llm_cost_attribution") or {}).get(
                "by_task_type"
            ),
            "by_model": (snap.get("llm_cost_attribution") or {}).get("by_model"),
            "rework_share": (snap.get("llm_cost_attribution") or {}).get(
                "rework_share"
            ),
            "total_llm_usd": (snap.get("llm_cost_attribution") or {}).get(
                "total_llm_usd"
            ),
            SELF_REPAIR_BURN_SHARE_KEY: (snap.get("llm_cost_attribution") or {}).get(
                SELF_REPAIR_BURN_SHARE_KEY
            ),
            "by_failure_class": (snap.get("llm_cost_attribution") or {}).get(
                "by_failure_class"
            ),
            "merged_count": (snap.get("llm_cost_attribution") or {}).get(
                "merged_count"
            ),
        }
        for snap in snapshots
        if snap.get("llm_cost_attribution")
    ]

    # Per-member provenance: records when each baseline member was seeded and
    # from which snapshot's window its value came.  For baselines that predate
    # provenance recording, infer retroactively on the read path (not persisted
    # here — the next write_snapshot will persist it inside the lock).
    member_provenance: Dict[str, Any] = dict(baseline.get("_member_provenance") or {})
    if not member_provenance and snapshots:
        for _member in [
            *ALL_AXES,
            "agent_timeout_rate",
            FORMAT_COMPLIANCE_KEY,
            "llm_cost_attribution",
            "effort_model_efficiency",
        ]:
            _bval = baseline.get(_member)
            if _bval is None:
                continue
            for _snap in snapshots:
                if _snap.get(_member) == _bval:
                    member_provenance[_member] = {
                        "seeded_at": _snap.get("captured_at"),
                        "snapshot_captured_at": _snap.get("captured_at"),
                        "_inferred": True,
                    }
                    break

    axes: Dict[str, Dict[str, Any]] = {}
    # Drift-suppression guard for AXIS_TOTAL_COST_PER_VERIFIED_TASK: when the
    # verified-merge denominator in the latest snapshot is below the floor, suppress
    # drift_pct so early low-N windows do not emit noisy drift. Mirrors the
    # SELF_REPAIR_MIN_ATTEMPTS guard for self_repair_success_rate.
    _verified_denom = latest.get("total_cost_per_verified_task_denominator", 0)
    _suppress_verified_drift = (
        isinstance(_verified_denom, int) and _verified_denom < VERIFIED_TASK_DRIFT_MIN_COUNT
    )
    for axis in ALL_AXES:
        cur = latest.get(axis)
        base = baseline.get(axis)
        drift_pct: float | None
        if (
            cur is None
            or base is None
            or not isinstance(cur, (int, float))
            or not isinstance(base, (int, float))
            or base == 0
        ):
            drift_pct = None
        elif axis == AXIS_TOTAL_COST_PER_VERIFIED_TASK and _suppress_verified_drift:
            drift_pct = None
        else:
            drift_pct = round(((cur - base) / base) * 100.0, 2)
        history = [
            {
                "captured_at": snap.get("captured_at"),
                "value": snap.get(axis),
                "def_version": snap.get("metric_def_version"),
            }
            for snap in snapshots
        ]
        _aprov = member_provenance.get(axis) or {}
        axes[axis] = {
            "current": cur,
            "baseline": base,
            "drift_pct": drift_pct,
            "history": history,
            "baseline_seeded_at": _aprov.get("seeded_at"),
            "baseline_seed_snapshot_at": _aprov.get("snapshot_captured_at"),
        }

    # Idle-inclusive cost-per-merged-PR as a companion trended axis. The
    # per-snapshot ``cost_per_merged_pr_incl_pool_idle`` already folds warm-pool
    # idle spend into the numerator (see compute_current_axes); here we surface
    # it as a full {current, baseline, drift_pct, history} series so an operator
    # can read TRUE cost regression in the same trend as the headline axis.
    #
    # Baseline is deliberately the immutable pre-pool ``cost_per_merged_pr``
    # baseline (idle cost was zero before the pool existed), so drift directly
    # answers "did enabling the pool regress cost per PR?". We do NOT add this to
    # ALL_AXES — that would seed a separate immutable inclusive baseline and a
    # migration, defeating the apples-to-apples separation the headline axis keeps.
    incl_current = latest.get("cost_per_merged_pr_incl_pool_idle")
    incl_baseline = baseline.get(AXIS_COST_PER_MERGED_PR)
    if (
        incl_current is None
        or incl_baseline is None
        or not isinstance(incl_current, (int, float))
        or not isinstance(incl_baseline, (int, float))
        or incl_baseline == 0
    ):
        incl_drift_pct = None
    else:
        incl_drift_pct = round(
            ((incl_current - incl_baseline) / incl_baseline) * 100.0, 2
        )
    cost_incl_pool_idle_axis = {
        "current": incl_current,
        "baseline": incl_baseline,
        "drift_pct": incl_drift_pct,
        "history": [
            {
                "captured_at": snap.get("captured_at"),
                "value": snap.get("cost_per_merged_pr_incl_pool_idle"),
                "def_version": snap.get("metric_def_version"),
            }
            for snap in snapshots
        ],
    }

    # Durable AGENT_TIMEOUT-rate measure (direction d-46468a41): surface the
    # latest snapshot's rate block against the immutable first-snapshot baseline
    # as current-vs-baseline drift, for the OVERALL rate AND each persisted
    # subtype. The block was computed from PERSISTED per-task ground truth in
    # compute_current_axes (see app.digest_metrics.compute_timeout_rate); here we
    # only diff latest vs baseline so an operator / the strategic layer can read a
    # real before/after delta. Rates are fractions; drift_pct is the relative
    # percentage change from baseline (None when either side is missing/zero).
    latest_timeout = latest.get("agent_timeout_rate") or {}
    baseline_timeout = baseline.get("agent_timeout_rate") or {}

    def _rate_drift_pct(cur: Any, base: Any) -> float | None:
        if (
            not isinstance(cur, (int, float))
            or not isinstance(base, (int, float))
            or base == 0
        ):
            return None
        return round(((cur - base) / base) * 100.0, 2)

    timeout_overall = {
        "current": latest_timeout.get("overall_rate"),
        "baseline": baseline_timeout.get("overall_rate"),
        "drift_pct": _rate_drift_pct(
            latest_timeout.get("overall_rate"), baseline_timeout.get("overall_rate")
        ),
        "history": [
            {
                "captured_at": snap.get("captured_at"),
                "value": (snap.get("agent_timeout_rate") or {}).get("overall_rate"),
            }
            for snap in snapshots
            if snap.get("agent_timeout_rate")
        ],
    }
    latest_by_subtype = latest_timeout.get("by_subtype") or {}
    baseline_by_subtype = baseline_timeout.get("by_subtype") or {}
    timeout_by_subtype = {
        subtype: {
            "current": (latest_by_subtype.get(subtype) or {}).get("rate"),
            "baseline": (baseline_by_subtype.get(subtype) or {}).get("rate"),
            "drift_pct": _rate_drift_pct(
                (latest_by_subtype.get(subtype) or {}).get("rate"),
                (baseline_by_subtype.get(subtype) or {}).get("rate"),
            ),
        }
        for subtype in set(latest_by_subtype) | set(baseline_by_subtype)
    }
    agent_timeout_rate = {
        "overall": timeout_overall,
        "by_subtype": timeout_by_subtype,
        "dominant_subtype": latest_timeout.get("dominant_subtype"),
        "dominant_share": latest_timeout.get("dominant_share"),
        "denominator": latest_timeout.get("denominator"),
        "denominator_definition": latest_timeout.get("denominator_definition"),
        "timeout_count": latest_timeout.get("timeout_count"),
        "window_days": latest_timeout.get("window_days"),
    }

    latest_format = latest.get(FORMAT_COMPLIANCE_KEY) or {}
    baseline_format = baseline.get(FORMAT_COMPLIANCE_KEY) or {}

    def _format_rate_axis(
        rate_key: str, *, suppress_drift: bool = False
    ) -> Dict[str, Any]:
        return {
            "current": latest_format.get(rate_key),
            "baseline": baseline_format.get(rate_key),
            "drift_pct": None
            if suppress_drift
            else _rate_drift_pct(
                latest_format.get(rate_key), baseline_format.get(rate_key)
            ),
            "history": [
                {
                    "captured_at": snap.get("captured_at"),
                    "value": (snap.get(FORMAT_COMPLIANCE_KEY) or {}).get(rate_key),
                }
                for snap in snapshots
                if snap.get(FORMAT_COMPLIANCE_KEY)
            ],
        }

    # Suppress drift on self_repair_success_rate when the clean denominator
    # (attempts minus infra failures) is below SELF_REPAIR_MIN_ATTEMPTS — the
    # clean count is what the metric actually divides by, so it's the right
    # threshold.  Falls back to gross repair_attempts for older snapshots that
    # predate infra-failure reporting (infra_failures absent => 0).
    _current_repair_attempts = latest_format.get("repair_attempts") or 0
    _current_infra_failures = latest_format.get("repair_infra_failures") or 0
    _current_clean_attempts = _current_repair_attempts - _current_infra_failures
    format_compliance = {
        FORMAT_COMPLIANCE_RATE_KEY: _format_rate_axis(FORMAT_COMPLIANCE_RATE_KEY),
        SELF_REPAIR_SUCCESS_RATE_KEY: _format_rate_axis(
            SELF_REPAIR_SUCCESS_RATE_KEY,
            suppress_drift=_current_clean_attempts < SELF_REPAIR_MIN_ATTEMPTS,
        ),
        "tasks_measured": latest_format.get("tasks_measured"),
        "well_formed_tasks": latest_format.get("well_formed_tasks"),
        "format_error_total": latest_format.get("format_error_total"),
        "repair_attempts": latest_format.get("repair_attempts"),
        "repair_successes": latest_format.get("repair_successes"),
        "repair_infra_failures": latest_format.get("repair_infra_failures"),
        "by_schema": latest_format.get("by_schema"),
        "format_miss_by_schema_and_reason": latest_format.get(
            "format_miss_by_schema_and_reason"
        ),
    }

    return {
        "axes": axes,
        "agent_timeout_rate": agent_timeout_rate,
        FORMAT_COMPLIANCE_KEY: format_compliance,
        # Per-member seed provenance: keyed by member name (ALL_AXES + nested
        # blocks), each value carries seeded_at / snapshot_captured_at so every
        # consumer can see which reference date each drift figure is measured from.
        "baseline_provenance": {
            k: v for k, v in member_provenance.items() if not str(k).startswith("_")
        },
        "baseline_captured_at": baseline.get("captured_at"),
        "last_snapshot_at": container.get("last_run_at") or latest.get("captured_at"),
        "vm_cold_start_breakdown": latest.get("vm_cold_start_breakdown"),
        "reuse_funnel": get_reuse_funnel_snapshot(),
        # Durable (restart-surviving) warm-reuse drop-out ranking from the latest
        # snapshot, plus its day-over-day trend. Unlike ``reuse_funnel`` above
        # (live in-memory counters), these persist in the efficiency snapshot.
        "reuse_dropout": latest.get("reuse_dropout"),
        "reuse_dropout_trend": reuse_dropout_trend,
        # Durable (restart-surviving) warm-POOL dispatch funnel from the latest
        # snapshot — lease hits, cold dispatches, overall + per-account hit rate,
        # the lease-miss-by-reason breakdown, and the single dominant-miss-reason
        # call-out — plus its day-over-day trend. A DIFFERENT funnel from
        # ``reuse_dropout`` above; both are persisted in the efficiency snapshot
        # specifically because the live in-memory funnel counters reset on restart.
        "pool_dispatch_funnel": latest.get("pool_dispatch_funnel"),
        "pool_dispatch_funnel_trend": pool_dispatch_funnel_trend,
        # Warm-pool idle cost from the latest snapshot, plus the net-cost
        # companion that folds it into cost_per_merged_pr (see compute_current_axes).
        "pool_idle_cost_usd": latest.get("pool_idle_cost_usd"),
        "pool_metrics": latest.get("pool_metrics"),
        "cost_per_merged_pr_incl_pool_idle": latest.get(
            "cost_per_merged_pr_incl_pool_idle"
        ),
        # Idle-inclusive cost as a full trended axis (current/baseline/drift/
        # history), baselined against the immutable pre-pool cost_per_merged_pr so
        # its drift directly answers "did enabling the pool regress cost per PR?".
        # Charted alongside the headline cost line in the warm-reuse-vs-cost trend.
        "cost_per_merged_pr_incl_pool_idle_axis": cost_incl_pool_idle_axis,
        # Per-origin warm-start breakdown (pool vs chain-continuity vs backlog-
        # reuse) + the pool-origin share, from the latest snapshot. Sits next to
        # the pool idle cost and both cost-per-merged-PR figures so an operator
        # can confirm BOTH halves of the goal — reuse lift above the floor AND no
        # cost regression — from one place.
        "warm_start_origin_breakdown": latest.get("warm_start_origin_breakdown"),
        # Cost-amortization companion: windowed pool idle cost (same window as
        # the pool dispatch funnel) against the live (instance-local) pool
        # dispatch funnel, so operators can see whether idle spend is being
        # amortized by warm-start hits — the same signal the maintainer's
        # cost-aware auto-drain guard acts on. Uses _compute_windowed_idle_cost
        # rather than the all-time snapshot field so lifetime idle spend from
        # before the window does NOT inflate cost_per_pool_hit_usd.
        "pool_cost_amortization": compute_pool_cost_amortization(
            _pool_amortization_windowed_idle_usd,
            pool_funnel,
        ),
        # Per-account-mix view: joins windowed per-account idle cost with the
        # per-account dispatch funnel so an operator can answer, PER ACCOUNT,
        # warm-vs-cold dispatch counts, hit rate, idle cost, and cost per warm
        # start during a controlled enable — pool lift is read off the funnel
        # hit rate, NOT the fix-loop-contaminated vm_reuse_rate.
        "pool_cost_amortization_by_account": compute_pool_cost_amortization_by_account(
            _pool_amortization_windowed_by_account,
            pool_funnel,
        ),
        # Synthesized per-project warm-pool PAYOFF VERDICT: folds the LIFT half
        # (pool-origin warm-start share + pool dispatch-funnel hit rate) and the
        # NO-regression half (idle-inclusive cost-per-merged-PR drift vs. the
        # immutable pre-pool baseline) into a single PROVEN / COST_REGRESSION /
        # NO_LIFT / INSUFFICIENT_DATA answer with supporting numbers and a
        # recommended action. Read off the POOL-specific signals, NOT the
        # fix-loop-contaminated vm_reuse_rate (see compute_pool_payoff_verdict).
        # Lift is read off the persisted snapshot funnel so it reflects the same
        # multi-day window as the cost drift.
        "pool_payoff_verdict": compute_pool_payoff_verdict(
            latest.get("warm_start_origin_breakdown"),
            cost_incl_pool_idle_axis,
            latest.get("pool_dispatch_funnel"),
        ),
        # Per-task-type format-compliance breakdown from the latest snapshot,
        # keyed by task type.  None for snapshots that predate this field (no
        # baseline/drift semantics — snapshot-only observability field).
        "format_compliance_by_type": latest.get("format_compliance_by_type"),
        # Durable per-driver LLM cost attribution from the latest snapshot: compact
        # breakdown of total LLM dollars by (provider,model), by task-type, and by
        # first-pass/rework split (llm_cost_attribution) plus a day-over-day trend
        # of the headline scalars so drift can be decomposed retroactively instead
        # of only reading that llm_cost_per_merged_pr changed.
        "llm_cost_attribution": latest.get("llm_cost_attribution"),
        "llm_cost_attribution_trend": llm_cost_attribution_trend,
        # Baseline-vs-current delta for the llm_cost_attribution decomposition:
        # which model/task-type share shifted, how much rework or self-repair moved,
        # and what the absolute USD change was.  None when either side is absent.
        # NOTE: this delta compares against the IMMUTABLE genesis baseline.
        "llm_cost_attribution_baseline_delta": compute_llm_cost_attribution_baseline_delta(
            baseline.get("llm_cost_attribution"),
            latest.get("llm_cost_attribution"),
        ),
        # Recent-vs-prior trailing-window cost-mix delta: compares the most recent K
        # snapshots against the K snapshots immediately before them. Complements the
        # genesis-baseline delta above — together they distinguish a RECENT regression
        # from cumulative project-evolution drift. None when trend depth is insufficient.
        "llm_cost_attribution_window_delta": compute_llm_cost_attribution_window_delta(
            llm_cost_attribution_trend
        ),
        # Provider routing A/B verdict (direction d-cf39cb40): DeepSeek-vs-Anthropic
        # cost ratio + verified-merge-rate delta, persisted in the daily snapshot and
        # surfaced here from the latest snapshot so the A/B is machine-checkable.
        "provider_routing_verdict": latest.get("provider_routing_verdict"),
        # Per-provider routing share from the latest snapshot: by_provider block
        # with run counts, cost, and verified landing rate per provider, plus
        # aggregate totals. Persisted alongside the verdict in the daily snapshot
        # for retroactive decomposition of model routing efficiency.
        "provider_routing_share": latest.get("provider_routing_share"),
        # Per-(provider,model) efficiency breakdown from the latest snapshot:
        # by_model rows with verified_landed_rate, cost_per_merge, run counts, and
        # aggregate totals. Persisted in the daily snapshot for retroactive A/B
        # verification of DeepSeek vs Anthropic routing across daily snapshots.
        "effort_model_efficiency": latest.get("effort_model_efficiency"),
        # VM cost attribution: per-task-type breakdown of VM spend, mirroring the
        # structure of llm_cost_attribution but for infrastructure costs. Allows
        # operators to see which task types (review, create, side-car, etc.)
        # dominate VM spend and whether that distribution has shifted.
        "vm_cost_attribution": latest.get("vm_cost_attribution"),
        # Day-over-day trend of VM cost attribution headline scalars (by_task_type,
        # total_vm_usd) so operators can track infrastructure spend drift over time.
        # Mirrors llm_cost_attribution_trend; omits pre-v8 snapshots that have no
        # vm_cost_attribution key.
        "vm_cost_attribution_trend": [
            {
                "captured_at": snap.get("captured_at"),
                "by_task_type": (snap.get("vm_cost_attribution") or {}).get(
                    "by_task_type"
                ),
                "total_vm_usd": (snap.get("vm_cost_attribution") or {}).get(
                    "total_vm_usd"
                ),
            }
            for snap in snapshots
            if snap.get("vm_cost_attribution")
        ],
    }


# ---------------------------------------------------------------------------
# Background-service helpers
# ---------------------------------------------------------------------------

# Snapshot cadence: one snapshot per UTC day.
# Service ticks every hour and decides whether the cadence has elapsed.
SNAPSHOT_INTERVAL_SECONDS = 3600
_DAY_SECONDS = 24 * 3600


def _last_run_due(last_run_at_iso: str | None, now: datetime) -> bool:
    """Return True when a new snapshot is due.

    Cadence is one snapshot per UTC day. The first call is always due.
    Subsequent calls are due once at least one day has passed since the
    previous ``last_run_at`` marker.
    """
    if not last_run_at_iso:
        return True
    try:
        last_run = datetime.fromisoformat(last_run_at_iso)
    except (TypeError, ValueError):
        return True
    if last_run.tzinfo is not None:
        last_run = last_run.replace(tzinfo=None)
    return (now - last_run).total_seconds() >= _DAY_SECONDS


async def maybe_write_daily_snapshot(
    session: AsyncSession, project_id: uuid.UUID, *, now: datetime | None = None
) -> Dict[str, Any] | None:
    """Write a snapshot iff the daily cadence has elapsed for the project.

    Reads ``design_doc["efficiency_baseline"]["last_run_at"]`` for the
    cadence marker. The marker doubles as a multi-instance-deploy guard:
    even if leader election briefly hands off, the next instance sees the
    marker and skips. Returns the snapshot dict on write, None on skip.
    """
    if now is None:
        now = _utcnow()

    # populate_existing: a caller that left a partial Project (e.g. via
    # ``load_only(Project.id)``) in the session identity map would otherwise
    # return that cached partial here, and the ``design_doc`` access below
    # would fire a sync lazy-load in async context. See ``run_once``.
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        return None

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    if not _last_run_due(container.get("last_run_at"), now):
        return None
    return await write_snapshot(session, project_id)


async def maybe_alert_pool_payoff_transition(
    session: AsyncSession, project_id: uuid.UUID
) -> str | None:
    """Raise an operator Alert when a scoped project's warm-pool payoff verdict
    crosses a decision boundary, deduplicated per project + verdict.

    Closes the loop on a *bounded* warm-pool enable: a controlled single-project
    enable otherwise relies on the operator remembering to come back and read the
    payoff verdict off the warm-VM panel. This per-project check — run every
    hourly snapshot tick, independent of the daily snapshot-write cadence — emits
    a deduplicated Alert when the verdict transitions to ``COST_REGRESSION``
    (WARNING; recommend reducing the target or disabling), so the keep / widen /
    disable decision is driven without polling. ``PROVEN`` remains visible in
    payoff summaries but does not create a durable alert.

    Reuses the verdict synthesized by :func:`read_baseline_with_drift` (which
    folds in :func:`compute_pool_payoff_verdict`) rather than re-deriving
    thresholds. ``NO_LIFT`` and ``INSUFFICIENT_DATA`` are not alert-worthy (the
    operator should keep measuring / tune, not be paged). DISTINCT from the
    silent fleet-wide cost-guard auto-drain in ``warm_pool_maintainer``: that
    protects spend automatically; this informs a per-project strategic decision.

    The transition edge is detected against ``last_alerted_payoff_verdict`` in the
    efficiency-baseline container (locked read-modify-write, mirroring
    :func:`write_snapshot`); the Alert title dedup is the cross-instance backstop.
    Gated on :func:`app.warm_pool.is_project_warm_pool_scoped` so only the scoped
    bounded-enable projects are evaluated. Returns the verdict state when an alert
    fired, else ``None``.
    """
    from app.alert_dispatch import _maybe_create_pool_payoff_alert
    from app.alert_policy import alert_policy_allows
    from app.models import AlertSeverity
    from app.task_locking import with_efficiency_snapshot_locked
    from app.warm_pool import is_project_warm_pool_scoped

    if not is_project_warm_pool_scoped(project_id):
        return None

    drift = await read_baseline_with_drift(session, project_id)
    verdict = drift.get("pool_payoff_verdict") or {}
    state = verdict.get("state")
    supporting = dict(verdict.get("supporting") or {})
    recommended_action = verdict.get("recommended_action") or ""

    # Decide alert-worthiness + severity. COST_REGRESSION alerts; PROVEN,
    # NO_LIFT, and INSUFFICIENT_DATA remain dashboard/history states.
    if state == POOL_PAYOFF_COST_REGRESSION:
        severity = AlertSeverity.WARNING
    else:
        return None

    decision = await alert_policy_allows(
        session,
        project_id,
        "warm_pool.payoff",
        severity=severity,
    )
    if not decision.emit:
        logger.info(
            "[POOL_PAYOFF_ALERT] project=%s verdict=%s suppressed=%s",
            project_id,
            state,
            decision.reason,
        )
        return None

    # Transition edge: only fire when the alert-worthy state differs from the
    # last state we have a durable alert for. The marker is advanced only after
    # an active alert row exists; otherwise a policy race could record "alerted"
    # without any operator-visible alert.
    _proj = await session.get(Project, project_id)
    if _proj is None:
        return None
    container_read = dict((_proj.design_doc or {}).get(DESIGN_DOC_KEY) or {})
    should_fire = container_read.get(LAST_ALERTED_PAYOFF_KEY) != state

    if not should_fire:
        return None

    alert, created = await _maybe_create_pool_payoff_alert(
        project_id,
        session,
        verdict_state=state,
        supporting=supporting,
        recommended_action=recommended_action,
        severity=severity,
    )
    if alert is None:
        return None

    changed = {"fire": False}

    def _advance_marker(container: Dict[str, Any]) -> None:
        if container.get(LAST_ALERTED_PAYOFF_KEY) == state:
            return
        container[LAST_ALERTED_PAYOFF_KEY] = state
        changed["fire"] = True
        changed["container"] = container

    wrote_marker = await with_efficiency_snapshot_locked(
        session, project_id, _advance_marker, skip_locked=False
    )
    if wrote_marker and changed["fire"]:
        from app.project_telemetry import set_project_telemetry
        if "container" in changed:
            await set_project_telemetry(session, project_id, DESIGN_DOC_KEY, changed["container"])
        await session.commit()
    else:
        await session.rollback()
    if not created:
        return None
    logger.info(
        "[POOL_PAYOFF_ALERT] project=%s verdict=%s severity=%s",
        project_id,
        state,
        severity.value,
    )
    return state


async def maybe_alert_unpriced_models_coverage(
    session: AsyncSession, project_id: uuid.UUID
) -> bool | None:
    """Raise an operator Alert when a project's recent LLM spend runs on
    unpriced (provider, model) combinations, edge-triggered and deduplicated.

    The cost-analytics surface prices each task's tokens from a hardcoded
    pricing table; a task on a (provider, model) absent from that table is
    counted as $0, silently understating cost-per-merge and total spend. This
    recurs naturally — the pipeline adopts newer models faster than the table is
    updated. The only prior signal was the cost-breakdown panel's coverage
    caveat; this proactively pages the operator instead.

    Reuses :func:`_compute_llm_cost_pricing_coverage` as the source of truth for
    coverage rather than re-deriving it. The alert fires when the unpriced share
    of *attributed* token-bearing tasks crosses
    :data:`UNPRICED_ALERT_SHARE_THRESHOLD` with at least
    :data:`UNPRICED_ALERT_MIN_TASKS` unpriced tasks. Token-less and
    unattributed (no provider/model) tasks are excluded from the denominator and
    never trip it.

    Edge-triggered against ``last_alerted_unpriced_coverage`` in the
    efficiency-baseline container (locked read-modify-write, mirroring
    :func:`maybe_alert_pool_payoff_transition`): fires once when coverage crosses
    into degraded, does not re-fire while it stays degraded, and re-arms when
    coverage recovers. Runs every hourly snapshot tick for every project.
    Returns ``True`` when an alert fired, else ``None``.
    """
    from app.alert_dispatch import _maybe_create_unpriced_models_alert
    from app.models import AlertSeverity
    from app.task_locking import with_efficiency_snapshot_locked

    tasks = await _load_recent_tasks(session, project_id)
    coverage = _compute_llm_cost_pricing_coverage(tasks)
    priced = int(coverage.get("priced_tasks") or 0)
    unpriced = int(coverage.get("unpriced_tasks") or 0)
    attributed = priced + unpriced
    unpriced_share = (unpriced / attributed) if attributed else 0.0
    degraded = (
        unpriced >= UNPRICED_ALERT_MIN_TASKS
        and unpriced_share >= UNPRICED_ALERT_SHARE_THRESHOLD
    )

    # Transition edge: fire only when crossing into degraded from a non-degraded
    # marker; re-arm the marker on recovery so a future degradation alerts again.
    # Locked read-modify-write via the dedicated efficiency snapshot helper so a
    # concurrent snapshot writer cannot clobber the marker.
    changed = {"fire": False}

    def _advance_marker(container: Dict[str, Any]) -> None:
        prev = container.get(LAST_ALERTED_UNPRICED_KEY)
        new_state = "degraded" if degraded else "healthy"
        if prev == new_state:
            return  # no transition — leave the marker untouched
        container[LAST_ALERTED_UNPRICED_KEY] = new_state
        changed["fire"] = degraded  # only the degraded crossing raises an alert
        changed["container"] = container

    await with_efficiency_snapshot_locked(
        session, project_id, _advance_marker, skip_locked=False
    )
    if changed.get("container") is not None:
        from app.project_telemetry import set_project_telemetry
        await set_project_telemetry(session, project_id, DESIGN_DOC_KEY, changed["container"])
    if not changed["fire"]:
        return None

    await _maybe_create_unpriced_models_alert(
        project_id,
        session,
        unpriced_combos=list(coverage.get("unpriced_combos") or []),
        unpriced_task_count=unpriced,
        attributed_task_count=attributed,
        unpriced_share=unpriced_share,
        severity=AlertSeverity.WARNING,
    )
    logger.info(
        "[UNPRICED_MODELS_ALERT] project=%s unpriced=%d/%d share=%.2f",
        project_id,
        unpriced,
        attributed,
        unpriced_share,
    )
    return True


async def maybe_alert_self_repair_burn_share(
    session: AsyncSession, project_id: uuid.UUID
) -> bool | None:
    """Raise an operator Alert when a project's self-repair burn-share exceeds ceiling.

    Self-repair tokens (reopen attempts during structured-output format recovery)
    are overhead that reduce effective LLM productivity. When repair tokens consume
    an excessive fraction of total LLM tokens, it signals repair-effectiveness drift
    and warrants investigation of the underlying failure patterns that require
    multiple attempts to resolve.

    Reuses :func:`_compact_llm_cost_attribution` as the source of truth for
    burn-share calculation. The alert fires when the self_repair_burn_share
    crosses :data:`SELF_REPAIR_BURN_SHARE_THRESHOLD`.

    Edge-triggered against ``last_alerted_self_repair_burn_share`` in the
    efficiency-baseline container (locked read-modify-write, mirroring
    :func:`maybe_alert_unpriced_models_coverage`): fires once when burn-share
    crosses above threshold from below, does not re-fire while it stays elevated,
    and re-arms when burn-share recovers. Runs every hourly snapshot tick for
    every project. Returns ``True`` when an alert fired, else ``None``.
    """
    from app.alert_dispatch import _maybe_create_self_repair_burn_share_alert
    from app.models import AlertSeverity
    from app.task_locking import with_efficiency_snapshot_locked

    from app.digest_metrics import _accumulate_format_compliance

    tasks = await _load_recent_tasks(session, project_id)
    llm_attribution = _compact_llm_cost_attribution(tasks)
    burn_share = llm_attribution.get(SELF_REPAIR_BURN_SHARE_KEY)

    # Extract top offending schemas by repair_attempt count for actionable alert context.
    fc_block = _accumulate_format_compliance(tasks)
    by_schema = fc_block.get("by_schema") or {}
    top_schemas: list[tuple[str, int]] = sorted(
        ((name, entry.get("repair_attempts", 0)) for name, entry in by_schema.items()
         if entry.get("repair_attempts", 0) > 0),
        key=lambda x: x[1],
        reverse=True,
    )[:3]

    # Elevated when burn_share exists and exceeds the threshold.
    elevated = (
        isinstance(burn_share, (int, float))
        and burn_share >= SELF_REPAIR_BURN_SHARE_THRESHOLD
    )

    # Transition edge: fire only when crossing into elevated from a non-elevated
    # marker; re-arm the marker on recovery so a future elevation alerts again.
    # Locked read-modify-write via the dedicated efficiency snapshot helper.
    changed = {"fire": False}

    def _advance_marker(container: Dict[str, Any]) -> None:
        prev = container.get(LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY)
        new_state = "elevated" if elevated else "healthy"
        if prev == new_state:
            return  # no transition — leave the marker untouched
        container[LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY] = new_state
        changed["fire"] = elevated  # only the elevated crossing raises an alert
        changed["container"] = container

    await with_efficiency_snapshot_locked(
        session, project_id, _advance_marker, skip_locked=False
    )
    if changed.get("container") is not None:
        from app.project_telemetry import set_project_telemetry
        await set_project_telemetry(session, project_id, DESIGN_DOC_KEY, changed["container"])
    if not changed["fire"]:
        return None

    await _maybe_create_self_repair_burn_share_alert(
        project_id,
        session,
        burn_share=burn_share,
        threshold=SELF_REPAIR_BURN_SHARE_THRESHOLD,
        severity=AlertSeverity.WARNING,
        top_schemas=top_schemas or None,
    )
    logger.info(
        "[SELF_REPAIR_BURN_SHARE_ALERT] project=%s burn_share=%.4f threshold=%.4f",
        project_id,
        burn_share,
        SELF_REPAIR_BURN_SHARE_THRESHOLD,
    )
    return True


async def maybe_alert_provider_routing_transition(
    session: AsyncSession, project_id: uuid.UUID
) -> str | None:
    """Raise an operator Alert when the provider routing A/B verdict transitions
    to ADOPT or REJECT, deduplicated per project + verdict.

    The DeepSeek-vs-Anthropic routing A/B produces a verdict from the per-model
    efficiency breakdown once enough PR-eligible tasks have landed on each side.
    ADOPT (DeepSeek is cheaper with no quality regression) and REJECT (cost not
    meaningfully lower or quality regressed) are decision boundaries — operators
    should act on them, not poll the efficiency panel to discover them.

    Edge-triggered against ``last_alerted_routing_verdict`` in the efficiency-
    baseline container (locked read-modify-write, mirroring
    :func:`maybe_alert_pool_payoff_transition`): fires once per verdict state,
    does not re-fire while the same verdict persists. Returns the verdict state
    when an alert fired, else ``None``.
    """
    from app.alert_dispatch import _maybe_create_provider_routing_alert
    from app.alert_policy import alert_policy_allows
    from app.models import AlertSeverity
    from app.task_locking import with_efficiency_snapshot_locked

    drift = await read_baseline_with_drift(session, project_id)
    verdict = drift.get("provider_routing_verdict") or {}
    state = verdict.get("state")

    # Only ADOPT and REJECT are alert-worthy; INSUFFICIENT_DATA is the
    # default "keep measuring" state and should not page operators.
    if state not in (PROVIDER_ROUTING_ADOPT, PROVIDER_ROUTING_REJECT):
        return None

    supporting = dict(verdict.get("supporting") or {})
    recommended_action = verdict.get("recommended_action") or ""

    decision = await alert_policy_allows(
        session,
        project_id,
        "efficiency.provider_routing_verdict",
        severity=AlertSeverity.WARNING,
    )
    if not decision.emit:
        logger.info(
            "[ROUTING_ALERT] project=%s verdict=%s suppressed=%s",
            project_id,
            state,
            decision.reason,
        )
        return None

    # Transition edge: only fire when the alert-worthy state differs from the
    # last state we have a durable alert for.
    _proj = await session.get(Project, project_id)
    if _proj is None:
        return None
    container_read = dict((_proj.design_doc or {}).get(DESIGN_DOC_KEY) or {})
    should_fire = container_read.get(LAST_ALERTED_ROUTING_KEY) != state

    if not should_fire:
        return None

    alert, created = await _maybe_create_provider_routing_alert(
        project_id,
        session,
        verdict_state=state,
        supporting=supporting,
        recommended_action=recommended_action,
        severity=AlertSeverity.WARNING,
    )
    if alert is None:
        return None

    # Advance the marker inside an efficiency-snapshot locked block so a concurrent
    # snapshot writer cannot clobber it.
    changed = {"fire": False}

    def _advance_marker(container: Dict[str, Any]) -> None:
        if container.get(LAST_ALERTED_ROUTING_KEY) == state:
            return
        container[LAST_ALERTED_ROUTING_KEY] = state
        changed["fire"] = True
        changed["container"] = container

    wrote_marker = await with_efficiency_snapshot_locked(
        session, project_id, _advance_marker, skip_locked=False
    )
    if wrote_marker and changed["fire"]:
        from app.project_telemetry import set_project_telemetry
        if "container" in changed:
            await set_project_telemetry(session, project_id, DESIGN_DOC_KEY, changed["container"])
        await session.commit()
    else:
        await session.rollback()
    if not created:
        return None
    logger.info(
        "[ROUTING_ALERT] project=%s verdict=%s",
        project_id,
        state,
    )
    return state


# ---------------------------------------------------------------------------
# HTTP endpoints (sub-router, mounted by app.routers.admin_digest)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/efficiency-axes")
async def get_efficiency_axes(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return per-axis current/baseline/drift/history for the project's efficiency baseline.

    Reads ``project.design_doc["efficiency_baseline"]`` (populated daily by
    the ``_efficiency_snapshot_service``) and returns each of the ten
    foundational axes — ``cost_per_merged_pr``, ``vm_cold_start_p50_ms``,
    ``vm_reuse_rate``, ``median_api_calls_per_task``,
    ``median_review_iterations``, ``llm_cost_per_merged_pr``,
    ``lane_cost_per_merged_pr``, ``lane_warm_resume_rate``,
    ``checkpoint_loss_rate``, ``prompt_static_bytes`` — with the latest
    snapshot value, the immutable baseline value, percentage drift from
    baseline, and a time-series of up to 365 daily snapshots for trend
    rendering.

    Also returns ``vm_cold_start_breakdown``: a per-stage p50 ms breakdown of
    the cold-start interval from the latest snapshot.  ``dispatch_to_slot_ms``
    is the queue-wait before a provisioning slot is acquired (isolates our own
    concurrency throttle from real GCE boot latency); the remaining stages
    decompose boot through agent claim.  All values are None when no tasks in
    the latest snapshot had stage data.

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
          "last_snapshot_at": iso8601|None,
          "vm_cold_start_breakdown": {
            "dispatch_to_slot_ms": float|None,
            "dispatch_to_creds_ms": float|None,
            "creds_to_env_ms": float|None,
            "env_to_payload_ms": float|None,
            "payload_to_systemctl_ms": float|None,
            "systemctl_to_startup_ms": float|None,
            "startup_to_claim_ms": float|None
          } | None
        }

    ``drift_pct`` is None whenever either side is missing or baseline is 0.
    ``history`` is ordered oldest-first; before any snapshot fires it is ``[]``.
    Before the first snapshot fires, all axis values are None and both
    timestamps are None.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return await read_baseline_with_drift(session, project_id)


def _format_rework_attribution(rework_breakdown: Dict[str, Any]) -> Dict[str, Any]:
    """Transform compute_pr_rework_chain_breakdown output to rework_attribution API format.

    Maps internal field names to API contract: rework_share_of_total_llm_cost ->
    rework_share, rework_llm_usd -> rework_llm_cost_usd, etc. Filters top_rework_prs
    to only include fields needed by the frontend.
    """
    return {
        "rework_share": rework_breakdown.get("rework_share_of_total_llm_cost"),
        "first_pass_llm_cost_usd": rework_breakdown["first_pass_llm_usd"],
        "rework_llm_cost_usd": rework_breakdown["rework_llm_usd"],
        "top_rework_prs": [
            {
                "pr_number": pr["pr_number"],
                "rework_llm_cost_usd": pr["rework_llm_usd"],
                "rework_vm_cost_usd": pr["rework_vm_usd"],
            }
            for pr in rework_breakdown["top_rework_prs"]
        ],
    }


def _compute_llm_cost_by_account(tasks: list[Task]) -> list[Dict[str, Any]]:
    """Per-account LLM token/cost rollup — groups tasks by llm_account_id, sorted by cost descending."""
    from collections import defaultdict
    from app.max_plan_pool import get_task_llm_account_id

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "llm_call_count": 0,
                 "task_count": 0, "cost_usd": 0.0}
    )
    for t in tasks:
        account_id = get_task_llm_account_id(t) or "unknown"
        b = buckets[account_id]
        seen_this_task = False
        for (provider, model, it, ot, cc, cache_cr, cache_rd) in _task_llm_attempts(t):
            if it <= 0 and ot <= 0:
                continue
            key = (provider.lower(), model.lower()) if provider and model else None
            pricing = LLM_TOKEN_PRICING.get(key) if key else None
            if pricing:
                cost = _price_llm_attempt(it, ot, cache_cr, cache_rd, *pricing,
                                           provider=provider, model=model)
            else:
                cost = 0.0
            b["input_tokens"] += int(it)
            b["output_tokens"] += int(ot)
            b["llm_call_count"] += cc
            b["cost_usd"] += cost
            seen_this_task = True
        if seen_this_task:
            b["task_count"] += 1
    for v in buckets.values():
        v["cost_usd"] = round(v["cost_usd"], 6)
    return [
        {"account_id": aid, **data}
        for aid, data in sorted(buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    ]


@router.get("/projects/{project_id}/llm-cost-breakdown")
async def get_llm_cost_breakdown(
    project_id: uuid.UUID,
    window_days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-provider and per-model LLM cost/token breakdown over a project window.

    Read-only view over recently-terminal tasks (COMPLETED/FAILED/CANCELLED
    within ``window_days``, default 7). Reuses the snapshot pricing table
    (:data:`app.efficiency_snapshot.LLM_TOKEN_PRICING`) so cost numbers are
    consistent with ``llm_cost_per_merged_pr``.

    Response shape::

        {
          "window_days": int,
          "generated_at": iso8601,
          "by_provider": [
            {
              "provider": str,
              "total_input_tokens": int,
              "total_output_tokens": int,
              "total_cost_usd": float,
              "mean_cost_per_task": float,
              "share_of_total_cost": float|None,
              "task_count": int,
              "llm_call_count": int
            }, ...
          ],
          "by_model": [
            {
              "provider": str,
              "model": str,
              "total_input_tokens": int,
              "total_output_tokens": int,
              "total_cost_usd": float,
              "mean_cost_per_task": float,
              "share_of_total_cost": float|None,
              "task_count": int,
              "llm_call_count": int,
              "priced": bool
            }, ...
          ],
          "totals": {
            "total_input_tokens": int,
            "total_output_tokens": int,
            "total_cost_usd": float,
            "task_count": int,
            "priced_task_count": int,
            "unpriced_task_count": int,
            "unpriced_coverage_fraction": float|None
          },
          "unpriced_combos": [str, ...],
          "rework_attribution": {
            "rework_share": float|None,
            "first_pass_llm_cost_usd": float,
            "rework_llm_cost_usd": float,
            "top_rework_prs": [
              {
                "pr_number": int,
                "rework_llm_cost_usd": float,
                "rework_vm_cost_usd": float
              }, ...
            ]
          }
        }
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    tasks = await _load_recent_tasks(session, project_id, window_days=window_days)
    breakdown = compute_llm_cost_breakdown(tasks)
    rework_breakdown = compute_pr_rework_chain_breakdown(tasks)
    return {
        "window_days": window_days,
        "generated_at": _utcnow().isoformat(),
        **breakdown,
        "by_task_type": compute_llm_cost_by_task_type(tasks),
        "by_account": _compute_llm_cost_by_account(tasks),
        "rework_attribution": _format_rework_attribution(rework_breakdown),
    }


@router.get("/projects/{project_id}/pool-hit-rate-estimate")
async def get_pool_hit_rate_estimate(
    project_id: uuid.UUID,
    target_size: int = Query(..., ge=0, le=100),
    window_days: int = Query(default=7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Offline counterfactual estimate of the warm-pool hit rate a project would
    achieve at a candidate ``target_size``, before any idle cost is paid.

    The warm pool ships disabled, so its observed hit rate is 0 and no operator
    has evidence that enabling it for a project will pay off. This read-only
    endpoint replays the project's recent per-account dispatch history
    (``vm_dispatched_at`` + resolved account on recently-terminal tasks within
    ``window_days``) over a simulated idle pool of ``target_size`` members and
    reports the share of dispatches that would plausibly have been served warm —
    so an operator can size the pool before enabling it, and re-run to tune N.

    It does NOT change the dispatch/claim path or provision anything; the window
    is bounded (``window_days`` ≤ 30, ≤ 5000 tasks) so it stays cheap on large
    histories. See :func:`estimate_pool_hit_rate` for the model and
    ``assumptions`` for the stated caveats.

    Response shape::

        {
          "project_id": str,
          "target_size": int,
          "window_days": int,
          "replenish_seconds": int,
          "idle_ttl_seconds": int,
          "total_dispatches": int,
          "estimated_warm_hits": int,
          "estimated_hit_rate": float|None,   # None when no dispatches in window
          "by_account": {"<account>": {
              "total_dispatches": int,
              "estimated_warm_hits": int,
              "estimated_hit_rate": float|None,
              "allocated_slots": int
          }, ...},
          "account_retarget_scenario": {
              "estimated_warm_hits": int,
              "estimated_hit_rate": float|None
          },
          "assumptions": str
        }
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return await estimate_pool_hit_rate(
        session, project_id, target_size=target_size, window_days=window_days
    )


async def compute_project_window_spend_usd(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_hours: int = 24,
) -> float:
    """Total (VM + LLM) spend for a project over a rolling window.

    Includes both terminal and in-flight tasks so the figure reflects
    real recent spend. Primary use case: the per-project spend ceiling gate
    in autonomous_research.

    A task is counted when its VM span overlaps the window — i.e. the task
    has a vm_provisioned_at AND is either still running (no vm_stopped_at) or
    stopped after the window cutoff. Long-running tasks that started before the
    window boundary but are still in-flight or only stopped inside the window
    are therefore included, closing the bypass hole for the spend ceiling gate.
    LLM-only tasks (no vm_provisioned_at) are captured via a fallback on
    created_at so existing LLM token cost behaviour is preserved.
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            or_(
                # VM span overlaps the window: has a VM and either still running
                # or stopped after the window cutoff.
                Task.vm_provisioned_at.isnot(None)
                & (Task.vm_stopped_at.is_(None) | (Task.vm_stopped_at >= cutoff)),
                # LLM-only tasks (no VM span): fall back to created_at so token
                # cost is still captured for tasks that never provisioned a VM.
                Task.vm_provisioned_at.is_(None) & (Task.created_at >= cutoff),
            ),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.vm_spot,
                Task.vm_machine_type,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                Task.vm_runtime_cost_banked_db,
                Task.attempt_history_db,
                Task.api_call_summary,
                Task.provider,
                Task.model,
                Task.llm_input_tokens_banked_db,
                Task.llm_output_tokens_banked_db,
                Task.llm_call_count_banked_db,
                raiseload=True,
            )
        )
    )
    now = _utcnow()
    tasks = list((await session.execute(stmt)).scalars().all())
    total = 0.0
    for t in tasks:
        # In-flight tasks have no vm_stopped_at; estimate cost up to now so
        # long-running tasks cannot bypass the spend ceiling.
        stopped_at = now if (t.vm_provisioned_at and not t.vm_stopped_at) else None
        total += get_task_total_vm_cost_usd(t, stopped_at=stopped_at)
        llm_cost, _any_unpriced = _task_llm_cost(t)
        total += llm_cost
    return round(total, 6)


# ---------------------------------------------------------------------------
# Agent-authenticated efficiency-snapshot read endpoint
# ---------------------------------------------------------------------------
# Research tasks that authenticate with an agent token (not admin credentials)
# can read the latest snapshot's per-task-type format-compliance breakdown here
# without needing access to the admin digest path.
#
# Auth: the same X-Agent-Token JWT used by all other agent endpoints; JWT is
# verified via verify_agent_jwt (the low-level validator), which avoids a
# circular import through app.routers.agent. For a read-only telemetry endpoint
# the task-state liveness checks in verify_agent_token are unnecessary.


@agent_router.get("/efficiency-snapshot/format-compliance")
async def agent_get_efficiency_snapshot_format_compliance(
    session: AsyncSession = Depends(get_session),
    x_agent_token: str = Header(..., description="Agent JWT", alias="X-Agent-Token"),
):
    """Return the latest snapshot's per-task-type format-compliance breakdown.

    Read-only endpoint for agent-authenticated tasks (research/measure task
    types directed at format-compliance reads) that cannot reach the admin
    digest path. Authenticates with the same X-Agent-Token JWT used by all
    other agent endpoints; extracts the project_id from the task identified
    by the token's ``sub`` claim.

    Returns::

        {
            "captured_at": "<ISO timestamp>",
            "format_compliance": {
                "format_compliance_rate": {...},
                "self_repair_success_rate": {...},
                "tasks_measured": <int>,
                "well_formed_tasks": <int>,
                "format_error_total": <int>,
                "repair_attempts": <int>,
                "repair_successes": <int>,
                "repair_infra_failures": <int>,
                "by_schema": {<schema_label>: {"miss": <int>, "repair_attempt": <int>, "repair_success": <int>}, ...}
            },
            "format_compliance_by_type": {<task_type>: {...}, ...},
            "by_schema": {<schema_label>: {"miss": <int>, "repair_attempt": <int>, "repair_success": <int>}, ...}
        }

    ``by_schema`` is also exposed at the top level (mirroring its location
    inside ``format_compliance``) so callers can access per-schema miss and
    repair counts without navigating the nested blob. Returns 404 when no
    snapshot exists yet.
    """
    from app.auth import reject_tool_asset_scope, verify_agent_jwt

    payload = verify_agent_jwt(x_agent_token)
    # A tool-asset capability token carries scope="tool-asset" and must not be
    # usable to read the efficiency snapshot — reject it with 403 (out of scope).
    reject_tool_asset_scope(payload)
    task_id_str = payload.get("sub")
    project_id_str = payload.get("project_id")

    if not task_id_str and not project_id_str:
        raise HTTPException(status_code=401, detail="token_missing_subject")

    if task_id_str:
        try:
            task_uuid = uuid.UUID(task_id_str)
        except ValueError:
            raise HTTPException(
                status_code=401, detail="token_invalid_subject"
            ) from None
        task = await session.get(Task, task_uuid)
        if task is None:
            raise HTTPException(status_code=404, detail="task_not_found")
        project_id = task.project_id
    else:
        try:
            project_id = uuid.UUID(project_id_str)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=401, detail="token_invalid_project_id"
            ) from None

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    snapshots = await _read_snapshot_history(session, project.id, container)
    if not snapshots:
        raise HTTPException(status_code=404, detail="no_snapshot_available")

    latest = snapshots[-1]
    fc_blob = latest.get(FORMAT_COMPLIANCE_KEY) or {}
    return {
        "captured_at": latest.get("captured_at"),
        FORMAT_COMPLIANCE_KEY: fc_blob,
        "format_compliance_by_type": latest.get("format_compliance_by_type"),
        "by_schema": fc_blob.get("by_schema"),
    }
