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
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update
from sqlmodel import select

from app.models import Project, SystemMaturity, Task, TaskStatus


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


# ── NaiveUTCDateTime process_bind_param ────────────────────────────────────


@pytest.mark.asyncio
async def test_naive_utc_datetime_strips_aware_writes(db_session):
    """Tz-aware datetime writes get normalized to naive UTC on the way to the DB.

    Round 3 audit M1: ``NaiveUTCDateTime.process_bind_param`` converts to UTC
    then strips tzinfo so the column stays internally consistent. Without this,
    tz-aware values would land in a column whose Python loads strip tz, so
    callers would see different values than they wrote.

    Tests against ``Task.created_at`` because it uses ``NaiveUTCDateTime`` (the
    columns that exercise the symmetric write path); ``last_heartbeat`` is a
    plain DateTime and would not exercise the decorator.
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
    refreshed = (await db_session.execute(
        select(Task).where(Task.id == task.id)
    )).scalar_one()
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

    refreshed = (await db_session.execute(
        select(Task).where(Task.id == task_id)
    )).scalar_one()
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
    await db_session.execute(text(
        """
        UPDATE task
        SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
        WHERE jsonb_typeof(context) = 'object'
          AND context ? '_heartbeat_count'
          AND jsonb_typeof(context->'_heartbeat_count') = 'number'
          AND heartbeat_count = 0
          AND id = :tid
        """
    ), {"tid": str(task_id)})
    await db_session.commit()

    # populate_existing bypasses the identity-map cache (the task added
    # by db_session.add lives in the cache with heartbeat_count=0; the
    # raw-SQL UPDATE bypassed the ORM so the cache is stale).
    refreshed = (await db_session.execute(
        select(Task)
        .where(Task.id == task_id)
        .execution_options(populate_existing=True)
    )).scalar_one()

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
        project_id=project.id, title="valid",
        context={"_heartbeat_count": 7},
    )
    bad_str = Task(
        project_id=project.id, title="bad-str",
        context={"_heartbeat_count": "abc"},
    )
    bad_array = Task(
        project_id=project.id, title="bad-array",
        context={"_heartbeat_count": [1, 2, 3]},
    )
    bad_null = Task(
        project_id=project.id, title="bad-null",
        context={"_heartbeat_count": None},
    )
    db_session.add_all([valid, bad_str, bad_array, bad_null])
    await db_session.commit()
    ids = {
        "valid": valid.id, "bad_str": bad_str.id,
        "bad_array": bad_array.id, "bad_null": bad_null.id,
    }

    # Run the guarded backfill SQL — must not raise.
    await db_session.execute(text(
        """
        UPDATE task
        SET heartbeat_count = COALESCE((context->>'_heartbeat_count')::int, 0)
        WHERE jsonb_typeof(context) = 'object'
          AND context ? '_heartbeat_count'
          AND jsonb_typeof(context->'_heartbeat_count') = 'number'
          AND heartbeat_count = 0
          AND project_id = :pid
        """
    ), {"pid": str(project.id)})
    await db_session.commit()

    # Force-refresh: identity map under expire_on_commit=False would
    # return cached pre-UPDATE state; populate_existing on the SELECT
    # bypasses the cache (mirrors task_completion.py:1671 / reaper.py:382).
    rows = (await db_session.execute(
        select(Task)
        .where(Task.project_id == project.id)
        .execution_options(populate_existing=True)
    )).scalars().all()
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


@pytest.mark.skipif(not USE_POSTGRES, reason="expression-on-JSONB index is Postgres-only")
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
    rows = (await db_session.execute(
        text(
            """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND tablename = 'task'
               AND indexname = 'ix_task_payloads_type_active'
            """
        )
    )).all()

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
    """The orchestrator pipeline-budget query plan must use the index.

    Even when the index exists, the planner can ignore it if the query is
    written so the predicate doesn't match — a typo in the JSON path or
    wrong status casing would silently revert the speedup. EXPLAIN catches
    that: the plan must mention ``ix_task_payloads_type_active`` somewhere
    (Index Scan / Index Only Scan / Bitmap Index Scan all qualify).

    Inserts a small fixture so the planner has a non-empty table, then
    EXPLAINs the query shape from orchestrator.py line ~1771.
    """
    project = Project(name="pipeline-budget-explain", user_id="user-pb")
    db_session.add(project)
    await db_session.commit()

    # Mix of types and statuses so the planner has reason to consider the
    # partial index. The exact count doesn't matter for EXPLAIN (vs
    # EXPLAIN ANALYZE), but a few rows make ANALYZE-driven plans realistic.
    tasks = []
    for i in range(20):
        status = TaskStatus.IN_PROGRESS if i % 2 == 0 else TaskStatus.QUEUED
        ttype = "review" if i % 3 == 0 else ("fix" if i % 3 == 1 else "research")
        tasks.append(Task(
            project_id=project.id,
            title=f"pb-{i}",
            payloads=[{"type": ttype, "prompt": "x"}],
            status=status,
        ))
    db_session.add_all(tasks)
    await db_session.commit()

    # ANALYZE so the planner has stats. Disable seqscan/bitmapscan: with
    # only 20 rows the planner will normally choose SeqScan regardless of
    # index quality (it's cheaper for tiny tables). We're not benchmarking;
    # we're verifying the planner *can* match the index against the
    # predicate. Forcing the planner past SeqScan and BitmapScan exposes
    # whether the partial expression matches at all.
    await db_session.execute(text("ANALYZE task"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    # EXPLAIN the exact filter shape the orchestrator issues.
    try:
        explain_rows = (await db_session.execute(
            text(
                """
                EXPLAIN (FORMAT TEXT)
                SELECT count(*) FROM task
                 WHERE status IN ('PROVISIONING', 'IN_PROGRESS')
                   AND (payloads -> 0 ->> 'type') IN ('review', 'fix')
                """
            )
        )).all()
        plan = "\n".join(row[0] for row in explain_rows)
    finally:
        # Reset planner flags so subsequent queries in the same session
        # (e.g. teardown) plan normally.
        await db_session.execute(text("RESET enable_seqscan"))
        await db_session.execute(text("RESET enable_bitmapscan"))

    assert "ix_task_payloads_type_active" in plan, (
        f"pipeline-budget query plan does not use ix_task_payloads_type_active — "
        f"got plan:\n{plan}\n"
        f"This means the index either doesn't match the predicate (typo? "
        f"wrong status casing?) or was dropped. The orchestrator dispatch "
        f"loop will SeqScan every 5s. Re-check alembic 0067 and the filter "
        f"shape at orchestrator.py:~1771."
    )
