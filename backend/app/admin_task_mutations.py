"""Mutation helpers extracted from admin.py PATCH /tasks/{task_id} (modify_task).

The public entry point is ``modify_task_apply``; admin.py calls it after the
auth/project-access checks that must remain in the HTTP handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.broadcast import broadcast_to_project  # noqa: F401
from app.branch_resolution import (
    _signal_blocking_project_branch_configuration,
)
from app.landed_signal import (
    corroborate_and_apply_merged_landed_signal,
    mark_unblocked_non_merge,
)
from app.models import (
    Project,
    Task,
    TaskHistory,
    TaskStatus,
    _utcnow,
)
from app.payload_schemas import validate_internal_payload
from app.research_execution_authority import (
    is_server_owned_research_context_key,
)
from app.retry_prompt import materialize_operator_retry_guidance
from app.shared_state import get_project_integration_branch
from app.sidecar_authority import (
    is_authenticated_backend_sidecar_shape,
    is_server_owned_sidecar_context_key,
)

logger = logging.getLogger(__name__)

_APPROVAL_HELPERS = frozenset(
    {
        "_apply_approval_mutation",
        "_handle_rollup_pr_approval",
        "_maybe_spawn_rollup_smoketest",
        "_handle_task_pr_approval",
        "_merge_critical_pr",
        "_record_failure_result",
        "_spawn_review_for_approved_task",
    }
)


def __getattr__(name: str):
    """Lazily re-export the canonical approval helpers without an import cycle."""
    if name not in _APPROVAL_HELPERS:
        raise AttributeError(name)
    from app import admin_task_approvals

    return getattr(admin_task_approvals, name)


_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
_GENERIC_MEMBER_MUTATION_FIELDS = frozenset(
    {
        "payloads",
        "parent_task_id",
        "context",
        "depends_on",
        "post_commit_pending",
    }
)
CANCEL_CASCADE_CHILD_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)
CANCEL_WAITING_DEPENDENT_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.BLOCKED,
    TaskStatus.PROVISIONING,
    TaskStatus.PARKED,
)


def _mark_admin_cancelled(
    task: Task,
    *,
    cancelled_by_task_id: str | None = None,
) -> None:
    """Persist operator-cancellation provenance on a cancelled task.

    Parent terminalization and direction-health accounting operate on the task
    row, not ``TaskHistory``. Keep the originating operator identity durable on
    both the explicitly cancelled task and any tasks cancelled as a consequence
    so they cannot be misclassified as agent deaths after the request completes.
    """
    from app.failure_classification import ADMIN_CANCEL, persist_failure_category

    persist_failure_category(task, ADMIN_CANCEL)
    ctx = dict(task.context or {})
    ctx["failure_class"] = ADMIN_CANCEL
    ctx.pop("timeout_subtype", None)
    ctx.pop("external_api_subtype", None)
    ctx["cancelled_by"] = "USER"
    if cancelled_by_task_id is not None:
        ctx["cancelled_by_task_id"] = cancelled_by_task_id
    task.context = ctx


async def _refresh_direction_outcomes_for_task(task: Task) -> None:
    """Re-run the research-history merge recompute for *task* on a fresh session.

    The admin merge paths persist a task's landed signal on the request session;
    its research cycle's ``prs_merged`` must then be recomputed from the now-
    committed child rows. The recompute takes the project's directions FOR-NO-KEY-
    UPDATE lock and commits, so it runs on its OWN ``AsyncSessionLocal`` rather
    than the in-flight request session. ``refresh_direction_outcomes_after_merge``
    is a no-op unless the task carries ``direction_id`` + ``research_task_id`` and
    is best-effort (it rolls back its own session on error); this wrapper adds a
    fresh-session re-fetch and a top-level guard so it can never break the PATCH.
    """
    from app.database import AsyncSessionLocal
    from app.direction_outcomes import refresh_direction_outcomes_after_merge

    try:
        async with AsyncSessionLocal() as _dir_session:
            _dir_task = await _dir_session.get(Task, task.id)
            if _dir_task is not None:
                await refresh_direction_outcomes_after_merge(_dir_task, _dir_session)
    except Exception as _dir_exc:  # noqa: BLE001 — best-effort; must not break PATCH
        logger.warning(
            "[ADMIN] direction-outcome refresh failed for merged task %s: %r",
            getattr(task, "id", None),
            _dir_exc,
        )


def _promotion_target_merge_allowed(task: Task, target_branch: str) -> bool:
    """Allow the intentionally-gated hotfix lane to merge to promotion."""
    if not target_branch:
        return False
    payload = task.first_payload or {}
    ctx = task.context or {}
    return bool(payload.get("hotfix") or ctx.get("hotfix"))


async def _merge_context_with_hotfix_non_landing(
    task: Task,
    project: Project | None,
    session: AsyncSession,
    *,
    base_context: Dict[str, Any],
    landed_branch: str,
    record_pr_merged: bool,
    preserve_hotfix_non_landing: bool = True,
    merge_commit_sha: str | None = None,
) -> Dict[str, Any]:
    """Preserve merge-equivalent unblock semantics for hotfix promotion merges.

    Async because the landing write routes through the corroborating twin
    :func:`app.landed_signal.corroborate_and_apply_merged_landed_signal`, which
    stamps the backend's own acceptance-test corroboration BEFORE the
    code-acceptance verdict is recorded — so an operator-approved merge mints
    machine-proven credit for a declared test that exists in the merged tree but
    not in the PR's diff. ``session`` is used only for that corroboration read.

    ``merge_commit_sha`` is the sha the merge API just returned. Callers pass it
    so that corroboration read is pinned to the immutable merge commit instead
    of the branch-tip cache, whose 60s TTL can still hold the PRE-merge tip and
    would report a test the PR itself added as absent.
    """
    task.context = dict(base_context)

    if record_pr_merged:
        await corroborate_and_apply_merged_landed_signal(
            task,
            project,
            session,
            base_branch=landed_branch,
            hotfix_lane=_promotion_target_merge_allowed(task, landed_branch),
            non_merge_reason="merged_pr_non_integration_branch",
            merge_commit_sha=merge_commit_sha,
        )
        return dict(task.context or {})

    if (
        not preserve_hotfix_non_landing
        or project is None
        or not _promotion_target_merge_allowed(task, landed_branch)
    ):
        return dict(base_context)

    from app.shared_state import get_project_promotion_target

    promotion_target = get_project_promotion_target(project)
    integration_branch = get_project_integration_branch(project)
    if landed_branch == promotion_target and landed_branch != integration_branch:
        mark_unblocked_non_merge(
            task,
            reason="merged_pr_non_integration_branch",
        )
        return dict(task.context or {})

    return dict(base_context)


async def _block_admin_merge_for_branch_safety(
    task: Task,
    project: Project | None,
    session: AsyncSession,
    detail: str,
    *,
    issue_code: str,
) -> None:
    """Keep the task approval-gated and persist a branch-safety alert."""
    if project is not None:
        _signal_blocking_project_branch_configuration(project, issue_code, detail)
    task.requires_approval = True
    task.latest_status_message = detail[:500]
    session.add(task)
    await session.commit()


# ---------------------------------------------------------------------------
# Background cleanup
# ---------------------------------------------------------------------------


async def _terminal_cleanup_bg(
    parent_task_id: str,
    parent_vm_id: str | None,
    parent_acct_id: str | None,
    children_cleanup: List[tuple],
    force_kill: bool,
    parent_project_id: str | None = None,
) -> None:
    """Slow VM/account cleanup for admin-driven terminal transitions.

    Scheduled via BackgroundTasks so the request handler returns immediately
    after status writes commit, avoiding DB connection-pool starvation.
    """
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.max_plan_pool import release_max_plan_account

    if parent_vm_id:
        if not force_kill:
            await asyncio.sleep(5)
        try:
            await _ensure_vm_deleted(parent_vm_id, reason="admin_terminal")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ADMIN] BG cleanup: VM delete failed for parent={parent_task_id} vm={parent_vm_id}: {exc}"
            )
    if parent_acct_id:
        try:
            await release_max_plan_account(
                parent_acct_id,
                project_id=parent_project_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ADMIN] BG cleanup: account release failed for parent={parent_task_id} acct={parent_acct_id}: {exc}"
            )

    for child_task_id, child_vm_id, child_acct_id, child_project_id in children_cleanup:
        if child_vm_id:
            try:
                await _ensure_vm_deleted(child_vm_id, reason="parent_cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[ADMIN] BG cleanup: VM delete failed for child={child_task_id} vm={child_vm_id}: {exc}"
                )
        if child_acct_id:
            try:
                await release_max_plan_account(
                    child_acct_id,
                    project_id=child_project_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[ADMIN] BG cleanup: account release failed for child={child_task_id} acct={child_acct_id}: {exc}"
                )


# ---------------------------------------------------------------------------
# Phase 1: status transition
# ---------------------------------------------------------------------------


async def _apply_status_mutation(
    task: Task,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    force_kill: bool,
    changes: Dict[str, Any],
) -> None:
    """Handle status-field mutations including cascade cancellation and requeue."""
    if "status" not in payload:
        if "requeue_prompt_suffix" in payload:
            raise HTTPException(
                status_code=400,
                detail="requeue_prompt_suffix is only valid with status=QUEUED",
            )
        return

    try:
        new_status = TaskStatus(payload["status"])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status") from None

    admin_allowed = {TaskStatus.QUEUED, TaskStatus.CANCELLED}
    task_type = str(task.first_payload.get("type") or "").lower()
    if new_status == TaskStatus.QUEUED:
        from app.review_execution_ownership import has_pending_pr_continuation

        if has_pending_pr_continuation(task):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot requeue {task_type} task while its post-commit "
                    "continuation is pending; wait for recovery or cancel the "
                    "task first"
                ),
            )
    raw_requeue_guidance = payload.get("requeue_prompt_suffix")
    if (
        new_status == TaskStatus.QUEUED
        and isinstance(payload.get("context"), dict)
        and "_original_prompt_suffix" in payload["context"]
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "_original_prompt_suffix is internal retry-dedup metadata and is not "
                "delivered to agents; use requeue_prompt_suffix instead"
            ),
        )

    requeue_guidance: str | None = None
    if "requeue_prompt_suffix" in payload:
        if new_status != TaskStatus.QUEUED:
            raise HTTPException(
                status_code=400,
                detail="requeue_prompt_suffix is only valid with status=QUEUED",
            )
        if (
            not isinstance(raw_requeue_guidance, str)
            or not raw_requeue_guidance.strip()
        ):
            raise HTTPException(
                status_code=400,
                detail="requeue_prompt_suffix must be a non-empty string",
            )
        if not task.payloads or not isinstance(task.payloads[0], dict):
            raise HTTPException(
                status_code=400,
                detail="Cannot attach retry guidance: task has no agent payload",
            )
        requeue_guidance = raw_requeue_guidance.strip()
    if new_status not in admin_allowed and task.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from terminal state {task.status} to {new_status} (only requeue or cancel is allowed)",
        )

    prior_status = task.status
    prelocked_requeue_ancestors: list[Task] | None = None
    prelocked_requeue_parent_id = task.parent_task_id
    if new_status == TaskStatus.QUEUED and task_type in {"review", "fix"}:
        from app.fix_admission import prepare_direct_fix_admission
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task
        from app.review_admission import prepare_direct_review_admission
        from app.review_execution_ownership import has_pending_pr_continuation
        from app.rollup_lineage_fence import lock_ancestor_rollups_for_requeue
        from app.task_locking import acquire_pr_spawn_lock

        pr_number = (task.first_payload or {}).get("pr_number")
        if pr_number is None:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot requeue {task_type} without PR",
            )
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)

        async def _fetch_requeued_pr() -> dict:
            return await get_pull_request(
                gh_token,
                owner,
                repo,
                int(pr_number),
                project_id=str(task.project_id),
            )

        # Preserve the repository PR admission order, but take hierarchy locks
        # from ancestor -> child.  Parent cancellation owns that same hierarchy
        # direction; allowing prepare_direct_*_admission to lock this child (or
        # a same-PR sibling) before its ancestor creates a child<->ancestor
        # deadlock.  The admission helper's repeated advisory acquire below is
        # transaction-reentrant.
        await acquire_pr_spawn_lock(session, task.project_id, int(pr_number))
        prelocked_requeue_ancestors = await lock_ancestor_rollups_for_requeue(
            task, session
        )

        if task_type == "fix":
            admission = await prepare_direct_fix_admission(
                session,
                project_id=task.project_id,
                pr_number=int(pr_number),
                fix_task_id=task.id,
                fetch_live_pr=_fetch_requeued_pr,
            )
        else:
            admission = await prepare_direct_review_admission(
                session,
                project_id=task.project_id,
                pr_number=int(pr_number),
                fetch_live_pr=_fetch_requeued_pr,
                review_task_id=task.id,
            )
        if not admission.allowed:
            await session.rollback()
            raise HTTPException(status_code=409, detail=admission.reason)

        # Admission owns the PR lock.  Re-read the exact row under a definitive
        # Task lock after the live lookup, discarding the route's stale identity-
        # map snapshot.  This closes completion -> pending racing the initial
        # guard without ever taking Task before PR.
        with session.no_autoflush:
            locked_task = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.id == task.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
        if locked_task is None:
            await session.rollback()
            raise HTTPException(status_code=404, detail="Task not found")
        task = locked_task
        prior_status = task.status
        if task.parent_task_id != prelocked_requeue_parent_id:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Task ancestry changed during review/fix retry admission; "
                    "retry the operation"
                ),
            )
        if has_pending_pr_continuation(task):
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot requeue {task_type} task while its post-commit "
                    "continuation is pending; wait for recovery or cancel the task first"
                ),
            )

        requeue_loop = dict((task.context or {}).get("review_loop") or {})
        if task_type == "fix":
            requeue_loop["fix_target"] = dict(admission.fix_target or {})
        else:
            requeue_loop["review_target"] = dict(admission.review_target or {})
        task.context = {**(task.context or {}), "review_loop": requeue_loop}
        flag_modified(task, "context")
        task.archived = False
        changes["archived"] = False

    if (
        new_status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
        and task.status == TaskStatus.COMPLETED
        and task.post_commit_pending
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot terminalize a COMPLETED task while its post-commit "
                "continuation is pending; wait for completion recovery"
            ),
        )

    if requeue_guidance is not None:
        updated_payloads = [dict(item) for item in task.payloads]
        materialize_operator_retry_guidance(updated_payloads[0], requeue_guidance)
        task.payloads = [
            validate_internal_payload(item, "admin:manual_requeue_guidance")
            for item in updated_payloads
        ]
        changes["requeue_prompt_suffix"] = "materialized"
    if (
        new_status == TaskStatus.QUEUED
        and prior_status in _TERMINAL_STATUSES
        and task_type in ("research", "plan")
    ):
        # A terminal rollup's PR identity and CI/review/verifier latches belong
        # to the attempt that just ended. Reconcile them BEFORE changing status:
        # the helper is terminal-only so active review progress cannot be reset.
        from app.terminal_rollup_requeue import reconcile_terminal_rollup_requeue

        try:
            reconciliation = await reconcile_terminal_rollup_requeue(
                task, session, payload
            )
        except HTTPException:
            raise
        except Exception as exc:
            # A requeue that cannot proceed must say WHY, never return a bare
            # "Internal Server Error". Log the full traceback and surface an
            # actionable 500 naming the failure; roll back so the task is not
            # left half-transitioned by a partial reconciliation.
            # Capture identity BEFORE rollback: a rollback expires every ORM
            # instance, so a later ``task.id`` read would itself raise
            # MissingGreenlet in the async context and mask the real cause.
            _task_id = str(task.id)
            logger.exception(
                "[ADMIN] Terminal rollup requeue reconciliation failed for task %s",
                _task_id,
            )
            await session.rollback()
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Requeue failed while reconciling the prior rollup attempt "
                    f"for task {_task_id}: {type(exc).__name__}: {str(exc)[:300]}"
                ),
            ) from exc
        changes["rollup_requeue_reconciliation"] = reconciliation
        if reconciliation == "merged_pr_rearmed":
            # The operator is requeueing a false terminal state after the
            # tracked rollup already landed. Run the authoritative rollup
            # classifier after commit instead of waiting for a sweep or ever
            # entering fresh-PR creation.
            changes["_check_merged_rollup_parent_now"] = True

    if (
        new_status == TaskStatus.QUEUED
        and prior_status in _TERMINAL_STATUSES
        and task.parent_task_id is not None
    ):
        from app.rollup_lineage_fence import invalidate_ancestor_rollups_for_requeue

        invalidated_ancestors = await invalidate_ancestor_rollups_for_requeue(
            task,
            session,
            prelocked_ancestors=prelocked_requeue_ancestors,
        )
        if invalidated_ancestors:
            changes["invalidated_ancestor_rollups"] = invalidated_ancestors

    task.status = new_status
    changes["status"] = new_status.value

    if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
        task.requires_approval = False

    if new_status == TaskStatus.CANCELLED:
        from app.task_lifecycle import close_orphan_pr_if_any

        _mark_admin_cancelled(task)
        await close_orphan_pr_if_any(task, session, reason="admin_status_change")

    parent_vm_id_for_bg: str | None = None
    parent_acct_id_for_bg: str | None = None
    if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED) and task.vm_instance_id:
        parent_vm_id_for_bg = task.vm_instance_id
        parent_acct_id_for_bg = (task.context or {}).get("max_plan_account_id")

    if (
        new_status == TaskStatus.CANCELLED
        and task.context
        and task.context.get("finding_hash")
    ):
        task_priority = task.priority if task.priority is not None else 50
        if task_priority == 0:
            suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
            task.context = {**task.context, "suppress_until": suppress_until}
            flag_modified(task, "context")
            logger.info(
                f"[ADMIN] Set suppress_until={suppress_until} on cancelled low-priority task {task.id}"
            )

    children_cleanup_for_bg: List[tuple] = []
    cascade_targets: Dict[uuid.UUID, Task] = {}
    if new_status == TaskStatus.CANCELLED:
        cancellation_source_id = str(task.id)
        active_children = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == task.id,
                        Task.project_id == task.project_id,
                        Task.status.in_(CANCEL_CASCADE_CHILD_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

        # A manually cancelled prerequisite must also terminalize dependents that
        # are still waiting to run. Filter the project-scoped candidate set in
        # Python because ``depends_on`` is JSONB in production and JSON in the
        # SQLite test seam.
        dependent_candidates = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == task.project_id,
                        Task.status.in_(CANCEL_WAITING_DEPENDENT_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        waiting_dependents = [
            candidate
            for candidate in dependent_candidates
            if cancellation_source_id
            in {str(dep_id) for dep_id in (candidate.depends_on or [])}
        ]

        cascade_targets = {child.id: child for child in active_children}
        cascade_targets.update(
            {dependent.id: dependent for dependent in waiting_dependents}
        )
        waiting_dependent_ids = {dependent.id for dependent in waiting_dependents}
        for child in cascade_targets.values():
            child.status = TaskStatus.CANCELLED
            if child.vm_provisioned_at:
                child.vm_stopped_at = _utcnow()
            child.requires_approval = False
            _mark_admin_cancelled(
                child,
                cancelled_by_task_id=cancellation_source_id,
            )
            if child.id in waiting_dependent_ids:
                child.latest_status_message = (
                    "Cancelled: dependency task administratively cancelled"
                )
            elif not child.latest_status_message:
                child.latest_status_message = (
                    "Cancelled: parent task administratively cancelled"
                )
            children_cleanup_for_bg.append(
                (
                    str(child.id),
                    child.vm_instance_id,
                    (child.context or {}).get("max_plan_account_id"),
                    str(child.project_id) if child.project_id else None,
                )
            )
            session.add(child)
        if cascade_targets:
            from app.task_lifecycle import close_orphan_pr_if_any

            for child in cascade_targets.values():
                await close_orphan_pr_if_any(
                    child, session, reason="admin_status_change_cascade"
                )
            logger.info(
                "[ADMIN] Cascaded ADMIN_CANCEL to %s child/dependent task(s) of task %s",
                len(cascade_targets),
                task.id,
            )

    if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
        from app import strategic_cycle

        # Snapshot the completion events while the rows are still fresh; they
        # are emitted post-commit by _commit_and_broadcast. Without the emit a
        # durably terminal cycle leaves the connected UI showing "Strategist
        # working…" forever (useLogViewer clears it only on
        # strategic_cycle_completed or a project switch).
        strategic_completion_events: List[Dict[str, Any]] = []
        reconciled = await strategic_cycle.reconcile_terminal_task(
            session, task, source="admin_task_mutation"
        )
        if reconciled is not None:
            strategic_completion_events.append(
                strategic_cycle.terminal_completion_event(reconciled, task)
            )
        for child in cascade_targets.values():
            reconciled_child = await strategic_cycle.reconcile_terminal_task(
                session, child, source="admin_task_mutation_cascade"
            )
            if reconciled_child is not None:
                strategic_completion_events.append(
                    strategic_cycle.terminal_completion_event(
                        reconciled_child, child
                    )
                )
        if strategic_completion_events:
            # Internal-only marker: popped in _commit_and_broadcast before the
            # TaskHistory row is built, then emitted after its commit.
            changes["_strategic_completion_events"] = strategic_completion_events

    if parent_vm_id_for_bg or parent_acct_id_for_bg or children_cleanup_for_bg:
        background_tasks.add_task(
            _terminal_cleanup_bg,
            parent_task_id=str(task.id),
            parent_vm_id=parent_vm_id_for_bg,
            parent_acct_id=parent_acct_id_for_bg,
            children_cleanup=children_cleanup_for_bg,
            force_kill=force_kill,
            parent_project_id=str(task.project_id) if task.project_id else None,
        )

    if new_status == TaskStatus.QUEUED:
        # Manual requeue must reopen a CLEAN attempt: drop any durable blocker
        # metadata and the BLOCKED_PRECONDITION failure tags from the prior run,
        # mirroring the automatic out-transition (unblock_to_queued). Otherwise a
        # task moved from BLOCKED/failed-retry back to QUEUED would still look and
        # classify as blocked work (stale context['blocker'], budget-exempt
        # failure_class) on its next dispatch. Safe no-op on never-blocked tasks.
        from app.task_blocked import reset_task_for_fresh_attempt

        is_parent_type = task_type in ("research", "plan")
        from app.task_lifecycle import _requeue_parent_task

        children = []
        if is_parent_type:
            children = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.parent_task_id == task.id,
                            Task.project_id == task.project_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        if children:
            try:
                retried = await _requeue_parent_task(
                    task,
                    session,
                    operator_retry_guidance=requeue_guidance,
                )
            except HTTPException:
                raise
            except Exception as exc:
                # The requeue path clones failed children, remaps depends_on, and
                # resolves the parent's terminal verdict — any of which can throw
                # on an unexpected child-state (e.g. a parent whose children are
                # all COMPLETED/merged). Surface the real reason with its
                # traceback instead of a bare, detail-less 500, and roll back so
                # the parent is not left half-transitioned (e.g. status flipped
                # to QUEUED but children never cloned).
                # Capture identity BEFORE rollback: a rollback expires every ORM
                # instance, so a later ``task.id`` read would itself raise
                # MissingGreenlet in the async context and mask the real cause.
                _task_id = str(task.id)
                logger.exception(
                    "[ADMIN] Requeue of parent task %s failed", _task_id
                )
                await session.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Requeue failed for parent task {_task_id}: "
                        f"{type(exc).__name__}: {str(exc)[:300]}"
                    ),
                ) from exc
            changes["status"] = TaskStatus.EVALUATING
            changes["retried_children"] = retried
        else:
            old_vm_id = task.vm_instance_id
            reset_task_for_fresh_attempt(task)
            task.attempt_count = 0
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            changes["attempt_count"] = 0
            changes["vm_instance_id"] = task.vm_instance_id
            changes["_old_vm_to_delete"] = old_vm_id


# ---------------------------------------------------------------------------
# Phase 2: approval mutation lives in app.admin_task_approvals and is lazily
# re-exported above.
#
# ---------------------------------------------------------------------------
# Phase 3: simple field mutations
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


async def _apply_simple_mutations(
    task: Task,
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Apply payloads, parent_task_id, context, and post_commit_pending mutations."""
    if "payloads" in payload:
        proposed_payloads = payload["payloads"]
        proposed_first = (
            proposed_payloads[0]
            if isinstance(proposed_payloads, list)
            and proposed_payloads
            and isinstance(proposed_payloads[0], dict)
            else {}
        )
        current_first = task.first_payload or {}
        immutable_identity_keys = (
            "type",
            "pr_number",
            "branch",
            "base_branch",
            "original_task_id",
        )
        changed_identity = [
            key
            for key in immutable_identity_keys
            if proposed_first.get(key) != current_first.get(key)
        ]
        if changed_identity:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Generic payload mutation cannot retarget task authority "
                    "identity: " + ", ".join(changed_identity)
                ),
            )
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        proposed_parent_id = payload["parent_task_id"]
        if proposed_parent_id:
            try:
                parsed_parent_id = uuid.UUID(str(proposed_parent_id))
            except (TypeError, ValueError, AttributeError):
                raise HTTPException(
                    status_code=400, detail="Invalid parent_task_id UUID"
                ) from None
            proposed_parent = (
                await session.execute(
                    select(Task).where(
                        Task.id == parsed_parent_id,
                        Task.project_id == task.project_id,
                    )
                )
            ).scalar_one_or_none()
            if proposed_parent is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "parent_task_id must reference an existing task in the "
                        "same project"
                    ),
                )

            # Parentage is a directed tree edge.  Walk the proposed parent's
            # persisted, same-project ancestry before assigning it so a member
            # PATCH cannot make a task its own parent, make an ancestor a child
            # of its descendant, or extend an already-corrupt/foreign chain.
            ancestor = proposed_parent
            seen_parent_ids: set[uuid.UUID] = set()
            while ancestor is not None:
                if ancestor.id == task.id:
                    raise HTTPException(
                        status_code=400,
                        detail="Circular parent_task_id relationship detected",
                    )
                if ancestor.id in seen_parent_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="Proposed parent has cyclic ancestry",
                    )
                seen_parent_ids.add(ancestor.id)
                if ancestor.parent_task_id is None:
                    break
                ancestor = (
                    await session.execute(
                        select(Task).where(
                            Task.id == ancestor.parent_task_id,
                            Task.project_id == task.project_id,
                        )
                    )
                ).scalar_one_or_none()
                if ancestor is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Proposed parent ancestry contains a missing or "
                            "cross-project task"
                        ),
                    )
            task.parent_task_id = parsed_parent_id
        else:
            task.parent_task_id = None
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
        protected_context_keys = {
            "review_loop",
            "pr_merged",
            "pr_closed_unmerged",
            "pr_rejected",
        }
        attempted_protected = sorted(
            key
            for key in payload["context"]
            if key in protected_context_keys
            or is_server_owned_sidecar_context_key(key)
            or is_server_owned_research_context_key(key)
        )
        if attempted_protected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Generic task context mutation cannot write backend-authority "
                    "state: " + ", ".join(attempted_protected)
                ),
            )
        merged = {**(task.context or {}), **payload["context"]}
        if len(json.dumps(merged)) > 102400:
            raise HTTPException(
                status_code=400, detail="Merged context exceeds 100KB limit"
            )
        task.context = merged
        changes["context"] = "updated"

    if (
        "post_commit_pending" in payload
        and payload["post_commit_pending"] is False
        and task.post_commit_pending
    ):
        task.post_commit_pending = False
        task.post_commit_failed = True
        new_ctx = dict(task.context or {})
        stripped_meta = new_ctx.pop("_post_commit_pending_meta", None)
        new_ctx["_post_commit_abandoned"] = {
            "at": _utcnow().isoformat(),
            "by": "ADMIN_PATCH",
            "prior_attempts": (stripped_meta or {}).get("attempts"),
            "prior_reason": (stripped_meta or {}).get("reason"),
        }
        task.context = new_ctx
        flag_modified(task, "context")
        changes["post_commit_pending"] = False
        logger.info(
            f"[ADMIN] Cleared post_commit_pending on task {task.id} via PATCH "
            f"(prior_attempts={(stripped_meta or {}).get('attempts')})"
        )


