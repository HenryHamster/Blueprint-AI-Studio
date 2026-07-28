import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from datetime import timedelta

from app.main import app
from app.models import Project, TaskStatus
from app.shared_state import (
    get_project_broadcast_limit,
    get_project_integration_branch,
    get_project_promotion_target,
    runtime_config,
)
from testing.factories import make_membership, make_task, _utcnow


@pytest.fixture
async def project_owner(db_session):
    """Create a project with test-user-123 as owner."""
    project = Project(
        name="Config Test Project",
        user_id="test-user-123",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
async def project_viewer(db_session):
    """Create a project with test-user-123 as a viewer."""
    project = Project(
        name="Viewer Resource Envelope Project",
        user_id="some-other-user",
        is_self_repo=True,
        build_config={
            "resource_envelope": {
                "max_concurrent_vms": 4,
                "spend_ceiling_usd": 50.0,
                "spend_window_days": 14,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="member")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_integration_branch_defaults_to_main(
    client: AsyncClient, db_session, project_owner
):
    """GET /integration-branch returns 'automain' for a self-repo with no override."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/integration-branch")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["integration_branch"] == "automain"
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
    """PATCH with null clears the override and restores the self-repo automain default."""
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
    assert data["integration_branch"] == "automain"
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
    p1 = Project(name="Project A", user_id="test-user-123", is_self_repo=True)
    p2 = Project(
        name="Project B",
        user_id="test-user-123",
        promotion_target="release",
    )
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
        f"/api/admin/projects/{p2.id}/integration-branch",
        json={"integration_branch": "trunk"},
    )

    r1 = await client.get(f"/api/admin/projects/{p1.id}/integration-branch")
    r2 = await client.get(f"/api/admin/projects/{p2.id}/integration-branch")

    assert r1.json()["integration_branch"] == "automain"
    assert r2.json()["integration_branch"] == "trunk"
    # They are independent — p1 override doesn't affect p2
    assert r1.json()["integration_branch"] != r2.json()["integration_branch"]


@pytest.mark.asyncio
async def test_get_project_integration_branch_helper_defaults_to_main():
    """The helper falls back to the per-project default of 'main'."""

    class _FakeProject:
        integration_branch = None

    assert get_project_integration_branch(_FakeProject()) == "main"


# ---------------------------------------------------------------------------
# Promotion-target config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_promotion_target_defaults_to_main(
    client: AsyncClient, db_session, project_owner
):
    """GET /promotion-target returns 'main' when no per-project value is set."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/promotion-target")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["promotion_target"] == "main"
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns the updated promotion target branch."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/promotion-target",
        json={"promotion_target": "lts"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["promotion_target"] == "lts"
    assert data["is_default"] is False

    get_resp = await client.get(f"/api/admin/projects/{project.id}/promotion-target")
    assert get_resp.status_code == 200
    assert get_resp.json()["promotion_target"] == "lts"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_null_reverts_to_default(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null clears the per-project override, reverting to global default."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/promotion-target",
        json={"promotion_target": "release"},
    )
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/promotion-target",
        json={"promotion_target": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_rejects_invalid_chars(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects promotion target branch names with shell metacharacters."""
    project = project_owner
    for bad in ['main";id;"', "main$(cmd)", "main\nfoo"]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/promotion-target",
            json={"promotion_target": bad},
        )
        assert resp.status_code == 422, (
            f"expected 422 for {bad!r}, got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_get_project_promotion_target_helper_defaults_to_main():
    """The helper falls back to the per-project default of 'main'."""

    class _FakeProject:
        promotion_target = None

    assert get_project_promotion_target(_FakeProject()) == "main"


# ---------------------------------------------------------------------------
# Task-branch namespace / prefix config tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def project_member(db_session):
    """Create a project where test-user-123 is a member (not owner)."""
    project = Project(
        name="Member Test Project",
        user_id="some-other-user",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="member")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_task_branch_namespace_defaults_to_blueprint(
    client: AsyncClient, db_session, project_owner
):
    """GET /task-branch-namespace returns 'blueprint' when no per-project value is set."""
    project = project_owner
    original = runtime_config.pop("task_branch_namespace", None)
    try:
        resp = await client.get(
            f"/api/admin/projects/{project.id}/task-branch-namespace"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["project_id"] == str(project.id)
        assert data["task_branch_namespace"] == "blueprint"
        assert data["is_default"] is True
    finally:
        if original is not None:
            runtime_config["task_branch_namespace"] = original


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_namespace_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns the updated namespace and stores it in build_config."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/task-branch-namespace",
        json={"task_branch_namespace": "autoproj"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["task_branch_namespace"] == "autoproj"
    assert data["is_default"] is False

    get_resp = await client.get(
        f"/api/admin/projects/{project.id}/task-branch-namespace"
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["task_branch_namespace"] == "autoproj"
    assert get_resp.json()["is_default"] is False

    await db_session.refresh(project)
    assert (project.build_config or {}).get("task_branch_namespace") == "autoproj"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_namespace_null_reverts_to_default(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null clears the build_config key and reverts to the default."""
    project = project_owner
    original = runtime_config.pop("task_branch_namespace", None)
    try:
        await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-namespace",
            json={"task_branch_namespace": "autoproj"},
        )
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-namespace",
            json={"task_branch_namespace": None},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["task_branch_namespace"] == "blueprint"
        assert data["is_default"] is True

        await db_session.refresh(project)
        assert "task_branch_namespace" not in (project.build_config or {})
    finally:
        if original is not None:
            runtime_config["task_branch_namespace"] = original


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_namespace_rejects_invalid_chars(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects namespaces with shell metacharacters or '/'."""
    project = project_owner
    for bad in ['ns";id;"', "ns$(cmd)", "ns\nfoo", "ns/with/slash"]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-namespace",
            json={"task_branch_namespace": bad},
        )
        assert resp.status_code == 422, (
            f"expected 422 for {bad!r}, got {resp.status_code}"
        )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_namespace_requires_owner_role(
    client: AsyncClient, db_session, project_member
):
    """A member (non-owner) cannot PATCH task-branch-namespace."""
    project = project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/task-branch-namespace",
        json={"task_branch_namespace": "autoproj"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_task_branch_prefix_defaults_to_namespace_slash(
    client: AsyncClient, db_session, project_owner
):
    """GET /task-branch-prefix derives ``{namespace}/`` when no explicit prefix is stored."""
    project = project_owner
    original = runtime_config.pop("task_branch_namespace", None)
    try:
        resp = await client.get(f"/api/admin/projects/{project.id}/task-branch-prefix")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["task_branch_prefix"] == "blueprint/"
        assert data["is_default"] is True

        # After overriding namespace, the derived prefix follows it.
        await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-namespace",
            json={"task_branch_namespace": "autoproj"},
        )
        resp2 = await client.get(f"/api/admin/projects/{project.id}/task-branch-prefix")
        assert resp2.json()["task_branch_prefix"] == "autoproj/"
        assert resp2.json()["is_default"] is True
    finally:
        if original is not None:
            runtime_config["task_branch_namespace"] = original


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_prefix_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with an explicit prefix overrides the derived default."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/task-branch-prefix",
        json={"task_branch_prefix": "legacy/"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["task_branch_prefix"] == "legacy/"
    assert data["is_default"] is False

    get_resp = await client.get(f"/api/admin/projects/{project.id}/task-branch-prefix")
    assert get_resp.json()["task_branch_prefix"] == "legacy/"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_prefix_null_reverts_to_derived(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null clears the explicit prefix and falls back to ``{namespace}/``."""
    project = project_owner
    original = runtime_config.pop("task_branch_namespace", None)
    try:
        await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-prefix",
            json={"task_branch_prefix": "legacy/"},
        )
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-prefix",
            json={"task_branch_prefix": None},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["task_branch_prefix"] == "blueprint/"
        assert data["is_default"] is True

        await db_session.refresh(project)
        assert "task_branch_prefix" not in (project.build_config or {})
    finally:
        if original is not None:
            runtime_config["task_branch_namespace"] = original


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_task_branch_prefix_rejects_invalid_chars(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects prefixes with shell metacharacters."""
    project = project_owner
    for bad in ['legacy";id;"', "legacy$(cmd)", "legacy\nfoo"]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/task-branch-prefix",
            json={"task_branch_prefix": bad},
        )
        assert resp.status_code == 422, (
            f"expected 422 for {bad!r}, got {resp.status_code}"
        )


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


# ---------------------------------------------------------------------------
# Auto-remediation config — project-aware defaults
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_auto_remediation_defaults_for_main_project(
    client: AsyncClient, db_session, project_owner
):
    """Self-repo defaults whitelist the integration trunk and promotion target."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/auto-remediation")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["config"]["branch_whitelist"] == ["automain", "main"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_auto_remediation_defaults_for_custom_branch_project(
    client: AsyncClient, db_session, project_owner
):
    """branch_whitelist default reflects the project's integration_branch, not hardcoded 'master'."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/integration-branch",
        json={"integration_branch": "trunk"},
    )
    resp = await client.get(f"/api/admin/projects/{project.id}/auto-remediation")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "trunk" in data["config"]["branch_whitelist"]
    assert "master" not in data["config"]["branch_whitelist"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_auto_remediation_stored_value_overrides_computed_default(
    client: AsyncClient, db_session, project_owner
):
    """Stored branch_whitelist takes precedence over the computed default."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/auto-remediation",
        json={"branch_whitelist": ["release"]},
    )
    resp = await client.get(f"/api/admin/projects/{project.id}/auto-remediation")
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["branch_whitelist"] == ["release"]


@pytest.mark.asyncio
async def test_auto_remediation_stats_exclude_non_auto_tasks(
    client: AsyncClient, db_session, project_owner
):
    """SQL containment filter excludes tasks without auto_remediation=True."""
    project = project_owner

    auto_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"auto_remediation": True},
    )
    regular_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"auto_remediation": False},
    )
    unrelated_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add_all([auto_task, regular_task, unrelated_task])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/auto-remediation")
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]
    assert stats["spawned_7d"] == 1


