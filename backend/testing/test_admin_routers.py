"""
Tests for admin routers:
1. admin_users.py  — user management + project member management
2. admin_projects.py — project CRUD + autonomous mode
3. admin_notifications.py — alerts, messages, playbook
4. admin_operator_triggers.py — manual operator triggers (self-audit, qualify,
   clear-wave-deps, rollup, findings)

Domain slices split out of this monolith into companion files (see the
``test_admin_routers__*.py`` naming convention; each is behavior-preserving):
  * __messages / __version_lifecycle / __runners / __users (earlier splits)
  * __projects — project CRUD, build-config, reset/archive, autonomous mode
  * __notifications — alerts, playbook, notification dispatch, direction settings
  * __problem_model — problem-model surface, pagination, pending confirmations
  * __strategic_directions — direction create/retire/churn, strategic messages
This file retains task-ops, qualification, release-version, compute-experiment,
strategist-registry, notification-config/evergreen settings (whose protected
conversational-Workday test_ids are pinned here by the protection manifest), and
the remaining admin-endpoint tests.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    Project,
    ProjectMember,
    LocalRunner,
    ReleaseVersion,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    Alert,
    ProjectMessage,
    MessageType,
    StrategicCycle,
    Direction,
    SystemMaturity,
    WakeDeliveryState,
    _utcnow,
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


def _assert_message_event_payload(
    payload,
    *,
    project_id,
    message_id: str,
    message_type: str,
    from_type: str,
    action: str,
    status: str = "open",
    excerpt: str | None = None,
):
    assert payload["message_id"] == message_id
    assert payload["type"] == message_type
    assert payload["from"] == from_type
    if action == "resolved":
        assert payload["status"] == "resolved"
    else:
        assert "status" not in payload
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project_id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == action
    assert payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}
    assert payload["message"]["id"] == message_id
    assert payload["message"]["type"] == message_type
    assert payload["message"]["from"] == from_type
    assert payload["message"]["status"] == status
    if excerpt is not None:
        assert payload["message"]["excerpt"] == excerpt
    assert len(json.dumps(payload, default=str).encode("utf-8")) < 16_384


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


def _patch_explicit_merge_target(
    mocker, project_id, pr_number: int, *, base_ref: str
):
    """Bind human-approved merge tests to one live immutable PR target."""
    target = {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": f"base-{pr_number}",
        "head_sha": f"head-{pr_number}",
        "base_ref": base_ref,
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
    }
    for patch_target in (
        "app.review_policy_merge._fetch_live_review_target",
        "app.review_merge_authority._fetch_live_review_target",
    ):
        mocker.patch(
            patch_target,
            new_callable=AsyncMock,
            return_value=dict(target),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# admin_users.py — User Management & Project Members
# (moved to test_admin_routers__users.py)
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# admin_projects.py — Project CRUD + Autonomous Mode
# ═══════════════════════════════════════════════════════════════════════════════


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
# admin_compute_jobs.py — Compute-job listing & kill
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeJobsPanel:
    @pytest.mark.asyncio
    async def test_compute_jobs_response_includes_vm_phase_and_sanitized_cause(
        self, client: AsyncClient, db_session
    ):
        """GET /api/admin/projects/{id}/compute-jobs must expose vm_phase and a
        sanitized provisioning_cause for a provisioning-phase terminal job, and
        a planted secret/signed-URL in the raw diagnostic must NOT leak into the
        response.
        """
        from datetime import timezone
        from app.models import ComputeJobStatus, ComputeJobOutcome

        project = await _make_project(db_session)
        job = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.TIMED_OUT,
            outcome=ComputeJobOutcome.TIMED_OUT,
            terminal_reason="compute job timed out while provisioning",
            # started_at is intentionally omitted (None) → provisioning phase
            completed_at=datetime.now(timezone.utc),
            polling_metadata={
                "requested_deadline_seconds": 1800,
                "provisioning_model": "SPOT",
                "compute_rpc_diagnostic": {
                    "operation": "create_instance",
                    "error_type": "QuotaExceededError",
                    "error_message": (
                        "RESOURCE_EXHAUSTED: quota exceeded "
                        "https://gcp.example.com/instances?token=LEAKED_TOKEN&key=LEAKED_KEY"
                    ),
                    "reason": "QUOTA_EXCEEDED",
                },
            },
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
        assert resp.status_code == 200, resp.text
        jobs = resp.json()["compute_jobs"]
        assert len(jobs) == 1
        data = jobs[0]

        # vm_phase must be exposed and correct for a job with no started_at
        assert data.get("vm_phase") == "provisioning", (
            f"vm_phase must be 'provisioning' for a job that never started; got {data.get('vm_phase')!r}"
        )

        # launch_contract must be promoted from polling_metadata
        contract = data.get("launch_contract")
        assert contract is not None, "launch_contract must be present in response"
        assert contract["requested_deadline_seconds"] == 1800
        assert contract["provisioning_model"] == "SPOT"

        # compute_rpc_diagnostic must be sanitized — no raw secrets
        diag = data.get("compute_rpc_diagnostic")
        assert diag is not None, "compute_rpc_diagnostic must be present"
        assert diag["error_type"] == "QuotaExceededError"
        assert diag["reason"] == "QUOTA_EXCEEDED"
        msg = diag.get("error_message", "")
        assert "LEAKED_TOKEN" not in msg, (
            "secret token must be redacted from error_message"
        )
        assert "LEAKED_KEY" not in msg, "secret key must be redacted from error_message"
        assert "[redacted]" in msg, "query string must be replaced with [redacted]"

    @pytest.mark.asyncio
    async def test_compute_jobs_response_runtime_phase_has_no_launch_contract(
        self, client: AsyncClient, db_session
    ):
        """A job that started (runtime phase) has vm_phase=runtime and no
        launch_contract promoted (runtime failures keep the original shape).
        """
        from datetime import timezone
        from app.models import ComputeJobStatus, ComputeJobOutcome

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)
        job = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.TIMED_OUT,
            outcome=ComputeJobOutcome.TIMED_OUT,
            terminal_reason="compute job deadline exceeded",
            started_at=now,
            completed_at=now,
            polling_metadata={
                "requested_deadline_seconds": 3600,
                "provisioning_model": "STANDARD",
            },
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
        assert resp.status_code == 200, resp.text
        data = resp.json()["compute_jobs"][0]
        assert data.get("vm_phase") == "runtime"

    @pytest.mark.asyncio
    async def test_compute_jobs_summary_across_window_boundary(
        self, client: AsyncClient, db_session
    ):
        """Summary block counts/spend/criterion are correct for mixed active+terminal
        jobs, and jobs outside the time window are excluded from totals."""
        from datetime import timezone
        from app.models import ComputeJobStatus, ComputeJobOutcome

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        # Active job — always included regardless of time window
        active_job = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.RUNNING,
        )
        # Terminal job inside the default 360-minute window
        terminal_in = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.SUCCEEDED,
            outcome=ComputeJobOutcome.SUCCESS,
            final_cost_usd=2.50,
            completed_at=now - timedelta(minutes=60),
        )
        # Terminal job outside the window (>360 min ago) — must be excluded
        terminal_out = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.FAILURE,
            final_cost_usd=1.00,
            completed_at=now - timedelta(minutes=500),
        )
        for obj in (active_job, terminal_in, terminal_out):
            db_session.add(obj)
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "summary" in body, "response must include a 'summary' block"
        s = body["summary"]

        assert s["total_jobs"] == 2, f"expected 2 in-window jobs, got {s['total_jobs']}"
        assert s["active_jobs"] == 1, f"expected 1 active job, got {s['active_jobs']}"
        assert s["terminal_jobs"] == 1, f"expected 1 terminal job, got {s['terminal_jobs']}"
        assert abs(s["total_spend_usd"] - 2.50) < 0.01, (
            f"expected spend $2.50, got {s['total_spend_usd']}"
        )
        assert s["outcome_breakdown"].get("SUCCEEDED") == 1, (
            f"outcome_breakdown must show SUCCEEDED=1; got {s['outcome_breakdown']}"
        )
        assert "FAILED" not in s["outcome_breakdown"], (
            "out-of-window FAILED job must not appear in outcome_breakdown"
        )
        # criterion_measured/met: no tasks with criteria in this fixture → both 0
        assert s["criterion_measured"] == 0
        assert s["criterion_met"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# admin_notifications.py — Alerts, Messages, Playbook
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# admin_notifications.py — Messages
# (moved to test_admin_routers__messages.py)
# ═══════════════════════════════════════════════════════════════════════════════


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
    async def test_strategic_session_settings(self, client: AsyncClient):
        """The strategic-session settings expose their contract
        defaults on GET, round-trip through PATCH, and reject bad values.

        Other builders reference these by name, so the names + defaults are
        pinned here: strategic_continuation_ttl_seconds (1500),
        strategic_local_hold_timeout_seconds (900),
        strategic_session_idle_tick_seconds (5),
        strategic_session_turn_timeout_seconds (120),
        strategic_session_exploration_interval_seconds (86400),
        strategic_session_max_unattended_seconds (7200).

        Note: the legacy ``strategic_session_enabled`` feature flag was retired
        from the settings contract; only the cadence/safety knobs remain."""
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["strategic_continuation_ttl_seconds"] == 1500
        assert data["strategic_local_hold_timeout_seconds"] == 900
        assert data["strategic_session_idle_tick_seconds"] == 5
        assert data["strategic_session_turn_timeout_seconds"] == 120
        assert data["strategic_session_exploration_interval_seconds"] == 86_400
        assert data["strategic_session_max_unattended_seconds"] == 7200

        # Integer knobs round-trip and clamp to their documented ranges.
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "strategic_continuation_ttl_seconds": 1800,
                "strategic_local_hold_timeout_seconds": 600,
                "strategic_session_idle_tick_seconds": 3,
                "strategic_session_turn_timeout_seconds": 300,
                "strategic_session_exploration_interval_seconds": 21_600,
                "strategic_session_max_unattended_seconds": 7200,
            },
        )
        assert resp.status_code == 200, resp.text
        data = (await client.get("/api/admin/settings")).json()
        assert data["strategic_continuation_ttl_seconds"] == 1800
        assert data["strategic_local_hold_timeout_seconds"] == 600
        assert data["strategic_session_idle_tick_seconds"] == 3
        assert data["strategic_session_turn_timeout_seconds"] == 300
        assert data["strategic_session_exploration_interval_seconds"] == 21_600
        assert data["strategic_session_max_unattended_seconds"] == 7200

        # Exploration alone supports an explicit disabled sentinel and a wider
        # one-week ceiling; semantic/operator/watch wakes remain unaffected.
        disabled = await client.patch(
            "/api/admin/settings",
            json={"strategic_session_exploration_interval_seconds": 0},
        )
        assert disabled.status_code == 200, disabled.text
        data = (await client.get("/api/admin/settings")).json()
        assert data["strategic_session_exploration_interval_seconds"] == 0
        weekly = await client.patch(
            "/api/admin/settings",
            json={"strategic_session_exploration_interval_seconds": 604_800},
        )
        assert weekly.status_code == 200, weekly.text
        data = (await client.get("/api/admin/settings")).json()
        assert data["strategic_session_exploration_interval_seconds"] == 604_800

        # Out-of-range / non-numeric are rejected. The continuation TTL and
        # local-hold timeout ceilings are lowered to 7200s (2h) so an operator
        # can never reintroduce a >12h local-pin wall.
        for key, bad_val in (
            ("strategic_continuation_ttl_seconds", 10),  # < 60
            ("strategic_continuation_ttl_seconds", 86_400),  # > 7200 ceiling
            ("strategic_local_hold_timeout_seconds", "x"),
            ("strategic_local_hold_timeout_seconds", 43_200),  # > 7200 ceiling
            ("strategic_session_idle_tick_seconds", 0),  # < 1
            ("strategic_session_turn_timeout_seconds", 99999),  # > 3600
            ("strategic_session_exploration_interval_seconds", 1),  # nonzero < 1h
            ("strategic_session_exploration_interval_seconds", 604_801),  # > 1 week
            ("strategic_session_min_tick_seconds", 600),  # retired ambiguous key
            ("strategic_session_max_unattended_seconds", 30),  # < 60
            ("strategic_session_max_unattended_seconds", 99999),  # > 28800
        ):
            bad = await client.patch("/api/admin/settings", json={key: bad_val})
            assert bad.status_code == 400, f"{key}={bad_val!r} should be rejected"

    @pytest.mark.asyncio
    async def test_patch_deploy_repo_identity(self, client: AsyncClient):
        """deploy_repo_identity accepts a normalized 'owner/repo', clears on null,
        rejects malformed values, and round-trips on GET."""
        resp = await client.patch(
            "/api/admin/settings", json={"deploy_repo_identity": " Acme/Widget "}
        )
        assert resp.status_code == 200, resp.text
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["deploy_repo_identity"] == "Acme/Widget"

        # null clears it.
        resp = await client.patch(
            "/api/admin/settings", json={"deploy_repo_identity": None}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["deploy_repo_identity"] is None

        # Malformed values are rejected (no slash / empty component / wrong type).
        for bad_val in ["no-slash", "acme/", "/widget", 123]:
            bad = await client.patch(
                "/api/admin/settings", json={"deploy_repo_identity": bad_val}
            )
            assert bad.status_code == 400, f"{bad_val!r} should be rejected"

    @pytest.mark.asyncio
    async def test_deploy_identity_resolved_in_settings(
        self, client: AsyncClient, monkeypatch
    ):
        """GET /api/admin/settings exposes deploy_identity_resolved — the actual
        resolved identity (env var + runtime_config override), not just the
        runtime_config value.  Operators use this to confirm that deploy-level
        serving confirmation is active/inactive."""
        # Without env var or runtime_config override, identity is unresolved.
        monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "deploy_identity_resolved" in data
        assert data["deploy_identity_resolved"] is None

        # Setting runtime_config makes it resolvable.
        await client.patch(
            "/api/admin/settings", json={"deploy_repo_identity": "acme/widget"}
        )
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deploy_identity_resolved"] == "acme/widget"

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
    async def test_patch_capability_holdback_fractions_round_trip_and_persist(
        self, client: AsyncClient, db_session
    ):
        """The three capability holdback fractions must PATCH, surface in GET,
        and persist into DeploymentState.runtime_config_json.

        Regression: PATCH previously rejected the keys via _KNOWN_SETTINGS_KEYS,
        so operators could not tune the rollout holdback for the three assigned
        capability trials from the admin settings API.
        """
        from app.models import DeploymentState

        payload = {
            "module_interfaces_holdback_fraction": 0.125,
            "related_tests_holdback_fraction": 0.25,
            "affected_consumers_holdback_fraction": 0.375,
        }

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch("/api/admin/settings", json=payload)
        assert resp.status_code == 200, resp.text

        get_resp = await client.get("/api/admin/settings")
        body = get_resp.json()
        for key, expected in payload.items():
            assert body[key] == expected

        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        for key, expected in payload.items():
            assert row.runtime_config_json[key] == expected

    @pytest.mark.asyncio
    async def test_patch_direction_refractory_settings_round_trip_and_fallback(
        self, client: AsyncClient, db_session
    ):
        """Direction refractory settings PATCH, surface in GET, persist, and fall back to cooldown when cleared."""
        from app.models import DeploymentState

        db_session.add(DeploymentState())
        await db_session.commit()

        payload = {
            "autonomous_direction_cooldown_minutes": 240,
            "direction_refractory_minutes": 180,
            "direction_saturation_refractory_minutes": 360,
        }

        resp = await client.patch("/api/admin/settings", json=payload)
        assert resp.status_code == 200, resp.text

        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_refractory_minutes"] == 180
        assert body["direction_saturation_refractory_minutes"] == 360

        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["direction_refractory_minutes"] == 180
        assert row.runtime_config_json["direction_saturation_refractory_minutes"] == 360

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "direction_refractory_minutes": None,
                "direction_saturation_refractory_minutes": None,
            },
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(row)
        assert "direction_refractory_minutes" not in row.runtime_config_json
        assert "direction_saturation_refractory_minutes" not in row.runtime_config_json
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        await sync_runtime_config_from_state(row)
        assert "direction_refractory_minutes" not in _shared_state.runtime_config
        assert (
            "direction_saturation_refractory_minutes"
            not in _shared_state.runtime_config
        )

        resp = await client.patch(
            "/api/admin/settings",
            json={"autonomous_direction_cooldown_minutes": 300},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(row)
        await sync_runtime_config_from_state(row)
        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_refractory_minutes"] == 300
        assert body["direction_saturation_refractory_minutes"] == 600

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
                json={
                    key: value for key, value in original.items() if value is not None
                },
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
    async def test_env_preflight_enabled_round_trip_and_persist(
        self, client: AsyncClient, db_session
    ):
        """env_preflight_enabled is settable via PATCH /api/admin/settings,
        visible on GET, persists into DeploymentState.runtime_config_json,
        and lands in the in-memory runtime_config — so an operator can disable
        the env_preflight dimension via the admin API to break a promotion
        deadlock without requiring a redeploy.

        Regression: the env_preflight_enabled was previously excluded from the
        runtime_config_json persist dict in _apply_settings, which meant an
        operator toggle survived only in-memory and was lost on any Cloud Run
        restart (reverting to the True default and re-creating the deadlock).
        This test guards the four wiring points (GET, PATCH apply, persist
        dict, config-sync restore key) to ensure the toggle is durable.
        """
        from app.models import DeploymentState
        from app.shared_state import runtime_config as _rc

        original = _rc.get("env_preflight_enabled", True)
        db_session.add(DeploymentState())
        await db_session.commit()
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"env_preflight_enabled": False},
            )
            assert resp.status_code == 200, resp.text

            # Round-trips on GET.
            body = (await client.get("/api/admin/settings")).json()
            assert body["env_preflight_enabled"] is False

            # Lands in the in-memory runtime_config the orchestrator reads.
            assert _rc.get("env_preflight_enabled") is False

            # Persists into the JSONB column so it survives a restart /
            # config-sync restore (not just one in-memory instance).
            row = await db_session.get(DeploymentState, 1)
            assert row is not None
            assert row.runtime_config_json is not None
            assert row.runtime_config_json["env_preflight_enabled"] is False

            # Re-enabling round-trips the other way too.
            resp = await client.patch(
                "/api/admin/settings",
                json={"env_preflight_enabled": True},
            )
            assert resp.status_code == 200, resp.text
            body = (await client.get("/api/admin/settings")).json()
            assert body["env_preflight_enabled"] is True
        finally:
            # Restore the default so the flag does not leak to other tests.
            await client.patch(
                "/api/admin/settings",
                json={"env_preflight_enabled": bool(original)},
            )

    @pytest.mark.asyncio
    async def test_no_ci_test_gate_enabled_round_trip_and_persist(
        self, client: AsyncClient, db_session
    ):
        """no_ci_test_gate_enabled (the no-CI verify_tests merge-gate kill-switch)
        is settable via PATCH, surfaces on GET, persists into
        DeploymentState.runtime_config_json, and lands in the in-memory
        runtime_config — so an operator can toggle the gate at runtime without a
        redeploy.

        Regression: PATCH previously rejected the key via _KNOWN_SETTINGS_KEYS
        (400 unknown key); the only way to disable the gate was the
        NO_CI_TEST_GATE_ENABLED env var, which needs a redeploy. Guards all four
        wiring points (allowlist, apply branch, GET response, persist dict) plus
        the config-sync restore key in runtime_config_state.py.
        """
        from app.models import DeploymentState
        from app.shared_state import runtime_config as _rc

        original = _rc.get("no_ci_test_gate_enabled", True)
        db_session.add(DeploymentState())
        await db_session.commit()
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"no_ci_test_gate_enabled": False},
            )
            assert resp.status_code == 200, resp.text

            # Round-trips on GET.
            body = (await client.get("/api/admin/settings")).json()
            assert body["no_ci_test_gate_enabled"] is False

            # Lands in the in-memory runtime_config the orchestrator reads.
            assert _rc.get("no_ci_test_gate_enabled") is False

            # Persists into the JSONB column so it survives a restart /
            # config-sync restore (not just one in-memory instance).
            row = await db_session.get(DeploymentState, 1)
            assert row is not None
            assert row.runtime_config_json is not None
            assert row.runtime_config_json["no_ci_test_gate_enabled"] is False

            # Re-enabling round-trips the other way too.
            resp = await client.patch(
                "/api/admin/settings",
                json={"no_ci_test_gate_enabled": True},
            )
            assert resp.status_code == 200, resp.text
            body = (await client.get("/api/admin/settings")).json()
            assert body["no_ci_test_gate_enabled"] is True
        finally:
            # Restore the default so the flag does not leak to other tests.
            await client.patch(
                "/api/admin/settings",
                json={"no_ci_test_gate_enabled": bool(original)},
            )

    @pytest.mark.asyncio
    async def test_capability_proof_execution_trials_enabled_round_trip_and_persist(
        self, client: AsyncClient, db_session
    ):
        """The executed-test earn path defaults on, while its operator kill switch
        round-trips through PATCH, GET, runtime config, and persisted settings."""
        from app.models import DeploymentState
        from app.shared_state import runtime_config as _rc

        original = _rc.get("capability_proof_execution_trials_enabled", True)
        # The complete discovery -> execution -> ingestion path is active by
        # default, so pre-existing working capabilities can earn ``proven``.
        body = (await client.get("/api/admin/settings")).json()
        assert body["capability_proof_execution_trials_enabled"] is True

        db_session.add(DeploymentState())
        await db_session.commit()
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"capability_proof_execution_trials_enabled": False},
            )
            assert resp.status_code == 200, resp.text

            body = (await client.get("/api/admin/settings")).json()
            assert body["capability_proof_execution_trials_enabled"] is False
            assert _rc.get("capability_proof_execution_trials_enabled") is False

            row = await db_session.get(DeploymentState, 1)
            assert row is not None
            assert row.runtime_config_json is not None
            assert (
                row.runtime_config_json["capability_proof_execution_trials_enabled"]
                is False
            )

            # Non-boolean is rejected.
            bad = await client.patch(
                "/api/admin/settings",
                json={"capability_proof_execution_trials_enabled": "yes"},
            )
            assert bad.status_code == 400
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"capability_proof_execution_trials_enabled": bool(original)},
            )

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
        """Two-key policies round-trip; legacy smoke weight is normalized away."""
        from app.shared_state import runtime_config as _rc

        original = _rc.get("promotion_weights")
        try:
            current = await client.patch(
                "/api/admin/settings",
                json={"promotion_weights": {"audit": 0.6, "test_erosion": 0.4}},
            )
            assert current.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["promotion_weights"] == pytest.approx(
                {"audit": 0.6, "test_erosion": 0.4}
            )

            legacy = await client.patch(
                "/api/admin/settings",
                json={
                    "promotion_weights": {
                        "audit": 0.5,
                        "test_erosion": 0.25,
                        "smoketest": 0.25,
                    }
                },
            )
            assert legacy.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["promotion_weights"] == pytest.approx(
                {"audit": 2 / 3, "test_erosion": 1 / 3}
            )
            assert "smoketest" not in body["promotion_weights"]

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
            bad_keys = await client.patch(
                "/api/admin/settings",
                json={"promotion_weights": {"audit": 1.0}},
            )
            assert bad_keys.status_code == 400
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
    async def test_merge_grade_block_threshold_round_trip(self, client: AsyncClient):
        """merge_grade_block_threshold is settable via PATCH, bounded to [0, 1],
        visible on GET, and defaults to 0.0 (ships dark)."""
        from app.shared_state import runtime_config as _rc

        original = _rc.get("merge_grade_block_threshold", 0.0)
        try:
            resp = await client.patch(
                "/api/admin/settings", json={"merge_grade_block_threshold": 1.0}
            )
            assert resp.status_code == 200
            body = (await client.get("/api/admin/settings")).json()
            assert body["merge_grade_block_threshold"] == pytest.approx(1.0)

            # 0.0 is allowed (disarms the gate).
            resp0 = await client.patch(
                "/api/admin/settings", json={"merge_grade_block_threshold": 0.0}
            )
            assert resp0.status_code == 200

            # Out of range rejected.
            assert (
                await client.patch(
                    "/api/admin/settings", json={"merge_grade_block_threshold": -0.1}
                )
            ).status_code == 400
            assert (
                await client.patch(
                    "/api/admin/settings", json={"merge_grade_block_threshold": 1.5}
                )
            ).status_code == 400
            # Non-numeric rejected.
            assert (
                await client.patch(
                    "/api/admin/settings",
                    json={"merge_grade_block_threshold": "high"},
                )
            ).status_code == 400
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"merge_grade_block_threshold": original},
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
    async def test_patch_notification_config_runner_events_are_valid(
        self, client: AsyncClient
    ):
        """The runner-health event types are accepted by PATCH validation
        so an operator can opt in (previously the allowlist rejected them as
        unknown events, silently blocking every runner-health external page)."""
        runner_events = [
            "RUNNER_DEACTIVATED",
            "RUNNER_DEGRADED",
            "RUNNER_DEGRADED_ESCALATED",
            "RUNNER_FLEET_OFFLINE",
            "RUNNER_LOW_DISK",
            "RUNNER_LOW_MEMORY",
            "RUNNER_OUTAGE_ESCALATED",
            "RUNNER_LOCAL_HOLD",
            "RUNNER_CONTAINER_HOLD",
        ]
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "notification_config": {
                    "webhook_url": "https://hooks.example.com/x",
                    "enabled_events": ["TASK_FAILED", *runner_events],
                    "throttle_seconds": 30,
                }
            },
        )
        assert resp.status_code == 200
        nc = resp.json()["notification_config"]
        for event in runner_events:
            assert event in nc["enabled_events"]

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


class TestEvergreenFillSettingsPersist:
    """evergreen_fill_* runtime knobs persist to DeploymentState.runtime_config_json
    and are restored by sync_runtime_config_from_state so every instance converges."""

    @pytest.mark.asyncio
    async def test_evergreen_fill_mode_persists_and_restores(
        self, client: AsyncClient, db_session
    ):
        """PATCH evergreen_fill_mode=observe writes to runtime_config_json;
        sync_runtime_config_from_state restores it on a fresh in-memory config."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "evergreen_fill_mode": "observe",
                "evergreen_fill_interval_minutes": 30,
                "evergreen_fill_util_threshold": 0.6,
                "evergreen_fill_min_free_slots": 3,
                "evergreen_fill_max_active": 2,
                "evergreen_fill_daily_cap": 5,
                "evergreen_fill_deferred_memo_enrollment": True,
            },
        )
        assert resp.status_code == 200, resp.text

        # GET /settings returns the updated values
        body = (await client.get("/api/admin/settings")).json()
        assert body["evergreen_fill_mode"] == "observe"
        assert body["evergreen_fill_interval_minutes"] == 30
        assert body["evergreen_fill_util_threshold"] == 0.6
        assert body["evergreen_fill_min_free_slots"] == 3
        assert body["evergreen_fill_max_active"] == 2
        assert body["evergreen_fill_daily_cap"] == 5
        assert body["evergreen_fill_deferred_memo_enrollment"] is True

        # Values are persisted to DeploymentState.runtime_config_json
        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["evergreen_fill_mode"] == "observe"
        assert row.runtime_config_json["evergreen_fill_interval_minutes"] == 30
        assert row.runtime_config_json["evergreen_fill_util_threshold"] == 0.6
        assert row.runtime_config_json["evergreen_fill_min_free_slots"] == 3
        assert row.runtime_config_json["evergreen_fill_max_active"] == 2
        assert row.runtime_config_json["evergreen_fill_daily_cap"] == 5
        assert (
            row.runtime_config_json["evergreen_fill_deferred_memo_enrollment"] is True
        )

        # Config-sync restore applies persisted values to a fresh in-memory config
        _shared_state.runtime_config["evergreen_fill_mode"] = "off"
        _shared_state.runtime_config["evergreen_fill_interval_minutes"] = 15
        _shared_state.runtime_config["evergreen_fill_util_threshold"] = 0.5
        _shared_state.runtime_config["evergreen_fill_min_free_slots"] = 2
        _shared_state.runtime_config["evergreen_fill_max_active"] = 1
        _shared_state.runtime_config["evergreen_fill_daily_cap"] = 3
        _shared_state.runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

        await sync_runtime_config_from_state(row)

        assert _shared_state.runtime_config["evergreen_fill_mode"] == "observe"
        assert _shared_state.runtime_config["evergreen_fill_interval_minutes"] == 30
        assert _shared_state.runtime_config["evergreen_fill_util_threshold"] == 0.6
        assert _shared_state.runtime_config["evergreen_fill_min_free_slots"] == 3
        assert _shared_state.runtime_config["evergreen_fill_max_active"] == 2
        assert _shared_state.runtime_config["evergreen_fill_daily_cap"] == 5
        assert (
            _shared_state.runtime_config["evergreen_fill_deferred_memo_enrollment"]
            is True
        )

    @pytest.mark.asyncio
    async def test_evergreen_fill_mode_junk_falls_back(
        self, client: AsyncClient, db_session
    ):
        """Junk value in runtime_config_json for evergreen_fill_mode is ignored on restore."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        state = DeploymentState()
        state.runtime_config_json = {"evergreen_fill_mode": "invalid_junk"}
        db_session.add(state)
        await db_session.commit()

        _shared_state.runtime_config["evergreen_fill_mode"] = "off"
        await sync_runtime_config_from_state(state)
        # Junk value is ignored — original default survives
        assert _shared_state.runtime_config["evergreen_fill_mode"] == "off"

    @pytest.mark.asyncio
    async def test_usage_headroom_auto_arm_override_persists_and_restores(
        self, client: AsyncClient, db_session
    ):
        """An operator's usage_headroom auto-arm override (the mode='off'
        force-disarm kill switch and observation window) must land in
        DeploymentState.runtime_config_json so it survives the wholesale
        rebuild in _apply_settings and is restored on restart — otherwise the
        module default 'auto' takes over and the reconciler re-arms the gate
        against the operator's intent."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "usage_headroom_auto_arm_mode": "off",
                "usage_headroom_auto_arm_observation_minutes": 120,
            },
        )
        assert resp.status_code == 200, resp.text

        # Override is echoed into the persisted JSONB column, not dropped by
        # the wholesale rebuild.
        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["usage_headroom_auto_arm_mode"] == "off"
        assert (
            row.runtime_config_json["usage_headroom_auto_arm_observation_minutes"]
            == 120
        )

        # Config-sync restores the operator override onto a fresh in-memory
        # config that has drifted back to the module defaults.
        _shared_state.runtime_config["usage_headroom_auto_arm_mode"] = "auto"
        _shared_state.runtime_config[
            "usage_headroom_auto_arm_observation_minutes"
        ] = 60

        await sync_runtime_config_from_state(row)

        assert _shared_state.runtime_config["usage_headroom_auto_arm_mode"] == "off"
        assert (
            _shared_state.runtime_config[
                "usage_headroom_auto_arm_observation_minutes"
            ]
            == 120
        )

    @pytest.mark.asyncio
    async def test_autonomous_spawn_budget_settings_persist_and_restore(
        self, client: AsyncClient, db_session
    ):
        """Initiative budget settings persist to runtime_config_json and sync back."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        payload = {
            "autonomous_spawn_budget_mode": "observe",
            "max_global_autonomous_initiative_tasks_per_hour": 7,
            "autonomous_spawn_budget_categories": {
                "initiative": ["research", "create", "strategic_review"],
                "servicing": ["review", "fix", "perf"],
            },
        }
        expected_categories = {
            "initiative": ["create", "research", "strategic_review"],
            "servicing": ["fix", "perf", "review"],
        }
        resp = await client.patch("/api/admin/settings", json=payload)
        assert resp.status_code == 200, resp.text

        body = (await client.get("/api/admin/settings")).json()
        assert body["autonomous_spawn_budget_mode"] == "observe"
        assert body["max_global_autonomous_initiative_tasks_per_hour"] == 7
        assert body["autonomous_spawn_budget_categories"] == expected_categories

        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["autonomous_spawn_budget_mode"] == "observe"
        assert (
            row.runtime_config_json["max_global_autonomous_initiative_tasks_per_hour"]
            == 7
        )
        assert row.runtime_config_json["autonomous_spawn_budget_categories"] == (
            expected_categories
        )

        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "off"
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = 1
        _shared_state.runtime_config["autonomous_spawn_budget_categories"] = {
            "initiative": ["research"],
            "servicing": ["review"],
        }

        changed = await sync_runtime_config_from_state(row)

        assert "autonomous_spawn_budget_mode" in changed
        assert "max_global_autonomous_initiative_tasks_per_hour" in changed
        assert "autonomous_spawn_budget_categories" in changed
        assert _shared_state.runtime_config["autonomous_spawn_budget_mode"] == "observe"
        assert (
            _shared_state.runtime_config[
                "max_global_autonomous_initiative_tasks_per_hour"
            ]
            == 7
        )
        assert _shared_state.runtime_config["autonomous_spawn_budget_categories"] == (
            expected_categories
        )

    @pytest.mark.asyncio
    async def test_autonomous_spawn_budget_mode_graduation_resolves_debt_via_settings(
        self, client: AsyncClient, db_session
    ):
        """PATCH /api/admin/settings resolves the seeded dormant debt when the
        spawn-budget mode graduates to enforce."""
        from app.models import CompletionDebtEntry, DeploymentState
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        seeded = CompletionDebtEntry(
            project_id=None,
            dedupe_key="dormant_knob:autonomous_spawn_budget_mode",
            kind="dormant_knob",
            title="Autonomous spawn-budget mode observe-only pending dedicated rollout",
        )
        db_session.add(seeded)
        await db_session.commit()
        await db_session.refresh(seeded)

        original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "observe"
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"autonomous_spawn_budget_mode": "enforce"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["autonomous_spawn_budget_mode"] == "enforce"

            await db_session.refresh(seeded)
            assert seeded.status == "resolved"
            assert seeded.resolution_reason == "graduated:enforce"
        finally:
            _shared_state.runtime_config["autonomous_spawn_budget_mode"] = (
                original_mode
            )

    @pytest.mark.asyncio
    async def test_autonomous_spawn_budget_settings_validate_categories(
        self, client: AsyncClient
    ):
        overlap = await client.patch(
            "/api/admin/settings",
            json={
                "autonomous_spawn_budget_categories": {
                    "initiative": ["research", "review"],
                    "servicing": ["review"],
                }
            },
        )
        assert overlap.status_code == 400
        assert "overlap" in overlap.text

        unknown_type = await client.patch(
            "/api/admin/settings",
            json={
                "autonomous_spawn_budget_categories": {
                    "initiative": ["research", "not_a_task_type"],
                }
            },
        )
        assert unknown_type.status_code == 400
        assert "unknown task type" in unknown_type.text

    @pytest.mark.asyncio
    async def test_structural_health_audit_mode_persists_across_sync(
        self, client: AsyncClient, db_session
    ):
        """PATCH structural_health_audit_mode=enforce writes to runtime_config_json;
        sync_runtime_config_from_state restores it on a fresh in-memory config."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "structural_health_audit_mode": "enforce",
                "structural_health_audit_interval_minutes": 120,
                "structural_health_audit_fat_row_bytes": 2097152,
            },
        )
        assert resp.status_code == 200, resp.text

        # GET /settings returns the updated values
        body = (await client.get("/api/admin/settings")).json()
        assert body["structural_health_audit_mode"] == "enforce"
        assert body["structural_health_audit_interval_minutes"] == 120
        assert body["structural_health_audit_fat_row_bytes"] == 2097152

        # Values are persisted to DeploymentState.runtime_config_json
        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["structural_health_audit_mode"] == "enforce"
        assert row.runtime_config_json["structural_health_audit_interval_minutes"] == 120
        assert row.runtime_config_json["structural_health_audit_fat_row_bytes"] == 2097152

        # Config-sync restore applies persisted values to a fresh in-memory config
        _shared_state.runtime_config["structural_health_audit_mode"] = "off"
        _shared_state.runtime_config["structural_health_audit_interval_minutes"] = 60
        _shared_state.runtime_config["structural_health_audit_fat_row_bytes"] = 1048576

        await sync_runtime_config_from_state(row)

        assert _shared_state.runtime_config["structural_health_audit_mode"] == "enforce"
        assert _shared_state.runtime_config["structural_health_audit_interval_minutes"] == 120
        assert _shared_state.runtime_config["structural_health_audit_fat_row_bytes"] == 2097152

    @pytest.mark.asyncio
    async def test_strategic_session_settings_persist_across_sync(
        self, client: AsyncClient, db_session
    ):
        """The strategic-session settings persist into
        DeploymentState.runtime_config_json AND are restored by
        sync_runtime_config_from_state, so an enable/tune survives a Cloud Run
        restart and reaches the leader across instances (regression: they were
        omitted from both the persist dict and the sync-back reader, so enabling
        reverted on restart and a multi-instance PATCH never propagated)."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "strategic_continuation_ttl_seconds": 1800,
                "strategic_local_hold_timeout_seconds": 600,
                "strategic_session_idle_tick_seconds": 3,
                "strategic_session_turn_timeout_seconds": 300,
                "strategic_session_exploration_interval_seconds": 21_600,
                "strategic_session_max_unattended_seconds": 7200,
            },
        )
        assert resp.status_code == 200, resp.text

        # Values are persisted to DeploymentState.runtime_config_json.
        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["strategic_continuation_ttl_seconds"] == 1800
        assert row.runtime_config_json["strategic_local_hold_timeout_seconds"] == 600
        assert row.runtime_config_json["strategic_session_idle_tick_seconds"] == 3
        assert row.runtime_config_json["strategic_session_turn_timeout_seconds"] == 300
        assert (
            row.runtime_config_json[
                "strategic_session_exploration_interval_seconds"
            ]
            == 21_600
        )
        assert (
            row.runtime_config_json["strategic_session_max_unattended_seconds"] == 7200
        )

        # Config-sync restore applies persisted values to a fresh in-memory config
        # (mirrors what a cold Cloud Run instance / the leader does on boot).
        _shared_state.runtime_config["strategic_continuation_ttl_seconds"] = 1500
        _shared_state.runtime_config["strategic_local_hold_timeout_seconds"] = 900
        _shared_state.runtime_config["strategic_session_idle_tick_seconds"] = 5
        _shared_state.runtime_config["strategic_session_turn_timeout_seconds"] = 120
        _shared_state.runtime_config[
            "strategic_session_exploration_interval_seconds"
        ] = 14_400
        _shared_state.runtime_config["strategic_session_max_unattended_seconds"] = 7200

        await sync_runtime_config_from_state(row)

        assert _shared_state.runtime_config["strategic_continuation_ttl_seconds"] == 1800
        assert (
            _shared_state.runtime_config["strategic_local_hold_timeout_seconds"] == 600
        )
        assert _shared_state.runtime_config["strategic_session_idle_tick_seconds"] == 3
        assert (
            _shared_state.runtime_config["strategic_session_turn_timeout_seconds"] == 300
        )
        assert (
            _shared_state.runtime_config[
                "strategic_session_exploration_interval_seconds"
            ]
            == 21_600
        )
        assert (
            _shared_state.runtime_config["strategic_session_max_unattended_seconds"]
            == 7200
        )


# ═══════════════════════════════════════════════════════════════════════════════
# dispatch_external_notification — webhook dispatch logic
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# notification_config startup restore — survives Cloud Run restart
# ═══════════════════════════════════════════════════════════════════════════════


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
        stale_heartbeat = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
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
# Provider routing verdict in digest payload
# ---------------------------------------------------------------------------


class TestDigestProviderRoutingVerdict:
    """provider_routing_verdict appears in the digest effort_model_efficiency payload."""

    @pytest.mark.asyncio
    async def test_digest_provider_routing_verdict_present(
        self, client: AsyncClient, db_session
    ):
        """(a/b) The digest payload includes provider_routing_verdict inside
        effort_model_efficiency with the expected shape."""
        from testing.factories import make_task, make_project

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create a minimal set of completed tasks with provider data so that
        # compute_effort_model_efficiency produces by_model rows and
        # compute_provider_routing_verdict derives a verdict from them.
        # With fewer than 20 PR-eligible tasks per provider the verdict will
        # be INSUFFICIENT_DATA — sufficient to assert the field is present and
        # structurally sound.
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                provider="anthropic",
                model="claude-opus-4-8",
                payloads=[{"type": "create", "effort": "high"}],
                context={"pr_merged": True},
                api_call_summary={
                    "total_input_tokens": 100,
                    "total_output_tokens": 100,
                    "llm_call_count": 1,
                },
            )
        )
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                provider="deepseek",
                model="deepseek-chat",
                payloads=[{"type": "create", "effort": "low"}],
                context={"pr_merged": True},
                api_call_summary={
                    "total_input_tokens": 100,
                    "total_output_tokens": 100,
                    "llm_call_count": 1,
                },
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
        assert resp.status_code == 200
        data = resp.json()

        efficiency = data.get("effort_model_efficiency")
        assert efficiency is not None, "effort_model_efficiency missing from digest"

        verdict = efficiency.get("provider_routing_verdict")
        assert verdict is not None, (
            "provider_routing_verdict missing from effort_model_efficiency"
        )

        # Verify structural shape.
        assert verdict["state"] in ("ADOPT", "REJECT", "INSUFFICIENT_DATA")
        assert isinstance(verdict.get("recommended_action"), str)
        supporting = verdict.get("supporting")
        assert supporting is not None
        # Check a few known supporting keys exist.
        assert "deepseek_run_count" in supporting
        assert "anthropic_run_count" in supporting
        assert "cost_ratio" in supporting
        assert "verified_rate_delta_pct" in supporting


# ---------------------------------------------------------------------------
# Accrual alternative-projection tests
# ---------------------------------------------------------------------------


class TestAccrualAlternativeProjections:
    """Digest accrual_projection includes alternative_projections at 0.1/0.3/0.5 holdback fractions."""

    @pytest.mark.asyncio
    async def test_alternative_projections_present_with_nonzero_holdback(
        self, client: AsyncClient, db_session
    ):
        """accrual_projection.alternative_projections maps fraction strings to float|None."""
        from unittest.mock import AsyncMock, patch

        project = await _make_project(db_session)
        # Pre-populate a verdict for repo_map so the accrual path activates.
        verdict_entry = {
            "verdict": "continue-trial",
            "lift": None,
            "n_with": 5,
            "n_without": 5,
            "thresholds": {"min_n_per_arm": 30},
            "arm_composition": {
                "with_assigned": 5,
                "without_assigned": 5,
            },
        }
        # Stub persist to return our pre-set verdict (avoids a full session-within-session open).
        persisted = {"repo_map": dict(verdict_entry)}

        with (
            patch(
                "app.routers.admin_digest.persist_trial_verdicts_in_transaction",
                new=AsyncMock(return_value=persisted),
            ),
            patch(
                "app.routers.admin_digest.compute_trial_arm_accrual",
                new=AsyncMock(
                    # with_7d/without_7d proportional to (1-0.3)/0.3 so rates are
                    # consistent with holdback_fraction=0.3, enabling the
                    # self-consistency check below.
                    return_value={"with_7d": 49, "without_7d": 21, "window_days": 7}
                ),
            ),
            patch(
                "app.routers.admin_digest._runtime_config",
                {"repo_map_holdback_fraction": 0.3},
                create=True,
            ) as _,
        ):
            # Inject holdback fraction via shared_state so the digest reads it.
            import app.shared_state as _shared_state

            orig = dict(_shared_state.runtime_config)
            _shared_state.runtime_config["repo_map_holdback_fraction"] = 0.3
            try:
                resp = await client.get(f"/api/admin/digest?project_id={project.id}")
            finally:
                _shared_state.runtime_config.clear()
                _shared_state.runtime_config.update(orig)

        assert resp.status_code == 200, resp.text
        verdicts = resp.json().get("capability_adoption_verdicts") or {}
        rm = verdicts.get("repo_map")
        assert rm is not None, "repo_map verdict missing from digest response"
        proj = rm.get("accrual_projection")
        assert proj is not None, "accrual_projection missing from repo_map verdict"
        alt = proj.get("alternative_projections")
        assert alt is not None, (
            "alternative_projections missing from accrual_projection"
        )
        # Exactly the three standard fractions must be present.
        assert set(alt.keys()) == {"0.1", "0.3", "0.5"}
        for frac_key, days_val in alt.items():
            assert days_val is None or isinstance(days_val, (int, float)), (
                f"alternative_projections[{frac_key!r}] must be float|None, got {days_val!r}"
            )
        # Self-consistency: alt projection at the CURRENT holdback fraction should
        # approximately equal days_to_min_n, because mock rates are proportional to
        # holdback (with_7d/without_7d = 49/21 ≈ 0.7/0.3), so redistributing at the
        # same fraction reproduces the original arm rates exactly.
        days_to_min_n = proj.get("days_to_min_n")
        assert days_to_min_n is not None
        alt_at_current = alt.get("0.3")
        assert alt_at_current is not None
        assert abs(alt_at_current - days_to_min_n) < 0.5, (
            f"alt_projection at current holdback ({alt_at_current}) should ≈ "
            f"days_to_min_n ({days_to_min_n})"
        )


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


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Version Lifecycle
# (moved to test_admin_routers__version_lifecycle.py)
# ═══════════════════════════════════════════════════════════════════════════════

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
            audit_reconciliation={
                "reconciled": True,
                "audit_leg_proof": {
                    "state": "proven",
                    "required": True,
                    "candidate_sha": "blockedsha",
                    "audit_task_id": "audit-1",
                    "audit_task_candidate_sha": "blockedsha",
                    "challenge_task_id": "challenge-1",
                    "challenged_candidate_sha": "blockedsha",
                    "binding_expected_count": 1,
                    "binding_record_count": 1,
                    "binding_complete": True,
                    "reconciliation_status": "available",
                    "reconciliation_reason": None,
                },
            },
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
        assert v["audit_leg_proof"]["state"] == "proven"
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


class TestGetTaskWakeRegistration:
    @pytest.mark.asyncio
    async def test_task_detail_returns_null_then_latest_wake_registration(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.PARKED,
            payloads=[{"type": "create"}],
            parked_at=datetime(2026, 6, 1, 12, 0, 0),
            park_reason="Waiting on compute job",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["wake_registration"] is None

        old_requested_at = datetime(2026, 6, 1, 12, 1, 0)
        latest_requested_at = old_requested_at + timedelta(minutes=5)
        db_session.add(
            TaskWakeRegistration(
                task_id=task.id,
                project_id=project.id,
                wake_source_kind="compute_job_terminal",
                wake_source_ref="old-job",
                requested_at=old_requested_at,
                delivery_state=WakeDeliveryState.PENDING,
            )
        )
        db_session.add(
            TaskWakeRegistration(
                task_id=task.id,
                project_id=project.id,
                wake_source_kind="compute_job_terminal",
                wake_source_ref="job-123",
                requested_at=latest_requested_at,
                deadline_at=datetime(2026, 6, 1, 13, 0, 0),
                fired_at=datetime(2026, 6, 1, 12, 10, 0),
                delivery_state=WakeDeliveryState.DELIVERED,
                result_state="resumed",
                result_detail="Task re-queued after compute job completed",
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/{task.id}")

        assert resp.status_code == 200, resp.text
        wake_registration = resp.json()["wake_registration"]
        assert wake_registration == {
            "wake_source_kind": "compute_job_terminal",
            "wake_source_ref": "job-123",
            "delivery_state": "DELIVERED",
            "deadline_at": "2026-06-01T13:00:00",
            "fired_at": "2026-06-01T12:10:00",
            "result_state": "resumed",
            "result_detail": "Task re-queued after compute job completed",
        }


class TestGetTaskVmCostDrivers:
    """GET /api/admin/tasks/{task_id} surfaces VM cost-driver fields
    (vm_machine_type, vm_spot, vm_cold_start_ms, vm_warm_start) so the
    task detail drawer can explain why a task cost what it did."""

    @pytest.mark.asyncio
    async def test_vm_cost_driver_fields_present_in_task_detail(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            vm_machine_type="n2d-standard-4",
            vm_spot=True,
            vm_cold_start_ms=4200,
            vm_warm_start=False,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["vm_machine_type"] == "n2d-standard-4"
        assert data["vm_spot"] is True
        assert data["vm_cold_start_ms"] == 4200
        assert data["vm_warm_start"] is False

    @pytest.mark.asyncio
    async def test_vm_cost_driver_fields_warm_start(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
            vm_machine_type="n2d-standard-2",
            vm_spot=False,
            vm_cold_start_ms=None,
            vm_warm_start=True,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["vm_machine_type"] == "n2d-standard-2"
        assert data["vm_spot"] is False
        assert data["vm_cold_start_ms"] is None
        assert data["vm_warm_start"] is True

    @pytest.mark.asyncio
    async def test_vm_cost_driver_fields_null_when_unset(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["vm_machine_type"] is None
        assert data["vm_spot"] is False
        assert data["vm_cold_start_ms"] is None


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

        return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)).isoformat()

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
                {
                    "severity": "high",
                    "summary": "Renumber-skip leaves table uncreated.",
                },
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
                {
                    "name": "test_login",
                    "status": "fail" if not passed else "pass",
                    "message": "auth error",
                },
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


def _make_debug_payload(
    task_id, *, confidence="high", fix_prompt="Fix the auth module"
):
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
    async def test_spawns_fix_task_and_alert_on_failure(
        self, db_session, mock_broadcast
    ):
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

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_smoketest_fix(
                smoketest_task, payload, db_session, str(smoketest_task.id)
            )
            await db_session.commit()

        spawned = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == smoketest_task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(spawned) == 1, "Expected exactly one spawned fix task"
        fix_task = spawned[0]
        assert fix_task.status == TaskStatus.QUEUED
        assert "smoketest" in fix_task.title.lower() or "fix" in fix_task.title.lower()
        assert fix_task.project_id == project.id

        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(Alert.task_id == smoketest_task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0].severity == "ERROR"

    @pytest.mark.asyncio
    async def test_dedup_skips_spawn_when_fix_task_exists(
        self, db_session, mock_broadcast
    ):
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

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_smoketest_fix(
                smoketest_task, payload, db_session, str(smoketest_task.id)
            )
            await db_session.commit()

        children = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == smoketest_task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, "Dedup: must not spawn a second fix task"

    @pytest.mark.asyncio
    async def test_skips_spawn_for_qualifier_smoketest(
        self, db_session, mock_broadcast
    ):
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

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_smoketest_fix(
                smoketest_task, payload, db_session, str(smoketest_task.id)
            )
            await db_session.commit()

        children = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == smoketest_task.id)
                )
            )
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

        payload = _make_debug_payload(
            debug_task.id, confidence="high", fix_prompt="Patch the auth module"
        )

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_debug_create(
                debug_task, payload, db_session, str(debug_task.id)
            )
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

        payload = _make_debug_payload(
            debug_task.id, confidence="low", fix_prompt="maybe fix here"
        )

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_debug_create(
                debug_task, payload, db_session, str(debug_task.id)
            )
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

        payload = _make_debug_payload(
            debug_task.id, confidence="high", fix_prompt="Fix it"
        )

        with patch(
            "app.agent_status_spawn.broadcast_to_project", new_callable=AsyncMock
        ):
            await maybe_spawn_debug_create(
                debug_task, payload, db_session, str(debug_task.id)
            )
            await db_session.commit()

        fix_tasks = (
            (await db_session.execute(select(Task).where(Task.title.like("Fix:%"))))
            .scalars()
            .all()
        )
        assert len(fix_tasks) == 0, (
            "Dedup: must not spawn fix when failed task already replaced by requeue"
        )


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
        """_apply_simple_mutations updates non-authority payload fields."""
        from app.admin_task_mutations import _apply_simple_mutations

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id)
        task.payloads = [{"type": "create", "prompt": "old"}]

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
        await _apply_simple_mutations(
            task, {"context": {"new_key": "new_val"}}, changes
        )

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

        with patch(
            "app.admin_task_mutations.broadcast_to_project", new_callable=AsyncMock
        ):
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
    async def test_admin_cancel_persists_provenance_and_cascades_dependents(
        self, db_session
    ):
        """Manual cancellation is durable and cannot become AGENT_TIMEOUT later."""
        from fastapi import BackgroundTasks

        from app.admin_helpers import modify_task_apply
        from app.failure_classification import (
            ADMIN_CANCEL,
            classify_failure,
            is_non_death_cancellation,
        )
        from app.models import TaskHistory

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id, TaskStatus.IN_PROGRESS)
        child = Task(
            project_id=project.id,
            parent_task_id=task.id,
            title="Active child",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        dependent = Task(
            project_id=project.id,
            title="Waiting dependent",
            status=TaskStatus.QUEUED,
            depends_on=[str(task.id)],
            created_by="ORCHESTRATOR",
        )
        unrelated = Task(
            project_id=project.id,
            title="Unrelated queued task",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add_all([child, dependent, unrelated])
        await db_session.commit()

        with (
            patch(
                "app.admin_task_mutations.broadcast_to_project",
                new_callable=AsyncMock,
            ),
            patch(
                "app.task_lifecycle.close_orphan_pr_if_any",
                new_callable=AsyncMock,
            ),
        ):
            await modify_task_apply(
                task=task,
                payload={"status": TaskStatus.CANCELLED.value},
                background_tasks=BackgroundTasks(),
                session=db_session,
            )

        for row in (task, child, dependent, unrelated):
            await db_session.refresh(row)

        assert task.status == TaskStatus.CANCELLED
        assert task.context["failure_category"] == ADMIN_CANCEL
        assert task.context["failure_class"] == ADMIN_CANCEL
        assert task.context["cancelled_by"] == "USER"
        assert classify_failure(task) == ADMIN_CANCEL
        assert is_non_death_cancellation(task)

        for cascaded in (child, dependent):
            assert cascaded.status == TaskStatus.CANCELLED
            assert cascaded.context["failure_category"] == ADMIN_CANCEL
            assert cascaded.context["failure_class"] == ADMIN_CANCEL
            assert cascaded.context["cancelled_by"] == "USER"
            assert cascaded.context["cancelled_by_task_id"] == str(task.id)
            assert is_non_death_cancellation(cascaded)
        assert dependent.latest_status_message.startswith("Cancelled: dependency task")
        assert unrelated.status == TaskStatus.QUEUED

        histories = (
            (
                await db_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(
            history.action == "MODIFIED"
            and (history.details or {}).get("modified_by") == "USER"
            for history in histories
        )

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
            payloads=[
                {"type": "research", "prompt": "rollup", "base_branch": "release/main"}
            ],
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
        _patch_explicit_merge_target(
            mocker,
            project.id,
            321,
            base_ref="release/main",
        )

        changes: dict = {}
        await _handle_rollup_pr_approval(
            task, task.context or {}, 321, db_session, changes
        )
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
        _patch_explicit_merge_target(
            mocker,
            project.id,
            654,
            base_ref="release/candidate-1",
        )

        changes: dict = {}
        await _handle_rollup_pr_approval(
            task, task.context or {}, 654, db_session, changes
        )
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
                {
                    "filename": "backend/alembic/versions/0002_dup.py",
                    "status": "added",
                    "patch": '+revision = "0002"\n+down_revision = "0001"',
                }
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

    @pytest.mark.asyncio
    async def test_rollup_approval_blocks_cross_branch_deployed_id_reuse(
        self, db_session, mocker
    ):
        """Regression: operator-approved rollup is blocked when the PR's new
        migration revision id is already deployed on the promotion_target under a
        DIFFERENT filename (the 0309-class collision) — even though the integration
        branch does not yet have that revision id.
        """
        from fastapi import HTTPException

        from app.admin_task_mutations import _handle_rollup_pr_approval

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "main"
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Rollup cross-branch id reuse",
            payloads=[
                {"type": "research", "prompt": "rollup", "base_branch": "automain"}
            ],
            status=TaskStatus.EVALUATING,
            context={"rollup_pr_number": 888},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("token", "owner", "repo"),
        )
        # PR adds 0309_performance_indexes.py (revision=0309, different body from
        # the deployed 0309_computejob_heartbeat.py on the promotion target).
        mocker.patch(
            "app.github.get_pr_files",
            new_callable=AsyncMock,
            return_value=[
                {
                    "filename": "backend/alembic/versions/0309_performance_indexes.py",
                    "status": "added",
                    "patch": '+revision = "0309"\n+down_revision = "0308"',
                }
            ],
        )
        mocker.patch(
            "app.github.get_pull_request_diff",
            new_callable=AsyncMock,
            return_value=(
                "diff --git a/backend/alembic/versions/0309_performance_indexes.py "
                "b/backend/alembic/versions/0309_performance_indexes.py\n"
                "+++ b/backend/alembic/versions/0309_performance_indexes.py\n"
                '+revision = "0309"\n'
                '+down_revision = "0308"\n'
            ),
        )

        def _list_versions_side_effect(token, owner, repo, ref):
            if ref == "main":
                # Deployed promotion target has 0309=heartbeat — different body.
                return ["0308_prev.py", "0309_computejob_heartbeat.py"]
            # Integration branch (automain) does not yet have 0309.
            return ["0308_prev.py"]

        mocker.patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
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
                task, task.context or {}, 888, db_session, changes
            )
        assert exc_info.value.status_code == 409
        assert "0309" in str(exc_info.value.detail)
        merge_mock.assert_not_called()
        await db_session.refresh(task)
        assert task.requires_approval is True
        assert "merged_pr" not in changes


# ═══════════════════════════════════════════════════════════════════════════════
# admin_operator_triggers.py — Manual Operator Triggers
# ═══════════════════════════════════════════════════════════════════════════════


class TestTriggerQualify:
    @pytest.mark.asyncio
    async def test_qualify_rejects_promotion_check_wait(
        self, client: AsyncClient, db_session
    ):
        """Re-running qualification while a managed promotion PR is open
        is rejected with a 409 — run_qualification no-ops (promotion_check_wait)
        and the endpoint surfaces it, mirroring the version-cut lever, instead of
        spawning a fresh audit/smoketest pair that clobbers the pending record."""
        from datetime import timedelta
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id, phase="stabilize")
        # A terminal audit child is not live, so only the managed-promotion
        # ownership guard catches it.
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
                "enabled_at": (_utcnow() - timedelta(minutes=45)).isoformat(),
            },
        }
        db_session.add(version)
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/qualify")
        assert resp.status_code == 409
        assert "managed promotion is pending" in resp.text
        # The qualify 409 surfaces the managed-wait age derived from enabled_at.
        assert "waiting ~" in resp.text

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

    @pytest.mark.asyncio
    async def test_qualify_forwards_requested_probes(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """The operator endpoint forwards ``probes`` to run_qualification —
        the only lever besides Strategic's qualify_request that can select the
        walkthrough_e2e live-evidence lane."""
        from app import release_qualifier

        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

        seen: dict = {}

        async def fake_run_qualification(session, project_id, version, **kwargs):
            seen.update(kwargs)
            return {"status": "spawned"}

        monkeypatch.setattr(
            release_qualifier, "run_qualification", fake_run_qualification
        )
        resp = await client.post(
            f"/api/admin/projects/{project.id}/qualify",
            json={"probes": ["walkthrough_e2e"]},
        )
        assert resp.status_code == 200
        assert seen["requested_probes"] == ["walkthrough_e2e"]
        assert seen["explicit_request"] is True

        # Omitting the body keeps the legacy probe-less contract.
        seen.clear()
        resp = await client.post(f"/api/admin/projects/{project.id}/qualify")
        assert resp.status_code == 200
        assert seen["requested_probes"] is None

    @pytest.mark.asyncio
    async def test_qualify_invalid_probe_returns_422(
        self, client: AsyncClient, db_session
    ):
        """An unsupported probe name surfaces as a 422 (from the qualifier's
        own validation), never a silent probe-less qualification."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id, phase="stabilize")

        resp = await client.post(
            f"/api/admin/projects/{project.id}/qualify",
            json={"probes": ["not_a_probe"]},
        )
        assert resp.status_code == 422
        assert "unsupported qualification probe" in resp.text


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
        body = resp.json()
        assert body["cleared"] == 0
        assert body["total_queued"] == 0
        assert body["truncated"] is False
        assert body["skipped_queued"] == 0

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

    @pytest.mark.asyncio
    async def test_truncates_queue_scan_at_cap(self, client: AsyncClient, db_session):
        """The stale-edge repair scan is capped and reports skipped queued rows."""
        project = await _make_project(db_session)
        for idx in range(503):
            db_session.add(
                Task(
                    project_id=project.id,
                    title=f"Queued task {idx}",
                    payloads=[{"type": "smoketest"}],
                    status=TaskStatus.QUEUED,
                )
            )
        await db_session.commit()

        resp = await client.post(f"/api/admin/projects/{project.id}/clear-wave-deps")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_queued"] == 503
        assert body["processed_queued"] == 500
        assert body["truncated"] is True
        assert body["skipped_queued"] == 3


