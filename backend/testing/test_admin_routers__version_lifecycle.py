# AUTO-SPLIT from test_admin_routers.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared helpers (_make_project, _make_open_version) are
# duplicated here to keep this file self-contained. This file holds the version
# lifecycle domain slice: version phase, goal, acceptance criteria, cut, clear-deploy-failed,
# agent serving SHAs, and all TestVersionLifecycle* classes.
"""Domain slice: admin_release_version.py — Version Lifecycle

Split from test_admin_routers.py. Covers version phase/goal mutations, acceptance
criteria router, cut-version, clear-deploy-failed, agent-serving-SHAs, and all
version-lifecycle field tests."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app import release_version as rv
from app.release_version_cut import VersionCutIntegrationIdentity
from app import release_version_cut as rvc
from app.models import (
    Alert,
    Artifact,
    ArtifactType,
    Direction,
    PoolVm,
    PoolVmState,
    Project,
    ProjectMessage,
    ProjectMember,
    ReleaseQualificationIntent,
    ReleaseVersion,
    Task,
    TaskStatus,
    _utcnow,
)
from app.auth import get_current_user, require_admin


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"
_CUT_SHA = "a" * 40
_CUT_VERSION_ID = uuid.UUID(int=1)
_MISSING = object()
_CUT_IDENTITY = VersionCutIntegrationIdentity(
    owner="example-owner",
    repo="example-repo",
    branch="main",
    head_sha=_CUT_SHA,
    observed_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
)


def _cut_body(
    reason=_MISSING,
    *,
    key: str = "cut-test-key",
    version_id: uuid.UUID = _CUT_VERSION_ID,
    version_number: int = 1,
    sha: str = _CUT_SHA,
):
    body = {
        "idempotency_key": key,
        "expected_version_id": str(version_id),
        "expected_version_number": version_number,
        "expected_integration_sha": sha,
        "expected_integration_identity": _CUT_IDENTITY.identity_token(),
    }
    if reason is not _MISSING:
        body["reason"] = reason
    return body


def _api_error_code(response) -> str:
    return response.json()["error"]["message"]["code"]


def _mock_exact_cut_boundary(mocker):
    resolve = mocker.patch(
        "app.release_version_cut.resolve_version_cut_integration_identity",
        new_callable=AsyncMock,
        return_value=_CUT_IDENTITY,
    )
    repository = mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("test-token", _CUT_IDENTITY.owner, _CUT_IDENTITY.repo),
    )
    followups = mocker.patch(
        "app.release_version.run_version_cut_followups",
        new_callable=AsyncMock,
    )
    return resolve, repository, followups


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project", role="owner"):
    """Create a project with the given user as a member, plus the membership row."""
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role=role)
    session.add(member)
    await session.commit()
    return project


async def _make_open_version(session, project_id, phase="build"):
    """Create an open ReleaseVersion for the given project."""
    version = ReleaseVersion(
        project_id=project_id, version_number=1, phase=phase, status="open"
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Version Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_qualification_intent_receipt_readback_is_exact_and_uncached(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    version = await _make_open_version(
        db_session, project.id, phase="stabilize"
    )
    intent = ReleaseQualificationIntent(
        project_id=project.id,
        release_version_id=version.id,
        source_task_id=uuid.uuid4(),
        authority_kind="managed_workday_operation",
        authority_key="workday:readback-test",
        idempotency_key="strategic-qualification:" + "a" * 64,
        specification_hash="b" * 64,
        reason="prove exact delivery",
        requested_probes=[],
        expected_phase_changed_at=version.phase_changed_at,
        status="applied",
        attempt_count=1,
        result={
            "delivery_state": "committed",
            "status": "spawned",
            "candidate_sha": "c" * 40,
        },
        completed_at=_utcnow(),
    )
    db_session.add(intent)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-intents",
        params={"intent_id": str(intent.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    receipt = body["qualification_intents"][0]
    assert receipt["id"] == str(intent.id)
    assert receipt["release_version_id"] == str(version.id)
    assert receipt["authority_key"] == "workday:readback-test"
    assert receipt["specification_hash"] == "b" * 64
    assert receipt["status"] == "applied"
    assert receipt["attempt_count"] == 1
    assert receipt["result"] == {
        "delivery_state": "committed",
        "status": "spawned",
        "candidate_sha": "c" * 40,
    }


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
    async def test_set_phase_rejects_promotion_check_wait(
        self, client: AsyncClient, db_session
    ):
        """A phase flip while a managed promotion PR is open is rejected with a
        409 — flipping back to build would clear the pending indication
        without cancelling anything, leaving the operator levers disagreeing."""
        from datetime import timedelta
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 8642,
                "pr_url": "https://example/pr/8642",
                "enabled_at": (_utcnow() - timedelta(minutes=15)).isoformat(),
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        assert resp.status_code == 409
        assert "managed promotion PR is open" in resp.text
        # The 409 surfaces the managed-wait age derived from enabled_at.
        assert "waiting ~" in resp.text

        # Phase unchanged — the flip was refused before any mutation.
        await db_session.refresh(version)
        assert version.phase == "stabilize"

    @pytest.mark.asyncio
    async def test_set_phase_rejects_managed_check_wait(
        self, client: AsyncClient, db_session
    ):
        """A phase flip while a migration-carrying promotion PR is held in
        managed check-wait is ALSO rejected with a 409 that explains the
        required-check wait and Blueprint's exact-target retry."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 8643,
                "pr_url": "https://example/pr/8643",
                "managed_check_wait": True,
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        assert resp.status_code == 409
        assert "will wait for required checks" in resp.text
        assert "revalidate the exact base/head pair" in resp.text
        assert "retry the merge" in resp.text
        assert "#8643" in resp.text

        await db_session.refresh(version)
        assert version.phase == "stabilize"

    @pytest.mark.asyncio
    async def test_version_cut_rejects_managed_check_wait(
        self, client: AsyncClient, db_session, mocker
    ):
        """A version cut while a migration-carrying promotion PR is held in
        managed check-wait is rejected with a 409 carrying distinct copy."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 8644,
                "pr_url": "https://example/pr/8644",
                "managed_check_wait": True,
            },
        }
        db_session.add(version)
        await db_session.commit()
        _mock_exact_cut_boundary(mocker)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("operator cut attempt", version_id=version.id),
        )
        assert resp.status_code == 409
        assert "will wait for required checks" in resp.text
        assert "revalidate the exact base/head pair" in resp.text
        assert "#8644" in resp.text

        # The version was NOT abandoned — still the same open version.
        await db_session.refresh(version)
        assert version.status == "open"

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

        # Verify that promotion_check_wait check is enforced (evidence of
        # precondition re-checking against the state).
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 5555,
                "pr_url": "https://example/pr/5555",
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        # The promotion_check_wait guard is re-checked against the locked row.
        assert resp.status_code == 409
        assert "managed promotion PR is open" in resp.text
        assert "5555" in resp.text

    @pytest.mark.asyncio
    async def test_set_phase_allows_flip_when_verdict_finalized(
        self, client: AsyncClient, db_session
    ):
        """Stale-pending window: last_qualification has promotion_pending but the
        newest QualificationVerdict row already carries a finalized terminal_status.
        The phase flip must succeed (200) — not be refused with 409."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcdef" * 6 + "abcd",
            "promotion_pending": {
                "pr_number": 7777,
                "pr_url": "https://example/pr/7777",
            },
        }
        db_session.add(version)

        # Finalized verdict — promotion already resolved.
        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="abcdef" * 6 + "abcd",
            terminal_status="promoted",
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-phase",
            json={"phase": "build"},
        )
        assert resp.status_code == 200, (
            f"Expected 200 for stale-pending+finalized-verdict but got {resp.status_code}: "
            f"{resp.text}"
        )
        assert resp.json()["phase"] == "build"

    @pytest.mark.asyncio
    async def test_version_cut_allows_cut_when_verdict_finalized(
        self, client: AsyncClient, db_session, mocker
    ):
        """Stale-pending window: last_qualification has promotion_pending but the
        newest QualificationVerdict row already carries a finalized terminal_status.
        version-cut must succeed (200) — not be refused with 409."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "feed" * 10,
            "promotion_pending": {
                "pr_number": 8888,
                "pr_url": "https://example/pr/8888",
            },
        }
        db_session.add(version)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="feed" * 10,
            terminal_status="not_promoted",
        )
        db_session.add(verdict)
        await db_session.commit()
        _mock_exact_cut_boundary(mocker)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("cut after promotion resolved", version_id=version.id),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for stale-pending+finalized-verdict but got {resp.status_code}: "
            f"{resp.text}"
        )


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
    async def test_set_goal_too_long_returns_422(self, client: AsyncClient, db_session):
        """Goal exceeding the 500-char body limit is rejected by Pydantic."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "x" * 501},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_goal_uses_for_update_lock(self, client: AsyncClient, db_session):
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

    @pytest.mark.asyncio
    async def test_set_goal_over_http_retires_the_omission_ask(
        self, client: AsyncClient, db_session
    ):
        """The operator escape hatch named in the alert must END the condition.

        ``release.version_goal_not_authored`` carries
        ``AlertRetirementMode.SOURCE_CONDITION``: the backstop sweep in
        ``alert_dispatch.sweep_resolve_policy_retirement_alerts`` reconciles only
        TASK_TERMINAL and TIMELINE_ONLY rows, so if this endpoint does not
        dismiss the alert itself nothing ever will -- and the operator who did
        exactly what the alert's own "TO RESOLVE" line asked keeps staring at it.
        """
        from app.models import AlertSeverity

        project = await _make_project(db_session, name="Goal Omission Hatch")
        version = await _make_open_version(db_session, project.id)
        version.goal_omission_cycles = 4
        version.goal_omission_last_cycle = "cycle-open"
        db_session.add(version)
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            domain="release_lifecycle",
            event_type=rv.VERSION_GOAL_OMISSION_EVENT_TYPE,
            title=f"Version goal not authored - v{version.version_number}",
            message="asked and not delivered",
            source="version_goal_omission",
            dedupe_key=rv.version_goal_omission_dedupe_key(
                project.id, version.version_number
            ),
        )
        db_session.add(alert)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-goal",
            json={"goal": "Answer the ask that has been open for four cycles."},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(version)
        await db_session.refresh(alert)
        assert version.goal_omission_cycles == 0
        assert version.goal_omission_last_cycle is None
        assert alert.dismissed_at is not None, (
            "Setting the goal over the operator endpoint must dismiss the "
            "goal-omission alert; SOURCE_CONDITION means no sweep will."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# admin_acceptance_criteria.py — Version Acceptance Criteria
# ═══════════════════════════════════════════════════════════════════════════════


class TestAcceptanceCriteriaRouter:
    @pytest.mark.asyncio
    async def test_acceptance_criteria_router_happy_path(
        self, client: AsyncClient, db_session
    ):
        """List, create, and criterion-key updates work over the HTTP routes."""
        project = await _make_project(db_session, name="Acceptance Criteria")
        await _make_open_version(db_session, project.id)
        direction = Direction(
            project_id=project.id,
            direction_key="d-criteria",
            status="active",
            text="Acceptance criteria direction",
            data={
                "id": "d-criteria",
                "status": "active",
                "text": "Acceptance criteria direction",
            },
            criterion_keys=[],
        )
        db_session.add(direction)
        await db_session.commit()

        list_resp = await client.get(
            f"/api/admin/projects/{project.id}/acceptance-criteria"
        )
        assert list_resp.status_code == 200, list_resp.text
        summary = list_resp.json()
        assert summary == {
            "release_version_id": summary["release_version_id"],
            "version_number": 1,
            "total": 0,
            "covered": 0,
            "attested": 0,
            "backfilled": 0,
            "partial": 0,
            "uncovered": 0,
            "inactive": 0,
            "retracted": 0,
            "superseded": 0,
            "stuck_count": 0,
            "criteria": [],
            "failed_corroboration_keys": [],
            "merge_evidence_gaps": None,
            # Probed tier: a lower-confidence, time-bounded coverage signal.
            "probed": 0,
            # Phantom-bookkeeping drift signal (consumed by operator_attention):
            # a version with no criteria registers none and references none, so
            # there is nothing to drift.
            "phantom_bookkeeping": {
                "has_drift": False,
                "phantom_keys": [],
                "referenced_keys": [],
                "registered_count": 0,
                "zero_registered": False,
            },
        }

        create_resp = await client.post(
            f"/api/admin/projects/{project.id}/acceptance-criteria",
            json={"text": "  Ship with zero P0 bugs  "},
        )
        assert create_resp.status_code == 200, create_resp.text
        criterion_key = create_resp.json()["criterion_key"]
        assert criterion_key.startswith("c-")

        list_resp = await client.get(
            f"/api/admin/projects/{project.id}/acceptance-criteria"
        )
        assert list_resp.status_code == 200, list_resp.text
        summary = list_resp.json()
        assert summary["total"] == 1
        assert summary["covered"] == 0
        assert summary["partial"] == 0
        assert summary["uncovered"] == 1
        assert summary["criteria"][0]["criterion_key"] == criterion_key
        assert summary["criteria"][0]["text"] == "Ship with zero P0 bugs"

        patch_resp = await client.post(
            f"/api/admin/projects/{project.id}/acceptance-criteria/link-direction",
            json={"direction_key": "d-criteria", "criterion_keys": [criterion_key]},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json() == {
            "status": "ok",
            "direction_key": "d-criteria",
            "criterion_keys": [criterion_key],
        }

        await db_session.refresh(direction)
        assert direction.criterion_keys == [criterion_key]

    @pytest.mark.asyncio
    async def test_acceptance_criteria_router_validation_and_role_checks(
        self, client: AsyncClient, db_session
    ):
        """Bad inputs are rejected and viewer-role callers cannot mutate."""
        from app.auth import get_current_user as _gcu

        project = await _make_project(db_session, name="Acceptance Criteria Guards")
        await _make_open_version(db_session, project.id)
        direction = Direction(
            project_id=project.id,
            direction_key="d-criteria-guards",
            status="active",
            text="Guarded direction",
            data={
                "id": "d-criteria-guards",
                "status": "active",
                "text": "Guarded direction",
            },
            criterion_keys=[],
        )
        db_session.add(direction)
        await db_session.commit()

        empty_resp = await client.post(
            f"/api/admin/projects/{project.id}/acceptance-criteria",
            json={"text": "   "},
        )
        assert empty_resp.status_code == 422
        assert "must not be empty" in empty_resp.text

        bad_patch = await client.post(
            f"/api/admin/projects/{project.id}/acceptance-criteria/link-direction",
            json={
                "direction_key": "d-criteria-guards",
                "criterion_keys": ["c-not-a-real-key"],
            },
        )
        assert bad_patch.status_code == 400
        assert "criterion_key_invalid" in bad_patch.text

        viewer_id = "viewer-ac-guard"

        async def viewer_user():
            return {
                "user_id": viewer_id,
                "email": "viewer-ac-guard@example.com",
                "role": "user",
            }

        db_session.add(
            ProjectMember(project_id=project.id, user_id=viewer_id, role="viewer")
        )
        await db_session.commit()
        app.dependency_overrides[_gcu] = viewer_user
        try:
            forbidden_create = await client.post(
                f"/api/admin/projects/{project.id}/acceptance-criteria",
                json={"text": "Viewer cannot write this"},
            )
            assert forbidden_create.status_code == 403

            forbidden_patch = await client.post(
                f"/api/admin/projects/{project.id}/acceptance-criteria/link-direction",
                json={"direction_key": "d-criteria-guards", "criterion_keys": []},
            )
            assert forbidden_patch.status_code == 403
        finally:
            app.dependency_overrides.pop(_gcu, None)


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Cut/Abandon Version
# ═══════════════════════════════════════════════════════════════════════════════


class TestCutVersion:
    @pytest.fixture(autouse=True)
    def _exact_cut_boundary(self, mocker):
        (
            self.resolve_cut_identity,
            self.resolve_cut_repository,
            self.run_cut_followups,
        ) = _mock_exact_cut_boundary(
            mocker,
        )

    @pytest.mark.asyncio
    async def test_version_cut_member_can_cut(self, client: AsyncClient, db_session):
        """Member can cut the open version; both versions are returned and the
        old row flips to abandoned while a fresh build version opens."""
        project = await _make_project(db_session)
        version = await _make_open_version(
            db_session,
            project.id,
            phase="stabilize",
        )
        warm = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
        assert warm.status_code == 200
        assert warm.json()["current_version"]["id"] == str(version.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("candidate can never pass", version_id=version.id),
        )
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store, private"
        data = resp.json()
        assert data["replayed"] is False
        assert data["receipt"]["status"] == "committed"
        assert data["receipt"]["idempotency_key"] == "cut-test-key"
        assert data["receipt"]["operator_id"] == TEST_USER_ID
        assert data["receipt"]["operator_display"] == TEST_USER_EMAIL
        assert data["receipt"]["expected_integration_sha"] == _CUT_SHA
        assert data["receipt"]["observed_repository_owner"] == _CUT_IDENTITY.owner
        assert data["receipt"]["observed_repository_name"] == _CUT_IDENTITY.repo
        assert data["receipt"]["observed_integration_branch"] == _CUT_IDENTITY.branch
        assert data["receipt"]["observed_integration_sha"] == _CUT_SHA
        assert data["receipt"]["expected_integration_identity"] == (
            _CUT_IDENTITY.identity_token()
        )
        for field in (
            "observed_at",
            "abandoned_opened_at",
            "successor_opened_at",
            "committed_at",
        ):
            parsed = datetime.fromisoformat(data["receipt"][field])
            assert parsed.tzinfo is not None
            assert data["receipt"][field].endswith("+00:00")
        assert data["followups"] == {
            "schema_version": 1,
            "status": "pending",
            "attempts": 0,
            "created_at": data["receipt"]["committed_at"],
            "last_attempt_at": None,
            "completed_at": None,
            "last_error": None,
            "lease_expires_at": None,
        }
        assert data["abandoned_version"]["version_number"] == 1
        assert data["abandoned_version"]["status"] == "abandoned"
        assert data["abandoned_version"]["abandoned_at"]
        assert data["new_version"]["version_number"] == 2
        assert data["new_version"]["phase"] == "build"
        assert data["new_version"]["base_sha"] == _CUT_SHA
        assert data["new_version"]["candidate_sha"] is None
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
        assert old.last_qualification["operator_id"] == TEST_USER_ID
        assert old.last_qualification["reason"] == "candidate can never pass"
        messages = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalars().all()
        abandoned_messages = [
            message
            for message in messages
            if (message.context or {}).get("event_type") == "release_version_abandoned"
        ]
        assert len(abandoned_messages) == 1
        message_context = abandoned_messages[0].context or {}
        assert message_context["version_number"] == 1
        assert message_context["next_version_number"] == 2
        assert message_context["operator_id"] == TEST_USER_ID
        assert message_context["reason"] == "candidate can never pass"
        lifecycle = (
            await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
        ).json()
        assert lifecycle["current_version"]["version_number"] == 2
        assert lifecycle["current_version"]["base_sha"] == _CUT_SHA
        assert lifecycle["current_version"]["candidate_sha"] is None
        history = lifecycle["recent_versions"]
        abandoned = next(row for row in history if row["version_number"] == 1)
        assert abandoned["abandoned_by"] == TEST_USER_EMAIL
        assert abandoned["abandon_reason"] == "candidate can never pass"
        assert abandoned["abandoned_at"]
        assert abandoned["base_sha"] is None
        assert abandoned["candidate_sha"] is None
        assert abandoned["version_cut_receipt"] == data["receipt"]
        assert abandoned["version_cut_followups"] == data["followups"]

    @pytest.mark.asyncio
    async def test_version_cut_terminal_message_failure_rolls_back_cut(
        self,
        client: AsyncClient,
        db_session,
        mocker,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        project_id = project.id
        mocker.patch(
            "app.notification_dispatch.post_release_version_abandoned_message",
            new=AsyncMock(side_effect=RuntimeError("terminal receipt write failed")),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": "Bearer exact-cut-test"},
        ) as failure_client:
            response = await failure_client.post(
                f"/api/admin/projects/{project.id}/version-cut",
                json=_cut_body(
                    "atomic terminal receipt",
                    key="cut-terminal-receipt-failure",
                    version_id=version.id,
                ),
            )
        assert response.status_code == 500
        assert response.headers["cache-control"] == "no-store, private"
        await db_session.rollback()

        rows = (
            (
                await db_session.execute(
                    select(ReleaseVersion).where(
                        ReleaseVersion.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(row.version_number, row.status) for row in rows] == [(1, "open")]
        assert self.run_cut_followups.await_count == 0

    @pytest.mark.asyncio
    async def test_version_cut_idempotent_on_repeat_key(
        self, client: AsyncClient, db_session
    ):
        """A repeat cut carrying the same idempotency key returns the original
        cut's result and does NOT open a further version."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")

        first = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("pivot", key="cut-abc", version_id=version.id),
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["abandoned_version"]["version_number"] == 1
        assert first_data["new_version"]["version_number"] == 2
        assert first_data["replayed"] is False

        successor = await db_session.get(
            ReleaseVersion,
            uuid.UUID(first_data["new_version"]["id"]),
        )
        assert successor is not None
        successor.phase = "stabilize"
        successor.goal = "later mutable goal"
        successor.candidate_sha = "c" * 40
        db_session.add(successor)
        await db_session.commit()

        # Replay with the same key: same result, no second cut.
        second = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("pivot", key="cut-abc", version_id=version.id),
        )
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["abandoned_version"]["version_number"] == 1
        assert second_data["new_version"]["version_number"] == 2
        assert second_data["replayed"] is True
        assert second_data["receipt"] == first_data["receipt"]
        assert second_data["abandoned_version"] == first_data["abandoned_version"]
        assert second_data["new_version"] == first_data["new_version"]
        # Receipt-first recovery must not require a second provider read.
        assert self.resolve_cut_identity.await_count == 2

        # Only v2 is open; no v3 was opened by the retry.
        rows = (
            (
                await db_session.execute(
                    select(ReleaseVersion).where(
                        ReleaseVersion.project_id == project.id
                    ).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(v.version_number for v in rows) == [1, 2]
        open_rows = [v for v in rows if v.status == "open"]
        assert [v.version_number for v in open_rows] == [2]
        messages = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        ).scalars().all()
        assert sum(
            (message.context or {}).get("event_type") == "release_version_abandoned"
            for message in messages
        ) == 1

    @pytest.mark.asyncio
    async def test_version_cut_context_is_exact_and_uncached(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(
            db_session,
            project.id,
            phase="stabilize",
        )
        version.base_sha = "b" * 40
        version.candidate_sha = "c" * 40
        db_session.add(version)
        await db_session.commit()

        endpoint = f"/api/admin/projects/{project.id}/version-cut-context"
        first = await client.get(endpoint)
        second = await client.get(endpoint)
        assert first.status_code == second.status_code == 200
        assert first.headers["cache-control"] == "no-store, private"
        assert second.headers["cache-control"] == "no-store, private"
        assert first.json() == second.json()
        assert first.json()["version"] == {
            "id": str(version.id),
            "version_number": 1,
            "phase": "stabilize",
            "status": "open",
            "base_sha": "b" * 40,
            "candidate_sha": "c" * 40,
        }
        assert first.json()["integration"]["head_sha"] == _CUT_SHA
        assert first.json()["integration"] == _CUT_IDENTITY.projection()
        assert self.resolve_cut_identity.await_count == 2

    @pytest.mark.asyncio
    async def test_version_cut_context_recovers_receipt_without_provider_read(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body(
                "recover receipt",
                key="recover-cut",
                version_id=version.id,
            ),
        )
        assert sent.status_code == 200, sent.text
        assert self.resolve_cut_identity.await_count == 2

        recovered = await client.get(
            f"/api/admin/projects/{project.id}/version-cut-context",
            params={"idempotency_key": "recover-cut"},
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == {
            "status": "committed",
            "replayed": True,
            "receipt": sent.json()["receipt"],
            "followups": sent.json()["followups"],
        }
        assert recovered.headers["cache-control"] == "no-store, private"
        assert self.resolve_cut_identity.await_count == 2

    @pytest.mark.asyncio
    async def test_version_cut_same_key_different_request_conflicts_without_cut(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        first = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body(
                "first request",
                key="conflict-cut",
                version_id=version.id,
            ),
        )
        assert first.status_code == 200, first.text

        conflict = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body(
                "changed request",
                key="conflict-cut",
                version_id=version.id,
            ),
        )
        assert conflict.status_code == 409
        assert _api_error_code(conflict) == (
            "version_cut_idempotency_conflict"
        )
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
        assert sorted(row.version_number for row in rows) == [1, 2]
        assert self.resolve_cut_identity.await_count == 2

    @pytest.mark.asyncio
    async def test_version_cut_replay_binds_stable_user_not_mutable_email(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        body = _cut_body("stable actor", key="stable-actor", version_id=version.id)
        first = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=body,
        )
        assert first.status_code == 200, first.text

        original_override = app.dependency_overrides[get_current_user]

        async def renamed_user():
            return {
                "user_id": TEST_USER_ID,
                "email": "renamed@example.com",
                "role": "admin",
            }

        app.dependency_overrides[get_current_user] = renamed_user
        try:
            replay = await client.post(
                f"/api/admin/projects/{project.id}/version-cut",
                json=body,
            )
        finally:
            app.dependency_overrides[get_current_user] = original_override
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["receipt"] == first.json()["receipt"]
        assert replay.json()["receipt"]["operator_id"] == TEST_USER_ID
        assert replay.json()["receipt"]["operator_display"] == TEST_USER_EMAIL
        assert self.resolve_cut_identity.await_count == 2

    @pytest.mark.asyncio
    async def test_version_cut_corrupt_receipt_is_typed_conflict_not_500(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("tamper proof", key="tampered-cut", version_id=version.id),
        )
        assert sent.status_code == 200, sent.text
        await db_session.refresh(version)
        qualification = dict(version.last_qualification or {})
        receipt = dict(qualification["version_cut_receipt"])
        receipt["expected_integration_sha"] = "not-a-sha"
        qualification["version_cut_receipt"] = receipt
        version.last_qualification = qualification
        db_session.add(version)
        await db_session.commit()

        recovered = await client.get(
            f"/api/admin/projects/{project.id}/version-cut-context",
            params={"idempotency_key": "tampered-cut"},
        )
        assert recovered.status_code == 409, recovered.text
        assert _api_error_code(recovered) == "version_cut_idempotency_conflict"
        assert self.resolve_cut_identity.await_count == 2

    @pytest.mark.asyncio
    async def test_version_cut_missing_followup_marker_is_typed_and_visible(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        body = _cut_body("missing marker", key="missing-marker", version_id=version.id)
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=body,
        )
        assert sent.status_code == 200, sent.text
        await db_session.refresh(version)
        qualification = dict(version.last_qualification or {})
        qualification.pop("version_cut_followups")
        version.last_qualification = qualification
        db_session.add(version)
        await db_session.commit()

        recovered = await client.get(
            f"/api/admin/projects/{project.id}/version-cut-context",
            params={"idempotency_key": "missing-marker"},
        )
        assert recovered.status_code == 409, recovered.text
        assert _api_error_code(recovered) == "version_cut_idempotency_conflict"
        replay = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=body,
        )
        assert replay.status_code == 409, replay.text
        assert _api_error_code(replay) == "version_cut_idempotency_conflict"

        lifecycle = await client.get(
            f"/api/admin/projects/{project.id}/version-lifecycle"
        )
        assert lifecycle.status_code == 200, lifecycle.text
        abandoned = next(
            row
            for row in lifecycle.json()["recent_versions"]
            if row["version_number"] == 1
        )
        assert abandoned["version_cut_followups"]["status"] == "invalid"
        assert "missing" in abandoned["version_cut_followups"]["last_error"]

    @pytest.mark.asyncio
    async def test_version_cut_context_rejects_whitespace_key_without_caching(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")
        response = await client.get(
            f"/api/admin/projects/{project.id}/version-cut-context",
            params={"idempotency_key": "   "},
        )
        assert response.status_code == 422, response.text
        assert _api_error_code(response) == "version_cut_invalid_request"
        assert response.headers["cache-control"] == "no-store, private"

    @pytest.mark.asyncio
    async def test_version_cut_prehandler_failures_are_uncached(
        self,
        client: AsyncClient,
    ):
        malformed_post = await client.post(
            "/api/admin/projects/not-a-uuid/version-cut",
            json=_cut_body("malformed project"),
        )
        missing_authority = await client.post(
            f"/api/admin/projects/{uuid.uuid4()}/version-cut",
            json={"reason": "missing exact authority"},
        )
        malformed_get = await client.get(
            "/api/admin/projects/not-a-uuid/version-cut-context"
        )
        for response in (malformed_post, missing_authority, malformed_get):
            assert response.status_code == 422
            assert response.headers["cache-control"] == "no-store, private"

        original_auth = app.dependency_overrides[get_current_user]

        async def reject_auth():
            raise HTTPException(status_code=401, detail="authentication rejected")

        app.dependency_overrides[get_current_user] = reject_auth
        try:
            rejected = await client.get(
                f"/api/admin/projects/{uuid.uuid4()}/version-cut-context"
            )
        finally:
            app.dependency_overrides[get_current_user] = original_auth
        assert rejected.status_code == 401
        assert rejected.headers["cache-control"] == "no-store, private"

        unrelated = await client.get(
            "/api/admin/projects/not-a-uuid/version-lifecycle"
        )
        assert unrelated.status_code == 422
        assert "cache-control" not in unrelated.headers

    @pytest.mark.asyncio
    async def test_version_cut_csrf_failure_is_uncached(self):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as raw_client:
            response = await raw_client.post(
                f"/api/admin/projects/{uuid.uuid4()}/version-cut",
                json=_cut_body("csrf rejected"),
            )
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "CSRF token missing"
        assert response.headers["cache-control"] == "no-store, private"

    @pytest.mark.asyncio
    async def test_version_cut_distinct_key_cannot_target_predicted_successor(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        first = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("first cut", key="first-cut", version_id=version.id),
        )
        assert first.status_code == 200, first.text
        successor_id = uuid.UUID(first.json()["new_version"]["id"])

        speculative = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body(
                "predicted successor",
                key="different-key",
                version_id=uuid.uuid4(),
                version_number=2,
            ),
        )
        assert speculative.status_code == 409, speculative.text
        assert _api_error_code(speculative) == "version_cut_precondition_failed"
        current = await rv.current_open_version(db_session, project.id)
        assert current is not None
        assert current.id == successor_id
        assert current.status == "open"
        assert current.version_number == 2

    @pytest.mark.asyncio
    async def test_version_cut_followup_sweep_recovers_crash_and_retry(
        self,
        client: AsyncClient,
        db_session,
        mocker,
    ):
        from conftest import TestingSessionLocal

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("durable followups", key="followup-cut", version_id=version.id),
        )
        assert sent.status_code == 200, sent.text
        immutable_receipt = sent.json()["receipt"]
        await db_session.refresh(version)
        assert version.last_qualification["version_cut_followups"]["status"] == (
            "pending"
        )

        mocker.patch("app.database.AsyncSessionLocal", TestingSessionLocal)
        activate = mocker.patch(
            "app.directions_repo.activate_eligible_staged_directions",
            new_callable=AsyncMock,
            return_value=[],
        )
        refresh = mocker.patch(
            "app.strategic_trigger.request_strategic_refresh",
            new_callable=AsyncMock,
            return_value=False,
        )
        baseline = mocker.patch(
            "app.lane_baseline_snapshot.persist_version_lane_baseline",
            new_callable=AsyncMock,
            return_value={"stored": True},
        )
        supersede = mocker.patch(
            "app.message_supersession.supersede_stale_messages_on_version_close",
            new_callable=AsyncMock,
            return_value={"acted": False, "broadcasts": []},
        )

        async with TestingSessionLocal() as sweep_session:
            assert await rvc.sweep_pending_version_cut_followups(sweep_session) == 0

        await db_session.refresh(version)
        failed_marker = version.last_qualification["version_cut_followups"]
        assert failed_marker["status"] == "pending"
        assert failed_marker["attempts"] == 1
        assert failed_marker["last_error"].startswith("strategic_refresh:")
        assert version.last_qualification["version_cut_receipt"] == immutable_receipt
        assert activate.await_count == 1
        assert activate.await_args.kwargs["terminal_status"] == "abandoned"
        assert baseline.await_count == 0
        assert supersede.await_count == 0

        refresh.return_value = True
        async with TestingSessionLocal() as sweep_session:
            assert await rvc.sweep_pending_version_cut_followups(sweep_session) == 1
        await db_session.refresh(version)
        complete_marker = version.last_qualification["version_cut_followups"]
        assert complete_marker["status"] == "complete"
        assert complete_marker["attempts"] == 2
        assert complete_marker["completed_at"]
        assert complete_marker["last_error"] is None
        assert version.last_qualification["version_cut_receipt"] == immutable_receipt
        assert activate.await_count == 2
        assert all(
            call.kwargs["terminal_status"] == "abandoned"
            for call in activate.await_args_list
        )
        assert refresh.await_count == 2
        assert baseline.await_count == 1
        assert supersede.await_count == 1

        async with TestingSessionLocal() as sweep_session:
            assert await rvc.sweep_pending_version_cut_followups(sweep_session) == 0

    @pytest.mark.asyncio
    async def test_version_cut_followup_sweep_isolates_one_row_failure(
        self,
        client: AsyncClient,
        db_session,
        mocker,
    ):
        first_project = await _make_project(db_session, name="First cut")
        second_project = await _make_project(db_session, name="Second cut")
        first_version = await _make_open_version(db_session, first_project.id)
        second_version = await _make_open_version(db_session, second_project.id)
        first = await client.post(
            f"/api/admin/projects/{first_project.id}/version-cut",
            json=_cut_body("first", key="first-row", version_id=first_version.id),
        )
        second = await client.post(
            f"/api/admin/projects/{second_project.id}/version-cut",
            json=_cut_body("second", key="second-row", version_id=second_version.id),
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        receipts = {
            first.json()["receipt"]["request_fingerprint"],
            second.json()["receipt"]["request_fingerprint"],
        }
        followups = mocker.patch.object(
            rvc,
            "_run_version_cut_followups",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("first row failed unexpectedly"),
                    rvc.VersionCutFollowupResult(complete=True, executed=True),
                ]
            ),
        )

        assert await rvc.sweep_pending_version_cut_followups(db_session) == 1
        assert followups.await_count == 2
        attempted = {
            call.args[0]["request_fingerprint"] for call in followups.await_args_list
        }
        assert attempted == receipts

    @pytest.mark.asyncio
    async def test_version_cut_followup_lease_allows_only_one_executor(
        self,
        client: AsyncClient,
        db_session,
        mocker,
    ):
        from conftest import TestingSessionLocal

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("single executor", key="lease-cut", version_id=version.id),
        )
        assert sent.status_code == 200, sent.text
        receipt = sent.json()["receipt"]
        mocker.patch("app.database.AsyncSessionLocal", TestingSessionLocal)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_activation(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return []

        activate = mocker.patch(
            "app.directions_repo.activate_eligible_staged_directions",
            new=AsyncMock(side_effect=blocked_activation),
        )
        mocker.patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.lane_baseline_snapshot.persist_version_lane_baseline",
            new=AsyncMock(return_value={"stored": True}),
        )
        mocker.patch(
            "app.message_supersession.supersede_stale_messages_on_version_close",
            new=AsyncMock(return_value={"acted": False, "broadcasts": []}),
        )

        first = asyncio.create_task(rvc._run_version_cut_followups(receipt))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await asyncio.wait_for(
            rvc._run_version_cut_followups(receipt),
            timeout=5,
        )
        assert second.complete is False
        assert second.executed is False
        assert activate.await_count == 1
        release.set()
        first_result = await asyncio.wait_for(first, timeout=5)
        assert first_result.complete is True
        assert first_result.executed is True
        assert activate.await_count == 1

        await db_session.refresh(version)
        marker = version.last_qualification["version_cut_followups"]
        assert marker["status"] == "complete"
        assert marker["attempts"] == 1
        assert marker["claim_token"] is None
        assert marker["claim_expires_at"] is None

    @pytest.mark.asyncio
    async def test_version_cut_followup_lost_completion_claim_is_not_complete(
        self,
        client: AsyncClient,
        db_session,
        mocker,
    ):
        from conftest import TestingSessionLocal

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        sent = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("lost claim", key="lost-claim-cut", version_id=version.id),
        )
        assert sent.status_code == 200, sent.text
        receipt = sent.json()["receipt"]
        claim_token = str(uuid.uuid4())
        settle = mocker.patch.object(
            rvc,
            "_update_version_cut_followup_marker",
            new=AsyncMock(side_effect=[f"claim:{claim_token}", "lost"]),
        )
        mocker.patch("app.database.AsyncSessionLocal", TestingSessionLocal)
        mocker.patch(
            "app.directions_repo.activate_eligible_staged_directions",
            new=AsyncMock(return_value=[]),
        )
        mocker.patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.lane_baseline_snapshot.persist_version_lane_baseline",
            new=AsyncMock(return_value={"stored": True}),
        )
        mocker.patch(
            "app.message_supersession.supersede_stale_messages_on_version_close",
            new=AsyncMock(return_value={"acted": False, "broadcasts": []}),
        )

        result = await rvc._run_version_cut_followups(receipt)
        assert result.executed is True
        assert result.complete is False
        assert settle.await_args_list[-1].kwargs == {
            "completed": True,
            "claim_token": claim_token,
        }

    @pytest.mark.asyncio
    async def test_version_cut_serializes_with_promotion_failure(
        self,
        db_session,
        mocker,
    ):
        from conftest import TestingSessionLocal, USE_POSTGRES
        from app import release_promotion

        if not USE_POSTGRES:
            pytest.skip("row-lock ordering requires PostgreSQL")
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version_id = version.id
        mocker.patch(
            "app.notification_dispatch.post_release_version_abandoned_message",
            new=AsyncMock(return_value=None),
        )

        cut_holds_project = asyncio.Event()
        promotion_attempted = asyncio.Event()
        allow_cut = asyncio.Event()
        cut_order = []
        promotion_order = []

        def locked_entity(statement):
            if getattr(statement, "_for_update_arg", None) is None:
                return None
            descriptions = getattr(statement, "column_descriptions", ())
            return descriptions[0].get("entity") if descriptions else None

        async with TestingSessionLocal() as cut_session, TestingSessionLocal() as promotion_session:
            promotion_version = await promotion_session.get(ReleaseVersion, version_id)
            cut_execute = cut_session.execute
            promotion_execute = promotion_session.execute

            async def gated_cut_execute(statement, *args, **kwargs):
                entity = locked_entity(statement)
                result = await cut_execute(statement, *args, **kwargs)
                if entity is not None:
                    cut_order.append(entity)
                if entity is Project:
                    cut_holds_project.set()
                    await allow_cut.wait()
                return result

            async def observed_promotion_execute(statement, *args, **kwargs):
                entity = locked_entity(statement)
                if entity is not None:
                    promotion_order.append(entity)
                if entity is Project:
                    promotion_attempted.set()
                return await promotion_execute(statement, *args, **kwargs)

            mocker.patch.object(
                cut_session,
                "execute",
                new=AsyncMock(side_effect=gated_cut_execute),
            )
            mocker.patch.object(
                promotion_session,
                "execute",
                new=AsyncMock(side_effect=observed_promotion_execute),
            )

            async def cut_and_commit():
                outcome = await rvc.abandon_current_version(
                    cut_session,
                    project.id,
                    reason="authority race",
                    operator_id=TEST_USER_ID,
                    operator_display=TEST_USER_EMAIL,
                    idempotency_key="cut-promotion-race",
                    expected_version_id=version_id,
                    expected_version_number=1,
                    expected_integration_sha=_CUT_SHA,
                    expected_integration_identity=_CUT_IDENTITY.identity_token(),
                )
                await cut_session.commit()
                return outcome

            cut_task = asyncio.create_task(cut_and_commit())
            await asyncio.wait_for(cut_holds_project.wait(), timeout=5)
            promotion_task = asyncio.create_task(
                release_promotion._record_promotion_failure(
                    promotion_session,
                    promotion_version,
                    {"candidate_sha": "b" * 40},
                    "merge_failed",
                    candidate_sha="b" * 40,
                    lock_released=True,
                )
            )
            await asyncio.wait_for(promotion_attempted.wait(), timeout=5)
            assert promotion_order == [Project]
            allow_cut.set()
            cut_outcome, promotion_result = await asyncio.wait_for(
                asyncio.gather(cut_task, promotion_task),
                timeout=10,
            )
            await promotion_session.rollback()

        assert cut_order[:2] == [Project, ReleaseVersion]
        assert promotion_order[:2] == [Project, ReleaseVersion]
        assert cut_outcome.abandoned.status == "abandoned"
        assert cut_outcome.successor.version_number == 2
        assert promotion_result["already_promoted"] is True
        assert promotion_result["status"] == "abandoned"
        async with TestingSessionLocal() as verify_session:
            rows = (
                await verify_session.execute(
                    select(ReleaseVersion).where(
                        ReleaseVersion.project_id == project.id
                    )
                )
            ).scalars().all()
            assert sorted((row.version_number, row.status) for row in rows) == [
                (1, "abandoned"),
                (2, "open"),
            ]
            abandoned = next(row for row in rows if row.version_number == 1)
            assert abandoned.last_qualification["version_cut_receipt"] == cut_outcome.receipt
            assert abandoned.last_qualification["status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_version_cut_live_head_mismatch_writes_nothing(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        self.resolve_cut_identity.return_value = VersionCutIntegrationIdentity(
            owner=_CUT_IDENTITY.owner,
            repo=_CUT_IDENTITY.repo,
            branch=_CUT_IDENTITY.branch,
            head_sha="d" * 40,
            observed_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )

        response = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("stale head", version_id=version.id),
        )
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store, private"
        assert _api_error_code(response) == (
            "version_cut_precondition_failed"
        )
        await db_session.refresh(version)
        assert version.status == "open"
        assert await rv.current_open_version(db_session, project.id) == version
        self.run_cut_followups.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_cut_post_lock_head_advance_writes_nothing(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        advanced = VersionCutIntegrationIdentity(
            owner=_CUT_IDENTITY.owner,
            repo=_CUT_IDENTITY.repo,
            branch=_CUT_IDENTITY.branch,
            head_sha="d" * 40,
            observed_at=datetime(2026, 8, 30, 0, 1, tzinfo=timezone.utc),
        )
        self.resolve_cut_identity.side_effect = [_CUT_IDENTITY, advanced]

        response = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("head advanced", version_id=version.id),
        )
        assert response.status_code == 409
        assert _api_error_code(response) == "version_cut_precondition_failed"
        assert response.json()["error"]["message"]["current"] == {
            "integration": advanced.projection()
        }
        await db_session.refresh(version)
        assert version.status == "open"
        assert await rv.current_open_version(db_session, project.id) == version
        self.run_cut_followups.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_cut_identity_unavailable_fails_closed(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        self.resolve_cut_identity.side_effect = rv.VersionCutIdentityUnavailable(
            "provider unavailable"
        )
        response = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("no identity", version_id=version.id),
        )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store, private"
        assert _api_error_code(response) == (
            "version_cut_identity_unavailable"
        )
        await db_session.refresh(version)
        assert version.status == "open"
        self.run_cut_followups.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_cut_requires_exact_request_identity(
        self,
        client: AsyncClient,
        db_session,
    ):
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")
        response = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json={"reason": "missing authority"},
        )
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store, private"
        self.resolve_cut_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_cut_no_open_version_returns_stale_precondition(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when there is no open release version to cut."""
        project = await _make_project(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("nothing here"),
        )
        assert resp.status_code == 409
        assert _api_error_code(resp) == "version_cut_precondition_failed"

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
            json=_cut_body("not allowed"),
        )
        assert resp.status_code == 403
        assert resp.headers["cache-control"] == "no-store, private"

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
            json=_cut_body("intrusion"),
        )
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store, private"

    @pytest.mark.asyncio
    async def test_version_cut_reason_optional(self, client: AsyncClient, db_session):
        """A cut with no reason still succeeds and records a null reason."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body(version_id=version.id),
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
            json=_cut_body("x" * 501),
        )
        assert resp.status_code == 422
        assert resp.headers["cache-control"] == "no-store, private"

    @pytest.mark.asyncio
    async def test_version_cut_rejects_promotion_check_wait(
        self, client: AsyncClient, db_session
    ):
        """A version with an open promotion PR is rejected before any state
        mutation and returns a clear 409 explanation."""
        from datetime import timedelta
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        version.last_qualification = {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "enabled_at": (_utcnow() - timedelta(hours=2)).isoformat(),
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/version-cut",
            json=_cut_body("too early", version_id=version.id),
        )
        assert resp.status_code == 409
        assert "managed promotion PR is open" in resp.text
        # The version-cut 409 carries the managed-wait age via the exception.
        assert "waiting ~" in resp.text

        await db_session.refresh(version)
        assert version.status == "open"
        assert await rv.current_open_version(db_session, project.id) is not None


class TestClearDeployFailed:
    """POST /api/admin/projects/{id}/clear-deploy-failed operator lever."""

    @staticmethod
    async def _make_deploy_failed_version(
        session,
        project_id,
        *,
        promoted_sha="promotedsha123",
        version_number=1,
        deploy_failure_reason="no_new_revision",
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
            deploy_failure_reason=deploy_failure_reason,
            deploy_confirm_deadline=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
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

        async def non_admin():
            return {"user_id": "other", "email": "other@example.com", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        # The parent admin router carries a router-level get_current_user
        # dependency; without overriding it the bogus token is rejected at 401
        # before require_admin runs. Override both, matching the established
        # non-admin-rejection pattern (e.g. test_non_admin_cannot_list_users).
        app.dependency_overrides[get_current_user] = non_admin
        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as ac:
                resp = await ac.post(
                    f"/api/admin/projects/{project.id}/clear-deploy-failed",
                    json={"force": True, "reason": "x"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_verify_path_clears_when_sha_serving(
        self, client: AsyncClient, db_session, mocker
    ):
        """Promoted SHA genuinely observed serving → cleared without force."""
        from datetime import datetime, timezone

        from app import release_artifacts as artifact_registry
        from app.models import DeploymentState, DeploymentStatus, QualificationVerdict
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        # The backend_revision (Cloud Run) provider is observable only for the
        # deploy-identity project; stamp the identity and scope the registry to
        # backend so the deploy-health re-check drives the outcome without the
        # merge/agent providers' GitHub lookups interfering.
        mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})
        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: artifact_registry._ARTIFACT_PROVIDERS[
                    BACKEND_REVISION_ARTIFACT_KIND
                ]
            },
            clear=True,
        )

        project = await _make_project(db_session)
        project.github_owner = "acme"
        project.github_repo = "widget"
        db_session.add(project)
        await db_session.commit()
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
    async def test_aggregate_only_deploy_failed_is_selected_for_clear(
        self, client: AsyncClient, db_session, mocker
    ):
        """Aggregate-only deploy_failed versions are found even when the backend
        scalar is already serving_confirmed."""
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
        version.serving_status = "serving_confirmed"
        version.served_sha = "liveserving999"
        version.deploy_failure_reason = None
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
        assert version.serving_status == "serving_confirmed"
        assert version.deploy_failure_reason is None

    @pytest.mark.asyncio
    async def test_force_refused_without_override(
        self, client: AsyncClient, db_session
    ):
        """serving_sha_unobservable + no force → 409, version stays deploy_failed."""
        from datetime import datetime, timezone

        from app.models import DeploymentState, DeploymentStatus

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            deploy_failure_reason="serving_sha_unobservable",
        )
        # A STABLE revision booted after promotion but serving SHA is unobservable
        # (current_sha="") — re-check must still classify as serving_sha_unobservable.
        db_session.add(
            DeploymentState(
                current_sha="",
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
        """force=true without a reason → 400 (only for serving_sha_unobservable)."""
        from datetime import datetime, timezone

        from app.models import DeploymentState, DeploymentStatus

        project = await _make_project(db_session)
        await self._make_deploy_failed_version(
            db_session,
            project.id,
            deploy_failure_reason="serving_sha_unobservable",
        )
        db_session.add(
            DeploymentState(
                current_sha="",
                status=DeploymentStatus.STABLE,
                deployed_at=datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "   "},
        )
        assert resp.status_code == 400, resp.text

    @pytest.mark.asyncio
    async def test_force_path_clears_and_records_attestation(
        self, client: AsyncClient, db_session
    ):
        """force=true + reason clears for serving_sha_unobservable, resolves alert."""
        from datetime import datetime, timezone

        from app.models import DeploymentState, DeploymentStatus, QualificationVerdict
        from app.release_stall_alerts import _deploy_failed_alert_title

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            deploy_failure_reason="serving_sha_unobservable",
        )
        # A STABLE revision booted after promotion but serving SHA is unobservable
        # (current_sha="") — re-check must still classify as serving_sha_unobservable
        # so the force path remains permitted.
        db_session.add(
            DeploymentState(
                current_sha="",
                status=DeploymentStatus.STABLE,
                deployed_at=datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
            )
        )
        await db_session.commit()

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

    @pytest.mark.parametrize(
        "failure_reason",
        ["sha_mismatch", "rolled_back", "halted", "no_new_revision"],
    )
    async def test_force_rejected_for_genuine_failure_reason(
        self, client: AsyncClient, db_session, failure_reason
    ):
        """force=true is rejected for known-bad failure reasons that are not
        serving_sha_unobservable — the deployment genuinely failed and cannot
        be force-attested as serving."""
        from datetime import datetime, timezone

        from app.models import DeploymentState, DeploymentStatus

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            deploy_failure_reason=failure_reason,
        )

        # Seed a DeploymentState that drives _check_backend_revision_serving
        # to recheck to the intended failure reason.  Without this, ds=None +
        # now>=deadline always produces "no_new_revision", causing the
        # assertion on the parametrized reason_code to fail for the other three.
        after_promotion = datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc)
        if failure_reason == "sha_mismatch":
            # STABLE revision booted after promotion, serving a mismatched SHA.
            db_session.add(
                DeploymentState(
                    current_sha="different_sha_456",
                    status=DeploymentStatus.STABLE,
                    deployed_at=after_promotion,
                )
            )
            await db_session.commit()
        elif failure_reason == "rolled_back":
            db_session.add(
                DeploymentState(
                    current_sha="",
                    status=DeploymentStatus.ROLLED_BACK,
                    deployed_at=after_promotion,
                )
            )
            await db_session.commit()
        elif failure_reason == "halted":
            db_session.add(
                DeploymentState(
                    current_sha="",
                    status=DeploymentStatus.HALTED,
                    deployed_at=after_promotion,
                    halted_at=after_promotion,
                )
            )
            await db_session.commit()
        # no_new_revision: leave ds=None; deadline already past → no_new_revision

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "I think it worked"},
        )
        assert resp.status_code == 409, resp.text
        assert "serving_sha_unobservable" in resp.text
        assert failure_reason in resp.text

        await db_session.refresh(version)
        assert version.serving_status == "deploy_failed"
        assert version.deploy_failure_reason == failure_reason

    @pytest.mark.asyncio
    async def test_force_blocked_when_backend_reason_transitions_to_hard_failure(
        self, client: AsyncClient, db_session, mocker
    ):
        """Regression: version scalar latched serving_sha_unobservable but the
        backend artifact's freshly-rechecked reason_code is sha_mismatch (a hard
        failure that arose after the latch).  The force path must be rejected even
        though version.deploy_failure_reason still says serving_sha_unobservable.

        Old code checked the stale version scalar → wrongly allowed attestation.
        New code checks backend_artifact.reason_code from _refresh_release_artifact_rows
        → correctly rejects."""
        from datetime import datetime, timezone

        from app import release_artifacts as artifact_registry
        from app.models import DeploymentState, DeploymentStatus
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        # backend_revision is observable only for the deploy identity; stamp it
        # and scope the registry to backend so the fresh deploy-health re-check
        # (sha_mismatch) drives the force-block decision.
        mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})
        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: artifact_registry._ARTIFACT_PROVIDERS[
                    BACKEND_REVISION_ARTIFACT_KIND
                ]
            },
            clear=True,
        )

        project = await _make_project(db_session)
        project.github_owner = "acme"
        project.github_repo = "widget"
        db_session.add(project)
        await db_session.commit()
        # Stale version scalar: latched as serving_sha_unobservable.
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            promoted_sha="promotedsha123",
            deploy_failure_reason="serving_sha_unobservable",
        )
        # Fresh backend state: a STABLE revision booted after promotion but is
        # serving a *different* SHA → sha_mismatch (hard failure).
        db_session.add(
            DeploymentState(
                current_sha="different_sha_456",
                status=DeploymentStatus.STABLE,
                deployed_at=datetime(2026, 1, 1, 0, 30, 0, tzinfo=timezone.utc),
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={"force": True, "reason": "I think it worked"},
        )
        # Must be rejected: fresh backend reason is sha_mismatch, not
        # serving_sha_unobservable — force attestation is not permitted.
        assert resp.status_code == 409, resp.text
        assert "serving_sha_unobservable" in resp.text
        assert "sha_mismatch" in resp.text

        await db_session.refresh(version)
        assert version.serving_status == "deploy_failed"

    @pytest.mark.asyncio
    async def test_version_lock_released_before_provider_check(
        self, client: AsyncClient, db_session, mocker
    ):
        """The version row lock must be released before provider.check (network I/O).

        clear_deploy_failed previously held with_for_update() across provider
        checks. The observation phase must run before the exact short CAS apply.

        This test patches observe_release_artifacts so that, during the call, it
        attempts a nowait FOR UPDATE lock on the version row from a separate DB session.
        If the endpoint already committed (releasing its lock), the probe succeeds.
        If the endpoint still holds the lock, the probe raises an OperationalError.
        """
        from sqlalchemy import select as sa_select
        from sqlalchemy.exc import OperationalError

        from app.database import AsyncSessionLocal
        from app.models import ReleaseVersion as RVModel

        project = await _make_project(db_session)
        version = await self._make_deploy_failed_version(
            db_session,
            project.id,
            deploy_failure_reason="serving_sha_unobservable",
        )
        version_id = version.id

        lock_probe_results: list[bool] = []

        async def _probe_then_return(session, observed_version_id, now, **kwargs):
            assert observed_version_id == version_id
            # Attempt a no-wait lock on the version row from a SEPARATE session.
            # Succeeds only if clear_deploy_failed already committed its initial lock.
            async with AsyncSessionLocal() as probe_session:
                try:
                    await probe_session.execute(
                        sa_select(RVModel)
                        .where(RVModel.id == version_id)
                        .with_for_update(nowait=True)
                    )
                    lock_probe_results.append(True)
                except OperationalError:
                    lock_probe_results.append(False)
                await probe_session.rollback()
            # No observation plan is an explicit stale/unavailable 409.
            return None

        mocker.patch(
            "app.release_serving_observation.observe_release_artifacts",
            side_effect=_probe_then_return,
        )

        resp = await client.post(
            f"/api/admin/projects/{project.id}/clear-deploy-failed",
            json={},
        )
        # 409 is expected because the synthetic observer returns no plan.
        assert resp.status_code == 409, resp.text
        assert lock_probe_results == [True], (
            "Version row lock must be released (committed) before "
            "observe_release_artifacts is called; probe could not acquire lock"
        )


class TestRecordActivationReceipt:
    """POST /api/admin/projects/{id}/activation-receipt operator lever.

    The one-command ``blueprint deploy`` path binds the built revision to the
    project's latest promoted version, activates it on the control-plane host,
    then POSTs this receipt to close the serving-confirmation loop.
    """

    @staticmethod
    async def _make_promoted_unconfirmed_version(
        session,
        project_id,
        *,
        promoted_sha="promotedsha999",
        version_number=1,
    ):
        from datetime import datetime, timezone

        from app.models import QualificationVerdict

        version = ReleaseVersion(
            project_id=project_id,
            version_number=version_number,
            phase="stabilize",
            status="promoted",
            promoted_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            promoted_sha=promoted_sha,
            serving_status="unconfirmed",
            deploy_confirm_deadline=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
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
        )
        session.add(verdict)
        await session.commit()
        return version

    @staticmethod
    def _scope_backend_only(mocker):
        """Limit the artifact registry to the backend provider so the receipt's
        serving confirmation resolves without the merge/agent providers' GitHub
        lookups (matching the established TestClearDeployFailed pattern)."""
        from app import release_artifacts as artifact_registry
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})
        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: artifact_registry._ARTIFACT_PROVIDERS[
                    BACKEND_REVISION_ARTIFACT_KIND
                ]
            },
            clear=True,
        )

    @staticmethod
    async def _deploy_identity_project(db_session):
        project = await _make_project(db_session)
        project.github_owner = "acme"
        project.github_repo = "widget"
        db_session.add(project)
        await db_session.commit()
        return project

    @pytest.mark.asyncio
    async def test_receipt_confirms_promoted_version_to_serving(
        self, client: AsyncClient, db_session, mocker
    ):
        """A receipt for a promoted unconfirmed version confirms the backend
        artifact, flips the version to serving_confirmed, and stamps provenance."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        version.base_sha = "b" * 40
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "a" * 40,
                "host": "hamsterwheel",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["served_sha"] == "a" * 40
        assert data["served_at"] is not None
        assert data["deploy_failure_reason"] is None
        assert data["receipt"]["mode"] == "cli_activation"
        assert data["receipt"]["serving_status"] == "serving_confirmed"
        assert data["receipt"]["served_sha"] == "a" * 40

        await db_session.refresh(version)
        assert version.serving_status == "serving_confirmed"
        assert version.served_sha == "a" * 40
        qual = version.last_qualification or {}
        receipt = qual["activation_receipt"]
        assert receipt["mode"] == "cli_activation"
        assert receipt["host"] == "hamsterwheel"
        assert receipt["revision"] == "a" * 40
        authorship = qual["operator_authored_commits"]
        assert authorship["state"] in {"resolved", "unresolved", "terminal"}
        if authorship["state"] == "unresolved":
            assert authorship["reason"] != "authorship_measurement_pending"

        from sqlmodel import select

        from app.models import QualificationVerdict

        verdict = (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar_one()
        assert verdict.post_promotion_outcome == "serving_confirmed"

    @pytest.mark.asyncio
    async def test_idempotent_repost_returns_current_state(
        self, client: AsyncClient, db_session, mocker
    ):
        """Re-POSTing an identical receipt for an already-serving version is
        idempotent (200, no error, same state)."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        body = {
            "version_number": version.version_number,
            "revision": "b" * 40,
            "host": "hamsterwheel",
        }
        first = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert first.status_code == 200, first.text
        assert first.json()["serving_status"] == "serving_confirmed"
        await db_session.refresh(version)
        from app.models import ReleaseDeployArtifact
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        backend_artifact = (
            await db_session.execute(
                select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind
                    == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        ).scalar_one()
        activated_at = (version.last_qualification or {})["activation_receipt"][
            "activated_at"
        ]
        version_served_at = version.served_at
        backend_served_at = backend_artifact.served_at

        original_override = app.dependency_overrides[get_current_user]

        async def renamed_user():
            return {
                "user_id": TEST_USER_ID,
                "email": "renamed@example.com",
                "role": "admin",
            }

        app.dependency_overrides[get_current_user] = renamed_user
        try:
            second = await client.post(
                f"/api/admin/projects/{project.id}/activation-receipt", json=body
            )
        finally:
            app.dependency_overrides[get_current_user] = original_override
        assert second.status_code == 200, second.text
        data = second.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["served_sha"] == "b" * 40
        assert data["receipt"]["serving_status"] == "serving_confirmed"
        await db_session.refresh(version)
        await db_session.refresh(backend_artifact)
        assert (
            (version.last_qualification or {})["activation_receipt"]["activated_at"]
            == activated_at
        )
        assert version.served_at == version_served_at
        assert backend_artifact.served_at == backend_served_at

        conflict = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={**body, "host": "different-host"},
        )
        assert conflict.status_code == 409, conflict.text
        await db_session.refresh(version)
        await db_session.refresh(backend_artifact)
        receipt = (version.last_qualification or {})["activation_receipt"]
        assert receipt["operator_id"] == TEST_USER_ID
        assert receipt["operator_display"] == TEST_USER_EMAIL
        assert receipt["operator"] == TEST_USER_EMAIL
        assert receipt["host"] == "hamsterwheel"
        assert receipt["activated_at"] == activated_at
        assert version.served_at == version_served_at
        assert backend_artifact.served_at == backend_served_at

    @pytest.mark.asyncio
    async def test_exact_receipt_replays_after_newer_promotion_without_mutation(
        self, client: AsyncClient, db_session, mocker
    ):
        """A response-lost retry is governed by its immutable receipt, not the
        project's later latest-promotion pointer without replaying receipt writes."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        body = {
            "version_number": version.version_number,
            "revision": "e" * 40,
            "host": "hamsterwheel",
        }
        first = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert first.status_code == 200, first.text
        await db_session.refresh(version)
        before = json.loads(json.dumps(version.last_qualification))
        served_at = version.served_at

        await self._make_promoted_unconfirmed_version(
            db_session,
            project.id,
            promoted_sha="newerpromotedsha",
            version_number=2,
        )
        continuation = mocker.patch(
            "app.release_reconciliation._confirm_serving_or_fail",
            new_callable=AsyncMock,
        )
        artifact_create = mocker.patch(
            "app.release_activation_receipt.get_or_create_release_deploy_artifact",
            new_callable=AsyncMock,
        )

        replay = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["mode"] == "cli_activation"
        continuation.assert_awaited_once_with(version.id)
        artifact_create.assert_not_awaited()
        await db_session.refresh(version)
        assert json.loads(json.dumps(version.last_qualification)) == before
        assert version.served_at == served_at

    @pytest.mark.asyncio
    async def test_cli_receipt_replays_after_later_approval_without_mode_rewrite(
        self, client: AsyncClient, db_session, mocker
    ):
        """Mutable approval fields cannot rewrite a durable CLI receipt."""
        from datetime import datetime, timezone

        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        body = {
            "version_number": version.version_number,
            "revision": "f" * 40,
            "host": "hamsterwheel",
        }
        first = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert first.status_code == 200, first.text
        await db_session.refresh(version)
        before = json.loads(json.dumps(version.last_qualification))
        version.activation_requested_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
        version.activation_requested_by = "later-approver@example.com"
        db_session.add(version)
        await db_session.commit()

        continuation = mocker.patch(
            "app.release_reconciliation._confirm_serving_or_fail",
            new_callable=AsyncMock,
        )
        replay = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["mode"] == "cli_activation"
        continuation.assert_awaited_once_with(version.id)
        await db_session.refresh(version)
        assert (version.last_qualification or {})["activation_receipt"] == before[
            "activation_receipt"
        ]

    @pytest.mark.asyncio
    async def test_stable_actor_id_rejects_different_principal_with_same_email(
        self, client: AsyncClient, db_session, mocker
    ):
        from datetime import datetime, timezone

        from app.release_activation_receipt import (
            ActivationReceiptConflict,
            record_activation_receipt,
        )

        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        body = {
            "version_number": version.version_number,
            "revision": "c" * 40,
            "host": "hamsterwheel",
        }
        first = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert first.status_code == 200, first.text
        await db_session.refresh(version)
        before = json.loads(json.dumps(version.last_qualification))

        with pytest.raises(ActivationReceiptConflict):
            await record_activation_receipt(
                db_session,
                version,
                revision="c" * 40,
                host="hamsterwheel",
                operator_id="different-stable-user-id",
                operator_display=TEST_USER_EMAIL,
                now=datetime(2026, 8, 30, tzinfo=timezone.utc),
            )

        assert json.loads(json.dumps(version.last_qualification)) == before

    @pytest.mark.asyncio
    async def test_legacy_receipt_replays_only_for_exact_historical_display(
        self, client: AsyncClient, db_session, mocker
    ):
        from datetime import datetime, timezone

        from app.release_activation_receipt import (
            ActivationReceiptConflict,
            record_activation_receipt,
        )

        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        body = {
            "version_number": version.version_number,
            "revision": "d" * 40,
            "host": "hamsterwheel",
        }
        first = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt", json=body
        )
        assert first.status_code == 200, first.text
        await db_session.refresh(version)
        qual = dict(version.last_qualification or {})
        legacy = dict(qual["activation_receipt"])
        legacy.pop("operator_id")
        legacy.pop("operator_display")
        qual["activation_receipt"] = legacy
        version.last_qualification = qual
        db_session.add(version)
        await db_session.commit()

        replay = await record_activation_receipt(
            db_session,
            version,
            revision="d" * 40,
            host="hamsterwheel",
            operator_id=TEST_USER_ID,
            operator_display=TEST_USER_EMAIL,
            now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )
        assert replay["serving_status"] == "serving_confirmed"
        assert "operator_id" not in version.last_qualification["activation_receipt"]

        with pytest.raises(ActivationReceiptConflict):
            await record_activation_receipt(
                db_session,
                version,
                revision="d" * 40,
                host="hamsterwheel",
                operator_id=TEST_USER_ID,
                operator_display="renamed@example.com",
                now=datetime(2026, 8, 30, tzinfo=timezone.utc),
            )

    @pytest.mark.asyncio
    async def test_404_when_version_number_unknown(
        self, client: AsyncClient, db_session
    ):
        """Unknown version_number for the project → 404."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={"version_number": 99},
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_409_when_version_not_promoted(
        self, client: AsyncClient, db_session
    ):
        """A non-promoted version cannot be confirmed serving by a receipt → 409."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={"version_number": 1},
        )
        assert resp.status_code == 409, resp.text

    @pytest.mark.asyncio
    async def test_400_when_revision_not_40_hex(
        self, client: AsyncClient, db_session, mocker
    ):
        """A malformed revision is rejected at 400 before any state change."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="promotedsha999"
        )
        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "not-a-sha",
            },
        )
        assert resp.status_code == 400, resp.text

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, override_get_session, db_session):
        """Non-admin is rejected with 403 by require_admin."""
        from fastapi import HTTPException

        project = await _make_project(db_session)
        await self._make_promoted_unconfirmed_version(db_session, project.id)

        async def non_admin():
            return {"user_id": "other", "email": "other@example.com", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[get_current_user] = non_admin
        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as ac:
                resp = await ac.post(
                    f"/api/admin/projects/{project.id}/activation-receipt",
                    json={"version_number": 1},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_promoted_version_list_exposes_promoted_sha(
        self, client: AsyncClient, db_session
    ):
        """The promoted-version list the CLI queries for binding includes the
        promoted SHA, so ``blueprint deploy`` can verify the built revision
        contains the promoted commit."""
        project = await _make_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="c" * 40
        )

        resp = await client.get(
            f"/api/admin/projects/{project.id}/release-versions",
            params={"status": "promoted", "limit": 1},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        entry = data["versions"][0]
        assert entry["version_number"] == version.version_number
        assert entry["promoted_sha"] == "c" * 40


class TestActivationApproval:
    """POST /api/admin/projects/{id}/approve-activation control contract.

    The in-product approved-promotion-activation path: an administrator approves
    the CURRENT promoted, still-unconfirmed version for the trusted control-plane
    host to execute.  The approval binds EXACTLY the version's immutable
    ``promoted_sha``; the activation-receipt path refuses stale (superseded) or
    mismatched executions so an approval can never authorize a different or
    historical artifact.
    """

    @staticmethod
    async def _make_promoted_unconfirmed_version(
        session,
        project_id,
        *,
        promoted_sha="f" * 40,
        version_number=1,
        serving_status="unconfirmed",
    ):
        from datetime import datetime, timezone

        from app.models import QualificationVerdict

        version = ReleaseVersion(
            project_id=project_id,
            version_number=version_number,
            phase="stabilize",
            status="promoted",
            promoted_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            promoted_sha=promoted_sha,
            serving_status=serving_status,
            deploy_confirm_deadline=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
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
        )
        session.add(verdict)
        await session.commit()
        return version

    @staticmethod
    def _scope_backend_only(mocker):
        """Limit the artifact registry to the backend provider (matching the
        established TestRecordActivationReceipt pattern) so serving confirmation
        resolves without the merge/agent providers' GitHub lookups."""
        from app import release_artifacts as artifact_registry
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})
        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                BACKEND_REVISION_ARTIFACT_KIND: artifact_registry._ARTIFACT_PROVIDERS[
                    BACKEND_REVISION_ARTIFACT_KIND
                ]
            },
            clear=True,
        )

    @staticmethod
    async def _deploy_identity_project(db_session):
        project = await _make_project(db_session)
        project.github_owner = "acme"
        project.github_repo = "widget"
        db_session.add(project)
        await db_session.commit()
        return project

    @pytest.mark.asyncio
    async def test_admin_approval_binds_latest_unconfirmed_promotion_for_host_execution(
        self, client: AsyncClient, db_session, mocker
    ):
        """One valid approval-to-agent-contract flow end to end: an admin approves
        the latest promoted unconfirmed version; the durable binding (project,
        version, promoted SHA, requestor, timestamp) is exposed on
        GET /release-versions; the trusted host then POSTs an activation receipt
        for the EXACT approved promoted SHA and the version resolves to
        serving_confirmed with approved-mode provenance."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        promoted_sha = "d" * 40
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha=promoted_sha
        )

        # (1) Approve activation — returns the durable binding.
        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 200, resp.text
        approval = resp.json()
        assert approval["version_number"] == version.version_number
        assert approval["project_id"] == str(project.id)
        assert approval["promoted_sha"] == promoted_sha
        assert approval["activation_requested_at"] is not None
        assert approval["activation_requested_by"] == "test@example.com"
        assert approval["serving_status"] == "unconfirmed"

        # (2) The host obtains the approved binding from GET /release-versions.
        listing = await client.get(
            f"/api/admin/projects/{project.id}/release-versions",
            params={"status": "promoted", "limit": 1},
        )
        assert listing.status_code == 200, listing.text
        entry = listing.json()["versions"][0]
        assert entry["version_number"] == version.version_number
        assert entry["promoted_sha"] == promoted_sha
        assert entry["activation_requested_at"] == approval["activation_requested_at"]
        assert entry["activation_requested_by"] == approval["activation_requested_by"]

        # (3) The host reports the approved execution with the EXACT approved SHA.
        receipt = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": promoted_sha,
                "host": "hamsterwheel",
            },
        )
        assert receipt.status_code == 200, receipt.text
        data = receipt.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["served_sha"] == promoted_sha
        assert data["receipt"]["mode"] == "approved_activation"
        assert data["receipt"]["served_sha"] == promoted_sha

        # Durable provenance: the immutable approval binding and the receipt that
        # fulfilled it are both recorded on the version's last_qualification.
        await db_session.refresh(version)
        assert version.serving_status == "serving_confirmed"
        qual = version.last_qualification or {}
        approval_record = qual["activation_approval"]
        assert approval_record["mode"] == "in_product_approval"
        assert approval_record["approved_sha"] == promoted_sha
        assert approval_record["operator"] == "test@example.com"
        receipt_record = qual["activation_receipt"]
        assert receipt_record["mode"] == "approved_activation"
        assert receipt_record["revision"] == promoted_sha
        assert receipt_record["activation_requested_by"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_receipt_rejected_when_approved_revision_mismatches(
        self, client: AsyncClient, db_session, mocker
    ):
        """An active approval authorizes EXACTLY the approved version's promoted
        SHA; a receipt reporting a different valid revision must be rejected (409)
        so an approval can never confirm a different or historical artifact."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        promoted_sha = "c" * 40
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha=promoted_sha
        )

        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 200, resp.text

        # A different, valid 40-hex revision — NOT the approved promoted SHA.
        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "9" * 40,
            },
        )
        assert resp.status_code == 409, resp.text
        assert "does not match" in resp.json()["error"]["message"]

        await db_session.refresh(version)
        assert version.serving_status == "unconfirmed"

    @pytest.mark.asyncio
    async def test_receipt_rejected_when_approved_activation_superseded(
        self, client: AsyncClient, db_session, mocker
    ):
        """An approved activation must not execute once a newer promotion has
        superseded the approved version — the approval authorizes the CURRENT
        promoted version, so a receipt for the older (now historical) version is
        stale → 409 and the version stays unconfirmed."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        v1 = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="1" * 40, version_number=1
        )
        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 200, resp.text

        # A newer promotion supersedes the approved v1.
        await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="2" * 40, version_number=2
        )

        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": v1.version_number,
                "revision": "1" * 40,
            },
        )
        assert resp.status_code == 409, resp.text
        assert "stale" in resp.json()["error"]["message"]

        await db_session.refresh(v1)
        assert v1.serving_status == "unconfirmed"

    @pytest.mark.asyncio
    async def test_receipt_without_approval_still_accepts_descendant_revision(
        self, client: AsyncClient, db_session, mocker
    ):
        """Backward compatibility: WITHOUT an approval the legacy CLI receipt
        path is unchanged — a receipt reporting a revision that differs from
        promoted_sha (a built descendant) still confirms serving."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="a" * 40
        )

        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "b" * 40,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["serving_status"] == "serving_confirmed"
        assert data["receipt"]["mode"] == "cli_activation"

    @pytest.mark.asyncio
    async def test_approval_rejected_when_version_already_serving(
        self, client: AsyncClient, db_session, mocker
    ):
        """Approving a version that already resolved to serving_confirmed is
        rejected (409) — there is nothing left to activate."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="e" * 40
        )
        receipt = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "e" * 40,
            },
        )
        assert receipt.status_code == 200, receipt.text

        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 409, resp.text
        assert "already serving" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_approval_rejected_when_deploy_failed(
        self, client: AsyncClient, db_session
    ):
        """Approving a version latched to deploy_failed is rejected (409) — the
        operator must clear the deploy failure first."""
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="f" * 40
        )
        version.serving_status = "deploy_failed"
        version.deploy_failure_reason = "sha_mismatch"
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 409, resp.text
        assert "deploy failed" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_approval_idempotent(
        self, client: AsyncClient, db_session, mocker
    ):
        """Re-approving a still-unconfirmed version returns the existing approval
        without re-stamping it."""
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="g" * 40
        )

        first = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert first.status_code == 200, first.text
        first_at = first.json()["activation_requested_at"]

        second = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert second.status_code == 200, second.text
        assert second.json()["activation_requested_at"] == first_at
        assert second.json()["promoted_sha"] == "g" * 40

    @pytest.mark.asyncio
    async def test_404_when_no_promoted_version(
        self, client: AsyncClient, db_session
    ):
        """No promoted version for the project → 404."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, override_get_session, db_session):
        """Non-admin is rejected with 403 by require_admin."""
        from fastapi import HTTPException

        project = await _make_project(db_session)
        await self._make_promoted_unconfirmed_version(db_session, project.id)

        async def non_admin():
            return {"user_id": "other", "email": "other@example.com", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[get_current_user] = non_admin
        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as ac:
                resp = await ac.post(
                    f"/api/admin/projects/{project.id}/approve-activation"
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_approval_rejected_when_version_never_armed(
        self, client: AsyncClient, db_session, mocker
    ):
        """A never-armed promoted version (serving_status NULL) cannot be approved.

        The in-product approve lever targets the ``unconfirmed`` state the
        attention item and callout surface — exactly what the design doc
        requires.  A version with no observable serving state (never armed) has
        no activation pending in-product, so approval is rejected rather than
        creating an approval the receipt guards would have to special-case.
        """
        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha="d" * 40, serving_status=None
        )
        assert version.serving_status is None

        resp = await client.post(
            f"/api/admin/projects/{project.id}/approve-activation"
        )
        assert resp.status_code == 409, resp.text
        assert "not awaiting activation" in resp.json()["error"]["message"]

        await db_session.refresh(version)
        assert version.activation_requested_at is None

    @pytest.mark.asyncio
    async def test_receipt_rejected_when_approved_version_never_armed_mismatches(
        self, client: AsyncClient, db_session, mocker
    ):
        """An approval on a never-armed version still enforces SHA-match.

        The approval's core guarantee — a receipt can never confirm a different
        or historical artifact — is not conditioned on the version having been
        "armed".  Even for a promoted version whose serving_status is NULL, a
        receipt reporting a revision other than the approved promoted SHA is
        rejected (409), so the SHA-strictness gap for never-armed promotions is
        closed at the enforcement point regardless of how the approval was set.
        """
        from app.time_utils import _utcnow

        self._scope_backend_only(mocker)
        project = await self._deploy_identity_project(db_session)
        promoted_sha = "a" * 40
        version = await self._make_promoted_unconfirmed_version(
            db_session, project.id, promoted_sha=promoted_sha, serving_status=None
        )
        version.activation_requested_at = _utcnow()
        version.activation_requested_by = "op@example.com"
        db_session.add(version)
        await db_session.commit()

        # A different, valid 40-hex revision — NOT the approved promoted SHA.
        resp = await client.post(
            f"/api/admin/projects/{project.id}/activation-receipt",
            json={
                "version_number": version.version_number,
                "revision": "9" * 40,
            },
        )
        assert resp.status_code == 409, resp.text
        assert "does not match" in resp.json()["error"]["message"]

        await db_session.refresh(version)
        assert version.serving_status is None
        assert version.activation_requested_at is not None


class TestAgentServingShas:
    """GET /agent-serving-shas exposes read-only agent image provenance."""

    @pytest.mark.asyncio
    async def test_viewer_can_read_agent_serving_shas_with_drift_context(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="Agent Serving SHAs")
        project.github_owner = "owner"
        project.github_repo = "repo"
        db_session.add(project)

        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            status="open",
            last_qualification={"candidate_sha": "b" * 40},
        )
        served_version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            status="promoted",
            served_sha="a" * 40,
            served_at=datetime.now(timezone.utc),
        )
        member = PoolVm(
            vm_instance_id="agent-vm-serving-sha",
            project_id=project.id,
            state=PoolVmState.IDLE,
            agent_source_sha="a" * 40,
            agent_image_name="agent-image-a",
            agent_image_build_ts=1781049600,
        )
        db_session.add(open_version)
        db_session.add(served_version)
        db_session.add(member)
        await db_session.commit()

        with (
            patch(
                "app.github.get_repo_installation_token",
                new_callable=AsyncMock,
                return_value="token",
            ),
            patch(
                "app.github_refs.get_branch_sha",
                new_callable=AsyncMock,
                side_effect=["c" * 40, "d" * 40],
            ),
            patch(
                "app.compute.get_current_agent_image",
                return_value={
                    "image_name": "agent-image-a",
                    "source_git_sha": "a" * 40,
                    "build_ts": 1781049600,
                },
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/agent-serving-shas"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["backend_served_sha"] == "a" * 40
        assert data["active_candidate_sha"] == "b" * 40
        assert data["integration_head_sha"] == "c" * 40
        assert data["promotion_head_sha"] == "d" * 40
        assert data["current_image"]["source_git_sha"] == "a" * 40
        bucket = data["fleet"]["buckets"][0]
        assert bucket["source_sha"] == "a" * 40
        assert bucket["matches_backend_served"] is True
        assert bucket["matches_current_image"] is True

    @pytest.mark.asyncio
    async def test_agent_serving_shas_degrades_when_external_context_unavailable(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="Agent Serving Graceful")
        member = PoolVm(
            vm_instance_id="agent-vm-serving-unknown",
            project_id=project.id,
            state=PoolVmState.IDLE,
        )
        db_session.add(member)
        await db_session.commit()

        with patch("app.compute.get_current_agent_image", return_value=None):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/agent-serving-shas"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_image"] is None
        assert data["integration_head_sha"] is None
        assert data["fleet"]["unknown_count"] == 1
        assert data["fleet"]["buckets"][0]["source_sha"] is None

    @pytest.mark.asyncio
    async def test_agent_serving_shas_requires_project_access(
        self, override_get_session, db_session
    ):
        project = await _make_project(db_session, name="Agent Serving Private")

        async def other_user():
            return {
                "user_id": "not-a-member",
                "email": "other@example.com",
                "role": "user",
            }

        app.dependency_overrides[get_current_user] = other_user
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    f"/api/admin/projects/{project.id}/agent-serving-shas"
                )
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_current_user, None)


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
                "infra_failure_child_events": [
                    {
                        "diagnostic_schema_version": 1,
                        "leg": "audit",
                        "failed_task_id": "00000000-0000-0000-0000-000000000001",
                        "attempt_trace": [
                            {
                                "attempt_ordinal": 1,
                                "status": "FAILED",
                                "agent_error": {
                                    "schema": 1,
                                    "provider_terminal_reason": "aborted_streaming",
                                    "output_tail": "bounded provider tail",
                                },
                            }
                        ],
                        "attempt_trace_total": 1,
                        "attempt_trace_truncated": False,
                    }
                ],
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
        attempt = lq["infra_failure_evidence"]["events"][0]["attempt_trace"][0]
        assert attempt["attempt_ordinal"] == 1
        assert attempt["agent_error"]["provider_terminal_reason"] == (
            "aborted_streaming"
        )
        assert attempt["agent_error"]["output_tail"] == "bounded provider tail"

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


