"""The ONE implementation of a review-fix iteration-cap reset (the UNPARK lever).

Two callers need this operation and they must not diverge:

* the operator lever ``POST /api/admin/tasks/{task_id}/reset-iteration-cap``
  (``app.admin.reset_iteration_cap``), and
* the strategist/Resident op ``reset_iteration_cap`` on
  ``POST /api/agent/strategic`` (``app.routers.admin_agent_ops``), added so
  releasing a parked review-fix chain does not bottleneck on a human.

This module holds the body; both callers do their own authorization and then
call :func:`apply_iteration_cap_reset`. A second, parallel implementation is
exactly the drift this extraction exists to prevent — per
``app.review_park_disposition``, this endpoint is one of only three documented
releases for a chain parked "for operator disposition", so a divergent copy
would silently strand rows.

WHAT THE RESET DOES
    * archives the FAILED ``review``/``fix`` tasks on the PR so they leave the
      per-PR cap census;
    * zeroes the per-chain ``review_loop.iteration`` counter and stamps
      ``iteration_reset_at`` / ``iteration_reset_by`` — that stamp is what also
      releases the merge gate's exact-target respawn budget, whose census
      excludes every review row created at or before it;
    * drops the gate's stale read-model of that budget;
    * clears the ``review_loop_abandon_candidate`` park marker; and
    * clears the approval hold ONLY when the hold provably belongs to this cap
      episode.

WHAT IT DOES NOT DO
    * It does not re-spawn anything. The next orchestrator cycle or an explicit
      rollup has to pick the chain back up.
    * It never closes, merges, or otherwise touches the PR.
    * It never stamps ``resolved: True`` on the terminal outcome — a reset is
      not evidence the loop converged (see ``review_park_disposition``).
"""

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


