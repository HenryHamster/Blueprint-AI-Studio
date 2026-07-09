"""Runner health-monitoring sweeps extracted from ``orchestrator_runner_dispatch``.

Owns the periodic sweeps that detect and alert on degraded runner states:
stale-runner-deactivation escalations, degraded runners, low-disk conditions,
idle-GPU conditions, and fleet-wide no-healthy-runner events.  All helpers take
the caller's ``session`` and lazily import alert primitives from
``app.alert_dispatch`` (which re-exports the runner alert helpers from
``app.runner_health_alerts``) to avoid load-time circular deps.

``_RUNNER_LIVENESS_SECONDS`` is the canonical definition of the runner
heartbeat window — used by both the dispatch and monitoring layers.  It is
re-exported from ``app.orchestrator_runner_dispatch`` so existing
``from app.orchestrator_runner_dispatch import _RUNNER_LIVENESS_SECONDS``
callers keep working without change.

Re-exported symbols:
  _RUNNER_LIVENESS_SECONDS, _RUNNER_DEGRADED_FAILURE_THRESHOLD,
  _RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT,
  _sweep_runner_deactivated_escalations,
  _check_low_disk_runners, _check_fleet_no_healthy_runner,
  _check_degraded_runners, _check_idle_gpu_runners
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import LocalRunner
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# A registered runner is considered live (eligible for dispatch) only if it has
# heartbeated within this window. Runners that miss heartbeats fall out of the
# dispatch cache and are swept to status='inactive'.
_RUNNER_LIVENESS_SECONDS = 300

# Consecutive-failure count at which a live runner is considered degraded.
# Mirrors admin_runners._FAILURE_STREAK_DEGRADED_THRESHOLD (both must stay in sync).
_RUNNER_DEGRADED_FAILURE_THRESHOLD = 3

# Default free-disk floor (bytes) below which a live runner is considered low-disk.
# 1 GB — configurable at runtime via ``runner_low_disk_threshold_bytes``.
_RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT = 1_073_741_824  # 1 GiB


async def _sweep_runner_deactivated_escalations(session: AsyncSession) -> None:
    """Escalate aging runner-outage alerts past the configured threshold.

    Queries open WARNING-severity runner-deactivated alerts older than
    ``runner_escalation_age_seconds`` (default 4h) and fires an escalated
    ERROR-severity notification for each unacknowledged runner that is still
    inactive. Called at the end of every ``_deactivate_stale_runners`` cycle.

    Flap suppression is natural: a runner that re-arms before the threshold
    dismisses its open alert (via ``_resolve_runner_deactivated_alert``), so this
    sweep finds nothing to escalate for a briefly-flapping runner.
    """
    from app.alert_dispatch import (
        _RUNNER_DEACTIVATED_EVENT_TYPE,
        _RUNNER_ESCALATION_AGE_SECONDS_DEFAULT,
        _maybe_escalate_runner_deactivated_alert,
    )
    from app.models import Alert, AlertSeverity

    threshold = int(
        _runtime_config.get(
            "runner_escalation_age_seconds", _RUNNER_ESCALATION_AGE_SECONDS_DEFAULT
        )
    )
    escalation_cutoff = _utcnow() - timedelta(seconds=threshold)
    rows = (
        await session.execute(
            select(Alert).where(
                Alert.event_type == _RUNNER_DEACTIVATED_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
                Alert.severity == AlertSeverity.WARNING,
                Alert.created_at <= escalation_cutoff,
            )
        )
    ).scalars().all()

    # Batch-fetch all referenced runners in a single query
    runner_ids: set[uuid.UUID] = set()
    for alert in rows:
        if not alert.dedupe_key:
            continue
        parts = alert.dedupe_key.split(":", 1)
        if len(parts) != 2:
            continue
        try:
            runner_ids.add(uuid.UUID(parts[1]))
        except ValueError:
            continue

    runners_map: dict[str, LocalRunner] = {}
    if runner_ids:
        runners = (
            await session.execute(
                select(LocalRunner).where(LocalRunner.id.in_(list(runner_ids)))
            )
        ).scalars().all()
        runners_map = {str(r.id): r for r in runners}

    # Extract plain fields before the loop; _maybe_escalate_runner_deactivated_alert
    # commits (runner_health_alerts.py:260), expiring all LocalRunner ORM objects,
    # and the rollback handler also expires them. Reading an expired object's
    # attributes triggers MissingGreenlet on subsequent loop iterations.
    runner_info: dict[str, dict[str, str | None]] = {}
    for rid, r in runners_map.items():
        runner_info[rid] = {"status": r.status, "hostname": r.hostname}

    for alert in rows:
        if not alert.dedupe_key:
            continue
        # Reconstruct runner_id from dedupe_key: "runner.deactivated:{runner_id}"
        parts = alert.dedupe_key.split(":", 1)
        if len(parts) != 2:
            continue
        try:
            runner_id = uuid.UUID(parts[1])
        except ValueError:
            continue
        info = runner_info.get(str(runner_id))
        if info is None or info["status"] == "active":
            continue
        try:
            await _maybe_escalate_runner_deactivated_alert(
                session,
                project_id=alert.project_id,
                runner_id=runner_id,
                hostname=info["hostname"],
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to escalate runner-deactivated alert for %s",
                runner_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception("[RUNNERS] rollback after escalation failure failed")


async def _check_low_disk_runners(session: AsyncSession) -> None:
    """Detect live runners whose free disk has fallen below the floor and alert/resolve.

    Queries active runners with a recent heartbeat; for those reporting
    ``disk_free_bytes`` below the configurable threshold, calls
    :func:`~app.alert_dispatch._maybe_create_runner_low_disk_alert` to raise (or
    dedup) a WARNING alert. Resolves alerts for runners whose disk has recovered.

    Called from :func:`_check_degraded_runners` so it shares the same 300s cadence
    as the other health sweeps.
    """
    from app.alert_dispatch import (
        _RUNNER_LOW_DISK_EVENT_TYPE,
        _maybe_create_runner_low_disk_alert,
        _resolve_runner_low_disk_alert,
    )
    from app.models import Alert

    threshold = int(
        _runtime_config.get(
            "runner_low_disk_threshold_bytes", _RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT
        )
    )

    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    active_rows = (
        await session.execute(
            select(
                LocalRunner.id,
                LocalRunner.project_id,
                LocalRunner.hostname,
                LocalRunner.capabilities,
            ).where(
                LocalRunner.status == "active",
                LocalRunner.last_seen_at.is_not(None),
                LocalRunner.last_seen_at >= cutoff,
            )
        )
    ).all()

    low_disk_runner_ids: set[uuid.UUID] = set()
    for runner_id, project_id, hostname, capabilities in active_rows:
        disk_free = (capabilities or {}).get("disk_free_bytes")
        if disk_free is None:
            continue
        if int(disk_free) < threshold:
            low_disk_runner_ids.add(runner_id)
            try:
                await _maybe_create_runner_low_disk_alert(
                    session,
                    project_id=project_id,
                    runner_id=runner_id,
                    hostname=hostname,
                    disk_free_bytes=int(disk_free),
                    threshold_bytes=threshold,
                )
            except Exception:
                logger.exception(
                    "[RUNNERS] Failed to create low-disk alert for runner %s", runner_id
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RUNNERS] rollback after low-disk alert create failed for %s",
                        runner_id,
                    )

    # Resolve alerts for runners whose disk has recovered (or that stopped reporting).
    open_low_disk = (
        await session.execute(
            select(Alert.project_id, Alert.dedupe_key).where(
                Alert.event_type == _RUNNER_LOW_DISK_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
            )
        )
    ).all()
    for project_id_row, dedupe_key in open_low_disk:
        if not dedupe_key:
            continue
        try:
            runner_id = uuid.UUID(dedupe_key.split(":", 1)[-1])
        except (ValueError, IndexError):
            continue
        if runner_id in low_disk_runner_ids:
            continue
        try:
            await _resolve_runner_low_disk_alert(session, project_id_row, runner_id)
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve low-disk alert for runner %s", runner_id
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after low-disk alert resolve failed for %s",
                    runner_id,
                )


async def _check_idle_gpu_runners(session: AsyncSession) -> None:
    """Detect GPU runners with low GPU utilization and alert/resolve.

    Queries active runners with recent heartbeats that report GPU utilization;
    for those reporting ``gpu_utilization_pct`` below the configurable threshold,
    calls :func:`~app.alert_dispatch._maybe_create_runner_idle_gpu_alert` to raise
    (or dedup) a WARNING alert. Resolves alerts for runners whose GPU utilization
    has recovered.

    Called from :func:`_check_degraded_runners` so it shares the same 300s cadence
    as the other health sweeps.
    """
    from app.alert_dispatch import (
        _RUNNER_IDLE_GPU_EVENT_TYPE,
        _RUNNER_IDLE_GPU_THRESHOLD_PCT_DEFAULT,
        _maybe_create_runner_idle_gpu_alert,
        _resolve_runner_idle_gpu_alert,
    )
    from app.models import Alert

    threshold = int(
        _runtime_config.get(
            "runner_idle_gpu_threshold_pct", _RUNNER_IDLE_GPU_THRESHOLD_PCT_DEFAULT
        )
    )

    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    active_rows = (
        await session.execute(
            select(
                LocalRunner.id,
                LocalRunner.project_id,
                LocalRunner.hostname,
                LocalRunner.capabilities,
            ).where(
                LocalRunner.status == "active",
                LocalRunner.last_seen_at.is_not(None),
                LocalRunner.last_seen_at >= cutoff,
            )
        )
    ).all()

    idle_gpu_runner_ids: set[uuid.UUID] = set()
    for runner_id, project_id, hostname, capabilities in active_rows:
        gpu_pct = (capabilities or {}).get("gpu_utilization_pct")
        if gpu_pct is None:
            continue  # No GPU or no GPU monitoring
        try:
            gpu_pct_float = float(gpu_pct)
        except (TypeError, ValueError):
            continue
        if gpu_pct_float < threshold:
            idle_gpu_runner_ids.add(runner_id)
            try:
                await _maybe_create_runner_idle_gpu_alert(
                    session,
                    project_id=project_id,
                    runner_id=runner_id,
                    hostname=hostname,
                    gpu_utilization_pct=gpu_pct_float,
                    threshold_pct=threshold,
                )
            except Exception:
                logger.exception(
                    "[RUNNERS] Failed to create idle-GPU alert for runner %s",
                    runner_id,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RUNNERS] rollback after idle-GPU alert create failed for %s",
                        runner_id,
                    )

    # Resolve alerts for runners whose GPU utilization has recovered.
    open_idle_gpu = (
        await session.execute(
            select(Alert.project_id, Alert.dedupe_key).where(
                Alert.event_type == _RUNNER_IDLE_GPU_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
            )
        )
    ).all()
    for project_id_row, dedupe_key in open_idle_gpu:
        if not dedupe_key:
            continue
        try:
            runner_id = uuid.UUID(dedupe_key.split(":", 1)[-1])
        except (ValueError, IndexError):
            continue
        if runner_id in idle_gpu_runner_ids:
            continue
        try:
            await _resolve_runner_idle_gpu_alert(
                session, project_id_row, runner_id
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve idle-GPU alert for runner %s",
                runner_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after idle-GPU alert resolve failed for %s",
                    runner_id,
                )


async def _check_fleet_no_healthy_runner(
    session: AsyncSession,
    degraded_runner_ids: set[uuid.UUID],
) -> None:
    """Raise or resolve the per-project fleet-no-healthy-runner aggregate alert.

    A project's self-hosted fleet is considered fully unhealthy when it has at
    least one registered runner but NONE are currently healthy (active + heartbeat
    within the liveness window + consecutive failures below the degraded threshold).
    When that condition holds, one deduplicated ERROR alert fires per project so
    the catastrophic outage is paged distinctly from an individual runner death.
    The alert auto-resolves when a healthy runner is detected.

    Called from :func:`_check_degraded_runners` with the set of runner IDs that
    are currently degraded (computed in the same sweep pass), so the fleet check
    does not re-query active/degraded runners.
    """
    from app.alert_dispatch import (
        _maybe_create_fleet_no_healthy_runner_alert,
        _resolve_fleet_no_healthy_runner_alert,
    )

    now = _utcnow()
    cutoff = now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)

    # Single pass over all registered runners to classify per-project health.
    all_rows = (
        await session.execute(
            select(
                LocalRunner.id,
                LocalRunner.project_id,
                LocalRunner.status,
                LocalRunner.last_seen_at,
            )
        )
    ).all()

    projects_with_runners: set[uuid.UUID] = set()
    projects_with_healthy: set[uuid.UUID] = set()
    offline_by_project: dict[uuid.UUID, int] = {}
    degraded_by_project: dict[uuid.UUID, int] = {}

    for runner_id, project_id, status, last_seen_at in all_rows:
        projects_with_runners.add(project_id)
        is_live = (
            status == "active"
            and last_seen_at is not None
            and last_seen_at > cutoff
        )
        if is_live and runner_id not in degraded_runner_ids:
            projects_with_healthy.add(project_id)
        elif is_live:
            degraded_by_project[project_id] = degraded_by_project.get(project_id, 0) + 1
        else:
            offline_by_project[project_id] = offline_by_project.get(project_id, 0) + 1

    for project_id in projects_with_runners - projects_with_healthy:
        try:
            await _maybe_create_fleet_no_healthy_runner_alert(
                session,
                project_id=project_id,
                offline_count=offline_by_project.get(project_id, 0),
                degraded_count=degraded_by_project.get(project_id, 0),
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to create fleet-no-healthy alert for project %s",
                project_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after fleet alert create failed for project %s",
                    project_id,
                )

    for project_id in projects_with_healthy:
        try:
            await _resolve_fleet_no_healthy_runner_alert(session, project_id)
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve fleet-no-healthy alert for project %s",
                project_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after fleet alert resolve failed for project %s",
                    project_id,
                )


async def _check_degraded_runners(session: AsyncSession) -> None:
    """Detect runners that are heartbeating but failing every job, and alert/resolve.

    Queries active runners with recent heartbeats and ``consecutive_job_failures``
    >= :data:`_RUNNER_DEGRADED_FAILURE_THRESHOLD`. For each degraded runner, calls
    :func:`~app.alert_dispatch._maybe_create_runner_degraded_alert` to raise (or
    dedup) a WARNING alert. Then resolves alerts for runners that have recovered.

    Called from the orchestrator's monitoring loop at the same 300s cadence as the
    stale-runner deactivation sweep.
    """
    from app.alert_dispatch import (
        _maybe_create_runner_degraded_alert,
        _maybe_escalate_runner_degraded_alerts,
        _resolve_runner_degraded_alert,
        _RUNNER_DEGRADED_EVENT_TYPE,
    )
    from app.models import Alert

    now = _utcnow()
    cutoff = now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)

    # 1. Alert for each actively-degraded runner.
    degraded_runners = (
        await session.execute(
            select(LocalRunner).where(
                LocalRunner.status == "active",
                LocalRunner.last_seen_at.is_not(None),
                LocalRunner.last_seen_at >= cutoff,
                LocalRunner.consecutive_job_failures >= _RUNNER_DEGRADED_FAILURE_THRESHOLD,
            )
        )
    ).scalars().all()

    degraded_runner_ids: set[uuid.UUID] = set()
    for runner in degraded_runners:
        degraded_runner_ids.add(runner.id)
        seconds_since_last_success = (
            max(0, int((now - runner.last_successful_job_at).total_seconds()))
            if runner.last_successful_job_at is not None
            else None
        )
        try:
            await _maybe_create_runner_degraded_alert(
                session,
                project_id=runner.project_id,
                runner_id=runner.id,
                hostname=runner.hostname,
                consecutive_failures=runner.consecutive_job_failures or 0,
                seconds_since_last_success=seconds_since_last_success,
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to create degraded alert for runner %s", runner.id
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after degraded-alert create failed for %s",
                    runner.id,
                )

    # 2. Resolve degraded alerts for runners that have recovered.
    # Only check runners that HAVE an open degraded alert to avoid querying every
    # non-degraded runner.
    open_degraded_keys = (
        await session.execute(
            select(Alert.project_id, Alert.dedupe_key).where(
                Alert.event_type == _RUNNER_DEGRADED_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
            )
        )
    ).all()

    for project_id_row, dedupe_key in open_degraded_keys:
        if not dedupe_key:
            continue
        try:
            runner_id = uuid.UUID(dedupe_key.split(":", 1)[-1])
        except (ValueError, IndexError):
            continue
        if runner_id in degraded_runner_ids:
            continue  # Still degraded — keep the alert.
        try:
            await _resolve_runner_degraded_alert(session, project_id_row, runner_id)
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve degraded alert for runner %s", runner_id
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RUNNERS] rollback after degraded-alert resolve failed for %s",
                    runner_id,
                )

    # 3. Escalate degraded alerts that have been open too long: re-page on the
    # persisted 1h-then-4h cadence and flip WARNING→ERROR past the age threshold.
    # Runs after the resolve step (2) so recovered runners' alerts are already
    # dismissed and never escalate — same flap-suppression the deactivated path
    # gets for free. Best-effort; a failure must not abort the rest of the sweep.
    try:
        await _maybe_escalate_runner_degraded_alerts(session)
    except Exception:
        logger.exception("[RUNNERS] Failed to sweep degraded-runner escalations")
        try:
            await session.rollback()
        except Exception:
            logger.exception("[RUNNERS] rollback after degraded escalation sweep failed")

    # 4. Fleet-level rollup: raise or resolve the per-project "no healthy runner" alert.
    try:
        await _check_fleet_no_healthy_runner(session, degraded_runner_ids)
    except Exception:
        logger.exception("[RUNNERS] Failed to sweep fleet-no-healthy-runner alerts")

    # 5. Proactive low-disk alert: runners reporting disk below the floor.
    try:
        await _check_low_disk_runners(session)
    except Exception:
        logger.exception("[RUNNERS] Failed to sweep low-disk runner alerts")

    # 6. Proactive idle-GPU alert: GPU runners reporting utilization below threshold.
    try:
        await _check_idle_gpu_runners(session)
    except Exception:
        logger.exception("[RUNNERS] Failed to sweep idle-GPU runner alerts")
