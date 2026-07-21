"""Focused tests for M2a database-only Resident operation fencing."""

from __future__ import annotations

import hashlib
import inspect
import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models import (
    ArtifactRef,
    Project,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.artifact_refs import RESIDENT_ARTIFACT_REF_GRANTS_KEY
from app.strategic_operation_authority import (
    AuthorityBoundaryMutation,
    AuthorityGenerationMismatch,
    ExpectedRevisionMismatch,
    FencedOperationRequest,
    FencedOperationUsageError,
    InactiveTurnAuthority,
    OperationIdentityCollision,
    OperationPayloadTooLarge,
    OperationResultTooLarge,
    MutationPlanTooLarge,
    ResidentEngineOwnerMismatch,
    UnsupportedRevisionKey,
    execute_fenced_db_operation,
)
from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY
from app.strategic_resident_managed_task import managed_task_create_callback
from testing._resident_seed import seed_without_authority_enforcement

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


@pytest_asyncio.fixture()
async def authority_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _authority_rows(
    session: AsyncSession,
    *,
    generation: int = 3,
    owner: str = "strategic_resident",
    mutation_authorized: bool = True,
    task_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> tuple[Project, Task, StrategicResident]:
    project = Project(
        name="Authority",
        user_id="operator",
        vision={"revision": 4},
        problem_model={"revision": 7},
        design_doc={"revision": 2},
        running_update="steady state",
    )
    session.add(project)
    await session.commit()
    task = Task(
        project_id=project.id,
        status=task_status,
        payloads=[
            {
                "type": "strategic_resident_turn",
                "prompt": "bounded turn",
                "resident_id": str(project.id),
                "authority_generation": generation,
                "engine_owner": owner,
                "shadow_only": False,
            }
        ],
        context={
            "_resident_mutation_authorized": mutation_authorized,
            "_resident_input_frozen": True,
            "_resident_project_id": str(project.id),
            "_resident_authority_generation": generation,
            "_resident_engine_owner_observed": owner,
            "_resident_shadow_only": False,
        },
    )
    session.add(task)
    await session.commit()
    # The checkpoint/resident pair forms a circular non-deferrable FK on the real
    # migrated schema and the resident/checkpoint fences reject seeding this
    # already-authoritative starting state directly. Relax enforcement for the
    # seed ONLY (no-op on SQLite); the tested action below runs with fences live.
    # The helper's replica-mode GUC is connection-scoped and does not survive an
    # intermediate commit (NullPool), so build all three rows and commit ONCE.
    # StrategicResidentCheckpoint.id is a client-side uuid4, so the pointer and
    # the task-payload lineage are known before the flush.
    checkpoint_body = {"schema_version": 1}
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=1,
        content_hash=hashlib.sha256(b'{"schema_version":1}').hexdigest(),
        checkpoint=checkpoint_body,
        authority_generation=generation,
        source_turn_task_id=None,
    )
    turn_payload = dict(task.first_payload)
    turn_payload.update(
        {
            "checkpoint_id": str(checkpoint.id),
            "checkpoint_version": checkpoint.version,
            "checkpoint_hash": checkpoint.content_hash,
        }
    )
    task.payloads = [turn_payload]
    resident = StrategicResident(
        project_id=project.id,
        engine_owner=owner,
        engine_version="resident-v2",
        authority_generation=generation,
        active_turn_task_id=task.id,
        active_turn_lane="interactive",
        current_checkpoint_id=checkpoint.id,
    )
    async with seed_without_authority_enforcement(session):
        session.add_all([checkpoint, task, resident])
        await session.commit()
    await session.refresh(task)
    return project, task, resident


def _request(
    project: Project,
    task: Task,
    *,
    generation: int = 3,
    operation_id: str = "op-1",
    payload: dict | None = None,
    expected: dict | None = None,
    include_required: bool = True,
) -> FencedOperationRequest:
    turn_payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    expected_revisions = (
        {
            "authority_generation": generation,
            "active_turn_task_version": int(task.version),
            "current_checkpoint_id": turn_payload.get("checkpoint_id"),
            "resident_checkpoint_version": turn_payload.get("checkpoint_version"),
            "resident_checkpoint_hash": turn_payload.get("checkpoint_hash"),
        }
        if include_required
        else {}
    )
    if expected is not None:
        expected_revisions.update(expected)
    return FencedOperationRequest(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=generation,
        operation_id=operation_id,
        operation_kind="update_working_strategy",
        capability="project.strategy.update",
        risk="medium",
        expected_revisions=expected_revisions,
        request_payload=payload or {"summary": "advance"},
    )


@pytest.mark.asyncio
async def test_valid_mutation_and_receipt_commit_atomically(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    expected_running = hashlib.sha256(b"steady state").hexdigest()

    def mutate(context):
        context.writer.set_running_update("advanced state")
        context.writer.set_vision({"revision": 5})
        return {
            "facts": [{"kind": "revision", "value": 5}],
            "operations": [{"kind": "strategy_update"}],
            "proposed_memory": [{"note": "proposal only"}],
        }

    receipt = await execute_fenced_db_operation(
        authority_db,
        _request(
            project,
            task,
            expected={
                "authority_generation": 3,
                "vision_revision": 4,
                "running_update_revision": expected_running,
            },
        ),
        mutate,
    )

    assert receipt.status == "applied"
    assert receipt.operation_kind == "update_working_strategy"
    assert receipt.capability == "project.strategy.update"
    assert receipt.risk_level == "medium"
    assert receipt.observed_revisions_before["vision_revision"] == 4
    assert receipt.observed_revisions_after["vision_revision"] == 5
    assert (
        receipt.receipt["structured_result"]["proposed_memory"][0]["note"]
        == "proposal only"
    )
    await authority_db.refresh(project)
    assert project.running_update == "advanced state"


@pytest.mark.asyncio
async def test_operation_context_hash_is_derived_and_persisted(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    from app.strategic_resident_context import reconstruct_authoritative_turn_input

    provider_input = await reconstruct_authoritative_turn_input(
        authority_db,
        project_id=project.id,
        turn_task_id=task.id,
    )
    context_hash = provider_input["lineage"]["context_hash"]
    request = _request(
        project,
        task,
        operation_id="inspect-context-bound",
        expected={"context_hash": context_hash},
    )
    request = FencedOperationRequest(
        **{
            **request.__dict__,
            "operation_kind": "inspect_ci",
            "capability": "ci.run.inspect",
        }
    )

    receipt = await execute_fenced_db_operation(
        authority_db,
        request,
        lambda _context: {
            "evidence": [
                {
                    "kind": "ci_run",
                    "source_id": "run-context-bound",
                    "revision": "sha-context-bound",
                    "checksum": "a" * 64,
                }
            ]
        },
    )
    assert receipt.expected_revisions["context_hash"] == context_hash
    assert receipt.observed_revisions_before["context_hash"] == context_hash
    assert receipt.receipt["structured_result"]["evidence"][0]["source_id"] == (
        "run-context-bound"
    )

    stale_request = _request(
        project,
        task,
        operation_id="inspect-stale-context",
        expected={"context_hash": "e" * 64},
    )
    with pytest.raises(ExpectedRevisionMismatch, match="context_hash"):
        await execute_fenced_db_operation(
            authority_db,
            stale_request,
            lambda _context: {},
        )


@pytest.mark.asyncio
async def test_stale_generation_cannot_mutate(authority_db):
    project, task, _resident = await _authority_rows(authority_db, generation=4)
    called = False

    def mutate(_context):
        nonlocal called
        called = True
        return {}

    with pytest.raises(AuthorityGenerationMismatch):
        await execute_fenced_db_operation(
            authority_db, _request(project, task, generation=3), mutate
        )

    assert called is False
    assert (await authority_db.scalar(select(StrategicOperationReceipt))) is None


@pytest.mark.asyncio
async def test_legacy_engine_owner_cannot_execute_mutation(authority_db):
    project, task, _resident = await _authority_rows(authority_db, owner="legacy")
    with pytest.raises(ResidentEngineOwnerMismatch):
        await execute_fenced_db_operation(
            authority_db, _request(project, task), lambda _ctx: {}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation_authorized", "task_status"),
    [
        (False, TaskStatus.IN_PROGRESS),
        (True, TaskStatus.QUEUED),
    ],
)
async def test_inactive_or_shadow_turn_cannot_mutate(
    authority_db, mutation_authorized, task_status
):
    project, task, _resident = await _authority_rows(
        authority_db,
        mutation_authorized=mutation_authorized,
        task_status=task_status,
    )
    with pytest.raises(InactiveTurnAuthority):
        await execute_fenced_db_operation(
            authority_db, _request(project, task), lambda _ctx: {}
        )


@pytest.mark.asyncio
async def test_wrong_and_cross_project_tasks_fail_closed(authority_db):
    project, active_task, resident = await _authority_rows(authority_db)
    sibling = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_resident_turn"}],
        context={"_resident_mutation_authorized": True},
    )
    other_project = Project(name="Other", user_id="operator")
    authority_db.add_all([sibling, other_project])
    await authority_db.commit()
    cross_project = Task(
        project_id=other_project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_resident_turn"}],
        context={"_resident_mutation_authorized": True},
    )
    authority_db.add(cross_project)
    await authority_db.commit()

    with pytest.raises(InactiveTurnAuthority):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, sibling, operation_id="wrong-active"),
            lambda _ctx: {},
        )

    resident.active_turn_task_id = cross_project.id
    authority_db.add(resident)
    await authority_db.commit()
    with pytest.raises(InactiveTurnAuthority):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, cross_project, operation_id="cross-project"),
            lambda _ctx: {},
        )

    resident.active_turn_task_id = active_task.id
    authority_db.add(resident)
    await authority_db.commit()


