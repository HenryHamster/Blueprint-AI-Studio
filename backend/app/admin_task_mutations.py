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

from app.broadcast import broadcast_to_project
from app.branch_resolution import (
    InvalidProjectBranchConfiguration,
    _signal_blocking_project_branch_configuration,
    classify_rollup_merge_outcome,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import apply_merged_landed_signal, mark_unblocked_non_merge
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
    _utcnow,
)
from app.payload_schemas import validate_internal_payload
from app.retry_prompt import materialize_operator_retry_guidance
from app.shared_state import (
    get_project_integration_branch,
    get_project_task_branch_namespace,
    resolve_task_branch as _resolve_task_branch,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
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


def _merge_context_with_hotfix_non_landing(
    task: Task,
    project: Project | None,
    *,
    base_context: Dict[str, Any],
    landed_branch: str,
    record_pr_merged: bool,
    preserve_hotfix_non_landing: bool = True,
) -> Dict[str, Any]:
    """Preserve merge-equivalent unblock semantics for hotfix promotion merges."""
    task.context = dict(base_context)

    if record_pr_merged:
        apply_merged_landed_signal(
            task,
            project,
            base_branch=landed_branch,
            hotfix_lane=_promotion_target_merge_allowed(task, landed_branch),
            non_merge_reason="merged_pr_non_integration_branch",
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
        updated_payloads = [dict(item) for item in task.payloads]
        materialize_operator_retry_guidance(updated_payloads[0], requeue_guidance)
        task.payloads = [
            validate_internal_payload(item, "admin:manual_requeue_guidance")
            for item in updated_payloads
        ]
        changes["requeue_prompt_suffix"] = "materialized"

    if new_status not in admin_allowed and task.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from terminal state {task.status} to {new_status} (only requeue or cancel is allowed)",
        )

    prior_status = task.status
    task_type = (task.first_payload).get("type", "")
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
    if new_status == TaskStatus.CANCELLED:
        cancellation_source_id = str(task.id)
        active_children = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == task.id,
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
                        select(Task).where(Task.parent_task_id == task.id)
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
# Phase 2: approval mutation
# ---------------------------------------------------------------------------


async def _apply_approval_mutation(
    task: Task,
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Handle requires_approval toggle and downstream approval flows."""
    if "requires_approval" not in payload:
        return

    was_blocked = task.requires_approval
    task.requires_approval = bool(payload["requires_approval"])
    changes["requires_approval"] = task.requires_approval

    if was_blocked and not task.requires_approval:
        logger.info(
            f"[ADMIN] Task {task.id} manually approved (was blocked: {task.latest_status_message or 'unknown reason'})"
        )
        # Allow compute_experiment launch to proceed: check_compute_spend_approval
        # gates on spend_approval_cleared_at being non-None. Set it here so that
        # clearing requires_approval via PATCH actually unblocks job launch.
        if task.spend_approval_cleared_at is None:
            task.spend_approval_cleared_at = _utcnow()

    if not (
        was_blocked
        and not task.requires_approval
        and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
    ):
        return

    ctx = task.context or {}
    rollup_pr_number = ctx.get("rollup_pr_number")

    if rollup_pr_number:
        await _handle_rollup_pr_approval(task, ctx, rollup_pr_number, session, changes)
    else:
        await _handle_task_pr_approval(task, ctx, payload, session, changes)


async def _handle_rollup_pr_approval(
    task: Task,
    ctx: Dict[str, Any],
    rollup_pr_number: int,
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Merge the rollup PR and optionally spawn a smoketest on human approval."""
    _project = await session.get(Project, task.project_id)
    try:
        base_branch = (task.first_payload).get(
            "base_branch"
        ) or get_project_integration_branch(_project)
    # Defense-in-depth: the resolver currently fails SAFE and does not raise.
    # This guard exists so a future re-introduction of a raising resolver path
    # refuses the merge cleanly instead of 500ing after the row is committed.
    except InvalidProjectBranchConfiguration as exc:
        await _block_admin_merge_for_branch_safety(
            task,
            _project,
            session,
            f"Approved rollup merge refused: {exc.detail}",
            issue_code="admin_rollup_integration_branch_invalid",
        )
        raise HTTPException(status_code=409, detail=exc.detail) from exc

    from app.shared_state import (
        get_project_promotion_target as _get_project_promotion_target,
    )

    _promotion_target = _get_project_promotion_target(_project)
    # A human has explicitly approved this rollup merge, so we do NOT refuse a
    # landing on the promotion target here (PR #7064 superseded the earlier
    # hard-refuse of PR #6995 for the human-approval lane). Instead the merge
    # proceeds and classify_rollup_merge_outcome below decides the outcome:
    # a landing on a distinct promotion target raises the out-of-band alert and
    # leaves pr_merged unset so the rollup stays unqualified, rather than
    # dead-ending the operator's explicit approval with a 409. The autonomous
    # rollup path (rollup_management) still HOLDS such landings for human
    # approval first; this handler IS that approval.

    try:
        from app.routers.agent import get_github_token_for_task
        from app.shared_state import runtime_config as _runtime_config
        from app.rollup_management import _cleanup_branches
        from app.github import merge_pull_request, create_pr_comment

        gh_token, owner, repo = await get_github_token_for_task(task.id, session)

        # Pre-merge Alembic dual-head guard. The operator-approved rollup PR
        # combines several child PRs and is exactly the multi-migration vector
        # this guard was built for; the autonomous rollup path guards this, the
        # human-approval path must too. On a block-or-defer verdict, refuse the
        # merge with a 409 (mirroring the branch-safety refusal) rather than
        # silently wedging the integration branch.
        from app.rollup_migration_validation import enforce_premerge_migration_guard

        _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
            gh_token,
            owner,
            repo,
            rollup_pr_number,
            base_branch,
            promotion_target=_promotion_target,
        )
        if not _ok_to_merge:
            detail = (
                f"Approved rollup merge refused: Alembic combined-chain guard "
                f"blocked PR #{rollup_pr_number} — {_hold_reason}"
            )
            await _block_admin_merge_for_branch_safety(
                task,
                _project,
                session,
                detail,
                issue_code="admin_rollup_alembic_dual_head",
            )
            raise HTTPException(status_code=409, detail=detail)

        merge_result = await merge_pull_request(
            gh_token, owner, repo, rollup_pr_number, allow_changes_requested=True
        )
        if merge_result.get("merged"):
            _project = await session.get(Project, task.project_id)
            base_branch = (task.first_payload).get(
                "base_branch"
            ) or get_project_integration_branch(_project)
            (
                record_pr_merged,
                terminal_rollup_merge,
                landed_branch,
            ) = await classify_rollup_merge_outcome(
                task,
                _project,
                session,
                pr_number=rollup_pr_number,
                merged_base_branch=base_branch,
                source="admin_task_mutations.rollup_pr_approval",
            )
            task.status = (
                TaskStatus.COMPLETED if terminal_rollup_merge else TaskStatus.EVALUATING
            )
            # The hotfix lane is intentionally allowed to merge to the promotion
            # target (see _promotion_target_merge_allowed). When it does, the
            # work never landed on the integration branch, so record_pr_merged
            # is False — but it must still unblock the rollup as a
            # merge-equivalent non-landing. Preserve that hotfix unblock here so
            # the hotfix path stamps pr_merged=True / landed_on_integration=False
            # while the non-hotfix promotion-target landing stays unqualified
            # (pr_merged unset).
            task.context = _merge_context_with_hotfix_non_landing(
                task,
                _project,
                base_context=ctx,
                landed_branch=landed_branch,
                record_pr_merged=record_pr_merged,
                preserve_hotfix_non_landing=True,
            )
            if terminal_rollup_merge:
                task.latest_status_message = (
                    f"Human-approved merge of rollup PR #{rollup_pr_number}"
                )
            else:
                task.latest_status_message = (
                    f"Human-approved merge of rollup PR #{rollup_pr_number} "
                    f"into {landed_branch}; awaiting downstream landing"
                )
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                rollup_pr_number,
                "**Blueprint Review Pipeline**: Human-approved rollup merge.",
            )
            changes["merged_pr"] = rollup_pr_number
            logger.info(
                f"[ADMIN] Human-approved rollup merge of PR #{rollup_pr_number}"
            )
            session.add(task)
            await session.commit()
            # Heal this task's direction research-history merge counts now that the
            # human-approved landing is committed. The terminal recompute on the
            # task-completion path never re-fires for an admin merge, so prs_merged
            # would otherwise stay stale-low. No-op unless the task carries
            # direction/research lineage; runs on a fresh session (the directions
            # lock + commit must not piggyback the request session mid-mutation).
            if record_pr_merged:
                await _refresh_direction_outcomes_for_task(task)
            await _cleanup_branches(task, session)
            if base_branch == _promotion_target and _runtime_config.get(
                "smoketest_enabled"
            ):
                await _maybe_spawn_rollup_smoketest(
                    task, owner, repo, base_branch, session
                )
        else:
            changes["merge_error"] = merge_result.get("message", "unknown")
            task.latest_status_message = f"Approved but rollup merge failed: {merge_result.get('message', 'unknown')}"
            logger.error(
                f"[ADMIN] Approved rollup merge of PR #{rollup_pr_number} failed: {merge_result.get('message')}"
            )
    except HTTPException:
        # Branch-safety / dual-head refusals must surface as a conflict to the
        # operator, not be swallowed into a "merge failed" status message.
        raise
    except Exception as e:
        changes["merge_error"] = str(e)[:200]
        task.latest_status_message = f"Approved but rollup merge failed: {str(e)[:200]}"
        logger.error(f"[ADMIN] Approved rollup merge failed: {e}", exc_info=True)


