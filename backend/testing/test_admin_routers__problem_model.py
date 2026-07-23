# AUTO-SPLIT from test_admin_routers.py — see refactor: decompose test_admin_routers.py
# monolith into focused domain slices. Behavior-preserving: conftest.py fixtures
# (client, db_session, session) are inherited automatically; shared helpers are
# duplicated here to keep this file self-contained.
"""Domain slice: admin_projects.py — problem-model surface, pagination,
running-update ack/edit, and pending-confirmation endpoints.

Split from test_admin_routers.py; no behavior change."""

from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest
from httpx import AsyncClient

from app.models import (
    Project,
    ProjectMember,
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
        resp = await client.get("/api/admin/projects?limit=501")
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

    @pytest.mark.asyncio
    async def test_patch_with_poisoned_problem_model_succeeds(
        self, client: AsyncClient, db_session
    ):
        """PATCH /projects/{id} with a packer-marker key in the stored problem_model
        succeeds — sanitize_problem_model_raw strips unknown keys before parsing."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "intent": {"purpose": "Old.", "current_focus": ""},
            "revision": 1,
            "truncated": True,  # packer marker that should be rejected by pydantic extra=forbid
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}",
            json={"problem_model_patch": {"intent": {"purpose": "New."}}},
        )
        # sanitize_problem_model_raw strips "truncated" so parse_problem_model succeeds
        assert resp.status_code == 200, (
            f"Expected 200 after sanitization, got {resp.status_code}: {resp.text}"
        )


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
    async def test_edit_blind_set_without_base(self, client: AsyncClient, db_session):
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
    async def test_edit_stale_base_conflicts(self, client: AsyncClient, db_session):
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
    async def test_edit_requires_project_access(self, client: AsyncClient, db_session):
        other = Project(name="Other", user_id="other-user")
        db_session.add(other)
        await db_session.commit()

        resp = await client.put(
            f"/api/admin/projects/{other.id}/running-update",
            json={"body": "x"},
        )
        assert resp.status_code == 404


class TestDismissPendingConfirmation:
    """POST /pending-confirm/{id}/dismiss removes a pending_user_confirmation
    item from problem_model and bumps the revision."""

    @pytest.mark.asyncio
    async def test_dismiss_removes_item_and_bumps_revision(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 3,
            "last_updated": "2026-01-01T00:00:00",
            "last_updated_by": "agent",
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [
                {
                    "id": "p-abc12345",
                    "type": "major_decision",
                    "proposal": "Should we migrate to Postgres 16?",
                    "rationale": "Performance improvements.",
                    "created_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "p-def67890",
                    "type": "credentials_needed",
                    "proposal": "Need AWS credentials.",
                    "created_at": "2026-01-02T00:00:00",
                },
            ],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-abc12345/dismiss"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dismissed"
        assert data["pending_id"] == "p-abc12345"

        await db_session.refresh(project)
        pm = project.problem_model
        assert len(pm["pending_user_confirmation"]) == 1
        assert pm["pending_user_confirmation"][0]["id"] == "p-def67890"
        assert pm["revision"] == 4
        assert pm["last_updated_by"] == "user"

    @pytest.mark.asyncio
    async def test_dismiss_unknown_id_returns_404(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 1,
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-notexist/dismiss"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dismiss_requires_project_access(
        self, client: AsyncClient, db_session
    ):
        other = Project(name="Other", user_id="other-user")
        db_session.add(other)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{other.id}/pending-confirm/p-abc12345/dismiss"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dismiss_records_operator_note(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 5,
            "last_updated": "2026-01-01T00:00:00",
            "last_updated_by": "agent",
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [
                {
                    "id": "p-cred001",
                    "type": "credentials_needed",
                    "proposal": "Need AWS credentials for prod account.",
                    "rationale": "Deploy pipeline is blocked.",
                    "created_at": "2026-01-03T00:00:00",
                },
            ],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-cred001/dismiss",
            json={"note": "Credentials added to vault"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dismissed"
        assert data["pending_id"] == "p-cred001"

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        assert pm["revision"] == 6
        assert pm["last_updated_by"] == "user"
        assert len(pm["revision_log"]) == 1
        last_log = pm["revision_log"][-1]
        assert last_log["changes"]["dismissal_note"] == "Credentials added to vault"
        assert last_log["changes"]["dismissed_proposal"] == "Need AWS credentials for prod account."

    @pytest.mark.asyncio
    async def test_dismiss_without_note_no_revision_log_entry(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 2,
            "last_updated": "2026-01-01T00:00:00",
            "last_updated_by": "agent",
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [
                {
                    "id": "p-access001",
                    "type": "access_needed",
                    "proposal": "Need read access to the S3 bucket.",
                    "created_at": "2026-01-03T00:00:00",
                },
            ],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-access001/dismiss"
        )
        assert resp.status_code == 200
        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        assert pm["revision_log"] == []

    @pytest.mark.asyncio
    async def test_dismiss_note_over_5000_chars_returns_422(
        self, client: AsyncClient, db_session
    ):
        """note field longer than 5000 characters must be rejected with 422."""
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 1,
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [
                {
                    "id": "p-abc12345",
                    "type": "major_decision",
                    "proposal": "Should we migrate?",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-abc12345/dismiss",
            json={"note": "x" * 5001},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_dismiss_with_poisoned_problem_model_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Dismiss endpoint strips packer-marker keys before parsing so a poisoned
        row succeeds after sanitize_problem_model_raw strips unknown keys."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 1,
            "pending_user_confirmation": [
                {
                    "id": "p-poison-dismiss",
                    "type": "major_decision",
                    "proposal": "Migrate?",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "truncated": True,  # packer marker
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-poison-dismiss/dismiss"
        )
        # sanitize_problem_model_raw strips "truncated" so parse_problem_model succeeds
        assert resp.status_code == 200, (
            f"Expected 200 after sanitization, got {resp.status_code}: {resp.text}"
        )


class TestAcceptPendingConfirmation:
    """POST /pending-confirm/{id}/accept applies type-specific action and clears the item."""

    _BASE_PM: dict = {
        "revision": 2,
        "last_updated": "2026-01-01T00:00:00",
        "last_updated_by": "agent",
        "intent": {"purpose": "Old purpose", "current_focus": "focus", "user_purpose": "Old purpose"},
        "context": {},
        "constraints": [],
        "capabilities": [],
        "gaps": [],
        "archived_gaps": [],
        "friction": [],
        "revision_log": [],
    }

    @pytest.mark.asyncio
    async def test_accept_intent_change_updates_purpose(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            **self._BASE_PM,
            "pending_user_confirmation": [
                {
                    "id": "p-ic001",
                    "type": "intent_change",
                    "proposal": "New project purpose text",
                    "rationale": "Direction shifted.",
                    "created_at": "2026-01-01T00:00:00",
                },
            ],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-ic001/accept",
            json={"answer": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["type"] == "intent_change"

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        assert pm["intent"]["purpose"] == "New project purpose text"
        assert pm["revision"] == 3
        assert pm["last_updated_by"] == "user"

    @pytest.mark.asyncio
    async def test_accept_major_decision_records_answer(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            **self._BASE_PM,
            "pending_user_confirmation": [
                {
                    "id": "p-md001",
                    "type": "major_decision",
                    "proposal": "Should we migrate to Postgres 16?",
                    "created_at": "2026-01-01T00:00:00",
                },
            ],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-md001/accept",
            json={"answer": "Yes, proceed with migration."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        # Answer is recorded in the revision log
        last_log = pm["revision_log"][-1]
        assert last_log["changes"]["decision_answer"] == "Yes, proceed with migration."
        assert last_log["changes"]["decision_question"] == "Should we migrate to Postgres 16?"

    @pytest.mark.asyncio
    async def test_accept_credentials_needed_records_answer(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            **self._BASE_PM,
            "pending_user_confirmation": [
                {
                    "id": "p-cred002",
                    "type": "credentials_needed",
                    "proposal": "Need DB password for prod.",
                    "created_at": "2026-01-04T00:00:00",
                },
            ],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-cred002/accept",
            json={"answer": "Password added to secrets manager."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["type"] == "credentials_needed"

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        last_log = pm["revision_log"][-1]
        assert last_log["changes"]["decision_answer"] == "Password added to secrets manager."
        assert last_log["changes"]["decision_question"] == "Need DB password for prod."

    @pytest.mark.asyncio
    async def test_accept_intent_change_with_note_records_revision_log(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {
            **self._BASE_PM,
            "pending_user_confirmation": [
                {
                    "id": "p-ic002",
                    "type": "intent_change",
                    "proposal": "Expand scope to include mobile.",
                    "created_at": "2026-01-04T00:00:00",
                },
            ],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-ic002/accept",
            json={"answer": "Agreed — prioritize Android first."},
        )
        assert resp.status_code == 200
        assert resp.json()["type"] == "intent_change"

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["pending_user_confirmation"] == []
        assert pm["intent"]["purpose"] == "Expand scope to include mobile."
        last_log = pm["revision_log"][-1]
        assert last_log["changes"]["operator_note"] == "Agreed — prioritize Android first."
        assert last_log["changes"]["accepted_proposal"] == "Expand scope to include mobile."

    @pytest.mark.asyncio
    async def test_accept_unknown_id_returns_404(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        project.problem_model = {**self._BASE_PM, "pending_user_confirmation": []}
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-notexist/accept",
            json={"answer": None},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_accept_answer_over_5000_chars_returns_422(
        self, client: AsyncClient, db_session
    ):
        """answer field longer than 5000 characters must be rejected with 422."""
        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 1,
            "intent": {"purpose": "", "current_focus": "", "user_purpose": ""},
            "context": {},
            "constraints": [],
            "capabilities": [],
            "gaps": [],
            "archived_gaps": [],
            "friction": [],
            "pending_user_confirmation": [
                {
                    "id": "p-dec001",
                    "type": "major_decision",
                    "proposal": "Go with option A?",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "revision_log": [],
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-dec001/accept",
            json={"answer": "y" * 5001},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_accept_with_poisoned_problem_model_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Accept endpoint strips packer-marker keys before parsing so a poisoned
        row succeeds — sanitize_problem_model_raw removes unknown keys first."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.problem_model = {
            "revision": 2,
            "pending_user_confirmation": [
                {
                    "id": "p-poison-accept",
                    "type": "major_decision",
                    "proposal": "Go with option A?",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "truncated": True,  # packer marker
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/pending-confirm/p-poison-accept/accept",
            json={"answer": "confirmed"},
        )
        # sanitize_problem_model_raw strips "truncated" so parse_problem_model succeeds
        assert resp.status_code == 200, (
            f"Expected 200 after sanitization, got {resp.status_code}: {resp.text}"
        )


class TestPendingConfirmationLockedRMW:
    """Regression: accept/dismiss must hold the project lock before reading
    problem_model to prevent a lost-update race against the strategist."""

    _PENDING_PM: dict = {
        "revision": 1,
        "last_updated": "2026-01-01T00:00:00",
        "last_updated_by": "agent",
        "intent": {"purpose": "p", "current_focus": "", "user_purpose": "p"},
        "context": {},
        "constraints": [],
        "capabilities": [],
        "gaps": [],
        "archived_gaps": [],
        "friction": [],
        "pending_user_confirmation": [
            {
                "id": "p-lock001",
                "type": "major_decision",
                "proposal": "Lock test proposal",
                "created_at": "2026-01-01T00:00:00",
            }
        ],
        "revision_log": [],
    }

    @pytest.mark.asyncio
    async def test_pending_confirmation_accept_dismiss_locked_rmw(
        self, client: AsyncClient, db_session
    ):
        """Both dismiss and accept acquire lock_project_or_429 before reading
        problem_model so concurrent writes are not lost.

        We verify this by spying on lock_project_or_429 (delegating to the real
        implementation so the transaction is valid) and asserting it is called
        with the expected project id.  The test also exercises the real mutation
        path and verifies the 200 response and correct problem_model state.
        """
        import copy
        from unittest.mock import patch

        from app.routers import admin_projects as _mod

        orig_lock = _mod.lock_project_or_429

        # ---- dismiss path ----
        project_d = await _make_project(db_session)
        project_d.problem_model = copy.deepcopy(self._PENDING_PM)
        db_session.add(project_d)
        await db_session.commit()

        lock_calls_d: list = []

        async def spy_lock_d(session, project_id):
            lock_calls_d.append(project_id)
            return await orig_lock(session, project_id)

        with patch.object(_mod, "lock_project_or_429", side_effect=spy_lock_d):
            resp = await client.post(
                f"/api/admin/projects/{project_d.id}/pending-confirm/p-lock001/dismiss"
            )

        assert resp.status_code == 200, resp.text
        assert project_d.id in lock_calls_d, (
            f"dismiss did not call lock_project_or_429 with project id; calls={lock_calls_d}"
        )
        await db_session.refresh(project_d)
        assert project_d.problem_model["pending_user_confirmation"] == [], (
            "dismiss did not remove the pending item from problem_model"
        )

        # ---- accept path ----
        project_a = await _make_project(db_session)
        project_a.problem_model = copy.deepcopy(self._PENDING_PM)
        db_session.add(project_a)
        await db_session.commit()

        lock_calls_a: list = []

        async def spy_lock_a(session, project_id):
            lock_calls_a.append(project_id)
            return await orig_lock(session, project_id)

        with patch.object(_mod, "lock_project_or_429", side_effect=spy_lock_a):
            resp = await client.post(
                f"/api/admin/projects/{project_a.id}/pending-confirm/p-lock001/accept",
                json={"answer": "confirmed"},
            )

        assert resp.status_code == 200, resp.text
        assert project_a.id in lock_calls_a, (
            f"accept did not call lock_project_or_429 with project id; calls={lock_calls_a}"
        )
        await db_session.refresh(project_a)
        assert project_a.problem_model["pending_user_confirmation"] == [], (
            "accept did not remove the pending item from problem_model"
        )
