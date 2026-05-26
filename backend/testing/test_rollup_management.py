"""Tests for rollup management: Alembic combined-chain guard and PR title prefix."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified
from unittest.mock import AsyncMock, MagicMock

from app.models import TaskStatus

# Reuse the in-memory DB fixture + rollup-parent/child builders.
from testing.test_rollup_cleanup import (
    _make_project,
    _make_rollup_parent_with_pr,
    _make_task,
    _mock_rollup_merge_conflict_deps,
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
async def test_clean_pr_sha_mismatch_skips_update_branch(cleanup_db, mocker):
    """A rollup PR whose head SHA differs from the target-tip SHA but whose
    mergeable_state is 'clean' must proceed directly to merge without calling
    update_branch_with_sha_retry.  Regression guard for the false-behind trigger
    that caused clean rollup PRs to enter unnecessary update/rebase recovery."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
        diff="",
    )
    # Override get_pull_request so the diff-size/pre-merge fetch includes a head
    # SHA (triggering the branch-tip comparison) and mergeable_state=clean.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {"merged": False, "state": "open"},
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "head": {"sha": "head-sha-111"},
                "mergeable_state": "clean",
            },
        ],
    )
    # Return a SHA that differs from head-sha-111 to trigger the mismatch branch.
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        return_value="base-tip-999",
    )
    mock_upd = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_upd.assert_not_called()
    mock_merge.assert_awaited_once()
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_post_merge_behind_ancestor_skips_update_branch(cleanup_db, mocker):
    """Post-merge-failure path: if GitHub reports mergeable_state='behind' but
    is_ancestor_of returns True (base tip already reachable from head), the new
    pr_is_not_behind guard should skip update_branch_with_sha_retry and defer
    to the next rollup sweep instead of escalating to behind-recovery."""
    _mock_rollup_merge_conflict_deps(
        mocker, mergeable_state="behind", head_sha="head-555"
    )
    # Override is_ancestor_of to True so pr_is_not_behind returns True.
    mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )
    mock_upd = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": True},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), True),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    # pr_is_not_behind returned True → skip update_branch, no fix spawn.
    mock_upd.assert_not_awaited()
    mock_spawn.assert_not_called()


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


