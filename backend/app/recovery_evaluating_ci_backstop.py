"""CI-waiting backstop handler, extracted from ``app.recovery_evaluating``.

A single self-contained per-row recovery branch lifted out of the
``_check_evaluating_timeouts`` Phase-B loop. It handles EVALUATING parents that
are blocked in a CI-wait state (``context.ci_waiting=True`` or
``review_loop.ci_waiting=True``) that have exceeded the configurable
``CI_BACKSTOP_SECONDS`` deadline.

The block:

  * Computes the CI-wait elapsed time using pause_clock accounting.
  * Checks early-recovery eligibility when CI passed but the review VM died.
  * Calls ``_verify_ci_before_backstop`` to read live CI state before killing.
  * Defers cascade-cancel when the chain has recoverable/retrying work.
  * Calls ``_attempt_ci_backstop_recovery`` to spawn a fresh review if
    ``ci_passed`` but review never approved.
  * Acquires a FOR-UPDATE lock and re-validates before marking FAILED.
  * Cascade-cancels children, closes orphaned PRs, emits alerts and broadcasts.

``_handle_ci_waiting_backstop`` returns ``True`` when it handled the row (the
caller ``continue``s) or ``False`` to fall through to the remaining recovery
branches unchanged (CI-wait conditions not met). The move is purely structural —
commit / notification ordering remains unchanged; review-gate authority is shared
with ``ci_merge_helpers`` so legacy verdict mirrors cannot suppress recovery.
``services.*`` refs and function-local deferred imports are kept unchanged so
test patches against ``app.services.<name>``, ``app.orchestrator.*``,
``app.failure_classification.*``, ``app.routers.agent_status.*``, etc. still
take effect.
"""

import contextlib
import logging
from datetime import datetime

