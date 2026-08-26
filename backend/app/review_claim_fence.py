"""Execution-time safety fence for every PR review and fix task.

Producer admission keeps duplicate work cheap.  This boundary is the stronger
structural guarantee: regardless of how a queued row was created or revived,
it cannot start executing while publication owns the same repository/PR lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.ci_merge_helpers import (
    _pr_number_match,
    revoke_review_authority_for_active_fix,
)
from app.closed_pr_reconciliation import append_superseded_pr_number
from app.github import get_open_pull_request_for_head_branch, get_pull_request
from app.github_token_resolver import get_github_token_for_task
from app.models import Task, TaskStatus, _utcnow
from app.review_execution_ownership import (
    has_pending_pr_continuation,
    is_pending_pr_continuation_owner,
    is_pr_execution_owner,
    pr_execution_owner_priority,
)
from app.review_repository_scope import repository_project_ids
from app.task_locking import acquire_pr_spawn_lock


@dataclass(frozen=True)
class PrClaimFenceDecision:
    applies: bool
    allowed: bool
    task: Task | None = None
    reason: str = ""
    retryable: bool = False


def _pr_task_identity(task: Task) -> tuple[str, int] | None:
    payload = task.first_payload
    task_type = str(payload.get("type") or "").lower()
    if task_type not in {"review", "fix"}:
        return None
    try:
        pr_number = int(payload.get("pr_number") or 0)
    except (TypeError, ValueError):
        return None
    return (task_type, pr_number) if pr_number > 0 else None


def _live_target(
    task: Task,
    pr_number: int,
    live_pr: dict,
) -> dict[str, str | int] | None:
    base_sha = str((live_pr.get("base") or {}).get("sha") or "")
    head_sha = str((live_pr.get("head") or {}).get("sha") or "")
    if not base_sha or not head_sha:
        return None
    return {
        "project_id": str(task.project_id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def _cancel_pr_task(task: Task, reason: str) -> None:
    """Terminalize ``task`` CANCELLED because the fence denied its claim.

    Every one of this helper's call sites means the same thing: this row must
    not execute, because either another row canonically owns the PR or the PR
    target it was assigned to no longer exists. That is ``ADMIN_CANCEL`` by the
    class's own definition ("orchestration path deliberately terminated the task
    before it ran meaningful work ... or system supersession"), and it is why
    the fence is safe to treat as a non-death: the work either continues under
    the canonical owner or has become moot.

    The stamp is load-bearing rather than cosmetic. Before it existed, all eight
    denial sites left a CANCELLED row whose only reason lived under the bespoke
    ``pr_execution_fence`` key, which the ground-truth reader
    (``platform_kill_metrics``) does not consult — so a fence denial was
    indistinguishable from a terminal row nobody bothered to explain. The
    specific denial sentence stays in ``pr_execution_fence['reason']`` so an
    operator gets the identity of the winning claimant, not just the bucket.
    """
    from app.failure_classification import (
        ADMIN_CANCEL,
        PR_EXECUTION_FENCE_DENIED_DETAIL,
        persist_failure_category,
    )

    context = dict(task.context or {})
    context["pr_execution_fence"] = {
        "allowed": False,
        "reason": reason,
        "checked_at": _utcnow().isoformat(),
    }
    context["failure_detail"] = PR_EXECUTION_FENCE_DENIED_DETAIL
    task.context = context
    task.status = TaskStatus.CANCELLED
    task.completed_at = _utcnow()
    task.latest_status_message = f"Cancelled by PR execution fence: {reason}"[:500]
    persist_failure_category(task, ADMIN_CANCEL)
    flag_modified(task, "context")


def _is_rollup_fix(task: Task, pr_number: int) -> bool:
    """Return whether *task* is a child fix for the tracked rollup PR."""

    context = task.context or {}
    review_loop = context.get("review_loop") or {}
    if (
        context.get("_is_rollup_review") is True
        or context.get("_rollup_retries") is not None
    ):
        return True
    try:
        rollup_pr = int(context.get("rollup_pr_number") or 0)
        loop_pr = int(review_loop.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    return rollup_pr == pr_number and loop_pr == pr_number


async def _retarget_closed_rollup_fix(
    task: Task,
    *,
    pr_number: int,
    closed_pr: dict,
    github_token: str,
    owner: str,
    repo: str,
) -> tuple[str, dict[str, str | int] | None]:
    """Rebind a rollup fix to its authoritative replacement PR, if present.

    A replacement is safe to adopt only when GitHub identifies one open PR for
    the same head branch and base branch.  A changed head on an otherwise-open
    PR, or an ambiguous/missing replacement, remains a hard stale-target
    failure below; this helper never turns arbitrary head drift into admission.
    """

    if not _is_rollup_fix(task, pr_number):
        return "not_applicable", None
    review_loop = dict((task.context or {}).get("review_loop") or {})
    head_ref = str(
        review_loop.get("branch")
        or (closed_pr.get("head") or {}).get("ref")
        or ""
    )
    base_ref = str(
        review_loop.get("base_branch")
        or (closed_pr.get("base") or {}).get("ref")
        or ""
    )
    if not head_ref or not base_ref:
        return "not_found", None
    try:
        replacement = await get_open_pull_request_for_head_branch(
            github_token,
            owner,
            repo,
            head_ref,
            base_ref,
            project_id=str(task.project_id),
        )
    except Exception as exc:  # noqa: BLE001 - execution must retry, not guess
        return f"lookup_failed: {exc}", None
    if not isinstance(replacement, dict):
        return "not_found", None
    try:
        replacement_number = int(replacement.get("number") or 0)
    except (TypeError, ValueError):
        replacement_number = 0
    if replacement_number <= 0 or replacement_number == pr_number:
        return "not_found", None
    if str(replacement.get("state") or "").lower() != "open" or replacement.get(
        "merged"
    ) is True:
        return "not_found", None
    replacement_target = _live_target(task, replacement_number, replacement)
    if replacement_target is None:
        return "unresolved", None

    payloads = list(task.payloads or [])
    if not payloads or not isinstance(payloads[0], dict):
        return "unresolved", None
    payloads[0] = {**payloads[0], "pr_number": replacement_number}
    task.payloads = payloads
    flag_modified(task, "payloads")

    # Keep a compact, semantic lineage of prior PRs.  The exact-head receipt
    # below protects this *current* task, but the next review/fix task rebuilds
    # ``review_loop`` and must retain the fact that this chain crossed PRs for
    # durable convergence reporting.
    superseded_pr_numbers = append_superseded_pr_number(review_loop, pr_number)

    replacement_loop = dict(review_loop)
    replacement_loop.update(
        {
            "pr_number": replacement_number,
            "branch": str(
                (replacement.get("head") or {}).get("ref") or head_ref
            ),
            "base_branch": str(
                (replacement.get("base") or {}).get("ref") or base_ref
            ),
            "fix_target": replacement_target,
            # This is a backend-issued exact target for the same fix task. It
            # prevents the old head from authorizing writes on the replacement
            # PR while leaving ``reviewed_head_sha`` untouched.  That field is
            # the historical review boundary used by the fix-completion handoff
            # to scope the next review; replacing it with the new live head
            # would silently review only the fix and skip replacement content.
            "head_drift_retargeted_from": {
                "pr_number": pr_number,
                "head_sha": str((closed_pr.get("head") or {}).get("sha") or ""),
            },
            "superseded_pr_numbers": superseded_pr_numbers,
        }
    )
    replacement_context = {**(task.context or {}), "review_loop": replacement_loop}
    if replacement_context.get("rollup_pr_number") == pr_number:
        replacement_context["rollup_pr_number"] = replacement_number
    if replacement.get("html_url"):
        replacement_context["rollup_pr_url"] = replacement["html_url"]
    task.context = replacement_context
    task.latest_status_message = (
        f"Retargeted rollup fix from closed PR #{pr_number} to open replacement "
        f"PR #{replacement_number}; review history and iteration state preserved"
    )[:500]
    flag_modified(task, "context")
    return "retargeted", replacement_target


def _target_matches_live(
    assigned: object,
    *,
    pr_number: int,
    live_target: dict[str, str | int],
) -> bool:
    """Validate the immutable PR/head identity of an assigned target.

    The base branch tip is ambient while review or fix work is being claimed:
    unrelated merges may move it without changing the code under review.  It
    is refreshed by the caller before the task is persisted and remains part
    of the target for the final merge-safety check.

    ``project_id`` is deliberately NOT compared.  This predicate is applied to
    sibling candidates drawn from ``chain_tasks``, which spans every project
    ``repository_project_ids`` returns for the same owner/repo — a duplicate
    registered under a second project carries its own ``project_id``.  Testing
    it against the fenced task's ``live_target`` would drop those siblings from
    the contender sets, letting each duplicate elect itself canonical and run a
    second concurrent review/fix on the same PR.  ``pr_number`` + ``head_sha``,
    within a scope already restricted to one repository, pin the PR identity.
    """
    if not isinstance(assigned, dict):
        return False
    try:
        assigned_pr = int(assigned.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    assigned_head = str(assigned.get("head_sha") or "")
    # Base SHA is deliberately not part of admission identity: it is the
    # ambient integration-branch tip and may advance under an unchanged PR.
    # The final merge-authority boundary is responsible for the strict
    # base+head comparison immediately before submission.
    return bool(
        assigned_pr == pr_number
        and assigned_head
        and assigned_head == live_target["head_sha"]
    )


def _fix_task_matches_live(
    task: Task,
    *,
    pr_number: int,
    live_target: dict[str, str | int],
) -> bool:
    loop = (task.context or {}).get("review_loop") or {}
    assigned = loop.get("fix_target")
    if assigned is not None:
        return _target_matches_live(
            assigned,
            pr_number=pr_number,
            live_target=live_target,
        )
    reviewed_head = str(loop.get("reviewed_head_sha") or "")
    return bool(reviewed_head and reviewed_head == live_target["head_sha"])


async def lock_pr_task_for_execution(
    session: AsyncSession,
    *,
    task_id: object,
    claimable_statuses: Iterable[TaskStatus],
    expected_vm_instance_id: str | None = None,
) -> PrClaimFenceDecision:
    """Lock and validate a PR task before it may become executable.

    The initial row is deliberately an unlocked identity peek.  The canonical
    repository/PR advisory lock is acquired before the definitive Task row
    lock, matching merge authority's PR-before-Task order.
    """
    claimable = tuple(claimable_statuses)
    peek = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.project_id,
                        Task.payloads,
                        Task.status,
                        Task.vm_instance_id,
                        raiseload=True,
                    )
                )
                .where(Task.id == task_id)
            )
        )
        .scalars()
        .first()
    )
    if peek is None or peek.status not in claimable:
        return PrClaimFenceDecision(False, False)
    identity = _pr_task_identity(peek)
    if identity is None:
        return PrClaimFenceDecision(False, False)
    task_type, pr_number = identity

    try:
        gh_token, owner, repo = await get_github_token_for_task(task_id, session)
    except Exception as exc:  # noqa: BLE001
        return PrClaimFenceDecision(
            True,
            False,
            reason=f"repository credentials unavailable: {exc}",
            retryable=True,
        )

    await acquire_pr_spawn_lock(
        session,
        peek.project_id,
        pr_number,
        repo_identity=f"{owner}/{repo}",
    )
    project_scope = await repository_project_ids(
        session,
        peek.project_id,
        owner=owner,
        repo=repo,
    )
    # Lock the complete same-repository/PR chain in one deterministic order.
    # Locking the selected task first and then a lower-id parent/sibling would
    # reintroduce a Task/Task deadlock even though PR admission is serialized.
    chain_tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id.in_(project_scope),
                    _pr_number_match(session, pr_number),
                )
                .order_by(Task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    task = next((candidate for candidate in chain_tasks if candidate.id == task_id), None)
    if (
        task is None
        or task.status not in claimable
        or (
            expected_vm_instance_id is not None
            and task.vm_instance_id != expected_vm_instance_id
        )
    ):
        return PrClaimFenceDecision(True, False)
    # Payload identity is protected while active, but recheck after the lock so
    # a legacy mutation cannot smuggle a different PR through the peek.
    if _pr_task_identity(task) != identity:
        return PrClaimFenceDecision(
            True,
            False,
            task=task,
            reason="task PR identity changed during claim",
            retryable=True,
        )
    if has_pending_pr_continuation(task):
        return PrClaimFenceDecision(
            True,
            False,
            task=task,
            reason="task has a pending post-commit continuation",
        )

    # A terminal task with deferred post-commit work still owns the complete
    # same-PR review/fix continuation.  This fence deliberately runs before a
    # live lookup or exact-target/type dedup: a stale target is exactly the
    # state the continuation may need to reconcile, and a review can hand off
    # to a fix (or vice versa) while retaining ownership.
    pending_owner = next(
        (
            candidate
            for candidate in chain_tasks
            if candidate.id != task.id
            and is_pending_pr_continuation_owner(candidate)
        ),
        None,
    )
    if pending_owner is not None:
        pending_type = pending_owner.first_payload.get("type")
        reason = (
            f"same-PR {pending_type} {pending_owner.id} has a pending "
            "post-commit continuation"
        )
        _cancel_pr_task(task, reason)
        session.add(task)
        return PrClaimFenceDecision(True, False, task=task, reason=reason)

    try:
        live_pr = await get_pull_request(gh_token, owner, repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        return PrClaimFenceDecision(
            True,
            False,
            task=task,
            reason=f"live PR lookup failed: {exc}",
            retryable=True,
        )
    state = str(live_pr.get("state") or "").lower()
    retargeted_live_target: dict[str, str | int] | None = None
    if live_pr.get("merged") is True or state in {"closed", "merged"}:
        if task_type == "fix" and live_pr.get("merged") is not True and state == "closed":
            retarget_status, replacement_target = await _retarget_closed_rollup_fix(
                task,
                pr_number=pr_number,
                closed_pr=live_pr,
                github_token=gh_token,
                owner=owner,
                repo=repo,
            )
            if retarget_status == "retargeted" and replacement_target is not None:
                replacement_pr_number = int(replacement_target["pr_number"])
                await acquire_pr_spawn_lock(
                    session,
                    task.project_id,
                    replacement_pr_number,
                    repo_identity=f"{owner}/{repo}",
                )
                project_scope = await repository_project_ids(
                    session,
                    task.project_id,
                    owner=owner,
                    repo=repo,
                )
                await session.flush()
                chain_tasks = (
                    (
                        await session.execute(
                            select(Task)
                            .where(
                                Task.project_id.in_(project_scope),
                                _pr_number_match(session, replacement_pr_number),
                            )
                            .order_by(Task.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                task = next(
                    (candidate for candidate in chain_tasks if candidate.id == task.id),
                    None,
                )
                if task is None:
                    return PrClaimFenceDecision(
                        True,
                        False,
                        reason="retargeted fix task disappeared during claim",
                        retryable=True,
                    )
                pr_number = replacement_pr_number
                retargeted_live_target = replacement_target
                state = "open"
            else:
                if retarget_status.startswith("lookup_failed") or retarget_status == (
                    "unresolved"
                ):
                    reason = (
                        "assigned fix head drift: replacement lookup failed"
                        if retarget_status.startswith("lookup_failed")
                        else "assigned fix head drift: replacement target unresolved"
                    )
                    return PrClaimFenceDecision(
                        True,
                        False,
                        task=task,
                        reason=reason,
                        retryable=True,
                    )
                reason = "assigned fix head drift: no authoritative open replacement"
                _cancel_pr_task(task, reason)
                session.add(task)
                return PrClaimFenceDecision(
                    True,
                    False,
                    task=task,
                    reason=reason,
                )
        else:
            reason = "assigned review head drift: PR is no longer open"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
    if state != "open":
        return PrClaimFenceDecision(
            True,
            False,
            task=task,
            reason="live PR state is unresolved",
            retryable=True,
        )
    live_target = retargeted_live_target or _live_target(task, pr_number, live_pr)
    if live_target is None:
        return PrClaimFenceDecision(
            True,
            False,
            task=task,
            reason="live PR target is unresolved",
            retryable=True,
        )

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    if task_type == "review":
        assigned = review_loop.get("review_target")
        if assigned is not None and not _target_matches_live(
            assigned,
            pr_number=pr_number,
            live_target=live_target,
        ):
            reason = "assigned review head drift"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
        exact_review_contenders = [
            candidate
            for candidate in chain_tasks
            if candidate.first_payload.get("type") == "review"
            and is_pr_execution_owner(candidate)
            and (
                ((candidate.context or {}).get("review_loop") or {}).get(
                    "review_target"
                )
                is None
                or _target_matches_live(
                    ((candidate.context or {}).get("review_loop") or {}).get(
                        "review_target"
                    ),
                    pr_number=pr_number,
                    live_target=live_target,
                )
            )
        ]
        canonical_review = min(
            exact_review_contenders,
            key=pr_execution_owner_priority,
            default=task,
        )
        if canonical_review.id != task.id:
            reason = f"exact-target review {canonical_review.id} already owns execution"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
        # Legacy/manual review rows without immutable assignment are bound here,
        # by the backend, before any agent can observe or execute them.
        review_loop["review_target"] = live_target
        review_loop["target_assigned_by_backend"] = True
    else:
        assigned_fix_target = review_loop.get("fix_target")
        reviewed_head = str(review_loop.get("reviewed_head_sha") or "")
        if assigned_fix_target is None and not reviewed_head:
            reason = "fix has no immutable assigned target"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
        if (
            assigned_fix_target is not None
            and not _target_matches_live(
                assigned_fix_target,
                pr_number=pr_number,
                live_target=live_target,
            )
        ) or (
            assigned_fix_target is None
            and reviewed_head
            and reviewed_head != live_target["head_sha"]
        ):
            reason = "assigned fix head drift"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
        review_loop["fix_target"] = live_target

        current_fix_contenders = [
            candidate
            for candidate in chain_tasks
            if candidate.first_payload.get("type") == "fix"
            and is_pr_execution_owner(candidate)
            and _fix_task_matches_live(
                candidate,
                pr_number=pr_number,
                live_target=live_target,
            )
        ]
        canonical_fix = min(
            current_fix_contenders,
            key=pr_execution_owner_priority,
            default=task,
        )
        if canonical_fix.id != task.id:
            reason = f"same-PR fix {canonical_fix.id} already owns execution"
            _cancel_pr_task(task, reason)
            session.add(task)
            return PrClaimFenceDecision(True, False, task=task, reason=reason)
        for candidate in chain_tasks:
            loop = (candidate.context or {}).get("review_loop")
            if isinstance(loop, dict):
                revoke_review_authority_for_active_fix(candidate, task.id)
                session.add(candidate)

    context["review_loop"] = review_loop
    context["pr_execution_fence"] = {
        "allowed": True,
        "task_type": task_type,
        "review_target": live_target,
        "checked_at": _utcnow().isoformat(),
    }
    task.context = context
    flag_modified(task, "context")
    session.add(task)
    return PrClaimFenceDecision(True, True, task=task)
