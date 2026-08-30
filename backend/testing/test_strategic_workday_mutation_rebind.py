"""Focused unit tests for receipt-bound Workday operation deduplication."""

import copy
import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Direction,
    KnowledgeDocument,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    ProjectMessage,
    ReleaseQualificationIntent,
    ReleaseVersion,
    SourceKind,
    Task,
    VersionAcceptanceCriterion,
)
from app.routers.admin_agent_ops import STRATEGIC_AGENT_OPS
from app.strategic_consultation_detail import project_consultation_detail
from app.time_utils import _utcnow
from app.strategic_autonomous_handoff import AUTONOMOUS_HANDOFF_OPS
import app.strategic_workday_mutation_rebind as mutation_rebind
from app.strategic_workday_mutation_rebind import (
    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_LIMIT,
    WORKDAY_RECEIPT_BOUND_OPS,
    WorkdayConsultationOperationBinding,
    authoritative_workday_operation_receipts,
    authoritative_workday_result_receipts,
    bounded_workday_mutation_receipts,
    find_workday_operation_replay,
    workday_operation_replay_response,
)
from testing.test_strategic_session_messages import (
    _activate_typed_fast_worker,
    _add_typed_fast_input,
    _headers,
    _make_resident_poll_worker,
    _typed_fast_claim_payload,
)


def _receipt(*, index: int, packet_id: uuid.UUID, task_id: uuid.UUID) -> dict:
    return {
        "version": 1,
        "packet_id": str(packet_id),
        "workday_claim_id": "00000000-0000-0000-0000-000000000002",
        "operation_id": str(uuid.UUID(int=index + 10)),
        "fast_claim_id": "00000000-0000-0000-0000-000000000003",
        "task_id": str(task_id),
        "worker_incarnation": "00000000-0000-0000-0000-000000000004",
        "incarnation_id": "00000000-0000-0000-0000-000000000004",
        "envelope_snapshot_id": "00000000-0000-0000-0000-000000000005",
        "resident_session_id": "resident-session",
        "session_id": "resident-session",
        "worker_generation": 1,
        "context_authority_generation_before": 1,
        "context_authority_generation_after": 1,
        "privacy_generation": 1,
        "op": "create_direction",
        "operation_fingerprint": f"{index + 1:064x}",
        "recorded_at": "2026-08-15T00:00:00+00:00",
        "entity_kind": "direction",
        "entity_id": f"direction-{index}",
        "effect": "created",
        "accepted_at": "2026-08-15T00:00:00+00:00",
    }


def _binding(
    *, packet: ProjectMessage, task: Task, operation_id: uuid.UUID
) -> WorkdayConsultationOperationBinding:
    return WorkdayConsultationOperationBinding(
        packet=packet,
        task=task,
        packet_id=packet.id,
        workday_claim_id=uuid.UUID(int=2),
        operation_id=operation_id,
        fast_claim_id=uuid.UUID(int=3),
        task_id=task.id,
        resident_session_id="resident-session",
        worker_generation=1,
        worker_incarnation=uuid.UUID(int=4),
        input_revision=1,
        transcript_sequence=1,
        envelope_snapshot_id=uuid.UUID(int=5),
        context_authority_generation_before=1,
        privacy_generation=1,
    )


def test_workday_operation_replay_survives_projection_window_and_is_packet_scoped():
    project_id = uuid.uuid4()
    task = Task(id=uuid.uuid4(), project_id=project_id, payloads=[{}])
    packet_id = uuid.uuid4()
    ledger = [
        _receipt(index=index, packet_id=packet_id, task_id=task.id)
        for index in range(WORKDAY_OPERATION_LEDGER_LIMIT)
    ]
    packet = ProjectMessage(
        id=packet_id,
        project_id=project_id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        content="private",
        context={WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: ledger},
    )
    before = copy.deepcopy(packet.context)
    oldest = ledger[0]
    binding = _binding(
        packet=packet,
        task=task,
        operation_id=uuid.UUID(oldest["operation_id"]),
    )

    assert find_workday_operation_replay(
        binding,
        op="create_direction",
        operation_fingerprint=oldest["operation_fingerprint"],
    ) == oldest
    assert packet.context == before
    with pytest.raises(HTTPException) as changed:
        find_workday_operation_replay(
            binding,
            op="create_direction",
            operation_fingerprint="f" * 64,
        )
    assert changed.value.status_code == 409
    assert "different operation" in changed.value.detail

    with pytest.raises(HTTPException) as full:
        find_workday_operation_replay(
            _binding(packet=packet, task=task, operation_id=uuid.uuid4()),
            op="create_direction",
            operation_fingerprint="e" * 64,
        )
    assert full.value.status_code == 409
    assert "capacity is exhausted" in full.value.detail

    next_packet = ProjectMessage(
        id=uuid.uuid4(),
        project_id=project_id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        content="next private consultation",
        context={},
    )
    assert find_workday_operation_replay(
        _binding(packet=next_packet, task=task, operation_id=uuid.uuid4()),
        op="create_direction",
        operation_fingerprint="d" * 64,
    ) is None

    wrong_packet_receipt = copy.deepcopy(oldest)
    wrong_packet_receipt["packet_id"] = str(next_packet.id)
    corrupt_packet = ProjectMessage(
        id=packet_id,
        project_id=project_id,
        sequence=3,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        content="cross-packet history must fail closed",
        context={WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [wrong_packet_receipt]},
    )
    with pytest.raises(HTTPException) as corrupt:
        find_workday_operation_replay(
            _binding(
                packet=corrupt_packet,
                task=task,
                operation_id=uuid.uuid4(),
            ),
            op="create_direction",
            operation_fingerprint="c" * 64,
        )
    assert corrupt.value.status_code == 409
    assert "history is invalid" in corrupt.value.detail


