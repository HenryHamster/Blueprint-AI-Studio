"""Background reaper service: detects and recovers orphaned/stale tasks."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus, Project, Artifact, ArtifactType
from app.compute import get_provision_error, record_zone_failure, _capture_serial_for_vm, get_vm_zone, INFRA_PROVISION_PATTERNS, any_zone_available, soonest_zone_recovery
from app.shared_state import runtime_config as _runtime_config
from app.shared_state import record_pipeline_event, external_health as _external_health, resolve_retry_budget
from app.failure_classification import classify_failure, persist_failure_category, INFRA_CLASSES
from app.retry_budget import append_failure_class_history
from app.time_utils import _utcnow, set_retry_after, parse_retry_after, RETRY_AFTER_MAX_MINUTES

logger = logging.getLogger(__name__)


# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}


_TRANSIENT_5XX_PATTERNS = frozenset({
    "http 502", "http 503", "http 504",
    "502 bad gateway", "503 service", "504 gateway",
    "server error",
})


_ANALYSIS_ARTIFACT_KEYS = (
    ("_pr_diff_summary", "_previous_diff_summary"),
    ("_test_failure_summary", "_previous_test_failure_summary"),
    ("_self_review_outcome", "_previous_self_review_outcome"),
    ("_analysis_plan", "_previous_analysis_plan"),
)


def _copy_analysis_artifacts_to_previous(ctx: dict) -> None:
    # Mirror failed-attempt artifacts into _previous_* so the agent's buildRetryContext
    # surfaces them — matches task_lifecycle._requeue_parent_task.
    for src, dst in _ANALYSIS_ARTIFACT_KEYS:
        if ctx.get(src):
            ctx[dst] = ctx[src]


_HEARTBEAT_STALE_DEFAULTS = {
    "small": 300,
    "medium": 420,
    "large": 900,
}


_HEARTBEAT_EFFORT_FLOORS = {
    "low": 300,
    "medium": 300,
    "high": 420,
    "max": 720,
}


_PRECLAIM_STALE_DEFAULTS = {
    "small": 360,
    "medium": 480,
    "large": 600,
}


def _preclaim_stale_cutoff(task, now: datetime) -> datetime:
    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get("preclaim_stale_seconds_by_scope", _PRECLAIM_STALE_DEFAULTS)
    seconds = thresholds.get(scope, thresholds.get("small", 360))
    return now - timedelta(seconds=seconds)


def _heartbeat_stale_cutoff(task, now: datetime) -> datetime:
    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get("heartbeat_stale_seconds_by_scope", _HEARTBEAT_STALE_DEFAULTS)
    seconds = thresholds.get(scope, thresholds.get("small", 300))

    effort = ctx.get("effort") or payload.get("effort")
    if not effort:
        task_type = ctx.get("type") or payload.get("type")
        if task_type:
            # Resolve effort from the provider settings bag: merge Anthropic
            # defaults with any per-task-type override. Reaper needs a scalar
            # effort to size the heartbeat-stale floor; other providers'
            # effort-like knobs (reasoning_effort, thinking_budget) aren't
            # normalized so we look at Anthropic only, which is the dispatch
            # default.
            defaults = _runtime_config.get("provider_settings_defaults", {}).get("anthropic", {})
            overrides = (
                _runtime_config.get("task_type_provider_overrides", {})
                .get(task_type, {})
                .get("anthropic", {})
            )
            effort = overrides.get("effort") or defaults.get("effort")
    floor = _HEARTBEAT_EFFORT_FLOORS.get(effort, 0)
    seconds = max(seconds, floor)
    return now - timedelta(seconds=seconds)


async def _task_has_open_pr(task, session) -> tuple[bool, int | None]:
    """Check whether a task already published a PR that's still open.

    Used by the reaper: a task that pushed a branch and opened a PR before
    being reaped should hand off to the review pipeline (EVALUATING) rather
    than retry from scratch (QUEUED) — the retry path would collide with the
    existing branch and be refused by the agent's stale-branch guard
    (agent/src/index.ts:706), orphaning the PR.

    Source of truth is the Artifact table: the backend writes a
    PULL_REQUEST artifact atomically with PR creation (see
    routers/agent.py:2035). Returns (True, pr_number) on match.
    """
    stmt = select(Artifact).where(
        Artifact.task_id == task.id,
        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
    )
    artifact = (await session.execute(stmt)).scalars().first()
    if artifact is None or not artifact.pr_url:
        return False, None
    # Parse PR number from the URL (e.g. https://github.com/owner/repo/pull/2903)
    try:
        pr_num = int(artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return False, None
    return True, pr_num


async def _sweep_orphan_direction_holds(session) -> int:
    """Release direction holds whose direction_id no longer maps to a live direction.

    A direction is "live" if it exists in some project's directions JSONB with
    status in (active, paused, proposed). completed/expired/rejected directions
    shouldn't hold tasks — and archived directions that disappear entirely
    definitely shouldn't. Returns the number of holds cleared.
    """
    from app.shared_state import (  # noqa: PLC0415
        direction_failure_streaks,
        clear_direction_hold,
    )
    live_dids: set[str] = set()
    projects = (await session.execute(select(Project))).scalars().all()
    for proj in projects:
        for d in (proj.directions or []):
            if d.get("status") in ("active", "paused", "proposed"):
                did_val = d.get("id")
                if did_val:
                    live_dids.add(did_val)
    cleared = 0
    for did, streak in list(direction_failure_streaks.items()):
        if not streak.get("held"):
            continue
        if did not in live_dids:
            clear_direction_hold(did)
            cleared += 1
            logger.info(
                f"[REAPER] Cleared hold on orphan direction {did} "
                "(no longer live in any project's directions)"
            )
    if cleared:
        logger.info(f"[REAPER] Cleared {cleared} orphan direction hold(s)")
    return cleared


async def _sweep_stale_residual_cases(session) -> int:
    """Close ACTIVE residual cases whose last_seen_at is older than 72 hours.

    When a CODE_ERROR fingerprint stops occurring (infra recovers, an unrelated
    PR fixes it, etc.) the ResidualRemediationCase row stays ACTIVE forever
    without this sweep. Returns the number of cases transitioned to
    CLOSED_ORGANICALLY.
    """
    from app.models import ResidualRemediationCase, ResidualCaseStatus  # noqa: PLC0415
    from app.failure_classification import _mark_residual_case_closed  # noqa: PLC0415

    stale_cutoff = _utcnow() - timedelta(hours=72)
    stale_cases = (await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.status == ResidualCaseStatus.ACTIVE,
            ResidualRemediationCase.last_seen_at < stale_cutoff,
        )
    )).scalars().all()

    closed = 0
    for case in stale_cases:
        case.status = ResidualCaseStatus.CLOSED_ORGANICALLY
        case.updated_at = _utcnow()
        session.add(case)
        _mark_residual_case_closed(case.fingerprint)
        closed += 1
        logger.info(
            "[REAPER] Closed stale residual case %s (fingerprint=%r, last_seen_at=%s)",
            case.id, case.fingerprint, case.last_seen_at.isoformat(),
        )

    if closed:
        await session.commit()
        logger.info("[REAPER] Closed %d stale residual remediation case(s)", closed)
    return closed


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import (  # noqa: PLC0415
        clear_task_llm_runtime_context,
        disable_account_model,
        get_task_llm_account_id,
        _extract_task_model,
        release_max_plan_account,
    )
    from app.main import (  # noqa: PLC0415
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
    )
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    from app.broadcast import broadcast_to_project  # noqa: PLC0415
    from app.task_lifecycle import _requeue_parent_task  # noqa: PLC0415
    from app.rollup_management import _check_and_trigger_rollup  # noqa: PLC0415

    consecutive_errors = 0
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Step 1: Read active tasks WITHOUT locks (avoids blocking heartbeat writes)
                stmt = select(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_tasks = (await session.execute(stmt)).scalars().all()

                now = _utcnow()
                ten_minutes_ago = now - timedelta(minutes=10)
                fifteen_minutes_ago = now - timedelta(minutes=15)

                # State-machine invariants after the clock-separation fix:
                #   - vm_provisioned_at: written by orchestrator when the VM is dispatched
                #     and when GCE confirms it's running. Never written by the agent.
                #   - last_heartbeat: written only by the agent (claim + heartbeat interval).
                #     None on a PROVISIONING task means the agent has not contacted us yet.
                #
                # Thresholds:
                #   - IN_PROGRESS + stale heartbeat → agent crashed mid-run (attempt budget).
                #     Stale cutoff is scope-aware: 5min (small) / 7min (medium) / 10min (large).
                #   - PROVISIONING + vm_provisioned_at > 6min ago + no heartbeat → silent pre-claim
                #     death (provision budget). 6min gives margin over the ~90-150s nominal cold
                #     boot + auth validation + claim sequence.
                #   - PROVISIONING + started_at > 15min ago → absolute catch-all.
                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    hb_cutoff = _heartbeat_stale_cutoff(task, now)
                    if task.status == TaskStatus.PROVISIONING:
                        if task.started_at and task.started_at < fifteen_minutes_ago:
                            orphaned_ids.append(task.id)  # absolute provisioning ceiling
                            logger.warning(f"[REAPER] Task {task.id} stuck in PROVISIONING for >15min (started_at={task.started_at})")
                        elif task.last_heartbeat and task.last_heartbeat < hb_cutoff:
                            # Rare: agent claimed during PROVISIONING (shouldn't happen since
                            # claim_task also flips status to IN_PROGRESS), then went silent.
                            orphaned_ids.append(task.id)
                        elif task.vm_provisioned_at and task.vm_provisioned_at < _preclaim_stale_cutoff(task, now) and task.last_heartbeat is None:
                            # Silent pre-claim death: VM was dispatched long enough ago
                            # (scope-aware: small 6min, medium 8min, large 10min) and the
                            # agent never claimed. Charge provision budget.
                            orphaned_ids.append(task.id)
                    elif task.status == TaskStatus.IN_PROGRESS:
                        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
                            orphaned_ids.append(task.id)
                        elif task.last_heartbeat is None and task.started_at and task.started_at < hb_cutoff:
                            # Defensive: IN_PROGRESS without any heartbeat shouldn't happen
                            # (claim_task sets last_heartbeat at agent.py:312) but if we hit
                            # it, treat as a post-claim crash. Use scope-aware cutoff so
                            # large/high-effort tasks aren't reaped prematurely.
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
                                if t.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                                    continue
                                if t.last_heartbeat and t.last_heartbeat >= _heartbeat_stale_cutoff(t, now):
                                    continue
                                _reaper_skip_counts.pop(t.id, None)
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
                    ten_minutes_ago = now - timedelta(minutes=10)
                    fifteen_minutes_ago = now - timedelta(minutes=15)
                    verified = []
                    for t in orphaned_tasks:
                        hb_cutoff = _heartbeat_stale_cutoff(t, now)
                        if t.status == TaskStatus.PROVISIONING:
                            if t.started_at and t.started_at < fifteen_minutes_ago:
                                verified.append(t)
                            elif t.last_heartbeat and t.last_heartbeat < hb_cutoff:
                                verified.append(t)
                            elif t.vm_provisioned_at and t.vm_provisioned_at < _preclaim_stale_cutoff(t, now) and t.last_heartbeat is None:
                                verified.append(t)
                        elif t.status == TaskStatus.IN_PROGRESS:
                            if t.last_heartbeat and t.last_heartbeat < hb_cutoff:
                                verified.append(t)
                            elif t.last_heartbeat is None and t.started_at and t.started_at < hb_cutoff:
                                verified.append(t)
                            elif t.last_heartbeat is None and t.started_at is None and t.created_at < ten_minutes_ago:
                                verified.append(t)
                        elif t.created_at < ten_minutes_ago:
                            verified.append(t)
                    orphaned_tasks = verified

                # Collect old VM IDs for deletion after commit (can't set attrs on SQLModel)
                _reaper_old_vms: Dict[str, str] = {}
                # Audit 2026-04-25 A8: defer parent re-queue to AFTER session.commit()
                # so the orphan-row FOR UPDATE lock is released first. The previous
                # version called _requeue_parent_task (which clones children + remaps
                # deps + does its own queries) WHILE holding force-locked rows, which
                # starved agent heartbeats with 429s and triggered cascading false
                # orphan detection on the next reaper cycle.
                _deferred_parent_requeues: list[Task] = []

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
                    acct_id = get_task_llm_account_id(fresh_task)
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
                        rl_acct_id = get_task_llm_account_id(task)
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
                            # classify_failure() wrote the structured signal into task.context
                            # on the line above; pass it through so reaper-path cooldowns honor
                            # retry hints and quota-exhausted flat curves the same way.
                            _reaper_ctx = task.context or {}
                            _reaper_kwargs = {
                                "retry_after_seconds": _reaper_ctx.get("rate_limit_retry_after_seconds"),
                                "kind": _reaper_ctx.get("rate_limit_kind") or "rate_limit",
                            }
                            try:
                                await disable_account_model(rl_acct_id, rl_model, **_reaper_kwargs)
                            except Exception as _disable_err:
                                logger.warning(f"[REAPER] disable_account_model failed for {rl_acct_id}/{rl_model}: {_disable_err}; retrying in 1s")
                                await asyncio.sleep(1)
                                try:
                                    await disable_account_model(rl_acct_id, rl_model, **_reaper_kwargs)
                                except Exception as _disable_err2:
                                    logger.error(
                                        f"[REAPER] disable_account_model retry also failed for {rl_acct_id}/{rl_model}: {_disable_err2}. "
                                        "Account remains active in pool — tight re-selection loop risk."
                                    )
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
                        _copy_analysis_artifacts_to_previous(rl_ctx)
                        rl_ctx = clear_task_llm_runtime_context(rl_ctx)
                        rl_ctx.pop("_slot_released", None)
                        # Audit W1: retry_after_until is a column now.
                        task.retry_after_until = None
                        # Rate-limit is an account-level issue, not a provision failure —
                        # don't carry provision budget into the next dispatch.
                        rl_ctx.pop("provision_failures", None)
                        task.context = rl_ctx
                        session.add(task)
                        await broadcast_to_project(str(task.project_id), "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED})
                        continue

                    # Track per-class budget/failure-class for non-provision retries.
                    # Initialized here so both retry and fail paths can reference them.
                    reap_failure_class: str | None = None
                    class_budget: int = _runtime_config["max_retries"]

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
                        failed_acct = get_task_llm_account_id(task)

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
                                            f"(task {task.id}):\n{serial_log}"
                                        )
                                except Exception:
                                    logger.exception(f"[REAPER] Failed to capture serial log for VM {vm_id}")

                            # Blacklist the account so retry uses a different one.
                            # We maintain two lists:
                            #   - blacklisted_accounts: pruned when model cooldown expires
                            #     (correct for rate-limit blacklists)
                            #   - provision_failed_accounts: sticky for this task's lifetime
                            #     (silent pre-claim death isn't model-cooldown-driven, so
                            #     pruning by _account_available_for_model wrongly reselects
                            #     the broken account — observed on task 812fdbb9 retrying
                            #     account 017d51cb 3x in a row in us-central1-a).
                            blacklisted = list((task.context or {}).get("blacklisted_accounts") or [])
                            sticky = list((task.context or {}).get("provision_failed_accounts") or [])
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
                            if failed_acct and failed_acct not in sticky:
                                sticky.append(failed_acct)
                            new_ctx = {
                                **(task.context or {}),
                                "blacklisted_accounts": blacklisted or None,
                                "provision_failed_accounts": sticky or None,
                            }
                            if serial_log:
                                new_ctx["last_serial_log"] = serial_log
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
                        # Classify failure to select per-class retry budget.
                        # Use agent-reported error logs as the primary signal. If the agent
                        # left no error context (pure heartbeat timeout), fall back to
                        # AGENT_TIMEOUT rather than the default CODE_ERROR — a silent death
                        # is not a code defect.
                        reap_failure_class = classify_failure(task, logs=combined_logs)
                        if reap_failure_class == "CODE_ERROR" and not combined_logs.strip():
                            reap_failure_class = "AGENT_TIMEOUT"
                        task_scope = (task.context or {}).get("scope")
                        class_budget = resolve_retry_budget(reap_failure_class, task_scope)
                        # Detect EXTERNAL_API fast-path: transient 5xx where the dependency
                        # health check shows recovery. These failures skip the attempt_count
                        # increment below (mirrors the RATE_LIMIT fast-path) since the retry
                        # is near-instant and not indicative of a code defect. Applies whether
                        # or not the task already has a PR — a transient 502 after pushing
                        # work should not burn a retry attempt if the dependency recovered.
                        _err_blob = (combined_logs + " " + (task.latest_status_message or "")).lower()
                        _transient_5xx = any(p in _err_blob for p in _TRANSIENT_5XX_PATTERNS)
                        _gh_consec = _external_health.get("consecutive_failures", {}).get("github_api", 0)
                        ext_api_fast_path = (
                            reap_failure_class == "EXTERNAL_API"
                            and _transient_5xx
                            and _gh_consec == 0
                        )
                        can_retry = task.attempt_count < class_budget

                    # Open-PR handoff: if this attempt already pushed a branch
                    # and opened a PR, retrying from scratch would collide with
                    # the existing branch (see agent/src/index.ts:706 stale-
                    # branch guard) and orphan the PR. Hand off to the review
                    # pipeline instead — review/fix iteration + the existing
                    # 6h EVALUATING hard timeout (services.py:807) + the 16-iter
                    # review-fix cap guarantee termination.
                    _has_open_pr, _pr_num = await _task_has_open_pr(task, session)
                    if _has_open_pr and _pr_num:
                        # Provision failures can't publish a PR (agent never ran),
                        # so class_budget/reap_failure_class are only bound when
                        # the agent-error branch above ran. Guard the format args.
                        _display_class = (
                            "PROVISION_FAILURE" if is_provision_failure
                            else locals().get("reap_failure_class", "UNKNOWN")
                        )
                        _display_budget = locals().get("class_budget", "?")
                        logger.info(
                            f"[REAPER] Task {task.id} reaped mid-run with open PR #{_pr_num}; "
                            f"handing off to review pipeline (was attempt {task.attempt_count}/{_display_budget})"
                        )
                        task.status = TaskStatus.EVALUATING
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        task.latest_status_message = (
                            f"Reaped mid-run with open PR #{_pr_num}; "
                            f"handed off to review pipeline ({_display_class})"
                        )
                        session.add(task)
                        # Spawn the review task that _process_post_commit would
                        # have spawned if the agent had reported COMPLETED.
                        try:
                            from app.review_pipeline import _spawn_pipeline_task  # noqa: PLC0415
                            _short_id = str(task.id).replace("-", "")[:8]
                            _task_type = (task.first_payload or {}).get("type", "") or ""
                            _type_prefix_map = {
                                "docs": "docs", "deps": "deps", "migrate": "migrate",
                                "refactor": "refactor", "perf": "task", "qa": "task",
                                "security_audit": "task", "debug": "task", "smoketest": "task",
                            }
                            _branch_prefix = _type_prefix_map.get(_task_type, "task")
                            _branch = f"blueprint/{_branch_prefix}-{_short_id}"
                            _base_branch = (task.first_payload or {}).get("base_branch", "main")
                            _original_prompt = (task.first_payload or {}).get("prompt", task.description or "")
                            await _spawn_pipeline_task(
                                task, "review", session, _pr_num, _branch, _base_branch,
                                original_prompt=_original_prompt, original_task_id=str(task.id),
                            )
                        except Exception as _spawn_exc:
                            # Review spawn is best-effort from the reaper — if it
                            # fails, reaper Step 3 (stale EVALUATING sweep) will
                            # retry within 15 min, and the 6h hard timeout still
                            # bounds the EVALUATING state. Don't let this crash
                            # the reaper cycle.
                            logger.warning(
                                f"[REAPER] Review spawn failed for reaped-with-PR task {task.id}: "
                                f"{type(_spawn_exc).__name__}: {_spawn_exc}",
                                exc_info=True,
                            )
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status}
                        )
                    elif can_retry:
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
                            # Audit A8: queue for post-commit processing; don't
                            # block heartbeats on this task while we clone children.
                            # Mark requeue_in_flight so the next reaper cycle (and
                            # the orchestrator dispatch) skips this task while we work.
                            logger.info(f"[REAPER] Deferring re-evaluation of parent task {task.id} until after lock release")
                            task.requeue_in_flight = True
                            session.add(task)
                            _deferred_parent_requeues.append(task)
                        else:
                            if not is_provision_failure and not ext_api_fast_path:
                                task.attempt_count += 1
                            if is_provision_failure:
                                retry_label = f"provision {provision_failures}/{max_provision_retries}"
                            else:
                                retry_label = f"attempt {task.attempt_count}/{class_budget} [{reap_failure_class}]"
                            logger.info(f"[REAPER] Retrying task {task.id} ({retry_label})")
                            reap_ctx = task.context or {}
                            reap_zone = reap_ctx.get("provision_zone", "?")
                            reap_acct = (get_task_llm_account_id(reap_ctx) or "?")[:8]
                            task.latest_status_message = f"Reaped ({retry_label}): {reap_reason} [zone={reap_zone}, acct={reap_acct}]"
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            reaper_ctx = dict(task.context or {})
                            # Clear account so dispatcher does fresh selection (respects blacklist)
                            reaper_ctx = clear_task_llm_runtime_context(reaper_ctx)
                            reaper_ctx.pop("_slot_released", None)
                            # Enrich with failure diagnosis for retry guidance
                            try:
                                from app.anomaly_detection import _error_fingerprint, _is_recurring_failure_class  # noqa: PLC0415
                                from app.task_lifecycle import _derive_failure_stage  # noqa: PLC0415
                                import app.shared_state as _anomaly_state  # noqa: PLC0415
                                _reap_failure_class = "PROVISION_FAILURE" if is_provision_failure else classify_failure(task, logs=combined_logs)
                                _fingerprint = _error_fingerprint(reap_reason)
                                # Audit W1: previous_error_fingerprint is a column.
                                task.previous_error_fingerprint = _fingerprint
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
                                    reaper_ctx["_max_attempts"] = class_budget
                                    reaper_ctx["_previous_error"] = (task.latest_status_message or "")[:1500]
                                    reaper_ctx["_previous_failure_class"] = _reap_failure_class
                                    reaper_ctx["_failure_class_history"] = append_failure_class_history(
                                        reaper_ctx.get("_failure_class_history"),
                                        _reap_failure_class,
                                    )
                                    if _reap_failure_class == "CODE_ERROR":
                                        reaper_ctx["_previous_error_summary"] = (task.latest_status_message or "")[:500]
                                    _copy_analysis_artifacts_to_previous(reaper_ctx)
                            except Exception:
                                logger.exception("[REAPER] Failed to enrich retry context for task %s", task.id)
                            if not is_provision_failure:
                                try:
                                    from app.task_lifecycle import _build_retry_prompt_suffix  # noqa: PLC0415
                                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                    _task_scope = (
                                        reaper_ctx.get("scope")
                                        or (task.first_payload or {}).get("scope")
                                        or "small"
                                    )
                                    _prior_error = (
                                        (task.context or {}).get("last_error")
                                        or reaper_ctx.get("_previous_error")
                                        or ""
                                    )
                                    _extra_guidance, _scope_override = _build_retry_prompt_suffix(
                                        reap_failure_class, _prior_error, task.attempt_count, _task_scope, reaper_ctx,
                                    )
                                    if _scope_override:
                                        reaper_ctx["scope"] = _scope_override
                                        reaper_ctx["_scope_reduced"] = True
                                    if _extra_guidance and task.payloads:
                                        _new_payloads = list(task.payloads)
                                        if isinstance(_new_payloads[0], dict):
                                            _p0 = dict(_new_payloads[0])
                                            _existing_suffix = _p0.get("prompt_suffix", "") or ""
                                            _p0["prompt_suffix"] = _extra_guidance + (
                                                "\n" + _existing_suffix if _existing_suffix else ""
                                            )
                                            _new_payloads[0] = _p0
                                            task.payloads = _new_payloads
                                            flag_modified(task, "payloads")
                                except Exception:
                                    logger.exception("[REAPER] Failed to inject retry guidance for task %s", task.id)
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
                            elif reap_failure_class == "EXTERNAL_API":
                                # Adaptive delay: transient HTTP 5xx typically resolves in 1-3
                                # minutes, so don't wait 5+ minutes on first retry. Sustained
                                # outages fall through to [5,15,30,60]. Non-transient errors
                                # (DNS, TLS, context_length_exceeded) indicate deeper problems
                                # and always use the longer sequence.
                                _err_blob = (combined_logs + " " + (task.latest_status_message or "")).lower()
                                _transient_5xx = any(p in _err_blob for p in _TRANSIENT_5XX_PATTERNS)
                                _gh_consec = _external_health.get("consecutive_failures", {}).get("github_api", 0)
                                if _transient_5xx and _gh_consec == 0:
                                    # Dependency already recovered — requeue immediately.
                                    delay_minutes = 0
                                elif _transient_5xx and task.attempt_count == 1:
                                    # First-attempt short delay for transient 5xx.
                                    delay_minutes = 1.5 * random.uniform(0.75, 1.25)
                                elif _transient_5xx:
                                    # Subsequent retries for transient 5xx: [5, 15, 30].
                                    _t5xx_delays = [5, 15, 30]
                                    _idx = min(task.attempt_count - 2, len(_t5xx_delays) - 1)
                                    delay_minutes = _t5xx_delays[_idx] * random.uniform(0.75, 1.25)
                                else:
                                    # Non-transient (DNS, TLS, context_length_exceeded) — use
                                    # the longer sequence since these indicate deeper problems.
                                    _ext_api_delays = [5, 15, 30, 60]
                                    _attempt_idx = min(task.attempt_count - 1, len(_ext_api_delays) - 1)
                                    delay_minutes = _ext_api_delays[_attempt_idx] * random.uniform(0.75, 1.25)
                            else:
                                # Exponential backoff for agent failures: 1, 2, 4, 8 min capped at 15
                                delay_minutes = min(2 ** (task.attempt_count - 1), 15) * random.uniform(0.75, 1.25)
                            task.context = reaper_ctx
                            set_retry_after(task, delay_minutes)
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status, "attempt_count": task.attempt_count}
                        )
                    else:
                        if is_provision_failure:
                            fail_reason = f"provision failed {provision_failures} times"
                        else:
                            fail_reason = f"failed after {class_budget} retries ({reap_failure_class})"
                        logger.error(f"[REAPER] Task {task.id} {fail_reason}.")
                        task.status = TaskStatus.FAILED
                        if is_provision_failure:
                            ctx = task.context or {}
                            provision_zone = ctx.get("provision_zone", "unknown zone")
                            blacklisted_accounts = ctx.get("blacklisted_accounts", []) or []
                            sticky_accounts = ctx.get("provision_failed_accounts", []) or []
                            all_blocked = list({*blacklisted_accounts, *sticky_accounts})
                            acct_id = get_task_llm_account_id(ctx) or "no account"
                            gcp_error = provision_err or "no GCP error captured"
                            gcp_error_short = gcp_error[:200]
                            # Distinguish: VM created but agent died vs. GCP API failure
                            if provision_zone and provision_zone != "unknown zone":
                                label = "Agent startup failed"
                                detail = f"VM created in {provision_zone} but agent never claimed task"
                                if all_blocked:
                                    detail += f" (blacklisted {len(all_blocked)} account(s))"
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
                        persist_failure_category(task, failure_class, logs=extra_logs or None)
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

                    # Audit A8: now that the orphan-row lock is released, do the
                    # slow parent-requeue work (child clone + dep remap). Heartbeats
                    # on the parent task will go through fine since we only set
                    # requeue_in_flight (a marker), not a row lock.
                    for parent in _deferred_parent_requeues:
                        try:
                            await _requeue_parent_task(parent, session)
                        except Exception as e:
                            logger.error(
                                f"[REAPER] Deferred parent requeue failed for {parent.id}: {e}",
                                exc_info=True,
                            )
                        finally:
                            # Always clear the in-flight marker so subsequent reaper
                            # cycles can re-evaluate if requeue itself failed.
                            parent.requeue_in_flight = False
                            session.add(parent)
                    if _deferred_parent_requeues:
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

                    # Close open PRs owned by terminally-failed fix tasks. When a
                    # fix exhausts retries, its PR stays OPEN on GitHub, and
                    # sibling tasks whose rollup waits on open PRs targeting the
                    # same research branch block forever. Closing the dead PR
                    # removes it from the sibling rollup-wait gate.
                    for task in orphaned_tasks:
                        if task.status != TaskStatus.FAILED:
                            continue
                        payload = task.first_payload if task.payloads else {}
                        if payload.get("type") != "fix":
                            continue
                        pr_number = (
                            (task.context or {}).get("review_loop", {}).get("pr_number")
                            or payload.get("pr_number")
                        )
                        if not pr_number:
                            continue
                        try:
                            from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
                            from app.github import close_pull_request, create_pr_comment  # noqa: PLC0415
                            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                            reason = task.latest_status_message or "fix task reached terminal failure"
                            try:
                                await create_pr_comment(
                                    gh_token, owner, repo, int(pr_number),
                                    f":warning: Abandoning this PR — fix task `{str(task.id)[:8]}` reached terminal failure.\n\n"
                                    f"{reason[:400]}\n\n"
                                    f"Closing so sibling rollups aren't blocked waiting for this branch.",
                                )
                            except Exception as ce:
                                logger.warning(f"[PIPELINE] Failed to comment on dead PR #{pr_number}: {ce}")
                            closed = await close_pull_request(gh_token, owner, repo, int(pr_number), project_id=str(task.project_id))
                            if closed:
                                logger.info(f"[PIPELINE] Closed PR #{pr_number} after fix task {task.id} terminal-failed")
                            else:
                                logger.warning(f"[PIPELINE] close_pull_request returned False for PR #{pr_number}")
                        except Exception as e:
                            logger.warning(f"[PIPELINE] Failed to close PR for failed fix task {task.id}: {e}", exc_info=True)

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
                        # All children are terminal and none completed — attempt re-evaluation
                        # via _requeue_parent_task which clones failed children with retry
                        # guidance (up to MAX_PARENT_REQUEUES attempts).  If attempts are
                        # exhausted it marks the parent FAILED and fires a failure alert.
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
                        session.add(parent)
                        await _requeue_parent_task(parent, session)
                        rescued += 1
                        logger.warning(
                            f"[REAPER] Rescued stuck EVALUATING parent {parent.id}: {diag}"
                        )
                    if rescued:
                        await session.commit()
                        logger.info(f"[REAPER] Rescued {rescued} all-children-failed EVALUATING parents")
                except Exception as e:
                    logger.error(f"[REAPER] All-children-failed rescue error: {e}", exc_info=True)

                # Step 5: Infrastructure recovery sweep — resurrect recently-failed tasks
                # that exhausted their retry budget due to transient infrastructure failures
                # once the specific root cause has resolved.  Conservative: max 1 resurrection
                # per task, only for confirmed infra failure classes, only when infra is healthy.
                try:
                    from app.max_plan_pool import _all_accounts, _account_available_for_model  # noqa: PLC0415
                    _infra_lookback = now - timedelta(hours=2)
                    # Exclude just-reaped tasks so Step 2 failures aren't immediately
                    # resurrected in the same cycle, defeating the retry budget.
                    _just_reaped_ids = {t.id for t in orphaned_tasks}
                    _infra_query = select(Task).where(
                        Task.status == TaskStatus.FAILED,
                        Task.updated_at >= _infra_lookback,
                    )
                    if _just_reaped_ids:
                        _infra_query = _infra_query.where(Task.id.notin_(_just_reaped_ids))
                    _infra_failed = (await session.execute(_infra_query)).scalars().all()

                    # Parent tasks (those with children) must not be resurrected as leaf
                    # dispatches — they only fail via _requeue_parent_task after exhausting
                    # the re-evaluation budget, and resurrecting them back to QUEUED would
                    # cause the orchestrator to dispatch a research/plan parent as if it
                    # were a work task. Parents recover through Step 4 (which re-clones
                    # failed children), not through Step 5. Batch-fetch parent IDs that
                    # have any children to skip them here.
                    _parent_ids_with_children: set[uuid.UUID] = set()
                    if _infra_failed:
                        _failed_ids = [_t.id for _t in _infra_failed]
                        _child_rows = (await session.execute(
                            select(Task.parent_task_id).where(
                                Task.parent_task_id.in_(_failed_ids)
                            ).distinct()
                        )).scalars().all()
                        _parent_ids_with_children = {pid for pid in _child_rows if pid is not None}

                    _resurrected = 0
                    for _t in _infra_failed:
                        _ctx = _t.context or {}
                        # Only resurrect infra-class failures, and only once per task.
                        _fail_cat = _ctx.get("failure_category", "")
                        if _fail_cat not in INFRA_CLASSES:
                            continue
                        if _ctx.get("_resurrection_count", 0) >= 1:
                            continue
                        # Skip parents — they must re-evaluate via _requeue_parent_task, not
                        # resurrect as leaf dispatches.
                        if _t.id in _parent_ids_with_children:
                            continue

                        # Check that the specific infrastructure issue has resolved.
                        _infra_ready = False
                        if _fail_cat == "PROVISION_FAILURE":
                            _infra_ready = any_zone_available()
                        elif _fail_cat == "RATE_LIMIT":
                            # Need at least one available account for this task's model
                            # that isn't already blacklisted by this task.
                            _task_model = _extract_task_model(_t)
                            _task_type = (_t.first_payload or {}).get("type", "create")
                            if not _task_model:
                                _task_model = _runtime_config.get("task_type_models", {}).get(_task_type)
                            _blacklisted = set(_ctx.get("blacklisted_accounts") or [])
                            _accounts = _all_accounts()
                            _infra_ready = any(
                                acct.get("id") not in _blacklisted
                                and _account_available_for_model(acct, _task_model)
                                for acct in _accounts
                            )
                        elif _fail_cat == "EXTERNAL_API":
                            _infra_ready = (
                                _external_health.get("github_api", False)
                                and _external_health.get("dispatch_api", False)
                            )
                        elif _fail_cat == "AGENT_TIMEOUT":
                            # Process crashes and heartbeat timeouts are transient per-VM
                            # issues. OOM kills are NOT transient — the task scope hasn't
                            # changed and it will OOM again on the same VM type. Skip
                            # resurrection for OOM; let scope reduction handle it.
                            if _ctx.get("timeout_subtype") == "oom":
                                continue
                            _infra_ready = any_zone_available()

                        if not _infra_ready:
                            continue

                        # Resurrect: reset to QUEUED with a clean slate.
                        logger.info(
                            f"[REAPER] Resurrecting infra-failed task {_t.id} "
                            f"(failure_category={_fail_cat}) — infrastructure recovered"
                        )
                        _new_ctx = dict(_ctx)
                        _new_ctx["_resurrection_count"] = _ctx.get("_resurrection_count", 0) + 1
                        _new_ctx["_resurrected_from"] = _fail_cat
                        # Clear all infra retry state so the task runs like a fresh dispatch.
                        _new_ctx.pop("provision_failures", None)
                        _new_ctx.pop("provision_failures_infra", None)
                        _new_ctx.pop("_infra_wait", None)
                        _new_ctx.pop("blacklisted_accounts", None)
                        _new_ctx.pop("provision_failed_accounts", None)
                        _new_ctx = clear_task_llm_runtime_context(_new_ctx)
                        _new_ctx.pop("_slot_released", None)
                        _t.retry_after_until = None  # audit W1: column source of truth
                        _new_ctx.pop("failure_category", None)
                        # Clear EXTERNAL_API retry counter: a resurrected task that hits another
                        # API error would otherwise see stale _api_failure_retries > 5 and
                        # bypass the EXTERNAL_API-specific backoff, falling through to the
                        # general retry path with a depleted budget.
                        _new_ctx.pop("_api_failure_retries", None)
                        _t.context = _new_ctx
                        _t.attempt_count = 0
                        _t.status = TaskStatus.QUEUED
                        _t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        _t.last_heartbeat = None
                        _t.started_at = None
                        _t.latest_status_message = (
                            f"Auto-resurrected after infrastructure recovery "
                            f"(was: {_fail_cat})"
                        )
                        session.add(_t)

                        # INFO alert so the user knows recovery happened.
                        try:
                            from app.models import Alert, AlertSeverity  # noqa: PLC0415
                            _task_type_label = (_t.first_payload or {}).get("type", "unknown")
                            _res_alert = Alert(
                                project_id=_t.project_id,
                                severity=AlertSeverity.INFO,
                                title=f"Task auto-resurrected: {_t.title or str(_t.id)[:8]}",
                                message=(
                                    f"Task type: {_task_type_label}. "
                                    f"Infrastructure failure ({_fail_cat}) resolved — "
                                    f"task re-queued automatically."
                                ),
                                task_id=_t.id,
                            )
                            session.add(_res_alert)
                            await broadcast_to_project(
                                str(_t.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(_res_alert.id),
                                    "severity": "INFO",
                                    "title": _res_alert.title,
                                },
                            )
                        except Exception as _alert_err:
                            logger.warning(f"[REAPER] Resurrection alert failed for {_t.id}: {_alert_err}")

                        _resurrected += 1

                    if _resurrected:
                        await session.commit()
                        logger.info(f"[REAPER] Resurrected {_resurrected} infra-failed task(s)")

                except Exception as e:
                    logger.error(f"[REAPER] Infra recovery sweep error: {e}", exc_info=True)

                # Step 5a: Clear infra holds on directions whose root-cause signal has recovered.
                # Mirrors the signal checks in Step 5 but applies to direction-level holds
                # in direction_failure_streaks rather than individual task resurrection.
                try:
                    from app.max_plan_pool import _all_accounts, _account_available_for_model  # noqa: PLC0415
                    from app.shared_state import direction_failure_streaks, clear_direction_hold  # noqa: PLC0415

                    _cleared_holds = 0
                    for _did, _streak in list(direction_failure_streaks.items()):
                        if not _streak.get("held") or _streak.get("held_reason") != "infra":
                            continue
                        _fc = _streak.get("held_failure_class") or _streak.get("last_failure_class") or ""
                        _ready = False
                        if _fc == "PROVISION_FAILURE" or _fc == "AGENT_TIMEOUT" or _fc == "SCOPE_OVERFLOW":
                            _ready = any_zone_available()
                        elif _fc == "RATE_LIMIT":
                            _ready = any(
                                _account_available_for_model(acct, None)
                                for acct in _all_accounts()
                            )
                        elif _fc == "EXTERNAL_API":
                            _ready = (
                                _external_health.get("github_api", False)
                                and _external_health.get("dispatch_api", False)
                            )
                        if _ready:
                            clear_direction_hold(_did)
                            _cleared_holds += 1
                            logger.info(
                                f"[REAPER] Cleared infra hold on direction {_did} "
                                f"(signal {_fc} recovered)"
                            )
                    if _cleared_holds:
                        logger.info(f"[REAPER] Cleared {_cleared_holds} infra direction hold(s)")
                except Exception as e:
                    logger.error(f"[REAPER] Infra direction hold recovery error: {e}", exc_info=True)

                # Step 5c: Clear holds on directions that are no longer live.
                # When a direction is auto-retired/completed/rejected/expired
                # while a task is held against it, the in-memory streak entry
                # keeps the hold set forever — the 4-hour timeout is the only
                # escape. That strands the task behind a direction that no
                # longer exists in any project's directions list.
                try:
                    await _sweep_orphan_direction_holds(session)
                except Exception as e:
                    logger.error(f"[REAPER] Orphan direction hold sweep error: {e}", exc_info=True)

                # Step 5d: Prune idle entries from long-lived in-memory state
                # dicts. direction_failure_streaks / circuit_breakers /
                # merge_error_occurrences / autonomous_failure_counts /
                # user_debug_spawn_times all accumulate keys monotonically
                # over an instance's lifetime. On Cloud Run instances that
                # stay up for days this contributes to 4GB OOMs.
                try:
                    from app.shared_state import sweep_stale_in_memory_state  # noqa: PLC0415
                    _mem_pruned = sweep_stale_in_memory_state()
                    if any(_mem_pruned.values()):
                        logger.info(f"[REAPER] Pruned idle in-memory state: {_mem_pruned}")
                except Exception as e:
                    logger.error(f"[REAPER] In-memory state sweep error: {e}", exc_info=True)

                # Step 5b: Clear stale or malformed retry_after on QUEUED tasks.
                # A far-future or unparseable value permanently blocks dispatch
                # (orchestrator skips tasks where retry_after > now).
                try:
                    _retry_cutoff = now + timedelta(minutes=RETRY_AFTER_MAX_MINUTES)
                    _queued_with_retry = (await session.execute(
                        select(Task).where(Task.status == TaskStatus.QUEUED)
                    )).scalars().all()
                    _cleared = 0
                    for _qt in _queued_with_retry:
                        # Audit W1: retry_after_until is a column now.
                        if _qt.retry_after_until is None:
                            continue
                        if _qt.retry_after_until > _retry_cutoff:
                            logger.warning(f"[REAPER] Task {_qt.id}: clearing far-future retry_after={_qt.retry_after_until.isoformat()} (>{RETRY_AFTER_MAX_MINUTES}min)")
                        else:
                            continue
                        _qt.retry_after_until = None
                        _new_ctx = dict(_qt.context or {})
                        _new_ctx.pop("_retry_after_resets", None)
                        _qt.context = _new_ctx
                        session.add(_qt)
                        _cleared += 1
                    if _cleared:
                        await session.commit()
                        logger.info(f"[REAPER] Cleared stale retry_after on {_cleared} task(s)")
                except Exception as e:
                    logger.error(f"[REAPER] Stale retry_after sweep error: {e}", exc_info=True)

                # Step 5e: Close ACTIVE ResidualRemediationCase rows whose fingerprint
                # has not been seen in the last 72 h. Without this sweep the table
                # accumulates stale ACTIVE rows for fingerprints that stopped occurring
                # organically (infra recovered, unrelated PR fixed the root cause, etc.).
                try:
                    await _sweep_stale_residual_cases(session)
                except Exception as e:
                    logger.error(f"[REAPER] Stale residual case sweep error: {e}", exc_info=True)

                # Step 6: QUEUED task age alerts — escalate through 3 tiers (INFO/WARNING/ERROR
                # at 1×/3×/6× the threshold) so long-stalled tasks re-alert instead of going silent.
                try:
                    from app.models import Alert, AlertSeverity  # noqa: PLC0415
                    from app.broadcast import broadcast_to_project as _bcast  # noqa: PLC0415
                    import app.shared_state as _shared_state  # noqa: PLC0415
                    _queued_threshold_hours = _runtime_config.get("queued_age_alert_hours", 2)
                    _queued_cutoff = now - timedelta(hours=_queued_threshold_hours)
                    _stale_queued = (await session.execute(
                        select(Task).where(
                            Task.status == TaskStatus.QUEUED,
                            Task.created_at <= _queued_cutoff,
                        )
                    )).scalars().all()

                    _tier_specs = [
                        (1, _queued_threshold_hours, AlertSeverity.INFO, "INFO"),
                        (2, _queued_threshold_hours * 3, AlertSeverity.WARNING, "WARNING"),
                        (3, _queued_threshold_hours * 6, AlertSeverity.ERROR, "ERROR"),
                    ]

                    _alerted = 0
                    for _qt in _stale_queued:
                        _qctx = _qt.context or {}
                        _prev_tier = _qctx.get("queued_age_alert_tier") or (1 if _qctx.get("queued_age_alert_fired") else 0)
                        _queue_hours = (now - _qt.created_at).total_seconds() / 3600
                        _target_tier, _target_severity, _target_label = 0, AlertSeverity.INFO, "INFO"
                        for _tier, _hrs, _sev, _sev_label in _tier_specs:
                            if _queue_hours >= _hrs:
                                _target_tier, _target_severity, _target_label = _tier, _sev, _sev_label
                        if _target_tier <= _prev_tier:
                            continue

                        _qt_type = (_qt.first_payload or {}).get("type", "unknown")
                        # Audit W1: retry_after_until is a column.
                        _ra_dt = _qt.retry_after_until
                        _ra_raw = _ra_dt.isoformat() if _ra_dt else None
                        _ra_recent_cutoff = now - timedelta(minutes=5)
                        if _qt.requires_approval:
                            _likely_cause = "awaiting operator approval"
                        elif _ra_dt is not None and _ra_dt >= _ra_recent_cutoff:
                            _resets = int(_qctx.get("_retry_after_resets") or 0)
                            if _ra_dt > now:
                                _held_min = (now - _qt.created_at).total_seconds() / 60
                                _likely_cause = (
                                    f"rate-limit retry delay active (retry_after={_ra_raw}, "
                                    f"held {_held_min:.1f}m, reset {_resets} times)"
                                )
                            else:
                                _expired_min = (now - _ra_dt).total_seconds() / 60
                                _likely_cause = (
                                    f"rate-limit retry delay recently expired ({_expired_min:.1f}m ago, "
                                    f"reset {_resets} times)"
                                )
                        else:
                            _rate, _sample = _shared_state.get_task_type_failure_rate(_qt_type, window_minutes=15)
                            if _sample >= 3 and _rate >= 0.7:
                                _likely_cause = f"type failure gate active ({_qt_type}: {_rate:.0%} failure rate)"
                            elif _shared_state.tasks_active_gauge >= _runtime_config.get("max_concurrent_vms", 8):
                                _likely_cause = "all accounts healthy but VM slot limit reached"
                            else:
                                from app.max_plan_pool import describe_pool_blockage, _extract_task_model  # noqa: PLC0415
                                _likely_cause = describe_pool_blockage(_extract_task_model(_qt))

                        _alert = Alert(
                            project_id=_qt.project_id,
                            severity=_target_severity,
                            title=f"Task stalled in queue: {_qt.title or str(_qt.id)[:8]}",
                            message=(
                                f"Task type: {_qt_type}. "
                                f"Queued for {_queue_hours:.1f}h (threshold: {_queued_threshold_hours}h, tier {_target_tier}). "
                                f"Likely cause: {_likely_cause}."
                            ),
                            task_id=_qt.id,
                        )
                        session.add(_alert)

                        _new_qctx = dict(_qctx)
                        _new_qctx["queued_age_alert_tier"] = _target_tier
                        _new_qctx["queued_age_alert_fired"] = True
                        _qt.context = _new_qctx
                        session.add(_qt)

                        try:
                            await _bcast(str(_qt.project_id), "alert_created", {
                                "alert_id": str(_alert.id),
                                "severity": _target_label,
                                "title": _alert.title,
                            })
                        except Exception as _bcast_err:
                            logger.warning(f"[REAPER] Queued age alert broadcast failed for {_qt.id}: {_bcast_err}")

                        _alerted += 1
                        logger.info(
                            f"[REAPER] Queued age alert tier {_target_tier} ({_target_label}) fired for task {_qt.id} "
                            f"(type={_qt_type}, queued={_queue_hours:.1f}h, cause={_likely_cause})"
                        )

                    if _alerted:
                        await session.commit()
                        logger.info(f"[REAPER] Fired queued age alerts for {_alerted} stale task(s)")

                except Exception as e:
                    logger.error(f"[REAPER] Queued age alert sweep error: {e}", exc_info=True)

                # Step 7: Stalled strategic-cycle reaper.
                # Audit W2/D2/D12: cycles stuck in phase*_running state for >3h
                # mean the synchronous phase1→phase2 chain crashed and the
                # lifecycle backstop missed it (e.g. orchestrator restart between
                # task termination and post-commit). Mark them stalled so the
                # strategic agent can see the failure on the next run.
                try:
                    from app import strategic_cycle  # noqa: PLC0415
                    from app.models import Alert, AlertSeverity  # noqa: PLC0415
                    from app.broadcast import broadcast_to_project as _bcast  # noqa: PLC0415
                    stalled = await strategic_cycle.find_stalled(session)
                    for c in stalled:
                        if c.outcome in (
                            strategic_cycle.OUTCOME_IN_PROGRESS,
                            strategic_cycle.OUTCOME_PHASE1_RUNNING,
                            strategic_cycle.OUTCOME_PHASE1_DONE,
                        ):
                            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE1
                            stalled_phase = "phase1"
                        else:
                            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE2
                            stalled_phase = "phase2"
                        if c.failure_reason is None:
                            c.failure_reason = f"reaper_stall: cycle in {stalled_phase} >3h with no completion"
                        session.add(c)
                        try:
                            alert = Alert(
                                project_id=c.project_id,
                                severity=AlertSeverity.WARNING,
                                title=f"Strategic cycle {c.id} stalled in {stalled_phase}",
                                message=(
                                    f"Cycle {c.id} has been in {stalled_phase} for >3h with no "
                                    f"completion handler update. Marking stalled — next strategic "
                                    f"cycle will spawn a fresh attempt. Phase1 task: {c.phase1_task_id}, "
                                    f"phase2 task: {c.phase2_task_id}."
                                ),
                            )
                            session.add(alert)
                            await _bcast(
                                str(c.project_id), "alert_created",
                                {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
                            )
                        except Exception:
                            logger.exception(f"[REAPER] Failed to create stalled-cycle alert for {c.id}")
                    if stalled:
                        await session.commit()
                        logger.info(f"[REAPER] Marked {len(stalled)} strategic cycle(s) stalled")
                except Exception as e:
                    logger.error(f"[REAPER] Stalled-cycle sweep error: {e}", exc_info=True)
            consecutive_errors = 0
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            try:
                await session.rollback()
            except Exception:
                logger.exception("[REAPER] Rollback after reaper error failed")

        # Sleep before next cycle; use exponential backoff on consecutive errors
        sleep_seconds = min(60 * (2 ** consecutive_errors), 300)
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
