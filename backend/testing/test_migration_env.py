"""Tests for alembic/migration_retry.py transient-lock retry logic.

Verifies that ``run_migrations_with_retry`` retries on transient Postgres lock
errors (deadlock 40P01, lock_timeout 55P03) and surfaces non-transient errors
immediately, without touching a real database.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.migration_retry import (
    MAX_MIGRATION_ATTEMPTS,
    is_transient_lock_error,
    run_migrations_with_retry,
)


# ---------------------------------------------------------------------------
# Unit tests for the error classifier
# ---------------------------------------------------------------------------


def test_is_transient_lock_error_deadlock():
    exc = RuntimeError("asyncpg.exceptions.DeadlockDetectedError: deadlock detected")
    assert is_transient_lock_error(exc) is True


def test_is_transient_lock_error_lock_not_available():
    exc = RuntimeError(
        "asyncpg.exceptions.LockNotAvailableError: canceling statement due to lock timeout"
    )
    assert is_transient_lock_error(exc) is True


def test_is_transient_lock_error_sqlstate_40p01():
    exc = RuntimeError("ERROR 40P01: deadlock cycle detected")
    assert is_transient_lock_error(exc) is True


def test_is_transient_lock_error_sqlstate_55p03():
    exc = RuntimeError("ERROR 55P03 lock not available")
    assert is_transient_lock_error(exc) is True


def test_is_transient_lock_error_lock_timeout_substring():
    exc = RuntimeError("canceling statement due to lock_timeout")
    assert is_transient_lock_error(exc) is True


def test_is_transient_lock_error_non_transient():
    exc = RuntimeError("syntax error at or near 'ALTER'")
    assert is_transient_lock_error(exc) is False


def test_is_transient_lock_error_integrity():
    exc = RuntimeError("IntegrityError: duplicate key value violates unique constraint")
    assert is_transient_lock_error(exc) is False


# ---------------------------------------------------------------------------
# Integration-style tests for run_migrations_with_retry
# ---------------------------------------------------------------------------


def _make_engine_mock(run_sync_side_effects):
    """Build a mock async engine whose run_sync raises the given sequence.

    None in the sequence means success on that attempt; an exception means
    the run_sync call raises it. execute() (for SET lock_timeout) always
    succeeds.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    effects = iter(run_sync_side_effects)

    async def run_sync_side_effect(_fn):
        effect = next(effects)
        if effect is not None:
            raise effect

    conn.run_sync.side_effect = run_sync_side_effect

    engine = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    engine.connect = MagicMock(return_value=cm)
    return engine, conn


@pytest.mark.asyncio
async def test_retry_on_transient_deadlock_then_success():
    """First attempt raises DeadlockDetected; second attempt succeeds."""
    deadlock = RuntimeError(
        "asyncpg.exceptions.DeadlockDetectedError: deadlock detected"
    )
    engine, _ = _make_engine_mock([deadlock, None])

    with patch(
        "app.migration_retry.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await run_migrations_with_retry(engine, MagicMock())

    assert engine.connect.call_count == 2
    assert mock_sleep.call_count == 1


@pytest.mark.asyncio
async def test_retry_on_lock_timeout_then_success():
    """First attempt raises lock_timeout (55P03); second attempt succeeds."""
    lock_err = RuntimeError("canceling statement due to lock timeout 55P03")
    engine, _ = _make_engine_mock([lock_err, None])

    with patch(
        "app.migration_retry.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await run_migrations_with_retry(engine, MagicMock())

    assert engine.connect.call_count == 2
    assert mock_sleep.call_count == 1


@pytest.mark.asyncio
async def test_non_transient_error_surfaces_immediately():
    """A non-transient error (e.g. syntax error) is NOT retried."""
    bad_sql = RuntimeError("ProgrammingError: column does not exist")
    engine, _ = _make_engine_mock([bad_sql])

    with (
        patch(
            "app.migration_retry.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
        pytest.raises(RuntimeError, match="column does not exist"),
    ):
        await run_migrations_with_retry(engine, MagicMock())

    assert engine.connect.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_budget_raises_runtime_error():
    """Persistent transient errors exhaust the budget and raise RuntimeError."""
    deadlock = RuntimeError(
        "asyncpg.exceptions.DeadlockDetectedError: deadlock detected"
    )
    engine, _ = _make_engine_mock([deadlock] * MAX_MIGRATION_ATTEMPTS)

    with (
        patch(
            "app.migration_retry.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
        pytest.raises(RuntimeError, match="transient lock errors"),
    ):
        await run_migrations_with_retry(engine, MagicMock())

    assert engine.connect.call_count == MAX_MIGRATION_ATTEMPTS
    assert mock_sleep.call_count == MAX_MIGRATION_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_lock_timeout_set_on_each_connection():
    """lock_timeout is SET on every connection attempt, including retries."""
    lock_err = RuntimeError("lock timeout 55P03")
    engine, conn = _make_engine_mock([lock_err, None])

    execute_stmts = []
    original_execute = conn.execute.side_effect

    async def tracking_execute(stmt):
        execute_stmts.append(str(stmt))
        if original_execute is not None:
            return await original_execute(stmt)

    conn.execute.side_effect = tracking_execute

    with patch("app.migration_retry.asyncio.sleep", new_callable=AsyncMock):
        await run_migrations_with_retry(engine, MagicMock())

    lock_timeout_calls = [s for s in execute_stmts if "lock_timeout" in s.lower()]
    assert len(lock_timeout_calls) == 2


@pytest.mark.asyncio
async def test_success_on_first_attempt_no_sleep():
    """A clean first attempt completes without any sleep calls."""
    engine, _ = _make_engine_mock([None])

    with patch(
        "app.migration_retry.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await run_migrations_with_retry(engine, MagicMock())

    assert engine.connect.call_count == 1
    mock_sleep.assert_not_called()
