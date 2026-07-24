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
    # namespace is a REQUIRED parameter (no silent "blueprint" default); these
    # self-repo cases pass "blueprint" explicitly to preserve the asserted name.
    def test_unknown_type_falls_back_to_task_prefix(self):
        # The fallback prefix protects against future task types not in the map.
        b = derive_task_branch(
            "11111111-2222-3333-4444-555555555555", "novel-type", "blueprint"
        )
        assert b == "blueprint/task-11111111"

    def test_refactor_uses_refactor_prefix(self):
        b = derive_task_branch(
            "11111111-2222-3333-4444-555555555555", "refactor", "blueprint"
        )
        assert b == "blueprint/refactor-11111111"

    def test_fix_uses_task_prefix(self):
        # Fix tasks reuse the parent branch in real runs, but if synthesized
        # they go under `task-` like create/qa per the agent's branch map.
        b = derive_task_branch(
            "11111111-2222-3333-4444-555555555555", "fix", "blueprint"
        )
        assert b == "blueprint/task-11111111"

    def test_custom_namespace_replaces_blueprint_segment(self):
        # A non-Blueprint repo threads its own namespace; the leading segment
        # must reflect it, never silently fall back to "blueprint".
        b = derive_task_branch(
            "11111111-2222-3333-4444-555555555555", "refactor", "autoproj"
        )
        assert b == "autoproj/refactor-11111111"

    def test_prefix_map_covers_known_types(self):
        # Guard against accidental drift from agent/handlers -- these prefixes
        # must remain in sync with the agent's per-type branch derivation.
        for t in ("create", "qa", "fix", "refactor", "docs", "deps", "migrate"):
            assert t in TASK_TYPE_BRANCH_PREFIX


