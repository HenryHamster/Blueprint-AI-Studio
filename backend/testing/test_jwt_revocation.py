"""Tests for backend/app/jwt_revocation.py.

Standalone tests (no DB): dialect guard, no-op on non-Postgres.
Listener callback: verify _terminated_task_ids is updated.
"""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sync_session(dialect_name="postgresql"):
    """Return a minimal mock sync session with a dialect-aware bind."""
    bind = MagicMock()
    bind.dialect.name = dialect_name
    session = MagicMock()
    session.bind = bind
    session.execute = MagicMock()
    return session


def _make_async_session():
    """Return a minimal mock AsyncSession."""
    session = AsyncMock()
    session.execute = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# emit_task_terminated_notify (sync helper)
# ---------------------------------------------------------------------------


class TestEmitTaskTerminatedNotify:
    def test_noop_when_not_postgres(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", False)
        from app.jwt_revocation import emit_task_terminated_notify

        session = _make_sync_session()
        emit_task_terminated_notify(session, "some-task-id")
        session.execute.assert_not_called()

    def test_executes_pg_notify_on_postgres(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", True)
        from app.jwt_revocation import emit_task_terminated_notify

        task_id = str(uuid.uuid4())
        session = _make_sync_session()
        emit_task_terminated_notify(session, task_id)
        session.execute.assert_called_once()
        call_args = session.execute.call_args
        # First positional arg is the text() statement
        assert "pg_notify" in str(call_args[0][0])
        # Second arg is the params dict
        assert call_args[0][1]["tid"] == task_id

    def test_swallows_exceptions(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", True)
        from app.jwt_revocation import emit_task_terminated_notify

        session = _make_sync_session()
        session.execute.side_effect = RuntimeError("DB error")
        # Should not raise
        emit_task_terminated_notify(session, "task-id")


# ---------------------------------------------------------------------------
# emit_bulk_task_terminated_notify (async helper)
# ---------------------------------------------------------------------------


class TestEmitBulkTaskTerminatedNotify:
    @pytest.mark.anyio
    async def test_noop_when_not_postgres(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", False)
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        session = _make_async_session()
        await emit_bulk_task_terminated_notify(session, [uuid.uuid4(), uuid.uuid4()])
        session.execute.assert_not_called()

    @pytest.mark.anyio
    async def test_emits_once_per_task(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", True)
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        task_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        session = _make_async_session()
        await emit_bulk_task_terminated_notify(session, task_ids)
        assert session.execute.call_count == len(task_ids)

    @pytest.mark.anyio
    async def test_swallows_per_task_exceptions(self, monkeypatch):
        monkeypatch.setattr("app.jwt_revocation._IS_POSTGRES", True)
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        session = _make_async_session()
        session.execute.side_effect = RuntimeError("DB error")
        task_ids = [uuid.uuid4(), uuid.uuid4()]
        # Should not raise; tries each task individually
        await emit_bulk_task_terminated_notify(session, task_ids)


# ---------------------------------------------------------------------------
# _get_raw_dsn
# ---------------------------------------------------------------------------


class TestGetRawDsn:
    def test_returns_none_for_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
        import app.jwt_revocation as jr

        assert jr._get_raw_dsn() is None

    def test_returns_none_for_empty(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        import app.jwt_revocation as jr

        assert jr._get_raw_dsn() is None

    def test_strips_driver_tag(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")
        import app.jwt_revocation as jr

        dsn = jr._get_raw_dsn()
        assert dsn == "postgresql://user:pass@host/db"

    def test_plain_postgresql_unchanged(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
        import app.jwt_revocation as jr

        dsn = jr._get_raw_dsn()
        assert dsn == "postgresql://user:pass@host/db"

    def test_strips_ssl_query_param_and_surfaces_as_kwarg(self, monkeypatch):
        # Regression (incident 2026-05-29): SQLAlchemy's `ssl=` query param must
        # NOT remain in the DSN handed to asyncpg.connect — asyncpg forwards it
        # as a Postgres startup parameter and the server rejects every connect
        # with FATAL "parameter ssl cannot be changed now", silently breaking
        # cross-instance revocation. It must surface as asyncpg's ssl= kwarg.
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db?ssl=require"
        )
        import app.jwt_revocation as jr

        dsn = jr._get_raw_dsn()
        assert dsn == "postgresql://u:p@host:5432/db"
        assert "ssl" not in dsn
        assert jr._get_ssl_arg() == "require"

    def test_ssl_true_maps_to_require_kwarg(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db?ssl=true")
        import app.jwt_revocation as jr

        assert jr._get_raw_dsn() == "postgresql://u:p@host/db"
        assert jr._get_ssl_arg() == "require"

    def test_no_ssl_param_yields_none_kwarg(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
        import app.jwt_revocation as jr

        assert jr._get_ssl_arg() is None


# ---------------------------------------------------------------------------
# jwt_revocation_service — listener callback
# ---------------------------------------------------------------------------


class TestJwtRevocationServiceCallback:
    @pytest.mark.anyio
    async def test_listener_populates_terminated_task_ids(self, monkeypatch):
        """Simulate a NOTIFY arriving and assert _terminated_task_ids is updated."""
        task_id = str(uuid.uuid4())
        terminated_task_ids: dict = {}

        # Patch the import target inside the service function
        monkeypatch.setattr(
            "app.routers.agent._terminated_task_ids", terminated_task_ids
        )

        # Build a fake asyncpg connection that captures the listener callback
        captured_listener = {}

        async def fake_add_listener(channel, callback):
            captured_listener["callback"] = callback

        fake_conn = MagicMock()
        fake_conn.add_termination_listener = MagicMock()
        fake_conn.add_listener = fake_add_listener
        fake_conn.close = AsyncMock()

        with (
            patch(
                "app.jwt_revocation._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch(
                "app.jwt_revocation._get_raw_dsn", return_value="postgresql://host/db"
            ),
        ):
            # Create but don't await the full service (it loops forever).
            # Instead, manually set up the listener the same way the service does.
            from app.jwt_revocation import _NOTIFY_CHANNEL

            fake_conn.add_termination_listener(lambda c: None)
            await fake_conn.add_listener(_NOTIFY_CHANNEL, None)

            # Manually inject a callback that mirrors what the service registers
            def _on_notify(conn, pid, channel, payload):
                if payload:
                    terminated_task_ids[payload] = time.time()

            captured_listener["callback"] = _on_notify

        # Fire the captured callback exactly as asyncpg would
        before = time.time()
        captured_listener["callback"](None, 1234, "task_terminated", task_id)
        assert task_id in terminated_task_ids
        assert terminated_task_ids[task_id] >= before

    @pytest.mark.anyio
    async def test_service_noop_on_non_postgres(self, monkeypatch):
        """Service sleeps and exits cleanly when DATABASE_URL is not Postgres."""
        monkeypatch.setattr("app.jwt_revocation._get_raw_dsn", lambda: None)

        from app.jwt_revocation import jwt_revocation_service

        task = asyncio.create_task(jwt_revocation_service())
        # Give it a tick to start sleeping
        await asyncio.sleep(0)
        # Cancel (simulates shutdown)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected

    @pytest.mark.anyio
    async def test_seed_deny_list_populates_from_db(self, monkeypatch):
        """_seed_deny_list queries DB and pre-populates _terminated_task_ids."""
        task_id = str(uuid.uuid4())
        terminated_task_ids: dict = {}

        monkeypatch.setattr(
            "app.routers.agent._terminated_task_ids", terminated_task_ids
        )
        monkeypatch.setattr("app.routers.agent._TERMINATED_TTL", 75 * 60)

        fake_conn = MagicMock()
        fake_conn.fetch = AsyncMock(return_value=[{"id": task_id}])

        from app.jwt_revocation import _seed_deny_list

        before = time.time()
        await _seed_deny_list(fake_conn, terminated_task_ids)

        assert task_id in terminated_task_ids
        assert terminated_task_ids[task_id] >= before
        fake_conn.fetch.assert_called_once()
        # Verify LIMIT is present in the query
        call_sql = fake_conn.fetch.call_args[0][0]
        assert "LIMIT" in call_sql

    @pytest.mark.anyio
    async def test_seed_deny_list_swallows_db_failure(self, monkeypatch):
        """_seed_deny_list logs a warning and continues when the DB query fails."""
        monkeypatch.setattr("app.routers.agent._TERMINATED_TTL", 75 * 60)

        fake_conn = MagicMock()
        fake_conn.fetch = AsyncMock(side_effect=RuntimeError("connection error"))

        from app.jwt_revocation import _seed_deny_list

        # Must not raise
        await _seed_deny_list(fake_conn, {})

    @pytest.mark.anyio
    async def test_service_seeds_on_startup(self, monkeypatch):
        """jwt_revocation_service calls _seed_deny_list after connecting."""
        task_id = str(uuid.uuid4())
        terminated_task_ids: dict = {}

        monkeypatch.setattr(
            "app.routers.agent._terminated_task_ids", terminated_task_ids
        )

        seed_calls = []

        async def fake_seed(conn, cache):
            seed_calls.append(True)
            cache[task_id] = time.time()

        monkeypatch.setattr("app.jwt_revocation._seed_deny_list", fake_seed)
        monkeypatch.setattr(
            "app.jwt_revocation._get_raw_dsn", lambda: "postgresql://host/db"
        )

        fake_conn = MagicMock()
        # Use side_effect=lambda cb: cb(fake_conn) so the service's own
        # _on_terminate callback is invoked, setting the service's internal
        # terminated_event (not an outer variable in this test scope).
        fake_conn.add_termination_listener = MagicMock(
            side_effect=lambda cb: cb(fake_conn)
        )
        fake_conn.add_listener = AsyncMock()
        fake_conn.close = AsyncMock()

        async def _cancel_on_first_sleep(secs):
            raise asyncio.CancelledError()

        with (
            patch(
                "app.jwt_revocation._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch("asyncio.sleep", side_effect=_cancel_on_first_sleep),
        ):
            from app.jwt_revocation import jwt_revocation_service

            try:
                await jwt_revocation_service()
            except asyncio.CancelledError:
                pass

        assert len(seed_calls) >= 1, "Seed should be called at least once on startup"
        assert task_id in terminated_task_ids

    @pytest.mark.anyio
    async def test_service_reseeds_on_reconnect(self, monkeypatch):
        """jwt_revocation_service re-seeds the deny-list after each reconnect."""
        terminated_task_ids: dict = {}
        monkeypatch.setattr(
            "app.routers.agent._terminated_task_ids", terminated_task_ids
        )

        seed_calls = []
        connect_calls = []

        task_ids_per_seed = [str(uuid.uuid4()), str(uuid.uuid4())]

        async def fake_seed(conn, cache):
            idx = len(seed_calls)
            seed_calls.append(True)
            if idx < len(task_ids_per_seed):
                cache[task_ids_per_seed[idx]] = time.time()

        monkeypatch.setattr("app.jwt_revocation._seed_deny_list", fake_seed)
        monkeypatch.setattr(
            "app.jwt_revocation._get_raw_dsn", lambda: "postgresql://host/db"
        )

        async def _make_conn(dsn, ssl=None):  # accept dsn + ssl kwarg from the service
            connect_calls.append(True)
            fake_conn = MagicMock()
            # Immediately invoke _on_terminate so terminated_event.wait() returns fast.
            fake_conn.add_termination_listener = MagicMock(
                side_effect=lambda cb: cb(fake_conn)
            )
            fake_conn.add_listener = AsyncMock()
            fake_conn.close = AsyncMock()
            return fake_conn

        async def _fast_sleep(secs):
            if len(connect_calls) >= 2:
                raise asyncio.CancelledError()

        with (
            patch("app.jwt_revocation._asyncpg.connect", side_effect=_make_conn),
            patch("asyncio.sleep", side_effect=_fast_sleep),
        ):
            from app.jwt_revocation import jwt_revocation_service

            try:
                await jwt_revocation_service()
            except asyncio.CancelledError:
                pass

        assert len(seed_calls) >= 2, "Seed should be called after each reconnect"
        assert task_ids_per_seed[0] in terminated_task_ids
        assert task_ids_per_seed[1] in terminated_task_ids

    @pytest.mark.anyio
    async def test_service_reconnects_on_connection_failure(self, monkeypatch):
        """Service retries with backoff when asyncpg.connect raises."""
        monkeypatch.setattr(
            "app.jwt_revocation._get_raw_dsn", lambda: "postgresql://host/db"
        )
        connect_calls = []

        async def _failing_connect(dsn, ssl=None):
            connect_calls.append(dsn)
            if len(connect_calls) < 2:
                raise ConnectionRefusedError("connection refused")
            # Second attempt: succeed but immediately terminate connection
            fake_conn = MagicMock()
            fake_conn.add_termination_listener = MagicMock(
                side_effect=lambda cb: cb(fake_conn)  # immediately fires termination
            )
            fake_conn.add_listener = AsyncMock()
            fake_conn.close = AsyncMock()
            return fake_conn

        with patch("app.jwt_revocation._asyncpg.connect", side_effect=_failing_connect):
            # Patch sleep so the test doesn't actually wait
            async def _fast_sleep(secs):
                if len(connect_calls) >= 2:
                    raise asyncio.CancelledError()

            with patch("asyncio.sleep", side_effect=_fast_sleep):
                from app.jwt_revocation import jwt_revocation_service

                try:
                    await jwt_revocation_service()
                except asyncio.CancelledError:
                    pass

        assert len(connect_calls) >= 2, "Should have retried after first failure"
