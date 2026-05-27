"""Tests for backfill_task_branch_namespace in shared_state."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.shared_state import backfill_task_branch_namespace


def _project(
    *,
    project_id=None,
    github_repo="myrepo",
    is_self_repo=False,
    build_config=None,
):
    return SimpleNamespace(
        id=project_id or uuid.uuid4(),
        github_repo=github_repo,
        is_self_repo=is_self_repo,
        build_config=build_config if build_config is not None else {},
    )


def _make_session(projects):
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = list(projects)
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    return session


class TestBackfillTaskBranchNamespace:
    @pytest.fixture(autouse=True)
    def _stub_flag_modified(self):
        with patch("app.shared_state.flag_modified") as m:
            yield m

    @pytest.mark.asyncio
    async def test_derives_namespace_for_non_self_repo(self):
        """Non-self-repo project with no namespace gets a derived slug."""
        project = _project(github_repo="my-cool-repo")
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        assert summary["namespace_skipped"] == 0
        assert project.build_config["task_branch_namespace"] == "my-cool-repo"

    @pytest.mark.asyncio
    async def test_skips_project_with_existing_namespace(self):
        """Projects already having task_branch_namespace are untouched."""
        project = _project(build_config={"task_branch_namespace": "existing"})
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_skipped"] == 1
        assert summary["namespace_filled"] == 0
        assert project.build_config["task_branch_namespace"] == "existing"

    @pytest.mark.asyncio
    async def test_self_repo_always_gets_blueprint(self):
        """Self-repo projects always get 'blueprint' regardless of github_repo."""
        project = _project(github_repo="SomeOtherRepo", is_self_repo=True)
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        assert project.build_config["task_branch_namespace"] == "blueprint"

    @pytest.mark.asyncio
    async def test_blueprintaistudio_repo_slugifies_normally(self):
        """github_repo='BlueprintAIStudio' that is NOT the self-repo gets slugified namespace."""
        project = _project(github_repo="BlueprintAIStudio", is_self_repo=False)
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        assert project.build_config["task_branch_namespace"] == "blueprintaistudio"

    @pytest.mark.asyncio
    async def test_idempotent_second_call_skips(self):
        """Calling twice: second call leaves value untouched."""
        project = _project(github_repo="acme-service")
        session = _make_session([project])

        await backfill_task_branch_namespace(session)
        assert project.build_config["task_branch_namespace"] == "acme-service"

        # Second call with already-set value
        session2 = _make_session([project])
        summary2 = await backfill_task_branch_namespace(session2)

        assert summary2["namespace_skipped"] == 1
        assert summary2["namespace_filled"] == 0

    @pytest.mark.asyncio
    async def test_slug_caps_at_40_chars(self):
        """Slugified namespace is capped at 40 characters."""
        long_repo = "a" * 50
        project = _project(github_repo=long_repo)
        session = _make_session([project])

        await backfill_task_branch_namespace(session)

        assert len(project.build_config["task_branch_namespace"]) == 40

    @pytest.mark.asyncio
    async def test_preserves_existing_build_config_keys(self):
        """Backfill merges into build_config without clobbering other keys."""
        project = _project(build_config={"some_other_key": "value"})
        session = _make_session([project])

        await backfill_task_branch_namespace(session)

        assert project.build_config["some_other_key"] == "value"
        assert "task_branch_namespace" in project.build_config

    @pytest.mark.asyncio
    async def test_single_project_filter(self):
        """When project_id is given, only that project is queried."""
        target_id = uuid.uuid4()
        project = _project(project_id=target_id, github_repo="filtered-repo")
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session, project_id=target_id)

        assert summary["namespace_filled"] == 1
        # Verify the WHERE clause was applied (execute was called with a filtered query)
        session.execute.assert_called_once()
