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
    _as_iso,
    _check_anomaly_restoration,
    _check_fast_recovery,
    _check_max_duration_safeguard,
    _error_fingerprint,
    _expire_stale_anomalies,
    _is_recurring_failure_class,
    _normalize_error_message,
    anomaly_maintenance_sweep,
    check_anomaly_actions,
    count_matching_fingerprints,
    find_directions_sharing_fingerprint,
    _ACTIONABLE_FAILURE_CLASSES,
    _ANOMALY_ACTION_THRESHOLD,
    _FAST_RECOVERY_MIN_SAMPLE,
    _FAST_RECOVERY_WINDOW_HOURS,
    _MAX_ADJUSTMENT_DURATION_HOURS,
    _SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS,
)
from app.time_utils import _utcnow
from collections import deque
from datetime import datetime, timedelta, timezone


_DEFAULT_ADJUSTMENTS = {
    "active": False,
    "adjusted_at": None,
    "trigger_anomalies": [],
    "pre_adjustment_max_vms": None,
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,
    "forced_restoration_count": 0,
    "forced_restored_at": None,
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


def test_restoration_deferred_when_deploy_paused():
    """If deploy_health paused (rc=0) or is ramping (rc=1/2), don't misread as admin override."""
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

    # Anomaly clears
    _shared_state.persistent_anomalies.clear()
    # deploy_health auto-paused: rc forced to 0
    _shared_state.runtime_config["max_concurrent_vms"] = 0

    result = _check_anomaly_restoration()
    assert result is False
    # Adjustments stay active; stash preserved so capacity isn't lost
    assert _shared_state.anomaly_adjustments["active"] is True
    assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] == 8
    assert _shared_state.runtime_config["max_concurrent_vms"] == 0

    # Simulate deploy_health ramp-up: rc=1, then 2 — still below adjusted floor (4)
    for ramp_val in (1, 2):
        _shared_state.runtime_config["max_concurrent_vms"] = ramp_val
        assert _check_anomaly_restoration() is False
        assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] == 8

    # Once deploy_health has fully restored (rc back to 8), stash clears cleanly
    _shared_state.runtime_config["max_concurrent_vms"] = 8
    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8
    assert _shared_state.anomaly_adjustments["active"] is False
    assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] is None


def test_max_duration_safeguard_deferred_when_paused():
    """Max-duration safeguard defers when deploy_health has paused (rc below adjusted floor)."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # deploy_health paused during the throttled period
    _shared_state.runtime_config["max_concurrent_vms"] = 0
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS + 0.5)

    result = _check_max_duration_safeguard()
    assert result is False
    # Stash preserved so deploy_health resume path can restore correctly
    assert _shared_state.anomaly_adjustments["active"] is True
    assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] == 8


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


def test_expire_stale_anomalies_tolerates_datetime_last_seen():
    """Legacy persisted snapshots may round-trip ``last_seen`` as a datetime
    instead of an ISO string (main.py used to call ``datetime.fromisoformat``
    during restore). The stale-cutoff comparison must coerce both shapes
    rather than raising ``TypeError: '<' not supported between datetime/str``.
    """
    proj = str(uuid.uuid4())
    fresh_dt = _utcnow()
    stale_dt = _utcnow() - timedelta(hours=49)
    _shared_state.persistent_anomalies[proj] = {
        "FRESH": {"severity": "high", "occurrence_count": 3,
                  "first_seen": fresh_dt, "last_seen": fresh_dt,
                  "latest_details": ""},
        "STALE": {"severity": "high", "occurrence_count": 3,
                  "first_seen": stale_dt, "last_seen": stale_dt,
                  "latest_details": ""},
    }
    # Must not raise.
    expired = _expire_stale_anomalies()
    assert expired == 1
    assert "FRESH" in _shared_state.persistent_anomalies[proj]
    assert "STALE" not in _shared_state.persistent_anomalies[proj]


@pytest.mark.asyncio
async def test_maintenance_sweep_restores_after_expiry():
    """Sweep expires the triggering anomaly, then restoration runs in same call."""
    proj = str(uuid.uuid4())
    stale_iso = (_utcnow() - timedelta(hours=49)).isoformat()
    # `adjusted_at` must be recent enough that the max-duration safeguard
    # doesn't fire — the point of this test is the expiry → restore path.
    recent_iso = _utcnow().isoformat()
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
        "adjusted_at": recent_iso,
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


def test_fast_recovery_tolerates_single_failure_below_rate_threshold():
    """One triggering failure among many successes is tolerated (<10% rate)."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # 19 successes + 1 PROVISION_FAILURE → 5% triggering rate, below 10% threshold
    for _ in range(19):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )
    _shared_state.deploy_monitoring["task_results"].append(
        _make_result(success=False, failure_class="PROVISION_FAILURE", task_type="create")
    )

    result = _check_anomaly_restoration()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is False
    assert _shared_state.runtime_config["max_concurrent_vms"] == 8