@pytest.mark.asyncio
async def test_expected_revision_mismatch_and_missing_artifact_resolver_fail_closed(
    authority_db,
):
    project, task, _resident = await _authority_rows(authority_db)
    with pytest.raises(ExpectedRevisionMismatch):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, expected={"vision_revision": 99}),
            lambda _ctx: {},
        )
    with pytest.raises(UnsupportedRevisionKey):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="artifact-no-resolver",
                expected={f"artifact:{uuid.uuid4()}": "rev-1"},
            ),
            lambda _ctx: {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_key",
    [
        None,
        "authority_generation",
        "active_turn_task_version",
        "current_checkpoint_id",
        "resident_checkpoint_version",
        "resident_checkpoint_hash",
    ],
)
async def test_required_authority_revisions_fail_closed_before_callback(
    authority_db,
    missing_key,
):
    project, task, _resident = await _authority_rows(authority_db)
    expected = {
        "authority_generation": 3,
        "active_turn_task_version": int(task.version),
        "current_checkpoint_id": task.first_payload["checkpoint_id"],
        "resident_checkpoint_version": task.first_payload["checkpoint_version"],
        "resident_checkpoint_hash": task.first_payload["checkpoint_hash"],
    }
    if missing_key is None:
        expected = {}
    else:
        expected.pop(missing_key)
    called = False

    def mutate(_context):
        nonlocal called
        called = True
        return {}

    with pytest.raises(ExpectedRevisionMismatch, match="missing required"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id=f"missing-required-{missing_key}",
                expected=expected,
                include_required=False,
            ),
            mutate,
        )
    assert called is False
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "revision_key"),
    [
        ("running_update", "running_update_revision"),
        ("vision", "vision_revision"),
        ("problem_model", "problem_model_revision"),
        ("design_doc", "design_doc_revision"),
    ],
)
async def test_each_mutated_field_requires_its_matching_expected_revision(
    authority_db,
    field,
    revision_key,
):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        if field == "running_update":
            context.writer.set_running_update("changed")
        else:
            getattr(context.writer, f"set_{field}")({"revision": 99})
        return {}

    with pytest.raises(ExpectedRevisionMismatch, match="mutation plan"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id=f"missing-field-revision-{field}",
            ),
            mutate,
        )
    stored = await authority_db.get(Project, project.id)
    assert stored is not None
    assert stored.running_update == "steady state"
    assert stored.vision == {"revision": 4}
    assert stored.problem_model == {"revision": 7}
    assert stored.design_doc == {"revision": 2}
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [True, {"revision": 1}, [1], "x" * 513])
async def test_expected_revision_values_are_scalar_and_bounded(authority_db, bad_value):
    project, task, _resident = await _authority_rows(authority_db)
    with pytest.raises(FencedOperationUsageError):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                expected={"vision_revision": bad_value},
            ),
            lambda _ctx: {},
        )


@pytest.mark.asyncio
async def test_checkpoint_revision_is_loaded_from_locked_resident_pointer(authority_db):
    project, task, resident = await _authority_rows(authority_db)
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=8,
        content_hash="a" * 64,
        checkpoint={},
        authority_generation=3,
        source_turn_task_id=task.id,
    )
    authority_db.add(checkpoint)
    await authority_db.commit()
    resident.current_checkpoint_id = checkpoint.id
    task_payload = dict(task.first_payload)
    task_payload.update(
        {
            "checkpoint_id": str(checkpoint.id),
            "checkpoint_version": 8,
            "checkpoint_hash": checkpoint.content_hash,
        }
    )
    task.payloads = [task_payload]
    authority_db.add(task)
    authority_db.add(resident)
    await authority_db.commit()
    await authority_db.refresh(task)
    receipt = await execute_fenced_db_operation(
        authority_db,
        _request(
            project,
            task,
            expected={
                "current_checkpoint_id": str(checkpoint.id),
                "resident_checkpoint_version": 8,
                "resident_checkpoint_hash": checkpoint.content_hash,
            },
        ),
        lambda _ctx: {},
    )
    assert receipt.observed_revisions_before["resident_checkpoint_version"] == 8
    assert receipt.observed_revisions_before["resident_checkpoint_hash"] == "a" * 64


@pytest.mark.asyncio
async def test_mutation_authority_requires_a_current_checkpoint(authority_db):
    project, task, resident = await _authority_rows(authority_db)
    resident.current_checkpoint_id = None
    authority_db.add(resident)
    await authority_db.commit()

    with pytest.raises(InactiveTurnAuthority, match="no current checkpoint"):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, operation_id="missing-current-checkpoint"),
            lambda _context: {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "value"),
    [
        ("payload_generation", 4),
        ("context_generation", 4),
        ("payload_project", "00000000-0000-0000-0000-000000000000"),
        ("context_project", "00000000-0000-0000-0000-000000000000"),
        ("payload_owner", "legacy"),
        ("context_owner", "legacy"),
        ("input_frozen", False),
    ],
)
async def test_frozen_turn_lineage_must_match_resident(
    authority_db,
    surface,
    value,
):
    project, task, _resident = await _authority_rows(authority_db)
    payload = dict(task.first_payload)
    context = dict(task.context or {})
    if surface == "payload_generation":
        payload["authority_generation"] = value
    elif surface == "context_generation":
        context["_resident_authority_generation"] = value
    elif surface == "payload_project":
        payload["resident_id"] = value
    elif surface == "context_project":
        context["_resident_project_id"] = value
    elif surface == "payload_owner":
        payload["engine_owner"] = value
    elif surface == "context_owner":
        context["_resident_engine_owner_observed"] = value
    else:
        context["_resident_input_frozen"] = value
    task.payloads = [payload]
    task.context = context
    authority_db.add(task)
    await authority_db.commit()
    await authority_db.refresh(task)

    with pytest.raises(InactiveTurnAuthority):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id=f"stale-lineage-{surface}",
            ),
            lambda _context: {},
        )


@pytest.mark.asyncio
async def test_stale_task_and_checkpoint_revisions_fail_closed(authority_db):
    project, task, resident = await _authority_rows(authority_db)
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=8,
        content_hash="b" * 64,
        checkpoint={},
        authority_generation=3,
        source_turn_task_id=task.id,
    )
    authority_db.add(checkpoint)
    await authority_db.commit()
    resident.current_checkpoint_id = checkpoint.id
    payload = dict(task.first_payload)
    payload.update(
        {
            "checkpoint_id": str(checkpoint.id),
            "checkpoint_version": checkpoint.version,
            "checkpoint_hash": checkpoint.content_hash,
        }
    )
    task.payloads = [payload]
    authority_db.add_all([resident, task])
    await authority_db.commit()
    await authority_db.refresh(task)

    base = {
        "current_checkpoint_id": str(checkpoint.id),
        "resident_checkpoint_version": checkpoint.version,
        "resident_checkpoint_hash": checkpoint.content_hash,
    }
    with pytest.raises(ExpectedRevisionMismatch):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="stale-task-version",
                expected={
                    **base,
                    "active_turn_task_version": int(task.version) - 1,
                },
            ),
            lambda _context: {},
        )
    with pytest.raises(ExpectedRevisionMismatch):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="stale-checkpoint-version",
                expected={
                    **base,
                    "resident_checkpoint_version": checkpoint.version - 1,
                },
            ),
            lambda _context: {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint_version", "checkpoint_hash"),
    [(7, "b" * 64), (8, "c" * 64)],
)
async def test_frozen_task_checkpoint_lineage_must_match_current_checkpoint(
    authority_db,
    checkpoint_version,
    checkpoint_hash,
):
    project, task, resident = await _authority_rows(authority_db)
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=8,
        content_hash="b" * 64,
        checkpoint={},
        authority_generation=3,
        source_turn_task_id=task.id,
    )
    authority_db.add(checkpoint)
    await authority_db.commit()
    resident.current_checkpoint_id = checkpoint.id
    payload = dict(task.first_payload)
    payload.update(
        {
            "checkpoint_id": str(checkpoint.id),
            "checkpoint_version": checkpoint_version,
            "checkpoint_hash": checkpoint_hash,
        }
    )
    task.payloads = [payload]
    authority_db.add_all([resident, task])
    await authority_db.commit()
    await authority_db.refresh(task)

    with pytest.raises(InactiveTurnAuthority, match="checkpoint lineage"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id=f"stale-checkpoint-lineage-{checkpoint_version}-{checkpoint_hash[0]}",
                expected={
                    "current_checkpoint_id": str(checkpoint.id),
                    "resident_checkpoint_version": checkpoint.version,
                    "resident_checkpoint_hash": checkpoint.content_hash,
                },
            ),
            lambda _context: {},
        )


@pytest.mark.asyncio
async def test_replay_does_not_rerun_callback_and_collision_is_rejected(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    calls = 0

    def mutate(_context):
        nonlocal calls
        calls += 1
        return {"facts": [{"call": calls}]}

    request = _request(project, task)
    first = await execute_fenced_db_operation(authority_db, request, mutate)
    replay = await execute_fenced_db_operation(authority_db, request, mutate)
    assert replay.id == first.id
    assert calls == 1

    with pytest.raises(OperationIdentityCollision):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, payload={"summary": "different"}),
            mutate,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_terminal_replay_uses_receipt_only_fast_path(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    calls = 0

    def mutate(_context):
        nonlocal calls
        calls += 1
        return {}

    request = _request(project, task, operation_id="fast-replay")
    first = await execute_fenced_db_operation(authority_db, request, mutate)
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(
        authority_db.bind.sync_engine,
        "before_cursor_execute",
        capture_statement,
    )
    try:
        replay = await execute_fenced_db_operation(authority_db, request, mutate)
    finally:
        event.remove(
            authority_db.bind.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert replay.id == first.id
    assert calls == 1
    assert len(selects) == 1
    assert "strategicoperationreceipt" in selects[0].lower()


@pytest.mark.asyncio
async def test_callback_receives_immutable_bounded_request_payload(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    payload = {"intent": {"steps": ["inspect", {"revision": 4}]}}

    def mutate(context):
        assert context.request_payload["intent"]["steps"][0] == "inspect"
        with pytest.raises(TypeError):
            context.request_payload["intent"] = {}
        with pytest.raises(TypeError):
            context.request_payload["intent"]["steps"][1]["revision"] = 5
        assert isinstance(context.request_payload["intent"]["steps"], tuple)
        return {}

    await execute_fenced_db_operation(
        authority_db,
        _request(
            project,
            task,
            operation_id="immutable-request-payload",
            payload=payload,
        ),
        mutate,
    )


@pytest.mark.asyncio
async def test_request_payload_must_be_strict_bounded_json(authority_db):
    project, task, _resident = await _authority_rows(authority_db)

    invalid_values = [
        {"object": object()},
        {"not_finite": float("nan")},
        {"not_finite": float("inf")},
        {1: "non-string key"},
    ]
    for index, payload in enumerate(invalid_values):
        with pytest.raises(FencedOperationUsageError):
            await execute_fenced_db_operation(
                authority_db,
                _request(
                    project,
                    task,
                    operation_id=f"invalid-request-{index}",
                    payload=payload,
                ),
                lambda _context: {},
            )

    cycle: dict = {}
    cycle["self"] = cycle
    with pytest.raises(OperationPayloadTooLarge, match="reference cycle"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="cyclic-request",
                payload=cycle,
            ),
            lambda _context: {},
        )

    too_deep: dict = {}
    cursor = too_deep
    for _ in range(11):
        child: dict = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(OperationPayloadTooLarge, match="depth limit"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="deep-request",
                payload=too_deep,
            ),
            lambda _context: {},
        )

    too_many_nodes = {"items": list(range(5_001))}
    with pytest.raises(OperationPayloadTooLarge, match="node budget"):
        await execute_fenced_db_operation(
            authority_db,
            _request(
                project,
                task,
                operation_id="wide-request",
                payload=too_many_nodes,
            ),
            lambda _context: {},
        )

    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
async def test_callback_failure_rolls_back_mutation_and_receipt(authority_db):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        context.writer.set_running_update("must roll back")
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await execute_fenced_db_operation(authority_db, _request(project, task), mutate)

    stored_project = await authority_db.get(Project, project.id)
    assert stored_project is not None
    assert stored_project.running_update == "steady state"
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
async def test_callback_context_exposes_no_ambient_authority(authority_db):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        assert not hasattr(context, "session")
        assert not hasattr(context, "resident")
        assert not hasattr(context, "turn_task")
        assert not hasattr(context, "receipt")
        writer_state = vars(context.writer)
        assert not any(
            hasattr(value, "_sa_instance_state") for value in writer_state.values()
        )
        # A raw transaction boundary cannot even be addressed: the private
        # fenced session/connection is not exposed to the callback.
        context.session.execute("COMMIT")
        return {}  # pragma: no cover

    with pytest.raises(AttributeError):
        await execute_fenced_db_operation(authority_db, _request(project, task), mutate)
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
async def test_observed_revision_snapshot_is_immutable(authority_db):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        context.observed_revisions["vision_revision"] = 999
        return {}

    with pytest.raises(TypeError):
        await execute_fenced_db_operation(authority_db, _request(project, task), mutate)
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
async def test_unbounded_mutation_plan_is_rejected_before_apply(authority_db):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        context.writer.set_vision({"huge": "x" * 100_001})
        return {}

    with pytest.raises(MutationPlanTooLarge):
        await execute_fenced_db_operation(authority_db, _request(project, task), mutate)
    stored = await authority_db.get(Project, project.id)
    assert stored is not None
    assert stored.vision == {"revision": 4}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered",
    [
        {"authority_generation": 99},
        {"vision": {"huge": "x" * 100_001}},
        {
            "vision": {"near_limit": "x" * 99_980},
            "problem_model": {"near_limit": "y" * 99_980},
        },
    ],
)
async def test_private_mutation_plan_tampering_is_revalidated(authority_db, tampered):
    project, task, _resident = await _authority_rows(authority_db)

    def mutate(context):
        context.writer._FencedMutationWriter__changes.update(tampered)
        return {}

    with pytest.raises((FencedOperationUsageError, MutationPlanTooLarge)):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, operation_id=f"tamper-{uuid.uuid4()}"),
            mutate,
        )
    stored = await authority_db.get(Project, project.id)
    assert stored is not None
    assert stored.vision == {"revision": 4}


