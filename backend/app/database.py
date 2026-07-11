"""Async database engine, session management, and connection-pool hardening.

Builds the lazily-initialized SQLAlchemy asyncpg engine from ``DATABASE_URL``
(normalizing SSL hints and listen DSNs), installs TCP keepalive and
disconnect-driven pool invalidation, and exposes the session entry points used
throughout the app: the ``get_session`` FastAPI dependency and the
``closing_session`` async context manager for background workers.
"""

import contextlib
import logging
import os
from typing import cast
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncEngine,
    AsyncSession,
)

DATABASE_URL = os.getenv("DATABASE_URL")
logger = logging.getLogger(__name__)


def resolve_dialect_name(session: AsyncSession) -> str:
    """Return the SQL dialect name (``"postgresql"`` / ``"sqlite"`` / ...) of
    the engine a query on ``session`` will actually run against.

    Footgun this guards against
    ---------------------------
    The naive idiom

        dialect_name = session.bind.dialect.name if session.bind else "postgresql"

    is a latent bug. An ``AsyncSession`` created from an ``async_sessionmaker``
    (i.e. every ``AsyncSessionLocal()`` background session, and FastAPI request
    sessions) exposes a **None ``.bind``** even though it resolves a concrete
    engine lazily at execute-time. The idiom therefore silently defaults to
    ``"postgresql"`` and emits Postgres-only SQL (e.g. the ``@>`` JSONB
    containment operator, ``jsonb`` casts, ``SET LOCAL statement_timeout``) even
    when the session is bound to **SQLite** — which blows up with
    ``sqlite3.OperationalError: unrecognized token: "@"`` on SQLite-based CI
    runs. A ``--postgres``-only test run never trips it (the dialect genuinely
    is Postgres), so it slips past local testing (triggering incident: PR
    #7099). Variants without the ``if session.bind`` None guard are worse still
    — they raise ``AttributeError`` on the None bind.

    Resolution order
    ----------------
    1. ``session.bind.dialect.name`` when ``session.bind`` is not None — this
       preserves today's behaviour for sessions that have an explicit bind, so
       all currently-passing tests are unaffected.
    2. ``session.get_bind().dialect.name`` otherwise — ``get_bind()`` resolves
       the ACTUAL engine the query will execute against (SQLite vs Postgres),
       independent of environment, which is exactly the None-bind case the
       idiom got wrong.
    3. ``"postgresql"`` (the production default) only if ``get_bind()`` itself
       raises (e.g. no engine configured at all).
    """
    bind = session.bind
    if bind is not None:
        return bind.dialect.name
    try:
        return session.get_bind().dialect.name
    except Exception:  # noqa: BLE001 — any bind-resolution failure must
        # degrade to the prod default rather than crash a query path; the
        # fallback IS the contract, so the catch is intentionally broad.
        return "postgresql"


def resolve_async_engine(session: AsyncSession) -> AsyncEngine:
    """Return the ``AsyncEngine`` a new ``async_sessionmaker`` should be bound
    to in order to mirror the engine ``session`` runs against.

    Footgun this guards against
    ---------------------------
    Code that needs a fresh, independently-committing session sometimes builds
    a scoped factory from the request session's bind::

        factory = async_sessionmaker(session.bind, ...)

    This is a latent bug for the same reason :func:`resolve_dialect_name`
    exists: an ``AsyncSession`` created from ``async_sessionmaker`` (every
    ``AsyncSessionLocal()`` background session and every FastAPI request
    session) exposes a **None ``.bind``** — it resolves a concrete engine
    lazily at execute-time. Passing that None straight into a new
    ``async_sessionmaker`` produces an *unbound* factory whose sessions raise
    ``UnboundExecutionError`` the moment they try to run a query. In tests the
    session is usually built with an explicit bind so the bug stays hidden; in
    production (None bind) it fails. The agent-status post-commit path and the
    admin bulk-retry path both build scoped factories this way.

    Resolution order
    ----------------
    1. ``session.bind`` when it is already an ``AsyncEngine`` — preserves the
       explicit-bind behaviour tests rely on.
    2. ``session.get_bind()`` when it resolves an ``AsyncEngine`` — this is the
       real engine the query would execute against in the None-bind case.
    3. the module-level lazy ``engine`` — the production default, matching the
       engine ``AsyncSessionLocal`` itself is bound to.
    """
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return bind
    try:
        resolved = session.get_bind()
        if isinstance(resolved, AsyncEngine):
            return resolved
    except Exception:  # noqa: BLE001 — any bind-resolution failure must
        # degrade to the module engine rather than crash the caller; the
        # fallback IS the contract, so the catch is intentionally broad.
        pass
    return cast(AsyncEngine, engine)


