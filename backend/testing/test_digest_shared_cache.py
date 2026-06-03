"""Tests for the cross-instance shared (L2) digest/health cache.

The leader-only in-process precompute (L1) helps only the leader; the shared
``digestcache`` table (L2) lets every Cloud Run instance serve the leader's
precomputed payload with a cheap primary-key read instead of re-running the
heavy scan (the dominant source of the 2026-06-03 statement_timeout 500s).

Covers:
  * ``shared_cache_key`` encoding (pure unit).
  * ``write_shared`` / ``read_shared`` round-trip + staleness + fail-open.
  * ``cleanup_stale_shared`` pruning.
  * Endpoint NON-LEADER simulation (the key regression): an empty L1 with a
    leader-written L2 row serves the precomputed payload WITHOUT recomputing —
    for both ``/api/admin/digest`` and ``/api/admin/health-overview``.
"""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select
from unittest.mock import AsyncMock

from app.digest_precompute import (
    DIGEST_KIND,
    HEALTH_KIND,
    cleanup_stale_shared,
    get_health_precompute_cache,
    get_precompute_cache,
    read_shared,
    shared_cache_key,
    write_shared,
)
from app.models import DigestCacheEntry
from app.routers import admin_digest as _admin_digest
from app.routers import admin_health as _admin_health
from app.time_utils import _utcnow


# ---------------------------------------------------------------------------
# shared_cache_key — pure unit
# ---------------------------------------------------------------------------


def test_shared_cache_key_encoding():
    import uuid

    pid = uuid.uuid4()
    assert shared_cache_key(DIGEST_KIND, 24, None) == "digest:24:global"
    assert shared_cache_key(HEALTH_KIND, 6, None) == "health:6:global"
    assert shared_cache_key(DIGEST_KIND, 24, pid) == f"digest:24:{pid}"
    # Kind + window + project all participate, so distinct views never collide.
    assert shared_cache_key(DIGEST_KIND, 24, pid) != shared_cache_key(
        HEALTH_KIND, 24, pid
    )
    assert shared_cache_key(DIGEST_KIND, 24, pid) != shared_cache_key(
        DIGEST_KIND, 6, pid
    )


# ---------------------------------------------------------------------------
# write_shared / read_shared — DB-backed round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_then_read_round_trip(db_session):
    key = shared_cache_key(DIGEST_KIND, 24, None)
    payload = {"generated_at": "2026-06-03T00:00:00", "completed_count": 5}

    ok = await write_shared(key, payload, computed_at=_utcnow(), session=db_session)
    assert ok is True

    entry = await read_shared(key, max_age_seconds=300.0, session=db_session)
    assert entry is not None
    assert entry.payload == payload
    assert entry.computed_at is not None


