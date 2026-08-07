"""Fix-task completion handling, extracted from task_lifecycle.py.

S4 of the fdd07b88 decomposition plan (knowledge doc
``fdd07b88-8bc5-4db3-89e0-016075fb7563``). This is the ``elif task_type ==
"fix"`` branch that runs inside the ``payload.status == TaskStatus.COMPLETED``
section of ``_process_post_commit``: when a fix task completes it security-scans
the pushed fix output and spawns a bounded exact-head-delta re-review (closing
the create→review→fix loop), escalating with an accurate spawn-reason alert when
the re-review spawn is deferred (transient) or fails terminally.

Free-variable set (per the design doc's live-in analysis): ``task``,
``task_payload``, ``review_loop``, and ``session``. Everything else the branch
reads — ``_shared_state`` (for ``get_project_integration_branch``),
``flag_modified``, ``select``, the ``Alert``/``AlertSeverity``/``Project``/
``Task`` models, ``SPAWN_REASON_KEY``/``SPAWN_REASON_DEFERRED``, and the
deferred-imported helpers below — resolves from this module's own namespace or
a deferred import at call time.

This is NOT the last stage — Stage D (completion dispatch) runs after it on the
non-early-return path. So this stage returns ``(task, response)``: ``task`` is
the (possibly re-fetched/mutated) task the caller must keep using, and
``response`` is the dict the caller should return immediately, or ``None`` when
the branch completed normally and the caller should fall through to Stage D.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Alert, AlertSeverity, Project, Task
from app.review_pipeline import SPAWN_REASON_DEFERRED, SPAWN_REASON_KEY
import app.shared_state as _shared_state

logger = logging.getLogger(__name__)


async def run_fix_completion(
    task: Task,
    *,
    task_payload: dict,
    review_loop: dict,
    session: AsyncSession,
) -> tuple[Task, dict | None]:
    """Run the fix-completion sequence and return ``(task, response)``.

    When ``response`` is not ``None`` the caller must return it immediately
    (matching the early ``return`` statements in the original inline code);
    otherwise the caller continues to Stage D with the returned (possibly
    re-fetched) ``task``.
    """
    # Deferred imports so test patches on app.task_lifecycle._spawn_pipeline_task /
    # app.task_lifecycle.broadcast_to_project / app.routers.agent.
    # get_github_token_for_task / app.github.* / app.security.* keep intercepting
    # the call (matches the original module-level / function-local bindings in
    # _process_post_commit).
    from app.task_lifecycle import (
        _safe_exception_message,
        _spawn_pipeline_task,
        broadcast_to_project,
    )
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import create_pr_comment, get_pull_request

    pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
    branch = review_loop.get("branch") or task_payload.get("branch")
    _fix_project = await session.get(Project, task.project_id)
    base_branch = (
        review_loop.get("base_branch")
        or task_payload.get("base_branch")
        or _shared_state.get_project_integration_branch(_fix_project)
    )
    original_task_id = review_loop.get("original_task_id", "") or task_payload.get(
        "original_task_id", ""
    )

    # Backend stamp: ensure fix_iteration is set on the self-review
    # outcome so the catch-rate dashboard and reviewer priming can
    # identify which fix iteration produced the SR. The agent stamps
    # it via code-change.ts readSelfReviewOutcome; this is the
    # fallback for agents that ran before that stamp was added.
    _fix_sro = (task.context or {}).get("_self_review_outcome")
    if isinstance(_fix_sro, dict) and "fix_iteration" not in _fix_sro:
        _pipeline_iter = int(review_loop.get("iteration", 2))
        # Pipeline sequence: review(1)→fix(2)→review(3)→fix(4)→...
        # fix_iteration = pipeline_iter // 2
        _fix_iter_n = max(1, _pipeline_iter // 2)
        _stamped_ctx = dict(task.context)
        _stamped_ctx["_self_review_outcome"] = {
            **_fix_sro,
            "fix_iteration": _fix_iter_n,
        }
        task.context = _stamped_ctx
        flag_modified(task, "context")
        session.add(task)

    if not pr_number or not branch:
        logger.info(
            f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
        )
        task.latest_status_message = (
            "Fix pipeline error: missing pr_number or branch"
        )
        session.add(task)
        await session.commit()
        return task, {"status": "accepted"}

    # Early exit: skip only when PR is genuinely merged/done.
    # A non-merged 'closed' state may be a transient self-close
    # from the orchestrator's mergeability-reset loop
    # (close+reopen to force GitHub to recompute mergeable=None).
    # Gating on merged==True only prevents silently dropping the
    # re-review handoff during that brief closed window.
    rereview_target: dict[str, str | int] | None = None
    try:
        (
            gh_token_check,
            owner_check,
            repo_check,
        ) = await get_github_token_for_task(task.id, session)
        pr_data_check = await get_pull_request(
            gh_token_check, owner_check, repo_check, pr_number
        )
        _rr_head_sha = str(pr_data_check.get("head", {}).get("sha", ""))
        _rr_base_sha = str(pr_data_check.get("base", {}).get("sha", ""))
        if _rr_head_sha and _rr_base_sha:
            rereview_target = {
                "project_id": str(task.project_id),
                "pr_number": int(pr_number),
                "base_sha": _rr_base_sha,
                "head_sha": _rr_head_sha,
            }
        if pr_data_check.get("merged"):
            logger.info(
                f"[REVIEW PIPELINE] PR #{pr_number} already merged — skipping fix re-review for task {task.id}"
            )
            task.latest_status_message = (
                f"PR #{pr_number} already merged — re-review skipped"
            )
            session.add(task)
            await session.commit()
            return task, {"status": "accepted"}
        if pr_data_check.get("state") == "closed":
            # PR is closed but not merged — could be a transient
            # mergeability-reset. Proceed with re-review so the
            # handoff is preserved; the review/merge gate will
            # handle a genuinely abandoned PR gracefully.
            logger.info(
                f"[REVIEW PIPELINE] PR #{pr_number} is closed (not merged) for task {task.id} — "
                f"may be a transient mergeability-reset; proceeding with re-review"
            )
    except Exception as e:
        logger.info(
            f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding"
        )

    # Security scan fix output before re-review
    scan_blocked = False
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        scan_result = await scan_pr_for_injection(
            gh_token, owner, repo, pr_number
        )
        logger.info(f"[SECURITY] {scan_result['summary']}")
        if scan_result["blocked"]:
            scan_blocked = True
            task.requires_approval = True
            task.latest_status_message = scan_result["summary"]
            session.add(task)
            await session.commit()
        if scan_result["findings"]:
            await create_pr_comment(
                gh_token, owner, repo, pr_number, scan_result["report"]
            )
    except Exception as e:
        logger.error(
            f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}",
            exc_info=True,
        )
        task.requires_approval = True
        task.latest_status_message = _safe_exception_message(
            e,
            "Security scan failed — review will proceed but merge requires manual approval",
        )
        session.add(task)
        await session.commit()

    # Every pushed fix invalidates prior approval. The only exception
    # is a blocking security scan, which halts for manual approval.
    if scan_blocked:
        # Scan blocked the fix output; task already flagged for manual
        # approval above. Do not park for CI or spawn a re-review.
        pass
    else:
        # Every pushed fix invalidates the prior verdict. Close the
        # loop with the bounded exact-head delta review for every fix
        # origin; security-blocked is the only non-review outcome.
        review_task, _ = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_task_id=original_task_id,
            delta_base_sha=str(
                review_loop.get("reviewed_head_sha")
                or review_loop.get("head_sha")
                or ""
            ),
            review_target=rereview_target,
        )
        if review_task:
            logger.info(
                f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}"
            )
        else:
            # _spawn_pipeline_task may have rolled back + committed
            # (DEFERRED lock-contention path). Re-fetch the task for
            # a fresh un-expired instance.
            fresh_task = await session.get(Task, task.id)
            if fresh_task is None:
                await session.commit()
                return task, {"status": "accepted"}
            task = fresh_task

            # Read the precise spawn reason written by
            # _spawn_pipeline_task into the source task's context.
            _sr = (task.context or {}).get(SPAWN_REASON_KEY, "unknown")
            _is_transient = _sr == SPAWN_REASON_DEFERRED

            if _is_transient:
                # Transient deferral (e.g. in-flight peer holds the
                # spawn lock): create a specific alert and retry once.
                logger.warning(
                    "[REVIEW PIPELINE] Review spawn after fix deferred "
                    "for PR #%s (reason=%s) — retrying once",
                    pr_number,
                    _sr,
                )
                from app.alert_dispatch import _maybe_create_failure_alert

                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"Review spawn after fix for PR #{pr_number} "
                    f"deferred ({_sr}) — retrying",
                )
                await asyncio.sleep(5)
                # Re-fetch the task in case the spawn call rolled back
                # the session and expired the ORM instance.
                _reloaded = await session.get(Task, task.id)
                if _reloaded is not None:
                    task = _reloaded
                review_task, _ = await _spawn_pipeline_task(
                    task,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_task_id=original_task_id,
                    delta_base_sha=str(
                        review_loop.get("reviewed_head_sha")
                        or review_loop.get("head_sha")
                        or ""
                    ),
                    review_target=rereview_target,
                )
                if review_task:
                    logger.info(
                        "[REVIEW PIPELINE] Spawned review after fix "
                        "(retry) for PR #%s → task %s",
                        pr_number,
                        review_task.id,
                    )

            if not review_task:
                # Terminal failure: cap, stuck cycle, already merged,
                # or retry also failed. Escalate with accurate reason.
                _spawn_reason_label = _sr.replace("_", " ").title()
                task.latest_status_message = (
                    f"Fix applied for PR #{pr_number} but review spawn "
                    f"{'deferred (concurrent spawn)' if _is_transient else 'failed (' + _spawn_reason_label + ')'}"
                )
                if not _is_transient:
                    task.requires_approval = True
                    from app.rollup_lineage_fence import (
                        mark_ancestor_rollups_awaiting_operator,
                    )

                    await mark_ancestor_rollups_awaiting_operator(
                        task,
                        session,
                        reason=f"review spawn failed ({_sr})",
                        pr_number=pr_number,
                    )
                session.add(task)

                # Always create a WARNING alert with the specific spawn
                # reason so operators see the failure immediately, not
                # just a generic "iteration cap" message.
                _alert_reason_label = (
                    "deferred" if _is_transient else _spawn_reason_label
                )
                alert_title = (
                    f"PR #{pr_number} stalled — review spawn {_alert_reason_label}"
                )
                existing_alert = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title == alert_title,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if not existing_alert:
                    _alert_msg = (
                        f"Fix was applied to PR #{pr_number} but the "
                        f"review spawn failed (reason: {_sr}). "
                        + (
                            "The spawn was deferred due to concurrent "
                            "activity; the next CI cycle will retry."
                            if _is_transient
                            else "Manual intervention may be required."
                        )
                    )
                    alert = Alert(
                        project_id=task.project_id,
                        severity=AlertSeverity.WARNING,
                        title=alert_title,
                        message=_alert_msg,
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
                    "[REVIEW PIPELINE] Review spawn after fix returned None "
                    "for PR #%s (reason: %s)%s",
                    pr_number,
                    _sr,
                    " — transient, will retry" if _is_transient else "",
                )

            # For transient deferrals, mark the CI task so the next
            # backstop cycle detects this as a recent unresolved
            # spawn and accelerates retry.
            if _is_transient:
                from app.time_utils import _utcnow as _ut
                _now = _ut().isoformat()
                # Propagate the deferred marker to the original CI
                # task (not just the fix task) so handle_ci_completion
                # can read it when entering review-wait.
                if original_task_id:
                    try:
                        _ci_task = await session.get(
                            Task, uuid.UUID(original_task_id)
                        )
                        if _ci_task:
                            _ci_ctx = dict(_ci_task.context or {})
                            _ci_rl = dict(
                                _ci_ctx.get("review_loop") or {}
                            )
                            _ci_rl["review_spawn_deferred_at"] = _now
                            _ci_ctx["review_loop"] = _ci_rl
                            _ci_task.context = _ci_ctx
                            flag_modified(_ci_task, "context")
                            session.add(_ci_task)
                    except Exception:
                        logger.exception(
                            "[REVIEW PIPELINE] Failed to propagate "
                            "deferred-spawn marker to CI task %s",
                            original_task_id,
                        )
                # Also set on the fix task's own review_loop as a
                # fallback (used when the CI task lookup fails).
                rl = dict(task.context.get("review_loop") or {})
                rl["review_spawn_deferred_at"] = _now
                task.context = {
                    **(task.context or {}),
                    "review_loop": rl,
                }
                flag_modified(task, "context")
                session.add(task)
                await session.commit()

    return task, None
