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
    async def test_onboard_sets_auto_remediation_defaults(
        self, client: AsyncClient, db_session
    ):
        """Onboarding initializes _auto_remediation in design_doc with sensible defaults."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "python"},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "myrepo",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "project_id" in data
        assert data["default_branch"] == "main"

        project = await db_session.get(Project, data["project_id"])
        assert project is not None
        ar = (project.design_doc or {}).get("_auto_remediation")
        assert ar is not None
        assert ar["enabled"] is False
        assert ar["branch_whitelist"] == ["main"]
        assert ar["max_per_hour"] == 2
        assert ar["max_concurrent"] == 2
        assert ar["require_review"] is True

    @pytest.mark.asyncio
    async def test_onboard_custom_default_branch_only_in_whitelist(
        self, client: AsyncClient, db_session
    ):
        """When default branch is not 'main' or 'master', whitelist contains only that branch."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="trunk",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "myrepo2",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["default_branch"] == "trunk"

        project = await db_session.get(Project, data["project_id"])
        ar = (project.design_doc or {}).get("_auto_remediation")
        assert ar["branch_whitelist"] == ["trunk"]
        assert "main" not in ar["branch_whitelist"]
        assert "master" not in ar["branch_whitelist"]

    @pytest.mark.asyncio
    async def test_onboard_sets_integration_branch_from_detected_default(
        self, client: AsyncClient, db_session
    ):
        """integration_branch column is populated from detected default branch on onboard."""
        from app.shared_state import get_project_integration_branch

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="master",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "myrepo3"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.integration_branch == "master"
        assert get_project_integration_branch(project) == "master"

    @pytest.mark.asyncio
    async def test_onboard_sets_promotion_target_from_detected_default(
        self, client: AsyncClient, db_session
    ):
        """promotion_target column is populated from detected default branch on onboard."""
        from app.shared_state import get_project_promotion_target

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="master",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "myrepo4"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.promotion_target == "master"
        assert get_project_promotion_target(project) == "master"

    @pytest.mark.asyncio
    async def test_onboard_main_default_branch_sets_integration_branch(
        self, client: AsyncClient, db_session
    ):
        """integration_branch is set to 'main' (not left NULL) when default is 'main'."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "myrepo5"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.integration_branch == "main"
        assert project.promotion_target == "main"

    @pytest.mark.asyncio
    async def test_onboard_seeds_ruff_lint_cmd_for_python_directories(
        self, client: AsyncClient, db_session
    ):
        """Onboarding detects Python directories and seeds lint_cmd='ruff check'."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [
                        {
                            "path": "./",
                            "type": "python",
                            "lint_cmd": "ruff check",
                            "test_cmd": "pytest",
                        }
                    ],
                },
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "mypyrepo"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        build_config = project.build_config or {}
        assert len(build_config.get("directories", [])) > 0
        assert build_config["directories"][0]["lint_cmd"] == "ruff check"


class TestOnboardIssueImplementation:
    @pytest.mark.asyncio
    async def test_onboard_does_not_seed_base_branch_in_issue_implementation(
        self, client: AsyncClient, db_session
    ):
        """Onboarding must not store base_branch in _issue_implementation — it's always live-derived."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="trunk",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "myrepo6",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        ii = (project.design_doc or {}).get("_issue_implementation")
        assert ii is not None
        assert "base_branch" not in ii
