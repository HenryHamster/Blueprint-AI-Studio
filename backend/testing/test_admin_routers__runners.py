# AUTO-SPLIT from test_admin_routers.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared helper (_make_project) is duplicated here to keep this
# file self-contained. This file holds the runner lifecycle and compute-image build domain
# slices: PATCH /runners/{id}/status, GET /runners/{id}/stats, POST/GET compute-image/*.
"""Domain slice: admin_runners.py + admin_project_tools.py — Runners & Compute Image

Split from test_admin_routers.py. Covers runner status transitions (pause/drain/resume),
runner cost-avoided and claim-latency stats, compute-image build trigger, and
compute-image status/drift endpoint."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    ComputeJob,
    LocalRunner,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


# ── Helpers ────────────────────────────────────────────────────────────────────


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
# Runner lifecycle: pause / drain / resume (PATCH /runners/{id}/status)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunnerStatusPatch:
    """Tests for PATCH /api/admin/projects/{project_id}/runners/{runner_id}/status."""

    @pytest.fixture(autouse=True)
    def _mock_workbench(self):
        with patch("app.routers.admin_runners.emit_workbench_refetch", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    async def test_active_to_paused(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="a" * 64, hostname="host1", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "paused"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "paused"
        await db_session.refresh(runner)
        assert runner.status == "paused"

    @pytest.mark.asyncio
    async def test_active_to_draining(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="b" * 64, hostname="host2", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "draining"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "draining"
        await db_session.refresh(runner)
        assert runner.status == "draining"

    @pytest.mark.asyncio
    async def test_paused_to_active(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="c" * 64, hostname="host3", status="paused"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "active"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"
        await db_session.refresh(runner)
        assert runner.status == "active"

    @pytest.mark.asyncio
    async def test_draining_to_active(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="d" * 64, hostname="host4", status="draining"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "active"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_draining_to_paused(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="e" * 64, hostname="host5", status="draining"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "paused"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "paused"

    @pytest.mark.asyncio
    async def test_invalid_transition_paused_to_draining_rejected(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="f" * 64, hostname="host6", status="paused"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "draining"},
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_deleted_runner_returns_404(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="9" * 64, hostname="gone", status="deleted"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "active"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_pause_releases_unpulled_pins(self, client: AsyncClient, db_session):
        """Pausing a runner must release tasks pinned to it that haven't been pulled yet."""
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="7" * 64, hostname="pinned", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        # Create a pinned task (PROVISIONING, started_at=None, vm_instance_id=runner:{id})
        pinned_task = Task(
            task_type="fix",
            project_id=project.id,
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner.id}",
            context={"runner_target_id": str(runner.id)},
        )
        db_session.add(pinned_task)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "paused"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(pinned_task)
        # Pin must be released: task goes back to QUEUED with no runner binding
        assert pinned_task.status == TaskStatus.QUEUED
        assert pinned_task.vm_instance_id is None
        assert "runner_target_id" not in (pinned_task.context or {})

    @pytest.mark.asyncio
    async def test_idempotent_same_status(self, client: AsyncClient, db_session):
        """Patching to the same status is a no-op (200 returned, no error)."""
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id, token_hash="8" * 64, hostname="steady", status="paused"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/status",
            json={"status": "paused"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "paused"


class TestRunnerStatsCloudCostAvoided:
    """Tests for the cloud-cost-avoided rollup on
    GET /api/admin/projects/{project_id}/runners/{runner_id}/stats.

    Cloud cost avoided = the on-demand cloud VM cost that terminal compute jobs
    run on this self-hosted runner would otherwise have incurred, priced from
    each job's honest runtime window and (machine_type, accelerator) shape.
    """

    def _pin(self, runner_id):
        return {"runner_target_id": str(runner_id)}

    @pytest.mark.asyncio
    async def test_cost_avoided_sums_priced_runtime_across_shapes(
        self, client: AsyncClient, db_session
    ):
        from app.models import ComputeJobStatus
        from app.vm_cost_rates import get_compute_rate_per_second

        project = await _make_project(db_session, name="CostAvoided Sum")
        runner = LocalRunner(
            project_id=project.id, token_hash="1" * 64, hostname="cpu-gpu", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        now = datetime.now(timezone.utc)
        # CPU job: 100s on the default machine type.
        cpu_job = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=now - timedelta(seconds=100),
            completed_at=now,
            polling_metadata=self._pin(runner.id),
        )
        # GPU job: 200s on an L4 accelerator (bundled G2 shape).
        gpu_job = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.SUCCEEDED,
            accelerator_type="L4",
            accelerator_count=1,
            started_at=now - timedelta(seconds=200),
            completed_at=now,
            polling_metadata=self._pin(runner.id),
        )
        db_session.add_all([cpu_job, gpu_job])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        expected = round(
            100 * get_compute_rate_per_second(machine_type=None, accelerator=None)
            + 200 * get_compute_rate_per_second(accelerator="L4", count=1),
            2,
        )
        assert body["cloud_cost_avoided_usd"] == pytest.approx(expected)
        # Both jobs contribute an honest runtime window.
        assert body["cloud_cost_avoided_basis"] == 2

    @pytest.mark.asyncio
    async def test_cost_avoided_zero_without_terminal_jobs(
        self, client: AsyncClient, db_session
    ):
        from app.models import ComputeJobStatus

        project = await _make_project(db_session, name="CostAvoided Empty")
        runner = LocalRunner(
            project_id=project.id, token_hash="2" * 64, hostname="idle", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        now = datetime.now(timezone.utc)
        # A RUNNING (non-terminal) pinned job must not contribute cost.
        running = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.RUNNING,
            started_at=now - timedelta(seconds=300),
            polling_metadata=self._pin(runner.id),
        )
        db_session.add(running)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cloud_cost_avoided_usd"] == 0.0
        assert body["cloud_cost_avoided_basis"] == 0

    @pytest.mark.asyncio
    async def test_cost_avoided_excludes_other_runner_and_missing_window(
        self, client: AsyncClient, db_session
    ):
        from app.models import ComputeJobStatus
        from app.vm_cost_rates import get_compute_rate_per_second

        project = await _make_project(db_session, name="CostAvoided Isolation")
        runner = LocalRunner(
            project_id=project.id, token_hash="3" * 64, hostname="mine", status="active"
        )
        other = LocalRunner(
            project_id=project.id, token_hash="4" * 64, hostname="theirs", status="active"
        )
        db_session.add_all([runner, other])
        await db_session.commit()

        now = datetime.now(timezone.utc)
        mine = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=now - timedelta(seconds=50),
            completed_at=now,
            polling_metadata=self._pin(runner.id),
        )
        # Pinned to a different runner — excluded.
        theirs = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.SUCCEEDED,
            started_at=now - timedelta(seconds=999),
            completed_at=now,
            polling_metadata=self._pin(other.id),
        )
        # Terminal but no started_at → no honest window, excluded from basis.
        no_window = ComputeJob(
            project_id=project.id,
            backend="test",
            status=ComputeJobStatus.FAILED,
            completed_at=now,
            polling_metadata=self._pin(runner.id),
        )
        db_session.add_all([mine, theirs, no_window])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        expected = round(
            50 * get_compute_rate_per_second(machine_type=None, accelerator=None), 2
        )
        assert body["cloud_cost_avoided_usd"] == pytest.approx(expected)
        assert body["cloud_cost_avoided_basis"] == 1


