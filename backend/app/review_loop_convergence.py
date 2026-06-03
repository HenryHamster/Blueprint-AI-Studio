"""Review<->fix outer-loop convergence machinery.

Extracted from review_pipeline.py to keep that module below its size cap.
Holds the scope-pressure / narrow-focus threshold constants and prompt
prefixes, the pipeline-task staleness check, and the terminal
cap-exhaustion handler.

These names are re-exported from app.review_pipeline so every existing
``from app.review_pipeline import X`` caller and test keeps working
unchanged. This module imports nothing from review_pipeline, so the
re-export is acyclic.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskStatus
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


_REVIEW_LOOP_TERMINAL_OUTCOME_KEY = "review_loop_terminal_outcome"


def _set_review_loop_terminal_outcome(
    task: Task,
    *,
    kind: str,
    reason: str,
    pr_number: int,
    iterations: int,
    scope: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist the chain-original terminal outcome in task.context.

    This is purely a structured breadcrumb for downstream aggregation. It does
    not change the existing alerting, gating, or close-comment behavior.
    """
    from sqlalchemy.orm.attributes import flag_modified

    ctx = dict(task.context or {})
    terminal_outcome: dict[str, object] = {
        "kind": kind,
        "reason": reason,
        "pr_number": pr_number,
        "iterations": iterations,
    }
    if scope is not None:
        terminal_outcome["scope"] = scope
    if detail is not None:
        terminal_outcome["detail"] = detail
    terminal_outcome["recorded_at"] = _utcnow().isoformat()
    ctx[_REVIEW_LOOP_TERMINAL_OUTCOME_KEY] = terminal_outcome
    task.context = ctx
    flag_modified(task, "context")


_CHURN_NARROW_FOCUS_PREFIX = (
    "⚠️ ISSUE CHURN DETECTED: the prior {window} fix iterations resolved some "
    "issues but introduced new ones (CRITICAL+MAJOR counts: {counts}). "
    "Prefer addressing only the listed issues; avoid incidental refactoring "
    "or cleanup. If the same issue keeps recurring, the fix you've been "
    "applying is probably wrong — don't apply it harder, investigate. Two "
    "common patterns: (1) the planned file set was wrong and the fix "
    "genuinely needs files outside scope (a sibling cluster's dep) — expand "
    "scope, explain why in the PR description, stop rewriting the same file "
    "differently each iteration; (2) review asks for a literal revert — run "
    "`git checkout origin/<base_branch> -- <file>` rather than rewriting "
    "from memory, which drifts each iteration and never converges.\n\n"
)

# Softer proactive directive injected when a fix iteration has consumed at
# least half the per-chain iteration cap but churn has not (yet) been
# detected. Unlike the churn prefix, this does NOT reduce the cap — it is
# guidance only, aimed at preventing churn before it begins.
MID_BUDGET_NARROW_FOCUS_THRESHOLD = 0.5
_MID_BUDGET_NARROW_FOCUS_PREFIX = (
    "⚠️ You have used over half of the fix iteration budget "
    "({iteration}/{cap}). Focus on the specific issues listed in the review "
    "feedback. Avoid modifying files not referenced by the issues. Defer "
    "cleanups and refactors to follow-up tasks.\n\n"
)

# Light-touch informational prefix injected from iteration 2 onward whenever
# repeating CRITICAL/MAJOR issues are observed across prior review iterations.
# No cap reduction, no narrow-focus urgency — just surfaces the pattern so fix
# agents have early visibility before churn or mid-budget thresholds trigger.
_EARLY_REPEATING_ISSUES_PREFIX = (
    "Note: some issues have appeared in multiple prior review iterations. "
    "Consider prioritizing a durable fix for these recurring patterns.\n\n"
)

# Utilization at/above this fraction of the scope file budget signals the fix
# agent to prefer minimal fixes over expansive refactoring — gives headroom
# before a fix iteration would tip the PR into scope-overflow territory.
SCOPE_PRESSURE_WARN_THRESHOLD = 0.7

