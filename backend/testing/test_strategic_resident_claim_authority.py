"""Claim-token fencing for authoritative Strategic Resident turns."""

from __future__ import annotations

import uuid

import pytest

from app import max_plan_pool
from app.auth import create_agent_token, verify_agent_jwt
from app.models import Project, StrategicResident, Task, TaskStatus
from app.strategic_resident_agent_binding import RESIDENT_CLAIM_NONCE_CONTEXT_KEY
from testing._resident_seed import seed_without_authority_enforcement


async def _seed_claimable_turn(db_session):
    project = Project(
        name=f"Resident claim {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    resident = StrategicResident(
        project_id=project.id,
        engine_owner="strategic_resident",
        engine_version="resident-v2",
        authority_generation=7,
    )
    task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        vm_instance_id="resident-claim-vm",
        payloads=[
            {
                "type": "strategic_resident_turn",
                "resident_id": str(project.id),
                "lane": "interactive",
                "authority_generation": 7,
                "engine_owner": "strategic_resident",
                "shadow_only": False,
                "trigger_kind": "operator_message",
                "checkpoint_version": 1,
                "checkpoint_hash": "a" * 64,
                "message_sequence_start": 1,
                "message_sequence_end": 1,
            }
        ],
        context={
            "_resident_mutation_authorized": True,
            "_resident_shadow_only": False,
            "_resident_input_frozen": True,
            "_resident_project_id": str(project.id),
            "_resident_authority_generation": 7,
            "_resident_engine_owner_observed": "strategic_resident",
        },
    )
    db_session.add(resident)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    resident.active_turn_task_id = task.id
    resident.active_turn_lane = "interactive"
    db_session.add(resident)
    await db_session.commit()
    token = create_agent_token(
        str(task.id),
        str(task.vm_instance_id),
        task_type="strategic_resident_turn",
        project_id=str(project.id),
    )
    return project, resident, task, token


