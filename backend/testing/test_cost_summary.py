"""Tests for GET /api/admin/cost-summary endpoint."""

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import TaskStatus
from app.time_utils import _utcnow
from testing.factories import make_project, make_task

# Rates mirror admin_project_config.py constants
_ON_DEMAND_RATE = 0.067 / 3600  # per second
_SPOT_RATE = 0.020 / 3600


class TestCostSummary:
    @pytest.mark.asyncio
    async def test_empty_returns_empty_summary(self, client: AsyncClient):
        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_task_type"] == {}
        assert body["total_estimated_cost"] == 0.0
        assert body["period_days"] == 7

    @pytest.mark.asyncio
    async def test_custom_days_param_reflected(self, client: AsyncClient):
        resp = await client.get("/api/admin/cost-summary?days=30")
        assert resp.status_code == 200
        assert resp.json()["period_days"] == 30

    @pytest.mark.asyncio
    async def test_tasks_without_vm_times_excluded(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=False,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_task_type"] == {}
        assert body["total_estimated_cost"] == 0.0

    @pytest.mark.asyncio
    async def test_tasks_outside_window_excluded(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        old_time = now - timedelta(days=10)
        prov = old_time
        stop = old_time + timedelta(seconds=3600)

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=False,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
            created_at=old_time,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary?days=7")
        assert resp.status_code == 200
        assert resp.json()["by_task_type"] == {}

    @pytest.mark.asyncio
    async def test_on_demand_cost_calculated_correctly(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=3600)

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=False,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert "gcp:on-demand" in body["by_task_type"]
        row = body["by_task_type"]["gcp:on-demand"]
        assert row["count"] == 1
        expected_cost = 3600 * _ON_DEMAND_RATE
        assert abs(row["estimated_cost"] - expected_cost) < 0.0001
        assert abs(body["total_estimated_cost"] - expected_cost) < 0.0001
        assert row["provider"] == "gcp"
        assert row["spot"] is False

    @pytest.mark.asyncio
    async def test_spot_cost_uses_spot_rate(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=3600)

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=True,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert "gcp:spot" in body["by_task_type"]
        row = body["by_task_type"]["gcp:spot"]
        expected_cost = 3600 * _SPOT_RATE
        assert abs(row["estimated_cost"] - expected_cost) < 0.0001
        assert row["spot"] is True

    @pytest.mark.asyncio
    async def test_spot_rate_lower_than_on_demand(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=3600)

        t_od = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=False,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        t_spot = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=True,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        db_session.add(t_od)
        db_session.add(t_spot)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        od_cost = body["by_task_type"]["gcp:on-demand"]["estimated_cost"]
        spot_cost = body["by_task_type"]["gcp:spot"]["estimated_cost"]
        assert spot_cost < od_cost

    @pytest.mark.asyncio
    async def test_null_provider_shown_as_unknown(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=1800)

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_spot=False,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert "unknown:on-demand" in body["by_task_type"]
        assert body["by_task_type"]["unknown:on-demand"]["provider"] is None

    @pytest.mark.asyncio
    async def test_multiple_tasks_aggregated_per_group(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=3600)

        for _ in range(3):
            t = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                provider="gcp",
                vm_spot=True,
                vm_provisioned_at=prov,
                vm_stopped_at=stop,
            )
            db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        row = body["by_task_type"]["gcp:spot"]
        assert row["count"] == 3
        assert abs(row["total_seconds"] - 3 * 3600) < 1
        expected_cost = 3 * 3600 * _SPOT_RATE
        assert abs(row["estimated_cost"] - expected_cost) < 0.0001

    @pytest.mark.asyncio
    async def test_total_cost_is_sum_across_groups(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        now = _utcnow()
        prov = now - timedelta(hours=2)
        stop = prov + timedelta(seconds=3600)

        t_od = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=False,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        t_spot = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            provider="gcp",
            vm_spot=True,
            vm_provisioned_at=prov,
            vm_stopped_at=stop,
        )
        db_session.add(t_od)
        db_session.add(t_spot)
        await db_session.commit()

        resp = await client.get("/api/admin/cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        expected_total = 3600 * _ON_DEMAND_RATE + 3600 * _SPOT_RATE
        assert abs(body["total_estimated_cost"] - expected_total) < 0.0001
