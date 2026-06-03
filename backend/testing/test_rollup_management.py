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
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
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
    # Target-branch migration listing for the combined-chain guard. Default to
    # an empty existing chain so the combined check degrades to the in-isolation
    # validation these tests assert against; target-branch-collision cases
    # override this return value.
    mocker.patch(
        "app.github.list_versions_at_ref", new_callable=AsyncMock, return_value=[]
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
async def test_rollup_auto_merges_for_custom_staging_prefix(cleanup_db, mocker):
    """A rollup whose target branch matches a NON-default configured staging
    prefix must be classified as intermediate (is_target_intermediate=True) and
    auto-merge. Regression guard for the bug where the rollup-completion path
    hardcoded 'research/'/'plan/' instead of the per-project
    staging_branch_prefixes — under a custom-prefix project the rollup would
    have been routed to review instead of auto-merging."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
        diff="",  # non-migration → guard returns ok without fetching diff
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    project.build_config = {"staging_branch_prefixes": ["feature/", "wip/"]}
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={
            "research_branch": "feature/conflict-stg",
            "parent_base": "feature/conflict-base",
        },
    )
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


@pytest.mark.asyncio
async def test_head_collision_single_migration_auto_renumbers_and_defers(
    cleanup_db, mocker
):
    """A rollup whose ONLY blocker is a single added migration colliding with the
    target branch head is auto-renumbered: the reconcile commit (renamed migration
    + bumped EXPECTED_SCHEMA_REVISION) is pushed to the STAGING branch and the
    rollup is deferred one sweep — NOT merged, NO operator/re-chain fix spawned,
    NOT held for approval. A per-PR reconcile counter is stamped so a
    non-converging block cannot loop forever. Applied history stays immutable:
    only the not-yet-merged staging branch is rewritten."""
    diff = _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/alembic/versions/0124_a.py"),
        diff=diff,
    )
    # Target branch already has 0124 → the added 0124 collides with the head.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new_callable=AsyncMock,
        return_value=["0123_base.py", "0124_taken.py"],
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new_callable=AsyncMock,
        return_value='EXPECTED_SCHEMA_REVISION = "0124"\n',
    )
    mock_commit = mocker.patch(
        "app.github.commit_file_changes", new_callable=AsyncMock, return_value=True
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_awaited_once()
    mock_merge.assert_not_awaited()
    mock_spawn.assert_not_called()
    # Rewrite lands on the staging branch (research/conflict-stg), never the target.
    assert mock_commit.await_args.args[3] == "research/conflict-stg"
    upserts = mock_commit.await_args.kwargs["upserts"]
    deletes = mock_commit.await_args.kwargs["deletes"]
    # Renumbered onto the next free revision after the head (0124 → 0125).
    assert "backend/alembic/versions/0125_a.py" in upserts
    assert "backend/app/main.py" in upserts
    assert '"0125"' in upserts["backend/app/main.py"]
    assert "backend/alembic/versions/0124_a.py" in deletes
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is False
    ctx = parent.context or {}
    assert ctx.get("_alembic_autoreconcile_pr") == 4242
    assert ctx.get("_alembic_autoreconcile_count") == 1


@pytest.mark.asyncio
async def test_head_collision_autoreconcile_cap_falls_back_to_fix(cleanup_db, mocker):
    """Once the per-PR auto-reconcile counter reaches the cap, a still-blocked
    rollup stops re-renumbering and falls back to the conservative operator-alert /
    re-chain fix path instead of looping the mechanical rewrite forever."""
    diff = _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/alembic/versions/0124_a.py"),
        diff=diff,
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new_callable=AsyncMock,
        return_value=["0123_base.py", "0124_taken.py"],
    )
    mock_commit = mocker.patch("app.github.commit_file_changes", new_callable=AsyncMock)

    from app.rollup_management import (
        _ALEMBIC_AUTORECONCILE_CAP,
        _check_and_trigger_rollup,
    )

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={
            "_alembic_autoreconcile_pr": 4242,
            "_alembic_autoreconcile_count": _ALEMBIC_AUTORECONCILE_CAP,
        },
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    # Cap reached → no further renumber; fall back to the re-chain fix path.
    mock_commit.assert_not_awaited()
    mock_merge.assert_not_awaited()
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert parent.status == TaskStatus.EVALUATING


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
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
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
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.time_utils._utcnow",
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


# ── C4 (4th same-class site): rollup-resume closed-not-merged gate ──────────
#
# rollup_management's resume path (existing rollup_pr_number) previously cleared
# the rollup_pr_number/rollup_pr_url markers on a bare ``state == 'closed'``
# check. During the orchestrator's transient close+reopen mergeability-reset
# window, a rollup PR is briefly closed-not-merged; clearing the markers then
# made the NEXT sweep create a DUPLICATE rollup PR. The fix gates the clear on
# the absence of a pending_reopen reset marker on the parent's review_loop,
# mirroring the other three handlers. These tests lock that in.


def _mock_rollup_resume_closed_pr(mocker, *, merged=False):
    """Mock just enough of the resume path so control reaches the closed-state
    branch with a closed-not-merged existing rollup PR. ``create_pull_request``
    is mocked so the duplicate-PR side effect is observable."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
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
    # Resume-time existing-PR validation returns closed-not-merged.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": merged, "state": "closed"},
    )
    # If the markers are (wrongly) cleared, the code falls through to create a
    # NEW rollup PR — this mock makes that duplicate observable.
    mock_create = mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"number": 9999, "html_url": "https://x/pull/9999"},
    )
    return mock_create