def test_authoritative_receipt_ledgers_reject_duplicate_operation_ids():
    packet_id = uuid.uuid4()
    task_id = uuid.uuid4()
    first = _receipt(index=1, packet_id=packet_id, task_id=task_id)
    contradictory = copy.deepcopy(first)
    contradictory["operation_fingerprint"] = "f" * 64
    contradictory["entity_id"] = "different-direction"

    for key, validator in (
        (WORKDAY_OPERATION_LEDGER_CONTEXT_KEY, authoritative_workday_operation_receipts),
        (WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY, authoritative_workday_result_receipts),
    ):
        with pytest.raises(HTTPException) as duplicate:
            validator({key: [first, contradictory]}, packet_id=packet_id)
        assert duplicate.value.status_code == 409
        assert "history is invalid" in duplicate.value.detail


def test_authoritative_receipt_ledgers_reject_divergent_semantic_effects():
    packet_id = uuid.uuid4()
    task_id = uuid.uuid4()
    first = _receipt(index=1, packet_id=packet_id, task_id=task_id)
    contradictory = _receipt(index=2, packet_id=packet_id, task_id=task_id)
    contradictory["operation_fingerprint"] = first["operation_fingerprint"]
    contradictory["entity_id"] = "different-direction"

    for key, validator in (
        (WORKDAY_OPERATION_LEDGER_CONTEXT_KEY, authoritative_workday_operation_receipts),
        (WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY, authoritative_workday_result_receipts),
    ):
        with pytest.raises(HTTPException) as ambiguous:
            validator({key: [first, contradictory]}, packet_id=packet_id)
        assert ambiguous.value.status_code == 409
        assert "history is invalid" in ambiguous.value.detail


def test_retry_with_new_claim_incarnation_and_operation_id_replays_exact_semantics():
    project_id = uuid.uuid4()
    task = Task(id=uuid.uuid4(), project_id=project_id, payloads=[{}])
    packet_id = uuid.uuid4()
    receipt = _receipt(index=1, packet_id=packet_id, task_id=task.id)
    receipt.update(
        task_id=str(uuid.uuid4()),
        resident_session_id="retired-resident-session",
        session_id="retired-resident-session",
        worker_generation=0,
        worker_incarnation=str(uuid.uuid4()),
        incarnation_id=str(uuid.uuid4()),
    )
    receipt["incarnation_id"] = receipt["worker_incarnation"]
    packet = ProjectMessage(
        id=packet_id,
        project_id=project_id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        content="private retry",
        context={WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [receipt]},
    )
    retry = _binding(packet=packet, task=task, operation_id=uuid.uuid4())
    retry = WorkdayConsultationOperationBinding(
        **{
            **retry.__dict__,
            "workday_claim_id": uuid.uuid4(),
            "worker_incarnation": uuid.uuid4(),
        }
    )

    assert find_workday_operation_replay(
        retry,
        op=receipt["op"],
        operation_fingerprint=receipt["operation_fingerprint"],
    ) == receipt
    assert find_workday_operation_replay(
        retry,
        op=receipt["op"],
        operation_fingerprint="a" * 64,
    ) is None


