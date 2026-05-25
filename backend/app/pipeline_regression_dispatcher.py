"""Pipeline-regression dispatcher.

Watches existing pipeline-wide regression signals (SR catch-rate, fix-loop
divergence, pipeline success-rate trend) and spawns ONE ``investigate`` task per
detected regression class so the existing investigate→fix chain
(``_handle_investigation_completion`` → ``_spawn_investigation_remediation``)
can autonomously diagnose and ship fixes for the autonomous loop's own
pipeline regressions.

Background-service-crash spawns are wired separately from the watchdog path
(``lifecycle._spawn_crash_investigations``). That path keys its own dedup off
``Task.context.service_name`` rather than ``regression_class``, so the two
paths do not share an in-flight dedup key — each enforces its own.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.deploy_health import (
    get_deploy_monitoring_defer_until,
    record_deploy_monitoring_deferral,
)
from app.models import (
    Alert,
    AlertSeverity,
    DeploymentState,
    ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
    ESCALATION_REASON_ESCALATED_CROSS_LAYER,
    ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
    ESCALATION_REASON_ESCALATED_RECURRENCE,
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.regression_layer_classifier import (
    LAYER_DISPLAY_ORDER,
    aggregate_targeted_layers,
    is_recurring_cross_layer_class,
)
from app.regression_dispatcher_metrics import (  # noqa: F401
    _ESCALATION_BUCKET_ALL_OTHER,
    _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP,
    _ESCALATION_BUCKET_ORDER,
    _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
    _META_ESCALATION_REASONS,
    _build_layer_defense_ledger,
    _compliance_followup_fix_idx,
    _compute_layer_fragility_stats,
    _compute_recovery_layer_mismatch,
    _layers_targeted_union,
    compute_avg_prevention_rate_7d,
    compute_class_layer_defense_counts,
    compute_escalation_outcome_breakdown,
    compute_first_attempt_compliance_trend,
    compute_layer_defense_density,
    compute_mtti_p50_sec,
    compute_untargeted_layers,
)
from app.dispatcher_read_api import (  # noqa: F401
    _REGRESSION_RECOVERY_COOLDOWN,
    get_dispatcher_cycle_records,
    get_regression_mtta_data,
    get_sr_persistent_blind_spot_hint,
)
from app.regression_layer_observations import (  # noqa: F401
    _LAYER_SYMPTOM_BOUND,
    _LAYER_SYMPTOM_WINDOW,
    _derive_observation_for_layer_classify,
    _distinct_recent_layers,
    _record_layer_observation,
    _representative_per_layer,
)
from app.shared_state import (
    runtime_config as _runtime_config,
    get_project_integration_branch as _get_project_integration_branch,
)
from app.time_utils import _utcnow
from app.dispatcher_formatters import (
    _CROSS_CASE_FINGERPRINT_LOOKBACK,
    _extract_evaluating_since,
    _extract_recurrence_cycles,
    _format_bundle_signal_lines,
    _format_cross_case_fingerprint_prompt,
    _format_cross_layer_prompt,
    _format_dispatcher_duration,
    _format_fragile_recurrence_meta_prompt,
    _format_layer_concentration_prompt,
    _format_prior_attempt_context,
    _format_prior_verdict_context,
    _format_recurrence_meta_prompt,
    _format_recurring_class_prompt,
    _format_reviewer_sla_stall_prompt,
    _format_signal_lines,
    _format_sr_fp_signal_lines,
    _format_strategic_stall_prompt,
    _format_theme_layer_meta_prompt,
    _is_fragile_recurrence,
    _parse_dispatcher_dt,
    _parse_iso_timestamp,
)
from app.dispatcher_formatters import (  # noqa: F401
    _SR_FP_RATE_FLOOR,
    _parse_signal_timestamp,
)
from app.pipeline_health import (  # noqa: F401
    _LONE_CHECKER_ALERT_TITLE,
    _evaluate_lone_checker_alert,
    _evaluate_sr_catch_rate_targets,
    compute_pipeline_health_summary,
)
from app.regression_recovery_watchdog import (  # noqa: F401
    _auto_close_recovered_manual_intervention,
    _run_compliance_followup_once,
    run_dispatcher_watchdog_alarms,
    run_recovery_once,
    run_watchdog_once,
)
from app.regression_recovery_watchdog import (
    _ensure_watchdog_alert_open,
    _resolve_watchdog_alert,
    _run_signal_staleness_watchdog,
)
from app.regression_recovery_evaluators import (  # noqa: F401
    BuiltInSignalSpec,
    BUILT_IN_SIGNAL_SPECS,
    DISPATCH_BUILT_IN_SIGNAL_SPECS,
    RegressionClassShape,
    SPAWNABLE_REGRESSION_CLASS_SHAPES,
    TelemetrySignalSpec,
    TELEMETRY_SIGNAL_SPECS,
    _ALL_EVALUATOR_NAMES,
    _recover_agent_vm_resource,
    _recover_cross_case_fingerprint,
    _recover_fix_loop_divergence_fingerprint,
    _recover_layer_concentration,
    _recover_observation_alert_sla,
    _recover_prevention_low,
    _recover_recurring_class,
    _recover_reviewer_sla_stall,
    _recover_sr_below_floor,
    _recover_sr_false_attestation,
    _recover_sr_pass_engagement_thinning,
    _recover_strategic_cycle_stall,
    _recover_viewport_coverage_drop,
    _recover_viewport_regression,
    _SIGNAL_STALENESS_WINDOWS,
    _SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW,
    _SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW,
    get_telemetry_signal_spec,
    match_spawnable_shape,
)
from app.viewport_coverage import (  # noqa: F401
    compute_viewport_coverage_snapshot,
    update_viewport_coverage_snapshot_from_diff,
)
from app.dispatcher_signal_data import (  # noqa: F401
    _AGENT_VM_RESOURCE_MIN_FAILURES,
    _AGENT_VM_RESOURCE_WINDOW_HOURS,
    _FIX_LOOP_DIVERGENCE_FINGERPRINT_MIN_TASKS,
    _FIX_LOOP_DIVERGENCE_FINGERPRINT_WINDOW_DAYS,
    _RECURRING_CLASS_META_PREFIXES,
    _VIEWPORT_REGRESSION_MIN_FAILURES,
    _VIEWPORT_REGRESSION_WINDOW_DAYS,
    _collect_agent_vm_failures,
    _collect_fix_loop_divergence_fingerprints,
    _collect_viewport_failures,
    _current_signal_value_for_class,
    _extract_failure_type,
    _group_recurring_class_entries,
    _index_cross_case_fingerprints,
    _signal_higher_is_better,
)
from app.regression_layer_backfill import (  # noqa: F401
    _AWAITING_BACKFILL_AGE_THRESHOLD,
    _AWAITING_BACKFILL_ALERT_TITLE,
    _AWAITING_BACKFILL_COUNT_THRESHOLD,
    _AWAITING_BACKFILL_WINDOW,
    _LAYER_BACKFILL_PER_TICK,
    _OBSERVATION_ALERT_SLA_HOURS,
    _OBSERVATION_ALERT_SLA_HOURS_DEFAULT,
    _OBSERVATION_ONLY_ALERT_TITLES,
    _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE,
    _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE,
    _PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD,
    _PATH_CLASSIFIER_COVERAGE_TOP_PATHS,
    _UNMATCHED_PATHS_PER_ENTRY_CAP,
    _backfill_layers_targeted_once,
    _classify_layers_from_paths,
    _escalate_stale_observation_alerts,
    _evaluate_awaiting_backfill_alert,
    _evaluate_path_classifier_coverage,
    compute_unclassified_fix_coverage,
)

logger = logging.getLogger(__name__)

ESCALATION_REASON_FRAGILE_RECURRENCE_META = "fragile_recurrence_meta"
ESCALATION_REASON_RECURRING_CLASS_META = "recurring_class_meta"

# Cooldown between consecutive spawns for the SAME regression_class. Mirrors
# the self_audit_dispatch cooldown pattern — keeps a noisy regression from
# generating an investigate every cycle once the prior task terminates.
_REGRESSION_COOLDOWN = timedelta(hours=6)

# Suppression window when an investigation reports verdict=="not_a_regression"
# on a pipeline-regression spawn. Long enough that intentional behavior +
# small-sample noise stops re-spawning every cooldown lapse, short enough that
# a genuine new occurrence of the same signal class is still caught within a
# week. The dispatcher silently re-arms the case to ACTIVE when this lapses.
_REGRESSION_FALSE_POSITIVE_TTL = timedelta(days=7)

# Per-cycle system-wide concurrency budget. A noisy regression cannot starve
# the rest of the pipeline by occupying every research slot.
_REGRESSION_DISPATCHER_SYSTEM_CAP = 2

# Per-cycle cap on direction-scoped SR catch-rate spawns to prevent signal storms
# when many directions regress simultaneously.
_MAX_DIRECTION_SR_SPAWNS_PER_CYCLE = 2

# Conservative floor on the 24h pipeline-success-rate sample. Below this the
# trend label is too noisy to trigger an investigation. 10 completed/failed
# tasks in 24h is the same threshold operators use to mute "low signal" pages.
_PIPELINE_SUCCESS_MIN_SAMPLE = 10

_RECURRENCE_LOOKBACK = timedelta(days=30)

# Watchdog staleness thresholds.  An ACTIVE case that hasn't been updated in
# 24h has silently stalled (investigation finished with low confidence and no
# remediation was spawned, or the close-loop step dropped it).  A MERGED case
# that hasn't been updated in 6h was not auto-closed by the recovery scan —
# typically because its signal is still tripped during the re-arm cooldown, or
# a transient evaluation error skipped it.  Both escalate to
# NEEDS_MANUAL_INTERVENTION and open an Alert so operators are notified.
_WATCHDOG_ACTIVE_STALE_THRESHOLD = timedelta(hours=24)
_WATCHDOG_MERGED_STALE_THRESHOLD = timedelta(hours=6)

# Persistent-anomaly dispatch thresholds. An anomaly must recur at least this
# many times within the recency window before an investigation is spawned — the
# same philosophy as the 5pp SR delta floor: one bad cycle is noise, repeated
# occurrences are signal.
_ANOMALY_DISPATCH_MIN_OCCURRENCES = 3
_ANOMALY_DISPATCH_RECENCY_HOURS = 48

# Fix-loop divergence, viewport-regression, and agent VM resource thresholds
# moved to dispatcher_signal_data.py.

# Fragile-recurrence constants moved to dispatcher_formatters.py.

# Recurring-class proactive meta-signal thresholds.
# A regression_class trips the signal when its fix-log history across all cases
# (within the lookback window) spans >= _RECURRING_CLASS_MIN_CYCLES fix cycles AND
# those cycles have collectively targeted >= _RECURRING_CLASS_MIN_LAYERS distinct
# canonical layers.  Catches classes that accumulate cross-layer history without
# any single signal firing all layers simultaneously.
_RECURRING_CLASS_LOOKBACK = timedelta(days=30)
_RECURRING_CLASS_MIN_CYCLES = 3
_RECURRING_CLASS_MIN_LAYERS = 2

# SR blind-spot theme occurrence floor. A blind_spot theme must appear at least
# this many times across the 14d window before it is considered a structural gap
# in the self-review prompt rather than a one-off observation from a single task.
_SR_BLIND_SPOT_THEME_FLOOR = 5

# SR false-positive signal thresholds. _SR_FP_RATE_FLOOR moved to dispatcher_formatters.py.
# The sample floor guards against noise from very small windows.
_SR_FP_MIN_SAMPLE = 5

# _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS is imported from regression_dispatcher_metrics
# above; that module is the single source of truth.

# Layer-concentration meta-signal thresholds.
# A canonical layer trips the signal when it absorbs fixes across at least
# _LAYER_CONCENTRATION_MIN_DISTINCT_CASES distinct regression cases AND the
# defenses are fragile: avg hold < _LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
# OR re-trip count >= _LAYER_CONCENTRATION_FRAGILE_RETRIPS.
_LAYER_CONCENTRATION_MIN_DISTINCT_CASES = 3
_LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS = 7.0
_LAYER_CONCENTRATION_FRAGILE_RETRIPS = 2

# Prevention-rate signal thresholds. When the 7d unweighted average prevention
# rate (defenses with complete 30d observation window) drops below this floor
# AND at least _PREVENTION_RATE_MIN_SAMPLE eligible defenses exist, an
# investigate task is spawned to diagnose why cross-layer defenses are not holding.
_PREVENTION_RATE_THRESHOLD = 0.60
_PREVENTION_RATE_MIN_SAMPLE = 5

# Cross-case error-fingerprint meta-signal thresholds.
# Trips when the SAME normalized error fingerprint surfaces across at least
# _CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES distinct ``regression_class``
# values AND aggregates at least _CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES
# entries inside the lookback window. Catches the case where one underlying
# root cause shows up under multiple surface classes (e.g. an import-ordering
# breakage that trips fix_loop_divergence:lint and sr_blind_spot:style).
_CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES = 2
_CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES = 3

# Strategic-cycle stall threshold: how many hours without a completed
# strategic_review or design_doc_update before we flag the engine as stalled.
_STRATEGIC_CYCLE_STALL_HOURS = 8

# Task types that constitute a strategic cycle completion.
_STRATEGIC_TASK_TYPES = frozenset({"strategic_review", "design_doc_update"})

# Self-watchdog alarm constants.
_DISPATCHER_STALENESS_THRESHOLD_SECONDS = 300  # 5 min; cycle interval is 60s
_DISPATCHER_ZERO_SPAWN_STREAK_K = 10  # ≈10 min of suppressed spawns
_STALENESS_ALERT_TITLE_PREFIX = "Dispatcher cycle staleness:"
_ZERO_SPAWN_ALERT_TITLE_PREFIX = "Dispatcher zero-spawn streak:"
_CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX = "Cross-layer backfill lag:"
_EVALUATOR_FAILURE_ALERT_TITLE_PREFIX = "Evaluator failure:"

_ACTIVE_INVESTIGATE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)

# Canonical layer order is owned by regression_layer_classifier.LAYER_DISPLAY_ORDER;
# this alias keeps the dispatcher's historical name available to existing
# callers (tests, admin_digest) without re-introducing a second source tuple.
_CANONICAL_RECURRENCE_LAYERS = LAYER_DISPLAY_ORDER

# Cycle records moved to durable storage: see ``DispatcherCycleRecord`` in
# ``backend/app/models.py``. ``run_once()`` inserts one row per tick; the
# admin /api/admin/regression-dispatcher/health endpoint and
# ``run_dispatcher_watchdog_alarms`` query the table.  This survives worker
# restarts and is consistent across multi-worker deployments — both blind
# spots of the prior process-local deque.

# Retention window for DispatcherCycleRecord rows. Older rows are pruned
# opportunistically inside ``run_once()`` so the table doesn't grow
# unboundedly (the prior deque was bounded at 1000 records, ~17h at a
# 60s tick).  30d roughly matches the existing admin window-hours upper
# bound (168h) plus headroom for retroactive analysis.
_DISPATCHER_CYCLE_TTL = timedelta(days=30)

# ContextVar lets run_once() accumulate rejection counts inside _maybe_spawn()
# without threading a mutable dict through every evaluate helper signature.
# Each asyncio Task gets its own copy of this value, so concurrent run_once()
# calls for different projects don't interfere.
_cycle_rejections_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_cycle_rejections",
    default=None,
)

# The earliest "signal first tripped" time is now sourced from the
# ``RegressionRemediationCase.signal_first_seen_at`` /
# ``signal_first_crossed_at`` columns — the dispatcher writes both on
# spawn and reads them on subsequent ticks.  No process-local cache:
# when no case row exists yet, the trip is by definition ``now`` (first
# observation), so persisting it across cooldown-blocked ticks would
# require a second durable write that adds no value for the first-spawn
# MTTD edge case.
#
# The "signal healthy since" recovery clock is now stored on
# ``RegressionRemediationCase.signal_healthy_since_at`` (durable, per-case)
# and read/written inside ``_auto_close_recovered_manual_intervention``.

# Per-project last cycle timestamp (monotonic). Updated in run_once finally
# block even when run_once raises. Used by the staleness watchdog alarm.
# An absent entry means no cycle has ever completed for this process boot.
_last_cycle_monotonic: dict[str, float] = {}

# ContextVar for per-cycle signal-trip count. Incremented in _maybe_spawn
# at entry, before any guard, so cooldown/dedup/cap rejections are counted.
_cycle_signals_tripped_var: contextvars.ContextVar[list] = contextvars.ContextVar(
    "_cycle_signals_tripped",
    default=None,
)

# Per-run cache for the project-wide layer-defense ledger block. Keyed by str(project_id).
# Populated by run_once() so the ledger DB query fires at most once per project per tick.
# Default None means caching is disabled (e.g. when called from run_recovery_once).
_cycle_layer_ledger_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_cycle_layer_ledger",
    default=None,
)

# Context vars for tracking recovery counts in watchdog cycles.
_recovery_counts_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "_recovery_counts",
    default=None,
)


async def _prune_old_dispatcher_cycle_records(session: AsyncSession) -> int:
    """Delete DispatcherCycleRecord rows older than the retention window.

    Called opportunistically inside ``run_once``; failures are swallowed so
    a prune error cannot mask a successful spawn commit.  Returns the count
    of rows deleted (0 when nothing matches).
    """
    from sqlalchemy import delete as sql_delete
    from app.models import DispatcherCycleRecord

    cutoff = _utcnow() - _DISPATCHER_CYCLE_TTL
    try:
        result = await session.execute(
            sql_delete(DispatcherCycleRecord).where(
                DispatcherCycleRecord.cycle_started_at < cutoff
            )
        )
        deleted = result.rowcount or 0
        if deleted:
            await session.commit()
        return int(deleted)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] cycle-record prune failed", exc_info=True
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def _last_spawn_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Task | None:
    """Return the most recent dispatcher-spawned task for this class, if any.

    Filters by ``context['spawned_by']`` and ``context['regression_class']`` in
    the WHERE clause so the cooldown guard cannot be silently bypassed by a
    busy project generating >N unrelated tasks within the cooldown window.
    """
    return (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.context["spawned_by"].as_string()
                    == "pipeline_regression_dispatcher",
                    Task.context["regression_class"].as_string() == regression_class,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _in_flight_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Task | None:
    """Return the in-flight investigation for this regression class, if any.

    Only catches dispatcher-spawned tasks: any task with
    ``context.regression_class == <class>`` in non-terminal status. The
    crash-investigation path in ``lifecycle._spawn_crash_investigations``
    does not write ``regression_class`` and is deduped separately on
    ``service_name``.
    """
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(_ACTIVE_INVESTIGATE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in rows:
        if (t.context or {}).get("regression_class") == regression_class:
            payload = t.first_payload or {}
            if payload.get("type") == "investigate":
                return t
    return None


async def _count_active_research_for_project(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Count research-shape tasks currently consuming a research slot.

    Mirrors the ``max_total_research_per_project`` accounting in
    ``autonomous_research.py``: research + investigate share the same budget so
    a regression flood cannot starve direction research.
    """
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for t in rows:
        ptype = (t.first_payload or {}).get("type")
        if ptype in ("research", "investigate"):
            n += 1
    return n