@pytest.mark.asyncio
async def test_rollup_resume_closed_pending_reopen_preserves_marker_no_duplicate(
    cleanup_db, mocker
):
    """C4 4th-site fix: a closed-not-merged rollup PR WITH review_loop.pending_reopen
    set is OUR transient mergeability-reset window. The resume path must DEFER —
    preserve rollup_pr_number/rollup_pr_url and NOT create a duplicate PR."""
    mock_create = _mock_rollup_resume_closed_pr(mocker, merged=False)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={
            # The orchestrator's CI poll records its self-close reset window
            # via review_loop.pending_reopen on THIS parent (the rollup PR's
            # review_loop, seeded at the large-rollup → review handoff).
            "review_loop": {
                "pr_number": 4242,
                "pending_reopen": True,
                "ci_waiting": True,
            },
        },
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("rollup_pr_number") == 4242, (
        "pending_reopen reset window must NOT clear rollup_pr_number — clearing it "
        "spawns a duplicate rollup PR next cycle (C4 4th site)."
    )
    assert ctx.get("rollup_pr_url"), (
        "rollup_pr_url must also survive the reset window."
    )
    mock_create.assert_not_called()
    # Parent deferred — still EVALUATING, not terminalized.
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_rollup_resume_closed_without_pending_reopen_clears_marker(
    cleanup_db, mocker
):
    """C4 4th-site counterpart: a closed-not-merged rollup PR with NO pending_reopen
    marker is a genuine abandonment — the resume path clears the markers (will
    retry by creating a fresh PR), preserving the pre-fix behavior for real
    closures."""
    _mock_rollup_resume_closed_pr(mocker, merged=False)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={"review_loop": {"pr_number": 4242, "ci_waiting": True}},
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert "rollup_pr_number" not in ctx, (
        "A genuine closed-not-merged (no pending_reopen) must clear the marker "
        "so the rollup is retried."
    )
    assert "rollup_pr_url" not in ctx


# ── Duplicate-create guard (rollup_pr_creating_at claim) ──────────────────────
# Two concurrent _check_and_trigger_rollup invocations (sibling children
# completing together, reaper + completion event both firing) raced in the
# lock-released window around create_pull_request and both POSTed, leaving a
# duplicate rollup PR (incident #7350/#7351). The fix stakes a
# `rollup_pr_creating_at` claim under the lock before the create window; a peer
# that sees a fresh claim defers. These tests lock the behavior in.