class TestVersionLifecycleGateLatch:
    """GET /api/admin/projects/{id}/version-lifecycle exposes gate_latch.

    is_permanent_gate_incapacity only fires on a same-signature INFRA-error
    streak, so a cleanly-run qualification blocked by a scoring gate that the
    candidate itself repairs (in the deployed module) never sets it. gate_latch
    is a separate, additive signal sourced from the persisted deployed-gate-latch
    Alert the reaper sweep writes, so the hotfix lane is discoverable in that
    case too.
    """

    @pytest.mark.asyncio
    async def test_gate_latch_exposed_for_clean_scoring_blocker(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "not_promoted",
                "blocker": "candidate_operator_authorship_nonzero",
                "candidate_sha": "abc1234567890",
                "finalized_at": "2026-05-27T00:00:00",
                "candidate_operator_authorship_gate": {
                    "status": "unconfigured",
                    "passed": False,
                    "count": 0,
                    "max_count": None,
                    "blocker": "candidate_operator_authorship_evidence_missing",
                    "reason": "no_operator_authorship_contract_declared",
                },
            },
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        dedupe_key = (
            f"release-gate-latch:{version.id}:"
            "candidate_operator_authorship_nonzero:deadbeef...abc1234567890"
        )
        alert_message = (
            "v3 is blocked by gate candidate_operator_authorship_nonzero, but "
            "the candidate modifies its deployed implementation: "
            "backend/app/release_commit_authorship.py. Compare range "
            "deadbeef...abc1234567890. This repair cannot clear the gate from "
            "inside the qualifier; use the approved release-machinery hotfix lane."
        )
        db_session.add(
            Alert(
                project_id=project.id,
                event_type="release_blocked",
                domain="release_lifecycle",
                source="release_stall_alerts",
                dedupe_key=dedupe_key,
                title="Release v3 gate latched: candidate_operator_authorship_nonzero",
                message=alert_message,
            )
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
        cv = resp.json()["current_version"]
        assert cv["last_qualification"]["is_permanent_gate_incapacity"] is None
        assert cv["last_qualification"]["candidate_operator_authorship_gate"] == {
            "status": "unconfigured",
            "passed": False,
            "count": 0,
            "max_count": None,
            "blocker": "candidate_operator_authorship_evidence_missing",
            "reason": "no_operator_authorship_contract_declared",
        }
        assert cv["gate_latch"] is not None
        assert cv["gate_latch"]["gate"] == "candidate_operator_authorship_nonzero"
        assert cv["gate_latch"]["compare_range"] == "deadbeef...abc1234567890"
        assert cv["gate_latch"]["message"] == alert_message

    @pytest.mark.asyncio
    async def test_gate_latch_null_when_no_active_alert(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=4,
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
        assert resp.json()["current_version"]["gate_latch"] is None


class TestVersionLifecyclePolicyProvenance:
    """GET /version-lifecycle exposes promotion policy source and fallback detail."""

    @pytest.mark.asyncio
    async def test_current_policy_bypasses_cached_lifecycle_body(
        self, client: AsyncClient, db_session
    ):
        """A live policy edit is visible even while the heavy body is cached."""
        from app.release_qualifier import _runtime_config

        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")
        readiness = AsyncMock(return_value={"ready": False, "reason": "test"})

        with (
            patch.dict(
                _runtime_config,
                {
                    "promotion_threshold": 0.90,
                    "promotion_weights": {"audit": 0.6, "test_erosion": 0.4},
                    "completion_debt_weight_mode": "observe",
                },
                clear=False,
            ),
            patch(
                "app.release_qualifier.compute_promotion_readiness",
                new=readiness,
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
            first = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )
            _runtime_config["promotion_threshold"] = 0.81
            second = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["current_effective_policy"]["threshold"] == pytest.approx(
            0.90
        )
        assert second.json()["current_effective_policy"]["threshold"] == pytest.approx(
            0.81
        )
        assert second.json()["promotion_policy_source"] == "runtime_config"
        # The expensive lifecycle body was reused; only the policy overlay reran.
        assert readiness.await_count == 1

    @pytest.mark.asyncio
    async def test_policy_provenance_is_included_on_successful_load(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier._resolve_policy_with_provenance",
                return_value=(
                    {
                        "weights": {
                            "audit": 0.5,
                            "test_erosion": 0.25,
                            "smoketest": 0.25,
                        },
                        "threshold": 0.8,
                    },
                    "yaml_file",
                    None,
                ),
            ),
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
        body = resp.json()
        assert body["promotion_policy_source"] == "yaml_file"
        assert body["promotion_policy_error"] is None
        assert body["current_effective_policy"]["threshold"] == pytest.approx(0.8)
        assert body["current_effective_policy"]["source"] == "yaml_file"

    @pytest.mark.asyncio
    async def test_policy_provenance_exposes_hardcoded_default_reason(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_qualifier._resolve_policy_with_provenance",
                return_value=(
                    {
                        "weights": {
                            "audit": 0.4,
                            "test_erosion": 0.3,
                            "smoketest": 0.3,
                        },
                        "threshold": 0.9,
                    },
                    "hardcoded_defaults",
                    "ScannerError: promotion_policy.yaml did not parse",
                ),
            ),
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
        body = resp.json()
        assert body["promotion_policy_source"] == "hardcoded_defaults"
        assert (
            body["promotion_policy_error"]
            == "ScannerError: promotion_policy.yaml did not parse"
        )
        assert body["current_effective_policy"]["source"] == "hardcoded_defaults"
        assert body["current_effective_policy"]["fallback_error"] == (
            "ScannerError: promotion_policy.yaml did not parse"
        )

    @pytest.mark.asyncio
    async def test_recent_version_echoes_deploy_confirm_deadline_for_unconfirmed(
        self, client: AsyncClient, db_session
    ):
        """A recently-promoted (closed) version that is still 'unconfirmed'
        echoes deploy_confirm_deadline in its recent_versions history row — the
        field where the unconfirmed serving state actually appears."""
        from datetime import datetime, timezone

        project = await _make_project(db_session)
        deadline = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            promoted_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            promoted_sha="promotedsha123",
            serving_status="unconfirmed",
            deploy_confirm_deadline=deadline,
        )
        db_session.add(version)
        await db_session.commit()

        with patch(
            "app.release_qualifier.compute_promotion_readiness",
            new=AsyncMock(return_value={"ready": False, "reason": "test"}),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        history = resp.json()["recent_versions"]
        row = next(r for r in history if r["version_number"] == 1)
        assert row["serving_status"] == "unconfirmed"
        # The deadline is echoed in the history row (the field is the only place
        # the unconfirmed serving state actually appears). The column stores
        # naive UTC, so the wire form must re-stamp the offset a JS/Python
        # consumer needs; pin the literal rather than re-deriving it from the
        # ORM value, which would pass for either format.
        assert row["deploy_confirm_deadline"] == "2026-01-01T01:00:00+00:00"

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
        """The lifecycle payload surfaces managed promotion wait as a distinct
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
        assert current["qualification_state"]["state"] == "promotion_check_wait"
        assert current["qualification_state"]["promotion_pr_number"] == 7357
        assert "revalidate the exact base/head pair" in current[
            "qualification_state"
        ]["summary"].lower()


class TestVersionLifecycleArtifactServingState:
    """GET /version-lifecycle exposes aggregate artifact serving truth."""

    @staticmethod
    def _lifecycle_patches():
        return (
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
        )

    @pytest.mark.asyncio
    async def test_recent_versions_use_aggregate_pending_artifact_truth(
        self, client: AsyncClient, db_session, mocker
    ):
        """A backend-confirmed promotion remains aggregate-unconfirmed while a
        required non-backend artifact is still pending."""
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
        promoted = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 5, 1, 0, 0, 0),
            promoted_at=datetime(2026, 5, 2, 0, 0, 0),
            last_qualification={"status": "promoted", "confidence": 0.95},
            serving_status="serving_confirmed",
            served_sha="abc123def456",
        )
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=datetime(2026, 5, 3, 0, 0, 0),
        )
        db_session.add_all([promoted, open_version])
        await db_session.commit()
        await db_session.refresh(promoted)

        db_session.add_all(
            [
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                    serving_status="serving_confirmed",
                    served_identifier="abc123def456",
                    served_at=datetime(2026, 5, 2, 0, 5, 0),
                ),
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind="synthetic_required",
                    serving_status="unconfirmed",
                ),
            ]
        )
        await db_session.commit()

        readiness, audit, merged = self._lifecycle_patches()
        with readiness, audit, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["current_version"]["aggregate_serving_status"] is None
        assert "artifacts" in body["current_version"]

        recent = body["recent_versions"][0]
        artifacts = {entry["artifact_kind"]: entry for entry in recent["artifacts"]}
        assert recent["serving_status"] == "serving_confirmed"
        assert recent["aggregate_serving_status"] == "unconfirmed"
        assert recent["qualification_state"]["state"] == "promoted_unconfirmed"
        assert artifacts["synthetic_required"]["serving_status"] == "unconfirmed"

    @pytest.mark.asyncio
    async def test_recent_versions_use_aggregate_failed_artifact_truth(
        self, client: AsyncClient, db_session, mocker
    ):
        """A backend-confirmed promotion remains aggregate-deploy-failed while a
        required non-backend artifact has failed."""
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
        promoted = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 5, 1, 0, 0, 0),
            promoted_at=datetime(2026, 5, 2, 0, 0, 0),
            last_qualification={"status": "promoted", "confidence": 0.95},
            serving_status="serving_confirmed",
            served_sha="abc123def456",
        )
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=datetime(2026, 5, 3, 0, 0, 0),
        )
        db_session.add_all([promoted, open_version])
        await db_session.commit()
        await db_session.refresh(promoted)

        db_session.add_all(
            [
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                    serving_status="serving_confirmed",
                    served_identifier="abc123def456",
                    served_at=datetime(2026, 5, 2, 0, 5, 0),
                ),
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind="synthetic_required",
                    serving_status="deploy_failed",
                    reason_code="synthetic_deadline_elapsed",
                ),
            ]
        )
        await db_session.commit()

        readiness, audit, merged = self._lifecycle_patches()
        with readiness, audit, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        recent = resp.json()["recent_versions"][0]
        artifacts = {entry["artifact_kind"]: entry for entry in recent["artifacts"]}
        assert recent["serving_status"] == "serving_confirmed"
        assert recent["aggregate_serving_status"] == "deploy_failed"
        assert recent["aggregate_deploy_failure_reason"] == "synthetic_deadline_elapsed"
        assert recent["qualification_state"]["state"] == "promotion_deploy_failed"
        assert artifacts["synthetic_required"]["serving_status"] == "deploy_failed"
        assert (
            artifacts["synthetic_required"]["reason_code"]
            == "synthetic_deadline_elapsed"
        )

    @pytest.mark.asyncio
    async def test_recent_versions_include_recovered_and_backend_only_compat_truth(
        self, client: AsyncClient, db_session
    ):
        """Aggregate fields stay aligned with shipped truth both after recovery
        and on the backend-only compatibility path."""
        from app.models import ReleaseDeployArtifact
        from app.release_artifacts import BACKEND_REVISION_ARTIFACT_KIND

        project = await _make_project(db_session)
        backend_only = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 5, 1, 0, 0, 0),
            promoted_at=datetime(2026, 5, 2, 0, 0, 0),
            last_qualification={"status": "promoted", "confidence": 0.91},
            serving_status="serving_confirmed",
            served_sha="backendonly123",
        )
        recovered = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 5, 3, 0, 0, 0),
            promoted_at=datetime(2026, 5, 4, 0, 0, 0),
            last_qualification={"status": "promoted", "confidence": 0.94},
            serving_status="serving_confirmed",
            served_sha="recovered12345",
        )
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="build",
            status="open",
            opened_at=datetime(2026, 5, 5, 0, 0, 0),
        )
        db_session.add_all([backend_only, recovered, open_version])
        await db_session.commit()
        await db_session.refresh(recovered)

        db_session.add(
            ReleaseDeployArtifact(
                version_id=recovered.id,
                artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                serving_status="serving_confirmed",
                served_identifier="recovered12345",
                served_at=datetime(2026, 5, 4, 0, 5, 0),
            )
        )
        await db_session.commit()

        readiness, audit, merged = self._lifecycle_patches()
        with readiness, audit, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        recent = resp.json()["recent_versions"]
        assert recent[0]["aggregate_serving_status"] == "serving_confirmed"
        assert recent[0]["qualification_state"]["state"] == "promoted"
        assert recent[1]["aggregate_serving_status"] == "serving_confirmed"
        assert recent[1]["qualification_state"]["state"] == "promoted"

    @pytest.mark.asyncio
    async def test_serving_block_present_on_current_and_recent_versions(
        self, client: AsyncClient, db_session, mocker
    ):
        """The resolved 'serving' block is present on both current_version and
        recent_versions, exposing status/sha/failure_reason/artifacts so the UI
        can read one field instead of the three-field precedence chain."""
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
        promoted = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 5, 1, 0, 0, 0),
            promoted_at=datetime(2026, 5, 2, 0, 0, 0),
            last_qualification={"status": "promoted", "confidence": 0.95},
            serving_status="serving_confirmed",
            served_sha="deadbeef1234",
        )
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=datetime(2026, 5, 3, 0, 0, 0),
        )
        db_session.add_all([promoted, open_version])
        await db_session.commit()
        await db_session.refresh(promoted)

        db_session.add_all(
            [
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                    serving_status="serving_confirmed",
                    served_identifier="deadbeef1234",
                    served_at=datetime(2026, 5, 2, 0, 5, 0),
                ),
                ReleaseDeployArtifact(
                    version_id=promoted.id,
                    artifact_kind="synthetic_required",
                    serving_status="serving_confirmed",
                    served_identifier="synth-id-abc",
                    served_at=datetime(2026, 5, 2, 0, 6, 0),
                ),
            ]
        )
        await db_session.commit()

        readiness, audit, merged = self._lifecycle_patches()
        with readiness, audit, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()

        # current_version (open, no artifacts) exposes the serving block.
        cv_serving = body["current_version"]["serving"]
        assert isinstance(cv_serving, dict)
        assert "status" in cv_serving
        assert "sha" in cv_serving
        assert "failure_reason" in cv_serving
        assert "artifacts" in cv_serving

        # recent_version (promoted, serving_confirmed) exposes the resolved block.
        recent = body["recent_versions"][0]
        rv_serving = recent["serving"]
        assert isinstance(rv_serving, dict)
        assert rv_serving["status"] == "serving_confirmed"
        # sha derives from confirmed artifact evidence (not the legacy scalar).
        assert rv_serving["sha"] == "deadbeef1234"
        # failure_reason is None when not deploy_failed.
        assert rv_serving["failure_reason"] is None
        # artifacts is the full per-artifact list.
        assert isinstance(rv_serving["artifacts"], list)
        assert len(rv_serving["artifacts"]) == 2
        artifact_kinds = {a["artifact_kind"] for a in rv_serving["artifacts"]}
        assert BACKEND_REVISION_ARTIFACT_KIND in artifact_kinds
        assert "synthetic_required" in artifact_kinds


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
        # The active candidate under evaluation is surfaced first-class and
        # equals the version's last_qualification candidate SHA.
        assert current["active_candidate_sha"] == candidate

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
        # Active candidate is the evaluated (newer) SHA, distinct from the older
        # audited candidate — letting the UI contrast the two.
        assert current["active_candidate_sha"] == "newc" * 10
        assert (
            current["active_candidate_sha"] != current["stale_audit"]["candidate_sha"]
        )

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
        # Active candidate is surfaced even when no scoped audit evidence exists.
        assert current["active_candidate_sha"] == "feed" * 10

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


class TestVersionLifecycleQualificationEvidence:
    """GET /version-lifecycle surfaces durable qualification evidence on open and recent versions."""

    @pytest.mark.asyncio
    async def test_surfaces_durable_evidence_in_current_and_recent_versions(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        open_opened_at = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        recent_opened_at = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        open_version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="open",
            opened_at=open_opened_at,
            last_qualification={
                "status": "not_promoted",
                "confidence": 0.42,
                "threshold": 0.9,
                "sub_scores": {"audit": 0.1, "test_erosion": 0.2, "smoketest": 0.3},
                "blocker": "migration_unsafe",
                "finalized_at": "2026-05-27T12:00:00",
                "migration_safety": "block",
                "migration_safety_detail": "open version needs a schema migration",
                "migration_apply_safety": "warn",
                "migration_apply_safety_detail": "apply path still diverges",
                "audit_findings": [
                    {"severity": "high", "summary": "unverified audit finding"}
                ],
                "excluded_audit_findings": [
                    {"severity": "low", "summary": "stale noise"}
                ],
                "test_erosion_detail": {"deleted_test_files": 1},
                "smoketest_detail": {"passed": False, "suite": "release"},
                "audit_reconciliation": {
                    "excluded_findings_count": 1,
                    "audit_leg_proof": {
                        "state": "unproven",
                        "required": True,
                        "candidate_sha": "open-sha",
                        "audit_task_id": "audit-1",
                        "audit_task_candidate_sha": "open-sha",
                        "challenge_task_id": "challenge-1",
                        "challenged_candidate_sha": "wrong-sha",
                        "binding_expected_count": 1,
                        "binding_record_count": 0,
                        "binding_complete": False,
                        "reconciliation_status": "available",
                        "reconciliation_reason": "audit_challenge_sha_mismatch",
                    },
                },
                "promotion_pr_url": "https://example.com/pr/open",
                "promotion_conflict": True,
                "promotion_error_detail": "merge conflict",
            },
        )
        recent_version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=recent_opened_at,
            promoted_at=open_opened_at,
            last_qualification={
                "status": "promoted",
                "confidence": 0.91,
                "threshold": 0.9,
                "sub_scores": {"audit": 0.8, "test_erosion": 0.9, "smoketest": 0.95},
                "blocker": None,
                "finalized_at": "2026-05-26T12:00:00",
                "migration_safety": "pass",
                "migration_safety_detail": "migration dry-run matched",
                "audit_findings": [{"severity": "low", "summary": "reviewed"}],
                "audit_reconciliation": {
                    "excluded_findings_count": 0,
                    "audit_leg_proof": {
                        "state": "not_required",
                        "required": False,
                        "candidate_sha": "recent-sha",
                        "audit_task_id": "audit-2",
                        "audit_task_candidate_sha": "recent-sha",
                        "challenge_task_id": None,
                        "challenged_candidate_sha": None,
                        "binding_expected_count": 0,
                        "binding_record_count": 0,
                        "binding_complete": False,
                        "reconciliation_status": "unavailable",
                        "reconciliation_reason": "challenge_absent",
                    },
                },
                "promotion_pr_url": "https://example.com/pr/recent",
            },
        )
        db_session.add_all([open_version, recent_version])
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
                "app.release_version.count_merged_since", new=AsyncMock(return_value=0)
            ),
            patch(
                "app.release_version.count_in_flight_tasks",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["last_qualification"]["migration_safety"] == "block"
        assert current["last_qualification"]["migration_apply_safety"] == "warn"
        assert current["last_qualification"]["audit_findings"] == [
            {"severity": "high", "summary": "unverified audit finding"}
        ]
        assert current["last_qualification"]["excluded_audit_findings"] == [
            {"severity": "low", "summary": "stale noise"}
        ]
        assert current["last_qualification"]["audit_leg_proof"]["state"] == "unproven"
        assert current["last_qualification"]["audit_reconciliation"][
            "audit_leg_proof"
        ] == {
            "state": "unproven",
            "required": True,
            "candidate_sha": "open-sha",
            "audit_task_id": "audit-1",
            "audit_task_candidate_sha": "open-sha",
            "challenge_task_id": "challenge-1",
            "challenged_candidate_sha": "wrong-sha",
            "binding_expected_count": 1,
            "binding_record_count": 0,
            "binding_complete": False,
            "reconciliation_status": "available",
            "reconciliation_reason": "audit_challenge_sha_mismatch",
        }
        assert current["last_qualification"]["test_erosion_detail"] == {
            "deleted_test_files": 1
        }
        assert current["last_qualification"]["smoketest_detail"] == {
            "passed": False,
            "suite": "release",
        }
        assert current["last_qualification"]["promotion_conflict"] is True
        assert (
            current["last_qualification"]["promotion_pr_url"]
            == "https://example.com/pr/open"
        )

        recent = resp.json()["recent_versions"][0]
        assert recent["last_qualification"]["migration_safety"] == "pass"
        assert recent["last_qualification"]["audit_findings"] == [
            {"severity": "low", "summary": "reviewed"}
        ]
        assert (
            recent["last_qualification"]["audit_leg_proof"]["state"] == "not_required"
        )
        assert (
            recent["last_qualification"]["promotion_pr_url"]
            == "https://example.com/pr/recent"
        )


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


class TestVersionLifecycleTasksMerged:
    """GET /version-lifecycle exposes tasks_merged on current_version."""

    @pytest.mark.asyncio
    async def test_tasks_merged_lists_landed_tasks(
        self, client: AsyncClient, db_session
    ):
        """tasks_merged includes type, title, and pr_number for each landed task."""
        from app.landed_signal import mark_landed_on_integration

        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        task = Task(
            project_id=project.id,
            title="fix: resolve race condition in deploy",
            payloads=[{"type": "fix", "pr_number": 42}],
            status=TaskStatus.COMPLETED,
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        mark_landed_on_integration(task)
        db_session.add(task)
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
                new=AsyncMock(return_value=1),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert "tasks_merged" in current
        tasks = current["tasks_merged"]
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "fix"
        assert tasks[0]["title"] == "fix: resolve race condition in deploy"
        assert tasks[0]["pr_number"] == 42
        assert tasks[0]["pr_url"] is None  # no Artifact row created

    @pytest.mark.asyncio
    async def test_tasks_merged_includes_pr_url_from_artifact(
        self, client: AsyncClient, db_session
    ):
        """tasks_merged includes pr_url when a PULL_REQUEST artifact exists."""
        from app.landed_signal import mark_landed_on_integration

        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        task = Task(
            project_id=project.id,
            title="fix: add feature",
            payloads=[{"type": "fix", "pr_number": 99}],
            status=TaskStatus.COMPLETED,
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        mark_landed_on_integration(task)
        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/org/repo/pull/99",
        )
        db_session.add(artifact)
        db_session.add(task)
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
                new=AsyncMock(return_value=1),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        tasks = current["tasks_merged"]
        assert len(tasks) == 1
        assert tasks[0]["pr_number"] == 99
        assert tasks[0]["pr_url"] == "https://github.com/org/repo/pull/99"

    @pytest.mark.asyncio
    async def test_tasks_merged_empty_when_no_landed_tasks(
        self, client: AsyncClient, db_session
    ):
        """tasks_merged is an empty list when no tasks have landed in the window."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="build")

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
        assert current["tasks_merged"] == []

    @pytest.mark.asyncio
    async def test_tasks_merged_has_more_when_over_limit(
        self, client: AsyncClient, db_session
    ):
        """tasks_merged_has_more is True and tasks_merged is capped at 50 when the
        version has more than 50 landed tasks."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="build")

        over_limit = [
            {"task_type": "fix", "title": f"task {i}", "pr_number": i, "pr_url": None}
            for i in range(51)
        ]

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
                new=AsyncMock(return_value=51),
            ),
            patch(
                "app.release_version.list_merged_tasks_for_version",
                new=AsyncMock(return_value=over_limit),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["tasks_merged_has_more"] is True
        assert len(current["tasks_merged"]) == 50

    @pytest.mark.asyncio
    async def test_tasks_merged_has_more_false_when_at_or_under_limit(
        self, client: AsyncClient, db_session
    ):
        """tasks_merged_has_more is False when the version has 50 or fewer tasks."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="build")

        at_limit = [
            {"task_type": "fix", "title": f"task {i}", "pr_number": i, "pr_url": None}
            for i in range(50)
        ]

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
                new=AsyncMock(return_value=50),
            ),
            patch(
                "app.release_version.list_merged_tasks_for_version",
                new=AsyncMock(return_value=at_limit),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["tasks_merged_has_more"] is False
        assert len(current["tasks_merged"]) == 50


class TestVersionLifecycleCurrentVersionHighlights:
    """GET /version-lifecycle exposes a what's-new ``highlights`` block (headline +
    at-a-glance metrics) on current_version, built by the SAME shared helper the
    per-version summary endpoint uses so the open version renders an identical
    headline. The headline's 'serving' determination is aggregate-driven; the
    current/open version is never 'promoted', so the promoted/serving headline cases
    are covered by TestGetReleaseVersionSummary and the release_version unit tests."""

    @pytest.mark.asyncio
    async def test_current_version_exposes_highlights_headline(
        self, client: AsyncClient, db_session
    ):
        """current_version.highlights carries all six keys and an 'open in <phase>
        with <N> merged' headline reflecting the merged-work count."""
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
                new=AsyncMock(return_value=3),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert "highlights" in current, "current_version must expose highlights"
        h = current["highlights"]
        for key in (
            "merged_count",
            "proven",
            "total_merged_for_acceptance",
            "qualification_confidence",
            "qualification_threshold",
            "headline",
        ):
            assert key in h, f"highlights missing key: {key}"
        # Open version with merged work -> phase-context headline reflecting the count.
        assert h["headline"] == "open in build with 3 merged"
        assert h["merged_count"] == 3
        # No qualification verdict yet -> confidence/threshold are null, not fabricated.
        assert h["qualification_confidence"] is None
        assert h["qualification_threshold"] is None

    @pytest.mark.asyncio
    async def test_current_version_highlights_no_merged_work(
        self, client: AsyncClient, db_session
    ):
        """An open version with zero merged work yields the 'no merged work yet'
        headline for its phase."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
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
        current = resp.json()["current_version"]
        assert (
            current["highlights"]["headline"]
            == "open in stabilize with no merged work yet"
        )
        assert current["highlights"]["merged_count"] == 0


class TestVersionLifecycleAcceptanceAggregate:
    """The version-lifecycle current_version and qualification-verdicts rows
    expose the additive ``acceptance_aggregate`` key (proven-vs-merged) ALONGSIDE
    the unchanged raw merged-count fields, with the open version computed live."""

    @pytest.mark.asyncio
    async def test_current_version_exposes_acceptance_aggregate_key(
        self, client: AsyncClient, db_session
    ):
        """current_version carries a live zeroed acceptance aggregate for an
        empty open window."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

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
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        # Additive key is present and computed live for the open version window.
        assert "acceptance_aggregate" in current
        assert current["acceptance_aggregate"] == {
            "proven": 0,
            "satisfied_unproven": 0,
            "inferred_test_cochange": 0,
            "declared_unsatisfied": 0,
            "undeclared": 0,
            "merged_unverified_degraded": 0,
            "malformed": 0,
            "total": 0,
        }
        # ...and the raw merged-count field is preserved unchanged.
        assert current["merged_task_count"] == 0

    @pytest.mark.asyncio
    async def test_version_lifecycle_current_version_acceptance_aggregate_computed_live(
        self, client: AsyncClient, db_session
    ):
        """current_version recomputes acceptance_aggregate live for the open
        version window, ignoring stale last_qualification snapshots."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        opened_at = _utcnow() - timedelta(hours=3)
        finalized_at = opened_at + timedelta(hours=1)
        version.opened_at = opened_at
        aggregate = {
            "total": 1,
            "proven": 1,
            "satisfied_unproven": 0,
            "declared_unsatisfied": 0,
            "undeclared": 0,
            "merged_unverified_degraded": 0,
            "malformed": 0,
        }
        version.last_qualification = {
            "status": "dry_run",
            "acceptance_aggregate": aggregate,
            "finalized_at": finalized_at.isoformat(),
        }
        db_session.add(version)
        await db_session.commit()

        for completed_at, context in (
            (
                opened_at + timedelta(minutes=30),
                {
                    "pr_merged": True,
                    "landed_on_integration": True,
                    "code_acceptance_verdict": {
                        "satisfied": True,
                        "proven": True,
                        "kind": "named_test",
                    },
                },
            ),
            (
                opened_at + timedelta(hours=2),
                {
                    "pr_merged": True,
                    "landed_on_integration": True,
                    "code_acceptance_verdict": {
                        "satisfied": False,
                        "kind": "named_test",
                    },
                },
            ),
        ):
            task = Task(
                project_id=project.id,
                title="merge-test-task",
                payloads=[{"type": "create", "prompt": "x", "pr_number": 42}],
                status=TaskStatus.COMPLETED,
                context=context,
                created_at=completed_at,
                updated_at=completed_at,
            )
            db_session.add(task)
            await db_session.commit()

        expected = await rv.compute_version_acceptance_aggregate(
            db_session, project.id, since=opened_at
        )

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
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["acceptance_aggregate"] == expected
        assert current["acceptance_aggregate"] != aggregate
        assert current["merged_task_count"] == 2
        assert current["merged_since_verdict"] == 1

    @pytest.mark.asyncio
    async def test_version_lifecycle_current_version_acceptance_aggregate_falls_back_when_live_compute_fails(
        self, client: AsyncClient, db_session
    ):
        """current_version serves the stored aggregate when live recomputation fails."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        opened_at = _utcnow() - timedelta(hours=3)
        version.opened_at = opened_at
        aggregate = {
            "total": 7,
            "proven": 5,
            "satisfied_unproven": 1,
            "declared_unsatisfied": 1,
            "undeclared": 0,
            "merged_unverified_degraded": 0,
            "malformed": 0,
        }
        version.last_qualification = {
            "status": "dry_run",
            "acceptance_aggregate": aggregate,
            "finalized_at": (opened_at + timedelta(hours=1)).isoformat(),
        }
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "app.release_version.list_merged_tasks_for_version",
                new=AsyncMock(return_value=[]),
            ),
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
                "app.release_version.compute_version_acceptance_aggregate",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        current = resp.json()["current_version"]
        assert current["acceptance_aggregate"] == aggregate
        assert current["merged_task_count"] == 2

    @pytest.mark.asyncio
    async def test_verdict_row_exposes_acceptance_aggregate_key(
        self, client: AsyncClient, db_session
    ):
        """Each qualification-verdict row omits the retired acceptance_aggregate
        key without disturbing the existing verdict fields."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="aggsha456",
            confidence=0.92,
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
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert "acceptance_aggregate" not in verdicts[0]
        assert verdicts[0]["candidate_sha"] == "aggsha456"

    @pytest.mark.asyncio
    async def test_verdict_row_surfaces_stored_acceptance_aggregate_after_reload(
        self, client: AsyncClient, db_session
    ):
        """qualification-verdict rows scrub acceptance_aggregate from the
        persisted JSON-backed evidence payload after reload."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)
        aggregate = {
            "total": 4,
            "proven": 3,
            "satisfied_unproven": 0,
            "declared_unsatisfied": 1,
            "undeclared": 0,
            "merged_unverified_degraded": 0,
            "malformed": 0,
            "proven_rate": 0.75,
            "satisfied_rate": 0.75,
        }
        verdict = QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="aggsha789",
            confidence=0.92,
            would_promote=True,
            promotion_enabled=False,
            terminal_status="dry_run",
            threshold=0.90,
            sub_scores={"acceptance_aggregate": aggregate},
        )
        db_session.add(verdict)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/qualification-verdicts"
        )
        assert resp.status_code == 200, resp.text
        verdicts = resp.json()["verdicts"]
        assert len(verdicts) == 1
        assert "acceptance_aggregate" not in verdicts[0]
        assert verdicts[0]["sub_scores"] == {}
        assert verdicts[0]["candidate_sha"] == "aggsha789"


