# Pipeline Audit Fix Plan v1

**Source audit:** `~/.claude/projects/D--Henry-VSCode-Codebase-Blueprint/memory/audit_pipeline_2026_04_25.md`
**Branch starting point:** `main` @ `c7dae430`
**Schema baseline:** `0044`
**Coverage:** all 9 CRITICAL + 26 HIGH findings, deduped to 6 workstreams
**Audience:** autonomous Blueprint agents (one workstream per task; later workstreams may depend on earlier ones — see Sequencing)

## Conventions

- **No backward-compat shims.** Blueprint has no external users (`project_not_public.md`). Hard cutovers everywhere.
- **Fail fast on real failures, never on legitimate work.** Every new validation check must reject only state that is provably wrong, not state that is merely incomplete-but-recoverable.
- **Migrations:** new files only in `backend/alembic/versions/` (existing are immutable). Bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` to match.
- **Testing per CLAUDE.md:**
  - Backend: `bash scripts/test-backend.sh --postgres testing/test_<file>.py`
  - Frontend: `cd admin-ui && npm run build && npx eslint .`
  - Agent: `cd agent && npx tsc --noEmit && npm test`
- **Lint touched files** before commit (ruff for Python; eslint for TS).
- **No `.github/workflows/` edits, no `scripts/` edits, no modifying applied migrations.**

## Sequencing

| Order | Workstream | Depends on | Migration # | Notes |
|-------|-----------|-----------|-------------|-------|
| 1 | **W1** DB locking & lifecycle columns | – | 0045 | Foundation; everything below benefits |
| 2 | **W6** Two-agent claim race | W1 (uses task_lock helper) | – | Quick standalone win |
| 3 | **W2** Strategic cycle table | W1 (uses task_lock helper) | 0046 | Independent of W3-W5 |
| 4 | **W3** Auth & token refresh | – | – | JWT shape change; agent + backend deploy together |
| 5 | **W4** Agent reliability | W3 (refresh endpoint) | – | Mostly TS work |
| 6 | **W5** Pool + classification | – | – | Quick wins; can also run early |

W2/W3/W5 can be parallelized once W1 lands. W4 needs W3's refresh endpoint.

---

# W1 — DB Locking & Lifecycle Idempotency

**Resolves:** F1, F2, F3, F5, F6, F4, F7, A1, A4, A8, A12, F13, A2, A5, A7, A14, F8 (17 findings)
**Complexity:** large (single PR, multi-day for autonomous agent)

## Step 1.1 — Inventory `task.context` keys

Run from repo root:

```bash
rg -n --no-heading "task\.context\[|task\.context\.get\(|context\[\"|context\.get\(" backend/app/ \
  | rg -v "^backend/app/__pycache__"
