"""Parent-child task lifecycle orchestration.

Extracted from main.py to reduce its size.  Contains the core task state
machine: _process_post_commit, _check_and_trigger_rollup, _spawn_pipeline_task,
and supporting functions.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (task_lifecycle is imported BY main) and preserve test-patchability
(tests mock via ``app.main.<name>``).
"""
import copy
import json
import logging
import re
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task, Artifact, TaskHistory, Project,
    TaskStatus, ArtifactType, Alert, AlertSeverity, ProjectStatus,
)
from app.schemas import AgentStatusPayload
from app.routers.agent import _has_local_cycle
from app.shared_state import runtime_config as _runtime_config
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)

async def _has_active_strategic_task(project_id, session: AsyncSession) -> bool:
    """Return True if any QUEUED/PROVISIONING/IN_PROGRESS task is a design_doc_update or strategic_review."""
    active = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    return any(
        (t.first_payload).get("type") in ("design_doc_update", "strategic_review")
        for t in active
    )

async def _increment_research_spawn_count(project: Project, session: AsyncSession) -> int:
    """Increment _research_spawn_count in project.vision and persist. Returns new count."""
    new_count = (project.vision or {}).get("_research_spawn_count", 0) + 1
    project.vision = {**(project.vision or {}), "_research_spawn_count": new_count}
    flag_modified(project, "vision")
    session.add(project)
    return new_count