async def _maybe_spawn_rollup_smoketest(
    task: Task,
    owner: str,
    repo: str,
    base_branch: str,
    session: AsyncSession,
) -> None:
    """Spawn a smoketest task after a human-approved rollup merge."""
    from app.shared_state import runtime_config as _runtime_config

    try:
        type_model = (
            _runtime_config.get("task_type_models", {}).get("smoketest") or None
        )
        type_effort = (
            _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
        )
        health_check_urls = _runtime_config.get("health_check_urls") or []
        smoketest_task = Task(
            project_id=task.project_id,
            title=f"Smoketest after rollup merge to {base_branch}",
            description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
            payloads=[
                validate_internal_payload(
                    {
                        "type": "smoketest",
                        "base_branch": base_branch,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {}),
                        **(
                            {"health_check_urls": health_check_urls}
                            if health_check_urls
                            else {}
                        ),
                    },
                    "admin:rollup_smoketest",
                )
            ],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(smoketest_task)
        await session.commit()
        await session.refresh(smoketest_task)
        history = TaskHistory(
            task_id=smoketest_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "ADMIN_ROLLUP_MERGE",
                "base_branch": base_branch,
            },
        )
        session.add(history)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {
                "task_id": str(smoketest_task.id),
                "status": TaskStatus.QUEUED,
            },
        )
        logger.info(
            f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after admin rollup merge to {base_branch}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[SMOKETEST] Failed to spawn smoketest after admin rollup merge: {e}",
            exc_info=True,
        )