```

Build a complete table of every key written/read, classified as:
- **PROMOTE TO COLUMN** — used in WHERE clauses, idempotency guards, queried frequently, or needs fresh reads
- **KEEP IN JSONB** — opaque metadata, written once, read for display only

Already-known PROMOTE candidates from the audit:

| Key | Type | Used by | Reason |
|---|---|---|---|
| `_post_commit_pending` | bool | agent_status idempotency guard | A2/A5 |
| `_post_commit_failed` | bool | NEW (W1) | distinguish "in flight" from "permanent fail" |
| `requeue_in_flight` | bool | reaper requeue dedup | A4 |
| `infra_wait` / `infra_wait_until` | timestamptz | orchestrator dispatch | F1 RMW race |
| `retry_after` / `retry_after_until` | timestamptz | orchestrator dispatch | F2 RMW race |
| `_previous_error_fingerprint` | text | auto_remediation cap query | B6/B14 (full Q3 audit) |
| `auto_remediation` | bool | many | concurrency cap queries |
| `strategic_cycle_id` | text/uuid | strategic completion handler | D4 (lifecycle integration) |
| `_failure_class_history` | text[] | retry_budget | B10 expiry logic |
| `spawn_failed` | bool | NEW (W5/A6) | smoketest-spawn fail surface |
| `last_remediation_at` | timestamptz | direction_lifecycle | D13 (set early) |

Inventory output should drive Step 1.2 schema.

## Step 1.2 — Migration `0045_promote_task_lifecycle_columns.py`

```python
"""Promote hot task.context fields to columns

Revision ID: 0045
Revises: 0044
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0045"
down_revision = "0044"

def upgrade():
    op.add_column("task", sa.Column("post_commit_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task", sa.Column("post_commit_failed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task", sa.Column("requeue_in_flight", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task", sa.Column("infra_wait_until", sa.DateTime(timezone=False), nullable=True))
    op.add_column("task", sa.Column("retry_after_until", sa.DateTime(timezone=False), nullable=True))
    op.add_column("task", sa.Column("previous_error_fingerprint", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("auto_remediation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task", sa.Column("strategic_cycle_id", sa.String(36), nullable=True))
    op.add_column("task", sa.Column("spawn_failed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("task", sa.Column("last_remediation_at", sa.DateTime(timezone=False), nullable=True))

    # DeploymentState optimistic-concurrency version
    op.add_column("deploymentstate", sa.Column("version", sa.Integer(), nullable=False, server_default="0"))

    # Indexes for hot query paths
    op.create_index("ix_task_auto_remediation", "task", ["auto_remediation"], postgresql_where=sa.text("auto_remediation IS TRUE"))
    op.create_index("ix_task_requeue_in_flight", "task", ["requeue_in_flight"], postgresql_where=sa.text("requeue_in_flight IS TRUE"))
    op.create_index("ix_task_strategic_cycle_id", "task", ["strategic_cycle_id"], postgresql_where=sa.text("strategic_cycle_id IS NOT NULL"))
    op.create_index("ix_task_previous_error_fingerprint", "task", ["previous_error_fingerprint"], postgresql_where=sa.text("previous_error_fingerprint IS NOT NULL"))

    # Backfill from context. Use raw SQL — JSONB ops are safe.
    op.execute("""
        UPDATE task SET
          post_commit_pending = COALESCE((context->>'_post_commit_pending')::boolean, FALSE),
          requeue_in_flight   = COALESCE((context->>'requeue_in_flight')::boolean, FALSE),
          auto_remediation    = COALESCE((context->>'auto_remediation')::boolean, FALSE),
          spawn_failed        = COALESCE((context->>'_spawn_failed')::boolean, FALSE),
          previous_error_fingerprint = context->>'_previous_error_fingerprint',
          strategic_cycle_id  = context->>'strategic_cycle_id'
        WHERE context IS NOT NULL;
    """)
    # Timestamp fields parsed best-effort; leave NULL on parse fail.
    op.execute(r"""
        UPDATE task SET
          retry_after_until = (context->>'retry_after')::timestamp
        WHERE context ? 'retry_after' AND context->>'retry_after' ~ '^\d{4}-\d{2}-\d{2}';
    """)
    op.execute(r"""
        UPDATE task SET
          infra_wait_until = (context->>'infra_wait_until')::timestamp
        WHERE context ? 'infra_wait_until' AND context->>'infra_wait_until' ~ '^\d{4}-\d{2}-\d{2}';
    """)
    op.execute(r"""
        UPDATE task SET
          last_remediation_at = (context->>'last_remediation_at')::timestamp
        WHERE context ? 'last_remediation_at' AND context->>'last_remediation_at' ~ '^\d{4}-\d{2}-\d{2}';
    """)

    # Strip the now-promoted keys from context to prevent dual-source drift.
    op.execute("""
        UPDATE task SET context = context
          - '_post_commit_pending'
          - 'requeue_in_flight'
          - 'auto_remediation'
          - '_spawn_failed'
          - '_previous_error_fingerprint'
          - 'strategic_cycle_id'
          - 'retry_after'
          - 'infra_wait_until'
          - 'last_remediation_at'
        WHERE context IS NOT NULL;
    """)

def downgrade():
    # Reverse only column drops; backfill is one-way.
    for col in ["post_commit_pending","post_commit_failed","requeue_in_flight",
                "infra_wait_until","retry_after_until","previous_error_fingerprint",
                "auto_remediation","strategic_cycle_id","spawn_failed","last_remediation_at"]:
        op.drop_column("task", col)
    op.drop_column("deploymentstate", "version")
```

Update `backend/app/main.py`: `EXPECTED_SCHEMA_REVISION = "0045"`.

Update `backend/app/models.py`: add the 10 new fields to `Task` SQLModel + `version` to `DeploymentState`. Use `Field(default=False)` / `Field(default=None)` matching nullability above.

## Step 1.3 — `app/task_locking.py` (new)

```python
"""Row-lock helper for task mutations. Use this anywhere task fields are mutated
under concurrent contention (orchestrator dispatch, CI loop, reaper, status POST)."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from app.models import Task

@asynccontextmanager
async def task_lock(session, task_id: str, *, skip_locked: bool = True, timeout: float = 30.0):
    """Acquire SELECT FOR UPDATE on a task row, yield the freshly-loaded Task or None.

    skip_locked=True (default): yield None if the row is held by another session.
        Caller must check for None and skip the iteration — never raises.
    skip_locked=False: wait up to `timeout` seconds, raise OperationalError on timeout.

    The lock is released when the surrounding session commits or rolls back.
    Always commit/rollback before exiting the `async with` to release the lock.
    """
    stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise OperationalError("task_lock acquisition timed out", None, e) from e
    yield result.scalar_one_or_none()
```

## Step 1.4 — Refactor orchestrator dispatch loop

`backend/app/orchestrator.py`: every site that mutates `task.context` (or now: the new columns) must use `task_lock`.

Before:
```python
for task in candidate_tasks:
    if task.context.get("retry_after") and ...:
        ctx = dict(task.context or {})
        ctx.pop("retry_after", None)
        task.context = ctx
        session.add(task)
```

After:
```python
for task_id in [t.id for t in candidate_tasks]:
    async with task_lock(session, task_id) as t:
        if t is None: continue
        if t.retry_after_until and t.retry_after_until > now: continue
        t.retry_after_until = None
    await session.commit()  # release lock
