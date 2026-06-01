"""Tests for the background digest-precompute path (Design-audit #1).

Covers:
  * ``DigestPrecomputeCache`` freshness / staleness logic (pure unit).
  * ``run_precompute_tick`` priming the cache via a session factory and
    surviving a per-key compute failure.
  * The ``/api/admin/digest`` endpoint serving the precomputed payload
    (annotated with precompute metadata) when fresh, WITHOUT re-running the
    heavy ``_compute_digest`` 5000-row scan.
  * The bounded fallback: an empty/stale precompute cache falls back to the
    on-demand compute (the prior behavior) and opportunistically primes the
    cache.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.digest_precompute import (
    DigestPrecomputeCache,
    annotate_precompute_metadata,
    get_precompute_cache,
    run_precompute_tick,
)
from app.routers import admin_digest as _admin_digest
from testing.factories import make_project, make_task
from app.models import TaskStatus


# ---------------------------------------------------------------------------
# DigestPrecomputeCache — pure unit
# ---------------------------------------------------------------------------


def test_precompute_cache_make_key_normalizes_project_id():
    cache = DigestPrecomputeCache()
    import uuid

    pid = uuid.uuid4()
    assert cache.make_key(24, None) == (24, None)
    assert cache.make_key(24, pid) == (24, str(pid))


def test_precompute_cache_get_fresh_returns_stored_entry():
    cache = DigestPrecomputeCache()
    cache.put(24, None, {"x": 1}, computed_wall_iso="2026-05-31T00:00:00")
    entry = cache.get_fresh(24, None, max_age_seconds=300.0)
    assert entry is not None
    assert entry.result == {"x": 1}
    assert entry.computed_wall_iso == "2026-05-31T00:00:00"


def test_precompute_cache_get_fresh_missing_key_returns_none():
    cache = DigestPrecomputeCache()
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is None
    # A primed key is isolated from a different (hours) key.
    cache.put(24, None, {"x": 1}, computed_wall_iso="t")
    assert cache.get_fresh(48, None, max_age_seconds=300.0) is None


def test_precompute_cache_get_fresh_respects_staleness(monkeypatch):
    cache = DigestPrecomputeCache()
    import app.digest_precompute as dp

    base = 1000.0
    monkeypatch.setattr(dp.time, "monotonic", lambda: base)
    cache.put(24, None, {"x": 1}, computed_wall_iso="t")
    # 10s later, with a 300s budget → still fresh.
    monkeypatch.setattr(dp.time, "monotonic", lambda: base + 10.0)
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    # 301s later → stale, treated as a miss.
    monkeypatch.setattr(dp.time, "monotonic", lambda: base + 301.0)
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is None


def test_precompute_cache_worker_only_filters_non_worker_entries():
    """worker_only=True treats a non-worker (never-refreshed) entry as a miss,
    even when it is within the freshness window — the freshness-regression
    guard for non-leader instances."""
    from app.digest_precompute import SOURCE_WORKER

    cache = DigestPrecomputeCache()

    # A non-worker entry is a miss under worker_only, but a hit otherwise.
    cache.put(24, None, {"x": 1}, computed_wall_iso="t", source="opportunistic")
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, None, max_age_seconds=300.0, worker_only=True) is None

    # A worker entry is a hit under both.
    cache.put(24, None, {"x": 2}, computed_wall_iso="t", source=SOURCE_WORKER)
    assert cache.get_fresh(24, None, max_age_seconds=300.0, worker_only=True) is not None

    # put defaults to the worker source.
    cache.put(48, None, {"y": 3}, computed_wall_iso="t")
    entry = cache.get_fresh(48, None, max_age_seconds=300.0, worker_only=True)
    assert entry is not None
    assert entry.source == SOURCE_WORKER


def test_annotate_precompute_metadata_does_not_mutate_source():
    src = {"completed_count": 3, "generated_at": "2026-05-31T00:00:00"}
    out = annotate_precompute_metadata(
        src,
        precomputed=True,
        precomputed_at="2026-05-31T00:00:00",
        precompute_age_seconds=12.34,
    )
    assert out is not src
    assert "precomputed" not in src  # source untouched
    assert out["precomputed"] is True
    assert out["precomputed_at"] == "2026-05-31T00:00:00"
    assert out["precompute_age_seconds"] == 12.3  # rounded to 1dp
    assert out["completed_count"] == 3  # original payload preserved


# ---------------------------------------------------------------------------
# run_precompute_tick — orchestration with a stub session factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_precompute_tick_primes_global_and_projects(monkeypatch):
    """A tick computes the global key plus each active project key and stores
    them in the module-level precompute cache."""
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()

    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()

    # Stub the heavy compute so the test is DB-free and fast; the digest body
    # is exercised by the integration tests below.
    async def _fake_compute(*, hours, project_id, session):
        return {
            "generated_at": "2026-05-31T00:00:00",
            "completed_count": 0,
            "_pid": str(project_id) if project_id else None,
        }

    monkeypatch.setattr(_admin_digest, "_compute_digest", _fake_compute)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=[proj_a, proj_b]))

    @asynccontextmanager
    async def _session_factory():
        yield object()  # _compute_digest is stubbed; session is unused

    refreshed = await run_precompute_tick(_session_factory)

    assert refreshed == 3  # global + 2 projects
    cache = get_precompute_cache()
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, proj_a, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, proj_b, max_age_seconds=300.0) is not None


@pytest.mark.asyncio
async def test_run_precompute_tick_survives_per_key_failure(monkeypatch):
    """One key's compute failure does not stop the tick from priming others."""
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()

    bad = uuid.uuid4()
    good = uuid.uuid4()

    async def _flaky_compute(*, hours, project_id, session):
        if project_id == bad:
            raise RuntimeError("boom")
        return {"generated_at": "t", "completed_count": 0}

    monkeypatch.setattr(_admin_digest, "_compute_digest", _flaky_compute)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=[bad, good]))

    @asynccontextmanager
    async def _session_factory():
        yield object()

    refreshed = await run_precompute_tick(_session_factory)

    cache = get_precompute_cache()
    # global + good succeeded; bad failed.
    assert refreshed == 2
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, good, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, bad, max_age_seconds=300.0) is None


