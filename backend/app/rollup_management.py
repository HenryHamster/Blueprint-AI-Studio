"""Rollup PR management and branch cleanup.

Extracted from task_lifecycle.py to reduce its size.  Contains the three
rollup-related helpers: _verify_pr_merged, _cleanup_branches, and
_check_and_trigger_rollup.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (rollup_management is imported BY main indirectly via task_lifecycle)
and preserve test-patchability.
"""
import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task, Artifact, Project,
    TaskStatus, ArtifactType,
)
from app.shared_state import runtime_config as _runtime_config
from app.review_pipeline import _spawn_pipeline_task

logger = logging.getLogger(__name__)


async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    from app.main import _maybe_create_failure_alert
    from app.routers.agent import get_github_token_for_task
    from app.github import get_pull_request

    if (task.context or {}).get("pr_merged"):
        return True
    # Look for PR artifact to verify via API
    art_result = await session.execute(
        select(Artifact).where(Artifact.task_id == task.id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )
    artifact = art_result.scalars().first()
    if not artifact or not artifact.pr_url:
        # No PR artifact — task completed without creating a PR (e.g., no changes needed).
        # Treat as "done" so it doesn't block rollup.
        # Cache in context so partial-rollup path (which checks context.pr_merged directly)
        # doesn't see this task as "unmerged".
        task.context = {**(task.context or {}), "pr_merged": True}
        return True
    if artifact and artifact.pr_url:
        try:
            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            try:
                pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            except HTTPException as api_err:
                if api_err.status_code not in (401, 500, 502, 503):
                    raise
                if api_err.status_code != 401:
                    # Transient GitHub API failure — can't verify now, will retry next cycle
                    logger.warning(f"[PR_VERIFY] GitHub API error ({api_err.status_code}) checking PR #{pr_number} for task {task.id} — skipping")
                    return False
                # Token expired — invalidate cache and retry once with a fresh token
                from app.routers.agent import invalidate_github_token_cache
                invalidate_github_token_cache(str(task.project_id))
                logger.warning(f"[PR_VERIFY] Auth failure for task {task.id} PR #{pr_number} — retrying with fresh token")
                gh_token, owner, repo = await get_github_token_for_task(task.id, session, force_refresh=True)
                pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("merged"):
                # Update in-memory first so caller sees the change even if DB write fails
                task.context = {**(task.context or {}), "pr_merged": True}
                # Persist to DB in separate session (best-effort — may fail if caller
                # holds FOR UPDATE lock on this row, but in-memory update is sufficient
                # for the current rollup cycle)
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {**(db_task.context or {}), "pr_merged": True}
                            write_session.add(db_task)
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(f"[PR_VERIFY] Failed to persist pr_merged for task {task.id}: {write_err}")
                return True
            # Detect closed-but-not-merged (rejected/abandoned PR) — don't block rollup forever
            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                logger.warning(f"[PR_VERIFY] Task {task.id} PR #{pr_number} was closed without merging — marking as rejected")
                # Update in-memory first — set message AND status together so the
                # outer session's commit always carries the explanatory message,
                # even if the separate write_session below fails (row lock, etc.).
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                task.latest_status_message = f"PR #{pr_number} closed without merging"
                from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
                from app.routers.agent_status import record_task_result  # noqa: PLC0415
                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(success=False, failure_class=_fc, task_id=str(task.id))
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {
                                **(db_task.context or {}),
                                "pr_closed_unmerged": True,
                                "failure_category": _fc,
                            }
                            db_task.status = TaskStatus.FAILED
                            db_task.latest_status_message = f"PR #{pr_number} closed without merging"
                            write_session.add(db_task)
                            await _maybe_create_failure_alert(
                                db_task, write_session,
                                f"PR #{pr_number} was closed without merging"
                            )
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(f"[PR_VERIFY] Failed to persist pr_closed_unmerged for task {task.id}: {write_err}")
                return True  # Return True so callers don't block — task is now FAILED which they handle
        except Exception as e:
            logger.warning(f"[PR_VERIFY] Failed to verify PR merge for task {task.id}: {e}", exc_info=True)
    return False

async def _cleanup_branches(task: Task, session: AsyncSession) -> None:
    """Delete staging and feature branches that are no longer needed."""
    from app.routers.agent import get_github_token_for_task
    from app.github import delete_branch

    ctx = task.context or {}
    gh_token = owner = repo = None

    async def _ensure_token():
        nonlocal gh_token, owner, repo
        if not gh_token:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    branches_to_delete = []

    # Staging branches (research/*, plan/*) are shared by ALL sibling tasks.
    # Only delete when the PARENT task completes (rollup), not on individual child completion.
    # Check if this task IS the parent (has plan_output or is a research task).
    is_parent = task.plan_output is not None or (task.first_payload).get("type") in ("research", "plan")
    if is_parent:
        staging = ctx.get("plan_branch") or ctx.get("research_branch")
        if staging:
            # Guard 1: Don't delete if children are still active (they push to this branch)
            active_children = (await session.execute(
                select(Task).where(
                    Task.parent_task_id == task.id,
                    Task.status.in_([
                        TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS
                    ])
                )
            )).scalars().all()
            if active_children:
                logger.warning(f"[CLEANUP] Skipping deletion of {staging} — "
                               f"{len(active_children)} child task(s) still active")
            else:
                # Guard 2: Don't delete branches that are target branches for other active rollups
                from sqlalchemy import or_ as sa_or
                referencing_parents = (await session.execute(
                    select(Task).where(
                        Task.status == TaskStatus.EVALUATING,
                        Task.id != task.id,
                        sa_or(
                            Task.context["parent_base"].as_string() == staging,
                            Task.context["original_base"].as_string() == staging,
                        )
                    )
                )).scalars().all()
                if referencing_parents:
                    logger.warning(f"[CLEANUP] Skipping deletion of {staging} — "
                                   f"{len(referencing_parents)} active parent(s) target it as rollup base")
                else:
                    # Guard 3: Don't delete if there are still open PRs targeting it
                    try:
                        _gh, _owner, _repo = await get_github_token_for_task(task.id, session)
                        from app.github import get_open_pull_requests
                        open_prs = await get_open_pull_requests(_gh, _owner, _repo, staging)
                        if open_prs:
                            pr_nums = [f"#{p['number']}" for p in open_prs]
                            logger.warning(f"[CLEANUP] Skipping deletion of {staging} — {len(open_prs)} open PR(s) targeting it: {', '.join(pr_nums)}")
                        else:
                            branches_to_delete.append(staging)
                    except Exception:
                        # If we can't check, don't delete (safe default)
                        logger.warning(f"[CLEANUP] Skipping deletion of {staging} — could not verify open PRs")

    # Feature branches after PR merge
    if ctx.get("pr_merged"):
        for payload in (task.payloads or []):
            branch = payload.get("branch")
            if branch and branch.startswith("blueprint/"):
                branches_to_delete.append(branch)

    for branch in branches_to_delete:
        try:
            await _ensure_token()
            deleted = await delete_branch(gh_token, owner, repo, branch)
            if deleted:
                logger.info(f"[CLEANUP] Deleted branch {branch} (task {task.id})")
        except Exception as e:  # noqa: PERF203 — per-branch try is required so one delete failure doesn't abort the rest
            logger.warning(f"[CLEANUP] Failed to delete {branch}: {e}", exc_info=True)

async def _check_and_trigger_rollup(child_task: Task, session: AsyncSession) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.
    """
    from app.main import _maybe_create_failure_alert, _utcnow
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment, delete_branch, create_pull_request,
        merge_pull_request, get_pull_request, get_pr_files, get_branch_sha,
    )

    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions.
    # skip_locked: if a sibling child is already running this check, defer — the
    # reaper's EVALUATING stale-parent sweep retries within 15 min as safety net.
    parent = (await session.execute(
        select(Task).where(Task.id == child_task.parent_task_id).with_for_update(skip_locked=True)
    )).scalars().first()
    if parent is None:
        logger.info(
            f"[ROLLUP] Skipped rollup check for task {child_task.id} — parent lock held, "
            "reaper sweep will retry"
        )
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    all_children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id).with_for_update()
    )).scalars().all()

    # Consider create, refactor, plan, research, and specialized agent tasks for rollup decisions.
    # Review/fix tasks are pipeline helpers that operate on the same PR — excluded.
    ROLLUP_TYPES = {"create", "refactor", "migrate", "", "plan", "research", "security_audit", "qa", "deps", "docs", "smoketest", "perf"}
    children = [c for c in all_children
                if (c.first_payload).get("type", "create") in ROLLUP_TYPES]

    # Guard: if all children are review/fix types (none in ROLLUP_TYPES), mark parent COMPLETED
    if not children and all_children:
        parent.status = TaskStatus.COMPLETED
        parent.latest_status_message = "All subtasks were pipeline tasks (review/fix) — no rollup needed"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
        logger.info(f"[ROLLUP] Parent {parent.id} completed — all children were review/fix pipeline tasks")
        return

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress
    pipeline_active = any(
        c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)
        for c in all_children
        if (c.first_payload).get("type") in ("review", "fix")
    )

    # Check all non-cancelled create children are done with PRs merged
    all_done = True
    cancelled_count = 0
    active_count = 0
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        # COMPLETED — check pr_merged for create tasks
        if not await _verify_pr_merged(c, session):
            all_done = False

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        if pipeline_active and (parent.latest_status_message or "").startswith("Blocked:"):
            parent.latest_status_message = "Fix/review pipeline running — waiting for PR to merge"
            session.add(parent)
            await session.commit()
        return

    # All children terminal but not all succeeded — finalize immediately instead of waiting 6 hours
    if not all_done and active_count == 0 and not pipeline_active:
        unmerged = [c for c in children
                    if c.status == TaskStatus.COMPLETED and not (c.context or {}).get("pr_merged")]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        completed_merged = [c for c in children
                           if c.status == TaskStatus.COMPLETED and (c.context or {}).get("pr_merged")]

        if unmerged:
            # Some completed but PRs not yet merged.
            # Check if any are stale (>10min) with no active review — respawn pipeline.
            now = _utcnow()
            stale_threshold = timedelta(minutes=10)
            respawned = 0
            for c in unmerged:
                age = (now - c.updated_at).total_seconds() if c.updated_at else 0
                if age < stale_threshold.total_seconds():
                    continue  # Too recent — normal pipeline may still be working
                # Look up PR artifact to get pr_number and branch
                pr_art = (await session.execute(
                    select(Artifact).where(
                        Artifact.task_id == c.id,
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    ).limit(1)
                )).scalars().first()
                if not pr_art or not pr_art.pr_url:
                    # No PR artifact — _verify_pr_merged should have handled this.
                    # Force-set pr_merged so next cycle picks it up.
                    c.context = {**(c.context or {}), "pr_merged": True}
                    session.add(c)
                    logger.info(f"[ROLLUP] Child {str(c.id)[:8]} has no PR artifact — marking as merged")
                    continue
                try:
                    pr_number = int(pr_art.pr_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    logger.warning(f"[ROLLUP] Could not parse PR number from {pr_art.pr_url} for child {str(c.id)[:8]}")
                    continue
                c_payload = c.first_payload
                branch = (c.context or {}).get("branch") or f"blueprint/task-{str(c.id).replace('-', '')[:8]}"
                base_branch = c_payload.get("base_branch", "main")
                original_prompt = c_payload.get("prompt", c.description or "")
                review_task, _ = await _spawn_pipeline_task(
                    c, "review", session, pr_number, branch, base_branch,
                    original_prompt=original_prompt, original_task_id=str(c.id),
                )
                if review_task and review_task.id != c.id:
                    respawned += 1
                    logger.warning(f"[ROLLUP] Respawned review for stale child {str(c.id)[:8]} PR #{pr_number} → review {str(review_task.id)[:8]}")

            titles = [c.title[:40] for c in unmerged]
            if respawned:
                parent.latest_status_message = f"Recovering: respawned {respawned} review(s) for unmerged PR(s) — {', '.join(titles)}"
            else:
                parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            logger.info(f"[ROLLUP] Parent {parent.id} — {len(unmerged)} unmerged, {respawned} reviews respawned")
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain.
        # Before giving up on failed children, attempt retry via _requeue_parent_task.
        if failed:
            MAX_PARENT_REQUEUES = 3
            parent_ctx = parent.context or {}
            requeue_count = parent_ctx.get("_requeue_count", 0)
            if requeue_count < MAX_PARENT_REQUEUES:
                # Retry: clone and re-queue failed children; parent returns to EVALUATING.
                # Next rollup cycle will produce a full rollup if retries succeed, or fall
                # through to partial rollup once the requeue limit is exhausted.
                from app.task_lifecycle import _requeue_parent_task
                logger.info(
                    f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, "
                    f"{len(completed_merged)} succeeded. Requeuing failed children "
                    f"(attempt {requeue_count + 1}/{MAX_PARENT_REQUEUES}) before partial rollup."
                )
                await _requeue_parent_task(parent, session)
                return
            # Requeue limit exhausted — fall through to partial rollup or full failure.
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — requeue limit reached "
                f"({requeue_count}/{MAX_PARENT_REQUEUES}); proceeding with partial rollup or failure."
            )

        if completed_merged:
            # Some succeeded — proceed with partial rollup of what worked
            logger.warning(f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, {len(completed_merged)} succeeded. Rolling up successes.")
            all_done = True  # Let rollup proceed with merged children
            # Fall through to rollup logic below
        else:
            # Nothing succeeded — fail the parent immediately
            parent.status = TaskStatus.FAILED
            parent.latest_status_message = f"{len(failed)} of {len(children)} subtask(s) failed, none succeeded"
            session.add(parent)
            await _maybe_create_failure_alert(parent, session, f"{len(failed)} subtasks failed, none succeeded")
            await session.commit()
            await broadcast_to_project(str(parent.project_id), "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED})
            logger.error(f"[ROLLUP] Parent {parent.id} — all {len(failed)} children failed, marking parent FAILED")
            return

    child_statuses = [c.status for c in children]

    # Edge case: all children cancelled — nothing to rollup, mark parent cancelled
    if children and cancelled_count == len(children):
        parent.status = TaskStatus.CANCELLED
        parent.latest_status_message = f"All {len(children)} subtasks were cancelled"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.CANCELLED})
        logger.info(f"[ROLLUP] Parent {parent.id} cancelled — all children cancelled")
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        failed_count = len(failed_children)
        completed_count = len(children) - cancelled_count - failed_count
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note += f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"
        if failed_children:
            rollup_note += f"\n\n*{len(failed_children)} subtask(s) failed and were excluded from this rollup.*"

        if staging_branch and target_branch and staging_branch != target_branch:
            gh_token = owner = repo = None
            try:
                gh_token, owner, repo = await get_github_token_for_task(parent.id, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests
                try:
                    open_prs = await get_open_pull_requests(gh_token, owner, repo, staging_branch)
                except HTTPException as api_err:
                    if api_err.status_code == 401:
                        from app.routers.agent import invalidate_github_token_cache
                        invalidate_github_token_cache(str(parent.project_id))
                        logger.warning(f"[ROLLUP] Auth failure listing open PRs for {owner}/{repo} — retrying with fresh token")
                        gh_token, owner, repo = await get_github_token_for_task(parent.id, session, force_refresh=True)
                        open_prs = await get_open_pull_requests(gh_token, owner, repo, staging_branch)
                    elif api_err.status_code in (403, 500, 502, 503):
                        logger.warning(f"[ROLLUP] GitHub error {api_err.status_code} listing open PRs — deferring to next cycle")
                        return
                    else:
                        raise
                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    parent.latest_status_message = f"Rollup waiting: {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)}"
                    session.add(parent)
                    await session.commit()
                    logger.info(f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting")
                    return

                # Verify target branch exists before creating PR.
                # Distinguish 404 (actually deleted) from transient GitHub errors
                # (5xx, rate-limit) — only fail on confirmed 404.
                try:
                    await get_branch_sha(gh_token, owner, repo, target_branch)
                except HTTPException as branch_err:
                    if branch_err.status_code == 404:
                        raise Exception(
                            f"Target branch '{target_branch}' does not exist — "
                            f"cannot create rollup PR. This indicates a branch was "
                            f"deleted out of order."
                        ) from branch_err
                    # Transient GitHub error — defer to next evaluation cycle
                    logger.warning(
                        f"[ROLLUP] Transient GitHub error checking target branch "
                        f"'{target_branch}': {branch_err.detail} — will retry"
                    )
                    return

                # Verify staging (head) branch exists — if it was deleted
                # (e.g., by a child rollup that found an empty diff and cleaned
                # up, or by GitHub auto-delete after a prior PR merge), check
                # whether all children completed successfully. If so, the work
                # already landed on the target branch and we can complete.
                staging_branch_missing = False
                try:
                    await get_branch_sha(gh_token, owner, repo, staging_branch)
                except HTTPException as http_err:
                    if http_err.status_code == 404:
                        logger.warning(f"[ROLLUP] Staging branch '{staging_branch}' not found (404) for task {parent.id}")
                        staging_branch_missing = True
                    elif http_err.status_code in (401, 403):
                        logger.error(f"[ROLLUP] Auth error {http_err.status_code} checking staging branch — deferring")
                        return
                    else:
                        logger.warning(f"[ROLLUP] GitHub {http_err.status_code} checking staging branch — deferring")
                        return
                except Exception as e:
                    logger.warning(f"[ROLLUP] Error checking staging branch '{staging_branch}': {e} — deferring")
                    return
                if staging_branch_missing:
                    # Check if all children completed — if so, work already landed
                    all_children_ok = completed_count > 0 and failed_count == 0
                    if all_children_ok:
                        logger.info(
                            f"[ROLLUP] Staging branch '{staging_branch}' gone but all "
                            f"{completed_count} children completed — work already on {target_branch}"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.latest_status_message = (
                            f"All {completed_count} subtasks completed with no actionable "
                            f"changes — research produced no new code to merge"
                        )
                        # Note on the direction that this cycle found nothing actionable
                        research_payload = parent.first_payload
                        dir_id = research_payload.get("current_direction_id")
                        if dir_id:
                            project = await session.get(Project, parent.project_id)
                            if project and project.directions:
                                directions = list(project.directions)
                                for d in directions:
                                    if d.get("id") == dir_id:
                                        notes = d.get("notes") or []
                                        notes.append({
                                            "text": "Research cycle completed with no actionable changes — all findings already implemented or not applicable.",
                                            "added_at": _utcnow().isoformat(),
                                            "source": "rollup_empty_diff",
                                        })
                                        d["notes"] = notes[-10:]  # Keep last 10
                                        break
                                project.directions = directions
                                flag_modified(project, "directions")
                                session.add(project)
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id), "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED}
                        )
                        return
                    raise Exception(
                        f"Staging branch '{staging_branch}' does not exist and "
                        f"{failed_count} child(ren) failed — cannot create rollup PR"
                    )

                is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")

                # Resume from prior crash — skip PR creation if already done
                existing_pr = parent_ctx.get("rollup_pr_number")
                if existing_pr:
                    # Validate PR is still open — it may have been merged/closed since crash
                    try:
                        pr_state = await get_pull_request(gh_token, owner, repo, existing_pr)
                        if pr_state.get("merged"):
                            logger.info(f"[ROLLUP] PR #{existing_pr} already merged — marking COMPLETED")
                            parent.status = TaskStatus.COMPLETED
                            parent.context = {**parent_ctx, "pr_merged": True}
                            parent.latest_status_message = f"Rollup PR #{existing_pr} already merged"
                            session.add(parent)
                            await session.commit()
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(str(parent.project_id), "task_updated",
                                                       {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
                            return
                        if pr_state.get("state") == "closed":
                            logger.warning(f"[ROLLUP] PR #{existing_pr} was closed without merge — clearing marker, will retry")
                            parent.context = {k: v for k, v in parent_ctx.items()
                                             if k not in ("rollup_pr_number", "rollup_pr_url")}
                            parent_ctx = parent.context
                            existing_pr = None
                            session.add(parent)
                            await session.commit()
                    except Exception as e:
                        logger.warning(f"[ROLLUP] Could not verify PR #{existing_pr} state: {e} — proceeding with existing marker")

                if existing_pr:
                    rollup_pr_number = existing_pr
                    rollup_pr_url = parent_ctx.get("rollup_pr_url", "")
                    logger.info(f"[ROLLUP] Resuming with existing PR #{rollup_pr_number}")
                else:
                    pr_title = f"[Blueprint] Rollup: {parent.title}"
                    pr_body = f"Rollup of {completed_count} subtasks from `{staging_branch}`{rollup_note}\n\n---\n*Automated by Blueprint Pipeline*"

                    pr_result = await create_pull_request(gh_token, owner, repo,
                        title=pr_title, head=staging_branch, base=target_branch, body=pr_body, project_id=str(parent.project_id))

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED")
                        parent.status = TaskStatus.COMPLETED
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as e:
                            logger.warning(f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent.id}: {e}")
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(str(parent.project_id), "task_updated",
                                                   {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
                        return

                    rollup_pr_url = pr_result.get("html_url", "")
                    rollup_pr_number = pr_result.get("number")
                    logger.info(f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}")

                    # Commit marker immediately — if we crash after this point,
                    # restart will find the marker and skip PR creation
                    parent.context = {**parent_ctx, "rollup_pr_number": rollup_pr_number, "rollup_pr_url": rollup_pr_url}
                    parent_ctx = parent.context  # Update local ref
                    session.add(parent)
                    await session.commit()

                # Re-acquire lock on parent for merge phase (marker commit released it).
                # skip_locked: rollup PR marker is already persisted, so if a concurrent
                # sibling holds the lock we defer safely — reaper sweep will retry within 15 min.
                parent_id = parent.id
                parent = (await session.execute(
                    select(Task).where(Task.id == parent_id).with_for_update(skip_locked=True)
                )).scalars().first()
                if parent is None:
                    logger.info(
                        f"[ROLLUP] Skipped merge phase for parent {parent_id} — lock held, "
                        "reaper sweep will retry"
                    )
                    return
                parent_ctx = parent.context or {}

                # Guard: concurrent sibling may have created a different rollup PR in the gap
                if parent_ctx.get("rollup_pr_number") != rollup_pr_number:
                    logger.info(f"[ROLLUP] Concurrent sibling created rollup PR for parent {parent.id} — yielding")
                    return

                # Check if any child task was high-risk — if so, require approval on the final merge.
                # Refactor tasks are structural (move code, no behavior change) and should not
                # trigger the high-risk gate even when classified as risk='high' by plan agents.
                # Gate is controlled by runtime setting (default True) — disable for repos
                # that don't need human-in-the-loop approval.
                has_high_risk_child = any(
                    (c.context or {}).get("risk") == "high"
                    for c in children
                    if c.status == TaskStatus.COMPLETED
                    and (c.first_payload or {}).get("type") != "refactor"  # refactors are structural, not behavioral risk
                )
                if has_high_risk_child and not is_target_intermediate and _runtime_config.get("require_approval_gates", True):
                    parent.requires_approval = True
                    parent.latest_status_message = (
                        f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting approval "
                        f"(contains high-risk changes)"
                    )
                    parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                    session.add(parent)
                    await session.commit()
                    await broadcast_to_project(str(parent.project_id), "task_updated",
                                               {"task_id": str(parent.id), "status": parent.status})
                    logger.info(f"[ROLLUP] PR #{rollup_pr_number} has high-risk children — requires approval before merge to {target_branch}")
                    return

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                try:
                    pr_detail = await get_pull_request(gh_token, owner, repo, rollup_pr_number, project_id=str(parent.project_id))
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get("deletions", 0)
                except Exception as e:
                    logger.warning(f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} diff size for task {parent.id}: {e}")
                    lines_changed = 999  # assume large if we can't check
                is_small_rollup = lines_changed <= 100
                logger.info(f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}")
                if is_target_intermediate and is_small_rollup:
                    # Always run security scan before any merge
                    try:
                        rollup_scan = await scan_pr_for_injection(gh_token, owner, repo, rollup_pr_number)
                        logger.info(f"[SECURITY] Rollup scan: {rollup_scan['summary']}")
                        if rollup_scan["blocked"]:
                            parent.requires_approval = True
                            parent.latest_status_message = f"Rollup security scan blocked: {rollup_scan['summary']}"
                            parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                            if rollup_scan["findings"]:
                                await create_pr_comment(gh_token, owner, repo, rollup_pr_number, rollup_scan["report"])
                            session.add(parent)
                            await session.commit()
                            return
                        if rollup_scan["findings"]:
                            await create_pr_comment(gh_token, owner, repo, rollup_pr_number, rollup_scan["report"])
                    except Exception as e:
                        logger.error(f"[SECURITY] Rollup scan failed for PR #{rollup_pr_number}, blocking: {e}", exc_info=True)
                        parent.requires_approval = True
                        parent.latest_status_message = f"Rollup security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[:500]
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                        session.add(parent)
                        await session.commit()
                        return

                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number, project_id=str(parent.project_id))
                    if merge_result.get("merged"):
                        parent.status = TaskStatus.COMPLETED
                        parent.context = {**parent_ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR auto-merged"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as del_err:
                            logger.warning(f"[ROLLUP] Failed to delete staging branch {staging_branch}: {del_err}")
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}")
                        try:
                            from app.notification_dispatch import post_pr_merged_message
                            _direction_id = (parent.context or {}).get("direction_id")
                            _direction_text = "unknown direction"
                            if _direction_id:
                                _proj = await session.get(Project, parent.project_id)
                                if _proj:
                                    for _d in (_proj.directions or []):
                                        if str(_d.get("id", "")) == str(_direction_id):
                                            _direction_text = _d.get("text", "unknown direction")
                                            break
                            await post_pr_merged_message(
                                session, parent.project_id, rollup_pr_number,
                                _direction_text, completed_count, parent.id,
                            )
                        except Exception as _e:
                            logger.warning(f"[NOTIFY] Failed to post PR merged message: {_e}")
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        # Observability: check if concurrent rollups may explain this conflict
                        try:
                            recent_rollups = (await session.execute(
                                select(_func.count()).select_from(Task).where(
                                    Task.status == TaskStatus.COMPLETED,
                                    Task.id != parent.id,
                                    Task.updated_at >= _utcnow() - timedelta(hours=1),
                                    Task.context["rollup_pr_url"].as_string().is_not(None),
                                )
                            )).scalar() or 0
                            if recent_rollups > 0:
                                logger.warning(
                                    f"[ROLLUP] Conflict on PR #{rollup_pr_number} may be stale-window: "
                                    f"{recent_rollups} other rollup(s) merged to {target_branch} in the last hour"
                                )
                        except Exception as e:
                            logger.warning(f"[ROLLUP] Failed to query concurrent rollup count for PR #{rollup_pr_number} (task {parent.id}): {e}")
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number, "_is_rollup_review": True}
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number)
                            files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                        except Exception as e:
                            logger.warning(f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} file list for task {parent.id}: {e}")
                            files_list = "  (could not fetch file list)"
                        parent_goal = (parent.description or parent.title or "")[:500]
                        fix_prompt = (
                            f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                            f"Error: {merge_msg}\n\n"
                            f"Rollup goal: {parent_goal}\n\n"
                            f"Files in this PR:\n{files_list}\n\n"
                            f"Steps to resolve:\n"
                            f"1. Run: git fetch origin {target_branch}\n"
                            f"2. Run: git rebase origin/{target_branch}\n"
                            f"3. Resolve any conflicts — {target_branch} is the source of truth. Keep its refactors and restructuring.\n"
                            f"   Re-apply only the changes that serve the rollup goal (described above) on top of {target_branch}'s current state.\n"
                            f"   If files were heavily rewritten on {target_branch}, adapt the rollup changes to the new structure.\n"
                            f"4. Run: git rebase --continue after resolving each file"
                        )
                        fix_task, _ = await _spawn_pipeline_task(
                            parent, "fix", session, rollup_pr_number, staging_branch, target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id)
                        )
                        if fix_task:
                            logger.error(f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}")
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned (iteration cap)"
                            from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
                            from app.routers.agent_status import record_task_result  # noqa: PLC0415
                            _fc = classify_failure(parent)
                            persist_failure_category(parent, _fc)
                            record_task_result(success=False, failure_class=_fc, task_id=str(parent.id))
                            logger.warning(f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number}")
                            # NOTE: Do NOT delete staging_branch here — it contains
                            # all merged child work and requeueing should be possible.
                else:
                    # Rollup-to-main / large rollup: review and CI run in parallel.
                    # ci_waiting=True only tells the orchestrator to poll CI; it is
                    # NOT permission to merge. Both ci_passed and review_approved
                    # must be set on this review_loop before the merge gate fires
                    # (see app.ci_completion._review_approved + the rollup gate
                    # handoff in app.task_lifecycle's verdict==APPROVE branch).
                    # Do NOT pre-seed review_approved here — the spawned review task
                    # writes it back to this parent's review_loop on APPROVE.
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {**parent_ctx,
                                      "rollup_pr_url": rollup_pr_url,
                                      "rollup_pr_number": rollup_pr_number,
                                      "_is_rollup_review": True,
                                      "review_loop": {
                                          "ci_waiting": True,
                                          "pr_number": rollup_pr_number,
                                          "branch": staging_branch,
                                          "base_branch": target_branch,
                                          "original_task_id": str(parent.id),
                                          "ci_wait_started": _utcnow().isoformat(),
                                      }}
                    review_task, _ = await _spawn_pipeline_task(
                        parent, "review", session, rollup_pr_number, staging_branch, target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id)
                    )
                    if review_task:
                        logger.info(f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}")
                    else:
                        parent.status = TaskStatus.FAILED
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"
                        from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
                        from app.routers.agent_status import record_task_result  # noqa: PLC0415
                        _fc = classify_failure(parent)
                        persist_failure_category(parent, _fc)
                        record_task_result(success=False, failure_class=_fc, task_id=str(parent.id))
                        logger.warning(f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number}")
                        # NOTE: Do NOT delete staging_branch here — it contains
                        # all merged child work and requeueing should be possible.
            except Exception as e:
                err_str = str(e).lower()
                # Auth failures: invalidate the cached token so the next retry uses a fresh one
                if isinstance(e, HTTPException) and e.status_code == 401:
                    from app.routers.agent import invalidate_github_token_cache
                    invalidate_github_token_cache(str(parent.project_id))
                    logger.warning(f"[ROLLUP] Auth failure (401) for parent {parent.id} — invalidated token cache")
                is_transient = (
                    isinstance(e, HTTPException) and e.status_code in (401, 500, 502, 503, 504)
                ) or any(kw in err_str for kw in [
                    "api.github.com",
                    "500 internal server error", "502 bad gateway", "503 service unavailable", "504 gateway timeout",
                    "internal server error",
                    "connection", "timeout",
                    "rate limit", "ssl", "name resolution",
                    # Staging branch may have been deleted by a race — retry
                    # to allow recovery or manual intervention
                    "staging branch", "validation failed",
                    # Auth failures — token expired/revoked; cache is invalidated above
                    "unauthorized", "bad credentials", "token expired",
                ])
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= (max_rollup := _runtime_config.get("max_rollup_retries", 5)):
                    # Transient GitHub error — leave parent EVALUATING, retry next cycle
                    parent.context = {**(parent.context or {}), "_rollup_retries": rollup_retries}
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/{max_rollup}), will retry: {type(e).__name__}"
                    logger.warning(f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}")
                else:
                    logger.warning(f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True)
                    parent.status = TaskStatus.FAILED
                    parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[:500]
                    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
                    from app.routers.agent_status import record_task_result  # noqa: PLC0415
                    _fc = classify_failure(parent)
                    persist_failure_category(parent, _fc)
                    record_task_result(success=False, failure_class=_fc, task_id=str(parent.id))
                    # NOTE: Do NOT delete staging_branch here — it contains all
                    # merged child work. Preserving it allows manual recovery or
                    # requeue. Branches are cleaned up by _cleanup_branches when
                    # the parent eventually reaches a terminal state, or by the
                    # stale branch cleanup service after 7 days.
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = f"All {completed_count} subtasks completed and merged"

        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": parent.status})
        logger.info(f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})")

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = f"{failed_count} of {len(children)} subtasks failed"
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        logger.error(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")