async def _format_prior_failure_context(
    case: RegressionRemediationCase,
    session: AsyncSession,
) -> str:
    """Build a 'PRIOR REMEDIATION FAILURE CONTEXT' block for fast-track spawns.

    Includes the prior remediation PR number, last_failure_class, and an
    excerpt of last_failure_summary. When last_failure_summary is null, falls
    back to the prior remediation task's latest_status_message (truncated to
    ~400 chars) so the block always carries at least one signal of what went
    wrong. The agent is told explicitly that the prior diagnosis already
    failed and to choose a different angle.
    """
    lines = [
        "",
        "PRIOR REMEDIATION FAILURE CONTEXT",
        "The prior remediation attempt FAILED (review-rejected, CI-failed, or merge-blocked) "
        "and this case is being fast-tracked back into investigation.",
    ]
    pr = case.last_remediation_pr_number
    if pr is not None:
        lines.append(
            f"Prior remediation PR: #{pr} — read its diff and review comments to "
            f"understand what the prior fix attempted."
        )
    failure_class = case.last_failure_class
    if failure_class:
        lines.append(f"Prior failure class: {failure_class}.")

    summary = case.last_failure_summary
    if not summary and case.last_remediation_task_id is not None:
        prior_task = await session.get(Task, case.last_remediation_task_id)
        if prior_task is not None and prior_task.latest_status_message:
            summary = prior_task.latest_status_message
    if summary:
        snippet = summary.strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "…"
        lines.append("Prior failure excerpt:")
        lines.append(snippet)

    lines.append(
        "The prior diagnosis already failed — do NOT repeat the same approach. "
        "Investigate the regression from a different angle: a deeper root cause, "
        "a contributing factor the prior attempt missed, or a different layer."
    )
    return "\n".join(lines)


