"""Cross-instance JWT revocation via PostgreSQL LISTEN/NOTIFY.

All Cloud Run instances listen on the 'task_terminated' channel so a
termination committed on instance A populates the in-process
_terminated_task_ids cache on instances B and C within seconds, turning
the slow-path DB check into a fast-path deny-list hit.
"""

import asyncio
import logging
import os
import time

try:
    import asyncpg as _asyncpg
except ImportError:  # pragma: no cover — asyncpg absent in standalone test env
    _asyncpg = None  # type: ignore[assignment]

from sqlalchemy import text

from app.database import split_listen_dsn

logger = logging.getLogger(__name__)

_NOTIFY_CHANNEL = "task_terminated"
# True when the process is connected to Postgres; controls no-op guards.
_IS_POSTGRES = "postgresql" in (os.getenv("DATABASE_URL") or "")


def _get_raw_dsn() -> str | None:
    """Return the asyncpg-compatible DSN derived from DATABASE_URL.

    Strips the SQLAlchemy driver tag (``+asyncpg``) AND the SQLAlchemy-style
    ``ssl`` query parameter (see ``app.database.split_listen_dsn`` — left in the
    DSN it makes asyncpg send ``ssl`` as a Postgres startup parameter, which the
    server rejects with FATAL, breaking revocation). Returns None for
    non-Postgres / absent DATABASE_URL.
    """
    return split_listen_dsn(os.getenv("DATABASE_URL"))[0]


def _get_ssl_arg() -> object | None:
    """asyncpg ``ssl=`` kwarg derived from DATABASE_URL's ssl/sslmode hint."""
    return split_listen_dsn(os.getenv("DATABASE_URL"))[1]


async def _seed_deny_list(conn, terminated_task_ids: dict) -> None:
    """Backfill the local deny-list from the DB for recently-terminated tasks.

    Called at startup (cold-start gap) and after each LISTEN reconnect
    (reconnect gap) so events that arrived while we were disconnected are
    not silently missed.  Failures are logged and swallowed — losing the
    seed degrades to the slow DB-recheck path, not a fatal error.
    """
    # Late import avoids circular dep; matches the import in jwt_revocation_service.
    from app.routers.agent import _TERMINATED_TTL  # noqa: PLC0415

    ttl_minutes = _TERMINATED_TTL // 60
    try:
        rows = await conn.fetch(
            """
            SELECT id::text FROM task
            WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
              AND updated_at > NOW() - ($1 * INTERVAL '1 minute')
            LIMIT 10000
            """,
            ttl_minutes,
        )
        now = time.time()
        added = 0
        for row in rows:
            task_id = row["id"]
            if task_id not in terminated_task_ids:
                terminated_task_ids[task_id] = now
                added += 1
        logger.info(
            "[JWT_REVOCATION] Seeded deny-list: %d tasks added (%d total terminal in window)",
            added,
            len(rows),
        )
    except Exception as exc:
        logger.warning(
            "[JWT_REVOCATION] _seed_deny_list failed, continuing without pre-seed: %s",
            exc,
        )


