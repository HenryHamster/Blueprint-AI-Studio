"""Leader-only pipeline-alert scan service.

This module re-homes the deterministic operator-Alert emitters that used to live
inside the pipeline-regression dispatcher / recovery-watchdog apparatus (now
removed). It does NOT detect-investigate-fix anything: every surviving signal is
preserved purely as a WARNING ``Alert`` (the existing Alert + webhook path),
deduped by title, so an operator sees it without any autonomous remediation.

Emitters re-homed / added here:

* ``_run_signal_staleness_watchdog`` — warns when a known telemetry signal source
  has stopped updating (formerly ``regression_recovery_watchdog``). The
  ``never_sufficient`` sr_catch_rate sub-alert was intentionally dropped (a noisy
  signal that never produced an actionable operator decision).
* ``_evaluate_sr_catch_rate_targets`` / ``_evaluate_lone_checker_alert`` — invoked
  from ``app.pipeline_health`` (the functions themselves stay there).
* ``_emit_agent_vm_resource_alert`` — VM-resource failure cluster, classified via
  ``failure_classification.classify_vm_exit_reason`` (copied out of the deleted
  ``dispatcher_signal_data._collect_agent_vm_failures``).
* ``_emit_pipeline_success_rate_alert`` — pipeline success-rate cliff, off
  ``pipeline_health.compute_pipeline_health_summary``.

The ``background_service_crash`` Alert already fires in
``lifecycle._monitor_background_tasks`` and is not duplicated here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import app.shared_state as _shared_state
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    ProjectSRAnalytics,
    Task,
    TaskStatus,
)
from app.pipeline_health import (
    _evaluate_lone_checker_alert,
    _evaluate_sr_catch_rate_targets,
    compute_pipeline_health_summary,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Cadence: 5-30 min is plenty — staleness windows are measured in hours and the
# success-rate / VM-resource clusters are 24h windows.
_SCAN_INTERVAL_SECONDS = 600  # 10 minutes

# Telemetry-signal freshness windows (re-homed from the deleted
# regression_recovery_evaluators.TELEMETRY_SIGNAL_SPECS). A signal whose source
# has not refreshed within its window emits a "Signal staleness" WARNING Alert.
_SIGNAL_STALENESS_WINDOWS: dict[str, timedelta] = {
    "sr_catch_rate": timedelta(hours=48),
    "fix_loop_divergence": timedelta(days=7),
    "persistent_anomaly": timedelta(hours=48),
    "deploy_health": timedelta(hours=6),
    "sr_blind_spot": timedelta(days=7),
}

# Frozen-signal detection for sr_catch_rate: the signal heartbeat is fresh but the
# underlying sufficiency data has not refreshed — the trend is frozen.
_SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW = timedelta(hours=24)
_SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW = timedelta(hours=6)

# agent_vm_resource cluster window (re-homed from the deleted
# dispatcher_signal_data._AGENT_VM_RESOURCE_WINDOW_HOURS).
_AGENT_VM_RESOURCE_WINDOW_HOURS = 24
_AGENT_VM_RESOURCE_ALERT_TITLE = "Agent-VM resource failures elevated"
_AGENT_VM_RESOURCE_MIN_FAILURES = 3

# pipeline_success_rate cliff. Mirrors the prior dispatcher minimum sample of 10.
_PIPELINE_SUCCESS_MIN_SAMPLE = 10
_PIPELINE_SUCCESS_RATE_FLOOR_DEFAULT = 0.5
_PIPELINE_SUCCESS_RATE_ALERT_TITLE = "Pipeline success-rate cliff"


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO timestamp into naive UTC (re-homed from dispatcher_formatters)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


async def _alert_exists(
    session: AsyncSession,
    project_id: uuid.UUID,
    title: str,
) -> bool:
    """True when an open WARNING Alert with this exact title already exists."""
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
    return existing is not None


async def _run_signal_staleness_watchdog(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict[str, Any] | None,
) -> int:
    """Emit warning alerts when a known signal source has stopped updating.

    Re-homed from ``regression_recovery_watchdog._run_signal_staleness_watchdog``.
    The ``never_sufficient`` sr_catch_rate sub-alert was deliberately dropped.
    """
    doc = project_design_doc or {}
    now = _utcnow()
    windows = _SIGNAL_STALENESS_WINDOWS
    specs: list[tuple[str, Any, timedelta]] = [
        (
            "sr_catch_rate",
            (doc.get("sr_catch_rate_trends") or {}).get("last_sufficient_computed_at"),
            windows["sr_catch_rate"],
        ),
        (
            "fix_loop_divergence",
            doc.get("fix_loop_divergence_signal_last_computed_at")
            or doc.get("fix_loop_divergence_detected_at"),
            windows["fix_loop_divergence"],
        ),
        (
            "persistent_anomaly",
            (
                (_shared_state.persistent_anomalies.get(str(project_id)) or {}).get(
                    "signal_last_computed_at"
                )
            ),
            windows["persistent_anomaly"],
        ),
        (
            "deploy_health",
            _shared_state.deploy_monitoring.get("signal_last_computed_at"),
            windows["deploy_health"],
        ),
        (
            "sr_blind_spot",
            doc.get("sr_blind_spot_signal_last_computed_at"),
            windows["sr_blind_spot"],
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
        if not await _alert_exists(session, project_id, title):
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=title,
                    message=message,
                    event_type="sr.signal_stale",
                )
            )
            created += 1
    # Frozen-signal sub-alert: heartbeat is fresh but the real data is stale.
    sr_trends = doc.get("sr_catch_rate_trends") or {}
    signal_ts = sr_trends.get("signal_last_computed_at")
    sufficient_ts = sr_trends.get("last_sufficient_computed_at")
    if signal_ts and sufficient_ts:
        signal_at = _parse_iso_timestamp(signal_ts)
        sufficient_at = _parse_iso_timestamp(sufficient_ts)
        if (
            signal_at is not None
            and sufficient_at is not None
            and (now - signal_at) <= _SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW
            and (now - sufficient_at) > _SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW
        ):
            title = "Signal frozen: sr_catch_rate"
            age = now - sufficient_at
            message = (
                "sr_catch_rate signal is computing (fresh heartbeat) but real data "
                f"has not refreshed in {round(age.total_seconds() / 3600, 1)}h. "
                "The trend is frozen."
            )
            if not await _alert_exists(session, project_id, title):
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=title,
                        message=message,
                        event_type="sr.signal_stale",
                    )
                )
                created += 1
    # NOTE: the ``never_sufficient`` sr_catch_rate sub-alert (fired when a metric
    # was computed but never reached data sufficiency) was intentionally removed —
    # it never produced an actionable operator decision.
    return created


async def _collect_agent_vm_failures(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict]:
    """Return recent FAILED tasks classified by VM-resource exit reason.

    Copied out of the deleted ``dispatcher_signal_data._collect_agent_vm_failures``.
    Inspects Task.context via ``failure_classification.classify_vm_exit_reason``;
    only failures attributable to the AGENT_VM infrastructure layer are included
    (code-level timeouts are excluded).

    Exit-reason labels: 'oom', 'timeout', 'spot_preempt', 'vm_provisioning_failure'.
    """
    from app.failure_classification import classify_vm_exit_reason

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


async def _emit_agent_vm_resource_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Emit a deduped WARNING Alert when agent-VM resource failures are elevated."""
    failures = await _collect_agent_vm_failures(session, project_id)
    if len(failures) < _AGENT_VM_RESOURCE_MIN_FAILURES:
        return 0
    title = _AGENT_VM_RESOURCE_ALERT_TITLE
    if await _alert_exists(session, project_id, title):
        return 0
    by_reason: dict[str, int] = {}
    for f in failures:
        reason = f.get("exit_reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    breakdown = ", ".join(
        f"{reason}={count}"
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1])
    )
    message = (
        f"{len(failures)} agent-VM resource failures in the last "
        f"{_AGENT_VM_RESOURCE_WINDOW_HOURS}h (oom / timeout / spot-preempt / "
        f"vm-provisioning). Breakdown: {breakdown}. These are infrastructure-layer "
        "failures, not code defects — review VM sizing, spot-preemption rate, or "
        "provisioning quota."
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=message[:1000],
            event_type="vm.low_memory",
        )
    )
    return 1


