"""Tests for admin_analytics endpoints."""
from collections import deque
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app import shared_state
from app.time_utils import _utcnow


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
