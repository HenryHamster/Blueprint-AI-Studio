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


# Human-readable labels for the review-fix early-escalation reasons. The reason
# key is part of the deduplicated alert title, so keep these stable.
_ESCALATION_REASON_LABELS = {
    "stuck": "stuck cycle",
    "standoff": "dispute standoff",
    "repeat-fingerprint": "repeat-fingerprint halt",
}


async def _maybe_create_escalation_alert(
    task: "Task",
    session: AsyncSession,
    *,
    pr_number: "int | None",
    reason: str,
    iteration: int,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert for a review-fix early escalation.

    Surfaces the three early-escalation outcomes that fire BEFORE the iteration
    cap is hit — ``stuck`` (same blocking issues recur), ``standoff`` (the fix
    agent disputed an issue and the reviewer re-raised it), and
    ``repeat-fingerprint`` (identical failure signature across attempts) — in the
    same Alerts banner as cap-exhaustion, *regardless* of whether manual approval
    gates are enabled.

    Deduplicated by ``(task_id, title)``. The title encodes the PR + reason but
    NOT the iteration, so a re-entered escalation on the same PR does not spam the
    banner; the iteration count lives in the message. ``task`` should be the
    chain-original task so the alert is task-linked (renders a "View task" button
    in the banner).
    """
    label = _ESCALATION_REASON_LABELS.get(reason, reason)
    pr_label = f"PR #{pr_number}" if pr_number is not None else "PR"
    title = f"Review-fix escalation: {label} on {pr_label}"
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=severity,
        title=title,
        message=(
            f"{pr_label} escalated for human attention after {iteration} "
            f"review-fix iteration(s): {label}. Manual review or strategic-agent "
            f"decision required."
        ),
        task_id=task.id,
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
        },
    )


# Title prefix for the per-project warm-pool payoff-verdict transition alert.
# The verdict state is appended to the prefix, so the dedup key
# ``(project_id, title)`` is effectively per project + verdict — exactly one
# alert per distinct verdict transition. Keep the prefix stable.
_POOL_PAYOFF_ALERT_TITLE_PREFIX = "Warm-pool payoff"


def _fmt_metric(value: "float | int | None", *, pct: bool = False) -> str:
    """Render a supporting metric for the alert message ('n/a' when None)."""
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:.2f}%" if pct else f"{value:.4f}".rstrip("0").rstrip(".")


async def _maybe_create_pool_payoff_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    verdict_state: str,
    supporting: "dict",
    recommended_action: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert when a scoped project's warm-pool
    payoff verdict crosses a decision boundary.

    Closes the loop on a *bounded* warm-pool enable: when the per-project payoff
    verdict transitions to ``COST_REGRESSION`` (idle-inclusive cost per merged PR
    is materially above the pre-pool baseline) or to ``PROVEN`` with material
    warm-start volume, the operator is alerted so the keep / widen / disable
    decision is made without polling the warm-VM panel. DISTINCT from the silent,
    cost-only, fleet-wide auto-drain guard (``warm_pool_maintainer``): that
    protects spend automatically; this informs a per-project strategic decision.

    The message carries the supporting numbers (pool hit rate, pool-origin
    warm-start share, idle-inclusive cost drift vs. baseline) plus the
    recommended action so the operator can act from the alert alone.

    Deduplicated by ``(project_id, title, dismissed_at IS NULL)`` where the title
    encodes the verdict state — so a verdict that persists across many ticks
    raises exactly ONE alert, and a genuinely different verdict gets its own.
    The supporting numbers live in the message so a drifting metric never
    defeats dedup.
    """
    title = f"{_POOL_PAYOFF_ALERT_TITLE_PREFIX}: {verdict_state} on project {str(project_id)[:8]}"
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return
    message = (
        f"Warm-pool payoff verdict for project {project_id} is now "
        f"{verdict_state}. Pool hit rate: "
        f"{_fmt_metric(supporting.get('pool_hit_rate'))}; pool-origin warm-start "
        f"share: {_fmt_metric(supporting.get('pool_origin_share'))} "
        f"({int(supporting.get('pool_warm_starts') or 0)} warm starts over "
        f"{int(supporting.get('total_pool_dispatches') or 0)} pool dispatches); "
        f"idle-inclusive cost/PR drift: "
        f"{_fmt_metric(supporting.get('cost_drift_pct'), pct=True)} "
        f"(current {_fmt_metric(supporting.get('cost_incl_idle_current'))} vs "
        f"baseline {_fmt_metric(supporting.get('cost_baseline'))}). "
        f"{recommended_action}"
    )[:1000]
    alert = Alert(
        project_id=project_id,
        severity=severity,
        title=title,
        message=message,
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
        },
    )


async def _maybe_create_precondition_blocker_alert(
    task: "Task",
    session: AsyncSession,
    *,
    blocker_kind: str,
    detail: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert for a persistent precondition blocker.

    Fires when a *recognized* precondition blocker won't clear on its own: a
    task deferred on the same file / pending-rollup conflict across repeated
    dispatch cycles (see ``orchestrator_filters._record_defer_and_maybe_escalate``),
    or a parent that exhausts its requeues on an upstream-dependency blocker
    rather than a code failure (see ``task_lifecycle_requeue._requeue_parent_task``).

    Surfaces the blocker in the same Alerts banner as review-fix escalations,
    *regardless* of approval-gate settings, so a blocked task is visible instead
    of silently deferring forever or quietly landing in a generic FAILED state.
    Alerting only — it does not attempt to resolve the blocker.

    Deduplicated by ``(task_id, title)``. The title encodes the blocker
    identity via ``blocker_kind`` (e.g. the overlapping files or the blocking
    rollup title), so a blocker that persists across many dispatch cycles
    raises exactly ONE alert. A genuinely *different* blocker on the same task
    (distinct ``blocker_kind``) gets its own alert.
    """
    title = f"Precondition blocker: {blocker_kind}"[:200]
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=severity,
        title=title,
        message=detail[:1000],
        task_id=task.id,
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
        },
    )


async def _maybe_create_cascade_cancel_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    prereq_task: "Task",
    cancelled_count: int,
) -> None:
    """Create ONE deduplicated operator Alert when a failed prerequisite cancels dependents.

    When a prerequisite task fails, the orchestrator cascade-cancels every
    downstream dependent (and its non-terminal children) so impossible work is
    never dispatched. That cancellation is otherwise silent — only the cancelled
    tasks' status messages change. This surfaces the root cause ONCE: a single
    WARNING alert naming the failed prerequisite and the number of dependent
    tasks cancelled because of it, *regardless* of approval-gate settings.

    Deduplicated by ``(project_id, title, dismissed_at IS NULL)``. The failed
    prerequisite's identity is encoded in the title, so a single root failure
    that cascades through A→B→C raises exactly one alert, and a later dispatch
    cycle that re-observes the same cascade does not spam a fresh alert. The
    cancelled count lives in the message so a changing count never defeats dedup.
    """
    prereq_label = prereq_task.title or str(prereq_task.id)[:8]
    title = f"Cascade cancel: prerequisite '{prereq_label}' failed"
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return
    plural = "s" if cancelled_count != 1 else ""
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=title,
        message=(
            f"Prerequisite task '{prereq_label}' "
            f"({str(prereq_task.id)[:8]}) failed, cancelling {cancelled_count} "
            f"dependent task{plural}. The downstream plan cannot proceed until "
            f"the root failure is resolved."
        ),
        task_id=prereq_task.id,
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
        },
    )


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
