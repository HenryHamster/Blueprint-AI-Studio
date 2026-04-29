"""Optimistic-concurrency tests for ``Task.version`` (audit 2026-04-29 M-C4).

These tests verify that the ``version_id_col`` mapper arg added on the
``Task`` class actually wires through to the dialect's UPDATE emission —
i.e. UPDATEs include ``WHERE version = :v`` and increment the version,
and a concurrent stale write raises ``sqlalchemy.orm.exc.StaleDataError``.

The tests use a fresh sessionmaker (``expire_on_commit=False`` to mirror
``database.py:47``) so the second ``await session.commit()`` triggers
SQLAlchemy's row-count-zero check rather than fetching fresh state from
the DB. This matches the runtime behavior the audit is hardening
against — the post-commit chain in ``task_completion.py`` /
``rollup_management.py`` does not refresh between mutations.
"""
import pytest
import sqlalchemy.orm.exc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus


@pytest.mark.asyncio
async def test_version_increments_on_update(db_session):
    """Each ORM-driven UPDATE bumps ``Task.version`` by one.

    The ``version_id_col`` mapper arg makes SQLAlchemy increment the
    column itself on every UPDATE — application code should never need
    to bump it manually. Default is 1 (matches the migration's
    ``server_default``); after one mutation it should be 2, after two
    mutations 3, etc.
    """
    project = Project(name="version-inc", user_id="user-version-inc")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="version inc test",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()

    # Default after INSERT.
    assert task.version == 1, f"expected version=1 after insert, got {task.version}"

    # First mutation → version 2.
    task.title = "first mutation"
    db_session.add(task)
    await db_session.commit()
    assert task.version == 2, f"expected version=2 after 1 update, got {task.version}"

    # Second mutation → version 3.
    task.title = "second mutation"
    db_session.add(task)
    await db_session.commit()
    assert task.version == 3, f"expected version=3 after 2 updates, got {task.version}"


@pytest.mark.asyncio
async def test_concurrent_stale_write_raises_stale_data_error(db_session):
    """A second session that committed first invalidates the first session's
    in-memory copy; the first session's commit then raises ``StaleDataError``.

    Sequence:
      1. session_a loads the task (sees version=1).
      2. session_b loads the same task in a fresh session (sees version=1).
      3. session_b mutates and commits → DB row now version=2.
      4. session_a (still holding the version=1 snapshot) mutates and commits.
         SQLAlchemy emits ``UPDATE task SET ... WHERE id = :id AND version = 1``.
         The WHERE doesn't match (DB has version=2) → 0 rows affected →
         ``StaleDataError`` raised.
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
            assert task_b.version == 2  # bumped by the version_id_col machinery

        # Now session_a tries to commit a stale write — the DB row is at
        # version=2 but session_a still thinks it's at version=1.
        task_a.title = "session_a stale mutation"
        session_a.add(task_a)
        with pytest.raises(sqlalchemy.orm.exc.StaleDataError):
            await session_a.commit()
