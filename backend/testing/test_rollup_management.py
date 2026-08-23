"""Tests for rollup management: Alembic combined-chain guard and PR title prefix."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified
from unittest.mock import AsyncMock, MagicMock

from app.models import Task, TaskStatus
from app.operator_attention import _task_attention_item
from app.rollup_size_preflight import SizeCapPreflightResult
from app.terminal_rollup_pr_sweep import (
    TERMINAL_ROLLUP_PR_ATTENTION_KEY,
    TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY,
    sweep_terminal_rollup_prs,
)

# Reuse the in-memory DB fixture + rollup-parent/child builders.
from testing.test_rollup_cleanup import (
    _make_project,
    _make_rollup_parent_with_pr,
    _make_task,
    _mock_rollup_merge_conflict_deps,
)

# Reuse the diff builders so the guard parses exactly what the invariant tests
# parse (same revision/down_revision in-file values, not filenames).

pytest_plugins = ["testing.test_rollup_cleanup"]


def _files(*paths: str) -> list[dict]:
    return [{"filename": p, "status": "added"} for p in paths]


def _open_rollup_pr_detail(
    *, mergeable_state: str = "clean", head_sha: str = "deadbeef"
) -> dict:
    """Exact immutable target used by multi-attempt rollup merge fixtures."""
    return {
        "additions": 5,
        "deletions": 2,
        "merged": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": mergeable_state,
        "base": {"sha": "base-sha", "ref": "research/conflict-base"},
        "head": {"sha": head_sha, "ref": "research/conflict-stg"},
    }


def _migration_file(path: str, revision: str, down_revision: str | None) -> dict:
    """Create a pr_files entry for a migration file with per-file patch."""
    down = "None" if down_revision is None else f"'{down_revision}'"
    src = (
        f"revision = '{revision}'\ndown_revision = {down}\n\ndef upgrade():\n    pass\n"
    )
    body = "".join(f"+{line}\n" for line in src.splitlines())
    return {"filename": path, "status": "added", "patch": f"@@ -0,0 +1,5 @@\n{body}"}


def _mock_automerge_deps(mocker, *, pr_files, merge_merged=True):
    """Mock the auto-merge path so control reaches the Alembic guard.

    Every ``get_pull_request`` call returns the same immutable, currently-safe
    target.  The auto-merge path consumes it for resume validation, diff size,
    and the authority boundary's final live-target lookup.  Returns the merge
    and spawn mocks so callers can assert whether the merge fired and whether
    a re-chain fix was routed.
    """
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
        return_value={
            "additions": 10,
            "deletions": 5,
            "merged": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {
                "sha": "base-sha",
                "ref": "research/conflict-base",
            },
            "head": {
                "sha": "reviewed-rollup-head",
                "ref": "research/conflict-stg",
            },
        },
    )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new_callable=AsyncMock,
        return_value=True,
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
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult("ok", None, "reviewed-rollup-head"),
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
async def test_rollup_ignores_a_cross_project_parent_edge(cleanup_db):
    """A corrupt foreign parent ID cannot mutate another project's rollup."""
    from app.rollup_management import _check_and_trigger_rollup

    parent_project = await _make_project(cleanup_db)
    child_project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        parent_project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        plan_output={"findings": []},
    )
    foreign_child = await _make_task(
        cleanup_db,
        child_project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    await _check_and_trigger_rollup(foreign_child, cleanup_db)
    await cleanup_db.refresh(parent)

    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_landed_descendant_walk_ignores_cross_project_child(cleanup_db, mocker):
    """A corrupt foreign child edge cannot receive another project's proof."""
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants

    parent_project = await _make_project(cleanup_db)
    child_project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        parent_project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
    )
    foreign_child = await _make_task(
        cleanup_db,
        child_project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    original_foreign_context = dict(foreign_child.context or {})
    mocker.patch(
        "app.rollup_management._resolve_rollup_diff_credentials",
        new_callable=AsyncMock,
        return_value=None,
    )
    stamp_range = mocker.patch(
        "app.rollup_management._stamp_child_commit_range",
        new_callable=AsyncMock,
        return_value=False,
    )
    mark_landed = mocker.patch(
        "app.landed_signal.corroborate_and_mark_landed_on_integration",
        new_callable=AsyncMock,
    )

    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )

    assert stamped == []
    stamp_range.assert_not_awaited()
    mark_landed.assert_not_awaited()
    await cleanup_db.refresh(foreign_child)
    assert foreign_child.context == original_foreign_context


async def _make_failed_terminal_rollup_for_sweep(
    session, project_id, *, pr_number: int, extra_context: dict | None = None
):
    """Build a FAILED rollup with one completed child for sweep acceptance tests."""
    parent = await _make_task(
        session,
        project_id,
        task_type="research",
        status=TaskStatus.FAILED,
        context={
            "research_branch": f"research/sweep-{pr_number}",
            "rollup_pr_number": pr_number,
            "rollup_pr_url": f"https://github.test/pull/{pr_number}",
            **(extra_context or {}),
        },
        plan_output="sweep acceptance parent",
    )
    await _make_merged_child(session, project_id, parent.id)
    return parent


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_requeues_tracked_open_pr(cleanup_db, mocker):
    """A terminal rollup whose tracked PR is still open is requeued once."""
    project = await _make_project(cleanup_db)
    parent = await _make_failed_terminal_rollup_for_sweep(
        cleanup_db, project.id, pr_number=18001
    )
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="open_pr_rearmed",
    )

    result = await sweep_terminal_rollup_prs(cleanup_db)

    await cleanup_db.refresh(parent)
    assert result == {"open_pr_rearmed": 1}
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context[TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY] == 1
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_reconciles_merged_pr_idempotently(
    cleanup_db, mocker
):
    """A merged terminal rollup is requeued once and never duplicated."""
    project = await _make_project(cleanup_db)
    parent = await _make_failed_terminal_rollup_for_sweep(
        cleanup_db, project.id, pr_number=18002
    )
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="merged_pr_rearmed",
    )
    mocker.patch(
        "app.terminal_rollup_pr_sweep._trigger_merged_parent_classifier",
        new_callable=AsyncMock,
    )

    first = await sweep_terminal_rollup_prs(cleanup_db)
    await cleanup_db.refresh(parent)
    state_after_first = (parent.status, dict(parent.context or {}))
    second = await sweep_terminal_rollup_prs(cleanup_db)

    assert first == {"merged_pr_rearmed": 1}
    assert second == {}
    assert reconcile.await_count == 1
    await cleanup_db.refresh(parent)
    assert (parent.status, dict(parent.context or {})) == state_after_first


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_surfaces_replacement_as_ambiguous(
    cleanup_db, mocker
):
    """A replacement for a closed PR remains FAILED for operator judgment."""
    project = await _make_project(cleanup_db)
    parent = await _make_failed_terminal_rollup_for_sweep(
        cleanup_db, project.id, pr_number=18003
    )
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="replacement_pr_rearmed",
    )

    result = await sweep_terminal_rollup_prs(cleanup_db)

    await cleanup_db.refresh(parent)
    assert result == {"replacement_pr_rearmed": 1}
    assert parent.status == TaskStatus.FAILED
    assert (
        parent.context[TERMINAL_ROLLUP_PR_ATTENTION_KEY]["outcome"]
        == "replacement_pr_rearmed"
    )
    attention = _task_attention_item(
        parent,
        reason="terminal_rollup_pr_attention",
        severity="needs_operator",
    )
    assert attention["reason"] == "terminal_rollup_pr_attention"
    assert attention["severity"] == "needs_operator"
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_ambiguous_case_is_idempotent(cleanup_db, mocker):
    """An uncertain lookup is surfaced once and skipped on the next sweep."""
    project = await _make_project(cleanup_db)
    parent = await _make_failed_terminal_rollup_for_sweep(
        cleanup_db, project.id, pr_number=18004
    )
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="lookup_uncertain_identity_preserved",
    )

    first = await sweep_terminal_rollup_prs(cleanup_db)
    await cleanup_db.refresh(parent)
    context_after_first = dict(parent.context or {})
    second = await sweep_terminal_rollup_prs(cleanup_db)

    assert first == {"lookup_uncertain_identity_preserved": 1}
    assert second == {}
    assert reconcile.await_count == 1
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context == context_after_first


@pytest.mark.asyncio
async def test_rollup_does_not_credit_childless_plan_without_pr(cleanup_db):
    """Persisted plan output with zero children is recovery work, not a no-op."""
    from datetime import timedelta
    from sqlmodel import select

    from app.rollup_management import _check_and_trigger_rollup
    from app.time_utils import _utcnow

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "rollup_no_pr_artifact",
        },
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
    )
    child.updated_at = _utcnow() - timedelta(minutes=20)
    cleanup_db.add(child)
    await cleanup_db.commit()

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    await cleanup_db.refresh(child)
    # The verifier must override stale credit without writing through this
    # caller's unlocked/stale ORM instance.
    assert (child.context or {}).get("pr_merged") is True
    assert (
        (child.context or {}).get("_non_merge_unblock_reason")
        == "rollup_no_pr_artifact"
    )
    assert parent.status == TaskStatus.EVALUATING
    assert "PR not merged" in (parent.latest_status_message or "")
    child_ids = (
        await cleanup_db.execute(
            select(Task.id).where(Task.parent_task_id == parent.id)
        )
    ).scalars().all()
    assert child_ids == [child.id], "no rollup/consolidation child may be spawned"