# Graduated iteration cap: once a medium-scope PR crosses
# SCOPE_PRESSURE_WARN_THRESHOLD (70%) of its production-file budget, the
# remaining review-fix iterations are scaled down by how close the PR is to
# full budget. At 70% utilization the fix agent keeps ~30% of its remaining
# budget; at 90% only ~10% (floor of 2); at 95%+ the floor of 2 dominates.
# The no-new-files prompt stays gated on the stricter
# SCOPE_PRESSURE_HARD_CAP_PERCENTAGE (90%) level.
SCOPE_PRESSURE_HARD_CAP_PERCENTAGE = 90
SCOPE_PRESSURE_HARD_CAP_REMAINING = 2
# For large-scope tasks, allow slightly more remaining iterations (floor of 3)
# so we don't slam to 2 remaining on a near-finished large refactor.
SCOPE_PRESSURE_HARD_CAP_REMAINING_LARGE = 3

_SCOPE_PRESSURE_HARD_CAP_PREFIX = (
    "🚫 SCOPE BUDGET EXHAUSTED: this PR has consumed {count}/{budget} "
    "({pct}%) of its medium-scope production file budget. You MUST NOT add "
    "any new production files — only modify the existing changed files. "
    "Defer any new-file work to follow-up tasks.\n\n"
)


# A pipeline task is considered 'stale' (effectively dead, safe to supersede)
# when it has not heartbeated for this long. Matches the orchestrator's own
# reap threshold (see orchestrator.py stale-PROVISIONING recovery) so we don't
# fight with it — whichever sweep reaches the task first cancels it.
PIPELINE_TASK_STALE_MINUTES = 15


def _is_pipeline_task_stale(task: Task) -> bool:
    """True if a review/fix task has gone silent and can be superseded.

    Only applies to tasks that have actually started running. A task still
    waiting in QUEUED (never claimed) is NOT considered stale — long queue
    waits typically mean rate-limit backoff, not a dead task, and the
    orchestrator's reap loop owns that state. Once a task has either
    heartbeated or reached ``started_at``, we measure silence from the
    freshest of those timestamps.
    """
    threshold = _utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES)
    if task.last_heartbeat is not None:
        return task.last_heartbeat < threshold
    if task.started_at is None:
        return False
    return task.started_at < threshold


async def _cap_exhausted_after_approve(
    orig: Task,
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    session: AsyncSession,
) -> bool:
    """FIX D: decide whether the review-fix cap was hit on an ALREADY-APPROVED
    PR (a benign non-convergence on the merge mechanics, e.g. repeated rebase /
    CI-flap churn after sign-off) rather than a genuine unresolved review
    rejection.

    Returns True when the last recorded verdict is APPROVE / clean:
      • the parent (``orig``) or the source task carries
        ``review_loop.review_approved == True`` (or legacy verdict == APPROVE);
      • the most recent chain review task recorded
        ``blueprint_review_verdict == 'APPROVE'``;
      • the latest iteration has ZERO outstanding CHANGES_REQUESTED issues.

    GUARDRAIL: any outstanding CHANGES_REQUESTED on the latest iteration (a
    genuine non-convergence) → returns False so the caller keeps today's
    FAILED + CODE_ERROR path.
    """
    from sqlalchemy import select

    # (1) Outstanding CHANGES_REQUESTED on the latest iteration is a hard veto —
    # check it FIRST so an approval flag from an earlier round can never override
    # a later unresolved rejection.
    for t in (orig, source_task):
        rl = (t.context or {}).get("review_loop") or {}
        if rl.get("verdict") == "REJECT" or rl.get("review_changes_requested"):
            return False
        history = rl.get("review_history") or []
        if history:
            last = history[-1] or {}
            if (last.get("verdict") or "").upper() in (
                "REJECT",
                "CHANGES_REQUESTED",
            ):
                return False
            if last.get("issues"):
                return False

    # (2) Parent / source approval flag.
    for t in (orig, source_task):
        rl = (t.context or {}).get("review_loop") or {}
        if rl.get("review_approved") is True or rl.get("verdict") == "APPROVE":
            return True
        if (t.context or {}).get("blueprint_review_verdict") == "APPROVE":
            return True

    # (3) Most recent chain review task's recorded verdict.
    try:
        review_rows = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == orig.project_id,
                        Task.status.in_(
                            [TaskStatus.COMPLETED, TaskStatus.EVALUATING]
                        ),
                    )
                    .order_by(Task.completed_at.desc().nullslast())
                )
            )
            .scalars()
            .all()
        )
        for rt in review_rows:
            rp = rt.first_payload or {}
            if rp.get("type") != "review":
                continue
            if rp.get("pr_number") != pr_number:
                continue
            r_orig = rp.get("original_task_id") or ""
            if r_orig and r_orig != resolved_original_id:
                continue
            verdict = (rt.context or {}).get("blueprint_review_verdict")
            if verdict == "APPROVE":
                return True
            if verdict in ("REJECT", "CHANGES_REQUESTED"):
                return False
            # First matching chain review with a non-APPROVE/non-REJECT verdict
            # is inconclusive; keep scanning older ones.
    except Exception as _vq_err:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion verdict lookup failed for PR #{pr_number}: "
            f"{_vq_err}"
        )

    return False


