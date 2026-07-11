"""Tests for the pull-based local fleet runner.

Covers the four new surfaces:
  * runner auth (app.auth: create/hash/verify token, get_current_runner)
  * admin management endpoints (app.routers.admin_runners)
  * runner polling/heartbeat endpoints (app.routers.runner)
  * orchestrator runner routing helpers (app.orchestrator)
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app import alert_dispatch
from app import auth as auth_mod
from app import compute_backends as compute_backends_mod
from app import max_plan_pool as mpp
from app import orchestrator as orch
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.models import (
    Alert,
    AlertSeverity,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    LocalRunner,
    MessagesNoisePrefs,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)
from app.messages_noise_prefs_store import get_messages_noise_prefs_row
from app.routers.admin_runners import _runner_health
from app.routers.runner import _parse_heartbeat_timestamp, _parse_multipart_form
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    WORKBENCH_STATE_KEY,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session, owner_id=TEST_USER_ID, name="Runner Project"):
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    await session.commit()
    return project


async def _make_runner(session, project_id, **kwargs):
    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project_id,
        token_hash=auth_mod.hash_runner_token(token),
        last_seen_at=kwargs.pop("last_seen_at", _utcnow()),
        capabilities=kwargs.pop(
            "capabilities",
            {"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        ),
        **kwargs,
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner, token


def _seed_pool_account(
    user_id: str = TEST_USER_ID,
    *,
    account_id: str | None = None,
    provider: str = "anthropic",
    auth_mode: str = "oauth",
    runtime_secret: str = "sk-ant-test-secret-token",
    active_tasks: int = 0,
    enabled: bool = True,
):
    """Seed a single LLM account into the in-memory pool so
    select_llm_runtime_for_task() can resolve one in tests."""
    acct_id = account_id or str(uuid.uuid4())
    entry: dict = {
        "id": acct_id,
        "user_id": user_id,
        "name": f"test-acct-{acct_id[:8]}",
        "provider": provider,
        "auth_mode": auth_mode,
        "secret_blob_ref": None,
        "provider_metadata": {},
        "runtime_secret": runtime_secret,
        "oauth_token": runtime_secret if provider == "anthropic" and auth_mode == "oauth" else None,
        "runtime_ready": True,
        "claude_config_json": None,
        "enabled": enabled,
        "active_tasks": int(active_tasks),
        "total_tasks": 0,
        "daily_task_cap": None,
        "max_concurrent_tasks": None,
        "auto_disable_after_rate_limits": 3,
        "daily_task_count": 0,
        "daily_task_count_reset_at": None,
        "disabled_reason": None,
        "disabled_at": None,
        "token_created_at": None,
        "rate_limit_state": {},
        "routing_tags": [],
        "target_utilization_pct": None,
        "gpu_count": None,
        "gpu_names": None,
    }
    mpp._account_by_id[acct_id] = entry
    mpp._max_plan_pools.setdefault(user_id, []).append(entry)
    return entry


def _clear_pool():
    """Remove all seeded accounts from the in-memory pool."""
    mpp._max_plan_pools.clear()
    mpp._account_by_id.clear()

def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


async def _seed_strategic_continuation(
    session,
    project,
    *,
    timestamp: str,
    interaction_mode: str = "continuous",
    runner_id: str | None = None,
    include_marker: bool = True,
    include_marker_runner: bool = True,
    include_workbench_runner: bool = True,
) -> None:
    workbench = {
        "version": 1,
        "interaction_mode": interaction_mode,
        "active_surface": "runtime",
        "selected_refs": [],
        "session_started_at": timestamp,
        "last_interaction_at": timestamp,
    }
    if runner_id and include_workbench_runner:
        workbench["preferred_runner_id"] = runner_id
    session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={WORKBENCH_STATE_KEY: workbench},
        )
    )
    if include_marker:
        marker = {
            "version": 1,
            "active": True,
            "operator_user_id": TEST_USER_ID,
            "updated_at": timestamp,
            "last_seen_at": timestamp,
        }
        if runner_id and include_marker_runner:
            marker["preferred_runner_id"] = runner_id
        project.vision = {
            **(project.vision or {}),
            STRATEGIC_LOCAL_CONTINUATION_KEY: marker,
        }
        session.add(project)
    await session.commit()


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestRunnerAuth:
    def test_token_is_random_and_hash_is_stable(self):
        t1 = auth_mod.create_runner_token(uuid.uuid4())
        t2 = auth_mod.create_runner_token(uuid.uuid4())
        assert t1 != t2
        assert len(t1) >= 32
        # Hash is deterministic SHA256 hex (64 chars), and never equals plaintext.
        h = auth_mod.hash_runner_token(t1)
        assert h == auth_mod.hash_runner_token(t1)
        assert len(h) == 64
        assert h != t1

    @pytest.mark.asyncio
    async def test_verify_runner_token_resolves_non_deleted(self, db_session):
        """FIX B3(a): any NON-deleted runner authenticates (incl. inactive).

        An inactive runner (deactivated after a missed heartbeat — slept/network-
        dropped host) MUST still authenticate so its heartbeat can re-arm it.
        Only deleted/unknown tokens are rejected.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        found = await auth_mod.verify_runner_token(token, db_session)
        assert found is not None and found.id == runner.id

        # Wrong token resolves to None.
        assert await auth_mod.verify_runner_token("nope", db_session) is None

        # An INACTIVE runner still authenticates (this is the lockout fix).
        runner.status = "inactive"
        db_session.add(runner)
        await db_session.commit()
        reactivatable = await auth_mod.verify_runner_token(token, db_session)
        assert reactivatable is not None and reactivatable.id == runner.id

        # A DELETED runner never authenticates.
        runner.status = "deleted"
        db_session.add(runner)
        await db_session.commit()
        assert await auth_mod.verify_runner_token(token, db_session) is None


@pytest.mark.asyncio
async def test_locked_noise_prefs_read_refreshes_identity_mapped_row(db_session):
    project = await _make_project(db_session)
    old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
    await _seed_strategic_continuation(
        db_session,
        project,
        timestamp=old_at,
        runner_id=str(uuid.uuid4()),
    )

    first = await get_messages_noise_prefs_row(
        db_session,
        TEST_USER_ID,
        project.id,
    )
    assert first is not None
    assert first.prefs[WORKBENCH_STATE_KEY]["interaction_mode"] == "continuous"
    await db_session.commit()

    Session = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as other_session:
        row = await get_messages_noise_prefs_row(
            other_session,
            TEST_USER_ID,
            project.id,
            for_update=True,
        )
        assert row is not None
        prefs = dict(row.prefs or {})
        workbench = dict(prefs[WORKBENCH_STATE_KEY])
        workbench["interaction_mode"] = "chat"
        prefs[WORKBENCH_STATE_KEY] = workbench
        row.prefs = prefs
        other_session.add(row)
        await other_session.commit()

    refreshed = await get_messages_noise_prefs_row(
        db_session,
        TEST_USER_ID,
        project.id,
        for_update=True,
    )
    assert refreshed is not None
    assert refreshed is first
    assert refreshed.prefs[WORKBENCH_STATE_KEY]["interaction_mode"] == "chat"


# ── Runner health / trust telemetry (pure, no DB) ────────────────────────────


def _health_runner(**kwargs) -> LocalRunner:
    """In-memory LocalRunner for exercising _runner_health without a session."""
    now = _utcnow()
    caps = kwargs.pop("capabilities", {"task_types": "all", "max_concurrent": 2})
    return LocalRunner(
        project_id=uuid.uuid4(),
        token_hash="x",
        last_seen_at=kwargs.pop("last_seen_at", now - timedelta(seconds=30)),
        capabilities=caps,
        **kwargs,
    )


class TestRunnerHealthTrustTelemetry:
    """The 'connected but failing' (degraded) false-green guard is pure logic."""

    def test_healthy_live_runner_is_connected_not_degraded(self):
        h = _runner_health(
            _health_runner(
                last_successful_job_at=_utcnow() - timedelta(seconds=60),
                consecutive_job_failures=0,
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "disk_free_bytes": 50 * 1024**3,
                    "gpu_utilization_pct": 42,
                },
            )
        )
        assert h["device_status"] == "connected"
        assert h["degraded"] is False
        assert h["degraded_reason"] is None
        assert h["consecutive_job_failures"] == 0
        assert h["gpu_utilization_pct"] == 42
        assert h["disk_low"] is False
        assert h["seconds_since_last_success"] == 60

    def test_failure_streak_marks_runner_degraded_but_still_live(self):
        h = _runner_health(
            _health_runner(
                last_successful_job_at=_utcnow() - timedelta(seconds=60),
                consecutive_job_failures=3,
            )
        )
        assert h["device_status"] == "degraded"
        assert h["degraded"] is True
        assert h["degraded_reason"] == "job_failure_streak"
        # A degraded runner is still connected/live — slot accounting is unaffected.
        assert h["is_live"] is True

    def test_recent_success_resets_to_ready(self):
        h = _runner_health(
            _health_runner(
                last_successful_job_at=_utcnow(),
                consecutive_job_failures=0,
            )
        )
        assert h["device_status"] == "connected"
        assert h["degraded"] is False

    def test_stale_success_while_working_is_degraded(self):
        h = _runner_health(
            _health_runner(
                last_successful_job_at=_utcnow() - timedelta(seconds=4000),
                consecutive_job_failures=1,
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "active_task_ids": ["t1"],
                },
            )
        )
        assert h["device_status"] == "degraded"
        assert h["degraded_reason"] == "success_stale"

    def test_fresh_runner_on_first_job_is_not_degraded(self):
        # Active work, never succeeded, zero failures — legitimately churning,
        # must NOT be mislabelled as failing.
        h = _runner_health(
            _health_runner(
                last_successful_job_at=None,
                consecutive_job_failures=0,
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "active_task_ids": ["t1"],
                },
            )
        )
        assert h["device_status"] == "connected"
        assert h["degraded"] is False

    def test_failure_streak_on_stale_runner_stays_stale_not_degraded(self):
        # Degraded is only computed for a LIVE runner; a stale heartbeat wins.
        h = _runner_health(
            _health_runner(
                last_seen_at=_utcnow() - timedelta(minutes=9),
                last_successful_job_at=None,
                consecutive_job_failures=5,
            )
        )
        assert h["device_status"] == "stale"
        assert h["degraded"] is False

    def test_low_disk_flagged_without_degrading(self):
        h = _runner_health(
            _health_runner(
                consecutive_job_failures=0,
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "disk_free_bytes": 1 * 1024**3,
                },
            )
        )
        assert h["disk_low"] is True
        assert h["disk_free_bytes"] == 1 * 1024**3
        assert h["degraded"] is False


def test_parse_heartbeat_timestamp_tolerates_bad_input():
    assert _parse_heartbeat_timestamp(None) is None
    assert _parse_heartbeat_timestamp("") is None
    assert _parse_heartbeat_timestamp("not-a-date") is None
    parsed = _parse_heartbeat_timestamp("2026-07-02T10:00:00Z")
    assert parsed is not None and parsed.tzinfo is None  # normalized to naive UTC
    # Offset is normalized to UTC (12:00+02:00 -> 10:00 naive UTC).
    assert _parse_heartbeat_timestamp("2026-07-02T12:00:00+02:00") == parsed


def _snapshot(caps: dict) -> "orch.RunnerDispatchSnapshot":
    """A minimal dispatch snapshot carrying just the capabilities under test."""
    return orch.RunnerDispatchSnapshot(
        id=uuid.uuid4(), project_id=uuid.uuid4(), capabilities=caps
    )


def test_runner_handles_task_type_intersects_operator_policy_and_runner_floor():
    """`_runner_handles_task_type` intersects the operator policy CEILING with the
    runner-declared agent-capability FLOOR (`supported_agent_task_types`).

    A docker-only runner declares only the read-only agent slice; a write/heavy
    agent type must NOT dispatch there even though the operator policy is "all".
    """
    read_only = ["research", "plan", "review", "design"]

    # Runner declared the read-only slice under an "all" operator policy.
    docker_only = _snapshot(
        {"task_types": "all", "supported_agent_task_types": read_only}
    )
    # A read-only agent type is in the floor → handled.
    assert orch._runner_handles_task_type(docker_only, "research") is True
    # A WRITE/heavy agent type is NOT in the floor → declined (the whole point).
    assert orch._runner_handles_task_type(docker_only, "create") is False
    assert orch._runner_handles_task_type(docker_only, "smoketest") is False

    # compute_experiment is a COMPUTE-class task (different backend) → never gated
    # by the agent-only floor, so a docker-only runner still handles it.
    assert orch._runner_handles_task_type(docker_only, "compute_experiment") is True

    # Absent floor (older runner / bwrap box) → no extra restriction: any type the
    # operator policy allows is handled.
    no_floor = _snapshot({"task_types": "all"})
    assert orch._runner_handles_task_type(no_floor, "create") is True
    assert orch._runner_handles_task_type(no_floor, "research") is True
    # Explicit None floor behaves identically to absent.
    null_floor = _snapshot(
        {"task_types": "all", "supported_agent_task_types": None}
    )
    assert orch._runner_handles_task_type(null_floor, "create") is True

    # Operator policy remains the CEILING: a type outside the operator allowlist is
    # declined even if the runner floor would permit it.
    policy_limited = _snapshot(
        {"task_types": ["research"], "supported_agent_task_types": read_only}
    )
    assert orch._runner_handles_task_type(policy_limited, "research") is True
    assert orch._runner_handles_task_type(policy_limited, "review") is False


# ── Admin endpoints ──────────────────────────────────────────────────────────


