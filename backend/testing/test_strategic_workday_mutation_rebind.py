"""Focused unit tests for receipt-bound Workday operation deduplication."""

import copy
import uuid

import pytest
from fastapi import HTTPException

from app.models import MessageSurface, MessageType, ProjectMessage, Task
from app.strategic_workday_mutation_rebind import (
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_LIMIT,
    WorkdayConsultationOperationBinding,
    find_workday_operation_replay,
    workday_operation_replay_response,
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


def test_completion_debt_replay_projects_the_exact_debt_id():
    receipt = {
        "op": "dispose_completion_debt",
        "entity_kind": "completion_debt",
        "entity_id": "00000000-0000-0000-0000-000000000123",
        "effect": "updated",
    }

    assert workday_operation_replay_response(receipt) == {
        "status": "ok",
        "op": "dispose_completion_debt",
        "idempotent_replay": True,
        "debt_id": "00000000-0000-0000-0000-000000000123",
    }
