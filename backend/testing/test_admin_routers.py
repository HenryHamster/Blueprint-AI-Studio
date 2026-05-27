"""
Tests for three admin routers:
1. admin_users.py  — user management + project member management
2. admin_projects.py — project CRUD + autonomous mode
3. admin_notifications.py — alerts, messages, playbook
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app.models import (
    AllowedUser,
    Project,
    ProjectMember,
    ReleaseVersion,
    Task,
    TaskStatus,
    Alert,
    ProjectMessage,
    MessageType,
    SystemMaturity,
    _on_task_terminal_callbacks,
)
from app.auth import get_current_user, require_admin


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


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
    async def test_update_autonomous_enabled(self, client: AsyncClient, db_session):
        """Can toggle autonomous_enabled on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}", json={"autonomous_enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is True

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
    async def test_member_cannot_update_vision(self, client: AsyncClient, db_session):
        """Member gets 403 when trying to change vision."""
        project = await self._make_member_project(db_session)
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"vision": {"text": "my vision"}},
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

        await db_session.refresh(project)
        assert (project.build_config or {}).get("env_vars", {}).get("TOKEN") == (
            "secret"
        )
        assert (project.build_config or {}).get("enable_fresh_reviewer") is True
        assert (project.build_config or {}).get("enable_parallel_review") is True
        assert (project.build_config or {}).get("enable_parallel_fix") is False
        assert (project.build_config or {}).get("task_branch_namespace") == "agents"
        assert (project.build_config or {}).get("task_branch_prefix") == "bot/"
        assert (project.build_config or {}).get("pr_title_prefix") == "[bot] "


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
        mocker.patch("app.main._ensure_vm_deleted", return_value=None)
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
    async def test_delete_project_force_populates_deny_list(
        self, client: AsyncClient, db_session, mocker
    ):
        """Force-delete fires terminal callbacks so cancelled task IDs enter the deny-list."""
        mocker.patch("app.main._ensure_vm_deleted", return_value=None)
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
        sentinel = lambda tid: fired_ids.append(tid)  # noqa: E731
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
    async def test_get_autonomous_status(self, client: AsyncClient, db_session):
        """Returns autonomous status summary across all user projects."""
        project = await _make_project(db_session, name="Auto Project")

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        ids = [p["project_id"] for p in data["projects"]]
        assert str(project.id) in ids

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
    async def test_delete_message_not_found_returns_404(self, client: AsyncClient):
        """Deleting a non-existent message returns 404."""
        resp = await client.delete(f"/api/admin/messages/{uuid.uuid4()}")
        assert resp.status_code == 404

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
        """Entry can specify which task types it applies to."""
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

        mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)

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
                # Previous window (24–48h ago): only 1 auto-paused — drives "up" trend
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
        assert data["auto_recovered_count"] == 2  # 3 paused − 1 retired
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


class TestViewportRegressionDigest:
    """Tests for viewport_regression:* cases appearing in /pipeline-regressions."""

    @pytest.mark.asyncio
    async def test_active_viewport_case_appears_in_pipeline_regressions(
        self, client: AsyncClient, db_session
    ):
        """An active viewport_regression:Foo@375 case appears in /pipeline-regressions."""
        from app.models import RegressionRemediationCase, RegressionCaseStatus

        project = await _make_project(db_session)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskDetailDrawerMeta@375",
            signal_name="viewport_regression",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        db_session.add(case)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/pipeline-regressions?project_id={project.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        classes = [r["regression_class"] for r in data["regressions"]]
        assert "viewport_regression:TaskDetailDrawerMeta@375" in classes

        vp = next(
            r
            for r in data["regressions"]
            if r["regression_class"] == "viewport_regression:TaskDetailDrawerMeta@375"
        )
        assert vp["severity"] == "warning"
        assert vp["resolved"] is False

    @pytest.mark.asyncio
    async def test_closed_viewport_case_excluded_from_pipeline_regressions(
        self, client: AsyncClient, db_session
    ):
        """A CLOSED viewport case does not appear in /pipeline-regressions active list."""
        from app.models import RegressionRemediationCase, RegressionCaseStatus

        project = await _make_project(db_session)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:SearchFilterBar@375",
            signal_name="viewport_regression",
            status=RegressionCaseStatus.CLOSED,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        db_session.add(case)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/pipeline-regressions?project_id={project.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        classes = [r["regression_class"] for r in data["regressions"]]
        assert "viewport_regression:SearchFilterBar@375" not in classes

    @pytest.mark.asyncio
    async def test_viewport_case_appears_in_regression_class_kpis(
        self, client: AsyncClient, db_session
    ):
        """A viewport_regression case appears in /regression-class-kpis output."""
        from app.models import RegressionRemediationCase, RegressionCaseStatus

        project = await _make_project(db_session)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:Foo@375",
            signal_name="viewport_regression",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        db_session.add(case)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/regression-class-kpis?project_id={project.id}&window_hours=168"
        )
        assert resp.status_code == 200
        data = resp.json()
        classes = [row["regression_class"] for row in data["classes"]]
        assert "viewport_regression:Foo@375" in classes

    @pytest.mark.asyncio
    async def test_existing_classes_unaffected_by_viewport_case(
        self, client: AsyncClient, db_session
    ):
        """Adding a viewport case does not change existing SR/fix-loop aggregation."""
        from app.models import RegressionRemediationCase, RegressionCaseStatus

        project = await _make_project(db_session)

        existing_case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="fix_loop_divergence:lint",
            signal_name="fix_loop_divergence",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        viewport_case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:AlertsBanner@375",
            signal_name="viewport_regression",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        db_session.add_all([existing_case, viewport_case])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/regression-class-kpis?project_id={project.id}&window_hours=168"
        )
        assert resp.status_code == 200
        data = resp.json()
        by_class = {row["regression_class"]: row for row in data["classes"]}
        # Both classes must be present
        assert "fix_loop_divergence:lint" in by_class
        assert "viewport_regression:AlertsBanner@375" in by_class
        # The existing class' opened count must not be polluted
        assert by_class["fix_loop_divergence:lint"]["cases_opened"] == 1
        assert by_class["viewport_regression:AlertsBanner@375"]["cases_opened"] == 1


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
    async def test_regression_cases_annotated(self, client: AsyncClient, db_session):
        """Viewport regression cases are counted and annotated on matching rows."""
        from unittest.mock import patch
        from app.models import RegressionRemediationCase, RegressionCaseStatus

        project = await _make_project(db_session)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:MessagesPanel.tsx@375",
            signal_name="viewport_regression",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=__import__("datetime").datetime.utcnow(),
        )
        db_session.add(case)
        await db_session.commit()

        fake_rows = [
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
        rows = resp.json()["rows"]
        assert rows[0]["regression_case_count"] == 1

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
                "app.release_qualifier.get_latest_release_audit_report",
                new=AsyncMock(return_value=None),
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
                "app.release_qualifier.get_latest_release_audit_report",
                new=AsyncMock(return_value=None),
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
