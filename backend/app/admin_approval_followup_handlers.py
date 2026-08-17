"""Large downstream handlers behind the admin approval facade.

The canonical, monkeypatchable entry points remain in
``app.admin_task_approvals``.  This leaf is imported lazily by those wrappers
so importing the facade first or the mutation re-export first stays safe.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    ApprovalDecision,
    approval_followup_authority,
    approval_merge_intent_writer,
    approval_merge_receipt_writer,
    approval_merge_submission_starter,
)
from app.admin_approval_merge_protocol import (
    _project_merge_config,
    _promotion_merge_policy_snapshot,
)
from app.branch_resolution import InvalidProjectBranchConfiguration
from app.models import (
    TASK_ORIGINATION_SMOKETEST,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import get_project_integration_branch
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    automatic_task_creation_parent_lock,
)

logger = logging.getLogger("app.admin_task_mutations")


def _approval_facade():
    """Load the canonical facade lazily to avoid an eager import back-edge."""
    import app.admin_task_approvals as facade

    return facade


def _mutation_facade():
    """Load mutation re-exports lazily so facade monkeypatch seams remain."""
    import app.admin_task_mutations as facade

    return facade


async def run_approval_merge_post_effects(
    decision: ApprovalDecision,
    session: AsyncSession,
) -> None:
    from app.admin_approval_merge_effects import run_approval_merge_post_effects as run

    await run(decision, session)


async def handle_rollup_pr_approval(
    task: Task,
    ctx: Dict[str, Any],
    rollup_pr_number: int,
    session: AsyncSession,
    changes: Dict[str, Any],
    *,
    approval_decision: ApprovalDecision,
) -> None:
    """Implement the human-approved rollup merge path."""
    approvals = _approval_facade()
    _project_id = task.project_id
    _project = await session.get(Project, _project_id)
    try:
        base_branch = (task.first_payload).get(
            "base_branch"
        ) or get_project_integration_branch(_project)
    # Defense-in-depth: the resolver currently fails SAFE and does not raise.
    # This guard exists so a future re-introduction of a raising resolver path
    # refuses the merge cleanly instead of 500ing after the row is committed.
    except InvalidProjectBranchConfiguration as exc:
        await approvals._refuse_approval_or_legacy(
            task,
            _project,
            session,
            f"Approved rollup merge refused: {exc.detail}",
            issue_code="admin_rollup_integration_branch_invalid",
            approval_decision=approval_decision,
        )

    from app.shared_state import (
        get_project_promotion_target as _get_project_promotion_target,
    )

    _promotion_target = _get_project_promotion_target(_project)
    _prechecked_project_config = _project_merge_config(_project)
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
        from app.github import merge_pull_request

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
            await approvals._refuse_approval_or_legacy(
                task,
                _project,
                session,
                detail,
                issue_code="admin_rollup_alembic_dual_head",
                approval_decision=approval_decision,
            )
            raise RuntimeError("rollup approval refusal helper returned")
        _prechecked_policy = {
            "pr_number": int(rollup_pr_number),
            "base_branch": str(base_branch),
            "promotion_target": str(_promotion_target or ""),
            "migration_guard": "ok",
            "promotion_policy": _promotion_merge_policy_snapshot(
                task,
                _prechecked_project_config,
                base_branch=base_branch,
                followup_kind=approval_decision.followup_kind,
            ),
        }

        async def _submit_human_rollup_merge(expected_head_sha: str) -> dict:
            return await merge_pull_request(
                gh_token,
                owner,
                repo,
                rollup_pr_number,
                project_id=str(_project_id),
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
            source_authority=approval_followup_authority(
                approval_decision,
                allowed_states=frozenset({"merge_intent"}),
            ),
            submission_intent=approval_merge_intent_writer(
                approval_decision,
                owner=owner,
                repo=repo,
                branch=str(
                    ctx.get("plan_branch")
                    or ctx.get("research_branch")
                    or (task.first_payload).get("branch")
                    or ""
                ),
                base_branch=base_branch,
                expected_project_config=_prechecked_project_config,
                prechecked_policy=_prechecked_policy,
            ),
            submission_start=approval_merge_submission_starter(approval_decision),
            submission_receipt=approval_merge_receipt_writer(
                approval_decision,
                branch=str(
                    ctx.get("plan_branch")
                    or ctx.get("research_branch")
                    or (task.first_payload).get("branch")
                    or ""
                ),
                base_branch=base_branch,
            ),
        )
        if not _merge_authority.allowed:
            return
        if merge_result is None:
            merge_result = {
                "merged": False,
                "message": "held because a same-PR code fix is active",
            }
        if merge_result.get("merged"):
            finalized_task = await approvals._finalize_approval_merge(
                approval_decision, session, changes
            )
            if finalized_task is None:
                return
            logger.info(
                f"[ADMIN] Human-approved rollup merge of PR #{rollup_pr_number}"
            )
        else:
            detail = (
                "Approved but rollup merge failed: "
                f"{merge_result.get('message', 'unknown')}"
            )
            raise approvals.ApprovalFollowupRefused(detail)
    except (approvals.ApprovalFollowupRefused, HTTPException):
        # Branch-safety / dual-head refusals must surface as a conflict to the
        # operator, not be swallowed into a "merge failed" status message.
        raise
    except Exception as e:
        from app.review_merge_authority import MergeSubmissionIndeterminate

        if isinstance(e, MergeSubmissionIndeterminate):
            raise
        raise approvals.ApprovalFollowupRefused(
            f"Approved but rollup merge failed: {str(e)[:200]}"
        ) from e


async def maybe_spawn_rollup_smoketest(
    task: Task,
    owner: str,
    repo: str,
    base_branch: str,
    session: AsyncSession,
    *,
    spawned_by: str = "ADMIN_ROLLUP_MERGE",
) -> bool:
    """Implement post-rollup smoketest admission and publication."""
    from app.shared_state import runtime_config as _runtime_config

    _project_id = task.project_id
    _parent_task_id = task.id
    smoketest_task: Task | None = None
    try:
        async with automatic_task_creation_parent_lock(
            session,
            _project_id,
            _parent_task_id,
            skip_locked=False,
        ) as (_admission, _project, _parent_task):
            if _project is None or _parent_task is None:
                await session.rollback()
                return False
            task = _parent_task
            # The parent row serializes intent-keyed dedup with publication.
            spawn_histories = (
                (
                    await session.execute(
                        select(TaskHistory)
                        .join(Task, Task.id == TaskHistory.task_id)
                        .where(
                            Task.project_id == _project_id,
                            TaskHistory.action == "SPAWNED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if any(
                (history.details or {}).get("spawned_by") == spawned_by
                and str((history.details or {}).get("parent_task_id") or "")
                == str(_parent_task_id)
                for history in spawn_histories
            ):
                await session.commit()
                return True
            if _admission != AUTOMATIC_TASK_CREATION_ALLOWED:
                task.context = {
                    **(task.context or {}),
                    "automatic_smoketest_creation_suppressed": _admission,
                }
                session.add(task)
                await session.commit()
                logger.info(
                    "[SMOKETEST] Suppressed post-rollup smoketest for task %s: %s",
                    _parent_task_id,
                    _admission,
                )
                return False

            type_model = (
                _runtime_config.get("task_type_models", {}).get("smoketest") or None
            )
            type_effort = (
                _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
            )
            health_check_urls = _runtime_config.get("health_check_urls") or []
            smoketest_task = Task(
                project_id=task.project_id,
                parent_task_id=task.id,
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
            await session.flush([smoketest_task])
            session.add(
                TaskHistory(
                    task_id=smoketest_task.id,
                    action="SPAWNED",
                    details={
                        "spawned_by": spawned_by,
                        "base_branch": base_branch,
                        "parent_task_id": str(_parent_task_id),
                    },
                )
            )
            await session.commit()
        assert smoketest_task is not None
        await session.refresh(smoketest_task)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        logger.warning(
            f"[SMOKETEST] Failed to spawn smoketest after admin rollup merge: {e}",
            exc_info=True,
        )
        return False
    try:
        await _mutation_facade().broadcast_to_project(
            str(_project_id),
            "task_updated",
            {
                "task_id": str(smoketest_task.id),
                "status": TaskStatus.QUEUED,
            },
        )
    except Exception:
        # Child + history are already atomic and durable; a retry dedups them.
        logger.exception(
            "[SMOKETEST] Broadcast failed after durable approval smoketest %s",
            smoketest_task.id,
        )
    logger.info(
        "[SMOKETEST] Spawned approval smoketest %s after merge to %s",
        smoketest_task.id,
        base_branch,
    )
    return True


async def merge_critical_pr(
    task: Task,
    ctx: Dict[str, Any],
    pr_number: int,
    branch: str,
    session: AsyncSession,
    changes: Dict[str, Any],
    *,
    approval_decision: ApprovalDecision,
) -> None:
    """Implement the security-critical PR merge path."""
    approvals = _approval_facade()
    _project_id = task.project_id
    try:
        from app.routers.agent import get_github_token_for_task
        from app.github import merge_pull_request

        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        _project = await session.get(Project, _project_id)
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
            await approvals._refuse_approval_or_legacy(
                task,
                _project,
                session,
                detail,
                issue_code="admin_critical_alembic_dual_head",
                approval_decision=approval_decision,
            )
            raise RuntimeError("critical approval refusal helper returned")
        _prechecked_project_config = _project_merge_config(_project)
        _prechecked_policy = {
            "pr_number": int(pr_number),
            "base_branch": str(base_branch),
            "promotion_target": str(
                _prechecked_project_config.get("promotion_target") or ""
            ),
            "migration_guard": "ok",
            "promotion_policy": _promotion_merge_policy_snapshot(
                task,
                _prechecked_project_config,
                base_branch=base_branch,
                followup_kind=approval_decision.followup_kind,
            ),
        }

        async def _submit_human_critical_merge(expected_head_sha: str) -> dict:
            return await merge_pull_request(
                gh_token,
                owner,
                repo,
                pr_number,
                project_id=str(_project_id),
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
            source_authority=approval_followup_authority(
                approval_decision,
                allowed_states=frozenset({"merge_intent"}),
            ),
            submission_intent=approval_merge_intent_writer(
                approval_decision,
                owner=owner,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
                expected_project_config=_prechecked_project_config,
                prechecked_policy=_prechecked_policy,
            ),
            submission_start=approval_merge_submission_starter(approval_decision),
            submission_receipt=approval_merge_receipt_writer(
                approval_decision,
                branch=branch,
                base_branch=base_branch,
            ),
        )
        if not _merge_authority.allowed:
            return
        if merge_result is None:
            merge_result = {
                "merged": False,
                "message": "held because a same-PR code fix is active",
            }
        if merge_result.get("merged"):
            finalized_task = await approvals._finalize_approval_merge(
                approval_decision, session, changes
            )
            if finalized_task is None:
                return
            logger.info(
                f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)"
            )
        else:
            detail = (
                f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
            )
            raise approvals.ApprovalFollowupRefused(detail)
    except (approvals.ApprovalFollowupRefused, HTTPException):
        # Branch-safety / dual-head refusals must surface as a conflict to the
        # operator, not be swallowed into a "merge failed" status message.
        raise
    except Exception as e:
        from app.review_merge_authority import MergeSubmissionIndeterminate

        if isinstance(e, MergeSubmissionIndeterminate):
            raise
        raise approvals.ApprovalFollowupRefused(
            f"Approved but merge failed: {str(e)[:200]}"
        ) from e