def test_version_and_acceptance_criterion_receipts_are_bounded_and_projectable():
    packet_id = uuid.uuid4()
    task_id = uuid.uuid4()
    goal = _receipt(index=2, packet_id=packet_id, task_id=task_id)
    goal.update(
        op="set_version_goal",
        target_kind="version_goal",
        target_revision=56,
        target_sha256="b" * 64,
    )
    for key in ("entity_kind", "entity_id", "effect", "accepted_at"):
        goal.pop(key)
    criterion = _receipt(index=3, packet_id=packet_id, task_id=task_id)
    criterion.update(
        op="create_acceptance_criterion",
        entity_kind="acceptance_criterion",
        entity_id="ac-criterion-key",
    )
    context = {WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: [goal, criterion]}

    assert bounded_workday_mutation_receipts(context, packet_id=packet_id) == [
        goal,
        criterion,
    ]
    assert workday_operation_replay_response(criterion)["criterion_key"] == (
        "ac-criterion-key"
    )


@pytest.mark.asyncio
async def test_private_strategic_operation_inventory_fails_closed_without_receipts(
    monkeypatch,
):
    """Every private strategic mutation is receipted or rejected pre-write."""

    async def _bound(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(
        mutation_rebind,
        "prepare_workday_consultation_operation_binding",
        _bound,
    )
    unsupported = (
        STRATEGIC_AGENT_OPS
        - WORKDAY_RECEIPT_BOUND_OPS
        - AUTONOMOUS_HANDOFF_OPS
    )
    assert unsupported
    for op in sorted(unsupported):
        with pytest.raises(HTTPException) as rejected:
            await mutation_rebind.prepare_bound_workday_operation(
                object(),
                project=object(),
                task=object(),
                resident_incarnation_id=uuid.uuid4(),
                packet_id=uuid.uuid4(),
                workday_claim_id=uuid.uuid4(),
                operation_id=uuid.uuid4(),
                op=op,
                data={"probe": op},
            )
        assert rejected.value.status_code == 409
        assert "not receipt-bound" in rejected.value.detail


async def _start_receipt_bound_workday_packet(client, db_session):
    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    turn_claim = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task,
            incarnation,
            inbound.id,
            client_claim_id=str(uuid.uuid4()),
        ),
    )
    assert turn_claim.status_code == 200, turn_claim.text
    identity = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
    }
    turn_complete = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "claim_id": turn_claim.json()["claim_id"],
            "owner": "fast",
            "content": "I will update the release contract privately.",
            "idempotency_key": f"criteria-receipt-public:{uuid.uuid4()}",
            "envelope": turn_claim.json()["envelope"],
            "workday_request": {
                "delivery_key": f"criteria-receipt:{uuid.uuid4()}",
                "question": "Create one measurable acceptance criterion.",
            },
        },
    )
    assert turn_complete.status_code == 200, turn_complete.text
    packet = await db_session.get(
        ProjectMessage,
        uuid.UUID(turn_complete.json()["workday_packet"]["packet_id"]),
    )
    assert packet is not None
    return project, task, identity, packet


async def _claim_receipt_bound_workday_packet(
    client,
    *,
    project,
    task,
    identity,
):
    claimed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert claimed.status_code == 200, claimed.text
    return claimed.json()["packet"]["workday_claim_id"]


