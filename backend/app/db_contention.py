"""Detection of transient PostgreSQL row/lock contention errors.

Centralised runtime detector for the DB-contention error class that surfaces
when a statement is cancelled by the global ``lock_timeout`` (30s; set in
``database.py``), a deadlock is broken, or a later statement runs against a
transaction already aborted by one of those. These errors are TRANSIENT — the
losing transaction was rolled back cleanly by Postgres and the operation
self-heals once the lock releases — so a caller should retry (or leave the work
queued) rather than treat the task as a poison/permanent failure.

Why a dedicated helper rather than a one-off string match at each call site:
the exception the ORM re-raises is rarely the raw asyncpg error. SQLAlchemy
wraps it as ``DBAPIError`` (the original hangs off ``.orig``) and, once the
transaction is poisoned, subsequent statements raise ``PendingRollbackError`` /
``InFailedSQLTransactionError`` whose *own* message never mentions the lock. So
detection must walk the ``__cause__`` / ``__context__`` / ``.orig`` chain and
inspect SQLSTATE codes — not just ``str()`` the top-level exception.

Related, scope-specific detectors kept deliberately separate (different
contracts, separately tested — folding them in here would broaden their
semantics):
  * ``migration_retry.is_transient_lock_error`` — Alembic DDL fast-fail retry
    (top-level only, 55P03/40P01; no InFailedSQLTransaction handling).
  * ``recovery_post_commit._post_commit_retry_is_transient`` — post-commit
    sweep cap, which ALSO treats GitHub/LLM rate-limit + quota as transient.
"""

from __future__ import annotations

# SQLSTATE codes for the transient-contention class.
#   55P03 lock_not_available          — lock_timeout expiry (LockNotAvailableError)
#   40P01 deadlock_detected           — a deadlock cycle was broken
#   40001 serialization_failure       — retryable per the SQL standard
#   25P02 in_failed_sql_transaction   — a stmt ran against an already-aborted txn
_CONTENTION_SQLSTATES = frozenset({"55P03", "40P01", "40001", "25P02"})

# Lowercased substrings that identify the contention class in an exception's
# type name or message. Matched against every link in the __cause__/.orig chain
# (and against the flattened message SQLAlchemy embeds in PendingRollbackError,
# which carries "... Original exception was: <orig>").
_CONTENTION_SUBSTRINGS = (
    "locknotavailable",
    "lock not available",
    "lock timeout",
    "lock_timeout",
    "canceling statement due to lock timeout",
    "deadlock",
    "infailedsqltransaction",
    "in failed sql transaction",
    "current transaction is aborted",
    "serializationfailure",
    "serialization failure",
)


def _iter_exc_chain(exc: BaseException):
    """Yield ``exc`` and every exception reachable via ``__cause__``,
    ``__context__`` and ``.orig`` (SQLAlchemy's wrapped DBAPI original), guarding
    against reference cycles."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.append(getattr(e, "__cause__", None))
        stack.append(getattr(e, "__context__", None))
        stack.append(getattr(e, "orig", None))


def is_db_contention_error(exc: BaseException | None) -> bool:
    """True when ``exc`` (or anything in its cause/context/orig chain) is a
    transient DB row/lock contention error: a ``lock_timeout`` cancellation
    (LockNotAvailableError / 55P03), a deadlock (DeadlockDetected / 40P01), a
    serialization failure (40001), an already-aborted transaction
    (InFailedSQLTransactionError / 25P02), or a SQLAlchemy ``DBAPIError`` /
    ``PendingRollbackError`` wrapping any of those.

    Detection is by SQLSTATE first (asyncpg exposes ``.sqlstate``; psycopg
    ``.pgcode``), then by type-name/message substring — matched over the full
    exception chain because the surfaced error is usually a wrapper whose own
    message omits the underlying lock cause.
    """
    if exc is None:
        return False
    for e in _iter_exc_chain(exc):
        for attr in ("sqlstate", "pgcode"):
            code = getattr(e, attr, None)
            if isinstance(code, str) and code.upper() in _CONTENTION_SQLSTATES:
                return True
        blob = f"{type(e).__name__}: {e}".lower()
        if any(s in blob for s in _CONTENTION_SUBSTRINGS):
            return True
    return False
