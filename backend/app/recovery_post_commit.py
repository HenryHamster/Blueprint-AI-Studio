"""Post-commit / orphan / sidecar / bootstrap recovery sweeps.

Extracted from ``app.recovery_sweeps`` alongside ``app.recovery_evaluating`` to
keep each module below its size cap. Holds the post-commit-pending sweep
(``_sweep_post_commit_pending``), the orphan-recovery sweep
(``_recover_orphaned_tasks``), the childless-EVALUATING backstop, the
stranded-sidecar sweep, the lane-baseline backfill, and the bootstrap-recovery
sweep, plus the post-commit transient-retry classifier.

Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing call sites and test patch targets keep resolving
unchanged. Shared bounding infrastructure lives in ``app.services`` and is
referenced through the ``services`` module object so test patches against
``app.services.<name>`` (and ``app.recovery_sweeps.services.<name>``) still take
effect on the moved code.
"""

import hashlib as _hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, func, text
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app import services
from app.database import resolve_dialect_name
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
                                _new_ctx = dict(refreshed.context or {})
                                _new_ctx["_post_commit_abandoned"] = {
                                    "at": _utcnow().isoformat(),
                                    "after_attempts": attempts,
                                    "after_total_attempts": total_attempts,
                                    "after_pending_age_s": int(_pending_age_s),
                                    "reason": "max_pending_age_exceeded",
                                    "last_reason": pending_meta.get("reason"),
                                }
                                _new_ctx.pop("_post_commit_pending_meta", None)
                                refreshed.context = _new_ctx
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
                                        new_pending.get(
                                            "total_attempts", total_attempts
                                        )
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
        async with services.AsyncSessionLocal() as session:
            # Candidate parents: EVALUATING, not archived, NO pending marker
            # (the marker-based loop owns marked rows), updated long enough ago
            # to clear the in-flight-spawn grace. ``plan_output`` must be loaded
            # — it is the COMPLETED-report signal and the re-run source. Bounded.
            candidates = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.archived.is_(False),
                            Task.post_commit_pending.is_(False),
                            Task.updated_at < grace_cutoff,
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            if not candidates:
                return

            for task in candidates:
                _mark_orchestrator_progress()
                # Only plan parents that actually reported a non-empty plan.
                # ``plan_output`` is persisted at the COMPLETED→EVALUATING flip
                # (plan_completion.py) and only for the spawn path; its presence
                # with subtasks is the "saved payload.status == COMPLETED" signal
                # for a marker-less row.
                plan_output = task.plan_output
                if not (
                    isinstance(plan_output, dict)
                    and isinstance(plan_output.get("subtasks"), list)
                    and plan_output.get("subtasks")
                ):
                    continue
                task_type = (task.first_payload or {}).get("type", "") or ""
                if task_type != "plan":
                    continue
                # ZERO-children-only guard (load-bearing — avoids duplicates).
                child_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(Task)
                        .where(Task.parent_task_id == task.id)
                    )
                ).scalar_one()
                if child_count:
                    continue

                logger.warning(
                    f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING plan "
                    f"{str(task.id)[:8]} (reported COMPLETED, plan_output present, "
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
                        task_id=task.id,
                        status=TaskStatus.COMPLETED,
                        logs=json.dumps(plan_output),
                    )
                except Exception as build_exc:
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Could not build synthetic payload "
                        f"for plan {str(task.id)[:8]}: {build_exc}"
                    )
                    continue
                try:
                    await services._bounded(
                        _process_post_commit(task.id, synthetic),
                        services._POST_COMMIT_CALL_TIMEOUT_S,
                        what=f"childless-eval re-run {str(task.id)[:8]}",
                    )
                    logger.info(
                        f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING "
                        f"plan {str(task.id)[:8]} re-run completed"
                    )
                except Exception as rerun_exc:
                    # Best-effort: next sweep retries (the parent stays childless
                    # EVALUATING until children spawn). The age guard means it is
                    # already past the in-flight window, so a retry can't race a
                    # live spawn.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Marker-less childless EVALUATING "
                        f"plan {str(task.id)[:8]} re-run failed (will retry next "
                        f"sweep): {type(rerun_exc).__name__}: {rerun_exc}"
                    )
    except Exception as e:
        logger.error(
            f"[POST_COMMIT SWEEP] Marker-less childless-EVALUATING sweep query "
            f"failed: {e}",
            exc_info=True,
        )


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
                                raiseload=True,
                            )
                        )
                        .where(
                            Task.status == TaskStatus.COMPLETED,
                            Task.created_by == "ORCHESTRATOR",
                            Task.created_at <= five_min_ago,
                            # Normal window: tasks created in the last hour.
                            # Extended: also include post_commit_failed=True tasks
                            # regardless of age — the post-commit sweep may abandon
                            # a task hours after creation (agent runtime +
                            # lock-starvation + 90-min backstop ≈ 3.5h observed),
                            # which puts it permanently outside the 1-hour window.
                            # post_commit_failed is write-only without this OR arm;
                            # coupling it here makes the abandon→recovery path
                            # event-driven on the flag rather than on creation age.
                            or_(
                                Task.created_at >= one_hour_ago,
                                Task.post_commit_failed.is_(True),
                            ),
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
                (t.id, (t.first_payload or {}).get("type", "")) for t in candidates
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


# ── Stranded-sidecar recovery sweep ───────────────────────────────────────────
_AWAITING_SIDECAR_KEYS = (
    "awaiting_quality_grader",
    "awaiting_adversarial_grader",
    "awaiting_safeguard_challenge",
    "awaiting_finding_challenge",
    "awaiting_review_second_opinion",
)
_SIDECAR_VERDICT_APPLICATION_PENDING_KEY = "sidecar_verdict_application_pending"


async def _sweep_stranded_sidecar_sources() -> None:
    """Unstrand EVALUATING source tasks whose side-car challenger died silently.

    Side-car challengers (quality grader, safeguard challenge, finding challenge,
    review second-opinion) normally resolve their source task by POSTing terminal
    status to the agent status-update route.  A side-car that never posts — reaper
    kill, VM death after retries exhausted, cancellation — leaves the source
    parked EVALUATING indefinitely.

    This sweep queries EVALUATING tasks whose awaiting_* key holds a UUID string
    (post-spawn; initial value is ``True``), loads each referenced sidecar, and,
    if the sidecar is terminal (FAILED/CANCELLED), calls the lane resolver via
    ``trigger_stranded_sidecar_resolution``, which constructs a synthetic payload
    and invokes the resolver with existing fail-closed semantics. It also retries
    terminal source tasks that already recorded a side-car verdict but still carry
    the durable application-pending marker.
    """
    import uuid as _uuid

    from app.task_lifecycle import trigger_stranded_sidecar_resolution

    _TERMINAL = {TaskStatus.FAILED, TaskStatus.CANCELLED}

    try:
        async with services.AsyncSessionLocal() as session:
            result = await session.execute(
                select(Task).where(Task.status == TaskStatus.EVALUATING)
            )
            evaluating = result.scalars().all()

            resolved = 0
            for source in evaluating:
                ctx = source.context or {}
                for key in _AWAITING_SIDECAR_KEYS:
                    ref = ctx.get(key)
                    if not ref or ref is True:
                        continue
                    try:
                        sidecar_uuid = _uuid.UUID(str(ref))
                    except (ValueError, TypeError):
                        continue
                    sidecar = await session.get(Task, sidecar_uuid)
                    if sidecar is None or sidecar.status not in _TERMINAL:
                        continue
                    logger.warning(
                        "[SIDECAR-RECOVERY] Source %s stranded EVALUATING "
                        "(key=%s sidecar=%s status=%s) — triggering fail-closed resolution",
                        source.id,
                        key,
                        sidecar.id,
                        sidecar.status,
                    )
                    try:
                        await trigger_stranded_sidecar_resolution(sidecar, session)
                        resolved += 1
                    except Exception:
                        logger.exception(
                            "[SIDECAR-RECOVERY] Resolver failed for sidecar %s / source %s",
                            sidecar.id,
                            source.id,
                        )
                    break  # one sidecar per source per sweep pass
            if resolved:
                logger.info(
                    "[SIDECAR-RECOVERY] Resolved %d stranded source task(s)", resolved
                )

            if resolve_dialect_name(session) == "postgresql":
                pending_marker_present = Task.context.has_key(
                    _SIDECAR_VERDICT_APPLICATION_PENDING_KEY
                )
            else:
                pending_marker_present = func.json_extract(
                    Task.context,
                    f"$.{_SIDECAR_VERDICT_APPLICATION_PENDING_KEY}",
                ).isnot(None)

            pending_result = await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.archived,
                        Task.project_id,
                        Task.context,
                        raiseload=True,
                    )
                )
                .where(
                    Task.status == TaskStatus.COMPLETED,
                    Task.archived.is_(False),
                    pending_marker_present,
                )
            )
            terminal_sources = pending_result.scalars().all()
            retried = 0
            for source in terminal_sources:
                ctx = source.context or {}
                marker = ctx.get(_SIDECAR_VERDICT_APPLICATION_PENDING_KEY)
                if not isinstance(marker, dict):
                    continue
                sidecar_ref = marker.get("sidecar_task_id")
                try:
                    sidecar_uuid = _uuid.UUID(str(sidecar_ref))
                except (ValueError, TypeError):
                    continue
                sidecar = await session.get(Task, sidecar_uuid)
                if sidecar is None or sidecar.status not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    continue
                logger.warning(
                    "[SIDECAR-RECOVERY] Source %s has unapplied side-car verdict "
                    "(lane=%s sidecar=%s status=%s) — retrying application",
                    source.id,
                    marker.get("lane"),
                    sidecar.id,
                    sidecar.status,
                )
                # Capture ids as plain strings BEFORE resolution. The resolver's
                # best-effort sub-op rollbacks (and the connection-drop path)
                # expire every ORM instance even under expire_on_commit=False, so
                # a post-failure ``source.id`` / ``sidecar.id`` read fires a sync
                # expired-load inside the async session -> MissingGreenlet, which
                # masks the real error AND aborts the rest of the batch.
                _sidecar_id_str = str(sidecar.id)
                _source_id_str = str(source.id)
                try:
                    await trigger_stranded_sidecar_resolution(sidecar, session)
                    retried += 1
                except Exception:
                    logger.exception(
                        "[SIDECAR-RECOVERY] Application retry failed for sidecar %s / source %s",
                        _sidecar_id_str,
                        _source_id_str,
                    )
                    # Roll back the (possibly failed-transaction) session so the
                    # next source in the batch starts clean rather than cascading
                    # PendingRollbackError through the remaining iterations.
                    try:
                        await session.rollback()
                    except Exception:
                        logger.debug(
                            "[SIDECAR-RECOVERY] post-failure rollback failed",
                            exc_info=True,
                        )
            if retried:
                logger.info(
                    "[SIDECAR-RECOVERY] Retried %d unapplied side-car verdict(s)",
                    retried,
                )
    except Exception:
        logger.exception("[SIDECAR-RECOVERY] Stranded-sidecar sweep failed")