class TestCancelStrategicRefresh:
    @pytest.mark.asyncio
    async def test_cancel_clears_armed_marker(self, client: AsyncClient, db_session):
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
            payloads=[
                {"type": "create", "prompt": "danger", "base_branch": "release/main"}
            ],
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
        _patch_explicit_merge_target(
            mocker,
            project.id,
            456,
            base_ref="release/candidate-1",
        )

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


class TestBulkCancel:
    """Bulk cancel must mirror the single-task cancel path: ADMIN_CANCEL
    provenance on the target and its cascaded child/dependent tasks, stranding
    no waiting dependent in BLOCKED/PARKED."""

    async def _make_task(self, session, project_id, status=TaskStatus.QUEUED):
        task = Task(
            project_id=project_id,
            title="Bulk cancel task",
            status=status,
            created_by="ORCHESTRATOR",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    @pytest.mark.asyncio
    async def test_bulk_cancel_persists_provenance_and_cascades_dependents(
        self, client: AsyncClient, db_session
    ):
        """Bulk cancellation writes ADMIN_CANCEL provenance on the target and
        cascades to both active children and waiting dependents."""
        from app.failure_classification import (
            ADMIN_CANCEL,
            classify_failure,
            is_non_death_cancellation,
        )

        project = await _make_project(db_session)
        task = await self._make_task(db_session, project.id, TaskStatus.IN_PROGRESS)
        child = Task(
            project_id=project.id,
            parent_task_id=task.id,
            title="Active child",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        dependent = Task(
            project_id=project.id,
            title="Waiting dependent",
            status=TaskStatus.QUEUED,
            depends_on=[str(task.id)],
            created_by="ORCHESTRATOR",
        )
        unrelated = Task(
            project_id=project.id,
            title="Unrelated queued task",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add_all([child, dependent, unrelated])
        await db_session.commit()

        resp = await client.post(
            "/api/admin/tasks/bulk-action",
            json={"action": "cancel", "task_ids": [str(task.id)]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"] == [{"task_id": str(task.id), "status": "ok"}]

        for row in (task, child, dependent, unrelated):
            await db_session.refresh(row)

        assert task.status == TaskStatus.CANCELLED
        assert task.context["failure_category"] == ADMIN_CANCEL
        assert task.context["failure_class"] == ADMIN_CANCEL
        assert task.context["cancelled_by"] == "USER"
        assert classify_failure(task) == ADMIN_CANCEL
        assert is_non_death_cancellation(task)

        for cascaded in (child, dependent):
            assert cascaded.status == TaskStatus.CANCELLED
            assert cascaded.context["failure_category"] == ADMIN_CANCEL
            assert cascaded.context["failure_class"] == ADMIN_CANCEL
            assert cascaded.context["cancelled_by"] == "USER"
            assert cascaded.context["cancelled_by_task_id"] == str(task.id)
            assert is_non_death_cancellation(cascaded)
        assert dependent.latest_status_message.startswith("Cancelled: dependency task")
        assert unrelated.status == TaskStatus.QUEUED


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
            payloads=[{"type": "create", "prompt": "x", "model": "claude-sonnet-4-6"}],
            status=TaskStatus.FAILED,
            vm_instance_id="agent-vm-old",
            attempt_count=3,
            retry_after_until=datetime(2099, 1, 1),
            llm_account_id="stale-account",
            context={
                "_retry_after_resets": 5,
                "llm_account_id": "stale-account",
                "llm_provider": "anthropic",
                "llm_model": "claude-sonnet-4-6",
                "runner_target_id": "dead-runner",
                "forced_runner_target_id": "operator-pin",
                "_requested_payload_model": "gpt-5.6-sol",
                "_resolved_payload_model": "claude-sonnet-4-6",
            },
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
        assert task.retry_after_until is None
        assert task.llm_account_id is None
        assert task.first_payload["model"] == "gpt-5.6-sol"
        assert "runner_target_id" not in (task.context or {})
        assert (task.context or {})["forced_runner_target_id"] == "operator-pin"
        assert (task.context or {}).get("_fresh_attempt_started_at")
        assert "_retry_after_resets" not in (task.context or {})


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
            retry_after_until=datetime(2099, 1, 1),
            context={"_retry_after_resets": 7},
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
        assert task.retry_after_until is None
        assert "_retry_after_resets" not in (task.context or {})
        # Clean attempt: counter reset like any leaf requeue.
        assert task.attempt_count == 0

    @pytest.mark.asyncio
    async def test_requeue_preserves_capability_execution_requirement(self, db_session):
        """Generic manual/bulk retry strips ordinary blocker metadata but must NOT
        strip the durable capability-execution requirement — otherwise a retry
        would bypass the execution-authority fence and let the task run without
        current platform proof. The requirement survives the fresh attempt so the
        fence re-blocks it at the next dispatch/claim."""
        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply
        from app.capability_execution_fence import (
            CAPABILITY_EXECUTION_REQUIREMENTS_KEY,
            block_task_for_capability_proof,
        )
        from app.task_blocked import BLOCKER_CONTEXT_KEY

        project = await _make_project(db_session)
        cap_need = {"kind": "capability", "ref": "forged-cap"}
        task = Task(
            project_id=project.id,
            title="Fence-blocked then requeued",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-old",
            context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [cap_need]},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # The fence parks it BLOCKED with the capability need.
        block_task_for_capability_proof(task, [cap_need], source="cold dispatch")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        assert task.status == TaskStatus.BLOCKED

        await modify_task_apply(
            task,
            {"status": TaskStatus.QUEUED.value},
            BackgroundTasks(),
            db_session,
        )
        await db_session.refresh(task)

        # Ordinary blocker metadata cleared, but the durable requirement survives.
        assert task.status == TaskStatus.QUEUED
        assert BLOCKER_CONTEXT_KEY not in (task.context or {})
        assert (task.context or {}).get(CAPABILITY_EXECUTION_REQUIREMENTS_KEY) == [
            cap_need
        ]


class TestManualRequeueGuidance:
    @pytest.mark.asyncio
    async def test_stale_fix_prompt_gets_agent_facing_operator_guidance(
        self, db_session, mocker
    ):
        """Manual guidance lands in payload.prompt_suffix, not internal context."""
        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply

        project = await _make_project(db_session)
        stale_prompt = "Renumber the migration to 0626."
        task = Task(
            project_id=project.id,
            title="Fix stale migration advice",
            payloads=[
                {
                    "pr_number": 15420,
                    "branch": "fix/stale-migration",
                    "type": "fix",
                    "prompt": stale_prompt,
                    "prompt_suffix": "Preserve the original review constraints.",
                }
            ],
            status=TaskStatus.FAILED,
            vm_instance_id="agent-vm-old",
            context={
                "_original_prompt_suffix": "This internal snapshot is not guidance."
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
            "app.github.get_pull_request",
            new_callable=AsyncMock,
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-15420"},
                "head": {"sha": "head-15420"},
            },
        )

        async def _expire_task_during_vm_cleanup(*_args, **_kwargs):
            # Production teardown can write forensics through another session.
            # Model the resulting stale identity-map state deterministically and
            # keep this guidance test from attempting a real GCP deletion.
            db_session.expire(task)
            return True

        mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted",
            new=AsyncMock(side_effect=_expire_task_during_vm_cleanup),
        )

        await modify_task_apply(
            task,
            {
                "status": TaskStatus.QUEUED.value,
                "requeue_prompt_suffix": (
                    "Do not create 0626. Remove the duplicate migration instead."
                ),
            },
            BackgroundTasks(),
            db_session,
        )

        await db_session.refresh(task)
        assert task.first_payload["prompt"] == stale_prompt
        suffix = task.first_payload["prompt_suffix"]
        assert suffix.startswith("Preserve the original review constraints.")
        assert suffix.endswith(
            "Do not create 0626. Remove the duplicate migration instead."
        )
        assert "## Operator retry guidance" in suffix
        assert (
            task.first_payload["operator_retry_guidance"]
            == "Do not create 0626. Remove the duplicate migration instead."
        )

        # A later operator correction replaces the earlier block instead of
        # stacking contradictory retry instructions.
        await modify_task_apply(
            task,
            {
                "status": TaskStatus.QUEUED.value,
                "requeue_prompt_suffix": "Delete only the semantic duplicate.",
            },
            BackgroundTasks(),
            db_session,
        )
        await db_session.refresh(task)
        suffix = task.first_payload["prompt_suffix"]
        assert "Do not create 0626" not in suffix
        assert suffix.count("## Operator retry guidance") == 1
        assert suffix.endswith("Delete only the semantic duplicate.")

    @pytest.mark.asyncio
    async def test_internal_suffix_context_write_is_rejected_on_requeue(
        self, db_session
    ):
        """The old silent no-op operator pattern fails with an actionable 400."""
        from fastapi import BackgroundTasks, HTTPException

        from app.admin_task_mutations import modify_task_apply

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Reject internal suffix mutation",
            payloads=[{"type": "fix", "prompt": "stale"}],
            status=TaskStatus.FAILED,
        )
        db_session.add(task)
        await db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await modify_task_apply(
                task,
                {
                    "status": TaskStatus.QUEUED.value,
                    "context": {"_original_prompt_suffix": "operator advice"},
                },
                BackgroundTasks(),
                db_session,
            )
        assert exc_info.value.status_code == 400
        assert "use requeue_prompt_suffix instead" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_guidance_without_queued_status_is_rejected(self, db_session):
        """Guidance-only PATCHes cannot succeed while delivering nothing."""
        from fastapi import BackgroundTasks, HTTPException

        from app.admin_task_mutations import modify_task_apply

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Reject detached guidance",
            payloads=[
                {
                    "type": "fix",
                    "prompt": "stale",
                    "pr_number": 15420,
                    "branch": "fix/stale",
                }
            ],
            status=TaskStatus.FAILED,
        )
        db_session.add(task)
        await db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await modify_task_apply(
                task,
                {"requeue_prompt_suffix": "new guidance"},
                BackgroundTasks(),
                db_session,
            )
        assert exc_info.value.status_code == 400


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
            payloads=[{"type": "create", "prompt": "x", "model": "claude-sonnet-4-6"}],
            status=TaskStatus.FAILED,
            vm_instance_id="agent-vm-old",
            attempt_count=3,
            llm_account_id="stale-account",
            context={
                "failure_class": "CODE_ERROR",
                "failure_category": "claude_code_failed",
                "_api_failure_retries": 2,
                "llm_account_id": "stale-account",
                "llm_provider": "anthropic",
                "llm_model": "claude-sonnet-4-6",
                "runner_target_id": "dead-runner",
                "forced_runner_target_id": "operator-pin",
                "_requested_payload_model": "gpt-5.6-sol",
                "_resolved_payload_model": "claude-sonnet-4-6",
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
        assert task.llm_account_id is None
        assert task.first_payload["model"] == "gpt-5.6-sol"
        assert "runner_target_id" not in ctx
        assert ctx["forced_runner_target_id"] == "operator-pin"
        assert ctx.get("_fresh_attempt_started_at")
        assert task.attempt_count == 0


class TestRequeueStuckTask:
    @pytest.mark.asyncio
    async def test_requeue_stuck_task_terminates_vm_and_resets_to_queued(
        self, db_session, mocker
    ):
        """Re-queuing a stuck IN_PROGRESS task must delete the old VM and reset
        the task to QUEUED with a fresh vm_instance_id and cleared heartbeat."""
        from unittest.mock import AsyncMock

        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply

        mock_delete_vm = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock
        )

        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Stuck task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="stuck-vm-abc123",
            last_heartbeat=datetime(2020, 1, 1),
            attempt_count=1,
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
        assert task.vm_instance_id != "stuck-vm-abc123"
        assert task.last_heartbeat is None
        assert task.attempt_count == 0
        mock_delete_vm.assert_awaited_once_with("stuck-vm-abc123", reason="admin_requeue")


class TestTriggerForcePromote:
    """Router-level coverage for the PLAIN force-promote lever
    (``POST /projects/{id}/promote/force``): the operator's ``reason`` +
    ``candidate_sha`` must be threaded through to
    ``force_promote_current_version`` (which persists ``forced_promote_reason``
    on the audit trail), and no-op statuses must map to the right HTTP codes.
    """

    @pytest.mark.asyncio
    async def test_reason_threaded_to_force_promote(self, db_session, mocker):
        """The operator justification + explicit candidate_sha are plumbed into
        force_promote_current_version alongside the acting user as ``actor``."""
        from app.routers.admin_operator_triggers import (
            trigger_force_promote,
            _ForcePromoteRequest,
        )

        project = await _make_project(db_session)

        force = mocker.patch(
            "app.release_qualifier.force_promote_current_version",
            new=AsyncMock(return_value={"status": "promoted"}),
        )

        body = _ForcePromoteRequest(
            candidate_sha="cafef00d" * 5,
            reason="prod gate false-block — machinery-only fix",
        )
        result = await trigger_force_promote(
            project.id,
            body=body,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        assert result == {"status": "promoted"}
        force.assert_awaited_once()
        kwargs = force.await_args.kwargs
        assert kwargs["candidate_sha"] == "cafef00d" * 5
        assert kwargs["reason"] == "prod gate false-block — machinery-only fix"
        # The audit trail must attribute the force-promote to the acting operator.
        assert kwargs["actor"] == TEST_USER_ID

    @pytest.mark.asyncio
    async def test_missing_body_threads_none_reason(self, db_session, mocker):
        """With no request body the lever still fires, threading reason=None and
        candidate_sha=None (default: current integration HEAD) — the reason key
        is always present on the trail, never silently dropped."""
        from app.routers.admin_operator_triggers import trigger_force_promote

        project = await _make_project(db_session)

        force = mocker.patch(
            "app.release_qualifier.force_promote_current_version",
            new=AsyncMock(return_value={"status": "promoted"}),
        )

        result = await trigger_force_promote(
            project.id,
            body=None,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        assert result == {"status": "promoted"}
        force.assert_awaited_once()
        kwargs = force.await_args.kwargs
        assert kwargs["reason"] is None
        assert kwargs["candidate_sha"] is None
        assert kwargs["actor"] == TEST_USER_ID

    @pytest.mark.asyncio
    async def test_no_open_version_maps_to_404(self, db_session, mocker):
        """A ``no_open_version`` result surfaces as HTTP 404, not a silent pass."""
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import (
            trigger_force_promote,
            _ForcePromoteRequest,
        )

        project = await _make_project(db_session)
        mocker.patch(
            "app.release_qualifier.force_promote_current_version",
            new=AsyncMock(return_value={"status": "no_open_version"}),
        )

        with pytest.raises(HTTPException) as exc_info:
            await trigger_force_promote(
                project.id,
                body=_ForcePromoteRequest(reason="x"),
                session=db_session,
                current_user={"user_id": TEST_USER_ID},
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_promotion_check_wait_maps_to_409(self, db_session, mocker):
        """A ``promotion_check_wait`` guard result surfaces as HTTP 409 with the
        blocking PR number embedded in the detail."""
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import (
            trigger_force_promote,
            _ForcePromoteRequest,
        )

        project = await _make_project(db_session)
        mocker.patch(
            "app.release_qualifier.force_promote_current_version",
            new=AsyncMock(
                return_value={
                    "status": "promotion_check_wait",
                    "promotion_pr_number": 4242,
                }
            ),
        )

        with pytest.raises(HTTPException) as exc_info:
            await trigger_force_promote(
                project.id,
                body=_ForcePromoteRequest(reason="x"),
                session=db_session,
                current_user={"user_id": TEST_USER_ID},
            )
        assert exc_info.value.status_code == 409
        assert "4242" in exc_info.value.detail


class TestHotfixScopePreview:
    """Router-level coverage for the read-only hotfix-scope preview
    (``GET /projects/{id}/promote/hotfix/preview``).

    The endpoint must report eligibility without triggering any promotion,
    thread through the optional ``candidate_sha`` query parameter, enforce
    owner-only access, and map internal statuses to the right HTTP codes.
    """

    @pytest.mark.asyncio
    async def test_hotfix_scope_preview_reports_eligibility_without_promoting(
        self, db_session, mocker
    ):
        """Happy path: machinery-only diff → eligible=True, no promotion occurs.

        This is the named acceptance-evidence test for the task.  The endpoint
        delegates to ``check_hotfix_scope_preview`` (mocked) and returns its
        result verbatim.  A real ``promote`` call would also need
        ``release_qualifier.hotfix_promote_current_version`` — we verify that
        function is NEVER called, proving the preview is side-effect-free.
        """
        from app.routers.admin_operator_triggers import get_hotfix_scope_preview

        project = await _make_project(db_session)

        preview_mock = mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(
                return_value={
                    "status": "eligible",
                    "eligible": True,
                    "changed_files": ["backend/app/release_qualifier.py"],
                    "offending_files": [],
                    "reason": "all changed files are allowlisted release machinery",
                    "candidate_sha": "deadbeef" * 5,
                    "prior_deployed_sha": "cafecafe" * 5,
                }
            ),
        )
        promote_mock = mocker.patch(
            "app.release_qualifier.hotfix_promote_current_version",
            new=AsyncMock(),
        )

        result = await get_hotfix_scope_preview(
            project.id,
            candidate_sha=None,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        assert result["eligible"] is True
        assert result["status"] == "eligible"
        assert result["offending_files"] == []
        assert "backend/app/release_qualifier.py" in result["changed_files"]
        # The preview must NOT trigger an actual promotion.
        promote_mock.assert_not_called()
        preview_mock.assert_awaited_once_with(
            db_session,
            project.id,
            candidate_sha=None,
        )

    @pytest.mark.asyncio
    async def test_hotfix_scope_preview_reports_ineligible_with_offending_files(
        self, db_session, mocker
    ):
        """A diff touching a non-allowlisted file → eligible=False with offending_files."""
        from app.routers.admin_operator_triggers import get_hotfix_scope_preview

        project = await _make_project(db_session)

        mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(
                return_value={
                    "status": "ineligible",
                    "eligible": False,
                    "changed_files": [
                        "backend/app/release_qualifier.py",
                        "backend/app/compute_jobs.py",
                    ],
                    "offending_files": ["backend/app/compute_jobs.py"],
                    "reason": "backend/app/compute_jobs.py is outside the release-machinery allowlist",
                    "candidate_sha": "deadbeef" * 5,
                    "prior_deployed_sha": "cafecafe" * 5,
                }
            ),
        )

        result = await get_hotfix_scope_preview(
            project.id,
            candidate_sha=None,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        assert result["eligible"] is False
        assert result["status"] == "ineligible"
        assert "backend/app/compute_jobs.py" in result["offending_files"]

    @pytest.mark.asyncio
    async def test_hotfix_scope_preview_respects_candidate_sha_param(
        self, db_session, mocker
    ):
        """An explicit ``candidate_sha`` is threaded through to
        ``check_hotfix_scope_preview``."""
        from app.routers.admin_operator_triggers import get_hotfix_scope_preview

        project = await _make_project(db_session)

        preview_mock = mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(
                return_value={
                    "status": "eligible",
                    "eligible": True,
                    "changed_files": [],
                    "offending_files": [],
                    "reason": "all changed files are allowlisted release machinery",
                    "candidate_sha": "abc1234" * 5 + "abc1234",
                    "prior_deployed_sha": "main000" * 5 + "main000",
                }
            ),
        )

        explicit_sha = "abc1234" * 5 + "abc1234"
        await get_hotfix_scope_preview(
            project.id,
            candidate_sha=explicit_sha,
            session=db_session,
            current_user={"user_id": TEST_USER_ID},
        )

        preview_mock.assert_awaited_once_with(
            db_session,
            project.id,
            candidate_sha=explicit_sha,
        )

    @pytest.mark.asyncio
    async def test_hotfix_scope_preview_returns_404_when_no_open_version(
        self, db_session, mocker
    ):
        """``no_open_version`` from the preview helper maps to HTTP 404."""
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import get_hotfix_scope_preview

        project = await _make_project(db_session)

        mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(return_value={"status": "no_open_version"}),
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_hotfix_scope_preview(
                project.id,
                candidate_sha=None,
                session=db_session,
                current_user={"user_id": TEST_USER_ID},
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_hotfix_scope_preview_requires_owner_role(self, db_session, mocker):
        """A member (non-owner) cannot call the preview endpoint — 404 from role check."""
        from fastapi import HTTPException
        from app.routers.admin_operator_triggers import get_hotfix_scope_preview
        import uuid as _uuid

        # Create a member user with a fresh user ID (not the owner).
        project = await _make_project(db_session)
        member_id = str(_uuid.uuid4())

        mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(return_value={"status": "eligible", "eligible": True}),
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_hotfix_scope_preview(
                project.id,
                candidate_sha=None,
                session=db_session,
                current_user={"user_id": member_id},
            )
        # _get_user_project_with_role raises 404 for users without the required role.
        assert exc_info.value.status_code == 404


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
                "head": {"ref": "research/abc12345", "sha": "head654"},
                "base": {"ref": "release/candidate-1", "sha": "base654"},
            },
        )
        mocker.patch(
            "app.github.get_pull_request",
            new_callable=AsyncMock,
            return_value={
                "state": "open",
                "head": {"ref": "research/abc12345", "sha": "head654"},
                "base": {"ref": "release/candidate-1", "sha": "base654"},
            },
        )
        mocker.patch(
            "app.review_merge_authority._fetch_live_review_target",
            new_callable=AsyncMock,
            return_value={
                "project_id": str(project.id),
                "pr_number": 654,
                "base_sha": "base654",
                "head_sha": "head654",
                "base_ref": "release/candidate-1",
                "state": "open",
                "merged": False,
                "draft": False,
                "mergeable": True,
                "mergeable_state": "clean",
            },
        )
        merge_mock = mocker.patch(
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
        assert merge_mock.call_args.kwargs["expected_head_sha"] == "head654"
        delete_branch_mock.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("changed_side", ["head", "base"])
    async def test_intermediate_rollup_holds_when_target_changes_after_guard(
        self, db_session, mocker, changed_side
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
            title="Manual rollup target race",
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
                "html_url": "https://github.com/owner/repo/pull/655",
                "number": 655,
                "head": {"ref": "research/abc12345", "sha": "head-old"},
                "base": {"ref": "release/candidate-1", "sha": "base-old"},
            },
        )
        mocker.patch(
            "app.github.get_pull_request",
            new_callable=AsyncMock,
            return_value={
                "state": "open",
                "head": {"ref": "research/abc12345", "sha": "head-old"},
                "base": {"ref": "release/candidate-1", "sha": "base-old"},
            },
        )
        mocker.patch(
            "app.review_merge_authority._fetch_live_review_target",
            new_callable=AsyncMock,
            return_value={
                "project_id": str(project.id),
                "pr_number": 655,
                "base_sha": "base-new" if changed_side == "base" else "base-old",
                "head_sha": "head-new" if changed_side == "head" else "head-old",
                "base_ref": "release/candidate-1",
                "state": "open",
                "merged": False,
                "draft": False,
                "mergeable": True,
                "mergeable_state": "clean",
            },
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

        merge_mock.assert_not_awaited()
        await db_session.refresh(task)
        assert task.status == TaskStatus.EVALUATING
        assert (
            result["merge_blocked_reason"]
            == "authoritative review gate held merge"
        )
        assert "live base/head" in (task.latest_status_message or "").lower()

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
            return_value={
                "html_url": "https://github.com/owner/repo/pull/456",
                "number": 456,
            },
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
            return_value=[
                {
                    "filename": "backend/alembic/versions/0002_dup.py",
                    "status": "added",
                    "patch": '+revision = "0002"\n+down_revision = "0001"',
                }
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
    async def test_operator_trigger_blocks_cross_branch_deployed_id_reuse(
        self, db_session, mocker
    ):
        """Regression: operator-triggered intermediate auto-merge is blocked when
        the PR's new migration revision id is already deployed on the
        promotion_target under a DIFFERENT filename (the 0309-class collision) —
        even though the integration branch does not yet have that revision id.
        """
        from app.routers.admin_operator_triggers import trigger_rollup

        project = await _make_project(db_session)
        project.integration_branch = "automain"
        project.promotion_target = "main"
        project.build_config = {"staging_branch_prefixes": ["research/"]}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="Operator trigger cross-branch id reuse",
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
                "html_url": "https://github.com/owner/repo/pull/999",
                "number": 999,
            },
        )
        # PR adds 0309_performance_indexes.py (revision=0309, different body from
        # the deployed 0309_computejob_heartbeat.py on the promotion target).
        mocker.patch(
            "app.github.get_pr_files",
            new_callable=AsyncMock,
            return_value=[
                {
                    "filename": "backend/alembic/versions/0309_performance_indexes.py",
                    "status": "added",
                    "patch": '+revision = "0309"\n+down_revision = "0308"',
                }
            ],
        )
        mocker.patch(
            "app.github.get_pull_request_diff",
            new_callable=AsyncMock,
            return_value=(
                "diff --git a/backend/alembic/versions/0309_performance_indexes.py "
                "b/backend/alembic/versions/0309_performance_indexes.py\n"
                "+++ b/backend/alembic/versions/0309_performance_indexes.py\n"
                '+revision = "0309"\n'
                '+down_revision = "0308"\n'
            ),
        )

        def _list_versions_side_effect(token, owner, repo, ref):
            if ref == "main":
                # Deployed promotion target has 0309=heartbeat — different body.
                return ["0308_prev.py", "0309_computejob_heartbeat.py"]
            # Integration branch (automain) does not yet have 0309.
            return ["0308_prev.py"]

        mocker.patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
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
        assert result["rollup_pr_number"] == 999
        assert result.get("merge_blocked_reason")
        assert "0309" in result.get("merge_blocked_reason", "")
        assert task.status == TaskStatus.EVALUATING

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
    async def test_manual_rollup_stakes_claim_before_create(self, db_session, mocker):
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
                (await db_session.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
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
        assert result["rollup_pr_url"] == ("https://github.com/owner/repo/pull/7373")


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


class TestRecordFindingResolution:
    """POST /api/admin/projects/{id}/finding-resolution — operator ledger write."""

    @pytest.mark.asyncio
    async def test_success_resolved_state(self, client: AsyncClient, db_session):
        """Member can write a 'resolved' entry; DB row is created."""
        from app.models import AuditFindingResolution

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/finding-resolution",
            json={
                "finding_key": "adb632b709685086",
                "release_version_id": str(version.id),
                "state": "resolved",
                "severity": "medium",
                "note": "Fixed in PR #42",
                "resolving_ref": "abc123",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["finding_key"] == "adb632b709685086"
        assert data["state"] == "resolved"
        assert data["severity"] == "medium"
        assert data["note"] == "Fixed in PR #42"
        assert data["resolving_ref"] == "abc123"
        assert data["project_id"] == str(project.id)
        assert data["release_version_id"] == str(version.id)

        # Verify the row exists in the DB.
        row = (
            (
                await db_session.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == version.id,
                        AuditFindingResolution.finding_key == "adb632b709685086",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.state == "resolved"

    @pytest.mark.asyncio
    async def test_success_stale_state(self, client: AsyncClient, db_session):
        """Member can write a 'stale' entry; row state is persisted correctly."""
        from app.models import AuditFindingResolution

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/finding-resolution",
            json={
                "finding_key": "deadbeef12345678",
                "release_version_id": str(version.id),
                "state": "stale",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "stale"

        row = (
            (
                await db_session.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == version.id,
                        AuditFindingResolution.finding_key == "deadbeef12345678",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.state == "stale"

    @pytest.mark.asyncio
    async def test_bad_state_returns_422(self, client: AsyncClient, db_session):
        """An invalid state value is rejected by Pydantic with 422."""
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/finding-resolution",
            json={
                "finding_key": "adb632b709685086",
                "release_version_id": str(version.id),
                "state": "garbage",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_wrong_project_returns_404(self, client: AsyncClient, db_session):
        """A release_version_id belonging to a different project returns 404."""
        project1 = await _make_project(db_session, name="Project 1")
        project2 = await _make_project(db_session, name="Project 2")
        version_p2 = await _make_open_version(db_session, project2.id)

        resp = await client.post(
            f"/api/admin/projects/{project1.id}/finding-resolution",
            json={
                "finding_key": "adb632b709685086",
                "release_version_id": str(version_p2.id),
                "state": "resolved",
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_viewer_role_returns_403(self, override_get_session, db_session):
        """Viewer role is rejected with 403 (member role required)."""
        from app.auth import get_current_user

        viewer_id = "viewer-user-99"
        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        member = ProjectMember(project_id=project.id, user_id=viewer_id, role="viewer")
        db_session.add(member)
        await db_session.commit()

        async def override_viewer():
            return {"user_id": viewer_id, "email": "viewer@example.com", "role": "user"}

        from app.main import app as _app

        _app.dependency_overrides[get_current_user] = override_viewer
        _app.dependency_overrides[override_get_session] = lambda: db_session
        try:
            async with AsyncClient(
                transport=ASGITransport(app=_app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as viewer_client:
                resp = await viewer_client.post(
                    f"/api/admin/projects/{project.id}/finding-resolution",
                    json={
                        "finding_key": "adb632b709685086",
                        "release_version_id": str(version.id),
                        "state": "resolved",
                    },
                )
            assert resp.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_current_user, None)
            _app.dependency_overrides.pop(override_get_session, None)

    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing_row(
        self, client: AsyncClient, db_session
    ):
        """A second write for the same (finding_key, release_version_id) updates
        the existing row rather than inserting a duplicate."""
        from app.models import AuditFindingResolution

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)

        # First write: resolved.
        resp1 = await client.post(
            f"/api/admin/projects/{project.id}/finding-resolution",
            json={
                "finding_key": "adb632b709685086",
                "release_version_id": str(version.id),
                "state": "resolved",
                "note": "first write",
            },
        )
        assert resp1.status_code == 200

        # Second write: stale with different note.
        resp2 = await client.post(
            f"/api/admin/projects/{project.id}/finding-resolution",
            json={
                "finding_key": "adb632b709685086",
                "release_version_id": str(version.id),
                "state": "stale",
                "note": "second write",
            },
        )
        assert resp2.status_code == 200
        assert resp2.json()["state"] == "stale"

        # Only one row in the DB.
        rows = (
            (
                await db_session.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == version.id,
                        AuditFindingResolution.finding_key == "adb632b709685086",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].state == "stale"
        assert rows[0].note == "second write"


class TestComputeExperimentTaskLaunch:
    async def _project_with_repo(self, session, repo="compute"):
        project = Project(
            name=f"acme/{repo}",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo=repo,
            integration_branch="automain",
            promotion_target="automain",
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        session.add(
            ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
        )
        await session.commit()
        return project

    def _valid_payload(self, repo="compute", **overrides):
        payload = {
            "github_repo": f"acme/{repo}",
            "title": "Measure GPU throughput",
            "prompt": "Run the throughput benchmark and persist the result.",
            "base_branch": "automain",
            "command": "python bench.py --json result.json",
            "spend_cap_usd": 5.0,
            "deadline_seconds": 900,
            "accelerator": "L4",
            "machine_type": "g2-standard-4",
            "cwd": "/workspace",
            "env": {"BENCHMARK_MODE": "smoke"},
            "experiment": "gpu-throughput",
            "benchmark": "tokens-per-second",
            "hypothesis": "L4 throughput exceeds the acceptance floor.",
            "metric": "tokens_per_second",
            "success_criterion": {
                "metric": "tokens_per_second",
                "operator": ">=",
                "threshold": 100.0,
            },
        }
        payload.update(overrides)
        return payload

    @pytest.mark.asyncio
    async def test_valid_launch_persists_compute_experiment_contract(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(
                    idempotency_key="gpu-smoke",
                    idea="Tune GPU throughput from the original operator prompt",
                    draft_notes="Drafting chose a benchmark-led contract.",
                    drafted_from_refinement=True,
                ),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["requires_approval"] is False
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        assert task.status == TaskStatus.QUEUED
        assert task.requires_approval is False
        payload = task.payloads[0]
        assert payload["type"] == "compute_experiment"
        assert payload["command"] == "python bench.py --json result.json"
        assert payload["spend_cap_usd"] == 5.0
        assert payload["deadline_seconds"] == 900
        assert payload["accelerator"] == "L4"
        assert payload["machine_type"] == "g2-standard-4"
        assert payload["cwd"] == "/workspace"
        assert payload["env"] == {"BENCHMARK_MODE": "smoke"}
        assert payload["experiment"] == "gpu-throughput"
        assert payload["benchmark"] == "tokens-per-second"
        assert payload["hypothesis"] == "L4 throughput exceeds the acceptance floor."
        assert payload["metric"] == "tokens_per_second"
        assert payload["provenance"] == {
            "idea": "Tune GPU throughput from the original operator prompt",
            "draft_notes": "Drafting chose a benchmark-led contract.",
            "drafted_from_refinement": True,
        }
        # The criterion now carries an explicit kind discriminator (defaulting to
        # "metric" for backward compatibility) plus the min_file_count field used
        # by the artifact_produced kind; the metric/operator/threshold trio is
        # unchanged for a metric criterion.
        assert payload["success_criterion"] == {
            "kind": "metric",
            "metric": "tokens_per_second",
            "operator": ">=",
            "threshold": 100.0,
            "min_file_count": 1,
        }

    @pytest.mark.asyncio
    async def test_container_fields_persist_for_runner_experiments(
        self, client: AsyncClient, db_session
    ):
        # Container-aware fields (image + registry auth + host setup_step) must
        # survive request validation -> launch_contract -> stored task payload,
        # or a CUDA/NGC experiment would silently run bare on the runner.
        await self._project_with_repo(db_session)
        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(
                container_image="nvcr.io/nvidia/tensorflow:25.01",
                container_registry_auth={
                    "server": "nvcr.io",
                    "username": "$oauthtoken",
                    "password": "nvapi-SECRET",
                },
                setup_step="echo host-prep",
            ),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert payload["container_image"] == "nvcr.io/nvidia/tensorflow:25.01"
        assert payload["container_registry_auth"] == {
            "server": "nvcr.io",
            "username": "$oauthtoken",
            "password": "nvapi-SECRET",
        }
        assert payload["setup_step"] == "echo host-prep"

    @pytest.mark.parametrize(
        ("override", "expected_detail"),
        [
            ({"command": None}, "command"),
            ({"spend_cap_usd": 0}, "spend_cap_usd must be positive"),
            ({"deadline_seconds": 0}, "deadline_seconds must be positive"),
        ],
    )
    async def test_malformed_contract_is_rejected_without_task(
        self, client: AsyncClient, db_session, override, expected_detail
    ):
        await self._project_with_repo(db_session)
        payload = self._valid_payload()
        for key, value in override.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 422, resp.text
        assert expected_detail in resp.text
        tasks = (await db_session.execute(select(Task))).scalars().all()
        assert tasks == []

    async def _enrolled_runner(self, session, project_id, hostname="dgx-1"):
        runner = LocalRunner(
            project_id=project_id,
            token_hash="r" * 64,
            hostname=hostname,
            status="active",
            last_seen_at=_utcnow(),
        )
        session.add(runner)
        await session.commit()
        await session.refresh(runner)
        return runner

    @pytest.mark.asyncio
    async def test_runner_target_persists_and_stamps_preferred_runner(
        self, client: AsyncClient, db_session
    ):
        """runner_target survives request → launch_contract → stored payload and
        stamps the existing preferred_runner_target_id dispatch seam."""
        project = await self._project_with_repo(db_session)
        runner = await self._enrolled_runner(db_session, project.id)

        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(
                runner_target={"runner_id": str(runner.id), "strict": True}
            ),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert payload["runner_target"] == {
            "runner_id": str(runner.id),
            "strict": True,
        }
        # The pin rides the existing soft-preference seam; strictness is read
        # from the payload at dispatch time.
        assert (task.context or {}).get("preferred_runner_target_id") == str(
            runner.id
        )

    @pytest.mark.asyncio
    async def test_runner_target_defaults_to_soft_pin(
        self, client: AsyncClient, db_session
    ):
        project = await self._project_with_repo(db_session)
        runner = await self._enrolled_runner(db_session, project.id)

        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(runner_target={"runner_id": str(runner.id)}),
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        assert task.payloads[0]["runner_target"] == {
            "runner_id": str(runner.id),
            "strict": False,
        }

    @pytest.mark.asyncio
    async def test_runner_target_unknown_runner_rejected_422(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)

        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(
                runner_target={"runner_id": str(uuid.uuid4()), "strict": True}
            ),
        )

        assert resp.status_code == 422, resp.text
        # The app-level HTTPException handler wraps the dict detail as
        # {"error": {"code": 422, "message": <detail dict>}}.
        detail = resp.json()["error"]["message"]
        assert detail["error"] == "runner_target_invalid"
        assert detail["reason_code"] == "not_found"
        tasks = (await db_session.execute(select(Task))).scalars().all()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_runner_target_cross_project_runner_rejected_422(
        self, client: AsyncClient, db_session
    ):
        """A runner enrolled in a DIFFERENT project is rejected at authoring time
        (review-verdict requirement: validate against the task's project's
        enrolled runners)."""
        await self._project_with_repo(db_session)
        other = Project(name="other/project", user_id=TEST_USER_ID)
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        foreign_runner = await self._enrolled_runner(db_session, other.id)

        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(
                runner_target={"runner_id": str(foreign_runner.id), "strict": False}
            ),
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["error"]["message"]
        assert detail["error"] == "runner_target_invalid"
        assert detail["reason_code"] == "not_enrolled"
        assert detail["runner_id"] == str(foreign_runner.id)
        tasks = (await db_session.execute(select(Task))).scalars().all()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_runner_target_invalid_uuid_rejected_422(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)

        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json=self._valid_payload(
                runner_target={"runner_id": "not-a-uuid", "strict": True}
            ),
        )

        assert resp.status_code == 422, resp.text
        assert "not a valid UUID" in resp.text
        tasks = (await db_session.execute(select(Task))).scalars().all()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_spend_above_threshold_requires_approval(
        self, client: AsyncClient, db_session
    ):
        # Operator-launched compute_experiment tasks above the spend threshold
        # must be held for approval (requires_approval=True).
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(spend_cap_usd=5.01, deadline_seconds=1200),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["requires_approval"] is True
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        assert task.requires_approval is True

    @pytest.mark.asyncio
    async def test_spend_below_threshold_runs_without_approval(
        self, client: AsyncClient, db_session
    ):
        # Operator-launched compute_experiment tasks at or below the spend
        # threshold run immediately without approval.
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 10.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(spend_cap_usd=5.0, deadline_seconds=900),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["requires_approval"] is False
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        assert task.requires_approval is False

    @pytest.mark.asyncio
    async def test_spend_at_threshold_is_runnable(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(spend_cap_usd=5.0),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        assert task.requires_approval is False
        assert task.latest_status_message is None

    @pytest.mark.asyncio
    async def test_require_live_release_persists_live_proof_in_payload(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(require_live_release=True),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert payload["live_proof"] is True

    @pytest.mark.asyncio
    async def test_require_live_release_default_omits_live_proof(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert "live_proof" not in payload

    @pytest.mark.asyncio
    async def test_accelerator_only_supported_request_persists(
        self, client: AsyncClient, db_session
    ):
        """An accelerator-only request for a supported accelerator stays valid.

        The canonical single-GPU machine type is resolved downstream when the
        agent launches the actual compute job, so omitting machine_type here is
        not a rejection.
        """
        await self._project_with_repo(db_session)
        payload = self._valid_payload()
        payload.pop("machine_type", None)
        payload["accelerator"] = "L4"

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        assert task.payloads[0]["accelerator"] == "L4"

    @pytest.mark.asyncio
    async def test_supported_explicit_t4_pairing_persists(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        payload = self._valid_payload(accelerator="T4", machine_type="n1-standard-1")

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        assert task.payloads[0]["accelerator"] == "T4"
        assert task.payloads[0]["machine_type"] == "n1-standard-1"

    @pytest.mark.parametrize(
        ("override", "expected_detail"),
        [
            # Unsupported / over-quota accelerator.
            ({"accelerator": "A100", "machine_type": None}, "unsupported accelerator"),
            # GPU accelerator paired with an unpriced GPU machine type.
            (
                {"accelerator": "T4", "machine_type": "n1-standard-99"},
                "not a valid single-GPU shape",
            ),
            # CPU-only request with a machine type that is not explicitly priced.
            (
                {"accelerator": None, "machine_type": "bogus-machine-type"},
                "unsupported machine_type",
            ),
            # GPU / machine-family mismatch: T4 (n1) paired with a G2 shape.
            (
                {"accelerator": "T4", "machine_type": "g2-standard-4"},
                "not a valid single-GPU shape",
            ),
        ],
    )
    async def test_gpu_shape_validation_rejects_without_creating_task(
        self, client: AsyncClient, db_session, override, expected_detail
    ):
        """Invalid accelerator/machine-shape combinations fail fast with 422.

        The launch endpoint applies the same accelerator/machine-shape truth as
        compute-job creation (``app.vm_cost_rates.validate_compute_request``)
        before persisting a Task, so no Task row is created for a rejected
        contract.
        """
        await self._project_with_repo(db_session)
        payload = self._valid_payload()
        for key, value in override.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 422, resp.text
        assert expected_detail in resp.text
        from sqlalchemy import func as _func

        count = (await db_session.execute(select(_func.count(Task.id)))).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_runner_pinned_contract_accepts_noncatalog_accelerator(
        self, client: AsyncClient, db_session
    ):
        """A runner-pinned contract may name an operator-hardware GPU that is
        not in the GCP catalog (e.g. RTX 5080) — the runner dispatch gate checks
        verified capability, not the name."""
        project = await self._project_with_repo(db_session)
        runner = await self._enrolled_runner(db_session, project.id)
        payload = self._valid_payload(
            runner_target={"runner_id": str(runner.id), "strict": True},
            accelerator="RTX 5080",
        )
        payload.pop("machine_type", None)

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        assert task.payloads[0]["accelerator"] == "RTX 5080"
        assert task.payloads[0]["runner_target"] == {
            "runner_id": str(runner.id),
            "strict": True,
        }

    @pytest.mark.asyncio
    async def test_unpinned_contract_still_rejects_noncatalog_accelerator(
        self, client: AsyncClient, db_session
    ):
        """Without a runner pin the contract would provision on GCP, so a
        non-catalog accelerator name keeps the strict 422 rejection."""
        await self._project_with_repo(db_session)
        payload = self._valid_payload(accelerator="RTX 5080")
        payload.pop("machine_type", None)

        resp = await client.post("/api/admin/tasks/compute-experiment", json=payload)

        assert resp.status_code == 422, resp.text
        assert "unsupported accelerator" in resp.text
        tasks = (await db_session.execute(select(Task))).scalars().all()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_require_durable_artifact_persists_in_payload(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(require_durable_artifact=True),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert payload["require_durable_artifact"] is True

    @pytest.mark.asyncio
    async def test_require_durable_artifact_default_omits_payload_flag(
        self, client: AsyncClient, db_session
    ):
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json=self._valid_payload(),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert "require_durable_artifact" not in payload

    @pytest.mark.asyncio
    async def test_auto_created_project_is_dispatchable(
        self, client: AsyncClient, db_session
    ):
        """A compute_experiment on a fresh repo auto-creates a project that is
        immediately dispatchable: autonomous_enabled=False (no autonomous
        research cycles) and bootstrap_completed_at set (bootstrap-recovery
        sweep will not spawn a competing repo_bootstrap task).
        """
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                # Use a repo that has no pre-existing project so the endpoint
                # auto-creates one.
                json=self._valid_payload(repo="fresh-repo-no-project"),
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Retrieve the auto-created project.
        project = (
            await db_session.execute(
                select(Project).where(
                    Project.github_owner == "acme",
                    Project.github_repo == "fresh-repo-no-project",
                )
            )
        ).scalar_one()

        # The task must be QUEUED and not held for approval.
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        assert task.status == TaskStatus.QUEUED
        assert task.requires_approval is False

        # The auto-created project must be marked non-autonomous so the
        # orchestrator does not try to run research cycles on it.
        assert project.autonomous_enabled is False, (
            "auto-created compute-experiment project must have autonomous_enabled=False "
            "so autonomous research cycles do not compete for dispatch slots"
        )

        # bootstrap_completed_at must be set so the bootstrap-recovery sweep
        # does not spawn a competing repo_bootstrap task.
        build_cfg = project.build_config or {}
        assert build_cfg.get("bootstrap_completed_at"), (
            "auto-created compute-experiment project must have bootstrap_completed_at "
            "set so the bootstrap-recovery sweep skips it"
        )

    @pytest.mark.asyncio
    async def test_arms_launch_synthesizes_command_and_persists_contract(
        self, client: AsyncClient, db_session
    ):
        """POST /api/admin/tasks/compute-experiment with arms + declared_metric
        must persist the arms/declared_metric on the payload and synthesize
        a command via the contract validator."""
        await self._project_with_repo(db_session)
        from app.shared_state import runtime_config as _rc

        previous_threshold = _rc.get("compute_experiment_spend_threshold_usd")
        _rc["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/admin/tasks/compute-experiment",
                json={
                    "github_repo": "acme/compute",
                    "title": "A vs B sweep",
                    "prompt": "Compare arm-A and arm-B",
                    "base_branch": "automain",
                    "arms": [
                        {"name": "arm-A", "command": "python train.py --lr 0.01"},
                        {"name": "arm-B", "command": "python train.py --lr 0.001"},
                    ],
                    "declared_metric": "val/accuracy",
                    "spend_cap_usd": 5.0,
                    "deadline_seconds": 900,
                },
            )
        finally:
            if previous_threshold is None:
                _rc.pop("compute_experiment_spend_threshold_usd", None)
            else:
                _rc["compute_experiment_spend_threshold_usd"] = previous_threshold

        assert resp.status_code == 200, resp.text
        data = resp.json()
        task = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(data["id"]))
            )
        ).scalar_one()
        payload = task.payloads[0]
        assert payload["type"] == "compute_experiment"
        # Arms and declared_metric must be persisted on the stored payload.
        assert payload["arms"] == [
            {"name": "arm-A", "command": "python train.py --lr 0.01"},
            {"name": "arm-B", "command": "python train.py --lr 0.001"},
        ]
        assert payload["declared_metric"] == "val/accuracy"
        # The contract validator synthesizes a command from the arms; it must be
        # a non-empty string (the actual script is an opaque base64 blob).
        assert isinstance(payload["command"], str) and payload["command"].strip()

    @pytest.mark.asyncio
    async def test_arms_without_declared_metric_rejected(
        self, client: AsyncClient, db_session
    ):
        """Providing arms without declared_metric must return 422."""
        await self._project_with_repo(db_session)
        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json={
                "github_repo": "acme/compute",
                "title": "Missing metric sweep",
                "prompt": "Compare arms without a declared metric",
                "base_branch": "automain",
                "arms": [
                    {"name": "arm-A", "command": "python train.py --lr 0.01"},
                    {"name": "arm-B", "command": "python train.py --lr 0.001"},
                ],
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
            },
        )
        assert resp.status_code == 422, resp.text
        assert "declared_metric" in resp.text

    @pytest.mark.asyncio
    async def test_neither_command_nor_arms_rejected(
        self, client: AsyncClient, db_session
    ):
        """Providing neither command nor arms must return 422."""
        await self._project_with_repo(db_session)
        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json={
                "github_repo": "acme/compute",
                "title": "Missing command sweep",
                "prompt": "No command or arms",
                "base_branch": "automain",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
            },
        )
        assert resp.status_code == 422, resp.text
        assert "command" in resp.text

    # ── Role floor ────────────────────────────────────────────────────────────
    #
    # This endpoint mints a compute_experiment with a FULL LAUNCH CONTRACT: it
    # spends real GPU budget against the project's spend_cap_usd. Membership
    # alone is not authorization to spend. Without the member+ floor, a viewer
    # denied POST /tasks/{id}/rerun could simply launch a fresh GPU job here
    # instead — and, by falling through to the auto-create branch, mint a shadow
    # project they own.

    async def _project_with_role(self, session, role, repo="compute"):
        project = Project(
            name=f"acme/{repo}",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo=repo,
            integration_branch="automain",
            promotion_target="automain",
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        session.add(
            ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role)
        )
        await session.commit()
        return project

    @pytest.mark.asyncio
    async def test_viewer_cannot_launch_compute_experiment(
        self, client: AsyncClient, db_session
    ):
        """Viewer → 403, no task enqueued, and NO shadow project auto-created."""
        from sqlalchemy import func as sa_func

        project = await self._project_with_role(db_session, "viewer")

        resp = await client.post(
            "/api/admin/tasks/compute-experiment", json=self._valid_payload()
        )

        assert resp.status_code == 403, resp.text
        assert "member" in resp.json()["error"]["message"].lower()

        # No compute_experiment task enqueued on the project...
        count = (
            await db_session.execute(
                select(sa_func.count(Task.id)).where(Task.project_id == project.id)
            )
        ).scalar_one()
        assert count == 0

        # ...and the 403 must NOT have fallen through into the auto-create
        # branch, which would hand the viewer a project they own outright.
        projects = (
            await db_session.execute(
                select(sa_func.count(Project.id)).where(
                    Project.github_owner == "acme", Project.github_repo == "compute"
                )
            )
        ).scalar_one()
        assert projects == 1

    @pytest.mark.asyncio
    async def test_member_can_launch_compute_experiment(
        self, client: AsyncClient, db_session
    ):
        """The floor must not over-reject: a member (not owner) can still launch."""
        from sqlalchemy import func as sa_func

        project = await self._project_with_role(db_session, "member")

        resp = await client.post(
            "/api/admin/tasks/compute-experiment", json=self._valid_payload()
        )

        assert resp.status_code == 200, resp.text
        count = (
            await db_session.execute(
                select(sa_func.count(Task.id)).where(Task.project_id == project.id)
            )
        ).scalar_one()
        assert count == 1


class TestRerunComputeExperiment:
    """POST /api/admin/tasks/{task_id}/rerun — re-run a compute_experiment."""

    async def _project_with_task(self, session, role="owner", **payload_overrides):
        project = Project(
            name="acme/rerun",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="rerun",
            integration_branch="automain",
            promotion_target="automain",
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role))
        base_payload = {
            "type": "compute_experiment",
            "title": "GPU bench",
            "prompt": "Run it.",
            "base_branch": "automain",
            "command": "python bench.py --out result.json",
            "spend_cap_usd": 5.0,
            "deadline_seconds": 900,
            "success_criterion": {
                "kind": "metric",
                "metric": "tokens_per_second",
                "operator": ">=",
                "threshold": 100.0,
                "min_file_count": 1,
            },
        }
        base_payload.update(payload_overrides)
        task = Task(
            project_id=project.id,
            title="GPU bench",
            description="Run it.",
            payloads=[base_payload],
            status=TaskStatus.COMPLETED,
            created_by="USER",
            launched_by_user_id=TEST_USER_ID,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return project, task

    @pytest.mark.asyncio
    async def test_rerun_creates_child_task_with_parent_link(
        self, client: AsyncClient, db_session
    ):
        """POST /rerun creates a QUEUED task with parent_task_id pointing at source."""
        _, source = await self._project_with_task(db_session)
        resp = await client.post(f"/api/admin/tasks/{source.id}/rerun", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["parent_task_id"] == str(source.id)
        child = (
            await db_session.execute(select(Task).where(Task.id == uuid.UUID(data["id"])))
        ).scalar_one()
        assert child.status == TaskStatus.QUEUED
        assert child.parent_task_id == source.id
        # Payload inherits source fields.
        assert child.payloads[0]["command"] == "python bench.py --out result.json"

    @pytest.mark.asyncio
    async def test_rerun_override_fields_take_precedence(
        self, client: AsyncClient, db_session
    ):
        """Caller-supplied overrides replace source payload fields."""
        _, source = await self._project_with_task(db_session)
        resp = await client.post(
            f"/api/admin/tasks/{source.id}/rerun",
            json={
                "spend_cap_usd": 2.5,
                "deadline_seconds": 600,
                "success_criterion": {
                    "metric": "tokens_per_second",
                    "operator": ">=",
                    "threshold": 150.0,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        child = (
            await db_session.execute(select(Task).where(Task.id == uuid.UUID(data["id"])))
        ).scalar_one()
        assert child.payloads[0]["spend_cap_usd"] == 2.5
        assert child.payloads[0]["deadline_seconds"] == 600
        assert child.payloads[0]["success_criterion"]["threshold"] == 150.0

    @pytest.mark.asyncio
    async def test_rerun_non_compute_task_returns_422(
        self, client: AsyncClient, db_session
    ):
        """Re-running a non-compute_experiment task returns 422."""
        project = Project(
            name="acme/other",
            user_id=TEST_USER_ID,
            github_owner="acme",
            github_repo="other",
            integration_branch="automain",
            promotion_target="automain",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        db_session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
        task = Task(
            project_id=project.id,
            title="Research task",
            description="Do research",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="USER",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        resp = await client.post(f"/api/admin/tasks/{task.id}/rerun", json={})
        assert resp.status_code == 422, resp.text
        assert "compute_experiment" in resp.text

    @pytest.mark.asyncio
    async def test_rerun_viewer_forbidden(self, client: AsyncClient, db_session):
        """A viewer may not relaunch a compute_experiment — it burns GPU spend.

        Membership alone is not authorization to spend: the re-run enqueues a
        new QUEUED task against the project's spend_cap_usd, so it requires the
        member+ floor (403), and no child task may be created.
        """
        _, source = await self._project_with_task(db_session, role="viewer")

        resp = await client.post(f"/api/admin/tasks/{source.id}/rerun", json={})
        assert resp.status_code == 403, resp.text

        children = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == source.id)
                )
            )
            .scalars()
            .all()
        )
        assert children == []

    @pytest.mark.asyncio
    async def test_rerun_member_allowed(self, client: AsyncClient, db_session):
        """The member floor must not over-reject: a member (not owner) can re-run."""
        _, source = await self._project_with_task(db_session, role="member")

        resp = await client.post(f"/api/admin/tasks/{source.id}/rerun", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["parent_task_id"] == str(source.id)


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
        # Simulate the local-HEAD backend actually being available so the absent
        # verdict is reached (not degraded to indeterminate by a missing backend).
        mocker.patch(
            "app.source_ref_validation._has_validation_backend",
            return_value=True,
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
            new=AsyncMock(side_effect=lambda path, **_: "alive.py" in path),
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
        mocker.patch(
            "app.source_ref_validation._has_validation_backend",
            return_value=True,
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
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalar_one()
        warnings = (task.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "absent"
        assert "backend/app/gone.py" in warnings["broken_refs"]
        # The provenance detail is carried on task.context only; the agent renders
        # it into the prompt at run time (formatInvestigateCitationProvenance in
        # agent/src/prompt.ts), so the backend no longer mutates the prompt string.
        assert "Provenance warning" not in task.payloads[0]["prompt"]
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
            new=AsyncMock(side_effect=lambda path, **_: "alive.py" in path),
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
            await db_session.execute(select(Task).where(Task.project_id == project.id))
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
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalar_one()
        verdict = (task.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"
        # Present citations leave the report untouched — no provenance warning.
        assert "_provenance_warnings" not in (task.context or {})
        assert "Provenance warning" not in task.payloads[0]["prompt"]

    @pytest.mark.asyncio
    async def test_investigate_task_partial_citation_seeds_indeterminate_with_note(
        self, client: AsyncClient, db_session, mocker
    ):
        """A partially-grounded bug report (one stale citation among surviving
        ones) is NOT treated as already-resolved: the verdict stays
        `indeterminate`, and the specific stale reference is preserved on the
        task context so the investigating agent can render it into its prompt
        (formatInvestigateCitationProvenance in agent/src/prompt.ts). The backend
        no longer bakes a prompt-string note."""
        project = await self._project_with_repo(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content=(
                "The flow spans backend/app/gone.py and backend/app/alive.py; "
                "the guard is dropped."
            ),
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        async def _exists(path, **_kw):
            return path != "backend/app/gone.py"

        mocker.patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.source_ref_validation._file_exists_at_head",
            side_effect=_exists,
        )
        mocker.patch(
            "app.source_ref_validation._has_validation_backend",
            return_value=True,
        )
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/citegate",
                "title": "Investigate partially-grounded report",
                "prompt": "Investigate the reported dropped guard.",
                "task_type": "investigate",
                "source_message_id": str(msg.id),
            },
        )
        assert resp.status_code == 200, resp.text
        task = (
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalar_one()
        # Mixed grounding ⇒ defect not presumed resolved.
        verdict = (task.context or {}).get("reproduce_verdict")
        assert verdict is not None
        assert verdict["status"] == "indeterminate"
        assert verdict["status"] != "already_resolved"
        # The stale reference is carried forward on the task context (broken_refs)
        # so the agent can name it in its prompt, without claiming the whole bug
        # is gone. The backend no longer mutates the prompt string itself.
        warnings = (task.context or {}).get("_provenance_warnings")
        assert warnings is not None
        assert warnings["citation_state"] == "present"
        assert "backend/app/gone.py" in warnings["broken_refs"]
        prompt = task.payloads[0]["prompt"]
        assert "Provenance note" not in prompt
        assert "Provenance warning" not in prompt

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
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalar_one()
        assert "_provenance_warnings" not in (task.context or {})
        assert task.payloads[0]["prompt"] == "Investigate something."


# ═══════════════════════════════════════════════════════════════════════════════
# task_capabilities.py — strategist spawn allowlist is registry-derived
# ═══════════════════════════════════════════════════════════════════════════════


class TestStrategistSpawnableRegistry:
    """Guard the registry-derived unified ``spawn_task`` policy."""

    def test_allowlist_includes_knowledge_and_normal_task_types(self):
        from app.task_capabilities import STRATEGIST_SPAWNABLE_TASK_TYPES

        expected = frozenset(
            {
                "create",
                "plan",
                "research",
                "qa",
                "refactor",
                "compute_experiment",
                "web_research",
                "design",
            }
        )
        assert expected == STRATEGIST_SPAWNABLE_TASK_TYPES

    def test_allowlists_are_derived_from_dispatch_policy(self):
        from app.task_capabilities import (
            STRATEGIST_CONTEXTUAL_TASK_TYPES,
            STRATEGIST_SPAWNABLE_TASK_TYPES,
            TASK_CAPABILITY_BY_VALUE,
        )

        direct = frozenset(
            value
            for value, cap in TASK_CAPABILITY_BY_VALUE.items()
            if cap.strategist_dispatch == "direct"
        )
        contextual = frozenset(
            value
            for value, cap in TASK_CAPABILITY_BY_VALUE.items()
            if cap.strategist_dispatch == "requires_context"
        )
        assert direct == STRATEGIST_SPAWNABLE_TASK_TYPES
        assert contextual == STRATEGIST_CONTEXTUAL_TASK_TYPES
        assert STRATEGIST_CONTEXTUAL_TASK_TYPES == frozenset()

    @pytest.mark.asyncio
    async def test_strategist_spawn_compute_experiment_persists_launch_contract(
        self, client: AsyncClient, db_session
    ):
        """Strategic spawn_task for compute_experiment persists launch contract fields
        in the task payload, applies the spend-gate, and does not require a prompt
        (hypothesis is the description).

        This exercises the spawn_task handler's compute_experiment branch:
        _author_compute_launch_contract + _validate_compute_launch_contract +
        resolve_compute_spend_approval, verifying the contract lands in the DB.
        """
        import app.shared_state as _shared_state
        from app.auth import create_agent_token

        project = Project(name="StrategistContractPersist", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for compute spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-strat-contract-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-strat-contract-test",
            task_type="strategic_review",
        )

        prev_threshold = _shared_state.runtime_config.get(
            "compute_experiment_spend_threshold_usd"
        )
        _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
        try:
            resp = await client.post(
                "/api/agent/strategic",
                headers={"x-agent-token": token},
                json={
                    "task_id": str(strategic_task.id),
                    "op": "spawn_task",
                    "data": {
                        "type": "compute_experiment",
                        "title": "Benchmark: throughput under load",
                        # No prompt — compute_experiment uses hypothesis instead
                        "hypothesis": "Batching requests reduces p99 latency by 30%",
                        "command": "python3 bench/run.py --batch 32",
                        "spend_cap_usd": 8.0,
                        "deadline_seconds": 900,
                        "success_criterion": {
                            "metric": "p99_latency_ms",
                            "operator": "<",
                            "threshold": 200,
                        },
                    },
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["op"] == "spawn_task"
            assert body["type"] == "compute_experiment"

            child_id = uuid.UUID(body["task_id"])
            child = await db_session.get(Task, child_id)
            assert child is not None

            # Launch contract fields must be persisted in the task payload
            payload = child.first_payload
            assert payload["type"] == "compute_experiment"
            assert payload["command"] == "python3 bench/run.py --batch 32"
            assert payload["spend_cap_usd"] == 8.0
            assert payload["deadline_seconds"] == 900
            assert payload["success_criterion"]["metric"] == "p99_latency_ms"
            assert payload["success_criterion"]["operator"] == "<"
            assert payload["success_criterion"]["threshold"] == 200

            # Spend gate: 8.0 > threshold 5.0 → requires_approval=True
            assert child.requires_approval is True

            # Spawned with strategic_direct context
            assert child.context["spawned_by"] == "strategic_direct"
            assert child.context["parent_strategic_task_id"] == str(strategic_task.id)
        finally:
            if prev_threshold is not None:
                _shared_state.runtime_config[
                    "compute_experiment_spend_threshold_usd"
                ] = prev_threshold
            else:
                _shared_state.runtime_config.pop(
                    "compute_experiment_spend_threshold_usd", None
                )


class TestStrategistSpawnCriterionIds:
    """Guard: spawn_task accepts explicit criterion_ids and acceptance_evidence.

    Explicit criterion_ids must be validated against active criteria (inactive
    keys silently dropped, exactly like direction-row inheritance).  An
    acceptance_evidence dict is forwarded into the task payload unchanged when
    well-formed, and rejected with a warning when malformed.
    """

    @pytest.mark.asyncio
    async def test_explicit_criterion_ids_stored_on_spawned_task(
        self, client: AsyncClient, db_session
    ):
        """criterion_ids passed to spawn_task land on the new task after
        filtering to active keys only (inactive / unknown keys are dropped)."""
        import uuid as _uuid
        from app.auth import create_agent_token
        from app.models import VersionAcceptanceCriterion

        project = Project(name="ExplicitCriterionIdSpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Seed one active and one inactive criterion for the open version.
        rv = await _make_open_version(db_session, project.id)

        active_key = f"c-active-{_uuid.uuid4().hex[:8]}"
        inactive_key = f"c-inactive-{_uuid.uuid4().hex[:8]}"
        db_session.add(
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=rv.id,
                criterion_key=active_key,
                status="active",
                text="Active criterion",
            )
        )
        db_session.add(
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=rv.id,
                criterion_key=inactive_key,
                status="retracted",
                text="Inactive criterion",
            )
        )
        await db_session.commit()

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for criterion spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-crit-spawn-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-crit-spawn-test",
            task_type="strategic_review",
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Investigate criterion coverage gap",
                    "prompt": "Research the coverage gap for the active criterion",
                    # Active key should survive; inactive should be dropped.
                    "criterion_ids": [active_key, inactive_key],
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_admin_routers.py",
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "spawn_task"

        child = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert child is not None
        # Only the active key survives the filter.
        assert child.criterion_ids == [active_key], (
            f"expected [{active_key!r}], got {child.criterion_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_criterion_ids_merged_with_direction_keys(
        self, client: AsyncClient, db_session
    ):
        """When both direction_id and explicit criterion_ids are supplied, the
        spawned task receives the union of both sources (filtered to active)."""
        import uuid as _uuid
        from app.auth import create_agent_token
        from app.models import VersionAcceptanceCriterion

        project = Project(name="MergedCriterionSpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        rv = await _make_open_version(db_session, project.id)

        dir_key = f"c-dir-{_uuid.uuid4().hex[:8]}"
        explicit_key = f"c-explicit-{_uuid.uuid4().hex[:8]}"
        for key in (dir_key, explicit_key):
            db_session.add(
                VersionAcceptanceCriterion(
                    project_id=project.id,
                    release_version_id=rv.id,
                    criterion_key=key,
                    status="active",
                    text=f"Criterion {key}",
                )
            )
        await db_session.commit()

        direction_id_str = f"d-{_uuid.uuid4().hex[:8]}"
        direction = Direction(
            project_id=project.id,
            direction_key=direction_id_str,
            status="active",
            text="Direction with criterion",
            data={"id": direction_id_str, "status": "active", "text": "Direction with criterion"},
            criterion_keys=[dir_key],
        )
        db_session.add(direction)
        await db_session.commit()

        # Project.directions must include the direction dict for the id-check in the handler.
        project.directions = [
            {"id": direction_id_str, "status": "active", "text": "Direction with criterion"}
        ]
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for merged criterion spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-merged-crit-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-merged-crit-test",
            task_type="strategic_review",
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with merged criteria",
                    "prompt": "Investigate both criterion areas",
                    "direction_id": direction_id_str,
                    "criterion_ids": [explicit_key],
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_admin_routers.py",
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        child = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert child is not None
        # Both the explicit key and the direction-derived key must be present.
        assert set(child.criterion_ids) == {dir_key, explicit_key}, (
            f"expected both keys, got {child.criterion_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_acceptance_evidence_forwarded_to_task_payload(
        self, client: AsyncClient, db_session
    ):
        """A well-formed acceptance_evidence dict is forwarded into the spawned
        task's payload verbatim."""
        import uuid as _uuid
        from app.auth import create_agent_token

        project = Project(name="AcceptanceEvidenceSpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for acceptance evidence spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-ae-spawn-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-ae-spawn-test",
            task_type="strategic_review",
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with acceptance evidence",
                    "prompt": "Investigate criterion coverage",
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_acceptance_criteria.py",
                        "test_id": "test_coverage_gate",
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        child = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert child is not None
        payload = child.first_payload
        assert "acceptance_evidence" in payload, (
            "acceptance_evidence must be forwarded into task payload"
        )
        ae = payload["acceptance_evidence"]
        assert ae["kind"] == "named_test"
        assert ae["test_path"] == "testing/test_acceptance_criteria.py"
        assert ae["test_id"] == "test_coverage_gate"

    @pytest.mark.asyncio
    async def test_malformed_acceptance_evidence_silently_warned(
        self, client: AsyncClient, db_session
    ):
        """A malformed acceptance_evidence is dropped with a warning (not a 400)
        so the spawn still succeeds — the declaration is advisory, not gate-blocking."""
        import uuid as _uuid
        from app.auth import create_agent_token

        project = Project(name="MalformedAESpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for malformed AE spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-bad-ae-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-bad-ae-test",
            task_type="strategic_review",
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with bad acceptance evidence",
                    "prompt": "Investigate coverage",
                    # Missing required fields — kind=named_test but no test_path.
                    "acceptance_evidence": {"kind": "named_test"},
                },
            },
        )
        # Should succeed (not a 400) — bad AE is a warning, not a hard failure.
        assert resp.status_code == 200, resp.text
        body = resp.json()

        child = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert child is not None
        payload = child.first_payload
        # Malformed AE must not land in the payload.
        assert "acceptance_evidence" not in payload

    @pytest.mark.asyncio
    async def test_criterion_ids_non_list_rejected(
        self, client: AsyncClient, db_session
    ):
        """criterion_ids must be a list; a non-list value returns 400."""
        from app.auth import create_agent_token

        project = Project(name="BadCriterionIdSpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for bad criterion spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-bad-crit-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-bad-crit-test",
            task_type="strategic_review",
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with bad criterion_ids type",
                    "prompt": "Investigate coverage",
                    "criterion_ids": "not-a-list",
                },
            },
        )
        assert resp.status_code == 400
        assert "criterion_ids" in resp.json().get("error", {}).get("message", "")

    @pytest.mark.asyncio
    async def test_spawn_task_criterion_ids_requires_acceptance_evidence(
        self, client: AsyncClient, db_session
    ):
        """criterion_ids without named_test acceptance_evidence is rejected (400).

        A strategist who passes criterion_ids but omits acceptance_evidence (or
        passes a non-named_test kind) creates a task that can never record a
        proven-verdict for those criteria — silently defeating the
        criterion-coverage gate. The endpoint must reject this at spawn time
        rather than let the task proceed to a merge that records nothing.
        """
        import uuid as _uuid
        from app.auth import create_agent_token
        from app.models import VersionAcceptanceCriterion

        project = Project(name="CritIdsReqAESpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        rv = await _make_open_version(db_session, project.id)
        active_key = f"c-active-{_uuid.uuid4().hex[:8]}"
        db_session.add(
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=rv.id,
                criterion_key=active_key,
                status="active",
                text="Active criterion requiring evidence",
            )
        )
        await db_session.commit()

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for criterion-ids-no-evidence",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-crit-no-ae-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-crit-no-ae-test",
            task_type="strategic_review",
        )

        # Case 1: criterion_ids without acceptance_evidence → 400
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with criteria but no evidence",
                    "prompt": "Investigate the criterion coverage gap",
                    "criterion_ids": [active_key],
                },
            },
        )
        assert resp.status_code == 400, (
            f"expected 400 when criterion_ids lacks acceptance_evidence, "
            f"got {resp.status_code}: {resp.text}"
        )
        msg = resp.json().get("detail") or resp.json().get("error", {}).get("message", "")
        assert "acceptance_evidence" in msg, (
            f"error must mention acceptance_evidence, got: {msg!r}"
        )
        assert "named_test" in msg, (
            f"error must mention named_test, got: {msg!r}"
        )
        assert "criterion_ids" in msg, (
            f"error must mention criterion_ids, got: {msg!r}"
        )

        # Case 2: criterion_ids with non-named_test acceptance_evidence → 400
        resp2 = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with criteria and wrong evidence kind",
                    "prompt": "Investigate the criterion coverage gap",
                    "criterion_ids": [active_key],
                    "acceptance_evidence": {
                        "kind": "observable_behavior",
                        "description": "the task produces a test that passes",
                    },
                },
            },
        )
        assert resp2.status_code == 400, (
            f"expected 400 when criterion_ids has non-named_test acceptance_evidence, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        msg2 = resp2.json().get("detail") or resp2.json().get("error", {}).get("message", "")
        assert "named_test" in msg2, (
            f"error must mention named_test, got: {msg2!r}"
        )

        # Case 3: criterion_ids with named_test acceptance_evidence → 200
        resp3 = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with criteria and named_test evidence",
                    "prompt": "Investigate the criterion coverage gap",
                    "criterion_ids": [active_key],
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_admin_routers.py",
                    },
                },
            },
        )
        assert resp3.status_code == 200, (
            f"expected 200 when criterion_ids has named_test acceptance_evidence, "
            f"got {resp3.status_code}: {resp3.text}"
        )

    @pytest.mark.asyncio
    async def test_direction_criterion_keys_require_acceptance_evidence(
        self, client: AsyncClient, db_session
    ):
        """Direction-derived criterion_keys without named_test acceptance_evidence
        is rejected (400).

        When criterion_ids come from the direction row (not the explicit
        strategist payload), the guard must still reject non-named_test
        acceptance_evidence — otherwise the spawned task would get a proven=False
        verdict at merge time and never record criterion evidence.
        """
        import uuid as _uuid
        from app.auth import create_agent_token
        from app.models import VersionAcceptanceCriterion

        project = Project(name="DirCritKeyReqAESpawn", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        rv = await _make_open_version(db_session, project.id)
        dir_key = f"c-dir-{_uuid.uuid4().hex[:8]}"
        db_session.add(
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=rv.id,
                criterion_key=dir_key,
                status="active",
                text="Direction criterion requiring evidence",
            )
        )
        await db_session.commit()

        direction_id_str = f"d-{_uuid.uuid4().hex[:8]}"
        direction = Direction(
            project_id=project.id,
            direction_key=direction_id_str,
            status="active",
            text="Direction with criterion_keys",
            data={"id": direction_id_str, "status": "active", "text": "Direction"},
            criterion_keys=[dir_key],
        )
        db_session.add(direction)
        await db_session.commit()

        project.directions = [
            {"id": direction_id_str, "status": "active", "text": "Direction"}
        ]
        from sqlalchemy.orm.attributes import flag_modified as _flag_modified
        _flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for dir-crit-no-evidence",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-dir-crit-no-ae-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-dir-crit-no-ae-test",
            task_type="strategic_review",
        )

        # Case 1: direction_id (direction has criterion_keys) without acceptance_evidence → 400
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with direction criterion_keys but no evidence",
                    "prompt": "Investigate the direction-criterion gap",
                    "direction_id": direction_id_str,
                },
            },
        )
        assert resp.status_code == 400, (
            f"expected 400 when direction criterion_keys lacks acceptance_evidence, "
            f"got {resp.status_code}: {resp.text}"
        )
        msg = resp.json().get("detail") or resp.json().get("error", {}).get("message", "")
        assert "acceptance_evidence" in msg, (
            f"error must mention acceptance_evidence, got: {msg!r}"
        )
        assert "named_test" in msg, (
            f"error must mention named_test, got: {msg!r}"
        )

        # Case 2: direction_id with non-named_test acceptance_evidence → 400
        resp2 = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with direction criterion_keys wrong evidence",
                    "prompt": "Investigate the direction-criterion gap",
                    "direction_id": direction_id_str,
                    "acceptance_evidence": {
                        "kind": "observable_behavior",
                        "description": "the task produces a passing test",
                    },
                },
            },
        )
        assert resp2.status_code == 400, (
            f"expected 400 when direction criterion_keys has non-named_test evidence, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        msg2 = resp2.json().get("detail") or resp2.json().get("error", {}).get("message", "")
        assert "named_test" in msg2, (
            f"error must mention named_test, got: {msg2!r}"
        )

        # Case 3: direction_id with named_test acceptance_evidence → 200
        resp3 = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Research with direction criterion_keys and named_test",
                    "prompt": "Investigate the direction-criterion gap",
                    "direction_id": direction_id_str,
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_admin_routers.py",
                    },
                },
            },
        )
        assert resp3.status_code == 200, (
            f"expected 200 when direction criterion_keys has named_test evidence, "
            f"got {resp3.status_code}: {resp3.text}"
        )


