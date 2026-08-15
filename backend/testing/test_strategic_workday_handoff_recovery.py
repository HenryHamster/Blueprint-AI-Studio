"""Terminal recovery regressions for typed Workday consultation handoffs."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlmodel import select

from app.models import MessageHandlingState, ProjectMessage
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


def _claim_body(identity: dict) -> dict:
    return {**identity, "lease_seconds": 45}


async def _claim_continuation(client, project, task, identity):
    return await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        "continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )


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
    await db_session.delete(packet)
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
