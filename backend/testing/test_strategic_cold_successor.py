"""Cold-successor authority is limited to exact durable strategic sources."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.auth import create_agent_token
from app.models import (
    KnowledgeDocument,
    Project,
    ProjectMessage,
    SourceKind,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, _MAX_PAYLOAD_BYTES
from app.strategic_cold_successor import (
    ColdSuccessorContractError,
    load_locked_cold_successor_contract,
    serialize_cold_successor_contract,
)
from app.strategic_memory_sources import canonical_json_bytes, sha256_hex
from app.strategic_cold_successor_authority import (
    enforce_cold_successor_request_authority,
)
import app.strategic_cold_successor_lifecycle as lifecycle
from app.strategic_cold_successor_lifecycle import (
    COLD_SUCCESSOR_ATTESTATION_KEY,
    COLD_SUCCESSOR_COMPLETION_EFFECT_KEY,
    COLD_SUCCESSOR_CONTEXT_KEY,
    COLD_SUCCESSOR_TERMINAL_KEY,
    ColdSuccessorAttestationRequest,
    ColdSuccessorHeartbeatRequest,
    ColdSuccessorReceiptRequest,
    ColdSuccessorStartRequest,
    ColdSuccessorTerminalRequest,
    attest_cold_successor,
    cold_successor_status,
    heartbeat_cold_successor,
    launch_cold_successor,
    read_cold_successor_receipt,
    terminal_cold_successor,
)
from app.strategic_resident_session import RESIDENT_SESSION_KEY


def _project(**overrides):
    values = {
        "name": "Cold successor",
        "user_id": "owner",
        "paused": True,
        "autonomous_enabled": False,
        "archived": False,
        "problem_model": {"revision": 7, "nested": {"unicode": "λ 🐹"}},
        "running_update": "  exact update\nwith trailing whitespace  \n",
    }
    values.update(overrides)
    return Project(**values)


def _memory(project_id, *, document_id, created_at, **overrides):
    values = {
        "id": document_id,
        "project_id": project_id,
        "source_kind": SourceKind.STRATEGIC_MEMORY.value,
        "title": "Durable rule",
        "summary": "Keep the exact source",
        "body": "Never infer from a transcript.",
        "topic": "operator_preference",
        "direction_id": "d-cold",
        "tags": ["authority", "exact"],
        "lifecycle_status": "consumed",
        "created_at": created_at,
    }
    values.update(overrides)
    return KnowledgeDocument(**values)


def _assert_section(section, exact_bytes):
    assert section["byte_count"] == len(exact_bytes)
    assert section["sha256"] == sha256_hex(exact_bytes)


@pytest.mark.asyncio
async def test_locked_contract_is_exact_ordered_and_excludes_session_sources(db_session):
    project = _project(
        vision={"forbidden_vision": "VISION_SENTINEL"},
        strategic_memory_notes=[{"note": "LEGACY_MEMORY_SENTINEL"}],
    )
    foreign = _project(name="Foreign")
    db_session.add_all([project, foreign])
    await db_session.flush()

    source_task = Task(
        project_id=project.id,
        title="Forbidden session source",
        payloads=[{"type": "strategic_review"}],
        context={
            "_session_summary": "SESSION_SUMMARY_SENTINEL",
            "_decisions_reg": ["DECISIONS_SENTINEL"],
        },
    )
    db_session.add(source_task)
    await db_session.flush()
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            content="TRANSCRIPT_SENTINEL",
            context={"cursor": "CURSOR_SENTINEL"},
        )
    )

    at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
    first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    included_second = _memory(
        project.id,
        document_id=second_id,
        created_at=at,
        title="Second by UUID",
        source_task_id=source_task.id,
        source_citations=[{"secret": "CITATION_SENTINEL"}],
        consumed_at=None,
    )
    included_first = _memory(
        project.id,
        document_id=first_id,
        created_at=at,
        title="First by UUID",
        body="Full body 🐹",
    )
    excluded = [
        _memory(
            project.id,
            document_id=uuid.uuid4(),
            created_at=at + timedelta(seconds=1),
            body="DISMISSED_SENTINEL",
            lifecycle_status="dismissed",
        ),
        _memory(
            project.id,
            document_id=uuid.uuid4(),
            created_at=at + timedelta(seconds=2),
            body="PENDING_SENTINEL",
            lifecycle_status="pending",
        ),
        _memory(
            project.id,
            document_id=uuid.uuid4(),
            created_at=at + timedelta(seconds=3),
            body="DESIGN_SENTINEL",
            source_kind=SourceKind.DESIGN.value,
        ),
        _memory(
            foreign.id,
            document_id=uuid.uuid4(),
            created_at=at + timedelta(seconds=4),
            body="FOREIGN_SENTINEL",
        ),
    ]
    db_session.add_all([included_second, included_first, *excluded])
    await db_session.commit()

    payload = await load_locked_cold_successor_contract(db_session, project.id)
    contract = payload["cold_successor_contract"]
    core = contract["core"]
    manifest = contract["manifest"]

    problem_model_bytes = core["problem_model_canonical_json"].encode("utf-8")
    memory_bytes = core["strategic_memory_canonical_json"].encode("utf-8")
    running_update_bytes = core["running_update"]["text"].encode("utf-8")
    memories = json.loads(memory_bytes)

    assert json.loads(problem_model_bytes) == project.problem_model
    assert core["running_update"] == {
        "present": True,
        "text": project.running_update,
    }
    assert [item["id"] for item in memories] == [str(first_id), str(second_id)]
    assert memories[0]["body"] == "Full body 🐹"
    assert memories[1]["title"] == "Second by UUID"
    assert manifest["strategic_memory"]["document_count"] == 2
    _assert_section(manifest["problem_model"], problem_model_bytes)
    _assert_section(manifest["running_update"], running_update_bytes)
    _assert_section(manifest["strategic_memory"], memory_bytes)
    _assert_section(manifest["core"], canonical_json_bytes(core))

    rendered = json.dumps(payload, default=str)
    assert len(rendered.encode()) <= _MAX_PAYLOAD_BYTES
    for sentinel in (
        "VISION_SENTINEL",
        "LEGACY_MEMORY_SENTINEL",
        "SESSION_SUMMARY_SENTINEL",
        "DECISIONS_SENTINEL",
        "TRANSCRIPT_SENTINEL",
        "CURSOR_SENTINEL",
        "CITATION_SENTINEL",
        "DISMISSED_SENTINEL",
        "PENDING_SENTINEL",
        "DESIGN_SENTINEL",
        "FOREIGN_SENTINEL",
        str(source_task.id),
    ):
        assert sentinel not in rendered


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"paused": False}, "project must be paused"),
        ({"autonomous_enabled": True}, "autonomy must be disabled"),
        ({"archived": True}, "archived project"),
    ],
)
def test_contract_fails_closed_outside_safety_fence(override, message):
    project = _project(**override)
    original = project.model_dump()

    with pytest.raises(ColdSuccessorContractError, match=message):
        serialize_cold_successor_contract(project, [])

    assert project.model_dump() == original


@pytest.mark.parametrize("running_update", [None, ""])
def test_contract_preserves_null_versus_empty_running_update(running_update):
    project = _project(running_update=running_update)

    payload = serialize_cold_successor_contract(project, [])
    contract = payload["cold_successor_contract"]
    running = contract["core"]["running_update"]
    section = contract["manifest"]["running_update"]

    assert running == {"present": running_update is not None, "text": running_update}
    exact_bytes = b"null" if running_update is None else b""
    _assert_section(section, exact_bytes)


def test_contract_rejects_oversize_without_trimming():
    project = _project()
    memory = _memory(
        project.id,
        document_id=uuid.uuid4(),
        created_at=datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
        body="x" * (_MAX_PAYLOAD_BYTES + 1),
    )

    with pytest.raises(PayloadTooLargeError) as raised:
        serialize_cold_successor_contract(project, [memory])

    assert raised.value.limit == _MAX_PAYLOAD_BYTES
    assert "_packer_meta" not in str(raised.value)


async def _ended_predecessor(db_session):
    project = _project(
        vision={
            "unrelated": "PRESERVE_ME",
            "_strategic_failures": 2,
        }
    )
    db_session.add(project)
    await db_session.flush()
    predecessor = Task(
        project_id=project.id,
        title="Ended predecessor",
        payloads=[{"type": "strategic_review"}],
        context={
            "_resident_session": True,
            "_session_summary": "FORBIDDEN_PREDECESSOR_SUMMARY",
            "_decisions_reg": "FORBIDDEN_PREDECESSOR_DECISIONS",
            "_exam_ledger": "FORBIDDEN_PREDECESSOR_EXAM",
            "message_cursor": "FORBIDDEN_PREDECESSOR_CURSOR",
        },
        status=TaskStatus.COMPLETED,
    )
    db_session.add(predecessor)
    await db_session.flush()
    project.vision = {
        **project.vision,
        RESIDENT_SESSION_KEY: {
            "version": 2,
            "session_id": "ss-ended-predecessor",
            "task_id": str(predecessor.id),
            "worker_generation": 4,
            "status": "ended",
            "stop_requested": True,
            "_session_summary": "FORBIDDEN_BLOB_SUMMARY",
            "_decisions_reg": "FORBIDDEN_BLOB_DECISIONS",
        },
    }
    memory = _memory(
        project.id,
        document_id=uuid.uuid4(),
        created_at=datetime(2026, 8, 30, 13, 0, 0, tzinfo=timezone.utc),
        body="Durable successor memory.",
        consumed_at=None,
    )
    db_session.add_all([project, memory])
    await db_session.commit()
    return project, predecessor


async def _launch_active_cold_successor(db_session, monkeypatch, *, key: str):
    project, predecessor = await _ended_predecessor(db_session)
    runner_id = uuid.uuid4()
    monkeypatch.setattr(
        lifecycle,
        "_resolve_override_runner",
        AsyncMock(return_value=SimpleNamespace(id=runner_id)),
    )
    monkeypatch.setattr(lifecycle, "_resident_runner_is_available", lambda _runner: True)
    launch_request = ColdSuccessorStartRequest(
        runner_id=runner_id,
        idempotency_key=key,
        expected_predecessor_session_id="ss-ended-predecessor",
        expected_predecessor_task_id=predecessor.id,
        expected_predecessor_worker_generation=4,
    )
    launched = await launch_cold_successor(
        db_session,
        project.id,
        launch_request,
        operator_user_id="owner",
    )
    assert launched["launch_effect_sha256"] == lifecycle._launch_effect_sha256(
        project.id, launch_request
    )
    task = await db_session.get(Task, uuid.UUID(launched["task_id"]))
    assert task is not None
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()
    heartbeat = ColdSuccessorHeartbeatRequest(
        session_id=launched["session_id"],
        task_id=task.id,
        worker_generation=1,
        resident_incarnation_id=uuid.uuid4(),
    )
    await heartbeat_cold_successor(db_session, project.id, heartbeat)
    return project, task, heartbeat


@pytest.mark.asyncio
async def test_cold_receipt_read_is_pure_exact_and_incarnation_fenced(
    db_session,
    monkeypatch,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-pure-receipt-read",
    )
    request = ColdSuccessorReceiptRequest(**heartbeat.model_dump())
    before_vision = json.loads(json.dumps(project.vision))
    before_context = json.loads(json.dumps(task.context))
    before_history = (
        await db_session.execute(
            select(TaskHistory.id).where(TaskHistory.task_id == task.id)
        )
    ).scalars().all()

    pending = await read_cold_successor_receipt(db_session, project.id, request)
    repeated = await read_cold_successor_receipt(db_session, project.id, request)

    assert pending == repeated
    assert pending["receipt_state"] == "pending"
    assert pending["task_status"] == "IN_PROGRESS"
    assert pending["attestation"] is None
    assert pending["terminal"] is None
    assert pending["completion_terminal_effect_id"] == task.context[
        COLD_SUCCESSOR_COMPLETION_EFFECT_KEY
    ]
    await db_session.refresh(project)
    await db_session.refresh(task)
    after_history = (
        await db_session.execute(
            select(TaskHistory.id).where(TaskHistory.task_id == task.id)
        )
    ).scalars().all()
    assert json.loads(json.dumps(project.vision)) == before_vision
    assert json.loads(json.dumps(task.context)) == before_context
    assert after_history == before_history

    stale = request.model_copy(update={"resident_incarnation_id": uuid.uuid4()})
    with pytest.raises(HTTPException) as rejected:
        await read_cold_successor_receipt(db_session, project.id, stale)
    assert rejected.value.status_code == 409
    assert rejected.value.detail == "Cold successor incarnation is stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_key", "corrupt_value"),
    [
        (COLD_SUCCESSOR_ATTESTATION_KEY, "corrupt"),
        (COLD_SUCCESSOR_ATTESTATION_KEY, ["corrupt"]),
        (COLD_SUCCESSOR_TERMINAL_KEY, "corrupt"),
        (COLD_SUCCESSOR_TERMINAL_KEY, ["corrupt"]),
    ],
)
async def test_cold_successor_malformed_durable_receipt_is_never_overwritten(
    db_session,
    monkeypatch,
    receipt_key,
    corrupt_value,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key=f"cold-corrupt-{receipt_key}-{type(corrupt_value).__name__}",
    )
    context = dict(task.context or {})
    context[receipt_key] = corrupt_value
    task.context = context
    db_session.add(task)
    await db_session.commit()
    before_context = json.loads(json.dumps(task.context))
    history_before = (
        await db_session.execute(
            select(TaskHistory.id).where(TaskHistory.task_id == task.id)
        )
    ).scalars().all()
    manifest = task.first_payload["cold_successor_contract"]["manifest"]
    if receipt_key == COLD_SUCCESSOR_ATTESTATION_KEY:
        request = ColdSuccessorAttestationRequest(
            **heartbeat.model_dump(),
            core_sha256=manifest["core"]["sha256"],
            problem_model_sha256=manifest["problem_model"]["sha256"],
            running_update_sha256=manifest["running_update"]["sha256"],
            strategic_memory_sha256=manifest["strategic_memory"]["sha256"],
            provider_terminal_reason="completed",
            provider_output_sha256="a" * 64,
            semantic_result="Loaded the durable strategic contract.",
        )
        mutation = attest_cold_successor(db_session, project.id, request)
        expected_detail = "Cold successor attestation receipt is malformed"
    else:
        request = ColdSuccessorTerminalRequest(
            **heartbeat.model_dump(),
            terminal_effect_id=uuid.uuid4(),
            status="FAILED",
            terminal_reason="internal_error",
            diagnostic_sha256="b" * 64,
        )
        mutation = terminal_cold_successor(db_session, project.id, request)
        expected_detail = "Cold successor terminal receipt is malformed"
    with pytest.raises(HTTPException) as rejected:
        await mutation
    assert rejected.value.status_code == 409
    assert rejected.value.detail == expected_detail

    with pytest.raises(HTTPException) as read_rejected:
        await read_cold_successor_receipt(
            db_session,
            project.id,
            ColdSuccessorReceiptRequest(**heartbeat.model_dump()),
        )
    assert read_rejected.value.status_code == 409
    assert read_rejected.value.detail == expected_detail
    await db_session.refresh(task)
    assert json.loads(json.dumps(task.context)) == before_context
    assert task.status == TaskStatus.IN_PROGRESS
    history_after = (
        await db_session.execute(
            select(TaskHistory.id).where(TaskHistory.task_id == task.id)
        )
    ).scalars().all()
    assert history_after == history_before


@pytest.mark.asyncio
async def test_cold_launch_is_force_fresh_idempotent_and_attested(
    db_session,
    monkeypatch,
):
    project, predecessor = await _ended_predecessor(db_session)
    runner_id = uuid.uuid4()
    monkeypatch.setattr(
        lifecycle,
        "_resolve_override_runner",
        AsyncMock(return_value=SimpleNamespace(id=runner_id)),
    )
    monkeypatch.setattr(lifecycle, "_resident_runner_is_available", lambda _runner: True)
    start = ColdSuccessorStartRequest(
        runner_id=runner_id,
        idempotency_key="cold-proof-v57",
        expected_predecessor_session_id="ss-ended-predecessor",
        expected_predecessor_task_id=predecessor.id,
        expected_predecessor_worker_generation=4,
    )

    launched = await launch_cold_successor(
        db_session,
        project.id,
        start,
        operator_user_id="owner",
    )
    task = await db_session.get(Task, uuid.UUID(launched["task_id"]))
    assert task is not None
    assert task.id != predecessor.id
    assert task.status == TaskStatus.PROVISIONING
    assert task.vm_instance_id == f"runner:{runner_id}"
    assert task.context[COLD_SUCCESSOR_CONTEXT_KEY] is True
    assert task.context["strategic_session_id"] == launched["session_id"]
    assert task.context["_resident_worker_generation"] == 1
    assert set(task.first_payload) == {
        "requires_browser",
        "cold_successor_contract",
        "type",
    }
    rendered = json.dumps({"payload": task.payloads, "context": task.context})
    for forbidden in (
        "FORBIDDEN_PREDECESSOR_SUMMARY",
        "FORBIDDEN_PREDECESSOR_DECISIONS",
        "FORBIDDEN_PREDECESSOR_EXAM",
        "FORBIDDEN_PREDECESSOR_CURSOR",
        "FORBIDDEN_BLOB_SUMMARY",
        "FORBIDDEN_BLOB_DECISIONS",
    ):
        assert forbidden not in rendered
    history = (
        await db_session.execute(
            select(TaskHistory).where(TaskHistory.task_id == task.id)
        )
    ).scalar_one()
    assert history.details["spawned_by"] == "COLD_SUCCESSOR"

    replay = await launch_cold_successor(
        db_session,
        project.id,
        start,
        operator_user_id="owner",
    )
    assert replay["task_id"] == launched["task_id"]
    with pytest.raises(HTTPException) as changed:
        await launch_cold_successor(
            db_session,
            project.id,
            start.model_copy(update={"idempotency_key": "different-effect"}),
            operator_user_id="owner",
        )
    assert changed.value.status_code == 409
    assert changed.value.detail == "Predecessor session identity changed"

    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()
    incarnation = uuid.uuid4()
    heartbeat = ColdSuccessorHeartbeatRequest(
        session_id=launched["session_id"],
        task_id=task.id,
        worker_generation=1,
        resident_incarnation_id=incarnation,
    )
    lease = await heartbeat_cold_successor(db_session, project.id, heartbeat)
    assert lease["authority_held"] is True
    assert lease["resident_incarnation_held"] is True

    manifest = task.first_payload["cold_successor_contract"]["manifest"]
    attestation = ColdSuccessorAttestationRequest(
        **heartbeat.model_dump(),
        core_sha256=manifest["core"]["sha256"],
        problem_model_sha256=manifest["problem_model"]["sha256"],
        running_update_sha256=manifest["running_update"]["sha256"],
        strategic_memory_sha256=manifest["strategic_memory"]["sha256"],
        provider_terminal_reason="completed",
        provider_output_sha256="a" * 64,
        semantic_result="Loaded the durable strategic contract.",
    )
    first = await attest_cold_successor(db_session, project.id, attestation)
    duplicate = await attest_cold_successor(db_session, project.id, attestation)
    assert first["status"] == "attested"
    assert duplicate == {"status": "duplicate", "attestation": first["attestation"]}
    await db_session.refresh(task)
    assert task.context[COLD_SUCCESSOR_ATTESTATION_KEY] == first["attestation"]
    terminal_effect_id = uuid.UUID(
        task.context[COLD_SUCCESSOR_COMPLETION_EFFECT_KEY]
    )
    assert first["attestation"]["session_id"] == heartbeat.session_id
    assert first["attestation"]["task_id"] == str(task.id)
    assert first["attestation"]["worker_generation"] == 1
    assert first["attestation"]["resident_incarnation_id"] == str(incarnation)
    assert first["attestation"]["completion_terminal_effect_id"] == str(
        terminal_effect_id
    )
    receipt_request = ColdSuccessorReceiptRequest(**heartbeat.model_dump())
    attested_receipt = await read_cold_successor_receipt(
        db_session,
        project.id,
        receipt_request,
    )
    assert attested_receipt["receipt_state"] == "attested"
    assert attested_receipt["attestation"] == first["attestation"]

    with pytest.raises(HTTPException) as mismatched:
        await attest_cold_successor(
            db_session,
            project.id,
            attestation.model_copy(update={"core_sha256": "b" * 64}),
        )
    assert mismatched.value.status_code == 409
    assert mismatched.value.detail == "Cold successor hashes do not match"

    downgrade = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=uuid.uuid4(),
        status="FAILED",
        terminal_reason="internal_error",
        diagnostic_sha256="c" * 64,
    )
    with pytest.raises(HTTPException) as rejected_downgrade:
        await terminal_cold_successor(db_session, project.id, downgrade)
    assert rejected_downgrade.value.status_code == 409
    assert rejected_downgrade.value.detail == (
        "Cold successor attestation cannot be downgraded"
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert COLD_SUCCESSOR_TERMINAL_KEY not in task.context

    terminal = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=terminal_effect_id,
        status="COMPLETED",
        terminal_reason="completed",
    )
    with pytest.raises(HTTPException) as unauthorized_effect:
        await terminal_cold_successor(
            db_session,
            project.id,
            terminal.model_copy(update={"terminal_effect_id": uuid.uuid4()}),
        )
    assert unauthorized_effect.value.status_code == 409
    assert unauthorized_effect.value.detail == (
        "Cold successor completion effect is not server-authorized"
    )
    accepted = await terminal_cold_successor(db_session, project.id, terminal)
    duplicate_terminal = await terminal_cold_successor(db_session, project.id, terminal)
    assert accepted["status"] == "accepted"
    assert duplicate_terminal["status"] == "duplicate"
    with pytest.raises(HTTPException) as changed_terminal:
        await terminal_cold_successor(
            db_session,
            project.id,
            terminal.model_copy(
                update={
                    "status": "FAILED",
                    "terminal_reason": "internal_error",
                    "diagnostic_sha256": "c" * 64,
                }
            ),
        )
    assert changed_terminal.value.status_code == 409
    assert changed_terminal.value.detail == (
        "Cold successor attestation cannot be downgraded"
    )
    duplicate_attestation = await attest_cold_successor(
        db_session, project.id, attestation
    )
    assert duplicate_attestation == {
        "status": "duplicate",
        "attestation": first["attestation"],
    }
    with pytest.raises(HTTPException) as ended_heartbeat:
        await heartbeat_cold_successor(db_session, project.id, heartbeat)
    assert ended_heartbeat.value.status_code == 409
    assert ended_heartbeat.value.detail == "Cold successor is not active"
    status = await cold_successor_status(db_session, project.id)
    assert status["proof_state"] == "succeeded"
    assert status["task_status"] == "COMPLETED"
    assert status["terminal"]["terminal_effect_id"] == str(terminal_effect_id)
    terminal_receipt = await read_cold_successor_receipt(
        db_session,
        project.id,
        receipt_request.model_copy(
            update={"resident_incarnation_id": uuid.uuid4()}
        ),
    )
    assert terminal_receipt["receipt_state"] == "terminal"
    assert terminal_receipt["terminal"] == status["terminal"]
    await db_session.refresh(task)
    assert task.completed_at is not None
    assert task.vm_stopped_at is not None
    terminal_history = (
        await db_session.execute(
            select(TaskHistory).where(
                TaskHistory.task_id == task.id,
                TaskHistory.action == "COLD_SUCCESSOR_TERMINAL",
            )
        )
    ).scalars().all()
    assert len(terminal_history) == 1
    await db_session.refresh(project)
    assert project.paused is True
    assert project.autonomous_enabled is False
    assert project.vision["unrelated"] == "PRESERVE_ME"
    assert project.vision["_strategic_failures"] == 2
    assert project.vision[RESIDENT_SESSION_KEY]["status"] == "ended"


@pytest.mark.asyncio
async def test_cold_terminal_without_attestation_is_durable_failure(
    db_session,
    monkeypatch,
):
    project, predecessor = await _ended_predecessor(db_session)
    runner_id = uuid.uuid4()
    monkeypatch.setattr(
        lifecycle,
        "_resolve_override_runner",
        AsyncMock(return_value=SimpleNamespace(id=runner_id)),
    )
    monkeypatch.setattr(lifecycle, "_resident_runner_is_available", lambda _runner: True)
    launched = await launch_cold_successor(
        db_session,
        project.id,
        ColdSuccessorStartRequest(
            runner_id=runner_id,
            idempotency_key="cold-proof-failure",
            expected_predecessor_session_id="ss-ended-predecessor",
            expected_predecessor_task_id=predecessor.id,
            expected_predecessor_worker_generation=4,
        ),
        operator_user_id="owner",
    )
    task = await db_session.get(Task, uuid.UUID(launched["task_id"]))
    task.status = TaskStatus.FAILED
    db_session.add(task)
    await db_session.commit()

    status = await cold_successor_status(db_session, project.id)

    assert status["proof_state"] == "failed"
    assert status["failure_reason"] == "terminal_without_exact_attestation"
    await db_session.refresh(project)
    assert project.vision["_strategic_failures"] == 2


@pytest.mark.asyncio
async def test_attested_out_of_band_completion_without_terminal_receipt_fails(
    db_session,
    monkeypatch,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-attested-without-terminal-receipt",
    )
    manifest = task.first_payload["cold_successor_contract"]["manifest"]
    attestation = ColdSuccessorAttestationRequest(
        **heartbeat.model_dump(),
        core_sha256=manifest["core"]["sha256"],
        problem_model_sha256=manifest["problem_model"]["sha256"],
        running_update_sha256=manifest["running_update"]["sha256"],
        strategic_memory_sha256=manifest["strategic_memory"]["sha256"],
        provider_terminal_reason="completed",
        provider_output_sha256="9" * 64,
        semantic_result="Loaded exact durable authority.",
    )
    await attest_cold_successor(db_session, project.id, attestation)
    await db_session.refresh(task)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    status = await cold_successor_status(db_session, project.id)

    assert status["proof_state"] == "failed"
    assert status["failure_reason"] == "terminal_without_exact_dedicated_receipt"
    assert status["attestation"] is not None
    assert status["terminal"] is None


@pytest.mark.asyncio
async def test_dedicated_terminal_requires_attestation_and_accepts_exact_failure(
    db_session,
    monkeypatch,
):
    project, predecessor = await _ended_predecessor(db_session)
    runner_id = uuid.uuid4()
    monkeypatch.setattr(
        lifecycle,
        "_resolve_override_runner",
        AsyncMock(return_value=SimpleNamespace(id=runner_id)),
    )
    monkeypatch.setattr(lifecycle, "_resident_runner_is_available", lambda _runner: True)
    launched = await launch_cold_successor(
        db_session,
        project.id,
        ColdSuccessorStartRequest(
            runner_id=runner_id,
            idempotency_key="cold-proof-dedicated-failure",
            expected_predecessor_session_id="ss-ended-predecessor",
            expected_predecessor_task_id=predecessor.id,
            expected_predecessor_worker_generation=4,
        ),
        operator_user_id="owner",
    )
    task = await db_session.get(Task, uuid.UUID(launched["task_id"]))
    assert task is not None
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()
    heartbeat = ColdSuccessorHeartbeatRequest(
        session_id=launched["session_id"],
        task_id=task.id,
        worker_generation=1,
        resident_incarnation_id=uuid.uuid4(),
    )
    await heartbeat_cold_successor(db_session, project.id, heartbeat)
    effect_id = uuid.uuid4()
    completion = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=effect_id,
        status="COMPLETED",
        terminal_reason="completed",
    )
    with pytest.raises(HTTPException) as unattested:
        await terminal_cold_successor(db_session, project.id, completion)
    assert unattested.value.status_code == 409
    assert unattested.value.detail == (
        "Cold successor completion requires exact attestation"
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS

    with pytest.raises(
        ValueError,
        match="failed terminal effects require a failure reason and diagnostic",
    ):
        ColdSuccessorTerminalRequest(
            **heartbeat.model_dump(),
            terminal_effect_id=effect_id,
            status="FAILED",
            terminal_reason="provider_terminal_failed",
        )

    failure = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=effect_id,
        status="FAILED",
        terminal_reason="provider_terminal_failed",
        diagnostic_sha256="d" * 64,
    )
    accepted = await terminal_cold_successor(db_session, project.id, failure)
    duplicate = await terminal_cold_successor(db_session, project.id, failure)
    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    status = await cold_successor_status(db_session, project.id)
    assert status["proof_state"] == "failed"
    assert status["task_status"] == "FAILED"
    assert status["failure_reason"] == "provider_terminal_failed"
    assert status["terminal"]["diagnostic_sha256"] == "d" * 64


@pytest.mark.asyncio
async def test_cold_terminal_releases_slot_only_after_durable_commit(
    db_session,
    monkeypatch,
):
    project, _task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-slot-commit-fence",
    )
    monkeypatch.setattr(lifecycle, "get_task_llm_account_id", lambda _task: "acct")
    release = AsyncMock()
    monkeypatch.setattr(lifecycle, "release_max_plan_account", release)
    project_id = project.id
    terminal = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=uuid.uuid4(),
        status="FAILED",
        terminal_reason="internal_error",
        diagnostic_sha256="e" * 64,
    )
    original_commit = db_session.commit
    failed = False

    async def fail_first_commit():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic terminal commit failure")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", fail_first_commit)
    with pytest.raises(RuntimeError, match="synthetic terminal commit failure"):
        await terminal_cold_successor(db_session, project_id, terminal)
    release.assert_not_awaited()
    await db_session.rollback()

    accepted = await terminal_cold_successor(db_session, project_id, terminal)
    duplicate = await terminal_cold_successor(db_session, project_id, terminal)
    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    release.assert_awaited_once_with("acct", project_id=str(project_id))


@pytest.mark.asyncio
async def test_cold_terminal_release_failure_keeps_committed_receipt(
    db_session,
    monkeypatch,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-slot-release-failure",
    )
    monkeypatch.setattr(lifecycle, "get_task_llm_account_id", lambda _task: "acct")
    release = AsyncMock(side_effect=RuntimeError("synthetic pool release failure"))
    monkeypatch.setattr(lifecycle, "release_max_plan_account", release)
    project_id = project.id
    terminal = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=uuid.uuid4(),
        status="FAILED",
        terminal_reason="internal_error",
        diagnostic_sha256="f" * 64,
    )

    accepted = await terminal_cold_successor(db_session, project_id, terminal)
    duplicate = await terminal_cold_successor(db_session, project_id, terminal)

    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    release.assert_awaited_once_with("acct", project_id=str(project_id))
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["_slot_released"] is True
    assert task.context["_cold_successor_terminal"]["terminal_effect_id"] == str(
        terminal.terminal_effect_id
    )


@pytest.mark.asyncio
async def test_cold_successor_terminal_http_allows_same_vm_inactive_duplicate(
    client,
    db_session,
    monkeypatch,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-http-terminal-replay",
    )
    project_id = project.id
    task_id = task.id
    token = create_agent_token(
        str(task_id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project_id),
    )
    terminal = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=uuid.uuid4(),
        status="FAILED",
        terminal_reason="provider_terminal_failed",
        diagnostic_sha256="1" * 64,
    )
    url = (
        f"/api/agent/projects/{project_id}/strategic-session/"
        "cold-successor/terminal"
    )
    headers = {"X-Agent-Token": token}

    accepted = await client.post(
        url,
        headers=headers,
        json=terminal.model_dump(mode="json"),
    )
    duplicate = await client.post(
        url,
        headers=headers,
        json=terminal.model_dump(mode="json"),
    )
    receipt_url = (
        f"/api/agent/projects/{project_id}/strategic-session/"
        "cold-successor/receipt"
    )
    receipt = await client.get(
        receipt_url,
        headers=headers,
        params=heartbeat.model_dump(mode="json"),
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["receipt_state"] == "terminal"
    assert receipt.json()["terminal"]["terminal_effect_id"] == str(
        terminal.terminal_effect_id
    )
    histories = (
        await db_session.execute(
            select(TaskHistory).where(
                TaskHistory.task_id == task_id,
                TaskHistory.action == "COLD_SUCCESSOR_TERMINAL",
            )
        )
    ).scalars().all()
    assert len(histories) == 1


@pytest.mark.asyncio
async def test_cold_successor_http_rejects_malformed_sub_and_wrong_vm(
    client,
    db_session,
    monkeypatch,
):
    project, task, heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-http-auth-reject",
    )
    project_id = project.id
    task_id = task.id
    terminal = ColdSuccessorTerminalRequest(
        **heartbeat.model_dump(),
        terminal_effect_id=uuid.uuid4(),
        status="FAILED",
        terminal_reason="internal_error",
        diagnostic_sha256="2" * 64,
    )
    url = (
        f"/api/agent/projects/{project_id}/strategic-session/"
        "cold-successor/terminal"
    )
    malformed = create_agent_token(
        "not-a-uuid",
        task.vm_instance_id,
        project_id=str(project_id),
    )
    wrong_vm = create_agent_token(
        str(task_id),
        "runner:wrong",
        project_id=str(project_id),
    )

    malformed_response = await client.post(
        url,
        headers={"X-Agent-Token": malformed},
        json=terminal.model_dump(mode="json"),
    )
    wrong_vm_response = await client.post(
        url,
        headers={"X-Agent-Token": wrong_vm},
        json=terminal.model_dump(mode="json"),
    )

    assert malformed_response.status_code == 401, malformed_response.text
    assert malformed_response.json()["error"]["message"] == "Task is no longer active"
    assert wrong_vm_response.status_code == 403, wrong_vm_response.text
    assert wrong_vm_response.json()["error"]["message"] == "vm_instance_mismatch"
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert "_cold_successor_terminal" not in task.context


@pytest.mark.asyncio
async def test_cold_successor_generic_status_fails_closed(
    client,
    db_session,
    monkeypatch,
):
    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-generic-status-fence",
    )
    project_id = project.id
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project_id),
    )
    release = AsyncMock()
    monkeypatch.setattr(
        "app.routers.agent_status.release_max_plan_account",
        release,
    )

    response = await client.post(
        "/api/agent/status",
        headers={"X-Agent-Token": token},
        json={"task_id": str(task.id), "status": "COMPLETED"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == (
        "Cold successor task path is not allowed"
    )
    release.assert_not_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert "_cold_successor_terminal" not in task.context


@pytest.mark.asyncio
async def test_cold_successor_jwt_cannot_escape_through_direct_token_surfaces(
    client,
    db_session,
    monkeypatch,
):
    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-direct-token-fences",
    )
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project.id),
    )
    task.context = {
        **task.context,
        "viewport_failure_screenshot_uri": (
            f"gs://bucket/viewport-failures/{project.id}/ColdProof-768.png"
        ),
    }
    db_session.add(task)
    await db_session.commit()
    signer = AsyncMock(return_value="must-not-sign")
    digest_probe = Mock(return_value={"must_not": "read"})
    snapshot_probe = AsyncMock(return_value=[{"must_not": "read"}])
    monkeypatch.setattr("app.storage.generate_download_signed_url", signer)
    monkeypatch.setattr(
        "app.digest_latency.get_digest_latency_snapshot",
        digest_probe,
    )
    monkeypatch.setattr(
        "app.efficiency_snapshot._read_snapshot_history",
        snapshot_probe,
    )

    responses = [
        await client.get(
            "/api/agent/efficiency-snapshot/format-compliance",
            headers={"x-agent-token": token},
        ),
        await client.get(
            "/api/admin/viewport-failures/download-url",
            params={
                "project_id": str(project.id),
                "component": "ColdProof",
                "width": 768,
            },
            headers={"x-agent-token": token},
        ),
        await client.get(
            "/api/admin/viewport-shots/download-url",
            params={
                "project_id": str(project.id),
                "task_id": str(task.id),
                "name": "cold-proof",
            },
            headers={"x-agent-token": token},
        ),
        await client.get(
            "/api/admin/digest-latency-stats",
            headers={"authorization": f"Bearer {token}"},
        ),
    ]
    for response in responses:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["message"] == (
            "Cold successor task path is not allowed"
        )
    signer.assert_not_awaited()
    digest_probe.assert_not_called()
    snapshot_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_successor_jwt_closes_websocket_before_any_data_surface(
    db_session,
    monkeypatch,
):
    import app.routers.projects as projects_router

    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-websocket-fence",
    )
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project.id),
    )
    session_context = AsyncMock()
    session_context.__aenter__.return_value = db_session
    session_context.__aexit__.return_value = None
    monkeypatch.setattr(
        projects_router,
        "AsyncSessionLocal",
        Mock(return_value=session_context),
    )
    connect = AsyncMock(return_value=True)
    broadcast = AsyncMock()
    strategic_delta = AsyncMock()
    conversation_delta = AsyncMock()
    monkeypatch.setattr(projects_router.manager, "check_and_add_ip", connect)
    monkeypatch.setattr(projects_router, "broadcast_to_project", broadcast)
    monkeypatch.setattr(
        projects_router,
        "emit_strategic_session_delta",
        strategic_delta,
    )
    monkeypatch.setattr(
        projects_router,
        "_broadcast_conversation_message_delta",
        conversation_delta,
    )
    websocket = AsyncMock()
    websocket.receive_json = AsyncMock(return_value={"token": token})
    websocket.receive_text = AsyncMock(return_value='{"type":"log"}')
    websocket.close = AsyncMock()

    await projects_router.agent_websocket_endpoint(websocket, task.id)

    websocket.accept.assert_awaited_once()
    websocket.close.assert_awaited_once_with(
        code=1008,
        reason="Authentication failed",
    )
    websocket.receive_text.assert_not_awaited()
    connect.assert_not_awaited()
    broadcast.assert_not_awaited()
    strategic_delta.assert_not_awaited()
    conversation_delta.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_successor_http_denies_all_repository_credentials(
    client,
    db_session,
    monkeypatch,
):
    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-repository-authority-fence",
    )
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project.id),
    )
    installation_token = AsyncMock(return_value="must-not-mint")
    read_token = AsyncMock(return_value="must-not-mint")
    monkeypatch.setattr(
        "app.github.get_repo_installation_token",
        installation_token,
    )
    monkeypatch.setattr(
        "app.github_auth.get_repo_installation_read_token",
        read_token,
    )

    proxy_response = await client.get(
        "/api/agent/github/token",
        params={"task_id": str(task.id)},
        headers={"X-Agent-Token": token},
    )
    read_response = await client.get(
        "/api/agent/git-read-token",
        headers={"X-Agent-Token": token},
    )

    for response in (proxy_response, read_response):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["message"] == (
            "Cold successor task path is not allowed"
        )
    installation_token.assert_not_awaited()
    read_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_successor_claim_is_exact_minimal_durable_and_replayable(
    client,
    db_session,
    monkeypatch,
):
    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-minimal-claim",
    )
    task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    expected_payload = json.loads(json.dumps(task.first_payload))
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_review",
        project_id=str(project.id),
    )
    generic_backfill = AsyncMock()
    generic_retry_clear = AsyncMock()
    broadcast = AsyncMock()
    recorded_vm_ids = []
    timing_instances = []

    class TimingProbe:
        def __init__(self):
            self.marks = []
            self.responses = []
            timing_instances.append(self)

        def mark(self, phase):
            self.marks.append(phase)

        def finish(self, response):
            self.responses.append(response)
            return {
                "total_ms": 1,
                "phase_ms": dict.fromkeys(self.marks, 0),
                "response_bytes_approx": 1,
                "size_measurement_ms": 0,
            }

    monkeypatch.setattr(
        "app.routers.agent.backfill_artifact_prerequisites",
        generic_backfill,
    )
    monkeypatch.setattr(
        "app.strategic_resident_session.clear_resident_worker_retry_wait",
        generic_retry_clear,
    )
    monkeypatch.setattr("app.routers.agent.broadcast_to_project", broadcast)
    monkeypatch.setattr("app.routers.agent.ClaimResponseTiming", TimingProbe)
    monkeypatch.setattr(
        "app.shared_state.record_vm_heartbeat",
        recorded_vm_ids.append,
    )

    first = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": token},
    )
    replay = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": token},
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    expected = {
        "task_id": str(task.id),
        "payloads": [expected_payload],
        "context": {
            "_cold_successor": True,
            "strategic_session_id": task.context["strategic_session_id"],
            "_resident_worker_generation": task.context[
                "_resident_worker_generation"
            ],
        },
        "project_metadata": {"project_id": str(project.id)},
        "task_capability": {"workspace_requirement": "none"},
    }
    assert first.json() == expected
    assert replay.json() == expected
    assert list(first.json()) == [
        "task_id",
        "payloads",
        "context",
        "project_metadata",
        "task_capability",
    ]
    generic_backfill.assert_not_awaited()
    generic_retry_clear.assert_not_awaited()
    assert recorded_vm_ids == [task.vm_instance_id, task.vm_instance_id]
    assert [entry.args for entry in broadcast.await_args_list] == [
        (
            str(project.id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
        ),
        (
            str(project.id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
        ),
    ]
    assert all(
        instance.marks == ["claim_transition", "execution_contract"]
        for instance in timing_instances
    )
    assert [instance.responses for instance in timing_instances] == [[expected], [expected]]
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.context["_agent_claim_replay"]["token_jti"]


@pytest.mark.asyncio
async def test_cold_successor_jwt_is_centrally_fenced_to_exact_boot_protocol(
    db_session,
    monkeypatch,
):
    project, task, _heartbeat = await _launch_active_cold_successor(
        db_session,
        monkeypatch,
        key="cold-central-jwt-fence",
    )
    base = (
        f"/api/agent/projects/{project.id}/strategic-session/"
        "cold-successor"
    )

    for method, path in (
        ("POST", "/api/agent/claim"),
        ("POST", "/api/agent/refresh-token"),
        ("POST", f"{base}/heartbeat"),
        ("POST", f"{base}/attest"),
        ("GET", f"{base}/receipt"),
        ("POST", f"{base}/terminal"),
    ):
        assert enforce_cold_successor_request_authority(
            task,
            method=method,
            path=path,
        ) is True

    for method, path in (
        ("GET", "/api/agent/task-capabilities"),
        ("POST", "/api/agent/heartbeat"),
        ("POST", "/api/agent/status"),
        ("POST", "/api/agent/spawn"),
        ("GET", "/api/agent/github/token"),
        ("GET", f"{base}/heartbeat"),
        ("POST", f"{base}/receipt"),
        ("POST", f"{base}/attest/extra"),
        (
            "POST",
            f"/api/agent/projects/{uuid.uuid4()}/strategic-session/"
            "cold-successor/heartbeat",
        ),
    ):
        with pytest.raises(HTTPException) as rejected:
            enforce_cold_successor_request_authority(
                task,
                method=method,
                path=path,
            )
        assert rejected.value.status_code == 403

    malformed = SimpleNamespace(
        first_payload=task.first_payload,
        context={**task.context, "_cold_successor_core_sha256": "broken"},
        vm_instance_id=task.vm_instance_id,
        project_id=task.project_id,
    )
    assert enforce_cold_successor_request_authority(
        malformed,
        method="POST",
        path="/api/agent/claim",
    ) is True
    with pytest.raises(HTTPException) as malformed_rejected:
        enforce_cold_successor_request_authority(
            malformed,
            method="POST",
            path="/api/agent/refresh-token",
        )
    assert malformed_rejected.value.status_code == 403
