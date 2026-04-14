"""Review pipeline: spawn and manage automated review/fix task cycles.

Extracted from task_lifecycle.py to reduce its size. Contains the per-PR
review-fix loop, iteration cap, and stuck-cycle detection logic.

All imports are module-level — no circular dependency risk with app.main
or app.task_lifecycle.
"""
import logging
import uuid
from typing import Optional, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)


MAX_REVIEW_FIX_ITERATIONS = 16

STUCK_CYCLE_THRESHOLD_DEFAULT = 0.5
STUCK_CYCLE_THRESHOLD_REFACTOR = 0.75


def _max_review_iterations(task: Task) -> int:
    """Scope-aware review-fix iteration limit."""
    ctx = task.context or {}
    scope = ctx.get('scope', 'medium')
    fix_reason = ctx.get('review_loop', {}).get('fix_reason', 'code_review')

    # CI-only fixes should converge fast (lint/type/test errors)
    if fix_reason == 'ci_failure':
        limit = 8
    # Small scope tasks: less code, fewer things to fix
    elif scope == 'small':
        limit = 10
    # Medium scope: moderate iteration budget
    elif scope == 'medium':
        limit = 12
    # Large scope or unknown: full budget
    else:
        limit = 16
    return min(limit, MAX_REVIEW_FIX_ITERATIONS)


