"""Focused tests for durable Strategic task watches."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models import Project, Task, TaskStatus
from app.strategic_watches import (
    WATCH_ACKNOWLEDGED,
    WATCH_EXPIRED,
    WATCH_FIRED,
    WATCH_PENDING,
    acknowledge_watch,
    register_task_watch,
    resolve_project_task_watches,
)
from app.time_utils import _utcnow


async def _project_and_tasks(db_session, *, terminal: bool = False):
    project = Project(name=f"Strategic Watch {uuid.uuid4().hex[:8]}", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    owner = Task(
        project_id=project.id,
        title="Resident Strategic",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    target = Task(
        project_id=project.id,
        title="Exact awaited result",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.COMPLETED if terminal else TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
        latest_status_message="finished cleanly" if terminal else "working",
    )
    db_session.add(owner)
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(owner)
    await db_session.refresh(target)
    return project, owner, target


@pytest.mark.asyncio
async def test_task_watch_fires_once_and_remains_until_acknowledged(db_session):
    project, owner, target = await _project_and_tasks(db_session)
    watch, created = await register_task_watch(
        db_session,
        project_id=project.id,
        created_by_task_id=owner.id,
        target_task_id=target.id,
        purpose="report the research result",
    )
    await db_session.commit()
    assert created is True
    assert watch.state == WATCH_PENDING

    target.status = TaskStatus.COMPLETED
    target.latest_status_message = "measurement available"
    db_session.add(target)
    await db_session.commit()

    visible = await resolve_project_task_watches(
        db_session,
        project_id=project.id,
    )
    await db_session.commit()
    assert [row.id for row in visible] == [watch.id]
    assert visible[0].state == WATCH_FIRED
    assert visible[0].receipt["target_task_id"] == str(target.id)
    assert visible[0].receipt["purpose"] == "report the research result"

    # Resolution is idempotent and the receipt remains visible until ack.
    visible_again = await resolve_project_task_watches(
        db_session,
        project_id=project.id,
    )
    assert [row.id for row in visible_again] == [watch.id]

    acknowledged = await acknowledge_watch(
        db_session,
        project_id=project.id,
        watch_id=watch.id,
        disposition="reported",
        note="operator received the result",
    )
    await db_session.commit()
    assert acknowledged.state == WATCH_ACKNOWLEDGED

    assert (
        await resolve_project_task_watches(db_session, project_id=project.id)
    ) == []


@pytest.mark.asyncio
async def test_already_terminal_watch_fires_immediately_and_registration_replays(
    db_session,
):
    project, owner, target = await _project_and_tasks(db_session, terminal=True)
    watch, created = await register_task_watch(
        db_session,
        project_id=project.id,
        created_by_task_id=owner.id,
        target_task_id=target.id,
        purpose="inspect final document",
    )
    await db_session.commit()
    assert created is True
    assert watch.state == WATCH_FIRED
    assert watch.terminal_status == TaskStatus.COMPLETED.value

    replay, replay_created = await register_task_watch(
        db_session,
        project_id=project.id,
        created_by_task_id=owner.id,
        target_task_id=target.id,
        purpose="different retry text is ignored",
    )
    assert replay_created is False
    assert replay.id == watch.id


@pytest.mark.asyncio
async def test_pending_watch_expires_visibly(db_session):
    project, owner, target = await _project_and_tasks(db_session)
    watch, _ = await register_task_watch(
        db_session,
        project_id=project.id,
        created_by_task_id=owner.id,
        target_task_id=target.id,
        purpose="bounded wait",
        deadline_at=_utcnow() - timedelta(seconds=1),
    )
    await db_session.commit()

    visible = await resolve_project_task_watches(
        db_session,
        project_id=project.id,
    )
    assert [row.id for row in visible] == [watch.id]
    assert visible[0].state == WATCH_EXPIRED
    assert visible[0].receipt["reason"] == "watch_deadline_elapsed"


@pytest.mark.asyncio
async def test_watch_rejects_cross_project_target(db_session):
    project, owner, _ = await _project_and_tasks(db_session)
    other = Project(name="Other watch project", user_id="test-user")
    db_session.add(other)
    await db_session.flush()
    target = Task(
        project_id=other.id,
        title="Other project task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(target)
    await db_session.commit()

    with pytest.raises(ValueError, match="not found in project"):
        await register_task_watch(
            db_session,
            project_id=project.id,
            created_by_task_id=owner.id,
            target_task_id=target.id,
        )