@pytest.mark.asyncio
async def test_oversized_result_rolls_back(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    huge = [{f"k{i}": "x" * 4_000 for i in range(30)} for _ in range(50)]

    with pytest.raises(OperationResultTooLarge):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task),
            lambda _ctx: {"facts": huge},
        )
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


@pytest.mark.asyncio
async def test_result_cycle_and_global_node_budget_fail_closed(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    cycle: dict = {}
    cycle["self"] = cycle
    with pytest.raises(OperationResultTooLarge, match="reference cycle"):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, operation_id="cyclic-result"),
            lambda _ctx: {"facts": [cycle]},
        )

    records = [{f"k{i}": i for i in range(30)} for _ in range(50)]
    with pytest.raises(OperationResultTooLarge, match="node budget"):
        await execute_fenced_db_operation(
            authority_db,
            _request(project, task, operation_id="global-node-result"),
            lambda _ctx: {
                "facts": records,
                "evidence": records,
                "operations": records,
                "proposed_memory": records,
                "required_attention": records,
            },
        )


@pytest.mark.asyncio
async def test_async_callback_is_rejected_before_it_can_run(authority_db):
    project, task, _resident = await _authority_rows(authority_db)
    ran = False

    async def mutate(_context):
        nonlocal ran
        ran = True
        await authority_db.commit()
        return {}

    with pytest.raises(FencedOperationUsageError, match="must be synchronous"):
        await execute_fenced_db_operation(authority_db, _request(project, task), mutate)
    assert ran is False
    assert await authority_db.scalar(select(StrategicOperationReceipt)) is None


def test_lock_order_is_project_resident_task_receipt():
    source = inspect.getsource(execute_fenced_db_operation)
    project_at = source.index("select(Project)")
    resident_at = source.index("select(StrategicResident)")
    task_at = source.index("select(Task)")
    receipt_at = source.index("select(StrategicOperationReceipt)")
    assert project_at < resident_at < task_at < receipt_at
    resident_lock_at = source.index(".with_for_update()", resident_at)
    generation_check_at = source.index("observed_generation =", receipt_at)
    callback_at = source.index("raw_result = mutate(context)", generation_check_at)
    # A concurrent generation bump must wait for the Resident row lock; the
    # locked generation is checked before the callback can mutate anything.
    assert resident_lock_at < generation_check_at < callback_at


@pytest.mark.skipif(
    not USE_POSTGRES, reason="same-operation row serialization requires PostgreSQL"
)
@pytest.mark.asyncio
async def test_concurrent_same_operation_executes_callback_once(db_session):
    project, task, _resident = await _authority_rows(db_session)
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    request = _request(project, task, operation_id=f"concurrent-{uuid.uuid4()}")
    start = asyncio.Event()
    callback_count = 0

    def mutate(_context):
        nonlocal callback_count
        callback_count += 1
        return {"facts": [{"callback_count": callback_count}]}

    async def first_call():
        await start.wait()
        async with Session() as session:
            return await execute_fenced_db_operation(session, request, mutate)

    async def second_call():
        await start.wait()
        async with Session() as session:
            return await execute_fenced_db_operation(session, request, mutate)

    first_task = asyncio.create_task(first_call())
    second_task = asyncio.create_task(second_call())
    start.set()
    first, second = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=15,
    )
    assert first.id == second.id
    assert callback_count == 1


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="same-operation collision serialization requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_concurrent_same_operation_different_hash_collides(db_session):
    project, task, _resident = await _authority_rows(db_session)
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    operation_id = f"concurrent-collision-{uuid.uuid4()}"
    first_request = _request(
        project, task, operation_id=operation_id, payload={"value": "first"}
    )
    second_request = _request(
        project, task, operation_id=operation_id, payload={"value": "second"}
    )
    start = asyncio.Event()
    callback_count = 0

    def mutate(_context):
        nonlocal callback_count
        callback_count += 1
        return {}

    async def call(request):
        await start.wait()
        async with Session() as session:
            return await execute_fenced_db_operation(session, request, mutate)

    first_task = asyncio.create_task(call(first_request))
    second_task = asyncio.create_task(call(second_request))
    start.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first_task, second_task, return_exceptions=True),
        timeout=15,
    )
    assert sum(isinstance(item, StrategicOperationReceipt) for item in outcomes) == 1
    assert sum(isinstance(item, OperationIdentityCollision) for item in outcomes) == 1
    assert callback_count == 1


@pytest.mark.skipif(
    not USE_POSTGRES, reason="authority-generation race requires PostgreSQL"
)
@pytest.mark.asyncio
async def test_generation_increment_race_fences_stale_worker(db_session):
    project, task, _resident = await _authority_rows(db_session)
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    bump_holds_authority = asyncio.Event()
    stale_call_started = asyncio.Event()
    callback_called = False

    async def bump_generation():
        async with Session() as session, session.begin():
            await session.execute(
                select(Project)
                .where(Project.id == project.id)
                .with_for_update(key_share=True)
            )
            await session.execute(
                update(StrategicResident)
                .where(StrategicResident.project_id == project.id)
                .values(authority_generation=4)
            )
            bump_holds_authority.set()
            await asyncio.wait_for(stale_call_started.wait(), timeout=5)

    async def stale_operation():
        nonlocal callback_called
        await bump_holds_authority.wait()
        stale_call_started.set()

        def mutate(_context):
            nonlocal callback_called
            callback_called = True
            return {}

        async with Session() as session:
            with pytest.raises(AuthorityGenerationMismatch):
                await execute_fenced_db_operation(
                    session,
                    _request(
                        project,
                        task,
                        operation_id=f"stale-race-{uuid.uuid4()}",
                        generation=3,
                    ),
                    mutate,
                )

    await asyncio.wait_for(
        asyncio.gather(bump_generation(), stale_operation()),
        timeout=15,
    )
    assert callback_called is False


@pytest.mark.asyncio
async def test_mutation_endpoint_applies_running_update_end_to_end(
    authority_db,
):
    """The /mutate endpoint drives a real fenced DB-only mutation to `applied`."""
    from datetime import datetime, timezone

    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_capability_invocation import (
        CapabilityEvidenceRef,
        ResidentCapabilityInvocation,
    )
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY

    project, task, _resident = await _authority_rows(authority_db)
    context_hash = "ctx-running-update-e2e"
    task.context = {**dict(task.context or {}), TURN_CONTEXT_HASH_KEY: context_hash}
    authority_db.add(task)
    await authority_db.commit()
    await authority_db.refresh(task)
    running_update_revision = hashlib.sha256(b"steady state").hexdigest()
    invocation = ResidentCapabilityInvocation.model_validate(
        {
            "capability_id": "strategy.running_update.set",
            "project_id": project.id,
            "turn_task_id": task.id,
            "authority_generation": 3,
            "parameters": {"running_update": "advanced via endpoint"},
            "operation_id": "endpoint-op-1",
            "expected_revisions": {
                "authority_generation": 3,
                "active_turn_task_version": int(task.version),
                "current_checkpoint_id": task.first_payload.get("checkpoint_id"),
                "resident_checkpoint_version": task.first_payload.get(
                    "checkpoint_version"
                ),
                "resident_checkpoint_hash": task.first_payload.get("checkpoint_hash"),
                "running_update_revision": running_update_revision,
            },
            "evidence": (
                CapabilityEvidenceRef(
                    kind="task",
                    project_id=project.id,
                    source_id=str(task.id),
                    revision="task:v1",
                    checksum="d" * 64,
                    inspected_at=datetime.now(timezone.utc),
                ),
            ),
        }
    )
    authority_db.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=3,
            operation_id="inspect-running-update-task",
            operation_kind="inspect_task",
            capability="task.inspect",
            risk_level="low",
            request_hash="a" * 64,
            expected_revisions={"context_hash": context_hash},
            status="applied",
            receipt={
                "schema_version": 1,
                "structured_result": {
                    "evidence": [
                        {
                            "kind": ref.kind,
                            "source_id": ref.source_id,
                            "revision": ref.revision,
                            "checksum": ref.checksum,
                        }
                        for ref in invocation.evidence
                    ]
                },
            },
        )
    )
    await authority_db.commit()
    token = {
        "sub": str(task.id),
        "project_id": str(project.id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }

    result = await capability_router.invoke_resident_mutation(
        None, invocation, session=authority_db, token=token
    )
    assert result["status"] == "applied"
    assert result["capability_id"] == "strategy.running_update.set"

    await authority_db.refresh(project)
    assert project.running_update == "advanced via endpoint"

    receipt = (
        await authority_db.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.operation_id == "endpoint-op-1"
            )
        )
    ).scalar_one()
    assert receipt.operation_kind == "set_running_update"
    assert receipt.capability == "strategy.running_update.set"
    assert receipt.risk_level == "low"
    assert receipt.status == "applied"

    # Idempotent replay of the same operation_id returns the same applied receipt
    # without changing the (now-advanced) field.
    replay = await capability_router.invoke_resident_mutation(
        None, invocation, session=authority_db, token=token
    )
    assert replay["operation_id"] == "endpoint-op-1"
    assert replay["status"] == "applied"
    all_receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert len(all_receipts) == 2


