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
``app.notification_dispatch.dispatch_external_notification`` for the helpers
that live here, and ``app.alert_dispatch.<helper>`` where a caller invokes one.
"""

import logging
import uuid  # used in the "uuid.UUID" string annotations on the alert helpers
from datetime import datetime  # used in the "datetime" string annotation

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.ci_resolution import (
    BENIGN_UNVERIFIED_REASONS,
    DEGRADED_UNVERIFIED_REASONS,
)
from app.models import (
    Project,
    Task,  # used in the "Task" string annotation on _maybe_create_failure_alert
    Alert,
    AlertSeverity,
)
from app.alert_policy import create_project_alert_if_allowed
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


_SPEND_HOLD_ALERT_TITLE = "Spend/budget approval hold awaiting operator"


async def _maybe_create_spend_hold_alert(
    task: "Task",
    session: AsyncSession,
    message: str,
) -> None:
    """Create a non-failure WARNING alert when a spend/budget approval hold opens.

    Distinct from ``_maybe_create_failure_alert`` (ERROR / failure semantics): a
    deliberate spend/budget hold is NOT a failure, so it carries WARNING severity
    and never records a failed task result. Deduplicates on (task, stable title)
    so repeated recovery-sweep passes don't create a second row for the same hold.
    Mirrors the INFO/WARNING alert pattern (e.g. circuit-breaker) — the caller
    commits the session; this only stages the row and broadcasts ``alert_created``.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.title == _SPEND_HOLD_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=_SPEND_HOLD_ALERT_TITLE,
        message=message,
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )


# Human-readable labels for the no-CI UNVERIFIED-merge reasons. The orchestrator
# writes the reason KEY into review_loop["unverified_merge_reason"] at the two
# UNVERIFIED decision sites, so keep these keys stable. The reason is NOT part of
# the dedup title (the PR number is), so the label only shapes the message text.
_UNVERIFIED_REASON_LABELS = {
    "no_detectable_test_command": "no runnable test command could be detected",
    "kill_switch_off": "the no-CI test-gate is disabled (kill-switch off)",
    "internal_staging_base": (
        "the PR targets an internal staging branch (CI is path-filtered there)"
    ),
    "undetected_runtime": "the detected test suite produced no usable verdict",
    "suite_all_skipped": (
        "a test suite was present but every test was skipped (no real verification ran)"
    ),
    "verify_setup_failed": (
        "the detected test suite exists but could not be run "
        "(setup/runner infrastructure failed after retries)"
    ),
    "probe_passed": (
        "no unit suite ran; only a cheap acceptance probe (compile/build) "
        "passed — not real test verification"
    ),
    "probe_could_not_run": (
        "no unit suite ran and the acceptance probe could not run "
        "(missing deps/unprovisioned setup)"
    ),
    "probe_no_artifact": (
        "no unit suite ran; the declared build command exited cleanly but "
        "produced no artifact (possible no-op build) — not real verification"
    ),
    "documented_command_could_not_run": (
        "a command the PR introduced/documented could not be run "
        "(missing deps/unprovisioned setup) -- claim unverified"
    ),
    "documented_command_passed": (
        "no unit suite ran; a command the PR introduced/documented was "
        "executed and passed"
    ),
    "documented_command_no_artifact": (
        "a build command the PR introduced/documented exited cleanly but "
        "produced no artifact (possible no-op build) -- claim unverified"
    ),
    "acceptance_test_not_run": (
        "the PR declared a named acceptance test but the gate could not confirm "
        "it ran (no targetable suite/environment failure) -- claim unverified"
    ),
}

# Severity escalation for no-CI UNVERIFIED-merge reasons: degraded environment/
# setup failures escalate to ERROR, benign fall-open reasons stay WARNING.
#
# DERIVED — not hand-listed — from the single source of truth in
# ``app.ci_resolution`` (``DEGRADED_UNVERIFIED_REASONS`` / ``BENIGN_UNVERIFIED_REASONS``)
# so the benign-vs-degraded partition lives in exactly one place and the
# AlertSeverity mapping cannot drift from the success-scoring classifier. The
# resulting dict is byte-for-byte identical to the former literal.
_UNVERIFIED_REASON_SEVERITY = {
    **{reason: AlertSeverity.ERROR for reason in DEGRADED_UNVERIFIED_REASONS},
    **{reason: AlertSeverity.WARNING for reason in BENIGN_UNVERIFIED_REASONS},
}


def _severity_for_unverified_reason(reason: str) -> AlertSeverity:
    """Map an unverified-merge reason to its alert severity.

    Degraded reasons (environment/setup failure blocked verification) escalate
    to ERROR; benign reasons (no verification expected) stay WARNING. Defaults
    to WARNING for unknown reasons to preserve backward compatibility.
    """
    return _UNVERIFIED_REASON_SEVERITY.get(reason, AlertSeverity.WARNING)