async def _evaluate_low_prevention_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Fire a research signal when the 7d average defense prevention rate drops below threshold."""
    if spawn_budget_left <= 0:
        return 0

    cases = list(
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        ).scalars()
    )

    result = compute_avg_prevention_rate_7d(cases)
    avg_rate = result["avg_rate"]
    eligible_count = result["eligible_count"]

    if avg_rate is None or avg_rate >= _PREVENTION_RATE_THRESHOLD:
        return 0
    if eligible_count < _PREVENTION_RATE_MIN_SAMPLE:
        return 0

    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="low_prevention_rate_7d",
        regression_class="cross_layer_defense_prevention_low",
        prior_rate=_PREVENTION_RATE_THRESHOLD,
        current_rate=avg_rate,
        sample_size=eligible_count,
        magnitude=_PREVENTION_RATE_THRESHOLD - avg_rate,
        title_hint=f"Low cross-layer defense prevention rate ({avg_rate:.0%})",
        extra_context_fields={
            "prevention_rate_7d": avg_rate,
            "eligible_defenses": eligible_count,
            "recurred_count": result["recurred_count"],
        },
    )
    return 1 if spawn is not None else 0


# ---------------------------------------------------------------------------
# Path-based layer classification — moved to regression_layer_backfill.
# Re-exported via the top-of-file import block; new code should import
# directly from app.regression_layer_backfill.
# ---------------------------------------------------------------------------


async def _get_remediation_case(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> RegressionRemediationCase | None:
    return (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.regression_class == regression_class,
                )
            )
        )
        .scalars()
        .first()
    )


async def _load_recurrence_prompt_context(
    session: AsyncSession,
    case: RegressionRemediationCase,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """Return recurrence metadata when a case clearly re-tripped."""
    cycles = _extract_recurrence_cycles(case)

    recent_closed = False
    if case.status == RegressionCaseStatus.CLOSED:
        closed_anchor = case.updated_at or case.last_seen_at or case.created_at
        recent_closed = now - closed_anchor <= _RECURRENCE_LOOKBACK

    rearmed_after_merge = (
        case.status == RegressionCaseStatus.ACTIVE
        and (case.attempts or 0) >= 1
        and case.last_remediation_result == "failed"
    )

    recurrence_from_history = bool(
        case.fingerprint_to_fix_log
        and any(
            isinstance(entry, dict)
            and (
                entry.get("fix_pr_number") is not None
                or entry.get("recovered_at") is not None
            )
            for entry in case.fingerprint_to_fix_log
        )
    )

    if not (recent_closed or rearmed_after_merge or recurrence_from_history):
        return None

    if not cycles:
        snapshot = case.signal_snapshot_at_merge or {}
        merged_anchor = _parse_dispatcher_dt(snapshot.get("captured_at"))
        cycles = [
            {
                "fix_pr_number": case.last_remediation_pr_number,
                "merged_at": merged_anchor or case.updated_at or case.created_at,
                "recovered_at": None,
                "layers_targeted_present": False,
                "layers_targeted": [],
            }
        ]

    task_rows = (
        await session.execute(
            select(Task.id, TaskHistory.timestamp)
            .join(TaskHistory, TaskHistory.task_id == Task.id)
            .where(
                Task.project_id == case.project_id,
                Task.context["spawned_by"].as_string()
                == "pipeline_regression_dispatcher",
                Task.context["regression_class"].as_string() == case.regression_class,
                TaskHistory.action == "SPAWNED",
            )
            .order_by(TaskHistory.timestamp.asc())
        )
    ).all()

    attempts: list[dict[str, Any]] = []
    for idx, row in enumerate(task_rows):
        task_id, _spawned_at = row
        cycle = cycles[idx] if idx < len(cycles) else {}
        merged_at = cycle.get("merged_at")
        recovered_at = cycle.get("recovered_at")
        if recovered_at is not None:
            held_for = now - recovered_at
        elif merged_at is not None:
            held_for = now - merged_at
        else:
            held_for = None
        attempts.append(
            {
                "task_id": str(task_id),
                "pr_number": cycle.get("fix_pr_number")
                or case.last_remediation_pr_number,
                "merged_at": merged_at.isoformat() if merged_at is not None else None,
                "recovered_at": (
                    recovered_at.isoformat() if recovered_at is not None else None
                ),
                "held_for": _format_dispatcher_duration(held_for),
                "layers_targeted_present": bool(cycle.get("layers_targeted_present")),
                "layers_targeted": list(cycle.get("layers_targeted") or []),
            }
        )

    recurrence_count = len(cycles)
    held_text = attempts[-1]["held_for"] if attempts else "unknown"
    return {
        "recurrence_count": recurrence_count,
        "attempts": attempts,
        "held_text": held_text,
    }


async def _maybe_spawn(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    signal_name: str,
    regression_class: str,
    prior_rate: float | None,
    current_rate: float | None,
    sample_size: int,
    magnitude: float | None,
    extras: list[str] | None = None,
    title_hint: str,
    extra_snapshot_fields: dict | None = None,
    prompt_override: str | None = None,
    extra_context_fields: dict | None = None,
    signal_first_seen_at: datetime | None = None,
) -> Task | None:
    """Apply dedup + cooldown gates and spawn an investigate task on green light."""
    logger.info(
        "[REGRESSION_DISPATCHER] signal-tripped project=%s class=%s prior=%s "
        "current=%s sample=%d",
        project_id,
        regression_class,
        prior_rate,
        current_rate,
        sample_size,
    )
    # Count every _maybe_spawn entry as a tripped signal (before guard gates).
    _st = _cycle_signals_tripped_var.get()
    if _st is not None:
        _st[0] += 1
    _rj = _cycle_rejections_var.get()

    in_flight = await _in_flight_for_class(session, project_id, regression_class)
    if in_flight is not None:
        if _rj is not None:
            _rj["dedup"] = _rj.get("dedup", 0) + 1
        logger.info(
            "[REGRESSION_DISPATCHER] dedup-hit project=%s class=%s in_flight_task=%s",
            project_id,
            regression_class,
            in_flight.id,
        )
        return None

    existing_case = await _get_remediation_case(session, project_id, regression_class)
    # Compute ``now`` up-front so the FALSE_POSITIVE check below has access
    # to it; the original code only assigned ``now`` after the cooldown
    # gate. Subsequent gates reuse the same value.
    now = _utcnow()
    if (
        existing_case is not None
        and existing_case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    ):
        if _rj is not None:
            _rj["manual_intervention_suppressed"] = (
                _rj.get("manual_intervention_suppressed", 0) + 1
            )
        logger.info(
            "[REGRESSION_DISPATCHER] manual-intervention-suppressed project=%s "
            "class=%s case_id=%s",
            project_id,
            regression_class,
            existing_case.id,
        )
        return None
    # FALSE_POSITIVE: investigation already concluded the signal does not
    # represent a real regression. Silently suppress until the TTL lapses,
    # then re-arm to ACTIVE so the next genuine occurrence of the same
    # signal class can still be investigated.
    if (
        existing_case is not None
        and existing_case.status == RegressionCaseStatus.FALSE_POSITIVE
    ):
        suppressed_until = existing_case.false_positive_until_at
        if suppressed_until is not None and now < suppressed_until:
            if _rj is not None:
                _rj["false_positive_suppressed"] = (
                    _rj.get("false_positive_suppressed", 0) + 1
                )
            logger.info(
                "[REGRESSION_DISPATCHER] false-positive-suppressed project=%s "
                "class=%s case_id=%s suppressed_until=%s",
                project_id,
                regression_class,
                existing_case.id,
                suppressed_until.isoformat(),
            )
            return None
        # TTL lapsed (or was never stamped) — re-arm so the existing-case
        # branch below treats this as a normal re-spawn. The case row is
        # mutated in-place; the spawn helper will reset signal_first_crossed_at
        # for a CLOSED-style fresh-cycle. We mirror CLOSED here so MTTD
        # reflects the new episode rather than the original false-positive
        # detection time.
        existing_case.status = RegressionCaseStatus.CLOSED
        existing_case.false_positive_until_at = None
        existing_case.signal_first_crossed_at = None
        session.add(existing_case)
        logger.info(
            "[REGRESSION_DISPATCHER] false-positive-rearm project=%s class=%s "
            "case_id=%s",
            project_id,
            regression_class,
            existing_case.id,
        )
    if (
        existing_case is not None
        and existing_case.status == RegressionCaseStatus.MERGED
    ):
        # Recovery scan owns MERGED cases — let it decide CLOSED vs re_arm()
        # before any new investigation can fire, otherwise a same-class
        # spawn within the dispatcher cooldown window would race the
        # recovery loop this PR adds.
        logger.info(
            "[REGRESSION_DISPATCHER] merged-awaiting-recovery-scan project=%s "
            "class=%s case_id=%s",
            project_id,
            regression_class,
            existing_case.id,
        )
        return None

    last_spawn = await _last_spawn_for_class(session, project_id, regression_class)
    fast_tracking_failed_remediation = (
        existing_case is not None
        and bool(getattr(existing_case, "fast_track_next_spawn", False))
        and (existing_case.attempts or 0) > 0
    )
    if last_spawn is not None and last_spawn.created_at:
        last_at = last_spawn.created_at
        if last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
        if now < last_at + _REGRESSION_COOLDOWN:
            if fast_tracking_failed_remediation:
                logger.info(
                    "[REGRESSION_DISPATCHER] fast-track-failed-remediation "
                    "project=%s class=%s case_id=%s attempts=%s last_spawn=%s "
                    "cooldown_until=%s",
                    project_id,
                    regression_class,
                    existing_case.id,
                    existing_case.attempts,
                    last_at.isoformat(),
                    (last_at + _REGRESSION_COOLDOWN).isoformat(),
                )
            else:
                if _rj is not None:
                    _rj["cooldown"] = _rj.get("cooldown", 0) + 1
                logger.info(
                    "[REGRESSION_DISPATCHER] cooldown-hit project=%s class=%s "
                    "last_spawn=%s cooldown_until=%s",
                    project_id,
                    regression_class,
                    last_at.isoformat(),
                    (last_at + _REGRESSION_COOLDOWN).isoformat(),
                )
                return None

    # Derive signal_first_seen_at for MTTD across all non-anomaly classes.
    # Anomaly class passes signal_first_seen_at explicitly from entry["first_seen"].
    # The case row's signal_first_seen_at / signal_first_crossed_at columns are
    # the durable source of truth across cooldown-blocked ticks and worker
    # restarts.  When no case exists yet (first observation), use ``now`` —
    # the trip is by definition right now from this dispatcher's perspective.
    if signal_first_seen_at is None:
        if existing_case is not None and (
            existing_case.signal_first_seen_at is not None
            or existing_case.signal_first_crossed_at is not None
        ):
            signal_first_seen_at = (
                existing_case.signal_first_seen_at
                or existing_case.signal_first_crossed_at
            )
        else:
            signal_first_seen_at = now

    # ---- Cross-layer regression accumulation ----------------------------------
    # Classify this observation into layer tags and merge into the case's
    # symptoms_by_layer log. Cross-layer escalation fires when the merged log
    # contains observations on >=2 distinct layers within the 48h window. The
    # regression_class itself is unchanged so the existing 6h cooldown still
    # applies and a cross-layer escalation occupies exactly one spawn slot.
    from app.regression_layer_classifier import classify_layers

    obs_failure_class, obs_message, obs_stage = _derive_observation_for_layer_classify(
        signal_name=signal_name,
        regression_class=regression_class,
        extras=extras,
        extra_snapshot_fields=extra_snapshot_fields,
    )
    obs_layers = classify_layers(obs_failure_class, obs_message, obs_stage or None)
    obs_fingerprint = ""
    if obs_message:
        try:
            from app.anomaly_detection import _error_fingerprint

            obs_fingerprint = _error_fingerprint(obs_message, obs_stage or None)
        except Exception:  # noqa: BLE001
            obs_fingerprint = ""
    obs_sample = obs_message[:500] if obs_message else (extras[0] if extras else "")

    prior_symptoms = (
        existing_case.symptoms_by_layer if existing_case is not None else {}
    ) or {}
    merged_symptoms = (
        _record_layer_observation(
            prior_symptoms,
            obs_layers,
            fingerprint=obs_fingerprint or regression_class,
            sample=obs_sample,
            now=now,
        )
        if obs_layers
        else dict(prior_symptoms)
    )
    distinct_layers = _distinct_recent_layers(merged_symptoms, now=now)
    is_cross_layer = len(distinct_layers) >= 2

    cooldown_until = (now + _REGRESSION_COOLDOWN).isoformat()
    base_prompt = (
        prompt_override
        if prompt_override is not None
        else _format_signal_lines(
            signal_name=signal_name,
            regression_class=regression_class,
            prior_rate=prior_rate,
            current_rate=current_rate,
            sample_size=sample_size,
            extras=extras,
        )
    )
    # Compute (or retrieve from per-run cache) the project-wide layer-defense ledger.
    # The cache is keyed by str(project_id) inside the ContextVar dict set by run_once();
    # a None dict means caching is disabled (e.g. run_recovery_once), so we compute
    # fresh but still use the result.
    _ledger_cache = _cycle_layer_ledger_var.get()
    _pid_key = str(project_id)
    if _ledger_cache is not None and _pid_key in _ledger_cache:
        _layer_defense_block = _ledger_cache[_pid_key]
    else:
        _all_cases = (
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
        _all_fix_logs: list[dict[str, Any]] = []
        for _c in _all_cases:
            if _c.fingerprint_to_fix_log:
                _all_fix_logs.extend(_c.fingerprint_to_fix_log)
        _layer_defense_block = _build_layer_defense_ledger(_all_fix_logs)
        if _ledger_cache is not None:
            _ledger_cache[_pid_key] = _layer_defense_block

    if is_cross_layer:
        layers_evidence = _representative_per_layer(
            merged_symptoms, distinct_layers, now=now
        )
        prompt = _format_cross_layer_prompt(
            signal_name=signal_name,
            regression_class=regression_class,
            layers_evidence=layers_evidence,
            base_lines=base_prompt,
            layer_defense_block=_layer_defense_block,
        )
    else:
        prompt = base_prompt
        layers_evidence = []

    recurrence_context = None
    _fragile_recurrence = False
    if existing_case is not None:
        recurrence_context = await _load_recurrence_prompt_context(
            session,
            existing_case,
            now=now,
        )
        if recurrence_context is not None:
            object.__setattr__(
                existing_case,
                "_recurrence_attempts",
                recurrence_context["attempts"],
            )
            object.__setattr__(
                existing_case,
                "_recurrence_count",
                recurrence_context["recurrence_count"],
            )
            object.__setattr__(
                existing_case,
                "_recurrence_held_text",
                recurrence_context["held_text"],
            )
            object.__setattr__(
                existing_case,
                "_recurrence_obs_layers",
                list(obs_layers or []),
            )
            # Upgrade to fragile-recurrence meta when the case is chronically fragile
            # and no higher-priority meta-signal (layer_concentration, cross_case_fingerprint,
            # cross_layer) is already handling it. Guard against re-escalating within the
            # same ACTIVE cycle.
            _fragile_recurrence = (
                signal_name not in ("layer_concentration", "cross_case_fingerprint")
                and not is_cross_layer
                and existing_case.escalation_reason
                != ESCALATION_REASON_FRAGILE_RECURRENCE_META
                and _is_fragile_recurrence(
                    existing_case,
                    recurrence_context["recurrence_count"],
                    now,
                )
            )
            if _fragile_recurrence:
                prompt = _format_fragile_recurrence_meta_prompt(
                    existing_case, base_prompt, layer_defense_block=_layer_defense_block
                )
            elif is_cross_layer:
                prompt = _format_recurrence_meta_prompt(
                    existing_case, prompt, layer_defense_block=_layer_defense_block
                )
            else:
                prompt = _format_recurrence_meta_prompt(
                    existing_case, base_prompt, layer_defense_block=_layer_defense_block
                )

    # SR blind-spot theme regressions already represent 5+ recurrences of the same
    # pattern; force the layer-enumeration meta block on every first dispatch so the
    # agent enumerates all six canonical layers rather than doing single-symptom triage.
    # Recurrence meta (above) already includes layer enumeration; cross-layer and
    # recurrence may both fire together; this only fires when neither path ran.
    if (
        not is_cross_layer
        and recurrence_context is None
        and regression_class.startswith("sr_blind_spot:theme:")
    ):
        prompt = _format_theme_layer_meta_prompt(
            base_prompt, layer_defense_block=_layer_defense_block
        )

    if (
        recurrence_context is None
        and existing_case is not None
        and (existing_case.attempts or 0) >= 2
    ):
        prompt += _format_prior_attempt_context(
            existing_case, omit_pr_line=fast_tracking_failed_remediation
        )
    if fast_tracking_failed_remediation:
        prompt += await _format_prior_failure_context(existing_case, session)
    if existing_case is not None and (
        existing_case.false_positive_reason or existing_case.last_unactionable_verdict
    ):
        prompt += _format_prior_verdict_context(existing_case)

    # Determine case-level escalation reason for operator visibility.
    if signal_name == "layer_concentration":
        case_escalation_reason: str | None = (
            ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION
        )
    elif signal_name == "cross_case_fingerprint":
        case_escalation_reason = ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT
    elif signal_name == "recurring_class":
        # Proactive recurring-class meta-spawns get their own audit-trail reason
        # so operators can distinguish them from generic cross-layer escalations.
        case_escalation_reason = ESCALATION_REASON_RECURRING_CLASS_META
    elif is_cross_layer:
        case_escalation_reason = ESCALATION_REASON_ESCALATED_CROSS_LAYER
    elif _fragile_recurrence:
        case_escalation_reason = ESCALATION_REASON_FRAGILE_RECURRENCE_META
    elif recurrence_context is not None:
        case_escalation_reason = ESCALATION_REASON_ESCALATED_RECURRENCE
    else:
        case_escalation_reason = None

    from app.models import Project as _SpawnProject  # noqa: PLC0415

    _spawn_project = await session.get(_SpawnProject, project_id)
    payload_dict: dict[str, Any] = {
        "type": "investigate",
        "prompt": prompt,
        "base_branch": _get_project_integration_branch(_spawn_project),
        "effort": "low",
    }
    if is_cross_layer:
        payload_dict["layer_context"] = {
            "layers": layers_evidence,
            "escalation": "cross_layer",
        }
    payload = validate_internal_payload(
        payload_dict,
        "pipeline_regression_dispatcher:investigate",
    )
    context: dict[str, Any] = {
        "spawned_by": "pipeline_regression_dispatcher",
        "regression_class": regression_class,
        "signal_name": signal_name,
        "magnitude": magnitude,
        "prior_rate": prior_rate,
        "current_rate": current_rate,
        "sample_size": sample_size,
        "dispatcher_cooldown_until_iso": cooldown_until,
    }
    if is_cross_layer:
        context["cross_layer"] = True
        context["escalation"] = "cross_layer"
        context["escalation_reason"] = "cross_layer"
        context["layer_context"] = {
            "layers": layers_evidence,
            "escalation": "cross_layer",
        }
        context["cross_layer_layers"] = list(distinct_layers)
    if recurrence_context is not None:
        context["recurrence_count"] = recurrence_context["recurrence_count"]
    if extra_context_fields:
        context.update(extra_context_fields)
    new_task = Task(
        project_id=project_id,
        title=f"Investigate pipeline regression: {title_hint}"[:200],
        description=(
            f"Auto-spawned from pipeline-regression dispatcher ({signal_name})"
        )[:500],
        payloads=[payload],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.flush()
    _history_details: dict[str, Any] = {
        "spawned_by": "PIPELINE_REGRESSION_DISPATCHER",
        "regression_class": regression_class,
        "signal_name": signal_name,
    }
    if case_escalation_reason is not None:
        _history_details["escalation_reason"] = case_escalation_reason
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details=_history_details,
        )
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.INFO,
            title=f"Pipeline regression investigation spawned: {regression_class}"[
                :200
            ],
            message=(
                f"Signal {signal_name} tripped (prior={prior_rate}, "
                f"current={current_rate}, n={sample_size}). Investigation "
                f"task {new_task.id} queued."
            )[:1000],
        )
    )

    signal_snapshot: dict[str, Any] = {
        "prior_rate": prior_rate,
        "current_rate": current_rate,
        "sample_size": sample_size,
        "signal_name": signal_name,
        "magnitude": magnitude,
        "captured_at": now.isoformat(),
    }
    if extra_snapshot_fields:
        signal_snapshot.update(extra_snapshot_fields)
    if existing_case is None:
        session.add(
            RegressionRemediationCase(
                project_id=project_id,
                regression_class=regression_class,
                status=RegressionCaseStatus.ACTIVE,
                signal_snapshot_at_spawn=signal_snapshot,
                last_remediation_task_id=new_task.id,
                last_seen_at=now,
                hit_count=1,
                signal_first_crossed_at=now,
                signal_first_seen_at=signal_first_seen_at or now,
                symptoms_by_layer=merged_symptoms,
                investigate_spawned_at=now,
                escalation_reason=case_escalation_reason,
            )
        )
    else:
        existing_case.signal_snapshot_at_spawn = signal_snapshot
        existing_case.hit_count = (existing_case.hit_count or 0) + 1
        existing_case.last_seen_at = now
        existing_case.last_remediation_task_id = new_task.id
        existing_case.symptoms_by_layer = merged_symptoms
        if case_escalation_reason is not None:
            existing_case.escalation_reason = case_escalation_reason
        # A CLOSED case re-tripping starts a new regression cycle — reset
        # signal_first_crossed_at so time-to-detect reflects the new episode.
        # For still-ACTIVE cases, preserve the original crossing time.
        if (
            existing_case.status == RegressionCaseStatus.CLOSED
            or existing_case.signal_first_crossed_at is None
        ):
            existing_case.signal_first_crossed_at = now
        # Propagate the earliest-tracked trip time to the case row so MTTD
        # reflects the true detection latency on re-spawn. The case row's
        # ``signal_first_seen_at`` is itself the durable source of truth
        # across cooldown-blocked ticks and worker restarts.
        if signal_first_seen_at is not None:
            existing_case.signal_first_seen_at = signal_first_seen_at
        # Preserve the first investigate spawn timestamp — operators want the
        # original detection-to-action latency, not the most recent re-spawn.
        if existing_case.investigate_spawned_at is None:
            existing_case.investigate_spawned_at = now
        # Reactivate so admin state matches the in-flight remediation. A
        # CLOSED case re-tripping is a fresh regression cycle; without this
        # the case would stay CLOSED while a new investigation runs.
        existing_case.status = RegressionCaseStatus.ACTIVE
        # One-shot: clear the failure-driven fast-track flag now that the
        # bypass has been honored and a new investigate task is in flight.
        # A subsequent failure on this new task will set it again via
        # re_arm()'s default fast_track=True.
        if existing_case.fast_track_next_spawn:
            existing_case.fast_track_next_spawn = False
        session.add(existing_case)

    mttd_seconds = (
        round((now - signal_first_seen_at).total_seconds())
        if signal_first_seen_at
        else None
    )
    logger.info(
        "[REGRESSION_DISPATCHER] spawn-success project=%s class=%s task_id=%s mttd_seconds=%s cross_layer=%s",
        project_id,
        regression_class,
        new_task.id,
        mttd_seconds,
        is_cross_layer,
    )
    return new_task


async def _evaluate_sr_catch_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped SR catch-rate categories and coalesce into one spawn."""
    if spawn_budget_left <= 0:
        return 0
    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    tripped: list[dict] = []
    for cat in sorted(categories):
        if not isinstance(categories[cat], dict):
            continue
        rc = f"sr_catch_rate:{cat}"
        state = await _current_signal_value_for_class(
            session, project_id, rc, project_design_doc
        )
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    cats_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = (
        _format_bundle_signal_lines(tripped)
        if len(tripped) > 1
        else _format_signal_lines(
            signal_name=rep["signal_name"],
            regression_class=rep["regression_class"],
            prior_rate=rep["prior_rate"],
            current_rate=rep["current_rate"],
            sample_size=rep["sample_size"],
            extras=rep["extras"],
        )
    )
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name=rep["signal_name"],
        regression_class="sr_catch_rate",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"SR catch-rate ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_false_positive_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when any SR category's false-positive rate exceeds the floor in both windows."""
    if spawn_budget_left <= 0:
        return 0
    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    tripped: list[dict] = []
    for cat in sorted(categories):
        if not isinstance(categories[cat], dict):
            continue
        rc = f"sr_false_positive:{cat}"
        state = await _current_signal_value_for_class(
            session, project_id, rc, project_design_doc
        )
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    cats_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = _format_sr_fp_signal_lines(
        regression_class=rep["regression_class"],
        current_fp_rate=rep["current_rate"],
        prior_fp_rate=rep["prior_rate"],
        sample_size=rep["sample_size"],
    )
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="sr_false_positive",
        regression_class="sr_false_positive",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"SR false-positive ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_catch_rate_direction_signals(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Spawn per-direction investigations when a direction regresses but project-wide is healthy."""
    if spawn_budget_left <= 0:
        return 0
    by_direction = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "by_direction"
    ) or {}
    if not isinstance(by_direction, dict):
        return 0
    _spawned = 0
    dir_spawns = 0
    for direction_id in sorted(by_direction):
        if (
            _spawned >= spawn_budget_left
            or dir_spawns >= _MAX_DIRECTION_SR_SPAWNS_PER_CYCLE
        ):
            break
        dir_cats = sorted(
            (by_direction[direction_id].get("categories") or {}).items(),
            key=lambda kv: kv[1].get("miss_rate", 0),
            reverse=True,
        )
        for cat, _ in dir_cats:
            rc = f"sr_catch_rate:{cat}:{direction_id}"
            state = await _current_signal_value_for_class(
                session, project_id, rc, project_design_doc
            )
            if state is None or not state["tripped"]:
                continue
            # Skip if project-wide aggregate for this category is also tripping.
            pw_state = await _current_signal_value_for_class(
                session, project_id, f"sr_catch_rate:{cat}", project_design_doc
            )
            if pw_state is not None and pw_state["tripped"]:
                continue
            prompt = _format_signal_lines(
                signal_name=state["signal_name"],
                regression_class=rc,
                prior_rate=state["prior_rate"],
                current_rate=state["current_rate"],
                sample_size=state["sample_size"],
                extras=state["extras"],
            )
            spawn = await _maybe_spawn(
                session,
                project_id,
                signal_name=state["signal_name"],
                regression_class=rc,
                prior_rate=state["prior_rate"],
                current_rate=state["current_rate"],
                sample_size=state["sample_size"],
                magnitude=state["magnitude"],
                title_hint=f"SR catch-rate ({cat}) direction {direction_id[:8]}",
                prompt_override=prompt,
                extra_context_fields={"direction_id": direction_id},
            )
            if spawn is not None:
                _spawned += 1
                dir_spawns += 1
            break  # one attempt per direction (worst tripped category first)
    return _spawned


