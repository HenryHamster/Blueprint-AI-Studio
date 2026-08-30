"""Terminal recovery regressions for typed Workday and operator handoffs."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete, event, update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.conversation_coordinator import (
    _OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY,
    _OPERATOR_TURN_MAX_DEAD_CLAIMS,
    _is_workday_consultation_packet,
)
import app.strategic_workday_handoff_recovery as handoff_recovery
from app.routers.agent_strategic_messages import (
    _reconciled_workday_continuation_receipts,
)
from app.models import (
    MessageHandlingState,
    Project,
    ProjectMessage,
    ProjectMessageSequence,
    StrategicContextEnvelopeSnapshot,
    StrategicMemorySource,
)
from app.project_message_creation import lock_project_and_next_message_sequence
from app.session_message_handoff import (
    RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
    handoff_session_messages,
)
from app.strategic_workday_handoff_recovery import (
    _canonical_source_receipt,
    _source_is_settled_or_live,
    _source_shape_is_canonical,
    recover_one_missing_workday_request,
)
from app.strategic_workday_mutation_rebind import (
    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
    WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
)
from testing.test_strategic_session_messages import (
    _activate_typed_fast_worker,
    _add_typed_fast_input,
    _headers,
    _make_resident_poll_worker,
    _typed_fast_claim_payload,
)


async def _start_workday_request(client: AsyncClient, db_session, *, key: str):
    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    claimed = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )
    assert claimed.status_code == 200, claimed.text
    identity = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
    }
    completed = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "claim_id": claimed.json()["claim_id"],
            "owner": "fast",
            "content": "I am checking this privately and will follow up.",
            "idempotency_key": f"{key}-public-request",
            "envelope": claimed.json()["envelope"],
            "workday_request": {
                "delivery_key": key,
                "question": "private secret question that must never leak",
            },
        },
    )
    assert completed.status_code == 200, completed.text
    source = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key == f"{key}-public-request"
            )
        )
    ).scalars().one()
    packet = await db_session.get(
        ProjectMessage, uuid.UUID(completed.json()["workday_packet"]["packet_id"])
    )
    assert packet is not None
    return project, task, incarnation, identity, source, packet


async def _add_workday_request(
    client: AsyncClient,
    db_session,
    *,
    project,
    task,
    incarnation,
    identity,
    key: str,
):
    inbound = await _add_typed_fast_input(db_session, project)
    claimed = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task,
            incarnation,
            inbound.id,
            client_claim_id=str(uuid.uuid4()),
        ),
    )
    assert claimed.status_code == 200, claimed.text
    completed = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "claim_id": claimed.json()["claim_id"],
            "owner": "fast",
            "content": f"public request {key}",
            "idempotency_key": f"{key}-public-request",
            "envelope": claimed.json()["envelope"],
            "workday_request": {
                "delivery_key": key,
                "question": f"private question {key}",
            },
        },
    )
    assert completed.status_code == 200, completed.text
    source = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.idempotency_key == f"{key}-public-request"
            )
        )
    ).scalars().one()
    packet = await db_session.get(
        ProjectMessage,
        uuid.UUID(completed.json()["workday_packet"]["packet_id"]),
    )
    assert packet is not None
    return source, packet


def _claim_body(identity: dict) -> dict:
    return {**identity, "lease_seconds": 45}


async def _claim_continuation(client, project, task, identity):
    return await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        "continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )


async def _move_private_handoff_to_retired_session(
    db_session,
    row: ProjectMessage,
    *,
    retired_session_id: str,
) -> uuid.UUID:
    """Make both the durable row and its immutable envelope genuinely old."""

    context = dict(row.context or {})
    snapshot_id = uuid.UUID(str(context["envelope"]["snapshot_id"]))
    snapshot = await db_session.get(StrategicContextEnvelopeSnapshot, snapshot_id)
    assert snapshot is not None
    snapshot.session_id = retired_session_id
    row.resident_session_id = retired_session_id
    db_session.add_all([snapshot, row])
    await db_session.commit()
    return snapshot_id


@pytest.mark.asyncio
async def test_preupgrade_ack_without_diagnostic_drains_and_is_idempotent(
    client: AsyncClient, db_session
):
    """An in-flight old Agent can ack after the API schema rolls forward.

    The server must expose that provider diagnostics were unavailable, while a
    lost-response retry remains an exact duplicate rather than creating a
    second attempt or trapping the runner in a 422 retry loop.
    """

    project, task, _incarnation, identity, _source, packet = (
        await _start_workday_request(client, db_session, key="legacy-ack")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    claimed = packet_claim.json()["packet"]
    endpoint = (
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack"
    )
    payload = {
        **identity,
        "workday_claim_id": claimed["workday_claim_id"],
        "outcome": "completed",
        "result_summary": "legacy private result",
    }

    acknowledged = await client.post(
        endpoint,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    replay = await client.post(
        endpoint,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert acknowledged.status_code == replay.status_code == 200
    assert acknowledged.json()["status"] == "acknowledged"
    assert replay.json()["status"] == "duplicate"

    refreshed_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    attempts = refreshed_packet.context["_workday_consultation_attempts"]
    assert len(attempts) == 1
    diagnostic = attempts[0]
    assert diagnostic["attempt"] == claimed["attempt"]
    assert diagnostic["workday_claim_id"] == claimed["workday_claim_id"]
    assert diagnostic["outcome"] == "completed"
    assert diagnostic["terminal_predicate"] == (
        "legacy_runner_ack_without_diagnostic"
    )
    assert diagnostic["provider_terminal_reason"] is None
    assert diagnostic["provider_output_tail"] is None
    assert diagnostic["result_chars"] == len("legacy private result")
    assert diagnostic["trace"] == [
        {
            "elapsed_ms": diagnostic["turn_ms"],
            "kind": "error",
            "label": "legacy_diagnostic_unavailable",
            "is_error": True,
        }
    ]
    assert diagnostic["trace_truncated"] is True


@pytest.mark.asyncio
async def test_malformed_packet_ledger_is_quarantined_before_claim_response(
    client: AsyncClient, db_session
):
    project, task, incarnation, identity, first_source, first_packet = (
        await _start_workday_request(client, db_session, key="poisoned-ledger")
    )
    _second_source, second_packet = await _add_workday_request(
        client,
        db_session,
        project=project,
        task=task,
        incarnation=incarnation,
        identity=identity,
        key="valid-behind-ledger",
    )
    first_context = dict(first_packet.context or {})
    first_context["_workday_consultation_operation_ledger"] = {
        "private_forged_receipt": "must never be projected"
    }
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == first_packet.id)
        .values(context=first_context)
    )
    await db_session.commit()
    snapshot_ids_before = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )

    claimed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    body = claimed.json()["packet"]
    assert body["packet_id"] == str(second_packet.id)
    assert body["question"] == second_packet.content
    assert body["prior_receipts"] == []
    assert body["prior_attempts"] == []
    assert "private_forged_receipt" not in claimed.text
    assert first_packet.content not in claimed.text

    rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    quarantined = by_id[first_packet.id]
    assert quarantined.status == "resolved"
    assert quarantined.handling_state == MessageHandlingState.SUPERSEDED
    assert quarantined.resolution == (
        "private_handoff_continuation_provenance_invalid"
    )
    assert quarantined.handled_by_task_id is None
    assert not any(
        key.startswith("_workday_consultation_claim_")
        for key in (quarantined.context or {})
    )
    recovery_rows = [
        row
        for row in rows
        if row.idempotency_key
        == f"workday-consultation-terminal-recovery:{first_source.id}"
    ]
    assert len(recovery_rows) == 1
    assert recovery_rows[0].content == handoff_recovery._RECOVERY_SUMMARY
    snapshot_ids_after = {
        snapshot_id
        for snapshot_id in (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    }
    assert len(snapshot_ids_after - snapshot_ids_before) <= 1

    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"{second_packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": body["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "valid packet completed after poison quarantine",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    empty = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["status"] == "empty"


@pytest.mark.asyncio
async def test_retired_session_packet_and_result_rebind_to_current_resident(
    client: AsyncClient, db_session
):
    """A private handoff cannot disappear behind its originating session.

    The normal stop/start handoff eagerly rebinds immediate-predecessor rows,
    but a packet can predate that predecessor after multiple stopped Residents.
    Claiming must discover the durable server-authored row project-wide, then
    rebind both its packet and result to the exact current worker before
    renew/ack or public continuation delivery.
    """

    project, task, incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retired-session")
    )
    original_packet_snapshot_id = await _move_private_handoff_to_retired_session(
        db_session,
        packet,
        retired_session_id="retired-resident-session",
    )

    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    claimed = packet_claim.json()["packet"]
    assert packet_claim.json()["status"] == "claimed"
    assert claimed["packet_id"] == str(packet.id)
    assert claimed["envelope"]["snapshot_id"] != str(original_packet_snapshot_id)
    await db_session.refresh(packet)
    assert packet.resident_session_id == identity["session_id"]
    rebound_packet_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        uuid.UUID(claimed["envelope"]["snapshot_id"]),
    )
    assert rebound_packet_snapshot is not None
    assert rebound_packet_snapshot.session_id == identity["session_id"]
    original_packet_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        original_packet_snapshot_id,
    )
    assert original_packet_snapshot is not None
    assert original_packet_snapshot.session_id == "retired-resident-session"
    assert original_packet_snapshot.canonical_sha256 == (
        rebound_packet_snapshot.canonical_sha256
    )
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in packet.context

    abandoned = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": claimed["workday_claim_id"],
            "outcome": "abandoned",
            "result_summary": "retry after the cross-session rebind",
        },
    )
    assert abandoned.status_code == 200, abandoned.text
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    assert packet_claim.json()["status"] == "claimed"
    claimed = packet_claim.json()["packet"]
    assert claimed["packet_id"] == str(packet.id)
    assert claimed["attempt"] == 2
    await db_session.refresh(packet)
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in packet.context

    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": claimed["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "durable result from the rebound packet",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text

    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    original_result_snapshot_id = await _move_private_handoff_to_retired_session(
        db_session,
        result,
        retired_session_id="retired-result-session",
    )

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    body = continuation.json()["continuation"]
    assert continuation.json()["status"] == "claimed"
    assert body["result_packet_id"] == str(result.id)
    assert body["consultation_id"] == str(source.id)
    assert body["envelope"]["snapshot_id"] != str(original_result_snapshot_id)
    await db_session.refresh(result)
    assert result.resident_session_id == identity["session_id"]
    original_result_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        original_result_snapshot_id,
    )
    rebound_result_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        uuid.UUID(body["envelope"]["snapshot_id"]),
    )
    assert original_result_snapshot is not None
    assert rebound_result_snapshot is not None
    assert original_result_snapshot.session_id == "retired-result-session"
    assert rebound_result_snapshot.session_id == identity["session_id"]
    assert original_result_snapshot.canonical_sha256 == (
        rebound_result_snapshot.canonical_sha256
    )
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in result.context

    released = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{result.id}/release",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": body["continuation_claim_id"],
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "released"
    reclaimed = await _claim_continuation(client, project, task, identity)
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["status"] == "claimed"
    reclaimed_body = reclaimed.json()["continuation"]
    assert reclaimed_body["result_packet_id"] == str(result.id)
    assert reclaimed_body["attempt"] == 2
    await db_session.refresh(result)
    assert result.status == "open"
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in result.context


@pytest.mark.asyncio
async def test_retired_session_legacy_result_remains_read_only_deliverable(
    client: AsyncClient, db_session
):
    """A pre-claim-id result can cross sessions without gaining write authority."""

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retired-legacy-result")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "legacy result remains useful after session replacement",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text

    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    context = dict(result.context or {})
    context.pop("claim_id", None)
    result.context = context
    await _move_private_handoff_to_retired_session(
        db_session,
        result,
        retired_session_id="retired-resident-session",
    )

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    body = continuation.json()["continuation"]
    assert continuation.json()["status"] == "claimed"
    assert body["result_packet_id"] == str(result.id)
    assert body["consultation_id"] == str(source.id)
    assert body["claim_id"] is None
    assert body["current_live_work_authority"][
        "consultation_mutation_receipts"
    ] == []
    assert body["current_live_work_authority"]["collection_metadata"][
        "consultation_mutation_receipts"
    ] == {"returned_count": 0, "has_more": False}
    await db_session.refresh(result)
    assert result.status == "open"
    assert result.resolution is None
    assert result.resident_session_id == identity["session_id"]


@pytest.mark.asyncio
async def test_retired_owner_receipts_replay_and_reach_current_continuation(
    client: AsyncClient, db_session
):
    """Replacement workers inherit packet history without replaying old writes."""

    project, task, _incarnation, identity, _source, packet = (
        await _start_workday_request(client, db_session, key="retired-receipts")
    )
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add(task)
    await db_session.commit()
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    claimed = packet_claim.json()["packet"]
    first_data = {"markdown": "# Durable update\nOne historical write."}

    async def write_running_update(data: dict) -> object:
        return await client.post(
            "/api/agent/strategic",
            headers=_headers(task, vm="agent-vm-resident-poll"),
            json={
                "task_id": str(task.id),
                "resident_incarnation_id": identity["incarnation_id"],
                "op": "set_running_update",
                "data": data,
                "packet_id": str(packet.id),
                "workday_claim_id": claimed["workday_claim_id"],
                "workday_operation_id": str(uuid.uuid4()),
            },
        )

    first_write = await write_running_update(first_data)
    assert first_write.status_code == 200, first_write.text
    assert first_write.json().get("idempotent_replay") is not True
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    old_receipt = durable_packet.context[
        "_workday_consultation_operation_ledger"
    ][0]
    assert old_receipt["task_id"] == str(task.id)
    assert old_receipt["resident_session_id"] == identity["session_id"]

    origin_session_id = identity["session_id"]
    changed = await handoff_session_messages(
        db_session,
        project.id,
        origin_session_id,
        replacement_resident_active=True,
    )
    assert changed >= 1
    await db_session.refresh(project)
    await db_session.refresh(task)
    replacement_session_id = "replacement-receipt-session"
    task_context = dict(task.context or {})
    task_context["strategic_session_id"] = replacement_session_id
    task_context["_resident_worker_generation"] = 3
    task.context = task_context
    flag_modified(task, "context")
    resident = dict(project.vision["_resident_strategic_session"])
    resident["session_id"] = replacement_session_id
    resident["worker_generation"] = 3
    project.vision = {**project.vision, "_resident_strategic_session": resident}
    flag_modified(project, "vision")
    db_session.add_all([project, task])
    await db_session.commit()
    identity = {
        **identity,
        "session_id": replacement_session_id,
        "worker_generation": 3,
    }
    replacement_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert replacement_claim.status_code == 200, replacement_claim.text
    assert replacement_claim.json()["status"] == "claimed"
    claimed = replacement_claim.json()["packet"]

    await db_session.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(running_update="Operator wrote a newer document.")
    )
    await db_session.commit()
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert durable_packet.handling_state == MessageHandlingState.CLAIMED
    assert durable_packet.handled_by_task_id == task.id
    assert durable_packet.resident_session_id == identity["session_id"]
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in durable_packet.context
    assert _is_workday_consultation_packet(
        durable_packet,
        resident_session_id=identity["session_id"],
    )

    replay = await write_running_update(first_data)
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    current_project = (
        await db_session.execute(
            select(Project)
            .where(Project.id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert current_project.running_update == "Operator wrote a newer document."
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert len(
        durable_packet.context["_workday_consultation_operation_ledger"]
    ) == 1

    second_write = await write_running_update(
        {"markdown": "# Durable update\nOne historical and one current write."}
    )
    assert second_write.status_code == 200, second_write.text
    durable_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    packet_receipts = durable_packet.context[
        "_workday_consultation_mutation_receipts"
    ]
    assert len(packet_receipts) == 2
    assert packet_receipts[0]["task_id"] == str(task.id)
    assert packet_receipts[0]["resident_session_id"] == origin_session_id

    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": claimed["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "Both durable writes are reconciled.",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    canonical_result_context = dict(result.context or {})
    result_context = dict(canonical_result_context)
    assert len(
        result_context["_workday_consultation_mutation_receipts"]
    ) == 2
    claimless_receipt_context = dict(result_context)
    claimless_receipt_context.pop("claim_id", None)
    with pytest.raises(HTTPException) as claimless_error:
        await _reconciled_workday_continuation_receipts(
            db_session,
            project_id=project.id,
            result_content=result.content,
            result_context=claimless_receipt_context,
            source_packet_id=packet.id,
        )
    assert "Legacy Workday continuation" in str(claimless_error.value.detail)

    # Model a result finalized by the pre-fix owner-filtering code. The result
    # projection omitted the retired owner's receipt, while the resolved source
    # packet retained the complete authoritative operation ledger.
    current_receipt = packet_receipts[-1]
    result_context["claim_id"] = durable_packet.context["claim_id"]
    result_context["_workday_consultation_mutation_receipts"] = [
        {**current_receipt, "target_sha256": "f" * 64}
    ]
    with pytest.raises(HTTPException) as projection_error:
        await _reconciled_workday_continuation_receipts(
            db_session,
            project_id=project.id,
            result_content=result.content,
            result_context=result_context,
            source_packet_id=packet.id,
        )
    assert "projection conflicts with source" in str(
        projection_error.value.detail
    )

    result_context["_workday_consultation_mutation_receipts"] = [
        current_receipt
    ]
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == result.id)
        .values(context=result_context)
    )
    await db_session.commit()
    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    receipts = continuation.json()["continuation"]["current_live_work_authority"][
        "consultation_mutation_receipts"
    ]
    assert len(receipts) == 2
    assert receipts[0]["task_id"] == str(task.id)
    assert receipts[0]["resident_session_id"] == origin_session_id
    assert receipts[1]["task_id"] == str(task.id)
    assert receipts[1]["resident_session_id"] == identity["session_id"]


@pytest.mark.asyncio
async def test_stale_workday_packet_settles_source_once_without_private_leak(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="stale-packet")
    )
    packet_context = dict(packet.context or {})
    packet_context["envelope"] = {
        **dict(packet_context["envelope"]),
        "snapshot_id": str(uuid.uuid4()),
    }
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == packet.id)
        .values(context=packet_context)
    )
    await db_session.commit()

    initial_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert initial_claim.status_code == 200, initial_claim.text
    assert initial_claim.json()["status"] == "empty"
    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    body = continuation.json()["continuation"]
    assert body["consultation_id"] == str(source.id)
    assert "became unavailable" in body["result_summary"]
    assert "private secret" not in body["result_summary"]
    recovery_result_id = uuid.UUID(body["result_packet_id"])
    recovery_result = await db_session.get(ProjectMessage, recovery_result_id)
    assert recovery_result is not None
    stale_snapshot_id = str(uuid.uuid4())
    recovery_context = dict(recovery_result.context or {})
    recovery_context["envelope"] = {
        **dict(recovery_context["envelope"]),
        "snapshot_id": stale_snapshot_id,
    }
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == recovery_result_id)
        .values(context=recovery_context)
    )
    await db_session.commit()

    restored = await _claim_continuation(client, project, task, identity)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "claimed"
    body = restored.json()["continuation"]
    assert body["result_packet_id"] == str(recovery_result_id)
    refreshed_recovery = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == recovery_result_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert refreshed_recovery.status == "open"
    assert refreshed_recovery.handling_state == MessageHandlingState.CLAIMED
    assert refreshed_recovery.resident_session_id == identity["session_id"]
    assert "_private_handoff_retirement" not in refreshed_recovery.context
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in (
        refreshed_recovery.context
    )
    assert refreshed_recovery.context["envelope"]["snapshot_id"] != (
        stale_snapshot_id
    )

    completion_payload = {
        **identity,
        "continuation_claim_id": body["continuation_claim_id"],
        "content": "The private check failed safely; no managed outcome is verified.",
        "idempotency_key": f"stale-packet-terminal:{source.id}",
    }
    endpoint = (
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{body['result_packet_id']}/complete"
    )
    completed = await client.post(
        endpoint,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=completion_payload,
    )
    replay = await client.post(
        endpoint,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=completion_payload,
    )
    assert completed.status_code == replay.status_code == 200
    assert completed.json()["status"] == "created"
    assert replay.json()["status"] == "duplicate"
    assert (await _claim_continuation(client, project, task, identity)).json()[
        "status"
    ] == "empty"


@pytest.mark.asyncio
async def test_stale_workday_result_is_replaced_by_neutral_terminal_result(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="stale-result")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    claimed_packet = packet_claim.json()["packet"]
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": claimed_packet["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "private stale result that must never be surfaced",
            "attempt_diagnostic": {
                "attempt": claimed_packet["attempt"],
                "workday_claim_id": claimed_packet["workday_claim_id"],
                "outcome": "completed",
                "terminal_predicate": "provider_result",
                "provider_terminal_reason": "success",
                "provider_output_tail": "private stale result completed",
                "started_at": "2026-08-29T18:00:00+00:00",
                "ended_at": "2026-08-29T18:00:00.010000+00:00",
                "turn_ms": 10,
                "ttft_ms": 1,
                "tool_call_count": 0,
                "tool_call_duration_ms": 0,
                "result_chars": 50,
                "trace": [
                    {"elapsed_ms": 10, "kind": "result", "label": "success"}
                ],
                "trace_truncated": False,
            },
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    stale_result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    result_context = dict(stale_result.context or {})
    result_context["envelope"] = {
        **dict(result_context["envelope"]),
        "snapshot_id": str(uuid.uuid4()),
    }
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == stale_result.id)
        .values(context=result_context)
    )
    await db_session.commit()

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    body = continuation.json()["continuation"]
    assert body["consultation_id"] == str(source.id)
    assert body["result_packet_id"] != str(stale_result.id)
    assert "became unavailable" in body["result_summary"]
    assert "private stale result" not in body["result_summary"]
    retired_result = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == stale_result.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    assert retired_result is not None
    assert retired_result.status == "resolved"
    assert retired_result.handling_state == MessageHandlingState.SUPERSEDED


@pytest.mark.asyncio
async def test_missing_handoff_recovers_but_deleted_source_publishes_nothing(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="missing-packet")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    assert recovered.json()["continuation"]["consultation_id"] == str(source.id)

    project2, task2, _incarnation2, identity2, source2, packet2 = (
        await _start_workday_request(client, db_session, key="deleted-source")
    )
    await db_session.delete(packet2)
    await db_session.delete(source2)
    await db_session.commit()
    no_leak = await _claim_continuation(client, project2, task2, identity2)
    assert no_leak.status_code == 200, no_leak.text
    assert no_leak.json()["status"] == "empty"
    recovery_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project2.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source2.id}",
            )
        )
    ).scalars().all()
    assert recovery_rows == []


@pytest.mark.asyncio
async def test_legacy_recovery_cursor_without_ceiling_restarts_without_skipping(
    client: AsyncClient, db_session
):
    """A pre-ceiling cursor cannot skip requests created during an upgrade."""

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="legacy-cursor")
    )
    await db_session.delete(packet)
    task.context = {
        **dict(task.context or {}),
        "_workday_consultation_recovery_before_sequence": source.sequence,
    }
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()

    recovered = await _claim_continuation(client, project, task, identity)

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    assert recovered.json()["continuation"]["consultation_id"] == str(
        source.id
    )
    refreshed_task = (
        await db_session.execute(
            select(type(task))
            .where(type(task).id == task.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    # The legacy, unpaired cursor was discarded. A new atomic cursor/ceiling
    # pair records the successful source and the frozen scan boundary so a
    # subsequent scan can continue without skipping concurrent arrivals.
    assert refreshed_task.context[
        "_workday_consultation_recovery_before_sequence"
    ] == source.sequence
    assert refreshed_task.context[
        "_workday_consultation_recovery_scan_ceiling"
    ] >= source.sequence


@pytest.mark.asyncio
async def test_retired_handoff_recovery_failure_retries_ahead_of_high_watermark(
    client: AsyncClient, db_session, monkeypatch
):
    """A transient neutral-result failure cannot orphan a retired packet."""

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retry-retirement")
    )
    assert (
        await recover_one_missing_workday_request(
            db_session,
            project=project,
            task=task,
            worker_generation=identity["worker_generation"],
            worker_incarnation=uuid.UUID(identity["incarnation_id"]),
            session_id=identity["session_id"],
        )
        is None
    )
    await db_session.commit()
    assert task.context[
        "_workday_consultation_recovery_high_watermark"
    ] >= source.sequence

    packet_context = dict(packet.context or {})
    packet_context["envelope"] = {
        **dict(packet_context["envelope"]),
        "snapshot_id": str(uuid.uuid4()),
    }
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == packet.id)
        .values(context=packet_context)
    )
    await db_session.commit()
    snapshot_ids_before_failure = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    counter_before_failure = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    counter_value_before_failure = counter_before_failure.last_sequence

    original_recover = handoff_recovery.recover_workday_request_by_source_id
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await original_recover(*args, **kwargs)
            raise ValueError("synthetic transient envelope pin failure")
        return await original_recover(*args, **kwargs)

    monkeypatch.setattr(
        handoff_recovery,
        "recover_workday_request_by_source_id",
        fail_once,
    )
    retired = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "empty"
    await db_session.refresh(task)
    assert task.context[
        "_workday_consultation_recovery_retry_source_ids"
    ] == [str(source.id)]
    failed_packet = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == packet.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert failed_packet.status == "resolved"
    assert failed_packet.handling_state == MessageHandlingState.SUPERSEDED
    assert set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    ) == snapshot_ids_before_failure
    counter_after_failure = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after_failure.last_sequence == counter_value_before_failure
    assert (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all() == []

    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    assert recovered.json()["continuation"]["consultation_id"] == str(
        source.id
    )
    await db_session.refresh(task)
    assert "_workday_consultation_recovery_retry_source_ids" not in task.context
    assert attempts == 2
    recovery_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all()
    assert len(recovery_rows) == 1
    counter_after_success = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after_success.last_sequence == counter_value_before_failure + 1


@pytest.mark.asyncio
async def test_recovery_retry_queue_rotates_persistent_failure(
    db_session, monkeypatch
):
    project, task = await _make_resident_poll_worker(db_session)
    blocked_source_id = uuid.uuid4()
    recoverable_source_id = uuid.uuid4()
    task.context = {
        **dict(task.context or {}),
        "_workday_consultation_recovery_retry_source_ids": [
            str(blocked_source_id),
            str(recoverable_source_id),
        ],
    }
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    sentinel = ProjectMessage(
        project_id=project.id,
        surface="conversation",
        from_type="system",
        content="bounded recovery sentinel",
        context={},
    )
    calls: list[uuid.UUID] = []

    async def recover_or_block(*args, source_id, **kwargs):
        calls.append(source_id)
        if source_id == blocked_source_id:
            raise ValueError("persistent synthetic conflict")
        return sentinel

    monkeypatch.setattr(
        handoff_recovery,
        "recover_workday_request_by_source_id",
        recover_or_block,
    )
    first = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=2,
        worker_incarnation=uuid.uuid4(),
        session_id="resident-session",
    )
    assert first is sentinel
    assert task.context[
        "_workday_consultation_recovery_retry_source_ids"
    ] == [str(blocked_source_id)]
    assert calls == [blocked_source_id, recoverable_source_id]


@pytest.mark.asyncio
async def test_persistent_retry_does_not_starve_broad_missing_source(
    client: AsyncClient, db_session, monkeypatch
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="broad-after-retry")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    blocked_source_id = uuid.uuid4()
    task.context = {
        **dict(task.context or {}),
        "_workday_consultation_recovery_retry_source_ids": [
            str(blocked_source_id)
        ],
    }
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    original_recover = handoff_recovery.recover_workday_request_by_source_id

    async def block_one(*args, source_id, **kwargs):
        if source_id == blocked_source_id:
            raise ValueError("persistent synthetic conflict")
        return await original_recover(*args, source_id=source_id, **kwargs)

    monkeypatch.setattr(
        handoff_recovery,
        "recover_workday_request_by_source_id",
        block_one,
    )
    recovered = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )

    assert recovered is not None
    assert recovered.context[
        "_workday_consultation_atomic_reply_id"
    ] == str(source.id)
    assert task.context[
        "_workday_consultation_recovery_retry_source_ids"
    ] == [str(blocked_source_id)]
