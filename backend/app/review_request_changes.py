"""Review request-changes handling, extracted from task_lifecycle.py.

S5 of the fdd07b88 decomposition plan (knowledge doc
``fdd07b88-8bc5-4db3-89e0-016075fb7563``). This is the ``else:``
(REQUEST_CHANGES) branch of the ``if verdict == "APPROVE":`` block inside the
``payload.status == TaskStatus.COMPLETED`` section of ``_process_post_commit``:
when a review requests changes it short-circuits unparseable verdicts, closes
and repurposes an empty rollup PR, guards against stale/unproven review
targets (park-honoring vs exact-target respawn), revokes the originating
task's merge authority, persists the bounded review history + issue ledger,
and spawns a bounded exact-head fix task with rejection-category learning.

Free-variable set (per the design doc's live-in analysis): ``task``,
``pr_number``, ``branch``, ``base_branch``, ``original_task_id``,
``original_prompt``, ``feedback``, ``issues``, ``resolutions``,
``investigated_files``, ``verdict_unparseable``, ``reviewed_head_sha``,
``reviewed_base_sha``, ``_review_iteration``, ``_current_review_entry``,
``review_loop``, ``_record_structured_review_result`` (the late-bound recorder
closure from ``_process_post_commit`` — passed through so its ``review_loop``
capture stays the pre-approval loop), and ``session``. Everything else the
branch reads resolves from this module's own namespace or a deferred import
at call time.

This stage returns ``(action_taken, response)``: ``action_taken`` mirrors the
original ``action_taken = True`` fall-through (used by the caller's
``if not action_taken:`` warning), and ``response`` is the dict the caller
should return immediately, or ``None`` when the branch completed normally.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.branch_resolution import classify_rollup_merge_outcome
from app.context_bounds import bound_review_history
from app.landed_signal import corroborate_and_apply_merged_landed_signal
from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.post_commit_errors import PostCommitFinalCommitFailed
from app.retry_prompt import _detect_ci_review_disagreement
from app.review_pipeline import (
    REVIEW_FIX_GOAL_PREAMBLE,
    build_issue_ledger,
    detect_fix_all_issue_class,
    fix_all_occurrences_directive,
    review_loop_parked_for_operator,
    stale_respawn_delta_base_sha,
)
from app.rollup_locking import _OriginalTaskLockTimeout, _lock_original_task

logger = logging.getLogger(__name__)


async def run_request_changes(
    task: Task,
    *,
    pr_number,
    branch,
    base_branch,
    original_task_id,
    original_prompt,
    feedback,
    issues,
    resolutions,
    investigated_files,
    verdict_unparseable,
    reviewed_head_sha,
    reviewed_base_sha,
    _review_iteration,
    _current_review_entry,
    review_loop,
    _record_structured_review_result,
    session: AsyncSession,
) -> tuple[bool, dict | None]:
    """Run the request-changes sequence and return ``(action_taken, response)``.

    When ``response`` is not ``None`` the caller must return it immediately
    (matching the early ``return`` statements in the original inline code);
    otherwise the caller uses ``action_taken`` for its ``if not action_taken:``
    warning.
    """
    # Deferred imports so test patches on app.task_lifecycle._spawn_pipeline_task /
    # app.task_lifecycle.broadcast_to_project / app.routers.agent.
    # get_github_token_for_task / app.github.* keep intercepting the call
    # (matches the original module-level / function-local bindings in
    # _process_post_commit).
    from app.task_lifecycle import (
        _pipeline_spawn_refusal_label,
        _persist_stale_review_evidence,
        _spawn_pipeline_task,
        broadcast_to_project,
    )
    from app.routers.agent import get_github_token_for_task
    from app.time_utils import _utcnow
    from app.github import (
        close_pull_request,
        get_check_runs_cached,
        get_pr_files,
        get_pull_request,
    )

    # REQUEST_CHANGES — include CI errors if available (don't wait).
    # After verdict normalization above this is the only non-APPROVE branch;
    # any non-standard verdict (e.g., legacy COMMENT) has already been
    # rewritten to REQUEST_CHANGES and lands here.

    # Unparseable verdict short-circuit: the fix agent would receive no
    # structured issues and a placeholder feedback string, consuming a VM
    # cycle without useful guidance. The original task is already flagged
    # requires_approval above, so human intervention is expected.
    if verdict_unparseable:
        logger.warning(
            f"[REVIEW PIPELINE] PR #{pr_number}: verdict unparseable — skipping fix spawn, original task flagged requires_approval"
        )
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = f"Review verdict unparseable for PR #{pr_number} — fix skipped, manual review required"
        session.add(task)
        await session.commit()
        return True, {"status": "accepted"}

    # NOTE: a verification-only REQUEST_CHANGES on
    # authoritatively-green CI is handled BEFORE the verdict
    # branch (search "verification-only REQUEST_CHANGES → treat
    # as APPROVE" above): it flips the verdict to APPROVE so the
    # PR merges through the normal approve path. By the time we
    # reach this REQUEST_CHANGES branch, the verdict is a genuine
    # change request (concrete defect, or CI not authoritatively
    # green), so we proceed to spawn a fix as usual.

    # Empty-PR short-circuit: if the PR has no file changes (because a
    # prior fix cycle rebased the branch flat so all its commits are in
    # the base), spawning another fix is futile — there's nothing to
    # fix. Close the PR and classify the rollup parent's landing
    # so intermediate staged targets keep waiting downstream.
    try:
        (
            _empty_gh,
            _empty_owner,
            _empty_repo,
        ) = await get_github_token_for_task(task.id, session)
        _pr_files = await get_pr_files(
            _empty_gh,
            _empty_owner,
            _empty_repo,
            pr_number,
            project_id=str(task.project_id),
        )
    except Exception as _empty_err:
        logger.warning(
            f"[REVIEW PIPELINE] Empty-PR check failed for #{pr_number}: {_empty_err} — proceeding with fix spawn"
        )
        _pr_files = None
    if _pr_files == []:
        logger.info(
            f"[REVIEW PIPELINE] PR #{pr_number} has no file changes — closing as empty rollup"
        )
        try:
            await close_pull_request(
                _empty_gh,
                _empty_owner,
                _empty_repo,
                pr_number,
                project_id=str(task.project_id),
            )
        except Exception as _close_err:
            logger.warning(
                f"[REVIEW PIPELINE] Failed to close empty PR #{pr_number}: {_close_err}"
            )
        if original_task_id:
            _orig_broadcast_status = TaskStatus.EVALUATING
            # Captured for the post-commit direction-outcome
            # refresh: the empty-PR cleanup lands ORIG as a
            # merge-equivalent integration landing, but ORIG (not
            # this review ``task``) carries the direction lineage,
            # so the terminal recompute below misses it.
            _orig_to_refresh: Task | None = None
            try:
                # Phase A1: skip_locked + bounded retry. If the
                # parent row is held, defer the empty-PR cleanup;
                # the next poll cycle will retry. Better than
                # 500'ing the review handler.
                _orig = await _lock_original_task(
                    session,
                    original_task_id,
                    source="review_empty_pr_cleanup",
                )
                if _orig:
                    _orig_loop = (_orig.context or {}).get(
                        "review_loop", {}
                    ) or {}
                    _orig_loop.pop("ci_waiting", None)
                    _orig_project = await session.get(
                        Project, _orig.project_id
                    )
                    (
                        _record_pr_merged,
                        _terminal_rollup_merge,
                        _landed_branch,
                    ) = await classify_rollup_merge_outcome(
                        _orig,
                        _orig_project,
                        session,
                        pr_number=pr_number,
                        merged_base_branch=base_branch,
                        source="task_lifecycle.review_empty_pr_cleanup",
                    )
                    _orig.context = {
                        **(_orig.context or {}),
                        "review_loop": _orig_loop,
                        "pr_closed_empty": True,
                    }
                    if _record_pr_merged:
                        # No merge_commit_sha exists on this path: the empty PR
                        # was CLOSED, not merged, and credited as a
                        # merge-equivalent landing. Corroboration therefore runs
                        # unpinned and, per its own contract, declines to stamp
                        # a durable absence read through the branch-tip cache.
                        await corroborate_and_apply_merged_landed_signal(
                            _orig,
                            _orig_project,
                            session,
                            base_branch=_landed_branch,
                            hotfix_lane=bool(
                                (_orig.first_payload or {}).get(
                                    "hotfix"
                                )
                                or (_orig.context or {}).get("hotfix")
                            ),
                            non_merge_reason=(
                                "merged_pr_non_integration_branch"
                            ),
                        )
                    flag_modified(_orig, "context")
                    _orig.status = (
                        TaskStatus.COMPLETED
                        if _terminal_rollup_merge
                        else TaskStatus.EVALUATING
                    )
                    if _terminal_rollup_merge:
                        _orig.latest_status_message = (
                            f"✓ Rollup PR #{pr_number} closed — "
                            f"branch equivalent to {base_branch}, "
                            "no new changes to merge"
                        )
                    else:
                        _orig.latest_status_message = (
                            f"✓ Rollup PR #{pr_number} closed — "
                            f"branch equivalent to {_landed_branch}, "
                            "no new changes to merge; awaiting "
                            "downstream landing"
                        )
                    _orig_broadcast_status = _orig.status
                    session.add(_orig)
                    if _record_pr_merged:
                        _orig_to_refresh = _orig
                else:
                    logger.warning(
                        f"[REVIEW PIPELINE] Empty-PR cleanup deferred — parent {original_task_id[:8]} contended"
                    )
            except Exception as _orig_err:
                logger.warning(
                    f"[REVIEW PIPELINE] Failed to update rollup parent after empty-PR close: {_orig_err}"
                )
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = f"Review complete — PR #{pr_number} closed as empty (obsolete rollup)"
        session.add(task)
        await session.commit()
        # Heal ORIG's direction merge counts now that its
        # merge-equivalent landing is committed (mirrors the
        # review_merge_success path). No-op unless ORIG carries a
        # direction/research lineage; best-effort.
        if original_task_id and _orig_to_refresh is not None:
            from app.direction_outcomes import (
                refresh_direction_outcomes_after_merge,
            )

            await refresh_direction_outcomes_after_merge(
                _orig_to_refresh, session
            )
        if _orig_to_refresh is not None:
            try:
                from app.reaper_sweeps import (
                    event_driven_unblock_dependents,
                )

                await event_driven_unblock_dependents(
                    _orig_to_refresh, session
                )
            except Exception:
                logger.exception(
                    "[PIPELINE] event_driven_unblock_dependents "
                    "failed for task %s",
                    _orig_to_refresh.id,
                )
        if original_task_id:
            try:  # noqa: SIM105
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {
                        "task_id": original_task_id,
                        "status": _orig_broadcast_status,
                    },
                )
            except Exception:  # noqa: S110
                pass
        return True, {"status": "accepted"}

    ci_errors_text = ""
    ci_check: dict | None = None
    head_sha = ""
    base_sha = ""
    try:
        gh_token, owner, repo = await get_github_token_for_task(
            task.id, session
        )
        pr_data = await get_pull_request(
            gh_token, owner, repo, pr_number
        )
        head_sha = pr_data.get("head", {}).get("sha", "")
        base_sha = str(pr_data.get("base", {}).get("sha", ""))
        if head_sha:
            ci_check = await get_check_runs_cached(
                gh_token, owner, repo, head_sha
            )
            if ci_check.get("conclusion") == "failure" and ci_check.get(
                "failed_logs"
            ):
                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
    except Exception as e:
        logger.error(
            f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}",
            exc_info=True,
        )

    # Stale/unproven-target guard: REQUEST_CHANGES can launch a
    # code-writing fix only when the reviewer proved the exact
    # immutable base/head it inspected. Missing provenance is no
    # safer than a mismatch. If GitHub cannot provide the live
    # target, fail closed for operator attention rather than
    # expanding to an unpinned review or using stale guidance.
    if not head_sha or not base_sha:
        task.status = TaskStatus.COMPLETED
        task.requires_approval = True
        task.latest_status_message = (
            f"Review changes for PR #{pr_number} withheld: could not "
            "prove the live immutable review target"
        )
        task.context = {
            **(task.context or {}),
            "_review_target_unavailable": True,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return True, {"status": "accepted"}

    review_target_unproven = (
        not reviewed_head_sha
        or not reviewed_base_sha
        or reviewed_head_sha != head_sha
        or reviewed_base_sha != base_sha
    )
    if review_target_unproven:
        # A stale finding can waste a fix iteration just as a
        # stale approval can merge bad code. Always discard and
        # respawn against the immutable live target; idempotent
        # spawn keys bound duplicate work without weakening the
        # target invariant.
        #
        # Park-honoring gate (lineage d3c12745 / PR #16759):
        # when the chain root was already escalated to the
        # operator (stuck/standoff), a respawned exact-target
        # review only burns another model run into the same
        # stuck gate — and counts toward the global
        # pr_iterations recount that eventually auto-closed the
        # parked PR. Discard the stale review WITHOUT
        # respawning and say so truthfully.
        _stale_park_reason = None
        if original_task_id:
            try:
                _stale_park_reason = review_loop_parked_for_operator(
                    await session.get(
                        Task, uuid.UUID(original_task_id)
                    )
                )
            except Exception as _park_err:
                logger.warning(
                    f"[REVIEW PIPELINE] Could not check operator-park "
                    f"state for chain root {original_task_id[:8]}: "
                    f"{_park_err}"
                )
        if _stale_park_reason is not None:
            task.status = TaskStatus.COMPLETED
            task.latest_status_message = (
                f"Stale-target review discarded for PR #{pr_number} — "
                f"review loop parked for operator "
                f"({_stale_park_reason}); not respawning review"
            )
            task.context = {
                **(task.context or {}),
                "_review_stale_head": True,
            }
            flag_modified(task, "context")
            session.add(task)
            logger.warning(
                f"[REVIEW PIPELINE] Stale-target review for PR "
                f"#{pr_number} discarded without respawn — chain root "
                f"{original_task_id[:8]} parked for operator "
                f"({_stale_park_reason})"
            )
        elif not (task.context or {}).get("_review_stale_head"):
            task.status = TaskStatus.COMPLETED
            task.latest_status_message = (
                f"Stale-target review discarded for PR #{pr_number} "
                f"(reviewed {reviewed_base_sha[:8]}..{reviewed_head_sha[:8]}, "
                f"now {base_sha[:8]}..{head_sha[:8]}) — respawning exact-target review"
            )
            await _persist_stale_review_evidence(
                task,
                session,
                original_task_id=original_task_id,
                pr_number=pr_number,
                review_loop=review_loop,
                review_entry=_current_review_entry,
                live_target={
                    "project_id": str(task.project_id),
                    "pr_number": int(pr_number),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                },
            )
            task.context = {
                **(task.context or {}),
                "_review_stale_head": True,
            }
            flag_modified(task, "context")
            session.add(task)
            fresh_review, _ = await _spawn_pipeline_task(
                task,
                "review",
                session,
                pr_number,
                branch,
                base_branch,
                original_task_id=original_task_id,
                # The prior review's head is the narrowest safe diff base for
                # the replacement review — but only when the head actually
                # moved and the base did not. Base-drift-only and empty
                # provenance stay a full-review fallback; a head-relative
                # delta would be empty and would silently drop the reported
                # defect.
                delta_base_sha=stale_respawn_delta_base_sha(
                    reviewed_head_sha=reviewed_head_sha,
                    reviewed_base_sha=reviewed_base_sha,
                    live_head_sha=head_sha,
                    live_base_sha=base_sha,
                ),
                review_target={
                    "project_id": str(task.project_id),
                    "pr_number": int(pr_number),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                }
                if base_sha and head_sha
                else None,
            )
            if fresh_review:
                logger.info(
                    "[REVIEW PIPELINE] Spawned exact-target review "
                    "%s for PR #%s after stale-head discard",
                    fresh_review.id,
                    pr_number,
                )
        await session.commit()
        return True, {"status": "accepted"}

    # A cached verdict must have evaluated this exact head.
    # Only exact-target verdicts are reusable on a later dispatch.
    if (
        reviewed_head_sha == head_sha
        and reviewed_base_sha == base_sha
    ):
        _record_structured_review_result(
            {
                "project_id": str(task.project_id),
                "pr_number": int(pr_number),
                "base_sha": base_sha,
                "head_sha": head_sha,
            }
            if base_sha and head_sha
            else None
        )

    # A same-head change request is merge-authority revocation,
    # not merely input to the next fix agent.  The originating
    # task can already be parked on green CI with an older exact
    # APPROVE (or an initial-review bypass marker).  Revoke that
    # proof and persist this review's bounded history + ledger on
    # the parent *before* a code-writing fix is allowed to commit.
    # Otherwise CI can race the fix spawn and publish the bytes
    # that this reviewer just blocked.
    if original_task_id:
        from app.task_locking import acquire_pr_spawn_lock

        await acquire_pr_spawn_lock(
            session, task.project_id, pr_number
        )
        try:
            _orig = await _lock_original_task(
                session,
                original_task_id,
                source="review_blocker_authority_revoke",
                skip_locked=False,
            )
        except _OriginalTaskLockTimeout as _revoke_lock_err:
            # The status endpoint's write-ahead post_commit_pending
            # marker makes this whole completion replayable.  Fail
            # closed and preserve that retry path; never spawn the
            # fix while the parent can still look approved.
            raise PostCommitFinalCommitFailed(
                "could not durably revoke parent review authority "
                f"for {original_task_id} before fixing PR #{pr_number}"
            ) from _revoke_lock_err

        if _orig is None:
            task.requires_approval = True
            task.latest_status_message = (
                f"Review blocked PR #{pr_number}, but its originating "
                "task is missing; fix withheld for manual recovery"
            )
            session.add(task)
            await session.commit()
            return True, {"status": "accepted"}

        _orig_loop = dict(
            (_orig.context or {}).get("review_loop", {}) or {}
        )

        # The review task owns the chain history it actually
        # received.  A parent can still carry a pre-cutover,
        # empty, or stale copy, so never prefer that copy merely
        # because it is a list.  Preserve only parent entries
        # whose iteration is genuinely newer than everything
        # known to this review task, then make the completing
        # review the final entry.
        _child_history = review_loop.get("review_history") or []
        if not isinstance(_child_history, list):
            _child_history = []
        _child_history = [
            _entry
            for _entry in _child_history
            if isinstance(_entry, dict)
        ]

        _parent_history_raw = _orig_loop.get("review_history") or []
        if not isinstance(_parent_history_raw, list):
            _parent_history_raw = []
        _child_review_ids = {
            str(_entry.get("review_task_id") or "")
            for _entry in _child_history
            if _entry.get("review_task_id")
        }
        _parent_only_entries = [
            _entry
            for _entry in _parent_history_raw
            if isinstance(_entry, dict)
            and (
                not _entry.get("review_task_id")
                or str(_entry.get("review_task_id"))
                not in _child_review_ids
            )
            and _entry not in _child_history
        ]

        _review_task_id = str(task.id)
        _parent_entry = {
            **_current_review_entry,
            # ``issues`` was expanded above to the complete
            # binding open set.  Persist that set, rather than
            # the narrower list captured before ledger rebuild,
            # so history bounding can never forget an omitted
            # blocker from an earlier child review.
            "issues": list(issues),
            "review_task_id": _review_task_id,
        }
        _parent_history = [
            _entry
            for _entry in [*_child_history, *_parent_only_entries]
            if _entry.get("review_task_id") != _review_task_id
        ]
        _parent_history = bound_review_history(
            [*_parent_history, _parent_entry]
        )
        _prior_parent_ledger = _orig_loop.get("issue_ledger")
        _parent_ledger = build_issue_ledger(
            _parent_history,
            **(
                {"seed_ledger": _prior_parent_ledger}
                if _prior_parent_ledger is not None
                else {}
            ),
            stamp_complete=True,
        )
        _parent_target = {
            "project_id": str(task.project_id),
            "pr_number": int(pr_number),
            "base_sha": base_sha,
            "head_sha": head_sha,
        }

        _orig_loop["review_approved"] = False
        _orig_loop["review_history"] = _parent_history
        _orig_loop["issue_ledger"] = _parent_ledger
        _orig_loop["review_target"] = _parent_target
        _orig_loop["base_sha"] = base_sha
        _orig_loop["head_sha"] = head_sha
        for _stale_authority_key in (
            "verdict",
            "review_not_required",
            "review_policy_reason",
            "authoritative_review_required",
            "authoritative_review_task_id",
            "authoritative_review_target",
            "authoritative_review_target_unresolved_at",
        ):
            _orig_loop.pop(_stale_authority_key, None)

        if _orig_loop.get("ci_waiting"):
            from app import pause_clock

            _orig_loop["ci_wait_started"] = _utcnow().isoformat()
            try:
                _orig_loop[
                    "ci_wait_pause_baseline"
                ] = await pause_clock.current_baseline(session)
            except Exception as _pause_err:  # noqa: BLE001
                logger.warning(
                    "[REVIEW PIPELINE] Could not refresh pause "
                    "baseline while revoking parent %s: %r",
                    str(original_task_id)[:8],
                    _pause_err,
                )

        _orig.context = {
            **(_orig.context or {}),
            "review_loop": _orig_loop,
        }
        flag_modified(_orig, "context")
        session.add(_orig)
        # This commit is intentionally before _spawn_pipeline_task:
        # that helper may commit or roll back internally.  A spawn
        # failure must leave the parent safely revoked and the
        # post-commit sweep free to retry the idempotent spawn.
        await session.commit()

    # CI vs reviewer build-gate disagreement detector. When the
    # reviewer flags build/lint complaints but CI shows the
    # corresponding job GREEN, the issue is almost always a
    # broken reviewer-VM env — pre-warn the fix agent so it
    # disputes via /tmp/fix_responses rather than thrashing on
    # source code that isn't broken (PR #3884 saga, 2026-05-06).
    disagreement_hint = _detect_ci_review_disagreement(
        issues,
        feedback,
        ci_check,
        pr_number,
    )
    if issues:

        def _fmt_issue(i):
            desc = i.get("description", "")
            file_ = i.get("file")
            line_ = i.get("line")
            if file_ and line_ is not None:
                return f"- [{file_}:{line_}] {desc}"
            if file_:
                return f"- [{file_}] {desc}"
            return f"- {desc}"

        critical = [
            i for i in issues if i.get("severity") == "critical"
        ]
        major = [i for i in issues if i.get("severity") == "major"]
        minor = [i for i in issues if i.get("severity") == "minor"]
        structured = ""
        if critical:
            structured += (
                "\n\nCRITICAL issues (must fix):\n"
                + "\n".join(_fmt_issue(i) for i in critical)
            )
        if major:
            structured += "\n\nMajor issues:\n" + "\n".join(
                _fmt_issue(i) for i in major
            )
        if minor:
            structured += "\n\nMinor issues:\n" + "\n".join(
                _fmt_issue(i) for i in minor
            )
        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Issues to address:{structured}{ci_errors_text}"
    else:
        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Reviewer feedback:\n\n{feedback}{ci_errors_text}"
    # FIX-ALL-OCCURRENCES: when the flagged issues look like
    # instances of ONE file-spanning class (shared description
    # stem, or an explicit "all instances"/"other call sites"
    # cue), prepend a directive to grep + fix EVERY occurrence
    # in this pass. Prevents the whack-a-mole loop where the fix
    # agent migrates only the cited line and the next review
    # finds the next sibling site. The directive explicitly
    # reconciles with the narrow-focus guardrails injected later
    # in _spawn_pipeline_task: migrating sibling sites of the
    # flagged class is completing the fix, not refactoring.
    if detect_fix_all_issue_class(issues):
        logger.info(
            f"[REVIEW PIPELINE] PR #{pr_number}: flagged issues "
            f"form a file-spanning class — injecting "
            f"fix-all-occurrences directive"
        )
        fix_prompt_text = (
            f"{fix_all_occurrences_directive()}{fix_prompt_text}"
        )
    fix_task, _ = await _spawn_pipeline_task(
        task,
        "fix",
        session,
        pr_number,
        branch,
        base_branch,
        fix_prompt=fix_prompt_text,
        original_prompt=original_prompt,
        original_task_id=original_task_id,
        current_feedback=feedback,
        current_issues=issues,
        current_resolutions=resolutions,
        investigated_files=investigated_files,
        reviewed_head_sha=reviewed_head_sha or head_sha,
        reviewed_base_sha=reviewed_base_sha or base_sha,
        target_verified_by_backend=bool(
            _current_review_entry.get(
                "target_verified_by_backend"
            )
        ),
    )
    if fix_task:
        logger.info(
            f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}"
        )
    else:
        # Surface the REAL refusal reason, not a blanket "cap
        # reached" (lineage d3c12745 / PR #16759: 13 tasks said
        # "iteration cap reached" when the truth was
        # stuck_cycle). _spawn_pipeline_task records why it
        # returned None in SPAWN_REASON_KEY on the source task.
        _fix_refusal_label = _pipeline_spawn_refusal_label(task)
        task.latest_status_message = (
            f"Fix not spawned for PR #{pr_number} — "
            f"{_fix_refusal_label}"
        )
        task.requires_approval = True
        session.add(task)
        cap_alert_title = (
            f"PR #{pr_number} stalled — {_fix_refusal_label}"
        )
        existing_cap_alert = (
            (
                await session.execute(
                    select(Alert).where(
                        Alert.project_id == task.project_id,
                        Alert.severity == AlertSeverity.WARNING,
                        Alert.dismissed_at.is_(None),
                        Alert.title == cap_alert_title,
                    )
                )
            )
            .scalars()
            .first()
        )
        if not existing_cap_alert:
            alert = Alert(
                project_id=task.project_id,
                severity=AlertSeverity.WARNING,
                title=cap_alert_title,
                message=f"Review requested changes on PR #{pr_number} but no fix was spawned ({_fix_refusal_label}). Manual intervention required.",
                task_id=task.id,
                event_type="review.escalated",
            )
            session.add(alert)
            await session.commit()
            await broadcast_to_project(
                str(task.project_id),
                "alert_created",
                {
                    "alert_id": str(alert.id),
                    "severity": "WARNING",
                    "title": alert.title,
                },
            )
        else:
            await session.commit()
        logger.warning(
            f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number} ({_fix_refusal_label})"
        )
    # On every review rejection, store which issue categories caused it
    # on the original task.  Iteration 1 → _first_attempt_rejection_categories
    # (existing field); all iterations → _all_iterations_rejection_categories
    # (accumulated superset used by all-iterations catch-rate metric).
    if issues and original_task_id:
        try:
            from app.review_learning import (
                cache_first_attempt_classification,
                _keyword_from_flat,
            )

            rh_items = [
                {
                    "description": i.get("description", ""),
                    "severity": i.get("severity", "minor"),
                    "iteration": _review_iteration,
                }
                for i in issues
                if i.get("description")
            ]
            if rh_items:
                orig_task = await session.get(
                    Task, uuid.UUID(original_task_id)
                )
                if orig_task:
                    task_ctx = dict(orig_task.context or {})
                    if _review_iteration == 1:
                        buckets = (
                            await cache_first_attempt_classification(
                                task_ctx, rh_items
                            )
                        )
                    else:
                        buckets = _keyword_from_flat(rh_items)
                    rejection_cats = {
                        cat: len(items)
                        for cat, items in buckets.items()
                        if items
                    }
                    if rejection_cats:
                        if _review_iteration == 1:
                            task_ctx[
                                "_first_attempt_rejection_categories"
                            ] = rejection_cats
                        existing_all = dict(
                            task_ctx.get(
                                "_all_iterations_rejection_categories"
                            )
                            or {}
                        )
                        for cat, cnt in rejection_cats.items():
                            existing_all[cat] = (
                                existing_all.get(cat, 0) + cnt
                            )
                        task_ctx[
                            "_all_iterations_rejection_categories"
                        ] = existing_all
                    orig_task.context = task_ctx
                    flag_modified(orig_task, "context")
                    session.add(orig_task)
                    await session.commit()
        except Exception as _cat_e:
            logger.warning(
                f"[REVIEW PIPELINE] Failed to record rejection categories: {_cat_e}"
            )
    return True, None