def _asyncpg_ssl_arg(value: str | None) -> object | None:
    """Map a SQLAlchemy/libpq SSL hint to an asyncpg ``ssl=`` connect kwarg.

    SQLAlchemy's asyncpg URL uses ``ssl=true``; libpq/asyncpg use ``sslmode``
    values. asyncpg accepts the libpq sslmode strings ('disable'..'verify-full')
    for its ``ssl`` kwarg, so we normalise to those. Returns ``None`` (asyncpg
    default) when there is no hint.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "1", "yes", "on", "require"):
        return "require"
    if v in ("false", "0", "no", "off", "disable"):
        return "disable"
    # libpq sslmode passthrough: allow / prefer / verify-ca / verify-full
    return v


def split_listen_dsn(database_url: str | None) -> tuple[str | None, object | None]:
    """Derive ``(dsn, ssl)`` for a raw ``asyncpg.connect`` from a SQLAlchemy URL.

    The LISTEN/NOTIFY connectors (``event_fanout``, ``jwt_revocation``) open
    dedicated raw asyncpg connections rather than borrowing from the pooled
    engine. They must NOT hand SQLAlchemy's URL straight to ``asyncpg.connect``:

    * the ``+asyncpg`` driver tag must be stripped, and
    * SQLAlchemy's ``?ssl=...`` query parameter must be pulled OUT of the DSN and
      translated into asyncpg's ``ssl=`` kwarg. Left in the DSN, asyncpg does not
      recognise ``ssl`` as a connection option and forwards it as a Postgres
      *startup parameter*; the server then rejects every connect with
      ``FATAL: parameter "ssl" cannot be changed now``, silently disabling all
      cross-instance fanout/revocation (incident 2026-05-29).

    Only the query string is rewritten — the ``user:pass@host`` netloc is left
    byte-for-byte intact. Returns ``(None, None)`` when ``database_url`` is
    absent or non-Postgres.
    """
    url = database_url or ""
    if "postgresql" not in url:
        return None, None
    # Strip the SQLAlchemy driver tag: postgresql+asyncpg -> postgresql.
    if "://" in url and "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = f"{scheme.split('+')[0]}://{rest}"
    base, _sep, query = url.partition("?")
    ssl_value: str | None = None
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, _eq, val = pair.partition("=")
        if key.lower() in ("ssl", "sslmode"):
            ssl_value = val
        else:
            kept.append(pair)
    dsn = base + (("?" + "&".join(kept)) if kept else "")
    return dsn, _asyncpg_ssl_arg(ssl_value)


# Client-side TCP keepalive tuning for the raw asyncpg LISTEN connections
# (``event_fanout``, ``jwt_revocation``). These connections block on
# ``terminated_event.wait()`` indefinitely, so a SILENT network partition
# (no FIN/RST — e.g. an NLB idle drop or a mid-path firewall eviction) leaves
# the listener hung forever with the connection appearing alive: no
# ``add_termination_listener`` callback fires and cross-instance fanout /
# revocation is stranded until the next process restart (incident class
# "LISTEN connections hang forever on silent network partition"). Enabling
# SO_KEEPALIVE with an aggressive idle/interval/count makes the kernel probe
# the peer and tear the socket down within ~KEEPIDLE + KEEPINTVL*KEEPCNT
# seconds of silence, which fires the termination listener and triggers
# reconnect. Values are conservative (detect within ~45s) but well above the
# loop's own SELECT-1 watchdog cadence so the two mechanisms compose.
_KEEPALIVE_IDLE_SECONDS = 30
_KEEPALIVE_INTERVAL_SECONDS = 5
_KEEPALIVE_COUNT = 3


def enable_tcp_keepalive(conn: object) -> None:
    """Enable client-side TCP keepalive on a raw asyncpg connection's socket.

    Best-effort: reaches through asyncpg's transport to the underlying socket
    and sets SO_KEEPALIVE plus the TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT
    tuning knobs where the platform exposes them (Linux always does; the
    per-knob options are absent on some platforms, so each is guarded
    independently). Never raises — a missing transport/option degrades to the
    OS default keepalive (or none), not a crash, so the caller's connect path
    is unaffected.
    """
    import socket

    try:
        transport = getattr(conn, "_transport", None)
        if transport is None:
            return
        sock = transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # TCP_KEEPIDLE / KEEPINTVL / KEEPCNT are Linux (and partially macOS);
        # guard each so absence on a platform is a no-op rather than an error.
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _KEEPALIVE_IDLE_SECONDS
            )
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_KEEPINTVL,
                _KEEPALIVE_INTERVAL_SECONDS,
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPALIVE_COUNT)
    except Exception as exc:  # noqa: BLE001 — keepalive is best-effort hardening
        logger.debug("[DB] enable_tcp_keepalive best-effort failed: %s", exc)


# Counter of consecutive invalidate() failures. Once this hits the escalation
# threshold we call `engine.sync_engine.pool.dispose()` to nuke the entire
# pool — necessary backstop if individual connection.invalidate() calls keep
# raising (e.g. driver-level corruption). Reset on every successful invalidate.
_invalidate_failure_count: int = 0
_INVALIDATE_FAILURE_ESCALATION_THRESHOLD: int = 3
# Counter of total invalidations for telemetry — exposed via /metrics or
# /api/admin/db-stats; dump at logger.warning whenever reasonable.
_invalidate_total_count: int = 0


def get_db_pool_invalidation_stats() -> dict:
    """Snapshot of pool-invalidation counters for /metrics and admin observability."""
    return {
        "invalidate_total": _invalidate_total_count,
        "invalidate_consecutive_failures": _invalidate_failure_count,
        "invalidate_escalation_threshold": _INVALIDATE_FAILURE_ESCALATION_THRESHOLD,
    }


# --- Connection-poisoning classifier (2026-06-14 flap) --------------------
#
# A query that hits a CLIENT-side command_timeout, that is cancelled by the
# server-side statement_timeout, or that races asyncpg's protocol into the
# "another operation is in progress" / "cannot switch to state" condition, leaves
# the connection in a state that is unsafe to hand back to the pool. None of
# these set SQLAlchemy's ``is_disconnect`` flag (the socket is still open), so
# without help the bad connection is returned to the pool and the next checkout
# inherits the wedge — surfacing as the
# ``InternalClientError: cannot switch to state N; another operation (M) is in
# progress`` cascade that hung the orchestrator/reaper background tasks for
# ~7 min and flapped the service on 2026-06-14. Treat these as eject-worthy
# alongside the disconnect class.
_POISON_EXC_NAMES = frozenset(
    {"TimeoutError", "QueryCanceledError", "InternalClientError"}
)
_POISON_MESSAGE_SUBSTRINGS = (
    "another operation is in progress",
    "cannot switch to state",
)


def _is_connection_poisoning_error(exc: object) -> bool:
    """True when ``exc`` (or its ``__cause__`` chain) indicates a connection left
    in a timed-out / protocol-wedged state that must be ejected from the pool
    rather than reused.

    Deliberately narrow: routine SQL errors (FK / unique violations, check
    constraint failures, etc.) match neither the exception-name set nor the
    message substrings, so the pool is NOT churned on every query error — only on
    the timeout / protocol-corruption class that genuinely poisons a connection.
    """
    seen = 0
    cur = exc
    while cur is not None and seen < 6:
        seen += 1
        if type(cur).__name__ in _POISON_EXC_NAMES:
            return True
        message = str(cur).lower()
        if any(sub in message for sub in _POISON_MESSAGE_SUBSTRINGS):
            return True
        cur = getattr(cur, "__cause__", None)
    return False


def _install_disconnect_invalidation(engine: AsyncEngine) -> None:
    """Eject pool connections after a disconnection-class error.

    `pool_pre_ping=True` only catches dead TCP at checkout. It does NOT catch
    a connection that fails mid-request because of a Cloud Run cutover, Cloud
    SQL failover, or asyncpg `InterfaceError("connection is closed")`. When
    that happens, the bad connection goes back to the pool, the next request
    pulls it out, and the SQLAlchemy session ends up in pending-rollback
    state — which surfaces downstream as `PendingRollbackError` on commit.

    Production incident 2026-05-04 19:46:43 UTC: Cloud Run rollout cutover
    triggered ~12 PendingRollbackError + InterfaceError cascades on the
    `/api/agent/status` and `/api/agent/heartbeat` endpoints. Adding a
    `handle_error` listener that calls `connection.invalidate()` on
    disconnection-class errors is the standard SQLAlchemy pattern for
    cutover-safe pools — it ejects the bad connection from the pool entirely
    so subsequent checkouts get a fresh one.

    R5 audit hardening (2026-05-04):
      - Use `is_disconnect` exclusively. The previous isinstance fallback
        against `sqlalchemy.exc.{Disconnection,Interface,Operational}Error`
        was misleading: by the time this listener fires, the original
        exception is the DBAPI-level wrapped form (e.g.
        `AsyncAdapt_asyncpg_dbapi.InterfaceError`), not the SQLAlchemy class.
        The dialect's `is_disconnect()` check (set BEFORE listeners fire) is
        the authoritative signal — `asyncpg`'s adapter correctly reports
        True for `InterfaceError("connection is closed")` and friends.
      - Escalate to `pool.dispose()` after 3 consecutive `invalidate()`
        failures, so the pool isn't left with stuck poisoned connections.
    """

    @event.listens_for(engine.sync_engine, "handle_error")
    def _invalidate_on_disconnect(ctx: object) -> None:
        global _invalidate_failure_count, _invalidate_total_count
        original_exc = getattr(ctx, "original_exception", None)
        # Eject on TWO classes:
        #   1. The disconnect class — `is_disconnect` is the authoritative
        #      SQLAlchemy-set flag (populated via `do_is_disconnect()` BEFORE
        #      listeners run); for asyncpg this catches "connection is closed",
        #      "admin shutdown", "connection does not exist", etc.
        #   2. The timeout / protocol-wedge class — command_timeout, server
        #      statement_timeout cancel, and "another operation is in progress" /
        #      "cannot switch to state". These do NOT set is_disconnect (the
        #      socket is still open) but still leave the connection unsafe to
        #      reuse; without ejecting them the poisoned connection re-enters the
        #      pool and wedges the next checkout (2026-06-14 flap).
        # Routine SQL errors match neither and leave the pool untouched.
        is_disconnect = bool(getattr(ctx, "is_disconnect", False))
        if not (is_disconnect or _is_connection_poisoning_error(original_exc)):
            return
        connection = getattr(ctx, "connection", None)
        if connection is None:
            return
        try:
            connection.invalidate()
            _invalidate_total_count += 1
            _invalidate_failure_count = 0  # reset on success
            logger.warning(
                "[DB] Invalidated pool connection after %s [%s] "
                "(total_invalidations=%d) — pool will recycle on next checkout",
                type(original_exc).__name__,
                "disconnect" if is_disconnect else "timeout/protocol-wedge",
                _invalidate_total_count,
            )
        except Exception as inv_exc:
            _invalidate_failure_count += 1
            logger.warning(
                "[DB] Connection invalidation failed (consecutive_failures=%d): %s",
                _invalidate_failure_count,
                inv_exc,
            )
            if _invalidate_failure_count >= _INVALIDATE_FAILURE_ESCALATION_THRESHOLD:
                # Repeated invalidation failures imply driver-level corruption.
                # Dispose the entire pool: existing checked-out connections
                # remain valid for in-flight requests, but the pool itself is
                # rebuilt on next checkout. Reset the counter so we don't
                # re-dispose on every subsequent error.
                logger.error(
                    "[DB] %d consecutive invalidate() failures — calling pool.dispose() "
                    "to rebuild the pool",
                    _invalidate_failure_count,
                )
                try:
                    engine.sync_engine.pool.dispose()
                except Exception as disp_exc:
                    logger.error(
                        "[DB] pool.dispose() raised: %s", disp_exc, exc_info=True
                    )
                _invalidate_failure_count = 0


# Client-side per-query deadline (seconds), passed straight to asyncpg.connect.
#
# server_settings.statement_timeout is SERVER-side: it only bounds a query the
# server is actively running on a LIVE connection. It cannot bound a query whose
# connection died mid-flight (no FIN/RST — an NLB idle drop / firewall eviction),
# nor one wedged behind asyncpg's "another operation is in progress" protocol
# lock — those hang with NO deadline until the background-task liveness watchdog
# cancels the whole task (~405s), the 2026-06-14 flap. command_timeout is the
# CLIENT-side backstop for exactly those cases. Set ABOVE statement_timeout so on
# a healthy connection the server-side cancel still wins (clean abort, connection
# stays usable) and this only fires when the server cannot respond at all.
_STATEMENT_TIMEOUT_MS = 120_000
_COMMAND_TIMEOUT_SECONDS = 150


def _engine_connect_args() -> dict:
    """asyncpg ``connect_args`` for the main pool. Extracted so the timeout
    hardening is unit-testable without standing up a live engine."""
    return {
        # CLIENT-side per-query deadline — backstop for dead / protocol-wedged
        # connections that server_settings.statement_timeout cannot bound.
        "command_timeout": _COMMAND_TIMEOUT_SECONDS,
        "server_settings": {
            "lock_timeout": "30000",  # 30s - prevents indefinite lock waits on FOR UPDATE
            "timezone": "UTC",  # Ensure naive timestamps are interpreted as UTC
            "idle_in_transaction_session_timeout": "60000",  # 60s - auto-rollback leaked transactions
            "statement_timeout": str(_STATEMENT_TIMEOUT_MS),  # kill runaway queries server-side
        },
    }


def _install_pool_keepalive(engine: AsyncEngine) -> None:
    """Apply TCP keepalive to every pooled asyncpg connection at connect time.

    ``enable_tcp_keepalive`` was previously wired only to the dedicated
    LISTEN/NOTIFY connections (event_fanout, jwt_revocation). The main query pool
    ran with the OS-default keepalive (Linux ``TCP_KEEPIDLE``=7200s), so a
    silently dropped pool socket went undetected for up to ~2h. Combined with the
    (previous) absence of a client-side command_timeout, a mid-query connection
    loss hung the issuing background task until the liveness watchdog cancelled it
    (2026-06-14). Registering ``enable_tcp_keepalive`` on the pool's ``connect``
    event brings pool sockets to the same ~45s dead-peer detection
    (KEEPIDLE 30s + KEEPINTVL 5s x KEEPCNT 3) as the LISTEN connections.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pool_keepalive(dbapi_connection: object, _record: object) -> None:
        # For the asyncpg dialect the raw asyncpg.Connection (which
        # enable_tcp_keepalive reaches through to the underlying socket) is
        # exposed as ``driver_connection``; fall back to the DBAPI wrapper
        # defensively. enable_tcp_keepalive never raises.
        raw = getattr(dbapi_connection, "driver_connection", dbapi_connection)
        enable_tcp_keepalive(raw)