async def _handle_task_pr_approval(
    task: Task,
    ctx: Dict[str, Any],
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Handle approval for a regular task PR (critical-file merge or review spawn)."""
    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not artifact or not artifact.pr_url:
        return

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    task_payload = task.first_payload
    _project = await session.get(Project, task.project_id)
    branch = _resolve_task_branch(task, get_project_task_branch_namespace(_project))
    try:
        base_branch = task_payload.get("base_branch") or get_project_integration_branch(
            _project
        )
    # Defense-in-depth: the resolver currently fails SAFE and does not raise.
    # This guard exists so a future re-introduction of a raising resolver path
    # refuses the merge cleanly instead of 500ing after the row is committed.
    except InvalidProjectBranchConfiguration as exc:
        await _block_admin_merge_for_branch_safety(
            task,
            _project,
            session,
            f"Approved merge refused: {exc.detail}",
            issue_code="admin_task_integration_branch_invalid",
        )
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    from app.shared_state import (
        get_project_promotion_target as _get_project_promotion_target,
    )

    promotion_target = _get_project_promotion_target(_project)
    integration_branch = get_project_integration_branch(_project)
    # Refuse ONLY when the merge would land on the promotion target AND that
    # target is distinct from the integration branch. Single-trunk projects
    # (integration == promotion) legitimately merge onto the shared trunk.
    if (
        base_branch == promotion_target
        and base_branch != integration_branch
        and not _promotion_target_merge_allowed(task, base_branch)
    ):
        detail = (
            f"Approved merge refused: target branch '{base_branch}' is this "
            "project's promotion target. Configure a distinct integration branch "
            "before landing fleet work."
        )
        await _block_admin_merge_for_branch_safety(
            task,
            _project,
            session,
            detail,
            issue_code="admin_task_target_matches_promotion_target",
        )
        raise HTTPException(status_code=409, detail=detail)

    if ctx.get("touched_critical"):
        await _merge_critical_pr(task, ctx, pr_number, branch, session, changes)
    else:
        await _spawn_review_for_approved_task(
            task, task_payload, pr_number, branch, base_branch, session, changes
        )

    if "status" in payload and TaskStatus(payload["status"]) == TaskStatus.QUEUED:
        task.status = TaskStatus.COMPLETED
        changes["status"] = TaskStatus.COMPLETED


async def _merge_critical_pr(
    task: Task,
    ctx: Dict[str, Any],
    pr_number: int,
    branch: str,
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Merge a security-critical-file PR on human approval."""
    try:
        from app.routers.agent import get_github_token_for_task
        from app.github import merge_pull_request, create_pr_comment, delete_branch

        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        _project = await session.get(Project, task.project_id)
        base_branch = (task.first_payload).get(
            "base_branch"
        ) or get_project_integration_branch(_project)

        # Pre-merge Alembic dual-head guard. A critical-file PR landing on the
        # integration branch is the same dual-head vector as a normal task PR;
        # validate the combined chain before merging and refuse with a 409 on a
        # block-or-defer verdict rather than wedging the branch.
        from app.rollup_migration_validation import enforce_premerge_migration_guard
        from app.shared_state import (
            get_project_promotion_target as _get_project_promotion_target,
        )

        _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
            gh_token,
            owner,
            repo,
            pr_number,
            base_branch,
            promotion_target=_get_project_promotion_target(_project),
        )
        if not _ok_to_merge:
            detail = (
                f"Approved merge refused: Alembic combined-chain guard blocked "
                f"PR #{pr_number} — {_hold_reason}"
            )
            await _block_admin_merge_for_branch_safety(
                task,
                _project,
                session,
                detail,
                issue_code="admin_critical_alembic_dual_head",
            )
            raise HTTPException(status_code=409, detail=detail)

        merge_result = await merge_pull_request(
            gh_token, owner, repo, pr_number, allow_changes_requested=True
        )
        if merge_result.get("merged"):
            record_pr_merged = await merged_pr_counts_as_integration_landing(
                task,
                _project,
                session,
                pr_number=pr_number,
                merged_base_branch=base_branch,
                source="admin_task_mutations.critical_pr_approval",
            )
            task.status = TaskStatus.COMPLETED
            task.context = _merge_context_with_hotfix_non_landing(
                task,
                _project,
                base_context=ctx,
                landed_branch=base_branch,
                record_pr_merged=record_pr_merged,
            )
            task.latest_status_message = f"Human-approved merge of PR #{pr_number}"
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).",
            )
            try:
                await delete_branch(gh_token, owner, repo, branch)
            except Exception:
                logger.exception(
                    f"[ADMIN] Failed to delete branch {branch} after merge"
                )
            changes["merged_pr"] = pr_number
            # Heal this task's direction research-history merge counts after the
            # shared session commits in _commit_and_broadcast. _merge_critical_pr
            # defers its commit there, so the refresh (which queries committed
            # child rows) must run post-commit — flag it for the caller.
            if record_pr_merged:
                changes["_refresh_direction_after_merge"] = True
            logger.info(
                f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)"
            )
        else:
            changes["merge_error"] = merge_result.get("message", "unknown")
            task.latest_status_message = (
                f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
            )
            logger.error(
                f"[ADMIN] Approved merge of PR #{pr_number} failed: {merge_result.get('message')}"
            )
    except HTTPException:
        # Branch-safety / dual-head refusals must surface as a conflict to the
        # operator, not be swallowed into a "merge failed" status message.
        raise
    except Exception as e:
        changes["merge_error"] = str(e)[:200]
        task.latest_status_message = f"Approved but merge failed: {str(e)[:200]}"
        logger.error(f"[ADMIN] Approved merge failed: {e}", exc_info=True)


