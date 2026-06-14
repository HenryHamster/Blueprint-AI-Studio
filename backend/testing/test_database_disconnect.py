"""Tests for `_install_disconnect_invalidation` in `app.database`.

Closes audit gap a059#7 (zero coverage on the handle_error listener) and
audit gap test-coverage#1 (HIGH). The listener is the production fix for
the 2026-05-04 19:46 PendingRollbackError + InterfaceError cascade — without
tests, a regression that disabled it would land silently.

The tests construct a minimal AsyncEngine, register the listener, simulate
disconnect-class errors via the dispatch hook, and verify the
connection.invalidate() call + counter behavior.
"""

from unittest.mock import MagicMock

import pytest

import app.database as db_module
from app.database import (
    _COMMAND_TIMEOUT_SECONDS,
    _STATEMENT_TIMEOUT_MS,
    _engine_connect_args,
    _install_disconnect_invalidation,
    _install_pool_keepalive,
    _is_connection_poisoning_error,
    get_db_pool_invalidation_stats,
)


def _named_exc(name: str, message: str = "") -> Exception:
    """Build an exception whose ``type(...).__name__`` is ``name`` — lets the
    tests mimic asyncpg's TimeoutError / QueryCanceledError / InternalClientError
    without importing asyncpg (the standalone gate runs without the driver)."""
    return type(name, (Exception,), {})(message)


def _make_ctx(
    *,
    is_disconnect: bool,
    original_exc: Exception | None = None,
    connection: object | None = None,
) -> MagicMock:
    """Construct a minimal SQLAlchemy ExceptionContext stand-in."""
    ctx = MagicMock()
    ctx.is_disconnect = is_disconnect
    ctx.original_exception = original_exc or RuntimeError("test-error")
    ctx.connection = connection
    return ctx


@pytest.fixture(autouse=True)
def _reset_counters():
    """Reset module-level counters before each test so per-test assertions
    don't leak across tests in this module."""
    db_module._invalidate_total_count = 0
    db_module._invalidate_failure_count = 0
    yield
    db_module._invalidate_total_count = 0
    db_module._invalidate_failure_count = 0


def _attach_listener_get_dispatcher() -> tuple[MagicMock, callable]:
    """Build a fake AsyncEngine, install the listener, return (engine, listener_callable)."""
    engine = MagicMock()
    engine.sync_engine = MagicMock()
    captured = {}

    def fake_listens_for(target, event_name):
        def decorator(fn):
            captured["fn"] = fn
            return fn

        return decorator

    # Patch event.listens_for with our capturing decorator so we can call the
    # listener directly rather than driving it via real SQL.
    import sqlalchemy

    original = sqlalchemy.event.listens_for
    sqlalchemy.event.listens_for = fake_listens_for
    try:
        _install_disconnect_invalidation(engine)
    finally:
        sqlalchemy.event.listens_for = original
    return engine, captured["fn"]


def test_listener_invalidates_connection_on_is_disconnect_true():
    """The canonical disconnect path: SQLAlchemy sets is_disconnect=True
    (via dialect.do_is_disconnect), listener fires, connection.invalidate()
    is called, total counter increments."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    listener(_make_ctx(is_disconnect=True, connection=conn))
    conn.invalidate.assert_called_once()
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 1
    assert get_db_pool_invalidation_stats()["invalidate_consecutive_failures"] == 0


def test_listener_skips_when_is_disconnect_false():
    """Non-disconnect errors must not trigger pool invalidation — that
    would churn the pool on every routine SQL error (FK violation, etc)."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    listener(_make_ctx(is_disconnect=False, connection=conn))
    conn.invalidate.assert_not_called()
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 0


def test_listener_handles_none_connection():
    """ctx.connection can be None during pool-checkout pre-ping failures —
    the listener must not crash."""
    _, listener = _attach_listener_get_dispatcher()
    listener(_make_ctx(is_disconnect=True, connection=None))
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 0