@pytest.mark.asyncio
async def test_inspection_endpoint_writes_citable_receipt(authority_db, monkeypatch):
    """A /capability inspection persists a context-bound, idempotent receipt."""
    from unittest.mock import AsyncMock

    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY

    project, turn_task, _resident = await _authority_rows(authority_db)
    # Simulate the frozen context_hash persisted at context-serve time.
    turn_task.context = {**turn_task.context, TURN_CONTEXT_HASH_KEY: "ctx-hash-1"}
    target = Task(project_id=project.id, title="target", payloads=[{"type": "plan"}])
    authority_db.add_all([turn_task, target])
    await authority_db.commit()

    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        AsyncMock(return_value=turn_task),
    )
    inspector = AsyncMock(wraps=capability_router.inspect_project_task)
    monkeypatch.setattr(capability_router, "inspect_project_task", inspector)
    token = {
        "sub": str(turn_task.id),
        "project_id": str(project.id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }
    body = ResidentCapabilityRequest(
        capability_id="task.logs.inspect",
        parameters={"task_id": str(target.id)},
    )

    result = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    # Response evidence kind is mapped into the trust taxonomy (logs -> task_log).
    assert result["evidence"]["kind"] == "task_log"
    assert result["evidence"]["inspected_at"]
    assert "observed_at" not in result["evidence"]

    receipt = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert receipt.operation_kind == "inspect_task_log"
    assert receipt.capability == "task.logs.inspect"
    assert receipt.status == "applied"
    assert receipt.expected_revisions == {"context_hash": "ctx-hash-1"}
    assert receipt.receipt["structured_result"]["evidence"][0]["kind"] == "task_log"

    # Idempotent: the per-turn ledger dedups the same inspection -> one receipt.
    target.title = "changed after canonical inspection"
    authority_db.add(target)
    await authority_db.commit()
    replay = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert replay == result
    inspector.assert_awaited_once()
    receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert len(receipts) == 1


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="operation-receipt trigger enforcement requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_postgres_fence_accepts_context_bound_inspection_receipt(db_session):
    """The DB fence accepts the exact read-only receipt shape emitted in production."""

    from app.strategic_resident_inspection_receipt import build_inspection_receipt

    context_hash = "e" * 64
    _project, turn_task, _resident = await _authority_rows(db_session)
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: context_hash,
    }
    db_session.add(turn_task)
    await db_session.commit()
    await db_session.refresh(turn_task)

    receipt = build_inspection_receipt(
        capability_id="task.inspect",
        turn_task=turn_task,
        context_hash=context_hash,
        request_hash="a" * 64,
        evidence_json={
            "kind": "task",
            "source_id": str(turn_task.id),
            "revision": "1",
            "checksum": "b" * 64,
        },
    )
    assert receipt is not None
    assert receipt.expected_revisions == {"context_hash": context_hash}
    db_session.add(receipt)
    await db_session.commit()

    persisted = await db_session.scalar(
        select(StrategicOperationReceipt).where(
            StrategicOperationReceipt.id == receipt.id
        )
    )
    assert persisted is not None
    assert persisted.status == "applied"

    from app.strategic_resident_external_inspection import (
        ExternalInspectionPlan,
        _pending_receipt,
    )

    applying = _pending_receipt(
        task=turn_task,
        capability_id="pr.inspect",
        request_hash="c" * 64,
        plan=ExternalInspectionPlan(
            expected_revisions={
                "context_hash": context_hash,
                "pr_head_sha": "deadbeef",
                "_inspection_plan_hash": "f" * 64,
            },
            reservation={"owner": "BlueprintAIStudio", "repo": "Blueprint", "pr": 1},
            external_scope={"owner": "BlueprintAIStudio", "repo": "Blueprint"},
        ),
        attempt_id="external-attempt-1",
    )
    db_session.add(applying)
    await db_session.commit()
    await db_session.refresh(applying)
    assert applying.status == "applying"
    assert applying.unknown_effects == []


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="operation-receipt trigger enforcement requires PostgreSQL",
)
@pytest.mark.parametrize(
    "tamper",
    [
        "context_hash",
        "capability_pair",
        "risk_level",
        "status",
        "unknown_effects",
        "operation_id",
        "mutation_shape",
    ],
)
@pytest.mark.asyncio
async def test_postgres_inspection_fence_rejects_noncanonical_receipts(
    db_session, tamper
):
    """Only the exact server-authored read-only receipt shape bypasses mutation keys."""

    from sqlalchemy.exc import DBAPIError

    from app.strategic_resident_inspection_receipt import build_inspection_receipt

    context_hash = "e" * 64
    _project, turn_task, _resident = await _authority_rows(db_session)
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: context_hash,
    }
    db_session.add(turn_task)
    await db_session.commit()
    await db_session.refresh(turn_task)

    receipt = build_inspection_receipt(
        capability_id="task.inspect",
        turn_task=turn_task,
        context_hash=context_hash,
        request_hash="f" * 64,
        evidence_json={
            "kind": "task",
            "source_id": str(turn_task.id),
            "revision": "1",
            "checksum": "a" * 64,
        },
    )
    assert receipt is not None
    if tamper == "context_hash":
        receipt.expected_revisions = {"context_hash": "d" * 64}
    elif tamper == "capability_pair":
        receipt.operation_kind = "inspect_pr"
    elif tamper == "risk_level":
        receipt.risk_level = "medium"
    elif tamper == "status":
        receipt.status = "pending"
    elif tamper == "unknown_effects":
        receipt.unknown_effects = [{"effect": "not-read-only"}]
    elif tamper == "operation_id":
        receipt.operation_id = f"not-inspect:{turn_task.id}"
    elif tamper == "mutation_shape":
        receipt.capability = "strategy.running_update.set"
        receipt.operation_kind = "set_running_update"
    else:
        raise AssertionError(f"unknown tamper: {tamper}")

    db_session.add(receipt)
    with pytest.raises(DBAPIError) as exc_info:
        await db_session.commit()
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
    assert "Resident mutation authority is stale or invalid" in str(exc_info.value)
    await db_session.rollback()

def _github_pr_payload(*, owner: str, repo: str, pr_number: int) -> dict:
    return {
        "id": 991,
        "node_id": "PR_node_991",
        "number": pr_number,
        "state": "open",
        "title": "Bounded Resident inspection",
        "draft": False,
        "locked": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "octocat"},
        "assignees": [],
        "requested_reviewers": [{"login": "reviewer"}],
        "labels": [{"name": "resident"}],
        "head": {
            "ref": "feature/resident",
            "sha": "a" * 40,
            "repo": {"full_name": f"{owner}/{repo}"},
        },
        "base": {
            "ref": "automain",
            "sha": "b" * 40,
            "repo": {"full_name": f"{owner}/{repo}"},
        },
        "created_at": "2026-07-18T20:00:00Z",
        "updated_at": "2026-07-18T20:05:00Z",
        "closed_at": None,
        "merged_at": None,
        "commits": 3,
        "additions": 40,
        "deletions": 7,
        "changed_files": 4,
        "comments": 2,
        "review_comments": 1,
    }


def _pr_evidence_kwargs(kwargs: dict) -> dict:
    return {
        key: kwargs[key]
        for key in ("project_id", "owner", "repo", "pr_number")
    }