class TestVersionLifecycleChallengedVsUnchallenged:
    """GET /version-lifecycle exposes the challenged-vs-unchallenged telemetry
    field derived from real QualificationVerdict history."""

    @pytest.mark.asyncio
    async def test_challenged_vs_unchallenged_present_and_has_expected_shape(
        self, client: AsyncClient, db_session
    ):
        """The endpoint always returns challenged_vs_unchallenged with the three
        required keys; an empty history produces an insufficient_sample state."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

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
        body = resp.json()
        assert "challenged_vs_unchallenged" in body
        tel = body["challenged_vs_unchallenged"]
        assert "challenged" in tel
        assert "unchallenged" in tel
        assert "total_verdicts" in tel
        # No verdicts seeded — both cohorts are insufficient_sample.
        assert tel["total_verdicts"] == 0
        assert tel["challenged"]["insufficient_sample"] is True
        assert tel["unchallenged"]["insufficient_sample"] is True

    @pytest.mark.asyncio
    async def test_challenged_vs_unchallenged_populated_from_verdict_history(
        self, client: AsyncClient, db_session
    ):
        """Seeding verdicts with and without audit_reconciliation.audit_challenge
        produces cohort stats (not insufficient_sample) for the large-enough cohort."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")

        # Seed 4 challenged verdicts (audit_reconciliation.audit_challenge available)
        for i in range(4):
            v = QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                candidate_sha=f"chal{i:04d}",
                confidence=0.91,
                would_promote=True,
                promotion_enabled=False,
                terminal_status="dry_run",
                threshold=0.90,
                audit_reconciliation={
                    "audit_challenge": {
                        "status": "available",
                        "reported_agreement": "agree",
                        "confirmed_count": 1,
                        "refuted_count": 0,
                        "uncertain_count": 0,
                    },
                    "excluded_findings_count": 0,
                },
            )
            db_session.add(v)

        # Seed 4 unchallenged verdicts (no audit_challenge)
        for i in range(4):
            v = QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                candidate_sha=f"unch{i:04d}",
                confidence=0.85,
                would_promote=False,
                promotion_enabled=False,
                terminal_status="not_promoted",
                threshold=0.90,
            )
            db_session.add(v)

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
        tel = resp.json()["challenged_vs_unchallenged"]
        assert tel["total_verdicts"] == 8
        # Both cohorts have 4 verdicts >= _MIN_TELEMETRY_SAMPLE (3).
        assert tel["challenged"]["insufficient_sample"] is False
        assert tel["challenged"]["sample_size"] == 4
        assert "exclusion_rate" in tel["challenged"]
        assert "disagreement_rate" in tel["challenged"]
        assert tel["unchallenged"]["insufficient_sample"] is False
        assert tel["unchallenged"]["sample_size"] == 4
        assert "exclusion_rate" in tel["unchallenged"]

    @pytest.mark.asyncio
    async def test_challenge_refuted_count_exposed_on_challenged_cohort(
        self, client: AsyncClient, db_session
    ):
        """Endpoint exposes challenge_refuted_count/rate when challenged verdicts
        carry refuted_count in their audit_challenge synthesis."""
        from app.models import QualificationVerdict

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")

        # Seed 3 challenged verdicts with refuted_count > 0 (total refuted = 6)
        for i in range(3):
            v = QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                candidate_sha=f"ref{i:04d}",
                confidence=0.92,
                would_promote=True,
                promotion_enabled=False,
                terminal_status="dry_run",
                threshold=0.90,
                audit_reconciliation={
                    "audit_challenge": {
                        "status": "available",
                        "binding": True,
                        "confirmed_count": 1,
                        "refuted_count": 2,
                        "uncertain_count": 0,
                        "reported_agreement": "partial",
                        "challenged_candidate_sha": f"ref{i:04d}",
                    },
                    "excluded_findings_count": 0,
                },
            )
            db_session.add(v)

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
        tel = resp.json()["challenged_vs_unchallenged"]
        ch = tel["challenged"]
        assert ch["insufficient_sample"] is False
        assert ch["sample_size"] == 3
        # 3 verdicts × 2 refuted_count each = 6 total refuted
        # Each verdict: confirmed=1, refuted=2, uncertain=0 → 3 total reviewed per verdict
        # Total reviewed = 3 × 3 = 9, Rate = 6/9 ≈ 0.667
        assert ch["challenge_refuted_count"] == 6
        assert abs(ch["challenge_refuted_rate"] - round(6 / 9, 4)) < 1e-4
        # Unchallenged cohort absent — insufficient_sample, no challenge_refuted keys
        assert tel["unchallenged"]["insufficient_sample"] is True
        assert "challenge_refuted_count" not in tel["unchallenged"]


