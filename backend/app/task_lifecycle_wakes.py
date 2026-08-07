"""Task park-and-wake primitive and orphan-PR cleanup, extracted from task_lifecycle.py.

Owns ``close_orphan_pr_if_any``, ``park_task``, ``fire_task_wakes``,
``deliver_wake``, ``expire_wake``, and ``_maybe_fire_terminal_compute_wake``
(plus the ``_PARKABLE_STATUSES`` / ``_PARK_STRAND_SECONDS`` constants).
``task_lifecycle.py`` re-exports all of them so all existing callers are
unaffected.  This module imports only from leaves of the import graph
(models, task_blocked, stdlib), so it creates no import cycle.
"""

import contextlib
import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Task,
    TaskStatus,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
    TASK_PARKABLE_STATUSES,
)
from app.task_blocked import reset_task_for_fresh_attempt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orphan-PR cleanup on cancellation
# ---------------------------------------------------------------------------
#
# When a task is cancelled, any PR it opened is left orphaned: the work is
# no longer wanted, but the PR sits there blocking the rollup gate (parent
# stays EVALUATING waiting on an open PR that nobody will merge). Most
# common cause is duplicate-dedup — a task's twin shipped the same finding,
# the twin merged via rollup, the duplicate was cancelled, but its PR
# stayed open. See c56fa118 / PR #5983 incident on 2026-05-26.
#
# We expose `close_orphan_pr_if_any` as an explicit helper that cancel-site
# callers invoke with their already-open session + the task being cancelled.
# An earlier rev tried to wire this into the Task.status terminal-transition
# event listener as a fire-and-forget; that opened a second session on the
# StaticPool SQLite engine the test fixtures share, racing the test's own
# session and breaking ~20 unrelated requeue tests. Explicit calls at known
# cancel sites are noisier but safer.
async def close_orphan_pr_if_any(
    task: Task, session: AsyncSession, *, reason: str = "task_cancelled"
) -> bool:
    """Best-effort: close the open PR associated with `task` if any.

    Returns True if a PR was closed, False otherwise. Skips merged PRs
    (state=closed,merged=true) and already-closed PRs. Failures (no token,
    GitHub 5xx, PR vanished) are logged but never raised — the caller's
    cancel must always complete.

    Caller should invoke this AFTER setting task.status to CANCELLED but
    BEFORE the session.commit() that persists the cancel — the GitHub PATCH
    is independent of the DB transaction either way, and grouping them
    lets the caller bail cleanly if the GitHub call hangs.
    """
    payload_types = {
        str(payload.get("type") or "")
        for payload in (task.payloads or [])
        if isinstance(payload, dict)
    }
    if payload_types & {"review"}:
        # Review tasks reference the PR they inspect; they do not own it.
        # In particular, review_loop.pr_number is shared with create/fix
        # tasks, so treating that reference as ownership lets cancelling a
        # duplicate/stale reviewer close the implementation PR underneath the
        # surviving review/fix lane.
        logger.info(
            "[CANCEL-PR-CLEANUP] Skipping PR close for non-owning review task "
            f"{task.id} (reason={reason})"
        )
        return False

    ctx = task.context or {}

    # Resolve PR number from any recorded location.  The url-keyed context
    # entries (_pr_url / pr_url) are checked first for backward compat; then
    # the per-review-cycle review_loop.pr_number (create/fix tasks) and the
    # rollup_pr_number (plan/research sub-rollup tasks whose PR targets the
    # parent staging branch rather than main).
    pr_number: int | None = None
    pr_url = ctx.get("_pr_url") or ctx.get("pr_url") or ""
    if pr_url:
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_number = int(m.group(1))
    if pr_number is None:
        # Also check review_loop.pr_number — set by the review pipeline for
        # sub-rollup and review-loop PRs that store the number but not a full URL
        # in _pr_url / pr_url (the canonical lookup above misses these, causing
        # the cancel path to leak the PR open and block the parent rollup).
        _rl = ctx.get("review_loop") or {}
        if isinstance(_rl, dict) and _rl.get("pr_number") is not None:
            try:
                pr_number = int(_rl["pr_number"])
            except (ValueError, TypeError):
                pass
    if pr_number is None:
        # Also check rollup_pr_number / rollup_pr_url — set for rollup parents
        # in awaiting-approval and security-blocked states (never seeds review_loop).
        _rollup_url = ctx.get("rollup_pr_url") or ""
        if _rollup_url:
            _m = re.search(r"/pull/(\d+)", _rollup_url)
            if _m:
                pr_number = int(_m.group(1))
    if pr_number is None:
        _rpn = ctx.get("rollup_pr_number")
        if _rpn is not None:
            try:
                pr_number = int(_rpn)
            except (ValueError, TypeError):
                pass
    if pr_number is None:
        return False

    from app.github import close_pull_request, get_pull_request
    from app.routers.agent import get_github_token_for_task

    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] get_github_token_for_task failed for "
            f"task {task.id} (reason={reason})"
        )
        return False
    try:
        pr = await get_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.warning(
            f"[CANCEL-PR-CLEANUP] get_pull_request failed for task "
            f"{task.id} PR #{pr_number} (reason={reason}) — likely already "
            f"closed or GitHub transient; skipping"
        )
        return False
    if pr.get("merged"):
        return False
    if pr.get("state") != "open":
        return False
    try:
        ok = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] close_pull_request raised for task "
            f"{task.id} PR #{pr_number} (reason={reason})"
        )
        return False
    if ok:
        logger.info(
            f"[CANCEL-PR-CLEANUP] Closed orphan PR #{pr_number} for "
            f"cancelled task {task.id} (reason={reason})"
        )
    return ok


