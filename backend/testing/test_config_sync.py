"""Tests for sync_runtime_config_from_state.

Cloud Run runs multiple instances; admin mutations are instance-local. The
sync helper pulls authoritative values from DeploymentState so each instance
converges. These tests cover the scalar cols, the JSONB column, the
PAUSED/HALTED override, and the changed-keys return value.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.shared_state as _shared_state
from app.models import DeploymentState, DeploymentStatus
from app.shared_state import (
    dispatcher_cycle_log,
    get_dispatcher_cycles,
    promotion_enabled_by_project_map,
    record_dispatcher_cycle,
    resolve_project_promotion_enabled,
    sync_runtime_config_from_state,
)


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


def test_repo_map_holdback_default_is_nonzero():
    assert _shared_state.runtime_config["repo_map_holdback_fraction"] == 0.1


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
            "task_type_provider_overrides": {
                "research": {"anthropic": {"effort": "max"}}
            },
            "warm_pin_clear_rate_threshold": 11,
            "warm_continuity_cooldown_seconds": 2400,
            "warm_backlog_reuse_failure_rate_threshold": 7,
            "warm_backlog_reuse_cooldown_seconds": 2400,
            "warm_backlog_scan_limit": 18,
            "warm_backlog_teardown_defer_secs": 25,
            "productive_direction_cooldown_minutes": 15,
            "directed_research_cooldown_minutes": 45,
            "undirected_research_cooldown_minutes": 30,
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": True,
        },
    )
    _shared_state.runtime_config["strategic_research_frequency"] = 6
    _shared_state.runtime_config["require_approval_gates"] = True
    _shared_state.runtime_config["max_provision_retries"] = 3
    _shared_state.runtime_config["provider_settings_defaults"] = {
        "anthropic": {"effort": "medium"}
    }
    _shared_state.runtime_config["task_type_provider_overrides"] = {}
    _shared_state.runtime_config["warm_pin_clear_rate_threshold"] = 10
    _shared_state.runtime_config["warm_continuity_cooldown_seconds"] = 1800
    _shared_state.runtime_config["warm_backlog_reuse_failure_rate_threshold"] = 5
    _shared_state.runtime_config["warm_backlog_reuse_cooldown_seconds"] = 1800
    _shared_state.runtime_config["warm_backlog_scan_limit"] = 10
    _shared_state.runtime_config["warm_backlog_teardown_defer_secs"] = 20
    _shared_state.runtime_config["productive_direction_cooldown_minutes"] = 45
    _shared_state.runtime_config["directed_research_cooldown_minutes"] = 120
    _shared_state.runtime_config["undirected_research_cooldown_minutes"] = 60
    _shared_state.runtime_config["improvement_lanes_enabled"] = False
    _shared_state.runtime_config["review_fix_lane_pilot_enabled"] = False

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["strategic_research_frequency"] == 9
    assert _shared_state.runtime_config["require_approval_gates"] is False
    assert _shared_state.runtime_config["max_provision_retries"] == 5
    assert _shared_state.runtime_config["provider_settings_defaults"] == {
        "anthropic": {"effort": "high"},
        "zai": {},
        "deepseek": {},
    }
    assert _shared_state.runtime_config["task_type_provider_overrides"] == {
        "research": {"anthropic": {"effort": "max"}}
    }
    assert _shared_state.runtime_config["warm_pin_clear_rate_threshold"] == 11
    assert _shared_state.runtime_config["warm_continuity_cooldown_seconds"] == 2400
    assert _shared_state.runtime_config[
        "warm_backlog_reuse_failure_rate_threshold"
    ] == 7
    assert _shared_state.runtime_config["warm_backlog_reuse_cooldown_seconds"] == 2400
    assert _shared_state.runtime_config["warm_backlog_scan_limit"] == 18
    assert _shared_state.runtime_config["warm_backlog_teardown_defer_secs"] == 25
    assert _shared_state.runtime_config["productive_direction_cooldown_minutes"] == 15
    assert _shared_state.runtime_config["directed_research_cooldown_minutes"] == 45
    assert _shared_state.runtime_config["undirected_research_cooldown_minutes"] == 30
    assert _shared_state.runtime_config["improvement_lanes_enabled"] is True
    assert _shared_state.runtime_config["review_fix_lane_pilot_enabled"] is True
    for key in (
        "strategic_research_frequency",
        "require_approval_gates",
        "max_provision_retries",
        "provider_settings_defaults",
        "task_type_provider_overrides",
        "warm_pin_clear_rate_threshold",
        "warm_continuity_cooldown_seconds",
        "warm_backlog_reuse_failure_rate_threshold",
        "warm_backlog_reuse_cooldown_seconds",
        "warm_backlog_scan_limit",
        "warm_backlog_teardown_defer_secs",
        "productive_direction_cooldown_minutes",
        "directed_research_cooldown_minutes",
        "undirected_research_cooldown_minutes",
        "improvement_lanes_enabled",
        "review_fix_lane_pilot_enabled",
    ):
        assert key in changed


def test_resolve_project_promotion_enabled_falls_back_to_fleet():
    """With no per-project override the resolver returns the fleet-wide value."""
    _shared_state.runtime_config["promotion_enabled"] = True
    _shared_state.runtime_config["promotion_enabled_by_project"] = {}
    assert resolve_project_promotion_enabled("p-x") is True
    _shared_state.runtime_config["promotion_enabled"] = False
    assert resolve_project_promotion_enabled("p-x") is False


def test_resolve_project_promotion_enabled_uses_explicit_override():
    """An explicit per-project override wins over the fleet default, both ways."""
    _shared_state.runtime_config["promotion_enabled"] = False
    _shared_state.runtime_config["promotion_enabled_by_project"] = {"p-x": True}
    assert resolve_project_promotion_enabled("p-x") is True
    # A project without an override still rides the fleet default.
    assert resolve_project_promotion_enabled("p-y") is False
    # Explicit False override holds even when the fleet is live.
    _shared_state.runtime_config["promotion_enabled"] = True
    _shared_state.runtime_config["promotion_enabled_by_project"] = {"p-x": False}
    assert resolve_project_promotion_enabled("p-x") is False
    assert resolve_project_promotion_enabled("p-y") is True


def test_promotion_enabled_by_project_map_normalizes():
    """The map helper trims keys, coerces bools, and drops blank keys."""
    _shared_state.runtime_config["promotion_enabled_by_project"] = {
        " p-a ": 1,
        "p-b": 0,
        "": True,  # dropped (blank key)
    }
    assert promotion_enabled_by_project_map() == {"p-a": True, "p-b": False}


@pytest.mark.asyncio
async def test_sync_applies_promotion_enabled_by_project():
    """Per-project promotion overrides round-trip through runtime_config_json."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={
            "promotion_enabled_by_project": {" p-a ": True, "p-b": 0, "": 1}
        },
    )
    _shared_state.runtime_config["promotion_enabled_by_project"] = {}
    changed = await sync_runtime_config_from_state(state)
    assert "promotion_enabled_by_project" in changed
    assert _shared_state.runtime_config["promotion_enabled_by_project"] == {
        "p-a": True,
        "p-b": False,
    }