def _mock_rollup_create_path(mocker, *, create_return=None):
    """Mock deps so a rollup parent with NO existing PR reaches the PR-creation
    branch. ``create_pull_request`` is mocked so a (would-be duplicate) POST is
    observable via the returned mock."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    return mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value=create_return
        or {"number": 5555, "html_url": "https://x/pull/5555"},
    )


async def _make_rollup_parent_no_pr(session, project_id, *, extra_context=None):
    """Rollup parent in EVALUATING with NO rollup_pr_number — the first-time
    create path (mirrors _make_rollup_parent_with_pr minus the PR marker)."""
    ctx = {
        "research_branch": "research/dup-stg",
        "parent_base": "research/dup-base",
    }
    if extra_context:
        ctx.update(extra_context)
    return await _make_task(
        session,
        project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context=ctx,
        plan_output="some plan",
    )


@pytest.mark.asyncio
async def test_rollup_defers_when_peer_is_creating_pr(cleanup_db, mocker):
    """A FRESH rollup_pr_creating_at claim means a peer invocation is mid-create:
    a concurrent _check_and_trigger_rollup must DEFER instead of POSTing a
    duplicate rollup PR (#7350/#7351). The claim is preserved for the peer."""
    import time as _time

    mock_create = _mock_rollup_create_path(mocker)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_no_pr(
        cleanup_db,
        project.id,
        extra_context={"rollup_pr_creating_at": _time.time()},
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_create.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    # Claim preserved so the peer that staked it still owns the create.
    assert (parent.context or {}).get("rollup_pr_creating_at") is not None


@pytest.mark.asyncio
async def test_rollup_creates_when_claim_is_stale(cleanup_db, mocker):
    """A STALE claim (peer crashed mid-create, past the TTL) must NOT wedge
    creation — the rollup proceeds to create the PR, and the stale claim is
    scrubbed on the terminal transition."""
    import time as _time
    from app.rollup_management import _ROLLUP_PR_CREATE_CLAIM_TTL_S

    # Empty-diff create result → COMPLETED via a short terminal path.
    mock_create = _mock_rollup_create_path(
        mocker,
        create_return={"empty_diff": True, "merged": True, "html_url": "", "number": 0},
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_no_pr(
        cleanup_db,
        project.id,
        extra_context={
            "rollup_pr_creating_at": _time.time() - _ROLLUP_PR_CREATE_CLAIM_TTL_S - 10
        },
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_create.assert_awaited_once()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert "rollup_pr_creating_at" not in (parent.context or {}), (
        "stale claim must be scrubbed on the terminal transition"
    )


@pytest.mark.asyncio
async def test_rollup_create_clears_claim_on_marker_commit(cleanup_db, mocker):
    """On a successful create the durable rollup_pr_number marker supersedes the
    transient claim — `rollup_pr_creating_at` must be dropped when the marker is
    persisted (it must never outlive the create). The parent starts with NO claim
    (a fresh claim would trip the defer guard); the create path stakes the claim
    internally, then the marker commit must drop it."""
    diff = _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
    # Full create→auto-merge path; get_pull_request as a constant so the create
    # path's call count (no resume-time fetch) doesn't desync a side_effect list.
    mock_merge, _ = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/alembic/versions/0124_a.py"),
        diff=diff,
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"additions": 10, "deletions": 5, "merged": False, "state": "open"},
    )
    mock_create = mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"number": 6001, "html_url": "https://x/pull/6001"},
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_no_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_create.assert_awaited_once()
    mock_merge.assert_awaited_once()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("rollup_pr_number") == 6001
    assert "rollup_pr_creating_at" not in ctx, (
        "the creation claim must be dropped once the durable marker lands"
    )


# ---------------------------------------------------------------------------
# Merge-boundary promotion-target safety (branch-resolver PR)
#
# The hot-path resolver fails SAFE and never raises; refusing to land fleet
# work on the promotion target is enforced here, at the merge/rollup boundary.
# The guard fires ONLY when the landing target == promotion target AND that
# target is distinct from the integration branch (single-trunk projects, where
# integration == promotion, legitimately land every rollup on the shared
# trunk). A 'hotfix' marker on the parent/task is the explicit exemption.
# ---------------------------------------------------------------------------


async def _make_distinct_branch_project(session):
    """Project whose integration branch ('automain') differs from its promotion
    target (NULL -> 'main'), so the promotion-target guard can fire."""
    project = await _make_project(session)
    project.integration_branch = "automain"
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_rollup_targeting_promotion_target_is_held_not_merged(cleanup_db, mocker):
    """A completed rollup whose target == promotion (distinct from integration)
    is HELD for approval instead of merged."""
    mock_merge, _ = _mock_automerge_deps(
        mocker, pr_files=_files("backend/app/models.py"), diff=""
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_distinct_branch_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"parent_base": "main"}
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert "promotion target" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_rollup_targeting_promotion_target_allowed_for_hotfix(cleanup_db, mocker):
    """A hotfix-marked rollup bypasses the promotion-target safety hold.

    Without the hotfix marker this exact configuration (target 'main' ==
    promotion, distinct from integration 'automain') is held — see
    test_rollup_targeting_promotion_target_is_held_not_merged. The marker is the
    explicit exemption, so the branch-safety hold must NOT fire.
    """
    _mock_automerge_deps(mocker, pr_files=_files("backend/app/models.py"), diff="")
    mock_hold = mocker.patch(
        "app.rollup_management._hold_rollup_for_branch_safety", new_callable=AsyncMock
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_distinct_branch_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"parent_base": "main", "hotfix": True}
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_hold.assert_not_called()


@pytest.mark.asyncio
async def test_single_trunk_rollup_to_shared_trunk_not_held(cleanup_db, mocker):
    """Single-trunk project (integration == promotion == 'main'): a rollup
    landing on that shared trunk must NOT be held for branch safety — the guard
    requires the target to be distinct from the integration branch."""
    _mock_automerge_deps(mocker, pr_files=_files("backend/app/models.py"), diff="")
    mock_hold = mocker.patch(
        "app.rollup_management._hold_rollup_for_branch_safety", new_callable=AsyncMock
    )

    from app.rollup_management import _check_and_trigger_rollup

    # _make_project leaves integration_branch NULL -> resolver returns "main",
    # matching the NULL promotion_target -> "main": a single-trunk project.
    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"parent_base": "main"}
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_hold.assert_not_called()


