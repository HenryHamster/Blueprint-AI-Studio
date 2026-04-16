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
from app.shared_state import record_pipeline_event
from app.time_utils import _utcnow
from app.failure_classification import classify_failure, persist_failure_category

logger = logging.getLogger(__name__)


# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import release_max_plan_account, disable_account_model, _extract_task_model  # noqa: PLC0415
    from app.main import (  # noqa: PLC0415
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
    )
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
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
                six_minutes_ago = now - timedelta(minutes=6)
                ten_minutes_ago = now - timedelta(minutes=10)
                fifteen_minutes_ago = now - timedelta(minutes=15)

                # State-machine invariants after the clock-separation fix:
                #   - vm_provisioned_at: written by orchestrator when the VM is dispatched
                #     and when GCE confirms it's running. Never written by the agent.
                #   - last_heartbeat: written only by the agent (claim + heartbeat interval).
                #     None on a PROVISIONING task means the agent has not contacted us yet.
                #
                # Thresholds:
                #   - IN_PROGRESS + last_heartbeat > 5min ago → agent crashed mid-run (attempt budget)
                #   - PROVISIONING + vm_provisioned_at > 6min ago + no heartbeat → silent pre-claim
                #     death (provision budget). 6min gives margin over the ~90-150s nominal cold
                #     boot + auth validation + claim sequence.
                #   - PROVISIONING + started_at > 15min ago → absolute catch-all.
                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    if task.status == TaskStatus.PROVISIONING:
                        if task.started_at and task.started_at < fifteen_minutes_ago:
                            orphaned_ids.append(task.id)  # absolute provisioning ceiling
                            logger.warning(f"[REAPER] Task {task.id} stuck in PROVISIONING for >15min (started_at={task.started_at})")
                        elif task.last_heartbeat and task.last_heartbeat < five_minutes_ago:
                            # Rare: agent claimed during PROVISIONING (shouldn't happen since
                            # claim_task also flips status to IN_PROGRESS), then went silent.
                            orphaned_ids.append(task.id)
                        elif task.vm_provisioned_at and task.vm_provisioned_at < six_minutes_ago and task.last_heartbeat is None:
                            # Silent pre-claim death: VM was dispatched 6+ min ago and the
                            # agent never claimed. Charge provision budget.
                            orphaned_ids.append(task.id)
                    elif task.status == TaskStatus.IN_PROGRESS:
                        if task.last_heartbeat and task.last_heartbeat < five_minutes_ago:
                            orphaned_ids.append(task.id)
                        elif task.last_heartbeat is None and task.started_at and task.started_at < six_minutes_ago:
                            # Defensive: IN_PROGRESS without any heartbeat shouldn't happen
                            # (claim_task sets last_heartbeat at agent.py:312) but if we hit
                            # it, treat as a post-claim crash.
                            orphaned_ids.append(task.id)
                        elif task.last_heartbeat is None and task.started_at is None and task.created_at < ten_minutes_ago:
                            # Fully-degenerate IN_PROGRESS row (no heartbeat, no started_at)
                            # that's been alive for >10 min — data corruption or a path that
                            # forgot to set started_at. Reap as a catch-all.
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
                    # expire_all() above ensures these objects have fresh DB values.
                    # Mirrors the state machine above — same thresholds, same branching.
                    now = _utcnow()  # Refresh now — time passed during lock acquisition
                    five_minutes_ago = now - timedelta(minutes=5)
                    six_minutes_ago = now - timedelta(minutes=6)
                    ten_minutes_ago = now - timedelta(minutes=10)
                    fifteen_minutes_ago = now - timedelta(minutes=15)
                    verified = []
                    for t in orphaned_tasks:
                        if t.status == TaskStatus.PROVISIONING:
                            if t.started_at and t.started_at < fifteen_minutes_ago:
                                verified.append(t)
                            elif t.last_heartbeat and t.last_heartbeat < five_minutes_ago:
                                verified.append(t)
                            elif t.vm_provisioned_at and t.vm_provisioned_at < six_minutes_ago and t.last_heartbeat is None:
                                verified.append(t)
                        elif t.status == TaskStatus.IN_PROGRESS:
                            if t.last_heartbeat and t.last_heartbeat < five_minutes_ago:
                                verified.append(t)
                            elif t.last_heartbeat is None and t.started_at and t.started_at < six_minutes_ago:
                                verified.append(t)
                            elif t.last_heartbeat is None and t.started_at is None and t.created_at < ten_minutes_ago:
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

                    # Determine why the reaper flagged this task. The classification slices
                    # into three cases based on status + which clock expired first.
                    if fresh_task.status == TaskStatus.PROVISIONING and fresh_task.started_at and fresh_task.started_at < fifteen_minutes_ago:
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = f"Absolute provision timeout — {age}s since dispatch"
                        reap_reason_code = 'absolute_timeout'
                    elif fresh_task.status == TaskStatus.PROVISIONING and fresh_task.last_heartbeat is None:
                        # VM created, agent never contacted us. Use vm_provisioned_at if available,
                        # else fall back to started_at for the age display.
                        clock = fresh_task.vm_provisioned_at or fresh_task.started_at
                        age = int((now - clock).total_seconds()) if clock else 0
                        reap_reason = f"Silent pre-claim death — {age}s since VM provisioned, no heartbeat"
                        reap_reason_code = 'provision_timeout_pre_claim'
                    elif fresh_task.last_heartbeat:
                        age = int((now - fresh_task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {fresh_task.last_heartbeat.isoformat()})"
                        reap_reason_code = 'heartbeat_timeout_post_claim'
                    elif fresh_task.started_at:
                        # IN_PROGRESS without a heartbeat (defensive branch — should be rare).
                        age = int((now - fresh_task.started_at).total_seconds())
                        reap_reason = f"IN_PROGRESS without heartbeat for {age}s since dispatch"
                        reap_reason_code = 'heartbeat_timeout_post_claim'
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

                    record_pipeline_event(
                        reap_reason_code,
                        task_id=str(task.id),
                        vm_id=fresh_task.vm_instance_id,
                        zone=(task.context or {}).get("provision_zone"),
                    )

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

                    # Rate-limited tasks: requeue without burning retry budget.
                    # Delegate to classify_failure so the reaper path applies the same
                    # Anthropic-529/GitHub-429 guards as the agent-status path, and
                    # matches real Claude rate-limit strings only (RATE_LIMIT_PATTERNS).
                    combined_logs = (
                        ((task.context or {}).get("last_error") or "") + "\n"
                        + ((task.error_logs or "") if hasattr(task, 'error_logs') else "")
                    )
                    is_rate_limited = classify_failure(task, logs=combined_logs) == "RATE_LIMIT"
                    if is_rate_limited:
                        rl_acct_id = (task.context or {}).get("max_plan_account_id")
                        logger.info(f"[REAPER] Task {task.id} rate-limited (account={rl_acct_id}) — requeuing immediately with account blacklisted")
                        # Disable the exhausted model on the account so the orchestrator's
                        # blacklist pruning preserves this entry on the next dispatch cycle.
                        # Without this, the pool sees disabled_models empty, _account_available
                        # returns True, and the same account gets re-selected immediately.
                        rl_model = _extract_task_model(task)
                        if not rl_model:
                            _rl_task_type = (task.first_payload or {}).get("type", "create")
                            rl_model = _runtime_config.get("task_type_models", {}).get(_rl_task_type)
                        if rl_acct_id and rl_model:
                            try:
                                await disable_account_model(rl_acct_id, rl_model)
                            except Exception as _disable_err:
                                logger.warning(f"[REAPER] disable_account_model failed for {rl_acct_id}/{rl_model}: {_disable_err}")
                        task.status = TaskStatus.QUEUED
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = "Rate limited — requeued with exhausted account blacklisted"
                        # Blacklist the exhausted account; let the orchestrator pick another
                        # from the pool on the next cycle. If all accounts are rate-limited,
                        # the orchestrator handles that case by skipping the task and retrying
                        # next cycle — no need for a task-level retry_after delay.
                        rl_ctx = dict(task.context or {})
                        if rl_acct_id:
                            blacklisted = list(rl_ctx.get("blacklisted_accounts") or [])
                            if rl_acct_id not in blacklisted:
                                blacklisted.append(rl_acct_id)
                                record_pipeline_event(
                                    "account_blacklisted",
                                    task_id=str(task.id),
                                    vm_id=fresh_task.vm_instance_id,
                                    zone=(task.context or {}).get("provision_zone"),
                                    account_id=rl_acct_id,
                                )
                            rl_ctx["blacklisted_accounts"] = blacklisted
                        rl_ctx.pop("max_plan_account_id", None)
                        rl_ctx.pop("_slot_released", None)
                        rl_ctx.pop("retry_after", None)  # clear any stale delay
                        # Rate-limit is an account-level issue, not a provision failure —
                        # don't carry provision budget into the next dispatch.
                        rl_ctx.pop("provision_failures", None)
                        task.context = rl_ctx
                        session.add(task)
                        await broadcast_to_project(str(task.project_id), "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED})
                        continue

                    if is_provision_failure:
                        provision_failures += 1
                        # Resolve zone with a fallback chain so serial log capture works
                        # even when the in-memory _vm_zones cache has been cleared (e.g.
                        # after an orchestrator restart between provision and reap).
                        #   1. get_vm_zone() — in-memory cache, populated at provision time
                        #   2. task.context["provision_zone"] — persisted at orchestrator.py:862
                        prov_zone = None
                        if fresh_task.vm_instance_id:
                            cached = get_vm_zone(fresh_task.vm_instance_id)
                            if cached and cached != "unknown":
                                prov_zone = cached
                        if not prov_zone:
                            prov_zone = (task.context or {}).get("provision_zone")
                        failed_acct = (task.context or {}).get("max_plan_account_id")

                        # Classify: infra-wide (all zones/quota exhausted) vs task-specific
                        provision_err = get_provision_error(fresh_task.vm_instance_id or "")
                        err_lower = ((provision_err or "") + " " + (task.latest_status_message or "")).lower()
                        is_infra = (
                            any(p in err_lower for p in INFRA_PROVISION_PATTERNS)
                            or (not prov_zone and not any_zone_available())
                        )

                        serial_log = None
                        if prov_zone:
                            # VM was created (GCP API succeeded) but agent never claimed.
                            # Capture serial logs before the VM is deleted — this is the
                            # only way to diagnose agent startup crashes. Serial log is
                            # persisted to task.context so it survives in the UI/db.
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
                                    logger.exception(f"[REAPER] Failed to capture serial log for VM {vm_id}")

                            # Blacklist the account so retry uses a different one.
                            blacklisted = list((task.context or {}).get("blacklisted_accounts") or [])
                            if failed_acct and failed_acct not in blacklisted:
                                blacklisted.append(failed_acct)
                                logger.warning(
                                    f"[REAPER] Agent startup failure on {prov_zone} acct={failed_acct[:8]} "
                                    f"— blacklisting account for task {task.id}"
                                )
                                record_pipeline_event(
                                    "account_blacklisted",
                                    task_id=str(task.id),
                                    vm_id=fresh_task.vm_instance_id,
                                    zone=prov_zone,
                                    account_id=failed_acct,
                                )
                            new_ctx = {
                                **(task.context or {}),
                                "blacklisted_accounts": blacklisted or None,
                            }
                            if serial_log:
                                new_ctx["last_serial_log"] = serial_log[-500:]
                            record_zone_failure(prov_zone, reason="AGENT_STARTUP_FAILURE")
                        else:
                            # No provision_zone — VM may never have been created, or the
                            # fallback chain couldn't resolve one.
                            logger.warning(
                                f"[REAPER] Skipping serial capture for task {task.id}: "
                                f"zone unknown (vm_id={fresh_task.vm_instance_id})"
                            )
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
                            # Enrich with failure diagnosis for retry guidance
                            try:
                                from app.anomaly_detection import _error_fingerprint, _is_recurring_failure_class  # noqa: PLC0415
                                from app.task_lifecycle import _derive_failure_stage  # noqa: PLC0415
                                import app.shared_state as _anomaly_state  # noqa: PLC0415
                                _reap_failure_class = "PROVISION_FAILURE" if is_provision_failure else classify_failure(task, logs=combined_logs)
                                _fingerprint = _error_fingerprint(reap_reason)
                                reaper_ctx["_previous_error_fingerprint"] = _fingerprint
                                _stage = _derive_failure_stage(_reap_failure_class, reap_reason)
                                if _stage:
                                    reaper_ctx["_previous_failure_stage"] = _stage
                                _proj_anomalies = _anomaly_state.persistent_anomalies.get(str(task.project_id), {})
                                if _is_recurring_failure_class(_proj_anomalies, _reap_failure_class):
                                    reaper_ctx["_previous_is_recurring"] = True
                                # Set base fields so buildRetryContext renders the retry section
                                # (only for agent failures — provision failures ran no agent code)
                                if not is_provision_failure:
                                    reaper_ctx["_attempt_number"] = task.attempt_count
                                    reaper_ctx["_previous_error"] = (task.latest_status_message or "")[:1500]
                                    reaper_ctx["_previous_failure_class"] = _reap_failure_class
                                    if _reap_failure_class == "CODE_ERROR":
                                        reaper_ctx["_previous_error_summary"] = (task.latest_status_message or "")[:500]
                            except Exception:
                                logger.exception("[REAPER] Failed to enrich retry context for task %s", task.id)
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
                        extra_log_parts = [
                            (task.context or {}).get("last_error", ""),
                            (task.context or {}).get("last_serial_log", ""),
                        ]
                        if is_provision_failure:
                            extra_log_parts.append(provision_err or "")
                        extra_logs = " ".join(filter(None, extra_log_parts))
                        failure_class = classify_failure(task, logs=extra_logs or None)
                        persist_failure_category(task, failure_class)
                        record_task_result(success=False, failure_class=failure_class, task_id=str(task.id), task_type=task.first_payload.get("type", ""))
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
                                logger.warning(f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}", exc_info=True)

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
                                logger.warning(f"[REAPER] Evaluating sweep rollup failed for parent {parent.id}: {e}", exc_info=True)
                    if evaluating_parents:
                        logger.info(f"[REAPER] Swept {len(evaluating_parents)} stale EVALUATING parents")
                except Exception as e:
                    logger.warning(f"[REAPER] Evaluating parent sweep error: {e}", exc_info=True)

                # Step 4: Rescue EVALUATING parents whose children ALL terminated without
                # any completing successfully.  These parents have no COMPLETED child to
                # trigger _check_and_trigger_rollup, so they stay EVALUATING forever.
                try:
                    _terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
                    rescued = 0
                    # Batch-fetch all children for all evaluating parents in a single query
                    # to avoid an N+1 pattern (one query per parent).
                    _children_by_parent: Dict[uuid.UUID, list] = {}
                    if evaluating_parents:
                        _parent_ids = [p.id for p in evaluating_parents]
                        _all_children = (await session.execute(
                            select(Task).where(Task.parent_task_id.in_(_parent_ids))
                        )).scalars().all()
                        for _c in _all_children:
                            _children_by_parent.setdefault(_c.parent_task_id, []).append(_c)
                    for parent in evaluating_parents:
                        children = _children_by_parent.get(parent.id, [])
                        if not children:
                            continue
                        if any(c.status not in _terminal for c in children):
                            # At least one child still running — not safe to rescue yet.
                            continue
                        if any(c.status == TaskStatus.COMPLETED for c in children):
                            # Step 3 handles this; skip.
                            continue
                        # All children are terminal and none completed — rescue the parent.
                        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
                        cancelled_children = [c for c in children if c.status == TaskStatus.CANCELLED]
                        # Build a diagnostic message including child failure classes.
                        parts = []
                        if failed_children:
                            classes = [
                                (c.context or {}).get("failure_category") or "unknown"
                                for c in failed_children
                            ]
                            class_summary = ", ".join(f"{cls}" for cls in classes)
                            parts.append(f"{len(failed_children)} failed ({class_summary})")
                        if cancelled_children:
                            parts.append(f"{len(cancelled_children)} cancelled")
                        diag = "; ".join(parts) if parts else f"{len(children)} terminal"
                        parent.latest_status_message = (
                            f"All child tasks terminated without success: {diag}"
                        )
                        parent.status = TaskStatus.FAILED
                        _fc = classify_failure(parent)
                        persist_failure_category(parent, _fc)
                        session.add(parent)
                        rescued += 1
                        logger.warning(
                            f"[REAPER] Rescued stuck EVALUATING parent {parent.id}: {diag}"
                        )
                    if rescued:
                        await session.commit()
                        logger.info(f"[REAPER] Rescued {rescued} all-children-failed EVALUATING parents")
                except Exception as e:
                    logger.error(f"[REAPER] All-children-failed rescue error: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            try:
                await session.rollback()
            except Exception:
                logger.exception("[REAPER] Rollback after reaper error failed")

        # Sleep for 1 minute before checking again
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
