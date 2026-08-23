"""Tests for onboarding endpoint — build config detection and auto_remediation defaults."""

import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient

from app.main import app
from app.models import Project, ProjectMessage, ProjectStatus, UserIntegration
from app.auth import get_current_user, require_admin
from app.routers.admin_onboard import (
    GREENFIELD_SOURCE_FILE_THRESHOLD,
    _detect_build_config,
    _classify_repo_emptiness,
    _seed_strategic_intent,
)

TEST_USER_ID = "test-user-123"
TEST_RUNNER_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "__fixtures__" / "test_runner_layouts.json"
)
TEST_RUNNER_COMMANDS_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "__fixtures__" / "test_runner_commands.json"
)

# Families where the backend's stored command intentionally differs from the
# agent's auto-detect command in test_runner_commands.json:
#   pytest — backend stores "python3 -m pytest"; agent adds "-x -q" flags
#   script — dynamic args (bash <script>), varies per repo
_BACKEND_COMMAND_SKIP_FAMILIES: frozenset[str] = frozenset({"pytest", "script"})


def _load_test_runner_layouts():
    with TEST_RUNNER_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_test_runner_commands():
    with TEST_RUNNER_COMMANDS_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


TEST_RUNNER_LAYOUTS = _load_test_runner_layouts()
TEST_RUNNER_COMMANDS = _load_test_runner_commands()


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


def _build_layout_tree(layout):
    """Synthesize a directory tree + file-content map from a layout's flat file
    list, so a single URL-routing GitHub mock can serve the detector's BFS walk
    (root listing → nested container/package listings → inline manifest content
    fetches) regardless of request order.

    Returns ``(dir_listings, file_contents)`` where ``dir_listings`` maps a
    directory path (``""`` for the repo root, ``"services/api"`` for nested
    dirs) to a Contents-API-style listing of its IMMEDIATE children, and
    ``file_contents`` maps a file path to its declared text body.
    """
    dir_children: dict[str, dict[str, str]] = {"": {}}
    file_contents: dict[str, str] = {}
    for item in layout["files"]:
        path = item["path"]
        file_contents[path] = item.get("content", "")
        parts = path.split("/")
        for i in range(len(parts) - 1):
            parent = "/".join(parts[:i])  # "" at the root
            dir_children.setdefault(parent, {})[parts[i]] = "dir"
            dir_children.setdefault("/".join(parts[: i + 1]), {})
        parent = "/".join(parts[:-1])
        dir_children.setdefault(parent, {})[parts[-1]] = "file"
    dir_listings = {
        directory: [{"name": name, "type": typ} for name, typ in children.items()]
        for directory, children in dir_children.items()
    }
    return dir_listings, file_contents


def _make_layout_github_side_effect(layout):
    """Build a ``_github_request`` side-effect callable that routes by URL.

    The detector interleaves directory listings and single-file content fetches
    across a depth-bounded BFS, so an order-sensitive response list is brittle
    for nested layouts. Routing on the requested contents path instead tolerates
    any traversal order: a path that names a file returns that file's body, a
    path that names a directory returns its child listing, and an unknown path
    returns an empty listing.
    """
    dir_listings, file_contents = _build_layout_tree(layout)
    prefix = "https://api.github.com/repos/owner/repo/contents/"

    def _route(method, url, headers, *args, **kwargs):
        path = url[len(prefix) :] if url.startswith(prefix) else url
        if path in file_contents:
            return _file_content_resp(file_contents[path])
        if path in dir_listings:
            return _listing_resp(dir_listings[path])
        return _listing_resp([])

    return _route


def _backend_family_for_directory(directory):
    directory_type = directory.get("type")
    if directory_type == "python":
        return "pytest"
    if directory_type == "node":
        return "npm"
    if directory_type == "go":
        return "go"
    if directory_type == "rust":
        return "cargo"
    if directory_type == "go":
        return "go"

    test_cmd = str(directory.get("test_cmd") or "").strip()
    if test_cmd == "make test":
        return "make"
    if test_cmd == "tox":
        return "tox"
    if test_cmd == "nox":
        return "nox"
    if test_cmd == "bundle exec rake test":
        return "ruby"
    if test_cmd == "./gradlew test":
        return "gradle"
    if test_cmd == "mvn test":
        return "maven"
    if test_cmd == "mix test":
        return "elixir"
    if test_cmd == "just test":
        return "just"
    if test_cmd == "task test":
        return "task"
    if test_cmd == "dotnet test":
        return "dotnet"
    if test_cmd == "vendor/bin/phpunit":
        return "php"
    if test_cmd == "composer test":
        return "composer"
    if test_cmd == "swift test":
        return "swift"
    if test_cmd == "sbt test":
        return "scala"
    if test_cmd == "bazel test //...":
        return "bazel"
    if test_cmd == "cabal test all":
        return "haskell"
    if test_cmd == "flutter test":
        return "flutter"
    if test_cmd.startswith("bash "):
        return "script"
    return None