@pytest.mark.asyncio
async def test_pr_inspection_releases_locks_and_replays_canonical_receipt(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-inspect",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    async def external_inspection(**kwargs):
        assert not authority_db.in_transaction(), "GitHub I/O ran under a DB transaction"
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"],
                repo=kwargs["repo"],
                pr_number=kwargs["pr_number"],
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    inspector = AsyncMock(side_effect=external_inspection)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    body = ResidentCapabilityRequest(
        capability_id="pr.inspect", parameters={"pr_number": 17}
    )
    token = _turn_token(project, turn_task)

    result = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert result["evidence"]["kind"] == "pr"
    assert result["evidence"]["source_id"] == (
        "github-pr:BlueprintAIStudio/Blueprint#17"
    )
    assert result["evidence"]["data"]["pr_number"] == 17
    assert "observed_at" not in result["evidence"]
    assert result["evidence"]["inspected_at"]

    receipt = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert receipt.capability == "pr.inspect"
    assert receipt.operation_kind == "inspect_pr"
    expected_revisions = dict(receipt.expected_revisions)
    plan_hash = expected_revisions.pop("_inspection_plan_hash")
    assert len(plan_hash) == 64
    assert expected_revisions == {
        "context_hash": "ctx-pr-inspect",
        "repository": "BlueprintAIStudio/Blueprint",
        "pr_number": 17,
    }
    assert receipt.receipt["structured_result"]["evidence"] == [result["evidence"]]

    replay = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert replay == result
    inspector.assert_awaited_once()
    assert len(
        (await authority_db.execute(select(StrategicOperationReceipt))).scalars().all()
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_id", "inspector_name", "operation_kind", "evidence_kind"),
    [
        ("pr.diff.inspect", "inspect_project_pr_diff", "inspect_pr_diff", "pr_diff"),
        (
            "pr.checks.inspect",
            "inspect_project_pr_checks",
            "inspect_pr_checks",
            "pr_check",
        ),
    ],
)
async def test_bounded_pr_evidence_adapters_release_locks_and_replay(
    authority_db,
    monkeypatch,
    capability_id,
    inspector_name,
    operation_kind,
    evidence_kind,
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_checks_inspection import build_pr_checks_evidence
    from app.strategic_resident_pr_diff_inspection import (
        BoundedDiffCapture,
        PRDiffIdentity,
        build_pr_diff_evidence,
    )

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: f"ctx-{evidence_kind}",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()
    head_sha = "b" * 40

    async def external_inspection(**kwargs):
        assert not authority_db.in_transaction(), "GitHub I/O ran under DB locks"
        if evidence_kind == "pr_diff":
            return build_pr_diff_evidence(
                BoundedDiffCapture(
                    content=b"diff --git a/a.py b/a.py\n+bounded\n",
                    content_length=38,
                    stream_truncated=False,
                ),
                project_id=kwargs["project_id"],
                identity=PRDiffIdentity(
                    repository=f'{kwargs["owner"]}/{kwargs["repo"]}',
                    pr_number=kwargs["pr_number"],
                    base_ref="automain",
                    base_sha="a" * 40,
                    head_ref="feature/bounded",
                    head_sha=head_sha,
                ),
            )
        return build_pr_checks_evidence(
            [
                {
                    "id": 1,
                    "name": "unit",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "actions"},
                }
            ],
            project_id=kwargs["project_id"],
            owner=kwargs["owner"],
            repo=kwargs["repo"],
            pr_number=kwargs["pr_number"],
            head_sha=kwargs["head_sha"],
            reported_total=1,
            pages_fetched=1,
        )

    inspector = AsyncMock(side_effect=external_inspection)
    monkeypatch.setattr(capability_router, inspector_name, inspector)
    parameters = {"pr_number": 31}
    if capability_id == "pr.checks.inspect":
        parameters["head_sha"] = head_sha
    body = ResidentCapabilityRequest(
        capability_id=capability_id,
        parameters=parameters,
    )
    token = _turn_token(project, turn_task)

    result = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert result["evidence"]["kind"] == evidence_kind
    receipt = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert receipt.capability == capability_id
    assert receipt.operation_kind == operation_kind
    assert receipt.receipt["structured_result"]["evidence"] == [result["evidence"]]

    replay = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert replay == result
    inspector.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_inspection_cannot_settle_after_authority_changes(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-authority-change",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    async def revoke_then_return(**kwargs):
        assert not authority_db.in_transaction()
        resident.engine_owner = "legacy"
        authority_db.add(resident)
        await authority_db.commit()
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"],
                repo=kwargs["repo"],
                pr_number=kwargs["pr_number"],
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    inspector = AsyncMock(side_effect=revoke_then_return)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    with pytest.raises(HTTPException) as stale:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="pr.inspect", parameters={"pr_number": 18}
            ),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert stale.value.status_code == 409
    inspector.assert_awaited_once()
    reservation = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert reservation.status == "applying"
    reservation_body = reservation.receipt["reservation"]
    assert reservation_body["repository"] == "BlueprintAIStudio/Blueprint"
    assert reservation_body["pr_number"] == 18
    assert len(reservation_body["attempt_id"]) == 32


@pytest.mark.asyncio
async def test_pr_inspection_cannot_settle_after_project_repository_changes(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-repo-change",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    async def change_repo_then_return(**kwargs):
        assert not authority_db.in_transaction()
        project.github_repo = "DifferentRepo"
        authority_db.add(project)
        await authority_db.commit()
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"], repo=kwargs["repo"], pr_number=kwargs["pr_number"]
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    inspector = AsyncMock(side_effect=change_repo_then_return)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    # The repository-change rejection below correctly rolls back the session,
    # which EXPIRES every ORM instance (real expiry is active on the Postgres
    # shard). Building the turn token from ``project``/``turn_task`` AFTER that
    # rollback would lazy-load their expired PKs -> MissingGreenlet. The token
    # content is invariant across both calls, so snapshot it once here while the
    # instances are still populated.
    token = _turn_token(project, turn_task)
    with pytest.raises(HTTPException) as changed:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="pr.inspect", parameters={"pr_number": 21}
            ),
            session=authority_db,
            token=token,
        )
    assert changed.value.status_code == 409
    assert "repository changed" in str(changed.value.detail)
    reservation = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert reservation.status == "applying"

    async def return_rebound(**kwargs):
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"], repo=kwargs["repo"], pr_number=kwargs["pr_number"]
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    inspector.side_effect = return_rebound
    rebound = await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id="pr.inspect", parameters={"pr_number": 21}
        ),
        session=authority_db,
        token=token,
    )
    assert rebound["evidence"]["source_id"] == (
        "github-pr:BlueprintAIStudio/DifferentRepo#21"
    )
    receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert len(receipts) == 2
    assert {item.status for item in receipts} == {"applying", "applied"}


@pytest.mark.asyncio
async def test_pr_inspection_retries_pending_reservation_after_external_failure(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-retry",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    async def fail_then_succeed(**kwargs):
        assert not authority_db.in_transaction()
        if fail_then_succeed.calls == 0:
            fail_then_succeed.calls += 1
            raise HTTPException(status_code=503, detail="GitHub unavailable")
        if fail_then_succeed.calls == 1:
            fail_then_succeed.calls += 1

            class OversizedEvidence:
                def model_dump(self, *, mode: str) -> dict:
                    assert mode == "json"
                    return {
                        "kind": "pr",
                        "project_id": str(project.id),
                        "source_id": "github-pr:oversized",
                        "revision": "pr:oversized",
                        "checksum": "a" * 64,
                        "observed_at": "2026-07-18T20:00:00Z",
                        "data": {"payload": "x" * 6_000},
                    }

            return OversizedEvidence()
        fail_then_succeed.calls += 1
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"], repo=kwargs["repo"], pr_number=kwargs["pr_number"]
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    fail_then_succeed.calls = 0
    inspector = AsyncMock(side_effect=fail_then_succeed)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    body = ResidentCapabilityRequest(
        capability_id="pr.inspect", parameters={"pr_number": 19}
    )
    token = _turn_token(project, turn_task)

    with pytest.raises(HTTPException) as unavailable:
        await capability_router.invoke_resident_capability(
            None, body, session=authority_db, token=token
        )
    assert unavailable.value.status_code == 503
    pending = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert pending.status == "pending"

    with pytest.raises(HTTPException) as oversized:
        await capability_router.invoke_resident_capability(
            None, body, session=authority_db, token=token
        )
    assert oversized.value.status_code == 409
    assert "size limit" in str(oversized.value.detail)
    await authority_db.refresh(pending)
    assert pending.status == "pending"

    result = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert result["evidence"]["data"]["pr_number"] == 19
    await authority_db.refresh(pending)
    assert pending.status == "applied"
    assert inspector.await_count == 3


@pytest.mark.asyncio
async def test_pr_inspection_adopts_concurrently_settled_canonical_receipt(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_inspection_receipt import build_inspection_receipt
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-concurrent",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    canonical_payload = _github_pr_payload(
        owner="BlueprintAIStudio", repo="Blueprint", pr_number=20
    )
    canonical_payload["title"] = "canonical winner"
    late_payload = dict(canonical_payload)
    late_payload["title"] = "late competing response"
    body = ResidentCapabilityRequest(
        capability_id="pr.inspect", parameters={"pr_number": 20}
    )
    token = _turn_token(project, turn_task)

    # Leave a durable pending reservation first. The retry then loads that row
    # into its identity map before the competing settlement, exercising the
    # phase-three populate_existing refresh rather than a fresh-object shortcut.
    monkeypatch.setattr(
        capability_router,
        "inspect_project_pr",
        AsyncMock(side_effect=HTTPException(status_code=503, detail="retry")),
    )
    with pytest.raises(HTTPException):
        await capability_router.invoke_resident_capability(
            None, body, session=authority_db, token=token
        )

    async def settle_elsewhere(**kwargs):
        assert not authority_db.in_transaction()
        canonical = build_pr_evidence(canonical_payload, **_pr_evidence_kwargs(kwargs))
        async with AsyncSession(
            bind=authority_db.bind, expire_on_commit=False, autoflush=False
        ) as competing:
            reservation = (
                await competing.execute(select(StrategicOperationReceipt))
            ).scalar_one()
            applied = build_inspection_receipt(
                capability_id="pr.inspect",
                turn_task=turn_task,
                context_hash="ctx-pr-concurrent",
                request_hash=reservation.request_hash,
                evidence_json=canonical.model_dump(mode="json"),
            )
            assert applied is not None
            reservation.status = "applied"
            reservation.receipt = applied.receipt
            competing.add(reservation)
            await competing.commit()
        return build_pr_evidence(late_payload, **_pr_evidence_kwargs(kwargs))

    inspector = AsyncMock(side_effect=settle_elsewhere)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    result = await capability_router.invoke_resident_capability(
        None,
        body,
        session=authority_db,
        token=token,
    )
    assert result["evidence"]["data"]["title"] == "canonical winner"
    inspector.assert_awaited_once()
    receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].status == "applied"


@pytest.mark.asyncio
async def test_pr_inspection_lease_fences_stale_attempt_and_fresh_duplicate_io(
    authority_db, monkeypatch
):
    from datetime import timedelta

    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_external_inspection import (
        EXTERNAL_INSPECTION_LEASE_SECONDS,
        EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS,
    )
    from app.strategic_resident_pr_inspection import build_pr_evidence

    assert (
        EXTERNAL_INSPECTION_LEASE_SECONDS
        > EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS
    )

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-attempt-fence",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()
    body = ResidentCapabilityRequest(
        capability_id="pr.inspect", parameters={"pr_number": 22}
    )
    token = _turn_token(project, turn_task)
    a_started = asyncio.Event()
    release_a = asyncio.Event()
    b_started = asyncio.Event()
    release_b = asyncio.Event()
    calls = 0

    async def stalled_inspector(**kwargs):
        nonlocal calls
        calls += 1
        payload = _github_pr_payload(
            owner=kwargs["owner"], repo=kwargs["repo"], pr_number=kwargs["pr_number"]
        )
        if calls == 1:
            payload["title"] = "stale attempt A"
            a_started.set()
            await release_a.wait()
        else:
            payload["title"] = "renewed attempt B"
            b_started.set()
            await release_b.wait()
        return build_pr_evidence(payload, **_pr_evidence_kwargs(kwargs))

    inspector = AsyncMock(side_effect=stalled_inspector)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    attempt_a = asyncio.create_task(
        capability_router.invoke_resident_capability(
            None, body, session=authority_db, token=token
        )
    )
    await a_started.wait()

    # A fresh duplicate observes A's lease and performs zero external I/O.
    async with AsyncSession(
        bind=authority_db.bind, expire_on_commit=False, autoflush=False
    ) as duplicate_session:
        with pytest.raises(HTTPException) as in_progress:
            await capability_router.invoke_resident_capability(
                None, body, session=duplicate_session, token=token
            )
    assert in_progress.value.status_code == 409
    assert in_progress.value.headers and in_progress.value.headers["Retry-After"]
    assert inspector.await_count == 1

    # Even at the perform deadline, A still owns a safety-margin lease. A
    # duplicate therefore cannot reclaim while legal execution remains possible.
    async with AsyncSession(
        bind=authority_db.bind, expire_on_commit=False, autoflush=False
    ) as deadline_session:
        reservation = (
            await deadline_session.execute(select(StrategicOperationReceipt))
        ).scalar_one()
        reservation.updated_at = reservation.updated_at - timedelta(
            seconds=EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS
        )
        deadline_session.add(reservation)
        await deadline_session.commit()

    async with AsyncSession(
        bind=authority_db.bind, expire_on_commit=False, autoflush=False
    ) as duplicate_session:
        with pytest.raises(HTTPException) as still_in_progress:
            await capability_router.invoke_resident_capability(
                None, body, session=duplicate_session, token=token
            )
    assert still_in_progress.value.status_code == 409
    assert inspector.await_count == 1

    # Once the safety-margin lease itself expires, B may reclaim after a crash.
    async with AsyncSession(
        bind=authority_db.bind, expire_on_commit=False, autoflush=False
    ) as expiry_session:
        reservation = (
            await expiry_session.execute(select(StrategicOperationReceipt))
        ).scalar_one()
        reservation.updated_at = reservation.updated_at - timedelta(
            seconds=EXTERNAL_INSPECTION_LEASE_SECONDS + 1
        )
        expiry_session.add(reservation)
        await expiry_session.commit()

    async def invoke_b():
        async with AsyncSession(
            bind=authority_db.bind, expire_on_commit=False, autoflush=False
        ) as session_b:
            return await capability_router.invoke_resident_capability(
                None, body, session=session_b, token=token
            )

    attempt_b = asyncio.create_task(invoke_b())
    await b_started.wait()
    release_a.set()
    with pytest.raises(HTTPException) as superseded:
        await attempt_a
    assert superseded.value.status_code == 409
    assert "superseded" in str(superseded.value.detail)

    release_b.set()
    result_b = await attempt_b
    assert result_b["evidence"]["data"]["title"] == "renewed attempt B"
    assert inspector.await_count == 2


