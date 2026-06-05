"""Integration tests for per-project live-promotion enablement.

Covers the deliverables of the per-project promotion override:
  * the POST /api/admin/projects/{id}/promotion-enabled control arms, holds, and
    clears a single project's override without touching the fleet default;
  * access is owner-gated (non-member → 404);
  * the GET /version-lifecycle surface reports the effective state + its source;
  * the override is honored by ``resolve_project_promotion_enabled`` (the value
    the qualifier promotion gate consults) and absent projects fall back to the
    fleet-wide ``promotion_enabled``.
"""

import pytest
from httpx import AsyncClient

from app.models import Project
from app.shared_state import (
    resolve_project_promotion_enabled,
    runtime_config as _runtime_config,
)
from testing.factories import make_membership


@pytest.fixture
async def project_owner(db_session):
    """A project with test-user-123 (the client fixture's user) as owner."""
    project = Project(name="Promo Ctl", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
async def foreign_project(db_session):
    """A project test-user-123 is NOT a member of (access must be denied)."""
    project = Project(name="Someone Else", user_id="other-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_promotion_enabled_denied_for_non_member(
    client: AsyncClient, db_session, foreign_project
):
    resp = await client.post(
        f"/api/admin/projects/{foreign_project.id}/promotion-enabled",
        json={"enabled": True},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_arm_hold_and_clear_project_override(
    client: AsyncClient, db_session, project_owner
):
    """Arming/holding stores an explicit override the gate consults; clearing
    restores fleet-default behavior without mutating other projects."""
    _runtime_config["promotion_enabled"] = False
    _runtime_config["promotion_enabled_by_project"] = {}
    sid = str(project_owner.id)

    # Arm — override present, effective True even though the fleet default is off.
    resp = await client.post(
        f"/api/admin/projects/{sid}/promotion-enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["override"] is True
    assert body["effective"] is True
    assert body["source"] == "project"
    assert resolve_project_promotion_enabled(project_owner.id) is True
    # A different project still rides the fleet default.
    assert resolve_project_promotion_enabled("p-other") is False

    # Hold — explicit False override holds even if the fleet later goes live.
    resp = await client.post(
        f"/api/admin/projects/{sid}/promotion-enabled", json={"enabled": False}
    )
    assert resp.json()["override"] is False
    _runtime_config["promotion_enabled"] = True
    assert resolve_project_promotion_enabled(project_owner.id) is False

    # Clear — override removed, project falls back to the (now live) fleet default.
    resp = await client.post(
        f"/api/admin/projects/{sid}/promotion-enabled", json={"enabled": None}
    )
    body = resp.json()
    assert body["override"] is None
    assert body["cleared"] is True
    assert body["source"] == "fleet"
    assert resolve_project_promotion_enabled(project_owner.id) is True


@pytest.mark.asyncio
async def test_version_lifecycle_surfaces_effective_state(
    client: AsyncClient, db_session, project_owner
):
    """The lifecycle surface reports the effective promotion state + source."""
    _runtime_config["promotion_enabled"] = False
    _runtime_config["promotion_enabled_by_project"] = {str(project_owner.id): True}
    resp = await client.get(
        f"/api/admin/projects/{project_owner.id}/version-lifecycle"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promotion_enabled_effective"] is True
    assert body["promotion_enabled_override"] is True
    assert body["promotion_enabled_source"] == "project"