```

Apply to lines `1050-1078` (retry_after / infra_wait clear), `1678-1682` (blacklist after `_max_plan_pool_lock`), and the CI polling loop `2230-2545` (8+ mutations on `ci_task`).

For the blacklist case (line 1678+), the blacklist still lives in `task.context` (not promoted — it's complex JSON). That's fine; the lock still protects the read-modify-write of the JSONB field.

## Step 1.5 — Reaper fixes (A1, A4, A8, A12, F13)

`backend/app/reaper.py`:

1. **Per-task `hb_cutoff`** — move `hb_cutoff = _heartbeat_stale_cutoff(t, now)` from L227 (Step 1 batch) to inside the Step 2 `task_lock` block at L322. Threshold becomes consistent with the freshly-loaded row.

2. **Requeue dedup flag** — inside the lock at L268, *before* releasing, set `t.requeue_in_flight = True`. Step 1 scan must filter `Task.requeue_in_flight.is_(False)` to skip in-flight requeues.

3. **Force-lock branch** — release the lock immediately after marking the task FAILED. Move `_requeue_parent_task(...)` to *after* `await session.commit()`.

4. **Heartbeat starvation** — verify the lock is held for <500ms in the common case; add timing log if >2s.

## Step 1.6 — Post-commit idempotency rewrite (A2, A5, A7, F7)

`backend/app/routers/agent_status.py`:

1. `_persist_post_commit_pending` (L1594-1609) writes to `task.post_commit_pending` column (not context). Use a fresh session as today, but use a single `UPDATE` statement, not a load+set:
   ```python
   await fresh_session.execute(
       update(Task).where(Task.id == task_id).values(post_commit_pending=True)
   )
   await fresh_session.commit()
   ```

2. **Idempotency guard at L545-569** — check the column directly:
   ```python
   pending_row = await session.execute(
       select(Task.post_commit_pending, Task.post_commit_failed)
       .where(Task.id == task_id)
   )
   pending, failed = pending_row.one()
   if task.status in TERMINAL_STATUSES and not pending:
       return {"status": "already_terminal"}
   if pending:
       # Re-run post-commit (idempotent)
       ...
   ```

3. **Session leak fix (F7)** — wrap `_process_post_commit` call in:
   ```python
   try:
       await asyncio.wait_for(_process_post_commit(...), timeout=240)
   except asyncio.TimeoutError:
       await _persist_post_commit_pending(task_id)
       raise HTTPException(status_code=504, detail="post_commit_pending")
   finally:
       await session.rollback()  # release any held locks
   ```

## Step 1.7 — DeploymentState consistency (F4)

Standardize all `DeploymentState` mutations to:
```python
async with task_lock(session, ...) as ds:  # adapt for non-Task; or use direct SELECT FOR UPDATE
    if ds.version != expected_version:
        raise OptimisticLockError
    ds.version += 1
    # mutate
await session.commit()
```

Files: `backend/app/deploy_health.py:219`, `backend/app/routers/admin_settings.py:706`. Both must use `with_for_update(skip_locked=True)` — never the blocking variant.

## Step 1.8 — Datetime convention sweep (F8)

```bash
rg -n "datetime\.now\(timezone\.utc\)|datetime\.utcnow\(\)" backend/app/ backend/testing/
```

Every match outside `backend/app/time_utils.py` must become `_utcnow()`. Specifically:
- `orchestrator.py:1567` (`now_check`)
- `github.py:698, 752` (check-run timestamps)
- any others surfaced by the grep

Verify with `ruff check backend/ --select DTZ`. Should be clean.

## Step 1.9 — Tests

New file `backend/testing/test_task_locking.py`:
- `test_task_lock_skip_locked_returns_none_when_held`
- `test_task_lock_yields_fresh_row_after_concurrent_write`
- `test_concurrent_dispatch_same_task_no_lost_mutations` (spawn 50 coros, verify exactly N mutations applied)
- `test_concurrent_reaper_cycles_no_duplicate_requeue` (spawn 5 reaper coros against same orphan, verify single requeue)

Extend `backend/testing/test_pipeline_health.py` (already in flight per uncommitted changes):
- `test_post_commit_idempotency_with_stale_context`
- `test_session_closed_on_post_commit_timeout`

Run: `bash scripts/test-backend.sh --postgres testing/test_task_locking.py testing/test_pipeline_health.py`

## Acceptance criteria

- [ ] All grep results for `task.context["_post_commit_pending"|"requeue_in_flight"|"auto_remediation"|"_spawn_failed"|"_previous_error_fingerprint"|"strategic_cycle_id"]` return zero matches in `backend/app/`
- [ ] All grep results for `datetime.now(timezone.utc)` and `datetime.utcnow()` outside `time_utils.py` return zero
- [ ] `bash scripts/test-backend.sh --postgres` passes
- [ ] Migration applies cleanly to a fresh DB and to a snapshot with existing data (test on staging snapshot if available)
- [ ] Reaper does not requeue a task twice when run concurrently (load test in `test_task_locking.py`)
- [ ] Post-commit retry after timeout finds the pending flag and re-runs (`test_pipeline_health.py`)

---

# W6 — Two-Agent Claim Race

**Resolves:** A3
**Complexity:** small (single PR, hours)

## Step 6.1 — Filter on `vm_instance_id IS NULL` in claim FOR UPDATE

`backend/app/routers/agent.py:545-599` — modify the claim query:

```python
async with task_lock(session, task_id, skip_locked=True) as task:
    if task is None:
        return JSONResponse(status_code=409, content={"error": "task_locked"})
    if task.status != TaskStatus.QUEUED:
        return JSONResponse(status_code=409, content={"error": "not_claimable", "status": task.status})
    if task.vm_instance_id is not None and task.vm_instance_id != vm_instance_id:
        return JSONResponse(status_code=409, content={"error": "already_claimed_by_other_vm"})
    task.vm_instance_id = vm_instance_id
    task.status = TaskStatus.IN_PROGRESS
    task.last_heartbeat = _utcnow()
await session.commit()
```

The `task.vm_instance_id is None` precondition is the new safety. If a stale-but-recovering agent retries with the same vm_instance_id, it succeeds (legitimate retry); if a different VM tries to grab it, 409 (real failure).

## Step 6.2 — Tests

`backend/testing/test_agent.py` (already modified, extend):
- `test_concurrent_claim_same_task_only_one_succeeds` — 10 concurrent POSTs from different VMs, exactly 1 returns 200
- `test_claim_idempotent_for_same_vm` — same vm_instance_id retries, all succeed (or first 200 + later 409 with `already_claimed_by_self` — pick one and document)

## Acceptance criteria

- [ ] Concurrent claim test passes deterministically across 100 runs
- [ ] No regression in existing claim tests

---

# W2 — Strategic Cycle Lifecycle Hardening

**Resolves:** D1, D2, D4, D12 (cycle stalls); D3, D5 (transactional ops); D7 (vision merge); D8 (starvation dedup)
**Complexity:** medium-large (single PR or split into 2)

## Step 2.1 — Migration `0046_add_strategic_cycle_table.py`

```python
revision = "0046"
down_revision = "0045"

