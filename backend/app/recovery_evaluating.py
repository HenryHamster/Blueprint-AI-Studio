"""EVALUATING-timeout recovery sweep, extracted from ``app.recovery_sweeps``.

Holds the bounded leader-cycle EVALUATING-timeout sweep
(``_check_evaluating_timeouts``) together with its chain-inspection helpers
(``_chain_has_active_pipeline_work`` and friends), the CI-backstop reason
picker, the long-queued-pipeline superseder, and the spend-hold signal
emitters.

Split out of ``app.recovery_sweeps`` to keep each module below its size cap.
Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing ``from app.services import ...`` call sites and
test patch targets keep resolving unchanged. Shared bounding infrastructure
(C3 constants, ``_bounded``, ``_DeployPauseSnapshot``, session/broadcast
helpers) lives in ``app.services`` and is referenced through the ``services``
module object so test patches against ``app.services.<name>`` still take effect.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import or_
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app import services
from app.branch_resolution import classify_rollup_merge_outcome
from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON
from app.landed_signal import (
    apply_merged_landed_signal,
    mark_landed_on_integration,
)
from app.models import (
    Task,
    TaskStatus,
    Alert,
    AlertSeverity,
    VmContact,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _last_status_transition_at(t: Task) -> datetime | None:
    """Timestamp of the task's most recent real STATUS transition, if logged.

    Reads the ``status_log`` entries appended by the ``_on_status_set`` model
    listener (``{"t": iso, "msg": "Status changed: X → Y"}``). Message-only
    entries (latest_status_message updates) are ignored — they record activity,
    not state transitions. Returns None when no transition entry exists (e.g. a
    row still parked in its initial status, or the best-effort append was
    skipped) so callers can fall back to ``created_at``.
    """
    for entry in reversed(t.status_log or []):
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("msg") or "").startswith("Status changed:"):
            continue
        try:
            return datetime.fromisoformat(str(entry.get("t")))
        except (ValueError, TypeError):
            continue
    return None


async def _chain_has_active_pipeline_work(
    parent: Task,
    session,
    stale_seconds: int = 600,
    queued_max_age_seconds: int = 1800,
) -> bool:
    """True if any review/fix task on this chain has heartbeated within ``stale_seconds``.

    The backstop uses wall-clock time as the sole kill signal, but a rollup PR
    that's legitimately cycling through REQUEST_CHANGES → fix → re-review can
    easily exceed the 30-min CI backstop while the pipeline is actively making
    progress.  Before killing such a task, check whether there is a review or
    fix task on the same chain (matching pr_number + original_task_id) whose
    last_heartbeat is recent — if so, the pipeline is alive and we defer to
    the next sweep.
    """
    rl = (parent.context or {}).get("review_loop", {})
    pr_number = rl.get("pr_number")
    original_id = rl.get("original_task_id") or str(parent.id)
    if not pr_number:
        return False
    try:
        candidates = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == parent.project_id,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        cutoff = _utcnow() - timedelta(seconds=stale_seconds)
        # Children matching this chain (review/fix + pr_number + original_id)
        # whose on-row last_heartbeat looked dead — collected so we can give them
        # a SECOND, off-row chance via vmcontact below before letting the kill
        # cascade-cancel them (Fix 4).
        heartbeat_dead_vm_ids: set[str] = set()
        for t in candidates:
            fp = t.first_payload
            if fp.get("type") not in ("review", "fix"):
                continue
            if fp.get("pr_number") != pr_number:
                continue
            if (fp.get("original_task_id") or "") != original_id:
                continue
            if t.last_heartbeat and t.last_heartbeat > cutoff:
                return True
            # QUEUED/PROVISIONING tasks have no heartbeat yet but count as live
            # work only if their last STATUS TRANSITION was recent — beyond
            # queued_max_age_seconds they are likely stuck (slot caps, direction
            # holds) and must not suppress the parent's hard timeout.
            #
            # Key "recent" on the last real status transition, NOT created_at:
            # a child that ran 50 min IN_PROGRESS then hit a transient
            # blocked-precondition and was optimistically requeued to QUEUED
            # (reaper_sweeps._BLOCKED_OPTIMISTIC_RETRY_CAP path) reads as an
            # HOUR-old row by created_at and would be (wrongly) treated as
            # stuck — that is exactly how the recently-requeued, fully-recoverable
            # fix child 53a75ed5 was cascade-cancelled (incident 2026-06-07).
            # The transition timestamp comes from the status_log entries the
            # _on_status_set model listener appends on every real status change
            # — deliberately NOT updated_at, which any incidental row write
            # (message updates, counters) refreshes; keying on updated_at would
            # let a genuinely-stuck-but-touched child defer the hard timeout
            # indefinitely. Fall back to created_at when no transition was ever
            # logged (a row parked in its initial status).
            if t.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING):
                queued_cutoff = _utcnow() - timedelta(seconds=queued_max_age_seconds)
                last_transition = _last_status_transition_at(t) or t.created_at
                if last_transition and last_transition > queued_cutoff:
                    return True
            # On-row last_heartbeat reads dead. Before giving up on this child,
            # remember its VM so we can corroborate with the uncontended
            # vmcontact signal — a child whose heartbeat write was lock-starved
            # by the rollup's task-row lock (the same false-reap this whole
            # vmcontact apparatus targets) still has a fresh off-row contact and
            # must NOT be cascade-killed by the parent's hard timeout.
            if t.status == TaskStatus.IN_PROGRESS and t.vm_instance_id:
                heartbeat_dead_vm_ids.add(t.vm_instance_id)

        # Fix 4: OR-in the uncontended vmcontact liveness signal for the chain's
        # heartbeat-dead children. A child whose contended last_heartbeat write
        # was skipped (rollup holds the task row FOR UPDATE) reads as dead on-row
        # but still upserts vmcontact(last_contact_at) on its own row — so a
        # contact within the SAME stale cutoff proves it is alive and the
        # irreversible hard-FAIL must defer. A genuinely dead child (no requests)
        # has a stale/absent contact row and the kill proceeds as before.
        # Best-effort: a lookup error never blocks the kill path.
        if heartbeat_dead_vm_ids:
            contact_rows = (
                await session.execute(
                    select(VmContact.vm_instance_id, VmContact.last_contact_at).where(
                        VmContact.vm_instance_id.in_(heartbeat_dead_vm_ids)
                    )
                )
            ).all()
            for _vm_id, last_contact in contact_rows:
                if last_contact is not None and last_contact > cutoff:
                    logger.info(
                        f"[EVAL TIMEOUT] Task {str(parent.id)[:8]} chain child on "
                        f"vm={_vm_id} had stale last_heartbeat but fresh vmcontact "
                        f"(last_contact_at={last_contact}) — pipeline alive "
                        f"(lock-starved heartbeat), deferring kill"
                    )
                    return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check chain activity for task {str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _approval_gate_chain_busy(parent: Task, session) -> bool:
    """True if this parent's review-fix chain still has NON-TERMINAL child work
    for the chain's PR — including EVALUATING children (a fix task parked in
    CI-wait), which :func:`_chain_has_active_pipeline_work` deliberately
    excludes from its liveness definition.

    Used ONLY to defer the approval-gate timeout (and bounded there by
    ``APPROVAL_GATE_DEFER_WINDOW_HOURS``), never to defer a kill cascade, so
    the generous activity definition is safe: the worst case of a false "busy"
    is a few extra hours of waiting before the unchanged FAILED terminalization.
    """
    rl = (parent.context or {}).get("review_loop", {}) or {}
    pr_number = rl.get("pr_number")
    if not pr_number:
        return False
    try:
        children = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == parent.id,
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
            )
            .scalars()
            .all()
        )
        for child in children:
            fp = child.first_payload
            if fp.get("type") not in ("review", "fix"):
                continue
            if fp.get("pr_number") == pr_number:
                return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check approval-gate chain children for "
            f"task {str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _chain_has_recoverable_failed_work(parent: Task, session) -> bool:
    """True if the chain has FAILED tasks with infra-class failure_category.

    The 6h EVALUATING wall-clock was designed for live deadlock — child stuck
    IN_PROGRESS, no heartbeat, parent waiting forever. It misfires when children
    are FAILED but RECOVERABLE: a PROVISION_FAILURE / RATE_LIMIT / EXTERNAL_API
    child is eligible for the resurrection sweep once zones recover, and an
    operator deploying a real fix may requeue these children.  Cascade-killing
    them at the wall-clock destroys recoverable work.

    Returns True if any direct child OR any chain pipeline task (review/fix
    matching this parent's chain) is FAILED with failure_category in
    INFRA_CLASSES. The caller defers the kill in that case for a bounded window.
    """
    from app.failure_classification import INFRA_CLASSES

    direct_children = (
        (await session.execute(select(Task).where(Task.parent_task_id == parent.id)))
        .scalars()
        .all()
    )
    direct_child_ids = {str(c.id) for c in direct_children}

    for c in direct_children:
        if c.status != TaskStatus.FAILED:
            continue
        cat = (c.context or {}).get("failure_category")
        if cat in INFRA_CLASSES:
            return True

    if not direct_child_ids:
        return False

    # Pipeline / descendant tasks whose original_task_id points at one of our
    # direct children — covers review/fix tasks attached to create children.
    try:
        candidates = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.project_id == parent.project_id)
                    .where(Task.status == TaskStatus.FAILED)
                    .order_by(Task.created_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        for t in candidates:
            orig = t.first_payload.get("original_task_id") or ""
            if orig not in direct_child_ids:
                continue
            cat = (t.context or {}).get("failure_category")
            if cat in INFRA_CLASSES:
                return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check chain recoverability for task {str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _chain_has_unexhausted_optimistic_retry_work(parent: Task, session) -> bool:
    """True if a chain review/fix task is QUEUED with optimistic-retry budget left.

    The CI-wait backstop's stale-liveness guard (``_chain_has_active_pipeline_work``)
    now keys QUEUED children on ``updated_at`` so a freshly-requeued child counts
    as live. But a child can sit QUEUED past ``queued_max_age_seconds`` between
    optimistic-retry episodes (slot caps stall its re-dispatch) and read as stale
    — even though the reaper will optimistically requeue it again the moment a slot
    frees, because its ``_blocked_optimistic_retries`` budget is not yet spent.
    Cascade-cancelling such a child at the backstop destroys recoverable work
    (incident 2026-06-07, fix child 53a75ed5).

    Returns True only while a chain review/fix task in QUEUED/PROVISIONING still
    has ``_blocked_optimistic_retries`` BELOW the reaper's cap — an exhausted
    child (``>= cap``) no longer re-arms, so it stops deferring the backstop and
    the system cannot wedge forever. Best-effort: a lookup error never blocks the
    kill path.
    """
    from app.reaper_sweeps import _BLOCKED_OPTIMISTIC_RETRY_CAP

    rl = (parent.context or {}).get("review_loop", {})
    pr_number = rl.get("pr_number")
    original_id = rl.get("original_task_id") or str(parent.id)
    if not pr_number:
        return False
    try:
        candidates = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == parent.project_id,
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for t in candidates:
            fp = t.first_payload
            if fp.get("type") not in ("review", "fix"):
                continue
            if fp.get("pr_number") != pr_number:
                continue
            if (fp.get("original_task_id") or "") != original_id:
                continue
            used = int((t.context or {}).get("_blocked_optimistic_retries", 0))
            if used < _BLOCKED_OPTIMISTIC_RETRY_CAP:
                return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check optimistic-retry budget for task "
            f"{str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _summarize_latest_review_for_chain(parent: Task, session) -> dict | None:
    """Return a small summary of the most recent review task on the same chain.

    Used by the CI-wait backstop to build an honest diagnostic reason when the
    review-approved gate never closed. Matches on project + pr_number + the
    parent's chain id (original_task_id or parent.id).
    """
    rl = (parent.context or {}).get("review_loop", {})
    pr_number = rl.get("pr_number")
    original_id = rl.get("original_task_id") or str(parent.id)
    if not pr_number:
        return None
    try:
        # Bound the candidate set: filter to relevant statuses and recent rows.
        # Payload-based filtering (type/pr_number/original_task_id) still happens
        # in Python since these live in JSONB.
        candidates = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.project_id == parent.project_id)
                    .where(
                        Task.status.in_(
                            (
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                            )
                        )
                    )
                    .order_by(Task.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        matches = [
            t
            for t in candidates
            if t.first_payload.get("type") == "review"
            and t.first_payload.get("pr_number") == pr_number
            and (t.first_payload.get("original_task_id") or "") == original_id
        ]
        if not matches:
            return None
        # datetime.min is a naive sentinel matching our naive-UTC column convention;
        # only used when created_at is unset (should not happen — DB sets a default).
        latest = max(matches, key=lambda t: t.created_at or datetime.min)  # noqa: DTZ901
        status_val = (
            latest.status.value
            if hasattr(latest.status, "value")
            else str(latest.status)
        )
        return {
            "id": str(latest.id),
            "short_id": str(latest.id)[:8],
            "status": status_val,
            "last_heartbeat": latest.last_heartbeat,
        }
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to summarize review for task {str(parent.id)[:8]}",
            exc_info=True,
        )
        return None


# Threshold for considering a never-claimed QUEUED pipeline task "abandoned"
# during CI backstop recovery. Matches the queued_max_age_seconds default in
# _chain_has_active_pipeline_work so the two paths agree on what "stuck" means.
_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS = 30 * 60


def _ci_backstop_timeout_reason(
    *,
    ci_ws_val: object,
    ci_ws_valid: bool,
    ci_passed: bool,
    review_approved: bool,
    was_escalated: bool,
    review_summary: dict | None,
    review_loop: dict,
) -> str:
    """Compose the EVALUATING-timeout reason string for a CI-waiting parent.

    Pure function so the branching can be unit-tested without spinning a DB.
    The (ci_passed AND review_approved) branch surfaces the downstream merge-
    gate state — mergeable_state, in-progress reset count, last merge message —
    because both pipeline gates being open while the parent is still EVALUATING
    means the blocker is on the merge side, not the gates.  The historical
    fall-through used to print "both gates unmet" in that case, which is the
    opposite of the truth and made these stuck tasks invisible.
    """
    if ci_ws_val is None:
        return "ci_wait_started missing from review_loop"
    if not ci_ws_valid:
        return f"ci_wait_started corrupt: {ci_ws_val!r}"
    if ci_passed and not review_approved:
        if review_summary:
            return (
                f"CI passed but review {review_summary['short_id']} is "
                f"{review_summary['status']} without verdict callback"
            )
        return "CI passed but no review task exists for this chain"
    if review_approved and not ci_passed:
        return "review approved but CI never completed within backstop window"
    if ci_passed and review_approved:
        mergeable_state = review_loop.get("mergeable_state")
        merge_in_progress_resets = review_loop.get("merge_in_progress_resets", 0)
        last_merge_msg = review_loop.get("merge_last_error") or review_loop.get(
            "last_merge_msg"
        )
        return (
            f"CI green and review approved but merge blocked: "
            f"mergeable_state={mergeable_state}, "
            f"merge_attempts={merge_in_progress_resets}, "
            f"last_merge_msg={(last_merge_msg or '')[:200]}"
        )
    if was_escalated:
        return "both gates unmet after 20-min escalation"
    return "neither CI nor review completed within backstop window"


async def _supersede_long_queued_pipeline_tasks(
    parent: Task,
    session,
    pr_number: int,
    original_task_id: str,
) -> int:
    """Cancel review/fix tasks on this chain that have been QUEUED past the
    abandon threshold without ever being claimed.

    Called from CI backstop recovery to unblock the dedup guard in
    _spawn_pipeline_task — a fix task that's sat in the queue for an hour
    waiting for an account is the same blocker as a dead VM, just slower
    to detect. Without this, the recovery review spawn defers and the
    parent gets hard-killed when it could have been recovered.

    Returns the count of tasks superseded so the caller can log it.
    """
    from app.failure_classification import AGENT_TIMEOUT, persist_failure_category

    cutoff = _utcnow() - timedelta(seconds=_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS)
    candidates = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == parent.project_id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    superseded = 0
    for t in candidates:
        if t.id == parent.id:
            continue
        fp = t.first_payload
        if fp.get("type") not in ("review", "fix"):
            continue
        if fp.get("pr_number") != pr_number:
            continue
        if (fp.get("original_task_id") or "") != original_task_id:
            continue
        # Skip tasks that have actually been claimed and are working.
        if t.last_heartbeat is not None or t.started_at is not None:
            continue
        if t.created_at and t.created_at >= cutoff:
            continue  # too fresh — could legitimately be waiting on capacity
        t.status = TaskStatus.CANCELLED
        t.latest_status_message = (
            f"Superseded by CI backstop recovery on PR #{pr_number}: "
            f"queued >{_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS // 60}min "
            f"without claim, blocking parent recovery"
        )
        persist_failure_category(t, AGENT_TIMEOUT)
        session.add(t)
        superseded += 1
        logger.warning(
            f"[BACKSTOP RECOVERY] Cancelling long-queued {fp.get('type')} "
            f"{str(t.id)[:8]} on PR #{pr_number} so recovery review can proceed"
        )
    if superseded:
        await session.flush()
    return superseded


async def _attempt_ci_backstop_recovery(
    parent: Task, session, review_loop: dict
) -> bool:
    """One-shot attempt to unstick a parent whose review gate never closed.

    Used when CI has passed but review_approved is absent — typically because
    the original review VM died (rate-limit, crash) before posting status.
    Spawns a fresh review via the normal pipeline path; the new dedup guard
    will supersede any stale corpse on the same chain.

    Before spawning, supersedes any review/fix on this chain that has sat in
    QUEUED past the abandon threshold (account exhaustion, slot starvation).
    Without this step, _spawn_pipeline_task's dedup guard defers the recovery
    review and the parent gets hard-killed instead of recovered.

    Returns True iff a fresh review was spawned (parent should reset its CI
    wait window and skip the hard kill for this tick).
    """
    from app.review_pipeline import _spawn_pipeline_task
    from app.models import Project

    pr_number = review_loop.get("pr_number")
    branch = review_loop.get("branch")
    _proj = await session.get(Project, parent.project_id)
    base_branch = review_loop.get(
        "base_branch"
    ) or _shared_state.get_project_integration_branch(_proj)
    original_task_id = review_loop.get("original_task_id") or str(parent.id)
    if not (pr_number and branch):
        return False
    try:
        await _supersede_long_queued_pipeline_tasks(
            parent,
            session,
            pr_number,
            original_task_id,
        )
        review_task, _ = await _spawn_pipeline_task(
            parent,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=(parent.description or parent.title or ""),
            original_task_id=original_task_id,
        )
        return review_task is not None
    except Exception as e:
        logger.warning(
            f"[EVAL TIMEOUT] Backstop review-recovery spawn failed for task "
            f"{str(parent.id)[:8]}: {e}",
            exc_info=True,
        )
        return False


async def _verify_ci_before_backstop(
    parent: Task, review_loop: dict, head_sha: str, session
) -> bool:
    """Read the PR's LIVE check-run conclusion before the backstop fails a parent.

    The CI-wait backstop fires on a wall-clock that can outlast a CI conclusion
    that actually landed before the wait window even started — e.g. when
    ``review_loop.ci_wait_started`` is re-armed after CI already concluded
    (incident 2026-06-07). This reads the real check-run state and routes on the
    truth instead of timing out blindly:

      - concluded FAILURE → delegate to ``handle_ci_completion`` so the existing
        CI-failure handling (fix-spawn path, ci_passed=False) runs;
      - concluded SUCCESS → record the pass and reset/clear the CI-wait clock so
        the parent stops counting toward the backstop;
      - still pending / GitHub error → return False (fail OPEN: fall through to
        the existing timeout behaviour — never kill on an outage, never block).

    ALL GitHub I/O is performed here, strictly OUTSIDE the caller's FOR-UPDATE
    re-validate window. ``handle_ci_completion`` re-locks the row itself.

    Returns True iff CI had genuinely concluded and was handled (the caller must
    then skip the kill); False to fall through to the existing timeout path.
    """
    from app.routers.agent import get_github_token_for_task
    from app.github_checks import get_check_runs_cached
    from app.ci_completion import handle_ci_completion

    pr_number = review_loop.get("pr_number")
    try:
        _gh, _owner, _repo = await services._bounded(
            get_github_token_for_task(parent.id, session),
            services._GH_CALL_TIMEOUT_S,
            what=f"ci-backstop token {str(parent.id)[:8]}",
        )
        ci_result = await services._bounded(
            get_check_runs_cached(
                _gh,
                _owner,
                _repo,
                head_sha,
                project_id=str(parent.project_id) if parent.project_id else None,
            ),
            services._GH_CALL_TIMEOUT_S,
            what=f"ci-backstop check-runs PR #{pr_number}",
        )
    except Exception as e:
        # Fail OPEN: a GitHub error / timeout must never block the backstop —
        # fall through to the existing timeout behaviour.
        logger.warning(
            f"[EVAL TIMEOUT] CI re-verify before backstop failed for task "
            f"{str(parent.id)[:8]} (PR #{pr_number}): {e} — falling through to timeout"
        )
        return False

    # API-error results are tagged so the caller doesn't cache a fake-pending —
    # treat them as 'no signal' and fail open.
    if ci_result.get("_from_api_error"):
        logger.info(
            f"[EVAL TIMEOUT] CI re-verify for task {str(parent.id)[:8]} (PR "
            f"#{pr_number}) hit a GitHub API error — falling through to timeout"
        )
        return False

    if ci_result.get("status") != "completed":
        # CI genuinely still pending — the backstop's wall-clock is the only
        # signal left; fall through.
        return False

    # CI has actually concluded. Delegate to the shared completion handler, which
    # re-locks the row, records the gate signal, and runs the fix-spawn (failure)
    # or merge/approval (success) path exactly as the orchestrator poll loop does.
    logger.warning(
        f"[EVAL TIMEOUT] Task {str(parent.id)[:8]} CI re-verify: PR #{pr_number} "
        f"actually concluded ({ci_result.get('conclusion')}) before backstop — "
        f"routing to CI-completion handler instead of failing the parent"
    )
    try:
        await handle_ci_completion(
            parent,
            ci_result,
            _gh,
            _owner,
            _repo,
            session,
            source="ci_backstop_verify",
        )
    except Exception as e:
        logger.warning(
            f"[EVAL TIMEOUT] handle_ci_completion failed during CI backstop "
            f"re-verify for task {str(parent.id)[:8]} (PR #{pr_number}): {e} — "
            f"falling through to timeout",
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()
        return False
    return True


def _dispatch_spend_hold_notification(
    task: Task,
    event_type: str,
    window_hours: int,
    hours_remaining: float | None = None,
) -> None:
    """Fire-and-forget external notification for a spend/budget approval hold.

    ``event_type`` is ``SPEND_HOLD_CREATED`` or ``SPEND_HOLD_EXPIRING``. Never
    raises — notification failure must never block recovery. Idempotency is the
    caller's responsibility (guarded by context flags so a hold notifies once).
    """
    try:
        from app.notification_dispatch import dispatch_external_notification

        payload = {
            "task_id": str(task.id),
            "task_title": task.title or str(task.id)[:8],
            "task_type": (task.first_payload or {}).get("type", ""),
            "approval_hold_reason": (task.context or {}).get(
                "approval_hold_reason", ""
            ),
            "window_hours": window_hours,
        }
        if hours_remaining is not None:
            payload["hours_remaining"] = hours_remaining
        asyncio.ensure_future(  # noqa: RUF006  # fire-and-forget; caller does not await
            dispatch_external_notification(task.project_id, event_type, payload)
        )
    except Exception as _ne:
        logger.warning(
            "[SPEND HOLD] %s dispatch setup failed for %s: %r",
            event_type,
            str(task.id)[:8],
            _ne,
        )


async def _emit_spend_hold_created_signals(
    task: Task, session, *, window_hours: int
) -> None:
    """Make a newly-seen spend/budget hold LOUD: WARNING alert + notification.

    The alert is a NON-FAILURE WARNING (the hold is a deliberate cost decision,
    not a pipeline failure), staged on the caller's session and deduped per task.
    """
    from app.alert_dispatch import _maybe_create_spend_hold_alert

    message = (
        f"Task '{task.title or str(task.id)[:8]}' is held for spend/budget "
        f"approval. If not approved within {window_hours}h it will be declined "
        f"(CANCELLED, not counted as a failure)."
    )
    try:
        await _maybe_create_spend_hold_alert(task, session, message)
    except Exception as _ae:
        logger.warning(
            "[SPEND HOLD] WARNING-alert create failed for %s: %r",
            str(task.id)[:8],
            _ae,
        )
    _dispatch_spend_hold_notification(task, "SPEND_HOLD_CREATED", window_hours)


async def _check_evaluating_timeouts(progress_cb=None):
    """Detect EVALUATING tasks that are stuck and recover or fail them.

    - If all children are terminal but parent is still EVALUATING → trigger missed rollup.
    - If evaluating >2h with non-terminal children → WARNING alert.
    - If evaluating >evaluating_fail_hours (default 48h) → FAIL with ERROR alert.
    - Skips CI-waiting tasks (they have their own 20-min escalation timeout).

    ``progress_cb`` (C3): a zero-arg liveness heartbeat pumped once per processed
    row. This sweep now runs in its OWN background task
    (``_evaluating_timeout_service``, lifecycle.py), so it must NOT pump the
    orchestrator_worker's shared ``_orchestrator_last_progress`` marker — a
    foreign task pumping the worker's heartbeat would MASK a genuinely hung
    orchestrator_worker from its /health gate + watchdog. Instead the caller
    threads its OWN marker (or relies on the background task's per-tick
    asyncio.timeout + _monitor_background_tasks supervision). Defaults to a
    no-op for direct callers / tests that don't supply one.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.rollup_management import _check_and_trigger_rollup
    from app.shared_state import runtime_config as _runtime_config

    _heartbeat = progress_cb if progress_cb is not None else (lambda: None)

    EVALUATING_WARN_SECONDS = 2 * 3600  # 2 hours
    # Default backstop bumped to 48h while silent-death v2 stabilizes
    # (2026-05-04): tasks reaped mid-flight by the 6h wall-clock were getting
    # cascade-killed during operator-intervention windows. Runtime knob
    # `evaluating_fail_hours` lets us drop it back once the recurrence rate
    # for silent-death + reaper sweeps comes down.
    # Defensive read: the PATCH endpoint validates 1..168, but a corrupted
    # runtime_config_json on disk could surface a non-int / out-of-range value
    # which would raise ValueError on `int(...)` and abort the whole sweep
    # cycle silently — no EVALUATING cleanup until someone PATCHes a fix.
    try:
        _eval_hours_raw = _runtime_config.get("evaluating_fail_hours", 48)
        _eval_hours = int(_eval_hours_raw)
        if not (1 <= _eval_hours <= 168):
            _eval_hours = 48
    except (ValueError, TypeError):
        _eval_hours = 48
    EVALUATING_FAIL_SECONDS = _eval_hours * 3600
    # Hard kill for CI-waiting tasks. Tunable so multi-iteration review/fix
    # cycles (each ~5-15 min, plus pipeline-cap queueing) don't trigger a
    # premature kill that cancels in-flight children. 60 min is the default
    # because realistic cycles routinely run 30-50 min; 30 min was firing
    # mid-iteration during pipeline-throttled periods.
    CI_BACKSTOP_SECONDS = (
        int(_runtime_config.get("ci_backstop_timeout_minutes", 60)) * 60
    )
    # Bounded defer window for the CI-wait backstop when the chain still holds
    # recoverable (infra-class FAILED / freshly-requeued) work — mirrors the 6h
    # hard-timeout path's ``EVAL_DEFER_WINDOW_HOURS``. A parent must not be
    # FAILED (nor its children cascade-cancelled) while salvageable work exists,
    # but a truly stuck case must still terminate eventually. Shorter than the
    # 6h path's 24h window: the CI-wait backstop guards a much tighter loop
    # (rollup PR CI + review iteration), and the optimistic-retry cap bounds how
    # long a recoverable child can keep re-arming anyway.
    CI_BACKSTOP_DEFER_WINDOW_HOURS = 6
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
    # Deliberate spend/budget approval holds (requires_approval=True AND
    # context.approval_hold_reason == "spend_budget") get their OWN longer,
    # operator-configurable window and resolve to a CLEAN NON-FAILURE CANCELLED
    # terminal on expiry — a cost decision an operator simply has not reached is
    # not a pipeline failure. Defensive read mirrors evaluating_fail_hours: a
    # corrupted runtime_config_json must not abort the whole sweep cycle.
    try:
        _spend_hours_raw = _runtime_config.get("spend_approval_timeout_hours", 24)
        _spend_hours = int(_spend_hours_raw)
        if not (1 <= _spend_hours <= 168):
            _spend_hours = 24
    except (ValueError, TypeError):
        _spend_hours = 24
    SPEND_APPROVAL_FAIL_SECONDS = _spend_hours * 3600
    # BLOCKED and PARKED are treated as terminal-for-now in this EVALUATING-timeout
    # sweep: both are durable non-terminal states that must NOT be cascade-CANCELLED
    # on a timeout. (a) Cancelling them destroys durable child state — a BLOCKED
    # child's blocked state, or a PARKED child's in-flight wake registration and
    # compute-job result — when each would have stayed put exactly as a FAILED child
    # stays terminal. (b) A parent whose remaining children are all BLOCKED/PARKED
    # must still count as all-children-settled so the missed-rollup recovery fires
    # instead of pinning EVALUATING forever.
    TERMINAL = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }

    try:
        from app import pause_clock
        from app.models import DeploymentState

        now = _utcnow()

        # ── Phase A: cycle reads on a short-lived session ──────────────────
        # Audit P1 (session hygiene): the cycle-level candidate scan + child
        # snapshot run on their OWN session that is CLOSED before the per-row
        # loop opens any per-row session. Previously a single shared session
        # was held open across the whole loop, and a _bounded() timeout /
        # cancellation mid-DB-op left that shared session in PendingRollback
        # for the REST of the tick — so every later per-row recovery mutation
        # was silently dropped (exactly under the load — many EVALUATING rows +
        # slow GitHub — where recovery matters most). The fix below gives each
        # row its OWN short-lived session, so a cancellation/rollback on one
        # row can never poison the others. Only PLAIN snapshots (ids, statuses)
        # cross the session boundary; no live ORM instance is reused across
        # sessions (which would risk a cross-session lazy-load / MissingGreenlet).
        # NB the in-memory SQLite test DB shares one connection across sessions
        # (StaticPool), so two concurrently-open sessions self-deadlock; Phase A
        # is fully closed before Phase B opens, and per-row sessions are
        # strictly sequential — never two open at once.
        async with services.AsyncSessionLocal() as read_session:
            # Fetch DeployState once per cycle so CI backstop can subtract
            # pause windows from elapsed (admin pause + auto-pause both count).
            # Snapshot the pause fields into a plain object so the per-row
            # sessions (Phase B) don't reuse this read_session's ORM instance.
            _ds_row = await read_session.get(DeploymentState, 1)
            _deploy_state_snapshot = (
                services._DeployPauseSnapshot.from_state(_ds_row) if _ds_row else None
            )
            # Phase B4 (H-P2): drop ``plan_output`` JSONB; the EVALUATING-timeout
            # loop only reads context/payloads/status_log of the heavy JSONB
            # columns. status_log is consumed at lines ~1216 and ~1384 to find
            # the last EVALUATING transition, so it must remain loaded.
            evaluating_tasks = (
                (
                    await read_session.execute(
                        select(Task)
                        .options(load_only(Task.id, Task.status, Task.archived, raiseload=True))
                        .where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.archived.is_(False),
                        )
                        # C3: per-cycle row cap. Without it this sweep is O(N
                        # EVALUATING backlog) of serial GitHub I/O on the leader
                        # cycle. Deferred rows re-qualify next cycle (the scan is
                        # status-based; oldest-updated first so the most-stuck
                        # parents are always serviced before the cap truncates).
                        .order_by(Task.updated_at.asc())
                        .limit(services._EVAL_TIMEOUT_MAX_PER_CYCLE)
                    )
                )
                .scalars()
                .all()
            )
            # Snapshot just the candidate IDs — each row is re-fetched fresh in
            # its own per-row session below.
            evaluating_task_ids = [t.id for t in evaluating_tasks]
            if len(evaluating_task_ids) >= services._EVAL_TIMEOUT_MAX_PER_CYCLE:
                logger.warning(
                    "[EVAL TIMEOUT] Hit per-cycle cap of %d EVALUATING rows — "
                    "remainder deferred to next sweep",
                    services._EVAL_TIMEOUT_MAX_PER_CYCLE,
                )

            # Phase B4 (H-P2): batch the child lookup for all evaluating parents
            # into one ``IN (...)`` query instead of one-per-parent (N+1).
            #
            # Only a plain (id, status) snapshot is kept — NOT live ORM rows.
            # The hot all-terminal check below reads the snapshot; the rare
            # branches that mutate children re-fetch them fresh on the per-row
            # session.
            _eval_child_status_by_parent: Dict[uuid.UUID, list[tuple]] = {}
            if evaluating_task_ids:
                _eval_child_rows = (
                    (
                        await read_session.execute(
                            select(Task)
                            .options(
                                load_only(Task.id, Task.parent_task_id, Task.status, raiseload=True)
                            )
                            .where(Task.parent_task_id.in_(evaluating_task_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for _ec in _eval_child_rows:
                    _eval_child_status_by_parent.setdefault(
                        _ec.parent_task_id, []
                    ).append((_ec.id, _ec.status))
        # read_session is now closed — no session is held across the loop.

        # ── Phase B: per-row recovery, each on its OWN short-lived session ──
        for _eval_task_id in evaluating_task_ids:
            # C3 heartbeat: this loop performs serial GitHub I/O
            # (get_pull_request / close_pull_request / _check_and_trigger_rollup)
            # per task and is the dominant contributor to the EVALUATING-sweep
            # wall time. Pump the CALLER'S OWN liveness marker each row so a
            # legitimately long-but-progressing sweep is never mistaken for a
            # hung task. NB: this is the background task's own heartbeat, NOT
            # the orchestrator_worker's — pumping the worker's shared marker
            # from here would mask a genuinely hung worker (fix D).
            _heartbeat()
            # Audit P1: open a FRESH session per row. A _bounded() timeout /
            # cancellation that poisons THIS session (PendingRollback) is
            # confined to this iteration — the `async with` closes it on the
            # way out and the next row gets a clean session, so one slow /
            # cancelled row no longer silently drops the rest of the sweep's
            # recovery mutations.
            async with services.AsyncSessionLocal() as session:
                # Re-fetch the candidate fresh on this session and re-validate
                # it is still an unarchived EVALUATING row (it may have been
                # resolved by another worker between Phase A and now). Same
                # load_only column set the cycle scan used to use, so deferred
                # heavy JSONB stays deferred and ``description`` stays loaded
                # for the _pr_state_check_pending consumer's fallback prompt
                # ([[load_only_lazy_load_trap]]).
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.status,
                                    Task.title,
                                    Task.project_id,
                                    Task.parent_task_id,
                                    Task.archived,
                                    Task.requires_approval,
                                    Task.created_at,
                                    Task.started_at,
                                    Task.updated_at,
                                    Task.latest_status_message,
                                    Task.context,
                                    Task.payloads,
                                    Task.status_log,
                                    Task.last_heartbeat,
                                    Task.attempt_count,
                                    Task.account_id,
                                    Task.vm_instance_id,
                                    Task.description,
                                raiseload=True,)
                            )
                            .where(Task.id == _eval_task_id)
                        )
                    )
                    .scalars()
                    .first()
                )
                if (
                    task is None
                    or task.status != TaskStatus.EVALUATING
                    or task.archived
                ):
                    continue
                # Re-fetch the live DeploymentState on THIS session for the
                # pause-aware elapsed calcs (the snapshot is only used as a
                # fallback if the row went away). Reusing a foreign-session ORM
                # instance here would risk a cross-session lazy-load.
                _deploy_state = (
                    await session.get(DeploymentState, 1)
                ) or _deploy_state_snapshot
                ctx = task.context or {}

                # _pr_state_check_pending consumer. The agent_status FAILED→
                # EVALUATING recovery (agent_status.py ~1567) sets this flag
                # before its GH PR check; if the post-GH lock re-acquire
                # races a concurrent writer (skip_locked returns None at
                # ~1616) the recovery returns ``accepted_pr_check_deferred``
                # leaving the flag stuck and no review task spawned. Without
                # this consumer the task sits until the 48h reaper backstop.
                if ctx.get("_pr_state_check_pending"):
                    pr_num = (ctx.get("review_loop") or {}).get("pr_number") or ctx.get(
                        "rollup_pr_number"
                    )
                    if not pr_num:
                        for entry in reversed(task.status_log or []):
                            m = re.search(r"open PR #(\d+)", entry.get("msg", "") or "")
                            if m:
                                pr_num = int(m.group(1))
                                break
                    # 5-min grace: let the original recovery's lock finish if it's
                    # going to. Without this we'd race the same code path.
                    flag_set_at = None
                    for entry in reversed(task.status_log or []):
                        if "checking PR state" in (entry.get("msg", "") or ""):
                            with contextlib.suppress(KeyError, ValueError, TypeError):
                                flag_set_at = datetime.fromisoformat(entry["t"])
                            break
                    if flag_set_at and (now - flag_set_at).total_seconds() < 300:
                        continue

                    from sqlalchemy.orm.attributes import flag_modified

                    _ctx2 = dict(ctx)
                    _ctx2.pop("_pr_state_check_pending", None)
                    # Set when this recovery records a real integration landing
                    # (out-of-band merge), so the direction-outcome refresh runs
                    # after the per-row commit. Re-fetched in a fresh session below
                    # because the per-row commit can expire ``task`` (and this row
                    # is loaded with load_only).
                    _recovery_merge_refresh_needed = False

                    if not pr_num:
                        # No PR number recoverable — clear flag, fall through to
                        # the normal evaluating timeout logic below.
                        task.context = _ctx2
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        ctx = _ctx2
                    else:
                        gh_pr_state = "open"
                        gh_pr_merged = False
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import get_pull_request

                            # Audit P1: get_github_token_for_task runs DB I/O on
                            # ``session``. ``session`` is now this ROW's own
                            # short-lived session, so a _bounded() timeout
                            # cancellation mid-query that leaves it in
                            # PendingRollback is confined to this iteration — the
                            # per-row try/except below logs+skips and the
                            # per-row ``async with`` closes the poisoned session;
                            # the next row gets a clean one.
                            gh_token, owner, repo = await services._bounded(
                                get_github_token_for_task(task.id, session),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"eval-timeout token {str(task.id)[:8]}",
                            )
                            pr_data = await services._bounded(
                                get_pull_request(gh_token, owner, repo, pr_num),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"eval-timeout get_pull_request #{pr_num}",
                            )
                            gh_pr_state = pr_data.get("state") or "open"
                            gh_pr_merged = bool(pr_data.get("merged"))
                        except Exception as gh_exc:
                            logger.warning(
                                f"[EVAL TIMEOUT pr_check] Task {str(task.id)[:8]} "
                                f"PR #{pr_num} GH lookup failed: {type(gh_exc).__name__}: "
                                f"{gh_exc} — assuming open"
                            )

                        if gh_pr_state == "closed" and gh_pr_merged:
                            from app.models import Project

                            _proj = await session.get(Project, task.project_id)
                            (
                                record_pr_merged,
                                terminal_rollup_merge,
                                landed_branch,
                            ) = await classify_rollup_merge_outcome(
                                task,
                                _proj,
                                session,
                                pr_number=pr_num,
                                merged_base_branch=(pr_data.get("base") or {}).get(
                                    "ref"
                                ),
                                source="services._check_evaluating_timeouts",
                            )
                            task.status = (
                                TaskStatus.COMPLETED
                                if terminal_rollup_merge
                                else TaskStatus.EVALUATING
                            )
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            if terminal_rollup_merge:
                                task.latest_status_message = (
                                    f"Recovered _pr_state_check_pending: PR "
                                    f"#{pr_num} merged out-of-band"
                                )
                            else:
                                task.latest_status_message = (
                                    f"Recovered _pr_state_check_pending: PR "
                                    f"#{pr_num} merged into {landed_branch}; "
                                    "awaiting downstream landing"
                                )
                            if record_pr_merged:
                                # Route through the guarded landed-signal writer
                                # so analytics get the integration-vs-promotion
                                # distinction (mirrors agent_status.py /
                                # reaper.py). Stage _ctx2 onto the task first so
                                # the helper layers on top, then read it back —
                                # _ctx2 is committed unconditionally below.
                                task.context = _ctx2
                                apply_merged_landed_signal(
                                    task,
                                    _proj,
                                    base_branch=landed_branch,
                                    hotfix_lane=bool(
                                        (task.first_payload or {}).get("hotfix")
                                        or _ctx2.get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                )
                                _ctx2 = dict(task.context or {})
                                _recovery_merge_refresh_needed = True
                        elif gh_pr_state == "closed" and not gh_pr_merged:
                            task.status = TaskStatus.FAILED
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = f"Recovered _pr_state_check_pending: PR #{pr_num} closed without merge"
                            _ctx2["pr_closed_unmerged"] = True
                        else:
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            try:
                                from app.review_pipeline import _spawn_pipeline_task
                                from app.models import Project

                                task_type_str = (task.first_payload or {}).get(
                                    "type", ""
                                ) or ""
                                _proj = await session.get(Project, task.project_id)
                                branch = services._derive_task_branch(
                                    task.id,
                                    task_type_str,
                                    _shared_state.get_project_task_branch_namespace(
                                        _proj
                                    ),
                                )
                                base_branch = (task.first_payload or {}).get(
                                    "base_branch"
                                ) or _shared_state.get_project_integration_branch(_proj)
                                # C6 rework (A): resolve the prompt from the
                                # payload first, and only fall back to
                                # ``task.description`` if it is absent. The old
                                # ``.get("prompt", task.description or "")`` form
                                # evaluated the default EAGERLY, touching
                                # ``task.description`` on every call — but the
                                # EVALUATING candidate query loads this row with
                                # ``load_only`` and does NOT include
                                # ``description``, so the read fired a sync
                                # lazy-load that raises MissingGreenlet under the
                                # async session (see [[load_only_lazy_load_trap]]),
                                # pushing every recovery into the spawn-failure
                                # branch in production. Read the payload key
                                # explicitly so the deferred column is only
                                # touched when actually needed.
                                original_prompt = (task.first_payload or {}).get(
                                    "prompt"
                                ) or (task.description or "")
                                await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    pr_num,
                                    branch,
                                    base_branch,
                                    original_prompt=original_prompt,
                                    original_task_id=str(task.id),
                                )
                                task.latest_status_message = f"Recovered _pr_state_check_pending: spawned review for PR #{pr_num}"
                            except Exception as spawn_exc:
                                logger.warning(
                                    f"[EVAL TIMEOUT pr_check] Review spawn failed for "
                                    f"task {str(task.id)[:8]}: {type(spawn_exc).__name__}: "
                                    f"{spawn_exc} — re-staging _pr_state_check_pending "
                                    f"so this consumer self-retries next sweep",
                                    exc_info=True,
                                )
                                task.latest_status_message = "Recovered _pr_state_check_pending: review spawn failed, will retry"
                                # C6 rework (A): this consumer is NOT one-shot. The
                                # marker was popped from _ctx2 up front (line ~1081)
                                # and _ctx2 is committed UNCONDITIONALLY below, so a
                                # failed re-spawn would otherwise DROP the marker and
                                # abandon the childless EVALUATING parent to the
                                # 6h/48h backstop — exactly the sustained-transient-
                                # outage scenario the classifier targets (producer
                                # keeps the marker on pass 1; consumer pass-2 spawn
                                # fails). Re-add it so the consumer re-checks the PR
                                # and re-spawns on the next sweep. Mirrors the
                                # producer-side discipline in agent_status.py /
                                # reaper.py: clear ONLY on confirmed spawn SUCCESS
                                # (the try path above, which falls through to the
                                # commit without re-adding the marker).
                                _ctx2["_pr_state_check_pending"] = True

                        task.context = _ctx2
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        # Heal the direction's research-history merge counts for an
                        # out-of-band merge recovered here. Done in a FRESH session
                        # re-fetching by id: the per-row commit + internal spawn can
                        # expire ``task`` (loaded with load_only), and the refresh
                        # takes the directions lock + commits, so it must not share
                        # this row's session. No-op unless the task carries
                        # direction/research lineage; best-effort.
                        if _recovery_merge_refresh_needed:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            try:
                                async with services.AsyncSessionLocal() as _dir_session:
                                    _dir_task = await _dir_session.get(
                                        Task, _eval_task_id
                                    )
                                    if _dir_task is not None:
                                        await refresh_direction_outcomes_after_merge(
                                            _dir_task, _dir_session
                                        )
                            except Exception as _dir_exc:
                                logger.warning(
                                    "[EVAL TIMEOUT pr_check] direction-outcome "
                                    "refresh failed for out-of-band merge of task "
                                    "%s: %r",
                                    _eval_task_id,
                                    _dir_exc,
                                )
                        # Status is unchanged on this recovery path (task stays
                        # EVALUATING). The _spawn_pipeline_task above can roll back
                        # internally and expire `task` (even with expire_on_commit=
                        # False), so a post-commit task.status read would sync
                        # lazy-load in async ctx → MissingGreenlet. Use the literal.
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
                        )
                        continue

                # EVALUATING-STRAND BACKSTOP. A review/CI task that spawned a
                # conflict-fix or ci-fix child (review_loop.conflict_fix_spawned /
                # ci_fix_spawned set) parks here until the chain resolves. The
                # historical strand: an APPROVED review hit a merge conflict,
                # spawned a conflict-fix, the fix's PR later MERGED, but the
                # original review was never terminalized — the conflict_fix_spawned
                # latch was never cleared (orchestrator:3086 skips it, ci_completion
                # only updates the chain root) so it sits in EVALUATING forever.
                # The root cause (children inheriting _is_rollup_review and looking
                # like rollup parents) is fixed in review_pipeline; this is a
                # belt-and-suspenders recovery for already-stranded rows.
                #
                # GUARDRAIL: only acts on NON-rollup-parent tasks (real rollup
                # parents are handled by their own merge gate), and NEVER completes
                # a task whose PR is still OPEN — open PRs are left EVALUATING.
                _strand_rl = ctx.get("review_loop") or {}
                _strand_spawned = _strand_rl.get("conflict_fix_spawned") or (
                    _strand_rl.get("ci_fix_spawned")
                )
                if _strand_spawned and not ctx.get("pr_merged"):
                    from app.rollup_state import _is_rollup_parent_awaiting_merge

                    if not _is_rollup_parent_awaiting_merge(ctx):
                        _strand_pr = _strand_rl.get("pr_number") or ctx.get(
                            "rollup_pr_number"
                        )
                        if _strand_pr:
                            _s_state = "open"
                            _s_merged = False
                            _s_lookup_ok = False
                            try:
                                from app.routers.agent import (
                                    get_github_token_for_task,
                                )
                                from app.github import get_pull_request

                                _gh = await services._bounded(
                                    get_github_token_for_task(task.id, session),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"strand token {str(task.id)[:8]}",
                                )
                                _gh_token, _owner, _repo = _gh
                                _pr = await services._bounded(
                                    get_pull_request(
                                        _gh_token, _owner, _repo, _strand_pr
                                    ),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"strand get_pull_request #{_strand_pr}",
                                )
                                _s_state = _pr.get("state") or "open"
                                _s_merged = bool(_pr.get("merged"))
                                _s_lookup_ok = True
                            except Exception as _strand_exc:
                                logger.warning(
                                    f"[EVAL TIMEOUT strand] Task {str(task.id)[:8]} "
                                    f"PR #{_strand_pr} lookup failed: "
                                    f"{type(_strand_exc).__name__}: {_strand_exc} — "
                                    f"leaving EVALUATING for next sweep"
                                )
                            if _s_lookup_ok and _s_state == "closed" and _s_merged:
                                from sqlalchemy.orm.attributes import flag_modified

                                task.status = TaskStatus.COMPLETED
                                task.latest_status_message = (
                                    f"Strand recovery: conflict/CI-fix PR "
                                    f"#{_strand_pr} merged out-of-band"
                                )
                                # The APPROVED original's work landed on the
                                # integration branch via its conflict/CI-fix
                                # child's merged PR, so stamp the canonical
                                # integration-landing signal (preserves the
                                # prior bare ``pr_merged=True`` analytics
                                # meaning while routing through the helper).
                                task.context = dict(ctx)
                                mark_landed_on_integration(task)
                                flag_modified(task, "context")
                                session.add(task)
                                await session.commit()
                                logger.info(
                                    f"[EVAL TIMEOUT strand] Task {str(task.id)[:8]} "
                                    f"terminalized COMPLETED — PR #{_strand_pr} merged"
                                )
                                await services.broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(task.id),
                                        "status": TaskStatus.COMPLETED,
                                    },
                                )
                                continue
                            if _s_lookup_ok and _s_state == "closed" and not _s_merged:
                                # PR closed without merging — terminal, but route
                                # through FIX A's non-rejection classifier so a
                                # cleanup/supersession close lands CANCELLED and a
                                # genuine rejection lands FAILED.
                                from app.rollup_management import (
                                    _pr_closed_is_non_rejection,
                                )
                                from sqlalchemy.orm.attributes import flag_modified
                                from app.failure_classification import (
                                    SCOPE_EMPTY,
                                    classify_failure,
                                    persist_failure_category,
                                )

                                _non_rej = False
                                with contextlib.suppress(Exception):
                                    _non_rej = await _pr_closed_is_non_rejection(
                                        task, _pr, _strand_pr, session
                                    )
                                if _non_rej:
                                    task.status = TaskStatus.CANCELLED
                                    task.latest_status_message = (
                                        f"Strand recovery: PR #{_strand_pr} closed "
                                        f"without merging (no rejection — cleanup)"
                                    )
                                    task.context = {
                                        **ctx,
                                        "pr_closed_unmerged": True,
                                        "failure_detail": "pr_closed_non_rejection",
                                        "failure_category": SCOPE_EMPTY,
                                    }
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
                                    continue
                                task.status = TaskStatus.FAILED
                                task.latest_status_message = (
                                    f"Strand recovery: PR #{_strand_pr} closed "
                                    f"without merging"
                                )
                                task.context = {**ctx, "pr_closed_unmerged": True}
                                _sfc = classify_failure(task)
                                persist_failure_category(task, _sfc)
                                flag_modified(task, "context")
                                session.add(task)
                                await _maybe_create_failure_alert(
                                    task,
                                    session,
                                    f"Strand recovery: PR #{_strand_pr} closed "
                                    f"without merging",
                                )
                                await session.commit()
                                await services.broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(task.id),
                                        "status": TaskStatus.FAILED,
                                    },
                                )
                                continue
                            # STILL OPEN (or lookup failed) → leave EVALUATING.
                            # GUARDRAIL: never complete a task whose PR is open.

                # ROLLUP-PARENT LOST-MARKER RECOVERY. When a rollup parent's
                # rollup_pr_number marker was not persisted (crash/reap window
                # between GitHub PR creation and the DB write), the parent stays
                # EVALUATING forever — every convergence path is keyed on the
                # marker.  Worse, the rollup machinery keeps re-commissioning
                # child tasks against the now-deleted staging branch, burning VMs
                # in a BRANCH_GONE loop.
                #
                # Detection: EVALUATING task has a staging branch in context
                # (plan_branch / research_branch) but no rollup_pr_number and no
                # pr_merged.  We query GitHub for closed PRs from that head
                # branch; if one is merged we converge the parent.
                #
                # GUARDRAIL: only acts when GitHub confirms a merged PR.  If the
                # lookup fails or the PR is still open (state=closed returns []),
                # fall through unchanged and re-check after _LM_RECHECK_INTERVAL_S
                # so the sweep catches the PR merging later.
                _LM_RECHECK_INTERVAL_S = 1800  # 30 minutes between re-checks
                _stg_branch = ctx.get("plan_branch") or ctx.get("research_branch")
                _lm_last_checked_at = ctx.get("_lost_marker_checked_at")
                _lm_recheck_due = not _lm_last_checked_at or (
                    (
                        datetime.utcnow()
                        - datetime.fromisoformat(_lm_last_checked_at)
                    ).total_seconds()
                    > _LM_RECHECK_INTERVAL_S
                )
                if (
                    _stg_branch
                    and not ctx.get("rollup_pr_number")
                    and not ctx.get("pr_merged")
                    and _lm_recheck_due
                ):
                    _lm_merged = False
                    _lm_pr_data: dict | None = None
                    _lm_lookup_ok = False
                    try:
                        from app.routers.agent import get_github_token_for_task
                        from app.github import get_pull_requests_for_head_branch

                        _lm_token, _lm_owner, _lm_repo = await services._bounded(
                            get_github_token_for_task(task.id, session),
                            services._GH_CALL_TIMEOUT_S,
                            what=f"lost-marker token {str(task.id)[:8]}",
                        )
                        _target_base = ctx.get("parent_base") or ctx.get(
                            "original_base"
                        )
                        _lm_prs = await services._bounded(
                            get_pull_requests_for_head_branch(
                                _lm_token,
                                _lm_owner,
                                _lm_repo,
                                _stg_branch,
                                base=_target_base,
                            ),
                            services._GH_CALL_TIMEOUT_S,
                            what=f"lost-marker list-prs {_stg_branch!r}",
                        )
                        # Fallback: if the base-filtered query returned nothing and
                        # we had a base filter, retry without it — the actual base
                        # ref may differ from what's in context.
                        if not _lm_prs and _target_base:
                            _lm_prs = await services._bounded(
                                get_pull_requests_for_head_branch(
                                    _lm_token,
                                    _lm_owner,
                                    _lm_repo,
                                    _stg_branch,
                                    base=None,
                                ),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"lost-marker list-prs unfiltered {_stg_branch!r}",
                            )
                        _lm_lookup_ok = True
                        for _pr in _lm_prs or []:
                            if _pr.get("merged_at") or bool(_pr.get("merged")):
                                _lm_merged = True
                                _lm_pr_data = _pr
                                break
                    except Exception as _lm_exc:
                        logger.warning(
                            f"[EVAL TIMEOUT lost-marker] Task {str(task.id)[:8]} "
                            f"branch {_stg_branch!r} lookup failed: "
                            f"{type(_lm_exc).__name__}: {_lm_exc} — leaving EVALUATING"
                        )

                    if _lm_lookup_ok and not _lm_merged:
                        # No merged PR found (PR still open, not yet created, or
                        # closed-unmerged).  Stamp a timestamp so we back off and
                        # re-check after _LM_RECHECK_INTERVAL_S rather than
                        # querying GitHub on every sweep cycle.
                        from sqlalchemy.orm.attributes import flag_modified

                        task.context = {
                            **ctx,
                            "_lost_marker_checked_at": datetime.utcnow().isoformat(),
                        }
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        ctx = task.context
                    elif _lm_lookup_ok and _lm_merged and _lm_pr_data is not None:
                        # Confirmed: rollup PR was merged.  Converge the parent.
                        from sqlalchemy.orm.attributes import flag_modified
                        from app.rollup_state import _clear_rollup_transient_state
                        from app.models import Project

                        _lm_pr_num = int(_lm_pr_data.get("number") or 0) or None
                        _lm_pr_url = _lm_pr_data.get("html_url") or None
                        _lm_base_ref = (_lm_pr_data.get("base") or {}).get(
                            "ref"
                        ) or None

                        _lm_proj = await session.get(Project, task.project_id)
                        (
                            _lm_record_merged,
                            _lm_terminal,
                            _lm_landed,
                        ) = await classify_rollup_merge_outcome(
                            task,
                            _lm_proj,
                            session,
                            pr_number=_lm_pr_num,
                            merged_base_branch=_lm_base_ref,
                            source="recovery_sweeps.lost_marker",
                        )
                        _lm_new_ctx = _clear_rollup_transient_state({**ctx})
                        if _lm_pr_num:
                            _lm_new_ctx["rollup_pr_number"] = _lm_pr_num
                        if _lm_pr_url:
                            _lm_new_ctx["rollup_pr_url"] = _lm_pr_url
                        task.context = _lm_new_ctx
                        if _lm_record_merged:
                            mark_landed_on_integration(task)
                        flag_modified(task, "context")
                        task.status = (
                            TaskStatus.COMPLETED
                            if _lm_terminal
                            else TaskStatus.EVALUATING
                        )
                        task.vm_instance_id = None
                        task.last_heartbeat = None
                        if _lm_terminal:
                            task.latest_status_message = (
                                f"✓ rollup PR #{_lm_pr_num} already merged to "
                                f"{_lm_landed}; converged from stranded EVALUATING"
                            )
                        else:
                            task.latest_status_message = (
                                f"Lost-marker recovery: rollup PR #{_lm_pr_num} "
                                f"merged into {_lm_landed}; awaiting downstream landing"
                            )
                        session.add(task)
                        await session.commit()
                        logger.info(
                            f"[EVAL TIMEOUT lost-marker] Task {str(task.id)[:8]} "
                            f"converged → {task.status} (rollup PR #{_lm_pr_num} "
                            f"merged into {_lm_landed})"
                        )
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {
                                "task_id": str(task.id),
                                "status": task.status,
                            },
                        )
                        continue

                # Approval-gate hard timeout. Parents with requires_approval=True
                # remain in EVALUATING waiting for human approval; the
                # evaluating_fail_hours backstop can be deferred by an active
                # pipeline chain, so approval-gated parents need their own
                # companion timeout.
                #
                # TWO disposition paths, keyed on context.approval_hold_reason:
                #  - DELIBERATE spend/budget hold (== "spend_budget"): a longer,
                #    operator-configurable window resolving to a CLEAN NON-FAILURE
                #    CANCELLED terminal (a cost decision an operator has not
                #    reached is not a pipeline failure). NEVER records a failed
                #    task result / failure category / failure alert.
                #  - Everything else (no marker / review-escalation churn): the
                #    EXACT current 2h → FAILED behavior, unchanged.
                if task.requires_approval:
                    from sqlalchemy.orm.attributes import flag_modified

                    is_spend_hold = (
                        ctx.get("approval_hold_reason")
                        == SPEND_BUDGET_APPROVAL_HOLD_REASON
                    )

                    approval_set_at = ctx.get("_approval_set_at")
                    if not approval_set_at:
                        new_ctx = {**ctx, "_approval_set_at": now.isoformat()}
                        # MAKE THE HOLD LOUD: when a spend/budget hold is first
                        # seen, fire the created notification + a non-failure
                        # WARNING alert, exactly once (guarded by the flag).
                        if is_spend_hold and not ctx.get(
                            "_spend_hold_created_notified"
                        ):
                            await _emit_spend_hold_created_signals(
                                task, session, window_hours=_spend_hours
                            )
                            new_ctx["_spend_hold_created_notified"] = True
                        task.context = new_ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        continue
                    try:
                        approval_since = datetime.fromisoformat(approval_set_at)
                        approval_elapsed = (now - approval_since).total_seconds()
                    except (ValueError, TypeError):
                        approval_elapsed = 0.0

                    if is_spend_hold:
                        # Fire the EXPIRING warning once, within the lead window
                        # (last 20% of the configured window, min 1h) before the
                        # hold expires — so the operator gets an active nudge.
                        warn_lead = max(SPEND_APPROVAL_FAIL_SECONDS * 0.2, 3600.0)
                        if (
                            not ctx.get("_spend_hold_expiring_notified")
                            and SPEND_APPROVAL_FAIL_SECONDS - warn_lead
                            <= approval_elapsed
                            < SPEND_APPROVAL_FAIL_SECONDS
                        ):
                            hours_remaining = round(
                                (SPEND_APPROVAL_FAIL_SECONDS - approval_elapsed) / 3600,
                                1,
                            )
                            _dispatch_spend_hold_notification(
                                task,
                                "SPEND_HOLD_EXPIRING",
                                _spend_hours,
                                hours_remaining=hours_remaining,
                            )
                            task.context = {
                                **ctx,
                                "_spend_hold_expiring_notified": True,
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            await session.commit()
                            continue
                        if approval_elapsed > SPEND_APPROVAL_FAIL_SECONDS:
                            hours = round(approval_elapsed / 3600, 1)
                            logger.info(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} "
                                f"spend/budget approval not granted in {hours}h — "
                                f"declining (CANCELLED, not a failure)"
                            )
                            task.status = TaskStatus.CANCELLED
                            task.latest_status_message = (
                                "Spend/budget approval not granted in time — "
                                "declined (not a failure)"
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
                            continue
                        # Within window, not yet at the warning lead — leave
                        # EVALUATING for a later sweep. A spend/budget hold never
                        # falls through to the review-escalation FAILED path.
                        continue

                    if approval_elapsed > APPROVAL_GATE_FAIL_SECONDS:
                        # Defer (bounded) while the chain is still actively
                        # working the PR: live review/fix children, or a fix
                        # parked in CI-wait. The two other backstops in this
                        # sweep already consult the chain before terminalizing;
                        # the approval gate was the one that did not.
                        if (
                            approval_elapsed
                            <= APPROVAL_GATE_FAIL_SECONDS
                            + APPROVAL_GATE_DEFER_WINDOW_HOURS * 3600
                        ):
                            _approval_chain_busy = False
                            try:
                                _approval_chain_busy = (
                                    await services._chain_has_active_pipeline_work(
                                        task, session
                                    )
                                    or await _approval_gate_chain_busy(task, session)
                                )
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
                                continue
                        hours = round(approval_elapsed / 3600, 1)
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
                        await _maybe_create_failure_alert(
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
                        continue

                ci_waiting = (
                    ctx.get("ci_waiting") is True
                    or ctx.get("review_loop", {}).get("ci_waiting") is True
                )
                if ci_waiting:
                    # CI-waiting tasks have their own 20-min escalation in orchestrator.py.
                    # Apply a 30-minute absolute backstop so tasks can never
                    # hang forever regardless of field state.
                    #
                    # Time base: prefer review_loop.ci_wait_started (when CI wait
                    # actually began) over the last EVALUATING transition. A task
                    # may sit in EVALUATING for a long time waiting on subtasks
                    # before the rollup PR is opened and CI polling starts — using
                    # the EVALUATING transition would false-positive the backstop
                    # the instant CI finally begins.
                    rl = ctx.get("review_loop", {})
                    ci_ws_val = rl.get("ci_wait_started")
                    was_escalated = rl.get("escalated", False)
                    evaluating_since = None
                    ci_ws_valid = False
                    if ci_ws_val is not None:
                        try:
                            evaluating_since = datetime.fromisoformat(ci_ws_val)
                            ci_ws_valid = True
                        except (ValueError, TypeError):
                            evaluating_since = None
                    if evaluating_since is None:
                        for entry in reversed(task.status_log or []):
                            msg = entry.get("msg", "")
                            if "→ EVALUATING" in msg:
                                with contextlib.suppress(
                                    KeyError, ValueError, TypeError
                                ):
                                    evaluating_since = datetime.fromisoformat(
                                        entry["t"]
                                    )
                                break
                    if evaluating_since is None:
                        evaluating_since = task.started_at or task.created_at

                    # Pause-aware elapsed: subtract any pause windows that
                    # occurred since the timer started. Legacy timers without
                    # a stashed baseline fall back to raw elapsed.
                    _ci_baseline = rl.get("ci_wait_pause_baseline")
                    elapsed = (
                        pause_clock.active_elapsed(
                            _deploy_state,
                            evaluating_since,
                            _ci_baseline,
                        )
                        if _deploy_state
                        else (now - evaluating_since).total_seconds()
                    )
                    # Early trigger: if CI has passed but review_approved is
                    # missing AND the review child task is already terminal
                    # (FAILED/CANCELLED), the review VM died silently — we
                    # have enough signal to recover now without waiting for
                    # the 30-min backstop. The elapsed guard remains as the
                    # fallback for cases where review status hasn't become
                    # terminal yet.
                    rl_ci_passed = bool(rl.get("ci_passed"))
                    rl_review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    rl_recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    early_recovery_eligible = False
                    if (
                        rl_ci_passed
                        and not rl_review_approved
                        and not rl_recovery_attempted
                    ):
                        _early_summary = await _summarize_latest_review_for_chain(
                            task, session
                        )
                        if _early_summary and _early_summary["status"] in (
                            "FAILED",
                            "CANCELLED",
                        ):
                            early_recovery_eligible = True
                    if elapsed <= CI_BACKSTOP_SECONDS and not early_recovery_eligible:
                        # Under backstop threshold — let normal CI timeout handle it
                        continue
                    # Pipeline still actively working on the chain (review/fix
                    # task heartbeated within 10min) — defer to next sweep so
                    # we don't kill legitimate in-flight iteration cycles.
                    if await services._chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop but chain "
                            f"has fresh review/fix heartbeat — skipping kill"
                        )
                        continue

                    # Gap C: verify REAL CI state before declaring a timeout.
                    # The backstop fires on a wall-clock that can outlast a CI
                    # conclusion that actually landed before the wait window even
                    # started (incident 2026-06-07: review_loop.ci_wait_started
                    # was re-armed AFTER CI had concluded, so the backstop "waited
                    # 1h for a conclusion that was already in"). Before failing a
                    # parent whose PR still has a head_sha to check, read the PR's
                    # live check-run conclusion and route on the truth:
                    #   concluded FAILURE → existing CI-failure handling
                    #       (handle_ci_completion fix-spawn path / ci_passed=False)
                    #   concluded SUCCESS → record the pass + clear the wait
                    #   pending / GitHub error → fall through to the timeout
                    #       (fail-OPEN: never kill, but never block on an outage).
                    # ALL GitHub I/O is strictly OUTSIDE the FOR-UPDATE window
                    # below — handle_ci_completion re-locks the row itself.
                    _ci_head_sha = rl.get("head_sha")
                    if _ci_head_sha and rl.get("pr_number"):
                        _ci_resolved = await _verify_ci_before_backstop(
                            task, rl, _ci_head_sha, session
                        )
                        if _ci_resolved:
                            # CI had actually concluded — handled (fix-spawn /
                            # pass recorded / wait cleared). Skip the kill.
                            continue

                    # Gap A: recoverable / freshly-requeuing work guard. Mirror
                    # the 6h hard-timeout path: a child / chain pipeline task that
                    # is FAILED with an infra-class category (resurrection-sweep
                    # or operator-requeue eligible) OR is QUEUED with optimistic-
                    # retry budget still left must NOT be cascade-cancelled while
                    # it can still come back. Defer for a bounded window so a truly
                    # stuck case still terminates, and respect the optimistic-retry
                    # cap so an exhausted child stops deferring the backstop.
                    _has_recoverable = await _chain_has_recoverable_failed_work(
                        task, session
                    )
                    _has_retrying = await _chain_has_unexhausted_optimistic_retry_work(
                        task, session
                    )
                    if _has_recoverable or _has_retrying:
                        from sqlalchemy.orm.attributes import flag_modified

                        _ci_first_defer_at = rl.get("_ci_backstop_first_defer_at")
                        # A defer marker older than this CI-wait episode is stale
                        # (left from a prior ci_wait_started) — discard it so the
                        # window measures from the current wait.
                        if _ci_first_defer_at and ci_ws_valid and evaluating_since:
                            try:
                                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                                if _cfd < evaluating_since:
                                    _ci_first_defer_at = None
                            except (ValueError, TypeError):
                                _ci_first_defer_at = None
                        if _ci_first_defer_at:
                            try:
                                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                                _ci_defer_elapsed = (now - _cfd).total_seconds()
                            except (ValueError, TypeError):
                                _ci_defer_elapsed = 0.0
                            if (
                                _ci_defer_elapsed
                                <= CI_BACKSTOP_DEFER_WINDOW_HOURS * 3600
                            ):
                                logger.info(
                                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI "
                                    f"backstop but chain has recoverable/requeuing work "
                                    f"— deferring ({round(_ci_defer_elapsed / 3600, 1)}h "
                                    f"into {CI_BACKSTOP_DEFER_WINDOW_HOURS}h window)"
                                )
                                continue
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                                f"{CI_BACKSTOP_DEFER_WINDOW_HOURS}h CI-backstop defer "
                                f"window despite recoverable work — proceeding to kill"
                            )
                            # fall through to existing kill
                        else:
                            new_rl = dict(rl)
                            new_rl["_ci_backstop_first_defer_at"] = now.isoformat()
                            task.context = {
                                **(task.context or {}),
                                "review_loop": new_rl,
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop "
                                f"with recoverable/requeuing chain work — deferring "
                                f"cascade-cancel for up to "
                                f"{CI_BACKSTOP_DEFER_WINDOW_HOURS}h"
                            )
                            await session.commit()
                            continue

                    hours = round(elapsed / 3600, 1)
                    ci_passed = bool(rl.get("ci_passed"))
                    review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    review_summary = await _summarize_latest_review_for_chain(
                        task, session
                    )

                    # Compose an honest reason. The historic "CI polling stalled after
                    # escalation" was a lie when the actual blocker was a missing review
                    # callback (review VM died silently) — name the gate that is open.
                    reason = _ci_backstop_timeout_reason(
                        ci_ws_val=ci_ws_val,
                        ci_ws_valid=ci_ws_valid,
                        ci_passed=ci_passed,
                        review_approved=review_approved,
                        was_escalated=was_escalated,
                        review_summary=review_summary,
                        review_loop=rl,
                    )

                    # One-shot recovery: if CI passed but the review-approved gate never
                    # closed (common when the review VM died before POSTing status),
                    # spawn a fresh review and reset the CI-wait clock instead of hard
                    # killing. The dedup guard will supersede any stale corpse.
                    if ci_passed and not review_approved and not recovery_attempted:
                        recovered = await _attempt_ci_backstop_recovery(
                            task, session, rl
                        )
                        if recovered:
                            from sqlalchemy.orm.attributes import flag_modified

                            new_rl = dict(rl)
                            new_rl["review_recovery_attempted"] = True
                            new_rl["review_recovery_at"] = _utcnow().isoformat()
                            new_rl["ci_wait_started"] = _utcnow().isoformat()
                            new_rl["ci_wait_pause_baseline"] = (
                                pause_clock.total_paused_seconds(_deploy_state)
                                if _deploy_state
                                else 0.0
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": new_rl,
                            }
                            flag_modified(task, "context")
                            task.latest_status_message = (
                                f"CI passed on PR #{new_rl.get('pr_number')} but review never "
                                f"completed ({reason}) — spawned fresh review, backstop reset"
                            )
                            session.add(task)
                            await session.commit()
                            # Recovery path keeps the task EVALUATING (reset CI-wait
                            # clock). _attempt_ci_backstop_recovery above can poison/
                            # roll back the session and expire `task`, so use the
                            # literal rather than a post-commit lazy-load.
                            await services.broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {
                                    "task_id": str(task.id),
                                    "status": TaskStatus.EVALUATING,
                                },
                            )
                            logger.warning(
                                f"[EVAL TIMEOUT] Backstop recovery for task "
                                f"{str(task.id)[:8]}: spawned fresh review on "
                                f"PR #{rl.get('pr_number')} ({reason})"
                            )
                            continue

                    # C3 lost-update guard: this backstop branch reached the
                    # FAILED decision from a NON-locked read of `task`, after
                    # several awaits (_chain_has_active_pipeline_work,
                    # _summarize_latest_review_for_chain, _attempt_ci_backstop_
                    # recovery). This sweep now runs in its OWN background task
                    # (_evaluating_timeout_service, lifecycle.py) and can
                    # interleave with the orchestrator's CI-poll loop
                    # (orchestrator.py:2994), which locks the SAME row FOR UPDATE
                    # and may resolve it (e.g. COMPLETED on merge) in the window
                    # between our read and this commit. Acquire the row lock and
                    # RE-VALIDATE under it before clobbering: if the CI loop holds
                    # the lock (skip_locked → None) or already moved the row out
                    # of EVALUATING, or the timeout no longer holds on the fresh
                    # context, defer to the next sweep instead of overwriting a
                    # state the CI loop just committed. Locking the single row at
                    # the decision point adds no serial coupling across rows.
                    from app.orchestrator import _lock_task_for_mutation

                    locked_task = await _lock_task_for_mutation(
                        session,
                        task.id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="eval-timeout CI backstop FAIL",
                    )
                    if locked_task is None:
                        # Row is locked by the CI-poll loop, or no longer
                        # EVALUATING (already resolved). _lock_task_for_mutation
                        # has already committed/rolled back to release. Skip.
                        continue
                    # populate_existing=True refreshed `locked_task` from the DB,
                    # so re-derive the CI-wait timer + active-pipeline guard from
                    # the *current* context and confirm the backstop still fires.
                    _rl_fresh = (locked_task.context or {}).get("review_loop", {}) or {}
                    if not (
                        _rl_fresh.get("ci_waiting") is True
                        or (locked_task.context or {}).get("ci_waiting") is True
                    ):
                        # CI loop cleared ci_waiting (e.g. merged/closed) between
                        # our read and the lock — no longer our row to fail.
                        await session.rollback()
                        continue
                    _ci_ws_fresh = _rl_fresh.get("ci_wait_started")
                    _since_fresh = None
                    if _ci_ws_fresh is not None:
                        with contextlib.suppress(ValueError, TypeError):
                            _since_fresh = datetime.fromisoformat(_ci_ws_fresh)
                    if _since_fresh is None:
                        _since_fresh = evaluating_since
                    _baseline_fresh = _rl_fresh.get("ci_wait_pause_baseline")
                    _elapsed_fresh = (
                        pause_clock.active_elapsed(
                            _deploy_state, _since_fresh, _baseline_fresh
                        )
                        if _deploy_state
                        else (now - _since_fresh).total_seconds()
                    )
                    if (
                        _elapsed_fresh <= CI_BACKSTOP_SECONDS
                        and not early_recovery_eligible
                    ):
                        # CI loop reset the wait clock (e.g. recovery / fresh
                        # poll) in the unlocked window — backstop no longer due.
                        await session.rollback()
                        continue
                    # Re-validated under the FOR UPDATE lock: still EVALUATING,
                    # still CI-waiting, still past the backstop. Safe to fail.
                    task = locked_task
                    logger.error(
                        f"[EVAL TIMEOUT] CI-waiting task {str(task.id)[:8]} has been "
                        f"EVALUATING for {hours}h — backstop timeout ({reason}), marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"CI wait backstop timeout after {hours}h ({reason})"
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
                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and this branch reads + mutates the live
                    # rows (cancel, latest_status_message, vm_instance_id).
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    from app.vm_lifecycle import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    child_cancel_msg = f"Cancelled: parent CI wait backstop timeout after {hours}h ({reason})"
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = child_cancel_msg
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"CI wait backstop timeout after {hours}h — {reason}",
                    )
                    await session.commit()
                    # task.status=FAILED / children=CANCELLED were set above; the
                    # alert + commit can poison/expire these instances, so broadcast
                    # the known literals rather than post-commit lazy-loads. child.id
                    # (PK) is always safe to read.
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.FAILED},
                    )
                    for child in non_terminal_children:
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": TaskStatus.CANCELLED},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="ci_backstop_timeout")
                    continue

                # Determine how long the task has been EVALUATING
                evaluating_since = None
                # Walk status_log backwards for the last EVALUATING transition
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        break
                if evaluating_since is None:
                    # Fallback to started_at or created_at
                    evaluating_since = task.started_at or task.created_at

                elapsed = (now - evaluating_since).total_seconds()

                # Child statuses snapshotted at cycle-read time (plain tuples,
                # Phase A). Kept as plain data so they survive across the
                # per-row sessions; the rare branches that MUTATE children
                # re-fetch them fresh on this row's session.
                child_status_snap = _eval_child_status_by_parent.get(task.id, [])

                all_children_terminal = bool(child_status_snap) and all(
                    c_status in TERMINAL for _, c_status in child_status_snap
                )

                # Recovery for missed rollup: all children done but parent still EVALUATING
                if all_children_terminal:
                    logger.warning(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has all children terminal "
                        f"but is still EVALUATING — triggering missed rollup"
                    )
                    # Re-fetch a terminal child fresh to trigger rollup.
                    trigger_child = await session.get(Task, child_status_snap[0][0])
                    if trigger_child is not None:
                        # C3: bound the rollup trigger — _check_and_trigger_rollup
                        # does serial GitHub I/O (PR verify/create/merge) per
                        # child. A hung call here would otherwise pin the whole
                        # sweep.
                        #
                        # Audit P1: this _bounded() wraps a coroutine that does
                        # DB I/O on ``session`` — now this ROW's own short-lived
                        # session. An asyncio.wait_for cancellation mid-DB-op
                        # that leaves it in PendingRollback is confined to this
                        # iteration: the explicit rollback below clears it for
                        # the rest of this row, and the per-row ``async with``
                        # closes it before the next row gets a clean session.
                        try:
                            await services._bounded(
                                # C3: thread the caller's own heartbeat so the
                                # per-child PR-verify loop pumps the background
                                # task's liveness marker, not the orchestrator's.
                                _check_and_trigger_rollup(
                                    trigger_child, session, progress_cb=_heartbeat
                                ),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"eval-timeout rollup {str(task.id)[:8]}",
                            )
                        except asyncio.TimeoutError:
                            with contextlib.suppress(Exception):
                                await session.rollback()
                            continue
                    await session.commit()
                    continue

                # 6-hour hard timeout
                if elapsed > EVALUATING_FAIL_SECONDS:
                    # Same protection as the CI backstop: if the chain has an
                    # actively heartbeating review/fix task, the pipeline is
                    # still doing real work — defer to next sweep.
                    if await services._chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                            f"{EVALUATING_FAIL_SECONDS // 3600}h hard timeout but "
                            f"chain has fresh review/fix heartbeat — skipping kill"
                        )
                        continue

                    # Recoverable-FAILED defer: a child / chain pipeline task in
                    # FAILED with infra-class failure_category may come back via
                    # resurrection sweep or operator requeue. Cascade-killing
                    # mid-recovery destroys salvageable work (incident
                    # 2026-04-29: PRs #3450/#3408 + parents c30701d3/ddd987fc).
                    # Defer for a bounded window so a truly stuck case still
                    # terminates eventually.
                    EVAL_DEFER_WINDOW_HOURS = 24
                    first_defer_at = ctx.get("_eval_timeout_first_defer_at")
                    # Treat a defer marker older than evaluating_since as stale
                    # (left over from a previous EVALUATING transition).
                    if first_defer_at:
                        try:
                            _fd = datetime.fromisoformat(first_defer_at)
                            if _fd < evaluating_since:
                                first_defer_at = None
                        except (ValueError, TypeError):
                            first_defer_at = None
                    has_recoverable = await _chain_has_recoverable_failed_work(
                        task, session
                    )
                    if has_recoverable:
                        if first_defer_at:
                            try:
                                _fd = datetime.fromisoformat(first_defer_at)
                                defer_elapsed = (now - _fd).total_seconds()
                            except (ValueError, TypeError):
                                defer_elapsed = 0.0
                            if defer_elapsed <= EVAL_DEFER_WINDOW_HOURS * 3600:
                                logger.info(
                                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                    f"{EVALUATING_FAIL_SECONDS // 3600}h but chain "
                                    f"has recoverable FAILED children — deferring "
                                    f"({round(defer_elapsed / 3600, 1)}h into "
                                    f"{EVAL_DEFER_WINDOW_HOURS}h window)"
                                )
                                continue
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                                f"{EVAL_DEFER_WINDOW_HOURS}h defer window despite recoverable "
                                f"FAILED children — proceeding to kill"
                            )
                            # fall through to existing kill
                        else:
                            from sqlalchemy.orm.attributes import flag_modified

                            task.context = {
                                **ctx,
                                "_eval_timeout_first_defer_at": now.isoformat(),
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                f"{EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable FAILED children — deferring cascade-cancel "
                                f"for up to {EVAL_DEFER_WINDOW_HOURS}h"
                            )
                            await _maybe_create_failure_alert(
                                task,
                                session,
                                f"Evaluation past {EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable infra failures in chain — deferring "
                                f"cascade-cancel for up to {EVAL_DEFER_WINDOW_HOURS}h",
                            )
                            await session.commit()
                            continue

                    hours = round(elapsed / 3600, 1)
                    logger.error(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                        f"for {hours}h — hard timeout, marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Evaluation timeout — children did not complete "
                        f"within {EVALUATING_FAIL_SECONDS // 3600} hours"
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

                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and the cascade-cancel + PR-close below
                    # read and mutate the live rows.
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )

                    # Cascade-cancel non-terminal children
                    from app.vm_lifecycle import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    hard_timeout_cancel_msg = (
                        f"Cancelled: parent evaluation timeout after {hours}h"
                    )
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = hard_timeout_cancel_msg
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    if non_terminal_children:
                        logger.info(
                            f"[EVAL TIMEOUT] Cascade-cancelled {len(non_terminal_children)} "
                            f"children of timed-out task {str(task.id)[:8]}"
                        )

                    # Close orphaned rollup PRs from CI-waiting children
                    from app.github import close_pull_request

                    for child in children:
                        child_pr = (
                            (child.context or {})
                            .get("review_loop", {})
                            .get("pr_number")
                        )
                        if child_pr and not (child.context or {}).get("pr_merged"):
                            try:
                                from app.routers.agent import get_github_token_for_task

                                _gh, _owner, _repo = await services._bounded(
                                    get_github_token_for_task(child.id, session),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"eval-timeout token {str(child.id)[:8]}",
                                )
                                await services._bounded(
                                    close_pull_request(_gh, _owner, _repo, child_pr),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"eval-timeout close PR #{child_pr}",
                                )
                                logger.info(
                                    f"[EVAL TIMEOUT] Closed orphaned PR #{child_pr} from child {str(child.id)[:8]}"
                                )
                            except Exception as pr_err:
                                logger.warning(
                                    f"[EVAL TIMEOUT] Failed to close PR #{child_pr}: {pr_err}"
                                )

                    # Create ERROR alert (deduplicated by _maybe_create_failure_alert)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"Evaluation timeout after {hours}h — children did not complete",
                    )
                    await session.commit()
                    # task.status=FAILED / children=CANCELLED were set above; the
                    # PR-close calls + alert + commit can poison/expire these
                    # instances, so broadcast the known literals rather than
                    # post-commit lazy-loads. child.id (PK) is always safe to read.
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.FAILED},
                    )
                    for child in non_terminal_children:
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": TaskStatus.CANCELLED},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="parent_eval_timeout")
                    continue

                # 2-hour warning
                if elapsed > EVALUATING_WARN_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    # Deduplicate: skip if an undismissed alert exists OR one was
                    # dismissed within the last 2 hours (prevents re-firing after dismiss).
                    dismiss_cooldown = _utcnow() - timedelta(hours=2)
                    existing = (
                        (
                            await session.execute(
                                select(Alert).where(
                                    Alert.task_id == task.id,
                                    or_(
                                        Alert.dismissed_at.is_(None),
                                        Alert.dismissed_at >= dismiss_cooldown,
                                    ),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing is None:
                        logger.warning(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                            f"for {hours}h — children may be stuck"
                        )
                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.WARNING,
                            title=f"Task {task.title or str(task.id)[:8]} stuck evaluating",
                            message=(
                                f"Task has been evaluating for {hours} hours "
                                f"— children may be stuck"
                            ),
                            task_id=task.id,
                        )
                        session.add(alert)
                        await session.commit()
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "alert_created",
                            {
                                "alert_id": str(alert.id),
                                "severity": "WARNING",
                                "title": alert.title,
                            },
                        )
    except Exception as e:
        logger.error(
            f"[EVAL TIMEOUT] Evaluating timeout sweep failed: {e}", exc_info=True
        )
