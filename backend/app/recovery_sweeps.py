"""Leader-cycle recovery sweeps extracted from ``app.services``.

Holds the three bounded leader-cycle recovery sweeps — the EVALUATING-timeout
sweep (``_check_evaluating_timeouts``), the post-commit-pending sweep
(``_sweep_post_commit_pending``), and the orphan-recovery sweep
(``_recover_orphaned_tasks``) — together with their chain-inspection helpers,
the CI-backstop reason picker, and the post-commit transient-retry classifier.

Split out of ``app.services`` to keep that module below its size cap. Every
public name is re-exported from ``app.services`` so existing
``from app.services import ...`` call sites (lifecycle/orchestrator service
loops) and test patch targets keep resolving against ``app.services`` unchanged.

The shared bounding infrastructure (the per-cycle/per-call C3 constants,
``_bounded``, ``_DeployPauseSnapshot``) and the session/broadcast helpers remain
defined in ``app.services``; this module references them through the ``services``
module object so that test patches against ``app.services.<name>`` (e.g.
``AsyncSessionLocal``, ``_bounded``, ``_GH_CALL_TIMEOUT_S``,
``_EVAL_TIMEOUT_MAX_PER_CYCLE``, ``WRITE_TASK_TYPES``,
``_chain_has_active_pipeline_work``) still take effect on the moved code.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import or_, func, text
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app import services
from app.branch_resolution import classify_rollup_merge_outcome
from app.database import resolve_dialect_name
from app.landed_signal import (
    apply_merged_landed_signal,
    mark_landed_on_integration,
)
from app.models import (
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    Alert,
    AlertSeverity,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


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
            # work only if recently created — beyond queued_max_age_seconds they
            # are likely stuck (slot caps, direction holds) and must not suppress
            # the parent's hard timeout.
            if t.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING):
                queued_cutoff = _utcnow() - timedelta(seconds=queued_max_age_seconds)
                if t.created_at and t.created_at > queued_cutoff:
                    return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check chain activity for task {str(parent.id)[:8]}",
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
    # Approval-gate companion timeout: parents flagged requires_approval can
    # otherwise stall indefinitely when _chain_has_active_pipeline_work defers
    # the EVALUATING_FAIL backstop. 2h gives operators a reasonable window
    # to approve/deny independent of the configured wall-clock.
    APPROVAL_GATE_FAIL_SECONDS = 2 * 3600
    # BLOCKED is treated as terminal-for-now in this EVALUATING-timeout sweep:
    # (a) a BLOCKED child must NOT be cascade-CANCELLED on a CI-backstop timeout
    # (that would destroy its durable blocked state — it stays parked exactly as
    # a FAILED child would have stayed terminal), and (b) a parent whose
    # remaining children are all BLOCKED must still count as all-children-settled
    # so the missed-rollup recovery fires instead of pinning EVALUATING forever.
    TERMINAL = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
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
                        .options(load_only(Task.id, Task.status, Task.archived))
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
                                load_only(Task.id, Task.parent_task_id, Task.status)
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
                                )
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
                            if (
                                _s_lookup_ok
                                and _s_state == "closed"
                                and not _s_merged
                            ):
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

                # Approval-gate hard timeout. Parents with requires_approval=True
                # remain in EVALUATING waiting for human approval; the
                # evaluating_fail_hours backstop can be deferred by an active
                # pipeline chain, so approval-gated parents need their own
                # shorter companion timeout.
                if task.requires_approval:
                    approval_set_at = ctx.get("_approval_set_at")
                    if not approval_set_at:
                        from sqlalchemy.orm.attributes import flag_modified

                        task.context = {**ctx, "_approval_set_at": now.isoformat()}
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        continue
                    try:
                        approval_since = datetime.fromisoformat(approval_set_at)
                        approval_elapsed = (now - approval_since).total_seconds()
                    except (ValueError, TypeError):
                        approval_elapsed = 0.0
                    if approval_elapsed > APPROVAL_GATE_FAIL_SECONDS:
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
                                {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
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


# Substrings that mark a post-commit retry failure as TRANSIENT (the
# underlying cause is expected to clear on its own) rather than persistent.
# Transient failures must NOT count against the sweeper's 5-attempt cap —
# otherwise a multi-hour GitHub-quota outage or sustained row contention burns
# the whole budget and the sweep gives up (post_commit_failed) on a task that
# would have recovered, silently dropping the deferred side effects
# (review/fix spawn, rollup nudge). The cap exists to bound a genuinely
# non-transient defect (e.g. a malformed payload, a bug in a completion
# handler), so only persistent failures should increment it.
_POST_COMMIT_TRANSIENT_SUBSTRINGS = (
    "rate limit",
    "ratelimit",
    "secondary rate",
    "rate_limit",
    "429",
    "quota",
    "lock_timeout",
    "lock not available",
    "locknotavailable",
    "could not obtain lock",
    "held by peer",
)


def _post_commit_retry_is_transient(exc: BaseException) -> bool:
    """Classify a post-commit retry exception as transient (retriable without
    counting against the cap) vs persistent (counts toward giving up).

    Transient classes:
      - ``PostCommitLocked``: the task row is still held by a peer
        (the inline status pass or a concurrent post-commit). Pure contention —
        retrying next sweep is correct.
      - GitHub/LLM rate-limit or quota exhaustion (string-matched): clears once
        the quota window resets; counting it against the cap would abandon a
        task during a transient outage.
      - Row-lock contention (LockNotAvailableError / "lock_timeout"): same as
        PostCommitLocked but surfaced as a DBAPI error rather than the typed
        exception.

    NB: ``asyncio.TimeoutError`` (the ``_bounded`` outer ceiling) is treated as
    PERSISTENT and DOES count — a post-commit pass that wedges past its own 240s
    budget every time is the non-transient case the cap is meant to bound.
    """
    from app.task_lifecycle import PostCommitLocked

    if isinstance(exc, PostCommitLocked):
        return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(s in msg for s in _POST_COMMIT_TRANSIENT_SUBSTRINGS)


async def _sweep_post_commit_pending():
    """Re-run _process_post_commit for terminal tasks whose prior attempt died.

    When _process_post_commit (review-pipeline spawn, rollup gate handoff,
    completion handlers) times out — usually because GitHub quota hit 0 and
    a defensive call took > 240s to give up — agent_status.py sets
    Task.post_commit_pending = True and stores retry metadata under
    context['_post_commit_pending_meta'] = {at, reason, payload, attempts}.
    The idempotency guard in agent_status re-runs post-commit when the agent
    retries its status POST, but the agent VM has almost always terminated
    by then, so without this sweeper the task sits with its verdict/rollup
    state silently lost.

    Audit W1 (A2/A5): post_commit_pending is a real column now; the scan
    filter and clear go through the column instead of JSONB.

    Write-ahead markers (reason="inline_running", 2026-05-21): agent_status
    sets the marker BEFORE _process_post_commit runs, so a hard instance kill
    that bypasses the except handler still leaves a retry trail. Such a marker
    is age-gated below — a recent one may belong to a live inline run, so it
    is only retried once provably orphaned (older than the 240s post-commit
    budget plus the Cloud Run request ceiling).

    This runs every 60s.  For each pending task:
      - Reconstruct AgentStatusPayload from the saved dict.
      - Call _process_post_commit on a fresh session.
      - On success: clear the post_commit_pending column.
      - On failure/timeout: bump attempts counter (max 5 — avoid infinite
        loop if the underlying cause is non-transient).
    """
    from app.routers.agent_status import _process_post_commit
    from app.orchestrator import _mark_orchestrator_progress
    from app.schemas import AgentStatusPayload

    MAX_ATTEMPTS = 5
    # C6 rework (C): the HARD outer bound (module-level MAX_TOTAL_ATTEMPTS) that
    # transient-misclassification cannot escape. The 5-attempt cap above is
    # incremented ONLY for PERSISTENT failures, so a genuinely persistent failure
    # whose error string merely CONTAINS a transient substring ("429" / "quota" /
    # "lock_timeout") is forever classified transient, never increments
    # ``attempts``, and re-runs every 60s sweep with the marker never clearing.
    # ``total_attempts`` increments on EVERY retry (transient OR persistent) and
    # terminalizes past MAX_TOTAL_ATTEMPTS. No schema change: counter lives in
    # the existing ``_post_commit_pending_meta`` JSON.
    # A write-ahead "inline_running" marker younger than this may still be a
    # live _process_post_commit pass (240s budget, 300s Cloud Run ceiling) —
    # retrying it would double-process. Past this age a still-"inline_running"
    # marker is provably orphaned (the inline pass was killed mid-flight).
    INLINE_RUNNING_GRACE_S = 600
    try:
        async with services.AsyncSessionLocal() as session:
            # Status filter: COMPLETED / FAILED only. Tasks that an admin
            # explicitly CANCELLED must NOT have their post-commit retried —
            # a successful retry of `_handle_plan_completion` (or analogous
            # handlers) would unconditionally flip status back to EVALUATING
            # and spawn children, silently overriding the admin's cancel
            # decision (incident 2026-05-04: cancelled plan b9456e05 was
            # zombied 2 hours later — sweep retried, status flipped
            # CANCELLED → EVALUATING, child b403b76a spawned).
            #
            # CANCELLED tasks with post_commit_pending=True are cleaned up
            # below as a separate sub-query so the flag doesn't keep
            # reappearing in the sweep candidate set.
            #
            # EVALUATING is included for the childless-zombie recovery path
            # (incident 2026-05-27, below): a completion handler can flip its
            # parent to EVALUATING and release the row lock *before* spawning
            # children, then die — leaving a pending marker that the old
            # COMPLETED/FAILED-only filter never picked up and the reaper
            # never rescues. The per-task EVALUATING guard below restricts the
            # retry to childless parents so a normally-parked parent is never
            # re-run (which would duplicate its children).
            tasks = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.post_commit_pending.is_(True),
                            Task.status.in_(
                                [
                                    TaskStatus.COMPLETED,
                                    TaskStatus.FAILED,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                            Task.archived.is_(False),
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )

            # Clear the pending flag on admin-cancelled tasks — the admin
            # cancel is sticky; we don't retry post-commit and we don't
            # want this row to clutter future sweeps.
            cancelled_pending = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.post_commit_pending.is_(True),
                            Task.status == TaskStatus.CANCELLED,
                            Task.archived.is_(False),
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            for task in cancelled_pending:
                logger.info(
                    f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} is CANCELLED — "
                    f"clearing post_commit_pending without retry"
                )
                from sqlalchemy.orm.attributes import flag_modified

                new_ctx = dict(task.context or {})
                new_ctx.pop("_post_commit_pending_meta", None)
                task.context = new_ctx
                task.post_commit_pending = False
                flag_modified(task, "context")
                session.add(task)
            if cancelled_pending:
                await session.commit()

            if not tasks:
                return

            logger.info(
                f"[POST_COMMIT SWEEP] Found {len(tasks)} task(s) with post_commit_pending"
            )

            for task in tasks:
                # C3 heartbeat: _process_post_commit per task is heavy
                # (review-pipeline spawn, rollup gate handoff, GitHub I/O) and up
                # to 50 run sequentially. Pump the orchestrator progress heartbeat
                # each row so a long-but-progressing sweep is never watchdog-killed.
                _mark_orchestrator_progress()
                ctx = task.context or {}
                pending_meta = ctx.get("_post_commit_pending_meta") or {}
                # Write-ahead markers are persisted BEFORE _process_post_commit
                # runs, so a recent one may be a live inline pass rather than a
                # dead one — skip until provably orphaned. Failure-set markers
                # (post_commit_timeout/error/locked) carry a different reason
                # and retry immediately, as before. If the age can't be
                # determined, fall through and retry (safer than skip-forever).
                if pending_meta.get("reason") == "inline_running":
                    _at = pending_meta.get("at")
                    _age = None
                    if _at:
                        try:
                            _age = (
                                _utcnow() - datetime.fromisoformat(_at)
                            ).total_seconds()
                        except (ValueError, TypeError):
                            _age = None
                    if _age is not None and _age < INLINE_RUNNING_GRACE_S:
                        continue

                # Childless-EVALUATING recovery (incident 2026-05-27: plan
                # e39bd5a3 pinned research 9c3a4bc2 for ~6h). A completion
                # handler (_handle_plan_completion / _handle_research_completion)
                # flips its parent COMPLETED→EVALUATING and releases the row
                # lock inside _ensure_branch BEFORE the child-spawn loop runs.
                # If post-commit then dies, the parent is stranded EVALUATING
                # with zero children: the reaper's Step 3 needs a COMPLETED
                # child and Step 4 explicitly skips childless parents, so
                # nothing ever recovers it. Re-running the handler (it
                # dispatches on the saved payload.status=COMPLETED, not the
                # live row status) fixes it — but ONLY when the parent has zero
                # children. Child creation in those handlers is unconditional
                # and atomic (one commit for all children), so a non-zero
                # child count means the spawn already ran and re-running would
                # DUPLICATE children. A childed EVALUATING parent is a normal
                # parked parent whose marker is stale; clear it without re-run.
                if task.status == TaskStatus.EVALUATING:
                    child_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(Task)
                            .where(Task.parent_task_id == task.id)
                        )
                    ).scalar_one()
                    if child_count:
                        logger.info(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} is "
                            f"EVALUATING with {child_count} child(ren) — handler "
                            f"already spawned; clearing stale pending marker "
                            f"without re-run (avoids duplicate children)"
                        )
                        from sqlalchemy.orm.attributes import flag_modified

                        new_ctx = dict(ctx)
                        new_ctx.pop("_post_commit_pending_meta", None)
                        task.context = new_ctx
                        task.post_commit_pending = False
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        continue

                attempts = int(pending_meta.get("attempts", 0) or 0)
                # C6 rework (C): the hard outer bound. Counts EVERY retry,
                # transient or not, so transient-misclassification can't escape
                # it. Back-compatible default: pre-rework markers have no
                # ``total_attempts`` — seed it from ``attempts`` so an already-
                # in-flight task isn't reset to 0.
                total_attempts = int(
                    pending_meta.get("total_attempts", attempts) or attempts
                )
                payload_dump = pending_meta.get("payload")

                if attempts >= MAX_ATTEMPTS or total_attempts >= services.MAX_TOTAL_ATTEMPTS:
                    # Give up.  Mark abandoned + clear pending so the task
                    # doesn't keep getting picked up every sweep. The
                    # total_attempts ceiling is the transient-misclassification
                    # backstop: a persistent failure that string-matches as
                    # transient never advances ``attempts`` but DOES advance
                    # ``total_attempts``, so it terminalizes here instead of
                    # re-running forever.
                    _cap = (
                        MAX_ATTEMPTS
                        if attempts >= MAX_ATTEMPTS
                        else services.MAX_TOTAL_ATTEMPTS
                    )
                    _which = (
                        "attempts" if attempts >= MAX_ATTEMPTS else "total_attempts"
                    )
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} exceeded "
                        f"{_cap} {_which} (attempts={attempts}, "
                        f"total_attempts={total_attempts}) — marking "
                        f"post_commit_abandoned"
                    )
                    new_ctx = dict(ctx)
                    new_ctx["_post_commit_abandoned"] = {
                        "at": _utcnow().isoformat(),
                        "after_attempts": attempts,
                        "after_total_attempts": total_attempts,
                        "last_reason": pending_meta.get("reason"),
                    }
                    new_ctx.pop("_post_commit_pending_meta", None)
                    task.context = new_ctx
                    task.post_commit_pending = False
                    task.post_commit_failed = True
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    continue

                if not payload_dump:
                    # Older pending flag that didn't store the payload — we
                    # can't reconstruct. Mark abandoned.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} has no saved payload — cannot retry"
                    )
                    new_ctx = dict(ctx)
                    new_ctx["_post_commit_abandoned"] = {
                        "at": _utcnow().isoformat(),
                        "reason": "no_saved_payload",
                    }
                    new_ctx.pop("_post_commit_pending_meta", None)
                    task.context = new_ctx
                    task.post_commit_pending = False
                    task.post_commit_failed = True
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    continue

                try:
                    _payload = AgentStatusPayload(**payload_dump)
                except Exception as e:
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} payload reconstruct failed: {e}"
                    )
                    continue

                logger.info(
                    f"[POST_COMMIT SWEEP] Retrying post-commit for task {str(task.id)[:8]} "
                    f"(attempt {attempts + 1}/{MAX_ATTEMPTS}, reason={pending_meta.get('reason')})"
                )
                try:
                    # C3: hard outer ceiling so a wedged inline pass (e.g. a
                    # GitHub call that never returns past _process_post_commit's
                    # own 240s budget) can't pin the whole sweep. On timeout this
                    # falls into the except below, which bumps the attempt counter
                    # exactly as a genuine failure would (next sweep retries).
                    await services._bounded(
                        _process_post_commit(task.id, _payload),
                        services._POST_COMMIT_CALL_TIMEOUT_S,
                        what=f"post-commit {str(task.id)[:8]}",
                    )
                    # Success — clear the flag on a fresh read (post_commit may
                    # have mutated context in other ways we want to keep).
                    async with services.AsyncSessionLocal() as clear_session:
                        refreshed = await clear_session.get(Task, task.id)
                        if refreshed:
                            refreshed.post_commit_pending = False
                            new_ctx = dict(refreshed.context or {})
                            new_ctx.pop("_post_commit_pending_meta", None)
                            refreshed.context = new_ctx
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(refreshed, "context")
                            clear_session.add(refreshed)
                            await clear_session.commit()
                    logger.info(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} recovered"
                    )
                except Exception as e:
                    # C6 fix: transient failures (PostCommitLocked / rate-limit /
                    # lock contention) must NOT count against the 5-attempt cap.
                    # Otherwise a sustained GitHub-quota outage or row contention
                    # exhausts the budget and the sweep abandons a task
                    # (post_commit_failed) that would have recovered once the
                    # transient cause cleared — silently dropping the deferred
                    # side effects. The cap is reserved for persistent defects.
                    if _post_commit_retry_is_transient(e):
                        logger.info(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} retry hit a "
                            f"transient failure ({type(e).__name__}) — NOT counting "
                            f"against the {MAX_ATTEMPTS}-attempt cap; will retry next sweep "
                            f"(total_attempts {total_attempts}/{services.MAX_TOTAL_ATTEMPTS})"
                        )
                        # C6 rework (C): still advance the HARD outer bound. A
                        # persistent failure that string-matches as transient
                        # lands here every sweep; without bumping total_attempts
                        # it would loop forever and the marker would never clear.
                        # Bump in its own session/try so a bump failure on one
                        # task doesn't poison the batch (mirrors the persistent
                        # path below). Short lock_timeout to fail fast on
                        # contention rather than burn cycle time.
                        try:
                            async with services.AsyncSessionLocal() as t_session:
                                if resolve_dialect_name(t_session) == "postgresql":
                                    await t_session.execute(
                                        text("SET LOCAL lock_timeout = '5s'")
                                    )
                                refreshed = await t_session.get(Task, task.id)
                                if refreshed:
                                    new_ctx = dict(refreshed.context or {})
                                    new_meta = dict(
                                        new_ctx.get("_post_commit_pending_meta")
                                        or pending_meta
                                    )
                                    new_meta["total_attempts"] = (
                                        int(
                                            new_meta.get(
                                                "total_attempts", total_attempts
                                            )
                                        )
                                        + 1
                                    )
                                    new_meta["last_transient_at"] = (
                                        _utcnow().isoformat()
                                    )
                                    new_ctx["_post_commit_pending_meta"] = new_meta
                                    refreshed.context = new_ctx
                                    from sqlalchemy.orm.attributes import (
                                        flag_modified,
                                    )

                                    flag_modified(refreshed, "context")
                                    t_session.add(refreshed)
                                    await t_session.commit()
                        except Exception as t_exc:
                            logger.warning(
                                f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} "
                                f"transient total_attempts bump failed (likely "
                                f"row-lock contention): {type(t_exc).__name__}: {t_exc}"
                            )
                        continue
                    # Persistent failure — bump attempts so we eventually give up.
                    # The retry path in _process_post_commit's exception handler
                    # already bumps attempts itself if it got to set the
                    # flag again; this covers cases where it didn't.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} retry failed: {e}"
                    )
                    # Bump in its own try/except so a bump failure on one task
                    # doesn't poison the entire sweep batch (which would let
                    # the outer except at the bottom abort the loop, leaving
                    # all subsequent pending tasks unprocessed). Short
                    # lock_timeout (5s) so we fail fast on row contention
                    # instead of burning 30s of orchestrator-cycle time per
                    # task — the next sweep will try again.
                    try:
                        async with services.AsyncSessionLocal() as bump_session:
                            # Postgres-only fast-fail on row contention; on
                            # SQLite (test DB) SET LOCAL is a no-op syntax
                            # error, so dialect-gate it.
                            if resolve_dialect_name(bump_session) == "postgresql":
                                await bump_session.execute(
                                    text("SET LOCAL lock_timeout = '5s'")
                                )
                            refreshed = await bump_session.get(Task, task.id)
                            if refreshed:
                                new_ctx = dict(refreshed.context or {})
                                new_pending = dict(
                                    new_ctx.get("_post_commit_pending_meta")
                                    or pending_meta
                                )
                                new_pending["attempts"] = (
                                    int(new_pending.get("attempts", attempts)) + 1
                                )
                                # C6 rework (C): persistent failures advance BOTH
                                # the 5-attempt cap and the hard outer bound, so
                                # the give-up check above fires on whichever
                                # ceiling is hit first.
                                new_pending["total_attempts"] = (
                                    int(
                                        new_pending.get("total_attempts", total_attempts)
                                    )
                                    + 1
                                )
                                new_pending["last_retry_at"] = _utcnow().isoformat()
                                new_ctx["_post_commit_pending_meta"] = new_pending
                                refreshed.context = new_ctx
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(refreshed, "context")
                                bump_session.add(refreshed)
                                await bump_session.commit()
                    except Exception as bump_exc:
                        logger.warning(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} "
                            f"attempt-bump failed (likely row-lock contention): "
                            f"{type(bump_exc).__name__}: {bump_exc}"
                        )
                        # Continue to next task — attempts stays where it
                        # was, but the sweep proceeds. If contention persists
                        # the operator must clear post_commit_pending via the
                        # admin PATCH (admin.py modify_task).
    except Exception as e:
        logger.error(f"[POST_COMMIT SWEEP] Sweep failed: {e}", exc_info=True)


async def _recover_orphaned_tasks():
    """Detect COMPLETED tasks with no PR artifact and no review child. Look up PR on GitHub and spawn review."""
    from app.routers.agent import get_github_token_for_task
    from app.review_pipeline import _spawn_pipeline_task
    from app.orchestrator import _mark_orchestrator_progress
    from app.models import Project

    try:
        five_min_ago = _utcnow() - timedelta(minutes=5)
        one_hour_ago = _utcnow() - timedelta(hours=1)

        # ── Phase A: cycle reads on a short-lived session, snapshot to plain
        # data, then CLOSE it before any per-row session opens (audit P1). A
        # single shared session held across the per-row loop meant a _bounded
        # token-fetch cancellation could poison it (PendingRollback), silently
        # dropping every later orphan's artifact/alert/review-spawn commit. We
        # snapshot just the skip-decision data here; each row that needs GitHub
        # I/O opens its OWN session below and re-fetches the task fresh.
        async with services.AsyncSessionLocal() as read_session:
            # Phase B4 (H-P6): drop status_log/plan_output JSONB. The recovery
            # loop only reads first_payload / context / description / project_id /
            # id and writes context. status_log is not consumed in this branch.
            candidates = (
                (
                    await read_session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.project_id,
                                Task.created_by,
                                Task.created_at,
                                Task.description,
                                Task.payloads,
                                Task.context,
                            )
                        )
                        .where(
                            Task.status == TaskStatus.COMPLETED,
                            Task.created_by == "ORCHESTRATOR",
                            Task.created_at <= five_min_ago,
                            Task.created_at
                            >= one_hour_ago,  # Only check recent tasks, not entire history
                        )
                        # C3: per-cycle row cap. Each candidate costs a serial
                        # get_github_token_for_task + list-pulls GitHub round-trip;
                        # an unbounded fan-out is the leader-cycle saturation this
                        # cluster targets. Oldest-first so the most-stale orphans
                        # are serviced before the cap truncates; deferred rows
                        # re-qualify next cycle (runs every 120s).
                        .order_by(Task.created_at.asc())
                        .limit(services._ORPHAN_RECOVERY_MAX_PER_CYCLE)
                    )
                )
                .scalars()
                .all()
            )
            if len(candidates) >= services._ORPHAN_RECOVERY_MAX_PER_CYCLE:
                logger.warning(
                    "[RECOVERY] Hit per-cycle cap of %d orphan candidates — "
                    "remainder deferred to next sweep",
                    services._ORPHAN_RECOVERY_MAX_PER_CYCLE,
                )

            # Snapshot the per-row skip-decision data as plain tuples so it
            # outlives the read_session (id, task_type, first_payload, context).
            cand_snaps = [
                (t.id, (t.first_payload or {}).get("type", ""))
                for t in candidates
            ]
            cand_ids = [t.id for t in candidates]
            if cand_ids:
                artifact_rows = (
                    await read_session.execute(
                        select(Artifact.task_id).where(
                            Artifact.task_id.in_(cand_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                tasks_with_pr = {row[0] for row in artifact_rows}

                child_rows = (
                    await read_session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(cand_ids)
                        )
                    )
                ).all()
                children_by_parent: dict = {}
                for parent_id, payloads in child_rows:
                    children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                tasks_with_pr = set()
                children_by_parent = {}
        # read_session closed — no session held across the per-row loop.

        for task_id, task_type in cand_snaps:
            # C3 heartbeat: serial GitHub I/O per candidate — pump the
            # orchestrator progress heartbeat each row so a long-but-
            # progressing recovery pass is not watchdog-killed.
            _mark_orchestrator_progress()
            # Canonical write-task-type allowlist: a task type that opens a PR
            # is recovery-eligible. Sourced from the registry so a newly-added
            # write type is honored here automatically (no string-literal tuple
            # to keep in sync). See app/task_capabilities.py:WRITE_TASK_TYPES.
            if task_type not in services.WRITE_TASK_TYPES:
                continue

            # Check if PR artifact exists
            if task_id in tasks_with_pr:
                continue

            # Check if review child exists
            child_payloads_list = children_by_parent.get(task_id, [])
            if any(
                (payloads or [{}])[0].get("type") == "review"
                for payloads in child_payloads_list
            ):
                continue

            # Audit P1: open a FRESH session per orphan. A _bounded() token /
            # list-pulls cancellation that poisons THIS session is confined to
            # this iteration — the `async with` closes it on the way out and the
            # next orphan gets a clean session, so one slow / cancelled lookup
            # no longer drops the rest of the sweep's artifact/alert/spawn work.
            async with services.AsyncSessionLocal() as session:
                # Re-fetch the orphan fresh on this session (the spawn path
                # below needs a live ORM instance bound to it) and re-validate
                # it is still a COMPLETED orphan.
                task = await session.get(Task, task_id)
                if task is None or task.status != TaskStatus.COMPLETED:
                    continue

                # Orphan has no PR yet. Cap retries — if we've looked 3 times
                # and found nothing, stop burning API quota on this row until
                # new evidence (an artifact showing up via webhook) arrives.
                # Recovery service runs every 120s; 3 attempts = ~6 min window
                # which is enough to catch the usual "PR created a few seconds
                # after agent reported done" race but won't keep polling for
                # the full 55-minute scan window.
                ctx = task.context or {}
                attempts_so_far = int(ctx.get("_recovery_attempts", 0))
                if attempts_so_far >= 3:
                    continue

                try:
                    gh_token, owner, repo = await services._bounded(
                        get_github_token_for_task(task.id, session),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"recovery token {str(task.id)[:8]}",
                    )
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub token fetch failed",
                        exc_info=True,
                    )
                    continue

                _proj = await session.get(Project, task.project_id)
                branch = services._derive_task_branch(
                    task.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_proj),
                )

                try:
                    from app.github import _github_request

                    headers = {
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github.v3+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    }
                    resp = await services._bounded(
                        _github_request(
                            "GET",
                            f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open",
                            headers,
                        ),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"recovery list-pulls {str(task.id)[:8]}",
                    )
                    prs = resp.json() if resp.status_code == 200 else []
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub PR lookup failed",
                        exc_info=True,
                    )
                    continue

                if not prs:
                    # Record the miss so we back off after N failed lookups.
                    task.context = {**ctx, "_recovery_attempts": attempts_so_far + 1}
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — no open PR found for branch {branch} (attempt {attempts_so_far + 1}/3)"
                    )
                    continue

                pr = prs[0]
                pr_url = pr["html_url"]
                pr_number = pr["number"]
                base_branch = task.first_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_proj)

                # Create the missing artifact
                artifact = Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.PULL_REQUEST,
                    pr_url=pr_url,
                )
                session.add(artifact)

                # Spawn review
                original_prompt = task.first_payload.get(
                    "prompt", task.description or ""
                )
                review_task, _ = await _spawn_pipeline_task(
                    task,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task.id),
                )

                # Create alert
                alert = Alert(
                    project_id=task.project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Auto-recovered orphaned PR #{pr_number}",
                    message=f"Task {str(task.id)[:8]} completed with PR #{pr_number} but review was never spawned. Auto-recovered.",
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
                logger.warning(
                    f"[RECOVERY] Auto-recovered orphaned task {str(task.id)[:8]} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}"
                )

        # Phase 2: Reaped-with-artifact recovery.
        # Tasks that were reaped (set back to QUEUED/PROVISIONING) after the PR
        # was already created. The PR endpoint saves the artifact, so these
        # tasks have an artifact but never reached COMPLETED and thus never
        # triggered pipeline routing.
        #
        # Audit P1: same Phase-A (short-lived read session, snapshot, close) /
        # Phase-B (per-row session) split as Phase 1 above.
        async with services.AsyncSessionLocal() as read_session:
            reaped_candidates = (
                (
                    await read_session.execute(
                        select(Task).where(
                            Task.status.in_(
                                [TaskStatus.QUEUED, TaskStatus.PROVISIONING]
                            ),
                            Task.created_by == "ORCHESTRATOR",
                            Task.attempt_count > 0,
                            Task.created_at >= one_hour_ago,
                        )
                    )
                )
                .scalars()
                .all()
            )

            reaped_snaps = [
                (t.id, (t.first_payload or {}).get("type", ""))
                for t in reaped_candidates
            ]
            reaped_ids = [t.id for t in reaped_candidates]
            if reaped_ids:
                pr_artifact_rows = (
                    await read_session.execute(
                        select(Artifact.task_id, Artifact.pr_url).where(
                            Artifact.task_id.in_(reaped_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                pr_url_by_task: dict = {}
                for _t_id, _pr_url in pr_artifact_rows:
                    if _pr_url and not pr_url_by_task.get(_t_id):
                        pr_url_by_task[_t_id] = _pr_url

                reaped_child_rows = (
                    await read_session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(reaped_ids)
                        )
                    )
                ).all()
                reaped_children_by_parent: dict = {}
                for parent_id, payloads in reaped_child_rows:
                    reaped_children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                pr_url_by_task = {}
                reaped_children_by_parent = {}
        # read_session closed — no session held across the per-row loop.

        for task_id, task_type in reaped_snaps:
            # Same canonical write-task-type allowlist as _recover_orphaned_tasks
            # — a reaped task that opened a PR is eligible for review handoff.
            # See app/task_capabilities.py:WRITE_TASK_TYPES.
            if task_type not in services.WRITE_TASK_TYPES:
                continue

            # Only recover tasks that already have a PR artifact
            pr_url = pr_url_by_task.get(task_id)
            if not pr_url:
                continue

            # Skip if review child already exists
            child_payloads_list = reaped_children_by_parent.get(task_id, [])
            if any(
                (payloads or [{}])[0].get("type") == "review"
                for payloads in child_payloads_list
            ):
                continue

            # Audit P1: per-row session. The lock+release-commit+spawn unit
            # runs in its own session so a cancellation/poison on one reaped
            # row can't drop the others.
            async with services.AsyncSessionLocal() as session:
                # ── Site 3 lock-during-IO fix ──
                # Previously this loop held FOR UPDATE on `task` across
                # `_spawn_pipeline_task`, which acquires
                # `pg_advisory_xact_lock(pr_number)` via
                # `acquire_pr_spawn_lock` (task_locking.py:223). If a
                # peer worker was mid-spawn for the same PR, this
                # iteration would block on the advisory lock with the
                # task FOR UPDATE held — starving any other writer
                # touching this row. Fix: hold the task FOR UPDATE only
                # for the COMPLETED transition, commit to release it,
                # then call `_spawn_pipeline_task` (the advisory lock is
                # per-PR and self-coordinating). The spawn-and-alert
                # commit follows on a fresh transaction.
                locked = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if not locked or locked.status not in (
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                ):
                    continue

                pr_number = int(pr_url.rstrip("/").split("/")[-1])
                short_id = str(locked.id).replace("-", "")[:8]
                _proj = await session.get(Project, locked.project_id)
                base_branch = locked.first_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_proj)
                branch = services._derive_task_branch(
                    locked.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_proj),
                )
                original_prompt = locked.first_payload.get(
                    "prompt", locked.description or ""
                )
                project_id_local = locked.project_id
                task_id_local = locked.id

                locked.status = TaskStatus.COMPLETED
                locked.latest_status_message = (
                    f"Auto-completed: PR #{pr_number} found after reap recovery"
                )
                session.add(locked)
                # Commit-release the task FOR UPDATE before invoking the
                # spawn (which acquires the per-PR advisory lock). The
                # task is now COMPLETED and persisted; the spawn can
                # proceed on a fresh transaction without holding the
                # task row lock across the advisory-lock acquire.
                await session.commit()

                # Re-fetch a fresh, unlocked instance for the spawn. The
                # spawn path reads source_task fields (context, payloads,
                # title) and writes back ``latest_status_message`` /
                # ``context`` markers; using the same `locked` object
                # without re-attach risks DetachedInstanceError after the
                # commit released the SQLA identity for some attributes.
                refreshed = await session.get(Task, task_id_local)
                if refreshed is None:
                    logger.warning(
                        f"[RECOVERY] Task {short_id} disappeared after release-commit — skipping spawn"
                    )
                    continue
                review_task, _ = await _spawn_pipeline_task(
                    refreshed,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task_id_local),
                )

                alert = Alert(
                    project_id=project_id_local,
                    severity=AlertSeverity.WARNING,
                    title=f"Auto-recovered reaped task with PR #{pr_number}",
                    message=f"Task {short_id} was reaped after creating PR #{pr_number}. Auto-completed and review spawned.",
                    task_id=task_id_local,
                )
                session.add(alert)
                await session.commit()

                await services.broadcast_to_project(
                    str(project_id_local),
                    "alert_created",
                    {
                        "alert_id": str(alert.id),
                        "severity": "WARNING",
                        "title": alert.title,
                    },
                )
                logger.warning(
                    f"[RECOVERY] Auto-completed reaped task {short_id} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}"
                )
    except Exception as e:
        logger.error(f"[RECOVERY] Orphaned task sweep failed: {e}", exc_info=True)