async def _emit_pipeline_success_rate_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Emit a deduped WARNING Alert when the 24h pipeline success-rate is below floor."""
    floor = float(
        _runtime_config.get(
            "pipeline_success_rate_alert_floor", _PIPELINE_SUCCESS_RATE_FLOOR_DEFAULT
        )
        or _PIPELINE_SUCCESS_RATE_FLOOR_DEFAULT
    )
    summary = await compute_pipeline_health_summary(
        session, project_id, lookback_hours=24
    )
    success_rate = summary.get("success_rate")
    if success_rate is None:
        return 0

    # Non-trivial sample gate: only fire when the 24h terminal-task count clears
    # the minimum so a 1-of-2 fluke does not produce a cliff alert.
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    sample_size = (
        await session.execute(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.updated_at >= window_start,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            )
        )
    ).all()
    if len(sample_size) < _PIPELINE_SUCCESS_MIN_SAMPLE:
        return 0
    if success_rate >= floor:
        return 0

    title = _PIPELINE_SUCCESS_RATE_ALERT_TITLE
    if await _alert_exists(session, project_id, title):
        return 0
    failure_distribution = summary.get("failure_distribution") or {}
    top = ", ".join(f"{k}={v}" for k, v in list(failure_distribution.items())[:3])
    top_block = f" Top failure classes: {top}." if top else ""
    message = (
        f"24h pipeline success-rate is {success_rate:.0%} (n={len(sample_size)} "
        f"terminal tasks), below the {floor:.0%} floor.{top_block} Trend: "
        f"{summary.get('trend') or 'unknown'}."
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=message[:1000],
            event_type="task.failed",
        )
    )
    return 1


async def run_pipeline_alert_scan_once(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Run all deterministic Alert emitters for one project. Returns alerts created.

    Each emitter is isolated AND commits independently the moment it succeeds, so
    a later emitter raising can never roll back an earlier emitter's already-added
    (but uncommitted) Alert. That shared-session rollback was the bug: ``created``
    was incremented before the single end-of-scan commit, so one failing emitter
    silently discarded a healthy emitter's Alert while the count still reported it.
    Committing per emitter keeps the returned count exactly equal to the number of
    Alert rows that actually persisted.
    """
    project = await session.get(Project, project_id)
    if project is None:
        return 0
    _pas_sr_row = await session.get(ProjectSRAnalytics, project_id)
    _pas_sr_data = _pas_sr_row.data if _pas_sr_row else None
    design_doc = _pas_sr_data or (project.design_doc or {})

    created = 0
    emitters: list[tuple[str, Any]] = [
        ("signal_staleness", _run_signal_staleness_watchdog),
        ("sr_catch_rate_targets", _evaluate_sr_catch_rate_targets),
        ("lone_checker", _evaluate_lone_checker_alert),
        ("agent_vm_resource", _emit_agent_vm_resource_alert),
        ("pipeline_success_rate", _emit_pipeline_success_rate_alert),
    ]
    for name, fn in emitters:
        try:
            if name == "signal_staleness":
                added = await fn(session, project_id, design_doc) or 0
                await session.commit()
                created += added
            elif name in ("sr_catch_rate_targets", "lone_checker"):
                # These commit internally and return None.
                await fn(session, project_id)
            else:
                added = await fn(session, project_id) or 0
                await session.commit()
                created += added
        except Exception as exc:  # noqa: BLE001,PERF203
            logger.warning(
                "[PIPELINE_ALERT_SCAN] emitter=%s project=%s raised: %s",
                name,
                project_id,
                exc,
            )
            # Only this emitter's uncommitted work is discarded — every prior
            # emitter already committed, so its Alert (and the count) survives.
            with contextlib.suppress(Exception):
                await session.rollback()
    return created


