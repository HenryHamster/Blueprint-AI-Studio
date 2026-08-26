"""Regression -- 2026-05-26 incident on PR #6096.

A pre-merge "is the rollup PR behind base?" check used to live in
``rollup_management._check_and_trigger_rollup`` and was gated on
``target_tip_sha != head_sha``. That gate is always true for any PR with
commits (the PR head is by definition >=1 commit ahead of base), so it
triggered ``update_branch`` on EVERY sweep -> GitHub returned 422
"no new commits on base branch" -> counted as failure -> after
``rollup_behind_update_retries=3`` sweeps it spawned a fix task with the
false premise that the PR was behind. Three independent agents confirmed
via ``git`` that PR #6096 was NOT behind.

The authoritative "is it behind?" check uses GitHub's
``mergeable_state == "behind"`` and lives on the post-merge path. These
tests pin that the pre-merge SHA-equality block is gone: a PR that is
``mergeable_state="clean"`` with ``head_sha != base_tip_sha`` must NOT
trigger ``update_branch`` and must NOT spawn a fix task.
"""

import pytest
from unittest.mock import AsyncMock, create_autospec

from app.rollup_size_preflight import SizeCapPreflightResult

from app.github import get_branch_sha as _real_get_branch_sha
from app.models import TaskStatus
from testing.test_rollup_cleanup import (
    _make_project,
    _make_rollup_parent_with_pr,
    _make_task,
)

pytest_plugins = ["testing.test_rollup_cleanup"]


def _mock_clean_premerge_deps(mocker, *, head_sha, base_tip_sha):
    """Mocks that drive the resume path to the merge call with a PR whose
    head differs from base tip (commits ahead of base -- the normal case)
    AND ``mergeable_state="clean"`` (not "behind")."""
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
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    # autospec on the real get_branch_sha enforces the (token, owner, repo,
    # branch) signature so we can assert it was NOT called as a pre-merge
    # tip-fetch (the removed block called it; the new flow does not).
    mock_tip = create_autospec(_real_get_branch_sha, return_value=base_tip_sha)
    mocker.patch("app.github.get_branch_sha", new=mock_tip)
    # get_pull_request fires on the resume, diff-size, and final exact-target
    # paths. head_sha
    # differs from base_tip_sha -- i.e. the PR has commits ahead of base,
    # the normal happy-path state for any PR with work in it.
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
                "mergeable_state": "clean",
                "base": {
                    "sha": base_tip_sha,
                    "ref": "research/conflict-base",
                },
                "head": {"sha": head_sha, "ref": "research/conflict-stg"},
            },
            {
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "base": {
                    "sha": base_tip_sha,
                    "ref": "research/conflict-base",
                },
                "head": {"sha": head_sha, "ref": "research/conflict-stg"},
            },
        ],
    )
    async def _bound_ok(
        *_args,
        expected_head_sha=None,
        target_branch=None,
        expected_base_sha=None,
        **_kwargs,
    ):
        # Mirror preflight_rollup_size_caps: a real verdict is stamped with
        # the exact head, base, and target it evaluated. A double that binds
        # only the head models a retired one-sided contract and is refused
        # by the merge chokepoint's is_bound_to() check.
        return SizeCapPreflightResult(
            "ok",
            None,
            expected_head_sha,
            target_branch=target_branch,
            base_sha=expected_base_sha,
        )

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        side_effect=_bound_ok,
    )
    return mock_tip


@pytest.mark.asyncio
async def test_clean_pr_with_head_ahead_of_base_does_not_trigger_update_branch(
    cleanup_db, mocker
):
    """A PR whose head SHA differs from base tip is in the NORMAL state for
    any PR with commits -- head is by definition ahead of base. The removed
    pre-merge SHA-equality gate falsely treated this as "behind" and called
    ``update_branch``, getting 422 "no new commits on base" -> fake failure
    -> fake fix spawn. Verifies the gate is gone.

    mergeable_state="clean" (the merge happens) -> no behind handling at all,
    no update_branch on the pre-merge path, no fix-task spawn from a phantom
    behind state.
    """
    _mock_clean_premerge_deps(mocker, head_sha="deadbeef", base_tip_sha="abc123")
    mock_update = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": True, "message": "ok"},
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": "merged-sha"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
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

    # The merge call itself fires -- the removed gate would have intercepted
    # this and called update_branch first.
    mock_merge.assert_awaited_once()
    # update_branch must NOT fire on the pre-merge path. The post-merge
    # mergeable_state="behind" path is the only legitimate caller, and the
    # merge succeeded here so that path is never reached either.
    mock_update.assert_not_called()
    # No phantom fix spawn from the false-positive "behind" classification.
    mock_spawn.assert_not_called()
    # Behind-retry counter must NOT have been written -- there was no behind
    # state to count.
    await cleanup_db.refresh(parent)
    assert "_behind_update_retries" not in (parent.context or {})


@pytest.mark.asyncio
async def test_failed_merge_with_clean_state_does_not_trigger_update_branch(
    cleanup_db, mocker
):
    """Even when the merge call returns merged=False, if the post-merge
    refetch reports mergeable_state="clean" (no behind state), update_branch
    must not fire and no fix task is spawned from a behind premise -- the
    flow continues to the non-conflict deferral branch.
    """
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
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mock_tip = create_autospec(_real_get_branch_sha, return_value="abc123")
    mocker.patch("app.github.get_branch_sha", new=mock_tip)
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
                "head": {"sha": "deadbeef"},
            },
            # Post-merge refetch: head ahead of base, but mergeable_state is
            # "clean" -- the merge_pull_request 503/network failure was the
            # reason for merged=False, not a real conflict-or-behind state.
            {
                "mergeable_state": "clean",
                "head": {"sha": "deadbeef"},
            },
        ],
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "transient merge call failure"},
    )
    mock_update = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": True, "message": "ok"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
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

    # mergeable_state="clean" never reaches the post-merge behind branch,
    # so update_branch is never called on this path either.
    mock_update.assert_not_called()
    # No fix task spawned -- "clean" is a non-conflict, non-behind state and
    # falls through to the deferral path, not fix-spawn.
    mock_spawn.assert_not_called()
    # No behind-retry counter written.
    await cleanup_db.refresh(parent)
    assert "_behind_update_retries" not in (parent.context or {})
