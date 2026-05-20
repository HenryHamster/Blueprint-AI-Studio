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

import contextvars
import hashlib
import logging
import re
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
    CLOSE_REASON_RECOVERED_MARGINAL,
    CLOSE_REASON_RECOVERED_UNATTENDED,
    DeploymentState,
    ESCALATION_REASON_STALE_ACTIVE,
    ESCALATION_REASON_STALE_MERGED,
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
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _to_naive_utc, _utcnow

logger = logging.getLogger(__name__)

# Meta-investigation escalation reason constants (case-level audit trail).
# Complement the four manual-intervention reasons defined in app.models.
ESCALATION_REASON_ESCALATED_CROSS_LAYER = "escalated_cross_layer"
ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION = "escalated_layer_concentration"
ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT = "escalated_cross_case_fingerprint"
ESCALATION_REASON_ESCALATED_RECURRENCE = "escalated_recurrence"
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

# Give merged signals time to settle before the recovery scan re-evaluates them.
_REGRESSION_RECOVERY_COOLDOWN = timedelta(minutes=15)
_RECURRENCE_LOOKBACK = timedelta(days=30)

# Watchdog staleness thresholds.  An ACTIVE case that hasn't been updated in
# 24h has silently stalled (investigation finished with low confidence and no
# remediation was spawned, or the close-loop step dropped it).  A MERGED case
# that hasn't been updated in 6h means the recovery scan cannot evaluate it
# (e.g. signal_snapshot_at_merge is missing).  Both escalate to
# NEEDS_MANUAL_INTERVENTION and open an Alert so operators are notified.
_WATCHDOG_ACTIVE_STALE_THRESHOLD = timedelta(hours=24)
_WATCHDOG_MERGED_STALE_THRESHOLD = timedelta(hours=6)

# Persistent-anomaly dispatch thresholds. An anomaly must recur at least this
# many times within the recency window before an investigation is spawned — the
# same philosophy as the 5pp SR delta floor: one bad cycle is noise, repeated
# occurrences are signal.
_ANOMALY_DISPATCH_MIN_OCCURRENCES = 3
_ANOMALY_DISPATCH_RECENCY_HOURS = 48

# Per-fingerprint fix-loop divergence threshold. Same noisy-code-path signal,
# but grouped by normalized error fingerprint so a poisoned area gets a
# targeted investigation instead of only the aggregate rate signal.
_FIX_LOOP_DIVERGENCE_FINGERPRINT_MIN_TASKS = 3
_FIX_LOOP_DIVERGENCE_FINGERPRINT_WINDOW_DAYS = 7

# Viewport-regression signal thresholds. Viewport tests run less often than fix-loop
# cycles, so the minimum failure count is lower. Same 7-day window as fingerprints.
_VIEWPORT_REGRESSION_MIN_FAILURES = 2
_VIEWPORT_REGRESSION_WINDOW_DAYS = 7

# Agent VM resource exhaustion signal thresholds.
# VM-layer failures (OOM, spot preemption, provisioning failure, heartbeat loss)
# must reach this count in the 24h window before an investigation is spawned.
# Informed by the playbook's e2-standard-2 memory-pressure entry: 3 occurrences
# is noise suppression matching _ANOMALY_DISPATCH_MIN_OCCURRENCES.
_AGENT_VM_RESOURCE_MIN_FAILURES = 3
_AGENT_VM_RESOURCE_WINDOW_HOURS = 24

# Fragile-recurrence meta-investigation thresholds.
# A case is "fragile" if it has recurred >= this many times OR any prior fix cycle
# held < _FRAGILE_RECURRENCE_SHORT_HOLD_DAYS before the signal re-tripped.
_FRAGILE_RECURRENCE_MIN_COUNT = 3
_FRAGILE_RECURRENCE_SHORT_HOLD_DAYS = 2.0

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

# SR false-positive signal thresholds. A category's FP rate must exceed this
# floor in BOTH the current and previous window (≥2 consecutive) before
# tripping an investigation. The sample floor guards against noise from very
# small windows (current_human_count used as proxy since fp_count is derived).
_SR_FP_RATE_FLOOR = 0.40
_SR_FP_MIN_SAMPLE = 5

# Lookback window for the project-wide layer-defense ledger injected into meta-investigation
# prompts. Covers ~3 months of defense activity across all regression classes for the project.
_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS = 90

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
_CROSS_CASE_FINGERPRINT_LOOKBACK = timedelta(days=14)
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

_SIGNAL_STALENESS_WINDOWS = {
    "sr_catch_rate": timedelta(hours=48),
    "fix_loop_divergence": timedelta(days=7),
    "sr_blind_spot": timedelta(days=7),
    "persistent_anomaly": timedelta(hours=48),
    "deploy_health": timedelta(hours=6),
    "ci_completion": timedelta(hours=6),
}

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


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


async def _run_signal_staleness_watchdog(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict[str, Any] | None,
) -> int:
    """Emit warning Alerts when a known signal source has stopped updating."""
    doc = project_design_doc or {}
    now = _utcnow()
    specs: list[tuple[str, Any, timedelta]] = [
        (
            "sr_catch_rate",
            (doc.get("sr_catch_rate_trends") or {}).get("signal_last_computed_at")
            or (doc.get("sr_catch_rate_trends") or {}).get("computed_at"),
            _SIGNAL_STALENESS_WINDOWS["sr_catch_rate"],
        ),
        (
            "fix_loop_divergence",
            doc.get("fix_loop_divergence_signal_last_computed_at")
            or doc.get("fix_loop_divergence_detected_at"),
            _SIGNAL_STALENESS_WINDOWS["fix_loop_divergence"],
        ),
        (
            "persistent_anomaly",
            (
                (_shared_state.persistent_anomalies.get(str(project_id)) or {}).get(
                    "signal_last_computed_at"
                )
            ),
            _SIGNAL_STALENESS_WINDOWS["persistent_anomaly"],
        ),
        (
            "deploy_health",
            _shared_state.deploy_monitoring.get("signal_last_computed_at"),
            _SIGNAL_STALENESS_WINDOWS["deploy_health"],
        ),
        (
            "sr_blind_spot",
            doc.get("sr_blind_spot_signal_last_computed_at"),
            _SIGNAL_STALENESS_WINDOWS["sr_blind_spot"],
        ),
    ]
    created = 0
    for signal_name, raw_ts, threshold in specs:
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None or (now - computed_at) <= threshold:
            continue
        age = now - computed_at
        title = f"Signal staleness: {signal_name}"
        message = (
            f"{signal_name} last computed {round(age.total_seconds() / 3600, 1)}h "
            f"ago; freshness window is {round(threshold.total_seconds() / 3600, 1)}h."
        )
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
        if existing is None:
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=title,
                    message=message,
                )
            )
            created += 1
    return created


