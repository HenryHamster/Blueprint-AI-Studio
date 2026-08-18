"""Monitor routing for exact review-wait-owned approval gates."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.models import Project, Task, TaskStatus


_HEAD_SHA = "owned-monitor-head"
_BASE_SHA = "owned-monitor-base"


@pytest.mark.parametrize(
    ("pr_outcome", "task_kind", "review_approved"),
    [
        ("open", "parent", False),
        ("merged", "ordinary", False),
        ("merged", "parent", False),
        ("merged", "child", False),
        ("merged", "ordinary", True),
        ("merged", "parent", True),
    ],
    ids=[
        "open-rollup-parent",
        "merged-ordinary",
        "merged-rollup-parent",
        "merged-rollup-child",
        "merged-reviewed-ordinary",
        "merged-reviewed-rollup-parent",
    ],
)
@pytest.mark.asyncio
async def test_monitor_routes_exact_owned_gate_to_review_wait_reconciliation(
    db_session, mocker, pr_outcome, task_kind, review_approved
):
    """An exact automatic gate can respawn or consume an already-merged wait."""
    from app.closed_pr_reconciliation import (
        REVIEW_WAIT_APPROVAL_HOLD_KEY,
        REVIEW_WAIT_APPROVAL_HOLD_OWNER,
    )
    from app.orchestrator_ci_monitor import _monitor_evaluating_tasks
    from app.shared_state import runtime_config

    mocker.patch.dict(
        runtime_config,
        {
            "require_approval_gates": True,
            # A merged target must release its exact automatic hold even when
            # review respawn is disabled; no new child is needed in that case.
            "review_wait_auto_respawn": pr_outcome == "open",
            "review_wait_respawn_cap": 1,
        },
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new=AsyncMock()
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.acceptance_gate.corroborate_acceptance_for_landing",
        new=AsyncMock(),
    )

    async def _apply_landed(ci_task, *args, **kwargs):
        ci_task.context = {
            **(ci_task.context or {}),
            "pr_merged": True,
            "landed_on_integration": True,
        }
        return True

    landed_mock = mocker.patch(
        "app.orchestrator_ci_monitor._apply_ci_merged_landed_signal",
        new=AsyncMock(side_effect=_apply_landed),
    )
    mocker.patch(
        "app.orchestrator_ci_monitor._mark_unblocked_dependents",
        new=AsyncMock(return_value=0),
    )

    project = Project(name=f"Owned monitor {pr_outcome}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    approval_set_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    ).isoformat()
    started_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task_context = {
        "_approval_set_at": approval_set_at,
        "review_loop": {
            "ci_waiting": True,
            "ci_passed": True,
            "review_approved": review_approved,
            "ci_wait_started": started_at,
            "review_wait_started": started_at,
            "review_wait_escalated": True,
            "ci_poll_interval": 0,
            "pr_number": 42,
            "branch": "blueprint/owned-monitor",
            "base_branch": "main",
            "iteration": 1,
            "ci_checked_head_sha": _HEAD_SHA,
            "ci_checked_base_sha": _BASE_SHA,
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                "project_id": str(project.id),
                "pr_number": 42,
                "base_sha": _BASE_SHA,
                "head_sha": _HEAD_SHA,
                "set_at": approval_set_at,
            },
        },
    }
    if task_kind != "ordinary":
        task_context["rollup_pr_number"] = 42
    if task_kind == "parent":
        task_context["_is_rollup_review"] = True
    task = Task(
        project_id=project.id,
        title=f"Owned review wait {pr_outcome}",
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/owned-monitor",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context=task_context,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "state": "closed" if pr_outcome == "merged" else "open",
                "merged": pr_outcome == "merged",
                "head": {"sha": _HEAD_SHA, "ref": "blueprint/owned-monitor"},
                "base": {"sha": _BASE_SHA, "ref": "main"},
                "mergeable": True,
            }
        ),
    )
    child = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Respawned exact review",
        payloads=[{"type": "review"}],
        status=TaskStatus.QUEUED,
    )

    async def _spawn(_source, _task_type, source_session, *args, **kwargs):
        authoritative = await kwargs["source_spawn_authority"](source_session)
        assert authoritative is not None
        return (child, True)

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_spawn),
    )

    await _monitor_evaluating_tasks(db_session)

    await db_session.rollback()
    fresh = await db_session.get(Task, task_id, populate_existing=True)
    assert fresh is not None
    loop = (fresh.context or {}).get("review_loop") or {}
    if pr_outcome == "merged":
        spawn_mock.assert_not_awaited()
    else:
        spawn_mock.assert_awaited_once()
    assert fresh.requires_approval is not True
    assert (fresh.context or {}).get("_approval_set_at") is None
    assert loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY) is None
    if pr_outcome == "merged":
        assert loop.get("ci_waiting") is False
        assert loop.get("review_wait_started") is None
        if task_kind == "parent":
            assert fresh.status == TaskStatus.EVALUATING
            landed_mock.assert_not_awaited()
            assert "pr_merged" not in (fresh.context or {})
            assert "landed_on_integration" not in (fresh.context or {})
        else:
            assert fresh.status == TaskStatus.COMPLETED
            landed_mock.assert_awaited_once()
            assert (fresh.context or {}).get("pr_merged") is True
            assert (fresh.context or {}).get("landed_on_integration") is True
    else:
        landed_mock.assert_not_awaited()
        assert loop.get("review_wait_respawn_task_id") == str(child.id)
        assert loop.get("review_wait_respawn_count") == 1
