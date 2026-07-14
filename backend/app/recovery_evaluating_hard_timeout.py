"""Hard-timeout disposition handler for EVALUATING tasks.

Extracted from ``app.recovery_evaluating._check_evaluating_timeouts``.
Called when a task has been EVALUATING longer than ``EVALUATING_FAIL_SECONDS``.
Marks the parent FAILED, cascade-cancels non-terminal children, closes orphaned
rollup PRs, and broadcasts terminal status updates.

Returns ``True`` when the caller should ``continue`` to the next task row.
"""

import logging
from datetime import datetime

from sqlmodel import select

from app import services
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)


async def _handle_evaluating_hard_timeout(
    task,
    ctx,
    session,
    now,
    evaluating_since,
    elapsed,
    evaluating_fail_seconds,
    terminal,
    chain_has_recoverable_failed_work,
    maybe_create_failure_alert,
) -> bool:
    """Handle the hard-timeout disposition for an EVALUATING task.

    Returns True if the caller should skip to the next row (continue).
    """
    if elapsed <= evaluating_fail_seconds:
        return False

    # If the chain has an actively heartbeating review/fix task, the pipeline
    # is still doing real work — defer to next sweep.
    if await services._chain_has_active_pipeline_work(task, session):
        logger.info(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
            f"{evaluating_fail_seconds // 3600}h hard timeout but "
            f"chain has fresh review/fix heartbeat — skipping kill"
        )
        return True

    # Recoverable-FAILED defer: a child / chain pipeline task in
    # FAILED with infra-class failure_category may come back via
    # resurrection sweep or operator requeue. Cascade-killing
    # mid-recovery destroys salvageable work (incident
    # 2026-04-29: PRs #3450/#3408 + parents c30701d3/ddd987fc).
    # Defer for a bounded window so a truly stuck case still
    # terminates eventually.
    EVAL_DEFER_WINDOW_HOURS = 24
    first_defer_at = ctx.get("_eval_timeout_first_defer_at")
    # Treat a defer marker older than evaluating_since as stale
    # (left over from a previous EVALUATING transition).
    if first_defer_at:
        try:
            _fd = datetime.fromisoformat(first_defer_at)
            if _fd < evaluating_since:
                first_defer_at = None
        except (ValueError, TypeError):
            first_defer_at = None
    has_recoverable = await chain_has_recoverable_failed_work(task, session)
    if has_recoverable:
        if first_defer_at:
            try:
                _fd = datetime.fromisoformat(first_defer_at)
                defer_elapsed = (now - _fd).total_seconds()
            except (ValueError, TypeError):
                defer_elapsed = 0.0
            if defer_elapsed <= EVAL_DEFER_WINDOW_HOURS * 3600:
                logger.info(
                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                    f"{evaluating_fail_seconds // 3600}h but chain "
                    f"has recoverable FAILED children — deferring "
                    f"({round(defer_elapsed / 3600, 1)}h into "
                    f"{EVAL_DEFER_WINDOW_HOURS}h window)"
                )
                return True
            logger.warning(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                f"{EVAL_DEFER_WINDOW_HOURS}h defer window despite recoverable "
                f"FAILED children — proceeding to kill"
            )
            # fall through to existing kill
        else:
            from sqlalchemy.orm.attributes import flag_modified

            task.context = {
                **ctx,
                "_eval_timeout_first_defer_at": now.isoformat(),
            }
            flag_modified(task, "context")
            session.add(task)
            logger.warning(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                f"{evaluating_fail_seconds // 3600}h with "
                f"recoverable FAILED children — deferring cascade-cancel "
                f"for up to {EVAL_DEFER_WINDOW_HOURS}h"
            )
            await maybe_create_failure_alert(
                task,
                session,
                f"Evaluation past {evaluating_fail_seconds // 3600}h with "
                f"recoverable infra failures in chain — deferring "
                f"cascade-cancel for up to {EVAL_DEFER_WINDOW_HOURS}h",
            )
            await session.commit()
            return True

    hours = round(elapsed / 3600, 1)
    logger.error(
        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
        f"for {hours}h — hard timeout, marking FAILED"
    )
    task.status = TaskStatus.FAILED
    task.latest_status_message = (
        f"Evaluation timeout — children did not complete "
        f"within {evaluating_fail_seconds // 3600} hours"
    )
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
    # status snapshot, and the cascade-cancel + PR-close below
    # read and mutate the live rows.
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )

    # Cascade-cancel non-terminal children
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.max_plan_pool import release_max_plan_account

    non_terminal_children = [c for c in children if c.status not in terminal]
    vms_to_delete = []
    hard_timeout_cancel_msg = f"Cancelled: parent evaluation timeout after {hours}h"
    for child in non_terminal_children:
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = hard_timeout_cancel_msg
        if child.vm_instance_id:
            vms_to_delete.append(child)
        child_acct = (child.context or {}).get("max_plan_account_id")
        if child_acct:
            await release_max_plan_account(
                child_acct,
                project_id=str(child.project_id) if child.project_id else None,
            )
        session.add(child)
    if non_terminal_children:
        logger.info(
            f"[EVAL TIMEOUT] Cascade-cancelled {len(non_terminal_children)} "
            f"children of timed-out task {str(task.id)[:8]}"
        )

    # Close orphaned rollup PRs from CI-waiting children
    from app.github import close_pull_request

    for child in children:
        child_pr = (child.context or {}).get("review_loop", {}).get("pr_number")
        if child_pr and not (child.context or {}).get("pr_merged"):
            try:
                from app.routers.agent import get_github_token_for_task

                _gh, _owner, _repo = await services._bounded(
                    get_github_token_for_task(child.id, session),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"eval-timeout token {str(child.id)[:8]}",
                )
                await services._bounded(
                    close_pull_request(_gh, _owner, _repo, child_pr),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"eval-timeout close PR #{child_pr}",
                )
                logger.info(
                    f"[EVAL TIMEOUT] Closed orphaned PR #{child_pr} from child {str(child.id)[:8]}"
                )
            except Exception as pr_err:
                logger.warning(
                    f"[EVAL TIMEOUT] Failed to close PR #{child_pr}: {pr_err}"
                )

    # Pre-capture identifiers before the alert. The alert (which may
    # fail and trigger a rollback) and the subsequent commit can expire
    # the session's instances — even PK attributes (due to dict_.clear()
    # in InstanceState._expire) require a lazy-load that crashes with
    # MissingGreenlet on asyncpg.  Capture literals now and use them in
    # the post-commit broadcast instead of reading from expired instances.
    _bcast_project_id = str(task.project_id)
    _bcast_parent_id = str(task.id)
    _bcast_child_ids = [
        str(c.id) for c in non_terminal_children
    ]

    # Create ERROR alert (deduplicated by _maybe_create_failure_alert)
    await maybe_create_failure_alert(
        task,
        session,
        f"Evaluation timeout after {hours}h — children did not complete",
    )
    await session.commit()
    # task.status=FAILED / children=CANCELLED were set above; the
    # PR-close calls + alert + commit can poison/expire these
    # instances, so broadcast the known literals rather than
    # post-commit lazy-loads on expired instances.
    await services.broadcast_to_project(
        _bcast_project_id,
        "task_updated",
        {"task_id": _bcast_parent_id, "status": TaskStatus.FAILED},
    )
    for child_id in _bcast_child_ids:
        await services.broadcast_to_project(
            _bcast_project_id,
            "task_updated",
            {"task_id": child_id, "status": TaskStatus.CANCELLED},
        )
    for child_vm in vms_to_delete:
        await _ensure_vm_deleted(child_vm, reason="parent_eval_timeout")
    return True