async def _evaluate_sr_below_floor_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when any SR category sits below the floor for min-windows consecutively."""
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import (
        _SR_FLOOR_THRESHOLD,
        _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    )

    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    floor_threshold = float(
        (project_design_doc or {}).get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD
    )
    floor_min_windows = int(
        (project_design_doc or {}).get("sr_floor_min_windows")
        or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )

    tripped: list[tuple[str, dict]] = []
    for cat, data in sorted(categories.items()):
        if not isinstance(data, dict):
            continue
        if not data.get("below_floor"):
            continue
        rate = data.get("current_hit_rate")
        history = data.get("rate_history") or []
        if rate is None or len(history) < floor_min_windows:
            continue
        tripped.append((cat, data))

    if not tripped:
        return 0

    cats_label = ", ".join(cat for cat, _ in tripped)
    lines = [
        "Pipeline quality signal: SR catch-rate sustained below floor "
        "(sub-signal=below_floor / class=sr_catch_rate_below_floor).",
        f"Categories stuck below {floor_threshold:.0%} floor for "
        f"{floor_min_windows}+ consecutive windows: {cats_label}.",
        "",
        "Sub-signal: below_floor — the hit-rate is chronically low (not just declining).",
        "The category has been stuck at a low catch-rate without getting worse, so the "
        "delta-trend signal has not fired. Investigate why the baseline is low and what "
        "structural changes to the self-review prompt or process could lift it.",
        "",
    ]
    for cat, data in tripped:
        rate = data.get("current_hit_rate")
        history = data.get("rate_history") or []
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        hist_str = ", ".join(f"{r:.0%}" for r in history[-floor_min_windows:])
        lines.append(
            f"  • {cat}: current={rate_str}, last {floor_min_windows} windows: [{hist_str}]"
        )
    lines.append("")
    lines.append(
        "FIRST: triage whether this signal actually represents a regression. "
        "Set `verdict` to 'not_a_regression' when the low rate is intentional or "
        "the sample is too small. Otherwise produce the structured diagnosis JSON."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = tripped[0]
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="sr_catch_rate_below_floor",
        regression_class="sr_catch_rate_below_floor",
        prior_rate=floor_threshold,
        current_rate=rep_data.get("current_hit_rate"),
        sample_size=int(rep_data.get("current_human_count") or 0),
        magnitude=round(floor_threshold - (rep_data.get("current_hit_rate") or 0), 3),
        title_hint=f"SR catch-rate below floor ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_pass_engagement_thinning_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when PASS reasoning engagement drops below threshold for a category."""
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import (
        _SR_ENGAGEMENT_THRESHOLD,
        _SR_ENGAGEMENT_MIN_PASSES,
    )

    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    eng_threshold = float(
        (project_design_doc or {}).get("sr_engagement_threshold")
        or _SR_ENGAGEMENT_THRESHOLD
    )
    eng_min_passes = int(
        (project_design_doc or {}).get("sr_engagement_min_passes")
        or _SR_ENGAGEMENT_MIN_PASSES
    )

    thinning: list[tuple[str, dict]] = []
    for cat, data in sorted(categories.items()):
        if not isinstance(data, dict):
            continue
        rate = data.get("reasoning_engagement_rate")
        total = data.get("reasoning_engagement_total") or 0
        if rate is None or total < eng_min_passes:
            continue
        if rate < eng_threshold:
            thinning.append((cat, data))

    if not thinning:
        return 0

    cats_label = ", ".join(cat for cat, _ in thinning)
    lines = [
        "Pipeline quality signal: SR PASS reasoning engagement thinning "
        "(sub-signal=pass_engagement_thinning / class=sr_pass_engagement_thinning).",
        f"Categories with PASS engagement below {eng_threshold:.0%}: {cats_label}.",
        "",
        "Sub-signal: pass_engagement_thinning — the agent is rubber-stamping PASS "
        "verdicts without substantive reasoning. This is an early indicator that "
        "precedes catch-rate regressions.",
        "",
    ]
    for cat, data in thinning:
        rate = data.get("reasoning_engagement_rate")
        total = data.get("reasoning_engagement_total") or 0
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        lines.append(f"  • {cat}: engagement={rate_str} (n={total} PASS verdicts)")
    lines.append("")
    lines.append(
        "Investigate why PASS reasoning is thin for these categories. "
        "Check the self-review prompt wording — does it make it too easy to "
        "skip substantive reasoning for PASS verdicts? Produce the structured "
        "diagnosis JSON ({summary, root_cause, recommended_fix, severity, "
        "confidence, affected_files, verdict, verdict_reason})."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = thinning[0]
    rep_rate = rep_data.get("reasoning_engagement_rate") or 0.0
    rep_total = int(rep_data.get("reasoning_engagement_total") or 0)
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="sr_pass_engagement_thinning",
        regression_class="sr_pass_engagement_thinning",
        prior_rate=eng_threshold,
        current_rate=rep_rate,
        sample_size=rep_total,
        magnitude=round(eng_threshold - rep_rate, 3),
        title_hint=f"SR PASS engagement thinning ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in thinning]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_false_attestation_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when SR false-attestation rate exceeds threshold for any category."""
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import _FALSE_ATTESTATION_RATE_THRESHOLD

    per_category = (
        (project_design_doc or {}).get("sr_false_attestation_trends") or {}
    ).get("per_category") or {}
    if not isinstance(per_category, dict):
        return 0

    elevated: list[tuple[str, dict]] = []
    for cat, data in sorted(per_category.items()):
        if not isinstance(data, dict):
            continue
        rate = data.get("false_attestation_rate")
        if rate is not None and rate > _FALSE_ATTESTATION_RATE_THRESHOLD:
            elevated.append((cat, data))

    if not elevated:
        return 0

    cats_label = ", ".join(cat for cat, _ in elevated)
    lines = [
        "Pipeline quality signal: SR false attestation elevated "
        "(sub-signal=sr_false_attestation / class=sr_false_attestation).",
        f"Categories where SR claimed passed but CI recorded failure: {cats_label}.",
        "",
        "Sub-signal: sr_false_attestation — SR claimed tests_executed_cleanly, "
        "lint_executed_cleanly, or typecheck_executed_cleanly passed but CI "
        "recorded ci_passed=False for the same task. This indicates the agent "
        "is attesting to clean execution without actually running the checks.",
        "",
    ]
    for cat, data in elevated:
        rate = data.get("false_attestation_rate")
        n = data.get("false_attested", 0)
        total = (data.get("ci_confirmed") or 0) + n
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        prev_rate = data.get("previous_false_attestation_rate")
        prev_str = f"{prev_rate:.0%}" if prev_rate is not None else "N/A"
        lines.append(
            f"  • {cat}: false_attestation_rate={rate_str} ({n}/{total}), "
            f"prior window={prev_str}"
        )
    lines.append("")
    lines.append(
        "Investigate why the agent is attesting to clean execution without "
        "running the checks. Check whether the self-review prompt adequately "
        "enforces that execution evidence must come from an actual run on the "
        "current VM. Produce the structured diagnosis JSON ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files, verdict, verdict_reason})."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = elevated[0]
    rep_rate = rep_data.get("false_attestation_rate") or 0.0
    prev_rate = rep_data.get("previous_false_attestation_rate") or 0.0
    rep_total = int(
        (rep_data.get("false_attested") or 0) + (rep_data.get("ci_confirmed") or 0)
    )
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="sr_false_attestation",
        regression_class="sr_false_attestation",
        prior_rate=prev_rate,
        current_rate=rep_rate,
        sample_size=rep_total,
        magnitude=round(rep_rate - prev_rate, 3),
        title_hint=f"SR false attestation elevated ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in elevated]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_fix_loop_divergence_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped fix-loop divergence loops and coalesce into one spawn."""
    if spawn_budget_left <= 0:
        return 0

    tripped: list[dict] = []
    for loop in ("lint", "test", "check"):
        rc = f"fix_loop_divergence:{loop}"
        state = await _current_signal_value_for_class(session, project_id, rc)
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    loops_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = (
        _format_bundle_signal_lines(tripped)
        if len(tripped) > 1
        else _format_signal_lines(
            signal_name=rep["signal_name"],
            regression_class=rep["regression_class"],
            prior_rate=rep["prior_rate"],
            current_rate=rep["current_rate"],
            sample_size=rep["sample_size"],
            extras=rep["extras"],
        )
    )
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name=rep["signal_name"],
        regression_class="fix_loop_divergence",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"fix-loop divergence ({loops_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_fix_loop_divergence_fingerprint_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    if spawn_budget_left <= 0 or not _runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    ):
        return 0
    trips = await _collect_fix_loop_divergence_fingerprints(session, project_id)
    spawned = 0
    for trip in trips:
        if spawned >= spawn_budget_left:
            break
        prompt = _format_signal_lines(
            signal_name="fix_loop_divergence_fingerprint",
            regression_class=f"fix_loop_divergence_fingerprint:{trip['short_fingerprint']}",
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            extras=[
                f"Normalized fingerprint: {trip['fingerprint']}",
                f"Affected scope(s): {', '.join(trip['scopes']) or 'unknown'}",
                f"Affected files: {', '.join(trip['files']) or '(none recorded)'}",
                *[f"Sample failure tail: {tail}" for tail in trip["samples"]],
            ],
        )
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="fix_loop_divergence_fingerprint",
            regression_class=f"fix_loop_divergence_fingerprint:{trip['short_fingerprint']}",
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            magnitude=float(trip["count"]),
            title_hint=f"fix-loop divergence fingerprint ({trip['short_fingerprint']})"[
                :80
            ],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_viewport_regression_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip viewport_regression:<Component>:<AssertionType>@<width> when ≥2 viewport-test failures recur."""
    if spawn_budget_left <= 0:
        return 0
    trips = await _collect_viewport_failures(session, project_id)
    spawned = 0
    for trip in trips:
        if spawned >= spawn_budget_left:
            break
        component = trip["component"]
        width = trip["width"]
        failure_type = trip.get("failure_type", "unknown")
        regression_class = f"viewport_regression:{component}:{failure_type}@{width}"
        prompt = _format_signal_lines(
            signal_name="viewport_regression",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            extras=[
                f"Component: {component}",
                f"Viewport width: {width}px",
                f"Failure type: {failure_type}",
                *[f"Sample failure: {s}" for s in trip["samples"]],
            ],
        )
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="viewport_regression",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            magnitude=float(trip["count"]),
            title_hint=f"viewport regression: {component} at {width}px"[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1
            if hasattr(RegressionRemediationCase, "introducing_pr_number"):
                case = await _get_remediation_case(
                    session, project_id, regression_class
                )
                if case is not None:
                    case.introducing_pr_number = trip.get("introducing_pr_number")
                    case.introducing_commit_sha = trip.get("introducing_commit_sha")
                    session.add(case)
    return spawned


def _probe_viewport_coverage_drop(project_design_doc: dict | None) -> bool:
    """Side-effect-free probe: would viewport_coverage_drop trip right now?"""
    doc = project_design_doc or {}
    snap = doc.get("viewport_coverage_snapshot")
    if not isinstance(snap, dict):
        return False
    before = snap.get("assertion_count_before")
    after = snap.get("assertion_count_after")
    if before is None or after is None:
        return False
    removed = snap.get("removed_component_files") or []
    if removed:
        return False
    escape_hatches = int(snap.get("new_escape_hatch_count") or 0)
    return int(after) < int(before) or escape_hatches > 0


async def _evaluate_viewport_coverage_drop_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip viewport_coverage_drop when a merged PR silences viewport assertions.

    Reads the pre-computed snapshot from project.design_doc[
    "viewport_coverage_snapshot"] and fires when:
      - post-merge assertion count is lower than pre-merge count (test deleted /
        skipped), OR
      - new data-viewport-allow-overflow / data-viewport-allow-small-target
        escape-hatch attributes were added (bypass without restoring the test),
    AND no component file was legitimately removed in the same PR.
    """
    if spawn_budget_left <= 0:
        return 0
    if not _probe_viewport_coverage_drop(project_design_doc):
        return 0
    doc = project_design_doc or {}
    snap = doc["viewport_coverage_snapshot"]
    before = int(snap["assertion_count_before"])
    after = int(snap["assertion_count_after"])
    escape_hatches = int(snap.get("new_escape_hatch_count") or 0)
    drop = before - after
    extras = [
        f"Assertion count before merge: {before}",
        f"Assertion count after merge: {after}",
    ]
    if drop > 0:
        extras.append(f"Net assertion drop: {drop}")
    if escape_hatches > 0:
        extras.append(f"New escape-hatch attributes added: {escape_hatches}")
    pr = snap.get("merged_pr")
    if pr:
        extras.append(f"Merged PR: #{pr}")
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="viewport_coverage_drop",
        regression_class="viewport_coverage_drop",
        prior_rate=None,
        current_rate=None,
        sample_size=max(drop, escape_hatches),
        magnitude=float(drop + escape_hatches),
        title_hint="viewport coverage drop: assertions removed or silenced",
        extras=extras,
    )
    return 1 if spawn is not None else 0


async def _evaluate_reviewer_sla_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip reviewer_sla_stall:<case_id> when a remediation task sits in EVALUATING too long."""
    if spawn_budget_left <= 0:
        return 0
    if not _runtime_config.get("reviewer_sla_stall_enabled", True):
        return 0

    threshold_hours = float(_runtime_config.get("reviewer_sla_stall_hours", 6))
    now = _utcnow()

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.ACTIVE,
                    RegressionRemediationCase.last_remediation_task_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    task_ids = [c.last_remediation_task_id for c in cases if c.last_remediation_task_id]
    tasks_by_id: dict = {}
    if task_ids:
        tasks_by_id = {
            t.id: t
            for t in (await session.execute(select(Task).where(Task.id.in_(task_ids))))
            .scalars()
            .all()
        }

    spawned = 0
    for case in cases:
        if spawned >= spawn_budget_left:
            break

        task = tasks_by_id.get(case.last_remediation_task_id)
        if task is None:
            continue

        # Terminal tasks have already resolved; non-EVALUATING tasks are not stalled yet.
        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        if task.status != TaskStatus.EVALUATING:
            continue

        evaluating_since = _extract_evaluating_since(task)
        elapsed_hours = (now - evaluating_since).total_seconds() / 3600
        if elapsed_hours < threshold_hours:
            continue

        regression_class = f"reviewer_sla_stall:{case.id}"
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="reviewer_sla_stall",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=int(elapsed_hours),
            magnitude=elapsed_hours,
            title_hint=f"reviewer SLA stall ({elapsed_hours:.1f}h)"[:80],
            prompt_override=_format_reviewer_sla_stall_prompt(
                case_id=case.id,
                pr_number=case.last_remediation_pr_number,
                elapsed_hours=elapsed_hours,
                task_id=task.id,
                task_status=str(task.status),
                latest_status_message=task.latest_status_message,
            ),
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_pipeline_success_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip pipeline_success_rate when 24h trend declines on a non-trivial sample."""
    if spawn_budget_left <= 0:
        return 0
    state = await _current_signal_value_for_class(
        session, project_id, "pipeline_success_rate"
    )
    if state is None or not state["tripped"]:
        return 0
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name=state["signal_name"],
        regression_class="pipeline_success_rate",
        prior_rate=state["prior_rate"],
        current_rate=state["current_rate"],
        sample_size=state["sample_size"],
        magnitude=state["magnitude"],
        extras=state["extras"],
        title_hint="24h pipeline success-rate declining",
    )
    return 1 if spawn is not None else 0


