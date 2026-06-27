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
                "has_gpu": True,
                "gpu_count": 1,
                "gpu_names": ["Tesla T4"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await db_session.refresh(runner)

        # Hardware fields are updated to runner-reported ground truth.
        assert runner.capabilities["has_gpu"] is True
        assert runner.capabilities["gpu_count"] == 1
        assert runner.capabilities["gpu_names"] == ["Tesla T4"]

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