from app import services
from app.ci_merge_helpers import _review_approved as _review_gate_is_approved
from app.models import Task, TaskStatus
from app.recovery_evaluating_helpers import (
    _chain_has_recoverable_failed_work,
    _chain_has_unexhausted_optimistic_retry_work,
    _summarize_latest_review_for_chain,
    _ci_backstop_timeout_reason,
    _attempt_ci_backstop_recovery,
    _verify_ci_before_backstop,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _handle_ci_waiting_backstop(
    task,
    ctx,
    session,
    now,
    _deploy_state,
    _heartbeat,
    ci_backstop_seconds,
    ci_backstop_defer_window_hours,
    TERMINAL,
    maybe_create_failure_alert,
) -> bool:
    """CI-waiting backstop handler for the EVALUATING-timeout sweep.

    Returns ``True`` when the row was handled (caller ``continue``s);
    ``False`` to fall through to the next recovery branch.

    Must only be called when ``ci_waiting`` is already confirmed by the caller.
    """
    from app import pause_clock

    # CI-waiting tasks have their own 20-min escalation in orchestrator.py.
    # Apply a 30-minute absolute backstop so tasks can never
    # hang forever regardless of field state.
    #
    # Time base: prefer review_loop.ci_wait_started (when CI wait
    # actually began) over the last EVALUATING transition. A task
    # may sit in EVALUATING for a long time waiting on subtasks
    # before the rollup PR is opened and CI polling starts — using
    # the EVALUATING transition would false-positive the backstop
    # the instant CI finally begins.
    rl = ctx.get("review_loop", {})
    ci_ws_val = rl.get("ci_wait_started")
    was_escalated = rl.get("escalated", False)
    evaluating_since = None
    ci_ws_valid = False
    if ci_ws_val is not None:
        try:
            evaluating_since = datetime.fromisoformat(ci_ws_val)
            ci_ws_valid = True
        except (ValueError, TypeError):
            evaluating_since = None
    if evaluating_since is None:
        for entry in reversed(task.status_log or []):
            msg = entry.get("msg", "")
            if "→ EVALUATING" in msg:
                with contextlib.suppress(KeyError, ValueError, TypeError):
                    evaluating_since = datetime.fromisoformat(entry["t"])
                break
    if evaluating_since is None:
        evaluating_since = task.started_at or task.created_at

    # Pause-aware elapsed: subtract any pause windows that
    # occurred since the timer started. Legacy timers without
    # a stashed baseline fall back to raw elapsed.
    _ci_baseline = rl.get("ci_wait_pause_baseline")
    elapsed = (
        pause_clock.active_elapsed(
            _deploy_state,
            evaluating_since,
            _ci_baseline,
        )
        if _deploy_state
        else (now - evaluating_since).total_seconds()
    )
    # Early trigger: if CI has passed but review_approved is
    # missing AND the review child task is already terminal
    # (FAILED/CANCELLED), the review VM died silently — we
    # have enough signal to recover now without waiting for
    # the 30-min backstop. The elapsed guard remains as the
    # fallback for cases where review status hasn't become
    # terminal yet.
    rl_ci_passed = bool(rl.get("ci_passed"))
    rl_review_approved = _review_gate_is_approved(rl)
    rl_recovery_attempted = bool(rl.get("review_recovery_attempted"))
    early_recovery_eligible = False
    if rl_ci_passed and not rl_review_approved and not rl_recovery_attempted:
        _early_summary = await _summarize_latest_review_for_chain(task, session)
        if _early_summary and _early_summary["status"] in (
            "FAILED",
            "CANCELLED",
        ):
            early_recovery_eligible = True
    if elapsed <= ci_backstop_seconds and not early_recovery_eligible:
        # Under backstop threshold — let normal CI timeout handle it
        return True
    # Pipeline still actively working on the chain (review/fix
    # task heartbeated within 10min) — defer to next sweep so
    # we don't kill legitimate in-flight iteration cycles.
    if await services._chain_has_active_pipeline_work(task, session):
        logger.info(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop but chain "
            f"has fresh review/fix heartbeat — skipping kill"
        )
        return True

    # Gap C: verify REAL CI state before declaring a timeout.
    # The backstop fires on a wall-clock that can outlast a CI
    # conclusion that actually landed before the wait window even
    # started (incident 2026-06-07: review_loop.ci_wait_started
    # was re-armed AFTER CI had concluded, so the backstop "waited
    # 1h for a conclusion that was already in"). Before failing a
    # parent whose PR still has a head_sha to check, read the PR's
    # live check-run conclusion and route on the truth:
    #   concluded FAILURE → existing CI-failure handling
    #       (handle_ci_completion fix-spawn path / ci_passed=False)
    #   concluded SUCCESS → record the pass + clear the wait
    #   pending / GitHub error → fall through to the timeout
    #       (fail-OPEN: never kill, but never block on an outage).
    # ALL GitHub I/O is strictly OUTSIDE the FOR-UPDATE window
    # below — handle_ci_completion re-locks the row itself.
    # Resolve the PR via the canonical accessor (review_loop.pr_number OR
    # context.rollup_pr_number) — a rollup parked with its identity only at
    # context.rollup_pr_number is now verified before any kill. head_sha is
    # resolved live inside _verify_ci_before_backstop when the loop never cached
    # one (the old hard head_sha precondition is what starved those rows of a
    # verify before the deadline).
    from app.ci_merge_helpers import effective_pr_number

    if effective_pr_number(task):
        _ci_resolved = await _verify_ci_before_backstop(
            task, rl, rl.get("head_sha"), session
        )
        if _ci_resolved:
            # CI reached a terminal verdict — handled (merge / fix-spawn /
            # pass recorded / wait cleared). Skip the kill.
            return True

    # Gap A: recoverable / freshly-requeuing work guard. Mirror
    # the 6h hard-timeout path: a child / chain pipeline task that
    # is FAILED with an infra-class category (resurrection-sweep
    # or operator-requeue eligible) OR is QUEUED with optimistic-
    # retry budget still left must NOT be cascade-cancelled while
    # it can still come back. Defer for a bounded window so a truly
    # stuck case still terminates, and respect the optimistic-retry
    # cap so an exhausted child stops deferring the backstop.
    _has_recoverable = await _chain_has_recoverable_failed_work(task, session)
    _has_retrying = await _chain_has_unexhausted_optimistic_retry_work(task, session)
    if _has_recoverable or _has_retrying:
        from sqlalchemy.orm.attributes import flag_modified

        _ci_first_defer_at = rl.get("_ci_backstop_first_defer_at")
        # A defer marker older than this CI-wait episode is stale
        # (left from a prior ci_wait_started) — discard it so the
        # window measures from the current wait.
        if _ci_first_defer_at and ci_ws_valid and evaluating_since:
            try:
                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                if _cfd < evaluating_since:
                    _ci_first_defer_at = None
            except (ValueError, TypeError):
                _ci_first_defer_at = None
        if _ci_first_defer_at:
            try:
                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                _ci_defer_elapsed = (now - _cfd).total_seconds()
            except (ValueError, TypeError):
                _ci_defer_elapsed = 0.0
            if _ci_defer_elapsed <= ci_backstop_defer_window_hours * 3600:
                logger.info(
                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI "
                    f"backstop but chain has recoverable/requeuing work "
                    f"— deferring ({round(_ci_defer_elapsed / 3600, 1)}h "
                    f"into {ci_backstop_defer_window_hours}h window)"
                )
                return True
            logger.warning(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                f"{ci_backstop_defer_window_hours}h CI-backstop defer "
                f"window despite recoverable work — proceeding to kill"
            )
            # fall through to existing kill
        else:
            new_rl = dict(rl)
            new_rl["_ci_backstop_first_defer_at"] = now.isoformat()
            task.context = {
                **(task.context or {}),
                "review_loop": new_rl,
            }
            flag_modified(task, "context")
            session.add(task)
            logger.warning(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop "
                f"with recoverable/requeuing chain work — deferring "
                f"cascade-cancel for up to "
                f"{ci_backstop_defer_window_hours}h"
            )
            await session.commit()
            return True

    hours = round(elapsed / 3600, 1)
    ci_passed = bool(rl.get("ci_passed"))
    review_approved = _review_gate_is_approved(rl)
    recovery_attempted = bool(rl.get("review_recovery_attempted"))
    review_summary = await _summarize_latest_review_for_chain(task, session)

    # Compose an honest reason. The historic "CI polling stalled after
    # escalation" was a lie when the actual blocker was a missing review
    # callback (review VM died silently) — name the gate that is open.
    reason = _ci_backstop_timeout_reason(
        ci_ws_val=ci_ws_val,
        ci_ws_valid=ci_ws_valid,
        ci_passed=ci_passed,
        review_approved=review_approved,
        was_escalated=was_escalated,
        review_summary=review_summary,
        review_loop=rl,
        parent_ctx=ctx,
    )

    # One-shot recovery: if CI passed but the review-approved gate never
    # closed (common when the review VM died before POSTing status),
    # spawn a fresh review and reset the CI-wait clock instead of hard
    # killing. The dedup guard will supersede any stale corpse.
    if ci_passed and not review_approved and not recovery_attempted:
        recovered = await _attempt_ci_backstop_recovery(task, session, rl)
        if recovered:
            from sqlalchemy.orm.attributes import flag_modified

            new_rl = dict(rl)
            new_rl["review_recovery_attempted"] = True
            new_rl["review_recovery_at"] = _utcnow().isoformat()
            new_rl["ci_wait_started"] = _utcnow().isoformat()
            new_rl["ci_wait_pause_baseline"] = (
                pause_clock.total_paused_seconds(_deploy_state)
                if _deploy_state
                else 0.0
            )
            task.context = {
                **(task.context or {}),
                "review_loop": new_rl,
            }
            flag_modified(task, "context")
            task.latest_status_message = (
                f"CI passed on PR #{new_rl.get('pr_number')} but review never "
                f"completed ({reason}) — spawned fresh review, backstop reset"
            )
            session.add(task)
            await session.commit()
            # Recovery path keeps the task EVALUATING (reset CI-wait
            # clock). _attempt_ci_backstop_recovery above can poison/
            # roll back the session and expire `task`, so use the
            # literal rather than a post-commit lazy-load.
            await services.broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": TaskStatus.EVALUATING,
                },
            )
            logger.warning(
                f"[EVAL TIMEOUT] Backstop recovery for task "
                f"{str(task.id)[:8]}: spawned fresh review on "
                f"PR #{rl.get('pr_number')} ({reason})"
            )
            return True

    # C3 lost-update guard: this backstop branch reached the
    # FAILED decision from a NON-locked read of `task`, after
    # several awaits (_chain_has_active_pipeline_work,
    # _summarize_latest_review_for_chain, _attempt_ci_backstop_
    # recovery). This sweep now runs in its OWN background task
    # (_evaluating_timeout_service, lifecycle.py) and can
    # interleave with the orchestrator's CI-poll loop
    # (orchestrator.py:2994), which locks the SAME row FOR UPDATE
    # and may resolve it (e.g. COMPLETED on merge) in the window
    # between our read and this commit. Acquire the row lock and
    # RE-VALIDATE under it before clobbering: if the CI loop holds
    # the lock (skip_locked → None) or already moved the row out
    # of EVALUATING, or the timeout no longer holds on the fresh
    # context, defer to the next sweep instead of overwriting a
    # state the CI loop just committed. Locking the single row at
    # the decision point adds no serial coupling across rows.
    from app.orchestrator import _lock_task_for_mutation

    locked_task = await _lock_task_for_mutation(
        session,
        task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="eval-timeout CI backstop FAIL",
    )
    if locked_task is None:
        # Row is locked by the CI-poll loop, or no longer
        # EVALUATING (already resolved). _lock_task_for_mutation
        # has already committed/rolled back to release. Skip.
        return True
    # populate_existing=True refreshed `locked_task` from the DB,
    # so re-derive the CI-wait timer + active-pipeline guard from
    # the *current* context and confirm the backstop still fires.
    _rl_fresh = (locked_task.context or {}).get("review_loop", {}) or {}
    if not (
        _rl_fresh.get("ci_waiting") is True
        or (locked_task.context or {}).get("ci_waiting") is True
    ):
        # CI loop cleared ci_waiting (e.g. merged/closed) between
        # our read and the lock — no longer our row to fail.
        await session.rollback()
        return True
    _ci_ws_fresh = _rl_fresh.get("ci_wait_started")
    _since_fresh = None
    if _ci_ws_fresh is not None:
        with contextlib.suppress(ValueError, TypeError):
            _since_fresh = datetime.fromisoformat(_ci_ws_fresh)
    if _since_fresh is None:
        _since_fresh = evaluating_since
    _baseline_fresh = _rl_fresh.get("ci_wait_pause_baseline")
    _elapsed_fresh = (
        pause_clock.active_elapsed(_deploy_state, _since_fresh, _baseline_fresh)
        if _deploy_state
        else (now - _since_fresh).total_seconds()
    )
    if _elapsed_fresh <= ci_backstop_seconds and not early_recovery_eligible:
        # CI loop reset the wait clock (e.g. recovery / fresh
        # poll) in the unlocked window — backstop no longer due.
        await session.rollback()
        return True
    # Re-validated under the FOR UPDATE lock: still EVALUATING,
    # still CI-waiting, still past the backstop. Safe to fail.
    task = locked_task
    logger.error(
        f"[EVAL TIMEOUT] CI-waiting task {str(task.id)[:8]} has been "
        f"EVALUATING for {hours}h — backstop timeout ({reason}), marking FAILED"
    )
    task.status = TaskStatus.FAILED
    task.latest_status_message = f"CI wait backstop timeout after {hours}h ({reason})"
    from app.failure_classification import (
        classify_failure,
        persist_failure_category,
    )
    from app.routers.agent_status import record_task_result

    _fc = classify_failure(task)
    persist_failure_category(task, _fc)
    record_task_result(
        success=False,
        failure_class=_fc,
        task_id=str(task.id),
        task_type=task.first_payload.get("type", ""),
    )
    session.add(task)
    # Re-fetch children fresh — the batch holds only a plain
    # status snapshot, and this branch reads + mutates the live
    # rows (cancel, latest_status_message, vm_instance_id).
    from sqlmodel import select

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.max_plan_pool import release_max_plan_account

    # TERMINAL REASON. See the sibling comment in
    # ``recovery_evaluating_hard_timeout``: until this stamp existed the only
    # record of why these children died was the prose in ``child_cancel_msg``,
    # matched by a literal substring in
    # ``platform_kill_metrics._TERMINAL_MESSAGE_MARKERS`` — a fan-out of platform
    # kills held hostage to the wording of a log line. ``failure_detail`` is a
    # durable field read at the classifier's highest precedence.
    #
    # Deliberately a detail and NOT a ``failure_category``: the matching
    # ``_DETAIL_VERDICTS`` entry reproduces the marker's PLATFORM_KILL verdict
    # exactly, so this moves the evidence without moving the number, and leaves
    # ``is_non_death_cancellation`` untouched.
    from app.failure_classification import PARENT_CI_BACKSTOP_CASCADE_DETAIL

    non_terminal_children = [c for c in children if c.status not in TERMINAL]
    vms_to_delete = []
    child_cancel_msg = (
        f"Cancelled: parent CI wait backstop timeout after {hours}h ({reason})"
    )
    for child in non_terminal_children:
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = child_cancel_msg
        child.context = {
            **(child.context or {}),
            "failure_detail": PARENT_CI_BACKSTOP_CASCADE_DETAIL,
        }
        if child.vm_instance_id:
            vms_to_delete.append(child)
        child_acct = (child.context or {}).get("max_plan_account_id")
        if child_acct:
            await release_max_plan_account(
                child_acct,
                project_id=str(child.project_id) if child.project_id else None,
            )
        session.add(child)
    await maybe_create_failure_alert(
        task,
        session,
        f"CI wait backstop timeout after {hours}h — {reason}",
    )
    await session.commit()
    # task.status=FAILED / children=CANCELLED were set above; the
    # alert + commit can poison/expire these instances, so broadcast
    # the known literals rather than post-commit lazy-loads. child.id
    # (PK) is always safe to read.
    await services.broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.FAILED},
    )
    for child in non_terminal_children:
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(child.id), "status": TaskStatus.CANCELLED},
        )
    for child_vm in vms_to_delete:
        await _ensure_vm_deleted(child_vm, reason="ci_backstop_timeout")
    return True
