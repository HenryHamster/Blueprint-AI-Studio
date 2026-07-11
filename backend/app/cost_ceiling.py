"""Daily cost ceiling enforcement.

Extracted from app.routers.agent_status so the ceiling check can be called
from both the ordinary agent terminal-status path and the compute-job wake
finalization path without introducing a circular import (agent_status.py is
a router and must not be imported by task_completion.py or task_lifecycle.py).
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, Task, TaskStatus, get_task_total_vm_cost_usd
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


async def check_daily_cost_ceiling(task: Task, session: AsyncSession) -> None:
    """Create a WARNING alert if the daily VM + compute spend exceeds the configured ceiling.

    Pure DB mutation: adds an Alert via session.add() with NO commit, so the
    caller controls when the alert is durably persisted. Side effects
    (broadcast + BUDGET_EXCEEDED notification) fire inline after the session.add()
    call — callers that need to defer those effects should use a post-commit hook.

    Deduplicated: only one undismissed 'Daily cost ceiling exceeded' alert per
    project is created regardless of how many times this is called.
    """
    ceiling = float(_runtime_config.get("daily_cost_ceiling_usd", 0))
    if ceiling <= 0:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Query tasks with an active VM today: still running (vm_stopped_at IS NULL)
    # or stopped within today's window. Exclude current task to avoid double-counting
    # (its vm_stopped_at was just set in-memory and may be flushed before the query runs).
    db_tasks = (
        await session.execute(
            select(Task)
            .options(
                load_only(
                    Task.vm_provisioned_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    Task.vm_machine_type,
                    Task.vm_runtime_cost_banked_db,
                    Task.context,
                raiseload=True,)
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.vm_provisioned_at.is_not(None),
                or_(
                    Task.vm_stopped_at.is_(None),
                    Task.vm_stopped_at >= today_start,
                ),
            )
        )
    ).scalars().all()

    daily_total = 0.0
    for t in db_tasks:
        if t.vm_stopped_at is None:
            # In-flight: estimate cost to now using machine-type-aware helper
            daily_total += get_task_total_vm_cost_usd(t, stopped_at=now)
        else:
            daily_total += get_task_total_vm_cost_usd(t)

    # Always include the current task's cost explicitly (it is in-memory, not yet committed).
    if task.vm_provisioned_at:
        if task.vm_stopped_at and task.vm_stopped_at >= today_start:
            daily_total += get_task_total_vm_cost_usd(task)
        elif task.vm_stopped_at is None:
            daily_total += get_task_total_vm_cost_usd(task, stopped_at=now)

    vm_total = daily_total

    # Off-VM compute jobs accrue their own durable spend ledger. Fold today's
    # settled (terminal, costed) compute-job spend into the daily total so the
    # ceiling reflects total project spend, not just agent-VM runtime.
    from app.compute_jobs import (
        get_recent_terminal_compute_spend_usd,
        get_active_compute_job_accrued_spend_usd,
    )

    compute_total = await get_recent_terminal_compute_spend_usd(
        session, project_id=task.project_id, since=today_start
    )
    # In-flight (non-terminal) compute jobs accumulate real spend before reaching
    # a terminal status. Include their accrued cost so the ceiling fires as soon
    # as the running total (including uncommitted compute spend) breaches the cap.
    inflight_compute_total = await get_active_compute_job_accrued_spend_usd(
        session, project_id=task.project_id
    )
    # Warm-pool idle VMs accumulate cost even when no task is running. Include
    # the project's current pool idle spend so the ceiling accounts for total
    # platform cost, not just active task spend.
    pool_idle_total = 0.0
    try:
        from app.efficiency_snapshot import _compute_windowed_idle_cost

        pool_idle_result = await _compute_windowed_idle_cost(
            session, task.project_id, today_start
        )
        pool_idle_total = float(pool_idle_result.get("total") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort; never block the ceiling check
        pool_idle_total = 0.0
    daily_total = vm_total + compute_total + inflight_compute_total + pool_idle_total

    if daily_total <= ceiling:
        return

    alert_title = "Daily cost ceiling exceeded"
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == task.project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == alert_title,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return

    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=alert_title,
        message=(
            f"Daily total spend ${daily_total:.2f} exceeds ceiling ${ceiling:.2f} "
            f"(VM ${vm_total:.2f} + settled compute ${compute_total:.2f} "
            f"+ inflight compute ${inflight_compute_total:.2f} "
            f"+ pool idle ${pool_idle_total:.2f})."
        ),
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert_title},
    )
    logger.warning(
        f"[COST] Daily cost ceiling exceeded for project {task.project_id}: "
        f"total ${daily_total:.2f} > ${ceiling:.2f} "
        f"(VM ${vm_total:.2f} + settled compute ${compute_total:.2f} "
        f"+ inflight compute ${inflight_compute_total:.2f} "
        f"+ pool idle ${pool_idle_total:.2f})"
    )
    try:
        from app.notification_dispatch import dispatch_external_notification
        from app.failure_classification import classify_failure

        ctx = task.context or {}
        first_payload = task.first_payload or {}
        direction_text = (
            ctx.get("direction_text") or first_payload.get("direction_text") or ""
        )
        # Aggregate today's cost by failure class across this project's tasks.
        # Exclude current task (added explicitly below) to avoid double-counting via identity map.
        cost_rows = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.vm_provisioned_at,
                            Task.vm_stopped_at,
                            Task.vm_spot,
                            Task.vm_machine_type,
                            Task.vm_runtime_cost_banked_db,
                            Task.status,
                            Task.id,
                            Task.context,
                            Task.latest_status_message,
                            # Read below for FAILED rows: `t.last_heartbeat or
                            # t.started_at` (classify_failure's at_time) and
                            # classify_failure → _stuck_llm_call_no_progress
                            # (failure_classification.py) reads api_call_summary;
                            # the raiseload error is otherwise silently swallowed
                            # by this block's broad except, dropping the ENTIRE
                            # BUDGET_EXCEEDED notification (the dispatch below
                            # never runs), not just the cost breakdown.
                            Task.last_heartbeat,
                            Task.started_at,
                            Task.api_call_summary,
                        raiseload=True,)
                    )
                    .where(
                        Task.project_id == task.project_id,
                        Task.id != task.id,
                        Task.vm_provisioned_at.is_not(None),
                        or_(
                            Task.vm_stopped_at.is_(None),
                            Task.vm_stopped_at >= today_start,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        cost_by_failure_class: dict[str, float] = {}
        for t in [*list(cost_rows), task]:
            if not t.vm_provisioned_at:
                continue
            if t.vm_stopped_at is None:
                t_cost = get_task_total_vm_cost_usd(t, stopped_at=now)
            else:
                t_cost = get_task_total_vm_cost_usd(t)
            status_val = getattr(t.status, "value", None) or (
                t.status if isinstance(t.status, str) else "UNKNOWN"
            )
            bucket = (
                classify_failure(t, at_time=t.last_heartbeat or t.started_at)
                if status_val == TaskStatus.FAILED.value
                else status_val
            )
            cost_by_failure_class[bucket] = round(
                cost_by_failure_class.get(bucket, 0.0) + t_cost, 4
            )
        _budget_future = asyncio.ensure_future(
            dispatch_external_notification(
                task.project_id,
                "BUDGET_EXCEEDED",
                {
                    "task_id": str(task.id),
                    "daily_total_usd": round(daily_total, 2),
                    "daily_vm_cost_usd": round(vm_total, 2),
                    "daily_compute_cost_usd": round(compute_total, 2),
                    "daily_inflight_compute_cost_usd": round(inflight_compute_total, 2),
                    "daily_pool_idle_cost_usd": round(pool_idle_total, 2),
                    "ceiling_usd": round(ceiling, 2),
                    "direction_text": direction_text[:200],
                    "cost_by_failure_class": cost_by_failure_class,
                },
            )
        )
        del _budget_future  # fire-and-forget; stored to satisfy RUF006
    except Exception as _ne:  # noqa: BLE001 — best-effort notify; never block the ceiling
        logger.warning(f"[NOTIFY] BUDGET_EXCEEDED dispatch setup failed: {_ne}")