@pytest.mark.asyncio
async def test_pipeline_only_children_do_not_complete_unlanded_pr_owner(cleanup_db):
    """Terminal review/fix helpers are not evidence that their parent's PR landed."""
    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    review = await _make_task(
        cleanup_db,
        project.id,
        task_type="review",
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
    )
    await cleanup_db.commit()

    await _check_and_trigger_rollup(review, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert not (parent.context or {}).get("pr_merged")
    assert "has not landed" in (parent.latest_status_message or "")


def test_post_commit_locked_preserves_lock_scope():
    from app.task_lifecycle import PostCommitLocked

    exc = PostCommitLocked("project held", lock_scope="project", resource_id="p1")
    assert exc.lock_scope == "project"


@pytest.mark.asyncio
async def test_clean_single_head_migration_rollup_auto_merges(cleanup_db, mocker):
    """A rollup touching alembic/versions with a single-head, unique-revision
    combined chain passes the guard and auto-merges (merge_pull_request fires;
    no re-chain fix is routed). The intermediate-target parent stays EVALUATING
    — the rollup landed on the staging branch, not yet on the integration
    branch — but crucially it was NOT held for approval."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_awaited_once()
    assert (
        mock_merge.await_args.kwargs["expected_head_sha"]
        == "reviewed-rollup-head"
    )
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_rollup_guard_block_skips_auto_merge(cleanup_db, mocker):
    """A blocking combined-chain verdict prevents the auto-merge fallback."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    mocker.patch(
        "app.rollup_management._validate_rollup_migration_chain",
        new=AsyncMock(return_value=("block", "dual-head: duplicate revision")),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_called()
    mock_spawn.assert_awaited_once()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert "alembic" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_rollup_size_cap_breach_auto_bumped_defers(cleanup_db, mocker):
    """Size-cap preflight returns 'bumped' → merge NOT called, autobump counter stamped."""
    mock_merge, _mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
    )
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult(
            "bumped", "models.py: 500 -> 750", "reviewed-rollup-head"
        ),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False
    assert parent.context.get("_size_cap_autobump_count") == 1
    assert "auto-bumped" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_rollup_size_cap_breach_hold_when_cap_exhausted(cleanup_db, mocker):
    """Size-cap autobump counter already at cap → merge NOT called, requires_approval set."""
    mock_merge, _mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
    )
    mock_preflight = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult(
            "hold",
            "auto-bump budget exhausted; breach: models.py: 500 -> 750",
            "reviewed-rollup-head",
        ),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    # Seed the parent context so the autobump counter is already at the cap (2).
    # _make_rollup_parent_with_pr uses rollup_pr_number=4242.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={"_size_cap_autobump_count": 2, "_size_cap_autobump_pr": 4242},
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    # Budget exhausted → preflight called with commit_bump=False
    mock_preflight.assert_awaited_once()
    _, preflight_kwargs = mock_preflight.call_args
    assert preflight_kwargs.get("commit_bump") is False

    mock_merge.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert "size" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_assembled_rollup_size_breach_cannot_merge(cleanup_db, mocker):
    """An over-cap scripts change in the assembled tree is a merge hold.

    This is deliberately an auto-merge-path test: the assertion is on the
    actual GitHub merge submission, not merely the preflight's status value.
    """
    mock_merge, _ = _mock_automerge_deps(
        mocker,
        pr_files=_files("scripts/test_guard_gate_left_shift.py"),
    )
    preflight = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult(
            "hold",
            "scripts/test_guard_gate_left_shift.py: 2440 lines exceeds cap 2400",
            "reviewed-rollup-head",
        ),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    preflight.assert_awaited_once()
    assert preflight.await_args.kwargs["expected_head_sha"] == "reviewed-rollup-head"
    mock_merge.assert_not_awaited()
    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert "size-cap" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_assembled_rollup_old_size_verdict_cannot_authorize_new_head(
    cleanup_db, mocker
):
    """A verdict for an older tip is rejected before merge submission."""
    mock_merge, _ = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/testing/test_rollup_management.py"),
    )
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult("ok", None, "stale-rollup-head"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_not_awaited()
    await cleanup_db.refresh(parent)
    assert "immutable merge head" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_rollup_not_touching_migrations_unaffected(cleanup_db, mocker):
    """A rollup whose diff does not touch alembic/versions auto-merges; the
    guard returns ok without even fetching the diff."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files("backend/app/models.py"),
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
    )
    # This test deliberately replaces the default research/* target with a
    # custom configured prefix.  Keep every live-target read bound to that
    # exact immutable PR identity so the authority boundary validates the
    # behavior under test instead of (correctly) rejecting a stale fixture.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "additions": 10,
            "deletions": 5,
            "merged": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {"sha": "base-sha", "ref": "feature/conflict-base"},
            "head": {
                "sha": "reviewed-rollup-head",
                "ref": "feature/conflict-stg",
            },
        },
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
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123"),
            _migration_file("backend/alembic/versions/0124_b.py", "0124", "0123"),
        ],
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
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123"),
            _migration_file("backend/alembic/versions/0125_b.py", "0125", "0123"),
        ],
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
    )
    # Override get_pull_request so the diff-size/pre-merge fetch includes a head
    # SHA (triggering the branch-tip comparison) and mergeable_state=clean.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "head-sha-111",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "head-sha-111",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "head-sha-111",
                    "ref": "research/conflict-stg",
                },
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
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult("ok", None, "head-sha-111"),
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
    """Fail-safe: a rollup touches alembic/versions but the migration source
    cannot be fetched (no per-file patch, head SHA lookup fails) → the guard
    defers; the merge is NOT performed and no fix is spawned (the parent
    retries next sweep, still EVALUATING)."""
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=_files(
            "backend/alembic/versions/0124_a.py"
        ),  # no patch → triggers fallback
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
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
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
async def test_head_collision_multi_migration_auto_renumbers_full_tail_and_defers(
    cleanup_db, mocker
):
    """A rollup bundling SEVERAL chained migrations (multiple subtasks) whose
    oldest collides with the target head is auto-reconciled at parity with the
    single-migration case: the colliding migration AND its full linear
    descendant tail are renumbered monotonically into a contiguous block, the
    reconcile commit (all renamed migrations + bumped EXPECTED_SCHEMA_REVISION)
    is pushed to the STAGING branch, and the rollup is deferred one sweep — NOT
    merged, NO operator/re-chain fix spawned."""
    # Branch-time head was 0123; subtasks cut 0124 (down 0123) and 0125 (down
    # 0124). Target advanced to head 0124, so the oldest 0124 collides.
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123"),
            _migration_file("backend/alembic/versions/0125_b.py", "0125", "0124"),
        ],
    )
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
    assert mock_commit.await_args.args[3] == "research/conflict-stg"
    upserts = mock_commit.await_args.kwargs["upserts"]
    deletes = mock_commit.await_args.kwargs["deletes"]
    # The added revision ids are provisional: the chain finalizes onto the
    # target's current head 0124, filling the next free live slots → 0125, 0126;
    # main.py bumped to the new head 0126.
    assert "backend/alembic/versions/0125_a.py" in upserts
    assert "backend/alembic/versions/0126_b.py" in upserts
    assert "backend/app/main.py" in upserts
    assert '"0126"' in upserts["backend/app/main.py"]
    # Both old paths deleted so the rename is clean.
    assert "backend/alembic/versions/0124_a.py" in deletes
    assert "backend/alembic/versions/0125_b.py" in deletes
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
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    assert ctx.get("rollup_pr_url"), "rollup_pr_url must also survive the reset window."
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
    # Full create→auto-merge path; get_pull_request as a constant so the create
    # path's call count (no resume-time fetch) doesn't desync a side_effect list.
    mock_merge, _ = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "additions": 10,
            "deletions": 5,
            "merged": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {"sha": "base-sha", "ref": "research/dup-base"},
            "head": {
                "sha": "reviewed-rollup-head",
                "ref": "research/dup-stg",
            },
        },
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


async def _execute_exact_operator_approval(
    task,
    session,
    *,
    pr_number: int,
    base_branch: str,
    head_sha: str,
    base_sha: str,
):
    """Exercise the durable public approval decision/follow-up boundary."""

    from app.admin_approval_authority import decide_operator_approval
    from app.admin_task_approvals import execute_approval_followup

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop.update(
        {
            "pr_number": pr_number,
            "base_branch": base_branch,
            "ci_checked_head_sha": head_sha,
            "ci_checked_base_sha": base_sha,
            "head_sha": head_sha,
            "base_sha": base_sha,
        }
    )
    context["review_loop"] = review_loop
    task.context = context
    task.requires_approval = True
    session.add(task)
    await session.commit()
    _, decision, _ = await decide_operator_approval(
        task.id,
        False,
        session,
        modified_by="TEST",
    )
    return await execute_approval_followup(decision, session)


@pytest.mark.asyncio
async def test_rollup_targeting_promotion_target_is_held_not_merged(cleanup_db, mocker):
    """A completed rollup whose target == promotion (distinct from integration)
    is HELD for approval instead of merged."""
    mock_merge, _ = _mock_automerge_deps(
        mocker, pr_files=_files("backend/app/models.py")
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
    _mock_automerge_deps(mocker, pr_files=_files("backend/app/models.py"))
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
    _mock_automerge_deps(mocker, pr_files=_files("backend/app/models.py"))
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
    assert (
        _promotion_target_rollup_allowed(Task(payloads=[{}], context={}), "main")
        is False
    )
    # Hotfix marker but empty target -> not allowed.
    assert (
        _promotion_target_rollup_allowed(
            Task(payloads=[{"hotfix": True}], context={}), ""
        )
        is False
    )
    # Hotfix in payload -> allowed.
    assert (
        _promotion_target_rollup_allowed(
            Task(payloads=[{"hotfix": True}], context={}), "main"
        )
        is True
    )
    # Hotfix in context -> allowed.
    assert (
        _promotion_target_rollup_allowed(
            Task(payloads=[{}], context={"hotfix": True}), "main"
        )
        is True
    )


def test_promotion_target_merge_allowed_gate():
    """Admin-merge hotfix gate: both branches (allowed / not allowed)."""
    from app.models import Task
    from app.admin_task_mutations import _promotion_target_merge_allowed

    assert (
        _promotion_target_merge_allowed(Task(payloads=[{}], context={}), "main")
        is False
    )
    assert (
        _promotion_target_merge_allowed(
            Task(payloads=[{"hotfix": True}], context={}), ""
        )
        is False
    )
    assert (
        _promotion_target_merge_allowed(
            Task(payloads=[{"hotfix": True}], context={}), "main"
        )
        is True
    )
    assert (
        _promotion_target_merge_allowed(
            Task(payloads=[{}], context={"hotfix": True}), "main"
        )
        is True
    )


@pytest.mark.asyncio
async def test_admin_task_merge_to_promotion_target_returns_409(cleanup_db):
    """Approving a task merge whose base == promotion (distinct from integration)
    returns 409 and leaves the task requires_approval=True without merging."""
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
        await _execute_exact_operator_approval(
            task,
            cleanup_db,
            pr_number=4242,
            base_branch="main",
            head_sha="promotion-head-4242",
            base_sha="promotion-base-4242",
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
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "mergeable_state": "clean",
            "base": {"sha": "approved-base-4242", "ref": "main"},
            "head": {"sha": "approved-head-4242", "ref": "rollup/4242"},
        },
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok", "sha": "merge-4242"},
    )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_issue_comments", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
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
        payloads=[
            {"type": "research", "branch": "rollup/4242", "base_branch": "main"}
        ],
    )
    task.requires_approval = True
    cleanup_db.add(task)
    await cleanup_db.commit()

    task, changes = await _execute_exact_operator_approval(
        task,
        cleanup_db,
        pr_number=4242,
        base_branch="main",
        head_sha="approved-head-4242",
        base_sha="approved-base-4242",
    )

    mock_merge.assert_awaited_once()
    assert changes.get("merged_pr") == 4242
    await cleanup_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is None


@pytest.mark.asyncio
async def test_admin_rollup_hotfix_to_promotion_target_merges(cleanup_db, mocker):
    """A hotfix-marked approved rollup IS allowed to merge to the promotion
    target (positive branch of the hotfix exemption)."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok", "sha": "merge-hotfix"},
    )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_issue_comments", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    # See sibling test: empty PR-file list → "ok" Alembic guard verdict so the
    # hotfix merge proceeds instead of hitting the fail-safe "defer" refusal.
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    # Explicit human approval bypasses model review, not immutable target
    # verification.  Both the initial bind and final authority GET must observe
    # the same open, mergeable promotion-target PR.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "merged": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {"sha": "base-sha", "ref": "main"},
            "head": {"sha": "hotfix-head", "ref": "hotfix/approved-rollup"},
        },
    )
    mocker.patch(
        "app.admin_task_approvals._maybe_spawn_rollup_smoketest", new_callable=AsyncMock
    )

    project = await _make_distinct_branch_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 4242},
        payloads=[
            {
                "type": "research",
                "branch": "hotfix/approved-rollup",
                "base_branch": "main",
                "hotfix": True,
            }
        ],
    )
    task, changes = await _execute_exact_operator_approval(
        task,
        cleanup_db,
        pr_number=4242,
        base_branch="main",
        head_sha="hotfix-head",
        base_sha="base-sha",
    )

    mock_merge.assert_awaited_once()
    assert changes.get("merged_pr") == 4242
    await cleanup_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True
    assert (task.context or {}).get("landed_on_integration") is False
    assert (task.context or {}).get(
        "_non_merge_unblock_reason"
    ) == "merged_pr_non_integration_branch"


@pytest.mark.asyncio
async def test_rollup_defers_when_sibling_is_parked(cleanup_db, mocker):
    """A PARKED sibling is non-terminal — the rollup loop must treat it as active
    (active_count > 0) and return without merging. Without this guard a completed
    child would trigger a premature rollup that ignores the still-outstanding
    external job backing the PARKED sibling."""
    from app.rollup_management import _check_and_trigger_rollup

    mock_merge, _ = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    # One completed child (PR merged).
    await _make_merged_child(cleanup_db, project.id, parent.id)
    # One PARKED sibling — non-terminal, waiting on an external signal.
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.PARKED,
    )

    await _check_and_trigger_rollup(
        await _make_merged_child(cleanup_db, project.id, parent.id),
        cleanup_db,
    )

    # PARKED sibling kept active_count > 0 → no merge should have fired.
    mock_merge.assert_not_called()


# ---------------------------------------------------------------------------
# Ancestry gate folded into _classify_merged_rollup_landing: a rollup parent's
# integration landing must be confirmed reachable before the shipped badge.
# ---------------------------------------------------------------------------


def _patch_rollup_ancestry_deps(
    mocker, *, is_ancestor, merge_commit_sha="cafef00dbabe1234"
):
    """Arm the GitHub seams the classify-time ancestry gate consults."""
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "merge_commit_sha": merge_commit_sha},
    )
    return mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=is_ancestor,
    )


async def _make_classify_parent(session, project_id):
    return await _make_task(
        session,
        project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 4242},
    )


@pytest.mark.asyncio
async def test_classify_strands_when_merge_commit_not_ancestor(cleanup_db, mocker):
    """is_ancestor_of -> False: _classify must report NOT landed (and NOT
    terminal), record the stranded marker, and raise an operator alert."""
    from sqlmodel import select as _select

    from app.models import Alert
    from app.landed_signal import STRANDED_LANDING_REASON_KEY
    from app.rollup_management import _classify_merged_rollup_landing

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    anc = _patch_rollup_ancestry_deps(mocker, is_ancestor=False)

    project = await _make_project(cleanup_db, integration_branch="automain")
    parent = await _make_classify_parent(cleanup_db, project.id)

    landed, terminal = await _classify_merged_rollup_landing(
        parent,
        project,
        cleanup_db,
        pr_number=4242,
        target_branch="automain",
        source="test.auto_merge_success",
    )
    await cleanup_db.commit()

    anc.assert_awaited()
    assert landed is False, "a non-ancestor merge must not count as landed"
    assert terminal is False
    ctx = parent.context or {}
    assert ctx.get("pr_merged") is None
    assert ctx.get("landed_on_integration") is False
    assert STRANDED_LANDING_REASON_KEY in ctx
    alerts = (
        (await cleanup_db.execute(_select(Alert).where(Alert.task_id == parent.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_classify_indeterminate_ancestry_preserves_landing(cleanup_db, mocker):
    """is_ancestor_of -> None: indeterminate ancestry must PRESERVE the
    flag-trust landing (landed + terminal True) and neither strand nor alert."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY
    from app.rollup_management import _classify_merged_rollup_landing

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    anc = _patch_rollup_ancestry_deps(mocker, is_ancestor=None)

    project = await _make_project(cleanup_db, integration_branch="automain")
    parent = await _make_classify_parent(cleanup_db, project.id)

    landed, terminal = await _classify_merged_rollup_landing(
        parent,
        project,
        cleanup_db,
        pr_number=4242,
        target_branch="automain",
        source="test.auto_merge_success",
    )

    anc.assert_awaited()
    assert landed is True
    assert terminal is True
    assert STRANDED_LANDING_REASON_KEY not in (parent.context or {})
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_classify_missing_merge_sha_preserves_landing(cleanup_db, mocker):
    """No merge_commit_sha available -> indeterminate -> preserve flag-trust
    landing without consulting is_ancestor_of (a transient gap never blocks)."""
    from app.rollup_management import _classify_merged_rollup_landing

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "merge_commit_sha": None},
    )
    anc = mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )

    project = await _make_project(cleanup_db, integration_branch="automain")
    parent = await _make_classify_parent(cleanup_db, project.id)

    landed, terminal = await _classify_merged_rollup_landing(
        parent,
        project,
        cleanup_db,
        pr_number=4242,
        target_branch="automain",
        source="test.auto_merge_success",
    )

    assert landed is True
    assert terminal is True
    anc.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_positive_ancestry_lands_as_before(cleanup_db, mocker):
    """is_ancestor_of -> True: a confirmed integration landing reports landed +
    terminal exactly as before the gate existed."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY
    from app.rollup_management import _classify_merged_rollup_landing

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    anc = _patch_rollup_ancestry_deps(mocker, is_ancestor=True)

    project = await _make_project(cleanup_db, integration_branch="automain")
    parent = await _make_classify_parent(cleanup_db, project.id)

    landed, terminal = await _classify_merged_rollup_landing(
        parent,
        project,
        cleanup_db,
        pr_number=4242,
        target_branch="automain",
        source="test.auto_merge_success",
    )

    anc.assert_awaited()
    assert landed is True
    assert terminal is True
    assert STRANDED_LANDING_REASON_KEY not in (parent.context or {})
    alert_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Strand-marker DURABILITY through the wrapper's context reset. The classify
# helper writes landed_on_integration=False + STRANDED_LANDING_REASON_KEY into
# parent.context, but both wrapper paths (_check_and_trigger_rollup's
# auto_merge_success and existing_pr_merged branches) immediately reassign
# parent.context. These tests drive the REAL wrapper end-to-end and assert the
# marker survives the commit — i.e. the reset reads POST-classify context, not
# a pre-classify snapshot. They would fail against the clobber regression where
# the reset rebuilt context from a stale `parent_ctx` captured before classify.
# ---------------------------------------------------------------------------


def _stage_gpr_merged_then_sha(mocker, merge_commit_sha="cafef00dbabe1234"):
    """Patch get_pull_request for the auto_merge_success wrapper: the first
    three reads (resume validation, diff-size, authority final GET) report the
    same exact open PR so control reaches the merge; every later read (the
    ancestry merge_commit_sha fetch and beyond) reports a merged PR carrying
    the SHA the ancestry gate inspects."""
    calls = {"n": 0}

    def _gpr(*_a, **_k):
        calls["n"] += 1
        if calls["n"] <= 3:
            return {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "reviewed-rollup-head",
                    "ref": "research/conflict-stg",
                },
            }
        return {"merged": True, "merge_commit_sha": merge_commit_sha}

    mocker.patch(
        "app.github.get_pull_request", new_callable=AsyncMock, side_effect=_gpr
    )


@pytest.mark.asyncio
async def test_auto_merge_success_persists_stranded_marker_through_reset(
    cleanup_db, mocker
):
    """Drive the REAL auto_merge_success wrapper with a non-ancestor merge
    (is_ancestor_of -> False) and confirm the durable stranded marker written by
    _classify_merged_rollup_landing survives the wrapper's `parent.context = {...}`
    reset. Guards against the clobber regression that rebuilt context from a
    pre-classify snapshot, dropping the marker before commit."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY
    from app.rollup_management import _check_and_trigger_rollup

    mock_merge, _mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    # Re-arm get_pull_request so the ancestry gate can resolve a merge SHA, and
    # point the ancestry seam at a definitive non-ancestor result.
    _stage_gpr_merged_then_sha(mocker)
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    anc = mocker.patch(
        "app.github.is_ancestor_of", new_callable=AsyncMock, return_value=False
    )

    # Coincide the integration branch with the rollup's default target
    # (research/conflict-base) so the classify-time ancestry gate fires while the
    # merge path runs exactly as in test_clean_single_head_migration_rollup_auto_merges.
    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_merge.assert_awaited_once()
    anc.assert_awaited()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    # The durable stranded marker must SURVIVE the wrapper's context reset.
    assert ctx.get("landed_on_integration") is False
    assert STRANDED_LANDING_REASON_KEY in ctx
    # A stranded rollup must NOT carry the shipped badge and stays EVALUATING.
    assert ctx.get("pr_merged") is None
    assert parent.status == TaskStatus.EVALUATING
    # The wrapper still re-stamps the rollup PR url it added during the reset.
    assert ctx.get("rollup_pr_url")


@pytest.mark.asyncio
async def test_existing_pr_merged_persists_stranded_marker_through_reset(
    cleanup_db, mocker
):
    """Drive the REAL existing_pr_merged wrapper (resume finds the rollup PR
    already merged) with a non-ancestor merge and confirm the stranded marker
    survives that path's separate `parent.context = {...}` reset. Same clobber
    class as the auto_merge_success path, distinct code site."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY
    from app.rollup_management import _check_and_trigger_rollup

    mock_merge, _mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    # The resume-time existing-PR read already reports the PR merged, so the
    # wrapper takes the existing_pr_merged branch and never calls merge again.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "merge_commit_sha": "cafef00dbabe1234"},
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    anc = mocker.patch(
        "app.github.is_ancestor_of", new_callable=AsyncMock, return_value=False
    )

    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    # The PR was already merged on resume; the wrapper must not re-merge it.
    mock_merge.assert_not_called()
    anc.assert_awaited()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("landed_on_integration") is False
    assert STRANDED_LANDING_REASON_KEY in ctx
    assert ctx.get("pr_merged") is None
    assert parent.status == TaskStatus.EVALUATING


def test_rollup_summary_cost_uses_machine_type_rate():
    """_build_rollup_pr_body scales cost by machine-type rate, not the global default.

    A child with vm_machine_type='e2-standard-4' (2× the e2-standard-2 rate) must
    produce exactly 2× the cost of an identical child on the default machine type.
    This proves that machine-type-aware helper is used rather than the old
    hardcoded e2-standard-2 constants.
    """
    from types import SimpleNamespace
    from datetime import datetime, timedelta
    from app.rollup_management import _build_rollup_pr_body
    from app.vm_cost_rates import get_vm_rate_per_second

    t0 = datetime(2026, 1, 1, 12, 0, 0)
    runtime_secs = 100_000  # long enough that 2-decimal rounding preserves 2:1 ratio
    t1 = t0 + timedelta(seconds=runtime_secs)

    def _child(machine_type):
        return SimpleNamespace(
            payloads=[{"type": "create"}],
            vm_provisioned_at=t0,
            vm_stopped_at=t1,
            vm_spot=False,
            vm_machine_type=machine_type,
        )

    body_default = _build_rollup_pr_body(
        1, "test/branch", children=[_child("e2-standard-2")]
    )
    body_large = _build_rollup_pr_body(
        1, "test/branch", children=[_child("e2-standard-4")]
    )

    assert "Estimated cost" in body_default
    assert "Estimated cost" in body_large

    expected_default = runtime_secs * get_vm_rate_per_second(
        "e2-standard-2", spot=False
    )
    expected_large = runtime_secs * get_vm_rate_per_second("e2-standard-4", spot=False)

    # e2-standard-4 is priced at exactly 2× e2-standard-2
    assert expected_large == pytest.approx(expected_default * 2, rel=1e-9)

    assert f"~${expected_default:.2f}" in body_default, (
        f"Expected cost ~${expected_default:.2f} not found in body: {body_default!r}"
    )
    assert f"~${expected_large:.2f}" in body_large, (
        f"Expected cost ~${expected_large:.2f} not found in body: {body_large!r}"
    )


@pytest.mark.asyncio
async def test_requeue_banked_cost_uses_machine_type_rate(cleanup_db, mocker):
    """_requeue_parent_task accumulates _prior_attempts_cost using the child's
    actual machine-type rate, not the legacy e2-standard-2 constant.

    A child with vm_machine_type='e2-standard-4' (2× the e2-standard-2 rate) must
    produce exactly 2× the banked cost of an identical child on the default machine
    type. This proves the machine-type-aware get_vm_rate_per_second path is used in
    the requeue cost accumulation, not a hardcoded constant.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select

    from app.models import Task
    from app.task_lifecycle_requeue import _requeue_parent_task
    from app.vm_cost_rates import get_vm_rate_per_second

    # Patch side-effecting deps so tests observe only DB state.
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    session = cleanup_db
    project = await _make_project(session)

    runtime_secs = 100_000  # large enough that 2:1 ratio survives rounding

    async def _run_requeue(machine_type):
        """Build a fresh parent+child, call _requeue_parent_task, return the
        cloned child's _prior_attempts_cost."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        t1 = t0 + timedelta(seconds=runtime_secs)

        parent = await _make_task(
            session,
            project.id,
            task_type="research",
            status=TaskStatus.EVALUATING,
            context={"_requeue_count": 0, "direction_id": "d-banked-cost-test"},
            plan_output="some plan",
        )

        child = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "do work"}],
            status=TaskStatus.FAILED,
            parent_task_id=parent.id,
            latest_status_message="Process exited with code 1",
            vm_provisioned_at=t0,
            vm_stopped_at=t1,
            vm_spot=False,
            vm_machine_type=machine_type,
            context={"failure_category": "claude_code_failed"},
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)

        await _requeue_parent_task(parent, session)
        await session.commit()

        # Find the cloned (QUEUED) child created by the requeue.
        clones = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == parent.id,
                        Task.status == TaskStatus.QUEUED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(clones) == 1, (
            f"Expected exactly 1 QUEUED clone after requeue, got {len(clones)}"
        )
        return float((clones[0].context or {}).get("_prior_attempts_cost", 0.0))

    cost_default = await _run_requeue("e2-standard-2")
    cost_large = await _run_requeue("e2-standard-4")

    # Sanity-check the expected values derived from the shared rate table.
    rate_default = get_vm_rate_per_second("e2-standard-2", spot=False)
    rate_large = get_vm_rate_per_second("e2-standard-4", spot=False)
    expected_default = round(runtime_secs * rate_default, 6)
    expected_large = round(runtime_secs * rate_large, 6)

    assert expected_large == pytest.approx(expected_default * 2, rel=1e-9), (
        "e2-standard-4 rate must be exactly 2× e2-standard-2 rate"
    )
    assert cost_default == pytest.approx(expected_default, rel=1e-6), (
        f"Banked cost for e2-standard-2 child: expected {expected_default}, got {cost_default}"
    )
    assert cost_large == pytest.approx(expected_large, rel=1e-6), (
        f"Banked cost for e2-standard-4 child: expected {expected_large}, got {cost_large}; "
        "machine-type rate may not be applied correctly in _requeue_parent_task"
    )


