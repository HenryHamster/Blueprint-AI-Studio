"""Requeue boundaries must preserve pending review/fix continuations."""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import services
from app.models import Project, Task, TaskStatus


async def _pending_pr_task(db_session, task_type: str) -> Task:
    project = Project(
        name=f"Pending {task_type} requeue {uuid.uuid4()}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title=f"Pending {task_type}",
        payloads=[{"type": task_type, "pr_number": 5201, "prompt": "continue"}],
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "attempts": 0,
                "total_attempts": 0,
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["review", "fix"])
async def test_manual_requeue_rejects_pending_pr_continuation(
    db_session, task_type
):
    from app.admin_task_mutations import modify_task_apply

    task = await _pending_pr_task(db_session, task_type)

    with pytest.raises(HTTPException) as exc_info:
        await modify_task_apply(
            task,
            {"status": TaskStatus.QUEUED.value},
            BackgroundTasks(),
            db_session,
        )

    assert exc_info.value.status_code == 409
    assert "post-commit continuation is pending" in exc_info.value.detail
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["review", "fix"])
async def test_bulk_retry_rejects_pending_pr_continuation(db_session, task_type):
    from app.admin import _bulk_retry_one_task

    task = await _pending_pr_task(db_session, task_type)

    with pytest.raises(HTTPException) as exc_info:
        await _bulk_retry_one_task(
            task.id,
            db_session,
            preloaded_has_children=False,
        )

    assert exc_info.value.status_code == 409
    assert "post-commit continuation is pending" in exc_info.value.detail
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["review", "fix"])
async def test_manual_requeue_revalidates_stale_pending_after_pr_admission(
    db_session, mocker, task_type
):
    """A completion that marks pending after route load cannot become QUEUED."""

    from app.admin_task_mutations import modify_task_apply

    task = await _pending_pr_task(db_session, task_type)
    task.post_commit_pending = False
    task.context = {}
    db_session.add(task)
    await db_session.commit()
    assert task.post_commit_pending is False

    async with services.AsyncSessionLocal() as peer:
        current = await peer.get(Task, task.id)
        current.post_commit_pending = True
        current.context = {
            "_post_commit_pending_meta": {
                "generation_token": uuid.uuid4().hex,
                "payload": {"task_id": str(task.id), "status": "COMPLETED"},
            }
        }
        peer.add(current)
        await peer.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            }
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await modify_task_apply(
            task,
            {"status": TaskStatus.QUEUED.value},
            BackgroundTasks(),
            db_session,
        )

    assert exc_info.value.status_code == 409
    assert "pending post-commit continuation" in exc_info.value.detail
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True