@pytest.mark.asyncio
async def test_handoff_cleanup_requires_exact_two_head_receipt_and_replays(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    heads = [
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            source_kind=SourceKind.STRATEGIC_MEMORY.value,
            title=f"Legacy handoff {index}",
            summary=f"Legacy handoff {index}",
            body=f"Durable predecessor checkpoint {index}.",
            tags=["handoff"],
            lifecycle_status="consumed",
        )
        for index in range(2)
    ]
    ordinary = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.UNKNOWN.value,
        title="Ordinary knowledge",
        summary="Ordinary knowledge",
        body="Private Workday must not gain ambient dismissal authority.",
        tags=[],
        lifecycle_status="consumed",
    )
    db_session.add_all([task, ordinary, *heads])
    await db_session.commit()
    heads.sort(key=lambda item: str(item.id))
    target, keeper = heads
    expected_head_ids = [str(item.id) for item in heads]
    data = {
        "knowledge_document_id": str(target.id),
        "reason": "Retire the stale predecessor after exact reconciliation.",
        "expected_handoff_head_ids": expected_head_ids,
        "superseded_by_id": str(keeper.id),
    }

    direct = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "resident_incarnation_id": identity["incarnation_id"],
            "op": "dismiss_knowledge",
            "data": data,
        },
    )
    assert direct.status_code == 409, direct.text
    assert "requires Workday receipt authority" in direct.text

    workday_claim_id = await _claim_receipt_bound_workday_packet(
        client,
        project=project,
        task=task,
        identity=identity,
    )
    request = {
        "task_id": str(task.id),
        "resident_incarnation_id": identity["incarnation_id"],
        "packet_id": str(packet.id),
        "workday_claim_id": workday_claim_id,
        "workday_operation_id": str(uuid.uuid4()),
        "op": "dismiss_knowledge",
        "data": data,
    }

    private_ordinary = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "data": {
                "knowledge_document_id": str(ordinary.id),
                "reason": "Must remain direct-only.",
            },
        },
    )
    assert private_ordinary.status_code == 409, private_ordinary.text
    await db_session.refresh(ordinary)
    assert ordinary.lifecycle_status == "consumed"

    wrong_set = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "data": {
                **data,
                "expected_handoff_head_ids": sorted(
                    [str(target.id), str(uuid.uuid4())]
                ),
            },
        },
    )
    assert wrong_set.status_code == 409, wrong_set.text
    invalid_types = [
        {**data, "reason": ["not", "a", "string"]},
        {**data, "superseded_by_id": 12345678901234567890123456789012},
        {
            **data,
            "expected_handoff_head_ids": [
                expected_head_ids[0],
                12345678901234567890123456789012,
            ],
        },
    ]
    for invalid_data in invalid_types:
        invalid = await client.post(
            "/api/agent/strategic",
            headers=_headers(task, vm="agent-vm-resident-poll"),
            json={
                **request,
                "workday_operation_id": str(uuid.uuid4()),
                "data": invalid_data,
            },
        )
        assert invalid.status_code == 400, invalid.text
    await db_session.refresh(target)
    await db_session.refresh(keeper)
    assert target.lifecycle_status == "consumed"
    assert keeper.lifecycle_status == "consumed"

    created = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "status": "ok",
        "op": "dismiss_knowledge",
        "document_id": str(target.id),
    }
    await db_session.refresh(target)
    await db_session.refresh(keeper)
    assert target.lifecycle_status == "dismissed"
    assert target.dismissed_at is not None
    assert target.dismissed_reason == (
        f"superseded by {keeper.id}: "
        "Retire the stale predecessor after exact reconciliation."
    )
    assert keeper.lifecycle_status == "consumed"
    assert keeper.dismissed_at is None
    dismissed_at = target.dismissed_at

    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    ledger = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY]
    assert len(ledger) == 1
    assert ledger[0]["operation_id"] == request["workday_operation_id"]
    assert ledger[0]["op"] == "dismiss_knowledge"
    assert ledger[0]["entity_kind"] == "knowledge_document"
    assert ledger[0]["entity_id"] == str(target.id)
    assert ledger[0]["effect"] == "updated"

    replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "status": "ok",
        "op": "dismiss_knowledge",
        "idempotent_replay": True,
        "document_id": str(target.id),
    }
    changed_replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "data": {**data, "reason": "Changed replay must fail closed."},
        },
    )
    assert changed_replay.status_code == 409, changed_replay.text

    semantic_replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**request, "workday_operation_id": str(uuid.uuid4())},
    )
    assert semantic_replay.status_code == 200, semantic_replay.text
    assert semantic_replay.json() == {
        "status": "ok",
        "op": "dismiss_knowledge",
        "idempotent_replay": True,
        "document_id": str(target.id),
    }

    rejected_after_cleanup = [
        {
            **data,
            "knowledge_document_id": str(keeper.id),
            "superseded_by_id": str(target.id),
        },
        {
            **data,
            "knowledge_document_id": str(keeper.id),
            "superseded_by_id": str(uuid.uuid4()),
        },
    ]
    for stale_data in rejected_after_cleanup:
        rejected = await client.post(
            "/api/agent/strategic",
            headers=_headers(task, vm="agent-vm-resident-poll"),
            json={
                **request,
                "workday_operation_id": str(uuid.uuid4()),
                "data": stale_data,
            },
        )
        assert rejected.status_code == 409, rejected.text
    missing_cas = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "data": {
                "knowledge_document_id": str(keeper.id),
                "reason": "Missing exact head set must fail.",
            },
        },
    )
    assert missing_cas.status_code == 400, missing_cas.text

    await db_session.refresh(target)
    await db_session.refresh(keeper)
    refreshed_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert target.dismissed_at == dismissed_at
    assert target.lifecycle_status == "dismissed"
    assert keeper.lifecycle_status == "consumed"
    assert refreshed_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY] == ledger