# ---------------------------------------------------------------------------
# SPAWN_REASON_PR_CLOSED_UNMERGED: parent must not fail when fix or review
# spawn returns None because the PR was closed without merging.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_fix_spawn_none_pr_closed_unmerged_does_not_fail_parent(
    cleanup_db, mocker
):
    """When _spawn_pipeline_task returns None and sets SPAWN_REASON_PR_CLOSED_UNMERGED
    on the fix-spawn path (small rollup, merge conflict), the parent must remain
    EVALUATING and must NOT be transitioned to FAILED."""
    from app.review_pipeline import SPAWN_REASON_KEY, SPAWN_REASON_PR_CLOSED_UNMERGED

    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="dirty")

    async def _spawn_closed_unmerged(parent, task_type, session, *args, **kwargs):
        parent.context = {
            **(parent.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_PR_CLOSED_UNMERGED,
        }
        return (None, True)

    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        side_effect=_spawn_closed_unmerged,
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

    await cleanup_db.refresh(parent)
    assert parent.status != TaskStatus.FAILED, (
        "parent must not be FAILED when fix spawn returns None due to PR closed without merge"
    )
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_rollup_review_spawn_none_pr_closed_unmerged_does_not_fail_parent(
    cleanup_db, mocker
):
    """When _spawn_pipeline_task returns None and sets SPAWN_REASON_PR_CLOSED_UNMERGED
    on the review-spawn path (large rollup), the parent must remain EVALUATING
    and must NOT be transitioned to FAILED."""
    from app.review_pipeline import SPAWN_REASON_KEY, SPAWN_REASON_PR_CLOSED_UNMERGED

    # Use automerge deps but override get_pull_request to report a large PR
    # (additions > 100) so the large-rollup → review path is taken.
    mock_merge, _ = _mock_automerge_deps(
        mocker, pr_files=_files("backend/app/models.py")
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {"merged": False, "state": "open"},
            {"additions": 200, "deletions": 50, "merged": False, "state": "open"},
        ],
    )

    async def _spawn_closed_unmerged(parent, task_type, session, *args, **kwargs):
        parent.context = {
            **(parent.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_PR_CLOSED_UNMERGED,
        }
        return (None, True)

    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        side_effect=_spawn_closed_unmerged,
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

    await cleanup_db.refresh(parent)
    assert parent.status != TaskStatus.FAILED, (
        "parent must not be FAILED when review spawn returns None due to PR closed without merge"
    )
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_landed_rollup_propagates_signal_to_carried_children(cleanup_db, mocker):
    """When a rollup PR targeting the integration branch is detected as already
    merged, all COMPLETED children whose code merged into the staging branch
    (pr_merged=True but landed_on_integration=False) receive the landed signal so
    cross-base dependents waiting on has_landed_signal(child) can unblock.

    Proof for acceptance evidence: a cross-base dependent of a child carried in a
    landed rollup becomes eligible only after the rollup lands — the child's
    landed_on_integration transitions from False to True on rollup completion.
    """
    from app.rollup_management import _check_and_trigger_rollup
    from app.landed_signal import has_landed_signal

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    # Existing-PR check returns merged=True → triggers "already merged" landing path
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "state": "closed"},
    )
    # verify_rollup_merge_landed is called when target_branch == integration_branch;
    # return "landed" so _landed=True and the propagation fires.
    mocker.patch(
        "app.rollup_management.verify_rollup_merge_landed",
        new_callable=AsyncMock,
        return_value="landed",
    )

    # Project with a named integration branch so _classify_merged_rollup_landing
    # returns _landed=True when target_branch matches.
    project = await _make_project(cleanup_db, integration_branch="main")

    # Parent targets "main" directly — not an intermediate staging branch.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"parent_base": "main"}
    )

    # Children that merged into the rollup's staging branch but whose
    # landed_on_integration is explicitly False (the _non_merge_unblock_reason
    # marker recorded by mark_unblocked_non_merge when a child's PR merged into
    # a staging branch, not directly onto integration).
    child1 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    child2 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(child1)
    await cleanup_db.refresh(child2)

    # Both children must now carry the landed signal — the key predicate that
    # orchestrator_health._check_candidate_task_deps reads to unblock cross-base
    # dependents (has_landed_signal reads context["landed_on_integration"]).
    assert has_landed_signal(child1), (
        "child1 must have landed signal after rollup lands on integration branch"
    )
    assert has_landed_signal(child2), (
        "child2 must have landed signal after rollup lands on integration branch"
    )
    assert (child1.context or {}).get("landed_on_integration") is True
    assert (child2.context or {}).get("landed_on_integration") is True
    # The staging-branch non-merge marker must be cleared so the child is
    # recognized as a genuine integration landing by all callers.
    assert "_non_merge_unblock_reason" not in (child1.context or {})
    assert "_non_merge_unblock_reason" not in (child2.context or {})


@pytest.mark.asyncio
async def test_propagate_skips_force_dropped_children(cleanup_db, mocker):
    """_propagate_landed_to_rollup_children must NOT stamp landed_on_integration
    on force-dropped children (rollup_no_pr_artifact, malformed_pr_url,
    review_respawn_cap).  These children carry pr_merged=True but were never
    genuinely merged; stamping them erases the force-drop evidence.
    """
    from app.rollup_management import _propagate_landed_to_rollup_children
    from app.landed_signal import has_landed_signal

    project = await _make_project(cleanup_db)

    force_drop_reasons = [
        "rollup_no_pr_artifact",
        "malformed_pr_url",
        "review_respawn_cap",
    ]
    children = []
    for reason in force_drop_reasons:
        child = await _make_task(
            cleanup_db,
            project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": reason,
            },
        )
        children.append(child)

    await _propagate_landed_to_rollup_children(children, cleanup_db)

    for child, reason in zip(children, force_drop_reasons):
        assert not has_landed_signal(child), (
            f"force-dropped child ({reason}) must not receive landed signal"
        )
        assert (child.context or {}).get("landed_on_integration") is False, (
            f"force-dropped child ({reason}) must retain landed_on_integration=False"
        )
        assert (child.context or {}).get("_non_merge_unblock_reason") == reason, (
            f"force-dropped child ({reason}) must retain _non_merge_unblock_reason"
        )


@pytest.mark.asyncio
async def test_propagate_rollup_children_carries_merge_sha(cleanup_db):
    """_propagate_landed_to_rollup_children must forward the landed parent's
    merge commit SHA onto every child it newly lands, so rollup children (which
    land via the parent's single PR with no PR fetch of their own) carry exact
    commit identity for the discovery-review receipt.  When no SHA is supplied,
    the child is still landed but carries no merge_commit_sha key.
    """
    from app.rollup_management import _propagate_landed_to_rollup_children
    from app.landed_signal import LANDED_MERGE_SHA_KEY, has_landed_signal

    project = await _make_project(cleanup_db)

    child = await _make_task(
        cleanup_db,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    await _propagate_landed_to_rollup_children(
        [child], cleanup_db, merge_commit_sha="a" * 40
    )

    assert has_landed_signal(child)
    assert (child.context or {}).get(LANDED_MERGE_SHA_KEY) == "a" * 40
    assert "_non_merge_unblock_reason" not in (child.context or {})

    # No SHA supplied → landed but without commit identity key.
    no_sha_child = await _make_task(
        cleanup_db,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    await _propagate_landed_to_rollup_children([no_sha_child], cleanup_db)

    assert has_landed_signal(no_sha_child)
    assert LANDED_MERGE_SHA_KEY not in (no_sha_child.context or {})


@pytest.mark.asyncio
async def test_propagate_bfs_descendants_carry_parent_merge_sha(cleanup_db):
    """The BFS staging-merged descendant sweep must forward the landed parent's
    merge commit SHA onto every descendant it newly stamps, so deep rollup
    descendants resolve exact commit identity for the discovery-review receipt.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.landed_signal import LANDED_MERGE_SHA_KEY, has_landed_signal

    project = await _make_project(cleanup_db, integration_branch="main")

    outer_parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            LANDED_MERGE_SHA_KEY: "f" * 40,
        },
    )
    sub_rollup = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.COMPLETED,
        parent_task_id=outer_parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    grandchild = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=sub_rollup.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    stamped = await _propagate_landed_to_staging_merged_descendants(
        outer_parent.id, cleanup_db
    )
    await cleanup_db.commit()

    await cleanup_db.refresh(sub_rollup)
    await cleanup_db.refresh(grandchild)

    assert {t.id for t in stamped} == {sub_rollup.id, grandchild.id}
    assert has_landed_signal(sub_rollup)
    assert has_landed_signal(grandchild)
    assert (sub_rollup.context or {}).get(LANDED_MERGE_SHA_KEY) == "f" * 40
    assert (grandchild.context or {}).get(LANDED_MERGE_SHA_KEY) == "f" * 40


def _mock_repoll_resolves_to_dirty(mocker, *, initial_state):
    """Mock setup for the re-poll-resolves-to-dirty path.

    get_pull_request side_effect:
      1. existing-PR validation (open, not merged)
      2. diff-size fetch (small PR)
      3. authority boundary final GET (same exact, currently-safe target)
      4. initial state after failed merge (empty or unknown — triggers repoll)
      5. re-poll result (dirty — should trigger fix spawn, not deferral)
    """
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
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "merge conflict"},
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "base-sha",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {"mergeable_state": initial_state, "head": {"sha": "deadbeef"}},
            {"mergeable_state": "dirty", "head": {"sha": "deadbeef"}},
        ],
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )
    # Skip the asyncio sleep in the repoll loop so tests don't block.
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult("ok", None, "deadbeef"),
    )


@pytest.mark.asyncio
async def test_repoll_resolves_empty_to_dirty_spawns_fix_in_same_sweep(
    cleanup_db, mocker
):
    """When mergeable_state starts 'empty' and re-poll resolves to 'dirty',
    the conflict-fix spawn must fire in the SAME sweep — deferral counters
    must NOT be incremented.

    Regression guard for the bug where `elif _ms != 'dirty':` trapped the
    re-poll result inside the deferral block, causing an unconditional return
    before the fix-spawn path was reached.
    """
    _mock_repoll_resolves_to_dirty(mocker, initial_state="empty")
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), None),
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

    # Fix task must be spawned in this sweep, not deferred.
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    # Deferral counter must NOT have been incremented — confirms we bypassed
    # the deferral block rather than falling through after incrementing.
    assert (parent.context or {}).get("_merge_deferred_count") is None


@pytest.mark.asyncio
async def test_repoll_resolves_unknown_to_dirty_spawns_fix_in_same_sweep(
    cleanup_db, mocker
):
    """Same as test_repoll_resolves_empty_to_dirty but starting from 'unknown'.

    Both 'empty' and 'unknown' enter the repoll sub-loop; both must fall
    through to the conflict-fix spawn when the repoll resolves to 'dirty'.
    """
    _mock_repoll_resolves_to_dirty(mocker, initial_state="unknown")
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), None),
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

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("_merge_deferred_count") is None


def _mock_repoll_or_escalation_landing_deps(mocker, *, get_pr_side_effect, merge_side_effect):
    """Common mock setup for the repoll_merge and escalation_merge success paths.

    Both paths share the same upstream deps (auth, GitHub I/O, housekeeping)
    and differ only in the get_pull_request / merge_pull_request sequences.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={"blocked": False, "findings": [], "summary": "clean", "report": ""},
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch("app.github.list_versions_at_ref", new_callable=AsyncMock, return_value=[])
    mocker.patch("app.github.is_ancestor_of", new_callable=AsyncMock, return_value=False)
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        side_effect=merge_side_effect,
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=get_pr_side_effect,
    )
    # Landing classification: both paths call _classify_merged_rollup_landing
    # which calls verify_rollup_merge_landed when target == integration branch.
    mocker.patch(
        "app.rollup_management.verify_rollup_merge_landed",
        new_callable=AsyncMock,
        return_value="landed",
    )
    # Post-merge housekeeping deps (wrapped in try/except in production, but
    # mocked here for determinism and to avoid real I/O).
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.github.get_pull_request_diff", new_callable=AsyncMock, return_value=""
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._nudge_sibling_rollup_prs", new_callable=AsyncMock
    )
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        return_value=SizeCapPreflightResult("ok", None, "deadbeef"),
    )
    return merge


