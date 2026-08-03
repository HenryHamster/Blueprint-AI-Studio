# AUTO-SPLIT from test_admin_routers.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared helpers (_make_project) are duplicated here to keep this
# file self-contained. This file holds the admin_users domain slice: user/member management
# and branch-config diagnostics.
"""Domain slice: admin_users.py — User Management & Project Members

Split from test_admin_routers.py. Covers AllowedUser CRUD, ProjectMember CRUD,
and the null-branch-config diagnostics endpoint."""

import asyncio
import os
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
)
from app.auth import get_current_user, require_admin
from app.admin_helpers import pending_project_member_user_id


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


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project", role="owner"):
    """Create a project with the given user as a member, plus the membership row.

    ``role`` defaults to ``owner``; pass ``viewer``/``member`` to exercise the
    role floor on mutating endpoints (viewer is documented read-only).
    """
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role=role)
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

        normalized = await client.post(
            "/api/admin/users",
            json={"email": "  Mixed.Case@Example.COM ", "role": "user"},
        )
        assert normalized.status_code == 201
        assert normalized.json()["email"] == "mixed.case@example.com"

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

    @pytest.mark.asyncio
    async def test_remove_user_cleans_all_pending_synthetic_memberships(
        self, client: AsyncClient, db_session
    ):
        first_project = await _make_project(db_session, name="First cleanup")
        second_project = await _make_project(db_session, name="Second cleanup")
        invited = AllowedUser(email="cleanup@example.com", role="invited")
        db_session.add(invited)
        await db_session.commit()
        pending_id = pending_project_member_user_id(invited.id)
        db_session.add_all(
            [
                ProjectMember(
                    project_id=first_project.id, user_id=pending_id, role="member"
                ),
                ProjectMember(
                    project_id=second_project.id, user_id=pending_id, role="viewer"
                ),
            ]
        )
        await db_session.commit()
        invited_id = invited.id

        response = await client.delete(f"/api/admin/users/{invited_id}")
        assert response.status_code == 200
        db_session.expire_all()
        assert await db_session.get(AllowedUser, invited_id) is None
        leftovers = (
            await db_session.execute(
                select(ProjectMember).where(ProjectMember.user_id == pending_id)
            )
        ).scalars().all()
        assert leftovers == []


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
    async def test_add_project_member_new_email_creates_pending_invitation(
        self, client: AsyncClient, db_session
    ):
        """An owner can invite a normalized email before its first login."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "  Ghost@Example.COM ", "role": "member"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == "ghost@example.com"
        assert resp.json()["status"] == "pending"
        assert resp.json()["user_id"] is None

        allowed = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == "ghost@example.com")
            )
        ).scalar_one()
        assert allowed.role == "invited"
        assert allowed.firebase_uid is None
        member = await db_session.get(ProjectMember, uuid.UUID(resp.json()["id"]))
        assert member is not None
        assert member.user_id == f"allowed-user:{allowed.id}"

    @pytest.mark.asyncio
    async def test_add_project_member_duplicate_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Repeating the same linked invite returns the existing membership."""
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
        assert resp.status_code == 201
        assert resp.json()["id"] == str(existing.id)
        assert resp.json()["status"] == "active"
        rows = (
            await db_session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == "already-uid",
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
        reason="concurrent invitation uniqueness requires PostgreSQL",
    )
    async def test_concurrent_same_email_invites_are_idempotent(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        url = f"/api/admin/projects/{project.id}/members"
        first, second = await asyncio.gather(
            client.post(
                url,
                json={"email": "race-invite@example.com", "role": "viewer"},
            ),
            client.post(
                url,
                json={"email": "RACE-INVITE@example.com", "role": "member"},
            ),
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["status"] == second.json()["status"] == "pending"

        allowed_rows = (
            await db_session.execute(
                select(AllowedUser).where(
                    AllowedUser.email == "race-invite@example.com"
                )
            )
        ).scalars().all()
        assert len(allowed_rows) == 1
        member_rows = (
            await db_session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id
                    == pending_project_member_user_id(allowed_rows[0].id),
                )
            )
        ).scalars().all()
        assert len(member_rows) == 1
        assert member_rows[0].role == "member"

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
    async def test_pending_member_lists_updates_and_deletes_by_stable_member_id(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        invited = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "pending@example.com", "role": "member"},
        )
        assert invited.status_code == 201
        member_id = invited.json()["id"]

        listed = await client.get(f"/api/admin/projects/{project.id}/members")
        pending = next(item for item in listed.json()["members"] if item["id"] == member_id)
        assert pending == {
            **pending,
            "email": "pending@example.com",
            "role": "member",
            "status": "pending",
            "user_id": None,
        }
        assert "allowed-user:" not in str(listed.json())

        updated = await client.patch(
            f"/api/admin/projects/{project.id}/members/{member_id}",
            json={"role": "viewer"},
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == member_id
        assert updated.json()["email"] == "pending@example.com"
        assert updated.json()["status"] == "pending"
        assert updated.json()["user_id"] is None
        assert updated.json()["role"] == "viewer"

        removed = await client.delete(
            f"/api/admin/projects/{project.id}/members/{member_id}"
        )
        assert removed.status_code == 200
        assert await db_session.get(ProjectMember, uuid.UUID(member_id)) is None

    @pytest.mark.asyncio
    async def test_cancelling_last_invite_removes_only_invitation_owned_allowlist(
        self, client: AsyncClient, db_session
    ):
        first_project = await _make_project(db_session, name="First")
        second_project = await _make_project(db_session, name="Second")
        first = await client.post(
            f"/api/admin/projects/{first_project.id}/members",
            json={"email": "multi@example.com", "role": "member"},
        )
        second = await client.post(
            f"/api/admin/projects/{second_project.id}/members",
            json={"email": "MULTI@example.com", "role": "viewer"},
        )
        allowed = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == "multi@example.com")
            )
        ).scalar_one()
        assert allowed.role == "invited"
        allowed_id = allowed.id

        removed_first = await client.delete(
            f"/api/admin/projects/{first_project.id}/members/{first.json()['id']}"
        )
        assert removed_first.status_code == 200
        assert await db_session.get(AllowedUser, allowed.id) is not None

        removed_second = await client.delete(
            f"/api/admin/projects/{second_project.id}/members/{second.json()['id']}"
        )
        assert removed_second.status_code == 200
        db_session.expire_all()
        assert await db_session.get(AllowedUser, allowed_id) is None

    @pytest.mark.asyncio
    async def test_cancelling_invite_preserves_manual_allowlist_row(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        manual = AllowedUser(email="manual@example.com", role="user")
        db_session.add(manual)
        await db_session.commit()
        invited = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "MANUAL@example.com", "role": "member"},
        )

        removed = await client.delete(
            f"/api/admin/projects/{project.id}/members/{invited.json()['id']}"
        )
        assert removed.status_code == 200
        preserved = await db_session.get(AllowedUser, manual.id)
        assert preserved is not None
        assert preserved.role == "user"

    @pytest.mark.asyncio
    async def test_ambiguous_case_variant_invite_fails_closed(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        db_session.add_all(
            [
                AllowedUser(
                    email="Conflict@Example.com",
                    firebase_uid="first-uid",
                    role="user",
                ),
                AllowedUser(
                    email="conflict@example.com",
                    firebase_uid="second-uid",
                    role="user",
                ),
            ]
        )
        await db_session.commit()

        response = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "CONFLICT@example.com", "role": "member"},
        )
        assert response.status_code == 409
        memberships = (
            await db_session.execute(
                select(ProjectMember).where(ProjectMember.project_id == project.id)
            )
        ).scalars().all()
        assert len(memberships) == 1  # the owner only

    @pytest.mark.asyncio
    async def test_orphan_pending_member_lists_without_exposing_synthetic_id(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        orphan = ProjectMember(
            project_id=project.id,
            user_id=pending_project_member_user_id(uuid.uuid4()),
            role="viewer",
        )
        db_session.add(orphan)
        await db_session.commit()

        listed = await client.get(f"/api/admin/projects/{project.id}/members")
        row = next(item for item in listed.json()["members"] if item["id"] == str(orphan.id))
        assert row["status"] == "pending"
        assert row["email"] is None
        assert row["user_id"] is None
        assert "allowed-user:" not in str(listed.json())

    @pytest.mark.asyncio
    async def test_unrelated_user_cannot_invite_or_mutate_pending_member(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        invited = await client.post(
            f"/api/admin/projects/{project.id}/members",
            json={"email": "protected-pending@example.com", "role": "member"},
        )
        member_id = invited.json()["id"]
        saved_override = app.dependency_overrides.get(get_current_user)

        async def unrelated_user():
            return {
                "user_id": "unrelated-uid",
                "email": "unrelated@example.com",
                "role": "user",
            }

        app.dependency_overrides[get_current_user] = unrelated_user
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as raw_client:
                create = await raw_client.post(
                    f"/api/admin/projects/{project.id}/members",
                    json={"email": "must-not-exist@example.com", "role": "member"},
                )
                update = await raw_client.patch(
                    f"/api/admin/projects/{project.id}/members/{member_id}",
                    json={"role": "viewer"},
                )
                remove = await raw_client.delete(
                    f"/api/admin/projects/{project.id}/members/{member_id}"
                )
        finally:
            if saved_override is None:
                app.dependency_overrides.pop(get_current_user, None)
            else:
                app.dependency_overrides[get_current_user] = saved_override

        assert create.status_code in (403, 404)
        assert update.status_code in (403, 404)
        assert remove.status_code in (403, 404)
        protected = await db_session.get(ProjectMember, uuid.UUID(member_id))
        assert protected is not None
        assert protected.role == "member"
        forbidden = (
            await db_session.execute(
                select(AllowedUser).where(
                    AllowedUser.email == "must-not-exist@example.com"
                )
            )
        ).scalar_one_or_none()
        assert forbidden is None

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

    @pytest.mark.asyncio
    async def test_buckets_deploy_repo_by_identity_despite_misset_flag(
        self, client: AsyncClient, db_session
    ):
        """Verify deploy-identity predicate is used instead of is_self_repo flag.

        When a project matches the deploy identity (github_owner/repo) but has
        is_self_repo=False (misset), it should still be classified in
        self_repo_skipped because deploy-identity is the source of truth.
        """
        from app.runtime_config_state import runtime_config

        # Set deploy identity via runtime_config (simulates DEPLOY_REPO_IDENTITY env)
        runtime_config["deploy_repo_identity"] = "acme/deploy-repo"

        # Create the actual deploy repo with misset is_self_repo flag
        deploy_repo_misset = Project(
            name="Deploy Repo (misset flag)",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="deploy-repo",
            is_self_repo=False,  # Misset — should be True but isn't
        )

        # Create a properly configured non-deploy project
        other_project = Project(
            name="Other Project",
            user_id=TEST_USER_ID,
            github_owner="other",
            github_repo="other-repo",
            is_self_repo=False,
        )

        db_session.add_all([deploy_repo_misset, other_project])
        await db_session.commit()

        resp = await client.get("/api/admin/diagnostics/null-branch-config")
        assert resp.status_code == 200
        data = resp.json()

        # The deploy_repo_misset should be in self_repo_skipped (despite is_self_repo=False)
        # because it matches the deploy identity
        assert data["total"] == 2
        assert len(data["self_repo_skipped"]) == 1
        skipped = data["self_repo_skipped"][0]
        assert skipped["id"] == str(deploy_repo_misset.id)
        assert skipped["name"] == "Deploy Repo (misset flag)"
        assert skipped["github_owner"] == "acme"
        assert skipped["github_repo"] == "deploy-repo"
        assert skipped["is_self_repo"] is False  # Misset flag, but still skipped by identity

        # The other project should be in projects (not skipped)
        assert len(data["projects"]) == 1
        project = data["projects"][0]
        assert project["id"] == str(other_project.id)