async def _probe_sr_blind_spot_any_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> bool:
    """Side-effect-free probe: would any sr_blind_spot signal trip right now?"""
    from app.sr_analytics import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
    from app.sr_blind_spots import (
        _count_reviewer_disagreement_directions,
        aggregate_sr_blind_spot_themes,
        is_disallowed_blind_spot,
    )
    from app.models import Task as TaskModel

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    tasks = (
        (
            await session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project_id,
                    TaskModel.status == TaskStatus.COMPLETED,
                    TaskModel.created_at >= now - timedelta(days=14),
                )
            )
        )
        .scalars()
        .all()
    )

    c7 = _naive(now - timedelta(days=7))
    curr_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) >= c7]
    prev_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) < c7]
    curr_dirs = _count_reviewer_disagreement_directions(curr_tasks)
    prev_dirs = _count_reviewer_disagreement_directions(prev_tasks)

    for (cat, task_type), c_bucket in curr_dirs.items():
        p_bucket = prev_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
        c_denom = c_bucket["miss"] + c_bucket["tp"] + c_bucket["fp"]
        p_denom = p_bucket["miss"] + p_bucket["tp"] + p_bucket["fp"]
        if c_denom < _MIN_SR_SAMPLE or p_denom < _MIN_SR_SAMPLE:
            continue
        c_rate = c_bucket["miss"] / c_denom
        p_rate = p_bucket["miss"] / p_denom
        if c_rate >= _TREND_DELTA_THRESHOLD and p_rate >= _TREND_DELTA_THRESHOLD:
            return True

    themes = await aggregate_sr_blind_spot_themes(
        session, project_id, days=14, limit=10
    )
    for theme_entry in themes:
        if theme_entry[
            "count"
        ] >= _SR_BLIND_SPOT_THEME_FLOOR and not is_disallowed_blind_spot(
            theme_entry["theme"]
        ):
            return True
    return False


