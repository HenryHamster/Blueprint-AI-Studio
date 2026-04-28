"""Tests for the GET /api/admin/provider-cost-summary endpoint."""
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount, TaskStatus
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


class TestProviderCostSummary:
    @pytest.mark.asyncio
    async def test_empty_returns_empty_rows(self, client: AsyncClient):
        resp = await client.get("/api/admin/provider-cost-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert body["window_hours"] == 24

    @pytest.mark.asyncio
    async def test_aggregates_cost_and_success_by_account(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        acct_id = uuid.uuid4()
        acct = MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="Test Anthropic",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
        db_session.add(acct)

        now = _utcnow()
        prov_at = now - timedelta(hours=1, seconds=3600)
        stop_at = prov_at + timedelta(seconds=3600)

        t1 = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": str(acct_id), "pr_merged": True},
            vm_provisioned_at=prov_at,
            vm_stopped_at=stop_at,
            vm_spot=False,
        )
        t2 = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            context={"llm_account_id": str(acct_id)},
            vm_provisioned_at=prov_at,
            vm_stopped_at=stop_at,
            vm_spot=True,
        )
        db_session.add(t1)
        db_session.add(t2)
        await db_session.commit()

        resp = await client.get("/api/admin/provider-cost-summary?hours=48")
        assert resp.status_code == 200
        body = resp.json()
        assert body["window_hours"] == 48

        rows = body["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["account_name"] == "Test Anthropic"
        assert row["provider"] == "anthropic"
        assert row["account_id"] == str(acct_id)
        assert row["total_tasks"] == 2
        assert row["success_count"] == 1
        assert row["merged_count"] == 1
        assert row["success_rate"] == pytest.approx(0.5, abs=0.001)

        expected_cost = round(
            3600 * ON_DEMAND_RATE + 3600 * SPOT_RATE, 2
        )
        assert row["total_cost_usd"] == pytest.approx(expected_cost, abs=0.01)
        assert row["cost_per_merged_pr"] == pytest.approx(expected_cost, abs=0.01)

    @pytest.mark.asyncio
    async def test_tasks_outside_window_excluded(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        acct_id = uuid.uuid4()
        acct = MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="Old Account",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
        db_session.add(acct)

        old_time = _utcnow() - timedelta(hours=72)
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": str(acct_id)},
            created_at=old_time,
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/provider-cost-summary?hours=24")
        assert resp.status_code == 200
        assert resp.json()["rows"] == []

    @pytest.mark.asyncio
    async def test_project_id_filter(self, client: AsyncClient, db_session):
        project_a = make_project(name="Project A")
        project_b = make_project(name="Project B")
        db_session.add(project_a)
        db_session.add(project_b)
        await db_session.flush()

        acct_id = uuid.uuid4()
        acct = MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="Shared Account",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
        db_session.add(acct)

        ctx = {"llm_account_id": str(acct_id)}
        t_a = make_task(project_id=project_a.id, status=TaskStatus.COMPLETED, context=ctx)
        t_b = make_task(project_id=project_b.id, status=TaskStatus.COMPLETED, context=ctx)
        db_session.add(t_a)
        db_session.add(t_b)
        await db_session.commit()

        resp = await client.get(f"/api/admin/provider-cost-summary?project_id={project_a.id}")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["total_tasks"] == 1

    @pytest.mark.asyncio
    async def test_no_merged_pr_cost_per_merge_is_null(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        acct_id = uuid.uuid4()
        acct = MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="No Merges",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
        db_session.add(acct)

        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": str(acct_id)},
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/provider-cost-summary")
        assert resp.status_code == 200
        row = resp.json()["rows"][0]
        assert row["cost_per_merged_pr"] is None
        assert row["merged_count"] == 0

    @pytest.mark.asyncio
    async def test_unknown_account_grouped_as_untracked(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        unknown_id = str(uuid.uuid4())
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": unknown_id},
        )
        db_session.add(t)
        await db_session.commit()

        resp = await client.get("/api/admin/provider-cost-summary")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["account_name"] == "unknown"
        assert rows[0]["provider"] == "unknown"

    @pytest.mark.asyncio
    async def test_rows_sorted_by_spend_descending(self, client: AsyncClient, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.flush()

        acct_cheap = MaxPlanAccount(
            id=uuid.uuid4(), user_id="u", name="Cheap", provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH
        )
        acct_expensive = MaxPlanAccount(
            id=uuid.uuid4(), user_id="u", name="Expensive", provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH
        )
        db_session.add(acct_cheap)
        db_session.add(acct_expensive)

        now = _utcnow()
        prov = now - timedelta(hours=2)

        t_cheap = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": str(acct_cheap.id)},
            vm_provisioned_at=prov,
            vm_stopped_at=prov + timedelta(seconds=60),
            vm_spot=True,
        )
        t_expensive = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"llm_account_id": str(acct_expensive.id)},
            vm_provisioned_at=prov,
            vm_stopped_at=prov + timedelta(seconds=7200),
            vm_spot=False,
        )
        db_session.add(t_cheap)
        db_session.add(t_expensive)
        await db_session.commit()

        resp = await client.get("/api/admin/provider-cost-summary")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) == 2
        assert rows[0]["account_name"] == "Expensive"
        assert rows[1]["account_name"] == "Cheap"
        assert rows[0]["total_cost_usd"] >= rows[1]["total_cost_usd"]
