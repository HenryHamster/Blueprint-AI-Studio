"""Canonical approval and pull-request merge orchestration for admin mutations.

This module owns the approval-specific mutation cohort extracted from
``admin_task_mutations``.  The latter re-exports these helpers so existing
imports remain stable while this module provides one implementation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.branch_resolution import (
    InvalidProjectBranchConfiguration,
    classify_rollup_merge_outcome,
    merged_pr_counts_as_integration_landing,
)
from app.models import (
    TASK_ORIGINATION_SMOKETEST,
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
    _utcnow,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    get_project_integration_branch,
    get_project_task_branch_namespace,
    resolve_task_branch as _resolve_task_branch,
)

logger = logging.getLogger("app.admin_task_mutations")


def _mutation_facade():
    """Load the facade lazily so the canonical leaf is safe to import first."""
    import app.admin_task_mutations as facade

    return facade

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
        await _mutation_facade()._block_admin_merge_for_branch_safety(
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
            await _mutation_facade()._block_admin_merge_for_branch_safety(
                task,
                _project,
                session,
                detail,
                issue_code="admin_rollup_alembic_dual_head",
            )
            raise HTTPException(status_code=409, detail=detail)

        async def _submit_human_rollup_merge(expected_head_sha: str) -> dict:
            return await merge_pull_request(
                gh_token,
                owner,
                repo,
                rollup_pr_number,
                project_id=str(task.project_id),
                expected_head_sha=expected_head_sha,
                allow_changes_requested=True,
            )

        from app.review_policy_merge import attempt_explicit_human_merge

        _merge_authority, merge_result = await attempt_explicit_human_merge(
            task,
            session,
            merge_call=_submit_human_rollup_merge,
            gh_token=gh_token,
            owner=owner,
            repo=repo,
            pr_number=rollup_pr_number,
            branch=str(
                ctx.get("plan_branch")
                or ctx.get("research_branch")
                or (task.first_payload).get("branch")
                or ""
            ),
            base_branch=base_branch,
        )
        if merge_result is None:
            merge_result = {
                "merged": False,
                "message": "held because a same-PR code fix is active",
            }
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
            task.context = await _mutation_facade()._merge_context_with_hotfix_non_landing(
                task,
                _project,
                session,
                base_context=ctx,
                landed_branch=landed_branch,
                record_pr_merged=record_pr_merged,
                preserve_hotfix_non_landing=True,
                merge_commit_sha=merge_result.get("sha"),
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
                await _mutation_facade()._refresh_direction_outcomes_for_task(task)
            await _cleanup_branches(task, session)
            if base_branch == _promotion_target and _runtime_config.get(
                "smoketest_enabled"
            ):
                await _mutation_facade()._maybe_spawn_rollup_smoketest(
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
            origination=TASK_ORIGINATION_SMOKETEST,
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
        await _mutation_facade().broadcast_to_project(
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
    except Exception as e:
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
        await _mutation_facade()._block_admin_merge_for_branch_safety(
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
        and not _mutation_facade()._promotion_target_merge_allowed(task, base_branch)
    ):
        detail = (
            f"Approved merge refused: target branch '{base_branch}' is this "
            "project's promotion target. Configure a distinct integration branch "
            "before landing fleet work."
        )
        await _mutation_facade()._block_admin_merge_for_branch_safety(
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
            await _mutation_facade()._block_admin_merge_for_branch_safety(
                task,
                _project,
                session,
                detail,
                issue_code="admin_critical_alembic_dual_head",
            )
            raise HTTPException(status_code=409, detail=detail)

        async def _submit_human_critical_merge(expected_head_sha: str) -> dict:
            return await merge_pull_request(
                gh_token,
                owner,
                repo,
                pr_number,
                project_id=str(task.project_id),
                expected_head_sha=expected_head_sha,
                allow_changes_requested=True,
            )

        from app.review_policy_merge import attempt_explicit_human_merge

        _merge_authority, merge_result = await attempt_explicit_human_merge(
            task,
            session,
            merge_call=_submit_human_critical_merge,
            gh_token=gh_token,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
        )
        if merge_result is None:
            merge_result = {
                "merged": False,
                "message": "held because a same-PR code fix is active",
            }
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
            task.context = await _mutation_facade()._merge_context_with_hotfix_non_landing(
                task,
                _project,
                session,
                base_context=ctx,
                landed_branch=base_branch,
                record_pr_merged=record_pr_merged,
                merge_commit_sha=merge_result.get("sha"),
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
