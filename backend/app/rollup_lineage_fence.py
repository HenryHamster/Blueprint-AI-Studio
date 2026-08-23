"""Keep ancestor rollups coherent when descendant work is retried or stalls."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)

_LINEAGE_GENERATION_KEY = "_rollup_lineage_generation"
_INVALIDATION_KEY = "_rollup_invalidated_by_descendant"
_OPERATOR_BLOCKS_KEY = "_descendant_operator_blocks"
_PRIOR_APPROVAL_KEY = "_descendant_operator_blocks_prior_approval"
_PRIOR_APPROVAL_STATE_KEY = "_descendant_operator_blocks_prior_approval_state"
_OPERATOR_BLOCK_HOLD_KEY = "_descendant_operator_blocks_approval_hold"
_DECIDED_BLOCKS_KEY = "_descendant_operator_blocks_decided"
_FENCE_HOLD_OWNER = "rollup_lineage_fence"
# How long a descendant block may keep an ancestor rollup parked once every
# blocker is TERMINAL and its state is fully known.  Past this window the
# fence DECIDES — it releases its own hold, records the still-unresolved
# descendants on the ancestor and lets the rollup resume — instead of parking
# forever or handing the question to a human.  A fence hold is machine-set:
# nothing about it becomes true by waiting longer than this.
_FENCE_BLOCK_DECIDE_AFTER_HOURS = 12
# Tri-state classification of one keyed descendant block.  ``unknown`` is the
# conservative state: it is NEVER progress and never permits a decision.
_BLOCK_RESOLVED = "resolved"
_BLOCK_OPEN = "open"
_BLOCK_UNKNOWN = "unknown"
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

        _release_descendant_block_entries(ancestor, [str(task.id)])

        session.add(ancestor)
    return invalidated


def _release_descendant_block_entries(
    ancestor: Task, resolved_ids: list[str]
) -> bool:
    """Drop ``resolved_ids`` from the ancestor's keyed descendant blocks.

    When the LAST block clears, the fence's approval hold is consumed (with
    ownership verification) and the prior approval state restored — identical
    semantics for the requeue path and the resolved-subject release path.
    Returns ``True`` iff every block is now gone and the hold was released.
    Callers own ``session.add`` / commit.
    """
    blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
    to_drop = [rid for rid in resolved_ids if rid in blocks]
    if not to_drop:
        return False
    cleaned = dict(ancestor.context or {})
    block_hold = copy.deepcopy(cleaned.get(_OPERATOR_BLOCK_HOLD_KEY))
    for rid in to_drop:
        blocks.pop(rid, None)
    if blocks:
        cleaned[_OPERATOR_BLOCKS_KEY] = blocks
        ancestor.context = cleaned
        ancestor.latest_status_message = (
            f"{len(blocks)} descendant task(s) still need operator "
            "intervention"
        )
        return False
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
    return True


def _parse_block_timestamp(raw: object) -> datetime | None:
    """Parse a recorded block timestamp into naive UTC, or ``None``."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _block_label(task_id: str, pr_number: object) -> str:
    """Short operator-readable identity for one keyed descendant block."""

    label = str(task_id)[:8]
    return f"{label} (PR #{pr_number})" if pr_number else label


def descendant_operator_block_hold_is_owned(ancestor: Task) -> bool:
    """Whether this exact row provably carries a LIVE fence-owned hold.

    Ownership is structural and must never be inferred from the hold's reason
    text: an operator/security/spend episode can leave the same latch TRUE.
    All three facts must agree —

      * a non-empty keyed ``_descendant_operator_blocks`` map,
      * the exact hold receipt this fence stamped when it took the latch
        (:func:`_owns_descendant_block_hold` re-derives the live episode), and
      * ``_approval_hold_provenance.owner == "rollup_lineage_fence"``.

    Any mismatch returns ``False`` so callers fall through to whatever they
    did before the fence existed.
    """

    context = dict(ancestor.context or {})
    blocks = context.get(_OPERATOR_BLOCKS_KEY)
    if not isinstance(blocks, dict) or not blocks:
        return False
    if not _owns_descendant_block_hold(
        ancestor, context.get(_OPERATOR_BLOCK_HOLD_KEY)
    ):
        return False
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    return (
        isinstance(provenance, dict)
        and provenance.get("owner") == _FENCE_HOLD_OWNER
    )


