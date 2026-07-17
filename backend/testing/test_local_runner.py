"""Tests for the pull-based local fleet runner.

Covers the four new surfaces:
  * runner auth (app.auth: create/hash/verify token, get_current_runner)
  * admin management endpoints (app.routers.admin_runners)
  * runner polling/heartbeat endpoints (app.routers.runner)
  * orchestrator runner routing helpers (app.orchestrator)
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified
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
    ComputeJobFile,
    ComputeJobOutcome,
    ComputeJobStatus,
    LocalRunner,
    MessagesNoisePrefs,
    Project,
    ProjectMember,
    RunnerProjectBinding,
    Task,
    TaskStatus,
    _utcnow,
)
from app.runner_project_binding import allowed_project_ids
from app.messages_noise_prefs_store import get_messages_noise_prefs_row
from app.routers.admin_runners import (
    _normalize_gpu_indices_allowed,
    _runner_health,
)
from app.routers.runner import (
    _FileRangeReader,
    _MAX_MULTIPART_FIELD_BYTES,
    _MAX_MULTIPART_PARTS,
    _parse_heartbeat_timestamp,
    _parse_multipart_form,
    _parse_multipart_form_stream,
)
from app.runner_config import config_generation, live_safe_config
from app.task_capabilities import STRATEGIC_LOCAL_AGENT_TASK_TYPES, STRATEGIC_TASK_TYPES
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


@pytest.mark.asyncio
async def test_runner_project_binding_backfill_and_resolver(db_session):
    """The allow-list resolver folds in the enrollment project and unions
    explicit bindings; the backfill produces exactly one row per runner."""
    from app import runner_project_binding as rpb

    proj_a = await _make_project(db_session, name="Alpha")
    proj_b = await _make_project(db_session, name="Beta")
    runner, _token = await _make_runner(db_session, proj_a.id)

    # 1) Freshly enrolled runner with NO binding rows: the resolver still
    #    reports its enrollment project (implicit anchor membership).
    allowed = await rpb.allowed_project_ids(db_session, runner)
    assert allowed == {proj_a.id}
    assert await rpb.bound_project_ids(db_session, runner.id) == set()

    # 2) Backfill creates exactly one binding row per runner (its enrollment
    #    project) and is idempotent on a second run.
    created = await rpb.backfill_enrollment_bindings(db_session)
    await db_session.commit()
    assert created == 1
    rows = (
        await db_session.execute(
            select(RunnerProjectBinding).where(
                RunnerProjectBinding.runner_id == runner.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].project_id == proj_a.id
    # Idempotent: re-running adds nothing.
    assert await rpb.backfill_enrollment_bindings(db_session) == 0

    # 3) Adding a second project to the allow-list unions into the resolver.
    await rpb.add_binding(db_session, runner.id, proj_b.id)
    await db_session.commit()
    allowed = await rpb.allowed_project_ids(db_session, runner)
    assert allowed == {proj_a.id, proj_b.id}
    assert await rpb.bound_project_ids(db_session, runner.id) == {
        proj_a.id,
        proj_b.id,
    }

    # add_binding is idempotent (unique constraint respected, no duplicate row).
    await rpb.add_binding(db_session, runner.id, proj_b.id)
    await db_session.commit()
    assert await rpb.bound_project_ids(db_session, runner.id) == {
        proj_a.id,
        proj_b.id,
    }

    # 4) Removing the enrollment-project binding does NOT revoke access — the
    #    resolver folds the enrollment anchor back in regardless.
    assert await rpb.remove_binding(db_session, runner.id, proj_a.id) is True
    await db_session.commit()
    allowed = await rpb.allowed_project_ids(db_session, runner)
    assert proj_a.id in allowed
    assert allowed == {proj_a.id, proj_b.id}


@pytest.mark.asyncio
async def test_multi_project_runner_claim_and_authz(client: AsyncClient, db_session):
    """Operator walkthrough: one runner enrolled under project A, granted project
    B via the owner-gated endpoint, serves BOTH — while work in a non-allowed
    project C is rejected server-side, and adding B does NOT create a second
    per-project GPU/concurrency budget (the ledger stays unified).
    """
    proj_a = await _make_project(db_session, name="Alpha")
    proj_b = await _make_project(db_session, name="Beta")
    proj_c = await _make_project(db_session, name="Gamma")  # NOT on the allow-list
    runner, token = await _make_runner(db_session, proj_a.id)
    _seed_pool_account(TEST_USER_ID)
    try:
        # 1) Add project B to the runner's allow-list via the admin endpoint.
        add_resp = await client.post(
            f"/api/admin/projects/{proj_a.id}/runners/{runner.id}/projects",
            json={"project_id": str(proj_b.id)},
        )
        assert add_resp.status_code == 200, add_resp.text
        listed = {p["project_id"] for p in add_resp.json()["projects"]}
        assert listed == {str(proj_a.id), str(proj_b.id)}
        # The enrollment project is flagged as the (non-removable) anchor member.
        enrollment_entry = next(
            p for p in add_resp.json()["projects"]
            if p["project_id"] == str(proj_a.id)
        )
        assert enrollment_entry["is_enrollment"] is True

        # 2) A project-B task pinned to this runner is returned by next-task, even
        #    though the runner ENROLLED under project A.
        task_b = Task(
            project_id=proj_b.id,
            payloads=[{"type": "create", "title": "cross-project claim"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task_b)
        await db_session.commit()

        nt_resp = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert nt_resp.status_code == 200, nt_resp.text
        assert nt_resp.json()["task_id"] == str(task_b.id)

        # 3) A compute job in ALLOWED project B is claimable and reportable.
        job_b = ComputeJob(
            project_id=proj_b.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo hi",
            },
        )
        # 4) A compute job in NON-allowed project C, pinned to the very same
        #    runner, must NOT be claimable — the allow-list is the authz boundary.
        job_c = ComputeJob(
            project_id=proj_c.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo nope",
            },
        )
        db_session.add(job_b)
        db_session.add(job_c)
        await db_session.commit()

        ncj_resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ncj_resp.status_code == 200, ncj_resp.text
        # Only the allowed-project job is handed out — the project-C job is never
        # returned even though it is pinned to this runner and older/newer ordering
        # cannot surface it.
        assert ncj_resp.json()["job_id"] == str(job_b.id)

        # A report against the non-allowed project-C job is rejected (404).
        report_c = await client.post(
            f"/api/runner/compute-job/{job_c.id}/report",
            headers={"Authorization": f"Bearer {token}"},
            json={"exec_status": "succeeded", "exit_code": 0},
        )
        assert report_c.status_code == 404

        # 5) UNIFIED LEDGER: the dispatch cache registers ONE shared snapshot per
        #    physical box under each allowed project — the SAME object, so pins and
        #    active counts accrue once and a single GPU cannot be double-booked
        #    across projects. Adding project B created no second budget.
        cache = await orch._load_active_runners_cache(db_session)
        snap_a = next((s for s in cache.get(proj_a.id, []) if s.id == runner.id), None)
        snap_b = next((s for s in cache.get(proj_b.id, []) if s.id == runner.id), None)
        assert snap_a is not None and snap_b is not None
        assert snap_a is snap_b  # one shared ledger, not a per-project copy
        # The non-allowed project has no candidate for this runner.
        assert all(s.id != runner.id for s in cache.get(proj_c.id, []))

        # 6) AUTHZ: attaching a project owned by a DIFFERENT account is rejected
        #    403 even when the caller holds an owner role on it (MVP consent model).
        cross = Project(name="CrossAccount", user_id="other-account-999")
        db_session.add(cross)
        await db_session.commit()
        await db_session.refresh(cross)
        db_session.add(
            ProjectMember(project_id=cross.id, user_id=TEST_USER_ID, role="owner")
        )
        await db_session.commit()
        cross_resp = await client.post(
            f"/api/admin/projects/{proj_a.id}/runners/{runner.id}/projects",
            json={"project_id": str(cross.id)},
        )
        assert cross_resp.status_code == 403

        # 7) The enrollment project cannot be removed from the allow-list.
        rm_enroll = await client.delete(
            f"/api/admin/projects/{proj_a.id}/runners/{runner.id}/projects/{proj_a.id}"
        )
        assert rm_enroll.status_code == 409

        # 8) Removing project B revokes cross-project service: after removal a
        #    project-B job is no longer claimable.
        rm_b = await client.delete(
            f"/api/admin/projects/{proj_a.id}/runners/{runner.id}/projects/{proj_b.id}"
        )
        assert rm_b.status_code == 200
        remaining = {p["project_id"] for p in rm_b.json()["projects"]}
        assert remaining == {str(proj_a.id)}

        job_b2 = ComputeJob(
            project_id=proj_b.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo hi again",
            },
        )
        db_session.add(job_b2)
        await db_session.commit()
        ncj_after = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ncj_after.status_code == 204
    finally:
        _clear_pool()


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


# ── Live-safe config projection + generation hash (pure, no DB) ───────────────


class TestLiveSafeConfigProjection:
    def test_projects_live_safe_subset_with_compute_default(self):
        cfg = live_safe_config(
            {
                "task_types": ["create", "fix"],
                "max_concurrent": 4,
                "has_gpu": True,  # telemetry — must NOT leak into the subset
                "gpu_count": 2,
            }
        )
        assert cfg == {
            "max_concurrent": 4,
            # compute ceiling defaults to max_concurrent when unset.
            "max_concurrent_compute": 4,
            "task_types": ["create", "fix"],
            "gpu_indices_allowed": "all",
            # per-job GPU count defaults to 1 when unset (backward compatible).
            "gpus_per_job": 1,
        }
        assert "has_gpu" not in cfg and "gpu_count" not in cfg

    def test_gpu_indices_allowed_dedup_and_sorted(self):
        cfg = live_safe_config({"gpu_indices_allowed": [2, 0, 2, 1]})
        assert cfg["gpu_indices_allowed"] == [0, 1, 2]

    def test_generation_is_order_independent_but_value_sensitive(self):
        base = live_safe_config(
            {"task_types": ["a", "b"], "gpu_indices_allowed": [1, 0]}
        )
        reordered = live_safe_config(
            {"gpu_indices_allowed": [0, 1], "task_types": ["a", "b"]}
        )
        # Same effective config → identical generation (order-independent).
        assert config_generation(base) == config_generation(reordered)
        # A single live-safe field change → different generation.
        changed = live_safe_config(
            {"task_types": ["a", "b"], "gpu_indices_allowed": [0, 1, 2]}
        )
        assert config_generation(changed) != config_generation(base)

    def test_missing_max_concurrent_compute_defaults_but_explicit_wins(self):
        assert live_safe_config({"max_concurrent": 3})["max_concurrent_compute"] == 3
        assert (
            live_safe_config({"max_concurrent": 3, "max_concurrent_compute": 1})[
                "max_concurrent_compute"
            ]
            == 1
        )


class TestNormalizeGpuIndicesAllowed:
    def test_accepts_all_sentinel_and_valid_list(self):
        assert _normalize_gpu_indices_allowed("all") == "all"
        assert _normalize_gpu_indices_allowed([3, 1, 1, 0]) == [0, 1, 3]
        # Empty list is a deliberate "no GPUs allowed" restriction.
        assert _normalize_gpu_indices_allowed([]) == []

    def test_rejects_junk(self):
        for bad in ["none", ["gpu0"], [1.5], [True], [-1], {"0": True}, 0]:
            with pytest.raises(ValueError):
                _normalize_gpu_indices_allowed(bad)


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


def test_runner_gpu_capable_verifies_self_report_against_telemetry():
    """`_runner_gpu_capable` disbelieves a mis-set `has_gpu` when the runner's OWN
    GPU telemetry (gpu_count/gpu_names) contradicts the claim, but trusts the flag
    when telemetry is absent (backward compatible with older runners)."""
    # No claim → not GPU-capable (unchanged from the bare has_gpu filter).
    assert orch._runner_gpu_capable(_snapshot({})) is False
    assert orch._runner_gpu_capable(_snapshot({"has_gpu": False})) is False

    # Claim CORROBORATED by telemetry → GPU-capable.
    assert (
        orch._runner_gpu_capable(
            _snapshot({"has_gpu": True, "gpu_count": 1, "gpu_names": ["Tesla T4"]})
        )
        is True
    )
    # gpu_count is authoritative when present: a positive count is trusted even if
    # names were not reported.
    assert (
        orch._runner_gpu_capable(_snapshot({"has_gpu": True, "gpu_count": 2}))
        is True
    )

    # Claim CONTRADICTED by telemetry (the mis-set-flag lie) → NOT GPU-capable.
    # gpu_count of 0 is a direct contradiction.
    assert (
        orch._runner_gpu_capable(_snapshot({"has_gpu": True, "gpu_count": 0}))
        is False
    )
    # Empty gpu_names (with no count) is the fallback contradiction signal.
    assert (
        orch._runner_gpu_capable(_snapshot({"has_gpu": True, "gpu_names": []}))
        is False
    )
    # gpu_count is authoritative over names: count 0 wins even if a stray name rode
    # along, so a runner reporting an honest nvidia-smi count of 0 is disbelieved.
    assert (
        orch._runner_gpu_capable(
            _snapshot({"has_gpu": True, "gpu_count": 0, "gpu_names": ["ghost"]})
        )
        is False
    )

    # Absence of telemetry is NOT a contradiction — an older runner that sets
    # has_gpu but reports no count/names is still trusted (backward compatible).
    assert orch._runner_gpu_capable(_snapshot({"has_gpu": True})) is True


def test_select_eligible_runner_excludes_gpu_liar_prefers_honest_runner():
    """A runner claiming `has_gpu=true` whose telemetry shows no GPU is excluded from
    GPU-accelerator dispatch; a genuinely GPU-capable runner is selected instead."""
    project_id = uuid.uuid4()
    # Liar: claims a GPU but its own telemetry reports zero GPUs.
    liar = _snapshot(
        {"task_types": "all", "has_gpu": True, "gpu_count": 0, "gpu_names": []}
    )
    # Honest GPU runner: claim corroborated by telemetry.
    honest = _snapshot(
        {
            "task_types": "all",
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_names": ["Tesla T4"],
        }
    )
    cache = {project_id: [liar, honest]}

    # A GPU-accelerator task must route to the honest runner, never the liar.
    chosen = orch._select_eligible_runner(
        cache, project_id, "compute_experiment", "T4", {}
    )
    assert chosen is honest

    # With ONLY the liar available, a GPU task finds no eligible runner (it then
    # holds for real hardware rather than dispatching to a box that cannot run it).
    liar_only = {project_id: [liar]}
    assert (
        orch._select_eligible_runner(
            liar_only, project_id, "compute_experiment", "T4", {}
        )
        is None
    )
    # A CPU task (no accelerator) is unaffected — the liar is a perfectly good CPU
    # runner, so the GPU gate never touches it.
    assert (
        orch._select_eligible_runner(
            liar_only, project_id, "compute_experiment", None, {}
        )
        is liar
    )


def test_runner_meets_min_compute_capability_gates_on_reported_arch():
    """`_runner_meets_min_compute_capability` returns True only when a reported GPU
    meets the floor; absent/malformed telemetry and a non-positive floor never gate."""
    from app.runner_eligibility import _runner_meets_min_compute_capability

    # A V100 box (sm_70) reports compute capability 7.0.
    v100 = _snapshot({"gpu_compute_capabilities": [7.0]})
    # An L40S box (sm_89) reports 8.9.
    l40s = _snapshot({"gpu_compute_capabilities": [8.9]})

    # Floor met by the newer arch, not by the older one.
    assert _runner_meets_min_compute_capability(l40s, 8.0) is True
    assert _runner_meets_min_compute_capability(v100, 8.0) is False
    # Exactly-at-floor counts (>= semantics).
    assert _runner_meets_min_compute_capability(v100, 7.0) is True
    # Any ONE GPU meeting the floor is enough (mixed fleet on one box).
    mixed = _snapshot({"gpu_compute_capabilities": [7.0, 8.9]})
    assert _runner_meets_min_compute_capability(mixed, 8.0) is True

    # A non-positive floor is no floor at all.
    assert _runner_meets_min_compute_capability(v100, 0.0) is True
    assert _runner_meets_min_compute_capability(v100, -1.0) is True

    # Absence of telemetry is NOT a disqualification (backward compatible — the
    # reactive in-sandbox probe remains the safety net for such runners).
    assert _runner_meets_min_compute_capability(_snapshot({}), 8.0) is True
    assert (
        _runner_meets_min_compute_capability(
            _snapshot({"gpu_compute_capabilities": []}), 8.0
        )
        is True
    )
    # Malformed telemetry is not trusted to gate.
    assert (
        _runner_meets_min_compute_capability(
            _snapshot({"gpu_compute_capabilities": ["oops"]}), 8.0
        )
        is True
    )


def test_coerce_min_compute_capability_defensive():
    """`_coerce_min_compute_capability` accepts numbers/numeric strings, rejects
    junk with an honest error, and treats unset/non-positive as no floor."""
    from app.orchestrator_runner_dispatch import _coerce_min_compute_capability

    assert _coerce_min_compute_capability({}) == (None, None)
    assert _coerce_min_compute_capability({"min_compute_capability": 8.0}) == (
        8.0,
        None,
    )
    assert _coerce_min_compute_capability({"min_compute_capability": "7.0"}) == (
        7.0,
        None,
    )
    # Nested compute_experiment form is read too.
    assert _coerce_min_compute_capability(
        {"compute_experiment": {"min_compute_capability": 8.9}}
    ) == (8.9, None)
    # Non-positive floor == no floor (not an error).
    assert _coerce_min_compute_capability({"min_compute_capability": 0}) == (
        None,
        None,
    )
    # Junk fails THIS task with a naming error, never raises.
    val, err = _coerce_min_compute_capability({"min_compute_capability": "8.0sm"})
    assert val is None and err is not None and "8.0sm" in err
    # bool is rejected explicitly (int subclass would otherwise mean a 1.0 floor).
    val, err = _coerce_min_compute_capability({"min_compute_capability": True})
    assert val is None and err is not None
    # inf is rejected (finite guard).
    val, err = _coerce_min_compute_capability(
        {"min_compute_capability": float("inf")}
    )
    assert val is None and err is not None


def test_select_eligible_runner_excludes_incompatible_gpu_arch():
    """A runner whose GPU arch is below an image's compute-capability floor is
    excluded from dispatch; a runner meeting the floor is selected instead."""
    project_id = uuid.uuid4()
    v100 = _snapshot(
        {
            "task_types": "all",
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_names": ["Tesla V100"],
            "gpu_compute_capabilities": [7.0],
        }
    )
    l40s = _snapshot(
        {
            "task_types": "all",
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_names": ["L40S"],
            "gpu_compute_capabilities": [8.9],
        }
    )
    cache = {project_id: [v100, l40s]}

    # An sm_80-floor image routes to the L40S, never the V100.
    chosen = orch._select_eligible_runner(
        cache,
        project_id,
        "compute_experiment",
        "gpu",
        {},
        min_compute_capability=8.0,
    )
    assert chosen is l40s

    # With ONLY the V100 available, an sm_80 image finds no eligible runner (it
    # holds for compatible hardware rather than dying in the in-sandbox probe).
    v100_only = {project_id: [v100]}
    assert (
        orch._select_eligible_runner(
            v100_only,
            project_id,
            "compute_experiment",
            "gpu",
            {},
            min_compute_capability=8.0,
        )
        is None
    )
    # Without a floor, the V100 is a perfectly good runner (gate is a no-op).
    assert (
        orch._select_eligible_runner(
            v100_only, project_id, "compute_experiment", "gpu", {}
        )
        is v100
    )


class TestRunnerHealthAgentUnavailableReason:
    """_runner_health() surfaces agent_unavailable_reason from capabilities JSONB."""

    def test_available_runner_has_null_reason(self):
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": True,
                    "agent_unavailable_reason": None,
                }
            )
        )
        assert h["agent_available"] is True
        assert h["agent_unavailable_reason"] is None

    def test_unavailable_runner_surfaces_bwrap_reason(self):
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": False,
                    "agent_unavailable_reason": "bwrap not available",
                }
            )
        )
        assert h["agent_available"] is False
        assert h["agent_unavailable_reason"] == "bwrap not available"

    def test_unavailable_runner_surfaces_node_reason(self):
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": False,
                    "agent_unavailable_reason": "node runtime not functional",
                }
            )
        )
        assert h["agent_unavailable_reason"] == "node runtime not functional"

    def test_old_runner_without_reason_field_returns_none(self):
        # Old runners don't send agent_unavailable_reason — must not error.
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": False,
                }
            )
        )
        assert h["agent_available"] is False
        assert h["agent_unavailable_reason"] is None

    def test_unavailable_runner_surfaces_compound_reason_and_legs(self):
        # The compound reason (every failing leg, "; "-joined) AND the structured
        # per-leg map are both surfaced verbatim from capabilities JSONB, so the
        # fleet panel can render a per-leg checklist instead of parsing a string.
        legs = {
            "bwrap_sandbox": False,
            "agent_dist": True,
            "node_runtime": False,
            "docker": False,
        }
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": False,
                    "agent_unavailable_reason": (
                        "bwrap sandbox unavailable; node runtime not functional; "
                        "docker agent unavailable"
                    ),
                    "agent_unavailable_legs": legs,
                }
            )
        )
        assert h["agent_available"] is False
        assert h["agent_unavailable_legs"] == legs
        # Every failing leg is named in the compound reason.
        assert "bwrap sandbox unavailable" in h["agent_unavailable_reason"]
        assert "node runtime not functional" in h["agent_unavailable_reason"]
        assert "docker agent unavailable" in h["agent_unavailable_reason"]

    def test_old_runner_without_legs_field_returns_none(self):
        # Old runners don't send agent_unavailable_legs — surfaced as None (absent
        # in caps), never a KeyError.
        h = _runner_health(
            _health_runner(
                capabilities={
                    "task_types": "all",
                    "max_concurrent": 2,
                    "agent_available": False,
                    "agent_unavailable_reason": "bwrap not available",
                }
            )
        )
        assert h["agent_unavailable_legs"] is None


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
        assert row.capabilities["task_types"] == sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES)
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


# ── Editable multi-project allow-list admin API ──────────────────────────────


class TestRunnerAllowedProjectsEndpoints:
    """The owner-gated, editable runner<->project allow-list endpoints."""

    async def _register_runner(self, client: AsyncClient, project) -> uuid.UUID:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={"hostname": "multi-box"},
        )
        assert resp.status_code == 200
        return uuid.UUID(resp.json()["runner_id"])

    @pytest.mark.asyncio
    async def test_list_runners_includes_allowed_projects(
        self, client: AsyncClient, db_session
    ):
        """A freshly registered runner shows its enrollment project via the
        dedicated allow-list endpoint."""
        project = await _make_project(db_session, name="Home Project")
        runner_id = await self._register_runner(client, project)

        # Verify the runner appears in the list.
        resp = await client.get(f"/api/admin/projects/{project.id}/runners")
        assert resp.status_code == 200
        rows = resp.json()
        assert any(r["id"] == str(runner_id) for r in rows)

        # Verify the allow-list via the dedicated endpoint.
        proj_resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner_id}/projects"
        )
        assert proj_resp.status_code == 200
        data = proj_resp.json()
        assert data["enrollment_project_id"] == str(project.id)
        project_ids = {p["project_id"] for p in data["projects"]}
        assert project_ids == {str(project.id)}

    @pytest.mark.asyncio
    async def test_add_project_extends_allow_list_and_get_reflects_it(
        self, client: AsyncClient, db_session
    ):
        """Adding a project the caller owns extends the persisted allow-list."""
        managing = await _make_project(db_session, name="Managing")
        target = await _make_project(db_session, name="Target")  # owned by TEST_USER
        runner_id = await self._register_runner(client, managing)

        add = await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(target.id)},
        )
        assert add.status_code == 200
        ids = {p["project_id"] for p in add.json()["projects"]}
        assert ids == {str(managing.id), str(target.id)}

        # GET reflects the extended allow-list.
        got = await client.get(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects"
        )
        assert got.status_code == 200
        got_ids = {p["project_id"] for p in got.json()["projects"]}
        assert got_ids == {str(managing.id), str(target.id)}

        # Idempotent: re-adding the same project is a no-op, not a duplicate.
        again = await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(target.id)},
        )
        assert again.status_code == 200
        assert {p["project_id"] for p in again.json()["projects"]} == {
            str(managing.id),
            str(target.id),
        }

    @pytest.mark.asyncio
    async def test_edit_runner_allowed_projects_requires_owner_on_target(
        self, client: AsyncClient, db_session
    ):
        """The caller owns the managing project but NOT the target project — the
        add is rejected 403 so an owner of A cannot silently grant a runner
        access to B they do not own. Covered for both a member-below-owner and a
        complete non-member on the target, and no binding is created."""
        managing = await _make_project(db_session, name="Managing Owned")
        runner_id = await self._register_runner(client, managing)

        # (a) Caller is a MEMBER (not owner) on the target → 403.
        member_target = await _make_project(
            db_session, owner_id="other-owner", name="Member Target"
        )
        db_session.add(
            ProjectMember(
                project_id=member_target.id,
                user_id=TEST_USER_ID,
                role="member",
            )
        )
        await db_session.commit()
        rejected_member = await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(member_target.id)},
        )
        assert rejected_member.status_code == 403

        # (b) Caller is NOT a member at all on the target → 404, preserving the
        # project-membership non-disclosure boundary.
        nonmember_target = await _make_project(
            db_session, owner_id="other-owner", name="Nonmember Target"
        )
        rejected_nonmember = await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(nonmember_target.id)},
        )
        assert rejected_nonmember.status_code == 404

        # No binding to either target project was created.
        bindings = (
            await db_session.execute(
                select(RunnerProjectBinding.project_id).where(
                    RunnerProjectBinding.runner_id == runner_id
                )
            )
        ).scalars().all()
        assert member_target.id not in bindings
        assert nonmember_target.id not in bindings

    @pytest.mark.asyncio
    async def test_add_requires_owner_on_managing_project(
        self, client: AsyncClient, db_session
    ):
        """A caller who only holds member on the MANAGING project cannot edit the
        allow-list even for a target they own."""
        managing = await _make_project(
            db_session, owner_id="other-owner", name="Member-Only Managing"
        )
        db_session.add(
            ProjectMember(
                project_id=managing.id, user_id=TEST_USER_ID, role="member"
            )
        )
        await db_session.commit()
        runner, _ = await _make_runner(db_session, managing.id)
        target = await _make_project(db_session, name="Owned Target")

        resp = await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner.id}/projects",
            json={"project_id": str(target.id)},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_remove_project_shrinks_allow_list(
        self, client: AsyncClient, db_session
    ):
        """Removing a project shrinks the allow-list; GET reflects the change."""
        managing = await _make_project(db_session, name="Managing R")
        target = await _make_project(db_session, name="Target R")
        runner_id = await self._register_runner(client, managing)
        await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(target.id)},
        )

        remove = await client.delete(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}"
            f"/projects/{target.id}"
        )
        assert remove.status_code == 200
        assert {p["project_id"] for p in remove.json()["projects"]} == {str(managing.id)}

        got = await client.get(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects"
        )
        got_ids = {p["project_id"] for p in got.json()["projects"]}
        assert got_ids == {str(managing.id)}

    @pytest.mark.asyncio
    async def test_remove_last_binding_rejected(
        self, client: AsyncClient, db_session
    ):
        """The enrollment project cannot be removed from the allow-list (409),
        and the runner's allow-list survives intact."""
        managing = await _make_project(db_session, name="Solo Managing")
        runner_id = await self._register_runner(client, managing)

        resp = await client.delete(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}"
            f"/projects/{managing.id}"
        )
        assert resp.status_code == 409

        # Allow-list still has the enrollment project.
        got = await client.get(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects"
        )
        assert got.status_code == 200
        project_ids = {p["project_id"] for p in got.json()["projects"]}
        assert str(managing.id) in project_ids

    @pytest.mark.asyncio
    async def test_remove_project_not_on_allow_list_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        managing = await _make_project(db_session, name="Managing NF")
        target = await _make_project(db_session, name="Target NF")
        runner_id = await self._register_runner(client, managing)
        # Add target so removing 'managing' is not blocked by the enrollment-project guard.
        await client.post(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
            json={"project_id": str(target.id)},
        )
        never_bound = uuid.uuid4()
        resp = await client.delete(
            f"/api/admin/projects/{managing.id}/runners/{runner_id}"
            f"/projects/{never_bound}"
        )
        assert resp.status_code == 200
        assert {
            item["project_id"] for item in resp.json()["projects"]
        } == {str(managing.id), str(target.id)}

    @pytest.mark.asyncio
    async def test_allowed_projects_cross_project_runner_404(
        self, client: AsyncClient, db_session
    ):
        """A runner is only reachable through its own project's path — listing or
        mutating it through an unrelated project 404s."""
        managing = await _make_project(db_session, name="Owner Managing X")
        other = await _make_project(db_session, name="Unrelated X")
        runner_id = await self._register_runner(client, managing)

        got = await client.get(
            f"/api/admin/projects/{other.id}/runners/{runner_id}/projects"
        )
        assert got.status_code == 404

    @pytest.mark.asyncio
    async def test_add_emits_workbench_refetch(
        self, client: AsyncClient, db_session
    ):
        managing = await _make_project(db_session, name="Emit Managing")
        target = await _make_project(db_session, name="Emit Target")
        runner_id = await self._register_runner(client, managing)

        with patch(
            "app.routers.admin_runners.emit_workbench_refetch",
            new=AsyncMock(),
        ) as emit:
            resp = await client.post(
                f"/api/admin/projects/{managing.id}/runners/{runner_id}/projects",
                json={"project_id": str(target.id)},
            )
        assert resp.status_code == 200
        emit.assert_awaited_once()
        assert emit.await_args.kwargs["resource"] == "local_runner"
        assert emit.await_args.kwargs["action"] == "allow_list_updated"


