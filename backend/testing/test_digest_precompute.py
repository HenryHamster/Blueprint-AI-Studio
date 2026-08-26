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
    get_health_precompute_cache,
    get_precompute_cache,
    payload_age_seconds,
    run_precompute_tick,
)
from app.routers import admin_digest as _admin_digest
from app.routers import admin_health as _admin_health
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
    assert (
        cache.get_fresh(24, None, max_age_seconds=300.0, worker_only=True) is not None
    )

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
# payload_age_seconds — the wire-stamp round-trip
# ---------------------------------------------------------------------------


def test_payload_age_seconds_parses_the_zone_carrying_wire_format():
    """Regression (#17890 fallout): a ``generated_at`` written by ``utc_iso``
    carries ``+00:00``, and the age math compares it against naive
    ``_utcnow()``. Subtracting aware from naive raises TypeError, which every
    call site swallowed via ``except (ValueError, TypeError)`` -- so the bug
    surfaced as a permanently ``None`` ``precompute_age_seconds`` on four
    admin endpoints rather than as an error.

    The offset is pinned as a literal here on purpose. Deriving the input from
    ``utc_iso()`` would make this test agree with whatever format the code
    happens to emit, which is exactly the self-certifying shape that cannot
    catch the next format change.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    stamped = (_utcnow() - timedelta(seconds=30)).isoformat() + "+00:00"
    age = payload_age_seconds(stamped)

    assert age is not None, "zone-carrying stamp must not fall through to None"
    assert 29.0 <= age <= 31.0, age


def test_payload_age_seconds_still_reads_legacy_zone_less_stamps():
    """Payloads cached before the zone-carrying format rolled out still hold a
    zone-less stamp. Those must keep reporting a real age across the rollout
    window rather than going None until the cache turns over."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    stamped = (_utcnow() - timedelta(seconds=30)).isoformat()
    assert "+00:00" not in stamped  # guard: this really is the legacy shape
    age = payload_age_seconds(stamped)

    assert age is not None
    assert 29.0 <= age <= 31.0, age


def test_payload_age_seconds_clamps_future_stamps_and_rejects_junk():
    """A clock-skewed future stamp clamps to 0 rather than going negative;
    absent/unparseable stamps are the only inputs that yield None."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    future = (_utcnow() + timedelta(hours=1)).isoformat() + "+00:00"
    assert payload_age_seconds(future) == 0.0

    assert payload_age_seconds(None) is None
    assert payload_age_seconds("") is None
    assert payload_age_seconds("not-a-timestamp") is None


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
    get_health_precompute_cache().clear()

    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()

    # Stub the heavy computes so the test is DB-free and fast; the digest /
    # health bodies are exercised by the integration tests below. The tick
    # primes BOTH kinds (digest first, then health) over the same hot key set.
    async def _fake_compute(*, hours, project_id, session, _statement_timeout_secs=30):
        return {
            "generated_at": "2026-05-31T00:00:00",
            "completed_count": 0,
            "_pid": str(project_id) if project_id else None,
        }

    async def _fake_health(*, project_id, lookback_hours, session):
        return {
            "generated_at": "2026-05-31T00:00:00",
            "_pid": str(project_id) if project_id else None,
        }

    monkeypatch.setattr(_admin_digest, "_compute_digest", _fake_compute)
    monkeypatch.setattr(_admin_health, "_compute_health_overview", _fake_health)
    monkeypatch.setattr(
        dp, "_active_project_ids", AsyncMock(return_value=[proj_a, proj_b])
    )

    @asynccontextmanager
    async def _session_factory():
        yield object()  # computes are stubbed; L2 writes fail-open on the stub

    result = await run_precompute_tick(_session_factory)

    # global + 2 projects, for EACH of digest and health = 6.
    assert result.refreshed == 6
    assert result.partial is False
    cache = get_precompute_cache()
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, proj_a, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, proj_b, max_age_seconds=300.0) is not None
    hcache = get_health_precompute_cache()
    assert hcache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert hcache.get_fresh(24, proj_a, max_age_seconds=300.0) is not None
    assert hcache.get_fresh(24, proj_b, max_age_seconds=300.0) is not None


@pytest.mark.asyncio
async def test_run_precompute_tick_survives_per_key_failure(monkeypatch):
    """One key's compute failure does not stop the tick from priming others."""
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()
    get_health_precompute_cache().clear()

    bad = uuid.uuid4()
    good = uuid.uuid4()

    async def _flaky_compute(*, hours, project_id, session, _statement_timeout_secs=30):
        if project_id == bad:
            raise RuntimeError("boom")
        return {"generated_at": "t", "completed_count": 0}

    async def _flaky_health(*, project_id, lookback_hours, session):
        if project_id == bad:
            raise RuntimeError("boom")
        return {"generated_at": "t"}

    monkeypatch.setattr(_admin_digest, "_compute_digest", _flaky_compute)
    monkeypatch.setattr(_admin_health, "_compute_health_overview", _flaky_health)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=[bad, good]))

    @asynccontextmanager
    async def _session_factory():
        yield object()

    result = await run_precompute_tick(_session_factory)

    cache = get_precompute_cache()
    hcache = get_health_precompute_cache()
    # For each kind: global + good succeeded; bad failed. 2 kinds x 2 = 4.
    assert result.refreshed == 4
    assert result.partial is False
    assert cache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, good, max_age_seconds=300.0) is not None
    assert cache.get_fresh(24, bad, max_age_seconds=300.0) is None
    assert hcache.get_fresh(24, None, max_age_seconds=300.0) is not None
    assert hcache.get_fresh(24, good, max_age_seconds=300.0) is not None
    assert hcache.get_fresh(24, bad, max_age_seconds=300.0) is None


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
    assert get_precompute_cache().get_fresh(24, None, max_age_seconds=300.0) is None


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


