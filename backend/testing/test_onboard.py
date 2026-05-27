"""Tests for onboarding endpoint — build config detection and auto_remediation defaults."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient

from app.main import app
from app.models import Project
from app.auth import get_current_user, require_admin
from app.routers.admin_onboard import _detect_build_config

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

    @pytest.mark.asyncio
    async def test_onboard_explicit_promotion_target_overrides_default(
        self, client: AsyncClient, db_session
    ):
        """When promotion_target is passed, it is stored separately from integration_branch."""
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
                return_value="automain",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "lts-repo",
                    "promotion_target": "main",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.integration_branch == "automain"
        assert project.promotion_target == "main"
        ar = (project.design_doc or {}).get("_auto_remediation")
        assert "automain" in ar["branch_whitelist"]
        assert "main" in ar["branch_whitelist"]


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


class TestOnboardBranchConfig:
    @pytest.mark.asyncio
    async def test_onboard_build_config_override_branch_keys_survive_validation(
        self, client: AsyncClient, db_session
    ):
        """build_config_override with staging/stale/protected/self-improvement keys survives schema validation."""
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
                    "github_repo": "branch-config-repo",
                    "build_config_override": {
                        "staging_branch_prefixes": ["feat/", "research/"],
                        "stale_branch_prefixes": ["feat/"],
                        "protected_branches": ["main", "release"],
                        "enable_self_improvement": True,
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        bc = project.build_config or {}
        assert bc.get("staging_branch_prefixes") == ["feat/", "research/"]
        assert bc.get("stale_branch_prefixes") == ["feat/"]
        assert bc.get("protected_branches") == ["main", "release"]
        assert bc.get("enable_self_improvement") is True

    @pytest.mark.asyncio
    async def test_onboard_build_config_override_empty_stale_prefixes_survives(
        self, client: AsyncClient, db_session
    ):
        """An explicit empty stale_branch_prefixes=[] in the override survives — it means 'disable cleanup'."""
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
                json={
                    "github_owner": "acme",
                    "github_repo": "no-stale-repo",
                    "build_config_override": {
                        "stale_branch_prefixes": [],
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        bc = project.build_config or {}
        # Empty list is a meaningful "disable" signal — must be preserved, not dropped.
        assert "stale_branch_prefixes" in bc
        assert bc["stale_branch_prefixes"] == []


class TestOnboardDefaultBranchErrors:
    @pytest.mark.asyncio
    async def test_onboard_github_non200_raises_502(
        self, client: AsyncClient, db_session
    ):
        """Non-200 GitHub response during default-branch detection surfaces as 502."""
        from fastapi import HTTPException

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
                side_effect=HTTPException(
                    status_code=502,
                    detail="GitHub API error detecting default branch (HTTP 403).",
                ),
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "err-repo1"},
            )

        assert resp.status_code == 502
        # No project should be created
        from sqlalchemy import select
        from app.models import Project as ProjectModel

        result = await db_session.execute(
            select(ProjectModel).where(
                ProjectModel.github_owner == "acme",
                ProjectModel.github_repo == "err-repo1",
            )
        )
        assert result.scalars().first() is None

    @pytest.mark.asyncio
    async def test_onboard_github_missing_default_branch_key_raises_502(
        self, client: AsyncClient, db_session
    ):
        """Missing default_branch key in GitHub 200 response surfaces as 502."""
        from fastapi import HTTPException

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
                side_effect=HTTPException(
                    status_code=502,
                    detail="GitHub API response missing default_branch field.",
                ),
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "err-repo2"},
            )

        assert resp.status_code == 502
        from sqlalchemy import select
        from app.models import Project as ProjectModel

        result = await db_session.execute(
            select(ProjectModel).where(
                ProjectModel.github_owner == "acme",
                ProjectModel.github_repo == "err-repo2",
            )
        )
        assert result.scalars().first() is None

    @pytest.mark.asyncio
    async def test_onboard_no_token_raises_422(self, client: AsyncClient, db_session):
        """When no installation token is available, onboarding raises 422 rather than silently defaulting."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "err-repo3"},
            )

        assert resp.status_code == 422
        from sqlalchemy import select
        from app.models import Project as ProjectModel

        result = await db_session.execute(
            select(ProjectModel).where(
                ProjectModel.github_owner == "acme",
                ProjectModel.github_repo == "err-repo3",
            )
        )
        assert result.scalars().first() is None


