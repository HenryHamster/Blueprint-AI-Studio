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
