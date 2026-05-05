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

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Alert, AlertSeverity, Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
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

_ACTIVE_INVESTIGATE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)


async def _last_spawn_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Optional[Task]:
    """Return the most recent dispatcher-spawned task for this class, if any.

    Filters by ``context['spawned_by']`` and ``context['regression_class']`` in
    the WHERE clause so the cooldown guard cannot be silently bypassed by a
    busy project generating >N unrelated tasks within the cooldown window.
    """
    return (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["spawned_by"].as_string()
            == "pipeline_regression_dispatcher",
            Task.context["regression_class"].as_string() == regression_class,
        ).order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()


async def _in_flight_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Optional[Task]:
    """Return the in-flight investigation for this regression class, if any.

    Only catches dispatcher-spawned tasks: any task with
    ``context.regression_class == <class>`` in non-terminal status. The
    crash-investigation path in ``lifecycle._spawn_crash_investigations``
    does not write ``regression_class`` and is deduped separately on
    ``service_name``.
    """
    rows = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(_ACTIVE_INVESTIGATE_STATUSES),
        )
    )).scalars().all()
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
    rows = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
        )
    )).scalars().all()
    n = 0
    for t in rows:
        ptype = (t.first_payload or {}).get("type")
        if ptype in ("research", "investigate"):
            n += 1
    return n


def _format_signal_lines(
    signal_name: str,
    regression_class: str,
    prior_rate: Optional[float],
    current_rate: Optional[float],
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


async def _maybe_spawn(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    signal_name: str,
    regression_class: str,
    prior_rate: Optional[float],
    current_rate: Optional[float],
    sample_size: int,
    magnitude: Optional[float],
    extras: list[str] | None = None,
    title_hint: str,
) -> Optional[Task]:
    """Apply dedup + cooldown gates and spawn an investigate task on green light."""
    logger.info(
        "[REGRESSION_DISPATCHER] signal-tripped project=%s class=%s prior=%s "
        "current=%s sample=%d",
        project_id, regression_class, prior_rate, current_rate, sample_size,
    )

    in_flight = await _in_flight_for_class(session, project_id, regression_class)
    if in_flight is not None:
        logger.info(
            "[REGRESSION_DISPATCHER] dedup-hit project=%s class=%s in_flight_task=%s",
            project_id, regression_class, in_flight.id,
        )
        return None

    last_spawn = await _last_spawn_for_class(session, project_id, regression_class)
    now = _utcnow()
    if last_spawn is not None and last_spawn.created_at:
        last_at = last_spawn.created_at
        if last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
        if now < last_at + _REGRESSION_COOLDOWN:
            logger.info(
                "[REGRESSION_DISPATCHER] cooldown-hit project=%s class=%s "
                "last_spawn=%s cooldown_until=%s",
                project_id, regression_class, last_at.isoformat(),
                (last_at + _REGRESSION_COOLDOWN).isoformat(),
            )
            return None

    cooldown_until = (now + _REGRESSION_COOLDOWN).isoformat()
    prompt = _format_signal_lines(
        signal_name=signal_name,
        regression_class=regression_class,
        prior_rate=prior_rate,
        current_rate=current_rate,
        sample_size=sample_size,
        extras=extras,
    )
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": "main",
            "effort": "low",
        },
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
    new_task = Task(
        project_id=project_id,
        title=f"Investigate pipeline regression: {title_hint}"[:200],
        description=(
            f"Auto-spawned from pipeline-regression dispatcher "
            f"({signal_name})"
        )[:500],
        payloads=[payload],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "PIPELINE_REGRESSION_DISPATCHER",
            "regression_class": regression_class,
            "signal_name": signal_name,
        },
    ))
    session.add(Alert(
        project_id=project_id,
        severity=AlertSeverity.INFO,
        title=f"Pipeline regression investigation spawned: {regression_class}"[:200],
        message=(
            f"Signal {signal_name} tripped (prior={prior_rate}, "
            f"current={current_rate}, n={sample_size}). Investigation "
            f"task {new_task.id} queued."
        )[:1000],
    ))
    logger.info(
        "[REGRESSION_DISPATCHER] spawn-success project=%s class=%s task_id=%s",
        project_id, regression_class, new_task.id,
    )
    return new_task


