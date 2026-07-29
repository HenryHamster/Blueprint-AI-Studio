"""Regression coverage for descendant retry and blocker propagation."""

import pytest
from fastapi import HTTPException

from app.ci_merge_helpers import _capture_merge_snapshot
from app.models import TaskStatus
from app.rollup_lineage_fence import (
    invalidate_ancestor_rollups_for_requeue,
    mark_ancestor_rollups_awaiting_operator,
)
from app.rollup_locking import _capture_rollup_parent_snapshot
from testing.factories import make_project, make_task


async def _lineage(db_session, *, parent_status=TaskStatus.EVALUATING):
    project = make_project(integration_branch="automain", promotion_target="main")
    db_session.add(project)
    await db_session.flush()
    parent = make_task(
        project.id,
        title="Rollup parent",
        payloads=[{"type": "research", "prompt": "audit"}],
        status=parent_status,
        context={
            "rollup_pr_number": 997,
            "rollup_pr_url": "https://example.test/pull/997",
            "pr_merged": False,
            "blueprint_review_verdict": "APPROVE",
            "review_loop": {
                "pr_number": 997,
                "branch": "research/root",
                "base_branch": "automain",
                "iteration": 2,
                "ci_waiting": True,
                "ci_passed": True,
                "review_approved": True,
                "verdict": "APPROVE",
                "verify_tests_verdict": "APPROVE",
            },
        },
    )
    db_session.add(parent)
    await db_session.flush()
    child = make_task(
        project.id,
        title="Terminal descendant",
        payloads=[{"type": "create", "prompt": "implement"}],
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    await db_session.refresh(child)
    return parent, child


@pytest.mark.asyncio
async def test_descendant_requeue_invalidates_open_ancestor_rollup(db_session):
    parent, child = await _lineage(db_session)
    old_merge_snapshot = _capture_merge_snapshot(parent)
    old_rollup_snapshot = _capture_rollup_parent_snapshot(parent)

    invalidated = await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert invalidated == [str(parent.id)]
    assert parent.context["rollup_pr_number"] == 997
    assert parent.context["review_loop"]["pr_number"] == 997
    assert parent.context["review_loop"]["iteration"] == 2
    assert "ci_waiting" not in parent.context["review_loop"]
    assert "ci_passed" not in parent.context["review_loop"]
    assert "review_approved" not in parent.context["review_loop"]
    assert "verdict" not in parent.context["review_loop"]
    assert "verify_tests_verdict" not in parent.context["review_loop"]
    assert "blueprint_review_verdict" not in parent.context
    assert "pr_merged" not in parent.context
    assert parent.context["_rollup_lineage_generation"] == 1
    assert (
        parent.context["_rollup_invalidated_by_descendant"]["descendant_task_id"]
        == str(child.id)
    )
    assert _capture_merge_snapshot(parent) != old_merge_snapshot
    assert _capture_rollup_parent_snapshot(parent) != old_rollup_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
async def test_descendant_requeue_below_terminal_ancestor_is_rejected(
    db_session, terminal_status
):
    parent, child = await _lineage(
        db_session, parent_status=terminal_status
    )

    with pytest.raises(HTTPException) as exc_info:
        await invalidate_ancestor_rollups_for_requeue(child, db_session)

    assert exc_info.value.status_code == 409
    assert str(parent.id) in exc_info.value.detail
    assert "requeue the ancestor first" in exc_info.value.detail


@pytest.mark.asyncio
async def test_terminal_descendant_blocker_is_visible_on_ancestor(db_session):
    parent, child = await _lineage(db_session)

    marked = await mark_ancestor_rollups_awaiting_operator(
        child,
        db_session,
        reason="review spawn failed (supersession churn)",
        pr_number=999,
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert marked == [str(parent.id)]
    assert parent.requires_approval is True
    assert parent.context["_descendant_operator_blocks"][str(child.id)] == {
        "task_id": str(child.id),
        "reason": "review spawn failed (supersession churn)",
        "pr_number": 999,
        "blocked_at": parent.context["_descendant_operator_blocks"][str(child.id)][
            "blocked_at"
        ],
    }
    assert parent.context["_descendant_operator_blocks_prior_approval"] is False
    assert str(child.id) in parent.latest_status_message
    assert "PR #999" in parent.latest_status_message


@pytest.mark.asyncio
async def test_requeue_clears_matching_ancestor_operator_block(db_session):
    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child,
        db_session,
        reason="review spawn failed (cap reached)",
        pr_number=997,
    )
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context
    assert "_descendant_operator_blocks_prior_approval" not in parent.context


@pytest.mark.asyncio
async def test_requeue_one_descendant_preserves_other_operator_block(db_session):
    parent, child = await _lineage(db_session)
    sibling = make_task(
        parent.project_id,
        title="Second terminal descendant",
        payloads=[{"type": "create", "prompt": "implement sibling"}],
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
    )
    db_session.add(sibling)
    await db_session.commit()

    await mark_ancestor_rollups_awaiting_operator(
        child,
        db_session,
        reason="review spawn failed (cap reached)",
        pr_number=997,
    )
    await mark_ancestor_rollups_awaiting_operator(
        sibling,
        db_session,
        reason="review spawn failed (supersession churn)",
        pr_number=998,
    )
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(sibling, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert parent.requires_approval is True
    blocks = parent.context["_descendant_operator_blocks"]
    assert set(blocks) == {str(child.id)}
    assert blocks[str(child.id)]["pr_number"] == 997
