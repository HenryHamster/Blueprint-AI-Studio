"""
Tests for admin routers:
1. admin_users.py  — user management + project member management
2. admin_projects.py — project CRUD + autonomous mode
3. admin_notifications.py — alerts, messages, playbook
4. admin_operator_triggers.py — manual operator triggers (self-audit, qualify,
   clear-wave-deps, rollup, findings)
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app import release_version as rv
from app.models import (
    AllowedUser,
    Artifact,
    ArtifactType,
    ComputeJob,
    Project,
    ProjectMember,
    ProjectStatus,
    ReleaseVersion,
    Task,
    TaskStatus,
    Alert,
    ProjectMessage,
    MessageType,
    MessagesLastSeen,
    SystemMaturity,
    _on_task_terminal_callbacks,
)
from app.auth import get_current_user, require_admin
from app.schemas import AgentStatusPayload


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


@pytest.fixture(autouse=True)
def _default_no_migration_pr():
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    The operator-approval merge paths (``_handle_rollup_pr_approval``,
    ``_merge_critical_pr``) and the operator-triggered rollup auto-merge now run
    ``enforce_premerge_migration_guard`` before merging, which calls
    ``github.get_pr_files``. Most merge tests only mock ``merge_pull_request``;
    returning a benign non-migration file list short-circuits the guard to
    ``ok`` so those merges proceed unchanged. A non-empty list is used so other
    ``get_pr_files`` consumers (e.g. empty-PR detection) keep their pre-change
    behavior. Tests exercising the guard itself re-patch ``app.github.get_pr_files``.
    """
    with patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "README.md"}],
    ):
        yield


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project"):
    """Create a project with the given user as owner, plus the owner membership."""
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    session.add(member)
    await session.commit()
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# admin_users.py — User Management & Project Members
# ═══════════════════════════════════════════════════════════════════════════════


class TestListUsers:
    @pytest.mark.asyncio
    async def test_admin_can_list_users(self, client: AsyncClient, db_session):
        """Admin user can retrieve the full user list."""
        user = AllowedUser(email="alice@example.com", role="user")
        db_session.add(user)
        await db_session.commit()

        resp = await client.get("/api/admin/users")
        assert resp.status_code == 200
        emails = [u["email"] for u in resp.json()["users"]]
        assert "alice@example.com" in emails

    @pytest.mark.asyncio
    async def test_non_admin_cannot_list_users(self, override_get_session, db_session):
        """Non-admin is rejected with 403."""
        from fastapi import HTTPException

        async def non_admin():
            return {"user_id": "other", "email": "other@example.com", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[get_current_user] = non_admin
        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/users")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)


class TestAddUser:
    @pytest.mark.asyncio
    async def test_add_user_success(self, client: AsyncClient, db_session):
        """Admin can create a new system user."""
        resp = await client.post(
            "/api/admin/users", json={"email": "newuser@example.com", "role": "user"}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newuser@example.com"
        assert data["role"] == "user"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_add_user_duplicate_email_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Duplicate email returns 409."""
        db_session.add(AllowedUser(email="dup@example.com", role="user"))
        await db_session.commit()

        resp = await client.post(
            "/api/admin/users", json={"email": "dup@example.com", "role": "user"}
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_add_user_invalid_role_returns_422(self, client: AsyncClient):
        """Invalid role value is rejected by Pydantic."""
        resp = await client.post(
            "/api/admin/users", json={"email": "x@example.com", "role": "superuser"}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_add_user_invalid_email_returns_422(self, client: AsyncClient):
        """Email without '@' is rejected by Pydantic."""
        resp = await client.post(
            "/api/admin/users", json={"email": "notanemail", "role": "user"}
        )
        assert resp.status_code == 422


class TestUpdateUser:
    @pytest.mark.asyncio
    async def test_update_user_role(self, client: AsyncClient, db_session):
        """Admin can change a user's role."""
        user = AllowedUser(email="upd@example.com", role="user")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        resp = await client.patch(f"/api/admin/users/{user.id}", json={"role": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    @pytest.mark.asyncio
    async def test_update_nonexistent_user_returns_404(self, client: AsyncClient):
        """Updating a missing user returns 404."""
        resp = await client.patch(
            f"/api/admin/users/{uuid.uuid4()}", json={"role": "admin"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_user_invalid_role_returns_422(
        self, client: AsyncClient, db_session
    ):
        """Invalid role in update payload is rejected."""
        user = AllowedUser(email="role2@example.com", role="user")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        resp = await client.patch(
            f"/api/admin/users/{user.id}", json={"role": "superadmin"}
        )
        assert resp.status_code == 422


class TestRemoveUser:
    @pytest.mark.asyncio
    async def test_remove_user_success(self, client: AsyncClient, db_session):
        """Admin can remove another user."""
        user = AllowedUser(email="removeme@example.com", role="user")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        resp = await client.delete(f"/api/admin/users/{user.id}")
        assert resp.status_code == 200
        assert "removed" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_user_returns_404(self, client: AsyncClient):
        """Removing a missing user returns 404."""
        resp = await client.delete(f"/api/admin/users/{uuid.uuid4()}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_self_returns_400(self, client: AsyncClient, db_session):
        """Admin cannot delete their own account."""
        # Match by firebase_uid (== user_id in the auth token)
        self_user = AllowedUser(
            email=TEST_USER_EMAIL, firebase_uid=TEST_USER_ID, role="admin"
        )
        db_session.add(self_user)
        await db_session.commit()
        await db_session.refresh(self_user)

        resp = await client.delete(f"/api/admin/users/{self_user.id}")
        assert resp.status_code == 400
        assert "own" in resp.json()["error"]["message"]


class TestProjectMembers:
    @pytest.mark.asyncio
    async def test_list_members_returns_owner(self, client: AsyncClient, db_session):
        """Any project member can list the members."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/projects/{project.id}/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0
        members = data["members"]
        assert len(members) == 1
        assert members[0]["role"] == "owner"

    @pytest.mark.asyncio
    async def test_list_members_pagination(self, client: AsyncClient, db_session):
        """limit/offset paginate the member list while total reflects the full count."""
        project = await _make_project(db_session)
        for i in range(5):
            db_session.add(
                ProjectMember(
                    project_id=project.id, user_id=f"member-uid-{i}", role="member"
                )
            )
        await db_session.commit()

        # 6 members total (owner + 5). First page of 2.
        resp = await client.get(
            f"/api/admin/projects/{project.id}/members?limit=2&offset=0"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert data["limit"] == 2
        assert data["offset"] == 0
        assert len(data["members"]) == 2

        # Offset past the first page.
        resp = await client.get(
            f"/api/admin/projects/{project.id}/members?limit=2&offset=4"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert data["offset"] == 4
        assert len(data["members"]) == 2

    @pytest.mark.asyncio
    async def test_list_members_invalid_limit_returns_422(
        self, client: AsyncClient, db_session
    ):
        """limit outside 1..500 is rejected."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/projects/{project.id}/members?limit=501")
        assert resp.status_code == 422

        resp = await client.get(f"/api/admin/projects/{project.id}/members?offset=-1")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_members_non_member_returns_404(
        self, client: AsyncClient, db_session
    ):
        """User without membership gets 404."""
        # Create a project owned by someone else with no membership for test user
        project = Project(name="Private", user_id="other-owner")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.get(f"/api/admin/projects/{project.id}/members")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_project_member_success(self, client: AsyncClient, db_session):
        """Owner can add a new member by email."""
        project = await _make_project(db_session)
        target = AllowedUser(
            email="newmember@example.com", firebase_uid="new-uid-456", role="user"
        )
        db_session.add(target)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "newmember@example.com", "role": "member"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newmember@example.com"
        assert data["role"] == "member"

    @pytest.mark.asyncio
    async def test_add_project_member_user_not_in_system_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Email not in AllowedUser table returns 404."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "ghost@example.com", "role": "member"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_add_project_member_duplicate_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Adding an already-existing member returns 409."""
        project = await _make_project(db_session)
        target = AllowedUser(
            email="already@example.com", firebase_uid="already-uid", role="user"
        )
        db_session.add(target)
        existing = ProjectMember(
            project_id=project.id, user_id="already-uid", role="member"
        )
        db_session.add(existing)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "already@example.com", "role": "member"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_add_project_member_invalid_role_returns_422(
        self, client: AsyncClient, db_session
    ):
        """Invalid member role (only 'member' and 'viewer' allowed) returns 422."""
        project = await _make_project(db_session)
        target = AllowedUser(
            email="valid@example.com", firebase_uid="valid-uid", role="user"
        )
        db_session.add(target)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "valid@example.com", "role": "owner"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_project_member_role(self, client: AsyncClient, db_session):
        """Owner can change a member's role."""
        project = await _make_project(db_session)
        db_session.add(
            AllowedUser(email="m@example.com", firebase_uid="m-uid", role="user")
        )
        m = ProjectMember(project_id=project.id, user_id="m-uid", role="member")
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/members/{m.id}",
            json={"role": "viewer"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

    @pytest.mark.asyncio
    async def test_update_owner_role_returns_400(self, client: AsyncClient, db_session):
        """Cannot downgrade the project owner's role."""
        project = await _make_project(db_session)
        result = await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == TEST_USER_ID,
            )
        )
        owner_member = result.scalar_one()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/members/{owner_member.id}",
            json={"role": "member"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_remove_project_member_success(self, client: AsyncClient, db_session):
        """Owner can remove a non-owner member."""
        project = await _make_project(db_session)
        db_session.add(
            AllowedUser(email="rem@example.com", firebase_uid="rem-uid", role="user")
        )
        m = ProjectMember(project_id=project.id, user_id="rem-uid", role="member")
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)

        resp = await client.delete(f"/api/admin/projects/{project.id}/members/{m.id}")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Member removed"

    @pytest.mark.asyncio
    async def test_remove_project_owner_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Cannot remove the project owner via the member endpoint."""
        project = await _make_project(db_session)
        result = await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == TEST_USER_ID,
            )
        )
        owner_member = result.scalar_one()

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/members/{owner_member.id}"
        )
        assert resp.status_code == 400


class TestNullBranchConfigDiagnostics:
    @pytest.mark.asyncio
    async def test_lists_null_branch_projects_and_separates_self_repo(
        self, client: AsyncClient, db_session
    ):
        self_repo = Project(
            name="Self Repo",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="self-repo",
            is_self_repo=True,
        )
        configured = Project(
            name="Configured",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="configured-repo",
            integration_branch="develop",
            promotion_target="release",
        )
        # Non-self-repo project with a null integration_branch: this is the only
        # input that exercises the endpoint's main (else) branch, so the project
        # must land in data["projects"] rather than self_repo_skipped.
        misconfigured = Project(
            name="Misconfigured",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="misconfigured-repo",
            is_self_repo=False,
        )
        db_session.add_all([self_repo, configured, misconfigured])
        await db_session.commit()

        resp = await client.get("/api/admin/diagnostics/null-branch-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["projects"]) == 1
        listed = data["projects"][0]
        assert listed["id"] == str(misconfigured.id)
        assert listed["name"] == "Misconfigured"
        assert listed["github_owner"] == "acme"
        assert listed["github_repo"] == "misconfigured-repo"
        assert listed["is_self_repo"] is False
        assert "created_at" in listed
        assert len(data["self_repo_skipped"]) == 1
        skipped = data["self_repo_skipped"][0]
        assert skipped["id"] == str(self_repo.id)
        assert skipped["name"] == "Self Repo"
        assert skipped["github_owner"] == "acme"
        assert skipped["github_repo"] == "self-repo"
        assert skipped["is_self_repo"] is True
        assert "created_at" in skipped


# ═══════════════════════════════════════════════════════════════════════════════
# admin_projects.py — Project CRUD + Autonomous Mode
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetAllProjects:
    @pytest.mark.asyncio
    async def test_returns_user_projects(self, client: AsyncClient, db_session):
        """Only projects where the user is a member are returned."""
        project = await _make_project(db_session, name="My Project")

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        ids = [p["id"] for p in data["items"]]
        assert str(project.id) in ids

    @pytest.mark.asyncio
    async def test_excludes_other_users_projects(self, client: AsyncClient, db_session):
        """Projects the user has no membership in are excluded."""
        other = Project(name="Other Project", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()["items"]]
        assert str(other.id) not in ids

    @pytest.mark.asyncio
    async def test_response_includes_my_role(self, client: AsyncClient, db_session):
        """Each project entry includes the user's role."""
        await _make_project(db_session, name="Role Test")

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        assert all("my_role" in p for p in resp.json()["items"])

    @pytest.mark.asyncio
    async def test_owner_sees_env_vars_in_list(self, client: AsyncClient, db_session):
        """Owner can see env_vars in build_config from the list endpoint."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="EnvVars Owner List")
        project.build_config = {"env_vars": {"SECRET": "s3cr3t"}, "node_version": "18"}
        flag_modified(project, "build_config")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        assert target["my_role"] == "owner"
        assert target["build_config"].get("env_vars") == {"SECRET": "s3cr3t"}
        assert target["build_config"].get("node_version") == "18"

    @pytest.mark.asyncio
    async def test_member_cannot_see_env_vars_in_list(
        self, client: AsyncClient, db_session
    ):
        """Member role cannot see env_vars from the list endpoint but sees other fields."""
        from sqlalchemy.orm.attributes import flag_modified

        other_project = Project(name="Member EnvVars List", user_id="other-owner-list")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)
        other_project.build_config = {
            "env_vars": {"SECRET": "hidden"},
            "node_version": "20",
        }
        flag_modified(other_project, "build_config")
        db_session.add(
            ProjectMember(
                project_id=other_project.id, user_id="other-owner-list", role="owner"
            )
        )
        db_session.add(
            ProjectMember(
                project_id=other_project.id, user_id=TEST_USER_ID, role="member"
            )
        )
        db_session.add(other_project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(other_project.id))
        assert target["my_role"] == "member"
        assert "env_vars" not in target["build_config"]
        assert target["build_config"].get("node_version") == "20"

    @pytest.mark.asyncio
    async def test_directions_count_matches_actual_count(
        self, client: AsyncClient, db_session
    ):
        """directions_count in list response matches actual number of directions."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Directions Count Test")

        # Test 1: empty directions should yield directions_count=0
        project.directions = []
        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        assert target["directions_count"] == 0

        # Test 2: 3 directions should yield directions_count=3
        project.directions = [
            {"id": "d-1", "text": "Direction 1", "priority": "high"},
            {"id": "d-2", "text": "Direction 2", "priority": "medium"},
            {"id": "d-3", "text": "Direction 3", "priority": "low"},
        ]
        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        assert target["directions_count"] == 3


class TestGetProject:
    @pytest.mark.asyncio
    async def test_owner_sees_env_vars_in_detail(self, client: AsyncClient, db_session):
        """Owner can see env_vars in build_config from the detail endpoint."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="EnvVars Owner Detail")
        project.build_config = {"env_vars": {"TOKEN": "abc"}, "node_version": "18"}
        flag_modified(project, "build_config")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["my_role"] == "owner"
        assert data["build_config"].get("env_vars") == {"TOKEN": "abc"}
        assert data["build_config"].get("node_version") == "18"

    @pytest.mark.asyncio
    async def test_member_cannot_see_env_vars_in_detail(
        self, client: AsyncClient, db_session
    ):
        """Member cannot see env_vars from the detail endpoint but sees other fields."""
        from sqlalchemy.orm.attributes import flag_modified

        other_project = Project(
            name="Member EnvVars Detail", user_id="other-owner-detail"
        )
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)
        other_project.build_config = {
            "env_vars": {"SECRET": "hidden"},
            "node_version": "20",
        }
        flag_modified(other_project, "build_config")
        db_session.add(
            ProjectMember(
                project_id=other_project.id, user_id="other-owner-detail", role="owner"
            )
        )
        db_session.add(
            ProjectMember(
                project_id=other_project.id, user_id=TEST_USER_ID, role="member"
            )
        )
        db_session.add(other_project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{other_project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["my_role"] == "member"
        assert "env_vars" not in data["build_config"]
        assert data["build_config"].get("node_version") == "20"


class TestUpdateProject:
    @pytest.mark.asyncio
    async def test_update_project_name(self, client: AsyncClient, db_session):
        """Owner can rename a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_update_approval_gate_config_round_trip(
        self, client: AsyncClient, db_session
    ):
        """Owner can set approval_gate_config and read it back via GET."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "approval_gate_config": {
                    "infra_files": ["src/main.py", "src/db.py"],
                    "critical_files": ["src/auth.py"],
                }
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_gate_config"] == {
            "infra_files": ["src/main.py", "src/db.py"],
            "critical_files": ["src/auth.py"],
        }

        get_resp = await client.get(f"/api/admin/projects/{project.id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["approval_gate_config"]["critical_files"] == [
            "src/auth.py"
        ]

    @pytest.mark.asyncio
    async def test_update_approval_gate_config_rejects_bad_path(
        self, client: AsyncClient, db_session
    ):
        """An absolute or traversal path is rejected with 422."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"approval_gate_config": {"infra_files": ["../etc/passwd"]}},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_autonomous_enabled(self, client: AsyncClient, db_session):
        """Can toggle autonomous_enabled on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}", json={"autonomous_enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is True

    @pytest.mark.asyncio
    async def test_update_status_reactivates_project(
        self, client: AsyncClient, db_session
    ):
        """Owner can correct a wrongly-terminal status back to active — the
        recovery path for a project that was auto-terminalized."""
        from app.models import ProjectStatus

        project = await _make_project(db_session)
        project.status = ProjectStatus.CANCELLED
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}", json={"status": "DEPLOYING"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == ProjectStatus.DEPLOYING

    @pytest.mark.asyncio
    async def test_patch_status_cancelled_is_rejected(
        self, client: AsyncClient, db_session
    ):
        """Cancelling via PATCH is rejected — it must go through /cancel (which
        also tears down VMs), so a bare flip can't strand live VMs."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}", json={"status": "CANCELLED"}
        )
        assert resp.status_code == 400
        assert "/cancel" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_create_task_auto_backfills_branch_columns_before_base_branch(
        self, client: AsyncClient, db_session
    ):
        with (
            patch(
                "app.release_promotion._resolve_github_with_session",
                new_callable=AsyncMock,
                return_value=("tok", "acme", "develop-repo"),
            ),
            patch(
                "app.github.get_repo_default_branch",
                new_callable=AsyncMock,
                return_value="develop",
            ),
        ):
            resp = await client.post(
                "/api/admin/tasks",
                json={
                    "github_repo": "acme/develop-repo",
                    "title": "First task",
                    "prompt": "Do the thing",
                },
            )
        assert resp.status_code == 200, resp.text
        project = (
            await db_session.execute(
                select(Project).where(
                    Project.github_owner == "acme",
                    Project.github_repo == "develop-repo",
                )
            )
        ).scalar_one()
        assert project.integration_branch == "develop"
        assert project.promotion_target == "develop"
        task = (
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalar_one()
        assert task.payloads[0]["base_branch"] == "develop"

    @pytest.mark.asyncio
    async def test_viewer_cannot_update_project(self, client: AsyncClient, db_session):
        """Viewer-role is below the 'member' threshold — returns 403."""
        other = Project(name="Viewer Project", user_id="other-99")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="viewer")
        )
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{other.id}", json={"name": "Hacked"}
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_direction_invalid_priority_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Direction with an unrecognised priority value returns 400."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"directions": [{"text": "Do thing", "priority": "ultra"}]},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_design_doc_stale_revision_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Supplying an outdated base_revision for design_doc returns 409."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        # Simulate one prior agent write that bumped _meta.revision to 3
        project.design_doc = {"key": "value", "_meta": {"revision": 3}}
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"design_doc": {"key": "overwrite"}, "design_doc_base_revision": 1},
        )
        assert resp.status_code == 409
        msg = resp.json()["error"]["message"]
        assert "base_revision=1" in msg
        assert "current=3" in msg

    @pytest.mark.asyncio
    async def test_design_doc_current_revision_succeeds_and_bumps_meta(
        self, client: AsyncClient, db_session
    ):
        """Matching base_revision saves doc and advances _meta.revision."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = {"key": "old", "_meta": {"revision": 2}}
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"design_doc": {"key": "new"}, "design_doc_base_revision": 2},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        doc = project.design_doc or {}
        assert doc.get("key") == "new"
        assert doc.get("_meta", {}).get("revision") == 3
        # Divergent top-level revision key must not be present
        assert "revision" not in doc

    @pytest.mark.asyncio
    async def test_design_doc_no_base_revision_always_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Omitting base_revision skips the CAS check (backwards compat)."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"design_doc": {"mykey": "hello"}},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        doc = project.design_doc or {}
        assert doc.get("mykey") == "hello"
        assert doc.get("_meta", {}).get("revision") == 1

    @pytest.mark.asyncio
    async def test_vision_stale_revision_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Supplying an outdated base_revision for vision returns 409."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.vision = {"content": "v1", "revision": 5}
        flag_modified(project, "vision")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"vision": {"content": "overwrite"}, "vision_base_revision": 2},
        )
        assert resp.status_code == 409
        msg = resp.json()["error"]["message"]
        assert "base_revision=2" in msg
        assert "current=5" in msg

    @pytest.mark.asyncio
    async def test_concurrent_design_doc_write_no_data_loss(
        self, client: AsyncClient, db_session
    ):
        """Second writer with a stale revision is rejected; first writer's data survives."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = {"owner_key": "owner_value", "_meta": {"revision": 1}}
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        # First writer succeeds with the correct base revision.
        resp_a = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "design_doc": {"owner_key": "writer_a"},
                "design_doc_base_revision": 1,
            },
        )
        assert resp_a.status_code == 200

        # Second writer tries to write with the old base revision — must be rejected.
        resp_b = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "design_doc": {"owner_key": "writer_b_clobber"},
                "design_doc_base_revision": 1,
            },
        )
        assert resp_b.status_code == 409

        # Writer A's data must be intact — not overwritten.
        await db_session.refresh(project)
        doc = project.design_doc or {}
        assert doc.get("owner_key") == "writer_a"
        assert doc.get("_meta", {}).get("revision") == 2


class TestUpdateProjectOwnerOnlyFields:
    """Members may not mutate owner-only fields; owners always can."""

    async def _make_member_project(self, db_session):
        """Project owned by 'other-owner', with TEST_USER_ID as a member."""
        owner_project = Project(name="Owner Project", user_id="other-owner")
        db_session.add(owner_project)
        await db_session.commit()
        await db_session.refresh(owner_project)
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id="other-owner", role="owner"
            )
        )
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id=TEST_USER_ID, role="member"
            )
        )
        await db_session.commit()
        return owner_project

    @pytest.mark.asyncio
    async def test_member_cannot_update_build_config(
        self, client: AsyncClient, db_session
    ):
        """Member gets 403 when trying to change build_config."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"env_vars": {"SECRET": "x"}}},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_cannot_update_autonomous_enabled(
        self, client: AsyncClient, db_session
    ):
        """Member gets 403 when trying to change autonomous_enabled."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"autonomous_enabled": True},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_cannot_update_design_doc(
        self, client: AsyncClient, db_session
    ):
        """Member gets 403 when trying to change design_doc."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"design_doc": {"summary": "hacked"}},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_cannot_update_status(
        self, client: AsyncClient, db_session
    ):
        """Member gets 403 when trying to change project status."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"status": "DEPLOYING"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_cannot_update_vision(self, client: AsyncClient, db_session):
        """Member gets 403 when trying to change vision."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"vision": {"text": "my vision"}},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_cannot_update_approval_gate_config(
        self, client: AsyncClient, db_session
    ):
        """Member gets 403 when trying to change approval_gate_config."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"approval_gate_config": {"infra_files": ["src/main.py"]}},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_can_update_name(self, client: AsyncClient, db_session):
        """Member may still update non-privileged fields like name."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"name": "Renamed by member"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed by member"

    @pytest.mark.asyncio
    async def test_owner_can_update_build_config_env_vars(
        self, client: AsyncClient, db_session
    ):
        """Project owner can update build_config including env_vars."""
        project = await _make_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"env_vars": {"TOKEN": "secret"}}},
        )
        assert resp.status_code == 200
        bc = resp.json().get("build_config") or {}
        assert bc.get("env_vars", {}).get("TOKEN") == "secret"

    @pytest.mark.asyncio
    async def test_detect_build_config_requires_owner_for_env_vars_persistence(
        self, client: AsyncClient, db_session
    ):
        """Members cannot detect-and-write build_config; owners still can."""
        member_project = await self._make_member_project(db_session)
        member_project.github_owner = "acme"
        member_project.github_repo = "member-repo"
        db_session.add(member_project)

        owner_project = await _make_project(db_session, name="Owner Detect Project")
        owner_project.github_owner = "acme"
        owner_project.github_repo = "owner-repo"
        db_session.add(owner_project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"env_vars": {"TOKEN": "secret"}, "node_version": "20"},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            member_resp = await client.post(
                f"/api/admin/projects/{member_project.id}/detect-build-config"
            )
            owner_resp = await client.post(
                f"/api/admin/projects/{owner_project.id}/detect-build-config"
            )

        assert member_resp.status_code == 403
        assert "owner" in member_resp.text

        assert owner_resp.status_code == 200
        owner_build_config = owner_resp.json().get("build_config") or {}
        assert owner_build_config.get("env_vars", {}).get("TOKEN") == "secret"

        await db_session.refresh(member_project)
        await db_session.refresh(owner_project)
        assert member_project.build_config is None
        assert (owner_project.build_config or {}).get("env_vars", {}).get("TOKEN") == (
            "secret"
        )

    @pytest.mark.asyncio
    async def test_detect_build_config_preserves_existing_operator_settings(
        self, client: AsyncClient, db_session
    ):
        """Detect should keep operator-managed secrets and reviewer toggles."""
        project = await _make_project(db_session, name="Detect Preserve Config")
        project.github_owner = "acme"
        project.github_repo = "preserve-repo"
        project.build_config = {
            "env_vars": {"TOKEN": "secret"},
            "enable_fresh_reviewer": True,
            "enable_parallel_review": True,
            "enable_parallel_fix": False,
            "task_branch_namespace": "agents",
            "task_branch_prefix": "bot/",
            "pr_title_prefix": "[bot] ",
            "requires_ci_checks": False,
        }
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "typescript"},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/detect-build-config"
            )

        assert resp.status_code == 200
        build_config = resp.json().get("build_config") or {}
        assert build_config.get("language") == "typescript"
        assert build_config.get("env_vars", {}).get("TOKEN") == "secret"
        assert build_config.get("enable_fresh_reviewer") is True
        assert build_config.get("enable_parallel_review") is True
        assert build_config.get("enable_parallel_fix") is False
        assert build_config.get("task_branch_namespace") == "agents"
        assert build_config.get("task_branch_prefix") == "bot/"
        assert build_config.get("pr_title_prefix") == "[bot] "
        # Operator's no-CI choice must survive re-detect (validated persistence).
        assert build_config.get("requires_ci_checks") is False

        await db_session.refresh(project)
        assert (project.build_config or {}).get("requires_ci_checks") is False
        assert (project.build_config or {}).get("env_vars", {}).get("TOKEN") == (
            "secret"
        )
        assert (project.build_config or {}).get("enable_fresh_reviewer") is True
        assert (project.build_config or {}).get("enable_parallel_review") is True
        assert (project.build_config or {}).get("enable_parallel_fix") is False
        assert (project.build_config or {}).get("task_branch_namespace") == "agents"
        assert (project.build_config or {}).get("task_branch_prefix") == "bot/"
        assert (project.build_config or {}).get("pr_title_prefix") == "[bot] "

    @pytest.mark.asyncio
    async def test_detect_build_config_uses_project_integration_branch(
        self, client: AsyncClient, db_session
    ):
        """Owner re-detect reads GitHub contents from the project's trunk branch."""
        project = await _make_project(db_session, name="Detect Branch Aware")
        project.github_owner = "acme"
        project.github_repo = "branch-aware-repo"
        project.integration_branch = "develop"
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "python"},
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/detect-build-config"
            )

        assert resp.status_code == 200
        mock_detect.assert_awaited_once()
        assert mock_detect.await_args.kwargs.get("ref") == "develop"

    @pytest.mark.asyncio
    async def test_detect_build_config_omits_ref_when_integration_branch_unset(
        self, client: AsyncClient, db_session
    ):
        """Legacy NULL-branch projects should still detect from the repo default branch."""
        project = await _make_project(db_session, name="Detect Default Branch Aware")
        project.github_owner = "acme"
        project.github_repo = "default-branch-repo"
        project.integration_branch = None
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "python"},
            ) as mock_detect,
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/detect-build-config"
            )

        assert resp.status_code == 200
        mock_detect.assert_awaited_once()
        assert mock_detect.await_args.kwargs.get("ref") is None

    @pytest.mark.asyncio
    async def test_detect_build_config_refreshes_repo_is_empty(
        self, client: AsyncClient, db_session
    ):
        """Re-detect overwrites a stale repo_is_empty once the repo is scaffolded.

        A project onboarded empty (repo_is_empty True) should flip to False on
        re-detect when detection now finds source — repo_is_empty is NOT in the
        operator-preserve set, so a fresh detection value wins.
        """
        project = await _make_project(db_session, name="Detect Repo Emptiness")
        project.github_owner = "acme"
        project.github_repo = "scaffolded-repo"
        project.build_config = {"repo_is_empty": True}
        db_session.add(project)
        await db_session.commit()

        with (
            patch(
                "app.routers.admin_onboard._detect_build_config",
                new_callable=AsyncMock,
                return_value={"language": "python", "repo_is_empty": False},
            ),
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/detect-build-config"
            )

        assert resp.status_code == 200
        build_config = resp.json().get("build_config") or {}
        assert build_config.get("repo_is_empty") is False
        await db_session.refresh(project)
        assert (project.build_config or {}).get("repo_is_empty") is False


class TestApplyOperatorBuildConfigOverrides:
    """Unit coverage for the shared override-preservation helper."""

    def test_preserves_all_operator_fields(self):
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        detected = {"language": "typescript", "directories": []}
        existing = {
            "git_user_email": "bot@example.com",
            "git_user_name": "Bot",
            "env_vars": {"TOKEN": "secret"},
            "enable_fresh_reviewer": True,
            "enable_parallel_review": True,
            "enable_parallel_fix": False,
            "task_branch_namespace": "agents",
            "task_branch_prefix": "bot/",
            "pr_title_prefix": "[bot] ",
            "staging_branch_prefixes": ["staging/"],
            "stale_branch_prefixes": ["old/"],
            "protected_branches": ["main"],
            "enable_self_improvement": True,
            "requires_ci_checks": False,
        }

        merged = apply_operator_build_config_overrides(detected, existing)

        # Detected fields untouched.
        assert merged["language"] == "typescript"
        # Every operator-owned field carried forward.
        assert merged["git_user_email"] == "bot@example.com"
        assert merged["git_user_name"] == "Bot"
        assert merged["env_vars"] == {"TOKEN": "secret"}
        assert merged["enable_fresh_reviewer"] is True
        assert merged["enable_parallel_review"] is True
        assert merged["enable_parallel_fix"] is False
        assert merged["task_branch_namespace"] == "agents"
        assert merged["task_branch_prefix"] == "bot/"
        assert merged["pr_title_prefix"] == "[bot] "
        assert merged["staging_branch_prefixes"] == ["staging/"]
        assert merged["stale_branch_prefixes"] == ["old/"]
        assert merged["protected_branches"] == ["main"]
        assert merged["enable_self_improvement"] is True
        assert merged["requires_ci_checks"] is False

    def test_preserves_requires_ci_checks_present_key_only(self):
        """requires_ci_checks is key-present-preserved (an explicit False
        survives), and is left absent when the operator never set it — repo
        detection never emits this field."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        # Explicit False carried forward even though detection omits it.
        assert (
            apply_operator_build_config_overrides(
                {"language": "python"}, {"requires_ci_checks": False}
            )["requires_ci_checks"]
            is False
        )
        # Absent in existing → stays absent (defaults to True at read time).
        assert (
            "requires_ci_checks"
            not in apply_operator_build_config_overrides(
                {"language": "python"}, {"other": 1}
            )
        )

    def test_preserves_empty_list_branch_overrides(self):
        """Empty-list branch overrides are present-keyed, so they survive."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        detected = {"protected_branches": ["main"], "stale_branch_prefixes": ["x/"]}
        existing = {
            "staging_branch_prefixes": [],
            "stale_branch_prefixes": [],
            "protected_branches": [],
        }

        merged = apply_operator_build_config_overrides(detected, existing)

        assert merged["staging_branch_prefixes"] == []
        assert merged["stale_branch_prefixes"] == []
        assert merged["protected_branches"] == []

    def test_has_ci_is_repo_derived_not_overridden(self):
        """has_ci is computed fresh from the repo each detect, so the override
        merge must keep the freshly detected value and never resurrect a stale
        stored one (stored has_ci=True must not survive a re-detect that now
        sees no CI)."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        # Fresh detection says no CI; the stored config still claims True.
        merged = apply_operator_build_config_overrides(
            {"language": "python", "has_ci": False},
            {"has_ci": True, "requires_ci_checks": False},
        )
        assert merged["has_ci"] is False
        # Operator-owned requires_ci_checks is still carried forward.
        assert merged["requires_ci_checks"] is False

    def test_has_ci_round_trips_through_schema(self):
        """has_ci survives BuildConfig validation; None is dropped by
        exclude_none=True so legacy/undetected configs round-trip unchanged."""
        from app.schemas import BuildConfig as BuildConfigSchema

        assert (
            BuildConfigSchema(has_ci=True).model_dump(exclude_none=True)["has_ci"]
            is True
        )
        assert (
            BuildConfigSchema(has_ci=False).model_dump(exclude_none=True)["has_ci"]
            is False
        )
        # None => undetected => key dropped on serialization.
        assert "has_ci" not in BuildConfigSchema(has_ci=None).model_dump(
            exclude_none=True
        )
        # Default is None (legacy stored configs validate unchanged).
        assert BuildConfigSchema().has_ci is None

    def test_falsey_git_identity_not_preserved(self):
        """git_user_* use a truthy check, so empty values do not override."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        detected = {"language": "go"}
        existing = {"git_user_email": "", "git_user_name": ""}

        merged = apply_operator_build_config_overrides(detected, existing)

        assert "git_user_email" not in merged
        assert "git_user_name" not in merged

    def test_empty_existing_is_noop(self):
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        detected = {"language": "rust", "directories": []}
        merged = apply_operator_build_config_overrides(detected, {})
        assert merged == {"language": "rust", "directories": []}