@pytest.mark.asyncio
async def test_active_project_ids_excludes_terminal_projects(db_session):
    """_active_project_ids primes only ACTIVE projects — terminal
    (COMPLETED / FAILED / CANCELLED) projects are excluded so the bounded
    per-tick slots aren't spent on idle/archived projects."""
    from app.digest_precompute import _active_project_ids
    from app.models import ProjectStatus

    active = make_project(name="Active", status=ProjectStatus.DEPLOYING)
    draft = make_project(name="Draft", status=ProjectStatus.DRAFT)
    completed = make_project(name="Done", status=ProjectStatus.COMPLETED)
    failed = make_project(name="Failed", status=ProjectStatus.FAILED)
    cancelled = make_project(name="Cancelled", status=ProjectStatus.CANCELLED)
    db_session.add_all([active, draft, completed, failed, cancelled])
    await db_session.commit()

    ids = set(await _active_project_ids(db_session))

    assert active.id in ids
    assert draft.id in ids
    assert completed.id not in ids
    assert failed.id not in ids
    assert cancelled.id not in ids


# ---------------------------------------------------------------------------
# /api/admin/digest endpoint — precompute read path + bounded fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_endpoint_serves_precomputed_without_recompute(
    client: AsyncClient, monkeypatch
):
    """When the precompute cache is fresh for the requested key, the endpoint
    returns the cached payload (annotated) and does NOT invoke _compute_digest
    (the 5000-row scan)."""
    get_precompute_cache().clear()
    get_precompute_cache().put(
        24,
        None,
        {"generated_at": "2026-05-31T00:00:00", "completed_count": 7},
        computed_wall_iso="2026-05-31T00:00:00",
    )

    spy = AsyncMock(side_effect=AssertionError("compute must not run on a cache hit"))
    monkeypatch.setattr(_admin_digest, "_compute_digest", spy)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 7
    assert data["precomputed"] is True
    assert data["precomputed_at"] == "2026-05-31T00:00:00"
    assert data["precompute_age_seconds"] is not None
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_digest_endpoint_falls_back_when_cache_empty_without_priming(
    client: AsyncClient, db_session
):
    """An empty precompute cache falls back to the real on-demand compute
    (the prior behavior) and marks the response as not precomputed.

    Freshness invariant: the fallback path must NOT prime the precompute cache.
    Priming a non-worker, never-refreshed entry would let a subsequent poll on
    a non-leader instance serve it as fresh-for-PRECOMPUTE_STALE_FALLBACK_SECONDS
    (300s), regressing freshness from the 15s DigestCache TTL to 5 minutes.
    The DigestCache (15s TTL + in-flight dedup) is the only short-window cache
    on this path."""
    get_precompute_cache().clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Completed Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 1
    assert data["precomputed"] is False  # came from the bounded fallback compute

    # The fallback path leaves the precompute cache empty — it does NOT prime
    # a non-worker entry that could later be served as fresh-for-300s.
    assert (
        get_precompute_cache().get_fresh(24, None, max_age_seconds=300.0) is None
    )