@pytest.mark.asyncio
async def test_qa_task_receives_rejection_patterns(client: AsyncClient, db_session):
    """A qa task's claim ships project_context.rejection_patterns.

    qa writes real test code and ships through the same code-review gate as
    create/fix; widening CODE_REVIEW_GATED_TASK_TYPES brings it into the same
    per-type rejection-pattern launch context that create/fix already receive.
    """
    from app.auth import create_agent_token
    from testing.factories import _utcnow, make_project, make_task

    project = make_project(name="QA Rejection Patterns")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from datetime import timedelta

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title="qa task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "qa", "prompt": "do work"}],
                context={
                    "_first_attempt_rejection_categories": {
                        "test-coverage": 2,
                        "correctness": 1,
                    }
                },
                created_at=_utcnow() - timedelta(hours=1),
            )
        )
    await db_session.commit()

    vm_id = f"agent-vm-qa-rej-{uuid.uuid4().hex[:8]}"
    task = make_task(
        project_id=project.id,
        title="qa claim",
        payloads=[{"type": "qa", "prompt": "Add tests"}],
        vm_instance_id=vm_id,
        context={"related_files": ["backend/app/foo.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context", {})
    block = project_ctx.get("rejection_patterns")
    assert block is not None, (
        f"expected rejection_patterns in project_context, got {list(project_ctx.keys())}"
    )
    assert block["task_type"] == "qa"
    categories = {c["category"] for c in block["categories"]}
    assert "test-coverage" in categories


# ═══════════════════════════════════════════════════════════════════════════════
# fleet-utilization endpoint
# ═══════════════════════════════════════════════════════════════════════════════


class TestFleetUtilizationEndpoint:
    @pytest.mark.asyncio
    async def test_fleet_utilization_returns_snapshot_for_admin(
        self, client: AsyncClient, db_session
    ):
        """Authenticated admin GET of /api/admin/fleet-utilization returns 200
        with utilization snapshot fields and a saturation state."""
        resp = await client.get("/api/admin/fleet-utilization")
        assert resp.status_code == 200
        data = resp.json()
        # All snapshot fields from get_fleet_utilization_snapshot must be present
        for field in (
            "active_now",
            "nominal_capacity",
            "usable_capacity",
            "cooled_accounts",
            "util_now",
            "util_avg",
            "window_hours",
            "concurrent_peak",
            "concurrent_p50",
            "concurrent_p90",
            "concurrent_p95",
            "concurrent_p99",
        ):
            assert field in data, f"missing field: {field}"
        # Derived saturation field must be present (may be None when no accounts)
        assert "saturation" in data
        # saturation is either None or one of the three valid labels
        if data["saturation"] is not None:
            assert data["saturation"] in ("healthy", "busy", "saturated")

    @pytest.mark.asyncio
    async def test_fleet_utilization_requires_admin(
        self, override_get_session, db_session
    ):
        """Non-admin caller is rejected with 403."""
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
                resp = await ac.get("/api/admin/fleet-utilization")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(require_admin, None)


class TestDeriveSaturation:
    """Unit tests for the pure _derive_saturation helper."""

    def test_healthy_when_util_avg_below_threshold(self):
        from app.routers.admin_health import _derive_saturation

        assert _derive_saturation(0.0, 0.0) == "healthy"
        assert _derive_saturation(0.5, 0.5) == "healthy"
        assert _derive_saturation(0.699, 0.9) == "healthy"

    def test_busy_when_util_avg_in_middle_band(self):
        from app.routers.admin_health import _derive_saturation

        assert _derive_saturation(0.70, None) == "busy"
        assert _derive_saturation(0.80, None) == "busy"
        assert _derive_saturation(0.899, None) == "busy"

    def test_saturated_when_util_avg_at_or_above_upper_threshold(self):
        from app.routers.admin_health import _derive_saturation

        assert _derive_saturation(0.90, None) == "saturated"
        assert _derive_saturation(1.0, None) == "saturated"
        assert _derive_saturation(1.5, None) == "saturated"

    def test_none_when_util_avg_is_none(self):
        from app.routers.admin_health import _derive_saturation

        assert _derive_saturation(None, None) is None
        assert _derive_saturation(None, 0.5) is None


class TestGetBuildConfigCommands:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401_or_403(self, override_get_session):
        """Unauthenticated GET to build-config/commands must be rejected."""
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    f"/api/admin/projects/{uuid.uuid4()}/build-config/commands"
                )
            assert resp.status_code in (401, 403)
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_admin_authenticated_returns_200(
        self, client: AsyncClient, db_session
    ):
        """Authenticated admin GET returns 200 with directories payload."""
        project = await _make_project(db_session, name="BuildCfg Commands")
        project.build_config = {
            "directories": [
                {
                    "path": "backend",
                    "type": "python",
                    "test_cmd": "pytest",
                    "build_cmd": None,
                    "install_cmd": "pip install -r requirements.txt",
                    "lint_cmd": "ruff check .",
                    "confirmed_test_cmd": "pytest -x",
                    "confirmed_build_cmd": None,
                    "confirmed_install_cmd": None,
                    "confirmed_lint_cmd": None,
                    "commands_confirmed_at": None,
                    "confirming_task_id": None,
                }
            ]
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/build-config/commands"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == str(project.id)
        assert len(data["directories"]) == 1
        assert data["directories"][0]["path"] == "backend"
        assert data["directories"][0]["detected_test_cmd"] == "pytest"
        assert data["directories"][0]["confirmed_test_cmd"] == "pytest -x"


# ═══════════════════════════════════════════════════════════════════════════════
# Improvement-lane pilot flag settings
# ═══════════════════════════════════════════════════════════════════════════════


class TestImprovementLaneSettings:
    """PATCH /api/admin/settings round-trip tests for improvement_lanes_enabled
    and review_fix_lane_pilot_enabled."""

    @pytest.mark.asyncio
    async def test_settings_patch_improvement_lane_flags(self, client: AsyncClient):
        """PATCH both flags true/false; GET confirms round-trip; non-bool is rejected."""
        # Default: both flags default False in GET response.
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert "improvement_lanes_enabled" in data
        assert "review_fix_lane_pilot_enabled" in data

        # warm_pool_enabled is required; enable it first.
        resp = await client.patch(
            "/api/admin/settings", json={"warm_pool_enabled": True}
        )
        assert resp.status_code == 200

        # Enable improvement_lanes_enabled.
        resp = await client.patch(
            "/api/admin/settings", json={"improvement_lanes_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["improvement_lanes_enabled"] is True

        # Enable review_fix_lane_pilot_enabled.
        resp = await client.patch(
            "/api/admin/settings", json={"review_fix_lane_pilot_enabled": True}
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["review_fix_lane_pilot_enabled"] is True

        # Disable both in a single PATCH.
        resp = await client.patch(
            "/api/admin/settings",
            json={
                "improvement_lanes_enabled": False,
                "review_fix_lane_pilot_enabled": False,
            },
        )
        assert resp.status_code == 200
        get_resp = await client.get("/api/admin/settings")
        body = get_resp.json()
        assert body["improvement_lanes_enabled"] is False
        assert body["review_fix_lane_pilot_enabled"] is False

        # Non-boolean values must be rejected (strings, ints).
        bad1 = await client.patch(
            "/api/admin/settings", json={"improvement_lanes_enabled": "true"}
        )
        assert bad1.status_code == 400

        bad2 = await client.patch(
            "/api/admin/settings", json={"review_fix_lane_pilot_enabled": 1}
        )
        assert bad2.status_code == 400

    @pytest.mark.asyncio
    async def test_settings_patch_unknown_key_still_rejected(self, client: AsyncClient):
        """A typo in a flag name must still be rejected (unknown-key gate intact)."""
        resp = await client.patch(
            "/api/admin/settings", json={"improvement_lanes_enabld": True}
        )
        assert resp.status_code == 400
        assert (
            "unknown settings key" in resp.text.lower()
            or "improvement_lanes_enabld" in resp.text
        )

    @pytest.mark.asyncio
    async def test_improvement_lanes_enforces_warm_pool_enabled(
        self, client: AsyncClient
    ):
        """Enabling improvement_lanes_enabled fails if warm_pool_enabled is false."""
        # First disable warm_pool_enabled.
        resp = await client.patch(
            "/api/admin/settings", json={"warm_pool_enabled": False}
        )
        assert resp.status_code == 200

        # Attempt to enable improvement_lanes_enabled should fail with 422.
        resp = await client.patch(
            "/api/admin/settings", json={"improvement_lanes_enabled": True}
        )
        assert resp.status_code == 422
        data = resp.json()
        error_msg = data.get("error", {}).get("message", {})
        assert isinstance(error_msg, dict)
        assert error_msg.get("warm_pool_enabled") == "not met"
        assert "warm_pool_enabled=true" in error_msg.get("fix_hint", "")

    @pytest.mark.asyncio
    async def test_review_fix_lane_pilot_enforces_warm_pool_enabled(
        self, client: AsyncClient
    ):
        """Enabling review_fix_lane_pilot_enabled fails if warm_pool_enabled is false."""
        # First disable warm_pool_enabled.
        resp = await client.patch(
            "/api/admin/settings", json={"warm_pool_enabled": False}
        )
        assert resp.status_code == 200

        # Attempt to enable review_fix_lane_pilot_enabled should fail with 422.
        resp = await client.patch(
            "/api/admin/settings", json={"review_fix_lane_pilot_enabled": True}
        )
        assert resp.status_code == 422
        data = resp.json()
        error_msg = data.get("error", {}).get("message", {})
        assert isinstance(error_msg, dict)
        assert error_msg.get("warm_pool_enabled") == "not met"
        assert "warm_pool_enabled=true" in error_msg.get("fix_hint", "")


# ---------------------------------------------------------------------------
# Spawn-gate census endpoint
# ---------------------------------------------------------------------------


class TestSpawnGateCensus:
    """Tests for GET /api/admin/spawn-gate-census."""

    @pytest.mark.asyncio
    async def test_spawn_gate_census_returns_aggregate(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns aggregate gate census for research tasks in the window."""
        from testing.factories import make_project, make_task

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={
                "research_spawn_gate_challenged_count": 3,
                "research_spawn_gate_parse_outcome": "no_verdict_returned",
                "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 2},
                "research_spawn_gate_wholesale_retry_exhausted": True,
            },
        )
        db_session.add(research_task)
        # Non-research task — must be excluded.
        create_task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"research_spawn_gate_challenged_count": 5},
        )
        db_session.add(create_task)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spawn-gate-census?project_id={project.id}&hours=24"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_hours"] == 24
        assert data["cycles_scanned"] == 1
        assert data["gate_invoked_count"] == 1
        assert data["graders_attempted_total"] == 3
        assert data["exhaustion_count"] == 1
        assert data["give_up_count"] == 0
        assert data["parse_outcome_distribution"] == {"no_verdict_returned": 1}
        assert data["cause_distribution"] == {"AGENT_TIMEOUT": 2}
        rows = data["rows"]
        assert len(rows) == 1
        assert rows[0]["gate_invoked"] is True
        assert rows[0]["exhausted"] is True

    @pytest.mark.asyncio
    async def test_spawn_gate_census_empty_window(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns zero counts when no tasks exist."""
        resp = await client.get("/api/admin/spawn-gate-census?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cycles_scanned"] == 0
        assert data["gate_invoked_count"] == 0
        assert data["rows"] == []

    @pytest.mark.asyncio
    async def test_spawn_gate_census_project_filter(
        self, client: AsyncClient, db_session
    ):
        """project_id filter excludes tasks from other projects."""
        from testing.factories import make_project, make_task

        proj_a = make_project()
        proj_b = make_project()
        db_session.add(proj_a)
        db_session.add(proj_b)
        await db_session.commit()
        await db_session.refresh(proj_a)
        await db_session.refresh(proj_b)

        db_session.add(
            make_task(
                project_id=proj_a.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"research_spawn_gate_challenged_count": 2},
            )
        )
        db_session.add(
            make_task(
                project_id=proj_b.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"research_spawn_gate_challenged_count": 4},
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spawn-gate-census?project_id={proj_a.id}&hours=24"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cycles_scanned"] == 1
        assert data["graders_attempted_total"] == 2


class TestCanaryAdversarialChallenge:
    """Tests for POST/GET /api/admin/projects/{id}/canary/adversarial-challenge."""

    @pytest.mark.asyncio
    async def test_post_spawns_canary_and_returns_ids(
        self, client: AsyncClient, db_session
    ):
        """Happy path: POST spawns the canary and grader, returns both task IDs."""
        from unittest.mock import AsyncMock, patch

        project = await _make_project(db_session)
        grader_id = uuid.uuid4()

        mock_grader = Task(
            project_id=project.id,
            title="[GRADE] canary grader",
            payloads=[{"type": "investigate", "scope": "small"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        mock_grader.id = grader_id

        with patch(
            "app.routers.admin_debug.spawn_forward_quality_grader",
            new_callable=AsyncMock,
            return_value=mock_grader,
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/canary/adversarial-challenge"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "canary_task_id" in data
        assert data["grader_task_id"] == str(grader_id)
        # canary_task_id is a valid UUID
        uuid.UUID(data["canary_task_id"])

    @pytest.mark.asyncio
    async def test_post_returns_409_when_grader_already_in_flight(
        self, client: AsyncClient, db_session
    ):
        """409 is returned when spawn_forward_quality_grader returns None (already in flight)."""
        from unittest.mock import AsyncMock, patch

        project = await _make_project(db_session)

        with patch(
            "app.routers.admin_debug.spawn_forward_quality_grader",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/canary/adversarial-challenge"
            )

        assert resp.status_code == 409
        assert "already in flight" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_viewer_forbidden(self, client: AsyncClient, db_session):
        """Viewer → 403: this commits a synthetic Task and spawns a REAL agent
        task that executes on a VM (LLM spend + compute). Membership alone does
        not authorize that — the sibling reset_circuit_breaker already takes the
        member floor. No canary task may be persisted.
        """
        from unittest.mock import AsyncMock, patch
        from sqlalchemy import func as sa_func

        project = await _make_project(
            db_session, name="Viewer Canary", role="viewer"
        )

        with patch(
            "app.routers.admin_debug.spawn_forward_quality_grader",
            new_callable=AsyncMock,
        ) as spawn:
            resp = await client.post(
                f"/api/admin/projects/{project.id}/canary/adversarial-challenge"
            )

        assert resp.status_code == 403, resp.text
        assert "member" in resp.json()["error"]["message"].lower()
        # The gate must reject BEFORE any agent work is spawned.
        spawn.assert_not_awaited()

        count = (
            await db_session.execute(
                select(sa_func.count(Task.id)).where(Task.project_id == project.id)
            )
        ).scalar_one()
        assert count == 0

    @pytest.mark.asyncio
    async def test_post_member_allowed(self, client: AsyncClient, db_session):
        """The floor must not over-reject: a member (not owner) can run the canary."""
        from unittest.mock import AsyncMock, patch

        project = await _make_project(
            db_session, name="Member Canary", role="member"
        )
        grader_id = uuid.uuid4()
        mock_grader = Task(
            project_id=project.id,
            title="[GRADE] canary grader",
            payloads=[{"type": "investigate", "scope": "small"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        mock_grader.id = grader_id

        with patch(
            "app.routers.admin_debug.spawn_forward_quality_grader",
            new_callable=AsyncMock,
            return_value=mock_grader,
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/canary/adversarial-challenge"
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["grader_task_id"] == str(grader_id)

    @pytest.mark.asyncio
    async def test_get_returns_404_when_task_not_found(
        self, client: AsyncClient, db_session
    ):
        """GET returns 404 when the canary_task_id does not exist."""
        project = await _make_project(db_session)
        bogus_id = uuid.uuid4()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/canary/adversarial-challenge/{bogus_id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_returns_404_when_task_is_not_canary(
        self, client: AsyncClient, db_session
    ):
        """GET rejects tasks not flagged as canaries (_is_canary not in context)."""
        from testing.factories import make_task

        project = await _make_project(db_session)
        non_canary = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={},  # no _is_canary
        )
        db_session.add(non_canary)
        await db_session.commit()
        await db_session.refresh(non_canary)

        resp = await client.get(
            f"/api/admin/projects/{project.id}/canary/adversarial-challenge/{non_canary.id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_returns_pending_when_no_gate_record(
        self, client: AsyncClient, db_session
    ):
        """GET returns status=pending when the grader has not yet written a verdict."""
        from testing.factories import make_task

        project = await _make_project(db_session)
        canary = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            context={
                "_is_canary": True,
                "awaiting_adversarial_grader": str(uuid.uuid4()),
            },
        )
        db_session.add(canary)
        await db_session.commit()
        await db_session.refresh(canary)

        resp = await client.get(
            f"/api/admin/projects/{project.id}/canary/adversarial-challenge/{canary.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["canary_task_id"] == str(canary.id)
        assert "grader_task_id" in data

    @pytest.mark.asyncio
    async def test_get_returns_grader_task_id_from_db(
        self, client: AsyncClient, db_session
    ):
        """GET includes grader_task_id from the canary context in the response."""
        from app.task_evaluators import (
            ADVERSARIAL_GATE_VERDICT_KEY,
            ADVERSARIAL_PARSE_OK,
        )
        from testing.factories import make_task

        project = await _make_project(db_session)
        grader_id = str(uuid.uuid4())
        gate_record = {
            "parse_outcome": ADVERSARIAL_PARSE_OK,
            "finding_verdicts": {"hash-abc": {"explicit": True, "verdict": "pass"}},
            "grader_task_id": grader_id,
        }
        canary = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "_is_canary": True,
                ADVERSARIAL_GATE_VERDICT_KEY: gate_record,
            },
        )
        db_session.add(canary)
        await db_session.commit()
        await db_session.refresh(canary)

        resp = await client.get(
            f"/api/admin/projects/{project.id}/canary/adversarial-challenge/{canary.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "real_verdict"
        assert data["explicit_count"] == 1
        assert data["grader_task_id"] == grader_id
        assert data["canary_task_id"] == str(canary.id)


# ═══════════════════════════════════════════════════════════════════════════════
# admin_release_version.py — Get Version Summary
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetVersionSummary:
    """Tests for GET /api/admin/projects/{project_id}/release-versions/{version_number}/summary."""

    URL = "/api/admin/projects/{project_id}/release-versions/{version_number}/summary"

    @pytest.mark.asyncio
    async def test_version_summary_endpoint(self, client: AsyncClient, db_session):
        """Returns summary fields for an open version."""
        project = await _make_project(db_session)
        opened = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
            opened_at=opened,
            qualification_attempts=0,
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=3),
            ),
            patch(
                "app.release_version.compute_version_acceptance_aggregate",
                new=AsyncMock(return_value={"proven": 2, "unproven": 1, "total": 3}),
            ),
            patch(
                "app.release_version.list_merged_tasks_for_version",
                new=AsyncMock(return_value=[]),
            ),
        ):
            resp = await client.get(
                self.URL.format(project_id=project.id, version_number=1)
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["version_number"] == 1
        assert data["phase"] == "build"
        assert data["status"] == "open"
        assert data["promoted_sha"] is None
        assert data["promoted_at"] is None
        assert data["merged_task_count"] == 3
        assert data["merged_count"] == 3
        assert data["acceptance_aggregate"]["proven"] == 2
        assert data["acceptance_aggregate"]["total"] == 3
        assert data["qualification_attempts"] == 0
        assert isinstance(data["highlights"], dict)
        assert "3" in data["highlights"].get("headline", "")
        assert isinstance(data["tasks_merged"], list)
        assert "qualification_state" in data

    @pytest.mark.asyncio
    async def test_version_summary_promoted_version(
        self, client: AsyncClient, db_session
    ):
        """Returns promoted_sha and promoted_at for a promoted version."""
        project = await _make_project(db_session)
        opened = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        promoted = datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="promoted",
            opened_at=opened,
            promoted_at=promoted,
            promoted_sha="abc1234def5678",
            qualification_attempts=1,
        )
        db_session.add(version)
        await db_session.commit()

        with (
            patch(
                "app.release_version.count_merged_since",
                new=AsyncMock(return_value=5),
            ),
            patch(
                "app.release_version.compute_version_acceptance_aggregate",
                new=AsyncMock(return_value={"proven": 4, "unproven": 1, "total": 5}),
            ),
            patch(
                "app.release_version.list_merged_tasks_for_version",
                new=AsyncMock(return_value=[]),
            ),
        ):
            resp = await client.get(
                self.URL.format(project_id=project.id, version_number=2)
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "promoted"
        assert data["promoted_sha"] == "abc1234def5678"
        assert data["promoted_at"] is not None
        assert isinstance(data["highlights"], dict)
        assert "promoted" in data["highlights"].get("headline", "")

    @pytest.mark.asyncio
    async def test_version_summary_404_for_nonexistent_version(
        self, client: AsyncClient, db_session
    ):
        """Returns 404 when the version_number does not exist for this project."""
        project = await _make_project(db_session)
        resp = await client.get(
            self.URL.format(project_id=project.id, version_number=99)
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_version_summary_404_for_non_member(
        self, override_get_session, db_session
    ):
        """Returns 404 when the user is not a member of the project."""
        project = await _make_project(db_session, owner_id="owner-user")
        opened = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
            opened_at=opened,
        )
        db_session.add(version)
        await db_session.commit()

        non_member_id = "non-member-999"

        def non_member():
            return {"user_id": non_member_id, "email": "other@example.com"}

        app.dependency_overrides[get_current_user] = non_member
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    self.URL.format(project_id=project.id, version_number=1)
                )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        # Non-member gets 403 or 404 depending on implementation.
        assert resp.status_code in (403, 404)

    @pytest.mark.asyncio
    async def test_version_summary_matches_canonical_for_closed_version(
        self, client: AsyncClient, db_session
    ):
        """Project-scoped endpoint returns window-bounded merged_count for a promoted
        version — matching the canonical UUID endpoint behavior.

        Verifies the acceptance-evidence proof: the endpoint calls
        count_merged_since with an until bound equal to promoted_at, so tasks
        that merged after promotion are excluded (no overcount).
        """
        from unittest.mock import AsyncMock, patch

        project = await _make_project(db_session)
        opened = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        promoted = datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="stabilize",
            status="promoted",
            opened_at=opened,
            promoted_at=promoted,
            promoted_sha="deadbeef1234",
            qualification_attempts=2,
        )
        db_session.add(version)
        await db_session.commit()

        count_mock = AsyncMock(return_value=4)
        tasks_mock = AsyncMock(
            return_value=[
                {
                    "task_id": "t1",
                    "title": "Fix login",
                    "pr_number": 101,
                    "task_type": "fix",
                },
            ]
        )
        aggregate_mock = AsyncMock(
            return_value={"proven": 3, "unproven": 1, "total": 4}
        )

        with (
            patch("app.release_version.count_merged_since", new=count_mock),
            patch("app.release_version.list_merged_tasks_for_version", new=tasks_mock),
            patch(
                "app.release_version.compute_version_acceptance_aggregate",
                new=aggregate_mock,
            ),
        ):
            resp = await client.get(
                self.URL.format(project_id=project.id, version_number=3)
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Window is bounded: count_merged_since must be called with until=promoted_at
        assert count_mock.call_count == 1
        call_kwargs = count_mock.call_args
        # until arg must be set (not None) for a promoted version
        until_arg = call_kwargs.kwargs.get("until") or (
            call_kwargs.args[3] if len(call_kwargs.args) > 3 else None
        )
        assert until_arg is not None, (
            "count_merged_since must be called with until= bound for promoted version"
        )

        # Rich payload fields present
        assert data["merged_count"] == 4
        assert data["merged_task_count"] == 4  # backward-compat alias
        assert isinstance(data["tasks_merged"], list)
        assert len(data["tasks_merged"]) == 1
        assert data["tasks_merged"][0]["pr_number"] == 101
        assert isinstance(data["highlights"], dict)
        assert "headline" in data["highlights"]
        assert "promoted" in data["highlights"]["headline"]
        assert "qualification_state" in data
        assert data["status"] == "promoted"
        assert data["promoted_sha"] == "deadbeef1234"


# ═══════════════════════════════════════════════════════════════════════════════
# admin_state.py — GET /api/admin/state
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetSystemState:
    @pytest.mark.asyncio
    async def test_fleet_wide_returns_deploy_fleet_no_version(
        self, client: AsyncClient, db_session
    ):
        """Admin GET /api/admin/state (no project_id) returns deploy+fleet, version=None."""
        from app.models import DeploymentState, DeploymentStatus

        db_session.add(
            DeploymentState(
                id=1,
                status=DeploymentStatus.STABLE,
                current_sha="abc1234",
                current_revision="rev-1",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "deploy" in data
        assert "fleet" in data
        assert "generated_at" in data
        assert data["version"] is None
        assert data["deploy"]["current_sha"] == "abc1234"
        assert isinstance(data["fleet"]["active"], int)
        assert isinstance(data["fleet"]["queued"], int)

    @pytest.mark.asyncio
    async def test_project_scoped_returns_version_block(
        self, client: AsyncClient, db_session
    ):
        """GET /api/admin/state?project_id=<id> returns a version block for open version."""
        from unittest.mock import AsyncMock, patch
        from app.system_state import (
            SystemStateSnapshot,
            DeployStateSnapshot,
            FleetSnapshot,
            VersionSnapshot,
            ServingSnapshot,
        )
        from datetime import datetime, timezone

        project = await _make_project(db_session)

        fake_snap = SystemStateSnapshot(
            deploy=DeployStateSnapshot(
                status="active",
                current_sha="abc",
                current_revision="r1",
                deployed_at=None,
                pause_reason=None,
                rollback_reason=None,
                rollback_count=0,
                last_known_good_sha="",
                halted=False,
            ),
            version=VersionSnapshot(
                version_number=12,
                phase="BUILD",
                status="open",
                opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                phase_changed_at=None,
                goal="EXPERIENCE",
                merged_task_count=5,
                in_flight_task_count=2,
                qualification_state=None,
                serving=ServingSnapshot(
                    status="serving_confirmed",
                    sha="abc",
                    confirmed_at=None,
                    failure_reason=None,
                    artifacts=[],
                ),
            ),
            fleet=FleetSnapshot(
                active=3,
                queued=1,
                evaluating=0,
                parked=0,
                stale_heartbeat_count=0,
            ),
            generated_at="2026-01-01T00:00:00Z",
        )

        with patch(
            "app.routers.admin_state.compute_system_state",
            new_callable=AsyncMock,
            return_value=fake_snap,
        ):
            resp = await client.get(f"/api/admin/state?project_id={project.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] is not None
        assert data["version"]["version_number"] == 12
        assert data["version"]["phase"] == "BUILD"
        assert data["version"]["serving"]["status"] == "serving_confirmed"
        assert data["fleet"]["active"] == 3

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_403(self, override_get_session, db_session):
        """Unauthenticated request returns 403 (no valid user)."""
        from fastapi import HTTPException

        async def no_user():
            raise HTTPException(status_code=403, detail="Not authenticated")

        app.dependency_overrides[get_current_user] = no_user
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/state")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_non_admin_fleet_wide_returns_403(
        self, override_get_session, db_session
    ):
        """Non-admin user requesting fleet-wide state (no project_id) gets 403."""

        async def non_admin():
            return {
                "user_id": "viewer-user",
                "email": "viewer@example.com",
                "role": "user",
            }

        app.dependency_overrides[get_current_user] = non_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/admin/state")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_cache_hit_on_second_call(self, client: AsyncClient, db_session):
        """compute_system_state is only called once when called twice (cache hit)."""
        from unittest.mock import AsyncMock, patch
        from app.system_state import (
            SystemStateSnapshot,
            DeployStateSnapshot,
            FleetSnapshot,
        )

        fake_snap = SystemStateSnapshot(
            deploy=DeployStateSnapshot(
                status="active",
                current_sha="sha123",
                current_revision="rev-1",
                deployed_at=None,
                pause_reason=None,
                rollback_reason=None,
                rollback_count=0,
                last_known_good_sha="",
                halted=False,
            ),
            version=None,
            fleet=FleetSnapshot(
                active=0,
                queued=0,
                evaluating=0,
                parked=0,
                stale_heartbeat_count=0,
            ),
            generated_at="2026-01-01T00:00:00Z",
        )

        with patch(
            "app.routers.admin_state.compute_system_state",
            new_callable=AsyncMock,
            return_value=fake_snap,
        ) as mock_compute:
            r1 = await client.get("/api/admin/state")
            r2 = await client.get("/api/admin/state")

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Should only compute once; second call hits cache
        mock_compute.assert_called_once()

    @pytest.mark.asyncio
    async def test_generated_at_is_iso_string(self, client: AsyncClient, db_session):
        """generated_at field in the response is a string (ISO-formatted datetime)."""
        resp = await client.get("/api/admin/state")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["generated_at"], str)
        # Must parse as a valid datetime
        from datetime import datetime

        parsed = datetime.fromisoformat(data["generated_at"].rstrip("Z"))
        assert parsed is not None


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/admin/release-versions/{version_id}/summary
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetReleaseVersionSummary:
    """Tests for the per-version what's-new summary endpoint."""

    @pytest.mark.asyncio
    async def test_summary_404_on_unknown_version_id(
        self, client: AsyncClient, db_session
    ):
        """Unknown version id returns 404."""
        unknown_id = uuid.uuid4()
        resp = await client.get(f"/api/admin/release-versions/{unknown_id}/summary")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_summary_returns_200_with_required_fields(
        self, client: AsyncClient, db_session
    ):
        """Happy-path open version returns 200 with all required top-level keys."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
            goal="ship the widget",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=0),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in (
            "version_number",
            "phase",
            "status",
            "goal",
            "opened_at",
            "promoted_at",
            "served_at",
            "phase_changed_at",
            "merged_count",
            "acceptance_aggregate",
            "qualification_state",
            "serving_status",
            "served_sha",
            "deploy_failure_reason",
            "aggregate_serving_status",
            "artifacts",
            "serving",
            "highlights",
        ):
            assert key in data, f"missing key: {key}"
        assert data["version_number"] == 1
        assert data["phase"] == "build"
        assert data["status"] == "open"
        assert data["goal"] == "ship the widget"
        assert data["highlights"]["headline"] == "open in build with no merged work yet"

    @pytest.mark.asyncio
    async def test_summary_merged_count_reflects_landed_tasks(
        self, client: AsyncClient, db_session
    ):
        """merged_count reflects tasks that landed since the version opened."""
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Create 2 merged tasks
        for _ in range(2):
            task = Task(
                project_id=project.id,
                title="merged task",
                prompt="do stuff",
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
                updated_at=_utcnow(),
            )
            db_session.add(task)
        await db_session.commit()

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["merged_count"] == 2
        assert data["highlights"]["merged_count"] == 2
        assert "2 merged" in data["highlights"]["headline"]

    @pytest.mark.asyncio
    async def test_summary_promoted_version_returns_200(
        self, client: AsyncClient, db_session
    ):
        """Promoted version (non-open) returns 200 with promoted status fields."""
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        now = _utcnow()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            promoted_at=now,
            promoted_sha="abc1234",
            serving_status="serving_confirmed",
            served_sha="abc1234",
            served_at=now,
            last_qualification={"status": "promoted", "confidence": 0.9},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=5),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "promoted"
        assert data["serving_status"] == "serving_confirmed"
        assert data["promoted_at"] is not None
        assert data["served_at"] is not None
        assert data["highlights"]["headline"] == "promoted & serving"

    @pytest.mark.asyncio
    async def test_summary_abandoned_version_returns_200(
        self, client: AsyncClient, db_session
    ):
        """Abandoned version returns 200 with abandoned headline."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="abandoned",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=0),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "abandoned"
        assert data["highlights"]["headline"] == "abandoned"

    @pytest.mark.asyncio
    async def test_summary_promoted_version_excludes_tasks_merged_after_promotion(
        self, client: AsyncClient, db_session
    ):
        """A promoted version's merged_count / tasks_merged / acceptance_aggregate
        exclude tasks that landed AFTER promoted_at — those belong to the next
        version and must not be double-counted (until=promoted_at upper bound)."""
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        opened_at = _utcnow() - timedelta(hours=3)
        promoted_at = _utcnow() - timedelta(hours=1)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=opened_at,
            promoted_at=promoted_at,
            promoted_sha="abc1234",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Landed inside the version span (before promotion) — counted.
        inside = Task(
            project_id=project.id,
            title="in-span task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            created_at=promoted_at - timedelta(minutes=30),
            updated_at=promoted_at - timedelta(minutes=30),
        )
        # Landed after promotion (belongs to the next version) — excluded.
        after = Task(
            project_id=project.id,
            title="post-promotion task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            created_at=promoted_at + timedelta(minutes=30),
            updated_at=promoted_at + timedelta(minutes=30),
        )
        db_session.add_all([inside, after])
        await db_session.commit()

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["merged_count"] == 1, "post-promotion task must be excluded"
        assert data["acceptance_aggregate"]["total"] == 1

    @pytest.mark.asyncio
    async def test_summary_abandoned_version_excludes_tasks_merged_after_abandonment(
        self, client: AsyncClient, db_session
    ):
        """An abandoned version bounds its window by last_qualification.abandoned_at,
        excluding tasks that landed after the version was abandoned."""
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        opened_at = _utcnow() - timedelta(hours=3)
        abandoned_at = _utcnow() - timedelta(hours=1)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="abandoned",
            opened_at=opened_at,
            last_qualification={"abandoned_at": abandoned_at.isoformat()},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        inside = Task(
            project_id=project.id,
            title="in-span task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            created_at=abandoned_at - timedelta(minutes=30),
            updated_at=abandoned_at - timedelta(minutes=30),
        )
        after = Task(
            project_id=project.id,
            title="post-abandon task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            created_at=abandoned_at + timedelta(minutes=30),
            updated_at=abandoned_at + timedelta(minutes=30),
        )
        db_session.add_all([inside, after])
        await db_session.commit()

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["merged_count"] == 1, "post-abandonment task must be excluded"
        assert data["acceptance_aggregate"]["total"] == 1

    @pytest.mark.asyncio
    async def test_summary_acceptance_aggregate_buckets_present(
        self, client: AsyncClient, db_session
    ):
        """acceptance_aggregate includes the standard acceptance buckets plus total."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=0),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        agg = resp.json()["acceptance_aggregate"]
        assert "total" in agg
        assert isinstance(agg["total"], int)

    @pytest.mark.asyncio
    async def test_summary_qualification_state_included(
        self, client: AsyncClient, db_session
    ):
        """qualification_state is non-null when a qualification verdict exists."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            last_qualification={
                "status": "pending",
                "candidate_sha": "abc1234",
                "confidence": 0.75,
                "threshold": 0.8,
            },
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=3),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["qualification_state"] is not None
        qs = data["qualification_state"]
        assert "state" in qs
        assert data["highlights"]["qualification_confidence"] == 0.75
        assert data["highlights"]["qualification_threshold"] == 0.8

    @pytest.mark.asyncio
    async def test_summary_tasks_merged_included(self, client: AsyncClient, db_session):
        """tasks_merged is a list with task_type, title, pr_number for each merged task."""
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
            title="feat: add changelog surface",
            payloads=[{"type": "feat", "pr_number": 42}],
            status=TaskStatus.COMPLETED,
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(version)
        await db_session.refresh(task)
        mark_landed_on_integration(task)
        db_session.add(task)
        await db_session.commit()

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "tasks_merged" in data
        tasks = data["tasks_merged"]
        assert isinstance(tasks, list)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "feat: add changelog surface"
        assert tasks[0]["task_type"] == "feat"
        assert tasks[0]["pr_number"] == 42

    @pytest.mark.asyncio
    async def test_summary_headline_reflects_aggregate_not_scalar_serving(
        self, client: AsyncClient, db_session
    ):
        """Headline uses aggregate serving status, not scalar version.serving_status.

        A promoted version with scalar serving_status='serving_confirmed' but an
        artifact row reporting 'deploy_failed' must produce headline='promoted'
        (not 'promoted & serving') and a qualification_state consistent with the
        deploy_failed aggregate.
        """
        from app.models import ReleaseDeployArtifact
        from app.release_artifacts import PROMOTION_MERGE_ARTIFACT_KIND
        from app.time_utils import _utcnow

        # Project with GitHub credentials so the promotion_merge artifact provider
        # is observable, making the artifact row participate in the aggregate.
        project = Project(
            name="External Project",
            user_id=TEST_USER_ID,
            github_owner="test-owner",
            github_repo="test-repo",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        member = ProjectMember(
            project_id=project.id, user_id=TEST_USER_ID, role="owner"
        )
        db_session.add(member)
        await db_session.commit()

        now = _utcnow()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            promoted_at=now,
            promoted_sha="abc1234",
            # Scalar says serving_confirmed — the bug would use this for the headline.
            serving_status="serving_confirmed",
            served_sha="abc1234",
            served_at=now,
            last_qualification={"status": "promoted", "confidence": 0.95},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Artifact row for the promotion_merge provider reports deploy_failed —
        # aggregate diverges from the scalar.
        artifact = ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind=PROMOTION_MERGE_ARTIFACT_KIND,
            serving_status="deploy_failed",
            reason_code="merge_not_detected",
        )
        db_session.add(artifact)
        await db_session.commit()

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=3),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Headline must NOT claim serving when the aggregate says deploy_failed.
        assert data["highlights"]["headline"] == "promoted", (
            f"Expected 'promoted' but got {data['highlights']['headline']!r} — "
            "headline should use aggregate serving_status, not the scalar"
        )
        # qualification_state must be consistent with deploy_failed aggregate.
        qs = data["qualification_state"]
        assert qs is not None
        assert qs["state"] == "promotion_deploy_failed", (
            f"Expected 'promotion_deploy_failed' but got {qs['state']!r} — "
            "qualification_state must reflect the deploy_failed aggregate"
        )

    @pytest.mark.asyncio
    async def test_summary_abandoned_version_malformed_abandoned_at_falls_back_unbounded(
        self, client: AsyncClient, db_session
    ):
        """An abandoned version with a malformed abandoned_at ISO string in
        last_qualification degrades gracefully to an unbounded window (returns
        200, not 500).  The except (ValueError, TypeError) branch in
        _closed_version_until must return None rather than raising."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="abandoned",
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={"abandoned_at": "not-a-date"},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_summary_abandoned_version_missing_abandoned_at_falls_back_unbounded(
        self, client: AsyncClient, db_session
    ):
        """An abandoned version with no abandoned_at key in last_qualification
        returns 200 and falls back to an unbounded merged-task window."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="abandoned",
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_summary_promoted_version_null_promoted_at_falls_back_unbounded(
        self, client: AsyncClient, db_session
    ):
        """A promoted version with promoted_at=None degrades to an unbounded
        merged-task window (returns 200, not 500).  _closed_version_until must
        return None rather than raising when promoted_at is absent."""
        project = await _make_project(db_session)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            promoted_at=None,
            promoted_sha="abc1234",
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_release_version_summary_headline_uses_aggregate_serving(
        self, client: AsyncClient, db_session
    ):
        """Headline should use aggregate serving status, not the scalar."""
        from app.models import ReleaseDeployArtifact
        from app.time_utils import _utcnow

        project = await _make_project(db_session)
        now = _utcnow()
        # Create a promoted version with scalar serving_status="unconfirmed"
        # (not "serving_confirmed")
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            promoted_at=now,
            promoted_sha="abc1234",
            serving_status="unconfirmed",  # Scalar is NOT serving_confirmed
            served_sha=None,
            served_at=None,
            last_qualification={"status": "promoted", "confidence": 0.9},
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Create a backend_revision artifact with serving_status="serving_confirmed"
        # to make the aggregate say "serving_confirmed"
        artifact = ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind="backend_revision",
            serving_status="serving_confirmed",
            served_identifier="abc1234",
            served_at=now,
        )
        db_session.add(artifact)
        await db_session.commit()

        with patch(
            "app.release_version.count_merged_since",
            new=AsyncMock(return_value=5),
        ):
            resp = await client.get(f"/api/admin/release-versions/{version.id}/summary")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "promoted"
        assert data["serving_status"] == "unconfirmed"  # The scalar remains unconfirmed
        # But the headline should be "promoted & serving" because the aggregate is
        # serving_confirmed
        assert data["highlights"]["headline"] == "promoted & serving"


# ═══════════════════════════════════════════════════════════════════════════════
# recent_versions highlights + merged count (GET /api/admin/projects/{id}/version-lifecycle)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecentVersionsHighlights:
    """recent_versions entries carry server-computed highlights and merged counts."""

    @pytest.mark.asyncio
    async def test_recent_versions_highlights_match_summary_endpoint(
        self, client: AsyncClient, db_session
    ):
        """recent_versions[].highlights.headline equals the per-version summary
        endpoint headline for the same promoted version, and merged_count /
        merged_task_count are present and correct over the bounded window."""
        from app.time_utils import _utcnow
        from datetime import timedelta

        project = await _make_project(db_session)
        opened = _utcnow() - timedelta(hours=2)
        promoted_at = _utcnow() - timedelta(hours=1)

        promoted = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=opened,
            promoted_at=promoted_at,
            serving_status="serving_confirmed",
            served_sha="abc1234567890",
            last_qualification={"status": "promoted", "confidence": 0.95},
        )
        # A second closed (abandoned) version exercises the multi-row enrichment
        # path and ensures sequential session use doesn't race.
        abandoned = ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="stabilize",
            status="abandoned",
            opened_at=promoted_at,
            promoted_at=None,
        )
        # Open version so the lifecycle endpoint finds a current_version.
        open_ver = ReleaseVersion(
            project_id=project.id,
            version_number=3,
            phase="build",
            status="open",
            opened_at=_utcnow(),
        )
        db_session.add_all([promoted, abandoned, open_ver])
        await db_session.commit()
        await db_session.refresh(promoted)

        # Two tasks landed inside the promoted version's window (opened_at → promoted_at).
        for _ in range(2):
            task = Task(
                project_id=project.id,
                title="landed task",
                prompt="do work",
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
                updated_at=opened + timedelta(minutes=30),
            )
            db_session.add(task)
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
        with readiness, audit:
            lifecycle_resp = await client.get(
                f"/api/admin/projects/{project.id}/version-lifecycle"
            )
        assert lifecycle_resp.status_code == 200, lifecycle_resp.text
        recent = lifecycle_resp.json()["recent_versions"]
        # Both closed versions must appear — this exercises the multi-row enrichment
        # path that previously used asyncio.gather and could race on the session.
        assert len(recent) >= 2, f"expected >=2 closed versions, got {len(recent)}"
        row = next(r for r in recent if r["version_number"] == 1)

        # Highlights and merged counts must be present.
        assert row["highlights"] is not None, "highlights must be populated"
        assert "headline" in row["highlights"]
        assert row["merged_count"] is not None
        assert row["merged_task_count"] is not None
        assert row["merged_count"] == row["merged_task_count"]

        # Fetch the per-version summary for the same closed version.
        summary_resp = await client.get(
            f"/api/admin/release-versions/{promoted.id}/summary"
        )
        assert summary_resp.status_code == 200, summary_resp.text
        summary = summary_resp.json()

        # The headline must match byte-for-byte between recent_versions and summary.
        assert row["highlights"]["headline"] == summary["highlights"]["headline"], (
            f"lifecycle headline {row['highlights']['headline']!r} != "
            f"summary headline {summary['highlights']['headline']!r}"
        )
        # Both merged counts must equal the summary endpoint's merged_count.
        assert row["merged_count"] == summary["merged_count"]
        assert row["merged_task_count"] == summary["merged_count"]

    @pytest.mark.asyncio
    async def test_recent_versions_highlights_null_gracefully_without_session(
        self, client: AsyncClient, db_session
    ):
        """recent_versions[] entries include highlights=null when the enrichment
        path is skipped (no session/project_id), meaning older callers stay compatible.
        Verified via unit-call of _serialize_version_history_entry without session."""
        from datetime import datetime
        from unittest.mock import MagicMock
        from app.routers.admin_release_version import _serialize_version_history_entry

        v = MagicMock(spec=ReleaseVersion)
        v.id = uuid.uuid4()
        v.version_number = 1
        v.status = "promoted"
        v.phase = "stabilize"
        v.opened_at = datetime(2026, 1, 1)
        v.promoted_at = datetime(2026, 6, 1)
        v.qualification_attempts = 0
        v.last_qualification = None
        v.serving_status = None
        v.served_sha = None
        v.served_at = None
        v.deploy_failure_reason = None
        v.deploy_confirm_deadline = None
        v.deploy_artifacts = []
        v.out_of_band_promotion = None

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

        # Without session/project_id the enrichment fields are null — safe degradation.
        assert result["highlights"] is None
        assert result["merged_count"] is None
        assert result["merged_task_count"] is None
        assert result["acceptance_aggregate"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# admin_agent_ops.py — Operator declare capability trial verdict
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminDeclareCapabilityVerdict:
    """Tests for POST /api/admin/projects/{project_id}/capability-trials/declare."""

    ENDPOINT = "/api/admin/projects/{project_id}/capability-trials/declare"

    def _url(self, project_id):
        return self.ENDPOINT.format(project_id=project_id)

    @pytest.mark.asyncio
    async def test_adopt_verdict_adopted(self, client: AsyncClient, db_session):
        """Adopt verdict is persisted with evidence_source='operator'."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "adopt", "rationale": None},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["cap_name"] == "repo_map"
        assert data["verdict"] == "adopt"

        await db_session.refresh(project)
        stored = (project.capability_adoption_verdicts or {}).get("repo_map", {})
        assert stored["verdict"] == "adopt"
        assert stored["evidence_source"] == "operator"

    @pytest.mark.asyncio
    async def test_admin_declare_capability_verdict_adopt(
        self, client: AsyncClient, db_session
    ):
        """Adopt verdict round-trips correctly: persisted with evidence_source='operator'."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "adopt"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["verdict"] == "adopt"

        await db_session.refresh(project)
        entry = (project.capability_adoption_verdicts or {})["repo_map"]
        assert entry["verdict"] == "adopt"
        assert entry["evidence_source"] == "operator"

    @pytest.mark.asyncio
    async def test_reject_verdict_persisted(self, client: AsyncClient, db_session):
        """Reject verdict is stored with evidence_source='operator'."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "related_tests", "verdict": "reject"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["verdict"] == "reject"

        await db_session.refresh(project)
        entry = (project.capability_adoption_verdicts or {})["related_tests"]
        assert entry["verdict"] == "reject"
        assert entry["evidence_source"] == "operator"

    @pytest.mark.asyncio
    async def test_continue_trial_overwrites_decided_verdict(
        self, client: AsyncClient, db_session
    ):
        """Operator continue-trial can overwrite a prior decided verdict (bypasses self-revert guard)."""
        project = await _make_project(db_session)

        # Pre-seed a decided verdict
        project.capability_adoption_verdicts = {
            "repo_map": {"verdict": "adopt", "evidence_source": "automated"}
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "continue-trial"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        entry = (project.capability_adoption_verdicts or {})["repo_map"]
        assert entry["verdict"] == "continue-trial"
        assert entry["evidence_source"] == "operator"

    @pytest.mark.asyncio
    async def test_rationale_stored(self, client: AsyncClient, db_session):
        """Non-empty rationale is stored in the persisted entry."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={
                "cap_name": "repo_map",
                "verdict": "reject",
                "rationale": "Not enough lift observed",
            },
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        entry = (project.capability_adoption_verdicts or {})["repo_map"]
        assert entry.get("rationale") == "Not enough lift observed"

    @pytest.mark.asyncio
    async def test_empty_cap_name_rejected(self, client: AsyncClient, db_session):
        """Empty cap_name returns 400."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "", "verdict": "adopt"},
        )
        assert resp.status_code == 400, resp.text

    @pytest.mark.asyncio
    async def test_unknown_verdict_rejected(self, client: AsyncClient, db_session):
        """An invalid verdict value returns 422 (Pydantic validation)."""
        project = await _make_project(db_session)

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "bogus"},
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_project_not_found(self, client: AsyncClient, db_session):
        """Unknown project_id returns 404."""
        missing_id = uuid.uuid4()

        resp = await client.post(
            self._url(missing_id),
            json={"cap_name": "repo_map", "verdict": "adopt"},
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, override_get_session, db_session):
        """Non-admin user is rejected with 403."""
        from fastapi import HTTPException

        project = await _make_project(db_session)

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as ac:
                resp = await ac.post(
                    self._url(project.id),
                    json={"cap_name": "repo_map", "verdict": "adopt"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_adopt_verdict_closes_stall_notice(
        self, client: AsyncClient, db_session
    ):
        """Adopt verdict resolves an open stall notice for the same cap_name."""
        project = await _make_project(db_session)

        # Seed an open stall notice with the matching dedupe_key
        stall_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="[CAPABILITY-TRIAL] repo_map stalled",
            status="open",
            context={
                "event_type": "capability_trial_stalled",
                "dedupe_key": "capability_trial_stalled_repo_map",
                "cap_name": "repo_map",
            },
        )
        db_session.add(stall_msg)
        await db_session.commit()

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "adopt"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(stall_msg)
        assert stall_msg.status == "resolved"
        assert stall_msg.resolved_at is not None

    @pytest.mark.asyncio
    async def test_reject_verdict_closes_stall_notice(
        self, client: AsyncClient, db_session
    ):
        """Reject verdict resolves an open stall notice for the same cap_name."""
        project = await _make_project(db_session)

        stall_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="[CAPABILITY-TRIAL] repo_map stalled",
            status="open",
            context={
                "event_type": "capability_trial_stalled",
                "dedupe_key": "capability_trial_stalled_repo_map",
                "cap_name": "repo_map",
            },
        )
        db_session.add(stall_msg)
        await db_session.commit()

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "reject"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(stall_msg)
        assert stall_msg.status == "resolved"
        assert stall_msg.resolved_at is not None

    @pytest.mark.asyncio
    async def test_continue_trial_leaves_stall_notice_open(
        self, client: AsyncClient, db_session
    ):
        """continue-trial verdict leaves an existing stall notice open."""
        project = await _make_project(db_session)

        stall_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="[CAPABILITY-TRIAL] repo_map stalled",
            status="open",
            context={
                "event_type": "capability_trial_stalled",
                "dedupe_key": "capability_trial_stalled_repo_map",
                "cap_name": "repo_map",
            },
        )
        db_session.add(stall_msg)
        await db_session.commit()

        resp = await client.post(
            self._url(project.id),
            json={"cap_name": "repo_map", "verdict": "continue-trial"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(stall_msg)
        assert stall_msg.status == "open"
        assert stall_msg.resolved_at is None


class TestProjectConcurrencyCapSettings:
    """PATCH /api/admin/settings round-trip and validation tests for
    max_concurrent_tasks_by_project (per-project concurrent-VM cap)."""

    @pytest.mark.asyncio
    async def test_patch_sets_and_get_returns_cap(self, client: AsyncClient):
        """PATCH a valid mapping and GET confirms the round-trip."""
        from app.shared_state import runtime_config as _rc

        original = dict(_rc.get("max_concurrent_tasks_by_project") or {})
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": {"proj-1": 3}},
            )
            assert resp.status_code == 200, resp.text
            get_resp = await client.get("/api/admin/settings")
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert "max_concurrent_tasks_by_project" in data
            assert data["max_concurrent_tasks_by_project"].get("proj-1") == 3
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": original},
            )

    @pytest.mark.asyncio
    async def test_patch_empty_map_clears_all_caps(self, client: AsyncClient):
        """Patching with an empty dict removes all per-project caps."""
        from app.shared_state import runtime_config as _rc

        original = dict(_rc.get("max_concurrent_tasks_by_project") or {})
        try:
            # First set a cap.
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": {"proj-a": 5}},
            )
            # Now clear it.
            resp = await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": {}},
            )
            assert resp.status_code == 200, resp.text
            get_resp = await client.get("/api/admin/settings")
            assert get_resp.json()["max_concurrent_tasks_by_project"] == {}
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": original},
            )

    @pytest.mark.asyncio
    async def test_patch_zero_value_removes_project_cap(self, client: AsyncClient):
        """A zero cap for a project silently removes it (uncapped behavior)."""
        from app.shared_state import runtime_config as _rc

        original = dict(_rc.get("max_concurrent_tasks_by_project") or {})
        try:
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": {"proj-x": 4}},
            )
            resp = await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": {"proj-x": 0}},
            )
            assert resp.status_code == 200, resp.text
            get_resp = await client.get("/api/admin/settings")
            caps = get_resp.json()["max_concurrent_tasks_by_project"]
            assert "proj-x" not in caps
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": original},
            )

    @pytest.mark.asyncio
    async def test_patch_non_dict_rejected(self, client: AsyncClient):
        """A non-mapping value is rejected with 400."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"max_concurrent_tasks_by_project": 5},
        )
        assert resp.status_code == 400
        assert "mapping" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_patch_non_integer_value_rejected(self, client: AsyncClient):
        """A non-integer cap value is rejected with 400."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"max_concurrent_tasks_by_project": {"proj-1": "many"}},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_out_of_range_value_rejected(self, client: AsyncClient):
        """A cap value outside 0..50 is rejected with 400."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"max_concurrent_tasks_by_project": {"proj-1": 51}},
        )
        assert resp.status_code == 400

        resp = await client.patch(
            "/api/admin/settings",
            json={"max_concurrent_tasks_by_project": {"proj-1": -1}},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_multiple_projects(self, client: AsyncClient):
        """Multiple project caps can be set in a single PATCH."""
        from app.shared_state import runtime_config as _rc

        original = dict(_rc.get("max_concurrent_tasks_by_project") or {})
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={
                    "max_concurrent_tasks_by_project": {
                        "project-a": 2,
                        "project-b": 10,
                    }
                },
            )
            assert resp.status_code == 200, resp.text
            data = (await client.get("/api/admin/settings")).json()
            caps = data["max_concurrent_tasks_by_project"]
            assert caps["project-a"] == 2
            assert caps["project-b"] == 10
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"max_concurrent_tasks_by_project": original},
            )