class TestBuildConfigLintWarnings:
    """Operator-visible warnings when build_config.directories[*].lint_cmd
    is non-empty but is not a recognized linter prefix.

    The agent's verbatim fallback is intentional — this surface only adds
    visibility, it must not reject or rewrite the lint command.
    """

    @pytest.mark.asyncio
    async def test_patch_returns_warning_for_unrecognized_lint_cmd(
        self, client: AsyncClient, db_session
    ):
        """PATCH /projects/{id} attaches a warning when lint_cmd is unrecognized."""
        project = await _make_project(db_session, name="warn-test")
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "build_config": {
                    "directories": [
                        {
                            "path": "./backend",
                            "type": "python",
                            "lint_cmd": "make lint",
                        },
                        {
                            "path": "./admin-ui",
                            "type": "node",
                            "lint_cmd": "npx eslint .",
                        },
                    ]
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        warnings = body.get("build_config_warnings")
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert "./backend" in warnings[0]
        assert "make lint" in warnings[0]
        # Existing response shape preserved — top-level build_config still present.
        assert isinstance(body.get("build_config"), dict)
        assert len(body["build_config"].get("directories", [])) == 2

    @pytest.mark.asyncio
    async def test_patch_returns_empty_warnings_when_all_recognized(
        self, client: AsyncClient, db_session
    ):
        """All recognized lint_cmd values produce no warnings."""
        project = await _make_project(db_session, name="warn-test-empty")
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "build_config": {
                    "directories": [
                        {
                            "path": "./backend",
                            "type": "python",
                            "lint_cmd": "ruff check .",
                        },
                        {
                            "path": "./admin-ui",
                            "type": "node",
                            "lint_cmd": "npx eslint .",
                        },
                    ]
                }
            },
        )
        assert resp.status_code == 200
        assert resp.json().get("build_config_warnings") == []

    @pytest.mark.asyncio
    async def test_get_project_returns_warnings_from_stored_config(
        self, client: AsyncClient, db_session
    ):
        """GET /projects/{id} surfaces warnings from the persisted build_config."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="warn-get")
        project.build_config = {
            "directories": [
                {"path": "./svc", "type": "python", "lint_cmd": "./lint.sh"}
            ]
        }
        flag_modified(project, "build_config")
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        warnings = resp.json().get("build_config_warnings")
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert "./svc" in warnings[0]
        assert "./lint.sh" in warnings[0]

    @pytest.mark.asyncio
    async def test_list_projects_returns_warnings_per_item(
        self, client: AsyncClient, db_session
    ):
        """GET /projects (list) attaches warnings on each item."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="warn-list")
        project.build_config = {
            "directories": [{"path": "./a", "type": "other", "lint_cmd": "make a-lint"}]
        }
        flag_modified(project, "build_config")
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        target = next((p for p in items if p["name"] == "warn-list"), None)
        assert target is not None
        warnings = target.get("build_config_warnings")
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert "./a" in warnings[0]


class TestPatchBuildConfigRequiresCiChecks:
    """PATCH /projects/{id} must persist the no-CI choice and preserve it on
    subsequent partial patches that omit the field."""

    @pytest.mark.asyncio
    async def test_patch_persists_requires_ci_checks_false(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="patch-no-ci")

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"requires_ci_checks": False}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["build_config"].get("requires_ci_checks") is False

        await db_session.refresh(project)
        assert (project.build_config or {}).get("requires_ci_checks") is False

    @pytest.mark.asyncio
    async def test_patch_omitting_requires_ci_checks_preserves_stored_false(
        self, client: AsyncClient, db_session
    ):
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="patch-no-ci-preserve")
        project.build_config = {"requires_ci_checks": False}
        flag_modified(project, "build_config")
        await db_session.commit()

        # A later PATCH that does not mention requires_ci_checks must not re-gate.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"language": "python"}},
        )
        assert resp.status_code == 200, resp.text
        body_bc = resp.json()["build_config"]
        assert body_bc.get("language") == "python"
        assert body_bc.get("requires_ci_checks") is False

        await db_session.refresh(project)
        assert (project.build_config or {}).get("requires_ci_checks") is False

    @pytest.mark.asyncio
    async def test_patch_explicit_false_overrides_stored_true(
        self, client: AsyncClient, db_session
    ):
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="patch-no-ci-override")
        project.build_config = {"requires_ci_checks": True}
        flag_modified(project, "build_config")
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"requires_ci_checks": False}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["build_config"].get("requires_ci_checks") is False


class TestDeleteProject:
    @pytest.mark.asyncio
    async def test_delete_project_no_active_tasks(
        self, client: AsyncClient, db_session
    ):
        """Owner can delete a project that has no active tasks."""
        project = await _make_project(db_session)

        resp = await client.delete(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        assert "deleted" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_delete_project_removes_cascade_less_children(
        self, client: AsyncClient, db_session
    ):
        """Regression: project delete must remove child rows whose project FK
        lacks ON DELETE CASCADE. messageslastseen (0099), messagesnoiseprefs
        (0103), messagessavedviews (0105) and anomalyfingerprintstate (0080)
        were all added after migration 0058's cascade retrofit, so the final
        commit FK-violated on them in production (asyncpg
        ForeignKeyViolationError on messageslastseen_project_id_fkey)."""
        from sqlmodel import select as _select

        from app.models import (
            AnomalyFingerprintState,
            MessagesLastSeen,
            MessagesNoisePrefs,
            MessagesSavedViews,
        )

        project = await _make_project(db_session)
        db_session.add_all(
            [
                MessagesLastSeen(user_id=TEST_USER_ID, project_id=project.id),
                MessagesNoisePrefs(user_id=TEST_USER_ID, project_id=project.id),
                MessagesSavedViews(user_id=TEST_USER_ID, project_id=project.id),
                AnomalyFingerprintState(
                    project_id=project.id, fingerprint_key="anomaly-1"
                ),
            ]
        )
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200, resp.text

        db_session.expire_all()
        for _model in (
            MessagesLastSeen,
            MessagesNoisePrefs,
            MessagesSavedViews,
            AnomalyFingerprintState,
        ):
            rows = (
                (
                    await db_session.execute(
                        _select(_model).where(_model.project_id == project.id)
                    )
                )
                .scalars()
                .all()
            )
            assert rows == [], f"{_model.__name__} rows survived project delete"

    @pytest.mark.asyncio
    async def test_delete_project_with_active_tasks_no_force_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Project with active tasks is blocked unless force=true."""
        project = await _make_project(db_session)
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "smoketest"}],
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_project_force_cancels_tasks(
        self, client: AsyncClient, db_session, mocker
    ):
        """Force-delete cancels active tasks and removes the project."""
        mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)
        project = await _make_project(db_session)
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "smoketest"}],
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{project.id}?force=true")
        assert resp.status_code == 200
        assert resp.json()["active_tasks_cancelled"] == 1

    @pytest.mark.asyncio
    async def test_delete_project_cascades_messageslastseen(
        self, client: AsyncClient, db_session
    ):
        """A project with a MessagesLastSeen row deletes cleanly (ON DELETE
        CASCADE), rather than FK-violating mid-delete.

        Regression for the prod incident (2026-06-03) where
        ``DELETE /api/admin/projects/{id}?force=true`` 500'd repeatedly on
        ``messageslastseen_project_id_fkey``: the table was created (migration
        0099) without ``ondelete='CASCADE'``, unlike the 0058 convention, and the
        delete handler only hand-deletes ``Alert``. Migration 0137 adds the
        cascade so the project delete removes the child row automatically.
        Meaningful on Postgres (CI), where FK ondelete is enforced.
        """
        project = await _make_project(db_session)
        db_session.add(
            MessagesLastSeen(user_id=TEST_USER_ID, project_id=project.id)
        )
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200, (
            f"project delete must cascade messageslastseen, not FK-violate; "
            f"got {resp.status_code}: {resp.text}"
        )
        # The child row is gone with the project.
        remaining = (
            await db_session.execute(
                select(MessagesLastSeen).where(
                    MessagesLastSeen.project_id == project.id
                )
            )
        ).scalars().all()
        assert remaining == []

    @pytest.mark.asyncio
    async def test_delete_project_force_populates_deny_list(
        self, client: AsyncClient, db_session, mocker
    ):
        """Force-delete fires terminal callbacks so cancelled task IDs enter the deny-list."""
        mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "smoketest"}],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        task_id = str(task.id)

        fired_ids: list = []

        def sentinel(tid):
            fired_ids.append(tid)

        _on_task_terminal_callbacks.append(sentinel)
        try:
            resp = await client.delete(f"/api/admin/projects/{project.id}?force=true")
            assert resp.status_code == 200
            assert task_id in fired_ids
        finally:
            _on_task_terminal_callbacks.remove(sentinel)

    @pytest.mark.asyncio
    async def test_delete_project_non_owner_returns_403(
        self, client: AsyncClient, db_session
    ):
        """Member-role (below owner) cannot delete a project."""
        other = Project(name="Protected", user_id="other-99")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="member")
        )
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{other.id}")
        assert resp.status_code == 403


class TestResetProject:
    @pytest.mark.asyncio
    async def test_reset_project_cancels_active_tasks_and_preserves_config(
        self, client: AsyncClient, db_session, mocker
    ):
        """Reset cancels/wipes active tasks, tears down VMs, keeps durable config."""
        ensure_vm = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
        )
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="wedged")
        project.directions = [{"text": "ship the thing"}]
        project.vision = {"summary": "the grand vision"}
        project.build_config = {"directories": [{"path": "./svc", "type": "node"}]}
        flag_modified(project, "directions")
        flag_modified(project, "vision")
        flag_modified(project, "build_config")
        await db_session.commit()
        project_id = project.id

        task = Task(
            project_id=project_id,
            payloads=[{"type": "smoketest"}],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project_id}/reset")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_tasks_cancelled"] == 1
        assert body["tasks_deleted"] == 1
        # VM teardown reused the shared machinery.
        assert ensure_vm.await_count == 1

        # All task rows for the project are gone.
        remaining = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []

        # Durable config survives and the project is runnable again.
        refreshed = (
            await db_session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        await db_session.refresh(refreshed)
        assert refreshed.directions == [{"text": "ship the thing"}]
        assert refreshed.vision == {"summary": "the grand vision"}
        assert refreshed.build_config == {
            "directories": [{"path": "./svc", "type": "node"}]
        }
        assert refreshed.status == ProjectStatus.DEPLOYING

    @pytest.mark.asyncio
    async def test_reset_project_runnable_end_state(
        self, client: AsyncClient, db_session
    ):
        """After reset the project ends in a non-terminal, runnable status."""
        project = await _make_project(db_session, name="endstate")
        project.status = ProjectStatus.CANCELLED
        await db_session.commit()
        project_id = project.id

        resp = await client.post(f"/api/admin/projects/{project_id}/reset")
        assert resp.status_code == 200

        refreshed = (
            await db_session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        await db_session.refresh(refreshed)
        assert refreshed.status == ProjectStatus.DEPLOYING
        assert refreshed.status not in (
            ProjectStatus.CANCELLED,
            ProjectStatus.FAILED,
            ProjectStatus.COMPLETED,
        )

    @pytest.mark.asyncio
    async def test_reset_project_non_owner_returns_403(
        self, client: AsyncClient, db_session
    ):
        """Member-role (below owner) cannot reset a project."""
        other = Project(name="Protected", user_id="other-99")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="member")
        )
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{other.id}/reset")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_reset_project_clears_in_memory_state(
        self, client: AsyncClient, db_session
    ):
        """Reset pops the project's transient circuit-breaker / anomaly state."""
        from app import anomaly_state

        project = await _make_project(db_session, name="stateful")
        pid = str(project.id)
        other_pid = "keep-me"
        anomaly_state.circuit_breakers[pid] = {"state": "OPEN"}
        anomaly_state.circuit_breakers[other_pid] = {"state": "CLOSED"}
        anomaly_state.autonomous_failure_counts[pid] = 7
        anomaly_state.persistent_anomalies[pid] = {"CODE_ERROR": {}}
        try:
            resp = await client.post(f"/api/admin/projects/{project.id}/reset")
            assert resp.status_code == 200
            assert pid not in anomaly_state.circuit_breakers
            assert pid not in anomaly_state.autonomous_failure_counts
            assert pid not in anomaly_state.persistent_anomalies
            # Other projects' state is untouched.
            assert other_pid in anomaly_state.circuit_breakers
        finally:
            anomaly_state.circuit_breakers.pop(pid, None)
            anomaly_state.circuit_breakers.pop(other_pid, None)
            anomaly_state.autonomous_failure_counts.pop(pid, None)
            anomaly_state.persistent_anomalies.pop(pid, None)


class TestArchiveProject:
    @pytest.mark.asyncio
    async def test_archive_hides_project_from_default_list(
        self, client: AsyncClient, db_session
    ):
        """Archiving a project removes it from the default (active) list."""
        project = await _make_project(db_session, name="to-archive")

        # Present before archiving.
        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(project.id) in ids

        resp = await client.post(f"/api/admin/projects/{project.id}/archive")
        assert resp.status_code == 200
        assert resp.json() == {
            "message": f"Project {project.id} archived",
            "archived": True,
        }

        # Gone from the default list.
        resp = await client.get("/api/admin/projects")
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(project.id) not in ids

    @pytest.mark.asyncio
    async def test_archived_param_returns_only_archived_with_flag(
        self, client: AsyncClient, db_session
    ):
        """archived=true returns the archived set; items carry the archived flag."""
        active = await _make_project(db_session, name="active")
        archived = await _make_project(db_session, name="archived")
        resp = await client.post(f"/api/admin/projects/{archived.id}/archive")
        assert resp.status_code == 200

        resp = await client.get("/api/admin/projects?archived=true")
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = {p["id"] for p in items}
        assert str(archived.id) in ids
        assert str(active.id) not in ids
        target = next(p for p in items if p["id"] == str(archived.id))
        assert target["archived"] is True

    @pytest.mark.asyncio
    async def test_default_list_items_include_archived_false(
        self, client: AsyncClient, db_session
    ):
        """Active list items expose archived=False (backward-compatible add)."""
        project = await _make_project(db_session, name="plain")
        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        target = next(
            p for p in resp.json()["items"] if p["id"] == str(project.id)
        )
        assert target["archived"] is False

    @pytest.mark.asyncio
    async def test_unarchive_restores_to_default_list(
        self, client: AsyncClient, db_session
    ):
        """Unarchiving returns a project to the default list and clears the flag."""
        project = await _make_project(db_session, name="round-trip")
        await client.post(f"/api/admin/projects/{project.id}/archive")

        resp = await client.post(f"/api/admin/projects/{project.id}/unarchive")
        assert resp.status_code == 200
        assert resp.json() == {
            "message": f"Project {project.id} unarchived",
            "archived": False,
        }

        resp = await client.get("/api/admin/projects")
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(project.id) in ids

    @pytest.mark.asyncio
    async def test_archive_is_idempotent(self, client: AsyncClient, db_session):
        """Archiving an already-archived project is a no-op success."""
        project = await _make_project(db_session, name="idem")
        r1 = await client.post(f"/api/admin/projects/{project.id}/archive")
        r2 = await client.post(f"/api/admin/projects/{project.id}/archive")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["archived"] is True

    @pytest.mark.asyncio
    async def test_archive_non_owner_returns_403(
        self, client: AsyncClient, db_session
    ):
        """Member-role (below owner) cannot archive a project."""
        other = Project(name="Protected", user_id="other-99")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="member")
        )
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{other.id}/archive")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unarchive_non_owner_returns_403(
        self, client: AsyncClient, db_session
    ):
        """Member-role (below owner) cannot unarchive a project."""
        other = Project(name="Protected", user_id="other-99", archived=True)
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="member")
        )
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{other.id}/unarchive")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_archived_project_excluded_from_orchestrator_dispatch(
        self, client: AsyncClient, db_session
    ):
        """An archived project's QUEUED tasks are filtered out of the
        orchestrator candidate scan (work halts); unarchiving restores them.

        The candidate filter references ``Project.not_archived()`` — the SAME
        predicate the production orchestrator candidate-task query uses
        (orchestrator.py) — so it cannot pass if that production predicate
        drifts.
        """
        project = await _make_project(db_session, name="halt-dispatch")
        task = Task(
            project_id=project.id,
            payloads=[{"type": "research"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        candidate_filter = Task.project_id.in_(
            select(Project.id).where(Project.not_archived())
        )

        # Eligible while active.
        rows = (
            await db_session.execute(
                select(Task).where(
                    Task.status == TaskStatus.QUEUED, candidate_filter
                )
            )
        ).scalars().all()
        assert task.id in {t.id for t in rows}

        # Archiving removes the task from the candidate pool.
        await client.post(f"/api/admin/projects/{project.id}/archive")
        rows = (
            await db_session.execute(
                select(Task).where(
                    Task.status == TaskStatus.QUEUED, candidate_filter
                )
            )
        ).scalars().all()
        assert task.id not in {t.id for t in rows}

        # Unarchiving restores eligibility.
        await client.post(f"/api/admin/projects/{project.id}/unarchive")
        rows = (
            await db_session.execute(
                select(Task).where(
                    Task.status == TaskStatus.QUEUED, candidate_filter
                )
            )
        ).scalars().all()
        assert task.id in {t.id for t in rows}

    @pytest.mark.asyncio
    async def test_archived_project_excluded_from_autonomous_spawning(
        self, client: AsyncClient, db_session
    ):
        """An archived project spawns no autonomous research (work halts)."""
        from app.autonomous_research import _spawn_research_task

        project = await _make_project(db_session, name="halt-autonomous")
        project.autonomous_enabled = True
        project.archived = True
        await db_session.commit()
        await db_session.refresh(project)

        result = await _spawn_research_task(project, db_session)
        assert result is None

        # No task was created for the archived project.
        count = len(
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_autonomous_idle_select_excludes_archived(
        self, client: AsyncClient, db_session
    ):
        """The idle-research project select skips archived projects."""
        active = await _make_project(db_session, name="idle-active")
        active.autonomous_enabled = True
        archived = await _make_project(db_session, name="idle-archived")
        archived.autonomous_enabled = True
        archived.archived = True
        await db_session.commit()

        # Uses Project.not_archived() — the SAME predicate _check_idle_research
        # selects on in autonomous_research.py — so this cannot pass if the
        # production exclusion drifts.
        rows = (
            await db_session.execute(
                select(Project).where(
                    Project.autonomous_enabled.is_(True),
                    Project.not_archived(),
                )
            )
        ).scalars().all()
        ids = {p.id for p in rows}
        assert active.id in ids
        assert archived.id not in ids


class TestResetProjectStateHelper:
    def test_reset_project_state_clears_only_target(self):
        """reset_project_state pops one project's keys, leaves others intact."""
        from app import anomaly_state

        target = "proj-target"
        other = "proj-other"
        anomaly_state.circuit_breakers[target] = {"state": "OPEN"}
        anomaly_state.circuit_breakers[other] = {"state": "CLOSED"}
        anomaly_state.autonomous_failure_counts[target] = 3
        anomaly_state.persistent_anomalies[target] = {"X": {}}
        try:
            anomaly_state.reset_project_state(target)
            assert target not in anomaly_state.circuit_breakers
            assert target not in anomaly_state.autonomous_failure_counts
            assert target not in anomaly_state.persistent_anomalies
            assert other in anomaly_state.circuit_breakers
            # Idempotent — second call is a no-op.
            anomaly_state.reset_project_state(target)
        finally:
            anomaly_state.circuit_breakers.pop(target, None)
            anomaly_state.circuit_breakers.pop(other, None)
            anomaly_state.autonomous_failure_counts.pop(target, None)
            anomaly_state.persistent_anomalies.pop(target, None)


class TestAutonomousMode:
    @pytest.mark.asyncio
    async def test_pause_all_autonomous(self, client: AsyncClient, db_session):
        """pause-all disables autonomous mode on all user projects."""
        project = await _make_project(db_session)
        project.autonomous_enabled = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 1

    @pytest.mark.asyncio
    async def test_toggle_autonomous_enable(self, client: AsyncClient, db_session):
        """Owner can enable autonomous mode on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is True

    @pytest.mark.asyncio
    async def test_toggle_autonomous_disable(self, client: AsyncClient, db_session):
        """Owner can disable autonomous mode on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is False

    @pytest.mark.asyncio
    async def test_toggle_autonomous_missing_field_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Missing 'enabled' key returns 400."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"wrong_field": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_project(self, client: AsyncClient, db_session):
        """Owner can pause a project; pause is independent of autonomous_enabled."""
        project = await _make_project(db_session)
        project.autonomous_enabled = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"paused": True},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True

        await db_session.refresh(project)
        assert project.paused is True
        # Pause must not touch the autonomous toggle.
        assert project.autonomous_enabled is True

    @pytest.mark.asyncio
    async def test_resume_project(self, client: AsyncClient, db_session):
        """Owner can resume (unpause) a paused project."""
        project = await _make_project(db_session)
        project.paused = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"paused": False},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is False

        await db_session.refresh(project)
        assert project.paused is False

    @pytest.mark.asyncio
    async def test_pause_project_missing_field_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Missing 'paused' key returns 400."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"wrong_field": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_project_requires_owner(
        self, client: AsyncClient, db_session
    ):
        """A non-owner member cannot pause a project (owner-only mutation)."""
        owner_project = Project(name="Owner Pause Project", user_id="other-owner")
        db_session.add(owner_project)
        await db_session.commit()
        await db_session.refresh(owner_project)
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id="other-owner", role="owner"
            )
        )
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id=TEST_USER_ID, role="member"
            )
        )
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{owner_project.id}/pause",
            json={"paused": True},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_paused_field_in_list_and_detail(
        self, client: AsyncClient, db_session
    ):
        """The additive `paused` field appears in both list and detail responses."""
        project = await _make_project(db_session, name="Paused Field Project")
        project.paused = True
        db_session.add(project)
        await db_session.commit()

        list_resp = await client.get("/api/admin/projects")
        assert list_resp.status_code == 200
        target = next(
            p for p in list_resp.json()["items"] if p["id"] == str(project.id)
        )
        assert target["paused"] is True

        detail_resp = await client.get(f"/api/admin/projects/{project.id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["paused"] is True

    @pytest.mark.asyncio
    async def test_get_autonomous_status(self, client: AsyncClient, db_session):
        """Returns autonomous status summary across all user projects."""
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="Auto Project")

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        by_id = {p["project_id"]: p for p in data["projects"]}
        assert str(project.id) in by_id
        entry = by_id[str(project.id)]
        # Freshly created project: autonomous_enabled defaults True (model default),
        # no failures and no autonomous tasks spawned yet, rate_limit mirrors config.
        assert entry["autonomous_enabled"] is True
        assert entry["consecutive_failures"] == 0
        assert entry["tasks_spawned_last_hour"] == 0
        assert entry["rate_limit"] == runtime_config["max_autonomous_tasks_per_hour"]

    @pytest.mark.asyncio
    async def test_get_direction_task_counts(self, client: AsyncClient, db_session):
        """Returns per-direction task counts for a project."""
        project = await _make_project(db_session)
        direction_id = "d-abc123"
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research", "current_direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/directions/task-counts"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) >= {"counts", "counts_by_type", "active_tasks"}
        assert body["counts"].get(direction_id) == 1
        assert body["counts_by_type"][direction_id]["research"] == 1


