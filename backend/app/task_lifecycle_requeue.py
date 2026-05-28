"""Parent-task requeue logic, extracted from task_lifecycle.py.

Owns _record_parent_failure and _requeue_parent_task.
task_lifecycle.py re-exports both so all existing callers are unaffected.
"""

import copy
import logging
import uuid
from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, TaskHistory, TaskStatus
from app.shared_state import DEFAULT_TASK_SCOPE
import app.shared_state as _shared_state
from app.routers.agent import _has_local_cycle
from app.payload_schemas import validate_internal_payload
from app.failure_stage import _derive_failure_stage
from app.retry_budget import (
    append_failure_class_history,
    classify_override_reason,
    compute_effective_max_requeues,
)
from app.retry_prompt import _build_retry_prompt_suffix, merge_retry_prompt_suffix

logger = logging.getLogger(__name__)


def _record_parent_failure(parent: Task, failure_class: str) -> None:
    """Record a parent-task FAILED transition in deploy_monitoring and direction health."""
    from app.routers.agent_status import record_task_result

    record_task_result(
        success=False,
        failure_class=failure_class,
        task_id=str(parent.id),
        task_type=(parent.first_payload or {}).get("type", ""),
    )
    direction_id = str((parent.context or {}).get("direction_id") or "")
    if direction_id:
        _shared_state.record_direction_result(
            direction_id,
            success=False,
            failure_class=failure_class,
            project_id=str(parent.project_id) if parent.project_id else "",
        )


