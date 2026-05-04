import logging
import os
from typing import cast
from sqlalchemy import event
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncEngine, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL")
logger = logging.getLogger(__name__)


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
    """

    @event.listens_for(engine.sync_engine, "handle_error")
    def _invalidate_on_disconnect(ctx: object) -> None:
        original_exc = getattr(ctx, "original_exception", None)
        is_disconnect = getattr(ctx, "is_disconnect", False)
        if not is_disconnect and not isinstance(
            original_exc, (DisconnectionError, InterfaceError, OperationalError),
        ):
            return
        # is_disconnect is the authoritative SQLAlchemy signal; the isinstance
        # check above is a belt-and-suspenders catch for asyncpg-driver
        # exceptions that don't always set is_disconnect on the dialect adapter.
        connection = getattr(ctx, "connection", None)
        if connection is None:
            return
        try:
            connection.invalidate()
            logger.warning(
                "[DB] Invalidated pool connection after %s — pool will recycle on next checkout",
                type(original_exc).__name__,
            )
        except Exception as inv_exc:
            logger.warning("[DB] Connection invalidation failed (non-fatal): %s", inv_exc)


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
AsyncSessionLocal = async_sessionmaker(
    cast(AsyncEngine, engine), class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    """Schema management is handled by Alembic migrations."""
    pass

async def get_session() -> AsyncSession: # type: ignore
    """Dependency to provide a database session to FastAPI routes."""
    async with AsyncSessionLocal() as session:
        yield session