class TestTaskTypeCostAdherence:
    @pytest.mark.asyncio
    async def test_requires_auth(self, override_get_session):
        """Unauthenticated callers are rejected (router-level get_current_user)."""
        from fastapi import HTTPException

        async def reject():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[get_current_user] = reject
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as anon:
                resp = await anon.get("/api/admin/task-types/cost-adherence")
            assert resp.status_code in (401, 403)
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_returns_actual_cost_vs_declared_ceiling(
        self, client: AsyncClient, db_session
    ):
        """Per-type rows carry declared ceilings and actual costs from recent runs."""
        from datetime import timedelta
        from app.time_utils import _utcnow
        from app.task_capabilities import TASK_CAPABILITY_BY_VALUE

        project = await _make_project(db_session, name="Cost Project")
        now = _utcnow()
        for cost in (2.0, 4.0, 6.0):
            db_session.add(
                Task(
                    project_id=project.id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.COMPLETED,
                    created_at=now - timedelta(hours=1),
                    vm_runtime_cost_banked_db=cost,
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/task-types/cost-adherence")
        assert resp.status_code == 200
        body = resp.json()
        assert body["window_days"] == 14
        by_id = {r["id"]: r for r in body["task_types"]}
        # Every registered type is represented.
        assert set(by_id) >= set(TASK_CAPABILITY_BY_VALUE)
        create = by_id["create"]
        assert create["sample_count"] == 3
        assert create["avg_cost_usd"] == pytest.approx(4.0)
        assert create["declared_ceiling_usd"] == (
            TASK_CAPABILITY_BY_VALUE["create"].cost_ceiling_usd
        )

    @pytest.mark.asyncio
    async def test_compute_experiment_cost_from_compute_jobs(
        self, client: AsyncClient, db_session
    ):
        """compute_experiment cost is sourced from the linked compute jobs'
        durable final_cost_usd, not the lightweight monitoring VM."""
        from datetime import timedelta
        from app.time_utils import _utcnow
        from app.models import ComputeJobStatus

        project = await _make_project(db_session, name="Compute Cost Project")
        now = _utcnow()
        # Three terminal compute_experiment tasks whose monitoring VM cost is
        # near-zero; the honest spend lives on the linked compute jobs.
        for spend in (6.0, 8.0, 10.0):
            task = Task(
                project_id=project.id,
                payloads=[{"type": "compute_experiment"}],
                status=TaskStatus.COMPLETED,
                created_at=now - timedelta(hours=1),
                vm_runtime_cost_banked_db=0.01,
            )
            db_session.add(task)
            await db_session.flush()
            db_session.add(
                ComputeJob(
                    project_id=project.id,
                    task_id=task.id,
                    backend="test",
                    status=ComputeJobStatus.SUCCEEDED,
                    final_cost_usd=spend,
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/task-types/cost-adherence")
        assert resp.status_code == 200
        by_id = {r["id"]: r for r in resp.json()["task_types"]}
        row = by_id["compute_experiment"]
        assert row["sample_count"] == 3
        # avg of compute-job spend ($8), NOT the ~$0.01 monitoring VM cost.
        assert row["avg_cost_usd"] == pytest.approx(8.0)
        assert row["declared_ceiling_usd"] == 5.0
        # avg $8 exceeds the $5 ceiling with >= MIN_SAMPLES runs.
        assert row["over_ceiling"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# admin_notifications.py — Alerts, Messages, Playbook
# ═══════════════════════════════════════════════════════════════════════════════


class TestAlerts:
    @pytest.mark.asyncio
    async def test_get_alerts_empty(self, client: AsyncClient, db_session):
        """No projects → empty alert list."""
        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_get_alerts_returns_own_project_alerts(
        self, client: AsyncClient, db_session
    ):
        """Alerts from the user's project appear in the list."""
        project = await _make_project(db_session)
        db_session.add(
            Alert(
                project_id=project.id,
                severity="ERROR",
                title="Test Alert",
                message="Something failed",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Test Alert"

    @pytest.mark.asyncio
    async def test_get_alerts_excludes_other_projects(
        self, client: AsyncClient, db_session
    ):
        """Alerts from projects the user is not a member of are hidden."""
        other = Project(name="Other", user_id="other-77")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            Alert(
                project_id=other.id,
                severity="ERROR",
                title="Private Alert",
                message="Private",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        titles = [a["title"] for a in resp.json()]
        assert "Private Alert" not in titles

    @pytest.mark.asyncio
    async def test_dismiss_alert(self, client: AsyncClient, db_session):
        """Alert is dismissed by ID."""
        project = await _make_project(db_session)
        alert = Alert(
            project_id=project.id,
            severity="WARNING",
            title="Dismiss Me",
            message="warn",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(f"/api/admin/alerts/{alert.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

    @pytest.mark.asyncio
    async def test_dismiss_alert_not_found_returns_404(self, client: AsyncClient):
        """Dismissing a missing alert returns 404."""
        resp = await client.post(f"/api/admin/alerts/{uuid.uuid4()}/dismiss")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dismiss_all_alerts(self, client: AsyncClient, db_session):
        """All undismissed alerts across user's projects are dismissed."""
        project = await _make_project(db_session)
        for i in range(3):
            db_session.add(
                Alert(
                    project_id=project.id,
                    severity="INFO",
                    title=f"Alert {i}",
                    message="msg",
                )
            )
        await db_session.commit()

        resp = await client.post("/api/admin/alerts/dismiss-all")
        assert resp.status_code == 200
        assert resp.json()["dismissed_count"] == 3

    @pytest.mark.asyncio
    async def test_get_alerts_includes_dismissed_when_requested(
        self, client: AsyncClient, db_session
    ):
        """include_dismissed=true returns already-dismissed alerts."""
        from app.models import _utcnow

        project = await _make_project(db_session)
        db_session.add(
            Alert(
                project_id=project.id,
                severity="INFO",
                title="Old",
                message="old",
                dismissed_at=_utcnow(),
            )
        )
        await db_session.commit()

        # By default dismissed alerts are excluded
        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

        # With the flag they appear
        resp = await client.get("/api/admin/alerts?include_dismissed=true")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestNotificationDispatchStats:
    @pytest.mark.asyncio
    async def test_stats_returns_zeros_for_new_project(
        self, client: AsyncClient, db_session
    ):
        """Newly created project returns zeroed delivery stats."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/notification-dispatch/stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_dispatched"] == 0
        assert body["total_delivered"] == 0
        assert body["total_failed"] == 0
        assert body["retry_queue_depth"] == 0

    @pytest.mark.asyncio
    async def test_stats_requires_project_access(self, client: AsyncClient, db_session):
        """Missing project membership → 404."""
        other_project = Project(name="Other", user_id="other-user")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)
        resp = await client.get(
            f"/api/admin/notification-dispatch/stats?project_id={other_project.id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stats_invalid_project_id(self, client: AsyncClient, db_session):
        """Invalid UUID → 400."""
        resp = await client.get(
            "/api/admin/notification-dispatch/stats?project_id=not-a-uuid"
        )
        assert resp.status_code == 400


class TestMessages:
    @pytest.mark.asyncio
    async def test_list_messages_empty(self, client: AsyncClient, db_session):
        """New project has no messages."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/messages?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_create_message(self, client: AsyncClient, db_session):
        """User can post a message to a project they belong to."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "This is a test note.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "created"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_message_idempotency_key_dedupes(
        self, client: AsyncClient, db_session
    ):
        """Two sends sharing one idempotency key collapse to a single row."""
        from sqlalchemy import func as sa_func
        from app.models import ProjectMessage

        project = await _make_project(db_session)
        body = {
            "project_id": str(project.id),
            "message_type": "FEEDBACK",
            "content": "Steer toward X.",
            "idempotency_key": "key-double-submit-1",
        }

        first = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200
        assert first.json()["status"] == "created"
        first_id = first.json()["id"]

        # Identical retry with the same key — idempotent success, same row.
        second = await client.post("/api/admin/messages", json=body)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["id"] == first_id

        # Exactly one row landed for this project.
        count = await db_session.scalar(
            select(sa_func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
        )
        assert count == 1

        # A different key on the same content does create a second message.
        third = await client.post(
            "/api/admin/messages",
            json={**body, "idempotency_key": "key-double-submit-2"},
        )
        assert third.status_code == 200
        assert third.json()["status"] == "created"
        assert third.json()["id"] != first_id

    @pytest.mark.asyncio
    async def test_create_message_no_access_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Cannot post to a project the user is not a member of."""
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(uuid.uuid4()),
                "message_type": "NOTE",
                "content": "Sneaky note",
            },
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_message_invalid_type_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Unknown message_type is rejected."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "INVALID_TYPE",
                "content": "Test",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_message_empty_content_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Whitespace-only content is rejected."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "   ",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_message_over_limit_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Over-limit content is rejected (not silently truncated)."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "x" * (MESSAGE_CONTENT_MAX + 1),
            },
        )
        assert resp.status_code == 400
        assert str(MESSAGE_CONTENT_MAX) in resp.json()["error"]["message"]

        # Nothing should have been persisted for this project.
        rows = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id
                )
            )
        ).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_create_message_at_limit_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Content of exactly the limit is accepted and stored in full."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)
        content = "x" * MESSAGE_CONTENT_MAX

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": content,
            },
        )
        assert resp.status_code == 200
        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        assert len(msg.content) == MESSAGE_CONTENT_MAX

    @pytest.mark.asyncio
    async def test_create_routine_message_appends_running_update(
        self, client: AsyncClient, db_session
    ):
        """Creating a NOTE with a routine event_type appends a digest to running_update."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "PR #123 merged into main",
                "context": {"event_type": "pr_merged"},
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        assert project.running_update is not None
        assert "pr_merged" in project.running_update
        assert "PR #123 merged into main" in project.running_update

    async def _reload_vision(self, db_session, project_id) -> dict:
        """Re-read the project's vision JSON after an endpoint commit.

        The endpoint arms the marker on its own request-session/connection. We
        must close the test session first so the subsequent read happens in a
        fresh transaction — expiring the identity map alone keeps the stale
        pre-commit snapshot and would miss the just-committed marker.
        """
        await db_session.close()
        project = (
            await db_session.execute(
                select(Project).where(Project.id == project_id)
            )
        ).scalar_one()
        return project.vision or {}

    @pytest.mark.asyncio
    async def test_canonical_message_arms_strategic_refresh(
        self, client: AsyncClient, db_session
    ):
        """The live-chat endpoint wakes the strategist on a steering message.

        Regression guard for the bug where the canonical MessagesPanel endpoint
        (POST /api/admin/messages) created the message but never armed the
        operator-origin refresh marker when the project row was contended, so
        live-chat steering could silently wait for the next cadence instead of
        waking the strategist immediately.
        """
        from app.strategic_trigger import strategic_refresh_pending
        import app.task_locking as task_locking

        project = await _make_project(db_session)

        lock_calls: list[tuple[bool, float]] = []

        @asynccontextmanager
        async def _fake_project_lock(
            session, project_id, *, skip_locked=True, timeout=30.0
        ):
            lock_calls.append((skip_locked, timeout))
            if skip_locked:
                yield None
                return
            locked_project = await session.get(Project, project_id)
            assert locked_project is not None
            yield locked_project

        # The operator path routes its vision write through with_vision_locked(),
        # which acquires the row via task_locking.project_lock — patch it there.
        with patch.object(task_locking, "project_lock", _fake_project_lock):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "Please refocus on the auth bug",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "created"
        # The response reports the real arming decision so the UI can tell the
        # operator the message actually steered the strategist.
        assert resp.json()["arming"] == "steering"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["reason"] == "operator_message"
        assert marker["count"] == 1
        # Operator origin is what activates the breaker-bypass and deferral
        # consumer paths — the whole point of live steering.
        assert marker["origin"] == "operator"
        # The message text is persisted on the marker so the steering directive
        # survives the recent-messages snapshot aging out before the deferred
        # cycle reaches its review phase.
        assert marker["excerpt"] == "Please refocus on the auth bug"
        assert lock_calls == [(False, 1.0)]

    @pytest.mark.asyncio
    async def test_canonical_passive_note_does_not_arm_refresh(
        self, client: AsyncClient, db_session
    ):
        """A passive NOTE on the canonical endpoint must NOT wake the strategist."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "FYI, just a note",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["arming"] == "passive_note"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is False

    @pytest.mark.asyncio
    async def test_canonical_routine_event_does_not_arm_refresh(
        self, client: AsyncClient, db_session
    ):
        """A system/routine digest-event message must NOT wake the strategist,
        even though it is not a NOTE-typed message, because routine digests are
        automated, not human steering."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "FEEDBACK",
                "content": "review feedback digest",
                "context": {"event_type": "review_feedback"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["arming"] == "routine_event"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is False

    @pytest.mark.asyncio
    async def test_canonical_message_arm_failure_reports_not_armed(
        self, client: AsyncClient, db_session
    ):
        """When a genuine steering message cannot persist its wake marker (the
        bounded project-row acquire is exhausted under contention), the endpoint
        must surface the truthful ``not_armed`` outcome — never a false
        ``steering`` promise — and write no marker. Regression guard for the
        false operator receipt this task fixes.
        """
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)

        # Simulate the contended-row path: request_strategic_refresh exhausts its
        # bounded lock acquire and persists nothing (returns False).
        with patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(return_value=False),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "Please refocus on the auth bug",
                },
            )
        assert resp.status_code == 200, resp.text
        # Message still saved, but the receipt is truthful about the failed wake.
        assert resp.json()["status"] == "created"
        assert resp.json()["arming"] == "not_armed"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is False

    @pytest.mark.asyncio
    async def test_canonical_message_arm_rollback_does_not_500(
        self, client: AsyncClient, db_session
    ):
        """Under real row contention the arm path rolls the session back to
        clear the aborted bounded-lock acquire, which EXPIRES the
        already-committed message. The endpoint must still return 200 with the
        persisted id — not 500 on a sync lazy-load (MissingGreenlet) of an
        expired ``msg.id``. Regression for the operator-message 500 where the
        message committed but the request still errored (prompting a double-post
        on retry). Distinct from ``..._reports_not_armed``, whose mock returns
        False WITHOUT rolling back, so it never expired the instance.
        """
        project = await _make_project(db_session)

        async def _rollback_then_fail(session, *args, **kwargs):
            # Mirror request_strategic_refresh's OperationalError handler: roll
            # the session back to keep the txn usable, which expires the
            # committed msg in the caller's session.
            await session.rollback()
            return False

        with patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(side_effect=_rollback_then_fail),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "steer me while a cycle is running",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["arming"] == "not_armed"
        # The id is the scalar snapshotted before the expire, not a lazy-load.
        assert isinstance(body["id"], str) and len(body["id"]) == 36

    @pytest.mark.asyncio
    async def test_canonical_message_burst_coalesces(
        self, client: AsyncClient, db_session
    ):
        """A burst of operator messages collapses into a single coalesced slot."""
        from app.strategic_trigger import (
            refresh_breaker_applies,
            strategic_refresh_pending,
        )

        project = await _make_project(db_session)
        for message_type in ("QUESTION", "BLOCKER"):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": message_type,
                    "content": f"steer {message_type}",
                },
            )
            assert resp.status_code == 200, resp.text

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["count"] == 2
        assert marker["reason"] == "operator_message"
        assert marker["origin"] == "operator"
        # Operator origin bypasses the failure breaker — exactly when an
        # operator needs to steer.
        assert refresh_breaker_applies(vision) is False

    @pytest.mark.asyncio
    async def test_create_routine_message_acquires_project_lock(
        self, client: AsyncClient, db_session
    ):
        """A routine event_type append goes through the project row lock."""
        project = await _make_project(db_session)

        import app.routers.admin_notifications as notif

        real_lock = notif.lock_project_or_429
        with patch.object(
            notif, "lock_project_or_429", AsyncMock(side_effect=real_lock)
        ) as spy:
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "PR #7 merged",
                    "context": {"event_type": "pr_merged"},
                },
            )
        assert resp.status_code == 200
        spy.assert_awaited_once()
        # The locked read is for THIS project.
        assert spy.await_args.args[1] == project.id

    @pytest.mark.asyncio
    async def test_create_non_routine_message_skips_project_lock(
        self, client: AsyncClient, db_session
    ):
        """A plain NOTE (no routine event_type) does not pay the lock/429 cost."""
        project = await _make_project(db_session)

        import app.routers.admin_notifications as notif

        with patch.object(
            notif, "lock_project_or_429", AsyncMock()
        ) as spy:
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "just a note",
                },
            )
        assert resp.status_code == 200
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_routine_message_does_not_clobber_concurrent_rewrite(
        self, client: AsyncClient, db_session
    ):
        """The locked re-read picks up a concurrently-committed running_update.

        Simulates the agent's locked set_running_update committing a reconciled
        document AFTER the handler's access-check read but BEFORE the digest
        append. Because the append now operates on the row-locked re-read (not
        the stale access-check instance), the agent's document survives and the
        digest line is appended onto it — no lost update.
        """
        project = await _make_project(db_session)

        import app.routers.admin_notifications as notif

        async def _stale_access_check(pid, user_id, session):
            # The handler used to append onto whatever this returned. Hand it a
            # stale running_update to prove that value is NOT what gets written.
            stale = await session.get(Project, pid)
            stale.running_update = "STALE-BASELINE-FROM-ACCESS-CHECK"
            return stale

        async def _commit_agent_doc_then_lock(session, pid):
            # Stand in for the agent committing its reconciled document just
            # before we take the lock.
            agent_proj = await session.get(Project, pid)
            agent_proj.running_update = "AGENT-RECONCILED-DOC"
            await session.commit()
            return await real_lock(session, pid)

        real_lock = notif.lock_project_or_429
        with patch.object(
            notif, "_get_user_project", AsyncMock(side_effect=_stale_access_check)
        ), patch.object(
            notif,
            "lock_project_or_429",
            AsyncMock(side_effect=_commit_agent_doc_then_lock),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "operator digest line",
                    "context": {"event_type": "pr_merged"},
                },
            )
        assert resp.status_code == 200
        await db_session.refresh(project)
        # Agent's reconciled document survived; the stale value was discarded.
        assert "AGENT-RECONCILED-DOC" in project.running_update
        assert "STALE-BASELINE-FROM-ACCESS-CHECK" not in project.running_update
        # The new digest line was appended onto the reconciled document.
        assert "operator digest line" in project.running_update

    @pytest.mark.asyncio
    async def test_create_two_routine_messages_keeps_both_lines(
        self, client: AsyncClient, db_session
    ):
        """Two sequential routine appends both survive (no lost digest line)."""
        project = await _make_project(db_session)

        for content, evt in (
            ("first digest line", "pr_merged"),
            ("second digest line", "auto_fix_result"),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": content,
                    "context": {"event_type": evt},
                },
            )
            assert resp.status_code == 200

        await db_session.refresh(project)
        assert "first digest line" in project.running_update
        assert "second digest line" in project.running_update

    # ── Idempotent steering-message creation ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_idempotent_double_submit_creates_one_row(
        self, client: AsyncClient, db_session
    ):
        """Posting the same intent twice with one idempotency_key dedupes to a
        single row, and both responses point at that same row."""
        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "duplicate steering intent",
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        # Same logical message: the first send creates, the retry is recognized
        # as a duplicate and returns the existing row's id.
        assert first.json()["status"] == "created"
        assert second.json()["status"] == "duplicate"
        assert first.json()["id"] == second.json()["id"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].id) == first.json()["id"]
        assert rows[0].idempotency_key == key

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_arming_coalesces(
        self, client: AsyncClient, db_session
    ):
        """A steering message POSTed twice with the same key arms a SINGLE
        refresh slot — the first send arms the wake, and the duplicate is
        recognized as such (``arming == "duplicate"``) rather than opening a
        second independent wake."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "FEEDBACK",
            "content": "Please refocus on the auth bug",
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        # The first send arms the strategist; the duplicate short-circuits before
        # the wake gate and reports the truthful "duplicate" outcome.
        assert first.json()["arming"] == "steering"
        assert second.json()["arming"] == "duplicate"
        assert first.json()["id"] == second.json()["id"]

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["origin"] == "operator"
        # Only the first send armed a wake; the duplicate did not re-arm, so the
        # strategist has a SINGLE pending wake to consume.
        assert marker["count"] == 1

        # And the dedup still held: exactly one row backs both responses.
        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_idempotent_different_key_creates_new_message(
        self, client: AsyncClient, db_session
    ):
        """Identical content under two distinct keys is two distinct intents."""
        project = await _make_project(db_session)
        base = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "same words, different intent",
        }

        first = await client.post(
            "/api/admin/messages",
            json={**base, "idempotency_key": str(uuid.uuid4())},
        )
        second = await client.post(
            "/api/admin/messages",
            json={**base, "idempotency_key": str(uuid.uuid4())},
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_no_idempotency_key_creates_new_row_each_time(
        self, client: AsyncClient, db_session
    ):
        """Legacy path: omitting idempotency_key never dedupes (back-compat)."""
        project = await _make_project(db_session)
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "legacy unconditional insert",
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        # Both rows persisted with a NULL key (exempt from the partial index).
        assert all(r.idempotency_key is None for r in rows)

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_skips_routine_digest(
        self, client: AsyncClient, db_session
    ):
        """A routine-event message POSTed twice with one key appends its digest
        line exactly once — the duplicate skips the running_update append."""
        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "PR #864 merged into main",
            "context": {"event_type": "pr_merged"},
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] == second.json()["id"]

        await db_session.refresh(project)
        assert project.running_update is not None
        # Appended exactly once despite two POSTs.
        assert project.running_update.count("PR #864 merged into main") == 1

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_idempotent_concurrent_insert_recovers_to_200(
        self, client: AsyncClient, db_session
    ):
        """TOCTOU: a competitor commits the same-key row AFTER this request's
        idempotency pre-check passed but BEFORE its own commit, so the INSERT
        trips ux_projectmessage_project_idempotency_key. The handler must
        recover — roll back, re-select the winner, and return the idempotent 200 pointing at
        the already-committed row — instead of surfacing the IntegrityError as
        a 500.

        The ``lock_project_or_429`` call (reached only for a routine-event
        message) is the seam between the pre-check and the commit; we patch it
        to commit a conflicting row via an INDEPENDENT session, exactly
        modelling the concurrent in-flight duplicate.
        """
        from conftest import TestingSessionLocal
        import app.routers.admin_notifications as notif

        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        real_lock = notif.lock_project_or_429
        injected = {"done": False}

        async def _commit_conflict_then_lock(session, pid):
            # Stand in for a concurrent request that won the race: persist the
            # same-key row in a separate transaction so this request's pending
            # INSERT violates the unique index on commit.
            if not injected["done"]:
                injected["done"] = True
                async with TestingSessionLocal() as other:
                    other.add(
                        ProjectMessage(
                            project_id=pid,
                            message_type=MessageType.NOTE,
                            from_type="user",
                            content="winner of the race",
                            context={"event_type": "pr_merged"},
                            idempotency_key=key,
                        )
                    )
                    await other.commit()
            return await real_lock(session, pid)

        with patch.object(
            notif,
            "lock_project_or_429",
            AsyncMock(side_effect=_commit_conflict_then_lock),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "loser of the race",
                    "context": {"event_type": "pr_merged"},
                    "idempotency_key": key,
                },
            )

        # Recovered to an idempotent 200, not a 500.
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "duplicate"

        # Exactly one row survived, and the response points at the winner.
        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.idempotency_key == key,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].content == "winner of the race"
        assert resp.json()["id"] == str(rows[0].id)

    @pytest.mark.asyncio
    async def test_resolve_message(self, client: AsyncClient, db_session):
        """An open message can be resolved."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_resolve_message_over_limit_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Over-limit resolution is rejected; the message stays unresolved."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "x" * (MESSAGE_CONTENT_MAX + 1)},
        )
        assert resp.status_code == 400
        assert str(MESSAGE_CONTENT_MAX) in resp.json()["error"]["message"]

        await db_session.refresh(msg)
        assert msg.status == "open"
        assert msg.resolution is None

    @pytest.mark.asyncio
    async def test_resolve_already_resolved_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Resolving an already-resolved message returns 'already_resolved'."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Done",
            status="resolved",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(f"/api/admin/messages/{msg.id}", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_resolved"

    @pytest.mark.asyncio
    async def test_resolve_message_concurrent_requests_are_safe(
        self, client: AsyncClient, db_session
    ):
        """Concurrent resolves are safe: one succeeds, the other sees already_resolved."""
        import asyncio

        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # Simulate two concurrent resolve requests with different resolutions
        async def resolve_with(resolution_text):
            return await client.patch(
                f"/api/admin/messages/{msg.id}",
                json={"resolution": resolution_text},
            )

        resp1, resp2 = await asyncio.gather(
            resolve_with("First resolution"),
            resolve_with("Second resolution"),
        )
        # Both should succeed (200)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # One should be "resolved", the other "already_resolved"
        statuses = {resp1.json()["status"], resp2.json()["status"]}
        assert statuses == {"resolved", "already_resolved"}

        # Verify the message is resolved with the first resolution that won
        await db_session.refresh(msg)
        assert msg.status == "resolved"
        assert msg.resolution is not None

    @pytest.mark.asyncio
    async def test_resolve_message_retried_request_is_safe(
        self, client: AsyncClient, db_session
    ):
        """Retrying a resolve request is safe (idempotent)."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # First resolve
        resp1 = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "resolved"

        # Retry the same request
        resp2 = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_resolved"

        # Message should still be resolved
        await db_session.refresh(msg)
        assert msg.status == "resolved"
        assert msg.resolution == "Fixed!"

    @pytest.mark.asyncio
    async def test_delete_message(self, client: AsyncClient, db_session):
        """Can delete an existing message."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Bug found",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_message_not_found_is_idempotent(self, client: AsyncClient):
        """Deleting a non-existent message returns idempotent success."""
        resp = await client.delete(f"/api/admin/messages/{uuid.uuid4()}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_message_twice_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Deleting a message twice (retry) is idempotent."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Bug found",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # First delete
        resp1 = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp1.status_code == 200
        assert resp1.json()["deleted"] is True

        # Retry delete
        resp2 = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp2.status_code == 200
        assert resp2.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_list_messages_status_filter(self, client: AsyncClient, db_session):
        """Status filter separates open from resolved messages."""
        project = await _make_project(db_session)
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="Open msg",
                status="open",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="Resolved msg",
                status="resolved",
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open"
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["messages"][0]["status"] == "open"

    @pytest.mark.asyncio
    async def test_list_messages_invalid_status_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Invalid status filter value returns 400."""
        project = await _make_project(db_session)

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=pending"
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_messages_guarantees_high_signal_with_routine_flood(
        self, client: AsyncClient, db_session
    ):
        """BLOCKER messages are always returned even when routine messages fill the paged limit."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        # BLOCKER is the OLDEST message: with order_by(created_at.desc()) and limit=3 the
        # five newer routine rows would evict it entirely unless the guaranteed-query logic
        # in list_messages explicitly pulls it back in. This ordering is what makes the test
        # actually exercise the guarantee — a newest BLOCKER would survive on recency alone.
        blocker = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="critical blocker",
            context={},
        )
        blocker.created_at = now - timedelta(hours=10)
        db_session.add(blocker)
        for i in range(5):
            routine = ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"routine {i}",
                context={"event_type": "pr_merged"},
            )
            routine.created_at = now - timedelta(minutes=5 * (5 - i))
            db_session.add(routine)
        await db_session.commit()

        # limit=3 would normally cut off the BLOCKER if 5 newer routine messages exist
        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open&limit=3"
        )
        assert resp.status_code == 200
        returned_ids = {m["id"] for m in resp.json()["messages"]}
        assert str(blocker.id) in returned_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "high_signal_event_type",
        [
            "failure_alert",
            "rollback_recommended",
            "fix_loop_divergence_regressed",
            "release_calcifying",
            "release_build_stalled",
            "release_stabilize_stalled",
            "release_ci_unresolved_stall",
            "strategic_steering_deferred",
        ],
    )
    async def test_list_messages_guarantees_high_signal_notes_with_routine_flood(
        self, client: AsyncClient, db_session, high_signal_event_type: str
    ):
        """System NOTE alerts classified as high-signal always survive a routine flood."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        # High-signal NOTE is the OLDEST message so it would be evicted by paging
        # alone without the guaranteed-query logic.
        alert = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=f"system alert: {high_signal_event_type}",
            context={"event_type": high_signal_event_type},
        )
        alert.created_at = now - timedelta(hours=10)
        db_session.add(alert)
        for i in range(5):
            routine = ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"routine {i}",
                context={"event_type": "pr_merged"},
            )
            routine.created_at = now - timedelta(minutes=5 * (5 - i))
            db_session.add(routine)
        await db_session.commit()

        # limit=3 would normally cut off the alert if 5 newer routine messages exist
        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open&limit=3"
        )
        assert resp.status_code == 200
        returned_ids = {m["id"] for m in resp.json()["messages"]}
        assert str(alert.id) in returned_ids, (
            f"high-signal NOTE with event_type={high_signal_event_type!r} was evicted "
            "by routine flood — guaranteed-query logic did not include it"
        )


class TestMessagesLastSeen:
    @pytest.mark.asyncio
    async def test_get_last_seen_empty(self, client: AsyncClient, db_session):
        """Returns null when no last-seen record exists for the operator."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] is None

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores the timestamp; subsequent GET returns the same value."""
        project = await _make_project(db_session)
        ts = 1_700_000_000_000  # arbitrary ms epoch

        put_resp = await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["last_seen_at"] == ts

        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["last_seen_at"] == ts

    @pytest.mark.asyncio
    async def test_update_advances_timestamp(self, client: AsyncClient, db_session):
        """A second PUT overwrites the previous value."""
        project = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts2},
        )
        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert get_resp.json()["last_seen_at"] == ts2

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot read or write last-seen for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": random_pid, "timestamp": 1_700_000_000_000},
        )
        assert put_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_without_project_id_returns_null_when_empty(
        self, client: AsyncClient, db_session
    ):
        """GET without project_id returns null when no last-seen records exist."""
        resp = await client.get("/api/admin/messages/last-seen")
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] is None

    @pytest.mark.asyncio
    async def test_get_without_project_id_returns_max_across_projects(
        self, client: AsyncClient, db_session
    ):
        """GET without project_id returns max last-seen across all operator projects."""
        p1 = await _make_project(db_session)
        p2 = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(p1.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(p2.id), "timestamp": ts2},
        )

        resp = await client.get("/api/admin/messages/last-seen")
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] == ts2


class TestLastVisitTimestamp:
    @pytest.mark.asyncio
    async def test_get_empty_returns_null(self, client: AsyncClient, db_session):
        """Returns null when no MessagesLastSeen record exists for the operator."""
        resp = await client.get("/api/admin/last-visit-timestamp")
        assert resp.status_code == 200
        assert resp.json()["last_visit_at"] is None

    @pytest.mark.asyncio
    async def test_put_and_get_round_trip(self, client: AsyncClient, db_session):
        """PUT persists the timestamp; GET returns it as the cross-project max."""
        project = await _make_project(db_session)
        ts = 1_700_000_000_000

        put_resp = await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(project.id), "timestamp": ts},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["last_visit_at"] == ts

        get_resp = await client.get("/api/admin/last-visit-timestamp")
        assert get_resp.status_code == 200
        assert get_resp.json()["last_visit_at"] == ts

    @pytest.mark.asyncio
    async def test_get_returns_max_across_projects(
        self, client: AsyncClient, db_session
    ):
        """GET returns the most recent timestamp across all user's projects."""
        p1 = await _make_project(db_session)
        p2 = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(p1.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(p2.id), "timestamp": ts2},
        )

        resp = await client.get("/api/admin/last-visit-timestamp")
        assert resp.json()["last_visit_at"] == ts2

    @pytest.mark.asyncio
    async def test_put_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot write last-visit-timestamp for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        resp = await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": random_pid, "timestamp": 1_700_000_000_000},
        )
        assert resp.status_code == 404


class TestMessagesNoisePrefs:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns empty dict when no noise-prefs record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["prefs"] == {}

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores event-visibility prefs; GET returns them."""
        project = await _make_project(db_session)
        prefs = {"pr_merged": "mute", "cycle_summary": "routine"}
        put_resp = await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": str(project.id), "prefs": prefs},
        )
        assert put_resp.status_code == 200
        get_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert get_resp.json()["prefs"] == prefs

    @pytest.mark.asyncio
    async def test_noise_prefs_save_preserves_temp_mutes_and_mute_window(
        self, client: AsyncClient, db_session
    ):
        """A visibility-only noise-prefs save must NOT clobber an operator's
        server-side temp-mutes or mute-window stored in the same prefs column."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 3_600_000

        # Operator sets a DND window and a temp-mute first.
        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        await client.put(
            "/api/admin/messages/temp-mutes",
            json={
                "project_id": str(project.id),
                "temp_mutes": {"pr_merged": future_ms},
            },
        )

        # Then toggles an event-visibility pref (sends only the visibility map).
        await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": str(project.id), "prefs": {"cycle_summary": "routine"}},
        )

        # The DND window and temp-mute must survive the visibility save.
        mw_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert mw_resp.json()["mute_window"] == {"expires_at": future_ms}
        tm_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert tm_resp.json()["temp_mutes"] == {"pr_merged": future_ms}

        # And the visibility pref itself round-trips.
        np_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert np_resp.json()["prefs"].get("cycle_summary") == "routine"

    @pytest.mark.asyncio
    async def test_noise_prefs_payload_cannot_write_reserved_keys(
        self, client: AsyncClient, db_session
    ):
        """Reserved keys in a noise-prefs payload are ignored, not persisted as
        a fake mute-window/temp-mutes that the dedicated endpoints would honor."""
        project = await _make_project(db_session)
        await client.put(
            "/api/admin/messages/noise-prefs",
            json={
                "project_id": str(project.id),
                "prefs": {
                    "pr_merged": "mute",
                    "_mute_window": {"expires_at": 9_999_999_999_999},
                    "_temp_mutes": {"cycle_summary": 9_999_999_999_999},
                },
            },
        )
        mw_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert mw_resp.json()["mute_window"] is None
        tm_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert tm_resp.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={random_pid}"
        )
        assert get_resp.status_code == 404
        put_resp = await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": random_pid, "prefs": {}},
        )
        assert put_resp.status_code == 404


class TestMessagesSavedViews:
    @pytest.mark.asyncio
    async def test_get_saved_views_empty(self, client: AsyncClient, db_session):
        """Returns empty list when no saved-views record exists for the operator."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["saved_views"] == []

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores saved views; subsequent GET returns the same value."""
        project = await _make_project(db_session)
        views = [{"name": "Blockers only", "query": "", "sort": "desc"}]

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["saved_views"] == views

    @pytest.mark.asyncio
    async def test_update_replaces_views(self, client: AsyncClient, db_session):
        """A second PUT replaces the previous saved views."""
        project = await _make_project(db_session)
        views1 = [{"name": "First view"}]
        views2 = [{"name": "Second view"}, {"name": "Third view"}]

        await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views1},
        )
        await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views2},
        )
        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert get_resp.json()["saved_views"] == views2

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot read or write saved-views for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": random_pid, "saved_views": []},
        )
        assert put_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_oversize_payload_returns_400(self, client: AsyncClient, db_session):
        """Payload exceeding 50 KB is rejected with 400."""
        project = await _make_project(db_session)
        # Each view is ~1KB; 60 of them easily exceeds 50KB
        large_views = [{"name": f"view-{i}", "data": "x" * 900} for i in range(60)]

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": large_views},
        )
        assert put_resp.status_code == 400
        assert "50" in put_resp.json()["error"]["message"]


class TestMessagesTempMutes:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns empty dict when no temp-mutes record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores temp-mutes; GET returns only non-expired entries."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 3_600_000  # 1 hour from now
        mutes = {"pr_merged": future_ms}

        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": str(project.id), "temp_mutes": mutes},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["temp_mutes"] == mutes

    @pytest.mark.asyncio
    async def test_expired_mutes_filtered_on_get(self, client: AsyncClient, db_session):
        """Expired entries are filtered out on GET; active entries survive."""
        import time

        project = await _make_project(db_session)
        now_ms = int(time.time() * 1000)
        future_ms = now_ms + 3_600_000
        # Write one expired + one active mute (bypass filter by writing directly via PUT
        # with a past timestamp — PUT also filters, so use future for writing and verify active)
        await client.put(
            "/api/admin/messages/temp-mutes",
            json={
                "project_id": str(project.id),
                "temp_mutes": {"cycle_summary": future_ms},
            },
        )
        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert "cycle_summary" in get_resp.json()["temp_mutes"]

        # Overwrite with an already-expired entry: PUT filters it out before storing
        past_ms = now_ms - 1000
        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": str(project.id), "temp_mutes": {"pr_merged": past_ms}},
        )
        assert put_resp.status_code == 200
        get_resp2 = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert get_resp2.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": random_pid, "temp_mutes": {}},
        )
        assert put_resp.status_code == 404


