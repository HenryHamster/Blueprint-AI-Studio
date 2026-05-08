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
        # `is_disconnect` is the authoritative SQLAlchemy-set flag. Dialects
        # populate it via `do_is_disconnect()` BEFORE listeners run; for
        # asyncpg this catches "connection is closed", "admin shutdown",
        # "connection does not exist", etc.
        if not getattr(ctx, "is_disconnect", False):
            return
        original_exc = getattr(ctx, "original_exception", None)
        connection = getattr(ctx, "connection", None)
        if connection is None:
            return
        try:
            connection.invalidate()
            _invalidate_total_count += 1
            _invalidate_failure_count = 0  # reset on success
            logger.warning(
                "[DB] Invalidated pool connection after %s "
                "(total_invalidations=%d) — pool will recycle on next checkout",
                type(original_exc).__name__,
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


def _make_engine():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")
    eng = create_async_engine(
        DATABASE_URL,
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",
        future=True,
        # Sized for Cloud SQL db-g1-small max_connections=200 cap across 5 Cloud Run instances; bump when tier upgrades.
        pool_size=10,
        max_overflow=15,
        pool_timeout=30,
        pool_recycle=540,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "lock_timeout": "30000",  # 30s - prevents indefinite lock waits on FOR UPDATE
                "timezone": "UTC",  # Ensure naive timestamps are interpreted as UTC
                "idle_in_transaction_session_timeout": "60000",  # 60s - auto-rollback leaked transactions
                "statement_timeout": "120000",  # 120s - kill runaway queries
            },
        },
    )
    _install_disconnect_invalidation(eng)
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
