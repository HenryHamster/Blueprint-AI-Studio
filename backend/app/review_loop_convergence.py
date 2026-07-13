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


async def _create_handmerge_attention_message(
    task: Task,
    pr_number: int,
    iterations: int,
    cap_label: str,
    session: AsyncSession,
) -> None:
    """Create a durable operator-attention ProjectMessage for a preserved hand-merge PR.

    Idempotent: a message keyed to the task is only inserted if one does not
    already exist (safe on retry).  The message is committed independently so
    it persists even if the caller's surrounding try/except swallows errors.
    The sweep ``sweep_closed_handmerge_prs`` resolves this message automatically
    when it detects the PR has been merged or closed.
    """
    from app.models import MessageType, ProjectMessage
    from sqlmodel import select as _sqlm_select

    ikey = f"handmerge:{task.id}"
    existing = (
        await session.execute(
            _sqlm_select(ProjectMessage).where(
                ProjectMessage.project_id == task.project_id,
                ProjectMessage.idempotency_key == ikey,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        ProjectMessage(
            project_id=task.project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            from_task_id=task.id,
            content=(
                f"PR #{pr_number} preserved for hand-merge — review-fix cap reached "
                f"({iterations} iterations, {cap_label}); PR left open with mergeable "
                f"work awaiting manual merge"
            ),
            context={
                "event_type": "handmerge_pr_preserved",
                "pr_number": pr_number,
                "task_id": str(task.id),
            },
            idempotency_key=ikey,
        )
    )
    await session.commit()


# Escalation kinds that flag the chain original ``requires_approval`` while the
# review-fix loop KEEPS RUNNING (stuck cycle / standoff / repeat-fingerprint
# halt). These holds exist to point an operator at a loop that looks like it is
# not converging — but the loop is not stopped by them, so when it demonstrably
# converges anyway (a later review APPROVEs, or the PR merges) the hold is
# stale and must be released. Left in place, the recovery sweep's 2h
# approval-gate timeout FAILs a healthy parent out from under its own in-flight
# fix chain (research 6506582b / PR #9179, 2026-06-09). ``cap_exhausted`` is
# deliberately NOT in this set: that path closes the PR and terminalizes the
# chain, so nothing can legitimately converge afterwards.
_RESOLVABLE_ESCALATION_GATE_KINDS = frozenset(
    {"stuck", "standoff", "repeat_fingerprint_halt", "supersession_churn_halt"}
)


def clear_review_escalation_approval_gate(orig: Task, *, resolved_by: str) -> bool:
    """Release a stale stuck/standoff/repeat-fingerprint/supersession hold.

    Pure mutation (mirrors :func:`_set_review_loop_terminal_outcome`): the
    caller owns the row lock and the commit. Only touches holds whose recorded
    terminal-outcome breadcrumb is one of the resolvable escalation kinds —
    spend/budget holds, branch-safety holds, and any hold without a matching
    breadcrumb are left untouched. Returns ``True`` iff the gate was released.
    """
    from sqlalchemy.orm.attributes import flag_modified

    if not orig.requires_approval:
        return False
    ctx = dict(orig.context or {})
    from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON

    if ctx.get("approval_hold_reason") == SPEND_BUDGET_APPROVAL_HOLD_REASON:
        return False
    outcome = ctx.get(_REVIEW_LOOP_TERMINAL_OUTCOME_KEY) or {}
    if outcome.get("kind") not in _RESOLVABLE_ESCALATION_GATE_KINDS:
        return False
    orig.requires_approval = False
    # Drop the timer baseline too — a later, unrelated hold must start fresh,
    # not inherit this hold's elapsed time.
    ctx.pop("_approval_set_at", None)
    ctx[_REVIEW_LOOP_TERMINAL_OUTCOME_KEY] = {
        **outcome,
        "resolved": True,
        "resolved_by": resolved_by,
        "resolved_at": _utcnow().isoformat(),
    }
    orig.context = ctx
    flag_modified(orig, "context")
    logger.info(
        "[PIPELINE] Released %s approval hold on %s (%s)",
        outcome.get("kind"),
        str(orig.id)[:8],
        resolved_by,
    )
    return True


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


def _task_is_actively_ci_waiting(task: Task) -> bool:
    """True if ``task`` is parked waiting on CI (``review_loop.ci_waiting``).

    A CI-waiting task's silence is EXPECTED, not the signature of a dying VM:
    the agent VM has already finished and pushed, and from here the ORCHESTRATOR
    — not the VM — polls GitHub CI, so ``last_heartbeat`` deliberately goes
    stale. A multi-shard CI run routinely outlasts ``PIPELINE_TASK_STALE_MINUTES``
    (15), so :func:`_is_pipeline_task_stale` reports such a task as stale even
    though it is making authoritative progress. Callers must therefore exempt a
    CI-waiting task from stale-supersession / stale-skip exactly as they exempt a
    BLOCKED precondition park — a genuinely-stuck CI-waiting task is bounded
    instead by ci_completion's in-progress reset cap and the EVALUATING CI
    backstop (which FAIL it, dropping it from the active pipeline). Without this
    exemption, three false stale-supersessions of one CI-waiting fix trip the
    supersession-churn halt and terminalize an otherwise-healthy chain
    (research b2231dd0 / rollup PR #10389, 2026-06-16).
    """
    return bool(((task.context or {}).get("review_loop") or {}).get("ci_waiting"))


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
                        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.EVALUATING]),
                    )
                    .order_by(Task.completed_at.desc().nullslast())
                    .limit(200)
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