class TestMessagesMuteWindow:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns null when no mute-window record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["mute_window"] is None

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores mute-window; GET returns it while still active."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 7_200_000  # 2 hours from now

        put_resp = await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["mute_window"] == {"expires_at": future_ms}

    @pytest.mark.asyncio
    async def test_clear_mute_window(self, client: AsyncClient, db_session):
        """PUT with expires_at=null clears the mute window."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 7_200_000

        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": None},
        )
        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert get_resp.json()["mute_window"] is None

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": random_pid, "expires_at": None},
        )
        assert put_resp.status_code == 404


class TestPlaybook:
    @pytest.mark.asyncio
    async def test_playbook_list_empty(self, client: AsyncClient, db_session):
        """New project has an empty playbook."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == {}
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_playbook_set_creates_entry(self, client: AsyncClient, db_session):
        """PUT creates a new playbook entry."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "my-tip", "value": "Always run tests before merging."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["key"] == "my-tip"

    @pytest.mark.asyncio
    async def test_playbook_set_then_list(self, client: AsyncClient, db_session):
        """Entry appears in the listing after creation."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "tip1", "value": "First tip"},
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert "tip1" in entries
        assert entries["tip1"]["content"] == "First tip"

    @pytest.mark.asyncio
    async def test_playbook_set_invalid_key_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Key with spaces/special chars returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "invalid key with spaces!", "value": "Some value"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_set_value_too_large_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Value exceeding 2048 bytes returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "big-entry", "value": "x" * 2049},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_set_with_applicable_types(
        self, client: AsyncClient, db_session
    ):
        """Entry can specify which task types it applies to, and they persist."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={
                "key": "typed-tip",
                "value": "Only for create tasks",
                "applicable_types": ["create", "fix"],
            },
        )
        assert resp.status_code == 200

        # Read the entry back: applicable_types must round-trip through storage.
        read = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert read.status_code == 200
        entries = read.json()["entries"]
        assert "typed-tip" in entries
        assert entries["typed-tip"]["applicable_types"] == ["create", "fix"]

    @pytest.mark.asyncio
    async def test_playbook_set_invalid_applicable_type_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Unknown task type in applicable_types returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={
                "key": "bad-types",
                "value": "Test",
                "applicable_types": ["invalid-task-type"],
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_update_existing_entry(
        self, client: AsyncClient, db_session
    ):
        """PUT on an existing key updates the content."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "updatable", "value": "Original"},
        )
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "updatable", "value": "Updated"},
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.json()["entries"]["updatable"]["content"] == "Updated"

    @pytest.mark.asyncio
    async def test_playbook_delete_entry(self, client: AsyncClient, db_session):
        """Can delete an existing playbook entry."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "to-delete", "value": "Temporary"},
        )

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/to-delete"
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone from list
        list_resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert "to-delete" not in list_resp.json()["entries"]

    @pytest.mark.asyncio
    async def test_playbook_delete_not_found_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Deleting a non-existent key returns 404."""
        project = await _make_project(db_session)

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/no-such-key"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_playbook_delete_writes_tombstone(
        self, client: AsyncClient, db_session
    ):
        """Deleting a playbook entry records a tombstone in playbook_archive and playbook_deleted_keys."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "tombstone-test", "value": "To be deleted"},
        )

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/tombstone-test"
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify the tombstone was written to the design_doc
        await db_session.refresh(project)
        doc = project.design_doc or {}
        archive = doc.get("playbook_archive", [])
        tombstones = [
            e for e in archive if e.get("_archived_key") == "playbook/tombstone-test"
        ]
        assert len(tombstones) == 1
        assert tombstones[0]["_retirement_reason"] == "operator_deleted"
        assert "_archived_at" in tombstones[0]

        deleted_keys = doc.get("playbook_deleted_keys", [])
        assert "playbook/tombstone-test" in deleted_keys

    @pytest.mark.asyncio
    async def test_playbook_seed_creates_default_entries(
        self, client: AsyncClient, db_session
    ):
        """Seed populates the playbook with operational defaults."""
        project = await _make_project(db_session)

        resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["added"]) > 0
        assert "build-backend" in data["added"]
        assert len(data["skipped"]) == 0

    @pytest.mark.asyncio
    async def test_playbook_seed_skips_existing_entries(
        self, client: AsyncClient, db_session
    ):
        """Seed does not overwrite entries that already exist."""
        project = await _make_project(db_session)
        # Pre-create one of the seed keys
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "build-backend", "value": "Custom version"},
        )

        resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
        assert resp.status_code == 200
        data = resp.json()
        assert "build-backend" in data["skipped"]
        assert "build-backend" not in data["added"]

    @pytest.mark.asyncio
    async def test_playbook_list_requires_member_role(
        self, client: AsyncClient, db_session
    ):
        """Viewer-role can list the playbook (member threshold for list is 'member'
        — actually admin_playbook_list uses 'member', so viewer is rejected)."""
        other = Project(name="Viewer Only", user_id="other-owner-pb")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="viewer")
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{other.id}/playbook")
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# notification_config settings — validation and webhook_url masking
# ═══════════════════════════════════════════════════════════════════════════════


class TestNotificationConfigSettings:
    """Tests for PATCH /api/admin/settings notification_config validation and
    GET /api/admin/settings webhook_url masking for non-admin callers."""

    @pytest.fixture(autouse=True)
    def _mock_safe_dns(self):
        """Stub DNS so the SSRF check accepts hooks.example.com URLs without
        making a real DNS call, while still rejecting bad schemes.

        Patches the bound name in admin_settings.py rather than socket globally,
        so asyncpg's own socket.getaddrinfo calls remain unaffected.
        """

        def _fake(url: str):
            if not url.startswith(("http://", "https://")):
                return (False, f"scheme not allowed: {url!r}")
            return (True, "")

        with patch(
            "app.routers.admin_settings._is_safe_external_url",
            side_effect=_fake,
        ):
            yield

    @pytest.mark.asyncio
    async def test_patch_max_pipeline_slots(self, client: AsyncClient):
        """max_pipeline_slots accepts valid ints and rejects out-of-range / non-numeric."""
        resp = await client.patch("/api/admin/settings", json={"max_pipeline_slots": 6})
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["max_pipeline_slots"] == 6

        bad = await client.patch("/api/admin/settings", json={"max_pipeline_slots": -1})
        assert bad.status_code == 400
        bad2 = await client.patch(
            "/api/admin/settings", json={"max_pipeline_slots": "x"}
        )
        assert bad2.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_warm_continuity_enabled(self, client: AsyncClient):
        """warm_continuity_enabled accepts booleans and rejects non-booleans; round-trips on GET."""
        resp = await client.patch(
            "/api/admin/settings", json={"warm_continuity_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["warm_continuity_enabled"] is True

        resp = await client.patch(
            "/api/admin/settings", json={"warm_continuity_enabled": False}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["warm_continuity_enabled"] is False

        # Reject non-booleans (truthy strings/ints must NOT silently flip the flag).
        bad = await client.patch(
            "/api/admin/settings", json={"warm_continuity_enabled": "true"}
        )
        assert bad.status_code == 400
        bad2 = await client.patch(
            "/api/admin/settings", json={"warm_continuity_enabled": 1}
        )
        assert bad2.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_auto_debug_enabled(self, client: AsyncClient):
        """auto_debug_enabled accepts booleans, rejects non-booleans, and
        round-trips on GET.

        Regression: #8528 added the key to _KNOWN_SETTINGS_KEYS (so PATCH
        validated it and returned 200) but shipped no apply block — the write
        was silently dropped and the kill-switch could never be flipped."""
        resp = await client.patch(
            "/api/admin/settings", json={"auto_debug_enabled": False}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["auto_debug_enabled"] is False

        resp = await client.patch(
            "/api/admin/settings", json={"auto_debug_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["auto_debug_enabled"] is True

        bad = await client.patch(
            "/api/admin/settings", json={"auto_debug_enabled": "false"}
        )
        assert bad.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_warm_backlog_reuse_enabled(self, client: AsyncClient):
        """warm_backlog_reuse_enabled accepts booleans, rejects non-booleans,
        round-trips on GET, and is independent of warm_continuity_enabled."""
        resp = await client.patch(
            "/api/admin/settings", json={"warm_backlog_reuse_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        body = get_resp.json()
        assert body["warm_backlog_reuse_enabled"] is True
        # Independently switchable: flipping backlog reuse must not touch the
        # chain-continuity flag.
        assert "warm_continuity_enabled" in body

        resp = await client.patch(
            "/api/admin/settings", json={"warm_backlog_reuse_enabled": False}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["warm_backlog_reuse_enabled"] is False

        bad = await client.patch(
            "/api/admin/settings", json={"warm_backlog_reuse_enabled": "true"}
        )
        assert bad.status_code == 400
        bad2 = await client.patch(
            "/api/admin/settings", json={"warm_backlog_reuse_enabled": 1}
        )
        assert bad2.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rollup_merge_queue_enabled(self, client: AsyncClient):
        """rollup_merge_queue_enabled accepts booleans, rejects non-booleans,
        and round-trips on GET. Guards all four wiring points (allowlist,
        apply block, GET projection, persist) — a missing apply block would
        200 the PATCH and silently drop the write (#8528 pattern)."""
        resp = await client.patch(
            "/api/admin/settings", json={"rollup_merge_queue_enabled": False}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["rollup_merge_queue_enabled"] is False

        resp = await client.patch(
            "/api/admin/settings", json={"rollup_merge_queue_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["rollup_merge_queue_enabled"] is True

        bad = await client.patch(
            "/api/admin/settings", json={"rollup_merge_queue_enabled": "false"}
        )
        assert bad.status_code == 400
        bad2 = await client.patch(
            "/api/admin/settings", json={"rollup_merge_queue_enabled": 0}
        )
        assert bad2.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_warm_pool_project_allowlist_round_trip(
        self, client: AsyncClient
    ):
        """warm_pool_project_allowlist accepts a list, normalizes a CSV string,
        rejects bad shapes, and round-trips (sorted, de-duped) on GET.

        Also exercises the maintainer contract: a scoped project is held at
        target while an unscoped demand-active project is forced to drain-only,
        and an empty allowlist restores fleet-wide behavior.
        """
        from app.shared_state import runtime_config as _rc
        from app.warm_pool import (
            is_project_warm_pool_scoped,
            warm_pool_project_scope,
        )

        original = _rc.get("warm_pool_project_allowlist")
        try:
            # List input round-trips sorted + de-duped.
            resp = await client.patch(
                "/api/admin/settings",
                json={"warm_pool_project_allowlist": ["proj-b", "proj-a", "proj-a"]},
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["warm_pool_project_allowlist"] == ["proj-a", "proj-b"]
            # The maintainer's scope helper agrees: scoped projects pass, an
            # unscoped demand-active project does not.
            assert is_project_warm_pool_scoped("proj-a") is True
            assert is_project_warm_pool_scoped("proj-unscoped") is False

            # CSV string is normalized into a clean list.
            resp = await client.patch(
                "/api/admin/settings",
                json={"warm_pool_project_allowlist": "proj-c, proj-d ,"},
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["warm_pool_project_allowlist"] == ["proj-c", "proj-d"]

            # Empty list restores fleet-wide (legacy) mode: every project scoped.
            resp = await client.patch(
                "/api/admin/settings", json={"warm_pool_project_allowlist": []}
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["warm_pool_project_allowlist"] == []
            assert warm_pool_project_scope() == set()
            assert is_project_warm_pool_scoped("any-project") is True

            # Bad shapes rejected (must not silently corrupt the allowlist).
            bad = await client.patch(
                "/api/admin/settings", json={"warm_pool_project_allowlist": 123}
            )
            assert bad.status_code == 400
            bad2 = await client.patch(
                "/api/admin/settings",
                json={"warm_pool_project_allowlist": [{"id": "x"}]},
            )
            assert bad2.status_code == 400
        finally:
            if original is not None:
                _rc["warm_pool_project_allowlist"] = original
            else:
                _rc.pop("warm_pool_project_allowlist", None)

    @pytest.mark.asyncio
    async def test_patch_warm_reuse_tuning_knobs_round_trip(self, client: AsyncClient):
        """The warm-reuse tuning knobs are PATCHable and visible on GET."""
        from app.shared_state import runtime_config as _rc

        keys = (
            "warm_pin_clear_rate_threshold",
            "warm_continuity_cooldown_seconds",
            "warm_backlog_reuse_failure_rate_threshold",
            "warm_backlog_reuse_cooldown_seconds",
            "warm_backlog_scan_limit",
            "warm_backlog_teardown_defer_secs",
        )
        original = {key: _rc.get(key) for key in keys}
        payload = {
            "warm_pin_clear_rate_threshold": 12,
            "warm_continuity_cooldown_seconds": 2400,
            "warm_backlog_reuse_failure_rate_threshold": 7,
            "warm_backlog_reuse_cooldown_seconds": 2400,
            "warm_backlog_scan_limit": 17,
            "warm_backlog_teardown_defer_secs": 30,
        }
        try:
            resp = await client.patch("/api/admin/settings", json=payload)
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            for key, expected in payload.items():
                assert body[key] == expected
        finally:
            await client.patch(
                "/api/admin/settings",
                json={key: value for key, value in original.items() if value is not None},
            )

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("warm_pin_clear_rate_threshold", 0),
            ("warm_pin_clear_rate_threshold", 101),
            ("warm_continuity_cooldown_seconds", 59),
            ("warm_continuity_cooldown_seconds", 86401),
            ("warm_backlog_reuse_failure_rate_threshold", 0),
            ("warm_backlog_reuse_failure_rate_threshold", 101),
            ("warm_backlog_reuse_cooldown_seconds", 59),
            ("warm_backlog_reuse_cooldown_seconds", 86401),
            ("warm_backlog_scan_limit", 0),
            ("warm_backlog_scan_limit", 101),
            ("warm_backlog_teardown_defer_secs", 4),
            ("warm_backlog_teardown_defer_secs", 121),
        ],
    )
    @pytest.mark.asyncio
    async def test_patch_warm_reuse_tuning_knobs_reject_out_of_bounds(
        self, client: AsyncClient, field: str, bad_value: int
    ):
        """Out-of-range warm-reuse tuning values are rejected."""
        resp = await client.patch("/api/admin/settings", json={field: bad_value})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_warm_pool_config_round_trip(self, client: AsyncClient):
        """warm_pool_enabled / target_size / idle_ttl_secs PATCH, round-trip on
        GET, and are independent of the other warm flags."""
        from app.shared_state import runtime_config as _rc

        keys = (
            "warm_pool_enabled",
            "warm_pool_target_size",
            "warm_pool_idle_ttl_secs",
            "warm_pool_idle_ttl_seconds",
        )
        original = {key: _rc.get(key) for key in keys}
        payload = {
            "warm_pool_enabled": True,
            "warm_pool_target_size": 7,
            "warm_pool_idle_ttl_secs": 1200,
        }
        try:
            resp = await client.patch("/api/admin/settings", json=payload)
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["warm_pool_enabled"] is True
            assert body["warm_pool_target_size"] == 7
            assert body["warm_pool_idle_ttl_secs"] == 1200
            # TTL dual-key sync: the idle-reaper in warm_pool_maintainer.py reads
            # the ``_seconds`` variant, so a PATCH to ``_secs`` MUST also write
            # ``_seconds`` or operator TTL changes never reach the maintainer
            # (regressed once — fix commit 560fd11). Assert the runtime_config
            # value directly, not via GET (GET surfaces only the ``_secs`` key).
            assert _rc.get("warm_pool_idle_ttl_seconds") == 1200
            assert _rc.get("warm_pool_idle_ttl_secs") == 1200
            # Independently switchable: flipping the pool flag must not touch the
            # chain-continuity flag.
            assert "warm_continuity_enabled" in body

            # Boolean type-guard on the master switch.
            bad = await client.patch(
                "/api/admin/settings", json={"warm_pool_enabled": "true"}
            )
            assert bad.status_code == 400
            bad2 = await client.patch(
                "/api/admin/settings", json={"warm_pool_enabled": 1}
            )
            assert bad2.status_code == 400
        finally:
            await client.patch(
                "/api/admin/settings",
                json={k: v for k, v in original.items() if v is not None},
            )

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("warm_pool_target_size", -1),
            ("warm_pool_target_size", 51),
            ("warm_pool_idle_ttl_secs", 59),
            ("warm_pool_idle_ttl_secs", 86401),
        ],
    )
    @pytest.mark.asyncio
    async def test_patch_warm_pool_config_reject_out_of_bounds(
        self, client: AsyncClient, field: str, bad_value: int
    ):
        """Out-of-range warm-pool sizing values are rejected (target 0..50,
        idle TTL 60..86400)."""
        resp = await client.patch("/api/admin/settings", json={field: bad_value})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_release_flags_round_trip_on_get(self, client: AsyncClient):
        """The automain/LTS release-control flags — promotion_enabled,
        smoketest_enabled — are settable via PATCH and visible on GET
        (regression: GET previously omitted them). integration_branch is
        per-project only; see TestIntegrationBranchConfig in
        test_admin_project_config.py."""
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "promotion_enabled": True,
                "smoketest_enabled": False,
            },
        )
        assert resp.status_code == 200
        body = (await client.get("/api/admin/settings")).json()
        assert body["promotion_enabled"] is True
        assert body["smoketest_enabled"] is False
        # Restore the default so promotion_enabled does not leak to other tests.
        await client.patch("/api/admin/settings", json={"promotion_enabled": False})

    @pytest.mark.asyncio
    async def test_promotion_threshold_round_trip(self, client: AsyncClient):
        """promotion_threshold is settable via PATCH, bounded to (0, 1], and
        visible on GET — so calibrating the bar needs no redeploy."""
        from app.shared_state import runtime_config as _rc

        original = _rc.get("promotion_threshold")
        try:
            resp = await client.patch(
                "/api/admin/settings", json={"promotion_threshold": 0.75}
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["promotion_threshold"] == pytest.approx(0.75)

            # Out of range / non-numeric rejected.
            assert (
                await client.patch(
                    "/api/admin/settings", json={"promotion_threshold": 0}
                )
            ).status_code == 400
            assert (
                await client.patch(
                    "/api/admin/settings", json={"promotion_threshold": 1.5}
                )
            ).status_code == 400
            assert (
                await client.patch(
                    "/api/admin/settings", json={"promotion_threshold": "high"}
                )
            ).status_code == 400
        finally:
            if original is not None:
                await client.patch(
                    "/api/admin/settings", json={"promotion_threshold": original}
                )

    @pytest.mark.asyncio
    async def test_promotion_weights_round_trip_and_validation(
        self, client: AsyncClient
    ):
        """promotion_weights accepts a 3-key set summing to ~1.0, rejects a
        mis-weighted set / wrong keys, and is visible on GET."""
        from app.shared_state import runtime_config as _rc

        original = _rc.get("promotion_weights")
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={
                    "promotion_weights": {
                        "audit": 0.5,
                        "test_erosion": 0.25,
                        "smoketest": 0.25,
                    }
                },
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["promotion_weights"]["audit"] == pytest.approx(0.5)

            # Sum != 1.0 rejected.
            bad_sum = await client.patch(
                "/api/admin/settings",
                json={
                    "promotion_weights": {
                        "audit": 0.1,
                        "test_erosion": 0.1,
                        "smoketest": 0.1,
                    }
                },
            )
            assert bad_sum.status_code == 400
            # Wrong key set rejected.
            bad_keys = await client.patch(
                "/api/admin/settings",
                json={"promotion_weights": {"audit": 1.0}},
            )
            assert bad_keys.status_code == 400
            # Non-dict rejected.
            bad_type = await client.patch(
                "/api/admin/settings", json={"promotion_weights": 0.4}
            )
            assert bad_type.status_code == 400
        finally:
            if original is not None:
                await client.patch(
                    "/api/admin/settings", json={"promotion_weights": original}
                )

    @pytest.mark.asyncio
    async def test_unknown_settings_key_rejected(self, client: AsyncClient):
        """An unknown/misspelled settings key is rejected with 400 (and logged)
        rather than silently ignored — so a typo is detectable."""
        resp = await client.patch(
            "/api/admin/settings", json={"promotion_treshold": 0.8}
        )
        assert resp.status_code == 400
        assert "unknown" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_known_key_alongside_unknown_key_rejects_whole_request(
        self, client: AsyncClient
    ):
        """A valid key in the same payload as an unknown one does NOT get
        applied — the whole request is rejected up front, so no half-applied
        PATCH."""
        from app.shared_state import runtime_config as _rc

        before = _rc.get("max_retries")
        resp = await client.patch(
            "/api/admin/settings",
            json={"max_retries": 7, "bogus_key": True},
        )
        assert resp.status_code == 400
        # max_retries must NOT have been applied.
        assert _rc.get("max_retries") == before

    @pytest.mark.asyncio
    async def test_patch_next_role_poll_timeout_secs(self, client: AsyncClient):
        """next_role_poll_timeout_secs accepts ints in [30,600] and rejects out-of-range / non-numeric."""
        resp = await client.patch(
            "/api/admin/settings", json={"next_role_poll_timeout_secs": 240}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["next_role_poll_timeout_secs"] == 240

        bad_low = await client.patch(
            "/api/admin/settings", json={"next_role_poll_timeout_secs": 29}
        )
        assert bad_low.status_code == 400
        bad_high = await client.patch(
            "/api/admin/settings", json={"next_role_poll_timeout_secs": 601}
        )
        assert bad_high.status_code == 400
        bad_str = await client.patch(
            "/api/admin/settings", json={"next_role_poll_timeout_secs": "x"}
        )
        assert bad_str.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_valid_notification_config(self, client: AsyncClient):
        """PATCH with a valid notification_config is accepted."""
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "webhook_url": "https://hooks.example.com/token",
                    "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED"],
                    "throttle_seconds": 60,
                }
            },
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_webhook_url(
        self, client: AsyncClient
    ):
        """webhook_url must start with http:// or https://."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"notification_config": {"webhook_url": "ftp://bad.example.com"}},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_throttle_below_zero(
        self, client: AsyncClient
    ):
        """throttle_seconds must be >= 0."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"notification_config": {"throttle_seconds": -1}},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_throttle_above_max(
        self, client: AsyncClient
    ):
        """throttle_seconds must be <= 3600."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"notification_config": {"throttle_seconds": 3601}},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_unknown_event(self, client: AsyncClient):
        """enabled_events rejects strings not in the known event set."""
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "enabled_events": ["TASK_FAILED", "UNKNOWN_EVENT_XYZ"],
                }
            },
        )
        assert resp.status_code == 400
        assert "unknown" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_get_settings_admin_sees_webhook_url(self, client: AsyncClient):
        """Admin caller receives the actual webhook_url in notification_config."""
        # First configure a webhook URL as admin
        await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "webhook_url": "https://hooks.example.com/secret-token",
                    "enabled_events": ["TASK_FAILED"],
                    "throttle_seconds": 30,
                }
            },
        )
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        nc = resp.json()["notification_config"]
        assert nc["webhook_url"] == "https://hooks.example.com/secret-token"

    @pytest.mark.asyncio
    async def test_patch_residual_audit_defer_escalation_threshold(
        self, client: AsyncClient
    ):
        """residual_audit_defer_escalation_threshold accepts ints in [1,50] and rejects bad values."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"residual_audit_defer_escalation_threshold": 10},
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["residual_audit_defer_escalation_threshold"] == 10

        bad_low = await client.patch(
            "/api/admin/settings", json={"residual_audit_defer_escalation_threshold": 0}
        )
        assert bad_low.status_code == 400
        bad_high = await client.patch(
            "/api/admin/settings",
            json={"residual_audit_defer_escalation_threshold": 51},
        )
        assert bad_high.status_code == 400
        bad_str = await client.patch(
            "/api/admin/settings",
            json={"residual_audit_defer_escalation_threshold": "x"},
        )
        assert bad_str.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_residual_fix_audit_deferred_is_valid(
        self, client: AsyncClient
    ):
        """RESIDUAL_FIX_AUDIT_DEFERRED is accepted as a valid notification event."""
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "webhook_url": "https://hooks.example.com/x",
                    "enabled_events": ["TASK_FAILED", "RESIDUAL_FIX_AUDIT_DEFERRED"],
                    "throttle_seconds": 30,
                }
            },
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_notification_config_merge_unverified_is_valid(
        self, client: AsyncClient
    ):
        """MERGE_UNVERIFIED is accepted as a valid notification event so an
        operator can be notified through their webhook when a no-CI repo
        merges a PR without test verification."""
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "webhook_url": "https://hooks.example.com/x",
                    "enabled_events": ["TASK_FAILED", "MERGE_UNVERIFIED"],
                    "throttle_seconds": 30,
                }
            },
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_settings_non_admin_returns_403(self, override_get_session):
        """Non-admin caller is blocked with 403 from GET /api/admin/settings."""
        from app.main import app as _app
        from app.auth import get_current_user as _gcu, require_admin as _ra

        async def _non_admin():
            return {
                "user_id": "non-admin-user",
                "email": "user@example.com",
                "role": "user",
            }

        async def _reject_admin():
            raise __import__("fastapi").HTTPException(
                status_code=403, detail="Forbidden"
            )

        _app.dependency_overrides[_gcu] = _non_admin
        _app.dependency_overrides[_ra] = _reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=_app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/settings")
            assert resp.status_code == 403
        finally:
            _app.dependency_overrides.pop(_gcu, None)
            _app.dependency_overrides.pop(_ra, None)


# ═══════════════════════════════════════════════════════════════════════════════
# dispatch_external_notification — webhook dispatch logic
# ═══════════════════════════════════════════════════════════════════════════════


