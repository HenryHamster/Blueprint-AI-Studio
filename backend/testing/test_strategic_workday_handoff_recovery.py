"""Terminal recovery regressions for typed Workday and operator handoffs."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlmodel import select

from app.conversation_coordinator import (
    _OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY,
    _OPERATOR_TURN_MAX_DEAD_CLAIMS,
)
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

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retired-session")
    )
    packet.resident_session_id = "retired-resident-session"
    db_session.add(packet)
    await db_session.commit()

    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    claimed = packet_claim.json()["packet"]
    assert packet_claim.json()["status"] == "claimed"
    assert claimed["packet_id"] == str(packet.id)
    await db_session.refresh(packet)
    assert packet.resident_session_id == identity["session_id"]

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
    result.resident_session_id = "retired-resident-session"
    db_session.add(result)
    await db_session.commit()

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    body = continuation.json()["continuation"]
    assert continuation.json()["status"] == "claimed"
    assert body["result_packet_id"] == str(result.id)
    assert body["consultation_id"] == str(source.id)
    await db_session.refresh(result)
    assert result.resident_session_id == identity["session_id"]


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