def _make_engine():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")
    eng = create_async_engine(
        DATABASE_URL,
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",
        future=True,
        # Sized for Cloud SQL max_connections=200 cap across the Cloud Run fleet; bump when the tier upgrades.
        pool_size=10,
        max_overflow=15,
        pool_timeout=30,
        pool_recycle=540,
        pool_pre_ping=True,
        connect_args=_engine_connect_args(),
    )
    _install_disconnect_invalidation(eng)
    _install_pool_keepalive(eng)
    return eng


# Lazy engine: created on first access so test environments can import without asyncpg
class _LazyEngine:
    _engine = None

    def __getattr__(self, name):
        if self._engine is None:
            self.__class__._engine = _make_engine()
        return getattr(self._engine, name)

    def __call__(self, *args, **kwargs):
        if self._engine is None:
            self.__class__._engine = _make_engine()
        return self._engine(*args, **kwargs)


engine = _LazyEngine()


async def dispose_engine() -> None:
    """Gracefully close the main query pool's connections at app shutdown.

    Cloud Run drains an instance with SIGTERM. Without an explicit dispose the
    process exits with its asyncpg pool sockets still open, so Cloud SQL has to
    reap them on its own timeout — leaving orphaned server-side connections that
    keep counting against ``max_connections`` during the instance scale-in churn
    of a busy fleet. Disposing here closes them cleanly on the way out.

    No-op when the lazy engine was never instantiated (e.g. a test import that
    never touched the DB) — read the class attribute directly so this never
    *creates* an engine just to dispose it. Best-effort: never raises.
    """
    eng = _LazyEngine._engine
    if eng is None:
        return
    try:
        await eng.dispose()
    except Exception as exc:  # noqa: BLE001 — shutdown hardening is best-effort
        logger.warning("[DB] engine.dispose() on shutdown failed: %s", exc)