async def _pr_is_preservable_on_cap(
    orig: Task,
    pr_number: int,
    session: AsyncSession,
) -> bool:
    """FIX #3b: decide whether a capped rollup PR is worth PRESERVING (left open
    for hand-merge) rather than destroyed (closed + FAILED + CODE_ERROR).

    A PR is preservable only when it carries real, mergeable work:
      • it is a rollup PR (rollup chains aggregate already-completed subtask work
        — discarding the whole PR over review-loop non-convergence throws away
        merged-elsewhere siblings), AND
      • it is NON-EMPTY (``changed_files > 0`` — a branch rebased flat has
        nothing to hand-merge), AND
      • it is NOT a genuine merge conflict (``mergeable_state != 'dirty'`` — a
        conflicted PR cannot be hand-merged without rework, so closing it is
        correct).

    GUARDRAIL: a genuinely broken/empty/conflicted PR returns False so the
    caller keeps today's close+FAIL path. On any GitHub lookup failure we also
    return False — when in doubt, fall back to the safe (destructive) path.
    """
    is_rollup = (orig.context or {}).get("_is_rollup_review") is True or (
        orig.context or {}
    ).get("_rollup_retries") is not None
    if not is_rollup:
        rollup_pr = (orig.context or {}).get("rollup_pr_number")
        if rollup_pr and rollup_pr == pr_number:
            is_rollup = True
    if not is_rollup:
        return False
    try:
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task

        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        pr = await get_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: preservability lookup failed for PR "
            f"#{pr_number}: {e} — falling back to close+FAIL"
        )
        return False
    if (pr.get("state") or "").lower() != "open":
        return False
    if (pr.get("changed_files") or 0) <= 0:
        return False
    # 'dirty' = genuine merge conflict per GitHub. Any other state (clean,
    # unstable, blocked, behind) is hand-mergeable. 'unknown'/None means GitHub
    # hasn't computed it yet — treat conservatively as NOT preservable so we
    # don't leave a possibly-conflicted PR dangling open.
    mergeable_state = (pr.get("mergeable_state") or "").lower()
    return mergeable_state not in ("dirty", "", "unknown")