# ---------------------------------------------------------------------------
# Task park-and-wake primitive (alembic 0199 / TaskWakeRegistration).
#
# A task that is waiting on an external, non-agent signal parks via
# :func:`park_task` — it releases its VM/JWT/fleet slot immediately and writes a
# durable :class:`TaskWakeRegistration`. The external source later fires via
# :func:`fire_task_wakes`, and the leader-owned wake-delivery worker
# (``reaper_sweeps.sweep_deliver_task_wakes``) claims each FIRED registration
# ``FOR UPDATE SKIP LOCKED`` and calls :func:`deliver_wake`, which advances the
# PARKED task **exactly once**. Registrations that never fire are aged out by
# :func:`expire_wake` at their deadline rather than stranding the task.
#
# These are the centralized, row-lock-aware helpers; the sweep is the only
# scheduled caller of deliver/expire, but tests drive them directly.
# ---------------------------------------------------------------------------

# Statuses a live task may transition into PARKED from. Terminal states and
# BLOCKED are excluded — a parked task is *running* work that chose to wait.
_PARKABLE_STATUSES = TASK_PARKABLE_STATUSES

# Default age after which a PARKED task with no live (PENDING/FIRED) wake
# registration is treated as orphaned and failed rather than stranded forever.
_PARK_STRAND_SECONDS = 6 * 60 * 60


