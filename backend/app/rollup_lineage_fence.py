"""Keep ancestor rollups coherent when descendant work is retried or stalls."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.closed_pr_reconciliation import reset_pr_attempt_state
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

_LINEAGE_GENERATION_KEY = "_rollup_lineage_generation"
_INVALIDATION_KEY = "_rollup_invalidated_by_descendant"
_OPERATOR_BLOCKS_KEY = "_descendant_operator_blocks"
_PRIOR_APPROVAL_KEY = "_descendant_operator_blocks_prior_approval"
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _locked_ancestors(task: Task, session: AsyncSession) -> list[Task]:
    """Return the task's ancestor chain, locked nearest-first."""

    ancestors: list[Task] = []
    parent_id = task.parent_task_id
    seen = {task.id}
    while parent_id is not None:
        if parent_id in seen:
            raise HTTPException(
                status_code=409,
                detail=f"Task hierarchy cycle detected at ancestor {parent_id}",
            )
        seen.add(parent_id)
        ancestor = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id == parent_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if ancestor is None:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task.id} references missing ancestor {parent_id}",
            )
        ancestors.append(ancestor)
        parent_id = ancestor.parent_task_id
    return ancestors


def _fresh_rollup_context(
    ancestor: Task,
    *,
    descendant_id: uuid.UUID,
    generation: int,
) -> dict[str, Any]:
    """Clear attempt proof while retaining the open rollup PR coordinates."""

    context = dict(ancestor.context or {})
    old_loop = dict(context.get("review_loop") or {})
    fresh_loop = reset_pr_attempt_state(old_loop)
    for key in ("pr_number", "branch", "base_branch", "original_task_id"):
        if old_loop.get(key) is not None:
            fresh_loop[key] = old_loop[key]
    if fresh_loop:
        context["review_loop"] = fresh_loop
    else:
        context.pop("review_loop", None)

    context.pop("blueprint_review_verdict", None)
    context.pop("pr_merged", None)
    context[_LINEAGE_GENERATION_KEY] = generation
    context[_INVALIDATION_KEY] = {
        "descendant_task_id": str(descendant_id),
        "generation": generation,
        "invalidated_at": _utcnow().isoformat(),
    }
    return context


async def invalidate_ancestor_rollups_for_requeue(
    task: Task,
    session: AsyncSession,
) -> list[str]:
    """Fence stale ancestor merge proof before requeueing a terminal descendant.

    Requeueing below a completed ancestor would create work whose result can no
    longer be included in that ancestor's already-landed snapshot, so reject it.
    Open evaluating rollups retain their PR identity but lose all review/CI
    attempt proof and receive a new lineage generation.
    """

    ancestors = await _locked_ancestors(task, session)
    terminal = next(
        (ancestor for ancestor in ancestors if ancestor.status in _TERMINAL_STATUSES),
        None,
    )
    if terminal is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot requeue task {task.id} beneath terminal ancestor "
                f"{terminal.id} ({terminal.status}); requeue the ancestor first "
                "or create a follow-up task whose rollup can include the new work"
            ),
        )

    invalidated: list[str] = []
    for ancestor in ancestors:
        context = dict(ancestor.context or {})
        loop = dict(context.get("review_loop") or {})
        has_rollup_attempt = bool(
            context.get("rollup_pr_number")
            or loop.get("pr_number")
            or loop.get("ci_waiting")
        )
        if ancestor.status == TaskStatus.EVALUATING and has_rollup_attempt:
            try:
                prior_generation = int(context.get(_LINEAGE_GENERATION_KEY) or 0)
            except (TypeError, ValueError):
                prior_generation = 0
            generation = prior_generation + 1
            ancestor.context = _fresh_rollup_context(
                ancestor,
                descendant_id=task.id,
                generation=generation,
            )
            ancestor.latest_status_message = (
                f"Rollup invalidated by requeued descendant {task.id}; "
                "waiting for refreshed descendant work and a fresh review/CI pass"
            )
            invalidated.append(str(ancestor.id))

        blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
        if str(task.id) in blocks:
            cleaned = dict(ancestor.context or {})
            blocks.pop(str(task.id), None)
            if blocks:
                cleaned[_OPERATOR_BLOCKS_KEY] = blocks
                ancestor.requires_approval = True
                ancestor.latest_status_message = (
                    f"{len(blocks)} descendant task(s) still need operator "
                    "intervention"
                )
            else:
                cleaned.pop(_OPERATOR_BLOCKS_KEY, None)
                ancestor.requires_approval = bool(
                    cleaned.pop(_PRIOR_APPROVAL_KEY, False)
                )
            ancestor.context = cleaned

        session.add(ancestor)
    return invalidated


async def mark_ancestor_rollups_awaiting_operator(
    task: Task,
    session: AsyncSession,
    *,
    reason: str,
    pr_number: int | None = None,
) -> list[str]:
    """Make a terminal descendant blocker actionable on every open ancestor."""

    marked: list[str] = []
    for ancestor in await _locked_ancestors(task, session):
        if ancestor.status not in (TaskStatus.EVALUATING, TaskStatus.BLOCKED):
            continue
        context = dict(ancestor.context or {})
        blocks = dict(context.get(_OPERATOR_BLOCKS_KEY) or {})
        if not blocks:
            context[_PRIOR_APPROVAL_KEY] = bool(ancestor.requires_approval)
        blocks[str(task.id)] = {
            "task_id": str(task.id),
            "reason": reason,
            "pr_number": pr_number,
            "blocked_at": _utcnow().isoformat(),
        }
        context[_OPERATOR_BLOCKS_KEY] = blocks
        ancestor.context = context
        ancestor.requires_approval = True
        pr_label = f" for PR #{pr_number}" if pr_number else ""
        ancestor.latest_status_message = (
            f"Descendant {task.id} needs operator intervention{pr_label}: {reason}"
        )
        session.add(ancestor)
        marked.append(str(ancestor.id))
    if marked:
        # A second descendant can be marked in the same transaction. Flush the
        # first keyed blocker before its ancestor row is reloaded FOR UPDATE so
        # populate_existing cannot replace the pending JSON assignment.
        await session.flush()
    return marked
