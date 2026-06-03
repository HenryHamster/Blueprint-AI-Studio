"""Tests for onboarding endpoint — build config detection and auto_remediation defaults."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient

from app.main import app
from app.models import Project, ProjectMessage, ProjectStatus
from app.auth import get_current_user, require_admin
from app.routers.admin_onboard import (
    GREENFIELD_SOURCE_FILE_THRESHOLD,
    _detect_build_config,
    _classify_repo_emptiness,
    _seed_strategic_intent,
)

TEST_USER_ID = "test-user-123"


def _listing_resp(items, status_code=200):
    """A MagicMock GitHub contents *directory listing* response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = items
    return resp


def _file_content_resp(text, status_code=200):
    """A MagicMock GitHub contents *single-file* response (base64-encoded body).

    Mirrors the real Contents API single-file payload the detector decodes when
    it must inspect a Makefile/composer.json body before emitting a command.
    """
    import base64

    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "type": "file",
        "content": base64.b64encode(text.encode()).decode(),
        "encoding": "base64",
    }
    return resp


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
    async def test_onboard_sets_branch_columns_from_detected_default(
        self, client: AsyncClient, db_session
    ):
        """Both integration_branch and promotion_target columns are populated
        from the detected default branch on onboard (and the matching
        get_project_* helpers resolve to the same value)."""
        from app.shared_state import (
            get_project_integration_branch,
            get_project_promotion_target,
        )

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
        assert project.promotion_target == "master"
        assert get_project_integration_branch(project) == "master"
        assert get_project_promotion_target(project) == "master"

    @pytest.mark.asyncio
    async def test_onboard_detects_build_config_from_default_branch(
        self, client: AsyncClient
    ):
        """Build-config detection is pinned to the resolved default branch."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_detect,
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
                json={"github_owner": "acme", "github_repo": "branch-aware"},
            )

        assert resp.status_code == 200
        mock_detect.assert_awaited_once()
        assert mock_detect.await_args.kwargs.get("ref") == "trunk"

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


    @pytest.mark.asyncio
    async def test_onboard_build_config_override_migration_survives_validation(
        self, client: AsyncClient, db_session
    ):
        """An operator-supplied build_config.migration round-trips through onboard validation."""
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
                    "github_repo": "migration-layout-repo",
                    "build_config_override": {
                        "migration": {
                            "versions_dir": "src/db/versions",
                            "models_dir": "src/db/models",
                            "alembic_cwd": "src",
                            # Empty string is the meaningful "no schema-revision gate" signal.
                            "schema_revision_file": "",
                            "schema_revision_const": "SCHEMA_REV",
                        }
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        bc = project.build_config or {}
        # The whole migration object must survive — not be silently dropped by
        # Pydantic's default extra-ignore behavior.
        assert bc.get("migration") == {
            "versions_dir": "src/db/versions",
            "models_dir": "src/db/models",
            "alembic_cwd": "src",
            "schema_revision_file": "",
            "schema_revision_const": "SCHEMA_REV",
        }

    @pytest.mark.asyncio
    async def test_onboard_build_config_override_requires_ci_checks_false_persists(
        self, client: AsyncClient, db_session
    ):
        """An operator declaring a no-CI repo at onboarding persists requires_ci_checks=False.

        This exercises the validated persistence path (model_dump(exclude_none=True));
        an explicit False must NOT be stripped the way an undeclared field would be.
        """
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
                    "github_repo": "no-ci-repo",
                    "build_config_override": {"requires_ci_checks": False},
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        bc = project.build_config or {}
        assert bc.get("requires_ci_checks") is False


class TestBuildConfigMigrationSchema:
    """Direct schema-level proof that BuildConfig accepts and round-trips `migration`."""

    def test_migration_object_round_trips_through_model_dump(self):
        from app.schemas import BuildConfig

        validated = BuildConfig(
            **{
                "language": "python",
                "migration": {
                    "versions_dir": "src/db/versions",
                    "schema_revision_file": "src/app/main.py",
                    "schema_revision_const": "EXPECTED_SCHEMA_REVISION",
                },
            }
        )
        dumped = validated.model_dump(exclude_none=True)
        assert dumped["migration"] == {
            "versions_dir": "src/db/versions",
            "schema_revision_file": "src/app/main.py",
            "schema_revision_const": "EXPECTED_SCHEMA_REVISION",
        }

    def test_empty_schema_revision_file_is_preserved(self):
        from app.schemas import BuildConfig

        validated = BuildConfig(**{"migration": {"schema_revision_file": ""}})
        dumped = validated.model_dump(exclude_none=True)
        # '' is a meaningful "no schema-revision gate" signal — must NOT be coerced to None/dropped.
        assert dumped["migration"] == {"schema_revision_file": ""}

    def test_unset_migration_falls_back_to_no_key(self):
        from app.schemas import BuildConfig

        validated = BuildConfig(**{"language": "python"})
        dumped = validated.model_dump(exclude_none=True)
        # Unset migration → key absent → agent falls back to Blueprint defaults.
        assert "migration" not in dumped


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
    async def test_empty_slug_repo_does_not_use_blueprint(
        self, client: AsyncClient, db_session
    ):
        """A repo whose name slugifies to empty falls back to 'project', not 'blueprint'."""
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
                json={"github_owner": "acme", "github_repo": "---"},
            )
        assert resp.status_code == 200
        project = await db_session.get(Project, resp.json()["project_id"])
        ns = (project.build_config or {}).get("task_branch_namespace")
        assert ns != "blueprint"
        assert ns == "project"

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


class TestOnboardStrategicIntentSeed:
    """The onboarding goal is seeded into both strategic-intent channels."""

    def _new_project(self):
        return Project(
            name="acme/myrepo",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="myrepo",
        )

    def test_goal_seeded_into_problem_model_intent(self):
        """A goal populates intent.purpose + current_focus with user provenance."""
        from app.problem_model import parse_problem_model

        project = self._new_project()
        goal = "Ship a self-serve billing portal"
        msg = _seed_strategic_intent(project, goal, None)

        pm = parse_problem_model(project.problem_model)
        assert pm.intent.purpose == goal
        assert pm.intent.current_focus == goal
        assert pm.last_updated_by == "user"
        assert pm.intent.source is not None
        assert pm.intent.source.type == "user_message"
        assert pm.intent.source.message_id == str(msg.id)

    def test_goal_returns_user_conversation_message(self):
        """The goal is also returned as a from_type='user' open ProjectMessage
        so it lands in the strategic agent's conversation_thread on cycle 1."""
        project = self._new_project()
        goal = "Make onboarding capture the user's goal"
        msg = _seed_strategic_intent(project, goal, None)

        assert isinstance(msg, ProjectMessage)
        assert msg.from_type == "user"
        assert msg.content == goal
        assert msg.status == "open"
        assert msg.project_id == project.id

    def test_empty_goal_is_a_noop(self):
        """An empty/whitespace goal preserves today's blank-slate behavior."""
        for goal in (None, "", "   "):
            project = self._new_project()
            assert _seed_strategic_intent(project, goal, None) is None
            # problem_model untouched — still the column default (empty dict).
            assert not project.problem_model

    def test_steering_merges_into_user_editable_subset(self):
        """Optional steering merges constraints/context; agent-owned keys ignored."""
        from app.problem_model import parse_problem_model

        project = self._new_project()
        steering = {
            "constraints": ["no breaking API changes"],
            "context": {"project_type": "startup"},
            "gaps": [{"id": "x", "name": "ignored"}],  # agent-owned -> dropped
        }
        _seed_strategic_intent(project, "Grow revenue", steering)

        pm = parse_problem_model(project.problem_model)
        assert pm.constraints == ["no breaking API changes"]
        assert pm.context.project_type == "startup"
        assert pm.gaps == []  # agent-owned steering key was not applied

    def test_steering_ui_notes_shape_merges_into_context(self):
        """The UI sends free-text steering as {'context': {'notes': <text>}} —
        a real ProjectContext field — which must merge cleanly (regression for
        the onboarding 500 when a bare string was sent for context)."""
        from app.problem_model import parse_problem_model

        project = self._new_project()
        steering = {"context": {"notes": "Prioritize reliability over features"}}
        _seed_strategic_intent(project, "Stabilize the platform", steering)

        pm = parse_problem_model(project.problem_model)
        assert pm.context.notes == "Prioritize reliability over features"

    @pytest.mark.asyncio
    async def test_onboard_persists_structured_initial_steering(
        self, client: AsyncClient, db_session
    ):
        """Structured onboarding steering should land in the persisted problem model."""
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
                    "github_repo": "platform",
                    "goal": "Ship the core fix",
                    "initial_steering": {
                        "context": {
                            "project_type": "startup",
                            "users": "Platform team",
                            "team": "4 engineers + 1 PM",
                            "deploy_cadence": "weekly",
                            "notes": "Reliability over speed",
                        },
                        "constraints": ["No workflow edits"],
                    },
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        assert project is not None
        from app.problem_model import parse_problem_model

        pm = parse_problem_model(project.problem_model)
        assert pm.intent.purpose == "Ship the core fix"
        assert pm.context.project_type == "startup"
        assert pm.context.users == "Platform team"
        assert pm.context.team == "4 engineers + 1 PM"
        assert pm.context.deploy_cadence == "weekly"
        assert pm.context.notes == "Reliability over speed"
        assert pm.constraints == ["No workflow edits"]

    def test_malformed_steering_context_raises_400_not_500(self):
        """A wrong-typed steering value (bare string where ProjectContext is
        expected) must surface as a clean HTTP 400, not an unhandled
        pydantic ValidationError that 500s the onboard transaction."""
        from fastapi import HTTPException

        project = self._new_project()
        # This is the exact shape the pre-fix UI sent: a bare string for context.
        steering = {"context": "just some free text"}
        with pytest.raises(HTTPException) as exc_info:
            _seed_strategic_intent(project, "Do the thing", steering)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_seeded_message_is_queryable_for_first_cycle(self, db_session):
        """End-to-end: a seeded goal message persists as an open user message,
        which is exactly what strategic_spawning selects for cycle 1."""
        from sqlalchemy import select

        project = self._new_project()
        db_session.add(project)
        await db_session.flush()

        msg = _seed_strategic_intent(project, "Reach product-market fit", None)
        db_session.add(msg)
        db_session.add(project)
        await db_session.commit()

        rows = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.status == "open",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].content == "Reach product-market fit"


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
        assert dir_config["test_cmd"] == "python3 -m pytest"

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
        assert dir_config["test_cmd"] == "python3 -m pytest"

    @pytest.mark.asyncio
    async def test_detect_build_config_never_emits_bare_pytest(self):
        """Regression C19: _detect_build_config must emit 'python3 -m pytest', never bare 'pytest'.

        Bare 'pytest' fails on agent VMs where pytest is installed as a Python
        package (pip3 install pytest) but is not on the OS PATH as a standalone
        binary.  'python3 -m pytest' works in both environments.
        """
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

        for dir_cfg in config.get("directories", []):
            test_cmd = dir_cfg.get("test_cmd", "")
            assert test_cmd != "pytest", (
                f"test_cmd must not be bare 'pytest'; got {test_cmd!r}. "
                "Use 'python3 -m pytest' so bare-VM invocations succeed."
            )
            if "pytest" in test_cmd:
                assert test_cmd.startswith("python3 -m pytest"), (
                    f"pytest test_cmd must use 'python3 -m pytest'; got {test_cmd!r}"
                )

    @pytest.mark.asyncio
    async def test_detect_build_config_forwards_ref_to_every_request(self):
        """When ``ref`` is provided it is forwarded as params={'ref': ...} to
        every GitHub contents call (root listing + each subdir)."""
        root_files = [
            {"name": "backend", "type": "dir"},
        ]
        subdir_files = [
            {"name": "pyproject.toml", "type": "file"},
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
        ) as mock_req:
            await _detect_build_config(
                "fake-token", "owner", "repo", ref="feature-x"
            )

        assert mock_req.call_count == 2
        for call in mock_req.call_args_list:
            assert call.kwargs.get("params") == {"ref": "feature-x"}

    @pytest.mark.asyncio
    async def test_detect_build_config_omits_params_when_no_ref(self):
        """Regression guard: ref=None must send no params (default-branch
        behavior identical to before the ref parameter was added)."""
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
        ) as mock_req:
            await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 1
        assert mock_req.call_args.kwargs.get("params") is None

    @pytest.mark.asyncio
    async def test_detect_build_config_scans_second_level_python_and_node_dirs(self):
        """Second-level monorepo layouts like apps/web and services/api are detected."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [
            {"name": "apps", "type": "dir"},
            {"name": "services", "type": "dir"},
        ]

        apps_resp = MagicMock()
        apps_resp.status_code = 200
        apps_resp.json.return_value = [{"name": "web", "type": "dir"}]

        services_resp = MagicMock()
        services_resp.status_code = 200
        services_resp.json.return_value = [{"name": "api", "type": "dir"}]

        web_resp = MagicMock()
        web_resp.status_code = 200
        web_resp.json.return_value = [{"name": "package.json", "type": "file"}] + [
            {"name": f"file{i}.ts", "type": "file"} for i in range(5)
        ]

        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = [{"name": "pyproject.toml", "type": "file"}] + [
            {"name": f"file{i}.py", "type": "file"} for i in range(4)
        ]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, apps_resp, services_resp, web_resp, api_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "mixed"
        assert config["repo_is_empty"] is False
        directories = {item["path"]: item for item in config["directories"]}
        assert directories["apps/web/"]["type"] == "node"
        assert directories["apps/web/"]["install_cmd"] == "npm ci"
        assert directories["apps/web/"]["build_cmd"] == "npm run build"
        assert directories["apps/web/"]["test_cmd"] == "npm test"
        assert directories["apps/web/"]["lint_cmd"] == "npx eslint ."
        assert directories["services/api/"]["type"] == "python"
        assert directories["services/api/"]["lint_cmd"] == "ruff check"
        assert directories["services/api/"]["test_cmd"] == "python3 -m pytest"

    @pytest.mark.asyncio
    async def test_detect_build_config_keeps_mixed_root_manifests_at_same_path(self):
        """A root directory with both Python and Node manifests keeps both entries."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [
            {"name": "pyproject.toml", "type": "file"},
            {"name": "package.json", "type": "file"},
            {"name": "app.py", "type": "file"},
            {"name": "index.ts", "type": "file"},
        ]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "mixed"
        directories = [
            item for item in config["directories"] if item["path"] == "./"
        ]
        assert len(directories) == 2
        directories_by_type = {item["type"]: item for item in directories}
        assert directories_by_type["python"]["lint_cmd"] == "ruff check"
        assert directories_by_type["python"]["test_cmd"] == "python3 -m pytest"
        assert directories_by_type["node"]["install_cmd"] == "npm ci"
        assert directories_by_type["node"]["build_cmd"] == "npm run build"
        assert directories_by_type["node"]["test_cmd"] == "npm test"
        assert "lint_cmd" not in directories_by_type["node"]

    @pytest.mark.asyncio
    async def test_detect_build_config_not_empty_when_nested_fetch_fails(self):
        """A failed second-level fetch must not yield repo_is_empty=True."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": "apps", "type": "dir"}]

        apps_resp = MagicMock()
        apps_resp.status_code = 200
        apps_resp.json.return_value = [{"name": "web", "type": "dir"}]

        web_resp = MagicMock()
        web_resp.status_code = 502
        web_resp.json.return_value = {}

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, apps_resp, web_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []
        assert config["repo_is_empty"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_sets_repo_is_empty_for_readme_only(self):
        """A repo with only a README is classified empty → repo_is_empty True."""
        root_files = [
            {"name": "README.md", "type": "file"},
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

        assert config["repo_is_empty"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_sets_repo_is_empty_true_for_config_only(self):
        """A repo with only config/manifests remains empty → repo_is_empty True."""
        root_files = [
            {"name": "package.json", "type": "file"},
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

        assert config["repo_is_empty"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_keeps_repo_is_empty_true_at_threshold(self):
        """A lightly scaffolded repo at the threshold still counts as empty."""
        root_files = [
            {"name": "src", "type": "dir"},
        ]
        subdir_files = [
            {"name": f"file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD)
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

        assert config["repo_is_empty"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_sets_repo_is_empty_false_over_threshold(self):
        """A lightly scaffolded repo over the threshold is non-empty."""
        root_files = [
            {"name": "src", "type": "dir"},
        ]
        subdir_files = [
            {"name": f"file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD + 1)
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

        assert config["repo_is_empty"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_not_empty_when_subdir_fetch_fails(self):
        """A failed subdir fetch must not yield repo_is_empty=True.

        Regression: a monorepo whose real source lives under subdirectories
        (backend/, agent/, ...) was classified greenfield when a transient
        subdir listing error left the partial listing looking empty.
        """
        root_files = [
            {"name": "README.md", "type": "file"},
            {"name": "backend", "type": "dir"},
        ]
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = root_files

        # The backend/ subdir listing fails to fetch.
        subdir_resp = MagicMock()
        subdir_resp.status_code = 502
        subdir_resp.json.return_value = {}

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, subdir_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["repo_is_empty"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_not_empty_when_root_fetch_fails(self):
        """A failed root fetch must not yield a greenfield signal."""
        root_resp = MagicMock()
        root_resp.status_code = 502
        root_resp.json.return_value = {}

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config.get("repo_is_empty") is not True

    @pytest.mark.asyncio
    async def test_detect_build_config_empty_when_all_subdir_fetches_succeed(self):
        """A fully-read, genuinely empty repo still classifies as greenfield."""
        root_files = [
            {"name": "README.md", "type": "file"},
            {"name": "docs", "type": "dir"},
        ]
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = root_files

        # docs/ fetched successfully and contains no real source.
        subdir_resp = MagicMock()
        subdir_resp.status_code = 200
        subdir_resp.json.return_value = [
            {"name": "guide.md", "type": "file"},
        ]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, subdir_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["repo_is_empty"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_has_ci_true_when_workflows_present(self):
        """A workflow file under .github/workflows => has_ci True."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": ".github", "type": "dir"}]

        github_resp = MagicMock()
        github_resp.status_code = 200
        github_resp.json.return_value = [{"name": "workflows", "type": "dir"}]

        workflows_resp = MagicMock()
        workflows_resp.status_code = 200
        workflows_resp.json.return_value = [
            {"name": "ci.yml", "type": "file"},
            {"name": "README.md", "type": "file"},
        ]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, github_resp, workflows_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["has_ci"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_has_ci_true_for_yaml_extension(self):
        """A .yaml (not .yml) workflow still flips has_ci True."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": ".github", "type": "dir"}]

        github_resp = MagicMock()
        github_resp.status_code = 200
        github_resp.json.return_value = [{"name": "workflows", "type": "dir"}]

        workflows_resp = MagicMock()
        workflows_resp.status_code = 200
        workflows_resp.json.return_value = [{"name": "deploy.yaml", "type": "file"}]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, github_resp, workflows_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["has_ci"] is True

    @pytest.mark.asyncio
    async def test_detect_build_config_has_ci_false_when_no_workflows_dir(self):
        """A fully-read repo with no .github/workflows => has_ci False."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": "pyproject.toml", "type": "file"}]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["has_ci"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_has_ci_false_when_workflows_dir_empty(self):
        """A read .github/workflows with no yml/yaml files => has_ci False."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": ".github", "type": "dir"}]

        github_resp = MagicMock()
        github_resp.status_code = 200
        github_resp.json.return_value = [{"name": "workflows", "type": "dir"}]

        workflows_resp = MagicMock()
        workflows_resp.status_code = 200
        workflows_resp.json.return_value = [{"name": "README.md", "type": "file"}]

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, github_resp, workflows_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["has_ci"] is False

    @pytest.mark.asyncio
    async def test_detect_build_config_has_ci_none_when_read_incomplete(self):
        """An incomplete walk never asserts no-CI => has_ci None."""
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = [{"name": "backend", "type": "dir"}]

        # The backend/ subdir listing fails to fetch, so the walk is incomplete
        # and we cannot rule out a workflow living under an unread path.
        subdir_resp = MagicMock()
        subdir_resp.status_code = 502
        subdir_resp.json.return_value = {}

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, subdir_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["has_ci"] is None


class TestFallbackTestEntrypointDetection:
    """Non-manifest / additional-language test entrypoint detection.

    These broaden the no-CI merge gate so repos whose tests run via
    make/tox/nox/a root script — or a language outside the core four
    (Ruby, JVM, Elixir, .NET, PHP) — produce a runnable ``test_cmd`` instead of
    silently merging UNVERIFIED. The emitted commands mirror the agent-side
    fallback detector in agent/src/utils/test-runners.ts.
    """

    @pytest.mark.asyncio
    async def test_makefile_with_test_target_emits_make_test(self):
        """A Makefile that declares a real ``test:`` target → ``make test``.

        The detector must fetch the Makefile CONTENT (a second request) and
        confirm the target before emitting the command.
        """
        root = _listing_resp([{"name": "Makefile", "type": "file"}])
        makefile = _file_content_resp(
            "build:\n\tgo build ./...\n\ntest:\n\tgo test ./...\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, makefile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["type"] == "other"
        assert dirs[0]["test_cmd"] == "make test"
        assert dirs[0]["path"] == "./"

    @pytest.mark.asyncio
    async def test_makefile_without_test_target_emits_no_test_cmd(self):
        """A Makefile with NO ``test:`` target must not emit ``make test``."""
        root = _listing_resp([{"name": "Makefile", "type": "file"}])
        makefile = _file_content_resp("build:\n\tgo build ./...\n\nlint:\n\truff check\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, makefile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []

    @pytest.mark.asyncio
    async def test_tox_ini_emits_tox(self):
        root = _listing_resp([{"name": "tox.ini", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["type"] == "other"
        assert dirs[0]["test_cmd"] == "tox"

    @pytest.mark.asyncio
    async def test_noxfile_emits_nox(self):
        root = _listing_resp([{"name": "noxfile.py", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["test_cmd"] == "nox"

    @pytest.mark.asyncio
    async def test_root_test_script_emits_bash_invocation(self):
        root = _listing_resp(
            [
                {"name": "run-tests.sh", "type": "file"},
                {"name": "main.c", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["test_cmd"] == "bash run-tests.sh"

    @pytest.mark.asyncio
    async def test_ruby_gemfile_and_rakefile_emit_rake_test(self):
        root = _listing_resp(
            [
                {"name": "Gemfile", "type": "file"},
                {"name": "Rakefile", "type": "file"},
                {"name": "app.rb", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["test_cmd"] == "bundle exec rake test"

    @pytest.mark.asyncio
    async def test_jvm_gradle_emits_gradlew_test(self):
        root = _listing_resp([{"name": "build.gradle", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "./gradlew test"

    @pytest.mark.asyncio
    async def test_jvm_maven_emits_mvn_test(self):
        root = _listing_resp([{"name": "pom.xml", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "mvn test"

    @pytest.mark.asyncio
    async def test_elixir_mix_emits_mix_test(self):
        root = _listing_resp([{"name": "mix.exs", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "mix test"

    @pytest.mark.asyncio
    async def test_dotnet_csproj_emits_dotnet_test(self):
        root = _listing_resp(
            [
                {"name": "App.csproj", "type": "file"},
                {"name": "Program.cs", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "dotnet test"

    @pytest.mark.asyncio
    async def test_php_phpunit_xml_emits_phpunit(self):
        root = _listing_resp([{"name": "phpunit.xml.dist", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "vendor/bin/phpunit"

    @pytest.mark.asyncio
    async def test_php_composer_with_test_script_emits_composer_test(self):
        """composer.json must declare ``scripts.test`` (verified by content)."""
        root = _listing_resp([{"name": "composer.json", "type": "file"}])
        composer = _file_content_resp(
            '{"scripts": {"test": "phpunit", "lint": "phpcs"}}'
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, composer],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"][0]["test_cmd"] == "composer test"

    @pytest.mark.asyncio
    async def test_php_composer_without_test_script_emits_no_test_cmd(self):
        """A composer.json with no ``scripts.test`` must not emit a command."""
        root = _listing_resp([{"name": "composer.json", "type": "file"}])
        composer = _file_content_resp('{"scripts": {"lint": "phpcs"}}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, composer],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []

    @pytest.mark.asyncio
    async def test_core_python_manifest_takes_precedence_over_makefile(self):
        """A pyproject.toml + Makefile dir keeps pytest and never adds make test.

        Precedence: a recognized language manifest's test_cmd wins per-directory,
        so the fallback is not emitted and the Makefile body is never fetched.
        """
        root = _listing_resp(
            [
                {"name": "pyproject.toml", "type": "file"},
                {"name": "Makefile", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],  # one call only — no Makefile content fetch
        ) as mock_req:
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 1
        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["type"] == "python"
        assert dirs[0]["test_cmd"] == "python3 -m pytest"

    @pytest.mark.asyncio
    async def test_bare_repo_with_no_entrypoints_yields_no_test_cmd(self):
        """A repo with none of the recognized signals stays test-less."""
        root = _listing_resp(
            [
                {"name": "README.md", "type": "file"},
                {"name": "LICENSE", "type": "file"},
                {"name": "notes.txt", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []
        assert not any(
            str(d.get("test_cmd") or "").strip() for d in config["directories"]
        )

    @pytest.mark.asyncio
    async def test_fallback_entry_makes_no_ci_gate_runnable(self):
        """A detected fallback test_cmd makes should_run_no_ci_test_gate True.

        Proves the detector output flows through to the gate decision:
        has_detectable_tests reads the emitted ``test_cmd`` and gates the merge.
        """
        from app.ci_resolution import should_run_no_ci_test_gate

        root = _listing_resp([{"name": "tox.ini", "type": "file"}])
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        project = Project(
            name="ext",
            user_id=TEST_USER_ID,
            github_owner="owner",
            github_repo="repo",
            integration_branch="main",
            build_config={"requires_ci_checks": False, **config},
        )
        assert (
            should_run_no_ci_test_gate(project, "main", gate_enabled=True) is True
        )


class TestMaybeRefreshEmptyBuildConfig:
    """Auto-refresh of an empty stored build_config once a repo gains structure."""

    @staticmethod
    def _detected_with_directories():
        return {
            "language": "python",
            "directories": [
                {
                    "path": "backend",
                    "type": "python",
                    "test_cmd": "python3 -m pytest",
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_refreshes_when_directories_empty(self, db_session):
        """Empty stored directories + repo now has a manifest => config refreshed."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Bootstrap",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="bootstrap-repo",
            integration_branch="develop",
            build_config={"language": "python", "directories": [], "repo_is_empty": True},
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=self._detected_with_directories(),
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is True
        # Detection ran against the project's integration branch.
        mock_detect.assert_awaited_once()
        assert mock_detect.await_args.kwargs.get("ref") == "develop"
        await db_session.refresh(project)
        dirs = (project.build_config or {}).get("directories")
        assert dirs and dirs[0]["path"] == "backend"

    @pytest.mark.asyncio
    async def test_noop_when_directories_already_populated(self, db_session):
        """A stored repo_is_empty=False signal => return immediately with NO GitHub call."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Configured",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="configured-repo",
            integration_branch="develop",
            build_config={
                "language": "python",
                "directories": [{"path": ".", "type": "python"}],
                "repo_is_empty": False,
            },
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
            ) as mock_token,
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is False
        mock_detect.assert_not_awaited()
        mock_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preserves_operator_overrides(self, db_session):
        """Operator-set fields survive the auto-refresh."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Overrides",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="overrides-repo",
            integration_branch="develop",
            build_config={
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
                "env_vars": {"FOO": "bar"},
                "task_branch_namespace": "team-x",
                "protected_branches": ["main", "release"],
            },
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=self._detected_with_directories(),
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is True
        await db_session.refresh(project)
        bc = project.build_config or {}
        assert bc.get("env_vars") == {"FOO": "bar"}
        assert bc.get("task_branch_namespace") == "team-x"
        assert bc.get("protected_branches") == ["main", "release"]
        assert (bc.get("directories") or [{}])[0].get("path") == "backend"

    @pytest.mark.asyncio
    async def test_detection_empty_leaves_config_unchanged(self, db_session):
        """Detection finding no directories => config untouched, no crash, no commit."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        original = {"language": "python", "directories": [], "repo_is_empty": True}
        project = Project(
            name="StillEmpty",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="still-empty-repo",
            integration_branch="develop",
            build_config=dict(original),
        )
        db_session.add(project)
        await db_session.commit()

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
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is False
        await db_session.refresh(project)
        assert (project.build_config or {}).get("directories") == []

    @pytest.mark.asyncio
    async def test_swallows_detection_exception(self, db_session):
        """A detection failure is logged and swallowed, never raised."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Boom",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="boom-repo",
            integration_branch="develop",
            build_config={"language": "python", "directories": [], "repo_is_empty": True},
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                side_effect=RuntimeError("github down"),
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is False

    @pytest.mark.asyncio
    async def test_refreshes_stale_repo_is_empty_even_when_directories_populated(
        self, db_session
    ):
        """A populated config with repo_is_empty=True still refreshes until the signal flips."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Stale Empty Signal",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="stale-empty-signal",
            integration_branch="develop",
            build_config={
                "language": "python",
                "directories": [{"path": ".", "type": "python"}],
                "repo_is_empty": True,
            },
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [{"path": "backend", "type": "python"}],
                    "repo_is_empty": False,
                },
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is True
        mock_detect.assert_awaited_once()
        await db_session.refresh(project)
        assert (project.build_config or {}).get("repo_is_empty") is False

    @pytest.mark.asyncio
    async def test_refreshes_when_detection_incomplete_even_if_not_empty(
        self, db_session
    ):
        """A repo flagged detection_incomplete (transient onboarding failure) is
        re-detected on landing even though repo_is_empty is absent/False, and a
        successful re-detect CLEARS the marker so it becomes a no-op next time."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Transient Failure",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="transient-failure-repo",
            integration_branch="develop",
            # No repo_is_empty, but detection never completed at onboarding.
            build_config={"detection_incomplete": True},
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [{"path": "backend", "type": "python"}],
                    "repo_is_empty": False,
                },
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is True
        mock_detect.assert_awaited_once()
        await db_session.refresh(project)
        bc = project.build_config or {}
        # Real test suite now detected, and the transient-failure marker is
        # cleared so the next landing is a no-op.
        assert (bc.get("directories") or [{}])[0].get("path") == "backend"
        assert bc.get("detection_incomplete") is False

    @pytest.mark.asyncio
    async def test_noop_when_detection_completed_with_no_tests(self, db_session):
        """A genuinely test-less repo (detection COMPLETED, repo_is_empty=False,
        no detection_incomplete marker) is NOT re-scanned on landing — proving
        the change is not a per-landing re-scan loop."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Legitimately Test-less",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="testless-repo",
            integration_branch="develop",
            build_config={
                "language": "python",
                "directories": [{"path": ".", "type": "python"}],
                "repo_is_empty": False,
                "detection_incomplete": False,
            },
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
            ) as mock_token,
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is False
        mock_detect.assert_not_awaited()
        mock_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redetect_failing_again_keeps_marker_set(self, db_session):
        """If re-detection still fails (returns {}), the detection_incomplete
        marker is preserved so the repo stays eligible for a later retry."""
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Still Failing",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="still-failing-repo",
            integration_branch="develop",
            build_config={"detection_incomplete": True},
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            refreshed = await maybe_refresh_empty_build_config(project, db_session)

        assert refreshed is False
        mock_detect.assert_awaited_once()
        await db_session.refresh(project)
        # Marker preserved — repo remains eligible on the next landing.
        assert (project.build_config or {}).get("detection_incomplete") is True


class TestOnboardDetectionIncompleteMarker:
    """Onboarding sets detection_incomplete only when detection did not complete."""

    async def _onboard(self, client, repo, *, detect_return=None, detect_exc=None):
        kwargs = {"new_callable": AsyncMock}
        if detect_exc is not None:
            kwargs["side_effect"] = detect_exc
        else:
            kwargs["return_value"] = detect_return
        with (
            patch("app.routers.admin_onboard._detect_build_config", **kwargs),
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
                json={"github_owner": "acme", "github_repo": repo},
            )
        assert resp.status_code == 200
        return resp.json()["project_id"]

    @pytest.mark.asyncio
    async def test_detection_exception_marks_incomplete(
        self, client: AsyncClient, db_session
    ):
        """A transient GitHub failure (exception) during detection => marker True."""
        project_id = await self._onboard(
            client, "boom-onboard", detect_exc=RuntimeError("github 503")
        )
        project = await db_session.get(Project, project_id)
        assert (project.build_config or {}).get("detection_incomplete") is True

    @pytest.mark.asyncio
    async def test_detection_empty_result_marks_incomplete(
        self, client: AsyncClient, db_session
    ):
        """A non-200 listing (detector returns {}) => marker True."""
        project_id = await self._onboard(
            client, "empty-onboard", detect_return={}
        )
        project = await db_session.get(Project, project_id)
        assert (project.build_config or {}).get("detection_incomplete") is True

    @pytest.mark.asyncio
    async def test_successful_detection_leaves_marker_false(
        self, client: AsyncClient, db_session
    ):
        """A completed detection => marker False (not flagged for re-scan)."""
        project_id = await self._onboard(
            client,
            "good-onboard",
            detect_return={
                "language": "python",
                "directories": [{"path": "backend", "type": "python"}],
                "repo_is_empty": False,
            },
        )
        project = await db_session.get(Project, project_id)
        assert (project.build_config or {}).get("detection_incomplete") is False


class TestClassifyRepoEmptiness:
    """Unit tests for the pure _classify_repo_emptiness helper."""

    def test_truly_empty_repo(self):
        assert _classify_repo_emptiness([]) is True

    def test_readme_and_license_only(self):
        items = [
            {"name": "README.md", "type": "file"},
            {"name": "LICENSE", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_ci_and_config_dotfiles_only(self):
        items = [
            {"name": ".github", "type": "dir"},
            {"name": ".gitignore", "type": "file"},
            {"name": ".editorconfig", "type": "file"},
            {"name": "CONTRIBUTING.md", "type": "file"},
            {"name": "CODE_OF_CONDUCT.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_source_directory_with_one_file_is_still_empty_below_threshold(self):
        items = [
            {"name": "README.md", "type": "file"},
            {"name": "src", "type": "dir"},
            {"name": "main.py", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_source_manifest_is_empty_below_threshold(self):
        items = [
            {"name": "pyproject.toml", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_threshold_source_file_count_is_still_empty(self):
        items = [
            {"name": f"src/file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD)
        ]
        assert _classify_repo_emptiness(items) is True

    def test_over_threshold_source_file_count_is_non_empty(self):
        items = [
            {"name": f"src/file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD + 1)
        ]
        assert _classify_repo_emptiness(items) is False

    def test_git_dir_ignored(self):
        items = [
            {"name": ".git", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_malformed_items_skipped(self):
        items = [
            "not-a-dict",
            {"type": "file"},  # no name
            {"name": "", "type": "file"},
            {"name": "README.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) is True

    def test_incomplete_listing_is_not_empty(self):
        """An incomplete read can never be classified as greenfield."""
        # Even an empty fetched listing degrades to not-empty when the read
        # was incomplete (a subdir fetch failed).
        assert _classify_repo_emptiness([], listing_complete=False) is False

    def test_incomplete_listing_overrides_below_threshold(self):
        """Below-threshold source + incomplete read => still not empty."""
        items = [{"name": "README.md", "type": "file"}]
        assert _classify_repo_emptiness(items, listing_complete=False) is False
