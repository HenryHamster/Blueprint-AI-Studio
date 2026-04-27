"""Tests for sync_runtime_config_from_state.

Cloud Run runs multiple instances; admin mutations are instance-local. The
sync helper pulls authoritative values from DeploymentState so each instance
converges. These tests cover the scalar cols, the JSONB column, the
PAUSED/HALTED override, and the changed-keys return value.
"""
import pytest

import app.shared_state as _shared_state
from app.models import DeploymentState, DeploymentStatus
from app.shared_state import sync_runtime_config_from_state


@pytest.fixture(autouse=True)
def restore_runtime_config():
    """Snapshot and restore runtime_config around each test so mutations
    don't leak between tests."""
    snap = dict(_shared_state.runtime_config)
    yield
    _shared_state.runtime_config.clear()
    _shared_state.runtime_config.update(snap)


@pytest.mark.asyncio
async def test_sync_returns_empty_for_none_state():
    changed = await sync_runtime_config_from_state(None)
    assert changed == []


@pytest.mark.asyncio
async def test_sync_scalar_cols_when_stable():
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_max_concurrent_vms=11,
        runtime_max_retries=7,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 3
    _shared_state.runtime_config["max_retries"] = 3

    changed = await sync_runtime_config_from_state(state)

    assert "max_concurrent_vms" in changed
    assert "max_retries" in changed
    assert _shared_state.runtime_config["max_concurrent_vms"] == 11
    assert _shared_state.runtime_config["max_retries"] == 7


@pytest.mark.asyncio
async def test_sync_forces_zero_when_paused_even_if_column_nonzero():
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.PAUSED,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        # pre_pause value — still persisted so resume can restore it — but
        # sync must override to 0 while status is PAUSED.
        runtime_max_concurrent_vms=8,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["max_concurrent_vms"] == 0
    assert "max_concurrent_vms" in changed


@pytest.mark.asyncio
async def test_sync_paused_does_not_bounce_max_concurrent_vms():
    """Regression: when status=PAUSED and the persisted column holds the
    pre-pause value (e.g. 15), repeated sync cycles must not flip the
    in-memory value 0→15→0. Previously the restore-then-override pattern
    left ``changed`` with ``["max_concurrent_vms", "max_concurrent_vms"]``
    and briefly exposed the pre-pause cap to lock-free readers."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.PAUSED,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_max_concurrent_vms=15,
    )
    # Steady state during pause: in-memory already 0 from prior sync.
    _shared_state.runtime_config["max_concurrent_vms"] = 0

    changed = await sync_runtime_config_from_state(state)

    # In-memory must stay 0 — the persisted 15 is for resume, not now.
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0
    # Must not double-list the key (was the source of the log spam).
    assert changed.count("max_concurrent_vms") <= 1


@pytest.mark.asyncio
async def test_sync_forces_zero_when_halted():
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.HALTED,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_max_concurrent_vms=8,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    await sync_runtime_config_from_state(state)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0


@pytest.mark.asyncio
async def test_sync_no_change_when_values_match():
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_max_concurrent_vms=5,
    )
    _shared_state.runtime_config["max_concurrent_vms"] = 5

    changed = await sync_runtime_config_from_state(state)
    assert "max_concurrent_vms" not in changed


@pytest.mark.asyncio
async def test_sync_applies_runtime_config_json():
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={
            "strategic_research_frequency": 9,
            "require_approval_gates": False,
            "max_provision_retries": 5,
            "provider_settings_defaults": {"anthropic": {"effort": "high"}},
            "task_type_provider_overrides": {"research": {"anthropic": {"effort": "max"}}},
        },
    )
    _shared_state.runtime_config["strategic_research_frequency"] = 6
    _shared_state.runtime_config["require_approval_gates"] = True
    _shared_state.runtime_config["max_provision_retries"] = 3
    _shared_state.runtime_config["provider_settings_defaults"] = {"anthropic": {"effort": "medium"}}
    _shared_state.runtime_config["task_type_provider_overrides"] = {}

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["strategic_research_frequency"] == 9
    assert _shared_state.runtime_config["require_approval_gates"] is False
    assert _shared_state.runtime_config["max_provision_retries"] == 5
    assert _shared_state.runtime_config["provider_settings_defaults"] == {"anthropic": {"effort": "high"}}
    assert _shared_state.runtime_config["task_type_provider_overrides"] == {"research": {"anthropic": {"effort": "max"}}}
    for key in (
        "strategic_research_frequency", "require_approval_gates",
        "max_provision_retries", "provider_settings_defaults", "task_type_provider_overrides",
    ):
        assert key in changed
