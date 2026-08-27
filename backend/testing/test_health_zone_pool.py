"""
Tests for:
  (1) /health endpoint (app/main.py)
  (2) Zone health logic (app/compute.py)
  (3) Max Plan account pool logic (app/main.py)
"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from httpx import AsyncClient, ASGITransport

from app.time_utils import _utcnow
import app.shared_state as _shared_state
import app.orchestrator as _orch_module
from app.main import app
from app.max_plan_pool import (
    select_max_plan_account,
    release_max_plan_account,
    recheck_disabled_accounts,
    _max_plan_pools,
    _account_by_id,
    _max_plan_pool_lock,
    _pending_rl_persists,
    _pending_cap_persists,
    _account_available_for_model,
    _account_state_for_model,
    cascade_disabled_models,
    clear_rate_limit_state_on_success,
    disable_account_model,
    record_completion_event,
    _compute_health_score,
    get_account_health_stats,
    _HEALTH_WINDOW_SECONDS,
)
from app.compute import (
    get_zone_health,
    get_zone_health_summary,
    any_zone_available,
    soonest_zone_recovery,
    FALLBACK_ZONES,
    ZONE_COOLDOWN_SECONDS,
    _zone_failures,
    _zone_health,
    _zone_suspect_events,
)
from app.provision_error_scope import (
    _global_quota_state,
    _region_quota_state,
)


# ─── Shared Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def clean_zone_state():
    """Reset zone health dicts before and after each zone test."""

    def _reset():
        _zone_failures.clear()
        _zone_health.clear()
        _global_quota_state.clear()
        _region_quota_state.clear()
        _zone_suspect_events.clear()

    _reset()
    yield
    _reset()


_TEST_USER = "test-user-123"


def _add_to_pool(entry: dict):
    """Helper: add an account entry to the test user's pool and flat lookup."""
    entry.setdefault("user_id", _TEST_USER)
    entry.setdefault(
        "oauth_token", "sk-ant-test-token"
    )  # Accounts need a token to be selectable
    _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
    _account_by_id[entry["id"]] = entry


def _pool_list() -> list:
    """Helper: return flat list of all accounts (for assertions)."""
    return list(_account_by_id.values())


@pytest.fixture()
def clean_max_plan_pool():
    """Save and restore pool around each pool test."""
    orig_pools = {k: list(v) for k, v in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    orig_pending = list(_pending_rl_persists)
    orig_cap_pending = set(_pending_cap_persists)
    _max_plan_pools.clear()
    _account_by_id.clear()
    _pending_rl_persists.clear()
    _pending_cap_persists.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _pending_rl_persists.clear()
    _pending_cap_persists.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)
    _pending_rl_persists.extend(orig_pending)
    _pending_cap_persists.update(orig_cap_pending)


@pytest.fixture()
def healthy_orchestrator():
    """Set the cycle + progress heartbeat to now so /health sees a live
    orchestrator. Liveness is gated on the progress heartbeat (not full-cycle
    completion) since the 2026-05-28 death-spiral fix, so a healthy orchestrator
    must have a fresh heartbeat too."""
    original_cycle = _orch_module._orchestrator_last_cycle
    original_progress = _orch_module._orchestrator_last_progress
    _orch_module._orchestrator_last_cycle = _utcnow()
    _orch_module._orchestrator_last_progress = _utcnow()
    yield
    _orch_module._orchestrator_last_cycle = original_cycle
    _orch_module._orchestrator_last_progress = original_progress


@pytest.fixture()
def leader_instance():
    """Pin leader-only /health assertions independent of shard test order."""
    original = _shared_state.is_leader
    _shared_state.is_leader = True
    yield
    _shared_state.is_leader = original


@pytest.fixture()
def all_deps_healthy():
    """Ensure all external deps are True for the duration of the test."""
    original = dict(_shared_state.external_health)
    _shared_state.external_health.update(
        {
            "github_api": True,
            "anthropic_api": True,
            "openai_api": True,
            "gemini_api": True,
            "dispatch_api": True,
            "gcp": True,
            "db": True,
            "provider_health": {},
            "dispatch_health": {},
            "last_check": None,
            "failed_deps": [],
            "errors": {},
        }
    )
    yield
    _shared_state.external_health.clear()
    _shared_state.external_health.update(original)