# ---------------------------------------------------------------------------
# Race-condition regression: empty-diff note uses with_directions_locked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_diff_directions_note_survives_concurrent_write(cleanup_db, mocker):
    """The rollup empty-diff note path must not clobber a pre-existing note.

    Simulates the lost-update race: before the fix, the code did
    session.get(Project, ...) without FOR UPDATE, so a concurrent writer could
    have appended a note between the read and the write-back, and that note
    would be silently dropped. After the fix the write goes through
    with_directions_locked which reloads the directions list under the lock,
    so both notes survive."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    # Target branch exists; staging branch is 404 (already cleaned up)
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        side_effect=[
            "target-sha",
            HTTPException(status_code=404, detail="Not Found"),
        ],
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    dir_id = str(uuid.uuid4())

    # Seed the project with a direction that already has one note (simulating
    # a concurrent write that landed before the rollup path runs).
    prior_note = {
        "text": "prior note from concurrent writer",
        "added_at": "2026-01-01T00:00:00",
        "source": "concurrent_test",
    }
    project.directions = [
        {"id": dir_id, "text": "test direction", "notes": [prior_note]}
    ]
    flag_modified(project, "directions")
    cleanup_db.add(project)
    await cleanup_db.commit()

    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/empty-diff-stg", "parent_base": "main"},
        plan_output="some plan",
        payloads=[
            {"type": "research", "prompt": "test", "current_direction_id": dir_id}
        ],
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

    await cleanup_db.refresh(project)
    directions = project.directions or []
    assert directions, "project.directions should not be empty after rollup"
    matched = next((d for d in directions if d.get("id") == dir_id), None)
    assert matched is not None, "direction with dir_id not found"
    notes = matched.get("notes") or []
    sources = [n.get("source") for n in notes]
    # Both the pre-existing note and the rollup note must survive — neither clobbers the other
    assert "concurrent_test" in sources, (
        "pre-existing note was clobbered by rollup write"
    )
    assert "rollup_empty_diff" in sources, "rollup_empty_diff note was not written"


# ---------------------------------------------------------------------------
# Unit tests: get_project_pr_title_prefix
# ---------------------------------------------------------------------------


class TestGetProjectPrTitlePrefix:
    """Unit tests for the per-project PR title prefix helper."""

    def _make_mock_project(self, build_config):
        p = MagicMock()
        p.build_config = build_config
        return p

    def test_prefix_configured_returns_value_with_trailing_space(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project({"pr_title_prefix": "[Blueprint]"})
        assert get_project_pr_title_prefix(project) == "[Blueprint] "

    def test_prefix_absent_returns_empty_string(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project({})
        assert get_project_pr_title_prefix(project) == ""

    def test_prefix_empty_string_returns_empty_string(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project({"pr_title_prefix": ""})
        assert get_project_pr_title_prefix(project) == ""

    def test_none_project_returns_empty_string(self):
        from app.shared_state import get_project_pr_title_prefix

        assert get_project_pr_title_prefix(None) == ""

    def test_none_build_config_returns_empty_string(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project(None)
        assert get_project_pr_title_prefix(project) == ""

    def test_custom_prefix_produces_correct_rollup_title(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project({"pr_title_prefix": "MyBrand"})
        prefix = get_project_pr_title_prefix(project)
        assert f"{prefix}Rollup: My Feature" == "MyBrand Rollup: My Feature"

    def test_absent_prefix_produces_brand_neutral_rollup_title(self):
        from app.shared_state import get_project_pr_title_prefix

        project = self._make_mock_project({})
        prefix = get_project_pr_title_prefix(project)
        assert f"{prefix}Rollup: My Feature" == "Rollup: My Feature"


@pytest.mark.asyncio
async def test_empty_diff_note_uses_directions_lock(cleanup_db, mocker):
    """Staging-branch-missing empty-diff path appends the note via with_directions_locked.

    Drives _check_and_trigger_rollup to the all-children-completed + staging-branch-gone
    branch and asserts that the direction-note mutation goes through
    app.rollup_management.with_directions_locked, not a bare session.get race.
    """
    import uuid
    from fastapi import HTTPException
    from app.rollup_management import _check_and_trigger_rollup
    from app.models import TaskStatus

    project = await _make_project(cleanup_db)
    direction_id = str(uuid.uuid4())

    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "research",
                "prompt": "research",
                "current_direction_id": direction_id,
            }
        ],
        context={
            "research_branch": "research/stg-missing",
            "parent_base": "main",
        },
        plan_output="some plan",
    )

    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.main._utcnow",
        return_value=__import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    # Target branch exists, staging branch returns 404
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        side_effect=[
            "abc123",  # target branch check succeeds
            HTTPException(status_code=404, detail="Not Found"),  # staging branch 404
        ],
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    mock_lock = mocker.patch(
        "app.rollup_management.with_directions_locked",
        new_callable=AsyncMock,
        return_value=True,
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_lock.assert_called_once()
    call_args = mock_lock.call_args
    assert call_args.args[1] == project.id
    # Verify the mutate_fn appends the note when invoked
    directions = [{"id": direction_id, "notes": []}]
    mutate_fn = call_args.args[2]
    mutate_fn(directions)
    assert directions[0]["notes"][0]["source"] == "rollup_empty_diff"
    assert "no actionable changes" in directions[0]["notes"][0]["text"]


@pytest.mark.asyncio
async def test_rollup_behind_fix_prompt_includes_noop_guidance(cleanup_db, mocker):
    """fix_prompt spawned when update_branch budget is exhausted must include
    no-op rebase guidance so the fix agent treats 'Current branch is up to date'
    as success rather than BLOCKED_PRECONDITION (motivating incident: PR #6153)."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="behind")
    mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": "503 Service Unavailable"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), True),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    # Pre-seed retries at 2 so the next increment == budget (3) → fix spawn.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"_behind_update_retries": 2}
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    fix_prompt = mock_spawn.await_args.kwargs.get("fix_prompt", "")
    # No-op rebase must be recognised as success, not failure.
    assert "up-to-date" in fix_prompt, fix_prompt
    assert "don't force-push" in fix_prompt, fix_prompt
    assert "changes needed" in fix_prompt, fix_prompt
