"""Transient-lock retry wrapper for Alembic migrations.

Extracted from env.py so the logic is importable in tests without
triggering alembic's module-level context execution.
"""

import asyncio
import logging

from sqlalchemy import text

logger = logging.getLogger("alembic.env")

# Fail-fast lock timeout per migration attempt. If a migration cannot acquire
# its DDL lock within this window, Postgres raises LockNotAvailable (55P03)
# and the retry loop fires rather than blocking indefinitely.
MIGRATION_LOCK_TIMEOUT = "10s"

# Maximum retry attempts for transient lock errors before giving up.
MAX_MIGRATION_ATTEMPTS = 5


def is_transient_lock_error(exc: BaseException) -> bool:
    """True when exc is a transient Postgres lock contention error.

    Covers deadlock cycles (40P01, DeadlockDetected) and lock_timeout
    expiry (55P03, LockNotAvailable). These are safe to retry because
    Postgres rolled back the victim transaction cleanly; no partial state
    was committed. Matching is string-based: SQLAlchemy wraps asyncpg
    exceptions and the stable surface is the message/class name text.
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    return (
        "deadlock" in msg
        or "lock timeout" in msg
        or "lock_timeout" in msg
        or "locknotavailable" in msg
        or "lock not available" in msg
        or "40p01" in msg
        or "55p03" in msg
    )


async def run_migrations_with_retry(connectable, do_run_fn) -> None:  # type: ignore[no-untyped-def]
    """Run do_run_fn on a fresh connection, retrying on transient lock errors.

    Acquires a new connection on each attempt and sets lock_timeout so the
    migration fails fast on lock contention instead of blocking indefinitely.
    Non-transient errors surface immediately; transient errors are retried up
    to MAX_MIGRATION_ATTEMPTS times with exponential backoff.

    lock_timeout is SET at session level then immediately committed so that
    alembic's MigrationContext sees connection.in_transaction() == False.
    Without the commit, the autobegin transaction would cause alembic to set
    _in_external_transaction=True, collapsing transaction_per_migration into
    a single transaction and preventing migrations from persisting.
    """
    last_exc: BaseException | None = None
    for attempt in range(MAX_MIGRATION_ATTEMPTS):
        if attempt > 0:
            delay = 2**attempt  # 2, 4, 8, 16 seconds
            logger.warning(
                "Migration attempt %d/%d failed with transient lock error; "
                "retrying in %.0fs: %s",
                attempt,
                MAX_MIGRATION_ATTEMPTS,
                delay,
                last_exc,
            )
            await asyncio.sleep(delay)
        try:
            async with connectable.connect() as connection:
                # Set fail-fast lock_timeout at session level. Commit the
                # implicit autobegin transaction immediately so that
                # do_run_fn sees in_transaction() == False and alembic manages
                # its own per-migration transactions (transaction_per_migration).
                # Session-level SET persists across the subsequent transaction.
                await connection.execute(
                    text(f"SET lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'")
                )
                await connection.commit()
                await connection.run_sync(do_run_fn)
            return  # success
        except Exception as exc:
            if is_transient_lock_error(exc):
                last_exc = exc
                continue
            raise  # non-transient: surface immediately

    # All attempts exhausted — re-raise as a clear failure.
    raise RuntimeError(
        f"Migration failed after {MAX_MIGRATION_ATTEMPTS} attempts "
        f"due to persistent transient lock errors"
    ) from last_exc