@pytest_asyncio.fixture()
async def health_client():
    """Lightweight AsyncClient for /health — no DB dependency needed.
    The health endpoint is unauthenticated and does not touch the database."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ─── /health Endpoint ────────────────────────────────────────────────────────


@pytest.mark.smoketest
@pytest.mark.usefixtures("leader_instance")
class TestHealthEndpoint:
    """Integration tests for GET /health.

    Uses a lightweight client (no DB schema required) since /health is
    unauthenticated and reads only module-level state.
    """

    @pytest.mark.asyncio
    async def test_health_returns_correct_structure(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """Response contains all required top-level keys."""
        resp = await health_client.get("/health")
        data = resp.json()
        assert "status" in data
        assert "revision" in data
        assert "orchestrator_alive" in data
        assert "orchestrator_cycle_age_seconds" in data
        assert "external_deps" in data

    @pytest.mark.asyncio
    async def test_health_healthy_returns_200(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """Returns 200 and status=healthy when orchestrator is alive and all deps OK."""
        resp = await health_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_db_failure_returns_503(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """Returns 503 and status=unhealthy when the DB health flag is False."""
        _shared_state.external_health["db"] = False
        _shared_state.external_health["failed_deps"] = ["db"]
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"
        finally:
            _shared_state.external_health["db"] = True
            _shared_state.external_health["failed_deps"] = []

    @pytest.mark.asyncio
    async def test_health_github_api_failure_returns_degraded(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """Returns 200 with status=degraded when GitHub API is down (non-critical dep)."""
        _shared_state.external_health["github_api"] = False
        _shared_state.external_health["failed_deps"] = ["github_api"]
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "degraded"
            assert "github_api" in data["degraded_deps"]
        finally:
            _shared_state.external_health["github_api"] = True
            _shared_state.external_health["failed_deps"] = []

    @pytest.mark.asyncio
    async def test_health_orchestrator_never_started_returns_503(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """Returns 503 when the orchestrator has never completed a cycle (no
        cycle AND no progress heartbeat yet — the cold-start window covered by
        the liveness probe's initialDelaySeconds)."""
        original_cycle = _orch_module._orchestrator_last_cycle
        original_progress = _orch_module._orchestrator_last_progress
        _orch_module._orchestrator_last_cycle = None
        _orch_module._orchestrator_last_progress = None
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert data["orchestrator_alive"] is False
            assert data["orchestrator_cycle_age_seconds"] is None
        finally:
            _orch_module._orchestrator_last_cycle = original_cycle
            _orch_module._orchestrator_last_progress = original_progress

    @pytest.mark.asyncio
    async def test_health_wedged_orchestrator_returns_503(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """Returns 503 only when the worker is genuinely WEDGED — no progress
        heartbeat for longer than ORCHESTRATOR_HANG_SECONDS. (Liveness is gated
        on the heartbeat, not full-cycle completion, since the 2026-05-28
        death-spiral fix.)"""
        original_cycle = _orch_module._orchestrator_last_cycle
        original_progress = _orch_module._orchestrator_last_progress
        wedged = _utcnow() - timedelta(
            seconds=_orch_module.ORCHESTRATOR_HANG_SECONDS + 30
        )
        _orch_module._orchestrator_last_cycle = wedged
        _orch_module._orchestrator_last_progress = wedged
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"
        finally:
            _orch_module._orchestrator_last_cycle = original_cycle
            _orch_module._orchestrator_last_progress = original_progress

    @pytest.mark.asyncio
    async def test_health_slow_cycle_but_progressing_returns_200(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """The core death-spiral fix: a cycle running far longer than the old
        60s window is still HEALTHY as long as it keeps heartbeating. Pre-fix
        this returned 503 and Cloud Run killed the container mid-cycle."""
        original_cycle = _orch_module._orchestrator_last_cycle
        original_progress = _orch_module._orchestrator_last_progress
        now = _utcnow()
        _orch_module._orchestrator_last_cycle = now - timedelta(seconds=240)
        _orch_module._orchestrator_last_progress = now - timedelta(seconds=2)
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "orchestrator_slow_cycle" in data["degraded_deps"]
        finally:
            _orch_module._orchestrator_last_cycle = original_cycle
            _orch_module._orchestrator_last_progress = original_progress

    @pytest.mark.asyncio
    async def test_health_cycle_age_seconds_reflects_elapsed_time(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """orchestrator_cycle_age_seconds is roughly the time since last cycle."""
        original = _orch_module._orchestrator_last_cycle
        _orch_module._orchestrator_last_cycle = _utcnow() - timedelta(
            seconds=10
        )
        try:
            resp = await health_client.get("/health")
            age = resp.json().get("orchestrator_cycle_age_seconds")
            assert age is not None
            # Allow generous tolerance for test execution time
            assert 5 <= age <= 30
        finally:
            _orch_module._orchestrator_last_cycle = original

    @pytest.mark.asyncio
    async def test_health_external_deps_contains_all_deps(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """external_deps contains each dependency key."""
        resp = await health_client.get("/health")
        deps = resp.json()["external_deps"]
        for key in (
            "github_api",
            "anthropic_api",
            "openai_api",
            "gemini_api",
            "dispatch_api",
            "gcp",
            "db",
        ):
            assert key in deps

    @pytest.mark.asyncio
    async def test_health_external_deps_excludes_metadata_keys(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """last_check and failed_deps are NOT exposed in the external_deps field."""
        resp = await health_client.get("/health")
        deps = resp.json()["external_deps"]
        assert "last_check" not in deps
        assert "failed_deps" not in deps

    @pytest.mark.asyncio
    async def test_health_orchestrator_alive_true_when_recent(
        self, health_client: AsyncClient, healthy_orchestrator, all_deps_healthy
    ):
        """orchestrator_alive is True when last cycle was recent."""
        resp = await health_client.get("/health")
        assert resp.json()["orchestrator_alive"] is True


# ─── Zone Health Logic ───────────────────────────────────────────────────────


class TestZoneHealth:
    """Unit tests for zone cooldown, recovery, and summary logic in compute.py."""

    @pytest.mark.smoketest
    def test_get_zone_health_returns_entry_for_every_fallback_zone(
        self, clean_zone_state
    ):
        """get_zone_health() has an entry for each zone in FALLBACK_ZONES."""
        result = get_zone_health()
        for zone, _ in FALLBACK_ZONES:
            assert zone in result, f"Missing zone: {zone}"

    @pytest.mark.smoketest
    def test_get_zone_health_fresh_zones_are_healthy(self, clean_zone_state):
        """Zones with no failure history report status=healthy and cooldown_active=False."""
        result = get_zone_health()
        for zone, _ in FALLBACK_ZONES:
            assert result[zone]["status"] == "healthy"
            assert result[zone]["consecutive_failures"] == 0
            assert result[zone]["cooldown_active"] is False
            assert result[zone]["last_success"] is None
            assert result[zone]["last_failure"] is None

    @pytest.mark.smoketest
    def test_zone_cooldown_activation_on_recent_failure(self, clean_zone_state):
        """A zone with a fresh failure entry enters cooldown and reports status=exhausted."""
        zone = FALLBACK_ZONES[0][0]
        _zone_failures[zone] = datetime.now(timezone.utc)
        _zone_health[zone] = {
            "consecutive_failures": 1,
            "last_failure": datetime.now(timezone.utc),
            "last_failure_reason": "RESOURCE_POOL_EXHAUSTED",
            "cooldown_expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
        }

        result = get_zone_health()
        assert result[zone]["status"] == "exhausted"
        assert result[zone]["cooldown_active"] is True
        assert result[zone]["consecutive_failures"] == 1

    def test_zone_status_recovering_after_cooldown_expires(self, clean_zone_state):
        """A zone whose cooldown window has passed reports status=recovering."""
        zone = FALLBACK_ZONES[0][0]
        old_time = datetime.now(timezone.utc) - timedelta(
            seconds=ZONE_COOLDOWN_SECONDS + 10
        )
        _zone_failures[zone] = old_time
        _zone_health[zone] = {
            "consecutive_failures": 2,
            "last_failure": old_time,
            "last_failure_reason": "QUOTA",
        }

        result = get_zone_health()
        assert result[zone]["status"] == "recovering"
        assert result[zone]["cooldown_active"] is False
        assert result[zone]["consecutive_failures"] == 2

    def test_zone_healthy_after_success_clears_failure_count(self, clean_zone_state):
        """A zone with consecutive_failures=0 (i.e. recovered) reports status=healthy."""
        zone = FALLBACK_ZONES[0][0]
        _zone_health[zone] = {
            "consecutive_failures": 0,
            "last_success": datetime.now(timezone.utc),
            "last_failure": datetime.now(timezone.utc) - timedelta(hours=1),
            "last_failure_reason": "QUOTA",
        }

        result = get_zone_health()
        assert result[zone]["status"] == "healthy"
        assert result[zone]["cooldown_active"] is False

    def test_get_zone_health_last_failure_reason_preserved(self, clean_zone_state):
        """last_failure_reason is surfaced verbatim in zone health output."""
        zone = FALLBACK_ZONES[0][0]
        _zone_failures[zone] = datetime.now(timezone.utc)
        _zone_health[zone] = {
            "consecutive_failures": 1,
            "last_failure": datetime.now(timezone.utc),
            "last_failure_reason": "QUOTA",
        }
        assert get_zone_health()[zone]["last_failure_reason"] == "QUOTA"

    def test_get_zone_health_last_success_as_iso_string(self, clean_zone_state):
        """last_success is returned as ISO 8601 string when present."""
        zone = FALLBACK_ZONES[0][0]
        now = datetime.now(timezone.utc)
        _zone_health[zone] = {
            "consecutive_failures": 0,
            "last_success": now,
        }
        result = get_zone_health()
        assert result[zone]["last_success"] == now.isoformat()

    def test_get_zone_health_last_failure_as_iso_string(self, clean_zone_state):
        """last_failure is returned as ISO 8601 string when present."""
        zone = FALLBACK_ZONES[0][0]
        _zone_failures[zone] = datetime.now(timezone.utc)
        fail_time = datetime.now(timezone.utc)
        _zone_health[zone] = {
            "consecutive_failures": 1,
            "last_failure": fail_time,
        }
        result = get_zone_health()
        assert result[zone]["last_failure"] == fail_time.isoformat()

    @pytest.mark.smoketest
    def test_get_zone_health_summary_shape(self, clean_zone_state):
        """get_zone_health_summary() returns exactly the expected keys."""
        summary = get_zone_health_summary()
        assert set(summary.keys()) == {
            "total_zones",
            "active_zones",
            "disabled",
            "available",
            "exhausted",
            "cooldown_recovery",
            "zone_success_rates",
            "global_quota_cooldown_until",
            "region_quota_cooldowns",
        }
        assert isinstance(summary["disabled"], list)
        assert isinstance(summary["exhausted"], list)
        assert isinstance(summary["cooldown_recovery"], dict)
        assert isinstance(summary["zone_success_rates"], dict)
        # Quota cooldowns (not zone marks) — None / empty when inactive.
        assert summary["global_quota_cooldown_until"] is None
        assert isinstance(summary["region_quota_cooldowns"], dict)

    def test_get_zone_health_summary_total_zones_matches_fallback_list(
        self, clean_zone_state
    ):
        """total_zones equals the length of FALLBACK_ZONES."""
        assert get_zone_health_summary()["total_zones"] == len(FALLBACK_ZONES)

    def test_get_zone_health_summary_no_exhausted_zones(self, clean_zone_state):
        """With no failures, available=total_zones and exhausted=[]."""
        summary = get_zone_health_summary()
        assert summary["available"] == len(FALLBACK_ZONES)
        assert summary["exhausted"] == []

    def test_get_zone_health_summary_counts_exhausted_zones(self, clean_zone_state):
        """Summary correctly counts and names exhausted zones."""
        zones_to_exhaust = [z for z, _ in FALLBACK_ZONES[:2]]
        for zone in zones_to_exhaust:
            _zone_failures[zone] = datetime.now(timezone.utc)
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "last_failure": datetime.now(timezone.utc),
                "last_failure_reason": "RESOURCE_POOL_EXHAUSTED",
                "cooldown_expires_at": datetime.now(timezone.utc)
                + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
            }

        summary = get_zone_health_summary()
        assert len(summary["exhausted"]) == 2
        assert summary["available"] == len(FALLBACK_ZONES) - 2
        for zone in zones_to_exhaust:
            assert zone in summary["exhausted"]

    def test_all_zones_exhausted_fallback_shows_zero_available(self, clean_zone_state):
        """When every zone is in cooldown, available=0 — the all-zones-exhausted state."""
        for zone, _ in FALLBACK_ZONES:
            _zone_failures[zone] = datetime.now(timezone.utc)
            _zone_health[zone] = {
                "consecutive_failures": 3,
                "last_failure": datetime.now(timezone.utc),
                "last_failure_reason": "RESOURCE_POOL_EXHAUSTED",
                "cooldown_expires_at": datetime.now(timezone.utc)
                + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
            }

        summary = get_zone_health_summary()
        assert summary["available"] == 0
        assert len(summary["exhausted"]) == len(FALLBACK_ZONES)

    def test_multiple_zones_independent_cooldowns(self, clean_zone_state):
        """Zones with different cooldown states are reported independently."""
        zones = [z for z, _ in FALLBACK_ZONES[:3]]
        zone_exhausted, zone_recovering, zone_healthy = zones

        # zone_exhausted: in cooldown (fresh failure)
        _zone_failures[zone_exhausted] = datetime.now(timezone.utc)
        _zone_health[zone_exhausted] = {
            "consecutive_failures": 1,
            "cooldown_expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
        }

        # zone_recovering: cooldown expired
        _zone_failures[zone_recovering] = datetime.now(timezone.utc) - timedelta(
            seconds=ZONE_COOLDOWN_SECONDS + 10
        )
        _zone_health[zone_recovering] = {
            "consecutive_failures": 1,
            "cooldown_expires_at": datetime.now(timezone.utc) - timedelta(seconds=10),
        }

        # zone_healthy: no failures
        # (already clean)

        result = get_zone_health()
        assert result[zone_exhausted]["status"] == "exhausted"
        assert result[zone_recovering]["status"] == "recovering"
        assert result[zone_healthy]["status"] == "healthy"

    # ── any_zone_available() ──────────────────────────────────────────────────

    @pytest.mark.smoketest
    def test_any_zone_available_true_when_no_cooldowns(self, clean_zone_state):
        """Returns True when no zone has cooldown state (all zones unknown = available)."""
        assert any_zone_available() is True

    def test_any_zone_available_true_when_one_zone_healthy(self, clean_zone_state):
        """Returns True when at least one zone is not in cooldown."""
        # Put all zones except the last into cooldown
        for zone, _ in FALLBACK_ZONES[:-1]:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": datetime.now(timezone.utc)
                + timedelta(seconds=300),
            }
        assert any_zone_available() is True

    def test_any_zone_available_false_when_all_in_cooldown(self, clean_zone_state):
        """Returns False when every zone has an active (future) cooldown_expires_at."""
        for zone, _ in FALLBACK_ZONES:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": datetime.now(timezone.utc)
                + timedelta(seconds=300),
            }
        assert any_zone_available() is False

    def test_any_zone_available_true_when_cooldown_expired(self, clean_zone_state):
        """Returns True when a zone's cooldown_expires_at is in the past."""
        for zone, _ in FALLBACK_ZONES:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": datetime.now(timezone.utc)
                - timedelta(seconds=10),
            }
        assert any_zone_available() is True

    # ── soonest_zone_recovery() ───────────────────────────────────────────────

    @pytest.mark.smoketest
    def test_soonest_zone_recovery_none_when_no_cooldowns(self, clean_zone_state):
        """Returns None when no zone has cooldown state (all available)."""
        assert soonest_zone_recovery() is None

    def test_soonest_zone_recovery_none_when_zone_available(self, clean_zone_state):
        """Returns None immediately if any zone is available (not all in cooldown)."""
        # At least one zone has no health entry → available
        assert soonest_zone_recovery() is None

    def test_soonest_zone_recovery_returns_earliest_expiry(self, clean_zone_state):
        """Returns the earliest cooldown_expires_at across all zones when all are in cooldown."""
        now = datetime.now(timezone.utc)
        expiries = []
        for i, (zone, _) in enumerate(FALLBACK_ZONES):
            expires = now + timedelta(seconds=300 + i * 60)
            expiries.append(expires)
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": expires,
            }
        result = soonest_zone_recovery()
        assert result is not None
        assert result == expiries[0]  # smallest expiry is first zone

    def test_soonest_zone_recovery_result_is_timezone_aware(self, clean_zone_state):
        """The returned datetime is timezone-aware (UTC), so subtraction with now(UTC) works."""
        now = datetime.now(timezone.utc)
        for zone, _ in FALLBACK_ZONES:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": now + timedelta(seconds=300),
            }
        result = soonest_zone_recovery()
        assert result is not None
        assert result.tzinfo is not None
        # Must not raise TypeError — this is the critical timezone-aware subtraction
        wait_sec = (result - datetime.now(timezone.utc)).total_seconds()
        assert wait_sec > 0