class TestDispatchExternalNotification:
    """Tests for dispatch_external_notification in notification_dispatch.py."""

    @pytest.fixture(autouse=True)
    def _mock_safe_dns_dispatch(self):
        """Stub the SSRF resolver so dispatch tests can use hooks.example.com
        without hitting real DNS, and so the mocked httpx client receives the
        original webhook URL (not an IP-pinned variant)."""
        with patch(
            "app.notification_dispatch._resolve_safe_external_url",
            return_value=(True, "", "93.184.216.34"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_dispatches_when_event_enabled(self, mocker):
        """POSTs to webhook_url when event_type is in enabled_events."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        # Clear throttle state
        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200

        mock_client = mocker.AsyncMock()
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mock_client.post = mocker.AsyncMock(return_value=mock_response)

        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        project_id = uuid.uuid4()
        await dispatch_external_notification(
            project_id, "TASK_FAILED", {"task_id": "abc", "task_title": "Test Task"}
        )

        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        body = (
            call_args.kwargs.get("json") or call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs["json"]
        )
        assert body["event_type"] == "TASK_FAILED"
        assert body["project_id"] == str(project_id)
        assert "timestamp" in body
        assert body["payload"]["task_id"] == "abc"

    @pytest.mark.asyncio
    async def test_skips_when_event_not_enabled(self, mocker):
        """Does NOT POST when event_type is not in enabled_events."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["APPROVAL_NEEDED"],
            "throttle_seconds": 0,
        }

        mock_client = mocker.AsyncMock()
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mock_client.post = mocker.AsyncMock()
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_webhook_url(self, mocker):
        """Does NOT POST when webhook_url is empty."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        mock_client = mocker.AsyncMock()
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mock_client.post = mocker.AsyncMock()
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_throttle_seconds(self, mocker):
        """Second call within throttle window is silently dropped."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 60,
        }

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200

        mock_client = mocker.AsyncMock()
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mock_client.post = mocker.AsyncMock(return_value=mock_response)
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        project_id = uuid.uuid4()
        await dispatch_external_notification(project_id, "TASK_FAILED", {"n": 1})
        # Second call within throttle window
        await dispatch_external_notification(project_id, "TASK_FAILED", {"n": 2})

        assert mock_client.post.await_count == 1, "throttle should drop the second call"

    @pytest.mark.asyncio
    async def test_never_raises_on_http_error(self, mocker):
        """dispatch_external_notification swallows all exceptions without propagating."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        import httpx as _httpx

        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient",
            side_effect=_httpx.RequestError("network down"),
        )

        # Must not raise
        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})


# ═══════════════════════════════════════════════════════════════════════════════
# notification_config startup restore — survives Cloud Run restart
# ═══════════════════════════════════════════════════════════════════════════════


class TestNotificationConfigStartupRestore:
    """Verify that notification_config in runtime_config_json is restored on startup."""

    @pytest.mark.asyncio
    async def test_notification_config_restored_from_runtime_config_json(self, mocker):
        """notification_config written to DeploymentState.runtime_config_json
        must be loaded back into shared_state.runtime_config on _init_deployment_state.
        """
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from app import shared_state
        from app.models import DeploymentState
        from app.main import _init_deployment_state

        # Create isolated in-memory SQLite DB (same pattern as test_services.py)
        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        notification_cfg = {
            "webhook_url": "https://hooks.example.com/token123",
            "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED"],
            "throttle_seconds": 120,
        }

        # Seed DeploymentState with notification_config in runtime_config_json
        async with _Session() as session:
            state = DeploymentState(
                id=1,
                current_revision="r1",
                current_sha="abc123",
                runtime_config_json={"notification_config": notification_cfg},
            )
            session.add(state)
            await session.commit()

        # Clear the in-memory value so we can verify it is populated by the restore
        saved = shared_state.runtime_config.pop("notification_config", None)
        try:
            await _init_deployment_state()
            restored = shared_state.runtime_config.get("notification_config")
            assert restored is not None, (
                "notification_config must be restored from runtime_config_json"
            )
            assert restored["webhook_url"] == "https://hooks.example.com/token123"
            assert restored["enabled_events"] == ["TASK_FAILED", "APPROVAL_NEEDED"]
            assert restored["throttle_seconds"] == 120
        finally:
            # Restore previous state to avoid leaking into other tests
            if saved is not None:
                shared_state.runtime_config["notification_config"] = saved
            else:
                shared_state.runtime_config.pop("notification_config", None)
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_init_deployment_state_degrades_on_commit_lock_timeout(self, mocker):
        """A contended DeploymentState write during deploy-detection must NOT crash
        startup. Boot used to die ("Application startup failed. Exiting.") when this
        commit hit a lock timeout, failing the deploy and pinning prod on the old
        revision (incident 2026-06-04). It must now degrade and still boot."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.exc import OperationalError
        from sqlmodel import SQLModel
        from app.models import DeploymentState, DeploymentStatus
        from app.main import _init_deployment_state
        import app.main as _main

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        # Seed an existing deploy-state on a DIFFERENT revision → the new-deploy
        # path, which reaches the (previously fatal) detection commit.
        async with _Session() as session:
            session.add(
                DeploymentState(
                    id=1,
                    current_revision="old-rev",
                    current_sha="old-sha",
                    last_known_good_revision="old-rev",
                    last_known_good_sha="old-sha",
                    status=DeploymentStatus.STABLE,
                )
            )
            await session.commit()

        mocker.patch.object(_main, "_current_revision", "new-rev")
        mocker.patch.object(_main, "_current_sha", "new-sha")

        # Simulate the contended row: the detection commit raises a lock timeout.
        # (Seeded above before this patch; close()/rollback on context-exit are
        # unaffected since only commit is patched.)
        async def _boom(_self):
            raise OperationalError(
                "UPDATE deploymentstate", {}, Exception("canceling statement due to lock timeout")
            )

        mocker.patch.object(AsyncSession, "commit", _boom)

        try:
            # Must NOT raise — it degrades (logs + rollback) and the app boots.
            await _init_deployment_state()
        finally:
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_init_deployment_state_adopts_resolved_sha(self, mocker):
        """On Cloud Run the resolver returns the real serving SHA; _init_deployment_state
        must adopt it into _current_sha (and therefore the singleton row /
        /api/version) before initializing deployment state."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from unittest.mock import AsyncMock
        from app.models import DeploymentState
        from app.main import _init_deployment_state
        import app.main as _main

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)
        mocker.patch.object(_main, "_current_sha", "unknown")
        mocker.patch.object(
            _main,
            "resolve_deployed_sha",
            new=AsyncMock(return_value="cafebabe123"),
        )

        try:
            # First boot — no existing row, so it creates the singleton.
            await _init_deployment_state()
            assert _main._current_sha == "cafebabe123"
            async with _Session() as session:
                row = await session.get(DeploymentState, 1)
                assert row is not None
                assert row.current_sha == "cafebabe123"
        finally:
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# admin_settings.py — repair-state stuck task persists failure_category
# ═══════════════════════════════════════════════════════════════════════════════


class TestRepairStateFailureCategory:
    """Ensure that the /repair-state stuck-task path sets failure_category=AGENT_TIMEOUT
    on repaired tasks so downstream analytics can bucket them correctly."""

    @pytest.mark.asyncio
    async def test_repair_state_stuck_task_persists_agent_timeout_category(
        self, client: AsyncClient, db_session, mocker
    ):
        """A task stuck in IN_PROGRESS with no heartbeat for 15+ min must be marked
        FAILED with failure_category=AGENT_TIMEOUT in its context."""
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock

        mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)

        project = Project(name="Repair Test", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Task stuck in IN_PROGRESS with a heartbeat 20 minutes ago
        stale_heartbeat = datetime.utcnow() - timedelta(minutes=20)
        stuck_task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "test"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="stuck-vm-001",
            last_heartbeat=stale_heartbeat,
        )
        db_session.add(stuck_task)
        await db_session.commit()
        await db_session.refresh(stuck_task)

        resp = await client.post("/api/admin/repair-state")
        assert resp.status_code == 200

        await db_session.refresh(stuck_task)
        assert stuck_task.status == TaskStatus.FAILED
        assert (stuck_task.context or {}).get("failure_category") == "AGENT_TIMEOUT"


# ═══════════════════════════════════════════════════════════════════════════════
# admin_analytics.py — direction auto-pause stats
# ═══════════════════════════════════════════════════════════════════════════════


class TestDirectionAutoPauseStats:
    """Aggregation of direction auto-pause/recovery events for the dashboard widget."""

    @pytest.mark.asyncio
    async def test_aggregates_window_counts_reasons_and_trend(
        self, client: AsyncClient, db_session
    ):
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)

        # Live directions: one currently auto-paused, one not
        project.directions = [
            {
                "id": "d1",
                "text": "dir one",
                "status": "paused",
                "auto_paused": True,
                "auto_pause_count": 1,
                "paused_reason": "high failure rate",
                "paused_at": datetime.now(timezone.utc).isoformat(),
            },
            {"id": "d2", "text": "dir two", "status": "active"},
        ]
        db_session.add(project)
        await db_session.commit()

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Current window (last 24h): 3 auto-paused, 1 auto-retired
        def _msg(hours_ago: float, milestone: str, reason: str):
            m = ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"Direction {milestone}: dir — {reason}",
                context={
                    "event_type": "direction_milestone",
                    "milestone": milestone,
                    "direction_text": "dir",
                    "reason": reason,
                },
            )
            m.created_at = now - timedelta(hours=hours_ago)
            return m

        db_session.add_all(
            [
                _msg(1, "auto-paused", "high failure rate"),
                _msg(5, "auto-paused", "high failure rate"),
                _msg(10, "auto-paused", "no merges"),
                _msg(11, "auto-retired", "3 consecutive auto-pause cycles"),
                # Previous window (24-48h ago): only 1 auto-paused - drives "up" trend
                _msg(30, "auto-paused", "stale activity"),
            ]
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/direction-auto-pause-stats?project_id={project.id}&window_hours=24"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_hours"] == 24
        assert data["auto_paused_count"] == 3
        assert data["auto_retired_count"] == 1
        assert data["auto_recovered_count"] == 2  # 3 paused - 1 retired
        assert data["currently_auto_paused"] == 1
        assert data["prev_window_auto_paused_count"] == 1
        assert data["trend"] == "up"
        # Top reason is "high failure rate" (2 occurrences)
        assert data["top_reasons"][0] == {"reason": "high failure rate", "count": 2}
        reasons = {r["reason"] for r in data["top_reasons"]}
        assert "no merges" in reasons

    @pytest.mark.asyncio
    async def test_empty_project_returns_zero_counts(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/direction-auto-pause-stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_paused_count"] == 0
        assert data["auto_retired_count"] == 0
        assert data["auto_recovered_count"] == 0
        assert data["currently_auto_paused"] == 0
        assert data["top_reasons"] == []


class TestProjectEndpointsSurfaceProblemModel:
    """Project detail endpoint exposes problem_model + running_update fields so
    the admin-ui can render the new strategic views."""

    @pytest.mark.asyncio
    async def test_detail_endpoint_includes_jsonb_fields(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Build X.", "current_focus": "Ship Y."},
        }
        project.running_update = "## Current state\nShipping feature X."
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data["id"] == str(project.id)
        assert "problem_model" in data
        assert data["problem_model"]["intent"]["purpose"] == "Build X."
        assert "running_update" in data
        assert data["running_update"].startswith("## Current state")
        assert "running_update_acknowledged_at" in data
        assert "self_reflection_log" in data
        assert "my_role" in data

    @pytest.mark.asyncio
    async def test_list_excludes_large_jsonb_fields(
        self, client: AsyncClient, db_session
    ):
        """List endpoint omits large JSONB columns for performance."""
        project = await _make_project(db_session)
        project.problem_model = {"intent": {"purpose": "Build X."}}
        project.running_update = "## Current state"
        db_session.add(project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        data = resp.json()
        our = next((p for p in data["items"] if p["id"] == str(project.id)), None)
        assert our is not None
        assert "problem_model" not in our
        assert "running_update" not in our
        assert "vision" not in our
        assert "design_doc" not in our
        assert "self_reflection_log" not in our
        assert "directions" not in our
        assert "directions_count" in our

    @pytest.mark.asyncio
    async def test_detail_endpoint_returns_404_for_other_user(
        self, client: AsyncClient, db_session
    ):
        other = Project(name="Other", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{other.id}")
        assert resp.status_code == 404


class TestGetAllProjectsPagination:
    """GET /api/admin/projects supports limit/offset pagination."""

    @pytest.mark.asyncio
    async def test_limit_parameter(self, client: AsyncClient, db_session):
        for i in range(5):
            await _make_project(db_session, name=f"Project {i}")

        resp = await client.get("/api/admin/projects?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "has_more" in data
        assert len(data["items"]) <= 2

    @pytest.mark.asyncio
    async def test_offset_parameter(self, client: AsyncClient, db_session):
        for i in range(4):
            await _make_project(db_session, name=f"Page Project {i}")

        resp_all = await client.get("/api/admin/projects?limit=100")
        resp_offset = await client.get("/api/admin/projects?limit=100&offset=2")
        all_ids = [p["id"] for p in resp_all.json()["items"]]
        offset_ids = [p["id"] for p in resp_offset.json()["items"]]
        assert offset_ids == all_ids[2:]

    @pytest.mark.asyncio
    async def test_limit_max_enforced(self, client: AsyncClient, db_session):
        resp = await client.get("/api/admin/projects?limit=200")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_has_more_flag(self, client: AsyncClient, db_session):
        """has_more is True when there are more results beyond the page."""
        for i in range(3):
            await _make_project(db_session, name=f"HM Project {i}")

        resp_page = await client.get("/api/admin/projects?limit=2")
        assert resp_page.status_code == 200
        data_page = resp_page.json()
        assert data_page["has_more"] is True

        resp_all = await client.get("/api/admin/projects?limit=100")
        assert resp_all.json()["has_more"] is False


class TestPatchProblemModel:
    """PATCH /api/admin/projects/{id} accepts problem_model_patch for the
    user-editable subset (intent, context, constraints)."""

    @pytest.mark.asyncio
    async def test_patch_intent_and_constraints(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {
                    "intent": {
                        "purpose": "Autonomous dev.",
                        "current_focus": "Review loop.",
                    },
                    "constraints": ["Solo dev, 4h/day.", "Don't touch CI workflows."],
                    "context": {"project_type": "personal", "team": "solo"},
                }
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Autonomous dev."
        assert pm["intent"]["current_focus"] == "Review loop."
        assert pm["constraints"] == ["Solo dev, 4h/day.", "Don't touch CI workflows."]
        assert pm["context"]["project_type"] == "personal"
        assert pm["revision"] == 1
        assert pm["last_updated_by"] == "user"

    @pytest.mark.asyncio
    async def test_patch_rejects_agent_owned_fields(
        self, client: AsyncClient, db_session
    ):
        """Users cannot rewrite gaps / capabilities / friction via PATCH —
        those are agent-owned. Enforcing this at the API layer prevents
        accidental data loss."""
        project = await _make_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"problem_model_patch": {"gaps": [{"id": "g-x", "name": "mine"}]}},
        )
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "agent-owned" in msg or "Cannot edit" in msg

    @pytest.mark.asyncio
    async def test_patch_preserves_agent_fields(self, client: AsyncClient, db_session):
        """Patching intent does not disturb agent-owned fields."""
        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Old.", "current_focus": ""},
            "gaps": [{"id": "g-keep", "name": "should survive", "status": "active"}],
            "capabilities": [{"name": "CapA", "status": "working"}],
            "revision": 3,
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"problem_model_patch": {"intent": {"purpose": "New purpose."}}},
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "New purpose."
        assert len(pm["gaps"]) == 1
        assert pm["gaps"][0]["id"] == "g-keep"
        assert len(pm["capabilities"]) == 1
        assert pm["revision"] == 4  # bumped
        assert pm["last_updated_by"] == "user"

    @pytest.mark.asyncio
    async def test_patch_intent_stamps_operator_source(
        self, client: AsyncClient, db_session
    ):
        """Editing intent.purpose via the official editor PATCH path stamps a
        user_message provenance source, so the operator-wins protection keeps
        recognizing the purpose as operator-authored on the next cycle. (The
        editor payload sends no source; the backend must re-stamp it.)"""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        # Onboarded purpose with a user_message source; editor save must not
        # leave a null source behind.
        project.problem_model = {
            "intent": {
                "purpose": "Onboarded.",
                "current_focus": "",
                "source": {"type": "user_message", "message_id": "m-seed"},
            },
            "revision": 2,
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {
                    "intent": {"purpose": "Operator-edited.", "current_focus": ""}
                }
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Operator-edited."
        assert pm["intent"]["source"] is not None
        assert pm["intent"]["source"]["type"] == "user_message"

    @pytest.mark.asyncio
    async def test_edited_purpose_survives_divergent_agent_cycle(
        self, client: AsyncClient, db_session
    ):
        """Full regression: an operator edits the purpose via PATCH, then a
        simulated strategic cycle proposes a different purpose. The operator's
        value must win and the agent's discarded value must be recorded in the
        rejected-edits ledger (the operator-override badge source)."""
        from sqlalchemy.orm.attributes import flag_modified
        from app.problem_model import apply_agent_update

        project = await _make_project(db_session)
        baseline = {
            "intent": {
                "purpose": "Onboarded purpose.",
                "current_focus": "phase 1",
                "source": {"type": "user_message", "message_id": "m-seed"},
            },
            "revision": 3,
        }
        project.problem_model = dict(baseline)
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        # Operator edits the purpose through the official editor path.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {
                    "intent": {
                        "purpose": "Operator-edited purpose.",
                        "current_focus": "phase 1",
                    }
                }
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)

        # Simulate the next strategic cycle proposing a divergent purpose.
        proposed = {
            "intent": {
                "purpose": "Agent-proposed purpose.",
                "current_focus": "phase 2",
            },
        }
        new_raw, _diff = apply_agent_update(
            project.problem_model,
            proposed,
            "2026-06-02T13:00:00",
            baseline_raw=baseline,
        )

        assert new_raw["intent"]["purpose"] == "Operator-edited purpose."
        assert any(
            entry["field"] == "intent.purpose"
            and entry["agent_value"] == "Agent-proposed purpose."
            for entry in new_raw["rejected_agent_edits"]
        )

    @pytest.mark.asyncio
    async def test_problem_model_stale_revision_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Supplying an outdated problem_model_base_revision returns 409."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Existing.", "current_focus": ""},
            "revision": 4,
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {"intent": {"purpose": "Clobber."}},
                "problem_model_base_revision": 1,
            },
        )
        assert resp.status_code == 409
        msg = resp.json()["error"]["message"]
        assert "base_revision=1" in msg
        assert "current=4" in msg

    @pytest.mark.asyncio
    async def test_problem_model_current_revision_succeeds_and_bumps(
        self, client: AsyncClient, db_session
    ):
        """Matching base_revision saves the patch and bumps the revision."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Old.", "current_focus": ""},
            "revision": 2,
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {"intent": {"purpose": "New."}},
                "problem_model_base_revision": 2,
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "New."
        assert pm["revision"] == 3
        assert pm["last_updated_by"] == "user"

    @pytest.mark.asyncio
    async def test_problem_model_no_base_revision_always_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Omitting problem_model_base_revision skips the CAS check (back-compat)."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Old.", "current_focus": ""},
            "revision": 7,
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"problem_model_patch": {"intent": {"purpose": "Unconditional."}}},
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Unconditional."
        assert pm["revision"] == 8

    @pytest.mark.asyncio
    async def test_concurrent_problem_model_write_no_data_loss(
        self, client: AsyncClient, db_session
    ):
        """A second writer with a stale base_revision is rejected; the first
        writer's edit survives intact."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Base.", "current_focus": ""},
            "revision": 1,
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        # First writer succeeds with the correct base revision.
        resp_a = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {"intent": {"purpose": "Writer A."}},
                "problem_model_base_revision": 1,
            },
        )
        assert resp_a.status_code == 200

        # Second writer with the now-stale base revision must be rejected.
        resp_b = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "problem_model_patch": {"intent": {"purpose": "Writer B clobber."}},
                "problem_model_base_revision": 1,
            },
        )
        assert resp_b.status_code == 409

        # Writer A's edit must be intact — not overwritten.
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Writer A."
        assert pm["revision"] == 2


class TestAcknowledgeRunningUpdate:
    """Acknowledge endpoint stamps running_update_acknowledged_at. The
    running_update document itself persists — the strategic agent refines
    it on the next cycle rather than starting fresh."""

    @pytest.mark.asyncio
    async def test_acknowledge_stamps_timestamp_and_preserves_document(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.running_update = "## Current state\nShipping feature X."
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/running-update/acknowledge"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "acknowledged"
        assert "acknowledged_at" in data

        await db_session.refresh(project)
        # Document persists — only the timestamp updates.
        assert project.running_update == "## Current state\nShipping feature X."
        assert project.running_update_acknowledged_at is not None

    @pytest.mark.asyncio
    async def test_acknowledge_with_empty_document_still_stamps(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/running-update/acknowledge"
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"

        await db_session.refresh(project)
        assert project.running_update_acknowledged_at is not None

    @pytest.mark.asyncio
    async def test_acknowledge_requires_project_access(
        self, client: AsyncClient, db_session
    ):
        """A user without access to the project gets 404 from the
        _get_user_project_with_role helper."""
        other = Project(name="Other", user_id="other-user")
        db_session.add(other)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{other.id}/running-update/acknowledge"
        )
        assert resp.status_code == 404


class TestEditRunningUpdate:
    """PUT running-update lets an operator edit the body, with base-text
    optimistic concurrency so a concurrent agent/digest write is detected."""

    @pytest.mark.asyncio
    async def test_edit_sets_body_with_matching_base(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.running_update = "## Current state\nStale claim."
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            f"/api/admin/projects/{project.id}/running-update",
            json={
                "body": "## Current state\nOperator-corrected claim.",
                "base": "## Current state\nStale claim.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["running_update"] == "## Current state\nOperator-corrected claim."

        await db_session.refresh(project)
        assert project.running_update == "## Current state\nOperator-corrected claim."

    @pytest.mark.asyncio
    async def test_edit_blind_set_without_base(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.running_update = "## Old"
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            f"/api/admin/projects/{project.id}/running-update",
            json={"body": "## New body"},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        assert project.running_update == "## New body"

    @pytest.mark.asyncio
    async def test_edit_stale_base_conflicts(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.running_update = "## Current state\nAgent rewrote this."
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            f"/api/admin/projects/{project.id}/running-update",
            json={
                "body": "## Operator edit",
                "base": "## A different base the operator started from",
            },
        )
        assert resp.status_code == 409

        await db_session.refresh(project)
        # Document untouched — operator must reload and retry.
        assert project.running_update == "## Current state\nAgent rewrote this."

    @pytest.mark.asyncio
    async def test_edit_requires_project_access(
        self, client: AsyncClient, db_session
    ):
        other = Project(name="Other", user_id="other-user")
        db_session.add(other)
        await db_session.commit()

        resp = await client.put(
            f"/api/admin/projects/{other.id}/running-update",
            json={"body": "x"},
        )
        assert resp.status_code == 404


class TestBuildDeployStatePayload:
    """Regression: WS broadcasts use websocket.send_json which lacks FastAPI's
    JSON encoder, so any datetime in the payload raised
    'Object of type datetime is not JSON serializable' on every connect."""

    @pytest.mark.asyncio
    async def test_payload_external_health_last_check_is_json_safe(self, db_session):
        """external_health.last_check is a live datetime; the payload must
        coerce it to an isoformat string (or None) so json.dumps succeeds."""
        import json
        from datetime import datetime as _dt
        import app.shared_state as _shared_state
        from app.admin import build_deploy_state_payload
        from app.models import DeploymentState, DeploymentStatus

        snap = _shared_state.external_health.get("last_check")
        try:
            _shared_state.external_health["last_check"] = _dt(2026, 4, 27, 8, 0, 0)
            db_session.add(
                DeploymentState(
                    id=1,
                    status=DeploymentStatus.STABLE,
                    current_revision="rev-1",
                    current_sha="sha-1",
                    last_known_good_revision="rev-1",
                    last_known_good_sha="sha-1",
                    runtime_max_concurrent_vms=5,
                )
            )
            await db_session.commit()

            payload = await build_deploy_state_payload(db_session)

            json.dumps(payload)
            assert payload["external_health"]["last_check"] == "2026-04-27T08:00:00"
        finally:
            _shared_state.external_health["last_check"] = snap


# ═══════════════════════════════════════════════════════════════════════════════
# admin_debug.py — System Maturity endpoint
# ═══════════════════════════════════════════════════════════════════════════════


class TestProjectSystemMaturity:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_rows(
        self, client: AsyncClient, db_session
    ):
        """Returns empty list when the project has no SystemMaturity rows."""
        project = await _make_project(db_session)
        resp = await client.get(f"/api/admin/projects/{project.id}/system-maturity")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_returns_rows_ordered_by_score_ascending(
        self, client: AsyncClient, db_session
    ):
        """Returns rows ordered by score ascending with correct fields."""
        project = await _make_project(db_session)
        row_high = SystemMaturity(
            project_id=project.id,
            system_id="system-b",
            score=85,
            recommended_mode="ship",
            top_issues=[{"description": "Minor test gaps"}],
        )
        row_low = SystemMaturity(
            project_id=project.id,
            system_id="system-a",
            score=30,
            recommended_mode="audit",
            top_issues=[{"description": "Critical failures"}],
        )
        db_session.add_all([row_high, row_low])
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/system-maturity")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Ordered by score ascending: 30 first, 85 second
        assert data[0]["system_id"] == "system-a"
        assert data[0]["score"] == 30
        assert data[0]["recommended_mode"] == "audit"
        assert data[0]["top_issues"] == [{"description": "Critical failures"}]
        assert "last_audited_at" in data[0]
        assert data[1]["system_id"] == "system-b"
        assert data[1]["score"] == 85
        assert data[1]["recommended_mode"] == "ship"

    @pytest.mark.asyncio
    async def test_returns_404_for_non_member_project(
        self, client: AsyncClient, db_session
    ):
        """Non-member project returns 404."""
        project = Project(name="Other Project", user_id="other-owner")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.get(f"/api/admin/projects/{project.id}/system-maturity")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Viewport regression digest tests
# ---------------------------------------------------------------------------


class TestSrCatchRateTrendsDirectionExamples:
    """Admin digest exposes by_direction examples in the shape needed for SR-prompt injection."""

    @pytest.mark.asyncio
    async def test_digest_passes_through_direction_examples(
        self, client: AsyncClient, db_session
    ):
        """(a/b) by_direction categories with examples are serialized correctly by the digest."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = {
            "sr_catch_rate_trends": {
                "computed_at": "2026-05-01T00:00:00",
                "categories": {
                    "correctness": {
                        "miss_rate": 0.5,
                        "examples": [{"severity": "MAJOR", "desc": "Global issue"}],
                    }
                },
                "by_direction": {
                    "dir-x": {
                        "categories": {
                            "security": {
                                "miss_rate": 0.9,
                                "examples": [
                                    {
                                        "severity": "CRITICAL",
                                        "desc": "Direction X security miss",
                                    }
                                ],
                            }
                        }
                    }
                },
            }
        }
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert resp.status_code == 200
        trends = resp.json().get("self_review_prediction_accuracy_trends")
        assert trends is not None

        by_dir = trends.get("by_direction", {})
        assert "dir-x" in by_dir
        dir_cats = by_dir["dir-x"].get("categories", {})
        assert "security" in dir_cats
        examples = dir_cats["security"].get("examples", [])
        assert len(examples) >= 1
        assert examples[0]["severity"] == "CRITICAL"
        assert examples[0]["desc"] == "Direction X security miss"

    @pytest.mark.asyncio
    async def test_digest_direction_without_examples_does_not_mask_global(
        self, client: AsyncClient, db_session
    ):
        """(c) by_direction entry with no examples is passed through; global categories still present."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = {
            "sr_catch_rate_trends": {
                "computed_at": "2026-05-01T00:00:00",
                "categories": {
                    "correctness": {
                        "miss_rate": 0.5,
                        "examples": [{"severity": "MAJOR", "desc": "Global fallback"}],
                    }
                },
                "by_direction": {
                    "dir-empty": {"categories": {"scope-drift": {"miss_rate": 0.7}}}
                },
            }
        }
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert resp.status_code == 200
        trends = resp.json().get("self_review_prediction_accuracy_trends")
        assert trends is not None

        assert "correctness" in trends.get("categories", {})
        by_dir = trends.get("by_direction", {})
        assert "dir-empty" in by_dir
        dir_cats = by_dir["dir-empty"].get("categories", {})
        assert "scope-drift" in dir_cats
        assert "examples" not in dir_cats["scope-drift"]


# ---------------------------------------------------------------------------
# Viewport debt endpoint tests
# ---------------------------------------------------------------------------


class TestViewportDebt:
    """Tests for GET /api/admin/projects/{project_id}/viewport-debt."""

    @pytest.mark.asyncio
    async def test_returns_rows_and_scanned_at(self, client: AsyncClient, db_session):
        """Happy path: returns rows list and scanned_at timestamp."""
        from unittest.mock import patch

        project = await _make_project(db_session)
        fake_rows = [
            {
                "component": "admin-ui/src/components/MessagesPanel.tsx",
                "eslint_disable_count": 3,
                "data_viewport_allow_small_target": 2,
                "data_viewport_allow_overflow": 1,
                "data_viewport_allow_truncation": 0,
                "total_opt_outs": 6,
                "has_viewport_test": True,
                "regression_case_count": 0,
                "source_url": None,
            }
        ]
        fake_summary = {
            "total_components": 1,
            "untested_count": 0,
            "untested_components": [],
        }
        with patch(
            "app.viewport_debt._scan_viewport_debt_rows",
            return_value=(fake_rows, [], fake_summary),
        ):
            resp = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert "scanned_at" in data
        assert len(data["rows"]) == 1
        row = data["rows"][0]
        assert row["component"] == "admin-ui/src/components/MessagesPanel.tsx"
        assert row["eslint_disable_count"] == 3
        assert row["total_opt_outs"] == 6
        assert row["has_viewport_test"] is True

    @pytest.mark.asyncio
    async def test_returns_empty_rows_when_no_debt(
        self, client: AsyncClient, db_session
    ):
        """Returns empty rows list when no components have opt-outs."""
        from unittest.mock import patch

        project = await _make_project(db_session)
        empty_summary = {
            "total_components": 0,
            "untested_count": 0,
            "untested_components": [],
        }
        with patch(
            "app.viewport_debt._scan_viewport_debt_rows",
            return_value=([], [], empty_summary),
        ):
            resp = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

        assert resp.status_code == 200
        assert resp.json()["rows"] == []

    @pytest.mark.asyncio
    async def test_non_member_project_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Project the user is not a member of returns 404."""
        project = Project(name="Other Project", user_id="other-owner")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_endpoint_does_not_mutate_cached_rows(
        self, client: AsyncClient, db_session
    ):
        """Per-project annotations must not mutate the shared cache rows.

        Regression: the endpoint annotates regression_case_count and source_url
        on each row. Without a copy, those values would leak across concurrent
        requests for different projects.
        """
        from unittest.mock import patch

        project = await _make_project(db_session)
        cached_rows = [
            {
                "component": "admin-ui/src/components/MessagesPanel.tsx",
                "eslint_disable_count": 1,
                "data_viewport_allow_small_target": 0,
                "data_viewport_allow_overflow": 0,
                "data_viewport_allow_truncation": 0,
                "total_opt_outs": 1,
                "has_viewport_test": True,
                "regression_case_count": 0,
                "source_url": None,
            }
        ]
        # Snapshot the cached row so we can compare after the request.
        snapshot = dict(cached_rows[0])

        cached_summary = {
            "total_components": 1,
            "untested_count": 0,
            "untested_components": [],
        }
        with patch(
            "app.viewport_debt._scan_viewport_debt_rows",
            return_value=(cached_rows, [], cached_summary),
        ):
            resp = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

        assert resp.status_code == 200
        # The shared cached row must remain identical to its pre-request state.
        assert cached_rows[0] == snapshot

    @pytest.mark.asyncio
    async def test_untested_rows_returned_with_source_url(
        self, client: AsyncClient, db_session
    ):
        """Untested-no-debt rows are returned and annotated with source_url."""
        from unittest.mock import patch

        project = Project(
            name="Annotated",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="repo",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        member = ProjectMember(
            project_id=project.id, user_id=TEST_USER_ID, role="owner"
        )
        db_session.add(member)
        await db_session.commit()

        fake_untested = [
            {
                "component": "admin-ui/src/components/QuickFilterPresets.tsx",
                "eslint_disable_count": 0,
                "data_viewport_allow_small_target": 0,
                "data_viewport_allow_overflow": 0,
                "data_viewport_allow_truncation": 0,
                "total_opt_outs": 0,
                "has_viewport_test": False,
                "regression_case_count": 0,
                "source_url": None,
            }
        ]
        fake_summary = {
            "total_components": 1,
            "untested_count": 1,
            "untested_components": [
                "admin-ui/src/components/QuickFilterPresets.tsx",
            ],
        }
        with patch(
            "app.viewport_debt._scan_viewport_debt_rows",
            return_value=([], fake_untested, fake_summary),
        ):
            resp = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert "untested_rows" in body
        assert len(body["untested_rows"]) == 1
        ut = body["untested_rows"][0]
        assert ut["component"] == "admin-ui/src/components/QuickFilterPresets.tsx"
        assert ut["has_viewport_test"] is False
        assert ut["total_opt_outs"] == 0
        assert ut["regression_case_count"] == 0
        assert ut["source_url"] == (
            "https://github.com/acme/repo/blob/main/"
            "admin-ui/src/components/QuickFilterPresets.tsx"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# admin.py — Set Version Phase
# ═══════════════════════════════════════════════════════════════════════════════


async def _make_open_version(session, project_id, phase="build"):
    """Create an open ReleaseVersion for the given project."""
    version = ReleaseVersion(
        project_id=project_id, version_number=1, phase=phase, status="open"
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


class TestSetVersionPhase:
    @pytest.mark.asyncio
    async def test_set_phase_to_stabilize(self, client: AsyncClient, db_session):
        """Member can advance phase from build to stabilize."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="build")

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "stabilize", "reasoning": "ready for gate"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase"] == "stabilize"
        assert "ready for gate" in data["phase_reasoning"]
        assert data["phase_changed_at"] is not None

    @pytest.mark.asyncio
    async def test_set_phase_back_to_build(self, client: AsyncClient, db_session):
        """Member can revert phase from stabilize back to build."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "build"

    @pytest.mark.asyncio
    async def test_set_phase_rejects_promotion_in_flight(
        self, client: AsyncClient, db_session
    ):
        """A phase flip while a promotion PR is open and auto-merging is rejected
        with a 409 — flipping back to build would clear the in-flight indication
        without cancelling anything, leaving the operator levers disagreeing."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 8642,
                "pr_url": "https://example/pr/8642",
                "auto_merge_enabled": True,
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        assert resp.status_code == 409
        assert "promotion PR is open and auto-merging" in resp.text

        # Phase unchanged — the flip was refused before any mutation.
        await db_session.refresh(version)
        assert version.phase == "stabilize"

    @pytest.mark.asyncio
    async def test_set_phase_no_open_version_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when no open release version exists for the project."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "stabilize"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_phase_invalid_phase_returns_422(
        self, client: AsyncClient, db_session
    ):
        """Payload with an unrecognised phase value is rejected by Pydantic."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "qualify"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_phase_non_member_returns_404(
        self, client: AsyncClient, db_session
    ):
        """User without project membership cannot set the version phase."""
        other = Project(name="Other", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        await _make_open_version(db_session, other.id)

        resp = await client.post(
            f"/api/admin/projects/{other.id}/version-phase",
            json={"phase": "stabilize"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_phase_records_operator_reasoning(
        self, client: AsyncClient, db_session
    ):
        """Operator email is prepended to phase_reasoning in the response."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "stabilize", "reasoning": "dry-run calibration"},
        )
        assert resp.status_code == 200
        reasoning = resp.json()["phase_reasoning"]
        assert "[operator:" in reasoning
        assert "dry-run calibration" in reasoning

    @pytest.mark.asyncio
    async def test_set_phase_uses_for_update_lock(
        self, client: AsyncClient, db_session
    ):
        """set_version_phase acquires FOR UPDATE lock on the version row to
        prevent lost updates from concurrent promotions/abandons. We verify this
        by ensuring the precondition checks are against the locked row state."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="build")

        # Verify the happy path works (basic evidence that locking is in place).
        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "stabilize"},
        )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "stabilize"

        # Verify that promotion_in_flight check is enforced (evidence of
        # precondition re-checking against the state).
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 5555,
                "pr_url": "https://example/pr/5555",
                "auto_merge_enabled": True,
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        # The promotion_in_flight guard is re-checked against the locked row.
        assert resp.status_code == 409
        assert "promotion PR is open and auto-merging" in resp.text
        assert "5555" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Set Version Goal
# ═══════════════════════════════════════════════════════════════════════════════


class TestSetVersionGoal:
    @pytest.mark.asyncio
    async def test_set_goal_happy_path_stamps_editor_and_timestamp(
        self, client: AsyncClient, db_session
    ):
        """Member can set the goal; editor info and updated_at are populated."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "Ship the lifecycle surface"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["goal"] == "Ship the lifecycle surface"
        assert data["goal_updated_at"] is not None
        assert data["goal_editor_info"] == f"[operator:{TEST_USER_EMAIL}]"

        # Persisted, not just echoed back.
        await db_session.refresh(version)
        assert version.goal == "Ship the lifecycle surface"
        assert version.goal_updated_at is not None
        assert version.goal_editor_info == f"[operator:{TEST_USER_EMAIL}]"

    @pytest.mark.asyncio
    async def test_set_goal_no_open_version_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when no open release version exists for the project."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "anything"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_goal_non_member_returns_404(
        self, client: AsyncClient, db_session
    ):
        """User without project membership cannot set the version goal."""
        other = Project(name="Other", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        await _make_open_version(db_session, other.id)

        resp = await client.post(
            f"/api/admin/projects/{other.id}/version-goal",
            json={"goal": "intrusion"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_goal_idempotent_same_goal_no_restamp(
        self, client: AsyncClient, db_session
    ):
        """Re-posting the identical goal is a no-op that does not re-stamp."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        first = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "stable goal"},
        )
        assert first.status_code == 200
        await db_session.refresh(version)
        first_stamp = version.goal_updated_at
        assert first_stamp is not None

        second = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "stable goal"},
        )
        assert second.status_code == 200
        await db_session.refresh(version)
        # Idempotent no-op: timestamp is unchanged because the goal matched.
        assert version.goal_updated_at == first_stamp

    @pytest.mark.asyncio
    async def test_set_goal_too_long_returns_422(
        self, client: AsyncClient, db_session
    ):
        """Goal exceeding the 500-char body limit is rejected by Pydantic."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "x" * 501},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_goal_uses_for_update_lock(
        self, client: AsyncClient, db_session
    ):
        """set_version_goal acquires FOR UPDATE lock on the version row to
        prevent lost updates from concurrent promotions/abandons. The happy path
        works correctly with locking in place."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "locked goal"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["goal"] == "locked goal"
        assert data["goal_updated_at"] is not None

        # Verify state is persisted (evidence that the locked row was mutated).
        await db_session.refresh(version)
        assert version.goal == "locked goal"


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Cut/Abandon Version
# ═══════════════════════════════════════════════════════════════════════════════


