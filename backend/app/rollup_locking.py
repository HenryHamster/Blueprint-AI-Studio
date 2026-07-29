"""Rollup parent-row locking primitives.

Extracted from rollup_management.py to reduce its size.  Contains the
lock-window-break triplet (_capture_rollup_parent_snapshot,
_release_parent_lock, _relock_parent) and the _ParentMutatedDuringPRCreation
sentinel that guards against peer mutation during the unlocked GitHub I/O
window.
"""

import asyncio
import contextlib
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task

logger = logging.getLogger(__name__)


class _OriginalTaskLockTimeout(Exception):
    """Raised by ``_lock_original_task(skip_locked=False)`` when a BLOCKING
    FOR UPDATE cannot acquire the row within the bounded ``lock_timeout``.

    Only the merge-success caller blocks; it catches this to log LOUDLY and
    leave a recoverable trail rather than silently dropping its one-shot
    side effects (back-merge, security rescan, remediation outcome, etc.).
    """


async def _lock_original_task(
    session: AsyncSession,
    original_task_id: str | uuid.UUID,
    *,
    attempts: int = 3,
    cached_task: Task | None = None,
    source: str = "",
    skip_locked: bool = True,
    lock_timeout_ms: int = 8000,
) -> Task | None:
    """Acquire FOR UPDATE on the rollup-parent / original task.

    Single home for the bounded-retry row-lock loop that the review/merge
    sibling handlers in ``task_lifecycle`` each re-implemented (the four
    copies the C4 audit cites: the rollup-handoff path, the empty-PR cleanup,
    the ci_wait reset, and — formerly an unlocked ``session.get`` — the
    merge-success ``orig`` transition).

    Two lock disciplines, selected by ``skip_locked``:

      * ``skip_locked=True`` (DEFAULT — sweep / recovery / handoff callers):
        ``with_for_update(skip_locked=True)`` + bounded retry. Returns
        ``None`` when the row is held by a peer for all ``attempts`` so the
        caller DEFERS — the parent's own poll picks the state up next cycle.
        These callers' work is idempotent / re-driven next cycle, so yielding
        to a peer is correct and cheap.

      * ``skip_locked=False`` (MERGE-SUCCESS caller only): the merge has
        ALREADY succeeded on GitHub, so the ``orig`` bookkeeping carries
        ONE-SHOT side effects (hotfix back-merge, security rescan spawn,
        remediation-outcome record + webhook, review-playbook extraction,
        regression-case advancement, post-merge smoketest) with NO retry
        path — if we skip them they are dropped permanently. Yielding to a
        peer here is wrong. Instead we BLOCK on FOR UPDATE, bounded by a
        ``SET LOCAL lock_timeout`` (``lock_timeout_ms``) so we never hang
        indefinitely. On a genuine (rare) timeout we raise
        ``_OriginalTaskLockTimeout`` for the caller to log loudly + leave a
        recoverable trail. ``attempts`` is honoured as a bounded retry on
        the timeout (a peer's hold is brief — usually the EVALUATING→terminal
        write — so a short re-block typically succeeds).

    Why this exists / what it prevents (C4):
      - The merge-success orig fetch used a bare ``session.get`` with no row
        lock, so the COMPLETED transition raced ``ci_completion`` mutating
        the SAME parent under EVALUATING. Locking removes that race.
      - The retry loops were byte-identical; one helper keeps them symmetric
        so a future fix lands in one place.

    Behaviour:
      - Returns the locked ``Task`` if the row was acquired.
      - ``skip_locked=True``: returns ``None`` when the row is held by a peer
        for all ``attempts`` (caller DEFERS) OR when the task genuinely no
        longer exists.
      - ``skip_locked=False``: blocks (bounded) until acquired; returns
        ``None`` only when the task genuinely no longer exists; raises
        ``_OriginalTaskLockTimeout`` if the bounded wait elapses.
      - ``cached_task``: if the caller already holds a (possibly stale)
        instance of this row in ``session``, pass it so we ``session.expire``
        it before the FOR-UPDATE re-SELECT. ``populate_existing`` alone has
        been observed unreliable under async + ``with_for_update(skip_locked
        =True)`` (see ``_relock_parent``); expiring first guarantees the
        re-SELECT cannot return stale identity-map attributes.
    """
    if isinstance(original_task_id, str):
        original_task_id = uuid.UUID(original_task_id)
    if cached_task is not None:
        try:
            session.expire(cached_task)
        except InvalidRequestError:
            session.expire_all()

    if not skip_locked:
        return await _lock_original_task_blocking(
            session,
            original_task_id,
            attempts=attempts,
            source=source,
            lock_timeout_ms=lock_timeout_ms,
        )

    orig: Task | None = None
    for _attempt in range(attempts):
        stmt = (
            select(Task)
            .where(Task.id == original_task_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        orig = (await session.execute(stmt)).scalars().first()
        if orig is not None:
            return orig
        # Row not returned: either held by a peer (skip_locked) or gone.
        # Distinguish so we don't keep retrying a deleted row.
        exists = (
            await session.execute(
                select(Task.id).where(Task.id == original_task_id)
            )
        ).first()
        if exists is None:
            return None  # Genuinely missing — no point retrying.
        if _attempt < attempts - 1:
            await asyncio.sleep(0.5 * (_attempt + 1))
    logger.info(
        "[ROLLUP] _lock_original_task: parent %s held by peer after %d attempts "
        "— deferring%s",
        original_task_id,
        attempts,
        f" ({source})" if source else "",
    )
    return None


async def _lock_original_task_blocking(
    session: AsyncSession,
    original_task_id: uuid.UUID,
    *,
    attempts: int,
    source: str,
    lock_timeout_ms: int,
) -> Task | None:
    """BLOCKING FOR UPDATE acquire, bounded by ``SET LOCAL lock_timeout``.

    Used by the merge-success caller (see ``_lock_original_task`` docstring):
    the merge already landed on GitHub, so the one-shot ``orig`` bookkeeping
    must run — we must NOT yield to a peer. A peer's hold on this row is brief
    (its own EVALUATING→terminal write), so blocking for a few seconds then
    succeeding is the correct trade. ``lock_timeout`` caps the wait so a stuck
    peer can never produce an unbounded hang. Raises ``_OriginalTaskLockTimeout``
    after ``attempts`` bounded blocks all time out.

    SQLite (test DB) has no ``SET LOCAL lock_timeout`` and no real row-level
    write-lock contention, so on a non-Postgres dialect we just issue a plain
    blocking FOR UPDATE (which returns immediately there).
    """
    is_pg = resolve_dialect_name(session) == "postgresql"
    last_exc: Exception | None = None
    for _attempt in range(max(1, attempts)):
        try:
            if is_pg:
                # SET LOCAL reverts on the transaction's commit/rollback. Bound
                # the wait so a wedged peer can't hang this handler forever.
                # NB: SET is a utility statement — Postgres/asyncpg reject bind
                # parameters here ("syntax error at or near $1"), so the value
                # MUST be inlined. lock_timeout_ms is an int we control (never
                # user input), so int() coercion makes the interpolation safe.
                await session.execute(
                    text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}'")
                )
            stmt = (
                select(Task)
                .where(Task.id == original_task_id)
                .with_for_update()  # BLOCKING — no skip_locked
                .execution_options(populate_existing=True)
            )
            orig = (await session.execute(stmt)).scalars().first()
            if orig is not None:
                return orig
            # Row not returned by a BLOCKING lock means it's genuinely gone
            # (a blocking FOR UPDATE never skips a held row).
            return None
        except Exception as exc:  # noqa: BLE001, PERF203
            # On Postgres a lock_timeout surfaces as LockNotAvailableError
            # (sqlstate 55P03), wrapped by SQLAlchemy. The aborted statement
            # leaves the transaction in a failed state, so roll back before
            # retrying the bounded block.
            last_exc = exc
            with contextlib.suppress(Exception):
                await session.rollback()
            if _attempt < max(1, attempts) - 1:
                await asyncio.sleep(0.5 * (_attempt + 1))
    logger.error(
        "[ROLLUP] _lock_original_task(blocking): parent %s still locked after "
        "%d bounded attempts (lock_timeout=%dms)%s — the merge ALREADY landed; "
        "one-shot post-merge effects (back-merge / rescan / remediation outcome) "
        "are deferred to recovery, NOT dropped silently: %r",
        original_task_id,
        attempts,
        lock_timeout_ms,
        f" ({source})" if source else "",
        last_exc,
    )
    raise _OriginalTaskLockTimeout(str(original_task_id))


class _ParentMutatedDuringPRCreation(Exception):
    """Sentinel: parent row was mutated by a peer during create_pull_request.

    Inside ``_check_and_trigger_rollup``, when ``_relock_parent`` returns None
    after the rollup PR has already been created on GitHub, the orphan-PR
    rescue path in the outer except handler must persist the rollup_pr_number
    marker via a fresh session, then bail — the rest of the handler depends
    on ``parent`` being a live ORM instance, which it isn't here.
    """


# Phase B3 (lock-window break): each GitHub API call inside
# _check_and_trigger_rollup can hang 5-30s (worst case minutes during
# rate-limit). Holding the parent's FOR UPDATE lock across that window
# starves concurrent heartbeats on sibling children — they see 30s
# lock_timeout and the reaper false-orphans them. The fix is to commit
# any pending mutations to release the lock, perform the GitHub call(s)
# without lock held, then re-acquire and validate `updated_at` to detect
# whether the staging-state changed during the unlocked window.
#
# Round 4 W1 (audit C1, C3, H6): three structural fixes layered on top:
#   - C1 (cache): SQLAlchemy identity map returned cached objects on
#     re-SELECT under ``expire_on_commit=False``, so the validation
#     never fired. Fix: use a fresh AsyncSession for the re-acquire so
#     the new SELECT bypasses the prior session's identity map.
#     ``populate_existing=True`` is also added as belt-and-suspenders
#     (defensive against future callers that re-use the same session).
#   - C3 (heartbeat): heartbeat ``onupdate=_utcnow`` advanced
#     ``updated_at`` every 30s, false-deferring on every overlapping
#     heartbeat. Fix: validate against a content snapshot of the fields
#     the unlocked window is actually meant to protect (status,
#     requires_approval, rollup_pr_number/_url markers, pr_merged) —
#     NOT ``updated_at``, NOT ``last_heartbeat``.
#   - H6 (pool): the prior pattern held a connection through the 5-150s
#     HTTP retry window. Fix: explicitly close the session after the
#     release-commit, run HTTP without DB connection, open a fresh
#     session for the re-acquire.
def _capture_rollup_parent_snapshot(parent: Task) -> dict:
    """Snapshot the rollup-parent fields that lock-window-break protects.

    The unlocked window between ``_release_parent_lock`` and ``_relock_parent``
    is meant to detect peer mutation of the rollup state machine: status
    transitions (EVALUATING → COMPLETED/FAILED), the rollup PR marker
    (``rollup_pr_number`` / ``rollup_pr_url``, the crash-recovery marker
    written at line ~1046), the ``pr_merged`` flag, and approval-gate flips.
    Heartbeat-only changes (last_heartbeat, last_metrics_json,
    heartbeat_count) don't touch any of these fields, so they don't trip
    the validator under load.
    """
    ctx = parent.context or {}
    return {
        "status": parent.status,
        "requires_approval": bool(parent.requires_approval),
        "rollup_pr_number": ctx.get("rollup_pr_number"),
        "rollup_pr_url": ctx.get("rollup_pr_url"),
        "pr_merged": ctx.get("pr_merged"),
        "rollup_lineage_generation": ctx.get("_rollup_lineage_generation", 0),
    }


async def _release_parent_lock(
    parent: Task,
    session: AsyncSession,
    *,
    context_dirty: bool = False,
) -> dict:
    """Commit any pending mutations on parent and release the FOR UPDATE lock.

    Returns a content snapshot that ``_relock_parent`` will compare against
    after the unlocked I/O window to detect peer mutation of the rollup
    state machine. Heartbeat-driven ``updated_at`` advances DO NOT trip the
    validator (R4-W1 audit C3).

    Round 4 W1 (H6): SQLA 2.0 async sessions return their underlying
    connection to the pool on transaction commit (default
    ``reset_on_return="rollback"``). The HTTP work that follows therefore
    runs without a checked-out connection — the next operation on the
    session auto-acquires a fresh connection. This addresses the pool-
    exhaustion concern at 1k-user scale without forcing every caller to
    juggle two sessions. ``populate_existing=True`` in ``_relock_parent``
    bypasses the identity-map cache (audit C1) for the re-SELECT.

    R5-W3-B (audit 2026-04-30): the prior version of this function called
    ``flag_modified(parent, "context")`` UNCONDITIONALLY, which forced a
    JSONB-column rewrite on every commit even when the caller hadn't
    actually mutated ``parent.context``. Most release sites in
    ``_check_and_trigger_rollup`` (lines ~954, ~1033, ~1098, ~1224, ~1290,
    ~1343) are read-only — the function snapshots state, releases the lock
    around external I/O, then re-acquires. With ~6 release/relock cycles
    per rollup decision and parent contexts that can grow to 100+ KB,
    flag_modified-on-every-release amplified TOAST-write traffic by 6x for
    no DB-correctness benefit.

    Switched to caller-driven: callers that mutated ``parent.context``
    before this call MUST pass ``context_dirty=True``. The default of
    ``False`` matches the actual behavior of every read-only call site.
    Callers that mutate context after a re-lock typically do so via
    ``parent.context = {...}`` and ``session.add(parent)`` directly, then
    ``session.commit()`` — those don't go through this function and the
    SQLA assignment-tracking already detects the mutation.

    Historical note (Round 4 W1 audit C1): the unconditional
    ``flag_modified`` was added because, after a prior ``_relock_parent``
    did ``populate_existing`` + ``expire_all``, the NEXT mutation to
    ``parent.context`` could go undetected by SQLA's change-tracking —
    empirically verified against the merge handler test. That risk still
    applies for callers that DO mutate context before release, hence the
    explicit ``context_dirty`` flag (rather than removing the call
    entirely).
    """
    if context_dirty and parent.context is not None:
        flag_modified(parent, "context")
    session.add(parent)
    await session.commit()
    return _capture_rollup_parent_snapshot(parent)


async def _relock_parent(
    session: AsyncSession,
    parent_id,
    expected_snapshot: dict | None,
    *,
    parent_in_session: Task | None = None,
) -> Task | None:
    """Re-acquire FOR UPDATE on the parent row and validate staging-state.

    Returns the freshly-loaded Task if the row is still ours and the
    rollup-state snapshot still matches, or None if:
        - the row is held by another writer (skip_locked yielded nothing), OR
        - any field in the captured snapshot mismatches (peer mutated the
          rollup state machine during the unlocked window).

    Round 4 W1 (audit C1, C3): replaces the prior ``expected_updated_at``
    check (which never fired due to the identity-map cache, and would
    false-trip on heartbeats even if the cache were bypassed). Validation
    is now against ``_capture_rollup_parent_snapshot`` — a content-based
    check covering status, requires_approval, rollup_pr_number/url,
    pr_merged. ``populate_existing=True`` forces SQLA to refresh
    attributes on the cached identity-map instance from this SELECT,
    bypassing the cache that previously made the validation a no-op
    under ``expire_on_commit=False``. When the caller mutates
    ``parent.context`` before release, it must pass
    ``context_dirty=True`` to ``_release_parent_lock`` so the
    ``flag_modified`` call fires and the mutation persists across the
    populate_existing's change-tracking reset (R5-W3-B; verified
    empirically — see ci_completion.py for the canonical comment).
    Read-only release sites omit the flag and pay no JSONB-write cost.

    Round 5 W1 (HIGH-2 belt-and-suspenders): callers can pass the
    released-but-cached ``parent`` instance via ``parent_in_session`` so
    we ``session.expire`` it before the re-SELECT. ``populate_existing``
    alone has been observed to be unreliable under async +
    ``with_for_update(skip_locked=True)`` — expiring the cached instance
    first guarantees the re-SELECT cannot return stale identity-map
    attributes. Mirrors the ``session.expire(task)`` pattern at
    ``task_completion.py:1671`` and ``ci_completion._relock_merge_task``.
    (Earlier docs cited reaper.py:382 as the original documentation site;
    the bug-doc comment has since rotated and `_relock_parent` is now the
    canonical reference for the pattern.)

    Mirrors the early-commit pattern in routers/agent_status.py:1694: release
    the lock around long-running I/O, then resume on a fresh row read.
    """
    if parent_in_session is not None:
        try:
            session.expire(parent_in_session)
        except InvalidRequestError:
            # Object detached from this session (e.g. expunged elsewhere)
            # — expire_all() is a safe broader fallback; the populate_existing
            # SELECT below will repopulate whatever we still need.
            session.expire_all()
    stmt = (
        select(Task)
        .where(Task.id == parent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(stmt)).scalars().first()
    if fresh is None:
        logger.info(
            "[ROLLUP] Re-lock skipped for parent %s — row held by peer, "
            "deferring to next cycle",
            parent_id,
        )
        return None
    if expected_snapshot is not None:
        actual = _capture_rollup_parent_snapshot(fresh)
        if actual != expected_snapshot:
            logger.info(
                "[ROLLUP] Re-lock detected concurrent mutation on parent %s "
                "(expected snapshot=%s, found %s) — deferring to next cycle",
                parent_id,
                expected_snapshot,
                actual,
            )
            return None
    return fresh
