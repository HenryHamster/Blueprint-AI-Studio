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
    _check_fast_recovery,
    _expire_stale_anomalies,
    anomaly_maintenance_sweep,
    check_anomaly_actions,
    _ACTIONABLE_FAILURE_CLASSES,
    _ANOMALY_ACTION_THRESHOLD,
    _FAST_RECOVERY_MIN_SAMPLE,
    _FAST_RECOVERY_WINDOW_HOURS,
)
from app.time_utils import _utcnow
from collections import deque
from datetime import timedelta


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
    _shared_state.deploy_monitoring["task_results"].clear()
    _shared_state.deploy_monitoring["pipeline_events"].clear()
    yield
    _shared_state.persistent_anomalies.clear()
    _shared_state.anomaly_adjustments.update(deepcopy(_DEFAULT_ADJUSTMENTS))
    _shared_state.runtime_config["max_concurrent_vms"] = original_max_vms
    _shared_state.deploy_monitoring["task_results"].clear()
    _shared_state.deploy_monitoring["pipeline_events"].clear()


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
# Independent maintenance sweep
# ─────────────────────────────────────────────────────────────────────────────


def test_expire_stale_anomalies_removes_old_entries():
    """Entries with last_seen older than 48h are removed; fresh entries kept."""
    proj = str(uuid.uuid4())
    fresh_iso = _utcnow().isoformat()
    stale_iso = (_utcnow() - timedelta(hours=49)).isoformat()
    _shared_state.persistent_anomalies[proj] = {
        "FRESH": {"severity": "high", "occurrence_count": 3,
                  "first_seen": stale_iso, "last_seen": fresh_iso,
                  "latest_details": ""},
        "STALE": {"severity": "high", "occurrence_count": 3,
                  "first_seen": stale_iso, "last_seen": stale_iso,
                  "latest_details": ""},
    }
    expired = _expire_stale_anomalies()
    assert expired == 1
    assert "FRESH" in _shared_state.persistent_anomalies[proj]
    assert "STALE" not in _shared_state.persistent_anomalies[proj]


def test_expire_stale_anomalies_drops_empty_project():
    """Project key is removed when all its anomalies expire."""
    proj = str(uuid.uuid4())
    stale_iso = (_utcnow() - timedelta(hours=72)).isoformat()
    _shared_state.persistent_anomalies[proj] = {
        "STALE": {"severity": "high", "occurrence_count": 3,
                  "first_seen": stale_iso, "last_seen": stale_iso,
                  "latest_details": ""},
    }
    _expire_stale_anomalies()
    assert proj not in _shared_state.persistent_anomalies


@pytest.mark.asyncio
async def test_maintenance_sweep_restores_after_expiry():
    """Sweep expires the triggering anomaly, then restoration runs in same call."""
    proj = str(uuid.uuid4())
    stale_iso = (_utcnow() - timedelta(hours=49)).isoformat()
    _shared_state.persistent_anomalies[proj] = {
        "PROVISION_FAILURE": {
            "severity": "high", "occurrence_count": 5,
            "first_seen": stale_iso, "last_seen": stale_iso,
            "latest_details": "",
        }
    }
    # Pre-existing adjustment that should be restored
    _shared_state.anomaly_adjustments.update({
        "active": True,
        "adjusted_at": stale_iso,
        "trigger_anomalies": ["PROVISION_FAILURE"],
        "pre_adjustment_max_vms": 10,
        "pre_adjustment_failure_gate": 0.8,
        "failure_gate_threshold": 0.6,
    })
    _shared_state.runtime_config["max_concurrent_vms"] = 5  # = 10 // 2
    result = await anomaly_maintenance_sweep()
    assert result["expired"] == 1
    assert result["action"] == "restored"
    assert _shared_state.runtime_config["max_concurrent_vms"] == 10
    assert _shared_state.anomaly_adjustments["active"] is False


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


# ─────────────────────────────────────────────────────────────────────────────
# Fast-recovery path
# ─────────────────────────────────────────────────────────────────────────────

def _make_result(success: bool, failure_class: str = "", task_type: str = "create", age_minutes: int = 0):
    """Build a task_results tuple with a timestamp `age_minutes` ago."""
    ts = (_utcnow() - timedelta(minutes=age_minutes)).isoformat()
    return (ts, "success" if success else "failure", failure_class, task_type)


