"""Cache behavior tests for admin polling endpoints (Phase E2 / M-P4).

Verifies the TTL+in-flight-dedup pattern (replicated from admin_digest.py)
applied to:
  - /api/admin/health-overview
  - /api/admin/max-plan-accounts/health
  - /api/admin/projects/{id}/version-lifecycle
  - /api/admin/projects/{id}/agent-serving-shas

The compounded admin-dashboard polling load (DashboardHome polls every
30s, multiple admins polling in parallel) makes deduplication a
correctness-adjacent invariant: without it, repeated multi-thousand-row
JSONB scans run in parallel and have caused OOM/instance kill before
(see digest_cache.py docstring).
"""

import asyncio
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.digest_cache import DigestCache
from app.main import app
from app.routers import admin_health as _admin_health
from app.routers import admin_pool_accounts as _admin_pool_accounts
from app.routers import admin_release_version as _admin_release_version
from app.routers.admin_health import get_health_overview  # noqa: F401  (route registered)
from app.routers.admin_pool_accounts import get_account_health_summary  # noqa: F401
from app.digest_cache import get_digest_cache
from app.shared_state import runtime_config as _runtime_config

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


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
async def test_warm_vm_state_exposes_per_project_pool_guard_and_caches(
    client: AsyncClient, db_session
):
    """The live endpoint exposes per-project pool-guard state alongside the
    legacy fleet-wide fields, and a second poll within the TTL returns the same
    cached payload."""
    from datetime import timedelta

    from app.models import PoolVm, PoolVmState, Project, Task, TaskStatus, _utcnow
    from app.warm_backlog_reuse import (
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        record_pool_lease_miss,
    )

    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    bad = Project(name="Health Guard Bad", user_id="health-guard-user")
    good = Project(name="Health Guard Good", user_id="health-guard-user")
    db_session.add_all([bad, good])
    await db_session.commit()
    await db_session.refresh(bad)
    await db_session.refresh(good)

    db_session.add_all(
        [
            PoolVm(
                vm_instance_id="vm-guard-bad",
                project_id=bad.id,
                state=PoolVmState.IDLE,
                idle_since=_utcnow() - timedelta(hours=2),
            ),
            PoolVm(
                vm_instance_id="vm-guard-good",
                project_id=good.id,
                state=PoolVmState.IDLE,
                idle_since=_utcnow() - timedelta(minutes=5),
            ),
        ]
    )
    # The per-project guard derives its hit rate from dispatched Task rows:
    # two recent cold dispatches (no warm_claim_origin in context._timing) for
    # the bad project → hit rate 0.0 below the floor → guard engages. The good
    # project has no dispatches in the window → hit rate None → disengaged.
    db_session.add_all(
        [
            Task(
                project_id=bad.id,
                payloads=[{"type": "create", "prompt": "guard cold dispatch"}],
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_dispatched_at=_utcnow() - timedelta(minutes=10),
            ),
            Task(
                project_id=bad.id,
                payloads=[{"type": "create", "prompt": "guard cold dispatch"}],
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_dispatched_at=_utcnow() - timedelta(minutes=5),
            ),
        ]
    )
    await db_session.commit()
    _pool_lease_hit_events.clear()
    _pool_lease_miss_events.clear()
    record_pool_lease_miss("no_idle_member", project_id=bad.id)
    record_pool_lease_miss("no_idle_member", project_id=bad.id)

    old_guard_config = {
        key: _runtime_config.get(key)
        for key in (
            "warm_pool_cost_guard_min_dispatches",
            "warm_pool_cost_guard_min_hit_rate",
            "warm_pool_cost_guard_min_idle_cost_usd",
        )
    }
    _runtime_config.update(
        {
            "warm_pool_cost_guard_min_dispatches": 2,
            "warm_pool_cost_guard_min_hit_rate": 0.75,
            "warm_pool_cost_guard_min_idle_cost_usd": 0.0,
        }
    )

    try:
        r1 = await client.get("/api/admin/warm-vm-state")
        r2 = await client.get("/api/admin/warm-vm-state")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)
        _runtime_config.update(old_guard_config)

    assert r1.status_code == 200
    assert r2.status_code == 200
    body1 = r1.json()
    body2 = r2.json()
    assert body1["active_vms"] == body2["active_vms"]
    assert body1["warm_pin_metrics"] == body2["warm_pin_metrics"]
    assert body1["generated_at"] == body2["generated_at"]
    assert body1["served_stale"] is False
    assert body2["served_stale"] is False
    assert body1["precomputed"] is False
    assert body2["precomputed"] is False
    assert body1["precompute_age_seconds"] is not None
    assert body1["precompute_age_seconds"] < 60
    pool = body1["pool_metrics"]
    assert "cost_guard" in pool
    assert "cost_guard_by_project" in pool
    guard_by_project = pool["cost_guard_by_project"]
    assert guard_by_project[str(bad.id)]["engaged"] is True
    assert guard_by_project[str(good.id)]["engaged"] is False


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on deferred-column lazy-load is asyncpg-specific; "
        "aiosqlite loads all columns eagerly regardless of load_only(), so the "
        "regression is invisible on SQLite. No proxy-based fault injection is "
        "practical because the deferred-column access occurs deep inside "
        "SQLAlchemy's attribute machinery, not at an application-level hook point."
    ),
)
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
        # Zone-carrying, matching what utc_iso() actually puts on the wire.
        # A bare .isoformat() here would feed the annotate path a format the
        # real endpoint no longer produces -- which is exactly why this test
        # stayed green while /api/admin/health-overview was returning a null
        # precompute_age_seconds in production.
        "generated_at": _utcnow().isoformat() + "+00:00",
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


