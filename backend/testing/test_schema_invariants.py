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
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.models import Project, SystemMaturity, Task, TaskStatus


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