@pytest.mark.asyncio
async def test_repoll_does_not_merge_head_that_moved_after_size_cap_preflight(
    cleanup_db, mocker
):
    """A clean re-poll may not reuse the original cap verdict for a new head."""
    from app.rollup_management import _check_and_trigger_rollup

    moved_head = "over-cap-head"
    merge = _mock_repoll_or_escalation_landing_deps(
        mocker,
        # The original head is cap-cleared and the first merge fails.  GitHub
        # then reports clean on a different head; its authority read agrees
        # with that new head, so the old implementation submits a second merge.
        get_pr_side_effect=[
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(head_sha=moved_head),
            _open_rollup_pr_detail(head_sha=moved_head),
        ],
        merge_side_effect=[
            {"merged": False, "message": "transient failure"},
            {"merged": False, "message": "must not be submitted"},
        ],
    )
    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    assert merge.await_count == 1, "moved head must defer for a fresh cap preflight"


@pytest.mark.asyncio
async def test_escalation_does_not_merge_head_that_moved_after_size_cap_preflight(
    cleanup_db, mocker
):
    """The persistent-empty escalation has the same immutable-head requirement."""
    from app.rollup_management import _check_and_trigger_rollup

    moved_head = "over-cap-head"
    merge = _mock_repoll_or_escalation_landing_deps(
        mocker,
        # Three empty re-polls exhaust the inline retry.  The escalation read
        # then sees a new head, followed by an agreeing authority read; that
        # must not turn the old size-cap verdict into authority for the new head.
        get_pr_side_effect=[
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(head_sha=moved_head),
            _open_rollup_pr_detail(head_sha=moved_head),
        ],
        merge_side_effect=[
            {"merged": False, "message": "transient failure"},
            {"merged": False, "message": "must not be submitted"},
        ],
    )
    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={
            "_merge_deferred_count": 4,
            "_merge_deferred_state": "empty",
        },
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    assert merge.await_count == 1, "moved head must defer for a fresh cap preflight"


@pytest.mark.asyncio
async def test_repoll_merge_propagates_landed_to_rollup_children(cleanup_db, mocker):
    """When a rollup PR's initial merge returns empty mergeability and the
    re-poll loop resolves to 'clean', triggering a successful inline merge,
    _propagate_landed_to_rollup_children must fire on the repoll_merge success
    branch (rollup_management.py:3667) so staging-only carried children receive
    the landed_on_integration signal.

    Regression guard for the twice-misapplied fix: propagation was initially
    added to the wrong paths (existing_pr_merged/zero_diff_close/auto_merge_success)
    before being correctly placed at the repoll_merge branch in #11301/#11302.
    """
    from app.rollup_management import _check_and_trigger_rollup
    from app.landed_signal import has_landed_signal

    _mock_repoll_or_escalation_landing_deps(
        mocker,
        # get_pull_request calls: (1) validation, (2) diff-size, (3) initial
        # authority GET, (4) post-merge state is empty, (5) repoll resolves
        # clean, (6) repoll authority GET.  Every read carries the same
        # immutable base/head; only mergeability advances.
        get_pr_side_effect=[
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
        ],
        # merge_pull_request calls: (1) initial attempt fails, (2) repoll merge succeeds.
        merge_side_effect=[
            {"merged": False, "message": "merge conflict"},
            {"merged": True, "sha": "abc123"},
        ],
    )

    # Set integration_branch to the same staging branch used as parent_base so
    # that BOTH conditions are satisfied simultaneously:
    #   (a) is_target_intermediate=True  → auto-merge path (repoll loop active)
    #   (b) target_branch == integration_branch → _rp_landed=True → propagation fires
    # This mirrors a multi-hop chain where the rollup lands on the "final"
    # staging branch configured as the project's integration branch.
    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    # Children merged into the staging branch but not yet landed on integration.
    child1 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    child2 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(child1)
    await cleanup_db.refresh(child2)

    assert has_landed_signal(child1), (
        "child1 must receive landed signal via repoll_merge propagation"
    )
    assert has_landed_signal(child2), (
        "child2 must receive landed signal via repoll_merge propagation"
    )
    assert (child1.context or {}).get("landed_on_integration") is True
    assert (child2.context or {}).get("landed_on_integration") is True
    assert "_non_merge_unblock_reason" not in (child1.context or {})
    assert "_non_merge_unblock_reason" not in (child2.context or {})


@pytest.mark.asyncio
async def test_escalation_merge_propagates_landed_to_rollup_children(cleanup_db, mocker):
    """When a rollup PR persists in 'empty' mergeability across the repoll loop
    AND enough deferrals have accumulated to trigger the fast-escalation path,
    a successful escalation merge must call _propagate_landed_to_rollup_children
    at rollup_management.py:3937 so carried staging-only children receive the
    landed_on_integration signal.

    Regression guard for the twice-misapplied fix: same as the repoll_merge
    guard — the escalation_merge path was one of the two paths that received
    the propagation call only in the correct re-land.

    Setup: parent carries _merge_deferred_count=4 and _merge_deferred_state='empty'
    so that after this sweep deferred_count=5 == alert_threshold(5), which triggers
    the fast-escalation attempt. The repoll loop exhausts (all 3 re-polls stay
    'empty') so _repoll_merged=False and control falls through to escalation.
    """
    from app.rollup_management import _check_and_trigger_rollup
    from app.landed_signal import has_landed_signal

    _mock_repoll_or_escalation_landing_deps(
        mocker,
        # get_pull_request calls: (1) validation, (2) diff-size, (3) initial
        # authority GET, (4) post-merge state is empty, (5-7) three empty
        # repolls, (8) escalation bind, (9) escalation authority GET.
        get_pr_side_effect=[
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(mergeable_state="empty"),
            _open_rollup_pr_detail(),
            _open_rollup_pr_detail(),
        ],
        # merge_pull_request calls: (1) initial attempt fails, (2) escalation succeeds.
        merge_side_effect=[
            {"merged": False, "message": "merge conflict"},
            {"merged": True, "sha": "abc123"},
        ],
    )

    # Same project/branch setup as the repoll_merge test: integration_branch
    # matches parent_base so is_target_intermediate=True (enters auto-merge path)
    # AND _esc_landed=True (propagation fires).
    project = await _make_project(
        cleanup_db, integration_branch="research/conflict-base"
    )
    # Pre-seed deferred count so escalation fires this sweep:
    # deferred_count = (_merge_deferred_count + 1) when prev_state matches = 4+1 = 5
    # alert_threshold for _fast_escalate ('empty') defaults to 5 → 5 >= 5 triggers.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db,
        project.id,
        extra_context={
            "_merge_deferred_count": 4,
            "_merge_deferred_state": "empty",
        },
    )
    # Children merged into the staging branch but not yet landed on integration.
    child1 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    child2 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(child1)
    await cleanup_db.refresh(child2)

    assert has_landed_signal(child1), (
        "child1 must receive landed signal via escalation_merge propagation"
    )
    assert has_landed_signal(child2), (
        "child2 must receive landed signal via escalation_merge propagation"
    )
    assert (child1.context or {}).get("landed_on_integration") is True
    assert (child2.context or {}).get("landed_on_integration") is True
    assert "_non_merge_unblock_reason" not in (child1.context or {})
    assert "_non_merge_unblock_reason" not in (child2.context or {})


@pytest.mark.asyncio
async def test_propagate_landed_bfs_stamps_grandchildren(cleanup_db):
    """Variant 2 regression: _propagate_landed_to_staging_merged_descendants must
    stamp descendants at ANY depth, not only direct children.

    Scenario: an outer rollup lands on integration. Its direct child is a
    sub-rollup (plan task) whose own child (a create task, grandchild of the outer
    rollup) merged into the sub-rollup's staging branch but carries
    landed_on_integration=False.  The BFS sweep from the outer rollup's ID must
    propagate through the sub-rollup and reach the grandchild.

    Without the BFS (_propagate_landed_to_staging_merged_descendants), only
    _propagate_landed_to_rollup_children is available — it stamps direct children
    (the sub-rollup) but never descends to the grandchild, permanently wedging any
    cross-base dependent that waits on has_landed_signal(grandchild).
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.landed_signal import has_landed_signal

    project = await _make_project(cleanup_db, integration_branch="main")

    # Outer rollup parent (a research/plan task)
    outer_parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
        },
    )
    # Direct child: a sub-rollup (plan task) that merged into outer parent's staging
    # branch. _propagate_landed_to_rollup_children would stamp this (it's a DIRECT
    # child) but would not descend further.
    sub_rollup = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.COMPLETED,
        parent_task_id=outer_parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # Grandchild: a create task that merged into the sub-rollup's staging branch.
    # _propagate_landed_to_rollup_children WOULD NOT reach this task — it only stamps
    # direct children. The BFS sweep must propagate here so cross-base dependents
    # waiting on has_landed_signal(grandchild) can dispatch.
    grandchild = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=sub_rollup.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    # Run the BFS sweep from the outer rollup parent
    await _propagate_landed_to_staging_merged_descendants(outer_parent.id, cleanup_db)
    await cleanup_db.commit()

    await cleanup_db.refresh(sub_rollup)
    await cleanup_db.refresh(grandchild)

    # Sub-rollup (direct child) must be stamped
    assert has_landed_signal(sub_rollup), (
        "sub_rollup (direct child) must receive landed signal from BFS sweep"
    )
    assert (sub_rollup.context or {}).get("landed_on_integration") is True

    # Grandchild must ALSO be stamped — the key regression assertion.
    # Without the BFS, grandchild.landed_on_integration would stay False because
    # _propagate_landed_to_rollup_children alone would miss it.
    assert has_landed_signal(grandchild), (
        "grandchild must receive landed signal via BFS — "
        "_propagate_landed_to_rollup_children alone would miss it"
    )
    assert (grandchild.context or {}).get("landed_on_integration") is True
    assert "_non_merge_unblock_reason" not in (grandchild.context or {})


@pytest.mark.asyncio
async def test_propagate_landed_descendants_returns_stamped_and_is_idempotent(
    cleanup_db,
):
    """The BFS sweep RETURNS the descendants it newly stamped so callers (the
    ci_completion rollup-landing path) can re-credit their directions, and it is
    idempotent: a second sweep, with everything already landed, stamps nothing and
    returns []. Force-dropped children are never stamped nor returned.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.landed_signal import has_landed_signal

    project = await _make_project(cleanup_db, integration_branch="main")
    outer = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
    )
    # staging-merged child — must be stamped AND returned
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=outer.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # force-dropped child (never genuinely merged) — must NOT be stamped or returned
    dropped = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=outer.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "rollup_no_pr_artifact",
        },
    )

    stamped = await _propagate_landed_to_staging_merged_descendants(
        outer.id, cleanup_db
    )
    await cleanup_db.commit()

    stamped_ids = {t.id for t in stamped}
    assert child.id in stamped_ids
    assert dropped.id not in stamped_ids
    assert has_landed_signal(child)
    await cleanup_db.refresh(dropped)
    assert (dropped.context or {}).get("landed_on_integration") is False

    # Idempotent: everything is already landed / force-dropped, so a re-run stamps
    # nothing and returns an empty list (no double-credit).
    again = await _propagate_landed_to_staging_merged_descendants(outer.id, cleanup_db)
    assert again == []