class TestRunnerStatsClaimLatency:
    """Tests for the per-runner claim-latency rollup on
    GET /api/admin/projects/{project_id}/runners/{runner_id}/stats.

    Claim latency = vm_cold_start_ms across terminal tasks dispatched to the
    runner via vm_instance_id = "runner:{runner_id}". The endpoint returns
    p50/p95/p99/max and a sample count.
    """

    @pytest.mark.asyncio
    async def test_claim_latency_returns_percentiles(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="ClaimLatency Pct")
        runner = LocalRunner(
            project_id=project.id, token_hash="5" * 64, hostname="latency-test", status="active"
        )
        other = LocalRunner(
            project_id=project.id, token_hash="6" * 64, hostname="other", status="active"
        )
        db_session.add_all([runner, other])
        await db_session.commit()

        runner_prefix = f"runner:{runner.id}"
        other_prefix = f"runner:{other.id}"

        # Tasks for this runner with known cold-start latencies: [100, 200, 500, 1000, 2000]
        # p50 = 500, p95 = 2000, p99 = 2000, max = 2000, count = 5
        tasks = [
            Task(
                project_id=project.id,
                title=f"latency-{v}",
                status=TaskStatus.COMPLETED,
                vm_instance_id=runner_prefix,
                vm_cold_start_ms=v,
                payloads=[{"type": "test", "data": {}}],
            )
            for v in (100, 200, 500, 1000, 2000)
        ]
        # Task for a different runner — excluded from this runner's stats.
        tasks.append(
            Task(
                project_id=project.id,
                title="other-runner",
                status=TaskStatus.COMPLETED,
                vm_instance_id=other_prefix,
                vm_cold_start_ms=99999,
                payloads=[{"type": "test", "data": {}}],
            )
        )
        # Terminal task without vm_cold_start_ms — excluded from sample.
        tasks.append(
            Task(
                project_id=project.id,
                title="no-cold-start",
                status=TaskStatus.FAILED,
                vm_instance_id=runner_prefix,
                vm_cold_start_ms=None,
                payloads=[{"type": "test", "data": {}}],
            )
        )
        # Non-terminal task (IN_PROGRESS) with vm_cold_start_ms — excluded from
        # aggregation since TASK_OUTCOME_TERMINAL_STATUSES is the filter.
        tasks.append(
            Task(
                project_id=project.id,
                title="in-progress",
                status=TaskStatus.IN_PROGRESS,
                vm_instance_id=runner_prefix,
                vm_cold_start_ms=500,
                payloads=[{"type": "test", "data": {}}],
            )
        )
        db_session.add_all(tasks)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["claim_latency_sample_count"] == 5
        assert body["claim_latency_ms_p50"] == 500  # median of [100, 200, 500, 1000, 2000]
        assert body["claim_latency_ms_p95"] == 2000  # int(5 * 0.95) = 4 → index 4
        assert body["claim_latency_ms_p99"] == 2000  # int(5 * 0.99) = 4 → index 4
        assert body["claim_latency_ms_max"] == 2000

    @pytest.mark.asyncio
    async def test_claim_latency_no_tasks_returns_nulls(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session, name="ClaimLatency Empty")
        runner = LocalRunner(
            project_id=project.id, token_hash="7" * 64, hostname="empty", status="active"
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners/{runner.id}/stats"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["claim_latency_sample_count"] == 0
        assert body["claim_latency_ms_p50"] is None
        assert body["claim_latency_ms_p95"] is None
        assert body["claim_latency_ms_p99"] is None
        assert body["claim_latency_ms_max"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# admin_project_tools.py — compute-image build trigger
#
# The build runs `docker build` + `docker push` of a REAL project image (a CUDA
# base plus a torch wheel is multi-GB, and the build can run the better part of
# an hour) as a subprocess ON THE ORCHESTRATOR HOST. It used to be reachable by
# any project MEMBER, with no concurrency limit, straight inside one HTTP
# request — an unbounded docker fan-out on the API process itself.
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeImageBuildTrigger:
    """Blast-radius bounds on the member-triggerable docker build."""

    @staticmethod
    def _patch_pipeline(monkeypatch, *, build_fn=None):
        """Stub manifest discovery + the build pipeline (no docker, no network)."""
        from app.routers import admin_project_tools as apt

        async def fake_manifest(_project):
            return {
                "tools": [],
                "skills": [],
                "compute": {
                    "image": {
                        "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                        "build": {"dockerfile": ".blueprint/Dockerfile.compute"},
                    }
                },
                "error": None,
            }

        monkeypatch.setattr(apt, "_discover_manifest_tools", fake_manifest)
        monkeypatch.setattr(
            apt._cb, "resolve_compute_image_registry_repo",
            lambda: "us-docker.pkg.dev/p/compute-images",
        )

        async def default_build(*a, **k):
            from app.compute_image_build import BuildResult

            return BuildResult(
                pinned_ref="us-docker.pkg.dev/p/compute-images/proj@sha256:" + "a" * 64,
                tag="deadbeef",
                dep_hash="deadbeef",
                already_present=False,
                built=True,
            )

        monkeypatch.setattr(
            apt._cb, "build_project_compute_image", build_fn or default_build
        )
        return apt

    @pytest.mark.asyncio
    async def test_build_succeeds_and_records_the_row(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Happy path still works end-to-end (owner, no build in flight)."""
        self._patch_pipeline(monkeypatch)
        project = await _make_project(db_session, name="ImgBuild OK")

        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["digest"] == "sha256:" + "a" * 64
        assert body["built"] is True

    @pytest.mark.asyncio
    async def test_concurrent_build_is_rejected_with_429(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A second build while one is in flight sheds load — it does not queue.

        Queuing would park an API worker (and its DB connection) behind an
        hour-long docker build; fanning out would run N multi-GB docker builds on
        the orchestrator host at once. Neither is acceptable, so the request is
        refused with a Retry-After.
        """
        apt = self._patch_pipeline(monkeypatch)
        project = await _make_project(db_session, name="ImgBuild Busy")

        # Simulate a build already occupying the only slot.
        await apt._compute_image_build_semaphore.acquire()
        try:
            resp = await client.post(
                f"/api/admin/projects/{project.id}/compute-image/build"
            )
        finally:
            apt._compute_image_build_semaphore.release()

        assert resp.status_code == 429, resp.text
        assert resp.headers.get("Retry-After")
        # The app wraps HTTPException detail; assert on the rendered body.
        assert "already running" in resp.text

    @pytest.mark.asyncio
    async def test_build_slot_is_released_after_a_failed_build(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A build that raises must not leak its concurrency slot."""

        async def boom(*a, **k):
            raise RuntimeError("docker build failed (exit 1)")

        apt = self._patch_pipeline(monkeypatch, build_fn=boom)
        project = await _make_project(db_session, name="ImgBuild Boom")

        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
        assert resp.status_code == 422, resp.text

        # Slot is free again — a later build is not permanently 429'd.
        assert not apt._compute_image_build_semaphore.locked()

    @pytest.mark.asyncio
    async def test_member_cannot_trigger_a_build(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Building is OWNER-grade: it runs docker on the host and pushes to the
        shared registry, like the other state-changing tool endpoints."""
        self._patch_pipeline(monkeypatch)
        project = await _make_project(db_session, name="ImgBuild Member", role="member")

        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")

        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_deadline_aborts_the_build_instead_of_abandoning_it(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A 504 must STOP the build — not walk away from it.

        ``asyncio.wait_for`` cancels the coroutine that is *awaiting* the build; the
        blocking ``docker build`` in the ``asyncio.to_thread`` worker keeps running.
        The old code then released the concurrency slot in the request's ``finally``
        and returned 504, so:

          * the abandoned build kept burning the orchestrator host,
          * the slot was handed to the next caller while it ran (the bound stopped
            bounding — repeated 504s fan out N concurrent multi-GB builds), and
          * the pipeline's temp dir was torn down under the still-running build,
            which then re-created parts of it — a disk leak.

        The deadline now publishes a kill switch, kills the docker children, waits
        for the pipeline to unwind, and only then releases the slot.
        """
        import tempfile
        import threading
        import time as _t

        from app import compute_image_build as _cib

        state = {
            "control": None,
            "tmp": None,
            "thread_exited": threading.Event(),
            "ran_to_completion": threading.Event(),
        }

        async def slow_build(*a, **k):
            # Mirrors compute_backends.build_project_compute_image: a temp-dir
            # context around a BLOCKING docker call dispatched to a worker thread.
            state["control"] = _cib.current_build_control()
            with tempfile.TemporaryDirectory(prefix="compute-image-build-") as tmp:
                state["tmp"] = tmp

                def blocking_docker_build():
                    try:
                        deadline = _t.monotonic() + 20
                        while _t.monotonic() < deadline:
                            control = _cib.current_build_control()
                            if control is not None and control.aborted:
                                raise _cib.BuildAborted("docker child killed")
                            _t.sleep(0.02)
                        state["ran_to_completion"].set()
                    finally:
                        state["thread_exited"].set()

                await asyncio.to_thread(blocking_docker_build)
            raise AssertionError("unreachable: the build should have been aborted")

        apt = self._patch_pipeline(monkeypatch, build_fn=slow_build)
        monkeypatch.setattr(apt, "_COMPUTE_IMAGE_BUILD_TIMEOUT_SECONDS", 0.5)
        project = await _make_project(db_session, name="ImgBuild Deadline")

        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")

        assert resp.status_code == 504, resp.text

        # 1. The build REALLY STOPPED. This is the finding: `wait_for` only cancels
        #    the awaiting coroutine, so the worker thread keeps running long after
        #    the 504 — burning the host with the slot already handed away.
        assert state["thread_exited"].wait(5.0), (
            "the build was still running 5s after the 504 — the deadline abandoned "
            "it instead of aborting it"
        )
        assert not state["ran_to_completion"].is_set(), (
            "the build ran to completion despite the deadline — it was abandoned, "
            "not aborted"
        )

        # 2. It stopped because it was given a kill switch, and the switch was pulled.
        assert state["control"] is not None, (
            "no BuildControl reached the build — the deadline has no way to stop the "
            "docker subprocesses and can only abandon them"
        )
        assert state["control"].aborted is True

        # 3. Its temp dir is gone (no disk leak from a half-torn-down clone).
        assert not os.path.exists(state["tmp"])

        # 4. The slot is released only after that unwind — and it IS released, so
        #    the bound is not permanently exhausted by repeated 504s.
        assert not apt._compute_image_build_semaphore.locked()

    @pytest.mark.asyncio
    async def test_retrigger_with_an_empty_local_docker_store_preserves_the_digest_pin(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Re-triggering with unchanged deps must NEVER destroy the stored digest pin.

        Unchanged deps take ``build_and_push``'s already-present fast path, which
        resolved the digest from the LOCAL docker store — empty on a fresh or pruned
        host. It returned a tag-only ``pinned_ref``, and the UPSERT wrote
        ``digest=NULL`` over a perfectly good pin (and flipped ``built`` back to
        False), silently unpinning the project's compute image.
        """
        from app.compute_image_build import BuildResult
        from app.models import ProjectComputeImage

        project = await _make_project(db_session, name="ImgBuild Pin")
        project_id = project.id

        good_digest = "sha256:" + "a" * 64
        registry_repo = "us-docker.pkg.dev/p/compute-images"
        pinned = f"{registry_repo}/proj@{good_digest}"
        db_session.add(
            ProjectComputeImage(
                project_id=project_id,
                image_name="proj",
                registry_repo=registry_repo,
                dep_hash="deadbeef",
                tag="deadbeef",
                pinned_ref=pinned,
                digest=good_digest,
                built=True,
            )
        )
        await db_session.commit()

        async def already_present_on_a_fresh_host(*a, **k):
            # Exactly what the fast path yields when neither the local store nor the
            # registry hands back a digest: already_present, no digest, tag-only ref.
            return BuildResult(
                pinned_ref=f"{registry_repo}/proj:deadbeef",
                tag="deadbeef",
                dep_hash="deadbeef",
                already_present=True,
                built=False,
                digest=None,
            )

        self._patch_pipeline(monkeypatch, build_fn=already_present_on_a_fresh_host)
        resp = await client.post(f"/api/admin/projects/{project_id}/compute-image/build")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["digest"] == good_digest, (
            "the unchanged-deps re-trigger overwrote the project's digest pin with "
            "NULL — the compute image is now unpinned"
        )
        assert body["pinned_ref"] == pinned
        assert body["built"] is True

        # And the DB row itself still carries the pin.
        row = (
            await db_session.execute(
                select(ProjectComputeImage)
                .where(
                    ProjectComputeImage.project_id == project_id,
                    ProjectComputeImage.dep_hash == "deadbeef",
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.digest == good_digest
        assert row.pinned_ref == pinned
        assert row.built is True

    @pytest.mark.asyncio
    async def test_a_digest_bearing_rebuild_still_updates_the_pin(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """The preserve-the-pin guard must not freeze a stale digest in place.

        It only refuses to overwrite a good digest with NOTHING; a build that DOES
        resolve a new digest still advances the pin.
        """
        from app.compute_image_build import BuildResult
        from app.models import ProjectComputeImage

        project = await _make_project(db_session, name="ImgBuild Repin")
        registry_repo = "us-docker.pkg.dev/p/compute-images"
        old_digest = "sha256:" + "a" * 64
        new_digest = "sha256:" + "b" * 64
        db_session.add(
            ProjectComputeImage(
                project_id=project.id,
                image_name="proj",
                registry_repo=registry_repo,
                dep_hash="deadbeef",
                tag="deadbeef",
                pinned_ref=f"{registry_repo}/proj@{old_digest}",
                digest=old_digest,
                built=True,
            )
        )
        await db_session.commit()

        async def rebuilt(*a, **k):
            return BuildResult(
                pinned_ref=f"{registry_repo}/proj@{new_digest}",
                tag="deadbeef",
                dep_hash="deadbeef",
                already_present=False,
                built=True,
                digest=new_digest,
            )

        self._patch_pipeline(monkeypatch, build_fn=rebuilt)
        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")

        assert resp.status_code == 200, resp.text
        assert resp.json()["digest"] == new_digest

    @pytest.mark.asyncio
    async def test_an_unconfined_build_network_is_surfaced_as_422(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """A build refused for network confinement is a 422, not a 500 traceback."""
        from app import compute_image_build as _cib

        async def refuses(*a, **k):
            raise _cib.BuildNetworkError(
                "build network 'bridge' can reach the GCP metadata server"
            )

        apt = self._patch_pipeline(monkeypatch, build_fn=refuses)
        project = await _make_project(db_session, name="ImgBuild Net")

        resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")

        assert resp.status_code == 422, resp.text
        assert "metadata server" in resp.text
        assert not apt._compute_image_build_semaphore.locked()


class TestComputeImageStatus:
    """Read-only baked-image status/drift endpoint (owner-gated)."""

    @staticmethod
    def _patch_manifest(monkeypatch, *, compute_image=None):
        """Stub manifest discovery to return the given compute image."""
        from app.routers import admin_project_tools as apt

        async def fake_manifest(_project):
            return {
                "tools": [],
                "skills": [],
                "compute": {"image": compute_image},
                "error": None,
            }

        monkeypatch.setattr(apt, "_discover_manifest_tools", fake_manifest)
        return apt

    @pytest.mark.asyncio
    async def test_status_no_compute_image(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """No compute section in manifest -> no_compute_image_declared."""
        self._patch_manifest(monkeypatch, compute_image=None)
        project = await _make_project(db_session, name="NoCI Project")

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "no_compute_image_declared"}

    @pytest.mark.asyncio
    async def test_status_never_built(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Has compute image but no ProjectComputeImage row -> never_built."""
        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(db_session, name="Unbuilt Project")

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "never_built"}

    @pytest.mark.asyncio
    async def test_status_returns_build_record(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Existing ProjectComputeImage row -> full status with is_digest_pinned."""
        from datetime import timezone
        from app.models import ProjectComputeImage

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(db_session, name="Built Project")
        now = datetime.now(timezone.utc)

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="deadbeef12345678",
            tag="latest",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj@sha256:"
            + "a" * 64,
            digest="sha256:" + "a" * 64,
            built=True,
            created_at=now,
            updated_at=now,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "built"
        assert body["pinned_ref"] == row.pinned_ref
        assert body["digest"] == row.digest
        assert body["dep_hash"] == row.dep_hash
        assert body["tag"] == row.tag
        assert body["built"] is True
        assert body["is_digest_pinned"] is True
        assert body["created_at"] is not None
        assert body["updated_at"] is not None
        # No drift when check_drift not requested.
        assert "drift" not in body

    @pytest.mark.asyncio
    async def test_status_digest_pinned_true(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Row with non-null digest -> is_digest_pinned True."""
        from app.models import ProjectComputeImage

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(db_session, name="Pinned Project")

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="desdhsh12345678",
            tag="desdhsh",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj@sha256:"
            + "b" * 64,
            digest="sha256:" + "b" * 64,
            built=True,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_digest_pinned"] is True

    @pytest.mark.asyncio
    async def test_status_mutable_tag_fallback(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Row with null digest -> is_digest_pinned False (mutable tag fallback)."""
        from app.models import ProjectComputeImage

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(db_session, name="MutableTag Project")

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="cafebabe87654321",
            tag="latest",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj:latest",
            digest=None,
            built=False,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_digest_pinned"] is False
        assert body["digest"] is None
        assert body["status"] == "built"

    @pytest.mark.asyncio
    async def test_status_owner_required(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Member (not owner) -> 403."""
        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(
            db_session, name="Member Project", role="member"
        )

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_status_returns_latest_row(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Multiple rows for same image -> returns the most recent by updated_at."""
        from datetime import timedelta, timezone
        from app.models import ProjectComputeImage

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": None,
            },
        )
        project = await _make_project(db_session, name="MultiRow Project")
        old_now = datetime.now(timezone.utc) - timedelta(days=1)

        old_row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="oldhash",
            tag="oldtag",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj@sha256:"
            + "c" * 64,
            digest="sha256:" + "c" * 64,
            built=True,
            created_at=old_now,
            updated_at=old_now,
        )
        db_session.add(old_row)
        new_row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="newhash",
            tag="newtag",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj@sha256:"
            + "d" * 64,
            digest="sha256:" + "d" * 64,
            built=True,
        )
        db_session.add(new_row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dep_hash"] == "newhash"

    @pytest.mark.asyncio
    async def test_status_drift_matches(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """check_drift=true with matching hashes -> drifted: false."""
        from app.compute_image_build import BuildResult
        from app.models import ProjectComputeImage
        from app.routers import admin_project_tools as apt

        common_hash = "matchhash12345678"

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": {"dockerfile": ".blueprint/Dockerfile.compute"},
            },
        )

        async def mock_build(*a, **k):
            return BuildResult(
                pinned_ref=(
                    "us-docker.pkg.dev/p/compute-images/proj:"
                    + common_hash
                ),
                tag=common_hash,
                dep_hash=common_hash,
                already_present=True,
                built=False,
            )

        monkeypatch.setattr(
            apt._cb, "build_project_compute_image", mock_build
        )

        project = await _make_project(db_session, name="DriftMatch Project")

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash=common_hash,
            tag=common_hash,
            pinned_ref=(
                "us-docker.pkg.dev/p/compute-images/proj@" + common_hash
            ),
            digest="sha256:" + common_hash,
            built=True,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
            "?check_drift=true"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "built"
        drift = body["drift"]
        assert drift["drifted"] is False
        assert drift["current_dep_hash"] == common_hash
        assert drift["stored_dep_hash"] == common_hash

    @pytest.mark.asyncio
    async def test_status_drift_differs(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """check_drift=true with differing hashes -> drifted: true."""
        from app.compute_image_build import BuildResult
        from app.models import ProjectComputeImage
        from app.routers import admin_project_tools as apt

        stored_hash = "storedhash12345"
        current_hash = "currenthash67890"

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": {"dockerfile": ".blueprint/Dockerfile.compute"},
            },
        )

        async def mock_build_diff(*a, **k):
            return BuildResult(
                pinned_ref=(
                    "us-docker.pkg.dev/p/compute-images/proj:"
                    + current_hash
                ),
                tag=current_hash,
                dep_hash=current_hash,
                already_present=True,
                built=False,
            )

        monkeypatch.setattr(
            apt._cb, "build_project_compute_image", mock_build_diff
        )

        project = await _make_project(db_session, name="DriftDiff Project")

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash=stored_hash,
            tag=stored_hash,
            pinned_ref=(
                "us-docker.pkg.dev/p/compute-images/proj@" + stored_hash
            ),
            digest="sha256:" + stored_hash,
            built=True,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
            "?check_drift=true"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        drift = body["drift"]
        assert drift["drifted"] is True
        assert drift["current_dep_hash"] == current_hash
        assert drift["stored_dep_hash"] == stored_hash

    @pytest.mark.asyncio
    async def test_status_drift_no_build_section(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """check_drift=true but no build section -> drifted: null + reason."""
        from app.models import ProjectComputeImage

        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                # No 'build' key.
            },
        )
        project = await _make_project(db_session, name="DriftNoBuild Project")

        row = ProjectComputeImage(
            project_id=project.id,
            image_name="proj",
            registry_repo="us-docker.pkg.dev/p/compute-images",
            dep_hash="somehash",
            tag="latest",
            pinned_ref="us-docker.pkg.dev/p/compute-images/proj:latest",
            digest=None,
            built=False,
        )
        db_session.add(row)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
            "?check_drift=true"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["drift"]["drifted"] is None
        assert "reason" in body["drift"]

    @pytest.mark.asyncio
    async def test_status_drift_never_built(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """check_drift=true but no build record -> status never_built, no drift."""
        self._patch_manifest(
            monkeypatch,
            compute_image={
                "name": "us-docker.pkg.dev/p/compute-images/proj:latest",
                "build": {"dockerfile": ".blueprint/Dockerfile.compute"},
            },
        )
        project = await _make_project(
            db_session, name="DriftNeverBuilt Project"
        )

        resp = await client.get(
            f"/api/admin/projects/{project.id}/compute-image/status"
            "?check_drift=true"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "never_built"
        assert "drift" not in body