class IterationCapResetError(Exception):
    """A refusal that maps to an HTTP status at the caller's boundary.

    ``status_code`` mirrors what the operator route already returned for each
    case (404 missing, 400 no PR, 409 pending post-commit) so extracting the
    body did not change a single response code.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _task_payload_dict(task: Task) -> dict:
    return task.first_payload or {}


async def apply_iteration_cap_reset(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: str,
) -> dict:
    """Reset the review-fix iteration cap for the PR ``task_id`` parks on.

    ``project_id`` is the caller's ALREADY-AUTHORIZED project scope; the locked
    re-read is filtered by it so a stale route identity can never reach across
    projects. ``actor`` is recorded as ``review_loop.iteration_reset_by``.

    Commits, and returns the operator-facing receipt. Raises
    :class:`IterationCapResetError` for the three refusals.
    """
    # The caller's authorization lookup is intentionally read-only. Re-read and
    # lock the exact row before interpreting cap ownership or mutating its
    # context so a newer manual/security/spend episode cannot be cleared by a
    # stale route identity.
    task = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        raise IterationCapResetError(404, "Task not found")

    ctx = task.context or {}
    review_loop = ctx.get("review_loop", {}) or {}
    pr_number = review_loop.get("pr_number") or ctx.get("rollup_pr_number")
    if not pr_number:
        raise IterationCapResetError(
            400,
            "Task has no PR number in context.review_loop.pr_number or context.rollup_pr_number",
        )

    # Archive FAILED review/fix tasks on this PR so they drop from the global count.
    # _pr_number_match is dialect-conditional: on SQLite, .as_string() JSON_QUOTE-
    # double-quotes the int and never matches, so reuse the shared helper which
    # falls back to func.json_extract for SQLite.
    from app.ci_completion import _pr_number_match

    failed_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == task.project_id,
            Task.status == TaskStatus.FAILED,
            Task.archived.is_(False),
            _pr_number_match(session, pr_number),
        )
        .options(
            load_only(
                Task.id,
                Task.archived,
                Task.payloads,
                Task.post_commit_pending,
                raiseload=True,
            )
        )
    )
    failed_review_fix_tasks = [
        candidate
        for candidate in failed_result.scalars().all()
        if _task_payload_dict(candidate).get("type") in ("review", "fix")
    ]
    pending_failed = next(
        (
            candidate
            for candidate in failed_review_fix_tasks
            if candidate.post_commit_pending
        ),
        None,
    )
    if pending_failed is not None:
        raise IterationCapResetError(
            409,
            "Cannot reset the iteration cap while failed review/fix task "
            f"{pending_failed.id} has a pending post-commit continuation",
        )
    failed_review_fix_ids = [candidate.id for candidate in failed_review_fix_tasks]
    archived_count = 0
    if failed_review_fix_ids:
        if resolve_dialect_name(session) == "sqlite":
            failed_task_type = func.json_extract(Task.payloads, "$[0].type")
        else:
            failed_task_type = Task.payloads[0]["type"].as_string()
        archive_result = await session.execute(
            update(Task)
            .where(
                Task.id.in_(failed_review_fix_ids),
                Task.project_id == task.project_id,
                Task.status == TaskStatus.FAILED,
                Task.archived.is_(False),
                Task.post_commit_pending.is_(False),
                failed_task_type.in_(("review", "fix")),
                _pr_number_match(session, pr_number),
            )
            .values(archived=True, version=Task.version + 1)
            .execution_options(synchronize_session=False)
        )
        archived_count = max(0, int(archive_result.rowcount or 0))

    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        approval_hold_provenance,
        consume_approval_hold_metadata,
        revoke_operator_approval_followup,
    )

    hold_provenance = ctx.get(APPROVAL_HOLD_PROVENANCE_KEY)
    hold_reason = ctx.get("approval_hold_reason")
    cap_hold_owned = bool(
        task.requires_approval is True
        and isinstance(hold_provenance, dict)
        and hold_provenance.get("episode")
        == ctx.get(APPROVAL_HOLD_EPISODE_KEY)
        and hold_provenance.get("set_at") == ctx.get("_approval_set_at")
        and hold_provenance.get("reason") == hold_reason
        and (
            (
                hold_provenance.get("owner") == "review_loop_convergence"
                and hold_reason
                in {
                    "approved_cap_pr_preserved_for_handmerge",
                    "review_cap_preserved_for_handmerge",
                    "review_cap_exhausted",
                }
            )
            or (
                hold_provenance.get("owner") == "review_pipeline"
                and hold_reason == "cross_episode_abandon_candidate"
                and isinstance(ctx.get("review_loop_abandon_candidate"), dict)
            )
        )
    )
    cap_hold_episode = approval_hold_provenance(task) if cap_hold_owned else None

    # Reset per-chain iteration counter on the parking task
    new_ctx = dict(ctx)
    new_review_loop = dict(review_loop)
    previous_iteration = new_review_loop.get("iteration", 0)
    new_review_loop["iteration"] = 0
    new_review_loop["iteration_reset_at"] = _utcnow().isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
    new_review_loop["iteration_reset_by"] = actor
    # The merge gate's exact-target respawn budget is a SECOND per-chain
    # counter, and it is keyed on the immutable (base_sha, head_sha) target
    # rather than on the iteration. Clearing only the iteration would leave it
    # exhausted, so the very next CI tick would re-park the PR the operator
    # just released -- with the same head, the same key, and the same count.
    # An explicit operator reset is precisely the disposition that budget was
    # escalating for, so it releases the budget too.
    #
    # What actually releases it is the ``iteration_reset_at`` stamp written
    # above: the gate derives that budget from review ROWS (a context counter
    # could not survive the ci-waiting driver row migrating), and its census
    # excludes every row created at or before the stamp -- the same closed-
    # episode window the per-PR cap query in review_pipeline already applies.
    # The three keys below are the gate's read-model of that census. They are
    # no longer authority for anything, but a stale copy of an exhausted count
    # is exactly what an operator would read next, so drop them with it.
    from app.review_merge_gate import (
        _SPAWN_BUDGET_CENSUS_KEY,
        _SPAWN_BUDGET_COUNT_KEY,
        _SPAWN_BUDGET_TARGET_KEY,
    )

    new_review_loop.pop(_SPAWN_BUDGET_COUNT_KEY, None)
    new_review_loop.pop(_SPAWN_BUDGET_TARGET_KEY, None)
    new_review_loop.pop(_SPAWN_BUDGET_CENSUS_KEY, None)
    new_ctx["review_loop"] = new_review_loop
    # An abandon-candidate hold is a recoverable episode stop. Resetting the
    # existing iteration budget explicitly starts a fresh census window; keep
    # the immutable pre-reset rows for audit, but remove the active stop marker
    # so the next orchestrator cycle can resume the PR.
    new_ctx.pop("review_loop_abandon_candidate", None)
    task.context = new_ctx
    flag_modified(task, "context")

    # Clear only an exact cap-owned episode. A newer independent hold may share
    # the boolean latch but is not authority for this operation to consume.
    was_requires_approval = cap_hold_episode is not None
    if cap_hold_episode is not None:
        task.requires_approval = False
        from app.closed_pr_reconciliation import revoke_review_wait_approval_hold

        revoke_review_wait_approval_hold(task)
        revoke_operator_approval_followup(task)
        consume_approval_hold_metadata(
            task,
            expected_episode=cap_hold_episode,
        )

    session.add(task)
    await session.commit()
    await session.refresh(task)
    logger.info(
        f"[ADMIN] Iteration cap reset on task {task.id} (PR #{pr_number}) by {actor}: "
        f"archived {archived_count} failed review/fix tasks, "
        f"per-chain iteration {previous_iteration}→0"
    )
    return {
        "task_id": str(task.id),
        "pr_number": pr_number,
        "archived_count": archived_count,
        "previous_per_chain_iteration": previous_iteration,
        "requires_approval_cleared": was_requires_approval,
    }
