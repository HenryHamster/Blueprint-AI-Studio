"""Regression coverage for descendant retry and blocker propagation."""

import copy
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.admin_approval_authority import (
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    establish_independent_approval_hold,
)
from app.ci_merge_helpers import _capture_merge_snapshot
from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    REVIEW_WAIT_APPROVAL_HOLD_OWNER,
)
from app.models import TaskStatus
from app.rollup_lineage_fence import (
    invalidate_ancestor_rollups_for_requeue,
    lock_ancestor_rollups_for_requeue,
    mark_ancestor_rollups_awaiting_operator,
)
from app.rollup_locking import _capture_rollup_parent_snapshot
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


def _approval_hold_state(task):
    context = dict(task.context or {})
    return {
        "requires_approval": task.requires_approval,
        "_approval_set_at": context.get("_approval_set_at"),
        "approval_hold_reason": copy.deepcopy(context.get("approval_hold_reason")),
        APPROVAL_HOLD_EPISODE_KEY: context.get(APPROVAL_HOLD_EPISODE_KEY),
        APPROVAL_HOLD_PROVENANCE_KEY: copy.deepcopy(
            context.get(APPROVAL_HOLD_PROVENANCE_KEY)
        ),
    }


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
async def test_multilevel_ancestor_locks_are_acquired_root_first(db_session, mocker):
    """Nested retry follows the same root-to-leaf order as parent cancel."""

    project = make_project(integration_branch="automain", promotion_target="main")
    db_session.add(project)
    await db_session.flush()
    grandparent = make_task(
        project.id,
        title="Grandparent rollup",
        payloads=[{"type": "plan", "prompt": "plan"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(grandparent)
    await db_session.flush()
    parent = make_task(
        project.id,
        title="Nested research rollup",
        payloads=[{"type": "research", "prompt": "research"}],
        parent_task_id=grandparent.id,
        status=TaskStatus.EVALUATING,
    )
    db_session.add(parent)
    await db_session.flush()
    child = make_task(
        project.id,
        title="Terminal review child",
        payloads=[{"type": "review", "pr_number": 998}],
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
    )
    db_session.add(child)
    await db_session.commit()

    locked_ids = []
    real_execute = db_session.execute

    async def _recording_execute(statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            params = statement.compile().params
            locked_ids.append(
                next(
                    value
                    for key, value in params.items()
                    if key.startswith("id_")
                )
            )
        return await real_execute(statement, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=_recording_execute)
    ancestors = await lock_ancestor_rollups_for_requeue(child, db_session)

    assert locked_ids == [grandparent.id, parent.id]
    assert [ancestor.id for ancestor in ancestors] == [parent.id, grandparent.id]


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
async def test_descendant_requeue_releases_exact_review_wait_approval_hold(db_session):
    parent, child = await _lineage(db_session)
    timer = "2026-08-17T12:00:00+00:00"
    context = dict(parent.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop.update(
        {
            "ci_checked_head_sha": "head-997",
            "ci_checked_base_sha": "base-997",
            "review_wait_escalated": True,
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                "project_id": str(parent.project_id),
                "pr_number": 997,
                "head_sha": "head-997",
                "base_sha": "base-997",
                "set_at": timer,
            },
        }
    )
    context["review_loop"] = review_loop
    context["_approval_set_at"] = timer
    context["unrelated_rollup_sentinel"] = {"keep": "exactly"}
    parent.context = context
    parent.requires_approval = True
    db_session.add(parent)
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert parent.requires_approval is False
    assert "_approval_set_at" not in parent.context
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in parent.context["review_loop"]
    assert "review_wait_escalated" not in parent.context["review_loop"]
    assert parent.context["unrelated_rollup_sentinel"] == {"keep": "exactly"}


@pytest.mark.asyncio
async def test_descendant_requeue_releases_current_hold_with_stale_nested_target(
    db_session,
):
    """A pre-force-push nested target cannot mask the current exact hold."""

    parent, child = await _lineage(db_session)
    timer = "2026-08-17T12:30:00+00:00"
    context = dict(parent.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop.update(
        {
            "ci_checked_head_sha": "force-pushed-head-997",
            "ci_checked_base_sha": "current-base-997",
            "review_target": {
                "project_id": str(parent.project_id),
                "pr_number": 997,
                "head_sha": "pre-force-push-head-997",
                "base_sha": "old-base-997",
            },
            "review_wait_escalated": True,
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                "project_id": str(parent.project_id),
                "pr_number": 997,
                "head_sha": "force-pushed-head-997",
                "base_sha": "current-base-997",
                "set_at": timer,
            },
        }
    )
    context["review_loop"] = review_loop
    context["_approval_set_at"] = timer
    parent.context = context
    parent.requires_approval = True
    db_session.add(parent)
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert parent.requires_approval is False
    assert "_approval_set_at" not in parent.context
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in parent.context["review_loop"]
    assert "review_wait_escalated" not in parent.context["review_loop"]


@pytest.mark.asyncio
async def test_descendant_requeue_preserves_independent_approval_hold_byte_for_byte(
    db_session,
):
    parent, child = await _lineage(db_session)
    independent_hold = {
        "_approval_set_at": "manual-timer",
        "approval_hold_reason": "manual security review",
        "_approval_hold_episode": "manual-episode",
        "_approval_hold_provenance": {
            "owner": "operator",
            "episode": "manual-episode",
            "set_at": "manual-timer",
            "reason": "manual security review",
        },
        "touched_critical": ["backend/app/security.py"],
    }
    context = dict(parent.context or {})
    context.update(independent_hold)
    parent.context = context
    parent.requires_approval = True
    db_session.add(parent)
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert parent.requires_approval is True
    assert {
        key: parent.context[key]
        for key in independent_hold
    } == independent_hold


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
async def test_requeue_preserves_newer_approval_episode_that_replaced_block_hold(
    db_session,
):
    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child,
        db_session,
        reason="review spawn failed (cap reached)",
        pr_number=997,
    )
    await db_session.commit()
    await db_session.refresh(parent)

    establish_independent_approval_hold(
        parent,
        reason="later explicit security hold",
        owner="operator",
    )
    newer_hold = _approval_hold_state(parent)
    db_session.add(parent)
    await db_session.commit()

    await invalidate_ancestor_rollups_for_requeue(child, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    assert _approval_hold_state(parent) == newer_hold
    assert "_descendant_operator_blocks" not in parent.context
    assert "_descendant_operator_blocks_prior_approval" not in parent.context


@pytest.mark.asyncio
async def test_requeue_restores_exact_prior_approval_episode_after_block_clears(
    db_session,
):
    parent, child = await _lineage(db_session)
    establish_independent_approval_hold(
        parent,
        reason="preexisting manual risk hold",
        owner="operator",
    )
    prior_hold = _approval_hold_state(parent)
    db_session.add(parent)
    await db_session.commit()

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

    assert _approval_hold_state(parent) == prior_hold
    assert "_descendant_operator_blocks" not in parent.context
    assert "_descendant_operator_blocks_prior_approval" not in parent.context


@pytest.mark.asyncio
async def test_requeue_does_not_restore_invalidated_prior_review_wait_hold(db_session):
    parent, child = await _lineage(db_session)
    timer = "2026-08-17T13:00:00+00:00"
    context = dict(parent.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop.update(
        {
            "ci_checked_head_sha": "head-997",
            "ci_checked_base_sha": "base-997",
            "review_wait_escalated": True,
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                "project_id": str(parent.project_id),
                "pr_number": 997,
                "head_sha": "head-997",
                "base_sha": "base-997",
                "set_at": timer,
            },
        }
    )
    context["review_loop"] = review_loop
    context["_approval_set_at"] = timer
    parent.context = context
    parent.requires_approval = True
    db_session.add(parent)
    await db_session.commit()

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
    assert "_approval_set_at" not in parent.context
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in parent.context["review_loop"]
    assert "review_wait_escalated" not in parent.context["review_loop"]


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


@pytest.mark.asyncio
async def test_cross_project_parent_cannot_be_mutated_as_rollup_ancestor(db_session):
    """A corrupt parent edge never crosses the project trust boundary."""
    child_project = make_project(
        name="Child project",
        integration_branch="automain",
        promotion_target="main",
    )
    foreign_project = make_project(
        name="Foreign parent project",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add_all([child_project, foreign_project])
    await db_session.flush()
    foreign_parent = make_task(
        foreign_project.id,
        title="Foreign rollup parent",
        payloads=[{"type": "research", "prompt": "foreign"}],
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 998, "pr_merged": False},
    )
    db_session.add(foreign_parent)
    await db_session.flush()
    child = make_task(
        child_project.id,
        title="Corrupt foreign-linked child",
        payloads=[{"type": "create", "prompt": "child"}],
        parent_task_id=foreign_parent.id,
        status=TaskStatus.FAILED,
    )
    db_session.add(child)
    await db_session.commit()
    original_foreign_context = dict(foreign_parent.context or {})

    with pytest.raises(HTTPException) as exc_info:
        await invalidate_ancestor_rollups_for_requeue(child, db_session)

    assert exc_info.value.status_code == 409
    assert "missing ancestor" in exc_info.value.detail
    await db_session.refresh(foreign_parent)
    assert foreign_parent.context == original_foreign_context


# ── out-of-band release of resolved descendant operator blocks ────────────────
#
# Incident 2026-08-19: PR #17419 merged at 02:15Z, yet the fence hold on
# ancestor 289809a3 persisted (the only release path was an operator requeue of
# the flagged descendant) and the approval-gate timeout FAILED the ancestor at
# 02:18Z; the requeued clone re-inherited the stale hold.


def _mock_github(mocker, pr_payload):
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("tok", "owner", "repo"),
    )
    if isinstance(pr_payload, Exception):
        mocker.patch("app.github.get_pull_request", side_effect=pr_payload)
    else:
        mocker.patch("app.github.get_pull_request", return_value=pr_payload)


@pytest.mark.asyncio
async def test_release_resolved_block_when_subject_pr_merged(db_session, mocker):
    from app.rollup_lineage_fence import (
        release_resolved_descendant_operator_blocks,
    )

    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason="review spawn failed", pr_number=17419
    )
    await db_session.commit()
    await db_session.refresh(parent)
    assert parent.requires_approval is True

    _mock_github(mocker, {"merged": True, "state": "closed"})
    released = await release_resolved_descendant_operator_blocks(
        parent, db_session
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert released is True
    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context
    assert "_descendant_operator_blocks_prior_approval" not in parent.context
    assert "resolved out-of-band" in parent.latest_status_message


@pytest.mark.asyncio
async def test_release_keeps_hold_while_subject_pr_open(db_session, mocker):
    from app.rollup_lineage_fence import (
        release_resolved_descendant_operator_blocks,
    )

    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason="review spawn failed", pr_number=17419
    )
    await db_session.commit()
    await db_session.refresh(parent)

    _mock_github(mocker, {"merged": False, "state": "open"})
    released = await release_resolved_descendant_operator_blocks(
        parent, db_session
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert released is False
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]


@pytest.mark.asyncio
async def test_release_keeps_hold_on_github_lookup_failure(db_session, mocker):
    from app.rollup_lineage_fence import (
        release_resolved_descendant_operator_blocks,
    )

    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason="review spawn failed", pr_number=17419
    )
    await db_session.commit()
    await db_session.refresh(parent)

    _mock_github(mocker, RuntimeError("github down"))
    released = await release_resolved_descendant_operator_blocks(
        parent, db_session
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert released is False
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]


@pytest.mark.asyncio
async def test_release_prless_block_when_descendant_completed(db_session, mocker):
    from app.rollup_lineage_fence import (
        release_resolved_descendant_operator_blocks,
    )

    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason="needs operator look", pr_number=None
    )
    await db_session.commit()

    child.status = TaskStatus.COMPLETED
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)

    released = await release_resolved_descendant_operator_blocks(
        parent, db_session
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert released is True
    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context


@pytest.mark.asyncio
async def test_partial_resolution_drops_entry_but_keeps_hold(db_session, mocker):
    from app.rollup_lineage_fence import (
        release_resolved_descendant_operator_blocks,
    )

    parent, child = await _lineage(db_session)
    sibling = make_task(
        parent.project_id,
        title="Second terminal descendant",
        payloads=[{"type": "create", "prompt": "other"}],
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
    )
    db_session.add(sibling)
    await db_session.commit()
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason="first blocker", pr_number=None
    )
    await mark_ancestor_rollups_awaiting_operator(
        sibling, db_session, reason="second blocker", pr_number=None
    )
    await db_session.commit()

    child.status = TaskStatus.COMPLETED
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)

    released = await release_resolved_descendant_operator_blocks(
        parent, db_session
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert released is False
    assert parent.requires_approval is True
    blocks = parent.context["_descendant_operator_blocks"]
    assert str(child.id) not in blocks
    assert str(sibling.id) in blocks


# ── Fence-hold ADJUDICATION ──────────────────────────────────────────────────
#
# Incident 2026-08-23, research task 70c5ec8f: the fence set a hold at 01:42Z
# ("descendant 5fb05561 needs operator intervention", owner
# "rollup_lineage_fence"), the generic 2h HUMAN-approval timeout FAILED the
# ancestor at ~03:42Z, and the blocking descendant's PR #17693 merged at
# 12:11Z — 8.5h after the rollup was already destroyed. A machine-set
# dependency must never be killed by a human-approval clock, and the release
# check must be reachable outside that one narrow timeout window.


async def _fenced_ancestor(db_session, *, pr_number=17693, reason="review spawn failed"):
    """Ancestor carrying a provably fence-owned descendant-block hold."""

    parent, child = await _lineage(db_session)
    await mark_ancestor_rollups_awaiting_operator(
        child, db_session, reason=reason, pr_number=pr_number
    )
    await db_session.commit()
    await db_session.refresh(parent)
    from app.rollup_lineage_fence import descendant_operator_block_hold_is_owned

    assert descendant_operator_block_hold_is_owned(parent) is True
    return parent, child


@pytest.mark.asyncio
async def test_fence_block_decide_window_is_pinned_to_twelve_hours():
    """Literal pin: the bounded decide window is 12h, not "whenever"."""

    from app.rollup_lineage_fence import _FENCE_BLOCK_DECIDE_AFTER_HOURS

    assert _FENCE_BLOCK_DECIDE_AFTER_HOURS == 12


@pytest.mark.asyncio
async def test_adjudicate_waits_and_names_the_open_blocking_pr(db_session, mocker):
    """INCIDENT REGRESSION: a still-OPEN blocker PR means WAIT, never fail.

    The hold is retained, the ancestor stays EVALUATING, and the status
    message surfaces the reason: which descendant, which PR, why, how long.
    """

    from app.rollup_lineage_fence import adjudicate_descendant_operator_blocks

    parent, child = await _fenced_ancestor(db_session)
    _mock_github(mocker, {"merged": False, "state": "open"})

    state = await adjudicate_descendant_operator_blocks(
        parent, db_session, now=_utcnow() + timedelta(hours=9)
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "waiting"
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]
    assert "_descendant_operator_blocks_decided" not in parent.context
    message = parent.latest_status_message or ""
    assert str(child.id)[:8] in message
    assert "PR #17693" in message
    assert "review spawn failed" in message
    assert "blocked 9h" in message


@pytest.mark.asyncio
async def test_adjudicate_waiting_message_is_idempotent_for_the_same_blockset(
    db_session, mocker
):
    """Re-entering sweeps must not churn the row with an identical message."""

    from app.rollup_lineage_fence import adjudicate_descendant_operator_blocks

    parent, _child = await _fenced_ancestor(db_session)
    _mock_github(mocker, {"merged": False, "state": "open"})
    now = _utcnow() + timedelta(hours=9)

    assert await adjudicate_descendant_operator_blocks(
        parent, db_session, now=now
    ) == "waiting"
    first = parent.latest_status_message
    assert await adjudicate_descendant_operator_blocks(
        parent, db_session, now=now
    ) == "waiting"
    assert parent.latest_status_message == first


@pytest.mark.asyncio
async def test_adjudicate_releases_once_the_blocking_pr_merges(db_session, mocker):
    """The same ancestor, after the blocker's PR merges out-of-band.

    #17693 merged 8.5h after the incident's ancestor was FAILED. With the
    ancestor still alive, the very next adjudication releases the hold and
    restores the exact prior approval state.
    """

    from app.rollup_lineage_fence import adjudicate_descendant_operator_blocks

    parent, _child = await _fenced_ancestor(db_session)
    _mock_github(mocker, {"merged": False, "state": "open"})
    assert await adjudicate_descendant_operator_blocks(
        parent, db_session, now=_utcnow() + timedelta(hours=2)
    ) == "waiting"
    await db_session.commit()

    # ... and now the blocker lands.
    _mock_github(mocker, {"merged": True, "state": "closed"})
    state = await adjudicate_descendant_operator_blocks(
        parent, db_session, now=_utcnow() + timedelta(hours=9)
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "released"
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context
    assert "_descendant_operator_blocks_prior_approval" not in parent.context
    assert "resolved out-of-band" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_adjudicate_decides_when_every_blocker_is_terminal_and_aged_out(
    db_session, mocker
):
    """Degrade gracefully or DECIDE — never park forever, never hand it back."""

    from app.rollup_lineage_fence import (
        _FENCE_BLOCK_DECIDE_AFTER_HOURS,
        adjudicate_descendant_operator_blocks,
    )

    parent, child = await _fenced_ancestor(db_session)
    assert child.status == TaskStatus.FAILED  # terminal blocker
    _mock_github(mocker, {"merged": False, "state": "open"})

    state = await adjudicate_descendant_operator_blocks(
        parent,
        db_session,
        now=_utcnow() + timedelta(hours=_FENCE_BLOCK_DECIDE_AFTER_HOURS + 1),
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "decided"
    assert parent.status == TaskStatus.EVALUATING  # resumed, NOT failed
    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context
    decided = parent.context["_descendant_operator_blocks_decided"]
    assert isinstance(decided, list) and len(decided) == 1
    record = decided[0]
    assert record["task_id"] == str(child.id)
    assert record["pr_number"] == 17693
    assert record["reason"] == "review spawn failed"
    assert record["blocked_at"] and record["decided_at"]
    assert "terminal" in record["why"]
    assert str(child.id)[:8] in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_adjudicate_waits_while_the_block_is_younger_than_the_window(
    db_session, mocker
):
    """Terminal blockers alone are not enough — the window must elapse."""

    from app.rollup_lineage_fence import (
        _FENCE_BLOCK_DECIDE_AFTER_HOURS,
        adjudicate_descendant_operator_blocks,
    )

    parent, child = await _fenced_ancestor(db_session)
    assert child.status == TaskStatus.FAILED
    _mock_github(mocker, {"merged": False, "state": "open"})

    state = await adjudicate_descendant_operator_blocks(
        parent,
        db_session,
        now=_utcnow() + timedelta(hours=_FENCE_BLOCK_DECIDE_AFTER_HOURS - 1),
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "waiting"
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]
    assert "_descendant_operator_blocks_decided" not in parent.context


@pytest.mark.asyncio
async def test_adjudicate_waits_while_the_blocking_descendant_is_still_running(
    db_session, mocker
):
    """A blocker that is still MOVING is not a stale block — wait for it.

    Everything else is decidable here: the PR state reads cleanly and the
    block is far past the window. But the descendant itself went back to
    IN_PROGRESS, so the dependency is live and deciding would discard a real,
    in-flight blocker. Only a TERMINAL descendant can age out.
    """

    from app.rollup_lineage_fence import (
        _FENCE_BLOCK_DECIDE_AFTER_HOURS,
        adjudicate_descendant_operator_blocks,
    )

    parent, child = await _fenced_ancestor(db_session)
    child.status = TaskStatus.IN_PROGRESS
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    _mock_github(mocker, {"merged": False, "state": "open"})

    state = await adjudicate_descendant_operator_blocks(
        parent,
        db_session,
        now=_utcnow() + timedelta(hours=_FENCE_BLOCK_DECIDE_AFTER_HOURS + 24),
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "waiting"
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]
    assert "_descendant_operator_blocks_decided" not in parent.context


@pytest.mark.asyncio
async def test_adjudicate_never_decides_on_an_unreadable_github_state(
    db_session, mocker
):
    """A raising/timing-out lookup is UNKNOWN — wait, never decide, never fail.

    Everything else about this row is decidable (terminal blocker, block far
    past the window); only the PR state is unreadable. Unknown is not
    progress.
    """

    from app.rollup_lineage_fence import (
        _FENCE_BLOCK_DECIDE_AFTER_HOURS,
        adjudicate_descendant_operator_blocks,
    )

    parent, child = await _fenced_ancestor(db_session)
    assert child.status == TaskStatus.FAILED
    _mock_github(mocker, TimeoutError("github timed out"))

    state = await adjudicate_descendant_operator_blocks(
        parent,
        db_session,
        now=_utcnow() + timedelta(hours=_FENCE_BLOCK_DECIDE_AFTER_HOURS + 24),
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "waiting"
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]
    assert "_descendant_operator_blocks_decided" not in parent.context


@pytest.mark.asyncio
async def test_adjudicate_declines_a_hold_the_fence_no_longer_owns(
    db_session, mocker
):
    """Ownership is structural. A newer independent episode is NOT the fence.

    The keyed blocks are still in context and the reason string still reads
    "descendant ... needs operator intervention", so a reason-match would
    wrongly claim this row. The receipt/provenance check must decline it and
    leave every other disposition alone.
    """

    from app.rollup_lineage_fence import adjudicate_descendant_operator_blocks

    parent, child = await _fenced_ancestor(db_session)
    establish_independent_approval_hold(
        parent, reason="operator wants a look", owner="admin_operator"
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    _mock_github(mocker, {"merged": True, "state": "closed"})

    state = await adjudicate_descendant_operator_blocks(
        parent, db_session, now=_utcnow() + timedelta(hours=48)
    )
    await db_session.commit()
    await db_session.refresh(parent)

    assert state == "not_fenced"
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]


@pytest.mark.asyncio
async def test_adjudicate_declines_when_the_hold_receipt_is_missing(
    db_session, mocker
):
    """No receipt => unproven => decline, exactly like a stranger's hold."""

    from app.rollup_lineage_fence import adjudicate_descendant_operator_blocks

    parent, _child = await _fenced_ancestor(db_session)
    context = dict(parent.context or {})
    context.pop("_descendant_operator_blocks_approval_hold", None)
    parent.context = context
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    _mock_github(mocker, {"merged": True, "state": "closed"})

    state = await adjudicate_descendant_operator_blocks(
        parent, db_session, now=_utcnow() + timedelta(hours=48)
    )
    assert state == "not_fenced"


@pytest.mark.asyncio
async def test_reaper_sweep_adjudicates_outside_any_timeout_branch(
    db_session, mocker
):
    """FAULT B: the release check must be reachable on its own cadence.

    No approval-gate timeout runs here at all — the reaper sweep alone finds
    the fenced ancestor and releases the hold once the blocker's PR merged.
    """

    from app.reaper_sweeps import sweep_release_resolved_descendant_fence_blocks

    parent, _child = await _fenced_ancestor(db_session)
    _mock_github(mocker, {"merged": True, "state": "closed"})

    changed = await sweep_release_resolved_descendant_fence_blocks(
        db_session, _utcnow()
    )
    await db_session.refresh(parent)

    assert changed == 1
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is False
    assert "_descendant_operator_blocks" not in parent.context


@pytest.mark.asyncio
async def test_reaper_sweep_skips_a_hold_the_fence_does_not_own(
    db_session, mocker
):
    """The sweep must never touch a row whose latch belongs to someone else."""

    from app.reaper_sweeps import sweep_release_resolved_descendant_fence_blocks

    parent, child = await _fenced_ancestor(db_session)
    establish_independent_approval_hold(
        parent, reason="operator wants a look", owner="admin_operator"
    )
    db_session.add(parent)
    await db_session.commit()
    _mock_github(mocker, {"merged": True, "state": "closed"})

    changed = await sweep_release_resolved_descendant_fence_blocks(
        db_session, _utcnow()
    )
    await db_session.refresh(parent)

    assert changed == 0
    assert parent.requires_approval is True
    assert str(child.id) in parent.context["_descendant_operator_blocks"]