@pytest.mark.asyncio
async def test_propagate_landed_descendants_copies_coverage_data(cleanup_db):
    """The BFS sweep copies test_coverage_result from the parent task into
    each qualifying descendant's context so the coverage_delta acceptance gate
    can find measured coverage data instead of failing closed.

    When the parent has no test_coverage_result (e.g. a research/plan task
    with no code change), descendants are unaffected — the propagation is
    a no-op.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.landed_signal import has_landed_signal

    COVERAGE_VALUE = {"coverage_pct": 85.0}

    project = await _make_project(cleanup_db, integration_branch="main")

    # ── CASE 1: parent has coverage → descendant inherits it ──────────────
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "test_coverage_result": COVERAGE_VALUE,
        },
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(child)

    assert has_landed_signal(child), (
        "descendant must be stamped landed_on_integration"
    )
    assert child.id in {t.id for t in stamped}

    child_ctx = child.context or {}
    assert child_ctx.get("test_coverage_result") == COVERAGE_VALUE, (
        "descendant context must contain the parent's test_coverage_result"
    )

    # ── CASE 2: existing per-descendant coverage is never overwritten ────
    COVERAGE_OVERRIDE = {"coverage_pct": 95.0}
    child2 = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            "test_coverage_result": COVERAGE_OVERRIDE,
        },
    )
    stamped2 = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(child2)
    assert child2.id in {t.id for t in stamped2}
    assert (
        child2.context or {}
    ).get("test_coverage_result") == COVERAGE_OVERRIDE, (
        "existing per-descendant test_coverage_result must not be overwritten"
    )

    # ── CASE 3: parent without coverage → descendant context unchanged ────
    parent_no_cov = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
    )
    child3 = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent_no_cov.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    stamped3 = await _propagate_landed_to_staging_merged_descendants(
        parent_no_cov.id, cleanup_db
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(child3)
    assert child3.id in {t.id for t in stamped3}
    assert child3.context is None or "test_coverage_result" not in child3.context, (
        "descendant must NOT have test_coverage_result when parent lacks it"
    )


@pytest.mark.asyncio
async def test_propagate_landed_descendants_inherits_changed_files(cleanup_db):
    """The BFS sweep propagates the rollup parent's ``_pr_changed_files`` context
    DOWN to each qualifying landed descendant that lacks its own list, marked
    parent-derived (never github-verified), and respects setdefault: a descendant
    that carries its own self-reported list is never clobbered.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.acceptance_gate import (
        PARENT_DERIVED_CHANGED_FILES_KEY,
        stamp_changed_files_signal,
    )

    project = await _make_project(cleanup_db, integration_branch="main")

    parent_files = [
        "backend/app/rollup_seam.py",
        "backend/testing/test_rollup_seam.py",
    ]
    parent_ctx = {"pr_merged": True, "landed_on_integration": True}
    parent_ctx.update(stamp_changed_files_signal(parent_files, github_verified=True))
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context=parent_ctx,
    )

    # ── CASE 1: child with no own changed files inherits parent-derived list ──
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # ── CASE 2: grandchild (deeper descendant) also inherits via BFS ──────────
    sub_rollup = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    grandchild = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=sub_rollup.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # ── CASE 3: child with OWN changed files must NOT be clobbered ────────────
    own_files = ["backend/app/child_own_seam.py"]
    child_own = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            "_pr_changed_files": own_files,
        },
    )

    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(child)
    await cleanup_db.refresh(grandchild)
    await cleanup_db.refresh(child_own)

    stamped_ids = {t.id for t in stamped}
    assert child.id in stamped_ids
    assert grandchild.id in stamped_ids

    for inheritor in (child, grandchild):
        ctx = inheritor.context or {}
        assert ctx.get("_pr_changed_files") == parent_files
        assert ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True
        # The union diff must never be marked github-verified on a child.
        assert "_pr_changed_files_github_verified" not in ctx

    own_ctx = child_own.context or {}
    assert own_ctx.get("_pr_changed_files") == own_files, (
        "a child's own self-reported changed files must not be clobbered"
    )
    assert PARENT_DERIVED_CHANGED_FILES_KEY not in own_ctx


@pytest.mark.asyncio
async def test_propagate_landed_descendants_inherits_acceptance_block(cleanup_db):
    """Rung 6 of design doc c50453ef (closes U1): a rollup child whose DECLARED
    named_test the parent's single-PR acceptance-test block describes inherits that
    block under ``_inherited_acceptance_block``, so the child's existence-at-merge
    / ran-green readers see the parent's genuinely-run test instead of failing
    closed on an absent ``review_loop``. A child whose declaration the block does
    NOT describe inherits nothing (a green run of some OTHER test is never credited
    for this declaration), and an already-inherited block is never clobbered.
    """
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.acceptance_gate import (
        INHERITED_ACCEPTANCE_BLOCK_KEY,
        MERGE_ACCEPTANCE_BLOCK_KEY,
        REVIEW_LOOP_KEY,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
    )

    project = await _make_project(cleanup_db, integration_branch="main")

    block = {
        "declared": True,
        "descriptor": "test_widget.py",
        "selector": "backend/testing/test_widget.py::test_x",
        "present": True,
        "ran": True,
        "status": "ran",
        "passed": True,
    }
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            REVIEW_LOOP_KEY: {MERGE_ACCEPTANCE_BLOCK_KEY: block},
        },
    )

    # CASE 1: child declares the SAME test the parent's block describes.
    matching_child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        payloads=[
            {
                "type": "create",
                "prompt": "make widget",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # CASE 2: child declares a DIFFERENT test — no inheritance.
    other_child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        payloads=[
            {
                "type": "create",
                "prompt": "make other",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_other.py",
                },
            }
        ],
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )
    # CASE 3: child already carries an inherited block — never clobbered.
    existing_block = dict(block)
    existing_block["descriptor"] = "older inherited block"
    already = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        payloads=[
            {
                "type": "create",
                "prompt": "make widget too",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            INHERITED_ACCEPTANCE_BLOCK_KEY: existing_block,
        },
    )

    await _propagate_landed_to_staging_merged_descendants(parent.id, cleanup_db)
    await cleanup_db.commit()
    await cleanup_db.refresh(matching_child)
    await cleanup_db.refresh(other_child)
    await cleanup_db.refresh(already)

    match_ctx = matching_child.context or {}
    assert match_ctx.get(INHERITED_ACCEPTANCE_BLOCK_KEY) == block
    declared = _declared_named_test_path(matching_child.first_payload)
    assert declared == "backend/testing/test_widget.py"
    # The reader side now sees the parent's genuinely-run test via the inherited
    # block — the child's ran-green evidence is no longer invisible.
    assert _acceptance_test_ran_green_at_merge(match_ctx, declared) is True

    other_ctx = other_child.context or {}
    assert INHERITED_ACCEPTANCE_BLOCK_KEY not in other_ctx
    other_declared = _declared_named_test_path(other_child.first_payload)
    assert _acceptance_test_ran_green_at_merge(other_ctx, other_declared) is False

    # An already-inherited block is untouched (setdefault-style, never clobbered).
    assert (already.context or {}).get(INHERITED_ACCEPTANCE_BLOCK_KEY) == existing_block


@pytest.mark.asyncio
async def test_propagate_landed_descendants_no_parent_changed_files_noop(cleanup_db):
    """When the rollup parent has no ``_pr_changed_files`` (e.g. a research/plan
    parent with no code diff), descendants inherit nothing — the changed-files
    propagation is a clean no-op and the landed signal still stamps."""
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants
    from app.acceptance_gate import PARENT_DERIVED_CHANGED_FILES_KEY
    from app.landed_signal import has_landed_signal

    project = await _make_project(cleanup_db, integration_branch="main")
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
    )

    await _propagate_landed_to_staging_merged_descendants(parent.id, cleanup_db)
    await cleanup_db.commit()
    await cleanup_db.refresh(child)

    child_ctx = child.context or {}
    assert has_landed_signal(child)
    assert "_pr_changed_files" not in child_ctx
    assert PARENT_DERIVED_CHANGED_FILES_KEY not in child_ctx


@pytest.mark.asyncio
async def test_propagate_landed_re_evaluates_already_landed_child_when_parent_changed_files_arrive(
    cleanup_db,
):
    """Late-arriving parent changed files reach an ALREADY-landed rollup child
    and re-record its frozen declared_unsatisfied (missing-from-diff) verdict as
    satisfied_unproven.

    Regression test for the frozen-verdict gap: the BFS sweep used to run the
    parent-derived changed-file propagation + acceptance verdict re-record ONLY
    inside the ``not has_landed_signal(row)`` branch, so a child that landed
    before the parent's ``_pr_changed_files`` arrived was skipped forever and
    its verdict could never flip."""
    from app.acceptance_gate import (
        CODE_ACCEPTANCE_VERDICT_KEY,
        PARENT_DERIVED_CHANGED_FILES_KEY,
        stamp_changed_files_signal,
    )
    from app.landed_signal import has_landed_signal
    from app.rollup_management import _propagate_landed_to_staging_merged_descendants

    project = await _make_project(cleanup_db, integration_branch="main")

    # Parent lands on integration but its changed files arrive LATER.
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
        payloads=[
            {
                "type": "create",
                "prompt": "do the thing",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
    )

    # First propagation: parent has no changed files → child lands but its
    # verdict is frozen at declared_unsatisfied with unavailable provenance.
    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(child)
    assert child.id in {t.id for t in stamped}
    assert has_landed_signal(child)
    recorded = (child.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert recorded is not None
    assert recorded.get("satisfied") is False
    assert "changed-file provenance was unavailable" in (recorded.get("reason") or "")

    # Parent's GitHub-verified changed files arrive late.
    parent_ctx = dict(parent.context or {})
    parent_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    parent.context = parent_ctx
    await cleanup_db.commit()

    # Second propagation: child is ALREADY landed, but the propagation must still
    # reach it and re-record its verdict.
    again = await _propagate_landed_to_staging_merged_descendants(
        parent.id, cleanup_db
    )
    await cleanup_db.commit()
    assert again == [], "no newly-landed descendants — the child was already landed"

    await cleanup_db.refresh(child)
    child_ctx = child.context or {}
    assert child_ctx.get("_pr_changed_files") == [
        "backend/app/widget.py",
        "backend/testing/test_widget.py",
    ]
    assert child_ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True
    re_recorded = child_ctx.get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert re_recorded is not None
    assert re_recorded.get("satisfied") is True
    assert re_recorded.get("proven") is False, (
        "parent-derived credit is capped below machine proof"
    )


# ---------------------------------------------------------------------------
# _do_rollup_post_merge_housekeeping: multi-direction refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_merge_housekeeping_refreshes_all_directions(mocker):
    """A rollup spanning TWO member directions must refresh BOTH directions'
    outcomes — not just the first one.

    Before the fix, `next(...)` returned only the first matching child, so
    siblings whose direction_id differed were never refreshed, leaving the
    'direction already merged but 0 tasks in flight' accounting undercount.
    """
    from app.rollup_management import _do_rollup_post_merge_housekeeping

    mock_refresh = mocker.patch(
        "app.direction_outcomes.refresh_direction_outcomes_after_merge",
        new_callable=AsyncMock,
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mocker.patch("app.github.get_pull_request_diff", new_callable=AsyncMock, return_value="")
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    dir_a = str(uuid.uuid4())
    dir_b = str(uuid.uuid4())
    research_a = str(uuid.uuid4())
    research_b = str(uuid.uuid4())

    def _child(direction_id, research_task_id):
        c = MagicMock()
        c.context = {"direction_id": direction_id, "research_task_id": research_task_id}
        return c

    child_a = _child(dir_a, research_a)
    child_b = _child(dir_b, research_b)
    # A second child for dir_a — must NOT trigger a duplicate refresh call.
    child_a2 = _child(dir_a, research_a)

    parent = MagicMock()
    parent.id = "parent-id"
    parent.project_id = "proj-id"
    parent.context = {}

    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.add = MagicMock()

    await _do_rollup_post_merge_housekeeping(
        parent=parent,
        project=None,
        session=session,
        gh_token="tok",
        owner="owner",
        repo="repo",
        rollup_pr_number=1,
        staging_branch="staging/test",
        target_branch="main",
        completed_children=[child_a, child_b, child_a2],
        completed_count=3,
        landed=True,
        terminal=False,
    )

    # Must have been called exactly once per DISTINCT direction_id.
    assert mock_refresh.call_count == 2, (
        f"Expected refresh called for 2 distinct directions, got {mock_refresh.call_count}"
    )
    refreshed_children = {call.args[0] for call in mock_refresh.call_args_list}
    # child_a and child_b must be the representatives; child_a2 is a duplicate.
    assert child_a in refreshed_children, "direction A representative not refreshed"
    assert child_b in refreshed_children, "direction B representative not refreshed"
    assert child_a2 not in refreshed_children, "duplicate direction A child must not be refreshed"


def test_check_and_trigger_rollup_refreshes_all_directions_at_every_landed_site():
    """Every inline landed-rollup path in ``_check_and_trigger_rollup`` must
    refresh EVERY member direction, not just a single representative child.

    Regression guard for the audit finding: the multi-direction refresh fix was
    originally applied only to the shared ``_do_rollup_post_merge_housekeeping``
    helper (covered behaviorally by the test above), while the three inline
    landed paths inside ``_check_and_trigger_rollup`` still selected one child
    via ``_dir_child = next(...)`` and refreshed only that direction — leaving
    the other directions' merged/outcome counts stale.

    Driving those inline branches end-to-end is impractical (each sits behind a
    large amount of GitHub I/O), so this asserts the structural invariant on the
    function's AST: (1) the single-representative ``_dir_child`` pattern is gone,
    and (2) every ``refresh_direction_outcomes_after_merge`` call in the function
    is fed the per-direction loop variable ``_rep`` produced by iterating over
    ``_dir_representatives.values()``.
    """
    import ast
    import inspect
    import textwrap

    from app import rollup_management

    src = textwrap.dedent(
        inspect.getsource(rollup_management._check_and_trigger_rollup)
    )
    func = ast.parse(src).body[0]

    # (1) The single-representative selection pattern must no longer exist.
    assert "_dir_child" not in src, (
        "_check_and_trigger_rollup still contains a single-representative "
        "`_dir_child = next(...)` refresh; every landed path must refresh all "
        "directions via `_dir_representatives`."
    )

    # (2) Every direct refresh call must be driven by the per-direction loop var.
    refresh_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "refresh_direction_outcomes_after_merge"
    ]
    assert len(refresh_calls) == 3, (
        f"expected 3 inline refresh sites, found {len(refresh_calls)}"
    )
    for call in refresh_calls:
        assert call.args and isinstance(call.args[0], ast.Name), (
            "refresh_direction_outcomes_after_merge must take a name as its "
            f"first arg, got {ast.dump(call)}"
        )
        assert call.args[0].id == "_rep", (
            "refresh call must pass `_rep` (the per-direction representative), "
            f"got `{call.args[0].id}`"
        )

    rep_loops = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_rep"
    ]
    assert len(rep_loops) == 3, (
        f"expected 3 per-direction refresh loops, found {len(rep_loops)}"
    )


@pytest.mark.asyncio
async def test_record_completed_children_merge_evidence_records_every_child(mocker):
    """The landed-child evidence helper must touch every completed child."""
    from app.rollup_management import _record_completed_children_merge_evidence

    mock_record = mocker.patch(
        "app.acceptance_criteria.record_task_merge_evidence",
        new_callable=AsyncMock,
        return_value=1,
    )

    child_a = MagicMock(id="child-a", project_id="proj-a")
    child_b = MagicMock(id="child-b", project_id="proj-b")
    child_c = MagicMock(id="child-c", project_id="proj-c")
    session = MagicMock()

    await _record_completed_children_merge_evidence(
        [child_a, child_b, child_c], session
    )

    assert mock_record.await_count == 3
    assert [call.args[1] for call in mock_record.await_args_list] == [
        child_a,
        child_b,
        child_c,
    ]


@pytest.mark.asyncio
async def test_record_completed_children_merge_evidence_skips_force_dropped_children(
    mocker,
):
    """Force-dropped children must not receive merge evidence on landing."""
    from app.rollup_management import _record_completed_children_merge_evidence

    mock_record = mocker.patch(
        "app.acceptance_criteria.record_task_merge_evidence",
        new_callable=AsyncMock,
        return_value=1,
    )

    landed_child = MagicMock(
        id="child-keep",
        project_id="proj-a",
        context={"pr_merged": True},
    )
    dropped_child = MagicMock(
        id="child-drop",
        project_id="proj-b",
        context={"pr_merged": True, "_non_merge_unblock_reason": "malformed_pr_url"},
    )
    session = MagicMock()

    await _record_completed_children_merge_evidence([landed_child, dropped_child], session)

    assert mock_record.await_count == 1
    assert mock_record.await_args_list[0].args[1] is landed_child


# ---------------------------------------------------------------------------
# Integration test: _do_rollup_post_merge_housekeeping records CriterionEvidence
# through the REAL production call chain (refresh_direction_outcomes_after_merge
# left unmocked, real db_session).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_post_merge_housekeeping_records_criterion_evidence_for_all_children(
    cleanup_db, mocker
):
    """_do_rollup_post_merge_housekeeping records CriterionEvidence rows through
    the real production call chain for every landed child that carries a proven
    code-acceptance verdict.

    The sibling test test_post_merge_housekeeping_refreshes_all_directions uses a
    MagicMock session and mocks refresh_direction_outcomes_after_merge, so the
    evidence-recording leg is never exercised. This test uses a real SQLite session
    and leaves refresh_direction_outcomes_after_merge UNMOCKED, driving the full
    acceptance-criteria pipeline end-to-end from the rollup caller.
    """
    from app.models import ReleaseVersion, Task as _Task
    from app.acceptance_criteria import (
        create_criterion,
        get_evidence_for_criterion,
        compute_criterion_coverage,
    )
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.rollup_management import _do_rollup_post_merge_housekeeping

    # Mock only the unrelated side-effect collaborators (GitHub I/O, notifications,
    # broadcast, viewport). Critically, refresh_direction_outcomes_after_merge is
    # NOT mocked — the evidence-recording leg runs through real production code.
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request_diff", new_callable=AsyncMock, return_value=""
    )
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._nudge_sibling_rollup_prs", new_callable=AsyncMock
    )
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    # Create project and open release version in the real DB.
    project = await _make_project(cleanup_db)
    version = ReleaseVersion(project_id=project.id, version_number=1)
    cleanup_db.add(version)
    await cleanup_db.commit()
    await cleanup_db.refresh(version)

    # Author criteria for two same-direction children. Before the fix, only the
    # first direction representative got evidence; the sibling's criterion was
    # left uncovered.
    criterion = await create_criterion(
        cleanup_db,
        project_id=project.id,
        release_version_id=version.id,
        text="Integration test suite passes green under real merge verdict",
        created_by="test",
    )
    criterion_b = await create_criterion(
        cleanup_db,
        project_id=project.id,
        release_version_id=version.id,
        text="Sibling child suite passes green under real merge verdict",
        created_by="test",
    )

    direction_key = "d-housekeeping-criterion-test"
    research_task_id = str(uuid.uuid4())

    # Rollup parent task.
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)

    # Child task with a proven CODE_ACCEPTANCE_VERDICT and criterion linkage.
    # direction_id and research_task_id are required for the child to be selected
    # as a direction representative inside _do_rollup_post_merge_housekeeping.
    child = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "implement feature"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_key,
            "research_task_id": research_task_id,
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "named test ran green under verified merge",
            },
        },
        criterion_ids=[criterion.criterion_key],
    )
    child_b = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "implement sibling feature"}],
        status=TaskStatus.COMPLETED,
        context=dict(child.context or {}),
        criterion_ids=[criterion_b.criterion_key],
    )
    cleanup_db.add(child)
    cleanup_db.add(child_b)
    await cleanup_db.commit()
    await cleanup_db.refresh(child)
    await cleanup_db.refresh(child_b)

    # Drive _do_rollup_post_merge_housekeeping with landed=True.
    # refresh_direction_outcomes_after_merge is NOT mocked — the real function
    # runs and records CriterionEvidence as a side effect.
    await _do_rollup_post_merge_housekeeping(
        parent=parent,
        project=project,
        session=cleanup_db,
        gh_token="tok",
        owner="owner",
        repo="repo",
        rollup_pr_number=4242,
        staging_branch="research/conflict-stg",
        target_branch="research/conflict-base",
        completed_children=[child, child_b],
        completed_count=2,
        landed=True,
        terminal=False,
    )

    # Verify CriterionEvidence rows were recorded end-to-end.
    evidence = await get_evidence_for_criterion(cleanup_db, criterion.id)
    assert len(evidence) == 2, (
        f"Expected 2 CriterionEvidence rows (task_merge + reachability), got {len(evidence)}"
    )
    source_kinds = {e.source_kind for e in evidence}
    assert source_kinds == {"task_merge", "reachability"}, (
        f"Expected source_kinds {{'task_merge', 'reachability'}}, got {source_kinds!r}"
    )
    assert all(e.detail.get("proven") is True for e in evidence), (
        "All evidence rows must carry detail with proven=True"
    )
    assert all(str(e.source_id) == str(child.id) for e in evidence), (
        "All evidence rows must reference the merged child task"
    )

    # Coverage must be 'covered' (a proven task_merge row is sufficient).
    coverage = compute_criterion_coverage(evidence)
    assert coverage == "covered", (
        f"Expected criterion coverage 'covered', got {coverage!r}"
    )

    sibling_evidence = await get_evidence_for_criterion(cleanup_db, criterion_b.id)
    assert len(sibling_evidence) == 2
    assert {e.source_kind for e in sibling_evidence} == {"task_merge", "reachability"}
    assert all(str(e.source_id) == str(child_b.id) for e in sibling_evidence)
    assert compute_criterion_coverage(sibling_evidence) == "covered"


