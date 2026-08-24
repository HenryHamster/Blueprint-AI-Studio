"""CI completion handler — shared between webhook and polling fallback.

Processes CI check results for tasks waiting on CI. Handles merge on success,
fix spawning on failure, and various edge cases (merge conflicts, race conditions).

Merge gate
==========
A PR is merged only when BOTH gates are open on the parking task's review_loop:

  - ``ci_passed: True``      — CI reported success (set by handle_ci_completion)
  - ``review_approved: True`` — A review task signed off (set by task_lifecycle)

Whichever signal arrives first sets its flag and waits; the second flips the
gate and triggers the merge. ``ci_waiting`` only means "orchestrator should poll
CI" — it is NOT permission to merge. A legacy scalar
``verdict == "APPROVE"`` is never merge authority; an exact-target APPROVE in
durable review history (or an explicit no-review policy marker) is required.
"""

import asyncio  # noqa: F401  # test mock target (app.ci_completion.asyncio.sleep)
import copy
import contextlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    approval_hold_provenance,
    establish_independent_approval_hold,
)
# Merge-helper layer (scope-aware retry-cap policy, merge-error normalization,
# the one-time merge summary, and the lock-window snapshot/relock idiom) lives
# in the ci_merge_helpers sibling. Re-export every public name so existing
# ``from app.ci_completion import ...`` call sites and tests resolve unchanged;
# the names below are referenced by the two routines that remain in this module.
from app.ci_merge_helpers import (  # noqa: F401
    MERGE_ESCALATE_BEFORE_CAP,
    MERGE_FAILURE_HARD_CAP,
    _capture_merge_snapshot,
    _is_deterministic_merge_error,
    _merge_hard_cap_for_task,
    _normalize_merge_error,
    _post_merge_summary,
    _pr_number_match,
    _relock_merge_task,
    _review_approved,
    _review_not_required,
    arm_ci_wait,
    effective_pr_number,
    locally_refused_merge_status,
    revoke_review_authority_for_active_fix,
)
from app.models import Task, TaskStatus, _utcnow

logger = logging.getLogger(__name__)

# ── CI rerun budget (general flake-tolerant gate) ──
# Number of times to re-run failed CI jobs (via GitHub's rerun-failed-jobs API)
# before spawning a full agent fix task. A value of 1 means one recheck attempt
# per task; after that the CI must genuinely pass or a fix task is spawned.
# Configured as a module constant so it's trivially overridable for testing;
# set to 0 to disable the general rerun gate entirely.
_CI_RERUN_BUDGET = 1


def _establish_ci_approval_hold(task: Task, *, reason: str) -> None:
    context = dict(task.context or {})
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    if (
        task.requires_approval is True
        and isinstance(provenance, dict)
        and provenance.get("owner") == "ci_completion"
        and provenance.get("reason") == reason
        and not context.get(APPROVAL_FOLLOWUP_CLAIM_KEY)
        and approval_hold_provenance(task).get("review_wait_hold") is None
    ):
        return
    establish_independent_approval_hold(
        task, reason=reason, owner="ci_completion"
    )


async def _auto_resolve_audit_findings_on_merge(
    project_id,
    finding_keys: list,
    resolving_ref: str | None,
    session,
) -> None:
    """Retain the historical seam without granting merge-time evidence.

    Finding-resolution keys are a claim, not current-tree verification. The
    explicit audit/operator resolution paths remain responsible for ledger
    writes; this compatibility helper is intentionally a no-op.

    The machine evidence a merged claim CAN earn is produced elsewhere:
    ``acceptance_gate.verify_finding_clearance_at_landing`` re-checks each
    claimed finding against the exact landed commit on every async landing lane
    (``landed_signal._verify_finding_clearance_before_landing``) and stamps
    ``finding_clearance_verification``, which
    ``acceptance_gate.finding_clearance_blocks_success`` reads.
    """
    return None


async def _refresh_empty_build_config_after_integration_landing(
    project_id, landed_ref: str
) -> bool:
    """Run best-effort build-config refresh in an isolated session."""
    try:
        from app.build_config_detection import maybe_refresh_empty_build_config
        from app.database import AsyncSessionLocal
        from app.models import Project

        async with AsyncSessionLocal() as refresh_session:
            project = await refresh_session.get(Project, project_id)
            if project is None:
                return False
            return await maybe_refresh_empty_build_config(
                project,
                refresh_session,
                ref=landed_ref,
            )
    except Exception as e:
        logger.warning(
            "[CI] Failed isolated build_config refresh after integration landing "
            "for project %s on %s: %s",
            project_id,
            landed_ref,
            e,
            exc_info=True,
        )
        return False


