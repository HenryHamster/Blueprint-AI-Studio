"""Focused tests for M2a database-only Resident operation fencing."""

from __future__ import annotations

import hashlib
import inspect
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models import (
    Project,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_operation_authority import (
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
    checkpoint_body = {"schema_version": 1}
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=1,
        content_hash=hashlib.sha256(b'{"schema_version":1}').hexdigest(),
        checkpoint=checkpoint_body,
        authority_generation=generation,
        source_turn_task_id=None,
    )
    session.add(checkpoint)
    await session.commit()
    turn_payload = dict(task.first_payload)
    turn_payload.update(
        {
            "checkpoint_id": str(checkpoint.id),
            "checkpoint_version": checkpoint.version,
            "checkpoint_hash": checkpoint.content_hash,
        }
    )
    task.payloads = [turn_payload]
    session.add(task)
    await session.commit()
    await session.refresh(task)
    resident = StrategicResident(
        project_id=project.id,
        engine_owner=owner,
        engine_version="resident-v2",
        authority_generation=generation,
        active_turn_task_id=task.id,
        active_turn_lane="interactive",
        current_checkpoint_id=checkpoint.id,
    )
    session.add(resident)
    await session.commit()
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
async def test_mutation_endpoint_applies_running_update_end_to_end(authority_db):
    """The /mutate endpoint drives a real fenced DB-only mutation to `applied`."""
    from datetime import datetime, timezone

    from app.routers import agent_resident_capabilities as capability_router
    from app.strategic_resident_capability_invocation import (
        CapabilityEvidenceRef,
        ResidentCapabilityInvocation,
    )

    project, task, _resident = await _authority_rows(authority_db)
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

    receipt = (await authority_db.execute(select(StrategicOperationReceipt))).scalar_one()
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
    assert len(all_receipts) == 1


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

    receipt = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalar_one()
    assert receipt.operation_kind == "inspect_task_log"
    assert receipt.capability == "task.logs.inspect"
    assert receipt.status == "applied"
    assert receipt.expected_revisions == {"context_hash": "ctx-hash-1"}
    assert receipt.receipt["structured_result"]["evidence"][0]["kind"] == "task_log"

    # Idempotent: the per-turn ledger dedups the same inspection -> one receipt.
    await capability_router.invoke_resident_capability(
        None, body, session=authority_db, token=token
    )
    receipts = (
        await authority_db.execute(select(StrategicOperationReceipt))
    ).scalars().all()
    assert len(receipts) == 1
