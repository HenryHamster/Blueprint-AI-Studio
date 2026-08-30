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


@pytest.mark.asyncio
async def test_broad_recovery_savepoint_rolls_back_complete_partial_result(
    client: AsyncClient, db_session, monkeypatch
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="broad-savepoint")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
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
    counter_before = await db_session.get(ProjectMessageSequence, project.id)
    assert counter_before is not None
    counter_value_before = counter_before.last_sequence
    original_create = handoff_recovery._create_recovery_result

    async def create_then_fail(*args, **kwargs):
        await original_create(*args, **kwargs)
        raise ValueError("synthetic post-create failure")

    monkeypatch.setattr(
        handoff_recovery,
        "_create_recovery_result",
        create_then_fail,
    )
    recovered = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )

    assert recovered is None
    assert task.context[
        "_workday_consultation_recovery_retry_source_ids"
    ] == [str(source.id)]
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert snapshot_ids_after == snapshot_ids_before
    counter_after = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after.last_sequence == counter_value_before
    partial_results = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all()
    assert partial_results == []
    await db_session.commit()

    monkeypatch.setattr(
        handoff_recovery,
        "_create_recovery_result",
        original_create,
    )
    redriven = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert redriven is not None
    await db_session.commit()
    await db_session.refresh(task)
    assert "_workday_consultation_recovery_retry_source_ids" not in task.context
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
    counter_after_redrive = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after_redrive.last_sequence == counter_value_before + 1

    duplicate = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert duplicate is None
    await db_session.commit()
    final_counter = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert final_counter.last_sequence == counter_value_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("server-lineage", "result-content", "result-state"),
)
async def test_invalid_continuation_provenance_is_quarantined_without_starvation(
    client: AsyncClient, db_session, corruption: str
):
    project, task, incarnation, identity, source, packet = (
        await _start_workday_request(
            client,
            db_session,
            key=f"invalid-continuation-{corruption}",
        )
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "result whose source provenance will be corrupted",
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
    await _move_private_handoff_to_retired_session(
        db_session,
        result,
        retired_session_id="invalid-provenance-retired-session",
    )
    await db_session.refresh(result)
    canonical_result_context = dict(result.context or {})
    result_context = dict(canonical_result_context)
    poisoned_result_id = result.id
    corrupt_values: dict[str, object] = {"context": result_context}
    if corruption == "server-lineage":
        result_context.pop("source_packet_id", None)
        corrupt_values["idempotency_key"] = (
            f"workday-consultation-result:{packet.id}:{uuid.uuid4()}"
        )
    elif corruption == "result-content":
        corrupt_values["content"] = "private forged result text"
    else:
        result_context["result_state"] = "failed"
    # Direct SQL models a pre-fix/corrupted durable row without invoking the
    # normal privacy hook, which correctly purges derived packets whenever
    # application code mutates their governed context.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == poisoned_result_id)
        .values(**corrupt_values)
    )
    await db_session.commit()

    _second_source, second_packet = await _add_workday_request(
        client,
        db_session,
        project=project,
        task=task,
        incarnation=incarnation,
        identity=identity,
        key="good-behind-poisoned",
    )
    second_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert second_claim.status_code == 200, second_claim.text
    assert second_claim.json()["packet"]["packet_id"] == str(second_packet.id)
    second_workday_claim_id = second_claim.json()["packet"]["workday_claim_id"]
    second_ack = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"{second_packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": second_workday_claim_id,
            "outcome": "completed",
            "result_summary": "canonical second result",
        },
    )
    assert second_ack.status_code == 200, second_ack.text
    good_result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == (
                    f"workday-consultation-result:{second_packet.id}:"
                    f"{second_workday_claim_id}"
                ),
            )
        )
    ).scalars().one()
    snapshot_ids_before_delivery = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    before_delivery_ids = set(
        (
            await db_session.execute(
                select(ProjectMessage.id).where(
                    ProjectMessage.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert poisoned_result_id in before_delivery_ids

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["status"] == "claimed"
    assert continuation.json()["continuation"]["result_summary"] == (
        "canonical second result"
    )
    snapshot_ids_after_delivery = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    if corruption == "server-lineage":
        # With no independently derivable source, quarantine cannot authorize
        # even a neutral replacement snapshot.
        assert snapshot_ids_after_delivery == snapshot_ids_before_delivery
    else:
        # Exact server lineage can authorize one neutral replacement, but the
        # corrupted content/state must never authorize a rebind or projection.
        assert len(snapshot_ids_after_delivery - snapshot_ids_before_delivery) <= 1
    completed_good = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{good_result.id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": continuation.json()["continuation"][
                "continuation_claim_id"
            ],
            "content": "Delivered the canonical second result.",
            "idempotency_key": "complete-good-behind-poisoned",
        },
    )
    assert completed_good.status_code == 200, completed_good.text
    result_rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    result_by_id = {row.id: row for row in result_rows}
    assert poisoned_result_id in result_by_id, [
        (str(row.id), row.status, str(row.resolution)) for row in result_rows
    ]
    result = result_by_id[poisoned_result_id]
    assert result.status == "resolved"
    assert result.handling_state == MessageHandlingState.SUPERSEDED
    assert result.resolution == "private_handoff_continuation_provenance_invalid"

    recovery_rows = [
        row
        for row in result_rows
        if row.idempotency_key
        == f"workday-consultation-terminal-recovery:{source.id}"
    ]
    if corruption == "server-lineage":
        assert recovery_rows == []
    else:
        assert len(recovery_rows) == 1
        assert recovery_rows[0].content == handoff_recovery._RECOVERY_SUMMARY
    assert not any(
        str(row.idempotency_key or "").startswith(
            f"workday-consultation-terminal-recovery-replacement:{source.id}:"
        )
        for row in result_rows
    )
    recovered_missing = await _claim_continuation(client, project, task, identity)
    assert recovered_missing.status_code == 200, recovered_missing.text
    assert recovered_missing.json()["status"] == "claimed"
    recovery_body = recovered_missing.json()["continuation"]
    assert recovery_body["consultation_id"] == str(source.id)
    assert recovery_body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    completed_recovery = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{recovery_body['result_packet_id']}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": recovery_body["continuation_claim_id"],
            "content": "The unavailable private result was recovered neutrally.",
            "idempotency_key": "complete-poisoned-neutral-recovery",
        },
    )
    assert completed_recovery.status_code == 200, completed_recovery.text
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"
    assert "source provenance will be corrupted" not in continuation.text
    assert "private forged result text" not in continuation.text


