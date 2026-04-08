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

from sqlalchemy import func as _func
from sqlmodel import select

from app.compute import (
    any_zone_available,
    get_provision_error,
    get_vm_zone,
    provision_agent_vm,
    soonest_zone_recovery,
)
from app.database import AsyncSessionLocal
from app.models import Project, Task, TaskStatus
from app.storage import upload_json_payload
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger("blueprint")


def _utcnow() -> datetime:
    """Return a timezone-naive UTC timestamp (matches DB convention)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Module-level globals managed by orchestrator_worker
# ---------------------------------------------------------------------------

_last_pool_refresh: datetime = _utcnow()
_last_zone_health_persist: datetime = _utcnow()
_last_orphan_check: datetime = _utcnow()
_last_evaluating_timeout_check: datetime = _utcnow()
_last_deadlock_check: float = 0.0
_last_zone_exhaustion_log: Optional[datetime] = None

# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: Optional[datetime] = None
_orchestrator_cycle_started: Optional[datetime] = None

# Consecutive error counters for exponential backoff
_orchestrator_consecutive_errors: int = 0
_orchestrator_consecutive_monitoring_errors: int = 0

# Track in-flight provision coroutines so stale recovery can cancel them.
# Without cancellation, zombie provisions hold semaphore slots for up to 360s,
# starving new provisions and creating a cascading failure where no VM can
# claim its task (the reaper assigns a new vm_instance_id before the VM boots).
_provision_coroutines: Dict[str, asyncio.Task] = {}


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    from app.main import _get_deploy_state_for_update
    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            # Prune: don't persist CLOSED breakers with no failures (default state)
            if v.get("state") == "CLOSED" and v.get("failure_count", 0) == 0:
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
    from app.main import _get_deploy_state_for_update, _pause_failure_streak, _health_recovery_streak
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


async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    # Late imports to avoid circular dependency with app.main
    from app.main import (
        load_max_plan_pool,
        _get_actual_gcp_vm_count,
        _ensure_vm_deleted,
        release_max_plan_account,
        select_max_plan_account,
        recheck_disabled_accounts,
        _extract_task_model,
        _verify_pr_merged,
        _maybe_create_failure_alert,
        _spawn_pipeline_task,
        record_task_result,
        broadcast_to_project,
        _recover_orphaned_tasks,
        _check_evaluating_timeouts,
        _check_monitoring_window,
        _check_external_health,
        _check_pause_mode,
        _check_idle_research,
        _create_orchestrator_error_alerts,
        _account_by_id,
        _max_plan_pools,
        _max_plan_pool_lock,
        get_github_token_for_task,
    )
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
                        if provision_failures >= _runtime_config["max_provision_retries"]:
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
                    logger.info('orchestrator_cycle_complete', extra={
                        'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                        'tasks_dispatched': 0,
                        'tasks_recovered': tasks_recovered,
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
                        logger.warning(f"[ORCHESTRATOR] High failure rate ({recent_failures}/{len(recent_window)}) — throttling dispatch for 30s")
                        # Run account re-enablement before sleeping so rate-limited accounts
                        # can recover even while dispatch is throttled
                        await recheck_disabled_accounts()
                        await asyncio.sleep(30)
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

                    all_met = True
                    any_failed = len(dep_tasks) < len(dep_uuids)  # Missing deps treated as failed
                    for dt in dep_tasks:
                        if dt.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]:
                            any_failed = True
                            break
                        if dt.status != TaskStatus.COMPLETED:
                            all_met = False
                            continue
                        # For create tasks, also require PR to be merged
                        dt_type = (dt.first_payload).get("type", "create")
                        if dt_type in ("create", "") and not await _verify_pr_merged(dt, session):
                            all_met = False

                    if any_failed:
                        # Use separate session to avoid polluting the main dispatch session
                        async with AsyncSessionLocal() as cancel_session:
                            cancel_task = await cancel_session.get(Task, task.id)
                            if cancel_task:
                                cancel_task.status = TaskStatus.CANCELLED
                                cancel_task.latest_status_message = "Cancelled: dependency task failed"
                                cancel_session.add(cancel_task)
                                await cancel_session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                                                   {"task_id": str(task.id), "status": TaskStatus.CANCELLED})
                        await _ensure_vm_deleted(task, reason="dependency_cancelled")
                        logger.error(f"[ORCHESTRATOR] Cancelled task {task.id} — dependency failed")
                    elif all_met:
                        eligible_ids.append(task.id)

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
                        for stask in stuck_tasks:
                            dep_uuids = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            # Verify each dep's actual DB status before clearing
                            # Only clear if deps are genuinely unresolvable (FAILED/CANCELLED/deleted)
                            genuinely_blocked = True
                            async with AsyncSessionLocal() as check_session:
                                for dep_id in dep_uuids:
                                    if dep_id in stuck_ids:
                                        continue  # Stuck in this pool — potential cycle
                                    dep_task = await check_session.get(Task, dep_id)
                                    if dep_task is None:
                                        continue  # Deleted dep — genuinely unresolvable
                                    if dep_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                                        continue  # Terminal — genuinely unresolvable
                                    if dep_task.status == TaskStatus.QUEUED:
                                        # QUEUED but outside fetch window — NOT a deadlock, just low priority
                                        genuinely_blocked = False
                                        break
                                    if dep_task.status in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
                                        # Still running — NOT a deadlock
                                        genuinely_blocked = False
                                        break
                            if genuinely_blocked and dep_uuids:
                                async with AsyncSessionLocal() as fix_session:
                                    fix_task = await fix_session.get(Task, stask.id)
                                    if fix_task:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock: clearing depends_on "
                                            f"for task {stask.id} (had {len(dep_uuids)} stuck/failed deps)"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = "Dependencies cleared: deadlock detected"
                                        fix_session.add(fix_task)
                                        await fix_session.commit()
                                eligible_ids.append(stask.id)
                                narrow_broken += 1

                    # Broader scan: check ALL QUEUED tasks older than 10 min (not just candidate batch)
                    ten_min_ago = _utcnow() - timedelta(minutes=10)
                    broad_stmt = select(Task).where(
                        Task.status == TaskStatus.QUEUED,
                        Task.created_at < ten_min_ago,
                        Task.requires_approval.is_(False),
                    )
                    broad_stuck = (await session.execute(broad_stmt)).scalars().all()
                    broad_stuck = [t for t in broad_stuck if (t.depends_on or []) and t.id not in eligible_ids]
                    broad_broken = 0
                    if broad_stuck:
                        broad_stuck_ids = {t.id for t in broad_stuck}
                        for stask in broad_stuck:
                            if stask.id in {t.id for t in candidate_tasks}:
                                continue  # Already checked above
                            dep_uuids = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            genuinely_blocked = True
                            async with AsyncSessionLocal() as check_session:
                                for dep_id in dep_uuids:
                                    if dep_id in broad_stuck_ids:
                                        continue
                                    dep_task = await check_session.get(Task, dep_id)
                                    if dep_task is None or dep_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                                        continue
                                    genuinely_blocked = False
                                    break
                            if genuinely_blocked and dep_uuids:
                                async with AsyncSessionLocal() as fix_session:
                                    fix_task = await fix_session.get(Task, stask.id)
                                    if fix_task:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock (broad scan): clearing depends_on "
                                            f"for task {stask.id} (stuck >{10}min with {len(dep_uuids)} unresolvable deps)"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = "Dependencies cleared: deadlock detected (broad scan)"
                                        fix_session.add(fix_task)
                                        await fix_session.commit()
                                broad_broken += 1
                    total_broken = narrow_broken + broad_broken
                    if total_broken > 0:
                        logger.warning(
                            f"[ORCHESTRATOR] Deadlock resolution cycle complete: "
                            f"{narrow_broken} narrow + {broad_broken} broad = {total_broken} deadlock(s) broken"
                        )

                # Step 2.5: File-conflict check — defer tasks that would conflict with in-progress work
                # Build a set of files currently being modified by IN_PROGRESS tasks
                in_progress_files: set[str] = set()
                for t in active_tasks_result:
                    if t.status == TaskStatus.IN_PROGRESS:
                        in_progress_files.update((t.context or {}).get("related_files", []))

                # Only apply conflict check if we have file data to work with
                if in_progress_files:
                    non_conflicting = []
                    for eid in eligible_ids:
                        candidate = next((t for t in candidate_tasks if t.id == eid), None)
                        if candidate is None:
                            non_conflicting.append(eid)
                            continue
                        candidate_files = set((candidate.context or {}).get("related_files", []))
                        if not candidate_files:
                            non_conflicting.append(eid)
                            continue
                        overlap = candidate_files & in_progress_files
                        if not overlap:
                            non_conflicting.append(eid)
                            continue
                        # Large scope tasks (refactors, etc.) defer on any overlap
                        scope = (candidate.context or {}).get("scope", "small")
                        task_type = (candidate.first_payload).get("type", "")
                        is_large = scope == "large" or task_type in ("refactor", "plan")
                        # Otherwise: defer if at least 2 files overlap AND >= 50% of candidate's files
                        should_defer = is_large or (len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5)
                        if should_defer:
                            logger.info(f"[DISPATCH] Deferring task {eid} — file conflict ({len(overlap)}/{len(candidate_files)} files, scope={scope}): {', '.join(sorted(overlap))}")
                        else:
                            non_conflicting.append(eid)
                    eligible_ids = non_conflicting

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
                            logger.info('[ORCHESTRATOR] All zones in cooldown — skipping dispatch')
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
                            if not (task.latest_status_message or "").startswith("All accounts"):
                                # Update directly on the locked task object (separate session would deadlock)
                                task.latest_status_message = stall_msg
                                session.add(task)
                                await broadcast_to_project(str(task.project_id), "task_updated",
                                    {"task_id": str(task.id), "status": task.status, "message": stall_msg})
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

                    # Mark as PROVISIONING to prevent other workers from picking it up
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = _utcnow()
                    task.vm_provisioned_at = _utcnow()
                    task.last_heartbeat = _utcnow()  # Reset stale-recovery clock from dispatch
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
                            payload_uri = None
                            if payloads:
                                try:
                                    payload_uri = await upload_json_payload(str(t_id), payloads)
                                except Exception as e:
                                    logger.error(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}", exc_info=True)

                            success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                               oauth_token=token, claude_config_json=config_json)
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
                                            hb_task.last_heartbeat = _utcnow()
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
                                # Capture serial port logs before VM deletion (best-effort)
                                serial_log = None
                                try:
                                    prov_zone = get_vm_zone(vm_id)
                                    if prov_zone and prov_zone != "unknown":
                                        from app.compute import _capture_serial_for_vm
                                        serial_log = await asyncio.to_thread(_capture_serial_for_vm, vm_id, prov_zone)
                                        if serial_log:
                                            logger.error(f"[ORCHESTRATOR] Serial log for failed VM {vm_id}:\n{serial_log[-500:]}")
                                except Exception:
                                    pass
                                # Requeue with retry tracking — zone exhaustion is transient
                                try:
                                    async with AsyncSessionLocal() as inner_session:
                                        failed_task = await inner_session.get(Task, t_id)
                                        if failed_task:
                                            ctx = dict(failed_task.context or {})
                                            prov_failures = ctx.get("provision_failures", 0) + 1
                                            ctx["provision_failures"] = prov_failures
                                            if serial_log:
                                                ctx["last_serial_log"] = serial_log[-500:]
                                            max_prov_retries = _runtime_config["max_provision_retries"]
                                            if prov_failures >= max_prov_retries:
                                                # Exhausted retries — permanent failure
                                                failed_task.status = TaskStatus.FAILED
                                                failed_task.latest_status_message = f"VM provisioning failed {prov_failures} times — giving up: {provision_err or 'unknown error'}"
                                                await _maybe_create_failure_alert(
                                                    failed_task, inner_session,
                                                    (provision_err or "unknown error")[:200]
                                                )
                                                logger.error(f"[ORCHESTRATOR] Task {t_id} provision failed {prov_failures} times — permanent failure")
                                            else:
                                                # Requeue for retry — zone exhaustion is transient
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
                CI_WAIT_TIMEOUT_MINUTES = 30
                ci_waiting_tasks = (await session.execute(
                    select(Task).where(Task.status == TaskStatus.EVALUATING)
                )).scalars().all()
                for ci_task in ci_waiting_tasks:
                    ci_loop = (ci_task.context or {}).get("review_loop", {})
                    if not ci_loop.get("ci_waiting"):
                        continue
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        continue

                    # Exponential backoff: skip if not enough time has elapsed since last poll
                    ci_poll_interval = ci_loop.get("ci_poll_interval", 10)
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
                        ci_wait_started = ci_loop.get("ci_wait_started")
                        if ci_wait_started:
                            try:
                                started = datetime.fromisoformat(ci_wait_started)
                                if (_utcnow() - started).total_seconds() > CI_WAIT_TIMEOUT_MINUTES * 60:
                                    logger.error(f"[CI] PR #{ci_pr_number} CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min")
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.latest_status_message = f"CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — manual inspection required"
                                    session.add(ci_task)
                                    await _maybe_create_failure_alert(
                                        ci_task, session,
                                        f"CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number}"
                                    )
                                    await session.commit()
                                    await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                               {"task_id": str(ci_task.id), "status": ci_task.status})
                                    await _ensure_vm_deleted(ci_task, reason="ci_timeout")
                                    continue
                            except (ValueError, TypeError) as e:
                                logger.error(f"[CI] Malformed ci_wait_started for task {ci_task.id}: {ci_wait_started} ({e})", exc_info=True)

                        ci_gh_token, ci_owner, ci_repo = await get_github_token_for_task(ci_task.id, session)

                        # Re-fetch current PR head to handle force-pushes
                        pr_data = await get_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
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
                            fix_prompt = (
                                f"PR #{ci_pr_number} has merge conflicts and CI cannot run.\n\n"
                                f"Rebase the branch onto {ci_base} and resolve all conflicts."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=fix_prompt, original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
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

                        ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, ci_head_sha)
                        if ci_result["status"] != "completed":
                            details = [d["name"] for d in ci_result.get("details", []) if d.get("status") in ("queued", "in_progress")]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            ci_loop["ci_poll_interval"] = min(ci_poll_interval * 2, 60)
                            ci_loop["ci_last_polled"] = _utcnow().isoformat()
                            ci_loop["head_sha"] = ci_head_sha
                            ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            continue

                        ci_payload = ci_task.first_payload
                        ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                        ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                        ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")

                        if ci_result["conclusion"] == "success":
                            # CI passed — merge
                            merge_result = await merge_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                            if merge_result.get("merged"):
                                await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                    "**Blueprint CI + Review**: All checks passed, PR auto-merged.")
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI passed, PR #{ci_pr_number} merged"
                                if ci_orig_id:
                                    try:
                                        orig = await session.get(Task, uuid.UUID(ci_orig_id))
                                        if orig:
                                            orig.context = {**(orig.context or {}), "pr_merged": True}
                                            if orig.status == TaskStatus.EVALUATING and not (orig.context or {}).get("review_loop", {}).get("ci_waiting"):
                                                orig.status = TaskStatus.COMPLETED
                                                orig.latest_status_message = "Rollup PR merged"
                                            session.add(orig)
                                    except Exception as e:
                                        logger.warning(f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True)
                                logger.info(f"[CI] PR #{ci_pr_number} CI passed, merged")
                            else:
                                for attempt in range(3):
                                    await asyncio.sleep(5 * (3 ** attempt))  # 5s, 15s, 45s
                                    merge_result = await merge_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                                    if merge_result.get('merged'):
                                        break
                                if merge_result.get('merged'):
                                    await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                        "**Blueprint CI + Review**: All checks passed, PR auto-merged.")
                                    ci_task.status = TaskStatus.COMPLETED
                                    ci_task.latest_status_message = f"CI passed, PR #{ci_pr_number} merged (after retry)"
                                    if ci_orig_id:
                                        try:
                                            orig = await session.get(Task, uuid.UUID(ci_orig_id))
                                            if orig:
                                                orig.context = {**(orig.context or {}), "pr_merged": True}
                                                if orig.status == TaskStatus.EVALUATING and not (orig.context or {}).get("review_loop", {}).get("ci_waiting"):
                                                    orig.status = TaskStatus.COMPLETED
                                                    orig.latest_status_message = "Rollup PR merged"
                                                session.add(orig)
                                        except Exception as e:
                                            logger.warning(f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True)
                                    logger.info(f"[CI] PR #{ci_pr_number} CI passed, merged after retry")
                                else:
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.latest_status_message = f"CI passed but merge blocked after 4 attempts: {merge_result.get('message', 'unknown')}"
                                    logger.error(f"[CI] PR #{ci_pr_number} merge blocked after retries: {merge_result}")
                        else:
                            # CI failed — check for existing active fix before spawning
                            active_tasks = (await session.execute(
                                select(Task).where(
                                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                                )
                            )).scalars().all()
                            existing_fix = next(
                                (t for t in active_tasks
                                 if t.first_payload.get("type") == "fix"
                                 and t.first_payload.get("pr_number") == ci_pr_number),
                                None
                            )
                            if existing_fix:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id}")
                                session.add(ci_task)
                                await session.commit()
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
                                continue
                            ci_errors = ci_result.get("failed_logs", "CI checks failed")
                            await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{ci_pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors — the review is already approved."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=ci_orig_id,
                                fix_reason="ci_failure"
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id}")
                            else:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None")

                        if ci_task.status == TaskStatus.FAILED:
                            await _maybe_create_failure_alert(
                                ci_task, session,
                                f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
                            )
                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                   {"task_id": str(ci_task.id), "status": ci_task.status})
                    except Exception as e:
                        logger.error(f"[CI] Error checking CI for task {ci_task.id}: {e}", exc_info=True)
                        try:
                            ci_task.latest_status_message = f"CI check error: {type(e).__name__}: {e!r}"[:500]
                            session.add(ci_task)
                            await session.commit()
                        except Exception as e:
                            logger.error(f"[CI] Failed to update ci_task status after error for task {ci_task.id}: {e}", exc_info=True)
                            try:
                                await session.rollback()
                            except Exception:
                                pass

            logger.info('orchestrator_cycle_complete', extra={
                'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                'tasks_dispatched': len(queued_tasks),
                'tasks_recovered': tasks_recovered,
            })
            _orchestrator_last_cycle = _utcnow()
            _orchestrator_consecutive_errors = 0
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
                pass
            # After 3 consecutive errors, alert all projects (deduplicated)
            if _orchestrator_consecutive_errors >= 3:
                try:
                    async with AsyncSessionLocal() as alert_session:
                        await _create_orchestrator_error_alerts(alert_session, _orchestrator_consecutive_errors)
                        await alert_session.commit()
                except Exception as alert_err:
                    logger.error(f"[ORCHESTRATOR] Failed to create error alerts: {alert_err}")

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
