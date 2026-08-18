"""Canonical approval and pull-request merge orchestration for admin mutations.

This module owns the approval-specific mutation cohort extracted from
``admin_task_mutations``.  The latter re-exports these helpers so existing
imports remain stable while this module provides one implementation.

The public helper names here are also intentional compatibility seams.  Tests,
operator routes, and the mutation facade monkeypatch these names directly, so
the large GitHub and smoketest handlers live behind thin lazy wrappers instead
of being imported eagerly.  Keeping the wrappers canonical avoids a second
implementation while preventing an import cycle among the approval facade, the
mutation facade, and the downstream handler leaf.  Authority decisions and
fresh-row finalization remain in this module because they define the shared
transaction boundary used by every downstream approval kind.
Downstream effects therefore remain replaceable without moving claim authority.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY as APPROVAL_FOLLOWUP_CLAIM_KEY,
    ApprovalDecision,
    approval_child_spawn_publication,
    consume_approval_followup_after_dedup,
    approval_claim_state,
    _claim_matches_authoritative_task,
    _claim_matches_task,
    _restore_refused_approval,
    approval_followup_authority,
    approval_review_spawn_claim_target,
    decide_operator_approval,
    establish_independent_approval_hold,
)
from app.branch_resolution import (
    InvalidProjectBranchConfiguration,
    _signal_blocking_project_branch_configuration,
)
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.shared_state import (
    get_project_integration_branch,
    get_project_task_branch_namespace,
    resolve_task_branch as _resolve_task_branch,
)
from app.task_locking import (
    lock_project_then_task,
)
from app.review_merge_authority import MergeSubmissionIndeterminate

logger = logging.getLogger("app.admin_task_mutations")


class ApprovalFollowupRefused(Exception):
    """A pre-submission safety verdict awaiting fresh claim authority."""

    def __init__(self, detail: str, *, issue_code: str | None = None) -> None:
        self.detail = detail
        self.issue_code = issue_code
        super().__init__(detail)


async def _refuse_approval_or_legacy(
    task: Task,
    project: Project | None,
    session: AsyncSession,
    detail: str,
    *,
    issue_code: str,
    approval_decision: ApprovalDecision | None,
) -> None:
    if approval_decision is not None:
        raise ApprovalFollowupRefused(detail, issue_code=issue_code)
    await _mutation_facade()._block_admin_merge_for_branch_safety(
        task, project, session, detail, issue_code=issue_code
    )
    raise HTTPException(status_code=409, detail=detail)


async def _persist_approval_refusal(
    decision: ApprovalDecision,
    session: AsyncSession,
    refusal: ApprovalFollowupRefused,
) -> bool:
    """Re-arm only the still-current claim under Project -> Task authority."""

    project_id = decision.project_id
    task_id = decision.task_id
    await session.rollback()
    await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    )
    async with lock_project_then_task(
        session,
        project_id,
        task_id,
        skip_locked=False,
    ) as (project, task):
        if (
            project is None
            or task is None
            or not await _claim_matches_authoritative_task(
                task,
                decision,
                session,
                allowed_states=frozenset({"pending"}),
            )
        ):
            await session.rollback()
            return False
        if refusal.issue_code:
            _signal_blocking_project_branch_configuration(
                project, refusal.issue_code, refusal.detail
            )
        establish_independent_approval_hold(
            task,
            reason=refusal.detail,
            owner="approval_followup_refused",
        )
        task.latest_status_message = refusal.detail[:500]
        session.add(project)
        session.add(task)
        session.add(
            TaskHistory(
                task_id=task.id,
                action="MODIFIED",
                details={
                    "changes": {
                        "approval_followup_pending": False,
                        "approval_refused": refusal.issue_code or "merge_not_completed",
                    },
                    "modified_by": decision.modified_by,
                    "approval_decision_token": (decision.claim or {}).get("token"),
                },
            )
        )
        await session.commit()
        return True


async def _finalize_approval_merge(
    decision: ApprovalDecision,
    session: AsyncSession,
    changes: Dict[str, Any],
) -> Task | None:
    """Canonical facade seam for lock-safe merge-fact finalization."""

    from app.admin_approval_merge_finalization import finalize_approval_merge

    return await finalize_approval_merge(decision, session, changes)


async def execute_approval_followup(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> tuple[Task, Dict[str, Any]]:
    """Run downstream work only while the committed receipt remains current."""

    task_id = decision.task_id
    task = await session.get(Task, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not decision.merge_recovery and (
        not decision.should_follow_up or not _claim_matches_task(task, decision)
    ):
        # Close the read transaction without expiring every prefetched object in
        # bulk callers. No source mutation is pending on this path.
        await session.commit()
        return task, {}

    changes: Dict[str, Any] = {}
    context = dict(task.context or {})
    try:
        if decision.merge_recovery:
            if not decision.post_effects_only:
                from app.admin_approval_authority import (
                    reconcile_approval_merge_ledger,
                )

                await reconcile_approval_merge_ledger(decision, session)
                await _finalize_approval_merge(decision, session, changes)
        elif approval_claim_state(task, decision) == "merge_submitted":
            await _finalize_approval_merge(decision, session, changes)
        elif decision.rollup and decision.pr_number is not None:
            await _handle_rollup_pr_approval(
                task,
                context,
                decision.pr_number,
                session,
                changes,
                approval_decision=decision,
            )
        else:
            await _handle_task_pr_approval(
                task,
                context,
                {},
                session,
                changes,
                approval_decision=decision,
            )
    except ApprovalFollowupRefused as refusal:
        await _persist_approval_refusal(decision, session, refusal)
        raise HTTPException(status_code=409, detail=refusal.detail) from refusal
    except MergeSubmissionIndeterminate as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail=(
                "Merge submission is indeterminate; Blueprint will only "
                "reconcile the exact PR and will not replay the PUT"
            ),
        ) from exc
    except HTTPException as exc:
        await session.rollback()
        await _restore_refused_approval(decision, session, detail=str(exc.detail))
        raise

    try:
        await _run_approval_merge_post_effects(decision, session)
    except Exception:
        # Objective merge/finalization is already committed.  Post-effects are
        # ledger-marked and retried by a duplicate approval request.
        logger.exception(
            "[ADMIN] Approval merge post-effects deferred for task %s",
            task_id,
        )
    if session.in_transaction():
        # Every authoritative result is committed at its own fresh boundary.
        # Anything left here belongs to a pre-I/O identity and is non-authority.
        await session.rollback()
    final_task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if final_task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await session.commit()
    return final_task, changes


def _mutation_facade():
    """Load the facade lazily so the canonical leaf is safe to import first."""
    import app.admin_task_mutations as facade

    return facade


async def _run_approval_merge_post_effects(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> None:
    from app.admin_approval_followup_handlers import run_approval_merge_post_effects

    await run_approval_merge_post_effects(decision, session)


async def _apply_approval_mutation(
    task: Task,
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
) -> None:
    """Compatibility wrapper around the canonical two-phase boundary."""
    if "requires_approval" not in payload:
        return
    _, decision, decision_changes = await decide_operator_approval(
        task.id,
        bool(payload["requires_approval"]),
        session,
        modified_by="USER",
    )
    changes.update(decision_changes)
    _, followup_changes = await execute_approval_followup(decision, session)
    changes.update(followup_changes)


async def _handle_rollup_pr_approval(
    task: Task,
    ctx: Dict[str, Any],
    rollup_pr_number: int,
    session: AsyncSession,
    changes: Dict[str, Any],
    *,
    approval_decision: ApprovalDecision,
) -> None:
    """Merge the rollup PR and optionally spawn a smoketest on human approval."""
    from app.admin_approval_followup_handlers import handle_rollup_pr_approval

    await handle_rollup_pr_approval(
        task,
        ctx,
        rollup_pr_number,
        session,
        changes,
        approval_decision=approval_decision,
    )


async def _maybe_spawn_rollup_smoketest(
    task: Task,
    owner: str,
    repo: str,
    base_branch: str,
    session: AsyncSession,
    *,
    spawned_by: str = "ADMIN_ROLLUP_MERGE",
) -> bool:
    """Spawn a smoketest task after a human-approved rollup merge."""
    from app.admin_approval_followup_handlers import maybe_spawn_rollup_smoketest

    return await maybe_spawn_rollup_smoketest(
        task,
        owner,
        repo,
        base_branch,
        session,
        spawned_by=spawned_by,
    )


async def _handle_task_pr_approval(
    task: Task,
    ctx: Dict[str, Any],
    payload: Dict[str, Any],
    session: AsyncSession,
    changes: Dict[str, Any],
    *,
    approval_decision: ApprovalDecision,
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
        await _refuse_approval_or_legacy(
            task,
            _project,
            session,
            f"Approved merge refused: {exc.detail}",
            issue_code="admin_task_integration_branch_invalid",
            approval_decision=approval_decision,
        )
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
        await _refuse_approval_or_legacy(
            task,
            _project,
            session,
            detail,
            issue_code="admin_task_target_matches_promotion_target",
            approval_decision=approval_decision,
        )

    if ctx.get("touched_critical"):
        await _merge_critical_pr(
            task,
            ctx,
            pr_number,
            branch,
            session,
            changes,
            approval_decision=approval_decision,
        )
    else:
        await _spawn_review_for_approved_task(
            task,
            task_payload,
            pr_number,
            branch,
            base_branch,
            session,
            changes,
            approval_decision=approval_decision,
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
    *,
    approval_decision: ApprovalDecision,
) -> None:
    """Merge a security-critical-file PR on human approval."""
    from app.admin_approval_followup_handlers import merge_critical_pr

    await merge_critical_pr(
        task,
        ctx,
        pr_number,
        branch,
        session,
        changes,
        approval_decision=approval_decision,
    )


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
    *,
    approval_decision: ApprovalDecision,
) -> None:
    """Spawn a review task for a security-scan-approved task."""
    from app.review_pipeline import _spawn_pipeline_task

    original_prompt = task_payload.get("prompt", task.description or "")
    review_target = approval_review_spawn_claim_target(approval_decision)
    if review_target is None:
        raise ApprovalFollowupRefused(
            "Approved review spawn refused: exact project/PR/base/head target "
            "is unavailable"
        )
    authority_state = {"granted": False}
    source_authority = approval_followup_authority(approval_decision)

    async def _tracked_source_authority(
        authority_session: AsyncSession,
    ) -> Task | None:
        authoritative_task = await source_authority(authority_session)
        authority_state["granted"] = authoritative_task is not None
        return authoritative_task

    review_task, _was_created = await _spawn_pipeline_task(
        task,
        "review",
        session,
        pr_number,
        branch,
        base_branch,
        original_prompt=original_prompt,
        original_task_id=str(task.id),
        review_target=review_target,
        source_spawn_authority=_tracked_source_authority,
        source_spawn_publication=approval_child_spawn_publication(approval_decision),
    )
    if review_task:
        if not _was_created and not await consume_approval_followup_after_dedup(
            approval_decision, review_task.id, session
        ):
            return
        changes["spawned_review"] = str(review_task.id)
    elif not authority_state["granted"]:
        # Project/PR contention or a newer hold/replacement won before the final
        # Task authority callback. Never write the pre-I/O ORM identity back.
        await session.rollback()
        return
    else:
        raise ApprovalFollowupRefused(
            "Iteration cap reached — no follow-on task spawned"
        )