class TestCloudRunAllowlistPersistence:
    """PATCH /api/admin/settings round-trip and persistence tests for
    cloud_run_task_type_allowlist."""

    @pytest.mark.asyncio
    async def test_cloud_run_allowlist_patch_and_get_roundtrip(
        self, client: AsyncClient
    ):
        """PATCH cloud_run_task_type_allowlist and GET confirms the round-trip."""
        from app.shared_state import runtime_config as _rc

        original = list(_rc.get("cloud_run_task_type_allowlist") or [])
        try:
            # Patch with a list of Cloud Run eligible task types.
            resp = await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": ["research", "web_research"]},
            )
            assert resp.status_code == 200, resp.text

            # GET should return the updated allowlist.
            get_resp = await client.get("/api/admin/settings")
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert "cloud_run_task_type_allowlist" in data
            assert sorted(data["cloud_run_task_type_allowlist"]) == [
                "research",
                "web_research",
            ]
        finally:
            # Restore original.
            await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": original},
            )

    @pytest.mark.asyncio
    async def test_cloud_run_allowlist_persists_across_sync(
        self, client: AsyncClient, db_session
    ):
        """Verify cloud_run_task_type_allowlist is persisted and restored by sync."""
        from app.shared_state import runtime_config as _rc
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state

        original = list(_rc.get("cloud_run_task_type_allowlist") or [])
        try:
            # Patch the allowlist.
            resp = await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": ["research", "memo"]},
            )
            assert resp.status_code == 200

            # Verify it's in memory.
            assert sorted(_rc["cloud_run_task_type_allowlist"]) == ["memo", "research"]

            # Refresh the session and read the persisted state from the database.
            await db_session.commit()
            state = await db_session.get(DeploymentState, 1)
            # DeploymentState may not exist in test DB; this is fine - we've
            # verified the in-memory value is correct via the response above.
            if state is not None and state.runtime_config_json is not None:
                # Verify the value is persisted.
                assert "cloud_run_task_type_allowlist" in state.runtime_config_json
                persisted = state.runtime_config_json["cloud_run_task_type_allowlist"]
                assert sorted(persisted) == ["memo", "research"]

                # Simulate a restart: clear the in-memory value and sync from DB.
                _rc["cloud_run_task_type_allowlist"] = []
                changed = await sync_runtime_config_from_state(state)

                # The allowlist should be restored from the database.
                assert "cloud_run_task_type_allowlist" in changed
                assert sorted(_rc["cloud_run_task_type_allowlist"]) == [
                    "memo",
                    "research",
                ]
        finally:
            # Restore original.
            await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": original},
            )

    @pytest.mark.asyncio
    async def test_cloud_run_allowlist_empty_list(self, client: AsyncClient):
        """An empty allowlist disables Cloud Run routing."""
        from app.shared_state import runtime_config as _rc

        original = list(_rc.get("cloud_run_task_type_allowlist") or [])
        try:
            # Set to empty list.
            resp = await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": []},
            )
            assert resp.status_code == 200

            get_resp = await client.get("/api/admin/settings")
            assert get_resp.json()["cloud_run_task_type_allowlist"] == []
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": original},
            )

    @pytest.mark.asyncio
    async def test_cloud_run_allowlist_invalid_task_type_rejected(
        self, client: AsyncClient
    ):
        """Invalid or write-only task types are rejected."""
        # Try to add a write-only task type (create is not in READ_ONLY_TASK_TYPES).
        resp = await client.patch(
            "/api/admin/settings",
            json={"cloud_run_task_type_allowlist": ["create", "research"]},
        )
        assert resp.status_code == 400
        assert "write/unknown task types" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_cloud_run_allowlist_comma_separated_string(
        self, client: AsyncClient
    ):
        """Allowlist accepts comma-separated string format."""
        from app.shared_state import runtime_config as _rc

        original = list(_rc.get("cloud_run_task_type_allowlist") or [])
        try:
            # Patch with comma-separated string.
            resp = await client.patch(
                "/api/admin/settings",
                json={
                    "cloud_run_task_type_allowlist": "research,web_research"
                },
            )
            assert resp.status_code == 200

            get_resp = await client.get("/api/admin/settings")
            result = sorted(get_resp.json()["cloud_run_task_type_allowlist"])
            assert result == ["research", "web_research"]
        finally:
            await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": original},
            )


