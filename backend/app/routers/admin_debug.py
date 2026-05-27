"""Admin diagnostics / debug endpoints.

Project-level and global diagnostic tools. These endpoints are read-only
snapshots of pipeline state used by the admin UI to debug why autonomy is
(or isn't) spawning, inspect strategic cycle state, surface stuck tasks,
and reset circuit breakers.
"""

import os
import uuid
import asyncio
import logging
from typing import Optional, Dict, List, TypedDict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task,
    TaskStatus,
    Project,
    DeploymentState,
    DeploymentStatus,
    SystemMaturity,
    _utcnow,
)
from app.auth import get_current_user, require_admin
from app.broadcast import broadcast_to_project
from app.admin import _get_user_project, _get_user_project_with_role
from app.reaper import _heartbeat_stale_cutoff

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["admin-debug"], dependencies=[Depends(get_current_user)]
)


# ---------------------------------------------------------------------------
# Diagnostics TypedDicts and helpers
# ---------------------------------------------------------------------------


class StuckTaskInfo(TypedDict):
    task_id: str
    title: str
    status: TaskStatus
    stuck_reason: str
    stuck_for_seconds: int
    attempt_count: int
    failure_class: Optional[str]
    unresolvable_deps: List[str]
    remediation_hint: str


class CircuitBreakerInfo(TypedDict):
    state: str
    failure_count: int
    infra_failure_count: int
    open_reason: Optional[str]
    cooldown_seconds: int
    cooldown_remaining_seconds: Optional[int]
    opened_at: Optional[str]


class ProjectSummary(TypedDict):
    project_id: str
    stuck_count: int
    queued_count: int
    in_progress_count: int
    circuit_breaker_state: str
    needs_manual_intervention_count: int


class DirectionRemediationInfo(TypedDict):
    direction_id: str
    direction_text: str
    status: str
    auto_paused: bool
    paused_reason: Optional[str]
    paused_at: Optional[str]
    auto_pause_count: int
    remediation_attempts: int
    last_remediation_result: Optional[str]
    last_remediation_at: Optional[str]
    error_fingerprint: Optional[str]
    remediation_monitor_active: bool
    remediation_monitor_started_at: Optional[str]
    remediation_regression_signal: bool
    remediation_regression_count: int
    needs_manual_intervention: bool


def _build_direction_remediation_state(
    project: Project,
) -> List[DirectionRemediationInfo]:
    """Surface per-direction diagnosis/remediation state for operators.

    Returns entries for directions that have notable state: active remediation
    monitoring, any prior remediation attempt, an active auto-pause, or a
    manual-intervention flag. Directions with no such state are omitted so the
    operator view stays focused on what actually needs attention.
    """
    entries: List[DirectionRemediationInfo] = []
    for d in project.directions or []:
        attempts = int(d.get("remediation_attempts", 0) or 0)
        monitor_started = d.get("remediation_monitor_started_at")
        needs_intervention = bool(d.get("needs_manual_intervention"))
        auto_paused = bool(d.get("auto_paused"))
        if not (attempts or monitor_started or needs_intervention or auto_paused):
            continue
        entries.append(
            {
                "direction_id": d.get("id", ""),
                "direction_text": d.get("text", ""),
                "status": d.get("status", "active"),
                "auto_paused": auto_paused,
                "paused_reason": d.get("paused_reason"),
                "paused_at": d.get("paused_at"),
                "auto_pause_count": int(d.get("auto_pause_count", 0) or 0),
                "remediation_attempts": attempts,
                "last_remediation_result": d.get("last_remediation_result"),
                "last_remediation_at": d.get("last_remediation_at"),
                "error_fingerprint": d.get("error_fingerprint"),
                "remediation_monitor_active": bool(monitor_started),
                "remediation_monitor_started_at": monitor_started,
                "remediation_regression_signal": bool(
                    d.get("remediation_regression_signal")
                ),
                "remediation_regression_count": int(
                    d.get("remediation_regression_count", 0) or 0
                ),
                "needs_manual_intervention": needs_intervention,
            }
        )
    return entries


def _classify_stuck_failure(task: Task) -> Optional[str]:
    """Classify stuck tasks using the canonical classifier.

    Stuck tasks have not actually failed yet, so misleading classifications are
    suppressed. CODE_ERROR (the default fallback) and AGENT_TIMEOUT (which
    classify_failure returns for empty/whitespace messages — meaningful for an
    actually-failed task with a crashed agent, but not for one still in flight
    that just hasn't emitted a status yet) both return None so the UI shows
    'unclassified' instead.
    """
    from app.failure_classification import classify_failure  # noqa: PLC0415

    if not (task.latest_status_message or "").strip():
        return None
    result = classify_failure(task)
    return result if result != "CODE_ERROR" else None


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


