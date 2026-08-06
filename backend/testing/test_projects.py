"""Regression tests for /api/deploy project creation and project API responses."""

import uuid
import pytest
from unittest.mock import AsyncMock, patch

from app.models import Project, ProjectMember

TEST_USER_ID = "test-user-123"


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project", build_config=None):
    """Create a project with owner membership, optionally with build_config."""
    project = Project(name=name, user_id=owner_id, build_config=build_config or {})
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    session.add(member)
    await session.commit()
    return project


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
                        {
                            "type": "create",
                            "prompt": "hello",
                            "title": "Init task",
                            "acceptance_evidence": {
                                "kind": "observable_behavior",
                                "description": "project bootstrap task ships a real change",
                            },
                        }
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


class TestOnboardingProofField:
    """GET /api/admin/projects and /api/admin/projects/{id} must include onboarding_proof."""

    @pytest.mark.asyncio
    async def test_get_project_includes_onboarding_proof(self, client, db_session):
        """Detail endpoint returns onboarding_proof block with repo_added_at set."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "onboarding_proof" in data
        proof = data["onboarding_proof"]
        assert proof is not None
        assert proof["repo_added_at"] is not None

    @pytest.mark.asyncio
    async def test_get_projects_list_includes_onboarding_proof(self, client, db_session):
        """List endpoint returns onboarding_proof block on every item."""
        await _make_project(db_session, name="List Project")

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        for item in items:
            assert "onboarding_proof" in item
            assert item["onboarding_proof"] is not None

    @pytest.mark.asyncio
    async def test_onboarding_proof_milestones_null_when_unset(self, client, db_session):
        """Milestones not yet reached are null; repo_added_at is always set."""
        project = await _make_project(db_session, build_config={})

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        proof = resp.json()["onboarding_proof"]
        assert proof["repo_added_at"] is not None
        assert proof["bootstrap_completed_at"] is None
        assert proof["first_task_spawned_at"] is None
        assert proof["first_pr_opened_at"] is None
        assert proof["first_merge_gate_verdict_at"] is None
        assert proof["first_pr_merged_at"] is None

    @pytest.mark.asyncio
    async def test_onboarding_proof_milestones_populated(self, client, db_session):
        """Stamped build_config milestones surface in the onboarding_proof block."""
        bc = {
            "bootstrap_completed_at": "2026-01-01T00:00:00",
            "first_pr_merged_at": "2026-02-15T12:00:00",
        }
        project = await _make_project(db_session, build_config=bc)

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        proof = resp.json()["onboarding_proof"]
        assert proof["bootstrap_completed_at"] == "2026-01-01T00:00:00"
        assert proof["first_pr_merged_at"] == "2026-02-15T12:00:00"
        assert proof["first_task_spawned_at"] is None