def _is_backmerge_resolver(orig: Task) -> bool:
    """True if ``orig`` is a back-merge conflict-resolver task.

    Back-merge resolvers carry ``context['backmerge_conflict'] == True`` and
    open a PR (base=automain) that re-applies main→automain sync work after a
    conflict. They are ``create`` tasks, NOT rollups, but — like a rollup — the
    PR aggregates already-completed work (the sync) that is expensive to redo;
    auto-closing it on review-loop non-convergence destroys that work and stalls
    main→automain sync. So a mergeable, conflict-free back-merge resolver PR is
    preservable on cap exhaustion exactly like a rollup.
    """
    return (orig.context or {}).get("backmerge_conflict") is True


async def _pr_is_preservable_on_cap(
    orig: Task,
    pr_number: int,
    session: AsyncSession,
    *,
    after_approve: bool = False,
) -> bool:
    """FIX #3b: decide whether a capped PR is worth PRESERVING (left open for
    hand-merge) rather than destroyed (closed + FAILED + CODE_ERROR).

    A PR is preservable when it carries real, mergeable work that is expensive
    to redo and safe to hand-merge:
      • it is EITHER a rollup PR (rollup chains aggregate already-completed
        subtask work — discarding the whole PR over review-loop non-convergence
        throws away merged-elsewhere siblings) OR a back-merge conflict-resolver
        PR (``context['backmerge_conflict'] is True`` — re-applied main→automain
        sync work; auto-closing it stalls promotion sync), AND
      • it is NON-EMPTY (``changed_files > 0`` — a branch rebased flat has
        nothing to hand-merge), AND
      • it is not a genuine merge conflict (``mergeable_state != 'dirty'``).

    GUARDRAIL — rollups: a broken/empty rollup PR returns False so the caller
    keeps today's close+FAIL path; ``''``/``'unknown'`` (not-yet-computed) and
    any GitHub lookup failure all return False — when in doubt, fall back to the
    safe destructive path.  ``'dirty'`` (conflict) is NOT preservable when
    called from the non-approved path (default ``after_approve=False``) because a
    non-approved conflicted PR is a genuine non-convergence.  When
    ``after_approve=True`` (the PR was signed off before the cap was hit),
    ``'dirty'`` IS preservable — the conflict is a mechanical blocker, not a
    rejection, and a human can resolve it for hand-merge.  Every other state
    (``clean`` / ``unstable`` / ``blocked`` / ``behind``) is treated as
    hand-mergeable regardless of ``after_approve``.

    Back-merge resolvers are stricter on CI but more conservative on ambiguity:
      • CI-RED: ``mergeable_state == 'unstable'`` (mergeable but a status check
        is failing) is NOT preservable — the task requires the resolver PR be
        CI-green, and a red back-merge shouldn't be hand-merged blind.
      • AMBIGUOUS: closing a back-merge resolver is the worse outcome (it can
        always be hand-merged and otherwise stalls sync), so when its live PR
        state is UNKNOWN or the GitHub lookup FAILS we PRESERVE.
      • The only hard non-preserve for a back-merge resolver is a confirmed
        empty PR or a confirmed ``dirty`` / ``unstable`` state.
    """
    is_rollup = (orig.context or {}).get("_is_rollup_review") is True or (
        orig.context or {}
    ).get("_rollup_retries") is not None
    if not is_rollup:
        rollup_pr = (orig.context or {}).get("rollup_pr_number")
        if rollup_pr and rollup_pr == pr_number:
            is_rollup = True
    is_backmerge = _is_backmerge_resolver(orig)
    if not is_rollup and not is_backmerge:
        return False
    try:
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task

        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        pr = await get_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
    except Exception as e:  # noqa: BLE001
        # On lookup failure: rollups fall back to the safe destructive path;
        # back-merge resolvers prefer preserve-over-destroy (closing a resolver
        # is the worse outcome — it can always be hand-merged).
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: preservability lookup failed for PR "
            f"#{pr_number}: {e} — "
            f"{'PRESERVING (back-merge resolver)' if is_backmerge else 'falling back to close+FAIL'}"
        )
        return is_backmerge
    if (pr.get("state") or "").lower() != "open":
        return False
    if (pr.get("changed_files") or 0) <= 0:
        # A confirmed-empty PR has nothing to hand-merge — never preservable,
        # not even for a back-merge resolver.
        return False
    mergeable_state = (pr.get("mergeable_state") or "").lower()
    if is_backmerge:
        # Back-merge resolver: require CI-green ('unstable' = a status check is
        # failing → NOT preservable) and conflict-free ('dirty'). Any other
        # state — INCLUDING the unknown/not-yet-computed case — is preservable;
        # prefer preserve-over-destroy for sync-critical resolver work.
        return mergeable_state not in ("dirty", "unstable")
    # Rollup: 'unknown'/'' means GitHub hasn't computed the state yet — treat
    # conservatively as NOT preservable regardless of after_approve.  'dirty'
    # (genuine merge conflict): non-approved PRs close as before; approved PRs
    # preserve — the conflict is mechanical and a human can resolve it for
    # hand-merge.  All other states (clean, unstable, blocked, behind) are
    # hand-mergeable in both modes.
    if mergeable_state in ("", "unknown"):
        return False
    if mergeable_state == "dirty":
        return after_approve
    return True


