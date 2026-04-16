"""Orchestrator worker – background loop that provisions tasks and monitors health.

Extracted from app.main to reduce module size. The worker polls for QUEUED tasks,
checks dependencies, handles file conflicts, provisions VMs, and monitors
EVALUATING tasks.
"""

import asyncio
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.compute import (
    INFRA_PROVISION_PATTERNS,
    any_zone_available,
    get_provision_error,
    get_vm_zone,
    get_zone_health_summary,
    prepare_and_store_agent_secret,
    provision_agent_vm,
    record_zone_failure,
    soonest_zone_recovery,
)
from app.database import AsyncSessionLocal
from app.models import Project, Task, TaskStatus
from app.orchestrator_filters import filter_file_conflicts, filter_rollup_conflicts
from app.storage import upload_json_payload
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# ---------------------------------------------------------------------------
# Module-level globals managed by orchestrator_worker
# ---------------------------------------------------------------------------

_last_pool_refresh: datetime = _utcnow()
_last_zone_health_persist: datetime = _utcnow()
_last_orphan_check: datetime = _utcnow()
_last_evaluating_timeout_check: datetime = _utcnow()
_last_deadlock_check: float = 0.0
_last_zone_exhaustion_log: Optional[datetime] = None
_last_concurrency_broadcast: float = 0.0

# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: Optional[datetime] = None
_orchestrator_cycle_started: Optional[datetime] = None

# Consecutive error counters for exponential backoff
_orchestrator_consecutive_errors: int = 0
_orchestrator_consecutive_monitoring_errors: int = 0

# Configurable throttle sleep when high failure rate detected
ORCHESTRATOR_THROTTLE_SECONDS = int(os.getenv('ORCHESTRATOR_THROTTLE_SECONDS', '30'))

# Track in-flight provision coroutines so stale recovery can cancel them.
# Without cancellation, zombie provisions hold semaphore slots for up to 360s,
# starving new provisions and creating a cascading failure where no VM can
# claim its task (the reaper assigns a new vm_instance_id before the VM boots).
_provision_coroutines: Dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Cycle detection for deadlock resolution
# ---------------------------------------------------------------------------

