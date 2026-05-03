"""Orchestrator worker - background loop that provisions tasks and monitors health.

Extracted from app.main to reduce module size. The worker polls for QUEUED tasks,
checks dependencies, handles file conflicts, provisions VMs, and monitors
EVALUATING tasks.
"""

import asyncio
import logging
import os
import random
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.compute import (
    INFRA_PROVISION_PATTERNS,
    VM_PROVISIONING_MODEL,
    _capture_serial_for_vm,
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
from app.models import Artifact, ArtifactType, DeploymentState, Project, Task, TaskStatus
from app.orchestrator_filters import (
    filter_file_conflicts, filter_rollup_conflicts, filter_held_directions,
    filter_pipeline_duplicates, filter_migration_serialization,
)
from app.storage import upload_json_payload
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config, sync_runtime_config_from_state as _sync_runtime_config_from_state
from app.task_locking import task_lock
from app.time_utils import _utcnow, clear_retry_after, rate_limit_retry_minutes, set_retry_after

logger = logging.getLogger("blueprint")


def _clear_retry_after_state(task_or_ctx) -> None:
    """Clear retry_after hold and its reset counter on a Task or context dict.

    Why: the counter feeds an escalation alert; leaving it across recovery
    episodes causes false escalations on the next rate-limit episode.

    Audit 2026-04-25 W1: ``task.retry_after_until`` is now a real column
    so callers should pass the Task; the dict-only form is kept for legacy
    callsites that operate on bare context dicts (e.g. spawn paths).
    """
    clear_retry_after(task_or_ctx)


def _capture_ci_polling_snapshot(task: Task) -> dict:
    """Snapshot the CI-polling fields a lock-window break must protect.

    R5-W2-A: ``updated_at`` advances every ~30s via the heartbeat
    ``onupdate=_utcnow`` and would false-defer the CI-polling re-acquire
    on every overlapping heartbeat. We validate against content fields
    instead — the ones whose mutation indicates a peer took over CI
    handling for this task:

      - ``status``: the most load-bearing; if a peer transitioned the
        task out of EVALUATING (e.g. to COMPLETED on PR merge or FAILED
        on PR delete), we must not re-apply CI-polling mutations on top.
      - ``review_loop.{pr_number, ci_passed, review_approved, iteration}``:
        if any of these moved, the review pipeline advanced under us.

    Heartbeat-only updates touch none of these.
    """
    rl = ((task.context or {}).get("review_loop") or {})
    return {
        "status": task.status,
        "pr_number": rl.get("pr_number"),
        "ci_passed": rl.get("ci_passed"),
        "review_approved": rl.get("review_approved"),
        "iteration": rl.get("iteration"),
    }


async def _lock_task_for_mutation(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    allowed_statuses: tuple[TaskStatus, ...] | None = None,
    reason: str = "task mutation",
    expected_snapshot: dict | None = None,
    snapshot_fn=None,
) -> Task | None:
    """Lock a task before a hot-path mutation and rollback on skipped rows.

    Commit-release-relock pattern (Phase B3) is used to break lock windows
    around long-running GitHub I/O. When ``expected_snapshot`` +
    ``snapshot_fn`` are passed, the re-acquired row is compared against the
    pre-release content snapshot — if a peer mutated it during the unlocked
    window we defer (return None) so the caller can retry on the next
    sweep or surface a conflict.

    R5-W2-A: this content-based snapshot replaced an earlier
    ``expected_updated_at`` timestamp validator that false-tripped on every
    overlapping heartbeat (heartbeats with ``onupdate=_utcnow`` advance
    ``updated_at`` every ~30s). The snapshot pair lets the caller capture
    the fields it actually cares about (e.g. status + review_loop
    pr_number / ci_passed / review_approved / iteration) and defer only
    when one of those has actually changed. R6-W3-B: dropped the unused
    ``expected_updated_at`` parameter — every CI-polling caller migrated
    to the snapshot pair, leaving the timestamp branch dead.
    """
    async with task_lock(session, task_id) as task:
        if task is None:
            logger.info("[ORCHESTRATOR] %s for task %s deferred: task row locked", reason, task_id)
            await session.rollback()
            return None
        if allowed_statuses is not None and task.status not in allowed_statuses:
            logger.info(
                "[ORCHESTRATOR] %s for task %s skipped: status=%s",
                reason,
                task_id,
                task.status,
            )
            await session.rollback()
            return None
        if expected_snapshot is not None and snapshot_fn is not None:
            current_snapshot = snapshot_fn(task)
            if current_snapshot != expected_snapshot:
                logger.info(
                    "[ORCHESTRATOR] %s for task %s deferred: row mutated during unlocked window "
                    "(expected snapshot=%s, found %s)",
                    reason, task_id, expected_snapshot, current_snapshot,
                )
                await session.rollback()
                return None
        return task


async def _lock_provisioning_task_for_vm(
    session: AsyncSession,
    task_id: uuid.UUID,
    vm_id: str,
) -> Task | None:
    """Lock a PROVISIONING task only if it still owns the provisioning VM.

    Provisioning completes on a background coroutine. By the time that coroutine
    reports success/failure, the reaper may already have requeued the task with
    a new ``vm_instance_id``. Mutating without this fresh locked check can
    overwrite the reaper's QUEUED state with stale provision-failure handling.
    """
    task = await _lock_task_for_mutation(
        session,
        task_id,
        allowed_statuses=None,
        reason="provision result",
    )
    if task is None:
        return None
    if task.status != TaskStatus.PROVISIONING or task.vm_instance_id != vm_id:
        logger.info(
            "[ORCHESTRATOR] Ignoring stale provision result for task %s "
            "(status=%s, vm=%s, result_vm=%s)",
            task_id,
            task.status,
            task.vm_instance_id,
            vm_id,
        )
        await session.rollback()
        return None
    return task


# ---------------------------------------------------------------------------
# Module-level globals managed by orchestrator_worker
# ---------------------------------------------------------------------------

_last_pool_refresh: datetime = _utcnow()
_last_zone_health_persist: datetime = _utcnow()
_last_anomaly_state_persist: datetime = _utcnow()
_last_anomaly_sweep: datetime = _utcnow()
_last_orphan_check: datetime = _utcnow()
_last_evaluating_timeout_check: datetime = _utcnow()
_last_post_commit_sweep: datetime = _utcnow()
_last_deadlock_check: float = 0.0
_last_zone_exhaustion_log: datetime | None = None
_last_concurrency_broadcast: float = 0.0
_last_type_throttle_broadcast: dict[str, float] = {}
_last_type_unthrottle_broadcast: dict[str, float] = {}
_previously_failing_types: set[str] = set()

# Debounce: fire the all-accounts-blocked alert once per stall episode.
# Reset when any task dispatches successfully (stall resolved), then re-arm
# if every eligible task is again blocked by account rate limits.
_all_accounts_blocked_alerted: bool = False


def _compute_all_accounts_blocked_action(
    blocked_count: int, dispatched_count: int, already_alerted: bool
) -> str:
    """Decide how to handle the all-accounts-blocked stall signal after a cycle.

    Returns 'fire' (emit a new stall alert), 'reset' (re-arm the debounce),
    or 'noop' (leave state untouched).
    """
    if blocked_count > 0 and dispatched_count == 0:
        return "noop" if already_alerted else "fire"
    if dispatched_count > 0:
        return "reset"
    return "noop"


def _compute_retry_after_escalation_action(
    resets: int, alert_threshold: int, fail_threshold: int
) -> str | None:
    """Decide how to escalate a task that keeps cycling through retry_after holds.

    Returns 'fail' once `resets` reaches the secondary `fail_threshold` (auto-fail
    so the requeue/retry machinery can take over with fresh context), 'alert'
    when only the WARNING `alert_threshold` is crossed, or None below both bars.
    The fail check wins ties when both thresholds are crossed.
    """
    if fail_threshold > 0 and resets >= fail_threshold:
        return "fail"
    if alert_threshold > 0 and resets >= alert_threshold:
        return "alert"
    return None


_STALL_TITLE_PREFIX = "Pipeline stalled —"


def _missing_runtime_credentials_stall_reason(
    *,
    user_pool_count: int,
    provider_account_count: int,
    ready_provider_account_count: int,
    runtime_provider: str | None,
) -> str:
    """Describe why dispatch cannot find any usable runtime credentials."""
    if user_pool_count == 0:
        return "no accounts configured"
    if runtime_provider and provider_account_count == 0:
        return f"no accounts configured for provider {runtime_provider}"
    if ready_provider_account_count == 0:
        provider_suffix = f" for provider {runtime_provider}" if runtime_provider else ""
        return f"no ready accounts{provider_suffix}"
    return "no eligible accounts or slot contention"


def _format_stall_alert(
    blockage_reasons: set[str] | None, earliest_recovery: datetime | None
) -> tuple[str, str]:
    """Build a truthful (title, body) for the all-accounts-blocked stall alert
    from the per-cycle blockage reasons emitted by describe_pool_blockage().

    A stall can come from rate-limit cooldown, slot/daily-cap contention,
    missing accounts, or a mix. Each shape gets a distinct title so the
    operator alert is not lying about why work isn't dispatching.

    The title says "eligible" rather than "all" because describe_pool_blockage
    summarizes per-task-model constraints — when only the model-eligible
    candidates are exhausted, other accounts in the pool can still serve
    different models. Calling that "all" was the 2026-04-24 misleading-alert
    bug where the user saw "all rate-limited" while 50 other tasks completed.

    With no reasons supplied (legacy callers / tests), fall back to the
    rate-limit wording so dedup keys and copy stay stable.
    """
    recovery_iso = earliest_recovery.isoformat() if earliest_recovery else None
    reasons = sorted(blockage_reasons) if blockage_reasons else []
    if not reasons:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts rate-limited"
        body = (
            "Every dispatchable task was skipped because the model-eligible "
            "Max plan accounts are in rate-limit cooldown. "
            + (f"Earliest recovery: {recovery_iso}." if recovery_iso else "Waiting for cooldown.")
        )
        return title, body
    rate_limited = any("rate-limit cooldown" in r or "rate-limited" in r for r in reasons)
    capacity = any("slot contention" in r or "at capacity" in r for r in reasons)
    no_pool_accounts = any(r == "no accounts configured" for r in reasons)
    no_provider_accounts = any("no accounts configured for provider" in r for r in reasons)
    no_ready_accounts = any("no ready accounts" in r for r in reasons)
    if no_pool_accounts and not no_provider_accounts and not no_ready_accounts and not rate_limited and not capacity:
        title = f"{_STALL_TITLE_PREFIX} no Max plan accounts configured"
    elif (no_pool_accounts or no_provider_accounts or no_ready_accounts) and not rate_limited and not capacity:
        title = f"{_STALL_TITLE_PREFIX} no Max plan accounts ready"
    elif rate_limited and capacity:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts unavailable (rate limits + capacity)"
    elif rate_limited:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts rate-limited"
    elif capacity:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts at capacity"
    else:
        title = f"{_STALL_TITLE_PREFIX} eligible Max plan accounts unavailable"
    body = "Every dispatchable task was skipped because: " + "; ".join(reasons) + "."
    if rate_limited:
        body += f" Earliest recovery: {recovery_iso}." if recovery_iso else " Waiting for cooldown."
    return title, body


async def _emit_all_accounts_blocked_stall(
    session: AsyncSession,
    earliest_recovery: datetime | None,
    blockage_reasons: set[str] | None = None,
) -> None:
    """Broadcast, alert, and notify operators that the pipeline is stalled on
    Max-plan account exhaustion. Intended to be called once per stall episode."""
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.models import Alert, AlertSeverity
    from app.notification_dispatch import dispatch_external_notification

    recovery_iso = earliest_recovery.isoformat() if earliest_recovery else None
    title, msg = _format_stall_alert(blockage_reasons, earliest_recovery)
    try:
        await ws_manager.broadcast_to_all("pipeline_throttle", {
            "reason": "all_accounts_blocked",
            "detail": msg,
            "earliest_recovery": recovery_iso,
            "blockage_reasons": sorted(blockage_reasons) if blockage_reasons else [],
        })
    except Exception:
        logger.exception("[ORCHESTRATOR] Failed to broadcast all_accounts_blocked throttle")
    try:
        projects = (await session.execute(select(Project))).scalars().all()
        project_ids = [p.id for p in projects]
        # Dedup across the "Pipeline stalled — ..." family, not the exact
        # title: with dynamic copy a new stall episode could otherwise stack
        # a second alert if its blockage shape (rate-limit vs capacity) flips.
        existing_rows = (await session.execute(
            select(Alert.project_id).where(
                Alert.project_id.in_(project_ids),
                Alert.task_id.is_(None),
                Alert.severity == AlertSeverity.ERROR,
                Alert.dismissed_at.is_(None),
                Alert.title.like(f"{_STALL_TITLE_PREFIX}%"),
            )
        )).all() if project_ids else []
        projects_with_alert = {row[0] for row in existing_rows}
        for proj in projects:
            if proj.id not in projects_with_alert:
                alert = Alert(
                    project_id=proj.id,
                    severity=AlertSeverity.ERROR,
                    title=title,
                    message=msg,
                )
                session.add(alert)
                await broadcast_to_project(
                    str(proj.id), "alert_created",
                    {"alert_id": str(alert.id), "severity": "ERROR", "title": title},
                )
            asyncio.ensure_future(dispatch_external_notification(  # noqa: RUF006
                proj.id, "PIPELINE_STALLED",
                {"reason": "all_accounts_blocked",
                 "earliest_recovery": recovery_iso or "",
                 "detail": msg},
            ))
    except Exception:
        logger.exception("[ORCHESTRATOR] Failed to create all-accounts-blocked stall alert")
    logger.warning(f"[ORCHESTRATOR] {title}: {msg}")

# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: datetime | None = None
_orchestrator_cycle_started: datetime | None = None

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

# Columns the dispatch loop loads for active (PROVISIONING/IN_PROGRESS) tasks.
# Heavy JSONB (status_log, depends_on, history) stays deferred. Anything a
# downstream filter touches (e.g. filter_file_conflicts -> context/title,
# filter_migration_serialization -> payloads via first_payload) MUST be here,
# or attribute access triggers a sync lazy-load from async context and crashes
# the whole dispatch cycle. Module-level so tests can mirror the exact set.
ACTIVE_TASKS_LIGHT_COLUMNS = (
    Task.id, Task.status, Task.last_heartbeat, Task.started_at,
    Task.vm_instance_id, Task.project_id, Task.context, Task.title,
    Task.payloads,
)


# ---------------------------------------------------------------------------
# Cycle detection for deadlock resolution
# ---------------------------------------------------------------------------

def _find_cycle_members(adj: Dict[str, list]) -> set:
    """Return node IDs participating in cycles (DFS white/gray/black coloring)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)
    members: set = set()
    def dfs(n, stack):
        color[n] = GRAY
        stack.append(n)
        for d in adj.get(n, []):
            if color.get(d) == GRAY:
                members.update(stack[stack.index(d):])
            elif color.get(d) == WHITE:
                dfs(d, stack)
        stack.pop()
        color[n] = BLACK
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
    from app.deploy_health import _get_deploy_state_for_update
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
    from app.deploy_health import (
        _get_deploy_state_for_update,
        _pause_failure_streak,
        _health_recovery_history,
        _last_auto_resume_at,
        _recent_auto_resumes,
        _last_resume_was_forced,
        _consecutive_force_resumes,
    )
    try:
        from app.compute import _zone_health, _zone_lock
        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {
                    k: (v.isoformat() if isinstance(v, datetime)
                        else list(v) if isinstance(v, deque)
                        else v)
                    for k, v in data.items()
                }
        snapshot["_pause_failure_streak"] = _pause_failure_streak
        snapshot["_health_recovery_history"] = list(_health_recovery_history)
        snapshot["_last_auto_resume_at"] = (
            _last_auto_resume_at.isoformat() if _last_auto_resume_at else None
        )
        snapshot["_recent_auto_resumes"] = [t.isoformat() for t in _recent_auto_resumes]
        snapshot["_last_resume_was_forced"] = bool(_last_resume_was_forced)
        snapshot["_consecutive_force_resumes"] = int(_consecutive_force_resumes)
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


def _serialize_anomaly_snapshot() -> dict:
    """Build the anomaly-state snapshot from in-memory shared_state."""
    serializable_anomalies: dict = {}
    for proj_id, proj_anomalies in _shared_state.persistent_anomalies.items():
        serializable_anomalies[proj_id] = {}
        for k, v in proj_anomalies.items():
            serializable_anomalies[proj_id][k] = {
                fk: fv.isoformat() if isinstance(fv, datetime) else fv
                for fk, fv in v.items()
            }
    return {
        "persistent_anomalies": serializable_anomalies,
        "anomaly_adjustments": dict(_shared_state.anomaly_adjustments),
    }


def _serialize_direction_streaks_snapshot() -> dict:
    """Build the direction-streak snapshot from in-memory shared_state."""
    return {
        direction_id: {
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in streak.items()
        }
        for direction_id, streak in _shared_state.direction_failure_streaks.items()
    }


def _serialize_pipeline_events_snapshot() -> dict:
    """Build the pipeline-events snapshot from in-memory shared_state."""
    events_list = [
        {k: v.isoformat() if isinstance(v, datetime) else v for k, v in e.items()}
        for e in _shared_state.deploy_monitoring["pipeline_events"]
    ]
    return {"events": events_list}


async def _persist_anomaly_state(session: AsyncSession | None = None):
    """Snapshot anomaly detection state to DB so occurrence counts survive restarts.

    If ``session`` is provided, mutates ``DeploymentState`` on that session and
    leaves the commit to the caller (lets the orchestrator coalesce multiple
    persist calls into one round-trip — see M-P6). When called without a
    session, opens its own session and commits, preserving the legacy
    fire-and-forget contract used by tests.
    """
    from app.deploy_health import _get_deploy_state_for_update
    try:
        snapshot = _serialize_anomaly_snapshot()
        if session is not None:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.persistent_anomaly_state = snapshot
                session.add(state)
            return
        async with AsyncSessionLocal() as new_session:
            state = await _get_deploy_state_for_update(new_session)
            if state:
                state.persistent_anomaly_state = snapshot
                new_session.add(state)
                await new_session.commit()
    except Exception as e:
        logger.warning(f"[ANOMALY STATE] Failed to persist: {e}", exc_info=True)


async def _persist_direction_streaks(session: AsyncSession | None = None):
    """Snapshot direction failure streaks to DB so holds survive restarts.

    See :func:`_persist_anomaly_state` for the optional-session contract.
    """
    from app.deploy_health import _get_deploy_state_for_update
    try:
        serializable = _serialize_direction_streaks_snapshot()
        if session is not None:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.direction_streak_state = serializable
                session.add(state)
            return
        async with AsyncSessionLocal() as new_session:
            state = await _get_deploy_state_for_update(new_session)
            if state:
                state.direction_streak_state = serializable
                new_session.add(state)
                await new_session.commit()
    except Exception as e:
        logger.warning(f"[DIRECTION STREAKS] Failed to persist: {e}", exc_info=True)


async def _persist_pipeline_events(session: AsyncSession | None = None):
    """Snapshot pipeline events ring buffer to DB so alert windows survive restarts.

    See :func:`_persist_anomaly_state` for the optional-session contract.
    """
    from app.deploy_health import _get_deploy_state_for_update
    try:
        snapshot = _serialize_pipeline_events_snapshot()
        if session is not None:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.pipeline_events_state = snapshot
                session.add(state)
            return
        async with AsyncSessionLocal() as new_session:
            state = await _get_deploy_state_for_update(new_session)
            if state:
                state.pipeline_events_state = snapshot
                new_session.add(state)
                await new_session.commit()
    except Exception as e:
        logger.warning(f"[PIPELINE EVENTS] Failed to persist: {e}", exc_info=True)


async def _persist_self_healing_state_bundle():
    """Coalesced 60s snapshot of anomaly + direction-streak + pipeline-events state.

    All three live on the same ``DeploymentState`` row, so we open one session,
    take the row lock once via ``_get_deploy_state_for_update``, mutate the
    three columns, and commit once. Replaces three separate session/commit
    cycles per orchestrator tick (M-P6).
    """
    from app.deploy_health import _get_deploy_state_for_update
    try:
        anomaly_snapshot = _serialize_anomaly_snapshot()
        direction_snapshot = _serialize_direction_streaks_snapshot()
        events_snapshot = _serialize_pipeline_events_snapshot()
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.persistent_anomaly_state = anomaly_snapshot
                state.direction_streak_state = direction_snapshot
                state.pipeline_events_state = events_snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[SELF-HEALING STATE] Failed to persist bundle: {e}", exc_info=True)


_CRITICAL_EXTERNAL_DEPS = ("github_api", "dispatch_api", "gcp")

_ENV_FALLBACK_DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-7",
    "openai": "gpt-5.4",
    "gemini": "gemini-2.5-pro",
}


def _provider_env_credential_hint(provider: str | None) -> str:
    if provider == "openai":
        return "OPENAI_API_KEY or CODEX_API_KEY or CODEX_HOME/auth.json"
    if provider == "gemini":
        return "GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS (+ GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION)"
    return "ANTHROPIC_API_KEY"


def _resolve_env_runtime_fallback(
    *,
    provider: str | None,
    model: str | None,
    execution_mode: str | None,
    task: Task,                              # noqa: ARG001 — kept for signature stability
    runtime_config: dict[str, Any],          # noqa: ARG001
) -> tuple[str | None, str, str | None, str | None, str | None, dict[str, str]]:
    """Return env-backed runtime credentials when no pool account is available.

    `execution_mode` is no longer resolved from runtime config (retired with
    the settings-bag redesign); whatever the caller passes is round-tripped.
    """
    provider_name = provider or "anthropic"
    runtime_model = model or _ENV_FALLBACK_DEFAULT_MODELS.get(provider_name)
    runtime_execution_mode = execution_mode
    provider_metadata: dict[str, str] = {}

    if provider_name == "openai":
        secret = os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
        auth_mode = "api_key"
        if not secret:
            codex_home = (os.getenv("CODEX_HOME") or "").strip()
            auth_path = Path(codex_home) / "auth.json" if codex_home else None
            if not auth_path or not auth_path.exists():
                return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
            try:
                secret = auth_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    f"[ORCHESTRATOR] Failed to read OpenAI auth.json from CODEX_HOME={codex_home!r}: {exc}"
                )
                return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
            auth_mode = "oauth"
        return secret, provider_name, auth_mode, runtime_model, runtime_execution_mode, provider_metadata

    if provider_name == "gemini":
        secret = os.getenv("GEMINI_API_KEY")
        if secret:
            return secret, provider_name, "api_key", runtime_model, runtime_execution_mode, provider_metadata
        service_account_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not service_account_path:
            return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
        try:
            secret = Path(service_account_path).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                f"[ORCHESTRATOR] Failed to read GOOGLE_APPLICATION_CREDENTIALS={service_account_path!r}: {exc}"
            )
            return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
        if not project or not location:
            logger.warning(
                "[ORCHESTRATOR] GOOGLE_APPLICATION_CREDENTIALS is set for Gemini service-account fallback "
                "but GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are both required"
            )
            return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
        if project:
            provider_metadata["project"] = project
        if location:
            provider_metadata["location"] = location
        return secret, provider_name, "service_account", runtime_model, runtime_execution_mode, provider_metadata

    secret = os.getenv("ANTHROPIC_API_KEY")
    if not secret:
        return None, provider_name, None, runtime_model, runtime_execution_mode, provider_metadata
    return secret, provider_name, "api_key", runtime_model, runtime_execution_mode, provider_metadata


def _sync_task_payload_runtime_fields(
    task: Task,
    *,
    model: str | None,
    execution_mode: str | None,
) -> bool:
    """Keep payload runtime hints aligned with the resolved task runtime identity."""
    if not task.payloads:
        return False
    payload = task.payloads[0]
    if payload is None:
        payload = {}
        task.payloads[0] = payload
    changed = False
    if model and payload.get("model") != model:
        payload["model"] = model
        changed = True
    if execution_mode and payload.get("execution_mode") != execution_mode:
        payload["execution_mode"] = execution_mode
        changed = True
    return changed


def get_confirmed_down_critical_deps() -> list:
    """Return critical dependency names confirmed down for 2+ consecutive health checks.

    DB is excluded — if DB is down the orchestrator itself cannot function, so
    there's no meaningful gate to apply.  A dep needs >=2 consecutive failures to
    avoid false-positives from transient probe errors.
    """
    consec = _shared_state.external_health.get("consecutive_failures", {})
    return [d for d in _CRITICAL_EXTERNAL_DEPS if consec.get(d, 0) >= 2]


async def _check_candidate_task_deps(
    task: Task,
    dep_tasks: list,
    dep_uuids: list,
    session: AsyncSession,
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
        recheck_disabled_accounts,
        _extract_task_model,
        _account_available_for_model,
        _account_by_id,
        _max_plan_pools,
        _max_plan_pool_lock,
        apply_task_llm_runtime_context,
        clear_task_llm_runtime_context,
        get_task_llm_account_id,
        select_llm_runtime_for_task,
    )
    from app.providers.registry import get_provider
    from app.main import (
        _get_actual_gcp_vm_count,
        _ensure_vm_deleted,
        _maybe_create_failure_alert,
        _create_orchestrator_error_alerts,
    )
    from app.routers.agent_status import record_task_result
    from app.deploy_health import (
        _check_monitoring_window,
        _check_external_health,
        _check_pause_mode,
    )
    from app.autonomous_research import _check_idle_research
    from app.rollup_management import _verify_pr_merged
    from app.review_pipeline import _spawn_pipeline_task
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.services import _recover_orphaned_tasks, _check_evaluating_timeouts, _sweep_post_commit_pending
    from app.routers.agent import get_github_token_for_task
    from app.github import (
        close_pull_request,
        get_check_runs_cached,
        get_pull_request,
        reopen_pull_request,
        update_branch_with_sha_retry,
    )
    global _last_pool_refresh, _orchestrator_last_cycle, _orchestrator_cycle_started
    global _orchestrator_consecutive_errors, _orchestrator_consecutive_monitoring_errors
    global _last_orphan_check, _last_evaluating_timeout_check, _last_post_commit_sweep
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

            # Periodic self-healing state persist — anomaly counts, direction holds, pipeline events.
            # All three live on the same DeploymentState row, so coalesce into a
            # single session/commit (M-P6) instead of three round-trips.
            global _last_anomaly_state_persist
            if (_utcnow() - _last_anomaly_state_persist).total_seconds() >= 60:
                await _persist_self_healing_state_bundle()
                _last_anomaly_state_persist = _utcnow()

            # Independent anomaly maintenance — expires stale entries and restores
            # pipeline adjustments without depending on autonomous research cycles.
            global _last_anomaly_sweep
            if (_utcnow() - _last_anomaly_sweep).total_seconds() >= 600:
                from app.anomaly_detection import anomaly_maintenance_sweep  # noqa: PLC0415
                try:
                    await anomaly_maintenance_sweep()
                except Exception:
                    logger.exception("anomaly_maintenance_sweep failed")
                _last_anomaly_sweep = _utcnow()

            # First-cycle awaits are the most common silent-hang site: connection
            # pool exhaustion, row-level locks from a previously OOM'd instance,
            # or VPC/Cloud-SQL proxy hiccups all manifest as an indefinite wait
            # here with no logs. Bound the whole session-acquire + config-sync
            # block so a stuck cycle becomes a loud TimeoutError instead.
            try:
                async with asyncio.timeout(30):
                    async with AsyncSessionLocal() as session:
                        # Sync runtime_config with DB each cycle. Cloud Run runs up to
                        # max_instance_count instances; admin mutations (pause, settings)
                        # only update the receiving instance's in-memory runtime_config,
                        # so other instances would otherwise dispatch past a pause or
                        # stale cap. DeploymentState is the authoritative source.
                        _deploy_state = await session.get(DeploymentState, 1)
                        _changed_keys = await _sync_runtime_config_from_state(_deploy_state)
                        if _changed_keys:
                            logger.info(f"[ORCHESTRATOR] Runtime config drift corrected: {_changed_keys}")
            except TimeoutError:
                logger.error(
                    "[ORCHESTRATOR] Timed out (30s) acquiring session / reading DeploymentState — "
                    "likely DB pool contention or row-level lock from a stale transaction. "
                    "Skipping this cycle."
                )
                _orchestrator_last_cycle = _utcnow()
                await asyncio.sleep(2)
                continue
            async with AsyncSessionLocal() as session:

                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                # Light query: only load columns needed for the stale-provision scan
                # and concurrency count. Heavy JSONB (context, status_log) is
                # deferred — the few stale tasks are re-read through task_lock()
                # before mutation below.
                # context + title are needed by filter_file_conflicts (Step 2.5).
                # payloads is needed by filter_migration_serialization (Step 2.72)
                # via _is_migration_task -> task.first_payload.
                # Without them, attribute access triggers a sync lazy-load from an
                # async context and crashes the whole dispatch cycle.
                _active_light_cols = load_only(*ACTIVE_TASKS_LIGHT_COLUMNS)
                active_tasks_result = (await session.execute(
                    select(Task)
                    .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                    .options(_active_light_cols)
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
                        # Re-read with task_lock to prevent races with the reaper
                        # and provisioning callback on provision_failures/requeue state.
                        fresh_t = await _lock_task_for_mutation(
                            session,
                            t.id,
                            allowed_statuses=(TaskStatus.PROVISIONING,),
                            reason="stale provisioning recovery",
                        )
                        if not fresh_t:
                            continue  # Reaper already handled it

                        # Capture serial console BEFORE any state mutation. This
                        # is the only forensic source for silent post-creds-fetch
                        # deaths — the in-script ERR-trap + failure-beacon path
                        # can be bypassed (SIGKILL, kernel hang, lost-after-trap
                        # poweroff). reaper.py:534 already does this for the
                        # agent-startup-failure reaper; the stale-PROVISIONING
                        # path was the silent gap.
                        old_vm_for_serial = fresh_t.vm_instance_id
                        serial_log: str | None = None
                        if old_vm_for_serial:
                            prov_zone = (fresh_t.context or {}).get("provision_zone")
                            if not prov_zone:
                                cached_zone = get_vm_zone(old_vm_for_serial)
                                if cached_zone and cached_zone != "unknown":
                                    prov_zone = cached_zone
                            if prov_zone:
                                try:
                                    serial_log = await asyncio.to_thread(
                                        _capture_serial_for_vm,
                                        old_vm_for_serial,
                                        prov_zone,
                                    )
                                    if serial_log:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Serial log for stale "
                                            f"provisioning VM {old_vm_for_serial} "
                                            f"(task {t.id}):\n{serial_log}"
                                        )
                                except Exception:
                                    logger.exception(
                                        f"[ORCHESTRATOR] Serial capture failed for "
                                        f"stale provisioning VM {old_vm_for_serial}"
                                    )
                            else:
                                logger.warning(
                                    f"[ORCHESTRATOR] Serial capture skipped for task "
                                    f"{t.id}: zone unknown (vm_id={old_vm_for_serial})"
                                )

                        # Check provision failure limit before requeueing
                        provision_failures = (fresh_t.context or {}).get("provision_failures", 0) + 1
                        if provision_failures >= _runtime_config.get("max_provision_retries", 3):
                            fresh_t.status = TaskStatus.FAILED
                            fresh_t.latest_status_message = f"Failed after {provision_failures} provision attempts"
                            failed_ctx = {**(fresh_t.context or {}), "failure_category": "PROVISION_FAILURE", "provision_failures": provision_failures}
                            if serial_log:
                                failed_ctx["last_serial_log"] = serial_log
                            fresh_t.context = failed_ctx
                            logger.error(f"[ORCHESTRATOR] Task {t.id} exceeded provision retry limit ({provision_failures}) — marking FAILED")
                            acct_id = get_task_llm_account_id(fresh_t)
                            if acct_id:
                                await release_max_plan_account(acct_id)
                            session.add(fresh_t)
                            await _maybe_create_failure_alert(fresh_t, session, f"Provision failed {provision_failures} times — exceeded retry limit")
                            record_task_result(success=False, failure_class="PROVISION_FAILURE", task_id=str(fresh_t.id), task_type=(fresh_t.first_payload).get("type", ""))
                            continue
                        stale_vm_ids.append(fresh_t.vm_instance_id)
                        # Release Max Plan account slot before requeueing
                        acct_id = get_task_llm_account_id(fresh_t)
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
                        requeue_ctx = {**(fresh_t.context or {}), "provision_failures": provision_failures}
                        if serial_log:
                            requeue_ctx["last_serial_log"] = serial_log
                        fresh_t.context = clear_task_llm_runtime_context(requeue_ctx)
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
                    # Re-fetch after recovery (light columns only — just need count)
                    active_tasks_result = (await session.execute(
                        select(Task)
                        .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                        .options(_active_light_cols)
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
                    _orchestrator_last_cycle = _utcnow()
                    # Monitoring must still run when the dispatch path is idle —
                    # otherwise an auto-pause (max_concurrent_vms=0) makes
                    # slots_available=0 every cycle, which `continue`s past the
                    # monitoring block at the bottom of the loop and freezes the
                    # 3-of-5 recovery ring buffer. Without this, pause is sticky
                    # forever because _check_external_health / _check_pause_mode
                    # never run while paused.
                    try:
                        await _check_monitoring_window()
                        await _check_external_health()
                        await _check_pause_mode()
                    except Exception:
                        logger.exception("[ORCHESTRATOR] Monitoring checks failed during idle cycle")
                    await asyncio.sleep(5)
                    continue

                # Failure-rate dispatch gate — throttle when recent tasks are mostly failing
                # Filter to last 15 min so the gate auto-clears when failures age out
                _fr_cutoff = (_utcnow() - timedelta(minutes=15)).isoformat()
                recent_results = [r for r in _shared_state.deploy_monitoring["task_results"] if r[0] >= _fr_cutoff]
                # Types with >70% failure rate (min 3 samples) are skipped in the dispatch loop
                # below without throttling healthy types.
                _failing_types: set = set()
                if len(recent_results) >= 5:
                    recent_window = recent_results[-10:]
                    recent_failures = sum(1 for r in recent_window if r[1] == "failure")
                    recent_rate = recent_failures / len(recent_window)
                    _failure_gate = _shared_state.anomaly_adjustments["failure_gate_threshold"]
                    if recent_rate >= _failure_gate:
                        # Global backstop: entire pipeline is degraded — sleep and retry
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
                    else:
                        # Per-type gate: detect individual failing types and skip only them
                        seen_types = {r[3] for r in recent_results if len(r) > 3 and r[3]}
                        for ttype in seen_types:
                            rate, sample_size = _shared_state.get_task_type_failure_rate(ttype, window_minutes=15)
                            if sample_size >= 3 and rate >= 0.7:
                                _failing_types.add(ttype)
                                logger.warning(
                                    f"[ORCHESTRATOR] Per-type failure gate: '{ttype}' "
                                    f"({rate:.0%} failure rate, n={sample_size}) — skipping this type"
                                )

                # Broadcast type_throttle events for each newly-blocked type (debounced 60s per type)
                global _last_type_throttle_broadcast, _last_type_unthrottle_broadcast, _previously_failing_types
                _now_mono = time.monotonic()
                if _failing_types:
                    for _ftype in _failing_types:
                        if (_now_mono - _last_type_throttle_broadcast.get(_ftype, 0.0)) > 60:
                            _last_type_throttle_broadcast[_ftype] = _now_mono
                            _frate, _fsample = _shared_state.get_task_type_failure_rate(_ftype, window_minutes=15)
                            try:
                                await ws_manager.broadcast_to_all("type_throttle", {
                                    "type": _ftype,
                                    "failure_rate": round(_frate, 3),
                                    "sample_size": _fsample,
                                    "detail": f"Task type '{_ftype}' throttled: {_frate:.0%} failure rate (n={_fsample})",
                                })
                            except Exception:
                                logger.exception(f"[ORCHESTRATOR] Failed to broadcast type_throttle for '{_ftype}'")

                # Broadcast type_unthrottle for types that were gated on the prior cycle but have
                # now recovered (aged out of _failing_types). Debounced 60s per type like throttle.
                _recovered_types = _previously_failing_types - _failing_types
                for _rtype in _recovered_types:
                    if (_now_mono - _last_type_unthrottle_broadcast.get(_rtype, 0.0)) > 60:
                        _last_type_unthrottle_broadcast[_rtype] = _now_mono
                        _rrate, _rsample = _shared_state.get_task_type_failure_rate(_rtype, window_minutes=15)
                        logger.info(f"[ORCHESTRATOR] Per-type failure gate cleared: '{_rtype}' recovered ({_rrate:.0%} failure rate, n={_rsample})")
                        try:
                            await ws_manager.broadcast_to_all("type_unthrottle", {
                                "type": _rtype,
                                "failure_rate": round(_rrate, 3),
                                "sample_size": _rsample,
                                "detail": f"Task type '{_rtype}' recovered: {_rrate:.0%} failure rate (n={_rsample})",
                            })
                        except Exception:
                            logger.exception(f"[ORCHESTRATOR] Failed to broadcast type_unthrottle for '{_rtype}'")
                _previously_failing_types = set(_failing_types)

                # External health gate — skip dispatch when a critical dependency has been
                # confirmed down for 2+ consecutive health checks (avoids flapping on transient probes).
                # DB is excluded: if DB is down the orchestrator itself cannot function.
                _confirmed_down = get_confirmed_down_critical_deps()
                if _confirmed_down:
                    _dep_str = ", ".join(_confirmed_down)
                    logger.warning(f"[ORCHESTRATOR] External health gate: {_dep_str} confirmed down — skipping dispatch this cycle")
                    try:
                        await ws_manager.broadcast_to_all("health_gate_throttle", {
                            "reason": "external_dependency_down",
                            "detail": f"Critical dependencies down: {_dep_str}",
                            "deps": _confirmed_down,
                        })
                    except Exception:
                        logger.exception("[ORCHESTRATOR] Failed to broadcast health_gate_throttle")
                    # Sleep before retrying so we don't tight-loop the gate check
                    # at ~50 cycles/sec, which spams logs and trips the cycle-hung
                    # monitor (no cycle ever "completes" because we keep continuing).
                    # ORCHESTRATOR_THROTTLE_SECONDS matches the failure-rate gate
                    # at line 877 — both back off the same way.
                    await asyncio.sleep(ORCHESTRATOR_THROTTLE_SECONDS)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle.
                # Fetch the JSON path raw and group in Python — avoids Postgres
                # GROUP BY quirks with JSONB path expressions (SQLAlchemy was
                # emitting a SELECT that Postgres saw as referencing task.context
                # without a matching GROUP BY token).
                active_per_account: Dict[str, int] = {}
                _acct_id_rows = (await session.execute(
                    select(Task.context)
                    .where(
                        Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).all()
                for row in _acct_id_rows:
                    ctx = row[0] or {}
                    acct_id = ctx.get("llm_account_id") or ctx.get("max_plan_account_id")
                    if acct_id:
                        active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                async with _max_plan_pool_lock:
                    for acct in _account_by_id.values():
                        # Reconcile from DB — the pre-increment in select_max_plan_account() is always
                        # committed as PROVISIONING before the next cycle's reconciliation runs.
                        db_count = active_per_account.get(acct["id"], 0)
                        acct["active_tasks"] = db_count

                # Step 1: Find candidate tasks WITHOUT lock (read-only scan for dep filtering)
                # R4-W2 (H4): drop heavy JSONB (status_log_data, history blob)
                # via load_only so a 50-row candidate pull doesn't TOAST-fetch
                # the death-trace window from every QUEUED task. Include
                # ``status_log`` per memory ``reference_load_only_event_listeners.md``
                # — Task.status / latest_status_message setters trigger event
                # listeners that read status_log, which raises MissingGreenlet
                # if it's a deferred-loaded attribute (no async session bound).
                candidate_stmt = select(Task).where(
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval.is_(False)
                ).options(load_only(
                    Task.id, Task.status, Task.priority, Task.created_at,
                    Task.requires_approval, Task.retry_after_until,
                    Task.context, Task.depends_on, Task.launched_by_user_id,
                    Task.project_id, Task.payloads, Task.status_log,
                    Task.latest_status_message,
                    # Round 5 (2026-05-03): include parent_task_id — downstream
                    # filter_rollup_conflicts at orchestrator_filters.py:184 reads
                    # candidate.parent_task_id; deferring it triggers MissingGreenlet
                    # under async and crashes the entire dispatch cycle, freezing
                    # all QUEUED tasks. Same gotcha class as status_log per memory
                    # `reference_load_only_event_listeners.md` — load_only that
                    # excludes columns downstream code reads is fragile.
                    Task.parent_task_id,
                )).order_by(Task.priority.desc().nullslast(), Task.created_at.asc()).limit(max(slots_available * 2, 50))  # Fetch extra; NULL priority = lowest
                candidate_tasks = (await session.execute(candidate_stmt)).scalars().all()

                # Per-user pool availability cache. Rate-limit-origin retry_after
                # holds (set without _infra_wait) can be released early when the
                # dispatching user's own pool recovers. Checking the global pool
                # would release tasks whose user pool is still exhausted, only to
                # re-block them at dispatch.
                # Keyed by (user_id, model) — model=None is conservative and would
                # hold a Sonnet task when only Opus is rate-limited (or vice versa).
                _user_pool_available: Dict[Tuple[str, str | None], bool] = {}
                # R5-W5 MED-5: cache Project→user_id within a single dispatch
                # cycle. When task.launched_by_user_id is NULL, the previous
                # implementation called session.get(Project, ...) per task —
                # 50 NULL-launcher candidates = 50 redundant Project SELECTs
                # every cycle.
                _project_user_cache: Dict[uuid.UUID, str] = {}

                async def _user_has_available_account(task: Task) -> bool:
                    user_id = task.launched_by_user_id
                    if not user_id:
                        cached = _project_user_cache.get(task.project_id)
                        if cached is not None:
                            user_id = cached
                        else:
                            proj = await session.get(Project, task.project_id)
                            user_id = proj.user_id if proj else ""
                            _project_user_cache[task.project_id] = user_id
                    model = _extract_task_model(task)
                    cache_key = (user_id, model)
                    if cache_key in _user_pool_available:
                        return _user_pool_available[cache_key]
                    pool = _max_plan_pools.get(user_id, [])
                    available = any(_account_available_for_model(a, model) for a in pool)
                    _user_pool_available[cache_key] = available
                    return available

                # Step 2: Filter out tasks with unmet dependencies (no lock needed)
                eligible_ids = []
                for task in candidate_tasks:
                    # Skip tasks held back due to retry_after hold (audit W1:
                    # ``retry_after_until`` is a column now, so a stale
                    # in-memory copy can't shadow a fresh DB value).
                    if task.retry_after_until and task.retry_after_until > _utcnow():
                        # Release rate-limit holds early when the dispatching
                        # user's pool has recovered. _infra_wait holds wait
                        # for zone recovery.
                        if not (task.context or {}).get("_infra_wait") and await _user_has_available_account(task):
                            locked_task = await _lock_task_for_mutation(
                                session,
                                task.id,
                                allowed_statuses=(TaskStatus.QUEUED,),
                                reason="retry_after release",
                            )
                            if locked_task is None:
                                continue
                            _clear_retry_after_state(locked_task)
                            session.add(locked_task)
                            await session.commit()
                            task = locked_task
                            logger.info(f"[ORCHESTRATOR] Task {task.id} retry_after cleared early — account pool recovered")
                        else:
                            logger.info(f"[ORCHESTRATOR] Task {task.id} held back — retry_after={task.retry_after_until.isoformat()}")
                            continue
                    # Clear infra-wait flag when task becomes eligible again
                    if (task.context or {}).get("_infra_wait"):
                        locked_task = await _lock_task_for_mutation(
                            session,
                            task.id,
                            allowed_statuses=(TaskStatus.QUEUED,),
                            reason="infra_wait clear",
                        )
                        if locked_task is None:
                            continue
                        ctx = dict(locked_task.context or {})
                        ctx.pop("_infra_wait", None)
                        locked_task.context = ctx
                        _clear_retry_after_state(locked_task)
                        session.add(locked_task)
                        await session.commit()
                        task = locked_task

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
                                cancel_acct = get_task_llm_account_id(cancel_task)
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
                                    child_acct = get_task_llm_account_id(child)
                                    if child_acct and not (child.context or {}).get("_slot_released"):
                                        await release_max_plan_account(child_acct)
                                        child.context = {**(child.context or {}), "_slot_released": True}
                                    cancel_session.add(child)
                                await _maybe_create_failure_alert(
                                    cancel_task, cancel_session,
                                    "Cascade cancelled: dependency task failed"
                                )
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

                # Step 2.7: Defer tasks whose direction is held due to consecutive code failures
                eligible_ids = await filter_held_directions(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.72: Serialize migration-producing tasks per project to avoid
                # duplicate alembic revisions racing on the same down_revision.
                eligible_ids = await filter_migration_serialization(
                    eligible_ids, candidate_tasks, active_tasks_result, session, broadcast_to_project
                )

                # Step 2.75: Pre-dispatch dedup safety net for review/fix pipeline
                # tasks. Catches duplicates that raced past _spawn_pipeline_task's
                # spawn-time dedup (TOCTOU on active-task query, or different
                # original_task_id resolutions for the same effective chain).
                # Never cancels the sole survivor — see filter docstring.
                eligible_ids = await filter_pipeline_duplicates(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.8: Health-aware reordering — boost small/low-risk tasks when degraded.
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

                        # Load projects for candidate tasks (once per cycle) so we can
                        # compute real P(success) via predict_spawn_success instead of
                        # relying on the static small/low-risk bonus alone. Cache the
                        # score per (project_id, direction_id, scope) since many tasks
                        # may share the same direction within a dispatch cycle.
                        from app.spawn_calibration import predict_spawn_success
                        _project_ids = {
                            _task_by_id[tid].project_id
                            for tid in eligible_ids
                            if tid in _task_by_id and _task_by_id[tid].project_id is not None
                        }
                        _projects_by_id: dict = {}
                        if _project_ids:
                            _proj_rows = (await session.execute(
                                select(Project).where(Project.id.in_(_project_ids))
                            )).scalars().all()
                            _projects_by_id = {p.id: p for p in _proj_rows}
                        _confidence_cache: dict = {}

                        def _confidence_score(tid: str) -> float:
                            t = _task_by_id.get(tid)
                            if t is None:
                                return 0.5
                            ctx = t.context or {}
                            scope = ctx.get("scope") or "medium"
                            dir_id = str(ctx.get("direction_id") or "")
                            cache_key = (t.project_id, dir_id, scope)
                            if cache_key in _confidence_cache:
                                return _confidence_cache[cache_key]
                            proj = _projects_by_id.get(t.project_id)
                            direction: dict = {}
                            if proj and dir_id:
                                for d in (proj.directions or []):
                                    if str(d.get("id") or "") == dir_id:
                                        direction = d
                                        break
                            try:
                                score = predict_spawn_success(scope, direction, proj)
                            except Exception:
                                score = 0.5
                            _confidence_cache[cache_key] = score
                            return score

                        eligible_ids = sorted(
                            eligible_ids,
                            key=lambda tid: (
                                _confidence_score(tid),
                                _health_score_task(_task_by_id[tid]) if tid in _task_by_id else 0,
                            ),
                            reverse=True,
                        )
                        if eligible_ids != _original_order:
                            logger.info(
                                "[ORCHESTRATOR] Health-aware reordering applied (%s): "
                                "%d eligible tasks reordered by P(success) confidence",
                                ", ".join(_degradation_signals),
                                len(eligible_ids),
                            )

                        # Confidence floor: during degraded operation, skip tasks
                        # whose P(success) is below the configured floor so scarce
                        # slots go to higher-confidence work. Skipped tasks remain
                        # QUEUED and become eligible again next cycle.
                        _conf_floor = float(_runtime_config.get("dispatch_confidence_floor", 0.10) or 0.0)
                        if _conf_floor > 0:
                            _before_floor = list(eligible_ids)
                            eligible_ids = [
                                tid for tid in eligible_ids
                                if _confidence_score(tid) >= _conf_floor
                            ]
                            _skipped = len(_before_floor) - len(eligible_ids)
                            if _skipped > 0:
                                logger.info(
                                    "[ORCHESTRATOR] Confidence floor (%.2f) skipped "
                                    "%d task(s) during degraded operation (%s)",
                                    _conf_floor,
                                    _skipped,
                                    ", ".join(_degradation_signals),
                                )


                # Step 2.9: Pipeline-slot cap — review/fix chains are self-perpetuating
                # (review spawns fix spawns review, up to 16 iterations per PR), so
                # without a ceiling they can fill every VM slot and starve create/
                # research tasks. Defer (keep QUEUED) review/fix tasks once the
                # configured pipeline budget is exhausted.
                _max_pipeline_slots = _runtime_config.get("max_pipeline_slots")
                if _max_pipeline_slots is None:
                    _max_pipeline_slots = max(1, _runtime_config["max_concurrent_vms"] // 2)
                try:
                    _max_pipeline_slots = int(_max_pipeline_slots)
                except (TypeError, ValueError):
                    _max_pipeline_slots = max(1, _runtime_config["max_concurrent_vms"] // 2)
                if eligible_ids and _max_pipeline_slots > 0:
                    _pipeline_active = (await session.execute(
                        select(_func.count()).select_from(Task).where(
                            Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                            Task.payloads[0]["type"].as_string().in_(("review", "fix")),
                        )
                    )).scalar() or 0
                    _pipeline_budget = max(0, _max_pipeline_slots - _pipeline_active)
                    _types_for_cap = {
                        t.id: ((t.first_payload or {}).get("type") or "")
                        for t in candidate_tasks
                    }
                    _kept: list = []
                    _deferred_pipeline = 0
                    _pipeline_kept = 0
                    for tid in eligible_ids:
                        if _types_for_cap.get(tid) in ("review", "fix"):
                            if _pipeline_kept < _pipeline_budget:
                                _pipeline_kept += 1
                                _kept.append(tid)
                            else:
                                _deferred_pipeline += 1
                        else:
                            _kept.append(tid)
                    if _deferred_pipeline:
                        logger.info(
                            f"[ORCHESTRATOR] Pipeline-slot cap: deferred {_deferred_pipeline} "
                            f"review/fix task(s) (active={_pipeline_active}, cap={_max_pipeline_slots})"
                        )
                    eligible_ids = _kept

                # Slot reservation: ensure high-priority task types (e.g. create,
                # research) get a minimum share of slots when they're queued behind
                # self-perpetuating review/fix chains. Only activates when queue
                # pressure exists (more eligible tasks than open slots) and there
                # are reserved-type tasks waiting beyond the dispatch cutoff.
                _reserved_config = _runtime_config.get(
                    "reserved_slots_for_types", {"create": 1, "research": 1}
                ) or {}
                if _reserved_config and len(eligible_ids) > slots_available:
                    _types_by_id = {
                        t.id: ((t.first_payload or {}).get("type") or "")
                        for t in candidate_tasks
                    }
                    _protected = set(_reserved_config.keys())
                    _initial = list(eligible_ids[:slots_available])
                    _waiting = list(eligible_ids[slots_available:])
                    for _rtype, _rn in _reserved_config.items():
                        try:
                            _rn = int(_rn)
                        except (TypeError, ValueError):
                            continue
                        if _rn <= 0:
                            continue
                        _current = sum(1 for tid in _initial if _types_by_id.get(tid) == _rtype)
                        _needed = _rn - _current
                        if _needed <= 0:
                            continue
                        _promote = [tid for tid in _waiting if _types_by_id.get(tid) == _rtype][:_needed]
                        if not _promote:
                            continue
                        _displaced = 0
                        for i in range(len(_initial) - 1, -1, -1):
                            if _displaced >= len(_promote):
                                break
                            if _types_by_id.get(_initial[i]) not in _protected:
                                _initial.pop(i)
                                _displaced += 1
                        if _displaced == 0:
                            continue
                        _promote = _promote[:_displaced]
                        _promoted_set = set(_promote)
                        _waiting = [tid for tid in _waiting if tid not in _promoted_set]
                        _initial.extend(_promote)
                        logger.info(
                            f"[ORCHESTRATOR] Slot reservation: promoted {len(_promote)} '{_rtype}' "
                            f"task(s) (reserved={_rn}, displaced {_displaced} non-reserved)"
                        )
                    eligible_ids = _initial + _waiting

                # Respect slot limit after conflict filtering
                eligible_ids = eligible_ids[:slots_available]

                # Step 3: Re-read ONLY the tasks we're about to dispatch. This
                # scan is read-only; the row is locked with task_lock() at the
                # top of the per-task mutation block below.
                # R4-W2 (H4): same load_only filter as the candidate fetch
                # — heavy JSONB columns aren't needed for the dispatch gate
                # work either. ``status_log`` retained for event-listener
                # reads on Task.status mutations downstream.
                queued_tasks = []
                if eligible_ids:
                    dispatch_stmt = select(Task).where(
                        Task.id.in_(eligible_ids),
                        Task.status == TaskStatus.QUEUED,
                    ).options(load_only(
                        Task.id, Task.status, Task.priority, Task.created_at,
                        Task.requires_approval, Task.retry_after_until,
                        Task.context, Task.depends_on, Task.launched_by_user_id,
                        Task.project_id, Task.payloads, Task.status_log,
                        Task.latest_status_message,
                        Task.parent_task_id,  # see candidate_stmt comment above
                    ))
                    queued_tasks = list((await session.execute(dispatch_stmt)).scalars().all())

                logger.info(f"[ORCHESTRATOR] Cycle: {len(queued_tasks)} queued, {active_count} active, {slots_available} slots, pool={len(_account_by_id)} accounts")
                # Snapshot per-task gate fields BEFORE entering the dispatch loop.
                # Inside the loop, _lock_task_for_mutation calls session.rollback()
                # on contention/status mismatch, which expires every attribute on
                # every object loaded in this session — including queued_task.payloads.
                # The next iteration's queued_task.first_payload would then trigger a
                # sync lazy-load from async context (bug pattern documented at
                # ACTIVE_TASKS_LIGHT_COLUMNS comment above) and crash the cycle.
                _queued_task_gate_info: list[tuple] = [
                    (qt.id, qt.first_payload.get("type", ""))
                    for qt in queued_tasks
                ]
                # Gauge query: skip autoflush so pending UPDATEs from earlier in the
                # cycle don't get pushed and contend on a row another transaction holds
                # (LockNotAvailableError). The count is a snapshot for metrics, not a
                # transactional read — pending state being absent is the right answer.
                # session.no_autoflush is a sync context manager (toggles a flag,
                # no I/O), so use plain ``with`` even on an AsyncSession.
                with session.no_autoflush:
                    _shared_state.tasks_queued_gauge = (await session.execute(
                        select(_func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
                    )).scalar() or 0
                # tasks_active_gauge already set above after active_count is computed

                # Zone pre-check: skip dispatch entirely if all GCP zones are in cooldown
                if queued_tasks and not any_zone_available():
                    global _last_zone_exhaustion_log
                    # Audit W1/F8: use _utcnow() (naive UTC) so subtraction
                    # against soonest_zone_recovery() (also naive) doesn't
                    # raise on aware/naive mismatch.
                    now_check = _utcnow()
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

                # All-types-blocked detection: if every queued task's type is in _failing_types,
                # the pipeline is silently stalled — create an operator alert.
                if _failing_types and queued_tasks:
                    _queued_types = {
                        t.first_payload.get("type", "") for t in queued_tasks
                        if t.first_payload.get("type", "")
                    }
                    if _queued_types and _queued_types.issubset(_failing_types):
                        _blocked_summary = ", ".join(sorted(_queued_types))
                        logger.warning(
                            f"[ORCHESTRATOR] All queued task types blocked by per-type failure gate: {_blocked_summary}"
                        )
                        try:
                            from app.models import Alert, AlertSeverity
                            _all_blocked_title = "Pipeline stalled — all task types throttled"
                            _all_blocked_msg = (
                                f"All queued task types are blocked by the per-type failure gate: {_blocked_summary}. "
                                "Each type has >70% failure rate in the last 15 minutes. The pipeline will not dispatch "
                                "new tasks until failure rates drop."
                            )
                            _projects = (await session.execute(select(Project))).scalars().all()
                            for _proj in _projects:
                                _existing_alert = (await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == _proj.id,
                                        Alert.task_id.is_(None),
                                        Alert.severity == AlertSeverity.ERROR,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == _all_blocked_title,
                                    )
                                )).scalars().first()
                                if _existing_alert is None:
                                    _alert = Alert(
                                        project_id=_proj.id,
                                        severity=AlertSeverity.ERROR,
                                        title=_all_blocked_title,
                                        message=_all_blocked_msg,
                                    )
                                    session.add(_alert)
                                    await broadcast_to_project(
                                        str(_proj.id),
                                        "alert_created",
                                        {"alert_id": str(_alert.id), "severity": "ERROR", "title": _all_blocked_title},
                                    )
                        except Exception:
                            logger.exception("[ORCHESTRATOR] Failed to create all-types-blocked project alerts")

                # Track per-cycle counts so a full stall on Max-plan account exhaustion
                # fires an alert+broadcast once, rather than only per-task retry_after.
                _cycle_accounts_blocked = 0
                _cycle_dispatched = 0
                _cycle_earliest_recovery: datetime | None = None
                _cycle_blockage_reasons: set[str] = set()

                for _qt_id, _task_type_for_gate in _queued_task_gate_info:
                    # Per-type failure gate: skip tasks whose type is currently failing
                    # (global 80% backstop already handled above; this skips only the bad type).
                    # Use the snapshot tuple — never read .first_payload off a session-bound
                    # object inside this loop, since _lock_task_for_mutation's rollback would
                    # expire it and trigger a sync lazy-load that crashes the cycle.
                    if _task_type_for_gate and _task_type_for_gate in _failing_types:
                        logger.info(
                            f"[ORCHESTRATOR] Skipping task {_qt_id} "
                            f"(type={_task_type_for_gate}) — per-type failure gate active"
                        )
                        continue

                    task = await _lock_task_for_mutation(
                        session,
                        _qt_id,
                        allowed_statuses=(TaskStatus.QUEUED,),
                        reason="dispatch",
                    )
                    if task is None:
                        continue
                    if task.requires_approval:
                        await session.rollback()
                        continue

                    # Phase 2c: warm-VM continuity skip-provision branch.
                    # Three outcomes when warm_target_vm_id is set on the task:
                    #   (a) deadline expired → clear warm fields, fall through
                    #       to provision (cold path).
                    #   (b) target VM heartbeated within 90s → skip provision
                    #       entirely. Task stays QUEUED and the agent on the
                    #       target VM will pick it up via /api/agent/next-role.
                    #   (c) target VM unresponsive → clear warm fields, fall
                    #       through to provision (cold path).
                    _warm_ctx = task.context or {}
                    _warm_target_vm_id = _warm_ctx.get("warm_target_vm_id")
                    if _warm_target_vm_id:
                        _warm_deadline_iso = _warm_ctx.get("warm_handoff_deadline")
                        _warm_expired = False
                        if _warm_deadline_iso:
                            try:
                                _warm_deadline = datetime.fromisoformat(str(_warm_deadline_iso))
                                if _warm_deadline.tzinfo is not None:
                                    _warm_deadline = _warm_deadline.astimezone(timezone.utc).replace(tzinfo=None)
                                _warm_expired = _utcnow() > _warm_deadline
                            except (ValueError, TypeError):
                                _warm_expired = True
                        else:
                            _warm_expired = True

                        if not _warm_expired and _shared_state.is_vm_alive(_warm_target_vm_id):
                            # (b) Skip provision: the warm VM is alive. Leave
                            # the task QUEUED — the agent's /next-role poll
                            # will claim it without a fresh provision.
                            logger.info(
                                f"[ORCHESTRATOR] Warm-pin skip-provision for task {task.id}: "
                                f"target_vm={_warm_target_vm_id} (heartbeat fresh, deadline {_warm_deadline_iso})"
                            )
                            await session.rollback()
                            continue

                        # (a) or (c): clear warm fields and fall through.
                        _new_ctx = {
                            k: v for k, v in _warm_ctx.items()
                            if k not in ("warm_target_vm_id", "warm_target_pr", "warm_handoff_deadline")
                        }
                        task.context = _new_ctx
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(task, "context")
                        _clear_reason = "deadline_expired" if _warm_expired else "vm_unresponsive"
                        # Phase 2e observability: track clear-out rate so we
                        # can detect warm-VM instability. Sustained >5/hour
                        # means the warm chain is failing more than expected.
                        # Defer record_warm_pin_clear until AFTER the commit
                        # that persists the warm-field clear lands — if the
                        # commit fails and we retry next cycle we'd otherwise
                        # double-count the metric (Issue 11).
                        _pending_warm_pin_clear: tuple[str, str] | None = (
                            _warm_target_vm_id, _clear_reason
                        )
                        logger.info(
                            f"[ORCHESTRATOR] Warm-pin cleared for task {task.id}: "
                            f"target_vm={_warm_target_vm_id} reason={_clear_reason}"
                        )
                    else:
                        _pending_warm_pin_clear = None

                    # Ensure VM ID exists (cleared during stale PROVISIONING recovery
                    # OR set to None for warm-pinned tasks whose pin was just cleared).
                    if not task.vm_instance_id:
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    logger.info(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Resolve the task's LLM runtime identity and select an eligible account.
                    # Two sources feed the effective blacklist:
                    #  - blacklisted_accounts: pruneable (rate-limit recovers → reusable)
                    #  - provision_failed_accounts: sticky (silent pre-claim deaths stay
                    #    unavailable for this task — prune logic below would otherwise
                    #    reselect the same broken account every attempt).
                    _blacklist = (task.context or {}).get("blacklisted_accounts")
                    _sticky = (task.context or {}).get("provision_failed_accounts") or []
                    # Launcher pays; fall back to project owner for autonomous tasks.
                    # R6-W3-D: reuse the cycle-local _project_user_cache built during
                    # eligibility filtering so we don't re-issue a Project SELECT per
                    # dispatched task (extends R5-W5 MED-5 from the eligibility scan
                    # to the dispatch site).
                    if task.launched_by_user_id:
                        dispatch_user_id = task.launched_by_user_id
                    else:
                        cached_owner = _project_user_cache.get(task.project_id)
                        if cached_owner is not None:
                            dispatch_user_id = cached_owner
                        else:
                            task_project = await session.get(Project, task.project_id)
                            dispatch_user_id = task_project.user_id if task_project else ""
                            _project_user_cache[task.project_id] = dispatch_user_id
                    task_model = _extract_task_model(task)
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
                    _effective_blacklist = list({*(_blacklist or []), *_sticky}) or None
                    runtime_selection = await select_llm_runtime_for_task(
                        user_id=dispatch_user_id,
                        task=task,
                        runtime_config=_runtime_config,
                        blacklist=_effective_blacklist,
                    )
                    runtime_secret = None
                    claude_config_json = None
                    account_id = None
                    runtime_provider = None
                    runtime_auth_mode = None
                    runtime_model = task_model
                    runtime_execution_mode = None
                    runtime_provider_metadata = None
                    runtime_provider_settings: dict | None = None
                    if runtime_selection:
                        runtime_provider = runtime_selection.provider
                        runtime_auth_mode = runtime_selection.auth_mode
                        runtime_model = runtime_selection.model or runtime_model
                        runtime_execution_mode = runtime_selection.execution_mode
                        runtime_provider_settings = runtime_selection.settings
                    account = runtime_selection.account if runtime_selection else None
                    if account:
                        account_id = account["id"]
                        runtime_secret = account.get("runtime_secret") or account.get("oauth_token")
                        claude_config_json = account.get("claude_config_json")
                        runtime_provider_metadata = account.get("provider_metadata") or {}
                        logger.info(
                            f"[ORCHESTRATOR] Using LLM account '{account['name']}' "
                            f"(provider={runtime_provider}, auth_mode={runtime_auth_mode}, model={runtime_model})"
                        )
                    else:
                        # No available account — either all disabled/blacklisted or none configured
                        user_pool = _max_plan_pools.get(dispatch_user_id, [])
                        provider_accounts = user_pool
                        if user_pool and runtime_provider:
                            provider_handler = get_provider(runtime_provider)
                            provider_accounts = [
                                acct for acct in user_pool
                                if str(acct.get("provider") or "anthropic") == runtime_provider
                            ]
                            ready_provider_accounts = [
                                acct for acct in provider_accounts
                                if acct.get("enabled", True) and provider_handler.account_has_credentials(acct)
                            ]
                        else:
                            ready_provider_accounts = list(provider_accounts)
                        if user_pool and ready_provider_accounts:
                            # Accounts exist but all blocked — notify user and skip
                            logger.info(f"[ORCHESTRATOR] All accounts blocked for task {task.id} — skipping (will retry next cycle)")
                            from app.max_plan_pool import summarize_pool_state, describe_pool_blockage  # noqa: PLC0415
                            _pool_summary = summarize_pool_state(task_model, user_id=dispatch_user_id)
                            earliest_recovery = _pool_summary["earliest_recovery"]
                            stall_msg = describe_pool_blockage(task_model, user_id=dispatch_user_id)
                            _cycle_blockage_reasons.add(stall_msg)
                            if earliest_recovery:
                                stall_delay_minutes = rate_limit_retry_minutes(earliest_recovery)
                            else:
                                stall_delay_minutes = 2
                            logger.info(
                                "[ORCHESTRATOR] pool_exhausted task=%s model=%s %s",
                                str(task.id)[:8], task_model, _pool_summary,
                            )
                            # Retry delay based on earliest known recovery to avoid
                            # hot-looping every 2 min when recovery is hours away.
                            # set_retry_after clamps to RETRY_AFTER_MAX_MINUTES.
                            stall_ctx = dict(task.context or {})
                            set_retry_after(stall_ctx, stall_delay_minutes)
                            task.context = stall_ctx
                            # Escalate when a task is stuck cycling through retry_after holds.
                            # Each reset is up to RETRY_AFTER_MAX_MINUTES (30m), so 8 resets ≈ 4h.
                            # At the secondary fail_threshold (default 2x alert), auto-fail so the
                            # requeue/retry machinery can take over with fresh context instead of
                            # blocking dispatch indefinitely.
                            _auto_failed = False
                            try:
                                _resets = int(stall_ctx.get("_retry_after_resets") or 0)
                                _reset_threshold = _runtime_config.get("retry_after_reset_alert_threshold", 8)
                                _fail_threshold = _runtime_config.get(
                                    "retry_after_reset_fail_threshold", _reset_threshold * 2
                                )
                                _action = _compute_retry_after_escalation_action(
                                    _resets, _reset_threshold, _fail_threshold
                                )
                                if _action in ("alert", "fail"):
                                    from app.models import Alert, AlertSeverity  # noqa: PLC0415
                                    from app.max_plan_pool import describe_pool_blockage  # noqa: PLC0415
                                    _blockage = describe_pool_blockage(task_model, user_id=dispatch_user_id)
                                    _hold_min = (_utcnow() - task.created_at).total_seconds() / 60 if task.created_at else 0.0
                                if _action == "fail":
                                    from app.failure_classification import persist_failure_category  # noqa: PLC0415
                                    _exhaust_msg = (
                                        f"Auto-failed after {_resets} retry_after cycles "
                                        f"(threshold {_fail_threshold}); held {_hold_min:.0f}m. "
                                        f"Pool state: {_blockage}."
                                    )
                                    task.status = TaskStatus.FAILED
                                    task.latest_status_message = _exhaust_msg[:500]
                                    persist_failure_category(task, "retry_after_exhausted")
                                    _acct_id = get_task_llm_account_id(task)
                                    if _acct_id:
                                        await release_max_plan_account(_acct_id)
                                    session.add(task)
                                    await _maybe_create_failure_alert(task, session, _exhaust_msg[:200])
                                    try:
                                        record_task_result(
                                            success=False,
                                            failure_class="retry_after_exhausted",
                                            task_id=str(task.id),
                                            task_type=(task.first_payload or {}).get("type", ""),
                                        )
                                    except Exception:
                                        logger.exception(
                                            f"[ORCHESTRATOR] Failed to record retry_after_exhausted result for task {task.id}"
                                        )
                                    await broadcast_to_project(
                                        str(task.project_id),
                                        "task_updated",
                                        {"task_id": str(task.id), "status": task.status, "message": task.latest_status_message},
                                    )
                                    logger.warning(
                                        f"[ORCHESTRATOR] Task {task.id} auto-failed after {_resets} retry_after cycles "
                                        f"(threshold={_fail_threshold}, blockage={_blockage})"
                                    )
                                    _auto_failed = True
                                elif _action == "alert":
                                    _stuck_title = f"Task stuck in retry_after cycle: {task.title or str(task.id)[:8]}"
                                    _existing_stuck = (await session.execute(
                                        select(Alert).where(
                                            Alert.task_id == task.id,
                                            Alert.title == _stuck_title,
                                            Alert.dismissed_at.is_(None),
                                        )
                                    )).scalars().first()
                                    if _existing_stuck is None:
                                        _stuck_msg = (
                                            f"Task {task.id} has been cycling through retry_after holds "
                                            f"{_resets} times (threshold {_reset_threshold}); held for "
                                            f"{_hold_min:.0f}m total. Pool state: {_blockage}."
                                        )
                                        _stuck_alert = Alert(
                                            project_id=task.project_id,
                                            severity=AlertSeverity.WARNING,
                                            title=_stuck_title,
                                            message=_stuck_msg,
                                            task_id=task.id,
                                        )
                                        session.add(_stuck_alert)
                                        await broadcast_to_project(
                                            str(task.project_id),
                                            "alert_created",
                                            {"alert_id": str(_stuck_alert.id), "severity": "WARNING", "title": _stuck_title},
                                        )
                                        logger.warning(
                                            f"[ORCHESTRATOR] Retry-after escalation alert for task {task.id} "
                                            f"(resets={_resets}, blockage={_blockage})"
                                        )
                            except Exception:
                                logger.exception(f"[ORCHESTRATOR] Failed to evaluate retry_after escalation for task {task.id}")
                            if _auto_failed:
                                await session.commit()
                                if _pending_warm_pin_clear is not None:
                                    _shared_state.record_warm_pin_clear(*_pending_warm_pin_clear)
                                    _pending_warm_pin_clear = None
                                continue
                            if task.latest_status_message != stall_msg:
                                # Update directly on the locked task object (separate session would deadlock)
                                task.latest_status_message = stall_msg
                                await broadcast_to_project(str(task.project_id), "task_updated",
                                    {"task_id": str(task.id), "status": task.status, "message": stall_msg})
                            session.add(task)
                            _cycle_accounts_blocked += 1
                            if earliest_recovery and (
                                _cycle_earliest_recovery is None or earliest_recovery < _cycle_earliest_recovery
                            ):
                                _cycle_earliest_recovery = earliest_recovery
                            await session.commit()
                            if _pending_warm_pin_clear is not None:
                                _shared_state.record_warm_pin_clear(*_pending_warm_pin_clear)
                                _pending_warm_pin_clear = None
                            continue
                        # No accounts at all — fall back to raw provider credentials when present
                        (
                            runtime_secret,
                            runtime_provider,
                            runtime_auth_mode,
                            runtime_model,
                            runtime_execution_mode,
                            runtime_provider_metadata,
                        ) = _resolve_env_runtime_fallback(
                            provider=runtime_provider,
                            model=runtime_model or task_model,
                            execution_mode=runtime_execution_mode,
                            task=task,
                            runtime_config=_runtime_config,
                        )
                        if runtime_secret:
                            logger.info(
                                f"[ORCHESTRATOR] No LLM pool account available, using env credential fallback "
                                f"(provider={runtime_provider}, auth_mode={runtime_auth_mode}, model={runtime_model})"
                            )
                        else:
                            logger.warning(f"[ORCHESTRATOR] No credentials available — skipping task {task.id}")
                            stall_msg = _missing_runtime_credentials_stall_reason(
                                user_pool_count=len(user_pool),
                                provider_account_count=len(provider_accounts),
                                ready_provider_account_count=len(ready_provider_accounts),
                                runtime_provider=runtime_provider,
                            )
                            _cycle_blockage_reasons.add(stall_msg)
                            _cycle_accounts_blocked += 1
                            # Update directly on the locked task object (separate session would deadlock)
                            task.latest_status_message = (
                                "Waiting for credentials — add an LLM account or set "
                                f"{_provider_env_credential_hint(runtime_provider)}"
                            )
                            session.add(task)
                            await session.commit()
                            if _pending_warm_pin_clear is not None:
                                _shared_state.record_warm_pin_clear(*_pending_warm_pin_clear)
                                _pending_warm_pin_clear = None
                            continue

                    if _sync_task_payload_runtime_fields(
                        task,
                        model=runtime_model,
                        execution_mode=runtime_execution_mode,
                    ):
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(task, "payloads")
                        session.add(task)

                    task.context = apply_task_llm_runtime_context(
                        task.context,
                        account_id=account_id,
                        provider=runtime_provider,
                        auth_mode=runtime_auth_mode,
                        model=runtime_model,
                        execution_mode=runtime_execution_mode,
                    )

                    # Mark as PROVISIONING to prevent other workers from picking it up.
                    # last_heartbeat is intentionally NOT set here — it must reflect real
                    # agent activity only. The reaper uses vm_provisioned_at as the clock
                    # for the pre-claim window so it can distinguish "VM created but agent
                    # never claimed" from "agent claimed and crashed".
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = _utcnow()
                    task.vm_provisioned_at = _utcnow()
                    # vm_spot must be written here — it's read by ~30 cost-rate sites
                    # (admin_digest, ci_completion, task_completion, etc.) gating
                    # SPOT_RATE vs ON_DEMAND_RATE. Without this write every cost
                    # dashboard overstates by ~3.3x when the orchestrator runs in
                    # SPOT mode. compute.VM_PROVISIONING_MODEL is the source of truth
                    # (env-driven; STANDARD by default).
                    task.vm_spot = (VM_PROVISIONING_MODEL == "SPOT")
                    task.last_heartbeat = None
                    _shared_state.deploy_monitoring["last_dispatch_time"] = _utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        # active_tasks already pre-incremented in select_max_plan_account()
                        logger.info(f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM")
                        # Issue 11: emit warm-pin-clear metric ONLY after the
                        # commit that persists the warm-field clear succeeds.
                        # On commit failure → rollback path below, the metric
                        # stays unrecorded so the next dispatch cycle can fire
                        # it cleanly without double-counting.
                        if _pending_warm_pin_clear is not None:
                            _shared_state.record_warm_pin_clear(*_pending_warm_pin_clear)
                            _pending_warm_pin_clear = None
                    except Exception as e:
                        import traceback
                        logger.error(f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}", exc_info=True)
                        traceback.print_exc()
                        # Release the pre-incremented slot on commit failure
                        if account_id:
                            await release_max_plan_account(account_id)
                        task.context = clear_task_llm_runtime_context(task.context)
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID,
                                                    payloads: list, secret_value: str | None, config_json: str | None,
                                                    acct_id_for_release: str,
                                                    llm_provider: str | None,
                                                    llm_auth_mode: str | None,
                                                    llm_model: str | None,
                                                    llm_execution_mode: str | None,
                                                    llm_provider_metadata: dict | None,
                                                    llm_provider_settings: dict | None):
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
                                prepare_and_store_agent_secret(
                                    vm_id, t_id, secret_value, config_json, t_type,
                                    provider=llm_provider, auth_mode=llm_auth_mode, model=llm_model,
                                    execution_mode=llm_execution_mode, provider_metadata=llm_provider_metadata,
                                    provider_settings=llm_provider_settings,
                                    project_id=str(task.project_id),  # audit W3/E10
                                ),
                            )

                            success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                               oauth_token=secret_value, claude_config_json=config_json,
                                                               task_type=t_type, secret_version_name=secret_version_name,
                                                               provider=llm_provider, auth_mode=llm_auth_mode,
                                                               model=llm_model, execution_mode=llm_execution_mode,
                                                               provider_metadata=llm_provider_metadata,
                                                               provider_settings=llm_provider_settings)
                            if success:
                                # Verify the task still wants this VM — stale recovery may have
                                # requeued the task with a new vm_id while we were provisioning.
                                # If so, this VM is an orphan: delete it immediately.
                                try:
                                    async with AsyncSessionLocal() as hb_session:
                                        hb_task = await _lock_provisioning_task_for_vm(hb_session, t_id, vm_id)
                                        if hb_task is None:
                                            current_task = await hb_session.get(Task, t_id)
                                            if not current_task or current_task.vm_instance_id != vm_id:
                                                current_vm = current_task.vm_instance_id if current_task else "gone"
                                                logger.warning(f"[ORCHESTRATOR] VM {vm_id} created but task {t_id} now has vm_id={current_vm} — deleting orphan")
                                                await _ensure_vm_deleted(vm_id, reason="vm_id_mismatch")
                                                success = False
                                                return
                                        else:
                                            # Refresh vm_provisioned_at to the point the GCE API
                                            # confirmed the VM is running. This gives the agent a
                                            # fresh window to claim before the reaper considers it
                                            # a silent startup failure. last_heartbeat is left alone
                                            # so only real agent activity writes it.
                                            hb_task.vm_provisioned_at = _utcnow()
                                            # vm_spot mirrors orchestrator-mode at provision-confirm time;
                                            # see comment at the dispatch-site write above.
                                            hb_task.vm_spot = (VM_PROVISIONING_MODEL == "SPOT")
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
                            _cleanup_key = f"orphan_cleanup_{vm_id}"
                            _cleanup_task = asyncio.create_task(_delayed_orphan_cleanup(vm_id))
                            _shared_state.background_tasks[_cleanup_key] = _cleanup_task
                            # Drop the dict entry once the task finishes so we don't retain
                            # completed asyncio.Task objects indefinitely (seen ~100s of these
                            # accumulating per day of provision churn — direct memory growth).
                            _cleanup_task.add_done_callback(
                                lambda _t, _k=_cleanup_key: _shared_state.background_tasks.pop(_k, None)
                            )
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
                                record_task_result(success=False, failure_class="PROVISION_FAILURE", task_id=str(t_id), task_type=t_type)
                                # Hoist zone lookup so it can be saved to context and used for zone failure recording
                                prov_zone = get_vm_zone(vm_id)
                                # Capture serial port logs before VM deletion (best-effort)
                                serial_log = None
                                try:
                                    if prov_zone and prov_zone != "unknown":
                                        serial_log = await asyncio.to_thread(_capture_serial_for_vm, vm_id, prov_zone)
                                        if serial_log:
                                            logger.error(f"[ORCHESTRATOR] Serial log for failed VM {vm_id}:\n{serial_log}")
                                except Exception:
                                    logger.exception(f"[ORCHESTRATOR] Failed to capture serial log for VM {vm_id}")
                                # Requeue with retry tracking — zone exhaustion is transient
                                try:
                                    async with AsyncSessionLocal() as inner_session:
                                        failed_task = await _lock_provisioning_task_for_vm(inner_session, t_id, vm_id)
                                        if failed_task:
                                            ctx = dict(failed_task.context or {})
                                            if prov_zone:
                                                ctx["provision_zone"] = prov_zone
                                            if serial_log:
                                                ctx["last_serial_log"] = serial_log

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
                                                    # Audit W1/F8: _utcnow() for naive subtraction
                                                    wait_sec = max(60, (recovery - _utcnow()).total_seconds() + 60)
                                                    delay_min = min(wait_sec / 60, 30)
                                                else:
                                                    delay_min = 10
                                                set_retry_after(failed_task, delay_min)
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
                                                    try:
                                                        record_task_result(
                                                            success=False,
                                                            failure_class="PROVISION_FAILURE",
                                                            task_id=str(failed_task.id),
                                                            task_type=(failed_task.first_payload or {}).get("type", ""),
                                                        )
                                                        _direction_id = str((failed_task.context or {}).get("direction_id") or "")
                                                        if _direction_id:
                                                            from app.shared_state import record_direction_result as _rdr
                                                            _rdr(
                                                                _direction_id,
                                                                success=False,
                                                                failure_class="PROVISION_FAILURE",
                                                                project_id=str(failed_task.project_id) if failed_task.project_id else "",
                                                            )
                                                    except Exception:
                                                        logger.exception(f"[ORCHESTRATOR] Failed to record provision-exhaustion result for task {t_id}")
                                                    logger.error(f"[ORCHESTRATOR] Task {t_id} provision failed {prov_failures} times — permanent failure")
                                                else:
                                                    # Requeue for retry
                                                    failed_task.status = TaskStatus.QUEUED
                                                    failed_task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                                    failed_task.last_heartbeat = None
                                                    failed_task.started_at = None
                                                    # Back off 2 min per failure to let zones recover
                                                    failed_task.context = ctx
                                                    set_retry_after(failed_task, 2 * prov_failures)
                                                    ctx = dict(failed_task.context or {})
                                                    failed_task.latest_status_message = f"Provision failed (attempt {prov_failures}/{max_prov_retries}), requeued: {provision_err or 'unknown'}"
                                                    logger.warning(f"[ORCHESTRATOR] Task {t_id} provision failed (attempt {prov_failures}) — requeuing")

                                            if failed_task.status == TaskStatus.QUEUED:
                                                ctx = clear_task_llm_runtime_context(ctx)
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
                        task.payloads, runtime_secret, claude_config_json, account_id,
                        runtime_provider, runtime_auth_mode, runtime_model,
                        runtime_execution_mode, runtime_provider_metadata,
                        runtime_provider_settings,
                    ))
                    _provision_coroutines[str(task.id)] = provision_coro
                    provision_coro.add_done_callback(lambda _, tid=str(task.id): _provision_coroutines.pop(tid, None))

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING}
                    )
                    _cycle_dispatched += 1

                # All-accounts-blocked stall detection: if at least one task was
                # skipped for rate-limited accounts and nothing dispatched, the
                # pipeline is silently stalled. Emit one alert/broadcast/notification
                # per episode (debounced until any task dispatches again).
                global _all_accounts_blocked_alerted
                _stall_action = _compute_all_accounts_blocked_action(
                    _cycle_accounts_blocked, _cycle_dispatched, _all_accounts_blocked_alerted
                )
                if _stall_action == "reset":
                    if _all_accounts_blocked_alerted:
                        _all_accounts_blocked_alerted = False
                        logger.info(
                            "[ORCHESTRATOR] Rate-limit stall resolved — "
                            f"{_cycle_dispatched} task(s) dispatched after prior all-accounts-blocked episode"
                        )
                        try:
                            from app.broadcast import manager as _ws_manager  # noqa: PLC0415
                            await _ws_manager.broadcast_to_all("rate_limit_stall_resolved", {
                                "dispatched_count": _cycle_dispatched,
                                "detail": "Pipeline recovered: tasks dispatching after all-accounts-blocked stall",
                            })
                        except Exception:
                            logger.exception("[ORCHESTRATOR] Failed to broadcast rate_limit_stall_resolved")
                elif _stall_action == "fire":
                    _all_accounts_blocked_alerted = True
                    await _emit_all_accounts_blocked_stall(
                        session, _cycle_earliest_recovery, _cycle_blockage_reasons or None
                    )

                # Per-cycle broadcast so operators can see held-task counts in
                # the admin UI without tailing logs. Distinct from the debounced
                # pipeline_throttle alert: this fires every cycle any tasks are held.
                if _cycle_accounts_blocked > 0:
                    try:
                        from app.broadcast import manager as _ws_manager  # noqa: PLC0415
                        await _ws_manager.broadcast_to_all("tasks_held_by_rate_limit", {
                            "count": _cycle_accounts_blocked,
                            "earliest_recovery": (
                                _cycle_earliest_recovery.isoformat()
                                if _cycle_earliest_recovery else None
                            ),
                        })
                    except Exception:
                        logger.exception("[ORCHESTRATOR] Failed to broadcast tasks_held_by_rate_limit")

                # Flush any status message updates from skipped tasks (e.g., "All accounts rate-limited")
                if session.dirty:
                    await session.commit()

                # Check tasks waiting on CI (review approved, CI pending)
                CI_WAIT_TIMEOUT_MINUTES = 20
                ci_waiting_task_ids = (await session.execute(
                    select(Task.id).where(Task.status == TaskStatus.EVALUATING).limit(200)
                )).scalars().all()
                for ci_task_id in ci_waiting_task_ids:
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling",
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
                    if not ci_loop.get("ci_waiting"):
                        await session.rollback()
                        continue
                    if ci_loop.get("conflict_fix_spawned"):
                        await session.rollback()
                        continue  # Already handled — fix task spawned for merge conflicts
                    if ci_loop.get("ci_fix_spawned"):
                        await session.rollback()
                        continue  # Already handled — fix task spawned for CI failure
                    if ci_loop.get("merge_rule_blocked"):
                        await session.rollback()
                        continue  # Merge blocked by branch protection — requires manual intervention
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        await session.rollback()
                        continue

                    # Ensure ci_wait_started is always set — some paths set ci_waiting=True
                    # without setting ci_wait_started, which silently disables the timeout.
                    # Commit immediately, then reacquire task_lock before more mutations below.
                    if not ci_loop.get("ci_wait_started"):
                        from app import pause_clock  # noqa: PLC0415
                        ci_loop["ci_wait_started"] = _utcnow().isoformat()
                        ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(session)
                        ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                        # Without flag_modified, JSONB nested-dict mutations
                        # may not be detected by SQLAlchemy when the new outer
                        # dict shares the same review_loop reference as the
                        # old. Drives the 2026-04-24 "CI wait exceeded 20min"
                        # flap where escalated/ci_last_polled never persisted.
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        ci_task = await _lock_task_for_mutation(
                            session,
                            ci_task_id,
                            allowed_statuses=(TaskStatus.EVALUATING,),
                            reason="CI polling after wait-start persist",
                        )
                        if ci_task is None:
                            continue
                        ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})

                    # Exponential backoff: skip if not enough time has elapsed since last poll.
                    # Webhooks handle the fast path — polling is a fallback.
                    ci_poll_interval = ci_loop.get("ci_poll_interval", 30)
                    ci_last_polled = ci_loop.get("ci_last_polled")
                    if ci_last_polled:
                        try:
                            _last_polled_dt = datetime.fromisoformat(ci_last_polled)
                            if (_utcnow() - _last_polled_dt).total_seconds() < ci_poll_interval:
                                await session.rollback()
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
                                # See note at the ci_wait_started persist above
                                # — without flag_modified the escalated flag
                                # silently fails to persist, the next cycle
                                # reads escalated=False, and this whole block
                                # re-fires every 2-4s, which is exactly the
                                # "CI wait exceeded 20min" flap pattern.
                                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                flag_modified(ci_task, "context")
                                ci_task.latest_status_message = f"CI wait exceeded {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — retrying, alert sent"
                                session.add(ci_task)
                                await _maybe_create_failure_alert(
                                    ci_task, session,
                                    f"Rollup PR #{ci_pr_number} stuck in CI-wait for >{CI_WAIT_TIMEOUT_MINUTES}min — still retrying"
                                )
                                await session.commit()
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after escalation persist",
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
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
                            # ── Phase B3: release lock around get_pull_request ──
                            # GitHub rate-limit can hold this call for minutes;
                            # holding the lock starves concurrent heartbeats on
                            # the same task. Release, fetch, re-acquire-validate.
                            # R5-W2-A: content snapshot, not updated_at —
                            # heartbeat onupdate would false-defer every poll.
                            ci_pre_pr_snapshot = _capture_ci_polling_snapshot(ci_task)
                            await session.commit()
                            try:
                                pr_data = await get_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                            except HTTPException as exc:
                                # Re-acquire to apply error-path mutations
                                ci_task = await _lock_task_for_mutation(
                                    session, ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after PR fetch error",
                                    expected_snapshot=ci_pre_pr_snapshot,
                                    snapshot_fn=_capture_ci_polling_snapshot,
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
                                if exc.status_code in (404, 410):
                                    _pr_gone_msg = f"PR #{ci_pr_number} no longer exists (deleted or repo archived)"
                                    logger.exception(f"[CI] {_pr_gone_msg} for task {ci_task.id}")
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.latest_status_message = _pr_gone_msg
                                    from app.failure_classification import persist_failure_category
                                    from app.routers.agent_status import record_task_result
                                    persist_failure_category(ci_task, "EXTERNAL_API")
                                    record_task_result(success=False, failure_class="EXTERNAL_API", task_id=str(ci_task.id), task_type=(ci_task.first_payload).get("type", ""))
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
                                if _fetch_failures >= 10 and not ci_loop.get("fetch_escalated"):
                                    ci_loop["fetch_escalated"] = True
                                    await _maybe_create_failure_alert(ci_task, session,
                                        f"PR #{ci_pr_number} GitHub API failures ({_fetch_failures}x) — still retrying")
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                flag_modified(ci_task, "context")
                                logger.warning(f"[CI] Transient error fetching PR #{ci_pr_number} for task {ci_task.id} "
                                               f"(attempt {_fetch_failures}): {exc.detail}")
                                session.add(ci_task)
                                await session.commit()
                                continue

                            if not pr_data:
                                await session.rollback()
                                continue

                            # Re-acquire lock after successful PR fetch.
                            ci_task = await _lock_task_for_mutation(
                                session, ci_task_id,
                                allowed_statuses=(TaskStatus.EVALUATING,),
                                reason="CI polling after PR fetch",
                                expected_snapshot=ci_pre_pr_snapshot,
                                snapshot_fn=_capture_ci_polling_snapshot,
                            )
                            if ci_task is None:
                                continue
                            ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})

                            if ci_loop.get("pr_fetch_failures"):
                                ci_loop["pr_fetch_failures"] = 0

                        pr_state = pr_data.get("state", "open")

                        # Check if PR was already merged (prevents double merge).
                        # If we self-closed for a mergeability reset and reopen failed,
                        # retry the reopen instead of marking COMPLETED — otherwise a
                        # transient reopen failure would silently discard the work.
                        if pr_state == "closed" or pr_data.get("merged"):
                            if pr_state == "closed" and not pr_data.get("merged") and ci_loop.get("pending_reopen"):
                                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                _reopen_attempts = ci_loop.get("reopen_attempts", 0) + 1
                                ci_loop["reopen_attempts"] = _reopen_attempts
                                _reopen_ok = False
                                try:
                                    _reopen_ok = bool(await reopen_pull_request(
                                        ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                        project_id=str(ci_task.project_id),
                                    ))
                                except Exception as _reopen_exc:
                                    logger.warning(f"[CI] retry reopen of PR #{ci_pr_number} raised: {_reopen_exc}")
                                if _reopen_ok:
                                    ci_loop["pending_reopen"] = False
                                    ci_loop["reopen_attempts"] = 0
                                    ci_loop.pop("reopen_alert_fired", None)
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} reopened after prior reset failure "
                                        f"(after {_reopen_attempts} attempt{'s' if _reopen_attempts != 1 else ''})"
                                    )
                                    ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                                _reopen_alert_threshold = _runtime_config.get("ci_reopen_alert_threshold", 5)
                                _reopen_approval_cap = _runtime_config.get("ci_reopen_approval_cap", 10)
                                ci_task.latest_status_message = (
                                    f"PR #{ci_pr_number} reopen failed (attempt {_reopen_attempts}) — "
                                    f"will retry on next poll"
                                )
                                if _reopen_attempts >= _reopen_alert_threshold and not ci_loop.get("reopen_alert_fired"):
                                    ci_loop["reopen_alert_fired"] = True
                                    await _maybe_create_failure_alert(
                                        ci_task, session,
                                        f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                                        f"GitHub may be rejecting the reopen, still retrying"
                                    )
                                if _reopen_attempts >= _reopen_approval_cap and not ci_task.requires_approval:
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.requires_approval = True
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                                        f"manual intervention required"
                                    )
                                    from app.failure_classification import persist_failure_category as _pfc_reopen
                                    _pfc_reopen(ci_task, "EXTERNAL_API")
                                    record_task_result(success=False, failure_class="EXTERNAL_API",
                                                       task_id=str(ci_task.id),
                                                       task_type=(ci_task.first_payload).get("type", ""))
                                    await _maybe_create_failure_alert(
                                        ci_task, session,
                                        f"PR #{ci_pr_number} reopen cap exhausted ({_reopen_attempts} attempts) — "
                                        f"task FAILED, manual intervention required"
                                    )
                                    logger.warning(
                                        f"[CI] PR #{ci_pr_number} reopen attempts ({_reopen_attempts}) "
                                        f"hit cap — marking task {ci_task.id} FAILED (requires_approval)"
                                    )
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                if ci_task.status == TaskStatus.FAILED:
                                    await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                               {"task_id": str(ci_task.id), "status": ci_task.status})
                                continue
                            if pr_data.get("merged"):
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} merged"
                                ci_task.context = {**(ci_task.context or {}), "pr_merged": True}
                                session.add(ci_task)
                                await session.commit()
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
                                logger.info(f"[CI] PR #{ci_pr_number} merged, marking task COMPLETED")
                            else:
                                # Closed without merge (human/bot intervention, not a self-close
                                # reset — pending_reopen case handled above). Do NOT mark
                                # COMPLETED — the code never landed. Fail the task so rollup
                                # parents don't silently report success.
                                _closed_msg = f"PR #{ci_pr_number} was closed without being merged"
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = _closed_msg
                                from app.failure_classification import persist_failure_category as _pfc_closed
                                _pfc_closed(ci_task, "EXTERNAL_API")
                                record_task_result(success=False, failure_class="EXTERNAL_API",
                                                   task_id=str(ci_task.id),
                                                   task_type=(ci_task.first_payload).get("type", ""))
                                session.add(ci_task)
                                await _maybe_create_failure_alert(ci_task, session, _closed_msg)
                                await session.commit()
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
                                logger.warning(f"[CI] {_closed_msg} — marking task {ci_task.id} FAILED")
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs.
                        # GitHub returns None while computing mergeability; usually transient
                        # but can get stuck after force-pushes or branch-protection changes.
                        # Track consecutive nulls and force a recompute via close+reopen
                        # before the 30-min CI backstop kills the task and discards all work.
                        if pr_data.get("mergeable") is None:
                            _null_count = ci_loop.get("mergeable_null_count", 0) + 1
                            ci_loop["mergeable_null_count"] = _null_count
                            _null_first_ts = ci_loop.get("mergeable_null_first_ts") or _utcnow().isoformat()
                            ci_loop["mergeable_null_first_ts"] = _null_first_ts
                            try:
                                _null_elapsed = (_utcnow() - datetime.fromisoformat(_null_first_ts)).total_seconds()
                            except (ValueError, TypeError):
                                _null_elapsed = 0.0
                            if _null_count >= 5 and _null_elapsed >= 180:
                                _reopen_cap = 3
                                _reopen_tries = ci_loop.get("mergeable_null_reopen_attempts", 0)
                                if _reopen_tries >= _reopen_cap:
                                    if not ci_task.requires_approval:
                                        ci_task.requires_approval = True
                                        ci_task.latest_status_message = (
                                            f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                            f"close/reopen attempts — manual intervention required"
                                        )
                                        await _maybe_create_failure_alert(
                                            ci_task, session,
                                            f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                            f"close/reopen attempts — escalating for manual intervention"
                                        )
                                    ci_loop["mergeable_null_count"] = 0
                                    ci_loop["mergeable_null_first_ts"] = None
                                    ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                                logger.warning(
                                    f"[CI] PR #{ci_pr_number} mergeable=None for {_null_count} polls over "
                                    f"{_null_elapsed:.0f}s — closing/reopening to force GitHub to recompute "
                                    f"(attempt {_reopen_tries + 1}/{_reopen_cap})"
                                )
                                _reset_ok = False
                                _did_close = False
                                try:
                                    if await close_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                                                project_id=str(ci_task.project_id)):
                                        _did_close = True
                                        _reset_ok = await reopen_pull_request(
                                            ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                            project_id=str(ci_task.project_id),
                                        )
                                except Exception as _reset_exc:
                                    logger.warning(f"[CI] close/reopen of PR #{ci_pr_number} raised: {_reset_exc}")
                                ci_loop["mergeable_null_count"] = 0
                                ci_loop["mergeable_null_first_ts"] = None
                                ci_loop["mergeable_null_reopen_attempts"] = _reopen_tries + 1
                                if _reset_ok:
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} mergeability stuck — closed/reopened to force recompute"
                                    )
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_loop["pending_reopen"] = False
                                else:
                                    if _did_close:
                                        ci_loop["pending_reopen"] = True
                                        ci_loop["polls_since_pr_fetch"] = 999
                                    await _maybe_create_failure_alert(
                                        ci_task, session,
                                        f"PR #{ci_pr_number} mergeability stuck ({_null_count} null polls) — close/reopen failed, still polling"
                                    )
                            ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                            session.add(ci_task)
                            await session.commit()
                            continue
                        if ci_loop.get("mergeable_null_count"):
                            ci_loop["mergeable_null_count"] = 0
                            ci_loop["mergeable_null_first_ts"] = None
                        if pr_data.get("mergeable") is False:
                            # GitHub's mergeable_state has several values that don't
                            # indicate a real merge conflict — only 'dirty' (and
                            # 'behind' as a fall-through fallback) should burn a VM
                            # fix cycle. Other states are recoverable by waiting:
                            # 'blocked' clears when required checks/reviews land,
                            # 'unstable' means CI is failing but the PR is mergeable,
                            # 'unknown' means GitHub is still computing.
                            _ms = (pr_data.get("mergeable_state") or "").lower()
                            _behind_fallthrough = False
                            if _ms == "behind":
                                # ── Phase B3: release lock around update_branch_with_sha_retry ──
                                # The retry helper internally retries on 422; cumulative
                                # wait can be 30s+. Drop the lock so concurrent heartbeats
                                # on this same task (rollup parents share a row) don't queue.
                                # R5-W2-A: content snapshot, not updated_at —
                                # heartbeat onupdate would false-defer every poll.
                                ci_pre_update_branch_snapshot = _capture_ci_polling_snapshot(ci_task)
                                ci_pid_str = str(ci_task.project_id)
                                _expected_head_sha = pr_data.get("head", {}).get("sha") or None
                                await session.commit()
                                # Trivially resolvable via GitHub's update-branch API.
                                # Update the branch in-place; the merge-commit
                                # triggers fresh CI and the next poll re-evaluates.
                                _upd = await update_branch_with_sha_retry(
                                    ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                    expected_head_sha=_expected_head_sha,
                                    project_id=ci_pid_str,
                                )
                                # Re-acquire to apply downstream mutations.
                                ci_task = await _lock_task_for_mutation(
                                    session, ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after update_branch",
                                    expected_snapshot=ci_pre_update_branch_snapshot,
                                    snapshot_fn=_capture_ci_polling_snapshot,
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
                                if _upd.get("updated"):
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} was behind base — updated branch via GitHub API, awaiting fresh CI"
                                    )
                                    # Force a refetch next cycle so mergeable/head_sha refresh.
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_loop["head_sha"] = ""
                                    # Reset behind-retry counter — any prior transient failures resolved.
                                    ci_loop.pop("_behind_update_retries", None)
                                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                    ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                               {"task_id": str(ci_task.id), "status": ci_task.status})
                                    logger.info(f"[CI] PR #{ci_pr_number} behind base — update_branch succeeded, skipped fix spawn")
                                    continue
                                # update_branch failed — likely a transient GitHub API error,
                                # not a real merge conflict. Defer to the next CI sweep with a
                                # bounded retry budget instead of immediately burning a fix
                                # iteration. Mirrors the rollup_management behind-update pattern.
                                _behind_retries = ci_loop.get("_behind_update_retries", 0) + 1
                                _behind_budget = _runtime_config.get("ci_behind_update_retries", 3)
                                _upd_msg = _upd.get("message") or "unknown"
                                if _behind_retries < _behind_budget:
                                    ci_loop["_behind_update_retries"] = _behind_retries
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} behind base — update_branch failed "
                                        f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                    )
                                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                    ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                                        f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                                    )
                                    continue
                                # Budget exhausted — alert, clear the counter, and fall through
                                # to fix spawn so a real engineer-style fix can resolve whatever
                                # GitHub can't auto-update.
                                await _maybe_create_failure_alert(
                                    ci_task, session,
                                    f"PR #{ci_pr_number} behind base — update_branch failed "
                                    f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn"
                                )
                                ci_loop.pop("_behind_update_retries", None)
                                _behind_fallthrough = True
                                logger.warning(
                                    f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                                    f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                                )
                            elif _ms != "dirty":
                                # Non-dirty non-behind mergeable_state values (blocked,
                                # unstable, unknown, empty, or anything unrecognized) are
                                # all recoverable by waiting rather than spawning a fix VM.
                                # Track consecutive same-state defers so silent stalls
                                # surface an alert before the 30-min CI backstop kills the
                                # task with no diagnostic breadcrumbs.
                                _ms_key = _ms or "empty"
                                _is_unrecognized = _ms_key not in ("blocked", "unstable", "unknown")
                                prev_state = ci_loop.get("_conflict_deferred_state")
                                deferred_count = (
                                    ci_loop.get("_conflict_deferred_count", 0) + 1
                                    if prev_state == _ms_key else 1
                                )
                                ci_loop["_conflict_deferred_count"] = deferred_count
                                ci_loop["_conflict_deferred_state"] = _ms_key
                                alert_threshold = 10 if _ms_key in ("blocked", "unstable") else 5
                                ci_task.latest_status_message = (
                                    f"PR #{ci_pr_number} merge deferred "
                                    f"(mergeable_state={_ms_key}, deferral {deferred_count}) — CI polling continues"
                                )
                                if deferred_count == alert_threshold:
                                    await _maybe_create_failure_alert(
                                        ci_task, session,
                                        f"PR #{ci_pr_number} stuck in mergeable_state={_ms_key} "
                                        f"for {deferred_count} consecutive CI polls"
                                    )
                                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop}
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                _log = logger.warning if _is_unrecognized else logger.debug
                                _log(
                                    f"[CI] PR #{ci_pr_number} mergeable=False, mergeable_state={_ms_key!r} "
                                    f"(deferral {deferred_count}/alert@{alert_threshold}) — continuing CI polling, not spawning fix"
                                )
                                continue
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
                                    _conflict_ci_result = await get_check_runs_cached(ci_gh_token, ci_owner, ci_repo, _conflict_head_sha)
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
                            if _behind_fallthrough:
                                # The PR is merely BEHIND base, not conflicting — but the
                                # GitHub update-branch API failed across the retry budget,
                                # so a manual rebase is needed. Describe the actual state
                                # (behind, not conflicting) so the agent does not chase
                                # nonexistent conflict markers.
                                fix_prompt = (
                                    f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                                    f"Orchestrator observation at spawn time: GitHub reported the PR as BEHIND `{ci_base}` "
                                    f"(no conflicts — just out-of-date). The orchestrator attempted to update the branch "
                                    f"via the GitHub update-branch API across the retry budget but every attempt failed; "
                                    f"a manual rebase is needed to unblock the merge.\n\n"
                                    f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                                    f"between spawn and execution. After cloning:\n"
                                    f"  1. Run `git fetch origin && git rebase origin/{ci_base}`.\n"
                                    f"  2. If the rebase surfaces unexpected conflicts (it shouldn't, given the "
                                    f"behind state), check `git status` and `git diff --name-only --diff-filter=U` "
                                    f"and resolve them.\n"
                                    f"  3. Re-query the PR's current state via the GitHub API "
                                    f"(`curl -H \"Authorization: Bearer $GITHUB_TOKEN\" "
                                    f"https://api.github.com/repos/{ci_owner}/{ci_repo}/pulls/{ci_pr_number}`) "
                                    f"to confirm what is actually blocking the merge now.\n\n"
                                    f"Primary action: rebase onto `{ci_base}` and force-push so the PR is no longer behind."
                                    f"{ci_preview_section}\n\n"
                                    f"Before declaring done: ensure (a) no conflict markers remain (if any surfaced), "
                                    f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                                    f"every fix — do not leave uncommitted changes."
                                )
                            else:
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
                            fix_task, fix_was_created = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=fix_prompt, original_task_id=ci_orig_id
                            )
                            ci_task = await _lock_task_for_mutation(
                                session,
                                ci_task_id,
                                allowed_statuses=(TaskStatus.EVALUATING,),
                                reason="CI conflict result",
                            )
                            if ci_task is None:
                                continue
                            ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
                            if fix_task:
                                from app.rollup_state import _is_rollup_parent_awaiting_merge
                                ci_loop_ctx = dict(ci_loop)
                                ci_loop_ctx["ci_waiting"] = False
                                ci_loop_ctx["conflict_fix_spawned"] = str(fix_task.id)
                                ci_task.context = {**(ci_task.context or {}), "review_loop": ci_loop_ctx}
                                # Rollup parents must stay in EVALUATING until the rollup PR
                                # actually merges — ci_completion will promote them via
                                # pr_merged=True. Marking COMPLETED here produces a ghost
                                # success while the fix task is still resolving conflicts.
                                if not _is_rollup_parent_awaiting_merge(ci_task.context):
                                    ci_task.status = TaskStatus.COMPLETED
                                if fix_was_created:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                    logger.info(f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}")
                                else:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}"
                                    logger.info(f"[CI] PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}")
                            else:
                                # Fix not spawned — either iteration cap exhausted, or a
                                # different-type task (e.g. a review) is still running on
                                # the same chain and must finish first. In both cases the
                                # CI-wait sweep on the next tick will re-evaluate; only
                                # escalate to FAILED + requires_approval when we actually
                                # know the cap is exhausted.
                                msg = (ci_task.latest_status_message or "").lower()
                                deferred = "deferred" in msg
                                if deferred:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — waiting on in-flight pipeline task"
                                    logger.info(f"[CI] PR #{ci_pr_number} conflict-fix deferred — a different-type pipeline task is still running, will retry on next sweep")
                                else:
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.requires_approval = True
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — fix iterations exhausted, requires manual intervention"
                                    logger.warning(f"[CI] PR #{ci_pr_number} merge conflicts, fix not spawned — marking FAILED (requires_approval)")
                                    await _maybe_create_failure_alert(ci_task, session, f"PR #{ci_pr_number} merge conflicts — fix iteration cap exhausted, manual intervention required")
                                    from app.failure_classification import classify_failure, persist_failure_category
                                    _fc_mc = classify_failure(ci_task)
                                    persist_failure_category(ci_task, _fc_mc)
                                    record_task_result(success=False, failure_class=_fc_mc, task_id=str(ci_task.id), task_type=(ci_task.first_payload).get("type", ""))
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

                        # ── Phase B3: release lock around get_check_runs_cached ──
                        # Even with TTL caching, a cache miss makes a real GitHub
                        # API call that can hang during rate-limit. Drop the lock
                        # so concurrent heartbeats proceed.
                        # R5-W2-A: content snapshot, not updated_at —
                        # heartbeat onupdate would false-defer every poll.
                        ci_pre_check_snapshot = _capture_ci_polling_snapshot(ci_task)
                        ci_project_id_str = str(ci_task.project_id)
                        await session.commit()
                        # Dedup cached — siblings on the same head_sha collapse
                        # into one underlying GitHub call within the 30s TTL.
                        ci_result = await get_check_runs_cached(ci_gh_token, ci_owner, ci_repo, ci_head_sha, project_id=ci_project_id_str)
                        # Re-acquire to apply downstream mutations.
                        ci_task = await _lock_task_for_mutation(
                            session, ci_task_id,
                            allowed_statuses=(TaskStatus.EVALUATING,),
                            reason="CI polling after check_runs fetch",
                            expected_snapshot=ci_pre_check_snapshot,
                            snapshot_fn=_capture_ci_polling_snapshot,
                        )
                        if ci_task is None:
                            continue
                        ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})

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
                                        ci_task = await _lock_task_for_mutation(
                                            session,
                                            ci_task_id,
                                            allowed_statuses=(TaskStatus.EVALUATING,),
                                            reason="CI polling after no-checks alert",
                                        )
                                        if ci_task is None:
                                            continue
                                        ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
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
                            # JSONB nested-dict mutation needs flag_modified —
                            # otherwise ci_last_polled/poll_interval don't
                            # persist and the per-task 30s skip never engages,
                            # so the orchestrator hammers GitHub every 2s.
                            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                            flag_modified(ci_task, "context")
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            continue

                        # CI completed — delegate to shared handler
                        from app.ci_completion import handle_ci_completion
                        logger.info(f"[CI] Poll fallback: CI completed for PR #{ci_pr_number}, processing")
                        await handle_ci_completion(ci_task, ci_result, ci_gh_token, ci_owner, ci_repo, session, source="poll")
                    except Exception as e:
                        logger.exception(f"[CI] Error checking CI for task {ci_task.id}")
                        try:
                            status_code = getattr(e, "status_code", None)
                            if status_code == 404:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = "CI polling failed: PR not found (404) — PR may have been deleted"
                                logger.exception(f"[CI] PR #{ci_task.context.get('review_loop', {}).get('pr_number')} returned 404 — marking task {ci_task.id} FAILED")
                                await _maybe_create_failure_alert(ci_task, session, "CI polling failed: PR returned 404 — PR may have been deleted or archived")
                                from app.failure_classification import persist_failure_category as _pfc404
                                _pfc404(ci_task, "EXTERNAL_API")
                                record_task_result(success=False, failure_class="EXTERNAL_API", task_id=str(ci_task.id), task_type=(ci_task.first_payload).get("type", ""))
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

                # ── Post-commit recovery ──
                # If _process_post_commit raised after the early-commit persisted
                # the task status, the PR may be orphaned (no review/fix spawned).
                # The agent retry handles most cases via the idempotency guard
                # modification, but if all retries failed the flag persists here.
                # Scan for these and directly spawn the missing review.
                # Audit W1 (A2/A5): post_commit_pending is now a column, so the
                # scan filter and clears go through the column instead of JSONB.
                try:
                    pending_tasks = (await session.execute(
                        select(Task).where(
                            Task.post_commit_pending.is_(True),
                            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                        ).limit(50)
                    )).scalars().all()
                    for pt in pending_tasks:
                        if pt.status != TaskStatus.COMPLETED:
                            pt.post_commit_pending = False
                            session.add(pt)
                            await session.commit()
                            continue
                        pt_payload = pt.first_payload
                        pt_type = pt_payload.get("type", "create")
                        if pt_type not in ("create", "refactor", "docs", "qa", "deps", "migrate", ""):
                            pt.post_commit_pending = False
                            session.add(pt)
                            await session.commit()
                            continue
                        pr_artifact = (await session.execute(
                            select(Artifact).where(
                                Artifact.task_id == pt.id,
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            ).limit(1)
                        )).scalars().first()
                        if not pr_artifact or not pr_artifact.url:
                            pt.post_commit_pending = False
                            session.add(pt)
                            await session.commit()
                            continue
                        try:
                            pc_pr_number = int(pr_artifact.url.rstrip("/").split("/")[-1])
                        except (ValueError, IndexError):
                            pt.post_commit_pending = False
                            session.add(pt)
                            await session.commit()
                            continue
                        pc_short_id = str(pt.id).replace('-', '')[:8]
                        pc_branch = f"blueprint/task-{pc_short_id}"
                        pc_base = pt_payload.get("base_branch", "main")
                        pc_prompt = pt_payload.get("prompt", pt.description or "")
                        logger.info(f"[RECOVERY] Post-commit pending for task {pt.id}, spawning review for PR #{pc_pr_number}")
                        from app.review_pipeline import _spawn_pipeline_task
                        review_task, was_created = await _spawn_pipeline_task(
                            pt, "review", session, pc_pr_number, pc_branch, pc_base,
                            original_prompt=pc_prompt, original_task_id=str(pt.id)
                        )
                        pt.post_commit_pending = False
                        if review_task and was_created:
                            logger.info(f"[RECOVERY] Spawned review {review_task.id} for orphaned PR #{pc_pr_number} (task {pt.id})")
                        elif review_task:
                            logger.info(f"[RECOVERY] Review already active for PR #{pc_pr_number} (task {pt.id}): {review_task.id}")
                        else:
                            logger.warning(f"[RECOVERY] Review not spawned for orphaned PR #{pc_pr_number} (task {pt.id}) — cap/guard")
                        session.add(pt)
                        await session.commit()
                except Exception as _pc_err:
                    logger.error(f"[RECOVERY] Post-commit recovery scan failed: {_pc_err}", exc_info=True)
                    try:
                        await session.rollback()
                    except Exception:
                        pass

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
            # _post_commit_pending sweeper — every 60s. Retries post-commit
            # processing for terminal tasks whose prior attempt timed out
            # (GitHub quota exhaustion, transient failures, etc.) without
            # waiting for the agent to retry (the VM has usually terminated).
            if (_utcnow() - _last_post_commit_sweep).total_seconds() >= 60:
                _last_post_commit_sweep = _utcnow()
                await _sweep_post_commit_pending()
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