def test_cochange_decomposition_defaults_off():
    """Co-change auto-decomposition ships disabled by default (live data showed
    0/14 split sub-findings merging while ~3x-ing fan-out). Re-enable per-deploy
    via COCHANGE_DECOMPOSITION_ENABLED or live via PATCH /api/admin/settings."""
    from app.runtime_config_state import runtime_config as _rc

    assert _rc.get("cochange_decomposition_enabled") is False


@pytest.mark.asyncio
async def test_sync_applies_cochange_decomposition_enabled():
    """The decomposition kill-switch propagates from persisted runtime_config_json
    so an admin PATCH flips it across every Cloud Run instance without a deploy."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={"cochange_decomposition_enabled": True},
    )
    _shared_state.runtime_config["cochange_decomposition_enabled"] = False

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["cochange_decomposition_enabled"] is True
    assert "cochange_decomposition_enabled" in changed


@pytest.mark.asyncio
async def test_sync_applies_warm_pool_project_allowlist():
    """The per-project warm-pool enablement scope (allowlist) must propagate
    from the persisted runtime_config_json to every instance, normalized to a
    sorted list of trimmed, non-empty string ids (dedup + CSV-tolerant), so the
    maintainer and provisioning entrypoints across the fleet agree on which
    projects the pool is scoped to."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={
            # Mixed shapes: ints, blanks, dupes, whitespace — all normalized.
            "warm_pool_project_allowlist": ["proj-b", " proj-a ", "proj-a", 7, ""],
        },
    )
    _shared_state.runtime_config["warm_pool_project_allowlist"] = []

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["warm_pool_project_allowlist"] == [
        "7",
        "proj-a",
        "proj-b",
    ]
    assert "warm_pool_project_allowlist" in changed


