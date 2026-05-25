"""Integration tests for the Alembic combined-chain guard wired into the
rollup auto-merge decision in ``_check_and_trigger_rollup``.

The DB-free chain helpers and the ``_validate_rollup_migration_chain`` guard
itself are unit-tested in ``test_migration_chain.py``. These tests exercise the
call site: a small rollup PR targeting an intermediate branch must auto-merge
when the combined migration chain is clean (or untouched), and must be HELD for
approval + routed to a re-chain fix when the chain is invalid — never merged.
The guard is fail-safe: an unfetchable/unparseable migration diff defers rather
than waving the merge through.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models import TaskStatus

# Reuse the in-memory DB fixture + rollup-parent/child builders.
from testing.test_rollup_cleanup import (
    _make_project,
    _make_rollup_parent_with_pr,
    _make_task,
)

# Reuse the diff builders so the guard parses exactly what the invariant tests
# parse (same revision/down_revision in-file values, not filenames).
from testing.test_migration_chain import _added_file_diff

pytest_plugins = ["testing.test_rollup_cleanup"]


def _files(*paths: str) -> list[dict]:
    return [{"filename": p, "status": "added"} for p in paths]


def _mock_automerge_deps(mocker, *, pr_files, diff, merge_merged=True):
    """Mock the auto-merge path so control reaches the Alembic guard.

    ``get_pull_request`` is consumed twice before the merge: the resume-time
    existing-PR validation and the diff-size fetch (small PR → auto-merge,
    no ``head`` so the branch-tip check is skipped). Returns the merge and
    spawn mocks so callers can assert whether the merge fired and whether a
    re-chain fix was routed.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch(
        "app.github.get_pr_files", new_callable=AsyncMock, return_value=pr_files
    )
    mocker.patch(
        "app.github.get_pull_request_diff", new_callable=AsyncMock, return_value=diff
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {"merged": False, "state": "open"},
            {"additions": 10, "deletions": 5, "merged": False, "state": "open"},
        ],
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": merge_merged, "message": "ok"},
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), True),
    )
    return mock_merge, mock_spawn


async def _make_merged_child(session, project_id, parent_id):
    return await _make_task(
        session,
        project_id,
        parent_task_id=parent_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )


@pytest.mark.asyncio
async def test_clean_single_head_migration_rollup_auto_merges(cleanup_db, mocker):
    """A rollup touching alembic/versions with a single-head, unique-revision
    combined chain passes the guard and auto-merges (merge_pull_request fires;
    no re-chain fix is routed). The intermediate-target parent stays EVALUATING
    — the rollup landed on the staging branch, not yet on the integration
    branch — but crucially it was NOT held for approval."""
    diff = _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/alembic/versions/0124_a.py"),
        diff=diff,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_awaited_once()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_rollup_not_touching_migrations_unaffected(cleanup_db, mocker):
    """A rollup whose diff does not touch alembic/versions auto-merges; the
    guard returns ok without even fetching the diff."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
        diff="",  # guard must not reach the diff fetch
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_awaited_once()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_duplicate_revision_rollup_blocked_and_routed_to_fix(cleanup_db, mocker):
    """Two sibling migrations declaring the same revision identifier → the
    combined chain is invalid; the rollup is NOT merged, the parent is held for
    approval, and a re-chain fix task is spawned."""
    diff = _added_file_diff(
        "backend/alembic/versions/0124_a.py", "0124", "0123"
    ) + _added_file_diff("backend/alembic/versions/0124_b.py", "0124", "0123")
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files(
            "backend/alembic/versions/0124_a.py",
            "backend/alembic/versions/0124_b.py",
        ),
        diff=diff,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_awaited()
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert parent.status == TaskStatus.EVALUATING
    assert "alembic" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_two_heads_rollup_blocked(cleanup_db, mocker):
    """Two migrations chained off the same head → two heads in the combined
    set; the rollup is blocked and held for approval rather than merged."""
    diff = _added_file_diff(
        "backend/alembic/versions/0124_a.py", "0124", "0123"
    ) + _added_file_diff("backend/alembic/versions/0125_b.py", "0125", "0123")
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files(
            "backend/alembic/versions/0124_a.py",
            "backend/alembic/versions/0125_b.py",
        ),
        diff=diff,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_awaited()
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_unfetchable_migration_diff_defers_failsafe(cleanup_db, mocker):
    """Fail-safe: a rollup touches alembic/versions but the diff cannot be
    fetched (empty response) → the guard defers; the merge is NOT performed and
    no fix is spawned (the parent retries next sweep, still EVALUATING)."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/alembic/versions/0124_a.py"),
        diff="",  # unfetchable diff → ("defer", ...)
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_awaited()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is False