class TestPatchRunnerHealthThresholds:
    """PATCH /api/admin/settings round-trip and validation for runner health thresholds.

    These knobs control liveness detection, degradation sensitivity, resource-pressure
    floors, escalation cadence, and data-retention windows for runners and task VMs.
    """

    @pytest.mark.asyncio
    async def test_defaults_appear_in_get(self, client: AsyncClient):
        """GET /api/admin/settings returns the documented defaults for all 11 keys."""
        body = (await client.get("/api/admin/settings")).json()
        assert body["runner_liveness_seconds"] == 300
        assert body["runner_degraded_failure_threshold"] == 3
        assert body["runner_low_disk_threshold_bytes"] == 1_073_741_824
        assert body["runner_low_memory_threshold_bytes"] == 536_870_912
        assert body["runner_escalation_age_seconds"] == 14_400
        assert body["runner_idle_gpu_threshold_pct"] == 10
        assert body["runner_token_stale_days"] == 90
        assert body["runner_post_update_silence_seconds"] == 600
        assert body["runner_heartbeat_retention_days"] == 7
        assert body["vm_low_disk_threshold_bytes"] == 1_073_741_824
        assert body["vm_low_memory_threshold_bytes"] == 536_870_912

    @pytest.mark.asyncio
    async def test_runner_liveness_seconds_round_trip(self, client: AsyncClient):
        """PATCH runner_liveness_seconds=120 -> GET confirms it; out-of-range rejected."""
        resp = await client.patch(
            "/api/admin/settings", json={"runner_liveness_seconds": 120}
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_liveness_seconds"
            ]
            == 120
        )
        # Below minimum (30).
        bad = await client.patch(
            "/api/admin/settings", json={"runner_liveness_seconds": 10}
        )
        assert bad.status_code == 400
        # Above maximum (3600).
        bad = await client.patch(
            "/api/admin/settings", json={"runner_liveness_seconds": 99999}
        )
        assert bad.status_code == 400

    @pytest.mark.asyncio
    async def test_runner_degraded_failure_threshold_round_trip(
        self, client: AsyncClient
    ):
        """PATCH runner_degraded_failure_threshold=5 -> GET confirms; out-of-range rejected."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"runner_degraded_failure_threshold": 5},
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_degraded_failure_threshold"
            ]
            == 5
        )
        for bad_val in (0, 99):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_degraded_failure_threshold": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_resource_threshold_bytes_round_trip(self, client: AsyncClient):
        """PATCH low-disk, low-memory, and VM resource thresholds round-trip."""
        keys_thresholds = {
            "runner_low_disk_threshold_bytes": 2_147_483_648,  # 2 GiB
            "runner_low_memory_threshold_bytes": 268_435_456,  # 256 MiB
            "vm_low_disk_threshold_bytes": 5_368_709_120,  # 5 GiB
            "vm_low_memory_threshold_bytes": 134_217_728,  # 128 MiB
        }
        for key, val in keys_thresholds.items():
            resp = await client.patch(
                "/api/admin/settings", json={key: val}
            )
            assert resp.status_code == 200, f"{key} PATCH failed: {resp.text}"
            assert (
                (await client.get("/api/admin/settings")).json()[key] == val
            ), f"{key} GET did not match after PATCH"
            # Below minimum (1 MiB).
            bad = await client.patch(
                "/api/admin/settings", json={key: 42}
            )
            assert bad.status_code == 400, (
                f"{key}=42 should be below minimum"
            )

    @pytest.mark.asyncio
    async def test_runner_escalation_age_seconds_round_trip(
        self, client: AsyncClient
    ):
        """PATCH runner_escalation_age_seconds=7200 -> GET confirms; out-of-range rejected."""
        resp = await client.patch(
            "/api/admin/settings", json={"runner_escalation_age_seconds": 7200}
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_escalation_age_seconds"
            ]
            == 7200
        )
        for bad_val in (30, 999_999):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_escalation_age_seconds": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_runner_idle_gpu_threshold_pct_round_trip(
        self, client: AsyncClient
    ):
        """PATCH runner_idle_gpu_threshold_pct=25 -> GET confirms."""
        resp = await client.patch(
            "/api/admin/settings", json={"runner_idle_gpu_threshold_pct": 25}
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_idle_gpu_threshold_pct"
            ]
            == 25
        )
        for bad_val in (-1, 101):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_idle_gpu_threshold_pct": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_runner_token_stale_days_round_trip(self, client: AsyncClient):
        """PATCH runner_token_stale_days=180 -> GET confirms; out-of-range rejected."""
        resp = await client.patch(
            "/api/admin/settings", json={"runner_token_stale_days": 180}
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_token_stale_days"
            ]
            == 180
        )
        for bad_val in (0, 366):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_token_stale_days": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_runner_post_update_silence_seconds_round_trip(
        self, client: AsyncClient
    ):
        """PATCH runner_post_update_silence_seconds=300 -> GET confirms."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"runner_post_update_silence_seconds": 300},
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_post_update_silence_seconds"
            ]
            == 300
        )
        for bad_val in (30, 9999):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_post_update_silence_seconds": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_runner_heartbeat_retention_days_round_trip(
        self, client: AsyncClient
    ):
        """PATCH runner_heartbeat_retention_days=14 -> GET confirms; out-of-range rejected."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"runner_heartbeat_retention_days": 14},
        )
        assert resp.status_code == 200, resp.text
        assert (
            (await client.get("/api/admin/settings")).json()[
                "runner_heartbeat_retention_days"
            ]
            == 14
        )
        for bad_val in (0, 366):
            bad = await client.patch(
                "/api/admin/settings",
                json={"runner_heartbeat_retention_days": bad_val},
            )
            assert bad.status_code == 400, f"{bad_val=} should be rejected"

    @pytest.mark.asyncio
    async def test_non_number_rejected(self, client: AsyncClient):
        """Non-numeric values for any runner health key are rejected with 400."""
        for key in (
            "runner_liveness_seconds",
            "runner_degraded_failure_threshold",
            "runner_low_disk_threshold_bytes",
            "runner_low_memory_threshold_bytes",
            "runner_escalation_age_seconds",
            "runner_idle_gpu_threshold_pct",
            "runner_token_stale_days",
            "runner_post_update_silence_seconds",
            "runner_heartbeat_retention_days",
            "vm_low_disk_threshold_bytes",
            "vm_low_memory_threshold_bytes",
        ):
            bad = await client.patch(
                "/api/admin/settings", json={key: "not-a-number"}
            )
            assert bad.status_code == 400, f"{key} should reject non-numeric"

    @pytest.mark.asyncio
    async def test_runner_list_reflects_configured_thresholds(
        self, client: AsyncClient, db_session
    ):
        """Runner list endpoint returns liveness_window_seconds and failure_streak_threshold
        driven by the runtime-configurable values, not hardcoded constants."""
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="a" * 64,
            hostname="threshold-test-runner",
            status="active",
            last_seen_at=_utcnow(),
        )
        db_session.add(runner)
        await db_session.commit()

        # Patch to non-default values.
        resp = await client.patch(
            "/api/admin/settings",
            json={"runner_liveness_seconds": 120, "runner_degraded_failure_threshold": 7},
        )
        assert resp.status_code == 200, resp.text

        listed = (
            await client.get(f"/api/admin/projects/{project.id}/runners")
        ).json()
        assert len(listed) == 1
        health = listed[0]["health"]
        assert health["liveness_window_seconds"] == 120, (
            "runner health must reflect configured liveness_seconds, not the hardcoded default"
        )
        assert health["failure_streak_threshold"] == 7, (
            "runner health must reflect configured degraded_failure_threshold, not the hardcoded default"
        )


class TestTaskLlmCostFields:
    """Unit tests for _task_llm_cost_fields helper in admin.py."""

    def _make_task(self, provider=None, model=None, input_tokens=0, output_tokens=0):
        t = Task(id=str(uuid.uuid4()), status=TaskStatus.COMPLETED)
        t.provider = provider
        t.model = model
        t.api_call_summary = {
            "llm_call_count": 1,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
        }
        t.attempt_history = None
        t.llm_call_count_banked_db = None
        return t

    def test_returns_none_when_no_tokens(self):
        from app.admin import _task_llm_cost_fields

        task = self._make_task()
        result = _task_llm_cost_fields(task)
        assert result["task_llm_cost_usd"] is None
        assert result["task_llm_cost_any_unpriced"] is False

    def test_returns_cost_for_priced_model(self):
        from app.admin import _task_llm_cost_fields
        from app.efficiency_snapshot import LLM_TOKEN_PRICING

        input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
        task = self._make_task(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        result = _task_llm_cost_fields(task)
        expected = round(1_000_000 * input_rate + 1_000_000 * output_rate, 6)
        assert result["task_llm_cost_usd"] == expected
        assert result["task_llm_cost_any_unpriced"] is False

    def test_any_unpriced_when_provider_missing(self):
        from app.admin import _task_llm_cost_fields

        task = self._make_task(
            provider=None,
            model=None,
            input_tokens=5000,
            output_tokens=2000,
        )
        result = _task_llm_cost_fields(task)
        assert result["task_llm_cost_usd"] is None
        assert result["task_llm_cost_any_unpriced"] is True


class TestPostCommitPendingField:
    """post_commit_pending is serialized in both task list and task detail responses."""

    @pytest.mark.asyncio
    async def test_task_list_exposes_post_commit_pending_true(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            post_commit_pending=True,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get("/api/admin/tasks")
        assert resp.status_code == 200, resp.text
        tasks = resp.json()["tasks"]
        matched = [t for t in tasks if t["id"] == str(task.id)]
        assert matched, "task not found in list response"
        assert matched[0]["post_commit_pending"] is True

    @pytest.mark.asyncio
    async def test_task_list_exposes_post_commit_pending_false(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            post_commit_pending=False,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get("/api/admin/tasks")
        assert resp.status_code == 200, resp.text
        tasks = resp.json()["tasks"]
        matched = [t for t in tasks if t["id"] == str(task.id)]
        assert matched, "task not found in list response"
        assert matched[0]["post_commit_pending"] is False

    @pytest.mark.asyncio
    async def test_task_detail_exposes_post_commit_pending_true(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            post_commit_pending=True,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["post_commit_pending"] is True

    @pytest.mark.asyncio
    async def test_task_detail_exposes_post_commit_pending_false(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            post_commit_pending=False,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["post_commit_pending"] is False

    @pytest.mark.asyncio
    async def test_task_detail_includes_bounded_sideboard_context(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="Workbench Project")
        project.github_owner = "HenryHamster"
        project.github_repo = "BlueprintAIStudio"
        project.directions = [
            {
                "id": "d-active",
                "text": "Keep active work tied to the operator sideboard.",
                "priority": "high",
                "status": "active",
            }
        ]
        project.problem_model = {
            "intent": {
                "current_focus": "Task sideboard integration",
                "purpose": "Keep active work understandable.",
            },
            "huge": "x" * 5000,
        }
        db_session.add(project)
        task = Task(
            project_id=project.id,
            title="Integrate task sideboard",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "direction_id": "d-active"}],
            context={"direction_id": "d-active", "design_doc": {"huge": "y" * 5000}},
            blocked_reason="Waiting for reviewer feedback.",
        )
        parent = Task(
            project_id=project.id,
            title="Parent task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)
        task.parent_task_id = parent.id
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        child = Task(
            project_id=project.id,
            title="Child task",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "fix"}],
            parent_task_id=task.id,
        )
        artifact_pr = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/42",
            commit_hash="abc123",
            repo_url="https://github.com/HenryHamster/BlueprintAIStudio",
        )
        cycle = StrategicCycle(
            id="sc-sideboard",
            project_id=project.id,
            outcome="phase2_running",
            phase2_task_id=task.id,
        )
        message = ProjectMessage(
            project_id=project.id,
            from_task_id=task.id,
            from_type="agent",
            message_type=MessageType.NOTE,
            content="Task update " + ("extra " * 200),
            context={"event_type": "task_update", "huge": "z" * 5000},
        )
        db_session.add_all([child, artifact_pr, cycle, message])
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        sideboard = resp.json()["sideboard_context"]

        assert sideboard["project"] == {
            "id": str(project.id),
            "name": "Workbench Project",
            "repo": "HenryHamster/BlueprintAIStudio",
        }
        assert sideboard["purpose"] == "Task sideboard integration"
        assert sideboard["strategic"]["direction"]["id"] == "d-active"
        assert sideboard["strategic"]["direction"]["text"].startswith("Keep active work")
        assert sideboard["strategic"]["cycle"] == {
            "id": "sc-sideboard",
            "phase": "strategic_review",
            "outcome": "phase2_running",
        }
        assert sideboard["pipeline"]["parent_task_id"] == str(parent.id)
        assert sideboard["pipeline"]["child_count"] == 1
        assert sideboard["pipeline"]["dependency_count"] == 0
        assert sideboard["pipeline"]["blocked_reason"] == "Waiting for reviewer feedback."
        assert sideboard["pipeline"]["movement_explanation"] == "Waiting for reviewer feedback."
        assert resp.json()["movement_explanation"] == "Waiting for reviewer feedback."
        assert sideboard["task"]["id"] == str(task.id)
        assert sideboard["task"]["title"] == "Integrate task sideboard"
        assert sideboard["task"]["status"] == "IN_PROGRESS"
        assert sideboard["task"]["task_type"] == "create"
        assert sideboard["task"]["blocker"]["reason"] == "Waiting for reviewer feedback."
        assert sideboard["task"]["runner"]["vm_instance_id"] is None
        assert sideboard["task"]["cost"]["estimated_vm_usd"] is None
        assert sideboard["task"]["cost"]["llm_usd"] is None
        assert sideboard["task"]["next_action"]["label"] == "Monitor progress"
        assert sideboard["artifacts"]["total"] == 1
        assert sideboard["artifacts"]["by_type"] == {
            "PULL_REQUEST": 1,
        }
        assert sideboard["artifacts"]["primary_pr_url"].endswith("/pull/42")
        assert sideboard["artifacts"]["primary_commit"] == "abc123"
        assert sideboard["messages"]["total_recent"] == 1
        assert len(sideboard["messages"]["items"][0]["content"]) <= 280
        assert sideboard["messages"]["items"][0]["context"] == {"event_type": "task_update"}
        assert {link["id"] for link in sideboard["links"]} >= {
            "pipeline",
            "parent_task",
            "direction",
            "pull_request",
        }
        links_by_id = {link["id"]: link for link in sideboard["links"]}
        assert links_by_id["pipeline"]["target"]["id"] == str(task.id)
        assert links_by_id["pipeline"]["target"]["task_id"] == str(task.id)
        assert links_by_id["parent_task"]["target"]["id"] == str(parent.id)
        assert links_by_id["parent_task"]["target"]["task_id"] == str(parent.id)
        assert links_by_id["direction"]["target"]["id"] == "d-active"
        assert links_by_id["direction"]["target"]["direction_id"] == "d-active"
        assert links_by_id["pull_request"]["target"]["id"].endswith("/pull/42")
        assert links_by_id["pull_request"]["target"]["url"].endswith("/pull/42")
        assert "problem_model" not in sideboard
        assert "design_doc" not in str(sideboard)
        assert "huge" not in str(sideboard)

    @pytest.mark.asyncio
    async def test_task_detail_sideboard_messages_include_context_linked_messages(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session, name="Other Project")
        task = Task(
            project_id=project.id,
            title="Task with linked messages",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
        )
        other_task = Task(
            project_id=project.id,
            title="Other task",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
        )
        db_session.add_all([task, other_task])
        await db_session.commit()
        await db_session.refresh(task)
        await db_session.refresh(other_task)
        base_created_at = datetime(2026, 1, 1, 12, 0, 0)
        messages = [
            ProjectMessage(
                project_id=project.id,
                from_task_id=other_task.id,
                from_type="agent",
                message_type=MessageType.NOTE,
                content="unrelated newer task",
                created_at=base_created_at + timedelta(minutes=6),
            ),
            ProjectMessage(
                project_id=project.id,
                from_type="system",
                message_type=MessageType.BLOCKER,
                content="convergence link",
                context={"_convergence_link": {"kind": "task", "id": str(task.id)}},
                created_at=base_created_at + timedelta(minutes=4),
            ),
            ProjectMessage(
                project_id=project.id,
                from_type="agent",
                message_type=MessageType.NOTE,
                content="source task context",
                context={"source_task_id": str(task.id)},
                created_at=base_created_at + timedelta(minutes=3),
            ),
            ProjectMessage(
                project_id=project.id,
                from_type="agent",
                message_type=MessageType.NOTE,
                content="task context",
                context={"task_id": str(task.id)},
                created_at=base_created_at + timedelta(minutes=2),
            ),
            ProjectMessage(
                project_id=project.id,
                from_task_id=task.id,
                from_type="agent",
                message_type=MessageType.NOTE,
                content="direct from task",
                created_at=base_created_at + timedelta(minutes=1),
            ),
            ProjectMessage(
                project_id=other_project.id,
                from_type="agent",
                message_type=MessageType.NOTE,
                content="other project link",
                context={"task_id": str(task.id)},
                created_at=base_created_at + timedelta(minutes=7),
            ),
        ]
        db_session.add_all(messages)
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text

        sideboard_messages = resp.json()["sideboard_context"]["messages"]
        assert sideboard_messages["total_recent"] == 4
        assert [item["content"] for item in sideboard_messages["items"]] == [
            "convergence link",
            "source task context",
            "task context",
            "direct from task",
        ]

    @pytest.mark.asyncio
    async def test_task_detail_sideboard_context_handles_plain_task(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Plain task",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        sideboard = resp.json()["sideboard_context"]

        assert sideboard["project"]["id"] == str(project.id)
        assert sideboard["project"]["name"] == project.name
        assert sideboard["project"]["repo"] is None
        assert sideboard["purpose"] is None
        assert sideboard["strategic"]["direction"] is None
        assert sideboard["strategic"]["cycle"] is None
        assert sideboard["pipeline"]["parent_task_id"] is None
        assert sideboard["pipeline"]["child_count"] == 0
        assert sideboard["pipeline"]["dependency_count"] == 0
        assert sideboard["artifacts"]["total"] == 0
        assert sideboard["messages"] == {"total_recent": 0, "items": []}

    @pytest.mark.asyncio
    async def test_task_detail_handles_malformed_first_payload(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="Malformed payload task",
            status=TaskStatus.QUEUED,
            payloads=["not-a-payload-object"],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["task_type"] == "create"
        assert body["sideboard_context"]["task"]["task_type"] == "create"

    @pytest.mark.asyncio
    async def test_task_detail_sideboard_child_count_stays_exact_when_child_ids_truncated(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="High fanout parent",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        db_session.add_all(
            [
                Task(
                    project_id=project.id,
                    title=f"Child {idx}",
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix"}],
                    parent_task_id=task.id,
                )
                for idx in range(1002)
            ]
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert len(body["child_task_ids"]) == 1000
        assert body["child_task_ids_truncated"] is True
        assert body["sideboard_context"]["pipeline"]["child_count"] == 1002

    @pytest.mark.asyncio
    async def test_task_detail_sideboard_cycle_fallback_is_project_scoped(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="Primary Project")
        other_project = await _make_project(db_session, name="Other Project")
        task = Task(
            project_id=project.id,
            title="Task with stale cycle pointer",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
            strategic_cycle_id="sc-other-project",
        )
        other_cycle = StrategicCycle(
            id="sc-other-project",
            project_id=other_project.id,
            outcome="completed",
        )
        db_session.add_all([task, other_cycle])
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text

        assert resp.json()["sideboard_context"]["strategic"]["cycle"] is None


class TestApprovalNeededFilter:
    """GET /api/admin/tasks supports the approval_needed triage filter."""

    @pytest.mark.asyncio
    async def test_admin_tasks_approval_needed_filter(
        self, client: AsyncClient, db_session
    ):
        """approval_needed narrows the list in both directions; omitting it
        returns both tasks (backward-compatible default)."""
        project = await _make_project(db_session)
        needs_approval = Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
            requires_approval=True,
        )
        no_approval = Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create"}],
            requires_approval=False,
        )
        db_session.add_all([needs_approval, no_approval])
        await db_session.commit()
        await db_session.refresh(needs_approval)
        await db_session.refresh(no_approval)

        needs_id = str(needs_approval.id)
        no_id = str(no_approval.id)

        # Default (param omitted) — both tasks are returned unchanged.
        resp = await client.get("/api/admin/tasks")
        assert resp.status_code == 200, resp.text
        all_ids = {t["id"] for t in resp.json()["tasks"]}
        assert needs_id in all_ids
        assert no_id in all_ids

        # approval_needed=true — only the requires_approval task.
        resp = await client.get("/api/admin/tasks?approval_needed=true")
        assert resp.status_code == 200, resp.text
        true_ids = {t["id"] for t in resp.json()["tasks"]}
        assert needs_id in true_ids
        assert no_id not in true_ids

        # approval_needed=false — only the non-approval task.
        resp = await client.get("/api/admin/tasks?approval_needed=false")
        assert resp.status_code == 200, resp.text
        false_ids = {t["id"] for t in resp.json()["tasks"]}
        assert no_id in false_ids
        assert needs_id not in false_ids

    @pytest.mark.asyncio
    async def test_patch_requires_approval_false_sets_spend_approval_cleared_at(
        self, client: AsyncClient, db_session
    ):
        """PATCH {requires_approval: false} on a held compute_experiment task must
        set spend_approval_cleared_at so check_compute_spend_approval unblocks."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "compute_experiment", "spend_cap_usd": 20.0}],
            requires_approval=True,
            spend_approval_cleared_at=None,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.patch(
            f"/api/admin/tasks/{task.id}",
            json={"requires_approval": False},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(task)
        assert task.requires_approval is False
        assert task.spend_approval_cleared_at is not None


class TestPoolAccountMutationsRequireAdmin:
    """Mutation endpoints on /max-plan-accounts must reject non-admin callers."""

    @pytest.mark.asyncio
    async def test_post_max_plan_accounts_requires_admin(self, override_get_session):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/admin/max-plan-accounts", json={})
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_delete_max_plan_account_requires_admin(self, override_get_session):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.delete(
                    "/api/admin/max-plan-accounts/00000000-0000-0000-0000-000000000001"
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_patch_max_plan_account_requires_admin(self, override_get_session):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.patch(
                    "/api/admin/max-plan-accounts/00000000-0000-0000-0000-000000000001",
                    json={},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_reset_rate_limits_requires_admin(self, override_get_session):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/admin/max-plan-accounts/00000000-0000-0000-0000-000000000001/reset-rate-limits"
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_clear_regression_quarantine_requires_admin(
        self, override_get_session
    ):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/admin/max-plan-accounts/00000000-0000-0000-0000-000000000001/regression-quarantine/clear",
                    params={"provider": "anthropic", "model": "claude-opus-4-8"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_refresh_models_requires_admin(self, override_get_session):
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/admin/max-plan-accounts/00000000-0000-0000-0000-000000000001/refresh-models"
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Per-project pool consumption endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestProjectPoolUsageEndpoint:
    """GET /api/admin/pool-accounts/project-usage and project-summary endpoints."""

    @pytest.mark.asyncio
    async def test_project_usage_requires_auth(self, override_get_session):
        from fastapi import HTTPException

        async def reject_auth():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[get_current_user] = reject_auth
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    "/api/admin/pool-accounts/project-usage"
                    "?project_id=00000000-0000-0000-0000-000000000001"
                )
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_project_usage_404_for_unknown_project(
        self, client, db_session
    ):
        """Non-existent project or non-member → 404."""
        import uuid as _uuid
        from app.models import Project

        pid = _uuid.uuid4()
        project = Project(
            id=pid,
            name="orphan-project",
            user_id="someone-else",
            github_owner="other",
            github_repo="repo",
        )
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/pool-accounts/project-usage?project_id={pid}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_project_summary_returns_empty_for_user_with_no_projects(
        self, client, db_session
    ):
        """When the user has no pool accounts with project state, return empty list."""
        resp = await client.get("/api/admin/pool-accounts/project-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        assert isinstance(data["projects"], list)
        assert "generated_at" in data

    @pytest.mark.asyncio
    async def test_project_usage_response_shape(
        self, client, db_session
    ):
        """Verify the response shape when the user is a member of the project."""
        import uuid as _uuid
        from app.models import Project, ProjectMember
        from app.max_plan_pool import _account_by_id, _max_plan_pools

        pid = _uuid.uuid4()
        user_id = "test-user-123"

        project = Project(
            id=pid,
            name="test-project",
            user_id=user_id,
            github_owner="test",
            github_repo="repo",
        )
        db_session.add(project)
        db_session.add(
            ProjectMember(
                project_id=pid, user_id=user_id, role="owner"
            )
        )
        await db_session.commit()

        _account_by_id.clear()
        _max_plan_pools.clear()
        try:
            acct = {
                "id": "acc-1",
                "user_id": user_id,
                "name": "Test Account",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "enabled": True,
                "active_tasks": 5,
                "daily_task_count": 20,
                "daily_task_cap": 50,
                "max_concurrent_tasks": 10,
                "runtime_ready": True,
                "oauth_token": "sk-ant-test",
                "project_state": {
                    str(pid): {"active_tasks": 3, "daily_task_count": 12}
                },
                "project_daily_task_cap": {str(pid): 40},
                "project_max_concurrent_tasks": {str(pid): 8},
            }
            _account_by_id["acc-1"] = acct
            _max_plan_pools[user_id] = [acct]

            resp = await client.get(
                f"/api/admin/pool-accounts/project-usage?project_id={pid}"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["project_id"] == str(pid)
            assert data["total_project_active_tasks"] == 3
            assert data["total_project_daily_task_count"] == 12
            assert data["total_accounts"] == 1
            assert "accounts" in data
            assert len(data["accounts"]) == 1
            a = data["accounts"][0]
            assert a["account_id"] == "acc-1"
            assert a["project_active_tasks"] == 3
            assert a["project_daily_task_count"] == 12
            assert a["project_daily_task_cap"] == 40
            assert a["project_max_concurrent_tasks"] == 8
            assert a["at_project_cap"] is False
            assert a["global_active_tasks"] == 5
        finally:
            _account_by_id.clear()
            _max_plan_pools.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Task payload secret redaction (compute_experiment launch-spec secrets)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskPayloadSecretRedaction:
    """A compute_experiment task carries launch-spec secrets inline in
    Task.payloads (container_registry_auth, env) that the agent claim-path needs
    but operators must never see. The operator-facing task list/detail endpoints
    must redact them — matching serialize_compute_job_response_safe — so the same
    registry password isn't redacted in one operator view and leaked in another.
    """

    @staticmethod
    def _secret_payload():
        return [
            {
                "type": "compute_experiment",
                "title": "GPU run",
                "prompt": "run it",
                "base_branch": "main",
                "command": "python run.py",
                "cwd": "/work",
                "container_registry_auth": {
                    "server": "nvcr.io",
                    "username": "$oauthtoken",
                    "password": "SUPER-SECRET-REGISTRY-PW",
                },
                "env": {"HF_TOKEN": "hf_SECRET_ENV_TOKEN"},
            }
        ]

    async def _make_compute_task(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            title="GPU run",
            status=TaskStatus.QUEUED,
            payloads=self._secret_payload(),
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return project, task

    @pytest.mark.asyncio
    async def test_operator_task_detail_redacts_registry_password(
        self, client: AsyncClient, db_session
    ):
        _project, task = await self._make_compute_task(db_session)

        resp = await client.get(f"/api/admin/tasks/{task.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        payload = body["payloads"][0]
        # secret-bearing launch-spec keys are stripped
        assert "container_registry_auth" not in payload
        assert "env" not in payload
        # non-secret launch fields are preserved for operator debugging
        assert payload["command"] == "python run.py"
        assert payload["cwd"] == "/work"
        # other non-secret fields are preserved
        assert payload["type"] == "compute_experiment"
        assert payload["base_branch"] == "main"
        # the plaintext secrets never appear anywhere in the serialized response
        assert "SUPER-SECRET-REGISTRY-PW" not in resp.text
        assert "hf_SECRET_ENV_TOKEN" not in resp.text

        # redaction must not mutate / persist the stored payload — the agent
        # claim-path still needs the secret to launch the compute job.
        await db_session.refresh(task)
        assert (
            task.payloads[0]["container_registry_auth"]["password"]
            == "SUPER-SECRET-REGISTRY-PW"
        )
        assert task.payloads[0]["env"]["HF_TOKEN"] == "hf_SECRET_ENV_TOKEN"

    @pytest.mark.asyncio
    async def test_operator_task_list_redacts_registry_password(
        self, client: AsyncClient, db_session
    ):
        project, task = await self._make_compute_task(db_session)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        match = next(t for t in body["tasks"] if t["id"] == str(task.id))
        payload = match["payloads"][0]
        # The list view intentionally returns a slim payload projection
        # (type / pr_number / spend_cap_usd only — a deferred-column perf
        # optimization), so command/cwd/base_branch are absent here by design.
        # Redaction parity for those launch fields is covered by the detail
        # test (test_operator_task_detail_redacts_registry_password). This test
        # asserts only that no secret leaks into the list payload / response.
        assert "container_registry_auth" not in payload
        assert "env" not in payload
        assert "SUPER-SECRET-REGISTRY-PW" not in resp.text
        assert "hf_SECRET_ENV_TOKEN" not in resp.text


# ---------------------------------------------------------------------------
# create_direction: retired-outcome grounding surfaced in response
# ---------------------------------------------------------------------------

def _make_retired_memory_entry(
    direction_id: str,
    text_tokens: list,
    *,
    classification: str = "success_evidence_met",
    prs_merged: int = 3,
    cycles: int = 4,
) -> dict:
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "id": direction_id,
        "text_tokens": text_tokens,
        "reason": "Success evidence met: webhook reliability confirmed",
        "retired_at": (now - datetime.timedelta(days=10)).isoformat(),
        "outcome": {"classification": classification},
        "metrics": {"prs_merged": prs_merged, "cycles": cycles},
    }


async def _make_strategic_task_for_project(db_session, project):
    from app import strategic_cycle
    from app.models import Task, TaskStatus

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-retired-grounding-test",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_create_direction_endpoint_surfaces_prior_retired_outcome_with_mocked_memo(
    client: AsyncClient, db_session, mocker
):
    """Creating a direction whose text overlaps a recently-retired direction
    returns 200 (not blocked) AND includes retired_grounding in the response."""
    from app.auth import create_agent_token

    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock())
    # Retired memory entry whose text_tokens overlap the new direction text
    retired_entry = _make_retired_memory_entry(
        "d-retired001",
        ["stabilize", "billing", "webhooks", "reliability"],
        classification="success_evidence_met",
        prs_merged=3,
    )
    project = Project(
        name="Retired Grounding Test",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": [retired_entry],
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_strategic_task_for_project(db_session, project)
    token = create_agent_token(
        str(task.id), "vm-retired-grounding-test", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    # Mock fetch_retirement_memo to avoid DB knowledge-doc dependency
    mocker.patch(
        "app.knowledge_query.fetch_retirement_memo",
        new=AsyncMock(return_value=None),
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Stabilize billing webhooks for reliability",
                "priority": "high",
                "success_evidence": "Webhook retries are verified end-to-end",
                "retirement_condition": "No open webhook incidents for 30 days",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Direction was created
    assert body["status"] == "ok"
    assert body["op"] == "create_direction"
    assert "direction_id" in body
    # Prior-outcome grounding is attached
    assert "retired_grounding" in body, f"Expected retired_grounding in response; got: {body}"
    grounding = body["retired_grounding"]
    assert grounding["direction_id"] == "d-retired001"
    assert grounding["classification"] == "success_evidence_met"
    assert grounding["metrics"]["prs_merged"] == 3


@pytest.mark.asyncio
async def test_create_direction_no_retired_match_omits_grounding_field(
    client: AsyncClient, db_session, mocker
):
    """Creating a direction with no retired-memory match returns the
    unchanged response shape — no retired_grounding field."""
    from app.auth import create_agent_token

    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock())
    # Retired entry with disjoint text_tokens so no match
    retired_entry = _make_retired_memory_entry(
        "d-retired002",
        ["totally", "unrelated", "archived", "initiative"],
        classification="abandoned",
    )
    project = Project(
        name="No Retired Match Test",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": [retired_entry],
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_strategic_task_for_project(db_session, project)
    token = create_agent_token(
        str(task.id), "vm-retired-grounding-test", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Ship new onboarding experience for first-time users",
                "priority": "medium",
                "success_evidence": "Funnel conversion measured and improved",
                "retirement_condition": "Onboarding KPIs stable for 2 weeks",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "create_direction"
    assert "direction_id" in body
    # No match → no retired_grounding field
    assert "retired_grounding" not in body, f"Unexpected retired_grounding: {body}"


@pytest.mark.asyncio
async def test_create_direction_retired_match_does_not_block(
    client: AsyncClient, db_session, mocker
):
    """Advisory grounding must never hard-block the create — direction is
    created successfully even with a landed retired match."""
    from app.auth import create_agent_token

    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock())
    # Strong overlap with a landed direction
    retired_entry = _make_retired_memory_entry(
        "d-retired003",
        ["improve", "test", "coverage", "backend", "unit"],
        classification="landed",
        prs_merged=5,
    )
    project = Project(
        name="No Block Test",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": [retired_entry],
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_strategic_task_for_project(db_session, project)
    token = create_agent_token(
        str(task.id), "vm-retired-grounding-test", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    mocker.patch(
        "app.knowledge_query.fetch_retirement_memo",
        new=AsyncMock(return_value=None),
    )

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Improve test coverage for backend unit tests",
                "priority": "low",
                "success_evidence": "Coverage above 80%",
                "retirement_condition": "Coverage threshold met",
            },
        },
        headers=headers,
    )
    # Must succeed, not be blocked
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "direction_id" in body
    # Grounding attached (landed match)
    assert "retired_grounding" in body
    assert body["retired_grounding"]["classification"] == "landed"


# ═══════════════════════════════════════════════════════════════════════════════
# Completion-debt admin endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompletionDebtAdmin:
    """Tests for GET /completion-debts, POST /dismiss, and POST /resolve."""

    @pytest.mark.asyncio
    async def test_completion_debt_list_and_dismiss(
        self, client: AsyncClient, db_session
    ):
        """List returns entries, status filter works, dismiss sets status and reason."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)

        entry1 = CompletionDebtEntry(
            project_id=project.id,
            kind="knob_merge",
            title="Enable auto-fill mode",
            detail="Knob merged but enforcement not wired",
        )
        entry2 = CompletionDebtEntry(
            project_id=project.id,
            kind="capability_trial",
            title="No-CI gate active but unresolved",
        )
        db_session.add(entry1)
        db_session.add(entry2)
        await db_session.commit()
        await db_session.refresh(entry1)
        await db_session.refresh(entry2)

        # List all open entries
        resp = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?status=open"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["count"] == 2
        ids = {e["id"] for e in data["entries"]}
        assert str(entry1.id) in ids
        assert str(entry2.id) in ids

        # Dismiss one entry
        dismiss_resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{entry1.id}/dismiss",
            json={"reason": "Deferred to Q3"},
        )
        assert dismiss_resp.status_code == 200, dismiss_resp.text
        dismissed = dismiss_resp.json()
        assert dismissed["status"] == "ok"
        assert dismissed["entry"]["status"] == "dismissed"
        assert dismissed["entry"]["dismiss_reason"] == "Deferred to Q3"
        assert dismissed["entry"]["dismissed_at"] is not None

        # List open only — should now be 1
        resp2 = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?status=open"
        )
        assert resp2.status_code == 200
        assert resp2.json()["count"] == 1
        assert resp2.json()["entries"][0]["id"] == str(entry2.id)

        # List dismissed — should be 1
        resp3 = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?status=dismissed"
        )
        assert resp3.status_code == 200
        assert resp3.json()["count"] == 1
        assert resp3.json()["entries"][0]["id"] == str(entry1.id)

    @pytest.mark.asyncio
    async def test_completion_debt_list_kind_filter(
        self, client: AsyncClient, db_session
    ):
        """Kind filter returns only matching entries."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)

        db_session.add(CompletionDebtEntry(project_id=project.id, kind="knob_merge", title="A"))
        db_session.add(CompletionDebtEntry(project_id=project.id, kind="retirement_residual", title="B"))
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?kind=knob_merge"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["entries"][0]["kind"] == "knob_merge"

    @pytest.mark.asyncio
    async def test_completion_debt_resolve(self, client: AsyncClient, db_session):
        """Resolve endpoint marks entry resolved."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(project_id=project.id, kind="knob_merge", title="Wire enforce")
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{entry.id}/resolve"
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["entry"]["status"] == "resolved"
        assert result["entry"]["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_dismiss_unknown_id_returns_404(self, client: AsyncClient, db_session):
        """Dismissing a nonexistent debt entry returns 404."""
        project = await _make_project(db_session)
        fake_id = "00000000-0000-0000-0000-000000000099"
        resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{fake_id}/dismiss",
            json={"reason": "doesn't matter"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dismiss_already_dismissed_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Dismissing an already-dismissed entry returns 409."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id,
            kind="knob_merge",
            title="Already done",
            status="dismissed",
            dismiss_reason="Old reason",
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{entry.id}/dismiss",
            json={"reason": "New reason"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_dismiss_empty_reason_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Dismiss with empty reason returns 400."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(project_id=project.id, kind="knob_merge", title="X")
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{entry.id}/dismiss",
            json={"reason": ""},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_admin_returns_403(self, override_get_session, db_session):
        """Non-admin gets 403 on list endpoint."""
        from fastapi import HTTPException

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                fake_id = "00000000-0000-0000-0000-000000000001"
                resp = await ac.get(f"/api/admin/projects/{fake_id}/completion-debts")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_list_returns_empty_by_default(self, client: AsyncClient, db_session):
        """Listing debts for a project with no entries returns empty list."""
        project = await _make_project(db_session)
        resp = await client.get(f"/api/admin/projects/{project.id}/completion-debts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_dismiss_and_resolve_global_completion_debt_row(
        self, client: AsyncClient, db_session
    ):
        """Global debt rows (project_id=None) must be dismissible and resolvable
        via any project URL — not just the project that owns them."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)

        # Create two global rows (project_id=None — deployment-wide seeds)
        dismiss_entry = CompletionDebtEntry(
            project_id=None,
            kind="seed_inventory",
            title="Global seed: no-CI gate live trial",
        )
        resolve_entry = CompletionDebtEntry(
            project_id=None,
            kind="dormant_knob",
            title="Global dormant knob: auto-fill mode",
        )
        db_session.add(dismiss_entry)
        db_session.add(resolve_entry)
        await db_session.commit()
        await db_session.refresh(dismiss_entry)
        await db_session.refresh(resolve_entry)

        # Dismiss the global entry via a project-scoped URL
        dismiss_resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{dismiss_entry.id}/dismiss",
            json={"reason": "No longer applicable"},
        )
        assert dismiss_resp.status_code == 200, dismiss_resp.text
        dismissed = dismiss_resp.json()
        assert dismissed["entry"]["status"] == "dismissed"
        assert dismissed["entry"]["dismiss_reason"] == "No longer applicable"

        # Resolve the global entry via the same project-scoped URL
        resolve_resp = await client.post(
            f"/api/admin/projects/{project.id}/completion-debts/{resolve_entry.id}/resolve"
        )
        assert resolve_resp.status_code == 200, resolve_resp.text
        resolved = resolve_resp.json()
        assert resolved["entry"]["status"] == "resolved"
        assert resolved["entry"]["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_completion_debt_list_returns_total_count_and_has_more(
        self, client: AsyncClient, db_session
    ):
        """total_count reflects the true DB count even when entries are capped by limit."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)

        # Create 5 open + 2 dismissed entries
        for i in range(5):
            db_session.add(
                CompletionDebtEntry(
                    project_id=project.id,
                    kind="dormant_knob",
                    title=f"Open knob {i}",
                )
            )
        for i in range(2):
            db_session.add(
                CompletionDebtEntry(
                    project_id=project.id,
                    kind="capability_trial",
                    title=f"Dismissed trial {i}",
                    status="dismissed",
                    resolution_reason="done",
                )
            )
        await db_session.commit()

        # With limit=2 and status=open, total_count must reflect all 5 open rows
        resp = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?status=open&limit=2"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "total_count" in data, "response must include total_count"
        assert data["count"] == 2, "count should equal number of returned entries"
        assert data["total_count"] == 5, "total_count must reflect all open rows in the DB"
        assert data["total_count"] > data["count"], "total_count must exceed count when truncated"

        # With no status filter, total_count should include open+dismissed (7 total)
        resp_all = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?limit=1"
        )
        assert resp_all.status_code == 200
        data_all = resp_all.json()
        assert data_all["total_count"] == 7
        assert data_all["count"] == 1

        # With status=dismissed, total_count should be 2
        resp_dismissed = await client.get(
            f"/api/admin/projects/{project.id}/completion-debts?status=dismissed"
        )
        assert resp_dismissed.status_code == 200
        assert resp_dismissed.json()["total_count"] == 2


class TestStrategistDisposeCompletionDebt:
    """Strategic ops: dispose_completion_debt + debt_id linkage on spawn."""

    async def _make_strategic_task(self, db_session, project, vm="agent-vm-debt-op"):
        task = Task(
            project_id=project.id,
            title="Strategic review for debt disposition",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id=vm,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        from app.auth import create_agent_token

        token = create_agent_token(str(task.id), vm, task_type="strategic_review")
        return task, token

    @pytest.mark.asyncio
    async def test_strategist_resolve_completion_debt_requires_evidence(
        self, client: AsyncClient, db_session
    ):
        """Resolving a debt with no evidence is a 400; with evidence it resolves
        and persists the reason. (Named acceptance test for this task.)"""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id, kind="dormant_knob", title="Wire enforce path"
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        task, token = await self._make_strategic_task(db_session, project)

        # No evidence → 400, row stays open.
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {"debt_id": str(entry.id), "disposition": "resolved"},
            },
        )
        assert resp.status_code == 400, resp.text
        await db_session.refresh(entry)
        assert entry.status == "open"

        # With evidence → resolves and persists the reason.
        resp2 = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {
                    "debt_id": str(entry.id),
                    "disposition": "resolved",
                    "evidence": "Enforce path merged in PR #123; knob now default-on",
                },
            },
        )
        assert resp2.status_code == 200, resp2.text
        body = resp2.json()
        assert body["op"] == "dispose_completion_debt"
        assert body["entry"]["status"] == "resolved"
        await db_session.refresh(entry)
        assert entry.status == "resolved"
        assert entry.resolution_reason == (
            "Enforce path merged in PR #123; knob now default-on"
        )

    @pytest.mark.asyncio
    async def test_strategist_dismiss_completion_debt(
        self, client: AsyncClient, db_session
    ):
        """Disposition=dismissed sets the dismiss reason and status."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id, kind="capability_trial", title="Trial pending"
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        task, token = await self._make_strategic_task(db_session, project, vm="vm-dismiss")

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {
                    "debt_id": str(entry.id),
                    "disposition": "dismissed",
                    "reason": "Capability abandoned; no longer relevant",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["entry"]["status"] == "dismissed"
        await db_session.refresh(entry)
        assert entry.status == "dismissed"
        assert entry.resolution_reason == "Capability abandoned; no longer relevant"

    @pytest.mark.asyncio
    async def test_dispose_unknown_id_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Disposing a nonexistent debt id returns 404."""
        project = await _make_project(db_session)
        task, token = await self._make_strategic_task(db_session, project, vm="vm-404")
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {
                    "debt_id": "00000000-0000-0000-0000-0000000000ff",
                    "disposition": "resolved",
                    "evidence": "done",
                },
            },
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_dispose_already_resolved_returns_409(
        self, client: AsyncClient, db_session
    ):
        """Disposing an already-resolved debt returns 409."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id,
            kind="dormant_knob",
            title="Already resolved",
            status="resolved",
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        task, token = await self._make_strategic_task(db_session, project, vm="vm-409")

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {
                    "debt_id": str(entry.id),
                    "disposition": "resolved",
                    "evidence": "already done",
                },
            },
        )
        assert resp.status_code == 409, resp.text

    @pytest.mark.asyncio
    async def test_dispose_requires_strategic_task_type(
        self, client: AsyncClient, db_session
    ):
        """A non-strategic task type may not dispose completion debt (403)."""
        from app.auth import create_agent_token
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id, kind="dormant_knob", title="X"
        )
        db_session.add(entry)
        task = Task(
            project_id=project.id,
            title="Non-strategic task",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "prompt": "build"}],
            vm_instance_id="vm-nonstrat",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        await db_session.refresh(entry)
        token = create_agent_token(str(task.id), "vm-nonstrat", task_type="create")

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "dispose_completion_debt",
                "data": {
                    "debt_id": str(entry.id),
                    "disposition": "resolved",
                    "evidence": "done",
                },
            },
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_spawn_task_links_debt(self, client: AsyncClient, db_session):
        """spawn_task with debt_id stamps detail.linked_tasks and echoes
        linked_debt_id."""
        from app.models import CompletionDebtEntry

        project = await _make_project(db_session)
        entry = CompletionDebtEntry(
            project_id=project.id, kind="dormant_knob", title="Needs follow-up work"
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        task, token = await self._make_strategic_task(db_session, project, vm="vm-spawn-link")

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Investigate the dormant knob",
                    "prompt": "Research how to graduate the dormant knob to enforce",
                    "debt_id": str(entry.id),
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "spawn_task"
        assert body["linked_debt_id"] == str(entry.id)

        await db_session.refresh(entry)
        linked = (entry.detail or {}).get("linked_tasks") or []
        assert any(x.get("task_id") == body["task_id"] for x in linked), (
            f"spawned task must be linked; got detail={entry.detail!r}"
        )


@pytest.mark.asyncio
async def test_direction_created_during_gated_op_survives_commit(
    client: AsyncClient, db_session, mocker
):
    """A direction written by a concurrent writer during the handler's slow
    awaits must survive in the final array.

    Previously every strategic op used ``lambda _: _d`` which returned the
    pre-await snapshot regardless of what ``_with_directions_locked`` loaded
    inside the lock, silently overwriting any direction appended concurrently.
    The fix replaces each stale-snapshot closure with one that applies only
    the targeted mutation to the *fresh* list the lock provides.

    This test injects a ``d-concurrent`` direction into the list that
    ``_with_project_locked_list_mutation`` presents to the mutate_fn so that
    the CREATE closure (lambda fresh: [*fresh, new_dir]) exercises the fresh
    path rather than discarding the concurrent write.
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    import app.task_locking as _tl

    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock())

    concurrent_direction = {
        "id": "d-concurrent",
        "text": "concurrent direction written mid-flight",
        "status": "active",
        "priority": "low",
        "notes": [],
        "research_history": [],
        "context": {
            "success_evidence": "seen",
            "retirement_condition": "done",
        },
    }

    original_fn = _tl._with_project_locked_list_mutation

    async def _injecting_wrapper(session, project_id, mutate_fn, *, skip_locked):
        """Inject a concurrent direction into the fresh list before calling mutate_fn."""
        def _mutate_with_concurrent(fresh: list):
            # Simulate a concurrent writer having appended d-concurrent since
            # the handler's initial load but before the lock was acquired.
            augmented = list(fresh) + [concurrent_direction]
            return mutate_fn(augmented)

        return await original_fn(
            session, project_id, _mutate_with_concurrent, skip_locked=skip_locked
        )

    mocker.patch.object(_tl, "_with_project_locked_list_mutation", new=_injecting_wrapper)

    project = Project(
        name="ConcurrentWriteTest",
        user_id=TEST_USER_ID,
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-preexisting",
                "text": "pre-existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {
                    "success_evidence": "done",
                    "retirement_condition": "done",
                },
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_strategic_task_for_project(db_session, project)
    token = create_agent_token(
        str(task.id), "vm-retired-grounding-test", task_type="strategic_review"
    )

    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "New direction added by strategic op",
                "priority": "high",
                "success_evidence": "feature ships",
                "retirement_condition": "feature verified",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    new_dir_id = body["direction_id"]

    await db_session.refresh(project)
    ids = {d.get("id") for d in (project.directions or [])}
    assert "d-preexisting" in ids, f"pre-existing direction lost; got ids={ids}"
    assert "d-concurrent" in ids, f"concurrent direction overwritten; got ids={ids}"
    assert new_dir_id in ids, f"new direction missing; got ids={ids}"


# ═══════════════════════════════════════════════════════════════════════════════
# pending_confirm op — dedup + cap
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategicPendingConfirmDedup:
    """pending_confirm op: same-id idempotency and size-cap enforcement."""

    async def _setup(self, db_session):
        from app.auth import create_agent_token
        from app import strategic_cycle
        from app.models import Task, TaskStatus

        project = Project(
            name="PC Dedup Test",
            user_id=TEST_USER_ID,
            vision={"purpose": "test", "revision": 1},
            directions=[],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        cycle = await strategic_cycle.create(db_session, project_id=project.id)
        task = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="vm-pc-dedup-test",
            payloads=[
                {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
            ],
            context={"strategic_cycle_id": cycle.id},
            strategic_cycle_id=cycle.id,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), "vm-pc-dedup-test", task_type="strategic_review")
        return project, task, token

    @pytest.mark.asyncio
    async def test_pending_confirm_same_id_is_idempotent(self, db_session):
        """Submitting pending_confirm twice with the same caller-supplied id returns ok
        both times but stores only one item in pending_user_confirmation."""
        project, task, token = await self._setup(db_session)
        headers = {"x-agent-token": token}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            first = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-fixed-id-001",
                        "type": "credentials_needed",
                        "proposal": "Need API key.",
                    },
                },
            )
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["status"] == "ok"
            assert first_body["id"] == "p-fixed-id-001"

            second = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-fixed-id-001",
                        "type": "credentials_needed",
                        "proposal": "Need API key (retry).",
                    },
                },
            )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["status"] == "ok"
        assert second_body["id"] == "p-fixed-id-001"

        await db_session.refresh(project)
        pending = (project.problem_model or {}).get("pending_user_confirmation", [])
        ids = [p["id"] for p in pending]
        assert ids.count("p-fixed-id-001") == 1, f"expected exactly 1 entry, got: {ids}"

    @pytest.mark.asyncio
    async def test_pending_confirm_cap_returns_429(self, db_session):
        """When pending_user_confirmation already has PENDING_USER_CONFIRMATION_MAX items,
        a new op with a fresh id returns 429."""
        from app.problem_model import PENDING_USER_CONFIRMATION_MAX
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)

        # Pre-seed the project with the maximum number of pending confirmations.
        existing = [
            {
                "id": f"p-seed-{i:03d}",
                "type": "credentials_needed",
                "proposal": f"Seed item {i}",
                "rationale": "",
                "created_at": "2026-01-01T00:00:00",
            }
            for i in range(PENDING_USER_CONFIRMATION_MAX)
        ]
        project.problem_model = {"pending_user_confirmation": existing}
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-new-over-cap",
                        "type": "access_needed",
                        "proposal": "Need repo access.",
                    },
                },
            )
        assert resp.status_code == 429, resp.text
        assert "cap" in resp.text.lower() or str(PENDING_USER_CONFIRMATION_MAX) in resp.text

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_removes_item_by_id(self, db_session):
        """resolve_pending_confirm clears the strategist's own pending item by id,
        leaving siblings intact and reporting removed=True."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-clear-me",
                    "type": "credentials_needed",
                    "proposal": "Need a token.",
                    "rationale": "",
                    "created_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "p-keep",
                    "type": "access_needed",
                    "proposal": "Keep me.",
                    "rationale": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            ]
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-clear-me"},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "resolve_pending_confirm"
        assert body["removed"] is True

        await db_session.refresh(project)
        ids = [
            p["id"]
            for p in (project.problem_model or {}).get("pending_user_confirmation", [])
        ]
        assert ids == ["p-keep"]

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_unknown_id_is_ok_removed_false(
        self, db_session
    ):
        """Clearing an id that is not present is idempotent: ok with removed=False."""
        project, task, token = await self._setup(db_session)
        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-does-not-exist"},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["removed"] is False

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_requires_id(self, db_session):
        """A resolve_pending_confirm with no id is a 400."""
        project, task, token = await self._setup(db_session)
        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {},
                },
            )
        assert resp.status_code == 400, resp.text

    @pytest.mark.asyncio
    async def test_pending_confirm_appends_revision_log_entry(self, db_session):
        """pending_confirm bumps revision and appends a revision_log entry with
        pending_confirmation_added containing the confirmation id."""
        project, task, token = await self._setup(db_session)
        headers = {"x-agent-token": token}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-fixed-id-001",
                        "type": "credentials_needed",
                        "proposal": "Need API key.",
                    },
                },
            )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        pm = project.problem_model or {}
        assert pm.get("revision", 0) >= 1, "revision must be bumped after pending_confirm"
        log = pm.get("revision_log", [])
        assert log, "revision_log must be non-empty after pending_confirm"
        last_entry = log[-1]
        assert last_entry.get("changes", {}).get("pending_confirmation_added") == [
            "p-fixed-id-001"
        ], f"unexpected revision_log entry: {last_entry}"

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_appends_revision_log_entry(self, db_session):
        """resolve_pending_confirm bumps revision and appends a revision_log entry with
        pending_confirmation_resolved containing the cleared id when removed=True."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        project.problem_model = {
            "revision": 5,
            "pending_user_confirmation": [
                {
                    "id": "p-clear-me",
                    "type": "credentials_needed",
                    "proposal": "Need a token.",
                    "rationale": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            ],
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-clear-me"},
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["removed"] is True

        await db_session.refresh(project)
        pm = project.problem_model or {}
        assert pm.get("revision", 0) == 6, f"revision must be bumped to 6, got {pm.get('revision')}"
        log = pm.get("revision_log", [])
        assert log, "revision_log must be non-empty after resolve_pending_confirm"
        last_entry = log[-1]
        assert last_entry.get("changes", {}).get("pending_confirmation_resolved") == [
            "p-clear-me"
        ], f"unexpected revision_log entry: {last_entry}"

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_unknown_id_no_revision_log(self, db_session):
        """When removed=False (unknown id), revision and revision_log are unchanged."""
        project, task, token = await self._setup(db_session)
        initial_revision = (project.problem_model or {}).get("revision", 0)
        initial_log_len = len((project.problem_model or {}).get("revision_log", []))

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-does-not-exist"},
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is False

        await db_session.refresh(project)
        pm = project.problem_model or {}
        assert pm.get("revision", 0) == initial_revision, "revision must not change when removed=False"
        assert len(pm.get("revision_log", [])) == initial_log_len, "revision_log must not grow when removed=False"

    @pytest.mark.asyncio
    async def test_pending_confirm_preserves_user_provenance(self, db_session):
        """A pending_confirm add is a collection-only write to the agent-owned
        confirmation queue: it must NOT restamp last_updated_by as "agent" and
        thereby erase the "user" provenance a context-only operator edit set.
        _preserve_operator_intent reads last_updated_by == "user" to protect the
        operator's context against a later blank-context agent proposal."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        # Simulate a context-only operator edit: user-owned provenance + context.
        project.problem_model = {
            "revision": 3,
            "last_updated_by": "user",
            "context": {"notes": "operator-set context that must survive"},
            "pending_user_confirmation": [],
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-keep-prov",
                        "type": "credentials_needed",
                        "proposal": "Need a key.",
                    },
                },
            )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(project)
        pm = project.problem_model or {}
        assert pm.get("last_updated_by") == "user", (
            "pending_confirm must preserve the operator's 'user' provenance, "
            f"got {pm.get('last_updated_by')!r}"
        )
        assert pm.get("context", {}).get("notes") == "operator-set context that must survive"
        assert pm.get("revision", 0) == 4, "revision must still be bumped"

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_preserves_user_provenance(self, db_session):
        """A resolve_pending_confirm is likewise a collection-only write: clearing
        an agent-owned confirmation must not erase the operator's "user"
        provenance."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        project.problem_model = {
            "revision": 7,
            "last_updated_by": "user",
            "context": {"notes": "operator-set context that must survive"},
            "pending_user_confirmation": [
                {
                    "id": "p-resolve-prov",
                    "type": "credentials_needed",
                    "proposal": "Need a token.",
                    "rationale": "",
                    "created_at": "2026-01-01T00:00:00",
                },
            ],
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-resolve-prov"},
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is True

        await db_session.refresh(project)
        pm = project.problem_model or {}
        assert pm.get("last_updated_by") == "user", (
            "resolve_pending_confirm must preserve the operator's 'user' provenance, "
            f"got {pm.get('last_updated_by')!r}"
        )
        assert pm.get("context", {}).get("notes") == "operator-set context that must survive"
        assert pm.get("revision", 0) == 8, "revision must still be bumped"

    @pytest.mark.asyncio
    async def test_pending_confirm_with_poisoned_problem_model_succeeds(self, db_session):
        """pending_confirm op strips packer-marker keys before parsing so a poisoned
        problem_model row succeeds (sanitize strips unknown keys, not a 500 crash)."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        project.problem_model = {
            "revision": 1,
            "truncated": True,  # packer marker
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "pending_confirm",
                    "data": {
                        "id": "p-poison-op",
                        "type": "credentials_needed",
                        "proposal": "Need API key.",
                    },
                },
            )
        # sanitize_problem_model_raw strips "truncated" so parse_problem_model succeeds
        assert resp.status_code == 200, (
            f"Expected 200 after sanitization, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_resolve_pending_confirm_with_poisoned_problem_model_succeeds(
        self, db_session
    ):
        """resolve_pending_confirm op strips packer-marker keys before parsing so
        a poisoned problem_model row succeeds (sanitize strips unknown keys, not a 500 crash)."""
        from sqlalchemy.orm.attributes import flag_modified

        project, task, token = await self._setup(db_session)
        project.problem_model = {
            "revision": 1,
            "pending_user_confirmation": [
                {
                    "id": "p-poison-resolve",
                    "type": "credentials_needed",
                    "proposal": "Need a token.",
                    "rationale": "",
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "truncated": True,  # packer marker
        }
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "resolve_pending_confirm",
                    "data": {"id": "p-poison-resolve"},
                },
            )
        # sanitize_problem_model_raw strips "truncated" so parse_problem_model succeeds
        assert resp.status_code == 200, (
            f"Expected 200 after sanitization, got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["removed"] is True


class TestSetProblemModelWithPoisonedStoredModel:
    """set_problem_model op: stored problem_model containing legacy unknown keys (e.g.
    truncated:true from the rev-1367 wipe) must NOT permanently wedge all subsequent
    agent writes; the write path must fail loudly for real oversize, not silently
    truncate or swallow the error.

    Regression guard for the rev-1367 problem-model write wedge: the stored model
    had a stale "truncated": true top-level key (packer marker) that apply_agent_update
    now strips before parsing (sanitize_problem_model_raw on current_raw).
    """

    async def _setup(self, db_session, pm: dict | None = None):
        from app.auth import create_agent_token
        from app import strategic_cycle
        from app.models import Task, TaskStatus

        project = Project(
            name="PM Poison Test",
            user_id=TEST_USER_ID,
            vision={"purpose": "test", "revision": 1},
            problem_model=pm or {},
            directions=[],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        cycle = await strategic_cycle.create(db_session, project_id=project.id)
        task = Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="vm-pm-poison-test",
            payloads=[
                {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
            ],
            context={"strategic_cycle_id": cycle.id},
            strategic_cycle_id=cycle.id,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(
            str(task.id), "vm-pm-poison-test", task_type="strategic_review"
        )
        return project, task, token

    @pytest.mark.asyncio
    async def test_set_problem_model_with_poisoned_stored_model_succeeds(self, db_session):
        """set_problem_model op succeeds when the stored problem_model contains legacy
        unknown keys (truncated:true).  apply_agent_update strips unknown top-level
        keys from current_raw before parsing so a poisoned stored row does NOT block
        every subsequent agent write with HTTP 400.

        Regression for the rev-1367 write wedge: the problem model was wiped to
        constraints-only plus a stale "truncated": true key, causing every agent
        set_problem_model to fail because ProblemModel(extra="forbid") rejected the
        key when current_raw was passed unfiltered to parse_problem_model."""
        from sqlalchemy.orm.attributes import flag_modified

        poisoned_pm = {
            "revision": 5,
            "truncated": True,  # legacy packer marker — should be stripped before parse
            "intent": {"purpose": "Old purpose.", "current_focus": ""},
        }
        project, task, token = await self._setup(db_session, pm=poisoned_pm)
        # Force-write the poisoned model (above _setup already sets it, but
        # flag_modified ensures SQLAlchemy flushes it to the DB).
        project.problem_model = poisoned_pm
        flag_modified(project, "problem_model")
        db_session.add(project)
        await db_session.commit()

        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "set_problem_model",
                    "data": {
                        "intent": {
                            "purpose": "Updated purpose after wedge heal.",
                            "current_focus": "Resumed work.",
                        },
                    },
                },
            )
        # apply_agent_update strips "truncated" so write succeeds — not wedged to 400
        assert resp.status_code == 200, (
            f"Expected 200 (wedge healed by sanitization), got {resp.status_code}: {resp.text}"
        )
        # The revision must advance past the poisoned model's revision (5)
        body = resp.json()
        assert body.get("revision", 0) > 5, (
            f"Expected revision > 5 to prove the write went through; got {body.get('revision')}"
        )

    @pytest.mark.asyncio
    async def test_set_problem_model_oversize_live_core_fails_loudly(self, db_session):
        """set_problem_model op returns HTTP 400 naming the oversize section when the
        merged model's live core alone exceeds PROBLEM_MODEL_BYTE_BUDGET — proving
        the write path fails loudly rather than silently truncating or returning 200.

        The payload is kept under the 100KB incoming-request guard (admin_agent_ops
        line ~1190) while still exceeding the 90KB PROBLEM_MODEL_BYTE_BUDGET used by
        gc_problem_model, so the oversize is caught at the model-merge layer (not the
        raw-payload gate)."""
        import json

        from app.problem_model import PROBLEM_MODEL_BYTE_BUDGET

        # 23 gaps × 4 000-char descriptions ≈ 94 KB — above the 90 KB model budget
        # but below the 100 KB raw-payload check, so apply_agent_update's live-core
        # check fires and the op must return 400 with an actionable error message.
        big = "x" * 4_000
        oversized_gaps = [
            {"id": f"g-huge-{i}", "name": f"gap {i}", "status": "active", "description": big}
            for i in range(23)
        ]
        oversized_pm = {"gaps": oversized_gaps}
        payload_bytes = len(json.dumps(oversized_pm))
        assert payload_bytes > PROBLEM_MODEL_BYTE_BUDGET, (
            f"test setup: proposed payload ({payload_bytes} bytes) must exceed "
            f"PROBLEM_MODEL_BYTE_BUDGET ({PROBLEM_MODEL_BYTE_BUDGET})"
        )
        assert payload_bytes < 100_000, (
            f"test setup: proposed payload ({payload_bytes} bytes) must be under "
            "the 100 KB raw-payload gate so the model-merge oversize check fires"
        )

        project, task, token = await self._setup(db_session)
        headers = {"x-agent-token": token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                headers=headers,
                json={
                    "task_id": str(task.id),
                    "op": "set_problem_model",
                    "data": oversized_pm,
                },
            )
        # Must fail loudly with 400, naming the oversize section — NOT 200 (silent pass)
        # and NOT 500 (opaque crash).
        # The strategic API endpoint returns {"error": {"code": N, "message": "..."}}
        # (via the app's error-handler middleware) rather than FastAPI's default
        # {"detail": "..."} shape.
        assert resp.status_code == 400, (
            f"Expected 400 for oversize live core, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        error_msg = (
            body.get("error", {}).get("message")
            or body.get("detail")
            or ""
        )
        assert (
            "oversize section" in error_msg
            or "live core" in error_msg
            or "byte budget" in error_msg
        ), (
            f"Expected error to name the oversize section; got message: {error_msg!r}, "
            f"full body: {body!r}"
        )


class TestRecordFindingResolutionViaStrategicOp:
    """POST /api/agent/strategic op=record_finding_resolution — finding ledger write."""

    async def _make_strategic_task(self, db_session, project, vm="vm-finding-res"):
        task = Task(
            project_id=project.id,
            title="Strategic review for finding resolution",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id=vm,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        from app.auth import create_agent_token

        token = create_agent_token(str(task.id), vm, task_type="strategic_review")
        return task, token

    @pytest.mark.asyncio
    async def test_strategic_op_records_finding_resolution(
        self, client: AsyncClient, db_session
    ):
        """A strategic_review task posting record_finding_resolution writes an
        AuditFindingResolution ledger row for the open version."""
        from app.models import AuditFindingResolution

        project = await _make_project(db_session)
        version = await _make_open_version(db_session, project.id)
        task, token = await self._make_strategic_task(db_session, project)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "record_finding_resolution",
                "data": {
                    "finding_key": "acaa610d12345678",
                    "state": "resolved",
                    "severity": "medium",
                    "note": "Fixed in PR #100",
                    "resolving_ref": "abc123",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["op"] == "record_finding_resolution"
        assert body["finding_key"] == "acaa610d12345678"
        assert body["state"] == "resolved"
        assert body["release_version_id"] == str(version.id)

        row = (
            (
                await db_session.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == version.id,
                        AuditFindingResolution.finding_key == "acaa610d12345678",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.state == "resolved"
        assert row.note == "Fixed in PR #100"

    @pytest.mark.asyncio
    async def test_invalid_state_returns_400(self, client: AsyncClient, db_session):
        """An unrecognized state value is rejected with 400."""
        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)
        task, token = await self._make_strategic_task(
            db_session, project, vm="vm-bad-state"
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "record_finding_resolution",
                "data": {"finding_key": "acaa610d12345678", "state": "bogus"},
            },
        )
        assert resp.status_code == 400, resp.text
        assert "state" in resp.text.lower() or "bogus" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_non_strategic_task_type_returns_403(
        self, client: AsyncClient, db_session
    ):
        """A non-strategic task type (create) cannot call record_finding_resolution."""
        from app.auth import create_agent_token

        project = await _make_project(db_session)
        await _make_open_version(db_session, project.id)
        task = Task(
            project_id=project.id,
            title="Non-strategic task",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create", "prompt": "build"}],
            vm_instance_id="vm-nonstrat-fr",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        token = create_agent_token(
            str(task.id), "vm-nonstrat-fr", task_type="create"
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "record_finding_resolution",
                "data": {"finding_key": "acaa610d12345678", "state": "resolved"},
            },
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_no_open_version_returns_409(self, client: AsyncClient, db_session):
        """When no open release version exists, the op returns 409."""
        project = await _make_project(db_session)
        task, token = await self._make_strategic_task(
            db_session, project, vm="vm-no-version"
        )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "record_finding_resolution",
                "data": {"finding_key": "acaa610d12345678", "state": "stale"},
            },
        )
        assert resp.status_code == 409, resp.text


# ── Strategic op inventory parity test ────────────────────────────────────────
# Asserts that every op in STRATEGIC_AGENT_OPS has a real elif handler in
# admin_agent_ops.py AND that every handler (except known internal-only ops)
# is listed in the inventory. This is a standalone (no-DB) test so it can run
# in the fast standalone suite.


def _parse_strategic_op_handlers() -> set[str]:
    """Return the set of payload.op strings that have elif branches in strategic_operation."""
    import ast
    import inspect
    import app.routers.admin_agent_ops as _mod

    src = inspect.getsource(_mod.strategic_operation)
    tree = ast.parse(src)
    ops: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # Match: payload.op == "some_op"
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and left.attr == "op"
            and isinstance(left.value, ast.Name)
            and left.value.id == "payload"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
        ):
            ops.add(node.comparators[0].value)
    return ops


def _load_internal_only_ops() -> set[str]:
    """Return the set of ops in strategic_ops_manifest.json that are
    intentionally internal-only (non-null ``internal_reason``).

    These ops have live handlers in strategic_operation but are deliberately
    excluded from the surfaced STRATEGIC_AGENT_OPS inventory because they are
    invoked programmatically (never by the agent CLI).
    """
    manifest_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "app",
        "strategic_ops_manifest.json",
    )
    with open(manifest_path) as f:
        entries = json.load(f)
    return {e["op"] for e in entries if e.get("internal_reason")}


