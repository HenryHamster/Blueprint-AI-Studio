"""
Tests for three admin routers:
1. admin_users.py  — user management + project member management
2. admin_projects.py — project CRUD + autonomous mode
3. admin_notifications.py — alerts, messages, playbook
"""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app.models import (
    AllowedUser, Project, ProjectMember, Task, TaskStatus,
    Alert, ProjectMessage, MessageType,
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
        emails = [u["email"] for u in resp.json()]
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
    async def test_add_user_duplicate_email_returns_409(self, client: AsyncClient, db_session):
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

        resp = await client.patch(
            f"/api/admin/users/{user.id}", json={"role": "admin"}
        )
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
    async def test_update_user_invalid_role_returns_422(self, client: AsyncClient, db_session):
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
        members = resp.json()
        assert len(members) == 1
        assert members[0]["role"] == "owner"

    @pytest.mark.asyncio
    async def test_list_members_non_member_returns_404(self, client: AsyncClient, db_session):
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
        db_session.add(AllowedUser(email="m@example.com", firebase_uid="m-uid", role="user"))
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
        db_session.add(AllowedUser(email="rem@example.com", firebase_uid="rem-uid", role="user"))
        m = ProjectMember(project_id=project.id, user_id="rem-uid", role="member")
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/members/{m.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Member removed"

    @pytest.mark.asyncio
    async def test_remove_project_owner_returns_400(self, client: AsyncClient, db_session):
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
        ids = [p["id"] for p in resp.json()]
        assert str(project.id) in ids

    @pytest.mark.asyncio
    async def test_excludes_other_users_projects(self, client: AsyncClient, db_session):
        """Projects the user has no membership in are excluded."""
        other = Project(name="Other Project", user_id="other-owner")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(ProjectMember(project_id=other.id, user_id="other-owner", role="owner"))
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert str(other.id) not in ids

    @pytest.mark.asyncio
    async def test_response_includes_my_role(self, client: AsyncClient, db_session):
        """Each project entry includes the user's role."""
        await _make_project(db_session, name="Role Test")

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        assert all("my_role" in p for p in resp.json())


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


class TestDeleteProject:
    @pytest.mark.asyncio
    async def test_delete_project_no_active_tasks(self, client: AsyncClient, db_session):
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
        counts = resp.json()["counts"]
        assert counts.get(direction_id) == 1


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
    async def test_playbook_update_existing_entry(self, client: AsyncClient, db_session):
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

    @pytest.mark.asyncio
    async def test_patch_valid_notification_config(self, client: AsyncClient):
        """PATCH with a valid notification_config is accepted."""
        resp = await client.patch("/api/admin/settings", json={
            "notification_config": {
                "webhook_url": "https://hooks.example.com/token",
                "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED"],
                "throttle_seconds": 60,
            }
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_webhook_url(self, client: AsyncClient):
        """webhook_url must start with http:// or https://."""
        resp = await client.patch("/api/admin/settings", json={
            "notification_config": {"webhook_url": "ftp://bad.example.com"}
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_throttle_below_zero(self, client: AsyncClient):
        """throttle_seconds must be >= 0."""
        resp = await client.patch("/api/admin/settings", json={
            "notification_config": {"throttle_seconds": -1}
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_invalid_throttle_above_max(self, client: AsyncClient):
        """throttle_seconds must be <= 3600."""
        resp = await client.patch("/api/admin/settings", json={
            "notification_config": {"throttle_seconds": 3601}
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_notification_config_unknown_event(self, client: AsyncClient):
        """enabled_events rejects strings not in the known event set."""
        resp = await client.patch("/api/admin/settings", json={
            "notification_config": {
                "enabled_events": ["TASK_FAILED", "UNKNOWN_EVENT_XYZ"],
            }
        })
        assert resp.status_code == 400
        assert "unknown" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_get_settings_admin_sees_webhook_url(self, client: AsyncClient):
        """Admin caller receives the actual webhook_url in notification_config."""
        # First configure a webhook URL as admin
        await client.patch("/api/admin/settings", json={
            "notification_config": {
                "webhook_url": "https://hooks.example.com/secret-token",
                "enabled_events": ["TASK_FAILED"],
                "throttle_seconds": 30,
            }
        })
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        nc = resp.json()["notification_config"]
        assert nc["webhook_url"] == "https://hooks.example.com/secret-token"

    @pytest.mark.asyncio
    async def test_get_settings_non_admin_webhook_url_masked(self, override_get_session):
        """Non-admin caller gets empty webhook_url and a boolean configured flag instead."""
        from app.main import app as _app
        from app.auth import get_current_user as _gcu, require_admin as _ra

        async def _non_admin():
            return {"user_id": "non-admin-user", "email": "user@example.com", "role": "user"}

        async def _reject_admin():
            raise __import__('fastapi').HTTPException(status_code=403, detail="Forbidden")

        # Set a webhook URL in runtime config first
        from app.shared_state import runtime_config
        runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/secret-token",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 30,
        }

        _app.dependency_overrides[_gcu] = _non_admin
        _app.dependency_overrides[_ra] = _reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=_app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/settings")
            assert resp.status_code == 200
            nc = resp.json()["notification_config"]
            assert nc["webhook_url"] == "", "webhook_url must be masked for non-admin"
            assert nc["webhook_url_configured"] is True, "configured flag must be True when URL is set"
        finally:
            _app.dependency_overrides.pop(_gcu, None)
            _app.dependency_overrides.pop(_ra, None)
            # Restore runtime_config
            runtime_config.pop("notification_config", None)
