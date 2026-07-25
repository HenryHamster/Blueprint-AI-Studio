"""Tests for the agent-facing task-capability contract endpoint.

The agent derives its read_only/write task partition from the published backend
contract instead of a hand-maintained table. The operator-facing endpoint
(GET /api/admin/task-capabilities) is Bearer/Firebase-authed and unreachable
with the agent's X-Agent-Token JWT, so the agent consumes this mirror endpoint
authed via verify_agent_token.
"""

import pytest
from httpx import AsyncClient

from app.main import app
from app.routers.agent import verify_agent_token
from app.task_capabilities import (
    ALL_TASK_TYPES,
    agent_task_capability_contract,
    task_capability_contract,
)


@pytest.mark.asyncio
async def test_agent_task_capabilities_route_is_registered():
    paths = set(app.openapi()["paths"])
    assert "/api/agent/task-capabilities" in paths


@pytest.mark.asyncio
async def test_agent_task_capabilities_extends_published_contract(client: AsyncClient):
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": "task-123",
        "vm_instance_id": "agent-vm-caps",
    }
    try:
        resp = await client.get("/api/agent/task-capabilities")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"task_capabilities": agent_task_capability_contract()}
        contract = body["task_capabilities"]
        assert {key: contract[key] for key in ALL_TASK_TYPES} == (
            task_capability_contract()
        )
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_agent_task_capabilities_include_non_routable_legacy_tombstones(
    client: AsyncClient,
):
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": "task-123",
        "vm_instance_id": "agent-vm-caps",
    }
    try:
        resp = await client.get("/api/agent/task-capabilities")
        assert resp.status_code == 200, resp.text
        contract = resp.json()["task_capabilities"]
        for task_type in ("panel_review", "strategic_resident_turn"):
            assert task_type not in ALL_TASK_TYPES
            assert contract[task_type]["agent_mode"] == "read_only"
            assert contract[task_type]["filter_visible"] is False
            assert contract[task_type]["user_launchable"] is False
            assert contract[task_type]["provider_configurable"] is False
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_agent_task_capabilities_entries_carry_agent_mode(client: AsyncClient):
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": "task-123",
        "vm_instance_id": "agent-vm-caps",
    }
    try:
        resp = await client.get("/api/agent/task-capabilities")
        assert resp.status_code == 200, resp.text
        contract = resp.json()["task_capabilities"]
        # Every entry must declare a valid agent_mode — the agent's permission
        # partition rejects a contract that omits or mis-types it.
        assert contract, "contract must not be empty"
        for task_type, entry in contract.items():
            assert entry["agent_mode"] in (
                "read_only",
                "write",
            ), f"{task_type} has invalid agent_mode {entry.get('agent_mode')!r}"
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)