# ── Live-safe config channel (heartbeat two-way + PATCH + view) ──────────────


class TestRunnerConfigChannel:
    @pytest.mark.asyncio
    async def test_heartbeat_response_carries_config_block_and_stable_generation(
        self, client: AsyncClient, db_session
    ):
        """The heartbeat response includes the live-safe config block + a stable
        generation, and keeps the backward-compat enrolled_max_concurrent field."""
        project = await _make_project(db_session)
        _runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": ["create", "fix"],
                "has_gpu": False,
                "max_concurrent": 3,
            },
        )
        resp1 = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 200
        body1 = resp1.json()
        # Backward-compat fields preserved.
        assert body1["enrolled_max_concurrent"] == 3
        assert "version_hint" in body1
        # New config block.
        config = body1["config"]
        assert config["max_concurrent"] == 3
        assert config["max_concurrent_compute"] == 3  # defaults to max_concurrent
        assert config["task_types"] == ["create", "fix"]
        assert config["gpu_indices_allowed"] == "all"
        gen1 = config["config_generation"]
        assert isinstance(gen1, str) and gen1
        assert body1["config_generation"] == gen1

        # A second heartbeat with no config change yields the SAME generation.
        resp2 = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": ["t1"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["config"]["config_generation"] == gen1

    @pytest.mark.asyncio
    async def test_patch_live_safe_field_changes_generation(
        self, client: AsyncClient, db_session
    ):
        """A PATCH that changes a live-safe field (max_concurrent, gpu allowlist)
        moves the config_generation surfaced on the runner view."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )

        before = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        gen_before = before["config"]["config_generation"]

        # Change a live-safe field.
        patch = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"gpu_indices_allowed": [0, 1]},
        )
        assert patch.status_code == 200
        cfg = patch.json()["config"]
        assert cfg["gpu_indices_allowed"] == [0, 1]
        assert cfg["config_generation"] != gen_before
        # Newly desired but not-yet-applied → pending.
        assert cfg["config_pending"] is True

        # The heartbeat response now advertises the new generation, so the daemon
        # can detect the drift.
        hb = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert hb.json()["config"]["config_generation"] == cfg["config_generation"]

    @pytest.mark.asyncio
    async def test_applied_config_generation_resolves_pending(
        self, client: AsyncClient, db_session
    ):
        """A heartbeat carrying applied_config_generation persists it; config_pending
        is False only when the applied generation matches the desired one."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 5},
        )

        # Discover the current desired generation from the heartbeat response.
        desired_gen = (
            await client.post(
                "/api/runner/heartbeat",
                json={"active_task_ids": []},
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()["config"]["config_generation"]

        # Before any applied report, the view shows pending/unknown, never errors.
        view0 = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert view0["config"]["applied_config_generation"] is None
        assert view0["config"]["config_pending"] is True

        # Daemon reports it has applied a DIFFERENT (stale) generation → pending.
        await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "applied_config_generation": "stale000deadbeef",
                "applied_max_concurrent": 2,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        view1 = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert view1["config"]["applied_config_generation"] == "stale000deadbeef"
        assert view1["config"]["applied"] == {"max_concurrent": 2}
        assert view1["config"]["config_pending"] is True

        # Daemon now reports the matching desired generation → NOT pending.
        await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "applied_config_generation": desired_gen,
                "applied_max_concurrent": 5,
                "applied_gpu_indices_allowed": "all",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        view2 = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert view2["config"]["applied_config_generation"] == desired_gen
        assert view2["config"]["config_pending"] is False

        # Applied report must NOT clobber the operator-configured desired subset.
        await db_session.refresh(runner)
        assert runner.capabilities["max_concurrent"] == 5
        assert runner.capabilities["task_types"] == "all"

    @pytest.mark.asyncio
    async def test_config_apply_rejection_reason_persists_and_surfaces(
        self, client: AsyncClient, db_session
    ):
        """A heartbeat reporting a config-apply REJECTION (unrecognized field)
        persists the reason + offending field(s) under dedicated caps keys and
        the fleet view surfaces them — but ONLY while the refused generation
        matches the current desired one; a stale reason is suppressed."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 5},
        )

        desired_gen = (
            await client.post(
                "/api/runner/heartbeat",
                json={"active_task_ids": []},
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()["config"]["config_generation"]

        # Daemon refuses the CURRENT desired generation for an unrecognized field.
        await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "config_apply_status": "rejected",
                "config_apply_reason": "unrecognized config field(s): retries",
                "config_rejected_fields": ["retries"],
                "config_rejected_generation": desired_gen,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        view = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert (
            view["config"]["apply_reason"]
            == "unrecognized config field(s): retries"
        )
        assert view["config"]["config_rejected_fields"] == ["retries"]
        # Stored under dedicated caps keys, never clobbering the desired subset.
        await db_session.refresh(runner)
        assert runner.capabilities["max_concurrent"] == 5
        assert runner.capabilities["config_rejected_generation"] == desired_gen

        # Operator changes the desired config: the stored rejection is now stale
        # (its generation no longer matches desired) → suppressed by the view.
        await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"max_concurrent": 3},
        )
        view_stale = (
            await client.get(f"/api/admin/projects/{project.id}/runners")
        ).json()[0]
        assert view_stale["config"]["apply_reason"] is None
        assert view_stale["config"]["config_rejected_fields"] is None

        # A subsequent clean 'applied' heartbeat CLEARS the stored rejection keys.
        new_gen = view_stale["config"]["config_generation"]
        await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "config_apply_status": "applied",
                "applied_config_generation": new_gen,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        await db_session.refresh(runner)
        assert "config_apply_reason" not in runner.capabilities
        assert "config_rejected_fields" not in runner.capabilities
        assert "config_rejected_generation" not in runner.capabilities

    @pytest.mark.asyncio
    async def test_gpu_indices_allowed_patch_validation(
        self, client: AsyncClient, db_session
    ):
        """PATCH accepts 'all' and a valid list (deduped/sorted); junk is 422."""
        project = await _make_project(db_session)
        runner, _ = await _make_runner(db_session, project.id)

        ok_all = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"gpu_indices_allowed": "all"},
        )
        assert ok_all.status_code == 200
        assert ok_all.json()["capabilities"]["gpu_indices_allowed"] == "all"

        ok_list = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"gpu_indices_allowed": [2, 0, 0, 1]},
        )
        assert ok_list.status_code == 200
        assert ok_list.json()["capabilities"]["gpu_indices_allowed"] == [0, 1, 2]

        for bad in (["gpu0"], [-1], "everything", [1.5]):
            rej = await client.patch(
                f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
                json={"gpu_indices_allowed": bad},
            )
            assert rej.status_code == 422, bad

    @pytest.mark.asyncio
    async def test_strategic_local_invariants_hold_with_gpu_axis(
        self, client: AsyncClient, db_session
    ):
        """Adding the GPU allowlist must not relax the strategic_local clamps:
        max_concurrent stays 1 and task_types stays the strategic set."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/runners/register",
            json={"hostname": "strategic-gpu", "preset": "strategic_local"},
        )
        assert resp.status_code == 200
        runner_id = uuid.UUID(resp.json()["runner_id"])

        patch = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner_id}/capabilities",
            json={
                "gpu_indices_allowed": [0],
                "max_concurrent": 8,  # must still be clamped to 1
                "task_types": "all",  # must still be clamped to strategic set
            },
        )
        assert patch.status_code == 200
        caps = patch.json()["capabilities"]
        assert caps["gpu_indices_allowed"] == [0]
        assert caps["max_concurrent"] == 1
        assert caps["task_types"] == sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES)
        # The config block reflects the clamped values, not the requested ones.
        assert patch.json()["config"]["desired"]["max_concurrent"] == 1

    @pytest.mark.asyncio
    async def test_gpus_per_job_pushed_via_heartbeat_and_applied_echo(
        self, client: AsyncClient, db_session
    ):
        """An operator PATCH of gpus_per_job flows down the heartbeat config block,
        and the daemon's applied_gpus_per_job echo resolves config_pending."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
        )

        # Operator pushes a per-job GPU count of 2.
        patch = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/capabilities",
            json={"gpus_per_job": 2},
        )
        assert patch.status_code == 200
        assert patch.json()["capabilities"]["gpus_per_job"] == 2

        # The daemon sees it in the heartbeat response config block.
        hb = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert hb.status_code == 200
        config = hb.json()["config"]
        assert config["gpus_per_job"] == 2
        desired_gen = config["config_generation"]

        # Before an applied echo, the view is pending.
        view0 = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert view0["config"]["config_pending"] is True

        # Daemon echoes the applied per-job count with the matching generation.
        await client.post(
            "/api/runner/heartbeat",
            json={
                "active_task_ids": [],
                "applied_config_generation": desired_gen,
                "applied_max_concurrent": 2,
                "applied_gpu_indices_allowed": "all",
                "applied_gpus_per_job": 2,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        view1 = (await client.get(f"/api/admin/projects/{project.id}/runners")).json()[0]
        assert view1["config"]["applied"]["gpus_per_job"] == 2
        assert view1["config"]["config_pending"] is False


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
    async def test_draining_runner_does_not_pull_new_task(
        self, client: AsyncClient, db_session
    ):
        """Drain prevents a race from handing new agent work to the host."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session, project.id, status="draining"
        )
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "title": "must stay unpulled"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task)
        await db_session.commit()

        response = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 204
        await db_session.refresh(task)
        assert task.started_at is None
        assert task.status == TaskStatus.PROVISIONING

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

        # When no LLM account is resolved, the handler returns 503 with
        # pool_exhausted=True and never sets the dispatch marker, so the task
        # stays claimable by a future poll cycle. The body also carries WHY.
        assert resp.status_code == 503
        body = resp.json()
        assert body["pool_exhausted"] is True
        assert "reason" in body
        # A plain no-account outcome is NOT flagged as a credential crash.
        assert "credential_resolution_failed" not in body

        # Task context was NOT stamped since select_llm_runtime_for_task returned None.
        await db_session.refresh(task_instance)
        assert task_instance.llm_account_id is None  # not stamped
        assert task_instance.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_next_task_reconciles_model_to_selected_account_provider(
        self, client: AsyncClient, db_session
    ):
        """Local dispatch must apply the same final identity guard as cloud."""
        from app.max_plan_pool import LLMSelectionResult

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(task)
        await db_session.commit()

        selection = LLMSelectionResult(
            provider="anthropic",
            auth_mode="api_key",
            model="claude-sonnet-4-6",
            quality_tier="sonnet",
            account={
                "id": "acct-openai",
                "provider": "openai",
                "auth_mode": "oauth",
                "runtime_secret": "openai-oauth-token",
                "provider_metadata": {},
            },
            settings={},
        )
        runtime_config = {
            "provider_selection_mode": "exclusive",
            "provider_tier_model_map": {
                "anthropic": {"sonnet": "claude-sonnet-4-6"},
                "openai": {"sonnet": "gpt-5.6-terra"},
            },
        }
        with patch("app.shared_state.runtime_config", new=runtime_config), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=selection),
        ):
            response = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200
        assert response.json()["credentials"]["env"]["PROVIDER"] == "openai"
        await db_session.refresh(task)
        assert task.llm_account_id == "acct-openai"
        assert task.context["llm_provider"] == "openai"
        assert task.context["llm_auth_mode"] == "oauth"
        assert task.context["llm_model"] == "gpt-5.6-terra"

    @pytest.mark.asyncio
    async def test_next_task_rolls_back_dispatch_on_credential_exception(
        self, client: AsyncClient, db_session
    ):
        """When an unexpected exception occurs during credential resolution,
        next-task must leave the dispatch marker unset (started_at = None) and
        503 so the task stays claimable rather than being dispatched without
        credentials — AND must say so distinctly (credential_resolution_failed),
        because a poisoned task that crashes _resolve_provider_materials used to
        return the identical {"pool_exhausted": true} body as genuine rate
        limiting, sending operators after the wrong cause."""
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

        # 503 so the runner backs off rather than receiving a task it can't
        # execute — but with a DISTINCT machine-readable cause and the task id,
        # not the same body a rate-limited pool returns.
        assert resp.status_code == 503
        body = resp.json()
        assert body["credential_resolution_failed"] is True
        # The offending task id is NESTED under "error", never at top level: a
        # top-level "task_id" in a 503 body is one missed check away from an
        # un-upgraded runner treating an error as a dispatched task.
        assert body["error"]["code"] == "credential_resolution_failed"
        assert body["error"]["skipped_task_id"] == str(task_instance.id)
        assert "task_id" not in body
        assert "pool connection lost" in body["reason"]
        # pool_exhausted stays True for back-compat: field-deployed runners gate
        # their back-off (and capacity-permit release) on exactly that key.
        assert body["pool_exhausted"] is True

        # Task context was NOT stamped and the dispatch marker was never set.
        await db_session.refresh(task_instance)
        assert task_instance.llm_account_id is None
        assert task_instance.status == TaskStatus.PROVISIONING  # unchanged
        # started_at must be None — task is still claimable.
        assert task_instance.started_at is None
        # The WHY is stamped on the task itself, so it shows in the admin UI.
        assert "credential_resolution_failed" in (
            task_instance.latest_status_message or ""
        )

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
            body = resp.json()
            assert body["pool_exhausted"] is True
            # The 503 carries WHY the pool came up empty, so an operator does not
            # have to reconstruct it from pool state.
            assert body["reason"]

            # started_at must NOT be set — the task was not dispatched.
            await db_session.refresh(task)
            assert task.started_at is None
            assert task.status == TaskStatus.PROVISIONING
        finally:
            _clear_pool()

    @staticmethod
    def _pinned(project_id, runner_id, *, task_type="create", **kwargs) -> Task:
        """A task pinned to `runner_id` and awaiting a /next-task pull."""
        return Task(
            project_id=project_id,
            payloads=[{"type": task_type}],
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner_id}",
            context={"runner_target_id": str(runner_id)},
            **kwargs,
        )

    @staticmethod
    def _selection(account_id: str = "acct-002"):
        from app.max_plan_pool import LLMSelectionResult

        return LLMSelectionResult(
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            quality_tier="opus",
            account={
                "id": account_id,
                "oauth_token": "sk-ant-test-token",
                "runtime_secret": "sk-ant-test-token",
                "claude_config_json": None,
                "provider_metadata": {},
            },
            settings={},
        )

    @pytest.mark.asyncio
    async def test_next_task_skips_unroutable_head_and_dispatches_next(
        self, client: AsyncClient, db_session
    ):
        """An unroutable HEAD task must not starve the routable tasks behind it.

        Regression for the 2026-07-11 PROVISIONING pileup: /next-task selected ONE
        candidate and 503'd if it couldn't get an account. Routing is per-task
        (task type / model pin / runner CLI inventory), so a single task pinned to
        a provider in cooldown blocked every other pinned task — and the head is
        reaper-exempt, so nothing ever cleared it. A healthy runner starved
        indefinitely behind one bad task.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=10)
        head = self._pinned(
            project.id, runner.id, task_type="fix", created_at=base
        )
        follower = self._pinned(
            project.id,
            runner.id,
            task_type="review",
            created_at=base + timedelta(minutes=1),
        )
        db_session.add_all([head, follower])
        await db_session.commit()

        async def _select(**kwargs):
            if kwargs["task"].id == head.id:
                kwargs["failure_reasons"].append(
                    "deepseek/deepseek-v4: model in rate-limit cooldown (240s left)"
                )
                return None
            return self._selection()

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_select),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # The routable follower dispatches; the unroutable head is skipped, not fatal.
        assert resp.status_code == 200
        assert resp.json()["task_id"] == str(follower.id)

        # Head is untouched and still claimable by a later poll...
        await db_session.refresh(head)
        assert head.started_at is None
        assert head.llm_account_id is None
        assert head.status == TaskStatus.PROVISIONING
        # ...but now carries the WHY, so it isn't a silent skip.
        assert "cooldown" in (head.latest_status_message or "")
        assert head.context["runner_pull_skip"]["code"] == "no_eligible_account"

        # Follower really was dispatched.
        await db_session.refresh(follower)
        assert follower.started_at is not None
        assert follower.llm_account_id == "acct-002"

    @pytest.mark.asyncio
    async def test_next_task_503_when_every_candidate_unroutable_and_carries_reasons(
        self, client: AsyncClient, db_session
    ):
        """When NO candidate can be routed, 503 — but carrying each task's reason."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        t1 = self._pinned(project.id, runner.id, task_type="fix")
        t2 = self._pinned(project.id, runner.id, task_type="plan")
        db_session.add_all([t1, t2])
        await db_session.commit()

        async def _select(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_select),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["pool_exhausted"] is True
        assert "all accounts at cap" in body["reason"]
        # Every candidate is accounted for individually, not collapsed into one bit.
        assert {s["task_id"] for s in body["skipped"]} == {str(t1.id), str(t2.id)}
        assert all(s["code"] == "no_eligible_account" for s in body["skipped"])

        for task in (t1, t2):
            await db_session.refresh(task)
            assert task.started_at is None
            assert task.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_next_task_honors_priority_over_created_at(
        self, client: AsyncClient, db_session
    ):
        """/next-task must use the SAME ordering as every cloud dispatch path
        (priority DESC, created_at ASC). Ordering by created_at alone made the
        pull-based fleet ignore priority entirely: a high-priority task pinned to
        a runner lost to any older task."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=10)
        old_low = self._pinned(
            project.id, runner.id, created_at=base, priority=10
        )
        new_high = self._pinned(
            project.id,
            runner.id,
            created_at=base + timedelta(minutes=5),
            priority=90,
        )
        db_session.add_all([old_low, new_high])
        await db_session.commit()

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=self._selection()),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        # The NEWER, higher-priority task wins over the older low-priority one.
        assert resp.json()["task_id"] == str(new_high.id)

    # ── Skip escalation: a bounded scan is only safe if skips have a consumer ──

    @staticmethod
    def _with_skip_marker(task: Task, *, count: int, age_seconds: int = 600) -> Task:
        """Pre-age a runner_pull_skip marker so the next skip re-stamps it.

        ``age_seconds`` must exceed SKIP_RESTAMP_SECONDS (300) so stamp_skip_reason
        does not throttle, and SKIP_RESOLVE_TTL_SECONDS (30) so the candidate is
        actually re-resolved rather than short-circuited.
        """
        ctx = dict(task.context or {})
        stamped = (_utcnow() - timedelta(seconds=age_seconds)).isoformat()
        ctx["runner_pull_skip"] = {
            "code": "no_eligible_account",
            "reason": "all accounts at cap",
            "at": stamped,
            "first_at": stamped,
            "count": count,
        }
        task.context = ctx
        return task

    @pytest.mark.asyncio
    async def test_repeated_skips_raise_operator_alert(
        self, client: AsyncClient, db_session
    ):
        """A skip counter with NO consumer re-creates the P0 one row further down.

        Nothing unpins, fails or reaps a permanently-unroutable pinned task — the
        head is reaper-exempt and no sweep clears it. So a bounded candidate scan
        of N means N+1 poisoned pinned tasks starve the runner exactly as before,
        SILENTLY. /decline-task already escalates at 3; the pull skip must too.
        """
        from app.models import Alert
        from app.runner_pull_skip import SKIP_ALERT_THRESHOLD, SKIP_ALERT_TITLE

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        # One short of the alert threshold: this poll's skip crosses it.
        poisoned = self._with_skip_marker(
            self._pinned(project.id, runner.id), count=SKIP_ALERT_THRESHOLD - 1
        )
        db_session.add(poisoned)
        await db_session.commit()

        async def _no_account(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_no_account),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503

        await db_session.refresh(poisoned)
        assert poisoned.context["runner_pull_skip"]["count"] == SKIP_ALERT_THRESHOLD
        # Below the unpin threshold, so the task is still pinned — but the operator
        # now KNOWS, instead of the counter incrementing into the void.
        assert poisoned.status == TaskStatus.PROVISIONING

        alerts = (
            await db_session.execute(
                select(Alert).where(
                    Alert.task_id == poisoned.id, Alert.title == SKIP_ALERT_TITLE
                )
            )
        ).scalars().all()
        assert len(alerts) == 1, "repeated skips must escalate to an operator"
        assert "all accounts at cap" in alerts[0].message

    @pytest.mark.asyncio
    async def test_skip_past_threshold_unpins_task_back_to_queued(
        self, client: AsyncClient, db_session
    ):
        """Past the unpin threshold the task returns to QUEUED for re-dispatch.

        This is what makes the poisoned set SELF-DRAINING. Without it, enough
        permanently-unroutable pinned tasks permanently fill the candidate scan
        window and the original head-of-line starvation returns — the scan bound
        would only have MOVED the boundary, not removed it.
        """
        from app.runner_pull_skip import SKIP_UNPIN_THRESHOLD

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        poisoned = self._with_skip_marker(
            self._pinned(project.id, runner.id), count=SKIP_UNPIN_THRESHOLD - 1
        )
        db_session.add(poisoned)
        await db_session.commit()

        async def _no_account(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_no_account),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503

        await db_session.refresh(poisoned)
        # Back to QUEUED, pin cleared — the orchestrator will re-dispatch it
        # (other hardware, or cloud), and it no longer occupies a scan slot.
        assert poisoned.status == TaskStatus.QUEUED
        assert poisoned.vm_instance_id is None
        assert "runner_target_id" not in (poisoned.context or {})
        # The marker is DROPPED, not zeroed: a zeroed marker would leave seen_at
        # recent, so a re-pin to the same runner inside the TTL would short-circuit
        # a task we specifically want re-resolved on arrival — and has_skip_marker
        # would keep deprioritising it in the resolve order forever.
        assert "runner_pull_skip" not in (poisoned.context or {})
        # The audit trail survives in its own key, where it cannot make a drained
        # task look "already examined".
        assert poisoned.context["runner_pull_unpins"] == 1

    @pytest.mark.asyncio
    async def test_fresh_skip_marker_short_circuits_credential_resolution(
        self, client: AsyncClient, db_session
    ):
        """A candidate proved unroutable seconds ago is NOT re-resolved.

        Credential resolution does a Project roundtrip AND acquires the global
        _max_plan_pool_lock. Re-running it for every pinned candidate on every
        poll of every runner — which is exactly what a batch scan would do —
        multiplies pool-lock traffic precisely during the pool-exhaustion storm
        this endpoint exists to survive. The marker we already write is read back
        to suppress the repeat work, and the cached WHY is still reported.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        # Marker is FRESH (5s old, inside SKIP_RESOLVE_TTL_SECONDS=30).
        fresh = self._with_skip_marker(
            self._pinned(project.id, runner.id), count=1, age_seconds=5
        )
        db_session.add(fresh)
        await db_session.commit()

        select_mock = AsyncMock(return_value=None)
        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch("app.max_plan_pool.select_llm_runtime_for_task", new=select_mock):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503
        select_mock.assert_not_awaited()  # the pool lock was never taken
        # ...and the runner still learns WHY, from the cached marker.
        body = resp.json()
        assert body["skipped"][0]["code"] == "no_eligible_account"
        assert "all accounts at cap" in body["reason"]

    @pytest.mark.asyncio
    async def test_abandoned_resolution_refunds_daily_quota_not_just_active_slot(
        self, client: AsyncClient, db_session
    ):
        """A dispatch that never happened must not burn a unit of DAILY quota.

        `select_max_plan_account` pre-increments BOTH `active_tasks` AND
        `daily_task_count`, and `_account_at_cap` gates on
        `daily_task_count >= daily_task_cap`. Releasing only the active slot leaves
        the daily counter permanently incremented, so every credential-resolution
        crash and every lost row-lock race silently eats the account's day. The
        canonical peer (orchestrator_provision) passes revert_daily_task_count=True.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task = self._pinned(project.id, runner.id)
        db_session.add(task)
        await db_session.commit()

        release_mock = AsyncMock()
        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=self._selection()),
        ), patch(
            # Materials-building blows up AFTER an account was selected (and thus
            # after both counters were pre-incremented).
            "app.compute_manifest._resolve_provider_materials",
            new=MagicMock(side_effect=RuntimeError("boom")),
        ), patch(
            "app.max_plan_pool.release_max_plan_account", new=release_mock
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503
        release_mock.assert_awaited_once()
        assert release_mock.await_args.kwargs.get("revert_daily_task_count") is True, (
            "the reserved account was released without refunding daily_task_count — "
            "a dispatch that never happened just burned a unit of daily quota"
        )

    @pytest.mark.asyncio
    async def test_lost_row_lock_race_refunds_daily_quota(
        self, client: AsyncClient, db_session
    ):
        """The OTHER release site: a candidate resolved but lost the winner row lock.

        `test_abandoned_resolution_refunds_daily_quota...` only covers the exception
        path inside _resolve_pool_credentials_for_dispatch. This covers
        _release_dispatch_account — the lost-race refund — which stayed green when
        its kwarg was reverted, i.e. it was untested and a refactor could drop it
        silently. Both counters are pre-incremented by the selector, so a dispatch
        that never happened must refund BOTH.

        The race is forced deterministically: account selection commits
        ``started_at`` through a second session (as a concurrent poll would), so
        the winner re-lock — which re-checks `started_at IS NULL` — finds nothing.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task = self._pinned(project.id, runner.id)
        db_session.add(task)
        await db_session.commit()

        Session = async_sessionmaker(
            db_session.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async def _select_and_steal(**kwargs):
            # A concurrent poll wins the row between our unlocked read and our
            # re-lock. Persist through another identity map: mutating the
            # request's ORM object alone is not flushed by the test session and
            # therefore does not simulate a real competing transaction.
            async with Session() as other_session:
                stolen = await other_session.get(Task, kwargs["task"].id)
                assert stolen is not None
                stolen.started_at = _utcnow()
                other_session.add(stolen)
                await other_session.commit()
            return self._selection()

        release_mock = AsyncMock()
        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_select_and_steal),
        ), patch(
            "app.max_plan_pool.release_max_plan_account", new=release_mock
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # We resolved an account but never dispatched it — so it must be handed back.
        assert resp.status_code == 503
        release_mock.assert_awaited_once()
        assert release_mock.await_args.kwargs.get("revert_daily_task_count") is True, (
            "a candidate that lost the winner row-lock race released its active "
            "slot but NOT its daily_task_count — every lost race permanently burns "
            "a unit of the account's daily quota"
        )

    @pytest.mark.asyncio
    async def test_successful_dispatch_clears_the_skip_marker(
        self, client: AsyncClient, db_session
    ):
        """The marker must not outlive its episode.

        Left in place it is immortal: `count` persists in task.context across
        SEPARATE episodes, so a task that hits a transient cooldown on five
        different occasions — days apart, each recovering on its own — accumulates
        count=5 and is eventually UNPINNED for no reason. `has_skip_marker` would
        also stay True forever, permanently deprioritising a healthy task.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        # Carries a stale marker from an earlier cooldown that has since lifted.
        task = self._with_skip_marker(
            self._pinned(project.id, runner.id), count=2, age_seconds=600
        )
        db_session.add(task)
        await db_session.commit()

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(return_value=self._selection()),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        await db_session.refresh(task)
        assert "runner_pull_skip" not in (task.context or {}), (
            "the skip marker survived a successful dispatch — its count will carry "
            "into unrelated future episodes and eventually unpin a healthy task"
        )

    @pytest.mark.asyncio
    async def test_skip_marker_from_another_runner_is_not_trusted(
        self, client: AsyncClient, db_session
    ):
        """Routability is RUNNER-dependent, so a marker is only valid for its author.

        `runner_available_clis` feeds select_llm_runtime_for_task, so runner A's
        "unroutable" says nothing about runner B. A task unpinned from A and
        re-pinned to B carries A's marker — and if B trusted it, B would
        short-circuit (and refuse to run) a task it is perfectly capable of running.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        # A FRESH marker (well inside the TTL) — but written by a different runner.
        task = self._pinned(project.id, runner.id)
        ctx = dict(task.context or {})
        ctx["runner_pull_skip"] = {
            "code": "no_eligible_account",
            "reason": "runner A has no CLI for this provider",
            "runner_id": str(uuid.uuid4()),  # NOT this runner
            "seen_at": _utcnow().isoformat(),
            "at": _utcnow().isoformat(),
            "first_at": _utcnow().isoformat(),
            "count": 1,
        }
        task.context = ctx
        db_session.add(task)
        await db_session.commit()

        select_mock = AsyncMock(return_value=self._selection())
        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},
        ), patch("app.max_plan_pool.select_llm_runtime_for_task", new=select_mock):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # This runner must resolve for ITSELF, not inherit the other runner's verdict.
        select_mock.assert_awaited_once()
        assert resp.status_code == 200
        assert resp.json()["task_id"] == str(task.id)

    @pytest.mark.asyncio
    async def test_second_poll_short_circuits_a_task_it_just_resolved(
        self, client: AsyncClient, db_session
    ):
        """Two REAL polls: the second must not re-resolve what the first proved bad.

        This is the load-bearing claim of the whole batch scan — "an examined-and-
        failed candidate carries a fresh marker, so the next poll short-circuits it
        and spends its budget further down the list". If it is false, the resolve
        budget is consumed re-proving the same head candidates forever and the P0 is
        merely re-bounded from 1 to `resolve_attempts`, not removed.

        The state that breaks it is the THROTTLED re-stamp: `stamp_skip_reason` only
        writes its timestamp when it also increments `count` (once per 300s), but
        freshness is judged against that same timestamp on a 30s TTL — and the runner
        backs its poll interval off to exactly 30s on every 503. So the marker is
        stale at exactly the poll cadence and the short-circuit never fires. A test
        that hand-writes a 5s-old marker cannot see this: that state only exists in
        the 30s after a restamp.

        Setup reproduces it: BOTH tasks already carry a marker OLDER than the resolve
        TTL but YOUNGER than the restamp window, so the next stamp takes the
        THROTTLED path (the one that never refreshed the timestamp). T2's model has
        since recovered, so it is routable now. Budget is 1, so if poll 2 wastes it
        re-resolving T1, the routable T2 is never examined and the runner starves.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=10)
        # T1: still unroutable; skipped 100s ago (> TTL=90, < restamp=300).
        t1 = self._with_skip_marker(
            self._pinned(project.id, runner.id, created_at=base),
            count=1,
            age_seconds=100,
        )
        # T2: skipped at the same time, but its model has since recovered — it is
        # routable now. Same stale-marker class as T1, so ordering between them is
        # by priority/created_at, not by examined-vs-unexamined.
        t2 = self._with_skip_marker(
            self._pinned(
                project.id, runner.id, created_at=base + timedelta(seconds=1)
            ),
            count=1,
            age_seconds=100,
        )
        db_session.add_all([t1, t2])
        await db_session.commit()

        resolved_ids: list[str] = []

        async def _select(**kwargs):
            resolved_ids.append(str(kwargs["task"].id))
            if kwargs["task"].id == t1.id:
                kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
                return None
            return self._selection()

        cfg = {
            "provider_selection_mode": "exclusive",
            "runner_next_task_resolve_attempts": 1,  # one resolution per poll
        }

        with patch("app.shared_state.runtime_config", new=cfg), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_select),
        ):
            first = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            # Poll 1 spends its single attempt on T1, fails, and marks it.
            assert first.status_code == 503
            assert resolved_ids == [str(t1.id)]

            second = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # Poll 2 MUST short-circuit T1 on its fresh marker and spend its budget on
        # T2 — which is routable, so the runner finally gets work. If T1 is
        # re-resolved instead, T2 is never examined and the runner starves.
        assert str(t1.id) not in resolved_ids[1:], (
            "poll 2 re-resolved a task poll 1 had just proved unroutable — the "
            "skip short-circuit is dead and the resolve budget is being burned "
            "re-proving the same head candidates"
        )
        assert second.status_code == 200
        assert second.json()["task_id"] == str(t2.id)

    @pytest.mark.asyncio
    async def test_unexamined_candidates_win_the_budget_over_stale_reprobes(
        self, client: AsyncClient, db_session
    ):
        """The scan window must SLIDE, not oscillate over the head.

        Even with freshness fixed, markers expire (TTL). If an expired head
        candidate outranks a never-examined one for the resolve budget, the poll
        cycles re-probing T1..T3 and never reaches the routable task deeper in the
        scan — starvation returns, just at a larger bound. So a candidate with NO
        marker (never examined) takes the budget before a candidate whose marker
        merely went stale.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=10)
        # Two already-examined-but-STALE unroutable heads (marker older than TTL).
        stale = [
            self._with_skip_marker(
                self._pinned(
                    project.id, runner.id, created_at=base + timedelta(seconds=i)
                ),
                count=1,
                age_seconds=600,
            )
            for i in range(2)
        ]
        # A never-examined routable task behind them.
        fresh_routable = self._pinned(
            project.id, runner.id, created_at=base + timedelta(seconds=10)
        )
        db_session.add_all([*stale, fresh_routable])
        await db_session.commit()

        stale_ids = {str(s.id) for s in stale}

        async def _select(**kwargs):
            if str(kwargs["task"].id) in stale_ids:
                kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
                return None
            return self._selection()

        cfg = {
            "provider_selection_mode": "exclusive",
            "runner_next_task_resolve_attempts": 1,  # budget of ONE
        }
        with patch("app.shared_state.runtime_config", new=cfg), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_select),
        ):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        # The single attempt went to the NEVER-EXAMINED task, not to re-proving a
        # stale head — so the runner dispatches instead of starving.
        assert resp.status_code == 200
        assert resp.json()["task_id"] == str(fresh_routable.id)

    @pytest.mark.asyncio
    async def test_undatable_marker_never_500s_the_dispatch_endpoint(
        self, client: AsyncClient, db_session
    ):
        """A marker with no parseable `seen_at` must SORT FIRST, not crash the poll.

        `skip_seen_at` is used directly as a `sorted()` key over the stale group. If
        its "cannot date this marker" sentinel is tz-AWARE while every real marker
        parses NAIVE (`_utcnow()` / TIMESTAMP WITHOUT TIME ZONE), then the moment one
        undatable marker shares the stale group with one datable marker:

            TypeError: can't compare offset-naive and offset-aware datetimes

        — an unhandled 500 on the pull fleet's dispatch endpoint, which is NOT the
        503 the runner knows how to back off from.

        The state is explicitly legal: `skip_marker_is_fresh` falls back to `at` for
        markers predating `seen_at`, the helper below writes exactly that shape, and
        operators hand-PATCH `task.context` during the very incidents this endpoint
        exists to survive. It only bites at scale — the undatable marker must survive
        long enough to MEET a stale datable one, which needs more never-examined
        candidates ahead of it than the resolve budget clears. So: the default scan.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=30)
        # One UNDATABLE marker (no `seen_at`, and an `at` that will not parse)...
        undatable = self._pinned(project.id, runner.id, created_at=base)
        undatable.context = {
            **(undatable.context or {}),
            "runner_pull_skip": {
                "code": "no_eligible_account",
                "reason": "hand-patched by an operator mid-incident",
                "at": "not-a-timestamp",
                "count": 1,
            },
        }
        db_session.add(undatable)
        # ...alongside enough datable candidates that the two must meet in the
        # stale group (more than the ~9 the resolve budget clears per rotation).
        for i in range(1, 25):
            db_session.add(
                self._pinned(
                    project.id, runner.id, created_at=base + timedelta(seconds=i)
                )
            )
        await db_session.commit()

        async def _no_account(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        clock = {"t": _utcnow()}

        def _now():
            return clock["t"]

        with patch(
            "app.shared_state.runtime_config",
            new={"provider_selection_mode": "exclusive"},  # DEFAULT scan/attempts
        ), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_no_account),
        ), patch("app.routers.runner._utcnow", new=_now), patch(
            "app.runner_pull_skip._utcnow", new=_now
        ):
            for _poll in range(8):
                clock["t"] = clock["t"] + timedelta(seconds=30)
                resp = await client.post(
                    "/api/runner/next-task",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 503, (
                    f"/next-task returned {resp.status_code}, not 503 — an undatable "
                    "skip marker crashed the sort. A 500 is not something the runner "
                    "can back off from; this takes the pull fleet down."
                )

    @pytest.mark.asyncio
    async def test_steady_state_rotation_never_freezes_a_candidate(
        self, client: AsyncClient, db_session
    ):
        """STEADY STATE, many polls: no candidate may be examined once and abandoned.

        Unexamined-first fixes the initial SLIDE. But once every candidate carries a
        marker, the stale group IS the whole list — and if that group is drained in
        PRIORITY order it resonates with the TTL: exactly `attempts` markers come due
        per poll, so the rotation locks onto the first `attempts * TTL/poll` = 9
        candidates forever. Candidates 10+ are examined ONCE and frozen:

          * never re-probed  => if one becomes routable it is NEVER dispatched;
          * `count` frozen at 1 => never reaches the alert (3) or unpin (5) threshold
            => never alerts, never drains, holds a candidate slot permanently.

        That is verbatim the failure the escalation exists to close: N is bounded by
        the scan (25), but the EFFECTIVE N would be 9. A 3-candidate single-poll test
        only ever exercises the slide phase and cannot see this — so this one runs the
        real endpoint over many polls at the runner's real 30s backoff cadence, on a
        virtual clock, and asserts that every candidate keeps making progress.
        """
        from app.runner_pull_skip import skip_count

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=30)
        tasks = [
            self._pinned(
                project.id, runner.id, created_at=base + timedelta(seconds=i)
            )
            for i in range(16)  # > the 9-candidate resonance bound
        ]
        db_session.add_all(tasks)
        await db_session.commit()
        task_ids = [t.id for t in tasks]

        async def _no_account(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        cfg = {
            "provider_selection_mode": "exclusive",
            "runner_next_task_resolve_attempts": 3,
            "runner_next_task_candidate_scan": 25,
        }

        # Virtual clock: the runner backs off to 30s on every 503, so that is the
        # real steady-state cadence. 24 polls = 720s, long enough for the escalation
        # clock (300s) to advance every candidate that is actually being re-probed,
        # and short enough that nothing reaches the unpin threshold (5) yet.
        clock = {"t": _utcnow()}

        def _now():
            return clock["t"]

        with patch("app.shared_state.runtime_config", new=cfg), patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new=AsyncMock(side_effect=_no_account),
        ), patch("app.routers.runner._utcnow", new=_now), patch(
            "app.runner_pull_skip._utcnow", new=_now
        ):
            for poll in range(24):
                clock["t"] = clock["t"] + timedelta(seconds=30)
                resp = await client.post(
                    "/api/runner/next-task",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 503

        # Every candidate must still be making progress: re-probed often enough that
        # its escalation counter advanced past the first sighting. A candidate stuck
        # at count==1 after 24 polls was examined once and abandoned — it will never
        # dispatch, never alert, and never drain.
        frozen = []
        for tid in task_ids:
            row = (
                await db_session.execute(select(Task).where(Task.id == tid))
            ).scalars().first()
            await db_session.refresh(row)
            if row.status == TaskStatus.PROVISIONING and skip_count(row) <= 1:
                frozen.append(str(tid))

        assert not frozen, (
            f"{len(frozen)} of {len(task_ids)} candidates were examined once and "
            f"then FROZEN — never re-probed, never escalated, never drained. The "
            f"stale-group rotation is resonating with the TTL and has locked onto "
            f"the head of the list: {frozen}"
        )

    @pytest.mark.asyncio
    async def test_resolution_attempts_are_capped_per_poll(
        self, client: AsyncClient, db_session
    ):
        """One poll runs at most `runner_next_task_resolve_attempts` resolutions.

        The batch scan must not turn one poll into N credential resolutions (N
        global-pool-lock acquisitions). The budget is spent, the examined
        candidates get fresh skip markers, and the NEXT poll's budget slides down
        the list — so work stays bounded without head-of-line blocking returning.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        base = _utcnow() - timedelta(minutes=10)
        for i in range(6):
            db_session.add(
                self._pinned(
                    project.id, runner.id, created_at=base + timedelta(seconds=i)
                )
            )
        await db_session.commit()

        async def _no_account(**kwargs):
            kwargs["failure_reasons"].append("anthropic/opus: all accounts at cap")
            return None

        select_mock = AsyncMock(side_effect=_no_account)
        with patch(
            "app.shared_state.runtime_config",
            new={
                "provider_selection_mode": "exclusive",
                "runner_next_task_resolve_attempts": 2,
            },
        ), patch("app.max_plan_pool.select_llm_runtime_for_task", new=select_mock):
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 503
        # 6 candidates pinned, but only the configured budget was resolved.
        assert select_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_decline_records_reason_and_counter(
        self, client: AsyncClient, db_session
    ):
        """A decline must be RECORDED (reason + per-runner counter), not just logged.

        Without a record the dispatcher has nothing to exclude on, and since
        `_select_eligible_runner`'s tiebreak (`r.id.int ^ dispatch_seed`) is stable
        per task, it deterministically re-picks the runner that just declined —
        pin → pull → decline, forever.
        """
        from app.runner_decline import recently_declined_runner_ids

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task = self._pinned(project.id, runner.id, started_at=_utcnow())
        db_session.add(task)
        await db_session.commit()

        resp = await client.post(
            "/api/runner/decline-task",
            json={"task_id": str(task.id), "reason": "no bwrap: fail-closed"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["decline_count"] == 1

        await db_session.refresh(task)
        # Released back to QUEUED as before...
        assert task.status == TaskStatus.QUEUED
        assert "runner_target_id" not in (task.context or {})
        # ...but now the WHY and WHO are persisted.
        record = task.context["declined_by"][str(runner.id)]
        assert record["count"] == 1
        assert record["reason"] == "no bwrap: fail-closed"
        assert "no bwrap" in (task.latest_status_message or "")

        # And the dispatcher will now exclude this runner for THIS task.
        assert recently_declined_runner_ids(task) == {str(runner.id)}

    @pytest.mark.asyncio
    async def test_declining_runner_not_reselected_and_escalates(
        self, client: AsyncClient, db_session
    ):
        """The declining runner is excluded from re-selection for that task, and
        crossing the decline threshold raises an operator alert carrying the reason."""
        from app.runner_decline import (
            DECLINE_ESCALATION_THRESHOLD,
            recently_declined_runner_ids,
        )

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        task = self._pinned(project.id, runner.id, started_at=_utcnow())
        db_session.add(task)
        await db_session.commit()

        for i in range(DECLINE_ESCALATION_THRESHOLD):
            # Re-pin as the dispatch loop would, then decline again. Refresh
            # first: the endpoint commits on its own session, so our copy is
            # stale and re-assigning the same in-memory values would emit no
            # UPDATE at all.
            await db_session.refresh(task)
            task.status = TaskStatus.PROVISIONING
            task.vm_instance_id = f"runner:{runner.id}"
            ctx = dict(task.context or {})
            ctx["runner_target_id"] = str(runner.id)
            task.context = ctx
            flag_modified(task, "context")
            db_session.add(task)
            await db_session.commit()

            resp = await client.post(
                "/api/runner/decline-task",
                json={"task_id": str(task.id), "reason": "docker is read-only"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert resp.json()["decline_count"] == i + 1

        await db_session.refresh(task)

        # 1. The declining runner is excluded, so `_select_eligible_runner` — whose
        #    tiebreak would otherwise re-pick it every single time — returns None
        #    rather than looping the task straight back to the same box.
        snap = orch.RunnerDispatchSnapshot(
            id=runner.id, project_id=project.id, capabilities={"task_types": "all"}
        )
        cache = {project.id: [snap]}
        assert (
            orch._select_eligible_runner(
                cache,
                project.id,
                "create",
                None,
                {},
                dispatch_seed=task.id.int,
                declined_runner_ids=recently_declined_runner_ids(task),
            )
            is None
        )
        # Sanity: without the exclusion the SAME runner is picked right back —
        # this is the livelock the exclusion breaks.
        assert (
            orch._select_eligible_runner(
                cache, project.id, "create", None, {}, dispatch_seed=task.id.int
            )
            is snap
        )

        # 2. The operator is told, with the runner's own reason — not left with a
        #    task silently cycling forever.
        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(Alert.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1  # deduplicated across repeated declines
        assert "docker is read-only" in alerts[0].message

    def test_decline_exclusion_expires_before_strategic_hold_forces_cloud(self):
        """The exclusion window MUST be shorter than the Strategic hold timeout.

        On the single-runner Strategic fleet the decliner is the ONLY
        Strategic-capable runner, so excluding it empties the candidate set,
        `_select_eligible_runner` returns None, `_hold_strategic_local_runner_task`
        parks the task — and at `strategic_local_hold_timeout_seconds` (default
        900s) `_fail_stale_local_runner_holds` clears the local-required pin and
        FORCES CLOUD FALLBACK.

        If the exclusion outlived that timeout (it was 1800s), one transient
        decline — a Popen spawn failure — would DETERMINISTICALLY evict a Resident
        Strategic Agent task off local execution, defeating continuous-local mode:
        the runner could not become eligible again until 15 min after the task had
        already been shipped to the cloud.
        """
        from app.runner_decline import DECLINE_EXCLUSION_SECONDS

        hold_timeout = orch._strategic_local_hold_timeout_seconds()
        assert hold_timeout == 900  # documented default; the bound below is vs this
        assert hold_timeout > DECLINE_EXCLUSION_SECONDS, (
            "a decline exclusion that outlives the Strategic hold timeout evicts "
            "the Resident Strategic Agent to cloud on a single transient decline"
        )

    def test_strategic_decline_exclusion_waived_rather_than_emptying_pool(self):
        """A transient decline must not cost a Strategic task its local execution.

        For a NORMAL task, emptying the candidate set is the graceful exit — the
        task falls through to cloud dispatch. For a local-required Strategic task
        it is not: an empty set parks the task and the 900s sweep evicts it to
        cloud. So while the declines still look transient we re-pick the decliner
        instead of emptying the pool — but ONLY while they look transient, and
        ONLY when no other hardware exists.
        """
        from app.runner_decline import (
            DECLINE_ESCALATION_THRESHOLD,
            decline_exclusion_is_advisory,
        )

        project_id = uuid.uuid4()
        runner_id = uuid.uuid4()
        snap = orch.RunnerDispatchSnapshot(
            id=runner_id, project_id=project_id, capabilities={"task_types": "all"}
        )
        cache = {project_id: [snap]}
        seed = uuid.uuid4().int

        def _task_with_declines(n: int) -> Task:
            at = _utcnow().isoformat()
            return Task(
                project_id=project_id,
                payloads=[{"type": "design_doc_update"}],  # a STRATEGIC task type
                status=TaskStatus.QUEUED,
                context={
                    "declined_by": {
                        str(runner_id): {"count": n, "at": at, "reason": "spawn failed"}
                    }
                },
            )

        # 1. ONE transient decline on a single-runner Strategic fleet: excluding
        #    the decliner would empty the pool, so the waiver keeps it. The task
        #    retries locally instead of being handed to the cloud.
        transient = _task_with_declines(1)
        assert decline_exclusion_is_advisory(transient, "design_doc_update") is True
        assert (
            orch._select_eligible_runner(
                cache,
                project_id,
                "design_doc_update",
                None,
                {},
                dispatch_seed=seed,
                declined_runner_ids={str(runner_id)},
                declined_exclusion_advisory=True,
            )
            is snap
        )

        # 2. Past the escalation threshold the decline is clearly NOT transient:
        #    the waiver stops, the pool empties, and the ordinary hold →
        #    cloud-fallback safety valve runs. Without this bound a persistently
        #    incapable single-runner fleet would livelock forever.
        persistent = _task_with_declines(DECLINE_ESCALATION_THRESHOLD)
        assert decline_exclusion_is_advisory(persistent, "design_doc_update") is False
        assert (
            orch._select_eligible_runner(
                cache,
                project_id,
                "design_doc_update",
                None,
                {},
                dispatch_seed=seed,
                declined_runner_ids={str(runner_id)},
                declined_exclusion_advisory=False,
            )
            is None
        )

        # 3. A NON-Strategic task never gets the waiver — emptying the pool is the
        #    correct outcome there (it falls through to cloud dispatch).
        normal = _task_with_declines(1)
        normal.payloads = [{"type": "create"}]
        assert decline_exclusion_is_advisory(normal, "create") is False

    def test_strategic_waiver_survives_an_ineligible_second_runner(self):
        """The waiver must key on ELIGIBILITY, not on a runner ROW merely existing.

        This is the LIVE Blueprint topology: a Strategic-capable laptop plus dgx-1,
        which does NOT accept `design_doc_update`. If the waiver asks "did any other
        candidate row survive the exclusion?", dgx-1 answers yes — so the exclusion
        is applied, dgx-1 then falls out on the task-type gate, `eligible` is empty,
        and the Resident Strategic Agent is parked and evicted to cloud by the 900s
        hold sweep on ONE transient decline. Precisely the eviction the waiver was
        written to prevent, defeated by a runner that could never have run the task.

        A single-runner cache cannot see this bug; it needs a second runner that
        exists but is incapable.
        """
        project_id = uuid.uuid4()
        laptop = uuid.uuid4()
        dgx = uuid.uuid4()
        cache = {
            project_id: [
                orch.RunnerDispatchSnapshot(
                    id=laptop,
                    project_id=project_id,
                    capabilities={"task_types": "all"},
                ),
                # Real dgx-1 shape: a compute box that does not take Strategic work.
                orch.RunnerDispatchSnapshot(
                    id=dgx,
                    project_id=project_id,
                    capabilities={"task_types": ["compute_experiment"]},
                ),
            ]
        }
        chosen = orch._select_eligible_runner(
            cache,
            project_id,
            "design_doc_update",
            None,
            {},
            dispatch_seed=uuid.uuid4().int,
            declined_runner_ids={str(laptop)},
            declined_exclusion_advisory=True,
        )
        assert chosen is not None, (
            "the Strategic task was handed to nobody because an INELIGIBLE runner "
            "row 'survived' the exclusion — it will now be evicted to cloud"
        )
        assert chosen.id == laptop

    def test_strategic_waiver_survives_a_busy_second_runner(self):
        """Same defect, other flavour: the second runner is capable but AT CAPACITY.

        A merely BUSY runner must not make the exclusion bite either — it cannot
        take the task this cycle, so the Strategic task would again be parked and
        evicted rather than retried on the (recovered) decliner.
        """
        project_id = uuid.uuid4()
        laptop = uuid.uuid4()
        busy = uuid.uuid4()
        cache = {
            project_id: [
                orch.RunnerDispatchSnapshot(
                    id=laptop,
                    project_id=project_id,
                    capabilities={"task_types": "all"},
                ),
                orch.RunnerDispatchSnapshot(
                    id=busy,
                    project_id=project_id,
                    capabilities={
                        "task_types": "all",
                        "max_concurrent": 1,
                        # Authoritative per-cycle load (see _runner_active_task_count).
                        "_active_agent_task_count": 1,
                    },
                ),
            ]
        }
        chosen = orch._select_eligible_runner(
            cache,
            project_id,
            "design_doc_update",
            None,
            {},
            dispatch_seed=uuid.uuid4().int,
            declined_runner_ids={str(laptop)},
            declined_exclusion_advisory=True,
        )
        assert chosen is not None
        assert chosen.id == laptop

    def test_strategic_waiver_never_overrides_available_other_hardware(self):
        """The waiver applies ONLY when the exclusion would empty the pool.

        Whenever other hardware exists the decline exclusion is honoured
        unconditionally — the whole point is to move the task OFF the box that
        just said no.
        """
        project_id = uuid.uuid4()
        decliner = uuid.uuid4()
        healthy = uuid.uuid4()
        cache = {
            project_id: [
                orch.RunnerDispatchSnapshot(
                    id=decliner,
                    project_id=project_id,
                    capabilities={"task_types": "all"},
                ),
                orch.RunnerDispatchSnapshot(
                    id=healthy,
                    project_id=project_id,
                    capabilities={"task_types": "all"},
                ),
            ]
        }
        chosen = orch._select_eligible_runner(
            cache,
            project_id,
            "design_doc_update",
            None,
            {},
            dispatch_seed=uuid.uuid4().int,
            declined_runner_ids={str(decliner)},
            declined_exclusion_advisory=True,  # advisory, but the set is NOT empty
        )
        assert chosen is not None
        assert chosen.id == healthy

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
        # ok:True as before; decline_count is an additive field (see the decline
        # bookkeeping that breaks the pin -> pull -> decline livelock).
        body = resp.json()
        assert body["ok"] is True
        assert body["decline_count"] == 1

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
    async def test_heartbeat_persists_and_bounds_image_cache_by_project(
        self, client: AsyncClient, db_session
    ):
        """Per-project image-cache accounting rides on caps, coerced and bounded."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        # Craft a report with an absurd byte value, a non-numeric count, and more
        # project buckets than the cap allows — all must be sanitized.
        absurd = 10 ** 60
        by_project = {f"proj-{i}": {"bytes": 1000, "image_count": 1} for i in range(70)}
        by_project["proj-bad"] = {"bytes": absurd, "image_count": "not-a-number"}

        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "image_cache_by_project": {
                    "total_bytes": absurd,
                    "image_count": 71,
                    "by_project": by_project,
                    "unattributed_bytes": -5,
                    "unattributed_image_count": 2,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        stored = runner.capabilities["image_cache_by_project"]
        # Absurd byte value clamped to the ceiling, not stored raw.
        assert stored["total_bytes"] == (1 << 50)
        assert stored["total_bytes"] < absurd
        # Negative coerced to 0; the project map is capped.
        assert stored["unattributed_bytes"] == 0
        assert len(stored["by_project"]) <= 64
        # Non-numeric count in a bucket degrades to 0, never raises.
        for bucket in stored["by_project"].values():
            assert isinstance(bucket["bytes"], int)
            assert isinstance(bucket["image_count"], int)

    @pytest.mark.asyncio
    async def test_heartbeat_tolerates_missing_image_cache_by_project(
        self, client: AsyncClient, db_session
    ):
        """A runner that reports no image cache leaves the field absent, no error."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"disk_free_bytes": 100},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert "image_cache_by_project" not in runner.capabilities

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
        passthrough = {
            "apptainer": {"verified": True, "mode": "nv", "reason": "verified"}
        }
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "version": "v22-15-gabc1234-dirty",
                "isolation_backends": inventory,
                "container_gpu_passthrough": passthrough,
                "agent_backend": "bwrap",
                "compute_backend": "bwrap",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.version == "v22-15-gabc1234-dirty"
        assert runner.capabilities["isolation_backends"] == inventory
        assert runner.capabilities["container_gpu_passthrough"] == passthrough
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
        assert "container_gpu_passthrough" not in runner.capabilities
        assert "agent_backend" not in runner.capabilities
        assert "compute_backend" not in runner.capabilities

    @pytest.mark.asyncio
    async def test_heartbeat_persists_llm_clis(
        self, client: AsyncClient, db_session
    ):
        """The heartbeat handler persists llm_clis in capabilities JSONB."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        llm_clis = {
            "claude": {"present": True, "version": "0.2.0"},
            "codex": {"present": False, "version": ""},
            "gemini": {"present": False, "version": ""},
        }
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "llm_clis": llm_clis,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.capabilities["llm_clis"] == llm_clis

    @pytest.mark.asyncio
    async def test_heartbeat_tolerates_missing_llm_clis(
        self, client: AsyncClient, db_session
    ):
        """An old runner that doesn't send llm_clis must not cause a heartbeat
        failure (backward compatibility)."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert "llm_clis" not in runner.capabilities

    @pytest.mark.asyncio
    async def test_heartbeat_persists_gpu_memory_and_cli_inventory(
        self, client: AsyncClient, db_session
    ):
        """Per-GPU VRAM lists + flat cli_inventory persist into capabilities.

        gpu_memory_total_mib is the key experiment_authoring_context's
        build_runner_device consumes for the devices block — the names must
        match exactly.
        """
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "gpu_memory_total_mib": [16384, 16384],
                "gpu_memory_free_mib": [15021, 830],
                "cli_inventory": {"claude": "2.1.0", "codex": None, "gemini": ""},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.capabilities["gpu_memory_total_mib"] == [16384, 16384]
        assert runner.capabilities["gpu_memory_free_mib"] == [15021, 830]
        assert runner.capabilities["cli_inventory"] == {
            "claude": "2.1.0",
            "codex": None,
            "gemini": "",
        }

    @pytest.mark.asyncio
    async def test_heartbeat_tolerates_missing_gpu_memory_and_cli_inventory(
        self, client: AsyncClient, db_session
    ):
        """Old runners omit the VRAM/CLI fields — heartbeat succeeds and the
        capability keys stay absent (never 0/empty, which would read as data)."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert "gpu_memory_total_mib" not in runner.capabilities
        assert "gpu_memory_free_mib" not in runner.capabilities
        assert "cli_inventory" not in runner.capabilities

    @pytest.mark.asyncio
    async def test_heartbeat_bounds_gpu_memory_and_cli_inventory(
        self, client: AsyncClient, db_session
    ):
        """Garbage-emitting runner: VRAM lists are length-capped and clamped
        non-negative; cli_inventory drops non-string names/versions and is
        entry-capped. Heartbeat still succeeds (no 500 on garbage)."""
        from app.routers.runner import (
            _MAX_CLI_INVENTORY_ENTRIES,
            _MAX_GPU_LIST_ENTRIES,
        )

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={
                "gpu_memory_total_mib": [-5] + [16384] * (_MAX_GPU_LIST_ENTRIES + 20),
                "gpu_memory_free_mib": [1024],
                "cli_inventory": {
                    **{f"cli{i}": "1.0" for i in range(_MAX_CLI_INVENTORY_ENTRIES + 10)},
                    "bad_version": 42,
                    "": "unnamed",
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        totals = runner.capabilities["gpu_memory_total_mib"]
        assert len(totals) == _MAX_GPU_LIST_ENTRIES
        assert totals[0] == 0  # negative clamped, not preserved
        inventory = runner.capabilities["cli_inventory"]
        assert len(inventory) <= _MAX_CLI_INVENTORY_ENTRIES
        assert "bad_version" not in inventory  # non-str version dropped
        assert "" not in inventory  # unnamed CLI dropped

    @pytest.mark.asyncio
    async def test_heartbeat_clamps_absurd_gpu_memory_to_upper_bound(
        self, client: AsyncClient, db_session
    ):
        """An absurd runner-reported VRAM integer is clamped to the 4 TiB
        upper bound (_MAX_GPU_MEMORY_MIB) so it can never reach authoring
        context / agent prompts, while plausible values pass through."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"gpu_memory_total_mib": [10**15]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)
        assert runner.capabilities["gpu_memory_total_mib"] == [4_194_304]

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


class TestNextComputeJobDiskFit:
    """Claim-time disk-fit re-check for container-image compute jobs."""

    async def _make_starting_job(self, session, project_id, runner_id, *, metadata):
        job = ComputeJob(
            project_id=project_id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={"runner_target_id": str(runner_id), **metadata},
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job

    @pytest.mark.asyncio
    async def test_container_image_job_204_when_runner_disk_below_floor(
        self, client: AsyncClient, db_session
    ):
        """A container-image job pinned to a runner whose freshest heartbeat
        disk_free_bytes is below the fit floor is NOT claimed: the endpoint
        returns 204 and leaves the job STARTING for re-dispatch."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True},
                "disk_free_bytes": 1_000_000,  # ~1 MB — far below the multi-GB floor
            },
        )
        job = await self._make_starting_job(
            db_session,
            project.id,
            runner.id,
            metadata={"container_image": "my-image:latest", "command": "run.sh"},
        )

        resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

        await db_session.refresh(job)
        assert job.status == ComputeJobStatus.STARTING, (
            "job must remain STARTING so it re-dispatches to a runner that fits it"
        )

    @pytest.mark.asyncio
    async def test_container_image_job_claimed_when_disk_above_floor(
        self, client: AsyncClient, db_session
    ):
        """A roomy runner still claims the container-image job (200 → RUNNING)."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True},
                "disk_free_bytes": 500_000_000_000,  # ~500 GB free
            },
        )
        job = await self._make_starting_job(
            db_session,
            project.id,
            runner.id,
            metadata={"container_image": "my-image:latest", "command": "run.sh"},
        )

        resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["job_id"] == str(job.id)

        await db_session.refresh(job)
        assert job.status == ComputeJobStatus.RUNNING
        # The claim body carries the job's owning project so a multi-project
        # runner can attribute its image-cache storage back to the right project.
        assert resp.json()["project_id"] == str(project.id)

    @pytest.mark.asyncio
    async def test_non_image_job_claimed_despite_low_disk(
        self, client: AsyncClient, db_session
    ):
        """The disk-fit re-check applies ONLY to container-image jobs: a plain
        job on a low-disk runner is still claimed."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True},
                "disk_free_bytes": 1_000,
            },
        )
        job = await self._make_starting_job(
            db_session,
            project.id,
            runner.id,
            metadata={"command": "run.sh"},  # no container_image
        )

        resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(job)
        assert job.status == ComputeJobStatus.RUNNING


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


class TestMultipartStreamParser:
    """Unit coverage for the memory-bounded streaming multipart parser used by
    the mid-run viewable endpoint to avoid buffering a multi-hundred-MiB video."""

    @staticmethod
    def _spool(body: bytes):
        import io

        return io.BytesIO(body)

    def test_locates_file_range_and_reads_scalar_fields(self):
        raw = bytes(range(256)) * 8  # includes CRLF-looking byte sequences
        body, content_type = _build_multipart(raw, 7, len(raw))
        fields, file_part = _parse_multipart_form_stream(self._spool(body), content_type)
        assert fields["file_count"] == b"7"
        assert fields["total_bytes"] == str(len(raw)).encode()
        assert file_part is not None
        assert file_part["name"] == "archive"
        assert file_part["filename"] == "artifacts.tar.gz"
        assert file_part["length"] == len(raw)

    def test_range_reader_yields_exact_file_bytes(self):
        raw = bytes(range(256)) * 8
        body, content_type = _build_multipart(raw, 1, len(raw))
        spool = self._spool(body)
        _fields, file_part = _parse_multipart_form_stream(spool, content_type)
        reader = _FileRangeReader(spool, file_part["start"], file_part["length"])
        # Chunked reads (as blob.upload_from_file does) reassemble EXACTLY the
        # file bytes with no trailing CRLF or boundary framing leaked.
        out = bytearray()
        while True:
            chunk = reader.read(97)  # non-aligned chunk size
            if not chunk:
                break
            out += chunk
        assert bytes(out) == raw

    def test_matches_in_memory_parser_semantics(self):
        raw = b"\x00\x01FAKE-MP4" * 32
        body, content_type = _build_multipart(raw, 2, len(raw))
        mem_fields, mem_files = _parse_multipart_form(body, content_type)
        spool = self._spool(body)
        stream_fields, file_part = _parse_multipart_form_stream(spool, content_type)
        assert stream_fields == mem_fields
        spool.seek(file_part["start"])
        assert spool.read(file_part["length"]) == mem_files["archive"]

    def test_missing_boundary_returns_none(self):
        fields, file_part = _parse_multipart_form_stream(
            self._spool(b"whatever"), "application/json"
        )
        assert fields == {} and file_part is None


class TestMultipartParserHardening:
    """Security hardening: oversized fields and excessive boundary counts are rejected."""

    @staticmethod
    def _spool(body: bytes):
        import io

        return io.BytesIO(body)

    @staticmethod
    def _build_viewable_multipart(filename: str, file_bytes: bytes, *, field_overrides=None):
        """Build a viewable-endpoint multipart body (filename field + file part)."""
        boundary = "----test-boundary-hardening"
        crlf = b"\r\n"
        extra_fields = field_overrides or {}
        body = bytearray()
        # optional extra scalar fields (used to inject oversized content)
        for name, value in extra_fields.items():
            body += b"--" + boundary.encode() + crlf
            body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
            body += value + crlf
        body += b"--" + boundary.encode() + crlf
        body += b'Content-Disposition: form-data; name="filename"' + crlf + crlf
        body += filename.encode() + crlf
        body += b"--" + boundary.encode() + crlf
        body += b'Content-Disposition: form-data; name="file"; filename="plot.png"' + crlf
        body += b"Content-Type: image/png" + crlf + crlf
        body += file_bytes + crlf
        body += b"--" + boundary.encode() + b"--" + crlf
        return bytes(body), f"multipart/form-data; boundary={boundary}"

    def test_stream_parser_rejects_oversized_non_file_field(self):
        """A non-file scalar field exceeding _MAX_MULTIPART_FIELD_BYTES raises ValueError."""
        oversized_value = b"x" * (_MAX_MULTIPART_FIELD_BYTES + 1)
        body, ct = self._build_viewable_multipart(
            "plot.png", b"PNG_BYTES", field_overrides={"extra": oversized_value}
        )
        with pytest.raises(ValueError, match=r"non-file field.*exceeds"):
            _parse_multipart_form_stream(self._spool(body), ct)

    def test_in_memory_parser_rejects_oversized_non_file_field(self):
        """_parse_multipart_form also rejects oversized scalar fields."""
        oversized_value = b"y" * (_MAX_MULTIPART_FIELD_BYTES + 1)
        body, ct = self._build_viewable_multipart(
            "plot.png", b"PNG_BYTES", field_overrides={"extra": oversized_value}
        )
        with pytest.raises(ValueError, match=r"non-file field.*exceeds"):
            _parse_multipart_form(body, ct)

    def test_stream_parser_rejects_too_many_boundaries(self):
        """A crafted body with more than _MAX_MULTIPART_PARTS boundary markers raises ValueError."""
        boundary = "----crafted-boundary"
        crlf = b"\r\n"
        # Build a body whose boundary string appears far more than max_parts times
        # without valid multipart structure — this exercises the Pass 1 counter.
        delimiter = b"--" + boundary.encode()
        # Each repetition is a minimal delimiter occurrence: delimiter + scalar field
        body = bytearray()
        for i in range(_MAX_MULTIPART_PARTS + 2):
            body += delimiter + crlf
            body += b'Content-Disposition: form-data; name="f"' + crlf + crlf
            body += b"v" + crlf
        body += delimiter + b"--" + crlf
        ct = f"multipart/form-data; boundary={boundary}"
        with pytest.raises(ValueError, match=r"more than.*boundary marker"):
            _parse_multipart_form_stream(self._spool(bytes(body)), ct)

    def test_stream_parser_normal_upload_still_succeeds(self):
        """A well-formed viewable multipart body is still parsed correctly after hardening."""
        file_bytes = b"FAKEPNG" * 16
        body, ct = self._build_viewable_multipart("reward.png", file_bytes)
        spool = self._spool(body)
        fields, file_part = _parse_multipart_form_stream(spool, ct)
        assert fields.get("filename") == b"reward.png"
        assert file_part is not None
        assert file_part["length"] == len(file_bytes)
        # Verify actual file bytes round-trip correctly
        spool.seek(file_part["start"])
        assert spool.read(file_part["length"]) == file_bytes

    def test_in_memory_parser_normal_upload_still_succeeds(self):
        """_parse_multipart_form still works for a well-formed viewable body after hardening."""
        file_bytes = b"FAKEPNG" * 8
        body, ct = self._build_viewable_multipart("chart.png", file_bytes)
        fields, files = _parse_multipart_form(body, ct)
        assert fields.get("filename") == b"chart.png"
        assert files.get("file") == file_bytes


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
    async def test_resumable_upload_completion_verifies_size_checksum_before_stamp(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {
                "upload_url": "https://storage.invalid/exact-object-signature",
                "method": "PUT",
                "headers": {"Content-Type": "application/gzip"},
                "expires_at": expires_at.isoformat(),
                "object_uri": f"gs://bucket/{job_id}/{transfer_id}",
            }

        monkeypatch.setattr(
            "app.storage.authorize_compute_job_artifact_transfer", fake_authorize
        )
        authorize = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 123, "crc32c": "AAAAAA==", "file_count": 2, "total_bytes": 456},
        )
        assert authorize.status_code == 200, authorize.text
        transfer_id = authorize.json()["transfer_id"]

        async def fake_verify(job_id, received_transfer_id):
            return {
                "uri": f"gs://bucket/{job_id}/{received_transfer_id}",
                "size": 122,
                "crc32c": "AAAAAA==",
                "generation": "7",
            }

        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verify)
        mismatch = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "7"},
        )
        assert mismatch.status_code == 409
        await db_session.refresh(job)
        assert "runner_artifact" not in (job.polling_metadata or {})
        assert job.polling_metadata["runner_artifact_transfer"]["status"] == "failed"
        assert job.polling_metadata["runner_artifact_transfer"]["failure_reason"] == "metadata_mismatch"

        async def fake_verified(job_id, received_transfer_id):
            result = await fake_verify(job_id, received_transfer_id)
            result["size"] = 123
            return result

        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verified)
        # Failed exact-object attempts are final: a fresh authorization is
        # required because ifGenerationMatch=0 prevents repairing that object.
        failed_retry = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "7"},
        )
        assert failed_retry.status_code == 409

        authorize = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 123, "crc32c": "AAAAAA==", "file_count": 2, "total_bytes": 456},
        )
        transfer_id = authorize.json()["transfer_id"]
        completed = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "7"},
        )
        assert completed.status_code == 200, completed.text
        stamped = completed.json()["runner_artifact"]
        assert stamped["stored"] is True
        assert stamped["verified"] is True
        assert stamped["size"] == 123
        assert stamped["crc32c"] == "AAAAAA=="
        assert stamped["generation"] == "7"

        repeated = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "7"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["runner_artifact"] == stamped

    @pytest.mark.asyncio
    async def test_transfer_authorization_failure_is_finalized_without_open_transaction(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(*_args, **_kwargs):
            assert not db_session.in_transaction()
            raise RuntimeError("signBlob unavailable")

        monkeypatch.setattr(
            "app.storage.authorize_compute_job_artifact_transfer", fake_authorize
        )
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        assert response.status_code == 503
        await db_session.refresh(job)
        transfer = job.polling_metadata["runner_artifact_transfer"]
        assert transfer["status"] == "failed"
        assert transfer["failure_reason"] == "authorization_failed"

    @pytest.mark.asyncio
    async def test_transfer_verification_reload_failure_is_finalized(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        async def fake_verify(*_args):
            raise RuntimeError("reload unavailable")

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verify)
        authorized = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        transfer_id = authorized.json()["transfer_id"]
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "1"},
        )
        assert response.status_code == 503
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact_transfer"]["status"] == "failed"
        assert job.polling_metadata["runner_artifact_transfer"]["failure_reason"] == "verification_unavailable"

    @pytest.mark.asyncio
    async def test_delayed_verification_failure_cannot_overwrite_verified_transfer(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        authorized = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        transfer_id = authorized.json()["transfer_id"]

        async def fake_verify(*_args):
            await db_session.refresh(job)
            artifact = {
                "uri": f"gs://bucket/{job.id}/{transfer_id}",
                "generation": "1",
                "transfer_id": transfer_id,
            }
            job.polling_metadata = {
                **job.polling_metadata,
                "runner_artifact": artifact,
                "runner_artifact_transfer": {
                    **job.polling_metadata["runner_artifact_transfer"],
                    "status": "verified",
                },
            }
            await db_session.commit()
            raise RuntimeError("delayed verification failure")

        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verify)
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "1"},
        )
        assert response.status_code == 503
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact_transfer"]["status"] == "verified"

    @pytest.mark.asyncio
    async def test_delayed_verification_success_cannot_overwrite_failed_transfer(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        authorized = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        transfer_id = authorized.json()["transfer_id"]

        async def fake_verify(job_id, received_transfer_id):
            await db_session.refresh(job)
            job.polling_metadata = {
                **job.polling_metadata,
                "runner_artifact_transfer": {
                    **job.polling_metadata["runner_artifact_transfer"],
                    "status": "failed",
                    "failure_reason": "concurrent_failure",
                },
            }
            await db_session.commit()
            return {
                "uri": f"gs://bucket/{job_id}/{received_transfer_id}",
                "size": 10,
                "crc32c": "AAAAAA==",
                "generation": "1",
            }

        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verify)
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "1"},
        )
        assert response.status_code == 409
        await db_session.refresh(job)
        assert "runner_artifact" not in job.polling_metadata
        assert job.polling_metadata["runner_artifact_transfer"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_superseded_transfer_cannot_complete(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        ids = []
        for size in (10, 11):
            response = await client.post(
                f"/api/runner/compute-job/{job.id}/artifact-transfer",
                headers={"Authorization": f"Bearer {token}"},
                json={"archive_size": size, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": size},
            )
            ids.append(response.json()["transfer_id"])
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": ids[0], "generation": "1"},
        )
        assert response.status_code == 409
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact_transfer"]["transfer_id"] == ids[1]

    @pytest.mark.asyncio
    async def test_wrong_pinned_runner_cannot_authorize_transfer(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        other_runner, _ = await _make_runner(db_session, project.id)
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(other_runner.id)},
        )
        db_session.add(job)
        await db_session.commit()
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_runner_cannot_authorize_transfer_in_another_project(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session, owner_id="other-owner")
        runner, token = await _make_runner(db_session, project.id)
        job = ComputeJob(
            project_id=other_project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(runner.id)},
        )
        db_session.add(job)
        await db_session.commit()
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_preissued_transfer_cannot_overwrite_finalized_evidence(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)
        final_artifact = {
            "kind": "runner_artifact",
            "uri": f"gs://bucket/compute-jobs/{job.id}/artifacts/bundle.tar.gz",
            "stored": True,
        }

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            current = await db_session.get(ComputeJob, job.id)
            await db_session.refresh(current)
            current.status = ComputeJobStatus.SUCCEEDED
            current.polling_metadata = {
                **current.polling_metadata,
                "runner_artifact": final_artifact,
            }
            await db_session.commit()
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        assert response.status_code == 409
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact"] == final_artifact
        assert job.polling_metadata["runner_artifact_transfer"]["status"] == "failed"
        assert job.polling_metadata["runner_artifact_transfer"]["failure_reason"] == "evidence_finalized"

    @pytest.mark.asyncio
    async def test_preissued_transfer_completion_cannot_overwrite_finalized_evidence(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        _project, _runner, token, job = await self._make_runner_job(db_session)

        async def fake_authorize(job_id, transfer_id, *, expires_at):
            return {"upload_url": "https://storage.invalid", "expires_at": expires_at.isoformat()}

        monkeypatch.setattr("app.storage.authorize_compute_job_artifact_transfer", fake_authorize)
        authorized = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer",
            headers={"Authorization": f"Bearer {token}"},
            json={"archive_size": 10, "crc32c": "AAAAAA==", "file_count": 1, "total_bytes": 10},
        )
        transfer_id = authorized.json()["transfer_id"]
        final_artifact = {
            "kind": "runner_artifact",
            "uri": f"gs://bucket/compute-jobs/{job.id}/artifacts/bundle.tar.gz",
            "stored": True,
        }
        await db_session.refresh(job)
        job.status = ComputeJobStatus.SUCCEEDED
        job.polling_metadata = {
            **job.polling_metadata,
            "runner_artifact": final_artifact,
        }
        db_session.add(job)
        await db_session.commit()

        async def fake_verify(job_id, received_transfer_id):
            return {
                "uri": f"gs://bucket/compute-jobs/{job_id}/artifacts/transfers/{received_transfer_id}.tar.gz",
                "size": 10,
                "crc32c": "AAAAAA==",
                "generation": "1",
            }

        monkeypatch.setattr("app.storage.verify_compute_job_artifact_transfer", fake_verify)
        response = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact-transfer/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"transfer_id": transfer_id, "generation": "1"},
        )
        assert response.status_code == 409
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact"] == final_artifact

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

    # ── Terminal-status guard ────────────────────────────────────────────────
    #
    # _COMPUTE_JOB_TERMINAL_STATUSES promises that runner reports/uploads can no
    # longer mutate a finalized job's evidence. /report enforced it; the artifact
    # upload did not — so a pinned runner could POST to a SUCCEEDED/FAILED job and
    # overwrite the durable GCS bundle a verdict had already been drawn from
    # (store_runner_artifact writes a FIXED per-job object path).

    @pytest.mark.parametrize(
        "terminal_status",
        [
            ComputeJobStatus.SUCCEEDED,
            ComputeJobStatus.FAILED,
            ComputeJobStatus.CANCELLED,
            ComputeJobStatus.TIMED_OUT,
        ],
    )
    @pytest.mark.asyncio
    async def test_upload_cannot_overwrite_finalized_job_artifact(
        self, client: AsyncClient, db_session, monkeypatch, terminal_status
    ):
        """A second upload to a FINALIZED job is refused — evidence is immutable."""
        project, runner, token, job = await self._make_runner_job(db_session)

        # The job is finalized AND already carries its durable evidence.
        original = {
            "kind": "runner_artifact",
            "uri": "gs://bucket/compute-jobs/x/artifacts/bundle.tar.gz",
            "file_count": 7,
            "total_bytes": 999,
            "stored": True,
        }
        job.status = terminal_status
        job.polling_metadata = {
            **(job.polling_metadata or {}),
            "runner_artifact": original,
        }
        db_session.add(job)
        await db_session.commit()

        async def fake_store(*a, **k):
            raise AssertionError(
                "store_runner_artifact must NOT run for a finalized job — "
                "it would overwrite the durable GCS object"
            )

        monkeypatch.setattr(
            compute_backends_mod, "store_runner_artifact", fake_store, raising=False
        )

        body, content_type = _build_multipart(b"\x1f\x8b\x08OVERWRITE", 1, 42)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )

        assert resp.status_code == 409, resp.text

        # The stamped evidence is untouched.
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact"] == original

    @pytest.mark.asyncio
    async def test_first_upload_after_finalize_is_still_accepted(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A terminal job with NO artifact yet still accepts its FIRST upload.

        This is the normal race, not an attack: the runner sends its terminal
        /report FIRST and uploads the artifact afterwards (local_runner.py does it
        in a `finally:` block), while the monitor finalizes the job as soon as it
        sees runner_exec_status=succeeded. Rejecting this would silently destroy
        the artifacts of every job whose upload lost that race — so the guard must
        bite on OVERWRITE, not on "terminal".
        """
        project, runner, token, job = await self._make_runner_job(db_session)
        job.status = ComputeJobStatus.SUCCEEDED  # finalized, no artifact stamped
        db_session.add(job)
        await db_session.commit()

        async def fake_store(session, j, archive_bytes, file_count, total_bytes):
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

        body, content_type = _build_multipart(b"\x1f\x8b\x08gz", 3, 77)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )

        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact"]["file_count"] == 3

    @pytest.mark.asyncio
    async def test_first_bundle_to_terminal_job_triggers_artifact_linkage(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """First bundle upload to a terminal job triggers _link_terminal_artifact.

        When a runner reports terminal status first (the normal race window in
        local_runner.py) and the monitor finalises the job before the bundle
        arrives, the /artifact endpoint must call _link_terminal_artifact so that
        artifact_id and result_reference are backfilled — otherwise the canvas
        loses the result once live preview synthesis stops.
        """
        project, runner, token, job = await self._make_runner_job(db_session)
        # Finalize the job before the bundle arrives (simulating the race).
        job.status = ComputeJobStatus.SUCCEEDED
        db_session.add(job)
        await db_session.commit()

        async def fake_store(session, j, archive_bytes, file_count, total_bytes):
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

        linkage_calls = []

        async def fake_link(session, j):
            linkage_calls.append(j.id)

        # Patch via the module the runner endpoint imports at call time.
        import app.compute_jobs as cj_mod

        monkeypatch.setattr(cj_mod, "_link_terminal_artifact", fake_link)

        body, content_type = _build_multipart(b"\x1f\x8b\x08gz", 3, 77)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )

        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        # The bundle metadata must be stamped.
        assert job.polling_metadata["runner_artifact"]["stored"] is True
        # _link_terminal_artifact must have been called to backfill artifact_id.
        assert job.id in linkage_calls

    @pytest.mark.asyncio
    async def test_upload_to_running_job_can_still_be_replaced(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A NON-terminal job's artifact is still replaceable (retry after a blip).

        The runner retries a dropped upload; the job is still RUNNING server-side,
        so re-stamping must remain allowed. The guard is scoped to finalized jobs.
        """
        project, runner, token, job = await self._make_runner_job(db_session)
        job.polling_metadata = {
            **(job.polling_metadata or {}),
            "runner_artifact": {"kind": "runner_artifact", "file_count": 1},
        }
        db_session.add(job)
        await db_session.commit()

        async def fake_store(session, j, archive_bytes, file_count, total_bytes):
            return {
                "kind": "runner_artifact",
                "uri": "gs://bucket/x/bundle.tar.gz",
                "file_count": file_count,
                "total_bytes": total_bytes,
                "stored": True,
            }

        monkeypatch.setattr(
            compute_backends_mod, "store_runner_artifact", fake_store, raising=False
        )

        body, content_type = _build_multipart(b"\x1f\x8b\x08gz", 5, 88)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/artifact",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )

        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        assert job.polling_metadata["runner_artifact"]["file_count"] == 5


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
    async def test_compute_assignments_attribute_running_job_by_claiming_runner(
        self, db_session
    ):
        """A RUNNING job is counted against the runner that AUTHORITATIVELY claimed
        it (ran_on_runner_id), not the dispatch reservation pin (runner_target_id).

        - A RUNNING job claimed by R2 but pinned to R1 counts for R2.
        - A QUEUED job (not yet claimed, ran_on_runner_id NULL) counts for its pin.
        - A legacy RUNNING job with NULL ran_on_runner_id falls back to its pin.
        """
        project = await _make_project(db_session)
        r1, _ = await _make_runner(db_session, project.id)
        r2, _ = await _make_runner(db_session, project.id)

        # Claimed-and-running on R2, but the stale dispatch pin still names R1.
        claimed = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            ran_on_runner_id=r2.id,
            polling_metadata={"runner_target_id": str(r1.id)},
        )
        # Reserved-but-unclaimed: no ran_on_runner_id yet, pinned to R1.
        reserved = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.QUEUED,
            ran_on_runner_id=None,
            polling_metadata={"runner_target_id": str(r1.id)},
        )
        # Legacy RUNNING row predating the column — falls back to its pin (R1).
        legacy = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            ran_on_runner_id=None,
            polling_metadata={"runner_target_id": str(r1.id)},
        )
        db_session.add_all([claimed, reserved, legacy])
        await db_session.commit()

        assignments = await orch._load_runner_compute_job_assignments(
            db_session, [r1.id, r2.id]
        )
        assert set(assignments.get(r2.id, [])) == {str(claimed.id)}
        assert set(assignments.get(r1.id, [])) == {str(reserved.id), str(legacy.id)}

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
        """Capacity is per CLASS: active compute jobs saturate the COMPUTE
        class (a further compute_experiment is refused), while the AGENT class
        stays dispatchable — one class must not starve the other."""
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
        compute_task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.QUEUED,
        )
        agent_task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(compute_task)
        db_session.add(agent_task)
        await db_session.commit()

        # Compute class full (2 active vs max_concurrent fallback 2) → refused.
        assert await orch._try_runner_dispatch(
            db_session, compute_task, {project.id: [runner]}, {}
        ) is False
        # Agent class is independent — the same runner still takes agent work.
        assert await orch._try_runner_dispatch(
            db_session, agent_task, {project.id: [runner]}, {}
        ) is True

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

    @pytest.mark.asyncio
    async def test_devices_absent_when_no_live_runner(self, db_session):
        """The additive `devices` key is OMITTED when no runner is live, so the
        load-bearing 3-key shape is preserved for existing consumers."""
        project = await _make_project(db_session)
        # Registered-but-stale runner → ever_registered True, no live devices.
        await _make_runner(
            db_session,
            project.id,
            status="active",
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
            last_seen_at=_utcnow() - timedelta(seconds=400),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        assert status == {
            "ever_registered": True,
            "active_total": 0,
            "active_gpu": 0,
        }
        assert "devices" not in status

    @pytest.mark.asyncio
    async def test_devices_block_for_live_gpu_runner(self, db_session):
        """A live runner's persisted capabilities become a bounded device entry:
        name, gpu_count, deduped models, per-GPU VRAM (when advertised),
        container capability, and current load."""
        project = await _make_project(db_session)
        await _make_runner(
            db_session,
            project.id,
            status="active",
            hostname="dgx-1",
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "max_concurrent": 2,
                "gpu_count": 8,
                "gpu_names": ["Tesla V100-SXM2-16GB"] * 8,
                "gpu_memory_total_mib": [16384] * 8,
                "isolation_backends": {
                    "bwrap_ok": True,
                    "userns_ok": True,
                    "docker_usable": True,
                    "docker_cdi_gpu": True,
                    "podman_usable": False,
                    "apptainer_ok": False,
                    "enroot_ok": False,
                    "systemd_run_ok": False,
                },
                "container_gpu_passthrough": {"docker": {"verified": True, "mode": "cdi"}},
                "active_task_ids": ["t-1"],
                "active_compute_job_ids": [],
            },
            last_seen_at=_utcnow(),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        assert status["active_gpu"] == 1
        assert status["devices"] == [
            {
                "runner_name": "dgx-1",
                "gpu_count": 8,
                "gpu_models": ["Tesla V100-SXM2-16GB"],
                "container_ok": True,
                "gpu_in_container_ok": True,
                "active_tasks": 1,
                "active_compute_jobs": 0,
                "gpu_vram_mib": [16384] * 8,
            }
        ]

    @pytest.mark.asyncio
    async def test_devices_skip_vram_when_not_advertised(self, db_session):
        """Older runners that do not advertise gpu_memory_total_mib get no
        gpu_vram_mib key (never a misleading zero), and a caps dict without an
        isolation inventory reads as no container runtime."""
        project = await _make_project(db_session)
        await _make_runner(
            db_session,
            project.id,
            status="active",
            hostname="old-box",
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "max_concurrent": 2,
                "gpu_count": 1,
                "gpu_names": ["NVIDIA GeForce RTX 3060"],
            },
            last_seen_at=_utcnow(),
        )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        (device,) = status["devices"]
        assert "gpu_vram_mib" not in device
        assert device["container_ok"] is False
        assert device["gpu_in_container_ok"] is False
        assert device["active_tasks"] == 0
        assert device["active_compute_jobs"] == 0

    @pytest.mark.asyncio
    async def test_devices_bounded_in_total_gpu_richest_first(self, db_session):
        """The devices list is capped at DEVICES_MAX_RUNNERS total, keeping the
        GPU-richest runners (the ones VRAM-fit reasoning needs)."""
        from app.experiment_authoring_context import DEVICES_MAX_RUNNERS

        project = await _make_project(db_session)
        for i in range(DEVICES_MAX_RUNNERS + 2):
            await _make_runner(
                db_session,
                project.id,
                status="active",
                hostname=f"node-{i}",
                capabilities={
                    "task_types": "all",
                    "has_gpu": i % 2 == 0,
                    "max_concurrent": 2,
                    # Alternating 1-GPU / CPU-only so the cap has to choose.
                    "gpu_count": 1 if i % 2 == 0 else 0,
                },
                last_seen_at=_utcnow(),
            )

        from app.strategic_spawning import _load_local_runner_status

        status = await _load_local_runner_status(db_session, project.id)
        devices = status["devices"]
        assert len(devices) == DEVICES_MAX_RUNNERS
        # GPU runners sort ahead of CPU-only runners.
        gpu_counts = [d["gpu_count"] for d in devices]
        assert gpu_counts == sorted(gpu_counts, reverse=True)

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

        # Terminal report: command_log routes to runner_command_log (interim
        # "running" reports route their tail to live_output instead).
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "failed",
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
                "exec_status": "failed",
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


class TestInputArtifactDownloadEndpoint:
    """The runner-facing cross-job checkpoint-resume download endpoint.

    Scoped exactly like the report/artifact endpoints (pinned runner + same
    project), and additionally checks the predecessor belongs to the runner's
    project before streaming its durable bundle.
    """

    async def _make_pinned_job(self, db_session, project, runner, meta):
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(runner.id), **meta},
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    @pytest.mark.asyncio
    async def test_streams_predecessor_bundle(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from app import storage

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        predecessor = await self._make_pinned_job(db_session, project, runner, {})
        job = await self._make_pinned_job(
            db_session, project, runner,
            {"input_artifact_job_id": str(predecessor.id)},
        )

        async def fake_download(job_id):
            return b"BUNDLE-BYTES" if str(job_id) == str(predecessor.id) else None

        monkeypatch.setattr(
            storage, "download_compute_job_artifact_bundle", fake_download
        )

        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == b"BUNDLE-BYTES"
        assert resp.headers["content-type"] == "application/gzip"

    @pytest.mark.asyncio
    async def test_streams_transfer_specific_predecessor_uri_for_legacy_client(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from app import storage

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        predecessor = await self._make_pinned_job(db_session, project, runner, {})
        transfer_id = uuid.uuid4()
        artifact_uri = storage.compute_job_artifact_transfer_uri(predecessor.id, transfer_id)
        predecessor.polling_metadata = {
            "runner_target_id": str(runner.id),
            "runner_artifact": {"stored": True, "verified": True, "uri": artifact_uri},
        }
        db_session.add(predecessor)
        await db_session.commit()
        job = await self._make_pinned_job(
            db_session, project, runner,
            {"input_artifact_job_id": str(predecessor.id)},
        )

        async def fake_download(uri):
            assert uri == artifact_uri
            return b"TRANSFER-BUNDLE"

        monkeypatch.setattr(storage, "download_compute_job_artifact_uri", fake_download)
        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.content == b"TRANSFER-BUNDLE"

    @pytest.mark.asyncio
    async def test_direct_download_returns_verified_transfer_metadata(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from app import storage

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        predecessor = await self._make_pinned_job(db_session, project, runner, {})
        artifact_uri = storage.compute_job_artifact_transfer_uri(predecessor.id, uuid.uuid4())
        predecessor.polling_metadata = {
            "runner_target_id": str(runner.id),
            "runner_artifact": {
                "stored": True,
                "verified": True,
                "uri": artifact_uri,
                "size": 987,
                "crc32c": "AAAAAA==",
                "generation": "42",
            },
        }
        db_session.add(predecessor)
        await db_session.commit()
        job = await self._make_pinned_job(
            db_session, project, runner,
            {"input_artifact_job_id": str(predecessor.id)},
        )

        async def fake_sign(uri):
            assert uri == artifact_uri
            return "https://storage.invalid/signed-get"

        monkeypatch.setattr(
            storage, "generate_compute_job_artifact_uri_download_signed_url", fake_sign
        )
        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact?direct=true",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "download_url": "https://storage.invalid/signed-get",
            "expires_in_seconds": 3600,
            "size": 987,
            "crc32c": "AAAAAA==",
            "generation": "42",
        }

    @pytest.mark.asyncio
    async def test_404_when_no_input_artifact_reference(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        job = await self._make_pinned_job(db_session, project, runner, {})

        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_when_predecessor_in_other_project(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from app import storage

        project = await _make_project(db_session)
        other = await _make_project(db_session, owner_id="other-owner")
        runner, token = await _make_runner(db_session, project.id)
        # Predecessor lives in a DIFFERENT project — must never be streamed.
        foreign = ComputeJob(
            project_id=other.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.SUCCEEDED,
            polling_metadata={},
        )
        db_session.add(foreign)
        await db_session.commit()
        await db_session.refresh(foreign)
        job = await self._make_pinned_job(
            db_session, project, runner,
            {"input_artifact_job_id": str(foreign.id)},
        )

        async def fake_download(job_id):
            return b"SHOULD-NOT-BE-SERVED"

        monkeypatch.setattr(
            storage, "download_compute_job_artifact_bundle", fake_download
        )

        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_when_bundle_missing(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from app import storage

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        predecessor = await self._make_pinned_job(db_session, project, runner, {})
        job = await self._make_pinned_job(
            db_session, project, runner,
            {"input_artifact_job_id": str(predecessor.id)},
        )

        async def fake_download(job_id):
            return None

        monkeypatch.setattr(
            storage, "download_compute_job_artifact_bundle", fake_download
        )

        resp = await client.get(
            f"/api/runner/compute-job/{job.id}/input-artifact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_next_compute_job_includes_input_artifact_job_id(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        predecessor_id = str(uuid.uuid4())
        task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "compute_experiment",
                    "require_durable_artifact": True,
                }
            ],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.flush()
        job = ComputeJob(
            task_id=task.id,
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo resume",
                "input_artifact_job_id": predecessor_id,
            },
        )
        db_session.add(job)
        await db_session.commit()

        resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["input_artifact_job_id"] == predecessor_id
        assert resp.json()["require_durable_artifact"] is True

    @pytest.mark.asyncio
    async def test_next_compute_job_stamps_ran_on_runner_id(
        self, client: AsyncClient, db_session
    ):
        """Claiming a runner-subprocess compute job via /next-compute-job records
        the claiming runner as the authoritative ``ran_on_runner_id``."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.STARTING,
            polling_metadata={
                "runner_target_id": str(runner.id),
                "command": "echo hi",
            },
        )
        db_session.add(job)
        await db_session.commit()
        assert job.ran_on_runner_id is None

        resp = await client.post(
            "/api/runner/next-compute-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        assert job.status == ComputeJobStatus.RUNNING
        assert job.ran_on_runner_id == runner.id

    @pytest.mark.asyncio
    async def test_report_compute_job_backfills_ran_on_runner_id_when_null(
        self, client: AsyncClient, db_session
    ):
        """A report on a job whose ``ran_on_runner_id`` is still NULL (claimed
        before this column shipped) idempotently backfills the claiming runner."""
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
        assert job.ran_on_runner_id is None

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "succeeded",
                "artifact_delivery": {
                    "status": "failed",
                    "protocol": "direct",
                    "command_exec_status": "succeeded",
                    "reason": "x" * 1000,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        assert job.ran_on_runner_id == runner.id
        delivery = job.polling_metadata["artifact_delivery"]
        assert delivery["command_exec_status"] == "succeeded"
        assert len(delivery["reason"]) == 256

    @pytest.mark.asyncio
    async def test_report_compute_job_does_not_overwrite_ran_on_runner_id(
        self, client: AsyncClient, db_session
    ):
        """A report never overwrites a non-null ``ran_on_runner_id`` — the value
        stamped at claim time is authoritative and stable across reports."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        # A distinct, real runner (FK requires a valid localrunner row) already
        # recorded as the authoritative owner — a later report must not clobber it.
        other_runner, _ = await _make_runner(db_session, project.id)
        original_runner_id = other_runner.id
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(runner.id)},
            ran_on_runner_id=original_runner_id,
        )
        db_session.add(job)
        await db_session.commit()

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={"exec_status": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        assert job.ran_on_runner_id == original_runner_id


@pytest.mark.asyncio
async def test_compute_claim_mints_token_for_claimed_job_project(db_session):
    """The GitHub installation token minted at claim time must be scoped to the
    CLAIMED JOB's owning project, not the runner's enrollment project.

    Today the claim filter forces ``job.project_id == runner.project_id`` so the
    two are always equal and the distinction is a no-op. But the in-flight
    multi-project allow-list replaces that filter with allow-list membership — at
    which point a runner enrolled under project A can legitimately claim a job for
    project B. The repo token clones project B's repo, so it MUST be minted from
    project B's GitHub installation, never project A's. We stub the claim SELECT
    (the "claim filter") to yield the cross-project job while exercising the REAL
    token-minting block that follows.
    """
    from fastapi import Response

    from app.routers.runner import next_compute_job

    project_a = await _make_project(db_session, name="Runner Enrollment Project A")
    project_b = await _make_project(db_session, name="Job Owner Project B")
    runner, _token = await _make_runner(db_session, project_a.id)

    job = ComputeJob(
        project_id=project_b.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.STARTING,
        polling_metadata={
            "runner_target_id": str(runner.id),
            "command": "python train.py",
            # repo_clone must be present for the token-minting block to run.
            "repo_clone": {"url": "https://github.com/acme/repo.git", "ref": "main"},
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Stub the claim query so it returns project B's job to a runner enrolled
    # under project A — simulating the allow-list having relaxed the project
    # filter. Everything downstream (token minting) runs for real.
    class _ClaimResult:
        def scalars(self):
            return self

        def all(self):
            # allowed_project_ids() -> bound_project_ids() runs first and calls
            # .scalars().all(); an empty allow-list is fine here because the
            # claim SELECT is stubbed to yield the cross-project job directly.
            return []

        def first(self):
            return job

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=_ClaimResult())
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()
    fake_session.refresh = AsyncMock()

    async def _get(_model, pk):
        if pk == project_b.id:
            return project_b
        if pk == project_a.id:
            return project_a
        return None

    fake_session.get = AsyncMock(side_effect=_get)

    token_mock = AsyncMock(return_value="ghs_scoped_to_job_project")
    with patch("app.branch_cleanup._get_project_github_token", token_mock):
        resp = await next_compute_job(Response(), runner=runner, session=fake_session)

    # The token minted and returned to the runner comes from project B.
    assert resp["repo_token"] == "ghs_scoped_to_job_project"
    assert token_mock.await_count == 1
    minted_for = token_mock.await_args.args[0]
    assert minted_for.id == project_b.id, (
        "repo token must be minted for the CLAIMED JOB's project (B), not the "
        "runner's enrollment project (A)"
    )
    assert minted_for.id != project_a.id
    # And the Project row was fetched by the job's project id, not the runner's.
    fake_session.get.assert_awaited_once_with(Project, project_b.id)


class TestReportInterimProgressAndLiveOutput:
    """Interim-report protocol: live_output tail + self-reported progress records.

    Progress records originate from SANDBOXED experiment code (the runner just
    relays its BLUEPRINT_PROGRESS_FILE tail), so the endpoint must validate
    every field, cap every dimension, and tolerate arbitrary garbage without a
    500 — and interim command_log tails must land on the UI's live_output key,
    never clobbering the terminal runner_command_log.
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
    async def test_running_command_log_stamps_live_output_not_runner_command_log(
        self, client: AsyncClient, db_session
    ):
        _project, _runner, token, job = await self._make_running_job(db_session)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "command_log": {"text": "epoch 3 loss=0.42\n", "truncated": True},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        meta = job.polling_metadata
        assert meta.get("live_output") == {
            "text": "epoch 3 loss=0.42\n",
            "truncated": True,
        }
        assert "runner_command_log" not in meta, (
            "an interim tail must never masquerade as the terminal command log"
        )

    @pytest.mark.asyncio
    async def test_running_live_output_is_tail_truncated_at_cap(
        self, client: AsyncClient, db_session
    ):
        """Oversized live tails keep the TAIL (most recent output), not the head."""
        from app.routers.runner import _MAX_RUNNER_LIVE_OUTPUT_BYTES

        _project, _runner, token, job = await self._make_running_job(db_session)
        big = "A" * _MAX_RUNNER_LIVE_OUTPUT_BYTES + "END"
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={"exec_status": "running", "command_log": {"text": big}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        live = job.polling_metadata.get("live_output", {})
        assert live.get("truncated") is True
        assert live.get("text", "").endswith("END")
        assert len(live["text"].encode("utf-8")) <= _MAX_RUNNER_LIVE_OUTPUT_BYTES

    @pytest.mark.asyncio
    async def test_progress_records_validated_and_stamped(
        self, client: AsyncClient, db_session
    ):
        """Valid records land in polling_metadata['progress'] with count +
        updated_at; invalid records (bad types, non-finite, bool value) drop."""
        _project, _runner, token, job = await self._make_running_job(db_session)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "running",
                "progress": {
                    "records": [
                        {"step": 1, "metric": "loss", "value": 2.5},
                        {"step": 2, "metric": "loss", "value": 2.1, "ts": "2026-07-11T00:00:00Z"},
                        {"metric": "loss", "value": True},  # bool → dropped
                        {"metric": "", "value": 1.0},  # blank metric → dropped
                        {"metric": "loss", "value": "NaN"},  # string → dropped
                        "garbage",  # non-dict → dropped
                    ],
                    "count": 2,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        progress = job.polling_metadata.get("progress")
        assert progress is not None
        assert progress["count"] == 2
        assert "updated_at" in progress
        assert progress["records"] == [
            {"step": 1, "metric": "loss", "value": 2.5},
            {"step": 2, "metric": "loss", "value": 2.1, "ts": "2026-07-11T00:00:00Z"},
        ]

    @pytest.mark.asyncio
    async def test_progress_garbage_payloads_tolerated_without_500(
        self, client: AsyncClient, db_session
    ):
        """Arbitrary garbage shapes must 200 and leave no progress key."""
        _project, _runner, token, job = await self._make_running_job(db_session)
        for garbage in (
            "not a dict",
            123,
            {"records": "nope"},
            {"records": []},
            {"records": [{"metric": "m"}]},  # no value
            {"no_records_key": True},
            {"records": [{"metric": "m", "value": 1.0}] * 500},  # wildly oversized
        ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/report",
                json={"exec_status": "running", "progress": garbage},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, f"payload {garbage!r}: {resp.text}"
        await db_session.refresh(job)
        assert "progress" not in (job.polling_metadata or {})

    @pytest.mark.asyncio
    async def test_progress_count_delta_merge_appends_only_new_and_caps_at_50(
        self, client: AsyncClient, db_session
    ):
        """Re-sent tail overlap is not double-appended (count-delta merge) and
        the stored list is capped at the last _MAX_PROGRESS_RECORDS_STORED."""
        from app.routers.runner import _MAX_PROGRESS_RECORDS_STORED

        _project, _runner, token, job = await self._make_running_job(db_session)

        async def _send(records, count):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/report",
                json={
                    "exec_status": "running",
                    "progress": {"records": records, "count": count},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text

        def _rec(step):
            return {"step": step, "metric": "loss", "value": 10.0 - step * 0.1}

        # Report 1: steps 1-3. Report 2 re-sends 2-3 plus new 4-5 (tail overlap).
        await _send([_rec(1), _rec(2), _rec(3)], 3)
        await _send([_rec(2), _rec(3), _rec(4), _rec(5)], 5)
        await db_session.refresh(job)
        progress = job.polling_metadata["progress"]
        assert [r["step"] for r in progress["records"]] == [1, 2, 3, 4, 5], (
            "overlapping tail entries must not be double-appended"
        )
        assert progress["count"] == 5

        # A re-sent identical snapshot (count unchanged) adds nothing.
        await _send([_rec(4), _rec(5)], 5)
        await db_session.refresh(job)
        assert [r["step"] for r in job.polling_metadata["progress"]["records"]] == [
            1, 2, 3, 4, 5,
        ]

        # Long run: the stored list caps at the newest 50.
        total = 5
        for batch_start in range(6, 106, 20):
            batch = [_rec(s) for s in range(batch_start, batch_start + 20)]
            total = batch_start + 19
            await _send(batch, total)
        await db_session.refresh(job)
        progress = job.polling_metadata["progress"]
        assert len(progress["records"]) == _MAX_PROGRESS_RECORDS_STORED
        assert progress["records"][-1]["step"] == 105
        assert progress["count"] == 105

    @pytest.mark.asyncio
    async def test_progress_retry_with_bumped_count_not_double_appended(
        self, client: AsyncClient, db_session
    ):
        """Suffix-dedup backstop: a retried report whose count was BUMPED but
        whose records are the already-stored tail verbatim adds nothing (the
        count delta alone would falsely admit it); the next genuinely new
        record still appends exactly once."""
        _project, _runner, token, job = await self._make_running_job(db_session)

        r1 = {"step": 1, "metric": "loss", "value": 2.5}
        r2 = {"step": 2, "metric": "loss", "value": 2.1}

        async def _send(records, count):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/report",
                json={
                    "exec_status": "running",
                    "progress": {"records": records, "count": count},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text

        await _send([r1], 1)
        # Retry of the SAME record with a bumped count: delta says "one new"
        # but the record equals the stored tail — must not re-append.
        await _send([r1], 2)
        await db_session.refresh(job)
        assert job.polling_metadata["progress"]["records"] == [r1]

        # Overlap plus one genuinely new record appends exactly the new one.
        await _send([r1, r2], 3)
        await db_session.refresh(job)
        assert job.polling_metadata["progress"]["records"] == [r1, r2]

    @pytest.mark.asyncio
    async def test_progress_accepted_on_terminal_report(
        self, client: AsyncClient, db_session
    ):
        """The final terminal report may carry the last progress records."""
        _project, _runner, token, job = await self._make_running_job(db_session)
        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/report",
            json={
                "exec_status": "failed",
                "exit_code": 124,
                "command_log": {"text": "killed at deadline", "truncated": False},
                "progress": {
                    "records": [{"step": 9, "metric": "reward", "value": 41.5}],
                    "count": 9,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(job)
        meta = job.polling_metadata
        assert meta["progress"]["records"][-1]["value"] == 41.5
        assert meta["runner_command_log"]["text"] == "killed at deadline"


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

    # ── Launch-contract runner_target pin (soft + strict) ────────────────────

    @pytest.mark.asyncio
    async def test_soft_runner_pin_prefers_pinned_runner_via_payload(
        self, db_session
    ):
        """runner_target (strict=false) in the launch contract wins over the
        least-loaded rule when the pinned runner is eligible — same seam as the
        existing context preference, but authored in the payload."""
        project = await _make_project(db_session)
        pinned, _ = await _make_runner(
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
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(pinned.id), "strict": False}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned, idle]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.context["runner_target_id"] == str(pinned.id)
        job = await _compute_job_for_task(db_session, task.id)
        assert (job.polling_metadata or {}).get("runner_target_id") == str(pinned.id)

    @pytest.mark.asyncio
    async def test_soft_runner_pin_falls_through_when_pinned_ineligible(
        self, db_session
    ):
        """A soft pin to an at-capacity runner falls through to normal selection
        (the idle runner gets the job) instead of holding or wedging."""
        project = await _make_project(db_session)
        pinned, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 1,
                "active_compute_job_ids": ["existing-job"],  # at capacity
            },
        )
        idle, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(pinned.id), "strict": False}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned, idle]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.context["runner_target_id"] == str(idle.id)
        assert "runner_pin_hold" not in (task.context or {})

    @pytest.mark.asyncio
    async def test_strict_runner_pin_dispatches_when_pinned_runner_eligible(
        self, db_session
    ):
        """A strict pin dispatches to the pinned runner (even when busier than a
        competitor) and clears a stale hold reason from a prior held cycle."""
        project = await _make_project(db_session)
        pinned, _ = await _make_runner(
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
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 4},
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(pinned.id), "strict": True}
                )
            ],
            status=TaskStatus.QUEUED,
            # Stale artifacts from a prior held cycle — must be cleared on launch.
            context={
                "awaiting_local_runner_since": _utcnow().isoformat(),
                "runner_pin_hold": {
                    "runner_id": str(pinned.id),
                    "strict": True,
                    "reason_code": "offline",
                    "detail": "stale",
                },
            },
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned, idle]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert task.context["runner_target_id"] == str(pinned.id)
        assert "awaiting_local_runner_since" not in task.context
        assert "runner_pin_hold" not in task.context
        job = await _compute_job_for_task(db_session, task.id)
        assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        assert (job.polling_metadata or {}).get("runner_target_id") == str(pinned.id)

    @pytest.mark.asyncio
    async def test_strict_runner_pin_holds_when_runner_offline(self, db_session):
        """strict=true + pinned runner not live → held with a truthful reason
        naming the runner (never dispatched to the other eligible runner, never
        a generic no-runner message)."""
        project = await _make_project(db_session)
        offline, _ = await _make_runner(
            db_session,
            project.id,
            status="inactive",  # enrolled but not live → not in the cache
        )
        idle, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(offline.id), "strict": True}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [idle]}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert (task.context or {}).get("awaiting_local_runner_since")
        hold = (task.context or {}).get("runner_pin_hold")
        assert hold is not None
        assert hold["runner_id"] == str(offline.id)
        assert hold["strict"] is True
        assert hold["reason_code"] == "offline"
        # The status message names the runner and the why — machine-checkable
        # by the UI, truthful for the operator.
        message = task.latest_status_message or ""
        assert str(offline.id) in message
        assert "offline" in message
        assert "runner_target hold" in message
        # Strict means strict: nothing dispatched anywhere.
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_strict_runner_pin_holds_when_at_capacity(self, db_session):
        project = await _make_project(db_session)
        pinned, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": False,
                "max_concurrent": 1,
                "active_compute_job_ids": ["existing-job"],  # at capacity
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(pinned.id), "strict": True}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned]}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        hold = (task.context or {}).get("runner_pin_hold")
        assert hold is not None
        assert hold["reason_code"] == "at_capacity"
        assert "at_capacity" in (task.latest_status_message or "")
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_strict_runner_pin_holds_when_not_enrolled(self, db_session):
        """A strict pin to another project's runner holds with reason_code
        not_enrolled (authoring-time 422 normally prevents this; dispatch stays
        truthful for agent-authored payloads that slipped one through)."""
        project = await _make_project(db_session)
        other_project = await _make_project(db_session, name="Other Project")
        foreign, _ = await _make_runner(db_session, other_project.id)
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(foreign.id), "strict": True}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        hold = (task.context or {}).get("runner_pin_hold")
        assert hold is not None
        assert hold["reason_code"] == "not_enrolled"
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_strict_runner_pin_holds_when_missing_gpu(self, db_session):
        """A strict pin to a live CPU runner for a GPU job holds with the
        capability-specific reason, not a generic one."""
        project = await _make_project(db_session)
        cpu_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    accelerator="T4",
                    runner_target={"runner_id": str(cpu_runner.id), "strict": True},
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [cpu_runner]}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        hold = (task.context or {}).get("runner_pin_hold")
        assert hold is not None
        assert hold["reason_code"] == "missing_gpu"
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_strict_runner_pin_accepts_nvidia_hook_without_cdi(
        self, db_session
    ):
        """A Docker GPU runner may use the NVIDIA runtime hook when CDI is absent."""
        project = await _make_project(db_session)
        docker_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "gpu_count": 1,
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": False},
                "container_gpu_passthrough": {"docker": {"verified": True, "mode": "hook"}},
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    accelerator="T4",
                    container_image="my-image:latest",
                    runner_target={"runner_id": str(docker_runner.id), "strict": True},
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [docker_runner]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert "runner_pin_hold" not in (task.context or {})
        assert await _compute_job_for_task(db_session, task.id) is not None

    @pytest.mark.asyncio
    async def test_strict_runner_pin_accepts_nvidia_hook_for_vram_only_job(
        self, db_session
    ):
        """The NVIDIA runtime hook also satisfies a VRAM-only container request."""
        project = await _make_project(db_session)
        docker_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_free_vram_mib": [40960],  # plenty of free VRAM
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": False},
                "container_gpu_passthrough": {"docker": {"verified": True, "mode": "hook"}},
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    vram_gb=8.0,
                    container_image="my-image:latest",
                    runner_target={"runner_id": str(docker_runner.id), "strict": True},
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [docker_runner]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert "runner_pin_hold" not in (task.context or {})
        assert await _compute_job_for_task(db_session, task.id) is not None

    @pytest.mark.asyncio
    async def test_launch_holds_when_no_gpu_arch_compatible_runner(self, db_session):
        """ACCEPTANCE: an image declaring a min_compute_capability above a runner's
        advertised gpu_compute_capabilities is NOT dispatched to that runner — the
        job holds QUEUED with an honest 'awaiting GPU with compute capability >= X.Y'
        reason instead of being sent and failing the in-sandbox probe."""
        project = await _make_project(db_session)
        # A V100 box (sm_70) that is otherwise perfectly eligible: GPU-capable,
        # container-capable, right task types — only its GPU ARCH is too old.
        v100_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_names": ["Tesla V100"],
                "gpu_compute_capabilities": [7.0],
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": True},
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    accelerator="gpu",
                    container_image="my-sm80-image:latest",
                    min_compute_capability=8.0,
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [v100_runner]}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        # Honest, arch-specific reason surfaced to the operator.
        assert "compute capability >= 8.0" in (task.latest_status_message or ""), (
            f"expected an arch-specific hold reason, got "
            f"{task.latest_status_message!r}"
        )
        # No job was dispatched to the incompatible box.
        assert await _compute_job_for_task(db_session, task.id) is None
        # Reuses the bounded-wait container-hold stamp so the existing sweep covers it.
        assert (task.context or {}).get("awaiting_container_runner_since")

    @pytest.mark.asyncio
    async def test_strict_pin_holds_when_gpu_arch_incompatible(self, db_session):
        """A strict pin to a runner whose GPU arch is below the image's floor holds
        with the incompatible_gpu_arch reason, not a generic not_eligible."""
        project = await _make_project(db_session)
        v100_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_names": ["Tesla V100"],
                "gpu_compute_capabilities": [7.0],
                "max_concurrent": 2,
                "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": True},
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    accelerator="gpu",
                    container_image="my-sm80-image:latest",
                    min_compute_capability=8.0,
                    runner_target={"runner_id": str(v100_runner.id), "strict": True},
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [v100_runner]}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        hold = (task.context or {}).get("runner_pin_hold")
        assert hold is not None
        assert hold["reason_code"] == "incompatible_gpu_arch", (
            f"expected 'incompatible_gpu_arch', got {hold['reason_code']}"
        )
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_launch_pins_arch_compatible_runner(self, db_session):
        """Sanity: an image floor MET by the runner's GPU arch dispatches normally —
        the gate is a no-op when the hardware is compatible."""
        project = await _make_project(db_session)
        l40s_runner, _ = await _make_runner(
            db_session,
            project.id,
            capabilities={
                "task_types": "all",
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_names": ["L40S"],
                "gpu_compute_capabilities": [8.9],
                "max_concurrent": 2,
            },
        )
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload(accelerator="L4", min_compute_capability=8.0)],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [l40s_runner]}, {}
        )
        assert outcome == "launched"
        await db_session.refresh(task)
        job = await _compute_job_for_task(db_session, task.id)
        assert job is not None
        assert (job.polling_metadata or {}).get("runner_target_id") == str(
            l40s_runner.id
        )

    @pytest.mark.asyncio
    async def test_strict_pin_hold_stable_reason_not_rewritten_each_cycle(
        self, db_session
    ):
        """Churn gate: repeated hold cycles with the SAME reason_code but a
        different volatile detail number (live load 3 → 4) must NOT rewrite
        the stamped hold record or the status message (first-write values
        retained); a genuinely DIFFERENT reason_code rewrites both."""
        project = await _make_project(db_session)
        caps = {
            "task_types": "all",
            "has_gpu": False,
            "max_concurrent": 1,
            "active_compute_job_ids": ["j1", "j2", "j3"],  # load 3
        }
        pinned, _ = await _make_runner(db_session, project.id, capabilities=caps)
        task = Task(
            project_id=project.id,
            payloads=[
                _ce_payload(
                    runner_target={"runner_id": str(pinned.id), "strict": True}
                )
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned]}, {}
        )
        assert outcome == "held"
        await db_session.refresh(task)
        first_hold = dict(task.context["runner_pin_hold"])
        first_message = task.latest_status_message
        assert first_hold["reason_code"] == "at_capacity"
        assert "(3 active+pinned" in first_hold["detail"]

        # Cycle 2: still at_capacity, but the volatile load count moved 3 → 4.
        pinned.capabilities = dict(
            caps, active_compute_job_ids=["j1", "j2", "j3", "j4"]
        )
        db_session.add(pinned)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [pinned]}, {}
        )
        assert outcome == "held"
        await db_session.refresh(task)
        # First-write values retained: same stable hold state, no per-cycle
        # rewrite of the held task row.
        assert task.context["runner_pin_hold"] == first_hold
        assert task.latest_status_message == first_message

        # Cycle 3: a DIFFERENT reason_code (runner gone from the live cache →
        # offline) rewrites both the record and the message.
        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )
        assert outcome == "held"
        await db_session.refresh(task)
        hold = task.context["runner_pin_hold"]
        assert hold["reason_code"] == "offline"
        assert "offline" in (task.latest_status_message or "")
        assert task.latest_status_message != first_message

    # ── Transient vs terminal launch preconditions ────────────────────────────
    # create_compute_job raises ComputeLaunchPreconditionFailed for a whole family
    # of reasons. They are NOT all failures: the project's compute-job concurrency
    # cap is pure back-pressure (active jobs only shrink absent new launches), so
    # the N+1th experiment in a capped project used to be PERMANENTLY FAILED for a
    # queueing decision the orchestrator itself made — burning the direction's
    # failure budget and feeding the churn guardrail phantom failures.

    @pytest.mark.asyncio
    async def test_concurrency_cap_defers_and_stays_retryable(self, db_session):
        """At the project's compute-job cap → DEFER, never terminal-FAIL."""
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="CapDeferProject")
        # Fill the project to its cap with an active job.
        db_session.add(
            ComputeJob(
                project_id=project.id,
                command="sleep 1",
                status=ComputeJobStatus.RUNNING,
                spend_cap_usd=1.0,
                deadline_seconds=300,
                backend="local",
            )
        )
        await db_session.commit()

        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 1}
        try:
            outcome = await orch._dispatch_compute_experiment_directly(
                db_session, task, {}, {}
            )
        finally:
            runtime_config["max_concurrent_compute_jobs_by_project"] = orig

        assert outcome == "deferred", f"expected 'deferred', got {outcome!r}"
        await db_session.refresh(task)
        # The task is RETRYABLE: still QUEUED, behind a retry_after backoff.
        assert task.status == TaskStatus.QUEUED, (
            "a transient cap must never leave the task FAILED — it launches "
            "unchanged once a running job frees the cap"
        )
        assert task.retry_after_until is not None, (
            "the deferral must stamp a retry_after backoff so the task re-enters "
            "dispatch on its own"
        )
        ctx = task.context or {}
        assert ctx.get("_infra_wait") is True
        # The reason is SURFACED, not swallowed.
        assert "cap" in (ctx.get("compute_launch_deferred_reason") or "").lower()
        assert "cap" in (task.latest_status_message or "").lower()
        # Crucially: no failure budget burned, and no billable job created.
        assert "failure_category" not in ctx, (
            "a deferral is not a failure and must not be classified as one"
        )
        assert await _compute_job_for_task(db_session, task.id) is None

    @pytest.mark.asyncio
    async def test_repeated_cap_deferrals_escalate_the_backoff(self, db_session):
        """A project sitting at its cap for hours must back off, not re-probe every
        cycle. The counter is owned by the deferral path precisely because the shared
        `_retry_after_resets` is cleared whenever a backoff lapses."""
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="CapBackoffProject")
        db_session.add(
            ComputeJob(
                project_id=project.id,
                command="sleep 1",
                status=ComputeJobStatus.RUNNING,
                spend_cap_usd=1.0,
                deadline_seconds=300,
                backend="local",
            )
        )
        await db_session.commit()

        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 1}
        try:
            for expected_count in (1, 2, 3):
                # Mimic the candidate scan clearing the lapsed backoff each cycle.
                task.retry_after_until = None
                db_session.add(task)
                await db_session.commit()

                outcome = await orch._dispatch_compute_experiment_directly(
                    db_session, task, {}, {}
                )
                assert outcome == "deferred"
                await db_session.refresh(task)
                assert (task.context or {}).get(
                    "compute_launch_defer_count"
                ) == expected_count
        finally:
            runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    @pytest.mark.asyncio
    async def test_saturated_cap_deferral_raises_one_operator_alert(self, db_session):
        """A deferral is unbounded BY DESIGN — so a saturated one needs a surface.

        Deferring instead of terminal-FAILing removed a loud (wrong) signal. Without an
        alert it is replaced by a SILENT one: the task parks behind the 15-min ceiling
        forever, the defer counter only resets on a successful launch, and
        operator_attention's stale-queued sweep filters on `depends_on` being non-empty,
        so a dep-less compute_experiment never surfaces there.

        Exactly ONE alert (deduplicated), and the task keeps deferring — the alert is a
        surface, not an outcome: no failure_category, still QUEUED, still retryable.
        """
        from sqlmodel import select as _select
        from app.models import Alert, AlertSeverity
        from app.shared_state import runtime_config
        from app.orchestrator_runner_dispatch import (
            _COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS,
            _COMPUTE_CAP_DEFER_ALERT_TITLE,
        )

        project = await _make_project(db_session, name="CapAlertProject")
        db_session.add(
            ComputeJob(
                project_id=project.id,
                command="sleep 1",
                status=ComputeJobStatus.RUNNING,
                spend_cap_usd=1.0,
                deadline_seconds=300,
                backend="local",
            )
        )
        await db_session.commit()

        # One deferral short of the alert threshold.
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
            context={
                "compute_launch_defer_count": (
                    _COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS - 1
                )
            },
        )
        db_session.add(task)
        await db_session.commit()

        orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 1}
        try:
            # This deferral crosses the threshold → alert.
            assert (
                await orch._dispatch_compute_experiment_directly(
                    db_session, task, {}, {}
                )
                == "deferred"
            )
            # …and the NEXT one must not raise a second.
            task.retry_after_until = None
            db_session.add(task)
            await db_session.commit()
            assert (
                await orch._dispatch_compute_experiment_directly(
                    db_session, task, {}, {}
                )
                == "deferred"
            )
        finally:
            runtime_config["max_concurrent_compute_jobs_by_project"] = orig

        alerts = (
            (
                await db_session.execute(
                    _select(Alert).where(
                        Alert.task_id == task.id,
                        Alert.title == _COMPUTE_CAP_DEFER_ALERT_TITLE,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1, (
            "a saturated cap deferral must raise exactly ONE operator alert, "
            f"deduplicated across every subsequent deferral; got {len(alerts)}"
        )
        assert alerts[0].severity == AlertSeverity.WARNING

        # The alert is a SURFACE, not an outcome — the task keeps deferring cleanly.
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.retry_after_until is not None
        assert (task.context or {}).get("failure_category") is None

        # DISMISSAL must not silence the surface forever. A deferral is unbounded by
        # design, so a task can keep deferring long after an operator dismisses the
        # alert — if the dedup predicate ignored dismissed_at, that task would go
        # permanently dark. Dismissing means "seen", not "never tell me again".
        alerts[0].dismissed_at = _utcnow()
        db_session.add(alerts[0])
        task.retry_after_until = None
        db_session.add(task)
        await db_session.commit()

        runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 1}
        try:
            assert (
                await orch._dispatch_compute_experiment_directly(
                    db_session, task, {}, {}
                )
                == "deferred"
            )
        finally:
            runtime_config["max_concurrent_compute_jobs_by_project"] = orig

        live = (
            (
                await db_session.execute(
                    _select(Alert).where(
                        Alert.task_id == task.id,
                        Alert.title == _COMPUTE_CAP_DEFER_ALERT_TITLE,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(live) == 1, (
            "after the operator DISMISSES the alert, a task that is still deferring "
            "must be able to re-raise it — otherwise one dismissal silences an "
            f"unbounded deferral forever; got {len(live)} live alert(s)"
        )

    @pytest.mark.asyncio
    async def test_long_lived_deferral_does_not_overflow_and_quarantine_the_task(
        self, db_session
    ):
        """A task that has been CORRECTLY deferring for a long time must keep deferring.

        The backoff is ``base_minutes * (2 ** prior_defers)``. ``float * int`` raises
        ``OverflowError: int too large to convert to float`` once ``prior_defers >= 1024``
        — and that raise happens inside ``_dispatch_compute_experiment_directly``, i.e.
        inside the dispatch loop's per-task containment, so the task would start crashing
        every cycle and be QUARANTINED as a poison task. It is reachable: a project pinned
        at its compute cap defers every ~15 min, so the counter passes 1024 after ~10.6
        days. Clamping the EXPONENT keeps the arithmetic finite while the backoff has long
        since saturated at its ceiling.

        Negative control: with an unclamped ``2 ** prior_defers`` this raises OverflowError.
        """
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="CapOverflowProject")
        db_session.add(
            ComputeJob(
                project_id=project.id,
                command="sleep 1",
                status=ComputeJobStatus.RUNNING,
                spend_cap_usd=1.0,
                deadline_seconds=300,
                backend="local",
            )
        )
        await db_session.commit()

        # A task that has been deferring for well over a week at the 15-min ceiling.
        task = Task(
            project_id=project.id,
            payloads=[_ce_payload()],
            status=TaskStatus.QUEUED,
            context={"compute_launch_defer_count": 5000},
        )
        db_session.add(task)
        await db_session.commit()

        orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
        runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 1}
        try:
            outcome = await orch._dispatch_compute_experiment_directly(
                db_session, task, {}, {}
            )
        finally:
            runtime_config["max_concurrent_compute_jobs_by_project"] = orig

        assert outcome == "deferred", (
            "a long-lived deferral must keep DEFERRING, not raise out of the dispatch "
            "path and get quarantined as a poison task"
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.retry_after_until is not None, (
            "the task must still be stamped retryable after a saturated backoff"
        )
        assert (task.context or {}).get("compute_launch_defer_count") == 5001

    @pytest.mark.asyncio
    async def test_hardware_policy_violation_still_terminal_fails(self, db_session):
        """Genuine misconfig must REMAIN terminal. A task asking for an accelerator
        the project forbids never self-clears — deferring it would wedge it forever,
        so it keeps burning the failure budget exactly as before."""
        project = await _make_project(db_session, name="HwPolicyFailProject")
        project.build_config = {
            **(project.build_config or {}),
            "resource_envelope": {"allowed_accelerator_types": ["T4"]},
        }
        db_session.add(project)
        await db_session.commit()

        task = Task(
            project_id=project.id,
            # A100 is not in the project's allow-list → project_hardware_policy_violated
            payloads=[_ce_payload(accelerator="A100")],
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {}, {}
        )

        assert outcome == "failed", (
            f"a hardware-policy violation is misconfig, not back-pressure; "
            f"got {outcome!r}"
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert (task.context or {}).get("failure_category") == "compute_launch_failed"
        assert task.retry_after_until is None, (
            "a terminal misconfig must not be stamped retryable"
        )
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


async def _add_runner_binding(session, runner_id, project_id):
    """Add one runner<->project allow-list binding row and commit it."""
    binding = RunnerProjectBinding(runner_id=runner_id, project_id=project_id)
    session.add(binding)
    await session.commit()
    return binding


@pytest.mark.asyncio
async def test_runner_cannot_claim_job_for_non_allowlisted_project(
    client: AsyncClient, db_session, caplog
):
    """Server-side allow-list is a hard job-claim authorization boundary.

    A runner bound to project A must be rejected — at BOTH the claim
    (/next-compute-job) and report (/compute-job/{id}/report) surfaces — for a
    compute job in project B that is NOT on its allow-list; the job stays STARTING
    (never claimed) and a cross-project WARNING fires. Adding B to the allow-list
    makes the SAME claim succeed (STARTING→RUNNING). Finally, one runner bound to
    A AND B serves agent tasks from both projects under ONE unified max_concurrent
    ledger: a third task holds once the unified limit is reached, regardless of
    which project it belongs to.
    """
    import logging

    project_a = await _make_project(db_session, name="Allowlist A")
    project_b = await _make_project(db_session, name="Allowlist B")
    # Runner's home is A, with an explicit allow-list binding to A only.
    runner, token = await _make_runner(
        db_session,
        project_a.id,
        capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
    )
    await _add_runner_binding(db_session, runner.id, project_a.id)
    assert await allowed_project_ids(db_session, runner) == {project_a.id}

    # A STARTING compute job in project B, pinned to this runner's id.
    job_b = ComputeJob(
        project_id=project_b.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.STARTING,
        polling_metadata={"runner_target_id": str(runner.id), "command": "echo b"},
    )
    db_session.add(job_b)
    await db_session.commit()
    await db_session.refresh(job_b)

    # (1) CLAIM rejected: B is not on the allow-list, so nothing is dispatchable
    # (204) and the job is NOT transitioned out of STARTING.
    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text
    await db_session.refresh(job_b)
    assert job_b.status == ComputeJobStatus.STARTING

    # (2) REPORT rejected server-side (404) with a genuine cross-project warning.
    with caplog.at_level(logging.WARNING, logger="blueprint"):
        resp = await client.post(
            f"/api/runner/compute-job/{job_b.id}/report",
            json={"exec_status": "succeeded", "exit_code": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert any(
        "Cross-project" in r.getMessage() for r in caplog.records
    ), "a non-allowlisted-project report must emit the cross-project warning"
    await db_session.refresh(job_b)
    # Evidence untouched — the rejected report never stamped an exec status.
    assert job_b.status == ComputeJobStatus.STARTING
    assert "runner_exec_status" not in (job_b.polling_metadata or {})

    # (3) Add B to the allow-list → the SAME claim now succeeds (STARTING→RUNNING).
    await _add_runner_binding(db_session, runner.id, project_b.id)
    assert await allowed_project_ids(db_session, runner) == {
        project_a.id,
        project_b.id,
    }
    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"] == str(job_b.id)
    await db_session.refresh(job_b)
    assert job_b.status == ComputeJobStatus.RUNNING

    # (4) UNIFIED LEDGER: the runner is now fanned into BOTH project buckets, and a
    # burst of agent tasks across the two projects is sized against ONE
    # max_concurrent=2 ledger — the third pin is refused no matter its project.
    cache = await orch._load_active_runners_cache(db_session)
    assert runner.id in {r.id for r in cache.get(project_a.id, [])}
    assert runner.id in {r.id for r in cache.get(project_b.id, [])}

    task_a1 = Task(
        project_id=project_a.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    task_b1 = Task(
        project_id=project_b.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    task_a2 = Task(
        project_id=project_a.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([task_a1, task_b1, task_a2])
    await db_session.commit()

    pins: dict = {}
    # First task (project A) pins.
    assert await orch._try_runner_dispatch(db_session, task_a1, cache, pins) is True
    # Second task (project B) pins onto the SAME runner — the pin count is shared
    # across projects, so this is the second unit of the unified ledger.
    assert await orch._try_runner_dispatch(db_session, task_b1, cache, pins) is True
    assert pins.get(runner.id) == 2
    # Third task (project A again) must HOLD: unified limit (2) reached across both
    # projects — proving the ledger is per-runner, not per-project.
    assert await orch._try_runner_dispatch(db_session, task_a2, cache, pins) is False
    await db_session.refresh(task_a2)
    assert task_a2.status == TaskStatus.QUEUED
    await db_session.refresh(task_a1)
    await db_session.refresh(task_b1)
    assert task_a1.status == TaskStatus.PROVISIONING
    assert task_b1.status == TaskStatus.PROVISIONING


class TestMidRunViewableUpload:
    """Tests for POST /api/runner/compute-job/{job_id}/viewable.

    Verifies that a RUNNING compute job's mid-run viewable-file upload registers a
    ``ComputeJobFile`` row and an entry in ``polling_metadata['viewable_files']``
    (surfaced as the admin API's ``viewable_artifacts``) without setting the terminal
    guard key ``viewable_file_count``, and that the terminal reconcile produces no
    duplicate rows.
    """

    async def _make_running_job(
        self, db_session, status=ComputeJobStatus.RUNNING, **extra_meta
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)
        meta = {"runner_target_id": str(runner.id)}
        meta.update(extra_meta)
        job = ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=status,
            polling_metadata=meta,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return project, runner, token, job

    @staticmethod
    def _fake_uri(job_id, safe_name: str) -> str:
        return (
            "gs://test-bucket/compute-jobs/"
            f"{job_id}/artifacts/viewable/{safe_name}"
        )

    @pytest.mark.asyncio
    async def test_upload_png_on_running_job_creates_row_and_manifest(
        self, client, db_session
    ):
        """Upload a .png to a RUNNING job → ComputeJobFile row + viewable_files entry."""
        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "reward_curve.png"
        fake_uri = self._fake_uri(job.id, safe_name)

        with patch("app.storage.upload_compute_job_viewable_file",
                   return_value=fake_uri):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"fake-png-bytes-12345", "image/png")},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["total_viewable_files"] == 1
        assert body["entry"]["storage_uri"] == fake_uri
        assert body["entry"]["kind"] == "image"
        assert body["entry"]["filename"] == safe_name

        # ── ComputeJobFile row exists ────────────────────────────────────────
        rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == fake_uri,
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            f"Expected exactly 1 ComputeJobFile row, got {len(rows)}"
        )
        assert rows[0].safe_relpath == safe_name
        assert rows[0].media_type == "image"

        # ── polling_metadata['viewable_files'] contains the entry ────────────
        await db_session.refresh(job)
        vf = (job.polling_metadata or {}).get("viewable_files", [])
        assert len(vf) == 1, (
            f"Expected 1 viewable_files entry, got {len(vf)}"
        )
        assert vf[0]["storage_uri"] == fake_uri

        # ── viewable_file_count MUST NOT be set (terminal guard key) ─────────
        assert "viewable_file_count" not in (job.polling_metadata or {}), (
            "mid-run upload must not set the terminal guard key viewable_file_count"
        )

    @pytest.mark.asyncio
    async def test_non_viewable_file_type_rejected(self, client, db_session):
        """Upload of a .txt file → 422."""
        _project, runner, token, job = await self._make_running_job(db_session)

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/viewable",
            data={"filename": "notes.txt"},
            files={"file": ("notes.txt", b"not an image", "text/plain")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 for non-viewable type, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_runner_generation_retry_keeps_revision(self, client, db_session):
        """A response-loss retry for one runner generation is idempotent."""
        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "reward.png"
        fake_uri = self._fake_uri(job.id, safe_name)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Blueprint-Viewable-Revision": "runner-generation-1",
        }

        with patch(
            "app.storage.upload_compute_job_viewable_file", return_value=fake_uri
        ):
            first = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"same-bytes", "image/png")},
                headers=headers,
            )
            retry = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"same-bytes", "image/png")},
                headers=headers,
            )

        assert first.status_code == 200
        assert retry.status_code == 200
        assert first.json()["entry"]["revision"] == "runner-generation-1"
        assert retry.json()["entry"]["revision"] == "runner-generation-1"

    @pytest.mark.asyncio
    async def test_invalid_runner_revision_rejected_before_storage(
        self, client, db_session
    ):
        _project, runner, token, job = await self._make_running_job(db_session)
        with patch("app.storage.upload_compute_job_viewable_file") as upload:
            response = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": "reward.png"},
                files={"file": ("reward.png", b"bytes", "image/png")},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Blueprint-Viewable-Revision": "bad revision/with spaces",
                },
            )
        assert response.status_code == 422
        upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversize_image_rejected(self, client, db_session):
        """Image exceeding the per-file byte cap → 413."""
        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "huge.png"
        fake_uri = self._fake_uri(job.id, safe_name)

        # Patch the per-image byte cap to a small value so a tiny payload
        # triggers the limit without allocating 8 MiB of test data.
        from app.routers import runner as runner_mod

        with patch.object(
            runner_mod, "_MAX_VIEWABLE_FILE_BYTES", 10
        ), patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={
                    "file": (
                        safe_name,
                        b"x" * 100,  # 100 bytes > patched cap of 10
                        "image/png",
                    )
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 413, (
            f"Expected 413 for oversize file, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_terminal_job_rejected(self, client, db_session):
        """Upload to a terminal job → 409."""
        _project, runner, token, job = await self._make_running_job(
            db_session, status=ComputeJobStatus.SUCCEEDED
        )

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/viewable",
            data={"filename": "reward.png"},
            files={"file": ("reward.png", b"data", "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409, (
            f"Expected 409 for terminal job, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_terminal_winning_during_live_upload_keeps_terminal_object(
        self, client, db_session
    ):
        """A late live upload must never overwrite terminal's canonical object.

        The fake storage write models the precise ordering from the race report:
        terminal reconciliation publishes its canonical bytes while the live
        request is stalled, then the stalled write completes.  Staging means the
        latter can only write an unreferenced object, and the locked re-check
        returns 409 without promoting it.
        """
        from app import storage

        _project, _runner, token, job = await self._make_running_job(db_session)
        safe_name = "reward_curve.png"
        objects: dict[str, bytes] = {}
        deleted: list[str] = []

        async def _late_live_upload(job_id, basename, data, content_type):
            # Terminal owns the canonical generation before the slow live write
            # returns.  This commit is observable by the endpoint's locked reread.
            job.status = ComputeJobStatus.SUCCEEDED
            db_session.add(job)
            await db_session.commit()
            objects[safe_name] = b"terminal-generation"
            objects[basename] = data
            return storage.compute_job_viewable_uri(job_id, basename)

        async def _delete_staging(job_id, basename):
            deleted.append(basename)
            objects.pop(basename, None)
            return True

        with patch(
            "app.storage.upload_compute_job_viewable_file", new=_late_live_upload
        ), patch(
            "app.storage.delete_compute_job_viewable_file", new=_delete_staging
        ), patch(
            "app.storage.promote_compute_job_viewable_file",
            new=AsyncMock(side_effect=AssertionError("terminal upload must not promote")),
        ):
            response = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"late-live-bytes", "image/png")},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 409, response.text
        assert objects[safe_name] == b"terminal-generation"
        assert len(deleted) == 1 and deleted[0].startswith(".staging/")

    @pytest.mark.asyncio
    async def test_file_cap_enforced(self, client, db_session):
        """Upload at the 25-file cap → 429."""
        from app.compute_backends import _MAX_VIEWABLE_FILE_COUNT

        _project, runner, token, job = await self._make_running_job(db_session)

        # Pre-fill viewable_files with `_MAX_VIEWABLE_FILE_COUNT` entries.
        preexisting = []
        for i in range(_MAX_VIEWABLE_FILE_COUNT):
            fname = f"file_{i:03d}.png"
            furi = self._fake_uri(job.id, fname)
            preexisting.append({
                "filename": fname,
                "storage_uri": furi,
                "content_type": "image/png",
                "size_bytes": 100,
                "kind": "image",
            })
        job.polling_metadata = {
            **(job.polling_metadata or {}),
            "viewable_files": preexisting,
        }
        db_session.add(job)
        await db_session.commit()

        overflow_name = "overflow.png"
        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=self._fake_uri(job.id, overflow_name),
        ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": overflow_name},
                files={
                    "file": (
                        overflow_name,
                        b"data",
                        "image/png",
                    )
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 429, (
            f"Expected 429 at file cap, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_large_video_uses_streaming_seam(self, client, db_session):
        """A video body that rolls past the in-memory spool threshold is uploaded
        via the STREAMING seam (``upload_compute_job_viewable_stream``) — its bytes
        are never buffered whole through the in-memory
        ``upload_compute_job_viewable_file`` path (the OOM vector the review
        flagged). The streamed file object yields EXACTLY the file part, with no
        multipart framing leaked, and the recorded size matches the payload."""
        from app.routers import runner as runner_mod

        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "rollout.mp4"
        fake_uri = self._fake_uri(job.id, safe_name)
        video_bytes = b"\x00\x01FAKE-MP4-PAYLOAD" * 64  # ~1 KiB, distinctive

        captured: dict = {}

        async def _fake_stream(job_id, basename, fileobj, content_type, *, size=None):
            # Drain the streamed file object exactly as the GCS resumable upload
            # does, capturing what actually crosses the seam.
            captured["read"] = fileobj.read()
            captured["size"] = size
            captured["content_type"] = content_type
            captured["basename"] = basename
            return fake_uri

        # Shrink the spool threshold so the modest test payload rolls to disk and
        # takes the streaming branch. The in-memory seam MUST NOT be touched.
        with patch.object(runner_mod, "_MAX_VIEWABLE_FILE_BYTES", 8), \
                patch.object(runner_mod, "_MULTIPART_BODY_MARGIN", 8), \
                patch("app.storage.upload_compute_job_viewable_stream", new=_fake_stream), \
                patch(
                    "app.storage.upload_compute_job_viewable_file",
                    new=AsyncMock(side_effect=AssertionError(
                        "rolled video must not use the in-memory upload seam"
                    )),
                ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, video_bytes, "video/mp4")},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200, (
            f"Expected 200 for streamed video, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["entry"]["kind"] == "video"
        assert body["entry"]["storage_uri"] == fake_uri
        assert body["entry"]["size_bytes"] == len(video_bytes)

        # The streamed bytes are EXACTLY the file part — no multipart framing.
        assert captured["read"] == video_bytes
        assert captured["size"] == len(video_bytes)
        assert captured["content_type"] == "video/mp4"
        assert captured["basename"].startswith(".staging/")
        assert captured["basename"].endswith(f"/{safe_name}")

        # DB row + manifest recorded with the streamed size.
        await db_session.refresh(job)
        vf = (job.polling_metadata or {}).get("viewable_files", [])
        assert len(vf) == 1 and vf[0]["size_bytes"] == len(video_bytes)
        rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == fake_uri,
                )
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0].media_type == "video"
        assert rows[0].file_size_bytes == len(video_bytes)

    @pytest.mark.asyncio
    async def test_reupload_same_relpath_advances_revision_and_terminal_reconciles(
        self, client, db_session
    ):
        """Same URI/size advances live revision; terminal replaces it once."""
        from app import compute_jobs as compute_jobs_mod

        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "reward_curve.png"
        fake_uri = self._fake_uri(job.id, safe_name)

        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            # Equal-size generations exercise the real defect: storage_uri and
            # byte length cannot distinguish the overwritten bytes.
            resp1 = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={
                    "file": (
                        safe_name,
                        b"version-one",
                        "image/png",
                    )
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp1.status_code == 200, (
                f"First upload failed: {resp1.status_code}: {resp1.text}"
            )
            assert resp1.json()["total_viewable_files"] == 1
            first_revision = resp1.json()["entry"]["revision"]
            assert resp1.json()["entry"]["live"] is True

            # Second upload: same relpath, same deterministic URI, same size.
            resp2 = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={
                    "file": (
                        safe_name,
                        b"version-two",
                        "image/png",
                    )
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 200, (
                f"Second upload failed: {resp2.status_code}: {resp2.text}"
            )
            assert resp2.json()["total_viewable_files"] == 1, (
                "Re-upload must not increase the manifest count"
            )
            second_revision = resp2.json()["entry"]["revision"]
            assert second_revision != first_revision
            assert resp2.json()["entry"]["storage_uri"] == fake_uri

        # ── Only ONE ComputeJobFile row ──────────────────────────────────────
        rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == fake_uri,
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            f"Re-upload must not create duplicate ComputeJobFile row: got "
            f"{len(rows)} rows for storage_uri={fake_uri}"
        )

        # ── Only ONE manifest entry ──────────────────────────────────────────
        await db_session.refresh(job)
        vf = (job.polling_metadata or {}).get("viewable_files", [])
        assert len(vf) == 1, (
            f"Expected 1 viewable_files entry after re-upload, got {len(vf)}"
        )
        assert vf[0]["revision"] == second_revision
        assert vf[0]["live"] is True

        # The terminal bundle is authoritative. Its legacy-shaped result has no
        # revision, so reconciliation must assign a final one, clear ``live``,
        # replace the manifest in place, and preserve the single signing row.
        job.status = ComputeJobStatus.SUCCEEDED
        db_session.add(job)
        await db_session.commit()
        terminal_manifest = [
            {
                "filename": safe_name,
                "storage_uri": fake_uri,
                "content_type": "image/png",
                "size_bytes": len(b"version-two"),
                "kind": "image",
            }
        ]
        with patch.object(
            compute_jobs_mod,
            "persist_viewable_artifacts_with_completeness",
            new=AsyncMock(return_value=(terminal_manifest, 0, True)),
        ):
            final_manifest, dropped = (
                await compute_jobs_mod.persist_viewable_files_for_job(db_session, job)
            )
        await db_session.commit()

        assert dropped == 0
        assert len(final_manifest) == 1
        assert final_manifest[0]["revision"] != second_revision
        assert final_manifest[0]["live"] is False
        await db_session.refresh(job)
        assert (job.polling_metadata or {})["viewable_files"] == final_manifest
        final_rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == fake_uri,
                )
            )
        ).scalars().all()
        assert len(final_rows) == 1

    @pytest.mark.asyncio
    async def test_no_filename_rejected(self, client, db_session):
        """Upload without filename → 400."""
        _project, runner, token, job = await self._make_running_job(db_session)

        resp = await client.post(
            f"/api/runner/compute-job/{job.id}/viewable",
            # Only the file, no filename field
            files={"file": ("img.png", b"data", "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, (
            f"Expected 400 for missing filename, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_terminal_reconcile_no_duplicate_row_for_midrun_file(
        self, client, db_session
    ):
        """Must-hold invariant: mid-run upload then terminal explode ⇒ ONE row.

        Exercises the CROSS-PATH composition the task called out: a file uploaded
        via the mid-run endpoint (``POST .../viewable``) then reconciled by the
        terminal ``persist_viewable_files_for_job`` pass. Because the terminal pass
        recomputes the authoritative superset manifest from the bundle and — for a
        file already uploaded mid-run — carries the IDENTICAL canonical
        ``storage_uri``, the SAVEPOINT + UNIQUE(storage_uri) insert in
        ``_record_viewable_job_files`` is a benign no-op: exactly one
        ``ComputeJobFile`` row survives for the mid-run relpath, and the terminal
        guard key ``viewable_file_count`` is stamped.
        """
        from app import compute_jobs as compute_jobs_mod

        _project, runner, token, job = await self._make_running_job(db_session)
        safe_name = "reward_curve.png"
        fake_uri = self._fake_uri(job.id, safe_name)

        # ── 1. Mid-run upload via the endpoint (RUNNING job) ─────────────────
        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"midrun-png-bytes", "image/png")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200, (
            f"Mid-run upload failed: {resp.status_code}: {resp.text}"
        )

        # One row now exists; the terminal guard key must NOT be set yet.
        await db_session.refresh(job)
        assert "viewable_file_count" not in (job.polling_metadata or {}), (
            "mid-run upload must not stamp the terminal guard key"
        )

        # ── 2. Job goes terminal, then the terminal viewable pass runs ───────
        job.status = ComputeJobStatus.SUCCEEDED
        db_session.add(job)
        await db_session.commit()

        # The terminal pass recomputes the authoritative superset from the
        # bundle. Simulate that superset: it INCLUDES the mid-run file (identical
        # storage_uri — this is what makes the dedup hold) plus a second file the
        # bundle contained but which was never pushed mid-run.
        second_name = "rollout.mp4"
        second_uri = self._fake_uri(job.id, second_name)
        terminal_manifest = [
            {
                "filename": safe_name,
                "storage_uri": fake_uri,
                "content_type": "image/png",
                "size_bytes": len(b"midrun-png-bytes"),
                "kind": "image",
            },
            {
                "filename": second_name,
                "storage_uri": second_uri,
                "content_type": "video/mp4",
                "size_bytes": 4096,
                "kind": "video",
            },
        ]

        with patch.object(
            compute_jobs_mod,
            "persist_viewable_artifacts_with_completeness",
            new=AsyncMock(return_value=(terminal_manifest, 0, True)),
        ):
            manifest, dropped = (
                await compute_jobs_mod.persist_viewable_files_for_job(
                    db_session, job
                )
            )
        db_session.add(job)
        await db_session.commit()

        assert dropped == 0
        assert len(manifest) == 2, (
            f"terminal manifest should be the superset (2 files), got {manifest!r}"
        )

        # ── 3. Exactly ONE ComputeJobFile row for the mid-run relpath ────────
        rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == fake_uri,
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            "terminal reconcile must not duplicate the mid-run file's row: got "
            f"{len(rows)} rows for storage_uri={fake_uri}"
        )

        # The bundle-only file the terminal pass discovered got its own row.
        second_rows = (
            await db_session.execute(
                select(ComputeJobFile).where(
                    ComputeJobFile.compute_job_id == job.id,
                    ComputeJobFile.storage_uri == second_uri,
                )
            )
        ).scalars().all()
        assert len(second_rows) == 1, (
            "terminal pass should register the bundle-only file exactly once"
        )

        # ── 4. Terminal guard key is now stamped ─────────────────────────────
        await db_session.refresh(job)
        meta = job.polling_metadata or {}
        assert meta.get("viewable_file_count") == 2, (
            f"viewable_file_count must be stamped by the terminal pass, got "
            f"{meta.get('viewable_file_count')!r}"
        )

    @pytest.mark.asyncio
    async def test_pdf_viewable_upload_accepted(self, client, db_session):
        """PDF files are a supported viewable kind and must be accepted by the endpoint.

        Finding 12: PDF artifacts should be uploadable via the live viewable
        pipeline (storage.py classifies .pdf as kind='pdf') and the endpoint
        must store the entry with the correct content_type/kind.
        """
        _project, _runner, token, job = await self._make_running_job(db_session)
        safe_name = "report.pdf"
        fake_uri = self._fake_uri(job.id, safe_name)

        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            resp = await client.post(
                f"/api/runner/compute-job/{job.id}/viewable",
                data={"filename": safe_name},
                files={
                    "file": (safe_name, b"%PDF-1.4 tiny", "application/pdf")
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200, (
            f"PDF viewable upload should be accepted, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["entry"]["kind"] == "pdf"
        assert body["entry"]["content_type"] == "application/pdf"
        assert body["entry"]["storage_uri"] == fake_uri

        await db_session.refresh(job)
        vf = (job.polling_metadata or {}).get("viewable_files", [])
        assert len(vf) == 1
        assert vf[0]["kind"] == "pdf"

    @pytest.mark.asyncio
    async def test_bound_secondary_project_viewable_upload_allowed(
        self, client, db_session
    ):
        """A runner bound to a secondary project can upload viewable files for jobs in that project.

        Finding 5: the live-viewable endpoint must authorize against
        allowed_project_ids (the runner's enrollment project UNION its explicit
        bindings), not just runner.project_id. A runner enrolled in project A but
        explicitly bound to project B must be able to push previews for a job in B.
        """
        from app import runner_project_binding as rpb

        # Runner is enrolled in project A.
        _project_a, runner, token, _job_a = await self._make_running_job(db_session)

        # Create project B and a job in it, pinned to this runner.
        project_b = await _make_project(db_session, name="ProjectB")
        job_b = ComputeJob(
            project_id=project_b.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.RUNNING,
            polling_metadata={"runner_target_id": str(runner.id)},
        )
        db_session.add(job_b)
        await db_session.commit()
        await db_session.refresh(job_b)

        # Upload to job_b BEFORE binding — must be rejected (runner not on allow-list).
        safe_name = "reward.png"
        fake_uri = self._fake_uri(job_b.id, safe_name)
        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            resp_before = await client.post(
                f"/api/runner/compute-job/{job_b.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"px", "image/png")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp_before.status_code == 404, (
            f"Upload to unbound project should be 404, got {resp_before.status_code}"
        )

        # Bind the runner to project B.
        await rpb.add_binding(db_session, runner.id, project_b.id)
        await db_session.commit()

        # Now upload must succeed.
        with patch(
            "app.storage.upload_compute_job_viewable_file",
            return_value=fake_uri,
        ):
            resp_after = await client.post(
                f"/api/runner/compute-job/{job_b.id}/viewable",
                data={"filename": safe_name},
                files={"file": (safe_name, b"px", "image/png")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp_after.status_code == 200, (
            f"Upload to bound project should succeed, got "
            f"{resp_after.status_code}: {resp_after.text}"
        )
        body = resp_after.json()
        assert body["ok"] is True
        assert body["entry"]["kind"] == "image"


# ── Capability proof trial: runner job + result ingestion (design 84021e2d §2) ─
#
# Slice 2: a `capability_proof_trial` task runs ONLY a working capability's
# declared covering tests as a targeted runner job; the ingestion completion
# handler mints a genuine `proven` named_test proof on GREEN and records NOTHING
# (never demotes) on RED. Everything is dark, gated on the runtime flag.


async def _make_trial_project(session, cap_name: str):
    """A project carrying one working capability with no proof yet."""
    project = await _make_project(session, name="Proof Trial Project")
    project.problem_model = {"capabilities": [{"name": cap_name, "status": "working"}]}
    project.capability_proof_records = {}
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


def _make_trial_task(project, cap_name: str, test_refs, head_sha="deadbeef"):
    return Task(
        project_id=project.id,
        title=f"Capability proof trial: {cap_name}",
        payloads=[{
            "type": "capability_proof_trial",
            "cap": cap_name,
            "test_refs": test_refs,
            "head_sha": head_sha,
        }],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )


class _TrialFlag:
    """Context manager toggling the dark trial flag and restoring it."""

    def __init__(self, value: bool):
        self.value = value
        self._prev = None

    def __enter__(self):
        from app.shared_state import runtime_config

        self._rc = runtime_config
        self._prev = runtime_config.get("capability_proof_execution_trials_enabled")
        runtime_config["capability_proof_execution_trials_enabled"] = self.value
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            self._rc.pop("capability_proof_execution_trials_enabled", None)
        else:
            self._rc["capability_proof_execution_trials_enabled"] = self._prev
        return False


@pytest.mark.asyncio
async def test_capability_proof_trial_green_records_proven(db_session):
    """A GREEN trial result ingestion mints a genuine `proven` named_test proof
    and the capability reads `proven` (design 84021e2d Slice 2 acceptance seam).

    This exercises the production ingestion handler
    ``_handle_capability_proof_trial_completion`` end-to-end through the shared
    proof recorder and the ``working_capability_proof_status`` reader.
    """
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "language-aware-repo-map"
    test_refs = ["backend/testing/test_prompt.py::test_map"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs, head_sha="cafef00d")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    green = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "cafef00d",
        "test_refs": test_refs,
        "ran": True,
        "passed": True,
        "could_not_run": False,
        "skipped_summary": {"total": 3, "skipped": 0},
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(green), db_session)

    await db_session.refresh(project)

    # The capability now reads the genuine `proven` tier.
    status_map = working_capability_proof_status(project.problem_model)
    assert status_map.get(cap) == "proven"

    # A durable executed-named_test proof was recorded in the ledger.
    entries = project.capability_proof_records[cap]
    assert len(entries) == 1
    e = entries[0]
    assert e["proof_kind"] == "named_test"
    assert e["executed"] is True
    assert e["commit_sha"] == "cafef00d"
    assert e["test_refs"] == test_refs


@pytest.mark.asyncio
async def test_capability_proof_trial_red_records_no_proof(db_session):
    """A RED trial records NO proof and never demotes; the contradiction is
    stamped on the task context for strategist visibility only."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "repo-map-red"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    red = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "deadbeef",
        "test_refs": test_refs,
        "ran": True,
        "passed": False,
        "could_not_run": False,
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(red), db_session)

    await db_session.refresh(project)
    await db_session.refresh(task)

    # No proof recorded — the capability is NOT proven.
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"
    assert cap not in (project.capability_proof_records or {})
    # The red result is surfaced (non-demoting) on the task context.
    contradiction = (task.context or {}).get("capability_proof_trial_contradiction")
    assert contradiction is not None
    assert contradiction["cap"] == cap
    assert contradiction["passed"] is False


@pytest.mark.asyncio
async def test_capability_proof_trial_definitive_red_revokes_prior_green(db_session):
    """A definitive RED trial (ran=True, passed=False, could_not_run=False) REVOKES
    any stale executed-proof ledger entry so the capability cannot keep clearing the
    enforce gate until the 30-day TTL expires.

    Acceptance: 'a definitive red B blocks the capability'.
    """
    import json as _json

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.capability_proof import record_capability_named_test_execution_proof
    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "repo-map-red-revoke"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs, head_sha="sha_b")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # First stamp a GREEN proof at sha_a (revision A).
    record_capability_named_test_execution_proof(
        project, cap, test_refs, head_sha="sha_a", suite_verifying=True
    )
    _flag_modified(project, "capability_proof_records")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Confirm the capability is now "proven" via the execution proof.
    assert (project.capability_proof_records or {}).get(cap) or \
        working_capability_proof_status(project.problem_model).get(cap) == "proven"

    # A RED trial at sha_b (revision B) should revoke the sha_a proof.
    red = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "sha_b",
        "test_refs": test_refs,
        "ran": True,
        "passed": False,
        "could_not_run": False,
    }
    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(red), db_session)

    await db_session.refresh(project)
    await db_session.refresh(task)

    # The named_test_exec ledger entry must be gone.
    norm_cap = cap
    records = project.capability_proof_records or {}
    exec_entries = [
        e for e in (records.get(norm_cap) or [])
        if e.get("task_id") == f"named_test_exec:{norm_cap}"
    ]
    assert exec_entries == [], "definitive RED must revoke the stale execution proof"

    # The contradiction is still surfaced.
    contradiction = (task.context or {}).get("capability_proof_trial_contradiction")
    assert contradiction is not None
    assert contradiction["cap"] == cap
    assert contradiction["passed"] is False
    assert contradiction["proof_revoked"] is True


@pytest.mark.asyncio
async def test_capability_proof_trial_indeterminate_result_leaves_proof(db_session):
    """An indeterminate result (could_not_run=True) must NOT revoke existing proof.

    Acceptance: 'an indeterminate B ... result cannot corrupt the current verdict'.
    """
    import json as _json

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.capability_proof import record_capability_named_test_execution_proof
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "repo-map-infra-fail"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs, head_sha="sha_b")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Stamp a GREEN proof at sha_a.
    record_capability_named_test_execution_proof(
        project, cap, test_refs, head_sha="sha_a", suite_verifying=True
    )
    _flag_modified(project, "capability_proof_records")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Infrastructure failure: could_not_run — indeterminate, should NOT revoke.
    indeterminate = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "sha_b",
        "test_refs": test_refs,
        "ran": False,
        "passed": False,
        "could_not_run": True,
    }
    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(
            task, _json.dumps(indeterminate), db_session
        )

    await db_session.refresh(project)
    records = project.capability_proof_records or {}
    exec_entries = [
        e for e in (records.get(cap) or [])
        if e.get("task_id") == f"named_test_exec:{cap}"
    ]
    assert exec_entries, "indeterminate result must NOT revoke the existing proof"

    contradiction = (task.context or {}).get("capability_proof_trial_contradiction")
    assert contradiction is not None
    assert contradiction.get("proof_revoked") is False


@pytest.mark.asyncio
async def test_enroll_capability_proof_trial_dedup_in_flight(db_session):
    """enroll_capability_proof_trial returns None when a trial for the same
    capability is already QUEUED/PROVISIONING/IN_PROGRESS — 'revision B commissions
    exactly one re-verification'."""
    from app.capability_proof import enroll_capability_proof_trial

    project = await _make_trial_project(db_session, "dedup-cap")

    # Enroll the first trial.
    with _TrialFlag(True):
        first = await enroll_capability_proof_trial(
            db_session, project, "dedup-cap", ["backend/testing/test_a.py"], "sha1"
        )
    assert first is not None
    assert first.status == TaskStatus.QUEUED

    # A second enrollment attempt for the same capability must be a no-op.
    with _TrialFlag(True):
        second = await enroll_capability_proof_trial(
            db_session, project, "dedup-cap", ["backend/testing/test_a.py"], "sha2"
        )
    assert second is None, "duplicate trial must be blocked by the dedup guard"


@pytest.mark.asyncio
async def test_capability_proof_trial_flag_off_records_nothing(db_session):
    """With the dark flag OFF, even a GREEN trial records nothing."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "repo-map-darkoff"
    test_refs = ["backend/testing/test_prompt.py::test_x"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs, head_sha="abc")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    green = {
        "type": "capability_proof_trial", "cap": cap, "head_sha": "abc",
        "test_refs": test_refs, "ran": True, "passed": True, "could_not_run": False,
    }

    with _TrialFlag(False):
        await _handle_capability_proof_trial_completion(task, _json.dumps(green), db_session)

    await db_session.refresh(project)
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"
    assert cap not in (project.capability_proof_records or {})


@pytest.mark.asyncio
async def test_capability_proof_trial_skipped_suite_records_nothing(db_session):
    """A green-but-all-skipped (non-verifying) trial records no proof."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "repo-map-skipped"
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, ["backend/testing/test_prompt.py"], head_sha="abc")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    vacuous = {
        "type": "capability_proof_trial", "cap": cap, "head_sha": "abc",
        "test_refs": ["backend/testing/test_prompt.py"],
        "ran": True, "passed": True, "could_not_run": False,
        "skipped_summary": {"total": 4, "skipped": 4},
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(vacuous), db_session)

    await db_session.refresh(project)
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"
    assert cap not in (project.capability_proof_records or {})


@pytest.mark.asyncio
async def test_capability_proof_trial_wrong_cap_rejected(db_session):
    """A trial result whose cap does not match the enrollment contract records
    no proof — the runner cannot mint proof for the wrong capability."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "enrolled-capability"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    # Enroll with cap "enrolled-capability"
    task = _make_trial_task(project, cap, test_refs)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Agent reports a DIFFERENT cap than what was enrolled.
    wrong = {
        "type": "capability_proof_trial",
        "cap": "different-capability",
        "head_sha": "deadbeef",
        "test_refs": test_refs,
        "ran": True, "passed": True, "could_not_run": False,
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(wrong), db_session)

    await db_session.refresh(project)
    # No proof recorded for the wrong cap.
    status_map = working_capability_proof_status(project.problem_model)
    assert status_map.get("different-capability") != "proven"
    assert "different-capability" not in (project.capability_proof_records or {})
    # And no proof for the enrolled cap either.
    assert status_map.get(cap) != "proven"
    assert cap not in (project.capability_proof_records or {})


@pytest.mark.asyncio
async def test_capability_proof_trial_extra_test_refs_rejected(db_session):
    """A trial result with test_refs that were NOT in the enrollment must NOT
    mint proof — the runner cannot claim green for refs it was never enrolled to run."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "refs-guard-cap"
    enrolled_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    # Enroll with a specific set of test_refs
    task = _make_trial_task(project, cap, enrolled_refs)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Agent reports extra test_refs not in the enrollment.
    extra_refs_report = enrolled_refs + ["backend/testing/test_extra.py"]
    wrong = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "deadbeef",
        "test_refs": extra_refs_report,
        "ran": True, "passed": True, "could_not_run": False,
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(wrong), db_session)

    await db_session.refresh(project)
    assert cap not in (project.capability_proof_records or {})
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"
    await db_session.refresh(task)
    contradiction = (task.context or {})["capability_proof_trial_contradiction"]
    assert contradiction["reason"] == "test_refs_mismatch"


@pytest.mark.asyncio
async def test_capability_proof_trial_missing_head_sha_is_rejected(db_session):
    """A green receipt without the enrolled revision cannot mint a proof."""
    import json as _json

    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "missing-sha-guard-cap"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, test_refs, head_sha="abc123")
    db_session.add(task)
    await db_session.commit()

    receipt_without_sha = {
        "type": "capability_proof_trial", "cap": cap,
        "test_refs": test_refs, "ran": True, "passed": True, "could_not_run": False,
    }
    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(
            task, _json.dumps(receipt_without_sha), db_session
        )

    await db_session.refresh(project)
    await db_session.refresh(task)
    assert cap not in (project.capability_proof_records or {})
    contradiction = (task.context or {})["capability_proof_trial_contradiction"]
    assert contradiction["reason"] == "head_sha_mismatch"


@pytest.mark.asyncio
async def test_capability_proof_trial_incomplete_receipt_records_nothing(db_session):
    """A trial result with an empty test_refs or a proper subset of enrolled refs
    must NOT mint proof — the agent must report the complete executed suite."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "incomplete-refs-cap"
    enrolled_refs = [
        "backend/testing/test_prompt.py::test_map",
        "backend/testing/test_prompt.py::test_search",
    ]
    project = await _make_trial_project(db_session, cap)
    task = _make_trial_task(project, cap, enrolled_refs, head_sha="deadbeef")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    base_result = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "deadbeef",
        "ran": True,
        "passed": True,
        "could_not_run": False,
    }

    # Case 1: empty test_refs — bypasses validation entirely without this fix.
    empty_refs_result = dict(base_result, test_refs=[])
    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(
            task, _json.dumps(empty_refs_result), db_session
        )
    await db_session.refresh(project)
    assert cap not in (project.capability_proof_records or {}), (
        "empty test_refs must not mint proof"
    )
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"

    # Case 2: proper subset — only one of two enrolled refs reported.
    partial_refs_result = dict(base_result, test_refs=[enrolled_refs[0]])
    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(
            task, _json.dumps(partial_refs_result), db_session
        )
    await db_session.refresh(project)
    assert cap not in (project.capability_proof_records or {}), (
        "partial test_refs subset must not mint proof"
    )
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"


@pytest.mark.asyncio
async def test_capability_proof_trial_mismatched_head_sha_rejected(db_session):
    """A trial result whose head_sha differs from the enrollment must NOT mint
    proof — the runner must execute the enrolled revision."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "sha-guard-cap"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    # Enroll with head_sha "abc123"
    task = _make_trial_task(project, cap, test_refs, head_sha="abc123")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Agent reports a different sha.
    wrong = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "def456",
        "test_refs": test_refs,
        "ran": True, "passed": True, "could_not_run": False,
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(wrong), db_session)

    await db_session.refresh(project)
    assert cap not in (project.capability_proof_records or {})
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"