@pytest.mark.asyncio
async def test_non_leader_non_worker_entry_not_served_stale(
    client: AsyncClient, db_session
):
    """Reviewer-flagged freshness regression guard (multi-instance / non-leader).

    On a non-leader instance the background worker never runs, so the only way
    the precompute cache can hold an entry for a key is a non-worker write
    (e.g. a leftover opportunistic prime). Such an entry is NEVER refreshed, so
    serving it as fresh-for-PRECOMPUTE_STALE_FALLBACK_SECONDS (300s) would
    regress freshness from the prior 15s DigestCache TTL to 5 minutes.

    Simulate that exact scenario: seed a stale (>15s old) NON-worker entry with
    obviously-wrong data, then read the endpoint. The read must IGNORE that
    non-worker entry (worker_only=True) and recompute from the DB, so the
    served payload reflects current state — NOT the 5-minute-stale non-worker
    value."""
    import time as _time

    import app.digest_precompute as dp

    get_precompute_cache().clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Completed Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()

    # Seed a NON-worker entry (source != worker), aged well past the 15s TTL
    # but still within the 300s fallback window — the exact band a regressed
    # non-leader read would serve stale.
    cache = get_precompute_cache()
    cache.put(
        24,
        None,
        {"generated_at": "stale-non-worker", "completed_count": 999},
        computed_wall_iso="stale-non-worker",
        source="opportunistic",
    )
    # Age the entry to 60s (> 15s DigestCache TTL, < 300s fallback window).
    entry = cache._entries[cache.make_key(24, None)]  # test introspection
    entry.computed_monotonic = _time.monotonic() - 60.0

    # Sanity: a worker_only read sees the non-worker entry as a MISS even though
    # it is well within the 300s window.
    assert (
        cache.get_fresh(
            24,
            None,
            max_age_seconds=dp.PRECOMPUTE_STALE_FALLBACK_SECONDS,
            worker_only=True,
        )
        is None
    )

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    # Recomputed from current DB state (1 completed task), NOT the stale 999.
    assert data["completed_count"] == 1
    assert data["precomputed"] is False


@pytest.mark.asyncio
async def test_digest_endpoint_stale_precompute_triggers_recompute(
    client: AsyncClient, db_session, monkeypatch
):
    """A precompute entry older than the fallback window is treated as a miss,
    so the endpoint recomputes rather than serving arbitrarily-old data."""
    import app.digest_precompute as dp

    get_precompute_cache().clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a stale entry at t0, then advance monotonic time past the window.
    base = 5000.0
    monkeypatch.setattr(dp.time, "monotonic", lambda: base)
    get_precompute_cache().put(
        24,
        None,
        {"generated_at": "old", "completed_count": 999},
        computed_wall_iso="old",
    )
    monkeypatch.setattr(
        dp.time, "monotonic", lambda: base + dp.PRECOMPUTE_STALE_FALLBACK_SECONDS + 1.0
    )

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    # Recomputed from the (empty) DB window, not the stale 999.
    assert data["completed_count"] == 0
    assert data["precomputed"] is False
