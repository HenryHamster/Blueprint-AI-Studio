"""Approval-episode authority for terminal status and cap-reset releases."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import update

from app.admin import reset_iteration_cap
from app.admin_approval_authority import (
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    establish_independent_approval_hold,
)
from app.admin_task_mutations import _apply_status_mutation
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Project, Task, TaskStatus


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent approval-release authority requires PostgreSQL")


def _stamp_hold(task: Task, *, owner: str, reason: str, sentinel: str) -> None:
    establish_independent_approval_hold(task, reason=reason, owner=owner)
    context = dict(task.context or {})
    context["review_loop_terminal_outcome"] = {
        "kind": "cap_exhausted",
        "reason": reason,
        "resolved": False,
    }
    context["touched_critical"] = ["preserve-evidence.py"]
    context["hold_sentinel"] = sentinel
    task.context = context


def _assert_episode_consumed(task: Task) -> None:
    context = dict(task.context or {})
    assert task.requires_approval is False
    assert "_approval_set_at" not in context
    assert APPROVAL_HOLD_EPISODE_KEY not in context
    assert APPROVAL_HOLD_PROVENANCE_KEY not in context
    assert "approval_hold_reason" not in context
    assert "review_loop_terminal_outcome" not in context
    assert context["touched_critical"] == ["preserve-evidence.py"]


@pytest.mark.asyncio
async def test_status_terminalization_consumes_parent_and_cascade_hold_episodes(
    db_session, mocker
):
    """Terminal parent/child writes cannot leave metadata for a later retry."""

    _require_postgres(db_session)
    mocker.patch(
        "app.task_lifecycle.close_orphan_pr_if_any",
        new=AsyncMock(),
    )
    project = Project(name="Terminal approval release", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    parent = Task(
        project_id=project.id,
        title="Cancel parent",
        description="Cascade one child",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "parent"}],
    )
    _stamp_hold(
        parent,
        owner="security_parent",
        reason="parent security hold",
        sentinel="parent",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Cancel child",
        description="Cascade target",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "audit", "prompt": "child"}],
    )
    _stamp_hold(
        child,
        owner="security_child",
        reason="child security hold",
        sentinel="child",
    )
    db_session.add(child)
    await db_session.commit()
    parent_id = parent.id
    child_id = child.id

    await _apply_status_mutation(
        parent,
        {"status": TaskStatus.CANCELLED.value},
        BackgroundTasks(),
        db_session,
        False,
        {},
    )
    await db_session.commit()

    async with AsyncSessionLocal() as verify:
        fresh_parent = await verify.get(Task, parent_id)
        fresh_child = await verify.get(Task, child_id)
    assert fresh_parent is not None
    assert fresh_child is not None
    _assert_episode_consumed(fresh_parent)
    _assert_episode_consumed(fresh_child)


async def _seed_cap_hold(db_session, *, name: str) -> Task:
    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Reset cap source",
        description="Exact cap episode",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "cap source"}],
        context={"review_loop": {"pr_number": 42, "iteration": 9}},
    )
    _stamp_hold(
        task,
        owner="review_loop_convergence",
        reason="review_cap_exhausted",
        sentinel="cap",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_reset_iteration_cap_clears_only_exact_cap_episode(db_session, mocker):
    _require_postgres(db_session)
    task = await _seed_cap_hold(db_session, name="Exact cap release")
    task_id = task.id
    mocker.patch(
        "app.admin._get_user_task_with_role",
        new=AsyncMock(return_value=task),
    )

    result = await reset_iteration_cap(
        task_id,
        db_session,
        {"user_id": "test-user-123", "email": "operator@example.com"},
    )

    await db_session.rollback()
    fresh = await db_session.get(Task, task_id, populate_existing=True)
    assert fresh is not None
    _assert_episode_consumed(fresh)
    assert result["requires_approval_cleared"] is True
    assert ((fresh.context or {}).get("review_loop") or {})["iteration"] == 0


@pytest.mark.asyncio
async def test_reset_iteration_cap_refreshes_and_preserves_new_independent_hold(
    db_session, mocker
):
    """A peer hold winning after route lookup is never cleared or overwritten."""

    _require_postgres(db_session)
    stale_task = await _seed_cap_hold(db_session, name="Cap reset hold race")
    task_id = stale_task.id

    async def _authorize_then_replace(*_args, **_kwargs):
        async with AsyncSessionLocal() as peer:
            fresh = await peer.get(Task, task_id, populate_existing=True)
            assert fresh is not None
            _stamp_hold(
                fresh,
                owner="security_policy",
                reason="new independent security hold",
                sentinel="peer-won",
            )
            peer.add(fresh)
            await peer.commit()
        return stale_task

    mocker.patch(
        "app.admin._get_user_task_with_role",
        new=AsyncMock(side_effect=_authorize_then_replace),
    )

    result = await reset_iteration_cap(
        task_id,
        db_session,
        {"user_id": "test-user-123", "email": "operator@example.com"},
    )

    await db_session.rollback()
    fresh = await db_session.get(Task, task_id, populate_existing=True)
    assert fresh is not None
    context = dict(fresh.context or {})
    assert result["requires_approval_cleared"] is False
    assert fresh.requires_approval is True
    assert context["approval_hold_reason"] == "new independent security hold"
    assert context["hold_sentinel"] == "peer-won"
    assert context[APPROVAL_HOLD_PROVENANCE_KEY]["owner"] == "security_policy"
    assert context["touched_critical"] == ["preserve-evidence.py"]
    assert (context.get("review_loop") or {})["iteration"] == 0


@pytest.mark.asyncio
async def test_reset_iteration_cap_never_archives_a_concurrently_requeued_attempt(
    db_session, mocker
):
    """Candidate archival is conditional on the fresh FAILED task authority."""

    _require_postgres(db_session)
    task = await _seed_cap_hold(db_session, name="Cap reset archive race")
    candidate = Task(
        project_id=task.project_id,
        title="Failed review candidate",
        description="Peer requeues this row after the reset scan",
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/reset-race",
                "base_branch": "main",
            }
        ],
        post_commit_pending=False,
    )
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(candidate)
    task_id = task.id
    candidate_id = candidate.id
    mocker.patch(
        "app.admin._get_user_task_with_role",
        new=AsyncMock(return_value=task),
    )

    original_execute = db_session.execute
    select_count = 0

    async def _execute_with_requeue(statement, *args, **kwargs):
        nonlocal select_count
        result = await original_execute(statement, *args, **kwargs)
        if getattr(statement, "is_select", False):
            select_count += 1
            if select_count == 2:
                async with AsyncSessionLocal() as peer:
                    await peer.execute(
                        update(Task)
                        .where(Task.id == candidate_id)
                        .values(status=TaskStatus.QUEUED, archived=False)
                    )
                    await peer.commit()
        return result

    mocker.patch.object(db_session, "execute", side_effect=_execute_with_requeue)

    result = await reset_iteration_cap(
        task_id,
        db_session,
        {"user_id": "test-user-123", "email": "operator@example.com"},
    )

    async with AsyncSessionLocal() as verify:
        fresh_candidate = await verify.get(Task, candidate_id)
    assert fresh_candidate is not None
    assert fresh_candidate.status == TaskStatus.QUEUED
    assert fresh_candidate.archived is False
    assert result["archived_count"] == 0
