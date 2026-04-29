import os
from typing import cast
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncEngine, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL")

def _make_engine():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")
    return create_async_engine(
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
