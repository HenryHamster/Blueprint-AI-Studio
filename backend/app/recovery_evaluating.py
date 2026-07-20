"""EVALUATING-timeout recovery sweep, extracted from ``app.recovery_sweeps``.

Holds the bounded leader-cycle EVALUATING-timeout sweep
(``_check_evaluating_timeouts``). Its chain-inspection helpers
(``_chain_has_active_pipeline_work`` and friends), the CI-backstop reason
picker/verifier, the long-queued-pipeline superseder, and the spend-hold signal
emitters were split out again into ``app.recovery_evaluating_helpers`` to keep
each module below its size cap; they are re-exported here so the sweep (and its
callers) reference them as bare module-globals exactly as before.

Split out of ``app.recovery_sweeps`` to keep each module below its size cap.
Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing ``from app.services import ...`` call sites and
test patch targets keep resolving unchanged. Shared bounding infrastructure
(C3 constants, ``_bounded``, ``_DeployPauseSnapshot``, session/broadcast
helpers) lives in ``app.services`` and is referenced through the ``services``
module object so test patches against ``app.services.<name>`` still take effect.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import or_
from sqlalchemy.orm import load_only
from sqlmodel import select

from app import pause_clock, services
from app.models import (
    Task,
    TaskStatus,
    Alert,
    AlertSeverity,
)
from app.recovery_evaluating_strand import (
    _handle_evaluating_strand_backstop,
    _handle_rollup_lost_marker,
)
from app.recovery_evaluating_approval import _handle_approval_gate_timeout
from app.recovery_evaluating_ci_backstop import _handle_ci_waiting_backstop
from app.recovery_evaluating_pr_check import _handle_pr_state_check_pending
from app.recovery_evaluating_reaped_owner import (
    _handle_reaped_owner_fallback,
    _handle_reaped_owner_reconcile,
)
from app.recovery_evaluating_hard_timeout import _handle_evaluating_hard_timeout
from app.recovery_evaluating_helpers import (  # noqa: F401
    _last_status_transition_at,
    _chain_has_active_pipeline_work,
    _approval_gate_chain_busy,
    _chain_has_recoverable_failed_work,
    _chain_has_unexhausted_optimistic_retry_work,
    _summarize_latest_review_for_chain,
    _QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS,
    _ci_backstop_timeout_reason,
    _supersede_long_queued_pipeline_tasks,
    _attempt_ci_backstop_recovery,
    _verify_ci_before_backstop,
    _dispatch_spend_hold_notification,
    _emit_spend_hold_created_signals,
    _heal_direction_outcomes_best_effort,
    _apply_rollup_merge_classification,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _check_evaluating_timeouts(progress_cb=None):
    """Detect EVALUATING tasks that are stuck and recover or fail them.

    - If all children are terminal but parent is still EVALUATING → trigger missed rollup.
    - If evaluating >2h with non-terminal children → WARNING alert.
    - If evaluating >evaluating_fail_hours (default 48h) → FAIL with ERROR alert.
    - Skips CI-waiting tasks (they have their own 20-min escalation timeout).

    ``progress_cb`` (C3): a zero-arg liveness heartbeat pumped once per processed
    row. This sweep now runs in its OWN background task
    (``_evaluating_timeout_service``, lifecycle.py), so it must NOT pump the
    orchestrator_worker's shared ``_orchestrator_last_progress`` marker — a
    foreign task pumping the worker's heartbeat would MASK a genuinely hung
    orchestrator_worker from its /health gate + watchdog. Instead the caller
    threads its OWN marker (or relies on the background task's per-tick
    asyncio.timeout + _monitor_background_tasks supervision). Defaults to a
    no-op for direct callers / tests that don't supply one.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.rollup_management import _check_and_trigger_rollup
    from app.shared_state import runtime_config as _runtime_config

    _heartbeat = progress_cb if progress_cb is not None else (lambda: None)

    EVALUATING_WARN_SECONDS = 2 * 3600  # 2 hours
    _REAPED_OWNER_FALLBACK_GRACE_S = (
        1800  # 30 min grace before GH poll for marker-less owners
    )
    # Default backstop bumped to 48h while silent-death v2 stabilizes
    # (2026-05-04): tasks reaped mid-flight by the 6h wall-clock were getting
    # cascade-killed during operator-intervention windows. Runtime knob
    # `evaluating_fail_hours` lets us drop it back once the recurrence rate
    # for silent-death + reaper sweeps comes down.
    # Defensive read: the PATCH endpoint validates 1..168, but a corrupted
    # runtime_config_json on disk could surface a non-int / out-of-range value
    # which would raise ValueError on `int(...)` and abort the whole sweep
    # cycle silently — no EVALUATING cleanup until someone PATCHes a fix.
    try:
        _eval_hours_raw = _runtime_config.get("evaluating_fail_hours", 48)
        _eval_hours = int(_eval_hours_raw)
        if not (1 <= _eval_hours <= 168):
            _eval_hours = 48
    except (ValueError, TypeError):
        _eval_hours = 48
    EVALUATING_FAIL_SECONDS = _eval_hours * 3600
    # Hard kill for CI-waiting tasks. Tunable so multi-iteration review/fix
    # cycles (each ~5-15 min, plus pipeline-cap queueing) don't trigger a
    # premature kill that cancels in-flight children. 60 min is the default
    # because realistic cycles routinely run 30-50 min; 30 min was firing
    # mid-iteration during pipeline-throttled periods.
    CI_BACKSTOP_SECONDS = (
        int(_runtime_config.get("ci_backstop_timeout_minutes", 60)) * 60
    )
    # Bounded defer window for the CI-wait backstop when the chain still holds
    # recoverable (infra-class FAILED / freshly-requeued) work — mirrors the 6h
    # hard-timeout path's ``EVAL_DEFER_WINDOW_HOURS``. A parent must not be
    # FAILED (nor its children cascade-cancelled) while salvageable work exists,
    # but a truly stuck case must still terminate eventually. Shorter than the
    # 6h path's 24h window: the CI-wait backstop guards a much tighter loop
    # (rollup PR CI + review iteration), and the optimistic-retry cap bounds how
    # long a recoverable child can keep re-arming anyway.
    CI_BACKSTOP_DEFER_WINDOW_HOURS = 6
    # Approval-gate companion timeout constants (APPROVAL_GATE_FAIL_SECONDS /
    # APPROVAL_GATE_DEFER_WINDOW_HOURS) now live in
    # ``app.recovery_evaluating_approval`` alongside the extracted handler.
    # Deliberate spend/budget approval holds (requires_approval=True AND
    # context.approval_hold_reason == "spend_budget") get their OWN longer,
    # operator-configurable window and resolve to a CLEAN NON-FAILURE CANCELLED
    # terminal on expiry — a cost decision an operator simply has not reached is
    # not a pipeline failure. Defensive read mirrors evaluating_fail_hours: a
    # corrupted runtime_config_json must not abort the whole sweep cycle.
    try:
        _spend_hours_raw = _runtime_config.get("spend_approval_timeout_hours", 24)
        _spend_hours = int(_spend_hours_raw)
        if not (1 <= _spend_hours <= 168):
            _spend_hours = 24
    except (ValueError, TypeError):
        _spend_hours = 24
    SPEND_APPROVAL_FAIL_SECONDS = _spend_hours * 3600
    # BLOCKED and PARKED are treated as terminal-for-now in this EVALUATING-timeout
    # sweep: both are durable non-terminal states that must NOT be cascade-CANCELLED
    # on a timeout. (a) Cancelling them destroys durable child state — a BLOCKED
    # child's blocked state, or a PARKED child's in-flight wake registration and
    # compute-job result — when each would have stayed put exactly as a FAILED child
    # stays terminal. (b) A parent whose remaining children are all BLOCKED/PARKED
    # must still count as all-children-settled so the missed-rollup recovery fires
    # instead of pinning EVALUATING forever.
    TERMINAL = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }

    try:
        from app.models import DeploymentState

        now = _utcnow()

        # ── Phase A: cycle reads on a short-lived session ──────────────────
        # Audit P1 (session hygiene): the cycle-level candidate scan + child
        # snapshot run on their OWN session that is CLOSED before the per-row
        # loop opens any per-row session. Previously a single shared session
        # was held open across the whole loop, and a _bounded() timeout /
        # cancellation mid-DB-op left that shared session in PendingRollback
        # for the REST of the tick — so every later per-row recovery mutation
        # was silently dropped (exactly under the load — many EVALUATING rows +
        # slow GitHub — where recovery matters most). The fix below gives each
        # row its OWN short-lived session, so a cancellation/rollback on one
        # row can never poison the others. Only PLAIN snapshots (ids, statuses)
        # cross the session boundary; no live ORM instance is reused across
        # sessions (which would risk a cross-session lazy-load / MissingGreenlet).
        # NB the in-memory SQLite test DB shares one connection across sessions
        # (StaticPool), so two concurrently-open sessions self-deadlock; Phase A
        # is fully closed before Phase B opens, and per-row sessions are
        # strictly sequential — never two open at once.
        async with services.AsyncSessionLocal() as read_session:
            # Fetch DeployState once per cycle so CI backstop can subtract
            # pause windows from elapsed (admin pause + auto-pause both count).
            # Snapshot the pause fields into a plain object so the per-row
            # sessions (Phase B) don't reuse this read_session's ORM instance.
            _ds_row = await read_session.get(DeploymentState, 1)
            _deploy_state_snapshot = (
                services._DeployPauseSnapshot.from_state(_ds_row) if _ds_row else None
            )
            # Phase B4 (H-P2): drop ``plan_output`` JSONB; the EVALUATING-timeout
            # loop only reads context/payloads/status_log of the heavy JSONB
            # columns. status_log is consumed at lines ~1216 and ~1384 to find
            # the last EVALUATING transition, so it must remain loaded.
            evaluating_tasks = (
                (
                    await read_session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id, Task.status, Task.archived, raiseload=True
                            )
                        )
                        .where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.archived.is_(False),
                        )
                        # C3: per-cycle row cap. Without it this sweep is O(N
                        # EVALUATING backlog) of serial GitHub I/O on the leader
                        # cycle. Deferred rows re-qualify next cycle (the scan is
                        # status-based; oldest-updated first so the most-stuck
                        # parents are always serviced before the cap truncates).
                        # nullsfirst(): a null updated_at must be treated as
                        # maximally stale (never invisible behind the cap limit).
                        .order_by(Task.updated_at.asc().nullsfirst())
                        .limit(services._EVAL_TIMEOUT_MAX_PER_CYCLE)
                    )
                )
                .scalars()
                .all()
            )
            # Snapshot just the candidate IDs — each row is re-fetched fresh in
            # its own per-row session below.
            evaluating_task_ids = [t.id for t in evaluating_tasks]
            if len(evaluating_task_ids) >= services._EVAL_TIMEOUT_MAX_PER_CYCLE:
                logger.warning(
                    "[EVAL TIMEOUT] Hit per-cycle cap of %d EVALUATING rows — "
                    "remainder deferred to next sweep",
                    services._EVAL_TIMEOUT_MAX_PER_CYCLE,
                )

            # Phase B4 (H-P2): batch the child lookup for all evaluating parents
            # into one ``IN (...)`` query instead of one-per-parent (N+1).
            #
            # Only a plain (id, status) snapshot is kept — NOT live ORM rows.
            # The hot all-terminal check below reads the snapshot; the rare
            # branches that mutate children re-fetch them fresh on the per-row
            # session.
            _eval_child_status_by_parent: Dict[uuid.UUID, list[tuple]] = {}
            if evaluating_task_ids:
                _eval_child_rows = (
                    (
                        await read_session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.parent_task_id,
                                    Task.status,
                                    raiseload=True,
                                )
                            )
                            .where(Task.parent_task_id.in_(evaluating_task_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for _ec in _eval_child_rows:
                    _eval_child_status_by_parent.setdefault(
                        _ec.parent_task_id, []
                    ).append((_ec.id, _ec.status))
        # read_session is now closed — no session is held across the loop.

        # ── Phase B: per-row recovery, each on its OWN short-lived session ──
        for _eval_task_id in evaluating_task_ids:
            # C3 heartbeat: this loop performs serial GitHub I/O
            # (get_pull_request / close_pull_request / _check_and_trigger_rollup)
            # per task and is the dominant contributor to the EVALUATING-sweep
            # wall time. Pump the CALLER'S OWN liveness marker each row so a
            # legitimately long-but-progressing sweep is never mistaken for a
            # hung task. NB: this is the background task's own heartbeat, NOT
            # the orchestrator_worker's — pumping the worker's shared marker
            # from here would mask a genuinely hung worker (fix D).
            _heartbeat()
            # Audit P1: open a FRESH session per row. A _bounded() timeout /
            # cancellation that poisons THIS session (PendingRollback) is
            # confined to this iteration — the `async with` closes it on the
            # way out and the next row gets a clean session, so one slow /
            # cancelled row no longer silently drops the rest of the sweep's
            # recovery mutations.
            async with services.AsyncSessionLocal() as session:
                # Re-fetch the candidate fresh on this session and re-validate
                # it is still an unarchived EVALUATING row (it may have been
                # resolved by another worker between Phase A and now). Same
                # load_only column set the cycle scan used to use, so deferred
                # heavy JSONB stays deferred and ``description`` stays loaded
                # for the _pr_state_check_pending consumer's fallback prompt
                # ([[load_only_lazy_load_trap]]).
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.status,
                                    Task.title,
                                    Task.project_id,
                                    Task.parent_task_id,
                                    Task.archived,
                                    Task.requires_approval,
                                    Task.created_at,
                                    Task.started_at,
                                    Task.updated_at,
                                    Task.latest_status_message,
                                    Task.context,
                                    Task.payloads,
                                    Task.status_log,
                                    Task.last_heartbeat,
                                    Task.attempt_count,
                                    Task.account_id,
                                    Task.vm_instance_id,
                                    Task.description,
                                    # Read by classify_failure → _stuck_llm_call_no_progress
                                    # (failure_classification.py); without it the raiseload
                                    # error is silently swallowed there and the RATE_LIMIT
                                    # reclassification is disabled.
                                    Task.api_call_summary,
                                    raiseload=True,
                                )
                            )
                            .where(Task.id == _eval_task_id)
                        )
                    )
                    .scalars()
                    .first()
                )
                if (
                    task is None
                    or task.status != TaskStatus.EVALUATING
                    or task.archived
                ):
                    continue
                # Re-fetch the live DeploymentState on THIS session for the
                # pause-aware elapsed calcs (the snapshot is only used as a
                # fallback if the row went away). Reusing a foreign-session ORM
                # instance here would risk a cross-session lazy-load.
                _deploy_state = (
                    await session.get(DeploymentState, 1)
                ) or _deploy_state_snapshot
                ctx = task.context or {}

                # _pr_state_check_pending consumer. The agent_status FAILED→
                # EVALUATING recovery (agent_status.py ~1567) sets this flag
                # before its GH PR check; if the post-GH lock re-acquire
                # races a concurrent writer (skip_locked returns None at
                # ~1616) the recovery returns ``accepted_pr_check_deferred``
                # leaving the flag stuck and no review task spawned. Without
                # this consumer the task sits until the 48h reaper backstop.
                if await _handle_pr_state_check_pending(
                    task,
                    ctx,
                    session,
                    now,
                    _deploy_state,
                    _heartbeat,
                    _maybe_create_failure_alert,
                ):
                    continue

                if await _handle_evaluating_strand_backstop(
                    task,
                    ctx,
                    session,
                    maybe_create_failure_alert=_maybe_create_failure_alert,
                ):
                    continue

                if await _handle_rollup_lost_marker(task, ctx, session):
                    continue
                # The lost-marker no-merged path stamps _lost_marker_checked_at
                # on task.context and commits without converging; re-sync the
                # loop-local ctx so downstream context writes preserve that
                # back-off timestamp (matches the original inline reassignment).
                ctx = task.context or {}

                # Approval-gate hard timeout. Parents with requires_approval=True
                # remain in EVALUATING waiting for human approval; the
                # evaluating_fail_hours backstop can be deferred by an active
                # pipeline chain, so approval-gated parents need their own
                # companion timeout.
                #
                # TWO disposition paths, keyed on context.approval_hold_reason:
                #  - DELIBERATE spend/budget hold (== "spend_budget"): a longer,
                #    operator-configurable window resolving to a CLEAN NON-FAILURE
                #    CANCELLED terminal (a cost decision an operator has not
                #    reached is not a pipeline failure). NEVER records a failed
                #    task result / failure category / failure alert.
                #  - Everything else (no marker / review-escalation churn): the
                #    EXACT current 2h → FAILED behavior, unchanged.
                if await _handle_approval_gate_timeout(
                    task,
                    ctx,
                    session,
                    now,
                    spend_approval_fail_seconds=SPEND_APPROVAL_FAIL_SECONDS,
                    spend_hours=_spend_hours,
                    maybe_create_failure_alert=_maybe_create_failure_alert,
                ):
                    continue

                ci_waiting = (
                    ctx.get("ci_waiting") is True
                    or ctx.get("review_loop", {}).get("ci_waiting") is True
                )
                if ci_waiting:
                    if await _handle_ci_waiting_backstop(
                        task,
                        ctx,
                        session,
                        now,
                        _deploy_state,
                        _heartbeat,
                        CI_BACKSTOP_SECONDS,
                        CI_BACKSTOP_DEFER_WINDOW_HOURS,
                        TERMINAL,
                        _maybe_create_failure_alert,
                    ):
                        continue

                # Determine how long the task has been EVALUATING
                evaluating_since = None
                # Walk status_log backwards for the last EVALUATING transition
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        break
                if evaluating_since is None:
                    # Fallback to started_at or created_at
                    evaluating_since = task.started_at or task.created_at

                # PAUSE-AWARE age: while the pipeline is PAUSED an EVALUATING
                # task's children cannot run, so it makes no progress and the
                # rollup that would clear EVALUATING never fires. Counting pause
                # time toward the 48h hard timeout (and the 2h warn / reaped-
                # owner grace, which reuse this same ``elapsed``) would force-
                # FAIL the parent, cascade-cancel its children, close their PRs
                # and delete VMs purely because the operator paused. The
                # ``→ EVALUATING`` transition is a synchronous model event with
                # no DeploymentState/async-session access, so no exact baseline
                # can be stamped there; ``active_elapsed_seconds`` falls back to
                # subtracting the currently-open pause window — which guarantees
                # the task is never force-failed WHILE paused (an optional
                # ``evaluating_pause_baseline`` in ctx, if some future path
                # stamps one, upgrades this to exact across multiple windows).
                # deploy_state=None → raw elapsed (unchanged legacy/test path).
                elapsed = pause_clock.active_elapsed_seconds(
                    _deploy_state,
                    evaluating_since,
                    ctx.get("evaluating_pause_baseline"),
                )

                # Child statuses snapshotted at cycle-read time (plain tuples,
                # Phase A). Kept as plain data so they survive across the
                # per-row sessions; the rare branches that MUTATE children
                # re-fetch them fresh on this row's session.
                child_status_snap = _eval_child_status_by_parent.get(task.id, [])

                all_children_terminal = bool(child_status_snap) and all(
                    c_status in TERMINAL for _, c_status in child_status_snap
                )

                # REAPED-OWNER MARKER-LESS FALLBACK (Defect 2) — extracted to
                # app.recovery_evaluating_reaped_owner._handle_reaped_owner_fallback.
                if not all_children_terminal and await _handle_reaped_owner_fallback(
                    task,
                    ctx,
                    session,
                    now,
                    elapsed,
                    _heartbeat,
                    _eval_task_id,
                    _maybe_create_failure_alert,
                ):
                    continue

                # Recovery for missed rollup: all children done but parent still EVALUATING
                if all_children_terminal:
                    # REAPED-OWNER RECONCILE (Defect 1) — extracted to
                    # app.recovery_evaluating_reaped_owner._handle_reaped_owner_reconcile.
                    if await _handle_reaped_owner_reconcile(
                        task,
                        ctx,
                        session,
                        _eval_task_id,
                        _heartbeat,
                    ):
                        continue
                    logger.warning(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has all children terminal "
                        f"but is still EVALUATING — triggering missed rollup"
                    )
                    # Re-fetch a terminal child fresh to trigger rollup.
                    trigger_child = await session.get(Task, child_status_snap[0][0])
                    if trigger_child is not None:
                        # C3: bound the rollup trigger — _check_and_trigger_rollup
                        # does serial GitHub I/O (PR verify/create/merge) per
                        # child. A hung call here would otherwise pin the whole
                        # sweep.
                        #
                        # Audit P1: this _bounded() wraps a coroutine that does
                        # DB I/O on ``session`` — now this ROW's own short-lived
                        # session. An asyncio.wait_for cancellation mid-DB-op
                        # that leaves it in PendingRollback is confined to this
                        # iteration: the explicit rollback below clears it for
                        # the rest of this row, and the per-row ``async with``
                        # closes it before the next row gets a clean session.
                        try:
                            await services._bounded(
                                # C3: thread the caller's own heartbeat so the
                                # per-child PR-verify loop pumps the background
                                # task's liveness marker, not the orchestrator's.
                                _check_and_trigger_rollup(
                                    trigger_child, session, progress_cb=_heartbeat
                                ),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"eval-timeout rollup {str(task.id)[:8]}",
                            )
                        except asyncio.TimeoutError:
                            with contextlib.suppress(Exception):
                                await session.rollback()
                            continue
                    await session.commit()
                    continue

                # 6-hour hard timeout — extracted to
                # app.recovery_evaluating_hard_timeout._handle_evaluating_hard_timeout
                if await _handle_evaluating_hard_timeout(
                    task,
                    ctx,
                    session,
                    now,
                    evaluating_since,
                    elapsed,
                    EVALUATING_FAIL_SECONDS,
                    TERMINAL,
                    _chain_has_recoverable_failed_work,
                    _maybe_create_failure_alert,
                ):
                    continue

                # 2-hour warning
                if elapsed > EVALUATING_WARN_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    # Deduplicate: skip if an undismissed alert exists OR one was
                    # dismissed within the last 2 hours (prevents re-firing after dismiss).
                    dismiss_cooldown = _utcnow() - timedelta(hours=2)
                    existing = (
                        (
                            await session.execute(
                                select(Alert).where(
                                    Alert.task_id == task.id,
                                    or_(
                                        Alert.dismissed_at.is_(None),
                                        Alert.dismissed_at >= dismiss_cooldown,
                                    ),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing is None:
                        logger.warning(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                            f"for {hours}h — children may be stuck"
                        )
                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.WARNING,
                            title=f"Task {task.title or str(task.id)[:8]} stuck evaluating",
                            message=(
                                f"Task has been evaluating for {hours} hours "
                                f"— children may be stuck"
                            ),
                            task_id=task.id,
                        )
                        session.add(alert)
                        await session.commit()
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "alert_created",
                            {
                                "alert_id": str(alert.id),
                                "severity": "WARNING",
                                "title": alert.title,
                            },
                        )
    except Exception as e:
        logger.error(
            f"[EVAL TIMEOUT] Evaluating timeout sweep failed: {e}", exc_info=True
        )
