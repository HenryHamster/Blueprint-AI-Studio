"""Keep ancestor rollups coherent when descendant work is retried or stalls."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    approval_hold_provenance,
    consume_approval_hold_metadata,
    establish_independent_approval_hold,
)
from app.ci_merge_helpers import effective_pr_number
from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    owns_review_wait_approval_hold,
    reset_pr_attempt_state,
)
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

_LINEAGE_GENERATION_KEY = "_rollup_lineage_generation"
_INVALIDATION_KEY = "_rollup_invalidated_by_descendant"
_OPERATOR_BLOCKS_KEY = "_descendant_operator_blocks"
_PRIOR_APPROVAL_KEY = "_descendant_operator_blocks_prior_approval"
_PRIOR_APPROVAL_STATE_KEY = "_descendant_operator_blocks_prior_approval_state"
_OPERATOR_BLOCK_HOLD_KEY = "_descendant_operator_blocks_approval_hold"
_APPROVAL_STATE_KEYS = (
    "_approval_set_at",
    "approval_hold_reason",
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    "review_loop_terminal_outcome",
)
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _locked_ancestors(task: Task, session: AsyncSession) -> list[Task]:
    """Lock the task's ancestor chain root-first; return it nearest-first.

    The unlocked discovery pass is only a receipt.  Each edge is checked again
    as its row is locked, so a concurrent topology rewrite fails closed.  Lock
    acquisition itself must run root-to-leaf: a parent cancellation owns that
    direction, and taking the immediate parent before a grandparent recreates
    the same hierarchy inversion this fence exists to prevent.
    """

    ancestry_snapshot: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    parent_id = task.parent_task_id
    seen = {task.id}
    while parent_id is not None:
        if parent_id in seen:
            raise HTTPException(
                status_code=409,
                detail=f"Task hierarchy cycle detected at ancestor {parent_id}",
            )
        seen.add(parent_id)
        ancestor_edge = (
            (
                await session.execute(
                    select(Task.id, Task.parent_task_id)
                    .where(
                        Task.id == parent_id,
                        Task.project_id == task.project_id,
                    )
                )
            )
            .first()
        )
        if ancestor_edge is None:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task.id} references missing ancestor {parent_id}",
            )
        ancestor_id, next_parent_id = ancestor_edge
        ancestry_snapshot.append((ancestor_id, next_parent_id))
        parent_id = next_parent_id

    locked_by_id: dict[uuid.UUID, Task] = {}
    for ancestor_id, expected_parent_id in reversed(ancestry_snapshot):
        ancestor = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.id == ancestor_id,
                        Task.project_id == task.project_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if ancestor is None or ancestor.parent_task_id != expected_parent_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Task {task.id} ancestry changed during retry admission; "
                    "retry the operation"
                ),
            )
        locked_by_id[ancestor.id] = ancestor

    return [locked_by_id[ancestor_id] for ancestor_id, _ in ancestry_snapshot]


async def lock_ancestor_rollups_for_requeue(
    task: Task,
    session: AsyncSession,
) -> list[Task]:
    """Pre-lock the exact ancestor chain before a PR-owned child is locked.

    Review/fix retry admission owns the repository PR lock and then locks the
    same-PR task rows.  Parent cancellation owns the opposite *hierarchy*
    direction (ancestor, then descendants), so a retry must acquire its
    ancestors before admission takes the child row or the two operations can
    deadlock.  The caller keeps this transaction open, definitively re-reads
    the child, verifies its parent edge did not change, and passes the returned
    rows back to :func:`invalidate_ancestor_rollups_for_requeue`.
    """

    return await _locked_ancestors(task, session)


def _validate_prelocked_ancestor_chain(
    task: Task,
    ancestors: list[Task],
) -> None:
    """Fail closed unless ``ancestors`` is the complete current task lineage."""

    expected_parent_id = task.parent_task_id
    seen = {task.id}
    for ancestor in ancestors:
        if (
            ancestor.project_id != task.project_id
            or ancestor.id != expected_parent_id
            or ancestor.id in seen
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Task {task.id} ancestry changed during retry admission; "
                    "retry the operation"
                ),
            )
        seen.add(ancestor.id)
        expected_parent_id = ancestor.parent_task_id
    if expected_parent_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Task {task.id} ancestry changed during retry admission; "
                "retry the operation"
            ),
        )


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


def _owns_invalidated_review_wait_hold(ancestor: Task, review_loop: dict) -> bool:
    """Whether this locked rollup's exact PR attempt owns its approval gate."""

    marker = review_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)
    if not isinstance(marker, dict):
        return False
    try:
        pr_number = int(effective_pr_number(ancestor))
    except (TypeError, ValueError):
        return False
    return owns_review_wait_approval_hold(
        ancestor,
        review_loop,
        pr_number=pr_number,
        head_sha=str(marker.get("head_sha") or ""),
        base_sha=str(marker.get("base_sha") or ""),
    )


def _capture_approval_state(task: Task) -> dict[str, Any]:
    """Snapshot only metadata replaced by an independent approval episode."""

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    if _owns_invalidated_review_wait_hold(task, review_loop):
        # This automatic episode belongs to the PR attempt that descendant
        # requeue invalidates. The descendant hold may supersede it temporarily,
        # but cleanup must not resurrect its timer without its reset marker.
        return {"requires_approval": False, "context": {}}
    return {
        "requires_approval": task.requires_approval is True,
        "context": {
            key: copy.deepcopy(context[key])
            for key in _APPROVAL_STATE_KEYS
            if key in context
        },
    }