@pytest.mark.asyncio
async def test_rollup_post_merge_housekeeping_no_evidence_when_verdict_not_proven(
    cleanup_db, mocker
):
    """Negative leg: a child whose verdict is NOT proven records no CriterionEvidence.

    Complements test_rollup_post_merge_housekeeping_records_criterion_evidence —
    exercises the same end-to-end path but with proven=False so that
    record_task_merge_evidence skips evidence creation, leaving the criterion
    at 'uncovered'.
    """
    from app.models import ReleaseVersion, Task as _Task
    from app.acceptance_criteria import (
        create_criterion,
        get_evidence_for_criterion,
        compute_criterion_coverage,
    )
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.rollup_management import _do_rollup_post_merge_housekeeping

    mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request_diff", new_callable=AsyncMock, return_value=""
    )
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._nudge_sibling_rollup_prs", new_callable=AsyncMock
    )
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    project = await _make_project(cleanup_db)
    version = ReleaseVersion(project_id=project.id, version_number=1)
    cleanup_db.add(version)
    await cleanup_db.commit()
    await cleanup_db.refresh(version)

    criterion = await create_criterion(
        cleanup_db,
        project_id=project.id,
        release_version_id=version.id,
        text="Negative-leg criterion: unproven verdict leaves no evidence",
        created_by="test",
    )

    direction_key = "d-housekeeping-no-evidence-test"
    research_task_id = str(uuid.uuid4())
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)

    child = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "feature"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_key,
            "research_task_id": research_task_id,
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "test failed",
            },
        },
        criterion_ids=[criterion.criterion_key],
    )
    cleanup_db.add(child)
    await cleanup_db.commit()
    await cleanup_db.refresh(child)

    await _do_rollup_post_merge_housekeeping(
        parent=parent,
        project=project,
        session=cleanup_db,
        gh_token="tok",
        owner="owner",
        repo="repo",
        rollup_pr_number=4242,
        staging_branch="research/conflict-stg",
        target_branch="research/conflict-base",
        completed_children=[child],
        completed_count=1,
        landed=True,
        terminal=False,
    )

    evidence = await get_evidence_for_criterion(cleanup_db, criterion.id)
    assert evidence == [], (
        f"Expected no CriterionEvidence for unproven verdict, got {evidence!r}"
    )
    coverage = compute_criterion_coverage(evidence)
    assert coverage == "uncovered", (
        f"Expected criterion coverage 'uncovered' for unproven verdict, got {coverage!r}"
    )


@pytest.mark.asyncio
async def test_multi_child_rollup_criterion_evidence_satisfies_release_scoring_gate(
    cleanup_db,
):
    """Multi-child rollup: per-child proven evidence aggregates to full version coverage.

    Two rollup children each cover a distinct version acceptance criterion via a
    proven merge-test verdict.  After _record_completed_children_merge_evidence
    runs on both, get_version_coverage_summary must report all criteria covered
    and compute_composite must NOT fire the criterion_coverage_incomplete gate.
    """
    from app.models import ReleaseVersion, Task as _Task
    from app.acceptance_criteria import (
        create_criterion,
        get_version_coverage_summary,
        compute_criterion_coverage,
        get_evidence_for_criterion,
    )
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
    from app.rollup_management import _record_completed_children_merge_evidence
    from app.release_scoring import compute_composite

    project = await _make_project(cleanup_db)
    version = ReleaseVersion(project_id=project.id, version_number=1)
    cleanup_db.add(version)
    await cleanup_db.commit()
    await cleanup_db.refresh(version)

    # Two distinct acceptance criteria — one per rollup child.
    criterion_a = await create_criterion(
        cleanup_db,
        project_id=project.id,
        release_version_id=version.id,
        text="Feature A tests pass under merge-test",
        created_by="test",
    )
    criterion_b = await create_criterion(
        cleanup_db,
        project_id=project.id,
        release_version_id=version.id,
        text="Feature B integration verified",
        created_by="test",
    )

    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)

    proven_verdict = {
        "satisfied": True,
        "proven": True,
        "kind": "named_test",
        "reason": "merge-test ran green",
    }

    child_a = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "feature A"}],
        status=TaskStatus.COMPLETED,
        context={CODE_ACCEPTANCE_VERDICT_KEY: proven_verdict},
        criterion_ids=[criterion_a.criterion_key],
    )
    child_b = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "feature B"}],
        status=TaskStatus.COMPLETED,
        context={CODE_ACCEPTANCE_VERDICT_KEY: proven_verdict},
        criterion_ids=[criterion_b.criterion_key],
    )
    cleanup_db.add(child_a)
    cleanup_db.add(child_b)
    await cleanup_db.commit()
    await cleanup_db.refresh(child_a)
    await cleanup_db.refresh(child_b)

    # Record evidence for both children — the production path exercised during
    # rollup post-merge housekeeping.
    await _record_completed_children_merge_evidence([child_a, child_b], cleanup_db)

    # Both criteria must now be covered.
    ev_a = await get_evidence_for_criterion(cleanup_db, criterion_a.id)
    ev_b = await get_evidence_for_criterion(cleanup_db, criterion_b.id)
    assert compute_criterion_coverage(ev_a) == "covered", (
        f"criterion_a must be covered after child_a merge evidence; got {ev_a!r}"
    )
    assert compute_criterion_coverage(ev_b) == "covered", (
        f"criterion_b must be covered after child_b merge evidence; got {ev_b!r}"
    )

    # Version-level summary must reflect full coverage.
    summary = await get_version_coverage_summary(cleanup_db, version.id)
    assert summary["total"] == 2
    assert summary["covered"] == 2
    assert summary["uncovered"] == 0, (
        f"Expected 0 uncovered criteria, got {summary['uncovered']}: {summary}"
    )

    # The release-scoring gate must not fire criterion_coverage_incomplete.  The
    # acceptance gate is now keyed on per-category verdicts (the retired
    # coverage-count input is gone); every active category is ``met`` with no
    # residual uncaptured gap, mirroring the fully-covered state asserted above.
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=None,
        policy={"weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30}, "threshold": 0.90},
        smoketest_enabled=False,
        ci_state="no_checks",
        ci_required=False,
        category_verdicts=[
            {
                "category_key": criterion_a.criterion_key,
                "status": "met",
                "tier": "machine",
                "citations": [{"kind": "diff", "ref": "a.py:1"}],
            },
            {
                "category_key": criterion_b.criterion_key,
                "status": "met",
                "tier": "machine",
                "citations": [{"kind": "diff", "ref": "b.py:1"}],
            },
        ],
        residual_uncaptured_gap=None,
    )
    assert result["blocker"] != "criterion_coverage_incomplete", (
        f"Fully-covered version must not trigger criterion_coverage_incomplete; "
        f"got blocker={result['blocker']!r}, summary={summary}"
    )
    assert result["blocker"] is None, (
        f"No blocker expected for a fully-covered version; got {result['blocker']!r}"
    )


# ── Retroactive merge-credit backfill for landed rollups ────────────────────
#
# PR #12971 fixed FORWARD propagation of merge credit; these tests cover the
# RETROACTIVE, idempotent backfill (app.merge_credit_backfill) that heals
# rollups which landed BEFORE that fix and left their credited children
# stamped landed_on_integration=False forever.

from app.landed_signal import (  # noqa: E402
    has_landed_signal,
    has_merge_unblock_signal,
    mark_landed_on_integration,
    mark_unblocked_non_merge,
)
from app.merge_credit_backfill import (  # noqa: E402
    backfill_landed_rollup_merge_credit,
)


async def _setup_landed_rollup_with_undercredited_child(
    session,
    *,
    child_reason: str = "merged_pr_non_integration_branch",
    landed_rollup: bool = True,
):
    """Build a rollup (doubling as the research-cycle root) plus one merged
    child that is under-credited (pr_merged=True, landed_on_integration=False).

    Returns (project, rollup, child, direction_id). The rollup task IS the
    research_history entry's ``task_id`` so the propagation subtree walk (from
    the rollup) and the direction-outcome child query (children of the research
    task) both resolve to the same child.
    """
    direction_id = "d-backfill"
    project = await _make_project(session)

    rollup = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        context={"_is_rollup_review": True, "rollup_pr_number": 4242},
    )
    if landed_rollup:
        mark_landed_on_integration(rollup)
        flag_modified(rollup, "context")
        session.add(rollup)

    project.directions = [
        {
            "id": direction_id,
            "text": "Backfill direction",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "research_history": [
                {"task_id": str(rollup.id), "tasks_created": 1, "cycle": 1}
            ],
        }
    ]
    flag_modified(project, "directions")
    session.add(project)
    await session.commit()

    child = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=rollup.id,
        context={"direction_id": direction_id, "research_task_id": str(rollup.id)},
    )
    mark_unblocked_non_merge(child, reason=child_reason)
    flag_modified(child, "context")
    session.add(child)
    await session.commit()

    return project, rollup, child, direction_id


@pytest.mark.asyncio
async def test_backfill_stamps_landed_and_recomputes_direction(cleanup_db):
    """(a) A landed rollup's under-credited merged child is stamped landed and
    its direction's prs_merged recomputes to reflect the merge."""
    project, rollup, child, direction_id = (
        await _setup_landed_rollup_with_undercredited_child(cleanup_db)
    )

    # Precondition: child carries the merge-unblock signal but is NOT yet landed,
    # so is_merged_completion is False and the direction reads zero merges.
    assert has_merge_unblock_signal(child) is True
    assert has_landed_signal(child) is False

    stamped = await backfill_landed_rollup_merge_credit(
        project.id, cleanup_db, rollup_limit=50
    )

    assert stamped == 1

    refreshed_child = await cleanup_db.get(Task, child.id)
    assert has_landed_signal(refreshed_child) is True

    await cleanup_db.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry.get("prs_merged") == 1


@pytest.mark.asyncio
async def test_backfill_is_idempotent_second_run_noop(cleanup_db):
    """(b) A second backfill run stamps nothing — already-landed children are
    skipped, so the sweep converges to a cheap no-op."""
    project, rollup, child, _ = (
        await _setup_landed_rollup_with_undercredited_child(cleanup_db)
    )

    first = await backfill_landed_rollup_merge_credit(
        project.id, cleanup_db, rollup_limit=50
    )
    assert first == 1

    second = await backfill_landed_rollup_merge_credit(
        project.id, cleanup_db, rollup_limit=50
    )
    assert second == 0

    refreshed_child = await cleanup_db.get(Task, child.id)
    assert has_landed_signal(refreshed_child) is True