@pytest.mark.asyncio
async def test_bound_set_memory_records_supersession_child_and_replays(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    predecessor = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.STRATEGIC_MEMORY.value,
        title="Previous handoff",
        summary="Previous handoff",
        body="Resume from the previous durable checkpoint.",
        tags=["handoff"],
        lifecycle_status="consumed",
    )
    db_session.add_all([task, predecessor])
    await db_session.commit()
    workday_claim_id = await _claim_receipt_bound_workday_packet(
        client,
        project=project,
        task=task,
        identity=identity,
    )
    operation_id = str(uuid.uuid4())
    request = {
        "task_id": str(task.id),
        "resident_incarnation_id": identity["incarnation_id"],
        "packet_id": str(packet.id),
        "workday_claim_id": workday_claim_id,
        "workday_operation_id": operation_id,
        "op": "set_memory",
        "data": {
            "title": "Current handoff",
            "body": "Resume from the current durable checkpoint.",
            "tags": ["handoff"],
            "expected_head_id": str(predecessor.id),
            "supersedes_id": str(predecessor.id),
        },
    }
    created = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert created.status_code == 200, created.text
    created_id = created.json()["doc_id"]
    await db_session.refresh(predecessor)
    assert predecessor.lifecycle_status == "dismissed"
    assert predecessor.dismissed_reason == f"superseded by {created_id}"

    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    ledger = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY]
    assert [receipt["op"] for receipt in ledger] == [
        "dismiss_knowledge",
        "set_memory",
    ]
    assert ledger[0]["operation_id"] != operation_id
    assert ledger[0]["entity_kind"] == "knowledge_document"
    assert ledger[0]["entity_id"] == str(predecessor.id)
    assert ledger[0]["effect"] == "updated"
    assert ledger[1]["operation_id"] == operation_id
    assert ledger[1]["entity_kind"] == "knowledge_document"
    assert ledger[1]["entity_id"] == created_id
    assert ledger[1]["effect"] == "created"

    replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "status": "ok",
        "op": "set_memory",
        "idempotent_replay": True,
        "doc_id": created_id,
    }
    refreshed_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert refreshed_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY] == ledger
    rows = (
        await db_session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.project_id == project.id,
                KnowledgeDocument.source_kind == SourceKind.STRATEGIC_MEMORY.value,
            )
        )
    ).scalars().all()
    assert {str(row.id) for row in rows} == {str(predecessor.id), created_id}


@pytest.mark.asyncio
async def test_direct_bound_qualification_records_main_operation_and_replays(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=57,
        status="open",
        phase="stabilize",
        phase_changed_at=_utcnow(),
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add_all([version, task])
    await db_session.commit()
    workday_claim_id = await _claim_receipt_bound_workday_packet(
        client,
        project=project,
        task=task,
        identity=identity,
    )
    operation_id = str(uuid.uuid4())
    request = {
        "task_id": str(task.id),
        "resident_incarnation_id": identity["incarnation_id"],
        "packet_id": str(packet.id),
        "workday_claim_id": workday_claim_id,
        "workday_operation_id": operation_id,
        "op": "request_qualification",
        "data": {"reason": "the bounded canary is ready", "probes": []},
    }

    created = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert created.status_code == 200, created.text
    intent_id = created.json()["qualification_intent_id"]
    assert created.json()["qualification_intent_status"] == "pending"
    intent = await db_session.get(
        ReleaseQualificationIntent,
        uuid.UUID(intent_id),
    )
    assert intent is not None
    assert intent.authority_key == f"workday:{packet.id}:{operation_id}"

    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    receipt = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY][-1]
    assert receipt["operation_id"] == operation_id
    assert receipt["op"] == "request_qualification"
    assert receipt["entity_kind"] == "release_qualification_intent"
    assert receipt["entity_id"] == intent_id

    replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "status": "ok",
        "op": "request_qualification",
        "idempotent_replay": True,
        "qualification_intent_id": intent_id,
    }
    rows = (
        await db_session.execute(
            select(ReleaseQualificationIntent).where(
                ReleaseQualificationIntent.project_id == project.id
            )
        )
    ).scalars().all()
    assert [row.id for row in rows] == [intent.id]


