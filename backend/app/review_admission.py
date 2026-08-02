"""Canonical admission boundary for directly launched PR reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.ci_merge_helpers import _pr_number_match
from app.models import Task
from app.review_execution_ownership import (
    is_pending_pr_continuation_owner,
    pr_execution_owner_clause,
)
from app.task_locking import _undo_projection_downgrade, acquire_pr_spawn_lock


@dataclass(frozen=True)
class ReviewAdmissionDecision:
    allowed: bool
    review_target: dict[str, str | int] | None = None
    existing_task: Task | None = None
    reason: str = ""


async def prepare_direct_review_admission(
    session: AsyncSession,
    *,
    project_id: object,
    pr_number: int,
    fetch_live_pr: Callable[[], Awaitable[dict]],
    review_task_id: object | None = None,
) -> ReviewAdmissionDecision:
    """Prove an exact open target and deduplicate its active review.

    The caller must insert and commit any admitted review in this transaction.
    Merge authority shares the project/PR lock and treats every other active
    same-PR review as a publication hold.
    """
    await acquire_pr_spawn_lock(session, project_id, pr_number)
    from app.review_repository_scope import repository_project_ids

    project_scope = await repository_project_ids(session, project_id)
    try:
        live_pr = await fetch_live_pr()
    except Exception as exc:  # noqa: BLE001
        return ReviewAdmissionDecision(False, reason=f"live PR lookup failed: {exc}")
    if (
        live_pr.get("merged") is True
        or str(live_pr.get("state") or "").lower() != "open"
    ):
        return ReviewAdmissionDecision(False, reason="PR is no longer open")

    base_sha = str((live_pr.get("base") or {}).get("sha") or "")
    head_sha = str((live_pr.get("head") or {}).get("sha") or "")
    if not base_sha or not head_sha:
        return ReviewAdmissionDecision(False, reason="PR target is unresolved")
    target: dict[str, str | int] = {
        "project_id": str(project_id),
        "pr_number": int(pr_number),
        "base_sha": base_sha,
        "head_sha": head_sha,
    }

    active_reviews = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.context,
                        Task.payloads,
                        Task.project_id,
                        Task.status,
                        Task.post_commit_pending,
                        Task.archived,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id.in_(project_scope),
                    pr_execution_owner_clause(),
                    _pr_number_match(session, pr_number),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # Do not leak this cheap projection into a caller's already-loaded Task.
    # A later refresh/expire would otherwise replay it and leave ordinary
    # synchronous attribute reads vulnerable to MissingGreenlet.
    for candidate in active_reviews:
        _undo_projection_downgrade(candidate)
    pending_owner = next(
        (
            candidate
            for candidate in active_reviews
            if is_pending_pr_continuation_owner(candidate)
        ),
        None,
    )
    if pending_owner is not None:
        pending_is_exact_review = bool(
            pending_owner.first_payload.get("type") == "review"
            and ((pending_owner.context or {}).get("review_loop") or {}).get(
                "review_target"
            )
            == target
        )
        return ReviewAdmissionDecision(
            False,
            review_target=target,
            existing_task=pending_owner if pending_is_exact_review else None,
            reason=(
                f"same-PR {pending_owner.first_payload.get('type')} "
                f"{pending_owner.id} has a pending post-commit continuation"
            ),
        )
    existing = next(
        (
            candidate
            for candidate in active_reviews
            if candidate.id != review_task_id
            and candidate.first_payload.get("type") == "review"
            and ((candidate.context or {}).get("review_loop") or {}).get(
                "review_target"
            )
            == target
        ),
        None,
    )
    if existing is not None:
        return ReviewAdmissionDecision(
            False,
            review_target=target,
            existing_task=existing,
            reason=f"exact-target review {existing.id} is already active",
        )
    return ReviewAdmissionDecision(True, review_target=target)