class TestResolveTaskBranch:
    # resolve_task_branch now takes a REQUIRED namespace, used only for the
    # last-resort synthesis branch; the recorded-branch precedence cases pass
    # "blueprint" but it is never read for them.
    def test_review_loop_branch_wins_over_payload_and_context(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={
                "branch": "blueprint/ctx-branch",
                "review_loop": {"branch": "blueprint/review-loop-branch"},
            },
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/review-loop-branch"

    def test_payload_branch_wins_over_context_when_review_loop_missing(self):
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/payload-branch"

    def test_context_branch_used_when_review_loop_and_payload_missing(self):
        t = _task(
            payloads=[{"type": "fix"}],
            context={"branch": "blueprint/ctx-branch"},
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/ctx-branch"

    def test_recorded_branch_ignores_namespace_arg(self):
        # When the task carries a real branch, the namespace argument must not
        # rewrite it -- it is only used for last-resort synthesis.
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={},
        )
        assert resolve_task_branch(t, "autoproj") == "blueprint/payload-branch"

    def test_review_loop_empty_branch_falls_through_to_payload(self):
        # An empty string or None in review_loop.branch must not block the
        # fallback chain -- those are sentinel "unset" values.
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": None}},
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/payload-branch"
        t = _task(
            payloads=[{"type": "fix", "branch": "blueprint/payload-branch"}],
            context={"review_loop": {"branch": ""}},
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/payload-branch"

    def test_no_branch_anywhere_falls_back_to_derive(self):
        # The synthesized branch must use the task's payload type, not the
        # default "task" prefix -- refactor children push to refactor-*.
        t = _task(
            task_id="aabbccdd-1111-2222-3333-444444444444",
            payloads=[{"type": "refactor"}],
            context={},
        )
        assert resolve_task_branch(t, "blueprint") == "blueprint/refactor-aabbccdd"

    def test_synthesis_uses_passed_namespace(self):
        # The last-resort synthesis must honor the caller's namespace, not the
        # canonical "blueprint" segment.
        t = _task(
            task_id="aabbccdd-1111-2222-3333-444444444444",
            payloads=[{"type": "refactor"}],
            context={},
        )
        assert resolve_task_branch(t, "autoproj") == "autoproj/refactor-aabbccdd"

    def test_none_context_is_safe(self):
        t = _task(payloads=[{"type": "create"}], context=None)
        # No context, no payload branch -> derived.
        expected = derive_task_branch(t.id, "create", "blueprint")
        assert resolve_task_branch(t, "blueprint") == expected

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
        out = resolve_task_branch(t, "blueprint")
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
        'main'). That leaves the integration branch equal to the promotion target; the
        hot-path resolver fails safe (returns the configured value rather than raising),
        and the unsafe integration==promotion landing is blocked at the merge/rollup
        boundary instead.
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

        # The hot-path resolver fails safe: it returns the configured value even
        # when integration == promotion (so read endpoints / background loops /
        # the protected_branches property never raise). Refusing to land work on
        # the promotion target is enforced at the merge/rollup boundary instead.
        assert _shared_state.get_project_integration_branch(project) == "develop"
        assert _shared_state.get_project_promotion_target(project) == "develop"


def _flush_session(existing_alerts=None):
    """Async-session double for flush_pending_branch_alerts tests.

    ``existing_alerts`` is a list of Alert-like objects (each with ``project_id``
    and ``title`` attributes) returned by ``result.scalars().all()`` to simulate
    already-persisted undismissed alerts (idempotency path).  Pass an empty list
    or omit for the no-existing-alert case.
    """
    session = AsyncMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = list(existing_alerts) if existing_alerts else []
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    return session


class TestProjectBranchResolvers:
    @pytest.fixture(autouse=True)
    def _reset_project_branch_alerts(self):
        import app.branch_resolution as branch_resolution

        branch_resolution._missing_project_branch_alerts.clear()
        branch_resolution._blocking_project_branch_alerts.clear()
        branch_resolution._pending_durable_branch_alerts.clear()
        yield
        branch_resolution._missing_project_branch_alerts.clear()
        branch_resolution._blocking_project_branch_alerts.clear()
        branch_resolution._pending_durable_branch_alerts.clear()

    def test_populated_branches_return_values_without_alerts(self):
        import app.branch_resolution as branch_resolution

        project = _project(
            integration_branch="develop",
            promotion_target="release",
        )

        with patch("app.branch_resolution.logger.warning") as mock_warning:
            assert _shared_state.get_project_integration_branch(project) == "develop"
            assert _shared_state.get_project_promotion_target(project) == "release"

        mock_warning.assert_not_called()
        # A populated column never enqueues a deferred durable alert.
        assert branch_resolution._pending_durable_branch_alerts == []

    def test_self_repo_null_integration_resolves_to_automain_with_single_alert(self):
        import app.branch_resolution as branch_resolution

        project = _project(
            integration_branch=None,
            promotion_target=None,
            is_self_repo=True,
            github_repo="BlueprintAIStudio",
        )

        assert _shared_state.get_project_integration_branch(project) == "automain"
        assert _shared_state.get_project_integration_branch(project) == "automain"
        assert _shared_state.get_project_promotion_target(project) == "main"
        assert _shared_state.get_project_promotion_target(project) == "main"

        # The synchronous resolver records intent (no inline DB I/O) — one
        # enqueue per missing column, deduped across repeated calls.
        pending = branch_resolution._pending_durable_branch_alerts
        assert len(pending) == 2
        assert pending[0]["project_id"] == project.id
        assert pending[0]["title"] == "Missing integration_branch"
        assert pending[0]["severity"] == "ERROR"
        assert pending[1]["title"] == "Missing promotion_target"
        assert pending[1]["severity"] == "ERROR"

    def test_null_branch_falls_back_to_main_once_per_project_when_non_self_repo(self):
        import app.branch_resolution as branch_resolution

        project = _project(integration_branch=None, promotion_target=None)

        # Fails safe to "main" for a non-self repo (never raises), so callers
        # in read endpoints / background loops never abort on a NULL column.
        assert _shared_state.get_project_integration_branch(project) == "main"
        assert _shared_state.get_project_integration_branch(project) == "main"
        assert _shared_state.get_project_promotion_target(project) == "main"
        assert _shared_state.get_project_promotion_target(project) == "main"

        # One enqueue per column, deduped across repeated calls.
        pending = branch_resolution._pending_durable_branch_alerts
        assert len(pending) == 2
        assert pending[0]["title"] == "Missing integration_branch"
        assert pending[1]["title"] == "Missing promotion_target"

    @pytest.mark.asyncio
    async def test_flush_pending_branch_alerts_persists_once(self):
        import app.branch_resolution as branch_resolution

        project = _project(integration_branch=None, promotion_target=None)
        _shared_state.get_project_integration_branch(project)
        _shared_state.get_project_promotion_target(project)
        assert len(branch_resolution._pending_durable_branch_alerts) == 2

        session = _flush_session()
        with patch(
            "app.alert_policy.create_project_alert_if_allowed",
            new=AsyncMock(return_value=(SimpleNamespace(), True, None)),
        ) as mock_policy:
            created = await branch_resolution.flush_pending_branch_alerts(session)

        assert created == 2
        session.add.assert_not_called()
        # Queue drained on success.
        assert branch_resolution._pending_durable_branch_alerts == []
        assert mock_policy.await_count == 2
        titles = {call.kwargs["title"] for call in mock_policy.await_args_list}
        assert titles == {"Missing integration_branch", "Missing promotion_target"}
        for call in mock_policy.await_args_list:
            assert call.kwargs["severity"] == AlertSeverity.ERROR
            assert call.kwargs["broadcast"] is True

    @pytest.mark.asyncio
    async def test_flush_is_idempotent_across_simulated_restart(self):
        import app.branch_resolution as branch_resolution

        project = _project(integration_branch=None, promotion_target=None)
        _shared_state.get_project_integration_branch(project)
        _shared_state.get_project_promotion_target(project)

        # First flush persists both alerts.
        with patch(
            "app.alert_policy.create_project_alert_if_allowed",
            new=AsyncMock(return_value=(SimpleNamespace(), True, None)),
        ):
            assert (
                await branch_resolution.flush_pending_branch_alerts(
                    _flush_session()
                )
                == 2
            )

        # Simulate a restart: the in-memory dedupe set forgets, so the resolver
        # re-enqueues on the next encounter.
        branch_resolution._missing_project_branch_alerts.clear()
        _shared_state.get_project_integration_branch(project)
        _shared_state.get_project_promotion_target(project)
        assert len(branch_resolution._pending_durable_branch_alerts) == 2

        # But the durable SELECT finds both existing undismissed alerts, so no
        # second rows are created — the signal stays one-per-misconfiguration.
        # The mock must carry matching (project_id, title) so the batched dedup
        # check in flush_pending_branch_alerts rejects both re-creates.
        existing_int = SimpleNamespace(
            project_id=project.id, title="Missing integration_branch"
        )
        existing_promo = SimpleNamespace(
            project_id=project.id, title="Missing promotion_target"
        )
        session = _flush_session(existing_alerts=[existing_int, existing_promo])
        with patch(
            "app.alert_policy.create_project_alert_if_allowed",
            new=AsyncMock(),
        ) as second_policy:
            created = await branch_resolution.flush_pending_branch_alerts(session)

        assert created == 0
        session.add.assert_not_called()
        second_policy.assert_not_awaited()

    def test_populating_column_stops_the_signal(self):
        import app.branch_resolution as branch_resolution

        # A NULL column enqueues once.
        project = _project(integration_branch=None, promotion_target="release")
        _shared_state.get_project_integration_branch(project)
        assert len(branch_resolution._pending_durable_branch_alerts) == 1

        # Populating the column means the resolver returns it and stops
        # signaling — no further pending item is recorded.
        branch_resolution._pending_durable_branch_alerts.clear()
        branch_resolution._missing_project_branch_alerts.clear()
        project.integration_branch = "develop"
        assert _shared_state.get_project_integration_branch(project) == "develop"
        assert branch_resolution._pending_durable_branch_alerts == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
