"""Terminal pipeline-death re-arm producer for the reaper.

Leaf module by design: it must be importable from inside ``reaper_service``'s
orphan loop without touching ``app.services``. Housing this helper in
``recovery_evaluating_pr_check`` (next to its consumer) created a cold-import
cycle — reaper → recovery_evaluating_pr_check → services → recovery_sweeps →
recovery_evaluating → recovery_evaluating_pr_check (partially initialized) —
so only ``app.models`` and function-local imports are allowed here.
"""

import logging
import uuid
from datetime import datetime, timezone

from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# Producer-side bound for terminal pipeline-death re-arms (see
# ``rearm_pr_state_check_for_dead_pipeline_task``). Counted on the ORIGINAL
# task's context so the bound survives across dead review/fix generations.
# Mirrors the flat-counter discipline of ``review_wait_respawn_count`` /
# ``_resurrection_count`` rather than inventing a new budget mechanism.
PIPELINE_TERMINAL_REARM_COUNT_KEY = "_pipeline_terminal_rearm_count"
PIPELINE_TERMINAL_REARM_EXHAUSTED_KEY = "_pipeline_terminal_rearm_exhausted"
PIPELINE_TERMINAL_REARM_CAP = 2


async def rearm_pr_state_check_for_dead_pipeline_task(
    dead_task,
    session,
    failure_class: str | None,
) -> bool:
    """Re-arm the canonical PR re-drive after a pipeline task dies verdict-less.

    Producer counterpart of ``_handle_pr_state_check_pending``
    (``recovery_evaluating_pr_check``) for the reaper's terminal-FAIL branch.
    When a pipeline ``review``/``fix`` task is reaped to terminal FAILED
    (e.g. AGENT_TIMEOUT after its class retry budget), no agent ever reports
    status, so the agent-status propagation path never runs; the resurrection
    sweep deliberately skips review/fix tasks carrying a ``pr_number`` (they
    must be re-driven from current GitHub state through canonical review
    admission); and an EVALUATING original parked with a ``review_loop`` whose
    ``ci_waiting`` is falsy sits outside every recovery surface — the CI
    monitor filter, the review-wait backstop, and the reaped-owner reconcile
    (which requires NO review_loop) all pass it by. The PR strands un-reviewed
    indefinitely (incident 2026-08-19, review task 20712c3f).

    This helper closes that gap by staging ``_pr_state_check_pending`` on the
    EVALUATING original, so the FIRST-priority eval-sweep consumer re-checks
    live PR state and respawns a review through ``_spawn_pipeline_task``
    (canonical admission: dedup, per-PR iteration caps, stuck-cycle and churn
    halts all apply).

    Bounded: infra-class deaths are excluded from the per-PR review-iteration
    cap by design (``task_counts_toward_review_cap``), so this producer keeps
    its own flat counter on the original (``_pipeline_terminal_rearm_count``,
    cap ``PIPELINE_TERMINAL_REARM_CAP``). On exhaustion it does NOT silently
    park: it stamps an operator-facing WHY on the original
    (``latest_status_message`` + ``_pipeline_terminal_rearm_exhausted``
    provenance) and raises a failure alert, then leaves the bounded
    EVALUATING hard-timeout backstop to terminate the row.

    Mutates + ``session.add``s the original only; the caller owns the commit.
    Returns ``True`` when the marker was staged.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.alert_dispatch import _maybe_create_failure_alert

    payload = dead_task.first_payload or {}
    task_type = str(payload.get("type") or "").lower()
    if task_type not in {"review", "fix"}:
        return False
    pr_num = payload.get("pr_number")
    orig_id = payload.get("original_task_id")
    if not pr_num or not orig_id:
        return False
    try:
        orig_uuid = uuid.UUID(str(orig_id))
    except (ValueError, TypeError):
        return False
    if orig_uuid == dead_task.id:
        return False
    orig = await session.get(Task, orig_uuid)
    if orig is None or orig.status != TaskStatus.EVALUATING:
        # COMPLETED rollup children are re-driven by rollup convergence's
        # bounded unmerged-child respawn; only the EVALUATING park is blind.
        return False
    octx = dict(orig.context or {})
    if octx.get("_pr_state_check_pending"):
        # Already armed — the consumer will re-drive; don't double-count.
        return False
    rearm_count = int(octx.get(PIPELINE_TERMINAL_REARM_COUNT_KEY, 0) or 0)
    dead_short = str(dead_task.id)[:8]
    if rearm_count >= PIPELINE_TERMINAL_REARM_CAP:
        if not octx.get(PIPELINE_TERMINAL_REARM_EXHAUSTED_KEY):
            why = (
                f"Review pipeline for PR #{pr_num} stalled: {task_type} task "
                f"{dead_short} died terminally ({failure_class or 'unknown'}) "
                f"and the automatic re-review budget "
                f"({PIPELINE_TERMINAL_REARM_CAP}) is exhausted — operator "
                f"attention required"
            )
            octx[PIPELINE_TERMINAL_REARM_EXHAUSTED_KEY] = {
                "pr_number": int(pr_num),
                "dead_task_id": str(dead_task.id),
                "dead_task_type": task_type,
                "failure_class": str(failure_class or ""),
                "cap": PIPELINE_TERMINAL_REARM_CAP,
                "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            }
            orig.latest_status_message = why
            orig.context = octx
            flag_modified(orig, "context")
            session.add(orig)
            await _maybe_create_failure_alert(orig, session, why)
            logger.warning(
                "[REAPER pipeline-rearm] PR #%s re-review budget exhausted "
                "(%d) on original %s after %s task %s died (%s) — escalated "
                "to operator",
                pr_num,
                PIPELINE_TERMINAL_REARM_CAP,
                str(orig.id)[:8],
                task_type,
                dead_short,
                failure_class,
            )
        return False
    octx[PIPELINE_TERMINAL_REARM_COUNT_KEY] = rearm_count + 1
    octx["_pr_state_check_pending"] = True
    orig.latest_status_message = (
        f"Pipeline {task_type} task {dead_short} failed terminally "
        f"({failure_class or 'unknown'}) without a verdict — re-armed PR "
        f"#{pr_num} state check to respawn review "
        f"({rearm_count + 1}/{PIPELINE_TERMINAL_REARM_CAP})"
    )
    orig.context = octx
    flag_modified(orig, "context")
    session.add(orig)
    logger.info(
        "[REAPER pipeline-rearm] %s task %s died terminally (%s) — staged "
        "_pr_state_check_pending on original %s for PR #%s (rearm %d/%d)",
        task_type,
        dead_short,
        failure_class,
        str(orig.id)[:8],
        pr_num,
        rearm_count + 1,
        PIPELINE_TERMINAL_REARM_CAP,
    )
    return True
