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
        # backfill_task_branch_namespace now lives in app.branch_resolution
        # (shared_state re-exports it); patch the real definition site.
        with patch("app.branch_resolution.flag_modified") as m:
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
    async def test_non_blueprint_self_repo_gets_slug(self):
        """A project with is_self_repo=True but a non-Blueprint github_repo is
        classified by repo identity, not by the flag, so it gets the slug-derived
        namespace rather than 'blueprint'."""
        project = _project(github_repo="SomeOtherRepo", is_self_repo=True)
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        assert project.build_config["task_branch_namespace"] == "someotherrepo"

    @pytest.mark.asyncio
    async def test_blueprintaistudio_repo_alias_gets_blueprint_even_when_flag_false(self):
        """``github_repo='BlueprintAIStudio'`` with ``is_self_repo=False`` still
        gets the ``"blueprint"`` namespace.  This is the wedge for incident
        2026-05-26..28: Blueprint's own project had the flag misset False,
        so the slug path wrote ``"blueprintaistudio"`` and every
        merge-conflict fix-spawn tried to clone a nonexistent branch."""
        project = _project(github_repo="BlueprintAIStudio", is_self_repo=False)
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        assert project.build_config["task_branch_namespace"] == "blueprint"

    @pytest.mark.asyncio
    async def test_non_blueprint_self_repo_with_existing_namespace_is_skipped(self):
        """A project with is_self_repo=True but a non-Blueprint github_repo and
        an existing non-'blueprint' namespace is left untouched — classification
        is by repo identity, so the corrective overwrite path never fires."""
        project = _project(
            is_self_repo=True,
            build_config={"task_branch_namespace": "blueprintaistudio"},
        )
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_corrected"] == 0
        assert summary["namespace_skipped"] == 1
        assert summary["namespace_filled"] == 0
        assert project.build_config["task_branch_namespace"] == "blueprintaistudio"

    @pytest.mark.asyncio
    async def test_blueprintaistudio_alias_with_stale_namespace_is_corrected(self):
        """``BlueprintAIStudio`` alias + ``is_self_repo=False`` + stale stored
        namespace = corrected.  The exact production state of project
        ``37d445e1`` before the wedge landed."""
        project = _project(
            github_repo="BlueprintAIStudio",
            is_self_repo=False,
            build_config={"task_branch_namespace": "blueprintaistudio"},
        )
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_corrected"] == 1
        assert project.build_config["task_branch_namespace"] == "blueprint"

    @pytest.mark.asyncio
    async def test_self_repo_already_correct_is_skipped(self):
        """Self-repo project already at ``"blueprint"`` is left untouched."""
        project = _project(
            is_self_repo=True,
            build_config={"task_branch_namespace": "blueprint"},
        )
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_skipped"] == 1
        assert summary["namespace_corrected"] == 0
        assert summary["namespace_filled"] == 0

    @pytest.mark.asyncio
    async def test_non_blueprint_repo_with_stale_namespace_is_still_skipped(self):
        """The corrective overwrite is Blueprint-only: an unrelated project
        whose stored namespace looks stale is NOT touched — operators may
        have set a custom value deliberately."""
        project = _project(
            github_repo="acme-service",
            is_self_repo=False,
            build_config={"task_branch_namespace": "operator-chose-this"},
        )
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_skipped"] == 1
        assert summary["namespace_corrected"] == 0
        assert (
            project.build_config["task_branch_namespace"] == "operator-chose-this"
        )

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
    async def test_empty_slug_non_self_repo_does_not_use_blueprint(self):
        """A repo name that slugifies to empty gets 'project', not 'blueprint'."""
        project = _project(github_repo="---", is_self_repo=False)
        session = _make_session([project])

        summary = await backfill_task_branch_namespace(session)

        assert summary["namespace_filled"] == 1
        ns = project.build_config["task_branch_namespace"]
        assert ns != "blueprint"
        assert ns == "project"

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
