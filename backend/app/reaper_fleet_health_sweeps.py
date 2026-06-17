"""Fleet-level health monitoring sweeps for the reaper.

Extracted from reaper_sweeps.py. Functions monitor degradation patterns
at the fleet level and raise operator alerts when thresholds are breached.
"""

import logging
import uuid as _uuid
from datetime import timedelta

import app.reaper_sweeps as _rs

from app.time_utils import _is_cadence_due, _utcnow

logger = logging.getLogger(__name__)


async def sweep_merge_gate_verification_degradation(session) -> int:
    """Alert fleet-wide when the no-CI merge-gate verification rate degrades.

    Computes the fleet's recent-window merge-gate health
    (:func:`digest_metrics.compute_merge_gate_fleet_health`) and, when the
    verification rate has collapsed and/or too many projects are degraded over a
    meaningful sample, broadcasts a deduplicated operator alert. A systemically
    broken gate merges 'done' PRs UNVERIFIED at scale while each project looks
    individually unremarkable; today an operator only catches that by chance on
    the dashboard. This raises the proactive alert every other fleet-level signal
    already has. Alerting only — it never influences a merge decision.

    Trigger (over the recent window, gated on a minimum sample so a single
    fall-open never fires): verification rate below ``merge_gate_degradation_min_rate``
    OR at least ``merge_gate_degradation_min_degraded_projects`` projects degraded.
    Severity escalates to ERROR when the dominant fall-open reason is a degraded
    environment/setup failure (reusing ``_severity_for_unverified_reason``), else
    WARNING. Returns the number of NEW alerts created across the fleet.
    """
    _now_scan = _utcnow()
    if not _is_cadence_due(
        _rs._last_merge_gate_degradation_scan, _rs._FLEET_SCAN_INTERVAL_SECONDS, _now_scan
    ):
        return 0
    _rs._last_merge_gate_degradation_scan = _now_scan

    from app.alert_dispatch import (
        _maybe_create_merge_gate_degradation_alerts,
        _severity_for_unverified_reason,
    )
    from app.digest_metrics import compute_merge_gate_fleet_health
    from app.models import AlertSeverity
    from app.shared_state import runtime_config

    window_days = int(runtime_config.get("merge_gate_degradation_window_days", 7))
    min_samples = int(runtime_config.get("merge_gate_degradation_min_samples", 10))
    min_rate = float(runtime_config.get("merge_gate_degradation_min_rate", 0.5))
    min_degraded = int(
        runtime_config.get("merge_gate_degradation_min_degraded_projects", 3)
    )

    window_start = _utcnow() - timedelta(days=window_days)
    health = await compute_merge_gate_fleet_health(session, window_start=window_start)

    total = health["verified_count"] + health["fell_open_count"]
    if total < min_samples:
        return 0
    rate = health["verification_rate"]
    degraded = health["degraded_project_count"]
    rate_is_low = rate is not None and rate < min_rate
    too_many_degraded = degraded >= min_degraded
    if not (rate_is_low or too_many_degraded):
        return 0

    reason = health["most_common_reason"]
    severity = (
        _severity_for_unverified_reason(reason) if reason else AlertSeverity.WARNING
    )
    created = await _maybe_create_merge_gate_degradation_alerts(
        session,
        verification_rate=rate,
        verified_count=health["verified_count"],
        fell_open_count=health["fell_open_count"],
        degraded_project_count=degraded,
        project_count=health["project_count"],
        most_common_reason=reason,
        severity=severity,
    )
    if created:
        await session.commit()
        logger.info(
            "[REAPER] raised fleet merge-gate degradation alert on %d project(s) "
            "(rate=%s, degraded=%d/%d, reason=%s)",
            created,
            rate,
            degraded,
            health["project_count"],
            reason,
        )
    return created


async def sweep_migration_slot_contention(session) -> int:
    """Alert per-project when a migration task has held the serialization slot too
    long while schema-touching tasks are parked waiting on it.

    Computes fleet-wide migration-slot contention
    (:func:`digest_metrics.compute_migration_slot_contention`) and, for each
    project whose holder has held the slot beyond
    ``migration_slot_hold_alert_seconds`` while at least
    ``migration_slot_min_waiters`` tasks are deferred, raises a deduplicated
    operator alert. A multi-hour hold parking the schema queue (the recent
    incident: ~2h holding, 5+ tasks parked) is invisible to the throughput-only
    migration-health summary and to the escalated-only parked-blocker signal —
    the deferred siblings are QUEUED, not BLOCKED. This raises the proactive
    signal every other contention/degradation surface already has. Alerting only —
    it never influences serialization. Returns the number of NEW alerts created.
    """
    from app.alert_dispatch import _maybe_create_migration_slot_contention_alert
    from app.digest_metrics import compute_migration_slot_contention
    from app.shared_state import runtime_config

    hold_alert_seconds = float(
        runtime_config.get("migration_slot_hold_alert_seconds", 3600)
    )
    min_waiters = int(runtime_config.get("migration_slot_min_waiters", 1))

    contention = await compute_migration_slot_contention(session)
    created = 0
    for proj in contention.get("projects", []):
        if not proj.get("slot_held"):
            continue
        held = proj.get("holder_held_seconds")
        waiting = proj.get("waiting_count", 0)
        if held is None or held < hold_alert_seconds or waiting < min_waiters:
            continue
        pid_raw = proj.get("project_id")
        if not pid_raw:
            continue
        try:
            pid = _uuid.UUID(str(pid_raw))
        except (ValueError, TypeError):
            continue
        created += await _maybe_create_migration_slot_contention_alert(
            session,
            pid,
            holder_task_id=proj.get("holder_task_id"),
            held_seconds=held,
            waiting_count=waiting,
            longest_wait_seconds=proj.get("longest_wait_seconds"),
        )
    if created:
        await session.commit()
        logger.info(
            "[REAPER] raised migration-slot-contention alert on %d project(s)",
            created,
        )
    return created
