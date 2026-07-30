"""Focused coverage for discovery-cycle admission rollout configuration."""

import pytest
from httpx import AsyncClient

import app.shared_state as _shared_state
from app.models import DeploymentState
from app.runtime_config_state import sync_runtime_config_from_state


def test_discovery_admission_mode_defaults_to_shadow():
    assert _shared_state.runtime_config["discovery_admission_mode"] == "shadow"


@pytest.mark.asyncio
async def test_discovery_admission_mode_update_persists_and_restores(
    client: AsyncClient, db_session
):
    db_session.add(DeploymentState())
    await db_session.commit()

    response = await client.patch(
        "/api/admin/settings",
        json={"discovery_admission_mode": "enforced"},
    )
    assert response.status_code == 200, response.text
    assert _shared_state.runtime_config["discovery_admission_mode"] == "enforced"

    settings = (await client.get("/api/admin/settings")).json()
    assert settings["discovery_admission_mode"] == "enforced"

    state = await db_session.get(DeploymentState, 1)
    assert state is not None
    assert state.runtime_config_json is not None
    assert state.runtime_config_json["discovery_admission_mode"] == "enforced"

    _shared_state.runtime_config["discovery_admission_mode"] = "shadow"
    changed = await sync_runtime_config_from_state(state)
    assert "discovery_admission_mode" in changed
    assert _shared_state.runtime_config["discovery_admission_mode"] == "enforced"


@pytest.mark.asyncio
async def test_discovery_admission_mode_rejects_invalid_value(
    client: AsyncClient,
):
    original = _shared_state.runtime_config["discovery_admission_mode"]

    response = await client.patch(
        "/api/admin/settings",
        json={"discovery_admission_mode": "enforce"},
    )

    assert response.status_code == 400
    assert "shadow, enforced" in response.text
    assert _shared_state.runtime_config["discovery_admission_mode"] == original
