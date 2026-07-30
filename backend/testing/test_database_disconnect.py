"""Tests for `_install_disconnect_invalidation` in `app.database`.

Closes audit gap a059#7 (zero coverage on the handle_error listener) and
audit gap test-coverage#1 (HIGH). The listener is the production fix for
the 2026-05-04 19:46 PendingRollbackError + InterfaceError cascade — without
tests, a regression that disabled it would land silently.

The tests construct a minimal AsyncEngine, register the listener, simulate
disconnect-class errors via the dispatch hook, and verify the
connection.invalidate() call + counter behavior.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.database as db_module
from app.database import (
    _COMMAND_TIMEOUT_SECONDS,
    _STATEMENT_TIMEOUT_MS,
    _database_pool_settings,
    _engine_connect_args,
    _install_disconnect_invalidation,
    _install_pool_keepalive,
    _is_connection_poisoning_error,
    dispose_engine,
    get_db_pool_invalidation_stats,
)


def _make_test_client(app) -> TestClient:
    """Construct a starlette/fastapi TestClient with the httpx ``app=`` shortcut
    deprecation suppressed.

    starlette 0.27's TestClient builds its httpx client via the ``app=`` shortcut
    that httpx 0.27+ deprecates (and 0.28 removed). That call is internal to
    starlette and unavoidable while fastapi is pinned to 0.104 — there is no test
    code to migrate. Suppress only that one DeprecationWarning at construction so
    the strict global ``filterwarnings = error`` doesn't fail on a
    framework-internal call; the test's own behavior assertions are unchanged.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return TestClient(app)


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


def test_deadlock_classifier_matches_sqlstate_name_and_message():
    """WI-6 defense-in-depth: ``_is_deadlock_error`` flags a Postgres deadlock
    (40P01) by SQLAlchemy-wrapped ``.orig.sqlstate``, by exception class name,
    or by message substring — so a residual post-commit deadlock routes to the
    retry-pending path (marker + sweep) instead of a 500 → Cloud Run quarantine.
    """
    from app.routers.agent_status import _is_deadlock_error

    # SQLAlchemy wraps asyncpg's DeadlockDetectedError; the stable surface is
    # the sqlstate on ``.orig``.
    wrapped = _named_exc("DBAPIError", "deadlock detected")
    wrapped.orig = _named_exc("DeadlockDetectedError", "deadlock detected")
    wrapped.orig.sqlstate = "40P01"
    assert _is_deadlock_error(wrapped)

    # Class name only (no sqlstate attribute).
    assert _is_deadlock_error(_named_exc("DeadlockDetectedError", "boom"))
    # Message substring only.
    assert _is_deadlock_error(_named_exc("OperationalError", "deadlock detected"))
    assert _is_deadlock_error(_named_exc("OperationalError", "40P01"))


