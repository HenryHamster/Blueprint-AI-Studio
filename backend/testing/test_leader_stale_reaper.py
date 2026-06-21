"""Stale background-leader lock reaper.

Incident 2026-06-15: a liveness-killed leader (event loop wedged → /health 503 →
Cloud Run SIGKILL) never ran its graceful shutdown, so the SESSION-scoped
``pg_try_advisory_lock`` was never released. The non-leader watchdog
(``_monitor_non_leader``) probes ``pg_try_advisory_lock`` every 30s but could not
win until Postgres released the dead holder's lock — which only happens via
passive TCP death detection (~2.5 min observed). Worse: the leader-lock
connection is deliberately committed-idle (exempt from
``idle_in_transaction_session_timeout`` so the lock survives), so a wedged-but-
TCP-alive leader would hold the lock FOREVER and the gap never self-heals.

``_reap_stale_leader_lock_holder`` closes both: on a failed probe the watchdog
finds the backend holding THIS advisory lock whose ``state_change`` is older than
``_LEADER_STALE_REAP_SECONDS`` (3x the 30s keepalive → provably dead/wedged) and
``pg_terminate_backend``s it, freeing the lock in the current cycle. These tests
pin the safety-critical bits: the exact lock-key match, the terminate accounting,
and the threshold being strictly above a live leader's keepalive cadence.
"""

import os as _os

import pytest

from app.lifecycle import (
    _BACKGROUND_LOCK_CLASSID,
    _BACKGROUND_LOCK_ID,
    _BACKGROUND_LOCK_OBJID,
    _LEADER_STALE_REAP_SECONDS,
    _reap_stale_leader_lock_holder,
)


class _FakeResult:
    def __init__(self, *, scalars=None, scalar=None):
        self._scalars = list(scalars or [])
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars)

    def scalar(self):
        return self._scalar


class _ReapSession:
    """Stub async session keyed off SQL text.

    The ``FROM pg_locks`` SELECT returns the configured stale-holder pids; each
    ``pg_terminate_backend(pid)`` returns ``terminate_ok`` (Postgres returns
    false when the pid is already gone). Records what it was asked to do so the
    tests can assert the exact lock-key match and terminate accounting.
    """

    def __init__(self, *, stale_pids=(), terminate_ok=True):
        self.stale_pids = list(stale_pids)
        self.terminate_ok = terminate_ok
        self.terminated = []
        self.commits = 0
        self.select_params = None

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_terminate_backend" in sql:
            self.terminated.append(params["pid"])
            return _FakeResult(scalar=self.terminate_ok)
        if "FROM pg_locks" in sql:
            self.select_params = params
            return _FakeResult(scalars=self.stale_pids)
        return _FakeResult()

    async def commit(self):
        self.commits += 1


async def test_no_stale_holder_reaps_nothing():
    """A live (or absent) holder → no termination; the call is a cheap no-op
    that still commits so the probe connection isn't left idle-in-transaction."""
    session = _ReapSession(stale_pids=())

    reaped = await _reap_stale_leader_lock_holder(session)

    assert reaped == 0
    assert session.terminated == []
    assert session.commits == 1


async def test_stale_holder_is_terminated():
    """A holder past the staleness threshold is pg_terminate_backend'd, freeing
    the session-scoped lock for the watchdog's re-probe."""
    session = _ReapSession(stale_pids=[4242])

    reaped = await _reap_stale_leader_lock_holder(session)

    assert reaped == 1
    assert session.terminated == [4242]
    assert session.commits == 1


async def test_already_gone_holder_not_counted():
    """pg_terminate_backend returns false when the pid vanished between SELECT
    and terminate — that is not a reap (the lock is already free)."""
    session = _ReapSession(stale_pids=[7], terminate_ok=False)

    reaped = await _reap_stale_leader_lock_holder(session)

    assert reaped == 0
    assert session.terminated == [7]  # attempted...
    assert session.commits == 1  # ...and still committed


async def test_query_matches_exact_lock_key_and_threshold():
    """Safety pin: the reaper targets ONLY backends holding THIS advisory lock
    (exact classid/objid halves) and uses the configured staleness window — so it
    can never terminate a backend holding some other advisory lock (migration /
    research-spawn / bootstrap locks all use different keys)."""
    session = _ReapSession(stale_pids=[])

    await _reap_stale_leader_lock_holder(session)

    assert session.select_params["classid"] == _BACKGROUND_LOCK_CLASSID
    assert session.select_params["objid"] == _BACKGROUND_LOCK_OBJID
    assert session.select_params["stale_secs"] == _LEADER_STALE_REAP_SECONDS