async def _maybe_fire_terminal_compute_wake(
    session,
    registration: TaskWakeRegistration,
    *,
    now,
) -> None:
    """Close the terminal-before-park lost-wakeup race for compute-job wakes.

    Register-then-recheck: after the PENDING ``compute_job_terminal`` registration
    exists, acquire the SAME compute-job row lock the monitor takes on its
    terminal-fire path (``compute_jobs.process_compute_jobs_once`` →
    ``_lock_compute_job``) and check whether the job is ALREADY terminal. If it is,
    fire the just-created registration immediately (PENDING → FIRED) so the leader
    sweep finalizes it identically to a normally-fired wake.

    Why this is race-free in BOTH orderings:

    * terminal-before-park — the monitor finalized + fired under the row lock while
      no registration existed (it fired nothing and will never revisit the terminal
      job). This recheck runs AFTER the lock is free, sees the terminal job, and
      fires the registration the monitor missed.
    * park-before-terminal — the recheck sees a non-terminal job and leaves the
      registration PENDING; the monitor later finalizes and fires it. If the two
      race, the row lock serializes them: whichever writer holds the lock completes
      before the other observes the job, so exactly one of them fires the row.

    Idempotent: only a PENDING registration is fired (PENDING → FIRED); an
    already-FIRED/DELIVERED registration is left untouched, so a delayed or retried
    park can never double-fire.
    """
    from app.compute_jobs import (
        COMPUTE_JOB_TERMINAL_WAKE_KIND,
        _COMPUTE_JOB_TERMINAL_STATUSES,
        _lock_compute_job,
    )

    if registration.wake_source_kind != COMPUTE_JOB_TERMINAL_WAKE_KIND:
        return
    if registration.delivery_state != WakeDeliveryState.PENDING:
        return
    if not registration.wake_source_ref:
        return
    try:
        job_id = uuid.UUID(str(registration.wake_source_ref))
    except (ValueError, TypeError):
        return

    # SELECT ... FOR UPDATE on the compute job row — the shared serialization
    # point with the monitor's terminal-fire path. autoflush persists the new
    # PENDING registration before this read, keeping it consistent in-session.
    job = await _lock_compute_job(session, job_id)
    if job is None:
        return
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        registration.delivery_state = WakeDeliveryState.FIRED
        registration.fired_at = now
        session.add(registration)
        logger.info(
            "[PARK] compute job %s already terminal at park time; fired "
            "registration %s (terminal-before-park catch-up)",
            job_id,
            registration.id,
        )