# ---------------------------------------------------------------------------
# DB-load reduction regression tests (task #6adb0c88)
# ---------------------------------------------------------------------------


def test_migration_0275_partial_index_ddl():
    """Migration 0275 registers the partial index used by the split OR query."""
    import pathlib

    path = (
        pathlib.Path(__file__).parent.parent
        / "alembic/versions/0275_task_requires_approval_partial_index.py"
    )
    text = path.read_text()
    assert "ix_task_requires_approval" in text
    assert "WHERE requires_approval IS TRUE" in text
    assert "project_id, created_at" in text


@pytest.mark.asyncio
async def test_background_precompute_passes_lower_statement_timeout(monkeypatch):
    """_precompute_one passes _statement_timeout_secs=5 (not 30) to _compute_digest."""
    import app.digest_precompute as dp
    from app.routers import admin_digest as _ad

    captured: list[int] = []

    async def _capturing_compute(
        *, hours, project_id, session, _statement_timeout_secs=30
    ):
        captured.append(_statement_timeout_secs)
        return {"generated_at": "2026-01-01T00:00:00", "completed_count": 0}

    async def _noop_write_shared(key, result, computed_at, session):
        pass

    monkeypatch.setattr(_ad, "_compute_digest", _capturing_compute)
    monkeypatch.setattr(dp, "write_shared", _noop_write_shared)

    from unittest.mock import MagicMock

    await dp._precompute_one(MagicMock(), hours=24, project_id=None)

    assert captured == [dp._BACKGROUND_STATEMENT_TIMEOUT_SECS]
    assert dp._BACKGROUND_STATEMENT_TIMEOUT_SECS == 5


@pytest.mark.asyncio
async def test_round_robin_slice_advances_offset(monkeypatch):
    """run_precompute_tick primes only _PROJECTS_PER_TICK_SLICE projects and advances _project_tick_offset."""
    import uuid

    import app.digest_precompute as dp
    from app.routers import admin_digest as _ad
    from app.routers import admin_health as _ah

    monkeypatch.setattr(dp, "_project_tick_offset", 0)
    projects = [uuid.uuid4() for _ in range(7)]
    digest_pids: list = []

    async def _capturing_compute(
        *, hours, project_id, session, _statement_timeout_secs=30
    ):
        digest_pids.append(project_id)
        return {"generated_at": "2026-01-01T00:00:00", "completed_count": 0}

    async def _fake_health(*, project_id, lookback_hours, session):
        return {"generated_at": "2026-01-01T00:00:00"}

    monkeypatch.setattr(_ad, "_compute_digest", _capturing_compute)
    monkeypatch.setattr(_ah, "_compute_health_overview", _fake_health)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=projects))

    @asynccontextmanager
    async def _session_factory():
        yield object()

    get_precompute_cache().clear()
    get_health_precompute_cache().clear()

    await run_precompute_tick(_session_factory)

    # Global key is primed first (project_id=None), then only 5 out of 7 projects.
    assert digest_pids[0] is None, "global key must be primed first"
    project_slice = [p for p in digest_pids if p is not None]
    assert len(project_slice) == dp._PROJECTS_PER_TICK_SLICE
    assert project_slice == projects[: dp._PROJECTS_PER_TICK_SLICE]

    # Offset advances by the slice size, wrapping mod n=7.
    assert dp._project_tick_offset == dp._PROJECTS_PER_TICK_SLICE % len(projects)


