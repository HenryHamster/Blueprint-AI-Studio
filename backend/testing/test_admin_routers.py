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
    AllowedUser,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    Alert,
    ProjectMessage,
    MessageType,
    SystemMaturity,
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
        members = resp.json()
        assert len(members) == 1
        assert members[0]["role"] == "owner"

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
        ids = [p["id"] for p in resp.json()]
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
    """Project endpoints expose problem_model + running_update fields so the
    admin-ui can render the new strategic views."""

    @pytest.mark.asyncio
    async def test_list_projects_includes_new_fields(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Build X.", "current_focus": "Ship Y."},
        }
        project.running_update = "## Current state\nShipping feature X."
        db_session.add(project)
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        our = next((p for p in data if p["id"] == str(project.id)), None)
        assert our is not None
        assert "problem_model" in our
        assert our["problem_model"]["intent"]["purpose"] == "Build X."
        assert "running_update" in our
        assert our["running_update"].startswith("## Current state")
        assert "running_update_acknowledged_at" in our
        assert "self_reflection_log" in our


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