class TestAdminRunnerEndpoints:
    @pytest.mark.asyncio
    async def test_register_returns_token_once_and_stores_only_hash(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={"hostname": "gpu-box", "capabilities": {"has_gpu": True}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "runner_id" in body and "token" in body
        token = body["token"]

        # Only the hash is persisted — plaintext token appears nowhere in the row.
        from sqlmodel import select

        row = (
            await db_session.execute(
                select(LocalRunner).where(
                    LocalRunner.id == uuid.UUID(body["runner_id"])
                )
            )
        ).scalars().first()
        assert row is not None
        assert row.token_hash == auth_mod.hash_runner_token(token)
        assert row.token_hash != token
        # Caller-supplied capability merges over defaults.
        assert row.capabilities["has_gpu"] is True
        assert row.capabilities["task_types"] == "all"

    @pytest.mark.asyncio
    async def test_register_strategic_local_preset_limits_runner_to_strategic_tasks(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={
                "hostname": "local-strategic",
                "preset": "strategic_local",
                "capabilities": {"task_types": "all", "max_concurrent": 9},
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        row = (
            await db_session.execute(
                select(LocalRunner).where(
                    LocalRunner.id == uuid.UUID(body["runner_id"])
                )
            )
        ).scalars().first()
        assert row is not None
        assert row.capabilities["runner_preset"] == "strategic_local"
        assert row.capabilities["workload"] == "strategic"
        assert row.capabilities["task_types"] == sorted(STRATEGIC_TASK_TYPES)
        assert row.capabilities["max_concurrent"] == 1

    @pytest.mark.asyncio
    async def test_register_emits_workbench_runner_refresh(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)

        with patch(
            "app.routers.admin_runners.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.post(
                f"/api/admin/projects/{project.id}/runners/register",
                json={"hostname": "local-strategic", "preset": "strategic_local"},
            )

        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "registered"
        assert "strategic_session" in emit.await_args.kwargs["refetch_surfaces"]
        assert "runtime" in emit.await_args.kwargs["refetch_surfaces"]
        assert emit.await_args.kwargs["summary"]["id"] == resp.json()["runner_id"]
        assert emit.await_args.kwargs["summary"]["preset"] == "strategic_local"

    @pytest.mark.asyncio
    async def test_register_rejects_malformed_capabilities(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        bad_payloads = [
            {"capabilities": {"task_types": ["unknown_type"]}},
            {"capabilities": {"task_types": []}},
            {"capabilities": {"max_concurrent": 0}},
            {"capabilities": {"max_concurrent": True}},
            {"capabilities": {"max_concurrent": 1.5}},
            {"capabilities": {"max_concurrent": "1.5"}},
            {"capabilities": {"has_gpu": "yes"}},
        ]
        for payload in bad_payloads:
            resp = await client.post(
                f"/api/admin/projects/{project.id}/runners/register",
                json=payload,
            )
            assert resp.status_code == 422, payload

    @pytest.mark.asyncio
    async def test_list_excludes_deleted(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        r1, _ = await _make_runner(db_session, project.id, hostname="a")
        r2, _ = await _make_runner(db_session, project.id, hostname="b", status="deleted")

        resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert resp.status_code == 200
        ids = {r["id"] for r in resp.json()}
        assert str(r1.id) in ids
        assert str(r2.id) not in ids

    @pytest.mark.asyncio
    async def test_list_returns_backend_computed_dispatch_health(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        live_runner, _ = await _make_runner(
            db_session,
            project.id,
            hostname="live",
            last_seen_at=_utcnow() - timedelta(seconds=30),
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 3,
                "active_task_ids": ["task-a"],
            },
        )
        stale_runner, _ = await _make_runner(
            db_session,
            project.id,
            hostname="stale",
            last_seen_at=_utcnow() - timedelta(minutes=9),
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        offline_runner, _ = await _make_runner(
            db_session,
            project.id,
            hostname="offline",
            status="inactive",
            last_seen_at=_utcnow() - timedelta(seconds=30),
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert resp.status_code == 200
        by_id = {row["id"]: row for row in resp.json()}

        live_health = by_id[str(live_runner.id)]["health"]
        assert live_health["device_status"] == "connected"
        assert live_health["device_status_reason"] is None
        assert live_health["is_live"] is True
        assert live_health["active_task_count"] == 1
        assert live_health["max_concurrent"] == 3
        assert live_health["available_slots"] == 2
        assert live_health["stale_reason"] is None

        stale_health = by_id[str(stale_runner.id)]["health"]
        assert stale_health["device_status"] == "stale"
        assert stale_health["device_status_reason"] == "heartbeat_stale"
        assert stale_health["is_live"] is False
        assert stale_health["stale_reason"] == "heartbeat_stale"
        assert stale_health["liveness_window_seconds"] == 300

        offline_health = by_id[str(offline_runner.id)]["health"]
        assert offline_health["device_status"] == "offline"
        assert offline_health["device_status_reason"] == "inactive"
        assert offline_health["is_live"] is False
        assert offline_health["stale_reason"] == "inactive"

    @pytest.mark.asyncio
    async def test_rotate_runner_token_replaces_hash_and_invalidates_old_token(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, old_token = await _make_runner(db_session, project.id)
        old_hash = runner.token_hash

        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/rotate-token"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["runner_id"] == str(runner.id)
        new_token = body["token"]
        assert new_token
        assert new_token != old_token

        await db_session.refresh(runner)
        assert runner.token_hash == auth_mod.hash_runner_token(new_token)
        assert runner.token_hash != old_hash
        assert runner.token_hash != new_token
        assert runner.token_hash != old_token

        assert await auth_mod.verify_runner_token(old_token, db_session) is None
        resolved = await auth_mod.verify_runner_token(new_token, db_session)
        assert resolved is not None
        assert resolved.id == runner.id

        old_heartbeat = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert old_heartbeat.status_code == 401
        new_heartbeat = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": ["after-rotation"]},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert new_heartbeat.status_code == 200
        await db_session.refresh(runner)
        assert runner.capabilities["active_task_ids"] == ["after-rotation"]

    @pytest.mark.asyncio
    async def test_rotate_runner_token_rejects_missing_deleted_cross_project_and_non_owner(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, old_token = await _make_runner(db_session, project.id)
        deleted_runner, deleted_token = await _make_runner(
            db_session, project.id, status="deleted"
        )
        other_project = await _make_project(db_session, name="Other Runner Project")
        non_owner_project = await _make_project(
            db_session,
            owner_id="other-owner",
            name="Member Only Runner Project",
        )
        db_session.add(
            ProjectMember(
                project_id=non_owner_project.id,
                user_id=TEST_USER_ID,
                role="member",
            )
        )
        non_owner_runner, non_owner_token = await _make_runner(
            db_session, non_owner_project.id
        )
        await db_session.commit()

        missing = await client.post(
            f"/api/admin/projects/{project.id}/runners/{uuid.uuid4()}/rotate-token"
        )
        assert missing.status_code == 404

        deleted = await client.post(
            f"/api/admin/projects/{project.id}/runners/{deleted_runner.id}/rotate-token"
        )
        assert deleted.status_code == 404

        cross_project = await client.post(
            f"/api/admin/projects/{other_project.id}/runners/{runner.id}/rotate-token"
        )
        assert cross_project.status_code == 404

        non_owner = await client.post(
            f"/api/admin/projects/{non_owner_project.id}/runners/{non_owner_runner.id}/rotate-token"
        )
        assert non_owner.status_code == 403

        await db_session.refresh(runner)
        await db_session.refresh(deleted_runner)
        await db_session.refresh(non_owner_runner)
        assert runner.token_hash == auth_mod.hash_runner_token(old_token)
        assert deleted_runner.token_hash == auth_mod.hash_runner_token(deleted_token)
        assert non_owner_runner.token_hash == auth_mod.hash_runner_token(
            non_owner_token
        )

    @pytest.mark.asyncio
    async def test_delete_marks_deleted(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        resp = await client.delete(
            f"/api/admin/projects/{project.id}/runners/{runner.id}"
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.status == "deleted"

        # Deleting again 404s.
        resp2 = await client.delete(
            f"/api/admin/projects/{project.id}/runners/{runner.id}"
        )
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_clears_preferred_runner_workbench_state(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import (
            STRATEGIC_LOCAL_CONTINUATION_KEY,
            WORKBENCH_STATE_KEY,
        )

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        now = _utcnow().isoformat()
        db_session.add(
            MessagesNoisePrefs(
                user_id=TEST_USER_ID,
                project_id=project.id,
                prefs={
                    WORKBENCH_STATE_KEY: {
                        "version": 1,
                        "interaction_mode": "continuous",
                        "active_surface": "runtime",
                        "selected_refs": [],
                        "preferred_runner_id": str(runner.id),
                    }
                },
            )
        )
        project.vision = {
            **(project.vision or {}),
            STRATEGIC_LOCAL_CONTINUATION_KEY: {
                "version": 1,
                "active": True,
                "operator_user_id": TEST_USER_ID,
                "updated_at": now,
                "last_seen_at": now,
                "preferred_runner_id": str(runner.id),
            },
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/runners/{runner.id}"
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            "preferred_runner_id"
            not in prefs_row.prefs[WORKBENCH_STATE_KEY]
        )
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["active"] is True
        assert "preferred_runner_id" not in marker

    @pytest.mark.asyncio
    async def test_delete_releases_unpulled_runner_pins(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        unpulled = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
            started_at=None,
        )
        pulled = Task(
            project_id=project.id,
            payloads=[{"type": "review"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
            started_at=_utcnow(),
        )
        db_session.add(unpulled)
        db_session.add(pulled)
        await db_session.commit()

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/runners/{runner.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["released_unpulled_pins"] == 1

        await db_session.refresh(unpulled)
        await db_session.refresh(pulled)
        assert unpulled.status == TaskStatus.QUEUED
        assert unpulled.vm_instance_id is None
        assert "runner_target_id" not in (unpulled.context or {})
        assert pulled.status == TaskStatus.PROVISIONING
        assert pulled.vm_instance_id == f"runner:{runner.id}"

    @pytest.mark.asyncio
    async def test_delete_emits_workbench_runner_refresh(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)

        with patch(
            "app.routers.admin_runners.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.delete(
                f"/api/admin/projects/{project.id}/runners/{runner.id}"
            )

        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.args[0] == project.id
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "deleted"
        assert "strategic_session" in emit.await_args.kwargs["refetch_surfaces"]
        assert "runtime" in emit.await_args.kwargs["refetch_surfaces"]
        assert emit.await_args.kwargs["summary"] == {
            "id": str(runner.id),
            "status": "deleted",
            "released_unpulled_pins": 0,
        }

    # ── Capabilities update (PATCH) ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_max_concurrent(
        self, client: AsyncClient, db_session
    ):
        """Normal bump: register with max_concurrent=2, PATCH to 4 → response shows 4."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["capabilities"]["max_concurrent"] == 4

        await db_session.refresh(runner)
        assert runner.capabilities["max_concurrent"] == 4

    @pytest.mark.asyncio
    async def test_update_rejects_out_of_range(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)

        resp_0 = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 0},
        )
        assert resp_0.status_code == 422

        resp_65 = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 65},
        )
        assert resp_65.status_code == 422

    @pytest.mark.asyncio
    async def test_update_strategic_local_pins_max_to_one(
        self, client: AsyncClient, db_session
    ):
        """A strategic_local runner clamps max_concurrent to 1 regardless of what the
        operator sends — the same invariant enforced at registration."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={
                "hostname": "strategic-clamp",
                "preset": "strategic_local",
            },
        )
        assert resp.status_code == 200
        runner_id = uuid.UUID(resp.json()["runner_id"])

        # PATCH with max_concurrent=4 — must be clamped to 1.
        patch = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner_id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert patch.status_code == 200
        assert patch.json()["capabilities"]["max_concurrent"] == 1

    @pytest.mark.asyncio
    async def test_update_preserves_runner_reported_fields(
        self, client: AsyncClient, db_session
    ):
        """Runner-reported hardware fields survive a capabilities PATCH."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 2,
            },
        )

        # First, Heartbeat to populate runner-reported GPU fields.
        hb = await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "has_gpu": True,
                "gpu_count": 2,
                "gpu_names": ["Tesla T4", "Tesla T4"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert hb.status_code == 200

        # Now PATCH max_concurrent.
        patch = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 6},
        )
        assert patch.status_code == 200
        caps = patch.json()["capabilities"]
        assert caps["max_concurrent"] == 6
        assert caps["has_gpu"] is True
        assert caps["gpu_count"] == 2
        assert caps["gpu_names"] == ["Tesla T4", "Tesla T4"]

    @pytest.mark.asyncio
    async def test_update_preserves_operator_fields_on_partial_patch(
        self, client: AsyncClient, db_session
    ):
        """PATCH only max_concurrent leaves task_types untouched (and vice versa)."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["create", "fix"],
                "has_gpu": False,
                "max_concurrent": 2,
            },
        )

        # PATCH only max_concurrent — task_types must survive.
        only_max = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 8},
        )
        assert only_max.status_code == 200
        caps = only_max.json()["capabilities"]
        assert caps["max_concurrent"] == 8
        assert caps["task_types"] == ["create", "fix"]

        # PATCH only task_types — max_concurrent must survive.
        only_tt = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"task_types": ["review"]},
        )
        assert only_tt.status_code == 200
        caps2 = only_tt.json()["capabilities"]
        assert caps2["task_types"] == ["review"]
        assert caps2["max_concurrent"] == 8

    @pytest.mark.asyncio
    async def test_update_rejects_non_owner(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)

        non_owner_project = await _make_project(
            db_session, owner_id="other-owner", name="Member Only Runner Cap Project"
        )
        db_session.add(
            ProjectMember(
                project_id=non_owner_project.id,
                user_id=TEST_USER_ID,
                role="member",
            )
        )
        non_owner_runner, _ = await _make_runner(
            db_session, non_owner_project.id
        )
        await db_session.commit()

        non_owner = await client.patch(
            f"/api/admin/projects/{non_owner_project.id}/runners/{non_owner_runner.id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert non_owner.status_code == 403

    @pytest.mark.asyncio
    async def test_update_deleted_runner_404(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id, status="deleted")

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_missing_runner_404(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        missing_id = uuid.uuid4()
        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{missing_id}/capabilities",
            json={"max_concurrent": 4},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_emits_workbench_refetch(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )

        with patch(
            "app.routers.admin_runners.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.patch(
                f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
                json={"max_concurrent": 4},
            )

        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.args[0] == project.id
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "updated"
        assert "strategic_session" in emit.await_args.kwargs["refetch_surfaces"]
        assert "runtime" in emit.await_args.kwargs["refetch_surfaces"]
        assert emit.await_args.kwargs["summary"] == {
            "id": str(runner.id),
            "status": runner.status,
            "hostname": runner.hostname,
        }


# ── Runner polling/heartbeat endpoints ───────────────────────────────────────


class TestRunnerEndpoints:
    @pytest.mark.asyncio
    async def test_next_task_returns_pinned_task_and_stays_provisioning(
        self, client: AsyncClient, db_session
    ):
        """next-task returns the JWT but does NOT transition to IN_PROGRESS.

        The PROVISIONING → IN_PROGRESS transition is the agent's job via
        /api/agent/claim, so the task must still be in PROVISIONING after
        next-task returns.  Transitioning here would break the claim path.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        _seed_pool_account(TEST_USER_ID)
        try:

            task = Task(
                project_id=project.id,
                payloads=[{"type": "create", "title": "do a thing"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["task_id"] == str(task.id)
            assert body["task_type"] == "create"
            assert body["agent_token"]
            assert body["vm_instance_id"] == f"runner:{runner.id}"
            assert body["payload"] == [{"type": "create", "title": "do a thing"}]

            # The minted JWT is task-scoped to this runner's vm id.
            claims = auth_mod.verify_agent_jwt(body["agent_token"])
            assert claims["sub"] == str(task.id)
            assert claims["vm_instance_id"] == f"runner:{runner.id}"

            # Task MUST still be in PROVISIONING — claim (not next-task) does the
            # PROVISIONING → IN_PROGRESS transition.  started_at is set by next-task
            # as a dispatch marker so re-polls skip this task until claim completes.
            await db_session.refresh(task)
            assert task.status == TaskStatus.PROVISIONING
            assert task.started_at is not None

            # Verify the response includes pooled credential info.
            credentials = body.get("credentials")
            assert isinstance(credentials, dict)
            assert credentials["env"]["PROVIDER"] == "anthropic"
            assert credentials["env"]["AUTH_MODE"] == "oauth"
            assert credentials["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-test-secret-token"
            assert isinstance(credentials["files"], list)
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("task_type", "output_kind"),
        [
            ("design_doc_update", "artifact"),
            ("strategic_review", "knowledge"),
        ],
    )
    async def test_next_task_returns_strategic_capability_slice(
        self, client: AsyncClient, db_session, task_type: str, output_kind: str
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        _seed_pool_account(TEST_USER_ID)
        try:

            task = Task(
                project_id=project.id,
                payloads=[{"type": task_type}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["task_type"] == task_type
            capability = body["capability_slice"]
            assert capability["id"] == task_type
            assert capability["agent_mode"] == "read_only"
            assert capability["run_shape"] == "single_agent"
            assert capability["output_kind"] == output_kind

            claims = auth_mod.verify_agent_jwt(body["agent_token"])
            assert claims["task_type"] == task_type
            assert claims["project_id"] == str(project.id)
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    async def test_next_task_repoll_returns_204_after_dispatch(
        self, client: AsyncClient, db_session
    ):
        """Re-poll after dispatch returns 204 — no duplicate dispatch.

        The runner loop re-polls immediately when active_count < max_concurrent.
        After next-task dispatches a task (setting started_at), a second
        sequential poll must return 204 so a second agent is not spawned for
        the same task while the first agent is booting and calling /api/agent/claim.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        _seed_pool_account(TEST_USER_ID)
        try:

            task = Task(
                project_id=project.id,
                payloads=[{"type": "create", "title": "no double dispatch"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            # First poll: dispatched.
            resp1 = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp1.status_code == 200
            assert resp1.json()["task_id"] == str(task.id)

            # Verify started_at is now set (dispatch marker).
            await db_session.refresh(task)
            assert task.started_at is not None
            assert task.status == TaskStatus.PROVISIONING

            # Second poll while task is still PROVISIONING (agent hasn't claimed yet):
            # must return 204 — no duplicate dispatch.
            resp2 = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 204
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    async def test_next_task_returns_credentials_when_pool_account_resolved(
        self, client: AsyncClient, db_session
    ):
        """next-task returns llm_env and llm_files when the account pool
        resolves an LLM account for the task, and stamps the task context with
        llm_account_id / llm_provider / llm_auth_mode / llm_model."""
        from app.max_plan_pool import LLMSelectionResult

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task_instance = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task_instance)
        await db_session.commit()

        mock_selection = LLMSelectionResult(
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            quality_tier="opus",
            account={
                "id": "acct-001",
                "oauth_token": "sk-ant-test-token",
                "runtime_secret": "sk-ant-test-token",
                "claude_config_json": None,
                "provider_metadata": {},
            },
            settings={},
        )

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=mock_selection),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "credentials" in body
        creds = body["credentials"]
        assert creds is not None
        assert "env" in creds
        assert "files" in creds
        env = creds["env"]
        assert env.get("PROVIDER") == "anthropic"
        assert env.get("AUTH_MODE") == "oauth"
        # Provider-specific secret should be present in the llm_env.
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-test-token"

        # Verify task context was stamped for cost attribution.
        await db_session.refresh(task_instance)
        assert task_instance.llm_account_id == "acct-001"
        assert task_instance.context.get("llm_account_id") == "acct-001"
        assert task_instance.context.get("llm_provider") == "anthropic"
        assert task_instance.context.get("llm_auth_mode") == "oauth"
        assert task_instance.context.get("llm_model") == "claude-opus-4-8"
        assert task_instance.status == TaskStatus.PROVISIONING  # unchanged

    @pytest.mark.asyncio
    async def test_next_task_returns_null_credentials_when_no_pool_account(
        self, client: AsyncClient, db_session
    ):
        """next-task returns 204 when no LLM account is resolved — the task stays
        in PROVISIONING for the next poll cycle, and no context fields are stamped."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task_instance = Task(
            project_id=project.id,
            payloads=[{"type": "create", "title": "no pool account"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task_instance)
        await db_session.commit()

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=None),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # When no LLM account is resolved, the handler returns 503
        # with pool_exhausted=True, rolling back the dispatch marker
        # so the task stays claimable by a future poll cycle.
        assert resp.status_code == 503
        assert resp.json() == {"pool_exhausted": True}

        # Task context was NOT stamped since select_llm_runtime_for_task returned None.
        await db_session.refresh(task_instance)
        assert task_instance.llm_account_id is None  # not stamped
        assert task_instance.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_next_task_rolls_back_dispatch_on_credential_exception(
        self, client: AsyncClient, db_session
    ):
        """When an unexpected exception occurs during credential resolution,
        next-task must roll back the dispatch marker (started_at = None) and
        return 503 with pool_exhausted=True so the task stays claimable rather
        than being dispatched without credentials."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task_instance = Task(
            project_id=project.id,
            payloads=[{"type": "create", "title": "cred resolution exception"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task_instance)
        await db_session.commit()

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=RuntimeError("pool connection lost")),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # Dispatch must be rolled back: 503 with pool_exhausted=True so the
        # runner retries rather than receiving a task it can't execute.
        assert resp.status_code == 503
        assert resp.json() == {"pool_exhausted": True}

        # Task context was NOT stamped and dispatch marker was rolled back.
        await db_session.refresh(task_instance)
        assert task_instance.llm_account_id is None
        assert task_instance.status == TaskStatus.PROVISIONING  # unchanged
        # started_at must be None — task is still claimable.
        assert task_instance.started_at is None

    @pytest.mark.asyncio
    async def test_agent_claim_after_runner_dispatch_transitions_to_in_progress(
        self, client: AsyncClient, db_session
    ):
        """Full next-task → /api/agent/claim flow works end-to-end.

        The runner polls next-task, gets a JWT, spawns the agent binary, which
        calls /api/agent/claim.  That endpoint must find the task still in
        PROVISIONING and transition it to IN_PROGRESS.
        """
        project = await _make_project(db_session)
        runner, runner_token = await _make_runner(db_session, project.id)
        _seed_pool_account(TEST_USER_ID)
        try:

            task = Task(
                project_id=project.id,
                payloads=[{"type": "create", "title": "runner claim test"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            # Step 1: runner polls next-task.
            next_resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {runner_token}"},
            )
            assert next_resp.status_code == 200
            agent_token = next_resp.json()["agent_token"]

            # Step 2: agent binary calls /api/agent/claim with the minted JWT.
            claim_resp = await client.post(
                "/api/agent/claim",
                headers={"x-agent-token": agent_token},
            )
            assert claim_resp.status_code == 200, claim_resp.text

            # Step 3: task is now IN_PROGRESS with last_heartbeat set.
            await db_session.refresh(task)
            assert task.status == TaskStatus.IN_PROGRESS
            assert task.last_heartbeat is not None
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    async def test_next_task_204_when_nothing_pinned(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_next_task_401_with_bad_token(self, client: AsyncClient, db_session):
        resp = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_next_task_rejects_foreign_project_pinned_task(
        self, client: AsyncClient, db_session
    ):
        """A runner registered to project A must NOT see tasks from project B
        even if those tasks are pinned to this runner's ID (defense-in-depth)."""
        project_a = await _make_project(db_session, name="Project A")
        project_b = await _make_project(db_session, name="Project B")
        runner_a, token_a = await _make_runner(db_session, project_a.id)

        # Create a task in project B pinned to runner_a's vm identity
        # (simulates a misconfiguration or race where project B's task
        # somehow targets this runner).
        foreign_task = Task(
            project_id=project_b.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner_a.id}",
            context={"runner_target_id": str(runner_a.id)},
        )
        db_session.add(foreign_task)
        await db_session.commit()

        resp = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        # Must not claim the cross-project task — 204 (no task) expected.
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_next_task_includes_pooled_credential_in_response(
        self, client: AsyncClient, db_session
    ):
        """The /next-task response must include provider, auth_mode, model,
        llm_env (with the resolved credential env vars), and llm_files from
        the pool-selected account — NOT from any static local env var."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        # Seed a DeepSeek account so the response proves it comes from the pool,
        # not from a statically-configured Anthropic credential.
        _seed_pool_account(
            TEST_USER_ID,
            provider="deepseek",
            auth_mode="api_key",
            runtime_secret="sk-deepseek-pool-assigned-key",
        )
        try:
            task = Task(
                project_id=project.id,
                payloads=[{"type": "create", "title": "pool credential test"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            body = resp.json()

            # Credential metadata and secrets come from the pool, not local env.
            credentials = body["credentials"]
            assert credentials["env"]["PROVIDER"] == "deepseek"
            assert credentials["env"]["AUTH_MODE"] == "api_key"
            assert credentials["env"]["DEEPSEEK_API_KEY"] == "sk-deepseek-pool-assigned-key"
            # files is present even when the provider only needs an API key.
            assert isinstance(credentials["files"], list)

            # The task context must carry the resolved account identity so the
            # completion handler can release the account slot.
            await db_session.refresh(task)
            ctx = task.context or {}
            assert ctx.get("llm_provider") == "deepseek"
            assert ctx.get("llm_auth_mode") == "api_key"
            assert ctx.get("llm_account_id") is not None
            # Promoted column must also be populated (D2 over-fetch fix).
            assert task.llm_account_id is not None
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    async def test_next_task_returns_503_when_pool_exhausted(
        self, client: AsyncClient, db_session
    ):
        """When no eligible account exists in the pool, /next-task must return
        503 with pool_exhausted=True and leave started_at unset so the task
        remains claimable once an account frees up.  A 503 signals resource
        exhaustion distinct from "no tasks available" (204)."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        # Do NOT seed any account — the pool is empty.
        _clear_pool()
        try:
            task = Task(
                project_id=project.id,
                payloads=[{"type": "create", "title": "exhausted pool test"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner.id}",
                context={"runner_target_id": str(runner.id)},
            )
            db_session.add(task)
            await db_session.commit()

            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 503
            assert resp.json() == {"pool_exhausted": True}

            # started_at must NOT be set — the task was not dispatched.
            await db_session.refresh(task)
            assert task.started_at is None
            assert task.status == TaskStatus.PROVISIONING
        finally:
            _clear_pool()

    @pytest.mark.asyncio
    async def test_decline_task_releases_back_to_queued(
        self, client: AsyncClient, db_session
    ):
        """Runner-declined task moves from PROVISIONING back to QUEUED.

        Edge cases covered inline: wrong runner → no-op, wrong status → no-op."""
        project = await _make_project(db_session)
        r1, t1 = await _make_runner(db_session, project.id)
        r2, _ = await _make_runner(db_session, project.id, hostname="other")

        # ── Happy path: decline own PROVISIONING task ──
        task1 = Task(
            project_id=project.id, payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{r1.id}",
            context={"runner_target_id": str(r1.id)},
            started_at=_utcnow(),
        )
        # Pinned to r2 — r1 can't release it.
        task2 = Task(
            project_id=project.id, payloads=[{"type": "review"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{r2.id}",
            context={"runner_target_id": str(r2.id)},
            started_at=_utcnow(),
        )
        # Wrong status — decline only releases PROVISIONING.
        task3 = Task(
            project_id=project.id, payloads=[{"type": "fix"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id=f"runner:{r1.id}",
            context={"runner_target_id": str(r1.id)},
            started_at=_utcnow(),
        )
        db_session.add_all([task1, task2, task3])
        await db_session.commit()

        resp = await client.post(
            "/api/runner/decline-task",
            json={"task_id": str(task1.id)},
            headers={"Authorization": f"Bearer {t1}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        await db_session.refresh(task1)
        assert task1.status == TaskStatus.QUEUED
        assert task1.vm_instance_id is None
        assert task1.vm_provisioned_at is None
        assert task1.started_at is None
        assert "runner_target_id" not in (task1.context or {})

        # task2 pinned to r2 — NOT released by r1's decline.
        await db_session.refresh(task2)
        assert task2.status == TaskStatus.PROVISIONING
        assert task2.vm_instance_id == f"runner:{r2.id}"

        # task3 in IN_PROGRESS — NOT released by decline.
        await db_session.refresh(task3)
        assert task3.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_decline_task_releases_pool_account(
        self, client: AsyncClient, db_session
    ):
        """Declining a task releases the LLM pool account slot."""
        _clear_pool()
        acct = _seed_pool_account(active_tasks=1)
        acct_id = acct["id"]
        assert acct["active_tasks"] == 1  # pre-condition: slot is claimed

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task = Task(
            project_id=project.id, payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={
                "runner_target_id": str(runner.id),
                "llm_account_id": acct_id,
            },
            llm_account_id=acct_id,
            started_at=_utcnow(),
        )
        db_session.add(task)
        await db_session.commit()

        resp = await client.post(
            "/api/runner/decline-task",
            json={"task_id": str(task.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # Pool slot was released back.
        assert acct["active_tasks"] == 0

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.llm_account_id is None
        ctx = task.context or {}
        assert "llm_account_id" not in ctx
        assert "llm_provider" not in ctx

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_seen(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session, project.id, last_seen_at=_utcnow() - timedelta(hours=1)
        )
        old_seen = runner.last_seen_at
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": ["abc", "def"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.last_seen_at > old_seen
        assert runner.capabilities["active_task_ids"] == ["abc", "def"]

    @pytest.mark.asyncio
    async def test_heartbeat_persists_trust_telemetry(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        success_at = (_utcnow() - timedelta(minutes=1)).replace(microsecond=0)

        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "last_successful_job_at": success_at.isoformat(),
                "consecutive_job_failures": 4,
                "disk_free_bytes": 12345678,
                "gpu_utilization_pct": 77,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        # First-class columns.
        assert runner.last_successful_job_at == success_at
        assert runner.consecutive_job_failures == 4
        # Display-only telemetry rides on capabilities.
        assert runner.capabilities["disk_free_bytes"] == 12345678
        assert runner.capabilities["gpu_utilization_pct"] == 77

        # The runner is live but has a >= 3 failure streak -> degraded false-green.
        list_resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert list_resp.status_code == 200
        health = list_resp.json()[0]["health"]
        assert health["device_status"] == "degraded"
        assert health["degraded"] is True
        assert health["consecutive_job_failures"] == 4
        assert health["gpu_utilization_pct"] == 77

        # A subsequent success (failures reset to 0) clears the degraded state.
        resp2 = await client.post(
            "/api/runner/heartbeat",
            json={
                "last_successful_job_at": _utcnow().isoformat(),
                "consecutive_job_failures": 0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        list_resp2 = await client.get(f"/api/admin/projects/{project.id}/runners")
        health2 = list_resp2.json()[0]["health"]
        assert health2["device_status"] == "connected"
        assert health2["degraded"] is False

    @pytest.mark.asyncio
    async def test_heartbeat_persists_isolation_backends(
        self, client: AsyncClient, db_session
    ):
        # The runner reports its isolation-backend inventory + selected backends +
        # version; all ride on capabilities (version is a first-class column) so the
        # fleet panel can show per-device backend availability.
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        inventory = {
            "bwrap_ok": True,
            "userns_ok": True,
            "docker_usable": False,
            "podman_usable": False,
            "apptainer_ok": True,
            "enroot_ok": False,
            "systemd_run_ok": True,
        }
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "version": "v22-15-gabc1234-dirty",
                "isolation_backends": inventory,
                "agent_backend": "bwrap",
                "compute_backend": "bwrap",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.version == "v22-15-gabc1234-dirty"
        assert runner.capabilities["isolation_backends"] == inventory
        assert runner.capabilities["agent_backend"] == "bwrap"
        assert runner.capabilities["compute_backend"] == "bwrap"

    @pytest.mark.asyncio
    async def test_heartbeat_tolerates_missing_isolation_backends(
        self, client: AsyncClient, db_session
    ):
        # Old runners omit the isolation fields entirely — heartbeat must still
        # succeed and leave capabilities without those keys.
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert "isolation_backends" not in runner.capabilities
        assert "agent_backend" not in runner.capabilities
        assert "compute_backend" not in runner.capabilities

    @pytest.mark.asyncio
    async def test_heartbeat_tolerates_missing_trust_telemetry(
        self, client: AsyncClient, db_session
    ):
        # Old runners omit the trust fields entirely — heartbeat must still succeed
        # and leave the columns at their defaults.
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.last_successful_job_at is None
        assert runner.consecutive_job_failures == 0

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_pinned_strategic_continuation(
        self, client: AsyncClient, db_session
    ):
        """SAFETY (dead-man's switch): bare runner liveness must NOT renew the
        continuation marker — even when the marker is fresh, active, and pinned to
        the heartbeating Strategic-capable runner. Renewal is gated on operator
        interaction OR an applied strategic op (progress); an alive-but-idle local
        session must be reclaimable by the cloud cadence once its lease lapses."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        preferred_runner_id = str(runner.id)
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=preferred_runner_id,
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        workbench = prefs_row.prefs[WORKBENCH_STATE_KEY]
        # Freshness is UNCHANGED by the heartbeat — no liveness-driven renewal.
        assert workbench["last_interaction_at"] == old_at
        assert workbench["preferred_runner_id"] == preferred_runner_id
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at
        assert marker["updated_at"] == old_at
        assert marker["preferred_runner_id"] == preferred_runner_id

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_unpinned_strategic_continuation(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=str(runner.id),
            include_marker_runner=False,
            include_workbench_runner=False,
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            prefs_row.prefs[WORKBENCH_STATE_KEY]["last_interaction_at"]
            == old_at
        )
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at
        assert "preferred_runner_id" not in marker

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_mismatched_runner_continuation(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        other_runner, _other_token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=str(other_runner.id),
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            prefs_row.prefs[WORKBENCH_STATE_KEY]["last_interaction_at"]
            == old_at
        )
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at
        assert marker["preferred_runner_id"] == str(other_runner.id)

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_stale_strategic_continuation(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (
            _utcnow()
            - timedelta(seconds=STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS + 60)
        ).isoformat()
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=str(runner.id),
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at
        assert marker["updated_at"] == old_at

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_when_owner_left_continuous_mode(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            interaction_mode="chat",
            runner_id=str(runner.id),
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            prefs_row.prefs[WORKBENCH_STATE_KEY]["last_interaction_at"]
            == old_at
        )
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at

    @pytest.mark.asyncio
    async def test_heartbeat_skips_recent_strategic_continuation_renewal(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        recent_at = (_utcnow() - timedelta(seconds=30)).isoformat()
        preferred_runner_id = str(runner.id)
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=recent_at,
            runner_id=preferred_runner_id,
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            prefs_row.prefs[WORKBENCH_STATE_KEY]["last_interaction_at"]
            == recent_at
        )
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == recent_at
        assert marker["updated_at"] == recent_at

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_renew_continuation_for_non_strategic_runner(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["docs"],
                "max_concurrent": 1,
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        preferred_runner_id = str(runner.id)
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=preferred_runner_id,
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_recreate_continuation_after_continuous_mode(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            interaction_mode="chat",
            runner_id=str(runner.id),
            include_marker=False,
        )

        resp = await client.post(
            "/api/runner/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        await db_session.refresh(project)
        assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (project.vision or {})

    @pytest.mark.asyncio
    async def test_heartbeat_emits_workbench_runner_refresh(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        with patch(
            "app.routers.runner.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.post(
                "/api/runner/heartbeat",
                json={
                    "active_task_ids": ["task-1"],
                    "active_compute_job_ids": ["job-1"],
                    "agent_available": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.args[0] == runner.project_id
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "heartbeat"
        assert emit.await_args.kwargs["refetch_surfaces"] == (
            "strategic_session",
            "runtime",
        )
        assert emit.await_args.kwargs["summary"] == {
            "id": str(runner.id),
            "status": "active",
            "active_task_count": 1,
            "active_compute_job_count": 1,
            "agent_available": True,
        }

    @pytest.mark.asyncio
    async def test_heartbeat_emits_refetch_when_stale_runner_becomes_live(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=9),
            capabilities={
                "active_task_ids": ["task-1"],
                "active_compute_job_ids": ["job-1"],
                "agent_available": True,
            },
        )

        with patch(
            "app.routers.runner.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.post(
                "/api/runner/heartbeat",
                json={
                    "active_task_ids": ["task-1"],
                    "active_compute_job_ids": ["job-1"],
                    "agent_available": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.args[0] == runner.project_id
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "heartbeat"
        assert emit.await_args.kwargs["refetch_surfaces"] == (
            "strategic_session",
            "runtime",
        )
        assert emit.await_args.kwargs["summary"] == {
            "id": str(runner.id),
            "status": "active",
            "active_task_count": 1,
            "active_compute_job_count": 1,
            "agent_available": True,
        }

    @pytest.mark.asyncio
    async def test_heartbeat_skips_refetch_for_default_empty_active_work(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        with patch(
            "app.routers.runner.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.post(
                "/api/runner/heartbeat",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_persists_runner_reported_capabilities(
        self, client: AsyncClient, db_session
    ):
        """Runner-reported GPU capabilities are persisted; operator fields are preserved."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 4,
            },
        )

        # Heartbeat carries runner-detected GPU info.
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "active_compute_job_ids": ["job-1"],
                "agent_available": False,
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_names": ["Tesla T4"],
                "hostname": "worker-host",
                "version": "local-runner/1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)

        # Hardware fields are updated to runner-reported ground truth.
        assert runner.capabilities["has_gpu"] is True
        assert runner.capabilities["gpu_count"] == 1
        assert runner.capabilities["gpu_names"] == ["Tesla T4"]
        assert runner.capabilities["active_compute_job_ids"] == ["job-1"]
        assert runner.capabilities["agent_available"] is False
        assert runner.hostname == "worker-host"
        assert runner.version == "local-runner/1"

        # Operator-configured fields must NOT be clobbered.
        assert runner.capabilities["task_types"] == "all"
        assert runner.capabilities["max_concurrent"] == 4

        # Backward-compat: omitting GPU fields in a follow-up heartbeat must
        # leave has_gpu intact (old runner protocol still works).
        resp2 = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": ["x"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        await db_session.refresh(runner)
        # has_gpu persists from the previous heartbeat.
        assert runner.capabilities["has_gpu"] is True
        assert runner.capabilities["active_task_ids"] == ["x"]

    @pytest.mark.asyncio
    async def test_heartbeat_stores_runner_declared_supported_agent_task_types(
        self, client: AsyncClient, db_session
    ):
        """A docker-only runner DECLARES supported_agent_task_types; the handler
        stores it under its own caps key WITHOUT clobbering the operator-configured
        `task_types` ceiling, and tolerates its absence on older runners.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "max_concurrent": 4},
        )

        read_only_slice = ["design", "plan", "research", "review"]
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "agent_available": True,
                "supported_agent_task_types": read_only_slice,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        # Runner-declared capability stored under its OWN key.
        assert runner.capabilities["supported_agent_task_types"] == read_only_slice
        # Operator-configured CEILING must NOT be clobbered (the core invariant).
        assert runner.capabilities["task_types"] == "all"
        assert runner.capabilities["max_concurrent"] == 4

        # Backward-compat: omitting it in a later heartbeat must not wipe the stored
        # value (an older runner that never advertises it keeps heartbeating).
        resp2 = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": ["x"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        await db_session.refresh(runner)
        assert runner.capabilities["supported_agent_task_types"] == read_only_slice
        assert runner.capabilities["task_types"] == "all"

    @pytest.mark.asyncio
    async def test_heartbeat_rejects_malformed_boolean_capabilities(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        _, token = await _make_runner(db_session, project.id)

        for payload in (
            {"active_task_ids": [], "has_gpu": "yes"},
            {"active_task_ids": [], "agent_available": "false"},
        ):
            resp = await client.post(
                "/api/runner/heartbeat",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 422, payload

    @pytest.mark.asyncio
    async def test_heartbeat_reactivates_inactive_runner(
        self, client: AsyncClient, db_session
    ):
        """FIX B3(b): a successful heartbeat flips an inactive runner back to active.

        After the stale-runner sweep deactivates a slept host, the runner's next
        authenticated heartbeat is proof of liveness and must re-arm the row so
        dispatch (which filters on recent last_seen_at) can target it again.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            status="inactive",
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        assert runner.status == "inactive"

        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        # Re-armed to active and last_seen_at refreshed.
        assert runner.status == "active"
        assert runner.last_seen_at > _utcnow() - timedelta(minutes=1)

    @pytest.mark.asyncio
    async def test_heartbeat_survives_declined_continuation_renewal_race(
        self, client: AsyncClient, db_session
    ):
        """Regression: the with_vision_locked decline branch must not 500.

        When a concurrent heartbeat wins the renewal race, the in-lock
        ``_renew_marker`` declines and ``renewed_marker`` stays False. That
        branch must release the project row lock with a COMMIT, not a rollback:
        a rollback expires every ORM instance in the shared session (even under
        expire_on_commit=False), including the caller's ``runner`` row, so the
        heartbeat handler's subsequent ``runner.status`` read would trigger a
        sync lazy-load -> MissingGreenlet (HTTP 500).

        Patching ``with_vision_locked`` to a no-op forces the decline branch
        deterministically: every pre-lock guard passes (renewal IS due) but the
        marker is never renewed, reproducing the race without real concurrency.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
        # 2 minutes ago -> renewal IS due, so the helper reaches
        # with_vision_locked before it can decline.
        old_at = (_utcnow() - timedelta(minutes=2)).isoformat()
        preferred_runner_id = str(runner.id)
        await _seed_strategic_continuation(
            db_session,
            project,
            timestamp=old_at,
            runner_id=preferred_runner_id,
        )

        with patch(
            "app.workbench_state.with_vision_locked",
            new=AsyncMock(return_value=True),
        ):
            resp = await client.post(
                "/api/runner/heartbeat",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )

        # Before the fix this was a 500 (MissingGreenlet on the expired runner).
        assert resp.status_code == 200
        # Decline must still decline: the marker is left untouched.
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["last_seen_at"] == old_at
        assert marker["updated_at"] == old_at
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                    MessagesNoisePrefs.project_id == project.id,
                )
            )
        ).scalar_one()
        assert (
            prefs_row.prefs[WORKBENCH_STATE_KEY]["last_interaction_at"] == old_at
        )

    @pytest.mark.asyncio
    async def test_update_report_creates_alert_on_success(
        self, client: AsyncClient, db_session
    ):
        """POST /api/runner/update-report with status=success creates an INFO Alert."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session, project.id, hostname="runner-update-test"
        )

        resp = await client.post(
            "/api/runner/update-report",
            json={
                "status": "success",
                "old_version": "a" * 40,
                "new_version": "b" * 40,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

        # Verify an alert was created.
        rows = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "runner.self_update",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        alert = rows[0]
        assert alert.severity == AlertSeverity.INFO
        assert "succeeded" in alert.title
        assert runner.hostname in alert.title or str(runner.id)[:8] in alert.title
        assert "aaaaaaaaaa → bbbbbbbbbb" in alert.message

    @pytest.mark.asyncio
    async def test_update_report_creates_alert_on_failure(
        self, client: AsyncClient, db_session
    ):
        """POST /api/runner/update-report with status=failure creates a WARNING Alert."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        resp = await client.post(
            "/api/runner/update-report",
            json={"status": "failure"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

        rows = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "runner.self_update",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        alert = rows[0]
        assert alert.severity == AlertSeverity.WARNING
        assert "failed" in alert.title

    @pytest.mark.asyncio
    async def test_update_report_rejects_invalid_status(
        self, client: AsyncClient, db_session
    ):
        """POST /api/runner/update-report rejects unknown status."""
        project = await _make_project(db_session)
        _runner, token = await _make_runner(db_session, project.id)

        resp = await client.post(
            "/api/runner/update-report",
            json={"status": "unknown"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_report_requires_auth(
        self, client: AsyncClient, db_session
    ):
        """POST /api/runner/update-report returns 401 without a valid token."""
        resp = await client.post(
            "/api/runner/update-report",
            json={"status": "success"},
        )
        assert resp.status_code == 401

# ── Durable artifact upload endpoint (FIX M1, backend side) ───────────────────


def _build_multipart(archive: bytes, file_count: int, total_bytes: int):
    """Build a multipart/form-data body matching the runner client's wire shape."""
    boundary = "----test-boundary-deadbeef"
    crlf = b"\r\n"
    body = bytearray()
    for name, value in (("file_count", file_count), ("total_bytes", total_bytes)):
        body += b"--" + boundary.encode() + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode() + crlf
    body += b"--" + boundary.encode() + crlf
    body += b'Content-Disposition: form-data; name="archive"; filename="artifacts.tar.gz"' + crlf
    body += b"Content-Type: application/gzip" + crlf + crlf
    body += archive + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class TestMultipartParser:
    """Unit coverage for the dependency-free multipart parser used by the
    artifact endpoint (avoids requiring python-multipart, which FastAPI File/Form
    would otherwise pull in)."""

    def test_parses_file_and_scalar_fields(self):
        body, content_type = _build_multipart(b"\x1f\x8b\x08TARGZ", 3, 4096)
        fields, files = _parse_multipart_form(body, content_type)
        assert files["archive"] == b"\x1f\x8b\x08TARGZ"
        assert fields["file_count"] == b"3"
        assert fields["total_bytes"] == b"4096"

    def test_binary_content_preserved_exactly(self):
        raw = bytes(range(256)) * 4  # includes CRLF-looking byte sequences
        body, content_type = _build_multipart(raw, 1, len(raw))
        _fields, files = _parse_multipart_form(body, content_type)
        assert files["archive"] == raw

    def test_missing_boundary_returns_empty(self):
        fields, files = _parse_multipart_form(b"whatever", "application/json")
        assert fields == {} and files == {}


class TestArtifactUploadEndpoint:
    async def _make_runner_job(self, db_session, *, pinned_runner_id=None):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        pin = str(pinned_runner_id or runner.id)
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": pin},
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return project, runner, token, job

    @pytest.mark.asyncio
    async def test_upload_stores_artifact_and_links_metadata(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project, runner, token, job = await self._make_runner_job(db_session)

        captured = {}

        async def fake_store(session, j, archive_bytes, file_count, total_bytes):
            captured["archive_len"] = len(archive_bytes)
            captured["file_count"] = file_count
            captured["total_bytes"] = total_bytes
            return {
                "kind": "runner_artifact",
                "uri": "gs://bucket/compute-jobs/x/artifacts/bundle.tar.gz",
                "file_count": file_count,
                "total_bytes": total_bytes,
                "stored": True,
            }

        monkeypatch.setattr(
            compute_backends_mod, "store_runner_artifact", fake_store, raising=False
        )

        archive = b"\x1f\x8b\x08gzip-bytes"
        body, content_type = _build_multipart(archive, 2, 1234)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["archive_len"] == len(archive)
        assert captured["file_count"] == 2
        assert captured["total_bytes"] == 1234

        await db_session.refresh(job)
        stored = job.polling_metadata["runner_artifact"]
        assert stored["stored"] is True
        assert stored["uri"].startswith("gs://")

    @pytest.mark.asyncio
    async def test_upload_over_cap_returns_413(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project, runner, token, job = await self._make_runner_job(db_session)

        called = {"n": 0}

        async def fake_store(*a, **k):
            called["n"] += 1
            return {}

        monkeypatch.setattr(
            compute_backends_mod, "store_runner_artifact", fake_store, raising=False
        )

        from app.routers import runner as runner_router

        oversize = b"x" * (runner_router._MAX_RUNNER_ARTIFACT_BYTES + 1)
        body, content_type = _build_multipart(oversize, 1, len(oversize))
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        assert resp.status_code == 413
        assert called["n"] == 0  # store never invoked on cap breach

    @pytest.mark.asyncio
    async def test_upload_wrong_runner_404(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        # Job pinned to a DIFFERENT runner id than the caller's token.
        project, runner, token, job = await self._make_runner_job(
            db_session, pinned_runner_id=uuid.uuid4()
        )

        async def fake_store(*a, **k):
            raise AssertionError("store must not run for a non-owned job")

        monkeypatch.setattr(
            compute_backends_mod, "store_runner_artifact", fake_store, raising=False
        )

        body, content_type = _build_multipart(b"gz", 1, 2)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        assert resp.status_code == 404


# ── Orchestrator routing helpers ─────────────────────────────────────────────


class TestOrchestratorRunnerRouting:
    @pytest.mark.asyncio
    async def test_load_cache_filters_by_liveness_and_status(self, db_session):
        project = await _make_project(db_session)
        live, _ = await _make_runner(db_session, project.id, last_seen_at=_utcnow())
        await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),  # stale
        )
        await _make_runner(
            db_session, project.id, status="inactive", last_seen_at=_utcnow()
        )

        cache = await orch._load_active_runners_cache(db_session)
        ids = {r.id for r in cache.get(project.id, [])}
        assert ids == {live.id}

    @pytest.mark.asyncio
    async def test_loaded_runner_cache_survives_session_expiry(self, db_session):
        """Runner dispatch cache must not lazy-load LocalRunner after rollback/expiry.

        Production crash family: a cached LocalRunner ORM instance survived a
        dispatch-cycle transaction boundary, then runner selection read expired
        ``runner.capabilities`` and raised MissingGreenlet. The cache must carry
        value snapshots instead.
        """
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 2,
            },
        )
        project_id = project.id
        runner_id = runner.id

        cache = await orch._load_active_runners_cache(db_session)
        assert {r.id for r in cache.get(project_id, [])} == {runner_id}

        # Rollback does this in the live dispatch loop; expunge_all reproduces the
        # same hazard without forcing an unrelated DB error in the test.
        # (expire_all() is a no-op on SQLite because InstanceState._expire is
        # globally patched in conftest.py; expunge_all() detaches without _expire.)
        db_session.expunge_all()

        task = Task(
            project_id=project_id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        pinned = await orch._try_runner_dispatch(db_session, task, cache)

        assert pinned is True
        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING
        assert task.vm_instance_id == f"runner:{runner_id}"

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_pins_task(self, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        cache = {project.id: [runner]}

        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        pinned = await orch._try_runner_dispatch(db_session, task, cache)
        assert pinned is True
        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING
        assert task.vm_instance_id == f"runner:{runner.id}"
        assert task.context["runner_target_id"] == str(runner.id)
        assert task.vm_provisioned_at is not None

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_skips_when_no_runner(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(db_session, task, {}) is False
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_gpu_experiment_blocked_by_cpu_runner(
        self, db_session
    ):
        """A GPU compute_experiment must not be pinned to a non-GPU runner."""
        project = await _make_project(db_session)
        # Runner has no GPU.
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment", "accelerator": "T4"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert (
            await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}
            )
            is False
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_cpu_compute_experiment_routes_to_runner(
        self, db_session
    ):
        """A CPU compute_experiment (no accelerator) is routed normally."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert (
            await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}
            )
            is True
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_respects_capability_task_types(
        self, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": ["review"], "has_gpu": False},
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        # Runner only handles "review" — "create" is not eligible.
        assert (
            await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_strategic_local_runner_pins_only_strategic_task_types(
        self, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": sorted(STRATEGIC_TASK_TYPES),
                "has_gpu": False,
                "max_concurrent": 4,
            },
        )
        for task_type in sorted(STRATEGIC_TASK_TYPES):
            task = Task(
                project_id=project.id,
                payloads=[{"type": task_type}],
                status=TaskStatus.QUEUED,
            )
            db_session.add(task)
            await db_session.commit()
            assert await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}, {}
            )
            await db_session.refresh(task)
            assert task.vm_instance_id == f"runner:{runner.id}"
            assert task.context["runner_target_id"] == str(runner.id)

        for task_type in ("create", "research", "compute_experiment"):
            task = Task(
                project_id=project.id,
                payloads=[{"type": task_type}],
                status=TaskStatus.QUEUED,
            )
            db_session.add(task)
            await db_session.commit()
            assert await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}, {}
            ) is False
            await db_session.refresh(task)
            assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_agent_unavailable_runner_skips_agent_tasks(self, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 2,
                "agent_available": False,
            },
        )
        create_task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        compute_task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(create_task)
        db_session.add(compute_task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, create_task, {project.id: [runner]}, {}
        ) is False
        assert await orch._try_runner_dispatch(
            db_session, compute_task, {project.id: [runner]}, {}
        ) is True

    @pytest.mark.asyncio
    async def test_active_compute_jobs_count_toward_runner_capacity(
        self, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 2,
                "active_compute_job_ids": ["job-a", "job-b"],
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [runner]}, {}
        ) is False

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_prefers_least_loaded(self, db_session):
        project = await _make_project(db_session)
        busy, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "active_task_ids": ["a", "b"]},
        )
        idle, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "active_task_ids": []},
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [busy, idle]}
        )
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{idle.id}"

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_prefers_requested_runner_when_eligible(
        self, db_session
    ):
        project = await _make_project(db_session)
        preferred, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": ["a", "b"],
                "max_concurrent": 4,
            },
        )
        idle, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 4,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(preferred.id)},
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [preferred, idle]}, {}
        )
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{preferred.id}"
        assert task.context["runner_target_id"] == str(preferred.id)

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_falls_back_when_preferred_runner_at_capacity(
        self, db_session
    ):
        project = await _make_project(db_session)
        preferred, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": ["existing-task"],
                "max_concurrent": 1,
            },
        )
        fallback, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 4,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(preferred.id)},
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [preferred, fallback]}, {}
        )
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{fallback.id}"
        assert task.context["runner_target_id"] == str(fallback.id)

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_falls_back_when_preferred_runner_at_cycle_capacity(
        self, db_session
    ):
        project = await _make_project(db_session)
        preferred, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 1,
            },
        )
        fallback, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 4,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(preferred.id)},
        )
        db_session.add(task)
        await db_session.commit()

        runner_pins_this_cycle = {preferred.id: 1}
        assert await orch._try_runner_dispatch(
            db_session,
            task,
            {project.id: [preferred, fallback]},
            runner_pins_this_cycle,
        )
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{fallback.id}"
        assert task.context["runner_target_id"] == str(fallback.id)
        assert runner_pins_this_cycle[preferred.id] == 1
        assert runner_pins_this_cycle[fallback.id] == 1

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_falls_back_when_preferred_runner_stale(
        self, db_session
    ):
        project = await _make_project(db_session)
        stale, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={"task_types": "all", "max_concurrent": 4},
        )
        live, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "max_concurrent": 4},
        )
        cache = await orch._load_active_runners_cache(db_session)
        assert {r.id for r in cache.get(project.id, [])} == {live.id}

        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(stale.id)},
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(db_session, task, cache, {})
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{live.id}"
        assert task.context["runner_target_id"] == str(live.id)

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_falls_back_when_preferred_runner_ineligible(
        self, db_session
    ):
        project = await _make_project(db_session)
        preferred, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["review"],
                "active_task_ids": [],
                "max_concurrent": 4,
            },
        )
        fallback, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 4,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(preferred.id)},
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [preferred, fallback]}, {}
        )
        await db_session.refresh(task)
        assert task.vm_instance_id == f"runner:{fallback.id}"
        assert task.context["runner_target_id"] == str(fallback.id)

    @pytest.mark.asyncio
    async def test_strategic_local_required_holds_when_no_runner(
        self, db_session, mocker
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-should-not-launch",
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
            },
        )
        db_session.add(task)
        await db_session.commit()

        assert (
            await orch._try_runner_dispatch(db_session, task, {project.id: []}, {})
            is False
        )
        broadcast = mocker.patch(
            "app.broadcast.broadcast_to_project",
            new=AsyncMock(),
        )

        assert await orch._hold_strategic_local_runner_task(db_session, task) is True

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id is None
        assert task.context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] is True
        assert task.context[STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY]
        assert "runner_target_id" not in task.context
        assert "waiting for a live Strategic-capable local runner" in (
            task.latest_status_message or ""
        )
        broadcast.assert_awaited_once()
        assert broadcast.await_args.args[0] == str(project.id)
        assert broadcast.await_args.args[1] == "task_updated"
        assert broadcast.await_args.args[2]["task_id"] == str(task.id)
        assert broadcast.await_args.args[2]["message"] == task.latest_status_message

    @pytest.mark.asyncio
    async def test_strategic_local_required_hold_clears_resource_placeholder(
        self, db_session, mocker
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-pre-dispatch-placeholder",
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
            },
        )
        db_session.add(task)
        await db_session.commit()

        mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())

        assert await orch._hold_strategic_local_runner_task(db_session, task) is True

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id is None
        assert task.context[STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY]

    @pytest.mark.asyncio
    async def test_strategic_local_required_runner_path_holds_without_resource_deferral(
        self, db_session, mocker
    ):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            vm_instance_id="agent-vm-pre-dispatch-placeholder",
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
            },
        )
        db_session.add(task)
        await db_session.commit()
        mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())

        handled = await orch._try_strategic_local_required_runner_path(
            db_session,
            task,
            {project.id: []},
            {},
        )

        await db_session.refresh(task)
        assert handled is True
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id is None
        assert task.context[STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY]

    @pytest.mark.asyncio
    async def test_strategic_local_required_clears_hold_when_runner_available(
        self, db_session
    ):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["strategic_review"],
                "has_gpu": False,
                "max_concurrent": 2,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: _utcnow().isoformat(),
            },
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session,
            task,
            {project.id: [runner]},
            {},
        )

        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING
        assert task.vm_instance_id == f"runner:{runner.id}"
        assert task.context["runner_target_id"] == str(runner.id)
        assert STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY not in task.context
        assert task.context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] is True

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_skips_already_pinned(self, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            context={"runner_target_id": "someone-else"},
        )
        db_session.add(task)
        await db_session.commit()
        assert (
            await orch._try_runner_dispatch(
                db_session, task, {project.id: [runner]}
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_respects_max_concurrent(self, db_session):
        project = await _make_project(db_session)
        # saturated runner: 2 active tasks, max_concurrent=2
        saturated, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": ["a", "b"],
                "max_concurrent": 2,
            },
        )
        # free runner: 0 active tasks, max_concurrent=2
        free, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": [],
                "max_concurrent": 2,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        assert await orch._try_runner_dispatch(
            db_session, task, {project.id: [saturated, free]}
        )
        await db_session.refresh(task)
        # saturated runner must not be chosen
        assert task.vm_instance_id == f"runner:{free.id}"

    @pytest.mark.asyncio
    async def test_try_runner_dispatch_skips_when_all_at_capacity(self, db_session):
        project = await _make_project(db_session)
        saturated, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "active_task_ids": ["a", "b"],
                "max_concurrent": 2,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        # all runners at capacity → dispatch returns False
        assert (
            await orch._try_runner_dispatch(
                db_session, task, {project.id: [saturated]}
            )
            is False
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_deactivate_stale_runners(self, db_session):
        project = await _make_project(db_session)
        fresh, _ = await _make_runner(db_session, project.id, last_seen_at=_utcnow())
        stale, _ = await _make_runner(
            db_session, project.id, last_seen_at=_utcnow() - timedelta(minutes=10)
        )

        await orch._deactivate_stale_runners(db_session)
        await db_session.refresh(fresh)
        await db_session.refresh(stale)
        assert fresh.status == "active"
        assert stale.status == "inactive"

    @pytest.mark.asyncio
    async def test_deactivate_stale_runners_emits_grouped_workbench_refetch(
        self, db_session
    ):
        project_a = await _make_project(db_session, name="Runner Project A")
        project_b = await _make_project(db_session, name="Runner Project B")
        fresh, _ = await _make_runner(db_session, project_a.id, last_seen_at=_utcnow())
        stale_a1, _ = await _make_runner(
            db_session, project_a.id, last_seen_at=_utcnow() - timedelta(minutes=10)
        )
        stale_a2, _ = await _make_runner(
            db_session, project_a.id, last_seen_at=_utcnow() - timedelta(minutes=11)
        )
        stale_b, _ = await _make_runner(
            db_session, project_b.id, last_seen_at=_utcnow() - timedelta(minutes=12)
        )
        pinned = Task(
            project_id=project_a.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{stale_a1.id}",
            vm_provisioned_at=_utcnow(),
            context={"runner_target_id": str(stale_a1.id)},
        )
        db_session.add(pinned)
        await db_session.commit()

        with patch(
            "app.orchestrator_runner_dispatch.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            await orch._deactivate_stale_runners(db_session)

        await db_session.refresh(fresh)
        await db_session.refresh(stale_a1)
        await db_session.refresh(stale_a2)
        await db_session.refresh(stale_b)
        assert fresh.status == "active"
        assert stale_a1.status == "inactive"
        assert stale_a2.status == "inactive"
        assert stale_b.status == "inactive"

        assert emit.await_count == 2
        calls_by_project = {call.args[0]: call for call in emit.await_args_list}
        assert set(calls_by_project) == {project_a.id, project_b.id}
        for call in calls_by_project.values():
            assert call.kwargs["resource"] == "local_runner"
            assert call.kwargs["action"] == "deactivated_stale"
            assert call.kwargs["refetch_surfaces"] == (
                "strategic_session",
                "runtime",
            )
            assert call.kwargs["summary_key"] == "runners"

        assert calls_by_project[project_a.id].kwargs["summary"] == {
            "deactivated_count": 2,
            "released_unpulled_pins": 1,
        }
        assert calls_by_project[project_b.id].kwargs["summary"] == {
            "deactivated_count": 1,
            "released_unpulled_pins": 0,
        }

    @pytest.mark.asyncio
    async def test_deactivate_stale_runners_null_last_seen_at(self, db_session):
        project = await _make_project(db_session)
        never_heartbeated, _ = await _make_runner(
            db_session, project.id, last_seen_at=None
        )
        alive, _ = await _make_runner(db_session, project.id, last_seen_at=_utcnow())

        await orch._deactivate_stale_runners(db_session)
        await db_session.refresh(never_heartbeated)
        await db_session.refresh(alive)
        assert never_heartbeated.status == "inactive"
        assert alive.status == "active"

    @pytest.mark.asyncio
    async def test_deactivate_stale_runners_releases_unpulled_pins(self, db_session):
        # A task pinned to a runner that goes stale before pulling it must be
        # released back to QUEUED. The orphan reaper intentionally no longer reaps
        # awaiting-pull runner pins, so deactivation is the only recovery path —
        # without it the task would wedge forever.
        project = await _make_project(db_session)
        stale, _ = await _make_runner(
            db_session, project.id, last_seen_at=_utcnow() - timedelta(minutes=10)
        )
        pinned = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{stale.id}",
            vm_provisioned_at=_utcnow(),
            context={"runner_target_id": str(stale.id)},
        )
        db_session.add(pinned)
        await db_session.commit()

        await orch._deactivate_stale_runners(db_session)

        await db_session.refresh(stale)
        await db_session.refresh(pinned)
        assert stale.status == "inactive"
        assert pinned.status == TaskStatus.QUEUED
        assert pinned.vm_instance_id is None
        assert pinned.vm_provisioned_at is None
        assert "runner_target_id" not in (pinned.context or {})

    @pytest.mark.asyncio
    async def test_deactivate_stale_runners_keeps_pulled_pins(self, db_session):
        # A pin the runner already pulled (started_at set) is in-flight work, not
        # an awaiting-pull pin — deactivation must NOT yank it back to QUEUED.
        project = await _make_project(db_session)
        stale, _ = await _make_runner(
            db_session, project.id, last_seen_at=_utcnow() - timedelta(minutes=10)
        )
        running = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{stale.id}",
            vm_provisioned_at=_utcnow(),
            started_at=_utcnow(),
            context={"runner_target_id": str(stale.id)},
        )
        db_session.add(running)
        await db_session.commit()

        await orch._deactivate_stale_runners(db_session)

        await db_session.refresh(running)
        assert running.status == TaskStatus.PROVISIONING
        assert running.vm_instance_id == f"runner:{stale.id}"


class TestRunnerDeathAlert:
    """Runner-death alerting: the stale-runner sweep raises a deduplicated,
    push-notified operator alert on active->inactive, and a heartbeat that
    re-arms the runner resolves it. See app.alert_dispatch._maybe_create_
    runner_deactivated_alert / _resolve_runner_deactivated_alert."""

    async def _runner_alerts(self, db_session, project_id):
        rows = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.event_type == "runner.deactivated",
                )
            )
        ).scalars().all()
        return list(rows)

    @pytest.mark.asyncio
    async def test_sweep_creates_warning_alert_for_idle_runner(self, db_session):
        project = await _make_project(db_session)
        idle, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={"task_types": "all", "active_task_ids": []},
        )
        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            await orch._deactivate_stale_runners(db_session)

        alerts = await self._runner_alerts(db_session, project.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.domain == "core"
        assert alert.dedupe_key == f"runner.deactivated:{idle.id}"
        assert alert.dismissed_at is None
        assert "went offline" in alert.title
        # Retry-capable notification dispatched for the newly-created alert.
        notify.assert_called_once()
        assert notify.call_args.args[1] == "RUNNER_DEACTIVATED"

    @pytest.mark.asyncio
    async def test_sweep_creates_error_alert_for_runner_with_active_work(
        self, db_session
    ):
        project = await _make_project(db_session)
        busy, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={
                "task_types": "all",
                "active_task_ids": ["a", "b"],
                "active_compute_job_ids": ["j1"],
            },
        )
        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ):
            await orch._deactivate_stale_runners(db_session)

        alerts = await self._runner_alerts(db_session, project.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.ERROR
        assert "mid-job" in alert.title
        # 3 active work items (2 tasks + 1 compute job) reported in the message.
        assert "3 active" in alert.message
        assert str(busy.id)[:8] in alert.message

    @pytest.mark.asyncio
    async def test_sweep_alert_is_deduplicated(self, db_session):
        # A repeated sweep over a still-dead runner must NOT create a second
        # alert (dedupe_key upsert). We reset the runner to active+stale to force
        # the sweep to re-select it, simulating re-entry without a recovery.
        project = await _make_project(db_session)
        stale, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            await orch._deactivate_stale_runners(db_session)
            # Force a re-selection: flip back to active but keep the heartbeat stale.
            stale.status = "active"
            stale.last_seen_at = _utcnow() - timedelta(minutes=10)
            db_session.add(stale)
            await db_session.commit()
            await orch._deactivate_stale_runners(db_session)

        alerts = await self._runner_alerts(db_session, project.id)
        assert len(alerts) == 1
        # Notification dispatched once (first creation only), not on the dedup re-entry.
        notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_resolves_open_alert(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            status="inactive",
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        # Seed an open runner-death alert as the sweep would have.
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Fleet runner went offline: runner x",
            message="dead",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=f"runner.deactivated:{runner.id}",
        )
        db_session.add(alert)
        await db_session.commit()

        with patch("app.alert_dispatch.broadcast_to_project", new=AsyncMock()):
            resp = await client.post(
                "/api/runner/heartbeat",
                json={"active_task_ids": []},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200

        await db_session.refresh(alert)
        assert alert.dismissed_at is not None
        await db_session.refresh(runner)
        assert runner.status == "active"

    @pytest.mark.asyncio
    async def test_resolve_is_noop_without_open_alert(self, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        with patch("app.alert_dispatch.broadcast_to_project", new=AsyncMock()):
            dismissed = await alert_dispatch._resolve_runner_deactivated_alert(
                db_session, project.id, runner.id
            )
        assert dismissed == 0

    @pytest.mark.asyncio
    async def test_escalation_fires_after_1h(self, db_session):
        """_maybe_escalate_runner_deactivated_alerts re-notifies after 1h open."""
        from app.alert_dispatch import _maybe_escalate_runner_deactivated_alerts, _runner_escalation_last_notified

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        # Seed an open runner-deactivated alert created > 1h ago.
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Fleet runner went offline: testhost",
            message="Runner offline",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=f"runner.deactivated:{runner.id}",
        )
        alert.created_at = _utcnow() - timedelta(hours=2)
        db_session.add(alert)
        await db_session.commit()

        dedupe_key = f"runner.deactivated:{runner.id}"
        _runner_escalation_last_notified.pop(dedupe_key, None)

        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            count = await _maybe_escalate_runner_deactivated_alerts(db_session)

        assert count == 1
        notify.assert_called_once()
        call_event = notify.call_args.args[1]
        assert call_event == "RUNNER_DEACTIVATED"
        payload = notify.call_args.args[2]
        assert payload.get("escalation") is True
        # State recorded so the next sweep doesn't re-fire immediately.
        assert dedupe_key in _runner_escalation_last_notified
        # Cleanup
        _runner_escalation_last_notified.pop(dedupe_key, None)

    @pytest.mark.asyncio
    async def test_escalation_does_not_fire_before_1h(self, db_session):
        """Escalation is suppressed for alerts younger than 1h."""
        from app.alert_dispatch import _maybe_escalate_runner_deactivated_alerts, _runner_escalation_last_notified

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Fleet runner went offline: testhost",
            message="Runner offline",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=f"runner.deactivated:{runner.id}",
        )
        alert.created_at = _utcnow() - timedelta(minutes=30)
        db_session.add(alert)
        await db_session.commit()

        dedupe_key = f"runner.deactivated:{runner.id}"
        _runner_escalation_last_notified.pop(dedupe_key, None)

        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            count = await _maybe_escalate_runner_deactivated_alerts(db_session)

        assert count == 0
        notify.assert_not_called()
        _runner_escalation_last_notified.pop(dedupe_key, None)

    @pytest.mark.asyncio
    async def test_resolve_clears_escalation_state(self, db_session):
        """Resolving a runner-deactivated alert clears the escalation tracking entry."""
        from app.alert_dispatch import _runner_escalation_last_notified

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        dedupe_key = f"runner.deactivated:{runner.id}"
        # Seed escalation state.
        _runner_escalation_last_notified[dedupe_key] = _utcnow()

        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Fleet runner went offline: testhost",
            message="Runner offline",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=dedupe_key,
        )
        db_session.add(alert)
        await db_session.commit()

        with patch("app.alert_dispatch.broadcast_to_project", new=AsyncMock()):
            dismissed = await alert_dispatch._resolve_runner_deactivated_alert(
                db_session, project.id, runner.id
            )

        assert dismissed == 1
        assert dedupe_key not in _runner_escalation_last_notified

    @pytest.mark.asyncio
    async def test_escalation_fires_for_born_error_alert(self, db_session):
        """Born-ERROR alerts (had_active_work=True) still get re-notified by the sweep.

        Regression guard for the guard that was incorrectly keying off raw severity:
        a runner that died mid-job creates an ERROR-severity alert from birth.
        The plural sweep must still re-notify for it — only alerts escalated from
        WARNING→ERROR by the singular _maybe_escalate_runner_deactivated_alert path
        should be skipped (those are tracked in _runner_outage_escalated_keys, not
        by severity alone).
        """
        from app.alert_dispatch import (
            _maybe_escalate_runner_deactivated_alerts,
            _runner_escalation_last_notified,
            _runner_outage_escalated_keys,
        )

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        dedupe_key = f"runner.deactivated:{runner.id}"

        # Seed a born-ERROR alert (had_active_work=True path creates ERROR severity).
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="Fleet runner went offline mid-job: testhost",
            message="Runner offline mid-job",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=dedupe_key,
        )
        alert.created_at = _utcnow() - timedelta(hours=2)
        db_session.add(alert)
        await db_session.commit()

        # Ensure no stale state from the singular escalation path.
        _runner_escalation_last_notified.pop(dedupe_key, None)
        _runner_outage_escalated_keys.discard(dedupe_key)

        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            count = await _maybe_escalate_runner_deactivated_alerts(db_session)

        # Must re-notify even though severity is ERROR (it was born ERROR, not
        # escalated-to-ERROR by the sweep).
        assert count == 1, "born-ERROR alert should receive re-notification from the sweep"
        notify.assert_called_once()
        call_event = notify.call_args.args[1]
        assert call_event == "RUNNER_DEACTIVATED"
        # Cleanup
        _runner_escalation_last_notified.pop(dedupe_key, None)
        _runner_outage_escalated_keys.discard(dedupe_key)

    async def test_acknowledged_born_error_alert_not_repaged(self, db_session):
        """Acknowledging a born-ERROR alert suppresses its RUNNER_DEACTIVATED re-pages.

        Mirrors the contract promised by Alert.acknowledged_at and the
        POST /alerts/{id}/acknowledge endpoint: acknowledging suppresses further
        escalation re-pages until the alert auto-resolves.
        """
        from app.alert_dispatch import (
            _maybe_escalate_runner_deactivated_alerts,
            _runner_escalation_last_notified,
            _runner_outage_escalated_keys,
        )

        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)
        dedupe_key = f"runner.deactivated:{runner.id}"

        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="Fleet runner went offline mid-job: testhost",
            message="Runner offline mid-job",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=dedupe_key,
        )
        alert.created_at = _utcnow() - timedelta(hours=2)
        alert.acknowledged_at = _utcnow()
        db_session.add(alert)
        await db_session.commit()

        _runner_escalation_last_notified.pop(dedupe_key, None)
        _runner_outage_escalated_keys.discard(dedupe_key)

        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            count = await _maybe_escalate_runner_deactivated_alerts(db_session)

        assert count == 0, "acknowledged born-ERROR alert must NOT be re-paged"
        notify.assert_not_called()
        # Cleanup
        _runner_escalation_last_notified.pop(dedupe_key, None)
        _runner_outage_escalated_keys.discard(dedupe_key)


class TestRunnerDegradedAlert:
    """Degraded-runner alerting: a runner that heartbeats but fails every job
    raises a WARNING alert via _maybe_create_runner_degraded_alert and the
    _check_degraded_runners sweep. The alert clears when the runner recovers.
    """

    async def _degraded_alerts(self, db_session, project_id):
        rows = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.event_type == "runner.degraded",
                )
            )
        ).scalars().all()
        return list(rows)

    @pytest.mark.asyncio
    async def test_degraded_runner_creates_warning_alert(self, db_session):
        """_check_degraded_runners creates a WARNING alert for a failure-streaking runner."""
        from app.orchestrator_runner_dispatch import _check_degraded_runners

        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        runner.consecutive_job_failures = 5
        db_session.add(runner)
        # Second healthy runner keeps the fleet-no-healthy aggregate alert
        # (RUNNER_FLEET_OFFLINE) out of frame — this test pins the per-runner
        # degraded alert only.
        await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        await db_session.commit()

        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            await _check_degraded_runners(db_session)

        alerts = await self._degraded_alerts(db_session, project.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.domain == "core"
        assert alert.dedupe_key == f"runner.degraded:{runner.id}"
        assert alert.dismissed_at is None
        assert "degraded" in alert.title
        assert "5 consecutive" in alert.message
        notify.assert_called_once()
        assert notify.call_args.args[1] == "RUNNER_DEGRADED"

    @pytest.mark.asyncio
    async def test_degraded_alert_is_deduplicated(self, db_session):
        """Repeated sweep passes over a still-degraded runner don't create duplicate alerts."""
        from app.orchestrator_runner_dispatch import _check_degraded_runners

        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        runner.consecutive_job_failures = 4
        db_session.add(runner)
        # Healthy sibling keeps the fleet-no-healthy aggregate alert silent.
        await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        await db_session.commit()

        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            await _check_degraded_runners(db_session)
            await _check_degraded_runners(db_session)

        alerts = await self._degraded_alerts(db_session, project.id)
        assert len(alerts) == 1
        # Notification dispatched once (first creation), not on the dedup re-entry.
        notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_healthy_runner_does_not_create_degraded_alert(self, db_session):
        """A runner with zero consecutive failures is not degraded."""
        from app.orchestrator_runner_dispatch import _check_degraded_runners

        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        runner.consecutive_job_failures = 0
        db_session.add(runner)
        await db_session.commit()

        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ):
            await _check_degraded_runners(db_session)

        alerts = await self._degraded_alerts(db_session, project.id)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_recovery_resolves_degraded_alert(self, db_session):
        """When a runner's consecutive_job_failures drops below threshold, the
        degraded alert is dismissed."""
        from app.orchestrator_runner_dispatch import _check_degraded_runners

        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        # Seed an open degraded alert (as if created by a prior sweep pass).
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title=f"Fleet runner degraded: {runner.hostname or 'runner ' + str(runner.id)[:8]}",
            message="Degraded",
            domain="core",
            event_type="runner.degraded",
            dedupe_key=f"runner.degraded:{runner.id}",
        )
        db_session.add(alert)
        runner.consecutive_job_failures = 0  # Runner recovered
        db_session.add(runner)
        await db_session.commit()

        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ):
            await _check_degraded_runners(db_session)

        await db_session.refresh(alert)
        assert alert.dismissed_at is not None

    @pytest.mark.asyncio
    async def test_inactive_runner_not_flagged_as_degraded(self, db_session):
        """An inactive runner (already deactivated) is not picked up by the degraded sweep."""
        from app.orchestrator_runner_dispatch import _check_degraded_runners

        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            status="inactive",
            last_seen_at=_utcnow() - timedelta(hours=1),
        )
        runner.consecutive_job_failures = 10
        db_session.add(runner)
        # Healthy sibling keeps the fleet-no-healthy aggregate alert
        # (RUNNER_FLEET_OFFLINE) silent so notify.assert_not_called() pins the
        # degraded sweep specifically.
        await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(seconds=10),
        )
        await db_session.commit()

        with patch(
            "app.alert_dispatch.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as notify:
            await _check_degraded_runners(db_session)

        alerts = await self._degraded_alerts(db_session, project.id)
        assert len(alerts) == 0
        notify.assert_not_called()



class TestRunnerDeathEscalation:
    """Runner-outage escalation: alerts older than the threshold are escalated
    to ERROR and re-paged. Acked alerts are suppressed. Flap suppression is
    natural (recovery before threshold dismisses the alert). See
    app.alert_dispatch._maybe_escalate_runner_deactivated_alert and
    app.orchestrator_runner_dispatch._sweep_runner_deactivated_escalations."""

    async def _seed_alert(self, db_session, project_id, runner_id, *, age_hours=5):
        """Seed an open runner-deactivated WARNING alert with the given age."""
        alert = Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=f"Fleet runner went offline: runner {str(runner_id)[:8]}",
            message="dead",
            domain="core",
            event_type="runner.deactivated",
            dedupe_key=f"runner.deactivated:{runner_id}",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)
        # Back-date created_at to simulate an aging alert.
        from sqlalchemy import update as _sa_update
        from app.models import Alert as _Alert
        await db_session.execute(
            _sa_update(_Alert)
            .where(_Alert.id == alert.id)
            .values(created_at=_utcnow() - timedelta(hours=age_hours))
        )
        await db_session.commit()
        await db_session.refresh(alert)
        return alert

    @pytest.mark.asyncio
    async def test_escalates_old_warning_alert(self, db_session):
        """An open WARNING alert older than the threshold is escalated to ERROR."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session, project.id, status="inactive",
            last_seen_at=_utcnow() - timedelta(hours=5),
        )
        alert = await self._seed_alert(db_session, project.id, runner.id, age_hours=5)
        with patch(
            "app.runner_health_alerts.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=AsyncMock(),
        ) as webhook:
            escalated = await alert_dispatch._maybe_escalate_runner_deactivated_alert(
                db_session,
                project_id=project.id,
                runner_id=runner.id,
                hostname=runner.hostname,
            )
            # Re-page is fire-and-forget via asyncio.ensure_future; yield so the
            # scheduled dispatch task runs before we assert on it.
            await asyncio.sleep(0)
        assert escalated is True
        await db_session.refresh(alert)
        assert alert.severity == AlertSeverity.ERROR
        assert "escalated outage" in alert.title
        # Escalation now routes through the operator-configured, retry-capable
        # dispatch_external_notification path — NOT the env-var-only
        # _send_webhook_notification (PIPELINE_WEBHOOK_URL) path.
        webhook.assert_awaited_once()
        assert webhook.await_args.args[1] == "RUNNER_OUTAGE_ESCALATED"
        assert webhook.await_args.args[0] == project.id

    @pytest.mark.asyncio
    async def test_skips_escalation_if_acknowledged(self, db_session):
        """An ack'd alert is not escalated — operator already knows."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session, project.id, status="inactive",
            last_seen_at=_utcnow() - timedelta(hours=5),
        )
        alert = await self._seed_alert(db_session, project.id, runner.id, age_hours=5)
        alert.acknowledged_at = _utcnow()
        db_session.add(alert)
        await db_session.commit()
        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=AsyncMock(),
        ) as webhook:
            escalated = await alert_dispatch._maybe_escalate_runner_deactivated_alert(
                db_session,
                project_id=project.id,
                runner_id=runner.id,
            )
            await asyncio.sleep(0)
        assert escalated is False
        webhook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_escalation_if_alert_young(self, db_session):
        """A recent alert (within threshold) is not escalated — flap suppression."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session, project.id, status="inactive",
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        alert = await self._seed_alert(db_session, project.id, runner.id, age_hours=0)
        # Ensure created_at is very recent (minutes, not hours).
        from sqlalchemy import update as _sa_update
        from app.models import Alert as _Alert
        await db_session.execute(
            _sa_update(_Alert)
            .where(_Alert.id == alert.id)
            .values(created_at=_utcnow() - timedelta(minutes=10))
        )
        await db_session.commit()
        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=AsyncMock(),
        ) as webhook:
            escalated = await alert_dispatch._maybe_escalate_runner_deactivated_alert(
                db_session,
                project_id=project.id,
                runner_id=runner.id,
            )
            await asyncio.sleep(0)
        assert escalated is False
        webhook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_escalates_inactive_runner(self, db_session):
        """_sweep_runner_deactivated_escalations fires escalation for aged alerts."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session, project.id, status="inactive",
            last_seen_at=_utcnow() - timedelta(hours=5),
        )
        await self._seed_alert(db_session, project.id, runner.id, age_hours=5)
        with patch(
            "app.runner_health_alerts.broadcast_to_project", new=AsyncMock()
        ), patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=AsyncMock(),
        ) as webhook:
            await orch._sweep_runner_deactivated_escalations(db_session)
            await asyncio.sleep(0)
        webhook.assert_awaited_once()
        assert webhook.await_args.args[1] == "RUNNER_OUTAGE_ESCALATED"

    @pytest.mark.asyncio
    async def test_sweep_skips_active_runner(self, db_session):
        """_sweep_runner_deactivated_escalations skips if runner recovered (active)."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session, project.id, status="active",
            last_seen_at=_utcnow() - timedelta(minutes=1),
        )
        await self._seed_alert(db_session, project.id, runner.id, age_hours=5)
        with patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=AsyncMock(),
        ) as webhook:
            await orch._sweep_runner_deactivated_escalations(db_session)
            await asyncio.sleep(0)
        webhook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_acknowledge_endpoint_stamps_timestamp(
        self, client: AsyncClient, db_session
    ):
        """POST /alerts/{id}/acknowledge sets acknowledged_at."""
        project = await _make_project(db_session)
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Test runner alert",
            message="down",
            event_type="runner.deactivated",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)
        resp = await client.post(f"/api/admin/alerts/{alert.id}/acknowledge")
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"
        await db_session.refresh(alert)
        assert alert.acknowledged_at is not None

    @pytest.mark.asyncio
    async def test_acknowledge_endpoint_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Acknowledging an already-acknowledged alert is a no-op (200)."""
        project = await _make_project(db_session)
        acked_at = _utcnow() - timedelta(minutes=1)
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="Test runner alert",
            message="down",
            event_type="runner.deactivated",
            acknowledged_at=acked_at,
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)
        resp = await client.post(f"/api/admin/alerts/{alert.id}/acknowledge")
        assert resp.status_code == 200
        await db_session.refresh(alert)
        # acknowledged_at should not be updated on second call.
        assert alert.acknowledged_at is not None


class TestDeadRunnerAttention:
    """Item 4: a registered runner that is not-live surfaces in the operator
    attention feed directly, independent of a pinned task. See
    app.operator_attention._load_dead_runners / _dead_runner_attention_item."""

    @pytest.mark.asyncio
    async def test_idle_dead_runner_surfaces_as_watch(self, db_session):
        from app.operator_attention import (
            _dead_runner_attention_item,
            _load_dead_runners,
        )

        project = await _make_project(db_session)
        dead, _ = await _make_runner(
            db_session,
            project.id,
            hostname="workstation",
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={"task_types": "all", "active_task_ids": []},
        )
        needs_op, watch, runners = await _load_dead_runners(db_session, project.id)
        assert needs_op == 0
        assert watch == 1
        assert [r.id for r in runners] == [dead.id]
        item = _dead_runner_attention_item(runners[0])
        assert item["reason"] == "dead_runner"
        assert item["severity"] == "watch"
        assert item["runner_id"] == str(dead.id)
        assert item["target"]["section"] == "devices"
        assert item["target"]["runner_id"] == str(dead.id)

    @pytest.mark.asyncio
    async def test_midjob_dead_runner_surfaces_as_needs_operator(self, db_session):
        from app.operator_attention import (
            _dead_runner_attention_item,
            _load_dead_runners,
        )

        project = await _make_project(db_session)
        await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={"task_types": "all", "active_task_ids": ["running-task"]},
        )
        needs_op, watch, runners = await _load_dead_runners(db_session, project.id)
        assert needs_op == 1
        assert watch == 0
        item = _dead_runner_attention_item(runners[0])
        assert item["severity"] == "needs_operator"
        assert item["label"] == "Fleet runner offline mid-job"

    @pytest.mark.asyncio
    async def test_live_and_booting_runners_excluded(self, db_session):
        from app.operator_attention import _load_dead_runners

        project = await _make_project(db_session)
        await _make_runner(db_session, project.id, last_seen_at=_utcnow())  # live
        # Never-heartbeated active runner: booting/just-registered, not a death.
        await _make_runner(db_session, project.id, status="active", last_seen_at=None)
        needs_op, watch, runners = await _load_dead_runners(db_session, project.id)
        assert (needs_op, watch, runners) == (0, 0, [])

    @pytest.mark.asyncio
    async def test_runner_with_open_alert_is_excluded(self, db_session):
        from app.operator_attention import _load_dead_runners

        project = await _make_project(db_session)
        dead, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        db_session.add(
            Alert(
                project_id=project.id,
                severity=AlertSeverity.WARNING,
                title="Fleet runner went offline: x",
                message="dead",
                domain="core",
                event_type="runner.deactivated",
                dedupe_key=f"runner.deactivated:{dead.id}",
            )
        )
        await db_session.commit()
        needs_op, watch, runners = await _load_dead_runners(db_session, project.id)
        # Covered by its open alert (which surfaces via the alert path) → excluded.
        assert (needs_op, watch, runners) == (0, 0, [])

    @pytest.mark.asyncio
    async def test_runner_with_unpulled_pin_is_excluded(self, db_session):
        from app.operator_attention import _load_dead_runners

        project = await _make_project(db_session)
        dead, _ = await _make_runner(
            db_session,
            project.id,
            last_seen_at=_utcnow() - timedelta(minutes=10),
        )
        # Unpulled pin → covered by the stale_runner_pin item, not dead_runner.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "compute_experiment"}],
                status=TaskStatus.PROVISIONING,
                started_at=None,
                vm_instance_id=f"runner:{dead.id}",
                vm_provisioned_at=_utcnow(),
                context={"runner_target_id": str(dead.id)},
            )
        )
        await db_session.commit()
        needs_op, watch, runners = await _load_dead_runners(db_session, project.id)
        assert (needs_op, watch, runners) == (0, 0, [])


# ── Strategic-spawning local-runner availability signal ──────────────────────


class TestLocalRunnerStatusForStrategist:
    """`strategic_spawning._load_local_runner_status` — live local-runner
    availability surfaced into the strategist's authoring context so it avoids
    authoring GPU compute_experiment tasks (which silently fall back to billable
    cloud GPU) while the operator's hardware is offline. Informational only.
    """

    @pytest.mark.asyncio
    async def test_no_runners_reports_never_registered(self, db_session):
        from app.strategic_spawning import _load_local_runner_status

        project = await _make_project(db_session)
        status = await _load_local_runner_status(db_session, project.id)
        assert status == {
            "ever_registered": False,
            "active_total": 0,
            "active_gpu": 0,
        }
        # The payload conditional-inclusion gate omits the field entirely when
        # the project never registered a runner (mirrors _spawn_strategic_review).
        payload_fragment = (
            {"local_runner_status": status}
            if status.get("ever_registered")
            else {}
        )
        assert "local_runner_status" not in payload_fragment

    @pytest.mark.asyncio
    async def test_stale_gpu_runner_registered_but_not_active(self, db_session):
        project = await _make_project(db_session)
        # Registered with GPU but last heartbeat is outside the 300s liveness
        # window → operator hardware is considered offline.
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow() - timedelta(seconds=400),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        assert status["ever_registered"] is True
        assert status["active_total"] == 0
        assert status["active_gpu"] == 0
        # Field IS included now (ever_registered True) so the agent sees the
        # hardware is down (active_gpu == 0) and can hold GPU experiments.
        payload_fragment = (
            {"local_runner_status": status}
            if status.get("ever_registered")
            else {}
        )
        assert payload_fragment["local_runner_status"] is status

    @pytest.mark.asyncio
    async def test_live_gpu_runner_counts_as_active_gpu(self, db_session):
        project = await _make_project(db_session)
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow(),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        assert status["ever_registered"] is True
        assert status["active_total"] == 1
        assert status["active_gpu"] == 1

    @pytest.mark.asyncio
    async def test_mixed_runners_count_active_gpu_subset(self, db_session):
        project = await _make_project(db_session)
        # Live non-GPU runner → counts toward active_total but not active_gpu.
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
            last_seen_at=_utcnow(),
        )
        # Live GPU runner → counts toward both.
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow(),
        )
        # Stale GPU runner → registered but not live.
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow() - timedelta(seconds=400),
        )
        # Deleted GPU runner → excluded from ever_registered and all counts.
        await _make_runner(
            db_session,
            project.id,
            status="deleted",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow(),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        assert status["ever_registered"] is True
        assert status["active_total"] == 2
        assert status["active_gpu"] == 1


    def test_local_runner_status_registered_in_strategic_schema(self):
        """Regression guard: local_runner_status must be in _STRATEGIC_REVIEW_SCHEMA
        with priority 'medium' so the packer does not silently downgrade it to 'low'."""
        from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

        entry = _STRATEGIC_REVIEW_SCHEMA.get("local_runner_status")
        assert entry is not None, (
            "local_runner_status is missing from _STRATEGIC_REVIEW_SCHEMA; "
            "pack_under_budget will default it to 'low' priority and may drop it "
            "under budget pressure even when it is the context preventing unsuitable "
            "GPU experiment proposals"
        )
        assert entry.get("priority") == "medium", (
            f"local_runner_status priority should be 'medium', got {entry.get('priority')!r}"
        )

    @pytest.mark.asyncio
    async def test_strategic_review_payload_keeps_local_runner_status(self, db_session):
        """local_runner_status must survive budget pressure in pack_under_budget.

        Constructs a near-budget payload with local_runner_status present and
        confirms the packer includes it in the result (not in _packer_meta dropped).
        """
        from app.payload_schemas import pack_under_budget
        from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

        runner_status = {
            "ever_registered": True,
            "active_total": 0,
            "active_gpu": 0,
            "require_local_runner": False,
        }
        # Build a payload that is large but not over budget so the packer runs
        # its priority logic.  Fill a medium-priority sibling (conversation_thread)
        # with enough data to make the test non-trivial but keep total well under budget.
        payload = {
            "type": "strategic_review",
            "project_name": "test-project",
            "base_branch": "main",
            "research_mode": "strategic",
            "is_self_repo": False,
            "enable_self_improvement": False,
            "model": "claude-sonnet-4-5",
            "effort": "high",
            "vision": {"purpose": "test"},
            "direction_counts": {"active": 2, "paused": 0, "total": 2, "max_active": 5},
            "max_active_directions": 5,
            "local_runner_status": runner_status,
        }
        packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)
        dropped_keys = {d["key"] for d in packed.get("_packer_meta", {}).get("dropped", [])}
        assert "local_runner_status" not in dropped_keys, (
            "local_runner_status was dropped by pack_under_budget; "
            f"_packer_meta.dropped = {packed['_packer_meta']['dropped']}"
        )
        assert packed.get("local_runner_status") == runner_status, (
            "local_runner_status value was not preserved correctly in the packed payload"
        )


class TestRegistryPasswordErasedOnTerminalFinalization:
    @pytest.mark.asyncio
    async def test_password_erased_from_polling_metadata_on_finalization(
        self, db_session
    ):
        from app.compute_jobs import _finalize_terminal_job

        project = await _make_project(db_session)
        now = _utcnow()
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={
                "container_registry_auth": {
                    "server": "registry.example.com",
                    "username": "myuser",
                    "password": "s3cr3t",
                },
                "runner_target_id": "some-runner-id",
            },
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        _finalize_terminal_job(
            job,
            status=ComputeJobStatus.SUCCEEDED,
            outcome=ComputeJobOutcome.SUCCESS,
            terminal_reason="job completed",
            now=now,
        )

        auth = job.polling_metadata.get("container_registry_auth", {})
        assert "password" not in auth, (
            "registry password must be erased from polling_metadata at terminal state"
        )
        assert auth.get("server") == "registry.example.com"
        assert auth.get("username") == "myuser"

    @pytest.mark.asyncio
    async def test_finalization_without_registry_auth_leaves_metadata_intact(
        self, db_session
    ):
        from app.compute_jobs import _finalize_terminal_job

        project = await _make_project(db_session)
        now = _utcnow()
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": "some-runner-id"},
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        _finalize_terminal_job(
            job,
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.FAILURE,
            terminal_reason="job failed",
            now=now,
        )

        assert "container_registry_auth" not in (job.polling_metadata or {})
        assert job.polling_metadata.get("runner_target_id") == "some-runner-id"


class TestReportCommandLogDropsArbitraryKeys:
    """Pin the allowlist behavior of the /compute-job/{id}/report endpoint.

    The handler ONLY extracts ``text`` from ``command_log`` and writes a
    fixed-shape ``{"text": ..., "truncated": ...}`` object — no injected keys
    leak through, and result_metrics is bounded by _MAX_RUNNER_RESULT_METRICS_KEYS.
    """

    async def _make_running_job(self, db_session):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(runner.id)},
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return project, runner, token, job

    @pytest.mark.asyncio
    async def test_report_command_log_drops_arbitrary_keys(
        self, client: AsyncClient, db_session
    ):
        """Extra/hostile keys in command_log must NOT appear in polling_metadata."""
        from app.routers.runner import _MAX_RUNNER_RESULT_METRICS_KEYS

        _project, runner, token, job = await self._make_running_job(db_session)

        # Build a command_log with hostile keys beyond ``text``.
        hostile_command_log = {
            "text": "run ok",
            "injected": "x",
            "container_registry_auth": {"password": "leak"},
            "runner_exit_code": 999,
        }

        # Build a result_metrics dict exceeding the key cap.
        oversized_metrics = {f"metric_{i}": i for i in range(_MAX_RUNNER_RESULT_METRICS_KEYS + 50)}

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "command_log": hostile_command_log,
                "result_metrics": oversized_metrics,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        await db_session.refresh(job)
        meta = job.polling_metadata

        # runner_command_log must contain ONLY text and truncated.
        command_log_stored = meta.get("runner_command_log", {})
        assert set(command_log_stored.keys()) == {"text", "truncated"}, (
            f"runner_command_log must have exactly {{text, truncated}}, got {set(command_log_stored.keys())}"
        )
        assert command_log_stored["text"] == "run ok"
        assert command_log_stored["truncated"] is False

        # Hostile keys must NOT appear as top-level polling_metadata keys.
        for hostile_key in ("injected", "container_registry_auth", "runner_exit_code"):
            assert hostile_key not in meta, (
                f"hostile key '{hostile_key}' leaked into top-level polling_metadata"
            )

        # result_metrics must be bounded.
        stored_metrics = meta.get("runner_result_metrics", {})
        assert len(stored_metrics) <= _MAX_RUNNER_RESULT_METRICS_KEYS, (
            f"result_metrics has {len(stored_metrics)} keys, expected <= {_MAX_RUNNER_RESULT_METRICS_KEYS}"
        )

    @pytest.mark.asyncio
    async def test_report_command_log_truncates_oversized_text(
        self, client: AsyncClient, db_session
    ):
        """Oversized text in command_log is truncated and truncated=True is set."""
        from app.routers.runner import _MAX_RUNNER_COMMAND_LOG_BYTES

        _project, _runner, token, job = await self._make_running_job(db_session)

        # Build a text that exceeds the byte cap.
        big_text = "A" * (_MAX_RUNNER_COMMAND_LOG_BYTES + 1000)

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "command_log": {"text": big_text, "extra_hostile": "should_drop"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(job)
        stored = job.polling_metadata.get("runner_command_log", {})
        assert stored.get("truncated") is True, "truncated must be True for oversized text"
        assert set(stored.keys()) == {"text", "truncated"}, (
            f"runner_command_log must have exactly {{text, truncated}}, got {set(stored.keys())}"
        )
        assert len(stored["text"].encode("utf-8")) <= _MAX_RUNNER_COMMAND_LOG_BYTES

    @pytest.mark.asyncio
    async def test_runner_report_drops_non_numeric_result_metric_values(
        self, client: AsyncClient, db_session
    ):
        """Non-numeric values in result_metrics are dropped at the report endpoint.

        Strings, None, and nested objects must be filtered out so a misbehaving
        runner cannot corrupt a JSONB experiment record with non-serialisable or
        unexpected types. Only finite numeric values survive.
        """
        _project, _runner, token, job = await self._make_running_job(db_session)

        mixed_metrics = {
            "valid_int": 42,
            "valid_float": 3.14,
            "string_val": "should_drop",
            "null_val": None,
            "nested_dict": {"inner": 1},
            "nested_list": [1, 2, 3],
            "bool_true": True,
            "bool_false": False,
        }

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "result_metrics": mixed_metrics,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(job)
        stored = job.polling_metadata.get("runner_result_metrics", {})

        # Finite numerics survive (int and float).
        assert stored.get("valid_int") == 42.0, (
            f"expected valid_int=42.0, got {stored.get('valid_int')!r}"
        )
        assert abs(stored.get("valid_float", 0) - 3.14) < 1e-9, (
            f"expected valid_float≈3.14, got {stored.get('valid_float')!r}"
        )

        # Booleans are coerced to float (True→1.0, False→0.0).
        assert stored.get("bool_true") == 1.0, (
            f"expected bool_true=1.0, got {stored.get('bool_true')!r}"
        )
        assert stored.get("bool_false") == 0.0, (
            f"expected bool_false=0.0, got {stored.get('bool_false')!r}"
        )

        # Non-numeric types are dropped entirely.
        assert "string_val" not in stored, (
            "string value must be dropped, but 'string_val' found in stored metrics"
        )
        assert "null_val" not in stored, (
            "null value must be dropped, but 'null_val' found in stored metrics"
        )
        assert "nested_dict" not in stored, (
            "nested dict must be dropped, but 'nested_dict' found in stored metrics"
        )
        assert "nested_list" not in stored, (
            "nested list must be dropped, but 'nested_list' found in stored metrics"
        )

    @pytest.mark.asyncio
    async def test_report_result_metrics_rejects_non_finite_floats(
        self, client: AsyncClient, db_session
    ):
        """NaN and Infinity values in result_metrics are dropped at the report endpoint.

        JSON does not natively represent NaN/Inf, but some JSON serialisers emit
        them as bare tokens. The endpoint must reject non-finite values to prevent
        JSONB corruption.
        """
        import math

        _project, _runner, token, job = await self._make_running_job(db_session)

        # Use a numeric value that is finite to confirm the filter is selective.
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "result_metrics": {
                    "finite_val": 1.5,
                    "very_large": 1e308,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(job)
        stored = job.polling_metadata.get("runner_result_metrics", {})
        assert stored.get("finite_val") == 1.5
        assert math.isfinite(stored.get("very_large", float("nan")))

        # Directly verify _filter_result_metrics drops non-finite floats.
        from app.routers.runner import _filter_result_metrics

        result = _filter_result_metrics({
            "nan_val": float("nan"),
            "inf_val": float("inf"),
            "neg_inf": float("-inf"),
            "ok": 7.0,
        })
        assert "nan_val" not in result, "NaN must be dropped"
        assert "inf_val" not in result, "Inf must be dropped"
        assert "neg_inf" not in result, "-Inf must be dropped"
        assert result.get("ok") == 7.0

    @pytest.mark.asyncio
    async def test_report_result_metrics_preserves_arms_block(
        self, client: AsyncClient, db_session
    ):
        """The 'arms' key with a dict value passes through verbatim.

        A/B experiment arm results are structured objects, not scalars. The filter
        must preserve the arms block exactly as received so downstream consumers
        (_extract_experiment_arms) can process it.
        """
        _project, _runner, token, job = await self._make_running_job(db_session)

        arms_block = {
            "control": {"accuracy": 0.92, "loss": 0.08},
            "treatment": {"accuracy": 0.95, "loss": 0.05},
        }

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "succeeded",
                "result_metrics": {
                    "accuracy": 0.95,
                    "arms": arms_block,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(job)
        stored = job.polling_metadata.get("runner_result_metrics", {})
        assert stored.get("accuracy") == 0.95
        assert stored.get("arms") == arms_block, (
            f"arms block must be preserved verbatim, got {stored.get('arms')!r}"
        )

    def test_filter_result_metrics_unit_caps_at_key_limit(self):
        """_filter_result_metrics enforces the key count cap."""
        from app.routers.runner import _MAX_RUNNER_RESULT_METRICS_KEYS, _filter_result_metrics

        oversized = {f"k{i}": float(i) for i in range(_MAX_RUNNER_RESULT_METRICS_KEYS + 100)}
        result = _filter_result_metrics(oversized)
        assert len(result) == _MAX_RUNNER_RESULT_METRICS_KEYS, (
            f"expected exactly {_MAX_RUNNER_RESULT_METRICS_KEYS} keys, got {len(result)}"
        )


# ── Direct compute_experiment dispatch (no agent half) ───────────────────────


async def _compute_job_for_task(session, task_id):
    """Return the ComputeJob created for a task, or None."""
    from sqlmodel import select

    return (
        await session.execute(select(ComputeJob).where(ComputeJob.task_id == task_id))
    ).scalars().first()


def _ce_payload(**overrides):
    """A minimal valid compute_experiment task payload."""
    p = {
        "type": "compute_experiment",
        "command": "echo hi",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
    }
    p.update(overrides)
    return p


class TestDirectComputeExperimentDispatch:
    """The orchestrator launches compute_experiment ComputeJobs directly (no agent
    half): runner-pinned when an eligible runner exists, cloud fallback otherwise,
    enforce-local hold, recoverable-block / clean-fail on launch errors."""

    @pytest.mark.asyncio
    async def test_launch_pins_gpu_runner_and_parks(self, db_session):
        project = await _make_project(db_session)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
        )
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(accelerator="T4")],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [runner]}, {}
        )
        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.status == TaskStatus.PARKED
        job = await _compute_job_for_task(db_session, task.id)
        assert job is not None
        # GPU job pinned to the runner → runner-subprocess backend + pin recorded.
        assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        assert (job.polling_metadata or {}).get("runner_target_id") == str(runner.id)

    @pytest.mark.asyncio
    async def test_launch_prefers_requested_compute_runner_when_eligible(
        self, db_session
    ):
        project = await _make_project(db_session)
        preferred, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 4,
                "active_compute_job_ids": ["existing-job"],
            },
        )
        idle, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 4,
                "active_compute_job_ids": [],
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
            context={"preferred_runner_target_id": str(preferred.id)},
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session,
            task,
            {project.id: [preferred, idle]},
            {},
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.status == TaskStatus.PARKED
        assert task.context["runner_target_id"] == str(preferred.id)
        job = await _compute_job_for_task(db_session, task.id)
        assert job is not None
        assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        assert (job.polling_metadata or {}).get("runner_target_id") == str(
            preferred.id
        )

    @pytest.mark.asyncio
    async def test_runner_subprocess_jobs_count_toward_next_cycle_capacity(
        self, db_session
    ):
        project = await _make_project(db_session)
        project.build_config = {"resource_envelope": {"require_local_runner": True}}
        db_session.add(project)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 1},
        )
        first = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        second = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        db_session.add(first)
        db_session.add(second)
        await db_session.commit()

        assert await orch._dispatch_compute_experiment_directly(
            db_session, first, {project.id: [runner]}, {}
        ) == "launched"
        first_job = await _compute_job_for_task(db_session, first.id)
        assert first_job is not None
        assert first_job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        assert first_job.status == ComputeJobStatus.QUEUED

        next_cycle_cache = await orch._load_active_runners_cache(db_session)
        next_cycle_runner = next_cycle_cache[project.id][0]
        assert str(first_job.id) in (
            next_cycle_runner.capabilities or {}
        ).get("active_compute_job_ids", [])

        assert await orch._dispatch_compute_experiment_directly(
            db_session, second, next_cycle_cache, {}
        ) == "held"
        await db_session.refresh(second)
        assert second.status == TaskStatus.QUEUED
        assert (second.context or {}).get("awaiting_local_runner_since")
        assert await _compute_job_for_task(db_session, second.id) is None

    @pytest.mark.asyncio
    async def test_launch_cloud_fallback_when_no_runner(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(machine_type="e2-standard-2")],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        # No runner registered and no enforce-local policy → cloud fallback.
        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.status == TaskStatus.PARKED
        job = await _compute_job_for_task(db_session, task.id)
        assert job is not None
        assert job.backend != RUNNER_SUBPROCESS_BACKEND_NAME

    @pytest.mark.asyncio
    async def test_held_when_require_local_and_no_runner(self, db_session):
        project = await _make_project(db_session)
        project.build_config = {"resource_envelope": {"require_local_runner": True}}
        db_session.add(project)
        await db_session.commit()
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "held"
        await db_session.refresh(task)
        # Held QUEUED with the wait stamp; NEVER a billable cloud job.
        assert task.status == TaskStatus.QUEUED
        assert (task.context or {}).get("awaiting_local_runner_since")
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_strategic_local_runner_does_not_satisfy_compute_local_policy(
        self, db_session
    ):
        project = await _make_project(db_session)
        project.build_config = {"resource_envelope": {"require_local_runner": True}}
        db_session.add(project)
        runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": sorted(STRATEGIC_TASK_TYPES),
                "has_gpu": False,
                "max_concurrent": 1,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [runner]}, {}
        )
        assert outcome == "held"
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert (task.context or {}).get("awaiting_local_runner_since")
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_clears_stale_pin_on_cloud_fallback(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(machine_type="e2-standard-2")],
            status=TaskStatus.QUEUED,
            # A pin left by a prior cycle whose runner is gone.
            context={"runner_target_id": str(uuid.uuid4())},
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.status == TaskStatus.PARKED
        # Stale pin dropped → job took cloud fallback, not the dead runner.
        assert "runner_target_id" not in (task.context or {})
        job = await _compute_job_for_task(db_session, task.id)
        assert job is not None
        assert job.backend != RUNNER_SUBPROCESS_BACKEND_NAME

    @pytest.mark.asyncio
    async def test_fails_cleanly_on_misconfig(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(command="")],  # empty command → misconfigured
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "failed"
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert (task.context or {}).get("failure_category") == "compute_launch_failed"
        # A misconfigured launch never creates a (billable) compute job.
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_non_positive_spend_cap_fails_not_clamped(self, db_session):
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(spend_cap_usd=0)],  # zero cap → reject loudly
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "failed"
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert await _compute_job_for_task(db_session, task.id) is None