@pytest.mark.asyncio
async def test_overlap_guard_skips_tick_when_in_progress(monkeypatch):
    """digest_precompute_service skips run_precompute_tick when _tick_in_progress is True."""
    import asyncio

    import app.digest_precompute as dp

    tick_calls: list[bool] = []
    _call_n = {"n": 0}

    async def _fast_sleep(_t):
        _call_n["n"] += 1
        if _call_n["n"] >= 2:
            raise asyncio.CancelledError

    async def _mock_tick(sf):
        tick_calls.append(True)
        return dp.TickResult(refreshed=0, partial=False)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(dp, "run_precompute_tick", _mock_tick)
    monkeypatch.setattr(dp, "_tick_in_progress", True)

    try:
        await dp.digest_precompute_service()
    except asyncio.CancelledError:
        pass
    finally:
        dp._tick_in_progress = False

    assert tick_calls == [], (
        "run_precompute_tick must not be called when overlap guard is active"
    )


# ---------------------------------------------------------------------------
# WI-2: cooperative elapsed-time budget (precompute must never cancel its own
# in-flight query). Regression guard for the ~44/hr "[DB] Invalidated pool
# connection after CancelledError" bleed that was 1:1 with each tick overrun.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_over_budget_returns_partial_and_never_cancels_query(monkeypatch):
    """A tick whose cumulative work exceeds the cooperative budget stops BETWEEN
    units and returns a partial result — it never wraps a query in a cancelling
    timeout, so an in-flight ``session.execute`` runs to completion and the
    session is never invalidated (the WI-2 CancelledError bleed).

    The globals are primed first and unconditionally; the two global units here
    "spend" 40s of simulated wall time (> the 35s budget), so the per-project
    loop must not start and the round-robin offset must not advance.
    """
    import asyncio
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()
    get_health_precompute_cache().clear()

    projects = [uuid.uuid4() for _ in range(5)]

    # Simulated monotonic clock the tick reads via dp.time.monotonic. Each
    # per-unit compute advances it 20s, so after the two global units (40s) the
    # 35s cooperative budget is already exceeded before any per-project work.
    clock = {"t": 1000.0}
    monkeypatch.setattr(dp.time, "monotonic", lambda: clock["t"])

    executed: list[bool] = []

    class _FakeSession:
        async def execute(self, *a, **k):
            # A real in-flight query — the fix must never cancel this.
            await asyncio.sleep(0)
            executed.append(True)
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def merge(self, *a, **k):
            return None

    @asynccontextmanager
    async def _session_factory():
        yield _FakeSession()

    async def _slow_compute(*, hours, project_id, session, _statement_timeout_secs=30):
        await session.execute("SELECT 1")  # in-flight query — must complete
        clock["t"] += 20.0
        return {
            "generated_at": "t",
            "completed_count": 0,
            "_pid": str(project_id) if project_id else None,
        }

    async def _slow_health(*, project_id, lookback_hours, session):
        await session.execute("SELECT 1")
        clock["t"] += 20.0
        return {"generated_at": "t"}

    monkeypatch.setattr(_admin_digest, "_compute_digest", _slow_compute)
    monkeypatch.setattr(_admin_health, "_compute_health_overview", _slow_health)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=projects))
    monkeypatch.setattr(dp, "_project_tick_offset", 0)

    # No CancelledError / TimeoutError may propagate; the tick returns cleanly.
    result = await run_precompute_tick(_session_factory)

    assert result.partial is True
    # Only the two global units ran; the per-project loop was skipped.
    assert result.refreshed == 2
    # The in-flight queries completed — none was cancelled mid-execute.
    assert executed == [True, True]
    # Globals are primed; no project key was primed this tick.
    assert get_precompute_cache().get_fresh(24, None, max_age_seconds=300.0) is not None
    assert get_health_precompute_cache().get_fresh(24, None, max_age_seconds=300.0) is not None
    assert (
        get_precompute_cache().get_fresh(24, projects[0], max_age_seconds=300.0) is None
    )
    # The round-robin offset did NOT advance past unprimed projects: the next
    # tick resumes at project 0.
    assert dp._project_tick_offset == 0