async def _evaluate_sr_blind_spot_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Evaluate SR blind-spot drift: reviewer-disagreement rate and recurring themes.

    Trips when either:
    (a) a (category, task_type)'s miss rate exceeds _TREND_DELTA_THRESHOLD in
        BOTH the current 7d window AND the prior 7-14d window (sustained drift,
        not a one-off), with at least _MIN_SR_SAMPLE denominator in each window;
    (b) a recurring blind_spot theme reaches _SR_BLIND_SPOT_THEME_FLOOR
        occurrences in the 14d window.
    """
    from app.sr_analytics import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
    from app.sr_blind_spots import (
        _count_reviewer_disagreement_directions,
        aggregate_sr_blind_spot_themes,
        is_disallowed_blind_spot,
    )
    from app.models import Task as TaskModel

    if spawn_budget_left <= 0:
        return 0

    spawned = 0
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # --- Disagreement-rate signal ---
    tasks = (
        (
            await session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project_id,
                    TaskModel.status == TaskStatus.COMPLETED,
                    TaskModel.created_at >= now - timedelta(days=14),
                )
            )
        )
        .scalars()
        .all()
    )

    c7 = _naive(now - timedelta(days=7))
    curr_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) >= c7]
    prev_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) < c7]
    curr_dirs = _count_reviewer_disagreement_directions(curr_tasks)
    prev_dirs = _count_reviewer_disagreement_directions(prev_tasks)

    # Dedupe by category: one spawn per category regardless of task_type.
    # Iterate sorted pairs; for each category, try each (cat, task_type)
    # until one trips (or none do for that category).
    from collections import defaultdict

    cat_to_pairs: dict[str, list[str]] = defaultdict(list)
    for cat, task_type in sorted(curr_dirs):
        cat_to_pairs[cat].append(task_type)

    for cat, task_types in sorted(cat_to_pairs.items()):
        if spawned >= spawn_budget_left:
            break
        for task_type in task_types:
            c_bucket = curr_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
            p_bucket = prev_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
            c_denom = c_bucket["miss"] + c_bucket["tp"] + c_bucket["fp"]
            p_denom = p_bucket["miss"] + p_bucket["tp"] + p_bucket["fp"]
            if c_denom < _MIN_SR_SAMPLE or p_denom < _MIN_SR_SAMPLE:
                continue
            c_rate = c_bucket["miss"] / c_denom
            p_rate = p_bucket["miss"] / p_denom
            if c_rate < _TREND_DELTA_THRESHOLD or p_rate < _TREND_DELTA_THRESHOLD:
                continue

            # Both windows trip — collect prompt evidence then spawn.
            reviewer_examples: list[str] = []
            for t in curr_tasks:
                sro = (t.context or {}).get("_self_review_outcome") or {}
                for finding in sro.get("reviewer_findings") or []:
                    if not isinstance(finding, dict):
                        continue
                    if finding.get("category") == cat:
                        desc = str(finding.get("description", ""))[:120]
                        if desc:
                            reviewer_examples.append(desc)
                    if len(reviewer_examples) >= 2:
                        break
                if len(reviewer_examples) >= 2:
                    break

            extras = [
                f"SR blind-spot: category '{cat}' consistently caught by reviewers "
                f"but missed by SR ({task_type} tasks).",
                f"Current 7d miss rate: {c_rate:.1%} ({c_bucket['miss']}/{c_denom})",
                f"Prior 7-14d miss rate: {p_rate:.1%} ({p_bucket['miss']}/{p_denom})",
            ]
            for ex in reviewer_examples:
                extras.append(f"Example reviewer finding: {ex}")

            regression_class = f"sr_blind_spot:{cat}"
            spawn = await _maybe_spawn(
                session,
                project_id,
                signal_name="sr_blind_spot",
                regression_class=regression_class,
                prior_rate=round(p_rate, 3),
                current_rate=round(c_rate, 3),
                sample_size=c_denom,
                magnitude=round(c_rate - p_rate, 3),
                extras=extras,
                title_hint=f"SR blind-spot: {cat} ({task_type})"[:80],
            )
            if spawn is not None:
                spawned += 1
            break  # one spawn per category

    # --- Theme signal ---
    themes = await aggregate_sr_blind_spot_themes(
        session, project_id, days=14, limit=100
    )
    for theme_entry in themes:
        if spawned >= spawn_budget_left:
            break
        count = theme_entry["count"]
        if count < _SR_BLIND_SPOT_THEME_FLOOR:
            break  # sorted descending — subsequent entries also below floor
        theme = theme_entry["theme"]
        if is_disallowed_blind_spot(theme):
            _rj_inner = _cycle_rejections_var.get()
            if _rj_inner is not None:
                _rj_inner["disallowed_theme"] = _rj_inner.get("disallowed_theme", 0) + 1
            logger.info(
                "[REGRESSION_DISPATCHER] disallowed-theme skip project=%s theme=%r",
                project_id,
                theme,
            )
            continue
        regression_class = f"sr_blind_spot:theme:{theme}"
        theme_extras: list[str] = [
            f"Recurring SR blind-spot theme: '{theme}' appeared {count}x in the last 14 days.",
        ]
        for ex in theme_entry.get("examples") or []:
            theme_extras.append(
                f"Example (task {ex['task_id']} / {ex['task_type']}): {ex['text']}"
            )
        triage = theme_entry.get("triage") or {}
        triage_context = triage.get("triage_context") or ""
        if triage_context:
            theme_extras.append(f"Triage: {triage_context}")
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="sr_blind_spot",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=count,
            magnitude=float(count),
            extras=theme_extras,
            title_hint=f"SR blind-spot theme: {theme}"[:80],
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_layer_concentration_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Escalate when a single canonical layer absorbs fragile fixes across many cases.

    Trips when, for a canonical layer:
    - At least _LAYER_CONCENTRATION_MIN_DISTINCT_CASES distinct regression cases
      contributed a fix targeting that layer in the lookback window, AND
    - The layer is fragile: avg hold days < _LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
      OR re-trip count >= _LAYER_CONCENTRATION_FRAGILE_RETRIPS.

    Uses regression_class="layer_concentration:<LAYER>" so each layer has its own
    dedup/cooldown slot via the existing _maybe_spawn gates.
    """
    if spawn_budget_left <= 0:
        return 0

    all_cases = (
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
    if not all_cases:
        return 0

    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]] = [
        (str(c.id), entry)
        for c in all_cases
        for entry in (c.fingerprint_to_fix_log or [])
        if isinstance(entry, dict)
    ]
    if not all_fix_logs_with_case_ids:
        return 0

    all_fix_logs = [entry for _, entry in all_fix_logs_with_case_ids]

    density = compute_layer_defense_density(all_fix_logs_with_case_ids)
    fragility = _compute_layer_fragility_stats(all_fix_logs)
    layer_defense_block = _build_layer_defense_ledger(all_fix_logs)

    spawned = 0
    for density_row in density:
        if spawned >= spawn_budget_left:
            break
        layer = density_row["layer"]
        sample_case_ids = density_row["sample_case_ids"]
        if len(sample_case_ids) < _LAYER_CONCENTRATION_MIN_DISTINCT_CASES:
            continue

        frag = fragility[layer]
        avg_hold = frag["avg_hold_days"]
        retrips = frag["retripped_within_48h_count"]
        is_fragile = (
            avg_hold is not None
            and avg_hold < _LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
        ) or retrips >= _LAYER_CONCENTRATION_FRAGILE_RETRIPS
        if not is_fragile:
            continue

        fix_count = density_row["fix_count"]
        prompt = _format_layer_concentration_prompt(
            layer=layer,
            sample_case_ids=sample_case_ids,
            all_fix_logs_with_case_ids=all_fix_logs_with_case_ids,
            layer_defense_block=layer_defense_block,
        )
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="layer_concentration",
            regression_class=f"layer_concentration:{layer}",
            prior_rate=None,
            current_rate=None,
            sample_size=fix_count,
            magnitude=float(fix_count),
            title_hint=f"Layer concentration: {layer} ({len(sample_case_ids)} cases)",
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_cross_case_fingerprint_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Escalate when the SAME error fingerprint surfaces across multiple regression classes.

    Trips when, for a normalized fingerprint string:
    - Entries observed within ``_CROSS_CASE_FINGERPRINT_LOOKBACK`` span at least
      ``_CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES`` distinct
      ``RegressionRemediationCase.regression_class`` values, AND
    - Total in-window occurrence count >= ``_CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES``.

    Uses regression_class="cross_case_fingerprint:<short-hash>" so each
    fingerprint has its own dedup/cooldown slot via the existing _maybe_spawn
    gates. ``<short-hash>`` is the first 8 hex chars of sha256(fingerprint).

    Skips empty fingerprints and fingerprints whose value equals the case's
    own ``regression_class`` (the fallback ``_record_layer_observation`` writes
    when no real classify_layers message was available — clustering those is
    pure noise).
    """
    if spawn_budget_left <= 0:
        return 0

    all_cases = (
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
    if not all_cases:
        return 0

    now = _utcnow()
    cutoff = now - _CROSS_CASE_FINGERPRINT_LOOKBACK

    # fingerprint -> aggregated state (shared with the recovery path so both
    # sides classify identically).
    by_fp = _index_cross_case_fingerprints(all_cases, cutoff)

    spawned = 0
    # Iterate in deterministic order (highest occurrence first, then fingerprint
    # text) so multiple eligible fingerprints in one cycle resolve consistently.
    candidates = sorted(
        by_fp.items(),
        key=lambda kv: (-kv[1]["occurrences"], kv[0]),
    )
    for fingerprint, bucket in candidates:
        if spawned >= spawn_budget_left:
            break
        occurrence_count = bucket["occurrences"]
        contributing_classes = sorted(bucket["classes"])
        if len(contributing_classes) < _CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES:
            continue
        if occurrence_count < _CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES:
            continue

        short_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]
        regression_class = f"cross_case_fingerprint:{short_hash}"
        prompt = _format_cross_case_fingerprint_prompt(
            fingerprint=fingerprint,
            short_hash=short_hash,
            contributing_classes=contributing_classes,
            contributing_case_ids=bucket["case_ids"],
            sample_messages=bucket["samples"],
            occurrence_count=occurrence_count,
        )
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="cross_case_fingerprint",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=occurrence_count,
            magnitude=float(occurrence_count),
            title_hint=(
                f"cross-case fingerprint {short_hash} "
                f"({len(contributing_classes)} classes, {occurrence_count} hits)"
            )[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_recurring_class_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Proactively escalate when a regression class spans 2+ layers across 3+ cycles.

    Unlike the inline cross-layer detection in _maybe_spawn (which only fires when
    the current signal also trips), this evaluator scans historical case records so
    a class that accumulated cross-layer history without concurrent signals is caught.

    Uses regression_class="recurring_class:<original_class>" for dedup/cooldown.
    """
    if spawn_budget_left <= 0:
        return 0

    cutoff = _utcnow() - _RECURRING_CLASS_LOOKBACK
    all_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.first_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if not all_cases:
        return 0

    # Group fix-log entries by regression_class, skipping meta-investigation
    # classes (shared with the recovery path so both sides group identically).
    by_class = _group_recurring_class_entries(all_cases)

    if not by_class:
        return 0

    # Compute layer defense block once (only if at least one class will trip).
    _layer_defense_block: str | None = None

    spawned = 0
    for regression_class, all_entries in sorted(by_class.items()):
        if spawned >= spawn_budget_left:
            break
        if not is_recurring_cross_layer_class(
            all_entries,
            min_cycles=_RECURRING_CLASS_MIN_CYCLES,
            min_layers=_RECURRING_CLASS_MIN_LAYERS,
        ):
            continue
        targeted = aggregate_targeted_layers(all_entries)

        if _layer_defense_block is None:
            all_fix_logs_with_case_ids: list[tuple[str, dict]] = [
                (str(c.id), entry)
                for c in all_cases
                for entry in (c.fingerprint_to_fix_log or [])
                if isinstance(entry, dict)
            ]
            _layer_defense_block = _build_layer_defense_ledger(
                all_fix_logs_with_case_ids
            )

        cycle_rows = [
            {
                "cycle": i + 1,
                "layers_targeted": e.get("layers_targeted") or [],
                "recommended_unmodifiable_layers": e.get(
                    "recommended_unmodifiable_layers"
                )
                or [],
                "merged_at": e.get("merged_at") or "unknown",
                "spawn_was_cross_layer": bool(e.get("spawn_was_cross_layer")),
                "layers_targeted_present": "layers_targeted" in e,
            }
            for i, e in enumerate(all_entries)
        ]
        prompt = _format_recurring_class_prompt(
            regression_class=regression_class,
            targeted_layers=sorted(targeted),
            cycle_rows=cycle_rows,
            layer_defense_block=_layer_defense_block or "",
        )
        meta_class = f"recurring_class:{regression_class}"
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="recurring_class",
            regression_class=meta_class,
            prior_rate=None,
            current_rate=None,
            sample_size=len(all_entries),
            magnitude=float(len(all_entries)),
            title_hint=(
                f"Recurring cross-layer class: {regression_class} "
                f"({len(all_entries)} cycles, {len(targeted)} layers)"
            )[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


def _get_staleness_raw_ts(
    signal_name: str,
    doc: dict,
    project_id: uuid.UUID,
) -> tuple[Any, timedelta]:
    """Return (raw_timestamp_str, freshness_threshold) for a staleness-monitored signal."""
    spec = get_telemetry_signal_spec(signal_name)
    raw = spec.last_computed_at(doc, project_id) if spec is not None else None
    return raw, _SIGNAL_STALENESS_WINDOWS.get(signal_name, timedelta(hours=48))


async def _evaluate_telemetry_staleness_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Spawn investigate tasks for pipeline signals that have stopped computing.

    Treats each stale signal as its own regression_class so dedup, cooldown,
    and MTTD/MTTR dashboards work identically to other regression classes.
    The existing WARNING Alerts from _run_signal_staleness_watchdog still fire
    — this adds an autonomous investigation, not a replacement.
    """
    if spawn_budget_left <= 0:
        return 0
    doc = project_design_doc or {}
    now = _utcnow()
    spawned = 0
    for spec in TELEMETRY_SIGNAL_SPECS:
        if spawned >= spawn_budget_left:
            break
        raw_ts, threshold = _get_staleness_raw_ts(spec.signal_name, doc, project_id)
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None or (now - computed_at) <= threshold:
            continue
        age = now - computed_at
        regression_class = f"telemetry_stale:{spec.signal_name}"
        prompt = "\n".join(
            [
                f"Telemetry staleness: signal '{spec.signal_name}' has not refreshed in {round(age.total_seconds() / 3600, 1)}h.",
                f"Last computed: {computed_at.isoformat()} (freshness window: {round(threshold.total_seconds() / 3600, 1)}h).",
                f"This signal is computed by: {spec.source_module}.",
                "",
                "Diagnose why this signal stopped computing and ship a fix.",
                "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
            ]
        )
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="telemetry_stale",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=0,
            magnitude=round(age.total_seconds() / 3600, 2),
            extras=[
                f"Signal '{spec.signal_name}' last computed {round(age.total_seconds() / 3600, 1)}h ago."
            ],
            title_hint=f"telemetry staleness: {spec.signal_name}",
            prompt_override=prompt,
            extra_snapshot_fields={
                "signal_last_computed_at": computed_at.isoformat(),
                "age_hours": round(age.total_seconds() / 3600, 1),
                "source_module": spec.source_module,
            },
        )
        if spawn is not None:
            spawned += 1

    # Detect frozen-but-fresh: signal is computing (fresh heartbeat) but real data
    # has not refreshed in the sufficiency-stale window. The metric is frozen.
    if spawned < spawn_budget_left:
        sr_spec = get_telemetry_signal_spec("sr_catch_rate")
        signal_ts = sr_spec.signal_heartbeat_at(doc, project_id) if sr_spec else None
        sufficient_ts = sr_spec.last_sufficient_at(doc, project_id) if sr_spec else None
        if (
            sr_spec is not None
            and sr_spec.signal_heartbeat_at is not None
            and sr_spec.last_sufficient_at is not None
            and signal_ts
            and sufficient_ts
        ):
            signal_at = _parse_iso_timestamp(signal_ts)
            sufficient_at = _parse_iso_timestamp(sufficient_ts)
            if (
                signal_at is not None
                and sufficient_at is not None
                and sr_spec.frozen_signal_fresh_window is not None
                and sr_spec.sufficiency_stale_window is not None
                and (now - signal_at) <= sr_spec.frozen_signal_fresh_window
                and (now - sufficient_at) > sr_spec.sufficiency_stale_window
            ):
                regression_class = (
                    sr_spec.sufficiency_stale_regression_class
                    or "telemetry_sufficiency_stale:sr_catch_rate"
                )
                age = now - sufficient_at
                prompt = "\n".join(
                    [
                        "Telemetry frozen: signal 'sr_catch_rate' is computing (fresh heartbeat)"
                        f" but real data has not refreshed in {round(age.total_seconds() / 3600, 1)}h.",
                        f"signal_last_computed_at: {signal_at.isoformat()} (fresh)",
                        f"last_sufficient_computed_at: {sufficient_at.isoformat()} "
                        f"({round(age.total_seconds() / 3600, 1)}h stale)",
                        f"This signal is computed by: {sr_spec.source_module}.",
                        "",
                        "The metric is running its computation cycle but failing to accumulate"
                        " sufficient data. Diagnose why new data is not flowing into the measurement.",
                        "Possible causes: insufficient reviewer rejections, bucket starvation,"
                        " changed sampling rules, or blocked data pipeline.",
                        "Produce the structured diagnosis JSON"
                        " ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
                    ]
                )
                spawn = await _maybe_spawn(
                    session,
                    project_id,
                    signal_name="telemetry_sufficiency_stale",
                    regression_class=regression_class,
                    prior_rate=None,
                    current_rate=None,
                    sample_size=0,
                    magnitude=round(age.total_seconds() / 3600, 2),
                    extras=[
                        f"Signal 'sr_catch_rate' frozen: real data stale for "
                        f"{round(age.total_seconds() / 3600, 1)}h but signal computing."
                    ],
                    title_hint="telemetry frozen: sr_catch_rate",
                    prompt_override=prompt,
                    extra_snapshot_fields={
                        "signal_last_computed_at": signal_at.isoformat(),
                        "last_sufficient_computed_at": sufficient_at.isoformat(),
                        "sufficiency_stale_hours": round(age.total_seconds() / 3600, 1),
                        "source_module": sr_spec.source_module,
                    },
                )
                if spawn is not None:
                    spawned += 1

    # Detect never-sufficient: sr_catch_rate has been running (heartbeat present)
    # but has never produced a real measurement (last_sufficient_computed_at absent).
    if spawned < spawn_budget_left:
        sr_spec = get_telemetry_signal_spec("sr_catch_rate")
        signal_ts = sr_spec.signal_heartbeat_at(doc, project_id) if sr_spec else None
        sufficient_ts = sr_spec.last_sufficient_at(doc, project_id) if sr_spec else None
        if (
            sr_spec is not None
            and sr_spec.signal_heartbeat_at is not None
            and sr_spec.last_sufficient_at is not None
            and signal_ts
            and not sufficient_ts
        ):
            regression_class = (
                sr_spec.never_sufficient_regression_class
                or "telemetry_never_sufficient:sr_catch_rate"
            )
            prompt = "\n".join(
                [
                    "Telemetry degenerate: signal 'sr_catch_rate' has been computed repeatedly"
                    " but has never reached data sufficiency.",
                    "signal_last_computed_at is set but last_sufficient_computed_at is absent.",
                    f"This signal is computed by: {sr_spec.source_module}.",
                    "",
                    "Diagnose why this signal never reaches data sufficiency and ship a fix.",
                    "Possible causes: sample size too small, no reviewer rejections in task"
                    " history, bucket thresholds too high.",
                    "Produce the structured diagnosis JSON"
                    " ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                    "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
                ]
            )
            spawn = await _maybe_spawn(
                session,
                project_id,
                signal_name="telemetry_never_sufficient",
                regression_class=regression_class,
                prior_rate=None,
                current_rate=None,
                sample_size=0,
                magnitude=0.0,
                extras=["Signal 'sr_catch_rate' has never reached data sufficiency."],
                title_hint="telemetry never-sufficient: sr_catch_rate",
                prompt_override=prompt,
                extra_snapshot_fields={
                    "signal_last_computed_at": signal_ts,
                    "last_sufficient_computed_at": None,
                    "source_module": sr_spec.source_module,
                },
            )
            if spawn is not None:
                spawned += 1

    return spawned


async def _evaluate_strategic_cycle_stall_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_vision: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip strategic_cycle_stall when no strategic task completed in stall window.

    Only fires for projects that have previously completed at least one strategic
    cycle (so new projects without any history are excluded) AND have non-strategic
    activity in the current window (so idle/paused projects are excluded).
    """
    if spawn_budget_left <= 0:
        return 0

    stall_cutoff = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS)

    recent_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.created_at >= stall_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    # If any strategic task completed recently, no stall.
    for t in recent_tasks:
        if (t.first_payload or {}).get(
            "type"
        ) in _STRATEGIC_TASK_TYPES and t.status == TaskStatus.COMPLETED:
            return 0

    # If no non-strategic tasks exist in the window, the project is idle — skip.
    if not any(
        (t.first_payload or {}).get("type") not in _STRATEGIC_TASK_TYPES
        for t in recent_tasks
    ):
        return 0

    # Guard: only fire for projects that have previously had at least one
    # completed strategic cycle. New projects (no prior strategic history)
    # are excluded — they have not yet established a cadence to stall against.
    prior_strategic = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at < stall_cutoff,
                )
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    if not any(
        (t.first_payload or {}).get("type") in _STRATEGIC_TASK_TYPES
        for t in prior_strategic
    ):
        return 0

    failed_strategic = [
        t
        for t in recent_tasks
        if (t.first_payload or {}).get("type") in _STRATEGIC_TASK_TYPES
        and t.status == TaskStatus.FAILED
    ]
    payload_bloat_skip = (project_vision or {}).get("_strategic_payload_bloat_skip")
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="strategic_cycle_stall",
        regression_class="strategic_cycle_stall",
        prior_rate=None,
        current_rate=None,
        sample_size=len(recent_tasks),
        magnitude=float(_STRATEGIC_CYCLE_STALL_HOURS),
        title_hint=f"strategic engine stalled (>{_STRATEGIC_CYCLE_STALL_HOURS}h)",
        prompt_override=_format_strategic_stall_prompt(
            stall_hours=_STRATEGIC_CYCLE_STALL_HOURS,
            recent_task_count=len(recent_tasks),
            failed_strategic=failed_strategic,
            payload_bloat_skip=payload_bloat_skip,
        ),
    )
    return 1 if spawn is not None else 0