# ── Lane-baseline backfill sweep ─────────────────────────────────────────────

_LANE_BASELINE_BACKFILL_BATCH_SIZE = 50


async def _sweep_lane_baseline_backfill() -> None:
    """Backfill lane baselines for recently-closed versions lacking a stored entry.

    Runs every ~600s. Iterates active (non-archived) projects and calls
    ``backfill_missing_lane_baselines`` for each.  Each project is independent:
    a failure on one project is logged and skipped without blocking others.
    Best-effort/non-fatal — mirrors the pattern of other leader-cycle sweeps.
    """
    from app.lane_baseline_snapshot import backfill_missing_lane_baselines
    from app.models import Project

    try:
        async with services.AsyncSessionLocal() as session:
            project_ids = list(
                (
                    await session.execute(
                        select(Project.id)
                        .where(Project.archived.is_(False))
                        .order_by(Project.id)
                        .limit(_LANE_BASELINE_BACKFILL_BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )

        for project_id in project_ids:
            try:
                backfilled = await backfill_missing_lane_baselines(
                    project_id=project_id
                )
                if backfilled:
                    logger.info(
                        "[LANE_BASELINE] backfill: wrote baselines for v%s on project=%s",
                        backfilled,
                        project_id,
                    )
            except Exception:
                logger.warning(
                    "[LANE_BASELINE] backfill sweep: project=%s failed (non-fatal)",
                    project_id,
                    exc_info=True,
                )
    except Exception:
        logger.exception("[LANE_BASELINE] lane-baseline backfill sweep failed")


# ── Bootstrap recovery sweep ───────────────────────────────────────────────────

_BOOTSTRAP_SWEEP_BATCH_SIZE = 20


def _bootstrap_spawn_lock_key(project_id: object) -> int:
    """Stable 31-bit advisory-lock key for a project's bootstrap spawn.

    Uses SHA-256 (not Python hash()) because hash() is PYTHONHASHSEED-salted
    and two orchestrator processes compute DIFFERENT keys for the same project —
    the advisory lock would never exclude anything across instances.
    """
    digest = _hashlib.sha256(f"bootstrap_spawn:{project_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


async def _try_bootstrap_spawn_advisory_lock(session, project_id: object) -> bool:
    """Try to take the per-project bootstrap-spawn advisory lock (Postgres).

    Transaction-scoped (``pg_try_advisory_xact_lock``) so it is released when
    the transaction commits or rolls back. Best-effort: reduces duplicate
    work between concurrently-ticking orchestrator instances. Non-Postgres
    dialects (tests) report no contention and always return True.
    """
    from sqlalchemy import text as _sql_text

    try:
        got = (
            await session.execute(
                _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _bootstrap_spawn_lock_key(project_id)},
            )
        ).scalar()
    except Exception:
        return True
    return bool(got)


async def _sweep_bootstrap_recovery() -> None:
    """Enqueue one repo_bootstrap task for each eligible unbootstrapped project.

    Eligible projects are: external (not is_self_repo), non-empty
    (repo_is_empty falsy), no bootstrap_completed_at, and no currently-active
    (QUEUED/PROVISIONING/IN_PROGRESS) repo_bootstrap task.

    The sweep is bounded and idempotent: the shared spawn_bootstrap_task helper
    refuses to create a duplicate when one is already active, so repeated ticks
    never pile up duplicate tasks. A per-project transaction-scoped advisory
    lock (``pg_try_advisory_xact_lock``) further guards against the non-atomic
    check-then-act race that lets two concurrent orchestrator instances both
    find no active task and both spawn one in the same second (mirrors the
    strategic double-spawn fix, PR #8927).
    """
    from app.models import Project
    from app.repo_bootstrap import spawn_bootstrap_task

    try:
        async with services.AsyncSessionLocal() as session:
            # Filter ineligible projects IN the query so the LIMIT caps spawnable
            # candidates, not the raw project set. Completed projects accumulate
            # unboundedly over time; applying the limit before filtering them out
            # would starve genuinely-eligible (degraded/failed/never-bootstrapped)
            # projects as the completed set grows past the batch size.
            #
            # bootstrap_completed_at: absent key or null → IS NULL → eligible.
            # repo_is_empty: absent key or 'false' → coalesce to 'false' → != 'true' → eligible.
            # ORDER BY id gives consistent ordering so new projects aren't missed.
            now_iso = _utcnow().isoformat()
            result = await session.execute(
                select(Project)
                .where(
                    Project.is_self_repo.is_(False),
                    Project.archived.is_(False),
                    Project.build_config["bootstrap_completed_at"]
                    .as_string()
                    .is_(None),
                    func.coalesce(
                        Project.build_config["repo_is_empty"].as_string(), "false"
                    )
                    != "true",
                    func.coalesce(
                        Project.build_config["bootstrap_recovery_state"].as_string(), ""
                    )
                    != "exhausted",
                    Project.build_config["bootstrap_recovery_exhausted_at"]
                    .as_string()
                    .is_(None),
                    or_(
                        Project.build_config["bootstrap_recovery_next_at"]
                        .as_string()
                        .is_(None),
                        Project.build_config["bootstrap_recovery_next_at"].as_string()
                        <= now_iso,
                    ),
                )
                .order_by(Project.id)
                .limit(_BOOTSTRAP_SWEEP_BATCH_SIZE)
            )
            candidates = result.scalars().all()

            spawned = 0
            for project in candidates:
                # Per-project advisory lock guards the non-atomic check-then-act:
                # two concurrent orchestrator instances may both read "no active
                # task" before either's spawn commits, then both insert a duplicate.
                # The transaction-scoped lock serialises spawns per project so only
                # one instance wins; the other sees "already_active" on its next
                # spawn_bootstrap_task call and skips. Non-Postgres (tests) returns
                # True (no contention) so test behaviour is unchanged.
                if not await _try_bootstrap_spawn_advisory_lock(session, project.id):
                    logger.debug(
                        "[BOOTSTRAP-RECOVERY] Another instance holds bootstrap lock "
                        "for project %s — skipping this tick",
                        project.id,
                    )
                    continue
                spawn_result = await spawn_bootstrap_task(
                    project,
                    session,
                    is_first_bootstrap=False,
                    spawn_reason="recovery_sweep",
                    automatic_recovery=True,
                )
                if spawn_result.outcome == "spawned":
                    spawned += 1

            await session.commit()
            if spawned:
                logger.info(
                    "[BOOTSTRAP-RECOVERY] Enqueued %d recovery bootstrap task(s)",
                    spawned,
                )
    except Exception:
        logger.exception("[BOOTSTRAP-RECOVERY] Bootstrap recovery sweep failed")
