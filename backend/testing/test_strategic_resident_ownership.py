"""Focused ownership cutover and rollback tests for the Strategic Resident."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import (
    Project,
    ProjectMessage,
    ProjectMessageSequence,
    StrategicCycle,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_cutover_boundary import (
    CUTOVER_CHECKPOINT_AUTHORIZATION_KEY,
    append_live_cutover_checkpoint,
)
from app.project_message_creation import lock_project_and_next_message_sequence
from app.project_message_sequence_fence import (
    lock_project_message_sequence_boundary,
)
from app.strategic_resident_message_locking import (
    lock_resident_message_owner_fence,
    lock_resident_message_owner_state,
)
from app.strategic_resident_ownership import (
    ResidentCheckpointBoundaryError,
    LegacyResidentDrainRequired,
    ResidentOwnershipStateError,
    UnreconciledResidentOperations,
    cutover_to_strategic_resident,
    rollback_to_legacy_resident,
)
from app.strategic_resident_cleanup import (
    _finish_cleanup,
    run_resident_transition_cleanup,
    sweep_resident_transition_cleanup,
)
from app.strategic_resident_transition_locks import (
    lock_and_validate_legacy_authority,
    lock_project_resident_tasks,
)
from app.strategic_resident_session import (
    _legacy_resident_engine_enabled,
    _auto_start_on_workday_entry,
    renew_resident_session_lease,
    start_resident_session,
)
from app.strategic_resident_turns import (
    INPUT_FROZEN_CONTEXT_KEY,
    MUTATION_AUTHORIZED_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    STRATEGIC_RESIDENT_TURN_TASK_TYPE,
    coalesce_shadow_resident_turn,
)
from testing.conftest import TestingSessionLocal, USE_POSTGRES


def test_ownership_mutation_fences_take_project_update_before_resident():
    for helper in (
        lock_resident_message_owner_fence,
        _legacy_resident_engine_enabled,
        lock_project_resident_tasks,
    ):
        source = inspect.getsource(helper)
        project_at = source.index("select(Project")
        update_at = source.index(".with_for_update(key_share=True)", project_at)
        resident_at = source.index("StrategicResident", update_at)
        assert project_at < update_at < resident_at
        assert "read=True" not in source[project_at:resident_at]


@pytest.mark.asyncio
async def test_owner_fences_refresh_preloaded_project_and_resident(ownership_factory):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as message_session, ownership_factory() as legacy_session:
        for session in (message_session, legacy_session):
            assert await session.get(Project, project_id) is not None
            assert await session.get(StrategicResident, project_id) is not None
            await session.commit()

        async with ownership_factory() as writer, writer.begin():
            project = await writer.get(Project, project_id)
            assert project is not None
            project.vision = {
                **(project.vision or {}),
                "_strategic_refresh": {"requested_at": "2026-07-17T00:00:00"},
                "_resident_strategic_session": {
                    "session_id": "fresh-session",
                    "task_id": str(uuid.uuid4()),
                    "status": "active",
                    "stop_requested": False,
                },
            }

        project, strategic_owner, refresh_pending, active, session_id = (
            await lock_resident_message_owner_state(message_session, project_id)
        )
        assert project.vision.get("_strategic_refresh") is not None
        assert strategic_owner is False
        assert refresh_pending is True
        assert active is True
        assert session_id == "fresh-session"

        async with ownership_factory() as writer, writer.begin():
            resident = await writer.get(StrategicResident, project_id)
            assert resident is not None
            resident.engine_owner = "strategic_resident"

        assert not await _legacy_resident_engine_enabled(
            legacy_session,
            project_id,
            lock_transition_rows=True,
        )


@pytest_asyncio.fixture()
async def ownership_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield factory
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _seed_project(
    factory,
    *,
    private_vision: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session, session.begin():
        project = Project(
            name="Ownership transition",
            user_id="operator",
            problem_model={
                "purpose": "Keep strategic ownership durable",
                "_private_charter_cache": "preserve",
            },
            vision={
                "purpose": "Ship the Strategic Resident",
                "_runtime_policy": {"keep": True},
                **(private_vision or {}),
            },
            directions=[
                {"id": "direction-1", "text": "Cut over safely", "status": "active"}
            ],
            strategic_memory_notes=[{"note": "Never lose the checkpoint"}],
            running_update="Resident cutover is ready.",
        )
        session.add(project)
        await session.flush()
        resident = await initialize_strategic_resident(
            session,
            project.id,
            locked_project=project,
        )
        resident.handled_message_sequence = 41
        resident.handled_project_change_cursor = {"project_revision": 7}
        resident.scheduling_state = {
            "deadline_at": "2026-07-17T12:00:00",
            "shadow_last_enqueue_at": "discard-on-cutover",
        }
        session.add(resident)
        return project.id, resident.current_checkpoint_id


def _resident_turn(
    *,
    project_id: uuid.UUID,
    generation: int,
    owner: str,
    shadow: bool,
    status: TaskStatus,
    input_frozen: bool,
    checkpoint_version: int = 1,
    checkpoint_hash: str = "a" * 64,
    lane: str = "interactive",
) -> Task:
    return Task(
        project_id=project_id,
        title="Resident turn",
        description="Ownership transition fixture",
        payloads=[
            {
                "type": STRATEGIC_RESIDENT_TURN_TASK_TYPE,
                "resident_id": str(project_id),
                "authority_generation": generation,
                "engine_owner": owner,
                "shadow_only": shadow,
                "checkpoint_version": checkpoint_version,
                "checkpoint_hash": checkpoint_hash,
                "lane": lane,
            }
        ],
        context={
            SHADOW_CONTEXT_KEY: shadow,
            MUTATION_AUTHORIZED_CONTEXT_KEY: not shadow,
            INPUT_FROZEN_CONTEXT_KEY: input_frozen,
            "_resident_project_id": str(project_id),
            "_resident_authority_generation": generation,
            "_resident_engine_owner_observed": owner,
            "_resident_lane": lane,
        },
        status=status,
        archived=True,
        created_by="ORCHESTRATOR",
    )


@pytest.mark.asyncio
async def test_cutover_fences_all_shadow_work_and_preserves_checkpoint_and_cursors(
    ownership_factory,
):
    project_id, checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        first = await coalesce_shadow_resident_turn(
            session,
            project_id=project_id,
            lane="interactive",
            trigger_kind="operator_message",
            message_sequence=1,
        )
        second = await coalesce_shadow_resident_turn(
            session,
            project_id=project_id,
            lane="interactive",
            trigger_kind="operator_message",
            message_sequence=2,
        )
        third = await coalesce_shadow_resident_turn(
            session,
            project_id=project_id,
            lane="background",
            trigger_kind="project_change",
            project_change_cursor={"revision": 8},
        )
        stray = _resident_turn(
            project_id=project_id,
            generation=1,
            owner="legacy",
            shadow=True,
            status=TaskStatus.QUEUED,
            input_frozen=False,
        )
        session.add(stray)
        await session.flush()
        expected_task_ids = {
            first.task_id,
            second.task_id,
            third.task_id,
            stray.id,
        }

    transition = await cutover_to_strategic_resident(
        ownership_factory,
        project_id,
        now=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert transition.previous_owner == "legacy"
    assert transition.new_owner == "strategic_resident"
    assert transition.previous_generation == 1
    assert transition.new_generation == 2
    assert transition.checkpoint_id != checkpoint_id
    assert transition.checkpoint_version == 2
    assert set(transition.cancelled_task_ids) == expected_task_ids
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_owner == "strategic_resident"
        assert resident.authority_generation == 2
        assert resident.current_checkpoint_id == transition.checkpoint_id
        assert resident.previous_checkpoint_id == checkpoint_id
        assert resident.handled_message_sequence == 0
        assert resident.handled_project_change_cursor["baseline_kind"] == (
            "legacy_cutover"
        )
        assert resident.active_turn_task_id is None
        assert resident.interactive_successor_task_id is None
        assert resident.background_successor_task_id is None
        assert resident.health_status == "recovering"
        assert resident.rollout_state["shadow"] is False
        assert CUTOVER_CHECKPOINT_AUTHORIZATION_KEY not in resident.rollout_state
        boundary_checkpoint = await session.get(
            StrategicResidentCheckpoint,
            transition.checkpoint_id,
        )
        assert boundary_checkpoint is not None
        boundary = boundary_checkpoint.checkpoint["migration"]["cutover_boundary"]
        assert boundary["previous_owner"] == "legacy"
        assert boundary["previous_authority_generation"] == 1
        assert boundary["previous_checkpoint_id"] == str(checkpoint_id)
        assert boundary["source_turn_task_id"] == str(
            boundary_checkpoint.source_turn_task_id
        )
        assert uuid.UUID(boundary["transition_id"])
        assert resident.scheduling_state["deadline_at"] == "2026-07-17T12:00:00"
        assert "shadow_last_enqueue_at" not in resident.scheduling_state
        tasks = (
            (await session.execute(select(Task).where(Task.id.in_(expected_task_ids))))
            .scalars()
            .all()
        )
        assert {task.status for task in tasks} == {TaskStatus.CANCELLED}
        assert all(
            task.context["_resident_superseded_by_generation"] == 2 for task in tasks
        )
        assert all(
            task.context[MUTATION_AUTHORIZED_CONTEXT_KEY] is False for task in tasks
        )


@pytest.mark.asyncio
async def test_cutover_is_idempotent_without_advancing_generation_again(
    ownership_factory,
):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    first = await cutover_to_strategic_resident(ownership_factory, project_id)
    second = await cutover_to_strategic_resident(ownership_factory, project_id)

    assert first.already_owned is False
    assert second.already_owned is True
    assert second.previous_generation == second.new_generation == 2
    assert second.cancelled_task_ids == ()
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.authority_generation == 2


def test_cutover_boundary_locks_sequence_before_scanning_messages():
    boundary_source = inspect.getsource(append_live_cutover_checkpoint)
    assert boundary_source.index("lock_project_message_sequence_boundary") < (
        boundary_source.index("_conversation_boundary")
    )
    fence_source = inspect.getsource(lock_project_message_sequence_boundary)
    assert "INSERT INTO projectmessagesequence AS counter" in fence_source
    assert "counter.last_sequence," in fence_source
    assert "counter.last_sequence +" not in fence_source
    cutover_source = inspect.getsource(cutover_to_strategic_resident)
    boundary_at = cutover_source.index("append_live_cutover_checkpoint")
    release_at = cutover_source.index(
        "release_cancelled_shadow_message_claims",
        boundary_at,
    )
    seed_at = cutover_source.index("seed_cutover_conversation_turn", release_at)
    assert boundary_at < release_at < seed_at


@pytest.mark.asyncio
async def test_cutover_releases_only_cancelled_shadow_message_claims(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        wake = await coalesce_shadow_resident_turn(
            session,
            project_id=project_id,
            lane="interactive",
            trigger_kind="operator_message",
            message_sequence=1,
        )
        assert wake.task_id is not None
        shadow_task_id = wake.task_id
        message = ProjectMessage(
            project_id=project_id,
            sequence=1,
            surface="conversation",
            handling_state="claimed",
            handled_by_task_id=shadow_task_id,
            from_type="user",
            content="A shadow claim must transfer at cutover.",
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        message = await session.get(ProjectMessage, message_id)
        shadow = await session.get(Task, shadow_task_id)
        assert resident is not None and message is not None and shadow is not None
        assert shadow.status == TaskStatus.CANCELLED
        assert message.handling_state == "pending"
        assert message.handled_by_task_id is None
        assert resident.active_turn_task_id is not None
        authoritative = await session.get(Task, resident.active_turn_task_id)
        assert authoritative is not None
        assert authoritative.id != shadow_task_id
        assert authoritative.first_payload["message_sequence_start"] == 1
        assert authoritative.first_payload["message_sequence_end"] == 1


@pytest.mark.asyncio
async def test_cutover_rejects_arbitrary_foreign_unresolved_message_claim(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        foreign_task = Task(
            project_id=project_id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
            context={},
        )
        session.add(foreign_task)
        await session.flush()
        message = ProjectMessage(
            project_id=project_id,
            sequence=1,
            surface="conversation",
            handling_state="claimed",
            handled_by_task_id=foreign_task.id,
            from_type="user",
            content="Do not steal this non-shadow claim.",
        )
        session.add(message)
        await session.flush()
        foreign_task_id = foreign_task.id
        message_id = message.id

    with pytest.raises(
        ResidentOwnershipStateError,
        match="non-shadow Tasks",
    ):
        await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        message = await session.get(ProjectMessage, message_id)
        foreign = await session.get(Task, foreign_task_id)
        assert resident is not None and message is not None and foreign is not None
        assert resident.engine_owner == "legacy"
        assert message.handling_state == "claimed"
        assert message.handled_by_task_id == foreign.id


@pytest.mark.asyncio
async def test_rollback_fences_authoritative_turns_and_projects_checkpoint_to_legacy(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        project = await session.get(Project, project_id)
        assert resident is not None and project is not None
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            resident.current_checkpoint_id,
        )
        assert checkpoint is not None
        project.vision = {
            **dict(project.vision or {}),
            "_runtime_policy": {"keep": "newer-private-value"},
        }
        active = _resident_turn(
            project_id=project_id,
            generation=2,
            owner="strategic_resident",
            shadow=False,
            status=TaskStatus.IN_PROGRESS,
            input_frozen=True,
            checkpoint_version=checkpoint.version,
            checkpoint_hash=checkpoint.content_hash,
        )
        successor = _resident_turn(
            project_id=project_id,
            generation=2,
            owner="strategic_resident",
            shadow=False,
            status=TaskStatus.QUEUED,
            input_frozen=False,
            checkpoint_version=checkpoint.version,
            checkpoint_hash=checkpoint.content_hash,
        )
        session.add_all([active, successor])
        await session.flush()
        resident.active_turn_task_id = active.id
        resident.active_turn_lane = "interactive"
        resident.interactive_successor_task_id = successor.id
        session.add(resident)
        session.add(project)

    transition = await rollback_to_legacy_resident(
        ownership_factory,
        project_id,
        now=datetime(2026, 7, 16, 13, 0, 0, tzinfo=timezone.utc),
    )

    assert transition.previous_owner == "strategic_resident"
    assert transition.new_owner == "legacy"
    assert transition.previous_generation == 2
    assert transition.new_generation == 3
    assert transition.legacy_projection_applied is True
    assert set(transition.cancelled_task_ids) == {active.id, successor.id}
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        project = await session.get(Project, project_id)
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            transition.checkpoint_id,
        )
        assert resident is not None and project is not None and checkpoint is not None
        assert resident.engine_owner == "legacy"
        assert resident.authority_generation == 3
        assert resident.current_checkpoint_id == transition.checkpoint_id
        assert resident.handled_message_sequence == 0
        assert resident.handled_project_change_cursor["baseline_kind"] == (
            "legacy_cutover"
        )
        assert resident.active_turn_task_id is None
        assert resident.interactive_successor_task_id is None
        assert resident.next_wakeup_at is None
        assert (
            project.problem_model["purpose"]
            == (checkpoint.checkpoint["charter"]["purpose"])
        )
        assert project.problem_model["_private_charter_cache"] == "preserve"
        assert project.vision["purpose"] == "Ship the Strategic Resident"
        assert project.vision["_runtime_policy"] == {"keep": "newer-private-value"}
        legacy = project.vision["_resident_strategic_session"]
        assert legacy["status"] == "ended"
        assert legacy["stop_requested"] is True
        assert legacy["rollback_checkpoint_id"] == str(transition.checkpoint_id)
        assert project.directions[0]["id"] == "direction-1"
        assert project.strategic_memory_notes[0]["note"] == (
            "Never lose the checkpoint"
        )
        assert project.running_update == "Resident cutover is ready."
        tasks = (
            (
                await session.execute(
                    select(Task).where(Task.id.in_([active.id, successor.id]))
                )
            )
            .scalars()
            .all()
        )
        assert {task.status for task in tasks} == {TaskStatus.CANCELLED}
        assert all(
            task.context["_resident_superseded_by_generation"] == 3 for task in tasks
        )


@pytest.mark.asyncio
async def test_rollback_refuses_unreconciled_receipt_without_partial_changes(
    ownership_factory,
):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            resident.current_checkpoint_id,
        )
        assert checkpoint is not None
        active = _resident_turn(
            project_id=project_id,
            generation=2,
            owner="strategic_resident",
            shadow=False,
            status=TaskStatus.IN_PROGRESS,
            input_frozen=True,
            checkpoint_version=checkpoint.version,
            checkpoint_hash=checkpoint.content_hash,
        )
        session.add(active)
        await session.flush()
        resident.active_turn_task_id = active.id
        resident.active_turn_lane = "interactive"
        session.add(resident)
        receipt = StrategicOperationReceipt(
            project_id=project_id,
            turn_task_id=active.id,
            authority_generation=2,
            operation_id="unknown-external-effect",
            operation_kind="deployment",
            capability="deploy",
            risk_level="high",
            request_hash="a" * 64,
            expected_revisions={},
            status="unknown_effect",
            unknown_effects=[{"kind": "deployment", "revision": "candidate"}],
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    with pytest.raises(UnreconciledResidentOperations) as exc_info:
        await rollback_to_legacy_resident(ownership_factory, project_id)
    assert exc_info.value.receipt_ids == (receipt_id,)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        task = await session.get(Task, active.id)
        assert resident is not None and task is not None
        assert resident.engine_owner == "strategic_resident"
        assert resident.authority_generation == 2
        assert resident.active_turn_task_id == active.id
        assert task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_rollback_is_idempotent_after_legacy_restoration(ownership_factory):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    first = await rollback_to_legacy_resident(ownership_factory, project_id)
    second = await rollback_to_legacy_resident(ownership_factory, project_id)

    assert first.already_owned is False
    assert second.already_owned is True
    assert second.previous_generation == second.new_generation == 3
    assert second.legacy_projection_applied is False


@pytest.mark.asyncio
async def test_cutover_requires_a_concrete_checkpoint_boundary(ownership_factory):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.current_checkpoint_id = None
        session.add(resident)

    with pytest.raises(ResidentCheckpointBoundaryError):
        await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_owner == "legacy"
        assert resident.authority_generation == 1


@pytest.mark.asyncio
async def test_cutover_refuses_a_stale_checkpoint_pointer(ownership_factory):
    project_id, _checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        checkpoint = (
            await session.execute(
                select(StrategicResidentCheckpoint).where(
                    StrategicResidentCheckpoint.project_id == project_id,
                    StrategicResidentCheckpoint.version == 1,
                )
            )
        ).scalar_one()
        assert resident is not None
        session.add(
            StrategicResidentCheckpoint(
                project_id=project_id,
                version=2,
                schema_version=1,
                content_hash="b" * 64,
                checkpoint=dict(checkpoint.checkpoint),
                authority_generation=resident.authority_generation,
                source_turn_task_id=uuid.uuid4(),
            )
        )

    with pytest.raises(ResidentCheckpointBoundaryError):
        await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_owner == "legacy"
        assert resident.authority_generation == 1


@pytest.mark.asyncio
async def test_cutover_refuses_live_legacy_session_task_and_lease(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        task = Task(
            project_id=project_id,
            title="Legacy Resident",
            payloads=[{"type": "strategic_review"}],
            context={
                "_resident_session": True,
                "strategic_session_id": "legacy-session",
            },
            status=TaskStatus.IN_PROGRESS,
        )
        session.add(task)
        await session.flush()
        project = await session.get(Project, project_id)
        assert project is not None
        project.vision = {
            **dict(project.vision or {}),
            "_resident_strategic_session": {
                "session_id": "legacy-session",
                "task_id": str(task.id),
                "status": "active",
                "stop_requested": False,
            },
            "_strategic_local_continuation": {"enabled": True},
        }
        session.add(project)

    with pytest.raises(LegacyResidentDrainRequired):
        await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session, session.begin():
        project = await session.get(Project, project_id)
        task = await session.get(Task, task.id)
        assert project is not None and task is not None
        task.status = TaskStatus.CANCELLED
        vision = dict(project.vision or {})
        vision.pop("_strategic_local_continuation", None)
        vision["_resident_strategic_session"] = {
            **dict(vision["_resident_strategic_session"]),
            "status": "ended",
            "stop_requested": True,
        }
        project.vision = vision
        session.add_all([project, task])

    transition = await cutover_to_strategic_resident(
        ownership_factory, project_id
    )
    assert transition.new_owner == "strategic_resident"


@pytest.mark.asyncio
async def test_active_legacy_cycle_blocks_cutover_but_not_idempotent_rollback(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        session.add(
            StrategicCycle(
                id=f"sc-{uuid.uuid4().hex[:12]}",
                project_id=project_id,
                outcome="phase1_running",
            )
        )

    with pytest.raises(LegacyResidentDrainRequired, match="StrategicCycle"):
        await cutover_to_strategic_resident(ownership_factory, project_id)

    transition = await rollback_to_legacy_resident(ownership_factory, project_id)
    assert transition.already_owned is True
    assert transition.new_owner == "legacy"


@pytest.mark.asyncio
async def test_legacy_authority_cas_uses_reserved_generation_for_missing_resident(
    ownership_factory,
):
    async with ownership_factory() as session, session.begin():
        project = Project(name="Missing Resident CAS", user_id="operator")
        session.add(project)
        await session.flush()
        project_id = project.id

    async with ownership_factory() as session, session.begin():
        generation = await lock_and_validate_legacy_authority(session, project_id)

    assert generation == 0


@pytest.mark.asyncio
async def test_cutover_rejects_cross_project_resident_pointer(ownership_factory):
    first_id, _ = await _seed_project(ownership_factory)
    second_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        foreign = _resident_turn(
            project_id=second_id,
            generation=1,
            owner="legacy",
            shadow=True,
            status=TaskStatus.QUEUED,
            input_frozen=True,
        )
        session.add(foreign)
        await session.flush()
        resident = await session.get(StrategicResident, first_id)
        assert resident is not None
        resident.active_turn_task_id = foreign.id
        resident.active_turn_lane = "interactive"
        session.add(resident)

    with pytest.raises(
        ResidentOwnershipStateError, match="cross-project"
    ):
        await cutover_to_strategic_resident(ownership_factory, first_id)
    async with ownership_factory() as session:
        foreign = await session.get(Task, foreign.id)
        assert foreign is not None
        assert foreign.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_cutover_rejects_tampered_checkpoint_hash(ownership_factory):
    project_id, checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        checkpoint = await session.get(StrategicResidentCheckpoint, checkpoint_id)
        assert checkpoint is not None
        body = dict(checkpoint.checkpoint)
        body["tampered"] = True
        checkpoint.checkpoint = body
        session.add(checkpoint)

    with pytest.raises(ResidentCheckpointBoundaryError, match="content hash"):
        await cutover_to_strategic_resident(ownership_factory, project_id)


@pytest.mark.asyncio
async def test_cutover_appends_live_boundary_and_preserves_pending_gap(
    ownership_factory,
):
    project_id, initial_checkpoint_id = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        project = await session.get(Project, project_id)
        assert project is not None
        project.problem_model = {
            "purpose": "Latest locked charter",
            "revision": 9,
            "_private_charter_cache": "new-private",
        }
        project.vision = {
            "purpose": "Latest locked strategy",
            "revision": "vision-12",
            "_runtime_policy": {"keep": "latest-private"},
        }
        project.directions = [
            {"id": "latest-direction", "text": "Use live state", "status": "active"}
        ]
        project.running_update = "Latest live update."
        session.add(project)
        session.add_all(
            [
                ProjectMessage(
                    project_id=project_id,
                    sequence=1,
                    surface="conversation",
                    handling_state="responded",
                    content="handled",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=2,
                    surface="conversation",
                    handling_state="superseded",
                    content="superseded",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=3,
                    surface="conversation",
                    handling_state="pending",
                    content="must remain pending",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=4,
                    surface="conversation",
                    handling_state="responded",
                    content="handled after a gap",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=5,
                    surface="activity",
                    handling_state="pending",
                    content="activity does not set the conversation cursor",
                ),
            ]
        )

    cutover_at = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
    transition = await cutover_to_strategic_resident(
        ownership_factory,
        project_id,
        now=cutover_at,
    )
    assert transition.checkpoint_version == 2
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            transition.checkpoint_id,
        )
        assert resident is not None and checkpoint is not None
        assert resident.previous_checkpoint_id == initial_checkpoint_id
        assert resident.handled_message_sequence == 2
        assert checkpoint.checkpoint["cursors"]["handled_message_sequence"] == 2
        assert (
            checkpoint.checkpoint["working_strategy"]["vision"]["purpose"]
            == "Latest locked strategy"
        )
        assert checkpoint.checkpoint["charter"]["purpose"] == "Latest locked charter"
        assert (
            checkpoint.checkpoint["working_strategy"]["directions"][0]["id"]
            == "latest-direction"
        )
        assert resident.handled_project_change_cursor["vision_revision"] == (
            "vision-12"
        )
        assert resident.handled_project_change_cursor["problem_model_revision"] == 9
        assert resident.next_wakeup_at == cutover_at.replace(tzinfo=None)
        assert resident.active_turn_task_id is not None
        assert resident.active_turn_lane == "interactive"
        turn = await session.get(Task, resident.active_turn_task_id)
        assert turn is not None
        assert turn.status == TaskStatus.QUEUED
        assert turn.first_payload["message_sequence_start"] == 3
        assert turn.first_payload["message_sequence_end"] == 4
        assert turn.first_payload["authority_generation"] == 2
        assert turn.first_payload["engine_owner"] == "strategic_resident"
        assert turn.context[MUTATION_AUTHORIZED_CONTEXT_KEY] is True
        assert turn.context[INPUT_FROZEN_CONTEXT_KEY] is True


@pytest.mark.asyncio
async def test_paused_cutover_still_seeds_pending_interactive_turn(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.background_policy = "paused"
        session.add(resident)
        session.add_all(
            [
                ProjectMessage(
                    project_id=project_id,
                    sequence=8,
                    surface="conversation",
                    handling_state="pending",
                    from_type="user",
                    content="Do not strand this while paused",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=9,
                    surface="conversation",
                    handling_state="responded",
                    from_type="user",
                    content="Existing tail still belongs in frozen context",
                ),
            ]
        )

    await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.background_policy == "paused"
        assert resident.handled_message_sequence == 7
        assert resident.active_turn_task_id is not None
        turn = await session.get(Task, resident.active_turn_task_id)
        assert turn is not None
        assert turn.first_payload["message_sequence_start"] == 8
        assert turn.first_payload["message_sequence_end"] == 9
        assert turn.first_payload["lane"] == "interactive"
        seeded_task_id = turn.id

    await rollback_to_legacy_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        seeded = await session.get(Task, seeded_task_id)
        pending = await session.scalar(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.sequence == 8,
            )
        )
        assert resident is not None and seeded is not None and pending is not None
        assert resident.engine_owner == "legacy"
        assert resident.active_turn_task_id is None
        assert seeded.status == TaskStatus.CANCELLED
        assert pending.handling_state == "pending"
        assert not any(
            str(key).startswith("cutover_pending_message_")
            for key in resident.scheduling_state
        )


@pytest.mark.asyncio
async def test_cutover_with_no_pending_messages_seeds_no_turn(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        session.add_all(
            [
                ProjectMessageSequence(
                    project_id=project_id,
                    last_sequence=30,
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=11,
                    surface="conversation",
                    handling_state="responded",
                    from_type="user",
                    content="Already handled",
                ),
                ProjectMessage(
                    project_id=project_id,
                    sequence=12,
                    surface="conversation",
                    handling_state="superseded",
                    from_type="user",
                    content="Already superseded",
                ),
            ]
        )

    await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.handled_message_sequence == 12
        assert resident.active_turn_task_id is None
        counter = await session.get(ProjectMessageSequence, project_id)
        assert counter is not None
        assert counter.last_sequence == 30
        resident_tasks = (
            (
                await session.execute(
                    select(Task).where(Task.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert not any(
            task.first_payload.get("type") == STRATEGIC_RESIDENT_TURN_TASK_TYPE
            for task in resident_tasks
        )


@pytest.mark.asyncio
async def test_cutover_does_not_absorb_activity_or_system_messages(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        foreign_task = Task(
            project_id=project_id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
            context={},
        )
        session.add(foreign_task)
        await session.flush()
        foreign_task_id = foreign_task.id
        system_message = ProjectMessage(
            project_id=project_id,
            sequence=20,
            surface="conversation",
            handling_state="claimed",
            handled_by_task_id=foreign_task_id,
            from_type="system",
            content="Routine system projection must not become Resident work.",
        )
        activity_message = ProjectMessage(
            project_id=project_id,
            sequence=21,
            surface="activity",
            handling_state="pending",
            from_type="user",
            content="Activity remains in its independent inbox.",
        )
        session.add_all([system_message, activity_message])
        await session.flush()
        system_message_id = system_message.id
        activity_message_id = activity_message.id

    await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        system_message = await session.get(ProjectMessage, system_message_id)
        activity_message = await session.get(ProjectMessage, activity_message_id)
        assert resident is not None
        assert system_message is not None
        assert activity_message is not None
        assert resident.handled_message_sequence == 20
        assert resident.active_turn_task_id is None
        assert system_message.handling_state == "claimed"
        assert system_message.handled_by_task_id == foreign_task_id
        assert activity_message.handling_state == "pending"
        assert activity_message.handled_by_task_id is None


@pytest.mark.asyncio
async def test_rollback_private_checkpoint_keys_cannot_overwrite_live_private_state(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        project = await session.get(Project, project_id)
        assert resident is not None and project is not None
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            resident.current_checkpoint_id,
        )
        assert checkpoint is not None
        body = json.loads(json.dumps(checkpoint.checkpoint))
        body["charter"]["_private_charter_cache"] = "stale"
        body["working_strategy"]["vision"]["_runtime_policy"] = {"keep": "stale"}
        checkpoint.checkpoint = body
        checkpoint.content_hash = hashlib.sha256(
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        project.problem_model = {
            **dict(project.problem_model or {}),
            "_private_charter_cache": "newest",
        }
        project.vision = {
            **dict(project.vision or {}),
            "_runtime_policy": {"keep": "newest"},
        }
        session.add_all([checkpoint, project])

    await rollback_to_legacy_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        assert project.problem_model["_private_charter_cache"] == "newest"
        assert project.vision["_runtime_policy"] == {"keep": "newest"}


@pytest.mark.asyncio
async def test_transition_api_rejects_caller_session(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session:
        with pytest.raises(TypeError, match="async_sessionmaker"):
            await cutover_to_strategic_resident(session, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_owner == "legacy"


@pytest.mark.asyncio
async def test_idempotent_cutover_audits_stranded_shadow_work(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        stranded = _resident_turn(
            project_id=project_id,
            generation=1,
            owner="legacy",
            shadow=True,
            status=TaskStatus.QUEUED,
            input_frozen=True,
        )
        session.add(stranded)
    with pytest.raises(
        ResidentOwnershipStateError, match="stranded shadow"
    ):
        await cutover_to_strategic_resident(ownership_factory, project_id)


@pytest.mark.asyncio
async def test_rollback_restores_exact_legacy_engine_version(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.engine_version = "legacy-v9"
        session.add(resident)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    await rollback_to_legacy_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_version == "legacy-v9"


@pytest.mark.asyncio
async def test_rollback_cleanup_failure_is_durable_and_retried(
    ownership_factory,
    monkeypatch,
):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        checkpoint = await session.get(
            StrategicResidentCheckpoint,
            resident.current_checkpoint_id,
        )
        assert checkpoint is not None
        active = _resident_turn(
            project_id=project_id,
            generation=resident.authority_generation,
            owner="strategic_resident",
            shadow=False,
            status=TaskStatus.IN_PROGRESS,
            input_frozen=True,
            checkpoint_version=checkpoint.version,
            checkpoint_hash=checkpoint.content_hash,
        )
        session.add(active)
        await session.flush()
        resident.active_turn_task_id = active.id
        resident.active_turn_lane = "interactive"
        session.add(resident)

    async def fail_cleanup(**_kwargs):
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(
        "app.strategic_resident_cleanup._run_external_cleanup",
        fail_cleanup,
    )
    await rollback_to_legacy_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        item = resident.recovery_state["task_cleanup"][0]
        assert item["status"] == "failed"
        assert resident.health_status == "recovering"

    async def cleanup_succeeds(**_kwargs):
        return True, False

    monkeypatch.setattr(
        "app.strategic_resident_cleanup._run_external_cleanup",
        cleanup_succeeds,
    )
    assert (
        await sweep_resident_transition_cleanup(
            session_factory=ownership_factory,
        )
        == 1
    )
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        item = resident.recovery_state["task_cleanup"][0]
        assert item["status"] == "complete"
        assert item["attempt_count"] == 2
        assert resident.health_status == "unknown"


@pytest.mark.asyncio
async def test_interrupted_cleanup_becomes_unknown_effect(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    task_id = uuid.uuid4()
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.health_status = "recovering"
        resident.recovery_state = {
            "task_cleanup": [
                {
                    "task_id": str(task_id),
                    "status": "applying",
                    "attempt_count": 1,
                }
            ]
        }
        session.add(resident)

    await run_resident_transition_cleanup(ownership_factory, project_id)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        item = resident.recovery_state["task_cleanup"][0]
        assert item["status"] == "unknown_effect"
        assert resident.health_status == "blocked"


@pytest.mark.parametrize(
    "cleanup_status",
    ["pending", "failed", "applying", "unknown_effect"],
)
@pytest.mark.asyncio
async def test_recutover_preserves_unsettled_rollback_cleanup(
    ownership_factory,
    cleanup_status,
):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    await rollback_to_legacy_resident(ownership_factory, project_id)
    task_id = uuid.uuid4()
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        item = {
            "task_id": str(task_id),
            "status": cleanup_status,
            "authority_generation": resident.authority_generation,
            "attempt_count": 1,
        }
        if cleanup_status == "applying":
            item.update(
                claim_id=str(uuid.uuid4()),
                claimed_at=datetime.now(timezone.utc).isoformat(),
            )
        resident.health_status = (
            "blocked" if cleanup_status == "unknown_effect" else "recovering"
        )
        resident.recovery_state = {"task_cleanup": [item]}
        session.add(resident)
        expected_state = json.loads(json.dumps(resident.recovery_state))

    with pytest.raises(ResidentOwnershipStateError, match="recutover is fenced"):
        await cutover_to_strategic_resident(ownership_factory, project_id)

    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.engine_owner == "legacy"
        assert resident.recovery_state == expected_state


@pytest.mark.asyncio
async def test_recutover_allows_completed_rollback_cleanup(ownership_factory):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    await rollback_to_legacy_resident(ownership_factory, project_id)
    async with ownership_factory() as session, session.begin():
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.recovery_state = {
            "task_cleanup": [
                {"task_id": str(uuid.uuid4()), "status": "complete"}
            ]
        }
        session.add(resident)

    transition = await cutover_to_strategic_resident(
        ownership_factory,
        project_id,
    )
    assert transition.new_owner == "strategic_resident"


@pytest.mark.asyncio
async def test_cleanup_finish_requires_exact_generation_and_claim(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    task = _resident_turn(
        project_id=project_id,
        generation=1,
        owner="legacy",
        shadow=True,
        status=TaskStatus.CANCELLED,
        input_frozen=True,
    )
    claim_id = str(uuid.uuid4())
    async with ownership_factory() as session, session.begin():
        session.add(task)
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.health_status = "recovering"
        resident.recovery_state = {
            "task_cleanup": [
                {
                    "task_id": str(task.id),
                    "status": "applying",
                    "claim_id": claim_id,
                    "authority_generation": resident.authority_generation,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        session.add(resident)

    assert not await _finish_cleanup(
        ownership_factory,
        project_id,
        task_id=task.id,
        claim_id=str(uuid.uuid4()),
        authority_generation=1,
        status="complete",
        error=None,
        account_released=True,
    )
    assert not await _finish_cleanup(
        ownership_factory,
        project_id,
        task_id=task.id,
        claim_id=claim_id,
        authority_generation=2,
        status="complete",
        error=None,
        account_released=True,
    )
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        stored_task = await session.get(Task, task.id)
        assert resident is not None and stored_task is not None
        assert resident.recovery_state["task_cleanup"][0]["status"] == "applying"
        assert (stored_task.context or {}).get("_slot_released") is not True

    assert await _finish_cleanup(
        ownership_factory,
        project_id,
        task_id=task.id,
        claim_id=claim_id,
        authority_generation=1,
        status="complete",
        error=None,
        account_released=True,
    )
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        stored_task = await session.get(Task, task.id)
        assert resident is not None and stored_task is not None
        assert resident.recovery_state["task_cleanup"][0]["status"] == "complete"
        assert (stored_task.context or {}).get("_slot_released") is True


@pytest.mark.asyncio
async def test_live_cleanup_claim_survives_sweep_and_fences_cutover(
    ownership_factory,
    monkeypatch,
):
    project_id, _ = await _seed_project(ownership_factory)
    task = _resident_turn(
        project_id=project_id,
        generation=1,
        owner="legacy",
        shadow=True,
        status=TaskStatus.CANCELLED,
        input_frozen=True,
    )
    async with ownership_factory() as session, session.begin():
        session.add(task)
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        resident.health_status = "recovering"
        resident.recovery_state = {
            "task_cleanup": [
                {
                    "task_id": str(task.id),
                    "status": "pending",
                    "authority_generation": resident.authority_generation,
                    "attempt_count": 0,
                }
            ]
        }
        session.add(resident)

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def pause_external_cleanup(**_kwargs):
        cleanup_started.set()
        await release_cleanup.wait()
        return True, False

    monkeypatch.setattr(
        "app.strategic_resident_cleanup._run_external_cleanup",
        pause_external_cleanup,
    )
    cleanup_run = asyncio.create_task(
        run_resident_transition_cleanup(ownership_factory, project_id)
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)

    assert (
        await sweep_resident_transition_cleanup(
            session_factory=ownership_factory,
        )
        == 1
    )
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        claimed = resident.recovery_state["task_cleanup"][0]
        assert claimed["status"] == "applying"
        assert claimed["claim_id"]
        assert claimed["authority_generation"] == 1

    with pytest.raises(ResidentOwnershipStateError, match="recutover is fenced"):
        await cutover_to_strategic_resident(ownership_factory, project_id)

    release_cleanup.set()
    await asyncio.wait_for(cleanup_run, timeout=2)
    async with ownership_factory() as session:
        resident = await session.get(StrategicResident, project_id)
        assert resident is not None
        assert resident.recovery_state["task_cleanup"][0]["status"] == "complete"
    transition = await cutover_to_strategic_resident(
        ownership_factory,
        project_id,
    )
    assert transition.new_owner == "strategic_resident"


@pytest.mark.asyncio
async def test_legacy_start_sync_and_heartbeat_stand_down_after_cutover(
    ownership_factory,
):
    project_id, _ = await _seed_project(ownership_factory)
    await cutover_to_strategic_resident(ownership_factory, project_id)
    async with ownership_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        with pytest.raises(Exception) as exc_info:
            await start_resident_session(
                session,
                project,
                operator_user_id="operator",
            )
        assert getattr(exc_info.value, "status_code", None) == 409

    assert (
        await _auto_start_on_workday_entry(
            project_id,
            operator_user_id="operator",
            session_factory=ownership_factory,
        )
        is None
    )

    legacy_task_id = uuid.uuid4()
    async with ownership_factory() as session, session.begin():
        project = await session.get(Project, project_id)
        assert project is not None
        project.vision = {
            **dict(project.vision or {}),
            "_resident_strategic_session": {
                "session_id": "stale-session",
                "task_id": str(legacy_task_id),
                "operator_user_id": "operator",
                "status": "active",
                "stop_requested": False,
            },
        }
        session.add(project)
    async with ownership_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        heartbeat = await renew_resident_session_lease(
            session,
            project,
            session_id="stale-session",
            task_id=str(legacy_task_id),
        )
        assert heartbeat["authority_held"] is False
        assert heartbeat["stop_requested"] is True
        assert heartbeat["status"] == "ended"


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="ownership-transition serialization requires PostgreSQL row locks",
)
@pytest.mark.asyncio
async def test_cutover_sequence_fence_captures_concurrent_accepted_message(
    db_session,
):
    project = Project(
        name=f"Concurrent cutover message {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await initialize_strategic_resident(db_session, project.id)
    await db_session.commit()
    writer_holds_sequence = asyncio.Event()
    release_writer = asyncio.Event()

    async def accept_message():
        async with TestingSessionLocal() as session, session.begin():
            sequence = await lock_project_and_next_message_sequence(
                session,
                project.id,
            )
            session.add(
                ProjectMessage(
                    project_id=project.id,
                    sequence=sequence,
                    surface="conversation",
                    handling_state="pending",
                    from_type="user",
                    content="Commit across the cutover boundary.",
                )
            )
            await session.flush()
            writer_holds_sequence.set()
            await release_writer.wait()

    writer = asyncio.create_task(accept_message())
    await asyncio.wait_for(writer_holds_sequence.wait(), timeout=3)
    cutover = asyncio.create_task(
        cutover_to_strategic_resident(TestingSessionLocal, project.id)
    )
    await asyncio.sleep(0.1)
    assert not cutover.done()
    release_writer.set()
    await asyncio.wait_for(asyncio.gather(writer, cutover), timeout=8)

    async with TestingSessionLocal() as verify:
        resident = await verify.get(StrategicResident, project.id)
        assert resident is not None
        assert resident.handled_message_sequence == 0
        assert resident.active_turn_task_id is not None
        turn = await verify.get(Task, resident.active_turn_task_id)
        assert turn is not None
        assert turn.first_payload["message_sequence_start"] == 1
        assert turn.first_payload["message_sequence_end"] == 1


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="ownership-transition serialization requires PostgreSQL row locks",
)
@pytest.mark.asyncio
async def test_concurrent_cutovers_serialize_to_one_generation(db_session):
    project = Project(
        name=f"Concurrent ownership {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await initialize_strategic_resident(db_session, project.id)
    await db_session.commit()

    first, second = await asyncio.wait_for(
        asyncio.gather(
            cutover_to_strategic_resident(TestingSessionLocal, project.id),
            cutover_to_strategic_resident(TestingSessionLocal, project.id),
        ),
        timeout=8,
    )

    assert {first.already_owned, second.already_owned} == {False, True}
    assert first.new_generation == second.new_generation == 2
    async with TestingSessionLocal() as verify:
        resident = await verify.get(StrategicResident, project.id)
        assert resident is not None
        assert resident.engine_owner == "strategic_resident"
        assert resident.authority_generation == 2
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string()
                    == STRATEGIC_RESIDENT_TURN_TASK_TYPE,
                    Task.status.not_in(
                        [
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.CANCELLED,
                        ]
                    ),
                )
            )
            == 0
        )