@pytest.mark.asyncio
async def test_capability_proof_trial_missing_sha_records_nothing(db_session):
    """A trial result that omits head_sha must NOT mint proof when the enrollment
    specified a head_sha — missing executed SHA is a receipt defect."""
    import json as _json

    from app.capability_registry import working_capability_proof_status
    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "incomplete-receipt-cap"
    test_refs = ["backend/testing/test_prompt.py"]
    project = await _make_trial_project(db_session, cap)
    # Enroll with a head_sha so the check is active.
    task = _make_trial_task(project, cap, test_refs, head_sha="abc123")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Agent reports a result WITHOUT head_sha (None / missing).
    incomplete = {
        "type": "capability_proof_trial",
        "cap": cap,
        # head_sha intentionally absent
        "test_refs": test_refs,
        "ran": True, "passed": True, "could_not_run": False,
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(incomplete), db_session)

    await db_session.refresh(project)
    assert cap not in (project.capability_proof_records or {})
    assert working_capability_proof_status(project.problem_model).get(cap) != "proven"


@pytest.mark.asyncio
async def test_capability_proof_trial_records_reported_sha_not_enrolled_sha(db_session):
    """When the enrollment has no head_sha but the agent reports one, the ledger
    records the agent-reported SHA (the actual executed revision), not an empty
    string or None."""
    import json as _json

    from app.task_completion import _handle_capability_proof_trial_completion

    cap = "sha-fallback-cap"
    test_refs = ["backend/testing/test_prompt.py::test_map"]
    project = await _make_trial_project(db_session, cap)
    # Enroll WITHOUT a head_sha (head_sha_enrolled = None).
    task = Task(
        project_id=project.id,
        title=f"Capability proof trial: {cap}",
        payloads=[{
            "type": "capability_proof_trial",
            "cap": cap,
            "test_refs": test_refs,
            # deliberately omit head_sha
        }],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    green = {
        "type": "capability_proof_trial",
        "cap": cap,
        "head_sha": "reported-sha-abc",
        "test_refs": test_refs,
        "ran": True, "passed": True, "could_not_run": False,
        "skipped_summary": {"total": 3, "skipped": 0},
    }

    with _TrialFlag(True):
        await _handle_capability_proof_trial_completion(task, _json.dumps(green), db_session)

    await db_session.refresh(project)
    entries = project.capability_proof_records.get(cap, [])
    assert len(entries) == 1
    # The recorded SHA is the agent-reported revision, not None or empty.
    assert entries[0]["commit_sha"] == "reported-sha-abc"


@pytest.mark.asyncio
async def test_enroll_capability_proof_trial_creates_bounded_task(db_session):
    """enroll_capability_proof_trial persists a bounded trial task when the flag
    is on, caps the test-file count, and is a no-op when the flag is off."""
    from app.capability_proof import (
        CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS,
        MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES,
        enroll_capability_proof_trial,
    )

    project = await _make_trial_project(db_session, "enroll-cap")

    # Flag OFF → no-op.
    with _TrialFlag(False):
        none_task = await enroll_capability_proof_trial(
            db_session, project, "enroll-cap", ["backend/testing/test_a.py"], "sha"
        )
    assert none_task is None

    # Flag ON → a QUEUED trial task with the bounded launch contract.
    many_refs = [f"backend/testing/test_{i}.py" for i in range(MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES + 5)]
    with _TrialFlag(True):
        task = await enroll_capability_proof_trial(
            db_session, project, "Enroll Cap", many_refs, "sha123"
        )
    assert task is not None
    assert task.status == TaskStatus.QUEUED
    payload = task.first_payload
    assert payload["type"] == "capability_proof_trial"
    # Capability name normalized to canonical kebab form.
    assert payload["cap"] == "enroll-cap"
    # Test-file count hard-capped.
    assert len(payload["test_refs"]) == MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES
    assert payload["head_sha"] == "sha123"
    assert payload["wall_clock_seconds"] == CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS


# ── Cross-job resume fail-closed (scripts/local_runner.py) ───────────────────


def test_large_checkpoint_round_trip_upload_bundle_resume(tmp_path, monkeypatch):
    """A >25 MiB tree uses the direct contract and rehydrates byte-for-byte.

    This exercises the dependency-free runner's production archive builder,
    chunked direct upload, completion handshake, authorized direct download,
    integrity check, safe extraction, and atomic workspace publication as one
    seam. A second download with mismatched authoritative CRC32C must leave the
    destination untouched.
    """
    import hashlib
    import io
    import json
    import os
    import random
    import shutil
    import sys
    from pathlib import Path

    script_candidates = (
        Path(__file__).parents[2] / "scripts",
        Path("/repo/scripts"),
    )
    scripts_dir = next(path for path in script_candidates if path.is_dir())
    sys.path.insert(0, str(scripts_dir))
    import local_runner

    source = tmp_path / "source"
    source.mkdir()
    checkpoint = source / "checkpoints" / "epoch-0042.bin"
    checkpoint.parent.mkdir()
    rng = random.Random(20260715)
    remaining = 27 * 1024 * 1024 + 137
    with checkpoint.open("wb") as output:
        while remaining:
            chunk = rng.randbytes(min(1024 * 1024, remaining))
            output.write(chunk)
            remaining -= len(chunk)
    (source / "notes.txt").write_text("resume-me\n", encoding="utf-8")

    stored_bundle = tmp_path / "durable-transfer.tar.gz"
    contract = {}

    def crc32c_for(path):
        state = 0xFFFFFFFF
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                state = local_runner._crc32c_update(state, chunk)
        return local_runner._crc32c_b64(state)

    def fake_post(url, _token, body):
        if url.endswith("/artifact-transfer/complete"):
            assert body == {"transfer_id": "transfer-large", "generation": "73"}
            assert stored_bundle.stat().st_size == contract["archive_size"]
            assert crc32c_for(stored_bundle) == contract["crc32c"]
            return 200, {
                "ok": True,
                "runner_artifact": {
                    "stored": True,
                    "verified": True,
                    "size": contract["archive_size"],
                    "crc32c": contract["crc32c"],
                    "generation": "73",
                },
            }
        assert url.endswith("/artifact-transfer")
        contract.update(body)
        return 200, {
            "ok": True,
            "transfer_id": "transfer-large",
            "upload_url": "https://storage.invalid/signed-put",
            "method": "PUT",
            "headers": {"Content-Type": "application/gzip"},
        }

    def fake_stream(url, method, headers, file_path, file_size):
        assert url == "https://storage.invalid/signed-put"
        assert method == "PUT"
        assert headers["Content-Type"] == "application/gzip"
        assert os.path.getsize(file_path) == file_size
        shutil.copyfile(file_path, stored_bundle)
        return 200, {"x-goog-generation": "73"}, b""

    monkeypatch.setattr(local_runner, "_post", fake_post)
    monkeypatch.setattr(local_runner, "_stream_http_file", fake_stream)
    delivery = local_runner._upload_artifact(
        "https://api.invalid", "runner-token", "job-large", str(source)
    )
    assert contract["total_bytes"] > 25 * 1024 * 1024
    assert contract["archive_size"] > 25 * 1024 * 1024
    assert delivery["status"] == "delivered"
    assert delivery["verified"] is True
    assert delivery["crc32c"] == crc32c_for(stored_bundle)

    class Response:
        def __init__(self, status, stream, content_type):
            self.status = status
            self._stream = stream
            self.headers = {"Content-Type": content_type}

        def read(self, size=-1):
            return self._stream.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._stream.close()
            return False

    expected_crc = contract["crc32c"]

    def fake_urlopen(request, timeout=None):
        assert timeout == 120
        if request.full_url.endswith("input-artifact?direct=true"):
            metadata = json.dumps(
                {
                    "download_url": "https://storage.invalid/signed-get",
                    "size": stored_bundle.stat().st_size,
                    "crc32c": expected_crc,
                    "generation": "73",
                }
            ).encode()
            return Response(200, io.BytesIO(metadata), "application/json")
        assert request.full_url == "https://storage.invalid/signed-get"
        return Response(200, stored_bundle.open("rb"), "application/gzip")

    monkeypatch.setattr(local_runner.urllib.request, "urlopen", fake_urlopen)
    resumed = tmp_path / "resumed"
    resumed.mkdir()
    provenance = local_runner._download_input_artifact(
        "https://api.invalid", "runner-token", "job-successor", str(resumed)
    )
    assert provenance == {
        "ok": True,
        "file_count": 2,
        "bytes": contract["total_bytes"],
    }

    def sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    assert sha256(checkpoint) == sha256(resumed / "checkpoints" / checkpoint.name)
    assert (resumed / "notes.txt").read_text(encoding="utf-8") == "resume-me\n"

    rejected = tmp_path / "rejected"
    rejected.mkdir()
    sentinel = rejected / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    expected_crc = "AAAAAA=="
    mismatch = local_runner._download_input_artifact(
        "https://api.invalid", "runner-token", "job-successor", str(rejected)
    )
    assert mismatch == {"ok": False, "reason": "integrity_mismatch:crc32c"}
    assert sentinel.read_text(encoding="utf-8") == "untouched"


class TestRunnerResumeFail_Closed:
    """_run_compute_job fails closed when the input-artifact bundle is unavailable.

    The self-hosted runner previously silently restarted from scratch when
    _download_input_artifact returned ok=False — discarding weeks of checkpoint
    progress. The fix: treat a hydration failure as a terminal failure so the
    operator sees an honest signal rather than a silent from-scratch burn.
    """

    @staticmethod
    def _bwrap_caps():
        import sys
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "scripts"))
        import runner_isolation as iso
        return iso.Capabilities(bwrap_path="/usr/bin/bwrap", userns_ok=True)

    @staticmethod
    def _make_job(**kwargs):
        base = {
            "job_id": "test-resume-001",
            "command": "echo should-not-run",
            "env": {},
            "cwd": None,
            "setup_step": None,
            "container_image": None,
            "container_registry_auth": {},
            "runtime_deadline_seconds": None,
            "cancel_requested": False,
        }
        base.update(kwargs)
        return base

    def test_runner_resume_fails_closed_when_bundle_unavailable(self):
        """A missing predecessor bundle aborts the job — command never starts."""
        import sys
        import pathlib
        import threading
        sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "scripts"))
        import local_runner
        from unittest.mock import patch

        reports = []
        uploads = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None,
                        resume_hydration=None, progress=None, **_kw):
            reports.append({
                "exec_status": exec_status,
                "exit_code": exit_code,
                "command_log": command_log,
                "resume_hydration": resume_hydration,
            })
            return False

        def fake_download(orch_url, token, job_id, artifact_dir):
            # Model a buggy extractor that left a predecessor fragment. The
            # terminal finally path must still scrub and suppress its upload.
            with open(os.path.join(artifact_dir, "partial-checkpoint.pt"), "wb") as fh:
                fh.write(b"partial predecessor")
            return {"ok": False, "reason": "http_404"}

        job = self._make_job(input_artifact_job_id="pred-job-001")
        active: set = {"test-resume-001"}

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner, "_download_input_artifact", fake_download), \
             patch.object(local_runner, "_upload_artifact", lambda *_args: uploads.append(True)):
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok",
                active, threading.Lock(),
                capabilities=self._bwrap_caps(),
            )

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        assert terminal, "expected a terminal exec_status report"
        assert terminal[-1]["exec_status"] == "failed", (
            f"expected 'failed', got '{terminal[-1]['exec_status']}'"
        )
        # The failure reason must be visible in the command_log so the operator
        # can see why the job stopped without digging into runner logs.
        log_text = (terminal[-1].get("command_log") or {}).get("text", "")
        assert "input_artifact_rehydration_failed" in log_text, (
            f"expected rehydration failure marker in command_log, got: {log_text!r}"
        )
        # The provenance dict must carry the failure reason through to the report.
        hydration = terminal[-1].get("resume_hydration") or {}
        assert hydration.get("reason") == "http_404", (
            f"expected reason='http_404' in resume_hydration, got: {hydration!r}"
        )
        assert uploads == [], "failed hydration must not upload predecessor fragments"
        # Job must be removed from active_jobs so capacity is released.
        assert "test-resume-001" not in active

    def test_runner_resume_fails_closed_on_extract_error(self):
        """An extraction error also aborts the job — command never starts."""
        import sys
        import pathlib
        import threading
        sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "scripts"))
        import local_runner
        from unittest.mock import patch

        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None,
                        resume_hydration=None, progress=None, **_kw):
            reports.append({"exec_status": exec_status})
            return False

        def fake_download(orch_url, token, job_id, artifact_dir):
            return {"ok": False, "reason": "extract_error:TarError"}

        job = self._make_job(input_artifact_job_id="pred-job-002")
        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner, "_download_input_artifact", fake_download):
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok",
                set(), threading.Lock(),
                capabilities=self._bwrap_caps(),
            )

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        assert terminal, "expected a terminal exec_status report"
        assert terminal[-1]["exec_status"] == "failed"

    def test_runner_resume_proceeds_when_bundle_available(self):
        """Successful proof is repeated on a later durable terminal report."""
        import sys
        import pathlib
        import threading
        sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "scripts"))
        import local_runner
        from unittest.mock import patch

        reports: list = []

        def fake_report(
            orch_url, token, job_id, *, exec_status, resume_hydration=None, **_kw
        ):
            reports.append(
                {"exec_status": exec_status, "resume_hydration": resume_hydration}
            )
            return False

        def fake_download(orch_url, token, job_id, artifact_dir):
            return {"ok": True, "file_count": 3, "bytes": 1024}

        job = self._make_job(
            input_artifact_job_id="pred-job-003",
            repo_clone={"owner": "o", "repo": "r", "ref": "main"},
            repo_token="token",
        )
        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner, "_download_input_artifact", fake_download), \
             patch.object(local_runner, "_clone_repo", return_value=(False, 1)):
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok",
                set(), threading.Lock(),
                capabilities=self._bwrap_caps(),
            )

        assert reports[0]["exec_status"] == "running"
        terminal = [r for r in reports if r["exec_status"] == "failed"][-1]
        assert terminal["resume_hydration"] == {
            "ok": True,
            "file_count": 3,
            "bytes": 1024,
            "resumed_from_job_id": "pred-job-003",
        }


