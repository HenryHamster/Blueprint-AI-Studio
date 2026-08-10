"""One authoritative merge boundary for review proof and active code fixes.

This module owns no scheduling policy.  It validates the immutable base/head
whose checks completed, rebuilds the binding blocker ledger, and either grants
merge authority or idempotently requests the exact review needed to obtain it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.ci_merge_helpers import (
    _fetch_live_review_target,
    _live_target_is_merge_submission_safe,
    _pr_number_match,
    _review_not_required,
    revoke_review_authority_for_active_fix,
)
from app.models import Task, _utcnow
from app.review_execution_ownership import pr_execution_owner_clause
from app.review_merge_gate import (
    _enforce_authoritative_review_merge_gate,
    review_target_matches_merge_identity,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeAuthorityDecision:
    allowed: bool
    checked_head_sha: str
    checked_base_sha: str
    review_loop: dict

async def enforce_merge_authority(
    task: Task,
    session: AsyncSession,
    *,
    gh_token: str,
    owner: str,
    repo: str,
    ci_loop: dict,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    checked_head_sha: str | None = None,
    checked_base_sha: str | None = None,
    precomputed_review_target: dict[str, str | int] | None = None,
    explicit_review_override: bool = False,
) -> MergeAuthorityDecision:
    """Return exact checked-head merge authority or fail closed in-place.

    An allowed return retains the per-PR transaction lock.  The caller must
    perform its one expected-head merge request immediately and then commit or
    roll back; fix admission uses the same lock and therefore cannot enter
    between the final active-fix check and the merge attempt.
    """

    expected_head = str(
        checked_head_sha
        or ci_loop.get("ci_checked_head_sha")
        or ci_loop.get("head_sha")
        or ""
    )
    expected_base = str(
        checked_base_sha or ci_loop.get("ci_checked_base_sha") or ""
    )

    if not expected_head:
        ci_loop["ci_passed"] = False
        ci_loop["ci_waiting"] = True
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: completed checks had no immutable "
            "head SHA; waiting for a bound CI result"
        )[:500]
        session.add(task)
        return MergeAuthorityDecision(False, "", expected_base, ci_loop)

    project_id = str(task.project_id) if task.project_id else ""
    precomputed_is_valid = review_target_matches_merge_identity(
        precomputed_review_target,
        project_id=project_id,
        pr_number=pr_number,
    )
    precomputed_head = str(
        (precomputed_review_target or {}).get("head_sha") or ""
    )
    precomputed_base = str(
        (precomputed_review_target or {}).get("base_sha") or ""
    )
    if not expected_base and precomputed_is_valid and precomputed_head == expected_head:
        expected_base = precomputed_base

    # Serialize only final merge submissions to this base, then share the
    # project-scoped PR admission lock with every fix writer. The live GET must
    # happen inside both locks and immediately precede the active-fix query.
    from app.task_locking import acquire_merge_submission_locks

    # The advisory-lock SELECTs must not autoflush ``task`` first: all merge
    # paths share the PR/base -> Task lock order.
    with session.no_autoflush:
        await acquire_merge_submission_locks(
            session,
            project_id=task.project_id,
            repo_identity=f"{owner}/{repo}",
            base_branch=base_branch,
            pr_number=pr_number,
        )
    # ``ci_loop`` is the caller's exact backend-built proof.  Stage it only
    # after the shared base -> PR locks: lifecycle deliberately keeps this
    # proof local while it performs pre-merge reads so an ORM autoflush cannot
    # take the Task row first and deadlock with PR admission.
    task.context = {**(task.context or {}), "review_loop": dict(ci_loop)}
    flag_modified(task, "context")
    session.add(task)
    # Lifecycle creates backend-owned review/no-review proof immediately before
    # entering this boundary.  Persist it only after the shared PR/base locks
    # are held, then reload the row below.  Relying on ORM autoflush made the
    # proof disappear under factories configured with ``autoflush=False``:
    # ``populate_existing`` replaced the dirty in-memory context with the older
    # database row and the strict gate correctly (but spuriously) saw no exact
    # approval.  The targeted flush preserves the global PR -> Task lock order,
    # avoids flushing unrelated caller-owned state, and makes the boundary
    # independent of sessionmaker configuration.
    await session.flush([task])
    # Keep the reload itself from autoflushing any unrelated caller-owned ORM
    # state.  Only the exact proof-bearing Task above is part of this boundary.
    with session.no_autoflush:
        locked_task = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id == task.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
    if locked_task is None:
        return MergeAuthorityDecision(False, expected_head, expected_base, ci_loop)
    task = locked_task
    ci_loop = dict((task.context or {}).get("review_loop") or {})
    # Caller snapshots are advisory unless explicitly supplied as the checked
    # result.  Rebuild implicit expectations from the row read *inside* the
    # admission locks so a concurrent revocation/CI advance cannot be lost to
    # the identity-map snapshot passed into this function.
    if checked_head_sha is None:
        expected_head = str(
            ci_loop.get("ci_checked_head_sha") or ci_loop.get("head_sha") or ""
        )
    if checked_base_sha is None:
        expected_base = str(ci_loop.get("ci_checked_base_sha") or "")
    live_target = None
    try:
        fetched_target = await _fetch_live_review_target(
            gh_token, owner, repo, pr_number, project_id=project_id
        )
        live_base = str(fetched_target.get("base_sha") or "")
        live_head = str(fetched_target.get("head_sha") or "")
        # Admission/staleness checks key on head only because the integration
        # branch tip is ambient. This final boundary intentionally differs:
        # the merge PUT must be authorized for the exact base+head pair CI
        # validated.
        if (
            live_base == expected_base
            and live_head == expected_head
            and _live_target_is_merge_submission_safe(
                fetched_target, base_branch=base_branch
            )
        ):
            live_target = fetched_target
        elif live_head or live_base:
            task.latest_status_message = (
                f"Merge held for PR #{pr_number}: live base/head or mergeability "
                f"differs from checked {expected_base[:8]}..{expected_head[:8]}; "
                "awaiting current CI"
            )[:500]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CI] Final live target lookup failed for PR #%s: %s; failing closed",
            pr_number,
            exc,
        )
    if live_target is None:
        ci_loop.update(
            {
                "ci_passed": False,
                "ci_waiting": True,
                "pr_number": pr_number,
                "authoritative_review_required": True,
                "authoritative_review_target_unresolved_at": _utcnow().isoformat(),
            }
        )
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        if not task.latest_status_message or "Merge held" not in str(
            task.latest_status_message
        ):
            task.latest_status_message = (
                f"Merge held for PR #{pr_number}: could not prove the checked "
                "target is still open, current, and mergeable"
            )[:500]
        session.add(task)
        return MergeAuthorityDecision(False, expected_head, expected_base, ci_loop)

    from app.review_repository_scope import repository_project_ids

    repository_projects = await repository_project_ids(
        session,
        task.project_id,
        owner=owner,
        repo=repo,
    )
    active_same_pr_tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.payloads,
                        Task.project_id,
                        Task.status_log,
                        Task.post_commit_pending,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id.in_(repository_projects),
                    Task.id != task.id,
                    pr_execution_owner_clause(),
                    _pr_number_match(session, pr_number),
                )
            )
        )
        .scalars()
        .all()
    )
    active_fix = next(
        (
            candidate
            for candidate in active_same_pr_tasks
            if candidate.first_payload.get("type") == "fix"
        ),
        None,
    )
    if active_fix is not None:
        revoked_loop = revoke_review_authority_for_active_fix(
            task,
            active_fix.id,
            review_loop=ci_loop,
        )
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: code fix {active_fix.id} is active; "
            "exact-head re-review required after it completes"
        )[:500]
        session.add(task)
        await session.commit()
        return MergeAuthorityDecision(
            False,
            expected_head,
            expected_base,
            revoked_loop,
        )

    latest_review = next(
        (
            entry
            for entry in reversed(ci_loop.get("review_history") or [])
            if isinstance(entry, dict)
        ),
        {},
    )
    consumed_review_id = (
        str(latest_review.get("review_task_id") or "").strip()
        if latest_review.get("target_verified_by_backend") is True
        and str(latest_review.get("verdict") or "").upper() == "APPROVE"
        and str(
            latest_review.get("reviewed_head_sha")
            or latest_review.get("head_sha")
            or ""
        )
        == expected_head
        and str(
            latest_review.get("reviewed_base_sha")
            or latest_review.get("base_sha")
            or ""
        )
        == expected_base
        else ""
    )
    active_review = next(
        (
            candidate
            for candidate in active_same_pr_tasks
            if candidate.first_payload.get("type") == "review"
            and (
                str(candidate.id) != consumed_review_id
                or candidate.post_commit_pending is True
            )
        ),
        None,
    )
    if active_review is not None and not explicit_review_override:
        ci_loop.update(
            {
                "ci_waiting": True,
                "pr_number": pr_number,
                "authoritative_review_required": True,
                "authoritative_review_task_id": str(active_review.id),
            }
        )
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: review {active_review.id} is active; "
            "waiting for its exact-target verdict"
        )[:500]
        session.add(task)
        await session.commit()
        return MergeAuthorityDecision(False, expected_head, expected_base, ci_loop)

    ci_loop["ci_checked_head_sha"] = expected_head
    ci_loop["ci_checked_base_sha"] = expected_base
    task.context = {**(task.context or {}), "review_loop": ci_loop}
    flag_modified(task, "context")

    bypass_target = ci_loop.get("review_target")
    bypass_is_exact = bool(
        explicit_review_override
        or (_review_not_required(ci_loop)
        and review_target_matches_merge_identity(
            bypass_target,
            project_id=project_id,
            pr_number=pr_number,
        )
        and str((bypass_target or {}).get("head_sha") or "") == expected_head
        and str((bypass_target or {}).get("base_sha") or "") == expected_base)
    )
    if not bypass_is_exact:
        ci_loop.pop("review_not_required", None)
        ci_loop.pop("review_policy_reason", None)
        allowed = await _enforce_authoritative_review_merge_gate(
            task,
            session,
            ci_loop=ci_loop,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            original_task_id=original_task_id,
            live_review_target=live_target,
        )
        if not allowed:
            return MergeAuthorityDecision(False, expected_head, expected_base, ci_loop)

    return MergeAuthorityDecision(True, expected_head, expected_base, ci_loop)


async def attempt_merge_with_authority(
    task: Task,
    session: AsyncSession,
    *,
    merge_call: Callable[[str], Awaitable[dict]],
    gh_token: str,
    owner: str,
    repo: str,
    ci_loop: dict,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    checked_head_sha: str | None,
    checked_base_sha: str | None,
    retry_nonterminal: bool = False,
    explicit_review_override: bool = False,
) -> tuple[MergeAuthorityDecision, dict | None]:
    """Freshly authorize each expected-head merge PUT and release its PR lock."""
    for attempt in range(4 if retry_nonterminal else 1):
        if attempt:
            await asyncio.sleep(5 * (3 ** (attempt - 1)))
        decision = await enforce_merge_authority(
            task,
            session,
            gh_token=gh_token,
            owner=owner,
            repo=repo,
            ci_loop=ci_loop,
            pr_number=pr_number,
            branch=branch,
            base_branch=base_branch,
            original_task_id=original_task_id,
            checked_head_sha=checked_head_sha,
            checked_base_sha=checked_base_sha,
            # A cached caller snapshot must never stand in for the final GET.
            precomputed_review_target=None,
            explicit_review_override=explicit_review_override,
        )
        ci_loop = decision.review_loop
        if not decision.allowed:
            await session.commit()
            return decision, None
        merge_error: Exception | None = None
        try:
            result = await merge_call(decision.checked_head_sha)
        except Exception as exc:  # noqa: PERF203
            merge_error = exc
        finally:
            # Release the submission locks only after the exact-head PUT has
            # returned.  The transport-level ``merged`` receipt deliberately
            # does NOT stamp ``context.pr_merged`` here: that field is the
            # semantic rollup-unblock / integration-landing signal, and only
            # the branch-aware caller can classify an intermediate staging,
            # integration, or promotion-target landing correctly.
            await session.commit()
        if merge_error is not None:
            if attempt >= (3 if retry_nonterminal else 0):
                raise merge_error
            continue
        if (
            result.get("merged")
            or result.get("conflict")
            or result.get("review_veto")
            or not retry_nonterminal
        ):
            return decision, result
    return decision, result