def test_fast_recovery_blocked_when_two_failures_exceed_tolerance():
    """Two triggering failures exceed the max-tolerated count even if rate is low."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)

    # 50 successes + 2 PROVISION_FAILUREs → ~3.8% rate but exceeds count tolerance of 1
    for _ in range(50):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=True, failure_class="", task_type="create")
        )
    for _ in range(2):
        _shared_state.deploy_monitoring["task_results"].append(
            _make_result(success=False, failure_class="PROVISION_FAILURE", task_type="create")
        )

    result = _check_anomaly_restoration()
    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True


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


def test_fast_recovery_uses_health_history_when_paused():
    """When pipeline is paused (max_concurrent_vms == 0), task_results stops growing.
    A full ring buffer of healthy probes substitutes for the task-result sample."""
    from app import deploy_health

    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # Pipeline is paused — adjustment captured pre_max=8, but deploy_health
    # subsequently dropped capacity to 0 to stop dispatch.
    _shared_state.runtime_config["max_concurrent_vms"] = 0

    deploy_health._reset_health_recovery_history()
    for _ in range(deploy_health._HEALTH_HISTORY_SIZE):
        deploy_health._health_recovery_history.append(True)

    try:
        result = _check_anomaly_restoration()
    finally:
        deploy_health._reset_health_recovery_history()

    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is False
    # max_concurrent_vms stays at 0 — that's deploy_health's value to manage on
    # its own resume path; anomaly system treats it as a manual override.
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8


def test_fast_recovery_health_history_path_requires_full_healthy_window():
    """Paused-pipeline fallback only fires when the buffer is full AND all-healthy."""
    from app import deploy_health

    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    _shared_state.runtime_config["max_concurrent_vms"] = 0

    # Mostly healthy but one flaky probe — should NOT fast-recover
    deploy_health._reset_health_recovery_history()
    for _ in range(deploy_health._HEALTH_HISTORY_SIZE - 1):
        deploy_health._health_recovery_history.append(True)
    deploy_health._health_recovery_history.append(False)

    try:
        result = _check_anomaly_restoration()
    finally:
        deploy_health._reset_health_recovery_history()

    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True


def test_fast_recovery_health_history_ignored_when_not_paused():
    """The health-history fallback only applies when max_concurrent_vms == 0."""
    from app import deploy_health

    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # Not paused: pipeline is throttled but still dispatching (4 == 8 // 2).
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4

    deploy_health._reset_health_recovery_history()
    for _ in range(deploy_health._HEALTH_HISTORY_SIZE):
        deploy_health._health_recovery_history.append(True)

    try:
        result = _check_anomaly_restoration()
    finally:
        deploy_health._reset_health_recovery_history()

    # Empty task_results + not paused → original min-sample bar still blocks
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


# ─────────────────────────────────────────────────────────────────────────────
# Max-duration safeguard
# ─────────────────────────────────────────────────────────────────────────────


def _backdate_adjusted_at(hours: float) -> None:
    """Rewrite adjusted_at to simulate an adjustment that has been active for `hours`."""
    _shared_state.anomaly_adjustments["adjusted_at"] = (
        _utcnow() - timedelta(hours=hours)
    ).isoformat()


def test_max_duration_no_safeguard_before_limit():
    """Before the max duration elapses, the safeguard does not fire."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # Only slightly past 1 hour — well below the 6h limit
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS - 1)

    result = _check_max_duration_safeguard()
    assert result is False
    assert _shared_state.anomaly_adjustments["active"] is True
    assert _shared_state.runtime_config["max_concurrent_vms"] == 4


def test_max_duration_forced_graduated_restoration():
    """After the max duration, forced restoration returns max_vms to 75% of pre-adjustment."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # Simulate persistent anomaly that never cleared — throttle has been active for >6h
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS + 0.1)

    result = _check_max_duration_safeguard()
    assert result is True

    # Failure gate fully restored
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8
    # max_vms restored to 75% of pre-adjustment (8 * 0.75 = 6), not the full 8
    assert _shared_state.runtime_config["max_concurrent_vms"] == 6
    # State reset
    assert _shared_state.anomaly_adjustments["active"] is False
    assert _shared_state.anomaly_adjustments["adjusted_at"] is None
    assert _shared_state.anomaly_adjustments["pre_adjustment_max_vms"] is None
    # Observability: forced_restoration_count bumped, timestamp set
    assert _shared_state.anomaly_adjustments["forced_restoration_count"] == 1
    assert _shared_state.anomaly_adjustments["forced_restored_at"] is not None


def test_max_duration_safeguard_respects_admin_override():
    """If admin changed max_vms during the adjustment, keep that value on forced restoration."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    # Admin manually changes the value
    _shared_state.runtime_config["max_concurrent_vms"] = 5
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS + 0.5)

    result = _check_max_duration_safeguard()
    assert result is True
    # Admin's value preserved (would have been 6 under graduated restoration)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 5
    # Gate is still fully restored
    assert _shared_state.anomaly_adjustments["failure_gate_threshold"] == 0.8
    assert _shared_state.anomaly_adjustments["forced_restoration_count"] == 1


