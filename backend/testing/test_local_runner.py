"""Tests for the pull-based local fleet runner.

Covers the four new surfaces:
  * runner auth (app.auth: create/hash/verify token, get_current_runner)
  * admin management endpoints (app.routers.admin_runners)
  * runner polling/heartbeat endpoints (app.routers.runner)
  * orchestrator runner routing helpers (app.orchestrator)
"""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app import auth as auth_mod
from app import orchestrator as orch
from app.models import LocalRunner, Project, ProjectMember, Task, TaskStatus, _utcnow

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
    async def test_verify_runner_token_active_only(self, db_session):
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        found = await auth_mod.verify_runner_token(token, db_session)
        assert found is not None and found.id == runner.id

        # Wrong token resolves to None.
        assert await auth_mod.verify_runner_token("nope", db_session) is None

        # Deleted/inactive runners never authenticate.
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
    async def test_try_runner_dispatch_skips_compute_experiment(self, db_session):
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
            is False
        )
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

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