@pytest.mark.asyncio
async def test_pr_inspection_total_timeout_releases_reservation_for_retry(
    authority_db, monkeypatch
):
    from app import strategic_resident_external_inspection as external_inspection
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_pr_inspection import build_pr_evidence

    project, turn_task, _resident = await _authority_rows(authority_db)
    project.github_owner = "BlueprintAIStudio"
    project.github_repo = "Blueprint"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-pr-total-timeout",
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    authority_db.add_all([project, turn_task])
    await authority_db.commit()

    monkeypatch.setattr(
        external_inspection,
        "EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS",
        0.01,
    )

    async def time_out(**kwargs):
        await asyncio.sleep(60)
        raise AssertionError("cancelled inspection unexpectedly completed")

    inspector = AsyncMock(side_effect=time_out)
    monkeypatch.setattr(capability_router, "inspect_project_pr", inspector)
    body = ResidentCapabilityRequest(
        capability_id="pr.inspect", parameters={"pr_number": 23}
    )
    token = _turn_token(project, turn_task)

    with pytest.raises(HTTPException) as timed_out:
        await capability_router.invoke_resident_capability(
            None, body, session=authority_db, token=token
        )
    assert timed_out.value.status_code == 504
    assert "total time limit" in str(timed_out.value.detail)
    reservation = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert reservation.status == "pending"
    assert inspector.await_count == 1

    async def succeed(**kwargs):
        return build_pr_evidence(
            _github_pr_payload(
                owner=kwargs["owner"],
                repo=kwargs["repo"],
                pr_number=kwargs["pr_number"],
            ),
            **_pr_evidence_kwargs(kwargs),
        )

    monkeypatch.setattr(
        capability_router, "inspect_project_pr", AsyncMock(side_effect=succeed)
    )
    result = await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    assert result["evidence"]["data"]["pr_number"] == 23
    await authority_db.refresh(reservation)
    assert reservation.status == "applied"


@pytest.mark.asyncio
async def test_real_inspect_response_can_authorize_real_mutation(authority_db):
    """The exact inspection wire identity passes the later receipt evidence gate."""
    from app.routers import agent_resident_capabilities as capability_router
    from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
    from app.strategic_resident_capability_invocation import ResidentCapabilityInvocation
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY

    project, turn_task, _resident = await _authority_rows(authority_db)
    context_hash = "ctx-real-inspect-mutate"
    turn_task.vm_instance_id = "vm-1"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: context_hash,
        "_strategic_resident_claim_nonce": "nonce-1",
    }
    target = Task(
        project_id=project.id,
        title="real inspect target",
        payloads=[{"type": "plan"}],
        priority=50,
    )
    authority_db.add_all([turn_task, target])
    await authority_db.commit()
    await authority_db.refresh(turn_task)
    await authority_db.refresh(target)
    token = _turn_token(project, turn_task)

    inspected = await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id="task.inspect",
            parameters={"task_id": str(target.id)},
        ),
        session=authority_db,
        token=token,
    )
    evidence = inspected["evidence"]
    assert evidence["inspected_at"]
    assert "observed_at" not in evidence
    await authority_db.refresh(turn_task)
    await authority_db.refresh(target)
    payload = turn_task.first_payload
    invocation = ResidentCapabilityInvocation.model_validate(
        {
            "capability_id": "task.reprioritize",
            "project_id": project.id,
            "turn_task_id": turn_task.id,
            "authority_generation": 3,
            "parameters": {"task_id": str(target.id), "priority": 90},
            "operation_id": "inspect-then-reprioritize",
            "expected_revisions": {
                "authority_generation": 3,
                "active_turn_task_version": int(turn_task.version),
                "current_checkpoint_id": payload.get("checkpoint_id"),
                "resident_checkpoint_version": payload.get("checkpoint_version"),
                "resident_checkpoint_hash": payload.get("checkpoint_hash"),
                "target_task_updated_at": target.updated_at.isoformat(),
            },
            "evidence": [
                {
                    key: evidence[key]
                    for key in (
                        "kind",
                        "project_id",
                        "source_id",
                        "revision",
                        "checksum",
                        "inspected_at",
                    )
                }
            ],
        }
    )
    result = await capability_router.invoke_resident_mutation(
        None,
        invocation,
        session=authority_db,
        token=token,
    )
    assert result["status"] == "applied"
    await authority_db.refresh(target)
    assert target.priority == 90


def _reprioritize_invocation(project, turn_task, target_task, priority, *, op="rp-1"):
    from datetime import datetime, timezone

    from app.strategic_resident_capability_invocation import (
        CapabilityEvidenceRef,
        ResidentCapabilityInvocation,
    )

    payload = turn_task.first_payload
    return ResidentCapabilityInvocation.model_validate(
        {
            "capability_id": "task.reprioritize",
            "project_id": project.id,
            "turn_task_id": turn_task.id,
            "authority_generation": 3,
            "parameters": {"task_id": str(target_task.id), "priority": priority},
            "operation_id": op,
            "expected_revisions": {
                "authority_generation": 3,
                "active_turn_task_version": int(turn_task.version),
                "current_checkpoint_id": payload.get("checkpoint_id"),
                "resident_checkpoint_version": payload.get("checkpoint_version"),
                "resident_checkpoint_hash": payload.get("checkpoint_hash"),
                "target_task_updated_at": (
                    target_task.updated_at.isoformat()
                    if target_task.updated_at
                    else None
                ),
            },
            "evidence": (
                CapabilityEvidenceRef(
                    kind="task",
                    project_id=project.id,
                    source_id=str(target_task.id),
                    revision="task:v1",
                    checksum="d" * 64,
                    inspected_at=datetime.now(timezone.utc),
                ),
            ),
        }
    )


def _turn_token(project, turn_task):
    return {
        "sub": str(turn_task.id),
        "project_id": str(project.id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }


def _task_cancel_invocation(project, turn_task, target):
    from datetime import datetime, timezone

    from app.strategic_resident_capability_invocation import (
        CapabilityEvidenceRef,
        ResidentCapabilityInvocation,
    )

    payload = turn_task.first_payload
    return ResidentCapabilityInvocation.model_validate(
        {
            "capability_id": "task.cancel",
            "project_id": project.id,
            "turn_task_id": turn_task.id,
            "authority_generation": 3,
            "parameters": {"task_id": str(target.id), "reason": "held by strategy"},
            "operation_id": "cancel-1",
            "expected_revisions": {
                "authority_generation": 3,
                "active_turn_task_version": int(turn_task.version),
                "current_checkpoint_id": payload.get("checkpoint_id"),
                "resident_checkpoint_version": payload.get("checkpoint_version"),
                "resident_checkpoint_hash": payload.get("checkpoint_hash"),
                "target_task_updated_at": (
                    target.updated_at.isoformat() if target.updated_at else None
                ),
            },
            "evidence": (
                CapabilityEvidenceRef(
                    kind="task",
                    project_id=project.id,
                    source_id=str(target.id),
                    revision="task:v1",
                    checksum="e" * 64,
                    inspected_at=datetime.now(timezone.utc),
                ),
            ),
        }
    )


@pytest.mark.asyncio
async def test_task_cancel_runs_the_operator_path_end_to_end(authority_db, monkeypatch):
    """task.cancel actually cancels the target task through modify_task_apply."""
    from app.routers import agent_resident_capabilities as capability_router
    from app import admin_task_mutations

    # Neutralize only the out-of-process broadcast; the real status transition runs.
    async def _noop_broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_task_mutations, "broadcast_to_project", _noop_broadcast)
    monkeypatch.setattr(
        capability_router,
        "_check_evidence_refs_against_receipts",
        AsyncMock(),
    )
    project, turn_task, _resident = await _authority_rows(authority_db)
    target = Task(
        project_id=project.id,
        title="worker",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "plan"}],
    )
    authority_db.add(target)
    await authority_db.commit()
    await authority_db.refresh(target)

    result = await capability_router.invoke_resident_mutation(
        None,
        _task_cancel_invocation(project, turn_task, target),
        session=authority_db,
        token=_turn_token(project, turn_task),
    )
    assert result["status"] in ("reconciled", "failed")

    await authority_db.refresh(target)
    # The operator modify path actually cancelled the target task.
    assert target.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_reprioritize_endpoint_mutates_target_task_with_cas(
    authority_db,
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY

    project, turn_task, _resident = await _authority_rows(authority_db)
    context_hash = "ctx-reprioritize-e2e"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: context_hash,
    }
    authority_db.add(turn_task)
    target = Task(project_id=project.id, title="worker", payloads=[{"type": "plan"}], priority=50)
    authority_db.add(target)
    await authority_db.commit()
    await authority_db.refresh(turn_task)
    await authority_db.refresh(target)

    invocation = _reprioritize_invocation(project, turn_task, target, 90)
    authority_db.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=turn_task.id,
            authority_generation=3,
            operation_id="inspect-reprioritize-target",
            operation_kind="inspect_task",
            capability="task.inspect",
            risk_level="low",
            request_hash="b" * 64,
            expected_revisions={"context_hash": context_hash},
            status="applied",
            receipt={
                "schema_version": 1,
                "structured_result": {
                    "evidence": [
                        {
                            "kind": ref.kind,
                            "source_id": ref.source_id,
                            "revision": ref.revision,
                            "checksum": ref.checksum,
                        }
                        for ref in invocation.evidence
                    ]
                },
            },
        )
    )
    await authority_db.commit()

    result = await capability_router.invoke_resident_mutation(
        None,
        invocation,
        session=authority_db,
        token=_turn_token(project, turn_task),
    )
    assert result["status"] == "applied"
    await authority_db.refresh(target)
    assert target.priority == 90

    receipt = (
        await authority_db.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.operation_id == "rp-1"
            )
        )
    ).scalar_one()
    assert receipt.operation_kind == "reprioritize_task"
    assert receipt.capability == "task.reprioritize"
    assert receipt.observed_revisions_before["target_task_updated_at"] is not None


