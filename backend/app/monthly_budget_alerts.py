"""Monthly and daily budget target enforcement — operator-configurable cost alerting.

Replaces reliance on the Terraform-hardcoded GCP billing budget as the
primary cost-signal path.  The GCP-native ``google_billing_budget`` resource
stays in place as a deliberately-high backstop (see ``infra/terraform/budget.tf``).

The operator sets a ``monthly_budget_target_usd`` via runtime config (editable
live from the admin UI, no deploy required).  On every call to
:func:`check_monthly_budget`, Blueprint's own cost telemetry (VM runtime cost,
off-VM compute spend, warm-pool idle cost) is summed month-to-date across all
projects and compared against the target.  Alerts fire at 50%, 90%, and 100%
of the target, mirroring the GCP budget's own threshold shape.

:func:`check_daily_budget` enforces the complementary ``daily_spend_budget_usd``
key: same attribution source, same fail-closed semantics, but windowed to the
current UTC calendar day.  A single alert fires when today's spend reaches 100%
of the daily target.  Unset key = gate disabled (no behaviour change).

Design follows the existing ``cost_ceiling.check_daily_cost_ceiling`` pattern.
Deduplication is per-threshold-level so the operator is not spammed on every
periodic tick.
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, _utcnow
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)

# Threshold fractions of the monthly target that trigger alerts (mirrors the GCP
# budget's 50/90/100% threshold shape).
_BUDGET_THRESHOLDS: tuple[float, ...] = (0.50, 0.90, 1.00)


async def check_monthly_budget(
    session: AsyncSession,
) -> tuple[bool, float | None]:
    """Compute fleet-wide month-to-date spend and fire alerts at threshold crossings.

    Reads ``monthly_budget_target_usd`` from runtime config.  When the target is
    ``<= 0`` the check is disabled (no-op).  Alerts are created once per
    threshold level — deduplication is per (project_id, dedupe_key) so crossing
    the same threshold again on a later tick does not create a duplicate alert.

    The function iterates all projects that have month-to-date VM spend and
    creates an Alert on the project with the highest spend, since the Alert model
    requires a ``project_id`` FK.  The alert's ``dedupe_key`` encodes the
    threshold fraction so each level alerts at most once until dismissed.

    Returns ``(exhausted, usage_ratio)`` where ``exhausted`` is True when
    month-to-date spend has reached or exceeded 100% of the target, and
    ``usage_ratio`` is the ratio of spend to target (or None when the check
    is disabled / there is no spend to compare).
    """
    target = float(_runtime_config.get("monthly_budget_target_usd", 0))
    if target <= 0:
        return False, None

    month_start = _utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Build month-to-date spend: VM runtime, off-VM compute, and pool idle.
    month_to_date_total, top_project_id, project_spend_map = (
        await _compute_month_to_date_spend(session, month_start)
    )

    if month_to_date_total <= 0:
        return False, None

    usage_ratio = month_to_date_total / target

    # Check each threshold level and fire an alert if crossed and not already
    # alerted (dedupe_key per threshold prevents duplicates).
    for fraction in _BUDGET_THRESHOLDS:
        if usage_ratio < fraction:
            continue  # threshold not yet reached

        if not top_project_id:
            continue

        dedupe_key = f"monthly_budget_{int(fraction * 100)}pct"

        # Dedup check: skip if an undismissed alert with this dedupe_key exists.
        existing = (
            await session.execute(
                select(Alert).where(
                    Alert.dedupe_key == dedupe_key,
                    Alert.project_id == top_project_id,
                    Alert.dismissed_at.is_(None),
                )
            )
        ).scalars().first()
        if existing is not None:
            continue

        pct = int(fraction * 100)
        if fraction >= 0.90:
            severity = AlertSeverity.ERROR
        else:
            severity = AlertSeverity.WARNING

        total_by_project_lines = "\n".join(
            sorted(
                f"  project={pid}: ${cost:.2f}"
                for pid, cost in project_spend_map.items()
            )
        ) if project_spend_map else "  (no per-project breakdown available)"

        alert = Alert(
            project_id=top_project_id,
            severity=severity,
            title=f"Monthly budget at {pct}%",
            message=(
                f"Month-to-date total spend ${month_to_date_total:.2f} "
                f"has reached {pct}% of the monthly target ${target:.2f}.\n"
                f"Per-project spend:\n{total_by_project_lines}"
            ),
            dedupe_key=dedupe_key,
            domain="billing",
            event_type="MONTHLY_BUDGET_ALERT",
            source="monthly_budget_alerts",
        )
        session.add(alert)
        await broadcast_to_project(
            str(top_project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": severity.value,
                "title": alert.title,
            },
        )
        logger.warning(
            "[BUDGET] Monthly budget at %d%% — $%.2f of $%.2f target. "
            "Top-spending project %s. Alert %s created.",
            pct,
            month_to_date_total,
            target,
            top_project_id,
            alert.id,
        )

    return usage_ratio >= 1.0, usage_ratio


async def check_daily_budget(
    session: AsyncSession,
) -> tuple[bool, float | None]:
    """Compute fleet-wide today-to-date spend and fire an alert when the daily budget is reached.

    Reads ``daily_spend_budget_usd`` from runtime config.  When the key is
    absent or ``<= 0`` the check is a no-op (gate disabled — no behaviour change).

    Uses the same cost attribution source as :func:`check_monthly_budget` but
    with a window starting at today's UTC midnight.  A single alert fires when
    today's spend reaches 100% of the daily target; deduplication is by the
    same (project_id, dedupe_key) pattern so the operator is not re-alerted on
    every periodic tick.

    Returns ``(exhausted, usage_ratio)`` where ``exhausted`` is True when
    today-to-date spend has reached or exceeded the daily target, and
    ``usage_ratio`` is the ratio of spend to target (or None when the check
    is disabled / there is no spend to compare).
    """
    target = float(_runtime_config.get("daily_spend_budget_usd", 0))
    if target <= 0:
        return False, None

    now = _utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Reuse the month-to-date spend computation with day_start as the window.
    day_total, top_project_id, project_spend_map = (
        await _compute_month_to_date_spend(session, day_start)
    )

    if day_total <= 0:
        return False, None

    usage_ratio = day_total / target
    if usage_ratio < 1.0 or not top_project_id:
        return False, usage_ratio

    dedupe_key = "daily_budget_100pct"

    # Dedup by calendar day: include the date in the key so the gate resets
    # each midnight without requiring the operator to dismiss the previous alert.
    dedupe_key = f"daily_budget_100pct_{now.strftime('%Y%m%d')}"

    existing = (
        await session.execute(
            select(Alert).where(
                Alert.dedupe_key == dedupe_key,
                Alert.project_id == top_project_id,
                Alert.dismissed_at.is_(None),
            )
        )
    ).scalars().first()

    if existing is None:
        total_by_project_lines = "\n".join(
            sorted(
                f"  project={pid}: ${cost:.2f}"
                for pid, cost in project_spend_map.items()
            )
        ) if project_spend_map else "  (no per-project breakdown available)"

        alert = Alert(
            project_id=top_project_id,
            severity=AlertSeverity.ERROR,
            title="Daily spend budget reached",
            message=(
                f"Today's total spend ${day_total:.2f} "
                f"has reached 100% of the daily budget ${target:.2f}.\n"
                f"Per-project spend:\n{total_by_project_lines}"
            ),
            dedupe_key=dedupe_key,
            domain="billing",
            event_type="DAILY_BUDGET_ALERT",
            source="monthly_budget_alerts",
        )
        session.add(alert)
        await broadcast_to_project(
            str(top_project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": AlertSeverity.ERROR.value,
                "title": alert.title,
            },
        )
        logger.warning(
            "[BUDGET] Daily budget reached — $%.2f of $%.2f daily target. "
            "Top-spending project %s. Alert %s created.",
            day_total,
            target,
            top_project_id,
            alert.id,
        )

    return True, usage_ratio


async def _compute_month_to_date_spend(
    session: AsyncSession,
    month_start: datetime,
) -> tuple[float, str | None, dict[str, float]]:
    """Compute month-to-date spend across all projects.

    Returns ``(total_spend, top_project_id, {project_id: spend})`` using the
    same cost telemetry as ``cost_ceiling.py`` and ``efficiency_snapshot.py``.

    The top-spending project's id is returned so the caller can attach the
    alert to a real project (required by the Alert model).
    """
    from sqlalchemy.orm import load_only  # noqa: PLC0415 — lazy import for module isolation

    from app.compute_jobs import (  # noqa: PLC0415
        get_recent_terminal_compute_spend_usd,
        get_active_compute_job_accrued_spend_usd,
    )
    from app.efficiency_snapshot import _compute_windowed_idle_cost  # noqa: PLC0415
    from app.models import Task, get_task_total_vm_cost_usd  # noqa: PLC0415

    now = _utcnow()

    # Gather all tasks with VM provisioned since month_start.
    db_tasks = (
        await session.execute(
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.project_id,
                    Task.vm_provisioned_at,
                    Task.vm_stopped_at,
                    Task.vm_spot,
                    Task.vm_machine_type,
                    Task.vm_runtime_cost_banked_db,
                    Task.status,
                    Task.context,
                    raiseload=True,
                )
            )
            .where(
                Task.vm_provisioned_at.is_not(None),
                Task.vm_provisioned_at >= month_start,
            )
        )
    ).scalars().all()

    # Per-project VM spend accumulator.
    project_vm_spend: dict[str, float] = {}
    projects_seen: set[str] = set()

    for t in db_tasks:
        pid = str(t.project_id)
        projects_seen.add(pid)
        if t.vm_stopped_at is None:
            cost = get_task_total_vm_cost_usd(t, stopped_at=now)
        else:
            cost = get_task_total_vm_cost_usd(t)
        project_vm_spend[pid] = project_vm_spend.get(pid, 0.0) + cost

    # Fold in compute-job spend per project (only for projects we saw).
    for pid in projects_seen:
        import uuid  # noqa: PLC0415 — inline import for module isolation

        pid_uuid = uuid.UUID(pid)
        try:
            compute_total = float(
                await get_recent_terminal_compute_spend_usd(
                    session, project_id=pid_uuid, since=month_start
                )
            )
        except Exception:  # noqa: BLE001 — best-effort
            compute_total = 0.0
        try:
            inflight_total = float(
                await get_active_compute_job_accrued_spend_usd(
                    session, project_id=pid_uuid
                )
            )
        except Exception:  # noqa: BLE001 — best-effort
            inflight_total = 0.0
        try:
            pool_idle_result = await _compute_windowed_idle_cost(
                session, pid_uuid, month_start
            )
            pool_total = float(pool_idle_result.get("total") or 0.0)
        except Exception:  # noqa: BLE001 — best-effort
            pool_total = 0.0

        project_vm_spend[pid] = (
            project_vm_spend.get(pid, 0.0)
            + compute_total
            + inflight_total
            + pool_total
        )

    total_spend = sum(project_vm_spend.values())
    top_project_id: str | None = (
        max(project_vm_spend, key=project_vm_spend.get)
        if project_vm_spend
        else None
    )

    return total_spend, top_project_id, project_vm_spend