def _setup_active_adjustment(proj_id: str, failure_class: str = "PROVISION_FAILURE", max_vms: int = 8):
    """Apply an active adjustment for `failure_class` with `max_vms` pre-adjustment."""
    _shared_state.persistent_anomalies[proj_id] = {
        failure_class: {
            "severity": "high",
            "occurrence_count": 3,
            "first_seen": "2026-04-16T00:00:00",
            "last_seen": "2026-04-16T01:00:00",
            "latest_details": "test",
        }
    }
    _shared_state.runtime_config["max_concurrent_vms"] = max_vms
    _apply_anomaly_adjustments()
    assert _shared_state.anomaly_adjustments["active"] is True


def test_fast_recovery_restores_with_healthy_recent_outcomes():
    """(1) adjustment applied; (2) fast-recovery restores when recent outcomes are healthy."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # Anomaly still present (occurrence_count still >= threshold)
    assert _shared_state.persistent_anomalies[proj_id]["PROVISION_FAILURE"]["occurrence_count"] == 3

    # Feed >= MIN_SAMPLE successes in the 2h window, no PROVISION_FAILURE failures
    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8


def test_fast_recovery_blocked_when_trigger_failures_still_occurring():
    """(3) fast-recovery does NOT restore when trigger-class failures are still occurring."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # Mix of successes + one PROVISION_FAILURE failure → no fast recovery
    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )
    _shared_state.deploy_monitoring["task_results"].append(
        _make_result(success=False, failure_class="PROVISION_FAILURE", task_type="create")
    )

    result = _check_anomaly_restoration()
    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


def test_fast_recovery_blocked_when_sample_too_small():
    """Fast-recovery requires >= MIN_SAMPLE completions; fewer completions block it."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # Only MIN_SAMPLE - 1 successes — not enough
    for _ in range(_FAST_RECOVERY_MIN_SAMPLE - 1):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    result = _check_anomaly_restoration()
    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True


def test_fast_recovery_blocked_by_provision_timeout_pipeline_event():
    """PROVISION_FAILURE fast-recovery also requires no recent provision_timeout_pre_claim events."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # Enough successes in task_results
    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    # But a recent provision_timeout_pre_claim event exists
    _shared_state.deploy_monitoring["pipeline_events"].append({
        "type": "provision_timeout_pre_claim",
        "ts": _utcnow() - timedelta(minutes=30),  # within 2h window
        "task_id": str(uuid.uuid4()),
        "vm_id": None,
        "zone": "us-central1-a",
        "account_id": None,
    })

    result = _check_anomaly_restoration()
    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True


def test_fast_recovery_not_blocked_by_stale_provision_timeout_event():
    """provision_timeout_pre_claim events older than the 2h window don't block recovery."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    # Stale event — older than FAST_RECOVERY_WINDOW_HOURS
    _shared_state.deploy_monitoring["pipeline_events"].append({
        "type": "provision_timeout_pre_claim",
        "ts": _utcnow() - timedelta(hours=_FAST_RECOVERY_WINDOW_HOURS + 1),
        "task_id": str(uuid.uuid4()),
        "vm_id": None,
        "zone": "us-central1-a",
        "account_id": None,
    })

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is False


def test_fast_recovery_external_api_no_provision_check():
    """EXTERNAL_API triggers don't apply the provision_timeout_pre_claim check."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "EXTERNAL_API", max_vms=8)

    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    # A provision timeout event exists, but the trigger is EXTERNAL_API — should not block
    _shared_state.deploy_monitoring["pipeline_events"].append({
        "type": "provision_timeout_pre_claim",
        "ts": _utcnow() - timedelta(minutes=30),
        "task_id": str(uuid.uuid4()),
        "vm_id": None,
        "zone": "us-central1-a",
        "account_id": None,
    })

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is False


def test_admin_override_respected_during_fast_recovery():
    """(4) Admin override is still respected even when fast recovery triggers."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # Admin manually changes max_concurrent_vms during adjustment
    _shared_state.runtime_config["max_concurrent_vms"] = 6

    # Enough healthy results for fast recovery
    for _ in range(_FAST_RECOVERY_MIN_SAMPLE):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )

    result = _check_anomaly_restoration()
    assert result is True
    # Admin's manual value (6) is kept, not restored to pre-adjustment (8)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6
    # Failure gate is still restored
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8
    assert _shared_state.anomaly_adjustments["active"] is False
