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
    # Both polls return the same payload (dedup) and the underlying compute ran
    # once. The endpoint additively stamps freshness provenance, so assert the
    # core payload is a subset rather than exact-equal.
    body1, body2 = r1.json(), r2.json()
    assert body1 == body2
    for key, value in expected.items():
        assert body1[key] == value
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_health_overview_stuck_tasks_no_missing_greenlet(client, db_session):
    """Regression: the REAL _compute_health_overview must not raise MissingGreenlet
    on active tasks whose stuck-task diagnostics read columns the active-tasks scan
    used to defer via load_only.

    Prod 2026-06-03: `/api/admin/health-overview` 500'd (MissingGreenlet) because
    the active-tasks query load_only()'d a column subset, but _build_stuck_tasks
    (and the reaper's _heartbeat_stale_cutoff it calls) read vm_provisioned_at,
    context, payloads, and status_log — the first deferred access lazy-loaded via
    sync IO under async. The heavy-column branches run for PROVISIONING /
    IN_PROGRESS / EVALUATING tasks, so seed one of each. Not mocked — the whole
    point is to exercise the real query + attribute reads.
    """
    from datetime import timedelta

    from app.models import Project, Task, TaskStatus, _utcnow

    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    proj = Project(name="Health OV Stuck", user_id="health-ov-user")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)

    now = _utcnow()
    db_session.add_all(
        [
            # provision_timeout branch → reads vm_provisioned_at + context
            Task(
                project_id=proj.id,
                status=TaskStatus.PROVISIONING,
                vm_provisioned_at=now - timedelta(hours=2),
                last_heartbeat=None,
                payloads=[{"type": "research"}],
                context={"provision_zone": "us-central1-a"},
            ),
            # no_heartbeat branch → _heartbeat_stale_cutoff reads context + payloads
            Task(
                project_id=proj.id,
                status=TaskStatus.IN_PROGRESS,
                last_heartbeat=now - timedelta(hours=2),
                payloads=[{"type": "research", "scope": "small"}],
                context={},
            ),
            # evaluation_timeout branch → reads context + status_log
            Task(
                project_id=proj.id,
                status=TaskStatus.EVALUATING,
                started_at=now - timedelta(hours=6),
                payloads=[{"type": "research"}],
                context={},
            ),
        ]
    )
    await db_session.commit()
    # Drop the just-created instances from the identity map so the request's
    # active-tasks query reloads them fresh — otherwise the in-memory objects
    # already have every column populated and the deferred-load path is skipped.
    db_session.expire_all()

    try:
        resp = await client.get("/api/admin/health-overview")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert resp.status_code == 200, (
        f"health-overview must not 500 on stuck active tasks (MissingGreenlet "
        f"from deferred columns); got {resp.status_code}: {resp.text}"
    )
    # The PROVISIONING task is well past the provision timeout → it should surface
    # as an actionable issue, proving the heavy-column branch actually ran.
    issues = resp.json().get("actionable_issues", [])
    assert any(
        i.get("category") == "stuck_tasks" for i in issues
    ), f"expected a stuck-task issue to be reported; got {issues}"


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

    assert r1.json()["overall_status"] == "first"
    assert r2.json()["overall_status"] == "second"
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

    assert r1.json()["hours"] == 24
    assert r2.json()["hours"] == 48
    assert r3.json()["hours"] == 24
    # 24h fetched once (r1, r3 share), 48h fetched once → 2 compute calls total.
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_health_overview_on_demand_stamps_live_freshness(
    client: AsyncClient, db_session, mocker
):
    """The on-demand compute path stamps a truthful, ~live freshness signal:
    served_stale False and a small precompute_age_seconds derived from the
    payload's generated_at — so the card renders it as a live verdict."""
    from app.time_utils import _utcnow

    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    payload: Dict[str, Any] = {
        "overall_status": "healthy",
        "success_rate": {"current": 1.0, "trend": None, "window_hours": 24},
        "actionable_issues": [],
        "direction_portfolio": {},
        "queue_depth": {"queued": 0, "active": 0, "evaluating": 0},
        "generated_at": _utcnow().isoformat(),
    }
    spy = AsyncMock(return_value=payload)
    mocker.patch.object(_admin_health, "_compute_health_overview", spy)

    try:
        resp = await client.get("/api/admin/health-overview")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "healthy"
    assert body["served_stale"] is False
    assert body["precomputed"] is False
    assert body["precompute_age_seconds"] is not None
    assert body["precompute_age_seconds"] < 60


@pytest.mark.asyncio
async def test_health_overview_serve_stale_marks_verdict_not_live(
    client: AsyncClient, db_session, mocker
):
    """The bug this task fixes: when the live compute fails and the cache serves
    the last good payload, the response must carry served_stale=True AND a large
    age, so the (potentially hours-old) green verdict is never shown as live."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    short_cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    old_iso = (_utcnow() - timedelta(hours=2)).isoformat()
    good: Dict[str, Any] = {
        "overall_status": "healthy",
        "success_rate": {"current": 1.0, "trend": None, "window_hours": 24},
        "actionable_issues": [],
        "direction_portfolio": {},
        "queue_depth": {"queued": 0, "active": 0, "evaluating": 0},
        "generated_at": old_iso,
    }
    spy = AsyncMock(side_effect=[good, RuntimeError("statement timeout")])
    mocker.patch.object(_admin_health, "_compute_health_overview", spy)

    try:
        r1 = await client.get("/api/admin/health-overview")
        await asyncio.sleep(0.1)  # expire the fresh entry → next poll recomputes
        r2 = await client.get("/api/admin/health-overview")  # compute fails → stale
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.status_code == 200
    assert r1.json()["served_stale"] is False

    assert r2.status_code == 200
    b2 = r2.json()
    # The old green verdict is still served (no 500) ...
    assert b2["overall_status"] == "healthy"
    # ... but it is explicitly marked not-live, by both signals the card reads.
    assert b2["served_stale"] is True
    assert b2["precompute_age_seconds"] > 120
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