async def jwt_revocation_service() -> None:
    """Background service: LISTEN for task_terminated notifications.

    Opens a dedicated raw asyncpg connection (never borrowed from the
    SQLAlchemy pool, which recycles connections and enforces
    idle_in_transaction_session_timeout).  On each NOTIFY, populates the
    in-process _terminated_task_ids dict exactly as the existing in-process
    terminal callback does.

    Reconnects with bounded exponential backoff (1 s → 60 s) using
    asyncpg's add_termination_listener + asyncio.Event.
    """
    # Late import avoids circular dep: agent.py → models.py → this module → agent.py
    from app.routers.agent import _terminated_task_ids  # noqa: PLC0415

    dsn = _get_raw_dsn()
    _ssl_arg = _get_ssl_arg()
    if not dsn or _asyncpg is None:
        logger.info(
            "[JWT_REVOCATION] Non-Postgres dialect or asyncpg absent; listener inactive"
        )
        # Sleep until cancelled so the monitor doesn't tight-loop restarts.
        try:
            await asyncio.sleep(365 * 24 * 3600)
        except asyncio.CancelledError:
            pass
        return

    attempt = 0
    while True:
        try:
            conn = await _asyncpg.connect(dsn, ssl=_ssl_arg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait = min(1 * 2**attempt, 60)
            logger.warning(
                "[JWT_REVOCATION] Connect failed (attempt %d, retry in %ds): %s",
                attempt,
                wait,
                exc,
            )
            attempt += 1
            await asyncio.sleep(wait)
            continue

        attempt = 0  # reset backoff after successful connect
        # A fresh Event starts cleared. Do not clear it again after registering
        # the termination listener or an immediate callback can be lost before
        # we enter the wait below.
        terminated_event: asyncio.Event = asyncio.Event()

        def _on_terminate(conn):  # noqa: ANN001 — asyncpg callback
            terminated_event.set()

        def _on_notify(conn, pid, channel, payload):  # noqa: ANN001
            if payload:
                _terminated_task_ids[payload] = time.time()
                logger.debug(
                    "[JWT_REVOCATION] Received task_terminated notify for task %s",
                    payload,
                )

        try:
            conn.add_termination_listener(_on_terminate)
            await conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
            await _seed_deny_list(conn, _terminated_task_ids)
            logger.info(
                "[JWT_REVOCATION] LISTEN %s — cross-instance revocation active",
                _NOTIFY_CHANNEL,
            )

            # Block until the server terminates the connection.
            await terminated_event.wait()
            logger.warning("[JWT_REVOCATION] Connection terminated; will reconnect")
        except asyncio.CancelledError:
            try:
                await conn.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.warning("[JWT_REVOCATION] Listener error: %s", exc)
        finally:
            try:
                await conn.close()
            except Exception:
                pass

        wait = min(1 * 2**attempt, 60)
        attempt += 1
        await asyncio.sleep(wait)


def emit_task_terminated_notify(session, task_id: str) -> None:
    """Sync helper: issue pg_notify inside the current transaction.

    Designed for use in SQLAlchemy ``before_flush`` event listeners where the
    session is the underlying sync Session (not AsyncSession).  Within that
    context, ``session.execute()`` is safe to call synchronously because
    flush itself runs in a greenlet (via ``run_sync``).

    NOTIFY is transactional in Postgres — listeners see the notification only
    after the surrounding transaction commits, so they observe the status
    change as durable before acting on it.

    No-ops on non-Postgres dialects (SQLite in tests).  Exceptions are
    swallowed so a NOTIFY failure never aborts the status mutation.
    """
    if not _IS_POSTGRES:
        return
    try:
        session.execute(
            text("SELECT pg_notify(:channel, :tid)"),
            {"channel": _NOTIFY_CHANNEL, "tid": task_id},
        )
    except Exception as exc:
        logger.warning(
            "[JWT_REVOCATION] emit_task_terminated_notify failed for task %s: %s",
            task_id,
            exc,
        )


async def emit_bulk_task_terminated_notify(session, task_ids) -> None:
    """Async helper: issue pg_notify for multiple tasks inside the current transaction.

    Designed for async router code (takes AsyncSession).  Issues a separate
    pg_notify per task so the NOTIFY channel payload can be a bare UUID
    string without additional framing.

    No-ops on non-Postgres dialects.  Exceptions are swallowed per-task so a
    single failure does not block the remaining notifications.
    """
    if not _IS_POSTGRES:
        return
    for task_id in task_ids:
        try:
            await session.execute(
                text("SELECT pg_notify(:channel, :tid)"),
                {"channel": _NOTIFY_CHANNEL, "tid": str(task_id)},
            )
        except Exception as exc:
            logger.warning(
                "[JWT_REVOCATION] emit_bulk_task_terminated_notify failed for task %s: %s",
                task_id,
                exc,
            )