def test_lock_key_halves_reconstruct_the_full_key():
    """The two unsigned 32-bit halves used to match pg_locks must reconstruct the
    unsigned 64-bit lock key exactly — a decomposition bug here would match the
    wrong (or no) backend."""
    rebuilt = (_BACKGROUND_LOCK_CLASSID << 32) | _BACKGROUND_LOCK_OBJID
    assert rebuilt == (_BACKGROUND_LOCK_ID & 0xFFFFFFFFFFFFFFFF)
    assert _BACKGROUND_LOCK_CLASSID <= 0xFFFFFFFF  # fits in a 32-bit oid column
    assert _BACKGROUND_LOCK_OBJID <= 0xFFFFFFFF


def test_threshold_is_safely_above_keepalive_cadence():
    """A live leader re-asserts the lock every LEADER_KEEPALIVE_SECONDS (default
    30s), so its holder's state_change is never older than one interval. The reap
    threshold must be a multiple of that so an active leader is provably never
    reaped (3 consecutive missed keepalives = dead/wedged)."""
    keepalive_default = 30.0
    min_safe_threshold = 3 * keepalive_default
    assert min_safe_threshold <= _LEADER_STALE_REAP_SECONDS


async def test_live_leader_not_reaped_within_threshold():
    """A holder whose state_change is fresher than stale_secs is NOT terminated.

    This is the SQLAlchemy pooling false-positive scenario: before the fix, an
    AsyncSession.commit() released the physical connection back to the pool, leaving
    the lock-holding backend idle. Its state_change would age past stale_secs and
    _reap_stale_leader_lock_holder would pg_terminate_backend it — killing a live
    leader's lock. With AsyncConnection, the keepalive always runs on the same
    connection so state_change is continuously refreshed, and this reaper path
    never fires on a healthy leader.

    Here we verify the guard: when the reaper sees zero stale pids (because the
    holder is live / recently active), it terminates nothing and returns 0.
    """
    session = _ReapSession(stale_pids=[])

    reaped = await _reap_stale_leader_lock_holder(session, stale_secs=90.0)

    assert reaped == 0
    assert session.terminated == []
    assert session.commits == 1


# ---------------------------------------------------------------------------
# Postgres-backed integration test
# ---------------------------------------------------------------------------

_PG_DB_URL = _os.getenv("DATABASE_URL", "")
_IS_POSTGRES = _os.getenv(
    "USE_TEST_POSTGRES", ""
).lower() == "true" and _PG_DB_URL.startswith("postgresql")


@pytest.mark.skipif(
    not _IS_POSTGRES,
    reason="Postgres advisory-lock integration test — set USE_TEST_POSTGRES=true with a postgresql DATABASE_URL",
)
async def test_live_leader_not_reaped_postgres_integration():
    """Fresh advisory-lock holder is NOT reaped; a time-expired holder IS terminated.

    Postgres-backed proof that the dedicated AsyncConnection fix prevents the
    false-positive: with stale_secs=90 the reaper sees the just-acquired lock
    holder as live and returns 0. With stale_secs=-1 (force-stale: any past
    state_change qualifies) the same holder is correctly terminated and the
    reaper returns 1. Skipped on the SQLite path.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    test_engine = create_async_engine(_PG_DB_URL, poolclass=NullPool)
    leader_conn = await test_engine.connect()
    try:
        # Acquire the advisory lock on a raw AsyncConnection (the v17 fix pattern).
        result = await leader_conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _BACKGROUND_LOCK_ID},
        )
        assert result.scalar(), (
            "Test setup: advisory lock already held (concurrent test?). "
            "Ensure no other test holds _BACKGROUND_LOCK_ID."
        )
        # Commit so the connection is idle (not idle-in-transaction); this also
        # updates state_change to now(), which the reaper query reads.
        await leader_conn.commit()

        ReaperSession = sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )

        # Part 1: fresh holder is NOT stale → reaper returns 0.
        async with ReaperSession() as reaper_session:
            reaped = await _reap_stale_leader_lock_holder(
                reaper_session, stale_secs=90.0
            )
        assert reaped == 0, (
            f"False-positive: healthy leader was reaped (reaped={reaped}). "
            "The dedicated-AsyncConnection fix may not be in effect."
        )

        # Part 2: force-stale (stale_secs=-1 makes any past state_change qualify).
        # Proves the reaper query reaches the lock row; if it returned 0 here too,
        # the lock-key constants or query would be broken.
        async with ReaperSession() as reaper_session2:
            reaped_stale = await _reap_stale_leader_lock_holder(
                reaper_session2, stale_secs=-1
            )
        assert reaped_stale == 1, (
            f"Stale holder was NOT terminated (reaped={reaped_stale}). "
            "Check _BACKGROUND_LOCK_CLASSID / _BACKGROUND_LOCK_OBJID constants."
        )
    finally:
        import contextlib

        async with contextlib.AsyncExitStack():
            with contextlib.suppress(Exception):
                await leader_conn.close()  # already terminated by stale-reap in Part 2
        await test_engine.dispose()
