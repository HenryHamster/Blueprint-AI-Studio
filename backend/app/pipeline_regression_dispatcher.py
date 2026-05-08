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
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Cooldown between consecutive spawns for the SAME regression_class. Mirrors
# the self_audit_dispatch cooldown pattern — keeps a noisy regression from
# generating an investigate every cycle once the prior task terminates.
_REGRESSION_COOLDOWN = timedelta(hours=6)

# Per-cycle system-wide concurrency budget. A noisy regression cannot starve
# the rest of the pipeline by occupying every research slot.
_REGRESSION_DISPATCHER_SYSTEM_CAP = 2

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

# SR blind-spot theme occurrence floor. A blind_spot theme must appear at least
# this many times across the 14d window before it is considered a structural gap
# in the self-review prompt rather than a one-off observation from a single task.
_SR_BLIND_SPOT_THEME_FLOOR = 5

# Lookback window for the project-wide layer-defense ledger injected into meta-investigation
# prompts. Covers ~3 months of defense activity across all regression classes for the project.
_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS = 90

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

_ACTIVE_INVESTIGATE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)

_CANONICAL_RECURRENCE_LAYERS = (
    "CI_WORKFLOW",
    "AGENT_VM",
    "AGENT_VERB",
    "BASELINE_GATE",
    "REVIEWER_PASS",
)

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
        }
        for r in rows
    ]


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
        "\nDiagnose the underlying root cause of this regression and produce "
        "the structured diagnosis JSON report ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the "
        "remediation."
    )
    return "\n".join(lines)


def _format_prior_attempt_context(case: RegressionRemediationCase) -> str:
    """Build a prior-attempt notice to append when re-spawning after >= 2 failures.

    Only called when existing_case.attempts >= 2 so the first re-arm (attempts==1)
    keeps the original prompt unchanged — this avoids noise on the second attempt
    and reserves the enrichment for cases that have already failed twice.
    """
    lines = [
        "",
        "PRIOR ATTEMPT CONTEXT",
        f"This regression has been investigated {case.attempts} time(s) without recovering the signal.",
    ]
    pr = case.last_remediation_pr_number
    if pr is not None:
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
        for layer in _CANONICAL_RECURRENCE_LAYERS
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
    for layer in _CANONICAL_RECURRENCE_LAYERS:
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


