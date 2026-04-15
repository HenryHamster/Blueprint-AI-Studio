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
from app.payload_schemas import validate_internal_payload
from app.notification_dispatch import post_auto_fix_result_message
from app.failure_classification import classify_failure

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

        # Capture before cancellation — cancellation overwrites both
        child_extra_logs = (child.context or {}).get("last_error", "")
        failure_class = classify_failure(child, logs=child_extra_logs or None)
        original_status_message = child.latest_status_message

        # Auto-generate playbook entry if this failure pattern is recurring
        try:
            from app.playbook_learning import _maybe_auto_playbook  # noqa: PLC0415
            await _maybe_auto_playbook(child, failure_class, original_status_message, session)
        except Exception:
            logger.exception("[AUTO_PLAYBOOK] Failed to process playbook for task %s", child.id)

        # Cancel the failed child so rollup skips it
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = "Parent requeued — replaced by retry"
        session.add(child)

        # Clone as a new task with fresh VM assignment — refresh base_branch
        # to prevent cloning into a deleted branch
        new_payloads = copy.deepcopy(child.payloads or [])
        if parent_staging and new_payloads:
            new_payloads[0]["base_branch"] = parent_staging

        # Inject retry guidance into prompt_suffix based on failure class
        child_ctx = child.context or {}
        task_scope = child_ctx.get("scope", "medium")
        attempt = child_ctx.get("_attempt_number", 1)
        clone_ctx = {k: v for k, v in child_ctx.items() if k != "max_plan_account_id"}
        clone_ctx["_attempt_number"] = attempt + 1
        clone_ctx["_previous_error"] = (original_status_message or "")[:1500]
        clone_ctx["_previous_failure_class"] = failure_class

        if new_payloads and isinstance(new_payloads[0], dict):
            existing_suffix = new_payloads[0].get("prompt_suffix", "") or ""
            extra_guidance = ""

            if failure_class == "AGENT_TIMEOUT":
                scope_guidance = (
                    "IMPORTANT: The previous attempt TIMED OUT. You MUST reduce scope:\n"
                    "- Focus on the single most critical change only\n"
                    "- Skip non-essential improvements, refactors, or cleanup\n"
                    "- Commit working changes early and often\n"
                    "- If the task requires changes across many files, pick the 2-3 most important\n"
                )
                extra_guidance = scope_guidance
                # Downgrade scope on second+ AGENT_TIMEOUT for large-scope tasks
                if attempt >= 2 and task_scope == "large":
                    clone_ctx["scope"] = "medium"
                    clone_ctx["_scope_reduced"] = True

            elif failure_class == "CODE_ERROR":
                error_text = (original_status_message or "").lower()
                hints = []
                if any(kw in error_text for kw in ["test", "assert", "pytest", "jest", "vitest"]):
                    hints.append("Fix the specific failing tests shown in the error log before making other changes.")
                if any(kw in error_text for kw in ["lint", "ruff", "eslint", "tsc"]):
                    hints.append("Fix lint/type errors first — run the linter before committing.")
                if hints:
                    extra_guidance = "RETRY GUIDANCE: " + " ".join(hints) + "\n"

            if extra_guidance:
                new_payloads[0]["prompt_suffix"] = extra_guidance + ("\n" + existing_suffix if existing_suffix else "")

        new_payloads = [validate_internal_payload(p, "task_lifecycle:requeue_child") for p in new_payloads]
        new_child = Task(
            project_id=child.project_id,
            title=child.title,
            description=child.description,
            parent_task_id=parent.id,
            payloads=new_payloads,
            context=clone_ctx,  # Strip runtime-only fields; may include _scope_reduced
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

from app.rollup_management import _check_and_trigger_rollup, _verify_pr_merged, _cleanup_branches

import random as _random

async def _merge_with_retry(
    merge_pull_request_fn, gh_token: str, owner: str, repo: str,
    pr_number: int, project_id: str, max_attempts: int = 3
) -> dict:
    """Call merge_pull_request with exponential backoff for transient 5xx errors.

    Returns the merge result dict on success/conflict (same semantics as
    merge_pull_request).  Only retries on raised exceptions — non-200 merge
    results (405/409) are returned as-is without retrying.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await merge_pull_request_fn(gh_token, owner, repo, pr_number, project_id=project_id)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                backoff = (2 ** attempt) * (1 + _random.random())  # ~1-2s, ~2-4s
                logger.warning(
                    f"[PIPELINE] Merge attempt {attempt + 1} for PR #{pr_number} raised {type(exc).__name__}, "
                    f"retrying in {backoff:.1f}s: {exc!r}"
                )
                await asyncio.sleep(backoff)
    raise last_exc  # type: ignore[misc]

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
        get_pr_files, get_check_runs, submit_pr_review, update_check_run,
    )
    from app.task_completion import (  # noqa: PLC0415
        _handle_plan_completion, _handle_design_doc_completion,
        _handle_security_audit_completion, _handle_perf_completion,
        _handle_investigation_completion,
    )
    from app.strategic_completion import _handle_strategic_completion, _record_strategic_task_failure  # noqa: PLC0415
    from app.research_completion import _handle_research_completion  # noqa: PLC0415

    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id).with_for_update()
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

            # Record strategic task failures for the circuit breaker and cycle history
            if task.first_payload.get("type") in ("design_doc_update", "strategic_review"):
                try:
                    await _record_strategic_task_failure(task, session)
                except Exception as _strat_exc:
                    logger.error(f"[STRATEGIC] Failed to record strategic failure for {task.id}: {_strat_exc!r}", exc_info=True)

            # Post result message for permanently failed auto-remediation tasks
            if (task.context or {}).get("auto_remediation") is True:
                try:
                    _ar_branch = (task.context or {}).get("ci_failure_branch", "main")
                    await post_auto_fix_result_message(
                        session, task.project_id,
                        branch=_ar_branch,
                        result="failed",
                        task_id=task.id,
                    )
                    await session.commit()
                except Exception as _ar_exc:
                    logger.warning("[AUTO-REMEDIATION] Failed to post failure result message: %s", _ar_exc)

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
                                if (task.context or {}).get("auto_remediation") is True:
                                    try:
                                        await post_auto_fix_result_message(
                                            session, task.project_id,
                                            branch=(task.context or {}).get("ci_failure_branch", base_branch),
                                            result="review_needed",
                                            pr_number=pr_number,
                                            task_id=task.id,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning("[AUTO-REMEDIATION] Failed to post review_needed message: %s", _ar_exc)
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
                            if (task.context or {}).get("auto_remediation") is True:
                                try:
                                    await post_auto_fix_result_message(
                                        session, task.project_id,
                                        branch=(task.context or {}).get("ci_failure_branch", base_branch),
                                        result="review_needed",
                                        pr_number=pr_number,
                                        task_id=task.id,
                                    )
                                except Exception as _ar_exc:
                                    logger.warning("[AUTO-REMEDIATION] Failed to post review_needed message: %s", _ar_exc)
                            await session.commit()
                            return {"status": "accepted"}

                        merge_result = await _merge_with_retry(merge_pull_request, gh_token, owner, repo, pr_number, project_id=str(task.project_id))
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
                                logger.error(f"[PIPELINE] Auto-merge failed and review spawn returned None for PR #{pr_number} — parking in CI-wait")
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge failed for PR #{pr_number}, review cap hit — CI monitor will retry merge"
                                task.context = {**(task.context or {}), "review_loop": {
                                    "ci_waiting": True, "pr_number": pr_number,
                                    "branch": branch, "base_branch": base_branch,
                                    "original_task_id": str(task.id),
                                    "ci_wait_started": _utcnow().isoformat()}}
                                session.add(task)
                                await session.commit()
                    except Exception as e:
                        logger.error(f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}", exc_info=True)
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            fallback_review = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not fallback_review:
                                logger.warning(f"[PIPELINE] Fallback review spawn returned None for PR #{pr_number} — parking in CI-wait")
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge error for PR #{pr_number}, review cap hit — CI monitor will retry merge"
                                task.context = {**(task.context or {}), "review_loop": {
                                    "ci_waiting": True, "pr_number": pr_number,
                                    "branch": branch, "base_branch": base_branch,
                                    "original_task_id": str(task.id),
                                    "ci_wait_started": _utcnow().isoformat()}}
                                session.add(task)
                                await session.commit()
                        except Exception as inner_e:
                            logger.error(f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {inner_e}", exc_info=True)
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Pipeline recovery: merge and review spawn failed for PR #{pr_number} — CI monitor will retry"
                            task.context = {**(task.context or {}), "review_loop": {
                                "ci_waiting": True, "pr_number": pr_number,
                                "branch": branch, "base_branch": base_branch,
                                "original_task_id": str(task.id),
                                "ci_wait_started": _utcnow().isoformat()}}
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

                # Method 2: Parse JSON from log output (string-aware bracket counting)
                if not verdict_parsed:
                    try:
                        for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                            start = m.start()
                            depth = 0
                            in_string = False
                            escape_next = False
                            for i in range(start, len(verdict_text)):
                                c = verdict_text[i]
                                if escape_next:
                                    escape_next = False
                                    continue
                                if c == '\\' and in_string:
                                    escape_next = True
                                    continue
                                if c == '"' and not escape_next:
                                    in_string = not in_string
                                    continue
                                if in_string:
                                    continue
                                if c == '{':
                                    depth += 1
                                elif c == '}':
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
                    # Fallback: look for APPROVE/REQUEST_CHANGES/COMMENT as standalone words (quoted or unquoted)
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
                    comment_idx = max(
                        upper_text.rfind('"COMMENT"'),
                        upper_text.rfind("COMMENT"),
                    )
                    if approve_idx == -1 and rc_idx == -1 and comment_idx == -1:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review")
                        verdict_unparseable = True
                    else:
                        best_idx = max(approve_idx, rc_idx, comment_idx)
                        if best_idx == approve_idx:
                            verdict = "APPROVE"
                        elif best_idx == comment_idx and comment_idx > rc_idx:
                            verdict = "COMMENT"
                        else:
                            verdict = "REQUEST_CHANGES"

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

                # Post the review to the PR via GitHub Reviews API (falls back to plain comment)
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    event = "APPROVE" if verdict == "APPROVE" else ("COMMENT" if verdict == "COMMENT" else "REQUEST_CHANGES")
                    inline_comments = []
                    body_issues = []
                    for issue in issues:
                        if issue.get("file") and issue.get("line"):
                            sev = issue.get("severity", "minor").upper()
                            inline_comments.append({
                                "path": issue["file"],
                                "line": issue["line"],
                                "body": f"[{sev}] {issue.get('description', '')}",
                            })
                        else:
                            body_issues.append(issue)
                    review_body = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    if body_issues:
                        issues_text = "\n".join(
                            f"- [{i.get('severity', '?').upper()}] {i.get('description', '')}"
                            for i in body_issues
                        )
                        review_body += f"\n\n**Issues:**\n{issues_text}"
                    result = await submit_pr_review(
                        gh_token, owner, repo, pr_number, event, review_body, inline_comments
                    )
                    if result is None:
                        raise ValueError("PR not found")
                except Exception as e:
                    logger.warning(f"[REVIEW PIPELINE] submit_pr_review failed, falling back: {e}")
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                        await create_pr_comment(gh_token, owner, repo, pr_number, review_comment)
                    except Exception as e2:
                        logger.warning(f"[REVIEW PIPELINE] Fallback comment also failed: {e2}", exc_info=True)

                # Update Check Run with verdict conclusion (best-effort — errors are swallowed)
                check_run_id = (task.context or {}).get("check_run_id")
                if check_run_id:
                    try:
                        _cr_token, _cr_owner, _cr_repo = await get_github_token_for_task(task.id, session)
                        _severity_map = {"critical": "failure", "major": "warning", "minor": "notice"}
                        _conclusion = (
                            "success" if verdict == "APPROVE"
                            else ("neutral" if verdict == "COMMENT" else "action_required")
                        )
                        _annotations = []
                        for _issue in issues:
                            if _issue.get("file") and _issue.get("line"):
                                _lvl = _severity_map.get(_issue.get("severity", "minor"), "notice")
                                _ln = int(_issue["line"])
                                _annotations.append({
                                    "path": _issue["file"],
                                    "start_line": _ln,
                                    "end_line": _ln,
                                    "annotation_level": _lvl,
                                    "message": _issue.get("description", ""),
                                })
                        _issue_counts = {}
                        for _issue in issues:
                            _sev = _issue.get("severity", "minor")
                            _issue_counts[_sev] = _issue_counts.get(_sev, 0) + 1
                        _counts_line = ", ".join(
                            f"{v} {k}" for k, v in sorted(_issue_counts.items())
                        ) if _issue_counts else "no issues"
                        _cr_summary = f"**Blueprint Code Review**: {verdict}\n\n{feedback[:3000]}"
                        if _issue_counts:
                            _cr_summary += f"\n\n**Issue summary**: {_counts_line}"
                        await update_check_run(
                            _cr_token, _cr_owner, _cr_repo,
                            check_run_id,
                            conclusion=_conclusion,
                            title=f"Blueprint Code Review: {verdict}",
                            summary=_cr_summary,
                            annotations=_annotations or None,
                        )
                        logger.info(f"[CHECK RUN] Updated check run {check_run_id} → {_conclusion} for task {task.id}")
                    except Exception as _cr_exc:
                        logger.warning(f"[CHECK RUN] Failed to update check run {check_run_id} for task {task.id}: {_cr_exc}")

                # Store Blueprint's verdict on the review task context so the
                # pull_request_review webhook handler can correlate quality signals.
                task.context = {**(task.context or {}), "blueprint_review_verdict": verdict}
                session.add(task)

                action_taken = False
                head_sha = ""
                if verdict == "APPROVE":
                    head_sha = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number, project_id=str(task.project_id))
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
                        ci_status = await get_check_runs(gh_token, owner, repo, head_sha, project_id=str(task.project_id)) if (head_sha and is_main_merge) else None

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

                        merge_result = await _merge_with_retry(merge_pull_request, gh_token, owner, repo, pr_number, project_id=str(task.project_id))

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
                                        # Post result message for auto-remediation tasks
                                        if (orig.context or {}).get("auto_remediation") is True:
                                            try:
                                                _ar_branch = (orig.context or {}).get("ci_failure_branch", base_branch)
                                                await post_auto_fix_result_message(
                                                    session, orig.project_id,
                                                    branch=_ar_branch,
                                                    result="merged",
                                                    pr_number=pr_number,
                                                    task_id=orig.id,
                                                )
                                                try:
                                                    from app.notification_dispatch import dispatch_external_notification
                                                    asyncio.ensure_future(dispatch_external_notification(
                                                        orig.project_id,
                                                        "AUTO_FIX_MERGED",
                                                        {"branch": _ar_branch, "pr_number": pr_number},
                                                    ))
                                                except Exception as _wh_exc:
                                                    logger.warning("[AUTO-REMEDIATION] Failed to dispatch AUTO_FIX_MERGED webhook: %s", _wh_exc)
                                            except Exception as _ar_exc:
                                                logger.warning("[AUTO-REMEDIATION] Failed to post result message: %s", _ar_exc)
                                        # Extract review feedback as persistent playbook entries
                                        # when a PR merges after a multi-iteration review cycle (3+)
                                        try:
                                            from app.playbook_learning import extract_review_feedback_to_playbook
                                            _orig_task_type = orig.first_payload.get("type", "create")
                                            await extract_review_feedback_to_playbook(
                                                task=task,
                                                review_loop=review_loop,
                                                pr_number=pr_number,
                                                original_task_type=_orig_task_type,
                                                session=session,
                                            )
                                        except Exception as _rp_exc:
                                            logger.warning("[REVIEW_PLAYBOOK] Failed to extract review feedback: %s", _rp_exc)
                                        await session.commit()
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Spawn a smoketest after rollup merges to main
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if is_rollup_merge and base_branch == "main" and _runtime_config.get("smoketest_enabled"):
                                            try:
                                                type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                                type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                                health_check_urls = _runtime_config.get("health_check_urls") or []
                                                _smoketest_payload = validate_internal_payload({
                                                    "type": "smoketest",
                                                    "base_branch": base_branch,
                                                    **({"model": type_model} if type_model else {}),
                                                    **({"effort": type_effort} if type_effort else {}),
                                                    **({"health_check_urls": health_check_urls} if health_check_urls else {}),
                                                }, "task_lifecycle:smoketest")
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[_smoketest_payload],
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
                                                        _rescan_payload = validate_internal_payload({
                                                            "type": "security_audit",
                                                            "prompt": (
                                                                f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                f"Output only PASS or remaining FAIL findings."
                                                            ),
                                                            **({"model": type_model} if type_model else {}),
                                                            **({"effort": type_effort} if type_effort else {}),
                                                        }, "task_lifecycle:security_audit")
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[_rescan_payload],
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
                        # Merge/CI-check failed — park in EVALUATING with ci_waiting so
                        # the orchestrator's CI monitoring loop retries the merge instead
                        # of silently orphaning the PR.
                        logger.error(f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}, parking in CI-wait: {e}", exc_info=True)
                        task.status = TaskStatus.EVALUATING
                        task.latest_status_message = f"Review approved, merge attempt failed ({type(e).__name__}) — CI monitor will retry"
                        task.context = {**(task.context or {}), "review_loop": {
                            **review_loop, "ci_waiting": True, "verdict": "APPROVE",
                            "head_sha": head_sha, "pr_number": pr_number,
                            "branch": branch, "base_branch": base_branch,
                            "original_task_id": original_task_id,
                            "ci_wait_started": _utcnow().isoformat()}}
                        session.add(task)
                        await session.commit()
                        action_taken = True
                elif verdict == "COMMENT":
                    # COMMENT — informational review posted, no fix task spawned, no merge
                    logger.info(f"[REVIEW PIPELINE] COMMENT verdict for PR #{pr_number} — informational review posted, no action taken")
                    task.latest_status_message = f"Informational review posted for PR #{pr_number} — no changes required"
                    session.add(task)
                    await session.commit()
                    action_taken = True
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
                from app.direction_outcomes import _update_direction_research_outcomes  # noqa: PLC0415
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