# ---------------------------------------------------------------------------
# local_runner._scan_viewable_files — scan cap
# ---------------------------------------------------------------------------


class TestScanViewableFilesCap:
    """_scan_viewable_files respects _VIEWABLE_SCAN_FILE_CAP."""

    @staticmethod
    def _import_local_runner():
        import sys
        import pathlib
        scripts_dir = pathlib.Path(__file__).parents[2] / "scripts"
        sys.path.insert(0, str(scripts_dir))
        import local_runner
        return local_runner

    def test_scan_returns_all_when_under_cap(self, tmp_path):
        """Fewer files than _VIEWABLE_SCAN_FILE_CAP → all viewable files returned."""
        local_runner = self._import_local_runner()
        art = tmp_path / "artifacts"
        art.mkdir()
        for i in range(5):
            (art / f"plot{i}.png").write_bytes(b"PNG")
        for i in range(3):
            (art / f"notes{i}.txt").write_bytes(b"txt")

        candidates = local_runner._scan_viewable_files(str(art))
        assert len(candidates) == 5  # only viewable files in the result
        relpaths = {c["relpath"] for c in candidates}
        assert all(r.endswith(".png") for r in relpaths)

    def test_scan_stops_at_cap(self, tmp_path):
        """When total entries exceed _VIEWABLE_SCAN_FILE_CAP, scan stops early.

        The cap is on total entries (viewable + non-viewable), so a directory
        flooded with non-viewable files triggers an early return. The returned
        list must be <= the cap size (it may be smaller since most files are
        non-viewable). Critically, the function must NOT walk all files.
        """
        local_runner = self._import_local_runner()
        cap = local_runner._VIEWABLE_SCAN_FILE_CAP
        art = tmp_path / "artifacts"
        art.mkdir()

        # Write cap+1 non-viewable files + 1 viewable file.
        for i in range(cap + 1):
            (art / f"checkpoint_{i:05d}.bin").write_bytes(b"X")
        (art / "reward.png").write_bytes(b"PNG")

        walks: list = []
        real_walk = os.walk

        def counting_walk(*args, **kw):
            for entry in real_walk(*args, **kw):
                walks.append(entry[2])  # filenames list per dir
                yield entry

        with patch("os.walk", side_effect=counting_walk):
            candidates = local_runner._scan_viewable_files(str(art))

        total_scanned = sum(len(fnames) for fnames in walks)
        assert total_scanned <= cap + 5, (
            f"scan walked {total_scanned} entries, expected to stop near cap={cap}"
        )
        # The function must return without error even when capped.
        assert isinstance(candidates, list)

    def test_scan_returns_sorted_candidates(self, tmp_path):
        """Candidates are sorted by relpath even when the scan is capped."""
        local_runner = self._import_local_runner()
        art = tmp_path / "artifacts"
        art.mkdir()
        for name in ("zzz.png", "aaa.png", "mmm.webp"):
            (art / name).write_bytes(b"X")

        candidates = local_runner._scan_viewable_files(str(art))
        relpaths = [c["relpath"] for c in candidates]
        assert relpaths == sorted(relpaths)