@pytest.mark.asyncio
async def test_tick_within_budget_primes_all_and_prunes(monkeypatch):
    """A tick that stays within budget is NOT partial and still runs the L2
    prune (the cooperative cutoff is only reached on overrun)."""
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()
    get_health_precompute_cache().clear()

    projects = [uuid.uuid4() for _ in range(2)]
    prune_calls: list[bool] = []

    async def _fast_compute(*, hours, project_id, session, _statement_timeout_secs=30):
        return {"generated_at": "t", "completed_count": 0}

    async def _fast_health(*, project_id, lookback_hours, session):
        return {"generated_at": "t"}

    async def _spy_cleanup(session, *, older_than_seconds):
        prune_calls.append(True)
        return 0

    monkeypatch.setattr(_admin_digest, "_compute_digest", _fast_compute)
    monkeypatch.setattr(_admin_health, "_compute_health_overview", _fast_health)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=projects))
    monkeypatch.setattr(dp, "cleanup_stale_shared", _spy_cleanup)
    monkeypatch.setattr(dp, "_project_tick_offset", 0)

    @asynccontextmanager
    async def _session_factory():
        yield object()

    # A comfortably large budget so nothing truncates.
    result = await run_precompute_tick(_session_factory, budget_seconds=1000.0)

    assert result.partial is False
    assert result.refreshed == 6  # global + 2 projects, x2 kinds
    assert prune_calls == [True], "L2 prune must run on a non-partial tick"


@pytest.mark.asyncio
async def test_partial_tick_skips_l2_prune(monkeypatch):
    """A budget-truncated (partial) tick skips the L2 prune — deferring the
    occasional sweep one tick keeps the cooperative wall-time bound honest."""
    import uuid

    import app.digest_precompute as dp

    get_precompute_cache().clear()
    get_health_precompute_cache().clear()

    projects = [uuid.uuid4() for _ in range(5)]
    prune_calls: list[bool] = []

    clock = {"t": 500.0}
    monkeypatch.setattr(dp.time, "monotonic", lambda: clock["t"])

    async def _slow_compute(*, hours, project_id, session, _statement_timeout_secs=30):
        clock["t"] += 20.0
        return {"generated_at": "t", "completed_count": 0}

    async def _slow_health(*, project_id, lookback_hours, session):
        clock["t"] += 20.0
        return {"generated_at": "t"}

    async def _spy_cleanup(session, *, older_than_seconds):
        prune_calls.append(True)
        return 0

    monkeypatch.setattr(_admin_digest, "_compute_digest", _slow_compute)
    monkeypatch.setattr(_admin_health, "_compute_health_overview", _slow_health)
    monkeypatch.setattr(dp, "_active_project_ids", AsyncMock(return_value=projects))
    monkeypatch.setattr(dp, "cleanup_stale_shared", _spy_cleanup)
    monkeypatch.setattr(dp, "_project_tick_offset", 0)

    @asynccontextmanager
    async def _session_factory():
        yield object()

    result = await run_precompute_tick(_session_factory)

    assert result.partial is True
    assert prune_calls == [], "L2 prune must be skipped on a partial (over-budget) tick"


def test_hard_timeout_is_a_generous_multiple_of_cooperative_budget():
    """The last-resort hard timeout must stay a generous multiple (>= 3x) of the
    cooperative budget so it effectively never fires mid-query — the WI-2
    invariant that keeps the query-cancelling path a rare-wedge safety, not the
    every-tick path."""
    import app.digest_precompute as dp

    assert (
        dp.PRECOMPUTE_TICK_HARD_TIMEOUT_SECONDS >= 3 * dp.PRECOMPUTE_TICK_BUDGET_SECONDS
    )


@pytest.mark.asyncio
async def test_service_handles_partial_tick_without_error(monkeypatch):
    """digest_precompute_service consumes a TickResult and treats a partial tick
    (cooperative budget hit) as a normal outcome — it must not raise and must
    clear the overlap guard so the next interval can run."""
    import asyncio

    import app.digest_precompute as dp

    tick_calls: list[bool] = []
    _call_n = {"n": 0}

    async def _fast_sleep(_t):
        _call_n["n"] += 1
        if _call_n["n"] >= 2:
            raise asyncio.CancelledError

    async def _partial_tick(sf):
        tick_calls.append(True)
        return dp.TickResult(refreshed=2, partial=True)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(dp, "run_precompute_tick", _partial_tick)
    monkeypatch.setattr(dp, "_tick_in_progress", False)

    try:
        await dp.digest_precompute_service()
    except asyncio.CancelledError:
        pass
    finally:
        dp._tick_in_progress = False

    assert tick_calls == [True], "the tick must run once before shutdown"
    assert dp._tick_in_progress is False, "overlap guard must be cleared after a partial tick"
