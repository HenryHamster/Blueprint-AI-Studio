"""Runtime invariants over schema-layer behaviors.

Round 4 audit fix R4-W3 (H10 + M5): zero coverage existed for two storage-
layer behaviors that the rest of the codebase silently depends on. If either
regresses, dozens of call-sites silently misbehave (tz-aware writes leaking
into naive comparisons, bulk archive looking "stale" forever).

Distinct from ``test_invariants.py`` which is intentionally
``--noconftest``-runnable and contains only structural / synchronous
assertions; these tests need a real DB session.

Coverage:

- **NaiveUTCDateTime ``process_bind_param``** (round 3 audit M1, alembic
  0061 + models.py): tz-aware datetimes written through the ORM must be
  normalized to naive UTC. Without this, the first call-site that switches
  from ``_utcnow()`` to ``datetime.now(timezone.utc)`` would land tz-aware
  values that asyncpg encodes correctly but downstream Python comparisons
  (e.g. ``task.last_heartbeat < cutoff_naive``) raise ``TypeError`` on.

- **Bulk archive ``updated_at`` advancement** (services.py / admin.py wave
  F4): bulk Core ``UPDATE(Task)... .values(archived=True, version=Task.version+1)``
  paths rely on the column-level ``onupdate=_utcnow`` hook firing for Core
  UPDATEs. SQLAlchemy *does* honor column-level ``onupdate`` for Core paths
  (mapper-level ``onupdate=`` would not), but no test pins this contract,
  so a future migration that drops the column-level hook would silently
  break the LRU-by-updated_at archive logic.

- **Alembic 0068 backfill data effects** (R5 audit MEDIUM-D test 1):
  ``UPDATE task SET heartbeat_count = COALESCE(...)::int WHERE ...``
  must (a) backfill numeric values from ``context['_heartbeat_count']``,
  and (b) NOT crash if any row holds a non-numeric value at that subkey.
  Postgres-only — SQLite stores JSONB as TEXT so ``jsonb_typeof`` doesn't
  apply.

- **ix_task_payloads_type_active partial expression index** (R5-W5 audit
  MED-6 / R4-W2 H5): alembic 0067 creates a partial expression index on
  ``(payloads -> 0 ->> 'type')`` WHERE status IN ('PROVISIONING',
  'IN_PROGRESS') so the orchestrator pipeline-budget query (every 5s)
  can filter active tasks by type without SeqScanning every active row.
  The index must exist, be partial (not waste pages on terminal/queued
  rows), and reference the JSON expression (a column-only B-tree on
  ``payloads`` would be ignored by the planner). Postgres-only — SQLite
  test path uses a different fallback index per 0067's branch.

- **ix_task_retry_after_active partial index** (R6 audit / R5-W2-C):
  alembic 0069 creates a partial index on ``retry_after_until`` WHERE
  ``status = 'QUEUED' AND retry_after_until IS NOT NULL`` so the
  reaper's stale ``retry_after`` sweep doesn't degrade to a linear scan
  of the QUEUED queue at 1k users. Same shape of test as the
  payloads_type_active pair (existence + partial-WHERE + EXPLAIN).

- **NaiveUTCDateTime symmetric writes through R5-W4 columns** (R6
  audit): R5-W4 routed 22 additional datetime columns through
  ``NaiveUTCDateTime``. The existing tests cover Project.created_at and
  SystemMaturity.last_audited_at; this section spot-checks the
  heartbeat path (Task.last_heartbeat) and the auto-disable path
  (MaxPlanAccount.disabled_at) so a misconfigured ``sa_column`` on
  either column fails the suite.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update
from sqlmodel import select

from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    MaxPlanAccount,
    Project,
    SystemMaturity,
    Task,
    TaskStatus,
)
from app.schemas import serialize_compute_job_response


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def test_compute_job_response_serializes_status_outcome_and_result_reference():
    job = ComputeJob(
        project_id=uuid.uuid4(),
        backend="gcp",
        external_compute_id="vm-blueprint-123",
        machine_type="e2-standard-4",
        status=ComputeJobStatus.RUNNING,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=12.5,
        accrued_cost_usd=1.25,
        result_reference="gs://blueprint-artifacts/jobs/123/result.json",
        polling_metadata={"zone": "us-central1-a", "operation": "op-123"},
        poll_attempt_count=2,
    )

    payload = serialize_compute_job_response(job)

    assert payload["status"] == "RUNNING"
    assert payload["outcome"] == "SUCCESS"
    assert payload["external_compute_id"] == "vm-blueprint-123"
    assert (
        payload["result_reference"] == "gs://blueprint-artifacts/jobs/123/result.json"
    )
    assert payload["polling_metadata"] == {
        "zone": "us-central1-a",
        "operation": "op-123",
    }
    assert payload["poll_attempt_count"] == 2
    assert isinstance(payload["created_at"], str)


# ── NaiveUTCDateTime process_bind_param ────────────────────────────────────


@pytest.mark.asyncio
async def test_naive_utc_datetime_strips_aware_writes(db_session):
    """Tz-aware datetime writes get normalized to naive UTC on the way to the DB.

    Round 3 audit M1: ``NaiveUTCDateTime.process_bind_param`` converts to UTC
    then strips tzinfo so the column stays internally consistent. Without this,
    tz-aware values would land in a column whose Python loads strip tz, so
    callers would see different values than they wrote.

    Tests against ``Project.created_at`` because it uses ``NaiveUTCDateTime``
    (the columns that exercise the symmetric write path).

    Note: ``Task.last_heartbeat`` was migrated to ``NaiveUTCDateTime`` in
    R5-W4 (it was previously plain DateTime); see
    ``test_task_last_heartbeat_strips_aware_writes`` below for the
    Task-side equivalent.
    """
    project = Project(name="naive-aware-write", user_id="user-naive-write")
    db_session.add(project)
    await db_session.commit()

    # Write tz-aware datetime in non-UTC zone (UTC+8 → 04:00 UTC) directly to a
    # NaiveUTCDateTime column. Use Project.created_at so the write goes through
    # the decorator's process_bind_param.
    aware_input = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    project.created_at = aware_input
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # process_bind_param must have stripped the tz; process_result_value
    # is also expected to strip on read. Both paths convert to UTC first.
    assert project.created_at is not None
    assert project.created_at.tzinfo is None, (
        f"expected naive datetime, got tzinfo={project.created_at.tzinfo!r} "
        "— NaiveUTCDateTime.process_bind_param did not strip tz on write"
    )
    # 12:00 UTC+8 = 04:00 UTC → 04:00 naive.
    assert project.created_at == datetime(2026, 1, 1, 4, 0), (
        f"expected 2026-01-01 04:00 (UTC), got {project.created_at!r} — "
        "NaiveUTCDateTime did not convert tz-aware UTC+8 to UTC before stripping"
    )


@pytest.mark.asyncio
async def test_naive_utc_datetime_strips_aware_writes_via_systemmaturity(db_session):
    """Same as above but through SystemMaturity.last_audited_at.

    Round 3 audit H9 routes SystemMaturity timestamps through
    ``NaiveUTCDateTime`` exactly so this symmetry holds — we pin the
    contract independently of the Task path so a refactor of either model
    can't regress just the other.
    """
    project = Project(name="naive-systemmaturity", user_id="user-naive-sm")
    db_session.add(project)
    await db_session.commit()

    aware_input = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    sm = SystemMaturity(
        project_id=project.id,
        system_id="example-system",
        score=42,
        last_audited_at=aware_input,
    )
    db_session.add(sm)
    await db_session.commit()
    await db_session.refresh(sm)

    assert sm.last_audited_at.tzinfo is None
    assert sm.last_audited_at == datetime(2026, 1, 1, 4, 0)


@pytest.mark.asyncio
async def test_naive_utc_datetime_passthrough_for_naive_writes(db_session):
    """Naive datetime writes are passed through unchanged.

    The decorator is a no-op when the input is already naive — no spurious
    tz conversion (which would mangle the value if the column ever migrated
    to TIMESTAMP without a session-level ``timezone='UTC'`` setting).
    """
    project = Project(name="naive-passthrough", user_id="user-naive-pass")
    db_session.add(project)
    await db_session.commit()

    naive_input = datetime(2026, 1, 1, 4, 0)  # already naive UTC
    project.created_at = naive_input
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert project.created_at == naive_input
    assert project.created_at.tzinfo is None


# ── Bulk archive updated_at bump (M5) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_archive_bumps_updated_at(db_session):
    """Bulk Core UPDATE on Task with ``archived=True`` advances ``updated_at``.

    services._run_auto_archive_cycle and admin bulk-archive paths rely on
    the column-level ``onupdate=_utcnow`` firing for Core UPDATEs (mapper-
    level onupdate would NOT fire for ``update(Task).where(...).values(...)``,
    but column-level onupdate DOES). Pin this contract so a future migration
    can't silently drop the hook and leave the LRU-by-updated_at archive
    logic looking at stale timestamps forever.

    Belt-and-suspenders: F4's ``.values(version=Task.version+1)`` is the
    explicit forensic-counter bump; updated_at advancing automatically is
    the implicit complement.
    """
    project = Project(name="archive-updated-at", user_id="user-archive-bump")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="archive updated_at test",
        status=TaskStatus.COMPLETED,
        archived=False,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    initial_updated_at = task.updated_at
    assert initial_updated_at is not None

    # Mirror the F4 archive call shape from services.py / admin.py exactly.
    # Sleep long enough that the resolution-coarsened SQLite/Postgres
    # timestamp comparison can still resolve a strictly-greater value.
    import asyncio

    await asyncio.sleep(0.01)

    await db_session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(archived=True, version=Task.version + 1)
    )
    await db_session.commit()

    # Re-fetch to get the DB-side state — the in-memory ``task`` object
    # was bypassed by the Core UPDATE.
    refreshed = (
        await db_session.execute(select(Task).where(Task.id == task.id))
    ).scalar_one()
    await db_session.refresh(refreshed)

    assert refreshed.archived is True
    assert refreshed.version == 2, (
        f"expected explicit version=2 bump, got {refreshed.version}"
    )
    assert refreshed.updated_at > initial_updated_at, (
        f"expected updated_at to advance via column-level onupdate=_utcnow, "
        f"got initial={initial_updated_at!r} → refreshed={refreshed.updated_at!r}"
    )


@pytest.mark.asyncio
async def test_bulk_archive_updated_at_naive_after_bump(db_session):
    """The auto-bumped ``updated_at`` lands as a naive datetime.

    ``NaiveUTCDateTime.process_bind_param`` plus ``onupdate=_utcnow`` (which
    returns naive UTC) keep the column on the naive-UTC contract even when
    the bump happens implicitly during a Core UPDATE. Without this, anything
    that compares ``updated_at`` to ``_utcnow()`` (reaper, archive cycle,
    rollup management) would crash on tz mix.
    """
    project = Project(name="archive-naive-after-bump", user_id="user-archive-naive")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="archive naive bump test",
        status=TaskStatus.COMPLETED,
        archived=False,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    await db_session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(archived=True, version=Task.version + 1)
    )
    await db_session.commit()

    refreshed = (
        await db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    await db_session.refresh(refreshed)

    assert refreshed.updated_at.tzinfo is None


# ── Alembic 0068 backfill data effects (R5 audit MEDIUM-D test 1) ──────────


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "Migration 0068 backfill SQL uses jsonb_typeof and ::int cast — "
        "Postgres-only. SQLite stores JSONB as TEXT and skips this path."
    ),
)
@pytest.mark.asyncio
async def test_migration_0068_backfill_normal_numeric_value(db_session):
    """Migration 0068's UPDATE backfills heartbeat_count from a numeric
    value at context['_heartbeat_count'].

    The migration runs once at test setup time, so by the time this test
    fires the UPDATE has already been applied to whatever rows existed
    at upgrade time. To test the data-effect contract independent of
    that one-shot, we re-run the same SQL on a row we control: insert
    a task with heartbeat_count=0 and ``context['_heartbeat_count']=42``,
    then execute the migration's UPDATE statement. The COALESCE+cast
    must promote 42 into the column.
    """
    project = Project(name="0068-backfill-numeric", user_id="user-bf-num")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="bf-numeric",
        context={"_heartbeat_count": 42, "marker": "numeric"},
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    # Re-execute the exact UPDATE from migration 0068 (with the R5-W4
    # MEDIUM-B guard included — the migration's current SQL).
    await db_session.execute(
        text(
            """
        UPDATE task
        SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
        WHERE jsonb_typeof(context) = 'object'
          AND context ? '_heartbeat_count'
          AND jsonb_typeof(context->'_heartbeat_count') = 'number'
          AND heartbeat_count = 0
          AND id = :tid
        """
        ),
        {"tid": str(task_id)},
    )
    await db_session.commit()

    # populate_existing bypasses the identity-map cache (the task added
    # by db_session.add lives in the cache with heartbeat_count=0; the
    # raw-SQL UPDATE bypassed the ORM so the cache is stale).
    refreshed = (
        await db_session.execute(
            select(Task)
            .where(Task.id == task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    assert refreshed.heartbeat_count == 42, (
        f"expected backfill to copy 42 from context, got {refreshed.heartbeat_count}"
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="Postgres-only: ``::int`` cast on JSONB string is the failure mode under test.",
)
@pytest.mark.asyncio
async def test_migration_0068_backfill_skips_non_numeric_garbage(db_session):
    """Migration 0068's UPDATE must NOT crash on rows whose
    ``context['_heartbeat_count']`` is non-numeric (string, array, null).

    R5 audit MEDIUM-B: the unguarded ``::int`` cast aborted the whole
    migration if any row held e.g. ``"abc"``. The added
    ``jsonb_typeof(context->'_heartbeat_count') = 'number'`` filter
    skips garbage rows safely. This test inserts three corrupted rows
    plus one valid row, runs the UPDATE, and asserts:
      - no exception raised (migration would have aborted otherwise),
      - valid row got backfilled,
      - corrupted rows stay at heartbeat_count=0.
    """
    project = Project(name="0068-backfill-garbage", user_id="user-bf-garb")
    db_session.add(project)
    await db_session.commit()

    valid = Task(
        project_id=project.id,
        title="valid",
        context={"_heartbeat_count": 7},
    )
    bad_str = Task(
        project_id=project.id,
        title="bad-str",
        context={"_heartbeat_count": "abc"},
    )
    bad_array = Task(
        project_id=project.id,
        title="bad-array",
        context={"_heartbeat_count": [1, 2, 3]},
    )
    bad_null = Task(
        project_id=project.id,
        title="bad-null",
        context={"_heartbeat_count": None},
    )
    db_session.add_all([valid, bad_str, bad_array, bad_null])
    await db_session.commit()
    ids = {
        "valid": valid.id,
        "bad_str": bad_str.id,
        "bad_array": bad_array.id,
        "bad_null": bad_null.id,
    }

    # Run the guarded backfill SQL — must not raise.
    await db_session.execute(
        text(
            """
        UPDATE task
        SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
        WHERE jsonb_typeof(context) = 'object'
          AND context ? '_heartbeat_count'
          AND jsonb_typeof(context->'_heartbeat_count') = 'number'
          AND heartbeat_count = 0
          AND project_id = :pid
        """
        ),
        {"pid": str(project.id)},
    )
    await db_session.commit()

    # Force-refresh: identity map under expire_on_commit=False would
    # return cached pre-UPDATE state; populate_existing on the SELECT
    # bypasses the cache (mirrors task_completion.py:1671 / reaper.py:382).
    rows = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}

    assert by_id[ids["valid"]].heartbeat_count == 7, (
        f"valid row should have been backfilled, got {by_id[ids['valid']].heartbeat_count}"
    )
    # Each garbage row stays at the column default, NOT crashed.
    for label in ("bad_str", "bad_array", "bad_null"):
        assert by_id[ids[label]].heartbeat_count == 0, (
            f"{label} row should have been skipped (heartbeat_count=0), "
            f"got {by_id[ids[label]].heartbeat_count}"
        )


# ── ix_task_payloads_type_active index shape (R5-W5 MED-6) ─────────────────


@pytest.mark.skipif(
    not USE_POSTGRES, reason="expression-on-JSONB index is Postgres-only"
)
@pytest.mark.asyncio
async def test_ix_task_payloads_type_active_exists_and_is_partial(db_session):
    """The orchestrator pipeline-budget index must be a partial expression index.

    Round 4 W2 / H5: orchestrator.py line ~1771 issues every 5s
    ``select count(*) from task where status in ('PROVISIONING','IN_PROGRESS')
    and payloads[0]['type'] = ANY('{review','fix'}')``. Without an index that
    matches the predicate plus the expression, the planner falls back to a
    SeqScan over every active task row — every 5s, forever.

    Migration 0067 creates::

        CREATE INDEX CONCURRENTLY ix_task_payloads_type_active
        ON task ((payloads -> 0 ->> 'type'))
        WHERE status IN ('PROVISIONING', 'IN_PROGRESS')

    Tests:
      1. The index exists in pg_indexes (alembic upgrade actually ran it).
      2. The index definition references the JSON expression (a future
         migration that "simplifies" to ``ON task (payloads)`` would be a
         silent regression — the column-only B-tree wouldn't be used by
         the type filter).
      3. The index has a WHERE clause restricting to active statuses (the
         partial predicate; without it the index spans every task ever
         created and pages out hot rows under cache pressure).
    """
    rows = (
        await db_session.execute(
            text(
                """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND tablename = 'task'
               AND indexname = 'ix_task_payloads_type_active'
            """
            )
        )
    ).all()

    assert rows, (
        "ix_task_payloads_type_active not found in pg_indexes — "
        "alembic 0067 either didn't run or was reverted; the orchestrator "
        "pipeline-budget query will SeqScan every dispatch cycle (every 5s)."
    )

    indexdef = rows[0][0]
    indexdef_lower = indexdef.lower()

    # (2) JSON expression reference — must mention ``payloads`` AND the
    # 'type' subkey so a column-only-on-payloads regression fails this.
    # Postgres normalizes the expression to e.g.
    # ``btree (((payloads -> 0) ->> 'type'::text))`` so we look for
    # the substring rather than an exact match.
    assert "payloads" in indexdef_lower and "'type'" in indexdef_lower, (
        f"ix_task_payloads_type_active no longer references payloads[0]['type'] "
        f"expression — got indexdef={indexdef!r}. The orchestrator filter "
        f"``Task.payloads[0]['type'].as_string().in_(...)`` will not be "
        f"matched by a plain column index."
    )

    # (3) Partial predicate must restrict to active statuses.
    assert "where" in indexdef_lower, (
        f"ix_task_payloads_type_active is no longer a partial index — "
        f"got indexdef={indexdef!r}. A non-partial would still satisfy the "
        f"planner but waste index pages on terminal/queued rows that the "
        f"orchestrator filter never touches."
    )
    # Both 'provisioning' and 'in_progress' must appear in the WHERE clause
    # (case-insensitive). Postgres normalizes string literals as-typed; the
    # migration uses uppercase.
    assert "provisioning" in indexdef_lower, (
        f"partial WHERE clause should include 'PROVISIONING'; got {indexdef!r}"
    )
    assert "in_progress" in indexdef_lower, (
        f"partial WHERE clause should include 'IN_PROGRESS'; got {indexdef!r}"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="EXPLAIN plan inspection is Postgres-only")
@pytest.mark.asyncio
async def test_pipeline_budget_query_uses_payloads_type_index(db_session):
    """The expression index ix_task_payloads_type_all must match the query predicate.

    Even when an index exists, the planner ignores it if the query predicate
    doesn't match the index expression — a typo in the JSON path would
    silently revert the speedup. EXPLAIN catches that.

    We test with a type-only filter (no status condition) so that
    ix_task_payloads_type_all is the ONLY usable index. This avoids a
    known planner instability: with both a status filter and a type filter,
    the planner may choose ix_task_status_priority_created (a simple column
    index) over either expression index — that choice is statistics-dependent
    and switches between "with ANALYZE" and "without ANALYZE" runs.
    By dropping the status condition the status indexes become irrelevant and
    the planner must use ix_task_payloads_type_all (alembic 0238) if seqscan
    and bitmapscan are disabled.

    The production query also matches ix_task_payloads_type_active (partial,
    PROVISIONING/IN_PROGRESS only); that index's shape is verified separately
    by test_ix_task_payloads_type_active_exists_and_is_partial.
    """
    project = Project(name="pipeline-budget-explain", user_id="user-pb")
    db_session.add(project)
    await db_session.commit()

    tasks = []
    for i in range(20):
        status = TaskStatus.IN_PROGRESS if i % 2 == 0 else TaskStatus.QUEUED
        ttype = "review" if i % 3 == 0 else ("fix" if i % 3 == 1 else "research")
        tasks.append(
            Task(
                project_id=project.id,
                title=f"pb-{i}",
                payloads=[{"type": ttype, "prompt": "x"}],
                status=status,
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    # Disable seqscan and bitmapscan. With no status filter in the query,
    # the only applicable index is ix_task_payloads_type_all — the planner
    # must use it (or fail to find a plan, which would expose a missing index).
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    # EXPLAIN a type-only filter — forces ix_task_payloads_type_all, no competition.
    try:
        explain_rows = (
            await db_session.execute(
                text(
                    """
                EXPLAIN (FORMAT TEXT)
                SELECT count(*) FROM task
                 WHERE (payloads -> 0 ->> 'type') IN ('review', 'fix')
                """
                )
            )
        ).all()
        plan = "\n".join(row[0] for row in explain_rows)
    finally:
        await db_session.execute(text("RESET enable_seqscan"))
        await db_session.execute(text("RESET enable_bitmapscan"))

    assert "ix_task_payloads_type_all" in plan, (
        f"type-filter query plan does not use ix_task_payloads_type_all — "
        f"got plan:\n{plan}\n"
        f"This means ix_task_payloads_type_all either doesn't exist, doesn't "
        f"match the predicate (payloads -> 0 ->> 'type'), or was dropped. "
        f"The orchestrator dispatch loop and admin task-type filters will "
        f"SeqScan. Re-check alembic 0238 and the filter shape."
    )


# ── ix_task_retry_after_active index shape (R6 audit / R5-W2-C) ────────────


@pytest.mark.skipif(
    not USE_POSTGRES, reason="partial expression index is Postgres-only"
)
@pytest.mark.asyncio
async def test_ix_task_retry_after_active_exists_and_is_partial(db_session):
    """The reaper's retry_after sweep index must be a partial index on
    ``retry_after_until`` filtered to QUEUED rows whose retry hold is set.

    R5-W2-C / alembic 0069: the reaper sweeps every cycle with
    ``status == QUEUED AND retry_after_until IS NOT NULL`` and the
    composite indexes don't match that exact partial predicate. Without
    this index the LIMIT-5000 sweep degrades to linear-scan cost at 1k
    users.

    Mirror of test_ix_task_payloads_type_active_exists_and_is_partial —
    same three checks (exists / partial / WHERE clause).
    """
    rows = (
        await db_session.execute(
            text(
                """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND tablename = 'task'
               AND indexname = 'ix_task_retry_after_active'
            """
            )
        )
    ).all()

    assert rows, (
        "ix_task_retry_after_active not found in pg_indexes — "
        "alembic 0069 either didn't run or was reverted; the reaper's "
        "stale retry_after sweep will linear-scan the QUEUED queue every cycle."
    )

    indexdef = rows[0][0]
    indexdef_lower = indexdef.lower()

    assert "retry_after_until" in indexdef_lower, (
        f"ix_task_retry_after_active no longer references retry_after_until — "
        f"got indexdef={indexdef!r}."
    )
    assert "where" in indexdef_lower, (
        f"ix_task_retry_after_active is no longer a partial index — "
        f"got indexdef={indexdef!r}. A non-partial would still satisfy the "
        f"planner but waste index pages on rows the reaper sweep never touches."
    )
    assert "queued" in indexdef_lower, (
        f"partial WHERE clause should restrict to QUEUED status; got {indexdef!r}"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="EXPLAIN plan inspection is Postgres-only")
@pytest.mark.asyncio
async def test_reaper_retry_after_query_uses_retry_index(db_session):
    """The reaper's retry_after sweep query plan must use the index.

    Mirror of test_pipeline_budget_query_uses_payloads_type_index — even
    when the index exists, the planner can ignore it if the predicate
    shape doesn't match. EXPLAIN catches that.
    """
    project = Project(name="reaper-retry-explain", user_id="user-rrx")
    db_session.add(project)
    await db_session.commit()

    # Mix of QUEUED-with-retry-hold, QUEUED-without, and other statuses
    # so the planner has reason to consider the partial index.
    now = datetime.utcnow()
    tasks = []
    for i in range(20):
        status = TaskStatus.QUEUED if i % 2 == 0 else TaskStatus.IN_PROGRESS
        retry_after = now + timedelta(seconds=30) if (i % 4 == 0) else None
        tasks.append(
            Task(
                project_id=project.id,
                title=f"retry-{i}",
                payloads=[{"type": "research"}],
                status=status,
                retry_after_until=retry_after,
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    await db_session.execute(text("ANALYZE task"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    try:
        explain_rows = (
            await db_session.execute(
                text(
                    """
                EXPLAIN (FORMAT TEXT)
                SELECT id FROM task
                 WHERE status = 'QUEUED'
                   AND retry_after_until IS NOT NULL
                 LIMIT 5000
                """
                )
            )
        ).all()
        plan = "\n".join(row[0] for row in explain_rows)
    finally:
        await db_session.execute(text("RESET enable_seqscan"))
        await db_session.execute(text("RESET enable_bitmapscan"))

    assert "ix_task_retry_after_active" in plan, (
        f"reaper retry_after sweep query plan does not use "
        f"ix_task_retry_after_active — got plan:\n{plan}\n"
        f"This means the index either doesn't match the predicate or was dropped. "
        f"The reaper sweep will linear-scan the QUEUED queue every cycle. "
        f"Re-check alembic 0069 and reaper.py."
    )


# ── NaiveUTCDateTime coverage on R5-W4 columns (R6 audit) ──────────────────


@pytest.mark.asyncio
async def test_task_last_heartbeat_strips_aware_writes(db_session):
    """R5-W4 routed Task.last_heartbeat from plain DateTime → NaiveUTCDateTime.

    Tz-aware writes must be normalized to naive UTC, mirroring the
    contract pinned for Project.created_at and SystemMaturity.last_audited_at.
    Without this, the reaper's ``task.last_heartbeat < cutoff_naive`` check
    raises TypeError on tz-aware values from any caller using
    ``datetime.now(timezone.utc)``.
    """
    project = Project(name="last-heartbeat-aware", user_id="user-lh")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="last_heartbeat tz test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(task)
    await db_session.commit()

    aware_input = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    task.last_heartbeat = aware_input
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.last_heartbeat is not None
    assert task.last_heartbeat.tzinfo is None, (
        f"expected naive datetime, got tzinfo={task.last_heartbeat.tzinfo!r} — "
        "Task.last_heartbeat sa_column is no longer wired through NaiveUTCDateTime"
    )
    assert task.last_heartbeat == datetime(2026, 1, 1, 4, 0)


@pytest.mark.asyncio
async def test_max_plan_account_disabled_at_strips_aware_writes(db_session):
    """R5-W4 routed MaxPlanAccount.disabled_at to NaiveUTCDateTime.

    The auto-disable / recheck-disabled-accounts path compares
    ``disabled_at + cooldown < _utcnow()`` on every reaper tick. A
    tz-aware write here would raise TypeError on every comparison.
    """
    aware_input = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    account = MaxPlanAccount(
        user_id="user-disabled-test",
        name="test-disabled-account",
        disabled_at=aware_input,
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    assert account.disabled_at is not None
    assert account.disabled_at.tzinfo is None, (
        f"expected naive datetime, got tzinfo={account.disabled_at.tzinfo!r} — "
        "MaxPlanAccount.disabled_at sa_column is no longer wired through NaiveUTCDateTime"
    )
    assert account.disabled_at == datetime(2026, 1, 1, 4, 0)