def _build_layer_defense_ledger(
    all_fix_logs: list[dict[str, Any]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> str:
    """Build a RECENT DEFENSE ACTIVITY BY LAYER block for meta-investigation prompts.

    Aggregates fingerprint_to_fix_log entries from all RegressionRemediationCase rows
    for a project, filters to merged_at within lookback_days, and computes per-layer
    statistics. All five canonical layers always appear. Returns empty string when
    all_fix_logs is empty.
    """
    if not all_fix_logs:
        return ""

    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    layer_stats: dict[str, dict[str, Any]] = {
        layer: {
            "fix_count": 0,
            "hold_days": [],  # hold durations for re-tripped entries (days)
            "still_holding_ages": [],  # age in days for still-holding entries
            "retripped_within_48h": 0,
            "held_gt_7d": 0,
        }
        for layer in _CANONICAL_RECURRENCE_LAYERS
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

        for layer in layers_targeted:
            if layer not in layer_stats:
                continue
            s = layer_stats[layer]
            s["fix_count"] += 1
            if recovered_dt is not None:
                hold = (recovered_dt - merged_dt).total_seconds() / 86400.0
                s["hold_days"].append(hold)
                if hold < 2.0:
                    s["retripped_within_48h"] += 1
                if hold > 7.0:
                    s["held_gt_7d"] += 1
            else:
                s["still_holding_ages"].append(age_days)
                if age_days > 7.0:
                    s["held_gt_7d"] += 1

    lines: list[str] = [
        "",
        f"RECENT DEFENSE ACTIVITY BY LAYER (last {lookback_days} days)",
    ]
    for layer in _CANONICAL_RECURRENCE_LAYERS:
        s = layer_stats[layer]
        if s["fix_count"] == 0:
            lines.append(
                f"  {layer}: no defenses added here in the last {lookback_days} days"
            )
        else:
            parts = [f"  {layer}: {s['fix_count']} defense(s) added"]
            hold_days_list = s["hold_days"]
            if hold_days_list:
                avg_hold = sum(hold_days_list) / len(hold_days_list)
                parts.append(f"avg hold {avg_hold:.1f}d before re-trip")
            if s["still_holding_ages"]:
                parts.append(f"{len(s['still_holding_ages'])} still holding")
            if s["held_gt_7d"] > 0:
                parts.append(f"{s['held_gt_7d']} held >7d")
            if s["retripped_within_48h"] > 0:
                parts.append(f"{s['retripped_within_48h']} retripped <48h (fragile)")
            lines.append("; ".join(parts))
    return "\n".join(lines)


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

    prior_layers_union: set[str] = set()
    for attempt in attempts:
        if attempt.get("layers_targeted_present"):
            prior_layers_union.update(attempt.get("layers_targeted") or [])
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
            "Enumerate ALL FIVE canonical layers explicitly:",
            ", ".join(_CANONICAL_RECURRENCE_LAYERS),
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
        "Enumerate ALL FIVE canonical layers explicitly:",
        ", ".join(_CANONICAL_RECURRENCE_LAYERS),
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
    elif signal_name in ("sr_catch_rate", "sr_blind_spot"):
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
            "Diagnose the SHARED root cause and propose a coordinated fix that "
            "addresses all of the layers below — do NOT bias toward whichever "
            "layer surfaced most recently. Each layer below shows one "
            "representative fingerprint + sample from this window."
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


# ---------------------------------------------------------------------------
# Path-based layer classification (for fix-log layers_targeted backfill)
# ---------------------------------------------------------------------------

# Bound for how many cases to backfill per dispatcher tick. The recovery scan
# runs every 60s and each call hits the GitHub API once per case; capping
# prevents a multi-hundred-case backlog from saturating the API budget.
_LAYER_BACKFILL_PER_TICK = 5


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
            or p == "check-viewport.sh"
        ):
            tags.add(LAYER_BASELINE_GATE)

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
            entry["layers_targeted"] = _classify_layers_from_paths(paths)
            log_changed = True
            mutated += 1
        if log_changed:
            case.fingerprint_to_fix_log = log
            flag_modified(case, "fingerprint_to_fix_log")
            session.add(case)

    if mutated:
        await session.commit()
    return mutated


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
    now = _utcnow()
    if last_spawn is not None and last_spawn.created_at:
        last_at = last_spawn.created_at
        if last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
        if now < last_at + _REGRESSION_COOLDOWN:
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
            if is_cross_layer:
                prompt = _format_recurrence_meta_prompt(
                    existing_case, prompt, layer_defense_block=_layer_defense_block
                )
            else:
                prompt = _format_recurrence_meta_prompt(
                    existing_case, base_prompt, layer_defense_block=_layer_defense_block
                )

    # SR blind-spot theme regressions already represent 5+ recurrences of the same
    # pattern; force the layer-enumeration meta block on every first dispatch so the
    # agent enumerates all five canonical layers rather than doing single-symptom triage.
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
        prompt += _format_prior_attempt_context(existing_case)
    payload_dict: dict[str, Any] = {
        "type": "investigate",
        "prompt": prompt,
        "base_branch": "main",
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
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "PIPELINE_REGRESSION_DISPATCHER",
                "regression_class": regression_class,
                "signal_name": signal_name,
            },
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
            )
        )
    else:
        existing_case.signal_snapshot_at_spawn = signal_snapshot
        existing_case.hit_count = (existing_case.hit_count or 0) + 1
        existing_case.last_seen_at = now
        existing_case.last_remediation_task_id = new_task.id
        existing_case.symptoms_by_layer = merged_symptoms
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
            case.mark_closed()
            # Reset the recovery clock so a future re-escalation does not
            # pick up a stale healthy_since on the same row (mirrors the
            # prior process-local ``_signal_healthy_since.pop`` semantics).
            case.signal_healthy_since_at = None
            case.fingerprint_to_fix_log = list(case.fingerprint_to_fix_log or []) + [
                {
                    "fingerprint": case.regression_class,
                    "recovered_at": now.isoformat(),
                    "post_merge_value": state["current_rate"],
                }
            ]
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
            case.re_arm()
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
            "base_branch": "main",
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

    mutated = 0
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

    auto_closed = await _auto_close_recovered_manual_intervention(session, project_id)
    return mutated + auto_closed


async def _auto_close_recovered_manual_intervention(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
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
    """
    from app.models import Project

    project = await session.get(Project, project_id)
    if project is None:
        return 0
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
        return 0

    window_hours = int(
        _runtime_config.get("regression_recovered_unattended_window_hours", 24) or 24
    )
    window = timedelta(hours=window_hours)
    now = _utcnow()
    auto_closed = 0
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
        locked.mark_closed()
        locked.last_remediation_result = CLOSE_REASON_RECOVERED_UNATTENDED
        locked.fingerprint_to_fix_log = list(locked.fingerprint_to_fix_log or []) + [
            {
                "fingerprint": locked.regression_class,
                "recovered_at": now.isoformat(),
                "post_recovery_value": post_value,
                "reason": CLOSE_REASON_RECOVERED_UNATTENDED,
            }
        ]
        # Cleared on close so the column never carries a stale value across
        # case lifecycles — mirrors the prior process-local ``.pop()`` calls.
        locked.signal_healthy_since_at = None
        session.add(locked)
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.INFO,
                title=f"Regression auto-recovered: {locked.regression_class}"[:200],
                message=(
                    f"NEEDS_MANUAL_INTERVENTION case for '{locked.regression_class}' "
                    f"auto-closed: underlying signal stayed below threshold for "
                    f"{window_hours}h without operator action."
                )[:1000],
            )
        )
        logger.info(
            "[REGRESSION_WATCHDOG] auto-recovered project=%s class=%s "
            "window_hours=%d post_value=%s",
            project_id,
            locked.regression_class,
            window_hours,
            post_value,
        )
        auto_closed += 1

    if auto_closed or mutated_rows:
        await session.commit()
    return auto_closed


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


async def run_dispatcher_watchdog_alarms(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> None:
    """Check cycle-staleness and zero-spawn-streak alarms for all projects.

    Must be called BEFORE the per-project run_once loop so the staleness alarm
    fires even when run_once hangs or raises on the subsequent tick.
    """
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
