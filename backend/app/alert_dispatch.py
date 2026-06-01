"""Alert creation + dispatch helpers.

Extracted from ``app.main`` (P5 service-locator decomposition) so callers
(orchestrator, reaper, services, ci_completion, plan_completion,
review_pipeline, rollup_management, task_completion, task_lifecycle,
task_lifecycle_requeue, strategic_completion, autonomous_research, and the
admin/agent_status routers) can import the alert helpers directly without
depending on the FastAPI app entrypoint. ``app.main`` re-exports these symbols
for backward compatibility.

Pure leaf module — depends only on ``app.broadcast``, ``app.notification``,
``app.shared_state``, ``app.time_utils``, ``app.models``. Tests patch
``app.alert_dispatch.broadcast_to_project`` and
``app.alert_dispatch._send_webhook_notification`` for the helpers that live
here, and ``app.alert_dispatch.<helper>`` where a caller invokes one.
"""

import logging
import uuid  # used in the "uuid.UUID" string annotations on the alert helpers
from datetime import datetime  # used in the "datetime" string annotation

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.models import (
    Project,
    Task,  # used in the "Task" string annotation on _maybe_create_failure_alert
    Alert,
    AlertSeverity,
)
from app.notification import _send_webhook_notification
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _get_broadcast_limit(design_doc: "dict | None" = None) -> int:
    """Return the broadcast limit for a project, falling back to the global default.

    Per-project override: store ``_broadcast_limit`` in project.design_doc.
    Set to 0 to opt a project out of system-wide broadcast alerts.
    """
    per_project = (design_doc or {}).get("_broadcast_limit")
    if per_project is not None:
        return int(per_project)
    return _runtime_config.get("project_broadcast_limit", 1000)


async def _create_orchestrator_error_alerts(
    session: AsyncSession, consecutive_errors: int
) -> None:
    """Create ERROR alerts on all projects when the orchestrator hits repeated errors (deduplicated)."""
    title = "Orchestrator error loop"
    msg = (
        f"The orchestrator has encountered {consecutive_errors} consecutive errors. "
        "The pipeline may be stalled. Check logs for details."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_orchestrator_error_alerts: project_ids capped at %d",
            global_limit,
        )
    existing_rows = (
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        ).all()
        if project_ids
        else []
    )
    projects_with_alert = {row[0] for row in existing_rows}
    for pid in project_ids:
        if pid in projects_with_alert:
            continue
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )


async def _create_halted_alerts(
    session: AsyncSession,
    rollback_count: int,
    last_known_good: str,
    halted_at: "datetime",
) -> None:
    """Create ERROR alerts on all projects when deployment enters HALTED state."""
    title = f"Deployment HALTED — {rollback_count} consecutive rollbacks, manual intervention required"
    msg = (
        f"Deployment halted after {rollback_count} consecutive rollbacks. "
        f"Last known good revision: {last_known_good or 'unknown'}. "
        f"Halted at: {halted_at.isoformat()}. "
        "Pipeline will auto-recover after 1h cooldown, or manually set max VMs > 0."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning("_create_halted_alerts: project_ids capped at %d", global_limit)
    for pid in project_ids:
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )
    await _send_webhook_notification("PIPELINE_HALTED", title, msg)


async def _create_rollback_alerts(
    session: AsyncSession, rolled_back_from: str, rolled_back_to: str
) -> None:
    """Create WARNING alerts on all projects when an automatic rollback is detected.
    Deduplicates: skips if an undismissed rollback alert already exists for a project."""
    title = "Automatic rollback detected — revision reverted to last known good"
    msg = (
        f"Deployment automatically rolled back from {rolled_back_from[:8]} "
        f"to {rolled_back_to[:8]}."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_rollback_alerts: project_ids capped at %d",
            global_limit,
        )
    existing_rows = (
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.dismissed_at.is_(None),
                    Alert.title.contains("Automatic rollback"),
                )
            )
        ).all()
        if project_ids
        else []
    )
    projects_with_alert = {row[0] for row in existing_rows}
    for pid in project_ids:
        if pid in projects_with_alert:
            continue
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
        )