async def _evaluate_anomaly_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip on persistent anomalies that have exceeded the occurrence threshold."""
    import app.shared_state as _ss

    if spawn_budget_left <= 0:
        return 0

    proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
    if not proj_anomalies:
        from app.models import Project as _Proj
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

        _proj = await session.get(_Proj, project_id)
        if _proj is not None:
            _stored = ((_proj.design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY)) or {}
            if _stored:
                _ss.persistent_anomalies[str(project_id)] = _stored
                proj_anomalies = _stored
    if not proj_anomalies:
        return 0

    now = _utcnow()
    recency_cutoff = (
        now - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
    ).isoformat()
    spawned = 0
    for anomaly_key, entry in list(proj_anomalies.items()):
        if spawned >= spawn_budget_left:
            break
        if not isinstance(entry, dict):
            continue
        occurrence_count = int(entry.get("occurrence_count") or 0)
        if occurrence_count < _ANOMALY_DISPATCH_MIN_OCCURRENCES:
            continue
        last_seen = str(entry.get("last_seen") or "")
        if last_seen < recency_cutoff:
            continue
        regression_class = f"anomaly:{anomaly_key}"
        sample_msg = entry.get("example_message") or entry.get("latest_details") or ""
        _first_seen_raw = entry.get("first_seen")
        _anomaly_first_seen: datetime | None = None
        if _first_seen_raw:
            try:
                _anomaly_first_seen = datetime.fromisoformat(
                    str(_first_seen_raw).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                pass
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="persistent_anomaly",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=occurrence_count,
            magnitude=float(occurrence_count),
            extras=[
                f"Persistent anomaly '{anomaly_key}' detected {occurrence_count}x "
                f"(severity: {entry.get('severity', 'unknown')}).",
                (entry.get("latest_details") or "")[:300],
            ],
            title_hint=f"persistent anomaly {anomaly_key}"[:80],
            extra_snapshot_fields={
                "hit_count": occurrence_count,
                "occurrence_count": occurrence_count,
                "first_seen": str(entry.get("first_seen") or ""),
                "last_seen": last_seen,
                "sample_message": sample_msg[:500],
            },
            signal_first_seen_at=_anomaly_first_seen,
        )
        if spawn is not None:
            spawned += 1
    return spawned


# _collect_agent_vm_failures moved to dispatcher_signal_data.py.


async def _evaluate_agent_vm_resource_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip agent_vm_resource_exhaustion when VM-layer failures spike in the 24h window."""
    if spawn_budget_left <= 0:
        return 0
    failures = await _collect_agent_vm_failures(session, project_id)
    if len(failures) < _AGENT_VM_RESOURCE_MIN_FAILURES:
        return 0

    reason_counts: dict[str, int] = {}
    task_id_samples: list[str] = []
    task_types: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    for f in failures:
        reason = f["exit_reason"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(task_id_samples) < 5:
            task_id_samples.append(f["task_id"])
        ttype = f["task_type"]
        task_types[ttype] = task_types.get(ttype, 0) + 1
        scope = f["scope"]
        scope_counts[scope] = scope_counts.get(scope, 0) + 1

    count = len(failures)
    reason_summary = ", ".join(f"{r}:{c}" for r, c in sorted(reason_counts.items()))
    type_summary = ", ".join(f"{t}:{c}" for t, c in sorted(task_types.items()))
    scope_summary = ", ".join(f"{s}:{c}" for s, c in sorted(scope_counts.items()))

    prompt = "\n".join(
        [
            "Pipeline regression detected (agent_vm_resource / class=agent_vm_resource_exhaustion).",
            "Canonical layer: AGENT_VM",
            f"{count} tasks failed with VM-infrastructure exit reasons in the last "
            f"{_AGENT_VM_RESOURCE_WINDOW_HOURS}h.",
            f"Exit reasons: {reason_summary}",
            f"Task types affected: {type_summary}",
            f"Task scopes affected: {scope_summary}",
            f"Sample task IDs: {', '.join(task_id_samples)}",
            "",
            "Investigate: is this code-budget creep (work outgrew the VM tier), spot "
            "capacity churn, OS image regression, or something else?",
            "Specifically:",
            "1. Is exhaustion concentrated on a particular scope (small/medium/large)?",
            "2. Is it concentrated on a particular task type?",
            "3. Are OOM kills indicating memory pressure vs. heartbeat losses indicating "
            "spot preemption?",
            "4. Does the timing correlate with an OS image or VM-type change?",
            "",
            "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, "
            "severity, confidence, affected_files}).",
        ]
    )
    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="agent_vm_resource",
        regression_class="agent_vm_resource_exhaustion",
        prior_rate=None,
        current_rate=None,
        sample_size=count,
        magnitude=float(count),
        title_hint=f"agent VM resource exhaustion ({reason_summary})"[:80],
        prompt_override=prompt,
        extra_context_fields={
            "reason_counts": reason_counts,
            "canonical_layer": "AGENT_VM",
        },
    )
    return 1 if spawn is not None else 0


async def _has_active_remediation_child(
    session: AsyncSession,
    case: RegressionRemediationCase,
) -> bool:
    """Return True if the case's most-recent child task is still running.

    Used by the watchdog to distinguish two ACTIVE-stale failure modes:
    a child task that is genuinely still working (auto-escalate one more
    autonomous round) vs. a child that finished without spawning a fix
    (page the operator immediately).
    """
    if case.last_remediation_task_id is None:
        return False
    child = await session.get(Task, case.last_remediation_task_id)
    if child is None:
        return False
    return child.status in _ACTIVE_INVESTIGATE_STATUSES


