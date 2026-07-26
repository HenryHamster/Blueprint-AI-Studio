"""Focused tests for durable Strategic task watches."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models import Project, Task, TaskStatus
from app.auth import create_agent_token
from app.strategic_watches import (
    WATCH_ACKNOWLEDGED,
    WATCH_CANCELLED,
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
async def test_fired_receipt_backlog_does_not_starve_later_pending_transition(
    db_session,
):
    project, owner, target = await _project_and_tasks(db_session)
    old_watch_ids = []
    for index in range(21):
        completed = Task(
            project_id=project.id,
            title=f"Old completed task {index}",
            created_by="STRATEGIC_AGENT",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
        )
        db_session.add(completed)
        await db_session.flush()
        old_watch, _ = await register_task_watch(
            db_session,
            project_id=project.id,
            created_by_task_id=owner.id,
            target_task_id=completed.id,
            purpose="unacknowledged old receipt",
        )
        old_watch_ids.append(old_watch.id)
    later_watch, _ = await register_task_watch(
        db_session,
        project_id=project.id,
        created_by_task_id=owner.id,
        target_task_id=target.id,
        purpose="must resolve behind the receipt backlog",
    )
    await db_session.commit()

    target.status = TaskStatus.COMPLETED
    db_session.add(target)
    await db_session.commit()

    visible = await resolve_project_task_watches(
        db_session,
        project_id=project.id,
    )
    await db_session.commit()
    await db_session.refresh(later_watch)

    assert [watch.id for watch in visible] == old_watch_ids[:20]
    assert later_watch.state == WATCH_FIRED
    assert later_watch.terminal_status == TaskStatus.COMPLETED.value


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


def _strategic_headers(task: Task) -> dict[str, str]:
    token = create_agent_token(
        str(task.id),
        "watch-ops-test-vm",
        task_type="strategic_review",
    )
    return {"x-agent-token": token}


@pytest.mark.asyncio
async def test_watch_operations_create_replay_fire_and_acknowledge(
    client,
    db_session,
):
    project, owner, target = await _project_and_tasks(db_session)
    headers = _strategic_headers(owner)
    request = {
        "task_id": str(owner.id),
        "op": "watch_task",
        "data": {
            "target_task_id": str(target.id),
            "purpose": "report the exact research result",
        },
    }

    response = await client.post("/api/agent/strategic", json=request, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["watch"]["target_task_id"] == str(target.id)
    assert body["watch"]["state"] == WATCH_PENDING
    watch_id = body["watch"]["id"]

    replay = await client.post("/api/agent/strategic", json=request, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["watch"]["id"] == watch_id

    target.status = TaskStatus.COMPLETED
    target.latest_status_message = "research artifact ready"
    db_session.add(target)
    await db_session.commit()
    visible = await resolve_project_task_watches(
        db_session,
        project_id=project.id,
    )
    await db_session.commit()
    assert [str(item.id) for item in visible] == [watch_id]

    acknowledged = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(owner.id),
            "op": "ack_watch",
            "data": {
                "watch_id": watch_id,
                "disposition": "reported",
                "note": "included in the operator update",
            },
        },
        headers=headers,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["watch"]["state"] == WATCH_ACKNOWLEDGED
    assert acknowledged.json()["watch"]["acknowledgement_disposition"] == "reported"


@pytest.mark.asyncio
async def test_watch_operations_enforce_strict_shape_project_and_state(
    client,
    db_session,
):
    project, owner, target = await _project_and_tasks(db_session)
    headers = _strategic_headers(owner)

    extra = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(owner.id),
            "op": "watch_task",
            "data": {
                "target_task_id": str(target.id),
                "purpose": "wait",
                "poll_interval": 5,
            },
        },
        headers=headers,
    )
    assert extra.status_code == 400, extra.text
    assert "extra_forbidden" in extra.text

    self_watch = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(owner.id),
            "op": "watch_task",
            "data": {"target_task_id": str(owner.id)},
        },
        headers=headers,
    )
    assert self_watch.status_code == 400, self_watch.text

    other = Project(name="Other watch API project", user_id="test-user")
    db_session.add(other)
    await db_session.flush()
    foreign_target = Task(
        project_id=other.id,
        title="Foreign target",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(foreign_target)
    await db_session.commit()
    foreign = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(owner.id),
            "op": "watch_task",
            "data": {"target_task_id": str(foreign_target.id)},
        },
        headers=headers,
    )
    assert foreign.status_code == 404, foreign.text


@pytest.mark.asyncio
async def test_cancel_watch_operation_is_idempotent(client, db_session):
    _project, owner, target = await _project_and_tasks(db_session)
    headers = _strategic_headers(owner)
    created = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(owner.id),
            "op": "watch_task",
            "data": {
                "target_task_id": str(target.id),
                "purpose": "no longer needed after superseding run",
            },
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    watch_id = created.json()["watch"]["id"]
    request = {
        "task_id": str(owner.id),
        "op": "cancel_watch",
        "data": {"watch_id": watch_id},
    }

    cancelled = await client.post(
        "/api/agent/strategic",
        json=request,
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["watch"]["state"] == WATCH_CANCELLED
    replay = await client.post(
        "/api/agent/strategic",
        json=request,
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["watch"]["state"] == WATCH_CANCELLED
