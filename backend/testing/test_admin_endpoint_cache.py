"""Cache behavior tests for admin polling endpoints (Phase E2 / M-P4).

Verifies the TTL+in-flight-dedup pattern (replicated from admin_digest.py)
applied to:
  - /api/admin/health-overview
  - /api/admin/max-plan-accounts/health

The compounded admin-dashboard polling load (DashboardHome polls every
30s, multiple admins polling in parallel) makes deduplication a
correctness-adjacent invariant: without it, repeated multi-thousand-row
JSONB scans run in parallel and have caused OOM/instance kill before
(see digest_cache.py docstring).
"""

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.digest_cache import DigestCache
from app.main import app
from app.routers import admin_health as _admin_health
from app.routers import admin_pool_accounts as _admin_pool_accounts
from app.routers.admin_health import get_health_overview  # noqa: F401  (route registered)
from app.routers.admin_pool_accounts import get_account_health_summary  # noqa: F401
from app.digest_cache import get_digest_cache


# ---------------------------------------------------------------------------
# /api/admin/health-overview cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_overview_caches_repeated_calls_within_ttl(
    client: AsyncClient, db_session, mocker
):
    """Two calls within TTL → underlying compute runs once."""
    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    expected: Dict[str, Any] = {
        "overall_status": "healthy",
        "success_rate": {"current": 1.0, "trend": None, "window_hours": 24},
        "actionable_issues": [],
        "direction_portfolio": {},
        "queue_depth": {"queued": 0, "active": 0, "evaluating": 0},
    }
    spy = AsyncMock(return_value=expected)
    mocker.patch.object(_admin_health, "_compute_health_overview", spy)

    try:
        r1 = await client.get("/api/admin/health-overview")
        r2 = await client.get("/api/admin/health-overview")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json() == expected
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_health_overview_recomputes_after_ttl(
    client: AsyncClient, db_session, mocker
):
    """After TTL elapses, the cache re-fetches."""
    short_cache = DigestCache(default_ttl=0.05)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    spy = AsyncMock(
        side_effect=[{"overall_status": "first"}, {"overall_status": "second"}]
    )
    mocker.patch.object(_admin_health, "_compute_health_overview", spy)

    try:
        r1 = await client.get("/api/admin/health-overview")
        await asyncio.sleep(0.1)
        r2 = await client.get("/api/admin/health-overview")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.json() == {"overall_status": "first"}
    assert r2.json() == {"overall_status": "second"}
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_health_overview_cache_key_includes_query_params(
    client: AsyncClient, db_session, mocker
):
    """Different lookback_hours → different cache slot, both are stored."""
    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    spy = AsyncMock(side_effect=[{"hours": 24}, {"hours": 48}, {"hours": 24}])
    mocker.patch.object(_admin_health, "_compute_health_overview", spy)

    try:
        r1 = await client.get("/api/admin/health-overview?lookback_hours=24")
        r2 = await client.get("/api/admin/health-overview?lookback_hours=48")
        r3 = await client.get(
            "/api/admin/health-overview?lookback_hours=24"
        )  # same key as r1
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.json() == {"hours": 24}
    assert r2.json() == {"hours": 48}
    assert r3.json() == {"hours": 24}
    # 24h fetched once (r1, r3 share), 48h fetched once → 2 compute calls total.
    assert spy.call_count == 2


# ---------------------------------------------------------------------------
# /api/admin/max-plan-accounts/health cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_accounts_health_caches_repeated_calls_within_ttl(
    client: AsyncClient, db_session, mocker
):
    """Two polls within TTL → underlying compute runs once."""
    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    expected: Dict[str, Any] = {"accounts": []}
    spy = AsyncMock(return_value=expected)
    mocker.patch.object(_admin_pool_accounts, "_compute_account_health_summary", spy)

    try:
        r1 = await client.get("/api/admin/max-plan-accounts/health")
        r2 = await client.get("/api/admin/max-plan-accounts/health")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json() == expected
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_pool_accounts_health_dedups_concurrent_polls(
    client: AsyncClient, db_session, mocker
):
    """N concurrent polls → underlying compute runs once (in-flight dedup)."""
    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    release = asyncio.Event()
    call_count = 0

    async def slow_compute(*, user_id: str, session) -> Dict[str, Any]:
        nonlocal call_count
        call_count += 1
        await release.wait()
        return {"accounts": []}

    mocker.patch.object(
        _admin_pool_accounts, "_compute_account_health_summary", slow_compute
    )

    try:
        # Fire N concurrent requests; they should all share the same in-flight
        # future and thus call the underlying compute exactly once.
        N = 6
        request_tasks = [
            asyncio.create_task(client.get("/api/admin/max-plan-accounts/health"))
            for _ in range(N)
        ]
        # Yield once so all requests reach get_or_compute and register as
        # awaiters before we release the slow compute.
        await asyncio.sleep(0.05)
        release.set()
        responses = await asyncio.gather(*request_tasks)
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"accounts": []} for r in responses)
    assert call_count == 1