async def pipeline_alert_scan_service() -> None:
    """Leader-only periodic scan: emits deterministic operator Alerts per project.

    Replaces the deleted ``_regression_dispatch_service`` loop. Enumerates
    autonomous projects and runs the Alert emitters on a fresh per-project session
    so one project's failure cannot poison the rest.
    """
    while True:
        await asyncio.sleep(_SCAN_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as _id_session:
                project_ids = (
                    (
                        await _id_session.execute(
                            select(Project.id).where(
                                Project.autonomous_enabled.is_(True),
                                Project.not_archived(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            for project_id in project_ids:
                try:
                    async with AsyncSessionLocal() as scan_session:
                        created = await run_pipeline_alert_scan_once(
                            scan_session, project_id
                        )
                        if created:
                            logger.info(
                                "[PIPELINE_ALERT_SCAN] project=%s alerts_created=%d",
                                project_id,
                                created,
                            )
                except Exception as proj_exc:  # noqa: BLE001,PERF203
                    logger.warning(
                        "[PIPELINE_ALERT_SCAN] project=%s error=%s",
                        project_id,
                        proj_exc,
                    )
        except Exception as scan_exc:
            logger.error(
                "[PIPELINE_ALERT_SCAN] scan error: %s", scan_exc, exc_info=True
            )