def test_strategic_agent_ops_all_have_real_handlers():
    """Bidirectional parity: STRATEGIC_AGENT_OPS ↔ strategic_operation handlers.

    Forward direction: every op in the inventory has a real elif handler.
    Reverse direction: every handler (except known internal-only ops, declared
    with non-null ``internal_reason`` in strategic_ops_manifest.json) is listed
    in the inventory.
    """
    from app.routers.admin_agent_ops import STRATEGIC_AGENT_OPS

    real_handlers = _parse_strategic_op_handlers()
    internal_only = _load_internal_only_ops()

    # Forward: ops in inventory but missing handler
    missing = STRATEGIC_AGENT_OPS - real_handlers
    assert not missing, (
        f"STRATEGIC_AGENT_OPS lists ops with no elif handler in strategic_operation: {sorted(missing)}. "
        "Add the handler or remove the op from STRATEGIC_AGENT_OPS."
    )

    # Reverse: handlers not in inventory and not intentionally internal-only
    orphans = real_handlers - STRATEGIC_AGENT_OPS - internal_only
    assert not orphans, (
        f"Handlers in strategic_operation missing from STRATEGIC_AGENT_OPS: {sorted(orphans)}. "
        "Add each op to the STRATEGIC_AGENT_OPS frozenset, or mark it as internal-only "
        "in strategic_ops_manifest.json with a non-null internal_reason."
    )


