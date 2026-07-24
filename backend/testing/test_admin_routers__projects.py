# AUTO-SPLIT from test_admin_routers.py — see refactor: decompose test_admin_routers.py
# monolith into focused domain slices. Behavior-preserving: conftest.py fixtures
# (client, db_session, session) are inherited automatically; shared helpers are
# duplicated here to keep this file self-contained.
"""Domain slice: admin_projects.py — project CRUD, build-config overrides,
reset/archive/delete, and autonomous-mode endpoints.

Split from test_admin_routers.py; no behavior change."""

import uuid
from datetime import timedelta
from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
    MessagesLastSeen,
    MessagesNoisePrefs,
    MessagesSavedViews,
    _on_task_terminal_callbacks,
    _utcnow,
)


# ── Constants ────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"


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
        item = next(p for p in data["items"] if p["id"] == str(project.id))
        assert item["name"] == "My Project"
        assert "my_role" in item

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

    @pytest.mark.asyncio
    async def test_task_status_counts_present_and_correct(
        self, client: AsyncClient, db_session
    ):
        """task_status_counts reflects active tasks for the project, independent of pagination."""
        from testing.factories import make_task

        project = await _make_project(db_session, name="Counts Project")
        db_session.add_all(
            [
                make_task(project_id=project.id, status=TaskStatus.QUEUED),
                make_task(project_id=project.id, status=TaskStatus.QUEUED),
                make_task(project_id=project.id, status=TaskStatus.IN_PROGRESS),
                make_task(project_id=project.id, status=TaskStatus.COMPLETED),
                make_task(project_id=project.id, status=TaskStatus.FAILED),
                make_task(project_id=project.id, status=TaskStatus.EVALUATING),
                make_task(project_id=project.id, status=TaskStatus.BLOCKED),
                make_task(project_id=project.id, status=TaskStatus.PROVISIONING),
            ]
        )
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        counts = target["task_status_counts"]
        assert counts["queued"] == 2
        assert counts["running"] == 2  # IN_PROGRESS + PROVISIONING
        assert counts["evaluating"] == 1
        assert counts["blocked"] == 1
        assert counts["failed"] == 1
        assert counts["completed"] == 1
        assert counts["total"] == 8

    @pytest.mark.asyncio
    async def test_task_status_counts_exclude_archived_tasks(
        self, client: AsyncClient, db_session
    ):
        """Archived terminal tasks do not inflate the project card counts."""
        from testing.factories import make_task

        project = await _make_project(db_session, name="Archived Counts Project")
        db_session.add_all(
            [
                make_task(project_id=project.id, status=TaskStatus.COMPLETED),
                make_task(
                    project_id=project.id,
                    status=TaskStatus.FAILED,
                    archived=True,
                ),
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    archived=True,
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        counts = target["task_status_counts"]
        assert counts["completed"] == 1
        assert counts["failed"] == 0
        assert counts["total"] == 1

    @pytest.mark.asyncio
    async def test_task_status_counts_cross_project_isolation(
        self, client: AsyncClient, db_session
    ):
        """Counts for one project do not include tasks from a different project."""
        from testing.factories import make_task

        project_a = await _make_project(db_session, name="Project A")
        project_b = await _make_project(db_session, name="Project B")
        db_session.add_all(
            [
                make_task(project_id=project_a.id, status=TaskStatus.COMPLETED),
                make_task(project_id=project_a.id, status=TaskStatus.COMPLETED),
                make_task(project_id=project_b.id, status=TaskStatus.FAILED),
            ]
        )
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        a = next(p for p in items if p["id"] == str(project_a.id))
        b = next(p for p in items if p["id"] == str(project_b.id))
        assert a["task_status_counts"]["completed"] == 2
        assert a["task_status_counts"]["failed"] == 0
        assert b["task_status_counts"]["failed"] == 1
        assert b["task_status_counts"]["completed"] == 0

    @pytest.mark.asyncio
    async def test_task_status_counts_membership_scope(
        self, client: AsyncClient, db_session
    ):
        """A project the caller is not a member of does not appear in the list."""
        from testing.factories import make_task

        # foreign project — no membership for test-user-123
        foreign = Project(name="Foreign Project", user_id="foreign-owner")
        db_session.add(foreign)
        await db_session.commit()
        await db_session.refresh(foreign)
        db_session.add(
            ProjectMember(project_id=foreign.id, user_id="foreign-owner", role="owner")
        )
        db_session.add(make_task(project_id=foreign.id, status=TaskStatus.COMPLETED))
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()["items"]]
        assert str(foreign.id) not in ids

    @pytest.mark.asyncio
    async def test_task_status_counts_empty_for_project_with_no_tasks(
        self, client: AsyncClient, db_session
    ):
        """A project with no tasks returns zero counts for all buckets."""
        await _make_project(db_session, name="Empty Project")

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["name"] == "Empty Project")
        counts = target["task_status_counts"]
        assert counts["queued"] == 0
        assert counts["running"] == 0
        assert counts["total"] == 0

    @pytest.mark.asyncio
    async def test_task_status_counts_reconcile_cancelled(
        self, client: AsyncClient, db_session
    ):
        """CANCELLED tasks appear in their own bucket and reconcile with total."""
        from testing.factories import make_task

        project = await _make_project(db_session, name="Cancelled Counts Project")
        db_session.add_all(
            [
                make_task(project_id=project.id, status=TaskStatus.COMPLETED),
                make_task(project_id=project.id, status=TaskStatus.CANCELLED),
                make_task(project_id=project.id, status=TaskStatus.CANCELLED),
            ]
        )
        await db_session.commit()

        resp = await client.get("/api/admin/projects")
        assert resp.status_code == 200
        items = resp.json()["items"]
        target = next(p for p in items if p["id"] == str(project.id))
        counts = target["task_status_counts"]
        assert counts["cancelled"] == 2
        assert counts["completed"] == 1
        assert counts["total"] == 3
        # All named buckets must reconcile with total.
        bucket_sum = (
            counts["queued"]
            + counts["running"]
            + counts["evaluating"]
            + counts["blocked"]
            + counts["parked"]
            + counts["failed"]
            + counts["completed"]
            + counts["cancelled"]
        )
        assert bucket_sum == counts["total"]


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

    @pytest.mark.asyncio
    async def test_detail_includes_capability_proof_annotations(
        self, client: AsyncClient, db_session
    ):
        """Project detail returns proof-enriched capabilities without changing the stored model."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Proof Detail")
        project.problem_model = {
            "revision": 3,
            "capabilities": [
                {"name": "language-aware repo map", "status": "working"},
                {"name": "claimed only capability", "status": "partial"},
            ],
        }
        flag_modified(project, "problem_model")
        project.capability_proof_records = {
            "language-aware repo map": [
                {
                    "task_id": "task-proof-1",
                    "proved_at": "2026-06-09T12:34:56Z",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_proof_recorded_on_named_test_proven",
                    "commit_sha": "abc123def456",
                }
            ]
        }
        flag_modified(project, "capability_proof_records")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200
        data = resp.json()
        caps = data["problem_model"]["capabilities"]
        assert caps[0]["proof_status"] == "proven"
        assert caps[0]["proof_records"][0]["proof_kind"] == "named_test"
        assert caps[0]["proof_records"][0]["proof_ref"].endswith(
            "test_proof_recorded_on_named_test_proven"
        )
        assert caps[0]["proof_records"][0]["commit_sha"] == "abc123def456"
        assert caps[0]["proof_records"][0]["proved_at"] == "2026-06-09T12:34:56Z"
        assert caps[1]["proof_status"] == "unproven"
        assert "proof_records" not in caps[1]


class TestCapabilityProofExemption:
    """Operator grant/revoke of the capability-proof exemption carve-out."""

    async def _make_proof_project(self, db_session, role="owner"):
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Exempt Test", role=role)
        project.problem_model = {
            "capabilities": [
                {"name": "Frontend Visual Verification", "status": "working"},
                {"name": "Some Planned Cap", "status": "planned"},
            ]
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()
        return project

    @pytest.mark.asyncio
    async def test_grant_writes_authorized_exemption_record(
        self, client: AsyncClient, db_session
    ):
        """Grant appends an operator-attributed exemption to the platform ledger
        and it is recognized by platform_exempt_capability_names."""
        from app.capability_registry import (
            CAPABILITY_EXEMPTION_PROOF_KIND,
            platform_exempt_capability_names,
        )

        project = await self._make_proof_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/capability-exemptions",
            json={
                "capability_name": "Frontend Visual Verification",
                "reason": "cannot be machine-verified",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "granted"
        assert data["capability_name"] == "frontend-visual-verification"
        assert data["already_exempt"] is False
        assert "frontend-visual-verification" in data["exempt_capabilities"]

        await db_session.refresh(project)
        records = project.capability_proof_records or {}
        entry = records["frontend-visual-verification"][0]
        assert entry["proof_kind"] == CAPABILITY_EXEMPTION_PROOF_KIND
        assert entry["authorized_by"] == "test@example.com"
        assert entry["proof_ref"] == "cannot be machine-verified"
        assert "frontend-visual-verification" in platform_exempt_capability_names(
            records
        )

    @pytest.mark.asyncio
    async def test_grant_is_idempotent(self, client: AsyncClient, db_session):
        """A second grant for the same capability does not append a duplicate."""
        project = await self._make_proof_project(db_session)
        url = f"/api/admin/projects/{project.id}/capability-exemptions"
        body = {"capability_name": "Frontend Visual Verification"}

        r1 = await client.post(url, json=body)
        assert r1.status_code == 200
        r2 = await client.post(url, json=body)
        assert r2.status_code == 200
        assert r2.json()["already_exempt"] is True

        await db_session.refresh(project)
        records = project.capability_proof_records or {}
        assert len(records["frontend-visual-verification"]) == 1

    @pytest.mark.asyncio
    async def test_revoke_removes_exemption_record(
        self, client: AsyncClient, db_session
    ):
        """Revoke removes the operator-exemption record for the named capability."""
        from app.capability_registry import platform_exempt_capability_names

        project = await self._make_proof_project(db_session)
        url = f"/api/admin/projects/{project.id}/capability-exemptions"
        await client.post(url, json={"capability_name": "Frontend Visual Verification"})

        resp = await client.delete(f"{url}/frontend-visual-verification")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "revoked"
        assert data["removed"] == 1
        assert data["exempt_capabilities"] == []

        await db_session.refresh(project)
        records = project.capability_proof_records or {}
        assert "frontend-visual-verification" not in platform_exempt_capability_names(
            records
        )

    @pytest.mark.asyncio
    async def test_revoke_absent_exemption_is_noop(
        self, client: AsyncClient, db_session
    ):
        """Revoking a capability with no exemption returns removed=0, not an error."""
        project = await self._make_proof_project(db_session)
        resp = await client.delete(
            f"/api/admin/projects/{project.id}/capability-exemptions/frontend-visual-verification"
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] == 0

    @pytest.mark.asyncio
    async def test_grant_unknown_capability_returns_404(
        self, client: AsyncClient, db_session
    ):
        """A name that is not a declared 'working' capability is rejected 404."""
        project = await self._make_proof_project(db_session)
        # 'Some Planned Cap' exists but is status=planned, not working.
        resp = await client.post(
            f"/api/admin/projects/{project.id}/capability-exemptions",
            json={"capability_name": "Some Planned Cap"},
        )
        assert resp.status_code == 404
        resp2 = await client.post(
            f"/api/admin/projects/{project.id}/capability-exemptions",
            json={"capability_name": "does-not-exist"},
        )
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_grant_requires_owner_role(self, client: AsyncClient, db_session):
        """A member (below owner) cannot grant an exemption — 403."""
        project = await self._make_proof_project(db_session, role="member")
        resp = await client.post(
            f"/api/admin/projects/{project.id}/capability-exemptions",
            json={"capability_name": "Frontend Visual Verification"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_revoke_requires_owner_role(self, client: AsyncClient, db_session):
        """A member (below owner) cannot revoke an exemption — 403."""
        project = await self._make_proof_project(db_session, role="member")
        resp = await client.delete(
            f"/api/admin/projects/{project.id}/capability-exemptions/frontend-visual-verification"
        )
        assert resp.status_code == 403


class TestGetProjectSummary:
    @pytest.mark.asyncio
    async def test_summary_excludes_design_doc_and_self_reflection_log(
        self, client: AsyncClient, db_session
    ):
        """GET /summary omits design_doc and self_reflection_log but returns
        essential fields like capability_adoption_verdicts and build_config."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Summary Endpoint Test")
        project.design_doc = {"repo_overview": {"overview": "x" * 5000}}
        project.self_reflection_log = [{"entry": "log entry 1"}]
        flag_modified(project, "design_doc")
        flag_modified(project, "self_reflection_log")
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "design_doc" not in data
        assert "self_reflection_log" not in data
        assert data["id"] == str(project.id)
        assert data["name"] == "Summary Endpoint Test"
        assert "capability_adoption_verdicts" in data
        assert "capability_proof_summary" in data
        assert "my_role" in data
        assert "build_config" in data

    @pytest.mark.asyncio
    async def test_summary_returns_404_for_unknown_project(
        self, client: AsyncClient, db_session
    ):
        """GET /summary returns 404 when project does not exist."""
        resp = await client.get(f"/api/admin/projects/{uuid.uuid4()}/summary")
        assert resp.status_code == 404


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
    async def test_design_doc_edit_preserves_underscore_system_keys(
        self, client: AsyncClient, db_session
    ):
        """A wholesale design_doc edit from the doc editor (which surfaces only
        public keys) must NOT drop system/operator-owned underscore-prefixed keys
        such as _auto_remediation — this is exactly how an operator's enabled
        auto-remediation config silently disappeared after a later doc edit. An
        underscore key that IS in the payload still wins (explicit intent)."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.design_doc = {
            "key": "old",
            "_auto_remediation": {"enabled": True, "max_per_hour": 2},
            "_issue_implementation": {"enabled": True},
            "_meta": {"revision": 4},
        }
        flag_modified(project, "design_doc")
        db_session.add(project)
        await db_session.commit()

        # The editor round-trips ONLY the public key.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"design_doc": {"key": "new"}, "design_doc_base_revision": 4},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        doc = project.design_doc or {}
        assert doc.get("key") == "new"
        # System/operator-owned underscore keys survived the wholesale replace.
        assert doc.get("_auto_remediation") == {"enabled": True, "max_per_hour": 2}
        assert doc.get("_issue_implementation") == {"enabled": True}
        assert doc.get("_meta", {}).get("revision") == 5

        # An explicit underscore key in a later payload overrides the stored one.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "design_doc": {
                    "key": "new",
                    "_auto_remediation": {"enabled": False},
                },
                "design_doc_base_revision": 5,
            },
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        doc = project.design_doc or {}
        assert doc.get("_auto_remediation") == {"enabled": False}
        # The untouched underscore key is still preserved.
        assert doc.get("_issue_implementation") == {"enabled": True}

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
    async def test_member_cannot_update_status(self, client: AsyncClient, db_session):
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
        assert "requires_ci_checks" not in apply_operator_build_config_overrides(
            {"language": "python"}, {"other": 1}
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

    def test_has_ci_false_clamps_stale_requires_ci_checks_true(self):
        """Secondary fix: detection reconcile — has_ci=False + stale
        requires_ci_checks=True is an impossible-to-satisfy state that strands
        main-targeting rollup PRs.  Re-detection must clamp requires_ci_checks
        to False when has_ci=False so the state is self-consistent."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        # Stale existing config claims CI checks are required, but fresh
        # detection found zero CI workflows.
        merged = apply_operator_build_config_overrides(
            {"language": "python", "has_ci": False},
            {"requires_ci_checks": True},
        )
        assert merged["has_ci"] is False
        # The contradiction is resolved: requires_ci_checks is clamped to False.
        assert merged["requires_ci_checks"] is False

    def test_has_ci_false_already_ungated_preserved(self):
        """Clamping is a no-op when existing requires_ci_checks is already False."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        merged = apply_operator_build_config_overrides(
            {"language": "python", "has_ci": False},
            {"requires_ci_checks": False},
        )
        assert merged["has_ci"] is False
        assert merged["requires_ci_checks"] is False

    def test_has_ci_none_does_not_clamp_requires_ci_checks(self):
        """has_ci=None (incomplete detection) must not trigger the clamp — only
        a definitive False does."""
        from app.build_config_detection import (
            apply_operator_build_config_overrides,
        )

        merged = apply_operator_build_config_overrides(
            {"language": "python", "has_ci": None},
            {"requires_ci_checks": True},
        )
        # has_ci=None is not authoritative; requires_ci_checks is preserved as-is.
        assert merged["requires_ci_checks"] is True

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

    def test_preserves_project_verification_by_normalized_directory_path(self):
        from app.build_config_detection import apply_operator_build_config_overrides

        detected = {
            "directories": [{"path": "backend", "type": "python"}],
        }
        existing = {
            "directories": [{
                "path": " ./backend/// ",
                "type": "python",
                "project_verification": {
                    "protocol": "blueprint-v1",
                    "harness": "scripts/test-backend.sh",
                },
            }],
        }

        merged = apply_operator_build_config_overrides(detected, existing)

        assert merged["directories"][0]["project_verification"] == {
            "protocol": "blueprint-v1",
            "harness": "scripts/test-backend.sh",
        }


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


class TestPatchBuildConfigProjectVerification:
    @pytest.mark.asyncio
    async def test_patch_persists_canonical_project_verification(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="patch-project-verification")

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "build_config": {
                    "directories": [{
                        "path": " backend/// ",
                        "type": "python",
                        "lint_cmd": "ruff check .",
                        "test_cmd": "pytest",
                        "project_verification": {
                            "protocol": "blueprint-v1",
                            "harness": "scripts/test-backend.sh",
                        },
                    }],
                },
            },
        )

        assert resp.status_code == 200, resp.text
        directory = resp.json()["build_config"]["directories"][0]
        assert directory["path"] == "backend"
        assert directory["project_verification"] == {
            "protocol": "blueprint-v1",
            "harness": "scripts/test-backend.sh",
        }
        await db_session.refresh(project)
        assert project.build_config["directories"][0] == directory

    @pytest.mark.asyncio
    async def test_patch_rejects_noncanonical_verification_harness(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="patch-invalid-verification")

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "build_config": {
                    "directories": [{
                        "path": "backend",
                        "type": "python",
                        "project_verification": {
                            "protocol": "blueprint-v1",
                            "harness": "scripts/test backend.sh",
                        },
                    }],
                },
            },
        )

        assert resp.status_code == 400


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


class TestPatchBuildConfigParallelismCaps:
    """PATCH /projects/{id} must persist, preserve, validate, and clear the
    per-project parallelism cap overrides (see schemas.BuildConfig)."""

    @pytest.mark.asyncio
    async def test_patch_persists_and_preserves_caps(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="patch-parallel-caps")

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={
                "build_config": {
                    "max_parallel_research": 2,
                    "max_total_research": 4,
                    "max_autonomous_tasks_per_hour": 8,
                }
            },
        )
        assert resp.status_code == 200, resp.text
        bc = resp.json()["build_config"]
        assert bc.get("max_parallel_research") == 2
        assert bc.get("max_total_research") == 4
        assert bc.get("max_autonomous_tasks_per_hour") == 8

        # A later PATCH that does not mention the caps must not drop them.
        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"language": "python"}},
        )
        assert resp.status_code == 200, resp.text
        bc = resp.json()["build_config"]
        assert bc.get("language") == "python"
        assert bc.get("max_parallel_research") == 2
        assert bc.get("max_total_research") == 4
        assert bc.get("max_autonomous_tasks_per_hour") == 8

        await db_session.refresh(project)
        assert (project.build_config or {}).get("max_parallel_research") == 2

    @pytest.mark.asyncio
    async def test_patch_explicit_null_clears_cap(
        self, client: AsyncClient, db_session
    ):
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="patch-parallel-caps-clear")
        project.build_config = {"max_parallel_research": 2}
        flag_modified(project, "build_config")
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"build_config": {"max_parallel_research": None}},
        )
        assert resp.status_code == 200, resp.text
        assert "max_parallel_research" not in resp.json()["build_config"]

        await db_session.refresh(project)
        assert "max_parallel_research" not in (project.build_config or {})

    @pytest.mark.asyncio
    async def test_patch_rejects_out_of_bounds_caps(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="patch-parallel-caps-bounds")
        for bad in (
            {"max_parallel_research": 0},
            {"max_total_research": 51},
            {"max_autonomous_tasks_per_hour": "fast"},
        ):
            resp = await client.patch(
                f"/api/admin/projects/{project.id}",
                json={"build_config": bad},
            )
            assert resp.status_code == 400, (bad, resp.text)


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

        # Snapshot the id BEFORE deleting: expire_all() below expires the
        # (now-deleted) project row, so a later ``project.id`` lazy-load would
        # raise MissingGreenlet under real ORM expiry.
        project_id = project.id
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
                        _select(_model).where(_model.project_id == project_id)
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
        db_session.add(MessagesLastSeen(user_id=TEST_USER_ID, project_id=project.id))
        await db_session.commit()

        resp = await client.delete(f"/api/admin/projects/{project.id}")
        assert resp.status_code == 200, (
            f"project delete must cascade messageslastseen, not FK-violate; "
            f"got {resp.status_code}: {resp.text}"
        )
        # The child row is gone with the project.
        remaining = (
            (
                await db_session.execute(
                    select(MessagesLastSeen).where(
                        MessagesLastSeen.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []

    @pytest.mark.asyncio
    async def test_delete_project_with_knowledge_document_succeeds(
        self, client: AsyncClient, db_session
    ):
        """A project that produced a KnowledgeDocument can be force-deleted.

        Regression for the latent FK violation: knowledgedocument.source_task_id
        had no ON DELETE action, so deleting the project cascaded to tasks but
        then blocked on the KnowledgeDocument → task FK (constraint
        knowledgedocument_source_task_id_fkey).  Migration 0289 adds CASCADE;
        this test proves the end-to-end delete succeeds and both the task and
        the knowledge document row are gone.
        """
        from app.models import KnowledgeDocument

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "web_research", "topic": "auth", "base_branch": "main"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        doc = KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Auth design",
            summary="Summary of auth design.",
            body="# Auth\n\nBody text.",
            source_kind="web_research",
        )
        db_session.add(doc)
        await db_session.commit()
        doc_id = doc.id

        resp = await client.delete(f"/api/admin/projects/{project.id}?force=true")
        assert resp.status_code == 200, (
            f"project delete must cascade knowledgedocument rows, not FK-violate; "
            f"got {resp.status_code}: {resp.text}"
        )

        db_session.expire_all()
        remaining_doc = (
            await db_session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
            )
        ).scalar_one_or_none()
        assert remaining_doc is None, (
            "KnowledgeDocument must be removed with its source task"
        )

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
        target = next(p for p in resp.json()["items"] if p["id"] == str(project.id))
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
    async def test_archive_non_owner_returns_403(self, client: AsyncClient, db_session):
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
            (
                await db_session.execute(
                    select(Task).where(
                        Task.status == TaskStatus.QUEUED, candidate_filter
                    )
                )
            )
            .scalars()
            .all()
        )
        assert task.id in {t.id for t in rows}

        # Archiving removes the task from the candidate pool.
        await client.post(f"/api/admin/projects/{project.id}/archive")
        rows = (
            (
                await db_session.execute(
                    select(Task).where(
                        Task.status == TaskStatus.QUEUED, candidate_filter
                    )
                )
            )
            .scalars()
            .all()
        )
        assert task.id not in {t.id for t in rows}

        # Unarchiving restores eligibility.
        await client.post(f"/api/admin/projects/{project.id}/unarchive")
        rows = (
            (
                await db_session.execute(
                    select(Task).where(
                        Task.status == TaskStatus.QUEUED, candidate_filter
                    )
                )
            )
            .scalars()
            .all()
        )
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
            (
                await db_session.execute(
                    select(Project).where(
                        Project.autonomous_enabled.is_(True),
                        Project.not_archived(),
                    )
                )
            )
            .scalars()
            .all()
        )
        ids = {p.id for p in rows}
        assert active.id in ids
        assert archived.id not in ids


class TestPurgeArchive:
    """POST /api/admin/tasks/purge-archive permanently deletes archived tasks."""

    @pytest.mark.asyncio
    async def test_purge_deletes_archived_tasks(self, client: AsyncClient, db_session):
        """Purging the archive permanently removes archived tasks."""
        from testing.factories import make_task

        project = await _make_project(db_session)
        task = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] >= 1

        # Task must no longer exist in the database
        result = await db_session.execute(select(Task).where(Task.id == task_id))
        assert result.scalars().first() is None

    @pytest.mark.asyncio
    async def test_purge_only_removes_archived_tasks(
        self, client: AsyncClient, db_session
    ):
        """Purge must not touch live (non-archived) tasks."""
        from testing.factories import make_task

        project = await _make_project(db_session)
        live = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=False
        )
        archived = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(live)
        db_session.add(archived)
        await db_session.commit()
        live_id, archived_id = live.id, archived.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200

        # Live task survives
        live_row = (
            (await db_session.execute(select(Task).where(Task.id == live_id)))
            .scalars()
            .first()
        )
        assert live_row is not None

        # Archived task is gone
        gone = (
            (await db_session.execute(select(Task).where(Task.id == archived_id)))
            .scalars()
            .first()
        )
        assert gone is None

    @pytest.mark.asyncio
    async def test_purge_deletes_payloadless_and_agent_executed_archive(
        self, client: AsyncClient, db_session
    ):
        # BACKEND_MANAGED_TASK_TYPES is empty, so the archive-purge does not
        # special-case any agent-executed rows — both archived tasks are purged.
        # NOTE: with no backend-managed task types currently defined, the purge's
        # "keep backend-managed" branch is presently vacuous — follow-up: prune
        # it or reintroduce a backend-managed type to re-cover it.
        project = await _make_project(db_session)
        payloadless = Task(
            project_id=project.id,
            title="legacy parent",
            payloads=[],
            archived=True,
            status=TaskStatus.COMPLETED,
        )
        agent_executed = Task(
            project_id=project.id,
            title="Agent-executed archive",
            payloads=[{"type": "create"}],
            archived=True,
            status=TaskStatus.QUEUED,
        )
        db_session.add(payloadless)
        db_session.add(agent_executed)
        await db_session.commit()

        response = await client.post("/api/admin/tasks/purge-archive", json={})
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 2
        remaining_ids = set(
            (
                await db_session.execute(
                    select(Task.id).where(
                        Task.id.in_([payloadless.id, agent_executed.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert payloadless.id not in remaining_ids
        assert agent_executed.id not in remaining_ids

    @pytest.mark.asyncio
    async def test_purge_older_than_days_filter(self, client: AsyncClient, db_session):
        """older_than_days skips recently archived tasks."""
        from testing.factories import make_task
        from datetime import timedelta
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        old_ts = _utcnow() - timedelta(days=60)
        recent_ts = _utcnow() - timedelta(days=1)

        old = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            archived=True,
            created_at=old_ts,
        )
        recent = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            archived=True,
            created_at=recent_ts,
        )
        db_session.add(old)
        db_session.add(recent)
        await db_session.commit()
        old_id, recent_id = old.id, recent.id

        resp = await client.post(
            "/api/admin/tasks/purge-archive", json={"older_than_days": 30}
        )
        assert resp.status_code == 200

        # Old task deleted, recent task survives
        assert (
            await db_session.execute(select(Task).where(Task.id == old_id))
        ).scalars().first() is None
        assert (
            await db_session.execute(select(Task).where(Task.id == recent_id))
        ).scalars().first() is not None

    @pytest.mark.asyncio
    async def test_purge_returns_zero_when_nothing_to_delete(
        self, client: AsyncClient, db_session
    ):
        """Returns deleted_count=0 when no archived tasks exist."""
        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 0

    @pytest.mark.asyncio
    async def test_purge_rejects_invalid_older_than_days(
        self, client: AsyncClient, db_session
    ):
        """older_than_days=0 is rejected with 400."""
        resp = await client.post(
            "/api/admin/tasks/purge-archive", json={"older_than_days": 0}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_purge_does_not_delete_other_users_tasks(
        self, client: AsyncClient, db_session
    ):
        """Purge is scoped to the caller's projects only."""
        from testing.factories import make_task

        foreign = Project(name="foreign-project", user_id="other-user-999")
        db_session.add(foreign)
        await db_session.commit()
        await db_session.refresh(foreign)

        task = make_task(
            project_id=foreign.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200

        # Foreign project's archived task must survive
        row = (
            (await db_session.execute(select(Task).where(Task.id == task_id)))
            .scalars()
            .first()
        )
        assert row is not None


    @pytest.mark.asyncio
    async def test_purge_detaches_surviving_child_from_archived_parent(
        self, client: AsyncClient, db_session
    ):
        """Purging an archived parent with a surviving child must succeed.

        The self-referential FK (task.parent_task_id -> task.id) uses NO ACTION,
        so deleting a parent while a live child still references it raises
        IntegrityError and rolls back the whole purge.  The fix nulls out
        parent_task_id on surviving tasks before the DELETE.
        """
        from testing.factories import make_task

        project = await _make_project(db_session)

        # Archived parent — eligible for purge
        parent = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(parent)
        await db_session.flush()

        # Live child referencing the archived parent — must NOT be deleted
        child = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=False
        )
        child.parent_task_id = parent.id
        db_session.add(child)
        await db_session.commit()
        parent_id, child_id = parent.id, child.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] >= 1

        # The endpoint runs on its OWN session (separate from this test's
        # ``db_session``) and commits the detach UPDATE + parent DELETE there.
        # This test's ``db_session`` still holds the ``parent``/``child`` ORM
        # instances it created in its identity map, and because the harness uses
        # ``expire_on_commit=False`` their attributes are NOT auto-expired. A
        # plain re-``select()`` would return those cached instances unchanged
        # (identity-map hit), masking the committed change — even ``expire_all()``
        # is insufficient here. Expunge the identity map so the re-queries below
        # materialize fresh objects from the rows the endpoint actually committed.
        db_session.expunge_all()

        # Archived parent is gone
        gone = (
            (await db_session.execute(select(Task).where(Task.id == parent_id)))
            .scalars()
            .first()
        )
        assert gone is None

        # Live child survives with its lineage detached
        surviving = (
            (await db_session.execute(select(Task).where(Task.id == child_id)))
            .scalars()
            .first()
        )
        assert surviving is not None
        assert surviving.parent_task_id is None