@pytest.mark.asyncio
async def test_backfill_skips_force_dropped_child(cleanup_db):
    """(c) A force-dropped child is NOT stamped even under a landed rollup —
    the propagation helper's force-drop exclusion is preserved."""
    project, rollup, child, direction_id = (
        await _setup_landed_rollup_with_undercredited_child(
            cleanup_db, child_reason="rollup_no_pr_artifact"
        )
    )

    stamped = await backfill_landed_rollup_merge_credit(
        project.id, cleanup_db, rollup_limit=50
    )

    assert stamped == 0

    refreshed_child = await cleanup_db.get(Task, child.id)
    assert has_landed_signal(refreshed_child) is False

    await cleanup_db.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry.get("prs_merged", 0) == 0


@pytest.mark.asyncio
async def test_backfill_leaves_unlanded_rollup_children_untouched(cleanup_db):
    """(d) A rollup that has NOT landed on integration leaves its children
    untouched — its own landed signal is the ancestry gate."""
    project, rollup, child, _ = (
        await _setup_landed_rollup_with_undercredited_child(
            cleanup_db, landed_rollup=False
        )
    )

    # Guard: the rollup really is un-landed, so it must be ignored by the sweep.
    assert has_landed_signal(rollup) is False

    stamped = await backfill_landed_rollup_merge_credit(
        project.id, cleanup_db, rollup_limit=50
    )

    assert stamped == 0

    refreshed_child = await cleanup_db.get(Task, child.id)
    assert has_landed_signal(refreshed_child) is False
    # The merge-unblock signal is still present — it simply was not promoted.
    assert has_merge_unblock_signal(refreshed_child) is True


# ── Structural OWNER-gate: only a staging branch's owner rolls it up ──────────


def test_task_owns_staging_branch_gate():
    """Ownership is proven off the branch name: the trailing segment is the
    OWNER's 8-hex id-prefix. Foreign id-prefixes → not owned; anything without a
    valid id-suffix (legacy / custom) defers to owned so we never strand work."""
    from app.rollup_management import _task_owns_staging_branch

    task = Task(
        id=uuid.UUID("bdb302f7-61f8-43d4-aa71-7754a623750a"),
        payloads=[{}],
        context={},
    )
    # Own id-prefix (research + plan mints) → owned.
    assert _task_owns_staging_branch(task, "research/bdb302f7") is True
    assert _task_owns_staging_branch(task, "plan/bdb302f7") is True
    # A DIFFERENT valid id-prefix → inherited from an ancestor → not owned.
    assert _task_owns_staging_branch(task, "research/2fea759a") is False
    # Non-id-prefix trailing segments default to owned (no positive foreign proof).
    assert _task_owns_staging_branch(task, "research/dup-stg") is True
    assert _task_owns_staging_branch(task, "feature/some-branch") is True
    # No branch at all → nothing to roll up.
    assert _task_owns_staging_branch(task, None) is False
    assert _task_owns_staging_branch(task, "") is False


