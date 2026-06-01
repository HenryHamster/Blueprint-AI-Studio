"""Unit tests for app.shared_state helpers."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import AlertSeverity
import app.shared_state as _shared_state
from app.shared_state import (
    TASK_TYPE_BRANCH_PREFIX,
    backfill_branch_columns,
    derive_task_branch,
    resolve_task_branch,
)


def _task(
    *,
    task_id="00000000-0000-0000-0000-0000000000aa",
    payloads=None,
    context=None,
):
    """Minimal duck-typed task with the attributes resolve_task_branch reads."""
    if payloads is None:
        payloads = [{"type": "create"}]
    first_payload = payloads[0] if payloads else None
    return SimpleNamespace(
        id=task_id,
        payloads=payloads,
        first_payload=first_payload,
        context=context or {},
    )


class TestDeriveTaskBranch:
    def test_unknown_type_falls_back_to_task_prefix(self):
        # The fallback prefix protects against future task types not in the map.
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "novel-type")
        assert b == "blueprint/task-11111111"

    def test_refactor_uses_refactor_prefix(self):
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "refactor")
        assert b == "blueprint/refactor-11111111"

    def test_fix_uses_task_prefix(self):
        # Fix tasks reuse the parent branch in real runs, but if synthesized
        # they go under `task-` like create/qa per the agent's branch map.
        b = derive_task_branch("11111111-2222-3333-4444-555555555555", "fix")
        assert b == "blueprint/task-11111111"

    def test_prefix_map_covers_known_types(self):
        # Guard against accidental drift from agent/handlers -- these prefixes
        # must remain in sync with the agent's per-type branch derivation.
        for t in ("create", "qa", "fix", "refactor", "docs", "deps", "migrate"):
            assert t in TASK_TYPE_BRANCH_PREFIX


class TestResolveTaskBranch:
    def test_review_loop_branch_wins_over_payload_and_context(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={
                "branch": "blueprint/ctx-branch",
                "review_loop": {"branch": "blueprint/review-loop-branch"},
            },
        )
        assert resolve_task_branch(t) == "blueprint/review-loop-branch"

    def test_payload_branch_wins_over_context_when_review_loop_missing(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"

    def test_context_branch_used_when_review_loop_and_payload_missing(self):
        t = _task(
            payloads=[{"type": "fix"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t) == "blueprint/ctx-branch"

    def test_review_loop_empty_branch_falls_through_to_payload(self):
        # An empty string or None in review_loop.branch must not block the
        # fallback chain -- those are sentinel "unset" values.
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": None}},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": ""}},
        )
        assert resolve_task_branch(t) == "blueprint/payload-branch"

    def test_no_branch_anywhere_falls_back_to_derive(self):
        # The synthesized branch must use the task's payload type, not the
        # default "task" prefix -- refactor children push to refactor-*.
        t = _task(
            task_id="aabbccdd-1111-2222-3333-444444444444",
            payloads=[{"type": "refactor"}],
            context={},
        )
        assert resolve_task_branch(t) == "blueprint/refactor-aabbccdd"

    def test_none_context_is_safe(self):
        t = _task(payloads=[{"type": "create"}], context=None)
        # No context, no payload branch -> derived.
        expected = derive_task_branch(t.id, "create")
        assert resolve_task_branch(t) == expected

    def test_fix_task_with_parent_branch_in_review_loop_returns_parent_branch(self):
        # Regression -- fix tasks reuse the parent's PR branch. The branch
        # carried in review_loop.branch is the PR's HEAD ref the parent
        # agent pushed; synthesizing blueprint/task-{fix_id} would yield a
        # name no agent has ever pushed, breaking the next review's clone.
        t = _task(
            task_id="ffffffff-0000-0000-0000-000000000000",
            payloads=[{"type": "fix"}],
            context={"review_loop": {"branch": "blueprint/task-abc12345"}},
        )
        out = resolve_task_branch(t)
        assert out == "blueprint/task-abc12345"
        # Critically: not the fix task's own synthesized branch.
        assert out != "blueprint/task-ffffffff"


def _project(
    *,
    project_id=None,
    integration_branch=None,
    promotion_target=None,
    is_self_repo=False,
    github_owner="acme",
    github_repo="myrepo",
):
    """Minimal duck-typed project for backfill_branch_columns tests."""
    return SimpleNamespace(
        id=project_id or uuid.uuid4(),
        integration_branch=integration_branch,
        promotion_target=promotion_target,
        is_self_repo=is_self_repo,
        github_owner=github_owner,
        github_repo=github_repo,
    )


def _make_session(projects):
    """Return a mock async session that yields the given projects list on execute."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = list(projects)
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    return session