# Stable title for the fleet-wide merge-gate degradation alert. Kept CONSTANT
# (the drifting rate/reason live in the message) so a degradation that persists
# across reaper ticks dedups to exactly ONE alert per project.
_MERGE_GATE_DEGRADATION_ALERT_TITLE = (
    "Fleet merge-gate verification degraded — PRs landing UNVERIFIED"
)


async def _maybe_create_merge_gate_degradation_alerts(
    session: AsyncSession,
    *,
    verification_rate: "float | None",
    verified_count: int,
    fell_open_count: int,
    degraded_project_count: int,
    project_count: int,
    most_common_reason: "str | None",
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> int:
    """Broadcast a deduplicated fleet-wide alert when the no-CI merge gate's
    verification rate has degraded — i.e. 'done' PRs are increasingly landing
    without their tests actually being exercised.

    Every other fleet-level health signal (rollbacks, halts, warm-pool payoff,
    stale staged directions, single unverified merges) raises a proactive alert;
    this closes that gap for systemic claim-verification erosion. Computed from
    the already-aggregated :func:`digest_metrics.compute_merge_gate_fleet_health`.

    Mirrors the fleet-broadcast convention of :func:`_create_rollback_alerts`:
    creates one alert per project (so it surfaces in every operator's Alerts
    banner) and deduplicates on a STABLE title so a degradation that persists
    across reaper ticks raises exactly ONE alert per project. The drifting
    numbers (rate, degraded-project count, dominant fall-open reason) live in the
    MESSAGE so a moving metric never defeats dedup. Caller owns the commit.

    Returns the number of NEW alerts created across the fleet.
    """
    reason_label = (
        _UNVERIFIED_REASON_LABELS.get(most_common_reason, most_common_reason)
        if most_common_reason
        else "no dominant reason recorded"
    )
    rate_pct = (
        f"{verification_rate * 100:.0f}%" if verification_rate is not None else "n/a"
    )
    title = _MERGE_GATE_DEGRADATION_ALERT_TITLE
    msg = (
        f"Fleet-wide no-CI merge-gate verification has degraded: {verified_count} "
        f"of {verified_count + fell_open_count} recent merges actually ran tests "
        f"({rate_pct} verified), and {degraded_project_count} of {project_count} "
        f"projects are degraded. Dominant fall-open reason: {reason_label}. "
        f"'Done' PRs are increasingly merging without their claims being exercised."
    )[:1000]

    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_maybe_create_merge_gate_degradation_alerts: project_ids capped at %d",
            global_limit,
        )
    existing_rows = (
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        ).all()
        if project_ids
        else []
    )
    projects_with_alert = {row[0] for row in existing_rows}
    created = 0
    for pid in project_ids:
        if pid in projects_with_alert:
            continue
        alert = Alert(
            project_id=pid,
            severity=severity,
            title=title,
            message=msg,
        )
        session.add(alert)
        created += 1
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": severity.value,
                "title": alert.title,
            },
        )
    return created


# Stable title for the per-project migration-slot-contention alert. Kept CONSTANT
# (the drifting hold age / waiter count / longest wait live in the message) so a
# hold that persists across reaper ticks dedups to exactly ONE alert per project.
_MIGRATION_SLOT_CONTENTION_ALERT_TITLE = (
    "Migration-serialization slot held — schema tasks parked"
)


def _fmt_duration_seconds(seconds: "float | int | None") -> str:
    """Human-readable compact duration ('2h 5m', '45m', '30s', 'n/a')."""
    if seconds is None:
        return "n/a"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


async def _maybe_create_migration_slot_contention_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    *,
    holder_task_id: "str | None",
    held_seconds: "float | int | None",
    waiting_count: int,
    longest_wait_seconds: "float | int | None",
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> int:
    """Raise a deduplicated alert when a migration task has held the per-project
    serialization slot too long while schema-touching tasks are parked waiting.

    A migration-producing task that is IN_PROGRESS holds the per-project
    migration-serialization slot; siblings defer to QUEUED/BLOCKED while it holds.
    A multi-hour hold (the recent incident: ~2h holding, 5+ tasks parked) is
    invisible to the throughput-only migration-health summary and to the
    escalated-only parked-blocker signal — the deferred siblings are QUEUED, not
    BLOCKED. This raises the proactive operator signal so a prolonged hold
    self-surfaces. Alerting only — it never changes serialization behavior.

    Computed from the per-project entry of
    :func:`digest_metrics.compute_migration_slot_contention`. Deduplicated on a
    STABLE title so a hold that persists across reaper ticks raises exactly ONE
    alert per project; the drifting hold age / waiter count / longest wait live in
    the MESSAGE so a moving metric never defeats dedup. Task-linked to the holder
    so the alert renders a "View task" button. Caller owns the commit.

    Returns 1 when a NEW alert was created, else 0.
    """
    title = _MIGRATION_SLOT_CONTENTION_ALERT_TITLE
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return 0

    msg = (
        f"A migration-producing task has held the migration-serialization slot "
        f"for {_fmt_duration_seconds(held_seconds)} while {waiting_count} "
        f"schema-touching task{'s' if waiting_count != 1 else ''} are parked "
        f"waiting (longest wait {_fmt_duration_seconds(longest_wait_seconds)}). "
        f"Migration work is the throughput bottleneck — review the holder "
        f"(task {holder_task_id}) for a stuck author/test loop."
    )[:1000]

    holder_uuid: "uuid.UUID | None" = None
    if holder_task_id:
        try:
            holder_uuid = uuid.UUID(str(holder_task_id))
        except (ValueError, TypeError):
            holder_uuid = None

    alert = Alert(
        project_id=project_id,
        severity=severity,
        title=title,
        message=msg,
        task_id=holder_uuid,
    )
    session.add(alert)
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
        },
    )
    return 1


