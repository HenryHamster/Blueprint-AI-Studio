"""Post-commit-pending sweep cluster extracted from recovery_post_commit.py.

Extracted from ``app.recovery_post_commit`` so the post-commit-pending sweep
cluster is independent of the orphan-recovery, stranded-sidecar, lane-baseline,
and bootstrap sweeps.  Holds:

* ``_claim_and_bump_total_attempts`` — isolated claim-time attempt accounting
* ``_build_post_commit_abandoned_context`` — pure context-dict builder
* ``_sweep_post_commit_pending`` — main pending-marker sweep (runs every 60s)
* ``_recover_childless_evaluating_without_marker`` — marker-less childless guard

All four names are re-exported from ``app.recovery_post_commit`` (and in turn
from ``app.recovery_sweeps`` and ``app.services``) via back-compat re-imports so
existing call sites and test patch targets keep resolving unchanged.

``_post_commit_retry_is_transient`` and ``_AWAITING_SIDECAR_KEYS`` stay in
``app.recovery_post_commit`` (shared with other sweeps); they are imported
function-locally here to avoid a circular-import at module initialisation time.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, text
from sqlalchemy.orm import aliased
from sqlmodel import select

from app import services
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Wall-clock backstop for the post-commit sweep. A pending marker older than
# this is terminalized by age even when its attempts/total_attempts counters
# never advanced — which is exactly what happens when the bump UPDATE that
# advances total_attempts is itself starved by the same row-lock contention it
# is trying to escape (asyncpg LockNotAvailableError on the bump → total_attempts
# frozen → the MAX_TOTAL_ATTEMPTS ceiling can never fire → the sweep churns the
# task forever, burning a lock_timeout every cycle). Root-cause fix for the
# 2026-06-14 stuck-task lock-churn incident (task 73281cea). Generous vs. any
# legitimate post-commit (240s per-call budget, retried at the 60s sweep cadence
# — a healthy marker clears in minutes, never 90 min).
POST_COMMIT_MAX_PENDING_AGE_S = 90 * 60


async def _claim_and_bump_total_attempts(task_id, pending_meta) -> dict | None:
    """Try to claim the task row and advance total_attempts atomically.

    Opens an isolated session, dialect-gates SET LOCAL lock_timeout, and issues
    SELECT ... FOR UPDATE SKIP LOCKED on the task row. Returns None if the row
    is held by another writer (caller should defer this marker without doing any
    work — zero lock_timeout burned, zero DB connections wasted). Returns a dict
    with the bumped ``total_attempts`` value when the lock is acquired; the commit
    releases the row lock in milliseconds so _process_post_commit's own
    skip_locked attempt succeeds immediately after.

    Write-ahead durability: the claim-commit advances total_attempts BEFORE work
    starts. A hard kill after this commit but before _process_post_commit means
    the next sweep sees total_attempts already incremented — correct; the ceiling
    fires in bounded cycles regardless of whether the work ran.
    """
    async with services.AsyncSessionLocal() as claim_session:
        if resolve_dialect_name(claim_session) == "postgresql":
            await claim_session.execute(text("SET LOCAL lock_timeout = '5s'"))
        locked = (
            (
                await claim_session.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is None:
            return None  # Row held by peer — defer this cycle

        new_ctx = dict(locked.context or {})
        new_meta = dict(new_ctx.get("_post_commit_pending_meta") or pending_meta)
        # Seed from on-disk value; fall back to attempts for pre-rework markers
        # that have no total_attempts field yet.
        _current_ta = int(
            new_meta.get("total_attempts") or new_meta.get("attempts", 0) or 0
        )
        new_ta = _current_ta + 1
        new_meta["total_attempts"] = new_ta
        new_ctx["_post_commit_pending_meta"] = new_meta
        locked.context = new_ctx
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(locked, "context")
        claim_session.add(locked)
        await claim_session.commit()
        return {"total_attempts": new_ta}


def _build_post_commit_abandoned_context(
    ctx: dict,
    *,
    after_attempts: int | None = None,
    after_total_attempts: int | None = None,
    after_pending_age_s: int | None = None,
    reason: str | None = None,
    last_reason: str | None = None,
) -> dict:
    """Build context dict with _post_commit_abandoned marker.

    Returns a new context dict with:
    - _post_commit_abandoned key set with provided fields (always includes "at")
    - _post_commit_pending_meta key removed

    Does not touch any session or database.
    """
    new_ctx = dict(ctx or {})
    abandoned = {"at": _utcnow().isoformat()}

    if after_attempts is not None:
        abandoned["after_attempts"] = after_attempts
    if after_total_attempts is not None:
        abandoned["after_total_attempts"] = after_total_attempts
    if after_pending_age_s is not None:
        abandoned["after_pending_age_s"] = after_pending_age_s
    if reason is not None:
        abandoned["reason"] = reason
    if last_reason is not None:
        abandoned["last_reason"] = last_reason

    new_ctx["_post_commit_abandoned"] = abandoned
    new_ctx.pop("_post_commit_pending_meta", None)
    return new_ctx


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

    # Function-local to break the initialisation-time import cycle:
    # recovery_post_commit_pending ← recovery_post_commit ← recovery_post_commit_pending
    from app.recovery_post_commit import (
        _post_commit_retry_is_transient,
        _AWAITING_SIDECAR_KEYS,
    )

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
                    # Parked-awaiting sidecar guard: an EVALUATING task that holds
                    # an awaiting_* key is intentionally parked waiting for a
                    # side-car verdict — it is NOT a dead childless handler. The
                    # stranded-sidecar sweep owns redelivery when the sidecar dies.
                    # Clear the stale post_commit_pending flag without re-running.
                    _awaiting_sidecar = any(ctx.get(k) for k in _AWAITING_SIDECAR_KEYS)
                    if _awaiting_sidecar:
                        logger.info(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} is "
                            f"EVALUATING and parked awaiting sidecar — clearing "
                            f"stale pending marker without re-run"
                        )
                        from sqlalchemy.orm.attributes import flag_modified as _fm2

                        new_ctx = dict(ctx)
                        new_ctx.pop("_post_commit_pending_meta", None)
                        task.context = new_ctx
                        task.post_commit_pending = False
                        _fm2(task, "context")
                        session.add(task)
                        await session.commit()
                        continue

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

                # Wall-clock backstop (see POST_COMMIT_MAX_PENDING_AGE_S). The
                # attempts/total_attempts ceilings below can only fire once the
                # bump UPDATE that advances them commits — but that bump is the
                # very write the row-lock contention starves, so a wedged marker
                # would churn forever. Terminalize by age in an ISOLATED session
                # with a fail-fast lock_timeout, so an abandon write that hits the
                # same contention neither poisons the batch nor burns ~30s of
                # cycle time (mirrors the bump isolation that
                # test_post_commit_sweep_bump_failure_does_not_poison_batch guards).
                # Prefer first_pending_at (preserve-once); fall back to "at" for
                # legacy markers written before that field existed.
                _age_anchor = pending_meta.get("first_pending_at") or pending_meta.get(
                    "at"
                )
                _pending_age_s = None
                if _age_anchor:
                    try:
                        _pending_age_s = (
                            _utcnow() - datetime.fromisoformat(_age_anchor)
                        ).total_seconds()
                    except (ValueError, TypeError):
                        _pending_age_s = None
                if (
                    _pending_age_s is not None
                    and _pending_age_s >= POST_COMMIT_MAX_PENDING_AGE_S
                ):
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} pending for "
                        f"{int(_pending_age_s)}s (>= {POST_COMMIT_MAX_PENDING_AGE_S}s) "
                        f"with attempts={attempts}, total_attempts={total_attempts} — "
                        f"bump is likely row-lock-starved; terminalizing by age "
                        f"(post_commit_abandoned)"
                    )
                    try:
                        async with services.AsyncSessionLocal() as age_session:
                            # Postgres-only fast-fail on row contention; on SQLite
                            # (test DB) SET LOCAL is a no-op syntax error, so
                            # dialect-gate it (mirrors the bump path).
                            if resolve_dialect_name(age_session) == "postgresql":
                                await age_session.execute(
                                    text("SET LOCAL lock_timeout = '5s'")
                                )
                            refreshed = await age_session.get(Task, task.id)
                            if refreshed and refreshed.post_commit_pending:
                                refreshed.context = (
                                    _build_post_commit_abandoned_context(
                                        refreshed.context,
                                        after_attempts=attempts,
                                        after_total_attempts=total_attempts,
                                        after_pending_age_s=int(_pending_age_s),
                                        reason="max_pending_age_exceeded",
                                        last_reason=pending_meta.get("reason"),
                                    )
                                )
                                refreshed.post_commit_pending = False
                                refreshed.post_commit_failed = True
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(refreshed, "context")
                                age_session.add(refreshed)
                                await age_session.commit()
                    except Exception as _age_exc:
                        # The abandon write hit the same contention. Don't poison
                        # the batch and don't fall through to the heavy retry for a
                        # task we've already decided to terminalize — the next
                        # sweep re-attempts the abandon once the lock frees.
                        logger.warning(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} age-abandon "
                            f"write blocked (likely row-lock contention): "
                            f"{type(_age_exc).__name__}: {_age_exc} — retry next sweep"
                        )
                    continue

                if (
                    attempts >= MAX_ATTEMPTS
                    or total_attempts >= services.MAX_TOTAL_ATTEMPTS
                ):
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
                    task.context = _build_post_commit_abandoned_context(
                        ctx,
                        after_attempts=attempts,
                        after_total_attempts=total_attempts,
                        last_reason=pending_meta.get("reason"),
                    )
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
                    task.context = _build_post_commit_abandoned_context(
                        ctx, reason="no_saved_payload"
                    )
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

                # Claim-time attempt accounting: advance total_attempts BEFORE
                # doing any work. This decouples the ceiling from the contended
                # row — if the row is held by a dead connection or a peer, we
                # defer cleanly (zero lock_timeout burned) rather than churning.
                # total_attempts reaching MAX_TOTAL_ATTEMPTS terminalizes the
                # marker in a bounded number of CLAIMABLE cycles regardless of
                # whether any work ever succeeded. The age backstop remains as
                # belt-and-suspenders for the permanently-unholdable case.
                _task_id_short = str(task.id)[:8]
                try:
                    claimed = await _claim_and_bump_total_attempts(
                        task.id, pending_meta
                    )
                except Exception as claim_exc:
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {_task_id_short} claim-time "
                        f"bump raised {type(claim_exc).__name__}: {claim_exc} — "
                        f"deferring (batch continues)"
                    )
                    continue

                if claimed is None:
                    logger.info(
                        f"[POST_COMMIT SWEEP] Task {_task_id_short} row held at "
                        f"claim time (total_attempts={total_attempts}, "
                        f"age={int(_pending_age_s or 0)}s) — deferring"
                    )
                    continue

                # Reload total_attempts from the claim-commit result; re-check
                # the ceiling with the freshly-bumped value. This handles the
                # "one-under" case: a task at total_attempts == MAX_TOTAL_ATTEMPTS-1
                # is claimed, bumped to MAX_TOTAL_ATTEMPTS, and terminalized here
                # without invoking _process_post_commit.
                total_attempts = claimed["total_attempts"]
                if total_attempts >= services.MAX_TOTAL_ATTEMPTS:
                    _cap_label = "total_attempts"
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {_task_id_short} reached "
                        f"{services.MAX_TOTAL_ATTEMPTS} {_cap_label} after claim-time "
                        f"bump (attempts={attempts}) — marking post_commit_abandoned"
                    )
                    async with services.AsyncSessionLocal() as ceiling_session:
                        refreshed = await ceiling_session.get(Task, task.id)
                        if refreshed and refreshed.post_commit_pending:
                            refreshed.context = _build_post_commit_abandoned_context(
                                refreshed.context,
                                after_attempts=attempts,
                                after_total_attempts=total_attempts,
                                last_reason=pending_meta.get("reason"),
                            )
                            refreshed.post_commit_pending = False
                            refreshed.post_commit_failed = True
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(refreshed, "context")
                            ceiling_session.add(refreshed)
                            await ceiling_session.commit()
                    continue

                logger.info(
                    f"[POST_COMMIT SWEEP] Retrying post-commit for task {_task_id_short} "
                    f"(attempt {attempts + 1}/{MAX_ATTEMPTS}, total_attempts={total_attempts}/{services.MAX_TOTAL_ATTEMPTS}, "
                    f"reason={pending_meta.get('reason')})"
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
                        what=f"post-commit {_task_id_short}",
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
                    logger.info(f"[POST_COMMIT SWEEP] Task {_task_id_short} recovered")
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
                            f"[POST_COMMIT SWEEP] Task {_task_id_short} retry hit a "
                            f"transient failure ({type(e).__name__}) — NOT counting "
                            f"against the {MAX_ATTEMPTS}-attempt cap; will retry next sweep "
                            f"(total_attempts {total_attempts}/{services.MAX_TOTAL_ATTEMPTS})"
                        )
                        # total_attempts already bumped at claim time — no
                        # additional bump needed here. The ceiling fires on the
                        # next sweep where the row is claimable.
                        continue
                    # Persistent failure — bump attempts so we eventually give up.
                    # total_attempts was already advanced at claim time, so only
                    # attempts needs bumping here. The retry path in
                    # _process_post_commit's exception handler may have already
                    # bumped attempts; this covers cases where it didn't.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {_task_id_short} retry failed: {e}"
                    )
                    # Bump in its own try/except so a bump failure on one task
                    # doesn't poison the entire sweep batch. Short lock_timeout
                    # (5s) so we fail fast on row contention instead of burning
                    # 30s of orchestrator-cycle time per task.
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
                                # total_attempts was already bumped at claim time
                                # — do NOT bump again here.
                                new_pending["last_retry_at"] = _utcnow().isoformat()
                                new_ctx["_post_commit_pending_meta"] = new_pending
                                refreshed.context = new_ctx
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(refreshed, "context")
                                bump_session.add(refreshed)
                                await bump_session.commit()
                    except Exception as bump_exc:
                        logger.warning(
                            f"[POST_COMMIT SWEEP] Task {_task_id_short} "
                            f"attempt-bump failed (likely row-lock contention): "
                            f"{type(bump_exc).__name__}: {bump_exc}"
                        )
                        # Continue to next task — attempts stays where it
                        # was, but the sweep proceeds. If contention persists
                        # the operator must clear post_commit_pending via the
                        # admin PATCH (admin.py modify_task).
    except Exception as e:
        logger.error(f"[POST_COMMIT SWEEP] Sweep failed: {e}", exc_info=True)

    # Marker-LESS childless-EVALUATING recovery. Runs after the marker-based
    # loop above (same 60s cadence). See the helper docstring for the gap it
    # closes; isolated in its own try so a failure here never aborts the
    # marker-based recovery and vice-versa.
    try:
        await _recover_childless_evaluating_without_marker()
    except Exception as e:
        logger.error(
            f"[POST_COMMIT SWEEP] Marker-less childless-EVALUATING recovery "
            f"failed: {e}",
            exc_info=True,
        )


# A childless EVALUATING plan parent younger than this may still be mid-spawn:
# _handle_plan_completion flips the parent COMPLETED→EVALUATING, releases the
# lock inside _ensure_branch, makes GitHub calls, then spawns children — a
# window that can legitimately last tens of seconds. Re-running before this
# grace elapses would race the in-flight spawn and risk duplicate children, so
# only a parent that has been childless+EVALUATING for longer than this is
# treated as genuinely stranded. Comfortably exceeds the _process_post_commit
# call budget (260s) so an in-flight pass has provably ended.
_CHILDLESS_EVALUATING_NO_MARKER_GRACE_S = 600
_CHILDLESS_EVALUATING_MAX_RECOVERY_ATTEMPTS = 20
_CHILDLESS_RECOVERY_CLAIM_TTL_S = 360
_CHILDLESS_RECOVERY_META_KEY = "_childless_post_commit_recovery"


async def _claim_childless_evaluating_recovery(task_id) -> dict | None:
    """Atomically revalidate and claim one marker-less recovery attempt."""
    async with services.AsyncSessionLocal() as session:
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
        if locked is None:
            return None
        plan_output = locked.plan_output
        if (
            locked.status != TaskStatus.EVALUATING
            or locked.post_commit_pending
            or (locked.first_payload or {}).get("type") != "plan"
            or not isinstance(plan_output, dict)
            or not isinstance(plan_output.get("subtasks"), list)
            or not plan_output.get("subtasks")
        ):
            return None
        ctx = dict(locked.context or {})
        meta = dict(ctx.get(_CHILDLESS_RECOVERY_META_KEY) or {})
        child_count = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.parent_task_id == task_id)
            )
        ).scalar_one()
        if child_count:
            # A previous recovery may have succeeded and then died before its
            # caller could observe the return. Child creation is the durable
            # success signal; remove retry metadata so successful parents do
            # not occupy the bounded candidate page forever.
            if _CHILDLESS_RECOVERY_META_KEY in ctx:
                ctx.pop(_CHILDLESS_RECOVERY_META_KEY, None)
                locked.context = ctx
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(locked, "context")
                session.add(locked)
                await session.commit()
            return None

        # The ten-minute grace protects only the FIRST recovery attempt. Claim
        # and failure bookkeeping updates Task.updated_at, so applying the age
        # guard to retries would accidentally delay every retry by ten minutes.
        if not meta and locked.updated_at >= _utcnow() - timedelta(
            seconds=_CHILDLESS_EVALUATING_NO_MARKER_GRACE_S
        ):
            return None
        claimed_at = meta.get("claimed_at")
        if claimed_at:
            try:
                claim_age_s = (
                    _utcnow() - datetime.fromisoformat(claimed_at)
                ).total_seconds()
            except (TypeError, ValueError):
                claim_age_s = _CHILDLESS_RECOVERY_CLAIM_TTL_S
            if claim_age_s < _CHILDLESS_RECOVERY_CLAIM_TTL_S:
                return None
        attempts = int(meta.get("attempts") or 0)
        if attempts >= _CHILDLESS_EVALUATING_MAX_RECOVERY_ATTEMPTS:
            ctx["_childless_post_commit_abandoned"] = {
                "at": _utcnow().isoformat(),
                "after_attempts": attempts,
                "last_error": meta.get("last_error"),
            }
            ctx.pop(_CHILDLESS_RECOVERY_META_KEY, None)
            locked.context = ctx
            locked.status = TaskStatus.FAILED
            locked.post_commit_failed = True
            locked.latest_status_message = (
                "Failed: childless plan recovery exhausted "
                f"{attempts} attempts; requeue required"
            )
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(locked, "context")
            session.add(locked)
            await session.commit()
            logger.error(
                "[POST_COMMIT SWEEP] Marker-less childless plan %s exhausted "
                "%s attempts; terminalized instead of retrying forever",
                str(task_id)[:8],
                attempts,
            )
            return {"terminalized": True}

        claim_token = uuid.uuid4().hex
        now_iso = _utcnow().isoformat()
        meta.setdefault("first_attempt_at", now_iso)
        meta["last_attempt_at"] = now_iso
        meta["claimed_at"] = now_iso
        meta["claim_token"] = claim_token
        meta["attempts"] = attempts + 1
        ctx[_CHILDLESS_RECOVERY_META_KEY] = meta
        locked.context = ctx
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(locked, "context")
        session.add(locked)
        await session.commit()
        return {
            "plan_output": plan_output,
            "attempts": attempts + 1,
            "claim_token": claim_token,
        }


async def _record_childless_recovery_failure(
    task_id, claim_token: str, exc: Exception
) -> None:
    async with services.AsyncSessionLocal() as session:
        task = (
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
        if task is None:
            return
        ctx = dict(task.context or {})
        meta = dict(ctx.get(_CHILDLESS_RECOVERY_META_KEY) or {})
        # A cancel/requeue/archive may win after the claimed post-commit call
        # starts (including while it is blocked on lifecycle locks). Never
        # recreate stale attempt metadata on that newer task state.
        if (
            task.status != TaskStatus.EVALUATING
            or task.archived
            or task.post_commit_pending
            or meta.get("claim_token") != claim_token
        ):
            return
        meta["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        meta["last_failure_at"] = _utcnow().isoformat()
        meta.pop("claimed_at", None)
        if getattr(exc, "lock_scope", None):
            meta["last_lock_scope"] = exc.lock_scope
        ctx[_CHILDLESS_RECOVERY_META_KEY] = meta
        task.context = ctx
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(task, "context")
        session.add(task)
        await session.commit()


async def _recover_childless_evaluating_without_marker():
    """Recover a plan parent stranded childless in EVALUATING with NO pending marker.

    Gap (related to incident 2026-05-27, marker-based variant handled in
    ``_sweep_post_commit_pending`` above): ``_handle_plan_completion`` flips its
    parent COMPLETED→EVALUATING, persists ``plan_output`` (the parsed
    ``{"subtasks": [...]}``), and releases the row lock inside ``_ensure_branch``
    BEFORE the child-spawn loop runs. The status endpoint sets a write-ahead
    ``_post_commit_pending`` marker for this, so most hard kills at that boundary
    ARE recovered by the marker-based loop. But the marker can be absent —
    cleared by a partially-successful pass, or a flip-to-EVALUATING path that
    never set it — leaving a childless EVALUATING parent that the reaper cannot
    rescue: reaper Step 3 needs a COMPLETED child and Step 4 explicitly skips
    childless parents (reaper.py ``if not children_snap: continue``).

    This guard finds such parents by their PERSISTED state — the agent reported
    COMPLETED (``plan_output`` with subtasks is persisted only on that path) and
    the row is EVALUATING with ZERO children — and re-runs the completion handler
    (via ``_process_post_commit`` on a synthetic COMPLETED payload reconstructed
    from ``plan_output``) to spawn the missing children.

    SAFETY (mirrors the marker-based childless guard):
      * ZERO-children only. ``_handle_plan_completion`` spawns all children in
        one atomic commit, so a non-zero child count means the spawn already ran
        and re-running would DUPLICATE children — those parents are skipped.
      * CANCELLED parents are excluded (status filter EVALUATING only) so an
        admin cancel is never zombied back into spawning children.
      * Age guard: only parents childless+EVALUATING for longer than
        ``_CHILDLESS_EVALUATING_NO_MARKER_GRACE_S`` are touched, so a normal
        in-flight spawn (which legitimately runs lock-released for tens of
        seconds) is never raced into duplicate children.
      * Marker present → skip (the marker-based loop owns it; no double-run).
    """
    # Import from agent_status (which re-exports the task_lifecycle impl) so a
    # test patching ``app.routers.agent_status._process_post_commit`` — the
    # canonical patch target used by the marker-based sweep tests — also
    # intercepts this re-run path.
    from app.routers.agent_status import _process_post_commit
    from app.orchestrator import _mark_orchestrator_progress
    from app.schemas import AgentStatusPayload

    now = _utcnow()
    grace_cutoff = now - timedelta(seconds=_CHILDLESS_EVALUATING_NO_MARKER_GRACE_S)
    try:
        # Keep this transaction short: fetch only candidate ids, then release
        # the connection before any bounded post-commit call (up to 260s).
        # Retry metadata bypasses the initial age gate because bookkeeping
        # writes refresh Task.updated_at; the claim transaction remains the
        # authoritative state/lease check.
        async with services.AsyncSessionLocal() as session:
            if resolve_dialect_name(session) == "postgresql":
                subtasks = Task.plan_output["subtasks"]
                subtask_count = case(
                    (
                        func.jsonb_typeof(subtasks) == "array",
                        func.jsonb_array_length(subtasks),
                    ),
                    else_=0,
                )
            else:
                subtask_count = case(
                    (
                        func.json_type(Task.plan_output, "$.subtasks") == "array",
                        func.json_array_length(Task.plan_output, "$.subtasks"),
                    ),
                    else_=0,
                )
            nonempty_subtasks = subtask_count > 0
            candidate_child = aliased(Task)
            no_children = ~(
                select(candidate_child.id)
                .where(candidate_child.parent_task_id == Task.id)
                .correlate(Task)
                .exists()
            )
            retry_attempt_exists = (
                Task.context[_CHILDLESS_RECOVERY_META_KEY]["attempts"]
                .as_integer()
                .is_not(None)
            )
            candidate_ids = (
                await session.execute(
                    select(Task.id)
                    .where(
                        Task.status == TaskStatus.EVALUATING,
                        Task.archived.is_(False),
                        Task.post_commit_pending.is_(False),
                        Task.payloads[0]["type"].as_string() == "plan",
                        Task.plan_output.is_not(None),
                        nonempty_subtasks,
                        no_children,
                        or_(Task.updated_at < grace_cutoff, retry_attempt_exists),
                    )
                    .order_by(Task.updated_at.asc())
                    .limit(50)
                )
            )
            candidate_ids = candidate_ids.scalars().all()
        if not candidate_ids:
            return

        for task_id in candidate_ids:
            try:
                _mark_orchestrator_progress()
                claim = await _claim_childless_evaluating_recovery(task_id)
                if not claim or claim.get("terminalized"):
                    continue
                plan_output = claim["plan_output"]

                logger.warning(
                    f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING plan "
                    f"{str(task_id)[:8]} (reported COMPLETED, plan_output present, "
                    f"0 children, EVALUATING > "
                    f"{_CHILDLESS_EVALUATING_NO_MARKER_GRACE_S}s) — re-running "
                    f"completion handler to spawn missing children"
                )
                # Reconstruct a synthetic COMPLETED payload from the persisted
                # plan_output. ``_handle_plan_completion`` re-parses
                # ``payload.logs`` via ``_parse_plan_output`` (string-aware JSON
                # extraction), so a JSON-serialized plan_output round-trips.
                try:
                    synthetic = AgentStatusPayload(
                        task_id=task_id,
                        status=TaskStatus.COMPLETED,
                        logs=json.dumps(plan_output),
                    )
                except Exception as build_exc:
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Could not build synthetic payload "
                        f"for plan {str(task_id)[:8]}: {build_exc}"
                    )
                    continue
                try:
                    await services._bounded(
                        _process_post_commit(
                            task_id,
                            synthetic,
                            _expected_childless_recovery_token=claim["claim_token"],
                        ),
                        services._POST_COMMIT_CALL_TIMEOUT_S,
                        what=f"childless-eval re-run {str(task_id)[:8]}",
                    )
                    logger.info(
                        f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING "
                        f"plan {str(task_id)[:8]} re-run completed"
                    )
                except Exception as rerun_exc:
                    await _record_childless_recovery_failure(
                        task_id,
                        claim["claim_token"],
                        rerun_exc,
                    )
                    # Best-effort: next sweep retries (the parent stays childless
                    # EVALUATING until children spawn). The age guard means it is
                    # already past the in-flight window, so a retry can't race a
                    # live spawn.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING "
                        f"plan {str(task_id)[:8]} re-run failed (will retry next "
                        f"sweep): {type(rerun_exc).__name__}: {rerun_exc}"
                    )
            except Exception:
                logger.exception(
                    "[POST_COMMIT SWEEP] Marker-less recovery candidate %s failed",
                    str(task_id)[:8],
                )
    except Exception as e:
        logger.error(
            f"[POST_COMMIT SWEEP] Marker-less childless-EVALUATING sweep query "
            f"failed: {e}",
            exc_info=True,
        )
