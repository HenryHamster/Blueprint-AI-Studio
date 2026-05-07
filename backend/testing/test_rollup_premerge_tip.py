import pytest
from unittest.mock import AsyncMock

from app.models import TaskStatus
from testing.test_rollup_cleanup import (
    _make_project,
    _make_rollup_parent_with_pr,
    _make_task,
    _mock_rollup_merge_conflict_deps,
)

pytest_plugins = ["testing.test_rollup_cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head_sha,target_tip,update_result,expect_update,expect_retry",
    [
        ("abc123", "abc123", None, False, None),
        ("deadbeef", "abc123", {"updated": True, "message": "ok"}, True, None),
        ("deadbeef", "abc123", {"updated": False, "message": "503"}, True, 1),
    ],
)
async def test_premerge_behind_check_requires_current_tip(
    cleanup_db, mocker, head_sha, target_tip, update_result, expect_update, expect_retry
):
    _mock_rollup_merge_conflict_deps(
        mocker, mergeable_state="unknown", head_sha=head_sha
    )
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
                "head": {"sha": head_sha},
            },
            {"mergeable_state": "unknown", "head": {"sha": head_sha}},
        ],
    )
    mock_tip = mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value=target_tip
    )
    mock_update = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value=update_result or {"updated": False, "message": "unused"},
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "merge conflict"},
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

    assert mock_tip.await_count >= 1
    assert mock_update.await_count == (1 if expect_update else 0)
    assert mock_merge.await_count == (1 if head_sha == target_tip else 0)
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    if expect_retry is None:
        assert "_behind_update_retries" not in (parent.context or {})
    else:
        assert (parent.context or {}).get("_behind_update_retries") == expect_retry