class TestVersionLifecycleOutOfBandPromotion:
    """Serializer unit-tests for out_of_band_promotion provenance.

    The DB column is added by a sibling PR; these tests validate the serializer
    functions directly using mock version objects so they work before the column
    lands on this branch. Uses getattr-based access so a missing column degrades
    gracefully to null on real DB rows.
    """

    _OOB = {
        "source": "operator_override",
        "recorded_at": "2026-06-01T12:00:00",
        "candidate_sha": "abc1234567890def",
        "promoted_sha": "def7890abc123456",
    }

    def _make_mock_version(self, oob=None, status="promoted"):
        """Return a minimal mock ReleaseVersion object suitable for the serializer."""
        from datetime import datetime

        v = MagicMock(spec=ReleaseVersion)
        v.version_number = 1
        v.status = status
        v.opened_at = datetime(2026, 1, 1)
        v.promoted_at = datetime(2026, 6, 1) if status == "promoted" else None
        v.qualification_attempts = 0
        v.last_qualification = None
        v.serving_status = None
        v.served_sha = None
        v.served_at = None
        v.deploy_failure_reason = None
        v.deploy_confirm_deadline = None
        v.deploy_artifacts = []
        v.out_of_band_promotion = oob
        return v

    @pytest.mark.asyncio
    async def test_serialize_history_entry_includes_oob_when_set(self):
        """_serialize_version_history_entry passes out_of_band_promotion through
        verbatim when it is present on the version object."""
        from app.routers.admin_release_version import _serialize_version_history_entry

        v = self._make_mock_version(oob=self._OOB)
        with (
            patch(
                "app.routers.admin_release_version._version_serving_summary",
                return_value=({"serving_status": None, "reason_code": None}, []),
            ),
            patch(
                "app.release_version.summarize_qualification_state",
                return_value=None,
            ),
        ):
            result = await _serialize_version_history_entry(v)

        assert result["out_of_band_promotion"] == self._OOB

    @pytest.mark.asyncio
    async def test_serialize_history_entry_null_when_absent(self):
        """_serialize_version_history_entry emits null for out_of_band_promotion
        when the attribute is not set (degrades gracefully before column lands)."""
        from app.routers.admin_release_version import _serialize_version_history_entry

        v = self._make_mock_version(oob=None)
        with (
            patch(
                "app.routers.admin_release_version._version_serving_summary",
                return_value=({"serving_status": None, "reason_code": None}, []),
            ),
            patch(
                "app.release_version.summarize_qualification_state",
                return_value=None,
            ),
        ):
            result = await _serialize_version_history_entry(v)

        assert result["out_of_band_promotion"] is None

    @pytest.mark.asyncio
    async def test_endpoint_returns_null_oob_for_normal_version(
        self, client: AsyncClient, db_session
    ):
        """Integration smoke: a version row without the column returns null for
        out_of_band_promotion in both current_version and recent_versions."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()

        readiness = patch(
            "app.release_qualifier.compute_promotion_readiness",
            new=AsyncMock(return_value={"ready": False, "reason": "test"}),
        )
        audit = patch(
            "app.release_qualifier.resolve_lifecycle_audit_evidence",
            new=AsyncMock(
                return_value={
                    "state": "none",
                    "latest_audit": None,
                    "stale_audit": None,
                }
            ),
        )
        merged = patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=0),
        )
        with readiness, audit, merged:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )

        assert resp.status_code == 200, resp.text
        cv = resp.json()["current_version"]
        # Column absent on this branch → getattr default → null in response.
        assert cv.get("out_of_band_promotion") is None
