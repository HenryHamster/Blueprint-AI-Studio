"""Regression tests for /api/deploy project creation."""

import uuid
import pytest
from unittest.mock import AsyncMock, patch

from app.models import Project, ProjectMember

TEST_USER_ID = "test-user-123"


class TestDeployBranchColumnInit:
    @pytest.mark.asyncio
    async def test_deploy_calls_backfill_branch_columns(self, client, db_session):
        """POST /api/deploy must call backfill_branch_columns on the new project.

        Regression: projects created via the deploy endpoint used to skip the
        branch-column initialization step, causing integration_branch /
        promotion_target to remain NULL. Any base-branch resolution then
        silently fell back to 'main', breaking repos whose default branch
        differs (e.g. 'master', 'trunk', 'develop').
        """
        backfill_result = {
            "filled": 2,
            "skipped": 0,
            "errored": 0,
            "self_repo_skipped": 0,
        }

        with patch(
            "app.routers.projects.backfill_branch_columns",
            new_callable=AsyncMock,
            return_value=backfill_result,
        ) as mock_backfill:
            resp = await client.post(
                "/api/deploy",
                json={
                    "name": "test-project",
                    "github_owner": "acme",
                    "github_repo": "myrepo",
                    "payloads": [
                        {"type": "create", "prompt": "hello", "title": "Init task"}
                    ],
                },
            )

        assert resp.status_code == 200
        created_project_id = resp.json()["project_id"]

        mock_backfill.assert_called_once()
        called_project_id = mock_backfill.call_args.kwargs["project_id"]
        assert str(called_project_id) == str(created_project_id)


class TestCancelProjectRoleGuard:
    """POST /api/projects/{id}/cancel must require owner role."""

    async def _make_project(self, session, owner_id=TEST_USER_ID):
        project = Project(name="cancel-test", user_id=owner_id)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project

    @pytest.mark.asyncio
    async def test_viewer_gets_403(self, client, db_session):
        """A project viewer must be denied with 403, not allowed to cancel."""
        project = await self._make_project(db_session)
        db_session.add(
            ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="viewer")
        )
        await db_session.commit()

        resp = await client.post(f"/api/projects/{project.id}/cancel")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_member_gets_404(self, client, db_session):
        """A user with no membership must get 404 (existence hiding), not 403."""
        other_user = str(uuid.uuid4())
        project = await self._make_project(db_session, owner_id=other_user)
        # TEST_USER_ID is not a member at all

        resp = await client.post(f"/api/projects/{project.id}/cancel")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_owner_can_cancel(self, client, db_session):
        """A project owner must be allowed to cancel."""
        project = await self._make_project(db_session)
        db_session.add(
            ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
        )
        await db_session.commit()

        with (
            patch(
                "app.routers.projects.broadcast_to_project",
                new_callable=AsyncMock,
            ),
            patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock),
        ):
            resp = await client.post(f"/api/projects/{project.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