# ── _plan_subtasks unit tests ──────────────────────────────────────────────────


def test_plan_subtasks_none_when_plan_output_is_none():
    from app.admin import _plan_subtasks

    assert _plan_subtasks(None) is None


def test_plan_subtasks_none_when_plan_output_is_list():
    from app.admin import _plan_subtasks

    # research tasks use a list-shaped plan_output
    assert _plan_subtasks([{"title": "Finding 1"}]) is None


def test_plan_subtasks_none_when_plan_output_is_string():
    from app.admin import _plan_subtasks

    assert _plan_subtasks("not a dict") is None  # type: ignore[arg-type]


def test_plan_subtasks_none_when_subtasks_missing():
    from app.admin import _plan_subtasks

    assert _plan_subtasks({"description": "no subtasks key"}) is None


def test_plan_subtasks_none_when_subtasks_not_a_list():
    from app.admin import _plan_subtasks

    assert _plan_subtasks({"subtasks": {"a": "b"}}) is None


def test_plan_subtasks_empty_list_when_subtasks_is_empty():
    from app.admin import _plan_subtasks

    assert _plan_subtasks({"subtasks": []}) == []


def test_plan_subtasks_filters_non_dict_entries():
    from app.admin import _plan_subtasks

    result = _plan_subtasks({"subtasks": ["string_entry", 42, {"title": "Real", "depends_on_indices": [0]}]})
    assert result == [{"title": "Real", "depends_on_indices": [0]}]