async def get_dispatcher_cycle_records(
    session: AsyncSession,
    project_id: str | None = None,
    limit: int = 100,
    window_hours: int | None = None,
) -> list[dict]:
    """Return recent dispatcher cycle records from the durable table.

    Reads from ``DispatcherCycleRecord``.  Optionally filters to one project
    and/or a trailing window.  Results are returned oldest-first (matching
    the deque-iteration order callers historically relied on).

    On an empty table, returns ``[]`` — never raises — so the health
    endpoint can render 'no data' on cold start.
    """
    from app.models import DispatcherCycleRecord  # noqa: PLC0415

    stmt = select(DispatcherCycleRecord)
    if project_id is not None:
        stmt = stmt.where(DispatcherCycleRecord.project_id == uuid.UUID(project_id))
    if window_hours is not None:
        cutoff = _utcnow() - timedelta(hours=window_hours)
        stmt = stmt.where(DispatcherCycleRecord.cycle_started_at >= cutoff)
    stmt = stmt.order_by(DispatcherCycleRecord.cycle_started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    # Return oldest-first to mirror the prior deque semantics.
    rows = list(reversed(rows))
    return [
        {
            "project_id": str(r.project_id),
            "cycle_started_at": r.cycle_started_at.isoformat(),
            "cycle_finished_at": r.cycle_finished_at.isoformat(),
            "signals_evaluated": r.signals_evaluated or 0,
            "signals_tripped": r.signals_tripped or 0,
            "spawns_emitted": r.spawns_emitted or 0,
            "rejections_by_reason": dict(r.rejections_by_reason or {}),
            "mttd_p50_sec": r.mttd_p50_sec,
            "marginal_recoveries_count": r.marginal_recoveries_count or 0,
            "confirmed_recoveries_count": r.confirmed_recoveries_count or 0,
        }
        for r in rows
    ]


def compute_mtti_p50_sec(cases: list) -> float | None:
    """Return median seconds from escalation to operator acknowledgement.

    Filters to cases where both ``needs_manual_intervention_at`` and
    ``operator_acknowledged_at`` are set.  Returns ``None`` when no acked
    cases are present in the input.  Uses ``statistics.median`` — no numpy.
    """
    import statistics  # noqa: PLC0415

    deltas = [
        (c.operator_acknowledged_at - c.needs_manual_intervention_at).total_seconds()
        for c in cases
        if c.needs_manual_intervention_at is not None
        and c.operator_acknowledged_at is not None
    ]
    return statistics.median(deltas) if deltas else None


async def _prune_old_dispatcher_cycle_records(session: AsyncSession) -> int:
    """Delete DispatcherCycleRecord rows older than the retention window.

    Called opportunistically inside ``run_once``; failures are swallowed so
    a prune error cannot mask a successful spawn commit.  Returns the count
    of rows deleted (0 when nothing matches).
    """
    from sqlalchemy import delete as sql_delete  # noqa: PLC0415
    from app.models import DispatcherCycleRecord  # noqa: PLC0415

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


def _format_signal_lines(
    signal_name: str,
    regression_class: str,
    prior_rate: float | None,
    current_rate: float | None,
    sample_size: int,
    extras: list[str] | None = None,
) -> str:
    prior_str = f"{prior_rate:.1%}" if prior_rate is not None else "N/A"
    curr_str = f"{current_rate:.1%}" if current_rate is not None else "N/A"
    lines = [
        f"Pipeline regression detected ({signal_name} / class={regression_class}).",
        f"Prior rate: {prior_str}",
        f"Current rate: {curr_str}",
        f"Sample size: {sample_size}",
    ]
    if extras:
        lines.extend(extras)
    lines.append(
        "\nFIRST: triage whether this signal actually represents a regression. "
        "Set `verdict` to 'not_a_regression' (not 'real_regression') when ANY "
        "of these apply:\n"
        "  - The behavior the signal observed is intentional / by design "
        "(e.g. a pipeline step that is intentionally skipped under known "
        "conditions, a feature flag deliberately gating a step, a recently "
        "merged change that altered the expected baseline).\n"
        "  - The sample size is too small for the prior/current delta to be "
        "statistically meaningful (random variance, not a real shift).\n"
        "  - The 'regression' is just a known-incomplete pipeline step (e.g. "
        "the agent did not run the full backend test suite — that is the "
        "documented design, not a bug).\n"
        "  - The signal is observing transient infrastructure noise (one-off "
        "VM crash, flaky external dep) with no code-side root cause.\n"
        "When verdict='not_a_regression', populate `verdict_reason` with one "
        "to three sentences citing concrete evidence (file paths, design "
        "doc passages, prior commit SHAs, etc.). Leave recommended_fix "
        "empty in that case — DO NOT fabricate a fix to satisfy the schema.\n"
        "\nOTHERWISE (verdict='real_regression'), diagnose the underlying "
        "root cause and produce the structured diagnosis JSON report "
        "({summary, root_cause, recommended_fix, severity, confidence, "
        "affected_files, verdict, verdict_reason}). Do NOT prescribe a fix "
        "yourself — let the investigate→fix chain produce the remediation."
    )
    return "\n".join(lines)


def _format_sr_fp_signal_lines(
    regression_class: str,
    current_fp_rate: float,
    prior_fp_rate: float,
    sample_size: int,
) -> str:
    cat = (
        regression_class.split(":", 1)[1]
        if ":" in regression_class
        else regression_class
    )
    lines = [
        f"SR over-paranoia detected (sr_false_positive / class={regression_class}).",
        f"Category: {cat}",
        f"Current false-positive rate: {current_fp_rate:.1%}",
        f"Prior window false-positive rate: {prior_fp_rate:.1%}",
        f"Sample size (human-reviewed tasks): {sample_size}",
        "",
        "A false positive means the self-review flagged this category as FAILED but no "
        "fresh reviewer confirmed the issue. Both windows are above the "
        f"{_SR_FP_RATE_FLOOR:.0%} floor, indicating a persistent over-paranoia pattern.",
        "",
        "Investigate whether the SR check criterion for this category is too aggressive. "
        "Specifically:",
        "1. Review the self-review prompt wording for this category — does it invite "
        "false positives by being too broadly worded?",
        "2. Check whether the verdict threshold needs tightening (e.g. the check flags "
        "on minor issues that reviewers consistently dismiss).",
        "3. Look at recent tasks where this category was flagged FAILED but the fresh "
        "reviewer passed it — what pattern do they share?",
        "4. Propose a prompt tweak or criterion adjustment, NOT an application-code change.",
        "",
        "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, "
        "severity, confidence, affected_files}) where recommended_fix describes the "
        "prompt/criterion change, not a code fix.",
    ]
    return "\n".join(lines)


def _format_prior_attempt_context(
    case: RegressionRemediationCase,
    *,
    omit_pr_line: bool = False,
) -> str:
    """Build a prior-attempt notice to append when re-spawning after >= 2 failures.

    Only called when existing_case.attempts >= 2 so the first re-arm (attempts==1)
    keeps the original prompt unchanged — this avoids noise on the second attempt
    and reserves the enrichment for cases that have already failed twice.

    ``omit_pr_line`` lets a sibling block (e.g. PRIOR REMEDIATION FAILURE
    CONTEXT, which already names the PR) suppress the duplicate PR mention.
    """
    lines = [
        "",
        "PRIOR ATTEMPT CONTEXT",
        f"This regression has been investigated {case.attempts} time(s) without recovering the signal.",
    ]
    pr = case.last_remediation_pr_number
    if pr is not None and not omit_pr_line:
        lines.append(
            f"Prior remediation PR: #{pr}. Read its diff to understand what was already tried "
            f"before forming a new hypothesis."
        )
    snap = case.signal_snapshot_at_merge or {}
    post_val = snap.get("current_rate")
    if post_val is not None:
        lines.append(f"Signal value after prior fix merged: {post_val:.1%}.")
    lines.append(
        "Do NOT converge on the same diagnosis as prior attempts. "
        "Approach from a different angle — look for a deeper root cause or a "
        "different contributing factor that the prior fix missed."
    )
    return "\n".join(lines)


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


def _parse_dispatcher_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _format_dispatcher_duration(delta: timedelta | None) -> str:
    if delta is None:
        return "unknown"
    seconds = max(int(delta.total_seconds()), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, rem = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{rem}s"


def _extract_recurrence_cycles(
    case: RegressionRemediationCase,
) -> list[dict[str, Any]]:
    log_entries = case.fingerprint_to_fix_log or []
    snapshot = case.signal_snapshot_at_merge or {}
    merged_fallback = _parse_dispatcher_dt(snapshot.get("captured_at"))
    cycles: list[dict[str, Any]] = []
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        has_fix = entry.get("fix_pr_number") is not None
        has_recovery = entry.get("recovered_at") is not None
        if has_fix:
            layers_present = "layers_targeted" in entry
            raw_layers = entry.get("layers_targeted") if layers_present else None
            layers_list = (
                [str(x) for x in raw_layers] if isinstance(raw_layers, list) else []
            )
            cycles.append(
                {
                    "fix_pr_number": entry.get("fix_pr_number"),
                    "merged_at": _parse_dispatcher_dt(entry.get("merged_at"))
                    or merged_fallback
                    or _parse_dispatcher_dt(entry.get("recovered_at"))
                    or case.updated_at
                    or case.created_at,
                    "recovered_at": None,
                    "layers_targeted_present": layers_present,
                    "layers_targeted": layers_list,
                }
            )
            continue
        if has_recovery:
            recovered_at = _parse_dispatcher_dt(entry.get("recovered_at"))
            if cycles and cycles[-1].get("recovered_at") is None:
                cycles[-1]["recovered_at"] = recovered_at
                continue
            cycles.append(
                {
                    "fix_pr_number": None,
                    "merged_at": merged_fallback or recovered_at or case.updated_at,
                    "recovered_at": recovered_at,
                    "layers_targeted_present": False,
                    "layers_targeted": [],
                }
            )
    if not cycles and (
        case.last_remediation_pr_number is not None
        or merged_fallback is not None
        or snapshot.get("captured_at") is not None
    ):
        cycles.append(
            {
                "fix_pr_number": case.last_remediation_pr_number,
                "merged_at": merged_fallback or case.updated_at or case.created_at,
                "recovered_at": None,
                "layers_targeted_present": False,
                "layers_targeted": [],
            }
        )
    return cycles


def compute_layer_defense_density(
    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Return per-canonical-layer defense activity over a lookback window.

    Walks ``(case_id, fix_log_entry)`` pairs from
    ``RegressionRemediationCase.fingerprint_to_fix_log``, applies the same
    ``merged_at`` cutoff used by ``_build_layer_defense_ledger``, and returns
    one entry per canonical layer in canonical order — including layers with
    zero activity. Each entry has ``layer``, ``fix_count``, ``last_fix_at``
    (ISO timestamp of the most recent qualifying ``merged_at`` for that layer,
    or ``None``), and ``sample_case_ids`` (up to 5 distinct case ids whose fix
    logs contributed an entry, ordered most-recent-first).
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    layer_state: dict[str, dict[str, Any]] = {
        layer: {
            "fix_count": 0,
            "last_fix_at": None,  # datetime
            "case_contribs": [],  # list[(merged_dt, case_id)] for sampling
        }
        for layer in LAYER_DISPLAY_ORDER
    }

    for case_id, entry in all_fix_logs_with_case_ids:
        if not isinstance(entry, dict):
            continue
        merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
        if merged_dt is None or merged_dt < cutoff:
            continue
        layers_targeted = entry.get("layers_targeted") or []
        if not layers_targeted:
            continue
        for layer in layers_targeted:
            if layer not in layer_state:
                continue
            s = layer_state[layer]
            s["fix_count"] += 1
            if s["last_fix_at"] is None or merged_dt > s["last_fix_at"]:
                s["last_fix_at"] = merged_dt
            s["case_contribs"].append((merged_dt, str(case_id)))

    result: list[dict[str, Any]] = []
    for layer in LAYER_DISPLAY_ORDER:
        s = layer_state[layer]
        contribs = sorted(s["case_contribs"], key=lambda p: p[0], reverse=True)
        seen: set[str] = set()
        sample_ids: list[str] = []
        for _dt, cid in contribs:
            if cid in seen:
                continue
            seen.add(cid)
            sample_ids.append(cid)
            if len(sample_ids) >= 5:
                break
        result.append(
            {
                "layer": layer,
                "fix_count": s["fix_count"],
                "last_fix_at": s["last_fix_at"].isoformat()
                if s["last_fix_at"] is not None
                else None,
                "sample_case_ids": sample_ids,
            }
        )
    return result


def _compute_layer_fragility_stats(
    all_fix_logs: list[dict[str, Any]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """Return per-layer fragility stats from fix-log entries within the lookback window.

    Each canonical layer entry contains: fix_count, avg_hold_days (float|None),
    still_holding_count, held_gt_7d_count, retripped_within_48h_count.
    avg_hold_days is None when no re-tripped entries exist (i.e. all defenses
    are still holding or no fixes recorded).
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    layer_stats: dict[str, dict[str, Any]] = {
        layer: {
            "fix_count": 0,
            "hold_days": [],
            "still_holding_ages": [],
            "retripped_within_48h": 0,
            "held_gt_7d": 0,
            "single_layer_fix_count": 0,
            "single_layer_hold_days": [],
            "multi_layer_fix_count": 0,
            "multi_layer_hold_days": [],
        }
        for layer in LAYER_DISPLAY_ORDER
    }

    for entry in all_fix_logs:
        if not isinstance(entry, dict):
            continue
        merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
        if merged_dt is None or merged_dt < cutoff:
            continue
        layers_targeted = entry.get("layers_targeted") or []
        if not layers_targeted:
            continue
        recovered_dt = _parse_dispatcher_dt(entry.get("recovered_at"))
        age_days = (now - merged_dt).total_seconds() / 86400.0
        is_multi_layer = len(layers_targeted) >= 2

        for layer in layers_targeted:
            if layer not in layer_stats:
                continue
            s = layer_stats[layer]
            s["fix_count"] += 1
            if is_multi_layer:
                s["multi_layer_fix_count"] += 1
            else:
                s["single_layer_fix_count"] += 1
            if recovered_dt is not None:
                hold = (recovered_dt - merged_dt).total_seconds() / 86400.0
                s["hold_days"].append(hold)
                if is_multi_layer:
                    s["multi_layer_hold_days"].append(hold)
                else:
                    s["single_layer_hold_days"].append(hold)
                if hold < 2.0:
                    s["retripped_within_48h"] += 1
                if hold > 7.0:
                    s["held_gt_7d"] += 1
            else:
                s["still_holding_ages"].append(age_days)
                if age_days > 7.0:
                    s["held_gt_7d"] += 1

    def _avg_or_none(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    result: dict[str, dict[str, Any]] = {}
    for layer in LAYER_DISPLAY_ORDER:
        s = layer_stats[layer]
        hold_days_list = s["hold_days"]
        result[layer] = {
            "fix_count": s["fix_count"],
            "avg_hold_days": _avg_or_none(hold_days_list),
            "still_holding_count": len(s["still_holding_ages"]),
            "held_gt_7d_count": s["held_gt_7d"],
            "retripped_within_48h_count": s["retripped_within_48h"],
            "single_layer_fix_count": s["single_layer_fix_count"],
            "single_layer_avg_hold_days": _avg_or_none(s["single_layer_hold_days"]),
            "multi_layer_fix_count": s["multi_layer_fix_count"],
            "multi_layer_avg_hold_days": _avg_or_none(s["multi_layer_hold_days"]),
        }
    return result


_META_ESCALATION_REASONS: frozenset[str] = frozenset(
    [
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
        ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
        ESCALATION_REASON_ESCALATED_RECURRENCE,
    ]
)

_ESCALATION_BUCKET_ALL_OTHER = "all_other"
_ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP = "compliance_followup"

_ESCALATION_BUCKET_ORDER: list[str] = [
    ESCALATION_REASON_ESCALATED_CROSS_LAYER,
    ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
    ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
    ESCALATION_REASON_ESCALATED_RECURRENCE,
    _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP,
    _ESCALATION_BUCKET_ALL_OTHER,
]


def _compliance_followup_fix_idx(raw_log: list) -> int:
    """Return the 0-based fix-cycle index of the first fix entry after a compliance_followup_spawned marker.

    Returns -1 if no such entry exists (no followup spawned or the spawned task has not yet merged).
    """
    saw_spawn = False
    fix_idx = 0
    for entry in raw_log:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "compliance_followup_spawned":
            saw_spawn = True
        elif entry.get("fix_pr_number") is not None:
            if saw_spawn:
                return fix_idx
            fix_idx += 1
    return -1


def compute_escalation_outcome_breakdown(
    cases: list[RegressionRemediationCase],
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate case outcomes by escalation_reason bucket.

    Groups cases into five buckets — one per meta-escalation reason plus
    'all_other' for None or any non-meta value — and computes per-bucket:
    - merged_count: number of cases whose first cycle merged within the window
    - retripped_within_30d_count: of those, how many had a subsequent cycle
      within 30 days of the first cycle's merged_at
    - durable_resolution_rate: 1 - retripped / merged, or None when merged_count == 0
    - median_hold_days: median of (recovered_at - merged_at) in days for cases
      whose first cycle has both timestamps
    - in_flight_excluded_count: cases whose every fix entry has merged_at == None

    Uses _extract_recurrence_cycles to walk fingerprint_to_fix_log. Cases whose
    all fix-log entries have merged_at == None are counted as in-flight and excluded
    from merged_count and all rate denominators.
    """
    if now is None:
        now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)
    retrip_window = timedelta(days=30)

    def _bucket_for(reason: str | None, raw_log: list) -> str:
        if (
            reason == ESCALATION_REASON_ESCALATED_CROSS_LAYER
            and _compliance_followup_fix_idx(raw_log) >= 0
        ):
            return _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP
        if reason in _META_ESCALATION_REASONS:
            return str(reason)
        return _ESCALATION_BUCKET_ALL_OTHER

    bucket_state: dict[str, dict[str, Any]] = {
        b: {
            "merged_count": 0,
            "retripped_within_30d_count": 0,
            "hold_days": [],
            "in_flight_excluded_count": 0,
            "prevention_prevented": 0,
            "prevention_recurred": 0,
        }
        for b in _ESCALATION_BUCKET_ORDER
    }

    for case in cases:
        raw_log = case.fingerprint_to_fix_log or []
        bucket = _bucket_for(getattr(case, "escalation_reason", None), raw_log)
        st = bucket_state[bucket]

        fix_entries = [
            e
            for e in raw_log
            if isinstance(e, dict) and e.get("fix_pr_number") is not None
        ]
        if fix_entries and all(e.get("merged_at") is None for e in fix_entries):
            st["in_flight_excluded_count"] += 1
            continue

        cycles = _extract_recurrence_cycles(case)
        # For compliance_followup cases, start from the cycle after the followup was spawned
        # so metrics reflect the compliance followup fix's durability, not the original non-compliant fix.
        if bucket == _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP:
            cf_idx = _compliance_followup_fix_idx(raw_log)
            effective_cycles = cycles[cf_idx:] if 0 <= cf_idx < len(cycles) else []
        else:
            effective_cycles = cycles
        if not effective_cycles:
            continue

        first = effective_cycles[0]
        merged_at = first["merged_at"]

        # Prevention rate: observe defenses with a complete 30d window (merged >= 30d ago).
        # This is independent of the merged_count lookback filter.
        if merged_at is not None and merged_at + retrip_window <= now:
            _retripped = any(
                c["merged_at"] is not None
                and c["merged_at"] <= merged_at + retrip_window
                for c in effective_cycles[1:]
            )
            if _retripped:
                st["prevention_recurred"] += 1
            else:
                st["prevention_prevented"] += 1

        if merged_at is None or not (cutoff <= merged_at <= now):
            continue

        st["merged_count"] += 1

        recovered_at = first.get("recovered_at")
        if isinstance(recovered_at, datetime):
            hold = (recovered_at - merged_at).total_seconds() / 86400.0
            if hold >= 0:
                st["hold_days"].append(hold)

        for later in effective_cycles[1:]:
            next_merged = later["merged_at"]
            if next_merged is not None and next_merged <= merged_at + retrip_window:
                st["retripped_within_30d_count"] += 1
                break

    result: dict[str, Any] = {}
    for bucket in _ESCALATION_BUCKET_ORDER:
        s = bucket_state[bucket]
        mc = s["merged_count"]
        rc = s["retripped_within_30d_count"]
        hold_list: list[float] = s["hold_days"]
        median_hold: float | None = None
        if hold_list:
            sorted_hold = sorted(hold_list)
            n = len(sorted_hold)
            mid = n // 2
            median_hold = (
                sorted_hold[mid]
                if n % 2 == 1
                else (sorted_hold[mid - 1] + sorted_hold[mid]) / 2.0
            )
        _prev = s["prevention_prevented"]
        _prec = s["prevention_recurred"]
        _pdenom = _prev + _prec
        result[bucket] = {
            "merged_count": mc,
            "retripped_within_30d_count": rc,
            "durable_resolution_rate": (1.0 - rc / mc) if mc > 0 else None,
            "median_hold_days": median_hold,
            "in_flight_excluded_count": s["in_flight_excluded_count"],
            "prevention_rate_30d": (_prev / _pdenom) if _pdenom > 0 else None,
            "recurrences_observed": _prec,
        }

    return {
        "by_reason": result,
        "lookback_days": lookback_days,
        "generated_at": now.isoformat(),
    }


def compute_avg_prevention_rate_7d(
    cases: list[RegressionRemediationCase],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Unweighted average prevention rate for defenses whose 30d window closed in the last 7d.

    A defense is eligible when its first cycle's merged_at falls in [now-37d, now-30d]
    (the 30d observation window opened and closed within the past 7 days).
    A defense "prevented" when no subsequent cycle occurred within 30d of that merge.

    Returns a dict with keys: avg_rate (float|None), eligible_count, prevented_count,
    recurred_count.
    """
    if now is None:
        now = _utcnow()
    window_open = now - timedelta(days=37)
    window_close = now - timedelta(days=30)
    retrip_window = timedelta(days=30)

    prevented = 0
    recurred = 0

    for case in cases:
        raw_log = case.fingerprint_to_fix_log or []
        fix_entries = [
            e
            for e in raw_log
            if isinstance(e, dict) and e.get("fix_pr_number") is not None
        ]
        if fix_entries and all(e.get("merged_at") is None for e in fix_entries):
            continue
        cycles = _extract_recurrence_cycles(case)
        if not cycles:
            continue
        first = cycles[0]
        merged_at = first["merged_at"]
        if merged_at is None:
            continue
        if not (window_open <= merged_at <= window_close):
            continue
        retripped = any(
            c["merged_at"] is not None and c["merged_at"] <= merged_at + retrip_window
            for c in cycles[1:]
        )
        if retripped:
            recurred += 1
        else:
            prevented += 1

    eligible = prevented + recurred
    return {
        "avg_rate": prevented / eligible if eligible > 0 else None,
        "eligible_count": eligible,
        "prevented_count": prevented,
        "recurred_count": recurred,
    }


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


def _build_layer_defense_ledger(
    all_fix_logs: list[dict[str, Any]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> str:
    """Build a RECENT DEFENSE ACTIVITY BY LAYER block for meta-investigation prompts.

    Aggregates fingerprint_to_fix_log entries from all RegressionRemediationCase rows
    for a project, filters to merged_at within lookback_days, and computes per-layer
    statistics. All six canonical layers always appear. Returns empty string when
    all_fix_logs is empty.
    """
    if not all_fix_logs:
        return ""

    stats = _compute_layer_fragility_stats(all_fix_logs, lookback_days)

    lines: list[str] = [
        "",
        f"RECENT DEFENSE ACTIVITY BY LAYER (last {lookback_days} days)",
    ]
    for layer in LAYER_DISPLAY_ORDER:
        s = stats[layer]
        if s["fix_count"] == 0:
            lines.append(
                f"  {layer}: no defenses added here in the last {lookback_days} days"
            )
        else:
            parts = [f"  {layer}: {s['fix_count']} defense(s) added"]
            avg_hold = s["avg_hold_days"]
            if avg_hold is not None:
                parts.append(f"avg hold {avg_hold:.1f}d before re-trip")
            if s["still_holding_count"] > 0:
                parts.append(f"{s['still_holding_count']} still holding")
            if s["held_gt_7d_count"] > 0:
                parts.append(f"{s['held_gt_7d_count']} held >7d")
            if s["retripped_within_48h_count"] > 0:
                parts.append(
                    f"{s['retripped_within_48h_count']} retripped <48h (fragile)"
                )
            lines.append("; ".join(parts))
    return "\n".join(lines)


def _layers_targeted_union(entries: list[dict[str, Any]] | None) -> set[str]:
    """Union of canonical ``layers_targeted`` across fix-log / cycle entries.

    Backwards-compatible name for callers (tests, admin_digest); the actual
    aggregation is owned by ``regression_layer_classifier.aggregate_targeted_layers``.
    Entries from ``_extract_recurrence_cycles`` carry an empty ``layers_targeted``
    list when ``layers_targeted_present`` is False, so the classifier's
    canonical-layer filter yields the same result as the prior dispatcher
    implementation.
    """
    return aggregate_targeted_layers(entries or [])


def _compute_recovery_layer_mismatch(
    fix_log: list[dict[str, Any]] | None,
    symptoms_by_layer: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Return a recovery_layer_mismatch entry when fix didn't cover all symptom layers.

    Compares the union of layers_targeted across fix-log entries against the
    layers that fired symptoms during the case lifecycle.  Returns None when
    there is no data to compare or when all symptom layers were addressed.
    """
    fix_layers = _layers_targeted_union(fix_log)
    symptom_layers = set((symptoms_by_layer or {}).keys())
    if not fix_layers or not symptom_layers:
        return None
    unaddressed = symptom_layers - fix_layers
    if not unaddressed:
        return None
    return {
        "type": "recovery_layer_mismatch",
        "evaluated_at": now.isoformat(),
        "fix_layers": sorted(fix_layers),
        "symptom_layers": sorted(symptom_layers),
        "unaddressed_layers": sorted(unaddressed),
    }


def compute_untargeted_layers(
    fingerprint_to_fix_log: list[dict[str, Any]] | None,
) -> list[str]:
    """Canonical layers never targeted by any prior fix in this case's history.

    Returns layers from ``LAYER_DISPLAY_ORDER`` (in canonical order)
    that do not appear in any entry's ``layers_targeted`` list. Unrecognized
    layer names in the fix-log are silently dropped so a typo or future-added
    layer never crashes the read path. Empty / missing / non-list fix-log
    yields the full canonical tuple — everything is untargeted by default.
    """
    if not isinstance(fingerprint_to_fix_log, list):
        return list(LAYER_DISPLAY_ORDER)
    targeted = _layers_targeted_union(fingerprint_to_fix_log)
    canonical = set(LAYER_DISPLAY_ORDER)
    targeted &= canonical
    return [layer for layer in LAYER_DISPLAY_ORDER if layer not in targeted]


def compute_class_layer_defense_counts(
    fix_logs: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Per-canonical-layer fix counts across a list of fix-log entries.

    Caller should pass the aggregated fix-log entries for all cases that share
    the same regression_class.  Returns one entry per canonical layer in
    canonical order (including layers with zero fixes) so the UI renders a
    stable six-row table.
    """
    counts: dict[str, int] = {layer: 0 for layer in LAYER_DISPLAY_ORDER}
    for entry in fix_logs or []:
        if not isinstance(entry, dict):
            continue
        for layer in entry.get("layers_targeted") or []:
            if layer in counts:
                counts[layer] += 1
    return counts


def _format_recurrence_meta_prompt(
    case: RegressionRemediationCase,
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    attempts = getattr(case, "_recurrence_attempts", []) or []
    recurrence_count = getattr(case, "_recurrence_count", len(attempts)) or len(
        attempts
    )
    held_text = getattr(case, "_recurrence_held_text", "unknown")
    obs_layers = list(getattr(case, "_recurrence_obs_layers", []) or [])

    def _format_attempt_layers(attempt: dict[str, Any]) -> str:
        if not attempt.get("layers_targeted_present"):
            return "layers: classification pending"
        layers = attempt.get("layers_targeted") or []
        if not layers:
            return "layers: none of the canonical layers"
        return f"layers: {', '.join(layers)}"

    prior_layers_union = _layers_targeted_union(attempts)
    obs_layers_set = set(obs_layers)
    mismatch = (
        bool(prior_layers_union)
        and bool(obs_layers_set)
        and prior_layers_union.isdisjoint(obs_layers_set)
    )

    lines: list[str] = []
    if mismatch:
        undefended = sorted(obs_layers_set - prior_layers_union)
        lines.extend(
            [
                "SYMPTOM-MISMATCH RECURRENCE",
                (
                    "Prior fixes targeted layers ["
                    f"{', '.join(sorted(prior_layers_union))}] but the current "
                    f"observation classifies as [{', '.join(sorted(obs_layers_set))}]."
                ),
                (f"Difference (un-defended layers): {', '.join(undefended)}."),
                (
                    "Start your layer reasoning from the un-defended layers above; "
                    "the previously-targeted layers have not prevented this re-trip."
                ),
                "",
            ]
        )
    lines.extend(
        [
            "RECURRENCE META-INVESTIGATION",
            (
                f"This regression class has recurred {recurrence_count} time(s) after "
                "prior remediation."
            ),
            f"Most recent prior fix held for {held_text} before the signal re-tripped.",
        ]
    )
    obs_text = ", ".join(obs_layers) if obs_layers else "classification pending"
    lines.append(f"Current observation layers: {obs_text}")
    lines.append("Prior remediation attempts:")
    if attempts:
        for idx, attempt in enumerate(attempts, start=1):
            task_id = attempt.get("task_id") or "unknown"
            pr_number = attempt.get("pr_number")
            merged_at = attempt.get("merged_at") or "unknown"
            recovered_at = attempt.get("recovered_at")
            held_for = attempt.get("held_for") or "unknown"
            parts = [
                f"- Attempt {idx}: task {task_id}",
                f"PR #{pr_number}" if pr_number is not None else "PR unknown",
                f"merged {merged_at}",
                f"held {held_for} before re-trip",
                _format_attempt_layers(attempt),
            ]
            if recovered_at is not None:
                parts.insert(3, f"recovered {recovered_at}")
            lines.append(" | ".join(parts))
    else:
        lines.append("- No prior remediation task metadata was found.")
    lines.extend(
        [
            "",
            "Enumerate ALL SIX canonical layers explicitly:",
            ", ".join(LAYER_DISPLAY_ORDER),
            (
                "For each layer answer: is it involved? if yes, what defense "
                "should we add? if no, why is it ruled out?"
            ),
        ]
    )
    lines.append("")
    lines.append(base_lines)
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _is_fragile_recurrence(
    case: RegressionRemediationCase,
    recurrence_count: int,
    now: datetime,
) -> bool:
    """Return True if the case is chronically fragile."""
    if recurrence_count >= _FRAGILE_RECURRENCE_MIN_COUNT:
        return True
    # Only completed cycles (recovered_at set) count as "held" — an in-progress
    # cycle has no confirmed hold duration yet.
    for cycle in _extract_recurrence_cycles(case):
        merged_at = cycle.get("merged_at")
        recovered_at = cycle.get("recovered_at")
        if merged_at is None or recovered_at is None:
            continue
        hold_days = (recovered_at - merged_at).total_seconds() / 86400
        if hold_days < _FRAGILE_RECURRENCE_SHORT_HOLD_DAYS:
            return True
    return False


def _format_fragile_recurrence_meta_prompt(
    case: RegressionRemediationCase,
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    """Build the fragile-recurrence cross-layer meta-investigation prompt."""
    attempts = getattr(case, "_recurrence_attempts", []) or []
    recurrence_count = getattr(case, "_recurrence_count", len(attempts)) or len(
        attempts
    )

    all_targeted = _layers_targeted_union(attempts) & set(LAYER_DISPLAY_ORDER)
    untargeted = compute_untargeted_layers(attempts)

    lines: list[str] = [
        "FRAGILE-RECURRENCE META-INVESTIGATION",
        (
            f"This regression class has re-tripped {recurrence_count} time(s) and is "
            "classified as chronically fragile (recurrence_count >= "
            f"{_FRAGILE_RECURRENCE_MIN_COUNT} OR a prior fix held < "
            f"{_FRAGILE_RECURRENCE_SHORT_HOLD_DAYS:.0f} days)."
        ),
        (
            "Single-layer fixes have not held. This meta-investigation must enumerate "
            "ALL canonical layers and propose defenses in AT LEAST TWO distinct layers, "
            "OR provide explicit written justification for why a single-layer defense is sufficient."
        ),
        "",
        f"Canonical defense layers: {', '.join(LAYER_DISPLAY_ORDER)}",
        (
            f"Layers already targeted by prior fixes: "
            f"{', '.join(sorted(all_targeted)) if all_targeted else 'none recorded'}"
        ),
        (
            f"Untargeted layers (start here): "
            f"{', '.join(untargeted) if untargeted else 'all layers have been targeted — look for cross-layer interactions'}"
        ),
        "",
        "For each untargeted layer answer: what defense should be added to prevent re-trip?",
        "For each targeted layer answer: why did the prior fix fail to hold?",
        "",
    ]
    lines.append(base_lines)
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _format_theme_layer_meta_prompt(
    base_lines: str, layer_defense_block: str = ""
) -> str:
    """Prepend the canonical layer-enumeration block for SR blind-spot theme regressions.

    Themes only fire after _SR_BLIND_SPOT_THEME_FLOOR (5+) occurrences, so the
    first spawn is already evidence that single-symptom fixes haven't resolved it.
    Force the full layer enumeration on every first dispatch rather than waiting
    for an explicit recurrence cycle.
    """
    lines = [
        "META-INVESTIGATION — SR BLIND-SPOT THEME",
        (
            "This theme has recurred across multiple tasks; single-symptom fixes "
            "have not extinguished the pattern."
        ),
        "",
        "Enumerate ALL SIX canonical layers explicitly:",
        ", ".join(LAYER_DISPLAY_ORDER),
        (
            "For each layer answer: is it involved? if yes, what defense "
            "should we add? if no, why is it ruled out?"
        ),
        "",
        base_lines,
    ]
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _format_bundle_signal_lines(signals: list[dict]) -> str:
    """Format a single investigate prompt covering a bundle of related signals."""
    lines = [
        f"Pipeline regression bundle: {len(signals)} related signal(s) tripped this cycle.",
        "Investigate the shared root cause across all signals below.\n",
    ]
    for s in signals:
        rc = s.get("regression_class", s["signal_name"])
        prior_str = (
            f"{s['prior_rate']:.1%}" if s.get("prior_rate") is not None else "N/A"
        )
        curr_str = (
            f"{s['current_rate']:.1%}" if s.get("current_rate") is not None else "N/A"
        )
        lines += [
            f"Signal: {s['signal_name']} / class={rc}",
            f"  Prior: {prior_str}  Current: {curr_str}  n={s.get('sample_size', 0)}",
        ]
        for extra in s.get("extras") or []:
            lines.append(f"  {extra}")
    lines.append(
        "\nDiagnose the shared root cause and produce the structured diagnosis "
        "JSON ({summary, root_cause, recommended_fix, severity, confidence, "
        "affected_files}). Do NOT prescribe a fix — let the investigate→fix "
        "chain produce the remediation."
    )
    return "\n".join(lines)


def _signal_higher_is_better(regression_class: str) -> bool:
    """Return True when a HIGHER ``current_rate`` is the recovery direction.

    Mirrors the ``tripped`` formulas in ``_current_signal_value_for_class``:
    sr_catch_rate trips on ``prior - curr`` (a hit-rate dropping is regression),
    and pipeline_success_rate is a success-percentage. All other classes
    (divergence, blind-spot miss-rate, anomaly counts, telemetry-stale age,
    crash recency) are lower-is-better.
    """
    if regression_class == "sr_catch_rate" or regression_class.startswith(
        "sr_catch_rate:"
    ):
        return True
    if regression_class == "pipeline_success_rate":
        return True
    return False


async def _current_signal_value_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None = None,
) -> dict[str, Any] | None:
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
    from app.models import Task as TaskModel

    # Bucket-level checks: tripped=True if any constituent signal is still tripped.
    if regression_class == "sr_catch_rate":
        categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
            "categories"
        ) or {}
        for cat in sorted(categories):
            rc = f"sr_catch_rate:{cat}"
            state = await _current_signal_value_for_class(
                session, project_id, rc, project_design_doc
            )
            if state is not None and state["tripped"]:
                return state
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": False,
            "extras": [],
        }

    if regression_class == "fix_loop_divergence":
        for loop in ("lint", "test", "check"):
            rc = f"fix_loop_divergence:{loop}"
            state = await _current_signal_value_for_class(session, project_id, rc)
            if state is not None and state["tripped"]:
                return state
        return {
            "signal_name": "fix_loop_divergence",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": False,
            "extras": [],
        }

    _sr_parts = regression_class.split(":", 2)
    if len(_sr_parts) == 3 and _sr_parts[0] == "sr_catch_rate":
        # Per-direction form: sr_catch_rate:{cat}:{direction_id}
        from app.playbook_learning import _MIN_DIRECTION_SR_SAMPLE

        _, cat, direction_id = _sr_parts
        trends = (project_design_doc or {}).get("sr_catch_rate_trends") or {}
        dir_entry = (trends.get("by_direction") or {}).get(direction_id) or {}
        dir_cat = (dir_entry.get("categories") or {}).get(cat) or {}
        direction_hit_rate = dir_cat.get("hit_rate")
        sample_count = dir_cat.get("sample_count", 0)
        if direction_hit_rate is None or sample_count < _MIN_DIRECTION_SR_SAMPLE:
            return None
        pw_cat = (trends.get("categories") or {}).get(cat) or {}
        baseline = pw_cat.get("previous_hit_rate") or pw_cat.get("current_hit_rate")
        if baseline is None:
            return None
        magnitude = round(baseline - direction_hit_rate, 3)
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": baseline,
            "current_rate": direction_hit_rate,
            "sample_size": sample_count,
            "magnitude": magnitude,
            "tripped": magnitude >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"Self-review catch-rate for category '{cat}' in direction '{direction_id}' is regressing."
            ],
        }

    if regression_class.startswith("sr_catch_rate:"):
        cat = regression_class.split(":", 1)[1]
        data = (
            ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
                "categories"
            )
            or {}
        ).get(cat) or {}
        prior = data.get("previous_hit_rate")
        curr = data.get("current_hit_rate")
        raw_sample = data.get("current_human_count")
        if prior is None or curr is None:
            return None
        sample_size = int(raw_sample) if raw_sample is not None else _MIN_SR_SAMPLE
        if raw_sample is not None and sample_size < _MIN_SR_SAMPLE:
            return None
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": prior,
            "current_rate": curr,
            "sample_size": sample_size,
            "magnitude": round(prior - curr, 3),
            "tripped": (prior - curr) >= _TREND_DELTA_THRESHOLD,
            "extras": [f"Self-review catch-rate for category '{cat}' is regressing."],
        }

    if regression_class.startswith("sr_false_positive:"):
        cat = regression_class.split(":", 1)[1]
        data = (
            ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
                "categories"
            )
            or {}
        ).get(cat) or {}
        curr_fp_rate = data.get("false_positive_rate")
        prev_fp_rate = data.get("previous_fp_rate")
        sample_size = int(data.get("current_human_count") or 0)
        if curr_fp_rate is None or prev_fp_rate is None:
            return None
        if sample_size < _SR_FP_MIN_SAMPLE:
            return None
        tripped = (
            curr_fp_rate >= _SR_FP_RATE_FLOOR and prev_fp_rate >= _SR_FP_RATE_FLOOR
        )
        return {
            "signal_name": "sr_false_positive",
            "prior_rate": prev_fp_rate,
            "current_rate": curr_fp_rate,
            "sample_size": sample_size,
            "magnitude": round(curr_fp_rate - prev_fp_rate, 3),
            "tripped": tripped,
            "extras": [
                f"SR false-positive rate for category '{cat}' is {curr_fp_rate:.1%} "
                f"(prior window: {prev_fp_rate:.1%}) — exceeds floor of {_SR_FP_RATE_FLOOR:.0%} "
                "in both windows."
            ],
        }

    if regression_class.startswith("fix_loop_divergence:"):
        loop = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                        TaskModel.created_at >= now - timedelta(days=14),
                    )
                )
            )
            .scalars()
            .all()
        )

        def _naive(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        c7 = _naive(now - timedelta(days=7))
        c14 = _naive(now - timedelta(days=14))
        stats = {"curr": {"n": 0, "d": 0}, "prev": {"n": 0, "d": 0}}
        for start, end, bucket in (
            (c7, datetime(9999, 12, 31), "curr"),
            (c14, c7, "prev"),
        ):
            for t in tasks:
                ts = _naive(t.created_at) if t.created_at else None
                if ts is None or not (start <= ts < end):
                    continue
                bucket_hist = (t.fix_loop_history or {}).get(loop) or []
                if not bucket_hist:
                    continue
                last_exit: str | None = None
                for ev in reversed(bucket_hist):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None and isinstance(bucket_hist[-1], dict):
                    last_exit = bucket_hist[-1].get("exit_reason")
                stats[bucket]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    stats[bucket]["d"] += 1
        cn, cd = stats["curr"]["n"], stats["curr"]["d"]
        pn, pd = stats["prev"]["n"], stats["prev"]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            return None
        curr_rate = cd / cn
        prior_rate = pd / pn
        return {
            "signal_name": "fix_loop_divergence",
            "prior_rate": round(prior_rate, 3),
            "current_rate": round(curr_rate, 3),
            "sample_size": cn,
            "magnitude": round(curr_rate - prior_rate, 3),
            "tripped": (curr_rate - prior_rate) >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"Fix-loop '{loop}' divergence rate is rising: {pd}/{pn} → {cd}/{cn} divergent."
            ],
        }

    if regression_class.startswith("sr_blind_spot:theme:"):
        from app.sr_blind_spots import aggregate_sr_blind_spot_themes

        theme = regression_class.split(":", 2)[2]
        themes = await aggregate_sr_blind_spot_themes(
            session, project_id, days=14, limit=100
        )
        count = next((e["count"] for e in themes if e["theme"] == theme), 0)
        return {
            "signal_name": "sr_blind_spot",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": count,
            "magnitude": float(count),
            "tripped": count >= _SR_BLIND_SPOT_THEME_FLOOR,
            "extras": [
                f"SR blind-spot theme '{theme}' appeared {count}x in the last 14 days."
            ],
        }

    if regression_class.startswith("sr_blind_spot:"):
        from app.sr_blind_spots import _count_reviewer_disagreement_directions
        from app.models import Task as TaskModel

        cat = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.status == TaskStatus.COMPLETED,
                        TaskModel.created_at >= now - timedelta(days=7),
                    )
                )
            )
            .scalars()
            .all()
        )

        def _naive(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        dirs = _count_reviewer_disagreement_directions(tasks)
        c_rate, c_denom = None, 0
        for (k_cat, _k_type), bucket in dirs.items():
            if k_cat != cat:
                continue
            denom = bucket["miss"] + bucket["tp"] + bucket["fp"]
            if denom < _MIN_SR_SAMPLE:
                continue
            rate = bucket["miss"] / denom
            if c_denom == 0 or denom > c_denom:
                c_rate = round(rate, 3)
                c_denom = denom
        if c_denom < _MIN_SR_SAMPLE:
            return None
        return {
            "signal_name": "sr_blind_spot",
            "prior_rate": None,
            "current_rate": c_rate,
            "sample_size": c_denom,
            "magnitude": c_rate,
            "tripped": (c_rate or 0.0) >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"SR blind-spot for category '{cat}' (reviewer-disagreement rate in current 7d window)."
            ],
        }

    if regression_class == "pipeline_success_rate":
        from app.models import Task as TaskModel
        from app.routers.admin_digest import compute_pipeline_health_summary

        summary = await compute_pipeline_health_summary(
            session, project_id, lookback_hours=24
        )
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        rows = (
            await session.execute(
                select(TaskModel.id, TaskModel.status, TaskModel.updated_at).where(
                    TaskModel.project_id == project_id,
                    TaskModel.updated_at >= window_start,
                    TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                )
            )
        ).all()
        sample_size = len(rows)
        if sample_size < _PIPELINE_SUCCESS_MIN_SAMPLE:
            return None
        failure_distribution = summary.get("failure_distribution") or {}
        extras = ["24h pipeline success-rate is declining."]
        if failure_distribution:
            top = ", ".join(
                f"{k}={v}" for k, v in list(failure_distribution.items())[:3]
            )
            extras.append(f"Top failure classes: {top}")
        return {
            "signal_name": "pipeline_success_rate",
            "prior_rate": None,
            "current_rate": summary.get("success_rate"),
            "sample_size": sample_size,
            "magnitude": None,
            "tripped": summary.get("trend") == "declining",
            "extras": extras,
        }

    if regression_class.startswith("anomaly:"):
        anomaly_key = regression_class[len("anomaly:") :]
        import app.shared_state as _ss
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

        entry = (_ss.persistent_anomalies.get(str(project_id)) or {}).get(anomaly_key)
        if entry is None:
            # Durable fallback: project.design_doc survives pod restarts.
            entry = (
                (project_design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY) or {}
            ).get(anomaly_key)
        if not isinstance(entry, dict):
            return {
                "signal_name": "persistent_anomaly",
                "prior_rate": None,
                "current_rate": None,
                "sample_size": 0,
                "magnitude": None,
                "tripped": False,
                "extras": [],
            }
        n = int(entry.get("occurrence_count") or 0)
        last_seen = str(entry.get("last_seen") or "")
        cutoff = (
            _utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
        ).isoformat()
        tripped = n >= _ANOMALY_DISPATCH_MIN_OCCURRENCES and last_seen >= cutoff
        return {
            "signal_name": "persistent_anomaly",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": n,
            "magnitude": float(n),
            "tripped": tripped,
            "extras": [f"Persistent anomaly '{anomaly_key}' seen {n}x."],
        }

    if regression_class.startswith("telemetry_stale:"):
        signal = regression_class[len("telemetry_stale:") :]
        doc = project_design_doc or {}
        now = _utcnow()
        raw_ts, threshold = _get_staleness_raw_ts(signal, doc, project_id)
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None:
            return None
        age = now - computed_at
        return {
            "signal_name": "telemetry_stale",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": round(age.total_seconds() / 3600, 2),
            "tripped": age > threshold,
            "extras": [
                f"Signal '{signal}' last computed {round(age.total_seconds() / 3600, 1)}h ago."
            ],
        }

    if regression_class.startswith("background_service_crash:"):
        service = regression_class[len("background_service_crash:") :]
        from app.models import Task as TaskModel

        now = _utcnow()
        # Considered still crashing if a crash investigation task was spawned
        # for this service within the last 2 hours (lifecycle._spawn_crash_investigations
        # writes spawned_by="crash_investigation" and service_name on each task).
        cutoff = now - timedelta(hours=2)
        recent_tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.created_at >= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        tripped = any(
            (t.context or {}).get("spawned_by") == "crash_investigation"
            and (t.context or {}).get("service_name") == service
            for t in recent_tasks
        )
        status_str = "still crashing" if tripped else "appears stable"
        return {
            "signal_name": "background_service_crash",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": tripped,
            "extras": [f"Background service '{service}' {status_str}."],
        }

    return None


def _parse_signal_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _collect_fix_loop_divergence_fingerprints(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from app.failure_classification import _fingerprint_message
    from app.fix_loop_telemetry import DIVERGENCE_EXIT_REASONS
    from app.models import Task as TaskModel

    now = _utcnow()
    cutoff = now - timedelta(days=_FIX_LOOP_DIVERGENCE_FINGERPRINT_WINDOW_DAYS)
    rows = (
        (
            await session.execute(
                select(TaskModel)
                .where(
                    TaskModel.project_id == project_id,
                    TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    TaskModel.updated_at >= cutoff,
                )
                .order_by(TaskModel.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[str, dict[str, Any]] = {}
    for task in rows:
        ctx = task.context or {}
        files = [
            str(f) for f in (ctx.get("related_files") or []) if isinstance(f, str) and f
        ][:10]
        scope = str(ctx.get("scope") or "unknown")
        for bucket in (task.fix_loop_history or {}).values():
            for ev in bucket or []:
                if (
                    not isinstance(ev, dict)
                    or ev.get("exit_reason") not in DIVERGENCE_EXIT_REASONS
                ):
                    continue
                ts = (
                    _parse_signal_timestamp(ev.get("ended_at") or ev.get("started_at"))
                    or task.updated_at
                )
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts < cutoff:
                    continue
                fingerprint = _fingerprint_message(str(ev.get("error_tail") or ""))
                if not fingerprint:
                    continue
                item = grouped.setdefault(
                    fingerprint,
                    {
                        "tasks": set(),
                        "samples": [],
                        "files": set(),
                        "scopes": set(),
                    },
                )
                item["tasks"].add(str(task.id))
                item["files"].update(files)
                item["scopes"].add(scope)
                if ev.get("error_tail") and len(item["samples"]) < 2:
                    item["samples"].append(str(ev.get("error_tail"))[:240])
    trips: list[dict[str, Any]] = []
    for fingerprint, item in grouped.items():
        count = len(item["tasks"])
        if count < _FIX_LOOP_DIVERGENCE_FINGERPRINT_MIN_TASKS:
            continue
        short_fingerprint = (
            "".join(
                ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in fingerprint
            )[:80].strip("-")
            or "unknown"
        )
        trips.append(
            {
                "fingerprint": fingerprint,
                "short_fingerprint": short_fingerprint,
                "count": count,
                "samples": item["samples"],
                "files": sorted(item["files"]),
                "scopes": sorted(item["scopes"]),
            }
        )
    return sorted(trips, key=lambda item: (-item["count"], item["fingerprint"]))


def _extract_failure_type(msg: str) -> str:
    """Extract failure type from viewport test error message.

    Returns 'overflow' for expectNoOverflowAt, 'text_truncation' for
    expectNoTextTruncationAt, 'touch_target' for expectMinTouchTargetAt,
    or 'unknown' if none is found.
    """
    if "expectNoOverflowAt" in msg:
        return "overflow"
    if "expectNoTextTruncationAt" in msg:
        return "text_truncation"
    if "expectMinTouchTargetAt" in msg:
        return "touch_target"
    return "unknown"


async def _collect_viewport_failures(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Collect recent VIEWPORT_REGRESSION task failures grouped by (component, width).

    Calls classify_failure() to filter for viewport-test errors, then extracts
    the component name (from .viewport.test.tsx file references in the message
    or task title) and the viewport width (from expectNoOverflowAt/expectMinTouchTargetAt
    error strings). Falls back to "unknown" / "0" when extraction fails.
    Also classifies each failure as 'overflow', 'touch_target', or 'mixed'.
    """
    import re
    from app.failure_classification import VIEWPORT_REGRESSION, classify_failure
    from app.models import Task as TaskModel

    now = _utcnow()
    cutoff = now - timedelta(days=_VIEWPORT_REGRESSION_WINDOW_DAYS)
    rows = (
        (
            await session.execute(
                select(TaskModel)
                .where(
                    TaskModel.project_id == project_id,
                    TaskModel.status == TaskStatus.FAILED,
                    TaskModel.updated_at >= cutoff,
                )
                .order_by(TaskModel.updated_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    _width_re = re.compile(
        r"expect(?:NoOverflow|NoTextTruncation|MinTouchTarget)At\((\d+)\)",
        re.IGNORECASE,
    )
    _component_re = re.compile(r"(\w+)\.viewport\.test\.tsx", re.IGNORECASE)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for task in rows:
        if classify_failure(task) != VIEWPORT_REGRESSION:
            continue
        msg = task.latest_status_message or ""
        combined = msg + " " + (task.title or "")

        width_m = _width_re.search(msg)
        width = width_m.group(1) if width_m else "0"

        comp_m = _component_re.search(combined)
        component = comp_m.group(1) if comp_m else "unknown"

        key = (component, width)
        item = grouped.setdefault(
            key,
            {
                "tasks": set(),
                "samples": [],
                "failure_types": set(),
                "earliest_task": None,
            },
        )
        item["tasks"].add(str(task.id))
        item["failure_types"].add(_extract_failure_type(msg))
        if msg and len(item["samples"]) < 2:
            item["samples"].append(msg[:240])
        # Track the earliest failing task for introducing-PR attribution.
        if (
            item["earliest_task"] is None
            or task.updated_at < item["earliest_task"].updated_at
        ):
            item["earliest_task"] = task

    trips: list[dict[str, Any]] = []
    for (component, width), item in grouped.items():
        count = len(item["tasks"])
        if count < _VIEWPORT_REGRESSION_MIN_FAILURES:
            continue
        # Determine failure_type: 'overflow', 'touch_target', or 'mixed'
        types = item["failure_types"] - {"unknown"}
        if not types:
            failure_type = "unknown"
        elif len(types) == 1:
            failure_type = types.pop()
        else:
            failure_type = "mixed"
        # Derive introducing-PR / introducing-commit from the earliest failing task.
        earliest_task = item.get("earliest_task")
        introducing_pr_number: int | None = None
        introducing_commit_sha: str | None = None
        if earliest_task is not None:
            pr_num = earliest_task.first_payload.get("pr_number")
            if pr_num is not None:
                try:
                    introducing_pr_number = int(pr_num)
                except (TypeError, ValueError):
                    pass
            if introducing_pr_number is None:
                head_sha = (earliest_task.context or {}).get(
                    "head_sha"
                ) or earliest_task.first_payload.get("head_sha")
                if head_sha:
                    introducing_commit_sha = str(head_sha)
        trips.append(
            {
                "component": component,
                "width": width,
                "count": count,
                "samples": item["samples"],
                "failure_type": failure_type,
                "introducing_pr_number": introducing_pr_number,
                "introducing_commit_sha": introducing_commit_sha,
            }
        )
    return sorted(trips, key=lambda x: (-x["count"], x["component"], x["width"]))


# ---------------------------------------------------------------------------
# Cross-layer regression detection
# ---------------------------------------------------------------------------

# Bound and trim parameters for the per-case symptoms_by_layer log. Bounded
# entries-per-layer prevents unbounded JSONB growth; the 48h window matches
# the anomaly recency horizon in _ANOMALY_DISPATCH_RECENCY_HOURS.
_LAYER_SYMPTOM_BOUND = 10
_LAYER_SYMPTOM_WINDOW = timedelta(hours=48)


def _derive_observation_for_layer_classify(
    *,
    signal_name: str,
    regression_class: str,
    extras: list[str] | None,
    extra_snapshot_fields: dict | None,
) -> tuple[str, str, str]:
    """Derive (failure_class, message, stage) inputs for ``classify_layers``.

    Maps the dispatcher's signal vocabulary onto the layer classifier's
    fingerprint inputs without coupling either side to the other.
    """
    extras = extras or []
    extra_snapshot_fields = extra_snapshot_fields or {}

    if regression_class.startswith("anomaly:"):
        anomaly_key = regression_class.split(":", 1)[1]
        first_token = anomaly_key.split("_", 1)[0]
        failure_class = first_token if first_token.isupper() else anomaly_key
    else:
        failure_class = signal_name.upper()

    stage = ""
    if regression_class.startswith("fix_loop_divergence:"):
        stage = regression_class.split(":", 1)[1]
    elif regression_class.startswith("fix_loop_divergence_fingerprint"):
        stage = "lint"
    elif regression_class.startswith("background_service_crash"):
        stage = "provision"
    elif regression_class.startswith("viewport_regression:"):
        stage = "viewport"
    elif signal_name in ("sr_catch_rate", "sr_blind_spot", "sr_false_positive"):
        stage = "review"
    elif signal_name == "pipeline_success_rate":
        stage = ""

    parts: list[str] = list(extras)
    sample_msg = extra_snapshot_fields.get("sample_message")
    if sample_msg:
        parts.append(str(sample_msg))
    latest = extra_snapshot_fields.get("latest_details")
    if latest:
        parts.append(str(latest))
    message = "\n".join(p for p in parts if p)
    return failure_class, message, stage


def _record_layer_observation(
    symptoms_by_layer: dict[str, Any] | None,
    layers: list[str],
    *,
    fingerprint: str,
    sample: str,
    now: datetime,
) -> dict[str, Any]:
    """Idempotently append a layer observation; return the new dict.

    - Per-layer entries are bounded to ``_LAYER_SYMPTOM_BOUND`` (most-recent kept).
    - Entries older than ``_LAYER_SYMPTOM_WINDOW`` are trimmed on write.
    - If ``fingerprint`` already exists for a layer, ``observed_at`` is refreshed
      and the entry is moved to the tail rather than duplicated, so repeated
      identical fingerprints don't grow the list unboundedly.
    """
    cutoff = now - _LAYER_SYMPTOM_WINDOW
    base: dict[str, Any] = dict(symptoms_by_layer or {})
    sample_short = (sample or "")[:500]
    observed_at_iso = now.isoformat()

    for layer in layers:
        existing = list(base.get(layer) or [])
        # Trim entries with parseable timestamps older than the window
        trimmed: list[dict[str, Any]] = []
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            obs_raw = entry.get("observed_at")
            keep = True
            if isinstance(obs_raw, str) and obs_raw:
                try:
                    obs_dt = datetime.fromisoformat(obs_raw.replace("Z", "+00:00"))
                    if obs_dt.tzinfo is not None:
                        obs_dt = obs_dt.replace(tzinfo=None)
                    if obs_dt < cutoff:
                        keep = False
                except ValueError:
                    pass
            if keep:
                trimmed.append(entry)

        # Idempotent merge by fingerprint
        merged: list[dict[str, Any]] = []
        replaced = False
        for entry in trimmed:
            if entry.get("fingerprint") == fingerprint:
                replaced = True
                continue  # we'll re-append the refreshed entry at the tail
            merged.append(entry)
        merged.append(
            {
                "fingerprint": fingerprint,
                "observed_at": observed_at_iso,
                "sample": sample_short,
            }
        )
        if not replaced and len(merged) > _LAYER_SYMPTOM_BOUND:
            merged = merged[-_LAYER_SYMPTOM_BOUND:]
        base[layer] = merged
    return base


def _distinct_recent_layers(
    symptoms_by_layer: dict[str, Any] | None,
    *,
    now: datetime,
    window: timedelta = _LAYER_SYMPTOM_WINDOW,
) -> list[str]:
    """Return layer names with at least one observation inside ``window``."""
    if not symptoms_by_layer:
        return []
    cutoff = now - window
    out: list[str] = []
    for layer, entries in symptoms_by_layer.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            obs_raw = entry.get("observed_at")
            try:
                obs_dt = datetime.fromisoformat(str(obs_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if obs_dt.tzinfo is not None:
                obs_dt = obs_dt.replace(tzinfo=None)
            if obs_dt >= cutoff:
                out.append(layer)
                break
    return sorted(out)


def _representative_per_layer(
    symptoms_by_layer: dict[str, Any] | None,
    layers: list[str],
    *,
    now: datetime,
    window: timedelta = _LAYER_SYMPTOM_WINDOW,
) -> list[dict[str, Any]]:
    """Pick the most-recent in-window entry per layer for the prompt."""
    if not symptoms_by_layer:
        return []
    cutoff = now - window
    chosen: list[dict[str, Any]] = []
    for layer in layers:
        entries = symptoms_by_layer.get(layer) or []
        latest: tuple[datetime, dict[str, Any]] | None = None
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
            if obs_dt < cutoff:
                continue
            if latest is None or obs_dt > latest[0]:
                latest = (obs_dt, entry)
        if latest is not None:
            chosen.append(
                {
                    "layer": layer,
                    "fingerprint": str(latest[1].get("fingerprint") or ""),
                    "sample": str(latest[1].get("sample") or "")[:500],
                    "observed_at": latest[1].get("observed_at"),
                }
            )
    return chosen


def _format_cross_layer_prompt(
    *,
    signal_name: str,
    regression_class: str,
    layers_evidence: list[dict[str, Any]],
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    """Build the cross-layer investigate prompt enumerating per-layer evidence."""
    layer_count = len(layers_evidence)
    layer_names = ", ".join(e["layer"] for e in layers_evidence)
    header = [
        f"CROSS-LAYER REGRESSION DETECTED ({signal_name} / class={regression_class}).",
        (
            f"This regression class has surfaced on {layer_count} distinct "
            f"infrastructure layer(s) within the last 48h: {layer_names}."
        ),
        (
            "Diagnose the SHARED root cause and propose defenses in AT LEAST TWO distinct "
            "infrastructure layers, OR provide explicit written justification for why a "
            "single-layer defense is sufficient. Do NOT bias toward whichever layer "
            "surfaced most recently. Each layer below shows one representative fingerprint "
            "+ sample from this window."
        ),
        "",
        "PER-LAYER EVIDENCE",
    ]
    for ev in layers_evidence:
        header.append(f"\nLayer: {ev['layer']}")
        if ev.get("fingerprint"):
            header.append(f"  Fingerprint: {ev['fingerprint']}")
        sample = ev.get("sample") or ""
        if sample:
            header.append(f"  Sample: {sample[:300]}")
    header.append(
        "\nNote: .github/workflows/ files cannot be modified by the agent. "
        "If the recommended fix would touch workflow YAML, treat it as a "
        "finding/note in the diagnosis report rather than a direct edit."
    )
    header.append("")
    header.append(base_lines)
    if layer_defense_block:
        header.append(layer_defense_block)
    return "\n".join(header)


def _format_layer_concentration_prompt(
    *,
    layer: str,
    sample_case_ids: list[str],
    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]],
    layer_defense_block: str = "",
) -> str:
    """Build the layer-concentration meta-investigation prompt."""
    lines = [
        f"LAYER-CONCENTRATION META-INVESTIGATION (layer_concentration:{layer})",
        (
            f"The {layer} infrastructure layer is the recurrent failure mode across "
            "multiple distinct regression classes. This is a cross-case meta-signal, "
            "not a single-case regression — the pattern spans independent regression "
            f"cases, each receiving fixes that target {layer}."
        ),
        "",
        f"CONTRIBUTING CASES (up to 5 most recent for {layer})",
    ]

    for case_id in sample_case_ids:
        case_entries = [
            e
            for cid, e in all_fix_logs_with_case_ids
            if cid == case_id and layer in (e.get("layers_targeted") or [])
        ]
        lines.append(f"\n  Case {case_id}:")
        if not case_entries:
            lines.append("    (no matching fix-log entries)")
            continue
        for e in case_entries[:3]:
            merged = e.get("merged_at") or "unknown"
            recovered = e.get("recovered_at")
            merged_short = merged[:10] if len(merged) >= 10 else merged
            if recovered:
                recovered_short = recovered[:10] if len(recovered) >= 10 else recovered
                merged_dt = _parse_dispatcher_dt(merged)
                recovered_dt = _parse_dispatcher_dt(recovered)
                if merged_dt and recovered_dt:
                    hold_days = (recovered_dt - merged_dt).total_seconds() / 86400.0
                    lines.append(
                        f"    merged={merged_short} recovered={recovered_short} "
                        f"hold={hold_days:.1f}d (re-tripped)"
                    )
                else:
                    lines.append(
                        f"    merged={merged_short} recovered={recovered_short}"
                    )
            else:
                merged_dt = _parse_dispatcher_dt(merged)
                if merged_dt:
                    age_days = (_utcnow() - merged_dt).total_seconds() / 86400.0
                    lines.append(
                        f"    merged={merged_short} still holding (age={age_days:.1f}d)"
                    )
                else:
                    lines.append(f"    merged={merged_short} still holding")

    lines += [
        "",
        "STRUCTURAL QUESTIONS TO INVESTIGATE",
        (
            f"  1. Is the {layer} layer's defense pattern structurally insufficient "
            "(architectural / categorical), rather than individual fixes being wrong? "
            "If every fix targets this layer but the regression recurs, the layer may "
            "have a systemic gap that point-fixes cannot close."
        ),
        (
            f"  2. Propose defenses in AT LEAST ONE OTHER layer to compensate for "
            f"{layer}'s fragility, OR provide written justification for why {layer}-only "
            "defense is sufficient. Defense-in-depth strategies (pre-flight checks, gate "
            "policies, observation thresholds in adjacent layers) may reduce blast radius "
            "when this layer fails."
        ),
        "",
        "Produce the structured diagnosis JSON "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
        "",
        "Note: .github/workflows/ files cannot be modified by the agent. "
        "If the recommended fix would touch workflow YAML, treat it as a "
        "finding/note in the diagnosis report rather than a direct edit.",
    ]

    if layer_defense_block:
        lines.append("")
        lines.append(layer_defense_block)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path-based layer classification (for fix-log layers_targeted backfill)
# ---------------------------------------------------------------------------

# Bound for how many cases to backfill per dispatcher tick. The recovery scan
# runs every 60s and each call hits the GitHub API once per case; capping
# prevents a multi-hundred-case backlog from saturating the API budget.
_LAYER_BACKFILL_PER_TICK = 5

# Cap for the per-entry ``unmatched_paths`` list written by
# ``_backfill_layers_targeted_once`` when classification yields ``[]``. Bounds
# the JSON column on noisy PRs so a single 200-file refactor cannot bloat the
# row.
_UNMATCHED_PATHS_PER_ENTRY_CAP = 20

# Path-classifier coverage signal thresholds. The signal trips when the share
# of recently-merged fix-log entries that classified to ``[]`` exceeds the
# rate over a minimum sample. Both are module-level so tests can monkeypatch.
_PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE = 5
_PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD = 0.20
# Top-N most-frequent unmatched paths to surface in the alert message.
_PATH_CLASSIFIER_COVERAGE_TOP_PATHS = 10
# Stable Alert title used for dedup so a recurring stale-classifier condition
# does not pile up duplicate alerts each cycle.
_PATH_CLASSIFIER_COVERAGE_ALERT_TITLE = "Path-layer classifier coverage stale"

# Awaiting-backfill backlog alert thresholds. Cross-layer compliance buckets
# fix-log entries with spawn_was_cross_layer=True and merged_at set but no
# layers_targeted into ``awaiting_backfill``. The 24h grace inside
# ``compute_cross_layer_compliance`` already separates transient (recent
# merges, backfill not yet retried) from systemic backlog. The thresholds
# below trip the operator alert when EITHER signal indicates the backlog has
# stopped draining: 5 entries past the 24h grace, OR an oldest entry older
# than 3 days. Both point at the same likely causes — GitHub auth failures in
# ``_backfill_layers_targeted_once`` (which logs and continues silently) or
# path-classifier coverage gaps that yield empty layer assignments.
_AWAITING_BACKFILL_COUNT_THRESHOLD = 5
_AWAITING_BACKFILL_AGE_THRESHOLD = timedelta(days=3)
# Window mirrors the GET /regression-cases/cross-layer-compliance default.
_AWAITING_BACKFILL_WINDOW = timedelta(days=30)
_AWAITING_BACKFILL_ALERT_TITLE = "Cross-layer awaiting_backfill backlog stuck"


def _classify_layers_from_paths(paths: list[str]) -> list[str]:
    """Map a list of changed file paths to canonical infrastructure-layer tags.

    Returns a sorted, deduplicated subset of ``ALL_LAYERS`` from
    ``regression_layer_classifier``. Paths that don't match any rule are
    silently dropped — empty result is meaningful ("none of the canonical
    layers were touched") and is preserved as ``[]`` for the caller.

    Rules align with the pattern matcher in ``regression_layer_classifier``
    so message-based and path-based classifications use the same vocabulary:
      * ``.github/workflows/**`` → CI_WORKFLOW
      * ``infra/packer/**``, ``setup.sh``, ``scripts/build*`` → AGENT_VM
      * ``agent/src/handlers/**``, ``agent/src/index.ts`` → AGENT_VERB
      * ``backend/app/review_pipeline.py``, ``backend/app/auto_review.py``,
        ``agent/src/handlers/review-fix.*``, ``agent/src/prompts/self-review.ts``,
        ``.claude/skills/review*``, ``.claude/skills/security-review*`` → REVIEWER_PASS
      * ``backend/testing/module_size_limits.json``, lint configs
        (``eslint.config.js``, ``ruff.toml``, ``pyproject.toml``),
        ``backend/app/source_ref_validation.py``, ``check`` script,
        ``backend/app/fix_loop_telemetry.py`` → BASELINE_GATE
    """
    from app.regression_layer_classifier import (  # noqa: PLC0415
        LAYER_AGENT_VERB,
        LAYER_AGENT_VM,
        LAYER_BASELINE_GATE,
        LAYER_CI_WORKFLOW,
        LAYER_REVIEWER_PASS,
        LAYER_VIEWPORT,
    )

    tags: set[str] = set()
    for raw in paths or []:
        if not raw or not isinstance(raw, str):
            continue
        # Strip a leading "./" prefix only — must NOT use lstrip("./") because
        # that would also chop the leading "." off paths like ".github/...".
        p = raw.strip()
        if p.startswith("./"):
            p = p[2:]

        # CI_WORKFLOW
        if p.startswith(".github/workflows/"):
            tags.add(LAYER_CI_WORKFLOW)

        # AGENT_VM — VM image, system-level provisioning
        if (
            p.startswith("infra/packer/")
            or p.startswith("infra/terraform/")
            or p == "setup.sh"
            or p.startswith("scripts/build")
            or p == "Dockerfile"
            or p.endswith("/Dockerfile")
        ):
            tags.add(LAYER_AGENT_VM)

        # REVIEWER_PASS — checked BEFORE AGENT_VERB so review-fix handlers
        # in agent/src/handlers/ classify as REVIEWER_PASS, not AGENT_VERB.
        is_reviewer = (
            p == "backend/app/review_pipeline.py"
            or p == "backend/app/auto_review.py"
            or p.startswith("backend/app/sr_")
            or p == "agent/src/prompts/self-review.ts"
            or p.startswith("agent/src/handlers/review-fix")
            or ".claude/skills/review" in p
            or ".claude/skills/security-review" in p
        )
        if is_reviewer:
            tags.add(LAYER_REVIEWER_PASS)

        # AGENT_VERB — handler invocation / verb dispatch / scope check
        if not is_reviewer and (
            p.startswith("agent/src/handlers/")
            or p == "agent/src/index.ts"
            or p == "agent/src/prompt.ts"
            or p.startswith("agent/src/prompts/")
        ):
            tags.add(LAYER_AGENT_VERB)

        # BASELINE_GATE — lint/scope/size/baseline configs and gate code
        if (
            p == "backend/testing/module_size_limits.json"
            or p == "eslint.config.js"
            or p.endswith("/eslint.config.js")
            or p == "admin-ui/eslint.config.js"
            or p == "ruff.toml"
            or p == "pyproject.toml"
            or p == "backend/app/source_ref_validation.py"
            or p == "backend/app/fix_loop_telemetry.py"
            or p == "check"
        ):
            tags.add(LAYER_BASELINE_GATE)

        # VIEWPORT — viewport test infrastructure and check script
        if (
            p == "check-viewport.sh"
            or p == "admin-ui/src/test/viewport.ts"
            or (p.startswith("admin-ui/src/") and p.endswith(".viewport.test.tsx"))
        ):
            tags.add(LAYER_VIEWPORT)

    return sorted(tags)


async def _backfill_layers_targeted_once(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Populate ``layers_targeted`` on fix-log entries that lack it.

    Idempotent backfill helper invoked from ``run_recovery_once``. Iterates
    cases for ``project_id`` whose ``fingerprint_to_fix_log`` contains entries
    with ``fix_pr_number`` set but no ``layers_targeted`` populated. For each
    such entry, fetches the PR's changed files via the existing GitHub helper
    and writes ``layers_targeted`` (a list, possibly empty) back onto the entry.

    Returns the number of entries mutated. Bounded to
    ``_LAYER_BACKFILL_PER_TICK`` cases per call so backlog cannot starve the
    rest of the dispatcher tick. GitHub auth / API failures are logged and
    skipped — never raised — so a transient outage never breaks recovery.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.github import get_pr_files, get_repo_installation_token  # noqa: PLC0415
    from app.models import Project  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None or not project.github_owner or not project.github_repo:
        return 0

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

    # Find cases with at least one entry missing layers_targeted.
    pending: list[RegressionRemediationCase] = []
    for case in rows:
        log = case.fingerprint_to_fix_log or []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            if entry.get("fix_pr_number") and "layers_targeted" not in entry:
                pending.append(case)
                break
        if len(pending) >= _LAYER_BACKFILL_PER_TICK:
            break

    if not pending:
        return 0

    token: str | None = None
    try:
        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] layers backfill skipped — token fetch failed for "
            "project=%s: %s",
            project_id,
            exc,
        )
        return 0
    if not token:
        return 0

    mutated = 0
    for case in pending:
        log = list(case.fingerprint_to_fix_log or [])
        log_changed = False
        for entry in log:
            if not isinstance(entry, dict):
                continue
            pr = entry.get("fix_pr_number")
            if not pr or "layers_targeted" in entry:
                continue
            try:
                files = await get_pr_files(
                    token,
                    project.github_owner,
                    project.github_repo,
                    int(pr),
                    project_id=str(project_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[REGRESSION_DISPATCHER] layers backfill skipped PR #%s for "
                    "project=%s case=%s: %s",
                    pr,
                    project_id,
                    case.id,
                    exc,
                )
                continue
            paths = [str(f.get("filename") or "") for f in files if isinstance(f, dict)]
            classified = _classify_layers_from_paths(paths)
            entry["layers_targeted"] = classified
            # When the path classifier returned [] but at least one path was
            # supplied, persist the unmatched paths so the coverage-staleness
            # signal can name them. Cap the list so a 200-file refactor cannot
            # bloat the JSON column. When classification is non-empty, do NOT
            # write unmatched_paths — by definition every path that didn't
            # match a rule is irrelevant to the matched layers.
            non_empty_paths = [p for p in paths if p]
            if not classified and non_empty_paths:
                entry["unmatched_paths"] = non_empty_paths[
                    :_UNMATCHED_PATHS_PER_ENTRY_CAP
                ]
            log_changed = True
            mutated += 1
        if log_changed:
            case.fingerprint_to_fix_log = log
            flag_modified(case, "fingerprint_to_fix_log")
            session.add(case)

    if mutated:
        await session.commit()
    return mutated


def compute_unclassified_fix_coverage(
    cases: list[RegressionRemediationCase],
    *,
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Aggregate path-classifier coverage across recent fix-log entries.

    Walks ``RegressionRemediationCase.fingerprint_to_fix_log`` entries within
    the same ``merged_at`` lookback window used by ``compute_layer_defense_density``
    and ``_build_layer_defense_ledger``. Counts entries where classification
    was attempted (``layers_targeted`` present OR ``unmatched_paths`` present)
    toward ``total_qualifying_entries``; among those, counts entries where
    ``layers_targeted`` is the empty list (or absent) toward
    ``entries_with_empty_layers``.

    Pre-classification entries (neither key present) are NOT counted in
    either numerator or denominator — the coverage signal only fires once
    backfill has actually run.

    Returns a dict with:
      * ``total_qualifying_entries`` (int)
      * ``entries_with_empty_layers`` (int)
      * ``unclassified_fix_rate`` (float; 0.0 when total < min sample)
      * ``top_unmatched_paths`` (list of (path, count) tuples, most-frequent
        first, capped at ``_PATH_CLASSIFIER_COVERAGE_TOP_PATHS``)
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    total_qualifying = 0
    empty_layers = 0
    path_freq: dict[str, int] = {}

    for case in cases or []:
        log = case.fingerprint_to_fix_log or []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
            if merged_dt is None or merged_dt < cutoff:
                continue
            has_layers = "layers_targeted" in entry
            has_unmatched = "unmatched_paths" in entry
            if not (has_layers or has_unmatched):
                continue
            total_qualifying += 1
            layers = entry.get("layers_targeted")
            is_empty_layers = (has_layers and not layers) or (
                not has_layers and has_unmatched
            )
            if is_empty_layers:
                empty_layers += 1
                for raw_path in entry.get("unmatched_paths") or []:
                    if not raw_path or not isinstance(raw_path, str):
                        continue
                    path_freq[raw_path] = path_freq.get(raw_path, 0) + 1

    rate = 0.0
    if total_qualifying >= _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE:
        rate = empty_layers / total_qualifying

    top_paths = sorted(path_freq.items(), key=lambda kv: (-kv[1], kv[0]))[
        :_PATH_CLASSIFIER_COVERAGE_TOP_PATHS
    ]

    return {
        "total_qualifying_entries": total_qualifying,
        "entries_with_empty_layers": empty_layers,
        "unclassified_fix_rate": rate,
        "top_unmatched_paths": top_paths,
    }


async def _evaluate_path_classifier_coverage(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Open a project-scoped Alert when path-classifier coverage is stale.

    Non-spawning observation step — never spawns investigate tasks or consumes
    spawn budget. Wrapped by callers in try/except so a transient failure here
    cannot break the rest of the recovery scan.

    Trips when both:
      * ``total_qualifying_entries >= _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE``
      * ``unclassified_fix_rate >= _PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD``

    Dedup mirrors ``_emit_signal_staleness_alerts`` — skip insertion when an
    open Alert with the same project_id, no task_id, WARNING severity, and
    matching title already exists.
    """
    cases = (
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

    coverage = compute_unclassified_fix_coverage(list(cases))

    total = coverage["total_qualifying_entries"]
    empty = coverage["entries_with_empty_layers"]
    rate = coverage["unclassified_fix_rate"]
    top_paths: list[tuple[str, int]] = coverage["top_unmatched_paths"]

    if total < _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE:
        return
    if rate < _PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD:
        return

    title = _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE
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
    if existing is not None:
        return

    path_lines = (
        "\n".join(f"  - {path} ({count}×)" for path, count in top_paths)
        or "  (no unmatched paths recorded yet)"
    )
    message = (
        f"Path-based layer classifier returned [] for {empty}/{total} "
        f"({rate:.0%}) of recently-merged fix-log entries — the canonical "
        f"layer rules in _classify_layers_from_paths are stale relative to "
        f"where the autonomous loop is shipping fixes. Most-frequent "
        f"unmatched paths:\n{path_lines}\n"
        f"Update _classify_layers_from_paths or extend the canonical layer "
        f"list to cover these paths."
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


async def _evaluate_awaiting_backfill_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Open a project-scoped Alert when cross-layer awaiting_backfill backlog is stuck.

    Non-spawning observation step — never spawns investigate tasks or consumes
    spawn budget. Wrapped by callers in try/except so a transient failure here
    cannot break the rest of the recovery scan.

    Trips when EITHER:
      * ``awaiting_backfill_over_threshold_count >= _AWAITING_BACKFILL_COUNT_THRESHOLD``
      * ``awaiting_backfill_oldest_merged_at`` is older than ``_AWAITING_BACKFILL_AGE_THRESHOLD``

    Dedup mirrors ``_evaluate_path_classifier_coverage`` — skip insertion when
    an open Alert with the same project_id, no task_id, WARNING severity, and
    matching title already exists.
    """
    # Lazy import: admin_digest imports from this module at function scope
    # (compute_pipeline_health_summary), so importing it at module-level here
    # would create a load-time cycle.
    from app.routers.admin_digest import compute_cross_layer_compliance

    cutoff = _utcnow() - _AWAITING_BACKFILL_WINDOW
    cases = (
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

    metrics = compute_cross_layer_compliance(list(cases), cutoff)

    awaiting_count = int(metrics.get("awaiting_backfill_count") or 0)
    over_threshold = int(metrics.get("awaiting_backfill_over_threshold_count") or 0)
    oldest_iso = metrics.get("awaiting_backfill_oldest_merged_at")
    oldest_dt: datetime | None = None
    if oldest_iso:
        try:
            oldest_dt = _to_naive_utc(
                datetime.fromisoformat(str(oldest_iso).replace("Z", "+00:00"))
            )
        except (ValueError, TypeError):
            oldest_dt = None

    age_trip = (
        oldest_dt is not None
        and (_utcnow() - oldest_dt) >= _AWAITING_BACKFILL_AGE_THRESHOLD
    )
    count_trip = over_threshold >= _AWAITING_BACKFILL_COUNT_THRESHOLD
    if not (age_trip or count_trip):
        return

    title = _AWAITING_BACKFILL_ALERT_TITLE
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
    if existing is not None:
        return

    oldest_str = oldest_iso if oldest_iso else "(none)"
    message = (
        f"Cross-layer awaiting_backfill backlog is not draining: "
        f"{awaiting_count} entries total, {over_threshold} older than 24h, "
        f"oldest merged_at={oldest_str}. Likely causes: (1) GitHub auth "
        f"failures in _backfill_layers_targeted_once (which logs and "
        f"continues silently — check logs for token/installation errors), "
        f"(2) path-classifier coverage gaps in _classify_layers_from_paths "
        f"that yield empty layer assignments and never advance to a "
        f"layers_targeted value. Backlog growth corrupts the "
        f"cross-layer-compliance numerator/denominator surfaced at "
        f"GET /api/regression-cases/cross-layer-compliance."
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
    from app.regression_layer_classifier import classify_layers  # noqa: PLC0415

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
            from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415

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

    payload_dict: dict[str, Any] = {
        "type": "investigate",
        "prompt": prompt,
        "base_branch": _runtime_config.get("integration_branch", "main"),
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
                    setattr(
                        case, "introducing_pr_number", trip.get("introducing_pr_number")
                    )
                    setattr(
                        case,
                        "introducing_commit_sha",
                        trip.get("introducing_commit_sha"),
                    )
                    session.add(case)
    return spawned


_VIEWPORT_ASSERTION_RE = re.compile(
    r"(expectNoOverflowAt|expectNoTextTruncationAt|expectMinTouchTargetAt)\s*\("
)
_VIEWPORT_ESCAPE_HATCH_RE = re.compile(
    r"data-viewport-allow-(overflow|truncation|small-target)"
)
_VIEWPORT_TEST_PATH_RE = re.compile(r"^admin-ui/src/.+\.viewport\.test\.[tj]sx?$")
_COMPONENT_PATH_RE = re.compile(r"^admin-ui/src/components/[^/]+\.tsx$")


def compute_viewport_coverage_snapshot(
    diff_text: str, pr_number: int | None = None
) -> dict:
    """Compute a viewport_coverage_snapshot dict from a PR unified diff.

    Counts expectNoOverflowAt / expectNoTextTruncationAt / expectMinTouchTargetAt
    call sites in *.viewport.test.{ts,tsx} files, new data-viewport-allow-*
    escape-hatch attributes in component files, and deleted component files.
    Returns a dict suitable for project.design_doc["viewport_coverage_snapshot"].
    """
    assertion_before = 0
    assertion_after = 0
    escape_hatches = 0
    removed_components: list[str] = []

    in_viewport_test = False
    in_component = False
    current_path = ""

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            # parts: ["diff", "--git", "a/path", "b/path"]
            raw = parts[3] if len(parts) >= 4 else ""
            current_path = raw[2:] if raw.startswith("b/") else raw
            in_viewport_test = bool(_VIEWPORT_TEST_PATH_RE.match(current_path))
            in_component = bool(_COMPONENT_PATH_RE.match(current_path))
            continue

        if line.startswith("deleted file mode") and _COMPONENT_PATH_RE.match(
            current_path
        ):
            removed_components.append(current_path)
            continue

        if in_viewport_test:
            if line.startswith("-") and not line.startswith("---"):
                if _VIEWPORT_ASSERTION_RE.search(line):
                    assertion_before += 1
            elif line.startswith("+") and not line.startswith("+++"):
                if _VIEWPORT_ASSERTION_RE.search(line):
                    assertion_after += 1
            else:
                # context line — present on both sides
                if _VIEWPORT_ASSERTION_RE.search(line):
                    assertion_before += 1
                    assertion_after += 1

        if in_component and line.startswith("+") and not line.startswith("+++"):
            if _VIEWPORT_ESCAPE_HATCH_RE.search(line):
                escape_hatches += 1

    return {
        "assertion_count_before": assertion_before,
        "assertion_count_after": assertion_after,
        "new_escape_hatch_count": escape_hatches,
        "removed_component_files": removed_components[:50],
        "merged_pr": pr_number,
    }


async def update_viewport_coverage_snapshot_from_diff(
    session: AsyncSession,
    project: Any,
    pr_number: int,
    diff_text: str,
) -> None:
    """Write viewport_coverage_snapshot to project.design_doc.

    Called by the PR-merge hook (rollup_management) each time a PR merges.
    Skips silently when the project has no admin-ui directory in the diff.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    if "admin-ui/" not in diff_text:
        return

    snap = compute_viewport_coverage_snapshot(diff_text, pr_number=pr_number)
    doc = dict(project.design_doc or {})
    doc["viewport_coverage_snapshot"] = snap
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    logger.info(
        "[REGRESSION_DISPATCHER] viewport_coverage_snapshot updated "
        "project=%s pr=%s before=%d after=%d escape_hatches=%d removed=%d",
        project.id,
        pr_number,
        snap["assertion_count_before"],
        snap["assertion_count_after"],
        snap["new_escape_hatch_count"],
        len(snap["removed_component_files"]),
    )


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


def _extract_evaluating_since(task: Task) -> datetime:
    """Return when the task most recently entered EVALUATING status.

    Scans status_log (newest-first) for the transition entry; falls back
    to task.updated_at then task.created_at if no entry is found.
    """
    for entry in reversed(task.status_log or []):
        if "→ EVALUATING" in str(entry.get("msg", "")):
            ts = _parse_iso_timestamp(entry.get("t"))
            if ts is not None:
                return ts
    anchor = task.updated_at or task.created_at
    return anchor.replace(tzinfo=None) if getattr(anchor, "tzinfo", None) else anchor


def _format_reviewer_sla_stall_prompt(
    *,
    case_id: uuid.UUID,
    pr_number: int | None,
    elapsed_hours: float,
    task_id: uuid.UUID,
    task_status: str,
    latest_status_message: str | None,
) -> str:
    pr_str = f"#{pr_number}" if pr_number is not None else "(no PR recorded)"
    elapsed_str = f"{elapsed_hours:.1f}h"
    status_excerpt = (latest_status_message or "").strip()[:500] or "(none)"
    lines = [
        f"Pipeline regression detected (reviewer_sla_stall / case={case_id}).",
        f"Remediation PR {pr_str} has been in EVALUATING for {elapsed_str} with no review action.",
        f"Linked task: {task_id} (status={task_status})",
        f"Last status message (truncated to 500 chars):\n  {status_excerpt}",
        "",
        "Investigate: why has the remediation PR stalled in review/CI?  Look for:",
        "  - PR blocked on missing reviewer or review queue backlog",
        "  - CI rebuild stuck or flaking (no retry attempted)",
        "  - Merge conflict preventing CI from running",
        "  - Agent VM failure that left the task in EVALUATING with no forward progress",
        "  - PR already closed/merged but the case was not updated (stale case state)",
        "",
        "Diagnose the root cause and produce the structured diagnosis JSON report "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
    ]
    return "\n".join(lines)


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

    spawned = 0
    for case in cases:
        if spawned >= spawn_budget_left:
            break

        task = await session.get(Task, case.last_remediation_task_id)
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
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
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
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
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


def _format_cross_case_fingerprint_prompt(
    *,
    fingerprint: str,
    short_hash: str,
    contributing_classes: list[str],
    contributing_case_ids: list[str],
    sample_messages: list[str],
    occurrence_count: int,
) -> str:
    """Build the cross-case-fingerprint meta-investigation prompt."""
    lines = [
        f"CROSS-CASE FINGERPRINT META-INVESTIGATION (cross_case_fingerprint:{short_hash})",
        (
            "The same normalized error fingerprint has been observed across MULTIPLE "
            "distinct regression_class values. The dispatcher cannot tell from "
            "fingerprint identity alone whether this is one shared root cause or "
            "several unrelated surfaces that happen to normalize the same way — "
            "your investigation must validate which it is."
        ),
        "",
        f"Normalized fingerprint: {fingerprint}",
        f"Total occurrences in last {int(_CROSS_CASE_FINGERPRINT_LOOKBACK.days)}d: {occurrence_count}",
        "",
        "Contributing regression_class values:",
    ]
    for cls in contributing_classes:
        lines.append(f"  - {cls}")
    lines += ["", "Contributing case_ids:"]
    for cid in contributing_case_ids:
        lines.append(f"  - {cid}")
    if sample_messages:
        lines += ["", "Sample failure messages (deduped, truncated):"]
        for msg in sample_messages:
            lines.append(f"  - {msg}")
    lines += [
        "",
        "FRAMING QUESTION FOR THE INVESTIGATION",
        (
            "Is this one shared root symptom we should fix once, rather than "
            "fixing each surface independently? If yes, identify the shared "
            "root cause and propose a single coordinated remediation. If no, "
            "explain why these surfaces happen to share a fingerprint despite "
            "being independent (e.g. fingerprint normalization is too coarse), "
            "and recommend whether the fingerprint extraction should be tightened."
        ),
        "",
        "Produce the structured diagnosis JSON "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
    ]
    return "\n".join(lines)


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

    # fingerprint -> aggregated state
    by_fp: dict[str, dict[str, Any]] = {}
    for case in all_cases:
        case_class = case.regression_class or ""
        case_id = str(case.id)
        symptoms = case.symptoms_by_layer or {}
        if not isinstance(symptoms, dict):
            continue
        for entries in symptoms.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fp_raw = entry.get("fingerprint")
                if not isinstance(fp_raw, str):
                    continue
                fp = fp_raw.strip()
                if not fp:
                    continue
                if fp == case_class:
                    continue
                obs_dt = _parse_iso_timestamp(entry.get("observed_at"))
                if obs_dt is None or obs_dt < cutoff:
                    continue
                bucket = by_fp.setdefault(
                    fp,
                    {
                        "occurrences": 0,
                        "classes": set(),
                        "case_ids": [],
                        "samples": [],
                    },
                )
                bucket["occurrences"] += 1
                bucket["classes"].add(case_class)
                if case_id not in bucket["case_ids"]:
                    bucket["case_ids"].append(case_id)
                sample = entry.get("sample")
                if isinstance(sample, str) and sample:
                    short = sample[:500]
                    if short not in bucket["samples"] and len(bucket["samples"]) < 5:
                        bucket["samples"].append(short)

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


def _classify_cycle_compliance(row: dict) -> str:
    """Return the compliance label for a single PER-CYCLE LAYER HISTORY row.

    Labels:
      - ``compliant``         — mandate True AND >=2 distinct canonical layers targeted
      - ``non-compliant``     — mandate True AND layers_targeted recorded but <2 canonical layers
      - ``awaiting-backfill`` — mandate True AND layers_targeted not yet present (do NOT accuse)
      - ``not-mandated``      — mandate False or absent
    """
    if not row.get("spawn_was_cross_layer"):
        return "not-mandated"
    if not row.get("layers_targeted_present"):
        return "awaiting-backfill"
    canonical = set(LAYER_DISPLAY_ORDER)
    distinct_canonical = {
        layer for layer in (row.get("layers_targeted") or []) if layer in canonical
    }
    if len(distinct_canonical) >= 2:
        return "compliant"
    return "non-compliant"


def _format_recurring_class_prompt(
    *,
    regression_class: str,
    targeted_layers: list[str],
    cycle_rows: list[dict],
    layer_defense_block: str = "",
) -> str:
    """Build the recurring-class proactive meta-investigation prompt."""
    cycle_count = len(cycle_rows)
    layer_list = ", ".join(targeted_layers)
    lines = [
        f"RECURRING CROSS-LAYER CLASS META-INVESTIGATION (recurring_class:{regression_class})",
        (
            f"Regression class '{regression_class}' has accumulated {cycle_count} fix cycles "
            f"across {len(targeted_layers)} distinct canonical layers ({layer_list}). "
            "Each cycle applied a single-layer fix that held temporarily, then re-tripped "
            "on a different layer — a pattern indicating an unaddressed shared root cause."
        ),
        "",
        "PER-CYCLE LAYER HISTORY",
    ]
    non_compliant_targeted: set[str] = set()
    canonical_set = set(LAYER_DISPLAY_ORDER)
    for row in cycle_rows:
        layers = (
            ", ".join(row["layers_targeted"]) if row["layers_targeted"] else "unknown"
        )
        compliance = _classify_cycle_compliance(row)
        mandate = "yes" if row.get("spawn_was_cross_layer") else "no"
        lines.append(
            f"  Cycle {row['cycle']}: layers={layers}  merged_at={row['merged_at']}"
            f"  cross_layer_mandate={mandate}  compliance={compliance}"
        )
        if compliance == "non-compliant":
            for layer in row.get("layers_targeted") or []:
                if layer in canonical_set:
                    non_compliant_targeted.add(layer)

    if non_compliant_targeted:
        # Reuse compute_untargeted_layers over the same per-row layers_targeted
        # data the cycle history was built from — equivalent to running it over
        # the project's full aggregated fix-log entries for this class.
        untargeted = compute_untargeted_layers(cycle_rows)
        targeted_str = ", ".join(sorted(non_compliant_targeted))
        untargeted_str = (
            ", ".join(untargeted)
            if untargeted
            else "(none — every layer has a prior fix)"
        )
        lines += [
            "",
            "PRIOR MANDATE NON-COMPLIANCE",
            (
                f"At least one prior cycle was spawned with the cross-layer mandate "
                f"but shipped a single-layer fix targeting only: {targeted_str}. "
                "That fix did NOT comply with the multi-layer requirement."
            ),
            (
                f"Canonical layers still UNTARGETED across this class's full fix history: "
                f"{untargeted_str}. "
                "Steer the new defense toward these untargeted layers."
            ),
        ]

    lines += [
        "",
        "REQUIRED: propose defenses in AT LEAST 2 distinct canonical layers.",
        f"Canonical layers: {', '.join(LAYER_DISPLAY_ORDER)}",
        (
            "A single-layer fix has already been tried multiple times — "
            "a multi-layer defense is mandatory."
        ),
        "",
        (
            ".github/workflows/ files cannot be modified by the agent; "
            "treat workflow findings as notes in the diagnosis report."
        ),
    ]
    if layer_defense_block:
        lines.append("")
        lines.append(layer_defense_block)
    return "\n".join(lines)


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

    # Group fix-log entries by regression_class, skipping meta-investigation classes.
    _META_PREFIXES = (
        "layer_concentration:",
        "cross_case_fingerprint:",
        "recurring_class:",
        "anomaly:",
    )
    by_class: dict[str, list[dict]] = {}
    for case in all_cases:
        rc = case.regression_class or ""
        if any(rc.startswith(p) for p in _META_PREFIXES):
            continue
        fix_log = case.fingerprint_to_fix_log or []
        if not isinstance(fix_log, list):
            continue
        entries = [e for e in fix_log if isinstance(e, dict) and "layers_targeted" in e]
        if entries:
            by_class.setdefault(rc, []).extend(entries)

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
    if signal_name == "sr_catch_rate":
        raw = (doc.get("sr_catch_rate_trends") or {}).get(
            "signal_last_computed_at"
        ) or (doc.get("sr_catch_rate_trends") or {}).get("computed_at")
    elif signal_name == "fix_loop_divergence":
        raw = doc.get("fix_loop_divergence_signal_last_computed_at") or doc.get(
            "fix_loop_divergence_detected_at"
        )
    elif signal_name == "persistent_anomaly":
        raw = (_shared_state.persistent_anomalies.get(str(project_id)) or {}).get(
            "signal_last_computed_at"
        )
    elif signal_name == "deploy_health":
        raw = _shared_state.deploy_monitoring.get("signal_last_computed_at")
    elif signal_name == "sr_blind_spot":
        raw = doc.get("sr_blind_spot_signal_last_computed_at")
    else:
        raw = None
    return raw, _SIGNAL_STALENESS_WINDOWS.get(signal_name, timedelta(hours=48))


_TELEMETRY_STALENESS_SIGNALS: list[tuple[str, str]] = [
    ("sr_catch_rate", "playbook_learning.compute_sr_catch_rate_trends"),
    (
        "fix_loop_divergence",
        "fix_loop_telemetry (computed alongside task fix-loop history)",
    ),
    ("persistent_anomaly", "anomaly_detection (updated on task completion)"),
    ("deploy_health", "deploy_health (updated on deploy events)"),
    (
        "sr_blind_spot",
        "sr_blind_spots.compute_sr_blind_spot_themes (call on task completion)",
    ),
]


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
    for signal, source_module in _TELEMETRY_STALENESS_SIGNALS:
        if spawned >= spawn_budget_left:
            break
        raw_ts, threshold = _get_staleness_raw_ts(signal, doc, project_id)
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None or (now - computed_at) <= threshold:
            continue
        age = now - computed_at
        regression_class = f"telemetry_stale:{signal}"
        prompt = "\n".join(
            [
                f"Telemetry staleness: signal '{signal}' has not refreshed in {round(age.total_seconds() / 3600, 1)}h.",
                f"Last computed: {computed_at.isoformat()} (freshness window: {round(threshold.total_seconds() / 3600, 1)}h).",
                f"This signal is computed by: {source_module}.",
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
                f"Signal '{signal}' last computed {round(age.total_seconds() / 3600, 1)}h ago."
            ],
            title_hint=f"telemetry staleness: {signal}",
            prompt_override=prompt,
            extra_snapshot_fields={
                "signal_last_computed_at": computed_at.isoformat(),
                "age_hours": round(age.total_seconds() / 3600, 1),
                "source_module": source_module,
            },
        )
        if spawn is not None:
            spawned += 1
    return spawned


def _format_strategic_stall_prompt(
    *,
    stall_hours: int,
    recent_task_count: int,
    failed_strategic: list[Task],
) -> str:
    lines = [
        "Pipeline regression detected (strategic_cycle_stall).",
        f"No strategic_review or design_doc_update task has completed in the last {stall_hours}h.",
        f"Non-strategic task activity in window: {recent_task_count} task(s) created.",
        "",
        "The strategic engine normally runs design_doc_update (Phase 1) to refresh project",
        "context, then strategic_review (Phase 2) to score findings, evaluate directions,",
        "and queue new research tasks. A prolonged stall leads to stale project context,",
        "missed signal patterns, and direction drift.",
        "",
    ]
    if failed_strategic:
        lines.append("Recent FAILED strategic tasks in this window:")
        for t in failed_strategic:
            ptype = (t.first_payload or {}).get("type", "unknown")
            lines.append(f"  - {ptype} task {t.id} FAILED")
        lines.append("")
    lines += [
        "Investigate: why has the strategic engine not completed a cycle? Look for:",
        "  - strategic_review or design_doc_update tasks stuck in QUEUED/IN_PROGRESS",
        "  - repeated FAILED outcomes on strategic tasks",
        "  - orchestrator scheduling not spawning strategic tasks",
        "  - research slot exhaustion (max_total_research_per_project cap) blocking spawns",
        "  - recent deploy changes that may correlate with the stall onset",
        "",
        "\nDiagnose the underlying root cause of this regression and produce "
        "the structured diagnosis JSON report ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the "
        "remediation.",
    ]
    return "\n".join(lines)


async def _evaluate_strategic_cycle_stall_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
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
        ),
    )
    return 1 if spawn is not None else 0


async def run_recovery_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Re-evaluate merged regression cases and close or re-arm them."""
    from app.models import Project

    project = await session.get(Project, project_id)
    if project is None:
        return 0

    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.MERGED,
                )
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for case in rows:
        snapshot = case.signal_snapshot_at_merge or {}
        merged_at_raw = snapshot.get("captured_at") or case.updated_at.isoformat()
        try:
            merged_at = datetime.fromisoformat(
                str(merged_at_raw).replace("Z", "+00:00")
            )
        except ValueError:
            merged_at = case.updated_at
        if merged_at.tzinfo is not None:
            merged_at = merged_at.replace(tzinfo=None)
        if now < merged_at + _REGRESSION_RECOVERY_COOLDOWN:
            continue
        state = await _current_signal_value_for_class(
            session,
            project_id,
            case.regression_class,
            project.design_doc,
        )
        if state is None:
            continue
        if not state["tripped"]:
            mismatch_entry = _compute_recovery_layer_mismatch(
                case.fingerprint_to_fix_log, case.symptoms_by_layer, now
            )
            case.mark_closed()
            # Reset the recovery clock so a future re-escalation does not
            # pick up a stale healthy_since on the same row (mirrors the
            # prior process-local ``_signal_healthy_since.pop`` semantics).
            case.signal_healthy_since_at = None
            new_entries: list[dict[str, Any]] = [
                {
                    "fingerprint": case.regression_class,
                    "recovered_at": now.isoformat(),
                    "post_merge_value": state["current_rate"],
                }
            ]
            if mismatch_entry:
                new_entries.append(mismatch_entry)
            case.fingerprint_to_fix_log = (
                list(case.fingerprint_to_fix_log or []) + new_entries
            )
        else:
            last_spawn = await _last_spawn_for_class(
                session, project_id, case.regression_class
            )
            if last_spawn is not None and last_spawn.created_at:
                last_at = last_spawn.created_at
                if last_at.tzinfo is not None:
                    last_at = last_at.replace(tzinfo=None)
                if now < last_at + _REGRESSION_COOLDOWN:
                    logger.info(
                        "[REGRESSION_DISPATCHER] recovery-rearm-cooldown-deferred "
                        "project=%s class=%s last_spawn=%s cooldown_until=%s",
                        project_id,
                        case.regression_class,
                        last_at.isoformat(),
                        (last_at + _REGRESSION_COOLDOWN).isoformat(),
                    )
                    continue
            # Recovery-scan re-arm: a MERGED case whose signal re-tripped
            # honors the 6h class-level cooldown (see guard above). Pass
            # fast_track=False so the dispatcher does not bypass that cooldown
            # — the post-merge signal needs time to settle, not an immediate
            # re-investigation. Failure-driven re-arm (task_lifecycle hook)
            # uses the default fast_track=True to opt into the bypass.
            case.re_arm(fast_track=False)
            # Re-armed cases start a fresh recovery clock — do not let a
            # stale healthy_since from a prior lifecycle short-circuit the
            # auto-close stable window after a future escalation.
            case.signal_healthy_since_at = None
        session.add(case)
        changed += 1
    if changed:
        await session.commit()

    # Opportunistic backfill: populate layers_targeted on any fix-log entries
    # that lack it. Bounded per tick; GitHub failures are logged and skipped.
    try:
        await _backfill_layers_targeted_once(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] layers_targeted backfill raised for project=%s: %s",
            project_id,
            exc,
        )

    # Non-spawning observation: open a WARNING alert when the path-based
    # layer classifier is increasingly returning [] across recently-merged
    # fix-log entries. Wrapped in try/except so a transient failure here
    # cannot break the rest of the recovery scan.
    try:
        await _evaluate_path_classifier_coverage(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] path-classifier coverage evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    # Non-spawning observation: open a WARNING alert when the cross-layer
    # awaiting_backfill backlog stops draining (count past 24h grace exceeds
    # threshold OR the oldest entry is older than age threshold). Wrapped in
    # try/except so a transient failure here cannot break the rest of the
    # recovery scan.
    try:
        await _evaluate_awaiting_backfill_alert(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] awaiting_backfill alert evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    # Non-spawning observation: open a WARNING alert when >=2 SR categories
    # have entered lone-checker state (SR is the only reviewer, catch-rate
    # unmeasurable). Wrapped in try/except for the same reason as above.
    try:
        from app.routers.admin_digest import _evaluate_lone_checker_alert

        await _evaluate_lone_checker_alert(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] lone-checker alert evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    # Non-spawning observation: emit/dismiss WARNING alerts when SR catch-rate
    # for any configured category falls below the project's per-category target
    # for >=2 consecutive windows. Same try/except pattern.
    try:
        from app.routers.admin_digest import _evaluate_sr_catch_rate_targets

        await _evaluate_sr_catch_rate_targets(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] sr_catch_rate_targets alert evaluation "
            "raised for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await _run_compliance_followup_once(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] compliance-followup raised for project=%s: %s",
            project_id,
            exc,
        )

    return changed


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


async def _collect_agent_vm_failures(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict]:
    """Return recent FAILED tasks classified by VM-resource exit reason.

    Inspects Task.context for failure_category, timeout_subtype, and
    spot_preemption_count. Only tasks whose failure is attributable to the
    AGENT_VM infrastructure layer are included; code-level timeouts (cascade,
    iteration_limit) are excluded so this signal does not double-fire with
    layer_concentration on the same events.

    Exit-reason labels: 'oom', 'timeout', 'spot_preempt', 'vm_provisioning_failure'.
    """
    cutoff = _utcnow() - timedelta(hours=_AGENT_VM_RESOURCE_WINDOW_HOURS)
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.FAILED,
                    Task.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    from app.failure_classification import classify_vm_exit_reason

    results = []
    for task in rows:
        exit_reason = classify_vm_exit_reason(task)
        if exit_reason is None:
            continue
        ctx = task.context or {}
        payload = task.first_payload or {}
        results.append(
            {
                "task_id": str(task.id),
                "task_type": payload.get("type", "unknown"),
                "exit_reason": exit_reason,
                "scope": ctx.get("scope", "unknown"),
            }
        )
    return results


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


async def _persist_recovery_cycle_record(
    session: AsyncSession,
    project_id: uuid.UUID,
    cycle_started_at: datetime,
    marginal_closed: int,
    confirmed_closed: int,
) -> None:
    """Persist a recovery cycle record with marginal/confirmed recovery counts."""
    from app.models import DispatcherCycleRecord  # noqa: PLC0415

    try:
        session.add(
            DispatcherCycleRecord(
                project_id=project_id,
                cycle_started_at=cycle_started_at,
                cycle_finished_at=_utcnow(),
                signals_evaluated=0,
                signals_tripped=0,
                spawns_emitted=0,
                rejections_by_reason={},
                marginal_recoveries_count=marginal_closed,
                confirmed_recoveries_count=confirmed_closed,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_WATCHDOG] recovery-cycle-record write failed",
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _check_marginal_recovery_rate_threshold(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Check if marginal recovery rate exceeds threshold for >=2 consecutive cycles.

    If both the last 2 recovery cycles have marginal_rate > threshold (default 0.30),
    emit a WARNING alert.
    """
    threshold = float(
        _runtime_config.get("marginal_recovery_rate_threshold", 0.30) or 0.30
    )
    records = await get_dispatcher_cycle_records(
        session,
        project_id=str(project_id),
        limit=2,
    )
    if len(records) < 2:
        return

    rates = []
    for rec in records[-2:]:
        marginal = rec.get("marginal_recoveries_count", 0)
        confirmed = rec.get("confirmed_recoveries_count", 0)
        total = marginal + confirmed
        if total > 0:
            rates.append(marginal / total)
        else:
            rates.append(0.0)

    if len(rates) == 2 and all(r > threshold for r in rates):
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title="High marginal recovery rate threshold exceeded"[:200],
                message=(
                    f"Marginal recovery rate exceeded {threshold:.0%} "
                    f"for {len(rates)} consecutive cycles. "
                    f"Rates: {[f'{r:.1%}' for r in rates]}. "
                    f"May indicate fixes that do not fully address root causes."
                )[:1000],
            )
        )
        await session.commit()


async def run_watchdog_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Escalate stale ACTIVE/MERGED cases and auto-close recovered NMI cases.

    Two-pass scan:
    - ACTIVE cases not updated in 24h or MERGED cases not updated in 6h are
      escalated to NEEDS_MANUAL_INTERVENTION with an operator-visible Alert.
    - NEEDS_MANUAL_INTERVENTION cases whose underlying signal has organically
      stayed below threshold for ``regression_recovered_unattended_window_hours``
      are auto-closed as ``recovered_unattended`` so the operator queue
      reflects only escalations that genuinely still need attention.

    Returns the total number of cases mutated this tick (escalated + auto-closed).
    """
    cycle_started_at = _utcnow()
    _recovery_counts = {"marginal": 0, "confirmed": 0}
    _token = _recovery_counts_var.set(_recovery_counts)
    mutated = 0
    marginal_closed = 0
    confirmed_closed = 0
    try:
        now = _utcnow()
        rows = (
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project_id,
                        RegressionRemediationCase.status.in_(
                            [
                                RegressionCaseStatus.ACTIVE,
                                RegressionCaseStatus.MERGED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        for case in rows:
            updated = case.updated_at
            if updated.tzinfo is not None:
                updated = updated.replace(tzinfo=None)

            if case.status == RegressionCaseStatus.ACTIVE:
                threshold = _WATCHDOG_ACTIVE_STALE_THRESHOLD
                reason = "ACTIVE case stale — investigation may have finished with low confidence and no remediation was spawned"
                escalation_reason = ESCALATION_REASON_STALE_ACTIVE
            else:  # MERGED
                threshold = _WATCHDOG_MERGED_STALE_THRESHOLD
                reason = "MERGED case stale — recovery scan could not evaluate it (signal_snapshot_at_merge may be empty)"
                escalation_reason = ESCALATION_REASON_STALE_MERGED

            if now < updated + threshold:
                continue

            # ACTIVE-stale auto-escalation: if the original investigation child is
            # still running and we have not yet auto-escalated this case, give the
            # autonomous loop ONE more attempt — spawn a meta-investigate task
            # focused on the remediation BLOCKER (merge conflicts, reviewer/CI
            # disagreement, leader-death cascade) and let the 24h window restart
            # from the auto-escalation moment. `auto_escalation_count` is the
            # cooldown gate (capped at 1 — the next stale tick goes straight to
            # NMI). We scope the meta task with `signal_name=auto_escalation_meta_investigate`
            # so the existing _last_spawn_for_class cooldown — keyed on
            # (spawned_by, regression_class) — does not block the meta-investigate;
            # `_in_flight_for_class` likewise dedups on regression_class, but here
            # we deliberately want a peer task running alongside the original
            # investigation, so we bypass _maybe_spawn entirely and emit the
            # task/history/alert inline.
            if (
                case.status == RegressionCaseStatus.ACTIVE
                and (case.auto_escalation_count or 0) == 0
            ):
                child_alive = await _has_active_remediation_child(session, case)
                if child_alive:
                    meta_task = await _spawn_auto_escalation_meta_investigate(
                        session, project_id, case
                    )
                    case.auto_escalation_count = (case.auto_escalation_count or 0) + 1
                    case.last_remediation_task_id = meta_task.id
                    case.updated_at = now
                    session.add(case)
                    logger.info(
                        "[REGRESSION_WATCHDOG] auto-escalate project=%s class=%s "
                        "meta_task=%s (one-shot, count=1)",
                        project_id,
                        case.regression_class,
                        meta_task.id,
                    )
                    mutated += 1
                    continue

            logger.warning(
                "[REGRESSION_WATCHDOG] stale-%s project=%s class=%s updated_at=%s "
                "threshold_hours=%s — escalating to NEEDS_MANUAL_INTERVENTION",
                case.status,
                project_id,
                case.regression_class,
                updated.isoformat(),
                threshold.total_seconds() / 3600,
            )
            case.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
            case.needs_manual_intervention_at = now
            case.escalation_reason = escalation_reason
            session.add(case)
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Regression case stalled: {case.regression_class}"[:200],
                    message=(
                        f"{reason}. Case last updated {updated.isoformat()}; "
                        f"staleness threshold {threshold}. Manual review required."
                    )[:1000],
                )
            )
            mutated += 1

        if mutated:
            await session.commit()

        (
            marginal_closed,
            confirmed_closed,
        ) = await _auto_close_recovered_manual_intervention(session, project_id)
        _recovery_counts["marginal"] = marginal_closed
        _recovery_counts["confirmed"] = confirmed_closed
        return mutated + marginal_closed + confirmed_closed
    finally:
        _recovery_counts_var.reset(_token)
        if marginal_closed or confirmed_closed:
            await _persist_recovery_cycle_record(
                session, project_id, cycle_started_at, marginal_closed, confirmed_closed
            )
            await _check_marginal_recovery_rate_threshold(session, project_id)


async def _auto_close_recovered_manual_intervention(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, int]:
    """Auto-close NEEDS_MANUAL_INTERVENTION cases whose signal has recovered.

    Re-evaluates each case's underlying signal via ``_current_signal_value_for_class``
    and persists the first-healthy moment on
    ``RegressionRemediationCase.signal_healthy_since_at``.  Once the case has
    been continuously healthy for the configured stable window, transitions
    to CLOSED with ``last_remediation_result = 'recovered_unattended'`` and
    emits a single INFO Alert.  The stable window therefore survives worker
    restarts — a worker that drops mid-window does not let the next worker
    reset the clock.

    Race protection: each candidate row is re-acquired with
    ``with_for_update(skip_locked=True)`` and the status is re-checked under the
    lock so the operator ``Clear`` button (which transitions back to ACTIVE) can't
    race against this auto-close pass into a double-write.

    Returns: tuple of (marginal_recoveries_count, confirmed_recoveries_count)
    """
    from app.models import Project

    project = await session.get(Project, project_id)
    if project is None:
        return 0, 0
    design_doc = project.design_doc

    candidate_rows = (
        await session.execute(
            select(
                RegressionRemediationCase.id,
                RegressionRemediationCase.regression_class,
            ).where(
                RegressionRemediationCase.project_id == project_id,
                RegressionRemediationCase.status
                == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            )
        )
    ).all()
    if not candidate_rows:
        return 0, 0

    window_hours = int(
        _runtime_config.get("regression_recovered_unattended_window_hours", 24) or 24
    )
    window = timedelta(hours=window_hours)
    now = _utcnow()
    marginal_closed = 0
    confirmed_closed = 0
    mutated_rows = 0
    for case_id, _candidate_class in candidate_rows:
        locked = (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.id == case_id,
                    RegressionRemediationCase.status
                    == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        # Row is held by a peer (operator clear, parallel watchdog) or its
        # status changed between the candidate scan and the lock. Skip;
        # no marker mutation needed because the column lives on the row
        # whose status will dictate the next-tick behavior.
        if locked is None:
            continue

        state = await _current_signal_value_for_class(
            session, project_id, locked.regression_class, design_doc
        )
        # state is None means telemetry is missing for this class (e.g. empty
        # design_doc) — treat as 'unknown' rather than 'healthy' so a case we
        # cannot evaluate does not silently retire after the stable window.
        if state is None:
            continue
        if state.get("tripped"):
            # Signal tripped again — reset the recovery clock so any future
            # healthy run starts the stable-window timer from scratch.
            if locked.signal_healthy_since_at is not None:
                locked.signal_healthy_since_at = None
                session.add(locked)
                mutated_rows += 1
            continue

        if locked.signal_healthy_since_at is None:
            locked.signal_healthy_since_at = now
            session.add(locked)
            mutated_rows += 1
            first_healthy = now
        else:
            first_healthy = locked.signal_healthy_since_at
        if now - first_healthy < window:
            continue

        post_value = state.get("current_rate")

        # Determine if the recovery is marginal by comparing the pre-fix signal
        # value (stored at merge time) with the current post-fix value. The
        # polarity (which direction is "improvement") depends on the signal:
        # sr_catch_rate and pipeline_success_rate are higher-is-better;
        # divergence/blind-spot/anomaly counts are lower-is-better.
        pre_value = (locked.signal_snapshot_at_merge or {}).get("current_rate")
        close_reason = CLOSE_REASON_RECOVERED_UNATTENDED
        alert_severity = AlertSeverity.INFO
        marginal = False
        if isinstance(pre_value, (int, float)) and isinstance(post_value, (int, float)):
            higher_is_better = _signal_higher_is_better(locked.regression_class)
            improvement = (
                post_value - pre_value if higher_is_better else pre_value - post_value
            )
            # Reference magnitude for the relative-improvement ratio is the
            # pre-fix value's distance from the "best" end of the scale. For
            # higher-is-better signals like a hit-rate, recovering from 0.40
            # toward 1.0 has a 0.60 headroom — using pre_value (0.40) as the
            # denominator would overstate the relative improvement.
            if higher_is_better:
                reference = max(1.0 - pre_value, 0.0)
            else:
                reference = pre_value if pre_value > 0 else 0.0
            rel_floor = float(
                _runtime_config.get(
                    "regression_recovered_marginal_relative_floor", 0.10
                )
                or 0.10
            )
            abs_floor = float(
                _runtime_config.get(
                    "regression_recovered_marginal_absolute_floor", 0.05
                )
                or 0.05
            )
            if improvement <= 0:
                # Signal did not improve (or got worse) despite not tripping.
                marginal = True
            else:
                rel_improvement = (
                    improvement / reference if reference > 0 else float("inf")
                )
                if rel_improvement < rel_floor and improvement < abs_floor:
                    marginal = True
            if marginal:
                close_reason = CLOSE_REASON_RECOVERED_MARGINAL
                alert_severity = AlertSeverity.WARNING
        elif pre_value is not None or post_value is not None:
            # Snapshot exists but does not carry numeric metrics we can compare
            # — operators should be able to see this in logs (otherwise it is
            # indistinguishable from a confirmed clean recovery).
            logger.info(
                "[REGRESSION_WATCHDOG] snapshot-missing project=%s class=%s "
                "pre_value=%r post_value=%r — falling back to %s without "
                "magnitude check",
                project_id,
                locked.regression_class,
                pre_value,
                post_value,
                CLOSE_REASON_RECOVERED_UNATTENDED,
            )

        watchdog_mismatch = _compute_recovery_layer_mismatch(
            locked.fingerprint_to_fix_log, locked.symptoms_by_layer, now
        )
        locked.mark_closed()
        locked.last_remediation_result = close_reason
        watchdog_entries: list[dict[str, Any]] = [
            {
                "fingerprint": locked.regression_class,
                "recovered_at": now.isoformat(),
                "post_recovery_value": post_value,
                "reason": close_reason,
            }
        ]
        if watchdog_mismatch:
            watchdog_entries.append(watchdog_mismatch)
        locked.fingerprint_to_fix_log = (
            list(locked.fingerprint_to_fix_log or []) + watchdog_entries
        )
        # Cleared on close so the column never carries a stale value across
        # case lifecycles — mirrors the prior process-local ``.pop()`` calls.
        locked.signal_healthy_since_at = None
        session.add(locked)
        if marginal:
            alert_title = f"Regression marginal recovery: {locked.regression_class}"[
                :200
            ]
            alert_message = (
                f"NEEDS_MANUAL_INTERVENTION case for '{locked.regression_class}' "
                f"auto-closed with marginal improvement: pre-fix={pre_value}, "
                f"post-fix={post_value}. Signal likely to re-trip."
            )[:1000]
        else:
            alert_title = f"Regression auto-recovered: {locked.regression_class}"[:200]
            alert_message = (
                f"NEEDS_MANUAL_INTERVENTION case for '{locked.regression_class}' "
                f"auto-closed: underlying signal stayed below threshold for "
                f"{window_hours}h without operator action."
            )[:1000]
        session.add(
            Alert(
                project_id=project_id,
                severity=alert_severity,
                title=alert_title,
                message=alert_message,
            )
        )
        # Spawn follow-up investigation for marginal recoveries so the
        # autonomous loop diagnoses inadequate fixes proactively.
        if marginal and bool(
            _runtime_config.get("regression_marginal_followup_enabled", True)
        ):
            followup_cooldown_hours = int(
                _runtime_config.get("regression_marginal_followup_cooldown_hours", 72)
                or 72
            )
            # Cooldown anchor lives on the row column so it survives any future
            # fix-log restructuring and is index-friendly. Strip tz for the
            # comparison since the column is stored as naive UTC.
            last_followup_at = locked.marginal_followup_spawned_at
            if last_followup_at is not None and last_followup_at.tzinfo is not None:
                last_followup_at = last_followup_at.replace(tzinfo=None)
            if last_followup_at is None or now - last_followup_at >= timedelta(
                hours=followup_cooldown_hours
            ):
                # Build layer-history summary from prior spawn log entries.
                _seen_layers: list[str] = []
                for _e in locked.fingerprint_to_fix_log or []:
                    if isinstance(_e, dict):
                        for _layer in _e.get("spawn_layers_enumerated") or []:
                            if _layer not in _seen_layers:
                                _seen_layers.append(_layer)
                _layer_block = (
                    f"\n\nLayer history: {', '.join(_seen_layers)}"
                    if _seen_layers
                    else ""
                )
                _followup_prompt = (
                    f"Prior fix for regression class '{locked.regression_class}' "
                    f"was closed as MARGINAL recovery: pre-fix signal={pre_value}, "
                    f"post-fix signal={post_value}. The improvement was insufficient "
                    f"(< 10% relative AND < 5% absolute).{_layer_block}\n\n"
                    "Diagnose why the prior fix was insufficient — examine the "
                    "fix-log entries and propose either (a) a stronger remediation "
                    "in the same layer, or (b) a defense in a different layer."
                )
                _followup_payload = validate_internal_payload(
                    {
                        "type": "investigate",
                        "prompt": _followup_prompt,
                        "base_branch": _runtime_config.get(
                            "integration_branch", "main"
                        ),
                        "effort": "low",
                    },
                    "pipeline_regression_dispatcher:marginal_followup",
                )
                _followup_task = Task(
                    project_id=project_id,
                    title=(f"Marginal-recovery follow-up: {locked.regression_class}")[
                        :200
                    ],
                    description=(
                        "Auto-spawned: prior fix closed as marginal recovery — "
                        "investigate why the prior remediation was insufficient."
                    )[:500],
                    payloads=[_followup_payload],
                    context={
                        "spawned_by": "pipeline_regression_dispatcher_marginal_followup",
                        "regression_class": locked.regression_class,
                        "signal_name": "marginal_followup",
                        "pre_fix_value": pre_value,
                        "post_fix_value": post_value,
                        "remediation_case_id": str(locked.id),
                    },
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
                session.add(_followup_task)
                await session.flush()
                session.add(
                    TaskHistory(
                        task_id=_followup_task.id,
                        action="SPAWNED",
                        details={
                            "spawned_by": "PIPELINE_REGRESSION_DISPATCHER_MARGINAL_FOLLOWUP",
                            "regression_class": locked.regression_class,
                            "signal_name": "marginal_followup",
                        },
                    )
                )
                # Persist cooldown anchor on the column (durable across fix-log
                # restructuring) and append a fix-log audit entry for history.
                locked.marginal_followup_spawned_at = now
                locked.fingerprint_to_fix_log = list(
                    locked.fingerprint_to_fix_log or []
                ) + [
                    {
                        "type": "marginal_followup_spawned",
                        "spawned_at": now.isoformat(),
                        "task_id": str(_followup_task.id),
                        "pre_value": pre_value,
                        "post_value": post_value,
                    }
                ]
                session.add(locked)
                logger.info(
                    "[REGRESSION_WATCHDOG] marginal-followup-spawned project=%s "
                    "class=%s task_id=%s pre_value=%s post_value=%s",
                    project_id,
                    locked.regression_class,
                    _followup_task.id,
                    pre_value,
                    post_value,
                )
        logger.info(
            "[REGRESSION_WATCHDOG] auto-recovered project=%s class=%s "
            "window_hours=%d pre_value=%s post_value=%s reason=%s",
            project_id,
            locked.regression_class,
            window_hours,
            pre_value,
            post_value,
            close_reason,
        )
        if marginal:
            marginal_closed += 1
        else:
            confirmed_closed += 1

    if marginal_closed or confirmed_closed or mutated_rows:
        await session.commit()
    return marginal_closed, confirmed_closed


async def _run_compliance_followup_once(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Spawn compliance follow-up tasks for non-compliant cross-layer fixes.

    Scans MERGED/CLOSED cases for fix-log entries where spawn_was_cross_layer
    is True and layers_targeted (post-backfill) covers <2 distinct canonical
    layers. Entries that include recommended_unmodifiable_layers (e.g. CI_WORKFLOW
    documented in the diagnosis report) count toward the effective layer total so
    that a fix that correctly documented an unmodifiable layer is not penalized.
    Gated by runtime config and a per-case cooldown column.
    """
    from app.regression_layer_classifier import ALL_LAYERS

    if not bool(_runtime_config.get("regression_compliance_followup_enabled", True)):
        return 0

    cooldown_hours = int(
        _runtime_config.get("regression_compliance_followup_cooldown_hours", 72) or 72
    )
    now = _utcnow()
    _all_layers = set(ALL_LAYERS)

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status.in_(
                        [RegressionCaseStatus.MERGED, RegressionCaseStatus.CLOSED]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    spawned = 0
    for case in rows:
        last_at = getattr(case, "compliance_followup_spawned_at", None)
        if last_at is not None:
            if last_at.tzinfo is not None:
                last_at = last_at.replace(tzinfo=None)
            if now - last_at < timedelta(hours=cooldown_hours):
                continue

        # Only the most-recent classified cross-layer-mandated entry decides
        # compliance for the case. If that entry is compliant, do not look further
        # back: an older non-compliant entry has already been superseded by a
        # newer compliant fix and re-spawning would be a permanent loop, since
        # the spawned compliance_followup's own merged fix is recorded with
        # spawn_was_cross_layer=False and never displaces the older entry.
        non_compliant_entry = None
        for entry in reversed(case.fingerprint_to_fix_log or []):
            if not isinstance(entry, dict):
                continue
            if not entry.get("spawn_was_cross_layer"):
                continue
            if "layers_targeted" not in entry:
                continue  # awaiting_backfill — not yet classified
            targeted = set(entry.get("layers_targeted") or []) & _all_layers
            recommended_unmod = (
                set(entry.get("recommended_unmodifiable_layers") or []) & _all_layers
            )
            if len(targeted | recommended_unmod) < 2:
                non_compliant_entry = entry
            break

        if non_compliant_entry is None:
            continue

        targeted = set(non_compliant_entry.get("layers_targeted") or []) & _all_layers
        spawn_layers = list(non_compliant_entry.get("spawn_layers_enumerated") or [])
        missing = [la for la in spawn_layers if la not in targeted]

        _prompt = (
            f"Cross-layer investigation for '{case.regression_class}' required "
            f"defenses in >=2 distinct canonical layers, but PR #"
            f"{non_compliant_entry.get('fix_pr_number', 'unknown')} only targeted: "
            f"{sorted(targeted) or ['(none)']}. "
            f"Layers seen in symptoms but not yet defended: {missing or ['(unknown)']}. "
            "Propose a defense in a distinct canonical layer not yet addressed.\n\n"
            "Note: .github/workflows/ cannot be modified by the agent. A CI_WORKFLOW "
            "recommendation belongs in the diagnosis report (set "
            "'recommended_unmodifiable_layers': ['CI_WORKFLOW'] in context), not the diff."
        )
        _payload = validate_internal_payload(
            {
                "type": "investigate",
                "prompt": _prompt,
                "base_branch": _runtime_config.get("integration_branch", "main"),
                "effort": "low",
            },
            "pipeline_regression_dispatcher:compliance_followup",
        )
        _task = Task(
            project_id=project_id,
            title=f"Compliance follow-up: {case.regression_class}"[:200],
            description=(
                "Auto-spawned: cross-layer fix targeted <2 canonical layers — "
                "compliance follow-up required."
            )[:500],
            payloads=[_payload],
            context={
                "spawned_by": "pipeline_regression_dispatcher_compliance_followup",
                "regression_class": case.regression_class,
                "signal_name": "compliance_followup",
                "targeted_layers": sorted(targeted),
                "missing_layers": missing,
                "remediation_case_id": str(case.id),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(_task)
        await session.flush()
        session.add(
            TaskHistory(
                task_id=_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "PIPELINE_REGRESSION_DISPATCHER_COMPLIANCE_FOLLOWUP",
                    "regression_class": case.regression_class,
                    "signal_name": "compliance_followup",
                },
            )
        )
        case.compliance_followup_spawned_at = now
        case.fingerprint_to_fix_log = list(case.fingerprint_to_fix_log or []) + [
            {
                "type": "compliance_followup_spawned",
                "spawned_at": now.isoformat(),
                "task_id": str(_task.id),
                "targeted_layers": sorted(targeted),
                "missing_layers": missing,
            }
        ]
        session.add(case)
        spawned += 1
        logger.info(
            "[REGRESSION_WATCHDOG] compliance-followup-spawned project=%s "
            "class=%s task_id=%s targeted=%s missing=%s",
            project_id,
            case.regression_class,
            _task.id,
            sorted(targeted),
            missing,
        )

    if spawned:
        await session.commit()
    return spawned


async def _any_signal_would_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
) -> bool:
    """Side-effect-free probe: would any subscribed signal trip right now?

    Used to gate the deploy-monitoring defer alert so we don't claim a signal
    tripped when none actually would. Mirrors the classes scanned in
    ``run_once``: SR catch-rate, fix-loop divergence, pipeline success-rate,
    persistent anomalies.
    """
    sr_state = await _current_signal_value_for_class(
        session,
        project_id,
        "sr_catch_rate",
        project_design_doc,
    )
    if sr_state is not None and sr_state["tripped"]:
        return True
    fl_state = await _current_signal_value_for_class(
        session,
        project_id,
        "fix_loop_divergence",
        project_design_doc,
    )
    if fl_state is not None and fl_state["tripped"]:
        return True
    if _runtime_config.get("fix_loop_divergence_fingerprint_enabled", False):
        if await _collect_fix_loop_divergence_fingerprints(session, project_id):
            return True
    ps_state = await _current_signal_value_for_class(
        session,
        project_id,
        "pipeline_success_rate",
        project_design_doc,
    )
    if ps_state is not None and ps_state["tripped"]:
        return True
    import app.shared_state as _ss

    proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
    recency_cutoff = (
        _utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
    ).isoformat()
    for entry in proj_anomalies.values():
        if not isinstance(entry, dict):
            continue
        if int(entry.get("occurrence_count") or 0) < _ANOMALY_DISPATCH_MIN_OCCURRENCES:
            continue
        if str(entry.get("last_seen") or "") < recency_cutoff:
            continue
        return True
    if await _probe_sr_blind_spot_any_trip(session, project_id):
        return True
    if await _probe_strategic_stall_any_trip(session, project_id):
        return True
    if await _collect_viewport_failures(session, project_id):
        return True
    if _probe_viewport_coverage_drop(project_design_doc):
        return True
    vm_failures = await _collect_agent_vm_failures(session, project_id)
    if len(vm_failures) >= _AGENT_VM_RESOURCE_MIN_FAILURES:
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


async def _ensure_watchdog_alert_open(
    session: AsyncSession,
    project_id: uuid.UUID,
    title_prefix: str,
    title: str,
    message: str,
    severity: AlertSeverity,
) -> None:
    """Open a watchdog alert if none is already open (deduped by title prefix)."""
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title.like(title_prefix + "%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        session.add(
            Alert(
                project_id=project_id,
                severity=severity,
                title=title[:200],
                message=message[:1000],
            )
        )
        await session.commit()


async def _resolve_watchdog_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    title_prefix: str,
) -> None:
    """Auto-resolve an open watchdog alert by setting dismissed_at."""
    open_alert = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title.like(title_prefix + "%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if open_alert is not None:
        open_alert.dismissed_at = _utcnow()
        await session.commit()


async def _check_cross_layer_backfill_lag(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> None:
    """Emit an INFO alert when cross-layer fix-log entries sit unbackfilled too long.

    Reads three runtime-config knobs:
      cross_layer_backfill_alert_threshold_hours (int, default 24)
      cross_layer_backfill_alert_dedupe_hours    (int, default 24)
      cross_layer_backfill_alert_disabled_projects (list[str] of project IDs)
    """
    threshold_hours = int(
        _runtime_config.get("cross_layer_backfill_alert_threshold_hours", 24) or 24
    )
    dedupe_hours = int(
        _runtime_config.get("cross_layer_backfill_alert_dedupe_hours", 24) or 24
    )
    disabled_projects = list(
        _runtime_config.get("cross_layer_backfill_alert_disabled_projects", []) or []
    )

    now = _utcnow()
    threshold_dt = now - timedelta(hours=threshold_hours)

    for pid in project_ids:
        if str(pid) in disabled_projects:
            continue

        cases = (
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )

        stale_count = 0
        oldest_merged_at: datetime | None = None

        for case in cases:
            for entry in case.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("spawn_was_cross_layer"):
                    continue
                if "layers_targeted" in entry:
                    continue
                merged_at_raw = entry.get("merged_at")
                if not merged_at_raw:
                    continue
                try:
                    merged_dt = _to_naive_utc(
                        datetime.fromisoformat(
                            str(merged_at_raw).replace("Z", "+00:00")
                        )
                    )
                except (ValueError, TypeError):
                    continue
                if merged_dt >= threshold_dt:
                    continue
                stale_count += 1
                if oldest_merged_at is None or merged_dt < oldest_merged_at:
                    oldest_merged_at = merged_dt

        if stale_count > 0:
            # Check dedupe: dismiss stale open alert if outside window, then re-emit.
            dedupe_cutoff = now - timedelta(hours=dedupe_hours)
            open_alert = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == pid,
                            Alert.title.like(
                                _CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX + "%"
                            ),
                            Alert.dismissed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if open_alert is not None:
                if open_alert.created_at >= dedupe_cutoff:
                    # Within dedupe window — skip to avoid duplicate
                    continue
                # Outside dedupe window: dismiss the stale one, emit fresh
                open_alert.dismissed_at = now
                await session.commit()

            oldest_str = (
                oldest_merged_at.date().isoformat() if oldest_merged_at else "unknown"
            )
            await _ensure_watchdog_alert_open(
                session,
                pid,
                _CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX,
                f"{_CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX} {stale_count} entries unbackfilled",
                (
                    f"{stale_count} cross-layer fix-log entries unbackfilled for "
                    f">{threshold_hours}h, oldest merged_at={oldest_str}; "
                    "compliance_rate may be biased"
                ),
                AlertSeverity.INFO,
            )
        else:
            await _resolve_watchdog_alert(
                session, pid, _CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX
            )


async def run_dispatcher_watchdog_alarms(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> None:
    """Check cycle-staleness and zero-spawn-streak alarms for all projects.

    Must be called BEFORE the per-project run_once loop so the staleness alarm
    fires even when run_once hangs or raises on the subsequent tick.
    """
    await _check_cross_layer_backfill_lag(session, project_ids)

    now_mono = time.monotonic()

    for pid in project_ids:
        pid_str = str(pid)

        # --- Alarm 1: cycle staleness (HIGH / ERROR) ---
        last_mono = _last_cycle_monotonic.get(pid_str)
        if last_mono is not None:
            elapsed = now_mono - last_mono
            if elapsed > _DISPATCHER_STALENESS_THRESHOLD_SECONDS:
                await _ensure_watchdog_alert_open(
                    session,
                    pid,
                    _STALENESS_ALERT_TITLE_PREFIX,
                    f"{_STALENESS_ALERT_TITLE_PREFIX} {int(elapsed)}s since last cycle",
                    f"Dispatcher has not completed a cycle for project {pid} in "
                    f"{int(elapsed)}s (threshold {_DISPATCHER_STALENESS_THRESHOLD_SECONDS}s). "
                    "The dispatcher session may be wedged or the orchestrator tick stalled.",
                    AlertSeverity.ERROR,
                )
            else:
                await _resolve_watchdog_alert(
                    session, pid, _STALENESS_ALERT_TITLE_PREFIX
                )
        # No baseline yet → don't alert; clock starts from first completed cycle.

        # --- Alarm 2: zero-spawn streak (MEDIUM / WARNING) ---
        # Read the most recent K cycle rows from the durable table so the
        # streak detection survives worker restarts. The query takes the
        # newest K and we evaluate the streak in any order — the "streak
        # broken" predicate is order-independent.
        recent = await get_dispatcher_cycle_records(
            session,
            project_id=pid_str,
            limit=_DISPATCHER_ZERO_SPAWN_STREAK_K,
        )
        if len(recent) >= _DISPATCHER_ZERO_SPAWN_STREAK_K:
            streak_broken = any(
                r["spawns_emitted"] > 0 or r.get("signals_tripped", 0) == 0
                for r in recent
            )
            if not streak_broken:
                await _ensure_watchdog_alert_open(
                    session,
                    pid,
                    _ZERO_SPAWN_ALERT_TITLE_PREFIX,
                    f"{_ZERO_SPAWN_ALERT_TITLE_PREFIX} {_DISPATCHER_ZERO_SPAWN_STREAK_K} consecutive cycles suppressed",
                    f"The last {_DISPATCHER_ZERO_SPAWN_STREAK_K} dispatcher cycles for project {pid} "
                    "all had tripped signals but zero spawns (cooldown/cap/dedup/manual-intervention). "
                    "Review active remediation cases or adjust spawn-suppression config.",
                    AlertSeverity.WARNING,
                )
            else:
                await _resolve_watchdog_alert(
                    session, pid, _ZERO_SPAWN_ALERT_TITLE_PREFIX
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

        _spawned += await _evaluate_sr_catch_rate_signal(
            session,
            project_id,
            project.design_doc,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_sr_false_positive_signal(
            session,
            project_id,
            project.design_doc,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_sr_catch_rate_direction_signals(
            session,
            project_id,
            project.design_doc,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_fix_loop_divergence_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_fix_loop_divergence_fingerprint_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_pipeline_success_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_anomaly_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_sr_blind_spot_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_layer_concentration_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_cross_case_fingerprint_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_recurring_class_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_strategic_cycle_stall_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_telemetry_staleness_signal(
            session,
            project_id,
            project.design_doc,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_viewport_regression_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_viewport_coverage_drop_signal(
            session,
            project_id,
            project.design_doc,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_reviewer_sla_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_agent_vm_resource_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )
        _spawned += await _evaluate_low_prevention_rate_signal(
            session,
            project_id,
            spawn_budget - _spawned,
        )

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
        from app.models import DispatcherCycleRecord  # noqa: PLC0415

        try:
            session.add(
                DispatcherCycleRecord(
                    project_id=project_id,
                    cycle_started_at=cycle_started_at,
                    cycle_finished_at=_utcnow(),
                    signals_evaluated=0,
                    signals_tripped=_st_counter[0],
                    spawns_emitted=_spawned,
                    rejections_by_reason=dict(_rj),
                )
            )
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "[REGRESSION_DISPATCHER] cycle-record write failed", exc_info=True
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
        # Periodic prune so DispatcherCycleRecord doesn't grow unboundedly.
        # Cheap when the index hits no expired rows; failures are swallowed.
        await _prune_old_dispatcher_cycle_records(session)
        _last_cycle_monotonic[str(project_id)] = time.monotonic()


async def get_regression_mtta_data(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    """Return MTTA lifecycle stats per regression class.

    Computes p50/p90 per lifecycle stage (signal-trip → detect → investigate →
    PR-merge → signal-recovery) from historical cases, and enriches active cases
    with live elapsed time plus predicted ETA derived from the class's historical
    p50 distribution.  Active cases whose p50 has been exceeded carry a negative
    ETA (overdue indicator).
    """
    from collections import defaultdict  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    cutoff = _utcnow() - timedelta(days=window_days)
    where: list[Any] = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(*where).limit(2000)
            )
        )
        .scalars()
        .all()
    )

    now = _utcnow()

    def _pct(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        return s[min(int(len(s) * p), len(s) - 1)]

    def _parse_dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except (ValueError, TypeError):
            return None

    class_cases: dict[str, list] = defaultdict(list)
    for c in cases:
        class_cases[c.regression_class].append(c)

    rows = []
    for rclass, cls_cases in class_cases.items():
        detect_s: list[float] = []
        invest_s: list[float] = []
        itom_s: list[float] = []
        mtor_s: list[float] = []
        active_info: list[dict[str, Any]] = []

        for c in cls_cases:
            if (ttd := c.time_to_detect_sec) is not None and ttd >= 0:
                detect_s.append(ttd)
            if (tti := c.time_to_investigate_sec) is not None and tti >= 0:
                invest_s.append(float(tti))
            for entry in c.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                merged_dt = _parse_dt(entry.get("merged_at"))
                recovered_dt = _parse_dt(entry.get("recovered_at"))
                if merged_dt and c.investigate_spawned_at:
                    d = (merged_dt - c.investigate_spawned_at).total_seconds()
                    if d >= 0:
                        itom_s.append(d)
                if merged_dt and recovered_dt:
                    d2 = (recovered_dt - merged_dt).total_seconds()
                    if d2 >= 0:
                        mtor_s.append(d2)

            if c.status == RegressionCaseStatus.CLOSED:
                continue
            anchor = (
                c.signal_first_crossed_at or c.signal_first_seen_at or c.first_seen_at
            )
            live_elapsed = round((now - anchor).total_seconds()) if anchor else None
            live_inv = (
                round((now - c.investigate_spawned_at).total_seconds())
                if c.investigate_spawned_at
                else None
            )
            rec_check_in: int | None = None
            elapsed_since_merge: int | None = None
            if c.status == RegressionCaseStatus.MERGED:
                last_entry = next(
                    (
                        e
                        for e in reversed(c.fingerprint_to_fix_log or [])
                        if isinstance(e, dict) and e.get("merged_at")
                    ),
                    None,
                )
                if last_entry and (m := _parse_dt(last_entry["merged_at"])):
                    secs = int(
                        (m + _REGRESSION_RECOVERY_COOLDOWN - now).total_seconds()
                    )
                    rec_check_in = secs if secs > 0 else None
                    elapsed_since_merge = int((now - m).total_seconds())
            active_info.append(
                {
                    "case_id": str(c.id),
                    "status": c.status,
                    "live_elapsed_sec": live_elapsed,
                    "live_elapsed_investigate_sec": live_inv,
                    "recovery_check_in_sec": rec_check_in,
                    "elapsed_since_merge_sec": elapsed_since_merge,
                }
            )

        p50_detect = _pct(detect_s, 0.5)
        p50_invest = _pct(invest_s, 0.5)
        p50_itom = _pct(itom_s, 0.5)
        p50_mtor = _pct(mtor_s, 0.5)

        # Enrich active cases with ETA predictions based on class historical p50.
        # Negative ETAs indicate the case has exceeded the typical p50 duration.
        for ai in active_info:
            ai["eta_to_investigate_sec"] = (
                int(p50_detect - ai["live_elapsed_sec"])
                if p50_detect is not None and ai["live_elapsed_sec"] is not None
                else None
            )
            ai["eta_to_merge_sec"] = (
                int(p50_itom - ai["live_elapsed_investigate_sec"])
                if p50_itom is not None
                and ai["live_elapsed_investigate_sec"] is not None
                else None
            )
            ai["eta_to_recovery_sec"] = (
                int(p50_mtor - ai["elapsed_since_merge_sec"])
                if p50_mtor is not None and ai["elapsed_since_merge_sec"] is not None
                else None
            )

        rows.append(
            {
                "regression_class": rclass,
                "sample_count": len(cls_cases),
                "stages": {
                    "signal_trip_to_detect": {
                        "p50_sec": p50_detect,
                        "p90_sec": _pct(detect_s, 0.9),
                        "sample_count": len(detect_s),
                    },
                    "detect_to_investigate": {
                        "p50_sec": p50_invest,
                        "p90_sec": _pct(invest_s, 0.9),
                        "sample_count": len(invest_s),
                    },
                    "investigate_to_merge": {
                        "p50_sec": p50_itom,
                        "p90_sec": _pct(itom_s, 0.9),
                        "sample_count": len(itom_s),
                    },
                    "merge_to_recovery": {
                        "p50_sec": p50_mtor,
                        "p90_sec": _pct(mtor_s, 0.9),
                        "sample_count": len(mtor_s),
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


async def get_sr_persistent_blind_spot_hint(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int = 14,
) -> dict[str, Any] | None:
    """Return the persistent SR blind-spot hint for agent task context injection.

    Queries the last *days* of completed tasks and calls
    ``sr_blind_spots.compute_persistent_sr_blind_spot_hint`` to identify
    categories whose reviewer-disagreement rate exceeds the threshold in >=2
    consecutive 7-day windows.  Returns None when no persistent categories
    exist (i.e. below-threshold or insufficient sample size).
    """
    from app.sr_blind_spots import compute_persistent_sr_blind_spot_hint  # noqa: PLC0415

    now = _utcnow()
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at >= now - timedelta(days=days),
                )
            )
        )
        .scalars()
        .all()
    )
    result = compute_persistent_sr_blind_spot_hint(tasks, now=now)
    return result if result.get("categories") else None
