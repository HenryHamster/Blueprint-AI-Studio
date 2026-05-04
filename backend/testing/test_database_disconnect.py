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
from app.database import _install_disconnect_invalidation, get_db_pool_invalidation_stats


def _make_ctx(*, is_disconnect: bool, original_exc: Exception | None = None,
              connection: object | None = None) -> MagicMock:
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
