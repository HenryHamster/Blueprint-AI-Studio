"""PR-less approval-gate hold: park unattended approval timeouts, never destroy them.

Companion to :mod:`app.recovery_evaluating_approval`. That module's non-spend
approval-gate timeout forks on whether the hold has a PR behind it, and every
non-destructive branch it offers — the merged-PR reconcile, the churn-halt
respawn, the preserve-for-handmerge latch — derives a PR number first and no-ops
without one. An approval hold carrying NO PR therefore fell straight through to
``FAILED``. For that shape the task row IS the work (nothing is durable on
GitHub), which makes FAILing it the one disposition that actually destroys
something.

Incident: research task ``70c5ec8f``, killed overnight 2026-08-22 by a 2h window
that measured an operator's sleep rather than a decision. A timeout is the
ABSENCE of an answer, not a decision to discard.

Lives in its own module purely to keep ``recovery_evaluating_approval`` under its
size cap; it is a direct continuation of that file's per-row recovery idiom and
is called from exactly one place.
"""

import logging
from datetime import timedelta

from app import services
from app.admin_approval_authority import (
    approval_hold_provenance,
    consume_approval_hold_metadata,
    revoke_operator_approval_followup,
)
from app.closed_pr_reconciliation import revoke_review_wait_approval_hold
from app.models import TaskStatus

logger = logging.getLogger(__name__)

# ── PR-less approval hold (incident: research task 70c5ec8f) ─────────────────
# Context marker for a PR-less approval-gate timeout that was PARKED rather than
# failed. Named to sit in the same family as the PR path's
# ``approval_timeout_preserved_for_handmerge`` — deliberately a DISTINCT key,
# because that one is a one-way door whose only release is a human hand-merging
# the PR it names. There is no PR here to hand-merge, so reusing it would both
# lie about the disposition and inherit an unreachable release path.
APPROVAL_TIMEOUT_PRLESS_HOLD_KEY = "approval_timeout_preserved_without_pr"
# Stamped on the CLEAN NON-FAILURE terminal once the hold's bound is reached.
# Doubles as the ``failure_detail`` marker so ``direction_outcomes`` can tell an
# unanswered operator hold apart from a genuine code/review failure.
APPROVAL_TIMEOUT_HOLD_EXPIRED_KEY = "approval_timeout_hold_expired"
# How long a PR-less approval hold is parked before it resolves itself.
#
# The bound is NOT optional. A latch with no release path is a PERMANENT wedge,
# which is strictly worse than a clean terminal — this repo has shipped that bug
# more than once. 72h is chosen to span a weekend plus a working day: the 2h
# window that killed 70c5ec8f measured an operator's sleep, and any bound short
# enough to expire unattended reproduces the same defect with extra steps.
#
# The bound is enforced from ``approval_elapsed`` — derived every sweep from the
# already-parsed ``_approval_set_at`` — and NOT from the ``approval_timeout_hold_
# expires_at`` stamp, which is operator-facing display only. A deadline that
# lives only in mutable JSON can be corrupted or dropped into an unbounded hold;
# a deadline recomputed from the same clock the 2h gate already trusts cannot.
APPROVAL_TIMEOUT_PRLESS_HOLD_HOURS = 72


def _approval_hold_pr_number(task, ctx):
    """Widest "is there a PR anywhere behind this hold?" derivation.

    Answers *is any of this task's output already durable outside the task row*.
    A parent whose PR number only lives in ``context.review_loop`` still HAS an
    open PR — the work survives the task row either way — so it stays on the
    existing PR-carrying disposition rather than being diverted into the PR-less
    hold.

    This is now also THE derivation the approval-gate merged-PR reconcile,
    preserve-for-handmerge and churn-halt-respawn branches use (see
    recovery_evaluating_approval.py). It was briefly wider than they were, and
    that skew was not a harmless conservatism: a ``review_loop``-only PR number
    was invisible to all three of them, yet visible HERE, so the PR-less hold
    declined as well and the row fell through BOTH safety nets to a bare FAILED.
    Keep this the single derivation — a predicate that gates a fallback must not
    be able to see more than the paths it falls back FROM.
    """
    review_loop = ctx.get("review_loop")
    return (
        ctx.get("rollup_pr_number")
        or (task.first_payload or {}).get("pr_number")
        or (review_loop.get("pr_number") if isinstance(review_loop, dict) else None)
        or ctx.get("pr_number")
    )


