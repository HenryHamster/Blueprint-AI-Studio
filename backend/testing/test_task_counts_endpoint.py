"""Tests for GET /api/admin/tasks/counts — the cheap aggregate counts endpoint."""
import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember, TaskStatus

TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Counts Test Project", owner_id=TEST_USER_ID):
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    await session.commit()
    return project


class TestGetTaskCounts:
    """GET /api/admin/tasks/counts returns aggregate counts without loading task rows."""

    @pytest.mark.asyncio
    async def test_counts_split_executing_starting_and_evaluating(
        self, client: AsyncClient, db_session
    ):
        """Reservations stay separate from tasks that are actually executing."""
        from testing.factories import make_task

        project = await _make_project(db_session)
        db_session.add_all([
            make_task(project_id=project.id, status=TaskStatus.IN_PROGRESS),
            make_task(project_id=project.id, status=TaskStatus.PROVISIONING),
            make_task(project_id=project.id, status=TaskStatus.EVALUATING),
            make_task(project_id=project.id, status=TaskStatus.QUEUED),
            make_task(project_id=project.id, status=TaskStatus.COMPLETED),
        ])
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/counts?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["active_count"] >= 1
        assert data["starting_count"] >= 1
        assert data["evaluating_count"] >= 1
        assert data["queued_count"] >= 1
        assert data["scoped_active_count"] == 1
        assert data["scoped_starting_count"] == 1
        assert data["scoped_evaluating_count"] == 1
        assert data["scoped_queued_count"] == 1
        assert "tasks" not in data

    @pytest.mark.asyncio
    async def test_counts_scoped_by_project(self, client: AsyncClient, db_session):
        """With project_id, scoped counts are also returned."""
        from testing.factories import make_task

        project_a = await _make_project(db_session, name="Counts Project A")
        project_b = await _make_project(db_session, name="Counts Project B")
        db_session.add_all([
            make_task(project_id=project_a.id, status=TaskStatus.IN_PROGRESS),
            make_task(project_id=project_b.id, status=TaskStatus.EVALUATING),
        ])
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/counts?project_id={project_a.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["scoped_active_count"] == 1
        assert data["scoped_starting_count"] == 0
        assert data["scoped_evaluating_count"] == 0
        assert data["scoped_queued_count"] == 0

    @pytest.mark.asyncio
    async def test_counts_excludes_foreign_project_tasks(
        self, client: AsyncClient, db_session
    ):
        """Tasks in projects the caller is not a member of are excluded."""
        from testing.factories import make_task

        foreign = Project(name="Foreign Counts", user_id="foreign-owner-counts")
        db_session.add(foreign)
        await db_session.commit()
        await db_session.refresh(foreign)
        db_session.add(
            ProjectMember(
                project_id=foreign.id, user_id="foreign-owner-counts", role="owner"
            )
        )
        db_session.add(make_task(project_id=foreign.id, status=TaskStatus.IN_PROGRESS))
        await db_session.commit()

        # Caller's own project with no tasks
        await _make_project(db_session, name="Own Counts Project")

        resp = await client.get("/api/admin/tasks/counts")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Foreign project tasks must not appear in the caller's counts
        assert data["active_count"] == 0