# Session factory for background tasks and dependency injection.
#
# expire_on_commit=False semantics (audit Phase C4 — M-C1):
# ---------------------------------------------------------
# By default, SQLAlchemy expires every attribute on every instance after
# `await session.commit()`. The next attribute access on a previously-loaded
# row issues a fresh SELECT (lazy refresh). That default protects callers
# from stale reads, but it has three drawbacks the codebase rejects:
#   1. Cloud SQL round-trip per attribute access (10-30ms each) inside hot
#      handler paths makes p99 latency unpredictable.
#   2. Async lazy-loads under FastAPI raise `MissingGreenlet` because attribute
#      access is synchronous from the framework's perspective — every
#      post-commit read would have to be wrapped in `await session.refresh()`.
#   3. Many handlers commit early to release `FOR UPDATE` locks before doing
#      external I/O (see `routers/agent_status.py:1694`,
#      `rollup_management.py:_release_parent_lock`). With expire-on-commit,
#      the row instance becomes a SELECT trap immediately after lock release;
#      with expire-off, attribute access continues to work without DB I/O.
#
# Trade-off: callers MUST treat any instance read after `commit()` as a
# point-in-time snapshot, not a live row. If correctness depends on the
# latest DB state (e.g. another writer may have mutated the row during the
# unlocked window), call `await session.refresh(obj)` or — preferred for
# locked re-acquire patterns — re-execute a `select(...).with_for_update()`
# and rebind the variable. See `rollup_management._relock_parent` for the
# canonical pattern: it re-selects, returns a fresh instance, and validates
# `updated_at` against a pre-release snapshot.
#
# Stable-identifier reads (primary keys, parent FKs) are always safe; only
# mutable columns (status, context, payloads, updated_at) carry stale-read
# risk. See docs/concurrency.md for the full discipline guide.
class _SafeCloseAsyncSession(AsyncSession):
    """AsyncSession that tolerates ``InterfaceError("connection is closed")``
    on close.

    Background services use ``async with AsyncSessionLocal() as session:``
    directly. When the asyncpg socket dies mid-cycle (Cloud SQL idle
    eviction, failover, Cloud Run cutover), the context manager's
    ``__aexit__`` invokes ``rollback()`` on a dead connection and
    raises — bubbling out as the actual production crash class.
    The disconnect listener has already invalidated the pool entry,
    so the bad connection won't be checked out again; swallowing the
    cleanup-time error keeps the surrounding cycle alive.

    Production 2026-05-08: services.py:44 + orchestrator.py:4750 +
    reaper.py:2407 cascades all originated in this close-time path.
    Subclassing here means *every* session — bare AsyncSessionLocal
    callers, ``get_session`` request handlers, and ``closing_session``
    callers — gets the same protection automatically.
    """

    async def close(self) -> None:
        from sqlalchemy.exc import InterfaceError as SAInterfaceError

        try:
            await super().close()
        except SAInterfaceError as e:
            msg = str(e).lower()
            if "connection is closed" in msg or "connection does not exist" in msg:
                logger.debug(
                    "[DB] Tolerating closed-connection during session close: %s", e
                )
                return
            raise