async def _hold_pr_less_approval_gate_timeout(
    task,
    ctx,
    session,
    now,
    *,
    approval_elapsed,
    hours,
) -> None:
    """Park a PR-less approval-gate timeout instead of destroying it.

    INCIDENT. Research task ``70c5ec8f`` (``requires_approval=True``, spawned by
    ``AUTONOMOUS_LOOP`` 2026-08-22T22:58Z, dead 00:58Z) died overnight carrying
    ``context.agent_failure_category_raw = "research_output_invalid"`` and the
    terminal message *"Approval-gate timeout after 2.0h — no manual approval
    received, escalating to FAILED"*. Nobody was awake. The 2h window measured an
    operator's sleep, not a decision, and because the task had no PR every
    non-destructive branch above skipped it: the merged-PR reconcile, the
    churn-halt respawn and the preserve-for-handmerge latch all derive a PR
    number first. Its findings lived only in the task row, so FAILing it was not
    "recording an outcome" — it was the only disposition that destroyed work.

    A timeout is the ABSENCE of an answer, not a decision to discard. So:

    * FIRST sweep past the gate → stamp :data:`APPROVAL_TIMEOUT_PRLESS_HOLD_KEY`,
      raise the ``approval.timeout_held`` operator alert (WARNING, *not* a
      failure alert) and leave the row EVALUATING with its hold intact. No
      ``record_task_result`` / ``classify_failure`` / failure alert ever fires on
      this path — an unanswered hold is not a task failure.
    * LATER sweeps inside the window → no-op. Deliberately no write, so
      ``updated_at`` keeps ageing and the candidate scan's
      ``ORDER BY updated_at ASC`` cannot starve the row behind its per-cycle cap.
    * Past the bound → CLEAN NON-FAILURE ``CANCELLED``, exactly the disposition
      the spend/budget hold above already uses for "the operator simply did not
      answer", with the hold released and the marker left behind for triage.

    WHY THIS CANNOT WEDGE. The release mechanism is this same function, reached
    from the same status-driven scan that set the latch: ``_check_evaluating_
    timeouts`` re-selects *every* non-archived ``EVALUATING`` row each cycle, so a
    parked row is re-examined by construction — there is no second sweep to
    forget to write and no flag stored anywhere else. The bound is computed from
    ``approval_elapsed`` (wall-clock), so it needs no GitHub call, no chain
    probe and no runtime config that could be unavailable or corrupt; the worst
    case is that nobody ever looks and the hold self-resolves to a clean
    non-failure terminal. Two earlier, faster exits also exist: an operator
    approving or denying clears ``requires_approval``, after which
    ``_handle_approval_gate_timeout`` returns ``False`` on its very first line
    and the row rejoins the ordinary recovery branches; an operator requeue or
    cancel terminalizes it directly.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _maybe_create_approval_timeout_hold_alert
    # Function-local: recovery_evaluating_approval imports THIS module at
    # module scope, so a top-level import back would be a cycle. The 2h gate
    # stays defined there — one definition, one place tests patch.
    from app.recovery_evaluating_approval import APPROVAL_GATE_FAIL_SECONDS

    hold_ceiling_seconds = (
        APPROVAL_GATE_FAIL_SECONDS + APPROVAL_TIMEOUT_PRLESS_HOLD_HOURS * 3600
    )

    if approval_elapsed <= hold_ceiling_seconds:
        if ctx.get(APPROVAL_TIMEOUT_PRLESS_HOLD_KEY):
            # Already parked and still inside the window — nothing to do.
            return
        expires_at = now + timedelta(
            seconds=hold_ceiling_seconds - approval_elapsed
        )
        logger.warning(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} approval-gate timeout after "
            f"{hours}h with NO PR behind it — HOLDING for an operator decision "
            f"until {expires_at.isoformat()} "
            f"(+{APPROVAL_TIMEOUT_PRLESS_HOLD_HOURS}h) instead of failing. An "
            f"unattended window must park work, not destroy it (task 70c5ec8f)."
        )
        task.context = {
            **ctx,
            APPROVAL_TIMEOUT_PRLESS_HOLD_KEY: True,
            # Operator-facing display ONLY. The bound is enforced from
            # approval_elapsed, never from this stamp — see the constant.
            "approval_timeout_hold_expires_at": expires_at.isoformat(),
            "failure_detail": APPROVAL_TIMEOUT_PRLESS_HOLD_KEY,
        }
        flag_modified(task, "context")
        session.add(task)
        await _maybe_create_approval_timeout_hold_alert(
            task,
            session,
            f"Approval-gate timeout after {hours}h with no PR to preserve. The "
            f"task is HELD, not failed: approve, deny or requeue it before "
            f"{expires_at.isoformat()}Z, after which the hold releases itself as "
            f"a clean non-failure CANCELLED.",
        )
        await session.commit()
        # Status is unchanged (still EVALUATING); use the literal rather than a
        # post-commit lazy-load, which can MissingGreenlet on an expired row.
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
        )
        return

    # BOUND REACHED. Resolve the hold ourselves rather than leaving it parked
    # forever. CANCELLED (not FAILED) and no failure accounting: after a
    # 72h operator window an unanswered approval request is an absence of
    # interest, not a pipeline failure, and counting it as one would re-create
    # the very miscount this change exists to remove.
    logger.info(
        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} PR-less approval hold reached "
        f"its {APPROVAL_TIMEOUT_PRLESS_HOLD_HOURS}h bound at {hours}h total with "
        f"no operator decision — releasing as CANCELLED (not a failure)"
    )
    task.status = TaskStatus.CANCELLED
    task.latest_status_message = (
        f"Approval hold expired after {hours}h with no operator decision — "
        f"released as CANCELLED (not a failure); requeue to resume"
    )[:500]
    task.requires_approval = False
    revoke_review_wait_approval_hold(task)
    revoke_operator_approval_followup(task)
    consume_approval_hold_metadata(
        task,
        expected_episode=approval_hold_provenance(task),
    )
    task.context = {
        **(task.context or {}),
        APPROVAL_TIMEOUT_PRLESS_HOLD_KEY: True,
        APPROVAL_TIMEOUT_HOLD_EXPIRED_KEY: True,
        "failure_detail": APPROVAL_TIMEOUT_HOLD_EXPIRED_KEY,
    }
    flag_modified(task, "context")
    session.add(task)
    await session.commit()
    await services.broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.CANCELLED},
    )