def _pr_merge_invocation(project, turn_task, *, pr_number=42, head="abc1234"):
    from datetime import datetime, timezone

    from app.strategic_resident_capability_invocation import (
        CapabilityEvidenceRef,
        ResidentCapabilityInvocation,
    )

    payload = turn_task.first_payload
    return ResidentCapabilityInvocation.model_validate(
        {
            "capability_id": "pr.merge",
            "project_id": project.id,
            "turn_task_id": turn_task.id,
            "authority_generation": 3,
            "parameters": {"pr_number": pr_number, "expected_head_sha": head},
            "operation_id": "merge-1",
            "expected_revisions": {
                "authority_generation": 3,
                "active_turn_task_version": int(turn_task.version),
                "current_checkpoint_id": payload.get("checkpoint_id"),
                "resident_checkpoint_version": payload.get("checkpoint_version"),
                "resident_checkpoint_hash": payload.get("checkpoint_hash"),
            },
            "evidence": tuple(
                CapabilityEvidenceRef(
                    kind=kind,
                    project_id=project.id,
                    source_id=f"pr:{pr_number}",
                    revision=head,
                    checksum=str(index) * 64,
                    inspected_at=datetime.now(timezone.utc),
                )
                for index, kind in enumerate(("pr", "pr_diff", "pr_check"))
            ),
        }
    )


@pytest.mark.asyncio
async def test_pr_merge_requires_a_matching_operator_approval(authority_db, monkeypatch):
    from app.routers import agent_resident_capabilities as capability_router

    external = AsyncMock()
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    project, turn_task, _resident = await _authority_rows(authority_db)

    # PR inspection adapters do not exist yet, so merge remains proposal-only.
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _pr_merge_invocation(project, turn_task),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 501
    external.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_merge_dispatches_with_a_matching_approval(authority_db, monkeypatch):
    from app.routers import agent_resident_capabilities as capability_router
    from app.models import StrategicOperationApproval
    from app.strategic_resident_operation_approval import (
        derive_approval_key,
        grant_operation_approval,
    )

    receipt = SimpleNamespace(
        operation_id="merge-1", status="reconciled", id=uuid.uuid4()
    )
    external = AsyncMock(return_value=receipt)
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    project, turn_task, _resident = await _authority_rows(authority_db)
    # An operator grants approval for THIS exact merge (pr 42 @ abc1234).
    await grant_operation_approval(
        authority_db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=derive_approval_key(
            "pr.merge", {"pr_number": 42, "expected_head_sha": "abc1234"}
        ),
        granted_by="operator-1",
        ttl_seconds=600,
    )
    await authority_db.commit()

    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _pr_merge_invocation(project, turn_task),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 501
    external.assert_not_awaited()
    # Proposal-only rejection happens before consuming a durable approval.
    approval = (
        await authority_db.execute(select(StrategicOperationApproval))
    ).scalar_one()
    assert approval.consumed_operation_id is None


@pytest.mark.asyncio
async def test_pr_merge_real_executor_accepts_exact_evidence_and_approval(
    authority_db, monkeypatch
):
    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY
    from app.strategic_resident_operation_approval import (
        derive_approval_key,
        grant_operation_approval,
    )

    project, turn_task, _resident = await _authority_rows(authority_db)
    context_hash = "ctx-real-approved-merge"
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: context_hash,
    }
    authority_db.add(turn_task)
    invocation = _pr_merge_invocation(project, turn_task)
    authority_db.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=turn_task.id,
            authority_generation=3,
            operation_id="inspect-pr-for-merge",
            operation_kind="inspect_pr",
            capability="pr.inspect",
            risk_level="low",
            request_hash="f" * 64,
            expected_revisions={"context_hash": context_hash},
            status="applied",
            receipt={
                "schema_version": 1,
                "structured_result": {
                    "evidence": [
                        {
                            "kind": ref.kind,
                            "source_id": ref.source_id,
                            "revision": ref.revision,
                            "checksum": ref.checksum,
                        }
                        for ref in invocation.evidence
                    ]
                },
            },
        )
    )
    await grant_operation_approval(
        authority_db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=derive_approval_key(
            "pr.merge", {"pr_number": 42, "expected_head_sha": "abc1234"}
        ),
        granted_by="operator-1",
        ttl_seconds=600,
    )
    await authority_db.commit()

    async def perform(_session):
        return {"merged": True, "pr_number": 42, "sha": "abc1234"}

    monkeypatch.setattr(
        capability_router,
        "build_external_effect",
        lambda *_args, **_kwargs: perform,
    )
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            invocation,
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 501
    assert (
        await authority_db.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.operation_id == "merge-1"
            )
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_pr_merge_untrusted_evidence_does_not_consume_approval(
    authority_db, monkeypatch
):
    from app.models import StrategicOperationApproval
    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY
    from app.strategic_resident_operation_approval import (
        derive_approval_key,
        grant_operation_approval,
    )

    project, turn_task, _resident = await _authority_rows(authority_db)
    turn_task.context = {
        **dict(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "ctx-approved-merge",
    }
    authority_db.add(turn_task)
    await grant_operation_approval(
        authority_db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=derive_approval_key(
            "pr.merge", {"pr_number": 42, "expected_head_sha": "abc1234"}
        ),
        granted_by="operator-1",
        ttl_seconds=600,
    )
    await authority_db.commit()

    external = AsyncMock()
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    # Proposal-only rejection precedes evidence and approval handling.
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _pr_merge_invocation(project, turn_task),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 501
    external.assert_not_awaited()
    approval = (
        await authority_db.execute(select(StrategicOperationApproval))
    ).scalar_one()
    assert approval.consumed_at is None
    assert approval.consumed_operation_id is None


@pytest.mark.asyncio
async def test_pr_merge_approval_for_a_different_pr_is_rejected(authority_db, monkeypatch):
    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_operation_approval import (
        derive_approval_key,
        grant_operation_approval,
    )

    external = AsyncMock()
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    project, turn_task, _resident = await _authority_rows(authority_db)
    # Even an unrelated approval is untouched while merge is proposal-only.
    await grant_operation_approval(
        authority_db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=derive_approval_key(
            "pr.merge", {"pr_number": 7, "expected_head_sha": "abc1234"}
        ),
        granted_by="operator-1",
        ttl_seconds=600,
    )
    await authority_db.commit()

    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _pr_merge_invocation(project, turn_task, pr_number=42),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 501
    external.assert_not_awaited()


@pytest.mark.asyncio
async def test_reprioritize_stale_target_version_fails_closed(authority_db):
    from app.routers import agent_resident_capabilities as capability_router

    project, turn_task, _resident = await _authority_rows(authority_db)
    target = Task(project_id=project.id, title="worker", payloads=[{"type": "plan"}], priority=50)
    authority_db.add(target)
    await authority_db.commit()
    await authority_db.refresh(target)
    invocation = _reprioritize_invocation(project, turn_task, target, 90)
    # Concurrent bump: the target task advances after the agent read its version.
    target.title = "worker-moved"
    authority_db.add(target)
    await authority_db.commit()

    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None, invocation, session=authority_db, token=_turn_token(project, turn_task)
        )
    assert exc.value.status_code == 409
    await authority_db.refresh(target)
    assert target.priority == 50  # unchanged


@pytest.mark.asyncio
async def test_reprioritize_cannot_target_a_resident_turn(authority_db):
    from app.routers import agent_resident_capabilities as capability_router

    project, turn_task, _resident = await _authority_rows(authority_db)
    # Another resident-turn task in the same project must never be a mutation target.
    other_turn = Task(
        project_id=project.id,
        title="other turn",
        payloads=[{"type": "strategic_resident_turn"}],
        priority=50,
    )
    authority_db.add(other_turn)
    await authority_db.commit()
    await authority_db.refresh(other_turn)

    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _reprioritize_invocation(project, turn_task, other_turn, 90),
            session=authority_db,
            token=_turn_token(project, turn_task),
        )
    assert exc.value.status_code == 409
    await authority_db.refresh(other_turn)
    assert other_turn.priority == 50


def test_request_fingerprint_binds_target_task_id():
    """Two requests identical except target_task_id hash differently.

    Guards the explicit target_task_id fingerprint binding for a future
    task-mutation capability whose params do NOT echo the target id (unlike
    task.reprioritize, whose request_payload already differs by task_id).
    """
    from app.strategic_operation_authority_validation import _request_fingerprint

    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    common = {
        "project_id": project_id,
        "turn_task_id": turn_id,
        "authority_generation": 3,
        "operation_id": "op-x",
        "operation_kind": "reprioritize_task",
        "capability": "task.reprioritize",
        "risk": "low",
        "expected_revisions": {"target_task_updated_at": "2026-07-17T00:00:00"},
    }
    # Identical request_payload on purpose, so only target_task_id differs.
    payload = {"note": "same payload"}
    a = FencedOperationRequest(
        **common, request_payload=payload, target_task_id=uuid.uuid4()
    )
    b = FencedOperationRequest(
        **common, request_payload=payload, target_task_id=uuid.uuid4()
    )
    assert _request_fingerprint(a, request_payload=payload) != _request_fingerprint(
        b, request_payload=payload
    )
    # A None target (Project-field op) hashes stably to itself.
    c = FencedOperationRequest(
        **common, request_payload=payload, target_task_id=None
    )
    assert _request_fingerprint(c, request_payload=payload) == _request_fingerprint(
        c, request_payload=payload
    )


@pytest.mark.asyncio
async def test_reprioritize_operation_id_reuse_across_targets_collides(
    authority_db, monkeypatch
):
    """Same operation_id with a different target task must 409, not mis-adopt."""
    from app.routers import agent_resident_capabilities as capability_router
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )

    project, turn_task, _resident = await _authority_rows(authority_db)
    first = Task(project_id=project.id, title="a", payloads=[{"type": "plan"}], priority=50)
    second = Task(project_id=project.id, title="b", payloads=[{"type": "plan"}], priority=50)
    authority_db.add_all([first, second])
    await authority_db.commit()
    await authority_db.refresh(first)
    await authority_db.refresh(second)

    token = _turn_token(project, turn_task)
    await capability_router.invoke_resident_mutation(
        None,
        _reprioritize_invocation(project, turn_task, first, 90, op="dup-op"),
        session=authority_db,
        token=token,
    )
    # Reusing op="dup-op" but pointing at a DIFFERENT target task must collide,
    # because target_task_id is bound into the request fingerprint.
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _reprioritize_invocation(project, turn_task, second, 90, op="dup-op"),
            session=authority_db,
            token=token,
        )
    assert exc.value.status_code == 409
    await authority_db.refresh(second)
    assert second.priority == 50  # the second target was never mutated