def test_max_duration_safeguard_noop_when_inactive():
    """Safeguard does nothing when no adjustments are active."""
    result = _check_max_duration_safeguard()
    assert result is False
    assert _shared_state.anomaly_adjustments["forced_restoration_count"] == 0


def test_reapply_within_reentry_window_resets_timer():
    """Anomaly re-triggering within the reentry window re-applies adjustments with a fresh timer."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS + 0.1)

    # Force-restore (simulates persistent anomaly hitting the 6h limit)
    assert _check_max_duration_safeguard() is True
    assert _shared_state.anomaly_adjustments["active"] is False
    forced_ts = _shared_state.anomaly_adjustments["forced_restored_at"]

    # Anomaly re-triggers immediately — persistent_anomalies still has it
    result = _apply_anomaly_adjustments()
    assert result is True
    assert _shared_state.anomaly_adjustments["active"] is True
    # Timer resets (adjusted_at set fresh) and max_vms drops again
    # from the 75%-restored baseline (6 // 2 = 3)
    assert _shared_state.runtime_config["max_concurrent_vms"] == 3
    # forced_restored_at is preserved for observability of the reentry event
    assert _shared_state.anomaly_adjustments["forced_restored_at"] == forced_ts


# ─────────────────────────────────────────────────────────────────────────────
# count_matching_fingerprints — remediation effectiveness helper
# ─────────────────────────────────────────────────────────────────────────────


class _FakeTask:
    def __init__(self, msg):
        self.latest_status_message = msg


def test_count_matching_fingerprints_empty_inputs():
    """Empty fingerprint or task list returns 0 without error."""
    assert count_matching_fingerprints("", [_FakeTask("boom")]) == 0
    assert count_matching_fingerprints("abc123", []) == 0
    assert count_matching_fingerprints("abc123", None) == 0


def test_count_matching_fingerprints_counts_recurrence_ignoring_task_specifics():
    """Normalized fingerprints group identical root causes across different PRs/UUIDs."""
    target = _error_fingerprint("process exited with code 1 on PR #100")
    tasks = [
        _FakeTask("process exited with code 1 on PR #200"),
        _FakeTask("process exited with code 1 on PR #201"),
        _FakeTask("could not resolve hostname api.github.com"),
        _FakeTask(None),
        _FakeTask(""),
    ]
    assert count_matching_fingerprints(target, tasks) == 2


# ─────────────────────────────────────────────────────────────────────────────
# find_directions_sharing_fingerprint — cross-direction systemic-failure helper
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProject:
    def __init__(self, directions):
        self.directions = directions


def _paused_direction(did, fp, *, status="paused", auto_paused=True, paused_at=None, pause_count=1, text="dir"):
    return {
        "id": did,
        "text": text,
        "status": status,
        "auto_paused": auto_paused,
        "error_fingerprint": fp,
        "paused_at": (paused_at if paused_at is not None else _utcnow().isoformat()),
        "auto_pause_count": pause_count,
    }


def test_find_directions_sharing_fingerprint_empty_fingerprint():
    """No fingerprint to match → no work, empty list."""
    project = _FakeProject([_paused_direction("d1", "abc")])
    assert find_directions_sharing_fingerprint(project, "") == []
    assert find_directions_sharing_fingerprint(project, None) == []


def test_find_directions_sharing_fingerprint_matches_other_paused_directions():
    """Other auto-paused directions sharing the fingerprint are returned; caller excluded."""
    project = _FakeProject([
        _paused_direction("d-self", "fp-shared"),
        _paused_direction("d-other", "fp-shared", text="some other direction"),
        _paused_direction("d-mismatch", "fp-different"),
    ])
    result = find_directions_sharing_fingerprint(project, "fp-shared", exclude_direction_id="d-self")
    assert len(result) == 1
    assert result[0]["id"] == "d-other"
    assert result[0]["text"] == "some other direction"
    assert result[0]["auto_pause_count"] == 1


def test_find_directions_sharing_fingerprint_skips_non_paused_and_user_paused():
    """Active directions and user-paused (not auto_paused) directions are ignored."""
    project = _FakeProject([
        _paused_direction("d-active", "fp-x", status="active"),
        _paused_direction("d-user-paused", "fp-x", auto_paused=False),
        _paused_direction("d-auto-paused", "fp-x"),
    ])
    result = find_directions_sharing_fingerprint(project, "fp-x")
    assert [r["id"] for r in result] == ["d-auto-paused"]


def test_find_directions_sharing_fingerprint_skips_stale_pauses():
    """Pauses older than the window are considered stale and excluded."""
    stale_ts = (_utcnow() - timedelta(hours=_SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS + 1)).isoformat()
    fresh_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = _FakeProject([
        _paused_direction("d-stale", "fp-y", paused_at=stale_ts),
        _paused_direction("d-fresh", "fp-y", paused_at=fresh_ts),
    ])
    result = find_directions_sharing_fingerprint(project, "fp-y")
    assert [r["id"] for r in result] == ["d-fresh"]


def test_find_directions_sharing_fingerprint_handles_missing_directions():
    """Project with no directions (None or []) returns empty list without error."""
    assert find_directions_sharing_fingerprint(_FakeProject(None), "fp") == []
    assert find_directions_sharing_fingerprint(_FakeProject([]), "fp") == []


@pytest.mark.asyncio
async def test_check_anomaly_actions_returns_forced_restored():
    """check_anomaly_actions surfaces forced_restored when the safeguard fires."""
    proj_id = str(uuid.uuid4())
    _setup_active_adjustment(proj_id, "PROVISION_FAILURE", max_vms=8)
    _backdate_adjusted_at(hours=_MAX_ADJUSTMENT_DURATION_HOURS + 0.5)

    result = await check_anomaly_actions()
    assert result["action"] == "forced_restored"
    assert result["max_concurrent_vms"] == 6
    assert result["failure_gate_threshold"] == 0.8
    assert result["forced_restoration_count"] == 1


# ---------------------------------------------------------------------------
# Pure-function helpers: _normalize_error_message, _as_iso,
# _is_recurring_failure_class
# ---------------------------------------------------------------------------


def test_normalize_error_message_strips_uuid():
    msg = "task abc12345-1234-5678-9abc-def012345678 failed"
    assert _normalize_error_message(msg) == "task <uuid> failed"


def test_normalize_error_message_strips_pr_numbers():
    assert _normalize_error_message("PR #123 blocked") == "<pr> blocked"
    assert _normalize_error_message("see #456") == "see <pr>"
    assert _normalize_error_message("pr #789") == "<pr>"


def test_normalize_error_message_strips_branch_name():
    assert _normalize_error_message("push to feature/foo-bar failed") == "push to <branch> failed"
    assert _normalize_error_message("blueprint/task-abc123 error") == "<branch> error"


def test_normalize_error_message_strips_iso_timestamp():
    assert _normalize_error_message("error at 2026-04-11T12:34:56Z") == "error at <ts>"
    assert (
        _normalize_error_message("heartbeat 2026-04-11T12:34:56.789+00:00 missed")
        == "heartbeat <ts> missed"
    )


def test_normalize_error_message_strips_long_numbers():
    assert _normalize_error_message("job 123456 crashed") == "job <id> crashed"
    # Short numbers (exit codes) preserved
    assert _normalize_error_message("exit code 2") == "exit code 2"


def test_normalize_error_message_empty():
    assert _normalize_error_message("") == ""
    assert _normalize_error_message(None) == ""  # type: ignore[arg-type]


def test_normalize_error_message_no_tokens():
    assert _normalize_error_message("connection refused") == "connection refused"


def test_as_iso_with_datetime():
    dt = datetime(2026, 4, 11, 12, 34, 56, tzinfo=timezone.utc)
    assert _as_iso(dt) == dt.isoformat()


def test_as_iso_with_string():
    assert _as_iso("2026-04-11T12:34:56") == "2026-04-11T12:34:56"


def test_as_iso_with_none_and_empty():
    assert _as_iso(None) == ""
    assert _as_iso("") == ""


def test_is_recurring_failure_class_missing_key():
    assert _is_recurring_failure_class({}, "CODE_ERROR") is False


def test_is_recurring_failure_class_below_threshold():
    anomalies = {"CODE_ERROR": {"occurrence_count": 2}}
    assert _is_recurring_failure_class(anomalies, "CODE_ERROR") is False


def test_is_recurring_failure_class_at_threshold():
    anomalies = {"CODE_ERROR": {"occurrence_count": 3}}
    assert _is_recurring_failure_class(anomalies, "CODE_ERROR") is True


def test_is_recurring_failure_class_above_threshold():
    anomalies = {"CODE_ERROR": {"occurrence_count": 7}}
    assert _is_recurring_failure_class(anomalies, "CODE_ERROR") is True