AsyncSessionLocal = async_sessionmaker(
    cast(AsyncEngine, engine),
    class_=_SafeCloseAsyncSession,
    expire_on_commit=False,
)


async def init_db():
    """Schema management is handled by Alembic migrations."""
    pass


async def get_session() -> AsyncSession:  # type: ignore
    """Dependency to provide a database session to FastAPI routes.

    The close path is wrapped to swallow ``InterfaceError("connection is closed")``
    that surfaces when the underlying asyncpg socket died mid-request (Cloud Run
    revision rollover, Cloud SQL failover, idle eviction). The disconnect listener
    in ``_install_disconnect_invalidation`` has already invalidated the pool entry;
    SQLAlchemy's session ``__aexit__`` then tries to roll back on the dead conn,
    asyncpg's adapter raises ``cannot call Transaction.rollback(): the underlying
    connection is closed``, which surfaces as a 500 on the request that already
    succeeded application-side. Suppressing the cleanup-time error keeps the
    response intact; the bad connection is gone from the pool either way.
    """
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await _safe_close(session)


async def _safe_close(session: AsyncSession) -> None:
    """Close a session, swallowing the asyncpg "connection is closed" rollback.

    See ``get_session`` for the full rationale. Background services that use
    ``async with AsyncSessionLocal() as session:`` directly bypass that
    handler and crash with ``InterfaceError`` whenever Cloud SQL evicts an
    idle connection between the last query and ``__aexit__``'s rollback.
    """
    from sqlalchemy.exc import InterfaceError as SAInterfaceError

    try:
        await session.close()
    except SAInterfaceError as e:
        msg = str(e).lower()
        if "connection is closed" in msg or "connection does not exist" in msg:
            logger.debug(
                "[DB] Tolerating closed-connection during session close: %s", e
            )
        else:
            raise


