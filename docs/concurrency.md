# Concurrency model

This document captures the canonical row-locking and session-lifecycle
contract that the backend relies on. It exists to prevent regression of
the deadlock and stale-read bugs flagged in the comprehensive database
audit (see `audit_db_comprehensive_2026_04_29.md`).

## Canonical row-lock order: **Project before Task, always**

The audit (CRITICAL C5) found cross-table lock-ordering deadlock potential:
some paths locked `Project` then `Task`, others locked `Task` then `Project`.
Two flows holding opposite orders deadlock until the 30-second
`lock_timeout` fires and surfaces `LockNotAvailable` as a 500 — Cloud Run
then quarantines the instance.

**The rule is:** when a code path needs both row locks, acquire `Project`
first, then `Task`. There is no exception. Sites that previously locked
`Task` first must either drop the `Project` lock (if not strictly needed)
or reorder to acquire `Project` first.

### Helpers (Phase A1 + Phase C1, in `backend/app/task_locking.py`)

- `lock_project_or_429(session, project_id)` — request-handler helper.
  `SELECT ... FOR UPDATE SKIP LOCKED`; raises HTTP 429 with `Retry-After`
  on contention so the client retries. Distinguishes 429 (held) from 404
  (missing).
- `project_lock(session, project_id, *, skip_locked=True)` —
  background-loop / post-commit chain context-manager. Yields the locked
  `Project` or `None` on contention. Caller checks for `None` and skips
  / retries.
- `task_lock(session, task_id, *, skip_locked=True)` — same shape for
  tasks.
- `lock_project_then_task(session, project_id, task_id, *, skip_locked=True)`
  — pair-locker that enforces the canonical order. If the project row is
  held, the task lock is *never attempted* (so the caller doesn't sit on
  a half-pair). Yields `(project, task)`; either may be `None` on
  contention or missing.
- `lock_project_for_strategic_transition(session, project_id)` —
  bounded-retry skip-locked acquisition for the strategic post-commit
  chain. Returns the locked `Project`, or `None` after the retry window.
  Used by `_process_post_commit` in `task_lifecycle.py` to acquire the
  project lock **once** at the top of the chain and pass it down to
  `_handle_design_doc_completion`, `_handle_strategic_completion`, and
  `_record_strategic_task_failure` — replacing the per-handler bounded-
  retry blocks that previously stacked back-to-back lock acquisitions for
  the same row in the same chain.

### Phase B3 (lock-while-call windows)

`rollup_management.py` previously held `FOR UPDATE` row locks while
making 5-30s GitHub API calls (minutes during rate-limit). Concurrent
heartbeats then 409'd and the reaper false-orphaned them. The fix
(Phase B3) is the **commit-and-release / re-acquire-and-validate**
pattern:

1. Acquire lock, read state, **commit-release** the transaction.
2. Make the GitHub API call(s) with **no lock held**.
3. Re-acquire the lock, validate the staging-state hasn't changed
   (compare `updated_at` or version), apply the mutation, commit.
4. If staging-state changed during the unlocked window, re-evaluate or
   surface conflict.

Do NOT reintroduce locks across external calls. The pattern is
documented further in the audit deferrals.

### Phase L-C4 audit (this file)

`task_lifecycle.py:_has_active_strategic_task` performs a non-locking
aggregate `SELECT` over QUEUED/PROVISIONING/IN_PROGRESS tasks **after**
acquiring the project lock. The audit asked whether this aggregate is
phantom-safe under READ COMMITTED.

**Symmetry argument confirmed:** every code path that inserts a strategic
task (`design_doc_update` or `strategic_review`) goes through the same
`Project` lock — the helpers above centralize this. So a phantom row
appearing between our `SELECT` and a peer's `INSERT` is impossible by
construction: the peer would have to be holding the same project row,
which our `SKIP LOCKED` here just rejected (we already returned `True`,
the fail-safe value, deferring our spawn). No SERIALIZABLE upgrade
required.

The invariant is: **as long as Project is locked before any
strategic-task SELECT/INSERT, READ COMMITTED is sufficient.** This
invariant is preserved by the canonical-order rule above; do not break
it without revisiting this analysis.

## `expire_on_commit=False` semantics

`AsyncSessionLocal` (in `backend/app/database.py`) is configured with
`expire_on_commit=False`. This means SQLAlchemy does **not** invalidate
loaded ORM objects after `session.commit()` — they keep their pre-commit
attribute values. This was a deliberate choice to avoid the surprise
re-fetch round-trip on every post-commit attribute access (especially
hot in heartbeat / status endpoints).

**The cost:** if your code reads a row, commits, then later in the same
session reads attributes that may have been modified by a peer, you'll
see the stale snapshot. Two safe patterns:

1. **Re-fetch through a fresh session.** Best for read-after-write across
   a "we no longer hold the lock" boundary.
2. **`await session.refresh(obj)`** — explicitly refreshes the instance
   from the database. Use when you need current state but want to keep
   the same session.

Sites that mix locked and unlocked reads in the same session must use
one of these patterns. The hot ones (`rollup_management.py` post-commit
reads at lines ~800-845) are documented inline.

## Session lifecycle in background loops

Long-running background loops should:
- Open a session for each iteration's logical unit of work, not for the
  whole loop body. The session is bound to a single transaction; holding
  it across many iterations defeats the per-iteration commit.
- Always `await session.close()` (or use `async with`) to return the
  connection to the pool. Outstanding sessions block pool exhaustion at
  ~25 conns / Cloud Run instance.
- Never hold a `FOR UPDATE` lock across an external call — see Phase B3
  pattern above.

## When in doubt

- Need both locks? Use `lock_project_then_task`.
- Need just the project lock from a handler? Use `lock_project_or_429`.
- Need just the project lock from a loop / post-commit chain? Use
  `project_lock` (or `lock_project_for_strategic_transition` for the
  strategic chain specifically).
- Need just the task lock? Use `task_lock`.

If you're tempted to write a fresh `with_for_update()` call: don't.
The plain variant uses the 30s `lock_timeout` and surfaces
`LockNotAvailableError` as a 500. Use `skip_locked=True` and handle the
contention case explicitly.
