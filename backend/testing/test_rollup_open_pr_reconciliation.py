"""Acceptance proof for the recurring terminal-rollup PR reconciliation."""

from unittest.mock import AsyncMock

import pytest

from app.models import TaskStatus
from app.terminal_rollup_pr_sweep import (
    TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY,
    sweep_terminal_rollup_prs,
)
from testing.factories import make_project, make_task


@pytest.mark.asyncio
async def test_terminal_rollup_with_open_child_pr_is_reconciled(db_session, mocker):
    """An open PR owned by a terminal rollup is rebound and scheduled again."""

    project = make_project(integration_branch="automain")
    db_session.add(project)
    await db_session.flush()
    parent = make_task(
        project.id,
        title="Terminal rollup parent",
        payloads=[{"type": "research", "prompt": "reconcile"}],
        status=TaskStatus.FAILED,
        context={
            "research_branch": "research/terminal-rollup",
            "parent_base": "automain",
            "rollup_pr_number": 19048,
            "rollup_pr_url": "https://example.test/pulls/19048",
            "_is_rollup_review": True,
            "review_loop": {
                "pr_number": 19048,
                "branch": "research/terminal-rollup",
                "base_branch": "automain",
                "ci_passed": True,
                "review_approved": True,
            },
        },
    )
    child = make_task(
        project.id,
        title="Open child PR",
        payloads=[{"type": "create", "prompt": "child work"}],
        parent_task_id=parent.id,
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "pr_number": 19049,
                "branch": "feature/child",
                "base_branch": "automain",
            }
        },
    )
    db_session.add_all([parent, child])
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 19048,
            "state": "open",
            "merged": False,
            "html_url": "https://example.test/pulls/19048",
            "head": {"ref": "research/terminal-rollup", "sha": "open-head"},
            "base": {"ref": "automain", "sha": "base-sha"},
        },
    )
    mocker.patch(
        "app.pause_clock.current_baseline",
        new_callable=AsyncMock,
        return_value=0.0,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    result = await sweep_terminal_rollup_prs(db_session)

    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert result == {"open_pr_rearmed": 1}
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context[TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY] == 1
    assert parent.context["review_loop"]["pr_number"] == 19048
    assert parent.context["review_loop"]["ci_waiting"] is True
    assert "#19048 open_pr_rearmed" in parent.latest_status_message
    assert child.status == TaskStatus.EVALUATING