# ─── Max Plan Account Pool ───────────────────────────────────────────────────


class TestMaxPlanAccountPool:
    """Unit tests for Max Plan account selection, cooldown, and re-enable logic."""

    @pytest.mark.asyncio
    async def test_select_max_plan_account_empty_pool_returns_none(
        self, clean_max_plan_pool
    ):
        """Returns None when the pool is empty."""
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_all_disabled_returns_none(
        self, clean_max_plan_pool
    ):
        """Returns None when all pool accounts are disabled."""
        _add_to_pool({"id": "a1", "name": "acct1", "enabled": False, "active_tasks": 0})
        _add_to_pool({"id": "a2", "name": "acct2", "enabled": False, "active_tasks": 0})
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_null_token(self, clean_max_plan_pool):
        """Accounts with no oauth_token (e.g. Secret Manager failure) are skipped."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "no_token",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": None,
            }
        )
        _add_to_pool(
            {"id": "a2", "name": "has_token", "enabled": True, "active_tasks": 0}
        )
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_all_null_tokens_returns_none(
        self, clean_max_plan_pool
    ):
        """Returns None when all accounts have null tokens."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "no_token1",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": None,
            }
        )
        _add_to_pool(
            {
                "id": "a2",
                "name": "no_token2",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": None,
            }
        )
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_selects_lowest_active_tasks(
        self, clean_max_plan_pool
    ):
        """Selects the enabled account with the fewest active tasks."""
        _add_to_pool({"id": "a1", "name": "busy", "enabled": True, "active_tasks": 5})
        _add_to_pool({"id": "a2", "name": "idle", "enabled": True, "active_tasks": 1})
        _add_to_pool({"id": "a3", "name": "medium", "enabled": True, "active_tasks": 3})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_disabled_accounts(
        self, clean_max_plan_pool
    ):
        """Skips disabled accounts even when they have zero active tasks."""
        _add_to_pool(
            {"id": "a1", "name": "disabled_idle", "enabled": False, "active_tasks": 0}
        )
        _add_to_pool(
            {"id": "a2", "name": "enabled_busy", "enabled": True, "active_tasks": 10}
        )
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_single_enabled_account(
        self, clean_max_plan_pool
    ):
        """Returns the only enabled account when there is just one."""
        _add_to_pool({"id": "a1", "name": "only", "enabled": True, "active_tasks": 2})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a1"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_defaults_missing_enabled_key_to_true(
        self, clean_max_plan_pool
    ):
        """An account without an 'enabled' key is treated as enabled (default=True)."""
        _add_to_pool(
            {"id": "a1", "name": "no_key", "active_tasks": 0}
            # deliberately omit "enabled"
        )
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a1"

    @pytest.mark.asyncio
    async def test_release_max_plan_account_decrements_active_tasks(
        self, clean_max_plan_pool
    ):
        """Releasing an account decrements its active_tasks by one."""
        _add_to_pool({"id": "a1", "name": "acct", "enabled": True, "active_tasks": 3})
        await release_max_plan_account("a1")
        assert _account_by_id["a1"]["active_tasks"] == 2

    @pytest.mark.asyncio
    async def test_release_max_plan_account_no_underflow(self, clean_max_plan_pool):
        """active_tasks is clamped at zero — never goes negative."""
        _add_to_pool({"id": "a1", "name": "acct", "enabled": True, "active_tasks": 0})
        await release_max_plan_account("a1")
        assert _account_by_id["a1"]["active_tasks"] == 0

    @pytest.mark.asyncio
    async def test_release_max_plan_account_unknown_id_is_noop(
        self, clean_max_plan_pool
    ):
        """Releasing an unknown account ID neither raises nor modifies other accounts."""
        _add_to_pool({"id": "a1", "name": "acct", "enabled": True, "active_tasks": 2})
        await release_max_plan_account("does-not-exist")
        assert _account_by_id["a1"]["active_tasks"] == 2

    # ── daily_task_count revert on abandon vs. completion ────────────────────
    # A dispatch ABANDONED after account selection must fully undo the reservation
    # (active_tasks AND daily_task_count). A genuine task COMPLETION reverts only
    # the concurrency slot — its daily usage is real and must stand.

    @pytest.mark.asyncio
    async def test_release_reverts_daily_task_count_on_abandon(
        self, clean_max_plan_pool
    ):
        """An abandoned dispatch (revert_daily_task_count=True) undoes BOTH the
        concurrency slot and the pre-reserved daily-usage increment, and marks the
        account dirty so the corrected value is re-persisted."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "acct",
                "enabled": True,
                "active_tasks": 3,
                "daily_task_count": 5,
            }
        )
        await release_max_plan_account("a1", revert_daily_task_count=True)
        assert _account_by_id["a1"]["active_tasks"] == 2
        assert _account_by_id["a1"]["daily_task_count"] == 4
        assert "a1" in _pending_cap_persists

    @pytest.mark.asyncio
    async def test_release_keeps_daily_task_count_on_completion(
        self, clean_max_plan_pool
    ):
        """A genuine completion (default revert_daily_task_count=False) reverts only
        the concurrency slot; the daily-usage increment stays counted."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "acct",
                "enabled": True,
                "active_tasks": 3,
                "daily_task_count": 5,
            }
        )
        await release_max_plan_account("a1")
        assert _account_by_id["a1"]["active_tasks"] == 2
        assert _account_by_id["a1"]["daily_task_count"] == 5

    @pytest.mark.asyncio
    async def test_release_daily_task_count_floors_at_zero(self, clean_max_plan_pool):
        """Reverting the daily counter never drives it negative."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "acct",
                "enabled": True,
                "active_tasks": 1,
                "daily_task_count": 0,
            }
        )
        await release_max_plan_account("a1", revert_daily_task_count=True)
        assert _account_by_id["a1"]["daily_task_count"] == 0

    @pytest.mark.asyncio
    async def test_select_then_abandon_leaves_daily_task_count_unchanged(
        self, clean_max_plan_pool
    ):
        """Selection pre-reserves daily_task_count; abandoning the dispatch must
        leave the counter exactly where it was before selection (no durable
        over-count that would trip the daily cap prematurely)."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "acct",
                "enabled": True,
                "active_tasks": 0,
                "daily_task_count": 7,
            }
        )
        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None and chosen["id"] == "a1"
        # selection pre-reserved both counters
        assert _account_by_id["a1"]["active_tasks"] == 1
        assert _account_by_id["a1"]["daily_task_count"] == 8
        await release_max_plan_account("a1", revert_daily_task_count=True)
        assert _account_by_id["a1"]["active_tasks"] == 0
        assert _account_by_id["a1"]["daily_task_count"] == 7

    @pytest.mark.asyncio
    async def test_select_then_complete_leaves_daily_task_count_incremented(
        self, clean_max_plan_pool
    ):
        """A task that actually runs leaves daily_task_count incremented by one
        after completion, while the concurrency slot is released."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "acct",
                "enabled": True,
                "active_tasks": 0,
                "daily_task_count": 7,
            }
        )
        await select_max_plan_account(_TEST_USER)
        await release_max_plan_account("a1")
        assert _account_by_id["a1"]["active_tasks"] == 0
        assert _account_by_id["a1"]["daily_task_count"] == 8

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_re_enables_after_30min_cooldown(
        self, clean_max_plan_pool
    ):
        """An account disabled more than 30 minutes ago is re-enabled."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "rate_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=31),
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True
        assert _account_by_id["a1"]["disabled_at"] is None

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_stays_disabled_before_cooldown(
        self, clean_max_plan_pool
    ):
        """An account disabled less than 30 minutes ago remains disabled."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "rate_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=10),
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_exactly_at_boundary_re_enables(
        self, clean_max_plan_pool
    ):
        """An account disabled exactly 30 minutes ago is re-enabled (condition uses >=)."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "rate_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=30),
            }
        )
        await recheck_disabled_accounts()
        # recheck_disabled_accounts uses >= COOLDOWN_MINUTES * 60, so exactly 30 min qualifies
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_ignores_enabled_accounts(
        self, clean_max_plan_pool
    ):
        """recheck_disabled_accounts does not modify accounts that are already enabled."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "healthy",
                "enabled": True,
                "active_tasks": 1,
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_skips_missing_disabled_at(
        self, clean_max_plan_pool
    ):
        """A disabled account without a disabled_at timestamp is left alone."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "no_timestamp",
                "enabled": False,
                "active_tasks": 0,
                # no "disabled_at" key — simulates an old record before the field was added
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_rate_limit_disables_account_and_cooldown_re_enables(
        self, clean_max_plan_pool
    ):
        """Full cycle: account disabled on rate limit, cooldown expires, re-enabled."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "claude",
                "enabled": True,
                "active_tasks": 1,
            }
        )

        # Simulate the disable-on-RATE_LIMIT path from update_agent_status
        for acct in _account_by_id.values():
            if acct["id"] == "a1":
                acct["enabled"] = False
                acct["disabled_at"] = _utcnow()

        assert _account_by_id["a1"]["enabled"] is False

        # Cooldown not yet elapsed — still disabled
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

        # Fast-forward: simulate 31 minutes passing
        _account_by_id["a1"]["disabled_at"] = _utcnow() - timedelta(minutes=31)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_multiple_accounts_re_enables_only_eligible(
        self, clean_max_plan_pool
    ):
        """Only accounts past their cooldown are re-enabled; others remain disabled."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "expired",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=45),
            }
        )
        _add_to_pool(
            {
                "id": "a2",
                "name": "fresh",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=5),
            }
        )
        await recheck_disabled_accounts()
        pool = dict(_account_by_id)
        assert pool["a1"]["enabled"] is True  # cooldown expired
        assert pool["a2"]["enabled"] is False  # still in cooldown

    # ── Exponential backoff cooldown tests ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_2(
        self, clean_max_plan_pool
    ):
        """rl_hit_count=2 → 60 min cooldown: re-enables after 61 min, not after 59 min."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "twice_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=59),
                "rl_hit_count": 2,
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 59 min < 60 min cooldown

        _account_by_id["a1"]["disabled_at"] = _utcnow() - timedelta(minutes=61)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True  # 61 min >= 60 min cooldown

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_3(
        self, clean_max_plan_pool
    ):
        """rl_hit_count=3 → 120 min cooldown: re-enables after 121 min, not after 119 min."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "thrice_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=119),
                "rl_hit_count": 3,
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 119 min < 120 min cooldown

        _account_by_id["a1"]["disabled_at"] = _utcnow() - timedelta(minutes=121)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True  # 121 min >= 120 min cooldown

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_4_capped_at_240(
        self, clean_max_plan_pool
    ):
        """rl_hit_count=4 → 240 min cap: re-enables after 241 min, not after 239 min."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "quad_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=239),
                "rl_hit_count": 4,
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 239 min < 240 min cooldown

        _account_by_id["a1"]["disabled_at"] = _utcnow() - timedelta(minutes=241)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True  # 241 min >= 240 min cap

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_high_rl_hit_count_still_capped(
        self, clean_max_plan_pool
    ):
        """rl_hit_count=10 is still capped at 240 min, not 30*2^9=15360 min."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "many_limited",
                "enabled": False,
                "active_tasks": 0,
                "disabled_at": _utcnow() - timedelta(minutes=241),
                "rl_hit_count": 10,
            }
        )
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True  # 241 min >= 240 min cap

    @pytest.mark.asyncio
    async def test_disable_path_increments_rl_hit_count(self, clean_max_plan_pool):
        """The rate-limit disable path increments rl_hit_count on each hit."""
        _add_to_pool(
            {
                "id": "a1",
                "name": "fresh",
                "enabled": True,
                "active_tasks": 1,
            }
        )

        # Simulate the inline disable-on-RATE_LIMIT code from update_agent_status
        async with _max_plan_pool_lock:
            for acct in _account_by_id.values():
                if acct["id"] == "a1":
                    acct["enabled"] = False
                    acct["disabled_at"] = _utcnow()
                    acct["rl_hit_count"] = acct.get("rl_hit_count", 0) + 1

        assert (
            _account_by_id["a1"]["rl_hit_count"] == 1
        )  # first hit → count=1 → 30 min cooldown

        # Re-enable manually, then simulate a second hit
        _account_by_id["a1"]["enabled"] = True
        async with _max_plan_pool_lock:
            for acct in _account_by_id.values():
                if acct["id"] == "a1":
                    acct["enabled"] = False
                    acct["disabled_at"] = _utcnow()
                    acct["rl_hit_count"] = acct.get("rl_hit_count", 0) + 1

        assert (
            _account_by_id["a1"]["rl_hit_count"] == 2
        )  # second hit → count=2 → 60 min cooldown

    # ── Model tier cascade tests ──────────────────────────────────────────────
    # Post-opus-4-8 migration: all new tasks use claude-opus-4-8. Haiku shares
    # Opus's meter exactly — it has no separate bucket, so rate-limit tracking
    # normalizes Haiku → Opus via _normalize_rl_model(). Legacy model names
    # (claude-opus-4-7, claude-opus-4-6, claude-sonnet-4-6) still cascade to the
    # opus-4-8 bucket so prior-queued tasks resolve correctly against the current
    # meter.

    def test_cascade_opus_4_8_covers_opus_4_8(self):
        """Primary model: opus-4-8 cascade is a single entry under the single-tier
        architecture (no separate sonnet bucket)."""
        cascade = cascade_disabled_models("claude-opus-4-8")
        assert cascade == ("claude-opus-4-8",)

    def test_cascade_legacy_opus_4_7_maps_to_opus_4_8(self):
        """Legacy opus-4-7 cascades to the current opus-4-8 meter so prior-queued
        tasks still disable the shared bucket correctly."""
        cascade = cascade_disabled_models("claude-opus-4-7")
        assert cascade == ("claude-opus-4-8",)

    def test_cascade_legacy_opus_4_6_maps_to_opus_4_8(self):
        """Legacy opus-4-6 cascades to the current opus-4-8 meter so prior-queued
        tasks still disable the shared bucket correctly."""
        cascade = cascade_disabled_models("claude-opus-4-6")
        assert cascade == ("claude-opus-4-8",)

    def test_cascade_legacy_sonnet_4_6_maps_to_opus_4_8(self):
        """Legacy sonnet-4-6 cascades to opus-4-8 (single shared meter) — there is no
        longer a separate sonnet-only bucket under the post-migration architecture."""
        cascade = cascade_disabled_models("claude-sonnet-4-6")
        assert cascade == ("claude-opus-4-8",)

    def test_cascade_haiku_normalizes_to_opus_4_8(self):
        """Haiku shares Opus's meter — cascade must key off Opus-4-8's bucket."""
        cascade = cascade_disabled_models("claude-haiku-4-5-20251001")
        assert cascade == ("claude-opus-4-8",)
        assert "claude-haiku-4-5-20251001" not in cascade

    def test_cascade_disabled_models_unknown_falls_back_to_single(self):
        # Unknown variants must not silently cascade to the whole account.
        assert cascade_disabled_models("claude-some-future-model") == (
            "claude-some-future-model",
        )

    def test_opus_rate_limit_blocks_opus_and_haiku_via_mark_cascade(
        self, clean_max_plan_pool
    ):
        """After an Opus-4-8 429, the mark path writes the opus-4-8 cascade entry.
        A Haiku lookup normalizes to Opus-4-8, so haiku requests are also blocked."""
        now = _utcnow()
        acct = {
            "id": "a1",
            "name": "evan",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
            "disabled_models": {
                m: {"hits": 1, "at": now}
                for m in cascade_disabled_models("claude-opus-4-8")
            },
        }
        assert _account_available_for_model(acct, "claude-opus-4-8") is False
        # Haiku normalizes to Opus-4-8 → also blocked (shared meter).
        assert _account_available_for_model(acct, "claude-haiku-4-5-20251001") is False

    def test_account_available_unknown_model_skips_account_with_disables(
        self, clean_max_plan_pool
    ):
        """model=None must NOT bypass the rate-limit check when disables exist.
        Previously returned True, which caused rate-limited accounts to be picked
        for tasks whose model resolution failed upstream."""
        acct = {
            "id": "a1",
            "name": "evan",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
            "disabled_models": {
                "claude-opus-4-8": {"hits": 1, "at": _utcnow()}
            },
        }
        assert _account_available_for_model(acct, None) is False

    def test_account_available_unknown_model_no_disables_allowed(
        self, clean_max_plan_pool
    ):
        """model=None on a healthy account is still allowed (only disables skip it)."""
        acct = {
            "id": "a1",
            "name": "evan",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
        }
        assert _account_available_for_model(acct, None) is True

    @pytest.mark.asyncio
    async def test_select_skips_account_after_opus_rate_limit_cascade(
        self, clean_max_plan_pool
    ):
        """After an Opus-4-8 429 marks the opus-4-8 meter, another Opus-4-8 request must
        skip that account in favor of a healthy one — even if the healthy
        account has more active tasks."""
        now = _utcnow()
        _add_to_pool(
            {
                "id": "a1",
                "name": "rate_limited",
                "enabled": True,
                "active_tasks": 0,
                "disabled_models": {
                    m: {"hits": 1, "at": now}
                    for m in cascade_disabled_models("claude-opus-4-8")
                },
            }
        )
        _add_to_pool(
            {
                "id": "a2",
                "name": "healthy",
                "enabled": True,
                "active_tasks": 5,
            }
        )
        result = await select_max_plan_account(_TEST_USER, model="claude-opus-4-8")
        assert result is not None
        assert (
            result["id"] == "a2"
        )  # healthy account chosen despite higher active_tasks


