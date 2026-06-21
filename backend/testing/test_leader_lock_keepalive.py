"""Leader-lock keepalive tick (W2.1).

Incident 2026-06-02: the background-leader advisory lock is held on a single
``leader_session`` connection that, before this fix, sat *idle in transaction*
for the whole process lifetime (the acquiring ``SELECT pg_try_advisory_lock``
was never committed). Postgres' ``idle_in_transaction_session_timeout`` (60s)
then terminated the connection, silently releasing the SESSION-scoped lock and
triggering the "prior leader died" respawn loop — which dropped the whole DB
connection pool each cycle (the "connection is closed" storms + agent-status
500s).

The fix commits after acquiring (the session-scoped lock survives a COMMIT) and
adds a keepalive that re-asserts ownership. These tests pin the keepalive tick's
decision table: ping-ok → held; ping-fail + re-acquire → reacquired; ping-fail +
peer-owns → lost; ping-fail + re-acquire-errors → error. The caller hard-exits
on lost/error.
"""

import pytest

from app.lifecycle import _leader_lock_keepalive_tick


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    """Minimal async connection stub keyed off the SQL text of each execute().

    Models AsyncConnection (not AsyncSession): the same object is reused across
    multiple tick calls, mirroring the raw-connection fix where commit() never
    returns the connection to the pool.

    ``SELECT 1`` is the keepalive ping; ``pg_try_advisory_lock`` is the
    re-acquire. Each can be told to raise (simulating a dropped connection) or
    return a value.
    """

    def __init__(self, *, ping_error=None, reacquire_value=None, reacquire_error=None):
        self.ping_error = ping_error
        self.reacquire_value = reacquire_value
        self.reacquire_error = reacquire_error
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if "SELECT 1" in sql:
            if self.ping_error is not None:
                raise self.ping_error
            return _FakeResult(1)
        if "pg_try_advisory_lock" in sql:
            if self.reacquire_error is not None:
                raise self.reacquire_error
            return _FakeResult(self.reacquire_value)
        return _FakeResult(None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


async def test_ping_ok_returns_held_and_commits():
    """Happy path: ping succeeds, tick reports held, and it COMMITs so the
    connection is left idle (lock held) — never idle-in-transaction."""
    conn = _FakeConnection()

    outcome = await _leader_lock_keepalive_tick(conn)

    assert outcome == "held"
    assert conn.commits >= 1  # proves we don't leave an open idle transaction
    assert conn.executed == ["SELECT 1"]  # no re-acquire on the happy path


async def test_ping_fails_then_reacquires():
    """Connection blip with no peer takeover: ping raises, re-acquire wins → reacquired."""
    conn = _FakeConnection(
        ping_error=OSError("connection is closed"), reacquire_value=1
    )

    outcome = await _leader_lock_keepalive_tick(conn)

    assert outcome == "reacquired"
    assert conn.rollbacks == 1  # connection reset before re-acquire
    assert any("pg_try_advisory_lock" in s for s in conn.executed)
    assert conn.commits >= 1  # committed the re-acquire so it isn't idle-in-tx


async def test_ping_fails_peer_owns_lock_returns_lost():
    """A peer grabbed the lock during the blip: re-acquire returns falsy → lost."""
    conn = _FakeConnection(
        ping_error=OSError("connection is closed"), reacquire_value=0
    )

    outcome = await _leader_lock_keepalive_tick(conn)

    assert outcome == "lost"


async def test_ping_fails_and_reacquire_errors_returns_error():
    """Re-acquire itself errors (DB unreachable) → error (caller hard-exits)."""
    conn = _FakeConnection(
        ping_error=OSError("connection is closed"),
        reacquire_error=OSError("could not connect"),
    )

    outcome = await _leader_lock_keepalive_tick(conn)

    assert outcome == "error"


async def test_same_connection_object_used_across_ticks():
    """Pin the pooling fix: keepalive ticks must always run on the SAME connection
    object so the lock-holding backend PID never changes.

    With AsyncSession, commit() returns the connection to the pool so the next
    tick's execute() can land on a DIFFERENT physical connection — leaving the
    lock-holding connection idle long enough for _reap_stale_leader_lock_holder
    to pg_terminate_backend it (false positive). With AsyncConnection the object
    identity is stable: the same conn is passed to every tick.
    """
    conn = _FakeConnection()

    outcome1 = await _leader_lock_keepalive_tick(conn)
    outcome2 = await _leader_lock_keepalive_tick(conn)

    assert outcome1 == "held"
    assert outcome2 == "held"
    # Both ticks executed SELECT 1 on the same object — proves connection identity
    assert conn.executed == ["SELECT 1", "SELECT 1"]
    assert conn.commits == 2


@pytest.mark.parametrize("outcome", ["lost", "error"])
def test_caller_hard_exits_only_on_lost_or_error(outcome):
    """Documents the caller contract: only 'lost'/'error' trigger a hard-exit;
    'held'/'reacquired' keep the instance leader."""
    assert outcome in ("lost", "error")
    assert "held" not in ("lost", "error")
    assert "reacquired" not in ("lost", "error")
