"""Forensic-counter tests for ``Task.version`` (audit 2026-04-29 round 2 / C4 + C5).

Round 1 (audit 2026-04-29 Phase C4 / finding M-C4) shipped
``__mapper_args__ = {"version_id_col": _TASK_VERSION_COL}`` to enforce
optimistic-concurrency on Task. Round 2's review found that landed
half-deployed:

  - Bulk Core UPDATEs (``update(Task).where(...).values(...)``) bypass
    the mapper entirely; ``version`` was never bumped on those paths.
  - Zero ``StaleDataError`` handlers existed in the codebase, so every
    ORM commit could surface a 500 on concurrent stale writes.

Round 2 dropped the ``version_id_col`` mapper arg as a safer default
(C4 + C5). The ``version`` column itself is **retained as a forensic
counter**: bulk archive paths bump it explicitly via
``.values(version=Task.version + 1)`` (round 2 wave F4), and
``_release_parent_lock`` snapshot validation reads it for drift
detection (round 2 M10). Future paths can re-enable mapper-driven
enforcement once a ``StaleDataError``-handler sweep lands.

These tests document the intentional behavior change:
  - ORM-driven UPDATE no longer auto-bumps ``version`` (mapper arg
    dropped) — callers must set it explicitly when they want forensic
    tracking.
  - Concurrent stale ORM writes no longer raise ``StaleDataError`` —
    they apply unconditionally, last-writer-wins.
"""

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus


@pytest.mark.asyncio
async def test_orm_update_does_not_auto_increment_version(db_session):
    """ORM-driven UPDATEs no longer auto-bump ``Task.version``.

    Round 2 audit (C4 + C5) dropped the ``version_id_col`` mapper arg
    because bulk Core UPDATEs bypassed it and zero ``StaleDataError``
    handlers existed. The column stays as a forensic counter — callers
    that want incremented version values must set them explicitly.
    """
    project = Project(name="version-no-auto", user_id="user-version-no-auto")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="version no-auto test",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()

    # Default after INSERT (server_default ``1``).
    assert task.version == 1, f"expected version=1 after insert, got {task.version}"

    # First mutation via ORM — no auto-bump now that ``version_id_col`` is gone.
    task.title = "first mutation"
    db_session.add(task)
    await db_session.commit()
    assert task.version == 1, (
        f"expected version=1 after ORM mutation (mapper arg dropped), "
        f"got {task.version}"
    )

    # Second mutation — still no auto-bump.
    task.title = "second mutation"
    db_session.add(task)
    await db_session.commit()
    assert task.version == 1, (
        f"expected version=1 after second ORM mutation, got {task.version}"
    )


@pytest.mark.asyncio
async def test_explicit_version_bump_via_core_update(db_session):
    """Forensic tracking still works when the bulk-update path bumps explicitly.

    Round 2 wave F4 hardens bulk archive paths in ``services.py`` and
    ``admin.py`` to pass ``version=Task.version + 1`` in ``.values(...)``.
    This test exercises that pattern directly — Core ``update(Task)`` with
    an explicit version bump should land cleanly, leaving the column at a
    higher value.
    """
    project = Project(name="version-explicit", user_id="user-version-explicit")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="version explicit test",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    assert task.version == 1

    # Bulk Core UPDATE with explicit version bump — mirrors what F4's
    # archive paths emit.
    await db_session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(archived=True, version=Task.version + 1)
    )
    await db_session.commit()

    # Re-read to pick up DB state (the in-memory ``task`` object's
    # version is unchanged because Core UPDATE doesn't sync the ORM).
    refreshed = await db_session.get(Task, task_id)
    await db_session.refresh(refreshed)
    assert refreshed.version == 2, (
        f"expected version=2 after explicit Core bump, got {refreshed.version}"
    )

    # A second explicit bump.
    await db_session.execute(
        update(Task).where(Task.id == task_id).values(version=Task.version + 1)
    )
    await db_session.commit()
    refreshed_2 = await db_session.get(Task, task_id)
    await db_session.refresh(refreshed_2)
    assert refreshed_2.version == 3


@pytest.mark.asyncio
async def test_concurrent_orm_writes_do_not_raise_stale_data_error(db_session):
    """Concurrent stale ORM writes apply unconditionally (last-writer-wins).

    Round 2 audit (C5) dropped the ``version_id_col`` mapper arg, so the
    ORM no longer adds ``WHERE version = :v`` to UPDATE statements. A
    second session that committed first does NOT invalidate the first
    session's commit — both writes succeed; the later one overwrites the
    earlier one.

    This is the deliberate trade-off documented above: until a
    ``StaleDataError``-handler sweep lands, the previous behavior was
    leaking unhandled 500s on every concurrent write. ``version_id_col``
    is temporarily removed pending that sweep; the column is retained
    as a forensic counter.
    """
    project = Project(name="version-stale", user_id="user-version-stale")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="version stale test",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # session_a loads first, holds the version=1 snapshot.
    async with Session() as session_a:
        result_a = await session_a.execute(select(Task).where(Task.id == task_id))
        task_a = result_a.scalar_one()
        assert task_a.version == 1

        # session_b loads, mutates, and commits while session_a still holds
        # its version=1 snapshot.
        async with Session() as session_b:
            result_b = await session_b.execute(select(Task).where(Task.id == task_id))
            task_b = result_b.scalar_one()
            assert task_b.version == 1
            task_b.title = "session_b mutation"
            session_b.add(task_b)
            await session_b.commit()
            # Without ``version_id_col`` enforcement the column is no
            # longer auto-bumped — it stays at 1 unless explicitly set.
            assert task_b.version == 1

        # session_a's commit no longer raises StaleDataError — the UPDATE
        # has no WHERE-version clause, so it lands unconditionally and
        # overwrites session_b's title. Last-writer-wins.
        task_a.title = "session_a stale mutation"
        session_a.add(task_a)
        await session_a.commit()  # no exception
        assert task_a.version == 1

    # Final DB state: session_a's title won, version still 1.
    async with Session() as verify_session:
        result = await verify_session.execute(select(Task).where(Task.id == task_id))
        final = result.scalar_one()
        assert final.title == "session_a stale mutation"
        assert final.version == 1