# ---------------------------------------------------------------------------
# Issue-implementation config — project-aware defaults
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_issue_implementation_defaults_for_main_project(
    client: AsyncClient, db_session, project_owner
):
    """Self-repo issue implementation defaults to the automain integration trunk."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/issue-implementation")
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["base_branch"] == "automain"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_issue_implementation_defaults_for_custom_branch_project(
    client: AsyncClient, db_session, project_owner
):
    """base_branch default reflects the project's integration_branch."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/integration-branch",
        json={"integration_branch": "trunk"},
    )
    resp = await client.get(f"/api/admin/projects/{project.id}/issue-implementation")
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["base_branch"] == "trunk"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_issue_implementation_rejects_base_branch(
    client: AsyncClient, db_session, project_owner
):
    """base_branch is not an allowed PATCH field — must be rejected with 422."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/issue-implementation",
        json={"base_branch": "release"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Branch-column backfill endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_post_backfill_branches_requires_owner_role(
    client: AsyncClient, db_session, project_member
):
    """A member (non-owner) cannot POST backfill-branches."""
    project = project_member
    resp = await client.post(f"/api/admin/projects/{project.id}/backfill-branches")
    assert resp.status_code == 403, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_post_backfill_branches_owner_succeeds(
    client: AsyncClient, db_session, project_owner
):
    """An owner can POST backfill-branches; the project row is actually mutated and persisted.

    Uses a self-repo project so the backfill takes the no-GitHub-round-trip path
    (promotion_target := 'main'), letting us assert real DB writes without mocking.
    """
    project = project_owner
    project.is_self_repo = True
    project.integration_branch = "automain"  # already set; backfill should skip
    project.promotion_target = None  # the column the backfill must fill
    db_session.add(project)
    await db_session.commit()

    resp = await client.post(f"/api/admin/projects/{project.id}/backfill-branches")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["filled"] == 1
    assert data["errored"] == 0

    # Verify the write was actually committed — re-read from the DB.
    db_session.expire(project)
    await db_session.refresh(project)
    assert project.promotion_target == "main"
    assert project.integration_branch == "automain"


# ---------------------------------------------------------------------------
# Remote branch-existence validation tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def github_project(db_session):
    """Project with GitHub integration configured (owner + repo set)."""
    project = Project(
        name="GitHub Project",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="myrepo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    from testing.factories import make_membership

    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_rejects_nonexistent_branch(
    client: AsyncClient, db_session, github_project
):
    """PATCH integration-branch with a branch that does not exist returns 422."""
    project = github_project
    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.github.get_branch_sha",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="not found"),
        ),
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": "developmnet"},
        )
    assert resp.status_code == 422, resp.text
    msg = resp.json()["error"]["message"]
    assert "developmnet" in msg
    assert "does not exist" in msg


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_accepts_existing_branch(
    client: AsyncClient, db_session, github_project
):
    """PATCH integration-branch with a branch that exists on the remote returns 200."""
    project = github_project
    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.github.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        ),
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": "development"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["integration_branch"] == "development"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_502_when_no_token(
    client: AsyncClient, db_session, github_project
):
    """PATCH integration-branch returns 502 when GitHub credential is unavailable."""
    project = github_project
    with patch(
        "app.github.get_repo_installation_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": "development"},
        )
    assert resp.status_code == 502, resp.text
    assert "credential unavailable" in resp.json()["error"]["message"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_null_skips_remote_check(
    client: AsyncClient, db_session, github_project
):
    """PATCH integration-branch with null skips GitHub and reverts to the safe default.

    A non-self repo with NULL integration_branch resolves to the safe 'main'
    fallback (the resolver fails safe rather than raising), so the readback
    succeeds and the endpoint reports ``is_default``.
    """
    project = github_project
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as safe_client:
        with patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
        ) as mock_token:
            resp = await safe_client.patch(
                f"/api/admin/projects/{project.id}/integration-branch",
                json={"integration_branch": None},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["integration_branch"] == "main"
    assert body["is_default"] is True
    mock_token.assert_not_called()


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_integration_branch_returns_default_for_non_self_repo_without_override(
    db_session, override_get_session, override_get_current_user
):
    """GET /integration-branch returns the safe default (not a 500) when unset.

    The resolver fails safe to 'main' for a non-self repo with NULL
    integration_branch, so the read endpoint stays observable and surfaces
    ``is_default`` so an operator can see the column is still unconfigured.
    """
    project = Project(name="External Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        resp = await safe_client.get(
            f"/api/admin/projects/{project.id}/integration-branch"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["integration_branch"] == "main"
    assert body["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_integration_branch_equal_to_promotion_target_blocks(
    db_session, override_get_session, override_get_current_user
):
    """PATCH integration-branch returns a clean 422 (no persist) when it would
    match the promotion target."""
    project = Project(
        name="Conflict Project",
        user_id="test-user-123",
        integration_branch="automain",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as safe_client:
        resp = await safe_client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": "release"},
        )

    assert resp.status_code == 422, resp.text
    assert "promotion target" in resp.json()["error"]["message"]
    # The invalid value must NOT be persisted — the column stays at its prior
    # value so the project never lands in a permanently-misconfigured state.
    await db_session.refresh(project)
    assert project.integration_branch == "automain"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_rejects_nonexistent_branch(
    client: AsyncClient, db_session, github_project
):
    """PATCH promotion-target with a branch that does not exist returns 422."""
    project = github_project
    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.github.get_branch_sha",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="not found"),
        ),
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/promotion-target",
            json={"promotion_target": "lts-typo"},
        )
    assert resp.status_code == 422, resp.text
    msg = resp.json()["error"]["message"]
    assert "lts-typo" in msg
    assert "does not exist" in msg


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_accepts_existing_branch(
    client: AsyncClient, db_session, github_project
):
    """PATCH promotion-target with a branch that exists on the remote returns 200."""
    project = github_project
    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.github.get_branch_sha",
            new_callable=AsyncMock,
            return_value="def456",
        ),
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/promotion-target",
            json={"promotion_target": "lts"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["promotion_target"] == "lts"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_502_when_no_token(
    client: AsyncClient, db_session, github_project
):
    """PATCH promotion-target returns 502 when GitHub credential is unavailable."""
    project = github_project
    with patch(
        "app.github.get_repo_installation_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/promotion-target",
            json={"promotion_target": "lts"},
        )
    assert resp.status_code == 502, resp.text
    assert "credential unavailable" in resp.json()["error"]["message"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_promotion_target_null_skips_remote_check(
    client: AsyncClient, db_session, github_project
):
    """PATCH promotion-target with null does not call GitHub at all."""
    project = github_project
    with patch(
        "app.github.get_repo_installation_token",
        new_callable=AsyncMock,
    ) as mock_token:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/promotion-target",
            json={"promotion_target": None},
        )
    assert resp.status_code == 200, resp.text
    mock_token.assert_not_called()


# ---------------------------------------------------------------------------
# Approval-gate config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_approval_gate_config_defaults_empty(
    client: AsyncClient, db_session, project_owner
):
    """GET /approval-gate-config returns empty lists and is_default when unset."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/approval-gate-config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["approval_gate_config"] == {"infra_files": [], "critical_files": []}
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_approval_gate_config_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns the stored infra/critical file lists; agents read it via the column."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={
            "infra_files": ["src/main.py", "src/db.py"],
            "critical_files": ["src/auth.py"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["approval_gate_config"]["infra_files"] == ["src/main.py", "src/db.py"]
    assert data["approval_gate_config"]["critical_files"] == ["src/auth.py"]
    assert data["is_default"] is False

    get_resp = await client.get(
        f"/api/admin/projects/{project.id}/approval-gate-config"
    )
    assert get_resp.json()["approval_gate_config"]["critical_files"] == ["src/auth.py"]

    # The claim-response seam reads straight off the column.
    await db_session.refresh(project)
    assert project.approval_gate_config["infra_files"] == ["src/main.py", "src/db.py"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_approval_gate_config_partial_update_merges(
    client: AsyncClient, db_session, project_owner
):
    """A PATCH supplying only one key leaves the other key untouched."""
    project = project_owner
    await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={"infra_files": ["a.py"], "critical_files": ["a.py"]},
    )
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={"critical_files": ["b.py"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["approval_gate_config"]["infra_files"] == ["a.py"]
    assert data["approval_gate_config"]["critical_files"] == ["b.py"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_approval_gate_config_rejects_bad_paths(
    client: AsyncClient, db_session, project_owner
):
    """PATCH rejects unknown fields, non-list values, and implausible paths."""
    project = project_owner
    bad_bodies = [
        {"bogus_field": ["x.py"]},
        {"infra_files": "src/main.py"},
        {"infra_files": [""]},
        {"infra_files": ["/etc/passwd"]},
        {"infra_files": ["../secrets.py"]},
        {"critical_files": ["src/$(cmd).py"]},
    ]
    for body in bad_bodies:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/approval-gate-config",
            json=body,
        )
        assert resp.status_code == 422, f"expected 422 for {body!r}, got {resp.text}"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_approval_gate_config_requires_owner_role(
    client: AsyncClient, db_session, project_member
):
    """A member (non-owner) cannot PATCH approval-gate-config."""
    project = project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={"infra_files": ["src/main.py"]},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Per-project staging/cleanup branch config round-trip tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_build_config_branch_keys_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH build_config with the 4 per-project branch keys survives validation unchanged."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}",
        json={
            "build_config": {
                "staging_branch_prefixes": ["feat/", "exp/"],
                "stale_branch_prefixes": ["feat/"],
                "protected_branches": ["main", "release"],
                "enable_self_improvement": True,
            }
        },
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    bc = project.build_config or {}
    assert bc.get("staging_branch_prefixes") == ["feat/", "exp/"]
    assert bc.get("stale_branch_prefixes") == ["feat/"]
    assert bc.get("protected_branches") == ["main", "release"]
    assert bc.get("enable_self_improvement") is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_build_config_omitting_branch_keys_preserves_existing(
    client: AsyncClient, db_session, project_owner
):
    """Subsequent PATCH that omits the 4 branch keys does not wipe previously stored values."""
    project = project_owner
    # First PATCH: store the 4 keys
    await client.patch(
        f"/api/admin/projects/{project.id}",
        json={
            "build_config": {
                "staging_branch_prefixes": ["research/", "plan/"],
                "stale_branch_prefixes": ["research/"],
                "protected_branches": ["main"],
                "enable_self_improvement": True,
            }
        },
    )
    # Second PATCH: update language only, omit all 4 keys
    resp = await client.patch(
        f"/api/admin/projects/{project.id}",
        json={"build_config": {"language": "python"}},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    bc = project.build_config or {}
    assert bc.get("staging_branch_prefixes") == ["research/", "plan/"]
    assert bc.get("stale_branch_prefixes") == ["research/"]
    assert bc.get("protected_branches") == ["main"]
    assert bc.get("enable_self_improvement") is True
    assert bc.get("language") == "python"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_build_config_empty_stale_prefixes_preserved(
    client: AsyncClient, db_session, project_owner
):
    """An explicit empty stale_branch_prefixes=[] survives round-trip (disables cleanup)."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}",
        json={"build_config": {"stale_branch_prefixes": []}},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    bc = project.build_config or {}
    assert "stale_branch_prefixes" in bc
    assert bc["stale_branch_prefixes"] == []


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_staging_branch_prefixes_defaults(client: AsyncClient, project_owner):
    """GET /staging-branch-prefixes returns the Blueprint defaults when unset."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/staging-branch-prefixes")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["staging_branch_prefixes"] == ["research/", "plan/"]
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_staging_branch_prefixes_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH then GET returns custom prefixes and is_default=False."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/staging-branch-prefixes",
        json={"staging_branch_prefixes": ["feature/", "exp/"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["staging_branch_prefixes"] == ["feature/", "exp/"]
    assert data["is_default"] is False

    await db_session.refresh(project)
    assert (project.build_config or {}).get("staging_branch_prefixes") == [
        "feature/",
        "exp/",
    ]


@pytest.mark.asyncio
async def test_patch_staging_branch_prefixes_revert_to_default(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with null reverts to global default and removes the build_config key."""
    project = project_owner
    project.build_config = {"staging_branch_prefixes": ["feature/"]}
    db_session.add(project)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/staging-branch-prefixes",
        json={"staging_branch_prefixes": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["staging_branch_prefixes"] == ["research/", "plan/"]
    assert data["is_default"] is True

    await db_session.refresh(project)
    assert "staging_branch_prefixes" not in (project.build_config or {})


@pytest.mark.asyncio
async def test_patch_staging_branch_prefixes_rejects_empty_entry(
    client: AsyncClient, project_owner
):
    """PATCH rejects a list containing an empty or whitespace-only string."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/staging-branch-prefixes",
        json={"staging_branch_prefixes": ["feature/", ""]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_staging_branch_prefixes_trims_whitespace(
    client: AsyncClient, db_session, project_owner
):
    """PATCH trims leading/trailing whitespace from each prefix entry."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/staging-branch-prefixes",
        json={"staging_branch_prefixes": ["  feature/  ", "exp/"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["staging_branch_prefixes"] == ["feature/", "exp/"]


# ---------------------------------------------------------------------------
# Block-degraded-unverified-merge policy config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_block_degraded_unverified_merge_defaults_false(
    client: AsyncClient, project_owner
):
    """GET returns {block_degraded_unverified_merge: false, is_default: true} when unset."""
    project = project_owner
    resp = await client.get(
        f"/api/admin/projects/{project.id}/block-degraded-unverified-merge"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["block_degraded_unverified_merge"] is False
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_degraded_unverified_merge_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH true → is_default=false; PATCH false → is_default=true (reverts to default)."""
    project = project_owner

    # Enable the policy.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/block-degraded-unverified-merge",
        json={"block_degraded_unverified_merge": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["block_degraded_unverified_merge"] is True
    assert data["is_default"] is False

    get_resp = await client.get(
        f"/api/admin/projects/{project.id}/block-degraded-unverified-merge"
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["block_degraded_unverified_merge"] is True
    assert get_resp.json()["is_default"] is False

    # Disable the policy — should revert to default (key removed from build_config).
    resp2 = await client.patch(
        f"/api/admin/projects/{project.id}/block-degraded-unverified-merge",
        json={"block_degraded_unverified_merge": False},
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["block_degraded_unverified_merge"] is False
    assert data2["is_default"] is True

    await db_session.refresh(project)
    assert "block_degraded_unverified_merge" not in (project.build_config or {})


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_degraded_unverified_merge_requires_owner_role(
    client: AsyncClient, project_member
):
    """A non-owner member cannot PATCH the block-degraded-unverified-merge policy."""
    project = project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/block-degraded-unverified-merge",
        json={"block_degraded_unverified_merge": True},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_degraded_unverified_merge_rejects_non_bool(
    client: AsyncClient, project_owner
):
    """PATCH rejects a non-boolean value for block_degraded_unverified_merge with 422."""
    project = project_owner
    for bad_val in ["yes", 1, None]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/block-degraded-unverified-merge",
            json={"block_degraded_unverified_merge": bad_val},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for value {bad_val!r}, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# block_no_test_unverified_merge config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_block_no_test_unverified_merge_defaults_false(
    client: AsyncClient, project_owner
):
    """GET returns {block_no_test_unverified_merge: false, is_default: true} when unset."""
    project = project_owner
    resp = await client.get(
        f"/api/admin/projects/{project.id}/block-no-test-unverified-merge"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["block_no_test_unverified_merge"] is False
    assert data["is_default"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_no_test_unverified_merge_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH true → is_default=false; PATCH false → is_default=true (reverts to default)."""
    project = project_owner

    # Enable the policy.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/block-no-test-unverified-merge",
        json={"block_no_test_unverified_merge": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["block_no_test_unverified_merge"] is True
    assert data["is_default"] is False

    get_resp = await client.get(
        f"/api/admin/projects/{project.id}/block-no-test-unverified-merge"
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["block_no_test_unverified_merge"] is True
    assert get_resp.json()["is_default"] is False

    # Disable the policy — should revert to default (key removed from build_config).
    resp2 = await client.patch(
        f"/api/admin/projects/{project.id}/block-no-test-unverified-merge",
        json={"block_no_test_unverified_merge": False},
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["block_no_test_unverified_merge"] is False
    assert data2["is_default"] is True

    await db_session.refresh(project)
    assert "block_no_test_unverified_merge" not in (project.build_config or {})


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_no_test_unverified_merge_requires_owner_role(
    client: AsyncClient, project_member
):
    """A non-owner member cannot PATCH the block-no-test-unverified-merge policy."""
    project = project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/block-no-test-unverified-merge",
        json={"block_no_test_unverified_merge": True},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_block_no_test_unverified_merge_rejects_non_bool(
    client: AsyncClient, project_owner
):
    """PATCH rejects a non-boolean value for block_no_test_unverified_merge with 422."""
    project = project_owner
    for bad_val in ["yes", 1, None]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/block-no-test-unverified-merge",
            json={"block_no_test_unverified_merge": bad_val},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for value {bad_val!r}, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Resource-envelope config tests
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_resource_envelope_returns_limits_and_usage(
    client: AsyncClient, db_session, project_viewer
):
    """GET returns the stored envelope plus live active-VM count and spend."""
    project = project_viewer
    now = _utcnow()
    active_one = make_task(
        project_id=project.id,
        status=TaskStatus.PROVISIONING,
        updated_at=now - timedelta(minutes=15),
    )
    active_two = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        updated_at=now - timedelta(minutes=5),
    )
    completed = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=7.25,
        updated_at=now - timedelta(days=1),
    )
    db_session.add_all([active_one, active_two, completed])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["max_concurrent_vms"] == 4
    assert data["spend_ceiling_usd"] == 50.0
    assert data["spend_window_days"] == 14
    assert data["active_vm_count"] == 2
    assert data["recent_spend_usd"] == pytest.approx(7.25, abs=1e-6)


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_persists_into_build_config(
    client: AsyncClient, db_session, project_owner
):
    """PATCH stores the envelope under build_config['resource_envelope'] and returns live usage."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={
            "max_concurrent_vms": None,
            "spend_ceiling_usd": 25.0,
            "spend_window_days": 21,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_concurrent_vms"] is None
    assert data["spend_ceiling_usd"] == 25.0
    assert data["spend_window_days"] == 21

    await db_session.refresh(project)
    assert (project.build_config or {}).get("resource_envelope") == {
        "spend_ceiling_usd": 25.0,
        "spend_window_days": 21,
    }


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_requires_owner_role(
    client: AsyncClient, project_member
):
    """A non-owner cannot PATCH the resource envelope."""
    project = project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"max_concurrent_vms": 2},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_rejects_invalid_values(
    client: AsyncClient, project_owner
):
    """PATCH rejects invalid envelope values using the nested schema validation."""
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"max_concurrent_vms": 0},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_sets_broad_execution_placement_policy(
    client: AsyncClient, db_session, project_owner
):
    project = project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"execution_placement_policy": "local_only"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["execution_placement_policy"] == "local_only"
    assert data["execution_placement_policy_is_explicit"] is True
    assert data["legacy_require_local_runner_active"] is False
    await db_session.refresh(project)
    assert (
        project.build_config["resource_envelope"]["execution_placement_policy"]
        == "local_only"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_requires_explicit_legacy_migration(
    client: AsyncClient, db_session, project_owner
):
    """A legacy compute-only setting cannot become a broad policy by accident."""
    project = project_owner
    project.build_config = {"resource_envelope": {"require_local_runner": True}}
    db_session.add(project)
    await db_session.commit()

    rejected = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"execution_placement_policy": "local_only"},
    )
    assert rejected.status_code == 422, rejected.text

    migrated = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={
            "execution_placement_policy": "local_only",
            "require_local_runner": None,
        },
    )
    assert migrated.status_code == 200, migrated.text
    data = migrated.json()
    assert data["execution_placement_policy"] == "local_only"
    assert data["legacy_require_local_runner_active"] is False
    await db_session.refresh(project)
    assert project.build_config["resource_envelope"] == {
        "execution_placement_policy": "local_only",
        # ResourceEnvelopeSchema serializes the existing default introduced by
        # the current mainline spend-window contract. The migration assertion is
        # about clearing the legacy flag, not suppressing that independent
        # default.
        "spend_window_days": 30,
    }


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_resource_envelope_marks_legacy_local_runner_scope(
    client: AsyncClient, db_session, project_viewer
):
    project = project_viewer
    project.build_config = {"resource_envelope": {"require_local_runner": True}}
    db_session.add(project)
    await db_session.commit()
    response = await client.get(
        f"/api/admin/projects/{project.id}/resource-envelope"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["execution_placement_policy"] == "managed_allowed"
    assert data["execution_placement_policy_is_explicit"] is False
    assert data["legacy_require_local_runner_active"] is True


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_resource_envelope_includes_compute_job_cap(
    client: AsyncClient, project_viewer
):
    """GET includes max_concurrent_compute_jobs in the response (None when no cap set)."""
    project = project_viewer
    resp = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "max_concurrent_compute_jobs" in data
    assert data["max_concurrent_compute_jobs"] is None


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_persists_compute_job_cap(
    client: AsyncClient, db_session, project_owner
):
    """PATCH with max_concurrent_compute_jobs updates runtime_config and GET reflects the new cap."""
    project = project_owner
    original_caps = dict(
        runtime_config.get("max_concurrent_compute_jobs_by_project") or {}
    )
    try:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/resource-envelope",
            json={"max_concurrent_compute_jobs": 5},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["max_concurrent_compute_jobs"] == 5

        # Verify runtime_config was updated in-memory so the enforcer sees the cap.
        from app.runtime_config_state import resolve_project_compute_job_concurrency_cap

        assert resolve_project_compute_job_concurrency_cap(project.id) == 5

        # Verify GET returns the updated cap.
        resp2 = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["max_concurrent_compute_jobs"] == 5

        # Verify the cap is NOT stored in build_config (it lives in runtime_config only).
        await db_session.refresh(project)
        envelope = (project.build_config or {}).get("resource_envelope", {})
        assert "max_concurrent_compute_jobs" not in envelope
    finally:
        # Restore runtime_config to avoid bleeding into other tests.
        caps = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        caps.pop(str(project.id), None)
        runtime_config["max_concurrent_compute_jobs_by_project"] = caps
        if original_caps:
            runtime_config["max_concurrent_compute_jobs_by_project"] = original_caps


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_clears_compute_job_cap(
    client: AsyncClient, project_owner
):
    """PATCH with max_concurrent_compute_jobs=None clears an existing cap."""
    project = project_owner
    # Set a cap first.
    caps = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    caps[str(project.id)] = 3
    runtime_config["max_concurrent_compute_jobs_by_project"] = caps
    try:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/resource-envelope",
            json={"max_concurrent_compute_jobs": None},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["max_concurrent_compute_jobs"] is None

        from app.runtime_config_state import resolve_project_compute_job_concurrency_cap

        assert resolve_project_compute_job_concurrency_cap(project.id) is None
    finally:
        caps = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        caps.pop(str(project.id), None)
        runtime_config["max_concurrent_compute_jobs_by_project"] = caps


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_rejects_invalid_compute_job_cap(
    client: AsyncClient, project_owner
):
    """PATCH rejects compute-job cap values below 1."""
    project = project_owner
    for bad_val in [0, -1]:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/resource-envelope",
            json={"max_concurrent_compute_jobs": bad_val},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for value {bad_val}, got {resp.status_code}"
        )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_patch_resource_envelope_hardware_policy_round_trip(
    client: AsyncClient, db_session, project_owner
):
    """PATCH allowed_machine_types/allowed_accelerator_types persists and GET returns them."""
    project = project_owner

    # Verify GET returns null for both fields when not set.
    resp = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["allowed_machine_types"] is None
    assert data["allowed_accelerator_types"] is None

    # PATCH sets both hardware policy fields.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={
            "allowed_machine_types": ["e2-standard-2", "e2-standard-4"],
            "allowed_accelerator_types": ["T4", "L4"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["allowed_machine_types"] == ["e2-standard-2", "e2-standard-4"]
    assert data["allowed_accelerator_types"] == ["T4", "L4"]

    # Verify persisted in build_config.
    await db_session.refresh(project)
    envelope = (project.build_config or {}).get("resource_envelope", {})
    assert envelope["allowed_machine_types"] == ["e2-standard-2", "e2-standard-4"]
    assert envelope["allowed_accelerator_types"] == ["T4", "L4"]

    # Verify GET also returns the persisted values.
    resp2 = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["allowed_machine_types"] == ["e2-standard-2", "e2-standard-4"]
    assert data2["allowed_accelerator_types"] == ["T4", "L4"]
