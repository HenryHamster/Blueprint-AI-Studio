"""Unit tests for filter_migration_serialization in orchestrator_filters."""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.models import Project, Task, TaskStatus
from app.orchestrator import ACTIVE_TASKS_LIGHT_COLUMNS
from app.orchestrator_filters import filter_migration_serialization
from testing.factories import make_project, make_task


def _task(project_id, status, ttype="migrate", files=None):
    return make_task(
        project_id=project_id,
        status=status,
        context={"related_files": files or []},
        payloads=[{"type": ttype}],
    )


class TestFilterMigrationSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_migrate_tasks_serialized(self):
        pid = uuid.uuid4()
        ip = _task(pid, TaskStatus.IN_PROGRESS, ttype="migrate")
        c = _task(pid, TaskStatus.QUEUED, ttype="migrate")
        session = MagicMock()
        broadcast = AsyncMock()

        result = await filter_migration_serialization(
            [c.id], [c], [ip], session, broadcast
        )
        assert result == []
        assert "serializing migration" in (c.latest_status_message or "")
        broadcast.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_task_with_alembic_file_triggers_serialization(self):
        pid = uuid.uuid4()
        ip = _task(
            pid, TaskStatus.IN_PROGRESS, ttype="create",
            files=["backend/alembic/versions/0042_x.py", "backend/app/models.py"],
        )
        c = _task(pid, TaskStatus.QUEUED, ttype="migrate")
        session = MagicMock()
        broadcast = AsyncMock()

        result = await filter_migration_serialization(
            [c.id], [c], [ip], session, broadcast
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_non_migration_task_unaffected(self):
        pid = uuid.uuid4()
        ip = _task(pid, TaskStatus.IN_PROGRESS, ttype="migrate")
        c = _task(pid, TaskStatus.QUEUED, ttype="fix", files=["backend/app/foo.py"])
        session = MagicMock()
        broadcast = AsyncMock()

        result = await filter_migration_serialization(
            [c.id], [c], [ip], session, broadcast
        )
        assert result == [c.id]
        broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_projects_not_serialized(self):
        ip = _task(uuid.uuid4(), TaskStatus.IN_PROGRESS, ttype="migrate")
        c = _task(uuid.uuid4(), TaskStatus.QUEUED, ttype="migrate")
        session = MagicMock()
        broadcast = AsyncMock()

        result = await filter_migration_serialization(
            [c.id], [c], [ip], session, broadcast
        )
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_no_in_progress_passes_through(self):
        pid = uuid.uuid4()
        c = _task(pid, TaskStatus.QUEUED, ttype="migrate")
        session = MagicMock()
        broadcast = AsyncMock()

        result = await filter_migration_serialization(
            [c.id], [c], [], session, broadcast
        )
        assert result == [c.id]

    @pytest.mark.asyncio
    async def test_active_tasks_loaded_with_orchestrator_load_only_no_lazy_load(self, db_session):
        """Regression: filter must not trigger sync lazy-load on Task.payloads.

        The orchestrator dispatch loop fetches active tasks with the
        ACTIVE_TASKS_LIGHT_COLUMNS load_only set. _is_migration_task reads
        task.first_payload (i.e. task.payloads), so payloads MUST be in that
        set or the access triggers a sync IO load from async context
        (MissingGreenlet) and crashes the cycle. Importing the constant means
        if someone drops Task.payloads from it, this test re-breaks.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        ip = make_task(
            project_id=project.id, status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "migrate"}],
        )
        cand = make_task(
            project_id=project.id, status=TaskStatus.QUEUED,
            payloads=[{"type": "migrate"}],
        )
        db_session.add_all([ip, cand])
        await db_session.commit()
        db_session.expunge_all()

        active = (await db_session.execute(
            select(Task)
            .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
            .options(load_only(*ACTIVE_TASKS_LIGHT_COLUMNS))
        )).scalars().all()
        candidates = (await db_session.execute(
            select(Task).where(Task.status == TaskStatus.QUEUED)
        )).scalars().all()

        result = await filter_migration_serialization(
            [c.id for c in candidates], candidates, active, db_session, AsyncMock()
        )
        assert result == []  # candidate deferred behind in-progress migration
