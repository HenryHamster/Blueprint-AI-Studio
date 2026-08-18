"""Exact approval-gate authority for review-wait backstop spawn claims."""

from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    owns_review_wait_approval_hold,
    revoke_review_wait_approval_hold,
)
from app.models import Task


def review_wait_spawn_claim_gate_is_current(
    task: Task,
    claim: dict[str, object],
    *,
    review_loop: dict,
) -> bool:
    """Accept either an open gate or the exact automatic gate in the claim."""

    claimed_marker = claim.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)
    if claimed_marker is None:
        return task.requires_approval is not True
    if (
        task.requires_approval is not True
        or not isinstance(claimed_marker, dict)
        or review_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY) != claimed_marker
    ):
        return False
    return owns_review_wait_approval_hold(
        task,
        review_loop,
        pr_number=int(claim.get("pr_number") or 0),
        head_sha=str(claim.get("head_sha") or ""),
        base_sha=str(claim.get("base_sha") or ""),
    )


def release_review_wait_gate_owned_by_spawn_claim(
    task: Task,
    claim: dict[str, object],
) -> bool:
    """Release only the exact automatic gate captured by this spawn claim."""

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    claimed_marker = claim.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)
    if (
        not isinstance(claimed_marker, dict)
        or review_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY) != claimed_marker
        or not owns_review_wait_approval_hold(
            task,
            review_loop,
            pr_number=int(claim.get("pr_number") or 0),
            head_sha=str(claim.get("head_sha") or ""),
            base_sha=str(claim.get("base_sha") or ""),
        )
    ):
        return False
    if not revoke_review_wait_approval_hold(task):
        return False
    task.requires_approval = False
    return True


def release_review_wait_gate_owned_by_live_target(
    task: Task,
    *,
    pr_number: int,
    head_sha: str,
    base_sha: str,
) -> bool:
    """Release the exact automatic gate after a fresh live-target proof."""

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    if not owns_review_wait_approval_hold(
        task,
        review_loop,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
    ) or not revoke_review_wait_approval_hold(task):
        return False
    task.requires_approval = False
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop["ci_waiting"] = False
    review_loop.pop("review_wait_started", None)
    review_loop.pop("review_wait_deferred_at", None)
    review_loop.pop("review_wait_escalated", None)
    task.context = {**context, "review_loop": review_loop}
    return True


def release_completed_review_wait_gate_owned_by_spawn_claim(
    task: Task,
    claim: dict[str, object],
) -> bool:
    """Consume an exact automatic gate after canonical merged completion."""

    if (task.context or {}).get("pr_merged") is not True:
        return False
    if not release_review_wait_gate_owned_by_spawn_claim(task, claim):
        return False
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop["ci_waiting"] = False
    review_loop.pop("review_wait_started", None)
    review_loop.pop("review_wait_deferred_at", None)
    review_loop.pop("review_wait_escalated", None)
    task.context = {**context, "review_loop": review_loop}
    return True