async def _attempt_merge_with_retry_and_escalation(
    task: Task,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str,
    precomputed_migration_guard: object | None = None,
    precomputed_size_cap: object | None = None,
    checked_head_sha: str | None = None,
    checked_base_sha: str | None = None,
) -> bool:
    """Both gates are open — try to merge. Mutates ``task`` in place; caller commits.

    Handles the existing retry / rule-block escalation / merge-failure path that
    used to live inline inside handle_ci_completion. Returns True iff merged.

    Round 3 H4 (lock-window break): caller (process_ci_completion) holds the
    task FOR UPDATE lock since line 762. The merge_pull_request call and its
    retry loop (5 + 15 + 45 = 65s combined) hold that lock across long-running
    GitHub I/O, starving concurrent heartbeats on the same row. We mirror the
    rollup_management ``_release_parent_lock`` / ``_relock_parent`` pattern:
    capture a content snapshot, commit-release, run the merge calls without
    lock, then re-acquire with snapshot validation. If a peer mutated the row
    during the unlocked window, the re-acquire fails and we defer.

    Round 4 W1 (audit C1, C3): the prior implementation snapshot ``updated_at``
    and validated against it on re-acquire. Two structural bugs:
      - C1 (cache): SQLAlchemy's identity map under ``expire_on_commit=False``
        returned the cached pre-commit instance on re-SELECT, so
        ``fresh_task.updated_at`` always equalled ``pre_merge_updated_at`` and
        the validation never fired. Fix: ``populate_existing=True`` on the
        re-SELECT forces SQLA to refresh attributes from the new row.
      - C3 (heartbeat): even with the cache fix, ``onupdate=_utcnow``
        advances ``updated_at`` on every heartbeat. Under load every
        merge attempt would false-defer. Fix: validate against a content
        snapshot of the fields the merge window protects (status,
        review_loop.iteration, review_loop.review_approved/verdict,
        ci_passed) — heartbeat-only mutations don't touch those fields.
    """
    from app.github import merge_pull_request, create_pr_comment, get_pull_request
    from app.alert_dispatch import (
        _maybe_create_failure_alert,
        _maybe_create_unverified_merge_alert,
    )
    from app.branch_resolution import (
        is_custom_staged_rollup_root,
        merged_pr_counts_as_integration_landing,
    )
    from app.landed_signal import corroborate_and_apply_merged_landed_signal
    from app.models import Project
    from app.shared_state import (
        get_project_integration_branch,
        get_project_promotion_target,
    )
    from app.shared_state import runtime_config as _rt_cfg

    # Approval gates are enforced at the merge call so a task parked in
    # ci_waiting cannot bypass an approval requirement added while CI was pending.
    # Leave the task EVALUATING until the operator clears the gate.
    if task.requires_approval and _rt_cfg.get("require_approval_gates", True):
        _gate_pr = (task.context or {}).get("review_loop", {}).get("pr_number")
        logger.info(
            f"[CI] Merge held for PR #{_gate_pr} — task {str(task.id)[:8]} "
            f"requires_approval (approval gate still set); leaving "
            f"EVALUATING, not merging"
        )
        task.latest_status_message = (
            f"Merge gated — awaiting approval for PR #{_gate_pr}"
        )
        session.add(task)
        return False

    ci_loop = (task.context or {}).get("review_loop", {})
    # PR-identity split-brain fix: resolve via the canonical accessor
    # (review_loop.pr_number OR context.rollup_pr_number) so a rollup parked with
    # its identity only at context.rollup_pr_number still merges here, and stamp
    # it back onto the loop so every downstream read (the merge call below) and
    # the recovery sweeps agree. Caller commits.
    ci_pr_number = effective_pr_number(task)
    if ci_pr_number and not ci_loop.get("pr_number"):
        ci_loop["pr_number"] = ci_pr_number
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
    ci_payload = task.first_payload
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get(
        "original_task_id", ""
    )
    ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
    _merge_project = (
        await session.get(Project, task.project_id) if task.project_id else None
    )
    ci_base = (
        ci_loop.get("base_branch", "")
        or ci_payload.get("base_branch", "")
        or get_project_integration_branch(_merge_project)
    )
    if not ci_pr_number:
        task.latest_status_message = "Merge held: missing PR identity"
        session.add(task)
        return False

    # Pre-merge Alembic dual-head guard (parity with the autonomous rollup and
    # hotfix back-merge paths). Two task PRs can each independently add 'the
    # next' migration with the same revision id; each passes its own isolated
    # CI and then lands a duplicate revision / dual head when the second one
    # merges against the advanced integration branch. Validate the combined
    # chain against ``ci_base`` BEFORE merging; on a block-or-defer verdict hold
    # the merge (leave the task EVALUATING, caller commits) and return False —
    # mirroring the requires_approval early-return above. This runs after that
    # approval gate so gate-block behavior is unchanged.
    if ci_pr_number:
        from app.migration_reservations import get_migration_reservation_for_task
        from app.rollup_management import attempt_pr_migration_finalization
        from app.rollup_migration_validation import (
            PremergeMigrationGuardResult,
            enforce_premerge_migration_guard,
        )

        # Use caller-supplied pre-computed guard result when available (SITE A
        # lock-window fix: the rollup-handoff path runs the guard before
        # acquiring FOR UPDATE on the rollup parent so GitHub I/O does not hold
        # the row lock).  Fall back to running the guard inline for all other
        # callers (CI-completion, promotion, hotfix back-merge) where the guard
        # already runs outside the critical lock window.
        from app.migration_guard_recovery import (
            coerce_migration_guard_result,
            mark_migration_guard_deferred,
            spawn_migration_guard_fix_task,
        )

        # A pre-computed verdict is reusable only when it proves the merge
        # target this chokepoint actually resolved.  The pre-lock site resolves
        # its base from a narrower view of the parent task, so an unbound or
        # differently-targeted verdict says nothing about the merge submitted
        # here — recompute inline rather than adopting it (parity with the
        # size-cap reuse check below).
        _reuse_precomputed_guard = (
            isinstance(precomputed_migration_guard, PremergeMigrationGuardResult)
            and bool(ci_base)
            and precomputed_migration_guard.is_bound_to_target(ci_base)
        )
        if _reuse_precomputed_guard:
            _guard_result = precomputed_migration_guard
        else:
            _guard_result = await enforce_premerge_migration_guard(
                gh_token,
                owner,
                repo,
                ci_pr_number,
                ci_base,
                promotion_target=(
                    get_project_promotion_target(_merge_project)
                    if _merge_project is not None
                    else None
                ),
            )
        _guard_verdict, _hold_reason = coerce_migration_guard_result(_guard_result)
        _ok_to_merge = _guard_verdict == "ok"
        if not _ok_to_merge:
            if _guard_verdict == "defer":
                mark_migration_guard_deferred(
                    task,
                    review_loop=ci_loop,
                    reason=_hold_reason,
                    context_snapshot=dict(task.context or {}),
                )
                session.add(task)
                return False
            reservation_task_id = uuid.UUID(ci_orig_id) if ci_orig_id else task.id
            reservation = await get_migration_reservation_for_task(
                session, task_id=reservation_task_id
            )
            if reservation is not None and ci_branch and ci_base:
                try:
                    finalization = await attempt_pr_migration_finalization(
                        gh_token,
                        owner,
                        repo,
                        pr_number=ci_pr_number,
                        pr_branch=ci_branch,
                        target_branch=ci_base,
                        promotion_target=(
                            get_project_promotion_target(_merge_project)
                            if _merge_project is not None
                            else None
                        ),
                        project_id=str(task.project_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "[CI] Migration finalization attempt failed for PR #%s: %s",
                        ci_pr_number,
                        exc,
                        exc_info=True,
                    )
                    finalization = None
                if finalization is not None:
                    from app.task_completion import _reset_review_loop_pr_head_cache

                    review_loop = _reset_review_loop_pr_head_cache(ci_loop)
                    review_loop = revoke_review_authority_for_active_fix(
                        task,
                        review_loop=review_loop,
                    )
                    # Re-arm the CI wait on the rewritten PR head. arm_ci_wait
                    # co-stamps pr_number + a fresh ci_wait_started so the merge
                    # poll never sees the identity-less {ci_waiting} shape.
                    arm_ci_wait(
                        review_loop,
                        pr_number=ci_pr_number,
                        now_iso=_utcnow().isoformat(),
                    )
                    review_loop["ci_passed"] = False
                    task.context = {
                        **(task.context or {}),
                        "_migration_finalization": finalization,
                        "review_loop": review_loop,
                    }
                    flag_modified(task, "context")
                    task.latest_status_message = (
                        "Merge held — auto-finalized provisional Alembic "
                        f"migration {finalization['old_revision']}->"
                        f"{finalization['finalized_revision']} against {ci_base}; "
                        "awaiting fresh CI on the rewritten PR head"
                    )[:500]
                    session.add(task)
                    return False
            logger.warning(
                f"[CI] Merge held for PR #{ci_pr_number} — Alembic combined-chain "
                f"guard: {_hold_reason}"
            )
            task.latest_status_message = (
                f"Merge held — Alembic dual-head guard: {_hold_reason}"
            )[:500]
            await spawn_migration_guard_fix_task(
                task,
                session,
                pr_number=ci_pr_number,
                branch=ci_branch,
                base_branch=ci_base,
                original_task_id=ci_orig_id,
                guard_reason=_hold_reason,
                review_loop=ci_loop,
                context_snapshot=dict(task.context or {}),
                gh_token=gh_token,
                owner=owner,
                repo=repo,
            )
            session.add(task)
            return False

    # Size-cap preflight for every reviewed task PR, including rollup parents.
    # Small intermediate rollups retain their earlier check in
    # rollup_management.py as an optimization, but reviewed rollups must reach
    # this canonical merge chokepoint with a verdict bound to their CI head.
    # A matching pre-lock result is reused below, so this does not issue a
    # second check or cap bump for the same immutable head.
    if ci_pr_number and ci_branch:
        from app.rollup_size_preflight import (
            SizeCapPreflightResult,
            preflight_rollup_size_caps,
        )
        from app.task_completion import _reset_review_loop_pr_head_cache

        # CI evidence, the cap verdict, and merge submission must name exactly
        # one immutable head.  A status/reason tuple has no authority: it may
        # describe a different branch tip by the time this path submits.
        _sc_expected_head = str(
            checked_head_sha
            or ci_loop.get("ci_checked_head_sha")
            or ci_loop.get("head_sha")
            or ""
        )
        if not _sc_expected_head:
            task.latest_status_message = (
                "Merge held — size-cap guard lacks an immutable CI head; "
                "awaiting fresh CI"
            )[:500]
            session.add(task)
            return False
        # A pre-lock verdict is reusable only when it proves both sides of the
        # merge identity: the immutable candidate head and the resolved merge
        # target.  In particular, a targetless defer must not suppress this
        # chokepoint's integration-branch evaluation.
        _reuse_precomputed_size_cap = (
            isinstance(precomputed_size_cap, SizeCapPreflightResult)
            and bool(ci_base)
            and precomputed_size_cap.target_branch == ci_base
            and precomputed_size_cap.is_bound_to(_sc_expected_head)
        )
        if _reuse_precomputed_size_cap:
            _sc_result = precomputed_size_cap
        else:
            _sc_result = await preflight_rollup_size_caps(
                gh_token,
                owner,
                repo,
                ci_pr_number,
                ci_branch,
                project_id=str(task.project_id) if task.project_id else None,
                expected_head_sha=_sc_expected_head,
                target_branch=ci_base,
            )
        if not isinstance(_sc_result, SizeCapPreflightResult):
            # No compatibility path: allowing legacy unkeyed tuples turns the
            # preflight into an advisory observation again.
            task.latest_status_message = (
                "Merge held — size-cap guard returned unbound evidence; "
                "awaiting fresh evaluation"
            )[:500]
            session.add(task)
            return False
        _sc_verdict = _sc_result.status
        _sc_reason = _sc_result.reason
        if _sc_verdict == "ok" and not _sc_result.is_bound_to(_sc_expected_head):
            task.latest_status_message = (
                "Merge held — size-cap verdict head differs from immutable CI head; "
                "awaiting fresh evaluation"
            )[:500]
            session.add(task)
            return False
        from app.shared_state import runtime_config as _rt_cfg

        _gates_on = _rt_cfg.get("require_approval_gates", True)
        if _sc_verdict == "hold":
            if _gates_on:
                _establish_ci_approval_hold(task, reason="merge_size_cap")
            task.latest_status_message = (
                f"Merge held — size-cap: {_sc_reason}"
            )[:500]
            session.add(task)
            return False
        if _sc_verdict == "bumped":
            _sc_loop = _reset_review_loop_pr_head_cache(ci_loop)
            _sc_loop = revoke_review_authority_for_active_fix(
                task,
                review_loop=_sc_loop,
            )
            arm_ci_wait(_sc_loop, pr_number=ci_pr_number, now_iso=_utcnow().isoformat())
            _sc_loop["ci_passed"] = False
            task.context = {
                **(task.context or {}),
                "_size_cap_bumped": _sc_reason,
                "review_loop": _sc_loop,
            }
            flag_modified(task, "context")
            task.latest_status_message = (
                f"Merge deferred — size-cap bumped: {_sc_reason}; awaiting fresh CI"
            )[:500]
            session.add(task)
            return False
        if _sc_verdict == "defer":
            logger.info(
                "[CI] PR #%s size-cap preflight transient error — deferring: %s",
                ci_pr_number,
                _sc_reason,
            )
            return False
        if _sc_verdict != "ok":
            task.latest_status_message = (
                f"Merge held — invalid size-cap verdict: {_sc_verdict!r}"
            )[:500]
            session.add(task)
            return False
        # The merge authority below must submit the very head the cap guard
        # evaluated, not merely the prior CI-loop read of it.
        checked_head_sha = _sc_result.head_sha

    # Empty-effective-diff gate for direct (non-rollup) task PRs. A code-fix /
    # create PR can reach this merge chokepoint with a zero-file effective diff
    # when its commits already landed on the base (a sibling merge / rebase-flat
    # collapsed the branch). Auto-merging such a PR marks the task COMPLETED
    # ('CI passed + review approved, PR merged') while NOTHING user-visible
    # lands (incident 2026-07-12, PR #14500 / commit 683abce38 merged 'green'
    # with an empty ``git show --stat``). Probe the effective diff here — AFTER
    # the migration and size-cap gates so their tests (which patch/raise
    # get_pr_files) reach their existing outcome first — and on a CONFIRMED
    # empty diff route to a distinct SCOPE_EMPTY no-op terminal (PR closed,
    # task NOT marked COMPLETED-success) and return False. An INDETERMINATE
    # probe (None) defers the merge without merging or no-op'ing. Rollup parents
    # handle their own empty-diff case upstream, so skip when rollup_pr_number.
    if ci_pr_number and not (task.context or {}).get("rollup_pr_number"):
        from app.github import pr_effective_diff_is_empty

        _diff_empty = await pr_effective_diff_is_empty(
            gh_token,
            owner,
            repo,
            ci_pr_number,
            project_id=str(task.project_id) if task.project_id else None,
        )
        if _diff_empty is True:
            from app.review_loop_convergence import (
                _handle_empty_effective_diff_at_merge,
            )

            _empty_orig_id = ci_orig_id or str(task.id)
            logger.warning(
                f"[CI] Merge blocked for PR #{ci_pr_number} — empty effective diff "
                f"at merge; closing as a no-op instead of a false success"
            )
            await _handle_empty_effective_diff_at_merge(
                task, _empty_orig_id, ci_pr_number, session
            )
            return False
        if _diff_empty is None:
            logger.info(
                "[CI] PR #%s effective-diff probe indeterminate — deferring merge",
                ci_pr_number,
            )
            return False
        # _diff_empty is False → non-empty diff → fall through to merge.

    # Round 4 W1: capture a content snapshot BEFORE releasing the lock.
    # ``parent_id_local`` mirrors the snapshot-stale convention from
    # rollup_management.py: the in-memory ``task`` is stale after release;
    # only stable identifiers (id, project_id) and the captured snapshot
    # are safe to read between release and re-acquire.
    #
    # The snapshot covers fields the merge gate protects from peer mutation:
    #   - status: must remain EVALUATING for the merge to apply
    #   - review_loop.iteration: review-fix iteration counter; advances if
    #     a fix-completion handler has already moved on
    #   - review_loop.review_approved / verdict: the gate flag itself; if
    #     a peer (e.g. APPROVE handler) cleared/changed it we must defer
    #   - review_loop.ci_passed: the other gate; same logic
    # ``updated_at`` is intentionally NOT in the snapshot — heartbeat
    # ``onupdate=_utcnow`` advances it every 30s and would false-defer
    # the merge under load (audit C3).
    parent_id_local = task.id
    pre_merge_snapshot = _capture_merge_snapshot(task)
    # R4-W1 (audit C1): explicitly mark ``context`` dirty before the commit.
    # The merge-failure path mutates ``task.context`` via full reassignment
    # AFTER the prior populate_existing reset SQLA's change-tracking; in
    # some SQLA states the post-populate assignment is not detected as
    # dirty (verified empirically against test_merge_failure_final_warn:
    # without flag_modified, the second invocation's commit failed to
    # persist the first invocation's dirty mutations). ``flag_modified``
    # is a no-op when the attribute is already tracked, but corrects the
    # state when it isn't.
    flag_modified(task, "context")
    session.add(task)
    await session.commit()  # release task FOR UPDATE lock; SQLA returns
    # the connection to the pool until the next
    # operation auto-acquires (audit H6 — no
    # connection held during the GitHub window).

    from app.review_merge_authority import attempt_merge_with_authority

    authority, merge_result = await attempt_merge_with_authority(
        task,
        session,
        merge_call=lambda expected_head: merge_pull_request(
            gh_token,
            owner,
            repo,
            ci_pr_number,
            expected_head_sha=expected_head,
        ),
        gh_token=gh_token,
        owner=owner,
        repo=repo,
        ci_loop=ci_loop,
        pr_number=int(ci_pr_number),
        branch=ci_branch,
        base_branch=ci_base,
        original_task_id=ci_orig_id,
        checked_head_sha=checked_head_sha,
        checked_base_sha=checked_base_sha,
        retry_nonterminal=source != "webhook",
    )
    if merge_result is None:
        return False
    ci_loop = authority.review_loop

    # ── PRE-RELOCK GITHUB I/O ──────────────────────────────────────────────
    # Round 5 W1 (this commit): all GitHub API calls below this point used to
    # run with the task FOR UPDATE lock held (re-acquired after merge_pull_request).
    # That meant 30-120s of held-lock during create_pr_comment + _post_merge_summary
    # (merged path), get_pull_request (recheck), and update_branch (behind path) —
    # blocking concurrent heartbeats and tripping idle_in_transaction_session_timeout.
    # Production 2026-05-04: same incident shape as agent_status.py:1430 cascade.
    #
    # New structure: do all remaining GitHub I/O while still UNLOCKED, decide the
    # outcome, then re-acquire the lock only to apply the final DB mutation.
    # ──────────────────────────────────────────────────────────────────────

    merge_msg = merge_result.get("message", "")
    _in_progress = (
        "in progress" in merge_msg.lower() or "status check" in merge_msg.lower()
    )

    # If the merge succeeded, post the cosmetic GitHub artefacts (merge banner +
    # summary comment) BEFORE re-acquiring the lock. We use the cached, stale
    # ``task`` instance for _post_merge_summary's read-only field access; the
    # post-relock task assignment will pick up any peer mutation if it happened.
    #
    # Idempotency gate (security audit MEDIUM-1, 2026-05-04): two concurrent
    # merge sweeps (e.g. webhook + CI poll) can both observe merged=True from
    # GitHub (idempotent on the second caller) and would otherwise BOTH post
    # the cosmetic comment + summary, producing duplicates on the PR. Gate on
    # task.context["_merge_comment_posted"] — set inside the relock window
    # below — so the second caller skips. Best-effort; if the relock fails
    # before flagging, a duplicate is still possible but the next caller's
    # gate will catch the third.
    if merge_result.get("merged") and not (task.context or {}).get(
        "_merge_comment_posted"
    ):
        try:
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                ci_pr_number,
                "**Blueprint CI + Review**: All checks passed, PR auto-merged.",
            )
        except Exception as e:
            logger.warning(
                f"[CI] create_pr_comment after merge failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )
        try:
            await _post_merge_summary(
                gh_token, owner, repo, ci_pr_number, task, ci_loop
            )
        except Exception as e:
            logger.warning(
                f"[CI] _post_merge_summary failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )

    # If the merge failed and we're not in the "checks in progress" early-exit
    # path, fetch the PR state to drive escalation logic. Done unlocked so the
    # GitHub round-trip (typ. 1-30s, up to 120s on cutover) doesn't hold FOR UPDATE.
    _recheck_pr: dict | None = None
    _recheck_err: BaseException | None = None
    _ms: str = ""
    _upd: dict | None = None
    if (
        not merge_result.get("merged")
        and not locally_refused_merge_status(merge_result, ci_pr_number)
        and not _in_progress
    ):
        try:
            _recheck_pr = await get_pull_request(gh_token, owner, repo, ci_pr_number)
            _ms = ((_recheck_pr or {}).get("mergeable_state") or "").lower()
        except Exception as e:
            _recheck_err = e
            logger.warning(
                f"[CI] get_pull_request recheck failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )

        # 'behind' path: call update_branch (GitHub write) BEFORE re-acquiring
        # the lock. The result is consumed in the lock-held block below.
        if _recheck_err is None and _ms == "behind":
            from app.github import pr_is_not_behind_for, update_branch

            _not_behind = await pr_is_not_behind_for(
                gh_token,
                owner,
                repo,
                pr_detail=_recheck_pr,
                base_branch=ci_base,
                call_site="ci_completion",
            )
            if _not_behind:
                _upd = {"not_behind": True}
                logger.info(
                    f"[CI] PR #{ci_pr_number} mergeable_state='behind' but "
                    f"pr_is_not_behind=True — skipping update_branch"
                )
            else:
                try:
                    _upd = await update_branch(gh_token, owner, repo, ci_pr_number)
                except Exception as e:
                    _upd = {"updated": False, "message": str(e)[:200]}
                    logger.warning(f"[CI] PR #{ci_pr_number} update_branch raised: {e}")
                else:
                    # Tag the canonical "no new commits on the base branch" 422
                    # so the existing `_upd.get("not_behind")` short-circuit in
                    # the merge-failure branch below treats it as a stale-cache
                    # signal rather than a transient failure that increments
                    # update_branch_failures.
                    from app.github import _tag_not_behind_if_no_new_commits

                    _upd = _tag_not_behind_if_no_new_commits(_upd)

    # ── RE-ACQUIRE FOR UPDATE FOR FINAL DB MUTATIONS ──────────────────────
    # All GitHub I/O above ran with no row lock held. Now re-acquire briefly
    # to apply state mutations (status, context, latest_status_message) and
    # any spawn calls (which do DB work but no further GitHub I/O).
    fresh_task = await _relock_merge_task(
        session,
        parent_id_local,
        pre_merge_snapshot,
        task,
        source=source,
    )
    if fresh_task is None:
        return bool(merge_result.get("merged"))
    # Re-bind ``task`` to the fresh, locked instance — the original is stale.
    task = fresh_task
    ci_loop = (task.context or {}).get("review_loop", {})

    _local_refusal = locally_refused_merge_status(merge_result, ci_pr_number)
    if _local_refusal:
        task.latest_status_message = _local_refusal[:500]
        session.add(task)
        return False
    if merge_result.get("merged"):
        # GitHub artefacts already posted (above). Apply DB state under the lock.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            f"CI passed + review approved, PR #{ci_pr_number} merged ({source})"
        )
        # Idempotency flag for the cosmetic comment post — guards a concurrent
        # merge sweep from posting duplicates (security audit MEDIUM-1).
        task.context = {**(task.context or {}), "_merge_comment_posted": True}
        flag_modified(task, "context")
        if ci_orig_id:
            # C1: the pr_merged write on the ORIGINAL (rollup-orig) task is an
            # independent operation that must NOT share this merge-task
            # ``session``. Two failure modes the old shared-session code had:
            #   (1) a raise here (e.g. session.get hitting a DB error) left the
            #       caller's session in PendingRollback, so the caller's commit
            #       of task.status=COMPLETED silently failed or 500'd; and
            #   (2) the function returned True regardless, so the caller treated
            #       the merge as fully done while the rollup-unblocking
            #       ``pr_merged`` flag was actually dropped — the rollup that
            #       gates on that flag then defers indefinitely.
            # Give it its OWN AsyncSessionLocal scope (mirrors
            # maybe_finalize_qualification / reaper Step 9): it commits the flag
            # independently and an error here is fully contained.
            try:
                from app.branch_resolution import (
                    _signal_blocking_project_branch_configuration,
                )
                from app.database import AsyncSessionLocal
                from app.models import Project
                from app.task_completion import (
                    _finalize_migration_reservation_after_merge,
                )

                # Self-orig guard: on a DIRECT merge (the common lane —
                # research/plan/create tasks merging their own PR),
                # original_task_id points back at THIS task. The dedicated
                # ``_orig_session`` below then UPDATEs the same ``tasks`` row
                # this session still holds FOR UPDATE (relocked above), so its
                # Query-invoked autoflush blocked for the full 30s lock_timeout
                # and died with LockNotAvailableError — silently dropping
                # pr_merged, the direction-outcome refresh, and the migration-
                # reservation finalize on EVERY direct merge (prod 2026-06-01
                # through 06-07, 144 drops). Commit the terminal state
                # NOW (mirrors the CI-failure path's commit-before-accounting)
                # so COMPLETED is durable and the row lock is released before
                # the dedicated session touches the row. If this commit fails,
                # the next CI sweep's merged-PR recheck path re-lands COMPLETED.
                _orig_is_self = False
                with contextlib.suppress(ValueError, TypeError):
                    _orig_is_self = uuid.UUID(str(ci_orig_id)) == task.id
                if _orig_is_self:
                    session.add(task)
                    await session.commit()

                async with AsyncSessionLocal() as _orig_session:
                    orig = await _orig_session.get(Task, uuid.UUID(ci_orig_id))
                    if orig:
                        orig_project = (
                            await _orig_session.get(Project, orig.project_id)
                            if orig.project_id
                            else None
                        )
                        _integration_branch = get_project_integration_branch(
                            orig_project
                        )
                        _promotion_target = get_project_promotion_target(orig_project)
                        _hotfix_lane = bool(
                            (task.first_payload or {}).get("hotfix")
                            or (task.context or {}).get("hotfix")
                            or ci_loop.get("hotfix")
                        )
                        # automain branch-safety signal: a rollup landing on the
                        # promotion target while that branch is distinct from the
                        # integration branch (outside the hotfix lane) is a
                        # misconfiguration — stage the deferred operator alert.
                        if (
                            _promotion_target
                            and ci_base == _promotion_target
                            and ci_base != _integration_branch
                            and not _hotfix_lane
                            and orig_project is not None
                        ):
                            _signal_blocking_project_branch_configuration(
                                orig_project,
                                "rollup_target_matches_promotion_target",
                                (
                                    f"Rollup blocked: target branch '{ci_base}' is this "
                                    "project's promotion target. Configure a distinct "
                                    "integration branch before landing fleet work."
                                ),
                            )
                        # PR feature: custom staged rollup roots stay non-terminal
                        # until they land downstream; promotion-target landings raise
                        # the dedicated landing alert and leave pr_merged unset.
                        _orig_is_custom_staged_rollup = is_custom_staged_rollup_root(
                            orig, orig_project, ci_base
                        )
                        _terminal_rollup_merge = True
                        if _orig_is_custom_staged_rollup and orig_project is not None:
                            _landed_on_integration = ci_base == _integration_branch
                            _landed_on_promotion = ci_base == _promotion_target
                            if not _landed_on_integration and _landed_on_promotion:
                                await merged_pr_counts_as_integration_landing(
                                    orig,
                                    orig_project,
                                    _orig_session,
                                    pr_number=ci_pr_number,
                                    merged_base_branch=ci_base,
                                    source="ci_completion.original_merge_success",
                                )
                            record_pr_merged = _landed_on_integration
                            _terminal_rollup_merge = (
                                _landed_on_integration or _landed_on_promotion
                            )
                        else:
                            record_pr_merged = (
                                await merged_pr_counts_as_integration_landing(
                                    orig,
                                    orig_project,
                                    _orig_session,
                                    pr_number=ci_pr_number,
                                    merged_base_branch=ci_base,
                                    source="ci_completion.original_merge_success",
                                )
                            )
                        if record_pr_merged:
                            # Ancestry gate before stamping the shipped badge:
                            # ``record_pr_merged`` above trusts branch-NAME
                            # equality / promotion-target classification, neither
                            # of which confirms the merge commit actually reached
                            # the integration branch. Verify reachability; a
                            # definitive negative strands the orig (marker + alert
                            # recorded by the gate) instead of stamping, while an
                            # indeterminate result preserves flag-trust.
                            from app.rollup_pr_verification import (
                                verify_rollup_merge_landed,
                            )

                            if (
                                await verify_rollup_merge_landed(
                                    orig,
                                    orig_project,
                                    _orig_session,
                                    pr_number=ci_pr_number,
                                    merge_commit_sha=merge_result.get("sha"),
                                    integration_branch=_integration_branch,
                                    source="ci_completion.original_merge_success",
                                )
                                == "stranded"
                            ):
                                record_pr_merged = False
                        # One representative task id per distinct direction among the
                        # rollup's descendants that this landing newly credits —
                        # populated by the membership propagation below and consumed by
                        # the post-commit direction recompute so multi-direction rollups
                        # heal every affected direction, not just orig's own. Storing the
                        # id (a plain value) rather than the ORM object keeps the loop
                        # safe across a mid-refresh session.rollback() that would expire
                        # every instance and turn a stale attribute read into a
                        # MissingGreenlet lazy-load.
                        _orig_dir_reps: dict = {}
                        if record_pr_merged:
                            # Bridge test coverage data from the CI loop into the
                            # original task's context so the coverage_delta acceptance
                            # gate can consume it during record_code_acceptance_verdict
                            # (called inside apply_merged_landed_signal).
                            _tcov = ci_loop.get("test_coverage_result")
                            if _tcov is not None:
                                _orig_ctx = dict(orig.context or {})
                                _orig_ctx["test_coverage_result"] = _tcov
                                orig.context = _orig_ctx
                            _orig_landed_on_integration = await corroborate_and_apply_merged_landed_signal(
                                orig,
                                orig_project,
                                _orig_session,
                                base_branch=ci_base,
                                hotfix_lane=bool(
                                    (orig.first_payload or {}).get("hotfix")
                                    or (orig.context or {}).get("hotfix")
                                ),
                                non_merge_reason="merged_pr_non_integration_branch",
                                merge_commit_sha=merge_result.get("sha"),
                                pr_number=ci_pr_number,
                                github_credentials=(gh_token, owner, repo),
                            )
                            # Membership-based merge-credit propagation across the
                            # nested-rollup + squash boundary. apply_merged_landed_signal
                            # above stamps only the rollup ITSELF; when it landed on the
                            # integration branch every child whose PR merged onto the
                            # rollup's staging branch also reached the trunk — but
                            # squash-merge leaves each child's own merge commit an
                            # ancestor of NOTHING on integration, so per-task ancestry
                            # reconciliation (verify_rollup_merge_landed) can never
                            # re-credit it. Stamp the landed signal by lineage subtree
                            # instead — idempotent, force-drop aware — mirroring the
                            # rollup-management sweep. Snapshot one representative task
                            # id per distinct direction now (a plain value, safe across
                            # a later session.rollback()) so the recompute below
                            # re-credits each direction's churn / merge-rate counters.
                            if _orig_landed_on_integration:
                                from app.rollup_management import (
                                    _propagate_landed_to_staging_merged_descendants,
                                )

                                _orig_stamped = (
                                    await _propagate_landed_to_staging_merged_descendants(
                                        orig.id, _orig_session
                                    )
                                )
                                for _desc in _orig_stamped:
                                    _desc_ctx = _desc.context or {}
                                    _desc_did = _desc_ctx.get("direction_id")
                                    _desc_rid = _desc_ctx.get("research_task_id")
                                    if (
                                        _desc_did
                                        and _desc_rid
                                        and _desc_did not in _orig_dir_reps
                                    ):
                                        _orig_dir_reps[_desc_did] = _desc.id
                        await _finalize_migration_reservation_after_merge(
                            orig,
                            _orig_session,
                            source_task=task,
                            terminal_merge=_terminal_rollup_merge,
                        )
                        # A merged PR is convergence by definition — release any
                        # stale stuck/standoff/repeat-fingerprint approval hold
                        # so the flag neither lingers on a terminal task nor
                        # 2h-FAILs an EVALUATING parent whose work just landed.
                        from app.review_loop_convergence import (
                            clear_review_escalation_approval_gate,
                        )

                        clear_review_escalation_approval_gate(
                            orig, resolved_by=f"pr_{ci_pr_number}_merged"
                        )
                        if orig.status == TaskStatus.EVALUATING and not (
                            orig.context or {}
                        ).get("review_loop", {}).get("ci_waiting"):
                            # Terminal stamp + ghost-success deliverable guard (PR #17480 class).
                            from app.rollup_deliverable_guard import finalize_rollup_parent_after_merge

                            await finalize_rollup_parent_after_merge(
                                orig, orig_project, _orig_session,
                                terminal=not _orig_is_custom_staged_rollup or _terminal_rollup_merge,
                                base_branch=ci_base, pr_number=ci_pr_number,
                                github_credentials=(gh_token, owner, repo),
                                ci_loop=ci_loop, merge_task=task,
                                source="ci_completion.original_merge_success",
                            )
                        _orig_session.add(orig)
                        await _orig_session.commit()
                        # Refresh the direction's research-history merge counts:
                        # prs_merged was snapshotted when this child COMPLETED
                        # (PR open, not merged); now that it has landed, recompute
                        # so the churn guardrail / lifetime-burn backstop /
                        # productivity scoring see the merge instead of a stale 0.
                        if record_pr_merged:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            await refresh_direction_outcomes_after_merge(
                                orig, _orig_session
                            )
                            # The rollup can carry children in directions OTHER than
                            # orig's own (a multi-direction research cycle). The
                            # refresh above only heals orig's direction; recompute
                            # each DISTINCT direction among the descendants just
                            # stamped landed so its churn / merge-rate counters pick
                            # up the merges that reached the trunk this tick. Deduped
                            # by direction, so a rollup with many same-direction
                            # children recomputes once.
                            #
                            # Re-materialize each rep by id inside the loop:
                            # refresh_direction_outcomes_after_merge rolls the session
                            # back on an internal recompute error, which expires every
                            # ORM instance — a stale rep read on the next iteration
                            # would then lazy-load under the async session and raise
                            # MissingGreenlet. A fresh .get after a rollback reloads
                            # cleanly (or returns None if the row vanished). Isolate
                            # each rep in its own try/except so one direction's failure
                            # can't abort the remaining recomputes for this tick.
                            for _rep_id in _orig_dir_reps.values():
                                try:
                                    _rep = await _orig_session.get(Task, _rep_id)
                                    if _rep is None:
                                        continue
                                    await refresh_direction_outcomes_after_merge(
                                        _rep, _orig_session
                                    )
                                except Exception:
                                    logger.warning(
                                        "[CI] direction recompute failed for rep "
                                        "task %s after rollup merge (non-fatal)",
                                        _rep_id,
                                        exc_info=True,
                                    )
                        # criterion evidence is recorded unconditionally inside
                        # refresh_direction_outcomes_after_merge (direction_outcomes.py)
                        # — no separate call needed here.

            except Exception as e:
                logger.warning(
                    f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True
                )
        # Once a bootstrapped repo gains a project structure on its landed
        # integration branch, refresh an empty build_config so verification
        # commands exist without a manual step. Keep this isolated from the
        # merge-task session because the helper swallows failures via rollback.
        if _merge_project is not None and ci_base == get_project_integration_branch(
            _merge_project
        ):
            await _refresh_empty_build_config_after_integration_landing(
                _merge_project.id,
                ci_base,
            )
        # No-CI UNVERIFIED-merge alert: if the orchestrator decided this merge
        # proceeds without test verification (undetected verdict / kill-switch
        # off / internal staging base / no detectable test command), surface it
        # ONCE to the operator now that the merge actually landed. Deduped per
        # PR inside the helper; isolated so an alert error never undoes the merge.
        _unverified_reason = ci_loop.get("unverified_merge_reason")
        # Onboarding-proof: stamp the first merge-gate terminal verdict for this
        # project. Truthfulness invariant — the gate genuinely executed the
        # repo's own tests ONLY when no unverified-merge reason was recorded;
        # any reason means the gate fell open, so executed_real_tests is False
        # and the fall-open reason is recorded honestly. Best-effort and
        # idempotent; never gates the merge.
        try:
            from app.onboarding_proof import (
                lock_project_for_onboarding_proof,
                stamp_first_merge_gate_verdict,
            )

            _proof_project = await lock_project_for_onboarding_proof(
                _merge_project,
                session,
            )
            if _proof_project is not None:
                _merge_project = _proof_project
                stamp_first_merge_gate_verdict(
                    _merge_project,
                    session,
                    executed_real_tests=_unverified_reason is None,
                    unverified_reason=_unverified_reason,
                    when=_utcnow(),
                )
        except Exception:  # observability stamp never blocks the merge
            logger.warning(
                "[CI] onboarding first-merge-gate-verdict stamp failed",
                exc_info=True,
            )
        if _unverified_reason:
            try:
                from app.alert_dispatch import _severity_for_unverified_reason

                await _maybe_create_unverified_merge_alert(
                    task,
                    session,
                    pr_number=ci_pr_number,
                    reason=_unverified_reason,
                    severity=_severity_for_unverified_reason(_unverified_reason),
                )
            except Exception as _ue:
                logger.warning(
                    f"[CI] PR #{ci_pr_number} unverified-merge alert failed: {_ue}",
                    exc_info=True,
                )
        logger.info(f"[CI] PR #{ci_pr_number} merged ({source})")
        return True

    if _in_progress:
        from app import pause_clock
        from app.shared_state import runtime_config as _rt_cfg

        # Cap repeated ``ci_wait_started`` resets so a PR whose required checks
        # never resolve cannot spin on this path until the 6-hour parent
        # timeout fires.  Each reset bumps the CI-wait deadline by another
        # full backstop window; without a cap the orchestrator polls forever
        # while logging "in progress" responses every cycle.  After the cap
        # we stop resetting (still log + persist the last-merge state) so the
        # normal CI backstop runs out on schedule and the parent fails with a
        # message that names the real blocker.
        _reset_cap = int(_rt_cfg.get("ci_in_progress_reset_cap", 3))
        _resets_so_far = int(ci_loop.get("merge_in_progress_resets", 0))
        # Persist the merge-side diagnostics so the EVALUATING-timeout reason-
        # picker in services.py can surface the real blocker instead of the
        # historical "both gates unmet" lie.
        ci_loop["mergeable_state"] = _ms or "unknown"
        ci_loop["last_merge_msg"] = merge_msg
        if _resets_so_far < _reset_cap:
            ci_loop["ci_poll_interval"] = 10
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            ci_loop["merge_in_progress_resets"] = _resets_so_far + 1
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"Merge blocked (checks re-running on PR #{ci_pr_number}) — back to CI polling "
                f"({_resets_so_far + 1}/{_reset_cap})"
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge blocked by in-progress checks — "
                f"returning to CI wait ({source}, reset {_resets_so_far + 1}/{_reset_cap})"
            )
        else:
            # Cap reached — stop resetting the CI-wait clock.  The normal
            # backstop runs out on schedule and services.py surfaces the
            # mergeable_state / last_merge_msg in the timeout reason.
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"Merge blocked on PR #{ci_pr_number} (in-progress reset cap "
                f"{_reset_cap} reached) — letting backstop run out"
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge in-progress reset cap "
                f"({_reset_cap}) reached — ci_wait_started no longer being reset "
                f"({source})"
            )
        return False

    try:
        # If get_pull_request raised in the unlocked window, surface that as
        # the recheck-failed branch (mirrors the original except-clause behavior).
        if _recheck_err is not None:
            raise _recheck_err
        # C4: the success-close branch MUST key on the affirmative merged==True
        # signal, NEVER on a bare state=='closed'. A closed-not-merged PR may be
        # a TRANSIENT self-close from the orchestrator's mergeability-reset loop
        # (close+reopen to recompute mergeable=None — orchestrator.py). The prior
        # ``state == 'closed'`` arm treated that transient window as a deliberate
        # success-close: it marked the parent COMPLETED with pr_closed_success,
        # which _verify_pr_merged then cements (rollup_management.py) — falsely
        # succeeding an UNMERGED rollup. The CI-poll loop already distinguishes
        # its own self-close via review_loop.pending_reopen; this gate now does
        # too: a closed-not-merged PR with pending_reopen set is OUR reset window
        # → defer (return False) so the reopen completes and the gate retries.
        if _recheck_pr and _recheck_pr.get("merged"):
            task.status = TaskStatus.COMPLETED
            # Mark a deliberate success-close so the rollup's _verify_pr_merged
            # doesn't later re-fetch this now-closed PR, see closed-not-merged,
            # and downgrade this COMPLETED child back to FAILED (the work landed
            # via the other task's PR — desired outcome achieved).
            task.context = {**(task.context or {}), "pr_closed_success": True}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"PR #{ci_pr_number} already merged by another task"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} was already merged — marking complete ({source})"
            )
            return True
        if _recheck_pr and _recheck_pr.get("state") == "closed":
            # Closed but NOT merged. If this is our own mergeability-reset
            # window (pending_reopen), defer — the orchestrator's CI poll will
            # reopen and the gate retries. Otherwise it's a genuine close that
            # happened to coincide with a merge attempt; treat it as a
            # success-close only when an explicit deliberate-close marker is
            # present (pr_closed_empty/pr_closed_success set elsewhere), else
            # leave the parent EVALUATING for _verify_pr_merged to classify.
            _ci_ctx = task.context or {}
            if ci_loop.get("pending_reopen"):
                logger.info(
                    f"[CI] PR #{ci_pr_number} closed-not-merged with pending_reopen set "
                    f"— transient mergeability-reset, deferring merge ({source})"
                )
                return False
            if _ci_ctx.get("pr_closed_empty") or _ci_ctx.get("pr_closed_success"):
                task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} closed (deliberate success-close)"
                )
                logger.info(
                    f"[CI] PR #{ci_pr_number} closed-not-merged but marked deliberate "
                    f"success-close — marking complete ({source})"
                )
                return True
            logger.info(
                f"[CI] PR #{ci_pr_number} closed-not-merged with no self-close marker "
                f"— leaving parent EVALUATING for _verify_pr_merged ({source})"
            )
            return False
        # Detect non-transient merge blocks — escalate immediately instead of retrying.
        # Prefer mergeable_state from the refetched PR as the primary signal: it is
        # authoritative and resilient to GitHub API error-message wording changes.
        # Fall back to keyword matching only when mergeable_state is absent or still
        # 'unknown' (GitHub hasn't finished computing it).
        if _ms and _ms != "unknown":
            _is_rule_block = _ms == "blocked"
        else:
            _merge_msg_lower = merge_msg.lower()
            _is_rule_block = any(
                kw in _merge_msg_lower
                for kw in [
                    "rule violation",
                    "branch protection",
                    "required review",
                    "required status",
                    "merge restrictions",
                    "not allowed to merge",
                    "at least",
                    "approving review",
                ]
            )
        # 'unstable': required status checks are failing or still pending.
        # This is not a merge infrastructure failure — defer back to CI polling
        # without burning the merge retry budget.
        if _ms == "unstable":
            from app import pause_clock

            ci_loop["ci_passed"] = False
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"PR #{ci_pr_number} mergeable_state='unstable' (checks pending/failing) "
                f"— re-entering CI polling"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} unstable — deferring to CI polling ({source})"
            )
            return False

        # 'behind': PR branch is behind the base. update_branch was already
        # called in the unlocked window above; ``_upd`` holds the result.
        if _ms == "behind":
            assert _upd is not None  # set in unlocked block when _ms == "behind"
            from app import pause_clock

            ci_loop["ci_passed"] = False
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            if _upd.get("not_behind"):
                # pr_is_not_behind confirmed head already contains base tip —
                # skip update_branch, re-enter CI polling for a fresh merge attempt.
                ci_loop["update_branch_failures"] = 0
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = (
                    f"PR #{ci_pr_number} reports 'behind' but head already contains"
                    f" base tip — re-entering CI polling without update"
                )
                logger.info(
                    f"[CI] PR #{ci_pr_number} not-behind — skipped update_branch ({source})"
                )
                return False
            if _upd.get("updated"):
                ci_loop["update_branch_failures"] = 0
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = f"PR #{ci_pr_number} branch behind base — update queued, re-entering CI polling"
                logger.info(
                    f"[CI] PR #{ci_pr_number} behind base — update_branch queued ({source})"
                )
                return False
            _ub_failures = ci_loop.get("update_branch_failures", 0) + 1
            ci_loop["update_branch_failures"] = _ub_failures
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            _hard_cap_ub = _merge_hard_cap_for_task(task)
            _merge_failures_so_far = ci_loop.get("merge_failures", 0)
            _ub_threshold = max(1, min(3, _hard_cap_ub - _merge_failures_so_far))
            if _ub_failures < _ub_threshold:
                task.latest_status_message = (
                    f"PR #{ci_pr_number} branch behind base — update_branch failed "
                    f"({_ub_failures}/{_ub_threshold}): {(_upd.get('message') or '')[:120]} — re-entering CI polling"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                    f"({_ub_failures}/{_ub_threshold}): {_upd.get('message')} ({source})"
                )
                return False
            # update_branch failures exceeded dynamic threshold — escalate to fix task with rebase.
            from app.review_pipeline import _spawn_pipeline_task
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Phase F3 (H5): push the per-PR filter into SQL using
            # cast(..., Integer) so the ``ix_task_payloads_pr_number`` partial
            # expression index (alembic 0057) can serve the predicate on
            # Postgres. SQLite seq-scans (test path); Postgres uses the index.
            # Matches the ``cast(Task.payloads[0]["pr_number"], Integer)`` form
            # used in review_pipeline.py:_spawn_pipeline_task to keep both call
            # sites dialect-symmetric. Include ``status_log`` in load_only:
            # the Task event listeners (models.py ~360) read it on every
            # status assignment under async sessions, and an unloaded value
            # triggers MissingGreenlet on downstream mutations even if this
            # site only filters the result set. Drop heavy plan_output JSONB.
            active_tasks = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.status_log,
                                raiseload=True,
                            )
                        )
                        .where(
                            Task.project_id == task.project_id,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                            Task.archived.is_(False),
                            _pr_number_match(session, ci_pr_number),
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_fix = next(
                (
                    t
                    for t in active_tasks
                    if t.id != task.id and t.first_payload.get("type") == "fix"
                ),
                None,
            )
            if existing_fix:
                ci_loop = revoke_review_authority_for_active_fix(
                    task, existing_fix.id, review_loop=ci_loop
                )
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x, "
                    f"fix already active → {existing_fix.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — fix already exists → "
                    f"{existing_fix.id} ({source})"
                )
                session.add(task)
                await session.commit()
                return False
            behind_prompt = (
                f"PR #{ci_pr_number} is behind `{ci_base}` and GitHub's update_branch API "
                f"has failed {_ub_failures} consecutive times. The code review is already approved — "
                f"focus only on rebasing the branch.\n\n"
                f"After cloning:\n"
                f"  1. Run `git fetch origin {ci_base} && git rebase origin/{ci_base}`.\n"
                f"  2. Resolve any conflicts that surface during the rebase.\n"
                f"  3. Re-run lint/type/test suites for the affected files.\n"
                f"  4. Force-push with `--force-with-lease`.\n\n"
                f"Last update_branch error: {(_upd.get('message') or '')[:500]}"
            )
            fix_task, _ = await _spawn_pipeline_task(
                task,
                "fix",
                session,
                ci_pr_number,
                ci_branch,
                ci_base,
                fix_prompt=behind_prompt,
                original_task_id=ci_orig_id,
                fix_reason="update_branch_failed",
                # Phase 2d: post-APPROVE — code review already approved, so the
                # warm chain has ended. The new fix must cold-provision rather
                # than (potentially) reuse a still-warm reviewer VM.
                suppress_warm_handoff=True,
            )
            if fix_task:
                ci_loop = revoke_review_authority_for_active_fix(
                    task,
                    fix_task.id,
                    review_loop=(task.context or {}).get("review_loop") or ci_loop,
                )
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x "
                    f"— spawned fix → {fix_task.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — spawned fix → "
                    f"{fix_task.id} ({source})"
                )
            else:
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x "
                    f"— fix spawn suppressed (iteration cap)"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — fix spawn suppressed ({source})"
                )
            return False

        # Real merge conflict ('dirty' state, or 409 conflict from the merge call).
        # Cannot be resolved by retrying — spawn a fix task with rebase instructions,
        # mirroring the CI-failure spawn path below.
        if merge_result.get("conflict") or _ms == "dirty":
            from app.review_pipeline import (
                _spawn_pipeline_task,
                build_merge_conflict_fix_prompt,
            )
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Dedup guard: if a fix task for this PR is already active, don't spawn another.
            # Mirrors the CI-failure path in handle_ci_completion to prevent duplicate fixes
            # contending on the same PR.
            # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
            # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
            # Postgres. Same dialect-symmetric approach as review_pipeline.py.
            # status_log included to satisfy the Task event listeners under
            # async sessions; plan_output dropped via load_only.
            active_tasks = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.status_log,
                                raiseload=True,
                            )
                        )
                        .where(
                            Task.project_id == task.project_id,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                            Task.archived.is_(False),
                            _pr_number_match(session, ci_pr_number),
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_fix = next(
                (
                    t
                    for t in active_tasks
                    if t.id != task.id and t.first_payload.get("type") == "fix"
                ),
                None,
            )
            if existing_fix:
                ci_loop = revoke_review_authority_for_active_fix(
                    task, existing_fix.id, review_loop=ci_loop
                )
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = f"PR #{ci_pr_number} merge conflict, fix already active → {existing_fix.id}"
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict, fix already exists → {existing_fix.id} ({source})"
                )
                session.add(task)
                await session.commit()
                return False
            conflict_prompt = build_merge_conflict_fix_prompt(
                ci_pr_number, ci_base, merge_error=merge_msg
            )
            # Defense-in-depth (incident 2026-05-26..28): prefer the PR's
            # actual head ref over our parent-task-derived ``ci_branch`` when
            # they diverge.  The agent always pushes to ``blueprint/...`` but
            # the orchestrator derives from per-project namespace, so a
            # misconfigured ``task_branch_namespace`` strands the fix-task's
            # initial ``git clone --branch`` against a nonexistent ref.  The
            # PR is the source of truth for the branch we're trying to fix.
            _pr_head_ref = (_recheck_pr or {}).get("head", {}).get("ref", "")
            _spawn_branch = ci_branch
            if _pr_head_ref and ci_branch and _pr_head_ref != ci_branch:
                logger.warning(
                    "[CI] PR #%s branch drift: derived=%r vs PR.head.ref=%r — "
                    "using PR head ref for fix-spawn to avoid clone-against-missing-branch",
                    ci_pr_number,
                    ci_branch,
                    _pr_head_ref,
                )
                _spawn_branch = _pr_head_ref
            fix_task, _ = await _spawn_pipeline_task(
                task,
                "fix",
                session,
                ci_pr_number,
                _spawn_branch,
                ci_base,
                fix_prompt=conflict_prompt,
                original_task_id=ci_orig_id,
                fix_reason="merge_conflict",
                # Phase 2d: post-APPROVE — chain has ended at review approval,
                # the rebase fix must cold-provision (release-on-APPROVE).
                suppress_warm_handoff=True,
            )
            if fix_task:
                ci_loop = revoke_review_authority_for_active_fix(
                    task,
                    fix_task.id,
                    review_loop=(task.context or {}).get("review_loop") or ci_loop,
                )
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} merge conflict — spawned fix → {fix_task.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict — spawned fix → {fix_task.id} ({source})"
                )
            else:
                task.latest_status_message = f"PR #{ci_pr_number} merge conflict — fix spawn suppressed (iteration cap)"
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict — fix spawn suppressed ({source})"
                )
            return False

        if _is_rule_block:
            from app.shared_state import runtime_config as _rt_cfg

            # Discretionary gate: only approval-gate when require_approval_gates is on.
            # With gates off the status message + alert below still surface the block
            # (visibility preserved), but we do NOT set requires_approval — otherwise
            # services.py's 2h approval-gate timeout FAILs the CI task and, via the
            # orig escalation, strands the parent. The merge stays rule-blocked; the
            # normal backstop applies. (Mirrors the rollup review-cap/respawn-cap gating.)
            _gates_on = _rt_cfg.get("require_approval_gates", True)
            task.latest_status_message = (
                f"CI passed but merge blocked on PR #{ci_pr_number}: "
                f"{merge_msg[:200]} — requires manual intervention"
            )
            if _gates_on:
                _establish_ci_approval_hold(task, reason="merge_rule_blocked")
            ci_loop["merge_rule_blocked"] = True
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            session.add(task)
            if ci_orig_id and _gates_on:
                try:
                    orig = await session.get(Task, uuid.UUID(ci_orig_id))
                    if orig:
                        _establish_ci_approval_hold(
                            orig, reason="merge_rule_blocked"
                        )
                        session.add(orig)
                except Exception as e:
                    logger.warning(
                        f"[CI] Failed to set requires_approval on {ci_orig_id}: {e}",
                        exc_info=True,
                    )
            await _maybe_create_failure_alert(
                task,
                session,
                f"PR #{ci_pr_number} merge blocked by repository rules — needs manual action",
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge blocked by repo rules — escalated ({source})"
            )
        else:
            _merge_failures = ci_loop.get("merge_failures", 0) + 1
            ci_loop["merge_failures"] = _merge_failures
            ci_loop["ci_poll_interval"] = min(30 * _merge_failures, 120)
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            # Track repeated-identical-error streak for early exit: if the merge
            # API keeps returning the same error, retrying won't change anything.
            _prev_err = ci_loop.get("merge_last_error")
            _norm_cur = _normalize_merge_error(merge_msg)
            _norm_prev = (
                _normalize_merge_error(_prev_err) if _prev_err is not None else None
            )
            if _norm_prev is not None and _norm_prev == _norm_cur:
                _err_streak = ci_loop.get("merge_error_streak", 1) + 1
            else:
                _err_streak = 1
            ci_loop["merge_last_error"] = merge_msg
            ci_loop["merge_error_streak"] = _err_streak
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            task.latest_status_message = f"Merge attempt {_merge_failures} failed on PR #{ci_pr_number}: {merge_msg} — retrying"
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge failed ({_merge_failures}x): {merge_msg} — will retry ({source})"
            )

            # Cross-PR systemic merge-blocker detection. If ≥N distinct PRs in
            # this project hit the same normalized error within the window,
            # pause retries (they won't succeed until the shared cause clears)
            # and raise a single alert surfacing the shared pattern. Resumes
            # automatically once the window ages out stale occurrences.
            from app.shared_state import (
                record_merge_error_occurrence,
                MERGE_PATTERN_MIN_PRS,
            )

            _project_key = str(task.project_id) if task.project_id else ""
            _pattern_prs = (
                record_merge_error_occurrence(_project_key, _norm_cur, ci_pr_number)
                if (_project_key and _norm_cur and ci_pr_number is not None)
                else 0
            )
            if _pattern_prs >= MERGE_PATTERN_MIN_PRS:
                ci_loop["merge_pattern_paused"] = True
                ci_loop["merge_pattern_pr_count"] = _pattern_prs
                ci_loop["ci_poll_interval"] = 120
                _already_alerted = ci_loop.get("merge_pattern_alerted")
                ci_loop["merge_pattern_alerted"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = (
                    f"PR #{ci_pr_number} merge paused — systemic pattern: "
                    f"{_pattern_prs} PRs blocked by same error "
                    f"({merge_msg[:100]}) — retries deferred"
                )
                if not _already_alerted:
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"Systemic merge blocker in project: {_pattern_prs} PRs "
                        f"failing with the same error — check branch protection "
                        f"rules and CI environment. Error: {merge_msg[:200]}",
                    )
                logger.warning(
                    f"[CI] Cross-PR merge pattern detected — pausing retries "
                    f"for PR #{ci_pr_number} ({_pattern_prs} PRs affected) ({source})"
                )
                return False

            _hard_cap = _merge_hard_cap_for_task(task)
            _escalate_at = max(1, _hard_cap - MERGE_ESCALATE_BEFORE_CAP)
            _deterministic = _is_deterministic_merge_error(_norm_cur)
            _repeated_identical = _err_streak >= 2

            # Spawn a rebase fix the first time a persistent merge failure appears
            # (internal 5/15/45s backoff above has already absorbed the stale-flag
            # window, so a failure that makes it here is either a real moving-parent
            # conflict or a true block).  Only one fix active per PR at a time;
            # the active-fix check is the gate — no counter, no sticky flag, so
            # if the fix completes and the next merge still fails, a fresh fix is
            # spawned.  Rule blocks are handled in the _is_rule_block branch above
            # and never reach here.  Skip rebase-fix spawn when the early-exit
            # conditions below would FAIL the task anyway (deterministic error,
            # repeated-identical streak, or hard-cap reached) — a rebase can't
            # recover from those and the test suite verifies those paths exit
            # cleanly without spawning.  Also skip when GitHub reports the PR as
            # 'clean' — the merge failure is a transient API hiccup, not a real
            # conflict; spawning a rebase fix against a clean PR is always wrong.
            if not (
                _deterministic
                or _repeated_identical
                or _merge_failures >= _hard_cap
                or _ms == "clean"
            ):
                from app.review_pipeline import _spawn_pipeline_task

                # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
                # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
                # Postgres. status_log included for the Task event listeners
                # under async sessions; plan_output dropped via load_only.
                active_tasks = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.status,
                                    Task.context,
                                    Task.payloads,
                                    Task.project_id,
                                    Task.status_log,
                                    raiseload=True,
                                )
                            )
                            .where(
                                Task.project_id == task.project_id,
                                Task.status.in_(
                                    [
                                        TaskStatus.QUEUED,
                                        TaskStatus.PROVISIONING,
                                        TaskStatus.IN_PROGRESS,
                                        TaskStatus.EVALUATING,
                                    ]
                                ),
                                Task.archived.is_(False),
                                _pr_number_match(session, ci_pr_number),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                existing_fix = next(
                    (
                        t
                        for t in active_tasks
                        if t.id != task.id and t.first_payload.get("type") == "fix"
                    ),
                    None,
                )
                if existing_fix is not None:
                    ci_loop = revoke_review_authority_for_active_fix(
                        task, existing_fix.id, review_loop=ci_loop
                    )
                    task.latest_status_message = (
                        f"Merge held on PR #{ci_pr_number}: rebase fix "
                        f"{existing_fix.id} already active"
                    )
                    session.add(task)
                    await session.commit()
                    return False
                else:
                    rebase_prompt = (
                        f"PR #{ci_pr_number} was approved and CI passed, but GitHub rejected the merge: "
                        f"{merge_msg[:300]}\n\n"
                        f"The base branch ({ci_base}) likely advanced since this PR's last rebase "
                        f"(sibling PRs merging into the same parent).  Rebase onto the latest "
                        f"{ci_base} so the PR moves forward:\n\n"
                        f"1. git fetch origin {ci_base}\n"
                        f"2. git rebase origin/{ci_base}\n"
                        f"3. If no conflicts and no commits were replayed, the PR is already "
                        f"up-to-date — GitHub's mergeability flag is just stale.  Report no "
                        f"changes needed (don't force-push); retries will clear it.\n"
                        f"4. Otherwise resolve conflicts and git push --force-with-lease.\n"
                    )
                    fix_task, _ = await _spawn_pipeline_task(
                        task,
                        "fix",
                        session,
                        ci_pr_number,
                        ci_branch,
                        ci_base,
                        fix_prompt=rebase_prompt,
                        original_task_id=ci_orig_id,
                        # Phase 2d post-APPROVE: approved + CI passed, merge rejected — a
                        # pure rebase (conflict-only reason), and it must cold-provision.
                        fix_reason="merge_conflict",
                        suppress_warm_handoff=True,
                    )
                    if fix_task:
                        ci_loop = revoke_review_authority_for_active_fix(
                            task,
                            fix_task.id,
                            review_loop=(task.context or {}).get("review_loop")
                            or ci_loop,
                        )
                        from app.rollup_state import _is_rollup_parent_awaiting_merge

                        if not _is_rollup_parent_awaiting_merge(task.context):
                            task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Merge blocked on PR #{ci_pr_number} ({merge_msg[:100]}) — spawned rebase fix → {fix_task.id}"
                        logger.info(
                            f"[CI] PR #{ci_pr_number} merge blocked, spawned rebase fix → {fix_task.id} ({source})"
                        )
                        session.add(task)
                        await session.commit()
                        return True

            # Single escalation rung: one deduped operator alert when the PR
            # first crosses _escalate_at, surfacing both that it's stuck and how
            # close the terminal hard-cap is. The ``merge_escalated`` latch makes
            # it fire exactly once — every later failure up to the cap is silent
            # (the operator already knows; the next signal is the hard-cap FAIL
            # below). Replaces the former two-rung "still retrying" + "imminent"
            # ladder, which double-alerted the same stuck PR for no added signal.
            if _merge_failures >= _escalate_at and not ci_loop.get("merge_escalated"):
                ci_loop["merge_escalated"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"PR #{ci_pr_number} merge failed {_merge_failures} times — "
                    f"still retrying ({_hard_cap - _merge_failures} attempts before "
                    f"hard cap, then manual action required)",
                )
            if _merge_failures >= _hard_cap or _repeated_identical or _deterministic:
                from app.shared_state import runtime_config as _rt_cfg

                # Gate only the approval flag — FAILED is the terminalization and stays.
                # With gates off the task still FAILs (with its status message + alert),
                # but does not approval-gate the orig/parent (which services.py would
                # otherwise 2h-timeout-FAIL). Mirrors the rollup review-cap gating.
                _gates_on = _rt_cfg.get("require_approval_gates", True)
                task.status = TaskStatus.FAILED
                if _gates_on:
                    _establish_ci_approval_hold(
                        task, reason="merge_retry_exhausted"
                    )
                _early_exit = (
                    _repeated_identical or _deterministic
                ) and _merge_failures < _hard_cap
                if _early_exit:
                    _retries_saved = _hard_cap - _merge_failures
                    task.context = {
                        **(task.context or {}),
                        "_merge_retries_saved": _retries_saved,
                        "review_loop": ci_loop,
                    }
                    if _deterministic and not _repeated_identical:
                        task.latest_status_message = (
                            f"PR #{ci_pr_number} merge failed with deterministic "
                            f"non-retryable error: {merge_msg[:200]} — "
                            f"giving up, needs manual intervention"
                        )
                    else:
                        task.latest_status_message = (
                            f"PR #{ci_pr_number} merge failed with identical error "
                            f"{_err_streak} times in a row: {merge_msg[:200]} — "
                            f"giving up, needs manual intervention"
                        )
                else:
                    task.latest_status_message = (
                        f"PR #{ci_pr_number} merge failed {_merge_failures} times "
                        f"(>= {_hard_cap}) — giving up, needs manual intervention"
                    )
                if ci_orig_id and _gates_on:
                    try:
                        orig = await session.get(Task, uuid.UUID(ci_orig_id))
                        if orig:
                            _establish_ci_approval_hold(
                                orig, reason="merge_retry_exhausted"
                            )
                            session.add(orig)
                    except Exception as e:
                        logger.warning(
                            f"[CI] Failed to set requires_approval on {ci_orig_id}: {e}",
                            exc_info=True,
                        )
                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"PR #{ci_pr_number} merge hard-capped after {_merge_failures} failures — manual action required",
                )
                logger.error(
                    f"[CI] PR #{ci_pr_number} hit merge hard cap ({_merge_failures}) — marking FAILED ({source})"
                )
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )
                from app.routers.agent_status import record_task_result
                from app.shared_state import record_direction_result

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(
                    success=False,
                    failure_class=_fc,
                    task_id=str(task.id),
                    task_type=task.first_payload.get("type", ""),
                    task_context=dict(task.context or {}),
                    auto_remediation=task.auto_remediation,
                )
                _direction_id = str((task.context or {}).get("direction_id") or "")
                if _direction_id:
                    record_direction_result(
                        _direction_id,
                        success=False,
                        failure_class=_fc,
                        project_id=str(task.project_id) if task.project_id else "",
                    )
                if _early_exit and task.project_id:
                    from app.broadcast import broadcast_to_project

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {
                            "task_id": str(task.id),
                            # task.status was set to FAILED at the top of this
                            # hard-cap branch; use the literal so a possibly-expired
                            # instance (failure-alert DB op above) isn't lazy-loaded.
                            "status": TaskStatus.FAILED,
                            "details": {
                                "merge_early_exit": True,
                                "retries_saved": _retries_saved,
                                "error_streak": _err_streak,
                            },
                        },
                    )
    except Exception:
        ci_loop["ci_poll_interval"] = 30
        ci_loop["ci_last_polled"] = _utcnow().isoformat()
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        task.latest_status_message = (
            f"Merge check failed on PR #{ci_pr_number} — retrying"
        )
        logger.warning(
            f"[CI] PR #{ci_pr_number} merge recheck failed — will retry ({source})"
        )
    return False