async def park_task(
    session,
    task: Task,
    *,
    wake_source_kind: str,
    wake_source_ref: str | None = None,
    source_metadata: dict | None = None,
    wake_action=WakeAction.RESUME,
    deadline_at=None,
    reason: str | None = None,
    now=None,
) -> TaskWakeRegistration:
    """Transition ``task`` into PARKED and persist a durable wake registration.

    Mirrors :func:`app.task_blocked.enter_blocked_state`: the caller owns the
    task row lock (``SELECT ... FOR UPDATE``), the ``session.commit()``, and any
    post-commit VM teardown (``_ensure_vm_deleted``). This helper does the
    in-critical-section work that must be atomic with the status flip: releasing
    the account/fleet slot exactly once, stamping operator-visible park metadata,
    and adding the ``TaskWakeRegistration`` row. Returns the registration.

    The fleet slot is released here (not deferred) so a parked task stops pinning
    capacity the moment it parks — that is the whole point of the primitive.

    Idempotent on retry: if the task is already PARKED and a live (PENDING or
    FIRED) wake registration matching (task_id, kind, ref) already exists, that
    registration is returned immediately without creating a duplicate.  This
    covers the normal failure mode where the agent's first park POST response was
    lost and it retries — the second call must not create a second registration.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()

    # Guard: only parkable statuses may transition into PARKED.
    # If already PARKED, check for an existing live registration before
    # creating another — idempotent retry support.
    if task.status == TaskStatus.PARKED:
        existing_reg = (
            (
                await session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.task_id == task.id,
                        TaskWakeRegistration.wake_source_kind
                        == str(wake_source_kind)[:64],
                        TaskWakeRegistration.wake_source_ref
                        == (str(wake_source_ref)[:255] if wake_source_ref else None),
                        TaskWakeRegistration.delivery_state.in_(
                            [WakeDeliveryState.PENDING, WakeDeliveryState.FIRED]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_reg is not None:
            # Delayed/retried park: re-run the terminal recheck so a job that went
            # terminal AFTER the original park (but whose monitor fire found no
            # registration, or whose registration is still PENDING) is caught here
            # too. Idempotent — only fires a PENDING registration.
            await _maybe_fire_terminal_compute_wake(session, existing_reg, now=now)
            return existing_reg
        # No live registration found (it was lost); fall through to create one.
    elif task.status not in _PARKABLE_STATUSES:
        raise ValueError(
            f"task {task.id} cannot be parked from status {task.status!r} "
            f"(parkable: {', '.join(s.value for s in _PARKABLE_STATUSES)})"
        )

    # Release the account/fleet slot exactly once (idempotent via the
    # ``_slot_released`` marker, mirroring the BLOCKED path in agent_status).
    try:
        from app.max_plan_pool import (
            get_task_llm_account_id,
            release_max_plan_account,
        )

        acct_id = get_task_llm_account_id(task)
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(
                acct_id,
                project_id=str(task.project_id) if task.project_id else None,
            )
            task.context = {**(task.context or {}), "_slot_released": True}
    except Exception:
        logger.exception("[PARK] slot release failed for task %s", task.id)

    task.status = TaskStatus.PARKED
    task.parked_at = now
    task.park_reason = (reason or f"waiting on {wake_source_kind}")[:2000]
    task.vm_stopped_at = now

    # Retire any LEASED pool member before clearing vm_instance_id. Once the
    # linkage is gone, the terminal-completion path (_release_pool_member_if_terminal)
    # can't find the member and it stays LEASED forever, silently exhausting pool
    # capacity. Best-effort: a pool failure must never block the park itself.
    _park_pool_vm_id = task.vm_instance_id
    if _park_pool_vm_id:
        try:
            from app.models import PoolVmState
            from app.warm_pool import (
                is_warm_pool_enabled,
                mark_pool_vm_retiring,
            )

            if is_warm_pool_enabled():
                await mark_pool_vm_retiring(
                    session, _park_pool_vm_id, source_states=(PoolVmState.LEASED,)
                )
        except Exception:
            logger.exception(
                "[PARK] pool-member retire failed for task %s vm=%s",
                task.id,
                _park_pool_vm_id,
            )

    task.vm_instance_id = None

    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=task.project_id,
        wake_source_kind=str(wake_source_kind)[:64],
        wake_source_ref=(str(wake_source_ref)[:255] if wake_source_ref else None),
        source_metadata=dict(source_metadata or {}),
        wake_action=WakeAction(wake_action),
        delivery_state=WakeDeliveryState.PENDING,
        requested_at=now,
        deadline_at=deadline_at,
    )
    session.add(task)
    session.add(reg)

    # Register-then-recheck: now that the PENDING registration exists, close the
    # terminal-before-park lost-wakeup race for the compute-job wake source. No-op
    # for every other wake kind.
    await _maybe_fire_terminal_compute_wake(session, reg, now=now)
    return reg


async def fire_task_wakes(
    session,
    *,
    wake_source_kind: str,
    wake_source_ref: str | None,
    fired_at=None,
    metadata_update: dict | None = None,
) -> int:
    """Mark every PENDING registration for ``(kind, ref)`` as FIRED. Idempotent.

    Called by the external consumer when its source fires (e.g. a ComputeJob
    reaching a terminal state). Rows are locked ``FOR UPDATE SKIP LOCKED`` so a
    concurrent firing cannot double-fire the same registration. The leader-owned
    worker delivers the FIRED rows on its next pass. Returns the count newly
    fired. The caller commits.
    """
    from app.time_utils import _utcnow

    fired_at = fired_at or _utcnow()
    stmt = (
        select(TaskWakeRegistration)
        .where(
            TaskWakeRegistration.wake_source_kind == str(wake_source_kind)[:64],
            TaskWakeRegistration.wake_source_ref == wake_source_ref,
            TaskWakeRegistration.delivery_state == WakeDeliveryState.PENDING,
        )
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    n = 0
    for reg in rows:
        reg.delivery_state = WakeDeliveryState.FIRED
        reg.fired_at = fired_at
        if metadata_update:
            reg.source_metadata = {**(reg.source_metadata or {}), **metadata_update}
        session.add(reg)
        n += 1
    return n


async def deliver_wake(
    session, registration: TaskWakeRegistration, *, now=None
) -> bool:
    """Advance the PARKED task behind a FIRED registration **exactly once**.

    Exactly-once is enforced by the FIRED→DELIVERED flip happening atomically
    with the task advance, under the registration row lock the worker already
    holds (``FOR UPDATE SKIP LOCKED``). Any second call — a retry, or a racing
    worker — sees ``delivery_state != FIRED`` (or the task no longer PARKED) and
    returns ``False`` without touching the task. Returns ``True`` iff this call
    advanced the task.

    The caller (the sweep, or a test) owns the ``session.commit()`` for EVERY
    branch — RESUME, generic FINALIZE, and the compute-job FINALIZE branch. None
    of them commit internally, so the FIRED→DELIVERED flip and the task advance
    commit together atomically under the caller's single commit. The compute
    branch finalizes through ``_handle_compute_experiment_completion`` with
    ``owns_commit=False`` (pure mutation, no internal commit); its post-commit
    side effects (operator broadcast + durable knowledge persistence) are run by
    the sweep AFTER it commits, via
    ``task_completion.run_compute_wake_post_commit``.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()
    registration.attempt_count = (registration.attempt_count or 0) + 1

    # Idempotency guard: only a FIRED registration is deliverable.
    if registration.delivery_state != WakeDeliveryState.FIRED:
        session.add(registration)
        return False

    task = (
        (
            await session.execute(
                select(Task).where(Task.id == registration.task_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if task is None or task.status != TaskStatus.PARKED:
        # The task moved on (already woken by a sibling registration, cancelled,
        # etc.). Record a truthful no-op outcome and do NOT advance anything.
        registration.delivery_state = WakeDeliveryState.FAILED
        registration.result_state = "task_not_parked"
        registration.result_detail = (
            f"task {registration.task_id} was "
            f"{getattr(task, 'status', None)} not PARKED at delivery"
        )[:2000]
        registration.delivered_at = now
        registration.claimed_at = registration.claimed_at or now
        session.add(registration)
        return False

    action = WakeAction(registration.wake_action)

    # Compute-job-terminal wake source: finalize the parked compute_experiment
    # task through its REAL completion path so it lands with the honest terminal
    # outcome, final cost, and result reference rather than a bare status flip.
    # The FIRED→DELIVERED flip is recorded BEFORE the handler runs so it commits
    # atomically with the task finalization the handler performs — preserving
    # exactly-once delivery (a re-fired or re-claimed registration is no longer
    # FIRED and returns False above). Critically the handler runs with
    # ``owns_commit=False`` (pure mutation, no internal commit), so the caller's
    # single commit covers BOTH the FIRED→DELIVERED flip and the task advance and
    # a mid-batch commit can never release sibling delivery locks early.
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

    if (
        action == WakeAction.FINALIZE
        and registration.wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND
    ):
        from app.compute_jobs import build_compute_result_logs_async
        from app.models import ComputeJob
        from app.task_completion import (
            _COMPUTE_WAKE_POST_COMMIT_META_KEY,
            _handle_compute_experiment_completion,
        )

        job = None
        if registration.wake_source_ref:
            with contextlib.suppress(ValueError, TypeError):
                job = await session.get(
                    ComputeJob, uuid.UUID(registration.wake_source_ref)
                )
        registration.delivery_state = WakeDeliveryState.DELIVERED
        registration.delivered_at = now
        registration.claimed_at = registration.claimed_at or now
        session.add(registration)
        # Clear the park markers before the completion handler mutates the task so
        # the finalized task carries no stale parked_at/park_reason (the completion
        # path only manages status, not the park bookkeeping).
        task.parked_at = None
        task.park_reason = None
        session.add(task)
        if job is None:
            # The ComputeJob row is missing (dangling ref, deleted row, or
            # unparseable wake_source_ref). Stamp a distinct, truthful failure
            # classification so operators and auto-remediation can distinguish
            # this from a legitimate metric-gate miss.
            registration.result_state = "job_record_missing"
            task.status = TaskStatus.FAILED
            task.latest_status_message = (
                "Compute wake finalized: job record missing at delivery"
            )[:500]
            ctx = dict(task.context or {})
            ctx["failure_category"] = "compute_job_record_missing"
            ctx["failure_class"] = "compute_job_record_missing"
            task.context = ctx
            flag_modified(task, "context")
            with contextlib.suppress(Exception):
                from app.jwt_revocation import emit_task_terminated_notify

                emit_task_terminated_notify(session, str(task.id))
            return True
        registration.result_state = "finalized"
        logs = await build_compute_result_logs_async(job)
        # _handle_compute_experiment_completion gates the artifact and sets the
        # task to its honest terminal status (COMPLETED on pass, FAILED on a missed
        # gate) and stamps the job result reference. With owns_commit=False it does
        # NOT commit, broadcast, or persist knowledge — the caller (sweep) commits,
        # then runs run_compute_wake_post_commit for the broadcast + knowledge.
        await _handle_compute_experiment_completion(
            task, logs, session, owns_commit=False
        )
        # Seed the recoverability marker atomically with the FIRED→DELIVERED commit
        # so a crash ANYWHERE after this point still leaves a marker for the retry
        # pass in sweep_deliver_task_wakes to pick up and re-attempt the post-commit
        # side effects (operator broadcast + durable KnowledgeDocument).
        _meta = dict(registration.source_metadata or {})
        _meta[_COMPUTE_WAKE_POST_COMMIT_META_KEY] = {}
        registration.source_metadata = _meta
        flag_modified(registration, "source_metadata")
        session.add(registration)
        return True

    if action == WakeAction.FINALIZE:
        terminal = TaskStatus.COMPLETED
        raw = (registration.source_metadata or {}).get("finalize_status")
        if raw:
            with contextlib.suppress(Exception):
                candidate = TaskStatus(raw)
                if candidate in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    terminal = candidate
        task.status = terminal
        task.parked_at = None
        task.park_reason = None
        task.latest_status_message = (
            f"Woken by {registration.wake_source_kind}: finalized {terminal.value}"
        )[:500]
        with contextlib.suppress(Exception):
            from app.jwt_revocation import emit_task_terminated_notify

            emit_task_terminated_notify(session, str(task.id))
        registration.result_state = "finalized"
    else:  # RESUME — return the task to the dispatch queue on a fresh VM.
        reset_task_for_fresh_attempt(task)
        task.status = TaskStatus.QUEUED
        task.latest_status_message = (
            f"Woken by {registration.wake_source_kind}: re-queued for dispatch"
        )[:500]
        registration.result_state = "resumed"

    registration.delivery_state = WakeDeliveryState.DELIVERED
    registration.delivered_at = now
    registration.claimed_at = registration.claimed_at or now
    session.add(task)
    session.add(registration)
    return True


async def expire_wake(session, registration: TaskWakeRegistration, *, now=None) -> bool:
    """Age out a PENDING registration whose deadline passed before it fired.

    The PARKED task is failed with a truthful ``park_wake_timeout`` classification
    rather than left to strand. Idempotent: a non-PENDING registration is a no-op.
    Returns ``True`` iff this call expired the registration.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()
    if registration.delivery_state != WakeDeliveryState.PENDING:
        return False

    task = (
        (
            await session.execute(
                select(Task).where(Task.id == registration.task_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    registration.delivery_state = WakeDeliveryState.EXPIRED
    registration.result_state = "expired"
    registration.delivered_at = now
    if task is not None and task.status == TaskStatus.PARKED:
        from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

        failure_detail = None
        diagnostics = None
        if registration.wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND:
            from app.models import ComputeJob

            job = None
            if registration.wake_source_ref:
                with contextlib.suppress(ValueError, TypeError):
                    job = await session.get(
                        ComputeJob, uuid.UUID(registration.wake_source_ref)
                    )
            deadline_at = registration.deadline_at
            job_completed_at = job.completed_at if job is not None else None
            job_started_at = job.started_at if job is not None else None
            job_created_at = job.created_at if job is not None else None
            job_status = getattr(job.status, "value", job.status) if job else None
            job_terminal_by_deadline = bool(
                job is not None
                and job_completed_at is not None
                and deadline_at is not None
                and job_completed_at <= deadline_at
            )
            # Provisioning vs execution split: how much of the deadline was spent
            # waiting for the job to start (provisioning) vs actually running.
            # Clamped to 0 to guard against clock-skew or backdated test fixtures.
            provisioning_seconds: float | None = None
            execution_seconds: float | None = None
            if job_created_at is not None and deadline_at is not None:
                if job_started_at is not None:
                    provisioning_seconds = max(
                        0.0,
                        round((job_started_at - job_created_at).total_seconds(), 1),
                    )
                    execution_seconds = max(
                        0.0,
                        round((deadline_at - job_started_at).total_seconds(), 1),
                    )
                else:
                    # Job never reached running state — all budget went to provisioning.
                    provisioning_seconds = max(
                        0.0,
                        round((deadline_at - job_created_at).total_seconds(), 1),
                    )
            diagnostics = {
                "wake_source_kind": registration.wake_source_kind,
                "wake_source_ref": registration.wake_source_ref,
                "requested_at": registration.requested_at.isoformat(),
                "claimed_at": (
                    registration.claimed_at.isoformat()
                    if registration.claimed_at
                    else None
                ),
                "fired_at": (
                    registration.fired_at.isoformat() if registration.fired_at else None
                ),
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "job_status": job_status,
                "job_created_at": job_created_at.isoformat()
                if job_created_at
                else None,
                "job_started_at": job_started_at.isoformat()
                if job_started_at
                else None,
                "job_completed_at": (
                    job_completed_at.isoformat() if job_completed_at else None
                ),
                "job_terminal_by_deadline": job_terminal_by_deadline,
                "provisioning_seconds": provisioning_seconds,
                "execution_seconds": execution_seconds,
            }
            if job_terminal_by_deadline:
                failure_detail = "compute_job_terminal_wake_lost_after_job_completed"
                task.latest_status_message = (
                    "Park wake on compute_job_terminal expired after the job "
                    "completed, but the wake never fired before the deadline "
                    f"(job_completed_at={diagnostics['job_completed_at']}, "
                    f"deadline_at={diagnostics['deadline_at']}, "
                    f"requested_at={diagnostics['requested_at']}, "
                    f"fired_at={diagnostics['fired_at']})"
                )[:500]
            else:
                failure_detail = "compute_job_terminal_job_not_terminal_by_deadline"
                task.latest_status_message = (
                    "Park wake on compute_job_terminal expired before the job "
                    "reached terminal state "
                    f"(job_status={diagnostics['job_status']}, "
                    f"job_completed_at={diagnostics['job_completed_at']}, "
                    f"deadline_at={diagnostics['deadline_at']}, "
                    f"requested_at={diagnostics['requested_at']}, "
                    f"fired_at={diagnostics['fired_at']})"
                )[:500]
        else:
            task.latest_status_message = (
                f"Park wake on {registration.wake_source_kind} timed out at deadline"
            )[:500]
        task.status = TaskStatus.FAILED
        task.parked_at = None
        # Clear park_reason too so an expired task carries no stale "Parked …"
        # marker on operator surfaces — the task is now FAILED, not parked.
        task.park_reason = None
        ctx = dict(task.context or {})
        ctx["failure_category"] = "park_wake_timeout"
        ctx["failure_class"] = "park_wake_timeout"
        if failure_detail is not None:
            ctx["failure_detail"] = failure_detail
            ctx["park_wake_timeout_diagnostics"] = diagnostics
        task.context = ctx
        registration.result_detail = task.latest_status_message
        with contextlib.suppress(Exception):
            from app.jwt_revocation import emit_task_terminated_notify

            emit_task_terminated_notify(session, str(task.id))
        session.add(task)
    session.add(registration)
    return True
