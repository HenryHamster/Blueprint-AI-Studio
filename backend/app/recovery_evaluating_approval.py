"""Approval-gate hard-timeout recovery handler, extracted from
``app.recovery_evaluating``.

A single self-contained per-row recovery branch lifted out of the
``_check_evaluating_timeouts`` Phase-B loop. It terminalizes EVALUATING parents
flagged ``requires_approval=True`` that otherwise stall indefinitely waiting for
human approval — the ``evaluating_fail_hours`` backstop can be deferred by an
active pipeline chain, so approval-gated parents need their own companion
timeout.

TWO disposition paths, keyed on ``context.approval_hold_reason``:

  * DELIBERATE spend/budget hold (== ``SPEND_BUDGET_APPROVAL_HOLD_REASON``): a
    longer, operator-configurable window resolving to a CLEAN NON-FAILURE
    ``CANCELLED`` terminal on expiry — a cost decision an operator simply has
    not reached is not a pipeline failure. This path NEVER records a failed
    task result, failure category, or failure alert.
  * Everything else (no marker / review-escalation churn): the existing
    2h → ``FAILED`` behavior, with classification, failure alert, and a bounded
    defer while the review/fix chain is still actively working the PR.

``_handle_approval_gate_timeout`` takes the per-row locals it needs (``task``,
``ctx``, ``session``, ``now``) plus the runtime-config-derived spend-window
values and returns ``True`` when it handled the row (the caller ``continue``s)
or ``False`` to fall through to the remaining recovery branches unchanged
(``requires_approval`` is False, or a non-spend hold still inside its 2h
window). The move is purely structural — every commit / notification /
classification semantic is preserved byte-for-byte. ``services.*`` refs and the
function-local ``app.failure_classification`` / ``app.routers.agent_status``
imports are kept unchanged so test patches against ``app.services.<name>``,
``app.routers.agent_status.record_task_result``, and
``app.alert_dispatch._maybe_create_failure_alert`` still take effect.
"""

import logging
from datetime import datetime

from app import services
from app.models import TaskStatus
from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON
from app.recovery_evaluating_helpers import (
    _approval_gate_chain_busy,
    _dispatch_spend_hold_notification,
    _emit_spend_hold_created_signals,
)

logger = logging.getLogger(__name__)

# Approval-gate companion timeout: parents flagged requires_approval can
# otherwise stall indefinitely when _chain_has_active_pipeline_work defers
# the EVALUATING_FAIL backstop. 2h gives operators a reasonable window
# to approve/deny independent of the configured wall-clock.
APPROVAL_GATE_FAIL_SECONDS = 2 * 3600
# Bounded defer window for the approval-gate timeout while the chain is
# STILL actively working the PR (live review/fix children, or a fix parked
# in CI-wait). A stuck/standoff escalation flags the parent but does NOT
# stop the loop — FAILing the parent at the bare 2h mark while its own fix
# chain is mid-flight orphans that work and strands the rollup (research
# 6506582b / PR #9179, 2026-06-09). Mirrors CI_BACKSTOP_DEFER_WINDOW_HOURS;
# beyond 2h + this window the original unconditional timeout applies, so a
# genuinely wedged chain still terminates.
APPROVAL_GATE_DEFER_WINDOW_HOURS = 6


