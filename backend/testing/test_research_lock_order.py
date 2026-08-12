"""Lock-order regressions for research completion entry points."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.schemas import AgentStatusPayload
from app.task_capabilities import get_task_capability


@pytest.mark.asyncio
async def test_normal_research_dispatch_passes_exact_locked_project(mocker):
    """Normal completion reuses Project -> Task's exact Project instance."""
    from app.task_lifecycle_completion_dispatch import run_completion_dispatch

    project = Project(id=uuid.uuid4(), name="Research lock project", user_id="user")
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Accepted research",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs="[]",
    )
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
    )
    session = AsyncMock()

    returned_task, response = await run_completion_dispatch(
        task,
        task_type="research",
        payload=payload,
        task_id=task.id,
        session=session,
        locked_project=project,
        preallocated_strategic_message_sequences=[],
        _peek_capability=get_task_capability("research"),
        _peek_task_type="research",
        _expected_post_commit_claim_token=None,
        _expected_childless_recovery_token=None,
        refetch=AsyncMock(),
    )

    assert returned_task is task
    assert response is None
    handler.assert_awaited_once()
    call = handler.await_args
    assert call.args == (task, "[]", session)
    assert call.kwargs["project"] is project


@pytest.mark.asyncio
async def test_sidecar_research_resume_relocks_project_then_source(db_session):
    """The sidecar resume boundary refreshes both rows in canonical order."""
    from app.task_lifecycle import _lock_sidecar_research_resume

    project = Project(name="Sidecar lock project", user_id="user")
    db_session.add(project)
    await db_session.flush()
    source = Task(
        project_id=project.id,
        title="Parked research source",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(source)
    await db_session.commit()

    locked_project, locked_source = await _lock_sidecar_research_resume(
        db_session,
        project_id=project.id,
        task_id=source.id,
    )

    assert locked_project is project
    assert locked_source is source


@pytest.mark.asyncio
async def test_spawn_boundary_rejects_parent_cancelled_after_completion_commit(
    db_session,
):
    """A stale completion worker cannot publish children after cancellation."""
    from conftest import TestingSessionLocal
    from app.research_execution_authority import lock_research_spawn_boundary

    project = Project(name="Cancelled spawn project", user_id="user")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="Completed research awaiting spawn",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        context={"research_depth": 0},
    )
    db_session.add(task)
    await db_session.commit()

    stale_context = {**(task.context or {}), "locally_parsed": True}
    async with TestingSessionLocal() as cancelling_session:
        cancelled = await cancelling_session.get(Task, task.id)
        assert cancelled is not None
        cancelled.status = TaskStatus.CANCELLED
        await cancelling_session.commit()

    locked_task, prior_children = await lock_research_spawn_boundary(
        db_session,
        task,
        local_context=stale_context,
        plan_output=[{"title": "must not publish"}],
        latest_status_message="stale completion",
    )

    assert locked_task is None
    assert prior_children == []
    await db_session.rollback()
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert "locally_parsed" not in (task.context or {})


@pytest.mark.asyncio
async def test_prelock_research_reads_never_autoflush_parent_task(
    db_session, mocker
):
    """Local completion state stays detached until Project -> Task is locked."""
    from sqlalchemy import event
    from conftest import TestingSessionLocal
    from app.knowledge_query import fetch_knowledge_substance_index as real_fetch
    from app.research_completion import _handle_research_completion
    from app.research_execution_authority import (
        lock_research_spawn_boundary as real_spawn_boundary,
    )
    from test_autonomous_research import (
        _active_direction,
        _make_finding,
        _make_research_task,
        _patch_research_externals,
    )

    direction = _active_direction("d-no-autoflush", "No autoflush")
    project = Project(
        name="No autoflush project",
        user_id="user",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    task = await _make_research_task(
        project,
        direction,
        db_session,
        status=TaskStatus.COMPLETED,
    )
    finding = _make_finding("Publish only after wiring", direction["id"])
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    probe = {"active": False}
    forbidden_flushes = []

    async def _commit_branch_then_arm(parent, *_args):
        await _args[-1].commit()
        probe["active"] = True
        return parent

    async def _disarm_then_lock(*args, **kwargs):
        probe["active"] = False
        return await real_spawn_boundary(*args, **kwargs)

    mocker.patch("app.task_completion._ensure_branch", _commit_branch_then_arm)
    mocker.patch(
        "app.research_completion.lock_research_spawn_boundary",
        side_effect=_disarm_then_lock,
    )

    def _record_prelock_flush(sync_session, _flush_context, _instances):
        if probe["active"] and task in sync_session.dirty:
            forbidden_flushes.append(task.status)

    event.listen(db_session.sync_session, "before_flush", _record_prelock_flush)

    async def _cancel_then_fetch(session, project_id):
        async with TestingSessionLocal() as cancelling_session:
            cancelled = await cancelling_session.get(Task, task.id)
            assert cancelled is not None
            cancelled.status = TaskStatus.CANCELLED
            await cancelling_session.commit()
        return await real_fetch(session, project_id)

    mocker.patch(
        "app.knowledge_query.fetch_knowledge_substance_index",
        side_effect=_cancel_then_fetch,
    )
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        event.remove(
            db_session.sync_session, "before_flush", _record_prelock_flush
        )

    assert forbidden_flushes == []
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_post_branch_project_lock_waits_for_task_lock_release(db_session, mocker):
    """The Task-only lock returned by branch setup is released before Project."""
    import app.research_completion as research_completion
    from test_autonomous_research import (
        _active_direction,
        _make_finding,
        _make_research_task,
        _patch_research_externals,
    )

    direction = _active_direction("d-post-branch-lock", "Canonical lock order")
    project = Project(
        name="Post-branch lock project",
        user_id="user",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    task = await _make_research_task(
        project, direction, db_session, status=TaskStatus.COMPLETED
    )
    observation = _make_finding("Persist observation", direction["id"])
    observation.update(
        {"type": "design_doc_observation", "section": "architecture"}
    )
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[observation],
    )

    phase = {"task_lock_held": False, "release_commits": 0, "project_lock": False}
    real_commit = db_session.commit
    real_design_lock = research_completion.with_design_doc_locked

    async def _tracked_commit():
        await real_commit()
        if phase["task_lock_held"]:
            phase["task_lock_held"] = False
            phase["release_commits"] += 1

    async def _return_task_locked(parent, *_args):
        await _args[-1].commit()
        phase["task_lock_held"] = True
        return parent

    async def _assert_released_before_project(*args, **kwargs):
        if phase["release_commits"]:
            assert phase["task_lock_held"] is False
            phase["project_lock"] = True
        return await real_design_lock(*args, **kwargs)

    mocker.patch.object(db_session, "commit", side_effect=_tracked_commit)
    mocker.patch("app.task_completion._ensure_branch", _return_task_locked)
    mocker.patch.object(
        research_completion,
        "with_design_doc_locked",
        side_effect=_assert_released_before_project,
    )

    await research_completion._handle_research_completion(task, "", db_session)

    assert phase["release_commits"] == 1
    assert phase["project_lock"] is True