def _find_cycle_members(adj: Dict[str, list]) -> set:
    """Return node IDs participating in cycles (DFS white/gray/black coloring)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)
    members: set = set()
    def dfs(n, stack):
        color[n] = GRAY; stack.append(n)
        for d in adj.get(n, []):
            if color.get(d) == GRAY:
                members.update(stack[stack.index(d):])
            elif color.get(d) == WHITE:
                dfs(d, stack)
        stack.pop(); color[n] = BLACK
    for n in list(adj):
        if color[n] == WHITE:
            dfs(n, [])
    return members


def _classify_stuck_tasks(stuck, dep_map, dep_status, stuck_ids) -> list:
    """Return [(task_id, reason)] for tasks whose depends_on should be cleared.

    reason is 'cycle' (true dependency cycle) or 'dead_deps' (all deps are
    FAILED/CANCELLED/deleted with none stuck or active).
    """
    adj = {str(t.id): [str(d) for d in dep_map[t.id] if d in stuck_ids] for t in stuck}
    cycle_m = _find_cycle_members(adj)
    out = []
    for t in stuck:
        deps = dep_map[t.id]
        if not deps:
            continue
        if str(t.id) in cycle_m:
            out.append((t.id, "cycle"))
            continue
        # Dead-dep: no dep is stuck (resolvable) or active
        if not any(
            d in stuck_ids or dep_status.get(d) not in (None, TaskStatus.FAILED, TaskStatus.CANCELLED)
            for d in deps
        ):
            out.append((t.id, "dead_deps"))
    return out


def _health_score_task(task: Task) -> int:
    """Return a dispatch-priority boost for health-aware reordering during degradation.

    Small scope and low risk are preferred because they are less likely to consume
    a scarce VM slot and fail.  Higher score = dispatched first.

    Scoring:
        +2  scope == "small"
        +1  risk  == "low"
    """
    ctx = task.context or {}
    score = 0
    if ctx.get("scope") == "small":
        score += 2
    if ctx.get("risk") == "low":
        score += 1
    return score


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    from app.deploy_health import _get_deploy_state_for_update  # noqa: PLC0415
    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            # Prune: don't persist CLOSED breakers with no failures (default state)
            if (
                v.get("state") == "CLOSED"
                and v.get("failure_count", 0) == 0
                and v.get("infra_failure_count", 0) == 0
            ):
                continue
            serializable[k] = {
                **v,
                "opened_at": v["opened_at"].isoformat() if v.get("opened_at") else None,
            }
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.circuit_breaker_states = serializable
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[CIRCUIT BREAKER] Failed to persist state: {e}", exc_info=True)


async def _persist_zone_health():
    """Snapshot zone health to DB so cooldowns survive restarts."""
    from app.deploy_health import _get_deploy_state_for_update, _pause_failure_streak, _health_recovery_streak  # noqa: PLC0415
    try:
        from app.compute import _zone_health, _zone_lock
        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {
                    k: v.isoformat() if isinstance(v, datetime) else v
                    for k, v in data.items()
                }
        snapshot["_pause_failure_streak"] = _pause_failure_streak
        snapshot["_health_recovery_streak"] = _health_recovery_streak
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


async def _check_candidate_task_deps(
    task: Task,
    dep_tasks: list,
    dep_uuids: list,
    session: "AsyncSession",
    verify_pr_merged,
) -> tuple[str, dict]:
    """Check whether a candidate task's dependencies are satisfied.

    Handles requeue-cancelled dependencies: if a dep task is CANCELLED but has
    ``_replaced_by_task_id`` in its context (set by ``_requeue_parent_task``),
    the replacement clone's status is used instead of treating the dependency as
    permanently failed.

    Returns:
        (result, deps_remapped) where:
        - result is ``'cascade_cancel'``, ``'eligible'``, or ``'waiting'``
        - deps_remapped maps old cancelled dep IDs to replacement IDs (for
          persisting remap so future cycles skip the lookup)
    """
    all_met = True
    any_failed = len(dep_tasks) < len(dep_uuids)  # Missing deps treated as failed
    deps_remapped: dict[str, str] = {}

    for dt in dep_tasks:
        effective_dt = dt
        if dt.status == TaskStatus.CANCELLED:
            replacement_id_str = (dt.context or {}).get("_replaced_by_task_id")
            if replacement_id_str:
                try:
                    replacement = await session.get(Task, uuid.UUID(replacement_id_str))
                except (ValueError, AttributeError):
                    replacement = None
                if replacement:
                    deps_remapped[str(dt.id)] = str(replacement.id)
                    effective_dt = replacement
                else:
                    # Replacement task is gone — treat as permanently cancelled
                    any_failed = True
                    break
            else:
                # Permanently cancelled (no replacement recorded)
                any_failed = True
                break

        if effective_dt.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]:
            any_failed = True
            break
        if effective_dt.status != TaskStatus.COMPLETED:
            all_met = False
            continue
        # For create tasks, also require PR to be merged
        dt_type = (effective_dt.first_payload).get("type", "create")
        if dt_type in ("create", "") and not await verify_pr_merged(effective_dt, session):
            all_met = False

    if any_failed:
        return "cascade_cancel", deps_remapped
    elif all_met:
        return "eligible", deps_remapped
    else:
        return "waiting", deps_remapped


async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    # Late imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        load_max_plan_pool,
        release_max_plan_account,
        select_max_plan_account,
        recheck_disabled_accounts,
        _extract_task_model,
        _account_available_for_model,
        _account_by_id,
        _max_plan_pools,
        _max_plan_pool_lock,
    )
    from app.main import (  # noqa: PLC0415
        _get_actual_gcp_vm_count,
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
        _create_orchestrator_error_alerts,
    )
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.deploy_health import (  # noqa: PLC0415
        _check_monitoring_window,
        _check_external_health,
        _check_pause_mode,
    )
    from app.autonomous_research import _check_idle_research  # noqa: PLC0415
    from app.rollup_management import _verify_pr_merged  # noqa: PLC0415
    from app.review_pipeline import _spawn_pipeline_task  # noqa: PLC0415
    from app.broadcast import broadcast_to_project, manager as ws_manager  # noqa: PLC0415
    from app.services import _recover_orphaned_tasks, _check_evaluating_timeouts  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.github import (
        create_pr_comment,
        get_check_runs,
        get_pull_request,
        merge_pull_request,
    )
    global _last_pool_refresh, _orchestrator_last_cycle, _orchestrator_cycle_started
    global _orchestrator_consecutive_errors, _orchestrator_consecutive_monitoring_errors
    global _last_orphan_check, _last_evaluating_timeout_check
    while True:
        queued_tasks = []
        session = None  # defined before try so except handler can reference it
        try:
            _orchestrator_cycle_started = _utcnow()
            cycle_start = time.monotonic()
            tasks_recovered = 0

            # Periodic pool refresh — pick up admin changes without restart
            if (_utcnow() - _last_pool_refresh).total_seconds() >= 300:
                await load_max_plan_pool()
                _last_pool_refresh = _utcnow()

            # Periodic zone health persist — prevents retry storms after restart
            global _last_zone_health_persist
            if (_utcnow() - _last_zone_health_persist).total_seconds() >= 60:
                await _persist_zone_health()
                _last_zone_health_persist = _utcnow()

            async with AsyncSessionLocal() as session:
                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                active_tasks_result = (await session.execute(
                    select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                )).scalars().all()

                # Recover stale PROVISIONING tasks (orchestrator crashed mid-dispatch)
                ten_min_ago = _utcnow() - timedelta(minutes=10)
                stale_vm_ids = []
                cancelled_provision_ids = []
                for t in active_tasks_result:
                    if t.status != TaskStatus.PROVISIONING:
                        continue
                    # Skip if provision coroutine is still running — it hasn't
                    # failed yet, just let it finish. Only requeue truly abandoned tasks.
                    active_coro = _provision_coroutines.get(str(t.id))
                    if active_coro and not active_coro.done():
                        continue
                    if ((t.last_heartbeat is None or t.last_heartbeat < ten_min_ago)
                        and t.started_at and t.started_at < ten_min_ago):
                        # Re-read with lock to prevent race with reaper on provision_failures
                        fresh_t = (await session.execute(
                            select(Task).where(Task.id == t.id).with_for_update(skip_locked=True)
                        )).scalars().first()
                        if not fresh_t or fresh_t.status != TaskStatus.PROVISIONING:
                            continue  # Reaper already handled it
                        # Check provision failure limit before requeueing
                        provision_failures = (fresh_t.context or {}).get("provision_failures", 0) + 1
                        if provision_failures >= _runtime_config.get("max_provision_retries", 3):
                            fresh_t.status = TaskStatus.FAILED
                            fresh_t.latest_status_message = f"Failed after {provision_failures} provision attempts"
                            logger.error(f"[ORCHESTRATOR] Task {t.id} exceeded provision retry limit ({provision_failures}) — marking FAILED")
                            acct_id = (fresh_t.context or {}).get("max_plan_account_id")
                            if acct_id:
                                await release_max_plan_account(acct_id)
                            session.add(fresh_t)
                            continue
                        stale_vm_ids.append(fresh_t.vm_instance_id)
                        # Release Max Plan account slot before requeueing
                        acct_id = (fresh_t.context or {}).get("max_plan_account_id")
                        if acct_id:
                            await release_max_plan_account(acct_id)
                        # Cancel any lingering coroutine reference
                        zombie = _provision_coroutines.pop(str(t.id), None)
                        if zombie and not zombie.done():
                            zombie.cancel()
                            cancelled_provision_ids.append(str(t.id))
                        fresh_t.status = TaskStatus.QUEUED
                        fresh_t.started_at = None
                        fresh_t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        fresh_t.context = {**(fresh_t.context or {}), "provision_failures": provision_failures}
                        session.add(fresh_t)
                        tasks_recovered += 1
                        if fresh_t.last_heartbeat is None:
                            logger.warning(f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED (no heartbeat, started_at={fresh_t.started_at})")
                        else:
                            logger.warning(f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED (stale heartbeat={fresh_t.last_heartbeat}, started_at={fresh_t.started_at})")
                if cancelled_provision_ids:
                    logger.warning(f"[ORCHESTRATOR] Cancelled {len(cancelled_provision_ids)} zombie provision coroutine(s): {cancelled_provision_ids}")
                if session.dirty:
                    await session.commit()
                    # Delete old VMs from stale provisions (after commit)
                    for old_vm in stale_vm_ids:
                        if old_vm:
                            await _ensure_vm_deleted(old_vm, reason="stale_provisioning")
                    # Re-fetch after recovery
                    active_tasks_result = (await session.execute(
                        select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                    )).scalars().all()

                # Enforce concurrency limit
                active_count = len(active_tasks_result)
                # Cross-check with actual GCP VM count (cached, refreshed every 2 min)
                actual_vm_count = await _get_actual_gcp_vm_count()
                effective_count = max(active_count, actual_vm_count)
                if actual_vm_count > active_count + 2:
                    logger.warning(
                        f"[ORCHESTRATOR] GCP VM count ({actual_vm_count}) > DB active count ({active_count}) "
                        f"— possible orphans, using GCP count for slot calculation"
                    )
                slots_available = max(0, _runtime_config["max_concurrent_vms"] - effective_count)
                _shared_state.tasks_active_gauge = active_count
                if slots_available == 0:
                    # Update queued gauge even when we skip dispatching
                    _shared_state.tasks_queued_gauge = (await session.execute(
                        select(_func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
                    )).scalar() or 0
                    global _last_concurrency_broadcast
                    if (time.monotonic() - _last_concurrency_broadcast) > 60:
                        _last_concurrency_broadcast = time.monotonic()
                        try:
                            await ws_manager.broadcast_to_all("pipeline_throttle", {
                                "reason": "concurrency_limit",
                                "detail": f"All {_runtime_config['max_concurrent_vms']} VM slots in use",
                                "recovery_seconds": None,
                            })
                        except Exception:
                            logger.exception("[ORCHESTRATOR] Failed to broadcast concurrency_limit throttle")
                    logger.info('orchestrator_slots_full', extra={
                        'slots_in_use': effective_count,
                        'total_slots': _runtime_config['max_concurrent_vms'],
                    })
                    logger.info('orchestrator_cycle_complete', extra={
                        'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                        'tasks_dispatched': 0,
                        'tasks_recovered': tasks_recovered,
                        'slots_in_use': effective_count,
                        'slots_available': 0,
                        'queue_depth': _shared_state.tasks_queued_gauge,
                    })
                    await asyncio.sleep(5)
                    continue

                # Failure-rate dispatch gate — throttle when recent tasks are mostly failing
                # Filter to last 15 min so the gate auto-clears when failures age out
                _fr_cutoff = (_utcnow() - timedelta(minutes=15)).isoformat()
                recent_results = [r for r in _shared_state.deploy_monitoring["task_results"] if r[0] >= _fr_cutoff]
                if len(recent_results) >= 5:
                    recent_window = recent_results[-10:]
                    recent_failures = sum(1 for r in recent_window if r[1] == "failure")
                    recent_rate = recent_failures / len(recent_window)
                    if recent_rate >= 0.8:
                        logger.warning(f"[ORCHESTRATOR] High failure rate ({recent_failures}/{len(recent_window)}) — throttling dispatch for {ORCHESTRATOR_THROTTLE_SECONDS}s")
                        try:
                            await ws_manager.broadcast_to_all("pipeline_throttle", {
                                "reason": "high_failure_rate",
                                "detail": f"{recent_failures}/{len(recent_window)} recent tasks failed",
                                "recovery_seconds": ORCHESTRATOR_THROTTLE_SECONDS,
                            })
                        except Exception:
                            logger.exception("[ORCHESTRATOR] Failed to broadcast high_failure_rate throttle")
                        # Run account re-enablement before sleeping so rate-limited accounts
                        # can recover even while dispatch is throttled
                        await recheck_disabled_accounts()
                        await asyncio.sleep(ORCHESTRATOR_THROTTLE_SECONDS)
                        continue

                # Reconcile Max Plan active_tasks counters from DB each cycle
                active_per_account: Dict[str, int] = {}
                for t in active_tasks_result:
                    acct_id = (t.context or {}).get("max_plan_account_id")
                    if acct_id:
                        active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                async with _max_plan_pool_lock:
                    for acct in _account_by_id.values():
                        # Reconcile from DB — the pre-increment in select_max_plan_account() is always
                        # committed as PROVISIONING before the next cycle's reconciliation runs.
                        db_count = active_per_account.get(acct["id"], 0)
                        acct["active_tasks"] = db_count

                # Step 1: Find candidate tasks WITHOUT lock (read-only scan for dep filtering)
                candidate_stmt = select(Task).where(
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval.is_(False)
                ).order_by(Task.priority.desc().nullslast(), Task.created_at.asc()).limit(max(slots_available * 2, 50))  # Fetch extra; NULL priority = lowest
                candidate_tasks = (await session.execute(candidate_stmt)).scalars().all()

                # Step 2: Filter out tasks with unmet dependencies (no lock needed)
                eligible_ids = []
                for task in candidate_tasks:
                    # Skip tasks held back due to retry_after delay
                    retry_after_str = (task.context or {}).get("retry_after")
                    if retry_after_str:
                        try:
                            retry_after_dt = datetime.fromisoformat(retry_after_str)
                            if retry_after_dt > _utcnow():
                                logger.info(f"[ORCHESTRATOR] Task {task.id} held back — retry_after={retry_after_str}")
                                continue
                        except (ValueError, TypeError):
                            pass  # Malformed retry_after — let it through
                    # Clear infra-wait flag when task becomes eligible again
                    if (task.context or {}).get("_infra_wait"):
                        ctx = dict(task.context or {})
                        ctx.pop("_infra_wait", None)
                        ctx.pop("retry_after", None)
                        task.context = ctx
                        session.add(task)

                    deps = task.depends_on or []
                    if not deps:
                        eligible_ids.append(task.id)
                        continue

                    # Check all dependencies are COMPLETED with PR merged
                    try:
                        dep_uuids = [uuid.UUID(d) for d in deps]
                    except (ValueError, AttributeError) as e:
                        logger.error(f"[ORCHESTRATOR] Task {task.id} has invalid dependency UUID: {e}", exc_info=True)
                        continue  # Don't dispatch — deps can't be validated
                    dep_result = await session.execute(select(Task).where(Task.id.in_(dep_uuids)))
                    dep_tasks = dep_result.scalars().all()

                    # Detect deleted dependencies — treat as failed to prevent premature dispatch
                    if len(dep_tasks) < len(dep_uuids):
                        found_ids = {str(dt.id) for dt in dep_tasks}
                        missing = [d for d in deps if d not in found_ids]
                        logger.error(f"[ORCHESTRATOR] Task {task.id} has {len(missing)} deleted dependencies: {missing}")

                    dep_result_str, deps_remapped = await _check_candidate_task_deps(
                        task, dep_tasks, dep_uuids, session, _verify_pr_merged
                    )
                    all_met = dep_result_str == "eligible"
                    any_failed = dep_result_str == "cascade_cancel"

                    # Persist remap of requeue-cancelled dep IDs → replacement clone IDs
                    # so future orchestrator cycles skip the _replaced_by_task_id lookup.
                    if deps_remapped and not any_failed:
                        new_deps = [deps_remapped.get(d, d) for d in (task.depends_on or [])]
                        if new_deps != list(task.depends_on or []):
                            async with AsyncSessionLocal() as remap_session:
                                remap_task = await remap_session.get(Task, task.id)
                                if remap_task:
                                    remap_task.depends_on = new_deps
                                    remap_session.add(remap_task)
                                    await remap_session.commit()
                            task.depends_on = new_deps
                            logger.info(
                                f"[ORCHESTRATOR] Remapped {len(deps_remapped)} requeue-cancelled "
                                f"dep(s) for task {task.id}"
                            )

                    if any_failed:
                        # Use separate session to avoid polluting the main dispatch session
                        async with AsyncSessionLocal() as cancel_session:
                            cancel_task = await cancel_session.get(Task, task.id)
                            if cancel_task:
                                cancel_task.status = TaskStatus.CANCELLED
                                cancel_task.latest_status_message = "Cancelled: dependency task failed"
                                # Release account slot if held
                                cancel_acct = (cancel_task.context or {}).get("max_plan_account_id")
                                if cancel_acct and not (cancel_task.context or {}).get("_slot_released"):
                                    await release_max_plan_account(cancel_acct)
                                    cancel_task.context = {**(cancel_task.context or {}), "_slot_released": True}
                                cancel_session.add(cancel_task)
                                # Cascade cancel to children in non-terminal states
                                children = (await cancel_session.execute(
                                    select(Task).where(
                                        Task.parent_task_id == task.id,
                                        Task.status.not_in([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
                                    )
                                )).scalars().all()
                                for child in children:
                                    child.status = TaskStatus.CANCELLED
                                    child.latest_status_message = "Cancelled: parent dependency failed"
                                    child_acct = (child.context or {}).get("max_plan_account_id")
                                    if child_acct and not (child.context or {}).get("_slot_released"):
                                        await release_max_plan_account(child_acct)
                                        child.context = {**(child.context or {}), "_slot_released": True}
                                    cancel_session.add(child)
                                await cancel_session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                                                   {"task_id": str(task.id), "status": TaskStatus.CANCELLED})
                        await _ensure_vm_deleted(task, reason="dependency_cancelled")
                        logger.error(f"[ORCHESTRATOR] Cancelled task {task.id} — dependency failed")
                    elif all_met:
                        eligible_ids.append(task.id)
                    else:
                        # Surface dependency-wait reason to UI
                        pending = [str(dt.id)[:8] for dt in dep_tasks if dt.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)]
                        dep_msg = f"Waiting on {len(pending)} dep{'s' if len(pending) != 1 else ''}: {', '.join(pending[:3])}"
                        if pending and task.latest_status_message != dep_msg:
                            try:
                                async with AsyncSessionLocal() as _dep_session:
                                    _dep_task = await _dep_session.get(Task, task.id)
                                    if _dep_task and _dep_task.latest_status_message != dep_msg:
                                        _dep_task.latest_status_message = dep_msg
                                        _dep_session.add(_dep_task)
                                        await _dep_session.commit()
                            except Exception:
                                logger.exception(f"[ORCHESTRATOR] Failed to update dep wait message for task {task.id}")

                # Step 2.4: Deadlock detection — break circular deps among stuck QUEUED tasks
                global _last_deadlock_check
                now = time.time()
                if now - _last_deadlock_check > 60:
                    _last_deadlock_check = now
                    stuck_tasks = [t for t in candidate_tasks
                                   if t.id not in eligible_ids
                                   and (t.depends_on or [])
                                   and t.status == TaskStatus.QUEUED]
                    narrow_broken = 0
                    if stuck_tasks:
                        stuck_ids = {t.id for t in stuck_tasks}

                        # Batch all dependency UUIDs across all stuck tasks into one query
                        all_dep_ids: set[uuid.UUID] = set()
                        task_dep_uuids: dict = {}
                        for stask in stuck_tasks:
                            dep_uuids: set[uuid.UUID] = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            task_dep_uuids[stask.id] = dep_uuids
                            all_dep_ids.update(dep_uuids)

                        dep_status: dict = {}
                        if all_dep_ids:
                            dep_rows = await session.execute(select(Task).where(Task.id.in_(all_dep_ids)))
                            for dep_task in dep_rows.scalars().all():
                                dep_status[dep_task.id] = dep_task.status

                        # True cycle detection — avoids false positives where a
                        # stuck task waits on another that depends on an active task
                        classified = _classify_stuck_tasks(
                            stuck_tasks, task_dep_uuids, dep_status, stuck_ids
                        )
                        if classified:
                            async with AsyncSessionLocal() as fix_session:
                                for task_id, reason in classified:
                                    fix_task = await fix_session.get(Task, task_id)
                                    if fix_task:
                                        if reason == "cycle":
                                            msg = "Dependencies cleared: dependency cycle detected"
                                            tag = "cycle broken"
                                        else:
                                            msg = "Dependencies cleared: all deps failed/cancelled"
                                            tag = "dead deps cleared"
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock ({tag}): "
                                            f"clearing depends_on for task {task_id}"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = msg
                                        fix_session.add(fix_task)
                                        await broadcast_to_project(
                                            str(fix_task.project_id), "task_updated",
                                            {"task_id": str(task_id), "status": "QUEUED", "message": msg}
                                        )
                                    eligible_ids.append(task_id)
                                    narrow_broken += 1
                                await fix_session.commit()

                    # Broader scan: check QUEUED tasks older than 10 min with deps (not just candidate batch)
                    ten_min_ago = _utcnow() - timedelta(minutes=10)
                    broad_stmt = select(Task).where(
                        Task.status == TaskStatus.QUEUED,
                        Task.created_at < ten_min_ago,
                        Task.requires_approval.is_(False),
                        Task.depends_on.isnot(None),
                    ).options(load_only(Task.id, Task.depends_on, Task.status, Task.created_at)).limit(100)
                    broad_stuck = (await session.execute(broad_stmt)).scalars().all()
                    candidate_task_ids = {t.id for t in candidate_tasks}
                    broad_stuck = [t for t in broad_stuck
                                   if (t.depends_on or [])
                                   and t.id not in eligible_ids
                                   and t.id not in candidate_task_ids]
                    broad_broken = 0
                    if broad_stuck:
                        broad_stuck_ids = {t.id for t in broad_stuck}

                        # Batch all dependency UUIDs for broad scan into one query
                        all_dep_ids_broad: set[uuid.UUID] = set()
                        broad_task_dep_uuids: dict = {}
                        for stask in broad_stuck:
                            dep_uuids_b: set[uuid.UUID] = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids_b.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            broad_task_dep_uuids[stask.id] = dep_uuids_b
                            all_dep_ids_broad.update(dep_uuids_b)

                        broad_dep_status: dict = {}
                        if all_dep_ids_broad:
                            broad_dep_rows = await session.execute(
                                select(Task.id, Task.status).where(Task.id.in_(all_dep_ids_broad))
                            )
                            for dep_id, dep_status in broad_dep_rows.all():
                                broad_dep_status[dep_id] = dep_status

                        broad_classified = _classify_stuck_tasks(
                            broad_stuck, broad_task_dep_uuids, broad_dep_status, broad_stuck_ids
                        )
                        if broad_classified:
                            async with AsyncSessionLocal() as fix_session:
                                for task_id, reason in broad_classified:
                                    fix_task = await fix_session.get(Task, task_id)
                                    if fix_task:
                                        if reason == "cycle":
                                            msg = "Dependencies cleared: dependency cycle detected (broad scan)"
                                            tag = "cycle broken"
                                        else:
                                            msg = "Dependencies cleared: all deps failed/cancelled (broad scan)"
                                            tag = "dead deps cleared"
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock (broad scan, {tag}): "
                                            f"clearing depends_on for task {task_id}"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = msg
                                        fix_session.add(fix_task)
                                        await broadcast_to_project(
                                            str(fix_task.project_id), "task_updated",
                                            {"task_id": str(task_id), "status": "QUEUED", "message": msg}
                                        )
                                        broad_broken += 1
                                await fix_session.commit()

                    total_broken = narrow_broken + broad_broken
                    if total_broken > 0:
                        logger.warning(
                            f"[ORCHESTRATOR] Deadlock resolution cycle complete: "
                            f"{narrow_broken} narrow + {broad_broken} broad = {total_broken} deadlock(s) broken"
                        )

                # Step 2.5: File-conflict check — defer tasks that would conflict with in-progress work
                eligible_ids = await filter_file_conflicts(
                    eligible_ids, candidate_tasks, active_tasks_result, session, broadcast_to_project
                )

                # Step 2.6: Defer tasks that overlap with pending-rollup files
                eligible_ids = await filter_rollup_conflicts(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.7: Health-aware reordering — boost small/low-risk tasks when degraded.
                # Only activates under degradation so normal priority ordering is preserved.
                # Degradation signals: any zone in cooldown, failure rate > 30%, or slots < 50% of max.
                if len(eligible_ids) > 1:
                    _degradation_signals = []

                    _zone_summary = get_zone_health_summary()
                    if _zone_summary["available"] < _zone_summary["total_zones"]:
                        _degradation_signals.append(
                            f"zones {_zone_summary['available']}/{_zone_summary['total_zones']}"
                        )

                    if recent_results:
                        _hw_window = recent_results[-10:]
                        _hw_fail_count = sum(1 for r in _hw_window if r[1] == "failure")
                        _hw_fail_rate = _hw_fail_count / len(_hw_window)
                        if _hw_fail_rate > 0.30:
                            _degradation_signals.append(
                                f"failure rate {_hw_fail_count}/{len(_hw_window)}"
                            )

                    _max_vms = _runtime_config["max_concurrent_vms"]
                    if slots_available < _max_vms * 0.5:
                        _degradation_signals.append(f"slots {slots_available}/{_max_vms}")

                    if _degradation_signals:
                        _task_by_id = {t.id: t for t in candidate_tasks}
                        _original_order = list(eligible_ids)
                        eligible_ids = sorted(
                            eligible_ids,
                            key=lambda tid: _health_score_task(_task_by_id[tid]) if tid in _task_by_id else 0,
                            reverse=True,
                        )
                        if eligible_ids != _original_order:
                            logger.info(
                                "[ORCHESTRATOR] Health-aware reordering applied (%s): "
                                "%d eligible tasks reordered to prioritize small/low-risk scope",
                                ", ".join(_degradation_signals),
                                len(eligible_ids),
                            )

                # Respect slot limit after conflict filtering
                eligible_ids = eligible_ids[:slots_available]

                # Step 3: Lock ONLY the tasks we're about to dispatch (minimal lock duration)
                queued_tasks = []
                if eligible_ids:
                    lock_stmt = select(Task).where(
                        Task.id.in_(eligible_ids),
                        Task.status == TaskStatus.QUEUED  # Re-check status under lock
                    ).with_for_update(skip_locked=True)
                    queued_tasks = list((await session.execute(lock_stmt)).scalars().all())

                logger.info(f"[ORCHESTRATOR] Cycle: {len(queued_tasks)} queued, {active_count} active, {slots_available} slots, pool={len(_account_by_id)} accounts")
                _shared_state.tasks_queued_gauge = (await session.execute(
                    select(_func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
                )).scalar() or 0
                # tasks_active_gauge already set above after active_count is computed

                # Zone pre-check: skip dispatch entirely if all GCP zones are in cooldown
                if queued_tasks and not any_zone_available():
                    global _last_zone_exhaustion_log
                    now_check = datetime.now(timezone.utc)
                    if _last_zone_exhaustion_log is None or (now_check - _last_zone_exhaustion_log).total_seconds() >= 60:
                        _last_zone_exhaustion_log = now_check
                        recovery = soonest_zone_recovery()
                        if recovery:
                            wait_sec = max(0, (recovery - now_check).total_seconds())
                            logger.info(f'[ORCHESTRATOR] All zones in cooldown — skipping dispatch (recovery in {int(wait_sec)}s)')
                        else:
                            wait_sec = None
                            logger.info('[ORCHESTRATOR] All zones in cooldown — skipping dispatch')
                        try:
                            await ws_manager.broadcast_to_all("pipeline_throttle", {
                                "reason": "zone_exhaustion",
                                "detail": "All GCP zones in cooldown",
                                "recovery_seconds": int(wait_sec) if wait_sec is not None else None,
                            })
                        except Exception:
                            logger.exception("[ORCHESTRATOR] Failed to broadcast zone_exhaustion throttle")
                    queued_tasks = []  # Clear so the loop below is skipped

                for task in queued_tasks:
                    # Ensure VM ID exists (cleared during stale PROVISIONING recovery)
                    if not task.vm_instance_id:
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    logger.info(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Select Max plan account from task owner's pool, filtered by model
                    _blacklist = (task.context or {}).get("blacklisted_accounts")
                    task_project = await session.get(Project, task.project_id)
                    # Launcher pays; fall back to project owner for autonomous tasks
                    dispatch_user_id = task.launched_by_user_id or (task_project.user_id if task_project else "")
                    # Resolve which model this task needs
                    task_model = _extract_task_model(task)
                    if not task_model:
                        task_type = task.payloads[0].get("type", "create") if task.payloads else "create"
                        task_model = _runtime_config.get("task_type_models", {}).get(task_type)
                    # Always persist model to payload so rate-limit handler knows which
                    # model was used. Claude Code defaults to Sonnet when no --model flag.
                    if task.payloads and not task.payloads[0].get("model"):
                        task.payloads[0]["model"] = task_model or "claude-sonnet-4-6"
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(task, "payloads")
                        session.add(task)
                    # Keep task_model in sync with the payload — otherwise
                    # select_max_plan_account gets model=None and bypasses
                    # disabled_models filtering entirely.
                    if task.payloads:
                        task_model = task.payloads[0].get("model") or task_model
                    # Prune stale blacklist entries — accounts whose model cooldown has expired
                    # should be eligible again, otherwise rate-limited tasks get stuck forever
                    if _blacklist:
                        async with _max_plan_pool_lock:
                            _blacklist = [
                                aid for aid in _blacklist
                                if aid in _account_by_id and not _account_available_for_model(_account_by_id[aid], task_model)
                            ]
                        if _blacklist != (task.context or {}).get("blacklisted_accounts"):
                            task.context = {**(task.context or {}), "blacklisted_accounts": _blacklist or None}
                            if not _blacklist:
                                task.context.pop("blacklisted_accounts", None)
                            session.add(task)
                    account = await select_max_plan_account(user_id=dispatch_user_id, model=task_model, blacklist=_blacklist)
                    oauth_token = None
                    claude_config_json = None
                    account_id = None
                    if account:
                        account_id = account["id"]
                        oauth_token = account.get("oauth_token")
                        claude_config_json = account.get("claude_config_json")
                        logger.info(f"[ORCHESTRATOR] Using Max plan account '{account['name']}'")
                    else:
                        # No available account — either all disabled/blacklisted or none configured
                        user_pool = _max_plan_pools.get(dispatch_user_id, [])
                        if user_pool:
                            # Accounts exist but all blocked — notify user and skip
                            logger.info(f"[ORCHESTRATOR] All accounts blocked for task {task.id} — skipping (will retry next cycle)")
                            # Calculate earliest recovery time from account cooldowns
                            earliest_recovery = None
                            for acct in user_pool:
                                dm = acct.get('disabled_models', {})
                                for model_name, info in dm.items():
                                    disabled_at = info.get('at')
                                    hits = info.get('hits', 1)
                                    cooldown_min = min(30 * (2 ** (hits - 1)), 240)
                                    if disabled_at:
                                        recovery = disabled_at + timedelta(minutes=cooldown_min)
                                        if earliest_recovery is None or recovery < earliest_recovery:
                                            earliest_recovery = recovery
                                # Also check legacy account-level disable
                                if not acct.get('enabled', True) and acct.get('disabled_at'):
                                    rl_hits = acct.get('rl_hit_count', 1)
                                    cooldown_min = min(30 * (2 ** (rl_hits - 1)), 240)
                                    recovery = acct['disabled_at'] + timedelta(minutes=cooldown_min)
                                    if earliest_recovery is None or recovery < earliest_recovery:
                                        earliest_recovery = recovery
                            if earliest_recovery:
                                wait_min = max(0, (earliest_recovery - _utcnow()).total_seconds() / 60)
                                stall_msg = f'All accounts rate-limited — earliest recovery in {int(wait_min)}min'
                            else:
                                stall_msg = 'All accounts rate-limited — waiting for cooldown'
                            # Short retry delay so we don't hot-loop while all accounts are blocked.
                            # Cap at 2 min to stay responsive when accounts recover sooner.
                            stall_ctx = dict(task.context or {})
                            stall_ctx["retry_after"] = (_utcnow() + timedelta(minutes=2)).isoformat()
                            task.context = stall_ctx
                            if not (task.latest_status_message or "").startswith("All accounts"):
                                # Update directly on the locked task object (separate session would deadlock)
                                task.latest_status_message = stall_msg
                                await broadcast_to_project(str(task.project_id), "task_updated",
                                    {"task_id": str(task.id), "status": task.status, "message": stall_msg})
                            session.add(task)
                            continue
                        # No accounts at all — check for API key fallback
                        if os.getenv("ANTHROPIC_API_KEY"):
                            logger.info("[ORCHESTRATOR] No Max plan accounts, using ANTHROPIC_API_KEY fallback")
                        else:
                            logger.warning(f"[ORCHESTRATOR] No credentials available — skipping task {task.id}")
                            # Update directly on the locked task object (separate session would deadlock)
                            task.latest_status_message = "Waiting for credentials — add a Max plan account or set ANTHROPIC_API_KEY"
                            session.add(task)
                            continue

                    # Store which account this task uses for cleanup on completion
                    task.context = {**(task.context or {}), "max_plan_account_id": account_id}

                    # Mark as PROVISIONING to prevent other workers from picking it up.
                    # last_heartbeat is intentionally NOT set here — it must reflect real
                    # agent activity only. The reaper uses vm_provisioned_at as the clock
                    # for the pre-claim window so it can distinguish "VM created but agent
                    # never claimed" from "agent claimed and crashed".
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = _utcnow()
                    task.vm_provisioned_at = _utcnow()
                    task.last_heartbeat = None
                    _shared_state.deploy_monitoring["last_dispatch_time"] = _utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        # active_tasks already pre-incremented in select_max_plan_account()
                        logger.info(f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM")
                    except Exception as e:
                        import traceback
                        logger.error(f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}", exc_info=True)
                        traceback.print_exc()
                        # Release the pre-incremented slot on commit failure
                        if account_id:
                            await release_max_plan_account(account_id)
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID,
                                                    payloads: list, token: str, config_json: str,
                                                    acct_id_for_release: str):
                        success = False
                        try:
                            t_type = (payloads[0] or {}).get("type", "") if payloads else ""

                            # Upload payload to GCS and store agent credentials in Secret Manager
                            # concurrently — both are independent I/O operations (~500ms each)
                            # that produce inputs for the VM startup script.
                            async def _upload_payload_safe() -> str | None:
                                if not payloads:
                                    return None
                                try:
                                    return await upload_json_payload(str(t_id), payloads)
                                except Exception as e:
                                    logger.error(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}", exc_info=True)
                                    return None

                            payload_uri, secret_version_name = await asyncio.gather(
                                _upload_payload_safe(),
                                prepare_and_store_agent_secret(vm_id, t_id, token, config_json, t_type),
                            )

                            success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                               oauth_token=token, claude_config_json=config_json,
                                                               task_type=t_type, secret_version_name=secret_version_name)
                            if success:
                                # Verify the task still wants this VM — stale recovery may have
                                # requeued the task with a new vm_id while we were provisioning.
                                # If so, this VM is an orphan: delete it immediately.
                                try:
                                    async with AsyncSessionLocal() as hb_session:
                                        hb_task = await hb_session.get(Task, t_id)
                                        if not hb_task or hb_task.vm_instance_id != vm_id:
                                            current_vm = hb_task.vm_instance_id if hb_task else "gone"
                                            logger.warning(f"[ORCHESTRATOR] VM {vm_id} created but task {t_id} now has vm_id={current_vm} — deleting orphan")
                                            await _ensure_vm_deleted(vm_id, reason="vm_id_mismatch")
                                            success = False
                                            return
                                        if hb_task.status == TaskStatus.PROVISIONING:
                                            # Refresh vm_provisioned_at to the point the GCE API
                                            # confirmed the VM is running. This gives the agent a
                                            # fresh window to claim before the reaper considers it
                                            # a silent startup failure. last_heartbeat is left alone
                                            # so only real agent activity writes it.
                                            hb_task.vm_provisioned_at = _utcnow()
                                            # Save provision zone for diagnostics
                                            prov_zone = get_vm_zone(vm_id)
                                            hb_task.context = {**(hb_task.context or {}), "provision_zone": prov_zone}
                                            hb_session.add(hb_task)
                                            await hb_session.commit()
                                except Exception as e:
                                    logger.warning(f"[ORCHESTRATOR] Failed to verify/touch heartbeat after provision for {t_id}: {e}")
                        except asyncio.CancelledError:
                            # Stale recovery cancelled us, but the underlying GCP thread may
                            # still create the VM. Schedule a delayed cleanup to catch it.
                            logger.warning(f"[ORCHESTRATOR] Provision for {vm_id} cancelled — scheduling orphan cleanup")
                            async def _delayed_orphan_cleanup(orphan_vm_id: str):
                                await asyncio.sleep(120)  # Wait for GCP thread to finish
                                await _ensure_vm_deleted(orphan_vm_id, reason="cancelled_provision_orphan")
                            asyncio.create_task(_delayed_orphan_cleanup(vm_id))
                            raise
                        except Exception as e:
                            logger.error(f"[ORCHESTRATOR] Provision crashed for task {t_id}: {e}", exc_info=True)
                            success = False
                        finally:
                            # Always release the Max Plan account slot on failure, even if an
                            # exception escapes the try/except above (e.g. from record_task_result)
                            if not success and acct_id_for_release:
                                await release_max_plan_account(acct_id_for_release)
                        if not success:
                            provision_err = get_provision_error(vm_id)
                            logger.error(f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}: {provision_err}")
                            try:
                                record_task_result(success=False, failure_class="PROVISION_FAILURE", task_id=str(t_id))
                                # Hoist zone lookup so it can be saved to context and used for zone failure recording
                                prov_zone = get_vm_zone(vm_id)
                                # Capture serial port logs before VM deletion (best-effort)
                                serial_log = None
                                try:
                                    if prov_zone and prov_zone != "unknown":
                                        from app.compute import _capture_serial_for_vm
                                        serial_log = await asyncio.to_thread(_capture_serial_for_vm, vm_id, prov_zone)
                                        if serial_log:
                                            logger.error(f"[ORCHESTRATOR] Serial log for failed VM {vm_id}:\n{serial_log[-500:]}")
                                except Exception:
                                    logger.exception(f"[ORCHESTRATOR] Failed to capture serial log for VM {vm_id}")
                                # Requeue with retry tracking — zone exhaustion is transient
                                try:
                                    async with AsyncSessionLocal() as inner_session:
                                        failed_task = await inner_session.get(Task, t_id)
                                        if failed_task:
                                            ctx = dict(failed_task.context or {})
                                            if prov_zone:
                                                ctx["provision_zone"] = prov_zone
                                            if serial_log:
                                                ctx["last_serial_log"] = serial_log[-500:]

                                            # Classify: infra-wide (zone/quota exhaustion) vs task-specific
                                            err_lower = (provision_err or "").lower()
                                            is_infra = (
                                                any(p in err_lower for p in INFRA_PROVISION_PATTERNS)
                                                or not any_zone_available()
                                            )

                                            if is_infra:
                                                # Infrastructure-wide failure — don't burn retry budget
                                                infra_count = ctx.get("provision_failures_infra", 0) + 1
                                                ctx["provision_failures_infra"] = infra_count
                                                ctx["_infra_wait"] = True
                                                failed_task.status = TaskStatus.QUEUED
                                                failed_task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                                failed_task.last_heartbeat = None
                                                failed_task.started_at = None
                                                # Align retry with zone recovery; cap at 30 min
                                                recovery = soonest_zone_recovery()
                                                if recovery:
                                                    wait_sec = max(60, (recovery - datetime.now(timezone.utc)).total_seconds() + 60)
                                                    delay_min = min(wait_sec / 60, 30)
                                                else:
                                                    delay_min = 10
                                                ctx["retry_after"] = (_utcnow() + timedelta(minutes=delay_min)).isoformat()
                                                failed_task.latest_status_message = f"Infra-wide provision failure (#{infra_count}), requeued — waiting for zone recovery: {provision_err or 'unknown'}"[:500]
                                                logger.warning(f"[ORCHESTRATOR] Task {t_id} infra provision failure — requeuing without burning retry budget (delay {delay_min:.0f}min)")
                                            else:
                                                # Task-specific failure — burn retry budget
                                                prov_failures = ctx.get("provision_failures", 0) + 1
                                                ctx["provision_failures"] = prov_failures
                                                ctx.pop("_infra_wait", None)
                                                max_prov_retries = _runtime_config.get("max_provision_retries", 3)
                                                if prov_failures >= max_prov_retries:
                                                    # Exhausted retries — permanent failure
                                                    failed_task.status = TaskStatus.FAILED
                                                    failed_task.latest_status_message = f"VM provisioning failed {prov_failures} times — giving up: {provision_err or 'unknown error'}"
                                                    ctx["failure_category"] = "PROVISION_FAILURE"
                                                    await _maybe_create_failure_alert(
                                                        failed_task, inner_session,
                                                        (provision_err or "unknown error")[:200]
                                                    )
                                                    logger.error(f"[ORCHESTRATOR] Task {t_id} provision failed {prov_failures} times — permanent failure")
                                                else:
                                                    # Requeue for retry
                                                    failed_task.status = TaskStatus.QUEUED
                                                    failed_task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                                    failed_task.last_heartbeat = None
                                                    failed_task.started_at = None
                                                    # Back off 2 min per failure to let zones recover
                                                    ctx["retry_after"] = (_utcnow() + timedelta(minutes=2 * prov_failures)).isoformat()
                                                    failed_task.latest_status_message = f"Provision failed (attempt {prov_failures}/{max_prov_retries}), requeued: {provision_err or 'unknown'}"
                                                    logger.warning(f"[ORCHESTRATOR] Task {t_id} provision failed (attempt {prov_failures}) — requeuing")

                                            failed_task.context = ctx
                                            inner_session.add(failed_task)
                                            await inner_session.commit()
                                            await broadcast_to_project(
                                                str(p_id),
                                                "task_updated",
                                                {"task_id": str(t_id), "status": failed_task.status}
                                            )
                                            # Record zone failure so the zone health system avoids this zone
                                            if prov_zone and prov_zone != "unknown":
                                                record_zone_failure(prov_zone, reason="PROVISION_INLINE_FAILURE")
                                except Exception as e:
                                    logger.error(f"[ORCHESTRATOR] Error handling provision failure for task {t_id}: {e}", exc_info=True)
                            finally:
                                # ALWAYS delete partially-created VM, even if requeue logic crashes
                                await _ensure_vm_deleted(vm_id, reason="provision_failure")

                    provision_coro = asyncio.create_task(_provision_and_handle(
                        task.vm_instance_id, task.id, task.project_id,
                        task.payloads, oauth_token, claude_config_json, account_id
                    ))
                    _provision_coroutines[str(task.id)] = provision_coro
                    provision_coro.add_done_callback(lambda _, tid=str(task.id): _provision_coroutines.pop(tid, None))

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING}
                    )

                # Flush any status message updates from skipped tasks (e.g., "All accounts rate-limited")
                if session.dirty:
                    await session.commit()

                # Check tasks waiting on CI (review approved, CI pending)
                CI_WAIT_TIMEOUT_MINUTES = 20
                ci_waiting_tasks = (await session.execute(
                    select(Task).where(Task.status == TaskStatus.EVALUATING).limit(200)
                )).scalars().all()
                for ci_task in ci_waiting_tasks:
                    ci_loop = (ci_task.context or {}).get("review_loop", {})
                    if not ci_loop.get("ci_waiting"):
                        continue
                    if ci_loop.get("conflict_fix_spawned"):
                        continue  # Already handled — fix task spawned for merge conflicts
                    if ci_loop.get("ci_fix_spawned"):
                        continue  # Already handled — fix task spawned for CI failure
                    if ci_loop.get("merge_rule_blocked"):
                        continue  # Merge blocked by branch protection — requires manual intervention
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        continue

                    # Ensure ci_wait_started is always set — some paths set ci_waiting=True
                    # without setting ci_wait_started, which silently disables the timeout.
                    # Commit immediately so the update is persisted even if we continue below.
                    if not ci_loop.get("ci_wait_started"):
                        ci_loop["ci_wait_started"] = _utcnow().isoformat()
                        ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                        session.add(ci_task)
                        await session.commit()

                    # Exponential backoff: skip if not enough time has elapsed since last poll.
                    # Webhooks handle the fast path — polling is a fallback.
                    ci_poll_interval = ci_loop.get("ci_poll_interval", 30)
                    ci_last_polled = ci_loop.get("ci_last_polled")
                    if ci_last_polled:
                        try:
                            _last_polled_dt = datetime.fromisoformat(ci_last_polled)
                            if (_utcnow() - _last_polled_dt).total_seconds() < ci_poll_interval:
                                continue
                        except (ValueError, TypeError):
                            pass

                    try:
                        # Timeout: if CI has been pending too long, proceed to merge anyway
                        # Use ci_wait_started if available, otherwise fall back to started_at/created_at
                        ci_wait_started = ci_loop.get("ci_wait_started")
                        try:
                            started = datetime.fromisoformat(ci_wait_started) if ci_wait_started else None
                        except (ValueError, TypeError):
                            started = None
                        if started is None:
                            started = ci_task.started_at or ci_task.created_at
                        if started and (_utcnow() - started).total_seconds() > CI_WAIT_TIMEOUT_MINUTES * 60:
                            # Escalate: alert the user but keep retrying.
                            # The 30-min backstop in services.py is the true hard-kill.
                            if not ci_loop.get("escalated"):
                                ci_loop["escalated"] = True
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                ci_task.latest_status_message = f"CI wait exceeded {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — retrying, alert sent"
                                session.add(ci_task)
                                await _maybe_create_failure_alert(
                                    ci_task, session,
                                    f"Rollup PR #{ci_pr_number} stuck in CI-wait for >{CI_WAIT_TIMEOUT_MINUTES}min — still retrying"
                                )
                                await session.commit()
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
                            # Fall through — continue polling CI even after escalation

                        ci_gh_token, ci_owner, ci_repo = await get_github_token_for_task(ci_task.id, session)

                        # Use cached head SHA when available to skip the PR fetch (saves 1 API call per poll).
                        # Re-fetch PR every 5th poll to detect force-pushes and merge state changes.
                        cached_head_sha = ci_loop.get("head_sha", "")
                        polls_since_fetch = ci_loop.get("polls_since_pr_fetch", 5) + 1  # default 5 to force first fetch
                        need_pr_fetch = not cached_head_sha or polls_since_fetch >= 5
                        pr_data = None

                        if not need_pr_fetch:
                            # Fast path: skip PR fetch, use cached SHA, just check CI
                            pr_data = {"state": "open", "head": {"sha": cached_head_sha}, "mergeable": True}
                            ci_loop["polls_since_pr_fetch"] = polls_since_fetch

                        if need_pr_fetch:
                            ci_loop["polls_since_pr_fetch"] = 0
                            try:
                                pr_data = await get_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                            except HTTPException as exc:
                                if exc.status_code in (404, 410):
                                    _pr_gone_msg = f"PR #{ci_pr_number} no longer exists (deleted or repo archived)"
                                    logger.exception(f"[CI] {_pr_gone_msg} for task {ci_task.id}")
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.latest_status_message = _pr_gone_msg
                                    session.add(ci_task)
                                    await _maybe_create_failure_alert(ci_task, session, _pr_gone_msg)
                                    await session.commit()
                                    await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                               {"task_id": str(ci_task.id), "status": ci_task.status})
                                    continue
                                # Transient GitHub API error — backoff and retry
                                _fetch_failures = ci_loop.get("pr_fetch_failures", 0) + 1
                                ci_loop["pr_fetch_failures"] = _fetch_failures
                                ci_loop["ci_last_polled"] = _utcnow().isoformat()
                                ci_loop["ci_poll_interval"] = min((ci_loop.get("ci_poll_interval", 30)) * 2, 300)
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                logger.warning(f"[CI] Transient error fetching PR #{ci_pr_number} for task {ci_task.id} "
                                               f"(attempt {_fetch_failures}): {exc.detail}")
                                session.add(ci_task)
                                await session.commit()
                                if _fetch_failures >= 10 and not ci_loop.get("fetch_escalated"):
                                    ci_loop["fetch_escalated"] = True
                                    ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                    await _maybe_create_failure_alert(ci_task, session,
                                        f"PR #{ci_pr_number} GitHub API failures ({_fetch_failures}x) — still retrying")
                                    session.add(ci_task)
                                    await session.commit()
                                continue

                            if not pr_data:
                                continue

                            if ci_loop.get("pr_fetch_failures"):
                                ci_loop["pr_fetch_failures"] = 0

                        pr_state = pr_data.get("state", "open")

                        # Check if PR was already merged (prevents double merge)
                        if pr_state == "closed" or pr_data.get("merged"):
                            ci_task.status = TaskStatus.COMPLETED
                            ci_task.latest_status_message = f"PR #{ci_pr_number} already merged/closed"
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            logger.info(f"[CI] PR #{ci_pr_number} already merged/closed, skipping")
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs
                        # GitHub returns None while computing mergeability — skip this cycle and recheck later
                        if pr_data.get("mergeable") is None:
                            continue
                        if pr_data.get("mergeable") is False:
                            ci_payload = ci_task.first_payload
                            ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                            ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                            ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")
                            # Peek at CI state for this PR's head SHA so the fix task's prompt
                            # can describe BOTH blockers (conflicts + CI) if they coexist.
                            # The mergeable state can flip between now and when the fix agent
                            # runs — so we also tell the agent to re-verify before acting.
                            ci_preview_section = ""
                            try:
                                _conflict_head_sha = pr_data.get("head", {}).get("sha", "")
                                if _conflict_head_sha:
                                    _conflict_ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, _conflict_head_sha)
                                    if _conflict_ci_result.get("status") == "completed":
                                        _conflict_failed_logs = _conflict_ci_result.get("failed_logs", "")
                                        if _conflict_failed_logs:
                                            ci_preview_section = (
                                                f"\n\nAdditionally, CI checks are currently failing on this PR head "
                                                f"(the conflict is blocking a re-run, but these failures pre-date the conflict "
                                                f"or exist independently):\n```\n{_conflict_failed_logs[:1500]}\n```\n"
                                                f"After resolving conflicts, fix the CI failures above in the same push."
                                            )
                            except Exception as _ci_preview_exc:
                                logger.debug(f"[CI] conflict-fix CI preview fetch failed for PR #{ci_pr_number}: {_ci_preview_exc}")
                            fix_prompt = (
                                f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                                f"Orchestrator observation at spawn time: GitHub reported the PR as UNMERGEABLE "
                                f"(merge conflicts against `{ci_base}`).\n\n"
                                f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                                f"between spawn and execution (a concurrent rollup may have resolved the conflict, "
                                f"or new commits may have reintroduced one). After cloning:\n"
                                f"  1. Attempt `git rebase origin/{ci_base}` — note whether it succeeds cleanly.\n"
                                f"  2. Check `git status` and `git diff --name-only --diff-filter=U` for unmerged paths.\n"
                                f"  3. If neither shows conflicts, the premise is stale — re-query the PR's "
                                f"current mergeable state and failing check runs via the GitHub API "
                                f"(`curl -H \"Authorization: Bearer $GITHUB_TOKEN\" "
                                f"https://api.github.com/repos/{ci_owner}/{ci_repo}/pulls/{ci_pr_number}`) "
                                f"and fix whatever is actually blocking the merge now.\n\n"
                                f"Primary action: rebase onto `{ci_base}` and resolve any conflicts."
                                f"{ci_preview_section}\n\n"
                                f"Before declaring done: ensure (a) no conflict markers remain in any file, "
                                f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                                f"every fix — do not leave uncommitted changes."
                            )
                            fix_task, _ = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=fix_prompt, original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                # Clear ci_waiting and mark as handled to prevent duplicate fix spawns
                                ci_loop_ctx = dict(ci_loop)
                                ci_loop_ctx["ci_waiting"] = False
                                ci_loop_ctx["conflict_fix_spawned"] = str(fix_task.id)
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop_ctx}
                                logger.info(f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}")
                            else:
                                # Fix not spawned — likely duplicate guard (fix already running) or iteration cap
                                ci_task.status = TaskStatus.FAILED
                                ci_task.requires_approval = True
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — fix iterations exhausted, requires manual intervention"
                                logger.warning(f"[CI] PR #{ci_pr_number} merge conflicts, fix not spawned — marking FAILED (requires_approval)")
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            continue

                        ci_head_sha = pr_data.get("head", {}).get("sha", "")
                        if not ci_head_sha:
                            ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            logger.info(f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle")
                            continue

                        ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, ci_head_sha, project_id=str(ci_task.project_id))

                        # Track whether CI checks have ever been observed for this PR
                        ci_details = ci_result.get("details", [])
                        if ci_details and not ci_loop.get("ci_checks_seen"):
                            ci_loop["ci_checks_seen"] = True
                            ci_loop["ci_first_check_seen_at"] = _utcnow().isoformat()

                        # Detect stalled CI: no checks appeared after reasonable wait
                        if not ci_loop.get("ci_checks_seen") and not ci_details:
                            _cws = ci_loop.get("ci_wait_started")
                            if _cws:
                                try:
                                    _no_checks_elapsed = (_utcnow() - datetime.fromisoformat(_cws)).total_seconds()
                                    if _no_checks_elapsed > 600 and not ci_loop.get("no_checks_escalated"):
                                        ci_loop["no_checks_escalated"] = True
                                        ci_task.latest_status_message = (
                                            f"No CI checks appeared on PR #{ci_pr_number} after "
                                            f"{int(_no_checks_elapsed / 60)}min — may need manual trigger"
                                        )
                                        ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                        session.add(ci_task)
                                        await _maybe_create_failure_alert(
                                            ci_task, session,
                                            f"PR #{ci_pr_number}: no CI checks after {int(_no_checks_elapsed / 60)}min"
                                        )
                                        await session.commit()
                                except (ValueError, TypeError):
                                    pass

                        if ci_result["status"] != "completed":
                            details = [d["name"] for d in ci_details if d.get("status") in ("queued", "in_progress")]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            # Smarter backoff: faster when CI is actively running
                            if ci_loop.get("escalated"):
                                max_interval = 60   # Post-escalation: poll frequently
                            elif ci_loop.get("ci_checks_seen"):
                                max_interval = 90   # Checks running: moderate backoff
                            else:
                                max_interval = 120  # No checks yet: cautious backoff
                            ci_loop["ci_poll_interval"] = min(ci_poll_interval * 2, max_interval)
                            ci_loop["ci_last_polled"] = _utcnow().isoformat()
                            ci_loop["head_sha"] = ci_head_sha
                            ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            continue

                        # CI completed — delegate to shared handler
                        from app.ci_completion import handle_ci_completion  # noqa: PLC0415
                        logger.info(f"[CI] Poll fallback: CI completed for PR #{ci_pr_number}, processing")
                        await handle_ci_completion(ci_task, ci_result, ci_gh_token, ci_owner, ci_repo, session, source="poll")
                    except Exception as e:
                        logger.exception(f"[CI] Error checking CI for task {ci_task.id}")
                        try:
                            status_code = getattr(e, "status_code", None)
                            if status_code == 404:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI polling failed: PR not found (404) — PR may have been deleted"
                                logger.exception(f"[CI] PR #{ci_task.context.get('review_loop', {}).get('pr_number')} returned 404 — marking task {ci_task.id} FAILED")
                            else:
                                ci_task.latest_status_message = f"CI check error: {type(e).__name__}: {e!r}"[:500]
                            session.add(ci_task)
                            await session.commit()
                        except Exception:
                            logger.exception(f"[CI] Failed to update ci_task status after error for task {ci_task.id}")
                            try:
                                await session.rollback()
                            except Exception:
                                logger.exception(f"[CI] Rollback also failed for task {ci_task.id}")

            _orchestrator_consecutive_errors = 0
            logger.info('orchestrator_cycle_complete', extra={
                'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                'tasks_dispatched': len(queued_tasks),
                'tasks_recovered': tasks_recovered,
                'slots_in_use': effective_count,
                'slots_available': slots_available,
                'queue_depth': _shared_state.tasks_queued_gauge,
            })
            _orchestrator_last_cycle = _utcnow()
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception:
            _orchestrator_consecutive_errors += 1
            backoff = min(0.5 * (2 ** (_orchestrator_consecutive_errors - 1)), 60) * random.uniform(0.8, 1.2)
            logger.error('orchestrator_cycle_error', extra={
                'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                'consecutive_errors': _orchestrator_consecutive_errors,
                'backoff_seconds': backoff,
            }, exc_info=True)
            _orchestrator_last_cycle = _utcnow()
            try:
                if session:
                    await session.rollback()
            except Exception:
                logger.exception("[ORCHESTRATOR] Rollback after cycle error failed")
            # After 3 consecutive errors, alert all projects (deduplicated)
            if _orchestrator_consecutive_errors >= 3:
                try:
                    async with AsyncSessionLocal() as alert_session:
                        await _create_orchestrator_error_alerts(alert_session, _orchestrator_consecutive_errors)
                        await alert_session.commit()
                except Exception:
                    logger.exception("[ORCHESTRATOR] Failed to create error alerts")

        # Monitoring checks run every cycle (not just when idle)
        try:
            await _check_monitoring_window()
            await _check_external_health()
            await _check_pause_mode()
            # Orphaned task recovery — every 120s
            if (_utcnow() - _last_orphan_check).total_seconds() >= 120:
                _last_orphan_check = _utcnow()
                await _recover_orphaned_tasks()
            # EVALUATING task timeout — every 300s
            if (_utcnow() - _last_evaluating_timeout_check).total_seconds() >= 300:
                _last_evaluating_timeout_check = _utcnow()
                await _check_evaluating_timeouts()
            _orchestrator_consecutive_monitoring_errors = 0
        except Exception as e:
            _orchestrator_consecutive_monitoring_errors += 1
            monitoring_backoff = min(0.5 * (2 ** (_orchestrator_consecutive_monitoring_errors - 1)), 60) * random.uniform(0.8, 1.2)
            logger.error(f"[ORCHESTRATOR] Monitoring check error (consecutive={_orchestrator_consecutive_monitoring_errors}, backoff={monitoring_backoff:.1f}s): {e}", exc_info=True)

        # Sleep based on whether we found tasks
        try:
            if _orchestrator_consecutive_errors > 0:
                backoff = min(0.5 * (2 ** (_orchestrator_consecutive_errors - 1)), 60) * random.uniform(0.8, 1.2)
                await asyncio.sleep(backoff)
            elif _orchestrator_consecutive_monitoring_errors > 0:
                monitoring_backoff = min(0.5 * (2 ** (_orchestrator_consecutive_monitoring_errors - 1)), 60) * random.uniform(0.8, 1.2)
                await asyncio.sleep(monitoring_backoff)
            elif queued_tasks:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(5)
            # Check autonomous research every cycle — interval gate inside handles throttling.
            # Spawns only when VM slots are available (not tied to queue being empty).
            await _check_idle_research()
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Sleep/idle check error: {e}", exc_info=True)