@pytest.mark.asyncio
async def test_result_lineage_requires_exact_packet_and_ack_authority(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="lineage-authority")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "canonical authority-bound result",
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
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) == source.id
    lineage = await handoff_recovery.workday_result_server_lineage(
        db_session,
        row=result,
    )
    assert lineage is not None
    assert lineage.public_source_id == source.id
    assert lineage.authority_row_id == packet.id
    assert lineage.kind == "consultation_packet"

    # Result-owned fields never select receipt authority. The immutable server
    # key still resolves packet/source identity, while the continuation route
    # separately rejects this forged projection by exact equality.
    result_context = dict(result.context or {})
    result_context["source_packet_id"] = str(uuid.uuid4())
    result.context = result_context
    db_session.add(result)
    await db_session.flush()
    forged_lineage = await handoff_recovery.workday_result_server_lineage(
        db_session,
        row=result,
    )
    assert forged_lineage == lineage

    canonical_result_key = result.idempotency_key
    result.idempotency_key = (
        f"workday-consultation-result:{packet.id}:{uuid.uuid4()}"
    )
    db_session.add(result)
    await db_session.flush()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) is None

    result.idempotency_key = canonical_result_key
    packet.idempotency_key = f"forged-packet-authority:{packet.id}"
    db_session.add_all([result, packet])
    await db_session.flush()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("missing-source-packet", "result-content"),
)
async def test_invalid_terminal_recovery_is_replaced_neutrally_exactly_once(
    client: AsyncClient, db_session, corruption: str
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="invalid-terminal")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    original_recovery = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert original_recovery is not None
    await db_session.commit()
    original_recovery_id = original_recovery.id
    original_context = dict(original_recovery.context or {})
    corrupt_values: dict[str, object]
    if corruption == "missing-source-packet":
        original_context.pop("source_packet_id", None)
        corrupt_values = {"context": original_context}
    else:
        corrupt_values = {"content": "private forged terminal recovery text"}
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == original_recovery_id)
        .values(**corrupt_values)
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

    claimed = await _claim_continuation(client, project, task, identity)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    body = claimed.json()["continuation"]
    replacement_id = uuid.UUID(body["result_packet_id"])
    assert replacement_id != original_recovery_id
    assert body["consultation_id"] == str(source.id)
    assert body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    assert "private forged terminal recovery text" not in claimed.text
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    # Snapshot storage may deduplicate the exact same canonical authority.
    # Either way, validation happened before any rebind and can mint at most
    # the one replacement envelope.
    assert len(snapshot_ids_after - snapshot_ids_before) <= 1

    rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    quarantined = by_id[original_recovery_id]
    replacement = by_id[replacement_id]
    assert quarantined.status == "resolved"
    assert quarantined.handling_state == MessageHandlingState.SUPERSEDED
    assert quarantined.resolution == (
        "private_handoff_continuation_provenance_invalid"
    )
    assert replacement.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{original_recovery_id}"
    )
    assert replacement.context["source_packet_id"] == str(source.id)
    assert replacement.context["_workday_consultation_atomic_reply_id"] == str(
        source.id
    )
    assert replacement.context["delivery_key"] == "terminal-recovery"
    assert replacement.context["_workday_terminal_recovery"] is True
    public_continuations = [
        row
        for row in rows
        if row.from_type == "strategic"
        and (row.context or {}).get("message_role")
        == "workday_consultation_continuation"
    ]
    assert public_continuations == []

    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{replacement_id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": body["continuation_claim_id"],
            "content": "The private check failed safely; no outcome is verified.",
            "idempotency_key": f"complete-invalid-terminal:{source.id}",
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "created"
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"
    final_rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert len(
        [
            row
            for row in final_rows
            if row.from_type == "strategic"
            and (row.context or {}).get("message_role")
            == "workday_consultation_continuation"
            and (row.context or {}).get("_workday_consultation_atomic_reply_id")
            == str(source.id)
        ]
    ) == 1
    assert len(
        [
            row
            for row in final_rows
            if str(row.idempotency_key or "").startswith(
                "workday-consultation-terminal-recovery"
            )
        ]
    ) == 2


@pytest.mark.asyncio
async def test_terminal_recovery_replacement_requires_exact_root_chain(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="replacement-chain")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    root = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert root is not None
    await db_session.commit()
    root_context = dict(root.context or {})
    root_context.pop("source_packet_id", None)
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == root.id)
        .values(context=root_context)
    )
    await db_session.commit()

    first_claim = await _claim_continuation(client, project, task, identity)
    assert first_claim.status_code == 200, first_claim.text
    assert first_claim.json()["status"] == "claimed"
    first_id = uuid.UUID(
        first_claim.json()["continuation"]["result_packet_id"]
    )
    first = await db_session.get(ProjectMessage, first_id)
    assert first is not None
    assert first.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{root.id}"
    )

    # Force a second legitimate replacement generation. Its authority now
    # depends on the complete immutable predecessor chain, not merely the
    # immediate predecessor's source prefix.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == first_id)
        .values(content="private forged intermediate recovery text")
    )
    await db_session.commit()
    second_claim = await _claim_continuation(client, project, task, identity)
    assert second_claim.status_code == 200, second_claim.text
    assert second_claim.json()["status"] == "claimed"
    second_id = uuid.UUID(
        second_claim.json()["continuation"]["result_packet_id"]
    )
    assert second_id != first_id
    second = await db_session.get(ProjectMessage, second_id)
    assert second is not None
    assert second.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{first_id}"
    )

    # Corrupt the middle row's pointer to the root while preserving its source
    # prefix. A one-hop validator would accept the newest row; the bounded walk
    # must fail closed before resolving or rebinding its private envelope.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == first_id)
        .values(
            idempotency_key=(
                "workday-consultation-terminal-recovery-replacement:"
                f"{source.id}:{uuid.uuid4()}"
            )
        )
    )
    await db_session.commit()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=second,
    ) is None
    original_key = second.idempotency_key
    original_context = dict(second.context or {})
    second.idempotency_key = f"{original_key}:garbage"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.idempotency_key = (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:not-a-uuid"
    )
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.idempotency_key = original_key
    second.context = {
        **original_context,
        "source_packet_id": str(uuid.uuid4()),
    }
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.context = {
        **original_context,
        "_workday_consultation_atomic_reply_id": str(uuid.uuid4()),
    }
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.context = original_context
    second.status = "resolved"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.status = "open"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) == source.id
    snapshot_ids_before = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    recovery_body = recovered.json()["continuation"]
    assert recovery_body["result_packet_id"] != str(second_id)
    assert recovery_body["consultation_id"] == str(source.id)
    assert recovery_body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    replacement = await db_session.get(
        ProjectMessage,
        uuid.UUID(recovery_body["result_packet_id"]),
    )
    assert replacement is not None
    assert replacement.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{root.id}"
    )
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert len(snapshot_ids_after - snapshot_ids_before) <= 1
    await db_session.refresh(second)
    assert second.status == "resolved"
    assert second.handling_state == MessageHandlingState.SUPERSEDED
    assert second.resolution == "private_handoff_continuation_provenance_invalid"
    assert "private forged intermediate recovery text" not in recovered.text
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{recovery_body['result_packet_id']}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": recovery_body["continuation_claim_id"],
            "content": "The corrupt replacement chain was recovered neutrally.",
            "idempotency_key": "complete-exact-root-chain-recovery",
        },
    )
    assert completed.status_code == 200, completed.text
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 64])
async def test_missing_recovery_select_count_is_constant_across_candidate_cardinality(
    db_session,
    candidate_count: int,
):
    project, task = await _make_resident_poll_worker(db_session)
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface="conversation",
        from_type="user",
        content="canonical covered input",
        context={},
    )
    db_session.add(inbound)
    await db_session.flush()
    sources = [
        ProjectMessage(
            project_id=project.id,
            sequence=index + 2,
            surface="conversation",
            from_type="strategic",
            reply_to_message_id=inbound.id,
            resident_session_id="resident-session",
            content=f"source {index}",
            context={
                "message_role": "operator_reply",
                "_workday_consultation_requested": True,
                "_conversation_public_reply_owner": "fast",
                # Keep the row query-eligible but semantically unrecoverable,
                # avoiding any side-effect path in this SELECT-count test.
                "privacy_state": "redacted",
            },
        )
        for index in range(candidate_count)
    ]
    counter = await db_session.get(ProjectMessageSequence, project.id)
    if counter is None:
        counter = ProjectMessageSequence(project_id=project.id)
    counter.last_sequence = candidate_count + 1
    db_session.add_all([*sources, counter])
    await db_session.commit()
    project_id = project.id
    task_id = task.id
    db_session.expunge_all()
    project = await db_session.get(Project, project_id)
    task = await db_session.get(type(task), task_id)
    assert project is not None and task is not None

    statements: list[str] = []

    def _record_statement(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db_session.bind
    event.listen(engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        recovered = await recover_one_missing_workday_request(
            db_session,
            project=project,
            task=task,
            worker_generation=2,
            worker_incarnation=uuid.uuid4(),
            session_id="resident-session",
        )
    finally:
        event.remove(
            engine.sync_engine,
            "before_cursor_execute",
            _record_statement,
        )

    assert recovered is None
    assert len(statements) == 4, statements
    if engine.dialect.name == "postgresql":
        candidate_statement = next(
            statement
            for statement in statements
            if "_workday_consultation_requested" in statement
        )
        linked_statement = next(
            statement
            for statement in statements
            if "_workday_consultation_atomic_reply_id" in statement
        )
        assert (
            "projectmessage.sequence IS NOT NULL AND "
            "projectmessage.from_type = 'strategic' AND "
            "projectmessage.surface = 'conversation' AND "
            "projectmessage.context @> "
            "'{\"_workday_consultation_requested\": true}'::jsonb"
        ) in candidate_statement
        assert (
            "projectmessage.context ? "
            "'_workday_consultation_atomic_reply_id'"
        ) in linked_statement
        assert (
            "projectmessage.context ->> "
            "'_workday_consultation_atomic_reply_id'"
        ) in linked_statement
    if candidate_count == 64:
        assert task.context[
            "_workday_consultation_recovery_before_sequence"
        ] == 34
        assert task.context[
            "_workday_consultation_recovery_scan_ceiling"
        ] == 65


@pytest.mark.asyncio
async def test_retired_session_missing_handoff_recovers_exactly_once(
    client: AsyncClient, db_session
):
    """A missing packet remains recoverable after its Resident is replaced."""

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retired-missing")
    )
    assert await _canonical_source_receipt(db_session, source=source) is not None
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
    replacement_session_id = "replacement-resident-session"
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
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    fresh_source = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == source.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert fresh_source.status == "resolved"
    assert fresh_source.handling_state == MessageHandlingState.RESPONDED
    assert fresh_source.resident_session_id == origin_session_id
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in fresh_source.context
    receipt = (
        await db_session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == fresh_source.project_id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(fresh_source.id),
            )
        )
    ).scalars().one()
    assert receipt.resident_session_id == origin_session_id
    assert receipt.source_revision == str(fresh_source.sequence)
    assert receipt.native_sequence == fresh_source.sequence
    assert receipt.content_hash == hashlib.sha256(
        fresh_source.content.encode("utf-8")
    ).hexdigest()
    assert receipt.payload_state == "active"
    assert receipt.tombstoned_at is None
    assert receipt.actor_kind == "strategic"
    assert (receipt.source_metadata or {}).get("surface") == "conversation"
    assert _source_shape_is_canonical(fresh_source)
    assert await _canonical_source_receipt(
        db_session,
        source=fresh_source,
    ) is not None
    assert not await _source_is_settled_or_live(
        db_session,
        source=fresh_source,
    )

    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    continuation = recovered.json()["continuation"]
    assert continuation["consultation_id"] == str(source.id)
    assert "became unavailable" in continuation["result_summary"]
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
    assert recovery_rows[0].resident_session_id == identity["session_id"]

    duplicate = await _claim_continuation(client, project, task, identity)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "claimed"
    assert duplicate.json()["continuation"]["result_packet_id"] == (
        continuation["result_packet_id"]
    )
    assert len(
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key
                    == f"workday-consultation-terminal-recovery:{source.id}",
                )
            )
        ).scalars().all()
    ) == 1