def test_promotion_target_rollup_allowed_gate():
    """The hotfix gate: allowed only when a hotfix marker is present and the
    target is non-empty."""
    from app.models import Task
    from app.rollup_management import _promotion_target_rollup_allowed

    # No hotfix marker -> not allowed.
    assert _promotion_target_rollup_allowed(Task(payloads=[{}], context={}), "main") is False
    # Hotfix marker but empty target -> not allowed.
    assert (
        _promotion_target_rollup_allowed(Task(payloads=[{"hotfix": True}], context={}), "")
        is False
    )
    # Hotfix in payload -> allowed.
    assert (
        _promotion_target_rollup_allowed(Task(payloads=[{"hotfix": True}], context={}), "main")
        is True
    )
    # Hotfix in context -> allowed.
    assert (
        _promotion_target_rollup_allowed(Task(payloads=[{}], context={"hotfix": True}), "main")
        is True
    )


def test_promotion_target_merge_allowed_gate():
    """Admin-merge hotfix gate: both branches (allowed / not allowed)."""
    from app.models import Task
    from app.admin_task_mutations import _promotion_target_merge_allowed

    assert _promotion_target_merge_allowed(Task(payloads=[{}], context={}), "main") is False
    assert (
        _promotion_target_merge_allowed(Task(payloads=[{"hotfix": True}], context={}), "")
        is False
    )
    assert (
        _promotion_target_merge_allowed(Task(payloads=[{"hotfix": True}], context={}), "main")
        is True
    )
    assert (
        _promotion_target_merge_allowed(Task(payloads=[{}], context={"hotfix": True}), "main")
        is True
    )