@pytest.mark.asyncio
async def test_direct_bound_qualification_without_target_fails_without_receipt(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add(task)
    await db_session.commit()
    workday_claim_id = await _claim_receipt_bound_workday_packet(
        client,
        project=project,
        task=task,
        identity=identity,
    )
    rejected = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "resident_incarnation_id": identity["incarnation_id"],
            "packet_id": str(packet.id),
            "workday_claim_id": workday_claim_id,
            "workday_operation_id": str(uuid.uuid4()),
            "op": "request_qualification",
            "data": {"reason": "no target exists", "probes": []},
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert "no durable intent was created" in rejected.text
    intents = (
        await db_session.execute(
            select(ReleaseQualificationIntent).where(
                ReleaseQualificationIntent.project_id == project.id
            )
        )
    ).scalars().all()
    assert intents == []
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert not durable_packet.context.get(WORKDAY_OPERATION_LEDGER_CONTEXT_KEY)


@pytest.mark.asyncio
async def test_bound_problem_model_qualification_rider_records_child_receipt(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=57,
        status="open",
        phase="stabilize",
        phase_changed_at=_utcnow(),
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add_all([version, task])
    await db_session.commit()
    workday_claim_id = await _claim_receipt_bound_workday_packet(
        client,
        project=project,
        task=task,
        identity=identity,
    )
    operation_id = str(uuid.uuid4())
    request = {
        "task_id": str(task.id),
        "resident_incarnation_id": identity["incarnation_id"],
        "packet_id": str(packet.id),
        "workday_claim_id": workday_claim_id,
        "workday_operation_id": operation_id,
        "op": "set_problem_model",
        "data": {
            "intent": {
                "purpose": "Ship the exact bounded successor proof.",
                "current_focus": "qualify v57 exactly once",
            },
            "capabilities": [],
            "gaps": [],
            "qualify_request": {"reason": "bounded canary complete", "probes": []},
        },
    }

    created = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert created.status_code == 200, created.text
    intents = (
        await db_session.execute(
            select(ReleaseQualificationIntent).where(
                ReleaseQualificationIntent.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(intents) == 1
    intent = intents[0]
    assert intent.authority_key == f"workday:{packet.id}:{operation_id}"
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    ledger = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY]
    child = next(row for row in ledger if row["op"] == "request_qualification")
    parent = next(row for row in ledger if row["op"] == "set_problem_model")
    assert child["operation_id"] != operation_id
    assert child["entity_kind"] == "release_qualification_intent"
    assert child["entity_id"] == str(intent.id)
    assert parent["operation_id"] == operation_id

    replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    rows = (
        await db_session.execute(
            select(ReleaseQualificationIntent.id).where(
                ReleaseQualificationIntent.project_id == project.id
            )
        )
    ).scalars().all()
    assert rows == [intent.id]


@pytest.mark.asyncio
async def test_direct_acceptance_criteria_is_composite_receipted_and_replay_safe(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=57,
        status="open",
    )
    direction = Direction(
        project_id=project.id,
        direction_key="d-receipt-contract",
        data={"id": "d-receipt-contract", "status": "active"},
        status="active",
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add_all([version, direction, task])
    await db_session.commit()

    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert packet_claim.status_code == 200, packet_claim.text
    workday_claim_id = packet_claim.json()["packet"]["workday_claim_id"]
    operation_id = str(uuid.uuid4())
    data = {
        "criteria": [
            {
                "text": "The direct criterion is durably receipt-bound.",
                "direction_keys": [direction.direction_key],
            }
        ]
    }
    request = {
        "task_id": str(task.id),
        "resident_incarnation_id": identity["incarnation_id"],
        "packet_id": str(packet.id),
        "workday_claim_id": workday_claim_id,
        "workday_operation_id": operation_id,
        "op": "set_acceptance_criteria",
        "data": data,
    }
    created = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert created.status_code == 200, created.text
    criterion_key = created.json()["criteria"][0]["criterion_key"]

    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    ledger = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY]
    assert [receipt["op"] for receipt in ledger] == [
        "create_acceptance_criterion",
        "update_direction",
        "set_acceptance_criteria",
    ]
    assert ledger[0]["entity_kind"] == "acceptance_criterion"
    assert ledger[0]["entity_id"] == criterion_key
    assert ledger[0]["effect"] == "created"
    assert ledger[1]["entity_kind"] == "direction"
    assert ledger[1]["entity_id"] == direction.direction_key
    assert ledger[2]["operation_id"] == operation_id
    assert ledger[2]["target_kind"] == "acceptance_criteria"
    assert ledger[2]["target_revision"] == 57

    replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "status": "ok",
        "op": "set_acceptance_criteria",
        "idempotent_replay": True,
        "revision": 57,
    }
    criteria = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.release_version_id == version.id
            )
        )
    ).scalars().all()
    assert [row.criterion_key for row in criteria] == [criterion_key]

    conflicting = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "data": {
                "criteria": [
                    {"text": "A different payload must not reuse authority."}
                ]
            },
        },
    )
    assert conflicting.status_code == 409
    assert "different operation" in conflicting.text

    duplicate_retract = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion_key,
                        "reason": "duplicate attempt one",
                    },
                    {
                        "criterion_key": criterion_key,
                        "reason": "duplicate attempt two",
                    },
                ]
            },
        },
    )
    assert duplicate_retract.status_code == 400
    assert "retracted at most once" in duplicate_retract.text
    criterion = (
        await db_session.execute(
            select(VersionAcceptanceCriterion)
            .where(VersionAcceptanceCriterion.criterion_key == criterion_key)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert criterion.status == "active"

    predecessor = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-prior-supersession",
        text="Prior superseded contract",
        status="superseded",
        superseded_by_key=criterion_key,
        created_by="strategist",
    )
    db_session.add(predecessor)
    await db_session.commit()
    retract_operation_id = str(uuid.uuid4())
    retract_data = {
        "retract": [
            {
                "criterion_key": criterion_key,
                "reason": "Replaced by exact canonical contract.",
            }
        ]
    }
    retracted = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": retract_operation_id,
            "data": retract_data,
        },
    )
    assert retracted.status_code == 200, retracted.text
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    ledger = durable_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY]
    assert [receipt["op"] for receipt in ledger[-3:]] == [
        "update_acceptance_criterion",
        "retract_acceptance_criterion",
        "set_acceptance_criteria",
    ]
    assert ledger[-3]["entity_id"] == predecessor.criterion_key
    assert ledger[-2]["entity_id"] == criterion_key
    assert ledger[-1]["operation_id"] == retract_operation_id
    await db_session.refresh(predecessor)
    await db_session.refresh(criterion)
    assert predecessor.superseded_by_key is None
    assert criterion.status == "retracted"

    semantic_replay = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "data": retract_data,
        },
    )
    assert semantic_replay.status_code == 200, semantic_replay.text
    assert semantic_replay.json()["idempotent_replay"] is True
    replay_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert replay_packet.context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY] == ledger

    unsupported = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **request,
            "workday_operation_id": str(uuid.uuid4()),
            "op": "set_session_summary",
            "data": {"summary": "must not commit privately without a receipt"},
        },
    )
    assert unsupported.status_code == 409
    assert "not receipt-bound" in unsupported.text

    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": workday_claim_id,
            "outcome": "completed",
            "result_summary": "The canonical release contract was updated.",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    consultation_id = uuid.UUID(
        str(
            (replay_packet.context or {})[
                "_workday_consultation_atomic_reply_id"
            ]
        )
    )
    detail = await project_consultation_detail(
        db_session,
        project_id=project.id,
        consultation_id=consultation_id,
    )
    assert detail is not None
    assert detail["state"] == "delivering"
    assert detail["outcome"] == "completed"
    assert detail["steps_truncated"] is False
    assert detail["result"]["receipts_have_more"] is False
    for receipts in (detail["steps"][:3], detail["result"]["receipts"][:3]):
        assert [receipt["op"] for receipt in receipts] == [
            "create_acceptance_criterion",
            "update_direction",
            "set_acceptance_criteria",
        ]
        assert receipts[0]["entity_kind"] == "acceptance_criterion"
        assert receipts[0]["entity_id"] == criterion_key
        assert receipts[0]["effect"] == "created"
        assert receipts[1]["entity_kind"] == "direction"
        assert receipts[1]["entity_id"] == direction.direction_key
        assert receipts[2]["target_kind"] == "acceptance_criteria"
        assert receipts[2]["target_revision"] == 57

    continuation = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["status"] == "claimed"
    projected = continuation.json()["continuation"][
        "current_live_work_authority"
    ]["consultation_mutation_receipts"]
    assert projected == authoritative_workday_operation_receipts(
        replay_packet.context,
        packet_id=packet.id,
    )