def _record_failure_result(task: Task, failure_class: str) -> None:
    """Record a task failure for deployment monitoring and direction health."""
    try:
        from app.routers.agent_status import record_task_result
        from app.shared_state import record_direction_result

        record_task_result(
            success=False,
            failure_class=failure_class,
            task_id=str(task.id),
            task_type=(task.first_payload or {}).get("type", ""),
        )
        direction_id = str((task.context or {}).get("direction_id") or "")
        if direction_id:
            record_direction_result(
                direction_id,
                success=False,
                failure_class=failure_class,
                project_id=str(task.project_id) if task.project_id else "",
            )
    except Exception:
        logger.exception(f"[ADMIN] Failed to record failure result for task {task.id}")


async def _spawn_review_for_approved_task(
    task: Task,
    task_payload: Dict[str, Any],
    pr_number: int,
    branch: str,
    base_branch: str,
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Spawn a review task for a security-scan-approved task."""
    from app.review_pipeline import _spawn_pipeline_task

    original_prompt = task_payload.get("prompt", task.description or "")
    review_task, _was_created = await _spawn_pipeline_task(
        task,
        "review",
        session,
        pr_number,
        branch,
        base_branch,
        original_prompt=original_prompt,
        original_task_id=str(task.id),
    )
    if review_task:
        changes["spawned_review"] = str(review_task.id)
    else:
        task.requires_approval = True
        task.status = TaskStatus.FAILED
        if task.vm_provisioned_at:
            task.vm_stopped_at = _utcnow()
        task.latest_status_message = "Iteration cap reached — no follow-on task spawned"
        from app.failure_classification import persist_failure_category

        persist_failure_category(task, "AGENT_TIMEOUT")
        _record_failure_result(task, "AGENT_TIMEOUT")
        session.add(task)
        await session.commit()
        raise HTTPException(
            status_code=422,
            detail="Iteration cap reached — cannot spawn follow-on task. Archive this task or reset iteration count.",
        )


# ---------------------------------------------------------------------------
# Phase 3: simple field mutations
# ---------------------------------------------------------------------------


async def _apply_simple_mutations(
    task: Task,
    payload: Dict[str, Any],
    changes: Dict[str, Any],
) -> None:
    """Apply payloads, parent_task_id, context, and post_commit_pending mutations."""
    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        task.parent_task_id = (
            uuid.UUID(payload["parent_task_id"]) if payload["parent_task_id"] else None
        )
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
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
            (await session.execute(select(Task).where(Task.id.in_(dep_uuids))))
            .scalars()
            .all()
        )
        if len(existing_tasks) != len(dep_uuids):
            raise HTTPException(
                status_code=400,
                detail="One or more depends_on task IDs do not exist",
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
                task_cache[start_id] = await session.get(Task, uuid.UUID(start_id))
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

    session.add(task)
    history = TaskHistory(
        task_id=task.id,
        action="MODIFIED",
        details={"changes": changes, "modified_by": "USER"},
    )
    session.add(history)
    await session.commit()
    await session.refresh(task)

    if _refresh_direction_after_merge:
        await _refresh_direction_outcomes_for_task(task)

    old_vm = changes.pop("_old_vm_to_delete", None)
    if old_vm:
        from app.vm_lifecycle import _ensure_vm_deleted

        await _ensure_vm_deleted(old_vm, reason="admin_requeue")

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

    await _apply_status_mutation(
        task, payload, background_tasks, session, force_kill, changes
    )
    await _apply_approval_mutation(task, payload, session, changes)
    await _apply_simple_mutations(task, payload, changes)
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