def _restore_approval_state(task: Task, snapshot: object) -> bool:
    """Restore a retained approval episode without minting new provenance."""

    if not isinstance(snapshot, dict) or not isinstance(
        snapshot.get("context"), dict
    ):
        return False
    context = dict(task.context or {})
    for key in _APPROVAL_STATE_KEYS:
        context.pop(key, None)
    for key, value in snapshot["context"].items():
        if key in _APPROVAL_STATE_KEYS:
            context[key] = copy.deepcopy(value)
    task.context = context
    task.requires_approval = snapshot.get("requires_approval") is True
    return True


def _descendant_block_hold_receipt(task: Task, *, episode: str) -> dict[str, Any]:
    context = dict(task.context or {})
    return {
        "episode": episode,
        "approval_state": approval_hold_provenance(task),
        "provenance": copy.deepcopy(context.get(APPROVAL_HOLD_PROVENANCE_KEY)),
    }


def _owns_descendant_block_hold(task: Task, receipt: object) -> bool:
    """Whether the descendant blocker still owns the exact current episode."""

    if not isinstance(receipt, dict) or task.requires_approval is not True:
        return False
    context = dict(task.context or {})
    episode = receipt.get("episode")
    return bool(
        episode
        and context.get(APPROVAL_HOLD_EPISODE_KEY) == episode
        and context.get(APPROVAL_HOLD_PROVENANCE_KEY) == receipt.get("provenance")
        and approval_hold_provenance(task) == receipt.get("approval_state")
    )


async def invalidate_ancestor_rollups_for_requeue(
    task: Task,
    session: AsyncSession,
    *,
    prelocked_ancestors: list[Task] | None = None,
) -> list[str]:
    """Fence stale ancestor merge proof before requeueing a terminal descendant.

    Requeueing below a completed ancestor would create work whose result can no
    longer be included in that ancestor's already-landed snapshot, so reject it.
    Open evaluating rollups retain their PR identity but lose all review/CI
    attempt proof and receive a new lineage generation.
    """

    ancestors = (
        await _locked_ancestors(task, session)
        if prelocked_ancestors is None
        else list(prelocked_ancestors)
    )
    if prelocked_ancestors is not None:
        _validate_prelocked_ancestor_chain(task, ancestors)
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
            release_review_wait_hold = _owns_invalidated_review_wait_hold(
                ancestor,
                loop,
            )
            try:
                prior_generation = int(context.get(_LINEAGE_GENERATION_KEY) or 0)
            except (TypeError, ValueError):
                prior_generation = 0
            generation = prior_generation + 1
            fresh_context = _fresh_rollup_context(
                ancestor,
                descendant_id=task.id,
                generation=generation,
            )
            if release_review_wait_hold:
                # reset_pr_attempt_state already removes this attempt's exact
                # marker and review_wait_escalated latch. Ownership above also
                # proves the top-level timer belongs to the same automatic
                # gate, so invalidate the entire gate without touching an
                # independent operator/security/spend approval episode.
                fresh_context.pop("_approval_set_at", None)
                ancestor.requires_approval = False
            ancestor.context = fresh_context
            ancestor.latest_status_message = (
                f"Rollup invalidated by requeued descendant {task.id}; "
                "waiting for refreshed descendant work and a fresh review/CI pass"
            )
            invalidated.append(str(ancestor.id))

        blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
        if str(task.id) in blocks:
            cleaned = dict(ancestor.context or {})
            block_hold = copy.deepcopy(cleaned.get(_OPERATOR_BLOCK_HOLD_KEY))
            blocks.pop(str(task.id), None)
            if blocks:
                cleaned[_OPERATOR_BLOCKS_KEY] = blocks
                ancestor.context = cleaned
                ancestor.latest_status_message = (
                    f"{len(blocks)} descendant task(s) still need operator "
                    "intervention"
                )
            else:
                cleaned.pop(_OPERATOR_BLOCKS_KEY, None)
                cleaned.pop(_PRIOR_APPROVAL_KEY, None)
                prior_state = copy.deepcopy(
                    cleaned.pop(_PRIOR_APPROVAL_STATE_KEY, None)
                )
                cleaned.pop(_OPERATOR_BLOCK_HOLD_KEY, None)
                ancestor.context = cleaned
                prior_state_is_restorable = isinstance(
                    prior_state, dict
                ) and isinstance(prior_state.get("context"), dict)
                if (
                    prior_state_is_restorable
                    and _owns_descendant_block_hold(ancestor, block_hold)
                ):
                    consumed = consume_approval_hold_metadata(
                        ancestor,
                        expected_episode=block_hold.get("approval_state"),
                    )
                    if consumed:
                        _restore_approval_state(ancestor, prior_state)

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
        first_block = not blocks
        if first_block:
            context[_PRIOR_APPROVAL_KEY] = bool(ancestor.requires_approval)
            context[_PRIOR_APPROVAL_STATE_KEY] = _capture_approval_state(ancestor)
        blocks[str(task.id)] = {
            "task_id": str(task.id),
            "reason": reason,
            "pr_number": pr_number,
            "blocked_at": _utcnow().isoformat(),
        }
        context[_OPERATOR_BLOCKS_KEY] = blocks
        ancestor.context = context
        if first_block:
            episode = establish_independent_approval_hold(
                ancestor,
                reason=f"descendant {task.id} needs operator intervention",
                owner="rollup_lineage_fence",
            )
            held_context = dict(ancestor.context or {})
            held_context[_OPERATOR_BLOCK_HOLD_KEY] = _descendant_block_hold_receipt(
                ancestor,
                episode=episode,
            )
            ancestor.context = held_context
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