async def _build_stuck_tasks(
    tasks: List[Task],
    now: datetime,
    session: AsyncSession,
) -> List[StuckTaskInfo]:
    """Detect stuck tasks and build the diagnostics list."""
    from app.compute import get_zone_health  # noqa: PLC0415

    stuck: List[StuckTaskInfo] = []
    # Align with reaper's pre-claim threshold (6 min) so diagnostics don't flag a
    # PROVISIONING task before the reaper would act on it. IN_PROGRESS heartbeat
    # threshold is computed per-task via reaper._heartbeat_stale_cutoff to match
    # the reaper's scope-aware logic (small=300, medium=420, large=900, plus
    # effort-based floors).
    provision_timeout_secs = 6 * 60
    long_queued_secs = 30 * 60
    stuck_deps_secs = 10 * 60
    evaluating_timeout_secs = 2 * 3600  # 2 hours

    # Pre-fetch all dependency statuses and titles in a single batch query to avoid N+1
    from app.time_utils import _to_naive_utc

    all_dep_uuids: set[uuid.UUID] = set()
    for task in tasks:
        if task.status == TaskStatus.QUEUED:
            # task.created_at is TIMESTAMPTZ since 0061; now is naive _utcnow().
            created_age = (now - _to_naive_utc(task.created_at)).total_seconds()
            if (task.depends_on or []) and created_age > stuck_deps_secs:
                for d in task.depends_on:
                    try:
                        all_dep_uuids.add(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass

    dep_status_map: Dict[str, TaskStatus] = {}
    dep_title_map: Dict[str, str] = {}
    if all_dep_uuids:
        dep_result = await session.execute(
            select(Task.id, Task.status, Task.title).where(Task.id.in_(all_dep_uuids))
        )
        for row in dep_result.all():
            dep_status_map[str(row.id)] = row.status
            dep_title_map[str(row.id)] = row.title or str(row.id)[:8]

    # Count running/queued tasks for capacity hints
    running_count = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
    queued_count = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)

    # Fetch zone health once for provision_timeout hints
    try:
        zone_health = await asyncio.to_thread(get_zone_health)
    except Exception:
        zone_health = {}

    for task in tasks:
        stuck_reason = None
        stuck_for_seconds = 0
        unresolvable_deps: List[str] = []
        remediation_hint = ""

        if (
            task.status == TaskStatus.PROVISIONING
            and task.last_heartbeat is None
            and task.vm_provisioned_at is not None
        ):
            # vm_provisioned_at is the orchestrator-side VM lifecycle clock (set
            # at dispatch and refreshed when GCE confirms running). last_heartbeat
            # is the agent-side clock and is None until claim. Using
            # vm_provisioned_at here matches the reaper's pre-claim threshold.
            age = (now - task.vm_provisioned_at).total_seconds()
            if age > provision_timeout_secs:
                stuck_reason = "provision_timeout"
                stuck_for_seconds = int(age - provision_timeout_secs)
                ctx = task.context or {}
                zone = ctx.get("provision_zone")
                if zone:
                    zh = zone_health.get(zone, {})
                    if zh.get("cooldown_active"):
                        remediation_hint = f"VM timed out in {zone} — zone in cooldown, reaper will retry"
                    else:
                        remediation_hint = f"VM timed out in {zone} — zone available, reaper will retry"
                else:
                    remediation_hint = (
                        "VM provision timed out — reaper will detect and retry"
                    )

        elif task.status == TaskStatus.IN_PROGRESS and task.last_heartbeat is not None:
            hb_cutoff = _heartbeat_stale_cutoff(task, now)
            last_hb = _to_naive_utc(task.last_heartbeat)
            if last_hb and last_hb < hb_cutoff:
                heartbeat_age = (now - last_hb).total_seconds()
                threshold_secs = (now - hb_cutoff).total_seconds()
                stuck_reason = "no_heartbeat"
                stuck_for_seconds = int(heartbeat_age - threshold_secs)
                remediation_hint = f"No heartbeat for {_fmt_duration(heartbeat_age)} — VM may have crashed, reaper will clean up"

        elif task.status == TaskStatus.QUEUED:
            created_age = (now - _to_naive_utc(task.created_at)).total_seconds()
            dep_ids = task.depends_on or []

            if dep_ids and created_age > stuck_deps_secs:
                # Check whether all deps are terminal-failure using pre-fetched map
                valid_dep_uuids = []
                for d in dep_ids:
                    try:
                        valid_dep_uuids.append(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass
                if valid_dep_uuids:
                    known_rows = [
                        (str(uid), dep_status_map[str(uid)])
                        for uid in valid_dep_uuids
                        if str(uid) in dep_status_map
                    ]
                    failed_dep_ids = [
                        dep_id
                        for dep_id, status in known_rows
                        if status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                    ]
                    if failed_dep_ids and len(failed_dep_ids) == len(known_rows):
                        stuck_reason = "stuck_deps"
                        stuck_for_seconds = int(created_age - stuck_deps_secs)
                        unresolvable_deps = failed_dep_ids
                        dep_titles = [
                            dep_title_map.get(d, d[:8]) for d in failed_dep_ids[:2]
                        ]
                        suffix = (
                            f" +{len(failed_dep_ids) - 2} more"
                            if len(failed_dep_ids) > 2
                            else ""
                        )
                        remediation_hint = f"Blocked by {len(failed_dep_ids)} failed task(s): {', '.join(dep_titles)}{suffix}"

            elif not dep_ids and created_age > long_queued_secs:
                stuck_reason = "long_queued"
                stuck_for_seconds = int(created_age - long_queued_secs)
                exhausted = [
                    z for z, d in zone_health.items() if d.get("cooldown_active")
                ]
                if exhausted:
                    remediation_hint = f"All zones exhausted — {running_count} running, {queued_count}Q waiting"
                else:
                    remediation_hint = f"No available slots — {running_count} running, {queued_count}Q waiting"

        elif task.status == TaskStatus.EVALUATING:
            # Skip CI-waiting tasks — they have their own timeout
            ctx = task.context or {}
            if not ctx.get("ci_waiting") and not ctx.get("review_loop", {}).get(
                "ci_waiting"
            ):
                # Find when the task entered EVALUATING from status_log
                evaluating_since = None
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        try:
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        except (KeyError, ValueError, TypeError):
                            pass
                        break
                if evaluating_since is None:
                    evaluating_since = task.started_at or task.created_at
                eval_age = (now - evaluating_since).total_seconds()
                if eval_age > evaluating_timeout_secs:
                    stuck_reason = "evaluation_timeout"
                    stuck_for_seconds = int(eval_age - evaluating_timeout_secs)
                    remediation_hint = f"Evaluating for {_fmt_duration(eval_age)} — may need manual review"

        if stuck_reason:
            stuck.append(
                {
                    "task_id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "stuck_reason": stuck_reason,
                    "stuck_for_seconds": stuck_for_seconds,
                    "attempt_count": task.attempt_count,
                    "failure_class": _classify_stuck_failure(task),
                    "unresolvable_deps": unresolvable_deps,
                    "remediation_hint": remediation_hint,
                }
            )

    return stuck


def _format_circuit_breaker(cb: dict) -> CircuitBreakerInfo:
    opened_at = cb.get("opened_at")
    cooldown_remaining = None
    if opened_at and cb.get("state") == "OPEN":
        elapsed = (_utcnow() - opened_at).total_seconds()
        cooldown_remaining = max(0, int(cb.get("cooldown_seconds", 1800) - elapsed))
    return {
        "state": cb.get("state", "CLOSED"),
        "failure_count": cb.get("failure_count", 0),
        "infra_failure_count": cb.get("infra_failure_count", 0),
        "open_reason": cb.get("open_reason"),
        "cooldown_seconds": cb.get("cooldown_seconds", 1800),
        "cooldown_remaining_seconds": cooldown_remaining,
        "opened_at": opened_at.isoformat() if opened_at else None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/autonomy-debug
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/autonomy-debug")
async def get_autonomy_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a detailed snapshot of why autonomy is or isn't spawning for a project."""
    from app.shared_state import runtime_config as _runtime_config, get_circuit_breaker
    from app.max_plan_pool import _max_plan_pools, _account_available_for_model
    import app.shared_state as _shared_state_mod

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()
    blocking_guards = []

    # Guard 0: no available Max Plan accounts
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(
        _account_available_for_model(a, research_model) for a in user_pool
    ) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        blocking_guards.append(
            {
                "guard": "no_available_accounts",
                "reason": f"No Max Plan accounts available for research model ({research_model})",
                "pool_size": len(user_pool),
            }
        )

    # Guard 1: autonomous_disabled
    if not project.autonomous_enabled:
        blocking_guards.append(
            {
                "guard": "autonomous_disabled",
                "reason": "project.autonomous_enabled is False",
            }
        )

    # Guard 2: circuit_breaker_open
    raw_breaker = get_circuit_breaker(str(project_id))
    breaker_state = raw_breaker["state"]
    cooldown_ends_at = None
    if raw_breaker.get("opened_at"):
        cooldown_end = raw_breaker["opened_at"] + timedelta(
            seconds=raw_breaker["cooldown_seconds"]
        )
        cooldown_ends_at = cooldown_end.isoformat()
        if breaker_state == "OPEN" and now < cooldown_end:
            blocking_guards.append(
                {
                    "guard": "circuit_breaker_open",
                    "reason": "Circuit breaker is OPEN and cooldown has not elapsed",
                    "opened_at": raw_breaker["opened_at"].isoformat(),
                    "cooldown_ends_at": cooldown_ends_at,
                    "failure_count": raw_breaker["failure_count"],
                }
            )

    circuit_breaker_info = {
        "state": breaker_state,
        "failure_count": raw_breaker["failure_count"],
        "success_count": raw_breaker["success_count"],
        "opened_at": raw_breaker["opened_at"].isoformat()
        if raw_breaker.get("opened_at")
        else None,
        "cooldown_seconds": raw_breaker["cooldown_seconds"],
        "cooldown_ends_at": cooldown_ends_at,
    }

    # Mirror `_spawn_research_task_inner`'s two separate concerns:
    #   - busy_direction_ids (direction eligibility): include EVALUATING because
    #     the direction is still "owned" by a research cycle whose children are
    #     merging — re-spawning on it would be wasteful.
    #   - active_research_count (parallel-research cap): exclude EVALUATING
    #     because those tasks aren't consuming VM slots and shouldn't count
    #     against max_parallel (see autonomous_research.py:366, "EVALUATING
    #     parents are just waiting on children — they don't block new research").
    #
    # Conflating them (the previous single-query approach) made this endpoint
    # report `research_task_active` blocks that the spawn path would not
    # actually hit, which was misleading the autonomy-debug UI.
    busy_tasks_result = await session.execute(
        select(Task)
        .where(
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
        .limit(1000)
    )
    busy_direction_ids: list[str] = []
    active_research_count = 0
    for t in busy_tasks_result.scalars().all():
        payload = t.first_payload
        if payload.get("type") != "research":
            continue
        did = payload.get("current_direction_id")
        if did:
            busy_direction_ids.append(did)
        # Only the VM-consuming statuses count against the parallel cap
        if t.status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
        ):
            active_research_count += 1

    # Guard 3: no_eligible_directions — a direction is eligible only if it's
    # active, out of cooldown, AND not already held by another research task.
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    cooldown_seconds_dir = cooldown_minutes * 60
    directions_info = []
    for d in project.directions or []:
        if d.get("status") != "active":
            continue
        last_researched_at = d.get("last_researched_at")
        if last_researched_at:
            last_dt = datetime.fromisoformat(last_researched_at)
            elapsed = (now - last_dt).total_seconds()
            remaining = cooldown_seconds_dir - elapsed
            in_cooldown = remaining > 0
            seconds_until_eligible = max(0, int(remaining)) if in_cooldown else None
        else:
            in_cooldown = False
            seconds_until_eligible = None
        is_busy = d.get("id") in busy_direction_ids
        is_eligible = not in_cooldown and not is_busy
        entry = {
            "id": d.get("id"),
            "text": d.get("text", ""),
            "status": d.get("status", "active"),
            "last_researched_at": last_researched_at,
            "cooldown_minutes": cooldown_minutes,
            "is_eligible": is_eligible,
            "seconds_until_eligible": seconds_until_eligible,
        }
        if is_busy:
            entry["busy_reason"] = (
                "direction has active research task (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING)"
            )
        directions_info.append(entry)

    eligible_directions = [d for d in directions_info if d["is_eligible"]]
    if not eligible_directions:
        min_wait = min(
            (
                d["seconds_until_eligible"]
                for d in directions_info
                if d["seconds_until_eligible"] is not None
            ),
            default=None,
        )
        busy_count = sum(1 for d in directions_info if d.get("busy_reason"))
        if not directions_info:
            reason = "No active directions exist"
        elif busy_count == len(directions_info):
            reason = f"All {len(directions_info)} active directions are held by running research"
        elif busy_count > 0:
            reason = f"All remaining directions in cooldown; {busy_count} held by running research"
        else:
            reason = "All active directions are still in cooldown"
        blocking_guards.append(
            {
                "guard": "no_eligible_directions",
                "reason": reason,
                "time_until_eligible_seconds": min_wait,
                "busy_direction_count": busy_count,
                "total_active_directions": len(directions_info),
            }
        )

    # Guard 4: research_task_active — parallel-research cap (now matches spawn logic)
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 3)
    if active_research_count >= max_parallel:
        blocking_guards.append(
            {
                "guard": "research_task_active",
                "reason": f"Active research ({active_research_count}) >= max_parallel ({max_parallel})",
                "active_research_count": active_research_count,
                "max_parallel": max_parallel,
            }
        )

    # Guard 5: rate_limit_exceeded
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
        .limit(5000)
    )
    recent_tasks = recent_tasks_result.scalars().all()
    tasks_this_hour = sum(
        1 for t in recent_tasks if (t.first_payload).get("type") == "research"
    )
    max_per_hour = _runtime_config.get("max_autonomous_tasks_per_hour", 5)
    if tasks_this_hour >= max_per_hour:
        blocking_guards.append(
            {
                "guard": "rate_limit_exceeded",
                "reason": f"tasks_this_hour ({tasks_this_hour}) >= max_per_hour ({max_per_hour})",
                "tasks_this_hour": tasks_this_hour,
                "max_per_hour": max_per_hour,
            }
        )

    # Guard 6a: pipeline_paused — admin-paused or auto-paused deployments halt dispatch,
    # which means _check_idle_research silently skips spawning.
    deploy_state = await session.get(DeploymentState, 1)
    if deploy_state and deploy_state.status == DeploymentStatus.PAUSED:
        blocking_guards.append(
            {
                "guard": "pipeline_paused",
                "reason": f"Pipeline is PAUSED ({deploy_state.pause_reason or 'no reason recorded'})",
                "pause_reason": deploy_state.pause_reason,
                "paused_at": deploy_state.paused_at.isoformat()
                if deploy_state.paused_at
                else None,
            }
        )

    # Guard 6b: vm_slots_full — _check_idle_research only spawns when VMs are available.
    # This is the most common silent-return path when max_concurrent_vms is small.
    vm_consuming = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
            )
        )
    ).scalar() or 0
    max_concurrent_vms = _runtime_config.get("max_concurrent_vms", 5)
    if vm_consuming >= max_concurrent_vms:
        blocking_guards.append(
            {
                "guard": "vm_slots_full",
                "reason": f"VM slots full ({vm_consuming}/{max_concurrent_vms}) — waiting for a task to finish",
                "vm_consuming": vm_consuming,
                "max_concurrent_vms": max_concurrent_vms,
            }
        )

    # Guard 6: waiting_for_interval — interval gate hasn't elapsed since last_idle_check
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    last_idle_check = _shared_state_mod.last_idle_check
    if last_idle_check:
        interval_elapsed = (
            now - last_idle_check
        ).total_seconds() >= interval_minutes * 60
        next_check_at = (
            last_idle_check + timedelta(minutes=interval_minutes)
        ).isoformat()
    else:
        interval_elapsed = True
        next_check_at = now.isoformat()

    if not interval_elapsed:
        secs_until_check = max(
            0,
            int(
                (
                    last_idle_check + timedelta(minutes=interval_minutes) - now
                ).total_seconds()
            ),
        )
        blocking_guards.append(
            {
                "guard": "waiting_for_interval",
                "reason": f"Research interval ({interval_minutes}m) has not elapsed since last check",
                "next_check_at": next_check_at,
                "seconds_until_check": secs_until_check,
            }
        )

    return {
        "spawn_blocked": len(blocking_guards) > 0,
        "blocking_guards": blocking_guards,
        "circuit_breaker": circuit_breaker_info,
        "rate_limit": {
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        },
        "directions": directions_info,
        "next_check_at": next_check_at,
        "strategic_cycle_history": (project.vision or {}).get(
            "_strategic_cycle_history", []
        ),
    }