async def _handle_approval_gate_timeout(
    task,
    ctx,
    session,
    now,
    *,
    spend_approval_fail_seconds,
    spend_hours,
    maybe_create_failure_alert,
) -> bool:
    """Approval-gate hard timeout for EVALUATING parents.

    Returns ``True`` when the row was handled (caller ``continue``s) and
    ``False`` to fall through to the remaining recovery branches — namely
    when ``task.requires_approval`` is False, or a non-spend approval hold is
    still within its 2h window.
    """
    if not task.requires_approval:
        return False

    from sqlalchemy.orm.attributes import flag_modified

    is_spend_hold = ctx.get("approval_hold_reason") == SPEND_BUDGET_APPROVAL_HOLD_REASON

    approval_set_at = ctx.get("_approval_set_at")
    if not approval_set_at:
        new_ctx = {**ctx, "_approval_set_at": now.isoformat()}
        # MAKE THE HOLD LOUD: when a spend/budget hold is first
        # seen, fire the created notification + a non-failure
        # WARNING alert, exactly once (guarded by the flag).
        if is_spend_hold and not ctx.get("_spend_hold_created_notified"):
            await _emit_spend_hold_created_signals(
                task, session, window_hours=spend_hours
            )
            new_ctx["_spend_hold_created_notified"] = True
        task.context = new_ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return True
    try:
        approval_since = datetime.fromisoformat(approval_set_at)
        approval_elapsed = (now - approval_since).total_seconds()
    except (ValueError, TypeError):
        approval_elapsed = 0.0

    if is_spend_hold:
        # Fire the EXPIRING warning once, within the lead window
        # (last 20% of the configured window, min 1h) before the
        # hold expires — so the operator gets an active nudge.
        warn_lead = max(spend_approval_fail_seconds * 0.2, 3600.0)
        if (
            not ctx.get("_spend_hold_expiring_notified")
            and spend_approval_fail_seconds - warn_lead
            <= approval_elapsed
            < spend_approval_fail_seconds
        ):
            hours_remaining = round(
                (spend_approval_fail_seconds - approval_elapsed) / 3600,
                1,
            )
            _dispatch_spend_hold_notification(
                task,
                "SPEND_HOLD_EXPIRING",
                spend_hours,
                hours_remaining=hours_remaining,
            )
            task.context = {
                **ctx,
                "_spend_hold_expiring_notified": True,
            }
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return True
        if approval_elapsed > spend_approval_fail_seconds:
            hours = round(approval_elapsed / 3600, 1)
            logger.info(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} "
                f"spend/budget approval not granted in {hours}h — "
                f"declining (CANCELLED, not a failure)"
            )
            task.status = TaskStatus.CANCELLED
            task.latest_status_message = (
                "Spend/budget approval not granted in time — declined (not a failure)"
            )
            # Clear the hold so it leaves the digest needs-
            # attention list; the task is terminal now. This path
            # deliberately does NOT call record_task_result /
            # classify_failure / _maybe_create_failure_alert — it
            # must not be counted as a task failure.
            task.requires_approval = False
            task.context = {**ctx, "spend_approval_declined": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            await services.broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": TaskStatus.CANCELLED,
                },
            )
            return True
        # Within window, not yet at the warning lead — leave
        # EVALUATING for a later sweep. A spend/budget hold never
        # falls through to the review-escalation FAILED path.
        return True

    if approval_elapsed > APPROVAL_GATE_FAIL_SECONDS:
        # Defer (bounded) while the chain is still actively
        # working the PR: live review/fix children, or a fix
        # parked in CI-wait. The two other backstops in this
        # sweep already consult the chain before terminalizing;
        # the approval gate was the one that did not.
        if (
            approval_elapsed
            <= APPROVAL_GATE_FAIL_SECONDS + APPROVAL_GATE_DEFER_WINDOW_HOURS * 3600
        ):
            _approval_chain_busy = False
            try:
                _approval_chain_busy = await services._chain_has_active_pipeline_work(
                    task, session
                ) or await _approval_gate_chain_busy(task, session)
            except Exception:
                logger.debug(
                    f"[EVAL TIMEOUT] Approval-gate chain check "
                    f"failed for {str(task.id)[:8]}",
                    exc_info=True,
                )
            if _approval_chain_busy:
                logger.info(
                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                    f"the approval-gate window but its review/fix "
                    f"chain is still working the PR — deferring "
                    f"(hard ceiling "
                    f"{round(APPROVAL_GATE_FAIL_SECONDS / 3600 + APPROVAL_GATE_DEFER_WINDOW_HOURS, 1)}h)"
                )
                return True
        hours = round(approval_elapsed / 3600, 1)

        # CREDIT-BLINDNESS GUARD: before FAILing, check whether the parent's
        # rollup PR has ALREADY MERGED. The preserve block below only handles
        # an OPEN mergeable PR — an already-merged PR is not "preservable", so
        # without this guard control falls through to FAILED and marks landed
        # work as a failure, costing the direction its merge credit (real
        # incident: research be331963 / PR #14342 merged, then FAILED 36s
        # later). A merged rollup is a SUCCESS: reconcile to COMPLETED via the
        # canonical rollup-merge path (never classify_failure/record_task_result/
        # failure alert).
        _merged_pr_num = ctx.get("rollup_pr_number") or (task.first_payload or {}).get(
            "pr_number"
        )
        if _merged_pr_num:
            _merged_pr_data = None
            try:
                from app.routers.agent import get_github_token_for_task
                from app.github import get_pull_request

                _gh_tok, _gh_owner, _gh_repo = await services._bounded(
                    get_github_token_for_task(task.id, session),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"approval-gate merged-check token {str(task.id)[:8]}",
                )
                _merged_pr_data = await services._bounded(
                    get_pull_request(
                        _gh_tok, _gh_owner, _gh_repo, int(_merged_pr_num)
                    ),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"approval-gate get_pull_request #{_merged_pr_num}",
                )
            except Exception:
                logger.debug(
                    f"[EVAL TIMEOUT] Approval-gate merged-PR check failed for "
                    f"{str(task.id)[:8]} PR #{_merged_pr_num} — falling through "
                    f"to preserve/FAIL",
                    exc_info=True,
                )
            if _merged_pr_data is not None and bool(_merged_pr_data.get("merged")):
                from app.models import Project
                from app.rollup_state import _clear_rollup_transient_state
                from app.recovery_evaluating_helpers import (
                    _apply_rollup_merge_classification,
                )

                _merged_proj = await session.get(Project, task.project_id)
                # Clear the approval hold — the work landed; this is terminal.
                task.requires_approval = False
                _merged_ctx = _clear_rollup_transient_state({**ctx})
                _merged_ctx["rollup_pr_number"] = int(_merged_pr_num)
                task.context = _merged_ctx
                await _apply_rollup_merge_classification(
                    task,
                    _merged_proj,
                    session,
                    pr_number=int(_merged_pr_num),
                    merged_base_branch=(
                        (_merged_pr_data.get("base") or {}).get("ref")
                    ),
                    source="recovery_evaluating.approval_gate_merged",
                    msg_terminal=(
                        f"✓ rollup PR #{_merged_pr_num} merged; reconciled "
                        f"from approval-gate timeout (landed to {{landed}})"
                    ),
                    msg_non_terminal=(
                        f"Approval-gate reconcile: rollup PR #{_merged_pr_num} "
                        f"merged into {{landed}}; awaiting downstream landing"
                    ),
                    hotfix_lane=None,
                )
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                logger.info(
                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} approval-gate "
                    f"timeout after {hours}h — rollup PR #{_merged_pr_num} "
                    f"ALREADY MERGED; reconciled to {task.status} (not FAILED)"
                )
                await services.broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {"task_id": str(task.id), "status": task.status},
                )
                return True

        # If the PR is still clean/mergeable and not yet preserved, leave it
        # open for hand-merge instead of silently abandoning it. Guard with
        # the preserve flag so re-entering sweeps are idempotent (no repeat
        # alerts). Broken/conflicted/non-rollup PRs fall through to FAILED.
        if not ctx.get("approval_timeout_preserved_for_handmerge"):
            _pr_num = ctx.get("rollup_pr_number") or (task.first_payload or {}).get(
                "pr_number"
            )
            if _pr_num:
                from app.review_loop_convergence import (
                    _PreservabilityUnavailable,
                    _pr_is_preservable_on_cap,
                )

                _is_preservable = False
                try:
                    _is_preservable = await _pr_is_preservable_on_cap(
                        task, int(_pr_num), session
                    )
                except _PreservabilityUnavailable as _unavail:
                    # TRANSIENT (deadline breach / GitHub rate limit) — we do NOT
                    # know this PR's state, so we must not abandon it. Without this
                    # clause the generic ``except Exception`` below swallows it,
                    # leaves _is_preservable=False, and the task lands FAILED with a
                    # perfectly mergeable PR abandoned — in BULK during an
                    # install-token-exhaustion window, which is exactly the incident
                    # the raising code exists to prevent.
                    #
                    # PRESERVE instead. Be honest about what that means: the preserve
                    # branch below persists ``approval_timeout_preserved_for_handmerge
                    # =True``, and BOTH the guard above and the early-return below
                    # then skip this task forever — so a transient blip LATCHES
                    # preserve permanently. There is no "a later sweep re-decides once
                    # GitHub is healthy"; this is a one-way door to human hand-merge.
                    #
                    # That is deliberate and it is the right trade here: the outcome
                    # is non-destructive (the PR stays OPEN, nothing is closed, no
                    # CODE_ERROR) and it is OBSERVABLE (the branch fires a hand-merge
                    # alert), so a latched preserve surfaces to an operator instead of
                    # silently destroying a mergeable PR. Latching a PR open for a
                    # human beats closing it because our quota ran out.
                    logger.warning(
                        f"[EVAL TIMEOUT] Preservability UNAVAILABLE for "
                        f"{str(task.id)[:8]} PR #{_pr_num} ({_unavail}) — PRESERVING "
                        f"for hand-merge rather than abandoning a PR we could not "
                        f"read. NOTE: this LATCHES (approval_timeout_preserved_for_"
                        f"handmerge); it will not be re-decided automatically."
                    )
                    _is_preservable = True
                except Exception:
                    logger.debug(
                        f"[EVAL TIMEOUT] Preservability check failed for "
                        f"{str(task.id)[:8]} PR #{_pr_num}",
                        exc_info=True,
                    )
                if _is_preservable:
                    logger.info(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} approval-gate "
                        f"timeout after {hours}h — PR #{_pr_num} is clean/mergeable, "
                        f"PRESERVING for hand-merge instead of abandoning"
                    )
                    task.context = {
                        **ctx,
                        "approval_timeout_preserved_for_handmerge": True,
                        "failure_detail": "approval_timeout_preserved_for_handmerge",
                    }
                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    await maybe_create_failure_alert(
                        task,
                        session,
                        f"Approval-gate timeout after {hours}h — PR #{_pr_num} "
                        f"left open for hand-merge (mergeable work, not abandoned)",
                    )
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    return True

        # Idempotent re-entry: already preserved on a prior sweep.
        if ctx.get("approval_timeout_preserved_for_handmerge"):
            return True

        logger.error(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been "
            f"requires_approval for {hours}h without resolution — "
            f"approval-gate timeout, marking FAILED"
        )
        task.status = TaskStatus.FAILED
        task.latest_status_message = (
            f"Approval-gate timeout after {hours}h — no manual "
            f"approval received, escalating to FAILED"
        )
        from app.failure_classification import (
            classify_failure,
            persist_failure_category,
        )
        from app.routers.agent_status import record_task_result

        _fc = classify_failure(task)
        persist_failure_category(task, _fc)
        record_task_result(
            success=False,
            failure_class=_fc,
            task_id=str(task.id),
            task_type=task.first_payload.get("type", ""),
        )
        session.add(task)
        await maybe_create_failure_alert(
            task,
            session,
            f"Approval-gate timeout after {hours}h — parent failed "
            f"without manual resolution",
        )
        await session.commit()
        # task.status was set to FAILED above; _maybe_create_failure_alert
        # can poison/roll back the session (expiring `task`), so use the
        # literal rather than a post-commit lazy-load (MissingGreenlet).
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        return True

    # Non-spend approval hold still within its 2h window — fall through
    # to the remaining recovery branches unchanged.
    return False