async def _create_halted_recovery_alerts(session: AsyncSession) -> None:
    """Create INFO alerts and dismiss existing HALTED ERROR alerts when deployment auto-recovers."""
    recovery_title = "Deployment auto-recovered from HALTED state after 1h cooldown"
    now = _utcnow()
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_halted_recovery_alerts: project_ids capped at %d",
            global_limit,
        )
    # Single batched query for all undismissed HALTED ERROR alerts across all projects
    halted_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id.in_(project_ids),
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.dismissed_at.is_(None),
                    Alert.title.contains("HALTED"),
                )
            )
        )
        .scalars()
        .all()
    )
    halted_by_project: dict = {}
    for h in halted_alerts:
        halted_by_project.setdefault(h.project_id, []).append(h)
    for pid in project_ids:
        # Dismiss existing HALTED ERROR alerts for this project
        for h in halted_by_project.get(pid, []):
            h.dismissed_at = now
            session.add(h)
        # Create INFO recovery alert
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.INFO,
            title=recovery_title,
            message=recovery_title,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "INFO", "title": alert.title},
        )


async def _maybe_create_failure_alert(
    task: "Task",
    session: AsyncSession,
    error_snippet: str | None = None,
) -> None:
    """Create an ERROR alert for a task failure unless it's a smoketest or retry task.
    Deduplicates: skips if an undismissed alert already exists for this task."""
    task_type = (task.first_payload).get("type", "")
    if task_type == "smoketest":
        return
    if (task.context or {}).get("is_retry") is True:
        return
    existing = await session.execute(
        select(Alert).where(Alert.task_id == task.id, Alert.dismissed_at.is_(None))
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.ERROR,
        title=f"Task failed: {task.title or str(task.id)[:8]}",
        message=f"Task type: {task_type}. {error_snippet or 'See task logs for details.'}",
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
    )
    try:
        from app.notification_dispatch import dispatch_external_notification
        from app.failure_classification import (
            classify_failure,
            classify_failure_taxonomy,
        )
        import asyncio as _asyncio

        ctx = task.context or {}
        payload = task.first_payload or {}
        direction_text = (
            ctx.get("direction_text") or payload.get("direction_text") or ""
        )
        pr_url = (
            ctx.get("pr_url") or ctx.get("rollup_pr_url") or payload.get("pr_url") or ""
        )
        _asyncio.ensure_future(  # noqa: RUF006  # fire-and-forget; caller does not await
            dispatch_external_notification(
                task.project_id,
                "TASK_FAILED",
                {
                    "task_id": str(task.id),
                    "task_title": task.title or str(task.id)[:8],
                    "task_type": task_type,
                    "error_snippet": error_snippet or "",
                    "failure_class": classify_failure(task),
                    "taxonomy": classify_failure_taxonomy(task),
                    "direction_id": ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": ctx.get("scope") or payload.get("scope") or "",
                    "pr_url": pr_url,
                    "attempt_count": int(task.attempt_count or 0),
                },
            )
        )
    except Exception as _ne:
        logger.warning(f"[NOTIFY] TASK_FAILED dispatch setup failed: {_ne}")


async def _maybe_create_circuit_breaker_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    reason: str,
) -> None:
    """Create a WARNING alert when the circuit breaker opens for a project.
    Deduplicates: skips if an undismissed circuit-breaker alert already exists."""
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == "Circuit breaker opened: autonomous tasks paused",
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title="Circuit breaker opened: autonomous tasks paused",
        message=reason,
    )
    session.add(alert)
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    await _send_webhook_notification("CIRCUIT_BREAKER_OPEN", alert.title, alert.message)


async def _create_strategic_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    message: str,
) -> None:
    """Create a WARNING alert for strategic cycle failures. Deduplicates by title."""
    title = "Strategic cycle issue"
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first():
        return  # Don't spam duplicate alerts
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message[:1000],
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": title},
    )