async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Remaps depends_on from old (cancelled) IDs to new (cloned) IDs.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    # Don't resurrect a cancelled parent
    if parent.status == TaskStatus.CANCELLED:
        logger.info(f"[REQUEUE] Skipping requeue — parent {parent.id} is CANCELLED")
        return 0

    # Cap re-evaluations to prevent infinite loops when children consistently fail
    MAX_PARENT_REQUEUES = 3
    parent_ctx = parent.context or {}
    requeue_count = parent_ctx.get("_requeue_count", 0)
    if requeue_count >= MAX_PARENT_REQUEUES:
        logger.warning(f"[REQUEUE] Parent {parent.id} hit re-evaluation limit ({requeue_count} attempts) — failing permanently")
        parent.status = TaskStatus.FAILED
        parent.latest_status_message = f"Re-evaluation limit reached ({requeue_count} attempts) — children consistently failing"
        session.add(parent)
        await session.commit()
        # Create failure alert
        await _maybe_create_failure_alert(parent, session, parent.latest_status_message)
        return 0

    children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
    parent_staging = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")

    # Pass 1: Clone failed children, build old→new ID mapping
    old_to_new: Dict[str, str] = {}
    cloned_pairs: list = []  # (original, clone)
    retried_count = 0

    for child in children:
        if child.status != TaskStatus.FAILED:
            continue
        # Cancel the failed child so rollup skips it
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = "Parent requeued — replaced by retry"
        session.add(child)

        # Clone as a new task with fresh VM assignment — refresh base_branch
        # to prevent cloning into a deleted branch
        new_payloads = copy.deepcopy(child.payloads or [])
        if parent_staging and new_payloads:
            new_payloads[0]["base_branch"] = parent_staging
        new_child = Task(
            project_id=child.project_id,
            title=child.title,
            description=child.description,
            parent_task_id=parent.id,
            payloads=new_payloads,
            context={k: v for k, v in (child.context or {}).items()
                     if k != "max_plan_account_id"},  # Strip runtime-only fields
            depends_on=[],  # Set in pass 2 after all clones exist
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=child.requires_approval,
        )
        session.add(new_child)
        await session.flush()  # Assigns new_child.id
        old_to_new[str(child.id)] = str(new_child.id)
        cloned_pairs.append((child, new_child))
        retried_count += 1

        history = TaskHistory(task_id=new_child.id, action="SPAWNED",
                              details={"spawned_by": "REQUEUE", "retrying": str(child.id)})
        session.add(history)

    # Pass 2: Remap depends_on from old (cancelled) IDs to new (cloned) IDs
    for original, clone in cloned_pairs:
        remapped = []
        for dep_id in (original.depends_on or []):
            if dep_id in old_to_new:
                remapped.append(old_to_new[dep_id])  # Point to the clone
            else:
                remapped.append(dep_id)  # Keep (COMPLETED sibling or still-active task)
        clone.depends_on = remapped

    # Pass 3: In-memory cycle check on cloned subgraph
    if cloned_pairs:
        graph = {str(c.id): c.depends_on or [] for _, c in cloned_pairs}
        if _has_local_cycle(graph):
            logger.warning(f"[REQUEUE] Circular deps detected in cloned children of {parent.id} — clearing all depends_on")
            for _, clone in cloned_pairs:
                clone.depends_on = []

    parent.context = {**(parent.context or {}), "_requeue_count": requeue_count + 1}
    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = f"Requeued — retrying {retried_count} failed subtask(s)"
    parent.attempt_count += 1
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(str(parent.project_id), "task_updated",
                               {"task_id": str(parent.id), "status": TaskStatus.EVALUATING})
    logger.info(f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried")
    return retried_count

from app.review_pipeline import (
    _spawn_pipeline_task, _max_review_iterations,
    MAX_REVIEW_FIX_ITERATIONS, STUCK_CYCLE_THRESHOLD_DEFAULT, STUCK_CYCLE_THRESHOLD_REFACTOR,
)

async def _update_direction_research_outcomes(
    project_id: uuid.UUID, direction_id: str, research_task_id: str, session: AsyncSession
) -> None:
    """Update a direction's research_history entry with child task outcome counts.

    Called whenever a child task of a research cycle reaches a terminal status so
    that research_history entries accumulate live outcome statistics.
    """
    from app.research_completion import _parse_research_context  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if not project:
        return
    directions = list(project.directions or [])
    updated = False
    for d in directions:
        if d.get("id") != direction_id:
            continue
        history = d.get("research_history", [])
        for entry in history:
            if entry.get("task_id") != research_task_id:
                continue
            child_ids = entry.get("child_task_ids", [])
            if not child_ids:
                break
            child_uuids = []
            for cid in child_ids:
                try:
                    child_uuids.append(uuid.UUID(cid))
                except ValueError:
                    pass
            if not child_uuids:
                break
            child_tasks_stmt = select(Task).where(Task.id.in_(child_uuids))
            child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
            entry["tasks_completed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED)
            entry["tasks_failed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED)
            entry["tasks_cancelled"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED)
            entry["prs_merged"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged")
            )
            # Capture failed task details so research agents can see WHY tasks failed
            failed_details = []
            for ct in child_tasks_rows:
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    reason = (ct.latest_status_message or "")[:120]
                    failed_details.append({"title": ct.title or "", "reason": reason})
            entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat

            # Maintain research_context: promote proposed findings based on outcomes
            ctx = d.get("research_context", "")
            if ctx:
                parsed = _parse_research_context(ctx)
                for ct in child_tasks_rows:
                    title = ct.title or ""
                    if not title:
                        continue
                    if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                        # Move matching proposed finding to Failed Approaches
                        reason = (ct.latest_status_message or "")[:120]
                        new_established = []
                        moved = False
                        for item in parsed["established"]:
                            if title.lower() in item.lower():
                                parsed["failed"].append(f"{title} — {reason}" if reason else title)
                                moved = True
                            else:
                                new_established.append(item)
                        if moved:
                            parsed["established"] = new_established
                    elif ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged"):
                        # Promote (proposed) finding to (confirmed)
                        new_established = []
                        for item in parsed["established"]:
                            if title.lower() in item.lower() and "(proposed)" in item:
                                new_established.append(item.replace("(proposed)", "(confirmed)"))
                            else:
                                new_established.append(item)
                        parsed["established"] = new_established

                # Reassemble the document (preserve eviction summary if present)
                parts: list = []
                if parsed.get("eviction_summary"):
                    parts.append("## Eviction Summary")
                    parts.append(parsed["eviction_summary"])
                    parts.append("")
                for header, key in [
                    ("## Established Findings", "established"),
                    ("## Failed Approaches", "failed"),
                    ("## Open Questions", "open_questions"),
                ]:
                    parts.append(header)
                    for item in parsed[key]:
                        parts.append(f"- {item}")
                    parts.append("")
                updated_ctx = "\n".join(parts).strip() + "\n"
                d["research_context"] = updated_ctx[:8000]

            updated = True
            break
        if updated:
            break
    if updated:
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()

# --- Rollup & Branch Cleanup Helpers ---
# Extracted so both update_agent_status and admin PATCH can trigger rollup.

async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.github import get_pull_request  # noqa: PLC0415

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
                # Update in-memory first
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {**(db_task.context or {}), "pr_closed_unmerged": True}
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
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.github import delete_branch  # noqa: PLC0415

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
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to delete {branch}: {e}", exc_info=True)

async def _check_and_trigger_rollup(child_task: Task, session: AsyncSession) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.
    """
    from app.main import _maybe_create_failure_alert, _utcnow  # noqa: PLC0415
    from app.broadcast import broadcast_to_project  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.security import scan_pr_for_injection  # noqa: PLC0415
    from app.github import (  # noqa: PLC0415
        create_pr_comment, delete_branch, create_pull_request,
        merge_pull_request, get_pull_request, get_pr_files, get_branch_sha,
    )

    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions
    parent = (await session.execute(
        select(Task).where(Task.id == child_task.parent_task_id).with_for_update()
    )).scalars().first()
    if not parent or parent.status != TaskStatus.EVALUATING:
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
            # Some completed but PRs not yet merged — wait for merge verification
            titles = [c.title[:40] for c in unmerged]
            parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            logger.error(f"[ROLLUP] Parent {parent.id} blocked — {len(unmerged)} children completed but unmerged")
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain
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

        completed_count = len(children) - cancelled_count
        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
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
                open_prs = await get_open_pull_requests(gh_token, owner, repo, staging_branch)
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
                        )
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
                try:
                    await get_branch_sha(gh_token, owner, repo, staging_branch)
                except Exception:
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
                        title=pr_title, head=staging_branch, base=target_branch, body=pr_body)

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED")
                        parent.status = TaskStatus.COMPLETED
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception:
                            pass
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

                # Re-acquire lock on parent for merge phase (marker commit released it)
                parent = (await session.execute(
                    select(Task).where(Task.id == parent.id).with_for_update()
                )).scalars().first()
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
                    pr_detail = await get_pull_request(gh_token, owner, repo, rollup_pr_number)
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get("deletions", 0)
                except Exception:
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

                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
                    if merge_result.get("merged"):
                        parent.status = TaskStatus.COMPLETED
                        parent.context = {**parent_ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR auto-merged"
                        await delete_branch(gh_token, owner, repo, staging_branch)
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}, deleted {staging_branch}")
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
                        except Exception:
                            pass
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number)
                            files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                        except Exception:
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
                        fix_task = await _spawn_pipeline_task(
                            parent, "fix", session, rollup_pr_number, staging_branch, target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id)
                        )
                        if fix_task:
                            logger.error(f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}")
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned (iteration cap)"
                            logger.warning(f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number}")
                            # NOTE: Do NOT delete staging_branch here — it contains
                            # all merged child work and requeueing should be possible.
                else:
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                    review_task = await _spawn_pipeline_task(
                        parent, "review", session, rollup_pr_number, staging_branch, target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id)
                    )
                    if review_task:
                        logger.info(f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}")
                    else:
                        parent.status = TaskStatus.FAILED
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"
                        logger.warning(f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number}")
                        # NOTE: Do NOT delete staging_branch here — it contains
                        # all merged child work and requeueing should be possible.
            except Exception as e:
                err_str = str(e).lower()
                is_transient = any(kw in err_str for kw in [
                    "api.github.com", "502", "503", "connection", "timeout",
                    "rate limit", "ssl", "name resolution",
                    # Staging branch may have been deleted by a race — retry
                    # to allow recovery or manual intervention
                    "staging branch", "validation failed",
                ])
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= 3:
                    # Transient GitHub error — leave parent EVALUATING, retry next cycle
                    parent.context = {**(parent.context or {}), "_rollup_retries": rollup_retries}
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/3), will retry: {type(e).__name__}"
                    logger.warning(f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}")
                else:
                    logger.warning(f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True)
                    parent.status = TaskStatus.FAILED
                    parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[:500]
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

async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
) -> dict:
    """Post-commit processing: review pipeline, completion handlers, rollup, aggregation.

    Uses its own session to avoid holding the FOR UPDATE lock from the
    status endpoint while making GitHub API calls (which can hang 15-120s).
    _session_factory is injectable for tests (defaults to AsyncSessionLocal).
    """
    from app.main import _utcnow  # noqa: PLC0415
    from app.broadcast import broadcast_to_project  # noqa: PLC0415
    from app.routers.agent import get_github_token_for_task  # noqa: PLC0415
    from app.security import scan_pr_for_injection  # noqa: PLC0415
    from app.github import (  # noqa: PLC0415
        create_pr_comment, merge_pull_request, get_pull_request,
        get_pr_files, get_check_runs,
    )
    from app.task_completion import (  # noqa: PLC0415
        _handle_plan_completion, _handle_design_doc_completion,
        _handle_security_audit_completion, _handle_perf_completion,
        _handle_investigation_completion,
    )
    from app.strategic_completion import _handle_strategic_completion  # noqa: PLC0415
    from app.research_completion import _handle_research_completion  # noqa: PLC0415

    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalars().first()
        if not task:
            return {"status": "accepted"}

        # Immediately unblock dependents when task terminally fails (retries exhausted)
        if payload.status == TaskStatus.FAILED:
            try:
                async with AsyncSessionLocal() as dep_session:
                    dep_stmt = select(Task).where(
                        Task.project_id == task.project_id,
                        Task.status == TaskStatus.QUEUED,
                        Task.depends_on.contains([str(task.id)]),
                    )
                    dep_result = await dep_session.execute(dep_stmt)
                    waiting_tasks = dep_result.scalars().all()
                    count = 0
                    for wt in waiting_tasks:
                        wt.depends_on = [d for d in (wt.depends_on or []) if d != str(task.id)]
                        dep_session.add(wt)
                        count += 1
                    if count:
                        await dep_session.commit()
                        logger.info(f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks")
            except Exception as e:
                logger.error(f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}", exc_info=True)

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = task.first_payload
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if task_type in ("create", "refactor", "docs", "qa", "deps", "migrate", "") and payload.pr_url and payload.artifact_type == ArtifactType.PULL_REQUEST:
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                short_id = str(task.id).replace('-', '')[:8]
                branch = f"blueprint/docs-{short_id}" if task_type == "docs" else f"blueprint/deps-{short_id}" if task_type == "deps" else f"blueprint/migrate-{short_id}" if task_type == "migrate" else f"blueprint/refactor-{short_id}" if task_type == "refactor" else f"blueprint/task-{short_id}"
                base_branch = task_payload.get("base_branch", "main")
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = base_branch.startswith("research/") or base_branch.startswith("plan/")
                scope = (task.context or {}).get("scope", "unknown")

                if is_intermediate and scope in ("small", "medium"):
                    # Small/medium scope on intermediate branch → security scan then auto-merge, skip review
                    # The rollup PR to main gets the full review — intermediate merges don't need it
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)

                        # Always run security scan before any merge
                        try:
                            scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                            logger.info(f"[SECURITY] Small-scope scan: {scan_result['summary']}")
                            if scan_result["blocked"]:
                                task.requires_approval = True
                                task.latest_status_message = f"Security scan blocked: {scan_result['summary']}"
                                session.add(task)
                                await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                                return {"status": "accepted"}
                            if scan_result["findings"]:
                                await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                        except Exception as e:
                            logger.error(f"[SECURITY] Small-scope scan failed for PR #{pr_number}, blocking: {e}", exc_info=True)
                            task.requires_approval = True
                            task.latest_status_message = f"Security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[:500]
                            session.add(task)
                            await session.commit()
                            return {"status": "accepted"}

                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                        if merge_result.get("merged"):
                            task.context = {**(task.context or {}), "pr_merged": True}
                            session.add(task)
                            await session.commit()
                            await _cleanup_branches(task, session)
                            logger.info(f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} (small scope, intermediate)")
                        else:
                            logger.error(f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}")
                            # Fall through to review pipeline for conflict resolution
                            review_task = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not review_task:
                                task.latest_status_message = f"Auto-merge failed and review not spawned for PR #{pr_number} — iteration cap"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                logger.error(f"[PIPELINE] WARNING: Auto-merge failed and review spawn returned None for PR #{pr_number}")
                    except Exception as e:
                        logger.error(f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}", exc_info=True)
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            fallback_review = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not fallback_review:
                                task.latest_status_message = f"Pipeline stalled: auto-merge failed and review not spawned (iteration cap) for PR #{pr_number}"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                logger.warning(f"[PIPELINE] WARNING: Fallback review spawn returned None for PR #{pr_number}")
                        except Exception as e:
                            logger.error(f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {e}", exc_info=True)
                            task.latest_status_message = f"Pipeline stalled: auto-merge and review spawn both failed — {type(e).__name__}: {e!r}"[:500]
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                        logger.info(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                    except Exception as e:
                        logger.error(f"[SECURITY] Scan failed for PR #{pr_number}, flagging for approval: {e}", exc_info=True)
                        task.requires_approval = True
                        task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[:500]
                        session.add(task)
                        await session.commit()

                    # PR file-path flagging: flag infrastructure/security-critical file changes
                    # Don't block here — let review run, block at merge time.
                    # Gated by require_approval_gates — these paths are hardcoded for the
                    # Blueprint repo and irrelevant for other projects.
                    if _runtime_config.get("require_approval_gates", True):
                        try:
                            pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                            INFRA_FILES = {"backend/app/main.py", "backend/app/compute.py", "backend/app/models.py",
                                           "backend/app/database.py", "backend/app/security.py", "backend/app/auth.py",
                                           "agent/src/index.ts", "CLAUDE.md"}
                            CRITICAL_FILES = {"backend/app/security.py", "backend/app/auth.py", "CLAUDE.md"}
                            touched_infra = [f["filename"] for f in pr_files if f["filename"] in INFRA_FILES]
                            touched_critical = [f for f in touched_infra if f in CRITICAL_FILES]
                            if touched_infra:
                                task.context = {**(task.context or {}), "extended_monitoring": True, "risk_tier": "large",
                                                "touched_infra": touched_infra, "touched_critical": touched_critical}
                                task.latest_status_message = f"Infrastructure change flagged: {', '.join(touched_infra)}"
                                logger.info(f"[PIPELINE] PR #{pr_number} touches infra files: {touched_infra} (critical: {touched_critical})")
                                session.add(task)
                                await session.commit()
                                _shared_state.deploy_monitoring["is_extended"] = True
                        except Exception as e:
                            logger.info(f"[PIPELINE] Could not check PR files for infra flagging: {e}")

                    if not task.requires_approval:
                        review_task = await _spawn_pipeline_task(
                            task, "review", session, pr_number, branch, base_branch,
                            original_prompt=original_prompt, original_task_id=str(task.id)
                        )
                        if review_task:
                            logger.info(f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}")
                        else:
                            task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                            task.requires_approval = True
                            session.add(task)
                            await session.commit()
                            logger.warning(f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}")

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")

                if not pr_number or not branch:
                    logger.info(f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Review pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip review pipeline if PR is already merged or closed
                try:
                    gh_token_check, owner_check, repo_check = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(gh_token_check, owner_check, repo_check, pr_number)
                    pr_state = pr_data_check.get("state", "open")
                    if pr_state == "closed" or pr_data_check.get("merged"):
                        merged_label = "merged" if pr_data_check.get("merged") else "closed"
                        logger.info(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping review pipeline for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review")

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text
                issues = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                import re
                verdict_parsed = False
                verdict_unparseable = False

                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            issues = vj.get("issues", []) if isinstance(vj.get("issues"), list) else []
                            verdict_parsed = True
                            logger.info(f"[REVIEW PIPELINE] Verdict from verdict_json field: {verdict}")
                    except (json.JSONDecodeError, TypeError):
                        logger.error(f"[REVIEW PIPELINE] verdict_json field present but invalid for task {task.id}")

                # Method 2: Parse JSON from log output
                if not verdict_parsed:
                    try:
                        # Find all JSON objects containing "verdict" key (handles any key order)
                        for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                            start = m.start()
                            depth = 0
                            for i in range(start, len(verdict_text)):
                                if verdict_text[i] == '{':
                                    depth += 1
                                elif verdict_text[i] == '}':
                                    depth -= 1
                                    if depth == 0:
                                        try:
                                            candidate = json.loads(verdict_text[start:i+1])
                                            if "verdict" in candidate:
                                                verdict = candidate["verdict"]
                                                feedback = candidate.get("feedback", verdict_text)
                                                issues = candidate.get("issues", []) if isinstance(candidate.get("issues"), list) else []
                                                verdict_parsed = True
                                        except json.JSONDecodeError:
                                            pass
                                        break
                    except Exception as e:
                        logger.error(f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}", exc_info=True)

                if not verdict_parsed:
                    # Fallback: look for APPROVE/REQUEST_CHANGES as standalone words (quoted or unquoted)
                    upper_text = verdict_text.upper()
                    approve_idx = max(
                        upper_text.rfind('"APPROVE"'),
                        upper_text.rfind("APPROVE"),
                        upper_text.rfind("APPROVED"),
                    )
                    rc_idx = max(
                        upper_text.rfind('"REQUEST_CHANGES"'),
                        upper_text.rfind("REQUEST_CHANGES"),
                        upper_text.rfind("REQUEST CHANGES"),
                    )
                    if approve_idx == -1 and rc_idx == -1:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review")
                        verdict_unparseable = True
                    elif approve_idx > rc_idx:
                        verdict = "APPROVE"
                    elif rc_idx > approve_idx:
                        verdict = "REQUEST_CHANGES"
                    else:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(f"[REVIEW PIPELINE] Ambiguous verdict for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review")
                        verdict_unparseable = True

                # If verdict was unparseable, flag the original task for manual review
                if verdict_unparseable:
                    source_loop = (task.context or {}).get("review_loop", {})
                    original_task_id = source_loop.get("original_task_id")
                    if original_task_id:
                        try:
                            orig = await session.get(Task, uuid.UUID(original_task_id))
                            if orig:
                                orig.requires_approval = True
                                orig.latest_status_message = "Review agent did not produce a verdict — manual review required"
                                session.add(orig)
                                await session.commit()
                        except Exception as e:
                            logger.warning(f"[REVIEW PIPELINE] Failed to mark requires_approval on {original_task_id}: {e}", exc_info=True)

                # Post the review to the PR as a comment
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    await create_pr_comment(gh_token, owner, repo, pr_number, review_comment)
                except Exception as e:
                    logger.warning(f"[REVIEW PIPELINE] Failed to post review comment: {e}", exc_info=True)

                action_taken = False
                if verdict == "APPROVE":
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            logger.info(f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge")
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.")
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging (only for PRs targeting main)
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        is_main_merge = base_branch == "main"
                        ci_status = await get_check_runs(gh_token, owner, repo, head_sha) if (head_sha and is_main_merge) else None

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Review approved, waiting for CI on PR #{pr_number}..."
                            task.context = {**(task.context or {}), "review_loop": {
                                **review_loop, "ci_waiting": True, "verdict": "APPROVE",
                                "head_sha": head_sha, "pr_number": pr_number,
                                "branch": branch, "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": _utcnow().isoformat()}}
                            session.add(task)
                            await session.commit()
                            logger.info(f"[CI] PR #{pr_number} approved, CI pending — waiting")
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = ci_status["failed_logs"] or "CI checks failed (no details available)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors. The code review is already "
                                f"approved — don't change logic, just fix CI failures."
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id,
                                fix_reason="ci_failure"
                            )
                            if fix_task:
                                logger.error(f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}")
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            logger.info(f"[CI] PR #{pr_number} CI passed")

                        # Critical files gate: block merge if security-critical files were touched
                        if original_task_id and _runtime_config.get("require_approval_gates", True):
                            try:
                                orig_task = await session.get(Task, uuid.UUID(original_task_id))
                                critical_touched = (orig_task.context or {}).get("touched_critical", []) if orig_task else []
                                if critical_touched:
                                    orig_task.requires_approval = True
                                    orig_task.status = TaskStatus.EVALUATING
                                    orig_task.latest_status_message = f"Review approved — awaiting human approval to merge (critical files: {', '.join(critical_touched)})"
                                    session.add(orig_task)
                                    await session.commit()
                                    await broadcast_to_project(str(orig_task.project_id), "task_updated",
                                                               {"task_id": str(orig_task.id), "status": TaskStatus.EVALUATING})
                                    await create_pr_comment(gh_token, owner, repo, pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but PR touches security-critical files ({', '.join(critical_touched)}). Merge requires human approval.")
                                    logger.error(f"[PIPELINE] PR #{pr_number} review approved but merge blocked — critical files: {critical_touched}")
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(f"[PIPELINE] Error checking critical files for merge gate: {e}", exc_info=True)

                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)

                        if merge_result.get("merged"):
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.")
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    orig = await session.get(Task, uuid.UUID(original_task_id))
                                    if orig:
                                        orig.context = {**(orig.context or {}), "pr_merged": True}
                                        # If this was a rollup PR review, mark the parent as COMPLETED
                                        is_rollup_merge = orig.status == TaskStatus.EVALUATING
                                        if is_rollup_merge:
                                            orig.status = TaskStatus.COMPLETED
                                            orig.latest_status_message = "Rollup PR merged"
                                        session.add(orig)
                                        await session.commit()
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Spawn a smoketest after rollup merges to main
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if is_rollup_merge and base_branch == "main":
                                            try:
                                                type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                                type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                                health_check_urls = _runtime_config.get("health_check_urls") or []
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[{
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **({"model": type_model} if type_model else {}),
                                                        **({"effort": type_effort} if type_effort else {}),
                                                        **({"health_check_urls": health_check_urls} if health_check_urls else {}),
                                                    }],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                    status=TaskStatus.QUEUED,
                                                    created_by="ORCHESTRATOR",
                                                )
                                                session.add(smoketest_task)
                                                await session.commit()
                                                await session.refresh(smoketest_task)
                                                history = TaskHistory(
                                                    task_id=smoketest_task.id, action="SPAWNED",
                                                    details={"spawned_by": "POST_ROLLUP_MERGE", "base_branch": base_branch}
                                                )
                                                session.add(history)
                                                await session.commit()
                                                await broadcast_to_project(
                                                    str(task.project_id), "task_updated",
                                                    {"task_id": str(smoketest_task.id), "status": TaskStatus.QUEUED}
                                                )
                                                logger.info(f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after rollup merge to {base_branch}")
                                            except Exception as e:
                                                logger.warning(f"[SMOKETEST] Failed to spawn smoketest after rollup merge: {e}", exc_info=True)
                                        # Spawn security rescan if merged task's parent was a security_audit
                                        if orig.parent_task_id:
                                            try:
                                                audit_parent = await session.get(Task, orig.parent_task_id)
                                                if (audit_parent and
                                                        audit_parent.first_payload.get("type") == "security_audit" and
                                                        audit_parent.status == TaskStatus.COMPLETED):
                                                    # Only rescan once per original audit
                                                    existing_rescan = (await session.execute(
                                                        select(Task).where(
                                                            Task.parent_task_id == audit_parent.id,
                                                            Task.title.ilike("%rescan%")
                                                        )
                                                    )).scalars().first()
                                                    if not existing_rescan:
                                                        try:
                                                            pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                                                            changed_files = ", ".join(f["filename"] for f in pr_files)
                                                        except Exception:
                                                            changed_files = "(could not fetch file list)"
                                                        audit_title = audit_parent.title
                                                        type_model = _runtime_config.get("task_type_models", {}).get("security_audit") or None
                                                        type_effort = _runtime_config.get("task_type_efforts", {}).get("security_audit") or None
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[{
                                                                "type": "security_audit",
                                                                "prompt": (
                                                                    f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                    f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                    f"Output only PASS or remaining FAIL findings."
                                                                ),
                                                                **({"model": type_model} if type_model else {}),
                                                                **({"effort": type_effort} if type_effort else {}),
                                                            }],
                                                            parent_task_id=audit_parent.id,
                                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                            status=TaskStatus.QUEUED,
                                                            requires_approval=False,
                                                            created_by="ORCHESTRATOR",
                                                        )
                                                        session.add(rescan_task)
                                                        await session.commit()
                                                        await session.refresh(rescan_task)
                                                        history = TaskHistory(
                                                            task_id=rescan_task.id, action="SPAWNED",
                                                            details={
                                                                "spawned_by": "POST_SECURITY_FIX_MERGE",
                                                                "pr_number": pr_number,
                                                                "audit_parent_id": str(audit_parent.id),
                                                            }
                                                        )
                                                        session.add(history)
                                                        await session.commit()
                                                        await broadcast_to_project(
                                                            str(orig.project_id), "task_updated",
                                                            {"task_id": str(rescan_task.id), "status": TaskStatus.QUEUED}
                                                        )
                                                        logger.info(f"[SECURITY RESCAN] Spawned rescan task {rescan_task.id} after security fix merge PR #{pr_number}")
                                            except Exception as e:
                                                logger.info(f"[SECURITY RESCAN] Failed to spawn security rescan: {e}")
                                except Exception as e:
                                    logger.warning(f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}", exc_info=True)
                            logger.info(f"[REVIEW PIPELINE] PR #{pr_number} approved and merged")
                            action_taken = True
                        else:
                            # Merge blocked — spawn fix task to rebase and resolve conflicts
                            merge_msg = merge_result.get("message", "unknown")
                            # Fetch PR file list for conflict context
                            try:
                                pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                                files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                            except Exception as e:
                                logger.warning(f"[REVIEW PIPELINE] Failed to fetch file list for PR #{pr_number}: {e}")
                                files_list = "  (could not fetch file list)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Spawning fix task to rebase and resolve conflicts.")
                            orig_goal = ""
                            if original_task_id:
                                try:
                                    orig_t = await session.get(Task, uuid.UUID(original_task_id))
                                    if orig_t:
                                        orig_goal = (orig_t.description or orig_t.title or "")[:500]
                                except Exception:
                                    pass
                            fix_prompt = (
                                f"This PR (#{pr_number}) was approved but could not be merged.\n"
                                f"Error: {merge_msg}\n"
                                + (f"\nPR goal: {orig_goal}\n" if orig_goal else "") +
                                f"\nFiles changed in this PR:\n{files_list}\n\n"
                                f"Steps to resolve:\n"
                                f"1. Run: git fetch origin {base_branch}\n"
                                f"2. Run: git rebase origin/{base_branch}\n"
                                f"3. If rebase conflicts occur: {base_branch} is the source of truth. Keep its refactors and restructuring.\n"
                                f"   Re-apply only the changes that serve the PR goal (described above) on top of {base_branch}'s current state.\n"
                                f"   If files were heavily rewritten on {base_branch}, adapt this PR's changes to the new structure.\n"
                                f"4. Run: git rebase --continue after resolving each conflicting file\n"
                                f"5. If rebase is too complex, abort with git rebase --abort, then use git merge origin/{base_branch} and resolve conflict markers"
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=fix_prompt,
                                original_task_id=original_task_id
                            )
                            if fix_task:
                                logger.error(f"[REVIEW PIPELINE] Merge blocked for PR #{pr_number}, spawned rebase fix → task {fix_task.id}")
                            action_taken = True
                    except Exception as e:
                        logger.warning(f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}: {e}", exc_info=True)
                        task.latest_status_message = f"Review approved but merge/CI-check failed: {type(e).__name__}: {e!r}"[:500]
                        session.add(task)
                        await session.commit()
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait)
                    ci_errors_text = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs(gh_token, owner, repo, head_sha)
                            if ci_check.get("conclusion") == "failure" and ci_check.get("failed_logs"):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        logger.error(f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}", exc_info=True)
                    if issues:
                        critical = [i for i in issues if i.get('severity') == 'critical']
                        major = [i for i in issues if i.get('severity') == 'major']
                        minor = [i for i in issues if i.get('severity') == 'minor']
                        structured = ""
                        if critical:
                            structured += "\n\nCRITICAL issues (must fix):\n" + "\n".join(f"- {i['description']}" for i in critical)
                        if major:
                            structured += "\n\nMajor issues:\n" + "\n".join(f"- {i['description']}" for i in major)
                        if minor:
                            structured += "\n\nMinor issues:\n" + "\n".join(f"- {i['description']}" for i in minor)
                        fix_prompt_text = f"A code review requested changes. Address the following issues:{structured}{ci_errors_text}"
                    else:
                        fix_prompt_text = f"A code review requested changes. Please address the following feedback:\n\n{feedback}{ci_errors_text}"
                    fix_task = await _spawn_pipeline_task(
                        task, "fix", session, pr_number, branch, base_branch,
                        fix_prompt=fix_prompt_text,
                        original_prompt=original_prompt,
                        original_task_id=original_task_id,
                        current_feedback=feedback,
                        current_issues=issues
                    )
                    if fix_task:
                        logger.info(f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}")
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = f"PR #{pr_number} stalled — iteration cap reached"
                        existing_cap_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title == cap_alert_title,
                            )
                        )).scalars().first()
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Review requested changes on PR #{pr_number} but the fix iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(str(task.project_id), "alert_created",
                                {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                        else:
                            await session.commit()
                        logger.warning(f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}")
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})")

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")

                if not pr_number or not branch:
                    logger.info(f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Fix pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip if PR is already merged or closed
                try:
                    gh_token_check, owner_check, repo_check = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(gh_token_check, owner_check, repo_check, pr_number)
                    if pr_data_check.get("state") == "closed" or pr_data_check.get("merged"):
                        merged_label = "merged" if pr_data_check.get("merged") else "closed"
                        logger.info(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping fix re-review for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — re-review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding")

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                    logger.info(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                except Exception as e:
                    logger.error(f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}", exc_info=True)
                    task.requires_approval = True
                    task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[:500]
                    session.add(task)
                    await session.commit()

                fix_reason = review_loop.get("fix_reason", "code_review")
                if not scan_blocked and fix_reason == "ci_failure":
                    # CI-only fix: skip re-review, go straight to CI check + merge.
                    # The original review already approved the code logic.
                    ci_loop = review_loop.copy()
                    ci_loop["ci_waiting"] = True
                    ci_loop["ci_wait_started"] = _utcnow().isoformat()
                    ci_loop["ci_poll_interval"] = 10
                    task.status = TaskStatus.EVALUATING
                    task.context = {**(task.context or {}), "review_loop": ci_loop}
                    task.latest_status_message = f"CI-only fix applied for PR #{pr_number} — waiting for CI (review already approved)"
                    session.add(task)
                    await session.commit()
                    logger.info(f"[REVIEW PIPELINE] CI-only fix for PR #{pr_number} — skipping re-review, waiting for CI")
                elif not scan_blocked:
                    review_task = await _spawn_pipeline_task(
                        task, "review", session, pr_number, branch, base_branch,
                        original_task_id=original_task_id
                    )
                    if review_task:
                        logger.info(f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}")
                    else:
                        task.latest_status_message = f"Fix applied but review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = f"PR #{pr_number} stalled — iteration cap reached"
                        existing_cap_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title == cap_alert_title,
                            )
                        )).scalars().first()
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Fix was applied to PR #{pr_number} but the review iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(str(task.project_id), "alert_created",
                                {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                        else:
                            await session.commit()
                        logger.warning(f"[REVIEW PIPELINE] WARNING: Review spawn after fix returned None for PR #{pr_number}")

            # Completion handlers — wrapped in try/except to prevent VM leak.
            # If a handler throws after the early commit, BackgroundTasks (including
            # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
            # VM cleanup via asyncio.create_task as a fallback.
            try:
                if task_type == "plan":
                    plan_logs = payload.logs or task.latest_status_message or ""
                    await _handle_plan_completion(task, plan_logs, session)

                elif task_type == "design_doc_update":
                    doc_logs = payload.logs or task.latest_status_message or ""
                    await _handle_design_doc_completion(task, doc_logs, session)

                elif task_type == "strategic_review":
                    review_logs = payload.logs or task.latest_status_message or ""
                    await _handle_strategic_completion(task, review_logs, session)

                elif task_type == "research":
                    research_logs = payload.logs or task.latest_status_message or ""
                    await _handle_research_completion(task, research_logs, session)

                elif task_type == "security_audit":
                    audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_security_audit_completion(task, audit_logs, session)

                elif task_type == "perf":
                    perf_logs = payload.logs or task.latest_status_message or ""
                    await _handle_perf_completion(task, perf_logs, session)

                elif payload.artifact_type == ArtifactType.INVESTIGATION_REPORT:
                    await _handle_investigation_completion(task, payload, session)
            except Exception as handler_exc:
                logger.error(
                    f"[STATUS] Completion handler failed for {task_type} task {task.id}: {handler_exc!r}",
                    exc_info=True,
                )
                # Detect transient errors (GitHub API, DB, network) and requeue instead of FAILED
                err_str = str(handler_exc).lower()
                is_transient = any(kw in err_str for kw in [
                    "api.github.com", "502", "503", "connection", "timeout",
                    "rate limit", "ssl", "name resolution", "connection refused",
                ])
                if is_transient and task.attempt_count < _runtime_config["max_retries"]:
                    task.status = TaskStatus.QUEUED
                    task.attempt_count += 1
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    task.started_at = None
                    task.latest_status_message = f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}"
                    logger.warning(f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}")
                else:
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Completed but handler failed: {type(handler_exc).__name__}: {str(handler_exc)[:200]}"
                    )
                session.add(task)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            try:
                await _check_and_trigger_rollup(task, session)
            except Exception as e:
                logger.error(f"[ROLLUP] Rollup check failed for task {task.id}: {e!r}", exc_info=True)

        # 6b. Update direction research history outcomes for child tasks of a research cycle
        _task_ctx = task.context or {}
        if (
            _task_ctx.get("direction_id")
            and _task_ctx.get("research_task_id")
            and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ):
            try:
                await _update_direction_research_outcomes(
                    task.project_id,
                    _task_ctx["direction_id"],
                    _task_ctx["research_task_id"],
                    session,
                )
            except Exception as _outcome_exc:
                logger.warning(
                    f"[RESEARCH] Failed to update direction research outcomes for task {task.id}: {_outcome_exc!r}"
                )

        # 7. Project State Aggregation
        # Only consider executable tasks (those with a VM) for aggregation.
        # Parent/logical tasks are updated to match the project outcome.
        all_tasks_stmt = select(Task).where(Task.project_id == task.project_id)
        all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
        executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
        parent_tasks = [t for t in all_tasks if not t.vm_instance_id and t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]]

        project_stmt = select(Project).where(Project.id == task.project_id)
        project = (await session.execute(project_stmt)).scalars().first()

        if project and project.status != ProjectStatus.CANCELLED and executable_statuses:
            new_project_status = None
            if all(s == TaskStatus.COMPLETED for s in executable_statuses):
                new_project_status = ProjectStatus.COMPLETED
            elif any(s == TaskStatus.FAILED for s in executable_statuses):
                new_project_status = ProjectStatus.FAILED
            elif all(s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED] for s in executable_statuses):
                new_project_status = ProjectStatus.CANCELLED

            if new_project_status:
                project.status = new_project_status
                session.add(project)
                # Update parent/logical tasks to match the project outcome
                terminal_task_status = TaskStatus.COMPLETED if new_project_status == ProjectStatus.COMPLETED else TaskStatus.FAILED if new_project_status == ProjectStatus.FAILED else TaskStatus.CANCELLED
                for pt in parent_tasks:
                    pt.status = terminal_task_status
                    session.add(pt)
                await session.commit()
                await broadcast_to_project(str(project.id), "project_updated", {"status": new_project_status})

        # Final commit for any deferred changes (status, artifacts, spawned tasks)
        if session.dirty or session.new:
            await session.commit()

        # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": payload.status}
        )

        return {
            "status": "received",
            "message": f"Status update for task {payload.task_id} recorded."
        }