class TestBackfillBranchColumns:
    @pytest.fixture(autouse=True)
    def _stub_flag_modified(self):
        # flag_modified() is a SQLAlchemy ORM marker; the test fixtures here
        # use SimpleNamespace stand-ins which lack the _sa_instance_state slot
        # the real call needs.  We only care that the attribute on the project
        # was updated, not that SA's dirty-tracking observed it.
        # backfill_branch_columns now lives in app.branch_resolution (shared_state
        # re-exports it); patch the real definition site.
        with patch("app.branch_resolution.flag_modified") as m:
            yield m

    @pytest.mark.asyncio
    async def test_skips_fully_populated_projects(self):
        """Projects with both columns set are counted as skipped."""
        projects = [
            _project(integration_branch="main", promotion_target="main"),
            _project(integration_branch="develop", promotion_target="main"),
        ]
        session = _make_session(projects)

        with (
            patch("app.release_promotion._resolve_github_with_session") as mock_resolve,
            patch("app.github.get_repo_default_branch") as mock_default,
        ):
            summary = await backfill_branch_columns(session)

        assert summary["skipped"] == 2
        assert summary["filled"] == 0
        assert summary["errored"] == 0
        mock_resolve.assert_not_called()
        mock_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_populates_null_integration_branch_from_github(self):
        """NULL integration_branch gets set to GitHub default branch."""
        project = _project(integration_branch=None, promotion_target="main")
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=("tok", "acme", "myrepo"),
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="develop",
            ),
        ):
            summary = await backfill_branch_columns(session)

        assert project.integration_branch == "develop"
        assert summary["filled"] == 1
        assert summary["errored"] == 0

    @pytest.mark.asyncio
    async def test_populates_null_promotion_target_from_github(self):
        """NULL promotion_target gets set to GitHub default branch."""
        project = _project(integration_branch="develop", promotion_target=None)
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=("tok", "acme", "myrepo"),
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="develop",
            ),
        ):
            summary = await backfill_branch_columns(session)

        assert project.promotion_target == "develop"
        assert summary["filled"] == 1

    @pytest.mark.asyncio
    async def test_self_repo_integration_branch_null_is_skipped(self):
        """Self-repo projects with NULL integration_branch are left alone."""
        project = _project(
            integration_branch=None,
            promotion_target="main",
            is_self_repo=True,
        )
        session = _make_session([project])

        with (
            patch("app.release_promotion._resolve_github_with_session") as mock_resolve,
        ):
            summary = await backfill_branch_columns(session)

        assert project.integration_branch is None
        assert summary["self_repo_skipped"] == 1
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_repo_null_promotion_target_gets_main(self):
        """Self-repo with NULL promotion_target is set to 'main' without GitHub API."""
        project = _project(
            integration_branch="automain",
            promotion_target=None,
            is_self_repo=True,
        )
        session = _make_session([project])

        with (
            patch("app.release_promotion._resolve_github_with_session") as mock_resolve,
        ):
            summary = await backfill_branch_columns(session)

        assert project.promotion_target == "main"
        assert summary["filled"] == 1
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_api_failure_emits_alert_and_does_not_fill(self):
        """When GitHub API fails, an Alert is emitted and columns remain NULL."""
        project = _project(integration_branch=None, promotion_target=None)
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                side_effect=Exception("network error"),
            ),
        ):
            summary = await backfill_branch_columns(session)

        assert project.integration_branch is None
        assert project.promotion_target is None
        assert summary["errored"] == 1
        assert summary["filled"] == 0
        # An Alert should have been created.
        session.add.assert_called()

    @pytest.mark.asyncio
    async def test_no_credentials_emits_alert(self):
        """When _resolve_github_with_session returns None, an Alert is emitted."""
        project = _project(integration_branch=None, promotion_target="main")
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            summary = await backfill_branch_columns(session)

        assert project.integration_branch is None
        assert summary["errored"] == 1
        session.add.assert_called()

    @pytest.mark.asyncio
    async def test_already_populated_rows_are_not_overwritten(self):
        """Only NULL columns are filled; existing non-NULL values are never touched."""
        project = _project(integration_branch="trunk", promotion_target=None)
        original_integration = project.integration_branch
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=("tok", "acme", "myrepo"),
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
        ):
            summary = await backfill_branch_columns(session)

        assert project.integration_branch == original_integration
        assert project.promotion_target == "main"
        assert summary["filled"] == 1

    @pytest.mark.asyncio
    async def test_auto_create_path_both_null_non_main_default(self):
        """Regression: project auto-created with both columns NULL, GitHub default != 'main'.

        Simulates the admin create-task and deploy-endpoint paths that create projects
        without initialising branch columns. After backfill, both integration_branch and
        promotion_target must reflect the repo's actual default (not silently resolve to
        'main'), and the branch resolvers must return the same non-'main' value.
        """
        project = _project(integration_branch=None, promotion_target=None)
        session = _make_session([project])

        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=("tok", "acme", "myrepo"),
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="develop",
            ),
        ):
            summary = await backfill_branch_columns(session, project_id=project.id)

        assert project.integration_branch == "develop"
        assert project.promotion_target == "develop"
        assert summary["filled"] == 2
        assert summary["errored"] == 0

        # Regression guard: branch resolvers must not fall back to 'main' once
        # the columns are populated by the backfill called from the auto-create path.
        assert _shared_state.get_project_integration_branch(project) == "develop"
        assert _shared_state.get_project_promotion_target(project) == "develop"