async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Remaps depends_on from old (cancelled) IDs to new (cloned) IDs.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    from app.main import _maybe_create_failure_alert
    # Deferred imports so test patches on app.task_lifecycle.* apply
    from app.task_lifecycle import broadcast_to_project, classify_failure

    # Don't resurrect a cancelled parent
    if parent.status == TaskStatus.CANCELLED:
        logger.info(f"[REQUEUE] Skipping requeue — parent {parent.id} is CANCELLED")
        return 0

    # Cap re-evaluations to prevent infinite loops when children consistently fail
    MAX_PARENT_REQUEUES = 3
    parent_ctx = parent.context or {}
    requeue_count = parent_ctx.get("_requeue_count", 0)

    effective_max = await compute_effective_max_requeues(
        parent, session, MAX_PARENT_REQUEUES
    )

    if requeue_count >= effective_max:
        logger.warning(
            f"[REQUEUE] Parent {parent.id} hit re-evaluation limit "
            f"({requeue_count}/{effective_max} attempts) — failing permanently"
        )
        parent.status = TaskStatus.FAILED
        failed_children = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == parent.id,
                        Task.status == TaskStatus.FAILED,
                    )
                )
            )
            .scalars()
            .all()
        )

        # Surface divergence cause in the status message when available so
        # operators on the kanban immediately see why the task was abandoned.
        _divergence_msg: str | None = None
        for _fc in failed_children:
            _abandoned = (_fc.context or {}).get("fix_loop_abandoned")
            if _abandoned:
                _divergence_msg = (
                    f"Abandoned early — fix loop stopped converging "
                    f"({_abandoned.get('exit_reason')} in {_abandoned.get('loop')} loop)"
                )
                break

        parent.latest_status_message = (
            _divergence_msg
            if _divergence_msg
            else f"Re-evaluation limit reached ({requeue_count} attempts) — children consistently failing"
        )
        from app.failure_classification import (
            worst_child_failure_category,
            persist_failure_category,
        )

        worst_cat = worst_child_failure_category(failed_children)
        persist_failure_category(parent, worst_cat)
        _record_parent_failure(parent, worst_cat)
        session.add(parent)
        await session.commit()
        # Create failure alert
        await _maybe_create_failure_alert(parent, session, parent.latest_status_message)
        return 0

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == parent.id)))
        .scalars()
        .all()
    )

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
    parent_staging = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")

    # Pass 1: Clone failed children, build old→new ID mapping
    old_to_new: Dict[str, str] = {}
    cloned_pairs: list = []  # (original, clone)
    retried_count = 0
    blocked_count = 0  # FAILED children skipped because of BLOCKED_PRECONDITION

    for child in children:
        if child.status != TaskStatus.FAILED:
            continue

        # Skip children that hit the review-fix cap: their PR is auto-closed and
        # marked terminal by _handle_cap_exhaustion. Cloning would spawn a fresh
        # create task + fresh PR, which typically re-hits the same merge conflict
        # that triggered the cap. Operators can manually requeue if desired.
        if (child.context or {}).get("failure_detail") == "review_cap_exhausted":
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — review_cap_exhausted, "
                f"PR already closed. Manual requeue required."
            )
            continue

        # Skip children whose fix loop concluded non-convergence — cloning would
        # re-discover the same divergence without making progress.
        if (child.context or {}).get("failure_detail") == "fix_loop_diverged":
            _abandoned = (child.context or {}).get("fix_loop_abandoned") or {}
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — fix_loop_diverged "
                f"({_abandoned.get('exit_reason')} in {_abandoned.get('loop')} loop). "
                f"Manual requeue required."
            )
            continue

        # Dedup: skip cloning if auto-remediation (debug → Fix: path) already spawned
        # a replacement for this child and that replacement is still active.  Cloning
        # here would produce two parallel tasks fixing the same thing (the race observed
        # in task b15b6813: _requeue_parent_task clone + debug-auto-fix both fired,
        # opened duplicate PRs, and the losing orphan burned its retry budget on an
        # empty-diff conflict).  We check the marker written by agent_status.py when it
        # creates the Fix: task; if the replacement failed/cancelled we fall through and
        # let requeue handle it normally.
        _debug_fix_id = (child.context or {}).get("_debug_fix_spawned_task_id")
        if _debug_fix_id:
            try:
                _debug_fix_uuid = uuid.UUID(_debug_fix_id)
                _debug_fix_task = (
                    await session.execute(
                        select(Task).where(
                            Task.id == _debug_fix_uuid,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if _debug_fix_task is not None:
                    logger.info(
                        "[REQUEUE] Skipping clone of child %s — debug-fix task %s already active",
                        str(child.id)[:8],
                        _debug_fix_id[:8],
                    )
                    continue
            except Exception:
                logger.exception(
                    "[REQUEUE] Failed to check debug-fix task for child %s — proceeding with clone",
                    child.id,
                )

        # Capture before cancellation — cancellation overwrites both
        child_extra_logs = (child.context or {}).get("last_error", "")
        failure_class = classify_failure(
            child,
            logs=child_extra_logs or None,
            at_time=child.vm_stopped_at or child.last_heartbeat,
        )
        original_status_message = child.latest_status_message

        # BLOCKED_PRECONDITION: agent surfaced a "BLOCKER:" message and paused
        # because a precondition (referenced file/branch/PR) wasn't ready.
        # Cloning would hit the same wall on the next attempt — the precondition
        # has to land first. Skip the clone-and-retry path so the parent doesn't
        # burn its requeue budget. Operator manually requeues once the upstream
        # resolves (e.g. blocking rollup PR merges to main).
        if failure_class == "BLOCKED_PRECONDITION":
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — BLOCKED_PRECONDITION "
                f"({(original_status_message or '')[:120]!r}). Manual requeue once "
                f"the upstream precondition resolves."
            )
            blocked_count += 1
            continue

        # Skip retry if the current failure fingerprint matches the prior retry's
        # fingerprint — same code produced the same error, so retry is unlikely
        # to help. The recurring-failure-class check fires only after 3+ hits
        # project-wide, which is too late for individual task abort.
        try:
            from app.anomaly_detection import _error_fingerprint as _fp_fn

            # Audit W1: previous_error_fingerprint is now a column.
            prior_fp = child.previous_error_fingerprint
            current_fp = _fp_fn(original_status_message or "")
            if prior_fp and current_fp and prior_fp == current_fp:
                logger.info(
                    f"[REQUEUE] Skipping child {str(child.id)[:8]} — "
                    f"identical error fingerprint across retries"
                )
                continue
        except Exception:
            logger.exception(
                "[REQUEUE] Fingerprint comparison failed for task %s", child.id
            )

        # Auto-generate playbook entry if this failure pattern is recurring
        try:
            from app.playbook_learning import _maybe_auto_playbook

            await _maybe_auto_playbook(
                child, failure_class, original_status_message, session
            )
        except Exception:
            logger.exception(
                "[AUTO_PLAYBOOK] Failed to process playbook for task %s", child.id
            )

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
        task_scope = child_ctx.get("scope", DEFAULT_TASK_SCOPE)
        attempt = child_ctx.get("_attempt_number", 1)
        _ctx_keys_to_strip = {
            "max_plan_account_id",
            "llm_account_id",
            "llm_provider",
            "llm_auth_mode",
            "llm_model",
            "llm_execution_mode",
            "provision_failures",
            "provision_failures_infra",
            "_infra_wait",
            "blacklisted_accounts",
            "provision_failed_accounts",
            "_slot_released",
        }
        clone_ctx = {k: v for k, v in child_ctx.items() if k not in _ctx_keys_to_strip}

        # Preserve the prior attempt's provider/model under a non-stripped key
        # so retry guidance can branch on what actually failed (e.g. emit
        # apply_patch wording for Codex, Edit/Write wording for Claude). The
        # canonical llm_provider/llm_model fields are stripped above so the
        # retry can re-select; _previous_provider survives for diagnostic use.
        prev_provider = child_ctx.get("llm_provider")
        prev_model = child_ctx.get("llm_model")
        if prev_provider:
            clone_ctx["_previous_provider"] = prev_provider
        if prev_model:
            clone_ctx["_previous_model"] = prev_model

        # RATE_LIMIT: propagate the quarantine so the clone can't re-select the
        # same account before its cooldown elapses. agent_status.py already put
        # the failed account in child_ctx["blacklisted_accounts"] — re-applying
        # it here closes the gap where _ctx_keys_to_strip would otherwise clear
        # the list and the clone starts with a blank slate.
        if failure_class == "RATE_LIMIT":
            rl_acct_id = child_ctx.get("llm_account_id") or child_ctx.get(
                "max_plan_account_id"
            )
            if rl_acct_id:
                prior_bl = list(child_ctx.get("blacklisted_accounts") or [])
                if rl_acct_id not in prior_bl:
                    prior_bl.append(rl_acct_id)
                clone_ctx["blacklisted_accounts"] = prior_bl
                logger.info(
                    "[REQUEUE] RATE_LIMIT on account %s — propagating blacklist to clone "
                    "(total blocked: %d)",
                    rl_acct_id[:8],
                    len(prior_bl),
                )

        clone_ctx["_attempt_number"] = attempt + 1
        clone_ctx["_max_attempts"] = MAX_PARENT_REQUEUES + 1

        # Accumulate VM cost of prior failed attempts so the digest can compute
        # total compute burned on retries that eventually succeeded.
        try:
            from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

            if child.vm_provisioned_at and child.vm_stopped_at:
                _rate = SPOT_RATE if child.vm_spot else ON_DEMAND_RATE
                _attempt_cost = (
                    _rate
                    * (child.vm_stopped_at - child.vm_provisioned_at).total_seconds()
                )
                _prior_cost = float(clone_ctx.get("_prior_attempts_cost") or 0.0)
                clone_ctx["_prior_attempts_cost"] = round(
                    _prior_cost + max(_attempt_cost, 0.0), 6
                )
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to accumulate prior attempt cost for task %s",
                child.id,
            )
        clone_ctx["_previous_error"] = (original_status_message or "")[:5000]
        clone_ctx["_previous_failure_class"] = failure_class
        clone_ctx["_failure_class_history"] = append_failure_class_history(
            clone_ctx.get("_failure_class_history"),
            failure_class,
        )

        # Enriched failure diagnosis: fingerprint, stage, error summary, and recurring flag
        try:
            from app.anomaly_detection import (
                _error_fingerprint,
                _is_recurring_failure_class,
            )

            error_msg = original_status_message or ""
            clone_ctx["_previous_error_fingerprint"] = _error_fingerprint(error_msg)
            failure_stage = _derive_failure_stage(failure_class, error_msg)
            if failure_stage:
                clone_ctx["_previous_failure_stage"] = failure_stage
            if failure_class == "CODE_ERROR":
                clone_ctx["_previous_error_summary"] = error_msg[:500]
            proj_anomalies = _shared_state.persistent_anomalies.get(
                str(child.project_id), {}
            )
            if _is_recurring_failure_class(proj_anomalies, failure_class):
                clone_ctx["_previous_is_recurring"] = True
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to enrich retry context for task %s", child.id
            )

        # Propagate PR artifacts from the failed attempt to the retry context
        prev_pr_url = child_ctx.get("_pr_url", "")
        prev_changed_files = child_ctx.get("_pr_changed_files", [])
        prev_file_change_details = child_ctx.get("_file_change_details", [])
        if prev_pr_url:
            clone_ctx["_previous_pr_url"] = prev_pr_url
        if prev_changed_files:
            clone_ctx["_previous_changed_files"] = prev_changed_files
        if prev_file_change_details:
            clone_ctx["_previous_file_change_details"] = prev_file_change_details

        # Propagate enriched step-level progress for multi-file retry guidance
        prev_diff_summary = child_ctx.get("_pr_diff_summary", [])
        prev_test_failure_summary = child_ctx.get("_test_failure_summary", "")
        prev_self_review_outcome = child_ctx.get("_self_review_outcome")
        prev_analysis_plan = child_ctx.get("_analysis_plan", "")
        if prev_diff_summary:
            clone_ctx["_previous_diff_summary"] = prev_diff_summary
        if prev_test_failure_summary:
            clone_ctx["_previous_test_failure_summary"] = prev_test_failure_summary
        if prev_self_review_outcome:
            clone_ctx["_previous_self_review_outcome"] = prev_self_review_outcome
        if prev_analysis_plan:
            clone_ctx["_previous_analysis_plan"] = prev_analysis_plan

        # Extract last review iteration's unresolved CRITICAL/MAJOR issues so the
        # retry can address structured reviewer feedback that truncated status
        # messages often miss.
        try:
            review_history = (child_ctx.get("review_loop") or {}).get(
                "review_history"
            ) or []
            if review_history:
                last_issues = (review_history[-1] or {}).get("issues") or []
                filtered = [
                    str(iss.get("description") or "").strip()
                    for iss in last_issues
                    if isinstance(iss, dict)
                    and str(iss.get("severity") or "").upper() in ("CRITICAL", "MAJOR")
                    and (iss.get("description") or "")
                ]
                if filtered:
                    clone_ctx["_previous_review_issues"] = filtered[:5]
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to extract review issues for task %s", child.id
            )

        # Carry forward the review-iteration count consumed by the prior attempt.
        # _max_review_iterations subtracts half of this from the new cap (floor 3)
        # so retries don't each receive a fresh full iteration budget.
        try:
            prior_iter = int((child_ctx.get("review_loop") or {}).get("iteration") or 0)
            if prior_iter > 0:
                clone_ctx["_prior_review_iterations_consumed"] = prior_iter
        except (TypeError, ValueError):
            pass

        if new_payloads and isinstance(new_payloads[0], dict):
            existing_suffix = new_payloads[0].get("prompt_suffix", "") or ""
            extra_guidance, scope_override = _build_retry_prompt_suffix(
                failure_class,
                original_status_message,
                attempt,
                task_scope,
                clone_ctx,
            )
            if scope_override:
                _override_reason = classify_override_reason(failure_class, clone_ctx)
                clone_ctx["scope"] = scope_override
                clone_ctx["_scope_reduced"] = True
                clone_ctx["scope_calibrated_from"] = f"{task_scope}:{_override_reason}"
            if extra_guidance:
                new_payloads[0]["prompt_suffix"] = merge_retry_prompt_suffix(
                    extra_guidance, existing_suffix, clone_ctx
                )

        new_payloads = [
            validate_internal_payload(p, "task_lifecycle:requeue_child")
            for p in new_payloads
        ]
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
        # Record the clone's ID in the cancelled child's context so the
        # orchestrator can resolve requeue-cancelled dependencies without
        # cascade-cancelling external tasks that depend on this child.
        child.context = {
            **(child.context or {}),
            "_replaced_by_task_id": str(new_child.id),
        }
        flag_modified(child, "context")
        retried_count += 1

        history = TaskHistory(
            task_id=new_child.id,
            action="SPAWNED",
            details={"spawned_by": "REQUEUE", "retrying": str(child.id)},
        )
        session.add(history)

    # Pass 2: Remap depends_on from old (cancelled) IDs to new (cloned) IDs
    for original, clone in cloned_pairs:
        remapped = []
        for dep_id in original.depends_on or []:
            if dep_id in old_to_new:
                remapped.append(old_to_new[dep_id])  # Point to the clone
            else:
                remapped.append(dep_id)  # Keep (COMPLETED sibling or still-active task)
        clone.depends_on = remapped

    # Pass 3: In-memory cycle check on cloned subgraph
    if cloned_pairs:
        graph = {str(c.id): c.depends_on or [] for _, c in cloned_pairs}
        if _has_local_cycle(graph):
            logger.warning(
                f"[REQUEUE] Circular deps detected in cloned children of {parent.id} — clearing all depends_on"
            )
            for _, clone in cloned_pairs:
                clone.depends_on = []

    # No failed children to clone — this is a no-op.  Don't burn the requeue
    # budget on callers that invoke us defensively (admin manual retry of a
    # COMPLETED parent, reaper self-timeout rescue, Step 4 all-children-terminal
    # sweep with no FAILED children).  Decide terminal state based on what
    # children actually exist so the parent doesn't sit in EVALUATING forever.
    if retried_count == 0:
        has_completed = any(c.status == TaskStatus.COMPLETED for c in children)
        has_active = any(
            c.status
            in (
                TaskStatus.QUEUED,
                TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS,
                TaskStatus.EVALUATING,
            )
            for c in children
        )
        if not has_completed and not has_active:
            # All children terminal and none succeeded — they're all CANCELLED
            # or skipped-FAILED (review_cap_exhausted, fix_loop_diverged,
            # BLOCKED_PRECONDITION, identical-fingerprint). Mark parent FAILED
            # with the right class so the reaper stops looping. Distinguish
            # the BLOCKED_PRECONDITION case so operators see "held on upstream"
            # rather than the generic "nothing to roll up" message.
            from app.failure_classification import (
                AGENT_TIMEOUT,
                BLOCKED_PRECONDITION,
                persist_failure_category,
            )

            parent.status = TaskStatus.FAILED
            if blocked_count > 0 and blocked_count == sum(
                1 for c in children if c.status == TaskStatus.FAILED
            ):
                parent_class = BLOCKED_PRECONDITION
                parent.latest_status_message = (
                    f"All {blocked_count} subtask(s) blocked on upstream precondition — "
                    f"manual requeue once it resolves"
                )
            else:
                parent_class = AGENT_TIMEOUT
                parent.latest_status_message = (
                    "No failed subtasks to retry and no successful subtasks to roll up"
                )
            persist_failure_category(parent, parent_class)
            _record_parent_failure(parent, parent_class)
            session.add(parent)
            await session.commit()
            await _maybe_create_failure_alert(
                parent, session, parent.latest_status_message
            )
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.warning(
                f"[REQUEUE] Parent {parent.id} has no failed children to retry and "
                f"no successful ones to roll up — marking FAILED ({parent_class})"
            )
            return 0
        # Has a COMPLETED child (reaper Step 3 + rollup will re-evaluate) or
        # active children (wait for them).  Nudge to EVALUATING so the rollup
        # sweep picks it up, but don't increment _requeue_count/attempt_count —
        # we didn't actually retry anything.
        parent.status = TaskStatus.EVALUATING
        parent.latest_status_message = (
            "No failed subtasks to retry — awaiting rollup re-evaluation"
        )
        session.add(parent)
        await session.commit()
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.EVALUATING},
        )
        logger.info(
            f"[REQUEUE] Parent {parent.id} — no failed children to retry "
            f"(has_completed={has_completed}, has_active={has_active}); "
            f"kept at EVALUATING without burning requeue budget"
        )
        return 0

    parent.context = {**(parent.context or {}), "_requeue_count": requeue_count + 1}
    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = (
        f"Requeued — retrying {retried_count} failed subtask(s)"
    )
    parent.attempt_count += 1
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(
        str(parent.project_id),
        "task_updated",
        {"task_id": str(parent.id), "status": TaskStatus.EVALUATING},
    )
    logger.info(
        f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried"
    )
    return retried_count
