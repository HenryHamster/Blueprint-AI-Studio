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


async def _terminal_review_task(db_session, *, pr_number: int) -> Task:
    project = Project(
        name=f"Bulk review admission {uuid.uuid4()}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Terminal review retry",
        payloads=[{"type": "review", "pr_number": pr_number, "prompt": "review"}],
        status=TaskStatus.FAILED,
        context={
            "review_loop": {
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": pr_number,
                    "base_sha": "stale-base",
                    "head_sha": "stale-head",
                }
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _terminal_pr_child(
    db_session,
    *,
    task_type: str,
    pr_number: int,
) -> tuple[Task, Task]:
    project = Project(
        name=f"PR retry hierarchy {uuid.uuid4()}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.flush()
    parent = Task(
        project_id=project.id,
        title="Open research rollup",
        payloads=[{"type": "research", "prompt": "parent"}],
        status=TaskStatus.EVALUATING,
        context={"rollup_pr_number": 7001},
    )
    db_session.add(parent)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title=f"Terminal {task_type} child",
        payloads=[
            {"type": task_type, "pr_number": pr_number, "prompt": task_type}
        ],
        status=TaskStatus.FAILED,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    await db_session.refresh(child)
    return parent, child


@pytest.mark.asyncio
async def test_manual_fix_retry_prelocks_ancestor_before_pr_task_admission(
    db_session, mocker
):
    """Manual PR retry never takes its child before the rollup ancestor."""

    from app.admin_task_mutations import modify_task_apply
    from app.fix_admission import FixAdmissionDecision
    from app.rollup_lineage_fence import (
        lock_ancestor_rollups_for_requeue as real_ancestor_lock,
    )

    parent, child = await _terminal_pr_child(
        db_session, task_type="fix", pr_number=5213
    )
    events: list[str] = []

    async def _pr_lock(*_args, **_kwargs):
        events.append("pr")

    async def _ancestor_lock(task, session):
        events.append("ancestor")
        return await real_ancestor_lock(task, session)

    async def _admit(*_args, **_kwargs):
        events.append("admission_task_rows")
        return FixAdmissionDecision(
            True,
            fix_target={
                "project_id": str(child.project_id),
                "pr_number": 5213,
                "base_sha": "base",
                "head_sha": "head",
            },
        )

    mocker.patch(
        "app.task_locking.acquire_pr_spawn_lock",
        new=_pr_lock,
    )
    mocker.patch(
        "app.rollup_lineage_fence.lock_ancestor_rollups_for_requeue",
        new=_ancestor_lock,
    )
    mocker.patch(
        "app.fix_admission.prepare_direct_fix_admission",
        new=_admit,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )

    await modify_task_apply(
        child,
        {"status": TaskStatus.QUEUED.value},
        BackgroundTasks(),
        db_session,
    )

    assert events == ["pr", "ancestor", "admission_task_rows"]
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert child.status == TaskStatus.QUEUED
    assert (parent.context or {})["_rollup_invalidated_by_descendant"][
        "descendant_task_id"
    ] == str(child.id)


@pytest.mark.asyncio
async def test_bulk_review_retry_prelocks_ancestor_before_pr_task_admission(
    db_session, mocker
):
    """Bulk PR retry uses the same PR -> ancestor -> child hierarchy order."""

    from app.admin import _bulk_retry_one_task
    from app.review_admission import ReviewAdmissionDecision
    from app.rollup_lineage_fence import (
        lock_ancestor_rollups_for_requeue as real_ancestor_lock,
    )

    parent, child = await _terminal_pr_child(
        db_session, task_type="review", pr_number=5214
    )
    events: list[str] = []

    async def _pr_lock(*_args, **_kwargs):
        events.append("pr")

    async def _ancestor_lock(task, session):
        events.append("ancestor")
        return await real_ancestor_lock(task, session)

    async def _admit(*_args, **_kwargs):
        events.append("admission_task_rows")
        return ReviewAdmissionDecision(
            True,
            review_target={
                "project_id": str(child.project_id),
                "pr_number": 5214,
                "base_sha": "base",
                "head_sha": "head",
            },
        )

    mocker.patch(
        "app.admin_retry_admission.acquire_pr_spawn_lock",
        new=_pr_lock,
    )
    mocker.patch(
        "app.admin_retry_admission.lock_ancestor_rollups_for_requeue",
        new=_ancestor_lock,
    )
    mocker.patch(
        "app.review_admission.prepare_direct_review_admission",
        new=_admit,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )

    await _bulk_retry_one_task(child.id, db_session)

    assert events == ["pr", "ancestor", "admission_task_rows"]
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert child.status == TaskStatus.QUEUED
    assert (parent.context or {})["_rollup_invalidated_by_descendant"][
        "descendant_task_id"
    ] == str(child.id)


@pytest.mark.asyncio
async def test_bulk_review_retry_rejects_competing_exact_owner(db_session, mocker):
    """Bulk retry must not bypass the canonical same-PR review owner."""

    from app.admin import _bulk_retry_one_task

    task = await _terminal_review_task(db_session, pr_number=5210)
    live_target = {
        "project_id": str(task.project_id),
        "pr_number": 5210,
        "base_sha": "live-base",
        "head_sha": "live-head",
    }
    owner = Task(
        project_id=task.project_id,
        title="Current exact review owner",
        payloads=[{"type": "review", "pr_number": 5210, "prompt": "owner"}],
        status=TaskStatus.IN_PROGRESS,
        context={"review_loop": {"review_target": live_target}},
    )
    db_session.add(owner)
    await db_session.commit()

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
                "base": {"sha": "live-base"},
                "head": {"sha": "live-head"},
            }
        ),
    )
    task_lock = mocker.patch(
        "app.admin.task_lock",
        side_effect=AssertionError("denied review must not acquire Task before PR"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _bulk_retry_one_task(task.id, db_session)

    assert exc_info.value.status_code == 409
    assert "exact-target review" in exc_info.value.detail
    task_lock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_bulk_review_retry_rejects_closed_pr(db_session, mocker):
    """A closed live PR is terminal before the bulk path takes the Task lock."""

    from app.admin import _bulk_retry_one_task

    task = await _terminal_review_task(db_session, pr_number=5211)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "closed",
                "merged": False,
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            }
        ),
    )
    task_lock = mocker.patch(
        "app.admin.task_lock",
        side_effect=AssertionError("closed PR must be denied before Task lock"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _bulk_retry_one_task(task.id, db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "PR is no longer open"
    task_lock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_bulk_review_retry_refreshes_stale_pr_target(db_session, mocker):
    """An admitted retry persists the exact current PR head, not stale proof."""

    from app.admin import _bulk_retry_one_task

    task = await _terminal_review_task(db_session, pr_number=5212)
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
                "base": {"sha": "fresh-base"},
                "head": {"sha": "fresh-head"},
            }
        ),
    )

    old_vm = await _bulk_retry_one_task(task.id, db_session)

    assert old_vm is None
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert (task.context or {})["review_loop"]["review_target"] == {
        "project_id": str(task.project_id),
        "pr_number": 5212,
        "base_sha": "fresh-base",
        "head_sha": "fresh-head",
    }


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
    project_task_lock = mocker.patch(
        "app.task_locking.lock_project_then_task",
        side_effect=AssertionError(
            "review/fix requeue must retain PR -> Task admission order"
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
    project_task_lock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.post_commit_pending is True
