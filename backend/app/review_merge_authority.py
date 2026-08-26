"""One authoritative merge boundary for review proof and active code fixes.

This module owns no scheduling policy.  It validates the immutable base/head
whose checks completed, rebuilds the binding blocker ledger, and either grants
merge authority or idempotently requests the exact review needed to obtain it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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


class MergeSubmissionIndeterminate(RuntimeError):
    """A durable submission-start exists but GitHub acceptance is unresolved."""


class MergePreSubmissionRefused(RuntimeError):
    """An exact pre-PUT gate closed the intent and requires fresh approval."""


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
    pre_lock_authority: Callable[[AsyncSession], Awaitable[bool]] | None = None,
    source_authority: Callable[[AsyncSession], Awaitable[Task | None]] | None = None,
    approval_protocol_owned: bool = False,
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
    expected_base = str(checked_base_sha or ci_loop.get("ci_checked_base_sha") or "")

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
    precomputed_head = str((precomputed_review_target or {}).get("head_sha") or "")
    precomputed_base = str((precomputed_review_target or {}).get("base_sha") or "")
    if not expected_base and precomputed_is_valid and precomputed_head == expected_head:
        expected_base = precomputed_base

    # A durable approval submission binds project branch/repository policy.  Its
    # second gate locks and validates Project before acquiring the base/PR
    # advisory locks, preserving the global Project -> base -> PR -> Task order.
    if pre_lock_authority is not None and not await pre_lock_authority(session):
        return MergeAuthorityDecision(
            False, expected_head, expected_base, dict(ci_loop)
        )

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
    if source_authority is not None:
        # Approval callers released their decision Task lock before credential,
        # migration, and live-target I/O. Reacquire the exact row only after the
        # canonical base -> PR locks, and reject a replaced decision before any
        # caller snapshot is staged or flushed.
        with session.no_autoflush:
            authoritative_task = await source_authority(session)
        if authoritative_task is None:
            return MergeAuthorityDecision(
                False, expected_head, expected_base, dict(ci_loop)
            )
        task = authoritative_task
        ci_loop = dict((task.context or {}).get("review_loop") or {})
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
    if not approval_protocol_owned:
        from app.admin_approval_merge_recovery import approval_merge_in_flight

        if await approval_merge_in_flight(task, session):
            return MergeAuthorityDecision(False, expected_head, expected_base, ci_loop)
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
        or (
            _review_not_required(ci_loop)
            and review_target_matches_merge_identity(
                bypass_target,
                project_id=project_id,
                pr_number=pr_number,
            )
            and str((bypass_target or {}).get("head_sha") or "") == expected_head
            and str((bypass_target or {}).get("base_sha") or "") == expected_base
        )
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
    source_authority: Callable[[AsyncSession], Awaitable[Task | None]] | None = None,
    submission_start: Callable[
        [AsyncSession, MergeAuthorityDecision],
        Awaitable[Any | None],
    ]
    | None = None,
    submission_receipt: Callable[
        [AsyncSession, MergeAuthorityDecision, dict, str], Awaitable[None]
    ]
    | None = None,
) -> tuple[MergeAuthorityDecision, dict | None]:
    """Freshly authorize each expected-head merge PUT and release its PR lock.

    Approval callers optionally linearize a durable ambiguous submission before
    the socket write.  Only the process that wins that transition receives a
    one-shot authority callback; it then reruns the final live-target/fix gate.
    Restarted callers never enter this function for an ambiguous intent.
    """
    merge_project_id = str(task.project_id)
    merge_task_id = task.id

    # ── Rollup child-deliverable gate (pre-lock, pre-PUT) ──────────────────
    # This function is the single choke point every merge path funnels through
    # (attempt_policy_bound_merge, attempt_explicit_human_merge, and the direct
    # ci_completion call), so placing the gate here means no merge path can be
    # added later that quietly skips it. It runs BEFORE enforce_merge_authority
    # takes the base/PR/Task locks: the check may make one GitHub get_pr_files
    # call, and held-lock GitHub I/O is its own incident class.
    #
    # Fail-open and free for non-rollup merges: a dict lookup on the task's
    # rollup identity answers first. explicit_review_override does NOT bypass it
    # — that flag bypasses model-review POLICY, never target/lock/fix SAFETY, and
    # silently destroying a merged child's deliverable is a safety condition.
    from app.rollup_deliverable_guard import (
        enforce_premerge_rollup_deliverable_guard,
    )

    _deliverable = await enforce_premerge_rollup_deliverable_guard(
        task,
        session,
        pr_number=pr_number,
        gh_token=gh_token,
        owner=owner,
        repo=repo,
        ci_loop=ci_loop,
        branch=branch,
        base_branch=base_branch,
    )
    if not _deliverable.allowed:
        # Re-arm the CI/merge poll through the canonical helper so the hold can
        # never write the identity-less {ci_waiting} shape the merge poll skips
        # (see ci_merge_helpers.arm_ci_wait). The hold IS a wait, so a fresh
        # ci_wait_started is correct, and PREMERGE_HOLD_CAP bounds how many
        # times it can be reset.
        from app.ci_merge_helpers import arm_ci_wait

        held_loop = arm_ci_wait(
            dict(ci_loop),
            pr_number=pr_number,
            base_branch=base_branch,
            now_iso=_utcnow().isoformat(),
            pause_baseline=float(ci_loop.get("ci_wait_pause_baseline") or 0.0),
        )
        held_loop["rollup_deliverable_hold_reason_code"] = _deliverable.reason_code
        task.context = {**(task.context or {}), "review_loop": held_loop}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.error(
            "[MERGE-AUTHORITY] merge held for PR #%s on task %s: %s (%s)",
            pr_number,
            task.id,
            _deliverable.reason,
            _deliverable.reason_code,
        )
        return (
            MergeAuthorityDecision(
                False,
                str(checked_head_sha or ""),
                str(checked_base_sha or ""),
                held_loop,
            ),
            None,
        )

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
            source_authority=source_authority,
            approval_protocol_owned=submission_start is not None,
        )
        ci_loop = decision.review_loop
        if not decision.allowed:
            if submission_start is not None:
                # Exact-CAS the known-unsent intent closed.  The callback raises
                # MergePreSubmissionRefused after installing the independent
                # hold; a lost/replaced CAS is indeterminate, never a silent hold.
                closed_authority = await submission_start(session, decision)
                if closed_authority is not None:
                    await closed_authority.abort_before_send(
                        session, "the first final merge-safety gate refused"
                    )
                raise MergeSubmissionIndeterminate(
                    "pre-submission denial could not close the exact merge intent"
                )
            await session.commit()
            return decision, None
        if submission_start is not None:
            started_authority = await submission_start(session, decision)
            if started_authority is None:
                return MergeAuthorityDecision(
                    False,
                    decision.checked_head_sha,
                    decision.checked_base_sha,
                    decision.review_loop,
                ), None
            task = await session.get(Task, merge_task_id, populate_existing=True)
            if task is None:
                abort_before_send = getattr(
                    started_authority, "abort_before_send", None
                )
                if abort_before_send is not None:
                    closed = await abort_before_send(
                        session, "the approval source disappeared before transport"
                    )
                    if closed:
                        raise MergePreSubmissionRefused(
                            "the approval source disappeared before transport"
                        )
                raise MergeSubmissionIndeterminate(
                    "approval source loss could not close the exact merge intent"
                )
            # The durable transition committed and released the first admission
            # locks. Reacquire base -> PR -> Task and repeat the final GitHub
            # target plus active-fix checks before this process's single PUT.
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
                precomputed_review_target=None,
                explicit_review_override=explicit_review_override,
                pre_lock_authority=getattr(
                    started_authority, "project_authority", None
                ),
                source_authority=getattr(
                    started_authority, "source_authority", started_authority
                ),
                approval_protocol_owned=True,
            )
            ci_loop = decision.review_loop
            if not decision.allowed:
                abort_before_send = getattr(
                    started_authority, "abort_before_send", None
                )
                if abort_before_send is not None:
                    closed = await abort_before_send(
                        session, "the second final merge-safety gate refused"
                    )
                else:
                    await session.commit()
                    closed = False
                if closed:
                    raise MergePreSubmissionRefused(
                        "the second final merge-safety gate refused"
                    )
                raise MergeSubmissionIndeterminate(
                    "pre-submission denial lost its exact terminalization CAS"
                )
            acquire_send_lease = getattr(
                started_authority, "acquire_send_lease", None
            )
            send_token = (
                await acquire_send_lease(session)
                if acquire_send_lease is not None
                else None
            )
            if not send_token:
                closed = await started_authority.abort_before_send(
                    session, "send-lease authority changed before transport"
                )
                if closed:
                    raise MergePreSubmissionRefused(
                        "send-lease authority changed before transport"
                    )
                raise MergeSubmissionIndeterminate(
                    "send-lease CAS was lost to reconciliation or replacement"
                )
            # The lease transition commits the exact Project/base/PR/Task gate,
            # releasing every database lock.  A last GET plus the sole guarded
            # PUT now run without blocking unrelated Project/Task writers.
            try:
                final_target = await _fetch_live_review_target(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    project_id=merge_project_id,
                )
            except Exception as exc:
                closed = await started_authority.close_without_merge(
                    session,
                    "known_not_sent",
                    "the final live pull-request target could not be read",
                    send_token,
                )
                if closed:
                    raise MergePreSubmissionRefused(
                        "the final live pull-request target could not be read"
                    ) from exc
                raise MergeSubmissionIndeterminate(
                    "final target lookup lost its exact send authority"
                ) from exc
            if not (
                str(final_target.get("head_sha") or "")
                == decision.checked_head_sha
                and str(final_target.get("base_sha") or "")
                == decision.checked_base_sha
                and str(final_target.get("head_ref") or "") == str(branch)
                and str(final_target.get("base_ref") or "") == str(base_branch)
                and _live_target_is_merge_submission_safe(
                    final_target, base_branch=base_branch
                )
            ):
                closed = await started_authority.close_without_merge(
                    session,
                    "unsent_aborted",
                    "the final live pull-request target changed before transport",
                    send_token,
                )
                if closed:
                    raise MergePreSubmissionRefused(
                        "the final live pull-request target changed before transport"
                    )
                raise MergeSubmissionIndeterminate(
                    "final target denial lost its exact send authority"
                )
            # Project/Task rows stayed available throughout the final GET. Now
            # exact-CAS the same token against fresh config/promotion authority,
            # renew its bounded PUT window, and release those rows again before
            # the sole transport attempt.
            try:
                send_authority_is_current = await started_authority.validate_send_lease(
                    session, send_token
                )
            except Exception as exc:
                await session.rollback()
                raise MergeSubmissionIndeterminate(
                    "send-lease authority could not be revalidated before transport"
                ) from exc
            if not send_authority_is_current:
                closed = await started_authority.close_without_merge(
                    session,
                    "unsent_aborted",
                    "project config or promotion authority changed during final target lookup",
                    send_token,
                )
                if closed:
                    raise MergePreSubmissionRefused(
                        "project config or promotion authority changed during final target lookup"
                    )
                raise MergeSubmissionIndeterminate(
                    "post-target validation lost its exact send authority"
                )
        else:
            started_authority = None
            send_token = None
        merge_error: Exception | None = None
        result: dict = {}
        submission_head_sha = decision.checked_head_sha
        submission_base_sha = decision.checked_base_sha
        try:
            result = await merge_call(submission_head_sha)
        except Exception as exc:  # noqa: BLE001
            merge_error = exc
        if submission_start is None:
            # Autonomous lanes retain their admission locks through transport;
            # approval lanes released them at the durable lease CAS above.
            await session.commit()
        if merge_error is not None:
            if submission_start is not None:
                if getattr(merge_error, "merge_not_attempted", False):
                    closed = await started_authority.close_without_merge(
                        session,
                        "known_not_sent",
                        "GitHub rate limiting refused the request before merge execution",
                        send_token,
                    )
                    if not closed:
                        raise MergeSubmissionIndeterminate(
                            "known-not-sent outcome lost its exact lease CAS"
                        ) from merge_error
                    raise merge_error
                await started_authority.release_for_reconciliation(
                    session, send_token
                )
                raise MergeSubmissionIndeterminate(
                    "merge submission may have been accepted; reconciliation required"
                ) from merge_error
            if attempt >= (3 if retry_nonterminal else 0):
                raise merge_error
            continue
        if result.get("pat_fallback_blocked") is True:
            # merge_pull_request refused locally BEFORE any PUT: the only
            # resolvable credential was the operator's own PAT and this lane
            # does not allow minting operator-attributed merge commits. This is
            # the same class as the rate-limit ``merge_not_attempted`` case
            # above — known-not-sent — so it must NOT reach the reconciliation
            # block below, which would perform a live target lookup and then
            # write a 'submission_rejected' receipt claiming GitHub rejected a
            # request that was never issued (or release the lease as
            # indeterminate). Terminalize honestly and stop retrying.
            if submission_start is not None:
                closed = await started_authority.close_without_merge(
                    session,
                    "known_not_sent",
                    "no non-operator GitHub credential was available; the merge "
                    "was never submitted",
                    send_token,
                )
                if not closed:
                    raise MergeSubmissionIndeterminate(
                        "known-not-sent outcome lost its exact lease CAS"
                    )
            return decision, result
        if (
            submission_start is not None
            and result.get("merged") is not True
            and result.get("conflict") is True
        ):
            closed = await started_authority.close_without_merge(
                session,
                "submission_rejected",
                "GitHub definitively rejected the merge request as conflicting",
                send_token,
            )
            if not closed:
                raise MergeSubmissionIndeterminate(
                    "conflict outcome lost its exact lease CAS"
                )
            return decision, result
        if submission_start is not None and result.get("merged") is not True:
            # GitHub's 405 conflates already-merged with rejected. Disambiguate
            # outside all DB locks; an exact merged observation becomes the
            # objective receipt, while an exact open/closed unmerged target is a
            # terminal rejection. Unknown identity stays ambiguous.
            try:
                observed_target = await _fetch_live_review_target(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    project_id=merge_project_id,
                )
            except Exception as exc:
                await started_authority.release_for_reconciliation(
                    session, send_token
                )
                raise MergeSubmissionIndeterminate(
                    "non-merge response could not be reconciled"
                ) from exc
            observed_identity_exact = bool(
                str(observed_target.get("head_sha") or "") == submission_head_sha
                and str(observed_target.get("head_ref") or "") == str(branch)
                and str(observed_target.get("base_ref") or "") == str(base_branch)
            )
            if observed_target.get("merged") is True and observed_identity_exact:
                result = {
                    "merged": True,
                    "sha": str(observed_target.get("merge_commit_sha") or ""),
                    "message": "exact merged PR observed after non-merge response",
                    "observed_merged": True,
                }
            elif (
                observed_identity_exact
                and observed_target.get("merged") is False
                and str(observed_target.get("state") or "").lower()
                in {"open", "closed"}
            ):
                closed = await started_authority.close_without_merge(
                    session,
                    "submission_rejected",
                    "GitHub confirmed the exact pull request remained unmerged",
                    send_token,
                )
                if not closed:
                    raise MergeSubmissionIndeterminate(
                        "non-merge terminalization lost its exact lease CAS"
                    )
                return decision, result
            else:
                await started_authority.release_for_reconciliation(
                    session, send_token
                )
                raise MergeSubmissionIndeterminate(
                    "non-merge response remains ambiguous for the bound target"
                )
        if submission_start is not None and result.get("merged") is True:
            merge_sha = str(result.get("sha") or result.get("merge_commit_sha") or "")
            if not merge_sha:
                await started_authority.release_for_reconciliation(
                    session, send_token
                )
                raise MergeSubmissionIndeterminate(
                    "merge response omitted its immutable merge commit"
                )
            # The base/PR/Task locks are released before this immutable proof GET.
            # A response loss or proof outage leaves the durable ledger ambiguous;
            # recovery performs GET-only reconciliation and never replays PUT.
            from app.ci_merge_helpers import (
                _merge_commit_parent_matches_checked_base,
            )

            try:
                base_parent_verified = await _merge_commit_parent_matches_checked_base(
                    gh_token,
                    owner,
                    repo,
                    merge_sha,
                    submission_base_sha,
                    project_id=merge_project_id,
                )
                result = {
                    **result,
                    "checked_base_parent_verified": base_parent_verified,
                }
                if submission_receipt is not None:
                    await submission_receipt(session, decision, result, send_token)
            except Exception as exc:
                await session.rollback()
                await started_authority.release_for_reconciliation(
                    session, send_token
                )
                raise MergeSubmissionIndeterminate(
                    "merge was acknowledged but its exact durable proof needs reconciliation"
                ) from exc
        if submission_start is not None and result.get("merged") is not True:
            raise MergeSubmissionIndeterminate(
                "merge response was not objective proof; reconciliation required"
            )
        if (
            result.get("merged")
            or result.get("conflict")
            or result.get("review_veto")
            or result.get("pat_fallback_blocked")
            or not retry_nonterminal
        ):
            return decision, result
    return decision, result