def test_listener_swallows_invalidate_failure_and_counts_consecutive():
    """If connection.invalidate() raises, the listener must not propagate
    (would crash whatever DB op was in flight). It increments the failure
    counter for escalation tracking."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    conn.invalidate.side_effect = RuntimeError("invalidate-broke")
    listener(_make_ctx(is_disconnect=True, connection=conn))
    listener(_make_ctx(is_disconnect=True, connection=conn))
    assert get_db_pool_invalidation_stats()["invalidate_consecutive_failures"] == 2
    # No successful invalidations — counter stays 0
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 0


def test_listener_resets_consecutive_failure_count_on_success():
    """After two invalidate failures and one success, the consecutive counter
    resets so a single transient invalidate failure can't escalate the next
    deploy cutover."""
    engine, listener = _attach_listener_get_dispatcher()
    bad = MagicMock()
    bad.invalidate.side_effect = RuntimeError("transient")
    good = MagicMock()
    listener(_make_ctx(is_disconnect=True, connection=bad))
    listener(_make_ctx(is_disconnect=True, connection=bad))
    assert get_db_pool_invalidation_stats()["invalidate_consecutive_failures"] == 2
    listener(_make_ctx(is_disconnect=True, connection=good))
    assert get_db_pool_invalidation_stats()["invalidate_consecutive_failures"] == 0
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 1


def test_listener_escalates_to_pool_dispose_after_threshold():
    """3 consecutive invalidate failures must trigger pool.dispose() to
    rebuild the entire pool — backstop if individual connection invalidation
    keeps raising. After dispose the counter resets so we don't re-dispose
    on every subsequent error."""
    engine, listener = _attach_listener_get_dispatcher()
    bad = MagicMock()
    bad.invalidate.side_effect = RuntimeError("driver-corrupt")
    for _ in range(3):
        listener(_make_ctx(is_disconnect=True, connection=bad))
    engine.sync_engine.pool.dispose.assert_called_once()
    # Counter resets after dispose to avoid re-dispose on next error.
    assert get_db_pool_invalidation_stats()["invalidate_consecutive_failures"] == 0


def test_get_db_pool_invalidation_stats_returns_expected_shape():
    """Shape contract for the /metrics endpoint — main.py reads these keys."""
    stats = get_db_pool_invalidation_stats()
    assert "invalidate_total" in stats
    assert "invalidate_consecutive_failures" in stats
    assert "invalidate_escalation_threshold" in stats
    assert isinstance(stats["invalidate_total"], int)
    assert isinstance(stats["invalidate_consecutive_failures"], int)
    assert stats["invalidate_escalation_threshold"] == 3


# --------------------------------------------------------------------------
# 2026-06-14 flap regression coverage: a timed-out / protocol-wedged connection
# must be ejected (not pooled & reused), the pool must run a client-side query
# deadline, and pool sockets must get TCP keepalive.
# --------------------------------------------------------------------------


def test_poison_classifier_matches_timeout_and_protocol_wedge_classes():
    """asyncpg leaves a connection unsafe to reuse after a timeout / protocol
    wedge; the classifier must flag each by exception name or message."""
    assert _is_connection_poisoning_error(_named_exc("TimeoutError", "timed out"))
    assert _is_connection_poisoning_error(_named_exc("QueryCanceledError"))
    assert _is_connection_poisoning_error(
        _named_exc(
            "InternalClientError",
            "cannot switch to state 11; another operation (2) is in progress",
        )
    )
    # Matched by message substring even when the type name is unremarkable.
    assert _is_connection_poisoning_error(
        _named_exc(
            "InterfaceError",
            "cannot perform operation: another operation is in progress",
        )
    )


def test_poison_classifier_walks_cause_chain():
    """SQLAlchemy hands the listener the DBAPI-wrapped exception; the underlying
    asyncpg timeout is reachable via ``__cause__`` and must still be detected."""
    wrapped = _named_exc("DBAPIError", "wrapped")
    wrapped.__cause__ = _named_exc("TimeoutError", "command_timeout elapsed")
    assert _is_connection_poisoning_error(wrapped)


def test_poison_classifier_ignores_routine_sql_errors():
    """Routine errors must NOT be flagged — otherwise the pool churns a
    connection on every FK / unique / value error."""
    assert not _is_connection_poisoning_error(None)
    assert not _is_connection_poisoning_error(ValueError("bad value"))
    assert not _is_connection_poisoning_error(
        _named_exc("IntegrityError", "duplicate key value violates unique constraint")
    )
    assert not _is_connection_poisoning_error(RuntimeError("routine error"))


def test_listener_invalidates_on_timeout_class_without_is_disconnect():
    """The core flap fix: a TimeoutError-class error does NOT set is_disconnect,
    but the listener must still eject the connection so it is not reused."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    listener(
        _make_ctx(
            is_disconnect=False,
            original_exc=_named_exc("TimeoutError", "query timed out"),
            connection=conn,
        )
    )
    conn.invalidate.assert_called_once()
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 1


