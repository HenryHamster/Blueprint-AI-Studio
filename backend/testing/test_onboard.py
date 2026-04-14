"""Tests for onboarding endpoint — build config detection and auto_remediation defaults."""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient

from app.main import app
from app.models import Project
from app.auth import get_current_user, require_admin

TEST_USER_ID = "test-user-123"


@pytest.fixture(autouse=True)
def override_auth():
    async def _user():
        return {"user_id": TEST_USER_ID, "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_admin] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_admin, None)


class TestOnboardAutoRemediation:
    @pytest.mark.asyncio
    async def test_onboard_sets_auto_remediation_defaults(self, client: AsyncClient, db_session):
        """Onboarding initializes _auto_remediation in design_doc with sensible defaults."""
        with (
            patch("app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock, return_value={"language": "python"}),
            patch("app.github.get_repo_installation_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.github.get_repo_default_branch", new_callable=AsyncMock, return_value="main"),
        ):
            resp = await client.post("/api/admin/projects/onboard", json={
                "github_owner": "acme",
                "github_repo": "myrepo",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "project_id" in data
        assert data["default_branch"] == "main"

        project = await db_session.get(Project, data["project_id"])
        assert project is not None
        ar = (project.design_doc or {}).get("_auto_remediation")
        assert ar is not None
        assert ar["enabled"] is False
        assert "main" in ar["branch_whitelist"]
        assert ar["max_per_hour"] == 2
        assert ar["max_concurrent"] == 2
        assert ar["require_review"] is True

    @pytest.mark.asyncio
    async def test_onboard_custom_default_branch_added_to_whitelist(self, client: AsyncClient, db_session):
        """When default branch is not 'main' or 'master', it is prepended to branch_whitelist."""
        with (
            patch("app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock, return_value={}),
            patch("app.github.get_repo_installation_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.github.get_repo_default_branch", new_callable=AsyncMock, return_value="develop"),
        ):
            resp = await client.post("/api/admin/projects/onboard", json={
                "github_owner": "acme",
                "github_repo": "myrepo2",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["default_branch"] == "develop"

        project = await db_session.get(Project, data["project_id"])
        ar = (project.design_doc or {}).get("_auto_remediation")
        assert ar["branch_whitelist"][0] == "develop"
        assert "main" in ar["branch_whitelist"]
        assert "master" in ar["branch_whitelist"]
