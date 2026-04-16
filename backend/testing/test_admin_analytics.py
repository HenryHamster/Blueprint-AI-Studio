"""Tests for admin_analytics recent-orchestrator-errors endpoint."""
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.auth import get_current_user, require_admin
from app.routers import admin_analytics
from app import shared_state
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
    admin_analytics._orchestrator_errors_cache.clear()
    yield
    admin_analytics._orchestrator_errors_cache.clear()


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
        with patch.object(admin_analytics, "_fetch_orchestrator_error_entries", return_value=entries):
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

        with patch.object(admin_analytics, "_fetch_orchestrator_error_entries", side_effect=_raise):
            resp = await client.get("/api/admin/recent-orchestrator-errors")

        assert resp.status_code == 200
        body = resp.json()
        assert body["buckets"] == []
        assert "error" in body
        assert "permission denied" in body["error"]

    @pytest.mark.asyncio
    async def test_zero_entries_returns_empty_buckets(self, client: AsyncClient):
        """No log entries yields an empty buckets array with no error field."""
        with patch.object(admin_analytics, "_fetch_orchestrator_error_entries", return_value=[]):
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

        with patch.object(admin_analytics, "_fetch_orchestrator_error_entries", side_effect=_fake):
            resp1 = await client.get("/api/admin/recent-orchestrator-errors?hours=1&limit=20")
            resp2 = await client.get("/api/admin/recent-orchestrator-errors?hours=1&limit=20")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert call_count["n"] == 1


@pytest.fixture
def _reset_task_results():
    prev_results = shared_state.deploy_monitoring["task_results"]
    prev_adj = dict(shared_state.anomaly_adjustments)
    shared_state.deploy_monitoring["task_results"] = deque(maxlen=100)
    yield
    shared_state.deploy_monitoring["task_results"] = prev_results
    shared_state.anomaly_adjustments.clear()
    shared_state.anomaly_adjustments.update(prev_adj)


class TestFailingTaskTypes:
    @pytest.mark.asyncio
    async def test_returns_gated_type_with_rate_and_samples(self, client: AsyncClient, _reset_task_results):
        """Type with >=70% failure rate and >=3 samples appears in gated_types."""
        now = _utcnow()
        for i in range(5):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "create")
            )
        shared_state.deploy_monitoring["task_results"].append(
            ((now - timedelta(minutes=6)).isoformat(), "success", None, "create")
        )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()

        gated = body["gated_types"]
        assert len(gated) == 1
        entry = gated[0]
        assert entry["task_type"] == "create"
        assert entry["failure_rate"] >= 0.7
        assert entry["sample_size"] == 6
        assert entry["threshold"] == 0.7

    @pytest.mark.asyncio
    async def test_no_gated_types_returns_empty_list(self, client: AsyncClient, _reset_task_results):
        """All-success recent results produce an empty gated_types list."""
        now = _utcnow()
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "success", None, "create")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["gated_types"] == []

    @pytest.mark.asyncio
    async def test_insufficient_samples_not_gated(self, client: AsyncClient, _reset_task_results):
        """A type with only 2 samples (below n=3 floor) is not reported as gated."""
        now = _utcnow()
        for i in range(2):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "fix")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        assert resp.json()["gated_types"] == []

    @pytest.mark.asyncio
    async def test_reports_anomaly_tightened_threshold(self, client: AsyncClient, _reset_task_results):
        """When anomaly_adjustments.active is True the response reflects the tightened threshold."""
        shared_state.anomaly_adjustments["active"] = True
        shared_state.anomaly_adjustments["failure_gate_threshold"] = 0.6

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["anomaly_tightened"] is True
        assert body["global_failure_threshold"] == 0.6

    @pytest.mark.asyncio
    async def test_healthy_type_excluded_from_gated(self, client: AsyncClient, _reset_task_results):
        """When both a failing and a healthy type exist, only the failing one is gated."""
        now = _utcnow()
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "create")
            )
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "success", None, "review")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        types_gated = [e["task_type"] for e in resp.json()["gated_types"]]
        assert "create" in types_gated
        assert "review" not in types_gated