def upgrade():
    op.create_table(
        "strategiccycle",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("phase", sa.String(32), nullable=False),  # phase1_running|phase1_done|phase2_running|completed|failed_phase1|failed_phase2|stalled_phase1|stalled_phase2
        sa.Column("phase1_task_id", sa.String(36), nullable=True),
        sa.Column("phase2_task_id", sa.String(36), nullable=True),
        sa.Column("phase1_started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("phase1_completed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("phase2_started_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("phase2_completed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("cycle_metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_strategiccycle_project_phase", "strategiccycle", ["project_id", "phase"])
    op.create_index("ix_strategiccycle_phase1_task_id", "strategiccycle", ["phase1_task_id"], postgresql_where=sa.text("phase1_task_id IS NOT NULL"))
    op.create_index("ix_strategiccycle_phase2_task_id", "strategiccycle", ["phase2_task_id"], postgresql_where=sa.text("phase2_task_id IS NOT NULL"))

    # Backfill from existing project.vision JSONB sliding window
    # cycle_history is at vision->'cycle_history' (or similar; verify path during impl)
    op.execute("""
        INSERT INTO strategiccycle (id, project_id, phase, phase1_task_id, phase2_task_id,
                                     phase1_started_at, phase1_completed_at,
                                     phase2_started_at, phase2_completed_at,
                                     failure_reason, cycle_metadata, created_at)
        SELECT
          COALESCE(c->>'cycle_id', gen_random_uuid()::text),
          p.id,
          CASE
            WHEN c->>'outcome' = 'completed' THEN 'completed'
            WHEN c->>'outcome' = 'failed' THEN 'failed_' || COALESCE(c->>'failed_phase', 'phase1')
            ELSE 'stalled_' || COALESCE(c->>'phase', 'phase1')
          END,
          c->>'phase1_task_id',
          c->>'phase2_task_id',
          COALESCE((c->>'phase1_started_at')::timestamp, NOW()),
          (c->>'phase1_completed_at')::timestamp,
          (c->>'phase2_started_at')::timestamp,
          (c->>'phase2_completed_at')::timestamp,
          c->>'failure_reason',
          COALESCE(c, '{}'::jsonb),
          COALESCE((c->>'phase1_started_at')::timestamp, NOW())
        FROM project p,
             jsonb_array_elements(COALESCE(p.vision->'cycle_history', '[]'::jsonb)) AS c
        WHERE p.vision IS NOT NULL;
    """)

    # Strip cycle_history from vision after backfill — new code reads from the table
    op.execute("UPDATE project SET vision = vision - 'cycle_history' WHERE vision ? 'cycle_history';")

def downgrade():
    op.drop_table("strategiccycle")
```

Update `backend/app/main.py`: `EXPECTED_SCHEMA_REVISION = "0046"`.

Add `StrategicCycle` to `backend/app/models.py`.

## Step 2.2 — Lifecycle integration (D4)

In `backend/app/task_lifecycle.py`, in the function that handles task terminal status transitions, after committing the task status:

```python
if task.strategic_cycle_id:
    await _update_strategic_cycle_for_task(session, task)

async def _update_strategic_cycle_for_task(session, task):
    cycle = await session.get(StrategicCycle, task.strategic_cycle_id)
    if cycle is None: return
    # Determine which phase this task is
    if cycle.phase1_task_id == task.id:
        cycle.phase1_completed_at = task.completed_at or _utcnow()
        if task.status == TaskStatus.COMPLETED:
            cycle.phase = "phase1_done"
            # Spawn phase 2
            await _spawn_strategic_review_for_cycle(session, cycle)
        else:
            cycle.phase = "failed_phase1"
            cycle.failure_reason = task.failure_summary or "phase1_failed"
    elif cycle.phase2_task_id == task.id:
        cycle.phase2_completed_at = task.completed_at or _utcnow()
        cycle.phase = "completed" if task.status == TaskStatus.COMPLETED else "failed_phase2"
        cycle.failure_reason = None if task.status == TaskStatus.COMPLETED else (task.failure_summary or "phase2_failed")
```

Remove duplicated logic in `backend/app/strategic_completion.py:_record_strategic_task_failure`. The strategic completion handler still does phase-2 spawning on phase-1 success when called normally, but the lifecycle hook is the safety net.

## Step 2.3 — Stalled-cycle reaper (D2, D12)

Add to `backend/app/reaper.py`:

```python
async def reap_stalled_strategic_cycles(session):
    """Mark cycles as stalled if their active phase exceeds 3 hours."""
    cutoff = _utcnow() - timedelta(hours=3)
    stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.phase.in_(["phase1_running", "phase2_running"]))
        .where(or_(
            and_(StrategicCycle.phase == "phase1_running", StrategicCycle.phase1_started_at < cutoff),
            and_(StrategicCycle.phase == "phase2_running", StrategicCycle.phase2_started_at < cutoff),
        ))
        .with_for_update(skip_locked=True)
    )
    for cycle in (await session.execute(stmt)).scalars():
        cycle.phase = "stalled_" + cycle.phase.replace("_running", "")
        cycle.failure_reason = "phase_timeout_3h"
        # Optional: emit alert via notification_dispatch
    await session.commit()
```

Wire into the existing reaper main loop alongside `reap_orphan_tasks`.

## Step 2.4 — Direction-op transactional batching (D3, D5)

In `backend/app/strategic_completion.py:_handle_strategic_completion`, wrap the direction-ops loop:

```python
try:
    async with session.begin():  # outer transaction; nested savepoints inside
        for op in direction_ops[:10]:
            # Lock-protected cap re-check
            active_count = await _count_active_directions(session, project_id, lock=True)
            if op.kind == "create" and active_count >= max_active:
                raise StrategicCapExceeded(active_count, max_active)
            await _apply_direction_op(session, op)
        await _update_cycle_history(session, cycle_id, ...)
except StrategicCapExceeded as e:
    # Roll back all ops; mark cycle for retry
    # No need to manually rollback — the `async with session.begin()` does it
    await _mark_cycle_for_retry(cycle_id, reason=str(e))
```

## Step 2.5 — Vision 3-way merge (D7)

In `strategic_completion.py:634-660`, replace shallow merge with a 3-way:

```python
def _merge_vision(baseline_vision: dict, agent_vision: dict, current_vision: dict) -> dict:
    """3-way merge with user (current) precedence at field level.

    For each field in agent_vision:
      - If current_vision[field] == baseline_vision[field]: take agent's change
      - Else: keep current_vision (user edited)
    For nested lists (systems, success_criteria):
      - Match by id/key; apply same rule per-item
    """
    result = dict(current_vision)
    for key, agent_val in agent_vision.items():
        baseline_val = baseline_vision.get(key)
        current_val = current_vision.get(key)
        if isinstance(agent_val, list) and isinstance(current_val, list):
            result[key] = _merge_list_by_key(baseline_val or [], agent_val, current_val, key_fn=lambda x: x.get("id") or x.get("name"))
        elif current_val == baseline_val:
            result[key] = agent_val
        # else: user touched this field, keep current
    return result
```

Snapshot `baseline_vision = copy.deepcopy(project.vision)` at cycle start (in `strategic_spawning.py:592-678`); store it on the cycle's `cycle_metadata['baseline_vision']`. Read at completion time.

## Step 2.6 — Starvation fallback dedup (D8)

`strategic_completion.py:385-440`:

```python
if starvation_detected:
    fallback_text = "Deliver user-visible product improvements"
    is_dup = await _is_direction_duplicate(session, project_id, fallback_text)
    if is_dup:
        await _create_strategic_alert(session, project_id, "starvation_fallback_dup",
            f"5 empty cycles, fallback would duplicate existing direction. Operator action required.")
        return
    await _create_direction(session, project_id, fallback_text, ...)
```

## Step 2.7 — Tests

`backend/testing/test_strategic_cycle_lifecycle.py` (new):
- `test_phase1_failure_via_lifecycle_marks_cycle_failed`
- `test_phase1_crash_with_no_completion_handler_call_stalled_by_reaper`
- `test_phase2_starvation_after_phase1_done_no_phase2_spawn`
- `test_direction_op_cap_exceeded_rolls_back_all`
- `test_vision_3way_merge_user_edit_preserved`
- `test_starvation_fallback_dedup_creates_alert`

Backfill smoke test: load a fixture project with cycle_history JSONB, run migration, verify all entries land in the table with correct phase mapping.

## Acceptance criteria

- [ ] Migration backfills existing cycle_history into table with correct phase
- [ ] `vision` no longer contains `cycle_history` after migration
- [ ] All strategic_cycle_lifecycle tests pass
- [ ] Stalled cycles auto-recover within 3h via reaper
- [ ] User edits to vision during a cycle are preserved at the field level

---

# W3 — Auth & Token Lifecycle

**Resolves:** E1 (no refresh, 2h TTL); E2 (path-based exemption); E3 (no task_type claim); E10 (no project_id claim)
**Complexity:** medium (touches every authenticated endpoint; no migration)

## Step 3.1 — Expanded JWT claims

`backend/app/auth.py:create_agent_token` (around L70-83):

```python
def create_agent_token(*, task_id: str, vm_instance_id: str, project_id: str,
                       task_type: str, ttl_hours: int = 8) -> str:
    now = _utcnow()
    payload = {
        "sub": task_id,
        "vm_instance_id": vm_instance_id,
        "project_id": project_id,
        "task_type": task_type,
        "exp": (now + timedelta(hours=ttl_hours)).timestamp(),
        "iat": now.timestamp(),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")
```

Update all callers to pass `project_id` and `task_type`. They're available wherever `task` is in scope.

`verify_agent_jwt` enforces presence of all five core claims; missing claim → 401.

## Step 3.2 — `POST /api/agent/refresh-token`

New endpoint in `backend/app/routers/agent.py`:

```python
class RefreshTokenRequest(BaseModel):
    pass  # body unused; auth via current token

@router.post("/api/agent/refresh-token")
async def refresh_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token_payload: dict = Depends(verify_agent_jwt),
):
    task_id = token_payload["sub"]
    vm_instance_id = token_payload["vm_instance_id"]
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    if task.vm_instance_id != vm_instance_id:
        raise HTTPException(409, "vm_instance_mismatch")
    if task.status not in ACTIVE_STATUSES:
        raise HTTPException(409, f"task_not_active:{task.status}")
    new_token = create_agent_token(
        task_id=task.id, vm_instance_id=task.vm_instance_id,
        project_id=task.project_id, task_type=task.task_type,
    )
    task.last_token_refresh_at = _utcnow()  # add this column in migration 0045 if not already
    await session.commit()
    return {"token": new_token}
```

If `last_token_refresh_at` isn't in the W1 migration, add it there (small addition).

## Step 3.3 — Remove path-based exemption (E2)

`backend/app/routers/agent.py:104-147` — current `verify_agent_token` exempts `/api/agent/status` and `/api/agent/credentials/rotate`. Replace with column-based check:

```python
async def verify_agent_token(request, session, ...):
    # ... decode JWT ...
    task_id = payload["sub"]
    token_vm = payload["vm_instance_id"]
    token_project = payload["project_id"]

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(401, "task_not_found")

    # Project authz (E10)
    if task.project_id != token_project:
        raise HTTPException(403, "project_mismatch")

    # VM-instance check applies to ALL endpoints (no path exemption)
    if task.vm_instance_id != token_vm:
        # This is a real failure: a different VM is using this task.
        # Status endpoint idempotency for the SAME VM doesn't violate this — same vm == same id.
        raise HTTPException(403, "vm_instance_mismatch")

    # Active-task check is now relaxed for status/credentials so legitimate retries from
    # the SAME VM can post final state on a terminal task. Different VM → already rejected above.
    path = request.url.path
    if task.status not in ACTIVE_STATUSES and path not in ("/api/agent/status", "/api/agent/credentials/rotate"):
        raise HTTPException(409, "task_not_active")

    return payload
```

Per Q6 (fail fast on real failures, not legitimate work): a same-VM retry on `/status` after task is COMPLETED is legitimate (idempotent post-commit retry); a different-VM call is a real failure.

## Step 3.4 — Per-endpoint authz decorators (E3)

New module `backend/app/agent_authz.py`:

```python
def require_task_type(*allowed_types: str):
    """Decorator for agent-facing endpoints that require a specific task_type claim."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, token_payload: dict = Depends(verify_agent_jwt), **kwargs):
            if token_payload["task_type"] not in allowed_types:
                raise HTTPException(403, f"task_type_not_permitted:{token_payload['task_type']}")
            return await fn(*args, token_payload=token_payload, **kwargs)
        return wrapper
    return deco
```

Apply to high-privilege endpoints. Inventory pass:
```bash
rg -n "@router\.(post|get|put|delete)" backend/app/routers/agent.py backend/app/routers/agent_status.py backend/app/routers/agent_credentials.py
```

For each route, document the set of task_types that legitimately need it, and decorate. Generic endpoints (heartbeat, status) accept all types; specialized endpoints (e.g., strategic-only) restrict.

## Step 3.5 — Agent-side refresh & atomic token swap

`agent/src/index.ts`:

1. Replace cached `apiHeaders` constant with a function (also fixes C7):
```typescript
function getApiHeaders(): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    'X-Agent-Token': AGENT_TOKEN,
  };
}
```
Every `fetch(...)` call uses `getApiHeaders()` at call time.

2. Background refresh at 4h:
```typescript
const REFRESH_AT_MS = 4 * 60 * 60 * 1000;
let refreshTimer: NodeJS.Timeout | null = null;

function scheduleTokenRefresh() {
  refreshTimer = setTimeout(() => {
    void refreshTokenWithRetry().then(() => scheduleTokenRefresh());
  }, REFRESH_AT_MS);
}

async function refreshTokenWithRetry(maxAttempts = 3): Promise<boolean> {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await fetch(`${ORCHESTRATOR_URL}/api/agent/refresh-token`, {
        method: 'POST', headers: getApiHeaders(),
      });
      if (res.ok) {
        const { token } = await res.json();
        AGENT_TOKEN = token;
        log('[token] refreshed');
        return true;
      }
    } catch (e) { log(`[token] refresh attempt ${i+1} failed: ${e}`); }
    await sleep(2000 * Math.pow(2, i));
  }
  return false;
}
```

3. On 401 from any apiPost, attempt one refresh before retrying. If refresh succeeds, retry; if refresh fails too, surface as task failure (not infinite retry).

## Step 3.6 — Tests

`backend/testing/test_auth.py` (extend or new):
- `test_token_includes_project_and_task_type`
- `test_refresh_token_succeeds_for_active_task_and_same_vm`
- `test_refresh_token_rejects_different_vm` (returns 409 vm_instance_mismatch)
- `test_refresh_token_rejects_inactive_task`
- `test_status_post_idempotent_for_same_vm_on_terminal_task`
- `test_status_post_rejects_different_vm_on_terminal_task` (this is the fix for E2)
- `test_cross_project_token_rejected`

`agent/src/index.test.ts`:
- `test_apiHeaders_uses_current_token_after_refresh`
- `test_401_triggers_refresh_then_retry`
- `test_refresh_failure_surfaces_as_task_failure`

## Acceptance criteria

- [ ] All 4 findings resolved per the audit
- [ ] No request returns 200 if vm_instance_id in token differs from task's current vm_instance_id
- [ ] Same-VM idempotent retry on status endpoint succeeds even after task is terminal
- [ ] Token refresh works mid-task; agent never fails a task purely because of token expiry
- [ ] Cross-project authz: agent for project A cannot affect tasks in project B

---

# W4 — Agent Execution Reliability

**Resolves:** C1, C2 (heartbeat); C3 (stream); C4 (handler dispatch); C5, C8 (process); C6 (websocket); C7 (folded into W3)
**Complexity:** medium (TS-only; no migration)

## Step 4.1 — Eliminate `void async` (C1, C2)

`agent/src/index.ts:504-557` (heartbeat setInterval):

Before:
```typescript
heartbeatTimer = setInterval(() => { void (async () => { ... })(); }, HEARTBEAT_INTERVAL);
```

After:
```typescript
heartbeatTimer = setInterval(() => {
  (async () => { ... })().catch(e => log(`[heartbeat] uncaught: ${e}`));
}, HEARTBEAT_INTERVAL);
```

`agent/src/index.ts:490-502` (initial heartbeat) — convert to awaited synchronous call before scheduling the timer. Use `sendHeartbeatSync` which already exists in the file. If `sendHeartbeatSync` doesn't exist, create a small helper that does an awaited POST.

```typescript
async function startHeartbeat(taskId: string, getMessage: () => string) {
  await sendHeartbeatSync(taskId, getMessage());  // awaited; if fails, returns false but we proceed
  heartbeatTimer = setInterval(...);
}
```

Audit the whole file for other `void (async ...)` patterns; same fix.

## Step 4.2 — Strict stream parsing (C3)

`agent/src/index.ts:2111-2130`:

```typescript
class ClaudeStreamError extends Error {
  constructor(public stage: string, message: string) { super(message); }
}

for (const line of text.split('\n').filter(Boolean)) {
  let obj: ClaudeOutput;
  try { obj = JSON.parse(line) as ClaudeOutput; } catch { sendLog(ws, text); continue; }

  switch (obj.type) {
    case 'assistant':
      if (obj.message?.content) { /* existing handling */ }
      break;
    case 'result':
      lastResultText = obj.result ?? '';
      break;
    case 'error':
      throw new ClaudeStreamError('claude', obj.message ?? 'unknown stream error');
    case 'system':
    case 'user':
      // benign; ignore
      break;
    default:
      log(`[stream] unknown type: ${obj.type}`);
  }
}
```

Outer try/catch around the runner: catch `ClaudeStreamError`, surface as task failure with `failure_category: PROVIDER_ERROR`.

## Step 4.3 — Strict handler dispatch (C4)

`agent/src/index.ts:2294-2299`:

```typescript
const handler = handlersMap[taskType];
if (!handler) {
  await reportPreClaimFailure(taskId, `Unknown task_type: ${taskType}`, 'UNKNOWN_TASK_TYPE');
  process.exit(1);
}
```

Add a build-time inventory test in `agent/src/index.test.ts`:

```typescript
test('every task type produced by backend has a handler', async () => {
  const knownTypes = ['create', 'fix', 'debug', 'plan', 'review', /* ... pull from a shared constants file */];
  for (const t of knownTypes) expect(handlers).toHaveProperty(t);
});
```

Better: pull the canonical task_type list from a shared constants file (e.g., `agent/src/constants.ts`), and have the backend reference the same list (a JSON manifest). Outside scope for W4 if too involved — note as follow-up.

## Step 4.4 — Process kill safety (C5)

`agent/src/index.ts:729-761`:

```typescript
const originalPid = proc.pid;
const termTimer = setTimeout(() => {
  timedOut = true;
  if (proc.pid !== originalPid || proc.exitCode !== null) return;
  try { process.kill(-originalPid, 'SIGTERM'); } catch { }
  killTimer = setTimeout(() => {
    if (proc.pid !== originalPid || proc.exitCode !== null) return;
    try { process.kill(-originalPid, 'SIGKILL'); } catch { }
  }, 5000);
}, timeoutMs);
```

## Step 4.5 — OOM detection (C8)

`agent/src/index.ts:756-759`:

```typescript
proc.on('close', (code, signal) => {
  clearTimeout(termTimer);
  if (killTimer) clearTimeout(killTimer);
  // ...
  if (signal === 'SIGKILL' && !timedOut) {
    err.code = 'EOOM';  // External SIGKILL — most likely cgroup OOM
  } else if (timedOut) {
    err.code = 'ETIMEDOUT';
  }
  reject(err);
});
```

Backend `failure_classification.py`: add EOOM matcher → map to `AGENT_TIMEOUT` category (similar retry semantics, but distinct fingerprint stage so monitoring can split OOM vs timeout).

## Step 4.6 — WebSocket connect-fail surface (C6)

`agent/src/index.ts:608-635`:

```typescript
async function connectLogStream(taskId: string): Promise<WebSocket | null> {
  // ... existing retry loop ...
  log(`[ws] failed to connect after ${maxRetries} attempts; logs may be incomplete`);
  logStreamFailed = true;  // module-level flag
  return null;
}
```

In the final status POST, include `log_stream_failed: logStreamFailed`. Backend `agent_status.py` records this on `task.context['log_stream_failed'] = True` (this one stays in JSONB — informational only).

## Step 4.7 — Tests

`agent/src/index.test.ts`:
- `test_heartbeat_async_iife_propagates_errors`
- `test_initial_heartbeat_awaited`
- `test_claude_stream_error_type_throws`
- `test_unknown_task_type_exits_with_failure`
- `test_kill_timer_aborts_if_pid_reused`
- `test_sigkill_classified_as_eoom`
- `test_websocket_connect_failure_sets_flag`

Run: `cd agent && npx tsc --noEmit && npm test`

## Acceptance criteria

- [ ] Zero `void (async ...)()` patterns remain in `agent/src/`
- [ ] Stream `type: error` results in classified task failure (not silent success)
- [ ] Unknown task_type causes immediate exit, not silent fallback
- [ ] All new tests pass

---

# W5 — Pool Resilience & Failure Classification

**Resolves:** E4 (pool exhaustion); B1 (rate-limit signal); A6 (smoketest spawn)
**Complexity:** small (single PR, hours)

## Step 5.1 — `MaxPlanPoolBreaker` (E4)

`backend/app/max_plan_pool.py`:

```python
class PoolState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class _BreakerState:
    state: PoolState = PoolState.CLOSED
    consecutive_misses: int = 0
    opened_at: datetime | None = None
    half_open_test_count: int = 0

_breaker = _BreakerState()
_BREAKER_LOCK = asyncio.Lock()
MISS_THRESHOLD = 5
OPEN_DURATION = timedelta(seconds=30)
HALF_OPEN_TESTS = 3

async def select_max_plan_account_with_breaker(...) -> Account | None:
    async with _BREAKER_LOCK:
        now = _utcnow()
        if _breaker.state == PoolState.OPEN:
            if now - _breaker.opened_at >= OPEN_DURATION:
                _breaker.state = PoolState.HALF_OPEN
                _breaker.half_open_test_count = 0
            else:
                return None  # circuit open

    account = await select_max_plan_account(...)  # existing impl

    async with _BREAKER_LOCK:
        if account is None:
            _breaker.consecutive_misses += 1
            if _breaker.state == PoolState.CLOSED and _breaker.consecutive_misses >= MISS_THRESHOLD:
                _breaker.state = PoolState.OPEN
                _breaker.opened_at = _utcnow()
                await _emit_pool_alert("opened")
            elif _breaker.state == PoolState.HALF_OPEN:
                _breaker.state = PoolState.OPEN
                _breaker.opened_at = _utcnow()
                await _emit_pool_alert("reopened")
        else:
            _breaker.consecutive_misses = 0
            if _breaker.state == PoolState.HALF_OPEN:
                _breaker.half_open_test_count += 1
                if _breaker.half_open_test_count >= HALF_OPEN_TESTS:
                    _breaker.state = PoolState.CLOSED
                    await _emit_pool_alert("closed")
    return account
```

Orchestrator dispatch must call the breaker variant; return 503 with Retry-After when None and breaker is OPEN.

Add `/api/admin/pool/breaker` GET endpoint exposing current state for the admin UI.

## Step 5.2 — Defensive rate-limit signal (B1)

`backend/app/failure_classification.py:315-324`:

```python
def _persist_rate_limit_signal_context(task: Task, signal) -> None:
    kind = getattr(signal, "kind", None)
    retry_after = getattr(signal, "retry_after_seconds", None)
    matched = getattr(signal, "matched_pattern", None)

    ctx = dict(task.context or {})
    ctx["rate_limit_kind"] = kind or "unknown"
    if retry_after is not None:
        ctx["rate_limit_retry_after_seconds"] = retry_after
    if matched is not None:
        ctx["rate_limit_matched_pattern"] = matched
    if kind is None:
        ctx["rate_limit_persistence_failed"] = True
    task.context = ctx
```

Downstream retry logic: if `rate_limit_persistence_failed`, apply conservative cooldown (e.g., 5 min) instead of immediate requeue.

## Step 5.3 — Smoketest spawn surface (A6)

`backend/app/routers/agent_status.py:725-833`:

```python
try:
    plan_task = await _spawn_smoketest_plan_task(...)
except Exception as e:
    logger.error("[smoketest] spawn failed: %s", e, exc_info=True)
    task.spawn_failed = True  # new column from W1 migration
    await _create_operator_alert(session, task.project_id, "smoketest_spawn_failed",
        f"Task {task.id} smoketest failure detected but plan task spawn failed: {e}")
    # Don't re-raise — task itself completed correctly. The alert fires for operator.
```

Add admin UI surfacing of `spawn_failed=True` tasks. Out of scope for this PR's backend changes if UI bandwidth is constrained.

## Step 5.4 — Tests

`backend/testing/test_max_plan_pool.py` (new or extend):
- `test_breaker_opens_after_threshold_misses`
- `test_breaker_half_open_recovers_after_window`
- `test_breaker_reopens_on_half_open_failure`

`backend/testing/test_failure_classification.py` (extend):
- `test_rate_limit_signal_missing_kind_marks_persistence_failed`
- `test_conservative_cooldown_when_persistence_failed`

`backend/testing/test_agent_status.py`:
- `test_smoketest_spawn_failure_creates_alert_and_sets_spawn_failed`

## Acceptance criteria

- [ ] Pool exhaustion stops dispatch (no infinite retry storm)
- [ ] Rate-limit classification never silently drops to immediate retry on signal failure
- [ ] Smoketest spawn failures surface as operator alerts

---

# Cross-cutting Q3 addendum: full `task.context` audit

W1 Step 1.1 produces the inventory. Promote any field that meets ≥1 of:
- Used in WHERE clause anywhere
- Read in idempotency/dedup guard
- Subject to RMW race (mutated in multiple sites)
- Needs fresh read after concurrent write

The promote list above (10 fields) is the known set; the inventory may surface more. For each new promotion, follow the same template:
- Add column to migration 0045 (or a follow-up 0048 if 0045 already merged)
- Add to `Task` model
- Add backfill SQL
- Strip from JSONB
- Update all `task.context[...]` reads/writes to use the column

Keep `task.context` JSONB only for:
- Truly opaque task-input metadata (the `payload` field)
- Free-form data that's read-once for display
- Blacklist (complex nested JSON)

---

# Validation strategy

After each workstream PR:

1. **Unit & integration tests**: `bash scripts/test-backend.sh --postgres` (full suite).
2. **Frontend if touched**: `cd admin-ui && npm run build && npx eslint .`
3. **Agent if touched**: `cd agent && npx tsc --noEmit && npm test`
4. **Migration smoke**: apply to staging snapshot, verify backfill correctness.
5. **Lint**: ruff on touched files; eslint on touched TS files.

After all 6 workstreams:

- 48-72h soak in production
- Monitor: `audit_pipeline_failures` table (or equivalent) for regressions
- Confirm "DB contention 30-40% baseline" per `project_deferred_robustness.md` drops materially

---

# What this plan does NOT cover

- Medium-severity findings (~35) — opportunistic cleanup, separate planning
- Test coverage gaps (D11, C15) beyond what's needed to validate the fixes
- Performance optimization unrelated to correctness
- Admin UI changes (only backend exposure of new state; UI follow-up)

# What an autonomous agent should NOT do

- Modify existing migration files (`backend/alembic/versions/0044_*` and earlier)
- Modify CI workflows (`.github/workflows/`)
- Modify scripts (`scripts/`)
- Skip the schema-revision bump
- Use `git push --force` or `--no-verify`

# Recovery if something breaks

- Each migration has a downgrade path (W1, W2)
- W3 is non-migrating; revert backend + agent simultaneously
- W4/W5 are revert-friendly (small surface)
- W6 is a single small change; revert is trivial
