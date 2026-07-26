"""Focused tests for durable, unified Strategic task dispatch."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import (
    CompletionDebtEntry,
    Project,
    StrategicSpawnRequest,
    StrategicWatch,
    Task,
    TaskStatus,
)
from app.strategic_spawn_requests import (
    cancel_deferred_spawn_request,
    drain_deferred_spawn_requests,
    drain_due_strategic_spawn_requests,
    submit_strategic_spawn,
)
from app.time_utils import _utcnow


async def _owner(db_session):
    project = Project(name=f"Spawn requests {uuid.uuid4().hex[:8]}", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    owner = Task(
        project_id=project.id,
        title="Resident Strategic",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(owner)
    await db_session.commit()
    return project, owner


def _spec(title: str, *, watch: bool = True) -> dict:
    return {
        "task": {
            "title": title,
            "description": "bounded work",
            "payloads": [{"type": "create", "prompt": "implement it"}],
            "context": {"spawned_by": "strategic_direct"},
            "priority": 100,
            "watch": watch,
        }
    }


@pytest.mark.asyncio
async def test_spawn_request_dispatches_once_and_registers_one_watch(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Implement verified fix"),
        idempotency_key="stable-request",
        direction_id=None,
    )
    replay = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Implement verified fix"),
        idempotency_key="stable-request",
        direction_id=None,
    )

    assert first.request.state == "dispatched"
    assert replay.replayed is True
    assert replay.task.id == first.task.id
    assert len(
        (
            await db_session.execute(
                select(StrategicSpawnRequest).where(
                    StrategicSpawnRequest.parent_task_id == owner.id
                )
            )
        )
        .scalars()
        .all()
    ) == 1
    watches = (
        (
            await db_session.execute(
                select(StrategicWatch).where(
                    StrategicWatch.created_by_task_id == owner.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [watch.target_task_id for watch in watches] == [first.task.id]


@pytest.mark.asyncio
async def test_reused_idempotency_key_rejects_different_specification(db_session):
    project, owner = await _owner(db_session)
    await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First"),
        idempotency_key="same-key",
        direction_id=None,
    )
    with pytest.raises(ValueError, match="different spawn specification"):
        await submit_strategic_spawn(
            db_session,
            project_id=project.id,
            parent_task_id=owner.id,
            task_type="create",
            specification=_spec("Second"),
            idempotency_key="same-key",
            direction_id=None,
        )


@pytest.mark.asyncio
async def test_abuse_burst_defers_then_drains_fifo(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First", watch=False),
        idempotency_key="first",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    second = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Second", watch=False),
        idempotency_key="second",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert first.task is not None
    assert second.task is None
    assert second.request.state == "deferred"

    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    second.request.not_before = _utcnow() - timedelta(seconds=1)
    db_session.add(first.request)
    db_session.add(second.request)
    await db_session.commit()

    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert [row.id for row in drained] == [second.request.id]
    await db_session.refresh(second.request)
    assert second.request.state == "dispatched"
    assert second.request.task_id is not None


@pytest.mark.asyncio
async def test_cancelled_deferred_request_never_drains(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First", watch=False),
        idempotency_key="first",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Cancelled", watch=False),
        idempotency_key="cancelled",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert deferred.request.state == "deferred"

    await cancel_deferred_spawn_request(
        db_session,
        project_id=project.id,
        request_id=deferred.request.id,
    )
    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    db_session.add(first.request)
    await db_session.commit()

    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert drained == []
    await db_session.refresh(deferred.request)
    assert deferred.request.state == "cancelled"
    assert deferred.request.task_id is None


@pytest.mark.asyncio
async def test_orchestrator_sweep_drains_without_heartbeat_and_links_debt_once(
    db_session,
):
    project, owner = await _owner(db_session)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind="capability_trial",
        dedupe_key=f"strategic-spawn:{uuid.uuid4()}",
        title="Follow through on accepted Strategic work",
        detail={},
    )
    db_session.add(debt)
    await db_session.commit()

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Occupy the abuse window", watch=False),
        idempotency_key="occupy-window",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    deferred_spec = _spec("Deferred linked work", watch=False)
    deferred_spec["debt_id"] = str(debt.id)
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=deferred_spec,
        idempotency_key="deferred-linked",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert deferred.request.state == "deferred"

    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    deferred.request.not_before = _utcnow() - timedelta(seconds=1)
    db_session.add(first.request)
    db_session.add(deferred.request)
    await db_session.commit()

    drained = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert [row.id for row in drained] == [deferred.request.id]

    # A later orchestrator tick is a no-op and the idempotent debt ledger keeps
    # exactly one forward pointer for the task created by the deferred request.
    assert (
        await drain_due_strategic_spawn_requests(
            db_session,
            burst_cap=1,
            burst_window_minutes=60,
        )
    ) == []
    await db_session.commit()
    await db_session.refresh(deferred.request)
    await db_session.refresh(debt)
    linked = (debt.detail or {}).get("linked_tasks") or []
    assert [row["task_id"] for row in linked] == [str(deferred.request.task_id)]