@pytest.mark.asyncio
async def test_sync_accepts_csv_warm_pool_project_allowlist():
    """A comma-separated string is tolerated (mirrors the admin PATCH surface)
    and an empty allowlist restores fleet-wide behavior."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={"warm_pool_project_allowlist": "proj-c, proj-d ,"},
    )
    _shared_state.runtime_config["warm_pool_project_allowlist"] = ["stale"]

    await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["warm_pool_project_allowlist"] == [
        "proj-c",
        "proj-d",
    ]


@pytest.mark.asyncio
async def test_sync_applies_warm_pool_target_size_by_project():
    """The per-project warm-pool target map must propagate from persisted
    runtime_config_json to every instance, normalized to string keys and integer
    values clamped to 0..50, with malformed entries dropped — so the maintainer
    fleet-wide agrees on each project's reconcile target."""
    state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        current_revision="rev-1",
        current_sha="sha-1",
        last_known_good_revision="rev-1",
        last_known_good_sha="sha-1",
        runtime_config_json={
            "warm_pool_target_size_by_project": {
                " proj-a ": "3",  # trimmed key, int-coerced value
                "proj-b": 99,  # clamped to 50
                "proj-c": -2,  # clamped to 0
                "": 4,  # dropped (blank key)
                "proj-d": "bad",  # dropped (non-int)
            },
        },
    )
    _shared_state.runtime_config["warm_pool_target_size_by_project"] = {}

    changed = await sync_runtime_config_from_state(state)

    assert _shared_state.runtime_config["warm_pool_target_size_by_project"] == {
        "proj-a": 3,
        "proj-b": 50,
        "proj-c": 0,
    }
    assert "warm_pool_target_size_by_project" in changed


# --- Dispatcher Cycle Log ---


@pytest.fixture(autouse=False)
def clean_dispatcher_log():
    dispatcher_cycle_log.clear()
    yield
    dispatcher_cycle_log.clear()


def _make_cycle(project_id="proj-1", offset_hours=0, **kwargs):
    now = datetime.now(tz=timezone.utc) - timedelta(hours=offset_hours)
    record_dispatcher_cycle(
        project_id=project_id,
        cycle_started_at=now,
        cycle_finished_at=now + timedelta(seconds=1),
        signals_evaluated=kwargs.get("signals_evaluated", 3),
        signals_tripped=kwargs.get("signals_tripped", 1),
        spawns_emitted=kwargs.get("spawns_emitted", 1),
        rejections_by_reason=kwargs.get("rejections_by_reason", {}),
        mttd_p50_sec=kwargs.get("mttd_p50_sec", None),
    )


def test_record_dispatcher_cycle_appends(clean_dispatcher_log):
    _make_cycle()
    assert len(dispatcher_cycle_log) == 1
    rec = dispatcher_cycle_log[-1]
    assert rec["project_id"] == "proj-1"
    assert rec["signals_evaluated"] == 3
    assert rec["signals_tripped"] == 1
    assert rec["spawns_emitted"] == 1
    assert rec["rejections_by_reason"] == {}
    assert rec["mttd_p50_sec"] is None


def test_get_dispatcher_cycles_window_filter(clean_dispatcher_log):
    _make_cycle(offset_hours=0)  # recent
    _make_cycle(offset_hours=25)  # outside 24h window
    result = get_dispatcher_cycles(window_hours=24)
    assert len(result) == 1


def test_get_dispatcher_cycles_project_filter(clean_dispatcher_log):
    _make_cycle(project_id="proj-A")
    _make_cycle(project_id="proj-B")
    result = get_dispatcher_cycles(project_id="proj-A")
    assert len(result) == 1
    assert result[0]["project_id"] == "proj-A"


def test_get_dispatcher_cycles_no_filter_returns_all(clean_dispatcher_log):
    _make_cycle(project_id="proj-A")
    _make_cycle(project_id="proj-B")
    result = get_dispatcher_cycles()
    assert len(result) == 2


def test_record_dispatcher_cycle_rejection_reasons(clean_dispatcher_log):
    _make_cycle(
        rejections_by_reason={"cooldown": 2, "dedup": 1},
        mttd_p50_sec=300.5,
    )
    rec = dispatcher_cycle_log[-1]
    assert rec["rejections_by_reason"] == {"cooldown": 2, "dedup": 1}
    assert rec["mttd_p50_sec"] == 300.5
