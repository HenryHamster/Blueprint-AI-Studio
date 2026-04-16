"""Standalone tests for anomaly-driven pipeline adjustments.

Tests cover:
- Automatic adjustment when high-severity surges are detected
- Restoration when anomalies clear
- Interaction with manual admin overrides
- Failure gate threshold integration

Run with: bash scripts/test-backend.sh testing/test_anomaly_actions.py --standalone
"""
import uuid
from copy import deepcopy

import pytest

import app.shared_state as _shared_state
from app.anomaly_detection import (
    _apply_anomaly_adjustments,
    _check_anomaly_restoration,
    check_anomaly_actions,
    _ACTIONABLE_FAILURE_CLASSES,
    _ANOMALY_ACTION_THRESHOLD,
)


_DEFAULT_ADJUSTMENTS = {
    "active": False,
    "adjusted_at": None,
    "trigger_anomalies": [],
    "pre_adjustment_max_vms": None,
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,
}


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset shared state before each test."""
    _shared_state.persistent_anomalies.clear()
    _shared_state.anomaly_adjustments.update(deepcopy(_DEFAULT_ADJUSTMENTS))
    original_max_vms = _shared_state.runtime_config["max_concurrent_vms"]
    yield
    _shared_state.persistent_anomalies.clear()
    _shared_state.anomaly_adjustments.update(deepcopy(_DEFAULT_ADJUSTMENTS))
    _shared_state.runtime_config["max_concurrent_vms"] = original_max_vms


# ─────────────────────────────────────────────────────────────────────────────
# _apply_anomaly_adjustments
# ─────────────────────────────────────────────────────────────────────────────


def test_no_adjustment_without_anomalies():
    """No adjustments when persistent_anomalies is empty."""
    _shared_state.runtime_config["max_concurrent_vms"] = 8
    result = _apply_anomaly_adjustments()
    assert result is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8
    assert _shared_state.anomaly_adjustments["active"] is False


def test_no_adjustment_for_low_occurrence():
    """Anomalies below the occurrence threshold don't trigger adjustments."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 2,  # Below threshold of 3
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8
    result = _apply_anomaly_adjustments()
    assert result is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8


def test_no_adjustment_for_medium_severity():
    """Only high severity anomalies trigger adjustments."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "medium",
            "occurrence_count": 5,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8
    result = _apply_anomaly_adjustments()
    assert result is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8


def test_no_adjustment_for_non_actionable_class():
    """CODE_ERROR and other non-actionable classes don't trigger adjustments."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "CODE_ERROR": {
            "severity": "high",
            "occurrence_count": 5,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8
    result = _apply_anomaly_adjustments()
    assert result is False


def test_apply_adjustment_on_provision_failure_surge():
    """PROVISION_FAILURE with high severity and count >= 3 triggers adjustments."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "Provision failures surging",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    result = _apply_anomaly_adjustments()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is True
    # max_vms reduced by 50%: 8 // 2 = 4
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4
    # Failure gate tightened
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.6
    # Pre-adjustment values stored
    assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] == 8
    assert _shared_state.anomaly_adjustments["pre_adjustment_failure_gate"] == 0.8
    assert "PROVISION_FAILURE" in _shared_state.anomaly_adjustments["trigger_anomalies"]


def test_apply_adjustment_on_external_api_surge():
    """EXTERNAL_API with high severity and count >= 3 also triggers adjustments."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "EXTERNAL_API": {
            "severity": "high",
            "occurrence_count": 4,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "API failures surging",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 10

    result = _apply_anomaly_adjustments()
    assert result is True
    # 10 // 2 = 5
    assert _shared_state.runtime_config["max_concurrent_vms"] == 5
    assert "EXTERNAL_API" in _shared_state.anomaly_adjustments["trigger_anomalies"]


def test_apply_adjustment_floor_of_2():
    """max_concurrent_vms never drops below 2."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 3

    _apply_anomaly_adjustments()
    # 3 // 2 = 1, but floor is 2
    assert _shared_state.runtime_config["max_concurrent_vms"] == 2


def test_apply_already_active_no_double_adjust():
    """If adjustments are already active, don't re-adjust — just update trigger list."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    # First adjustment
    _apply_anomaly_adjustments()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    # Add another anomaly type
    _shared_state.persistent_anomalies[proj_id]["EXTERNAL_API"] = {
        "severity": "high",
        "occurrence_count": 5,
        "first_seen": "2026-04-16T00:00:00",
        "last_seen": "2026-04-16T01:00:00",
        "latest_details": "test",
    }

    # Second call should not further reduce VMs
    result = _apply_anomaly_adjustments()
    assert result is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4
    assert sorted(_shared_state.anomaly_adjustments["trigger_anomalies"]) == [
        "EXTERNAL_API", "PROVISION_FAILURE"
    ]


