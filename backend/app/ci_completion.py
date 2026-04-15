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
CI" — it is NOT permission to merge. The legacy ``verdict == "APPROVE"`` form is
accepted as a fallback so in-flight tasks from before the gate split still merge.
"""
import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, TaskStatus, _utcnow

logger = logging.getLogger(__name__)


def _review_approved(review_loop: dict) -> bool:
    """True if a review task signed off on this PR.

    Accepts the new ``review_approved`` flag and the legacy ``verdict == "APPROVE"``
    form so EVALUATING tasks written before the gate split still merge.
    """
    if review_loop.get("review_approved"):
        return True
    return review_loop.get("verdict") == "APPROVE"


async def _attempt_merge_with_retry_and_escalation(
    task: Task,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str,
) -> bool:
    """Both gates are open — try to merge. Mutates ``task`` in place; caller commits.

    Handles the existing retry / rule-block escalation / merge-failure path that
    used to live inline inside handle_ci_completion. Returns True iff merged.
    """
    from app.github import merge_pull_request, create_pr_comment, get_pull_request
    from app.main import _maybe_create_failure_alert

    ci_loop = (task.context or {}).get("review_loop", {})
    ci_pr_number = ci_loop.get("pr_number")
    ci_payload = task.first_payload
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")

    merge_result = await merge_pull_request(gh_token, owner, repo, ci_pr_number)
    if not merge_result.get("merged") and source != "webhook":
        # Retry with backoff (skip in webhook context — would timeout delivery)
        for attempt in range(3):
            await asyncio.sleep(5 * (3 ** attempt))
            merge_result = await merge_pull_request(gh_token, owner, repo, ci_pr_number)
            if merge_result.get("merged"):
                break

    if merge_result.get("merged"):
        await create_pr_comment(gh_token, owner, repo, ci_pr_number,
            "**Blueprint CI + Review**: All checks passed, PR auto-merged.")
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = f"CI passed + review approved, PR #{ci_pr_number} merged ({source})"
        if ci_orig_id:
            try:
                orig = await session.get(Task, uuid.UUID(ci_orig_id))
                if orig:
                    orig.context = {**(orig.context or {}), "pr_merged": True}
                    if orig.status == TaskStatus.EVALUATING and not (orig.context or {}).get("review_loop", {}).get("ci_waiting"):
                        orig.status = TaskStatus.COMPLETED
                        orig.latest_status_message = "Rollup PR merged"
                    session.add(orig)
            except Exception as e:
                logger.warning(f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True)
        logger.info(f"[CI] PR #{ci_pr_number} merged ({source})")
        return True

    merge_msg = merge_result.get("message", "")
    if "in progress" in merge_msg.lower() or "status check" in merge_msg.lower():
        ci_loop["ci_poll_interval"] = 10
        ci_loop["ci_last_polled"] = _utcnow().isoformat()
        ci_loop.setdefault("ci_wait_started", _utcnow().isoformat())
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        task.latest_status_message = f"Merge blocked (checks re-running on PR #{ci_pr_number}) — back to CI polling"
        logger.warning(f"[CI] PR #{ci_pr_number} merge blocked by in-progress checks — returning to CI wait ({source})")
        return False

    try:
        _recheck_pr = await get_pull_request(gh_token, owner, repo, ci_pr_number)
        if _recheck_pr and (_recheck_pr.get("merged") or _recheck_pr.get("state") == "closed"):
            task.status = TaskStatus.COMPLETED
            task.latest_status_message = f"PR #{ci_pr_number} already merged by another task"
            logger.info(f"[CI] PR #{ci_pr_number} was already merged — marking complete ({source})")
            return True
        # Detect non-transient merge blocks — escalate immediately instead of retrying
        _merge_msg_lower = merge_msg.lower()
        _is_rule_block = any(kw in _merge_msg_lower for kw in [
            "rule violation", "branch protection", "required review",
            "required status", "merge restrictions", "not allowed to merge",
            "at least", "approving review",
        ])
        if _is_rule_block:
            task.latest_status_message = (
                f"CI passed but merge blocked on PR #{ci_pr_number}: "
                f"{merge_msg[:200]} — requires manual intervention"
            )
            task.requires_approval = True
            ci_loop["merge_rule_blocked"] = True
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            session.add(task)
            if ci_orig_id:
                try:
                    orig = await session.get(Task, uuid.UUID(ci_orig_id))
                    if orig:
                        orig.requires_approval = True
                        session.add(orig)
                except Exception as e:
                    logger.warning(f"[CI] Failed to set requires_approval on {ci_orig_id}: {e}", exc_info=True)
            await _maybe_create_failure_alert(task, session,
                f"PR #{ci_pr_number} merge blocked by repository rules — needs manual action")
            logger.warning(f"[CI] PR #{ci_pr_number} merge blocked by repo rules — escalated ({source})")
        else:
            _merge_failures = ci_loop.get("merge_failures", 0) + 1
            ci_loop["merge_failures"] = _merge_failures
            ci_loop["ci_poll_interval"] = min(30 * _merge_failures, 120)
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            task.latest_status_message = f"Merge attempt {_merge_failures} failed on PR #{ci_pr_number}: {merge_msg} — retrying"
            logger.warning(f"[CI] PR #{ci_pr_number} merge failed ({_merge_failures}x): {merge_msg} — will retry ({source})")
            if _merge_failures >= 10 and not ci_loop.get("merge_escalated"):
                ci_loop["merge_escalated"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                await _maybe_create_failure_alert(task, session,
                    f"Rollup PR #{ci_pr_number} merge failed {_merge_failures} times — still retrying")
    except Exception:
        ci_loop["ci_poll_interval"] = 30
        ci_loop["ci_last_polled"] = _utcnow().isoformat()
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        task.latest_status_message = f"Merge check failed on PR #{ci_pr_number} — retrying"
        logger.warning(f"[CI] PR #{ci_pr_number} merge recheck failed — will retry ({source})")
    return False


async def handle_ci_completion(
    task: Task,
    ci_result: dict,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str = "poll",
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
    from app.main import _maybe_create_failure_alert
    from app.broadcast import broadcast_to_project

    ci_loop = (task.context or {}).get("review_loop", {})
    ci_pr_number = ci_loop.get("pr_number")
    ci_payload = task.first_payload
    ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
    ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")

    if ci_result["status"] != "completed":
        return False

    if ci_result["conclusion"] == "success":
        # Record the CI gate signal. JSONB columns don't track in-place dict
        # mutations — flag_modified ensures the change is flushed to disk.
        ci_loop["ci_passed"] = True
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")

        if not _review_approved(ci_loop):
            # Review hasn't approved yet — leave parked, just persist the signal.
            # The review task's APPROVE handler will write review_approved on the
            # parent and trigger the gate when it runs.
            task.latest_status_message = f"CI passed on PR #{ci_pr_number} — waiting for review approval"
            logger.info(f"[CI] PR #{ci_pr_number} CI passed, waiting for review approval ({source})")
            session.add(task)
            await session.commit()
            await broadcast_to_project(str(task.project_id), "task_updated",
                                       {"task_id": str(task.id), "status": task.status})
            return True

        # Both gates met — perform the merge
        await _attempt_merge_with_retry_and_escalation(
            task, gh_token, owner, repo, session, source=source
        )
    else:
        # CI failed — spawn fix task
        active_tasks = (await session.execute(
            select(Task).where(
                Task.project_id == task.project_id,
                Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
            )
        )).scalars().all()
        existing_fix = next(
            (t for t in active_tasks
             if t.first_payload.get("type") == "fix"
             and t.first_payload.get("pr_number") == ci_pr_number),
            None
        )
        if existing_fix:
            task.status = TaskStatus.COMPLETED
            task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
            logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id} ({source})")
            session.add(task)
            await session.commit()
            await broadcast_to_project(str(task.project_id), "task_updated",
                                       {"task_id": str(task.id), "status": task.status})
            return True

        ci_errors = ci_result.get("failed_logs", "CI checks failed")
        await create_pr_comment(gh_token, owner, repo, ci_pr_number,
            f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```")
        ci_fix_prompt = (
            f"PR #{ci_pr_number} is blocked by failing CI checks (code review already approved).\n\n"
            f"IMPORTANT — verify current state before acting. Between when this task was spawned and "
            f"when you run, the PR's mergeable state or check-run results may have changed. After cloning:\n"
            f"  1. Attempt `git rebase origin/{ci_base}` — if conflicts appear, resolve them first.\n"
            f"  2. Inspect the failing checks via the GitHub API "
            f"(`curl -H \"Authorization: Bearer $GITHUB_TOKEN\" "
            f"https://api.github.com/repos/{owner}/{repo}/commits/$(git rev-parse HEAD)/check-runs`) "
            f"to confirm which checks are still failing on the latest head.\n\n"
            f"CI errors captured at spawn time:\n{ci_errors[:2000]}\n\n"
            f"Focus: fix the build/lint/test errors — the code review is already approved, "
            f"don't re-architect unrelated code.\n\n"
            f"Before declaring done: ensure (a) `git rebase origin/{ci_base}` succeeds cleanly with no "
            f"remaining conflict markers, (b) local lint/type/test suites pass on the changed files, "
            f"and (c) every fix is committed."
        )
        fix_task, _ = await _spawn_pipeline_task(
            task, "fix", session, ci_pr_number, ci_branch, ci_base,
            fix_prompt=ci_fix_prompt,
            original_task_id=ci_orig_id,
            fix_reason="ci_failure"
        )
        if fix_task:
            ci_loop_ctx = dict(ci_loop)
            ci_loop_ctx["ci_waiting"] = False
            ci_loop_ctx["ci_fix_spawned"] = str(fix_task.id)
            task.context = {**(task.context or {}), "review_loop": ci_loop_ctx}
            task.status = TaskStatus.COMPLETED
            task.latest_status_message = f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
            logger.error(f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id} ({source})")
        else:
            task.status = TaskStatus.FAILED
            task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
            logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None ({source})")
            from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
            from app.routers.agent_status import record_task_result  # noqa: PLC0415
            _fc = classify_failure(task)
            persist_failure_category(task, _fc)
            record_task_result(success=False, failure_class=_fc, task_id=str(task.id))

    if task.status == TaskStatus.FAILED:
        await _maybe_create_failure_alert(
            task, session,
            f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
        )
    session.add(task)
    await session.commit()
    await broadcast_to_project(str(task.project_id), "task_updated",
                               {"task_id": str(task.id), "status": task.status})
    return True
