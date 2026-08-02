"""Chain-inspection / CI-backstop / spend-hold helpers for the EVALUATING sweep.

Extracted from :mod:`app.recovery_evaluating` to keep that module below its
size cap. Holds the standalone helpers that the bounded EVALUATING-timeout
sweep (``_check_evaluating_timeouts``) leans on: the chain-activity inspectors
(``_chain_has_active_pipeline_work`` and friends), the CI-backstop reason
picker and verifier, the long-queued-pipeline superseder, the spend-hold
signal emitters, and the best-effort direction-outcome healer.

Every public name here is re-exported from :mod:`app.recovery_evaluating`
(and in turn from ``app.recovery_sweeps`` / ``app.services``) so existing
``from app.services import ...`` call sites and test patch targets keep
resolving unchanged. Shared bounding infrastructure lives in ``app.services``
and is referenced through the ``services`` module object so test patches
against ``app.services.<name>`` still take effect.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from sqlmodel import select

import app.shared_state as _shared_state
from app.models import (
    Task,
    TaskStatus,
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
    parent_ctx: dict | None = None,
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
        # Rollup parents write merge-gate state to parent.context directly
        # (_merge_deferred_*), not to review_loop. Surface those signals when
        # review_loop has no merge diagnostics so the backstop message names
        # the actual blocker instead of always showing "merge_attempts=0".
        if (
            parent_ctx is not None
            and mergeable_state is None
            and merge_in_progress_resets == 0
        ):
            _deferred_state = parent_ctx.get("_merge_deferred_state")
            _deferred_count = parent_ctx.get("_merge_deferred_count", 0)
            _total_deferrals = parent_ctx.get("_merge_total_deferrals", 0)
            if _deferred_state:
                return (
                    f"CI green and review approved but rollup merge deferred: "
                    f"mergeable_state={_deferred_state}, "
                    f"deferred_count={_deferred_count}, "
                    f"total_deferrals={_total_deferrals}"
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
    from app.failure_classification import ADMIN_CANCEL, persist_failure_category

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
        persist_failure_category(t, ADMIN_CANCEL)
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
    parent: Task, review_loop: dict, head_sha: str | None, session
) -> bool:
    """Read the PR's LIVE CI verdict before the backstop fails a parent.

    The CI-wait backstop fires on a wall-clock that can outlast a CI conclusion
    that actually landed before the wait window even started — e.g. when
    ``review_loop.ci_wait_started`` is re-armed after CI already concluded
    (incident 2026-06-07). It also fired on rollups targeting ``automain`` that
    carry ZERO CI checks and sit ``mergeable=CLEAN``: the old gate required a
    cached ``head_sha`` AND ``review_loop.pr_number``, so a row parked with its
    identity only at ``context.rollup_pr_number`` (or with no cached head) was
    never verified and got hard-killed at the deadline.

    This routes through the SAME strict resolver + no-checks policy the
    orchestrator poll loop uses (``ci_resolution.resolve_pr_ci_status`` +
    ``resolve_no_checks_escalation``), so the verdict here can never disagree
    with what the merger would do:

      - GREEN / RED → delegate to ``handle_ci_completion`` (merge / fix-spawn),
        which re-locks the row and re-enforces the ``_review_approved`` gate;
      - NO_CHECKS resolving to ``merge_unverified`` (staging base / declared
        no-CI / test-less repo) → treat as green so the same handler runs the
        mergeability-gated merge — this is what fast-resolves the 0-check/CLEAN
        rollup the backstop used to kill;
      - NO_CHECKS resolving to ``verify_tests`` / ``keep_polling``, a still
        PENDING build, or ANY GitHub error → return False (fail OPEN: fall
        through to the existing timeout path — never a wrong kill, never block on
        an outage, never merge on absence of signal for a CI-gated repo).

    The PR number is resolved via :func:`effective_pr_number`
    (``review_loop.pr_number`` OR ``context.rollup_pr_number``); ``head_sha`` is
    looked up from the live PR when the parked loop never cached one.

    ALL GitHub I/O is performed here, strictly OUTSIDE the caller's FOR-UPDATE
    re-validate window. ``handle_ci_completion`` re-locks the row itself.

    Returns True iff a terminal verdict was reached and handled (the caller must
    then skip the kill); False to fall through to the existing timeout path.
    """
    from app import ci_resolution, services
    from app.ci_completion import handle_ci_completion
    from app.ci_merge_helpers import effective_pr_number
    from app.github import get_pull_request
    from app.models import Project
    from app.routers.agent import get_github_token_for_task
    from app.shared_state import runtime_config as _rt_cfg

    pr_number = effective_pr_number(parent)
    if not pr_number:
        # No PR to poll — the backstop's wall-clock is the only signal.
        return False

    _pr_data = None
    try:
        _gh, _owner, _repo = await services._bounded(
            get_github_token_for_task(parent.id, session),
            services._GH_CALL_TIMEOUT_S,
            what=f"ci-backstop token {str(parent.id)[:8]}",
        )
        # Resolve head_sha live when the parked loop never cached one — the old
        # hard precondition on a cached head_sha is exactly what starved the
        # identity-incomplete rows of a verify before the kill.
        _head_sha = head_sha or ""
        if not _head_sha:
            _pr_data = await services._bounded(
                get_pull_request(_gh, _owner, _repo, pr_number),
                services._GH_CALL_TIMEOUT_S,
                what=f"ci-backstop PR #{pr_number}",
            )
            _head_sha = (_pr_data.get("head") or {}).get("sha") or ""
        _resolved = await services._bounded(
            ci_resolution.resolve_pr_ci_status(
                pr_number=pr_number,
                branch_sha=_head_sha,
                token=_gh,
                owner=_owner,
                repo=_repo,
                project_id=str(parent.project_id) if parent.project_id else None,
            ),
            services._GH_CALL_TIMEOUT_S,
            what=f"ci-backstop resolve PR #{pr_number}",
        )
    except Exception as e:
        # Fail OPEN: a GitHub error / timeout must never block the backstop —
        # fall through to the existing timeout behaviour.
        logger.warning(
            f"[EVAL TIMEOUT] CI re-verify before backstop failed for task "
            f"{str(parent.id)[:8]} (PR #{pr_number}): {e} — falling through to timeout"
        )
        return False

    _state = _resolved.get("state")
    ci_result: dict | None = None
    if _state == "green":
        ci_result = {
            "status": "completed",
            "conclusion": "success",
            "details": _resolved.get("details", []),
            "failed_logs": "",
        }
    elif _state == "red":
        ci_result = {
            "status": "completed",
            "conclusion": "failure",
            "details": _resolved.get("details", []),
            "failed_logs": _resolved.get("failed_logs", "")
            or "CI checks failed (see workflow run)",
        }
    elif _state == "no_checks":
        # Definitive 0-check state: apply the SAME no-checks policy the poll loop
        # uses. Only a merge_unverified verdict resolves here; verify_tests /
        # keep_polling fall open so the poll loop's own no-checks escalation
        # owns the verify-gate / alert-and-wait path (never merge on absence of
        # signal for a CI-gated repo).
        _base_ref = ""
        if _pr_data is None:
            with contextlib.suppress(Exception):
                _pr_data = await services._bounded(
                    get_pull_request(_gh, _owner, _repo, pr_number),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"ci-backstop PR #{pr_number} base",
                )
        if _pr_data:
            _base_ref = (_pr_data.get("base") or {}).get("ref", "") or ""
        _base_ref = _base_ref or review_loop.get("base_branch", "")
        _proj = (
            await session.get(Project, parent.project_id)
            if parent.project_id
            else None
        )
        _gate_on = _rt_cfg.get("no_ci_test_gate_enabled", True)
        _action = (
            ci_resolution.resolve_no_checks_escalation(
                _proj, _base_ref, gate_enabled=_gate_on
            )
            if _proj is not None
            else "keep_polling"
        )
        if _action == "merge_unverified":
            ci_result = {
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        else:
            return False
    else:
        # pending / unresolved — the backstop's wall-clock is the only signal.
        return False

    # A terminal verdict was reached. Delegate to the shared completion handler,
    # which re-locks the row, records the gate signal, RE-ENFORCES the
    # _review_approved gate, and runs the fix-spawn (failure) or merge (success)
    # path exactly as the orchestrator poll loop does.
    logger.warning(
        f"[EVAL TIMEOUT] Task {str(parent.id)[:8]} CI re-verify: PR #{pr_number} "
        f"resolved to '{_state}' before backstop — routing to CI-completion "
        f"handler instead of failing the parent"
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
            checked_head_sha=_head_sha,
            checked_base_sha=(
                ((_pr_data or {}).get("base") or {}).get("sha") or ""
            ),
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


async def _apply_rollup_merge_classification(
    task: Task,
    proj,
    session,
    *,
    pr_number: int | None,
    merged_base_branch: str | None,
    source: str,
    msg_terminal: str,
    msg_non_terminal: str,
    hotfix_lane: bool | None,
    ctx_override: dict | None = None,
    non_merge_reason: str | None = None,
) -> tuple[bool, bool, str]:
    """Shared classify→status→signal sequence for rollup-merge recovery sites.

    Steps: classify_rollup_merge_outcome → task.status/vm_instance_id/last_heartbeat
    → latest_status_message → conditional landed-signal write.

    ``msg_terminal`` / ``msg_non_terminal`` may contain a ``{landed}`` placeholder
    expanded with the resolved landing branch. ``hotfix_lane=None`` routes through
    ``mark_landed_on_integration``; a bool value routes through
    ``apply_merged_landed_signal``. ``ctx_override`` is staged onto ``task.context``
    before the signal write when supplied (used at site 1 where _ctx2 must be
    committed after the signal).

    Returns (record_merged, terminal, landed_branch).
    """
    from app.branch_resolution import classify_rollup_merge_outcome
    from app.landed_signal import (
        apply_merged_landed_signal,
        has_landed_signal,
        mark_landed_on_integration,
    )

    record_merged, terminal, landed = await classify_rollup_merge_outcome(
        task,
        proj,
        session,
        pr_number=pr_number,
        merged_base_branch=merged_base_branch,
        source=source,
    )
    task.status = TaskStatus.COMPLETED if terminal else TaskStatus.EVALUATING
    task.vm_instance_id = None
    task.last_heartbeat = None
    task.latest_status_message = (
        msg_terminal.format(landed=landed)
        if terminal
        else msg_non_terminal.format(landed=landed)
    )
    if record_merged:
        if ctx_override is not None:
            task.context = ctx_override
        if hotfix_lane is None:
            mark_landed_on_integration(task)
        else:
            apply_merged_landed_signal(
                task,
                proj,
                base_branch=landed,
                hotfix_lane=hotfix_lane,
                non_merge_reason=non_merge_reason,
            )
        # Membership-based merge-credit propagation. The signal writes above only
        # stamp the rollup ITSELF; when it landed on the integration branch, every
        # child whose PR merged onto the rollup's staging branch reached the trunk
        # too — but squash-merge makes each child's own merge commit an ancestor of
        # nothing on integration, so per-task ancestry reconciliation can never
        # re-credit it. Propagate by lineage subtree instead. This is a bare
        # ``session.add`` on descendant rows (no commit); every recovery caller
        # (reaper orphan sweep, EVALUATING pr-check, lost-marker recovery) commits
        # ``task`` afterwards, so the child stamps ride that commit. Best-effort: a
        # propagation error must never undo the recovered rollup landing.
        if has_landed_signal(task):
            try:
                from app.rollup_management import (
                    _propagate_landed_to_staging_merged_descendants,
                )

                await _propagate_landed_to_staging_merged_descendants(task.id, session)
            except Exception:  # noqa: BLE001 — credit propagation never blocks recovery
                logger.warning(
                    "[ROLLUP] landed-signal propagation failed for recovered "
                    "rollup %s (%s)",
                    getattr(task, "id", None),
                    source,
                    exc_info=True,
                )
    return record_merged, terminal, landed


async def _heal_direction_outcomes_best_effort(task_id, *, label: str) -> None:
    """Heal a direction's research-history merge counts after an out-of-band
    reconcile, in a FRESH session re-fetching by id.

    The reconcile commit (and any internal spawn) can expire the load_only-
    projected owner row, and ``refresh_direction_outcomes_after_merge`` itself
    takes the directions lock and commits, so it must not share the sweep row's
    session. No-op unless the task carries direction/research lineage;
    best-effort — a failure here must not poison the sweep. Mirrors the sibling
    ``_pr_state_check_pending`` recovery path (see the merge-refresh block above).
    """
    from app import services
    from app.direction_outcomes import refresh_direction_outcomes_after_merge

    try:
        async with services.AsyncSessionLocal() as _dir_session:
            _dir_task = await _dir_session.get(Task, task_id)
            if _dir_task is not None:
                await refresh_direction_outcomes_after_merge(_dir_task, _dir_session)
    except Exception as _dir_exc:
        logger.warning(
            "[EVAL TIMEOUT %s] direction-outcome refresh failed for "
            "out-of-band merge of task %s: %r",
            label,
            task_id,
            _dir_exc,
        )