# ─────────────────────────────────────────────────────────────────────────────
# _check_anomaly_restoration
# ─────────────────────────────────────────────────────────────────────────────


def test_restoration_when_anomalies_clear():
    """Values are restored when all triggering anomalies drop below threshold."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    # Apply adjustment
    _apply_anomaly_adjustments()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    # Simulate anomaly clearing (occurrence count reset to 0 by _check_anomaly_proposals)
    _shared_state.persistent_anomalies[proj_id]["PROVISION_FAILURE"]["occurrence_count"] = 0

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8
    assert _shared_state.anomaly_adjustments["active"] is False


def test_restoration_when_anomalies_expired():
    """Values are restored when anomalies are expired (removed from dict)."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    _apply_anomaly_adjustments()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    # Simulate 48h expiry
    _shared_state.persistent_anomalies.clear()

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8


def test_no_restoration_while_anomaly_active():
    """Don't restore while triggering anomalies still qualify."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 5,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    _apply_anomaly_adjustments()
    result = _check_anomaly_restoration()
    assert result is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


def test_no_restoration_when_not_active():
    """Restoration is a no-op when no adjustments are active."""
    result = _check_anomaly_restoration()
    assert result is False


def test_manual_override_respected():
    """If an admin changes max_concurrent_vms during adjustment, don't overwrite it."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    _apply_anomaly_adjustments()
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    # Admin manually sets a different value
    _shared_state.runtime_config["max_concurrent_vms"] = 6

    # Clear anomaly
    _shared_state.persistent_anomalies.clear()

    _check_anomaly_restoration()
    # Should keep the admin's manual value (6), not restore to pre-adjustment (8)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6
    # But the gate threshold should still be restored
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8


# ─────────────────────────────────────────────────────────────────────────────
# check_anomaly_actions (async entry point)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_anomaly_actions_returns_adjusted():
    """check_anomaly_actions returns 'adjusted' when adjustments are applied."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    result = await check_anomaly_actions()
    assert result["action"] == "adjusted"
    assert result["max_concurrent_vms"] == 4
    assert result["failure_gate_threshold"] == 0.6
    assert "PROVISION_FAILURE" in result["trigger_anomalies"]


@pytest.mark.asyncio
async def test_check_anomaly_actions_returns_restored():
    """check_anomaly_actions returns 'restored' when adjustments are cleared."""
    proj_id = str(uuid.uuid4())
    _shared_state.persistent_anomalies[proj_id] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    # Apply first
    await check_anomaly_actions()

    # Clear anomaly
    _shared_state.persistent_anomalies.clear()

    result = await check_anomaly_actions()
    assert result["action"] == "restored"
    assert result["max_concurrent_vms"] == 8
    assert result["failure_gate_threshold"] == 0.8


@pytest.mark.asyncio
async def test_check_anomaly_actions_returns_none():
    """check_anomaly_actions returns 'none' when nothing to do."""
    result = await check_anomaly_actions()
    assert result["action"] == "none"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-project anomaly aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_adjustment_triggered_by_any_project():
    """Anomalies from any project trigger global adjustments."""
    proj_a = str(uuid.uuid4())
    proj_b = str(uuid.uuid4())
    # Neither project alone has enough, but proj_b does
    _shared_state.persistent_anomalies[proj_a] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 1,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.persistent_anomalies[proj_b] = {
        "PROVISION_FAILURE": {
            "severity": "high",
            "occurrence_count": 4,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = 8

    result = _apply_anomaly_adjustments()
    assert result is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────


def test_actionable_failure_classes():
    """Verify the expected actionable failure classes."""
    assert "PROVISION_FAILURE" in _ACTIONABLE_FAILURE_CLASSES
    assert "EXTERNAL_API" in _ACTIONABLE_FAILURE_CLASSES
    assert "CODE_ERROR" not in _ACTIONABLE_FAILURE_CLASSES


def test_anomaly_action_threshold():
    """Verify the threshold value."""
    assert _ANOMALY_ACTION_THRESHOLD == 3