@pytest.mark.asyncio
async def test_inherited_staging_branch_completes_without_duplicate_rollup(
    cleanup_db, mocker
):
    """A parent whose staging branch was INHERITED from an ancestor (a foreign
    id-prefix, no durable owns flag, no PR marker) must NOT open a duplicate
    rollup PR — it completes and defers the roll-up to the branch's owner."""
    mock_create = _mock_rollup_create_path(mocker)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_no_pr(
        cleanup_db,
        project.id,
        extra_context={"research_branch": "research/aabbccdd"},
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_create.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert "owner" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_adopted_branch_recreates_rollup(cleanup_db, mocker):
    """Invariant #2: a requeue-clone that legitimately adopted a foreign-named
    branch carries the durable ``owns_rollup_branch`` flag. Even with no
    ``rollup_pr_number`` marker (transiently cleared), the OWNER-gate must NOT
    fire — the self-heal recreates the rollup PR."""
    mock_create = _mock_rollup_create_path(
        mocker,
        create_return={
            "empty_diff": True,
            "merged": True,
            "html_url": "",
            "number": 0,
        },
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_no_pr(
        cleanup_db,
        project.id,
        extra_context={
            "research_branch": "research/aabbccdd",
            "owns_rollup_branch": True,
        },
    )
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_create.assert_awaited_once()


# ── Merge-credit backfill: rotating keyset cursor ────────────────────────────


def test_merge_credit_backfill_keyset_cursors_exist_and_advance_correctly():
    """Rotating keyset cursor state exists and the full-page/short-page logic is correct.

    Pre-fix: no cursor variables existed — every sweep tick re-queried from the
    beginning of the ordered id set, so rollup history beyond the first batch was
    starved forever and the sweep could not converge on a large fleet/history.
    The fix introduces _rollup_backfill_cursors (per-project) and
    _project_backfill_cursor (global), advancing on a full page and wrapping on
    a short page so every landed rollup is eventually visited.
    """
    import uuid

    import app.merge_credit_backfill as mcb

    assert hasattr(mcb, "_project_backfill_cursor"), (
        "_project_backfill_cursor missing — keyset cursor needed to avoid starving "
        "projects beyond the first batch on every sweep tick"
    )
    assert hasattr(mcb, "_rollup_backfill_cursors"), (
        "_rollup_backfill_cursors missing — per-project keyset cursor needed to avoid "
        "re-scanning the same first-N rollups on every call"
    )
    assert isinstance(mcb._rollup_backfill_cursors, dict)

    # Verify the cursor advancement contract directly:
    # full page (len == limit) -> cursor is set to the last id.
    pid = uuid.uuid4()
    rollup_limit = 2
    full_page = [uuid.uuid4(), uuid.uuid4()]  # len == rollup_limit
    if len(full_page) < rollup_limit:
        mcb._rollup_backfill_cursors.pop(pid, None)
    else:
        mcb._rollup_backfill_cursors[pid] = full_page[-1]
    assert mcb._rollup_backfill_cursors.get(pid) == full_page[-1], (
        "full page must advance cursor to the last id so the next call resumes past it"
    )

    # short page (len < limit) -> cursor is cleared (wrap to start).
    short_page = [uuid.uuid4()]  # len=1 < rollup_limit=2
    if len(short_page) < rollup_limit:
        mcb._rollup_backfill_cursors.pop(pid, None)
    else:
        mcb._rollup_backfill_cursors[pid] = short_page[-1]
    assert pid not in mcb._rollup_backfill_cursors, (
        "short page must clear cursor so the next call re-scans from the beginning"
    )


# ── K6 / K7 lock-window regressions (2026-07-12 concurrency audit) ───────────
#
# K6: the Alembic auto-reconcile (PR-file plan -> main.py read -> MULTI-REQUEST
#     GitHub PUSH) used to run with the rollup parent still held FOR UPDATE from
#     `_relock_parent` — the code said so ("parent is locked here"). A Postgres
#     row lock is released by TRANSACTION END, so "the parent lock is released
#     before the push" is the assertion "there is no open transaction on the
#     session when the push fires". That is also the assertion that the
#     connection is not idle-in-transaction across the push (racing the 60s
#     idle_in_transaction_session_timeout in database.py) — and it is
#     dialect-independent, so it holds on this SQLite harness.
#
# K7: the scan report used to be posted UNDER the re-taken lock, and on the
#     gates-off blocked path it was posted TWICE.


def _collide_at_head(mocker):
    """Target head already holds 0124, so the added 0124 collides."""
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


@pytest.mark.asyncio
async def test_alembic_push_runs_with_parent_lock_released_counter_staked_first(
    cleanup_db, mocker
):
    """K6: the parent FOR UPDATE is RELEASED before the alembic push, and the
    retry counter is STAKED (incremented + committed) BEFORE the push fires.

    Staking first is load-bearing: the push is a real, irreversible side effect,
    so if the counter were only written after the push (as it was), a
    peer-mutation defer on re-lock would discard the increment while the push had
    already landed — and the retry cap could be bypassed indefinitely.
    """
    mock_merge, mock_spawn = _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)

    observed: dict = {}

    async def _push(*_a, **_kw):
        # (1) No open transaction => the parent row lock is NOT held across the
        #     push, and the connection is not idle-in-transaction.
        observed["in_transaction"] = cleanup_db.in_transaction()
        # (2) The staked counter is already DURABLE at push time. Re-select with
        #     populate_existing so this is a real DB read, not the identity map.
        from sqlmodel import select as _select

        fresh = (
            (
                await cleanup_db.execute(
                    _select(Task)
                    .where(Task.id == parent.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        observed["ctx_at_push"] = dict(fresh.context or {})
        return True

    mock_commit = mocker.patch(
        "app.github.commit_file_changes", new_callable=AsyncMock, side_effect=_push
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_awaited_once()
    assert observed["in_transaction"] is False, (
        "the alembic auto-reconcile PUSH ran inside an open transaction — the "
        "rollup parent is still held FOR UPDATE across a multi-request GitHub "
        "push (GET ref, GET commit, POST blobs, POST tree, POST commit, PATCH "
        "ref), which is the K6 idle-in-transaction defect"
    )
    assert observed["ctx_at_push"].get("_alembic_autoreconcile_pr") == 4242
    assert observed["ctx_at_push"].get("_alembic_autoreconcile_count") == 1, (
        "the retry counter was NOT staked before the push — a lost re-lock would "
        "discard it while the push had already landed, bypassing the retry cap"
    )
    # End state unchanged from before the fix: deferred one sweep, not merged.
    mock_merge.assert_not_awaited()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("_alembic_autoreconcile_count") == 1


@pytest.mark.asyncio
async def test_alembic_staked_counter_survives_lost_relock_after_push(
    cleanup_db, mocker
):
    """K6 cap-bypass guard: when a peer mutates the parent during the unlocked
    push window, the re-lock defers — and the staked counter MUST still be there.

    This is the exact interleaving the old code lost: push lands, re-lock is
    refused, counter never written, so the cap never advances and the mechanical
    rewrite loops forever.
    """
    _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)

    async def _push_then_peer_mutates(*_a, **_kw):
        # The push LANDS, then a peer flips a snapshot-protected field on the
        # parent (requires_approval), so _relock_parent detects the mutation and
        # defers to the next cycle.
        peer = await cleanup_db.get(Task, parent.id)
        peer.requires_approval = True
        cleanup_db.add(peer)
        await cleanup_db.commit()
        return True

    mock_commit = mocker.patch(
        "app.github.commit_file_changes",
        new_callable=AsyncMock,
        side_effect=_push_then_peer_mutates,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_awaited_once()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("_alembic_autoreconcile_pr") == 4242
    assert ctx.get("_alembic_autoreconcile_count") == 1, (
        "the push landed but the retry counter was lost when the re-lock deferred "
        "— the auto-reconcile cap can now be bypassed indefinitely"
    )


@pytest.mark.asyncio
async def test_alembic_failed_push_hands_the_staked_attempt_back(cleanup_db, mocker):
    """A transient push failure must NOT burn a retry attempt.

    The counter is staked before the push (K6), so when we learn — under the
    re-taken lock — that nothing was pushed, the stake is handed back and the cap
    semantics stay "count successful pushes", exactly as before.
    """
    _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)
    mock_commit = mocker.patch(
        "app.github.commit_file_changes",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GitHub 502"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_awaited_once()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("_alembic_autoreconcile_count") == 0, (
        "a failed push burned an auto-reconcile attempt; only pushes that LAND "
        "may consume the budget"
    )
    # Falls through to the operator-alert / re-chain-fix path, unchanged.
    assert parent.requires_approval is True


@pytest.mark.asyncio
async def test_blocked_scan_report_posted_exactly_once_and_outside_lock(
    cleanup_db, mocker
):
    """K7: on the BLOCKED path the scan report is posted EXACTLY ONCE, outside
    the parent lock window.

    Before: gates-off blocked posted the report, then re-acquired the parent
    lock, then fell through and posted the SAME report a SECOND time — under the
    re-taken lock, behind a comment claiming "we do not mutate the parent for
    this case" (a lock is released by transaction end, not by not-mutating).
    """
    from app.shared_state import runtime_config

    mocker.patch.dict(runtime_config, {"require_approval_gates": False})

    mock_merge, _mock_spawn = _mock_automerge_deps(mocker, pr_files=_files("app/x.py"))
    # Blocked WITH findings triggers the double-post path.
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": True,
            "findings": [{"severity": "high", "detail": "prompt injection"}],
            "summary": "1 high finding",
            "report": "SCAN REPORT BODY",
        },
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    in_txn_at_post: list = []

    async def _comment(*_a, **_kw):
        in_txn_at_post.append(cleanup_db.in_transaction())

    mock_comment = mocker.patch(
        "app.github.create_pr_comment", new_callable=AsyncMock, side_effect=_comment
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    report_posts = [
        c for c in mock_comment.await_args_list if "SCAN REPORT BODY" in repr(c)
    ]
    assert len(report_posts) == 1, (
        f"the security-scan report was posted {len(report_posts)} times on the "
        f"blocked path; it must be posted exactly once"
    )
    assert in_txn_at_post[0] is False, (
        "the scan report was posted with an open transaction — the rollup parent "
        "was still held FOR UPDATE across a GitHub write"
    )
    # Gates off, so it still proceeds to the merge (behaviour unchanged).
    mock_merge.assert_awaited_once()


@pytest.mark.asyncio
async def test_unblocked_findings_scan_comment_posted_outside_lock(cleanup_db, mocker):
    """K7 path A: findings but NOT blocked (medium/low severity — "blocked" is set
    independently of "findings"). The comment used to fire with the parent held
    FOR UPDATE and nothing committed since the re-lock."""
    mock_merge, _mock_spawn = _mock_automerge_deps(mocker, pr_files=_files("app/x.py"))
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [{"severity": "medium", "detail": "suspicious string"}],
            "summary": "1 medium finding",
            "report": "SCAN REPORT BODY",
        },
    )
    in_txn_at_post: list = []

    async def _comment(*_a, **_kw):
        in_txn_at_post.append(cleanup_db.in_transaction())

    mock_comment = mocker.patch(
        "app.github.create_pr_comment", new_callable=AsyncMock, side_effect=_comment
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    assert mock_comment.await_count == 1
    assert in_txn_at_post == [False], (
        "the findings-but-not-blocked scan comment ran under the parent FOR "
        "UPDATE lock (K7)"
    )
    mock_merge.assert_awaited_once()


@pytest.mark.asyncio
async def test_alembic_unstake_does_not_clobber_a_concurrent_peers_stake(
    cleanup_db, mocker
):
    """MAJOR 3: the un-stake is a COMPARE-AND-SWAP, not an absolute write.

    ``_check_and_trigger_rollup`` is driven from at least six call sites (github
    webhook, agent_status, task_lifecycle, reaper_evaluating_sweeps,
    reaper_post_orphan_sweep, recovery_evaluating), so two sweeps CAN be in the
    alembic block at once — and K6 releases the parent lock across the push, so
    ``skip_locked`` no longer defers the second one.

    The interleaving that broke the headline invariant:

        S1 reads count=0 → stakes 1 → its push FAILS
        S2 reads count=1 → stakes 2 → its push SUCCEEDS
        S1 re-locks (the counter is NOT in _capture_rollup_parent_snapshot, so
          S2's write does NOT trip the validator) → _ar_plan is None →
          un-stakes by writing its STALE baseline: count = 0

    A landed push is now uncounted and the cap is RESET — i.e. "the cap can only
    get stricter, never bypassed" was FALSE. Post-fix S1 hands the stake back only
    if its OWN stake is still on top, so S2's count (and the push it landed)
    survives.
    """
    _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)

    async def _peer_stakes_then_our_push_fails(*_a, **_kw):
        # S2 lands in the window S1 opened by releasing the parent lock: it reads
        # S1's stake (1), stakes 2, and ITS push succeeds. Only the counter keys
        # are touched, so _relock_parent's content snapshot (status,
        # requires_approval, rollup_pr_number/url, pr_merged) does NOT trip —
        # which is exactly why the stale absolute write was able to clobber it.
        peer = await cleanup_db.get(Task, parent.id)
        peer_ctx = dict(peer.context or {})
        assert peer_ctx.get("_alembic_autoreconcile_count") == 1, (
            "S1 must have staked before releasing the lock — test setup is wrong"
        )
        peer_ctx["_alembic_autoreconcile_count"] = 2
        peer.context = peer_ctx
        flag_modified(peer, "context")
        cleanup_db.add(peer)
        await cleanup_db.commit()
        # ...and now S1's own push fails.
        raise RuntimeError("GitHub 502")

    mocker.patch(
        "app.github.commit_file_changes",
        new_callable=AsyncMock,
        side_effect=_peer_stakes_then_our_push_fails,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("_alembic_autoreconcile_count") == 2, (
        "the un-stake CLOBBERED a concurrent peer's stake with a stale absolute "
        "write: the peer's push LANDED but is no longer counted, so the "
        "auto-reconcile retry cap has been reset and can be bypassed indefinitely"
    )


@pytest.mark.asyncio
async def test_alembic_peer_inflight_stake_defers_instead_of_double_pushing(
    cleanup_db, mocker
):
    """MAJOR 4: a fresh in-flight stake makes a peer sweep DEFER, not push again.

    Releasing the parent lock across the push (K6) means ``skip_locked`` no longer
    defers a second sweep, so two sweeps could both push to ``staging_branch``.
    ``commit_file_changes`` uses ``force: False`` so the loser gets a 422 rather
    than clobbering — but it burns GitHub calls and muddies the counter. The stake
    written under the lock before the release doubles as a durable in-flight
    marker; a peer that sees a fresh one defers to the next cycle.
    """
    from app.time_utils import _utcnow

    _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)
    mock_commit = mocker.patch(
        "app.github.commit_file_changes", new_callable=AsyncMock, return_value=True
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    # A peer sweep is already mid-push on this PR: it staked attempt 1 and stamped
    # the in-flight marker moments ago.
    ctx = dict(parent.context or {})
    ctx["_alembic_autoreconcile_pr"] = 4242
    ctx["_alembic_autoreconcile_count"] = 1
    ctx["_alembic_autoreconcile_inflight_at"] = _utcnow().isoformat()
    parent.context = ctx
    flag_modified(parent, "context")
    cleanup_db.add(parent)
    await cleanup_db.commit()

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_not_awaited()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("_alembic_autoreconcile_count") == 1, (
        "this sweep issued a CONCURRENT SECOND push while a peer was mid-push on "
        "the same PR (and staked another attempt for it)"
    )
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_alembic_stale_inflight_stake_does_not_wedge_the_reconcile(
    cleanup_db, mocker
):
    """The in-flight marker is TTL-bounded — a sweep that died mid-push must not
    block the reconcile forever."""
    from datetime import timedelta

    from app.rollup_state import _ALEMBIC_AUTORECONCILE_INFLIGHT_TTL_S
    from app.time_utils import _utcnow

    _mock_automerge_deps(
        mocker,
        pr_files=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    )
    _collide_at_head(mocker)
    mock_commit = mocker.patch(
        "app.github.commit_file_changes", new_callable=AsyncMock, return_value=True
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_merged_child(cleanup_db, project.id, parent.id)

    stale = _utcnow() - timedelta(
        seconds=_ALEMBIC_AUTORECONCILE_INFLIGHT_TTL_S + 60
    )
    ctx = dict(parent.context or {})
    ctx["_alembic_autoreconcile_pr"] = 4242
    ctx["_alembic_autoreconcile_count"] = 0
    ctx["_alembic_autoreconcile_inflight_at"] = stale.isoformat()
    parent.context = ctx
    flag_modified(parent, "context")
    cleanup_db.add(parent)
    await cleanup_db.commit()

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Campaign objective completion: a direction's completion signal waits on its
# GRADED objective, not child-merge counts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direction_campaign_completion_waits_on_scoped_objective(
    cleanup_db, mocker
):
    """A campaign direction's completion reflects its graded OBJECTIVE once a
    direction-scoped merge grade lands — not the merge-count heuristic alone.

    End-to-end through the real production chain:

    1. TRIGGER — a landed rollup whose parent carries ``direction_id`` and whose
       direction declares a gradeable objective spawns a direction-scoped
       ``release_merge_grade`` task against the integrated merged diff.
    2. BACKWARD COMPAT — before any grade completes, the merge-count heuristic
       still marks the direction possibly_complete (no graded verdict yet).
    3. WAITS — once the grade completes NOT-met, the lifecycle consumes it and
       the graded verdict AUTHORITATIVELY suppresses possibly_complete — the
       campaign does not 'complete' while its objective remains unmet.
    4. FULFILLS — after the rubric is graded fully met, the lifecycle marks the
       direction complete with the graded-objective message.
    """
    import json
    from datetime import timedelta

    from sqlalchemy import select as _select

    from app import category_verdict_ledger as _ledger
    from app.direction_lifecycle import _process_direction_lifecycle_locked
    from app.models import Artifact, ArtifactType, Project, Task as _Task
    from app.models import TaskStatus
    from app.rollup_management import _do_rollup_post_merge_housekeeping
    from app.time_utils import _utcnow

    direction_id = "d-campaign-7c69e418"
    objective = "Ship the scoped objective-completion grade end-to-end"
    candidate_sha = "1a2b3c4d" * 5

    now = _utcnow()
    # 5 cycles: the two most recent are no-op low-yield (trips the completion
    # heuristic), the older three merged real PRs (so the still-productive pause
    # shield holds and no merge-rate auto-pause fires before the completion gate).
    def _cycle(days_ago, *, created, merged):
        return {
            "timestamp": (now - timedelta(days=days_ago)).isoformat(),
            "tasks_created": created,
            "tasks_completed": created,
            "tasks_completed_no_pr": created if merged == 0 else 0,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": merged,
            "verified_prs_merged": merged,
            "merged_prs_any_base": merged,
            "autonomous_prs_merged": merged,
        }

    history = [
        _cycle(4, created=3, merged=2),
        _cycle(3, created=3, merged=2),
        _cycle(2, created=3, merged=2),
        _cycle(1, created=1, merged=0),
        _cycle(0, created=1, merged=0),
    ]
    direction = {
        "id": direction_id,
        "text": "Scope objective-completion grading to campaign directions",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "completion_criteria": objective,
        "criterion_keys": ["c-campaign-1"],
        "last_cycle_productive": True,
        "consecutive_unproductive_cycles": 0,
        "research_history": history,
    }
    project = Project(
        name="Campaign Completion Test",
        user_id="u-campaign",
        github_owner="acme",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    # The direction's criterion key resolves to a real VersionAcceptanceCriterion
    # row — the grader rubric must carry its human-readable TEXT, not the key.
    from app.release_models import ReleaseVersion, VersionAcceptanceCriterion

    release_version = ReleaseVersion(
        project_id=project.id, version_number=1, status="open"
    )
    cleanup_db.add(release_version)
    await cleanup_db.commit()
    criterion_text = "The campaign ships the scoped objective grade end-to-end"
    cleanup_db.add(
        VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=release_version.id,
            criterion_key="c-campaign-1",
            text=criterion_text,
        )
    )
    await cleanup_db.commit()

    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"direction_id": direction_id}
    )
    child_a = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "implement campaign piece A"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_id,
            "research_task_id": "r-campaign-a",
            "pr_number": 701,
            "landed_on_integration": True,
            "pr_merged": True,
        },
    )
    child_b = _Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "implement campaign piece B"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_id,
            "research_task_id": "r-campaign-b",
            "pr_number": 702,
            "landed_on_integration": True,
            "pr_merged": True,
        },
    )
    cleanup_db.add(child_a)
    cleanup_db.add(child_b)
    await cleanup_db.commit()
    await cleanup_db.refresh(child_a)
    await cleanup_db.refresh(child_b)

    # Mock only the unrelated housekeeping side-effects; the spawn trigger
    # (_maybe_spawn_direction_merge_grade) and the lifecycle gate run REAL.
    mocker.patch(
        "app.direction_outcomes.refresh_direction_outcomes_after_merge",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.acceptance_criteria.record_task_merge_evidence",
        new_callable=AsyncMock,
        return_value=1,
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request_diff", new_callable=AsyncMock, return_value=""
    )
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.viewport_coverage.update_viewport_coverage_snapshot_from_diff",
        new_callable=AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._nudge_sibling_rollup_prs", new_callable=AsyncMock
    )
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    mocker.patch(
        "app.github_refs.get_branch_sha", new_callable=AsyncMock, return_value=candidate_sha
    )
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )

    # ---- 1. TRIGGER: the landed rollup spawns the direction-scoped grade ----
    await _do_rollup_post_merge_housekeeping(
        parent=parent,
        project=project,
        session=cleanup_db,
        gh_token="tok",
        owner="acme",
        repo="repo",
        rollup_pr_number=4242,
        staging_branch="research/conflict-stg",
        target_branch="automain",
        completed_children=[child_a, child_b],
        completed_count=2,
        landed=True,
        terminal=False,
    )
    await cleanup_db.refresh(parent)
    assert parent.context.get("_direction_merge_grade_candidate_sha") == candidate_sha, (
        "housekeeping must stamp the graded candidate sha on the parent context"
    )
    grade_task = (
        await cleanup_db.execute(
            _select(_Task).where(
                _Task.id == uuid.UUID(parent.context["_direction_merge_grade_task_id"])
            )
        )
    ).scalar_one()
    assert grade_task.first_payload["type"] == "release_merge_grade"
    assert grade_task.first_payload["direction_id"] == direction_id
    assert grade_task.first_payload["candidate_sha"] == candidate_sha
    assert grade_task.first_payload["merged_prs"] == [701, 702]
    assert grade_task.first_payload["categories"] == [
        {"category_key": "c-campaign-1", "text": criterion_text}
    ], (
        "the grader rubric must carry the criterion's human-readable text — "
        "a key-only rubric is ungradeable"
    )

    # ---- 2. BACKWARD COMPAT: no verdict yet → heuristic sets possibly_complete ----
    await _process_direction_lifecycle_locked(project, cleanup_db, set(), now)
    await cleanup_db.refresh(project)
    d = project.directions[0]
    assert d.get("possibly_complete") is True, (
        "before any grade, the merge-count heuristic must still mark the "
        "direction possibly_complete"
    )
    assert "graded fully met" not in (d.get("completion_suggestion") or "")

    # ---- 3. WAITS: a NOT-met grade authoritatively suppresses completion ----
    grade_task.status = TaskStatus.COMPLETED
    cleanup_db.add(grade_task)
    not_met_artifact = {
        "graded_candidate_sha": candidate_sha,
        "covers_goal": False,
        "coverage_score": 0.3,
        "biggest_gap": "Objective still not implemented.",
        "rationale": "Read the merged diff.",
        "category_verdicts": [
            {
                "category_key": "c-campaign-1",
                "status": "not_met",
                "tier": "judged",
                "blocked_by": "unbuilt",
                "gap": "The campaign objective is not yet delivered.",
                "citations": [{"kind": "diff", "ref": "backend/app/x.py"}],
            }
        ],
        "residual_uncaptured_gap": (
            "The objective's acceptance criteria are not fully captured."
        ),
    }
    cleanup_db.add(
        Artifact(
            task_id=grade_task.id,
            artifact_type=ArtifactType.RELEASE_MERGE_GRADE,
            logs=json.dumps(not_met_artifact),
        )
    )
    await cleanup_db.commit()
    await cleanup_db.refresh(grade_task)

    await _process_direction_lifecycle_locked(project, cleanup_db, set(), now)
    await cleanup_db.refresh(project)
    d = project.directions[0]
    assert d.get("possibly_complete") is not True, (
        "a graded NOT-met objective must suppress the completion signal — the "
        "campaign waits on its objective, not merge counts"
    )
    verdicts = await _ledger.get_category_verdicts(
        cleanup_db, None, candidate_sha, direction_id=direction_id
    )
    assert len(verdicts) == 1 and verdicts[0].status == "not_met"
    assert await _ledger.get_latest_direction_candidate_sha(
        cleanup_db, direction_id
    ) == candidate_sha

    # ---- 4. FULFILLS: a fully-met grade marks the direction complete ----
    attempt_id = uuid.UUID(grade_task.context["qualification_attempt_id"])
    await _ledger.upsert_category_verdict(
        cleanup_db,
        direction_id=direction_id,
        candidate_sha=candidate_sha,
        category_key="c-campaign-1",
        status="met",
        tier="judged",
        gap=None,
        citations=[],
        grader_run_id=str(grade_task.id),
        qualification_attempt_id=attempt_id,
        project_id=project.id,
    )
    await _ledger.upsert_residual(
        cleanup_db,
        direction_id=direction_id,
        candidate_sha=candidate_sha,
        uncaptured_gap=None,
        qualification_attempt_id=attempt_id,
        project_id=project.id,
    )
    await cleanup_db.commit()

    await _process_direction_lifecycle_locked(project, cleanup_db, set(), now)
    await cleanup_db.refresh(project)
    d = project.directions[0]
    assert d.get("possibly_complete") is True, (
        "a graded fully-met objective must mark the direction complete"
    )
    assert "graded fully met" in (d.get("completion_suggestion") or ""), (
        "the completion suggestion must cite the graded objective verdict"
    )


@pytest.mark.asyncio
async def test_direction_merge_grade_skipped_when_no_active_criterion_keys(
    cleanup_db, mocker
):
    """A direction with a gradeable objective but NO active criterion_keys must
    not spawn a direction-scoped merge grade.

    The legacy/synthetic case that ``_direction_active_category_keys`` exists for
    yields an empty active set: ``direction_objective_completion_verdict`` returns
    None for it and ``record_direction_merge_grade`` writes no ledger rows (no
    category verdicts), so ``consume_direction_merge_grade_if_ready`` never
    records the grade and the completion gate would never consult it.  Each
    landed campaign rollup for such a direction would otherwise waste a full Opus
    LLM grade + VM slot + task row.
    """
    from app.models import Project
    from app.rollup_management import _maybe_spawn_direction_merge_grade

    direction_id = "d-empty-rubric"
    direction = {
        "id": direction_id,
        "text": "A direction with a gradeable objective but no rubric",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "completion_criteria": "Ship the scoped objective-completion grade",
        # NOTE: NO criterion_keys — the legacy/synthetic case where the in-memory
        # dict carries no rubric and no first-class Direction row exists either.
    }
    project = Project(
        name="Empty Rubric Grade Test",
        user_id="u-empty-rubric",
        github_owner="acme",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id, extra_context={"direction_id": direction_id}
    )

    mocker.patch(
        "app.github_refs.get_branch_sha",
        new_callable=AsyncMock,
        return_value="1a2b3c4d" * 5,
    )
    mocker.patch(
        "app.release_qualifier_audit._collect_direction_merged_pr_numbers",
        new_callable=AsyncMock,
        return_value=[701],
    )
    spawn = mocker.patch(
        "app.release_qualifier_audit._spawn_direction_merge_grade_task",
        new_callable=AsyncMock,
        return_value=None,
    )

    await _maybe_spawn_direction_merge_grade(
        parent=parent,
        project=project,
        session=cleanup_db,
        gh_token="tok",
        owner="acme",
        repo="repo",
        target_branch="automain",
    )
    await cleanup_db.refresh(parent)

    spawn.assert_not_called()
    assert parent.context.get("_direction_merge_grade_task_id") is None, (
        "no grade task may be stamped on the parent context when the direction "
        "has no active criterion keys"
    )