async def _classify_descendant_blocks(
    ancestor: Task,
    session: AsyncSession,
) -> dict[str, str]:
    """Read every keyed descendant block's live state in ONE lookup pass.

    Returns a ``block_id -> state`` map:

      * ``resolved`` — the subject PR merged or closed, or the PR-less flagged
        descendant is COMPLETED;
      * ``open`` — provably still outstanding;
      * ``unknown`` — the state could not be read at all (no GitHub
        credentials, a lookup error or timeout, a missing task row, an
        unparseable entry).

    ``unknown`` is never progress. Both callers — the release path and the
    adjudication path — must treat it as "keep the hold", which is why they
    share this one function rather than each running their own GitHub
    round-trips and risking a disagreement about the same PR.
    """

    blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
    if not blocks:
        return {}

    from app.github import get_pull_request
    from app.routers.agent import get_github_token_for_task

    gh_auth: Any = None
    pr_state_cache: dict[int, dict | None] = {}

    async def _pr_state(pr_number: int) -> str:
        nonlocal gh_auth
        if pr_number in pr_state_cache:
            pr = pr_state_cache[pr_number]
        else:
            if gh_auth is None:
                try:
                    gh_auth = await get_github_token_for_task(
                        ancestor.id, session
                    )
                except Exception:  # noqa: BLE001 — config failure: keep hold
                    gh_auth = ()
            if not gh_auth:
                pr_state_cache[pr_number] = None
                return _BLOCK_UNKNOWN
            gh_token, owner, repo = gh_auth
            try:
                pr = await asyncio.wait_for(
                    get_pull_request(
                        gh_token,
                        owner,
                        repo,
                        pr_number,
                        project_id=str(ancestor.project_id),
                    ),
                    timeout=30,
                )
            except Exception:  # noqa: BLE001 — unknown state: keep hold
                pr = None
            pr_state_cache[pr_number] = pr
        if not isinstance(pr, dict):
            return _BLOCK_UNKNOWN
        if bool(pr.get("merged")) or str(pr.get("state")) == "closed":
            return _BLOCK_RESOLVED
        return _BLOCK_OPEN

    states: dict[str, str] = {}
    for block_id, entry in blocks.items():
        entry = entry if isinstance(entry, dict) else {}
        pr_number = entry.get("pr_number")
        if pr_number:
            try:
                states[block_id] = await _pr_state(int(pr_number))
            except (TypeError, ValueError):
                states[block_id] = _BLOCK_UNKNOWN
            continue
        try:
            block_uuid = uuid.UUID(str(entry.get("task_id") or block_id))
        except (TypeError, ValueError):
            states[block_id] = _BLOCK_UNKNOWN
            continue
        descendant = await session.get(Task, block_uuid)
        if descendant is None:
            states[block_id] = _BLOCK_UNKNOWN
        elif descendant.status == TaskStatus.COMPLETED:
            states[block_id] = _BLOCK_RESOLVED
        else:
            states[block_id] = _BLOCK_OPEN
    return states


def _stamp_out_of_band_release(ancestor: Task, resolved_count: int) -> None:
    """Record the single out-of-band release receipt both paths emit."""

    ancestor.latest_status_message = (
        "Descendant operator block(s) resolved out-of-band "
        "(PR merged/closed or descendant completed) — released "
        "the fence hold and restored prior approval state"
    )
    logger.info(
        "[LINEAGE FENCE] Released %d resolved descendant block(s) on "
        "ancestor %s — hold cleared without operator requeue",
        resolved_count,
        str(ancestor.id)[:8],
    )