async def _maybe_create_unverified_merge_alert(
    task: "Task",
    session: AsyncSession,
    *,
    pr_number: "int | None",
    reason: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert when a no-CI merge proceeds UNVERIFIED.

    For repos with no required CI checks, Blueprint runs the repo's own detected
    tests as the merge gate. The 'undetected' verdict — no detectable test
    command, the gate kill-switch off, an internal staging base, or a verify run
    that produced no usable verdict — falls back to merging the PR WITHOUT test
    verification so genuinely test-less repos still make progress. That fallback
    was previously info-log only; this surfaces it in the same Alerts banner as
    other pipeline escalations so the operator who turned off 'required CI checks'
    learns that a specific merge proceeded with zero test verification, and why.

    The PASS and FAIL verdicts gate correctly and never reach here — only the
    UNVERIFIED merge does. Alerting only; it does not change the merge outcome.

    Deduplicated by ``(task_id, title)`` where the title encodes the PR number
    (NOT the reason), so a single unverified merge raises exactly ONE alert even
    if a later merge sweep re-enters the path. ``task`` should be the chain task
    so the alert is task-linked (renders a "View task" button in the banner).
    """
    label = _UNVERIFIED_REASON_LABELS.get(reason, reason)
    pr_label = f"PR #{pr_number}" if pr_number is not None else "PR"
    title = f"Merged UNVERIFIED: {pr_label} — no test verification"
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
            f"{pr_label} merged without test verification because {label}. "
            f"This project has no required CI checks, so Blueprint runs the "
            f"repo's own detected tests as the merge gate — this merge proceeded "
            f"UNVERIFIED. Review the merge if test coverage was expected."
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
    try:
        from app.notification_dispatch import dispatch_external_notification
        import asyncio as _asyncio

        _asyncio.ensure_future(  # noqa: RUF006  # fire-and-forget; caller does not await
            dispatch_external_notification(
                task.project_id,
                "MERGE_UNVERIFIED",
                {
                    "task_id": str(task.id),
                    "task_title": task.title or str(task.id)[:8],
                    "pr_number": pr_number,
                    "reason": reason,
                    "reason_label": label,
                },
            )
        )
    except Exception as _ne:
        logger.warning(f"[NOTIFY] MERGE_UNVERIFIED dispatch setup failed: {_ne}")


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


_ADVERSARIAL_GATE_FAIL_OPEN_TITLE = (
    "Adversarial gate failed open: second-opinion model unavailable"
)


async def _maybe_create_adversarial_gate_fail_open_alert(
    review: "Task",
    challenger: "Task",
    session: AsyncSession,
) -> None:
    """Create a deduplicated WARNING alert when the adversarial review gate degrades.

    When the panel-review challenger model is unavailable (provider model-config
    rejection), the gate falls back to the primary verdict and auto-merges trunk
    APPROVEs — a fail-open. Previously this emitted only a logger.warning; this
    surfaces it in the Alerts banner so operators can investigate.

    Deduplicates on ``(review.id, title)`` so a single fail-open event raises
    exactly one alert even if the resolution path is re-entered.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == review.id,
            Alert.dismissed_at.is_(None),
            Alert.title == _ADVERSARIAL_GATE_FAIL_OPEN_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=review.project_id,
        severity=AlertSeverity.WARNING,
        title=_ADVERSARIAL_GATE_FAIL_OPEN_TITLE,
        message=(
            f"The second-opinion panel model (challenger task {str(challenger.id)[:8]}) "
            f"was unavailable due to a provider model-config rejection. "
            f"The review gate degraded gracefully: the primary verdict was preserved "
            f"and the merge proceeded without adversarial verification. "
            f"Review task: {str(review.id)[:8]}."
        ),
        task_id=review.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(review.project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
        },
    )


_SPEND_CEILING_ALERT_TITLE = "Spend ceiling reached: autonomous spawning paused"