@pytest.mark.asyncio
async def test_warm_vm_state_serve_stale_is_annotated(
    client: AsyncClient, db_session, mocker
):
    """warm-vm-state must expose the DigestCache stale-fallback signal."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    short_cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    old_iso = (_utcnow() - timedelta(hours=2)).isoformat()
    good: Dict[str, Any] = {
        "active_vms": [],
        "warm_pin_metrics": {"active_warm_chains": 0},
        "generated_at": old_iso,
    }
    spy = AsyncMock(side_effect=[good, RuntimeError("warm-vm scan timed out")])
    mocker.patch.object(_admin_health, "_compute_warm_vm_state", spy)

    try:
        r1 = await client.get("/api/admin/warm-vm-state")
        await asyncio.sleep(0.1)
        r2 = await client.get("/api/admin/warm-vm-state")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.status_code == 200
    assert r1.json()["served_stale"] is False

    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["active_vms"] == []
    assert b2["served_stale"] is True
    assert b2["precomputed"] is False
    assert b2["precomputed_at"] == old_iso
    assert b2["precompute_age_seconds"] > 120
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_fleet_utilization_on_demand_stamps_live_freshness(
    client: AsyncClient, db_session, mocker
):
    """fleet-utilization fresh compute emits generated_at and live metadata."""
    fresh_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: fresh_cache

    spy = AsyncMock(return_value={"util_avg": 0.5, "util_now": 0.25})
    mocker.patch.object(_admin_health, "get_fleet_utilization_snapshot", spy)

    try:
        resp = await client.get("/api/admin/fleet-utilization")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["util_avg"] == 0.5
    assert body["util_now"] == 0.25
    assert body["saturation"] == "healthy"
    assert body["generated_at"]
    assert body["served_stale"] is False
    assert body["precomputed"] is False
    assert body["precomputed_at"] == body["generated_at"]
    assert body["precompute_age_seconds"] is not None
    assert body["precompute_age_seconds"] < 60
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_fleet_utilization_serve_stale_is_annotated(
    client: AsyncClient, db_session, mocker
):
    """fleet-utilization must expose the DigestCache stale-fallback signal."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    short_cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    old_iso = (_utcnow() - timedelta(hours=2)).isoformat()
    good: Dict[str, Any] = {
        "util_avg": 0.95,
        "util_now": 1.0,
        "saturation": "saturated",
        "generated_at": old_iso,
    }
    spy = AsyncMock(side_effect=[good, RuntimeError("fleet scan timed out")])
    mocker.patch.object(_admin_health, "_compute_fleet_utilization", spy)

    try:
        r1 = await client.get("/api/admin/fleet-utilization")
        await asyncio.sleep(0.1)
        r2 = await client.get("/api/admin/fleet-utilization")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert r1.status_code == 200
    assert r1.json()["served_stale"] is False

    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["saturation"] == "saturated"
    assert b2["served_stale"] is True
    assert b2["precomputed"] is False
    assert b2["precomputed_at"] == old_iso
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
    # The payload now carries additive freshness keys; the cached body is still
    # identical across the two polls and the underlying compute runs once.
    assert r1.json() == r2.json()
    assert r1.json()["accounts"] == expected["accounts"]
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
    assert all(r.json()["accounts"] == [] for r in responses)
    assert call_count == 1