# ---------------------------------------------------------------------------
# Phase 4: dependency graph mutation
# ---------------------------------------------------------------------------


async def _apply_depends_on_mutation(
    task: Task,
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Validate and apply depends_on with cycle detection."""
    if "depends_on" not in payload:
        return

    new_deps = payload["depends_on"] if isinstance(payload["depends_on"], list) else []
    if new_deps:
        try:
            dep_uuids = [uuid.UUID(d) for d in new_deps]
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400, detail="Invalid UUID in depends_on"
            ) from None

        existing_tasks = (
            (
                await session.execute(
                    select(Task).where(
                        Task.id.in_(dep_uuids),
                        Task.project_id == task.project_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(existing_tasks) != len(dep_uuids):
            raise HTTPException(
                status_code=400,
                detail=(
                    "One or more depends_on task IDs do not exist in the same "
                    "project"
                ),
            )
        dep_tasks_by_id = {str(t.id): t for t in existing_tasks}

        task_id_str = str(task.id)
        task_cache: dict[str, Task | None] = {}

        async def _has_path_to(
            start_id: str, target_id: str, visited: set, depth: int = 0
        ) -> bool:
            if depth > 50:
                return False
            if start_id == target_id:
                return True
            if start_id in visited:
                return False
            visited.add(start_id)
            if start_id not in task_cache:
                task_cache[start_id] = (
                    await session.execute(
                        select(Task).where(
                            Task.id == uuid.UUID(start_id),
                            Task.project_id == task.project_id,
                        )
                    )
                ).scalar_one_or_none()
            dep_task = task_cache[start_id]
            if not dep_task:
                return False
            for d in dep_task.depends_on or []:
                if await _has_path_to(d, target_id, visited, depth + 1):
                    return True
            return False

        for dep_id in new_deps:
            if await _has_path_to(dep_id, task_id_str, set()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Circular dependency detected: {dep_id} eventually depends on {task_id_str}",
                )

        # Reject cross-base dependency edges. The dispatch loop only *waits* for
        # a dependency whose merge lands on the dependent's base branch
        # (_same_base_branch); a cross-base edge is silently skipped — the
        # dependent dispatches without ever inheriting the dependency's landed
        # code. Surface that as a creation-time error rather than letting the
        # orchestrator drop the wait. Uses the orchestrator's own predicate so
        # this gate rejects exactly the edges the dispatch loop would skip, with
        # no false positive on the common None/None default-base case.
        from app.orchestrator_health import _same_base_branch

        for dep_id in new_deps:
            dep_task = dep_tasks_by_id[dep_id]
            if not _same_base_branch(task, dep_task):
                task_base = (task.first_payload or {}).get("base_branch") or "(default)"
                dep_base = (dep_task.first_payload or {}).get(
                    "base_branch"
                ) or "(default)"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cross-base dependency rejected: this task's base branch "
                        f"'{task_base}' differs from dependency {dep_id}'s base "
                        f"'{dep_base}'. A dependency only propagates its landed code "
                        f"when both share a base branch; otherwise the dependent "
                        f"would dispatch without the prerequisite's changes. Align "
                        f"the base branches or re-point the dependency."
                    ),
                )

    task.depends_on = new_deps
    changes["depends_on"] = task.depends_on


# ---------------------------------------------------------------------------
# Phase 5: commit and broadcast
# ---------------------------------------------------------------------------


async def _commit_and_broadcast(
    task: Task,
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Save changes, append history, delete old VM, and trigger rollup checks."""
    if not changes:
        return

    # Internal-only marker set by _merge_critical_pr: pop before history so it
    # isn't serialized into TaskHistory.details, then run the direction-outcome
    # refresh AFTER the commit below (it must see the committed child rows).
    _refresh_direction_after_merge = bool(
        changes.pop("_refresh_direction_after_merge", False)
    )
    _check_merged_rollup_parent_now = bool(
        changes.pop("_check_merged_rollup_parent_now", False)
    )
    # Internal-only marker set by _apply_status_mutation for every strategic
    # cycle its terminal transition reconciled. Popped before history, then
    # broadcast after the commit below (post-commit emit convention).
    _strategic_completion_events = changes.pop("_strategic_completion_events", None)

    session.add(task)
    history = TaskHistory(
        task_id=task.id,
        action="MODIFIED",
        details={"changes": changes, "modified_by": "USER"},
    )
    session.add(history)
    await session.commit()
    await session.refresh(task)

    if _strategic_completion_events:
        from app import strategic_cycle

        await strategic_cycle.emit_terminal_completion_events(
            _strategic_completion_events
        )

    if _refresh_direction_after_merge:
        await _refresh_direction_outcomes_for_task(task)

    old_vm = changes.pop("_old_vm_to_delete", None)
    if old_vm:
        from app.vm_lifecycle import _ensure_vm_deleted

        await _ensure_vm_deleted(old_vm, reason="admin_requeue")
        # VM teardown may persist forensics through an independent session.  Do
        # not rely on ORM attributes remaining loaded across that cross-session
        # side effect: async lazy-loading below would raise MissingGreenlet.
        await session.refresh(task)

    from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches

    should_check_rollup = False
    if "status" in payload and task.parent_task_id:
        new_status = TaskStatus(payload["status"])
        if new_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            should_check_rollup = True
        if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
            ctx = task.context or {}
            if ctx.get("plan_branch") or ctx.get("research_branch"):
                await _cleanup_branches(task, session)
    if (
        "context" in payload
        and task.parent_task_id
        and isinstance(payload["context"], dict)
        and payload["context"].get("pr_merged")
    ):
        should_check_rollup = True
    if _check_merged_rollup_parent_now:
        # _check_and_trigger_rollup is intentionally child-driven: passing the
        # parent is a no-op because parent_task_id is absent. Re-enter through a
        # real completed child so the normal parent lock, merged-PR landing
        # classifier, branch cleanup, and terminal broadcast all run now.
        rollup_child = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.parent_task_id == task.id,
                        Task.project_id == task.project_id,
                        Task.status == TaskStatus.COMPLETED,
                    )
                    .order_by(Task.updated_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if rollup_child is not None:
            # This re-drive runs AFTER the status transition has already been
            # durably committed above, so the parent is already parked at
            # EVALUATING (or its reconciled state). It is a best-effort
            # convergence nudge: the reaper's stale-EVALUATING sweep re-drives
            # rollup on its own cadence. An unhandled exception here (e.g. a
            # GitHub round-trip failure or a MissingGreenlet on the poisoned
            # session's recovery path) must NOT turn the already-successful
            # operator requeue into a bare, detail-less 500. Log it with the
            # traceback and let the committed transition stand; roll back any
            # partial, uncommitted rollup mutations so the session is clean.
            try:
                await _check_and_trigger_rollup(rollup_child, session)
            except Exception:
                logger.exception(
                    "[ADMIN] Post-requeue rollup re-drive failed for merged "
                    "rollup parent %s; the parent transition already committed, "
                    "so it stands and the reaper's stale-EVALUATING sweep will "
                    "re-drive rollup convergence",
                    task.id,
                )
                # A rollback expires every ORM instance; re-load the task so the
                # response-shaping attribute reads below do not trip a
                # MissingGreenlet on an expired instance in the async context.
                await session.rollback()
                await session.refresh(task)
        else:
            logger.warning(
                "[ADMIN] Already-merged rollup parent %s has no completed child "
                "to drive authoritative convergence; leaving it for recovery",
                task.id,
            )
    elif should_check_rollup:
        await _check_and_trigger_rollup(task, session)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def modify_task_apply(
    task: Task,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    force_kill: bool = False,
) -> Dict[str, Any]:
    """Apply all mutations from a PATCH /tasks/{task_id} payload.

    Called by admin.py after auth/project-access checks.  Returns the
    same response shape as the original modify_task endpoint.
    """
    changes: Dict[str, Any] = {}

    generic_mutation_fields = sorted(
        _GENERIC_MEMBER_MUTATION_FIELDS.intersection(payload)
    )
    task_locked_for_generic_mutation = False
    if generic_mutation_fields:
        # The route-level authorization lookup intentionally does not lock the
        # task. Generic JSON/topology writes therefore must discard that stale
        # identity-map snapshot before merging or validating. The project row
        # serializes every same-project member graph edit, so opposing parent or
        # dependency edges cannot both validate against an obsolete graph.
        from app.task_locking import lock_project_then_task

        task_project_id = task.project_id
        task_id = task.id
        session.expire(task)
        async with lock_project_then_task(
            session, task_project_id, task_id, skip_locked=False
        ) as (locked_project, locked_task):
            if (
                locked_project is None
                or locked_task is None
                or locked_task.project_id != locked_project.id
            ):
                raise HTTPException(status_code=404, detail="Task not found")
            task = locked_task
            task_locked_for_generic_mutation = True

    # A backend sidecar is an independent grader only while its frozen prompt,
    # delivery topology, and complete context remain backend-authored. Blocking
    # only the obvious source/verdict keys is insufficient: a project member
    # could otherwise bias the grader via an innocuous-looking context field,
    # rewrite its prompt while retaining type/base identity, or parent it so the
    # resolver rejects the returned verdict. Status-only cancel/requeue remains
    # available; requeue must reuse the frozen prompt without an injected suffix.
    if is_authenticated_backend_sidecar_shape(task):
        protected_sidecar_fields = {
            "payloads",
            "parent_task_id",
            "context",
            "requeue_prompt_suffix",
        }
        attempted_sidecar_mutations = sorted(
            protected_sidecar_fields.intersection(payload)
        )
        if attempted_sidecar_mutations:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Generic task mutation cannot rewrite a backend sidecar: "
                    + ", ".join(attempted_sidecar_mutations)
                ),
            )

    if generic_mutation_fields and "status" in payload:
        if (
            payload.get("status") == TaskStatus.QUEUED.value
            and isinstance(payload.get("context"), dict)
            and "_original_prompt_suffix" in payload["context"]
        ):
            # Preserve the established, actionable validation for this common
            # malformed retry request even though mixed writes are separated.
            raise HTTPException(
                status_code=400,
                detail=(
                    "_original_prompt_suffix is internal retry-dedup metadata "
                    "and is not delivered to agents; use requeue_prompt_suffix "
                    "instead"
                ),
            )
        # Status transitions can commit internally (research/plan parent
        # requeue and approval flows), and review/fix requeue deliberately owns
        # PR -> Task ordering. Mixing one with a generic write would therefore
        # either release this graph lock before validation or invert a lock
        # contract. Status-only behavior is unchanged; submit the generic edit
        # as a second PATCH after a successful transition.
        raise HTTPException(
            status_code=400,
            detail=(
                "Apply status separately from generic payload, context, or "
                "topology mutation"
            ),
        )

    if generic_mutation_fields and "requires_approval" in payload:
        # Approval can perform external merge work and commit before returning.
        # Do not let such a commit release the graph lock before the generic
        # mutation validates and persists.
        raise HTTPException(
            status_code=400,
            detail=(
                "Apply requires_approval separately from generic payload, "
                "context, or topology mutation"
            ),
        )

    terminal_statuses = {
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }
    requested_status = payload.get("status")
    route_task_type = str(task.first_payload.get("type") or "").lower()
    status_requires_project_task_lock = requested_status in terminal_statuses or (
        requested_status == TaskStatus.QUEUED.value
        and route_task_type not in {"review", "fix"}
    )
    if (
        status_requires_project_task_lock
        and not task_locked_for_generic_mutation
    ):
        # Continuations and administrative status writers share the canonical
        # Project -> Task order.  A QUEUED retry is also a destructive reset of
        # attempt/runtime state, so it must re-read under the same pair: a route
        # snapshot loaded before an atomic research child publication must not
        # overwrite the newly-EVALUATING parent or reset it as a childless leaf.
        #
        # Review/fix retries are deliberately excluded. Their admission flow
        # owns the PR -> Task order and performs its own definitive Task re-read
        # after acquiring the PR lock; pre-locking Project/Task here would invert
        # that established contract.
        from app.task_locking import lock_project_then_task

        task_project_id = task.project_id
        task_id = task.id
        session.expire(task)
        async with lock_project_then_task(
            session, task_project_id, task_id, skip_locked=False
        ) as (_locked_project, locked_task):
            if locked_task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            task = locked_task
            await _apply_status_mutation(
                task, payload, background_tasks, session, force_kill, changes
            )
    else:
        await _apply_status_mutation(
            task, payload, background_tasks, session, force_kill, changes
        )
    await __getattr__("_apply_approval_mutation")(task, payload, session, changes)
    await _apply_simple_mutations(task, payload, session, changes)
    await _apply_depends_on_mutation(task, payload, session, changes)
    await _commit_and_broadcast(task, payload, session, changes)

    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "status": task.status,
        "vm_instance_id": task.vm_instance_id,
        "attempt_count": task.attempt_count,
        "requires_approval": task.requires_approval,
    }