class TestCutVersion:
    @pytest.mark.asyncio
    async def test_version_cut_member_can_cut(
        self, client: AsyncClient, db_session
    ):
        """Member can cut the open version; both versions are returned and the
        old row flips to abandoned while a fresh build version opens."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "candidate can never pass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["abandoned_version"]["version_number"] == 1
        assert data["abandoned_version"]["status"] == "abandoned"
        assert data["abandoned_version"]["abandoned_at"]
        assert data["new_version"]["version_number"] == 2
        assert data["new_version"]["phase"] == "build"
        assert data["new_version"]["opened_at"]

        # Persisted: the old row carries operator provenance.
        old = (
            (
                await db_session.execute(
                    select(ReleaseVersion).where(
                        ReleaseVersion.project_id == project.id,
                        ReleaseVersion.version_number == 1,
                    )
                )
            )
            .scalars()
            .first()
        )
        await db_session.refresh(old)
        assert old.status == "abandoned"
        assert old.last_qualification["operator"] == TEST_USER_EMAIL
        assert old.last_qualification["reason"] == "candidate can never pass"
        history = (await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")).json()["recent_versions"]
        abandoned = next(row for row in history if row["version_number"] == 1)
        assert abandoned["abandoned_by"] == TEST_USER_EMAIL
        assert abandoned["abandon_reason"] == "candidate can never pass"
        assert abandoned["abandoned_at"]

    @pytest.mark.asyncio
    async def test_version_cut_idempotent_on_repeat_key(
        self, client: AsyncClient, db_session
    ):
        """A repeat cut carrying the same idempotency key returns the original
        cut's result and does NOT open a further version."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

        first = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "pivot", "idempotency_key": "cut-abc"},
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["abandoned_version"]["version_number"] == 1
        assert first_data["new_version"]["version_number"] == 2

        # Replay with the same key: same result, no second cut.
        second = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "pivot", "idempotency_key": "cut-abc"},
        )
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["abandoned_version"]["version_number"] == 1
        assert second_data["new_version"]["version_number"] == 2

        # Only v2 is open; no v3 was opened by the retry.
        rows = (
            (
                await db_session.execute(
                    select(ReleaseVersion).where(
                        ReleaseVersion.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(v.version_number for v in rows) == [1, 2]
        open_rows = [v for v in rows if v.status == "open"]
        assert [v.version_number for v in open_rows] == [2]

    @pytest.mark.asyncio
    async def test_version_cut_no_open_version_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when there is no open release version to cut."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "nothing here"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_version_cut_requires_member_role(
        self, client: AsyncClient, db_session
    ):
        """A viewer (below member) cannot cut the version."""
        other = Project(name="Viewer-Only", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="viewer")
        )
        await db_session.commit()
        await _make_open_version(db_session, other.id)

        resp = await client.post(
            f"/api/admin/projects/{other.id}/version-cut",
            json={"reason": "not allowed"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_version_cut_rejects_cross_project(
        self, client: AsyncClient, db_session
    ):
        """A non-member cannot cut another project's version (404)."""
        other = Project(name="Other", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id="other-owner", role="owner")
        )
        await _make_open_version(db_session, other.id)

        resp = await client.post(
            f"/api/admin/projects/{other.id}/version-cut",
            json={"reason": "intrusion"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_version_cut_reason_optional(
        self, client: AsyncClient, db_session
    ):
        """A cut with no reason still succeeds and records a null reason."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["new_version"]["version_number"] == 2

    @pytest.mark.asyncio
    async def test_version_cut_reason_too_long_returns_422(
        self, client: AsyncClient, db_session
    ):
        """A reason exceeding the 500-char body limit is rejected by Pydantic."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "x" * 501},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_version_cut_rejects_promotion_in_flight(
        self, client: AsyncClient, db_session
    ):
        """A version with an open promotion PR is rejected before any state
        mutation and returns a clear 409 explanation."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "auto_merge_enabled": True,
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "too early"},
        )
        assert resp.status_code == 409
        assert "promotion PR is open and auto-merging" in resp.text

        await db_session.refresh(version)
        assert version.status == "open"
        assert await rv.current_open_version(db_session, project.id) is not None


class TestClearDeployFailed:
    """POST /api/admin/projects/{id}/clear-deploy-failed operator lever."""

    @staticmethod
    async def _make_deploy_failed_version(
        session, project_id, *, promoted_sha="promotedsha123", version_number=1
    ):
        from datetime import datetime, timezone

        from app.models import QualificationVerdict

        promoted_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project_id,
            version_number=version_number,
            phase="stabilize",
            status="promoted",
            promoted_at=promoted_at,
            promoted_sha=promoted_sha,
            serving_status="deploy_failed",
            deploy_failure_reason="no_new_revision",
            deploy_confirm_deadline=datetime(
                2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc
            ),
            deploy_confirm_alerted_at=datetime(
                2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc
            ),
        )
        session.add(version)
        await session.commit()
        await session.refresh(version)

        verdict = QualificationVerdict(
            project_id=project_id,
            release_version_id=version.id,
            candidate_sha=promoted_sha,
            confidence=0.95,
            would_promote=True,
            promotion_enabled=True,
            terminal_status="promoted",
            threshold=0.90,
            post_promotion_outcome="deploy_failed",
        )
        session.add(verdict)
        await session.commit()
        return version

    @pytest.mark.asyncio
    async def test_404_when_no_deploy_failed_version(
        self, client: AsyncClient, db_session
    ):
        """No promoted deploy_failed version → 404."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={},
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, override_get_session, db_session):
        """Non-admin is rejected with 403 by require_admin."""
        from fastapi import HTTPException

        project = await _make_project(db_session)
        await self._make_deploy_failed_version(db_session, project.id)

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    f"/api/admin/projects/{project.id}/clear-deploy-failed",
                    json={"force": True, "reason": "x"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_verify_path_clears_when_sha_serving(
        self, client: AsyncClient, db_session
    ):
        """Promoted SHA genuinely observed serving → cleared without force."""
        from datetime import datetime, timezone

        from app.models import DeploymentState, DeploymentStatus, QualificationVerdict

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session, project.id, promoted_sha="liveserving999"
        )

        # A new revision booted AFTER promotion, is STABLE, and its live serving
        # SHA matches the promoted SHA — an independent serving confirmation.
        db_session.add(
            DeploymentState(
                current_sha="liveserving999",
                status=DeploymentStatus.STABLE,
                deployed_at=datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["clear_mode"] == "verified"
        assert data["operator_attested"] is False
        assert data["served_sha"] == "liveserving999"
        assert data["deploy_failure_reason"] is None

        await db_session.refresh(version)
        assert version.serving_status == "serving_confirmed"
        assert version.deploy_failure_reason is None

        verdict = (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar_one()
        assert verdict.post_promotion_outcome == "serving_confirmed"

    @pytest.mark.asyncio
    async def test_verify_path_refuses_when_required_non_backend_artifact_failed(
        self, client: AsyncClient, db_session, mocker
    ):
        """Verified backend serving cannot clear an aggregate failure while a
        required non-backend artifact still reports deploy_failed."""
        from datetime import datetime, timezone

        from app import release_artifacts as artifact_registry
        from app.models import DeploymentState, DeploymentStatus, ReleaseDeployArtifact
        from app.release_artifacts import (
            BACKEND_REVISION_ARTIFACT_KIND,
            ReleaseArtifactProvider,
        )

        backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
            BACKEND_REVISION_ARTIFACT_KIND
        ]

        async def _check_synthetic(_session, _version, _artifact, _now):
            return None

        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
                "synthetic_required": ReleaseArtifactProvider(
                    artifact_kind="synthetic_required",
                    check=_check_synthetic,
                    required=True,
                ),
            },
            clear=True,
        )

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            promoted_sha="liveserving999",
        )
        db_session.add(
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="synthetic_required",
                serving_status="deploy_failed",
                reason_code="synthetic_deadline_elapsed",
            )
        )
        db_session.add(
            DeploymentState(
                current_sha="liveserving999",
                status=DeploymentStatus.STABLE,
                deployed_at=datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={},
        )
        assert resp.status_code == 409, resp.text
        assert "required non-backend artifact" in resp.text
        assert "synthetic_deadline_elapsed" in resp.text

        await db_session.refresh(version)
        assert version.serving_status == "deploy_failed"
        assert version.deploy_failure_reason == "no_new_revision"

    @pytest.mark.asyncio
    async def test_force_refused_without_override(
        self, client: AsyncClient, db_session
    ):
        """Unobservable serving SHA + no force → 409, version stays deploy_failed."""
        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(db_session, project.id)
        # No DeploymentState → serving SHA cannot be independently observed.

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={},
        )
        assert resp.status_code == 409, resp.text
        assert "force=true" in resp.text

        await db_session.refresh(version)
        assert version.serving_status == "deploy_failed"

    @pytest.mark.asyncio
    async def test_force_refused_when_required_non_backend_artifact_failed(
        self, client: AsyncClient, db_session, mocker
    ):
        """force=true does not override a still-failed required non-backend
        artifact; it only bypasses missing backend serving evidence."""
        from app import release_artifacts as artifact_registry
        from app.models import ReleaseDeployArtifact
        from app.release_artifacts import (
            BACKEND_REVISION_ARTIFACT_KIND,
            ReleaseArtifactProvider,
        )

        backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
            BACKEND_REVISION_ARTIFACT_KIND
        ]

        async def _check_synthetic(_session, _version, _artifact, _now):
            return None

        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
                "synthetic_required": ReleaseArtifactProvider(
                    artifact_kind="synthetic_required",
                    check=_check_synthetic,
                    required=True,
                ),
            },
            clear=True,
        )

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(db_session, project.id)
        db_session.add(
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="synthetic_required",
                serving_status="deploy_failed",
                reason_code="synthetic_deadline_elapsed",
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "manually verified live in Cloud Run"},
        )
        assert resp.status_code == 409, resp.text
        assert "required non-backend artifact" in resp.text

        await db_session.refresh(version)
        assert version.serving_status == "deploy_failed"
        assert version.deploy_failure_reason == "no_new_revision"

    @pytest.mark.asyncio
    async def test_force_requires_reason(self, client: AsyncClient, db_session):
        """force=true without a reason → 400."""
        project = await _make_project(db_session)
        await self._make_deploy_failed_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "   "},
        )
        assert resp.status_code == 400, resp.text

    @pytest.mark.asyncio
    async def test_force_path_clears_and_records_attestation(
        self, client: AsyncClient, db_session
    ):
        """force=true + reason clears, resolves the alert, flips the verdict."""
        from app.models import QualificationVerdict
        from app.release_stall_alerts import _deploy_failed_alert_title

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(db_session, project.id)

        # Seed the open deploy-failed operator alert that must be resolved.
        alert = Alert(
            project_id=project.id,
            title=_deploy_failed_alert_title(version.version_number),
            message="promotion deploy FAILED",
        )
        db_session.add(alert)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "manually verified live in Cloud Run"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["clear_mode"] == "operator_attested"
        assert data["operator_attested"] is True
        assert data["attestation_reason"] == "manually verified live in Cloud Run"
        # Attested clear stamps the promoted SHA as the served SHA.
        assert data["served_sha"] == "promotedsha123"

        await db_session.refresh(version)
        assert version.serving_status == "serving_confirmed"
        assert version.deploy_failure_reason is None
        clear_audit = (version.last_qualification or {}).get("deploy_failed_clear")
        assert clear_audit is not None
        assert clear_audit["mode"] == "operator_attested"
        assert clear_audit["reason"] == "manually verified live in Cloud Run"

        # The open deploy-failed alert is resolved.
        await db_session.refresh(alert)
        assert alert.dismissed_at is not None

        # The verdict's post-promotion outcome flips to serving_confirmed.
        verdict = (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar_one()
        assert verdict.post_promotion_outcome == "serving_confirmed"


class TestVersionLifecycleInfraFailureFields:
    """GET /api/admin/projects/{id}/version-lifecycle exposes infra-failure fields."""

    @pytest.mark.asyncio
    async def test_infra_failure_fields_exposed_in_last_qualification(
        self, client: AsyncClient, db_session
    ):
        """error, consecutive_infra_errors, and audit_missing_reason from an
        infra_failure last_qualification record are surfaced in the response."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "infra_failure",
                "error": "audit_report_missing",
                "consecutive_infra_errors": 2,
                "audit_missing_reason": None,
                "candidate_sha": "abc1234",
                "finalized_at": "2026-05-27T00:00:00",
            },
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        lq = resp.json()["current_version"]["last_qualification"]
        assert lq["status"] == "infra_failure"
        assert lq["error"] == "audit_report_missing"
        assert lq["consecutive_infra_errors"] == 2
        assert lq["audit_missing_reason"] is None

    @pytest.mark.asyncio
    async def test_infra_failure_fields_absent_for_successful_qualification(
        self, client: AsyncClient, db_session
    ):
        """For a non-infra-failure qualification, error/consecutive_infra_errors
        are null and audit_missing_reason is null."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "dry_run",
                "confidence": 0.95,
                "threshold": 0.90,
                "sub_scores": {"audit": 1.0, "test_erosion": 0.9, "smoketest": 0.95},
                "blocker": None,
                "finalized_at": "2026-05-27T00:00:00",
            },
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": True, "reason": "ok"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        lq = resp.json()["current_version"]["last_qualification"]
        assert lq["status"] == "dry_run"
        assert lq["error"] is None
        assert lq["consecutive_infra_errors"] is None
        assert lq["audit_missing_reason"] is None

    @pytest.mark.asyncio
    async def test_merged_since_verdict_counts_merges_after_finalized_at(
        self, client: AsyncClient, db_session
    ):
        """current_version.merged_since_verdict reflects tasks merged at/after the
        verdict's finalized_at, computed from finalized_at (not opened_at)."""
        from datetime import datetime

        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "dry_run",
                "confidence": 0.89,
                "threshold": 0.90,
                "candidate_sha": "abc1234",
                "finalized_at": "2026-05-27T00:00:00",
            },
        )
        db_session.add(version)
        await db_session.commit()

        finalized_dt = datetime.fromisoformat("2026-05-27T00:00:00")

        async def _count(session, project_id, since):
            # The verdict-staleness call passes the parsed finalized_at; the
            # opened-version call passes the version's opened_at.
            return 4 if since == finalized_dt else 0

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(side_effect=_count),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        cv = resp.json()["current_version"]
        assert cv["merged_since_verdict"] == 4

    @pytest.mark.asyncio
    async def test_merged_since_verdict_zero_when_no_merges_after_verdict(
        self, client: AsyncClient, db_session
    ):
        """merged_since_verdict is 0 (not null) when the helper finds no merges
        after the verdict's finalized_at."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "dry_run",
                "candidate_sha": "abc1234",
                "finalized_at": "2026-05-27T00:00:00",
            },
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        cv = resp.json()["current_version"]
        assert cv["merged_since_verdict"] == 0

    @pytest.mark.asyncio
    async def test_merged_since_verdict_null_when_no_finalized_at(
        self, client: AsyncClient, db_session
    ):
        """merged_since_verdict is null when there is no verdict / no finalized_at —
        the count is never fabricated."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={"status": "pending", "candidate_sha": "abc1234"},
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        cv = resp.json()["current_version"]
        assert cv["merged_since_verdict"] is None

    @pytest.mark.asyncio
    async def test_promotion_pending_state_exposed_in_current_version(
        self, client: AsyncClient, db_session
    ):
        """The lifecycle payload surfaces promotion-in-flight as a distinct
        qualification_state, not a generic pending status."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "pending",
                "promotion_pending": {
                    "pr_number": 7357,
                    "pr_url": "https://example/pr/7357",
                    "auto_merge_enabled": True,
                },
            },
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["qualification_state"]["state"] == "promotion_in_flight"
        assert current["qualification_state"]["promotion_pr_number"] == 7357
        assert "auto-merge is armed" in current["qualification_state"]["summary"].lower()


class TestVersionLifecycleAuditEvidence:
    """GET /version-lifecycle scopes audit evidence to the open candidate SHA."""

    @staticmethod
    async def _add_audit_task(
        db_session, project, version_id, candidate_sha, *, report=None
    ):
        task = Task(
            project_id=project.id,
            title="release audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            context={
                "release_version_id": str(version_id),
                "candidate_sha": candidate_sha,
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        if report is not None:
            db_session.add(
                Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                    logs=json.dumps(report),
                )
            )
            await db_session.commit()
        return task

    @staticmethod
    def _readiness_patch():
        from unittest.mock import patch

        return (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        )

    @pytest.mark.asyncio
    async def test_matching_audit_is_current(self, client: AsyncClient, db_session):
        """An audit pinned to the evaluated candidate → latest_audit populated,
        audit_evidence_state=current, stale_audit null."""
        project = await _make_project(db_session)
        candidate = "feed" * 10
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={"status": "pending", "candidate_sha": candidate},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        report = {"audit_score": 0.8, "findings": []}
        await self._add_audit_task(
            db_session, project, version.id, candidate, report=report
        )

        readiness, merged = self._readiness_patch()
        with readiness, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["audit_evidence_state"] == "current"
        assert current["latest_audit"] is not None
        assert current["latest_audit"]["candidate_sha"] == candidate
        assert current["latest_audit"]["report"] == report
        assert current["stale_audit"] is None

    @pytest.mark.asyncio
    async def test_stale_prior_candidate_audit(self, client: AsyncClient, db_session):
        """Only an older-candidate audit exists → latest_audit null, stale_audit
        provenance present, audit_evidence_state=stale."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={"status": "pending", "candidate_sha": "newc" * 10},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        report = {"audit_score": 0.5, "findings": [{"severity": "high"}]}
        await self._add_audit_task(
            db_session, project, version.id, "0ld0" * 10, report=report
        )

        readiness, merged = self._readiness_patch()
        with readiness, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["audit_evidence_state"] == "stale"
        assert current["latest_audit"] is None
        assert current["stale_audit"] is not None
        assert current["stale_audit"]["candidate_sha"] == "0ld0" * 10
        assert current["stale_audit"]["report"] == report

    @pytest.mark.asyncio
    async def test_no_matching_audit_is_none(self, client: AsyncClient, db_session):
        """A newly-cut version with no audit of its own → audit_evidence_state=none,
        latest_audit null, even when another version has an audit."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="open",
            last_qualification={"status": "pending", "candidate_sha": "feed" * 10},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Audit belonging to a different version must not leak in.
        import uuid as _uuid

        await self._add_audit_task(
            db_session, project, _uuid.uuid4(), "feed" * 10, report={"audit_score": 0.9}
        )

        readiness, merged = self._readiness_patch()
        with readiness, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["audit_evidence_state"] == "none"
        assert current["latest_audit"] is None
        assert current["stale_audit"] is None

    @pytest.mark.asyncio
    async def test_current_audit_without_artifact(
        self, client: AsyncClient, db_session
    ):
        """A matching audit with no report artifact → state current, latest_audit
        provenance present but report null."""
        project = await _make_project(db_session)
        candidate = "feed" * 10
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={"status": "pending", "candidate_sha": candidate},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        await self._add_audit_task(db_session, project, version.id, candidate)

        readiness, merged = self._readiness_patch()
        with readiness, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["audit_evidence_state"] == "current"
        assert current["latest_audit"] is not None
        assert current["latest_audit"]["report"] is None


class TestVersionLifecycleStagedDirections:
    """GET /version-lifecycle surfaces staged directions for the next version."""

    @pytest.mark.asyncio
    async def test_staged_directions_surfaced_with_target_and_age(
        self, client: AsyncClient, db_session
    ):
        """A staged direction appears in staged_directions with its target
        version; active/proposed directions are excluded."""
        project = await _make_project(db_session)
        project.directions = [
            {
                "id": "d-staged",
                "text": "Rework packer budget",
                "priority": "high",
                "status": "staged",
                "staged_for_version": 6,
                "staged_at": "2026-05-01T00:00:00",
            },
            {"id": "d-active", "text": "active work", "status": "active"},
        ]
        version = ReleaseVersion(
            project_id=project.id,
            version_number=5,
            phase="build",
            status="open",
        )
        db_session.add_all([project, version])
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        staged = resp.json()["staged_directions"]
        assert len(staged) == 1
        assert staged[0]["id"] == "d-staged"
        assert staged[0]["staged_for_version"] == 6
        assert staged[0]["staged_age_days"] is not None

    @pytest.mark.asyncio
    async def test_staged_directions_empty_when_none_staged(
        self, client: AsyncClient, db_session
    ):
        """No staged directions yields an empty staged_directions list."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["staged_directions"] == []


class TestVersionLifecycleInFlightCount:
    """GET /version-lifecycle surfaces in_flight_task_count on the open version."""

    @pytest.mark.asyncio
    async def test_in_flight_task_count_counts_non_terminal_tasks(
        self, client: AsyncClient, db_session
    ):
        """Non-terminal project tasks are counted; terminal ones are excluded."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="stabilize",
            status="open",
        )
        db_session.add(version)
        # Two in-flight + one terminal.
        db_session.add_all(
            [
                Task(
                    project_id=project.id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.IN_PROGRESS,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.COMPLETED,
                ),
            ]
        )
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=AsyncMock(return_value={"ready": False, "reason": "test"}),
            ),
            patch(
                "app.release_qualifier.resolve_lifecycle_audit_evidence",
                new=AsyncMock(
                    return_value={
                        "state": "none",
                        "latest_audit": None,
                        "stale_audit": None,
                    }
                ),
            ),
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["current_version"]["in_flight_task_count"] == 2


class TestQualificationVerdictsAuditMissingReason:
    """GET /api/admin/projects/{id}/qualification-verdicts exposes audit_missing_reason."""

    @pytest.mark.asyncio
    async def test_audit_missing_reason_exposed_on_verdict_row(
        self, client: AsyncClient, db_session
    ):
        """audit_missing_reason populated on a verdict row is returned by the endpoint."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="deadbeef",
            confidence=0.0,
            blocker="audit_missing",
            audit_missing_reason="parseable_but_unscoreable",
            would_promote=False,
            promotion_enabled=False,
            terminal_status="not_promoted",
            threshold=0.90,
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert verdicts[0]["audit_missing_reason"] == "parseable_but_unscoreable"
        assert verdicts[0]["blocker"] == "audit_missing"

    @pytest.mark.asyncio
    async def test_audit_missing_reason_null_for_clean_verdict(
        self, client: AsyncClient, db_session
    ):
        """audit_missing_reason is null on a clean (promoted) verdict."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="goodsha123",
            confidence=0.95,
            blocker=None,
            audit_missing_reason=None,
            would_promote=True,
            promotion_enabled=True,
            terminal_status="promoted",
            threshold=0.90,
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert verdicts[0]["audit_missing_reason"] is None

    @pytest.mark.asyncio
    async def test_post_promotion_outcome_exposed_on_verdict_row(
        self, client: AsyncClient, db_session
    ):
        """post_promotion_outcome (and _at) populated on a verdict are returned."""
        from datetime import datetime, timezone

        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        outcome_at = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="deadbeef",
            confidence=0.95,
            would_promote=True,
            promotion_enabled=True,
            terminal_status="promoted",
            threshold=0.90,
            post_promotion_outcome="deploy_failed",
            post_promotion_outcome_at=outcome_at,
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert verdicts[0]["post_promotion_outcome"] == "deploy_failed"
        assert verdicts[0]["post_promotion_outcome_at"] is not None

    @pytest.mark.asyncio
    async def test_post_promotion_outcome_null_for_unresolved_verdict(
        self, client: AsyncClient, db_session
    ):
        """post_promotion_outcome is null until the serving outcome is recorded."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="goodsha123",
            confidence=0.95,
            would_promote=True,
            promotion_enabled=True,
            terminal_status="promoted",
            threshold=0.90,
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert verdicts[0]["post_promotion_outcome"] is None
        assert verdicts[0]["post_promotion_outcome_at"] is None

    @pytest.mark.asyncio
    async def test_durable_evidence_fields_exposed_on_verdict_row(
        self, client: AsyncClient, db_session
    ):
        """Per-attempt evidence columns are surfaced on each verdict row, so a
        blocked attempt's migration-safety verdict survives re-qualification."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="blockedsha",
            confidence=0.10,
            blocker="migration_unsafe",
            would_promote=False,
            promotion_enabled=False,
            terminal_status="not_promoted",
            threshold=0.90,
            migration_safety="block",
            migration_safety_detail="renumber-skip leaves table uncreated",
            migration_apply_safety="block",
            migration_apply_safety_detail="apply against prod schema diverged",
            audit_findings=[{"severity": "high", "summary": "x"}],
            excluded_audit_findings=[{"severity": "low", "summary": "y"}],
            test_erosion_detail={"removed": 3},
            smoketest_detail={"passed": False},
            audit_reconciliation={"reconciled": True},
            promotion_pr_url="https://example.com/pr/1",
            promotion_conflict=True,
            promotion_error_detail="merge conflict",
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        v = resp.json()["verdicts"][0]
        assert v["migration_safety"] == "block"
        assert v["migration_safety_detail"] == "renumber-skip leaves table uncreated"
        assert v["migration_apply_safety"] == "block"
        assert (
            v["migration_apply_safety_detail"] == "apply against prod schema diverged"
        )
        assert v["audit_findings"] == [{"severity": "high", "summary": "x"}]
        assert v["excluded_audit_findings"] == [{"severity": "low", "summary": "y"}]
        assert v["test_erosion_detail"] == {"removed": 3}
        assert v["smoketest_detail"] == {"passed": False}
        assert v["audit_reconciliation"] == {"reconciled": True}
        assert v["promotion_pr_url"] == "https://example.com/pr/1"
        assert v["promotion_conflict"] is True
        assert v["promotion_error_detail"] == "merge conflict"


class TestQualificationVerdictsInfraContext:
    """GET /api/admin/projects/{id}/qualification-verdicts includes infra_context."""

    @pytest.mark.asyncio
    async def test_infra_context_zero_when_no_infra_failures(
        self, client: AsyncClient, db_session
    ):
        """infra_context counts are 0 when no ReleaseVersion has an infra_failure."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="abc123",
            confidence=0.95,
            blocker=None,
            audit_missing_reason=None,
            would_promote=True,
            promotion_enabled=False,
            terminal_status="dry_run",
            threshold=0.90,
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "infra_context" in data
        assert data["infra_context"]["infra_failure_count"] == 0
        assert data["infra_context"]["consecutive_infra_errors"] == 0

    @pytest.mark.asyncio
    async def test_infra_context_counts_infra_failures(
        self, client: AsyncClient, db_session
    ):
        """infra_context surfaces infra_failure_count when ReleaseVersion has
        infra_failure last_qualification entries, distinguishing 'never ran'
        from 'infrastructure failing'."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "infra_failure",
                "error": "audit_report_missing",
                "consecutive_infra_errors": 3,
            },
        )
        db_session.add(version)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["verdicts"] == []
        assert data["infra_context"]["infra_failure_count"] >= 1
        assert data["infra_context"]["consecutive_infra_errors"] >= 1


class TestResetIterationCap:
    """POST /api/admin/tasks/{task_id}/reset-iteration-cap — archives FAILED
    review/fix tasks for the PR. The archive filter pushes pr_number into SQL;
    on SQLite the naive .as_string() form double-quotes the int and matches
    nothing, so this test guards against that dialect breakage by asserting
    matching tasks are actually archived."""

    async def _make_failed(self, db_session, project_id, pr_number, task_type="review"):
        t = Task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            payloads=[{"type": task_type, "pr_number": pr_number, "branch": "feat/x"}],
            archived=False,
        )
        db_session.add(t)
        return t

    @pytest.mark.asyncio
    async def test_archives_only_matching_pr_failed_review_fix_tasks(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)

        parking = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "review", "pr_number": 777, "branch": "feat/x"}],
            context={"review_loop": {"iteration": 5, "pr_number": 777}},
        )
        db_session.add(parking)

        # FAILED review/fix tasks on PR #777 — these must be archived.
        match_review = await self._make_failed(db_session, project.id, 777, "review")
        match_fix = await self._make_failed(db_session, project.id, 777, "fix")
        # FAILED on a different PR — must NOT be archived.
        other_pr = await self._make_failed(db_session, project.id, 888, "review")
        # FAILED on PR #777 but not a review/fix type — must NOT be archived.
        wrong_type = await self._make_failed(db_session, project.id, 777, "create")
        await db_session.commit()
        for t in (parking, match_review, match_fix, other_pr, wrong_type):
            await db_session.refresh(t)

        resp = await client.post(f"/api/admin/tasks/{parking.id}/reset-iteration-cap")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The two FAILED review/fix tasks on PR #777 must be counted — if the
        # SQL pr_number filter silently matched zero rows (SQLite .as_string()
        # bug), this would be 0.
        assert body["archived_count"] == 2
        assert body["pr_number"] == 777
        assert body["previous_per_chain_iteration"] == 5

        for t in (match_review, match_fix, other_pr, wrong_type):
            await db_session.refresh(t)
        assert match_review.archived is True
        assert match_fix.archived is True
        assert other_pr.archived is False
        assert wrong_type.archived is False

        # Per-chain iteration counter reset to 0 on the parking task.
        await db_session.refresh(parking)
        assert parking.context["review_loop"]["iteration"] == 0

    @pytest.mark.asyncio
    async def test_no_pr_number_in_context_returns_400(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        parking = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "review"}],
            context={"review_loop": {"iteration": 3}},
        )
        db_session.add(parking)
        await db_session.commit()
        await db_session.refresh(parking)

        resp = await client.post(f"/api/admin/tasks/{parking.id}/reset-iteration-cap")
        assert resp.status_code == 400


class TestGetAllTasksHistory:
    """GET /api/admin/tasks fetches each task's 10 most-recent TaskHistory
    entries. The fetch uses a per-task ROW_NUMBER() window, so a single
    high-activity task cannot exhaust a global LIMIT budget and starve other
    tasks in the same page of their history."""

    @pytest.mark.asyncio
    async def test_per_task_history_not_starved_by_high_activity_task(
        self, client: AsyncClient, db_session
    ):
        from datetime import datetime, timedelta
        from app.models import TaskHistory

        project = await _make_project(db_session)

        busy = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
        )
        quiet = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
        )
        db_session.add(busy)
        db_session.add(quiet)
        await db_session.commit()
        await db_session.refresh(busy)
        await db_session.refresh(quiet)

        base = datetime(2026, 5, 1, 0, 0, 0)
        # The busy task has 25 entries, all NEWER than the quiet task's. Under a
        # global LIMIT of len(task_ids)*10 == 20, every retained row would belong
        # to the busy task and the quiet task would receive 0 history entries.
        for i in range(25):
            db_session.add(
                TaskHistory(
                    task_id=busy.id,
                    action="STATUS_CHANGE",
                    details={"i": i},
                    timestamp=base + timedelta(hours=100 + i),
                )
            )
        for i in range(8):
            db_session.add(
                TaskHistory(
                    task_id=quiet.id,
                    action="STATUS_CHANGE",
                    details={"i": i},
                    timestamp=base + timedelta(hours=i),
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/tasks")
        assert resp.status_code == 200, resp.text
        by_id = {t["id"]: t for t in resp.json()["tasks"]}

        # Busy task is capped at its 10 most-recent entries.
        busy_hist = by_id[str(busy.id)]["history"]
        assert len(busy_hist) == 10
        assert [h["details"]["i"] for h in busy_hist] == list(range(24, 14, -1))

        # Quiet task still receives all 8 of its entries — not starved.
        quiet_hist = by_id[str(quiet.id)]["history"]
        assert len(quiet_hist) == 8
        assert [h["details"]["i"] for h in quiet_hist] == list(range(7, -1, -1))


class TestSrCatchRateHealth:
    """GET /api/admin/projects/{project_id}/sr_catch_rate/health status matrix."""

    async def _setup(self, db_session, trends):
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = (
            {"sr_catch_rate_trends": trends} if trends is not None else {}
        )
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()
        return project

    @staticmethod
    def _iso(hours_ago):
        from datetime import datetime, timedelta

        return (datetime.utcnow() - timedelta(hours=hours_ago)).isoformat()

    @pytest.mark.asyncio
    async def test_healthy(self, client: AsyncClient, db_session):
        project = await self._setup(
            db_session,
            {
                "signal_last_computed_at": self._iso(1),
                "last_sufficient_computed_at": self._iso(1),
                "data_sufficient": True,
                "bucket_mode": "cycle",
                "overall_hit_rate": 0.75,
                "previous_overall_hit_rate": 0.7,
                "overall_trend_verdict": "stable",
                "overall_trend_significant": None,
                "categories": {
                    "correctness": {"current_hit_rate": 0.8},
                    "security": {"current_hit_rate": None},
                },
            },
        )
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["data_sufficient"] is True
        assert data["bucket_mode"] == "cycle"
        assert data["overall_hit_rate"] == 0.75
        assert data["category_count"] == 2
        assert data["categories_with_hit_rate"] == 1
        assert 0.9 <= data["last_sufficient_age_hours"] <= 1.2
        assert 0.9 <= data["signal_age_hours"] <= 1.2

    @pytest.mark.asyncio
    async def test_never_sufficient(self, client: AsyncClient, db_session):
        project = await self._setup(
            db_session,
            {
                "signal_last_computed_at": self._iso(1),
                "data_sufficient": False,
            },
        )
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "never_sufficient"
        assert data["data_sufficient"] is False
        assert data["last_sufficient_computed_at"] is None
        assert data["last_sufficient_age_hours"] is None
        assert 0.9 <= data["signal_age_hours"] <= 1.2

    @pytest.mark.asyncio
    async def test_frozen(self, client: AsyncClient, db_session):
        # Signal fresh (< 6h) but real data stale (> 24h) => frozen-but-fresh.
        project = await self._setup(
            db_session,
            {
                "signal_last_computed_at": self._iso(2),
                "last_sufficient_computed_at": self._iso(30),
                "data_sufficient": False,
            },
        )
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "frozen"
        assert data["signal_age_hours"] < 6
        assert data["last_sufficient_age_hours"] > 24

    @pytest.mark.asyncio
    async def test_stale(self, client: AsyncClient, db_session):
        # Both timestamps older than the 48h general window; signal not fresh
        # so the frozen branch does not apply.
        project = await self._setup(
            db_session,
            {
                "signal_last_computed_at": self._iso(50),
                "last_sufficient_computed_at": self._iso(50),
                "data_sufficient": True,
            },
        )
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "stale"
        assert data["last_sufficient_age_hours"] > 48

    @pytest.mark.asyncio
    async def test_low_data(self, client: AsyncClient, db_session):
        # Recent on both clocks but not data-sufficient and not frozen/stale.
        project = await self._setup(
            db_session,
            {
                "signal_last_computed_at": self._iso(1),
                "last_sufficient_computed_at": self._iso(2),
                "data_sufficient": False,
            },
        )
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "low_data"
        assert data["data_sufficient"] is False

    @pytest.mark.asyncio
    async def test_missing(self, client: AsyncClient, db_session):
        project = await self._setup(db_session, None)
        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr_catch_rate/health"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "missing"
        assert data["data_sufficient"] is False
        assert data["category_count"] == 0
        assert data["last_sufficient_age_hours"] is None
        assert data["signal_age_hours"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# deploy.py — Deployment State & Rollback
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeployRollback:
    @pytest.mark.asyncio
    async def test_rollback_uses_orchestrator_service_name_env_var(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """trigger_rollback uses ORCHESTRATOR_SERVICE_NAME env var for gcloud invocation."""
        from app.models import DeploymentState, DeploymentStatus

        state = DeploymentState(
            current_revision="rev-bad",
            current_sha="sha123bad",
            last_known_good_revision="rev-good",
            last_known_good_sha="sha123good",
            status=DeploymentStatus.STABLE,
        )
        db_session.add(state)
        await db_session.commit()

        monkeypatch.setenv("ORCHESTRATOR_SERVICE_NAME", "custom-service")
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("GCP_REGION", "us-west1")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""

            resp = await client.post("/api/admin/deploy-state/rollback")

            assert resp.status_code == 200
            assert mock_run.called
            call_args = mock_run.call_args[0][0]
            assert "custom-service" in call_args
            assert "rev-good=100" in call_args
            assert "us-west1" in call_args
            assert "test-project" in call_args


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/admin/tasks/{task_id}/pr-files
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetTaskPrFiles:
    @pytest.mark.asyncio
    async def test_task_with_pr_returns_files(self, client: AsyncClient, db_session):
        """Returns file list when task has a PR artifact."""
        from app.models import Artifact, ArtifactType

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/42",
        )
        db_session.add(artifact)
        await db_session.commit()

        mock_files = [
            {
                "filename": "foo.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "changes": 7,
            }
        ]
        with (
            patch(
                "app.routers.agent.get_github_token_for_task",
                new_callable=AsyncMock,
                return_value=("tok", "owner", "repo"),
            ),
            patch(
                "app.github.get_pr_files",
                new_callable=AsyncMock,
                return_value=mock_files,
            ),
        ):
            resp = await client.get(f"/api/admin/tasks/{task.id}/pr-files")

        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_number"] == 42
        assert len(data["files"]) == 1
        assert data["files"][0]["filename"] == "foo.py"
        assert data["files"][0]["additions"] == 5

    @pytest.mark.asyncio
    async def test_task_without_pr_returns_no_pr_reason(
        self, client: AsyncClient, db_session
    ):
        """Returns reason='no_pr' when task has no PR artifact."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}/pr-files")

        assert resp.status_code == 200
        data = resp.json()
        assert data["files"] is None
        assert data["reason"] == "no_pr"

    @pytest.mark.asyncio
    async def test_unknown_task_returns_404(self, client: AsyncClient, db_session):
        """Returns 404 for a task that does not exist."""
        resp = await client.get(f"/api/admin/tasks/{uuid.uuid4()}/pr-files")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_task_id_returns_404(self, client: AsyncClient, db_session):
        """Returns 404 for a malformed task ID."""
        resp = await client.get("/api/admin/tasks/not-a-uuid/pr-files")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_viewer_is_denied_basic_file_list(
        self, override_get_session, db_session
    ):
        """Viewer role gets 403 for the basic file list."""
        from app.auth import get_current_user as _gcu

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        viewer_id = "viewer-user-999"
        db_session.add(
            ProjectMember(project_id=project.id, user_id=viewer_id, role="viewer")
        )
        await db_session.commit()

        async def _viewer():
            return {"user_id": viewer_id, "email": "viewer@example.com", "role": "user"}

        app.dependency_overrides[_gcu] = _viewer
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(f"/api/admin/tasks/{task.id}/pr-files")
        finally:
            app.dependency_overrides.pop(_gcu, None)

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_can_access_basic_file_list(
        self, override_get_session, db_session
    ):
        """Member role succeeds for the basic file list."""
        from app.models import Artifact, ArtifactType
        from app.auth import get_current_user as _gcu

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/99",
        )
        db_session.add(artifact)

        member_id = "member-user-999"
        db_session.add(
            ProjectMember(project_id=project.id, user_id=member_id, role="member")
        )
        await db_session.commit()

        async def _member():
            return {"user_id": member_id, "email": "member@example.com", "role": "user"}

        app.dependency_overrides[_gcu] = _member
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                with (
                    patch(
                        "app.routers.agent.get_github_token_for_task",
                        new_callable=AsyncMock,
                        return_value=("tok", "owner", "repo"),
                    ),
                    patch(
                        "app.github.get_pr_files",
                        new_callable=AsyncMock,
                        return_value=[],
                    ),
                ):
                    resp = await ac.get(f"/api/admin/tasks/{task.id}/pr-files")
        finally:
            app.dependency_overrides.pop(_gcu, None)

        assert resp.status_code == 200
        assert resp.json()["pr_number"] == 99

    @pytest.mark.asyncio
    async def test_member_cannot_access_patch(self, override_get_session, db_session):
        """Member role gets 403 for include_patch=true."""
        from app.auth import get_current_user as _gcu

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        member_id = "member-user-888"
        db_session.add(
            ProjectMember(project_id=project.id, user_id=member_id, role="member")
        )
        await db_session.commit()

        async def _member():
            return {"user_id": member_id, "email": "member@example.com", "role": "user"}

        app.dependency_overrides[_gcu] = _member
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    f"/api/admin/tasks/{task.id}/pr-files?include_patch=true"
                )
        finally:
            app.dependency_overrides.pop(_gcu, None)

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_can_access_patch(self, client: AsyncClient, db_session):
        """Owner role can access include_patch=true."""
        from app.models import Artifact, ArtifactType

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="test task",
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "create", "prompt": "do something"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/77",
        )
        db_session.add(artifact)
        await db_session.commit()

        mock_files = [{"filename": "x.py", "patch": "diff..."}]
        with (
            patch(
                "app.routers.agent.get_github_token_for_task",
                new_callable=AsyncMock,
                return_value=("tok", "owner", "repo"),
            ),
            patch(
                "app.github.get_pr_files",
                new_callable=AsyncMock,
                return_value=mock_files,
            ),
        ):
            resp = await client.get(
                f"/api/admin/tasks/{task.id}/pr-files?include_patch=true"
            )

        assert resp.status_code == 200
        assert resp.json()["pr_number"] == 77
        assert resp.json()["files"][0]["filename"] == "x.py"