async def _handle_cap_exhaustion(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    iterations: int,
    session: AsyncSession,
    *,
    scope: str,
) -> None:
    """Terminal handler when the review-fix iteration cap is hit.

    Transitions the create task (``orig``) to FAILED with
    failure_detail=review_cap_exhausted, closes the GitHub PR, and fires an
    approval alert. Idempotent — safe to re-enter on the same orig without
    duplicating side effects.

    Why FAILED instead of COMPLETED+requires_approval:
    The prior behaviour left the create task COMPLETED and relied on
    requires_approval=True as a terminal signal, but rollup_management's
    stale-unmerged respawn loop doesn't check that flag — it kept re-entering
    the cap path every 10 min indefinitely. Marking FAILED routes the task
    through the standard failure → partial-rollup path.

    ``scope`` is either "global" (cross-chain) or "per-chain" — used only for
    human-readable messages.
    """
    from app.failure_classification import CODE_ERROR, persist_failure_category
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    cap_label = "global" if scope == "global" else "per-chain"
    source_task.latest_status_message = f"Review-fix cap reached for PR #{pr_number} ({iterations} iterations, {cap_label})"
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to load orig {resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} not found"
        )
        return

    already_handled = orig.status in (
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and (orig.context or {}).get("failure_detail") in (
        "review_cap_exhausted",
        "cap_exhausted_after_approve",
        "review_cap_preserved_for_handmerge",
    )
    if already_handled:
        logger.info(
            f"[PIPELINE] Cap re-triggered for PR #{pr_number} — orig {resolved_original_id[:8]} "
            f"already terminal with {(orig.context or {}).get('failure_detail')}, "
            f"skipping side effects"
        )
        await session.commit()
        return

    # FIX D: if the cap was exhausted on an already-APPROVED PR (benign merge-
    # mechanics churn after sign-off, not an unresolved review rejection), land
    # CANCELLED with failure_detail='cap_exhausted_after_approve' and do NOT
    # record CODE_ERROR — that would falsely ding the account/direction for a
    # non-failure. GUARDRAIL: _cap_exhausted_after_approve returns False on any
    # outstanding CHANGES_REQUESTED, preserving today's FAILED + CODE_ERROR path
    # for genuine non-convergence.
    if await _cap_exhausted_after_approve(
        orig, source_task, resolved_original_id, pr_number, session
    ):
        from sqlalchemy.orm.attributes import flag_modified

        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} AFTER approval — landing "
            f"orig {resolved_original_id[:8]} CANCELLED (cap_exhausted_after_approve), "
            f"no CODE_ERROR recorded"
        )
        orig.status = TaskStatus.CANCELLED
        orig.latest_status_message = (
            f"Review-fix cap reached ({iterations} iterations, {cap_label}) after "
            f"approval — PR #{pr_number} auto-closed (no rejection)"
        )
        ctx = dict(orig.context or {})
        ctx["failure_detail"] = "cap_exhausted_after_approve"
        ctx["review_cap_iterations"] = iterations
        # NB: deliberately NOT setting pr_rejected — this was an approved PR.
        orig.context = ctx
        flag_modified(orig, "context")
        session.add(orig)
        try:
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): failed to commit orig "
                f"{resolved_original_id[:8]}: {e}",
                exc_info=True,
            )
            return
        # Best-effort: still close the GitHub PR and record the terminal
        # outcome for direction state, but as a SUCCESS-shaped (non-failure)
        # signal — skip record_task_result/record_direction_result failure dings
        # and the failure alert.
        try:
            from app.task_completion import _record_remediation_if_terminal

            await _record_remediation_if_terminal(
                orig, TaskStatus.CANCELLED, session
            )
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): remediation record "
                f"failed for PR #{pr_number}: {e}"
            )
        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            try:
                await create_pr_comment(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    f"Auto-closed: review-fix cap reached ({iterations} iterations, "
                    f"{cap_label}) after approval — no outstanding changes requested. "
                    f"Work was signed off; closing as superseded.",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve): comment failed on PR "
                    f"#{pr_number}: {e}"
                )
            closed = await close_pull_request(
                gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
            )
            if closed:
                logger.info(
                    f"[PIPELINE] Closed PR #{pr_number} after approved-cap exhaustion "
                    f"({iterations} iterations)"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): PR close failed for "
                f"#{pr_number}: {e}"
            )
        return

    # FIX #3b: PRESERVE WORK ON CAP. When the cap is hit on REQUEST_CHANGES for a
    # NON-EMPTY, conflict-free ROLLUP PR, the prior behaviour (close PR + FAIL +
    # CODE_ERROR) destroyed salvageable aggregated work over review-loop
    # non-convergence. Instead, leave the PR OPEN and flag the orig
    # requires_approval so an operator can hand-merge it. The orig still lands
    # terminal (FAILED routes through the partial-rollup path and stops the
    # stale-unmerged respawn loop, per the docstring above) but with a distinct
    # failure_detail, NO CODE_ERROR, NO pr_rejected, and NO PR close.
    # GUARDRAIL: _pr_is_preservable_on_cap returns False for empty / conflicted /
    # non-rollup PRs (and on any GitHub lookup failure) → those keep today's
    # close+FAIL+CODE_ERROR path below.
    if await _pr_is_preservable_on_cap(orig, pr_number, session):
        from sqlalchemy.orm.attributes import flag_modified

        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} on a non-empty, "
            f"conflict-free rollup — PRESERVING work: leaving PR open + flagging "
            f"orig {resolved_original_id[:8]} requires_approval for hand-merge "
            f"(no CODE_ERROR, no PR close)"
        )
        orig.status = TaskStatus.FAILED
        # Always flag requires_approval here regardless of the global gate — the
        # whole point of preservation is to route this to a human; without the
        # flag the open PR would dangle with no owner.
        orig.requires_approval = True
        orig.latest_status_message = (
            f"Review-fix cap reached ({iterations} iterations, {cap_label}) — "
            f"PR #{pr_number} LEFT OPEN with mergeable work; manual hand-merge "
            f"required (not auto-closed)"
        )
        ctx = dict(orig.context or {})
        ctx["failure_detail"] = "review_cap_preserved_for_handmerge"
        ctx["review_cap_iterations"] = iterations
        ctx["pr_preserved_for_handmerge"] = True
        # NB: deliberately NOT setting pr_rejected / CODE_ERROR — the work is
        # salvageable, not a code failure, and the PR is NOT closed.
        orig.context = ctx
        flag_modified(orig, "context")
        session.add(orig)
        try:
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): failed to commit orig "
                f"{resolved_original_id[:8]}: {e}",
                exc_info=True,
            )
            return
        try:
            from app.task_completion import _record_remediation_if_terminal

            await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): remediation record failed "
                f"for PR #{pr_number}: {e}"
            )
        # Leave a PR comment so the operator knows it was left open on purpose.
        # Do NOT close the PR.
        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"This PR has mergeable work and no genuine conflict, so it was "
                f"LEFT OPEN for manual hand-merge rather than auto-closed. "
                f"Operator review required.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): comment failed on PR "
                f"#{pr_number}: {e}"
            )
        # Surface for the operator via the normal failure-alert channel — this
        # IS a manual-intervention event even though the work is salvageable.
        try:
            await _maybe_create_failure_alert(
                orig,
                session,
                f"PR #{pr_number} hit review-fix cap ({iterations} iterations, "
                f"{cap_label}) — left open with mergeable work, hand-merge required",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): alert failed for PR "
                f"#{pr_number}: {e}"
            )
        return

    orig.status = TaskStatus.FAILED
    # The approval gate is discretionary: only raise it when require_approval_gates
    # is enabled. With gates off the operator has opted out of manual gating, so the
    # capped child still FAILs (terminal → routes through the normal partial-rollup
    # path) but we must NOT set requires_approval. Otherwise rollup_management
    # escalates this child's flag onto the parent (rollup_management.py ~1086) and
    # services.py's 2h approval-gate timeout then FAILs the otherwise-healthy parent,
    # discarding its already-merged siblings.
    if _runtime_config.get("require_approval_gates", True):
        orig.requires_approval = True
    orig.latest_status_message = (
        f"Review-fix cycle reached limit ({iterations} iterations, {cap_label}) — "
        f"PR #{pr_number} auto-closed, manual review required"
    )
    persist_failure_category(orig, CODE_ERROR)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "review_cap_exhausted"
    ctx["review_cap_iterations"] = iterations
    ctx["pr_rejected"] = True
    ctx["pr_rejection_note"] = (
        f"Auto-closed by Blueprint: review-fix cap exhausted after {iterations} iterations"
    )
    ctx["review_loop_terminal_outcome"] = {
        "kind": "cap_exhausted",
        "reason": "review_cap_exhausted",
        "pr_number": pr_number,
        "iterations": iterations,
        "scope": scope,
        "detail": cap_label,
        "recorded_at": _utcnow().isoformat(),
    }
    orig.context = ctx
    _set_review_loop_terminal_outcome(
        orig,
        kind="cap_exhausted",
        reason="review_cap_exhausted",
        pr_number=pr_number,
        iterations=iterations,
        scope=scope,
        detail=cap_label,
    )
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to commit orig {resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

    # Surface cap-exhaustion to deploy_monitoring + direction health tracking.
    # Without this, cap failures bypass per-type failure gates and anomaly detection.
    record_task_result(
        success=False,
        failure_class=CODE_ERROR,
        task_id=str(orig.id),
        task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id,
            success=False,
            failure_class=CODE_ERROR,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    # Cap exhaustion bypasses the normal status-update handler; call
    # _record_remediation_if_terminal directly to keep direction state consistent.
    try:
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to record remediation outcome for PR #{pr_number}: {e}"
        )
    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            fingerprint = ((orig.context or {}).get("failure_fingerprints") or [None])[
                0
            ]
            fingerprint_note = (
                f"\n\nFailure fingerprint: `{fingerprint}`" if fingerprint else ""
            )
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Auto-closed: review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"Manual review required.{fingerprint_note}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: failed to comment on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed PR #{pr_number} after cap exhaustion ({iterations} iterations)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )

    try:
        await _maybe_create_failure_alert(
            orig,
            session,
            f"PR #{pr_number} hit review-fix cap ({iterations} iterations, {cap_label}) — "
            f"auto-closed, manual review required",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to create alert for PR #{pr_number}: {e}"
        )