@router.get("/projects/{project_id}/strategic-debug")
async def get_strategic_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return at-a-glance status for the project's strategic / architecture cycle.

    Strategic cycles are triggered every N research spawns (strategic_research_frequency),
    and every Mth strategic cycle also runs an architecture review (architecture_review_frequency).
    This endpoint surfaces: current phase, next-trigger countdown, recent cycle outcomes,
    and any blockers (failure threshold, missing purpose) so the user doesn't have to
    dig into the Strategic tab to tell what's happening.
    """
    from app.shared_state import runtime_config as _runtime_config

    project = await _get_user_project(project_id, current_user["user_id"], session)
    vision = project.vision or {}
    now = _utcnow()
    blocking_guards: list[dict] = []

    # Active strategic task (design_doc_update or strategic_review) — mirrors
    # _has_active_strategic_task but without the FOR UPDATE lock (read-only view).
    active_stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(
                [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
            ),
            Task.payloads[0]["type"].astext.in_(
                ["design_doc_update", "strategic_review"]
            ),
        )
        .limit(1)
    )
    active_strategic_task: Optional[Task] = None
    active_phase: Optional[str] = None
    result = (await session.execute(active_stmt)).scalars().first()
    if result is not None:
        active_strategic_task = result
        active_phase = (result.first_payload).get("type")

    # Trigger countdowns
    strategic_frequency = vision.get(
        "_strategic_frequency_override"
    ) or _runtime_config.get("strategic_research_frequency", 6)
    arch_frequency = _runtime_config.get("architecture_review_frequency", 3)
    research_spawn_count = vision.get("_research_spawn_count", 0) or 0
    strategic_cycle_count = vision.get("_strategic_cycle_count", 0) or 0

    if strategic_frequency > 0:
        remainder = research_spawn_count % strategic_frequency
        # When count > 0 and remainder == 0, strategic is due to spawn this cycle
        # (autonomous_research.py:365 triggers on the same condition). Reporting
        # 0 lets the UI show "ready now" instead of jumping back to `frequency`,
        # which looked like the counter overflowing/resetting.
        if research_spawn_count == 0:
            next_strategic_in = strategic_frequency
        else:
            next_strategic_in = (strategic_frequency - remainder) % strategic_frequency
    else:
        next_strategic_in = None

    # Will the *next* strategic cycle also do architecture?
    next_cycle_number = strategic_cycle_count + 1
    next_is_architecture = (
        arch_frequency > 0 and next_cycle_number % arch_frequency == 0
    )

    # Strategic failure state (auto-decays after 48h)
    strategic_failures = vision.get("_strategic_failures", 0) or 0
    failures_at_iso = vision.get("_strategic_failures_at")
    failures_decay_at_iso: Optional[str] = None
    if failures_at_iso:
        try:
            last_failure = datetime.fromisoformat(
                failures_at_iso.replace("Z", "+00:00")
            )
            if last_failure.tzinfo is not None:
                last_failure = last_failure.astimezone(timezone.utc).replace(
                    tzinfo=None
                )
            decay_deadline = last_failure + timedelta(hours=48)
            failures_decay_at_iso = decay_deadline.isoformat()
            if strategic_failures >= 3 and now < decay_deadline:
                blocking_guards.append(
                    {
                        "guard": "strategic_failures_high",
                        "reason": f"{strategic_failures} consecutive strategic failures — auto-recovery in "
                        f"{int((decay_deadline - now).total_seconds() / 3600)}h",
                        "failures": strategic_failures,
                        "decay_at": failures_decay_at_iso,
                    }
                )
        except (ValueError, TypeError):
            pass

    # Vision bootstrapping — without purpose, strategic can only attempt once
    # and won't auto-advance if it fails (strategic_failures gates the retry loop).
    has_purpose = bool(vision.get("purpose"))

    # Recent cycle history (last 3, most recent first)
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    recent_cycles = list(reversed(cycle_history[-3:]))
    last_cycle = recent_cycles[0] if recent_cycles else None

    # Determine phase
    # 2 = Running (task active), 3 = Waiting (blocked), 1 = Checking (near trigger,
    # no blockers), 0 = Idle (not near trigger, no activity).
    phase: int
    if active_strategic_task is not None:
        phase = 2
    elif blocking_guards:
        phase = 3
    elif next_strategic_in is not None and next_strategic_in <= 1:
        # One or zero spawns away from triggering a cycle — effectively "checking"
        phase = 1
    else:
        phase = 0

    # Architecture (design doc) summary: last update, revision, system count,
    # and how many strategic cycles until the next architecture review fires.
    design_doc = project.design_doc or {}
    dd_meta = design_doc.get("_meta") or {}
    system_keys = [
        k for k in design_doc.keys() if isinstance(k, str) and k.startswith("systems/")
    ]
    capability_keys = [
        k
        for k in design_doc.keys()
        if isinstance(k, str) and k.startswith("capabilities/")
    ]
    if arch_frequency > 0:
        arch_remainder = strategic_cycle_count % arch_frequency
        if strategic_cycle_count == 0:
            next_arch_in_cycles = arch_frequency
        else:
            next_arch_in_cycles = (arch_frequency - arch_remainder) % arch_frequency
    else:
        next_arch_in_cycles = None
    architecture_info = {
        "revision": dd_meta.get("revision", 0),
        "last_updated": dd_meta.get("last_updated"),
        "updated_by_task": dd_meta.get("updated_by_task"),
        "system_count": len(system_keys),
        "capability_count": len(capability_keys),
        "next_arch_in_cycles": next_arch_in_cycles,
    }

    return {
        "phase": phase,
        "blocking_guards": blocking_guards,
        "has_purpose": has_purpose,
        "research_spawn_count": research_spawn_count,
        "strategic_cycle_count": strategic_cycle_count,
        "strategic_frequency": strategic_frequency,
        "architecture_review_frequency": arch_frequency,
        "next_strategic_in": next_strategic_in,
        "next_is_architecture": next_is_architecture,
        "strategic_failures": strategic_failures,
        "strategic_failures_at": failures_at_iso,
        "strategic_failures_decay_at": failures_decay_at_iso,
        "architecture": architecture_info,
        "active_task": {
            "id": str(active_strategic_task.id),
            "phase": active_phase,
            "status": active_strategic_task.status,
            "started_at": active_strategic_task.started_at.isoformat()
            if active_strategic_task.started_at
            else None,
        }
        if active_strategic_task
        else None,
        "last_cycle": last_cycle,
        "recent_cycles": recent_cycles,
    }


@router.post("/projects/{project_id}/circuit-breaker/reset")
async def reset_circuit_breaker(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually reset a project's circuit breaker to CLOSED state."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    import app.shared_state as _shared_state
    from app.orchestrator import _persist_circuit_breakers

    key = str(project_id)
    breaker = _shared_state.circuit_breakers.get(key)
    if not breaker:
        raise HTTPException(
            status_code=404, detail="No circuit breaker state found for this project"
        )

    base_cooldown = breaker.get("base_cooldown", 1800)
    breaker["state"] = "CLOSED"
    breaker["failure_count"] = 0
    breaker["cooldown_seconds"] = base_cooldown
    breaker["opened_at"] = None
    breaker["half_open_task_id"] = None

    await _persist_circuit_breakers()

    # Also clear strategic cycle state so the cycle can recover
    project = await session.get(Project, project_id)
    if project and project.vision:
        vision_keys_to_clear = {"_strategic_failures", "_pending_strategic_review"}
        if vision_keys_to_clear & project.vision.keys():
            project.vision = {**project.vision}
            for key in vision_keys_to_clear:
                project.vision.pop(key, None)
            flag_modified(project, "vision")
            session.add(project)
            await session.commit()
            logger.info(
                f"[ADMIN] Cleared strategic cycle state (_strategic_failures, _pending_strategic_review) for project {project_id}"
            )

    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    logger.info(
        f"[ADMIN] Circuit breaker manually reset for project {project_id} by {user_email}"
    )

    await broadcast_to_project(
        key, "autonomy_updated", {"project_id": key, "circuit_breaker_reset": True}
    )

    return {
        "project_id": key,
        "state": breaker["state"],
        "failure_count": breaker["failure_count"],
        "cooldown_seconds": breaker["cooldown_seconds"],
        "opened_at": None,
        "half_open_task_id": None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/diagnostics
# ---------------------------------------------------------------------------


_MAX_ACTIVE_TASKS_DEFAULT = 500
_MAX_ACTIVE_TASKS_CEILING = 5000


@router.get("/projects/{project_id}/diagnostics")
async def get_project_diagnostics(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    max_active_tasks: int = Query(
        _MAX_ACTIVE_TASKS_DEFAULT,
        ge=1,
        le=_MAX_ACTIVE_TASKS_CEILING,
    ),
):
    """Return a structured health snapshot for a single project's pipeline.

    ``max_active_tasks`` caps the number of active tasks fetched (default 500,
    bounded to [1, 5000]). Tasks are ordered oldest-first so a truncated subset
    still surfaces the genuinely stuck tasks. When the result is truncated the
    response includes ``active_tasks_truncated: true``.
    """
    from app.shared_state import get_circuit_breaker
    from app.compute import get_zone_health_summary

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()

    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        # Oldest-first so a truncated subset keeps the genuinely stuck tasks.
        .order_by(Task.created_at.asc())
        .limit(max_active_tasks + 1)
    )
    active_tasks_raw = active_result.scalars().all()
    active_tasks_truncated = len(active_tasks_raw) > max_active_tasks
    active_tasks = active_tasks_raw[:max_active_tasks]

    stuck_tasks = await _build_stuck_tasks(active_tasks, now, session)

    queued_count = sum(1 for t in active_tasks if t.status == TaskStatus.QUEUED)
    in_progress_count = sum(
        1 for t in active_tasks if t.status == TaskStatus.IN_PROGRESS
    )

    cb_raw = get_circuit_breaker(str(project_id))
    circuit_breaker = _format_circuit_breaker(cb_raw)

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    direction_remediation = _build_direction_remediation_state(project)

    return {
        "project_id": str(project_id),
        "stuck_tasks": stuck_tasks,
        "circuit_breaker": circuit_breaker,
        "queued_count": queued_count,
        "in_progress_count": in_progress_count,
        "zone_health": zone_health,
        "direction_remediation": direction_remediation,
        "needs_manual_intervention_count": sum(
            1 for e in direction_remediation if e["needs_manual_intervention"]
        ),
        "active_tasks_truncated": active_tasks_truncated,
        "active_tasks_cap": max_active_tasks,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/diagnostics  (aggregated across all projects)
# ---------------------------------------------------------------------------

DIAGNOSTICS_TOP_N = 20


@router.get("/diagnostics")
async def get_global_diagnostics(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate pipeline diagnostics across all projects.

    Uses limit/offset pagination over active tasks (default limit=50, max=500).
    Returns stuck_tasks and direction_remediation as top-N summaries with total counts.
    """
    from app.shared_state import get_circuit_breaker, circuit_breakers
    from app.compute import get_zone_health_summary

    now = _utcnow()

    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]

    # Count total active tasks for pagination warnings without loading full rows.
    count_result = await session.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    total_active_count = count_result.scalar() or 0

    # Global per-status counts over the FULL active set (independent of the
    # paginated window below). queued_count / in_progress_count are consumed as
    # global totals by scripts/ops.py and scripts/audit_tasks_prs.py, so they must
    # not be derived from the page (which is oldest-first and would drop fresh
    # QUEUED tasks). Cheap GROUP BY, no row payloads loaded.
    status_count_result = await session.execute(
        select(Task.status, func.count())
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        .group_by(Task.status)
    )
    status_counts: Dict[TaskStatus, int] = {
        row[0]: row[1] for row in status_count_result.all()
    }
    total_queued = status_counts.get(TaskStatus.QUEUED, 0)
    total_in_progress = status_counts.get(TaskStatus.IN_PROGRESS, 0)

    active_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.title,
                Task.created_at,
                Task.updated_at,
                Task.vm_instance_id,
                Task.last_heartbeat,
                Task.vm_provisioned_at,
                Task.started_at,
                Task.attempt_count,
                Task.latest_status_message,
                Task.context,
                Task.status_log,
                Task.depends_on,
                # payloads is read by classify_failure → _classify_stuck_failure;
                # omitting it here triggers MissingGreenlet on lazy-load.
                Task.payloads,
            )
        )
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        .order_by(Task.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    active_tasks = list(active_result.scalars().all())

    # Build warnings when the scan window doesn't cover all active tasks.
    warnings: List[str] = []
    tasks_beyond_window = total_active_count - (offset + len(active_tasks))
    if tasks_beyond_window > 0:
        warnings.append(
            f"Showing {len(active_tasks)} of {total_active_count} active tasks "
            f"(offset={offset}, limit={limit}); {tasks_beyond_window} tasks not scanned — "
            "use offset to paginate."
        )
    if limit >= 400:
        warnings.append(
            f"limit={limit} is near the maximum (500); response may be large."
        )

    # Group tasks by project
    by_project: Dict[str, List[Task]] = {}
    for task in active_tasks:
        pid = str(task.project_id)
        by_project.setdefault(pid, []).append(task)

    # Fetch project remediation state once so the aggregate can surface
    # needs_manual_intervention directions even for projects with no active tasks.
    # Skip projects with no directions — they can never contribute remediation entries.
    projects_result = await session.execute(
        select(Project)
        .where(func.coalesce(func.jsonb_array_length(Project.directions), 0) > 0)
        .options(load_only(Project.id, Project.directions))
        .limit(500)
    )
    remediation_by_project: Dict[str, List[DirectionRemediationInfo]] = {}
    for proj in projects_result.scalars().all():
        entries = _build_direction_remediation_state(proj)
        if entries:
            remediation_by_project[str(proj.id)] = entries

    all_stuck: List[StuckTaskInfo] = []
    project_summaries: List[ProjectSummary] = []
    all_remediation: List[DirectionRemediationInfo] = []

    project_ids = set(by_project.keys()) | set(remediation_by_project.keys())
    for pid in project_ids:
        tasks = by_project.get(pid, [])
        stuck = await _build_stuck_tasks(tasks, now, session) if tasks else []
        # Per-project queued/in_progress are window-scoped (reflect only the
        # tasks on this page); the global queued_count / in_progress_count in the
        # response come from the GROUP BY above.
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        in_prog = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
        all_stuck.extend(stuck)
        remediation = remediation_by_project.get(pid, [])
        all_remediation.extend(remediation)
        manual_count = sum(1 for e in remediation if e["needs_manual_intervention"])

        cb_raw = get_circuit_breaker(pid)
        project_summaries.append(
            {
                "project_id": pid,
                "stuck_count": len(stuck),
                "queued_count": queued,
                "in_progress_count": in_prog,
                "circuit_breaker_state": cb_raw.get("state", "CLOSED"),
                "needs_manual_intervention_count": manual_count,
            }
        )

    # All known circuit breakers (includes projects with no active tasks)
    all_circuit_breakers = {
        pid: _format_circuit_breaker(cb) for pid, cb in circuit_breakers.items()
    }

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    # Return top-N for stuck_tasks and direction_remediation to bound response size.
    # Callers that need the full list should page through all offsets and aggregate.
    stuck_by_urgency = sorted(
        all_stuck, key=lambda x: x["stuck_for_seconds"], reverse=True
    )
    remediation_by_priority = sorted(
        all_remediation,
        key=lambda x: (not x["needs_manual_intervention"], x.get("direction_text", "")),
    )

    return {
        "stuck_tasks": stuck_by_urgency[:DIAGNOSTICS_TOP_N],
        # total_stuck_count reflects the scanned window only. Stuck detection is a
        # per-task derived property (heartbeat/timing/JSONB) with no cheap GROUP BY
        # equivalent, so computing it globally would require the full active-set
        # scan this endpoint is designed to bound. Stuck tasks are old by
        # definition and the window is ordered created_at ASC (oldest first), so
        # the default window captures them; widen via limit/offset for an exhaustive count.
        "total_stuck_count": len(all_stuck),
        "queued_count": total_queued,
        "in_progress_count": total_in_progress,
        "total_active_tasks_scanned": len(active_tasks),
        "projects": project_summaries,
        "circuit_breakers": all_circuit_breakers,
        "zone_health": zone_health,
        "direction_remediation": remediation_by_priority[:DIAGNOSTICS_TOP_N],
        "total_direction_remediation_count": len(all_remediation),
        "needs_manual_intervention_count": sum(
            1 for e in all_remediation if e["needs_manual_intervention"]
        ),
        # NOTE: semantics changed with pagination. truncated now means "this page
        # does not cover all active tasks" (tasks_beyond_window > 0), not the prior
        # ">5000 active tasks exist" sentinel. The global endpoint has no frontend
        # consumer reading this field; see warnings for the human-readable detail.
        "truncated": tasks_beyond_window > 0,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/system-maturity
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/system-maturity")
async def get_project_system_maturity(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return per-system maturity rows for a project, ordered by score ascending."""
    await _get_user_project(project_id, current_user["user_id"], session)
    result = await session.execute(
        select(SystemMaturity)
        .where(SystemMaturity.project_id == project_id)
        .order_by(SystemMaturity.score.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "system_id": r.system_id,
            "score": r.score,
            "recommended_mode": r.recommended_mode,
            "top_issues": r.top_issues or [],
            "last_audited_at": r.last_audited_at.isoformat(),
        }
        for r in rows
    ]


# GET /api/admin/tasks/{task_id}/fix-loop-history
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/fix-loop-history")
async def get_task_fix_loop_history(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the fix-loop telemetry persisted on a task.

    Per-loop attempt arrays + a small summary block (attempts count and
    last_exit_reason for lint/test/check). Auth follows the surrounding
    project drilldown pattern: load the task, then verify the caller has
    access to its project.
    """
    from app.fix_loop_telemetry import summarize_fix_loop_history  # noqa: PLC0415

    task = (
        (await session.execute(select(Task).where(Task.id == task_id)))
        .scalars()
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await _get_user_project(task.project_id, current_user["user_id"], session)

    history = task.fix_loop_history or {}
    return {
        "task_id": str(task.id),
        "fix_loop_history": history,
        "summary": summarize_fix_loop_history(history),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/recent-findings
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/recent-findings")
async def get_recent_findings(
    project_id: uuid.UUID,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return recent research findings with actionability grades and grade distribution."""
    await _get_user_project(project_id, current_user["user_id"], session)
    cutoff = _utcnow() - timedelta(days=7)
    result = await session.execute(
        select(Task)
        .options(load_only(Task.id, Task.plan_output, Task.created_at, Task.context))
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
            Task.plan_output.isnot(None),
        )
        .order_by(Task.created_at.desc())
        .limit(100)
    )
    tasks = result.scalars().all()

    findings_out: list[dict] = []
    grade_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for t in tasks:
        raw = t.plan_output
        if not isinstance(raw, list):
            continue
        for f in raw:
            if not isinstance(f, dict) or not f.get("title"):
                continue
            grade = f.get("actionability_grade", "MEDIUM")
            if grade not in grade_counts:
                grade = "MEDIUM"
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
            findings_out.append(
                {
                    "task_id": str(t.id),
                    "title": f.get("title", ""),
                    "type": f.get("type", ""),
                    "scope": f.get("scope", ""),
                    "priority": f.get("priority", "medium"),
                    "actionability_grade": grade,
                    "direction_id": f.get("direction_id"),
                    "researched_at": t.created_at.isoformat() if t.created_at else None,
                }
            )
            if len(findings_out) >= limit:
                break
        if len(findings_out) >= limit:
            break

    total = sum(grade_counts.values())
    grade_pct = {
        g: round(c / total * 100, 1) if total > 0 else 0.0
        for g, c in grade_counts.items()
    }
    return {
        "findings": findings_out,
        "grade_distribution": grade_counts,
        "grade_distribution_pct": grade_pct,
        "total": total,
    }