@pytest.mark.asyncio
async def test_operator_turn_park_is_committed_and_returned_by_the_claim_route(
    client: AsyncClient, db_session
):
    """A park must reach the operator AND survive the request.

    Incident: operator conversation messages sat at handling_state='claimed'
    forever, held by a task that reported "waiting for operator input". The
    coordinator now DECIDES instead of re-handing out a turn whose claim has
    died _OPERATOR_TURN_MAX_DEAD_CLAIMS times -- but the decision is written
    into the request's session, so a route that rolled back on the raised
    OperatorTurnParked would throw the answer away and re-enter the same loop
    on the next poll. This pins the commit and the reason-bearing 409.

    Lives here rather than in test_strategic_session_messages.py (which owns
    the route) only because that module is at its module_size_limits.json cap.
    """
    project, task = await _make_resident_poll_worker(db_session)
    project_id = project.id
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    inbound.context = {
        **dict(inbound.context or {}),
        _OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY: _OPERATOR_TURN_MAX_DEAD_CLAIMS,
    }
    db_session.add(inbound)
    await db_session.commit()

    parked = await client.post(
        f"/api/agent/projects/{project_id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )

    assert parked.status_code == 409, parked.text
    # Surface reasons, not states: never a bare "already claimed" 409.
    detail = parked.json()["error"]["message"]
    assert "could not process this message" in detail
    assert "Send it again to retry" in detail

    await db_session.refresh(inbound)
    assert inbound.handling_state == MessageHandlingState.RESPONDED
    assert inbound.status == "handled"
    reply = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "strategic",
                    ProjectMessage.content == detail,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(reply) == 1