# ─── Circuit Breaker (healthy / probing / unavailable) ──────────────────────

# Post-migration: all new tasks use opus-4-8, haiku shares opus-4-8's meter.
# Legacy opus-4-7, sonnet-4-6 and opus-4-6 still exist in cascade map for
# backward compat, but tests exercise the current single-tier behavior against
# opus-4-8 directly.
OPUS = "claude-opus-4-8"
HAIKU = "claude-haiku-4-5-20251001"


def _disabled_entry(minutes_ago: int, hits: int = 1) -> dict:
    return {"hits": hits, "at": _utcnow() - timedelta(minutes=minutes_ago)}


def _acct_with_disables(acct_id: str, dm: dict, **extra) -> dict:
    """Build an account dict with the given disabled_models map."""
    return {
        "id": acct_id,
        "name": f"acct-{acct_id}",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "disabled_models": dm,
        **extra,
    }


class TestCircuitBreaker:
    """Three-state circuit breaker: healthy / probing / unavailable.
    Probing is selectable but serialized to one in-flight task per account,
    preventing thundering-herd after cooldown expires on a rate-limited account."""

    # ── State machine ────────────────────────────────────────────────────────

    def test_state_healthy_with_no_disables(self, clean_max_plan_pool):
        acct = {
            "id": "a1",
            "name": "clean",
            "enabled": True,
            "oauth_token": "sk-ant-test",
        }
        assert _account_state_for_model(acct, OPUS) == "healthy"

    def test_state_unavailable_when_availability_excludes_model(
        self, clean_max_plan_pool
    ):
        """account_has_model returns False (confirmed not in visible-models)
        → skip the account for this model so dispatch doesn't 404."""
        acct = {
            "id": "a1",
            "name": "gemini-ga-only",
            "enabled": True,
            "oauth_token": "AIza-x",
            "provider_metadata": {
                "available_models": ["gemini-2.5-pro", "gemini-2.5-flash"],
            },
        }
        assert _account_state_for_model(acct, "gemini-3.1-pro-preview") == "unavailable"

    def test_state_healthy_when_availability_includes_model(self, clean_max_plan_pool):
        acct = {
            "id": "a1",
            "name": "preview-ok",
            "enabled": True,
            "oauth_token": "AIza-x",
            "provider_metadata": {
                "available_models": ["gemini-2.5-pro", "gemini-3.1-pro-preview"],
            },
        }
        assert _account_state_for_model(acct, "gemini-3.1-pro-preview") == "healthy"

    def test_state_healthy_when_availability_unknown(self, clean_max_plan_pool):
        """Unknown availability must NOT skip — fail open. Otherwise a failed
        refresh would starve every account that wasn't probed yet."""
        acct = {
            "id": "a1",
            "name": "not-probed",
            "enabled": True,
            "oauth_token": "AIza-x",
            "provider_metadata": {},  # no available_models key
        }
        assert _account_state_for_model(acct, "gemini-3.1-pro-preview") == "healthy"

    def test_state_unavailable_within_cooldown(self, clean_max_plan_pool):
        # hits=1 → 30min cooldown. 10 min ago → still cooling.
        acct = _acct_with_disables("a1", {OPUS: _disabled_entry(minutes_ago=10)})
        assert _account_state_for_model(acct, OPUS) == "unavailable"

    def test_state_probing_past_cooldown(self, clean_max_plan_pool):
        # hits=1 → 30min cooldown. 35 min ago → elapsed → probing.
        acct = _acct_with_disables("a1", {OPUS: _disabled_entry(minutes_ago=35)})
        assert _account_state_for_model(acct, OPUS) == "probing"

    # ── Two-tier selection ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_prefers_healthy_over_probing(self, clean_max_plan_pool):
        """A healthy account with high load still wins over an idle probing account."""
        _add_to_pool(
            _acct_with_disables("probe", {OPUS: _disabled_entry(minutes_ago=35)})
        )
        _add_to_pool(
            {
                "id": "healthy",
                "name": "healthy",
                "enabled": True,
                "active_tasks": 5,
                "oauth_token": "sk-ant-test",
            }
        )
        result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert result is not None
        assert result["id"] == "healthy"

    @pytest.mark.asyncio
    async def test_select_falls_back_to_probing_when_no_healthy(
        self, clean_max_plan_pool
    ):
        _add_to_pool(
            _acct_with_disables("probe", {OPUS: _disabled_entry(minutes_ago=35)})
        )
        result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert result is not None
        assert result["id"] == "probe"
        assert result["active_tasks"] == 1  # pre-incremented

    @pytest.mark.asyncio
    async def test_select_serializes_probing_account(self, clean_max_plan_pool):
        """Single probing account, already mid-probe (active_tasks=1) → select returns None."""
        acct = _acct_with_disables(
            "probe", {OPUS: _disabled_entry(minutes_ago=35)}, active_tasks=1
        )
        _add_to_pool(acct)
        result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert result is None

    @pytest.mark.asyncio
    async def test_select_parallel_probing_across_accounts(self, clean_max_plan_pool):
        """Two probing accounts both idle → two selects return distinct accounts,
        third returns None (each account serialized to one in-flight probe)."""
        _add_to_pool(
            _acct_with_disables("probe1", {OPUS: _disabled_entry(minutes_ago=35)})
        )
        _add_to_pool(
            _acct_with_disables("probe2", {OPUS: _disabled_entry(minutes_ago=35)})
        )
        r1 = await select_max_plan_account(_TEST_USER, model=OPUS)
        r2 = await select_max_plan_account(_TEST_USER, model=OPUS)
        r3 = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert r1 is not None and r2 is not None
        assert {r1["id"], r2["id"]} == {"probe1", "probe2"}
        assert r3 is None

    # ── Thundering-herd regression ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_thundering_herd_prevented_on_single_probing_account(
        self, clean_max_plan_pool
    ):
        """The core regression: when cooldown expires on the only account, only
        ONE task gets dispatched (the probe), not N tasks in parallel."""
        _add_to_pool(
            _acct_with_disables("probe", {OPUS: _disabled_entry(minutes_ago=35)})
        )
        r1 = await select_max_plan_account(_TEST_USER, model=OPUS)
        r2 = await select_max_plan_account(_TEST_USER, model=OPUS)
        r3 = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert r1 is not None and r1["id"] == "probe"
        assert r2 is None  # serialized: probe already in flight
        assert r3 is None

    # ── Clear-on-success path ────────────────────────────────────────────────
    # Post-opus-4-7 migration: the cascade is single-tier (all new models share
    # the opus-4-7 meter). The old "opus cascades to sonnet" semantics are gone,
    # so success clears only the opus-4-7 bucket. Haiku still normalizes to opus-4-7.

    @pytest.mark.asyncio
    async def test_clear_opus_success_clears_opus_bucket(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Opus success clears the opus-4-7 bucket — the shared meter for all tasks."""
        now = _utcnow()
        _add_to_pool(
            _acct_with_disables(
                "a1",
                {
                    OPUS: {"hits": 1, "at": now},
                },
            )
        )
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        assert await clear_rate_limit_state_on_success("a1", OPUS) is True
        assert (
            "disabled_models" not in _account_by_id["a1"]
            or not _account_by_id["a1"]["disabled_models"]
        )

    @pytest.mark.asyncio
    async def test_clear_haiku_success_clears_opus_bucket(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Haiku success proves Opus-4-7's meter is fine (shared bucket) —
        clearing Haiku normalizes to clearing the opus-4-7 entry."""
        now = _utcnow()
        _add_to_pool(
            _acct_with_disables(
                "a1",
                {
                    OPUS: {"hits": 1, "at": now},
                },
            )
        )
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        assert await clear_rate_limit_state_on_success("a1", HAIKU) is True
        dm = _account_by_id["a1"].get("disabled_models") or {}
        assert dm == {}

    @pytest.mark.asyncio
    async def test_clear_on_healthy_account_is_noop(
        self, clean_max_plan_pool, monkeypatch
    ):
        _add_to_pool(
            {
                "id": "a1",
                "name": "clean",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": "sk-ant-test",
            }
        )
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        assert await clear_rate_limit_state_on_success("a1", OPUS) is False

    # ── Full probe lifecycle ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_probe_success_restores_healthy(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Mark → fast-forward → probing → select → simulate success (clear) → healthy."""
        _add_to_pool(_acct_with_disables("a1", {OPUS: _disabled_entry(minutes_ago=35)}))
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        # First select: probing, grabbed as probe
        probe_result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert probe_result is not None
        assert probe_result["id"] == "a1"

        # Simulate task completion: release + clear
        await release_max_plan_account("a1")
        cleared = await clear_rate_limit_state_on_success("a1", OPUS)
        assert cleared is True

        # Next select sees it as healthy
        assert _account_state_for_model(_account_by_id["a1"], OPUS) == "healthy"
        result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert result is not None
        assert result["id"] == "a1"

    @pytest.mark.asyncio
    async def test_probe_failure_extends_cooldown(self, clean_max_plan_pool):
        """A probe that 429s extends cooldown exponentially; next select returns None."""
        _add_to_pool(_acct_with_disables("a1", {OPUS: _disabled_entry(minutes_ago=35)}))

        # Select the probe
        probe = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert probe is not None
        assert probe["id"] == "a1"

        # Simulate the mark path re-stamping on 429: hits=2, at=now → 60min cooldown
        async with _max_plan_pool_lock:
            _account_by_id["a1"]["disabled_models"][OPUS] = {
                "hits": 2,
                "at": _utcnow(),
            }
        await release_max_plan_account("a1")

        # Now unavailable (fresh 60min cooldown, 0 min elapsed)
        assert _account_state_for_model(_account_by_id["a1"], OPUS) == "unavailable"
        result = await select_max_plan_account(_TEST_USER, model=OPUS)
        assert result is None


async def _async_noop(*args, **kwargs):
    """Test helper: a no-op async function for monkeypatching DB persistence."""
    return None


# ─── Account Health Tracking ─────────────────────────────────────────────────


def _make_acct(acct_id: str, **extra) -> dict:
    """Build a minimal account dict with sane defaults for health tests."""
    return {
        "id": acct_id,
        "name": f"acct-{acct_id}",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        **extra,
    }


class TestHealthScoreComputation:
    """Unit tests for _compute_health_score and get_account_health_stats."""

    def test_empty_account_scores_zero(self):
        """An account with no event history has a neutral score of 0."""
        acct = _make_acct("a1")
        assert _compute_health_score(acct) == 0.0

    def test_completions_increase_score(self):
        """Each completion adds +1 to the health score."""
        now = _utcnow()
        acct = _make_acct(
            "a1",
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 120.0},
                {"at": now - timedelta(hours=2), "duration_seconds": 90.0},
            ],
        )
        assert _compute_health_score(acct, now=now) == 2.0

    def test_rate_limit_events_decrease_score(self):
        """Each rate-limit event subtracts 2 from the health score."""
        now = _utcnow()
        acct = _make_acct(
            "a1",
            rl_events=[
                now - timedelta(hours=1),
                now - timedelta(hours=3),
            ],
        )
        assert _compute_health_score(acct, now=now) == -4.0

    def test_mixed_events_combined_correctly(self):
        """Health score = completions - 2 * rl_events."""
        now = _utcnow()
        acct = _make_acct(
            "a1",
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0},
                {"at": now - timedelta(hours=2), "duration_seconds": 80.0},
                {"at": now - timedelta(hours=5), "duration_seconds": 100.0},
            ],
            rl_events=[now - timedelta(hours=6)],
        )
        # 3 completions - 2*1 rl = 3 - 2 = 1
        assert _compute_health_score(acct, now=now) == 1.0

    def test_events_outside_window_excluded(self):
        """Events older than 24h are not counted in the health score."""
        now = _utcnow()
        cutoff_plus_one = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS + 1)
        acct = _make_acct(
            "a1",
            completion_events=[{"at": cutoff_plus_one, "duration_seconds": 60.0}],
            rl_events=[cutoff_plus_one],
        )
        assert _compute_health_score(acct, now=now) == 0.0

    def test_events_at_window_boundary_excluded(self):
        """Events exactly at the 24h cutoff (not strictly greater) are excluded."""
        now = _utcnow()
        exactly_at_cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
        acct = _make_acct(
            "a1",
            completion_events=[{"at": exactly_at_cutoff, "duration_seconds": 30.0}],
        )
        assert _compute_health_score(acct, now=now) == 0.0

    def test_get_account_health_stats_structure(self):
        """get_account_health_stats returns the expected keys."""
        acct = _make_acct("a1")
        stats = get_account_health_stats(acct)
        assert set(stats.keys()) == {
            "rl_events_24h",
            "completions_24h",
            "avg_duration_seconds",
            "health_score",
            "success_rate_7d",
            "success_rate_by_type_7d",
        }

    def test_get_account_health_stats_avg_duration(self):
        """avg_duration_seconds is the mean of recent completion durations."""
        now = _utcnow()
        acct = _make_acct(
            "a1",
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0},
                {"at": now - timedelta(hours=2), "duration_seconds": 120.0},
            ],
        )
        stats = get_account_health_stats(acct, now=now)
        assert stats["avg_duration_seconds"] == 90.0
        assert stats["completions_24h"] == 2

    def test_get_account_health_stats_avg_duration_none_when_no_completions(self):
        """avg_duration_seconds is None when there are no recent completions."""
        acct = _make_acct("a1")
        assert get_account_health_stats(acct)["avg_duration_seconds"] is None

    def test_get_account_health_stats_rl_count(self):
        """rl_events_24h counts rate-limit events within the window."""
        now = _utcnow()
        acct = _make_acct(
            "a1",
            rl_events=[
                now - timedelta(hours=1),
                now - timedelta(hours=23),
                now - timedelta(hours=25),  # outside window
            ],
        )
        stats = get_account_health_stats(acct, now=now)
        assert stats["rl_events_24h"] == 2


