"""Background reaper service: detects and recovers orphaned/stale tasks."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus
from app.compute import get_provision_error, record_zone_failure, _capture_serial_for_vm, get_vm_zone, INFRA_PROVISION_PATTERNS, any_zone_available, soonest_zone_recovery
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import release_max_plan_account  # noqa: PLC0415
    from app.main import (  # noqa: PLC0415
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
    )
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.failure_classification import classify_failure  # noqa: PLC0415
    from app.broadcast import broadcast_to_project  # noqa: PLC0415
    from app.task_lifecycle import _requeue_parent_task  # noqa: PLC0415
    from app.rollup_management import _check_and_trigger_rollup  # noqa: PLC0415

    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Step 1: Read active tasks WITHOUT locks (avoids blocking heartbeat writes)
                stmt = select(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_tasks = (await session.execute(stmt)).scalars().all()

                now = _utcnow()
                five_minutes_ago = now - timedelta(minutes=5)
                four_minutes_ago = now - timedelta(minutes=4)
                ten_minutes_ago = now - timedelta(minutes=10)

                fifteen_minutes_ago = now - timedelta(minutes=15)
                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    if task.status == TaskStatus.PROVISIONING and task.started_at and task.started_at < fifteen_minutes_ago:
                        orphaned_ids.append(task.id)  # PROVISIONING timeout regardless of heartbeat
                        logger.warning(f"[REAPER] Task {task.id} stuck in PROVISIONING for >15min (started_at={task.started_at})")
                    elif task.last_heartbeat:
                        # Has heartbeat — ONLY check heartbeat freshness.
                        # Do NOT fall through to started_at — that's for tasks with no heartbeat.
                        if task.last_heartbeat < five_minutes_ago:
                            orphaned_ids.append(task.id)
                    elif task.started_at:
                        # No heartbeat ever received — startup callback should arrive within ~2 min.
                        # 4-minute threshold gives 2.5x margin for slow boots.
                        if task.started_at < four_minutes_ago:
                            orphaned_ids.append(task.id)
                    elif task.created_at < ten_minutes_ago:
                        orphaned_ids.append(task.id)

                # Step 2: Lock ONLY orphaned rows (heartbeats on healthy tasks are never blocked)
                # expire_all() nukes the identity map so step 2 MUST read fresh from DB.
                # populate_existing=True was tried but doesn't reliably refresh with
                # with_for_update(skip_locked=True) on async sessions.
                orphaned_tasks = []
                if orphaned_ids:
                    session.expire_all()
                    lock_stmt = select(Task).where(
                        Task.id.in_(orphaned_ids)
                    ).with_for_update(skip_locked=True)
                    orphaned_tasks = list((await session.execute(lock_stmt)).scalars().all())

                    # Track tasks skipped by skip_locked — attempt force-reap after 5 consecutive skips.
                    # The threshold is 5 (not 3) to give more time before force-lock attempts, since
                    # transient lock contention (e.g. a heartbeat write) often resolves within a few cycles.
                    locked_ids = set(orphaned_ids) - {t.id for t in orphaned_tasks}
                    for lid in locked_ids:
                        _reaper_skip_counts[lid] = _reaper_skip_counts.get(lid, 0) + 1
                    for t in orphaned_tasks:
                        _reaper_skip_counts.pop(t.id, None)

                    # Force-lock persistently skipped tasks using skip_locked=True (non-blocking).
                    # A task skipped here stays in _reaper_skip_counts and will be retried next cycle,
                    # so the reaper never stalls waiting on a contested row.
                    force_lock_ids = [lid for lid, count in _reaper_skip_counts.items() if count >= 5]
                    if force_lock_ids:
                        try:
                            session.expire_all()
                            # Extend lock_timeout for force-lock — 30s default is too short
                            # when competing with long-running operations. Reverts on commit/rollback.
                            from sqlalchemy import text as _text
                            await session.execute(_text("SET LOCAL lock_timeout = '60000'"))
                            force_stmt = select(Task).where(
                                Task.id.in_(force_lock_ids)
                            ).with_for_update(skip_locked=True)
                            force_locked = list((await session.execute(force_stmt)).scalars().all())
                            for t in force_locked:
                                _reaper_skip_counts.pop(t.id, None)
                                if t.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                                    continue
                                if t.last_heartbeat and t.last_heartbeat >= five_minutes_ago:
                                    continue
                                orphaned_tasks.append(t)
                                logger.warning(f"[REAPER] Force-locked persistently skipped task {t.id}")
                        except Exception as e:
                            logger.warning(f"[REAPER] Force-lock failed (lock timeout?): {e}")
                            # Reset skip counts so force-lock is retried next cycle
                            for lid in force_lock_ids:
                                _reaper_skip_counts.pop(lid, None)

                    # Clean stale entries from skip counts
                    orphaned_id_set = set(orphaned_ids)
                    for tid in list(_reaper_skip_counts.keys()):  # noqa: PERF101 — deletes from dict during iteration
                        if tid not in orphaned_id_set:
                            del _reaper_skip_counts[tid]

                    # Re-verify after acquiring lock (heartbeat may have arrived between step 1 and 2)
                    # expire_all() above ensures these objects have fresh DB values
                    now = _utcnow()  # Refresh now — time passed during lock acquisition
                    five_minutes_ago = now - timedelta(minutes=5)
                    four_minutes_ago = now - timedelta(minutes=4)
                    ten_minutes_ago = now - timedelta(minutes=10)
                    verified = []
                    for t in orphaned_tasks:
                        if t.last_heartbeat:
                            # Has heartbeat — ONLY reap if stale. Fresh heartbeat = healthy.
                            if t.last_heartbeat < five_minutes_ago:
                                verified.append(t)
                        elif t.started_at:
                            # No heartbeat — startup callback should arrive within ~2 min
                            if t.started_at < four_minutes_ago:
                                verified.append(t)
                        elif t.created_at < ten_minutes_ago:
                            verified.append(t)
                    orphaned_tasks = verified

                # Collect old VM IDs for deletion after commit (can't set attrs on SQLModel)
                _reaper_old_vms: Dict[str, str] = {}

                for task in orphaned_tasks:
                    # Re-validate status before acting — task may have completed/been cancelled
                    # between the initial query and now
                    fresh_task = await session.get(Task, task.id)
                    if fresh_task is None or fresh_task.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                        logger.info(f"[REAPER] Skipping task {task.id} — status transitioned to {fresh_task.status if fresh_task else 'gone'} before reap")
                        continue
                    task = fresh_task  # Use fresh data for all subsequent reads and writes

                    # Determine why the reaper flagged this task (use fresh_task for accurate data)
                    if fresh_task.last_heartbeat:
                        age = int((now - fresh_task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {fresh_task.last_heartbeat.isoformat()})"
                        reap_reason_code = 'heartbeat_timeout'
                    elif fresh_task.started_at:
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = f"No heartbeat received after {age}s since provisioning"
                        reap_reason_code = 'provision_timeout'
                    else:
                        age = int((now - fresh_task.created_at).total_seconds())
                        reap_reason = f"Never started — {age}s since creation, no heartbeat"
                        reap_reason_code = 'never_started'

                    task_type = (fresh_task.first_payload).get("type", "unknown")
                    logger.info('reaper_task_reaped', extra={
                        'task_id': str(task.id),
                        'task_type': task_type,
                        'reap_reason': reap_reason_code,
                        'vm_instance_id': fresh_task.vm_instance_id,
                    })

                    # Capture old VM ID before any overwrite — deletion happens after commit
                    if fresh_task.vm_instance_id:
                        _reaper_old_vms[str(task.id)] = fresh_task.vm_instance_id

                    # Release Max Plan account slot (idempotent — skip if already released)
                    acct_id = (fresh_task.context or {}).get("max_plan_account_id")
                    if acct_id and not (fresh_task.context or {}).get("_slot_released"):
                        await release_max_plan_account(acct_id)
                        task.context = {**(task.context or {}), "_slot_released": True}

                    # Separate provision failures from agent failures for retry budgeting
                    is_provision_failure = fresh_task.status == TaskStatus.PROVISIONING
                    provision_failures = (task.context or {}).get("provision_failures", 0)
                    max_provision_retries = _runtime_config["max_provision_retries"]
                    can_retry = False

                    # Rate-limited tasks: requeue without burning retry budget
                    # Check status message, error_logs, error_logs artifact, AND failure_category
                    from app.max_plan_pool import RATE_LIMIT_KEYWORDS  # noqa: PLC0415 — deferred to avoid circular import
                    msg_lower = (task.latest_status_message or "").lower()
                    error_lower = ((task.context or {}).get("last_error") or "").lower()
                    error_logs_lower = ((task.error_logs or "") if hasattr(task, 'error_logs') else "").lower()
                    is_rate_limited = (
                        (task.context or {}).get("failure_category") == "rate_limited"
                        or any(kw in msg_lower + " " + error_lower + " " + error_logs_lower
                               for kw in RATE_LIMIT_KEYWORDS)
                    )
                    if is_rate_limited:
                        rl_acct_id = (task.context or {}).get("max_plan_account_id")
                        logger.info(f"[REAPER] Task {task.id} rate-limited (account={rl_acct_id}) — requeuing without burning retry")
                        task.status = TaskStatus.QUEUED
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = f"Rate limited — requeued, waiting for account cooldown"
                        # Blacklist the exhausted account and add cooldown
                        rl_ctx = dict(task.context or {})
                        if rl_acct_id:
                            blacklisted = list(rl_ctx.get("blacklisted_accounts") or [])
                            if rl_acct_id not in blacklisted:
                                blacklisted.append(rl_acct_id)
                            rl_ctx["blacklisted_accounts"] = blacklisted
                        rl_ctx.pop("max_plan_account_id", None)
                        rl_ctx.pop("_slot_released", None)
                        # Wait 30 min before retrying — usage limits reset on longer windows
                        rl_ctx["retry_after"] = (_utcnow() + timedelta(minutes=30)).isoformat()
                        task.context = rl_ctx
                        session.add(task)
                        await broadcast_to_project(str(task.project_id), "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED})
                        continue

                    if is_provision_failure:
                        provision_failures += 1
                        prov_zone = (task.context or {}).get("provision_zone")
                        failed_acct = (task.context or {}).get("max_plan_account_id")

                        # Classify: infra-wide (all zones/quota exhausted) vs task-specific
                        provision_err = get_provision_error(fresh_task.vm_instance_id or "")
                        err_lower = ((provision_err or "") + " " + (task.latest_status_message or "")).lower()
                        is_infra = (
                            any(p in err_lower for p in INFRA_PROVISION_PATTERNS)
                            or (not prov_zone and not any_zone_available())
                        )

                        if prov_zone:
                            # VM was created (GCP API succeeded) but agent never claimed.
                            # Capture serial logs before the VM is deleted — this is the
                            # only way to diagnose agent startup crashes.
                            serial_log = None
                            vm_id = fresh_task.vm_instance_id
                            if vm_id:
                                try:
                                    serial_log = await asyncio.to_thread(_capture_serial_for_vm, vm_id, prov_zone)
                                    if serial_log:
                                        logger.warning(
                                            f"[REAPER] Serial log for dying agent {vm_id} "
                                            f"(task {task.id}):\n{serial_log[-500:]}"
                                        )
                                except Exception:
                                    pass

                            # Blacklist the account so retry uses a different one.
                            blacklisted = list((task.context or {}).get("blacklisted_accounts") or [])
                            if failed_acct and failed_acct not in blacklisted:
                                blacklisted.append(failed_acct)
                                logger.warning(
                                    f"[REAPER] Agent startup failure on {prov_zone} acct={failed_acct[:8]} "
                                    f"— blacklisting account for task {task.id}"
                                )
                            new_ctx = {
                                **(task.context or {}),
                                "blacklisted_accounts": blacklisted or None,
                            }
                            if serial_log:
                                new_ctx["last_serial_log"] = serial_log[-500:]
                            record_zone_failure(prov_zone, reason="AGENT_STARTUP_FAILURE")
                        else:
                            # No provision_zone — VM may never have been created.
                            new_ctx = dict(task.context or {})

                        if is_infra:
                            # Infrastructure-wide failure — don't burn task retry budget
                            provision_failures -= 1  # Undo the increment above
                            infra_count = new_ctx.get("provision_failures_infra", 0) + 1
                            new_ctx["provision_failures_infra"] = infra_count
                            new_ctx["provision_failures"] = provision_failures
                            new_ctx["_infra_wait"] = True
                            task.context = new_ctx
                            can_retry = True
                            logger.warning(f"[REAPER] Task {task.id} infra-wide provision failure (#{infra_count}) — requeuing without burning retry budget")
                        else:
                            new_ctx["provision_failures"] = provision_failures
                            new_ctx.pop("_infra_wait", None)
                            task.context = new_ctx
                            can_retry = provision_failures < max_provision_retries
                            if not can_retry:
                                logger.error(f"[REAPER] Task {task.id} provision failed {provision_failures} times — giving up")
                    else:
                        can_retry = task.attempt_count < _runtime_config["max_retries"]

                    if can_retry:
                        # Check if this is a research/plan parent with work children —
                        # re-evaluate instead of re-run.
                        task_type = (task.first_payload).get("type", "")
                        is_parent_type = task_type in ("research", "plan")
                        children = []
                        if is_parent_type:
                            children = (await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )).scalars().all()
                        if children:
                            logger.info(f"[REAPER] Re-evaluating parent task {task.id}")
                            await _requeue_parent_task(task, session)
                        else:
                            if not is_provision_failure:
                                task.attempt_count += 1
                            retry_label = f"provision {provision_failures}/{max_provision_retries}" if is_provision_failure else f"attempt {task.attempt_count}/{_runtime_config['max_retries']}"
                            logger.info(f"[REAPER] Retrying task {task.id} ({retry_label})")
                            reap_ctx = task.context or {}
                            reap_zone = reap_ctx.get("provision_zone", "?")
                            reap_acct = (reap_ctx.get("max_plan_account_id") or "?")[:8]
                            task.latest_status_message = f"Reaped ({retry_label}): {reap_reason} [zone={reap_zone}, acct={reap_acct}]"
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            reaper_ctx = dict(task.context or {})
                            # Clear account so dispatcher does fresh selection (respects blacklist)
                            reaper_ctx.pop("max_plan_account_id", None)
                            reaper_ctx.pop("_slot_released", None)
                            if is_provision_failure and (task.context or {}).get("_infra_wait"):
                                # Infra-wide failure: align with zone recovery
                                recovery = soonest_zone_recovery()
                                if recovery:
                                    wait_sec = max(60, (recovery - datetime.now(timezone.utc)).total_seconds() + 60)
                                    delay_minutes = min(wait_sec / 60, 30)
                                else:
                                    delay_minutes = 10 * random.uniform(0.75, 1.25)
                            elif is_provision_failure:
                                delay_minutes = 5 * random.uniform(0.75, 1.25)  # 3.75-6.25 minutes
                            else:
                                # Exponential backoff for agent failures: 1, 2, 4, 8 min capped at 15
                                delay_minutes = min(2 ** (task.attempt_count - 1), 15) * random.uniform(0.75, 1.25)
                            reaper_ctx["retry_after"] = (_utcnow() + timedelta(minutes=delay_minutes)).isoformat()
                            task.context = reaper_ctx
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status, "attempt_count": task.attempt_count}
                        )
                    else:
                        fail_reason = f"provision failed {provision_failures} times" if is_provision_failure else f"failed after {_runtime_config['max_retries']} attempts"
                        logger.error(f"[REAPER] Task {task.id} {fail_reason}.")
                        task.status = TaskStatus.FAILED
                        if is_provision_failure:
                            ctx = task.context or {}
                            provision_zone = ctx.get("provision_zone", "unknown zone")
                            blacklisted_accounts = ctx.get("blacklisted_accounts", [])
                            acct_id = ctx.get("max_plan_account_id", "no account")
                            gcp_error = provision_err or "no GCP error captured"
                            gcp_error_short = gcp_error[:200]
                            # Distinguish: VM created but agent died vs. GCP API failure
                            if provision_zone and provision_zone != "unknown zone":
                                label = "Agent startup failed"
                                detail = f"VM created in {provision_zone} but agent never claimed task"
                                if blacklisted_accounts:
                                    detail += f" (blacklisted {len(blacklisted_accounts)} account(s))"
                            else:
                                label = "Provision failed"
                                detail = f"GCP error: {gcp_error_short}"
                            task.latest_status_message = (
                                f"{label} {provision_failures}x — "
                                f"zone: {provision_zone}, acct: {acct_id}. "
                                f"{detail}. "
                                f"Last reap: {reap_reason}"
                            )
                        else:
                            task.latest_status_message = f"{fail_reason.capitalize()} — last reap: {reap_reason}"
                        record_task_result(success=False, failure_class=classify_failure(task), task_id=str(task.id))
                        session.add(task)
                        await _maybe_create_failure_alert(task, session)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.FAILED}
                        )

                if orphaned_tasks:
                    await session.commit()

                    # Delete old VMs after commit (IDs captured before overwrite)
                    for old_vm in _reaper_old_vms.values():
                        await _ensure_vm_deleted(old_vm, reason="reaper")

                    # Trigger rollup checks for failed tasks with parents (after commit)
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED and task.parent_task_id:
                            try:
                                await _check_and_trigger_rollup(task, session)
                            except Exception as e:
                                logger.error(f"[REAPER] Rollup check failed for task {task.id}: {e}", exc_info=True)

                    # Immediately unblock dependents of terminally failed tasks
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED:
                            try:
                                async with AsyncSessionLocal() as dep_session:
                                    dep_stmt = select(Task).where(
                                        Task.project_id == task.project_id,
                                        Task.status == TaskStatus.QUEUED,
                                        Task.depends_on.contains([str(task.id)]),
                                    )
                                    dep_result = await dep_session.execute(dep_stmt)
                                    waiting_tasks = dep_result.scalars().all()
                                    count = 0
                                    for wt in waiting_tasks:
                                        wt.depends_on = [d for d in (wt.depends_on or []) if d != str(task.id)]
                                        dep_session.add(wt)
                                        count += 1
                                    if count:
                                        await dep_session.commit()
                                        logger.info(f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks")
                            except Exception as e:
                                logger.error(f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}", exc_info=True)

                # Step 3: Sweep EVALUATING parents with stale unmerged children.
                # When the initial review spawn after a child completes fails silently,
                # no event re-triggers _check_and_trigger_rollup.  This sweep catches
                # those stuck parents and lets the stale-PR recovery path respawn reviews.
                try:
                    stale_cutoff = now - timedelta(minutes=15)
                    evaluating_parents = (await session.execute(
                        select(Task).where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.updated_at < stale_cutoff,
                        )
                    )).scalars().all()
                    for parent in evaluating_parents:
                        # Find a completed child to use as the trigger for _check_and_trigger_rollup
                        child = (await session.execute(
                            select(Task).where(
                                Task.parent_task_id == parent.id,
                                Task.status == TaskStatus.COMPLETED,
                            ).limit(1)
                        )).scalars().first()
                        if child:
                            try:
                                await _check_and_trigger_rollup(child, session)
                            except Exception as e:
                                logger.error(f"[REAPER] Evaluating sweep rollup failed for parent {parent.id}: {e}", exc_info=True)
                    if evaluating_parents:
                        logger.info(f"[REAPER] Swept {len(evaluating_parents)} stale EVALUATING parents")
                except Exception as e:
                    logger.error(f"[REAPER] Evaluating parent sweep error: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            try:
                await session.rollback()
            except Exception:
                pass

        # Sleep for 1 minute before checking again
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