@pytest.fixture(autouse=True)
def override_auth():
    async def _user():
        return {"user_id": TEST_USER_ID, "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_admin] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_admin, None)


class TestOnboardNonAdminOwnership:
    """Self-service onboarding requires caller-scoped GitHub write access."""

    @pytest.mark.asyncio
    async def test_non_admin_user_can_onboard_and_becomes_owner(
        self, client: AsyncClient, db_session
    ):
        async def _plain_user():
            return {
                "user_id": "fresh-user-999",
                "email": "newcomer@example.com",
                "role": "user",
            }

        # Override the autouse admin identity with a plain allowlisted user for
        # the duration of this request.
        app.dependency_overrides[get_current_user] = _plain_user
        db_session.add(
            UserIntegration(
                user_id="fresh-user-999", github_oauth_token="oauth-secret-ref"
            )
        )
        await db_session.commit()
        repo_access = MagicMock()
        repo_access.status_code = 200
        repo_access.json.return_value = {"permissions": {"push": True}}
        try:
            with (
                patch(
                    "app.secrets_manager.get_user_secret",
                    new_callable=AsyncMock,
                    return_value="caller-oauth-token",
                ),
                patch(
                    "app.github._github_request",
                    new_callable=AsyncMock,
                    return_value=repo_access,
                ),
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
                patch(
                    "app.github.ensure_repo_initialized",
                    new_callable=AsyncMock,
                    return_value=False,
                ),
            ):
                resp = await client.post(
                    "/api/admin/projects/onboard",
                    json={"github_owner": "acme", "github_repo": "firstproj"},
                )
        finally:
            app.dependency_overrides[get_current_user] = _restore_admin_override

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "project_id" in data

        from sqlalchemy import select
        from app.models import ProjectMember

        project = await db_session.get(Project, data["project_id"])
        assert project is not None
        assert project.user_id == "fresh-user-999"
        assert project.autonomous_enabled is False

        member = (
            await db_session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == "fresh-user-999",
                )
            )
        ).scalars().first()
        assert member is not None
        assert member.role == "owner"

    @pytest.mark.asyncio
    async def test_non_admin_without_linked_github_identity_is_forbidden_before_token_mint(
        self, client: AsyncClient
    ):
        async def _plain_user():
            return {"user_id": "no-github", "email": "user@example.com", "role": "user"}

        app.dependency_overrides[get_current_user] = _plain_user
        mint = AsyncMock(return_value="installation-token")
        try:
            with patch("app.github.get_repo_installation_token", mint):
                resp = await client.post(
                    "/api/admin/projects/onboard",
                    json={"github_owner": "acme", "github_repo": "private-repo"},
                )
        finally:
            app.dependency_overrides[get_current_user] = _restore_admin_override

        assert resp.status_code == 403
        assert "Link your GitHub account" in resp.json()["error"]["message"]
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_admin_without_push_permission_is_forbidden_before_token_mint(
        self, client: AsyncClient, db_session
    ):
        async def _plain_user():
            return {"user_id": "read-only", "email": "user@example.com", "role": "user"}

        db_session.add(UserIntegration(user_id="read-only", github_oauth_token="oauth-ref"))
        await db_session.commit()
        repo_access = MagicMock()
        repo_access.status_code = 200
        repo_access.json.return_value = {"permissions": {"pull": True, "push": False}}
        app.dependency_overrides[get_current_user] = _plain_user
        mint = AsyncMock(return_value="installation-token")
        try:
            with (
                patch(
                    "app.secrets_manager.get_user_secret",
                    new_callable=AsyncMock,
                    return_value="caller-oauth-token",
                ),
                patch("app.github._github_request", new_callable=AsyncMock, return_value=repo_access),
                patch("app.github.get_repo_installation_token", mint),
            ):
                resp = await client.post(
                    "/api/admin/projects/onboard",
                    json={"github_owner": "acme", "github_repo": "private-repo"},
                )
        finally:
            app.dependency_overrides[get_current_user] = _restore_admin_override

        assert resp.status_code == 403
        assert "push access" in resp.json()["error"]["message"]
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_bypasses_caller_github_identity_check(
        self, client: AsyncClient
    ):
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
            ) as installation_token,
            patch("app.github.get_repo_default_branch", new_callable=AsyncMock, return_value="main"),
            patch("app.github.ensure_repo_initialized", new_callable=AsyncMock, return_value=False),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "admin-project"},
            )

        assert resp.status_code == 200, resp.text
        installation_token.assert_any_await("acme", "admin-project")


