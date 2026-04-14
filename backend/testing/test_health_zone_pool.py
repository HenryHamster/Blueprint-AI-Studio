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

import app.shared_state as _shared_state
import app.main as _main_module
import app.orchestrator as _orch_module
from app.main import app
from app.max_plan_pool import (
    select_max_plan_account,
    release_max_plan_account,
    recheck_disabled_accounts,
    _max_plan_pools,
    _account_by_id,
    _max_plan_pool_lock,
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
)


# ─── Shared Fixtures ────────────────────────────────────────────────────────

@pytest.fixture()
def clean_zone_state():
    """Reset zone health dicts before and after each zone test."""
    _zone_failures.clear()
    _zone_health.clear()
    yield
    _zone_failures.clear()
    _zone_health.clear()


_TEST_USER = "test-user-123"

def _add_to_pool(entry: dict):
    """Helper: add an account entry to the test user's pool and flat lookup."""
    entry.setdefault("user_id", _TEST_USER)
    entry.setdefault("oauth_token", "sk-ant-test-token")  # Accounts need a token to be selectable
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
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


@pytest.fixture()
def healthy_orchestrator():
    """Set _orchestrator_last_cycle to now so /health sees a live orchestrator."""
    original = _orch_module._orchestrator_last_cycle
    _orch_module._orchestrator_last_cycle = datetime.utcnow()
    yield
    _orch_module._orchestrator_last_cycle = original


@pytest.fixture()
def all_deps_healthy():
    """Ensure all external deps are True for the duration of the test."""
    original = dict(_shared_state.external_health)
    _shared_state.external_health.update({
        "github_api": True,
        "claude_api": True,
        "gcp": True,
        "db": True,
        "last_check": None,
        "failed_deps": [],
        "errors": {},
    })
    yield
    _shared_state.external_health.clear()
    _shared_state.external_health.update(original)