async def release_resolved_descendant_operator_blocks(
    ancestor: Task,
    session: AsyncSession,
) -> bool:
    """Release fence blocks whose subjects have since resolved on their own.

    ``mark_ancestor_rollups_awaiting_operator`` had exactly ONE release path:
    an operator requeueing the flagged descendant
    (``invalidate_ancestor_rollups_for_requeue``). When the flagged blocker
    instead resolved out-of-band — its subject PR merged or closed, or the
    PR-less descendant completed — the ancestor's approval hold persisted
    forever and the approval-gate timeout killed the row (incident
    2026-08-19: PR #17419 merged at 02:15Z, yet ancestor 289809a3 was FAILED
    by the 2h timeout at 02:18Z, and its requeued clone re-inherited the
    stale hold).

    A block entry is treated as resolved when its recorded ``pr_number`` is
    MERGED or CLOSED on GitHub, or — entries without a PR — when the flagged
    descendant task is COMPLETED. Every lookup failure is conservative: the
    entry stays and the hold is kept. Returns ``True`` iff every block
    resolved and the hold was released (prior approval state restored).
    Mutates + ``session.add``s the ancestor; the caller owns the commit.
    """
    blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
    if not blocks:
        return False
    states = await _classify_descendant_blocks(ancestor, session)
    resolved = [
        block_id
        for block_id, state in states.items()
        if state == _BLOCK_RESOLVED
    ]
    if not resolved:
        return False
    released = _release_descendant_block_entries(ancestor, resolved)
    if released:
        _stamp_out_of_band_release(ancestor, len(resolved))
    session.add(ancestor)
    return released


def _waiting_status_message(blocks: dict[str, Any], *, now: datetime) -> str:
    """Name every unresolved block so the wait states its own reason."""

    parts: list[str] = []
    for block_id, raw_entry in sorted(blocks.items()):
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        blocked_at = _parse_block_timestamp(entry.get("blocked_at"))
        if blocked_at is None:
            age = "blocked for an unknown duration"
        else:
            elapsed = max((now - blocked_at).total_seconds(), 0.0)
            age = f"blocked {int(elapsed // 3600)}h"
        label = _block_label(
            str(entry.get("task_id") or block_id), entry.get("pr_number")
        )
        reason = str(entry.get("reason") or "no reason recorded")
        parts.append(f"{label} — {reason} ({age})")
    return (
        "Waiting on descendant operator block(s); this rollup is NOT failed "
        "and its approval hold is machine-owned, not a question for an "
        "operator: " + "; ".join(parts)
    )