@pytest.mark.asyncio
async def test_admin_task_merge_to_promotion_target_returns_409(cleanup_db):
    """Approving a task merge whose base == promotion (distinct from integration)
    returns 409 and leaves the task requires_approval=True without merging."""
    from app.admin_task_mutations import _handle_task_pr_approval
    from app.models import Artifact, ArtifactType

    project = await _make_distinct_branch_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "base_branch": "main"}],
    )
    task.requires_approval = True
    cleanup_db.add(task)
    # The handler resolves the PR number from the task's PULL_REQUEST artifact
    # before reaching the promotion-target guard, so the task needs one.
    cleanup_db.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/acme/repo/pull/4242",
        )
    )
    await cleanup_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _handle_task_pr_approval(
            task, task.context or {}, task.first_payload, cleanup_db, {}
        )

    assert exc_info.value.status_code == 409
    assert "promotion target" in exc_info.value.detail
    await cleanup_db.refresh(task)
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_admin_rollup_merge_to_promotion_target_proceeds_unqualified(
    cleanup_db, mocker
):
    """A human-approved rollup merge whose base == promotion (distinct from
    integration) PROCEEDS rather than dead-ending with a 409 (PR #7064 superseded
    the earlier hard-refuse of PR #6995 for the human-approval lane). The merge
    lands but stays unqualified: pr_merged is left unset and the out-of-band
    promotion-target alert is raised. The autonomous rollup path still holds such
    landings for approval first; this handler IS that approval."""
    from app.admin_task_mutations import _handle_rollup_pr_approval

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    # The pre-merge Alembic dual-head guard lists the PR's files; this rollup
    # touches no migrations, so an empty list yields an "ok" verdict and the
    # merge proceeds. Without the mock the guard's GitHub call 401s and the
    # fail-safe "defer" verdict would wrongly refuse this merge.
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = await _make_distinct_branch_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 4242},
        payloads=[{"type": "research", "base_branch": "main"}],
    )
    task.requires_approval = True
    cleanup_db.add(task)
    await cleanup_db.commit()

    changes: dict = {}
    await _handle_rollup_pr_approval(task, task.context or {}, 4242, cleanup_db, changes)

    mock_merge.assert_awaited_once()
    assert changes.get("merged_pr") == 4242
    await cleanup_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is None


@pytest.mark.asyncio
async def test_admin_rollup_hotfix_to_promotion_target_merges(cleanup_db, mocker):
    """A hotfix-marked approved rollup IS allowed to merge to the promotion
    target (positive branch of the hotfix exemption)."""
    from app.admin_task_mutations import _handle_rollup_pr_approval

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    # See sibling test: empty PR-file list → "ok" Alembic guard verdict so the
    # hotfix merge proceeds instead of hitting the fail-safe "defer" refusal.
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.admin_task_mutations._maybe_spawn_rollup_smoketest", new_callable=AsyncMock
    )

    project = await _make_distinct_branch_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 4242},
        payloads=[{"type": "research", "base_branch": "main", "hotfix": True}],
    )
    changes: dict = {}

    await _handle_rollup_pr_approval(task, task.context or {}, 4242, cleanup_db, changes)

    mock_merge.assert_awaited_once()
    assert changes.get("merged_pr") == 4242
    await cleanup_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True
    assert (task.context or {}).get("landed_on_integration") is False
    assert (
        (task.context or {}).get("_non_merge_unblock_reason")
        == "merged_pr_non_integration_branch"
    )