async def _spawn_pipeline_task(
    source_task: Task, task_type: str, session: AsyncSession,
    pr_number: int, branch: str, base_branch: str,
    original_prompt: str = "", fix_prompt: str = "", original_task_id: str = "",
    current_feedback: str = "", fix_reason: str = "", current_issues: list = None
) -> Optional[Task]:
    """Spawn a review or fix task as part of the automated review pipeline.
    Returns None if the review-fix cycle limit has been reached.
    Returns the existing active task if a review/fix is already active for this PR."""
    # Guard: don't spawn if PR is already merged
    orig_id = original_task_id or (source_task.context or {}).get("review_loop", {}).get("original_task_id", "") or (source_task.first_payload).get("original_task_id", "")
    if orig_id:
        try:
            orig_task = await session.get(Task, uuid.UUID(orig_id))
            if orig_task and (orig_task.context or {}).get("pr_merged"):
                logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {orig_id[:8]})")
                source_task.latest_status_message = f"PR #{pr_number} already merged — skipping {task_type}"
                session.add(source_task)
                return None
        except Exception:
            logger.debug(f"[PIPELINE] Could not verify pr_merged for original task {orig_id[:8]}: skipping check")
    # Also check the source task itself
    if (source_task.context or {}).get("pr_merged"):
        logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)")
        return None

    # Guard: don't spawn if there's already an ACTIVE review or fix for this PR.
    # Same-type check prevents duplicates. Cross-type check prevents spawning a
    # review while a fix is running (the fix will spawn its own review when done).
    active_pipeline = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
        )
    )).scalars().all()
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        t_type = t_payload.get("type")
        if t_payload.get("pr_number") == pr_number and t_type in ("review", "fix"):
            logger.warning(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — {t_type} already active: {t.id}")
            source_task.latest_status_message = f"{task_type.title()} skipped for PR #{pr_number} — {t_type} active → {str(t.id)[:8]}"
            session.add(source_task)
            return t

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains
    # Filter to tasks matching this PR number (check payloads JSONB)
    pr_pipeline_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
    )).scalars().all()
    pr_iterations = sum(
        1 for t in pr_pipeline_tasks
        if t.first_payload.get("pr_number") == pr_number
        and t.first_payload.get("type") in ("review", "fix")
    )
    global_cap = _max_review_iterations(source_task)
    # Rollup reviews accumulate iterations across retry attempts — always use the max cap
    # so transient review failures don't permanently block rollup merges.
    # Two detection paths: (1) _rollup_retries set when PR creation fails transiently,
    # (2) 'rollup' in status_message set by rollup_management before calling _spawn_pipeline_task.
    is_rollup = (
        (source_task.context or {}).get("_is_rollup_review") is True
        or (source_task.context or {}).get("_rollup_retries") is not None
        or 'rollup' in (source_task.latest_status_message or '').lower()
    )
    if is_rollup:
        global_cap = MAX_REVIEW_FIX_ITERATIONS
    if pr_iterations >= global_cap:
        logger.warning(f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)")
        source_task.latest_status_message = f"Review-fix cap reached for PR #{pr_number} ({pr_iterations} total iterations)"
        session.add(source_task)
        return None

    # Track review-fix iteration count to prevent infinite loops (per-chain)
    source_loop = (source_task.context or {}).get("review_loop", {})
    source_payload = source_task.first_payload
    iteration = source_loop.get("iteration", 0) + 1
    per_chain_cap = _max_review_iterations(source_task)

    if iteration > per_chain_cap:
        # Cap reached — require manual intervention
        orig_id = original_task_id or source_loop.get("original_task_id", "") or source_payload.get("original_task_id", "")
        if orig_id:
            try:
                orig = await session.get(Task, uuid.UUID(orig_id))
                if orig:
                    orig.requires_approval = True
                    orig.latest_status_message = f"Review-fix cycle reached limit ({per_chain_cap} iterations) — manual review required"
                    session.add(orig)
                    await session.commit()
            except Exception as e:
                logger.warning(f"[PIPELINE] Failed to mark iteration cap on {orig_id}: {e}", exc_info=True)
        logger.warning(f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations")
        return None

    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Determine parent: use the original task's parent (so review/fix are siblings of the
    # create task under the plan/research parent), falling back to original task itself
    resolved_original_id = original_task_id or str(source_task.id)
    parent_id = None
    inherited_priority = source_task.priority
    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
        if orig:
            inherited_priority = orig.priority
    except Exception:
        logger.debug(f"[PIPELINE] Could not inherit properties from original task {resolved_original_id[:8] if resolved_original_id else '?'}")

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {"type": "review", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        "original_task_type": source_task.first_payload.get("type", ""),
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}
    else:
        title = f"Fix PR #{pr_number}"

        # Enrich fix prompt with history of prior review feedback for this PR
        enriched_fix_prompt = fix_prompt
        try:
            prior_reviews = (await session.execute(
                select(Task).where(
                    Task.project_id == source_task.project_id,
                    Task.status == TaskStatus.COMPLETED,
                ).order_by(Task.created_at.asc())
            )).scalars().all()
            prior_feedback_entries = []
            for t in prior_reviews:
                t_payload = t.first_payload
                if t_payload.get("pr_number") == pr_number and t_payload.get("type") == "review":
                    feedback_text = t.latest_status_message or t.description or ""
                    if feedback_text:
                        prior_feedback_entries.append(feedback_text)
            prior_feedback_entries = prior_feedback_entries[-3:]  # last 3 iterations
            if prior_feedback_entries:
                history_section = "\n\n---\n\n".join(prior_feedback_entries)
                enriched_fix_prompt = (
                    f"## Previous review feedback (already addressed):\n{history_section}"
                    f"\n\n## Current review feedback:\n{fix_prompt}"
                )
        except Exception as e:
            logger.warning(f"[PIPELINE] Failed to build review history for PR #{pr_number}: {e}", exc_info=True)

        payload_data = {"type": "fix", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "prompt": enriched_fix_prompt,
                        "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    new_review_entry = {"iteration": iteration - 1, "feedback": current_feedback}
    if current_issues:
        new_review_entry["issues"] = current_issues
    new_review_history = (
        [*prev_review_history, new_review_entry]
        if task_type == "fix" and current_feedback
        else prev_review_history
    )

    # Stuck-cycle detection: if the same CRITICAL/MAJOR issues repeat across consecutive
    # iterations, escalate for manual review instead of consuming the full iteration budget.
    if task_type == "fix" and iteration >= 2 and len(new_review_history) >= 2:
        prev_entry = new_review_history[-2]
        curr_entry = new_review_history[-1]
        prev_issues_list = prev_entry.get("issues") or []
        curr_issues_list = curr_entry.get("issues") or []
        prev_critical_major = {
            i.get("description", "").lower().strip()
            for i in prev_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        }
        curr_critical_major = [
            i.get("description", "").lower().strip()
            for i in curr_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        ]
        if curr_critical_major and prev_critical_major:
            matched_count = sum(
                1 for desc in curr_critical_major
                if any(desc in prev_desc or prev_desc in desc for prev_desc in prev_critical_major)
            )
            total_count = len(curr_critical_major)
            # Refactor tasks get a higher threshold — structural review feedback often repeats
            # because the fix agent cannot fully resolve architectural concerns in scope
            source_type = (source_task.first_payload or {}).get("type", "")
            stuck_threshold = STUCK_CYCLE_THRESHOLD_REFACTOR if source_type == "refactor" else STUCK_CYCLE_THRESHOLD_DEFAULT
            if matched_count / total_count >= stuck_threshold:
                logger.warning(
                    f"[PIPELINE] Stuck cycle detected for PR #{pr_number}: "
                    f"{matched_count}/{total_count} issues repeated"
                )
                # resolved_original_id is always non-empty (falls back to str(source_task.id))
                try:
                    stuck_orig = await session.get(Task, uuid.UUID(resolved_original_id))
                    if stuck_orig:
                        stuck_orig.requires_approval = True
                        stuck_orig.latest_status_message = (
                            f"Review-fix cycle appears stuck — same issues repeated across "
                            f"{iteration} iterations. Manual review required."
                        )
                        session.add(stuck_orig)
                        await session.commit()
                        try:
                            import asyncio as _asyncio  # noqa: PLC0415
                            from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
                            _asyncio.ensure_future(dispatch_external_notification(
                                stuck_orig.project_id,
                                "APPROVAL_NEEDED",
                                {
                                    "task_id": str(stuck_orig.id),
                                    "task_title": stuck_orig.title or str(stuck_orig.id)[:8],
                                    "pr_number": pr_number,
                                    "iteration": iteration,
                                    "reason": "Review-fix cycle stuck — manual review required",
                                },
                            ))
                        except Exception as _ne:
                            logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {_ne}")
                except Exception as e:
                    logger.warning(
                        f"[PIPELINE] Failed to mark stuck cycle on {resolved_original_id}: {e}",
                        exc_info=True,
                    )
                return None

    payload_data = validate_internal_payload(payload_data, f"review_pipeline:{task_type}:pr#{pr_number}")
    source_ctx = source_task.context or {}
    propagated_scope = source_ctx.get('scope', source_loop.get('scope', 'medium'))
    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        priority=inherited_priority,
        payloads=[payload_data],
        context={"scope": propagated_scope,
                 "review_loop": {"pr_number": pr_number, "branch": branch,
                                  "base_branch": base_branch,
                                  "original_task_id": resolved_original_id,
                                  "iteration": iteration,
                                  **({"review_history": new_review_history} if new_review_history else {}),
                                  **({"fix_reason": fix_reason} if fix_reason else {})}},
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(task_id=new_task.id, action="SPAWNED",
                          details={"spawned_by": "REVIEW_PIPELINE", "type": task_type, "pr_number": pr_number})
    session.add(history)
    await session.commit()

    await broadcast_to_project(str(source_task.project_id), "task_updated",
                               {"task_id": str(new_task.id), "status": TaskStatus.QUEUED})
    return new_task