class TestAccountSelectionByHealthScore:
    """Tests for health-score-aware account selection in select_max_plan_account."""

    @pytest.mark.asyncio
    async def test_select_prefers_higher_health_score(self, clean_max_plan_pool):
        """Account with more completions and fewer rate limits is preferred over idle account."""
        now = _utcnow()
        # Healthy account with good track record
        _add_to_pool(
            _make_acct(
                "good",
                active_tasks=1,
                completion_events=[
                    {"at": now - timedelta(hours=1), "duration_seconds": 60.0},
                    {"at": now - timedelta(hours=2), "duration_seconds": 90.0},
                ],
            )
        )
        # Idle account with recent rate limits (lower health score)
        _add_to_pool(
            _make_acct(
                "bad",
                active_tasks=0,
                rl_events=[
                    now - timedelta(hours=1),
                    now - timedelta(hours=3),
                ],
            )
        )
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "good", (
            "Account with better health score should be preferred"
        )

    @pytest.mark.asyncio
    async def test_select_uses_active_tasks_as_tiebreaker(self, clean_max_plan_pool):
        """Among accounts with equal health scores, fewest active_tasks wins."""
        _add_to_pool(_make_acct("busy", active_tasks=5))
        _add_to_pool(_make_acct("idle", active_tasks=1))
        _add_to_pool(_make_acct("medium", active_tasks=3))
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "idle"

    @pytest.mark.asyncio
    async def test_select_new_accounts_neutral_score_compete_by_load(
        self, clean_max_plan_pool
    ):
        """New accounts with no history all score 0 — load balancing takes over."""
        _add_to_pool(_make_acct("a1", active_tasks=0))
        _add_to_pool(_make_acct("a2", active_tasks=2))
        _add_to_pool(_make_acct("a3", active_tasks=1))
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a1"  # fewest tasks among equal-score accounts

    @pytest.mark.asyncio
    async def test_disable_account_model_records_rl_event(
        self, clean_max_plan_pool, monkeypatch
    ):
        """disable_account_model records a rate-limit event for health tracking."""
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        _add_to_pool(_make_acct("a1"))
        await disable_account_model("a1", OPUS)
        assert len(_account_by_id["a1"].get("rl_events", [])) == 1

    @pytest.mark.asyncio
    async def test_multiple_rate_limits_accumulate_in_rl_events(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Each disable call appends to rl_events, lowering the health score."""
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        _add_to_pool(_make_acct("a1"))
        # Opus and Haiku share the same meter (normalized) — each disable still
        # appends a distinct rl_event, so two calls yield two events.
        await disable_account_model("a1", OPUS)
        await disable_account_model("a1", HAIKU)
        assert len(_account_by_id["a1"].get("rl_events", [])) == 2
        assert _compute_health_score(_account_by_id["a1"]) == -4.0

    @pytest.mark.asyncio
    async def test_record_completion_event_adds_to_events(self, clean_max_plan_pool):
        """record_completion_event stores duration and timestamp for the account."""
        _add_to_pool(_make_acct("a1"))
        await record_completion_event("a1", duration_seconds=150.0)
        events = _account_by_id["a1"].get("completion_events", [])
        assert len(events) == 1
        assert events[0]["duration_seconds"] == 150.0
        assert isinstance(events[0]["at"], datetime)

    @pytest.mark.asyncio
    async def test_record_completion_event_unknown_account_noop(
        self, clean_max_plan_pool
    ):
        """record_completion_event for an unknown account silently does nothing."""
        await record_completion_event(
            "does-not-exist", duration_seconds=60.0
        )  # no raise

    @pytest.mark.asyncio
    async def test_health_score_improves_after_completion_event(
        self, clean_max_plan_pool, monkeypatch
    ):
        """After a successful task, health score increases and the account remains preferred."""
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        # Account with one past rate limit
        _add_to_pool(_make_acct("a1"))
        await disable_account_model("a1", OPUS)
        score_after_rl = _compute_health_score(_account_by_id["a1"])
        assert score_after_rl == -2.0

        # Now a successful completion
        await record_completion_event("a1", duration_seconds=90.0)
        score_after_completion = _compute_health_score(_account_by_id["a1"])
        assert score_after_completion == -1.0  # -2 + 1 = -1

    @pytest.mark.asyncio
    async def test_select_deprefers_chronically_rate_limited_account(
        self, clean_max_plan_pool, monkeypatch
    ):
        """An account with multiple recent rate limits loses to a healthier alternative."""
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        now = _utcnow()

        # Chronically rate-limited account (but currently healthy — models cleared)
        _add_to_pool(
            _make_acct(
                "chronic",
                active_tasks=0,
                rl_events=[
                    now - timedelta(hours=1),
                    now - timedelta(hours=3),
                    now - timedelta(hours=6),
                ],
            )
        )
        # Clean account with modest load
        _add_to_pool(_make_acct("clean", active_tasks=2))

        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "clean", (
            "Healthier account should be chosen despite higher load"
        )


class TestDisabledStatePersistenceAndRestoration:
    """Verify that disabled_models state persists to DB and is restored on startup.

    The persistence mechanism was already present before this feature.
    These tests document and regression-test the existing behavior.
    """

    @pytest.mark.asyncio
    async def test_disable_persists_to_db(self, clean_max_plan_pool, monkeypatch):
        """disable_account_model calls _persist_rate_limit_state to save to DB."""
        persist_calls = []

        async def _capture_persist(acct_id):
            persist_calls.append(acct_id)

        monkeypatch.setattr(
            "app.max_plan_pool._persist_rate_limit_state", _capture_persist
        )
        _add_to_pool(_make_acct("a1"))
        await disable_account_model("a1", OPUS)
        assert "a1" in persist_calls, (
            "_persist_rate_limit_state must be called on rate limit"
        )

    @pytest.mark.asyncio
    async def test_clear_on_success_persists_to_db(
        self, clean_max_plan_pool, monkeypatch
    ):
        """clear_rate_limit_state_on_success calls _persist_rate_limit_state."""
        persist_calls = []

        async def _capture_persist(acct_id):
            persist_calls.append(acct_id)

        monkeypatch.setattr(
            "app.max_plan_pool._persist_rate_limit_state", _capture_persist
        )
        now = datetime.now(timezone.utc)
        _add_to_pool(
            _acct_with_disables(
                "a1", {OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}}
            )
        )
        await clear_rate_limit_state_on_success("a1", OPUS)
        assert "a1" in persist_calls

    def test_load_max_plan_pool_restores_active_cooldown(self):
        """Verifies that disabled_models within active cooldown are restored after restart.

        This tests the in-memory restoration logic from load_max_plan_pool() directly
        by simulating the state that would come from the DB.
        """
        from app.max_plan_pool import _account_state_for_model

        # _utcnow() returns naive datetimes — use _utcnow() to match
        now = _utcnow()
        # Build an entry that simulates what load_max_plan_pool would restore
        restored_entry = {
            "id": "a1",
            "name": "restored",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
            "disabled_models": {
                OPUS: {
                    "hits": 1,
                    "at": now - timedelta(minutes=10),
                },  # 30min cooldown, only 10min elapsed
            },
        }
        # The in-memory dict uses datetime objects for "at" (as load_max_plan_pool converts ISO strings)
        assert _account_state_for_model(restored_entry, OPUS) == "unavailable"

    def test_load_max_plan_pool_skips_expired_cooldown_on_restore(self):
        """Cooldowns that elapsed while the service was down are not restored."""
        from app.max_plan_pool import _account_state_for_model

        now = _utcnow()
        # Cooldown elapsed (hits=1 → 30min, but 45min have passed)
        expired_entry = {
            "id": "a1",
            "name": "expired",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
            "disabled_models": {
                OPUS: {"hits": 1, "at": now - timedelta(minutes=45)},
            },
        }
        # Should be probing (cooldown elapsed) rather than unavailable
        assert _account_state_for_model(expired_entry, OPUS) == "probing"

    def test_summarize_pool_state_distinguishes_rate_limited_from_other(self):
        """summarize_pool_state should count healthy/probing/rate_limited buckets
        and surface the earliest cooldown recovery time."""
        from app.max_plan_pool import (
            summarize_pool_state,
            _account_by_id,
            _max_plan_pools,
        )

        _account_by_id.clear()
        _max_plan_pools.clear()
        now = _utcnow()
        a_healthy = {
            "id": "h",
            "name": "h",
            "user_id": "u",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-1",
        }
        a_rl_short = {
            "id": "r1",
            "name": "r1",
            "user_id": "u",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-2",
            "disabled_models": {OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
        }
        a_rl_long = {
            "id": "r2",
            "name": "r2",
            "user_id": "u",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-3",
            "disabled_models": {OPUS: {"hits": 2, "at": now - timedelta(minutes=1)}},
        }
        a_no_token = {
            "id": "n",
            "name": "n",
            "user_id": "u",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": None,
        }
        for a in (a_healthy, a_rl_short, a_rl_long, a_no_token):
            _account_by_id[a["id"]] = a
            _max_plan_pools.setdefault("u", []).append(a)
        summary = summarize_pool_state(OPUS)
        assert summary["total"] == 4
        assert summary["healthy"] == 1
        assert summary["rate_limited"] == 2
        assert summary["other_unavailable"] == 1
        # Earliest recovery comes from a_rl_short (hits=1, 30min cooldown, started 5min ago → ~25min from now)
        assert summary["earliest_recovery"] is not None
        assert summary["earliest_recovery"] < now + timedelta(minutes=30)
        _account_by_id.clear()
        _max_plan_pools.clear()


# ─── disable_account_model (shared rate-limit mark path) ────────────────────


class TestDisableAccountModel:
    """The helper used by both agent_status.py and reaper.py to mark an
    account's cascade of models as rate-limited. Regression target: without
    this being called from reaper.py, the orchestrator's blacklist pruning
    strips the entry on the next dispatch cycle and the same account is
    re-selected on every requeue (observed as hit #17 on one account while
    three others sat idle).

    Post-opus-4-7 migration: the cascade is single-tier. All new models
    (opus-4-7 and haiku via normalization) share one bucket. Legacy sonnet-4-6
    / opus-4-6 also cascade into opus-4-7."""

    @pytest.mark.asyncio
    async def test_disables_opus_writes_single_bucket(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Opus-4-7 429 writes the single opus-4-7 cascade entry — no sonnet key
        under the post-migration single-tier architecture."""
        _add_to_pool({"id": "a1", "name": "opus_limited", "active_tasks": 0})
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        await disable_account_model("a1", OPUS)

        dm = _account_by_id["a1"]["disabled_models"]
        assert OPUS in dm
        assert HAIKU not in dm, "Haiku is never a separate key — it shares Opus's meter"
        assert dm[OPUS]["hits"] == 1
        assert _account_state_for_model(_account_by_id["a1"], OPUS) == "unavailable"
        # Haiku lookup normalizes to Opus → also blocked.
        assert _account_state_for_model(_account_by_id["a1"], HAIKU) == "unavailable"

    @pytest.mark.asyncio
    async def test_opus_disable_blocks_haiku_via_shared_meter(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Opus-4-7 429 writes {opus-4-7}. Haiku lookup normalizes to Opus-4-7,
        so haiku requests are also blocked (shared meter)."""
        _add_to_pool({"id": "a1", "name": "opus_limited", "active_tasks": 0})
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        await disable_account_model("a1", OPUS)

        dm = _account_by_id["a1"]["disabled_models"]
        assert OPUS in dm
        assert HAIKU not in dm, "Haiku is never a separate key — it shares Opus's meter"
        assert _account_state_for_model(_account_by_id["a1"], HAIKU) == "unavailable"

    @pytest.mark.asyncio
    async def test_disable_haiku_marks_opus_shared_meter(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Haiku 429 normalizes to Opus-4-7: writes {opus-4-7}, blocks both haiku and opus-4-7."""
        _add_to_pool({"id": "a1", "name": "haiku_limited", "active_tasks": 0})
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        await disable_account_model("a1", HAIKU)

        dm = _account_by_id["a1"]["disabled_models"]
        assert OPUS in dm, "Haiku 429 must disable Opus-4-7 (shared meter)"
        assert HAIKU not in dm
        assert _account_state_for_model(_account_by_id["a1"], OPUS) == "unavailable"
        assert _account_state_for_model(_account_by_id["a1"], HAIKU) == "unavailable"

    @pytest.mark.asyncio
    async def test_repeated_disable_increments_hits(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Each call to disable_account_model bumps the hit counter and timestamp."""
        _add_to_pool({"id": "a1", "name": "repeat", "active_tasks": 0})
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        await disable_account_model("a1", OPUS)
        assert _account_by_id["a1"]["disabled_models"][OPUS]["hits"] == 1

        await disable_account_model("a1", OPUS)
        assert _account_by_id["a1"]["disabled_models"][OPUS]["hits"] == 2

        await disable_account_model("a1", OPUS)
        assert _account_by_id["a1"]["disabled_models"][OPUS]["hits"] == 3

    @pytest.mark.asyncio
    async def test_missing_account_is_silent_noop(
        self, clean_max_plan_pool, monkeypatch
    ):
        """Calling with an unknown account id is a silent no-op (doesn't raise)."""
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)
        await disable_account_model("does-not-exist", OPUS)  # should not raise

    @pytest.mark.asyncio
    async def test_persist_failure_queues_retry(self, clean_max_plan_pool, monkeypatch):
        """When DB persist fails, the account is queued in _pending_rl_persists."""
        from sqlalchemy.exc import SQLAlchemyError

        _add_to_pool({"id": "a1", "name": "persist_fail", "active_tasks": 0})

        async def _raising(*args, **kwargs):
            raise SQLAlchemyError("simulated DB outage")

        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _raising)
        _pending_rl_persists.clear()

        await disable_account_model("a1", OPUS)

        # In-memory state still updated
        assert OPUS in _account_by_id["a1"]["disabled_models"]
        # Retry queue has the account
        assert "a1" in _pending_rl_persists
        _pending_rl_persists.clear()

    @pytest.mark.asyncio
    async def test_blacklist_pruning_preserves_disabled_account(
        self, clean_max_plan_pool, monkeypatch
    ):
        """End-to-end regression: after disable_account_model, the orchestrator's
        blacklist-pruning predicate must keep the account in the blacklist.

        The pruning logic at orchestrator.py:716-726 is:
            _blacklist = [aid for aid in _blacklist
                          if not _account_available_for_model(..., model)]
        With the reaper path now calling disable_account_model, this predicate
        must evaluate to False (i.e. keep the entry) — meaning
        _account_available_for_model returns False after the disable."""
        _add_to_pool({"id": "a1", "name": "will_prune", "active_tasks": 0})
        monkeypatch.setattr("app.max_plan_pool._persist_rate_limit_state", _async_noop)

        # Before disable: account is healthy → blacklist entry gets pruned.
        pre_blacklist = ["a1"]
        pre_pruned = [
            aid
            for aid in pre_blacklist
            if not _account_available_for_model(_account_by_id[aid], OPUS)
        ]
        assert pre_pruned == [], "healthy account should be pruned from task blacklist"

        # After disable: account is unavailable → blacklist entry is preserved.
        await disable_account_model("a1", OPUS)
        post_pruned = [
            aid
            for aid in pre_blacklist
            if not _account_available_for_model(_account_by_id[aid], OPUS)
        ]
        assert post_pruned == ["a1"], (
            "disabled account must stay in blacklist so it isn't re-selected — this is "
            "the fix for the hit #17 thundering-herd loop"
        )