class TestArchiveRoleFloor:
    """bulk-archive / purge-archive are mutations → member+ only.

    Both were scoped by membership alone (any role), while the sibling
    ``POST /tasks/bulk-action`` already filtered ``role.in_(["member","owner"])``
    — so this was an inconsistency, not a design choice. purge-archive is the
    sharpest case: it PERMANENTLY deletes up to 5000 task rows per call, and a
    viewer is documented read-only.
    """

    @pytest.mark.asyncio
    async def test_purge_archive_skips_viewer_project(
        self, client: AsyncClient, db_session
    ):
        """A viewer must not be able to permanently delete another member's tasks."""
        from testing.factories import make_task

        project = await _make_project(
            db_session, name="Viewer Purge", role="viewer"
        )
        task = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted_count"] == 0

        # The archived task on the viewer-only project must still exist.
        db_session.expunge_all()
        row = (
            (await db_session.execute(select(Task).where(Task.id == task_id)))
            .scalars()
            .first()
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_purge_archive_deletes_member_project_tasks(
        self, client: AsyncClient, db_session
    ):
        """The floor must not over-reject: a member (not owner) can still purge."""
        from testing.factories import make_task

        project = await _make_project(
            db_session, name="Member Purge", role="member"
        )
        task = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=True
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post("/api/admin/tasks/purge-archive", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted_count"] >= 1

        db_session.expunge_all()
        row = (
            (await db_session.execute(select(Task).where(Task.id == task_id)))
            .scalars()
            .first()
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_bulk_archive_skips_viewer_project(
        self, client: AsyncClient, db_session
    ):
        """A viewer must not be able to bulk-archive another member's tasks."""
        from testing.factories import make_task

        project = await _make_project(
            db_session, name="Viewer Archive", role="viewer"
        )
        task = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=False
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post(
            "/api/admin/tasks/bulk-archive", json={"task_ids": [str(task_id)]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_count"] == 0

        await db_session.refresh(task)
        assert task.archived is False

    @pytest.mark.asyncio
    async def test_bulk_archive_archives_member_project_tasks(
        self, client: AsyncClient, db_session
    ):
        """The floor must not over-reject: a member (not owner) can still archive."""
        from testing.factories import make_task

        project = await _make_project(
            db_session, name="Member Archive", role="member"
        )
        task = make_task(
            project_id=project.id, status=TaskStatus.COMPLETED, archived=False
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        resp = await client.post(
            "/api/admin/tasks/bulk-archive", json={"task_ids": [str(task_id)]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_count"] == 1

        db_session.expunge_all()
        row = (
            (await db_session.execute(select(Task).where(Task.id == task_id)))
            .scalars()
            .first()
        )
        assert row is not None and row.archived is True


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
    async def test_pause_all_skips_viewer_projects(
        self, client: AsyncClient, db_session
    ):
        """pause-all must not touch projects where the caller is only a viewer.

        Viewers are read-only: disabling autonomous mode is a mutation they may
        not perform, so the project is left running and excluded from the count.

        This route takes no ``project_id``, so it is inherently the "pause
        everything I can" bulk shape — scoping viewer-only projects out silently
        is correct, but it must report what it left RUNNING via ``skipped_count``
        rather than hiding it.
        """
        viewed = await _make_project(db_session, name="Viewed", role="viewer")
        viewed.autonomous_enabled = True
        db_session.add(viewed)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 0
        # The still-running viewer project is surfaced, not silently dropped.
        assert resp.json()["skipped_count"] == 1

        await db_session.refresh(viewed)
        assert viewed.autonomous_enabled is True

    @pytest.mark.asyncio
    async def test_pause_all_pauses_member_projects(
        self, client: AsyncClient, db_session
    ):
        """The member floor must not over-reject: a member (not owner) can pause."""
        owned = await _make_project(db_session, name="Member Project", role="member")
        owned.autonomous_enabled = True
        db_session.add(owned)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 1

        await db_session.refresh(owned)
        assert owned.autonomous_enabled is False

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
    async def test_pause_project_requires_owner(self, client: AsyncClient, db_session):
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
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="Auto Project")

        # Seed a downstream servicing orchestrator task to confirm the
        # health view separates churn from initiative spawns.
        non_research_task = Task(
            project_id=project.id,
            payloads=[{"type": "review", "pr_number": 1}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=_utcnow() - timedelta(minutes=30),
        )
        db_session.add(non_research_task)
        await db_session.commit()

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        by_id = {p["project_id"]: p for p in data["projects"]}
        assert str(project.id) in by_id
        entry = by_id[str(project.id)]
        # Freshly created project: autonomous_enabled defaults True (model default),
        # no failures, rate_limit mirrors global config, source is fleet_default.
        assert entry["autonomous_enabled"] is True
        assert entry["consecutive_failures"] == 0
        # Total churn is still visible, but it is separated from initiative.
        assert entry["tasks_spawned_last_hour"] == 1
        assert entry["initiative_tasks_spawned_last_hour"] == 0
        assert entry["servicing_tasks_spawned_last_hour"] == 1
        assert entry["research_create_tasks_spawned_last_hour"] == 0
        assert entry["last_research_at"] is None
        assert entry["rate_limit"] == runtime_config["max_autonomous_tasks_per_hour"]
        assert (
            entry["fleet_default_rate_limit"]
            == runtime_config["max_autonomous_tasks_per_hour"]
        )
        assert entry["rate_limit_source"] == "fleet_default"
        assert "global_rate_limit" in data
        assert data["global_rate_limit"]["global_headroom"] >= 0

    @pytest.mark.asyncio
    async def test_get_autonomous_status_uses_runtime_categories(
        self, client: AsyncClient, db_session
    ):
        """Custom autonomous spawn categories stay aligned with the status split."""
        from app.shared_state import runtime_config
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="Categorized Auto Project")
        original_categories = runtime_config.get("autonomous_spawn_budget_categories")
        runtime_config["autonomous_spawn_budget_categories"] = {
            "initiative": ["review"],
            "servicing": ["qa", "deps", "security_audit"],
        }
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "review"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=30),
            )
        )
        await db_session.commit()
        try:
            resp = await client.get("/api/admin/autonomous-status")
            assert resp.status_code == 200
            data = resp.json()
            entry = next(p for p in data["projects"] if p["project_id"] == str(project.id))
            assert entry["tasks_spawned_last_hour"] == 1
            assert entry["initiative_tasks_spawned_last_hour"] == 1
            assert entry["servicing_tasks_spawned_last_hour"] == 0
            assert entry["other_tasks_spawned_last_hour"] == 0
        finally:
            runtime_config["autonomous_spawn_budget_categories"] = original_categories

    @pytest.mark.asyncio
    async def test_get_autonomous_status_project_override(
        self, client: AsyncClient, db_session
    ):
        """Project with build_config override reports the override cap and correct source."""
        from app.shared_state import runtime_config
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Override Project")
        project.build_config = {"max_autonomous_tasks_per_hour": 5}
        flag_modified(project, "build_config")
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        by_id = {p["project_id"]: p for p in data["projects"]}
        assert str(project.id) in by_id
        entry = by_id[str(project.id)]
        assert entry["rate_limit"] == 5
        assert (
            entry["fleet_default_rate_limit"]
            == runtime_config["max_autonomous_tasks_per_hour"]
        )
        assert entry["rate_limit_source"] == "project_override"

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

    @pytest.mark.asyncio
    async def test_admin_tasks_direction_filter_matches_canonical_shapes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session)
        direction_id = "d-canonical"
        expected_tasks = [
            Task(
                project_id=project.id,
                title="context direction",
                context={"direction_id": direction_id},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="payload direction",
                payloads=[{"type": "research", "direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            ),
            Task(
                project_id=project.id,
                title="current direction",
                payloads=[{"type": "research", "current_direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            ),
            Task(
                project_id=project.id,
                title="directions array",
                payloads=[{"type": "plan", "directions": [{"id": direction_id}]}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="plan output source",
                payloads=[{"type": "plan"}],
                plan_output={"source_direction_id": direction_id},
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="nested finding",
                payloads=[{"type": "research"}],
                plan_output={"findings": [{"direction_id": direction_id}]},
                status=TaskStatus.QUEUED,
            ),
        ]
        excluded_tasks = [
            Task(
                project_id=project.id,
                title="other direction",
                context={"direction_id": "d-other"},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=other_project.id,
                title="cross project direction",
                context={"direction_id": direction_id},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
        ]
        db_session.add_all([*expected_tasks, *excluded_tasks])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/tasks?project_id={project.id}&direction_id={direction_id}&limit=50"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        returned_ids = {row["id"] for row in body["tasks"]}
        assert returned_ids == {str(task.id) for task in expected_tasks}
        assert body["total"] == len(expected_tasks)

    @pytest.mark.asyncio
    async def test_get_direction_task_counts_matches_canonical_shapes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session)
        direction_id = "d-counts"
        db_session.add_all(
            [
                Task(
                    project_id=project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "research", "direction_id": direction_id}],
                    status=TaskStatus.IN_PROGRESS,
                ),
                Task(
                    project_id=project.id,
                    payloads=[
                        {
                            "type": "research",
                            "current_direction_id": direction_id,
                            "directions": [{"id": "d-not-current"}],
                        }
                    ],
                    status=TaskStatus.IN_PROGRESS,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "plan", "directions": [{"id": direction_id}]}],
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "plan"}],
                    plan_output={"research_findings": [{"source_direction_id": direction_id}]},
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "failed"}],
                    status=TaskStatus.FAILED,
                ),
                Task(
                    project_id=other_project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/directions/task-counts"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The task with direction_id only in plan_output is not counted: the endpoint
        # skips plan_output for performance (task_primary_direction_ids_shallow).
        assert body["counts"][direction_id] == 4
        assert "d-not-current" not in body["counts"]
        assert body["counts_by_type"][direction_id]["create"] == 1
        assert body["counts_by_type"][direction_id]["research"] == 2
        assert body["counts_by_type"][direction_id]["plan"] == 1

    @pytest.mark.asyncio
    async def test_directions_lite_mode_omits_heavy_fields(
        self, client: AsyncClient, db_session
    ):
        """?lite=1 omits design_doc, problem_model, self_reflection_log, and
        running_update_reconciliation while preserving directions and lightweight fields."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.directions = [{"id": "d-lite1", "text": "Lite test", "priority": "medium"}]
        project.design_doc = {"key": "large doc"}
        project.problem_model = {"intent": {"purpose": "Build X."}}
        project.self_reflection_log = [{"entry": "some reflection"}]
        flag_modified(project, "directions")
        flag_modified(project, "design_doc")
        flag_modified(project, "problem_model")
        flag_modified(project, "self_reflection_log")
        db_session.add(project)
        await db_session.commit()

        # Full response includes heavy fields
        full = await client.get(f"/api/admin/projects/{project.id}/directions")
        assert full.status_code == 200
        full_data = full.json()
        assert "design_doc" in full_data
        assert "problem_model" in full_data
        assert "self_reflection_log" in full_data
        assert len(full_data["directions"]) == 1

        # Lite response omits heavy fields but keeps directions and lightweight fields
        lite = await client.get(
            f"/api/admin/projects/{project.id}/directions?lite=1"
        )
        assert lite.status_code == 200
        lite_data = lite.json()
        assert "design_doc" not in lite_data
        assert "problem_model" not in lite_data
        assert "self_reflection_log" not in lite_data
        assert "running_update_reconciliation" not in lite_data
        assert len(lite_data["directions"]) == 1
        assert "autonomous_enabled" in lite_data
        assert "running_update" in lite_data


class TestAutonomyDebug:
    """Tests for GET /api/admin/projects/{project_id}/autonomy-debug."""

    @pytest.mark.asyncio
    async def test_rate_limit_counts_only_research_tasks(
        self, client: AsyncClient, db_session
    ):
        """tasks_this_hour in rate_limit block counts only research-type ORCHESTRATOR
        tasks, not non-research ORCHESTRATOR tasks created in the last hour.

        This exercises the dialect-specific SQL COUNT that replaced the Python-side
        sum() loop — verifying it filters on payloads[0].type == 'research'.
        """
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="AutonomyDebug Rate Limit")
        now = _utcnow()

        # Research task within the last hour — should count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=20),
            )
        )
        # Another research task within the last hour — should also count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=10),
            )
        )
        # Non-research ORCHESTRATOR task within the last hour — must NOT count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=15),
            )
        )
        # Research task older than one hour — must NOT count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(hours=2),
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/autonomy-debug")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "rate_limit" in data
        tasks_this_hour = data["rate_limit"]["tasks_this_hour"]
        assert tasks_this_hour == 2, (
            f"Expected 2 research tasks this hour, got {tasks_this_hour}. "
            "Non-research and out-of-window tasks must be excluded."
        )

    @pytest.mark.asyncio
    async def test_autonomy_debug_reports_global_backstop_and_project_pause(
        self, client: AsyncClient, db_session
    ):
        """The debug endpoint mirrors the global backstop and Project.paused guard."""
        import app.shared_state as _shared_state

        project = await _make_project(db_session, name="AutonomyDebug Pause")
        project.paused = True
        await db_session.commit()
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "review", "pr_number": 77}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=10),
            )
        )
        await db_session.commit()

        original_global = _shared_state.runtime_config[
            "max_global_autonomous_tasks_per_hour"
        ]
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 1
        try:
            resp = await client.get(f"/api/admin/projects/{project.id}/autonomy-debug")
        finally:
            _shared_state.runtime_config[
                "max_global_autonomous_tasks_per_hour"
            ] = original_global

        assert resp.status_code == 200, resp.text
        data = resp.json()
        guards = {guard["guard"]: guard for guard in data["blocking_guards"]}
        assert "project_paused" in guards
        assert "global_rate_limit_exceeded" in guards
        assert guards["global_rate_limit_exceeded"]["current"] == 1
        assert guards["global_rate_limit_exceeded"]["limit"] == 1
        assert data["global_rate_limit"]["global_cap"] == 1
        assert data["global_rate_limit"]["global_headroom"] == 0
