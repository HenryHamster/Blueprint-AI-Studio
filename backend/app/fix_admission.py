"""Canonical transaction boundary for direct code-fix admission."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.ci_merge_helpers import (
    _pr_number_match,
    revoke_review_authority_for_active_fix,
)
from app.models import Task
from app.review_execution_ownership import (
    is_pending_pr_continuation_owner,
    is_pr_execution_owner,
)
from app.task_locking import _undo_projection_downgrade, acquire_pr_spawn_lock


class FixAdmissionDenial(str, Enum):
    """Why a direct fix was denied and how its caller should respond."""

    TERMINAL = "terminal"
    RETRYABLE = "retryable"
    OCCUPIED = "occupied"


@dataclass(frozen=True)
class FixAdmissionDecision:
    allowed: bool
    reason: str = ""
    fix_target: dict[str, str | int] | None = None
    denial: FixAdmissionDenial | None = None


async def prepare_direct_fix_admission(
    session: AsyncSession,
    *,
    project_id: object,
    pr_number: int,
    fix_task_id: object,
    fetch_live_pr: Callable[[], Awaitable[dict]] | None = None,
) -> FixAdmissionDecision:
    """Lock one project PR, prove it remains open, and revoke merge authority.

    The caller must insert the fix and commit in this same transaction. Merge
    authority takes the same lock through its final PUT, so a direct writer
    cannot become visible between the active-fix query and merge submission.
    """
    await acquire_pr_spawn_lock(session, project_id, pr_number)
    from app.review_repository_scope import repository_project_ids

    project_scope = await repository_project_ids(session, project_id)

    if fetch_live_pr is None:
        return FixAdmissionDecision(
            False,
            "live PR lookup is required",
            denial=FixAdmissionDenial.RETRYABLE,
        )
    try:
        live_pr = await fetch_live_pr()
    except Exception as exc:  # noqa: BLE001
        return FixAdmissionDecision(
            False,
            f"live PR lookup failed: {exc}",
            denial=FixAdmissionDenial.RETRYABLE,
        )
    state = str(live_pr.get("state") or "").lower()
    if live_pr.get("merged") is True or state in {"closed", "merged"}:
        return FixAdmissionDecision(
            False,
            "PR is no longer open",
            denial=FixAdmissionDenial.TERMINAL,
        )
    if state != "open":
        return FixAdmissionDecision(
            False,
            "live PR state is unresolved",
            denial=FixAdmissionDenial.RETRYABLE,
        )
    base_sha = str((live_pr.get("base") or {}).get("sha") or "")
    head_sha = str((live_pr.get("head") or {}).get("sha") or "")
    if not base_sha or not head_sha:
        return FixAdmissionDecision(
            False,
            "PR target is unresolved",
            denial=FixAdmissionDenial.RETRYABLE,
        )
    fix_target: dict[str, str | int] = {
        "project_id": str(project_id),
        "pr_number": int(pr_number),
        "base_sha": base_sha,
        "head_sha": head_sha,
    }

    chain_tasks = (
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
                    _pr_number_match(session, pr_number),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # populate_existing + load_only can permanently narrow an instance that
    # was already fully loaded in this shared session. Restore its prior loader
    # contract before returning to callers that may later refresh/expire it.
    for candidate in chain_tasks:
        _undo_projection_downgrade(candidate)
    if any(
        (candidate.context or {}).get("pr_merged")
        or (candidate.context or {}).get("pr_closed_unmerged")
        for candidate in chain_tasks
    ):
        return FixAdmissionDecision(
            False,
            "live PR is open but the task chain carries stale terminal state",
            denial=FixAdmissionDenial.RETRYABLE,
        )

    pending_owner = next(
        (
            candidate
            for candidate in chain_tasks
            if is_pending_pr_continuation_owner(candidate)
        ),
        None,
    )
    if pending_owner is not None:
        return FixAdmissionDecision(
            False,
            f"same-PR {pending_owner.first_payload.get('type')} "
            f"{pending_owner.id} has a pending post-commit continuation",
            denial=FixAdmissionDenial.OCCUPIED,
        )

    # The PR advisory lock makes this a real admission decision rather than a
    # best-effort preflight: only one same-PR code writer may be active.  The
    # active idempotency index cannot provide this guarantee because direct
    # launchers use different keys (or no key at all).
    active_fix = next(
        (
            candidate
            for candidate in chain_tasks
            if candidate.id != fix_task_id
            and is_pr_execution_owner(candidate)
            and candidate.first_payload.get("type") == "fix"
        ),
        None,
    )
    if active_fix is not None:
        return FixAdmissionDecision(
            False,
            f"same-PR fix {active_fix.id} already owns execution",
            denial=FixAdmissionDenial.OCCUPIED,
        )

    for candidate in chain_tasks:
        review_loop = (candidate.context or {}).get("review_loop")
        if isinstance(review_loop, dict):
            revoke_review_authority_for_active_fix(candidate, fix_task_id)
            session.add(candidate)
    return FixAdmissionDecision(True, fix_target=fix_target)
