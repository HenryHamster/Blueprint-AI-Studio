"""Tests for reserve_migration_revisions in orchestrator_filters.

The old project-wide IN_PROGRESS serialization is replaced by a reserve-then-
release flow: a dispatchable migration-producing candidate is handed a distinct
provisional revision id up-front (locking its number) and is NOT held behind a
sibling's full execution/test lifecycle. These tests prove the slot releases
immediately after reservation and that distinct, monotonic ids are allocated —
while non-migration tasks are untouched and an unknown remote head defers only
the unsafe migration task.

The on-disk migration head is pinned via ``_current_merged_head`` so these tests
are independent of the live alembic chain (which grows as migrations land).
"""

import pytest
from sqlmodel import select

from app import migration_reservations
from app.migration_reservations import get_migration_reservation_for_task
from app.models import (
    MigrationReservation,
    Project,
    Task,
    TaskStatus,
)
from app.orchestrator_filters import reserve_migration_revisions


@pytest.fixture(autouse=True)
def _pin_head(monkeypatch):
    """Pin the merged chain head to 0151 regardless of the live alembic dir."""
    monkeypatch.setattr(
        migration_reservations,
        "_current_merged_head",
        lambda versions_dir=None: "0151",
    )


async def _project(session, name="Reserve Migration"):
    project = Project(name=name, user_id="test-user")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _task(session, project_id, *, ttype="migrate", files=None):
    task = Task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": ttype}],
        context={"related_files": files or []},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


class TestReserveMigrationRevisions:
    @pytest.mark.asyncio
    async def test_concurrent_migration_tasks_both_release_with_distinct_revisions(
        self, db_session
    ):
        """Two concurrent migrate tasks each get a distinct id and BOTH dispatch.

        This is the core of de-convoying: neither candidate is deferred behind
        the other (the slot releases the moment numbering is locked).
        """
        project = await _project(db_session)
        a = await _task(db_session, project.id, ttype="migrate")
        b = await _task(db_session, project.id, ttype="migrate")

        result = await reserve_migration_revisions([a.id, b.id], [a, b], db_session)

        # Slot releases for BOTH — neither is held behind the other.
        assert result == [a.id, b.id]

        res_a = await get_migration_reservation_for_task(db_session, task_id=a.id)
        res_b = await get_migration_reservation_for_task(db_session, task_id=b.id)
        assert res_a is not None and res_b is not None
        # Distinct, monotonic ids anchored on the same merged head.
        assert {res_a.provisional_revision, res_b.provisional_revision} == {
            "0152",
            "0153",
        }
        assert res_a.provisional_down_revision == "0151"
        assert res_b.provisional_down_revision == "0151"

    @pytest.mark.asyncio
    async def test_create_task_touching_models_py_is_reserved(self, db_session):
        """A create task that touches models.py is migration-producing → reserved."""
        project = await _project(db_session)
        c = await _task(
            db_session,
            project.id,
            ttype="create",
            files=["backend/app/models.py"],
        )

        result = await reserve_migration_revisions([c.id], [c], db_session)

        assert result == [c.id]
        res = await get_migration_reservation_for_task(db_session, task_id=c.id)
        assert res is not None
        assert res.provisional_revision == "0152"
        assert res.provisional_down_revision == "0151"

    @pytest.mark.asyncio
    async def test_create_task_touching_alembic_file_is_reserved(self, db_session):
        project = await _project(db_session)
        c = await _task(
            db_session,
            project.id,
            ttype="create",
            files=["backend/alembic/versions/0152_x.py"],
        )

        result = await reserve_migration_revisions([c.id], [c], db_session)

        assert result == [c.id]
        assert (
            await get_migration_reservation_for_task(db_session, task_id=c.id)
            is not None
        )

    @pytest.mark.asyncio
    async def test_non_migration_task_gets_no_reservation(self, db_session):
        project = await _project(db_session)
        c = await _task(
            db_session, project.id, ttype="fix", files=["backend/app/foo.py"]
        )

        result = await reserve_migration_revisions([c.id], [c], db_session)

        assert result == [c.id]
        assert (
            await get_migration_reservation_for_task(db_session, task_id=c.id) is None
        )

    @pytest.mark.asyncio
    async def test_reservation_is_idempotent_across_cycles(self, db_session):
        """Re-running the filter every dispatch cycle never burns a fresh id."""
        project = await _project(db_session)
        a = await _task(db_session, project.id, ttype="migrate")

        await reserve_migration_revisions([a.id], [a], db_session)
        first = await get_migration_reservation_for_task(db_session, task_id=a.id)

        await reserve_migration_revisions([a.id], [a], db_session)
        rows = (
            (
                await db_session.execute(
                    select(MigrationReservation).where(
                        MigrationReservation.task_id == a.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        second = await get_migration_reservation_for_task(db_session, task_id=a.id)
        assert second is not None and first is not None
        assert second.id == first.id
        assert second.provisional_revision == first.provisional_revision

    @pytest.mark.asyncio
    async def test_different_projects_each_reserve_off_their_own_head(self, db_session):
        p1 = await _project(db_session, name="P1")
        p2 = await _project(db_session, name="P2")
        a = await _task(db_session, p1.id, ttype="migrate")
        b = await _task(db_session, p2.id, ttype="migrate")

        result = await reserve_migration_revisions([a.id, b.id], [a, b], db_session)

        assert result == [a.id, b.id]
        res_a = await get_migration_reservation_for_task(db_session, task_id=a.id)
        res_b = await get_migration_reservation_for_task(db_session, task_id=b.id)
        # Separate projects each anchor on the head independently — same id is fine.
        assert res_a is not None and res_a.provisional_revision == "0152"
        assert res_b is not None and res_b.provisional_revision == "0152"

    @pytest.mark.asyncio
    async def test_empty_eligible_returns_empty(self, db_session):
        assert await reserve_migration_revisions([], [], db_session) == []

    @pytest.mark.asyncio
    async def test_unknown_remote_head_defers_migration_dispatch(
        self, db_session, monkeypatch
    ):
        """An unknown task-base head must fail closed instead of numbering locally."""
        project = await _project(db_session)
        a = await _task(db_session, project.id, ttype="migrate")

        async def _boom(session, task, **kw):
            raise migration_reservations.MigrationHeadUnavailableError(
                "remote head unavailable"
            )

        monkeypatch.setattr(
            migration_reservations, "allocate_migration_reservation", _boom
        )

        result = await reserve_migration_revisions([a.id], [a], db_session)

        assert result == []
        assert (
            await get_migration_reservation_for_task(db_session, task_id=a.id) is None
        )

    @pytest.mark.asyncio
    async def test_unrelated_allocator_failure_retains_best_effort_dispatch(
        self, db_session, monkeypatch
    ):
        project = await _project(db_session)
        a = await _task(db_session, project.id, ttype="migrate")

        async def _boom(session, task, **kw):
            raise RuntimeError("database refresh failed")

        monkeypatch.setattr(
            migration_reservations, "allocate_migration_reservation", _boom
        )

        assert await reserve_migration_revisions([a.id], [a], db_session) == [a.id]