# ---------------------------------------------------------------------------
# local_runner._stream_viewable_files — oversize/count accounting reset
# ---------------------------------------------------------------------------


class TestStreamViewableAccountingReset:
    """oversize_relpaths and count_cap_relpaths are reset on each scan cycle."""

    @staticmethod
    def _import_local_runner():
        import sys
        import pathlib
        scripts_dir = pathlib.Path(__file__).parents[2] / "scripts"
        sys.path.insert(0, str(scripts_dir))
        import local_runner
        return local_runner

    def test_oversize_relpaths_reset_per_scan(self, tmp_path):
        """A file that was oversize in a prior scan is not counted if now gone.

        _stream_viewable_files resets oversize_relpaths at the start of each
        call so the omission count reflects the CURRENT scan, not cumulative
        history.  Verifies the reset by calling _stream_viewable_files twice:
        the first call adds a file to oversize_relpaths; the second call with an
        empty dir must report 0 oversize files.
        """
        local_runner = self._import_local_runner()
        art = tmp_path / "artifacts"
        art.mkdir()

        state = local_runner._ViewableStreamState()
        image_cap = local_runner._MAX_VIEWABLE_IMAGE_BYTES

        # First scan: one oversized image.
        oversized = art / "big.png"
        oversized.write_bytes(b"X" * (image_cap + 1))

        local_runner._stream_viewable_files(
            "https://orch.example.com", "tok", "job-acct", str(art), state
        )

        assert len(state.oversize_relpaths) == 1, (
            "first scan should record the oversize file"
        )

        # Second scan: the file is gone (removed between heartbeats).
        oversized.unlink()

        local_runner._stream_viewable_files(
            "https://orch.example.com", "tok", "job-acct", str(art), state
        )

        assert len(state.oversize_relpaths) == 0, (
            "second scan with no oversize files should reset the counter to 0, "
            f"got {len(state.oversize_relpaths)}"
        )

    def test_count_cap_relpaths_reset_per_scan(self, tmp_path):
        """count_cap_relpaths is cleared at the start of each scan call."""
        local_runner = self._import_local_runner()
        art = tmp_path / "artifacts"
        art.mkdir()

        state = local_runner._ViewableStreamState()
        # Simulate already-sent 25 files so any new file gets count-capped.
        for i in range(local_runner._MAX_VIEWABLE_FILE_COUNT):
            state.sent[f"file{i}.png"] = (0, 0, 0)

        # Write one new viewable file that will be count-capped.
        (art / "extra.png").write_bytes(b"X")

        local_runner._stream_viewable_files(
            "https://orch.example.com", "tok", "job-cnt", str(art), state
        )

        assert len(state.count_cap_relpaths) == 1, (
            "count-capped file should appear in count_cap_relpaths"
        )

        # Second scan: the extra file is removed.
        (art / "extra.png").unlink()

        local_runner._stream_viewable_files(
            "https://orch.example.com", "tok", "job-cnt", str(art), state
        )

        assert len(state.count_cap_relpaths) == 0, (
            "second scan with no new files should reset count_cap_relpaths to 0, "
            f"got {len(state.count_cap_relpaths)}"
        )