# ── Rate limiting ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_polling_endpoint_rate_limited(
    client: AsyncClient, db_session, monkeypatch
):
    """Runner heartbeat endpoint returns 429 + Retry-After once limit is exceeded.

    Verifies: (a) under-limit calls return 200, (b) the call that crosses the
    threshold returns 429 with a Retry-After header, (c) a second distinct runner
    is not throttled (per-(runner_id, endpoint) bucketing, not global).
    """
    import app.runner_rate_limiter as rl_mod
    from app.runner_rate_limiter import reset_runner_rate_limiter

    project = await _make_project(db_session)
    runner_a, token_a = await _make_runner(db_session, project.id)
    runner_b, token_b = await _make_runner(db_session, project.id)

    # Lower the heartbeat limit to 3 so the test is fast and deterministic.
    monkeypatch.setitem(rl_mod._ENDPOINT_LIMITS, "heartbeat", 3)
    reset_runner_rate_limiter()

    # First 3 calls from runner A must succeed.
    for _ in range(3):
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    # 4th call from runner A must be rate-limited.
    resp = await client.post(
        "/api/runner/heartbeat",
        json={"active_task_ids": []},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None, "Expected Retry-After header in 429 response"
    assert int(retry_after) > 0, f"Retry-After must be positive, got {retry_after!r}"

    # Runner B's bucket is independent — it must still get through.
    resp_b = await client.post(
        "/api/runner/heartbeat",
        json={"active_task_ids": []},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 200, (
        f"Runner B should not be throttled, got {resp_b.status_code}"
    )


@pytest.mark.asyncio
async def test_report_compute_job_cross_project_emits_warning(
    client: AsyncClient, db_session, caplog
):
    """When a runner reports a compute job that belongs to a different project,
    a structured WARNING is emitted with runner_id, runner_project_id, job_id,
    and job_actual_project_id so operators can detect misconfigured runners."""
    import logging

    project_a = await _make_project(db_session, name="Project A")
    project_b = await _make_project(db_session, name="Project B")
    runner_a, token_a = await _make_runner(db_session, project_a.id)

    # Create a RUNNING job that belongs to project_b, pinned to runner_a's id
    # (simulates a misconfiguration where the runner was re-registered to a new project
    # but still knows about old job IDs, or vice versa).
    job = ComputeJob(
        project_id=project_b.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"runner_target_id": str(runner_a.id)},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    with caplog.at_level(logging.WARNING, logger="blueprint"):
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={"exec_status": "running"},
            headers={"Authorization": f"Bearer {token_a}"},
        )

    assert resp.status_code == 404
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    mismatch_warnings = [r for r in warning_records if "Cross-project" in r.getMessage()]
    assert len(mismatch_warnings) == 1, f"Expected 1 cross-project warning, got {len(mismatch_warnings)}"
    msg = mismatch_warnings[0].getMessage()
    assert str(runner_a.id) in msg
    assert str(project_a.id) in msg
    assert str(job.id) in msg
    assert str(project_b.id) in msg