def _decide_descendant_operator_blocks(
    ancestor: Task,
    blocks: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Release an aged-out fence hold and record WHY on the ancestor."""

    decided_at = now.isoformat()
    why = (
        "every blocking descendant is terminal and the block aged past "
        f"{_FENCE_BLOCK_DECIDE_AFTER_HOURS}h with no operator intervention "
        "— the rollup resumes and reports this descendant as unresolved"
    )
    records: list[dict[str, Any]] = []
    for block_id, raw_entry in sorted(blocks.items()):
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        records.append(
            {
                "task_id": str(entry.get("task_id") or block_id),
                "pr_number": entry.get("pr_number"),
                "reason": entry.get("reason"),
                "blocked_at": entry.get("blocked_at"),
                "decided_at": decided_at,
                "why": why,
            }
        )
    _release_descendant_block_entries(ancestor, list(blocks))
    context = dict(ancestor.context or {})
    prior = context.get(_DECIDED_BLOCKS_KEY)
    context[_DECIDED_BLOCKS_KEY] = (
        list(prior) if isinstance(prior, list) else []
    ) + records
    ancestor.context = context
    labels = ", ".join(
        _block_label(record["task_id"], record["pr_number"])
        for record in records
    )
    ancestor.latest_status_message = (
        f"Descendant operator block(s) still unresolved after "
        f"{_FENCE_BLOCK_DECIDE_AFTER_HOURS}h with every blocker terminal "
        f"— decided: released the fence hold and resumed the rollup, "
        f"reporting {labels} as unresolved"
    )
    logger.info(
        "[LINEAGE FENCE] Decided %d aged-out descendant block(s) on ancestor "
        "%s after %dh — hold released, rollup resumed, unresolved "
        "descendants recorded at %s",
        len(records),
        str(ancestor.id)[:8],
        _FENCE_BLOCK_DECIDE_AFTER_HOURS,
        _DECIDED_BLOCKS_KEY,
    )
    return records


async def adjudicate_descendant_operator_blocks(
    ancestor: Task,
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> str:
    """Own the whole lifecycle of a MACHINE-set descendant-block hold.

    ``mark_ancestor_rollups_awaiting_operator`` establishes an approval hold
    that is *not* a question posed to a human about this ancestor: it encodes
    a dependency on a descendant with its own lifecycle. Running the generic
    2h human-approval clock against it FAILS the ancestor while the blocker
    is still moving — which resolves nothing and destroys the rollup
    (incident 2026-08-23: research 70c5ec8f was FAILED at ~03:42Z for a fence
    hold set at 01:42Z, and the blocking descendant's PR #17693 merged at
    12:11Z, 8.5h after the ancestor was already dead).

    The fence therefore adjudicates itself, and returns which disposition it
    took:

      * ``"not_fenced"`` — this row does not provably carry a fence hold
        (:func:`descendant_operator_block_hold_is_owned`); the caller MUST
        leave every other disposition exactly as it was.
      * ``"released"`` — every block resolved on its own (PR merged or
        closed, or the PR-less descendant completed); the hold is handed back
        and the prior approval state restored, identically to the requeue
        path.
      * ``"decided"`` — every blocker is TERMINAL, every state is known,
        and the oldest block has aged past
        ``_FENCE_BLOCK_DECIDE_AFTER_HOURS``. Waiting longer cannot make a
        machine-set hold truer, so the fence releases it, records the
        still-unresolved descendants under
        ``_descendant_operator_blocks_decided`` for the rollup to report, and
        lets the ancestor resume normal evaluation.
      * ``"waiting"`` — anything else, including EVERY unreadable state.
        The hold stays, the status message names each unresolved block, and
        the ancestor is never failed.

    Unknown is never progress: a GitHub lookup that errors or times out, a
    missing task row, or an unparseable ``blocked_at`` all keep the hold and
    return ``"waiting"`` — never ``"decided"``, and never a failure.
    Mutates and ``session.add``s the ancestor; the caller owns the commit.
    """

    if not descendant_operator_block_hold_is_owned(ancestor):
        return "not_fenced"
    if not isinstance(now, datetime):
        now = _utcnow()
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)

    states = await _classify_descendant_blocks(ancestor, session)
    resolved = [
        block_id
        for block_id, state in states.items()
        if state == _BLOCK_RESOLVED
    ]
    if resolved and _release_descendant_block_entries(ancestor, resolved):
        _stamp_out_of_band_release(ancestor, len(resolved))
        session.add(ancestor)
        return "released"

    blocks = dict((ancestor.context or {}).get(_OPERATOR_BLOCKS_KEY) or {})
    if not blocks:
        # A partial release emptied the map without handing the latch back
        # (a newer independent episode owns it). Nothing left to adjudicate.
        session.add(ancestor)
        return "waiting"

    # A decision needs COMPLETE knowledge. Any unreadable state, any missing
    # or non-terminal descendant row, any unparseable timestamp keeps the
    # hold — the fence waits rather than guessing.
    decidable = all(
        states.get(block_id, _BLOCK_UNKNOWN) != _BLOCK_UNKNOWN
        for block_id in blocks
    )
    oldest: datetime | None = None
    for block_id, raw_entry in blocks.items():
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        blocked_at = _parse_block_timestamp(entry.get("blocked_at"))
        if blocked_at is None:
            decidable = False
        elif oldest is None or blocked_at < oldest:
            oldest = blocked_at
        try:
            block_uuid = uuid.UUID(str(entry.get("task_id") or block_id))
        except (TypeError, ValueError):
            decidable = False
            continue
        descendant = await session.get(Task, block_uuid)
        if descendant is None or descendant.status not in _TERMINAL_STATUSES:
            decidable = False

    if (
        decidable
        and oldest is not None
        and (now - oldest).total_seconds()
        >= _FENCE_BLOCK_DECIDE_AFTER_HOURS * 3600
    ):
        _decide_descendant_operator_blocks(ancestor, blocks, now=now)
        session.add(ancestor)
        return "decided"

    message = _waiting_status_message(blocks, now=now)
    if ancestor.latest_status_message != message:
        ancestor.latest_status_message = message
    session.add(ancestor)
    return "waiting"


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
                owner=_FENCE_HOLD_OWNER,
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