async def _restore_admin_override():
    return {"user_id": TEST_USER_ID, "email": "admin@example.com", "role": "admin"}


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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
    async def test_onboard_preview_only_returns_detected_config_without_persisting(
        self, client: AsyncClient, db_session
    ):
        """preview_only=true returns onboarding detection data but creates no project row."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [
                        {
                            "path": "backend/",
                            "type": "python",
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                    "github_repo": "preview-only-repo",
                    "preview_only": True,
                    "build_config_override": {"requires_ci_checks": False},
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["default_branch"] == "main"
        assert data["build_config"]["requires_ci_checks"] is False
        assert data["merge_verification_verdict"]["state"] == "tests_gated"
        from sqlalchemy import select
        from app.models import Project as ProjectModel

        result = await db_session.execute(
            select(ProjectModel).where(
                ProjectModel.github_owner == "acme",
                ProjectModel.github_repo == "preview-only-repo",
            )
        )
        assert result.scalars().first() is None

    @pytest.mark.asyncio
    async def test_onboard_persists_edited_merge_gate_settings(
        self, client: AsyncClient, db_session
    ):
        """Final onboarding stores the edited no-CI merge-gate settings on the project."""
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": "python",
                    "directories": [
                        {
                            "path": "backend/",
                            "type": "python",
                            "test_cmd": "",
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                    "github_repo": "edited-merge-gate-repo",
                    "build_config_override": {
                        "requires_ci_checks": False,
                        "directories": [
                            {
                                "path": "backend/",
                                "type": "python",
                                "test_cmd": "python3 -m pytest",
                            }
                        ],
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        project = await db_session.get(Project, data["project_id"])
        bc = project.build_config or {}
        assert bc.get("requires_ci_checks") is False
        assert bc["directories"][0]["test_cmd"] == "python3 -m pytest"
        assert data["merge_verification_verdict"]["state"] == "tests_gated"

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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
        assert pm.intent.user_purpose == goal
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
        assert msg.sequence == 1
        assert msg.surface == "conversation"
        assert msg.handling_state == "pending"

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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.from_type == "user",
                        ProjectMessage.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )
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
        # A pyproject-only dir's test command is gated on the body configuring
        # pytest (matching the runtime), so the detector fetches its content.
        pyproject_resp = _file_content_resp(
            "[tool.pytest.ini_options]\nminversion = '7'\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, pyproject_resp],
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
            {"name": "setup.cfg", "type": "file"},
        ]
        root_resp = MagicMock()
        root_resp.status_code = 200
        root_resp.json.return_value = root_files

        subdir_resp = MagicMock()
        subdir_resp.status_code = 200
        subdir_resp.json.return_value = subdir_files
        setup_cfg_resp = _file_content_resp("[tool:pytest]\naddopts = -q\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, subdir_resp, setup_cfg_resp],
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
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, pyproject_resp],
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
    async def test_detect_build_config_emits_pytest_for_bare_pytest_ini(self):
        """A bare pytest.ini (no pyproject/setup) is recognized as a Python
        project and emits the canonical 'python3 -m pytest' test_cmd, mirroring
        the agent-side detector so the no-CI merge gate runs the real suite."""
        root_files = [
            {"name": "pytest.ini", "type": "file"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[_listing_resp(root_files)],
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
    async def test_detect_build_config_emits_pytest_for_subdir_pytest_ini(self):
        """A bare pytest.ini in a nested subdirectory is recognized the same way
        a core Python manifest would be, emitting a runnable pytest test_cmd."""
        root_files = [
            {"name": "service", "type": "dir"},
        ]
        subdir_files = [
            {"name": "pytest.ini", "type": "file"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[
                _listing_resp(root_files),
                _listing_resp(subdir_files),
            ],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["path"] == "service/"
        assert dir_config["type"] == "python"
        assert dir_config["test_cmd"] == "python3 -m pytest"

    @pytest.mark.asyncio
    async def test_detect_build_config_bare_pyproject_keeps_lint_only(self):
        """A pyproject.toml without a pytest signal is Python-for-lint, not pytest-gated."""
        root = _listing_resp([{"name": "pyproject.toml", "type": "file"}])
        pyproject = _file_content_resp(
            '[build-system]\nrequires = ["setuptools>=68"]\n'
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, pyproject],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["type"] == "python"
        assert dir_config["lint_cmd"] == "ruff check"
        assert "test_cmd" not in dir_config

    @pytest.mark.asyncio
    async def test_detect_build_config_bare_setup_cfg_keeps_lint_only(self):
        """A setup.cfg without pytest config must not claim a runnable suite."""
        root = _listing_resp([{"name": "setup.cfg", "type": "file"}])
        setup_cfg = _file_content_resp("[metadata]\nname = demo\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, setup_cfg],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["type"] == "python"
        assert dir_config["lint_cmd"] == "ruff check"
        assert "test_cmd" not in dir_config

    @pytest.mark.asyncio
    async def test_detect_build_config_bare_setup_py_keeps_lint_only(self):
        """A bare setup.py is a packaging signal only, not a pytest signal."""
        root = _listing_resp([{"name": "setup.py", "type": "file"}])

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        assert len(config["directories"]) == 1
        dir_config = config["directories"][0]
        assert dir_config["type"] == "python"
        assert dir_config["lint_cmd"] == "ruff check"
        assert "test_cmd" not in dir_config

    @pytest.mark.asyncio
    async def test_detect_build_config_pytest_ini_with_pyproject_no_duplicate(self):
        """pytest.ini alongside a core Python manifest yields exactly one Python
        directory — the (path, type) dedupe prevents a duplicate emission."""
        root_files = [
            {"name": "pytest.ini", "type": "file"},
            {"name": "pyproject.toml", "type": "file"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[_listing_resp(root_files)],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "python"
        python_dirs = [d for d in config["directories"] if d.get("type") == "python"]
        assert len(python_dirs) == 1
        assert python_dirs[0]["test_cmd"] == "python3 -m pytest"

    @pytest.mark.asyncio
    async def test_pytest_ini_project_gates_no_ci_test_run(self):
        """A pytest.ini-only build_config makes has_detectable_tests() and
        should_run_no_ci_test_gate() both return True, so the no-CI merge gate
        spawns verify_tests instead of falling open to UNVERIFIED."""
        from app.ci_resolution import (
            has_detectable_tests,
            should_run_no_ci_test_gate,
        )

        root_files = [
            {"name": "pytest.ini", "type": "file"},
        ]
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[_listing_resp(root_files)],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        # An EXTERNAL no-CI project carrying the detected config: the gate must
        # see a runnable suite and decide to run it (not fall open to UNVERIFIED).
        project = Project(
            name="pytest.ini no-CI repo",
            user_id=TEST_USER_ID,
            github_owner="owner",
            github_repo="repo",
            integration_branch="main",
            build_config={"requires_ci_checks": False, **config},
        )

        assert has_detectable_tests(project) is True
        assert should_run_no_ci_test_gate(project, "main", gate_enabled=True) is True

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
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        # The pyproject body fetch (to confirm pytest) must carry the same ref.
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        with (
            patch(
                "app.github._github_request",
                new_callable=AsyncMock,
                side_effect=[root_resp, subdir_resp, pyproject_resp],
            ) as mock_req,
            patch(
                "app.github_repo_snapshot._get_snapshot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await _detect_build_config("fake-token", "owner", "repo", ref="feature-x")

        assert mock_req.call_count == 3
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
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, pyproject_resp],
        ) as mock_req:
            await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 2
        # Both the listing and the pyproject body fetch must omit params (ref=None).
        for call in mock_req.call_args_list:
            assert call.kwargs.get("params") is None

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

        # Content fetches confirming each dir's runtime-honored test command:
        # the web package.json declares scripts.test, the api pyproject pytest.
        web_pkg_resp = _file_content_resp('{"scripts": {"test": "vitest run"}}')
        api_pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[
                root_resp,
                apps_resp,
                services_resp,
                web_resp,
                web_pkg_resp,
                api_resp,
                api_pyproject_resp,
            ],
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
        # Python is detected before Node, so the pyproject body is fetched first,
        # then the package.json body — both confirming their test commands.
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")
        package_resp = _file_content_resp('{"scripts": {"test": "jest"}}')

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, pyproject_resp, package_resp],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["language"] == "mixed"
        directories = [item for item in config["directories"] if item["path"] == "./"]
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
        """A failed second-level fetch must not yield an asserted-non-empty signal.

        An incomplete walk is undetermined (None), never a durable False — the
        greenfield decision is deferred to the live clone-time signal rather
        than suppressed by a false negative.
        """
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
        assert config["repo_is_empty"] is None

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
        # Node detection now fetches package.json to gate the test command;
        # a script-less package.json still records the dir but emits no test_cmd.
        package_resp = _file_content_resp('{"name": "thing"}')

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, package_resp],
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

        The incomplete walk is now recorded as undetermined (None), never a
        durable False either — emptiness is never asserted from a partial read.
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

        assert config["repo_is_empty"] is None

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
        pyproject_resp = _file_content_resp("[tool.pytest.ini_options]\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root_resp, pyproject_resp],
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


class TestDetectorRuntimeParity:
    """The stored detector must emit only test commands the runtime can honor.

    The no-CI preflight (``ci_resolution.has_detectable_tests``) reads the
    stored ``build_config.directories[].test_cmd``; the live merge verifier
    (``agent/src/utils/test-runners.ts``) re-resolves entrypoints with its own
    rules. These pin the alignment: Node ``npm test`` is persisted only when
    ``scripts.test`` exists, Python ``python3 -m pytest`` is unconditional for
    pytest.ini but content-gated for a pyproject- or setup.cfg-only dir, and a
    bare setup.py (packaging only) emits no test command at all.
    """

    @pytest.mark.asyncio
    async def test_bare_package_json_without_test_script_emits_no_test_cmd(self):
        """package.json with no ``scripts.test`` records node but no test_cmd.

        The runtime would resolve such a repo to no-tests, so persisting
        ``npm test`` is the exact drift that promises a false "tests gated".
        """
        root = _listing_resp(
            [
                {"name": "package.json", "type": "file"},
                {"name": "index.ts", "type": "file"},
            ]
        )
        package = _file_content_resp('{"name": "app", "scripts": {"build": "tsc"}}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node_dirs = [d for d in config["directories"] if d["type"] == "node"]
        assert len(node_dirs) == 1
        assert "test_cmd" not in node_dirs[0]
        # Install/build commands still seeded — only the unrunnable test is dropped.
        assert node_dirs[0]["install_cmd"] == "npm ci"
        assert node_dirs[0]["build_cmd"] == "npm run build"

    @pytest.mark.asyncio
    async def test_package_json_with_test_script_emits_npm_test(self):
        """package.json declaring ``scripts.test`` emits ``npm test``."""
        root = _listing_resp(
            [
                {"name": "package.json", "type": "file"},
                {"name": "index.ts", "type": "file"},
            ]
        )
        package = _file_content_resp('{"scripts": {"test": "vitest run"}}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node_dirs = [d for d in config["directories"] if d["type"] == "node"]
        assert len(node_dirs) == 1
        assert node_dirs[0]["test_cmd"] == "npm test"

    @pytest.mark.asyncio
    async def test_pytest_ini_only_dir_emits_python3_pytest(self):
        """A pytest.ini-only repo (no pyproject/setup) is now detected.

        The runtime honors pytest.ini directly; the stored detector previously
        missed it (it was absent from the python trigger set), causing a
        needless UNVERIFIED fallback. No content fetch is needed — pytest.ini is
        an unconditional pytest signal.
        """
        root = _listing_resp(
            [
                {"name": "pytest.ini", "type": "file"},
                {"name": "app.py", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],  # one call — pytest.ini needs no body fetch
        ) as mock_req:
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 1
        py_dirs = [d for d in config["directories"] if d["type"] == "python"]
        assert len(py_dirs) == 1
        assert py_dirs[0]["test_cmd"] == "python3 -m pytest"
        assert py_dirs[0]["lint_cmd"] == "ruff check"

    @pytest.mark.asyncio
    async def test_bare_pyproject_without_pytest_emits_no_test_cmd(self):
        """A packaging-only pyproject.toml (no pytest) records python, no test_cmd."""
        root = _listing_resp(
            [
                {"name": "pyproject.toml", "type": "file"},
                {"name": "app.py", "type": "file"},
            ]
        )
        pyproject = _file_content_resp(
            "[build-system]\nrequires = ['setuptools']\n\n[project]\nname = 'thing'\n"
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, pyproject],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        py_dirs = [d for d in config["directories"] if d["type"] == "python"]
        assert len(py_dirs) == 1
        assert "test_cmd" not in py_dirs[0]
        assert py_dirs[0]["lint_cmd"] == "ruff check"

    @pytest.mark.asyncio
    async def test_setup_py_dir_emits_no_test_cmd_without_pytest_signal(self):
        """A bare setup.py is packaging-only: the runtime emits no runner for it,
        so onboarding must not claim a pytest suite (no-CI detector parity)."""
        root = _listing_resp(
            [
                {"name": "setup.py", "type": "file"},
                {"name": "app.py", "type": "file"},
            ]
        )
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root],
        ) as mock_req:
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 1
        py_dirs = [d for d in config["directories"] if d["type"] == "python"]
        assert py_dirs and "test_cmd" not in py_dirs[0]

    @pytest.mark.asyncio
    async def test_bare_pyproject_keeps_no_ci_gate_unverified(self):
        """A script-less node dir leaves the no-CI gate at proceed-without-checks.

        Proves the conservative emission flows through to the gate decision:
        with no runnable ``test_cmd`` stored, ``should_run_no_ci_test_gate`` is
        False, so the operator sees no false "tests gated" promise.
        """
        from app.ci_resolution import should_run_no_ci_test_gate

        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp('{"name": "thing"}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
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
        assert should_run_no_ci_test_gate(project, "main", gate_enabled=True) is False


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
        makefile = _file_content_resp(
            "build:\n\tgo build ./...\n\nlint:\n\truff check\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, makefile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []

    @pytest.mark.asyncio
    async def test_makefile_with_prefixed_target_emits_no_test_cmd(self):
        """A Makefile whose only target is ``test-integration:`` (prefix, not an
        exact ``test`` target) must NOT emit ``make test``.

        ``make test`` only ever runs an EXACT ``test`` target; a prefixed name
        fails with "No rule to make target test". This locks the strict Makefile
        semantics in parity with the agent-side ``textDefinesMakeTestTarget``.
        """
        root = _listing_resp([{"name": "Makefile", "type": "file"}])
        makefile = _file_content_resp("test-integration:\n\tgo test ./...\n")

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, makefile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []

    @pytest.mark.asyncio
    async def test_justfile_with_test_target_emits_just_test(self):
        root = _listing_resp([{"name": "Justfile", "type": "file"}])
        justfile = _file_content_resp(
            'set shell := ["bash", "-eu", "-o", "pipefail", "-c"]\n\ntest:\n\tcargo test\n'
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, justfile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["type"] == "other"
        assert dirs[0]["test_cmd"] == "just test"

    @pytest.mark.asyncio
    async def test_justfile_without_test_target_emits_no_test_cmd(self):
        root = _listing_resp([{"name": "justfile", "type": "file"}])
        justfile = _file_content_resp(
            "build:\n\tcargo build\n\nlint:\n\tcargo clippy\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, justfile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert config["directories"] == []

    @pytest.mark.asyncio
    async def test_taskfile_yaml_with_test_task_emits_task_test(self):
        root = _listing_resp([{"name": "Taskfile.yaml", "type": "file"}])
        taskfile = _file_content_resp(
            "version: '3'\n\ntasks:\n  test:\n    cmds:\n      - go test ./...\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, taskfile],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["type"] == "other"
        assert dirs[0]["test_cmd"] == "task test"

    @pytest.mark.asyncio
    async def test_taskfile_without_test_task_emits_no_test_cmd(self):
        root = _listing_resp([{"name": "Taskfile.yml", "type": "file"}])
        taskfile = _file_content_resp(
            "version: '3'\n\ntasks:\n  build:\n    cmds:\n      - go build ./...\n"
        )

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, taskfile],
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
    async def test_scripts_test_sh_emits_repo_root_invocation(self):
        """scripts/test.sh must match the agent's repo-root bash invocation."""
        root = _listing_resp([{"name": "scripts", "type": "dir"}])
        scripts = _listing_resp([{"name": "test.sh", "type": "file"}])

        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, scripts],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        dirs = config["directories"]
        assert len(dirs) == 1
        assert dirs[0]["path"] == "./"
        assert dirs[0]["test_cmd"] == "bash scripts/test.sh"

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
    async def test_node_package_with_test_script_emits_npm_test(self):
        """package.json with a real ``scripts.test`` still emits ``npm test``."""
        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp('{"scripts": {"test": "jest", "build": "tsc"}}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node = config["directories"][0]
        assert node["type"] == "node"
        assert node["install_cmd"] == "npm ci"
        assert node["build_cmd"] == "npm run build"
        assert node["test_cmd"] == "npm test"

    @pytest.mark.asyncio
    async def test_node_package_without_test_script_emits_no_test_cmd(self):
        """package.json with no ``scripts.test`` keeps the build entry but emits
        no ``test_cmd`` — so has_detectable_tests / the verdict read 'unverified'.
        """
        from app.ci_resolution import (
            compute_merge_verification_verdict,
            has_detectable_tests,
        )

        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp('{"scripts": {"lint": "eslint ."}}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node = config["directories"][0]
        assert node["type"] == "node"
        assert node["install_cmd"] == "npm ci"
        assert node["build_cmd"] == "npm run build"
        assert "test_cmd" not in node

        project = MagicMock()
        # No-CI repo: requires_ci_checks=False routes the verdict through the
        # tests-gated/unverified branch, which now reflects the missing test_cmd.
        config["requires_ci_checks"] = False
        project.build_config = config
        assert has_detectable_tests(project) is False
        assert compute_merge_verification_verdict(project)["state"] == "unverified"

    @pytest.mark.asyncio
    async def test_node_package_no_scripts_key_emits_no_test_cmd(self):
        """package.json with no ``scripts`` object at all emits no ``test_cmd``."""
        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp('{"name": "pkg", "version": "1.0.0"}')
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node = config["directories"][0]
        assert node["type"] == "node"
        assert "test_cmd" not in node

    @pytest.mark.asyncio
    async def test_node_package_fetch_failure_emits_no_test_cmd(self):
        """A failed package.json content fetch is treated as 'cannot confirm' —
        the build entry is still emitted but without ``test_cmd``."""
        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp("", status_code=404)
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node = config["directories"][0]
        assert node["type"] == "node"
        assert "test_cmd" not in node

    @pytest.mark.asyncio
    async def test_node_package_malformed_json_emits_no_test_cmd(self):
        """An unparseable package.json body emits no ``test_cmd``."""
        root = _listing_resp([{"name": "package.json", "type": "file"}])
        package = _file_content_resp("{not valid json")
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=[root, package],
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        node = config["directories"][0]
        assert node["type"] == "node"
        assert "test_cmd" not in node

    @pytest.mark.asyncio
    async def test_core_python_manifest_takes_precedence_over_makefile(self):
        """A pyproject.toml + Makefile dir keeps pytest and never adds make test.

        Precedence: a recognized language manifest's test_cmd wins per-directory,
        so the fallback is not emitted and the Makefile body is never fetched.
        The pyproject body IS fetched (to confirm it configures pytest), but the
        Makefile is not — proving the python manifest short-circuits the fallback.
        """
        root = _listing_resp(
            [
                {"name": "pyproject.toml", "type": "file"},
                {"name": "Makefile", "type": "file"},
            ]
        )
        pyproject = _file_content_resp("[tool.pytest.ini_options]\n")
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            # Two calls: the root listing + the pyproject body. The Makefile body
            # is never fetched because the python test_cmd already won.
            side_effect=[root, pyproject],
        ) as mock_req:
            config = await _detect_build_config("fake-token", "owner", "repo")

        assert mock_req.call_count == 2
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
        assert should_run_no_ci_test_gate(project, "main", gate_enabled=True) is True


class TestSharedTestRunnerLayouts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "layout",
        TEST_RUNNER_LAYOUTS,
        ids=[layout["name"] for layout in TEST_RUNNER_LAYOUTS],
    )
    async def test_shared_layout_parity(self, layout):
        """The onboarding detector agrees with the shared runner-family fixture."""
        from app.ci_resolution import has_detectable_tests

        # The detector walks a depth-bounded subtree (root → nested containers /
        # packages) and fetches the CONTENT of certain manifests before emitting
        # a command: package.json (node ``scripts.test`` guard), composer.json
        # (composer ``scripts.test`` guard), Makefile (real ``test:`` target),
        # and pyproject.toml when pytest must be confirmed from its body.
        # A URL-routing side effect serves listings and file bodies in whatever
        # order the BFS issues them, so nested layouts work without sequencing.
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=_make_layout_github_side_effect(layout),
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        directories = config.get("directories") or []
        assert (
            any(str(item.get("test_cmd") or "").strip() for item in directories)
            is layout["detectable"]
        )

        project = Project(
            name=f"shared-layout-{layout['name']}",
            user_id=TEST_USER_ID,
            github_owner="owner",
            github_repo="repo",
            integration_branch="main",
            build_config=config,
        )
        assert has_detectable_tests(project) is layout["detectable"]

        if not layout["detectable"]:
            assert not any(
                str(item.get("test_cmd") or "").strip() for item in directories
            )
            return

        # Multi-manifest and root+nested layouts make the two no-CI detectors
        # diverge in the empty-config auto-detection fallback, and capturing that
        # difference explicitly is the point of these fixtures. The onboarding
        # (backend) detector walks and catalogs EVERY directory additively: it
        # emits two runners for one polyglot directory (e.g. a root carrying both
        # pytest.ini and a package.json with scripts.test) AND a runner for a
        # nested package even when a root manifest exists. The merge-time AGENT
        # detector instead stops at the first detection level (a root-level
        # runner globally suppresses its nested-package scan), so it emits only
        # the single root family — see the agent-side multiset assertion in
        # agent/src/utils/test-runners.test.ts. The build-config-mediated path
        # (a populated stored config) runs every configured directory identically
        # on both sides; this divergence is confined to auto-detection. Assert a
        # sorted MULTISET of onboarding families (rather than the old "exactly one
        # runner") so the fixture can express the difference and it can never
        # silently widen. Single-manifest layouts fall back to [family].
        expected_families = sorted(layout.get("onboard_families") or [layout["family"]])
        actual_families = sorted(
            _backend_family_for_directory(d)
            for d in directories
            if str(d.get("test_cmd") or "").strip()
        )
        assert actual_families == expected_families

        # Strengthened parity: for each non-skip family, assert the backend's
        # detected test_cmd matches the shared commands fixture — so trailing-
        # argument drift (e.g. a flag added only on the agent side) is caught here.
        actual_cmd_by_family: dict[str, str] = {}
        for d in directories:
            cmd = str(d.get("test_cmd") or "").strip()
            if cmd:
                fam = _backend_family_for_directory(d)
                if fam:
                    actual_cmd_by_family[fam] = cmd

        for family in expected_families:
            if family in _BACKEND_COMMAND_SKIP_FAMILIES:
                continue
            assert family in TEST_RUNNER_COMMANDS, (
                f"commands fixture has no entry for detectable family '{family}'"
            )
            assert family in actual_cmd_by_family, (
                f"no directory detected for expected family '{family}'"
            )
            assert actual_cmd_by_family[family] == TEST_RUNNER_COMMANDS[family], (
                f"backend emits '{actual_cmd_by_family[family]}' for family '{family}', "
                f"but commands fixture says '{TEST_RUNNER_COMMANDS[family]}'"
            )

    def test_detector_parity_extra_family_table(self):
        """Every detectable non-skip onboard family has a non-empty entry in the
        shared commands fixture — backend analog to the agent's coverage guard.

        Fails if a new ecosystem lands in the shared layouts but its command is
        missing from test_runner_commands.json, which would silently degrade the
        per-layout full-command assertion in test_shared_layout_parity above.
        """
        detectable_families: set[str] = set()
        for layout in TEST_RUNNER_LAYOUTS:
            if not layout.get("detectable"):
                continue
            for family in layout.get("onboard_families") or [layout.get("family")]:
                if family and family not in _BACKEND_COMMAND_SKIP_FAMILIES:
                    detectable_families.add(family)

        missing = [
            f
            for f in sorted(detectable_families)
            if not TEST_RUNNER_COMMANDS.get(f) or not TEST_RUNNER_COMMANDS[f].strip()
        ]
        assert missing == [], (
            f"onboard families missing from commands fixture: {missing}"
        )

    @pytest.mark.asyncio
    async def test_onboard_detects_swiftpm_test_command(self):
        """Onboarding detector emits 'swift test' for a Package.swift layout."""
        from app.ci_resolution import has_detectable_tests

        layout = {
            "files": [{"path": "Package.swift"}],
        }
        with patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            side_effect=_make_layout_github_side_effect(layout),
        ):
            config = await _detect_build_config("fake-token", "owner", "repo")

        directories = config.get("directories") or []
        assert any(d.get("test_cmd") == "swift test" for d in directories), (
            f"expected 'swift test' in directories, got: {directories}"
        )
        project = Project(
            name="swift-pm-test",
            user_id=TEST_USER_ID,
            github_owner="owner",
            github_repo="repo",
            integration_branch="main",
            build_config=config,
        )
        assert has_detectable_tests(project) is True


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
            build_config={
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
            },
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
            build_config={
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
            },
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

    @pytest.mark.asyncio
    async def test_maybe_refresh_empty_build_config_honors_cooldown(self, db_session):
        """Second call within the 6h cooldown window skips detection entirely."""
        import app.build_config_detection as bcd
        from app.build_config_detection import maybe_refresh_empty_build_config

        project = Project(
            name="Cooldown test",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="cooldown-repo",
            integration_branch="main",
            build_config={
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
            },
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        bcd._redetect_empty_cooldown.clear()

        detected = {
            "repo_is_empty": False,
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
        }
        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value=detected,
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            # First call refreshes and stamps the cooldown.
            r1 = await maybe_refresh_empty_build_config(project, db_session)
            assert r1 is True
            assert mock_detect.call_count == 1

            # Simulate repo_is_empty still True so guard doesn't skip by itself.
            project.build_config = {
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
            }
            await db_session.commit()

            # Second call within the cooldown window must skip detection.
            r2 = await maybe_refresh_empty_build_config(project, db_session)
            assert r2 is False
            assert mock_detect.call_count == 1, (
                "_detect_build_config must NOT be called on the second call within cooldown"
            )

        bcd._redetect_empty_cooldown.clear()


class TestMaybeRedetectOnEmptinessDisagreement:
    """Bidirectional re-detect when agent-observed emptiness disagrees with the
    durable ``build_config.repo_is_empty`` signal in EITHER direction."""

    @staticmethod
    def _project(repo, build_config):
        return Project(
            name=repo,
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo=repo,
            integration_branch="develop",
            build_config=build_config,
        )

    @pytest.mark.asyncio
    async def test_durable_empty_agent_nonempty_redetects_and_flips(self, db_session):
        """Direction A: durable repo_is_empty=True but agent sees real source =>
        re-detect and persist the fresh (non-empty) verdict."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "greenfield-gained-code",
            {"language": "python", "directories": [], "repo_is_empty": True},
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
                    "source_file_count": 42,
                },
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            redetected = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=False
            )

        assert redetected is True
        mock_detect.assert_awaited_once()
        assert mock_detect.await_args.kwargs.get("ref") == "develop"
        await db_session.refresh(project)
        bc = project.build_config or {}
        assert bc.get("repo_is_empty") is False
        assert bc.get("source_file_count") == 42
        bcd._redetect_disagreement_cooldown.clear()

    @pytest.mark.asyncio
    async def test_durable_nonempty_agent_empty_redetects_and_flips(self, db_session):
        """Direction B: durable repo_is_empty=False but agent sees an empty tree =>
        re-detect; the authoritative walk confirms empty and the signal flips."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "misclassified-nonempty",
            {"language": "python", "directories": [], "repo_is_empty": False},
        )
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={
                    "language": None,
                    "directories": [],
                    "repo_is_empty": True,
                    "source_file_count": 0,
                },
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            redetected = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=True
            )

        assert redetected is True
        mock_detect.assert_awaited_once()
        await db_session.refresh(project)
        assert (project.build_config or {}).get("repo_is_empty") is True
        bcd._redetect_disagreement_cooldown.clear()

    @pytest.mark.asyncio
    async def test_signals_agree_is_noop(self, db_session):
        """Durable and observed agree (both non-empty) => no GitHub call."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "agree-repo",
            {"language": "python", "directories": [], "repo_is_empty": False},
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
            redetected = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=False
            )

        assert redetected is False
        mock_detect.assert_not_awaited()
        mock_token.assert_not_awaited()
        bcd._redetect_disagreement_cooldown.clear()

    @pytest.mark.asyncio
    async def test_absent_durable_signal_is_noop(self, db_session):
        """An indeterminate (absent/None) durable signal has nothing to disagree
        with => no GitHub call (that case belongs to maybe_refresh)."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "indeterminate-repo",
            {"language": "python", "directories": []},  # no repo_is_empty key
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
            redetected = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=True
            )

        assert redetected is False
        mock_detect.assert_not_awaited()
        mock_token.assert_not_awaited()
        bcd._redetect_disagreement_cooldown.clear()

    @pytest.mark.asyncio
    async def test_honors_cooldown(self, db_session):
        """A second disagreement within the 6h window skips detection entirely."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "disagreement-cooldown",
            {"language": "python", "directories": [], "repo_is_empty": True},
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
            r1 = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=False
            )
            assert r1 is True
            assert mock_detect.call_count == 1

            # Re-arm the disagreement so only the cooldown can skip it.
            project.build_config = {
                "language": "python",
                "directories": [],
                "repo_is_empty": True,
            }
            await db_session.commit()

            r2 = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=False
            )
            assert r2 is False
            assert mock_detect.call_count == 1
        bcd._redetect_disagreement_cooldown.clear()

    @pytest.mark.asyncio
    async def test_swallows_detection_exception(self, db_session):
        """A detection failure is logged and swallowed, never raised."""
        import app.build_config_detection as bcd
        from app.build_config_detection import (
            maybe_redetect_on_emptiness_disagreement,
        )

        bcd._redetect_disagreement_cooldown.clear()
        project = self._project(
            "disagreement-boom",
            {"language": "python", "directories": [], "repo_is_empty": True},
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
            redetected = await maybe_redetect_on_emptiness_disagreement(
                project, db_session, observed_empty=False
            )

        assert redetected is False
        await db_session.refresh(project)
        # Config untouched on failure.
        assert (project.build_config or {}).get("repo_is_empty") is True
        bcd._redetect_disagreement_cooldown.clear()


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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
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
        project_id = await self._onboard(client, "empty-onboard", detect_return={})
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
    """Unit tests for the pure _classify_repo_emptiness helper.

    The helper now returns ``(is_empty, source_file_count)``; the count is the
    authoritative real-source total persisted for the agent to compare against.
    """

    def test_truly_empty_repo(self):
        assert _classify_repo_emptiness([]) == (True, 0)

    def test_readme_and_license_only(self):
        items = [
            {"name": "README.md", "type": "file"},
            {"name": "LICENSE", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 0)

    def test_ci_and_config_dotfiles_only(self):
        items = [
            {"name": ".github", "type": "dir"},
            {"name": ".gitignore", "type": "file"},
            {"name": ".editorconfig", "type": "file"},
            {"name": "CONTRIBUTING.md", "type": "file"},
            {"name": "CODE_OF_CONDUCT.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 0)

    def test_source_directory_with_one_file_is_still_empty_below_threshold(self):
        items = [
            {"name": "README.md", "type": "file"},
            {"name": "src", "type": "dir"},
            {"name": "main.py", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 1)

    def test_source_manifest_is_empty_below_threshold(self):
        items = [
            {"name": "pyproject.toml", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 0)

    def test_threshold_source_file_count_is_still_empty(self):
        items = [
            {"name": f"src/file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD)
        ]
        empty, count = _classify_repo_emptiness(items)
        assert empty is True
        assert count == GREENFIELD_SOURCE_FILE_THRESHOLD

    def test_over_threshold_source_file_count_is_non_empty(self):
        items = [
            {"name": f"src/file{i}.py", "type": "file"}
            for i in range(GREENFIELD_SOURCE_FILE_THRESHOLD + 1)
        ]
        empty, count = _classify_repo_emptiness(items)
        assert empty is False
        # The full count is reported even past the emptiness threshold.
        assert count == GREENFIELD_SOURCE_FILE_THRESHOLD + 1

    def test_git_dir_ignored(self):
        items = [
            {"name": ".git", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 0)

    def test_malformed_items_skipped(self):
        items = [
            "not-a-dict",
            {"type": "file"},  # no name
            {"name": "", "type": "file"},
            {"name": "README.md", "type": "file"},
        ]
        assert _classify_repo_emptiness(items) == (True, 0)

    def test_incomplete_listing_is_undetermined_not_asserted_non_empty(self):
        """An incomplete read is undetermined (None), never an asserted False.

        Tri-state parity with ``has_ci``: emptiness is never inferred from a
        partial walk. A transient subdir fetch failure must not stamp a durable,
        authoritative "not empty" — the greenfield decision is deferred (None)
        and no count is asserted.
        """
        assert _classify_repo_emptiness([], listing_complete=False) == (None, None)

    def test_incomplete_listing_overrides_below_threshold(self):
        """Below-threshold source + incomplete read => undetermined, not False."""
        items = [{"name": "README.md", "type": "file"}]
        assert _classify_repo_emptiness(items, listing_complete=False) == (None, None)


class TestOnboardRepoBootstrapSpawn:
    """Verify that onboarding auto-spawns repo_bootstrap for eligible projects."""

    @pytest.mark.asyncio
    async def test_non_empty_external_repo_spawns_bootstrap(
        self, client: AsyncClient, db_session
    ):
        """A non-self-repo, non-empty project onboard spawns a repo_bootstrap task."""
        from sqlalchemy import select as _select
        from app.models import Task, TaskStatus

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "python", "repo_is_empty": False},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "ext", "github_repo": "bootstrap-check"},
            )

        assert resp.status_code == 200
        project_id = resp.json()["project_id"]
        project = await db_session.get(Project, project_id)
        assert project is not None
        assert project.autonomous_enabled is False

        tasks = (
            (
                await db_session.execute(
                    _select(Task).where(
                        Task.project_id == project.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        bootstrap_tasks = [
            t for t in tasks if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 1, (
            f"Expected 1 repo_bootstrap task, got {len(bootstrap_tasks)}"
        )
        bt = bootstrap_tasks[0]
        assert bt.status == TaskStatus.QUEUED
        assert bt.first_payload["github_owner"] == "ext"
        assert bt.first_payload["github_repo"] == "bootstrap-check"
        assert bt.first_payload["integration_branch"] == "main"

    @pytest.mark.asyncio
    async def test_empty_repo_skips_bootstrap(self, client: AsyncClient, db_session):
        """A repo_is_empty=True project onboard does NOT spawn a repo_bootstrap task."""
        from sqlalchemy import select as _select
        from app.models import Task

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"repo_is_empty": True},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "ext", "github_repo": "empty-bootstrap"},
            )

        assert resp.status_code == 200
        project_id = resp.json()["project_id"]
        project = await db_session.get(Project, project_id)
        tasks = (
            (
                await db_session.execute(
                    _select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        bootstrap_tasks = [
            t for t in tasks if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 0, (
            "repo_is_empty project must not spawn a repo_bootstrap task"
        )


class TestOnboardWarmPool:
    """Tests for warm_pool_target_size opt-in at onboarding time."""

    def _common_patches(self):
        return (
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        )

    @pytest.mark.asyncio
    async def test_onboard_with_warm_pool_target_size_calls_enable(
        self, client: AsyncClient, db_session
    ):
        """Passing warm_pool_target_size enables the pool and seeds the project."""
        apply_calls = []
        seed_calls = []

        async def _fake_apply(payload, session, config):
            apply_calls.append(payload)

        async def _fake_seed(session, project_id, user_id):
            seed_calls.append({"project_id": str(project_id), "user_id": user_id})
            return {
                "project_id": str(project_id),
                "seeded": 3,
                "recycled": 0,
                "drained": 0,
            }

        fake_lock = MagicMock()
        fake_lock.__aenter__ = AsyncMock(return_value=None)
        fake_lock.__aexit__ = AsyncMock(return_value=False)

        p1, p2, p3, p4 = self._common_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("app.routers.admin_settings._apply_settings", _fake_apply),
            patch("app.routers.admin_warm_pool._seed_project_pool", _fake_seed),
            patch("app.warm_pool.warm_pool_project_scope", return_value=set()),
            patch("app.warm_pool.warm_pool_project_target_map", return_value={}),
            patch("app.shared_state.runtime_config", MagicMock()),
            patch("app.shared_state.runtime_config_lock", fake_lock),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "stsbot2",
                    "warm_pool_target_size": 3,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "project_id" in data

        # _apply_settings was called with correct warm-pool keys
        assert len(apply_calls) == 1
        applied = apply_calls[0]
        assert applied["warm_pool_enabled"] is True
        assert applied["warm_pool_target_size_by_project"][data["project_id"]] == 3

        # _seed_project_pool was called for the new project
        assert len(seed_calls) == 1
        assert seed_calls[0]["project_id"] == data["project_id"]

        # Response carries warm_pool summary
        assert data["warm_pool"]["enabled"] is True
        assert data["warm_pool"]["target_size"] == 3
        assert data["warm_pool"]["seeded"] == 3

    @pytest.mark.asyncio
    async def test_onboard_without_warm_pool_target_size_skips_pool(
        self, client: AsyncClient, db_session
    ):
        """Omitting warm_pool_target_size leaves warm-pool state untouched."""
        seed_calls = []

        async def _fake_seed(session, project_id, user_id):
            seed_calls.append(project_id)
            return {}

        p1, p2, p3, p4 = self._common_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("app.routers.admin_warm_pool._seed_project_pool", _fake_seed),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "no-pool"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "warm_pool" not in data
        assert len(seed_calls) == 0

    @pytest.mark.asyncio
    async def test_onboard_preview_only_skips_warm_pool(
        self, client: AsyncClient, db_session
    ):
        """preview_only=True with warm_pool_target_size does NOT call the enable path."""
        apply_calls = []

        async def _fake_apply(payload, session, config):
            apply_calls.append(payload)

        p1, p2, p3, p4 = self._common_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("app.routers.admin_settings._apply_settings", _fake_apply),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "preview-pool",
                    "warm_pool_target_size": 2,
                    "preview_only": True,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "warm_pool" not in data
        assert len(apply_calls) == 0


class TestOnboardRunnerBindings:
    """A newly onboarded project must be runnable by the fleet its owner has.

    Live defect this covers: onboarding created a project bound to ZERO local
    runners, so the very first task it spawned sat QUEUED forever behind a
    "waiting for local capacity" message while two healthy idle runners existed.
    The only way out was a human POSTing a runner_project_binding grant.
    """

    def _common_patches(self):
        return (
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
                "app.github.ensure_repo_initialized",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        )

    async def _make_runner(self, db_session, project_id, status="active"):
        import uuid as _uuid

        from app.models import LocalRunner, _utcnow

        runner = LocalRunner(
            project_id=project_id,
            token_hash=_uuid.uuid4().hex,
            status=status,
            last_seen_at=_utcnow(),
            capabilities={"task_types": "all", "max_concurrent": 2},
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)
        return runner

    async def _make_project(self, db_session, user_id, name):
        project = Project(name=name, user_id=user_id)
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        return project

    async def _bound_runner_ids(self, db_session, project_id):
        import uuid as _uuid

        from sqlmodel import select as _select

        from app.models import RunnerProjectBinding

        rows = await db_session.execute(
            _select(RunnerProjectBinding.runner_id).where(
                RunnerProjectBinding.project_id == _uuid.UUID(str(project_id))
            )
        )
        return {str(rid) for rid in rows.scalars().all()}

    @pytest.mark.asyncio
    async def test_onboard_grants_owner_existing_runners_to_new_project(
        self, client: AsyncClient, db_session
    ):
        """Runners already enrolled under the caller account are bound."""
        owner_project = await self._make_project(
            db_session, TEST_USER_ID, "First project of the owner"
        )
        runner_a = await self._make_runner(db_session, owner_project.id)
        runner_b = await self._make_runner(db_session, owner_project.id)

        p1, p2, p3, p4 = self._common_patches()
        with p1, p2, p3, p4:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "bind-me"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        new_project_id = data["project_id"]

        granted = set(data["runner_bindings"]["granted_runner_ids"])
        assert granted == {str(runner_a.id), str(runner_b.id)}
        assert data["runner_bindings"]["warnings"] == []

        # The durable rows, not just the response.
        assert await self._bound_runner_ids(db_session, new_project_id) == {
            str(runner_a.id),
            str(runner_b.id),
        }

        # And the project is therefore no longer structurally undispatchable.
        import uuid as _uuid

        from app.runner_project_binding import count_runners_allowed_for_project

        assert (
            await count_runners_allowed_for_project(
                db_session, _uuid.UUID(new_project_id)
            )
            == 2
        )

    @pytest.mark.asyncio
    async def test_onboard_with_no_owner_runners_succeeds_with_a_warning(
        self, client: AsyncClient, db_session
    ):
        """No fleet is not an error, but the operator must be told."""
        p1, p2, p3, p4 = self._common_patches()
        with p1, p2, p3, p4:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "no-fleet"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["runner_bindings"]["granted_runner_ids"] == []
        warnings = data["runner_bindings"]["warnings"]
        assert len(warnings) == 1
        assert "No local runner is enrolled under this account" in warnings[0]
        # The warning must name the remedy, not merely the state.
        assert (
            "POST /api/admin/projects/{enrollment_project_id}/runners/"
            "{runner_id}/projects" in warnings[0]
        )
        assert await self._bound_runner_ids(db_session, data["project_id"]) == set()

    @pytest.mark.asyncio
    async def test_onboard_never_grants_a_runner_owned_by_another_account(
        self, client: AsyncClient, db_session
    ):
        """SECURITY PIN: cross-tenant auto-grant must never happen.

        A runner enrolled on a project belonging to a DIFFERENT ``user_id`` is
        not the hardware of the onboarding caller. Auto-granting it would
        silently hand another account machine to this project. The literal
        assertion below (the foreign runner id is absent from the binding
        table) is the boundary; do not relax it to a count or a subset check.
        """
        other_owner_project = await self._make_project(
            db_session, "some-other-user-999", "Project of a different account"
        )
        foreign_runner = await self._make_runner(db_session, other_owner_project.id)

        mine_project = await self._make_project(
            db_session, TEST_USER_ID, "Existing project of the caller"
        )
        my_runner = await self._make_runner(db_session, mine_project.id)

        p1, p2, p3, p4 = self._common_patches()
        with p1, p2, p3, p4:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "tenant-boundary"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        granted = set(data["runner_bindings"]["granted_runner_ids"])

        assert str(foreign_runner.id) not in granted
        assert granted == {str(my_runner.id)}

        bound = await self._bound_runner_ids(db_session, data["project_id"])
        assert str(foreign_runner.id) not in bound
        assert bound == {str(my_runner.id)}

    @pytest.mark.asyncio
    async def test_onboard_survives_a_failing_grant_and_reports_it(
        self, client: AsyncClient, db_session
    ):
        """A binding write failure warns; it never fails onboarding."""
        owner_project = await self._make_project(
            db_session, TEST_USER_ID, "Owner project for failure path"
        )
        await self._make_runner(db_session, owner_project.id)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("binding table unavailable")

        p1, p2, p3, p4 = self._common_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("app.runner_project_binding.add_binding", _boom),
        ):
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "grant-fails"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "project_id" in data
        assert data["runner_bindings"]["granted_runner_ids"] == []
        assert any(
            "binding table unavailable" in w
            for w in data["runner_bindings"]["warnings"]
        )

    @pytest.mark.asyncio
    async def test_only_a_real_onboard_binds_runners_not_a_preview(
        self, client: AsyncClient, db_session
    ):
        """preview_only writes nothing; the same call for real does bind.

        Both halves are asserted in one test on purpose: an "absent key" check
        alone would pass just as happily against the unfixed code, proving
        nothing. The contrast is the assertion.
        """
        owner_project = await self._make_project(
            db_session, TEST_USER_ID, "Owner project for preview"
        )
        runner = await self._make_runner(db_session, owner_project.id)

        p1, p2, p3, p4 = self._common_patches()
        with p1, p2, p3, p4:
            preview = await client.post(
                "/api/admin/projects/onboard",
                json={
                    "github_owner": "acme",
                    "github_repo": "preview-bind",
                    "preview_only": True,
                },
            )

        assert preview.status_code == 200, preview.text
        assert "runner_bindings" not in preview.json()

        p1, p2, p3, p4 = self._common_patches()
        with p1, p2, p3, p4:
            real = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "preview-bind"},
            )

        assert real.status_code == 200, real.text
        data = real.json()
        assert data["runner_bindings"]["granted_runner_ids"] == [str(runner.id)]
        assert await self._bound_runner_ids(db_session, data["project_id"]) == {
            str(runner.id)
        }
