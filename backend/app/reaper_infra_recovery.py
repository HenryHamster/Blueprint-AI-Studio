"""Infrastructure-recovery and direction-hold maintenance helpers for the reaper.

Extracted from reaper.reaper_service Step 5 / 5a-5e. Entry point is
run_infra_recovery_and_hold_maintenance(); internal helpers are the
_sweep_* functions below.
"""

import logging
import uuid
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import load_only
from sqlmodel import select

from app.compute import any_zone_available
from app.failure_classification import INFRA_CLASSES
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    Task,
    TaskStatus,
    bank_task_current_attempt,
)
from app.shared_state import (
    external_health as _external_health,
    runtime_config as _runtime_config,
)
from app.time_utils import RETRY_AFTER_MAX_MINUTES, _utcnow, clear_retry_after

logger = logging.getLogger(__name__)

_BATCH = 5000


async def _safe_rollback(session) -> None:
    """Best-effort rollback so one failed sweep can't leave the reaper's shared
    session aborted and cascade ``PendingRollbackError`` into every subsequent
    sweep. The reaper runs all of Steps 5-10 on a single session, so a sweep
    that raises mid-flush must restore the session for the next one."""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        logger.warning("[REAPER] Rollback after sweep error failed", exc_info=True)


async def _sweep_orphan_direction_holds(session) -> int:
    """Release direction holds whose direction_id no longer maps to a live direction.

    A direction is "live" if it exists in some project's directions JSONB with
    status in (active, paused, proposed). completed/expired/rejected directions
    shouldn't hold tasks — and archived directions that disappear entirely
    definitely shouldn't. Returns the number of holds cleared.
    """
    from app.shared_state import (
        direction_failure_streaks,
        clear_direction_hold,
    )

    live_dids: set[str] = set()
    projects = (
        (
            await session.execute(
                select(Project).options(
                    load_only(Project.id, Project.directions, raiseload=True)
                )
            )
        )
        .scalars()
        .all()
    )
    for proj in projects:
        for d in proj.directions or []:
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
    """Close stale residual cases and auto-resolve quiet REMEDIATING cases.

    ACTIVE rows transition to CLOSED_ORGANICALLY after the configured quiet
    window, unless the fingerprint has been seen recently. REMEDIATING rows
    transition to RESOLVED when the remediation has been stale for the
    configured window and no new failures carried that fingerprint in-window.
    """
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.failure_classification import _mark_residual_case_closed
    from app.notification_dispatch import post_event_message

    now = _utcnow()
    active_hours = int(_runtime_config.get("residual_active_auto_close_hours", 72))
    remediating_hours = int(
        _runtime_config.get("residual_remediating_auto_resolve_hours", active_hours)
    )
    recent_hit_grace_hours = int(
        _runtime_config.get("residual_recent_hit_grace_hours", 1)
    )
    active_cutoff = now - timedelta(hours=active_hours)
    remediating_cutoff = now - timedelta(hours=remediating_hours)
    recent_hit_cutoff = now - timedelta(hours=recent_hit_grace_hours)

    active_stale_cases = (
        (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.status == ResidualCaseStatus.ACTIVE,
                    ResidualRemediationCase.last_seen_at < active_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    remediating_stale_cases = (
        (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.status == ResidualCaseStatus.REMEDIATING,
                    ResidualRemediationCase.last_spawned_at.is_not(None),
                    ResidualRemediationCase.last_spawned_at < remediating_cutoff,
                    ResidualRemediationCase.last_seen_at < remediating_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    closed = 0
    for case in active_stale_cases:
        if case.last_seen_at >= recent_hit_cutoff:
            logger.info(
                "[REAPER] Deferring ACTIVE residual case close %s (fingerprint=%r, last_seen_at=%s)",
                case.id,
                case.fingerprint,
                case.last_seen_at.isoformat(),
            )
            continue
        case.status = ResidualCaseStatus.CLOSED_ORGANICALLY
        case.updated_at = now
        session.add(case)
        _mark_residual_case_closed(case.fingerprint)
        closed += 1
        logger.info(
            "[REAPER] Closed stale residual case %s (fingerprint=%r, last_seen_at=%s)",
            case.id,
            case.fingerprint,
            case.last_seen_at.isoformat(),
        )
    for case in remediating_stale_cases:
        case.status = ResidualCaseStatus.RESOLVED
        case.updated_at = now
        session.add(case)
        _mark_residual_case_closed(case.fingerprint)
        closed += 1
        await post_event_message(
            session,
            case.project_id,
            event_type="residual_fix_resolved_organically",
            summary=f"Residual auto-fix resolved organically for {case.fingerprint[:32]} after quiet period",
            context={"fingerprint": case.fingerprint, "result": "resolved_organically"},
            task_id=case.last_spawned_task_id,
        )
        logger.info(
            "[REAPER] Auto-resolved stale REMEDIATING residual case %s (fingerprint=%r, last_spawned_at=%s, last_seen_at=%s)",
            case.id,
            case.fingerprint,
            case.last_spawned_at.isoformat() if case.last_spawned_at else None,
            case.last_seen_at.isoformat(),
        )

    if closed:
        await session.commit()
        logger.info("[REAPER] Closed %d stale residual remediation case(s)", closed)
    return closed


def _noop_progress() -> None:
    """Default heartbeat — does nothing. Overridden by the reaper, which passes
    ``_mark_reaper_progress`` so this sweep's sub-steps keep the reaper liveness
    heartbeat fresh (cluster C3). A late-bound callback avoids a circular import
    (reaper.py imports this module)."""
    return None


async def run_infra_recovery_and_hold_maintenance(
    session, now, just_reaped_ids=None, progress_cb=None
) -> None:
    """Run Step 5 infrastructure-recovery sweep and hold-maintenance sub-steps.

    Step 5:  Resurrect recently-failed tasks whose infra root cause has resolved.
    Step 5a: Clear infra holds on directions whose signal recovered.
    Step 5c: Clear holds on directions no longer live.
    Step 5d: Prune idle in-memory state dicts.
    Step 5b: Clear stale/malformed retry_after on QUEUED tasks.
    Step 5e: Close stale ACTIVE ResidualRemediationCase rows.

    Preserves all log prefixes and ordering from the original inlined code.

    ``progress_cb`` (C3): called at the start of each sub-step so the reaper's
    in-cycle liveness heartbeat advances even when one sub-step (e.g. serial
    resurrection DB work + broadcast fan-out) runs long. Defaults to a no-op so
    direct callers / tests need not supply it.
    """
    _heartbeat = progress_cb or _noop_progress
    # Step 5: Infrastructure recovery sweep — resurrect recently-failed tasks
    # that exhausted their retry budget due to transient infrastructure failures
    # once the specific root cause has resolved.  Conservative: max 1 resurrection
    # per task, only for confirmed infra failure classes, only when infra is healthy.
    _heartbeat()
    try:
        from app.max_plan_pool import (
            _all_accounts,
            _account_available_for_model,
            clear_task_llm_runtime_context,
            _extract_task_model,
        )
        from app.broadcast import broadcast_to_project

        _infra_lookback = now - timedelta(hours=2)
        # Exclude just-reaped tasks so Step 2 failures aren't immediately
        # resurrected in the same cycle, defeating the retry budget.
        # Use the id snapshot (1039), not t.id on the ORM objects: a
        # per-task reap crash rolls back + expires every orphaned_tasks
        # object, so reading t.id here would raise MissingGreenlet.
        _just_reaped_ids = set(just_reaped_ids or [])
        _infra_query = (
            select(Task)
            .where(
                Task.status == TaskStatus.FAILED,
                Task.updated_at >= _infra_lookback,
            )
            .order_by(Task.updated_at.desc())
            .limit(200)
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
            _child_rows = (
                (
                    await session.execute(
                        select(Task.parent_task_id)
                        .where(Task.parent_task_id.in_(_failed_ids))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            _parent_ids_with_children = {pid for pid in _child_rows if pid is not None}

        # Pre-compute idempotency_keys already held by an ACTIVE task.
        # Resurrection flips FAILED→QUEUED, which enters the partial unique
        # index ux_task_active_idempotency_key (active statuses only). If a
        # live duplicate already holds that key the resurrection commit aborts
        # with UniqueViolationError; on the reaper's shared session that single
        # abort cascades PendingRollbackError into every downstream sweep and,
        # via Step 9's re-raise, stalls the liveness heartbeat → restart.
        _active_idem_keys: set[str] = set()
        _cand_idem_keys = {
            _t.idempotency_key for _t in _infra_failed if _t.idempotency_key
        }
        if _cand_idem_keys:
            _active_idem_keys = set(
                (
                    await session.execute(
                        select(Task.idempotency_key).where(
                            Task.idempotency_key.in_(_cand_idem_keys),
                            # Mirror the ux_task_active_idempotency_key partial-index
                            # predicate EXACTLY (migration 0199 includes BLOCKED +
                            # PARKED). A status the index counts but this guard omits
                            # lets a colliding requeue trip the unique-violation
                            # IntegrityError on commit → whole-batch rollback.
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                    TaskStatus.BLOCKED,
                                    TaskStatus.PARKED,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )

        _resurrected = 0
        # Keys claimed earlier in THIS batch — guards intra-batch collisions
        # between two FAILED duplicates sharing a key (neither is active at
        # query time, so the pre-query above can't catch the pair).
        _resurrecting_keys: set[str] = set()
        for _t in _infra_failed:
            _ctx = _t.context or {}
            _payload = _t.first_payload
            # PR review/fix failures must be re-driven from current GitHub state
            # through canonical review admission. A generic FAILED -> QUEUED
            # flip can resurrect a stale review target or writer and wastes an
            # agent even though the claim fence would ultimately reject it.
            if (
                _payload.get("type") in {"review", "fix"}
                and _payload.get("pr_number")
            ):
                logger.info(
                    "[REAPER] Skipping generic resurrection of PR %s task %s",
                    _payload.get("type"),
                    _t.id,
                )
                continue
            # Only resurrect infra-class failures, and only once per task.
            _fail_cat = _ctx.get("failure_category", "")
            if _fail_cat not in INFRA_CLASSES:
                continue
            if _ctx.get("_resurrection_count", 0) >= 1:
                continue
            # Skip terminal PR-closure outcomes. classify_failure routes
            # "closed without merging" / empty-rollup closures into
            # EXTERNAL_API (it shares the merge-blocking GitHub bank),
            # but operator rejection and empty-rollup short-circuit are
            # not infrastructure failures — re-running produces the same
            # rejected/empty PR. Mirrors the closure markers documented
            # in services._pr_possibly_open.
            if (
                _ctx.get("pr_closed_unmerged") is True
                or _ctx.get("pr_closed_empty") is True
                or _ctx.get("pr_rejected") is True
            ):
                continue
            # Skip parents — they must re-evaluate via _requeue_parent_task, not
            # resurrect as leaf dispatches.
            if _t.id in _parent_ids_with_children:
                continue

            # Skip when a live duplicate (or an earlier candidate this batch)
            # already holds this idempotency_key — flipping to QUEUED would
            # violate ux_task_active_idempotency_key and poison the session.
            # The active duplicate already represents the work.
            if _t.idempotency_key and (
                _t.idempotency_key in _active_idem_keys
                or _t.idempotency_key in _resurrecting_keys
            ):
                logger.info(
                    f"[REAPER] Skipping resurrection of {_t.id}: "
                    "idempotency_key already held by an active task"
                )
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
                    _task_model = _runtime_config.get("task_type_models", {}).get(
                        _task_type
                    )
                _blacklisted = set(_ctx.get("blacklisted_accounts") or [])
                _accounts = _all_accounts()
                _infra_ready = any(
                    acct.get("id") not in _blacklisted
                    and _account_available_for_model(acct, _task_model)
                    for acct in _accounts
                )
            elif _fail_cat == "EXTERNAL_API":
                _infra_ready = _external_health.get(
                    "github_api", False
                ) and _external_health.get("dispatch_api", False)
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
            if _t.idempotency_key:
                _resurrecting_keys.add(_t.idempotency_key)
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
            _new_ctx.pop("failure_category", None)
            _new_ctx.pop("failure_class", None)
            # Clear EXTERNAL_API retry counter: a resurrected task that hits another
            # API error would otherwise see stale _api_failure_retries > 5 and
            # bypass the EXTERNAL_API-specific backoff, falling through to the
            # general retry path with a depleted budget.
            _new_ctx.pop("_api_failure_retries", None)
            _t.context = _new_ctx
            # Keep the promoted column in sync (D2 over-fetch fix).
            _t.llm_account_id = None
            clear_retry_after(_t)
            _t.attempt_count = 0
            bank_task_current_attempt(_t, stopped_at=_utcnow())
            _t.status = TaskStatus.QUEUED
            _t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            _t.last_heartbeat = None
            _t.started_at = None
            _t.latest_status_message = (
                f"Auto-resurrected after infrastructure recovery (was: {_fail_cat})"
            )
            session.add(_t)

            # INFO alert so the user knows recovery happened.
            try:
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
                    event_type="deployment.auto_resume",
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
            except Exception as _alert_err:  # noqa: BLE001
                logger.warning(
                    f"[REAPER] Resurrection alert failed for {_t.id}: {_alert_err}"
                )

            _resurrected += 1

        if _resurrected:
            await session.commit()
            logger.info(f"[REAPER] Resurrected {_resurrected} infra-failed task(s)")

    except Exception as e:
        logger.error(f"[REAPER] Infra recovery sweep error: {e}", exc_info=True)
        await _safe_rollback(session)

    # Step 5a: Clear infra holds on directions whose root-cause signal has recovered.
    # Mirrors the signal checks in Step 5 but applies to direction-level holds
    # in direction_failure_streaks rather than individual task resurrection.
    _heartbeat()
    try:
        from app.max_plan_pool import (
            _all_accounts,
            _account_available_for_model,
        )
        from app.shared_state import (
            direction_failure_streaks,
            clear_direction_hold,
        )

        _cleared_holds = 0
        for _did, _streak in list(direction_failure_streaks.items()):
            if not _streak.get("held") or _streak.get("held_reason") != "infra":
                continue
            _fc = (
                _streak.get("held_failure_class")
                or _streak.get("last_failure_class")
                or ""
            )
            _ready = False
            if (
                _fc == "PROVISION_FAILURE"
                or _fc == "AGENT_TIMEOUT"
                or _fc == "SCOPE_OVERFLOW"
            ):
                _ready = any_zone_available()
            elif _fc == "RATE_LIMIT":
                _ready = any(
                    _account_available_for_model(acct, None) for acct in _all_accounts()
                )
            elif _fc == "EXTERNAL_API":
                _ready = _external_health.get(
                    "github_api", False
                ) and _external_health.get("dispatch_api", False)
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
        logger.error(
            f"[REAPER] Infra direction hold recovery error: {e}",
            exc_info=True,
        )

    # Step 5c: Clear holds on directions that are no longer live.
    # When a direction is auto-retired/completed/rejected/expired
    # while a task is held against it, the in-memory streak entry
    # keeps the hold set forever — the 4-hour timeout is the only
    # escape. That strands the task behind a direction that no
    # longer exists in any project's directions list.
    _heartbeat()
    try:
        await _sweep_orphan_direction_holds(session)
    except Exception as e:
        logger.error(
            f"[REAPER] Orphan direction hold sweep error: {e}",
            exc_info=True,
        )
        await _safe_rollback(session)

    # Step 5d: Prune idle entries from long-lived in-memory state
    # dicts. direction_failure_streaks / circuit_breakers /
    # merge_error_occurrences / autonomous_failure_counts /
    # user_debug_spawn_times all accumulate keys monotonically
    # over an instance's lifetime. On Cloud Run instances that
    # stay up for days this contributes to 4GB OOMs.
    _heartbeat()
    try:
        from app.shared_state import sweep_stale_in_memory_state

        _mem_pruned = sweep_stale_in_memory_state()
        if any(_mem_pruned.values()):
            logger.info(f"[REAPER] Pruned idle in-memory state: {_mem_pruned}")
    except Exception as e:
        logger.error(f"[REAPER] In-memory state sweep error: {e}", exc_info=True)

    # Step 5b: Clear stale or malformed retry_after on QUEUED tasks.
    # A far-future or unparseable value permanently blocks dispatch
    # (orchestrator skips tasks where retry_after > now).
    _heartbeat()
    try:
        # Park-and-retarget expiry: a QUEUED task holding a warm VM must never
        # keep that VM indefinitely. Once the bounded park window elapses,
        # convert it back to the ordinary cold-requeue shape and tear the held
        # VM down explicitly.
        _queued_with_vm = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.QUEUED,
                        Task.vm_instance_id.is_not(None),
                        # Filter to parked rows at SQL level so the LIMIT budget
                        # is not consumed by ordinary cold-requeued QUEUED+vm tasks.
                        # JSON path access compiles to ?-operator on PostgreSQL and
                        # json_extract on SQLite — both return NULL when key is absent.
                        Task.context["parked_for_retarget"].is_not(None),
                    )
                    .limit(_BATCH)
                )
            )
            .scalars()
            .all()
        )
        expired_parked = 0
        expired_vm_ids: list[str] = []
        for _qt in _queued_with_vm:
            _ctx = dict(_qt.context or {})
            if not _ctx.get("parked_for_retarget"):
                continue
            raw_deadline = _ctx.get("park_deadline")
            park_deadline: datetime | None = None
            if isinstance(raw_deadline, str):
                try:
                    park_deadline = datetime.fromisoformat(raw_deadline)
                except ValueError:
                    park_deadline = None
            if park_deadline is not None and park_deadline > now:
                continue
            if park_deadline is None:
                logger.warning(
                    "[REAPER] Task %s has malformed park_deadline=%r; forcing cold requeue",
                    _qt.id,
                    raw_deadline,
                )
            old_vm_id = _qt.vm_instance_id
            if old_vm_id:
                expired_vm_ids.append(old_vm_id)
            _qt.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            _qt.last_heartbeat = None
            _qt.started_at = None
            _qt.latest_status_message = (
                "Park-and-retarget budget expired — cold requeueing"
            )
            _ctx.pop("parked_for_retarget", None)
            _ctx.pop("park_deadline", None)
            _ctx.pop("warm_target_vm_id", None)
            _ctx.pop("warm_handoff_deadline", None)
            _ctx.pop("warm_target_pr", None)
            _qt.context = _ctx
            clear_retry_after(_qt)
            session.add(_qt)
            expired_parked += 1
        if expired_parked:
            await session.commit()
            from app.vm_lifecycle import _ensure_vm_deleted

            for old_vm_id in expired_vm_ids:
                await _ensure_vm_deleted(
                    old_vm_id, reason="park_retarget_budget_expired"
                )
            logger.info(
                "[REAPER] Expired %d parked-for-retarget task(s) back to cold requeue",
                expired_parked,
            )

        # Audit R3-W2 (H7): cap per-tick fetch at _BATCH rows.
        # Audit R4-W2 (M2): filter ``retry_after_until IS NOT NULL``
        # at SQL — without this, the LIMIT _BATCH budget was wasted
        # iterating QUEUED rows that never had a retry_after set
        # (the common case), starving the actual stale-far-future
        # candidates the sweep was meant to catch.
        _retry_cutoff = now + timedelta(minutes=RETRY_AFTER_MAX_MINUTES)
        _queued_with_retry = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.QUEUED,
                        Task.retry_after_until.is_not(None),
                    )
                    .limit(_BATCH)
                )
            )
            .scalars()
            .all()
        )
        if len(_queued_with_retry) >= _BATCH:
            logger.warning(
                "[REAPER] retry_after sweep hit cap=%d rows — result truncated; "
                "follow-up: chunk-paginate",
                _BATCH,
            )
        _cleared = 0
        for _qt in _queued_with_retry:
            # Audit W1: retry_after_until is a column now.
            if _qt.retry_after_until is None:
                continue
            if _qt.retry_after_until > _retry_cutoff:
                logger.warning(
                    f"[REAPER] Task {_qt.id}: clearing far-future retry_after={_qt.retry_after_until.isoformat()} (>{RETRY_AFTER_MAX_MINUTES}min)"
                )
            else:
                continue
            clear_retry_after(_qt)
            session.add(_qt)
            _cleared += 1
        if _cleared:
            await session.commit()
            logger.info(f"[REAPER] Cleared stale retry_after on {_cleared} task(s)")
    except Exception as e:
        logger.error(f"[REAPER] Stale retry_after sweep error: {e}", exc_info=True)
        await _safe_rollback(session)

    # Step 5e: Close ACTIVE ResidualRemediationCase rows whose fingerprint
    # has not been seen in the last 72 h. Without this sweep the table
    # accumulates stale ACTIVE rows for fingerprints that stopped occurring
    # organically (infra recovered, unrelated PR fixed the root cause, etc.).
    _heartbeat()
    try:
        await _sweep_stale_residual_cases(session)
    except Exception as e:
        logger.error(f"[REAPER] Stale residual case sweep error: {e}", exc_info=True)
        await _safe_rollback(session)
