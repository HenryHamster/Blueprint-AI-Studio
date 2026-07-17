"""Tests for admin_notifications.py — audience-aware alert filtering."""

import pytest
from httpx import AsyncClient

from app.models import Alert, Project, ProjectMember


TEST_USER_ID = "test-user-123"


async def _make_project(session, owner_id=TEST_USER_ID):
    project = Project(name="Notif Test Project", user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    session.add(member)
    await session.commit()
    return project


class TestGetAlertsAudienceFilter:
    @pytest.mark.asyncio
    async def test_get_alerts_excludes_strategist_internal_event_types(
        self, client: AsyncClient, db_session
    ):
        """get_alerts must drop strategist-internal alerts and retain operator ones.

        warm_pool.payoff is STRATEGIST_INTERNAL; runner.deactivated is
        OPERATOR_ACTION_NEEDED.  Only the latter should appear in the response.
        """
        project = await _make_project(db_session)

        db_session.add(
            Alert(
                project_id=project.id,
                severity="INFO",
                title="Internal Payoff Alert",
                message="strategist only",
                event_type="warm_pool.payoff",
            )
        )
        db_session.add(
            Alert(
                project_id=project.id,
                severity="WARNING",
                title="Runner Deactivated Alert",
                message="operator should see this",
                event_type="runner.deactivated",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        data = resp.json()
        titles = [a["title"] for a in data]

        assert "Runner Deactivated Alert" in titles
        assert "Internal Payoff Alert" not in titles

    @pytest.mark.asyncio
    async def test_get_alerts_retains_null_event_type(
        self, client: AsyncClient, db_session
    ):
        """Legacy alerts with no event_type must still be returned (fail-open for unknown)."""
        project = await _make_project(db_session)

        db_session.add(
            Alert(
                project_id=project.id,
                severity="ERROR",
                title="Legacy Alert",
                message="no event type",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        titles = [a["title"] for a in resp.json()]
        assert "Legacy Alert" in titles
