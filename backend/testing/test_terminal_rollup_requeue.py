"""Regression coverage for terminal rollup parent operator requeue."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import select

from app.admin_task_mutations import modify_task_apply
from app.closed_pr_reconciliation import reset_pr_attempt_state
from app.models import TaskHistory, TaskStatus
from app.terminal_rollup_requeue import reconcile_terminal_rollup_requeue
from testing.factories import make_project, make_task


async def _rollup_parent(db_session, *, context: dict, status=TaskStatus.FAILED):
    project = make_project(
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.flush()
    parent = make_task(
        project.id,
        title="Research parent",
        payloads=[{"type": "research", "prompt": "audit"}],
        status=status,
        context={
            "research_branch": "research/parent1",
            "parent_base": "automain",
            **context,
        },
    )
    db_session.add(parent)
    await db_session.flush()
    child = make_task(
        project.id,
        title="Completed child",
        payloads=[{"type": "create", "prompt": "implement"}],
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


def _stale_loop(pr_number: int) -> dict:
    return {
        "pr_number": pr_number,
        "branch": "research/parent1",
        "base_branch": "automain",
        "iteration": 3,
        "original_task_id": "chain-root",
        "fix_reason": "preserve this chain reason",
        "ci_waiting": True,
        "ci_wait_started": "2026-07-17T11:24:00",
        "ci_wait_pause_baseline": 2.0,
        "ci_passed": True,
        "review_approved": True,
        "verdict": "APPROVE",
        "verify_tests_task_id": "old-verifier",
        "verify_tests_verdict": "APPROVE",
        "escalated": True,
        "merge_failures": 4,
    }


def _stale_rollup_cycle_context() -> dict:
    return {
        "_merge_total_deferrals": 61,
        "_merge_escalation_attempted": True,
        "rollup_pr_creating_at": 1_721_234_567.0,
        "_rollup_retries": 5,
    }


@pytest.mark.parametrize("terminal_status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
@pytest.mark.asyncio
async def test_terminal_requeue_manual_open_rebind_gets_fresh_attempt(
    db_session, mocker, terminal_status
):
    """A parent-level manual rebind wins over the stale predecessor loop.

    Requeue retains the valid open replacement but cannot reuse approval,
    verifier, escalation, merge budget, or the old CI backstop clock.
    """

    parent = await _rollup_parent(
        db_session,
        status=terminal_status,
        context={
            **_stale_rollup_cycle_context(),
            "rollup_pr_number": 15494,
            "rollup_pr_url": "https://example.test/pulls/15494",
            "_is_rollup_review": True,
            "blueprint_review_verdict": "APPROVE",
            "review_loop": _stale_loop(15488),
        },
    )
    token = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 15494,
            "state": "open",
            "merged": False,
            "html_url": "https://example.test/pulls/15494",
            "head": {"ref": "research/parent1", "sha": "replacement-head"},
            "base": {"ref": "automain"},
        },
    )
    replacement_lookup = mocker.patch(
        "app.github.get_open_pull_request_for_head_branch",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.pause_clock.current_baseline",
        new_callable=AsyncMock,
        return_value=9.5,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    await modify_task_apply(
        parent,
        {"status": TaskStatus.QUEUED.value},
        BackgroundTasks(),
        db_session,
    )

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    ctx = parent.context or {}
    loop = ctx["review_loop"]
    assert ctx["rollup_pr_number"] == 15494
    assert loop["pr_number"] == 15494
    assert loop["head_sha"] == "replacement-head"
    assert loop["ci_waiting"] is True
    assert loop["ci_wait_started"] != "2026-07-17T11:24:00"
    assert datetime.fromisoformat(loop["ci_wait_started"])
    assert loop["ci_wait_pause_baseline"] == 9.5
    for stale_key in (
        "ci_passed",
        "review_approved",
        "verdict",
        "verify_tests_task_id",
        "verify_tests_verdict",
        "escalated",
        "merge_failures",
    ):
        assert stale_key not in loop
    assert "blueprint_review_verdict" not in ctx
    assert loop["iteration"] == 3
    assert loop["original_task_id"] == "chain-root"
    assert loop["fix_reason"] == "preserve this chain reason"
    for stale_key in _stale_rollup_cycle_context():
        assert stale_key not in ctx
    token.assert_awaited_once()
    assert get_pr.await_args.args[3] == 15494
    replacement_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_requeue_closed_only_releases_identity_for_fresh_rollup(
    db_session, mocker
):
    parent = await _rollup_parent(
        db_session,
        context={
            **_stale_rollup_cycle_context(),
            "rollup_pr_number": 15488,
            "rollup_pr_url": "https://example.test/pulls/15488",
            "_is_rollup_review": True,
            "blueprint_review_verdict": "APPROVE",
            "review_loop": _stale_loop(15488),
        },
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 15488,
            "state": "closed",
            "merged": False,
            "head": {"ref": "research/parent1", "sha": "closed-head"},
            "base": {"ref": "automain"},
        },
    )
    replacement_lookup = mocker.patch(
        "app.github.get_open_pull_request_for_head_branch",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    await modify_task_apply(
        parent,
        {"status": TaskStatus.QUEUED.value},
        BackgroundTasks(),
        db_session,
    )

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    ctx = parent.context or {}
    assert "rollup_pr_number" not in ctx
    assert "rollup_pr_url" not in ctx
    assert "_is_rollup_review" not in ctx
    assert "blueprint_review_verdict" not in ctx
    assert ctx["owns_rollup_branch"] is True
    for stale_key in _stale_rollup_cycle_context():
        assert stale_key not in ctx
    loop = ctx["review_loop"]
    assert loop == {
        "branch": "research/parent1",
        "base_branch": "automain",
        "iteration": 3,
        "original_task_id": "chain-root",
        "fix_reason": "preserve this chain reason",
    }
    replacement_lookup.assert_awaited_once_with(
        "token",
        "owner",
        "repo",
        "research/parent1",
        "automain",
        project_id=str(parent.project_id),
    )
    histories = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == parent.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        (history.details or {}).get("changes", {}).get("rollup_requeue_reconciliation")
        == "closed_pr_released"
        for history in histories
    )


@pytest.mark.asyncio
async def test_active_rollup_state_is_not_reset(db_session, mocker):
    original_context = {
        "rollup_pr_number": 15494,
        "_is_rollup_review": True,
        "review_loop": _stale_loop(15494),
    }
    parent = await _rollup_parent(
        db_session,
        context=original_context,
        status=TaskStatus.EVALUATING,
    )
    token = mocker.patch(
        "app.routers.agent.get_github_token_for_task", new_callable=AsyncMock
    )
    get_pr = mocker.patch("app.github.get_pull_request", new_callable=AsyncMock)
    before = dict(parent.context or {})

    outcome = await reconcile_terminal_rollup_requeue(parent, db_session)

    assert outcome == "active_unchanged"
    assert parent.context == before
    token.assert_not_awaited()
    get_pr.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure_stage", "expected_outcome"),
    [
        ("tracked_pr", "lookup_uncertain_identity_preserved"),
        ("replacement", "replacement_lookup_uncertain_identity_preserved"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_requeue_lookup_uncertainty_preserves_identity_not_proof(
    db_session, mocker, failure_stage, expected_outcome
):
    parent = await _rollup_parent(
        db_session,
        context={
            "rollup_pr_number": 15494,
            "_is_rollup_review": True,
            "blueprint_review_verdict": "APPROVE",
            "review_loop": _stale_loop(15494),
        },
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    if failure_stage == "tracked_pr":
        get_pr_result = AsyncMock(side_effect=RuntimeError("temporary GitHub outage"))
    else:
        get_pr_result = AsyncMock(
            return_value={
                "number": 15494,
                "state": "closed",
                "merged": False,
                "head": {"ref": "research/parent1", "sha": "closed-head"},
                "base": {"ref": "automain"},
            }
        )
    mocker.patch("app.github.get_pull_request", new=get_pr_result)
    replacement_lookup = mocker.patch(
        "app.github.get_open_pull_request_for_head_branch",
        new_callable=AsyncMock,
        side_effect=RuntimeError("temporary replacement lookup outage"),
    )

    outcome = await reconcile_terminal_rollup_requeue(parent, db_session)

    assert outcome == expected_outcome
    ctx = parent.context or {}
    assert ctx["rollup_pr_number"] == 15494
    assert "blueprint_review_verdict" not in ctx
    assert ctx["review_loop"] == {
        "pr_number": 15494,
        "branch": "research/parent1",
        "base_branch": "automain",
        "iteration": 3,
        "original_task_id": "chain-root",
        "fix_reason": "preserve this chain reason",
    }
    if failure_stage == "tracked_pr":
        replacement_lookup.assert_not_awaited()
    else:
        replacement_lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_requeue_closed_pr_adopts_unique_open_replacement(
    db_session, mocker
):
    parent = await _rollup_parent(
        db_session,
        context={
            "rollup_pr_number": 15488,
            "_is_rollup_review": True,
            "blueprint_review_verdict": "APPROVE",
            "review_loop": _stale_loop(15488),
        },
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 15488,
            "state": "closed",
            "merged": False,
            "head": {"ref": "research/parent1", "sha": "closed-head"},
            "base": {"ref": "automain"},
        },
    )
    replacement_lookup = mocker.patch(
        "app.github.get_open_pull_request_for_head_branch",
        new_callable=AsyncMock,
        return_value={
            "number": 15494,
            "state": "open",
            "merged": False,
            "html_url": "https://example.test/pulls/15494",
            "head": {"ref": "research/parent1", "sha": "replacement-head"},
            "base": {"ref": "automain"},
        },
    )
    mocker.patch(
        "app.pause_clock.current_baseline",
        new_callable=AsyncMock,
        return_value=4.0,
    )

    outcome = await reconcile_terminal_rollup_requeue(parent, db_session)

    assert outcome == "replacement_pr_rearmed"
    ctx = parent.context or {}
    loop = ctx["review_loop"]
    assert ctx["rollup_pr_number"] == 15494
    assert ctx["rollup_pr_url"] == "https://example.test/pulls/15494"
    assert loop["pr_number"] == 15494
    assert loop["head_sha"] == "replacement-head"
    assert loop["ci_waiting"] is True
    assert loop["ci_wait_started"] != "2026-07-17T11:24:00"
    assert loop["ci_wait_pause_baseline"] == 4.0
    assert "review_approved" not in loop
    assert "verify_tests_task_id" not in loop
    replacement_lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_requeue_merged_pr_immediately_runs_rollup_classifier(
    db_session, mocker
):
    parent = await _rollup_parent(
        db_session,
        context={
            "rollup_pr_number": 15494,
            "rollup_pr_url": "https://example.test/pulls/15494",
            "_is_rollup_review": True,
            "review_loop": _stale_loop(15494),
        },
    )
    merged_pr = {
        "number": 15494,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-07-17T12:00:00Z",
        "html_url": "https://example.test/pulls/15494",
        "head": {"ref": "research/parent1", "sha": "merged-head"},
        "base": {"ref": "automain"},
    }
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=merged_pr,
    )
    create_pr = mocker.patch("app.github.create_pull_request", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_open_pull_requests",
        new_callable=AsyncMock,
        return_value=[],
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        return_value="branch-sha",
    )
    mocker.patch(
        "app.pause_clock.current_baseline",
        new_callable=AsyncMock,
        return_value=0.0,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    await modify_task_apply(
        parent,
        {"status": TaskStatus.QUEUED.value},
        BackgroundTasks(),
        db_session,
    )

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert (parent.context or {}).get("pr_merged") is True
    assert (parent.context or {}).get("rollup_pr_number") == 15494
    assert "already merged" in (parent.latest_status_message or "").lower()
    assert get_pr.await_count >= 2
    create_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_requeue_atomic_manual_rebind_sanitizes_effective_context(
    db_session, mocker
):
    """A combined context+status PATCH is rejected before stale proof is read."""

    parent = await _rollup_parent(
        db_session,
        context={
            "rollup_pr_number": 15488,
            "review_loop": _stale_loop(15488),
        },
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 15494,
            "state": "open",
            "merged": False,
            "head": {"ref": "research/parent1", "sha": "replacement-head"},
            "base": {"ref": "automain"},
        },
    )
    mocker.patch(
        "app.github.get_open_pull_request_for_head_branch",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.pause_clock.current_baseline",
        new_callable=AsyncMock,
        return_value=7.0,
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    incoming_loop = _stale_loop(15494)
    with pytest.raises(HTTPException) as exc_info:
        await modify_task_apply(
            parent,
            {
                "status": TaskStatus.QUEUED.value,
                "context": {
                    **_stale_rollup_cycle_context(),
                    "rollup_pr_number": 15494,
                    "blueprint_review_verdict": "APPROVE",
                    "review_loop": incoming_loop,
                },
            },
            BackgroundTasks(),
            db_session,
        )
    assert exc_info.value.detail == (
        "Apply status separately from generic payload, context, or topology mutation"
    )
    get_pr.assert_not_awaited()

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    ctx = parent.context or {}
    loop = ctx["review_loop"]
    assert ctx["rollup_pr_number"] == 15488
    assert loop == _stale_loop(15488)


def test_attempt_reset_preserves_chain_metadata_only():
    stale = _stale_loop(15488)
    stale.update(
        {
            "review_history": [{"verdict": "APPROVE"}],
            "_ci_backstop_first_defer_at": "old",
            "future_unknown_attempt_latch": True,
            "issue_ledger": {
                "open": [
                    {
                        "issue_id": "BP-example",
                        "severity": "major",
                        "description": "binding blocker",
                        "file": "app.py",
                    }
                ]
            },
            "fix_agent_responses": [{"issue": "chain evidence"}],
            "iteration_reset_at": "2026-07-17T10:00:00",
            "iteration_reset_by": "operator@example.test",
            "max_iterations": 6,
            "reviewer_model": "reviewer-chain-pin",
        }
    )

    clean = reset_pr_attempt_state(stale)

    assert clean == {
        "branch": "research/parent1",
        "base_branch": "automain",
        "iteration": 3,
        "original_task_id": "chain-root",
        "fix_reason": "preserve this chain reason",
        "issue_ledger": {
            "open": [
                {
                    "issue_id": "BP-example",
                    "severity": "major",
                    "description": "binding blocker",
                    "file": "app.py",
                }
            ]
        },
        "fix_agent_responses": [{"issue": "chain evidence"}],
        "iteration_reset_at": "2026-07-17T10:00:00",
        "iteration_reset_by": "operator@example.test",
        "max_iterations": 6,
        "reviewer_model": "reviewer-chain-pin",
    }