class TestProjectBranchResolvers:
    @pytest.fixture(autouse=True)
    def _reset_missing_branch_alerts(self):
        import app.shared_state as shared_state

        shared_state._missing_project_branch_alerts.clear()
        yield
        shared_state._missing_project_branch_alerts.clear()

    def test_populated_branches_return_values_without_alerts(self):
        project = _project(
            integration_branch="develop",
            promotion_target="release",
        )
        session = MagicMock()

        with (
            patch(
                "app.branch_resolution.object_session", return_value=session
            ) as mock_session,
            patch("app.branch_resolution.logger.warning") as mock_warning,
        ):
            assert _shared_state.get_project_integration_branch(project) == "develop"
            assert _shared_state.get_project_promotion_target(project) == "release"

        mock_session.assert_not_called()
        mock_warning.assert_not_called()
        session.add.assert_not_called()

    def test_null_branch_emits_alert_once_per_project_and_column(self):
        project = _project(integration_branch=None, promotion_target=None)
        session = MagicMock()

        with (
            patch("app.branch_resolution.object_session", return_value=session),
            patch("app.branch_resolution.logger.warning") as mock_warning,
        ):
            assert _shared_state.get_project_integration_branch(project) == "main"
            assert _shared_state.get_project_integration_branch(project) == "main"
            assert _shared_state.get_project_promotion_target(project) == "main"
            assert _shared_state.get_project_promotion_target(project) == "main"

        assert session.add.call_count == 2
        integration_alert = session.add.call_args_list[0].args[0]
        promotion_alert = session.add.call_args_list[1].args[0]
        assert integration_alert.severity == AlertSeverity.ERROR
        assert integration_alert.title == "Missing integration_branch"
        assert promotion_alert.severity == AlertSeverity.ERROR
        assert promotion_alert.title == "Missing promotion_target"
        assert mock_warning.call_count == 2
        assert mock_warning.call_args_list[0].args[1:] == (
            str(project.id),
            "integration_branch",
        )
        assert mock_warning.call_args_list[1].args[1:] == (
            str(project.id),
            "promotion_target",
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