def test_listener_invalidates_on_protocol_wedge_message():
    """The exact 2026-06-14 signature — 'cannot switch to state ...; another
    operation is in progress' with is_disconnect=False — must eject."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    listener(
        _make_ctx(
            is_disconnect=False,
            original_exc=_named_exc(
                "InternalClientError",
                "cannot switch to state 11; another operation (2) is in progress",
            ),
            connection=conn,
        )
    )
    conn.invalidate.assert_called_once()
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 1


def test_listener_still_skips_benign_non_disconnect_error():
    """Regression guard for the broadened predicate: a routine non-disconnect
    SQL error must STILL be skipped (no pool churn)."""
    _, listener = _attach_listener_get_dispatcher()
    conn = MagicMock()
    listener(
        _make_ctx(
            is_disconnect=False,
            original_exc=_named_exc("IntegrityError", "duplicate key"),
            connection=conn,
        )
    )
    conn.invalidate.assert_not_called()
    assert get_db_pool_invalidation_stats()["invalidate_total"] == 0


def test_engine_connect_args_sets_command_timeout_above_statement_timeout():
    """The pool must carry a CLIENT-side command_timeout (server-side
    statement_timeout cannot bound a dead/wedged connection), and it must sit
    above statement_timeout so the server-side cancel wins on a live connection."""
    args = _engine_connect_args()
    assert args["command_timeout"] == _COMMAND_TIMEOUT_SECONDS
    # command_timeout is seconds; statement_timeout is milliseconds.
    assert _COMMAND_TIMEOUT_SECONDS * 1000 > _STATEMENT_TIMEOUT_MS
    assert args["server_settings"]["statement_timeout"] == str(_STATEMENT_TIMEOUT_MS)


def test_pool_keepalive_registered_on_connect_event_and_applies_to_raw_conn(
    monkeypatch,
):
    """Pool connections (not just LISTEN connections) must get TCP keepalive: the
    listener registers on the ``connect`` event and applies keepalive to the raw
    asyncpg connection (``driver_connection``)."""
    import sqlalchemy

    captured: dict = {}
    keepalive_calls: list = []
    monkeypatch.setattr(
        db_module, "enable_tcp_keepalive", lambda c: keepalive_calls.append(c)
    )

    def fake_listens_for(target, event_name):
        captured["event_name"] = event_name

        def decorator(fn):
            captured["fn"] = fn
            return fn

        return decorator

    original = sqlalchemy.event.listens_for
    sqlalchemy.event.listens_for = fake_listens_for
    try:
        engine = MagicMock()
        engine.sync_engine = MagicMock()
        _install_pool_keepalive(engine)
    finally:
        sqlalchemy.event.listens_for = original

    assert captured["event_name"] == "connect"
    raw_conn = object()
    dbapi_conn = MagicMock()
    dbapi_conn.driver_connection = raw_conn
    captured["fn"](dbapi_conn, MagicMock())
    assert keepalive_calls == [raw_conn]
