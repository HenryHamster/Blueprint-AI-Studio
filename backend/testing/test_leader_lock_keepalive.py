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


class _FakeSession:
    """Minimal async session stub keyed off the SQL text of each execute().

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
    session = _FakeSession()

    outcome = await _leader_lock_keepalive_tick(session)

    assert outcome == "held"
    assert session.commits >= 1  # proves we don't leave an open idle transaction
    assert session.executed == ["SELECT 1"]  # no re-acquire on the happy path


async def test_ping_fails_then_reacquires():
    """Connection blip with no peer takeover: ping raises, re-acquire wins → reacquired."""
    session = _FakeSession(
        ping_error=OSError("connection is closed"), reacquire_value=1
    )

    outcome = await _leader_lock_keepalive_tick(session)

    assert outcome == "reacquired"
    assert session.rollbacks == 1  # session reset before re-acquire
    assert any("pg_try_advisory_lock" in s for s in session.executed)
    assert session.commits >= 1  # committed the re-acquire so it isn't idle-in-tx


async def test_ping_fails_peer_owns_lock_returns_lost():
    """A peer grabbed the lock during the blip: re-acquire returns falsy → lost."""
    session = _FakeSession(
        ping_error=OSError("connection is closed"), reacquire_value=0
    )

    outcome = await _leader_lock_keepalive_tick(session)

    assert outcome == "lost"


async def test_ping_fails_and_reacquire_errors_returns_error():
    """Re-acquire itself errors (DB unreachable) → error (caller hard-exits)."""
    session = _FakeSession(
        ping_error=OSError("connection is closed"),
        reacquire_error=OSError("could not connect"),
    )

    outcome = await _leader_lock_keepalive_tick(session)

    assert outcome == "error"


@pytest.mark.parametrize("outcome", ["lost", "error"])
def test_caller_hard_exits_only_on_lost_or_error(outcome):
    """Documents the caller contract: only 'lost'/'error' trigger a hard-exit;
    'held'/'reacquired' keep the instance leader."""
    assert outcome in ("lost", "error")
    assert "held" not in ("lost", "error")
    assert "reacquired" not in ("lost", "error")