@contextlib.asynccontextmanager
async def closing_session():
    """Async-cm replacement for ``async with AsyncSessionLocal() as session``.

    Background services (reaper, orchestrator, gcp_cleanup, lifecycle, ...)
    use the bare ``AsyncSessionLocal()`` context manager and crash on close
    when the asyncpg socket has died mid-cycle (Cloud SQL idle eviction,
    failover, Cloud Run cutover). Switching such call sites to this helper
    routes the close through ``_safe_close`` and keeps the cycle alive — the
    disconnect listener has already invalidated the pool entry, so the next
    iteration gets a fresh connection.
    """
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await _safe_close(session)


# ---------------------------------------------------------------------------
# Rollback-safe read patterns — documented for the AST-based regression guard
# in test_audit_finding_class_guards.py::TestRollbackSafetyRegressionCheck.
# ---------------------------------------------------------------------------

# Recognised safe-read patterns that a function containing session.rollback()
# must use in its body to safely avoid MissingGreenlet on asyncpg.
# The test scanner checks for these patterns via AST analysis of backend/app/
# production code.  This constant lets the test exercise production code and
# also serves as canonical documentation of what constitutes a safe rollback.
ROLLBACK_SAFE_PATTERNS: frozenset[str] = frozenset({
    "session.refresh",       # explicit re-load from DB
    "db_session.refresh",    # explicit re-load via db_session
    "session.get",           # re-fetch via primary key
    "db.get",                # re-fetch via db.get
    "session.execute",       # re-fetch via query
    "db_session.execute",    # re-fetch via db_session
    "expunge_all",           # remove all objects from identity map
    "expire_all",            # expire all objects (no-op on SQLite, real on asyncpg)
    "__dict__",              # bypass expiry by reading from instance dict
    "return",                # exit function immediately after rollback
    "raise",                 # propagate exception after rollback
    "continue",              # continue loop iteration (no ORM read in this iteration)
    "break",                 # break out of loop (no ORM read in this iteration)
    "logger.exception",      # log and exit (typically followed by return/raise)
    "logger.warning",        # log and fall through or return
    "expired",               # .expired attribute access (self-aware check)
    "unloaded",              # sa_inspect(obj).unloaded deferred-column check
})

# Subset of ROLLBACK_SAFE_PATTERNS that are unconditionally safe regardless of
# where they appear relative to the rollback call.  A session.refresh() or
# session.execute() anywhere after rollback genuinely re-loads ORM state.
# Control-flow tokens (return/raise/break/continue) are only safe when they
# appear IN THE SAME BLOCK as the rollback, after it — a return at function
# level after an ORM attribute read does NOT make the read safe.  The flow-aware
# scanner in test_audit_finding_class_guards.py uses this constant to distinguish
# the two categories; this definition serves as canonical documentation.
ROLLBACK_SAFE_REFETCH_PATTERNS: frozenset[str] = frozenset({
    "session.refresh",
    "db_session.refresh",
    "session.get",
    "db.get",
    "session.execute",
    "db_session.execute",
    "expunge_all",
    "expire_all",
})
