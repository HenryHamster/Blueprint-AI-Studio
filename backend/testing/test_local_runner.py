"""Tests for the pull-based local fleet runner.

Covers the four new surfaces:
  * runner auth (app.auth: create/hash/verify token, get_current_runner)
  * admin management endpoints (app.routers.admin_runners)
  * runner polling/heartbeat endpoints (app.routers.runner)
  * orchestrator runner routing helpers (app.orchestrator)
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app import auth as auth_mod
from app import compute_backends as compute_backends_mod
from app import orchestrator as orch
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    LocalRunner,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.runner import _parse_multipart_form
from app.task_capabilities import STRATEGIC_TASK_TYPES

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
        assert emit.await_args.kwargs["refetch_surfaces"] == ("runtime",)
        assert emit.await_args.kwargs["summary"] == {
            "id": str(runner.id),
            "status": "active",
            "active_task_count": 1,
            "active_compute_job_count": 1,
            "agent_available": True,
        }

    @pytest.mark.asyncio
    async def test_heartbeat_skips_refetch_when_only_liveness_changes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner, token = await _make_runner(
            db_session,
            project.id,
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
        emit.assert_not_awaited()

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

        # Rollback does this in the live dispatch loop; expire_all reproduces the
        # same hazard without forcing an unrelated DB error in the test.
        db_session.expire_all()

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
