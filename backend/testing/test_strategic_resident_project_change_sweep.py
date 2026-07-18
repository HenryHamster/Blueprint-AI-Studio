"""Durable production cursors for authoritative Resident background wakeups."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from app.models import (
    Project,
    QualificationVerdict,
    ReleaseVersion,
    StrategicCycle,
    StrategicResident,
    StrategicResidentSweepState,
    Task,
)
from app import strategic_resident_project_change_sweep as sweep_module
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
async def test_durable_sweep_combines_terminal_cycle_and_qualification_cursor(
    db_session,
):
    now = _utcnow()
    project, _resident = await _authoritative_project(
        db_session,
        name="Durable wake",
    )
    paused_project, _paused_resident = await _authoritative_project(
        db_session,
        name="Paused durable wake",
        paused=True,
    )
    completed = StrategicCycle(
        id="sc-completed",
        project_id=project.id,
        outcome="completed",
        updated_at=now - timedelta(minutes=2),
    )
    active = StrategicCycle(
        id="sc-active",
        project_id=project.id,
        outcome="phase2_running",
        updated_at=now,
    )
    paused_cycle = StrategicCycle(
        id="sc-paused",
        project_id=paused_project.id,
        outcome="completed",
        updated_at=now,
    )
    version = ReleaseVersion(project_id=project.id)
    db_session.add_all([completed, active, paused_cycle, version])
    await db_session.flush()
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        terminal_status="not_promoted",
        created_at=now - timedelta(minutes=1),
    )
    db_session.add(verdict)
    await db_session.commit()
    factory = _factory(db_session)

    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=factory,
            now=now,
            batch_size=10,
        )
        == 1
    )
    await db_session.rollback()
    task = (await db_session.execute(select(Task))).scalar_one()
    cursor = task.first_payload["project_change_cursor"]
    assert "sc-completed" in cursor["strategic_cycle_terminal"]
    assert "sc-active" not in cursor["strategic_cycle_terminal"]
    assert str(verdict.id) in cursor["qualification_verdict"]
    assert "sc-paused" not in str(cursor)

    async with factory() as handled_session:
        handled_resident = await handled_session.get(
            type(_resident),
            project.id,
        )
        assert handled_resident is not None
        handled_resident.handled_project_change_cursor = cursor
        handled_session.add(handled_resident)
        await handled_session.commit()

    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=factory,
            now=now,
            batch_size=10,
        )
        == 0
    )
    await db_session.rollback()
    assert len((await db_session.execute(select(Task))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_durable_sweep_filters_handled_projects_before_batch_limit(db_session):
    now = _utcnow()
    seeded = []
    for index in range(3):
        project, resident = await _authoritative_project(
            db_session,
            name=f"Durable batch {index}",
        )
        cycle = StrategicCycle(
            id=f"sc-batch-{index}",
            project_id=project.id,
            outcome="completed",
            updated_at=now - timedelta(minutes=index),
        )
        db_session.add(cycle)
        seeded.append((project, resident, cycle))
    await db_session.commit()
    factory = _factory(db_session)

    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=factory,
            now=now,
            batch_size=1,
        )
        == 1
    )
    await db_session.rollback()
    first_task = (await db_session.execute(select(Task))).scalar_one()
    first_cursor = first_task.first_payload["project_change_cursor"]
    first_project_id = first_task.project_id

    async with factory() as handled_session:
        handled_resident = await handled_session.get(
            type(seeded[0][1]),
            first_project_id,
        )
        assert handled_resident is not None
        handled_resident.handled_project_change_cursor = first_cursor
        handled_session.add(handled_resident)
        await handled_session.commit()

    assert (
        await sweep_durable_project_change_wakeups(
            session_factory=factory,
            now=now,
            batch_size=1,
        )
        == 1
    )
    await db_session.rollback()
    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 2
    assert len({task.project_id for task in tasks}) == 2


@pytest.mark.asyncio
async def test_unchanged_durable_event_does_not_create_inflight_successor(db_session):
    now = _utcnow()
    project, resident = await _authoritative_project(db_session, name="In-flight")
    db_session.add(
        StrategicCycle(
            id="sc-inflight",
            project_id=project.id,
            outcome="completed",
            updated_at=now,
        )
    )
    await db_session.commit()
    factory = _factory(db_session)

    assert await sweep_durable_project_change_wakeups(
        session_factory=factory, now=now
    ) == 1
    assert await sweep_durable_project_change_wakeups(
        session_factory=factory, now=now
    ) == 0
    await db_session.rollback()
    assert len((await db_session.execute(select(Task))).scalars().all()) == 1
    await db_session.refresh(resident)
    assert resident.background_successor_task_id is None


@pytest.mark.asyncio
async def test_recovery_block_does_not_starve_later_residents(db_session):
    now = _utcnow()
    available_project_ids = set()
    for index in range(3):
        project, resident = await _authoritative_project(
            db_session, name=f"Recovery fairness {index}"
        )
        if index == 0:
            resident.recovery_state = {
                "unknown_operation_receipt_ids": ["receipt-unknown"]
            }
            db_session.add(resident)
        else:
            available_project_ids.add(project.id)
        db_session.add(
            StrategicCycle(
                id=f"sc-recovery-fairness-{index}",
                project_id=project.id,
                outcome="completed",
                updated_at=now,
            )
        )
    await db_session.commit()
    factory = _factory(db_session)

    for _ in range(3):
        await sweep_durable_project_change_wakeups(
            session_factory=factory, now=now, batch_size=1
        )
    await db_session.rollback()
    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert {task.project_id for task in tasks} == available_project_ids


@pytest.mark.asyncio
async def test_regressed_durable_candidate_never_opens_signal_transaction(
    db_session, monkeypatch
):
    now = _utcnow()
    project, resident = await _authoritative_project(db_session, name="Regressed")
    resident.handled_project_change_cursor = {
        "strategic_cycle_terminal": "v2:99999999999999999999:newer:completed"
    }
    db_session.add_all(
        [
            resident,
            StrategicCycle(
                id="sc-regressed",
                project_id=project.id,
                outcome="completed",
                updated_at=now,
            ),
        ]
    )
    await db_session.commit()
    signal = AsyncMock()
    monkeypatch.setattr(sweep_module, "signal_project_change", signal)

    assert await sweep_durable_project_change_wakeups(
        session_factory=_factory(db_session), now=now
    ) == 0
    signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_round_robin_position_survives_module_restart(db_session):
    """A leader restart cannot repeatedly strand projects past the first page."""

    now = _utcnow()
    project_ids = set()
    for index in range(6):
        project, _resident = await _authoritative_project(
            db_session, name=f"Restart fairness {index}"
        )
        project_ids.add(project.id)
        db_session.add(
            StrategicCycle(
                id=f"sc-restart-fairness-{index}",
                project_id=project.id,
                outcome="completed",
                updated_at=now,
            )
        )
    await db_session.commit()
    factory = _factory(db_session)

    assert await sweep_module.sweep_durable_project_change_wakeups(
        session_factory=factory, now=now, batch_size=1
    ) == 1
    await db_session.rollback()
    first_project_id = (await db_session.execute(select(Task))).scalar_one().project_id
    state = await db_session.get(
        StrategicResidentSweepState, "durable_project_change"
    )
    assert state is not None
    assert state.after_project_id == first_project_id

    # Reloading clears every module global exactly as a new leader process
    # would. The next sweep must continue from the durable singleton row.
    import importlib

    restarted = importlib.reload(sweep_module)
    assert await restarted.sweep_durable_project_change_wakeups(
        session_factory=factory, now=now, batch_size=1
    ) == 1
    await db_session.rollback()
    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 2
    assert len({task.project_id for task in tasks}) == 2
    assert {task.project_id for task in tasks}.issubset(project_ids)


@pytest.mark.asyncio
async def test_wake_budget_stays_at_32_while_scanning_200_projects(db_session):
    """Caller-controlled scan breadth cannot become a model-call burst."""

    now = _utcnow()
    projects = [
        Project(id=uuid.UUID(int=index + 1), name=f"Wide scan {index}", user_id="operator")
        for index in range(200)
    ]
    db_session.add_all(projects)
    await db_session.flush()
    residents = [
        StrategicResident(
            project_id=project.id,
            engine_owner="strategic_resident",
            engine_version="strategic-resident-v1",
            authority_generation=2,
            background_policy="active",
        )
        for project in projects
    ]
    db_session.add_all(residents)
    # Only the final 32 rows have a meaningful change. The sweep must cheaply
    # inspect the entire 200-row page, but may open no more than 32 turns.
    db_session.add_all(
        [
            StrategicCycle(
                id=f"sc-wide-{index}",
                project_id=project.id,
                outcome="completed",
                updated_at=now,
            )
            for index, project in enumerate(projects[-32:])
        ]
    )
    await db_session.commit()

    assert await sweep_durable_project_change_wakeups(
        session_factory=_factory(db_session),
        now=now,
        batch_size=200,
        scan_batch_size=200,
    ) == 32
    await db_session.rollback()
    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 32
    state = await db_session.get(
        StrategicResidentSweepState, "durable_project_change"
    )
    assert state is not None
    assert state.after_project_id == projects[-1].id