async def _evaluate_sr_catch_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Re-read the persisted SR catch-rate trends snapshot and trip per-category."""
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD  # noqa: PLC0415

    if spawn_budget_left <= 0:
        return 0
    doc = project_design_doc or {}
    trends = doc.get("sr_catch_rate_trends") or {}
    categories = trends.get("categories") or {}
    if not isinstance(categories, dict):
        return 0
    spawned = 0
    for cat, data in categories.items():
        if spawned >= spawn_budget_left:
            break
        if not isinstance(data, dict):
            continue
        prior = data.get("previous_hit_rate")
        curr = data.get("current_hit_rate")
        if prior is None or curr is None:
            continue
        delta = curr - prior
        if -delta < _TREND_DELTA_THRESHOLD:
            continue
        # Per-category sample sizes are persisted alongside the rates by
        # compute_sr_catch_rate_trends (current_human_count /
        # previous_human_count). Fall back to _MIN_SR_SAMPLE only for legacy
        # snapshots predating the schema bump — which is the floor the trends
        # function already enforces at the project level (line 2087-2088).
        raw_sample = data.get("current_human_count")
        if raw_sample is not None:
            sample_size = int(raw_sample)
            if sample_size < _MIN_SR_SAMPLE:
                continue
        else:
            sample_size = _MIN_SR_SAMPLE
        regression_class = f"sr_catch_rate:{cat}"
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="sr_catch_rate",
            regression_class=regression_class,
            prior_rate=prior,
            current_rate=curr,
            sample_size=sample_size,
            magnitude=round(prior - curr, 3),
            extras=[f"Self-review catch-rate for category '{cat}' is regressing."],
            title_hint=f"SR catch-rate {cat}",
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_fix_loop_divergence_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Recompute the fix-loop divergence regression and trip per loop."""
    from app.playbook_learning import (  # noqa: PLC0415
        _MIN_SR_SAMPLE,
        _TREND_DELTA_THRESHOLD,
    )
    from app.models import Task as TaskModel  # noqa: PLC0415

    if spawn_budget_left <= 0:
        return 0

    now = _utcnow()
    tasks = (await session.execute(
        select(TaskModel).where(
            TaskModel.project_id == project_id,
            TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            TaskModel.created_at >= now - timedelta(days=14),
        )
    )).scalars().all()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    c7 = _naive(now - timedelta(days=7))
    c14 = _naive(now - timedelta(days=14))
    loops = ("lint", "test", "check")

    def _stats(start: datetime, end: datetime) -> dict[str, dict]:
        s: dict[str, dict] = {l: {"n": 0, "d": 0} for l in loops}
        for t in tasks:
            ts = _naive(t.created_at) if t.created_at else None
            if ts is None or not (start <= ts < end):
                continue
            for loop in loops:
                bucket = (t.fix_loop_history or {}).get(loop) or []
                if not bucket:
                    continue
                last_exit: Optional[str] = None
                for ev in reversed(bucket):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None and isinstance(bucket[-1], dict):
                    last_exit = bucket[-1].get("exit_reason")
                s[loop]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    s[loop]["d"] += 1
        return s

    curr = _stats(c7, datetime(9999, 12, 31))
    prev = _stats(c14, c7)

    spawned = 0
    for loop in loops:
        if spawned >= spawn_budget_left:
            break
        cn, cd = curr[loop]["n"], curr[loop]["d"]
        pn, pd = prev[loop]["n"], prev[loop]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            continue
        curr_rate = cd / cn
        prior_rate = pd / pn
        delta = curr_rate - prior_rate
        if delta < _TREND_DELTA_THRESHOLD:
            continue
        regression_class = f"fix_loop_divergence:{loop}"
        spawn = await _maybe_spawn(
            session,
            project_id,
            signal_name="fix_loop_divergence",
            regression_class=regression_class,
            prior_rate=round(prior_rate, 3),
            current_rate=round(curr_rate, 3),
            sample_size=cn,
            magnitude=round(delta, 3),
            extras=[
                f"Fix-loop '{loop}' divergence rate is rising: "
                f"{pd}/{pn} → {cd}/{cn} divergent.",
            ],
            title_hint=f"fix-loop divergence {loop}",
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
    from app.routers.admin_digest import compute_pipeline_health_summary  # noqa: PLC0415
    from app.models import Task as TaskModel  # noqa: PLC0415

    if spawn_budget_left <= 0:
        return 0

    summary = await compute_pipeline_health_summary(session, project_id, lookback_hours=24)
    trend = summary.get("trend")
    if trend != "declining":
        return 0
    success_rate = summary.get("success_rate")

    # Recompute total terminal count over the same 24h window — the summary
    # doesn't expose the raw sample size, but the gate is conservative (>=10).
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    rows = (await session.execute(
        select(TaskModel.id, TaskModel.status, TaskModel.updated_at).where(
            TaskModel.project_id == project_id,
            TaskModel.updated_at >= window_start,
            TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
    )).all()
    sample_size = len(rows)
    if sample_size < _PIPELINE_SUCCESS_MIN_SAMPLE:
        return 0

    failure_distribution = summary.get("failure_distribution") or {}
    extras_lines = ["24h pipeline success-rate is declining."]
    if failure_distribution:
        top = ", ".join(f"{k}={v}" for k, v in list(failure_distribution.items())[:3])
        extras_lines.append(f"Top failure classes: {top}")

    spawn = await _maybe_spawn(
        session,
        project_id,
        signal_name="pipeline_success_rate",
        regression_class="pipeline_success_rate",
        prior_rate=None,
        current_rate=success_rate,
        sample_size=sample_size,
        magnitude=None,
        extras=extras_lines,
        title_hint="24h pipeline success-rate declining",
    )
    return 1 if spawn is not None else 0


async def run_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Evaluate all subscribed pipeline-regression signals for ``project_id``.

    Returns the number of investigate tasks spawned this call. Caller is
    responsible for committing the session — this matches the
    ``_spawn_proactive_fingerprint_investigations`` pattern in
    ``anomaly_detection`` and lets the orchestrator coalesce dispatcher work
    with its existing 60s tick.
    """
    from app.models import Project  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        return 0

    # Cap 1: research slot budget shared with autonomous research.
    max_total = int(_runtime_config.get("max_total_research_per_project", 5) or 5)
    active_research = await _count_active_research_for_project(session, project_id)
    project_remaining = max_total - active_research
    if project_remaining <= 0:
        logger.info(
            "[REGRESSION_DISPATCHER] cap-hit project=%s active_research=%d/%d "
            "(max_total_research_per_project) — skipping",
            project_id, active_research, max_total,
        )
        return 0

    # Cap 2: per-cycle system-wide concurrency budget.
    spawn_budget = min(project_remaining, _REGRESSION_DISPATCHER_SYSTEM_CAP)

    spawned = 0
    spawned += await _evaluate_sr_catch_rate_signal(
        session, project_id, project.design_doc, spawn_budget - spawned,
    )
    spawned += await _evaluate_fix_loop_divergence_signal(
        session, project_id, spawn_budget - spawned,
    )
    spawned += await _evaluate_pipeline_success_signal(
        session, project_id, spawn_budget - spawned,
    )

    if spawned:
        await session.commit()
    return spawned