async def _spawn_auto_escalation_meta_investigate(
    session: AsyncSession,
    project_id: uuid.UUID,
    case: RegressionRemediationCase,
) -> Task:
    """Spawn the one-shot meta-investigation for a stalled ACTIVE case.

    The prompt is intentionally distinct from the original signal-tripped
    investigate: it tells the agent the remediation has stalled and to
    diagnose the BLOCKER (merge conflict, reviewer/CI disagreement,
    infrastructure precondition, leader-election issue) rather than
    re-investigate the original signal. Bypasses ``_maybe_spawn`` because
    the cooldown/dedup guards there are designed to prevent peer
    investigations for the same regression_class — here we explicitly
    want a peer investigation alongside the existing in-flight child.
    """
    prior_id = case.last_remediation_task_id
    prior_status_line = ""
    if prior_id is not None:
        prior = await session.get(Task, prior_id)
        if prior is not None:
            prior_status_line = (
                f"Prior remediation task: {prior.id} (status={prior.status}). "
            )
    prompt_lines = [
        f"A remediation for regression class '{case.regression_class}' has been "
        f"ACTIVE for >24h without resolving the underlying signal. "
        f"{prior_status_line}"
        "Diagnose the REMEDIATION BLOCKER (not the original signal): merge "
        "conflict patterns, reviewer/CI disagreement on prior fix attempts, "
        "infrastructure preconditions (toolchain, missing system libs), or "
        "leader-election / orchestrator cascade issues are the most common "
        "stall classes.",
        "Produce the structured diagnosis JSON ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files}). Do NOT "
        "re-investigate the original signal — focus on what is preventing "
        "remediation from converging.",
    ]
    prompt = "\n\n".join(prompt_lines)
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": _runtime_config.get("integration_branch", "main"),
            "effort": "low",
        },
        "pipeline_regression_dispatcher:auto_escalation_meta_investigate",
    )
    context: dict[str, Any] = {
        "spawned_by": "pipeline_regression_dispatcher_watchdog",
        "regression_class": case.regression_class,
        "signal_name": "auto_escalation_meta_investigate",
        "auto_escalation": True,
        "prior_remediation_task_id": str(prior_id) if prior_id else None,
        "remediation_case_id": str(case.id),
    }
    new_task = Task(
        project_id=project_id,
        title=f"Auto-escalate stalled remediation: {case.regression_class}"[:200],
        description=(
            "Watchdog auto-escalation: the prior investigate child has been "
            "ACTIVE for >24h without resolving the signal. Diagnose the "
            "remediation blocker."
        )[:500],
        payloads=[payload],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "PIPELINE_REGRESSION_DISPATCHER_WATCHDOG",
                "regression_class": case.regression_class,
                "signal_name": "auto_escalation_meta_investigate",
                "auto_escalation": True,
            },
        )
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.INFO,
            title=f"Auto-escalating stalled remediation: {case.regression_class}"[:200],
            message=(
                f"Remediation case for '{case.regression_class}' has been ACTIVE "
                f"for >24h. Spawned meta-investigate task {new_task.id} to "
                f"diagnose the remediation blocker before paging operators."
            )[:1000],
        )
    )
    return new_task


def _dispatch_signal_context_value(
    key: str,
    project_design_doc: dict | None,
    project_vision: dict | None,
) -> Any:
    if key == "project_design_doc":
        return project_design_doc
    if key == "project_vision":
        return project_vision
    raise ValueError(f"unknown dispatch signal context key: {key}")


async def _invoke_dispatch_signal_evaluator(
    spec: BuiltInSignalSpec,
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    project_vision: dict | None,
    spawn_budget_left: int,
) -> int:
    """Run one built-in signal evaluator via the canonical registry."""
    if spec.dispatch_evaluator_name is None:
        return 0
    evaluator = globals()[spec.dispatch_evaluator_name]
    args: list[Any] = [session, project_id]
    args.extend(
        _dispatch_signal_context_value(key, project_design_doc, project_vision)
        for key in spec.dispatch_context_keys
    )
    args.append(spawn_budget_left)
    return await evaluator(*args)


async def _built_in_signal_would_trip(
    spec: BuiltInSignalSpec,
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
) -> bool:
    """Evaluate the deploy-defer probe configured for one built-in signal."""
    if spec.probe_kind is None:
        return False
    if spec.probe_kind == "current_state":
        if spec.probe_regression_class is None:
            return False
        state = await _current_signal_value_for_class(
            session,
            project_id,
            spec.probe_regression_class,
            project_design_doc,
        )
        return state is not None and state["tripped"]
    if spec.probe_kind == "fix_loop_divergence_fingerprint":
        return bool(
            _runtime_config.get("fix_loop_divergence_fingerprint_enabled", False)
            and await _collect_fix_loop_divergence_fingerprints(session, project_id)
        )
    if spec.probe_kind == "persistent_anomaly_collection":
        import app.shared_state as _ss

        proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
        recency_cutoff = (
            _utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
        ).isoformat()
        for entry in proj_anomalies.values():
            if not isinstance(entry, dict):
                continue
            if (
                int(entry.get("occurrence_count") or 0)
                < _ANOMALY_DISPATCH_MIN_OCCURRENCES
            ):
                continue
            if str(entry.get("last_seen") or "") < recency_cutoff:
                continue
            return True
        return False
    if spec.probe_kind == "sr_blind_spot_any":
        return await _probe_sr_blind_spot_any_trip(session, project_id)
    if spec.probe_kind == "strategic_cycle_stall_any":
        return await _probe_strategic_stall_any_trip(session, project_id)
    if spec.probe_kind == "viewport_regression_collection":
        return bool(await _collect_viewport_failures(session, project_id))
    if spec.probe_kind == "viewport_coverage_drop":
        return _probe_viewport_coverage_drop(project_design_doc)
    if spec.probe_kind == "agent_vm_resource_count":
        vm_failures = await _collect_agent_vm_failures(session, project_id)
        return len(vm_failures) >= _AGENT_VM_RESOURCE_MIN_FAILURES
    raise ValueError(f"unknown probe kind: {spec.probe_kind}")


async def _any_signal_would_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
) -> bool:
    """Side-effect-free probe: would any subscribed signal trip right now?

    Used to gate the deploy-monitoring defer alert so we don't claim a signal
    tripped when none actually would. The registry marks which built-in
    signals participate in the side-effect-free probe.
    """
    for spec in DISPATCH_BUILT_IN_SIGNAL_SPECS:
        if await _built_in_signal_would_trip(
            spec,
            session,
            project_id,
            project_design_doc,
        ):
            return True
    return False


async def _probe_strategic_stall_any_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> bool:
    """Side-effect-free probe: would the strategic_cycle_stall signal trip?"""
    stall_cutoff = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS)
    recent_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.created_at >= stall_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if any(
        (t.first_payload or {}).get("type") in _STRATEGIC_TASK_TYPES
        and t.status == TaskStatus.COMPLETED
        for t in recent_tasks
    ):
        return False
    if not any(
        (t.first_payload or {}).get("type") not in _STRATEGIC_TASK_TYPES
        for t in recent_tasks
    ):
        return False
    prior_strategic = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at < stall_cutoff,
                )
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return any(
        (t.first_payload or {}).get("type") in _STRATEGIC_TASK_TYPES
        for t in prior_strategic
    )


async def run_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Evaluate all subscribed pipeline-regression signals for ``project_id``.

    Returns the number of investigate tasks spawned this call. Caller is
    responsible for committing the session — this matches the
    ``_spawn_proactive_fingerprint_investigations`` pattern in
    ``anomaly_detection`` and lets the orchestrator coalesce dispatcher work
    with its existing 60s tick.
    """
    from app.models import Project

    cycle_started_at = _utcnow()
    _rj: dict = {
        "dedup": 0,
        "cooldown": 0,
        "system_cap": 0,
        "manual_intervention_suppressed": 0,
        "false_positive_suppressed": 0,
        "disallowed_theme": 0,
    }
    _token = _cycle_rejections_var.set(_rj)
    _st_counter: list = [0]
    _st_token = _cycle_signals_tripped_var.set(_st_counter)
    _ledger_cache_init: dict = {}
    _ledger_token = _cycle_layer_ledger_var.set(_ledger_cache_init)
    _spawned = 0
    _se_count = 0
    _failed_evaluator_names: list[str] = []
    try:
        # Force a full SELECT rather than session.get(): a caller that left a
        # partial Project (e.g. via ``load_only(Project.id)``) in the session
        # identity map would otherwise return that cached partial here, and the
        # ``project.design_doc`` access below would trigger a sync lazy-load in
        # async context that poisons the connection. populate_existing=True
        # repopulates any cached row with all columns.
        project = (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if project is None:
            return 0

        stale_alerts_created = await _run_signal_staleness_watchdog(
            session, project_id, project.design_doc
        )

        # Deploy-monitoring guard: if a fresh deploy is in its observation window,
        # defer spawns to avoid chasing deploy-induced signal noise.  PAUSED/HALTED
        # are operator-controlled states the dispatcher already skips via other
        # coordination; only MONITORING needs a time-bounded defer here. Use the
        # canonical helpers in deploy_health so the defer rule has one home.
        deploy_state = await session.get(DeploymentState, 1)
        defer_until = get_deploy_monitoring_defer_until(deploy_state)
        if defer_until is not None and _utcnow() < defer_until:
            # Only emit the operator-visible Alert when a signal actually would
            # have tripped this cycle — otherwise the run_once tick (every 60s)
            # would spam ~30 misleading "signal tripped" alerts across the 30-min
            # window. record_deploy_monitoring_deferral additionally dedupes by
            # title so a single alert covers the whole monitoring window.
            if await _any_signal_would_trip(session, project_id, project.design_doc):
                logger.info(
                    "[REGRESSION_DISPATCHER] deploy-monitoring-defer project=%s "
                    "defer_until=%s — skipping spawn",
                    project_id,
                    defer_until.isoformat(),
                )
                await record_deploy_monitoring_deferral(
                    session,
                    project_id,
                    deploy_state,
                    "pipeline_regression_dispatcher",
                )
                await session.commit()
            elif stale_alerts_created:
                await session.commit()
            return 0

        # Cap 1: research slot budget shared with autonomous research.
        max_total = int(_runtime_config.get("max_total_research_per_project", 5) or 5)
        active_research = await _count_active_research_for_project(session, project_id)
        project_remaining = max_total - active_research
        if project_remaining <= 0:
            _rj["system_cap"] += 1
            logger.info(
                "[REGRESSION_DISPATCHER] cap-hit project=%s active_research=%d/%d "
                "(max_total_research_per_project) — skipping",
                project_id,
                active_research,
                max_total,
            )
            return 0

        # Cap 2: per-cycle system-wide concurrency budget.
        spawn_budget = min(project_remaining, _REGRESSION_DISPATCHER_SYSTEM_CAP)

        for spec in DISPATCH_BUILT_IN_SIGNAL_SPECS:
            try:
                _spawned += await _invoke_dispatch_signal_evaluator(
                    spec,
                    session,
                    project_id,
                    project.design_doc,
                    project.vision,
                    spawn_budget - _spawned,
                )
                _se_count += 1
            except Exception as _exc:  # noqa: BLE001
                if spec.evaluator_name is not None:
                    _failed_evaluator_names.append(spec.evaluator_name)
                    logger.warning(
                        "[REGRESSION_DISPATCHER] evaluator %s raised project=%s: %s",
                        spec.evaluator_name,
                        project_id,
                        _exc,
                    )

        # Surface / resolve per-evaluator failure alerts so operators can see
        # which signals are partially blind.  Uses the same deduped-alert
        # pattern as _run_signal_staleness_watchdog.
        _failed_set = set(_failed_evaluator_names)
        for _ev_name in _ALL_EVALUATOR_NAMES:
            _ev_title = f"{_EVALUATOR_FAILURE_ALERT_TITLE_PREFIX} {_ev_name}"
            if _ev_name in _failed_set:
                await _ensure_watchdog_alert_open(
                    session,
                    project_id,
                    _ev_title,
                    _ev_title,
                    f"Evaluator {_ev_name!r} raised for project {project_id}; remaining evaluators still ran.",
                    AlertSeverity.WARNING,
                )
            else:
                await _resolve_watchdog_alert(session, project_id, _ev_title)

        if _spawned or stale_alerts_created:
            await session.commit()
        return _spawned
    finally:
        _cycle_rejections_var.reset(_token)
        _cycle_signals_tripped_var.reset(_st_token)
        _cycle_layer_ledger_var.reset(_ledger_token)
        # Persist the cycle record to the durable table.  Use a fresh
        # session-scope INSERT in its own commit so a write error here cannot
        # mask the spawn commits already flushed above.  Matches the prior
        # ``finally``-block semantics: the record is written even when
        # run_once short-circuits early (deploy-monitoring defer, project not
        # found, exception inside an evaluator).
        from app.models import DispatcherCycleRecord

        try:
            # The run_once body may have left the session needing a rollback
            # (an evaluator raised mid-transaction); a stale failed transaction
            # would make the INSERT below raise PendingRollbackError. Clearing
            # it first lets the cycle record write on a clean transaction — the
            # spawn commits above already flushed, so nothing is lost.
            await session.rollback()
            session.add(
                DispatcherCycleRecord(
                    project_id=project_id,
                    cycle_started_at=cycle_started_at,
                    cycle_finished_at=_utcnow(),
                    signals_evaluated=_se_count,
                    signals_tripped=_st_counter[0],
                    spawns_emitted=_spawned,
                    rejections_by_reason=dict(_rj),
                )
            )
            await session.commit()
        except Exception as rec_exc:  # noqa: BLE001
            logger.warning(
                "[REGRESSION_DISPATCHER] cycle-record write failed: %s",
                rec_exc,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
        # Periodic prune so DispatcherCycleRecord doesn't grow unboundedly.
        # Cheap when the index hits no expired rows; failures are swallowed.
        await _prune_old_dispatcher_cycle_records(session)
        _last_cycle_monotonic[str(project_id)] = time.monotonic()