def test_plan_subtasks_returns_title_and_depends_on_indices():
    from app.admin import _plan_subtasks

    plan = {
        "subtasks": [
            {"title": "Step A", "depends_on_indices": []},
            {"title": "Step B", "depends_on_indices": [0], "extra_field": "ignored"},
        ]
    }
    result = _plan_subtasks(plan)
    assert result == [
        {"title": "Step A", "depends_on_indices": []},
        {"title": "Step B", "depends_on_indices": [0]},
    ]


def test_plan_subtasks_defaults_missing_fields():
    from app.admin import _plan_subtasks

    # subtask dict with neither title nor depends_on_indices
    result = _plan_subtasks({"subtasks": [{}]})
    assert result == [{"title": "", "depends_on_indices": []}]


# ── GET /api/admin/tasks — plan_subtasks contract ─────────────────────────────


class TestTaskListPlanSubtasksContract:
    """GET /api/admin/tasks drops heavy JSONB blobs and exposes plan_subtasks."""

    @pytest.mark.asyncio
    async def test_list_hides_backend_managed_unless_explicitly_included(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        normal = Task(project_id=project.id, title="normal", payloads=[])
        archived = Task(
            project_id=project.id,
            title="archived",
            payloads=[{"type": "create"}],
            archived=True,
        )
        db_session.add(normal)
        db_session.add(archived)
        await db_session.commit()

        default = await client.get(f"/api/admin/tasks?project_id={project.id}")
        # BACKEND_MANAGED_TASK_TYPES is empty now, so the default view no longer
        # hides any backend-managed row — it matches the debug view below.
        assert {row["title"] for row in default.json()["tasks"]} == {
            "normal",
            "archived",
        }
        debug = await client.get(
            f"/api/admin/tasks?project_id={project.id}&include_backend_managed=true"
        )
        assert {row["title"] for row in debug.json()["tasks"]} == {
            "normal",
            "archived",
        }

    @pytest.mark.asyncio
    async def test_list_omits_heavy_jsonb_fields(self, client: AsyncClient, db_session):
        """status_log, plan_output, and fix_loop_history must not appear in list response."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.IN_PROGRESS,
            plan_output={
                "subtasks": [
                    {"title": "Write tests", "depends_on_indices": []},
                    {"title": "Review", "depends_on_indices": [0]},
                ]
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        tasks = resp.json()["tasks"]
        assert len(tasks) == 1
        t = tasks[0]

        # Heavy JSONB columns must be absent from the list wire payload
        assert "status_log" not in t, "status_log must not be in list response"
        assert "plan_output" not in t, "plan_output must not be in list response"
        assert "fix_loop_history" not in t, "fix_loop_history must not be in list response"

    @pytest.mark.asyncio
    async def test_list_returns_plan_subtasks_shape(self, client: AsyncClient, db_session):
        """plan_subtasks carries {title, depends_on_indices} for each subtask."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.IN_PROGRESS,
            plan_output={
                "subtasks": [
                    {"title": "Step 1", "depends_on_indices": []},
                    {"title": "Step 2", "depends_on_indices": [0], "extra": "dropped"},
                ]
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        t = resp.json()["tasks"][0]

        assert t["plan_subtasks"] == [
            {"title": "Step 1", "depends_on_indices": []},
            {"title": "Step 2", "depends_on_indices": [0]},
        ]

    @pytest.mark.asyncio
    async def test_list_plan_subtasks_null_when_no_plan_output(
        self, client: AsyncClient, db_session
    ):
        """plan_subtasks is null when a task has no plan_output."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        t = resp.json()["tasks"][0]
        assert t["plan_subtasks"] is None

    @pytest.mark.asyncio
    async def test_list_fix_loop_summary_is_null(self, client: AsyncClient, db_session):
        """fix_loop_summary in the list response is always null (fix_loop_history not loaded)."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        t = resp.json()["tasks"][0]
        assert t["fix_loop_summary"] is None

    @pytest.mark.asyncio
    async def test_list_payloads_strips_large_fields(self, client: AsyncClient, db_session):
        """payloads in the list response contains only slim display fields — no prompt/research_findings."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{
                "type": "review",
                "pr_number": 42,
                "prompt": "A" * 10000,
                "research_findings": [{"title": "Finding"}] * 50,
                "status_log": [{"msg": "step"}] * 200,
            }],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        t = resp.json()["tasks"][0]

        # task_type is still derived from payloads[0].type
        assert t["task_type"] == "review"

        # payloads in the list response must be slim — no large fields
        assert isinstance(t["payloads"], list)
        payload = t["payloads"][0] if t["payloads"] else {}
        assert payload.get("type") == "review"
        assert payload.get("pr_number") == 42
        assert "prompt" not in payload, "prompt must not appear in list payloads"
        assert "research_findings" not in payload, "research_findings must not appear in list payloads"
        assert "status_log" not in payload, "status_log must not appear in list payloads"

    @pytest.mark.asyncio
    async def test_list_task_type_from_slim_payload_query(self, client: AsyncClient, db_session):
        """task_type is correctly derived from the slim payload batch query."""
        project = await _make_project(db_session)
        for task_type in ("create", "research", "fix", "review"):
            db_session.add(Task(
                project_id=project.id,
                payloads=[{"type": task_type, "prompt": "large prompt " * 100}],
                title=f"task-{task_type}",
                status=TaskStatus.QUEUED,
            ))
        await db_session.commit()

        resp = await client.get(f"/api/admin/tasks?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        by_title = {t["title"]: t for t in resp.json()["tasks"]}
        for task_type in ("create", "research", "fix", "review"):
            assert by_title[f"task-{task_type}"]["task_type"] == task_type, (
                f"task_type mismatch for {task_type}"
            )


class TestStrategistCancelTask:
    """Strategic cancel_task op: the strategist can retract its own dispatched work."""

    @pytest.mark.asyncio
    async def test_cancel_spawned_task(self, client: AsyncClient, db_session):
        """Spawn a task via spawn_task, then cancel it. Assert status transitions
        to CANCELLED and the reason is recorded in the task context."""
        from app.auth import create_agent_token

        project = Project(name="CancelSpawnedTaskTest", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for cancel test",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-cancel-spawn-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-cancel-spawn-test",
            task_type="strategic_review",
        )

        # Spawn a QUEUED child task
        spawn_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Cancel test research",
                    "prompt": "Investigate cancellability",
                },
            },
        )
        assert spawn_resp.status_code == 200, spawn_resp.text
        spawn_body = spawn_resp.json()
        child_id = spawn_body["task_id"]

        child = await db_session.get(Task, uuid.UUID(child_id))
        assert child is not None
        assert child.status == TaskStatus.QUEUED

        # Cancel the spawned task
        cancel_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "cancel_task",
                "data": {
                    "target_task_id": child_id,
                    "reason": "Wrong diagnosis — the identified issue does not exist",
                },
            },
        )
        assert cancel_resp.status_code == 200, cancel_resp.text
        cancel_body = cancel_resp.json()
        assert cancel_body["status"] == "ok"
        assert cancel_body["op"] == "cancel_task"
        assert cancel_body["target_task_id"] == child_id
        assert cancel_body["previous_status"] == "QUEUED"

        await db_session.refresh(child)
        assert child.status == TaskStatus.CANCELLED
        assert child.context.get("cancel_reason") == (
            "Wrong diagnosis — the identified issue does not exist"
        )
        assert child.context.get("cancelled_by") == "STRATEGIC_AGENT"
        assert child.context.get("cancelled_by_task_id") == str(strategic_task.id)
        assert child.context.get("cancelled_at") is not None

    @pytest.mark.asyncio
    async def test_cancel_rejects_wrong_session(self, client: AsyncClient, db_session):
        """Cancelling a task that the calling strategic session did NOT spawn
        must be rejected with 403."""
        from app.auth import create_agent_token

        project = Project(name="CancelWrongSessionTest", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Session A spawns a task
        session_a = Task(
            project_id=project.id,
            title="Session A strategic review",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-cancel-sess-a",
        )
        db_session.add(session_a)
        await db_session.commit()
        await db_session.refresh(session_a)

        token_a = create_agent_token(
            str(session_a.id),
            "agent-vm-cancel-sess-a",
            task_type="strategic_review",
        )

        spawn_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token_a},
            json={
                "task_id": str(session_a.id),
                "op": "spawn_task",
                "data": {
                    "type": "research",
                    "title": "Session A research",
                    "prompt": "Research A",
                },
            },
        )
        assert spawn_resp.status_code == 200, spawn_resp.text
        child_id = spawn_resp.json()["task_id"]

        # Session B tries to cancel it
        session_b = Task(
            project_id=project.id,
            title="Session B strategic review",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-cancel-sess-b",
        )
        db_session.add(session_b)
        await db_session.commit()
        await db_session.refresh(session_b)

        token_b = create_agent_token(
            str(session_b.id),
            "agent-vm-cancel-sess-b",
            task_type="strategic_review",
        )

        cancel_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token_b},
            json={
                "task_id": str(session_b.id),
                "op": "cancel_task",
                "data": {
                    "target_task_id": child_id,
                    "reason": "Session B thinks this was a mistake",
                },
            },
        )
        assert cancel_resp.status_code == 403, cancel_resp.text

        # The child must remain QUEUED
        child = await db_session.get(Task, uuid.UUID(child_id))
        assert child is not None
        assert child.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_cancel_rejects_in_progress(self, client: AsyncClient, db_session):
        """Cancelling an IN_PROGRESS task must be rejected with 409."""
        from app.auth import create_agent_token

        project = Project(name="CancelInProgressTest", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for IN_PROGRESS cancel",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-cancel-ip-test",
            context={
                "parent_strategic_task_id": "placeholder",
                "spawned_by": "strategic_direct",
            },
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        # Set parent_strategic_task_id after we know the strategic task's id
        from sqlalchemy.orm.attributes import flag_modified
        strategic_task.context["parent_strategic_task_id"] = str(strategic_task.id)
        flag_modified(strategic_task, "context")
        db_session.add(strategic_task)
        await db_session.commit()

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-cancel-ip-test",
            task_type="strategic_review",
        )

        cancel_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "cancel_task",
                "data": {
                    "target_task_id": str(strategic_task.id),
                    "reason": "Test: cancelling IN_PROGRESS should fail",
                },
            },
        )
        assert cancel_resp.status_code == 409, cancel_resp.text

        # The strategic task must still be IN_PROGRESS
        await db_session.refresh(strategic_task)
        assert strategic_task.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_cancel_requires_reason(self, client: AsyncClient, db_session):
        """cancel_task without a 'reason' must be rejected."""
        from app.auth import create_agent_token

        project = Project(name="CancelNoReasonTest", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        strategic_task = Task(
            project_id=project.id,
            title="Strategic review for cancel no-reason",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-cancel-nr-test",
        )
        db_session.add(strategic_task)
        await db_session.commit()
        await db_session.refresh(strategic_task)

        from sqlalchemy.orm.attributes import flag_modified
        strategic_task.context["parent_strategic_task_id"] = str(strategic_task.id)
        flag_modified(strategic_task, "context")
        db_session.add(strategic_task)
        await db_session.commit()

        token = create_agent_token(
            str(strategic_task.id),
            "agent-vm-cancel-nr-test",
            task_type="strategic_review",
        )

        cancel_resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic_task.id),
                "op": "cancel_task",
                "data": {
                    "target_task_id": str(strategic_task.id),
                },
            },
        )
        assert cancel_resp.status_code == 400, cancel_resp.text


# ---------------------------------------------------------------------------
# Spend accounting — LLM costs in project spend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_recent_spend_includes_llm_costs(db_session):
    """LLM API costs are included in get_project_recent_spend_usd total."""
    from app.models import TaskStatus, _utcnow
    from app.resource_envelope import get_project_recent_spend_usd
    from testing.factories import make_project, make_task

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-8",
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
        },
        vm_runtime_cost_banked_db=5.0,
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )

    # LLM cost: 1000 input @ 5e-6 + 200 output @ 25e-6 = 0.01
    assert result["llm"] == pytest.approx(0.01, abs=1e-6)
    # Separate VM cost
    assert result["agent_vm"] == pytest.approx(5.0, abs=1e-6)
    # Total includes both cost types
    assert result["total"] == pytest.approx(5.01, abs=1e-6)


# ── POST /api/admin/projects/{project_id}/messages/attachment-upload-url ─────


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_success(
    client: AsyncClient, db_session
):
    """Successfully generate signed upload + download URLs for a message attachment."""
    from unittest.mock import MagicMock

    project = Project(name="Attach Upload", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="member")
    db_session.add(member)
    await db_session.commit()

    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-upload"
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    from app import storage as _app_storage

    with patch.object(_app_storage.storage, "Client", return_value=mock_client):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "test-image.png", "content_type": "image/png", "size_bytes": 1234},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["upload_url"] == "https://storage.googleapis.com/signed-upload"
    # The mock returns the same URL for both upload and download signing.
    assert data["download_url"] == "https://storage.googleapis.com/signed-upload"
    assert "gs://" in data["storage_uri"]
    assert "message-attachments/" in data["storage_uri"]
    assert data["content_type"] == "image/png"
    assert data["size_bytes"] == 1234
    assert data["attachment_id"] is not None
    assert len(data["attachment_id"]) > 0
    assert mock_blob.generate_signed_url.call_args_list[0].kwargs["method"] == "PUT"
    assert mock_blob.generate_signed_url.call_args_list[0].kwargs["content_type"] == "image/png"
    assert mock_blob.generate_signed_url.call_args_list[0].kwargs["headers"] == {"Content-Length": "1234"}
    assert mock_blob.generate_signed_url.call_args_list[0].kwargs["expiration"].total_seconds() == 900
    assert mock_blob.generate_signed_url.call_args_list[1].kwargs["method"] == "GET"
    assert mock_blob.generate_signed_url.call_args_list[1].kwargs["expiration"].total_seconds() == 3600
    from app.models import ProjectMessageAttachmentUpload
    receipt = await db_session.get(
        ProjectMessageAttachmentUpload, uuid.UUID(data["attachment_id"])
    )
    assert receipt is not None
    assert receipt.expected_size_bytes == 1234
    assert receipt.actor_user_id == TEST_USER_ID


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_sanitizes_filename_and_content_type(
    client: AsyncClient, db_session
):
    """Object names stay under the project attachment prefix with safe filename segments."""
    from unittest.mock import MagicMock

    project = Project(name="Attach Upload Sanitized", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="member")
    db_session.add(member)
    await db_session.commit()

    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-upload"
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    from app import storage as _app_storage

    with patch.object(_app_storage.storage, "Client", return_value=mock_client):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "../../nested\\evil file!!.png", "size_bytes": 9},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["content_type"] == "application/octet-stream"
    assert f"message-attachments/{project.id}/" in data["storage_uri"]
    assert data["storage_uri"].endswith("/evil_file_.png")
    assert mock_bucket.blob.call_args_list[0].args[0].endswith("/evil_file_.png")
    assert mock_blob.generate_signed_url.call_args_list[0].kwargs["content_type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_auth_failure(
    override_get_session, db_session
):
    """Unauthenticated callers are rejected with 401/403."""
    from unittest.mock import MagicMock
    from app.auth import get_current_user

    project = Project(name="Attach Upload Auth", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="member")
    db_session.add(member)
    await db_session.commit()

    async def _reject():
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user] = _reject
    try:
        from httpx import AsyncClient, ASGITransport

        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        from app import storage as _app_storage

        _TEST_CSRF_TOKEN = "test-csrf-token-auth"

        with patch.object(_app_storage.storage, "Client", return_value=mock_client):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"X-CSRF-Token": _TEST_CSRF_TOKEN},
                cookies={"csrf_token": _TEST_CSRF_TOKEN},
            ) as anon:
                resp = await anon.post(
                    f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
                    json={"filename": "test.png", "content_type": "image/png", "size_bytes": 9},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code in (401, 403), f"Expected auth failure, got {resp.status_code}"


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_nonmember_rejected_before_signing(
    client: AsyncClient, db_session
):
    """Project membership is checked before any signed URL is minted."""
    from unittest.mock import MagicMock

    project = Project(name="Attach Upload Private", user_id="other-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mock_client = MagicMock()

    from app import storage as _app_storage

    with patch.object(_app_storage.storage, "Client", return_value=mock_client):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "test.png", "content_type": "image/png", "size_bytes": 9},
        )

    assert resp.status_code == 404, resp.text
    mock_client.bucket.assert_not_called()


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_rejects_invalid_content_type(
    client: AsyncClient, db_session
):
    """Invalid Content-Type values are rejected before GCS signing."""
    from unittest.mock import MagicMock

    project = Project(name="Attach Upload Bad Content Type", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="member")
    db_session.add(member)
    await db_session.commit()

    mock_client = MagicMock()

    from app import storage as _app_storage

    with patch.object(_app_storage.storage, "Client", return_value=mock_client):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "test.png", "content_type": "image/png\r\nx: y", "size_bytes": 9},
        )

    assert resp.status_code == 400, resp.text
    mock_client.bucket.assert_not_called()


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_storage_unavailable(
    client: AsyncClient, db_session
):
    """When GCS signing fails, the endpoint returns 503."""
    from unittest.mock import MagicMock

    project = Project(name="Attach Upload Fail", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="member")
    db_session.add(member)
    await db_session.commit()

    mock_blob = MagicMock()
    mock_blob.generate_signed_url.side_effect = Exception("GCS signing failed")
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    from app import storage as _app_storage

    with patch.object(_app_storage.storage, "Client", return_value=mock_client):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "test.png", "content_type": "image/png", "size_bytes": 9},
        )

    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_get_message_attachment_upload_url_rejects_oversized_declared_body(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    response = await client.post(
        f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
        json={
            "filename": "too-large.bin",
            "content_type": "application/octet-stream",
            "size_bytes": 50 * 1024 * 1024 + 1,
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_message_attachment_upload_cleans_unconsumed_object(
    client: AsyncClient, db_session
):
    from app.models import ProjectMessageAttachmentUpload

    project = await _make_project(db_session)
    attachment_id = uuid.uuid4()
    storage_uri = (
        f"gs://bucket/message-attachments/{project.id}/{attachment_id}/orphan.bin"
    )
    db_session.add(ProjectMessageAttachmentUpload(
        attachment_id=attachment_id,
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=storage_uri,
        filename="orphan.bin",
        content_type="application/octet-stream",
        expected_size_bytes=10,
        expires_at=_utcnow() + timedelta(minutes=15),
    ))
    await db_session.commit()

    with patch(
        "app.routers.admin_project_strategic_session.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as cleanup:
        response = await client.delete(
            f"/api/admin/projects/{project.id}/messages/attachment-uploads/{attachment_id}"
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deleted", "deleted": True}
    cleanup.assert_awaited_once_with(storage_uri)
    assert await db_session.get(ProjectMessageAttachmentUpload, attachment_id) is None


@pytest.mark.asyncio
async def test_upload_url_opportunistically_cleans_bounded_expired_receipts(
    client: AsyncClient, db_session
):
    from unittest.mock import MagicMock
    from app.models import ProjectMessageAttachmentUpload
    from app import storage as _app_storage

    project = await _make_project(db_session)
    expired_id = uuid.uuid4()
    expired_uri = (
        f"gs://bucket/message-attachments/{project.id}/{expired_id}/expired.bin"
    )
    db_session.add(ProjectMessageAttachmentUpload(
        attachment_id=expired_id,
        project_id=project.id,
        actor_user_id=TEST_USER_ID,
        storage_uri=expired_uri,
        filename="expired.bin",
        content_type="application/octet-stream",
        expected_size_bytes=10,
        created_at=_utcnow() - timedelta(minutes=16),
        expires_at=_utcnow() - timedelta(minutes=1),
    ))
    await db_session.commit()

    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://storage.invalid/signed"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    storage_client = MagicMock()
    storage_client.bucket.return_value = bucket
    with (
        patch.object(_app_storage.storage, "Client", return_value=storage_client),
        patch(
            "app.routers.admin_project_strategic_session.delete_storage_uri_best_effort",
            new=AsyncMock(return_value=True),
        ) as cleanup,
    ):
        response = await client.post(
            f"/api/admin/projects/{project.id}/messages/attachment-upload-url",
            json={"filename": "fresh.bin", "size_bytes": 10},
        )

    assert response.status_code == 200, response.text
    cleanup.assert_any_await(expired_uri)
    db_session.expire_all()
    assert await db_session.get(ProjectMessageAttachmentUpload, expired_id) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Runner lifecycle + compute-image endpoints
# (moved to test_admin_routers__runners.py)
# ═══════════════════════════════════════════════════════════════════════════════