@pytest_asyncio.fixture()
async def health_client():
    """Lightweight AsyncClient for /health — no DB dependency needed.
    The health endpoint is unauthenticated and does not touch the database."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ─── /health Endpoint ────────────────────────────────────────────────────────

@pytest.mark.smoketest
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
        """Returns 503 when the orchestrator has never completed a cycle."""
        original = _orch_module._orchestrator_last_cycle
        _orch_module._orchestrator_last_cycle = None
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert data["orchestrator_alive"] is False
            assert data["orchestrator_cycle_age_seconds"] is None
        finally:
            _orch_module._orchestrator_last_cycle = original

    @pytest.mark.asyncio
    async def test_health_stale_orchestrator_returns_503(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """Returns 503 when the orchestrator last ran more than 60 seconds ago."""
        original = _orch_module._orchestrator_last_cycle
        _orch_module._orchestrator_last_cycle = datetime.utcnow() - timedelta(seconds=90)
        try:
            resp = await health_client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"
        finally:
            _orch_module._orchestrator_last_cycle = original

    @pytest.mark.asyncio
    async def test_health_cycle_age_seconds_reflects_elapsed_time(
        self, health_client: AsyncClient, all_deps_healthy
    ):
        """orchestrator_cycle_age_seconds is roughly the time since last cycle."""
        original = _orch_module._orchestrator_last_cycle
        _orch_module._orchestrator_last_cycle = datetime.utcnow() - timedelta(seconds=10)
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
        for key in ("github_api", "claude_api", "gcp", "db"):
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
    def test_get_zone_health_returns_entry_for_every_fallback_zone(self, clean_zone_state):
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
            "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
        }

        result = get_zone_health()
        assert result[zone]["status"] == "exhausted"
        assert result[zone]["cooldown_active"] is True
        assert result[zone]["consecutive_failures"] == 1

    def test_zone_status_recovering_after_cooldown_expires(self, clean_zone_state):
        """A zone whose cooldown window has passed reports status=recovering."""
        zone = FALLBACK_ZONES[0][0]
        old_time = datetime.now(timezone.utc) - timedelta(seconds=ZONE_COOLDOWN_SECONDS + 10)
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
        assert set(summary.keys()) == {"total_zones", "available", "exhausted", "cooldown_recovery"}
        assert isinstance(summary["exhausted"], list)
        assert isinstance(summary["cooldown_recovery"], dict)

    def test_get_zone_health_summary_total_zones_matches_fallback_list(self, clean_zone_state):
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
                "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
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
                "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
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
            "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=ZONE_COOLDOWN_SECONDS),
        }

        # zone_recovering: cooldown expired
        _zone_failures[zone_recovering] = datetime.now(timezone.utc) - timedelta(seconds=ZONE_COOLDOWN_SECONDS + 10)
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
                "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=300),
            }
        assert any_zone_available() is True

    def test_any_zone_available_false_when_all_in_cooldown(self, clean_zone_state):
        """Returns False when every zone has an active (future) cooldown_expires_at."""
        for zone, _ in FALLBACK_ZONES:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": datetime.now(timezone.utc) + timedelta(seconds=300),
            }
        assert any_zone_available() is False

    def test_any_zone_available_true_when_cooldown_expired(self, clean_zone_state):
        """Returns True when a zone's cooldown_expires_at is in the past."""
        for zone, _ in FALLBACK_ZONES:
            _zone_health[zone] = {
                "consecutive_failures": 1,
                "cooldown_expires_at": datetime.now(timezone.utc) - timedelta(seconds=10),
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
    async def test_select_max_plan_account_empty_pool_returns_none(self, clean_max_plan_pool):
        """Returns None when the pool is empty."""
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_all_disabled_returns_none(self, clean_max_plan_pool):
        """Returns None when all pool accounts are disabled."""
        _add_to_pool({"id": "a1", "name": "acct1", "enabled": False, "active_tasks": 0})
        _add_to_pool({"id": "a2", "name": "acct2", "enabled": False, "active_tasks": 0})
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_null_token(self, clean_max_plan_pool):
        """Accounts with no oauth_token (e.g. Secret Manager failure) are skipped."""
        _add_to_pool({"id": "a1", "name": "no_token", "enabled": True, "active_tasks": 0, "oauth_token": None})
        _add_to_pool({"id": "a2", "name": "has_token", "enabled": True, "active_tasks": 0})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_all_null_tokens_returns_none(self, clean_max_plan_pool):
        """Returns None when all accounts have null tokens."""
        _add_to_pool({"id": "a1", "name": "no_token1", "enabled": True, "active_tasks": 0, "oauth_token": None})
        _add_to_pool({"id": "a2", "name": "no_token2", "enabled": True, "active_tasks": 0, "oauth_token": None})
        assert await select_max_plan_account(_TEST_USER) is None

    @pytest.mark.asyncio
    async def test_select_max_plan_account_selects_lowest_active_tasks(self, clean_max_plan_pool):
        """Selects the enabled account with the fewest active tasks."""
        _add_to_pool({"id": "a1", "name": "busy",   "enabled": True, "active_tasks": 5})
        _add_to_pool({"id": "a2", "name": "idle",   "enabled": True, "active_tasks": 1})
        _add_to_pool({"id": "a3", "name": "medium", "enabled": True, "active_tasks": 3})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_disabled_accounts(self, clean_max_plan_pool):
        """Skips disabled accounts even when they have zero active tasks."""
        _add_to_pool({"id": "a1", "name": "disabled_idle", "enabled": False, "active_tasks": 0})
        _add_to_pool({"id": "a2", "name": "enabled_busy",  "enabled": True,  "active_tasks": 10})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a2"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_single_enabled_account(self, clean_max_plan_pool):
        """Returns the only enabled account when there is just one."""
        _add_to_pool({"id": "a1", "name": "only", "enabled": True, "active_tasks": 2})
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a1"

    @pytest.mark.asyncio
    async def test_select_max_plan_account_defaults_missing_enabled_key_to_true(self, clean_max_plan_pool):
        """An account without an 'enabled' key is treated as enabled (default=True)."""
        _add_to_pool(
            {"id": "a1", "name": "no_key", "active_tasks": 0}
            # deliberately omit "enabled"
        )
        result = await select_max_plan_account(_TEST_USER)
        assert result is not None
        assert result["id"] == "a1"

    @pytest.mark.asyncio
    async def test_release_max_plan_account_decrements_active_tasks(self, clean_max_plan_pool):
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
    async def test_release_max_plan_account_unknown_id_is_noop(self, clean_max_plan_pool):
        """Releasing an unknown account ID neither raises nor modifies other accounts."""
        _add_to_pool({"id": "a1", "name": "acct", "enabled": True, "active_tasks": 2})
        await release_max_plan_account("does-not-exist")
        assert _account_by_id["a1"]["active_tasks"] == 2

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_re_enables_after_30min_cooldown(self, clean_max_plan_pool):
        """An account disabled more than 30 minutes ago is re-enabled."""
        _add_to_pool({
            "id": "a1",
            "name": "rate_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=31),
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True
        assert _account_by_id["a1"]["disabled_at"] is None

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_stays_disabled_before_cooldown(self, clean_max_plan_pool):
        """An account disabled less than 30 minutes ago remains disabled."""
        _add_to_pool({
            "id": "a1",
            "name": "rate_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=10),
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_exactly_at_boundary_re_enables(self, clean_max_plan_pool):
        """An account disabled exactly 30 minutes ago is re-enabled (condition uses >=)."""
        _add_to_pool({
            "id": "a1",
            "name": "rate_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=30),
        })
        await recheck_disabled_accounts()
        # recheck_disabled_accounts uses >= COOLDOWN_MINUTES * 60, so exactly 30 min qualifies
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_ignores_enabled_accounts(self, clean_max_plan_pool):
        """recheck_disabled_accounts does not modify accounts that are already enabled."""
        _add_to_pool({
            "id": "a1",
            "name": "healthy",
            "enabled": True,
            "active_tasks": 1,
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_disabled_accounts_skips_missing_disabled_at(self, clean_max_plan_pool):
        """A disabled account without a disabled_at timestamp is left alone."""
        _add_to_pool({
            "id": "a1",
            "name": "no_timestamp",
            "enabled": False,
            "active_tasks": 0,
            # no "disabled_at" key — simulates an old record before the field was added
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_rate_limit_disables_account_and_cooldown_re_enables(self, clean_max_plan_pool):
        """Full cycle: account disabled on rate limit, cooldown expires, re-enabled."""
        _add_to_pool({
            "id": "a1",
            "name": "claude",
            "enabled": True,
            "active_tasks": 1,
        })

        # Simulate the disable-on-RATE_LIMIT path from update_agent_status
        for acct in _account_by_id.values():
            if acct["id"] == "a1":
                acct["enabled"] = False
                acct["disabled_at"] = datetime.utcnow()

        assert _account_by_id["a1"]["enabled"] is False

        # Cooldown not yet elapsed — still disabled
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False

        # Fast-forward: simulate 31 minutes passing
        _account_by_id["a1"]["disabled_at"] = datetime.utcnow() - timedelta(minutes=31)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_recheck_multiple_accounts_re_enables_only_eligible(self, clean_max_plan_pool):
        """Only accounts past their cooldown are re-enabled; others remain disabled."""
        _add_to_pool({
            "id": "a1",
            "name": "expired",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=45),
        })
        _add_to_pool({
            "id": "a2",
            "name": "fresh",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=5),
        })
        await recheck_disabled_accounts()
        pool = dict(_account_by_id)
        assert pool["a1"]["enabled"] is True   # cooldown expired
        assert pool["a2"]["enabled"] is False  # still in cooldown

    # ── Exponential backoff cooldown tests ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_2(self, clean_max_plan_pool):
        """rl_hit_count=2 → 60 min cooldown: re-enables after 61 min, not after 59 min."""
        _add_to_pool({
            "id": "a1",
            "name": "twice_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=59),
            "rl_hit_count": 2,
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 59 min < 60 min cooldown

        _account_by_id["a1"]["disabled_at"] = datetime.utcnow() - timedelta(minutes=61)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True   # 61 min >= 60 min cooldown

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_3(self, clean_max_plan_pool):
        """rl_hit_count=3 → 120 min cooldown: re-enables after 121 min, not after 119 min."""
        _add_to_pool({
            "id": "a1",
            "name": "thrice_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=119),
            "rl_hit_count": 3,
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 119 min < 120 min cooldown

        _account_by_id["a1"]["disabled_at"] = datetime.utcnow() - timedelta(minutes=121)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True   # 121 min >= 120 min cooldown

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_rl_hit_count_4_capped_at_240(self, clean_max_plan_pool):
        """rl_hit_count=4 → 240 min cap: re-enables after 241 min, not after 239 min."""
        _add_to_pool({
            "id": "a1",
            "name": "quad_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=239),
            "rl_hit_count": 4,
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is False  # 239 min < 240 min cooldown

        _account_by_id["a1"]["disabled_at"] = datetime.utcnow() - timedelta(minutes=241)
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True   # 241 min >= 240 min cap

    @pytest.mark.asyncio
    async def test_recheck_exponential_backoff_high_rl_hit_count_still_capped(self, clean_max_plan_pool):
        """rl_hit_count=10 is still capped at 240 min, not 30*2^9=15360 min."""
        _add_to_pool({
            "id": "a1",
            "name": "many_limited",
            "enabled": False,
            "active_tasks": 0,
            "disabled_at": datetime.utcnow() - timedelta(minutes=241),
            "rl_hit_count": 10,
        })
        await recheck_disabled_accounts()
        assert _account_by_id["a1"]["enabled"] is True   # 241 min >= 240 min cap

    @pytest.mark.asyncio
    async def test_disable_path_increments_rl_hit_count(self, clean_max_plan_pool):
        """The rate-limit disable path increments rl_hit_count on each hit."""
        _add_to_pool({
            "id": "a1",
            "name": "fresh",
            "enabled": True,
            "active_tasks": 1,
        })

        # Simulate the inline disable-on-RATE_LIMIT code from update_agent_status
        async with _max_plan_pool_lock:
            for acct in _account_by_id.values():
                if acct["id"] == "a1":
                    acct["enabled"] = False
                    acct["disabled_at"] = datetime.utcnow()
                    acct["rl_hit_count"] = acct.get("rl_hit_count", 0) + 1

        assert _account_by_id["a1"]["rl_hit_count"] == 1  # first hit → count=1 → 30 min cooldown

        # Re-enable manually, then simulate a second hit
        _account_by_id["a1"]["enabled"] = True
        async with _max_plan_pool_lock:
            for acct in _account_by_id.values():
                if acct["id"] == "a1":
                    acct["enabled"] = False
                    acct["disabled_at"] = datetime.utcnow()
                    acct["rl_hit_count"] = acct.get("rl_hit_count", 0) + 1

        assert _account_by_id["a1"]["rl_hit_count"] == 2  # second hit → count=2 → 60 min cooldown
