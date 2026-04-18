"""Tests for admin_orchestrator_errors recent-orchestrator-errors endpoint."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.auth import get_current_user, require_admin
from app.routers import admin_orchestrator_errors
from app.time_utils import _utcnow


def _make_entry(payload: str, when: datetime):
    return SimpleNamespace(payload=payload, timestamp=when)


ATTR_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/app/routers/agent.py", line 123, in claim_task\n'
    "    ts = task.updated_at.isoformat()\n"
    "AttributeError: 'Task' object has no attribute 'updated_at'"
)

LOCK_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/app/rollups.py", line 456, in _check_and_trigger_rollup\n'
    "    await session.execute(stmt)\n"
    "asyncpg.exceptions.LockNotAvailableError: could not obtain lock on row"
)


@pytest.fixture(autouse=True)
def _clear_errors_cache():
    admin_orchestrator_errors._orchestrator_errors_cache.clear()
    yield
    admin_orchestrator_errors._orchestrator_errors_cache.clear()


class TestRecentOrchestratorErrors:
    @pytest.mark.asyncio
    async def test_buckets_entries_by_exception_class_and_source(self, client: AsyncClient):
        """Entries from two exception classes bucket correctly with counts."""
        now = _utcnow()
        entries = [
            _make_entry(ATTR_TRACEBACK, now),
            _make_entry(ATTR_TRACEBACK, now - timedelta(minutes=3)),
            _make_entry(LOCK_TRACEBACK, now - timedelta(minutes=1)),
        ]
        with patch.object(admin_orchestrator_errors, "_fetch_orchestrator_error_entries", return_value=entries):
            resp = await client.get("/api/admin/recent-orchestrator-errors?hours=1&limit=20")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("error") is None
        assert body["total_entries_scanned"] == 3
        buckets = body["buckets"]
        assert len(buckets) == 2

        by_class = {b["exception_class"]: b for b in buckets}
        assert "AttributeError" in by_class
        assert "asyncpg.exceptions.LockNotAvailableError" in by_class
        assert by_class["AttributeError"]["count"] == 2
        assert by_class["asyncpg.exceptions.LockNotAvailableError"]["count"] == 1
        assert "agent.py" in by_class["AttributeError"]["source"]
        assert "123" in by_class["AttributeError"]["source"]

    @pytest.mark.asyncio
    async def test_logging_client_failure_returns_error_field(self, client: AsyncClient):
        """Cloud Logging exceptions produce a graceful error response, not HTTP 500."""
        def _raise(_hours: int):
            raise RuntimeError("permission denied on logs")

        with patch.object(admin_orchestrator_errors, "_fetch_orchestrator_error_entries", side_effect=_raise):
            resp = await client.get("/api/admin/recent-orchestrator-errors")

        assert resp.status_code == 200
        body = resp.json()
        assert body["buckets"] == []
        assert "error" in body
        assert "permission denied" in body["error"]

    @pytest.mark.asyncio
    async def test_zero_entries_returns_empty_buckets(self, client: AsyncClient):
        """No log entries yields an empty buckets array with no error field."""
        with patch.object(admin_orchestrator_errors, "_fetch_orchestrator_error_entries", return_value=[]):
            resp = await client.get("/api/admin/recent-orchestrator-errors")

        assert resp.status_code == 200
        body = resp.json()
        assert body["buckets"] == []
        assert body["total_entries_scanned"] == 0
        assert body.get("error") is None or "error" not in body

    @pytest.mark.asyncio
    async def test_non_admin_returns_403(self, override_get_session):
        """Non-admin callers are rejected by require_admin."""
        async def non_admin():
            return {"user_id": "u2", "email": "u2@example.com", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[get_current_user] = non_admin
        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/recent-orchestrator-errors")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_fetch(self, client: AsyncClient):
        """Second call within TTL returns cached payload without re-querying."""
        now = _utcnow()
        entries = [_make_entry(ATTR_TRACEBACK, now)]
        call_count = {"n": 0}

        def _fake(_hours: int):
            call_count["n"] += 1
            return entries

        with patch.object(admin_orchestrator_errors, "_fetch_orchestrator_error_entries", side_effect=_fake):
            resp1 = await client.get("/api/admin/recent-orchestrator-errors?hours=1&limit=20")
            resp2 = await client.get("/api/admin/recent-orchestrator-errors?hours=1&limit=20")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert call_count["n"] == 1
