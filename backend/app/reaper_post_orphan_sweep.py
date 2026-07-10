"""Post-reap cleanup sweep: deferred work that runs after the orphan-loop commits.

After reaper_service()'s per-orphan mutation loop commits, this module handles
the independent cleanup operations that consume the loop's output:

1. Deferred parent requeue (child clone + dep remap released from row lock)
2. Old-VM deletion (IDs captured before the reap overwrite)
3. Rollup checks for failed tasks with parents
4. Terminal-dependency unblocking for QUEUED dependents
5. Safeguard merge-gate release for reaped security_audit tasks
6. Dead-PR closing for exhausted fix tasks
"""

import contextlib
import logging

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)


async def run_post_orphan_sweep(
    session,
    orphaned_tasks,
    *,
    orphan_commit_ok: bool,
    orphaned_task_ids: list,
    deferred_parent_requeues: list,
    reaper_old_vms: dict,
    progress_cb,
    landed_cb,
) -> None:
    """Run the post-reap cleanup sweep.

    Called immediately after the orphan-loop commits. All operations are
    independent of the per-orphan row lock — they only consume the loop's
    output (lists of task IDs / parent tasks / old VM IDs).

    Args:
        session: Shared reaper AsyncSession.
        orphaned_tasks: Tasks processed by the orphan loop (non-empty guard).
        orphan_commit_ok: True if the orphan-loop batch commit succeeded.
        orphaned_task_ids: IDs of all orphaned tasks (for fresh re-fetch).
        deferred_parent_requeues: Parent tasks to requeue outside the row lock.
        reaper_old_vms: {old_vm_id: vm_name} dict to delete post-commit.
        progress_cb: Callable that marks reaper watchdog progress (heartbeat).
        landed_cb: Async callable(parent_id, session) for warm-reuse stamping.
    """
    if not (orphaned_tasks and orphan_commit_ok):
        return

    # Deferred imports (mirrors reaper_service() pattern to avoid circular deps).
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.task_lifecycle import _requeue_parent_task
    from app.rollup_management import _check_and_trigger_rollup

    # Re-fetch fresh task state by id before the post-commit loops.
    # A per-task reap crash above triggers session.rollback() (2211),
    # which expires EVERY orphaned_tasks ORM object (rollback expires
    # regardless of expire_on_commit). Reading task.status/.context on
    # an expired object below drives a sync lazy-refresh on the
    # AsyncSession -> MissingGreenlet (see 1028-1036). Re-fetching by id
    # keeps every attribute access inside the async greenlet bridge and
    # reflects the committed (or rolled-back) state of each task.
    post_reap_tasks: list[Task] = []
    if orphaned_task_ids:
        _rows = await session.execute(
            select(Task).where(Task.id.in_(orphaned_task_ids))
        )
        post_reap_tasks = list(_rows.scalars().all())

    # Snapshot ids now (fresh objects). Both post-commit loops below re-fetch
    # each task by id because the internal rollbacks (97 deferred-requeue, 142
    # rollup-check) expire EVERY post_reap_tasks ORM object regardless of
    # expire_on_commit; a raw task.status read on an expired instance would fire
    # a sync lazy-load inside the AsyncSession -> MissingGreenlet and abort the
    # sweep uncaught.
    post_reap_task_ids = [t.id for t in post_reap_tasks]

    # Audit A8: now that the orphan-row lock is released, do the
    # slow parent-requeue work (child clone + dep remap). Heartbeats
    # on the parent task will go through fine since we only set
    # requeue_in_flight (a marker), not a row lock.
    for parent in deferred_parent_requeues:
        # Heartbeat: parent requeue clones children + remaps deps
        # (multiple queries) per parent — keep the watchdog happy.
        progress_cb()
        try:
            await _requeue_parent_task(parent, session)
        except Exception as e:  # per-attempt try in deferred-requeue loop
            logger.error(
                f"[REAPER] Deferred parent requeue failed for {parent.id}: {e}",
                exc_info=True,
            )
            # C1: _requeue_parent_task commits internally; a
            # failure (IntegrityError on the cloned child) leaves
            # the shared session aborted, which would poison the
            # remaining requeues, the post-reap rollup loop, and
            # Steps 3-10. Roll back so each subsequent operation
            # starts on a clean session.
            with contextlib.suppress(Exception):
                await session.rollback()
        finally:
            # Always clear the in-flight marker so subsequent reaper
            # cycles can re-evaluate if requeue itself failed.
            parent.requeue_in_flight = False
            session.add(parent)
    if deferred_parent_requeues:
        await session.commit()

    # Delete old VMs after commit (IDs captured before overwrite)
    for old_vm in reaper_old_vms.values():
        # Heartbeat: each VM delete is a GCE API call.
        progress_cb()
        await _ensure_vm_deleted(old_vm, reason="reaper")

    # Trigger rollup checks for failed tasks with parents (after commit)
    # Batch-fetch ALL tasks once before the loop to avoid N+1 session.get calls.
    # The rollback inside the exception handler expires all ORM objects; after
    # a rollback we re-batch in a single query instead of falling back to per-
    # iteration gets.
    _rollup_task_batch: dict = {}
    if post_reap_task_ids:
        _rows = await session.execute(
            select(Task).where(Task.id.in_(post_reap_task_ids))
        )
        _rollup_task_batch = {t.id: t for t in _rows.scalars().all()}

    for _post_reap_task_id in post_reap_task_ids:
        # Heartbeat: _check_and_trigger_rollup hits GitHub per task.
        progress_cb()
        task = _rollup_task_batch.get(_post_reap_task_id)
        if task is None:
            continue
        if task.status == TaskStatus.FAILED and task.parent_task_id:
            try:
                # C3: pass the heartbeat so the per-child
                # _verify_pr_merged loop inside the rollup check
                # also heartbeats — a fan-heavy parent's PR-verify
                # round-trips cannot silently exhaust
                # REAPER_HANG_SECONDS on this loop either (mirrors
                # the stale-EVALUATING sweep at reaper.py:2789).
                # C4: _check_and_trigger_rollup may perform a silent internal
                # rollback (e.g. `_sess_poisoned` recovery) WITHOUT re-raising.
                # When that happens, all ORM objects in the shared session are
                # expired — the next iteration's `task.status` would trigger a
                # sync lazy-refresh inside the AsyncSession and raise
                # MissingGreenlet on asyncpg. Re-batch immediately if the
                # return value signals a silent rollback.
                _rolled_back = await _check_and_trigger_rollup(
                    task, session, progress_cb=progress_cb
                )
                if _rolled_back:
                    _rows = await session.execute(
                        select(Task).where(Task.id.in_(post_reap_task_ids))
                    )
                    _rollup_task_batch = {t.id: t for t in _rows.scalars().all()}
                # Completion-side warm-reuse: if that landing
                # completed the parent rollup, stamp the
                # _deps_satisfied marker on the dependents it just
                # unblocked so a polling warm VM can claim them
                # before tearing down cold (see helper docstring).
                await landed_cb(task.parent_task_id, session)
            except Exception as e:
                logger.error(
                    f"[REAPER] Rollup check failed for task {task.id}: {e}",
                    exc_info=True,
                )
                # C1: _check_and_trigger_rollup runs FOR UPDATE
                # selects + commits that can raise on a DB error,
                # leaving the shared session aborted. Roll back so
                # the next task's rollup check (and Steps 3-10) do
                # not inherit a PendingRollback session.
                with contextlib.suppress(Exception):
                    await session.rollback()
                # Re-batch after rollback: the rollback expired every
                # ORM object. A single batch query replaces up to N
                # individual session.get() calls for remaining tasks.
                _rows = await session.execute(
                    select(Task).where(Task.id.in_(post_reap_task_ids))
                )
                _rollup_task_batch = {t.id: t for t in _rows.scalars().all()}

    # Immediately unblock dependents of terminally failed tasks
    # Batch-fetch ALL tasks once before the loop so we read from a local
    # dict instead of issuing N individual session.get() queries.
    _unblock_task_batch: dict = {}
    if post_reap_task_ids:
        _rows = await session.execute(
            select(Task).where(Task.id.in_(post_reap_task_ids))
        )
        _unblock_task_batch = {t.id: t for t in _rows.scalars().all()}

    for _post_reap_task_id in post_reap_task_ids:
        # Heartbeat: per-task dependent-unblock opens its own session.
        progress_cb()
        task = _unblock_task_batch.get(_post_reap_task_id)
        if task is None:
            continue
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
                        wt.depends_on = [
                            d for d in (wt.depends_on or []) if d != str(task.id)
                        ]
                        dep_session.add(wt)
                        count += 1
                    if count:
                        await dep_session.commit()
                        logger.info(
                            f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                        )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                    exc_info=True,
                )

    # C5: release the cross-cutting safeguard merge gate when a
    # reaper-terminated safeguard task FAILS. The gate is otherwise
    # released only on the security_audit COMPLETED branch, so a
    # reaped safeguard (orphan-timeout, retries exhausted) would
    # leave orig.requires_approval=True forever — a permanent
    # strand that services.py then 2h-timeout-FAILs into a healthy
    # parent. Each release runs in its OWN session (mirrors the
    # dependent-unblock loop above and reaper Step 9
    # sweep_stuck_qualifications) so a failure here cannot poison
    # the shared reaper session. _maybe_release_safeguard_gate
    # no-ops for non-safeguard tasks; findings=None → release
    # (a reaped safeguard produced no blocker).
    for task in post_reap_tasks:
        progress_cb()
        if task.status != TaskStatus.FAILED:
            continue
        _t_payload = task.first_payload if task.payloads else {}
        if _t_payload.get("type") != "security_audit":
            continue
        try:
            from app.task_completion import (
                _maybe_release_safeguard_gate,
            )

            async with AsyncSessionLocal() as _sg_session:
                _sg_task = await _sg_session.get(Task, task.id)
                if _sg_task is not None:
                    await _maybe_release_safeguard_gate(_sg_task, None, _sg_session)
        except Exception as _sg_exc:  # noqa: BLE001
            logger.warning(
                f"[SAFEGUARD] Failed to release gate on reaped "
                f"safeguard {task.id}: {_sg_exc}",
                exc_info=True,
            )

    # Close open PRs owned by terminally-failed fix tasks. When a
    # fix exhausts retries, its PR stays OPEN on GitHub, and
    # sibling tasks whose rollup waits on open PRs targeting the
    # same research branch block forever. Closing the dead PR
    # removes it from the sibling rollup-wait gate.
    # Batch-fetch unique projects referenced by FAILED fix tasks
    # so each _proj lookup below uses a dict instead of a
    # per-iteration session.get(Project, ...).
    _dead_pr_project_cache: dict = {}
    _failed_fix_pids = {
        t.project_id
        for t in post_reap_tasks
        if t.status == TaskStatus.FAILED
        and (t.first_payload or {}).get("type") == "fix"
        and t.project_id is not None
    }
    if _failed_fix_pids:
        _rows = await session.execute(
            select(Project).where(Project.id.in_(list(_failed_fix_pids)))
        )
        _dead_pr_project_cache = {p.id: p for p in _rows.scalars().all()}

    for task in post_reap_tasks:
        # Heartbeat: closing a dead PR is a GitHub API call per task.
        progress_cb()
        if task.status != TaskStatus.FAILED:
            continue
        payload = task.first_payload if task.payloads else {}
        if payload.get("type") != "fix":
            continue
        pr_number = (task.context or {}).get("review_loop", {}).get(
            "pr_number"
        ) or payload.get("pr_number")
        if not pr_number:
            continue
        # Skip closing rollup PRs (branch=staging prefix). A
        # rollup PR represents many tasks' worth of merged work;
        # closing it discards the whole body. Better to leave the
        # rollup open for human follow-up than to throw away N
        # children's successfully-merged contributions for a
        # single fix task that exhausted retries. Match the FULL
        # set of per-project staging prefixes, not just research/.
        branch = payload.get("branch", "")
        _proj = _dead_pr_project_cache.get(task.project_id) if task.project_id else None
        if _proj is not None and _proj.is_staging_branch(branch):
            logger.warning(
                f"[REAPER] Skip closing rollup PR #{pr_number} "
                f"(branch={branch}, fix task {task.id}) — "
                f"closing would discard the rollup body"
            )
            continue
        # Infra-recovery guard: do NOT close PRs for fix tasks that
        # failed due to transient infrastructure (AGENT_TIMEOUT, VM
        # preemption, etc.) while the review-loop still has iteration
        # budget.  The code on the PR is valid — the agent may never
        # have run.  Leave the PR open so the eval-sweep reaped-owner
        # reconcile (recovery_evaluating.py) or the resurrection sweep
        # can re-drive the chain.  Only close for genuine merit/code
        # failures or when iteration budget is truly exhausted.
        # Sibling chokepoints that are failure-class-blind (rollup-wait
        # gate in rollup_management.py ~2031, _resolve_terminal_parent_
        # verdict in task_lifecycle_requeue.py, review cap handler) are
        # tracked as follow-up work — they require broader DB queries
        # and separate review to fix safely.
        from app.failure_classification import (
            is_recoverable_infra_terminal,
        )

        if is_recoverable_infra_terminal(task):
            logger.info(
                f"[PIPELINE] Preserving PR #{pr_number} for infra-reaped "
                f"fix task {task.id} — review-loop budget remains, "
                f"skipping close so eval-sweep can re-drive the chain"
            )
            continue
        try:
            from app.routers.agent import get_github_token_for_task
            from app.github import close_pull_request, create_pr_comment

            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            reason = task.latest_status_message or "fix task reached terminal failure"
            try:
                await create_pr_comment(
                    gh_token,
                    owner,
                    repo,
                    int(pr_number),
                    f":warning: Abandoning this PR — fix task `{str(task.id)[:8]}` reached terminal failure.\n\n"
                    f"{reason[:400]}\n\n"
                    f"Closing so sibling rollups aren't blocked waiting for this branch.",
                )
            except Exception as ce:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Failed to comment on dead PR #{pr_number}: {ce}"
                )
            closed = await close_pull_request(
                gh_token,
                owner,
                repo,
                int(pr_number),
                project_id=str(task.project_id),
            )
            if closed:
                logger.info(
                    f"[PIPELINE] Closed PR #{pr_number} after fix task {task.id} terminal-failed"
                )
            else:
                logger.warning(
                    f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Failed to close PR for failed fix task {task.id}: {e}",
                exc_info=True,
            )