async def _handle_cap_exhaustion(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    iterations: int,
    session: AsyncSession,
    *,
    scope: str,
    lane_counts: dict | None = None,
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
    from app.failure_classification import (
        APPROVED_CAP_PR_CLOSED,
        CODE_ERROR,
        persist_failure_category,
    )
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    cap_label = scope if scope in ("global", "per-chain") else scope
    _lane_detail = (
        "; ".join(f"{k}={v}" for k, v in sorted(lane_counts.items()))
        if lane_counts
        else ""
    )
    source_task.latest_status_message = (
        f"Review-fix cap reached for PR #{pr_number} ({iterations} iterations, {cap_label})"
        + (f" [{_lane_detail}]" if _lane_detail else "")
    )
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
    # CANCELLED with failure_detail='cap_exhausted_after_approve' and record the
    # distinct APPROVED_CAP_PR_CLOSED outcome instead of CODE_ERROR. GUARDRAIL:
    # _cap_exhausted_after_approve returns False on any outstanding
    # CHANGES_REQUESTED, preserving today's FAILED + CODE_ERROR path for genuine
    # non-convergence.
    if await _cap_exhausted_after_approve(
        orig, source_task, resolved_original_id, pr_number, session
    ):
        from sqlalchemy.orm.attributes import flag_modified

        # APPROVED + PRESERVABLE: an approved rollup/back-merge PR is the
        # strongest candidate for preservation — close+CANCEL destroys
        # salvageable, signed-off work. Leave it open for hand-merge and
        # keep the APPROVED_CAP_PR_CLOSED labeling (approval changes the
        # label, never whether the PR is destroyed). Non-preservable approved
        # PRs (empty, non-rollup) keep the close+CANCEL path.  Conflicted
        # (dirty) rollup/back-merge PRs are also preserved here: the conflict
        # is a mechanical, recoverable blocker on an APPROVED PR — a human can
        # resolve it for hand-merge (after_approve=True loosens the dirty guard).
        if await _pr_is_preservable_on_cap(orig, pr_number, session, after_approve=True):
            _preserve_kind = (
                "back-merge resolver" if _is_backmerge_resolver(orig) else "rollup"
            )
            logger.info(
                f"[PIPELINE] Cap reached for PR #{pr_number} on an APPROVED, "
                f"non-empty {_preserve_kind} — PRESERVING: leaving "
                f"PR open + flagging orig {resolved_original_id[:8]} "
                f"requires_approval for hand-merge "
                f"(APPROVED_CAP_PR_CLOSED, no CODE_ERROR, no PR close)"
            )
            orig.status = TaskStatus.FAILED
            orig.requires_approval = True
            orig.latest_status_message = (
                f"Review-fix cap reached ({iterations} iterations, {cap_label}) "
                f"after approval — PR #{pr_number} LEFT OPEN with mergeable work; "
                f"manual hand-merge required (approved cap, not auto-closed)"
            )
            persist_failure_category(orig, APPROVED_CAP_PR_CLOSED)
            ctx = dict(orig.context or {})
            ctx["failure_detail"] = "cap_exhausted_after_approve"
            ctx["review_cap_iterations"] = iterations
            ctx["pr_preserved_for_handmerge"] = True
            ctx["handmerge_pr_number"] = pr_number
            # NB: deliberately NOT setting pr_rejected — this was an approved PR.
            orig.context = ctx
            flag_modified(orig, "context")
            session.add(orig)
            try:
                await session.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): failed to "
                    f"commit orig {resolved_original_id[:8]}: {e}",
                    exc_info=True,
                )
                return
            record_task_result(
                success=False,
                failure_class=APPROVED_CAP_PR_CLOSED,
                task_id=str(orig.id),
                task_type=(orig.first_payload or {}).get("type", ""),
            )
            _direction_id = str((orig.context or {}).get("direction_id") or "")
            if _direction_id:
                record_direction_result(
                    _direction_id,
                    success=False,
                    failure_class=APPROVED_CAP_PR_CLOSED,
                    project_id=str(orig.project_id) if orig.project_id else "",
                )
            try:
                from app.task_completion import _record_remediation_if_terminal

                await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
                await session.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): remediation "
                    f"record failed for PR #{pr_number}: {e}"
                )
            try:
                gh_token, owner, repo = await get_github_token_for_task(
                    orig.id, session
                )
                await create_pr_comment(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    f"Review-fix cap reached ({iterations} iterations, {cap_label}) "
                    f"after approval. This PR has mergeable work and was LEFT OPEN "
                    f"for manual hand-merge rather than auto-closed. "
                    f"Operator review required.",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): comment "
                    f"failed on PR #{pr_number}: {e}"
                )
            try:
                await _maybe_create_failure_alert(
                    orig,
                    session,
                    f"PR #{pr_number} hit review-fix cap ({iterations} iterations, "
                    f"{cap_label}) after approval — left open with approved mergeable "
                    f"work, hand-merge required",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): alert "
                    f"failed for PR #{pr_number}: {e}"
                )
            try:
                await _create_handmerge_attention_message(
                    orig, pr_number, iterations, cap_label, session
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): attention "
                    f"message failed for PR #{pr_number}: {e}"
                )
            return

        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} AFTER approval — landing "
            f"orig {resolved_original_id[:8]} CANCELLED (cap_exhausted_after_approve), "
            f"recording {APPROVED_CAP_PR_CLOSED}"
        )
        orig.status = TaskStatus.CANCELLED
        orig.latest_status_message = (
            f"Review-fix cap reached ({iterations} iterations, {cap_label}) after "
            f"approval — PR #{pr_number} auto-closed because merge mechanics exhausted "
            f"the cap (no rejection)"
        )
        persist_failure_category(orig, APPROVED_CAP_PR_CLOSED)
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
        # outcome as a distinct, budget-exempt failure class so analytics and
        # digests can list approved PR closures without CODE_ERROR/PR_REJECTED.
        record_task_result(
            success=False,
            failure_class=APPROVED_CAP_PR_CLOSED,
            task_id=str(orig.id),
            task_type=(orig.first_payload or {}).get("type", ""),
        )
        _direction_id = str((orig.context or {}).get("direction_id") or "")
        if _direction_id:
            record_direction_result(
                _direction_id,
                success=False,
                failure_class=APPROVED_CAP_PR_CLOSED,
                project_id=str(orig.project_id) if orig.project_id else "",
            )
        try:
            from app.task_completion import _record_remediation_if_terminal

            await _record_remediation_if_terminal(orig, TaskStatus.CANCELLED, session)
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
                    f"Work was signed off, but merge mechanics exhausted the cap; "
                    f"closing as superseded.",
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
    # NON-EMPTY, conflict-free PR that aggregates expensive-to-redo work — a
    # ROLLUP PR (merged-elsewhere siblings) or a BACK-MERGE conflict-resolver PR
    # (re-applied main→automain sync) — the prior behaviour (close PR + FAIL +
    # CODE_ERROR) destroyed salvageable work over review-loop non-convergence,
    # and for a back-merge resolver additionally stalled main→automain sync.
    # Instead, leave the PR OPEN and flag the orig requires_approval so an
    # operator can hand-merge it. The orig still lands terminal (FAILED routes
    # through the partial-rollup path and stops the stale-unmerged respawn loop,
    # per the docstring above) but with a distinct failure_detail, NO CODE_ERROR,
    # NO pr_rejected, and NO PR close.
    # GUARDRAIL: _pr_is_preservable_on_cap returns False for empty / conflicted /
    # CI-red PRs and for non-rollup non-backmerge tasks → those keep today's
    # close+FAIL+CODE_ERROR path below. (Rollups also fall back to close on an
    # unknown mergeable state or GitHub lookup failure; back-merge resolvers
    # prefer preserve in those ambiguous cases since closing them is worse.)
    if await _pr_is_preservable_on_cap(orig, pr_number, session):
        from sqlalchemy.orm.attributes import flag_modified

        _preserve_kind = (
            "back-merge resolver" if _is_backmerge_resolver(orig) else "rollup"
        )
        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} on a non-empty, "
            f"conflict-free {_preserve_kind} — PRESERVING work: leaving PR open "
            f"+ flagging orig {resolved_original_id[:8]} requires_approval for "
            f"hand-merge (no CODE_ERROR, no PR close)"
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
        ctx["handmerge_pr_number"] = pr_number
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
        try:
            await _create_handmerge_attention_message(
                orig, pr_number, iterations, cap_label, session
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): attention message failed "
                f"for PR #{pr_number}: {e}"
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
        + (f" [{_lane_detail}]" if _lane_detail else "")
    )
    persist_failure_category(orig, CODE_ERROR)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "review_cap_exhausted"
    ctx["review_cap_iterations"] = iterations
    ctx["pr_rejected"] = True
    ctx["pr_rejection_note"] = (
        f"Auto-closed by Blueprint: review-fix cap exhausted after {iterations} iterations"
    )
    _terminal_detail = cap_label + (f"; {_lane_detail}" if _lane_detail else "")
    ctx["review_loop_terminal_outcome"] = {
        "kind": "cap_exhausted",
        "reason": "review_cap_exhausted",
        "pr_number": pr_number,
        "iterations": iterations,
        "scope": scope,
        "detail": _terminal_detail,
        "lane_counts": lane_counts or {},
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
        detail=_terminal_detail,
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


async def _handle_empty_effective_diff_at_merge(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    session: AsyncSession,
) -> None:
    """Terminal handler when a task PR reaches the merge gate with an EMPTY
    effective diff (``get_pr_files`` returned ``[]`` vs base).

    A code-fix/create PR whose effective diff went empty by merge time (its
    commits already landed on base via a sibling merge / rebase-flat) MUST NOT
    auto-merge and MUST NOT report success — merging it would mark the task
    COMPLETED ('CI passed + review approved, PR merged') while zero user-visible
    change landed (incident 2026-07-12, PR #14500 / commit 683abce38: an empty
    ``git show --stat`` merged 'green' with a title claiming a real fix).

    Instead: close the empty PR and land the chain original terminal with the
    benign, budget-exempt ``SCOPE_EMPTY`` failure class and
    ``failure_detail="empty_effective_diff_at_merge"``. ``pr_closed_empty=True``
    marks the close as an intentional empty no-op (not a review rejection or a
    merge failure) so downstream verification (rollup_pr_verification) does not
    mis-downgrade the outcome.

    Idempotent — re-entering on an already-terminal orig skips side effects.
    """
    from app.failure_classification import SCOPE_EMPTY, persist_failure_category
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    source_task.latest_status_message = (
        f"Empty effective diff at merge for PR #{pr_number} — nothing to land; "
        f"not merging (no-op)"
    )
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to load orig "
            f"{resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: orig {resolved_original_id[:8]} not found"
        )
        return

    already_handled = orig.status in (
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and (orig.context or {}).get("failure_detail") == "empty_effective_diff_at_merge"
    if already_handled:
        logger.info(
            f"[PIPELINE] Empty-diff-at-merge re-triggered for PR #{pr_number} — "
            f"orig {resolved_original_id[:8]} already terminal, skipping side effects"
        )
        await session.commit()
        return

    logger.info(
        f"[PIPELINE] PR #{pr_number} reached the merge gate with an EMPTY effective "
        f"diff — landing orig {resolved_original_id[:8]} CANCELLED "
        f"(empty_effective_diff_at_merge / {SCOPE_EMPTY}), closing PR, NOT merging"
    )
    orig.status = TaskStatus.CANCELLED
    orig.latest_status_message = (
        f"PR #{pr_number} had an empty effective diff at merge — no change to land; "
        f"closed as a no-op instead of reporting a false success"
    )
    persist_failure_category(orig, SCOPE_EMPTY)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "empty_effective_diff_at_merge"
    ctx["pr_closed_empty"] = True
    # NB: deliberately NOT setting pr_rejected — this is a benign no-op, not a
    # review rejection or a code failure.
    orig.context = ctx
    _set_review_loop_terminal_outcome(
        orig,
        kind="empty_effective_diff",
        reason="empty_effective_diff_at_merge",
        pr_number=pr_number,
        iterations=int((ctx.get("review_loop") or {}).get("iteration") or 0),
        detail="empty_effective_diff_at_merge",
    )
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to commit orig "
            f"{resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

    record_task_result(
        success=False,
        failure_class=SCOPE_EMPTY,
        task_id=str(orig.id),
        task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id,
            success=False,
            failure_class=SCOPE_EMPTY,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    try:
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(orig, TaskStatus.CANCELLED, session)
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to record remediation for "
            f"PR #{pr_number}: {e}"
        )

    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                "Auto-closed: this PR had an empty effective diff at merge time "
                "(its changes already landed on the base branch). Closing as a "
                "no-op rather than reporting a false success.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Empty-diff-at-merge: comment failed on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed empty-effective-diff PR #{pr_number} (no-op)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for empty PR #{pr_number}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )


async def _handle_persistent_conflict_escalation(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    iterations: int,
    conflict_lane_count: int,
    session: AsyncSession,
) -> None:
    """Terminal handler for PRs with a persistent, unresolvable merge conflict.

    Called when the conflict-fix lane count reaches
    PERSISTENT_CONFLICT_ESCALATION_THRESHOLD consecutive attempts without
    resolving the conflict. Closes the PR, marks the orig FAILED with
    failure_detail="persistent_merge_conflict", and posts an explicit
    conflict-named PR comment and alert so operators can distinguish this
    from generic review non-convergence.

    Fires BEFORE the lane sub-cap check so the loop stops at iteration 3
    (threshold=2 prior completions) rather than grinding to LANE_CAP_CONFLICT=4
    or the global cap (~16).
    """
    from app.failure_classification import CODE_ERROR, persist_failure_category
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    source_task.latest_status_message = (
        f"Persistent merge conflict on PR #{pr_number} — "
        f"{conflict_lane_count} conflict-fix attempts failed to resolve it; "
        f"escalating for manual intervention"
    )
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Persistent-conflict: failed to load orig "
            f"{resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Persistent-conflict: orig {resolved_original_id[:8]} not found"
        )
        return

    already_handled = orig.status in (
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and (orig.context or {}).get("failure_detail") in (
        "persistent_merge_conflict",
        "review_cap_exhausted",
        "cap_exhausted_after_approve",
        "review_cap_preserved_for_handmerge",
    )
    if already_handled:
        logger.info(
            f"[PIPELINE] Persistent-conflict re-triggered for PR #{pr_number} — "
            f"orig {resolved_original_id[:8]} already terminal, skipping"
        )
        await session.commit()
        return

    orig.status = TaskStatus.FAILED
    if _runtime_config.get("require_approval_gates", True):
        orig.requires_approval = True
    orig.latest_status_message = (
        f"Merge conflict on PR #{pr_number} persisted after {conflict_lane_count} "
        f"fix attempts — auto-closed, manual conflict resolution required"
    )
    persist_failure_category(orig, CODE_ERROR)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "persistent_merge_conflict"
    ctx["review_cap_iterations"] = iterations
    ctx["pr_rejected"] = True
    ctx["pr_rejection_note"] = (
        f"Auto-closed by Blueprint: merge conflict persisted after "
        f"{conflict_lane_count} fix attempts"
    )
    orig.context = ctx
    _set_review_loop_terminal_outcome(
        orig,
        kind="persistent_conflict",
        reason="persistent_merge_conflict",
        pr_number=pr_number,
        iterations=iterations,
        detail=f"conflict_lane_count={conflict_lane_count}",
    )
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Persistent-conflict: failed to commit orig "
            f"{resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

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

    try:
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Persistent-conflict: failed to record remediation for "
            f"PR #{pr_number}: {e}"
        )

    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Auto-closed: merge conflict on this PR persisted after "
                f"{conflict_lane_count} automated fix attempts. "
                f"Manual conflict resolution required before this work can land.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Persistent-conflict: comment failed on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed PR #{pr_number} after persistent merge conflict "
                f"({conflict_lane_count} conflict-fix attempts)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for PR #{pr_number} "
                f"(persistent conflict)"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Persistent-conflict: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )

    try:
        await _maybe_create_failure_alert(
            orig,
            session,
            f"PR #{pr_number} has a persistent merge conflict ({conflict_lane_count} "
            f"fix attempts failed) — auto-closed, manual resolution required",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Persistent-conflict: failed to create alert for PR "
            f"#{pr_number}: {e}"
        )