@pytest.mark.asyncio
async def test_reprioritize_cannot_target_own_turn_task(authority_db):
    from app.routers import agent_resident_capabilities as capability_router

    project, turn_task, _resident = await _authority_rows(authority_db)
    invocation = _reprioritize_invocation(project, turn_task, turn_task, 90)
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None, invocation, session=authority_db, token=_turn_token(project, turn_task)
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_id", "task_type"),
    [
        ("task.code_change.create", "create"),
        ("task.debug.create", "debug"),
    ],
)
async def test_managed_task_create_is_atomic_replay_safe_and_source_bound(
    authority_db,
    capability_id,
    task_type,
):
    project, turn_task, _resident = await _authority_rows(authority_db)
    artifact = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=7,
        checksum="a" * 64,
        content_snapshot={"text": "frozen source"},
        retrieval_metadata={"capability": "project_tool_revision"},
    )
    authority_db.add(artifact)
    debug_target = None
    if task_type == "debug":
        debug_target = Task(
            project_id=project.id,
            title="Failed managed source",
            payloads=[{"type": "create", "prompt": "Original work"}],
        )
        authority_db.add(debug_target)
    await authority_db.flush()
    turn_task.context = {
        **(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "b" * 64,
        RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(artifact.id)],
    }
    await authority_db.commit()
    await authority_db.refresh(turn_task)
    request = _request(
        project,
        turn_task,
        operation_id=f"managed-{task_type}",
        payload={
            **({"task_id": str(debug_target.id)} if debug_target else {}),
            "title": f"Managed {task_type}",
            "prompt": "Do bounded work.",
        },
        expected=(
            {"target_task_updated_at": debug_target.updated_at.isoformat()}
            if debug_target
            else None
        ),
    )
    request = FencedOperationRequest(
        **{
            **request.__dict__,
            "operation_kind": "create_managed_task",
            "capability": capability_id,
            "risk": "medium",
            "request_payload": {
                **({"task_id": str(debug_target.id)} if debug_target else {}),
                "title": f"Managed {task_type}",
                "prompt": "Do bounded work.",
                "_source_evidence": [
                    {
                        "kind": "task",
                        "project_id": str(project.id),
                        "source_id": (
                            str(debug_target.id)
                            if debug_target
                            else "task:source"
                        ),
                        "revision": "revision-1",
                        "checksum": "c" * 64,
                        "inspected_at": "2026-07-18T00:00:00Z",
                    }
                ] if debug_target else [],
            },
            "target_task_id": debug_target.id if debug_target else None,
        }
    )
    callback_calls = 0
    adapter = managed_task_create_callback(capability_id)
    assert adapter is not None

    def mutate(context):
        nonlocal callback_calls
        callback_calls += 1
        return adapter(context)

    first = await execute_fenced_db_operation(authority_db, request, mutate)
    replay = await execute_fenced_db_operation(authority_db, request, mutate)
    rows = (
        await authority_db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.created_by == "STRATEGIC_AGENT",
                Task.id != turn_task.id,
            )
        )
    ).scalars().all()

    assert replay.id == first.id
    assert callback_calls == 1
    assert len(rows) == 1
    child = rows[0]
    assert child.status == TaskStatus.QUEUED
    assert child.parent_task_id is None
    assert child.first_payload["type"] == task_type
    lineage = child.context["_resident_managed_task"]
    assert lineage["source_turn_task_id"] == str(turn_task.id)
    assert lineage["receipt_id"] == str(first.id)
    assert lineage["context_hash"] == "b" * 64
    assert lineage["artifact_refs"][0]["revision"] == 7
    assert lineage["artifact_refs"][0]["checksum"] == "a" * 64
    if debug_target:
        assert lineage["evidence"][0]["source_id"] == str(debug_target.id)
    else:
        assert lineage["evidence"] == []
    assert set(lineage["artifact_refs"][0]) == {
        "artifact_ref_id",
        "tool_id",
        "kind",
        "revision",
        "checksum",
        "author",
        "timestamp",
        "retrieval",
    }
    assert lineage["target_task_id"] == (
        str(debug_target.id) if debug_target else None
    )
    assert child.context[RESIDENT_ARTIFACT_REF_GRANTS_KEY] == [str(artifact.id)]
    assert child.idempotency_key == f"resident-receipt:{first.id}"
    if task_type == "debug":
        assert child.first_payload["failure_context"] == "Do bounded work."
    assert first.receipt["structured_result"]["facts"][0]["task_id"] == str(
        child.id
    )
    changed_evidence_request = FencedOperationRequest(
        **{
            **request.__dict__,
            "request_payload": {
                **dict(request.request_payload),
                "_source_evidence": (
                    [
                        {
                            **dict(request.request_payload["_source_evidence"][0]),
                            "checksum": "e" * 64,
                        }
                    ]
                    if debug_target
                    else [
                        {
                            "kind": "task",
                            "project_id": str(project.id),
                            "source_id": "newly-added-evidence",
                            "revision": "revision-2",
                            "checksum": "e" * 64,
                            "inspected_at": "2026-07-18T00:00:00Z",
                        }
                    ]
                ),
            },
        }
    )
    with pytest.raises(OperationIdentityCollision):
        await execute_fenced_db_operation(
            authority_db, changed_evidence_request, mutate
        )


@pytest.mark.asyncio
async def test_managed_task_callback_cannot_escape_receipt_capability(authority_db):
    project, turn_task, _resident = await _authority_rows(authority_db)
    request = _request(
        project,
        turn_task,
        operation_id="managed-capability-mismatch",
        payload={"title": "Unauthorized child", "prompt": "Must not run."},
    )

    with pytest.raises(
        FencedOperationUsageError,
        match="not authorized by the receipt capability",
    ):
        await execute_fenced_db_operation(
            authority_db,
            request,
            managed_task_create_callback("task.code_change.create"),
        )

    tasks = (await authority_db.execute(select(Task))).scalars().all()
    assert [task.id for task in tasks] == [turn_task.id]
    receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert receipts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["foreign_project", "resident_turn"])
async def test_managed_debug_rejects_invalid_target_scope(authority_db, target_kind):
    project, turn_task, _resident = await _authority_rows(authority_db)
    if target_kind == "resident_turn":
        target = turn_task
    else:
        foreign = Project(name="Foreign", user_id="operator")
        authority_db.add(foreign)
        await authority_db.flush()
        target = Task(
            project_id=foreign.id,
            title="Foreign task",
            payloads=[{"type": "create", "prompt": "foreign"}],
        )
        authority_db.add(target)
        await authority_db.commit()
    request = _request(
        project,
        turn_task,
        operation_id=f"managed-debug-{target_kind}",
        payload={
            "task_id": str(target.id),
            "title": "Debug target",
            "prompt": "Diagnose it.",
        },
        expected={"target_task_updated_at": target.updated_at.isoformat()},
    )
    request = FencedOperationRequest(
        **{
            **request.__dict__,
            "operation_kind": "create_managed_task",
            "capability": "task.debug.create",
            "risk": "medium",
            "target_task_id": target.id,
        }
    )
    expected_error = (
        AuthorityBoundaryMutation
        if target_kind == "resident_turn"
        else InactiveTurnAuthority
    )
    with pytest.raises(expected_error):
        await execute_fenced_db_operation(
            authority_db,
            request,
            managed_task_create_callback("task.debug.create"),
        )


@pytest.mark.asyncio
async def test_managed_debug_requires_target_updated_at_cas(authority_db):
    project, turn_task, _resident = await _authority_rows(authority_db)
    target = Task(
        project_id=project.id,
        title="Debug source",
        payloads=[{"type": "create", "prompt": "source"}],
    )
    authority_db.add(target)
    await authority_db.commit()
    request = _request(
        project,
        turn_task,
        operation_id="managed-debug-missing-target-cas",
        payload={
            "task_id": str(target.id),
            "title": "Debug source",
            "prompt": "Diagnose it.",
        },
    )
    request = FencedOperationRequest(
        **{
            **request.__dict__,
            "operation_kind": "create_managed_task",
            "capability": "task.debug.create",
            "risk": "medium",
            "target_task_id": target.id,
        }
    )
    with pytest.raises(ExpectedRevisionMismatch, match="target_task_updated_at"):
        await execute_fenced_db_operation(
            authority_db,
            request,
            managed_task_create_callback("task.debug.create"),
        )


@pytest.mark.asyncio
async def test_managed_task_same_operation_id_is_isolated_across_projects(
    authority_db,
):
    rows = []
    for index in range(2):
        project, turn_task, _resident = await _authority_rows(authority_db)
        turn_task.context = {
            **(turn_task.context or {}),
            TURN_CONTEXT_HASH_KEY: f"project-{index}-context",
        }
        await authority_db.commit()
        await authority_db.refresh(turn_task)
        request = _request(
            project,
            turn_task,
            operation_id="same-operation-id",
            payload={
                "title": f"Project {index} task",
                "prompt": "Implement bounded work.",
            },
        )
        request = FencedOperationRequest(
            **{
                **request.__dict__,
                "operation_kind": "create_managed_task",
                "capability": "task.code_change.create",
                "risk": "medium",
            }
        )
        receipt = await execute_fenced_db_operation(
            authority_db,
            request,
            managed_task_create_callback("task.code_change.create"),
        )
        child = (
            await authority_db.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "STRATEGIC_AGENT",
                    Task.id != turn_task.id,
                )
            )
        ).scalar_one()
        rows.append((receipt, child))

    assert rows[0][0].operation_id == rows[1][0].operation_id
    assert rows[0][1].idempotency_key != rows[1][1].idempotency_key
    assert rows[0][1].idempotency_key == f"resident-receipt:{rows[0][0].id}"
    assert rows[1][1].idempotency_key == f"resident-receipt:{rows[1][0].id}"


@pytest.mark.asyncio
async def test_managed_task_rejects_oversized_artifact_handoff(authority_db):
    from app.strategic_resident_managed_task import MAX_MANAGED_TASK_ARTIFACT_REFS

    project, turn_task, _resident = await _authority_rows(authority_db)
    artifacts = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=hashlib.sha256(f"artifact-{index}".encode()).hexdigest(),
            content_snapshot={"text": f"source {index}"},
        )
        for index in range(MAX_MANAGED_TASK_ARTIFACT_REFS + 1)
    ]
    authority_db.add_all(artifacts)
    await authority_db.flush()
    turn_task.context = {
        **(turn_task.context or {}),
        TURN_CONTEXT_HASH_KEY: "oversized-artifact-context",
        RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(item.id) for item in artifacts],
    }
    await authority_db.commit()
    await authority_db.refresh(turn_task)
    request = _request(
        project,
        turn_task,
        operation_id="managed-artifact-overflow",
        payload={"title": "Too many sources", "prompt": "Keep bounded."},
    )
    request = FencedOperationRequest(
        **{
            **request.__dict__,
            "operation_kind": "create_managed_task",
            "capability": "task.code_change.create",
            "risk": "medium",
        }
    )
    with pytest.raises(FencedOperationUsageError, match="bounded handoff limit"):
        await execute_fenced_db_operation(
            authority_db,
            request,
            managed_task_create_callback("task.code_change.create"),
        )