async def _maybe_create_spend_ceiling_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    window_spend_usd: float,
    ceiling_usd: float,
    spend_window_days: int,
) -> None:
    """Create a deduplicated WARNING alert when a project's spend ceiling is hit.

    Deduplicates on a stable title: one undismissed alert per project per trip.
    Caller owns the commit.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == _SPEND_CEILING_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return
    window_label = f"{spend_window_days}-day" if spend_window_days != 1 else "1-day"
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=_SPEND_CEILING_ALERT_TITLE,
        message=(
            f"{window_label} window spend ${window_spend_usd:.4f} has reached the "
            f"per-project ceiling ${ceiling_usd:.4f}. New autonomous "
            f"spawning is paused until spend falls below the ceiling."
        ),
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )


_SPEND_APPROACHING_ALERT_TITLE = (
    "Spend ceiling approaching: autonomous spawning may pause soon"
)


async def _maybe_create_approaching_ceiling_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    window_spend_usd: float,
    ceiling_usd: float,
    warning_fraction: float,
    spend_window_days: int,
) -> None:
    """Create a deduplicated WARNING alert when a project's spend approaches its ceiling.

    Fires when rolling spend is in the configured band below the ceiling
    (spend >= ceiling * warning_fraction AND spend < ceiling). When spend reaches
    the ceiling, _maybe_create_spend_ceiling_alert fires instead — these two alerts
    are mutually exclusive per tick. Caller owns the commit.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == _SPEND_APPROACHING_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return
    window_label = f"{spend_window_days}-day" if spend_window_days != 1 else "1-day"
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=_SPEND_APPROACHING_ALERT_TITLE,
        message=(
            f"Rolling {window_label} window spend ${window_spend_usd:.4f} has reached "
            f"{warning_fraction * 100:.0f}% of the per-project ceiling "
            f"${ceiling_usd:.4f}. Autonomous spawning will pause when spend "
            f"reaches the ceiling."
        ),
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )


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
) -> tuple[Alert | None, bool]:
    """Create a deduplicated operator Alert when a scoped project's warm-pool
    payoff verdict crosses a decision boundary.

    Closes the loop on a *bounded* warm-pool enable: when the per-project payoff
    verdict transitions to ``COST_REGRESSION`` (idle-inclusive cost per merged PR
    is materially above the pre-pool baseline), the operator is alerted so the
    keep / disable decision is made without polling the warm-VM panel. ``PROVEN``
    remains a reader-facing payoff state, not a durable alert. DISTINCT from the
    silent, cost-only, fleet-wide auto-drain guard (``warm_pool_maintainer``): that
    protects spend automatically; this informs a per-project strategic decision.

    The message carries the supporting numbers (pool hit rate, pool-origin
    warm-start share, idle-inclusive cost drift vs. baseline) plus the
    recommended action so the operator can act from the alert alone.

    Deduplicated by active ``warm_pool.payoff`` key where the key encodes the
    verdict state — so a verdict that persists across many ticks
    raises exactly ONE alert, and a genuinely different verdict gets its own.
    The supporting numbers live in the message so a drifting metric never
    defeats dedup.
    """
    title = f"{_POOL_PAYOFF_ALERT_TITLE_PREFIX}: {verdict_state} on project {str(project_id)[:8]}"
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
    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type="warm_pool.payoff",
        severity=severity,
        title=title,
        message=message,
        source="alert_dispatch",
        dedupe_key=f"warm_pool.payoff:{project_id}:{verdict_state}",
        broadcast=False,
    )
    if alert is None:
        return None, False
    if not created:
        await session.commit()
        return alert, False
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
    return alert, True


# Title prefix for the per-project provider-routing-verdict transition alert.
# The verdict state is appended to the prefix, so the dedup key
# ``(project_id, dedupe_key)`` is effectively per project + verdict — exactly
# one alert per distinct verdict transition. Keep the prefix stable.
_PROVIDER_ROUTING_ALERT_TITLE_PREFIX = "Provider routing verdict"


async def _maybe_create_provider_routing_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    verdict_state: str,
    supporting: "dict",
    recommended_action: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> tuple[Alert | None, bool]:
    """Create a deduplicated operator Alert when the provider routing A/B verdict
    crosses a decision boundary.

    Closes the loop on the DeepSeek-vs-Anthropic routing A/B: when the verdict
    transitions to ADOPT or REJECT, the operator is alerted so the keep / revert
    decision is made without polling the efficiency panel. INSUFFICIENT_DATA is
    not a decision boundary and never reaches this helper.

    The message carries the supporting numbers (cost ratio, verified-rate delta,
    per-provider counts) plus the recommended action so the operator can act from
    the alert alone.

    Deduplicated by ``efficiency.provider_routing_verdict:{project_id}:{verdict_state}``
    — so a verdict that persists across many ticks raises exactly ONE alert, and
    a genuinely different verdict gets its own.
    """
    title = f"{_PROVIDER_ROUTING_ALERT_TITLE_PREFIX}: {verdict_state} on project {str(project_id)[:8]}"
    cost_ratio = supporting.get("cost_ratio")
    rate_delta = supporting.get("verified_rate_delta_pct")
    ds_run = int(supporting.get("deepseek_run_count") or 0)
    an_run = int(supporting.get("anthropic_run_count") or 0)
    msg_parts = [
        f"Provider routing verdict for project {project_id} is now {verdict_state}.",
    ]
    if cost_ratio is not None:
        msg_parts.append(f"DeepSeek cost ratio vs Anthropic: {cost_ratio:.4f}")
    if rate_delta is not None:
        msg_parts.append(
            f"Verified-merge rate delta: {rate_delta:+.2f} percentage points"
        )
    msg_parts.append(
        f"DeepSeek runs: {ds_run}, Anthropic runs: {an_run}. {recommended_action}"
    )
    message = " ".join(msg_parts)[:1000]

    dedupe_key = f"efficiency.provider_routing_verdict:{project_id}:{verdict_state}"
    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type="efficiency.provider_routing_verdict",
        severity=severity,
        title=title,
        message=message,
        source="alert_dispatch",
        dedupe_key=dedupe_key,
        broadcast=False,
    )
    if alert is None:
        return None, False
    if not created:
        await session.commit()
        return alert, False
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
    return alert, True


