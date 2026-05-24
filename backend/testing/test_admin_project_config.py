import pytest
from httpx import AsyncClient

from app.models import Project
from app.shared_state import (
    get_project_broadcast_limit,
    get_project_integration_branch,
    runtime_config,
)
from testing.factories import make_membership


@pytest.fixture
async def project_owner(db_session):
    """Create a project with test-user-123 as owner."""
    project = Project(name="Config Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_integration_branch_defaults_to_main(
    client: AsyncClient, db_session, project_owner
):
    """GET /integration-branch returns 'main' when no per-project value is set."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/integration-branch")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["integration_branch"] == "main"
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns the updated branch name."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/integration-branch",
        json={"integration_branch": "automain"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["integration_branch"] == "automain"
    assert data["is_default"] is False

    get_resp = await client.get(f"/api/admin/projects/{project.id}/integration-branch")
    assert get_resp.status_code == 200
    assert get_resp.json()["integration_branch"] == "automain"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_null_reverts_to_default(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null clears the per-project override, reverting to global default."""
    project = project_owner
    # Set a value first
    await client.patch(
        f"/api/admin/projects/{project.id}/integration-branch",
        json={"integration_branch": "dev"},
    )
    # Revert to default
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/integration-branch",
        json={"integration_branch": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_rejects_invalid_chars(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects branch names with shell metacharacters."""
    project = project_owner
    for bad in ['main";id;"', "main$(cmd)", "main\nfoo"]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": bad},
        )
        assert resp.status_code == 422, (
            f"expected 422 for {bad!r}, got {resp.status_code}"
        )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_two_projects_independent_integration_branches(
    client: AsyncClient, db_session
):
    """Two projects can each have different integration_branch values independently."""
    p1 = Project(name="Project A", user_id="test-user-123")
    p2 = Project(name="Project B", user_id="test-user-123")
    db_session.add(p1)
    db_session.add(p2)
    await db_session.commit()
    await db_session.refresh(p1)
    await db_session.refresh(p2)

    for project in (p1, p2):
        member = make_membership(project.id, user_id="test-user-123", role="owner")
        db_session.add(member)
    await db_session.commit()

    await client.patch(
        f"/api/admin/projects/{p1.id}/integration-branch",
        json={"integration_branch": "automain"},
    )
    await client.patch(
        f"/api/admin/projects/{p2.id}/integration-branch",
        json={"integration_branch": "main"},
    )

    r1 = await client.get(f"/api/admin/projects/{p1.id}/integration-branch")
    r2 = await client.get(f"/api/admin/projects/{p2.id}/integration-branch")

    assert r1.json()["integration_branch"] == "automain"
    assert r2.json()["integration_branch"] == "main"
    # They are independent — p1 override doesn't affect p2
    assert r1.json()["integration_branch"] != r2.json()["integration_branch"]


@pytest.mark.asyncio
async def test_get_project_integration_branch_helper_legacy_fallback():
    """The helper falls back to runtime_config when project has no integration_branch set."""
    original = runtime_config.get("integration_branch")
    try:
        runtime_config["integration_branch"] = "legacy-trunk"

        class _FakeProject:
            integration_branch = None

        result = get_project_integration_branch(_FakeProject())
        assert result == "legacy-trunk"
    finally:
        if original is None:
            runtime_config.pop("integration_branch", None)
        else:
            runtime_config["integration_branch"] = original


# ---------------------------------------------------------------------------
# Broadcast-limit config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_broadcast_limit_defaults_to_none(
    client: AsyncClient, db_session, project_owner
):
    """GET /broadcast-limit returns is_default=True and null when no per-project value is set."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/broadcast-limit")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["broadcast_limit"] is None
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_broadcast_limit_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns the updated broadcast limit."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/broadcast-limit",
        json={"broadcast_limit": 50},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["broadcast_limit"] == 50
    assert data["is_default"] is False

    get_resp = await client.get(f"/api/admin/projects/{project.id}/broadcast-limit")
    assert get_resp.status_code == 200
    assert get_resp.json()["broadcast_limit"] == 50
    assert get_resp.json()["is_default"] is False


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_broadcast_limit_null_reverts_to_default(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null clears the per-project override, reverting to global default."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/broadcast-limit",
        json={"broadcast_limit": 25},
    )
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/broadcast-limit",
        json={"broadcast_limit": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["broadcast_limit"] is None
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_broadcast_limit_rejects_invalid_values(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects non-integer and negative values."""
    project = project_owner
    for bad_body in [
        {"broadcast_limit": -1},
        {"broadcast_limit": "100"},
        {"broadcast_limit": True},
        {"broadcast_limit": 3.5},
    ]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/broadcast-limit",
            json=bad_body,
        )
        assert resp.status_code == 422, (
            f"expected 422 for {bad_body!r}, got {resp.status_code}"
        )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_two_projects_independent_broadcast_limits(
    client: AsyncClient, db_session
):
    """Two projects can each have different broadcast_limit values independently."""
    p1 = Project(name="BroadcastA", user_id="test-user-123")
    p2 = Project(name="BroadcastB", user_id="test-user-123")
    db_session.add(p1)
    db_session.add(p2)
    await db_session.commit()
    await db_session.refresh(p1)
    await db_session.refresh(p2)

    for project in (p1, p2):
        member = make_membership(project.id, user_id="test-user-123", role="owner")
        db_session.add(member)
    await db_session.commit()

    await client.patch(
        f"/api/admin/projects/{p1.id}/broadcast-limit",
        json={"broadcast_limit": 10},
    )
    await client.patch(
        f"/api/admin/projects/{p2.id}/broadcast-limit",
        json={"broadcast_limit": 500},
    )

    r1 = await client.get(f"/api/admin/projects/{p1.id}/broadcast-limit")
    r2 = await client.get(f"/api/admin/projects/{p2.id}/broadcast-limit")

    assert r1.json()["broadcast_limit"] == 10
    assert r2.json()["broadcast_limit"] == 500
    assert r1.json()["broadcast_limit"] != r2.json()["broadcast_limit"]


@pytest.mark.asyncio
async def test_get_project_broadcast_limit_helper():
    """The helper returns the per-project override when set and global default when unset."""
    original = runtime_config.get("project_broadcast_limit")
    try:
        runtime_config["project_broadcast_limit"] = 999

        class _ProjectWithOverride:
            design_doc = {"_broadcast_limit": 42}

        class _ProjectWithoutOverride:
            design_doc = None

        assert get_project_broadcast_limit(_ProjectWithOverride()) == 42
        assert get_project_broadcast_limit(_ProjectWithoutOverride()) == 999
    finally:
        if original is None:
            runtime_config.pop("project_broadcast_limit", None)
        else:
            runtime_config["project_broadcast_limit"] = original