@pytest.mark.asyncio
async def test_claim_rotates_private_nonce_and_replays_without_duplication(
    client,
    db_session,
):
    _project, _resident, task, bootstrap_token = await _seed_claimable_turn(
        db_session
    )
    headers = {"x-agent-token": bootstrap_token}

    first = await client.post("/api/agent/claim", headers=headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    first_claims = verify_agent_jwt(first_body["agent_token"])
    first_nonce = first_claims["claim_nonce"]
    assert first_nonce
    assert RESIDENT_CLAIM_NONCE_CONTEXT_KEY not in first_body["context"]
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY] == first_nonce

    replay = await client.post("/api/agent/claim", headers=headers)
    assert replay.status_code == 200, replay.text
    replay_claims = verify_agent_jwt(replay.json()["agent_token"])
    assert replay_claims["claim_nonce"] == first_nonce

    task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()
    reclaimed = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": first_body["agent_token"]},
    )
    assert reclaimed.status_code == 200, reclaimed.text
    second_token = reclaimed.json()["agent_token"]
    second_nonce = verify_agent_jwt(second_token)["claim_nonce"]
    assert second_nonce != first_nonce

    superseded_bootstrap = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": bootstrap_token},
    )
    assert superseded_bootstrap.status_code == 409
    for path in (
        f"/api/agent/github/token?task_id={task.id}",
        "/api/agent/git-read-token",
    ):
        denied = await client.get(path, headers={"x-agent-token": second_token})
        assert denied.status_code == 403
        assert denied.json()["error"]["message"] == "resident_endpoint_not_allowed"

    stale_heartbeat = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "status_message": "stale claim"},
        headers={"x-agent-token": first_body["agent_token"]},
    )
    assert stale_heartbeat.status_code == 403
    stale_status = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "FAILED"},
        headers={"x-agent-token": first_body["agent_token"]},
    )
    assert stale_status.status_code == 403
    current_status = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "FAILED"},
        headers={"x-agent-token": second_token},
    )
    assert current_status.status_code == 403
    assert current_status.json()["error"]["message"] == (
        "resident_endpoint_not_allowed"
    )

    stale_refresh = await client.post(
        "/api/agent/refresh-token",
        headers={"x-agent-token": first_body["agent_token"]},
    )
    assert stale_refresh.status_code == 403
    current_refresh = await client.post(
        "/api/agent/refresh-token",
        headers={"x-agent-token": second_token},
    )
    assert current_refresh.status_code == 200, current_refresh.text
    assert verify_agent_jwt(current_refresh.json()["token"])["claim_nonce"] == (
        second_nonce
    )

    await db_session.refresh(task)
    task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()
    third = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": second_token},
    )
    assert third.status_code == 200, third.text
    third_nonce = verify_agent_jwt(third.json()["agent_token"])["claim_nonce"]
    assert third_nonce not in {first_nonce, second_nonce}
    oldest_replay = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": first_body["agent_token"]},
    )
    assert oldest_replay.status_code == 409
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_claim_projects_server_derived_resident_runtime_provider(
    client,
    db_session,
):
    _project, _resident, task, token = await _seed_claimable_turn(db_session)
    payload = dict(task.payloads[0])
    payload["_resident_expected_runtime_provider"] = "openai"
    task.payloads = [payload]
    task.context = {**task.context, "llm_provider": "Anthropic"}
    db_session.add(task)
    await db_session.commit()

    response = await client.post(
        "/api/agent/claim", headers={"x-agent-token": token}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payloads"][0]["_resident_expected_runtime_provider"] == "anthropic"
    assert "llm_provider" not in body["context"]


@pytest.mark.asyncio
async def test_bounded_failure_clears_pointer_and_replays_idempotently(
    client,
    db_session,
    monkeypatch,
):
    _project, resident, task, bootstrap = await _seed_claimable_turn(db_session)
    task.context = {**(task.context or {}), "llm_account_id": "resident-fail-account"}
    task.llm_account_id = "resident-fail-account"
    db_session.add(task)
    await db_session.commit()
    account = {
        "id": "resident-fail-account",
        "name": "Resident fail",
        "active_tasks": 1,
        "project_state": {str(task.project_id): {"active_tasks": 1}},
    }
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "resident-fail-account",
        account,
    )
    claim = await client.post(
        "/api/agent/claim", headers={"x-agent-token": bootstrap}
    )
    assert claim.status_code == 200, claim.text
    token = claim.json()["agent_token"]
    body = {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "failure_id": "failure-1",
        "reason": "provider exited before returning a structured result",
        "failure_category": "rate_limited",
    }
    failed = await client.post(
        "/api/agent/resident-turn/fail",
        json=body,
        headers={"x-agent-token": token},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["idempotent_replay"] is False
    await db_session.refresh(task)
    await db_session.refresh(resident)
    assert task.status == TaskStatus.FAILED
    assert task.context["_resident_failure"]["failure_id"] == "failure-1"
    assert "agent_error_report" not in task.context
    assert task.context["_slot_released"] is True
    assert account["active_tasks"] == 0
    assert account["project_state"][str(task.project_id)]["active_tasks"] == 0
    assert resident.active_turn_task_id is not None
    assert resident.active_turn_lane == "interactive"
    retry = await db_session.get(Task, resident.active_turn_task_id)
    assert retry is not None
    assert retry.retry_after_until is not None
    assert retry.first_payload["message_sequence_start"] == 1
    assert retry.first_payload["message_sequence_end"] == 1
    assert resident.next_wakeup_at is None

    replay = await client.post(
        "/api/agent/resident-turn/fail",
        json=body,
        headers={"x-agent-token": token},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert account["active_tasks"] == 0


@pytest.mark.asyncio
async def test_failure_sanitizes_report_and_cleans_retry_context(
    client,
    db_session,
):
    _project, resident, task, bootstrap = await _seed_claimable_turn(db_session)

    claim = await client.post(
        "/api/agent/claim", headers={"x-agent-token": bootstrap}
    )
    report = {
        "schema": 1,
        "task_id": str(task.id),
        "provider": "openai",
        "auth_mode": "oauth",
        "exit_code": 1,
        "exit_signal": None,
        "duration_ms": 123,
        "model_output_seen": False,
        "result_event_seen": False,
        "provider_errors": [
            {
                "source": "codex-turn-failed",
                "code": "startup_error",
                "message": f"token=abcdefgh12345678 {'x' * 700}",
            }
            for _ in range(8)
        ],
        "stderr_tail": (
            f"{'x' * 3_000} missing optional dependency "
            "@openai/codex-linux-x64 token=abcdefgh12345678"
        ),
        "last_stream_event": "turn.failed",
        "unexpected": {"nested": "discard me"},
    }
    failed = await client.post(
        "/api/agent/resident-turn/fail",
        json={
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "failure_id": "structured-provider-failure",
            "reason": "Resident provider exited with code 1",
            "failure_category": "fatal",
            "error_report": report,
        },
        headers={"x-agent-token": claim.json()["agent_token"]},
    )
    assert failed.status_code == 200, failed.text
    await db_session.refresh(task)
    await db_session.refresh(resident)

    stored = task.context["agent_error_report"]
    assert stored["exit_code"] == 1
    assert len(stored["stderr_tail"]) <= 2_000
    assert "abcdefgh12345678" not in stored["stderr_tail"]
    assert len(stored["provider_errors"]) == 5
    assert all(len(row["message"]) <= 500 for row in stored["provider_errors"])
    assert all(
        "abcdefgh12345678" not in row["message"]
        for row in stored["provider_errors"]
    )
    assert "unexpected" not in stored
    marker = task.context["_resident_failure"]
    assert marker["category"] == "fatal"
    assert "raw_category" not in marker
    assert "failure_class" not in marker
    assert resident.recovery_state["failure_category"] == "fatal"
    assert "failure_class" not in resident.recovery_state

    retry = await db_session.get(Task, resident.active_turn_task_id)
    assert retry is not None
    assert "agent_error_report" not in (retry.context or {})

@pytest.mark.asyncio
async def test_failure_promotes_interactive_successor_while_background_paused(
    client,
    db_session,
):
    _project, resident, task, bootstrap = await _seed_claimable_turn(db_session)
    resident.background_policy = "paused"
    successor = Task(
        project_id=task.project_id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "strategic_resident_turn",
                "resident_id": str(task.project_id),
                "lane": "interactive",
                "authority_generation": 7,
                "engine_owner": "strategic_resident",
                "shadow_only": False,
                "trigger_kind": "operator_message",
                "checkpoint_version": 1,
                "checkpoint_hash": "a" * 64,
                "message_sequence_start": 1,
                "message_sequence_end": 1,
            }
        ],
        context={
            "_resident_mutation_authorized": True,
            "_resident_shadow_only": False,
            "_resident_input_frozen": False,
            "_resident_project_id": str(task.project_id),
            "_resident_authority_generation": 7,
            "_resident_engine_owner_observed": "strategic_resident",
        },
    )
    db_session.add(successor)
    await db_session.flush()
    resident.interactive_successor_task_id = successor.id
    db_session.add(resident)
    await db_session.commit()
    claim = await client.post(
        "/api/agent/claim", headers={"x-agent-token": bootstrap}
    )
    token = claim.json()["agent_token"]
    body = {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "failure_id": "failure-with-successor",
        "reason": "provider failed",
        "failure_category": "fatal",
    }
    failed = await client.post(
        "/api/agent/resident-turn/fail",
        json=body,
        headers={"x-agent-token": token},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["promoted_task_id"] == str(successor.id)
    await db_session.refresh(resident)
    await db_session.refresh(successor)
    assert resident.active_turn_task_id == successor.id
    assert resident.active_turn_lane == "interactive"
    assert resident.interactive_successor_task_id is None
    assert successor.context["_resident_input_frozen"] is True
    original_promoted_id = failed.json()["promoted_task_id"]
    replacement = Task(
        project_id=task.project_id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "memo"}],
    )
    db_session.add(replacement)
    await db_session.flush()
    resident.active_turn_task_id = replacement.id
    resident.active_turn_lane = "background"
    db_session.add(resident)
    await db_session.commit()

    replay = await client.post(
        "/api/agent/resident-turn/fail",
        json=body,
        headers={"x-agent-token": token},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["promoted_task_id"] == original_promoted_id


@pytest.mark.asyncio
async def test_failure_redacts_secrets_and_stops_after_retry_limit(
    client,
    db_session,
):
    _project, resident, task, bootstrap = await _seed_claimable_turn(db_session)
    resident.health_state = {"consecutive_turn_failures": 4}
    db_session.add(resident)
    await db_session.commit()
    claim = await client.post(
        "/api/agent/claim", headers={"x-agent-token": bootstrap}
    )
    body = {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "failure_id": "secret-failure",
        "reason": "provider token=abcdefgh12345678 was rejected",
        "failure_category": "fatal",
    }
    failed = await client.post(
        "/api/agent/resident-turn/fail",
        json=body,
        headers={"x-agent-token": claim.json()["agent_token"]},
    )
    assert failed.status_code == 200, failed.text
    await db_session.refresh(task)
    await db_session.refresh(resident)
    assert "abcdefgh12345678" not in task.context["_resident_failure"]["reason"]
    assert "token=***" in task.context["_resident_failure"]["reason"]
    assert resident.health_state["consecutive_turn_failures"] == 5
    assert resident.recovery_state["status"] == "attention"
    assert resident.next_wakeup_at is None
    assert resident.health_status == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_boundary", ["owner", "generation", "pointer"])
async def test_claim_rejects_stale_live_resident_boundary_without_mutation(
    client,
    db_session,
    stale_boundary,
):
    from app.warm_vm_state import vm_last_heartbeat

    _project, resident, task, token = await _seed_claimable_turn(db_session)
    vm_last_heartbeat.pop(str(task.vm_instance_id), None)
    # Seeding a deliberately-stale boundary: reverting engine_owner to 'legacy'
    # without advancing the generation is a state the authority-monotonic fence
    # forbids in production (a real rollback advances the generation). We create
    # it artificially to isolate the claim-time owner-boundary check. Enforcement
    # is relaxed for the seed only (no-op on SQLite); the /api/agent/claim action
    # under test runs with all fences ACTIVE.
    async with seed_without_authority_enforcement(db_session):
        if stale_boundary == "owner":
            resident.engine_owner = "legacy"
        elif stale_boundary == "generation":
            resident.authority_generation += 1
        else:
            resident.active_turn_task_id = None
            resident.active_turn_lane = None
        db_session.add(resident)
        await db_session.commit()

    response = await client.post(
        "/api/agent/claim",
        headers={"x-agent-token": token},
    )
    assert response.status_code == 409
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert RESIDENT_CLAIM_NONCE_CONTEXT_KEY not in task.context
    assert str(task.vm_instance_id) not in vm_last_heartbeat


@pytest.mark.asyncio
async def test_resident_websocket_rejects_non_log_frame(mocker):
    import json
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import WebSocketDisconnect

    import app.routers.projects as projects_module

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    nonce = "current-resident-nonce"
    token = create_agent_token(
        str(task_id),
        "resident-ws-vm",
        task_type="strategic_resident_turn",
        project_id=str(project_id),
        claim_nonce=nonce,
    )
    task = MagicMock(
        id=task_id,
        project_id=project_id,
        vm_instance_id="resident-ws-vm",
        status=TaskStatus.IN_PROGRESS,
        context={RESIDENT_CLAIM_NONCE_CONTEXT_KEY: nonce},
    )
    execute_result = MagicMock()
    execute_result.scalars.return_value.first.return_value = task
    session = AsyncMock()
    session.execute.return_value = execute_result
    session.get.return_value = task
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    mocker.patch(
        "app.routers.projects.AsyncSessionLocal", return_value=session_cm
    )
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    emit = mocker.patch(
        "app.routers.projects.emit_strategic_session_delta",
        new_callable=AsyncMock,
    )
    ws = AsyncMock()
    ws.receive_json.return_value = {"token": token}
    ws.receive_text.side_effect = [
        json.dumps({"type": "strategic_session", "payload": {"text": "escape"}}),
        WebSocketDisconnect(),
    ]

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_awaited_once_with(
        code=1008, reason="Resident frame type is not authorized"
    )
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_resident_websocket_closes_after_nonce_rotation(mocker):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import app.routers.projects as projects_module

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = create_agent_token(
        str(task_id),
        "resident-ws-vm",
        task_type="strategic_resident_turn",
        project_id=str(project_id),
        claim_nonce="old-nonce",
    )
    initial = MagicMock(
        id=task_id,
        project_id=project_id,
        vm_instance_id="resident-ws-vm",
        status=TaskStatus.IN_PROGRESS,
        context={RESIDENT_CLAIM_NONCE_CONTEXT_KEY: "old-nonce"},
    )
    rotated = MagicMock(
        id=task_id,
        project_id=project_id,
        vm_instance_id="resident-ws-vm",
        status=TaskStatus.IN_PROGRESS,
        context={RESIDENT_CLAIM_NONCE_CONTEXT_KEY: "new-nonce"},
    )
    execute_result = MagicMock()
    execute_result.scalars.return_value.first.return_value = initial
    session = AsyncMock()
    session.execute.return_value = execute_result
    session.get.return_value = rotated
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    mocker.patch(
        "app.routers.projects.AsyncSessionLocal", return_value=session_cm
    )
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")

    pending = asyncio.Event()

    async def _no_frame():
        await pending.wait()
        return ""

    async def _timer_tick(_seconds):
        return None

    mocker.patch("app.routers.projects.asyncio.sleep", new=_timer_tick)
    ws = AsyncMock()
    ws.receive_json.return_value = {"token": token}
    ws.receive_text.side_effect = _no_frame

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_awaited_once_with(
        code=1008, reason="Task is no longer active"
    )
    assert session.get.await_count >= 1
    pending.set()