# Stable per-project title for the unpriced-LLM-models coverage alert. The
# drifting numbers (unpriced share, affected combos) live in the MESSAGE so a
# degradation that persists across snapshot ticks dedups to exactly ONE alert
# per project. Keep the prefix stable.
_UNPRICED_MODELS_ALERT_TITLE_PREFIX = "LLM spend on unpriced models"


async def _maybe_create_unpriced_models_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    unpriced_combos: list,
    unpriced_task_count: int,
    attributed_task_count: int,
    unpriced_share: float,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert when a meaningful share of a
    project's recent LLM-bearing tasks ran on (provider, model) combinations
    that have NO entry in the cost-analytics pricing table.

    Unpriced tasks contribute ``0.0`` to the headline cost economics
    (cost-per-merge, total spend), so spend silently leaks into a zero-cost
    bucket until someone adds a price entry. The only place an operator could
    previously notice this was the LLM cost-breakdown panel's pricing-coverage
    caveat — there was no proactive signal. This mirrors the warm-pool payoff
    alert: it names the unpriced combinations and the affected coverage so the
    operator learns a price entry is missing without polling the panel.

    Deduplicated by ``(project_id, title, dismissed_at IS NULL)`` on a STABLE
    title — so a coverage gap that persists across many snapshot ticks raises
    exactly ONE alert per project. The drifting numbers (share, combos) live in
    the message so a moving metric never defeats dedup. Caller owns the
    transition-edge gating; this helper only enforces title-level dedup and
    commits its own ``Alert`` row.
    """
    title = f"{_UNPRICED_MODELS_ALERT_TITLE_PREFIX} on project {str(project_id)[:8]}"
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
    combos_label = ", ".join(unpriced_combos) if unpriced_combos else "(none recorded)"
    message = (
        f"{unpriced_task_count} of {attributed_task_count} recently attributed "
        f"LLM-bearing tasks ({unpriced_share * 100:.0f}%) ran on (provider, "
        f"model) combinations with no entry in the cost pricing table, so their "
        f"spend is counted as $0 and understates cost-per-merge and total spend. "
        f"Unpriced combinations: {combos_label}. Add a price entry for these "
        f"models to LLM_TOKEN_PRICING to restore accurate cost analytics."
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


_SELF_REPAIR_BURN_SHARE_ALERT_TITLE_PREFIX = "Self-repair token overhead"


async def _maybe_create_self_repair_burn_share_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    burn_share: float,
    threshold: float,
    severity: AlertSeverity = AlertSeverity.WARNING,
    top_schemas: list[tuple[str, int]] | None = None,
) -> None:
    """Create a deduplicated operator Alert when a project's self-repair burn-share
    exceeds the efficiency ceiling.

    Self-repair tokens (reopen attempts during structured-output format recovery)
    represent overhead that reduces LLM productivity. When repair tokens consume
    an excessive fraction of total LLM tokens, it signals a drift in repair
    effectiveness and warrants investigation of the underlying failure patterns.
    This alert informs the operator without requiring manual polling of metrics.

    Deduplicated by ``(project_id, title, dismissed_at IS NULL)`` on a STABLE
    title — so an elevated burn-share that persists across many snapshot ticks
    raises exactly ONE alert per project. The current burn-share value lives in
    the message so the metric is visible without polling. Caller owns the
    transition-edge gating; this helper only enforces title-level dedup and
    commits its own ``Alert`` row.
    """
    title = f"{_SELF_REPAIR_BURN_SHARE_ALERT_TITLE_PREFIX} on project {str(project_id)[:8]}"
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
    burn_share_pct = (burn_share * 100) if burn_share is not None else 0.0
    threshold_pct = threshold * 100
    schema_suffix = ""
    if top_schemas:
        schema_lines = ", ".join(
            f"{name} ({attempts} attempts)" for name, attempts in top_schemas
        )
        schema_suffix = f" Top offending schemas: {schema_lines}."
    message = (
        f"Self-repair tokens consumed {burn_share_pct:.1f}% of total LLM tokens, "
        f"exceeding the efficiency ceiling of {threshold_pct:.1f}%. High repair "
        f"overhead indicates multiple format-recovery attempts are required; "
        f"investigate whether recent prompt or schema changes have degraded "
        f"structured-output success rates.{schema_suffix}"
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
) -> bool:
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

    Commit boundary: when it inserts a new alert this helper commits ONLY its
    own ``Alert`` row — it does NOT own, and must not be relied on to flush, any
    pending task mutation the caller staged. Callers that park a task into
    ``BLOCKED`` (``orchestrator_filters._record_defer_and_maybe_escalate``,
    ``task_lifecycle_requeue``) commit that transition themselves BEFORE calling
    here, so the durability of the task state never depends on whether this
    helper happened to create an alert (and therefore commit) this cycle.
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
        return False
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
    return True


# Stable title prefix for stale-staged-direction alerts. The title encodes the
# direction id so dedup is per-direction: a single stalled staged direction
# raises exactly ONE alert across many reaper sweeps, while distinct stalled
# directions each get their own. Keep the prefix stable.
_STALE_STAGED_DIRECTION_TITLE_PREFIX = "Stale staged direction"


async def _maybe_create_stale_staged_direction_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    *,
    direction_id: str,
    direction_text: "str | None",
    waited_days: int,
    staged_for_version: "int | None",
    open_version: "int | None",
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> bool:
    """Create a deduplicated operator Alert for a staged direction that has
    waited past the staleness threshold while its target version has NOT yet
    promoted.

    A *staged* direction (``status == "staged"``, ``staged_for_version=N``) is
    inert work that only begins once version ``N`` promotes/opens. The single
    thing that un-stages it is that promotion — but a version can stay open far
    longer than the ~14-day stall gate (deferred by repeated qualification
    failures or operator inaction), stranding the staged direction indefinitely
    and invisibly. This surfaces that stall so an operator can retarget,
    force-activate, or cancel the work. Alerting only — it never mutates the
    direction or its staging state.

    Deduplicated by ``(project_id, title, dismissed_at IS NULL)`` where the title
    encodes ``direction_id`` — so a staged direction that stays stale across many
    reaper sweeps raises exactly ONE alert, and a genuinely *different* stalled
    direction gets its own. ``project_id``-scoped with ``task_id=None`` (mirrors
    the warm-pool payoff / strategic alerts). The waited age lives in the message
    so a creeping age never defeats dedup. Returns ``True`` when it inserted a new
    alert, ``False`` when dedup short-circuited.

    Commits ONLY its own ``Alert`` row and broadcasts ``alert_created``; callers
    (the reaper staleness sweep) wrap it best-effort so an alert failure can never
    abort the sweep.
    """
    title = (f"{_STALE_STAGED_DIRECTION_TITLE_PREFIX}: {str(direction_id)[:64]}")[:200]
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return False
    label = (direction_text or "(no description)").strip()
    if len(label) > 200:
        label = label[:197] + "…"
    target = (
        f"v{staged_for_version}"
        if staged_for_version is not None
        else "an unknown version"
    )
    open_desc = (
        f"the current open version is v{open_version}"
        if open_version is not None
        else "no open version is recorded"
    )
    message = (
        f'Staged direction {direction_id} ("{label}") has waited '
        f"{waited_days} day(s) for its target version ({target}) to promote, "
        f"but {open_desc} — the version has not promoted, so the staged work "
        f"is stalled. Retarget, force-activate, or cancel it."
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
    return True


async def _resolve_stale_staged_direction_alerts(
    session: AsyncSession,
    project_id: "uuid.UUID",
    direction_ids,
) -> int:
    """Dismiss any open stale-staged-direction alert for the given directions.

    The companion to :func:`_maybe_create_stale_staged_direction_alert`. When a
    staged direction's stall finally clears — most importantly when its target
    version promotes and the direction auto-activates — the WARNING alert that
    flagged the stall no longer describes a real condition and must be resolved,
    mirroring how the deployment-HALTED recovery path stamps ``dismissed_at`` on
    its alert once recovery happens.

    The stale-staged alert title deterministically encodes the direction id, so
    the matching open alert is looked up directly per direction. We rebuild the
    lookup title with the SAME construction the create path uses (same prefix,
    same truncation), so the comparison is byte-identical. Only open
    (``dismissed_at IS NULL``), project-scoped (``task_id IS NULL``) alerts are
    touched, so this is a safe no-op when no such alert exists and idempotent
    across repeated calls (a second call finds nothing open to dismiss).

    Stamps ``dismissed_at`` on each match, commits its own rows, and broadcasts
    one ``alert_dismissed`` event per dismissed alert so the operator UI drops it
    without waiting for the next poll. Returns the number of alerts dismissed.

    Best-effort contract: callers wrap this in try/except so a resolution failure
    can never abort the activation that triggered it.
    """
    titles = {
        (f"{_STALE_STAGED_DIRECTION_TITLE_PREFIX}: {str(did)[:64]}")[:200]
        for did in (direction_ids or [])
        if did
    }
    if not titles:
        return 0
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.dismissed_at.is_(None),
            Alert.title.in_(titles),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


# Shared title prefix for the "permanently blocked" ERROR escalation alert. The
# create path appends the blocker_kind; the reconcile path matches on the prefix
# so a resumed task's stale alert is dismissed regardless of which kind it named.
_BLOCKED_ESCALATION_TITLE_PREFIX = "Task permanently blocked"


async def _resolve_blocked_escalation_alert(
    task: "Task",
    session: AsyncSession,
) -> int:
    """Dismiss any open 'permanently blocked' ERROR alert for a resumed task.

    The companion to :func:`_maybe_create_blocked_escalation_alert`. When a parked
    (escalated) blocker's need finally becomes satisfiable and the recovery sweep
    resumes the task, the ERROR alert that declared it permanently blocked no
    longer describes a real condition and must be reconciled — mirroring how
    :func:`_resolve_stale_staged_direction_alerts` dismisses its alert once the
    underlying stall clears.

    Matches on the deterministic title prefix (the create path appends the
    blocker_kind, which may already have been cleared by the time we resume), so
    any open ``Task permanently blocked: …`` alert for this task is dismissed.
    Only open (``dismissed_at IS NULL``), task-scoped alerts are touched, so this
    is a safe no-op when none exists and idempotent across repeated calls.

    Best-effort contract: the caller wraps this in try/except so a reconcile
    failure can never abort the resume that triggered it. Returns the number of
    alerts dismissed.
    """
    result = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title.like(f"{_BLOCKED_ESCALATION_TITLE_PREFIX}%"),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(task.project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


async def _maybe_create_blocked_escalation_alert(
    task: "Task",
    session: AsyncSession,
    *,
    blocker_kind: str,
    detail: str,
) -> None:
    """Create ONE deduplicated ERROR alert for a permanently-stuck BLOCKED task.

    The terminal escalation path of the unblock sweep
    (``reaper_sweeps.sweep_unblock_blocked_tasks``): when a BLOCKED task's
    precondition is genuinely unsatisfiable (its dependency cascade-cancelled)
    or its blocker outlives the escalation deadline, the sweep stops rechecking
    and raises this alert so the operator can intervene (requeue, re-scope, or
    cancel). It is distinct from ``_maybe_create_precondition_blocker_alert``
    (a WARNING raised the moment a blocker first persists) — this ERROR means
    "automatic recovery has given up".

    Deduplicated by ``(task_id, title)`` against undismissed alerts, where the
    title encodes ``blocker_kind``, so the deadline/dead-dependency escalation
    fires exactly once per blocked task and does not re-fire while the operator
    still has the alert open.
    """
    title = f"{_BLOCKED_ESCALATION_TITLE_PREFIX}: {blocker_kind}"[:200]
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
        severity=AlertSeverity.ERROR,
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
            "severity": AlertSeverity.ERROR.value,
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


# Stable title for the per-project wake-delivery-stall alert. Kept CONSTANT
# (the drifting stall_cycles count lives in the message) so a persistent stall
# dedups to exactly ONE alert per project across reaper ticks.
_WAKE_DELIVERY_STALL_ALERT_TITLE = "Wake delivery stalled — parked tasks not waking"


async def _maybe_create_wake_delivery_stall_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    *,
    stall_cycles: int,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> int:
    """Raise a deduplicated alert when wake delivery is stalled.

    A stalled wake-delivery sweep means PARKED tasks whose registered wake
    sources have fired are silently never advanced. Deduplicated on a STABLE
    title so a sustained stall raises exactly ONE alert per project; the
    drifting stall_cycles count lives in the message so a moving counter
    never defeats dedup. Caller owns the commit.

    Returns 1 when a NEW alert was created, else 0.
    """
    title = _WAKE_DELIVERY_STALL_ALERT_TITLE
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first() is not None:
        return 0

    msg = (
        f"The wake-delivery sweep has made no progress against the FIRED "
        f"registration backlog for {stall_cycles} consecutive reaper cycles. "
        f"Parked tasks whose wake sources have fired are not being advanced — "
        f"check for a wedged or crashed delivery path."
    )[:1000]

    alert = Alert(
        project_id=project_id,
        severity=severity,
        title=title,
        message=msg,
    )
    session.add(alert)
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
        },
    )
    return 1


async def _resolve_wake_delivery_stall_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
) -> int:
    """Dismiss any open wake-delivery-stall alert for a project.

    The companion to :func:`_maybe_create_wake_delivery_stall_alert`. Called
    when the wake-delivery sweep recovers (delivery_stalled=False) to stamp
    dismissed_at on any open stall alert so the operator UI drops it. Commits
    its own rows and broadcasts alert_dismissed per dismissed alert.

    Returns the number of alerts dismissed (0 or 1 in practice).
    """
    title = _WAKE_DELIVERY_STALL_ALERT_TITLE
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)



# Runner health alert helpers live in app.runner_health_alerts; re-exported here
# so existing ``from app.alert_dispatch import ...`` callers keep working.
from app.runner_health_alerts import (  # noqa: F401
    _FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE,
    _CONTAINER_RUNNER_HOLD_ALERT_TITLE,
    _LOCAL_RUNNER_HOLD_ALERT_TITLE,
    _RUNNER_DEACTIVATED_EVENT_TYPE,
    _RUNNER_DEGRADED_EVENT_TYPE,
    _RUNNER_ESCALATION_AGE_SECONDS_DEFAULT,
    _RUNNER_ESCALATION_FIRST_SECONDS,
    _RUNNER_ESCALATION_REPEAT_SECONDS,
    _RUNNER_LOW_DISK_EVENT_TYPE,
    _RUNNER_LOW_MEMORY_EVENT_TYPE,
    _fleet_no_healthy_runner_dedupe_key,
    _maybe_create_container_runner_hold_alert,
    _maybe_create_fleet_no_healthy_runner_alert,
    _maybe_create_local_runner_hold_alert,
    _maybe_create_runner_deactivated_alert,
    _maybe_create_runner_degraded_alert,
    _maybe_create_runner_low_disk_alert,
    _maybe_create_runner_low_memory_alert,
    _maybe_create_runner_post_update_silent_alert,
    _maybe_escalate_runner_deactivated_alert,
    _maybe_escalate_runner_deactivated_alerts,
    _maybe_escalate_runner_degraded_alerts,
    _resolve_container_runner_hold_alert,
    _resolve_fleet_no_healthy_runner_alert,
    _resolve_local_runner_hold_alert,
    _resolve_runner_deactivated_alert,
    _resolve_runner_degraded_alert,
    _resolve_runner_low_disk_alert,
    _resolve_runner_low_memory_alert,
    _resolve_runner_post_update_silent_alert,
    _runner_deactivated_dedupe_key,
    _runner_degraded_dedupe_key,
    _runner_escalation_last_notified,
    _runner_low_disk_dedupe_key,
    _runner_low_memory_dedupe_key,
    _runner_outage_escalated_keys,
    # Idle-GPU alert: runner GPU utilization below threshold
    _RUNNER_IDLE_GPU_EVENT_TYPE,
    _RUNNER_IDLE_GPU_THRESHOLD_PCT_DEFAULT,
    _maybe_create_runner_idle_gpu_alert,
    _resolve_runner_idle_gpu_alert,
    _runner_idle_gpu_dedupe_key,
    # Token-stale alert: runner credential not rotated within recommended window
    _RUNNER_TOKEN_STALE_EVENT_TYPE,
    _maybe_create_runner_token_stale_alert,
    _resolve_runner_token_stale_alert,
    _runner_token_stale_dedupe_key,
    # Repeated cross-project runner access alert
    _RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE,
    _RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD,
    _RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS,
    _maybe_create_runner_cross_project_access_alert,
    _record_runner_cross_project_mismatch,
    _resolve_runner_cross_project_access_alert,
    _runner_cross_project_access_dedupe_key,
    _runner_cross_project_mismatch_timestamps,
)

# Stable title prefix for the per-project cost-telemetry-reconciliation-failure alert.
# The project id suffix ensures dedup is per-project: one undismissed alert per
# project while the mismatch persists. Keep the prefix stable.
_COST_TELEMETRY_RECONCILIATION_ALERT_TITLE_PREFIX = (
    "Cost telemetry reconciliation failed tolerance"
)


async def _maybe_create_cost_telemetry_reconciliation_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    *,
    absolute_delta: float,
    tolerance: float,
    task_count: int,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Create a deduplicated operator Alert when the daily efficiency snapshot's
    cost telemetry reconciliation exceeds the declared tolerance.

    LLM cost telemetry is computed via two structurally independent paths
    (snapshot aggregation and flat per-attempt iteration) and must agree within
    COST_TELEMETRY_TOLERANCE (1e-6 USD). A mismatch indicates that the per-task
    cost numbers the snapshot reports are suspect — operators should investigate
    before relying on cost-per-merged-PR or cost-per-verified-task metrics.

    Deduplicated on a STABLE title so a mismatch that persists across snapshot
    ticks raises exactly ONE alert per project. The drifting delta and task count
    live in the message so a moving metric never defeats dedup. Caller owns the
    commit.
    """
    title = (
        f"{_COST_TELEMETRY_RECONCILIATION_ALERT_TITLE_PREFIX}"
        f" on project {str(project_id)[:8]}"
    )[:200]
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
        f"LLM cost telemetry reconciliation failed: snapshot vs direct-path "
        f"cost delta ${absolute_delta:.6f} exceeds tolerance ${tolerance:.6f} "
        f"over {task_count} task(s). The efficiency snapshot's LLM cost "
        f"data may be unreliable."
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
