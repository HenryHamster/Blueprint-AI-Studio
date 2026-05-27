import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException

from app.models import Project
from app.shared_state import (
    get_project_broadcast_limit,
    get_project_integration_branch,
    get_project_promotion_target,
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
    project = Project(name="Member Test Project", user_id="some-other-user")
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
    """branch_whitelist default includes 'main' when no integration_branch is set."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/auto-remediation")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "main" in data["config"]["branch_whitelist"]


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


# ---------------------------------------------------------------------------
# Issue-implementation config — project-aware defaults
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_get_issue_implementation_defaults_for_main_project(
    client: AsyncClient, db_session, project_owner
):
    """base_branch defaults to 'main' when no integration_branch is set."""
    project = project_owner
    resp = await client.get(f"/api/admin/projects/{project.id}/issue-implementation")
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["base_branch"] == "main"


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
    """PATCH integration-branch with null does not call GitHub at all."""
    project = github_project
    with patch(
        "app.github.get_repo_installation_token",
        new_callable=AsyncMock,
    ) as mock_token:
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/integration-branch",
            json={"integration_branch": None},
        )
    assert resp.status_code == 200, resp.text
    mock_token.assert_not_called()


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
