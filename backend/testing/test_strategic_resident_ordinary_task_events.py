"""Admission and cadence tests for ordinary Task terminal outcomes."""

from __future__ import annotations

from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from app import strategic_resident_project_change_sweep as sweep_module
from app.models import (
    Project,
    StrategicResidentOrdinaryTaskTerminalEvent,
    Task,
    TaskStatus,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_project_change_sweep import (
    sweep_durable_project_change_wakeups,
)
from app.time_utils import _utcnow
from testing._resident_seed import seed_without_authority_enforcement


async def _authoritative_project(db_session, *, name: str, paused: bool = False):
    project = Project(name=name, user_id="operator")
    db_session.add(project)
    await db_session.flush()
    async with seed_without_authority_enforcement(db_session):
        resident = await initialize_strategic_resident(
            db_session,
            project.id,
            locked_project=project,
        )
        resident.engine_owner = "strategic_resident"
        resident.engine_version = "strategic-resident-v1"
        resident.authority_generation = 2
        resident.background_policy = "paused" if paused else "active"
        db_session.add(resident)
        await db_session.flush()
    return project, resident


def _factory(db_session):
    return async_sessionmaker(
        bind=db_session.bind,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.mark.asyncio
async def test_only_operator_root_outcomes_enter_ordinary_ledger_and_wake(
    db_session, monkeypatch
):
    project, _resident = await _authoritative_project(
        db_session, name="Admitted ordinary outcomes"
    )
    operator = Task(
        project_id=project.id,
        title="Operator mission",
        created_by="USER",
        payloads=[{"type": "create", "prompt": "bounded at inspection time"}],
        context={"large": "never copied to event"},
        status=TaskStatus.COMPLETED,
    )
    pipeline = Task(
        project_id=project.id,
        title="Routine pipeline child",
        created_by="ORCHESTRATOR",
        payloads=[{"type": "review"}],
        status=TaskStatus.FAILED,
    )
    resident_authored = Task(
        project_id=project.id,
        title="Must not recursively wake",
        created_by="STRATEGIC_AGENT",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
    )
    fixture_provenance = Task(
        project_id=project.id,
        title="Noncanonical fixture provenance is not operator authority",
        created_by="user",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([operator, pipeline, resident_authored, fixture_provenance])
    await db_session.flush()
    child = Task(
        project_id=project.id,
        parent_task_id=operator.id,
        title="Operator-provenance child is still pipeline detail",
        created_by="USER",
        payloads=[{"type": "fix"}],
        status=TaskStatus.CANCELLED,
    )
    db_session.add(child)
    await db_session.commit()

    events = list(
        (
            await db_session.execute(
                select(StrategicResidentOrdinaryTaskTerminalEvent).order_by(
                    StrategicResidentOrdinaryTaskTerminalEvent.sequence
                )
            )
        ).scalars()
    )
    assert len(events) == 1
    event = events[0]
    assert event.project_id == project.id
    assert event.task_id == operator.id
    assert event.event_kind == "COMPLETED"
    assert event.task_created_by == "USER"
    assert set(event.model_dump()) == {
        "sequence",
        "project_id",
        "task_id",
        "event_kind",
        "task_created_by",
    }

    signal = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(sweep_module, "signal_project_change", signal)
    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 1
    )
    cursor = signal.await_args.kwargs["project_change_cursor"]
    assert cursor == {
        "ordinary_task_terminal": (f"v2:{event.sequence:020d}:{operator.id}:COMPLETED")
    }


@pytest.mark.asyncio
async def test_ordinary_outcome_survives_revival_and_repeated_sweep_is_unchanged(
    db_session, monkeypatch
):
    project, resident = await _authoritative_project(
        db_session, name="Ordinary outcome revival"
    )
    task = Task(
        project_id=project.id,
        title="Revived operator mission",
        created_by="USER",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.commit()
    task.status = TaskStatus.QUEUED
    await db_session.commit()

    signal = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(sweep_module, "signal_project_change", signal)
    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 1
    )
    cursor = signal.await_args.kwargs["project_change_cursor"]
    assert cursor["ordinary_task_terminal"].endswith(f":{task.id}:FAILED")
    resident.handled_project_change_cursor = cursor
    db_session.add(resident)
    await db_session.commit()

    signal.reset_mock()
    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 0
    )
    signal.assert_not_awaited()

    task.status = TaskStatus.CANCELLED
    await db_session.commit()
    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 1
    )
    advanced = signal.await_args.kwargs["project_change_cursor"][
        "ordinary_task_terminal"
    ]
    assert advanced.endswith(f":{task.id}:CANCELLED")
    assert advanced > cursor["ordinary_task_terminal"]


@pytest.mark.asyncio
async def test_paused_resident_catches_up_to_ordinary_outcome_after_activation(
    db_session, monkeypatch
):
    project, resident = await _authoritative_project(
        db_session, name="Paused ordinary outcome", paused=True
    )
    task = Task(
        project_id=project.id,
        title="Done while paused",
        created_by="USER",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    signal = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(sweep_module, "signal_project_change", signal)

    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 0
    )
    signal.assert_not_awaited()
    resident.background_policy = "active"
    db_session.add(resident)
    await db_session.commit()
    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=_factory(db_session), now=_utcnow()
        )
        == 1
    )
    assert signal.await_args.kwargs["project_change_cursor"][
        "ordinary_task_terminal"
    ].endswith(f":{task.id}:COMPLETED")


@pytest.mark.asyncio
async def test_rolled_back_ordinary_terminal_transition_emits_no_event(db_session):
    project, _resident = await _authoritative_project(
        db_session, name="Rolled back ordinary outcome"
    )
    await db_session.commit()
    task = Task(
        project_id=project.id,
        title="Rolled back operator mission",
        created_by="USER",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.flush()
    assert (
        await db_session.execute(select(StrategicResidentOrdinaryTaskTerminalEvent))
    ).scalar_one()
    await db_session.rollback()
    assert (
        await db_session.execute(select(StrategicResidentOrdinaryTaskTerminalEvent))
    ).scalar_one_or_none() is None