def test_deadlock_classifier_ignores_non_deadlock_lock_errors():
    """The classifier is scoped to deadlock ONLY — a plain lock_timeout /
    LockNotAvailable (55P03) keeps its existing 500 path and must NOT be
    misrouted to retry-pending as a deadlock."""
    from app.routers.agent_status import _is_deadlock_error

    assert not _is_deadlock_error(_named_exc("LockNotAvailableError", "55P03"))
    assert not _is_deadlock_error(_named_exc("OperationalError", "lock timeout"))
    assert not _is_deadlock_error(ValueError("routine error"))
    lock_only = _named_exc("DBAPIError", "canceling statement due to lock timeout")
    lock_only.orig = _named_exc("LockNotAvailableError", "lock not available")
    lock_only.orig.sqlstate = "55P03"
    assert not _is_deadlock_error(lock_only)


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


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("api", (5, 5)),
        ("worker", (8, 8)),
        ("all", (10, 15)),
        ("unknown", (10, 15)),
    ],
)
def test_database_pool_settings_are_role_aware(monkeypatch, role, expected):
    monkeypatch.setenv("BLUEPRINT_PROCESS_ROLE", role)
    monkeypatch.delenv("BLUEPRINT_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("BLUEPRINT_DB_MAX_OVERFLOW", raising=False)
    assert _database_pool_settings() == expected


def test_database_pool_settings_allow_valid_overrides(monkeypatch):
    monkeypatch.setenv("BLUEPRINT_PROCESS_ROLE", "api")
    monkeypatch.setenv("BLUEPRINT_DB_POOL_SIZE", "12")
    monkeypatch.setenv("BLUEPRINT_DB_MAX_OVERFLOW", "3")
    assert _database_pool_settings() == (12, 3)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BLUEPRINT_DB_POOL_SIZE", "0", "at least 1"),
        ("BLUEPRINT_DB_MAX_OVERFLOW", "-1", "non-negative"),
    ],
)
def test_database_pool_settings_reject_invalid_limits(
    monkeypatch, name, value, message
):
    monkeypatch.setenv("BLUEPRINT_PROCESS_ROLE", "worker")
    monkeypatch.delenv("BLUEPRINT_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("BLUEPRINT_DB_MAX_OVERFLOW", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        _database_pool_settings()


def test_engine_connect_args_tags_connections_with_process_role(monkeypatch):
    monkeypatch.setenv("BLUEPRINT_PROCESS_ROLE", "worker")
    args = _engine_connect_args()
    assert args["server_settings"]["application_name"] == "blueprint-worker"


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


# ── dispose_engine: graceful pool close on shutdown ───────────────────────────


async def test_dispose_engine_noop_when_engine_uninitialized(monkeypatch):
    """No-op — and never *creates* an engine — when the lazy engine was never
    instantiated (e.g. a DB-free import that touched no session)."""
    monkeypatch.setattr(db_module._LazyEngine, "_engine", None)
    await dispose_engine()  # must not raise
    assert db_module._LazyEngine._engine is None  # and must not instantiate one


async def test_dispose_engine_disposes_live_engine(monkeypatch):
    """Awaits the live engine's dispose() exactly once so Cloud Run drain
    closes pooled asyncpg connections instead of abandoning them."""
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    monkeypatch.setattr(db_module._LazyEngine, "_engine", fake_engine)
    await dispose_engine()
    fake_engine.dispose.assert_awaited_once()


async def test_dispose_engine_swallows_dispose_failure(monkeypatch):
    """Shutdown hardening is best-effort: a dispose() failure never propagates."""
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(db_module._LazyEngine, "_engine", fake_engine)
    await dispose_engine()  # must not raise
    fake_engine.dispose.assert_awaited_once()


# ── /metrics endpoint auth tests ───────────────────────────────────────────


def test_metrics_endpoint_unauthenticated_when_token_unset(monkeypatch):
    """When METRICS_TOKEN is unset, /metrics returns 200 without token auth.
    Regression guard for #11217 that regressed to fail-closed (403)."""
    # Import here to avoid circular imports at module level
    import app.main as main_module

    # Monkeypatch _METRICS_TOKEN to empty string (unset)
    monkeypatch.setattr(main_module, "_METRICS_TOKEN", "")

    from app.main import app
    client = _make_test_client(app)

    # Without token, should return 200
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "blueprint_tasks_queued" in response.text


def test_metrics_endpoint_requires_token_when_set(monkeypatch):
    """When METRICS_TOKEN is set, /metrics requires matching token or returns 403."""
    import app.main as main_module

    test_token = "test-secure-token-12345"
    monkeypatch.setattr(main_module, "_METRICS_TOKEN", test_token)

    from app.main import app
    client = _make_test_client(app)

    # Without token, should return 403
    response = client.get("/metrics")
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}

    # With wrong token, should return 403
    response = client.get("/metrics?token=wrong-token")
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}


def test_metrics_endpoint_accepts_correct_token(monkeypatch):
    """When METRICS_TOKEN is set and correct token is provided, /metrics returns 200."""
    import app.main as main_module

    test_token = "test-secure-token-12345"
    monkeypatch.setattr(main_module, "_METRICS_TOKEN", test_token)

    from app.main import app
    client = _make_test_client(app)

    # With correct token, should return 200
    response = client.get(f"/metrics?token={test_token}")
    assert response.status_code == 200
    assert "blueprint_tasks_queued" in response.text