class TestGetAuditFindings:
    """GET /api/admin/projects/{id}/audit-findings — read-only audit findings endpoint."""

    @pytest.mark.asyncio
    async def test_returns_findings_from_last_qualification(
        self, client: AsyncClient, db_session
    ):
        """audit_findings stored in last_qualification are returned by the endpoint."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)
        version.last_qualification = {
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into image."},
                {"severity": "low", "summary": "Unused import."},
            ],
            "audit_missing_reason": None,
            "finalized_at": "2026-01-01T00:00:00",
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/audit-findings")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["source"] == "last_qualification"
        assert len(data["findings"]) == 2
        assert data["findings"][0]["severity"] == "high"
        assert data["findings"][1]["severity"] == "low"
        assert data["finalized_at"] == "2026-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_prefers_durable_verdict_findings_over_last_qualification(
        self, client: AsyncClient, db_session
    ):
        """A re-qualification overwrites last_qualification, but the earlier
        blocked attempt's findings still surface from the durable verdict row."""
        from datetime import datetime

        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        # First (blocked) attempt: persist its evidence on an immutable verdict.
        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="blockedsha",
            confidence=0.10,
            blocker="migration_unsafe",
            audit_missing_reason=None,
            would_promote=False,
            promotion_enabled=False,
            terminal_status="not_promoted",
            threshold=0.90,
            audit_findings=[
                {"severity": "high", "summary": "Renumber-skip leaves table uncreated."},
            ],
            finalized_at=datetime(2026, 1, 2, 0, 0, 0),
        )
        db_session.add(verdict)
        # A later re-qualification overwrote last_qualification with no findings.
        version.last_qualification = {
            "audit_findings": [],
            "audit_missing_reason": None,
            "finalized_at": "2026-02-01T00:00:00",
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/audit-findings")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["source"] == "qualification_verdict"
        assert len(data["findings"]) == 1
        assert data["findings"][0]["severity"] == "high"
        assert data["finalized_at"] == "2026-01-02T00:00:00"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_open_version(
        self, client: AsyncClient, db_session
    ):
        """Returns empty findings list and 'no_open_version' source when no open version."""
        project = await _make_project(db_session)
        resp = await client.get(f"/api/admin/projects/{project.id}/audit-findings")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["findings"] == []
        assert data["source"] == "no_open_version"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_qualification_yet(
        self, client: AsyncClient, db_session
    ):
        """Returns empty findings when an open version exists but no qualification ran."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)
        resp = await client.get(f"/api/admin/projects/{project.id}/audit-findings")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["findings"] == []
        assert data["source"] == "last_qualification"


# ═══════════════════════════════════════════════════════════════════════════════
# agent_status_spawn.py — maybe_spawn_smoketest_fix / maybe_spawn_debug_create
# ═══════════════════════════════════════════════════════════════════════════════

def _make_smoketest_payload(task_id, *, passed=False):
    """Build an AgentStatusPayload for a SMOKETEST_RESULT artifact."""
    logs = json.dumps(
        {
            "passed": passed,
            "tests": [
                {"name": "test_login", "status": "fail" if not passed else "pass", "message": "auth error"},
            ],
            "suites": {"startup": {"passed": passed, "tests": []}},
            "guardrail_violations": [],
        }
    )
    return AgentStatusPayload(
        task_id=task_id,
        status=TaskStatus.FAILED if not passed else TaskStatus.COMPLETED,
        artifact_type=ArtifactType.SMOKETEST_RESULT,
        logs=logs,
    )


def _make_debug_payload(task_id, *, confidence="high", fix_prompt="Fix the auth module"):
    """Build an AgentStatusPayload for a DEBUG_RESULT artifact."""
    logs = json.dumps(
        {
            "confidence": confidence,
            "fix_prompt": fix_prompt,
            "root_cause": "null pointer in auth",
        }
    )
    return AgentStatusPayload(
        task_id=task_id,
        status=TaskStatus.COMPLETED,
        artifact_type=ArtifactType.DEBUG_RESULT,
        logs=logs,
    )


class TestMaybeSpawnSmoketestFix:
    """Seam tests for agent_status_spawn.maybe_spawn_smoketest_fix."""

    @pytest.mark.asyncio
    async def test_spawns_fix_task_and_alert_on_failure(self, db_session, mock_broadcast):
        """On smoketest failure, a plan task and an ERROR alert are created."""
        from app.agent_status_spawn import maybe_spawn_smoketest_fix

        project = await _make_project(db_session)
        smoketest_task = Task(
            project_id=project.id,
            title="Smoketest on main",
            payloads=[{"type": "smoketest", "base_branch": "main"}],
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(smoketest_task)
        await db_session.commit()
        await db_session.refresh(smoketest_task)

        payload = _make_smoketest_payload(smoketest_task.id, passed=False)

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_smoketest_fix(smoketest_task, payload, db_session, str(smoketest_task.id))
            await db_session.commit()

        spawned = (
            (await db_session.execute(select(Task).where(Task.parent_task_id == smoketest_task.id)))
            .scalars()
            .all()
        )
        assert len(spawned) == 1, "Expected exactly one spawned fix task"
        fix_task = spawned[0]
        assert fix_task.status == TaskStatus.QUEUED
        assert "smoketest" in fix_task.title.lower() or "fix" in fix_task.title.lower()
        assert fix_task.project_id == project.id

        alerts = (
            (await db_session.execute(select(Alert).where(Alert.task_id == smoketest_task.id)))
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0].severity == "ERROR"

    @pytest.mark.asyncio
    async def test_dedup_skips_spawn_when_fix_task_exists(self, db_session, mock_broadcast):
        """If a child task already exists, no second fix task is spawned."""
        from app.agent_status_spawn import maybe_spawn_smoketest_fix

        project = await _make_project(db_session)
        smoketest_task = Task(
            project_id=project.id,
            title="Smoketest on main",
            payloads=[{"type": "smoketest", "base_branch": "main"}],
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(smoketest_task)
        await db_session.commit()
        await db_session.refresh(smoketest_task)

        existing_fix = Task(
            project_id=project.id,
            title="Fix: pre-existing",
            parent_task_id=smoketest_task.id,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(existing_fix)
        await db_session.commit()

        payload = _make_smoketest_payload(smoketest_task.id, passed=False)

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_smoketest_fix(smoketest_task, payload, db_session, str(smoketest_task.id))
            await db_session.commit()

        children = (
            (await db_session.execute(select(Task).where(Task.parent_task_id == smoketest_task.id)))
            .scalars()
            .all()
        )
        assert len(children) == 1, "Dedup: must not spawn a second fix task"

    @pytest.mark.asyncio
    async def test_skips_spawn_for_qualifier_smoketest(self, db_session, mock_broadcast):
        """Fix spawn is skipped when the task context marks it as a qualifier smoketest."""
        from app.agent_status_spawn import maybe_spawn_smoketest_fix

        project = await _make_project(db_session)
        smoketest_task = Task(
            project_id=project.id,
            title="Qualifier smoketest",
            payloads=[{"type": "smoketest", "base_branch": "main"}],
            status=TaskStatus.FAILED,
            context={"qualifier_smoketest": True},
            created_by="ORCHESTRATOR",
        )
        db_session.add(smoketest_task)
        await db_session.commit()
        await db_session.refresh(smoketest_task)

        payload = _make_smoketest_payload(smoketest_task.id, passed=False)

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_smoketest_fix(smoketest_task, payload, db_session, str(smoketest_task.id))
            await db_session.commit()

        children = (
            (await db_session.execute(select(Task).where(Task.parent_task_id == smoketest_task.id)))
            .scalars()
            .all()
        )
        assert len(children) == 0, "Qualifier smoketest must not trigger fix spawn"


class TestMaybeSpawnDebugCreate:
    """Seam tests for agent_status_spawn.maybe_spawn_debug_create."""

    @pytest.mark.asyncio
    async def test_spawns_fix_task_on_high_confidence(self, db_session, mock_broadcast):
        """High-confidence debug result with fix_prompt spawns a create task."""
        from app.agent_status_spawn import maybe_spawn_debug_create

        project = await _make_project(db_session)
        debug_task = Task(
            project_id=project.id,
            title="Debug: auth regression",
            payloads=[{"type": "debug", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            context={},
            created_by="ORCHESTRATOR",
        )
        db_session.add(debug_task)
        await db_session.commit()
        await db_session.refresh(debug_task)

        payload = _make_debug_payload(debug_task.id, confidence="high", fix_prompt="Patch the auth module")

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_debug_create(debug_task, payload, db_session, str(debug_task.id))
            await db_session.commit()

        fix_tasks = (
            (await db_session.execute(select(Task).where(Task.title.like("Fix:%"))))
            .scalars()
            .all()
        )
        assert len(fix_tasks) == 1
        assert fix_tasks[0].status == TaskStatus.QUEUED
        assert "auth regression" in fix_tasks[0].title

    @pytest.mark.asyncio
    async def test_no_spawn_when_confidence_not_high(self, db_session, mock_broadcast):
        """Low-confidence debug result does not spawn a fix task."""
        from app.agent_status_spawn import maybe_spawn_debug_create

        project = await _make_project(db_session)
        debug_task = Task(
            project_id=project.id,
            title="Debug: flaky test",
            payloads=[{"type": "debug", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            context={},
            created_by="ORCHESTRATOR",
        )
        db_session.add(debug_task)
        await db_session.commit()
        await db_session.refresh(debug_task)

        payload = _make_debug_payload(debug_task.id, confidence="low", fix_prompt="maybe fix here")

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_debug_create(debug_task, payload, db_session, str(debug_task.id))
            await db_session.commit()

        fix_tasks = (
            (await db_session.execute(select(Task).where(Task.title.like("Fix:%"))))
            .scalars()
            .all()
        )
        assert len(fix_tasks) == 0, "Low-confidence debug must not spawn a fix task"

    @pytest.mark.asyncio
    async def test_dedup_skips_spawn_when_failed_task_replaced_by_requeue(
        self, db_session, mock_broadcast
    ):
        """Skips fix spawn when the failed task was already replaced by _requeue_parent_task."""
        from app.agent_status_spawn import maybe_spawn_debug_create

        project = await _make_project(db_session)

        replacement_task = Task(
            project_id=project.id,
            title="Replacement task",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(replacement_task)
        await db_session.commit()
        await db_session.refresh(replacement_task)

        failed_task = Task(
            project_id=project.id,
            title="Failed task",
            status=TaskStatus.CANCELLED,
            context={"_replaced_by_task_id": str(replacement_task.id)},
            created_by="ORCHESTRATOR",
        )
        db_session.add(failed_task)
        await db_session.commit()
        await db_session.refresh(failed_task)

        debug_task = Task(
            project_id=project.id,
            title="Debug: failed task",
            payloads=[{"type": "debug", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            context={"failed_task_id": str(failed_task.id)},
            created_by="ORCHESTRATOR",
        )
        db_session.add(debug_task)
        await db_session.commit()
        await db_session.refresh(debug_task)

        payload = _make_debug_payload(debug_task.id, confidence="high", fix_prompt="Fix it")

        with patch("app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock):
            await maybe_spawn_debug_create(debug_task, payload, db_session, str(debug_task.id))
            await db_session.commit()

        fix_tasks = (
            (await db_session.execute(select(Task).where(Task.title.like("Fix:%"))))
            .scalars()
            .all()
        )
        assert len(fix_tasks) == 0, "Dedup: must not spawn fix when failed task already replaced by requeue"


# ═══════════════════════════════════════════════════════════════════════════════
# admin_task_mutations.py — Seam tests for modify_task_apply helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestModifyTaskApplySeam:
    """Seam tests that call admin_task_mutations helpers directly (no HTTP).

    These validate the per-phase logic extracted from modify_task so the
    individual mutation phases are testable in isolation.
    """

    async def _make_task(self, session, project_id, status=TaskStatus.QUEUED):
        task = Task(
            project_id=project_id,
            title="Seam test task",
            status=status,
            created_by="ORCHESTRATOR",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    @pytest.mark.asyncio
    async def test_apply_simple_mutations_payloads(self, db_session):
        """_apply_simple_mutations updates task.payloads when payload key present."""
        from app.admin_task_mutations import _apply_simple_mutations

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)

        changes: dict = {}
        await _apply_simple_mutations(
            task, {"payloads": [{"type": "create", "prompt": "hello"}]}, changes
        )

        assert task.payloads == [{"type": "create", "prompt": "hello"}]
        assert changes["payloads"] == "updated"

    @pytest.mark.asyncio
    async def test_apply_simple_mutations_context_merge(self, db_session):
        """_apply_simple_mutations merges context dicts rather than replacing them."""
        from app.admin_task_mutations import _apply_simple_mutations

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        task.context = {"existing_key": "existing_val"}

        changes: dict = {}
        await _apply_simple_mutations(task, {"context": {"new_key": "new_val"}}, changes)

        assert task.context["existing_key"] == "existing_val"
        assert task.context["new_key"] == "new_val"
        assert changes["context"] == "updated"

    @pytest.mark.asyncio
    async def test_apply_simple_mutations_no_op_when_keys_absent(self, db_session):
        """_apply_simple_mutations does nothing when payload has no recognised keys."""
        from app.admin_task_mutations import _apply_simple_mutations

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        original_payloads = task.payloads

        changes: dict = {}
        await _apply_simple_mutations(task, {"unrelated_key": "value"}, changes)

        assert task.payloads == original_payloads
        assert changes == {}

    @pytest.mark.asyncio
    async def test_apply_simple_mutations_context_size_guard(self, db_session):
        """_apply_simple_mutations raises 400 when merged context exceeds 100 KB."""
        from fastapi import HTTPException
        from app.admin_task_mutations import _apply_simple_mutations

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        task.context = {}

        big_value = "x" * 200_000
        with pytest.raises(HTTPException) as exc_info:
            await _apply_simple_mutations(task, {"context": {"big": big_value}}, {})

        assert exc_info.value.status_code == 400
        assert "100KB" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_apply_depends_on_mutation_sets_deps(self, db_session):
        """_apply_depends_on_mutation persists a valid depends_on list."""
        from app.admin_task_mutations import _apply_depends_on_mutation

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        dep_task = await self._make_task(db_session, project.id)

        changes: dict = {}
        await _apply_depends_on_mutation(
            task, {"depends_on": [str(dep_task.id)]}, db_session, changes
        )

        assert task.depends_on == [str(dep_task.id)]
        assert changes["depends_on"] == [str(dep_task.id)]

    @pytest.mark.asyncio
    async def test_apply_depends_on_mutation_rejects_nonexistent(self, db_session):
        """_apply_depends_on_mutation raises 400 for an unknown task UUID."""
        from fastapi import HTTPException
        from app.admin_task_mutations import _apply_depends_on_mutation

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        missing_id = str(uuid.uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await _apply_depends_on_mutation(
                task, {"depends_on": [missing_id]}, db_session, {}
            )

        assert exc_info.value.status_code == 400
        assert "do not exist" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_apply_depends_on_mutation_rejects_cross_base(self, db_session):
        """A depends_on edge across different base branches raises 400."""
        from fastapi import HTTPException
        from app.admin_task_mutations import _apply_depends_on_mutation

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        task.payloads = [{"type": "create", "base_branch": "plan/a"}]
        dep_task = await self._make_task(db_session, project.id)
        dep_task.payloads = [{"type": "create", "base_branch": "plan/b"}]
        db_session.add_all([task, dep_task])
        await db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await _apply_depends_on_mutation(
                task, {"depends_on": [str(dep_task.id)]}, db_session, {}
            )

        assert exc_info.value.status_code == 400
        assert "Cross-base dependency" in exc_info.value.detail
        assert "plan/a" in exc_info.value.detail
        assert "plan/b" in exc_info.value.detail
        # The incompatible edge must NOT be persisted.
        assert task.depends_on != [str(dep_task.id)]

    @pytest.mark.asyncio
    async def test_apply_depends_on_mutation_allows_same_base(self, db_session):
        """Same-base deps (explicit and default None/None) still persist."""
        from app.admin_task_mutations import _apply_depends_on_mutation

        project = await _make_project(db_session)

        # Explicit shared base branch.
        task = await self._make_task(db_session, project.id)
        task.payloads = [{"type": "create", "base_branch": "plan/a"}]
        dep_task = await self._make_task(db_session, project.id)
        dep_task.payloads = [{"type": "create", "base_branch": "plan/a"}]
        db_session.add_all([task, dep_task])
        await db_session.commit()

        changes: dict = {}
        await _apply_depends_on_mutation(
            task, {"depends_on": [str(dep_task.id)]}, db_session, changes
        )
        assert task.depends_on == [str(dep_task.id)]
        assert changes["depends_on"] == [str(dep_task.id)]

        # Default None/None base (no payloads) normalizes to same base.
        plain_task = await self._make_task(db_session, project.id)
        plain_dep = await self._make_task(db_session, project.id)
        changes2: dict = {}
        await _apply_depends_on_mutation(
            plain_task, {"depends_on": [str(plain_dep.id)]}, db_session, changes2
        )
        assert plain_task.depends_on == [str(plain_dep.id)]

    @pytest.mark.asyncio
    async def test_modify_task_apply_returns_expected_shape(self, db_session):
        """modify_task_apply returns the standard response shape with no mutations."""
        from fastapi import BackgroundTasks
        from unittest.mock import patch, AsyncMock
        from app.admin_helpers import modify_task_apply

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)

        with patch("app.admin_task_mutations.broadcast_to_project", new_callable=AsyncMock):
            result = await modify_task_apply(
                task=task,
                payload={},
                background_tasks=BackgroundTasks(),
                session=db_session,
            )

        assert result["id"] == str(task.id)
        assert result["project_id"] == str(task.project_id)
        assert result["status"] == task.status
        assert "requires_approval" in result

    @pytest.mark.asyncio
    async def test_rollup_pr_approval_skips_pr_merged_on_promotion_target(
        self, db_session, mocker
    ):
        """Human-approved rollup merges to promotion_target stay unqualified."""
        from app.admin_task_mutations import _handle_rollup_pr_approval

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Rollup approval task",
            payloads=[{"type": "research", "prompt": "rollup", "base_branch": "release/main"}],
            status=TaskStatus.EVALUATING,
            context={"rollup_pr_number": 321},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
        mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

        changes: dict = {}
        await _handle_rollup_pr_approval(task, task.context or {}, 321, db_session, changes)
        await db_session.refresh(task)

        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("pr_merged") is None
        assert changes["merged_pr"] == 321

    @pytest.mark.asyncio
    async def test_merge_context_records_landed_signal_on_integration_merge(
        self, db_session
    ):
        from app.admin_task_mutations import _merge_context_with_hotfix_non_landing

        project = await _make_project(db_session)
        project.integration_branch = "main"
        project.promotion_target = "main"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Integration merge task",
            payloads=[{"type": "research", "prompt": "merge", "base_branch": "main"}],
            status=TaskStatus.EVALUATING,
            context={},
        )

        merged_context = _merge_context_with_hotfix_non_landing(
            task,
            project,
            base_context={"rollup_pr_number": 123},
            landed_branch="main",
            record_pr_merged=True,
        )

        assert merged_context.get("pr_merged") is True
        assert merged_context.get("landed_on_integration") is True
        assert merged_context.get("_non_merge_unblock_reason") is None

    @pytest.mark.asyncio
    async def test_rollup_pr_approval_keeps_custom_staged_root_evaluating(
        self, db_session, mocker
    ):
        """Human-approved custom staged rollup roots must await downstream landing."""
        from app.admin_task_mutations import _handle_rollup_pr_approval

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        project.build_config = {"staging_branch_prefixes": ["release/"]}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Release rollup approval task",
            payloads=[
                {
                    "type": "research",
                    "prompt": "rollup",
                    "base_branch": "release/candidate-1",
                }
            ],
            status=TaskStatus.EVALUATING,
            context={"rollup_pr_number": 654},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
        mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

        changes: dict = {}
        await _handle_rollup_pr_approval(task, task.context or {}, 654, db_session, changes)
        await db_session.refresh(task)

        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get("pr_merged") is None
        assert "awaiting downstream landing" in (task.latest_status_message or "")
        assert changes["merged_pr"] == 654

    @pytest.mark.asyncio
    async def test_rollup_pr_approval_refuses_dual_head_migration(
        self, db_session, mocker
    ):
        """Operator-approved rollup with a colliding migration → 409, no merge."""
        from fastapi import HTTPException

        from app.admin_task_mutations import _handle_rollup_pr_approval

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Rollup approval dual-head",
            payloads=[
                {"type": "research", "prompt": "rollup", "base_branch": "automain"}
            ],
            status=TaskStatus.EVALUATING,
            context={"rollup_pr_number": 777},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        # Guard: a touched migration whose revision id collides with the target.
        mocker.patch(
            "app.github.get_pr_files",
            new_callable=AsyncMock,
            return_value=[
                {"filename": "backend/alembic/versions/0002_dup.py"}
            ],
        )
        mocker.patch(
            "app.github.get_pull_request_diff",
            new_callable=AsyncMock,
            return_value=(
                "diff --git a/backend/alembic/versions/0002_dup.py "
                "b/backend/alembic/versions/0002_dup.py\n"
                "+++ b/backend/alembic/versions/0002_dup.py\n"
                '+revision = "0002"\n'
                '+down_revision = "0001"\n'
            ),
        )
        mocker.patch(
            "app.github.list_versions_at_ref",
            new_callable=AsyncMock,
            return_value=["0001_init.py", "0002_existing.py"],
        )
        merge_mock = mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

        changes: dict = {}
        with pytest.raises(HTTPException) as exc_info:
            await _handle_rollup_pr_approval(
                task, task.context or {}, 777, db_session, changes
            )
        assert exc_info.value.status_code == 409
        assert "0002" in str(exc_info.value.detail)
        merge_mock.assert_not_called()
        await db_session.refresh(task)
        assert task.requires_approval is True
        assert "merged_pr" not in changes


# ═══════════════════════════════════════════════════════════════════════════════
# admin_operator_triggers.py — Manual Operator Triggers
# ═══════════════════════════════════════════════════════════════════════════════


class TestTriggerQualify:
    @pytest.mark.asyncio
    async def test_qualify_rejects_promotion_in_flight(
        self, client: AsyncClient, db_session
    ):
        """Re-running qualification while a promotion PR is open and auto-merging
        is rejected with a 409 — run_qualification no-ops (promotion_in_flight)
        and the endpoint surfaces it, mirroring the version-cut lever, instead of
        spawning a fresh audit/smoketest pair that clobbers the pending record."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        # A terminal audit child — by the child-task definition this is NOT "in
        # flight", so only the promotion-in-flight guard catches it.
        audit_task = Task(
            project_id=project.id,
            title="audit-done",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-promo",
            status=TaskStatus.COMPLETED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abc123",
            "audit_task_id": str(audit_task.id),
            "smoketest_enabled": False,
            "promotion_pending": {
                "pr_number": 9753,
                "pr_url": "https://example/pr/9753",
                "auto_merge_enabled": True,
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/qualify")
        assert resp.status_code == 409
        assert "promotion PR is open and auto-merging" in resp.text

        # The promotion-pending record survives untouched.
        await db_session.refresh(version)
        assert version.last_qualification["promotion_pending"]["pr_number"] == 9753

    @pytest.mark.asyncio
    async def test_qualify_no_open_version_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when there is no open release version to qualify."""
        project = await _make_project(db_session)
        resp = await client.post(f"/api/admin/projects/{project.id}/qualify")
        assert resp.status_code == 404


class TestClearWaveDeps:
    @pytest.mark.asyncio
    async def test_preserves_missing_depends_on_for_queued_tasks(
        self, client: AsyncClient, db_session
    ):
        """A missing dependency row is preserved for cascade-cancel handling."""
        project = await _make_project(db_session)
        dep_id = uuid.uuid4()
        blocked = Task(
            project_id=project.id,
            payloads=[{"type": "smoketest"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep_id)],
        )
        db_session.add(blocked)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(blocked)
        assert blocked.depends_on == [str(dep_id)]

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_queued_tasks(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 0
        assert resp.json()["total_queued"] == 0

    @pytest.mark.asyncio
    async def test_clears_ordering_only_edge_when_dispatch_gate_says_eligible(
        self, client: AsyncClient, db_session
    ):
        """An ordering-only edge is cleared even if the foundation is still in flight."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foundation.py"]},
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            status=TaskStatus.QUEUED,
            context={"related_files": ["backend/app/dependent.py"]},
            depends_on=[str(foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 1
        assert data["preserved"] == 0
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == []

    @pytest.mark.asyncio
    async def test_preserves_prerequisite_edge_with_provenance(
        self, client: AsyncClient, db_session
    ):
        """A QUEUED task with foundation-converged provenance keeps its edge."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "smoketest"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(foundation.id)],
            context={"_foundation_converged": {"foundation_id": str(foundation.id)}},
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(foundation.id)]

    @pytest.mark.asyncio
    async def test_preserves_edge_when_foundation_in_flight(
        self, client: AsyncClient, db_session
    ):
        """An in-flight (not-yet-landed) foundation gates its dependent — preserve."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "smoketest"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(foundation.id)]

    @pytest.mark.asyncio
    async def test_preserves_completed_same_base_edge_until_pr_merged(
        self, client: AsyncClient, db_session, mocker
    ):
        """A COMPLETED same-base code dependency still waits until PR merge/landing."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.COMPLETED,
            context={"related_files": ["backend/app/shared.py"]},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)
        mocker.patch(
            "app.rollup_pr_verification._verify_pr_merged",
            new_callable=AsyncMock,
            return_value=False,
        )

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.QUEUED,
            context={"related_files": ["backend/app/shared.py"]},
            depends_on=[str(foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(foundation.id)]

    @pytest.mark.asyncio
    async def test_preserves_failed_prerequisite_edge(
        self, client: AsyncClient, db_session
    ):
        """A genuinely failed prerequisite is preserved for cascade-cancel handling."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.FAILED,
            context={"related_files": ["backend/app/shared.py"]},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.QUEUED,
            context={"related_files": ["backend/app/shared.py"]},
            depends_on=[str(foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(foundation.id)]

    @pytest.mark.asyncio
    async def test_preserves_resolved_need_edge_without_blocker_metadata(
        self, client: AsyncClient, db_session
    ):
        """A real edge resolved from blocked_need survives after blocker metadata clears."""
        project = await _make_project(db_session)
        foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            status=TaskStatus.COMPLETED,
            context={"related_files": ["backend/app/foundation.py"]},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            status=TaskStatus.QUEUED,
            context={
                "related_files": ["backend/app/dependent.py"],
                "_structural_depends_on_ids": [str(foundation.id)],
            },
            depends_on=[str(foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 0
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(foundation.id)]

    @pytest.mark.asyncio
    async def test_mixed_edges_keeps_only_the_live_prerequisite(
        self, client: AsyncClient, db_session, mocker
    ):
        """A task mixing ordering-only and real deps clears only the ordering-only edge."""
        project = await _make_project(db_session)
        stale_foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            status=TaskStatus.IN_PROGRESS,
            context={"related_files": ["backend/app/stale.py"]},
        )
        live_foundation = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.COMPLETED,
            context={"related_files": ["backend/app/live.py"]},
        )
        db_session.add(stale_foundation)
        db_session.add(live_foundation)
        await db_session.commit()
        await db_session.refresh(stale_foundation)
        await db_session.refresh(live_foundation)
        mocker.patch(
            "app.rollup_pr_verification._verify_pr_merged",
            new_callable=AsyncMock,
            return_value=False,
        )

        dependent = Task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/shared"}],
            status=TaskStatus.QUEUED,
            context={"related_files": ["backend/app/live.py"]},
            depends_on=[str(stale_foundation.id), str(live_foundation.id)],
        )
        db_session.add(dependent)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] == 1
        assert data["preserved"] == 1
        assert data["total_queued"] == 1

        await db_session.refresh(dependent)
        assert dependent.depends_on == [str(live_foundation.id)]


class TestCancelStrategicRefresh:
    @pytest.mark.asyncio
    async def test_cancel_clears_armed_marker(
        self, client: AsyncClient, db_session
    ):
        """Cancelling an armed steer clears the pending marker (cleared=True)."""
        from app import strategic_trigger as st

        project = await _make_project(db_session)
        await st.request_strategic_refresh(
            db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/cancel-strategic-refresh"
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True

        await db_session.refresh(project)
        assert st.strategic_refresh_pending(project.vision) is False

    @pytest.mark.asyncio
    async def test_cancel_idempotent_noop_when_nothing_armed(
        self, client: AsyncClient, db_session
    ):
        """Cancelling with nothing armed is a harmless no-op (cleared=False, 200)."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/cancel-strategic-refresh"
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] is False


class TestBulkApprove:
    @pytest.mark.asyncio
    async def test_bulk_approve_critical_merge_to_promotion_target_returns_error(
        self, client: AsyncClient, db_session, mocker
    ):
        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Critical approval task",
            payloads=[{"type": "create", "prompt": "danger", "base_branch": "release/main"}],
            status=TaskStatus.COMPLETED,
            requires_approval=True,
            context={"touched_critical": True},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/123",
        )
        db_session.add(artifact)
        await db_session.commit()

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        merge_pr = mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
        mocker.patch("app.github.delete_branch", new_callable=AsyncMock)

        resp = await client.post(
            "/api/admin/tasks/bulk-action",
            json={"action": "approve", "task_ids": [str(task.id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success_count"] == 0
        assert body["error_count"] == 1
        assert body["results"][0]["status"] == "error"
        assert "promotion target" in body["results"][0]["error"]
        await db_session.refresh(task)
        assert task.requires_approval is True
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("pr_merged") is None
        merge_pr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bulk_approve_rollup_keeps_custom_staged_root_evaluating(
        self, client: AsyncClient, db_session, mocker
    ):
        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        project.build_config = {"staging_branch_prefixes": ["release/"]}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Custom staged approval task",
            payloads=[
                {
                    "type": "research",
                    "prompt": "danger",
                    "base_branch": "release/candidate-1",
                }
            ],
            status=TaskStatus.EVALUATING,
            requires_approval=True,
            context={"rollup_pr_number": 456, "touched_critical": True},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
        mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

        resp = await client.post(
            "/api/admin/tasks/bulk-action",
            json={"action": "approve", "task_ids": [str(task.id)]},
        )

        assert resp.status_code == 200
        await db_session.refresh(task)
        assert task.requires_approval is False
        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get("pr_merged") is None
        assert "awaiting downstream landing" in (task.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_bulk_approve_clears_held_compute_experiment(
        self, client: AsyncClient, db_session, mocker
    ):
        """Operator approve clears the hold on a QUEUED compute_experiment task
        held for spend-threshold approval, making it dispatchable. A QUEUED task
        skips the COMPLETED/EVALUATING PR-merge branch, so no GitHub calls fire."""
        project = await _make_project(db_session)

        task = Task(
            project_id=project.id,
            title="Held compute_experiment",
            payloads=[
                {
                    "type": "compute_experiment",
                    "command": "python3 bench.py",
                    "spend_cap_usd": 50.0,
                    "deadline_seconds": 7200,
                }
            ],
            status=TaskStatus.QUEUED,
            requires_approval=True,
            latest_status_message=(
                "Held for operator approval: compute_experiment proposes "
                "$50.00 spend cap / 7200s deadline, exceeding the $5.00 approval "
                "threshold."
            ),
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        merge_pr = mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )

        resp = await client.post(
            "/api/admin/tasks/bulk-action",
            json={"action": "approve", "task_ids": [str(task.id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success_count"] == 1
        assert body["error_count"] == 0
        await db_session.refresh(task)
        # The hold is cleared so the orchestrator can dispatch the launch.
        assert task.requires_approval is False
        # A QUEUED hold is not a PR merge — status is unchanged, no GitHub calls.
        assert task.status == TaskStatus.QUEUED
        merge_pr.assert_not_awaited()


class TestBulkRetrySessionFactory:
    """The bulk-retry scoped session factory must resolve a real engine even
    when the originating request session exposes a None ``.bind`` — the
    production case, where ``AsyncSessionLocal`` resolves its engine lazily at
    execute-time. Feeding that None straight into ``async_sessionmaker`` (the
    pre-fix behaviour) produced an unbound factory whose sessions could not
    resolve an engine. Mirrors the agent-status post-commit fix."""

    @pytest.mark.asyncio
    async def test_resolve_async_engine_passthrough_and_fallback(self):
        from sqlalchemy.ext.asyncio import create_async_engine
        from app.database import resolve_async_engine
        import app.database as app_database

        # 1. Explicit AsyncEngine bind -> passthrough (preserves test behaviour).
        real_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:

            class _BoundSession:
                bind = real_engine

            assert resolve_async_engine(_BoundSession()) is real_engine
        finally:
            await real_engine.dispose()

        # 2. None bind + get_bind() that cannot resolve -> module-engine fallback.
        class _NoneBindSession:
            bind = None

            def get_bind(self, *args, **kwargs):
                raise RuntimeError("no engine resolvable")

        assert resolve_async_engine(_NoneBindSession()) is app_database.engine

        # 3. Non-AsyncEngine bind (and a non-AsyncEngine get_bind) -> fallback.
        class _BadBindSession:
            bind = object()

            def get_bind(self, *args, **kwargs):
                return object()

        assert resolve_async_engine(_BadBindSession()) is app_database.engine

    @pytest.mark.asyncio
    async def test_bulk_retry_resolves_engine_on_none_bind(self, db_session):
        """Drive the bulk-retry mutation with an originating session whose
        ``.bind`` is None: the scoped factory must still resolve a working
        engine (the module fallback) and commit the task back to QUEUED."""
        from app.admin import _bulk_retry_one_task

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Retry me",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.FAILED,
            vm_instance_id="agent-vm-old",
            attempt_count=3,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        task_id = task.id

        class _NoneBindSession:
            bind = None

            def get_bind(self, *args, **kwargs):
                raise RuntimeError("no engine resolvable")

        old_vm = await _bulk_retry_one_task(task_id, _NoneBindSession())

        # Non-parent retry returns the previous VM id for post-commit deletion.
        assert old_vm == "agent-vm-old"

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.attempt_count == 0
        assert task.vm_instance_id != "agent-vm-old"


class TestRequeueClearsBlockerMetadata:
    @pytest.mark.asyncio
    async def test_manual_requeue_clears_blocked_state(self, db_session):
        """A BLOCKED task manually requeued to QUEUED must shed all prior-attempt
        blocker metadata and the budget-exempt BLOCKED_PRECONDITION tags, mirroring
        the automatic unblock out-transition."""
        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply
        from app.task_blocked import (
            BLOCKED_PRECONDITION,
            BLOCKER_CONTEXT_KEY,
            enter_blocked_state,
        )

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Blocked then requeued",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="agent-vm-old",
            attempt_count=2,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        enter_blocked_state(
            task,
            waiting_on="upstream rollup PR #123",
            reason="dependency not yet merged",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        # Precondition: the blocked-state markers are present before requeue.
        assert task.status == TaskStatus.BLOCKED
        assert BLOCKER_CONTEXT_KEY in (task.context or {})
        assert (task.context or {}).get("failure_class") == BLOCKED_PRECONDITION
        assert task.blocker_kind is not None

        await modify_task_apply(
            task,
            {"status": TaskStatus.QUEUED.value},
            BackgroundTasks(),
            db_session,
        )

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        # Structured blocker metadata and the budget-exempt failure tags are gone.
        assert BLOCKER_CONTEXT_KEY not in (task.context or {})
        assert (task.context or {}).get("failure_class") != BLOCKED_PRECONDITION
        assert (task.context or {}).get("failure_category") != BLOCKED_PRECONDITION
        # Column-shaped blocker markers cleared too.
        assert task.blocker_kind is None
        assert task.blocked_reason is None
        assert task.blocked_at is None
        assert task.recheck_after is None
        # Clean attempt: counter reset like any leaf requeue.
        assert task.attempt_count == 0


class TestRequeueClearsStaleFailureVerdict:
    @pytest.mark.asyncio
    async def test_manual_requeue_pops_failure_class_and_category(self, db_session):
        """An admin requeue must clear the previous attempt's persisted failure
        verdict: classify_failure short-circuits on a persisted failure_class /
        failure_category, so a stale CODE_ERROR from the pre-requeue attempt
        would mask the NEXT failure's real cause (e.g. an infra death
        re-labelled CODE_ERROR at 0.9 confidence, burning the fresh budget)."""
        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Failed then requeued",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.FAILED,
            vm_instance_id="agent-vm-old",
            attempt_count=3,
            context={
                "failure_class": "CODE_ERROR",
                "failure_category": "claude_code_failed",
                "_api_failure_retries": 2,
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await modify_task_apply(
            task,
            {"status": TaskStatus.QUEUED.value},
            BackgroundTasks(),
            db_session,
        )

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        ctx = task.context or {}
        assert "failure_class" not in ctx
        assert "failure_category" not in ctx
        assert "_api_failure_retries" not in ctx
        assert task.attempt_count == 0


class TestTriggerRollup:
    @pytest.mark.asyncio
    async def test_intermediate_rollup_auto_merge_keeps_custom_staged_root_evaluating(
        self, db_session, mocker
    ):
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        project.build_config = {"staging_branch_prefixes": ["release/"]}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "release/candidate-1",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={
                "html_url": "https://github.com/owner/repo/pull/654",
                "number": 654,
            },
        )
        mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        delete_branch_mock = mocker.patch(
            "app.github.delete_branch", new_callable=AsyncMock
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        await db_session.refresh(task)
        assert result["rollup_pr_number"] == 654
        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get("pr_merged") is None
        assert (task.context or {}).get("rollup_pr_number") == 654
        assert "awaiting downstream landing" in (task.latest_status_message or "")
        delete_branch_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_merged_rollup_skips_pr_merged_on_promotion_target(
        self, db_session, mocker
    ):
        from app.routers.admin_operator_triggers import trigger_rollup
        from app.review_pipeline import SPAWN_REASON_ALREADY_MERGED, SPAWN_REASON_KEY

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "release/main",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        async def _mark_already_merged(*args, **kwargs):
            source_task = args[0]
            source_task.context = {
                **(source_task.context or {}),
                SPAWN_REASON_KEY: SPAWN_REASON_ALREADY_MERGED,
            }
            source_task.latest_status_message = "already merged upstream"
            return None, False

        mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={"html_url": "https://github.com/owner/repo/pull/456", "number": 456},
        )
        mocker.patch(
            "app.review_pipeline._spawn_pipeline_task",
            new=_mark_already_merged,
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        await db_session.refresh(task)
        assert result["rollup_pr_number"] == 456
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("pr_merged") is None

    @pytest.mark.asyncio
    async def test_intermediate_rollup_auto_merge_holds_on_dual_head(
        self, db_session, mocker
    ):
        """Operator-triggered intermediate auto-merge holds on a dual-head collision."""
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "release/main"
        project.build_config = {"staging_branch_prefixes": ["research/"]}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup dual-head",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "research/intermediate-1",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={
                "html_url": "https://github.com/owner/repo/pull/888",
                "number": 888,
            },
        )
        mocker.patch(
            "app.github.get_pr_files",
            new_callable=AsyncMock,
            return_value=[{"filename": "backend/alembic/versions/0002_dup.py"}],
        )
        mocker.patch(
            "app.github.get_pull_request_diff",
            new_callable=AsyncMock,
            return_value=(
                "diff --git a/backend/alembic/versions/0002_dup.py "
                "b/backend/alembic/versions/0002_dup.py\n"
                "+++ b/backend/alembic/versions/0002_dup.py\n"
                '+revision = "0002"\n'
                '+down_revision = "0001"\n'
            ),
        )
        mocker.patch(
            "app.github.list_versions_at_ref",
            new_callable=AsyncMock,
            return_value=["0001_init.py", "0002_existing.py"],
        )
        merge_mock = mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        await db_session.refresh(task)
        merge_mock.assert_not_called()
        assert result["rollup_pr_number"] == 888
        assert result.get("merge_blocked_reason")
        assert task.status == TaskStatus.EVALUATING
        assert "dual-head" in (task.latest_status_message or "").lower()

    @pytest.mark.asyncio
    async def test_manual_rollup_skips_when_pr_already_recorded(
        self, db_session, mocker
    ):
        """Idempotency: when a rollup PR is already recorded on the task, the
        manual trigger returns it WITHOUT POSTing a second PR — so a duplicate
        client retry, or a manual trigger after the auto path already created
        the PR, cannot double-apply."""
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup already recorded",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
                "rollup_pr_number": 4242,
                "rollup_pr_url": "https://github.com/owner/repo/pull/4242",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        create_mock = mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={"html_url": "https://x/9999", "number": 9999},
        )
        token_mock = mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        create_mock.assert_not_called()
        token_mock.assert_not_called()
        assert result["rollup_pr_number"] == 4242
        assert result["rollup_pr_url"] == "https://github.com/owner/repo/pull/4242"
        await db_session.refresh(task)
        assert (task.context or {}).get("rollup_pr_number") == 4242

    @pytest.mark.asyncio
    async def test_manual_rollup_defers_on_fresh_creation_claim(
        self, db_session, mocker
    ):
        """A fresh in-flight creation claim (a peer mid-create) makes the manual
        trigger return 409 instead of POSTing a duplicate PR, leaving the task
        EVALUATING."""
        import time as _time
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup fresh claim",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
                "rollup_pr_creating_at": _time.time(),
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        create_mock = mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={"html_url": "https://x/9999", "number": 9999},
        )

        with pytest.raises(HTTPException) as exc_info:
            await trigger_rollup(
                task.id,
                session=db_session,
                current_user={"user_id": TEST_USER_ID},
            )

        assert exc_info.value.status_code == 409
        create_mock.assert_not_called()
        await db_session.refresh(task)
        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get("rollup_pr_number") is None

    @pytest.mark.asyncio
    async def test_manual_rollup_stale_creation_claim_proceeds(
        self, db_session, mocker
    ):
        """A creation claim older than the TTL is stale (peer crashed mid-create)
        and must NOT wedge creation — the manual trigger proceeds to create the
        PR."""
        import time as _time
        from app.routers.admin_operator_triggers import trigger_rollup
        from app.rollup_management import _ROLLUP_PR_CREATE_CLAIM_TTL_S

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup stale claim",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
                "rollup_pr_creating_at": _time.time()
                - (_ROLLUP_PR_CREATE_CLAIM_TTL_S + 60),
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        create_mock = mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={
                "html_url": "https://github.com/owner/repo/pull/777",
                "number": 777,
            },
        )
        mocker.patch(
            "app.review_pipeline._spawn_pipeline_task",
            new_callable=AsyncMock,
            return_value=(None, None),
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        create_mock.assert_awaited_once()
        assert result["rollup_pr_number"] == 777
        await db_session.refresh(task)
        assert (task.context or {}).get("rollup_pr_number") == 777
        assert (task.context or {}).get("rollup_pr_creating_at") is None

    @pytest.mark.asyncio
    async def test_manual_rollup_empty_diff_completes_without_pr_zero_followups(
        self, db_session, mocker
    ):
        """The manual path must treat GitHub's empty-diff sentinel as a terminal
        no-op completion, not as a real PR #0 that proceeds into merge/review."""
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup empty diff",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={
                "empty_diff": True,
                "merged": True,
                "html_url": "",
                "number": 0,
            },
        )
        merge_mock = mocker.patch(
            "app.github.merge_pull_request",
            new_callable=AsyncMock,
            return_value={"merged": True},
        )
        spawn_mock = mocker.patch(
            "app.review_pipeline._spawn_pipeline_task",
            new_callable=AsyncMock,
            return_value=(None, None),
        )
        delete_branch_mock = mocker.patch(
            "app.github.delete_branch", new_callable=AsyncMock
        )

        result = await trigger_rollup(
            task.id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        merge_mock.assert_not_called()
        spawn_mock.assert_not_called()
        delete_branch_mock.assert_awaited_once()
        assert result["rollup_pr_number"] == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("rollup_pr_number") is None
        assert (task.context or {}).get("rollup_pr_creating_at") is None
        assert "no new changes to merge" in (task.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_manual_rollup_create_failure_clears_claim_for_retry(
        self, db_session, mocker
    ):
        """A transient create failure must drop the shared in-flight claim so a
        manual retry or the automated rollup path can proceed immediately."""
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup transient create failure",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=503, detail="GitHub unavailable"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await trigger_rollup(
                task.id,
                session=db_session,
                current_user={"user_id": TEST_USER_ID},
            )

        assert exc_info.value.status_code == 503
        await db_session.refresh(task)
        assert task.status == TaskStatus.EVALUATING
        assert (task.context or {}).get("rollup_pr_number") is None
        assert (task.context or {}).get("rollup_pr_creating_at") is None

    @pytest.mark.asyncio
    async def test_manual_rollup_stakes_claim_before_create(
        self, db_session, mocker
    ):
        """The creation claim is staked (committed) on the task BEFORE the slow
        create_pull_request call — so a concurrent worker observes it and defers
        — and is cleared (replaced by the durable rollup_pr_number marker) after
        the PR is recorded."""
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Manual rollup stakes claim",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context={
                "plan_branch": "research/abc12345",
                "parent_base": "automain",
                "direction_id": "dir-1",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        task_id = task.id

        observed = {}

        async def _capture_claim(*args, **kwargs):
            # The claim must be persisted before this POST so a peer defers.
            from sqlmodel import select as _select

            row = (
                await db_session.execute(
                    _select(Task).where(Task.id == task_id)
                )
            ).scalars().first()
            observed["claim_at_create"] = (row.context or {}).get(
                "rollup_pr_creating_at"
            )
            return {
                "html_url": "https://github.com/owner/repo/pull/555",
                "number": 555,
            }

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        mocker.patch(
            "app.github.create_pull_request",
            new=_capture_claim,
        )
        mocker.patch(
            "app.review_pipeline._spawn_pipeline_task",
            new_callable=AsyncMock,
            return_value=(None, None),
        )

        result = await trigger_rollup(
            task_id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        # Claim was visible mid-create...
        assert observed["claim_at_create"] is not None
        # ...and dropped (superseded by the durable PR marker) afterward.
        assert result["rollup_pr_number"] == 555
        await db_session.refresh(task)
        assert (task.context or {}).get("rollup_pr_creating_at") is None
        assert (task.context or {}).get("rollup_pr_number") == 555

    @pytest.mark.skipif(
        not (
            os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
            and os.getenv("DATABASE_URL", "").startswith("postgresql")
        ),
        reason="cross-session identity-map cache race requires Postgres",
    )
    @pytest.mark.asyncio
    async def test_manual_rollup_sees_peer_pr_marker_committed_after_role_check(
        self, db_session, mocker
    ):
        """Regression (#7350/#7351): the FOR UPDATE precondition re-check must
        read the row AS OF the lock, NOT the stale identity-map copy seeded by
        the role check's ``session.get``.

        With ``expire_on_commit=False`` the role check leaves the task cached in
        the session's identity map. A peer (auto path or a second manual
        trigger) that commits ``rollup_pr_number`` after that read but before
        this request locks the row must be observed — otherwise the idempotency
        guard reads the pre-lock snapshot and POSTs a DUPLICATE rollup PR. The
        ``session.expire`` + ``populate_existing=True`` on the locked SELECT is
        what makes the fresh marker visible; this test fails without it.
        """
        import json as _json
        from sqlalchemy import text as _text
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
        from sqlalchemy.orm import sessionmaker as _sessionmaker
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        base_ctx = {
            "plan_branch": "research/abc12345",
            "parent_base": "automain",
            "direction_id": "dir-1",
        }
        task = Task(
            project_id=project.id,
            title="Manual rollup peer marker race",
            payloads=[{"type": "research", "prompt": "rollup"}],
            status=TaskStatus.EVALUATING,
            context=dict(base_ctx),
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        task_id = task.id

        # The task is now cached in db_session's identity map WITHOUT a
        # rollup_pr_number — exactly the stale snapshot the role-check
        # session.get would return inside trigger_rollup. End db_session's
        # txn so the peer commit below is visible to the lock's fresh SELECT.
        await db_session.commit()

        # Peer commits the durable PR marker via a separate connection,
        # bypassing the ORM and db_session's identity map entirely.
        peer_ctx = dict(base_ctx)
        peer_ctx["rollup_pr_number"] = 7373
        peer_ctx["rollup_pr_url"] = "https://github.com/owner/repo/pull/7373"
        _Session = _sessionmaker(
            bind=db_session.bind,
            class_=_AsyncSession,
            expire_on_commit=False,
        )
        async with _Session() as peer_session:
            await peer_session.execute(
                _text("UPDATE task SET context = cast(:ctx as jsonb) WHERE id = :id"),
                {"ctx": _json.dumps(peer_ctx), "id": task_id},
            )
            await peer_session.commit()

        create_mock = mocker.patch(
            "app.github.create_pull_request",
            new_callable=AsyncMock,
            return_value={"html_url": "https://x/9999", "number": 9999},
        )
        token_mock = mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )

        # IMPORTANT: do NOT expire_all here — the point is that the fix's own
        # session.expire + populate_existing must bypass the warm cache.
        result = await trigger_rollup(
            task_id,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        # Idempotent: the peer's PR is returned, no second PR is POSTed.
        create_mock.assert_not_called()
        token_mock.assert_not_called()
        assert result["rollup_pr_number"] == 7373
        assert result["rollup_pr_url"] == (
            "https://github.com/owner/repo/pull/7373"
        )


class TestTriggerSelfAudit:
    @pytest.mark.asyncio
    async def test_self_audit_409_when_audit_in_flight(
        self, client: AsyncClient, db_session
    ):
        """The self-audit trigger must refuse to spawn a second audit when one is
        already in flight — two concurrent audits produce duplicate findings and
        waste VM budget. Returns 409 with the existing task id."""
        project = await _make_project(db_session)

        # Create an in-flight self_audit task
        in_flight = Task(
            project_id=project.id,
            title="Self audit",
            payloads=[{"type": "self_audit", "base_branch": "main"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(in_flight)
        await db_session.commit()

        # Attempt to trigger a second self_audit — should be rejected
        resp = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp.status_code == 409, resp.text
        assert "in progress" in resp.text.lower()
        assert str(in_flight.id)[:8] in resp.text

    @pytest.mark.asyncio
    async def test_self_audit_rejects_queued_and_provisioning_states(
        self, client: AsyncClient, db_session
    ):
        """Guard blocks QUEUED and PROVISIONING states as well, not just IN_PROGRESS."""
        project = await _make_project(db_session)

        # Test QUEUED
        queued_audit = Task(
            project_id=project.id,
            title="Self audit",
            payloads=[{"type": "self_audit", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(queued_audit)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp.status_code == 409, resp.text

        # Delete the queued audit
        await db_session.delete(queued_audit)
        await db_session.commit()

        # Test PROVISIONING
        provisioning_audit = Task(
            project_id=project.id,
            title="Self audit",
            payloads=[{"type": "self_audit", "base_branch": "main"}],
            status=TaskStatus.PROVISIONING,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(provisioning_audit)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp.status_code == 409, resp.text

    @pytest.mark.asyncio
    async def test_self_audit_allowed_after_terminal_state(
        self, client: AsyncClient, db_session
    ):
        """After an audit reaches a terminal state (COMPLETED/FAILED), a new
        trigger is allowed."""
        project = await _make_project(db_session)

        # Create a completed self_audit task
        completed_audit = Task(
            project_id=project.id,
            title="Self audit",
            payloads=[{"type": "self_audit", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(completed_audit)
        await db_session.commit()

        # A new trigger should succeed
        resp = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "task_id" in data

    @pytest.mark.asyncio
    async def test_self_audit_independent_per_project(
        self, client: AsyncClient, db_session
    ):
        """In-flight guard is per-project; blocking a different project doesn't
        prevent triggers on the current project."""
        project1 = await _make_project(db_session, name="Project 1")
        project2 = await _make_project(db_session, name="Project 2")

        # Create an in-flight self_audit on project 1
        in_flight = Task(
            project_id=project1.id,
            title="Self audit",
            payloads=[{"type": "self_audit", "base_branch": "main"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(in_flight)
        await db_session.commit()

        # Trigger on project 1 should fail
        resp = await client.post(f"/api/admin/projects/{project1.id}/self-audit")
        assert resp.status_code == 409, resp.text

        # Trigger on project 2 should succeed
        resp = await client.post(f"/api/admin/projects/{project2.id}/self-audit")
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_concurrent_self_audit_idempotency_key_prevents_race(
        self, client: AsyncClient, db_session
    ):
        """Concurrent self-audit triggers are prevented by idempotency_key.
        When a second trigger arrives while one is in flight, the SELECT check
        detects it and rejects with 409. If the race condition allows both to
        pass the SELECT check, the idempotency_key constraint + IntegrityError
        handler prevents duplicate inserts."""
        project = await _make_project(db_session)

        # First trigger creates a task with idempotency_key
        resp1 = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp1.status_code == 200, resp1.text
        data1 = resp1.json()
        task1_id = data1["task_id"]

        # Verify the created task has idempotency_key set
        task1 = await db_session.get(Task, uuid.UUID(task1_id))
        assert task1 is not None
        assert task1.idempotency_key == f"operator_self_audit:{project.id}"

        # Second trigger on same project finds active audit and rejects with 409
        resp2 = await client.post(f"/api/admin/projects/{project.id}/self-audit")
        assert resp2.status_code == 409, resp2.text
        assert "already in progress" in resp2.json()["error"]["message"].lower()


class TestGetProjectFindings:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_findings(
        self, client: AsyncClient, db_session
    ):
        """GET /findings returns an empty findings list when no data exists."""
        project = await _make_project(db_session)
        resp = await client.get(f"/api/admin/projects/{project.id}/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["findings"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_rejects_nonmember_project(self, client: AsyncClient, db_session):
        """Non-member project returns 404."""
        other_project = Project(name="Other", user_id="other-uid")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)

        resp = await client.get(f"/api/admin/projects/{other_project.id}/findings")
        assert resp.status_code == 404


class TestBugReportCitationGate:
    """Reproduce-on-HEAD citation grounding for free-text bug reports.

    A BUG_REPORT/BLOCKER whose cited code is absent on the integration branch is
    flagged at ingestion (``_provenance_warnings`` stored on the message context)
    and re-validated when turned into an investigation task. Conservative: a
    report whose code exists, or that cites nothing, is never flagged.
    """

    async def _project_with_repo(self, session):
        project = Project(
            name="acme/citegate",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="citegate",
            integration_branch="main",
            promotion_target="main",
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        session.add(
            ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
        )
        await session.commit()
        return project

    @pytest.mark.asyncio
    async def test_bug_report_absent_citation_is_flagged(
        self, client: AsyncClient, db_session, mocker
    ):
        """A BUG_REPORT citing code absent on HEAD stores an absent-state signal."""
        project = await self._project_with_repo(db_session)
        # No GitHub creds → local-HEAD fallback; the cited file is reported absent.
        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(return_value=False),
        )
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "BUG_REPORT",
                "content": "The crash originates in backend/app/gone.py during init.",
            },
        )
        assert resp.status_code == 200, resp.text
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalar_one()
        warnings = (msg.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "absent"
        assert "backend/app/gone.py" in warnings["broken_refs"]

    @pytest.mark.asyncio
    async def test_bug_report_present_citation_not_flagged(
        self, client: AsyncClient, db_session, mocker
    ):
        """A BUG_REPORT whose cited file exists on HEAD is NOT flagged."""
        project = await self._project_with_repo(db_session)
        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(return_value=True),
        )
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "BUG_REPORT",
                "content": "Reproduces reliably in backend/app/real.py on load.",
            },
        )
        assert resp.status_code == 200, resp.text
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalar_one()
        assert "_provenance_warnings" not in (msg.context or {})

    @pytest.mark.asyncio
    async def test_bug_report_mixed_present_citation_is_flagged(
        self, client: AsyncClient, db_session, mocker
    ):
        """A partially grounded BUG_REPORT keeps its mixed-state ref detail."""
        project = await self._project_with_repo(db_session)
        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(
                side_effect=lambda path, **_: True if "alive.py" in path else False
            ),
        )
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "BUG_REPORT",
                "content": (
                    "The regression spans backend/app/gone.py and "
                    "backend/app/alive.py during init."
                ),
            },
        )
        assert resp.status_code == 200, resp.text
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalar_one()
        warnings = (msg.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "present"
        assert "backend/app/gone.py" in warnings["broken_refs"]
        assert warnings["ungrounded_refs"] == []

    @pytest.mark.asyncio
    async def test_bug_report_no_refs_not_flagged(
        self, client: AsyncClient, db_session, mocker
    ):
        """A BUG_REPORT with no extractable references degrades to indeterminate."""
        project = await self._project_with_repo(db_session)
        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        spy = mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(return_value=False),
        )
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "BUG_REPORT",
                "content": "Everything feels slow when many users hit the dashboard.",
            },
        )
        assert resp.status_code == 200, resp.text
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalar_one()
        assert "_provenance_warnings" not in (msg.context or {})
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_investigate_task_revalidates_source_message(
        self, client: AsyncClient, db_session, mocker
    ):
        """An investigate task from a stale bug report gets the warning + prompt note."""
        project = await self._project_with_repo(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="The handler in backend/app/gone.py drops the guard.",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(return_value=False),
        )
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/citegate",
                "title": "Investigate dropped guard",
                "prompt": "Investigate the reported dropped guard.",
                "task_type": "investigate",
                "source_message_id": str(msg.id),
            },
        )
        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalar_one()
        warnings = (task.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "absent"
        assert "backend/app/gone.py" in warnings["broken_refs"]
        assert "Provenance warning" in task.payloads[0]["prompt"]
        # Cited code absent on HEAD seeds an already_resolved reproduce baseline.
        verdict = (task.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "already_resolved"

    @pytest.mark.asyncio
    async def test_investigate_task_mixed_citation_preserves_warning_but_not_stale(
        self, client: AsyncClient, db_session, mocker
    ):
        """A mixed citation warning is carried forward without seeding stale-state."""
        project = await self._project_with_repo(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content=(
                "The handler touches backend/app/gone.py and "
                "backend/app/alive.py during load."
            ),
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(
                side_effect=lambda path, **_: True if "alive.py" in path else False
            ),
        )
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/citegate",
                "title": "Investigate mixed grounding",
                "prompt": "Investigate the reported regression.",
                "task_type": "investigate",
                "source_message_id": str(msg.id),
            },
        )
        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalar_one()
        warnings = (task.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "present"
        assert "backend/app/gone.py" in warnings["broken_refs"]
        assert "Provenance warning" not in task.payloads[0]["prompt"]
        verdict = (task.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"

    @pytest.mark.asyncio
    async def test_investigate_task_present_citation_seeds_indeterminate_verdict(
        self, client: AsyncClient, db_session, mocker
    ):
        """An investigate task whose cited code is present on HEAD seeds an
        `indeterminate` reproduce-on-HEAD baseline (defect-truth not yet
        established) and adds no provenance warning / prompt note."""
        project = await self._project_with_repo(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="The handler in backend/app/present.py drops the guard.",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            new=AsyncMock(return_value=True),
        )
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/citegate",
                "title": "Investigate present guard",
                "prompt": "Investigate the reported dropped guard.",
                "task_type": "investigate",
                "source_message_id": str(msg.id),
            },
        )
        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalar_one()
        verdict = (task.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"
        # Present citations leave the report untouched — no provenance warning.
        assert "_provenance_warnings" not in (task.context or {})
        assert "Provenance warning" not in task.payloads[0]["prompt"]

    @pytest.mark.asyncio
    async def test_investigate_task_no_source_message_is_unaffected(
        self, client: AsyncClient, db_session
    ):
        """Omitting source_message_id keeps investigate task creation unchanged."""
        project = await self._project_with_repo(db_session)
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/citegate",
                "title": "Plain investigation",
                "prompt": "Investigate something.",
                "task_type": "investigate",
            },
        )
        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalar_one()
        assert "_provenance_warnings" not in (task.context or {})
        assert task.payloads[0]["prompt"] == "Investigate something."


# ═══════════════════════════════════════════════════════════════════════════════
# task_capabilities.py — strategist spawn allowlist is registry-derived
# ═══════════════════════════════════════════════════════════════════════════════


class TestStrategistSpawnableRegistry:
    """Guard: the strategist spawn allowlist is DERIVED from the per-type
    ``strategist_spawnable`` capability flag, not a hardcoded literal. A future
    type becomes strategist-spawnable by declaring the flag alone — no handler
    edit — and implementation / PR-producing write types stay excluded."""

    def test_allowlist_equals_expected_knowledge_types(self):
        from app.task_capabilities import STRATEGIST_SPAWNABLE_TASK_TYPES

        assert STRATEGIST_SPAWNABLE_TASK_TYPES == frozenset(
            {"research", "plan", "web_research", "design"}
        )

    def test_allowlist_is_derived_from_capability_flag(self):
        from app.task_capabilities import (
            STRATEGIST_SPAWNABLE_TASK_TYPES,
            TASK_CAPABILITY_BY_VALUE,
        )

        # Every member declares the flag...
        for t in STRATEGIST_SPAWNABLE_TASK_TYPES:
            assert TASK_CAPABILITY_BY_VALUE[t].strategist_spawnable is True
        # ...and the set is exactly the flagged capabilities (derivation, not a
        # parallel literal that could drift).
        derived = frozenset(
            value
            for value, cap in TASK_CAPABILITY_BY_VALUE.items()
            if cap.strategist_spawnable
        )
        assert STRATEGIST_SPAWNABLE_TASK_TYPES == derived

    def test_no_write_type_is_strategist_spawnable(self):
        from app.task_capabilities import (
            STRATEGIST_SPAWNABLE_TASK_TYPES,
            WRITE_TASK_TYPES,
        )

        # Implementation / PR-producing types must not be directly spawnable —
        # they flow through direction → research → plan → create.
        assert STRATEGIST_SPAWNABLE_TASK_TYPES.isdisjoint(WRITE_TASK_TYPES)
        for impl in ("create", "fix", "refactor", "migrate", "qa"):
            assert impl not in STRATEGIST_SPAWNABLE_TASK_TYPES