class TestOnboardTaskBranchNamespace:
    """Onboarding seeds task_branch_namespace in build_config."""

    @pytest.mark.asyncio
    async def test_typical_repo_gets_derived_namespace(
        self, client: AsyncClient, db_session
    ):
        """A typical repo name is lowercased and non-alphanumeric chars replaced with '-'."""
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
                json={"github_owner": "acme", "github_repo": "my-cool-repo"},
            )
        assert resp.status_code == 200
        project = await db_session.get(Project, resp.json()["project_id"])
        assert (project.build_config or {}).get(
            "task_branch_namespace"
        ) == "my-cool-repo"

    @pytest.mark.asyncio
    async def test_uppercase_and_special_chars_are_normalized(
        self, client: AsyncClient, db_session
    ):
        """Uppercase letters are lowercased; non-alphanumeric chars become '-'."""
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
                json={"github_owner": "acme", "github_repo": "MyAwesome_Repo.v2"},
            )
        assert resp.status_code == 200
        project = await db_session.get(Project, resp.json()["project_id"])
        ns = (project.build_config or {}).get("task_branch_namespace")
        assert ns == "myawesome-repo-v2"

    @pytest.mark.asyncio
    async def test_blueprintaistudio_repo_slugifies_normally(
        self, client: AsyncClient, db_session
    ):
        """A repo named 'BlueprintAIStudio' that is not the self-repo gets a slugified namespace."""
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
                json={
                    "github_owner": "HenryHamster",
                    "github_repo": "BlueprintAIStudio",
                },
            )
        assert resp.status_code == 200
        project = await db_session.get(Project, resp.json()["project_id"])
        assert (project.build_config or {}).get(
            "task_branch_namespace"
        ) == "blueprintaistudio"

    @pytest.mark.asyncio
    async def test_build_config_override_wins(self, client: AsyncClient, db_session):
        """Explicit task_branch_namespace in build_config_override overrides derived value."""
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
                json={
                    "github_owner": "acme",
                    "github_repo": "some-repo",
                    "build_config_override": {"task_branch_namespace": "custom-ns"},
                },
            )
        assert resp.status_code == 200
        project = await db_session.get(Project, resp.json()["project_id"])
        assert (project.build_config or {}).get("task_branch_namespace") == "custom-ns"


class TestOnboardBranchBackfillIntegration:
    """Regression: branches are populated post-onboard; backfill skips populated rows."""

    @pytest.mark.asyncio
    async def test_onboard_leaves_no_null_branches(
        self, client: AsyncClient, db_session
    ):
        """Neither integration_branch nor promotion_target may be NULL after onboarding."""
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
                return_value="develop",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "backfill-reg1"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.integration_branch is not None, (
            "integration_branch must not be NULL after onboard"
        )
        assert project.promotion_target is not None, (
            "promotion_target must not be NULL after onboard"
        )

    @pytest.mark.asyncio
    async def test_backfill_skips_rows_populated_by_onboard(
        self, client: AsyncClient, db_session
    ):
        """backfill_branch_columns must not overwrite columns already set by onboarding."""
        from app.shared_state import backfill_branch_columns

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
                return_value="develop",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "backfill-reg2"},
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project.integration_branch == "develop"
        assert project.promotion_target == "develop"

        # Run backfill — must not touch already-populated columns.
        summary = await backfill_branch_columns(db_session)
        assert summary["filled"] == 0, (
            f"backfill must not write to already-populated columns; got filled={summary['filled']}"
        )
        assert summary["errored"] == 0, (
            f"backfill must not error for already-populated project; got errored={summary['errored']}"
        )

        # Values must remain unchanged after the backfill pass.
        await db_session.refresh(project)
        assert project.integration_branch == "develop"
        assert project.promotion_target == "develop"


class TestBuildConfigDetection:
    @pytest.mark.asyncio
    async def test_detect_build_config_seeds_lint_cmd_for_root_python(self):
        """Root-level Python project detection includes lint_cmd='ruff check'."""
        root_files = [
            {"name": "pyproject.toml", "type": "file"},
        ]
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = root_files

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["path"] == "./"
        assert dir_config["type"] == "python"
        assert dir_config["lint_cmd"] == "ruff check"
        assert dir_config["test_cmd"] == "pytest"

    @pytest.mark.asyncio
    async def test_detect_build_config_seeds_lint_cmd_for_subdir_python(self):
        """Subdirectory Python project detection includes lint_cmd='ruff check'."""
        root_files = [
            {"name": "backend", "type": "dir"},
        ]
        subdir_files = [
            {"name": "setup.py", "type": "file"},
        ]
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = root_files

        subdir_resp = MagicMock()
        subdir_resp.status_code = 200
        subdir_resp.json.return_value = subdir_files

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, subdir_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["path"] == "backend/"
        assert dir_config["type"] == "python"
        assert dir_config["lint_cmd"] == "ruff check"
        assert dir_config["test_cmd"] == "pytest"