@pytest.mark.asyncio
async def test_composite_receipt_capacity_failure_rolls_back_every_write(
    client: AsyncClient,
    db_session,
):
    project, task, identity, packet = await _start_receipt_bound_workday_packet(
        client, db_session
    )
    version = ReleaseVersion(
        project_id=project.id,
        version_number=57,
        status="open",
    )
    existing = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-capacity-existing",
        text="Existing criterion must survive rollback.",
        created_by="strategist",
    )
    direction = Direction(
        project_id=project.id,
        direction_key="d-capacity",
        data={
            "id": "d-capacity",
            "status": "active",
            "criterion_release_version_id": str(version.id),
        },
        status="active",
        criterion_keys=[existing.criterion_key],
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add_all([version, existing, direction, task])
    await db_session.commit()

    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert packet_claim.status_code == 200, packet_claim.text
    workday_claim_id = packet_claim.json()["packet"]["workday_claim_id"]
    packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    packet_context = dict(packet.context or {})
    envelope = packet_context["envelope"]
    seeded_ledger: list[dict] = []
    for index in range(WORKDAY_OPERATION_LEDGER_LIMIT - 3):
        receipt = _receipt(index=index, packet_id=packet.id, task_id=task.id)
        receipt.update(
            workday_claim_id=workday_claim_id,
            fast_claim_id=packet_context["claim_id"],
            worker_incarnation=identity["incarnation_id"],
            incarnation_id=identity["incarnation_id"],
            envelope_snapshot_id=envelope["snapshot_id"],
            resident_session_id=identity["session_id"],
            session_id=identity["session_id"],
            worker_generation=identity["worker_generation"],
        )
        seeded_ledger.append(receipt)
    seeded_ledger = authoritative_workday_operation_receipts(
        {WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: seeded_ledger},
        packet_id=packet.id,
    )
    assert len(seeded_ledger) == WORKDAY_OPERATION_LEDGER_LIMIT - 3
    packet_context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY] = seeded_ledger
    packet.context = packet_context
    db_session.add(packet)
    await db_session.commit()

    # The capacity fixture must remain a production-shaped claimed packet;
    # otherwise the request would fail before exercising transactional receipt
    # exhaustion and could not prove rollback of the composite mutation.
    from app.conversation_coordinator import _is_workday_consultation_packet

    assert packet.status == "open"
    assert packet.handling_state == MessageHandlingState.CLAIMED
    assert packet.handled_by_task_id == task.id
    assert packet.resident_session_id == identity["session_id"]
    assert _is_workday_consultation_packet(
        packet,
        resident_session_id=identity["session_id"],
    ), {
        "from_type": packet.from_type,
        "surface": packet.surface,
        "status": packet.status,
        "resident_session_id": packet.resident_session_id,
        "packet_marker": packet.context.get("_conversation_consultation_packet"),
        "audience": packet.context.get("audience"),
        "message_role": packet.context.get("message_role"),
        "public_reply_owner": packet.context.get("public_reply_owner"),
        "atomic_reply_id": packet.context.get(
            "_workday_consultation_atomic_reply_id"
        ),
    }
    task_context_before = copy.deepcopy(task.context)
    packet_context_before = copy.deepcopy(packet.context)
    direction_keys_before = list(direction.criterion_keys)
    direction_data_before = copy.deepcopy(direction.data)
    packet_id = packet.id
    version_id = version.id
    existing_key = existing.criterion_key
    direction_id = direction.id
    direction_key = direction.direction_key
    task_id = task.id
    project_id = project.id
    message_ids_before = set(
        (
            await db_session.execute(
                select(ProjectMessage.id).where(
                    ProjectMessage.project_id == project.id
                )
            )
        ).scalars().all()
    )

    rejected = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "resident_incarnation_id": identity["incarnation_id"],
            "packet_id": str(packet_id),
            "workday_claim_id": workday_claim_id,
            "workday_operation_id": str(uuid.uuid4()),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "This write must roll back at receipt capacity.",
                        "direction_keys": [direction_key],
                    }
                ],
                "retract": [
                    {
                        "criterion_key": existing_key,
                        "reason": "Ledger-capacity rollback probe.",
                    }
                ],
            },
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert "capacity is exhausted" in rejected.text
    await db_session.rollback()

    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as verify:
        durable_packet = await verify.get(ProjectMessage, packet_id)
        assert durable_packet is not None
        assert durable_packet.context == packet_context_before
        assert authoritative_workday_operation_receipts(
            durable_packet.context,
            packet_id=packet_id,
        ) == seeded_ledger
        criteria = (
            await verify.execute(
                select(VersionAcceptanceCriterion).where(
                    VersionAcceptanceCriterion.release_version_id == version_id
                )
            )
        ).scalars().all()
        assert [row.criterion_key for row in criteria] == [existing_key]
        durable_existing = criteria[0]
        assert durable_existing.status == "active"
        assert durable_existing.retraction_reason is None
        assert durable_existing.retracted_at is None
        assert durable_existing.retracted_by is None
        durable_direction = (
            await verify.execute(
                select(Direction).where(Direction.id == direction_id)
            )
        ).scalars().one()
        assert durable_direction.criterion_keys == direction_keys_before
        assert durable_direction.data == direction_data_before
        durable_task = await verify.get(Task, task_id)
        assert durable_task is not None
        assert durable_task.context == task_context_before
        message_ids_after = set(
            (
                await verify.execute(
                    select(ProjectMessage.id).where(
                        ProjectMessage.project_id == project_id
                    )
                )
            ).scalars().all()
        )
        assert message_ids_after == message_ids_before