# ---------------------------------------------------------------------------
# /api/admin/projects/{id}/version-lifecycle cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_lifecycle_caches_repeated_calls_within_ttl(
    client: AsyncClient, db_session, mocker
):
    """Two polls within TTL → expensive DB compute runs once; precomputed flag absent on miss."""
    from app.models import Project, ProjectMember

    # Create a project with no open version (simplest scenario).
    project = Project(name="lifecycle-cache-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    # Install a fresh cache so tests are isolated from module-level state.
    fresh_cache = DigestCache(default_ttl=60.0, stale_retry_ttl=10.0)
    mocker.patch.object(_admin_release_version, "_version_lifecycle_cache", fresh_cache)

    # Spy on compute_promotion_readiness — it runs on every un-cached call.
    spy = AsyncMock(return_value={"infra_failure_count": 0, "consecutive_infra_errors": 0})
    mocker.patch(
        "app.release_qualifier.compute_promotion_readiness",
        spy,
    )
    mocker.patch(
        "app.release_readiness.get_challenged_vs_unchallenged_telemetry",
        AsyncMock(return_value={}),
    )

    r1 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    r2 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both responses return the same payload (dedup via TTL cache).
    assert r1.json()["current_version"] == r2.json()["current_version"]
    # precomputed=False on first (fresh compute); precomputed=False again on
    # second because it came from the fresh TTL cache, not the stale fallback.
    assert r1.json()["precomputed"] is False
    assert r2.json()["precomputed"] is False
    # The underlying compute ran exactly once.
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_version_lifecycle_serves_stale_on_failure(
    client: AsyncClient, db_session, mocker
):
    """When compute fails after a cached result exists, the stale result is served
    and precomputed=True signals the client that data may be old."""
    from app.models import Project, ProjectMember

    project = Project(name="lifecycle-stale-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    # Short negative-cache TTL so failure-path can complete quickly.
    fresh_cache = DigestCache(default_ttl=60.0, stale_retry_ttl=5.0)
    mocker.patch.object(_admin_release_version, "_version_lifecycle_cache", fresh_cache)

    call_count = 0

    async def _good_readiness(*_a, **_kw):
        return {"infra_failure_count": 0, "consecutive_infra_errors": 0}

    async def _failing_readiness(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("DB timeout")

    mocker.patch(
        "app.release_readiness.get_challenged_vs_unchallenged_telemetry",
        AsyncMock(return_value={}),
    )

    # First call succeeds and populates the last-good cache.
    mocker.patch("app.release_qualifier.compute_promotion_readiness", _good_readiness)
    r1 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert r1.status_code == 200
    assert r1.json()["precomputed"] is False

    # Expire the TTL entries so the next call triggers a fresh compute.
    # Do NOT wipe _last_good — that is the stale fallback we want to exercise.
    fresh_cache._results.clear()

    # Second call: compute fails → stale fallback is served.
    mocker.patch("app.release_qualifier.compute_promotion_readiness", _failing_readiness)
    r2 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert r2.status_code == 200
    assert r2.json()["precomputed"] is True  # served from stale fallback

@pytest.mark.asyncio
async def test_version_lifecycle_access_check_runs_on_cache_hit(
    client: AsyncClient, db_session, mocker
):
    """Access check must run on every request — even when the cache is warm.

    A caller without project access must receive a 403/404, not the cached
    payload that was served to an authorized caller on the prior request.
    """
    from app.auth import get_current_user
    from app.main import app
    from app.models import Project, ProjectMember

    # Authorized project with test-user-123 as owner.
    project = Project(name="lifecycle-access-check-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()

    fresh_cache = DigestCache(default_ttl=60.0, stale_retry_ttl=10.0)
    mocker.patch.object(_admin_release_version, "_version_lifecycle_cache", fresh_cache)
    mocker.patch(
        "app.release_qualifier.compute_promotion_readiness",
        AsyncMock(return_value={"infra_failure_count": 0, "consecutive_infra_errors": 0}),
    )
    mocker.patch(
        "app.release_readiness.get_challenged_vs_unchallenged_telemetry",
        AsyncMock(return_value={}),
    )

    # First request: authorized user populates the cache.
    r1 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert r1.status_code == 200

    # Second request: unauthorized user — not a project member — should be rejected
    # even though the cache is now warm.
    async def _unauthorized_user():
        return {"user_id": "other-user-not-a-member", "email": "other@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user] = _unauthorized_user
    try:
        r2 = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert r2.status_code in (403, 404), (
        f"Expected 403/404 for unauthorized user on cached endpoint, got {r2.status_code}"
    )

# ---------------------------------------------------------------------------
# /api/admin/projects/{id}/agent-serving-shas cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_serving_shas_caches_github_branch_lookups(
    client: AsyncClient, db_session, mocker
):
    """Two calls within TTL → GitHub branch SHA calls run once each (cached)."""
    from app.models import Project, ProjectMember

    project = Project(
        name="sha-cache-test",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    # Install a fresh cache with a long TTL so both requests are cache hits.
    fresh_cache = DigestCache(default_ttl=300.0, stale_retry_ttl=60.0)
    mocker.patch.object(_admin_release_version, "_agent_serving_shas_cache", fresh_cache)

    github_token_mock = AsyncMock(return_value="token")
    branch_sha_mock = AsyncMock(side_effect=["int-sha-1", "promo-sha-1"])
    mocker.patch("app.compute.get_current_agent_image", return_value=None)

    with (
        patch("app.github.get_repo_installation_token", github_token_mock),
        patch("app.github_refs.get_branch_sha", branch_sha_mock),
    ):
        r1 = await client.get(f"/api/admin/projects/{project.id}/agent-serving-shas")
        r2 = await client.get(f"/api/admin/projects/{project.id}/agent-serving-shas")

    assert r1.status_code == 200
    assert r2.status_code == 200
    data1, data2 = r1.json(), r2.json()
    assert data1["integration_head_sha"] == "int-sha-1"
    assert data1["promotion_head_sha"] == "promo-sha-1"
    # Second call returns the same cached SHAs.
    assert data2["integration_head_sha"] == "int-sha-1"
    assert data2["promotion_head_sha"] == "promo-sha-1"
    # GitHub branch SHA lookups ran exactly once (cache hit on second call).
    assert branch_sha_mock.call_count == 2  # one per distinct branch, not per request