_REVIEW_WAIT_SPAWN_CLAIM_KEY = "review_wait_spawn_claim"


def _owns_review_wait_spawn_claim(
    task: Task,
    claim: dict[str, object],
) -> bool:
    """Validate one revocable, exact PR-attempt spawn claim on a fresh row."""

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    try:
        current_pr_number = effective_pr_number(task)
        claimed_pr_number = int(claim.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    from app.review_wait_spawn_authority import (
        review_wait_spawn_claim_gate_is_current,
    )

    return bool(
        task.status == TaskStatus.EVALUATING
        and task.archived is not True
        and review_wait_spawn_claim_gate_is_current(
            task,
            claim,
            review_loop=review_loop,
        )
        and str(task.project_id) == str(claim.get("project_id") or "")
        and current_pr_number == claimed_pr_number
        and review_loop.get(_REVIEW_WAIT_SPAWN_CLAIM_KEY) == claim
        and review_loop.get("review_wait_started")
        == claim.get("review_wait_started")
        and str(review_loop.get("ci_checked_head_sha") or "")
        == str(claim.get("head_sha") or "")
        and str(review_loop.get("ci_checked_base_sha") or "")
        == str(claim.get("base_sha") or "")
        and review_loop.get("ci_passed") is True
        and not _review_approved(review_loop)
        and (
            not review_loop.get("review_wait_escalated")
            or claim.get("review_wait_approval_hold") is not None
        )
        and context.get("approval_hold_reason")
        == claim.get("approval_hold_reason")
        and context.get("_approval_set_at") == claim.get("approval_set_at")
        and context.get("touched_critical") == claim.get("touched_critical")
    )


async def _reconcile_review_wait_backstop_spawn(
    *,
    task: Task,
    session: AsyncSession,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_prompt: str,
    original_task_id: str,
    review_target: dict[str, str | int],
    review_wait_started: str,
) -> tuple[Task | None, str, str | None]:
    """Run the canonical review spawn outside the CI task's caller lock.

    ``_spawn_pipeline_task`` owns commits/rollbacks on several paths.  Persist
    the current wait snapshot and release the caller lock first, then relock and
    validate that the same immutable PR attempt still owns the row before the
    caller records any outcome. The returned task is locked unless the outcome
    is ``stale`` (whose transaction is released here).
    """

    from app.review_pipeline import (
        SPAWN_REASON_ADMISSION_PAUSED,
        SPAWN_REASON_ALREADY_MERGED,
        SPAWN_REASON_CONTENDED,
        SPAWN_REASON_DEDUP,
        SPAWN_REASON_DEFERRED,
        SPAWN_REASON_KEY,
        _PIPELINE_ACTIVE_STATUSES,
        _spawn_pipeline_task,
    )
    from app.review_wait_spawn_authority import (
        release_completed_review_wait_gate_owned_by_spawn_claim,
        release_review_wait_gate_owned_by_spawn_claim,
    )

    source_task_id = task.id
    source_context = dict(task.context or {})
    source_review_loop = dict(source_context.get("review_loop") or {})
    from app.closed_pr_reconciliation import REVIEW_WAIT_APPROVAL_HOLD_KEY

    spawn_claim: dict[str, object] = {
        "token": str(uuid.uuid4()),
        "project_id": str(task.project_id),
        "pr_number": int(pr_number),
        "base_sha": str(review_target["base_sha"]),
        "head_sha": str(review_target["head_sha"]),
        "review_wait_started": review_wait_started,
        "approval_set_at": source_context.get("_approval_set_at"),
        "approval_hold_reason": source_context.get("approval_hold_reason"),
        # Legacy rows may store a boolean rather than a file list. Preserve the
        # exact JSON shape so the claim remains both compatible and revocable.
        "touched_critical": copy.deepcopy(source_context.get("touched_critical")),
        REVIEW_WAIT_APPROVAL_HOLD_KEY: copy.deepcopy(
            source_review_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)
        ),
    }
    # Persist a revocable, attempt-scoped claim before releasing the source row.
    # Replacement reset clears unknown attempt fields by default; a peer
    # backstop overwrites the token. Either action invalidates this spawner.
    claimed_context = source_context
    claimed_context.pop(SPAWN_REASON_KEY, None)
    claimed_loop = dict(claimed_context.get("review_loop") or {})
    claimed_loop[_REVIEW_WAIT_SPAWN_CLAIM_KEY] = spawn_claim
    # If cleanup itself finds a peer lock, this committed claim still leaves the
    # scheduler on a short retry rather than the parked-review 120s+ ramp.
    claimed_loop["ci_poll_interval"] = 30
    task.context = {
        **claimed_context,
        "review_loop": claimed_loop,
    }
    flag_modified(task, "context")
    session.add(task)
    await session.commit()

    spawned_task_id = None
    was_created = False
    spawn_failed = False
    observed_spawn_reason = None
    authority_state = {"granted": False, "source_task": None}

    async def _acquire_exact_source_authority(
        authority_session: AsyncSession,
    ) -> Task | None:
        authoritative_task = (
            (
                await authority_session.execute(
                    select(Task)
                    .where(Task.id == source_task_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if authoritative_task is None or not _owns_review_wait_spawn_claim(
            authoritative_task, spawn_claim
        ):
            return None
        authority_state["granted"] = True
        authority_state["source_task"] = authoritative_task
        return authoritative_task

    try:
        spawned_task, was_created = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=original_prompt,
            original_task_id=original_task_id,
            review_target=review_target,
            source_spawn_authority=_acquire_exact_source_authority,
        )
        spawned_task_id = getattr(spawned_task, "id", None)
        # Some canonical terminal branches commit their alert/escalation work,
        # release Project/PR/Task authority, and only then annotate the old
        # source ORM identity with a spawn reason. A replacement/approval may
        # win that released window. Capture the return metadata without
        # triggering a lazy load, then roll back every uncommitted return-path
        # annotation so no post-commit stale state can flush over that peer.
        returned_source = authority_state.get("source_task") or task
        returned_context = returned_source.__dict__.get("context")
        if isinstance(returned_context, dict):
            observed_spawn_reason = returned_context.get(SPAWN_REASON_KEY)
        # The canonical spawner commits every authoritative child/escalation
        # action itself. Its uncommitted return state is source bookkeeping only
        # (DEDUP/DEFERRED/terminal reason), which this wrapper captured above and
        # must never commit from a released identity.
        session.expire(returned_source)
        await session.rollback()
    except Exception as exc:  # noqa: BLE001 - transient retry is fail-closed
        spawn_failed = True
        await session.rollback()
        logger.warning(
            "[CI] PR #%s review-wait canonical spawn failed; deferring safely: %s",
            pr_number,
            exc,
            exc_info=True,
        )

    fresh_task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == source_task_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if fresh_task is None:
        await session.rollback()
        return (None, "stale", None)

    if not _owns_review_wait_spawn_claim(fresh_task, spawn_claim):
        # Preserve any peer state; consume the exact gate only when canonical
        # already-merged completion committed while holding this same claim.
        fresh_context = dict(fresh_task.context or {})
        fresh_loop = dict(fresh_context.get("review_loop") or {})
        if fresh_loop.get(_REVIEW_WAIT_SPAWN_CLAIM_KEY) == spawn_claim:
            if observed_spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                release_completed_review_wait_gate_owned_by_spawn_claim(fresh_task, spawn_claim)
                fresh_context = dict(fresh_task.context or {})
                fresh_loop = dict(fresh_context.get("review_loop") or {})
            fresh_loop.pop(_REVIEW_WAIT_SPAWN_CLAIM_KEY, None)
            fresh_task.context = {**fresh_context, "review_loop": fresh_loop}
            flag_modified(fresh_task, "context")
            session.add(fresh_task)
        await session.commit()
        return (None, "stale", None)

    fresh_loop = dict((fresh_task.context or {}).get("review_loop") or {})
    fresh_loop.pop(_REVIEW_WAIT_SPAWN_CLAIM_KEY, None)
    fresh_task.context = {
        **(fresh_task.context or {}),
        "review_loop": fresh_loop,
    }
    flag_modified(fresh_task, "context")
    session.add(fresh_task)

    if spawn_failed or not authority_state["granted"]:
        return (fresh_task, "retry", None)

    if was_created and spawned_task_id is not None:
        release_review_wait_gate_owned_by_spawn_claim(fresh_task, spawn_claim)
        return (fresh_task, "created", str(spawned_task_id))
    if not was_created and spawned_task_id is not None:
        candidate = None
        if spawned_task_id is not None:
            candidate = await session.get(
                Task, spawned_task_id, populate_existing=True
            )
        candidate_loop = (
            dict((candidate.context or {}).get("review_loop") or {})
            if candidate is not None
            else {}
        )
        exact_live_review = (
            candidate is not None
            and candidate.project_id == fresh_task.project_id
            and candidate.archived is not True
            and candidate.status in _PIPELINE_ACTIVE_STATUSES
            and candidate.first_payload.get("type") == "review"
            and candidate_loop.get("review_target") == review_target
        )
        if exact_live_review:
            release_review_wait_gate_owned_by_spawn_claim(fresh_task, spawn_claim)
            return (fresh_task, "dedup", str(candidate.id))
        return (fresh_task, "invalid_dedup", None)
    spawn_reason = observed_spawn_reason
    if spawn_reason == SPAWN_REASON_ALREADY_MERGED:
        release_review_wait_gate_owned_by_spawn_claim(fresh_task, spawn_claim)
        return (fresh_task, "already_merged", None)
    if spawn_reason == SPAWN_REASON_DEFERRED:
        return (fresh_task, "deferred", None)
    if spawn_reason == SPAWN_REASON_ADMISSION_PAUSED:
        return (fresh_task, "paused", None)
    if spawn_reason == SPAWN_REASON_CONTENDED:
        return (fresh_task, "retry", None)
    # A source-neutral suppression before authority (for example PR-lock
    # contention) is always transient. Only a canonical reason written while
    # the exact source lock was held may close the autonomous retry path.
    if authority_state["granted"] and spawn_reason not in (
        None,
        SPAWN_REASON_DEDUP,
    ):
        return (fresh_task, "terminal", None)
    return (fresh_task, "retry", None)


async def handle_ci_completion(
    task: Task,
    ci_result: dict,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str = "poll",
    checked_head_sha: str | None = None,
    checked_base_sha: str | None = None,
) -> bool:
    """Process CI completion for a ci_waiting task.

    Args:
        task: The EVALUATING task with ci_waiting=True
        ci_result: Result from get_check_runs (status, conclusion, failed_logs, details)
        gh_token: GitHub token for API calls
        owner, repo: GitHub repo coordinates
        session: DB session
        source: "webhook" or "poll" for logging

    Returns:
        True if an action was taken (merge, fix spawn, etc.), False if CI still pending.
    """
    from app.review_pipeline import _spawn_pipeline_task
    from app.github import create_pr_comment
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.broadcast import broadcast_to_project
    from app.models import Project
    from app.shared_state import get_project_integration_branch

    # Serialize concurrent callers (webhook + poller) by locking the task row.
    # skip_locked: if another handler already holds the lock, exit — it will
    # handle the CI result.
    locked_task = (
        (
            await session.execute(
                select(Task).where(Task.id == task.id).with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if locked_task is None:
        logger.info(
            f"[CI] Skipping CI completion for task {task.id} — lock held by concurrent handler ({source})"
        )
        return False
    task = locked_task

    # Identity snapshot: a rollback inside the failure-class helpers further
    # below can expire ``task`` (a rollback defers every attr even with
    # expire_on_commit=False), so the final broadcast must not re-read it.
    # id/project_id are immutable — capture them now while ``task`` is fresh.
    _bcast_task_id = str(task.id)
    _bcast_project_id = str(task.project_id) if task.project_id else ""
    # Broadcast status for the shared tail broadcast below. The fix-spawned path
    # pre-sets this (it may have expired ``task`` via _spawn_pipeline_task's
    # rollback, so it must not re-read task.status); every other path leaves it
    # None and the tail falls back to a fresh task.status read.
    _bcast_status = None

    # Re-check: task may have been completed by the concurrent handler that held the lock
    if task.status != TaskStatus.EVALUATING:
        logger.info(
            f"[CI] Task {task.id} no longer EVALUATING (now {task.status}) — skipping ({source})"
        )
        return False

    ci_loop = (task.context or {}).get("review_loop", {})
    # Canonical PR-identity resolve (review_loop.pr_number OR
    # context.rollup_pr_number). Stamp it back so the merge path below and the
    # recovery sweeps agree — a rollup whose identity lived only at
    # context.rollup_pr_number is no longer skipped by the merger.
    ci_pr_number = effective_pr_number(task)
    if ci_pr_number and not ci_loop.get("pr_number"):
        ci_loop["pr_number"] = ci_pr_number
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
    ci_payload = task.first_payload
    ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
    _ci_project = (
        await session.get(Project, task.project_id) if task.project_id else None
    )
    ci_base = (
        ci_loop.get("base_branch", "")
        or ci_payload.get("base_branch", "")
        or get_project_integration_branch(_ci_project)
    )
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get(
        "original_task_id", ""
    )

    if ci_result["status"] != "completed":
        return False

    if ci_result["conclusion"] == "success":
        # Record the CI gate signal. JSONB columns don't track in-place dict
        # mutations — flag_modified ensures the change is flushed to disk.
        ci_loop["ci_passed"] = True
        # Legacy fix-completion code used this marker to request a pre-backstop
        # review retry. That path is intentionally retired: exact review
        # reconciliation begins only at the 60-minute backstop.
        _checked_head_sha = str(
            checked_head_sha
            or ci_loop.get("ci_checked_head_sha")
            or ci_loop.get("head_sha")
            or ""
        )
        _checked_base_sha = str(
            checked_base_sha or ci_loop.get("ci_checked_base_sha") or ""
        )
        if _checked_head_sha:
            ci_loop["ci_checked_head_sha"] = _checked_head_sha
        if _checked_base_sha:
            ci_loop["ci_checked_base_sha"] = _checked_base_sha
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")

        # A prior authoritative-gate attempt may have failed before it could
        # resolve or spawn the exact live target. In that state there is no
        # review task to wait for, so re-enter the merge chokepoint on the next
        # CI signal instead of parking until the generic one-hour backstop.
        _authoritative_retry_needed = bool(
            ci_loop.get("authoritative_review_required") is True
            and not ci_loop.get("authoritative_review_task_id")
        )
        # A legacy scalar APPROVE used to open this gate directly.  It is no
        # longer authority, but simply parking it here would strand an
        # in-flight pre-ledger task until the generic orphan backstop.  Route
        # that one legacy approval shape through the authoritative boundary so
        # it is replaced immediately by an exact-target review.  Other legacy
        # verdicts remain closed and follow their normal review/fix lifecycle.
        _legacy_approval_needs_exact_review = bool(
            ci_loop.get("verdict") == "APPROVE"
            and not _review_approved(ci_loop)
        )
        if (
            not _review_approved(ci_loop)
            and not _authoritative_retry_needed
            and not _legacy_approval_needs_exact_review
        ):
            # Review hasn't approved yet — leave parked, just persist the signal.
            # The review task's APPROVE handler will write review_approved on the
            # parent and trigger the gate when it runs.
            _review_wait_entry_status = task.latest_status_message
            task.latest_status_message = (
                f"CI passed on PR #{ci_pr_number} — waiting for review approval"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} CI passed, waiting for review approval ({source})"
            )
            # Stamp ci_last_polled so the orchestrator's per-task throttle
            # engages on the NEXT cycle instead of re-polling immediately.
            # Use a longer interval for parked PRs — review approval is driven
            # by an event (review task APPROVE), not by rapid polling.
            _REVIEW_WAIT_POLL_INTERVAL = 120  # seconds
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_poll_interval"] = max(
                ci_loop.get("ci_poll_interval", 30), _REVIEW_WAIT_POLL_INTERVAL
            )
            # Track when we first entered review-wait so the backstop below
            # can detect an orphaned review task.
            if not ci_loop.get("review_wait_started"):
                ci_loop["review_wait_started"] = _utcnow().isoformat()
                # Per-episode respawn budget: when a new review-wait episode
                # begins AND a fix has been applied since the last episode,
                # reset the backstop's per-episode count so a respawn consumed
                # in an earlier episode does not permanently exhaust the budget.
                if ci_loop.get("ci_fix_spawned"):
                    ci_loop.pop("review_wait_respawn_count", None)
                    ci_loop.pop("review_wait_deferred_at", None)

            # Orphan-review backstop: after 60 minutes, reconcile through the
            # canonical exact-target spawner. The spawner can commit/rollback,
            # so the helper below first releases this caller's row lock, then
            # relocks and revalidates the immutable PR attempt before recording
            # CREATED/DEDUP/DEFERRED. No first-green spawn occurs here.
            _REVIEW_WAIT_TIMEOUT_MIN = 60
            _REVIEW_WAIT_DEFER_GRACE_S = 5 * 60
            _park_ctx_snapshot = dict(task.context or {})
            _rw_started = ci_loop.get("review_wait_started")
            if _rw_started and (
                not ci_loop.get("review_wait_escalated")
                or task.requires_approval is True
            ):
                try:
                    from datetime import datetime

                    from app.shared_state import runtime_config as _rt_cfg

                    _rw_dt = datetime.fromisoformat(_rw_started)
                    _rw_age_s = (_utcnow() - _rw_dt).total_seconds()
                    if _rw_age_s > _REVIEW_WAIT_TIMEOUT_MIN * 60:
                        _respawned = False
                        _respawn_outcome = "terminal"
                        _respawn_task_id = None
                        _auto_respawn = _rt_cfg.get("review_wait_auto_respawn", True)
                        _respawn_count = int(
                            ci_loop.get("review_wait_respawn_count", 0) or 0
                        )
                        _respawn_cap = int(
                            _rt_cfg.get("review_wait_respawn_cap", 1)
                        )

                        # Per-episode respawn budget: if the previously respawned
                        # review was superseded (CANCELLED/FAILED) before reaching
                        # a terminal verdict, it should not consume the current
                        # episode's budget. This prevents one consumed respawn in
                        # an earlier review-wait episode from permanently starving
                        # a later episode after a fix has landed new changes.
                        if (
                            _respawn_count >= _respawn_cap
                            and _respawn_count > 0
                        ):
                            _prev_id = ci_loop.get(
                                "review_wait_respawn_task_id"
                            )
                            if _prev_id:
                                try:
                                    _prev = await session.get(
                                        Task, uuid.UUID(_prev_id)
                                    )
                                except (ValueError, TypeError):
                                    _prev = None
                                if _prev and _prev.status in (
                                    TaskStatus.CANCELLED,
                                    TaskStatus.FAILED,
                                ):
                                    _respawn_count = max(
                                        0, _respawn_count - 1
                                    )
                                    ci_loop[
                                        "review_wait_respawn_count"
                                    ] = _respawn_count
                                    logger.info(
                                        "[CI] PR #%s previous respawned review "
                                        "%s was superseded (status=%s) — "
                                        "resetting effective count to %d for "
                                        "new review-wait episode",
                                        ci_pr_number,
                                        _prev_id[:8],
                                        _prev.status.value,
                                        _respawn_count,
                                    )

                        # Exact authority comes only from this CI observation's
                        # head+base pair. Never combine a webhook head with a
                        # base cached by an earlier poll (or vice versa).
                        _current_head_sha = str(checked_head_sha or "")
                        _current_base_sha = str(checked_base_sha or "")
                        _review_target = (
                            {
                                "project_id": str(task.project_id),
                                "pr_number": int(ci_pr_number),
                                "base_sha": _current_base_sha,
                                "head_sha": _current_head_sha,
                            }
                            if task.project_id
                            and ci_pr_number
                            and _current_head_sha
                            and _current_base_sha
                            else None
                        )

                        # A manual, spend, security, or other independent hold
                        # already owns the operator-facing reason on this row.
                        # The review-wait backstop may observe the CI signal, but
                        # it cannot replace that hold, latch its own escalation,
                        # or consume its alert budget. An exact backstop-owned
                        # marker is the only approval episode this path may resume.
                        _independent_approval_hold = False
                        _exact_review_wait_hold = False
                        if task.requires_approval is True:
                            from app.closed_pr_reconciliation import (
                                owns_review_wait_approval_hold,
                            )

                            _exact_review_wait_hold = bool(
                                _review_target is not None
                                and owns_review_wait_approval_hold(
                                    task,
                                    ci_loop,
                                    pr_number=ci_pr_number,
                                    head_sha=str(_review_target["head_sha"]),
                                    base_sha=str(_review_target["base_sha"]),
                                )
                            )
                            _independent_approval_hold = not _exact_review_wait_hold

                        if _independent_approval_hold:
                            _respawn_outcome = "held"
                        elif (
                            (
                                _auto_respawn
                                and _respawn_count < _respawn_cap
                            )
                            or (
                                _exact_review_wait_hold
                                and (task.context or {}).get("pr_merged") is True
                            )
                        ) and _review_target is not None:
                            _orig_prompt = ci_payload.get("prompt", "") or (
                                task.description or ""
                            )
                            task.context = {
                                **_park_ctx_snapshot,
                                "review_loop": ci_loop,
                            }
                            flag_modified(task, "context")
                            (
                                _fresh_task,
                                _respawn_outcome,
                                _respawn_task_id,
                            ) = await _reconcile_review_wait_backstop_spawn(
                                task=task,
                                session=session,
                                pr_number=ci_pr_number,
                                branch=ci_branch,
                                base_branch=ci_base,
                                original_prompt=_orig_prompt,
                                original_task_id=ci_orig_id,
                                review_target=_review_target,
                                review_wait_started=_rw_started,
                            )
                            if _respawn_outcome == "stale":
                                # Replacement/review completion won the unlocked
                                # window and is already committed. Never rewrite
                                # it from this stale handler.
                                return True
                            if _fresh_task is not None:
                                task = _fresh_task
                                ci_loop = dict(
                                    (task.context or {}).get("review_loop") or {}
                                )
                                _park_ctx_snapshot = dict(task.context or {})
                        if _respawn_outcome in ("created", "dedup"):
                                _respawned = True
                                ci_loop["review_wait_respawned"] = True
                                ci_loop["review_wait_respawn_task_id"] = (
                                    _respawn_task_id
                                )
                                # A canonical exact DEDUP consumes the same
                                # timeout budget as a newly created review. It
                                # grants that live row one full follow-up window,
                                # but cannot reset the 60-minute clock forever
                                # when the row remains BLOCKED/PARKED.
                                ci_loop["review_wait_respawn_count"] = (
                                    _respawn_count + 1
                                )
                                # An exact live review is authority to WAIT only.
                                # Merge remains closed until its lifecycle writes
                                # an authoritative APPROVE receipt.
                                ci_loop["authoritative_review_required"] = True
                                ci_loop["authoritative_review_task_id"] = (
                                    _respawn_task_id
                                )
                                ci_loop["authoritative_review_target"] = dict(
                                    _review_target
                                )
                                # Reset the window so the replacement review
                                # task gets a full timeout before escalation.
                                ci_loop["review_wait_started"] = (
                                    _utcnow().isoformat()
                                )
                                ci_loop.pop("review_wait_deferred_at", None)
                                ci_loop["ci_poll_interval"] = 120
                                ci_loop.pop("review_wait_escalated", None)
                                task.latest_status_message = (
                                    f"CI passed on PR #{ci_pr_number} but review "
                                    f"approval never arrived after "
                                    f"{_REVIEW_WAIT_TIMEOUT_MIN}min — "
                                    f"{_respawn_outcome} exact review task "
                                    f"{_respawn_task_id}"
                                )
                                _bcast_status = TaskStatus.EVALUATING
                                logger.warning(
                                    "[CI] PR #%s review-wait backstop reconciled "
                                    "task %s (%.0f min elapsed) — %s exact review "
                                    "%s",
                                    ci_pr_number,
                                    task.id,
                                    _rw_age_s / 60,
                                    _respawn_outcome,
                                    _respawn_task_id,
                                )
                        elif _exact_review_wait_hold and _respawn_outcome == "terminal":
                            _respawn_outcome = "held"
                        elif _respawn_outcome == "held":
                            pass
                        elif _review_target is None:
                            # A webhook-only head (or any incomplete snapshot)
                            # cannot safely identify a review target. Preserve
                            # the elapsed backstop and let a complete poll retry;
                            # do not turn missing provenance into a human gate.
                            _respawn_outcome = "retry"
                            ci_loop["polls_since_pr_fetch"] = 999

                        if _respawn_outcome == "deferred":
                            _deferred_now = _utcnow()
                            _deferred_started = ci_loop.get("review_wait_deferred_at")
                            try:
                                _deferred_age_s = (
                                    (_deferred_now - datetime.fromisoformat(_deferred_started))
                                    .total_seconds()
                                    if _deferred_started
                                    else 0
                                )
                            except (TypeError, ValueError):
                                _deferred_age_s = 0
                                _deferred_started = None
                            if _deferred_started is None:
                                ci_loop["review_wait_deferred_at"] = (
                                    _deferred_now.isoformat()
                                )
                            elif _deferred_age_s >= _REVIEW_WAIT_DEFER_GRACE_S:
                                # Cross-type/mid-retry work is a bounded defer,
                                # not authority to restart the 60-minute wait
                                # forever. Escalation below preserves review/
                                # merge closure for the still-stalled attempt.
                                _respawn_outcome = "terminal"

                        if _respawn_outcome == "retry":
                            _retry_at = _utcnow().isoformat()
                            # Transient lock/error/incomplete-snapshot outcomes
                            # preserve the elapsed 60-minute episode. A short
                            # poll retries as soon as exact authority is available.
                            ci_loop["ci_last_polled"] = _retry_at
                            ci_loop["ci_poll_interval"] = 30
                            ci_loop.pop("review_wait_escalated", None)
                            task.latest_status_message = (
                                f"CI passed on PR #{ci_pr_number}; exact review "
                                "reconciliation is temporarily unavailable — "
                                "retrying shortly"
                            )
                            logger.info(
                                "[CI] PR #%s review-wait reconciliation retry for "
                                "task %s; preserved elapsed backstop clock",
                                ci_pr_number,
                                task.id,
                            )
                        elif _respawn_outcome == "paused":
                            _retry_at = _utcnow().isoformat()
                            ci_loop["review_wait_started"] = _retry_at
                            ci_loop.pop("review_wait_deferred_at", None)
                            ci_loop["ci_last_polled"] = _retry_at
                            ci_loop["ci_poll_interval"] = 30
                            ci_loop.pop("review_wait_escalated", None)
                            task.latest_status_message = (
                                f"CI passed on PR #{ci_pr_number}; review spawn "
                                "paused by current project admission state — "
                                "backstop clock paused for retry"
                            )
                            logger.info(
                                "[CI] PR #%s review-wait spawn paused for task "
                                "%s; paused backstop clock without escalation",
                                ci_pr_number,
                                task.id,
                            )
                        elif _respawn_outcome == "deferred":
                            _retry_at = _utcnow().isoformat()
                            ci_loop["ci_last_polled"] = _retry_at
                            ci_loop["ci_poll_interval"] = 30
                            ci_loop.pop("review_wait_escalated", None)
                            task.latest_status_message = (
                                f"CI passed on PR #{ci_pr_number}; review spawn "
                                "deferred by active cross-type work — retrying "
                                "within the bounded defer grace"
                            )
                            logger.info(
                                "[CI] PR #%s review-wait spawn deferred for task "
                                "%s; preserved elapsed backstop clock",
                                ci_pr_number,
                                task.id,
                            )
                        elif _respawn_outcome == "already_merged":
                            # The canonical spawner proved the exact PR was
                            # already merged. Rollup parents intentionally remain
                            # EVALUATING for rollup convergence, but this CI wait
                            # is finished and must never become a manual gate.
                            ci_loop["ci_waiting"] = False
                            ci_loop.pop("review_wait_started", None)
                            ci_loop.pop("review_wait_deferred_at", None)
                            ci_loop.pop("review_wait_escalated", None)
                            logger.info(
                                "[CI] PR #%s review-wait backstop observed an "
                                "already-merged rollup for task %s; leaving "
                                "terminal classification to rollup convergence",
                                ci_pr_number,
                                task.id,
                            )
                        elif _respawn_outcome == "held":
                            # Preserve the independent hold's exact provenance,
                            # timer, and operator-visible reason. With no
                            # review_wait_escalated latch, an explicit release
                            # naturally re-enters this aged backstop next poll.
                            ci_loop["ci_last_polled"] = _utcnow().isoformat()
                            ci_loop["ci_poll_interval"] = 120
                            task.latest_status_message = _review_wait_entry_status
                            logger.info(
                                "[CI] PR #%s review-wait backstop deferred for "
                                "task %s because an independent approval hold "
                                "owns the row",
                                ci_pr_number,
                                task.id,
                            )
                        elif not _respawned:
                            # Respawn disabled, suppressed, or cap reached —
                            # gate on a human so the PR isn't stranded silently.
                            # Claim ownership only when this backstop transitions
                            # an otherwise-open approval gate.
                            from app.closed_pr_reconciliation import (
                                REVIEW_WAIT_APPROVAL_HOLD_KEY,
                                REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                            )

                            ci_loop["review_wait_escalated"] = True
                            ci_loop.pop("review_wait_deferred_at", None)
                            if task.requires_approval is not True:
                                _approval_set_at = _utcnow().isoformat()
                                task.requires_approval = True  # approval-provenance: review-wait-owner
                                ci_loop[REVIEW_WAIT_APPROVAL_HOLD_KEY] = {
                                    "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                                    "project_id": str(task.project_id),
                                    "pr_number": int(ci_pr_number),
                                    "base_sha": _review_target["base_sha"],
                                    "head_sha": _review_target["head_sha"],
                                    "set_at": _approval_set_at,
                                }
                                _park_ctx_snapshot["_approval_set_at"] = (
                                    _approval_set_at
                                )
                            task.latest_status_message = (
                                f"CI passed on PR #{ci_pr_number} but review "
                                f"approval never arrived after "
                                f"{_REVIEW_WAIT_TIMEOUT_MIN}min — review task may "
                                "have died; requires manual approval"
                            )
                            await _maybe_create_failure_alert(
                                task,
                                session,
                                f"PR #{ci_pr_number}: review approval pending >"
                                f"{_REVIEW_WAIT_TIMEOUT_MIN}min — review task may "
                                "be orphaned; manual approval required",
                            )
                            logger.warning(
                                "[CI] PR #%s review-wait backstop fired for task "
                                "%s (%.0f min elapsed) — set requires_approval",
                                ci_pr_number,
                                task.id,
                                _rw_age_s / 60,
                            )
                except (ValueError, TypeError):
                    pass
            # Re-merge only onto the original locked snapshot or the fresh
            # relocked snapshot returned by the canonical backstop helper.
            task.context = {**_park_ctx_snapshot, "review_loop": ci_loop}
            flag_modified(task, "context")
            session.add(task)
            # ``task.status`` is never mutated on the park path; read it from the
            # instance dict (never IO) and default to EVALUATING if a respawn
            # rollback expired the attribute.
            _bcast_status = (
                _bcast_status
                or task.__dict__.get("status")
                or TaskStatus.EVALUATING
            )
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": _bcast_status},
            )
            return True

        # Both gates met — perform the merge
        await _attempt_merge_with_retry_and_escalation(
            task,
            gh_token,
            owner,
            repo,
            session,
            source=source,
            checked_head_sha=_checked_head_sha,
            checked_base_sha=_checked_base_sha,
        )
    else:
        # CI failed — spawn fix task
        # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
        # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
        # Postgres. status_log included for the Task event listeners under
        # async sessions; plan_output dropped via load_only.
        active_tasks = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.context,
                            Task.payloads,
                            Task.project_id,
                            Task.status_log,
                            raiseload=True,
                        )
                    )
                    .where(
                        Task.project_id == task.project_id,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                            ]
                        ),
                        Task.archived.is_(False),
                        _pr_number_match(session, ci_pr_number),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_fix = next(
            (
                t
                for t in active_tasks
                if t.id != task.id and t.first_payload.get("type") == "fix"
            ),
            None,
        )
        if existing_fix:
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Rollup parents stay in EVALUATING — ci_completion will promote them
            # via pr_merged=True once the fix lands and the PR merges.
            if not _is_rollup_parent_awaiting_merge(task.context):
                task.status = TaskStatus.COMPLETED
            task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id} ({source})"
            )
            session.add(task)
            _bcast_status = task.status
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": _bcast_status},
            )
            return True

        # ── Setup-phase infra rerun gate (repo-agnostic) ──
        # If the CI failure is purely a GitHub built-in setup-step failure
        # (container/runner init) rather than a code defect, it's a transient
        # infra flake — re-run the failed jobs ONCE before treating it as real,
        # so a Docker-Hub/runner blip doesn't burn a cold-VM fix cycle. Keys off
        # GitHub's own setup-step vocabulary (github_checks), never log content,
        # so it's repo-agnostic. Capped at one rerun per task via a context flag:
        # a genuinely broken setup fails the rerun too and falls through to the
        # normal fix path on the next poll.
        if ci_result.get("setup_phase_failure") and not (task.context or {}).get(
            "ci_infra_rerun_done"
        ):
            from app.github_checks import rerun_failed_jobs

            _reran_any = False
            for _run_id in ci_result.get("failed_run_ids") or []:
                if await rerun_failed_jobs(gh_token, owner, repo, _run_id):
                    _reran_any = True
            if _reran_any:
                _ctx = dict(task.context or {})
                _ctx["ci_infra_rerun_done"] = True
                task.context = _ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                logger.info(
                    f"[CI] PR #{ci_pr_number} CI failure is setup-phase infra — "
                    f"re-ran failed jobs, deferring fix spawn to next poll ({source})"
                )
                return False

        # ── General CI rerun gate (flake-tolerant) ──
        # If the CI failure involves user/test steps (not just GitHub built-in
        # setup infra), re-run failed jobs once before burning a full fix cycle.
        # A test flake (intermittent pass/fail) would otherwise trigger an
        # expensive agent fix task unnecessarily. Mirrors the bounded retry-budget
        # pattern used for behind-base in orchestrator_ci_monitor.py
        # (ci_behind_update_retries). Capped at one rerun per task via a context
        # counter; a genuinely broken test fails the rerun too and falls through
        # to the normal fix path on the next sweep.
        _rerun_consumed = (task.context or {}).get("ci_rerun_consumed", 0)
        if _rerun_consumed < _CI_RERUN_BUDGET:
            from app.github_checks import rerun_failed_jobs

            _reran_any = False
            for _run_id in ci_result.get("failed_run_ids") or []:
                if await rerun_failed_jobs(gh_token, owner, repo, _run_id):
                    _reran_any = True
            if _reran_any:
                _ctx = dict(task.context or {})
                _ctx["ci_rerun_consumed"] = _rerun_consumed + 1
                task.context = _ctx
                flag_modified(task, "context")
                task.latest_status_message = (
                    f"CI failed on PR #{ci_pr_number} — re-ran failed jobs "
                    f"({_rerun_consumed + 1}/{_CI_RERUN_BUDGET}), "
                    f"deferring fix spawn to next sweep"
                )
                session.add(task)
                await session.commit()
                logger.info(
                    f"[CI] PR #{ci_pr_number} CI failure — general rerun "
                    f"({_rerun_consumed + 1}/{_CI_RERUN_BUDGET}), "
                    f"deferring fix spawn to next sweep ({source})"
                )
                return False

        ci_errors = ci_result.get("failed_logs", "") or ""
        if not ci_errors.strip():
            _fail_detail_names = ", ".join(
                d.get("name", "")
                for d in (ci_result.get("details") or [])
                if d.get("conclusion") == "failure"
            )
            ci_errors = (
                f"CI checks failed: {_fail_detail_names}"
                if _fail_detail_names
                else "no detail available (see workflow run)"
            )

        # ── C2 lock-window break (CI-failure path) ──
        # The task row has been held FOR UPDATE since ~1341. ``create_pr_comment``
        # is a GitHub POST that can block 5-30s under rate-limit; holding the
        # task lock across it starves concurrent heartbeats on this row and can
        # trip the 60s idle_in_transaction timeout. Mirror the merge SUCCESS
        # path (``_attempt_merge_with_retry_and_escalation``): capture a content
        # snapshot, commit-release the lock, post the comment lock-free, then
        # re-acquire with snapshot validation. The subsequent
        # ``_spawn_pipeline_task`` manages its own per-PR advisory lock and
        # commits internally, so it does not need the task lock held — but the
        # terminal status writes below DO, hence the relock. If a peer mutated
        # the gate fields during the unlocked window we defer to the next sweep.
        _ci_fail_task_id = task.id
        _ci_fail_snapshot = _capture_merge_snapshot(task)
        flag_modified(task, "context")
        session.add(task)
        await session.commit()  # release the task FOR UPDATE lock

        await create_pr_comment(
            gh_token,
            owner,
            repo,
            ci_pr_number,
            f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```",
        )

        relocked = await _relock_merge_task(
            session,
            _ci_fail_task_id,
            _ci_fail_snapshot,
            task,
            source=source,
        )
        if relocked is None:
            logger.info(
                f"[CI] Task {_bcast_task_id} held/mutated during CI-failure "
                f"comment window — deferring fix spawn to next sweep ({source})"
            )
            return False
        task = relocked

        from app.review_pipeline import build_ci_failure_fix_prompt

        ci_fix_prompt = build_ci_failure_fix_prompt(
            pr_number=ci_pr_number,
            base_branch=ci_base,
            ci_errors=ci_errors,
            owner=owner,
            repo=repo,
        )
        fix_task, _ = await _spawn_pipeline_task(
            task,
            "fix",
            session,
            ci_pr_number,
            ci_branch,
            ci_base,
            fix_prompt=ci_fix_prompt,
            original_task_id=ci_orig_id,
            fix_reason="ci_failure",
            ci_failure_logs=ci_errors,
            # Phase 2d: CI ran as a merge gate after review approval —
            # the warm chain has ended, the new fix must cold-provision.
            suppress_warm_handoff=True,
        )
        if fix_task:
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            ci_loop = revoke_review_authority_for_active_fix(
                task,
                fix_task.id,
                review_loop=(task.context or {}).get("review_loop") or ci_loop,
            )
            ci_loop["ci_waiting"] = False
            # Rollup parents stay in EVALUATING — ci_completion will promote them
            # via pr_merged=True once the fix lands and the PR merges.
            # Capture the broadcast status HERE, at the point of decision, rather
            # than re-reading task.status before the broadcast below: a rollback
            # inside _spawn_pipeline_task above can expire `task` (rollback defers
            # every attr even with expire_on_commit=False), and on the
            # rollup-awaiting path task.status is not re-assigned, so a late read
            # would lazy-load in async context → MissingGreenlet (#6754 family;
            # prod 2026-06-03 ci_completion crash).
            if not _is_rollup_parent_awaiting_merge(task.context):
                task.status = TaskStatus.COMPLETED
                _bcast_status = TaskStatus.COMPLETED
            else:
                _bcast_status = TaskStatus.EVALUATING
            task.latest_status_message = (
                f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
            )
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id} ({source})"
            )
        else:
            _ci_fail_msg = (
                f"CI failed on PR #{ci_pr_number}, fix not spawned (pipeline limit)"
            )
            task.status = TaskStatus.FAILED
            task.latest_status_message = _ci_fail_msg
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None ({source})"
            )
            # Persist the terminal FAILED transition NOW, on a known-healthy
            # session, before the best-effort failure accounting below. Of the
            # accounting helpers only ``_maybe_create_failure_alert`` touches
            # the DB; an inner error there poisons the transaction, so a
            # deferred commit would raise PendingRollbackError and strand the
            # task in EVALUATING. Committing first makes FAILED durable.
            session.add(task)
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": TaskStatus.FAILED},
            )

            # Best-effort failure accounting (classification, deploy-monitor
            # signal, direction streak, alert). Isolated so a DB error here
            # cannot undo the FAILED status already committed above.
            try:
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )
                from app.routers.agent_status import record_task_result
                from app.shared_state import record_direction_result

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(
                    success=False,
                    failure_class=_fc,
                    task_id=_bcast_task_id,
                    task_type=task.first_payload.get("type", ""),
                )
                _direction_id = str((task.context or {}).get("direction_id") or "")
                if _direction_id:
                    record_direction_result(
                        _direction_id,
                        success=False,
                        failure_class=_fc,
                        project_id=_bcast_project_id,
                    )
                await _maybe_create_failure_alert(task, session, _ci_fail_msg)
                session.add(task)
                await session.commit()
            except Exception as acct_exc:
                logger.warning(
                    "[CI] failure accounting errored for task %s (%s) — FAILED "
                    "status already persisted",
                    _bcast_task_id,
                    acct_exc,
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            return True

    # Reached only by the fix-spawned branch above — the fix-not-spawned
    # branch commits and returns inline. ``task`` CAN be expired here: the
    # nested _spawn_pipeline_task / prior session work may have rolled back,
    # and rollback expires instances even with expire_on_commit=False.
    session.add(task)
    # The fix-spawned branch pre-set _bcast_status at its status-decision point so
    # we don't re-read a possibly-expired ``task`` (a rollback inside
    # _spawn_pipeline_task expires it → sync lazy-load in async context →
    # MissingGreenlet, #6754 family). Other paths reach here with a fresh ``task``,
    # but "fresh" proved unreliable in prod (2026-06-11: an unrelated mid-handler
    # rollback expired the instance and the direct read crashed the worker), so
    # the fallback reads from the instance dict — never IO — and only goes to the
    # DB through an explicit async SELECT when the attribute really is expired.
    if _bcast_status is None:
        _bcast_status = task.__dict__.get("status")
    if _bcast_status is None:
        _bcast_status = (
            await session.execute(select(Task.status).where(Task.id == _bcast_task_id))
        ).scalar_one_or_none()
    await session.commit()
    await broadcast_to_project(
        _bcast_project_id,
        "task_updated",
        {"task_id": _bcast_task_id, "status": _bcast_status},
    )
    return True