@pytest.mark.asyncio
async def test_write_shared_upserts_in_place(db_session):
    """A second write for the same key updates the row, not inserts a duplicate."""
    key = shared_cache_key(DIGEST_KIND, 24, None)
    await write_shared(key, {"v": 1}, computed_at=_utcnow(), session=db_session)
    await write_shared(key, {"v": 2}, computed_at=_utcnow(), session=db_session)

    rows = (
        (
            await db_session.execute(
                select(DigestCacheEntry).where(DigestCacheEntry.cache_key == key)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload == {"v": 2}


@pytest.mark.asyncio
async def test_read_shared_treats_stale_entry_as_miss(db_session):
    key = shared_cache_key(DIGEST_KIND, 24, None)
    # Written 400s ago — beyond a 300s freshness window.
    await write_shared(
        key, {"v": 1}, computed_at=_utcnow() - timedelta(seconds=400), session=db_session
    )
    assert await read_shared(key, max_age_seconds=300.0, session=db_session) is None
    # A more generous window still serves it.
    assert (
        await read_shared(key, max_age_seconds=600.0, session=db_session) is not None
    )


@pytest.mark.asyncio
async def test_read_shared_missing_key_returns_none(db_session):
    assert (
        await read_shared("digest:24:nope", max_age_seconds=300.0, session=db_session)
        is None
    )


@pytest.mark.asyncio
async def test_read_shared_ignores_non_worker_rows(db_session):
    """Only SOURCE_WORKER rows are served — the same invariant L1 enforces via
    worker_only. A (hypothetical) non-worker row is treated as a miss even when
    fresh, so a never-refreshed entry can't be served for the long window."""
    key = shared_cache_key(DIGEST_KIND, 24, None)
    db_session.add(
        DigestCacheEntry(
            cache_key=key, payload={"v": 1}, computed_at=_utcnow(), source="manual"
        )
    )
    await db_session.commit()
    assert await read_shared(key, max_age_seconds=300.0, session=db_session) is None


@pytest.mark.asyncio
async def test_read_and_write_shared_fail_open_on_bad_session():
    """A broken session never raises out of the cache helpers — they degrade to
    a miss / no-op so the endpoint and the tick keep working."""
    assert await read_shared("k", max_age_seconds=300.0, session=object()) is None
    assert await write_shared("k", {"v": 1}, computed_at=_utcnow(), session=object()) is False


@pytest.mark.asyncio
async def test_cleanup_stale_shared_prunes_only_old_rows(db_session):
    fresh = shared_cache_key(DIGEST_KIND, 24, None)
    old = shared_cache_key(HEALTH_KIND, 24, None)
    await write_shared(fresh, {"v": 1}, computed_at=_utcnow(), session=db_session)
    await write_shared(
        old, {"v": 1}, computed_at=_utcnow() - timedelta(seconds=1000), session=db_session
    )

    deleted = await cleanup_stale_shared(db_session, older_than_seconds=600.0)
    assert deleted == 1

    remaining = (
        (await db_session.execute(select(DigestCacheEntry.cache_key))).scalars().all()
    )
    assert fresh in remaining
    assert old not in remaining


# ---------------------------------------------------------------------------
# Endpoint NON-LEADER simulation — the key regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_endpoint_serves_from_shared_cache_without_recompute(
    client: AsyncClient, db_session, monkeypatch
):
    """Non-leader instance simulation: L1 (in-process precompute) is empty, but a
    leader has written an L2 row. The endpoint must serve the shared payload and
    NOT run the heavy _compute_digest scan — the whole point of the fix."""
    # Empty L1 → forces the L2 path (this is the non-leader / cold-cache state).
    get_precompute_cache().clear()

    await write_shared(
        shared_cache_key(DIGEST_KIND, 24, None),
        {"generated_at": "2026-06-03T00:00:00", "completed_count": 42},
        computed_at=_utcnow(),
        session=db_session,
    )

    spy = AsyncMock(side_effect=AssertionError("compute must not run on an L2 hit"))
    monkeypatch.setattr(_admin_digest, "_compute_digest", spy)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 42
    assert data["precomputed"] is True
    assert data["precompute_age_seconds"] is not None
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_health_overview_endpoint_serves_from_shared_cache_without_recompute(
    client: AsyncClient, db_session, monkeypatch
):
    """Same non-leader simulation for health-overview, which previously had NO
    precompute at all (every request live-computed)."""
    get_health_precompute_cache().clear()

    await write_shared(
        shared_cache_key(HEALTH_KIND, 24, None),
        {"generated_at": "2026-06-03T00:00:00", "top_issues": [], "marker": "from-l2"},
        computed_at=_utcnow(),
        session=db_session,
    )

    spy = AsyncMock(
        side_effect=AssertionError("health compute must not run on an L2 hit")
    )
    monkeypatch.setattr(_admin_health, "_compute_health_overview", spy)

    response = await client.get("/api/admin/health-overview")
    assert response.status_code == 200
    data = response.json()
    assert data["marker"] == "from-l2"
    assert data["precomputed"] is True
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_digest_endpoint_falls_through_when_shared_stale(
    client: AsyncClient, db_session, monkeypatch
):
    """A stale L2 row (older than the freshness window) is NOT served — the
    endpoint falls through to the bounded on-demand compute, preserving the
    'degrade rather than serve arbitrarily old data' guarantee."""
    get_precompute_cache().clear()

    await write_shared(
        shared_cache_key(DIGEST_KIND, 24, None),
        {"generated_at": "old", "completed_count": 999},
        computed_at=_utcnow() - timedelta(seconds=1000),
        session=db_session,
    )

    ran = AsyncMock(return_value={"generated_at": "now", "completed_count": 0})
    monkeypatch.setattr(_admin_digest, "_compute_digest", ran)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    # Stale L2 ignored → fell through to the on-demand compute.
    assert data["precomputed"] is False
    assert data["completed_count"] == 0
    ran.assert_awaited()
